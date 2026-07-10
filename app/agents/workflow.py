from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from app.agents.contracts import (
    AgentAccess,
    AgentPolicy,
    AgentRequest,
    AgentResult,
    AgentTask,
    AgentTaskStatus,
    ExecutionPlan,
    ExecutionResult,
    ReviewResult,
    ReviewVerdict,
    ValidationResult,
)
from app.agents.runtime import AgentRuntime
from app.agents.store import AgentTaskStore
from app.core.contracts import PolicyBoundary, PolicyCheck, to_plain, utc_now
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
        self.git_worktrees = git_worktrees or GitWorktreeService()
        self.policy_loader = ProjectPolicyLoader()
        self.policy_checker = PolicyChecker()
        self.runtime = runtime
        self.validator = validator or DeterministicValidator()
        self.max_iterations = max_iterations

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

    def create_task(self, title: str, requirement: str, project_id: str) -> AgentTask:
        if not title.strip() or not requirement.strip():
            raise ValueError("任务标题和需求不能为空。")
        if not project_id.strip():
            raise ValueError("project_id 不能为空；新任务必须属于已注册 Git 项目。")
        task = AgentTask(
            title=title.strip(),
            requirement=requirement.strip(),
            project_id=project_id,
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
        task = self.store.load(task_id)
        if not task.project_id or not task.workspace or not task.task_branch:
            raise ValueError("任务没有可清理的项目 worktree。")
        project = self.projects.get(task.project_id)
        prepared = self._prepared_from_task(task, project)
        if task.status in (AgentTaskStatus.DRAFT, AgentTaskStatus.PREPARING_WORKSPACE):
            task.transition(AgentTaskStatus.CANCELLING, reason="user_cancelled")
            self.store.save(task)
        elif task.status is not AgentTaskStatus.CANCELLING:
            raise ValueError(
                f"任务 {task.task_id} 状态为 {task.status.value}，要求 draft 或 cancelling。"
            )
        self.git_worktrees.remove(project, prepared)
        task.transition(AgentTaskStatus.CANCELLED, reason="workspace_removed")
        self.store.save(task)
        return task

    def workspace_path(self, task_id: str) -> Path:
        task = self.store.load(task_id)
        if not task.workspace:
            raise ValueError(f"任务 {task_id} 没有 Git worktree。")
        return Path(task.workspace)

    def analyze(self, task_id: str) -> AgentTask:
        task = self.store.load(task_id)
        self._require_status(task, AgentTaskStatus.DRAFT)
        policy = self._load_project_policy(task)
        task.transition(AgentTaskStatus.ANALYZING)
        self.store.save(task)

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
            plan = ExecutionPlan.from_dict(response.output)
        except ValueError as error:
            return self._fail(task, AgentResult(succeeded=False, error=f"规划结果无效：{error}"))
        task.plan_version += 1
        task.sessions["planner"] = response.session_id
        plan_ref = f"artifacts/plans/{task.plan_version}.json"
        self.store.write_json(self.store.task_dir(task.task_id) / plan_ref, plan)
        task.artifacts["plan"] = plan_ref
        task.transition(AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
        self.store.save(task)
        return task

    def approve_plan(self, task_id: str) -> AgentTask:
        task = self.store.load(task_id)
        self._require_status(task, AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL)
        plan = self._load_plan(task)
        if plan.open_questions:
            raise ValueError("计划仍有未决问题，不能批准。")
        project = self.projects.get(task.project_id)
        self.git_worktrees.ensure_prepared(project, self._prepared_from_task(task, project))
        policy = self.policy_loader.load(self.workspace_path(task.task_id), project.config_path)
        policy.required_commands(plan.required_tests)
        effective_agent_policy = self._agent_policy(policy, plan.required_tests)

        task.approved_plan_version = task.plan_version
        workspace_path = self.workspace_path(task.task_id)
        workspace = Workspace(workspace_path)
        base = workspace.snapshot()
        base_ref = "artifacts/workspace-base.json"
        self.store.write_json(self.store.task_dir(task.task_id) / base_ref, base)
        task.artifacts["workspace_base"] = base_ref
        policy_ref = "artifacts/project-policy.json"
        self.store.write_json(self.store.task_dir(task.task_id) / policy_ref, policy)
        task.artifacts["project_policy"] = policy_ref
        self.store.save(task)
        review_feedback: ReviewResult | None = None

        for round_index in range(1, self.max_iterations + 1):
            task.iteration = round_index
            task.transition(AgentTaskStatus.EXECUTING)
            self.store.save(task)
            round_dir = self.store.task_dir(task.task_id) / "artifacts" / "rounds" / str(task.iteration)
            before_check = self._check_workspace_policy(workspace, base, policy)
            self.store.write_json(round_dir / "policy-before.json", before_check)
            if not before_check.passed:
                return self._block_policy(task, before_check)
            execution = self._invoke_agent(
                task,
                AgentRequest(
                    task_id=task.task_id,
                    role="executor",
                    instructions=self._executor_instructions(plan, review_feedback),
                    workspace=workspace_path,
                    access=AgentAccess.WORKSPACE_WRITE,
                    policy=effective_agent_policy,
                    session_id=task.sessions.get("executor", ""),
                ),
            )
            if not execution.succeeded:
                return self._fail(task, execution)
            try:
                execution_result = ExecutionResult.from_dict(execution.output)
            except ValueError as error:
                return self._fail(task, AgentResult(succeeded=False, error=f"执行结果无效：{error}"))
            self.store.write_json(round_dir / "execution.json", execution_result)
            current = workspace.snapshot()
            diff = workspace.diff(base, current)
            self.store.write_text(round_dir / "changes.diff", diff)

            task.transition(AgentTaskStatus.VALIDATING)
            self.store.save(task)
            after_check = self._check_workspace_policy(workspace, base, policy)
            self.store.write_json(round_dir / "policy-after.json", after_check)
            if not after_check.passed:
                return self._block_policy(task, after_check)
            try:
                validation = self.validator.validate(task.task_id, workspace_path, plan, policy)
            except Exception as error:  # noqa: BLE001 - validator failures must leave a reloadable task
                return self._fail(
                    task,
                    AgentResult(succeeded=False, error=f"验证器异常：{error}"),
                )
            self.store.write_json(round_dir / "validation.json", validation)
            validation_check = self._check_workspace_policy(workspace, base, policy)
            self.store.write_json(round_dir / "policy-validation.json", validation_check)
            current = workspace.snapshot()
            diff = workspace.diff(base, current)
            self.store.write_text(round_dir / "changes.diff", diff)
            if not validation_check.passed:
                return self._block_policy(task, validation_check)
            if not validation.passed:
                task.error = validation.error or "必需验证未通过。"
                task.transition(AgentTaskStatus.BLOCKED)
                self.store.save(task)
                return task

            task.transition(AgentTaskStatus.REVIEWING)
            self.store.save(task)
            review = self._invoke_agent(
                task,
                AgentRequest(
                    task_id=task.task_id,
                    role="reviewer",
                    instructions=self._reviewer_instructions(task, plan, diff, validation),
                    workspace=workspace_path,
                    access=AgentAccess.READ_ONLY,
                    policy=effective_agent_policy,
                    session_id=task.sessions.get("reviewer", ""),
                ),
            )
            if not review.succeeded:
                return self._fail(task, review)
            try:
                review_result = ReviewResult.from_dict(review.output)
                review_result.validate_pass(plan)
            except ValueError as error:
                return self._fail(task, AgentResult(succeeded=False, error=f"审核结果无效：{error}"))
            self.store.write_json(round_dir / "review.json", review_result)

            verdict = review_result.verdict
            if verdict is ReviewVerdict.PASS:
                task.error = ""
                task.transition(AgentTaskStatus.READY_TO_DELIVER)
                self.store.save(task)
                return task
            if verdict is ReviewVerdict.REVISE_CODE:
                review_feedback = review_result
                self.store.save(task)
                continue

            task.error = f"审核要求人工处理：{verdict.value}"
            task.transition(AgentTaskStatus.BLOCKED)
            self.store.save(task)
            return task

        task.error = f"代码返修达到最大轮次 {self.max_iterations}。"
        task.transition(AgentTaskStatus.BLOCKED)
        self.store.save(task)
        return task

    def _load_plan(self, task: AgentTask) -> ExecutionPlan:
        path = self.store.task_dir(task.task_id) / task.artifacts["plan"]
        return ExecutionPlan.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _require_status(self, task: AgentTask, expected: AgentTaskStatus) -> None:
        if task.status is not expected:
            raise ValueError(f"任务 {task.task_id} 状态为 {task.status.value}，要求 {expected.value}。")

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
        task.transition(AgentTaskStatus.BLOCKED)
        self.store.save(task)
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
            base_commit=task.base_commit,
            target_branch=task.target_branch,
            task_branch=task.task_branch,
        )

    def _invoke_agent(self, task: AgentTask, request: AgentRequest) -> AgentResult:
        task.run_count += 1
        run_ref = f"artifacts/runs/{task.run_count}-{request.role}.json"
        started_at = utc_now()
        record = {
            "schema_version": 1,
            "index": task.run_count,
            "role": request.role,
            "status": "running",
            "access": request.access.value,
            "policy": to_plain(request.policy),
            "session_id": request.session_id,
            "instructions": request.instructions,
            "started_at": started_at,
            "finished_at": "",
            "output": {},
            "error": "",
        }
        self.store.write_json(self.store.task_dir(task.task_id) / run_ref, record)
        task.artifacts["last_agent_run"] = run_ref
        self.store.save(task)

        try:
            response = self.runtime.invoke(request)
        except Exception as error:  # noqa: BLE001 - runtime failures become persistent task results
            response = AgentResult(succeeded=False, error=f"AgentRuntime 异常：{error}")

        record.update(
            {
                "status": "succeeded" if response.succeeded else "failed",
                "session_id": response.session_id or request.session_id,
                "finished_at": utc_now(),
                "output": response.output,
                "events": response.events,
                "error": response.error,
            }
        )
        self.store.write_json(self.store.task_dir(task.task_id) / run_ref, record)
        if response.succeeded and response.session_id:
            task.sessions[request.role] = response.session_id
        self.store.save(task)
        return response

    def _fail(self, task: AgentTask, response: AgentResult) -> AgentTask:
        task.error = response.error or "代理运行失败。"
        task.transition(AgentTaskStatus.FAILED)
        self.store.save(task)
        return task

    def _planner_instructions(self, task: AgentTask) -> str:
        return f"分析任务并生成结构化 ExecutionPlan。\n标题：{task.title}\n需求：{task.requirement}"

    def _executor_instructions(
        self,
        plan: ExecutionPlan,
        review_feedback: ReviewResult | None,
    ) -> str:
        payload = {"plan": to_plain(plan), "review_feedback": to_plain(review_feedback)}
        return "按照已批准的 ExecutionPlan 修改当前工作区。\n" + json.dumps(payload, ensure_ascii=False)

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
        return "独立审核当前只读工作区，并输出结构化 ReviewResult。\n" + json.dumps(payload, ensure_ascii=False)
