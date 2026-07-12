from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Protocol

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
from app.agents.runtime import AgentRuntime
from app.agents.store import AgentTaskStore
from app.agents.workflow_config import (
    BUILTIN_WORKFLOWS,
    WorkflowCatalog,
    WorkflowDefinition,
    WorkflowNodeKind,
    workflow_from_dict,
)
from app.core.contracts import PolicyBoundary, PolicyCheck, to_plain, utc_now
from app.core.redaction import redact, redact_value
from app.policy.policy_checker import PolicyChecker
from app.projects.contracts import Project, ProjectPolicy
from app.projects.git_worktree import GitWorktreeService, PreparedWorktree
from app.projects.policy import ProjectPolicyLoader
from app.projects.registry import ProjectRegistry
from app.tools.workspace import Workspace
from app.validation.runner import DeterministicValidator


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
    ):
        if max_iterations <= 0:
            raise ValueError("max_iterations 必须大于 0。")
        self.root = Path(root)
        self.store = AgentTaskStore(self.root / "tasks")
        self.projects = ProjectRegistry(self.root / "projects")
        self.workflows = WorkflowCatalog(self.root / "workflows.json")
        self.git_worktrees = git_worktrees or GitWorktreeService()
        self.policy_loader = ProjectPolicyLoader()
        self.policy_checker = PolicyChecker()
        self.runtime = runtime
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
        repo_root, branch = self.git_worktrees.inspect(repository, default_branch)
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
            )
        )

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
        task = AgentTask(
            title=title.strip(),
            requirement=requirement.strip(),
            project_id=project_id,
            workflow_id=workflow.workflow_id,
            workflow=to_plain(workflow),
            budget=effective_budget,
        )
        project = self.projects.get(project_id)
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
        role_by_phase = {
            AgentTaskStatus.ANALYZING: "planner",
            AgentTaskStatus.EXECUTING: "executor",
            AgentTaskStatus.REVIEWING: "reviewer",
            AgentTaskStatus.REPLANNING: "planner",
        }
        if rerun and (role := role_by_phase.get(phase)):
            task.sessions.pop(role, None)
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
        if phase is AgentTaskStatus.EXECUTING and pause_reason == "max_iterations":
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

    def revalidate_integrated(self, task_id: str) -> AgentTask:
        task = self.store.load(task_id)
        self._require_status(task, AgentTaskStatus.INTEGRATING)
        plan = self._load_plan(task)
        policy = self._load_project_policy(task)
        policy.required_commands(plan.required_tests)
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

        response = self._invoke_agent(
            task,
            AgentRequest(
                task_id=task.task_id,
                role="planner",
                instructions=self._planner_instructions(task),
                workspace=self.workspace_path(task.task_id),
                access=AgentAccess.READ_ONLY,
                policy=self._agent_policy(policy, []),
                session_id=task.sessions.get("planner", ""),
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
        task.sessions["planner"] = response.session_id
        plan_ref = f"artifacts/plans/{task.plan_version}.json"
        self.store.write_json(self.store.task_dir(task.task_id) / plan_ref, plan)
        task.artifacts["plan"] = plan_ref
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

        if task.approved_plan_version != task.plan_version:
            task.plan_iteration = 0
        task.approved_plan_version = task.plan_version
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
        while True:
            if phase is AgentTaskStatus.EXECUTING:
                if new_round:
                    if task.plan_iteration >= task.budget.max_iterations:
                        return self._pause(
                            task,
                            "max_iterations",
                            f"代码返修达到最大轮次 {task.budget.max_iterations}。",
                            resume_phase=AgentTaskStatus.EXECUTING,
                        )
                    task.iteration += 1
                    task.plan_iteration += 1
                if (
                    task.status is not AgentTaskStatus.EXECUTING
                    and not self._transition_unless_cancelled(
                        task, AgentTaskStatus.EXECUTING
                    )
                ):
                    return self._finish_or_return_cancellation(task)
                round_dir = self._round_dir(task)
                before_check = self._check_workspace_policy(workspace, base, policy)
                self.store.write_json(round_dir / "policy-before.json", before_check)
                if not before_check.passed:
                    return self._block_policy(task, before_check)
                execution = self._invoke_agent(
                    task,
                    AgentRequest(
                        task_id=task.task_id,
                        role="executor",
                        instructions=self._executor_instructions(task, plan, review_feedback),
                        workspace=workspace_path,
                        access=AgentAccess.WORKSPACE_WRITE,
                        policy=effective_agent_policy,
                        budget=self._agent_budget(task),
                        session_id=task.sessions.get("executor", ""),
                    ),
                )
                if not execution.succeeded:
                    return self._fail(task, execution)
                try:
                    execution_result = ExecutionResult.from_dict(execution.output)
                except ValueError as error:
                    return self._fail(
                        task,
                        AgentResult(succeeded=False, error=f"执行结果无效：{error}"),
                    )
                self.store.write_json(round_dir / "execution.json", execution_result)
                current = workspace.snapshot()
                self.store.write_text(round_dir / "changes.diff", workspace.diff(base, current))
                phase = AgentTaskStatus.VALIDATING
                new_round = False

            round_dir = self._round_dir(task)
            if phase is AgentTaskStatus.VALIDATING:
                if (
                    task.status is not AgentTaskStatus.VALIDATING
                    and not self._transition_unless_cancelled(
                        task, AgentTaskStatus.VALIDATING
                    )
                ):
                    return self._finish_or_return_cancellation(task)
                after_check = self._check_workspace_policy(workspace, base, policy)
                self.store.write_json(round_dir / "policy-after.json", after_check)
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
                    "started_at": utc_now(),
                    "finished_at": "",
                    "error": "",
                }
                self.store.write_json(validation_run_path, validation_run)
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
                validation_check = self._check_workspace_policy(workspace, base, policy)
                self.store.write_json(
                    round_dir / "policy-validation.json",
                    validation_check,
                )
                current = workspace.snapshot()
                diff = workspace.diff(base, current)
                self.store.write_text(round_dir / "changes.diff", diff)
                if not validation_check.passed:
                    return self._block_policy(task, validation_check)
                if not validation.passed:
                    return self._pause(
                        task,
                        "validation_failed",
                        validation.error or "必需验证未通过。",
                        resume_phase=AgentTaskStatus.VALIDATING,
                    )
                budget_error = self._task_budget_overrun(task)
                if budget_error:
                    return self._pause(
                        task,
                        budget_error,
                        resume_phase=AgentTaskStatus.REVIEWING,
                    )
                phase = AgentTaskStatus.REVIEWING
            else:
                validation = self._load_round_validation(task)
                current = workspace.snapshot()
                diff = workspace.diff(base, current)

            if (
                task.status is not AgentTaskStatus.REVIEWING
                and not self._transition_unless_cancelled(
                    task, AgentTaskStatus.REVIEWING
                )
            ):
                return self._finish_or_return_cancellation(task)
            review = self._invoke_agent(
                task,
                AgentRequest(
                    task_id=task.task_id,
                    role="reviewer",
                    instructions=self._reviewer_instructions(task, plan, diff, validation),
                    workspace=workspace_path,
                    access=AgentAccess.READ_ONLY,
                    policy=effective_agent_policy,
                    budget=self._agent_budget(task),
                    session_id=task.sessions.get("reviewer", ""),
                ),
            )
            if not review.succeeded:
                return self._fail(task, review)
            try:
                review_result = ReviewResult.from_dict(review.output)
                review_result.validate_pass(plan)
            except ValueError as error:
                return self._fail(
                    task,
                    AgentResult(succeeded=False, error=f"审核结果无效：{error}"),
                )
            self.store.write_json(round_dir / "review.json", review_result)

            verdict = review_result.verdict
            if verdict is ReviewVerdict.PASS:
                task.error = ""
                task.pause_reason = ""
                if not self._transition_unless_cancelled(
                    task, AgentTaskStatus.READY_TO_DELIVER
                ):
                    return self._finish_or_return_cancellation(task)
                return task
            if verdict is ReviewVerdict.REVISE_CODE:
                review_feedback = review_result
                phase = AgentTaskStatus.EXECUTING
                new_round = True
                continue
            if verdict is ReviewVerdict.REPLAN:
                return self._replan(task, plan, review_result, policy)

            task.error = f"审核要求人工处理：{verdict.value}"
            if not self._transition_unless_cancelled(task, AgentTaskStatus.BLOCKED):
                return self._finish_or_return_cancellation(task)
            return task

    def _replan(
        self,
        task: AgentTask,
        previous_plan: ExecutionPlan,
        review: ReviewResult,
        policy: ProjectPolicy,
    ) -> AgentTask:
        if not self._transition_unless_cancelled(task, AgentTaskStatus.REPLANNING):
            return self._finish_or_return_cancellation(task)
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
                session_id=task.sessions.get("planner", ""),
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
        plan_ref = f"artifacts/plans/{task.plan_version}.json"
        self.store.write_json(self.store.task_dir(task.task_id) / plan_ref, plan)
        task.artifacts["plan"] = plan_ref
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

    def _load_round_review(self, task: AgentTask) -> ReviewResult:
        path = (
            self.store.task_dir(task.task_id)
            / "artifacts"
            / "rounds"
            / str(task.iteration)
            / "review.json"
        )
        if not path.is_file():
            raise FileNotFoundError(f"任务 {task.task_id} 缺少可恢复审核工件：{path}")
        return ReviewResult.from_dict(json.loads(path.read_text(encoding="utf-8")))

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
        cost = response.usage.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            task.budget.consumed_cost_usd += float(cost)
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
                "events": response.events,
                "raw_events": response.raw_events,
                "usage": response.usage,
                "error_type": response.error_type,
                "error": response.error,
            })
            self.store.write_json(
                run_path,
                redact_value(to_plain(record), request.policy.redact_patterns),
            )
            if response.session_id:
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

    def _planner_instructions(self, task: AgentTask) -> str:
        payload = {
            "title": task.title,
            "requirement": task.requirement,
            "clarifications": task.clarifications,
        }
        instructions = (
            "分析任务并生成结构化 ExecutionPlan。每次最多保留一个高影响未决问题；"
            "已有澄清答复必须作为需求约束。只输出符合 ExecutionPlan Schema 的完整 "
            "JSON 对象，不要 Markdown、代码围栏或解释文字。\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        return self._with_node_instructions(
            instructions,
            self._task_workflow(task).instructions_for(WorkflowNodeKind.PLANNER),
        )

    def _executor_instructions(
        self,
        task: AgentTask,
        plan: ExecutionPlan,
        review_feedback: ReviewResult | None,
    ) -> str:
        payload = {"plan": to_plain(plan), "review_feedback": to_plain(review_feedback)}
        instructions = "按照已批准的 ExecutionPlan 修改当前工作区。\n" + json.dumps(
            payload, ensure_ascii=False
        )
        return self._with_node_instructions(
            instructions,
            self._task_workflow(task).instructions_for(WorkflowNodeKind.EXECUTOR),
        )

    def _reviewer_instructions(
        self,
        task: AgentTask,
        plan: ExecutionPlan,
        diff: str,
        validation: ValidationResult,
    ) -> str:
        payload = {
            "requirement": task.requirement,
            "plan": to_plain(plan),
            "diff": diff,
            "validation": {
                **to_plain(validation),
            },
        }
        instructions = (
            "独立审核当前只读工作区，并输出结构化 ReviewResult。只输出符合 "
            "ReviewResult Schema 的完整 JSON 对象，不要 Markdown、代码围栏或解释文字。\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        return self._with_node_instructions(
            instructions,
            self._task_workflow(task).instructions_for(WorkflowNodeKind.REVIEWER),
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
