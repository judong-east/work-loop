from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.contracts import (
    AgentAccess,
    AgentRequest,
    AgentResult,
    AgentTaskStatus,
    ExecutionPlan,
    ValidationResult,
)
from app.agents.plan_graph import (
    ModelBinding,
    PlanGraph,
    PlanNode,
    PlanNodeAccess,
    PlanNodeKind,
)
from app.agents.runtime import AgentRuntime
from app.agents.scheduler import PersistentAgentScheduler
from app.agents.workflow import AgentWorkflow
from tests.git_support import create_repository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def executor_output(steps: list[str], files: list[str]) -> dict:
    return {
        "completed_steps": steps,
        "modified_files": files,
        "tests": [],
        "deviations": [],
        "remaining_risks": [],
        "next_steps": [],
    }


def passing_review() -> dict:
    return {
        "verdict": "pass",
        "acceptance": [{"criterion": "result.txt 内容为 done", "passed": True}],
        "issues": [],
        "recommended_tests": [],
        "summary": "实现和验证均通过。",
    }


class PassingValidator:
    def validate(self, task_id: str, workspace: Path, plan: ExecutionPlan, policy) -> ValidationResult:
        return ValidationResult(
            passed=True,
            checks=[{"command": "fake-check", "exit_code": 0, "stdout": "ok", "stderr": ""}],
        )


@dataclass
class FakeStep:
    output: dict[str, Any] = field(default_factory=dict)
    writes: dict[str, str] = field(default_factory=dict)
    succeeded: bool = True
    error: str = ""
    session_id: str = ""


class NodeScriptedFakeRuntime(AgentRuntime):
    """Fake runtime keyed by ``request.node_id`` (falling back to ``role``),
    so each plan node can be scripted independently."""

    def __init__(self, scripts: dict[str, list[FakeStep]]):
        self.scripts = {key: list(steps) for key, steps in scripts.items()}
        self.requests: list[AgentRequest] = []

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        key = request.node_id or request.role
        steps = self.scripts.get(key)
        if not steps:
            return AgentResult(
                succeeded=False,
                error=f"NodeScriptedFakeRuntime 没有 {key} 的脚本步骤。",
                runtime="fake",
                runtime_version="1",
                model="scripted",
            )
        step = steps.pop(0)
        if step.writes and request.access is not AgentAccess.WORKSPACE_WRITE:
            return AgentResult(
                succeeded=False,
                session_id=step.session_id or request.session_id,
                error=f"{key} 以只读权限运行，不能修改工作区。",
                runtime="fake",
                runtime_version="1",
                model="scripted",
            )
        if step.succeeded:
            self._apply_writes(request.workspace, step.writes)
        return AgentResult(
            succeeded=step.succeeded,
            output=dict(step.output),
            session_id=step.session_id or request.session_id or f"SESSION-{key}",
            error=step.error,
            runtime="fake",
            runtime_version="1",
            model="scripted",
        )

    def _apply_writes(self, workspace: Path, writes: dict[str, str]) -> None:
        root = workspace.resolve()
        for relative, content in writes.items():
            target = (root / relative).resolve()
            target.relative_to(root)  # path-escape guard
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def describe(self, request: AgentRequest) -> dict:
        return {"runtime": "fake", "runtime_version": "1", "model": "scripted", "config": {}}


@contextmanager
def graph_execution_env():
    prior = os.environ.get("WORKLOOP_EXECUTION")
    os.environ["WORKLOOP_EXECUTION"] = "graph"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("WORKLOOP_EXECUTION", None)
        else:
            os.environ["WORKLOOP_EXECUTION"] = prior


def project_workflow(root: Path, runtime: AgentRuntime, validator=PassingValidator()):
    repository = create_repository(root)
    workflow = AgentWorkflow(root, runtime=runtime, validator=validator)
    project = workflow.register_project("测试项目", repository, "main")
    return workflow, project


def impl_node(
    node_id: str,
    title: str,
    depends_on: list[str],
    on_failure: str = "human",
    model: ModelBinding | None = None,
) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        title=title,
        kind=PlanNodeKind.IMPLEMENTATION,
        depends_on=list(depends_on),
        instructions=title,
        model=model or ModelBinding(),
        access=PlanNodeAccess.WORKSPACE_WRITE,
        inputs=[],
        outputs=[],
        on_failure=on_failure,
    )


def review_node(node_id: str, depends_on: list[str]) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        title="Review completed implementation",
        kind=PlanNodeKind.REVIEW,
        depends_on=list(depends_on),
        instructions="Review the implementation against the acceptance criteria.",
        access=PlanNodeAccess.READ_ONLY,
    )


