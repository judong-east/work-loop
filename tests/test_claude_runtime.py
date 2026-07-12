from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.claude_code import ClaudeCodeProfile, ClaudeCodeRuntime
from app.agents.codex_cli import CodexCliProfile, CodexCliRuntime
from app.agents.claude_protocol import (
    ClaudeProtocolState,
    execution_plan_schema,
    review_result_schema,
)
from app.agents.contracts import (
    AgentAccess,
    AgentBudget,
    AgentEventType,
    AgentPolicy,
    AgentRequest,
    AgentTaskStatus,
    ExecutionPlan,
    ValidationResult,
)
from app.agents.runtime import RoleRoutedRuntime
from app.agents.workflow import AgentWorkflow
from tests.git_support import create_repository
from tests.test_codex_runtime import FAKE_CODEX


PLANNER_SESSION = "11111111-1111-4111-8111-111111111111"
REVIEWER_SESSION = "22222222-2222-4222-8222-222222222222"


FAKE_CLAUDE = r'''
import json
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
args = sys.argv[2:]
if "--version" in args:
    print("2.1.126 (Claude Code)")
    raise SystemExit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "oauth_token", "apiProvider": "firstParty"}))
    raise SystemExit(0)

prompt = sys.stdin.read()
schema = json.loads(args[args.index("--json-schema") + 1])
is_planner = "requirement_understanding" in schema["properties"]
existing = []
if log_path.exists():
    existing = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
role_runs = [item for item in existing if item.get("role") == ("planner" if is_planner else "reviewer")]
session_id = "11111111-1111-4111-8111-111111111111" if is_planner else "22222222-2222-4222-8222-222222222222"
record = {"args": args, "stdin": prompt, "role": "planner" if is_planner else "reviewer"}
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\n")

if is_planner:
    output = {
        "requirement_understanding": "Add a result file",
        "non_goals": [],
        "files_and_symbols": ["result.txt"],
        "steps": ["Write result.txt"],
        "constraints": ["Keep the change scoped"],
        "acceptance_criteria": ["result.txt contains done"],
        "required_tests": ["fake-check"],
        "risks": [],
        "open_questions": [],
    }
elif not role_runs:
    output = {
        "verdict": "revise_code",
        "acceptance": [{"criterion": "result.txt contains done", "passed": False}],
        "issues": [{
            "file": "result.txt", "line": 1, "severity": "warning",
            "message": "Content is not done", "suggestion": "Replace it with done",
            "evidence": "The first line is first",
        }],
        "recommended_tests": ["fake-check"],
        "summary": "Revision required.",
    }
else:
    output = {
        "verdict": "pass",
        "acceptance": [{"criterion": "result.txt contains done", "passed": True}],
        "issues": [],
        "recommended_tests": [],
        "summary": "All acceptance criteria pass.",
    }

events = [
    {"type": "system", "subtype": "init", "session_id": session_id,
     "tools": ["Read", "Glob", "Grep"], "mcp_servers": [], "model": "claude-test",
     "permissionMode": "plan", "claude_code_version": "2.1.126", "api_key": "vendor-secret"},
    {"type": "assistant", "message": {"role": "assistant",
     "content": [{"type": "text", "text": "finished"}],
     "usage": {"input_tokens": 10, "output_tokens": 5}}, "session_id": session_id},
    {"type": "result", "subtype": "success", "is_error": False,
     "result": "finished", "session_id": session_id, "total_cost_usd": 0.01,
     "usage": {"input_tokens": 10, "output_tokens": 5}, "structured_output": output},
]
for event in events:
    print(json.dumps(event), flush=True)
'''


LOGGED_OUT_CLAUDE = r'''
import json
import sys
if "--version" in sys.argv:
    print("2.1.126 (Claude Code)")
elif sys.argv[-2:] == ["auth", "status"]:
    print(json.dumps({"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}))
else:
    raise SystemExit(2)
'''


SLOW_CLAUDE = r'''
import json
import subprocess
import sys
import time
from pathlib import Path

marker = Path(sys.argv[1])
late = Path(sys.argv[2])
if "--version" in sys.argv:
    print("2.1.126 (Claude Code)")
    raise SystemExit(0)
if sys.argv[-2:] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True}))
    raise SystemExit(0)
marker.write_text("started", encoding="utf-8")
subprocess.Popen([sys.executable, "-c", "import pathlib,time,sys; time.sleep(2); pathlib.Path(sys.argv[1]).write_text('late')", str(late)])
print(json.dumps({"type": "system", "subtype": "init", "session_id": "11111111-1111-4111-8111-111111111111"}), flush=True)
time.sleep(20)
'''


