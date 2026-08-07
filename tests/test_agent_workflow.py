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
    ReviewResult,
    ReviewVerdict,
    ValidationResult,
)
from app.agents.fake_runtime import FakeAgentStep, ScriptedFakeRuntime
from app.agents.plan_graph import (
    ModelBinding,
    PlanNode,
    PlanNodeAccess,
    PlanNodeKind,
)
from app.agents.runtime import AgentRuntime, RoleRoutedRuntime
from app.agents.workflow import (
    AgentWorkflow,
    _MAX_RUN_EVENT_FIELD_CHARS,
    _MAX_RUN_EVENTS_KEPT,
    _bound_run_events,
)
from app.agents.workflow_config import workflow_from_dict
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


def passing_review(summary: str) -> dict:
    return {
        "verdict": "pass",
        "acceptance": [{"criterion": "result.txt 内容为 done", "passed": True}],
        "issues": [],
        "recommended_tests": [],
        "summary": summary,
    }


class PassingValidator:
    def validate(self, task_id: str, workspace: Path, plan: ExecutionPlan, policy) -> ValidationResult:
        return ValidationResult(
            passed=True,
            checks=[{"command": "fake-check", "exit_code": 0, "stdout": "ok", "stderr": ""}],
        )


class RecordingValidator(PassingValidator):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def validate(self, task_id: str, workspace: Path, plan: ExecutionPlan, policy) -> ValidationResult:
        self.calls.append(task_id)
        return super().validate(task_id, workspace, plan, policy)


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
    def test_custom_workflow_executes_reordered_and_repeated_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = ScriptedFakeRuntime(
                {
                    "planner": [FakeAgentStep(output=execution_plan())],
                    "executor": [
                        FakeAgentStep(
                            output={
                                "completed_steps": ["first pass"],
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
                                "completed_steps": ["first pass revision"],
                                "modified_files": ["result.txt"],
                                "tests": [],
                                "deviations": [],
                                "remaining_risks": [],
                                "next_steps": [],
                            },
                            writes={"result.txt": "done\n"},
                        ),
                        FakeAgentStep(
                            output={
                                "completed_steps": ["second pass"],
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
                                "acceptance": [
                                    {"criterion": "result.txt 内容为 done", "passed": False}
                                ],
                                "issues": [
                                    {
                                        "file": "result.txt",
                                        "line": 1,
                                        "severity": "warning",
                                        "message": "FIRST EXECUTOR FEEDBACK",
                                        "suggestion": "Finish the first pass",
                                        "evidence": "result.txt contains first",
                                    }
                                ],
                                "recommended_tests": [],
                                "summary": "Early review requested a revision.",
                            }
                        ),
                        FakeAgentStep(output=passing_review("Early review passed.")),
                        FakeAgentStep(output=passing_review("Post-execution review passed.")),
                        FakeAgentStep(output=passing_review("Final review passed.")),
                    ],
                }
            )
            validator = RecordingValidator()
            workflow, project = project_workflow(root, runtime, validator)
            workflow.workflows.save(
                workflow_from_dict(
                    {
                        "workflow_id": "composed",
                        "label": "Composed",
                        "nodes": [
                            {"node_id": "plan", "kind": "planner", "label": "Plan"},
                            {
                                "node_id": "validate-pre",
                                "kind": "validation",
                                "label": "Preflight",
                            },
                            {
                                "node_id": "execute-first",
                                "kind": "executor",
                                "label": "First pass",
                                "instructions": "FIRST EXECUTOR INSTRUCTION",
                            },
                            {
                                "node_id": "review-early",
                                "kind": "reviewer",
                                "label": "Early review",
                                "instructions": "EARLY REVIEW INSTRUCTION",
                            },
                            {
                                "node_id": "execute-final",
                                "kind": "executor",
                                "label": "Final pass",
                                "instructions": "FINAL EXECUTOR INSTRUCTION",
                            },
                            {
                                "node_id": "review-after-execution",
                                "kind": "reviewer",
                                "label": "Post-execution review",
                                "instructions": "POST EXECUTION REVIEW INSTRUCTION",
                            },
                            {
                                "node_id": "validate-final",
                                "kind": "validation",
                                "label": "Final validation",
                            },
                            {
                                "node_id": "review-final",
                                "kind": "reviewer",
                                "label": "Final review",
                                "instructions": "FINAL REVIEW INSTRUCTION",
                            },
                            {"node_id": "deliver", "kind": "delivery", "label": "Deliver"},
                        ],
                    }
                )
            )
            task = workflow.create_task(
                "Composed execution",
                "Create result.txt",
                project.project_id,
                workflow_id="composed",
            )

            workflow.analyze(task.task_id)
            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertEqual(
                [request.role for request in runtime.requests],
                [
                    "planner",
                    "executor",
                    "reviewer",
                    "executor",
                    "reviewer",
                    "executor",
                    "reviewer",
                    "reviewer",
                ],
            )
            self.assertEqual(len(validator.calls), 2)
            executor_requests = [request for request in runtime.requests if request.role == "executor"]
            reviewer_requests = [request for request in runtime.requests if request.role == "reviewer"]
            self.assertIn("FIRST EXECUTOR INSTRUCTION", executor_requests[0].instructions)
            self.assertIn("FIRST EXECUTOR INSTRUCTION", executor_requests[1].instructions)
            self.assertIn("FIRST EXECUTOR FEEDBACK", executor_requests[1].instructions)
            self.assertIn("FINAL EXECUTOR INSTRUCTION", executor_requests[2].instructions)
            self.assertNotIn("FIRST EXECUTOR FEEDBACK", executor_requests[2].instructions)
            self.assertIn("EARLY REVIEW INSTRUCTION", reviewer_requests[0].instructions)
            self.assertIn("EARLY REVIEW INSTRUCTION", reviewer_requests[1].instructions)
            self.assertIn(
                "POST EXECUTION REVIEW INSTRUCTION", reviewer_requests[2].instructions
            )
            self.assertIn("FINAL REVIEW INSTRUCTION", reviewer_requests[3].instructions)
            self.assertIn('"available": false', reviewer_requests[0].instructions)
            self.assertIn('"available": false', reviewer_requests[1].instructions)
            self.assertIn('"available": false', reviewer_requests[2].instructions)
            self.assertIn('"passed": true', reviewer_requests[3].instructions)
            self.assertEqual(completed.workflow_cursor, 8)

            completed.transition(AgentTaskStatus.INTEGRATION_REQUIRED)
            completed.transition(AgentTaskStatus.INTEGRATING)
            workflow.store.save(completed)
            revalidation_runtime = ScriptedFakeRuntime(
                {
                    "reviewer": [
                        FakeAgentStep(
                            output=passing_review(
                                "Integrated pre-validation review passed."
                            )
                        ),
                        FakeAgentStep(
                            output=passing_review("Integrated final review passed.")
                        ),
                    ]
                }
            )
            revalidation_validator = RecordingValidator()
            reloaded = AgentWorkflow(root, revalidation_runtime, revalidation_validator)

            revalidated = reloaded.revalidate_integrated(task.task_id)

            self.assertEqual(revalidated.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertEqual(len(revalidation_validator.calls), 1)
            self.assertEqual(
                [request.workflow_node_id for request in revalidation_runtime.requests],
                ["review-after-execution", "review-final"],
            )
            self.assertIn(
                '"available": false', revalidation_runtime.requests[0].instructions
            )
            self.assertIn('"passed": true', revalidation_runtime.requests[1].instructions)

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


class PromptSchemaHintTest(unittest.TestCase):
    """Text-JSON runtimes (PiRpcRuntime) parse the model's final message as
    JSON, so the role prompts must name the exact structured-output fields.
    ClaudeCodeRuntime enforces these shapes via tool input_schema, so the
    hints are redundant there and harmless; without them a text-JSON runtime's
    model invents its own field names and the strict from_dict parsers
    reject it (verified end-to-end against a real reasoning model)."""

    def _make_workflow_task_plan(self) -> tuple:
        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        runtime = ScriptedFakeRuntime({})
        workflow, project = project_workflow(root, runtime, PassingValidator())
        task = workflow.create_task("Schema hint", "Write result.txt", project.project_id)
        plan = ExecutionPlan.from_dict(execution_plan())
        return workflow, task, plan

    def test_planner_instructions_name_execution_plan_fields(self) -> None:
        workflow, task, _ = self._make_workflow_task_plan()
        instructions = workflow._planner_instructions(task)
        self.assertIn("ExecutionPlan", instructions)
        for field in ("requirement_understanding", "steps", "acceptance_criteria", "required_tests"):
            self.assertIn(field, instructions)

    def test_replanner_instructions_name_execution_plan_fields(self) -> None:
        workflow, task, plan = self._make_workflow_task_plan()
        review = ReviewResult(
            verdict=ReviewVerdict.REPLAN,
            acceptance=[],
            issues=[],
            recommended_tests=[],
            summary="redo",
        )
        instructions = workflow._replanner_instructions(task, plan, review)
        self.assertIn("ExecutionPlan", instructions)
        self.assertIn("requirement_understanding", instructions)

    def test_executor_instructions_name_execution_result_and_test_shape(self) -> None:
        workflow, task, plan = self._make_workflow_task_plan()
        instructions = workflow._executor_instructions(task, plan, None)
        self.assertIn("ExecutionResult", instructions)
        self.assertIn("tests", instructions)
        self.assertIn("exit_code", instructions)

    def test_graph_node_instructions_name_execution_result_and_test_shape(self) -> None:
        node = PlanNode(
            node_id="step-1",
            title="写入 result.txt",
            kind=PlanNodeKind.IMPLEMENTATION,
            depends_on=["planning"],
            instructions="写入 result.txt",
            model=ModelBinding(),
            access=PlanNodeAccess.WORKSPACE_WRITE,
            inputs=[],
            outputs=[],
            on_failure="human",
        )
        instructions = AgentWorkflow._node_instructions(node, None)
        self.assertIn("ExecutionResult", instructions)
        self.assertIn("exit_code", instructions)

    def test_reviewer_instructions_name_review_result_fields(self) -> None:
        workflow, task, plan = self._make_workflow_task_plan()
        validation = ValidationResult(passed=True, checks=[])
        instructions = workflow._reviewer_instructions(task, plan, "", validation)
        self.assertIn("ReviewResult", instructions)
        for field in ("verdict", "acceptance", "issues", "recommended_tests", "summary"):
            self.assertIn(field, instructions)


class RunEventBoundingTest(unittest.TestCase):
    """A verbose reasoning-model session can produce an event stream large
    enough that json.dumps(to_plain(record)) hit MemoryError mid-run
    (observed during self-dogfooding). raw_events is never read back for
    control flow, so bounding the persisted copy must prevent OOM without
    losing anything the system depends on."""

    def test_caps_entry_count_with_truncation_marker(self) -> None:
        events = [{"type": "x", "i": i} for i in range(_MAX_RUN_EVENTS_KEPT + 100)]
        bounded = _bound_run_events(events)
        self.assertEqual(len(bounded), _MAX_RUN_EVENTS_KEPT + 1)
        self.assertEqual(bounded[_MAX_RUN_EVENTS_KEPT // 2]["type"], "events_truncated")
        self.assertEqual(bounded[_MAX_RUN_EVENTS_KEPT // 2]["dropped"], 100)
        # first and last real events are preserved
        self.assertEqual(bounded[0]["i"], 0)
        self.assertEqual(bounded[-1]["i"], _MAX_RUN_EVENTS_KEPT + 99)

    def test_truncates_overlong_string_fields(self) -> None:
        huge = "y" * (_MAX_RUN_EVENT_FIELD_CHARS * 5)
        bounded = _bound_run_events([{"content": huge, "nested": {"deep": huge}}])
        self.assertLessEqual(len(bounded[0]["content"]), _MAX_RUN_EVENT_FIELD_CHARS + len("…<truncated>"))
        self.assertTrue(bounded[0]["content"].endswith("…<truncated>"))
        self.assertLessEqual(len(bounded[0]["nested"]["deep"]), _MAX_RUN_EVENT_FIELD_CHARS + len("…<truncated>"))

    def test_small_list_is_unchanged_in_content(self) -> None:
        events = [{"type": "a"}, {"type": "b"}]
        self.assertEqual(_bound_run_events(events), events)

    def test_non_list_and_empty_become_empty_list(self) -> None:
        self.assertEqual(_bound_run_events(None), [])
        self.assertEqual(_bound_run_events([]), [])

    def test_bounded_output_serializes_to_bounded_json(self) -> None:
        # A list that would otherwise serialize to many MB: 5000 entries each
        # carrying a large string. Bounded output must stay small and serializable.
        events = [{"content": "z" * 5000} for _ in range(5000)]
        bounded = _bound_run_events(events)
        serialized = json.dumps(bounded, ensure_ascii=False)
        self.assertLessEqual(len(bounded), _MAX_RUN_EVENTS_KEPT + 1)
        # ~400 entries x ~4KB cap ~ well under a few MB
        self.assertLess(len(serialized), 4_000_000)


if __name__ == "__main__":
    unittest.main()