def planning_node() -> PlanNode:
    return PlanNode(
        node_id="planning",
        title="Approved execution plan",
        kind=PlanNodeKind.PLANNING,
        access=PlanNodeAccess.READ_ONLY,
        outputs=["execution-plan"],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class PlanGraphExecutionTest(unittest.TestCase):
    def test_linear_graph_executes_per_node_and_delivers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, graph_execution_env():
            root = Path(tmp)
            runtime = NodeScriptedFakeRuntime(
                {
                    "planner": [FakeStep(output=execution_plan(), session_id="planner-session")],
                    "step-1": [
                        FakeStep(
                            output=executor_output(["写入 result.txt"], ["result.txt"]),
                            writes={"result.txt": "done\n"},
                            session_id="executor-session",
                        )
                    ],
                    "review": [FakeStep(output=passing_review(), session_id="reviewer-session")],
                }
            )
            workflow, project = project_workflow(root, runtime)
            task = workflow.create_task("生成结果", "创建 result.txt", project.project_id)
            workflow.analyze(task.task_id)

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            # The executor ran once, for the single implementation node "step-1".
            executor_calls = [r for r in runtime.requests if r.role == "executor"]
            self.assertEqual(len(executor_calls), 1)
            self.assertEqual(executor_calls[0].node_id, "step-1")
            # Per-node state persisted.
            self.assertEqual(completed.node_runs["step-1"]["status"], "completed")
            # Workspace mutated and round artifacts written.
            self.assertEqual(
                (workflow.workspace_path(task.task_id) / "result.txt").read_text(encoding="utf-8"),
                "done\n",
            )
            task_dir = root / "tasks" / task.task_id
            execution = json.loads(
                (task_dir / "artifacts/rounds/1/execution.json").read_text(encoding="utf-8")
            )
            self.assertEqual(execution["completed_steps"], ["写入 result.txt"])

    def test_multi_node_graph_runs_in_topological_order_with_context_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, graph_execution_env():
            root = Path(tmp)
            runtime = NodeScriptedFakeRuntime(
                {
                    "planner": [FakeStep(output=execution_plan(), session_id="planner-session")],
                    "backend": [
                        FakeStep(
                            output=executor_output(["实现后端 API"], ["backend.txt"]),
                            writes={"backend.txt": "api\n"},
                        )
                    ],
                    "ui": [
                        FakeStep(
                            output=executor_output(["实现前端页面"], ["ui.txt"]),
                            writes={"ui.txt": "page\n"},
                        )
                    ],
                    "review": [FakeStep(output=passing_review(), session_id="reviewer-session")],
                }
            )
            workflow, project = project_workflow(root, runtime)
            task = workflow.create_task("全栈任务", "后端加前端", project.project_id)
            workflow.analyze(task.task_id)
            # Replace the auto-generated linear graph with a chain that lets the
            # UI node depend on the backend node, so its context carries the
            # backend's completed steps downstream.
            graph = PlanGraph(
                requirement_summary="后端加前端",
                nodes=[
                    planning_node(),
                    impl_node("backend", "实现后端 API", depends_on=["planning"]),
                    impl_node("ui", "实现前端页面", depends_on=["backend"]),
                    review_node("review", depends_on=["ui"]),
                ],
            )
            workflow.save_plan_graph(task.task_id, graph)

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            order = [r.node_id for r in runtime.requests if r.role == "executor"]
            self.assertEqual(order, ["backend", "ui"])
            # The UI node received the backend's compressed context (its completed
            # step) because it depends on the backend node.
            ui_request = next(r for r in runtime.requests if r.node_id == "ui")
            self.assertIn("实现后端 API", ui_request.instructions)
            # The combined round execution result merges both nodes.
            task_dir = root / "tasks" / task.task_id
            execution = json.loads(
                (task_dir / "artifacts/rounds/1/execution.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                execution["completed_steps"], ["实现后端 API", "实现前端页面"]
            )
            self.assertEqual(completed.node_runs["backend"]["status"], "completed")
            self.assertEqual(completed.node_runs["ui"]["status"], "completed")

    def test_resume_retries_failed_node_and_skips_completed_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, graph_execution_env():
            root = Path(tmp)
            # First pass: backend succeeds, ui fails (human pause).
            runtime_a = NodeScriptedFakeRuntime(
                {
                    "planner": [FakeStep(output=execution_plan(), session_id="planner-session")],
                    "backend": [
                        FakeStep(
                            output=executor_output(["实现后端 API"], ["backend.txt"]),
                            writes={"backend.txt": "api\n"},
                        )
                    ],
                    "ui": [FakeStep(succeeded=False, error="ui boom")],
                    "review": [FakeStep(output=passing_review(), session_id="reviewer-session")],
                }
            )
            workflow_a, project = project_workflow(root, runtime_a)
            task = workflow_a.create_task("全栈任务", "后端加前端", project.project_id)
            workflow_a.analyze(task.task_id)
            graph = PlanGraph(
                requirement_summary="后端加前端",
                nodes=[
                    planning_node(),
                    impl_node("backend", "实现后端 API", depends_on=["planning"]),
                    impl_node("ui", "实现前端页面", depends_on=["backend"]),
                    review_node("review", depends_on=["ui"]),
                ],
            )
            workflow_a.save_plan_graph(task.task_id, graph)
            paused = workflow_a.approve_plan(task.task_id)

            self.assertEqual(paused.status, AgentTaskStatus.PAUSED)
            self.assertEqual(paused.node_runs["backend"]["status"], "completed")
            self.assertEqual(paused.node_runs["ui"]["status"], "failed")

            # Second pass: fresh runtime where ui now succeeds; resume via the
            # scheduler. Completed nodes (planning, backend) must NOT re-run.
            runtime_b = NodeScriptedFakeRuntime(
                {
                    "ui": [
                        FakeStep(
                            output=executor_output(["实现前端页面"], ["ui.txt"]),
                            writes={"ui.txt": "page\n"},
                        )
                    ],
                    "review": [FakeStep(output=passing_review(), session_id="reviewer-session")],
                }
            )
            workflow_b = AgentWorkflow(root, runtime=runtime_b, validator=PassingValidator())
            scheduler = PersistentAgentScheduler(workflow_b)
            scheduler.resume(task.task_id)
            scheduler.run_next()

            resumed = workflow_b.get_task(task.task_id)
            self.assertEqual(resumed.status, AgentTaskStatus.READY_TO_DELIVER)
            # Only the previously-failed node and the reviewer ran on resume.
            self.assertEqual(
                [r.node_id for r in runtime_b.requests],
                ["ui", "review"],
            )
            self.assertEqual(resumed.node_runs["backend"]["status"], "completed")
            self.assertEqual(resumed.node_runs["ui"]["status"], "completed")
            # Both nodes' results are present in the combined execution artifact.
            task_dir = root / "tasks" / task.task_id
            execution = json.loads(
                (task_dir / "artifacts/rounds/1/execution.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                execution["completed_steps"], ["实现后端 API", "实现前端页面"]
            )

    def test_node_failure_with_human_policy_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, graph_execution_env():
            root = Path(tmp)
            runtime = NodeScriptedFakeRuntime(
                {
                    "planner": [FakeStep(output=execution_plan(), session_id="planner-session")],
                    "step-1": [FakeStep(succeeded=False, error="boom")],
                    "review": [FakeStep(output=passing_review(), session_id="reviewer-session")],
                }
            )
            workflow, project = project_workflow(root, runtime)
            task = workflow.create_task("生成结果", "创建 result.txt", project.project_id)
            workflow.analyze(task.task_id)
            graph = PlanGraph(
                requirement_summary="生成结果文件",
                nodes=[
                    planning_node(),
                    impl_node("step-1", "写入 result.txt", depends_on=["planning"], on_failure="human"),
                    review_node("review", depends_on=["step-1"]),
                ],
            )
            workflow.save_plan_graph(task.task_id, graph)

            result = workflow.approve_plan(task.task_id)

            self.assertEqual(result.status, AgentTaskStatus.PAUSED)
            self.assertEqual(result.pause_reason, "node_failed")
            self.assertEqual(result.node_runs["step-1"]["status"], "failed")
            self.assertIn("boom", result.node_runs["step-1"]["error"])

    def test_node_failure_with_skip_policy_continues_to_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, graph_execution_env():
            root = Path(tmp)
            runtime = NodeScriptedFakeRuntime(
                {
                    "planner": [FakeStep(output=execution_plan(), session_id="planner-session")],
                    "step-1": [FakeStep(succeeded=False, error="boom")],
                    "review": [FakeStep(output=passing_review(), session_id="reviewer-session")],
                }
            )
            workflow, project = project_workflow(root, runtime)
            task = workflow.create_task("生成结果", "创建 result.txt", project.project_id)
            workflow.analyze(task.task_id)
            graph = PlanGraph(
                requirement_summary="生成结果文件",
                nodes=[
                    planning_node(),
                    impl_node("step-1", "写入 result.txt", depends_on=["planning"], on_failure="skip"),
                    review_node("review", depends_on=["step-1"]),
                ],
            )
            workflow.save_plan_graph(task.task_id, graph)

            result = workflow.approve_plan(task.task_id)

            self.assertEqual(result.status, AgentTaskStatus.READY_TO_DELIVER)
            self.assertEqual(result.node_runs["step-1"]["status"], "skipped")

    def test_default_path_is_single_executor_without_graph_env(self) -> None:
        # With WORKLOOP_EXECUTION unset, the proven single-executor path runs
        # and the per-node graph driver is dormant (regression guard).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ.pop("WORKLOOP_EXECUTION", None)
            runtime = NodeScriptedFakeRuntime(
                {
                    "planner": [FakeStep(output=execution_plan(), session_id="planner-session")],
                    "step-1": [
                        FakeStep(
                            output=executor_output(["写入 result.txt"], ["result.txt"]),
                            writes={"result.txt": "done\n"},
                        )
                    ],
                    "review": [FakeStep(output=passing_review(), session_id="reviewer-session")],
                }
            )
            workflow, project = project_workflow(root, runtime)
            task = workflow.create_task("生成结果", "创建 result.txt", project.project_id)
            workflow.analyze(task.task_id)

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            # The default path keys the executor by role, not node, so it does
            # not consume the "step-1" script via node_id — it falls back to role.
            # graph_execution stays off and node_runs stays empty.
            self.assertFalse(completed.graph_execution)


if __name__ == "__main__":
    unittest.main()
