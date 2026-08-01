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
    deletes: list[str] = field(default_factory=list)
    succeeded: bool = True
    error: str = ""
    session_id: str = ""


class NodeScriptedFakeRuntime(AgentRuntime):
    """Fake runtime keyed by ``request.node_id`` (falling back to ``role``),
    so each plan node can be scripted independently."""

    def __init__(self, scripts: dict[str, list[FakeStep]]):
        self.scripts = {key: list(steps) for key, steps in scripts.items()}
        self.requests: list[AgentRequest] = []
        # Files present in request.workspace at the start of each invoke,
        # keyed by node_id/role — used to assert upstream propagation into a
        # node's isolated worktree.
        self.workspace_files_at_start: dict[str, list[str]] = {}

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        key = request.node_id or request.role
        self.workspace_files_at_start.setdefault(key, self._list_files(request.workspace))
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
        if (step.writes or step.deletes) and request.access is not AgentAccess.WORKSPACE_WRITE:
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
            self._apply_deletes(request.workspace, step.deletes)
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

    def _apply_deletes(self, workspace: Path, deletes: list[str]) -> None:
        root = workspace.resolve()
        for relative in deletes:
            target = (root / relative).resolve()
            target.relative_to(root)  # path-escape guard
            if target.exists():
                target.unlink()

    @staticmethod
    def _list_files(workspace: Path) -> list[str]:
        root = workspace.resolve()
        if not root.is_dir():
            return []
        files: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative == ".git" or relative.startswith(".git/"):
                continue
            files.append(relative)
        return files

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


@contextmanager
def node_worktree_env():
    """Enable graph execution AND per-node isolated worktrees."""
    prior_exec = os.environ.get("WORKLOOP_EXECUTION")
    prior_node = os.environ.get("WORKLOOP_NODE_WORKTREE")
    os.environ["WORKLOOP_EXECUTION"] = "graph"
    os.environ["WORKLOOP_NODE_WORKTREE"] = "1"
    try:
        yield
    finally:
        for key, prior in (
            ("WORKLOOP_EXECUTION", prior_exec),
            ("WORKLOOP_NODE_WORKTREE", prior_node),
        ):
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


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

    def test_per_node_model_binding_flows_into_request(self) -> None:
        # The headline feature: each node's ModelBinding (provider/model/thinking)
        # reaches the runtime on the AgentRequest, so a single Pi runtime can
        # route per node (Opus-planning / GPT-execution / Kimi-ui) without a
        # separate runtime per node. This locks the wiring end-to-end.
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
            graph = PlanGraph(
                requirement_summary="后端加前端",
                nodes=[
                    planning_node(),
                    impl_node(
                        "backend",
                        "实现后端 API",
                        depends_on=["planning"],
                        model=ModelBinding(provider="openai", model="gpt-5", thinking="high"),
                    ),
                    impl_node(
                        "ui",
                        "实现前端页面",
                        depends_on=["backend"],
                        model=ModelBinding(provider="moonshot", model="kimi-k2", thinking="low"),
                    ),
                    review_node("review", depends_on=["ui"]),
                ],
            )
            workflow.save_plan_graph(task.task_id, graph)

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            backend_req = next(r for r in runtime.requests if r.node_id == "backend")
            ui_req = next(r for r in runtime.requests if r.node_id == "ui")
            # Each node's ModelBinding is honored on its request.
            self.assertEqual(backend_req.model, "gpt-5")
            self.assertEqual(backend_req.provider, "openai")
            self.assertEqual(backend_req.thinking, "high")
            self.assertEqual(ui_req.model, "kimi-k2")
            self.assertEqual(ui_req.provider, "moonshot")
            self.assertEqual(ui_req.thinking, "low")
            # node_id is carried so a node-aware runtime could route per node.
            self.assertEqual(backend_req.node_id, "backend")
            self.assertEqual(ui_req.node_id, "ui")

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


