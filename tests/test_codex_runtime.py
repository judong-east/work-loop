from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.agents.codex_cli import CodexCliProfile, CodexCliRuntime
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
from app.agents.fake_runtime import FakeAgentStep, ScriptedFakeRuntime
from app.agents.runtime import RoleRoutedRuntime
from app.agents.workflow import AgentWorkflow
from tests.git_support import create_repository


FAKE_CODEX = r'''
import json
import sys
from pathlib import Path

if "--version" in sys.argv:
    print("codex-cli fake-1.2.3")
    raise SystemExit(0)
if sys.argv[-2:] == ["login", "status"]:
    print("Logged in")
    raise SystemExit(0)

args = sys.argv[1:]
prompt = sys.stdin.read()
Path("codex-invocation.json").write_text(
    json.dumps({"args": args, "stdin": prompt}, ensure_ascii=True),
    encoding="utf-8",
)
Path("result.txt").write_text("done\n", encoding="utf-8")
output_path = Path(args[args.index("--output-last-message") + 1])
final = {
    "completed_steps": ["implemented"],
    "modified_files": ["result.txt"],
    "tests": [],
    "deviations": [],
    "remaining_risks": [],
    "next_steps": [],
}
output_path.write_text(json.dumps(final), encoding="utf-8")
events = [
    {"type": "thread.started", "thread_id": "session-123", "api_key": "raw-key-secret"},
    {"type": "turn.started"},
    {
        "type": "item.started",
        "item": {"id": "item-1", "type": "command_execution", "command": "write result TOKEN=vendor-secret"},
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item-1",
            "type": "command_execution",
            "command": "write result TOKEN=vendor-secret",
            "exit_code": 0,
            "status": "completed",
        },
    },
    {
        "type": "item.completed",
        "item": {"id": "item-2", "type": "agent_message", "text": json.dumps(final)},
    },
    {
        "type": "turn.completed",
        "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 45},
    },
]
for event in events:
    print(json.dumps(event), flush=True)
'''

SLOW_FAKE_CODEX = r'''
import json
import subprocess
import sys
import time
from pathlib import Path

if "--version" in sys.argv:
    print("codex-cli fake-slow")
    raise SystemExit(0)

Path("slow-codex-started.json").write_text(json.dumps({"args": sys.argv[1:]}))
print(json.dumps({"type": "thread.started", "thread_id": "slow-session"}), flush=True)
marker = Path("late-grandchild.txt").resolve()
child = (
    "import time; from pathlib import Path; "
    f"time.sleep(2); Path({str(marker)!r}).write_text('late', encoding='utf-8')"
)
subprocess.Popen([sys.executable, "-c", child])
time.sleep(30)
'''

SILENT_FAKE_CODEX = r'''
import sys
import time

if "--version" in sys.argv:
    print("codex-cli fake-silent")
    raise SystemExit(0)
while True:
    print("diagnostic without JSONL event", file=sys.stderr, flush=True)
    time.sleep(0.1)
'''

BROKEN_FAKE_CODEX = r'''
import json
import sys
from pathlib import Path

if "--version" in sys.argv:
    print("codex-cli fake-broken")
    raise SystemExit(0)
args = sys.argv[1:]
output_path = Path(args[args.index("--output-last-message") + 1])
output_path.write_text(json.dumps({"completed_steps": [], "modified_files": [], "tests": [], "deviations": [], "remaining_risks": [], "next_steps": []}))
print(json.dumps({"type": "thread.started", "thread_id": "broken-session"}), flush=True)
print("this is not json", flush=True)
'''

EOF_FAKE_CODEX = r'''
import os
import sys
import time

if "--version" in sys.argv:
    print("codex-cli fake-eof")
    raise SystemExit(0)
os.close(sys.stdout.fileno())
os.close(sys.stderr.fileno())
time.sleep(30)
'''

