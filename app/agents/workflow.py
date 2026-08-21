from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from app.agents.contracts import (
    AgentAccess,
    AgentBudget,
    AgentEvent,
    AgentEventType,
    AgentPolicy,
    AgentRequest,
    AgentResult,
    AgentTask,
    AgentTaskStatus,
    ExecutionPlan,
    ExecutionResult,
    ReviewIssue,
    ReviewResult,
    ReviewVerdict,
    TaskBudget,
    ValidationResult,
)
from app.agents.context_ledger import (
    ContextLedger,
    ContextPack,
)
from app.agents import prompts
from app.agents.composition import ExecutionComposer
from app.agents.graph_executor import GraphExecutionMixin
from app.agents.plan_graph import (
    ModelBinding,
    PlanGraph,
    PlanNode,
    PlanNodeAccess,
    PlanNodeKind,
)
from app.agents.runtime import AgentRuntime
from app.agents.store import AgentTaskStore
from app.agents.task_budget import (
    agent_budget,
    task_budget_error,
    task_budget_overrun,
    usage_tokens,
)
from app.agents.workflow_config import (
    BUILTIN_WORKFLOWS,
    WorkflowCatalog,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowNodeKind,
    workflow_from_dict,
)
from app.core.contracts import (
    FileChange,
    PolicyBoundary,
    PolicyCheck,
    Severity,
    to_plain,
    utc_now,
)
from app.core.redaction import redact, redact_value
from app.memory.experience_store import ExperienceStore
from app.policy.policy_checker import PolicyChecker
from app.projects.contracts import Project, ProjectPolicy
from app.projects.directory_project import DirectoryProjectService
from app.projects.git_worktree import GitWorktreeService, PreparedWorktree
from app.projects.policy import ProjectPolicyLoader
from app.projects.registry import ProjectRegistry
from app.tools.workspace import Workspace
from app.validation.runner import DeterministicValidator

# Persisted run records store the agent session's events for diagnostics.
# A verbose reasoning-model session (long reasoning + tool I/O such as a
# validation command's output) can produce an enormous event stream; storing
# the full list made json.dumps(to_plain(record)) hit MemoryError mid-run
# (observed during self-dogfooding with a real glm-5.2 session). raw_events is
# never read back for control flow, so bounding it is safe and keeps the run
# record writable. Bound both the number of entries and any single string
# field so a huge tool_result cannot OOM the encoder either.
_MAX_RUN_EVENTS_KEPT = 400
_MAX_RUN_EVENT_FIELD_CHARS = 4000


def _bound_run_events(events: Any) -> list[Any]:
    """Return a diagnostic-safe, bounded copy of a run's event list.

    Caps the entry count (keeping the first and last portions with a
    truncation marker) and truncates any over-long string field inside each
    event, so serializing the run record cannot exhaust memory on a verbose
    session. Never used for control flow, so truncation loses nothing the
    system depends on."""
    if not isinstance(events, list) or not events:
        return list(events) if isinstance(events, list) else []

    def truncate(value: Any) -> Any:
        if isinstance(value, str):
            return value if len(value) <= _MAX_RUN_EVENT_FIELD_CHARS else (
                value[:_MAX_RUN_EVENT_FIELD_CHARS] + "…<truncated>"
            )
        if isinstance(value, list):
            # A nested list (a message's content parts, for example) is bounded
            # too, but say so instead of dropping the tail silently — a
            # diagnostic that lies about its own completeness is worse than a
            # long one.
            kept = [truncate(item) for item in value[:_MAX_RUN_EVENTS_KEPT]]
            if len(value) > _MAX_RUN_EVENTS_KEPT:
                kept.append(
                    {"type": "items_truncated", "dropped": len(value) - _MAX_RUN_EVENTS_KEPT}
                )
            return kept
        if isinstance(value, dict):
            return {key: truncate(val) for key, val in value.items()}
        return value

    bounded = [truncate(event) for event in events]
    if len(bounded) <= _MAX_RUN_EVENTS_KEPT:
        return bounded
    half = _MAX_RUN_EVENTS_KEPT // 2
    return [
        *bounded[:half],
        {"type": "events_truncated", "dropped": len(bounded) - _MAX_RUN_EVENTS_KEPT},
        *bounded[-half:],
    ]


# When a text-JSON runtime (PiRpcRuntime) emits output the strict from_dict
# parsers reject, the model often only needs a nudge: re-invoke once with the
# parse error and a strict schema reminder so it can self-correct, instead of
# failing the whole task. Bounded to one attempt; a still-bad output falls
# through to the existing failure handling. ClaudeCodeRuntime enforces the
# schema via tool input_schema and never reaches this path.
_MAX_OUTPUT_REPAIR_ATTEMPTS = 1




class TaskValidator(Protocol):
    def validate(
        self,
        task_id: str,
        workspace: Path,
        plan: ExecutionPlan,
        policy: ProjectPolicy,
    ) -> ValidationResult: ...