class NodeWorktreeExecutionTest(unittest.TestCase):
    """Per-node isolated git worktrees (WORKLOOP_NODE_WORKTREE=1 + graph).
    Each implementation node runs in its own detached worktree; writes merge
    back into the shared task worktree uncommitted, so the post-graph
    validation/review/delivery pipeline is unchanged."""

    def test_nodes_run_in_isolated_worktrees_and_merge_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, node_worktree_env():
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
            # Each node ran in its own worktree, distinct from the shared task
            # worktree and from each other.
            backend_req = next(r for r in runtime.requests if r.node_id == "backend")
            ui_req = next(r for r in runtime.requests if r.node_id == "ui")
            task_workspace = workflow.workspace_path(task.task_id)
            self.assertNotEqual(Path(backend_req.workspace), task_workspace)
            self.assertNotEqual(Path(ui_req.workspace), task_workspace)
            self.assertNotEqual(backend_req.workspace, ui_req.workspace)
            self.assertIn("node-worktrees", Path(backend_req.workspace).parts)
            # The UI node saw the backend's write propagated into its worktree.
            self.assertIn("backend.txt", runtime.workspace_files_at_start["ui"])
            # Node worktrees were cleaned up.
            node_wt_root = workflow.store.task_dir(task.task_id) / "node-worktrees"
            self.assertFalse((node_wt_root / "backend").exists())
            self.assertFalse((node_wt_root / "ui").exists())
            # All writes merged back into the shared task worktree (uncommitted).
            self.assertEqual(
                (task_workspace / "backend.txt").read_text(encoding="utf-8"), "api\n"
            )
            self.assertEqual(
                (task_workspace / "ui.txt").read_text(encoding="utf-8"), "page\n"
            )

    def test_upstream_deletions_propagate_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, node_worktree_env():
            root = Path(tmp)
            runtime = NodeScriptedFakeRuntime(
                {
                    "planner": [FakeStep(output=execution_plan(), session_id="planner-session")],
                    "cleaner": [
                        FakeStep(
                            output=executor_output(["删除 app.txt"], []),
                            deletes=["app.txt"],
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
            task = workflow.create_task("清理并新建", "删除 app.txt 再写前端", project.project_id)
            workflow.analyze(task.task_id)
            graph = PlanGraph(
                requirement_summary="清理并新建",
                nodes=[
                    planning_node(),
                    impl_node("cleaner", "删除 app.txt", depends_on=["planning"]),
                    impl_node("ui", "实现前端页面", depends_on=["cleaner"]),
                    review_node("review", depends_on=["ui"]),
                ],
            )
            workflow.save_plan_graph(task.task_id, graph)

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            task_workspace = workflow.workspace_path(task.task_id)
            # The deletion merged back into the shared task worktree.
            self.assertFalse((task_workspace / "app.txt").exists())
            self.assertEqual(
                (task_workspace / "ui.txt").read_text(encoding="utf-8"), "page\n"
            )
            # The downstream UI node did NOT see app.txt: the deletion
            # propagated into its isolated worktree before it ran.
            self.assertNotIn("app.txt", runtime.workspace_files_at_start["ui"])

    def test_resume_skips_completed_nodes_and_reruns_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, node_worktree_env():
            root = Path(tmp)
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
            # backend's write merged back during the first pass; ui failed (no merge).
            task_workspace = workflow_a.workspace_path(task.task_id)
            self.assertEqual(
                (task_workspace / "backend.txt").read_text(encoding="utf-8"), "api\n"
            )

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
            # Only the previously-failed node and the reviewer ran on resume;
            # the completed backend node was skipped (no worktree recreated).
            self.assertEqual([r.node_id for r in runtime_b.requests], ["ui", "review"])
            # Both nodes' writes are present in the shared task worktree.
            self.assertEqual(
                (task_workspace / "backend.txt").read_text(encoding="utf-8"), "api\n"
            )
            self.assertEqual(
                (task_workspace / "ui.txt").read_text(encoding="utf-8"), "page\n"
            )

    def test_graph_without_node_worktree_flag_uses_shared_worktree(self) -> None:
        # Graph mode ON but WORKLOOP_NODE_WORKTREE unset: nodes run in the
        # shared task worktree (regression guard for the per-node path).
        with tempfile.TemporaryDirectory() as tmp, graph_execution_env():
            root = Path(tmp)
            os.environ.pop("WORKLOOP_NODE_WORKTREE", None)
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
            self.assertFalse(completed.node_worktree)
            task_workspace = workflow.workspace_path(task.task_id)
            for request in runtime.requests:
                if request.role == "executor":
                    self.assertEqual(Path(request.workspace), task_workspace)
            self.assertFalse(
                (workflow.store.task_dir(task.task_id) / "node-worktrees").exists()
            )


# ---------------------------------------------------------------------------
# Bounded output self-repair
# ---------------------------------------------------------------------------


def invalid_executor_output() -> dict:
    """A shape a real text-JSON model emits that ExecutionResult.from_dict
    rejects: test items use name/status/detail instead of command/exit_code."""
    return {
        "completed_steps": ["写入 result.txt"],
        "modified_files": ["result.txt"],
        "tests": [{"name": "fake-check", "status": "pass", "detail": "ok"}],
        "deviations": [],
        "remaining_risks": [],
        "next_steps": [],
    }


def invalid_review_output() -> dict:
    """A shape a real text-JSON model emits that ReviewResult.from_dict
    rejects: acceptance_results instead of acceptance."""
    return {
        "verdict": "pass",
        "acceptance_results": [{"criterion": "result.txt 内容为 done", "passed": True}],
        "issues": [],
        "recommended_tests": [],
        "summary": "实现和验证均通过。",
    }


class OutputRepairTest(unittest.TestCase):
    """When a text-JSON runtime emits an unparseable output, the loop re-invokes
    once with the parse error and a strict schema reminder (bounded self-repair).
    Existing happy-path tests never trigger this — they emit valid output — so
    these are the only tests covering the repair path. Repair is bounded to one
    attempt; a still-bad output falls through to the existing failure handling."""

    def _linear_workflow(self, root, runtime):
        workflow, project = project_workflow(root, runtime)
        task = workflow.create_task("生成结果", "创建 result.txt", project.project_id)
        workflow.analyze(task.task_id)
        return workflow, task

    def test_executor_invalid_then_valid_self_repairs_and_delivers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, graph_execution_env():
            root = Path(tmp)
            runtime = NodeScriptedFakeRuntime(
                {
                    "planner": [FakeStep(output=execution_plan(), session_id="planner-session")],
                    "step-1": [
                        # First turn: writes the file but emits an unparseable
                        # result (the test item lacks command/exit_code).
                        FakeStep(
                            output=invalid_executor_output(),
                            writes={"result.txt": "done\n"},
                        ),
                        # Repair turn: emits a conforming result.
                        FakeStep(output=executor_output(["写入 result.txt"], ["result.txt"])),
                    ],
                    "review": [FakeStep(output=passing_review(), session_id="reviewer-session")],
                }
            )
            workflow, task = self._linear_workflow(root, runtime)

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            # The original turn's file write survives (the model wrote the file
            # correctly; only its JSON was wrong).
            self.assertEqual(
                (workflow.workspace_path(task.task_id) / "result.txt").read_text(encoding="utf-8"),
                "done\n",
            )
            self.assertEqual(completed.node_runs["step-1"]["status"], "completed")
            executor_reqs = [r for r in runtime.requests if r.role == "executor"]
            self.assertEqual(len(executor_reqs), 2)
            # The second executor invoke is the repair and carries the parse error.
            self.assertIn("解析错误", executor_reqs[1].instructions)

    def test_executor_repair_still_invalid_fails_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, graph_execution_env():
            root = Path(tmp)
            runtime = NodeScriptedFakeRuntime(
                {
                    "planner": [FakeStep(output=execution_plan(), session_id="planner-session")],
                    "step-1": [
                        FakeStep(output=invalid_executor_output()),
                        FakeStep(output=invalid_executor_output()),
                    ],
                    "review": [FakeStep(output=passing_review(), session_id="reviewer-session")],
                }
            )
            workflow, task = self._linear_workflow(root, runtime)
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
            self.assertIn("结果无效", result.node_runs["step-1"]["error"])
            # Bounded: exactly one original invoke + one repair invoke, no more.
            self.assertEqual(len([r for r in runtime.requests if r.role == "executor"]), 2)

    def test_reviewer_invalid_then_valid_self_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, graph_execution_env():
            root = Path(tmp)
            runtime = NodeScriptedFakeRuntime(
                {
                    "planner": [FakeStep(output=execution_plan(), session_id="planner-session")],
                    "step-1": [
                        FakeStep(
                            output=executor_output(["写入 result.txt"], ["result.txt"]),
                            writes={"result.txt": "done\n"},
                        )
                    ],
                    "review": [
                        FakeStep(output=invalid_review_output()),
                        FakeStep(output=passing_review()),
                    ],
                }
            )
            workflow, task = self._linear_workflow(root, runtime)

            completed = workflow.approve_plan(task.task_id)

            self.assertEqual(completed.status, AgentTaskStatus.READY_TO_DELIVER)
            review_reqs = [r for r in runtime.requests if r.role == "reviewer"]
            self.assertEqual(len(review_reqs), 2)
            self.assertIn("解析错误", review_reqs[1].instructions)

    def test_reviewer_repair_still_invalid_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, graph_execution_env():
            root = Path(tmp)
            runtime = NodeScriptedFakeRuntime(
                {
                    "planner": [FakeStep(output=execution_plan(), session_id="planner-session")],
                    "step-1": [
                        FakeStep(
                            output=executor_output(["写入 result.txt"], ["result.txt"]),
                            writes={"result.txt": "done\n"},
                        )
                    ],
                    "review": [
                        FakeStep(output=invalid_review_output()),
                        FakeStep(output=invalid_review_output()),
                    ],
                }
            )
            workflow, task = self._linear_workflow(root, runtime)

            result = workflow.approve_plan(task.task_id)

            self.assertEqual(result.status, AgentTaskStatus.FAILED)
            self.assertIn("审核结果无效", result.error)
            self.assertEqual(len([r for r in runtime.requests if r.role == "reviewer"]), 2)


if __name__ == "__main__":
    unittest.main()
