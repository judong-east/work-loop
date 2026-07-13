from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from app.agents.contracts import (
    AgentAccess,
    AgentResult,
    AgentTask,
    AgentTaskStatus,
    ExecutionPlan,
    ValidationResult,
)
from app.agents.fake_runtime import FakeAgentStep, ScriptedFakeRuntime
from app.agents.runtime import AgentRuntime, RoleRoutedRuntime
from app.agents.workflow import AgentWorkflow
from tests.git_support import create_repository


def execution_plan() -> dict:
    return {
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


class PassingValidator:
    def validate(self, task_id: str, workspace: Path, plan: ExecutionPlan, policy) -> ValidationResult:
        return ValidationResult(
            passed=True,
            checks=[{"command": "fake-check", "exit_code": 0, "stdout": "ok", "stderr": ""}],
        )


class RaisingValidator:
    def validate(self, task_id: str, workspace: Path, plan: ExecutionPlan, policy) -> ValidationResult:
        raise RuntimeError("validator boom")


class InspectingRaisingRuntime(AgentRuntime):
    def __init__(self, root: Path):
        self.root = root
        self.saw_running_record = False

    def invoke(self, request):
        run_files = list((self.root / "tasks" / request.task_id / "artifacts" / "runs").glob("*.json"))
        if len(run_files) == 1:
            record = json.loads(run_files[0].read_text(encoding="utf-8"))
            self.saw_running_record = record["status"] == "running" and record["role"] == request.role
        raise RuntimeError("boom")


class CancellableRuntime(AgentRuntime):
    def __init__(self):
        self.executor_started = threading.Event()
        self.cancelled = threading.Event()

    def invoke(self, request):
        if request.role == "planner":
            return AgentResult(
                succeeded=True,
                output=execution_plan(),
                session_id="planner-session",
                runtime="fake-cancellable",
                runtime_version="1",
                model="scripted",
            )
        if request.role == "executor":
            self.executor_started.set()
            self.cancelled.wait(timeout=10)
            return AgentResult(
                succeeded=False,
                session_id="executor-session",
                error="cancelled",
                error_type="user_cancelled",
                runtime="fake-cancellable",
                runtime_version="1",
                model="scripted",
            )
        return AgentResult(succeeded=False, error="unexpected role")

    def cancel(self, task_id: str) -> bool:
        self.cancelled.set()
        return True


class PausingRoleRoutedRuntime(RoleRoutedRuntime):
    def __init__(self, runtimes):
        super().__init__(runtimes)
        self.executor_route_removed = threading.Event()
        self.release_executor_result = threading.Event()

    def invoke(self, request):
        result = super().invoke(request)
        if request.role == "executor":
            self.executor_route_removed.set()
            self.release_executor_result.wait(timeout=10)
        return result


class IdentityFailureRuntime(AgentRuntime):
    def invoke(self, request):
        raise RuntimeError("identity failure")

    def describe(self, request):
        return {
            "runtime": "identity-runtime",
            "runtime_version": "9.8.7",
            "model": "identity-model",
            "config": {"sandbox": "read-only"},
        }


class SecretRuntime(AgentRuntime):
    def invoke(self, request):
        plan = execution_plan()
        plan["requirement_understanding"] = "password=planner-output-secret"
        return AgentResult(
            succeeded=True,
            output=plan,
            session_id="secret-session",
            final_message="api_key=final-message-secret",
            raw_events=[
                {
                    "password": "raw-event-secret",
                    "accessToken": "camel-access-secret",
                    "input_tokens": 42,
                }
            ],
            error="token=error-secret",
            runtime="secret-runtime",
            runtime_version="1",
            model="secret-model",
        )


def project_workflow(root: Path, runtime: AgentRuntime, validator):
    repository = create_repository(root)
    workflow = AgentWorkflow(root, runtime=runtime, validator=validator)
    project = workflow.register_project("测试项目", repository, "main")
    return workflow, project


class AgentWorkflowTest(unittest.TestCase):
    def test_canonical_plan_maps_only_explicit_policy_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical_plan = execution_plan()
            canonical_plan["required_tests"] = ["API test", "responsive test"]
            canonical_plan["steps"].append("Run fake-check")
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [
                        FakeAgentStep(output=canonical_plan, session_id="canonical-plan")
                    ]
                }
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("Canonical plan", "Write result.txt", project.project_id)

            analyzed = workflow.analyze(task.task_id)
            plan = workflow.get_plan(task.task_id)

            self.assertEqual(analyzed.status, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
            self.assertEqual(plan.required_tests, ["fake-check"])

    def test_canonical_plan_without_named_validation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical_plan = execution_plan()
            canonical_plan["required_tests"] = ["API test", "responsive test"]
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [
                        FakeAgentStep(output=canonical_plan, session_id="canonical-plan")
                    ]
                }
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("Canonical plan", "Write result.txt", project.project_id)

            analyzed = workflow.analyze(task.task_id)

            self.assertEqual(analyzed.status, AgentTaskStatus.FAILED)
            self.assertIn("required_tests", analyzed.error)

    def test_native_claude_plan_maps_only_explicit_policy_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native_plan = {
                "title": "Write result",
                "plan": {
                    "steps": [
                        {"description": "Write result.txt"},
                        {"description": "Run fake-check"},
                    ]
                },
                "requirements": {
                    "acceptance_criteria": ["result.txt contains done"],
                    "clarifications": [],
                },
                "risks": [],
                "issues": [],
            }
            runtime = ScriptedFakeRuntime(
                {"planner": [FakeAgentStep(output=native_plan, session_id="native-plan")]}
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("Native plan", "Write result.txt", project.project_id)

            analyzed = workflow.analyze(task.task_id)
            plan = workflow.get_plan(task.task_id)

            self.assertEqual(analyzed.status, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
            self.assertEqual(plan.steps, ["Write result.txt", "Run fake-check"])
            self.assertEqual(plan.required_tests, ["fake-check"])
            self.assertEqual(plan.acceptance_criteria, ["result.txt contains done"])

    def test_native_claude_plan_without_explicit_validation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native_plan = {
                "title": "Write result",
                "plan": {"steps": [{"description": "Write result.txt"}]},
                "requirements": {
                    "acceptance_criteria": ["result.txt contains done"],
                    "clarifications": [],
                },
                "risks": [],
                "issues": [],
            }
            runtime = ScriptedFakeRuntime(
                {"planner": [FakeAgentStep(output=native_plan, session_id="native-plan")]}
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("Native plan", "Write result.txt", project.project_id)

            analyzed = workflow.analyze(task.task_id)

            self.assertEqual(analyzed.status, AgentTaskStatus.FAILED)
            self.assertIn("required_tests", analyzed.error)

    def test_native_top_level_steps_use_explicit_numbered_requirement_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native_plan = {
                "title": "Write result",
                "steps": [
                    {"description": "Write result.txt"},
                    {"description": "Run fake-check"},
                ],
                "issues": [],
                "optimistic": False,
            }
            runtime = ScriptedFakeRuntime(
                {"planner": [FakeAgentStep(output=native_plan, session_id="native-plan")]}
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task(
                "Native plan",
                "Acceptance criteria:\n1. result.txt contains done",
                project.project_id,
            )

            analyzed = workflow.analyze(task.task_id)
            plan = workflow.get_plan(task.task_id)

            self.assertEqual(analyzed.status, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
            self.assertEqual(plan.acceptance_criteria, ["result.txt contains done"])
            self.assertEqual(plan.required_tests, ["fake-check"])

    def test_cancel_after_delegate_exit_cannot_be_overwritten_by_stale_workflow_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripted = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=execution_plan())],
                    "executor": [
                        FakeAgentStep(
                            output={
                                "completed_steps": ["写入"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "done\n"},
                        )
                    ],
                }
            )
            runtime = PausingRoleRoutedRuntime(
                {"planner": scripted, "executor": scripted, "reviewer": scripted}
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("竞态取消", "executor 退出后立即取消", project.project_id)
            workflow.analyze(task.task_id)
            result: dict[str, AgentTask] = {}
            thread = threading.Thread(
                target=lambda: result.setdefault("task", workflow.approve_plan(task.task_id))
            )
            thread.start()
            self.assertTrue(runtime.executor_route_removed.wait(timeout=5))

            try:
                cancelling = workflow.cancel_task(task.task_id)
            finally:
                runtime.release_executor_result.set()

            self.assertEqual(cancelling.status, AgentTaskStatus.CANCELLING)
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["task"].status, AgentTaskStatus.CANCELLED)
            self.assertEqual(workflow.get_task(task.task_id).status, AgentTaskStatus.CANCELLED)
            self.assertFalse(Path(task.workspace).exists())

    def test_active_executor_cancellation_reaches_terminal_state_and_removes_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = CancellableRuntime()
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("取消执行", "终止正在运行的 executor", project.project_id)
            workflow.analyze(task.task_id)
            result: dict[str, AgentTask] = {}
            thread = threading.Thread(
                target=lambda: result.setdefault("task", workflow.approve_plan(task.task_id))
            )
            thread.start()
            self.assertTrue(runtime.executor_started.wait(timeout=5))

            cancelling = workflow.cancel_task(task.task_id)

            self.assertEqual(cancelling.status, AgentTaskStatus.CANCELLING)
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            cancelled = result["task"]
            self.assertEqual(cancelled.status, AgentTaskStatus.CANCELLED)
            self.assertEqual(workflow.get_task(task.task_id).status, AgentTaskStatus.CANCELLED)
            self.assertFalse(Path(cancelled.workspace).exists())
            executor_run = json.loads(
                (
                    root
                    / "tasks"
                    / task.task_id
                    / "artifacts/runs/2-executor.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(executor_run["status"], "cancelled")
            self.assertEqual(executor_run["error_type"], "user_cancelled")

    def test_task_rejects_illegal_state_transition(self) -> None:
        task = AgentTask(title="非法迁移", requirement="不能跳过工作流")

        with self.assertRaisesRegex(ValueError, "draft.*ready_to_deliver"):
            task.transition(AgentTaskStatus.READY_TO_DELIVER)

        self.assertEqual(task.status, AgentTaskStatus.DRAFT)

    def test_validator_exception_is_persisted_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=execution_plan())],
                    "executor": [
                        FakeAgentStep(
                            output={
                                "completed_steps": ["写入"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "done\n"},
                        )
                    ],
                }
            )
            workflow, project = project_workflow(root, runtime, RaisingValidator())
            task = workflow.create_task("验证异常", "验证器会抛异常", project.project_id)
            workflow.analyze(task.task_id)

            result = workflow.approve_plan(task.task_id)

            self.assertEqual(result.status, AgentTaskStatus.FAILED)
            self.assertIn("validator boom", result.error)
            self.assertEqual(workflow.get_task(task.task_id).status, AgentTaskStatus.FAILED)

    def test_runtime_exception_is_recorded_and_task_can_be_reloaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = InspectingRaisingRuntime(root)
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("异常持久化", "运行时会抛异常", project.project_id)

            result = workflow.analyze(task.task_id)

            self.assertTrue(runtime.saw_running_record)
            self.assertEqual(result.status, AgentTaskStatus.FAILED)
            self.assertIn("boom", result.error)
            self.assertEqual(workflow.get_task(task.task_id).status, AgentTaskStatus.FAILED)
            run_files = list((root / "tasks" / task.task_id / "artifacts" / "runs").glob("*.json"))
            self.assertEqual(len(run_files), 1)
            record = json.loads(run_files[0].read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "failed")
            self.assertIn("boom", record["error"])

    def test_failed_result_preserves_preflight_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, project = project_workflow(
                root, IdentityFailureRuntime(), PassingValidator()
            )
            task = workflow.create_task("身份保留", "运行失败也保留身份", project.project_id)

            result = workflow.analyze(task.task_id)

            self.assertEqual(result.status, AgentTaskStatus.FAILED)
            run = json.loads(
                (
                    root
                    / "tasks"
                    / task.task_id
                    / "artifacts/runs/1-planner.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(run["runtime"], "identity-runtime")
            self.assertEqual(run["runtime_version"], "9.8.7")
            self.assertEqual(run["model"], "identity-model")
            self.assertEqual(run["runtime_config"]["sandbox"], "read-only")

    def test_agent_run_and_structured_artifacts_redact_sensitive_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow, project = project_workflow(root, SecretRuntime(), PassingValidator())
            task = workflow.create_task(
                "脱敏", "分析 api_key=prompt-secret", project.project_id
            )

            result = workflow.analyze(task.task_id)

            self.assertEqual(result.status, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
            task_dir = root / "tasks" / task.task_id
            run_text = (task_dir / "artifacts/runs/1-planner.json").read_text("utf-8")
            plan_text = (task_dir / "artifacts/plans/1.json").read_text("utf-8")
            for secret in (
                "prompt-secret",
                "planner-output-secret",
                "final-message-secret",
                "raw-event-secret",
                "camel-access-secret",
                "error-secret",
            ):
                self.assertNotIn(secret, run_text)
                self.assertNotIn(secret, plan_text)
            self.assertIn("[REDACTED]", run_text)
            self.assertIn('"input_tokens": 42', run_text)

    def test_task_id_cannot_escape_task_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = AgentWorkflow(root, runtime=ScriptedFakeRuntime({}), validator=PassingValidator())

            with self.assertRaisesRegex(ValueError, "task_id"):
                workflow.workspace_path("../outside")

            self.assertFalse((root / "outside").exists())

    def test_read_only_agent_cannot_modify_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {"planner": [FakeAgentStep(output=execution_plan(), writes={"unauthorized.txt": "x"})]}
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("只读分析", "分析但不要修改", project.project_id)

            result = workflow.analyze(task.task_id)

            self.assertEqual(result.status, AgentTaskStatus.FAILED)
            self.assertIn("只读", result.error)
            self.assertFalse((workflow.workspace_path(task.task_id) / "unauthorized.txt").exists())

    def test_review_cannot_pass_with_failed_acceptance_criterion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=execution_plan())],
                    "executor": [
                        FakeAgentStep(
                            output={
                                "completed_steps": ["写入错误内容"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "wrong\n"},
                        )
                    ],
                    "reviewer": [
                        FakeAgentStep(
                            output={
                                "verdict": "pass",
                                "acceptance": [{"criterion": "result.txt 内容为 done", "passed": False}],
                                "issues": [],
                                "recommended_tests": [],
                                "summary": "错误地声称通过。",
                            }
                        )
                    ],
                }
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("审核门禁", "result.txt 必须为 done", project.project_id)
            workflow.analyze(task.task_id)

            result = workflow.approve_plan(task.task_id)

            self.assertEqual(result.status, AgentTaskStatus.FAILED)
            self.assertIn("验收", result.error)
            self.assertEqual(workflow.get_task(task.task_id).status, AgentTaskStatus.FAILED)

    def test_review_cannot_pass_with_blocker_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=execution_plan())],
                    "executor": [
                        FakeAgentStep(
                            output={
                                "completed_steps": ["写入"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "done\n"},
                        )
                    ],
                    "reviewer": [
                        FakeAgentStep(
                            output={
                                "verdict": "pass",
                                "acceptance": [{"criterion": "result.txt 内容为 done", "passed": True}],
                                "issues": [
                                    {
                                        "file": "result.txt",
                                        "line": 1,
                                        "severity": "blocker",
                                        "message": "仍存在阻断问题",
                                        "suggestion": "先修复再通过",
                                        "evidence": "文件内容虽然匹配，但权限不正确",
                                    }
                                ],
                                "recommended_tests": [],
                                "summary": "结论自相矛盾。",
                            }
                        )
                    ],
                }
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("阻断门禁", "阻断问题不得通过", project.project_id)
            workflow.analyze(task.task_id)

            result = workflow.approve_plan(task.task_id)

            self.assertEqual(result.status, AgentTaskStatus.FAILED)
            self.assertIn("阻断", result.error)

    def test_approved_task_reaches_ready_to_deliver_and_survives_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=execution_plan())],
                    "executor": [
                        FakeAgentStep(
                            output={
                                "completed_steps": ["写入 result.txt"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "done\n"},
                        )
                    ],
                    "reviewer": [
                        FakeAgentStep(
                            output={
                                "verdict": "pass",
                                "acceptance": [{"criterion": "result.txt 内容为 done", "passed": True}],
                                "issues": [],
                                "recommended_tests": [],
                                "summary": "实现和验证均通过。",
                            }
                        )
                    ],
                }
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())

            task = workflow.create_task("生成结果", "创建 result.txt", project.project_id)
            planned = workflow.analyze(task.task_id)

            self.assertEqual(planned.status, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
            self.assertEqual([request.role for request in runtime.requests], ["planner"])
            self.assertFalse((workflow.workspace_path(task.task_id) / "result.txt").exists())

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertEqual(completed.plan_version, 1)
            self.assertEqual(completed.approved_plan_version, 1)
            self.assertEqual(completed.iteration, 1)
            self.assertEqual(
                [(request.role, request.access) for request in runtime.requests],
                [
                    ("planner", AgentAccess.READ_ONLY),
                    ("executor", AgentAccess.WORKSPACE_WRITE),
                    ("reviewer", AgentAccess.READ_ONLY),
                ],
            )
            self.assertEqual(
                (workflow.workspace_path(task.task_id) / "result.txt").read_text(encoding="utf-8"),
                "done\n",
            )

            task_dir = root / "tasks" / task.task_id
            for relative in [
                "workflow-state.json",
                "artifacts/workspace-base.json",
                "artifacts/plans/1.json",
                "artifacts/rounds/1/execution.json",
                "artifacts/rounds/1/validation.json",
                "artifacts/rounds/1/review.json",
                "artifacts/rounds/1/changes.diff",
            ]:
                self.assertTrue((task_dir / relative).is_file(), relative)

            executor_run = json.loads(
                (task_dir / "artifacts/runs/2-executor.json").read_text(encoding="utf-8")
            )
            self.assertEqual(executor_run["runtime"], "fake")
            self.assertEqual(executor_run["runtime_version"], "1")
            self.assertEqual(executor_run["model"], "scripted")
            self.assertIn("runtime_config", executor_run)
            self.assertEqual(executor_run["budget"]["total_timeout_seconds"], 1800)
            self.assertEqual(executor_run["output"]["modified_files"], ["result.txt"])
            self.assertIn("usage", executor_run)
            self.assertIn("raw_events", executor_run)

            reloaded = AgentWorkflow(root, runtime=ScriptedFakeRuntime({}), validator=PassingValidator())
            restored = reloaded.get_task(task.task_id)
            self.assertEqual(restored.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertEqual(restored.sessions["executor"], completed.sessions["executor"])
            self.assertEqual(restored.sessions["reviewer"], completed.sessions["reviewer"])

    def test_revise_code_resumes_executor_until_review_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [
                        FakeAgentStep(output=execution_plan(), session_id="planner-claude-session")
                    ],
                    "executor": [
                        FakeAgentStep(
                            output={
                                "completed_steps": ["初版"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "first\n"},
                        ),
                        FakeAgentStep(
                            output={
                                "completed_steps": ["返修"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "done\n"},
                        ),
                    ],
                    "reviewer": [
                        FakeAgentStep(
                            output={
                                "verdict": "revise_code",
                                "acceptance": [{"criterion": "result.txt 内容为 done", "passed": False}],
                                "issues": [
                                    {
                                        "file": "result.txt",
                                        "line": 1,
                                        "severity": "warning",
                                        "message": "内容不是 done",
                                        "suggestion": "改为 done",
                                        "evidence": "result.txt 第一行为 first",
                                    }
                                ],
                                "recommended_tests": [],
                                "summary": "需要返修。",
                            },
                            session_id="reviewer-claude-session",
                        ),
                        FakeAgentStep(
                            output={
                                "verdict": "pass",
                                "acceptance": [{"criterion": "result.txt 内容为 done", "passed": True}],
                                "issues": [],
                                "recommended_tests": [],
                                "summary": "返修通过。",
                            }
                        ),
                    ],
                }
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("返修结果", "result.txt 最终必须为 done", project.project_id)
            workflow.analyze(task.task_id)

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertEqual(completed.iteration, 2)
            self.assertEqual(
                [request.role for request in runtime.requests],
                ["planner", "executor", "reviewer", "executor", "reviewer"],
            )
            executor_requests = [request for request in runtime.requests if request.role == "executor"]
            reviewer_requests = [request for request in runtime.requests if request.role == "reviewer"]
            self.assertEqual(executor_requests[1].session_id, completed.sessions["executor"])
            self.assertEqual(reviewer_requests[0].session_id, "")
            self.assertEqual(reviewer_requests[1].session_id, "reviewer-claude-session")
            self.assertNotEqual(completed.sessions["planner"], completed.sessions["reviewer"])
            self.assertIn("内容不是 done", executor_requests[1].instructions)
            self.assertEqual(
                (workflow.workspace_path(task.task_id) / "result.txt").read_text(encoding="utf-8"),
                "done\n",
            )
            for round_index in (1, 2):
                round_dir = root / "tasks" / task.task_id / "artifacts" / "rounds" / str(round_index)
                self.assertTrue((round_dir / "execution.json").is_file())
                self.assertTrue((round_dir / "validation.json").is_file())
                self.assertTrue((round_dir / "review.json").is_file())

    def test_planner_and_reviewer_cannot_share_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared_session = "shared-claude-session"
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [
                        FakeAgentStep(output=execution_plan(), session_id=shared_session)
                    ],
                    "executor": [
                        FakeAgentStep(
                            output={
                                "completed_steps": ["写入"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "done\n"},
                        )
                    ],
                    "reviewer": [
                        FakeAgentStep(
                            output={
                                "verdict": "pass",
                                "acceptance": [
                                    {"criterion": "result.txt 内容为 done", "passed": True}
                                ],
                                "issues": [],
                                "recommended_tests": [],
                                "summary": "通过。",
                            },
                            session_id=shared_session,
                        )
                    ],
                }
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("会话隔离", "规划与审核会话必须隔离", project.project_id)
            workflow.analyze(task.task_id)

            result = workflow.approve_plan(task.task_id)

            self.assertEqual(result.status, AgentTaskStatus.FAILED)
            self.assertIn("相互隔离", result.error)
            reviewer_run = json.loads(
                (
                    root
                    / "tasks"
                    / task.task_id
                    / "artifacts/runs/3-reviewer.json"
                ).read_text("utf-8")
            )
            self.assertEqual(reviewer_run["status"], "failed")
            self.assertEqual(reviewer_run["error_type"], "policy_blocked")
            self.assertEqual(reviewer_run["events"][-1]["event_type"], "failed")

    def test_replan_generates_new_plan_and_requires_new_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            second_plan = execution_plan()
            second_plan["steps"] = ["根据审核意见重新实现 result.txt"]
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [
                        FakeAgentStep(
                            output=execution_plan(),
                            session_id="planner-replan-session",
                        ),
                        FakeAgentStep(output=second_plan),
                    ],
                    "executor": [
                        FakeAgentStep(
                            output={
                                "completed_steps": ["初版"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "first\n"},
                        ),
                        FakeAgentStep(
                            output={
                                "completed_steps": ["按新计划返工"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "done\n"},
                        ),
                    ],
                    "reviewer": [
                        FakeAgentStep(
                            output={
                                "verdict": "replan",
                                "acceptance": [
                                    {"criterion": "result.txt 内容为 done", "passed": False}
                                ],
                                "issues": [
                                    {
                                        "file": "result.txt",
                                        "line": 1,
                                        "severity": "blocker",
                                        "message": "原计划遗漏关键约束",
                                        "suggestion": "重新规划后再执行",
                                        "evidence": "初版结果不满足需求",
                                    }
                                ],
                                "recommended_tests": [],
                                "summary": "计划本身需要重做。",
                            },
                            session_id="reviewer-replan-session",
                        ),
                        FakeAgentStep(
                            output={
                                "verdict": "pass",
                                "acceptance": [
                                    {"criterion": "result.txt 内容为 done", "passed": True}
                                ],
                                "issues": [],
                                "recommended_tests": [],
                                "summary": "新计划实现通过。",
                            }
                        ),
                    ],
                }
            )
            workflow, project = project_workflow(root, runtime, PassingValidator())
            task = workflow.create_task("重新规划", "计划错误时必须重新批准", project.project_id)
            workflow.analyze(task.task_id)

            replanned = workflow.approve_plan(task.task_id)

            self.assertEqual(
                replanned.status,
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            )
            self.assertEqual(replanned.plan_version, 2)
            self.assertEqual(replanned.approved_plan_version, 1)
            self.assertEqual(replanned.artifacts["plan"], "artifacts/plans/2.json")
            self.assertEqual(
                [request.role for request in runtime.requests],
                ["planner", "executor", "reviewer", "planner"],
            )
            second_planner_request = runtime.requests[-1]
            self.assertEqual(second_planner_request.session_id, "planner-replan-session")
            self.assertIn("原计划遗漏关键约束", second_planner_request.instructions)
            self.assertEqual(
                json.loads(
                    (
                        root
                        / "tasks"
                        / task.task_id
                        / "artifacts/plans/2.json"
                    ).read_text("utf-8")
                )["steps"],
                second_plan["steps"],
            )

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertEqual(completed.approved_plan_version, 2)
            self.assertEqual(
                [request.role for request in runtime.requests],
                ["planner", "executor", "reviewer", "planner", "executor", "reviewer"],
            )
            self.assertEqual(runtime.requests[-1].session_id, "reviewer-replan-session")
            first_review = json.loads(
                (
                    root
                    / "tasks"
                    / task.task_id
                    / "artifacts/rounds/1/review.json"
                ).read_text("utf-8")
            )
            self.assertEqual(first_review["verdict"], "replan")
            final_diff = (
                root
                / "tasks"
                / task.task_id
                / "artifacts/rounds/2/changes.diff"
            ).read_text("utf-8")
            self.assertIn("result.txt", final_diff)
            self.assertIn("+done", final_diff)


if __name__ == "__main__":
    unittest.main()