LEAKING_BROKEN_FAKE_CODEX = r'''
import sys

if "--version" in sys.argv:
    print("codex-cli fake-leaking")
    raise SystemExit(0)
print('{"api_key":"raw-json-secret"', flush=True)
'''

INVALID_OUTPUT_FAKE_CODEX = r'''
import json
import sys
from pathlib import Path

if "--version" in sys.argv:
    print("codex-cli fake-invalid-output")
    raise SystemExit(0)
args = sys.argv[1:]
output_path = Path(args[args.index("--output-last-message") + 1])
output_path.write_text("[]", encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": "invalid-output-session"}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}), flush=True)
'''

LOGGED_OUT_FAKE_CODEX = r'''
import sys

if "--version" in sys.argv:
    print("codex-cli fake-logged-out")
    raise SystemExit(0)
if sys.argv[-2:] == ["login", "status"]:
    print("Not logged in", file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(2)
'''


class CodexCliRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.script = self.root / "fake_codex.py"
        self.script.write_text(FAKE_CODEX, encoding="utf-8")
        self.runtime = CodexCliRuntime(
            CodexCliProfile(
                command=[sys.executable, str(self.script)],
                model="gpt-test",
            )
        )

    def request(self, instructions: str, session_id: str = "") -> AgentRequest:
        return AgentRequest(
            task_id="TASK-codex",
            role="executor",
            instructions=instructions,
            workspace=self.workspace,
            access=AgentAccess.WORKSPACE_WRITE,
            policy=AgentPolicy(
                allowed_commands=[[sys.executable, "-m", "unittest"]],
                protected_paths=[".env"],
                timeout_seconds=30,
                network_allowed=False,
            ),
            budget=AgentBudget(total_timeout_seconds=10, idle_timeout_seconds=3),
            session_id=session_id,
        )

    def invocation(self) -> dict:
        return json.loads((self.workspace / "codex-invocation.json").read_text("utf-8"))

    def test_rejects_launcher_arguments_that_override_workloop_permissions(self) -> None:
        dangerous_arguments = [
            "--dangerously-bypass-approvals-and-sandbox",
            "--enable",
            "--oss",
            "-csandbox_permissions=['disk-full-read-access']",
            "-sdanger-full-access",
            "-C..",
        ]
        for argument in dangerous_arguments:
            with self.subTest(argument=argument), self.assertRaisesRegex(ValueError, "权限参数"):
                CodexCliRuntime(
                    CodexCliProfile(
                        command=["codex", argument],
                        model="gpt-test",
                    )
                )

    def test_health_check_maps_authentication_status(self) -> None:
        logged_out_script = self.root / "logged_out_codex.py"
        logged_out_script.write_text(LOGGED_OUT_FAKE_CODEX, encoding="utf-8")
        runtime = CodexCliRuntime(
            CodexCliProfile(
                command=[sys.executable, str(logged_out_script)],
                model="gpt-test",
            )
        )

        health = runtime.health_check()

        self.assertFalse(health["available"])
        self.assertFalse(health["authenticated"])
        self.assertEqual(health["runtime_version"], "fake-logged-out")
        self.assertEqual(health["error_type"], "authentication_failed")

    def test_invokes_codex_with_stdin_schema_and_normalized_jsonl_events(self) -> None:
        instructions = "implement the approved plan; secret prompt text"

        health = self.runtime.health_check()
        result = self.runtime.invoke(self.request(instructions))

        self.assertTrue(health["available"], health["error"])
        self.assertTrue(health["authenticated"])
        self.assertEqual(health["runtime_version"], "fake-1.2.3")
        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(result.session_id, "session-123")
        self.assertEqual(result.output["modified_files"], ["result.txt"])
        self.assertEqual(result.runtime, "codex-cli")
        self.assertEqual(result.runtime_version, "fake-1.2.3")
        self.assertEqual(result.model, "gpt-test")
        self.assertEqual(result.usage["input_tokens"], 120)
        self.assertEqual(result.usage["output_tokens"], 45)
        self.assertEqual(result.runtime_config["approval_policy"], "never")
        self.assertEqual(result.runtime_config["sandbox"], "workspace-write")
        self.assertNotIn("vendor-secret", json.dumps(result.raw_events))
        self.assertNotIn("raw-key-secret", json.dumps(result.raw_events))
        self.assertEqual(result.raw_events[0]["api_key"], "[REDACTED]")
        self.assertEqual(
            [event.event_type for event in result.events],
            [
                AgentEventType.SESSION_STARTED,
                AgentEventType.HEARTBEAT,
                AgentEventType.TOOL_STARTED,
                AgentEventType.TOOL_COMPLETED,
                AgentEventType.MESSAGE_DELTA,
                AgentEventType.USAGE_UPDATED,
                AgentEventType.COMPLETED,
            ],
        )
        self.assertEqual(len(result.raw_events), 6)

        invocation = self.invocation()
        args = invocation["args"]
        self.assertEqual(invocation["stdin"], instructions)
        self.assertNotIn(instructions, args)
        self.assertIn("--ask-for-approval", args)
        self.assertEqual(args[args.index("--ask-for-approval") + 1], "never")
        self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
        self.assertIn("sandbox_workspace_write.network_access=false", args)
        self.assertEqual(args[args.index("--cd") + 1], str(self.workspace))
        self.assertIn("--json", args)
        self.assertIn("--output-schema", args)
        self.assertEqual(args[-1], "-")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)

    def test_resumes_exact_codex_session_and_still_reads_instructions_from_stdin(self) -> None:
        instructions = "apply review feedback"

        result = self.runtime.invoke(self.request(instructions, session_id="session-existing"))

        self.assertTrue(result.succeeded, result.error)
        invocation = self.invocation()
        args = invocation["args"]
        self.assertEqual(invocation["stdin"], instructions)
        self.assertNotIn(instructions, args)
        self.assertIn("resume", args)
        self.assertIn("session-existing", args)
        self.assertEqual(args[-1], "-")

    def test_cancel_terminates_codex_process_tree_and_returns_cancelled_result(self) -> None:
        slow_script = self.root / "slow_fake_codex.py"
        slow_script.write_text(SLOW_FAKE_CODEX, encoding="utf-8")
        runtime = CodexCliRuntime(
            CodexCliProfile(command=[sys.executable, str(slow_script)], model="gpt-test")
        )
        result: dict[str, object] = {}

        thread = threading.Thread(
            target=lambda: result.setdefault("value", runtime.invoke(self.request("slow task")))
        )
        thread.start()
        started = self.workspace / "slow-codex-started.json"
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(started.exists())

        duplicate = runtime.invoke(self.request("duplicate task"))

        self.assertFalse(duplicate.succeeded)
        self.assertEqual(duplicate.error_type, "policy_blocked")
        self.assertEqual(duplicate.events[-1].event_type, AgentEventType.FAILED)

        self.assertTrue(runtime.cancel("TASK-codex"))
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        time.sleep(2.5)

        response = result["value"]
        self.assertFalse(response.succeeded)
        self.assertEqual(response.error_type, "user_cancelled")
        self.assertEqual(response.events[-1].event_type, AgentEventType.CANCELLED)
        self.assertFalse((self.workspace / "late-grandchild.txt").exists())

    def test_idle_event_budget_terminates_silent_codex_run(self) -> None:
        silent_script = self.root / "silent_fake_codex.py"
        silent_script.write_text(SILENT_FAKE_CODEX, encoding="utf-8")
        runtime = CodexCliRuntime(
            CodexCliProfile(command=[sys.executable, str(silent_script)], model="gpt-test")
        )
        request = self.request("silent task")
        request.budget = AgentBudget(total_timeout_seconds=10, idle_timeout_seconds=1)

        started = time.monotonic()
        result = runtime.invoke(request)
        elapsed = time.monotonic() - started

        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_type, "idle_timeout")
        self.assertLess(elapsed, 5)
        self.assertEqual(result.events[-1].event_type, AgentEventType.FAILED)

    def test_malformed_or_truncated_jsonl_cannot_report_success(self) -> None:
        broken_script = self.root / "broken_fake_codex.py"
        broken_script.write_text(BROKEN_FAKE_CODEX, encoding="utf-8")
        runtime = CodexCliRuntime(
            CodexCliProfile(command=[sys.executable, str(broken_script)], model="gpt-test")
        )

        result = runtime.invoke(self.request("broken protocol"))

        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_type, "structured_output_failed")
        self.assertIn("JSONL", result.error)
        self.assertEqual(result.events[-1].event_type, AgentEventType.FAILED)

    def test_total_budget_applies_after_streams_close_before_process_exit(self) -> None:
        eof_script = self.root / "eof_fake_codex.py"
        eof_script.write_text(EOF_FAKE_CODEX, encoding="utf-8")
        runtime = CodexCliRuntime(
            CodexCliProfile(command=[sys.executable, str(eof_script)], model="gpt-test")
        )
        request = self.request("process outlives streams")
        request.budget = AgentBudget(total_timeout_seconds=1, idle_timeout_seconds=10)

        started = time.monotonic()
        result = runtime.invoke(request)
        elapsed = time.monotonic() - started

        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_type, "call_timeout")
        self.assertLess(elapsed, 3)

    def test_malformed_jsonl_and_sensitive_keys_are_redacted(self) -> None:
        leaking_script = self.root / "leaking_broken_fake_codex.py"
        leaking_script.write_text(LEAKING_BROKEN_FAKE_CODEX, encoding="utf-8")
        runtime = CodexCliRuntime(
            CodexCliProfile(command=[sys.executable, str(leaking_script)], model="gpt-test")
        )

        result = runtime.invoke(self.request("broken protocol"))

        self.assertFalse(result.succeeded)
        self.assertNotIn("raw-json-secret", result.error)
        self.assertIn("[REDACTED]", result.error)
        self.assertEqual(result.events[-1].event_type, AgentEventType.FAILED)

    def test_structured_output_failure_replaces_provider_completed_terminal_event(self) -> None:
        invalid_script = self.root / "invalid_output_fake_codex.py"
        invalid_script.write_text(INVALID_OUTPUT_FAKE_CODEX, encoding="utf-8")
        runtime = CodexCliRuntime(
            CodexCliProfile(command=[sys.executable, str(invalid_script)], model="gpt-test")
        )

        result = runtime.invoke(self.request("invalid final output"))

        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_type, "structured_output_failed")
        terminals = [
            event.event_type
            for event in result.events
            if event.event_type
            in {AgentEventType.COMPLETED, AgentEventType.FAILED, AgentEventType.CANCELLED}
        ]
        self.assertEqual(terminals, [AgentEventType.FAILED])

    def test_cancel_during_version_probe_prevents_codex_process_start(self) -> None:
        marker = self.workspace / "agent-process-started.txt"
        slow_version = self.root / "slow_version_codex.py"
        slow_version.write_text(
            "\n".join(
                [
                    "import sys, time",
                    "from pathlib import Path",
                    "if '--version' in sys.argv:",
                    "    time.sleep(1)",
                    "    print('codex-cli slow-version')",
                    "    raise SystemExit(0)",
                    f"Path({str(marker)!r}).write_text('started')",
                ]
            ),
            encoding="utf-8",
        )
        runtime = CodexCliRuntime(
            CodexCliProfile(command=[sys.executable, str(slow_version)], model="gpt-test")
        )
        result: dict[str, object] = {}
        thread = threading.Thread(
            target=lambda: result.setdefault("value", runtime.invoke(self.request("cancel early")))
        )
        thread.start()
        deadline = time.monotonic() + 2
        cancelled = False
        while time.monotonic() < deadline and not cancelled:
            cancelled = runtime.cancel("TASK-codex")
            if not cancelled:
                time.sleep(0.01)

        self.assertTrue(cancelled)
        cancelled_at = time.monotonic()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - cancelled_at, 0.75)
        response = result["value"]
        self.assertEqual(response.error_type, "user_cancelled")
        self.assertFalse(marker.exists())

    def test_total_budget_includes_version_probe(self) -> None:
        marker = self.workspace / "agent-process-started-after-budget.txt"
        slow_version = self.root / "budgeted_version_codex.py"
        slow_version.write_text(
            "\n".join(
                [
                    "import sys, time",
                    "from pathlib import Path",
                    "if '--version' in sys.argv:",
                    "    time.sleep(5)",
                    "    print('codex-cli too-late')",
                    "    raise SystemExit(0)",
                    f"Path({str(marker)!r}).write_text('started')",
                ]
            ),
            encoding="utf-8",
        )
        runtime = CodexCliRuntime(
            CodexCliProfile(command=[sys.executable, str(slow_version)], model="gpt-test")
        )
        request = self.request("budget includes preflight")
        request.budget = AgentBudget(total_timeout_seconds=0.5, idle_timeout_seconds=10)

        started = time.monotonic()
        result = runtime.invoke(request)
        elapsed = time.monotonic() - started

        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_type, "call_timeout")
        self.assertLess(elapsed, 2)
        self.assertFalse(marker.exists())

    def test_role_router_runs_codex_executor_inside_persistent_workflow(self) -> None:
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

        plan = {
            "requirement_understanding": "生成结果文件",
            "non_goals": [],
            "files_and_symbols": ["result.txt"],
            "steps": ["写入 result.txt"],
            "constraints": ["只修改任务工作区"],
            "acceptance_criteria": ["result.txt 内容为 done"],
            "required_tests": ["fake-check"],
            "risks": [],
            "open_questions": [],
        }
        review = {
            "verdict": "pass",
            "acceptance": [{"criterion": "result.txt 内容为 done", "passed": True}],
            "issues": [],
            "recommended_tests": [],
            "summary": "通过",
        }
        claude_fake = ScriptedFakeRuntime(
            {
                "planner": [FakeAgentStep(output=plan)],
                "reviewer": [FakeAgentStep(output=review)],
            }
        )
        routed = RoleRoutedRuntime(
            {
                "planner": claude_fake,
                "executor": self.runtime,
                "reviewer": claude_fake,
            }
        )
        repository = create_repository(self.root)
        workflow = AgentWorkflow(
            self.root / "workloop-data",
            runtime=routed,
            validator=PassingValidator(),
        )
        project = workflow.register_project("Codex 项目", repository, "main")
        task = workflow.create_task("Codex 执行", "创建 result.txt", project.project_id)
        workflow.analyze(task.task_id)

        completed = workflow.approve_plan(task.task_id)

        self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER, completed.error)
        self.assertEqual((Path(completed.workspace) / "result.txt").read_text("utf-8"), "done\n")
        invocation = json.loads(
            (Path(completed.workspace) / "codex-invocation.json").read_text("utf-8")
        )
        self.assertIn("生成结果文件", invocation["stdin"])
        run = json.loads(
            (
                self.root
                / "workloop-data"
                / "tasks"
                / task.task_id
                / "artifacts/runs/2-executor.json"
            ).read_text("utf-8")
        )
        self.assertEqual(run["runtime"], "codex-cli")
        self.assertEqual(run["runtime_version"], "fake-1.2.3")
        self.assertEqual(run["model"], "gpt-test")
        self.assertEqual(run["session_id"], "session-123")
        self.assertEqual(run["usage"]["input_tokens"], 120)
        self.assertEqual(run["runtime_config"]["approval_policy"], "never")
        self.assertTrue(run["events"])
        self.assertTrue(run["raw_events"])


if __name__ == "__main__":
    unittest.main()
