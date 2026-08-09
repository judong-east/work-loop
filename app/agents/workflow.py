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
    ReviewResult,
    ReviewVerdict,
    TaskBudget,
    ValidationResult,
)
from app.agents.context_ledger import (
    MAX_CONTEXT_PROMPT_CHARS,
    ContextLedger,
    ContextPack,
)
from app.agents.composition import ExecutionComposer
from app.agents.plan_graph import (
    ModelBinding,
    PlanGraph,
    PlanNode,
    PlanNodeAccess,
    PlanNodeKind,
)
from app.agents.runtime import AgentRuntime
from app.agents.store import AgentTaskStore
from app.agents.workflow_config import (
    BUILTIN_WORKFLOWS,
    WorkflowCatalog,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowNodeKind,
    workflow_from_dict,
)
from app.core.contracts import FileChange, PolicyBoundary, PolicyCheck, to_plain, utc_now
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


# PiRpcRuntime gets structured output by parsing the model's final text
# message as JSON (see app/agents/pi_rpc.py), so the executor prompt must
# explicitly request an ExecutionResult JSON object. ClaudeCodeRuntime
# enforces the same shape via tool-use input_schema, so this instruction is
# redundant there and harmless. Without it, a text-JSON runtime's executor
# emits a natural-language summary and ExecutionResult.from_dict rejects it.
_EXECUTOR_OUTPUT_INSTRUCTION = (
    "\n\n# 输出要求\n"
    "完成上述工作区修改后，只输出一个符合 ExecutionResult Schema 的完整 JSON "
    "对象，不要 Markdown、代码围栏或解释文字。对象至少包含字段："
    "completed_steps（字符串数组）、modified_files（字符串数组）、"
    "tests（数组）、deviations（字符串数组）、remaining_risks（字符串数组）、"
    "next_steps（字符串数组）。\n"
    "tests 仅记录你实际通过 bash 运行过的测试/验证命令的结果；没有运行过命令时必须"
    "为空数组 []，切勿填入描述性条目。每个 tests 元素必须是 "
    "{\"command\": \"<运行的命令>\", \"exit_code\": <整数退出码>, "
    "\"stdout\": \"<标准输出>\", \"stderr\": \"<标准错误>\"}，其中 exit_code 必须是"
    "整数（例如 0 表示成功）。"
)

# ReviewResult has no lenient fallback (unlike the planner), so a text-JSON
# runtime such as PiRpcRuntime must be told the exact field names or the model
# invents its own and ReviewResult.from_dict rejects it. ClaudeCodeRuntime
# enforces this shape via tool input_schema, so the instruction is redundant
# there and harmless.
_REVIEWER_OUTPUT_INSTRUCTION = (
    "\n\n# 输出要求\n"
    "只输出一个符合 ReviewResult Schema 的完整 JSON 对象，不要 Markdown、代码"
    "围栏或解释文字。对象必须且仅包含这些顶层字段：\n"
    "- verdict：字符串，取值之一 \"pass\"、\"revise_code\"、\"replan\"、\"blocked\"；"
    "全部验收通过且无阻断问题时用 \"pass\"。\n"
    "- acceptance：数组，每个元素是 {\"criterion\": <验收标准字符串>, \"passed\": "
    "true/false}；criterion 必须与计划中的 acceptance_criteria 完全一致（逐字"
    "相同），每个验收标准都要出现且仅出现一次。\n"
    "- issues：数组，无问题时为 []；每项为 {\"file\": \"\", \"line\": 0, "
    "\"severity\": \"info|warning|blocker\", \"message\": \"...\", \"evidence\": "
    "\"...\", \"suggestion\": \"\"}。\n"
    "- recommended_tests：字符串数组，可为 []。\n"
    "- summary：字符串，简要总结。"
)

# ExecutionPlan.from_dict is strict on requirement_understanding/steps/
# acceptance_criteria/required_tests, and the lenient fallback only recognizes
# a few step-key aliases, so a text-JSON runtime such as PiRpcRuntime must be
# told the exact field names or the model invents its own shape and planning
# fails nondeterministically. ClaudeCodeRuntime enforces the schema via tool
# input_schema, so this instruction is redundant there and harmless.
_PLANNER_OUTPUT_INSTRUCTION = (
    "\n\n# 输出要求\n"
    "只输出一个符合 ExecutionPlan Schema 的完整 JSON 对象，不要 Markdown、代码"
    "围栏或解释文字。对象必须且仅包含这些顶层字段：\n"
    "- requirement_understanding：字符串，对需求的理解（非空）。\n"
    "- non_goals：字符串数组，可为 []。\n"
    "- files_and_symbols：字符串数组，涉及的关键文件/符号，可为 []。\n"
    "- steps：字符串数组，每个元素是一条可执行的实现步骤描述（纯字符串，不要写成"
    "对象），至少一条。\n"
    "- constraints：字符串数组，可为 []。\n"
    "- acceptance_criteria：字符串数组，验收标准（非空、不重复），应与需求中的验收"
    "项逐字对应。\n"
    "- required_tests：字符串数组，需要运行的项目验证命令名（非空），每一项都必须"
    "逐字取自需求中列出的项目验证命令名，不要自造命令名或写成 shell 命令行。\n"
    "- risks：字符串数组，可为 []。\n"
    "- open_questions：字符串数组，未决问题，可为 []。\n"
    "验证由系统在执行完成后自动运行，不要把运行验证命令列为 steps 中的实现步骤；"
    "steps 只应包含实际的编码或文件修改动作。"
)


