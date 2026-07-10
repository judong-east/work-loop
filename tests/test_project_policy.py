from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agents.contracts import AgentTaskStatus
from app.agents.fake_runtime import FakeAgentStep, ScriptedFakeRuntime
from app.agents.workflow import AgentWorkflow
from app.projects.contracts import ValidationCommand
from app.validation.runner import (
    CodexCommandSandbox,
    DeterministicValidator,
    ProcessOutcome,
    UnsafeDirectCommandSandbox,
)
from tests.git_support import create_repository, run_git


def _toml_string(value: str) -> str:
    return json.dumps(value)


def write_project_policy(
    repository: Path,
    commands: dict[str, list[str]],
    *,
    protected_paths: list[str] | None = None,
    timeout_seconds: int = 2,
    network: str = "deny",
    redact_patterns: list[str] | None = None,
) -> None:
    lines = [
        "schema_version = 1",
        "",
        "[permissions]",
        "protected_paths = ["
        + ", ".join(_toml_string(item) for item in (protected_paths or []))
        + "]",
        f"network = {_toml_string(network)}",
        "",
        "[validation]",
        f"timeout_seconds = {timeout_seconds}",
    ]
    for name, argv in commands.items():
        lines.extend(
            [
                "",
                "[[validation.commands]]",
                f"name = {_toml_string(name)}",
                "argv = [" + ", ".join(_toml_string(item) for item in argv) + "]",
            ]
        )
    if redact_patterns:
        lines.extend(
            [
                "",
                "[evidence]",
                "redact_patterns = ["
                + ", ".join(_toml_string(item) for item in redact_patterns)
                + "]",
            ]
        )
    config = repository / ".workloop" / "project.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run_git(repository, "add", ".workloop/project.toml")
    run_git(repository, "commit", "-m", "add workloop policy")


def plan(required_tests: list[str], files: list[str] | None = None) -> dict:
    return {
        "requirement_understanding": "按策略修改并验证",
        "non_goals": [],
        "files_and_symbols": files or ["result.txt"],
        "steps": ["完成修改"],
        "constraints": ["遵守项目策略"],
        "acceptance_criteria": ["实现满足需求"],
        "required_tests": required_tests,
        "risks": [],
        "open_questions": [],
    }


def execution_output(modified_files: list[str]) -> dict:
    return {
        "completed_steps": ["完成修改"],
        "modified_files": modified_files,
        "tests": [],
        "deviations": [],
        "remaining_risks": [],
        "next_steps": [],
    }


def passing_review() -> dict:
    return {
        "verdict": "pass",
        "acceptance": [{"criterion": "实现满足需求", "passed": True}],
        "issues": [],
        "recommended_tests": [],
        "summary": "策略与验证均通过。",
    }


def create_policy_workflow(
    root: Path,
    runtime: ScriptedFakeRuntime,
    commands: dict[str, list[str]],
    **policy_options,
) -> tuple[AgentWorkflow, str]:
    repository = create_repository(root)
    write_project_policy(repository, commands, **policy_options)
    workflow = AgentWorkflow(
        root / "workloop-data",
        runtime=runtime,
        validator=DeterministicValidator(UnsafeDirectCommandSandbox()),
    )
    project = workflow.register_project("策略项目", repository, "main")
    return workflow, project.project_id