class AgentWorkflow(GraphExecutionMixin):
    """Persistent orchestration seam for the next-generation agent workflow."""

    def __init__(
        self,
        root: Path,
        runtime: AgentRuntime,
        validator: TaskValidator | None = None,
        max_iterations: int = 3,
        git_worktrees: GitWorktreeService | None = None,
        directory_projects: DirectoryProjectService | None = None,
        composer: ExecutionComposer | None = None,
        experience_store: ExperienceStore | None = None,
    ):
        if max_iterations <= 0:
            raise ValueError("max_iterations 必须大于 0。")
        self.root = Path(root)
        self.store = AgentTaskStore(self.root / "tasks")
        self.context_ledger = ContextLedger(self.root / "tasks")
        self.projects = ProjectRegistry(self.root / "projects")
        self.workflows = WorkflowCatalog(self.root / "workflows.json")
        self.git_worktrees = git_worktrees or GitWorktreeService()
        self.directory_projects = directory_projects or DirectoryProjectService(
            self.root / "managed-projects"
        )
        self.policy_loader = ProjectPolicyLoader()
        self.policy_checker = PolicyChecker()
        self.runtime = runtime
        self.composer = composer
        self.experience_store = experience_store
        self.validator = validator or DeterministicValidator()
        self.max_iterations = max_iterations
        self._task_state_lock = threading.RLock()

    def register_project(
        self,
        name: str,
        repository: Path,
        default_branch: str = "",
        config_path: str = ".workloop/project.toml",
    ) -> Project:
        if not name.strip():
            raise ValueError("项目名称不能为空。")
        requested = Path(repository).expanduser().resolve()
        if not requested.is_dir():
            raise ValueError(f"项目目录不存在或不可访问：{requested}")
        try:
            repo_root, detected_branch = self.git_worktrees.inspect(requested, "")
        except ValueError:
            repo_root, detected_branch = None, ""
        if repo_root == requested:
            branch = detected_branch
            if default_branch.strip():
                _, branch = self.git_worktrees.inspect(requested, default_branch)
            try:
                self.root.resolve().relative_to(repo_root)
            except ValueError:
                pass
            else:
                raise ValueError("Workloop 数据根必须位于目标 Git 仓库之外。")
            self.policy_loader.load(repo_root, config_path)
            return self.projects.add(
                Project(
                    name=name.strip(),
                    repository=str(repo_root),
                    default_branch=branch,
                    config_path=config_path,
                    workspace_mode="git",
                    source_directory=str(repo_root),
                    instructions="",
                )
            )

        project = Project(
            name=name.strip(),
            repository="",
            default_branch=default_branch.strip() or "main",
            config_path=config_path,
            workspace_mode="directory",
            source_directory=str(requested),
            instructions="",
        )
        project.repository = str(
            self.directory_projects.root / project.project_id / "repository"
        )
        project.managed_policy = self.directory_projects.initialize(project)
        try:
            self.policy_loader.load(Path(project.repository), config_path)
        except Exception:
            shutil.rmtree(Path(project.repository).parent, ignore_errors=True)
            raise
        return self.projects.add(project)

    def create_task(
        self,
        title: str,
        requirement: str,
        project_id: str,
        budget: TaskBudget | None = None,
        workflow_id: str = "quick",
    ) -> AgentTask:
        if not title.strip() or not requirement.strip():
            raise ValueError("任务标题和需求不能为空。")
        if not project_id.strip():
            raise ValueError("project_id 不能为空；新任务必须属于已注册 Git 项目。")
        effective_budget = replace(budget) if budget is not None else TaskBudget(
            max_iterations=self.max_iterations
        )
        effective_budget.validate()
        workflow = self.workflows.get(workflow_id)
        project = self.projects.get(project_id)
        source_digest = ""
        if project.workspace_mode == "directory":
            source_digest, _ = self.directory_projects.sync_source(project)
        task = AgentTask(
            title=title.strip(),
            requirement=requirement.strip(),
            project_id=project_id,
            workflow_id=workflow.workflow_id,
            workflow=to_plain(workflow),
            budget=effective_budget,
            graph_execution=True,
            source_digest=source_digest,
        )
        prepared = self.git_worktrees.plan(
            project,
            task.task_id,
            self.store.workspace_location(task.task_id),
        )
        task.base_commit = prepared.base_commit
        task.target_branch = prepared.target_branch
        task.task_branch = prepared.task_branch
        task.workspace = str(prepared.path)
        task.transition(AgentTaskStatus.PREPARING_WORKSPACE, reason="workspace_planned")
        self.store.save(task)
        self.git_worktrees.ensure_prepared(project, prepared)
        task.transition(AgentTaskStatus.DRAFT, reason="workspace_prepared")
        self.store.save(task)
        return task

    def get_task(self, task_id: str) -> AgentTask:
        return self.store.load(task_id)

    def get_project(self, project_id: str) -> Project:
        return self.projects.get(project_id)

    def get_plan(self, task_id: str) -> ExecutionPlan:
        return self._load_plan(self.store.load(task_id))

    def get_plan_graph(self, task_id: str) -> PlanGraph:
        task = self.store.load(task_id)
        if task.plan_graph:
            return PlanGraph.from_dict(task.plan_graph)
        plan = self.get_plan(task_id)
        graph = self.composer.compose(plan) if self.composer is not None else PlanGraph.from_execution_plan(plan)
        task.plan_graph = graph.to_dict()
        self.store.save(task)
        return graph

    def save_plan_graph(self, task_id: str, graph: PlanGraph) -> AgentTask:
        graph.validate()
        if self.composer is not None:
            graph = self.composer.normalize(graph)
        with self._task_state_lock:
            task = self.store.load(task_id)
            if task.status is not AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL:
                raise ValueError(
                    "plan graph can only be changed while waiting for plan approval; "
                    f"current status is {task.status.value}"
                )
            graph_ref = f"artifacts/plan-graphs/{graph.version}.json"
            self.store.write_json(self.store.task_dir(task_id) / graph_ref, graph.to_dict())
            task.plan_graph = graph.to_dict()
            task.artifacts["plan_graph"] = graph_ref
            self.store.save(task)
            return task

    def get_workflow(self, task_id: str) -> WorkflowDefinition:
        return self._task_workflow(self.store.load(task_id))

    def requires_plan_approval(self, task_id: str) -> bool:
        return self.get_workflow(task_id).requires_plan_approval

    def record_clarification(self, task_id: str, answer: str, question: str = "") -> AgentTask:
        """Answer one open question (defaults to the first); kept for API compatibility."""
        question = question.strip()
        return self.record_clarifications(
            task_id,
            [{"question": question} if question else {}],
            [answer],
        )

    def record_clarifications(
        self,
        task_id: str,
        questions: list[dict],
        answers: list[str],
    ) -> AgentTask:
        """Record answers for every open question of the current plan at once."""
        if len(questions) != len(answers):
            raise ValueError("澄清问题与答复数量不一致。")
        task = self.store.load(task_id)
        self._require_status(task, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
        plan = self._load_plan(task)
        if not plan.open_questions:
            raise ValueError("当前计划没有待回答的澄清问题。")
        open_questions = list(plan.open_questions)
        entries = []
        for index, (raw_question, raw_answer) in enumerate(zip(questions, answers), start=1):
            question = str(raw_question.get("question", "")).strip()
            if not question:
                question = open_questions[0]
            if question not in open_questions:
                raise ValueError(f"第 {index} 条答复的问题不在当前计划的待澄清列表中：{question}")
            cleaned = raw_answer.strip()
            if not cleaned:
                raise ValueError(f"第 {index} 条澄清答复不能为空。")
            entries.append({"question": question, "answer": cleaned, "at": utc_now()})
        task.clarifications.extend(entries)
        self.store.save(task)
        self.store.append_event(
            task.task_id,
            {"type": "clarified", "status": task.status.value, "count": len(entries)},
        )
        return task

    def resume_task_creation(self, task_id: str) -> AgentTask:
        task = self.store.load(task_id)
        self._require_status(task, AgentTaskStatus.PREPARING_WORKSPACE)
        project = self.projects.get(task.project_id)
        prepared = self._prepared_from_task(task, project)
        self.git_worktrees.ensure_prepared(project, prepared)
        task.transition(AgentTaskStatus.DRAFT, reason="workspace_prepared")
        self.store.save(task)
        return task

    def cancel_task(self, task_id: str) -> AgentTask:
        with self._task_state_lock:
            task = self.store.load(task_id)
            if not task.project_id or not task.workspace or not task.task_branch:
                raise ValueError("任务没有可清理的项目 worktree。")
            project = self.projects.get(task.project_id)
            prepared = self._prepared_from_task(task, project)
            active_statuses = {
                AgentTaskStatus.ANALYZING,
                AgentTaskStatus.EXECUTING,
                AgentTaskStatus.REVIEWING,
                AgentTaskStatus.REPLANNING,
            }
            if task.status in active_statuses:
                task.transition(AgentTaskStatus.CANCELLING, reason="active_run_cancel_requested")
                self.store.save(task)
                active = True
                retrying = False
            elif task.status in (
                AgentTaskStatus.DRAFT,
                AgentTaskStatus.PREPARING_WORKSPACE,
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
                AgentTaskStatus.QUEUED_FOR_ANALYSIS,
                AgentTaskStatus.QUEUED_FOR_EXECUTION,
                AgentTaskStatus.QUEUED_FOR_RECOVERY,
                AgentTaskStatus.INTERRUPTED,
                AgentTaskStatus.PAUSED,
                AgentTaskStatus.INTEGRATION_REQUIRED,
            ):
                task.transition(AgentTaskStatus.CANCELLING, reason="user_cancelled")
                self.store.save(task)
                active = False
                retrying = False
            elif task.status is AgentTaskStatus.CANCELLING:
                reason = task.transitions[-1].get("reason", "") if task.transitions else ""
                active = reason in {"active_run_cancel_requested", "runtime_cancelled"}
                retrying = True
            else:
                raise ValueError(
                    f"任务 {task.task_id} 状态为 {task.status.value}，无法取消。"
                )
        if active:
            # The delegate may have exited between its final event and this request.
            # The persisted cancellation intent remains authoritative in that race.
            delegate_found = self.runtime.cancel(task.task_id)
            if not retrying or delegate_found:
                return task
        self.git_worktrees.remove(project, prepared)
        with self._task_state_lock:
            task.transition(AgentTaskStatus.CANCELLED, reason="workspace_removed")
            self.store.save(task)
        return task

    def workspace_path(self, task_id: str) -> Path:
        task = self.store.load(task_id)
        if not task.workspace:
            raise ValueError(f"任务 {task_id} 没有 Git worktree。")
        return Path(task.workspace)

    def resume_interrupted(self, task_id: str, rerun: bool = False) -> AgentTask:
        task = self.store.load(task_id)
        self._require_status(task, AgentTaskStatus.QUEUED_FOR_RECOVERY)
        try:
            phase = AgentTaskStatus(task.interrupted_status)
        except ValueError as error:
            raise ValueError(
                f"任务 {task_id} 的中断阶段无效：{task.interrupted_status!r}。"
            ) from error
        if rerun:
            self._clear_phase_sessions(task, phase)
            self.store.save(task)
        pause_reason = task.pause_reason
        task.pause_reason = ""
        task.error = ""
        self.store.save(task)
        if phase is AgentTaskStatus.ANALYZING:
            return self.analyze(task_id)
        if phase is AgentTaskStatus.REPLANNING:
            plan = self._load_plan(task)
            review = self._load_round_review(task)
            policy = self._load_project_policy(task)
            task.transition(AgentTaskStatus.REVIEWING, reason="resume_replanning")
            self.store.save(task)
            return self._replan(task, plan, review, policy)
        if phase not in {
            AgentTaskStatus.EXECUTING,
            AgentTaskStatus.VALIDATING,
            AgentTaskStatus.REVIEWING,
        }:
            raise ValueError(f"任务 {task_id} 的阶段 {phase.value} 不支持自动恢复。")
        plan = self._load_plan(task)
        policy = self._load_project_policy(task)
        effective_agent_policy = self._agent_policy(policy, plan.required_tests)
        workspace = Workspace(self.workspace_path(task.task_id))
        base = self._load_workspace_base(task)
        pending_revision = (
            phase is AgentTaskStatus.REVIEWING
            and bool(task.revision_target_node_id)
        )
        if pending_revision:
            feedback, new_round = self._load_pending_revision(task)
        elif (
            phase is AgentTaskStatus.EXECUTING and pause_reason == "max_iterations"
        ):
            feedback = self._load_round_review(task)
            new_round = True
        else:
            feedback = (
                self._load_previous_revision_feedback(task)
                if phase is AgentTaskStatus.EXECUTING
                else None
            )
            new_round = False
        return self._run_approved_plan(
            task=task,
            plan=plan,
            policy=policy,
            effective_agent_policy=effective_agent_policy,
            workspace=workspace,
            base=base,
            phase=phase,
            new_round=new_round,
            review_feedback=feedback,
        )

    @staticmethod
    def _clear_phase_sessions(task: AgentTask, phase: AgentTaskStatus) -> None:
        """Discard only the model sessions owned by the phase being rerun."""
        keys: set[str] = set()
        aliases: set[str] = set()
        if phase in {AgentTaskStatus.ANALYZING, AgentTaskStatus.REPLANNING}:
            keys.add("node:planning")
            aliases.add("planner")
        elif phase is AgentTaskStatus.REVIEWING:
            keys.add("node:review")
            aliases.add("reviewer")
        elif phase is AgentTaskStatus.EXECUTING:
            aliases.add("executor")
            try:
                graph = PlanGraph.from_dict(task.plan_graph)
            except (TypeError, ValueError):
                graph = None
            if graph is not None:
                for node in graph.execution_nodes():
                    keys.add(f"node:{node.node_id}")
                    state = task.node_runs.get(node.node_id)
                    if isinstance(state, dict):
                        state["session_id"] = ""
        for key in keys | aliases:
            task.sessions.pop(key, None)

    def revalidate_integrated(self, task_id: str) -> AgentTask:
        task = self.store.load(task_id)
        self._require_status(task, AgentTaskStatus.INTEGRATING)
        plan = self._load_plan(task)
        policy = self._load_project_policy(task)
        policy.required_commands(plan.required_tests)
        workflow = self._task_workflow(task)
        last_executor = max(
            index
            for index, node in enumerate(workflow.nodes)
            if node.kind is WorkflowNodeKind.EXECUTOR
        )
        task.workflow_cursor = last_executor + 1
        task.iteration += 1
        self.store.save(task)
        return self._run_approved_plan(
            task=task,
            plan=plan,
            policy=policy,
            effective_agent_policy=self._agent_policy(policy, plan.required_tests),
            workspace=Workspace(self.workspace_path(task_id)),
            base=self._load_workspace_base(task),
            phase=AgentTaskStatus.VALIDATING,
            new_round=False,
            review_feedback=None,
        )

    def analyze(self, task_id: str) -> AgentTask:
        task = self.store.load(task_id)
        self._require_status_in(
            task,
            {
                AgentTaskStatus.DRAFT,
                AgentTaskStatus.QUEUED_FOR_ANALYSIS,
                AgentTaskStatus.QUEUED_FOR_RECOVERY,
            },
        )
        policy = self._load_project_policy(task)
        if not self._transition_unless_cancelled(task, AgentTaskStatus.ANALYZING):
            return self._finish_or_return_cancellation(task)

        workflow = self._task_workflow(task)
        planner_node = workflow.node(WorkflowNodeKind.PLANNER)
        planning_binding = self._planning_binding(task)
        response = self._invoke_agent(
            task,
            AgentRequest(
                task_id=task.task_id,
                role="planner",
                instructions=self._planner_instructions(task, policy, planner_node),
                workspace=self.workspace_path(task.task_id),
                access=AgentAccess.READ_ONLY,
                policy=self._agent_policy(policy, []),
                workflow_node_id=planner_node.node_id,
                **self._planning_request_fields(task, planning_binding),
            ),
        )
        if not response.succeeded:
            return self._fail(task, response)

        try:
            plan = self._execution_plan_from_output(
                response.output,
                task.requirement,
                policy,
            )
            policy.required_commands(plan.required_tests)
        except ValueError as error:
            return self._fail(task, AgentResult(succeeded=False, error=f"规划结果无效：{error}"))
        task.plan_version += 1
        task.workflow_cursor = 1
        task.sessions["planner"] = response.session_id
        plan_ref = f"artifacts/plans/{task.plan_version}.json"
        self.store.write_json(self.store.task_dir(task.task_id) / plan_ref, plan)
        task.artifacts["plan"] = plan_ref
        self._save_generated_plan_graph(task, plan, planning_binding)
        if not self._transition_unless_cancelled(
            task, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL
        ):
            return self._finish_or_return_cancellation(task)
        return task

    def approve_plan(self, task_id: str) -> AgentTask:
        task = self.store.load(task_id)
        self._require_status_in(
            task,
            {
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
                AgentTaskStatus.QUEUED_FOR_EXECUTION,
                AgentTaskStatus.QUEUED_FOR_RECOVERY,
            },
        )
        plan = self._load_plan(task)
        if plan.open_questions:
            raise ValueError("计划仍有未决问题，不能批准。")
        project = self.projects.get(task.project_id)
        self.git_worktrees.ensure_prepared(
            project,
            self._prepared_from_task(task, project),
            allow_task_changes=task.approved_plan_version > 0,
        )
        policy = self.policy_loader.load(self.workspace_path(task.task_id), project.config_path)
        policy.required_commands(plan.required_tests)
        effective_agent_policy = self._agent_policy(policy, plan.required_tests)
        if task.plan_graph:
            graph = PlanGraph.from_dict(task.plan_graph)
            if self.composer is not None:
                graph = self.composer.normalize(graph)
            graph = replace(graph, status="approved", approved_at=utc_now())
            graph_ref = f"artifacts/plan-graphs/{graph.version}.json"
            self.store.write_json(
                self.store.task_dir(task.task_id) / graph_ref,
                graph.to_dict(),
            )
            task.plan_graph = graph.to_dict()
            task.artifacts["plan_graph"] = graph_ref

        # Per-node isolated worktrees (only meaningful with graph execution):
        # each implementation node runs in its own detached git worktree, then
        # its writes are merged back into the shared task worktree uncommitted.
        task.node_worktree = (
            task.graph_execution
            and os.environ.get("WORKLOOP_NODE_WORKTREE", "").strip().lower()
            in {"1", "true", "yes", "on", "isolate", "node"}
        )

        if task.approved_plan_version != task.plan_version:
            task.plan_iteration = 0
        task.approved_plan_version = task.plan_version
        workflow = self._task_workflow(task)
        if task.workflow_cursor <= 0:
            task.workflow_cursor = 1
        if (
            task.workflow_cursor < len(workflow.nodes)
            and workflow.nodes[task.workflow_cursor].kind is WorkflowNodeKind.PLAN_APPROVAL
        ):
            task.workflow_cursor += 1
        workspace_path = self.workspace_path(task.task_id)
        workspace = Workspace(workspace_path)
        if task.artifacts.get("workspace_base"):
            base = json.loads(
                (
                    self.store.task_dir(task.task_id)
                    / task.artifacts["workspace_base"]
                ).read_text(encoding="utf-8")
            )
        else:
            base = workspace.snapshot()
            base_ref = "artifacts/workspace-base.json"
            self.store.write_json(self.store.task_dir(task.task_id) / base_ref, base)
            task.artifacts["workspace_base"] = base_ref
        policy_ref = "artifacts/project-policy.json"
        self.store.write_json(self.store.task_dir(task.task_id) / policy_ref, policy)
        task.artifacts["project_policy"] = policy_ref
        if not self._save_unless_cancelled(task):
            return self._finish_or_return_cancellation(task)
        return self._run_approved_plan(
            task=task,
            plan=plan,
            policy=policy,
            effective_agent_policy=effective_agent_policy,
            workspace=workspace,
            base=base,
            phase=AgentTaskStatus.EXECUTING,
            new_round=True,
            review_feedback=None,
        )

    def _run_approved_plan(
        self,
        task: AgentTask,
        plan: ExecutionPlan,
        policy: ProjectPolicy,
        effective_agent_policy: AgentPolicy,
        workspace: Workspace,
        base: dict[str, str],
        phase: AgentTaskStatus,
        new_round: bool,
        review_feedback: ReviewResult | None,
    ) -> AgentTask:
        workspace_path = self.workspace_path(task.task_id)
        workflow = self._task_workflow(task)
        if task.workflow_cursor <= 0:
            task.workflow_cursor = self._workflow_cursor_for_phase(workflow, phase)
            self.store.save(task)

        while True:
            if task.workflow_cursor >= len(workflow.nodes):
                return self._fail(
                    task,
                    AgentResult(succeeded=False, error="工作流游标越过了 delivery 节点。"),
                )
            node = workflow.nodes[task.workflow_cursor]
            if node.kind is WorkflowNodeKind.PLAN_APPROVAL:
                task.workflow_cursor += 1
                self.store.save(task)
                continue
            if node.kind is WorkflowNodeKind.DELIVERY:
                task.error = ""
                task.pause_reason = ""
                if not self._transition_unless_cancelled(
                    task,
                    AgentTaskStatus.READY_TO_DELIVER,
                    reason=f"workflow_node_completed:{node.node_id}",
                ):
                    return self._finish_or_return_cancellation(task)
                return task
            if node.kind is WorkflowNodeKind.PLANNER:
                return self._fail(
                    task,
                    AgentResult(succeeded=False, error="已批准工作流不能再次进入 planner 节点。"),
                )

            phase = self._workflow_status(node)
            if new_round:
                if task.plan_iteration >= task.budget.max_iterations:
                    return self._pause(
                        task,
                        "max_iterations",
                        f"代码返修达到最大轮次 {task.budget.max_iterations}。",
                        resume_phase=phase,
                    )
                task.iteration += 1
                task.plan_iteration += 1
                new_round = False
                self.store.save(task)

            if (
                task.status is not phase
                and not self._transition_unless_cancelled(
                    task,
                    phase,
                    reason=f"workflow_node_started:{node.node_id}",
                )
            ):
                return self._finish_or_return_cancellation(task)

            round_dir = self._round_dir(task)
            node_dir = self._workflow_node_dir(round_dir, task.workflow_cursor, node)
            if node.kind is WorkflowNodeKind.EXECUTOR:
                node_review_feedback = (
                    review_feedback
                    if task.revision_target_node_id == node.node_id
                    else None
                )
                round_dir = self._round_dir(task)
                before_check = self._check_workspace_policy(workspace, base, policy)
                self.store.write_json(round_dir / "policy-before.json", before_check)
                self.store.write_json(node_dir / "policy-before.json", before_check)
                if not before_check.passed:
                    return self._block_policy(task, before_check)
                if task.graph_execution:
                    if task.graph_workflow_node_id != node.node_id:
                        self._reset_write_nodes_for_revision(task, plan)
                        task.graph_workflow_node_id = node.node_id
                        self.store.save(task)
                    graph_outcome = self._execute_plan_graph(
                        task=task,
                        plan=plan,
                        policy=policy,
                        effective_agent_policy=effective_agent_policy,
                        workspace=workspace,
                        workspace_path=workspace_path,
                        base=base,
                        round_dir=round_dir,
                        review_feedback=node_review_feedback,
                        workflow_node=node,
                    )
                    if graph_outcome is not None:
                        return graph_outcome
                    execution_result = ExecutionResult.from_dict(
                        json.loads((round_dir / "execution.json").read_text(encoding="utf-8"))
                    )
                else:
                    executor_request = AgentRequest(
                        task_id=task.task_id,
                        role="executor",
                        instructions=self._executor_instructions(
                            task, plan, node_review_feedback, node
                        ),
                        workspace=workspace_path,
                        access=AgentAccess.WORKSPACE_WRITE,
                        policy=effective_agent_policy,
                        budget=agent_budget(task),
                        workflow_node_id=node.node_id,
                        **self._node_request_fields(
                            task, PlanNodeKind.IMPLEMENTATION, "executor", node
                        ),
                    )
                    execution = self._invoke_agent(task, executor_request)
                    if not execution.succeeded:
                        return self._fail(task, execution)
                    try:
                        execution_result = ExecutionResult.from_dict(execution.output)
                    except ValueError as error:
                        # Same bounded self-repair as the graph node path: an
                        # unparseable ExecutionResult no longer fails the whole
                        # task outright. Re-invoke once with the parse error and
                        # a schema reminder; fall through to failure if still bad.
                        repaired = self._repair_node_output(
                            task, executor_request, error, execution.output
                        )
                        if repaired is None:
                            return self._fail(
                                task,
                                AgentResult(succeeded=False, error=f"执行结果无效：{error}"),
                            )
                        execution_result = repaired[0]
                    self.store.write_json(round_dir / "execution.json", execution_result)
                self.store.write_json(node_dir / "execution.json", execution_result)
                current = workspace.snapshot()
                diff = workspace.diff(base, current)
                self.store.write_text(round_dir / "changes.diff", diff)
                self.store.write_text(node_dir / "changes.diff", diff)
                if task.revision_target_node_id == node.node_id:
                    task.revision_target_node_id = ""
                    task.revision_feedback_iteration = 0
                    review_feedback = None
                self._advance_workflow_cursor(task)
                continue

            if node.kind is WorkflowNodeKind.VALIDATION:
                after_check = self._check_workspace_policy(workspace, base, policy)
                self.store.write_json(round_dir / "policy-after.json", after_check)
                self.store.write_json(node_dir / "policy-after.json", after_check)
                if not after_check.passed:
                    return self._block_policy(task, after_check)
                budget_error = task_budget_error(task)
                if budget_error:
                    return self._pause(task, budget_error)
                validation_started = time.monotonic()
                validation_run_path = round_dir / "validation-run.json"
                validation_run = {
                    "schema_version": 1,
                    "task_id": task.task_id,
                    "round": task.iteration,
                    "status": "running",
                    "budget": to_plain(task.budget),
                    "workflow_node_id": node.node_id,
                    "workflow_node_instructions": node.instructions,
                    "started_at": utc_now(),
                    "finished_at": "",
                    "error": "",
                }
                self.store.write_json(validation_run_path, validation_run)
                self.store.write_json(node_dir / "validation-run.json", validation_run)
                try:
                    validation = self.validator.validate(
                        task.task_id,
                        workspace_path,
                        plan,
                        policy,
                    )
                except Exception as error:  # noqa: BLE001 - persist validator failures
                    validation_run.update(
                        {
                            "status": "failed",
                            "finished_at": utc_now(),
                            "error": str(error),
                        }
                    )
                    self.store.write_json(validation_run_path, validation_run)
                    self.store.write_json(node_dir / "validation-run.json", validation_run)
                    return self._fail(
                        task,
                        AgentResult(succeeded=False, error=f"验证器异常：{error}"),
                    )
                finally:
                    task.budget.consumed_active_seconds += (
                        time.monotonic() - validation_started
                    )
                    self.store.save(task)
                validation_run.update(
                    {
                        "status": "succeeded",
                        "finished_at": utc_now(),
                        "budget": to_plain(task.budget),
                        "passed": validation.passed,
                    }
                )
                self.store.write_json(validation_run_path, validation_run)
                self.store.write_json(round_dir / "validation.json", validation)
                self.store.write_json(node_dir / "validation-run.json", validation_run)
                self.store.write_json(node_dir / "validation.json", validation)
                validation_check = self._check_workspace_policy(workspace, base, policy)
                self.store.write_json(
                    round_dir / "policy-validation.json",
                    validation_check,
                )
                self.store.write_json(node_dir / "policy-validation.json", validation_check)
                current = workspace.snapshot()
                diff = workspace.diff(base, current)
                self.store.write_text(round_dir / "changes.diff", diff)
                self.store.write_text(node_dir / "changes.diff", diff)
                if not validation_check.passed:
                    return self._block_policy(task, validation_check)
                if not validation.passed:
                    return self._pause(
                        task,
                        "validation_failed",
                        validation.error or "必需验证未通过。",
                        resume_phase=AgentTaskStatus.VALIDATING,
                    )
                self._advance_workflow_cursor(task)
                budget_error = task_budget_overrun(task)
                if budget_error:
                    resume_phase = self._workflow_status(
                        workflow.nodes[task.workflow_cursor]
                    )
                    return self._pause(
                        task,
                        budget_error,
                        resume_phase=resume_phase,
                    )
                continue

            validation = self._optional_round_validation(
                round_dir,
                workflow,
                task.workflow_cursor,
            )
            current = workspace.snapshot()
            diff = workspace.diff(base, current)
            review_request = AgentRequest(
                task_id=task.task_id,
                role="reviewer",
                instructions=self._reviewer_instructions(
                    task, plan, diff, validation, node
                ),
                workspace=workspace_path,
                access=AgentAccess.READ_ONLY,
                policy=effective_agent_policy,
                budget=agent_budget(task),
                artifact_root=self.store.task_dir(task.task_id),
                workflow_node_id=node.node_id,
                **self._node_request_fields(task, PlanNodeKind.REVIEW, "reviewer", node),
            )
            review = self._invoke_agent(task, review_request)
            if not review.succeeded:
                return self._fail(task, review)
            # A parse error is a shape problem a model can self-correct on a
            # second turn, so only that triggers the bounded repair. A pass
            # verdict whose acceptance does not line up with the approved plan
            # is also fixable output drift: degrade it to a revision round so
            # the executor/reviewer loop converges instead of failing the task.
            try:
                review_result = ReviewResult.from_dict(review.output)
            except ValueError as error:
                repaired = self._repair_review_output(
                    task, review_request, error, review.output
                )
                if repaired is None:
                    return self._fail(
                        task,
                        AgentResult(succeeded=False, error=f"审核结果无效：{error}"),
                    )
                review_result = repaired[0]
            try:
                review_result.validate_pass(plan)
            except ValueError as error:
                review_result = self._inconsistent_pass_review(review_result, error)
            self.store.write_json(round_dir / "review.json", review_result)
            self.store.write_json(node_dir / "review.json", review_result)

            verdict = review_result.verdict
            if verdict is ReviewVerdict.PASS:
                self._advance_workflow_cursor(task)
                continue
            if verdict is ReviewVerdict.REVISE_CODE:
                review_feedback = review_result
                # Graph execution treats a node whose node_runs say "completed"
                # as done, so an interrupted task resumes without redoing
                # finished work. A revision round is the opposite case: the
                # reviewer rejected the result, so every write node has to run
                # again against the feedback. Without this reset the graph has
                # nothing ready, the round is a silent no-op, and the task
                # burns its whole iteration budget re-reviewing an unchanged
                # worktree until it pauses on max_iterations.
                self._reset_write_nodes_for_revision(task, plan)
                task.graph_workflow_node_id = ""
                executor_index = self._preceding_executor_index(
                    workflow, task.workflow_cursor
                )
                if executor_index is None:
                    task.error = "审核要求返修，但该 reviewer 之前没有 executor 节点。"
                    if not self._transition_unless_cancelled(
                        task, AgentTaskStatus.BLOCKED
                    ):
                        return self._finish_or_return_cancellation(task)
                    return task
                task.revision_target_node_id = workflow.nodes[executor_index].node_id
                task.revision_feedback_iteration = task.iteration
                task.workflow_cursor = executor_index
                self.store.save(task)
                new_round = True
                continue
            if verdict is ReviewVerdict.REPLAN:
                return self._replan(task, plan, review_result, policy)

            task.error = f"审核要求人工处理：{verdict.value}"
            if not self._transition_unless_cancelled(task, AgentTaskStatus.BLOCKED):
                return self._finish_or_return_cancellation(task)
            return task

    @staticmethod
    def _workflow_status(node: WorkflowNode) -> AgentTaskStatus:
        statuses = {
            WorkflowNodeKind.EXECUTOR: AgentTaskStatus.EXECUTING,
            WorkflowNodeKind.VALIDATION: AgentTaskStatus.VALIDATING,
            WorkflowNodeKind.REVIEWER: AgentTaskStatus.REVIEWING,
        }
        try:
            return statuses[node.kind]
        except KeyError as error:
            raise ValueError(f"节点 {node.node_id} 不是可运行的中间节点。") from error

    @staticmethod
    def _workflow_cursor_for_phase(
        workflow: WorkflowDefinition,
        phase: AgentTaskStatus,
    ) -> int:
        kind_by_status = {
            AgentTaskStatus.EXECUTING: WorkflowNodeKind.EXECUTOR,
            AgentTaskStatus.VALIDATING: WorkflowNodeKind.VALIDATION,
            AgentTaskStatus.REVIEWING: WorkflowNodeKind.REVIEWER,
        }
        expected = kind_by_status.get(phase)
        if expected is not None:
            for index, node in enumerate(workflow.nodes):
                if node.kind is expected:
                    return index
        cursor = 1
        if workflow.nodes[cursor].kind is WorkflowNodeKind.PLAN_APPROVAL:
            cursor += 1
        return cursor

    @staticmethod
    def _workflow_node_dir(round_dir: Path, cursor: int, node: WorkflowNode) -> Path:
        return round_dir / "workflow-nodes" / f"{cursor + 1}-{node.node_id}"

    def _advance_workflow_cursor(self, task: AgentTask) -> None:
        task.workflow_cursor += 1
        self.store.save(task)

    @staticmethod
    def _preceding_executor_index(
        workflow: WorkflowDefinition,
        cursor: int,
    ) -> int | None:
        for index in range(cursor - 1, -1, -1):
            if workflow.nodes[index].kind is WorkflowNodeKind.EXECUTOR:
                return index
        return None

    @staticmethod
    def _optional_round_validation(
        round_dir: Path,
        workflow: WorkflowDefinition,
        cursor: int,
    ) -> ValidationResult | None:
        preceding_nodes = workflow.nodes[:cursor]
        last_executor = max(
            (
                index
                for index, node in enumerate(preceding_nodes)
                if node.kind is WorkflowNodeKind.EXECUTOR
            ),
            default=-1,
        )
        last_validation = max(
            (
                index
                for index, node in enumerate(preceding_nodes)
                if node.kind is WorkflowNodeKind.VALIDATION
            ),
            default=-1,
        )
        if last_validation < last_executor:
            return None
        path = round_dir / "validation.json"
        if not path.is_file():
            return None
        return ValidationResult.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _replan(
        self,
        task: AgentTask,
        previous_plan: ExecutionPlan,
        review: ReviewResult,
        policy: ProjectPolicy,
    ) -> AgentTask:
        if not self._transition_unless_cancelled(task, AgentTaskStatus.REPLANNING):
            return self._finish_or_return_cancellation(task)
        task.node_worktree = False
        task.graph_workflow_node_id = ""
        task.revision_target_node_id = ""
        task.revision_feedback_iteration = 0
        task.node_runs = {}
        workflow = self._task_workflow(task)
        planner_node = workflow.node(WorkflowNodeKind.PLANNER)
        planning_binding = self._planning_binding(task)
        response = self._invoke_agent(
            task,
            AgentRequest(
                task_id=task.task_id,
                role="planner",
                instructions=self._replanner_instructions(task, previous_plan, review),
                workspace=self.workspace_path(task.task_id),
                access=AgentAccess.READ_ONLY,
                policy=self._agent_policy(policy, []),
                budget=agent_budget(task),
                workflow_node_id=planner_node.node_id,
                **self._planning_request_fields(task, planning_binding),
            ),
        )
        if not response.succeeded:
            return self._fail(task, response)
        try:
            plan = self._execution_plan_from_output(
                response.output,
                task.requirement,
                policy,
            )
            policy.required_commands(plan.required_tests)
        except ValueError as error:
            return self._fail(
                task,
                AgentResult(succeeded=False, error=f"重新规划结果无效：{error}"),
            )
        task.plan_version += 1
        task.workflow_cursor = 1
        plan_ref = f"artifacts/plans/{task.plan_version}.json"
        self.store.write_json(self.store.task_dir(task.task_id) / plan_ref, plan)
        task.artifacts["plan"] = plan_ref
        self._save_generated_plan_graph(task, plan, planning_binding)
        task.error = ""
        if not self._transition_unless_cancelled(
            task,
            AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            reason="review_requested_replan",
        ):
            return self._finish_or_return_cancellation(task)
        return task

    @staticmethod
    def _execution_plan_from_output(
        output: dict,
        requirement: str,
        policy: ProjectPolicy,
    ) -> ExecutionPlan:
        try:
            canonical_plan = ExecutionPlan.from_dict(output)
        except ValueError as error:
            canonical_error = str(error)
        else:
            canonical_data = to_plain(canonical_plan)
            canonical_data["required_tests"] = AgentWorkflow._explicit_policy_tests(
                output,
                policy,
            )
            try:
                return ExecutionPlan.from_dict(canonical_data)
            except ValueError as error:
                raise ValueError(f"Workloop ExecutionPlan 无效：{error}") from error
        if not isinstance(output, dict) or not isinstance(output.get("title"), str):
            raise ValueError(canonical_error)

        raw_tasks = output.get("tasks")
        if not isinstance(raw_tasks, list):
            raw_plan = output.get("plan")
            raw_tasks = raw_plan.get("steps") if isinstance(raw_plan, dict) else None
        if not isinstance(raw_tasks, list):
            raw_tasks = output.get("steps")
        if not isinstance(raw_tasks, list):
            raise ValueError(canonical_error)
        steps = []
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            description = item.get("description")
            title = item.get("title")
            text = description if isinstance(description, str) and description else title
            if isinstance(text, str) and text:
                steps.append(text)

        raw_requirements = output.get("requirements")
        acceptance = (
            raw_requirements.get("acceptance_criteria")
            if isinstance(raw_requirements, dict)
            else output.get("acceptance_criteria")
        )
        acceptance = (
            list(acceptance)
            if isinstance(acceptance, list)
            and all(isinstance(item, str) for item in acceptance)
            else []
        )
        if not acceptance:
            acceptance = [
                match.group(1).strip()
                for line in requirement.splitlines()
                if (match := re.match(r"^\s*\d+\.\s+(.+?)\s*$", line))
            ]
        open_questions = (
            raw_requirements.get("clarifications", [])
            if isinstance(raw_requirements, dict)
            else output.get("open_questions", [])
        )
        open_questions = (
            list(open_questions)
            if isinstance(open_questions, list)
            and all(isinstance(item, str) for item in open_questions)
            else []
        )
        files = output.get("files", [])
        files = (
            list(files)
            if isinstance(files, list) and all(isinstance(item, str) for item in files)
            else []
        )
        risks = output.get("risks", [])
        risks = (
            list(risks)
            if isinstance(risks, list) and all(isinstance(item, str) for item in risks)
            else []
        )
        required_tests = AgentWorkflow._explicit_policy_tests(output, policy)
        description = output.get("description")
        understanding = (
            description
            if isinstance(description, str) and description
            else output.get("title") or requirement
        )
        try:
            return ExecutionPlan.from_dict(
                {
                    "requirement_understanding": understanding,
                    "non_goals": [],
                    "files_and_symbols": files,
                    "steps": steps,
                    "constraints": [],
                    "acceptance_criteria": acceptance,
                    "required_tests": required_tests,
                    "risks": risks,
                    "open_questions": open_questions,
                }
            )
        except ValueError as native_error:
            raise ValueError(
                f"Workloop ExecutionPlan 无效：{canonical_error}；"
                f"Claude 原生计划映射无效：{native_error}"
            ) from native_error

    def _save_generated_plan_graph(
        self,
        task: AgentTask,
        plan: ExecutionPlan,
        planning_binding: ModelBinding | None = None,
    ) -> None:
        graph = self.composer.compose(plan) if self.composer is not None else PlanGraph.from_execution_plan(plan)
        graph = PlanGraph(
            requirement_summary=graph.requirement_summary,
            nodes=graph.nodes,
            planning_model=planning_binding or graph.planning_model,
            review_model=graph.review_model,
            graph_id=graph.graph_id,
            version=task.plan_version,
            status=graph.status,
            created_at=graph.created_at,
            approved_at=graph.approved_at,
        )
        graph_ref = f"artifacts/plan-graphs/{graph.version}.json"
        plan_ref = task.artifacts.get("plan", "") or f"artifacts/plans/{task.plan_version}.json"
        self.store.write_json(self.store.task_dir(task.task_id) / graph_ref, graph.to_dict())
        task.plan_graph = graph.to_dict()
        task.artifacts["plan_graph"] = graph_ref
        pack = ContextPack(
            task_id=task.task_id,
            node_id="planning",
            summary=plan.requirement_understanding,
            facts=list(plan.acceptance_criteria),
            constraints=list(plan.constraints),
            inputs=[task.requirement],
            artifacts=[plan_ref, graph_ref],
            open_questions=list(plan.open_questions),
            source_sessions=[task.sessions.get("node:planning", task.sessions.get("planner", ""))],
        )
        task.artifacts["context_plan"] = self.context_ledger.write(pack)
        self.store.save(task)

    def _planning_binding(self, task: AgentTask) -> ModelBinding:
        workflow = self._task_workflow(task)
        planner_node = next(
            (node for node in workflow.nodes if node.kind is WorkflowNodeKind.PLANNER),
            None,
        )
        selected = self._binding_for_workflow_node(
            planner_node, "planning", AgentAccess.READ_ONLY, task.requirement
        )
        if selected.profile_id:
            return selected
        if task.plan_graph:
            try:
                binding = PlanGraph.from_dict(task.plan_graph).planning_model
            except (TypeError, ValueError):
                binding = ModelBinding()
            if binding.profile_id:
                return binding
        if self.composer is not None:
            return self.composer.select_binding(
                "planning", AgentAccess.READ_ONLY, task.requirement
            )
        return ModelBinding()

    def _binding_for_workflow_node(
        self,
        workflow_node: WorkflowNode | None,
        capability: str,
        access: AgentAccess,
        text: str,
    ) -> ModelBinding:
        profile_id = str(getattr(workflow_node, "model_profile_id", "") or "").strip()
        if not profile_id or self.composer is None:
            return ModelBinding()
        return self.composer.binding_for_profile(profile_id, capability, access, text)

    def _planning_request_fields(
        self,
        task: AgentTask,
        binding: ModelBinding | None = None,
    ) -> dict[str, str]:
        binding = binding or self._planning_binding(task)
        if binding.profile_id:
            key = "node:planning"
            return {
                "model_profile_id": binding.profile_id,
                "session_key": key,
                "session_id": task.sessions.get(key, task.sessions.get("planner", "")),
                "node_id": "planning",
                "provider": binding.provider,
                "model": binding.model,
                "thinking": binding.thinking,
                "context_ref": task.artifacts.get("context_plan", ""),
            }
        key = "node:planning"
        return {
            "model_profile_id": "",
            "session_key": key,
            "session_id": task.sessions.get(key, task.sessions.get("planner", "")),
            "node_id": "",
            "provider": "",
            "model": "",
            "thinking": "",
            "context_ref": task.artifacts.get("context_plan", ""),
        }

    @staticmethod
    def _repair_request(request: AgentRequest, correction: str) -> AgentRequest:
        """Return a fresh request asking the model to re-emit conforming JSON.

        ``session_id`` is cleared so the repair is a self-contained turn that
        carries the prior bad output and the parse error in its instructions;
        this works for any runtime, including ones that cannot resume sessions.
        Node routing fields (node_id/provider/model/thinking/context_ref) are
        preserved so a per-node runtime still routes the repair correctly."""
        return replace(
            request,
            instructions=request.instructions + correction,
            session_id="",
        )


    def _repair_node_output(
        self,
        task: AgentTask,
        request: AgentRequest,
        error: str,
        bad_output: dict,
    ) -> tuple[ExecutionResult, AgentResult] | None:
        """One bounded repair attempt for an unparseable executor output.

        Returns ``(parsed_result, repair_agent_result)`` on success, or ``None``
        when repair is disabled, the repair invoke failed, or the repaired
        output still fails to parse (the caller then falls through to the
        existing failure handling)."""
        if _MAX_OUTPUT_REPAIR_ATTEMPTS <= 0:
            return None
        repair_result = self._invoke_agent(
            task,
            self._repair_request(
                request, prompts.executor_repair_prompt(error, bad_output)
            ),
        )
        if not repair_result.succeeded:
            return None
        try:
            return ExecutionResult.from_dict(repair_result.output), repair_result
        except ValueError:
            return None

    def _repair_review_output(
        self,
        task: AgentTask,
        request: AgentRequest,
        error: str,
        bad_output: dict,
    ) -> tuple[ReviewResult, AgentResult] | None:
        """One bounded repair attempt for an unparseable reviewer output."""
        if _MAX_OUTPUT_REPAIR_ATTEMPTS <= 0:
            return None
        repair_result = self._invoke_agent(
            task,
            self._repair_request(
                request, prompts.reviewer_repair_prompt(error, bad_output)
            ),
        )
        if not repair_result.succeeded:
            return None
        try:
            return ReviewResult.from_dict(repair_result.output), repair_result
        except ValueError:
            return None

    @staticmethod
    def _inconsistent_pass_review(result: ReviewResult, error: ValueError) -> ReviewResult:
        """Turn a rejected pass verdict into a revision request.

        The host still refuses to accept the pass (the delivery gate re-checks
        validate_pass against the approved plan), but the task itself degrades
        to a revise_code round instead of dying on reviewer wording drift.
        """
        return ReviewResult(
            verdict=ReviewVerdict.REVISE_CODE,
            acceptance=list(result.acceptance),
            issues=[
                ReviewIssue(
                    file="",
                    line=0,
                    severity=Severity.BLOCKER,
                    message=f"宿主拒绝该 pass 结论：{error}",
                    suggestion="逐字引用批准计划中的验收标准，并如实标记每项是否通过。",
                    evidence="宿主 validate_pass 校验",
                )
            ],
            recommended_tests=list(result.recommended_tests),
            summary=f"审核结论与批准计划不一致，已降级为返修：{error}",
        )

    def _load_plan(self, task: AgentTask) -> ExecutionPlan:
        path = self.store.task_dir(task.task_id) / task.artifacts["plan"]
        return ExecutionPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _load_round_review(
        self,
        task: AgentTask,
        iteration: int | None = None,
    ) -> ReviewResult:
        path = (
            self.store.task_dir(task.task_id)
            / "artifacts"
            / "rounds"
            / str(task.iteration if iteration is None else iteration)
            / "review.json"
        )
        if not path.is_file():
            raise FileNotFoundError(f"任务 {task.task_id} 缺少可恢复审核工件：{path}")
        return ReviewResult.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _load_pending_revision(self, task: AgentTask) -> tuple[ReviewResult, bool]:
        feedback_iteration = task.revision_feedback_iteration
        if feedback_iteration > 0:
            feedback = self._load_round_review(task, feedback_iteration)
            return feedback, task.iteration == feedback_iteration

        # Compatibility for tasks persisted before revision_feedback_iteration
        # was introduced. A review in the current round means its revision
        # round has not opened yet; otherwise the preceding round owns it.
        try:
            return self._load_round_review(task), True
        except FileNotFoundError as current_error:
            feedback = self._load_previous_revision_feedback(task)
            if feedback is None:
                raise current_error
            return feedback, False

    def _load_round_validation(self, task: AgentTask) -> ValidationResult:
        path = self._round_dir(task) / "validation.json"
        if not path.is_file():
            raise FileNotFoundError(f"任务 {task.task_id} 缺少可恢复验证工件：{path}")
        return ValidationResult.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _load_previous_revision_feedback(self, task: AgentTask) -> ReviewResult | None:
        if task.iteration <= 1:
            return None
        path = (
            self.store.task_dir(task.task_id)
            / "artifacts"
            / "rounds"
            / str(task.iteration - 1)
            / "review.json"
        )
        if not path.is_file():
            return None
        review = ReviewResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return review if review.verdict is ReviewVerdict.REVISE_CODE else None

    def _load_workspace_base(self, task: AgentTask) -> dict[str, str]:
        reference = task.artifacts.get("workspace_base", "")
        if not reference:
            raise FileNotFoundError(f"任务 {task.task_id} 缺少 workspace base 工件。")
        data = json.loads(
            (self.store.task_dir(task.task_id) / reference).read_text(encoding="utf-8")
        )
        if not isinstance(data, dict):
            raise ValueError(f"任务 {task.task_id} workspace base 工件必须是对象。")
        return {str(key): str(value) for key, value in data.items()}

    def _round_dir(self, task: AgentTask) -> Path:
        if task.iteration <= 0:
            raise ValueError(f"任务 {task.task_id} 尚未开始执行轮次。")
        return (
            self.store.task_dir(task.task_id)
            / "artifacts"
            / "rounds"
            / str(task.iteration)
        )

    def _require_status(self, task: AgentTask, expected: AgentTaskStatus) -> None:
        if task.status is not expected:
            raise ValueError(f"任务 {task.task_id} 状态为 {task.status.value}，要求 {expected.value}。")

    def _require_status_in(
        self,
        task: AgentTask,
        expected: set[AgentTaskStatus],
    ) -> None:
        if task.status not in expected:
            names = ", ".join(sorted(status.value for status in expected))
            raise ValueError(
                f"任务 {task.task_id} 状态为 {task.status.value}，要求以下之一：{names}。"
            )




    def _load_project_policy(self, task: AgentTask) -> ProjectPolicy:
        project = self.projects.get(task.project_id)
        return self.policy_loader.load(self.workspace_path(task.task_id), project.config_path)

    def _agent_policy(self, policy: ProjectPolicy, command_names: list[str]) -> AgentPolicy:
        commands = policy.required_commands(command_names)
        return AgentPolicy(
            allowed_commands=[list(command.argv) for command in commands],
            protected_paths=list(policy.protected_paths),
            timeout_seconds=policy.timeout_seconds,
            # The versioned project policy is the human authorization: a repo
            # that opts into network=allow explicitly grants its executor and
            # validation commands network access.
            network_allowed=policy.network == "allow",
            redact_patterns=list(policy.redact_patterns),
        )

    def _check_workspace_policy(
        self,
        workspace: Workspace,
        base: dict[str, str],
        policy: ProjectPolicy,
    ) -> PolicyCheck:
        boundary = PolicyBoundary(deny_paths=list(policy.protected_paths))
        return workspace.validate(workspace.changes_since(base), boundary, self.policy_checker)

    def _block_policy(self, task: AgentTask, check: PolicyCheck) -> AgentTask:
        task.error = "；".join(check.issues) or "工作区变更被项目策略阻止。"
        if not self._transition_unless_cancelled(task, AgentTaskStatus.BLOCKED):
            return self._finish_or_return_cancellation(task)
        return task

    def _pause(
        self,
        task: AgentTask,
        reason: str,
        error: str = "",
        resume_phase: AgentTaskStatus | None = None,
    ) -> AgentTask:
        task.interrupted_status = (resume_phase or task.status).value
        task.pause_reason = reason
        task.error = error or "任务预算已耗尽，已暂停。"
        if not self._transition_unless_cancelled(task, AgentTaskStatus.PAUSED, reason=reason):
            return self._finish_or_return_cancellation(task)
        return task

    def _prepared_from_task(self, task: AgentTask, project: Project) -> PreparedWorktree:
        expected_workspace = self.store.workspace_location(task.task_id).resolve()
        actual_workspace = Path(task.workspace).resolve()
        expected_branch = f"workloop/{task.task_id.lower()}"
        if actual_workspace != expected_workspace or task.task_branch != expected_branch:
            raise ValueError("任务身份与 workspace 或任务分支不匹配。")
        if task.target_branch != project.default_branch:
            raise ValueError("任务目标分支与注册项目不匹配。")
        return PreparedWorktree(
            task_id=task.task_id,
            path=actual_workspace,
            base_commit=task.delivery_base_commit or task.base_commit,
            target_branch=task.target_branch,
            task_branch=task.task_branch,
        )

    def _invoke_agent(self, task: AgentTask, request: AgentRequest) -> AgentResult:
        started = time.monotonic()
        task.run_count += 1
        run_ref = f"artifacts/runs/{task.run_count}-{request.role}.json"
        started_at = utc_now()
        try:
            identity = self.runtime.describe(request)
        except Exception as error:  # noqa: BLE001 - identity failures are persisted below
            identity = {
                "runtime": type(self.runtime).__name__,
                "runtime_version": "",
                "model": "",
                "config": {},
            }
            identity_error = f"无法读取 AgentRuntime 身份：{error}"
        else:
            identity_error = ""
        record = {
            "schema_version": 1,
            "index": task.run_count,
            "role": request.role,
            "workflow_node_id": request.workflow_node_id,
            "status": "running",
            "access": request.access.value,
            "policy": to_plain(request.policy),
            "budget": to_plain(request.budget),
            "task_budget": to_plain(task.budget),
            "runtime": identity.get("runtime", type(self.runtime).__name__),
            "runtime_version": identity.get("runtime_version", ""),
            "model": identity.get("model", ""),
            "runtime_config": identity.get("config", {}),
            "session_id": request.session_id,
            "session_key": request.session_key or request.role,
            "model_profile_id": request.model_profile_id,
            "instructions": request.instructions,
            "started_at": started_at,
            "finished_at": "",
            "output": {},
            "final_message": "",
            "events": [],
            "raw_events": [],
            "usage": {},
            "error_type": "",
            "error": "",
        }
        run_path = self.store.task_dir(task.task_id) / run_ref
        self.store.write_json(run_path, redact_value(to_plain(record), request.policy.redact_patterns))
        with self._task_state_lock:
            latest = self.store.load(task.task_id)
            if latest.status in {AgentTaskStatus.CANCELLING, AgentTaskStatus.CANCELLED}:
                self._adopt_cancellation(task, latest)
            else:
                task.artifacts["last_agent_run"] = run_ref
                self.store.save(task)

        budget_error = task_budget_error(task)
        if task.status in {AgentTaskStatus.CANCELLING, AgentTaskStatus.CANCELLED}:
            response = AgentResult(
                succeeded=False,
                error="代理运行已由用户取消。",
                error_type="user_cancelled",
            )
        elif budget_error:
            response = AgentResult(
                succeeded=False,
                error="任务预算已耗尽。",
                error_type=budget_error,
            )
        elif identity_error:
            response = AgentResult(
                succeeded=False,
                error=identity_error,
                error_type="environment_missing",
            )
        else:
            try:
                response = self.runtime.invoke(request)
            except Exception as error:  # noqa: BLE001 - runtime failures become persistent task results
                response = AgentResult(succeeded=False, error=f"AgentRuntime 异常：{error}")

        response = self._validate_role_session(task, request, response)

        task.budget.consumed_active_seconds += time.monotonic() - started
        input_tokens, output_tokens, cached_tokens, uncached_input_tokens = (
            usage_tokens(response.usage)
        )
        task.budget.consumed_input_tokens += input_tokens
        task.budget.consumed_output_tokens += output_tokens
        task.budget.consumed_cached_input_tokens += cached_tokens
        cost = response.usage.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            task.budget.consumed_cost_usd += float(cost)
        elif request.model_profile_id and self.composer is not None:
            option = self.composer.catalog.get(request.model_profile_id)
            cached_rate = (
                option.cached_input_cost_per_million
                if option.cached_input_cost_per_million is not None
                else option.input_cost_per_million
            )
            task.budget.consumed_cost_usd += (
                uncached_input_tokens * option.input_cost_per_million
                + cached_tokens * cached_rate
                + output_tokens * option.output_cost_per_million
            ) / 1_000_000
        budget_error = task_budget_overrun(task)
        if response.succeeded and budget_error:
            response = self._reject_role_session(
                response,
                request.role,
                "任务预算已耗尽。",
                budget_error,
            )

        response.output = redact_value(response.output, request.policy.redact_patterns)
        response.error = redact(response.error, request.policy.redact_patterns)
        response.final_message = redact(response.final_message, request.policy.redact_patterns)

        with self._task_state_lock:
            latest = self.store.load(task.task_id)
            if latest.status in {AgentTaskStatus.CANCELLING, AgentTaskStatus.CANCELLED}:
                self._adopt_cancellation(task, latest)
                response.succeeded = False
                response.error = response.error or "代理运行已由用户取消。"
                response.error_type = "user_cancelled"

            record.update({
                "status": (
                    "succeeded"
                    if response.succeeded
                    else "cancelled"
                    if response.error_type == "user_cancelled"
                    else "failed"
                ),
                "runtime": response.runtime or record["runtime"],
                "runtime_version": response.runtime_version or record["runtime_version"],
                "model": response.model or record["model"],
                "runtime_config": response.runtime_config or record["runtime_config"],
                "task_budget": to_plain(task.budget),
                "session_id": response.session_id or request.session_id,
                "finished_at": utc_now(),
                "output": response.output,
                "final_message": response.final_message,
                "events": _bound_run_events(response.events),
                "raw_events": _bound_run_events(response.raw_events),
                "usage": response.usage,
                "error_type": response.error_type,
                "error": response.error,
            })
            self.store.write_json(
                run_path,
                redact_value(to_plain(record), request.policy.redact_patterns),
            )
            if response.session_id:
                session_key = request.session_key or request.role
                task.sessions[session_key] = response.session_id
                # Compatibility aliases remain observable but are never read by
                # composed requests, which resume only their own node key.
                task.sessions[request.role] = response.session_id
            if task.status is not AgentTaskStatus.CANCELLED:
                self.store.save(task)
        return response

    @staticmethod
    def _validate_role_session(
        task: AgentTask,
        request: AgentRequest,
        response: AgentResult,
    ) -> AgentResult:
        if not response.succeeded or request.role not in {"planner", "reviewer"}:
            return response
        if not response.session_id:
            return AgentWorkflow._reject_role_session(
                response,
                request.role,
                f"{request.role} 运行缺少可持久化 session。",
                "structured_output_failed",
            )
        if request.session_id and response.session_id != request.session_id:
            return AgentWorkflow._reject_role_session(
                response,
                request.role,
                (
                    f"{request.role} 恢复后的 session 不一致：{request.session_id} != "
                    f"{response.session_id}。"
                ),
                "structured_output_failed",
            )
        other_role = "reviewer" if request.role == "planner" else "planner"
        if response.session_id == task.sessions.get(other_role):
            return AgentWorkflow._reject_role_session(
                response,
                request.role,
                f"{request.role} session 与 {other_role} session 必须相互隔离。",
                "policy_blocked",
            )
        return response


    @staticmethod
    def _reject_role_session(
        response: AgentResult,
        role: str,
        error: str,
        error_type: str,
    ) -> AgentResult:
        terminal_types = {
            AgentEventType.COMPLETED,
            AgentEventType.FAILED,
            AgentEventType.CANCELLED,
        }
        response.succeeded = False
        response.output = {}
        response.error = error
        response.error_type = error_type
        response.events = [
            event for event in response.events if event.event_type not in terminal_types
        ]
        response.events.append(
            AgentEvent(AgentEventType.FAILED, role, {"reason": error_type})
        )
        return response

    def _fail(self, task: AgentTask, response: AgentResult) -> AgentTask:
        with self._task_state_lock:
            latest = self.store.load(task.task_id)
            if latest.status is AgentTaskStatus.CANCELLED:
                return latest
            if latest.status is AgentTaskStatus.CANCELLING or response.error_type == "user_cancelled":
                if latest.status is not AgentTaskStatus.CANCELLING:
                    latest.transition(AgentTaskStatus.CANCELLING, reason="runtime_cancelled")
                    self.store.save(latest)
                cancelled = latest
            elif response.error_type in {
                "budget_exhausted",
                "token_budget_exhausted",
                "input_token_budget_exhausted",
                "output_token_budget_exhausted",
                "call_timeout",
                "idle_timeout",
                "permission_required",
                "total_timeout",
            }:
                task.interrupted_status = task.status.value
                task.pause_reason = response.error_type
                task.error = response.error or "任务预算已耗尽，已暂停。"
                task.transition(AgentTaskStatus.PAUSED, reason=response.error_type)
                self.store.save(task)
                return task
            else:
                task.error = response.error or "代理运行失败。"
                task.transition(AgentTaskStatus.FAILED)
                self.store.save(task)
                return task
        return self._finish_cancellation(cancelled)

    def _finish_cancellation(self, task: AgentTask) -> AgentTask:
        project = self.projects.get(task.project_id)
        prepared = self._prepared_from_task(task, project)
        self.git_worktrees.remove(project, prepared)
        with self._task_state_lock:
            latest = self.store.load(task.task_id)
            if latest.status is AgentTaskStatus.CANCELLED:
                return latest
            latest.error = ""
            latest.transition(AgentTaskStatus.CANCELLED, reason="active_run_stopped")
            self.store.save(latest)
            self._adopt_cancellation(task, latest)
            return task

    def _save_unless_cancelled(self, task: AgentTask) -> bool:
        with self._task_state_lock:
            latest = self.store.load(task.task_id)
            if latest.status in {AgentTaskStatus.CANCELLING, AgentTaskStatus.CANCELLED}:
                self._adopt_cancellation(task, latest)
                return False
            self.store.save(task)
            return True

    def _transition_unless_cancelled(
        self,
        task: AgentTask,
        status: AgentTaskStatus,
        reason: str = "",
    ) -> bool:
        with self._task_state_lock:
            latest = self.store.load(task.task_id)
            if latest.status in {AgentTaskStatus.CANCELLING, AgentTaskStatus.CANCELLED}:
                self._adopt_cancellation(task, latest)
                return False
            if latest.status is not task.status:
                raise ValueError(
                    f"任务 {task.task_id} 持久状态 {latest.status.value} 与内存状态 "
                    f"{task.status.value} 不一致。"
                )
            task.transition(status, reason=reason)
            self.store.save(task)
            return True

    def _finish_or_return_cancellation(self, task: AgentTask) -> AgentTask:
        if task.status is AgentTaskStatus.CANCELLED:
            return task
        return self._finish_cancellation(task)

    @staticmethod
    def _adopt_cancellation(task: AgentTask, latest: AgentTask) -> None:
        task.status = latest.status
        task.transitions = latest.transitions
        task.updated_at = latest.updated_at
        task.error = latest.error


    def _relevant_experiences(self, task: AgentTask) -> list[dict[str, str]]:
        if self.experience_store is None:
            return []

        def terms(text: str) -> set[str]:
            lowered = text.lower()
            result = set(re.findall(r"[a-z0-9_]{2,}", lowered))
            for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
                result.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
            return result

        query = terms(f"{task.title} {task.requirement}")
        ranked: list[tuple[int, str, str, str]] = []
        for record in self.experience_store.approved():
            overlap = len(query & terms(record.text))
            if overlap <= 0:
                continue
            ranked.append((overlap, record.updated_at, record.kind, record.text))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected: list[dict[str, str]] = []
        total_chars = 0
        for _, _, kind, text in ranked[:5]:
            bounded = text[:600]
            if total_chars + len(bounded) > 2400:
                bounded = bounded[: max(0, 2400 - total_chars)]
            if not bounded:
                break
            selected.append({"kind": kind, "text": bounded})
            total_chars += len(bounded)
        return selected



    def _additional_instructions(
        self,
        task: AgentTask,
        workflow_node: WorkflowNode | None,
        kind: WorkflowNodeKind,
    ) -> str:
        parts: list[str] = []
        try:
            project = self.projects.get(task.project_id)
            if project.instructions.strip():
                parts.append("项目级指令（适用于该项目的所有任务）：\n" + project.instructions.strip())
        except (FileNotFoundError, ValueError):
            pass
        if workflow_node is not None and workflow_node.instructions.strip():
            parts.append(workflow_node.instructions.strip())
        elif workflow_node is None:
            default = self._task_workflow(task).instructions_for(kind)
            if default.strip():
                parts.append(default.strip())
        return "\n\n".join(parts)

    def _planner_instructions(
        self,
        task: AgentTask,
        policy: ProjectPolicy | None = None,
        workflow_node: WorkflowNode | None = None,
    ) -> str:
        return prompts.planner_instructions(
            task,
            experiences=self._relevant_experiences(task),
            validation_command_names=(
                [command.name for command in policy.validation_commands]
                if policy is not None
                else None
            ),
            additional_instructions=self._additional_instructions(
                task, workflow_node, WorkflowNodeKind.PLANNER
            ),
        )

    def _executor_instructions(
        self,
        task: AgentTask,
        plan: ExecutionPlan,
        review_feedback: ReviewResult | None,
        workflow_node: WorkflowNode | None = None,
    ) -> str:
        return prompts.executor_instructions(
            plan,
            review_feedback,
            additional_instructions=self._additional_instructions(
                task, workflow_node, WorkflowNodeKind.EXECUTOR
            ),
        )

    def _reviewer_instructions(
        self,
        task: AgentTask,
        plan: ExecutionPlan,
        diff: str,
        validation: ValidationResult | None,
        workflow_node: WorkflowNode | None = None,
    ) -> str:
        return prompts.reviewer_instructions(
            task,
            plan,
            diff,
            validation,
            diff_artifact=str(
                (
                    self.store.task_dir(task.task_id)
                    / f"artifacts/rounds/{task.iteration}/changes.diff"
                ).resolve()
            ),
            additional_instructions=self._additional_instructions(
                task, workflow_node, WorkflowNodeKind.REVIEWER
            ),
        )

    def _replanner_instructions(
        self,
        task: AgentTask,
        previous_plan: ExecutionPlan,
        review: ReviewResult,
    ) -> str:
        return prompts.replanner_instructions(
            task,
            previous_plan,
            review,
            additional_instructions=self._task_workflow(task).instructions_for(
                WorkflowNodeKind.PLANNER
            ),
        )

    @staticmethod
    def _task_workflow(task: AgentTask) -> WorkflowDefinition:
        if task.workflow:
            return workflow_from_dict(
                task.workflow,
                builtin=bool(task.workflow.get("builtin", False)),
            )
        # Tasks created before workflow snapshots were introduced retain guarded behavior.
        return BUILTIN_WORKFLOWS["guarded"]