SILENT_CLAUDE = r'''
import json
import sys
import time
if "--version" in sys.argv:
    print("2.1.126 (Claude Code)")
    raise SystemExit(0)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "11111111-1111-4111-8111-111111111111"}), flush=True)
time.sleep(20)
'''


INVALID_OUTPUT_CLAUDE = r'''
import json
import sys
if "--version" in sys.argv:
    print("2.1.126 (Claude Code)")
    raise SystemExit(0)
session_id = "11111111-1111-4111-8111-111111111111"
print(json.dumps({"type": "system", "subtype": "init", "session_id": session_id}), flush=True)
print("api_key=malformed-stream-secret", flush=True)
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "session_id": session_id, "result": "invalid",
    "structured_output": {"steps": []},
}), flush=True)
'''


class ClaudeCodeRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.log = self.root / "claude-invocations.jsonl"
        self.script = self.root / "fake_claude.py"
        self.script.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.runtime = ClaudeCodeRuntime(
            ClaudeCodeProfile(
                command=[sys.executable, str(self.script), str(self.log)],
                model="claude-test",
            )
        )

    def request(
        self,
        role: str = "planner",
        instructions: str = "inspect the worktree; api_key=prompt-secret",
        session_id: str = "",
    ) -> AgentRequest:
        return AgentRequest(
            task_id="TASK-claude",
            role=role,
            instructions=instructions,
            workspace=self.workspace,
            access=AgentAccess.READ_ONLY,
            policy=AgentPolicy(network_allowed=False),
            budget=AgentBudget(
                total_timeout_seconds=10,
                idle_timeout_seconds=3,
                max_cost_usd=0.25,
            ),
            session_id=session_id,
        )

    def invocations(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text("utf-8").splitlines()]

    def test_protocol_fixtures_normalize_events_and_structured_results(self) -> None:
        fixture_root = Path(__file__).parent / "fixtures" / "claude"
        cases = [
            ("planner_success.jsonl", "planner", AgentEventType.COMPLETED),
            ("reviewer_revise.jsonl", "reviewer", AgentEventType.COMPLETED),
            ("reviewer_pass.jsonl", "reviewer", AgentEventType.COMPLETED),
        ]
        for filename, role, terminal in cases:
            with self.subTest(filename=filename):
                state = ClaudeProtocolState(role)
                for line in (fixture_root / filename).read_text("utf-8").splitlines():
                    state.consume(json.loads(line))
                result = state.finish(return_code=0, stderr="")
                self.assertTrue(result.succeeded, result.error)
                self.assertEqual(result.events[-1].event_type, terminal)
                self.assertTrue(result.session_id)
                self.assertTrue(result.output)

    def test_protocol_accepts_strict_json_result_when_relay_omits_structured_output(self) -> None:
        state = ClaudeProtocolState("planner")
        output = {
            "requirement_understanding": "Inspect the repository",
            "non_goals": [],
            "files_and_symbols": ["README.md"],
            "steps": ["Read README.md"],
            "constraints": ["Do not modify files"],
            "acceptance_criteria": ["Repository is inspected"],
            "required_tests": ["fake-check"],
            "risks": [],
            "open_questions": [],
        }
        state.consume(
            {
                "type": "system",
                "subtype": "init",
                "session_id": PLANNER_SESSION,
            }
        )
        state.consume(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": PLANNER_SESSION,
                "result": json.dumps(output),
            }
        )

        result = state.finish(return_code=0, stderr="")

        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(result.output, output)

    def test_protocol_normalizes_auditable_native_reviewer_pass(self) -> None:
        criteria = ["Metrics endpoint returns grouped counts", "Full tests pass"]
        state = ClaudeProtocolState("reviewer", acceptance_criteria=criteria)
        state.consume(
            {"type": "system", "subtype": "init", "session_id": REVIEWER_SESSION}
        )
        state.consume(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": REVIEWER_SESSION,
                "result": json.dumps(
                    {
                        "schema_version": 1,
                        "review_type": "full",
                        "requirement": "Add metrics",
                        "test_results": [
                            {"name": "full-tests", "status": "passed", "exit_code": 0}
                        ],
                        "files": [],
                        "summary": "All acceptance criteria are satisfied.",
                        "approved": True,
                        "comments": [],
                    }
                ),
            }
        )

        result = state.finish(return_code=0, stderr="")

        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(result.output["verdict"], "pass")
        self.assertEqual(
            result.output["acceptance"],
            [{"criterion": item, "passed": True} for item in criteria],
        )

    def test_protocol_rejects_non_json_relay_result_without_structured_output(self) -> None:
        state = ClaudeProtocolState("planner")
        state.consume(
            {
                "type": "system",
                "subtype": "init",
                "session_id": PLANNER_SESSION,
            }
        )
        state.consume(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": PLANNER_SESSION,
                "result": "not json",
            }
        )

        result = state.finish(return_code=0, stderr="")

        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_type, "structured_output_failed")
        self.assertIn("不是合法 JSON", result.error)

    def test_protocol_accepts_full_json_code_fence_from_relay(self) -> None:
        state = ClaudeProtocolState("planner")
        output = {
            "requirement_understanding": "Inspect the repository",
            "non_goals": [],
            "files_and_symbols": ["README.md"],
            "steps": ["Read README.md"],
            "constraints": ["Do not modify files"],
            "acceptance_criteria": ["Repository is inspected"],
            "required_tests": ["fake-check"],
            "risks": [],
            "open_questions": [],
        }
        state.consume(
            {"type": "system", "subtype": "init", "session_id": PLANNER_SESSION}
        )
        state.consume(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": PLANNER_SESSION,
                "result": "```json\n" + json.dumps(output) + "\n```",
            }
        )

        result = state.finish(return_code=0, stderr="")

        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(result.output, output)

    def test_protocol_preserves_native_planner_output_for_host_normalization(self) -> None:
        state = ClaudeProtocolState("planner")
        output = {
            "title": "Add metrics",
            "plan": {"steps": [{"description": "Run fake-check"}]},
            "requirements": {
                "acceptance_criteria": ["Metrics are visible"],
                "clarifications": [],
            },
            "risks": [],
            "issues": [],
        }
        state.consume(
            {"type": "system", "subtype": "init", "session_id": PLANNER_SESSION}
        )
        state.consume(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": PLANNER_SESSION,
                "result": json.dumps(output),
            }
        )

        result = state.finish(return_code=0, stderr="")

        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(result.output, output)

    def test_invokes_planner_with_stdin_read_only_schema_and_normalized_events(self) -> None:
        instructions = "inspect the worktree; api_key=prompt-secret"

        result = self.runtime.invoke(self.request(instructions=instructions))

        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(result.session_id, PLANNER_SESSION)
        self.assertEqual(result.output["required_tests"], ["fake-check"])
        self.assertEqual(result.runtime, "claude-code")
        self.assertEqual(result.runtime_version, "2.1.126")
        self.assertEqual(result.model, "claude-test")
        self.assertEqual(result.usage["input_tokens"], 10)
        self.assertEqual(result.usage["total_cost_usd"], 0.01)
        self.assertEqual(result.runtime_config["permission_mode"], "dontAsk")
        self.assertEqual(result.runtime_config["tools"], ["Read", "Glob", "Grep"])
        self.assertNotIn("vendor-secret", json.dumps(result.raw_events))
        self.assertEqual(result.raw_events[0]["api_key"], "[REDACTED]")
        terminals = [
            event.event_type
            for event in result.events
            if event.event_type
            in {AgentEventType.COMPLETED, AgentEventType.FAILED, AgentEventType.CANCELLED}
        ]
        self.assertEqual(terminals, [AgentEventType.COMPLETED])

        invocation = self.invocations()[0]
        args = invocation["args"]
        self.assertEqual(invocation["stdin"], instructions)
        self.assertNotIn(instructions, args)
        self.assertIn("--print", args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep")
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--setting-sources") + 1], "")
        inline_settings = json.loads(args[args.index("--settings") + 1])
        self.assertTrue(inline_settings["disableAllHooks"])
        self.assertFalse(inline_settings["enableAllProjectMcpServers"])
        self.assertEqual(inline_settings["enabledPlugins"], {})
        self.assertEqual(args[args.index("--max-budget-usd") + 1], "0.25")
        schema = json.loads(args[args.index("--json-schema") + 1])
        self.assertEqual(schema, execution_plan_schema())
        for forbidden in (
            "Bash",
            "Edit",
            "Write",
            "WebSearch",
            "WebFetch",
            "--add-dir",
            "--dangerously-skip-permissions",
            "--allow-dangerously-skip-permissions",
        ):
            self.assertNotIn(forbidden, args)

    def test_only_allowlisted_auth_environment_is_loaded_from_user_settings(self) -> None:
        settings_dir = self.root / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_AUTH_TOKEN": "relay-secret",
                        "ANTHROPIC_BASE_URL": "https://relay.example.test",
                        "UNRELATED_SECRET": "must-not-be-loaded",
                    },
                    "hooks": {"PreToolUse": ["unsafe"]},
                }
            ),
            encoding="utf-8",
        )
        with patch("app.agents.claude_code.Path.home", return_value=self.root):
            runtime = ClaudeCodeRuntime(
                ClaudeCodeProfile(
                    command=[sys.executable, str(self.script), str(self.log)],
                    model="claude-test",
                )
            )
            environment = runtime._authentication_environment()
            config = runtime.describe(self.request())["config"]

        self.assertEqual(
            set(environment),
            {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"},
        )
        self.assertNotIn("relay-secret", json.dumps(config))
        self.assertEqual(
            config["authentication_env_keys"],
            ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"],
        )
        self.assertEqual(config["setting_sources"], [])

    def test_reviewer_uses_review_schema_and_resumes_exact_session(self) -> None:
        first = self.runtime.invoke(self.request(role="reviewer", instructions="review round one"))
        second = self.runtime.invoke(
            self.request(
                role="reviewer",
                instructions="review round two",
                session_id=first.session_id,
            )
        )

        self.assertTrue(first.succeeded, first.error)
        self.assertEqual(first.output["verdict"], "revise_code")
        self.assertTrue(second.succeeded, second.error)
        self.assertEqual(second.output["verdict"], "pass")
        self.assertEqual(second.session_id, REVIEWER_SESSION)
        invocation = self.invocations()[1]
        args = invocation["args"]
        self.assertIn("--resume", args)
        self.assertEqual(args[args.index("--resume") + 1], REVIEWER_SESSION)
        schema = json.loads(args[args.index("--json-schema") + 1])
        self.assertEqual(schema, review_result_schema())
        self.assertEqual(invocation["stdin"], "review round two")

    def test_rejects_write_network_wrong_role_and_launcher_permission_arguments(self) -> None:
        write_request = self.request()
        write_request.access = AgentAccess.WORKSPACE_WRITE
        network_request = self.request()
        network_request.policy.network_allowed = True
        executor_request = self.request(role="executor")

        for request in (write_request, network_request, executor_request):
            with self.subTest(request=request):
                result = self.runtime.invoke(request)
                self.assertFalse(result.succeeded)
                self.assertIn(result.error_type, {"policy_blocked", "permission_required"})
                self.assertEqual(result.events[-1].event_type, AgentEventType.FAILED)

        for argument in ("--dangerously-skip-permissions", "--add-dir", "-p"):
            with self.subTest(argument=argument), self.assertRaisesRegex(ValueError, "权限参数"):
                ClaudeCodeRuntime(
                    ClaudeCodeProfile(command=["claude", argument], model="claude-test")
                )

    def test_health_check_maps_logged_out_status_without_model_call(self) -> None:
        script = self.root / "logged_out_claude.py"
        script.write_text(LOGGED_OUT_CLAUDE, encoding="utf-8")
        runtime = ClaudeCodeRuntime(
            ClaudeCodeProfile(command=[sys.executable, str(script)], model="claude-test")
        )

        health = runtime.health_check()

        self.assertFalse(health["available"])
        self.assertFalse(health["authenticated"])
        self.assertEqual(health["runtime_version"], "2.1.126")
        self.assertEqual(health["error_type"], "authentication_failed")

    def test_malformed_stream_and_invalid_output_fail_with_one_terminal_event(self) -> None:
        script = self.root / "invalid_output_claude.py"
        script.write_text(INVALID_OUTPUT_CLAUDE, encoding="utf-8")
        runtime = ClaudeCodeRuntime(
            ClaudeCodeProfile(command=[sys.executable, str(script)], model="claude-test")
        )

        result = runtime.invoke(self.request())

        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_type, "structured_output_failed")
        self.assertNotIn("malformed-stream-secret", result.error)
        self.assertIn("[REDACTED]", result.error)
        terminals = [
            event.event_type
            for event in result.events
            if event.event_type
            in {AgentEventType.COMPLETED, AgentEventType.FAILED, AgentEventType.CANCELLED}
        ]
        self.assertEqual(terminals, [AgentEventType.FAILED])

    def test_idle_timeout_terminates_claude_after_last_event(self) -> None:
        script = self.root / "silent_claude.py"
        script.write_text(SILENT_CLAUDE, encoding="utf-8")
        runtime = ClaudeCodeRuntime(
            ClaudeCodeProfile(command=[sys.executable, str(script)], model="claude-test")
        )
        request = self.request()
        request.budget = AgentBudget(total_timeout_seconds=10, idle_timeout_seconds=0.25)

        started = time.monotonic()
        result = runtime.invoke(request)

        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_type, "idle_timeout")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result.events[-1].event_type, AgentEventType.FAILED)

    def test_total_timeout_includes_version_probe(self) -> None:
        marker = self.root / "agent-started-after-version.txt"
        script = self.root / "slow_version_claude.py"
        script.write_text(
            "\n".join(
                [
                    "import sys, time",
                    "from pathlib import Path",
                    "if '--version' in sys.argv:",
                    "    time.sleep(5)",
                    "    print('2.1.126 (Claude Code)')",
                    "    raise SystemExit(0)",
                    f"Path({str(marker)!r}).write_text('started')",
                ]
            ),
            encoding="utf-8",
        )
        runtime = ClaudeCodeRuntime(
            ClaudeCodeProfile(command=[sys.executable, str(script)], model="claude-test")
        )
        request = self.request()
        request.budget = AgentBudget(total_timeout_seconds=0.3, idle_timeout_seconds=10)

        started = time.monotonic()
        result = runtime.invoke(request)

        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_type, "call_timeout")
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(marker.exists())

    def test_cancel_terminates_process_tree_and_returns_single_cancelled_event(self) -> None:
        marker = self.root / "slow-started.txt"
        late = self.root / "late-grandchild.txt"
        script = self.root / "slow_claude.py"
        script.write_text(SLOW_CLAUDE, encoding="utf-8")
        runtime = ClaudeCodeRuntime(
            ClaudeCodeProfile(
                command=[sys.executable, str(script), str(marker), str(late)],
                model="claude-test",
            )
        )
        result: dict[str, object] = {}
        thread = threading.Thread(
            target=lambda: result.setdefault("value", runtime.invoke(self.request()))
        )
        thread.start()
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists())

        self.assertTrue(runtime.cancel("TASK-claude"))
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        time.sleep(2.5)

        response = result["value"]
        self.assertFalse(response.succeeded)
        self.assertEqual(response.error_type, "user_cancelled")
        terminals = [
            event.event_type
            for event in response.events
            if event.event_type
            in {AgentEventType.COMPLETED, AgentEventType.FAILED, AgentEventType.CANCELLED}
        ]
        self.assertEqual(terminals, [AgentEventType.CANCELLED])
        self.assertFalse(late.exists())

    def test_role_router_completes_claude_codex_revise_workflow(self) -> None:
        class PassingValidator:
            def validate(self, task_id, workspace, plan: ExecutionPlan, policy):
                return ValidationResult(
                    passed=True,
                    checks=[
                        {
                            "command": "fake-check",
                            "exit_code": 0,
                            "stdout": "ok",
                            "stderr": "",
                        }
                    ],
                )

        codex_script = self.root / "fake_codex.py"
        codex_script.write_text(FAKE_CODEX, encoding="utf-8")
        codex = CodexCliRuntime(
            CodexCliProfile(
                command=[sys.executable, str(codex_script)],
                model="codex-test",
            )
        )
        routed = RoleRoutedRuntime(
            {
                "planner": self.runtime,
                "executor": codex,
                "reviewer": self.runtime,
            }
        )
        repository = create_repository(self.root)
        workflow_root = self.root / "workloop-data"
        workflow = AgentWorkflow(
            workflow_root,
            runtime=routed,
            validator=PassingValidator(),
        )
        project = workflow.register_project("Claude Codex", repository, "main")
        task = workflow.create_task("Complete loop", "Create result.txt", project.project_id)

        planned = workflow.analyze(task.task_id)
        completed = workflow.approve_plan(task.task_id)

        self.assertEqual(planned.status, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
        self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER, completed.error)
        self.assertEqual(completed.iteration, 2)
        self.assertEqual(completed.sessions["planner"], PLANNER_SESSION)
        self.assertEqual(completed.sessions["reviewer"], REVIEWER_SESSION)
        self.assertNotEqual(completed.sessions["planner"], completed.sessions["reviewer"])
        invocations = self.invocations()
        self.assertEqual(
            [invocation["role"] for invocation in invocations],
            ["planner", "reviewer", "reviewer"],
        )
        final_review_args = invocations[-1]["args"]
        self.assertEqual(
            final_review_args[final_review_args.index("--resume") + 1],
            REVIEWER_SESSION,
        )
        final_review_run = json.loads(
            (
                workflow_root
                / "tasks"
                / task.task_id
                / "artifacts/runs/5-reviewer.json"
            ).read_text("utf-8")
        )
        self.assertEqual(final_review_run["runtime"], "claude-code")
        self.assertEqual(final_review_run["session_id"], REVIEWER_SESSION)
        self.assertEqual(final_review_run["output"]["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