class ProjectPolicyWorkflowTest(unittest.TestCase):
    def test_production_sandbox_fails_closed_when_network_canary_connects(self) -> None:
        class RecordingProcesses:
            def __init__(self):
                self.calls: list[list[str]] = []

            def run(self, argv, workspace, timeout_seconds, environment):
                self.calls.append(list(argv))
                return ProcessOutcome(exit_code=86, stdout="NETWORK_OPEN\n")

        with tempfile.TemporaryDirectory() as tmp:
            processes = RecordingProcesses()
            sandbox = CodexCommandSandbox(processes=processes, executable=sys.executable)

            outcome = sandbox.run(
                ValidationCommand("unit", [sys.executable, "-c", "print('target')"]),
                Path(tmp),
                2,
                "deny",
            )

            self.assertIsNone(outcome.exit_code)
            self.assertIn("网络隔离", outcome.error)
            self.assertEqual(len(processes.calls), 1)
            self.assertNotIn("print('target')", processes.calls[0])

    def test_production_sandbox_checks_network_and_file_boundaries_before_target(self) -> None:
        class RecordingProcesses:
            def __init__(self):
                self.calls: list[list[str]] = []
                self.outcomes = [
                    ProcessOutcome(exit_code=0),
                    ProcessOutcome(exit_code=0),
                    ProcessOutcome(exit_code=0, stdout="target-ok\n"),
                ]

            def run(self, argv, workspace, timeout_seconds, environment):
                self.calls.append(list(argv))
                return self.outcomes.pop(0)

        with tempfile.TemporaryDirectory() as tmp:
            processes = RecordingProcesses()
            sandbox = CodexCommandSandbox(processes=processes, executable=sys.executable)

            outcome = sandbox.run(
                ValidationCommand("unit", [sys.executable, "-c", "print('target')"]),
                Path(tmp),
                2,
                "deny",
            )

            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(outcome.stdout, "target-ok\n")
            self.assertEqual(len(processes.calls), 3)
            for argv in processes.calls:
                self.assertIn("--sandbox-state-disable-network", argv)
            self.assertIn("print('target')", processes.calls[2])

    def test_runs_only_approved_required_validation_and_records_complete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit_command = [sys.executable, "-c", "print('validated')"]
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan(["unit"]))],
                    "executor": [
                        FakeAgentStep(
                            output=execution_output(["result.txt"]),
                            writes={"result.txt": "done\n"},
                        )
                    ],
                    "reviewer": [FakeAgentStep(output=passing_review())],
                }
            )
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {
                    "unit": unit_command,
                    "not-selected": [sys.executable, "-c", "print('must not run')"],
                },
                protected_paths=[".workloop/project.toml"],
            )
            task = workflow.create_task("策略验证", "创建 result.txt", project_id)
            workflow.analyze(task.task_id)

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            task_dir = root / "workloop-data" / "tasks" / task.task_id
            policy = json.loads((task_dir / "artifacts/project-policy.json").read_text("utf-8"))
            self.assertEqual([item["name"] for item in policy["validation_commands"]], ["unit", "not-selected"])
            evidence = json.loads(
                (task_dir / "artifacts/rounds/1/validation.json").read_text("utf-8")
            )
            self.assertTrue(evidence["passed"])
            self.assertEqual(len(evidence["checks"]), 1)
            check = evidence["checks"][0]
            self.assertEqual(check["name"], "unit")
            self.assertEqual(check["command"], unit_command)
            self.assertEqual(check["working_directory"], str(workflow.workspace_path(task.task_id)))
            self.assertEqual(check["exit_code"], 0)
            self.assertEqual(check["stdout"], "validated\n")
            self.assertEqual(check["stderr"], "")
            self.assertEqual(check["error"], "")
            self.assertFalse(check["timed_out"])
            self.assertGreaterEqual(check["duration_seconds"], 0)
            self.assertTrue(check["started_at"])
            self.assertTrue(check["finished_at"])
            executor_request = next(item for item in runtime.requests if item.role == "executor")
            self.assertFalse(executor_request.policy.network_allowed)
            self.assertEqual(executor_request.policy.allowed_commands, [unit_command])

    def test_plan_cannot_approve_validation_outside_project_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {"planner": [FakeAgentStep(output=plan(["not-allowed"]))]}
            )
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {"unit": [sys.executable, "-c", "print('ok')"]},
            )
            task = workflow.create_task("拒绝未知验证", "不能执行未授权命令", project_id)
            workflow.analyze(task.task_id)

            with self.assertRaisesRegex(ValueError, "未获项目策略允许"):
                workflow.approve_plan(task.task_id)

            self.assertEqual(
                workflow.get_task(task.task_id).status,
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            )
            self.assertEqual([request.role for request in runtime.requests], ["planner"])

    def test_plan_without_required_validation_cannot_reach_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {"planner": [FakeAgentStep(output=plan([]))]}
            )
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {"unit": [sys.executable, "-c", "print('ok')"]},
            )
            task = workflow.create_task("拒绝空验证", "必须声明确定性验证", project_id)

            failed = workflow.analyze(task.task_id)

            self.assertEqual(failed.status, AgentTaskStatus.FAILED)
            self.assertIn("required_tests", failed.error)
            self.assertEqual([request.role for request in runtime.requests], ["planner"])

    def test_plan_cannot_approve_after_workspace_or_policy_is_tampered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {"planner": [FakeAgentStep(output=plan(["unit"]))]}
            )
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {"unit": [sys.executable, "-c", "print('ok')"]},
            )
            task = workflow.create_task("拒绝脏基线", "批准前基线不可变化", project_id)
            workflow.analyze(task.task_id)
            config = workflow.workspace_path(task.task_id) / ".workloop/project.toml"
            config.write_text(config.read_text("utf-8") + "# tampered\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "未提交修改"):
                workflow.approve_plan(task.task_id)

            self.assertEqual(
                workflow.get_task(task.task_id).status,
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            )
            self.assertEqual([request.role for request in runtime.requests], ["planner"])

    def test_protected_path_change_is_blocked_before_validation_or_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan(["unit"], ["protected/secret.txt"]))],
                    "executor": [
                        FakeAgentStep(
                            output=execution_output(["protected/secret.txt"]),
                            writes={"protected/secret.txt": "changed\n"},
                        )
                    ],
                }
            )
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {"unit": [sys.executable, "-c", "print('must not run')"]},
                protected_paths=["protected/**"],
            )
            task = workflow.create_task("阻止越权", "不得修改 protected", project_id)
            workflow.analyze(task.task_id)

            blocked = workflow.approve_plan(task.task_id)

            self.assertEqual(blocked.status, AgentTaskStatus.BLOCKED)
            self.assertIn("protected/secret.txt", blocked.error)
            self.assertEqual([request.role for request in runtime.requests], ["planner", "executor"])
            round_dir = root / "workloop-data" / "tasks" / task.task_id / "artifacts/rounds/1"
            before = json.loads((round_dir / "policy-before.json").read_text("utf-8"))
            after = json.loads((round_dir / "policy-after.json").read_text("utf-8"))
            self.assertTrue(before["passed"])
            self.assertFalse(after["passed"])
            self.assertIn("protected/secret.txt", " ".join(after["issues"]))
            self.assertFalse((round_dir / "validation.json").exists())

    def test_protected_binary_change_is_detected_by_policy_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = create_repository(root)
            binary = repository / "protected" / "asset.bin"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(b"\xffold")
            run_git(repository, "add", "protected/asset.bin")
            run_git(repository, "commit", "-m", "add protected binary")
            write_project_policy(
                repository,
                {"unit": [sys.executable, "-c", "print('must not run')"]},
                protected_paths=["protected/**"],
            )
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan(["unit"], ["protected/asset.bin"]))],
                    "executor": [
                        FakeAgentStep(
                            output=execution_output(["protected/asset.bin"]),
                            writes={"protected/asset.bin": b"\xffnew"},
                        )
                    ],
                }
            )
            workflow = AgentWorkflow(
                root / "workloop-data",
                runtime=runtime,
                validator=DeterministicValidator(UnsafeDirectCommandSandbox()),
            )
            project = workflow.register_project("二进制策略项目", repository, "main")
            task = workflow.create_task("阻止二进制越权", "不得修改受保护二进制", project.project_id)
            workflow.analyze(task.task_id)

            blocked = workflow.approve_plan(task.task_id)

            self.assertEqual(blocked.status, AgentTaskStatus.BLOCKED)
            self.assertIn("protected/asset.bin", blocked.error)
            self.assertEqual([request.role for request in runtime.requests], ["planner", "executor"])

    def test_protected_text_line_ending_change_is_detected_by_policy_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = create_repository(root)
            run_git(repository, "config", "core.autocrlf", "false")
            run_git(repository, "restore", "app.txt", ".workloop/project.toml")
            protected = repository / "protected" / "line-endings.txt"
            protected.parent.mkdir(parents=True, exist_ok=True)
            protected.write_bytes(b"same text\n")
            run_git(repository, "add", "protected/line-endings.txt")
            run_git(repository, "commit", "-m", "add protected text")
            write_project_policy(
                repository,
                {"unit": [sys.executable, "-c", "print('must not run')"]},
                protected_paths=["protected/**"],
            )
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [
                        FakeAgentStep(output=plan(["unit"], ["protected/line-endings.txt"]))
                    ],
                    "executor": [
                        FakeAgentStep(
                            output=execution_output(["protected/line-endings.txt"]),
                            writes={"protected/line-endings.txt": b"same text\r\n"},
                        )
                    ],
                }
            )
            workflow = AgentWorkflow(
                root / "workloop-data",
                runtime=runtime,
                validator=DeterministicValidator(UnsafeDirectCommandSandbox()),
            )
            project = workflow.register_project("换行策略项目", repository, "main")
            task = workflow.create_task("阻止换行越权", "换行变化也是修改", project.project_id)
            workflow.analyze(task.task_id)

            blocked = workflow.approve_plan(task.task_id)

            self.assertEqual(blocked.status, AgentTaskStatus.BLOCKED)
            self.assertIn("protected/line-endings.txt", blocked.error)

    def test_failed_required_validation_records_evidence_and_skips_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan(["unit"]))],
                    "executor": [
                        FakeAgentStep(
                            output=execution_output(["result.txt"]),
                            writes={"result.txt": "done\n"},
                        )
                    ],
                }
            )
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {"unit": [sys.executable, "-c", "import sys; print('bad'); sys.exit(7)"]},
            )
            task = workflow.create_task("失败验证", "失败证据必须阻断", project_id)
            workflow.analyze(task.task_id)

            blocked = workflow.approve_plan(task.task_id)

            self.assertEqual(blocked.status, AgentTaskStatus.BLOCKED)
            self.assertEqual([request.role for request in runtime.requests], ["planner", "executor"])
            validation_path = (
                root
                / "workloop-data"
                / "tasks"
                / task.task_id
                / "artifacts/rounds/1/validation.json"
            )
            evidence = json.loads(validation_path.read_text("utf-8"))
            self.assertFalse(evidence["passed"])
            self.assertEqual(evidence["checks"][0]["exit_code"], 7)
            self.assertEqual(evidence["checks"][0]["stdout"], "bad\n")
            self.assertIn("退出码 7", evidence["error"])

    def test_silent_and_large_validation_outputs_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan(["silent", "large"]))],
                    "executor": [FakeAgentStep(output=execution_output([]))],
                    "reviewer": [FakeAgentStep(output=passing_review())],
                }
            )
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {
                    "silent": [sys.executable, "-c", "pass"],
                    "large": [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.write('x' * 65536); sys.stderr.write('y' * 32768)",
                    ],
                },
            )
            task = workflow.create_task("完整命令证据", "保存静默和大量输出", project_id)
            workflow.analyze(task.task_id)

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            validation_path = (
                root
                / "workloop-data"
                / "tasks"
                / task.task_id
                / "artifacts/rounds/1/validation.json"
            )
            checks = json.loads(validation_path.read_text("utf-8"))["checks"]
            self.assertEqual(checks[0]["stdout"], "")
            self.assertEqual(checks[0]["stderr"], "")
            self.assertEqual(len(checks[1]["stdout"]), 65536)
            self.assertEqual(len(checks[1]["stderr"]), 32768)

    def test_validation_command_cannot_modify_a_protected_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan(["unit"]))],
                    "executor": [FakeAgentStep(output=execution_output([]))],
                }
            )
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {
                    "unit": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('protected/by-test.txt').parent.mkdir(parents=True, exist_ok=True); Path('protected/by-test.txt').write_text('bad')",
                    ]
                },
                protected_paths=["protected/**"],
            )
            task = workflow.create_task("阻止验证越权", "验证命令也受路径策略约束", project_id)
            workflow.analyze(task.task_id)

            blocked = workflow.approve_plan(task.task_id)

            self.assertEqual(blocked.status, AgentTaskStatus.BLOCKED)
            self.assertIn("protected/by-test.txt", blocked.error)
            self.assertEqual([request.role for request in runtime.requests], ["planner", "executor"])
            policy_path = (
                root
                / "workloop-data"
                / "tasks"
                / task.task_id
                / "artifacts/rounds/1/policy-validation.json"
            )
            self.assertFalse(json.loads(policy_path.read_text("utf-8"))["passed"])

    def test_timed_out_required_validation_is_preserved_as_blocking_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan(["slow"]))],
                    "executor": [FakeAgentStep(output=execution_output([]))],
                }
            )
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {"slow": [sys.executable, "-c", "import time; time.sleep(2)"]},
                timeout_seconds=1,
            )
            task = workflow.create_task("超时验证", "超时必须阻断", project_id)
            workflow.analyze(task.task_id)

            blocked = workflow.approve_plan(task.task_id)

            self.assertEqual(blocked.status, AgentTaskStatus.BLOCKED)
            validation_path = (
                root
                / "workloop-data"
                / "tasks"
                / task.task_id
                / "artifacts/rounds/1/validation.json"
            )
            check = json.loads(validation_path.read_text("utf-8"))["checks"][0]
            self.assertTrue(check["timed_out"])
            self.assertIsNone(check["exit_code"])
            self.assertIn("超时", check["error"])

    def test_validation_uses_minimal_environment_and_redacts_sensitive_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan(["inspect-env"]))],
                    "executor": [FakeAgentStep(output=execution_output([]))],
                    "reviewer": [FakeAgentStep(output=passing_review())],
                }
            )
            command = [
                sys.executable,
                "-c",
                "import json, os; print(json.dumps({'token': os.getenv('WORKLOOP_POLICY_SECRET', 'json-secret with suffix, quote=\"leak-after\"'), 'api_key': 'abc,def'})); print('customer-12345')",
            ]
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {"inspect-env": command},
                redact_patterns=["customer-*"],
            )
            task = workflow.create_task("验证环境隔离", "秘密不得进入证据", project_id)
            workflow.analyze(task.task_id)

            with patch.dict(os.environ, {"WORKLOOP_POLICY_SECRET": "very-secret-value"}):
                completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            validation_path = (
                root
                / "workloop-data"
                / "tasks"
                / task.task_id
                / "artifacts/rounds/1/validation.json"
            )
            check = json.loads(validation_path.read_text("utf-8"))["checks"][0]
            self.assertNotIn("very-secret-value", check["stdout"])
            self.assertNotIn("json-secret", check["stdout"])
            self.assertNotIn("with suffix", check["stdout"])
            self.assertNotIn("leak-after", check["stdout"])
            self.assertNotIn("abc,def", check["stdout"])
            self.assertNotIn("customer-12345", check["stdout"])
            self.assertIn("[REDACTED]", check["stdout"])
            self.assertEqual(check["environment"]["network"], "deny")
            self.assertNotIn("WORKLOOP_POLICY_SECRET", check["environment"]["inherited_names"])

    def test_unclosed_escaped_secret_output_is_redacted_without_backtracking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan(["malformed-output"]))],
                    "executor": [FakeAgentStep(output=execution_output([]))],
                    "reviewer": [FakeAgentStep(output=passing_review())],
                }
            )
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {
                    "malformed-output": [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.write('token=\"' + chr(92) * 100000)",
                    ]
                },
            )
            task = workflow.create_task("线性脱敏", "畸形输出不能卡住编排", project_id)
            workflow.analyze(task.task_id)

            started = time.monotonic()
            completed = workflow.approve_plan(task.task_id)
            elapsed = time.monotonic() - started

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertLess(elapsed, 5)
            validation_path = (
                root
                / "workloop-data"
                / "tasks"
                / task.task_id
                / "artifacts/rounds/1/validation.json"
            )
            stdout = json.loads(validation_path.read_text("utf-8"))["checks"][0]["stdout"]
            self.assertEqual(stdout, 'token="[REDACTED]')

    def test_multiline_quoted_secret_is_fully_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan(["multiline-secret"]))],
                    "executor": [FakeAgentStep(output=execution_output([]))],
                    "reviewer": [FakeAgentStep(output=passing_review())],
                }
            )
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {
                    "multiline-secret": [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.write('secret=\"-----BEGIN PRIVATE KEY-----\\nbody-value\\n-----END PRIVATE KEY-----\"\\nvisible')",
                    ]
                },
            )
            task = workflow.create_task("多行脱敏", "多行秘密不能部分泄漏", project_id)
            workflow.analyze(task.task_id)

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            validation_path = (
                root
                / "workloop-data"
                / "tasks"
                / task.task_id
                / "artifacts/rounds/1/validation.json"
            )
            stdout = json.loads(validation_path.read_text("utf-8"))["checks"][0]["stdout"]
            self.assertEqual(stdout, 'secret="[REDACTED]"\nvisible')

    def test_validation_timeout_terminates_descendant_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "late-child-write.txt"
            child_code = (
                "import time; from pathlib import Path; "
                f"time.sleep(2); Path({str(marker)!r}).write_text('late', encoding='utf-8')"
            )
            parent_code = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(10)"
            )
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=plan(["spawn-child"]))],
                    "executor": [FakeAgentStep(output=execution_output([]))],
                }
            )
            workflow, project_id = create_policy_workflow(
                root,
                runtime,
                {"spawn-child": [sys.executable, "-c", parent_code]},
                timeout_seconds=1,
            )
            task = workflow.create_task("终止验证进程树", "超时不能遗留子进程", project_id)
            workflow.analyze(task.task_id)

            blocked = workflow.approve_plan(task.task_id)
            time.sleep(2.5)

            self.assertEqual(blocked.status, AgentTaskStatus.BLOCKED)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
