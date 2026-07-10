from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.agents.contracts import (
    AgentAccess,
    AgentTask,
    AgentTaskStatus,
    ExecutionPlan,
    ValidationResult,
)
from app.agents.fake_runtime import FakeAgentStep, ScriptedFakeRuntime
from app.agents.runtime import AgentRuntime
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
    def validate(self, task_id: str, workspace: Path, plan: ExecutionPlan) -> ValidationResult:
        return ValidationResult(
            passed=True,
            checks=[{"command": "fake-check", "exit_code": 0, "stdout": "ok", "stderr": ""}],
        )


class RaisingValidator:
    def validate(self, task_id: str, workspace: Path, plan: ExecutionPlan) -> ValidationResult:
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


def project_workflow(root: Path, runtime: AgentRuntime, validator):
    repository = create_repository(root)
    workflow = AgentWorkflow(root, runtime=runtime, validator=validator)
    project = workflow.register_project("测试项目", repository, "main")
    return workflow, project


class AgentWorkflowTest(unittest.TestCase):
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
                    "planner": [FakeAgentStep(output=execution_plan())],
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
                            }
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
            self.assertEqual(executor_requests[1].session_id, completed.sessions["executor"])
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


if __name__ == "__main__":
    unittest.main()