class TaskValidator(Protocol):
    def validate(
        self,
        task_id: str,
        workspace: Path,
        plan: ExecutionPlan,
        policy: ProjectPolicy,
    ) -> ValidationResult: ...


class AgentWorkflow:
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
                )
            )

        project = Project(
            name=name.strip(),
            repository="",
            default_branch=default_branch.strip() or "main",
            config_path=config_path,
            workspace_mode="directory",
            source_directory=str(requested),
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
        workflow_id: str = "guarded",
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

    def record_clarification(self, task_id: str, answer: str) -> AgentTask:
        task = self.store.load(task_id)
        self._require_status(task, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
        plan = self._load_plan(task)
        if not plan.open_questions:
            raise ValueError("当前计划没有待回答的澄清问题。")
        cleaned = answer.strip()
        if not cleaned:
            raise ValueError("澄清答复不能为空。")
        task.clarifications.append(
            {
                "question": plan.open_questions[0],
                "answer": cleaned,
                "at": utc_now(),
            }
        )
        self.store.save(task)
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
                        budget=self._agent_budget(task),
                        workflow_node_id=node.node_id,
                        **self._node_request_fields(
                            task, PlanNodeKind.IMPLEMENTATION, "executor"
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
                budget_error = self._task_budget_error(task)
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
                budget_error = self._task_budget_overrun(task)
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
                budget=self._agent_budget(task),
                artifact_root=self.store.task_dir(task.task_id),
                workflow_node_id=node.node_id,
                **self._node_request_fields(task, PlanNodeKind.REVIEW, "reviewer"),
            )
            review = self._invoke_agent(task, review_request)
            if not review.succeeded:
                return self._fail(task, review)
            # A parse error is a shape problem a model can self-correct on a
            # second turn; a validate_pass error is a semantic rejection
            # (missing/failed acceptance) that repair cannot fix. Only the
            # former triggers a bounded repair; the latter fails as before.
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
                return self._fail(
                    task,
                    AgentResult(succeeded=False, error=f"审核结果无效：{error}"),
                )
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
                budget=self._agent_budget(task),
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
    def _node_request_fields(
        task: AgentTask,
        kind: PlanNodeKind,
        role: str,
    ) -> dict[str, str]:
        def fallback() -> dict[str, str]:
            phase_key = "node:review" if role == "reviewer" else role
            return {
                "model_profile_id": "",
                "session_key": phase_key,
                "session_id": task.sessions.get(phase_key, task.sessions.get(role, "")),
                "node_id": "review" if role == "reviewer" else "",
                "provider": "",
                "model": "",
                "thinking": "",
                "context_ref": task.artifacts.get("context_plan", ""),
            }

        if not task.plan_graph:
            return fallback()
        try:
            graph = PlanGraph.from_dict(task.plan_graph)
        except (TypeError, ValueError):
            return fallback()
        if kind is PlanNodeKind.REVIEW and graph.review_model.profile_id:
            binding = graph.review_model
            session_key = "node:review"
            return {
                "model_profile_id": binding.profile_id,
                "session_key": session_key,
                "session_id": task.sessions.get(
                    session_key, task.sessions.get("reviewer", "")
                ),
                "node_id": "review",
                "provider": binding.provider,
                "model": binding.model,
                "thinking": binding.thinking,
                "context_ref": task.artifacts.get("context_plan", ""),
            }
        candidates = [node for node in graph.nodes if node.kind is kind and node.enabled]
        if not candidates:
            return fallback()
        node = next((item for item in candidates if item.model.model or item.model.provider), candidates[0])
        session_key = f"node:{node.node_id}"
        return {
            "model_profile_id": node.model.profile_id,
            "session_key": session_key,
            "session_id": task.sessions.get(session_key, task.sessions.get(role, "")),
            "node_id": node.node_id,
            "provider": node.model.provider,
            "model": node.model.model,
            "thinking": node.model.thinking,
            "context_ref": task.artifacts.get("context_plan", ""),
        }

    # ------------------------------------------------------------------
    # Graph-driven execution (one executor call per implementation node)
    # ------------------------------------------------------------------

    @staticmethod
    def _node_fields(node: PlanNode, context_ref: str) -> dict[str, str]:
        return {
            "model_profile_id": node.model.profile_id,
            "session_key": f"node:{node.node_id}",
            "node_id": node.node_id,
            "provider": node.model.provider,
            "model": node.model.model,
            "thinking": node.model.thinking,
            "context_ref": context_ref,
        }

    def _node_context(self, task: AgentTask, node: PlanNode) -> ContextPack | None:
        """Merge the ContextPacks produced by this node's dependencies (plus the
        approved-plan pack) into one durable handoff pack. Returns ``None`` when
        there is no upstream context to inject — the node then runs standalone.
        """
        packs: list[ContextPack] = []
        plan_summary = ""
        for dependency in node.depends_on:
            reference = str(task.node_runs.get(dependency, {}).get("context_ref", ""))
            if reference:
                pack = self.context_ledger.read_ref(task.task_id, reference)
                if pack is not None:
                    packs.append(pack)
        plan_reference = task.artifacts.get("context_plan", "")
        if plan_reference:
            plan_pack = self.context_ledger.read_ref(task.task_id, plan_reference)
            if plan_pack is not None:
                packs.append(plan_pack)
                plan_summary = plan_pack.summary
        if not packs:
            return None
        return self.context_ledger.merge(
            task_id=task.task_id,
            node_id=node.node_id,
            packs=packs,
            summary=plan_summary or node.title or node.node_id,
            inputs=list(node.inputs),
        )

    @staticmethod
    def _node_instructions(
        node: PlanNode,
        context: ContextPack | None,
        review_feedback: ReviewResult | None = None,
        workflow_instructions: str = "",
        artifact_root: Path | None = None,
    ) -> str:
        base = (node.instructions or node.title).strip() or node.title
        body = base
        if context is not None:
            lines: list[str] = []
            if context.summary:
                lines.append(f"任务目标：{context.summary[:1000]}")
            if context.inputs:
                lines.append("任务输入：")
                lines.extend(f"- {item[:1000]}" for item in context.inputs[-3:])
            if context.artifacts:
                lines.append("相关工件（按引用读取，不在上下文中复制内容）：")
                for artifact in context.artifacts[:20]:
                    path = Path(artifact)
                    if artifact_root is not None and not path.is_absolute():
                        path = artifact_root / path
                    lines.append(f"- {str(path.resolve())[:1000]}")
            if context.facts:
                lines.append("已完成的关键事实：")
                lines.extend(f"- {fact}" for fact in context.facts)
            if context.constraints:
                lines.append("约束：")
                lines.extend(f"- {constraint}" for constraint in context.constraints)
            if context.decisions:
                lines.append("已确定的决策：")
                lines.extend(f"- {decision}" for decision in context.decisions)
            if context.open_questions:
                lines.append("未决问题：")
                lines.extend(f"- {question[:700]}" for question in context.open_questions[:8])
            if lines:
                handoff = "\n".join(lines)[:MAX_CONTEXT_PROMPT_CHARS]
                body = base + "\n\n# 上游节点交接的压缩上下文\n" + handoff
        if review_feedback is not None:
            # A revision round re-runs this node. Without the reviewer's
            # findings it would only reproduce the result that was rejected,
            # so the feedback has to reach the node prompt the same way
            # _executor_instructions delivers it on the single-executor path.
            body += "\n\n# 上一轮审核要求返修\n" + json.dumps(
                to_plain(review_feedback), ensure_ascii=False
            )
        if workflow_instructions.strip():
            body += "\n\n# 工作流执行阶段附加要求\n" + workflow_instructions.strip()
        return body + _EXECUTOR_OUTPUT_INSTRUCTION

    def _execute_plan_graph(
        self,
        task: AgentTask,
        plan: ExecutionPlan,
        policy: ProjectPolicy,
        effective_agent_policy: AgentPolicy,
        workspace: Workspace,
        workspace_path: Path,
        base: dict[str, str],
        round_dir: Path,
        review_feedback: ReviewResult | None,
        workflow_node: WorkflowNode,
    ) -> AgentTask | None:
        """Run the implementation/integration nodes of the task PlanGraph in
        topological order, one executor call per node, with structured context
        handoff between nodes. Returns ``None`` when the graph finished cleanly
        (the combined ``ExecutionResult`` is written to ``round_dir``); returns
        the task on a terminal outcome (paused/failed/blocked)."""
        if task.plan_graph:
            try:
                graph = PlanGraph.from_dict(task.plan_graph)
            except (TypeError, ValueError) as error:
                return self._pause(
                    task,
                    "invalid_plan_graph",
                    f"任务执行图无效：{error}",
                    resume_phase=AgentTaskStatus.EXECUTING,
                )
        else:
            graph = PlanGraph.from_execution_plan(plan)
        if not graph.execution_nodes():
            self.store.write_json(round_dir / "execution.json", ExecutionResult())
            return None

        completed: set[str] = set()
        failed: set[str] = set()
        # Replay finished nodes (resume path): completed/skipped nodes are left
        # done; everything else (pending/running/failed) is re-run. Pre-complete
        # the planning node, which represents the already-approved plan.
        for node in graph.nodes:
            status = str(task.node_runs.get(node.node_id, {}).get("status", ""))
            if status in {"completed", "skipped"}:
                completed.add(node.node_id)
            if node.kind is PlanNodeKind.PLANNING and node.node_id not in completed:
                completed.add(node.node_id)
                task.node_runs.setdefault(
                    node.node_id,
                    {
                        "status": "completed",
                        "round": task.iteration,
                        "session_id": task.sessions.get("planner", ""),
                        "context_ref": task.artifacts.get("context_plan", ""),
                        "run_ref": "",
                        "result_ref": "",
                        "started_at": "",
                        "finished_at": "",
                        "error": "",
                    },
                )
        self.store.save(task)

        while True:
            ready = [
                node for node in graph.ready(completed, failed)
                if node in graph.execution_nodes()
            ]
            if not ready:
                break
            for node in ready:
                outcome = self._run_plan_node(
                    task, node, plan, policy, effective_agent_policy,
                    workspace, workspace_path, base, review_feedback, workflow_node,
                )
                if outcome is not None:
                    return outcome
                completed.add(node.node_id)
                self.store.save(task)

        unfinished = [
            node.node_id
            for node in graph.execution_nodes()
            if node.node_id not in completed
        ]
        if unfinished:
            return self._pause(
                task,
                "plan_graph_blocked",
                "执行图存在无法调度的启用节点：" + ", ".join(unfinished),
                resume_phase=AgentTaskStatus.EXECUTING,
            )

        # Assemble the combined result from every completed implementation node,
        # including nodes finished on a prior run that we just replayed (resume).
        per_node_results: list[ExecutionResult] = []
        for node in graph.execution_nodes():
            if node.node_id not in completed:
                continue
            result_ref = str(task.node_runs.get(node.node_id, {}).get("result_ref", ""))
            if not result_ref:
                continue
            path = self.store.task_dir(task.task_id) / result_ref
            if path.is_file():
                per_node_results.append(
                    ExecutionResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
                )
        self.store.write_json(round_dir / "execution.json", self._merge_node_results(per_node_results))
        return None

    def _reset_write_nodes_for_revision(
        self,
        task: AgentTask,
        plan: ExecutionPlan,
    ) -> None:
        """Clear write-node state and every transitive dependent before revision.

        ``_execute_plan_graph`` replays completed ``node_runs``. A revision must
        rerun write nodes and recompute their downstream analysis while retaining
        each node's own session, so no dependent consumes stale ContextPacks.
        Planning state remains unchanged; a rejected plan clears all node runs.

        No-op outside graph execution, where the single executor call already
        re-runs each round."""
        if not task.graph_execution or not task.node_runs:
            return
        try:
            graph = PlanGraph.from_dict(task.plan_graph)
        except (TypeError, ValueError):
            graph = PlanGraph.from_execution_plan(plan)
        invalidated = {node.node_id for node in graph.write_nodes()}
        changed = True
        while changed:
            changed = False
            for node in graph.execution_nodes():
                if node.node_id not in invalidated and any(
                    dependency in invalidated for dependency in node.depends_on
                ):
                    invalidated.add(node.node_id)
                    changed = True
        for node in graph.execution_nodes():
            if node.node_id not in invalidated:
                continue
            previous = task.node_runs.get(node.node_id, {})
            task.node_runs[node.node_id] = {
                "status": "pending",
                "round": task.iteration,
                "session_id": str(previous.get("session_id", "")),
                "context_ref": "",
                "run_ref": "",
                "result_ref": "",
                "started_at": "",
                "finished_at": "",
                "error": "",
            }
        self.store.save(task)

    def _run_plan_node(
        self,
        task: AgentTask,
        node: PlanNode,
        plan: ExecutionPlan,
        policy: ProjectPolicy,
        effective_agent_policy: AgentPolicy,
        workspace: Workspace,
        workspace_path: Path,
        base: dict[str, str],
        review_feedback: ReviewResult | None,
        workflow_node: WorkflowNode,
    ) -> AgentTask | None:
        """Execute one plan node via the executor runtime. Returns ``None`` on
        success or skip (node state persisted); returns the task on a terminal
        pause/failure that should stop the round.

        When ``task.node_worktree`` is set, the node runs in its own detached
        git worktree. Before each attempt the task worktree's accumulated writes
        are replicated into the node worktree (so the node sees its upstream),
        and on success the node's own delta is merged back into the shared task
        worktree, leaving it uncommitted so validation/review/delivery stay
        unchanged. The node worktree is always cleaned up in ``finally``."""
        state = task.node_runs.setdefault(
            node.node_id,
            {
                "status": "pending",
                "round": task.iteration,
                "session_id": "",
                "context_ref": "",
                "run_ref": "",
                "result_ref": "",
                "started_at": "",
                "finished_at": "",
                "error": "",
            },
        )
        if state.get("status") in {"completed", "skipped"}:
            return None

        budget_error = self._task_budget_error(task)
        if budget_error:
            return self._pause(
                task, budget_error, "节点执行前预算已耗尽。",
                resume_phase=AgentTaskStatus.EXECUTING,
            )

        context = self._node_context(task, node)
        context_ref = ""
        if context is not None:
            context_ref = f"artifacts/context/{node.node_id}/{context.version}.json"

        writes_workspace = node.access is PlanNodeAccess.WORKSPACE_WRITE
        use_node_worktree = task.graph_execution and task.node_worktree and writes_workspace
        node_worktree_path: Path | None = None
        node_workspace: Workspace | None = None
        node_before: dict[str, str] | None = None
        request_workspace = workspace_path
        if use_node_worktree:
            node_worktree_path = self._prepare_node_worktree(task, node)
            node_workspace = Workspace(node_worktree_path)
            request_workspace = node_worktree_path

        resumed_session = str(
            state.get("session_id", "")
            or task.sessions.get(f"node:{node.node_id}", "")
        )
        state["session_id"] = resumed_session
        request = AgentRequest(
            task_id=task.task_id,
            role="executor" if writes_workspace else "worker",
            instructions=self._node_instructions(
                node,
                context,
                review_feedback,
                workflow_node.instructions,
                self.store.task_dir(task.task_id),
            ),
            workspace=request_workspace,
            access=(
                AgentAccess.WORKSPACE_WRITE
                if writes_workspace
                else AgentAccess.READ_ONLY
            ),
            policy=effective_agent_policy,
            budget=self._agent_budget(task),
            artifact_root=self.store.task_dir(task.task_id),
            session_id=resumed_session,
            workflow_node_id=workflow_node.node_id,
            **self._node_fields(node, context_ref),
        )

        max_attempts = 2 if node.on_failure == "retry" else 1
        result: AgentResult | None = None
        node_result: ExecutionResult | None = None
        try:
            for _ in range(max_attempts):
                state["status"] = "running"
                state["started_at"] = utc_now()
                self.store.save(task)
                if use_node_worktree:
                    # Reset the node worktree to the task's current state so each
                    # attempt starts from a clean upstream baseline.
                    self._replicate_into_node_worktree(workspace, base, node_workspace)
                    node_before = node_workspace.snapshot()
                result = self._invoke_agent(task, request)
                if result.session_id:
                    state["session_id"] = result.session_id
                    request = replace(request, session_id=result.session_id)
                    self.store.save(task)
                if result.succeeded:
                    try:
                        node_result = ExecutionResult.from_dict(result.output)
                        break
                    except ValueError as error:
                        # The model produced output we cannot parse. Try one
                        # bounded self-repair (re-invoke with the parse error and
                        # a schema reminder) before declaring the attempt failed;
                        # a real text-JSON runtime often self-corrects on the
                        # second turn. On success, adopt the repaired result so
                        # the node's session/run refs point at the last invoke.
                        repaired = self._repair_node_output(
                            task, request, error, result.output
                        )
                        if repaired is not None:
                            node_result, result = repaired
                            break
                        result = AgentResult(
                            succeeded=False, error=f"节点 {node.node_id} 结果无效：{error}"
                        )
                # failed attempt; loop again if attempts remain
            else:
                assert result is not None
                return self._handle_node_failure(task, node, result, plan, policy, state)

            # Success: merge the node's own writes back into the shared task
            # worktree (uncommitted), so the post-graph diff/validation/review/
            # delivery pipeline is unchanged.
            if use_node_worktree:
                self._apply_file_changes(
                    node_workspace,
                    node_workspace.changes_since(node_before),
                    workspace,
                )

            result_ref = (
                f"artifacts/node-runs/{workflow_node.node_id}/"
                f"{node.node_id}/{task.iteration}.json"
            )
            self.store.write_json(self.store.task_dir(task.task_id) / result_ref, node_result)
            state["status"] = "completed"
            state["session_id"] = result.session_id
            state["run_ref"] = task.artifacts.get("last_agent_run", "")
            state["result_ref"] = result_ref
            state["finished_at"] = utc_now()
            state["error"] = ""
            pack = ContextPack(
                task_id=task.task_id,
                node_id=node.node_id,
                summary=node.title or node.node_id,
                facts=list(node_result.completed_steps),
                inputs=list(node.inputs),
                artifacts=[result_ref],
                source_sessions=[result.session_id] if result.session_id else [],
            )
            state["context_ref"] = self.context_ledger.write(pack)
            self.store.save(task)
            return None
        finally:
            if node_worktree_path is not None:
                self._remove_node_worktree(task, node_worktree_path)

    # ------------------------------------------------------------------
    # Per-node worktree helpers (graph execution only)
    # ------------------------------------------------------------------

    def _prepare_node_worktree(self, task: AgentTask, node: PlanNode) -> Path:
        """Create a fresh detached worktree for one node at the task's current
        delivery baseline. The path is deterministic from ``(task_id, node_id)``
        so resume is safe: a stale worktree from a crashed run is pruned first.

        The baseline must be ``delivery_base_commit or base_commit`` — the same
        expression ``_prepared_from_task`` uses. After an integration rebase the
        shared task worktree sits on the advanced target commit, so building
        node worktrees from the original ``base_commit`` would hand the node a
        tree that the replicated delta cannot reconcile."""
        path = self.store.task_dir(task.task_id) / "node-worktrees" / node.node_id
        project = self.get_project(task.project_id)
        return self.git_worktrees.add_node_worktree(
            Path(project.repository),
            path,
            task.delivery_base_commit or task.base_commit,
        )

    def _remove_node_worktree(self, task: AgentTask, path: Path) -> None:
        """Remove a node worktree, best-effort: a stuck worktree does not
        corrupt the task result and is pruned on the next ``add_node_worktree``."""
        try:
            project = self.get_project(task.project_id)
            self.git_worktrees.remove_node_worktree(Path(project.repository), path)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _replicate_into_node_worktree(
        workspace: Workspace,
        base: dict[str, str],
        node_workspace: Workspace,
    ) -> None:
        """Mirror the shared task worktree into the node worktree, including
        upstream deletions, by applying ``task.changes_since(base)`` with real
        bytes (binary-safe)."""
        AgentWorkflow._apply_file_changes(
            workspace, workspace.changes_since(base), node_workspace
        )

    @staticmethod
    def _apply_file_changes(
        src: Workspace,
        changes: list[FileChange],
        dst: Workspace,
    ) -> None:
        """Apply file changes from ``src`` into ``dst`` by copying real bytes
        (binary-safe) for writes and unlinking for deletes. ``changes`` is used
        only for path/action detection (from ``changes_since``); content is read
        from the source filesystem, not the (text-only) change record."""
        for change in changes:
            if change.action == "delete":
                target = dst.root / change.path
                if target.exists():
                    target.unlink()
                continue
            source = src.root / change.path
            target = dst.root / change.path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def _handle_node_failure(
        self,
        task: AgentTask,
        node: PlanNode,
        result: AgentResult,
        plan: ExecutionPlan,
        policy: ProjectPolicy,
        state: dict,
    ) -> AgentTask | None:
        state["status"] = "failed"
        if result.session_id:
            state["session_id"] = result.session_id
        state["error"] = result.error
        state["finished_at"] = utc_now()
        self.store.save(task)
        if node.on_failure == "skip":
            # Skip this node and let its dependents proceed; not terminal.
            state["status"] = "skipped"
            self.store.save(task)
            return None
        # NOTE(v1): node-level replan is mapped to a human pause rather than an
        # automatic replan, because a true replan needs a reviewer-style
        # handoff that node failures do not carry. The review-driven REPLAN
        # path (REVIEWING phase -> reviewer verdict REPLAN) stays fully intact.
        return self._pause(
            task,
            "node_failed",
            f"节点 {node.node_id} 执行失败：{result.error}",
            resume_phase=AgentTaskStatus.EXECUTING,
        )

    # ------------------------------------------------------------------
    # Bounded output self-repair (text-JSON runtimes only)
    # ------------------------------------------------------------------

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

    @staticmethod
    def _executor_repair_prompt(error: str, bad_output: dict) -> str:
        snippet = json.dumps(bad_output, ensure_ascii=False)[:2000]
        return (
            "\n\n# 上一条输出无法解析为执行结果，请修正后只重新输出该 JSON 对象\n"
            f"解析错误：{error}\n"
            f"上一条输出（片段）：{snippet}\n"
            "请只输出一个严格符合 ExecutionResult schema 的 JSON 对象：\n"
            "- completed_steps：字符串数组\n"
            "- modified_files：字符串数组\n"
            "- tests：数组，每项为 {\"command\":字符串,\"exit_code\":整数,"
            "\"stdout\":字符串,\"stderr\":字符串}；未运行命令时为 []\n"
            "- deviations：字符串数组\n"
            "- remaining_risks：字符串数组\n"
            "- next_steps：字符串数组\n"
            "不要使用 name/status/detail 等其它键名包裹测试结果。"
        )

    @staticmethod
    def _reviewer_repair_prompt(error: str, bad_output: dict) -> str:
        snippet = json.dumps(bad_output, ensure_ascii=False)[:2000]
        return (
            "\n\n# 上一条审核输出无法解析，请修正后只重新输出该 JSON 对象\n"
            f"解析错误：{error}\n"
            f"上一条输出（片段）：{snippet}\n"
            "请只输出一个严格符合 ReviewResult schema 的 JSON 对象：\n"
            "- verdict：\"pass\"、\"revise_code\"、\"replan\" 或 \"blocked\"\n"
            "- acceptance：数组，每项 {\"criterion\":字符串,\"passed\":布尔}，"
            "criterion 与计划 acceptance_criteria 逐字一致\n"
            "- issues：数组，每项 {\"file\":字符串,\"line\":非负整数,"
            "\"severity\":\"info|warning|blocker\",\"message\":非空字符串,"
            "\"evidence\":非空字符串,\"suggestion\":字符串}；无问题为 []\n"
            "- recommended_tests：字符串数组\n"
            "- summary：字符串\n"
            "不要使用 acceptance_results、diff_review 等其它键名。"
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
                request, self._executor_repair_prompt(error, bad_output)
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
                request, self._reviewer_repair_prompt(error, bad_output)
            ),
        )
        if not repair_result.succeeded:
            return None
        try:
            return ReviewResult.from_dict(repair_result.output), repair_result
        except ValueError:
            return None

    @staticmethod
    def _merge_node_results(results: list[ExecutionResult]) -> ExecutionResult:
        completed_steps: list[str] = []
        modified_files: list[str] = []
        tests: list = []
        deviations: list[str] = []
        remaining_risks: list[str] = []
        next_steps: list[str] = []
        for result in results:
            completed_steps.extend(result.completed_steps)
            modified_files.extend(result.modified_files)
            tests.extend(result.tests)
            deviations.extend(result.deviations)
            remaining_risks.extend(result.remaining_risks)
            next_steps.extend(result.next_steps)
        return ExecutionResult(
            completed_steps=list(dict.fromkeys(completed_steps)),
            modified_files=list(dict.fromkeys(modified_files)),
            tests=list(tests),
            deviations=list(dict.fromkeys(deviations)),
            remaining_risks=list(dict.fromkeys(remaining_risks)),
            next_steps=list(dict.fromkeys(next_steps)),
        )

    @staticmethod
    def _explicit_policy_tests(output: dict, policy: ProjectPolicy) -> list[str]:
        serialized = json.dumps(output, ensure_ascii=False)
        return [
            command.name
            for command in policy.validation_commands
            if re.search(
                rf"(?<![A-Za-z0-9_.-]){re.escape(command.name)}(?![A-Za-z0-9_.-])",
                serialized,
            )
        ]

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

    def _agent_budget(self, task: AgentTask) -> AgentBudget:
        remaining_time = max(
            0.001,
            task.budget.total_timeout_seconds
            - task.budget.consumed_active_seconds,
        )
        remaining_cost = (
            max(0.001, task.budget.max_cost_usd - task.budget.consumed_cost_usd)
            if task.budget.max_cost_usd is not None
            else None
        )
        return AgentBudget(
            total_timeout_seconds=min(task.budget.call_timeout_seconds, remaining_time),
            idle_timeout_seconds=min(task.budget.idle_timeout_seconds, remaining_time),
            max_cost_usd=remaining_cost,
        )

    @staticmethod
    def _task_budget_error(task: AgentTask) -> str:
        if task.budget.consumed_active_seconds >= task.budget.total_timeout_seconds:
            return "total_timeout"
        if (
            task.budget.max_cost_usd is not None
            and task.budget.consumed_cost_usd >= task.budget.max_cost_usd
        ):
            return "budget_exhausted"
        if (
            task.budget.max_total_tokens is not None
            and task.budget.consumed_input_tokens + task.budget.consumed_output_tokens
            >= task.budget.max_total_tokens
        ):
            return "token_budget_exhausted"
        if (
            task.budget.max_input_tokens is not None
            and task.budget.consumed_input_tokens >= task.budget.max_input_tokens
        ):
            return "input_token_budget_exhausted"
        if (
            task.budget.max_output_tokens is not None
            and task.budget.consumed_output_tokens >= task.budget.max_output_tokens
        ):
            return "output_token_budget_exhausted"
        return ""

    @staticmethod
    def _task_budget_overrun(task: AgentTask) -> str:
        if task.budget.consumed_active_seconds > task.budget.total_timeout_seconds:
            return "total_timeout"
        if (
            task.budget.max_cost_usd is not None
            and task.budget.consumed_cost_usd > task.budget.max_cost_usd
        ):
            return "budget_exhausted"
        if (
            task.budget.max_total_tokens is not None
            and task.budget.consumed_input_tokens + task.budget.consumed_output_tokens
            > task.budget.max_total_tokens
        ):
            return "token_budget_exhausted"
        if (
            task.budget.max_input_tokens is not None
            and task.budget.consumed_input_tokens > task.budget.max_input_tokens
        ):
            return "input_token_budget_exhausted"
        if (
            task.budget.max_output_tokens is not None
            and task.budget.consumed_output_tokens > task.budget.max_output_tokens
        ):
            return "output_token_budget_exhausted"
        return ""

    def _load_project_policy(self, task: AgentTask) -> ProjectPolicy:
        project = self.projects.get(task.project_id)
        return self.policy_loader.load(self.workspace_path(task.task_id), project.config_path)

    def _agent_policy(self, policy: ProjectPolicy, command_names: list[str]) -> AgentPolicy:
        commands = policy.required_commands(command_names)
        return AgentPolicy(
            allowed_commands=[list(command.argv) for command in commands],
            protected_paths=list(policy.protected_paths),
            timeout_seconds=policy.timeout_seconds,
            network_allowed=False,
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

        budget_error = self._task_budget_error(task)
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
            self._usage_tokens(response.usage)
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
        budget_error = self._task_budget_overrun(task)
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
    def _usage_tokens(usage: dict[str, Any]) -> tuple[int, int, int, int]:
        def first(*keys: str) -> int:
            for key in keys:
                value = usage.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return max(0, int(value))
            return 0

        raw_input = first("input_tokens", "prompt_tokens", "input")
        output = first("output_tokens", "completion_tokens", "output")
        cached = first(
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cached_tokens",
            "cache_read",
        )
        cache_is_included = any(
            key in usage for key in ("cached_input_tokens", "cached_tokens")
        )
        uncached = max(0, raw_input - cached) if cache_is_included else raw_input
        total_input = raw_input if cache_is_included else raw_input + cached
        return total_input, output, cached, uncached

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

    def _planner_instructions(
        self,
        task: AgentTask,
        policy: ProjectPolicy | None = None,
        workflow_node: WorkflowNode | None = None,
    ) -> str:
        payload = {
            "title": task.title,
            "requirement": task.requirement,
            "clarifications": task.clarifications,
        }
        experiences = self._relevant_experiences(task)
        if experiences:
            payload["approved_experience"] = experiences
        # The planner may only select named commands from the project policy
        # (ProjectPolicy.required_commands rejects anything else), so give it
        # the real list instead of leaving it to infer names from prose.
        if policy is not None:
            payload["available_validation_commands"] = [
                command.name for command in policy.validation_commands
            ]
        instructions = (
            "分析任务并生成结构化 ExecutionPlan。每次最多保留一个高影响未决问题；"
            "已有澄清答复必须作为需求约束。只输出符合 ExecutionPlan Schema 的完整 "
            "JSON 对象，不要 Markdown、代码围栏或解释文字。\n"
            + json.dumps(payload, ensure_ascii=False)
            + _PLANNER_OUTPUT_INSTRUCTION
        )
        return self._with_node_instructions(
            instructions,
            (
                workflow_node.instructions
                if workflow_node is not None
                else self._task_workflow(task).instructions_for(WorkflowNodeKind.PLANNER)
            ),
        )

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

    def _executor_instructions(
        self,
        task: AgentTask,
        plan: ExecutionPlan,
        review_feedback: ReviewResult | None,
        workflow_node: WorkflowNode | None = None,
    ) -> str:
        payload = {"plan": to_plain(plan), "review_feedback": to_plain(review_feedback)}
        instructions = "按照已批准的 ExecutionPlan 修改当前工作区。\n" + json.dumps(
            payload, ensure_ascii=False
        ) + _EXECUTOR_OUTPUT_INSTRUCTION
        return self._with_node_instructions(
            instructions,
            (
                workflow_node.instructions
                if workflow_node is not None
                else self._task_workflow(task).instructions_for(WorkflowNodeKind.EXECUTOR)
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
        changed_files = list(
            dict.fromkeys(
                match.group(1)
                for match in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE)
                if match.group(1) != "/dev/null"
            )
        )
        payload = {
            "requirement": task.requirement,
            "acceptance_criteria": list(plan.acceptance_criteria),
            "constraints": list(plan.constraints),
            "required_tests": list(plan.required_tests),
            "change_evidence": {
                "changed_files": changed_files,
                "diff_lines": len(diff.splitlines()),
                "diff_artifact": str(
                    (
                        self.store.task_dir(task.task_id)
                        / f"artifacts/rounds/{task.iteration}/changes.diff"
                    ).resolve()
                ),
                "instruction": "Inspect the read-only workspace and diff artifact when details are needed.",
            },
            "validation": (
                {**to_plain(validation)}
                if validation is not None
                else {"available": False, "reason": "No validation node has run yet."}
            ),
        }
        instructions = (
            "独立审核当前只读工作区，并输出结构化 ReviewResult。只输出符合 "
            "ReviewResult Schema 的完整 JSON 对象，不要 Markdown、代码围栏或解释文字。\n"
            + json.dumps(payload, ensure_ascii=False)
            + _REVIEWER_OUTPUT_INSTRUCTION
        )
        return self._with_node_instructions(
            instructions,
            (
                workflow_node.instructions
                if workflow_node is not None
                else self._task_workflow(task).instructions_for(WorkflowNodeKind.REVIEWER)
            ),
        )

    def _replanner_instructions(
        self,
        task: AgentTask,
        previous_plan: ExecutionPlan,
        review: ReviewResult,
    ) -> str:
        payload = {
            "title": task.title,
            "requirement": task.requirement,
            "previous_plan": to_plain(previous_plan),
            "review": to_plain(review),
        }
        instructions = (
            "审核认定已批准计划需要重做。重新检查当前只读工作区并生成新的 "
            "ExecutionPlan；新计划必须再次由用户批准。只输出符合 ExecutionPlan "
            "Schema 的完整 JSON 对象，不要 Markdown、代码围栏或解释文字。\n"
            + json.dumps(payload, ensure_ascii=False)
            + _PLANNER_OUTPUT_INSTRUCTION
        )
        return self._with_node_instructions(
            instructions,
            self._task_workflow(task).instructions_for(WorkflowNodeKind.PLANNER),
        )

    @staticmethod
    def _with_node_instructions(base: str, additional: str) -> str:
        if not additional:
            return base
        return f"{base}\n工作流节点附加要求：\n{additional}"

    @staticmethod
    def _task_workflow(task: AgentTask) -> WorkflowDefinition:
        if task.workflow:
            return workflow_from_dict(
                task.workflow,
                builtin=bool(task.workflow.get("builtin", False)),
            )
        # Tasks created before workflow snapshots were introduced retain guarded behavior.
        return BUILTIN_WORKFLOWS["guarded"]
