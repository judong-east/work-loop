from __future__ import annotations

import json
from pathlib import Path

from app.agents.contracts import (
    AgentTask,
    AgentTaskStatus,
    DeliveryReport,
    ExecutionResult,
    ReviewResult,
    ReviewVerdict,
    ValidationResult,
    delivery_report_from_dict,
)
from app.agents.workflow import AgentWorkflow
from app.core.contracts import to_plain, utc_now
from app.projects.git_delivery import GitDelivery


class DeliveryService:
    def __init__(self, workflow: AgentWorkflow, git: GitDelivery | None = None):
        self.workflow = workflow
        self.git = git or GitDelivery()

    def prepare(self, task_id: str) -> AgentTask:
        task = self.workflow.get_task(task_id)
        if task.status is not AgentTaskStatus.READY_TO_DELIVER:
            raise ValueError(f"任务 {task_id} 状态为 {task.status.value}，不能生成交付。")
        plan = self.workflow.get_plan(task_id)
        validation, review = self._verified_evidence(task, plan.acceptance_criteria)
        project = self.workflow.get_project(task.project_id)
        repository = Path(project.repository)
        workspace = Path(task.workspace)
        target_commit = self.git.head(repository, f"refs/heads/{task.target_branch}")
        effective_base = task.delivery_base_commit or task.base_commit

        if task.task_commit and self.git.is_clean(workspace):
            task_commit = self.git.head(workspace)
            if task_commit != task.task_commit:
                raise ValueError("任务分支 HEAD 与已记录任务提交不一致。")
        else:
            if self.git.head(workspace) != effective_base:
                raise ValueError("任务 worktree HEAD 与当前交付基线不一致。")
            task_commit = self.git.commit_all(
                workspace,
                f"workloop({task.task_id}): {task.title}",
            )
            if task_commit == effective_base:
                raise ValueError("任务没有可交付的代码变更。")
            task.task_commit = task_commit
            self.workflow.store.save(task)

        if target_commit != effective_base:
            return self._require_integration(task, target_commit)

        report = self._build_report(
            task,
            target_commit,
            task_commit,
            validation,
            review,
        )
        report_ref = f"artifacts/delivery-reports/{task_commit}.json"
        self.workflow.store.write_json(
            self.workflow.store.task_dir(task.task_id) / report_ref,
            report,
        )
        task.artifacts["delivery_report"] = report_ref
        task.delivery_target_commit = target_commit
        task.error = ""
        task.pause_reason = ""
        self.workflow.store.save(task)
        return task

    def load_report(self, task_id: str) -> DeliveryReport:
        task = self.workflow.get_task(task_id)
        reference = task.artifacts.get("delivery_report", "")
        if not reference:
            raise FileNotFoundError(f"任务 {task_id} 尚无有效 DeliveryReport。")
        path = self.workflow.store.task_dir(task_id) / reference
        return delivery_report_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def integrate(self, task_id: str) -> AgentTask:
        task = self.workflow.get_task(task_id)
        if task.status is not AgentTaskStatus.INTEGRATION_REQUIRED:
            raise ValueError(f"任务 {task_id} 不处于 integration_required。")
        project = self.workflow.get_project(task.project_id)
        repository = Path(project.repository)
        workspace = Path(task.workspace)
        target_commit = self.git.head(repository, f"refs/heads/{task.target_branch}")
        task.transition(AgentTaskStatus.INTEGRATING, reason="target_branch_advanced")
        task.error = ""
        self.workflow.store.save(task)

        succeeded, detail = self.git.rebase(workspace, task.target_branch)
        if not succeeded:
            task = self.workflow.get_task(task_id)
            task.pause_reason = "integration_conflict"
            task.error = f"重新整合发生冲突，等待人工处理：{detail}"
            task.transition(AgentTaskStatus.BLOCKED, reason="integration_conflict")
            self.workflow.store.save(task)
            return task

        rebased_commit = self.git.head(workspace)
        base = self.git.snapshot(repository, target_commit)
        base[".git"] = self.git.worktree_marker(workspace)
        self.git.reset_mixed(workspace, target_commit)
        task = self.workflow.get_task(task_id)
        task.integration_count += 1
        integration_dir = (
            self.workflow.store.task_dir(task_id)
            / "artifacts"
            / "integrations"
            / str(task.integration_count)
        )
        base_ref = f"artifacts/integrations/{task.integration_count}/workspace-base.json"
        self.workflow.store.write_json(integration_dir / "workspace-base.json", base)
        self.workflow.store.write_json(
            integration_dir / "integration.json",
            {
                "schema_version": 1,
                "target_commit": target_commit,
                "rebased_commit": rebased_commit,
                "at": utc_now(),
            },
        )
        task.delivery_base_commit = target_commit
        task.task_commit = ""
        task.delivery_target_commit = ""
        task.artifacts["workspace_base"] = base_ref
        task.artifacts.pop("delivery_report", None)
        self.workflow.store.save(task)

        reviewed = self.workflow.revalidate_integrated(task_id)
        if reviewed.status is not AgentTaskStatus.READY_TO_DELIVER:
            return reviewed
        return self.prepare(task_id)

    def deliver(
        self,
        task_id: str,
        strategy: str,
        confirmed: bool,
    ) -> AgentTask:
        if not confirmed:
            raise ValueError("交付必须经过用户明确确认。")
        task = self.workflow.get_task(task_id)
        if task.status is not AgentTaskStatus.READY_TO_DELIVER:
            raise ValueError(f"任务 {task_id} 状态为 {task.status.value}，不能交付。")
        report = self.load_report(task_id)
        if report.task_commit != task.task_commit:
            raise ValueError("DeliveryReport 与任务提交不一致。")
        project = self.workflow.get_project(task.project_id)
        repository = Path(project.repository)
        current_target = self.git.head(repository, f"refs/heads/{task.target_branch}")
        if current_target != report.target_commit:
            return self._require_integration(task, current_target)

        delivered_commit = self.git.deliver(
            repository,
            task.target_branch,
            task.task_branch,
            task.task_commit,
            strategy,
        )
        task.delivered_commit = delivered_commit
        task.transition(AgentTaskStatus.DELIVERED, reason=f"confirmed_{strategy}")
        record_ref = "artifacts/delivery-record.json"
        self.workflow.store.write_json(
            self.workflow.store.task_dir(task_id) / record_ref,
            {
                "schema_version": 1,
                "strategy": strategy,
                "task_commit": task.task_commit,
                "target_before": current_target,
                "delivered_commit": delivered_commit,
                "confirmed": True,
                "delivered_at": utc_now(),
            },
        )
        task.artifacts["delivery_record"] = record_ref
        task.error = ""
        self.workflow.store.save(task)
        return task

    def _require_integration(self, task: AgentTask, target_commit: str) -> AgentTask:
        reference = task.artifacts.pop("delivery_report", "")
        if reference:
            task.artifacts[f"stale_delivery_report_{task.integration_count + 1}"] = reference
        task.delivery_target_commit = ""
        task.error = (
            f"目标分支已从交付基线前进到 {target_commit}，旧验证和审核已失效。"
        )
        task.transition(
            AgentTaskStatus.INTEGRATION_REQUIRED,
            reason="target_branch_advanced",
        )
        self.workflow.store.save(task)
        return task

    def _verified_evidence(
        self,
        task: AgentTask,
        criteria: list[str],
    ) -> tuple[ValidationResult, ReviewResult]:
        round_dir = self._round_dir(task)
        validation = ValidationResult.from_dict(
            json.loads((round_dir / "validation.json").read_text(encoding="utf-8"))
        )
        review = ReviewResult.from_dict(
            json.loads((round_dir / "review.json").read_text(encoding="utf-8"))
        )
        if not validation.passed:
            raise ValueError("最终确定性验证未通过，不能生成交付。")
        if review.verdict is not ReviewVerdict.PASS:
            raise ValueError("最终审核未通过，不能生成交付。")
        plan = self.workflow.get_plan(task.task_id)
        if criteria != plan.acceptance_criteria:
            raise ValueError("验收标准与批准计划不一致。")
        review.validate_pass(plan)
        return validation, review

    def _build_report(
        self,
        task: AgentTask,
        target_commit: str,
        task_commit: str,
        validation: ValidationResult,
        review: ReviewResult,
    ) -> DeliveryReport:
        plan = self.workflow.get_plan(task.task_id)
        execution = self._latest_execution(task)
        project = self.workflow.get_project(task.project_id)
        modified = self.git.changed_files(
            Path(project.repository),
            target_commit,
            task_commit,
        )
        risks = list(dict.fromkeys([*plan.risks, *execution.remaining_risks]))
        next_steps = list(dict.fromkeys(execution.next_steps or ["Confirm delivery"]))
        return DeliveryReport(
            requirement_summary=task.requirement,
            acceptance=list(review.acceptance),
            modified_files=modified,
            implementation_summary=list(execution.completed_steps),
            validation_evidence=[dict(item) for item in validation.checks],
            review_verdict=review.verdict.value,
            review_summary=review.summary,
            known_risks=risks,
            human_next_steps=next_steps,
            task_branch=task.task_branch,
            target_branch=task.target_branch,
            target_commit=target_commit,
            task_commit=task_commit,
        )

    def _latest_execution(self, task: AgentTask) -> ExecutionResult:
        for index in range(task.iteration, 0, -1):
            path = (
                self.workflow.store.task_dir(task.task_id)
                / "artifacts"
                / "rounds"
                / str(index)
                / "execution.json"
            )
            if path.is_file():
                return ExecutionResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
        raise FileNotFoundError(f"任务 {task.task_id} 缺少执行工件。")

    def _round_dir(self, task: AgentTask) -> Path:
        return (
            self.workflow.store.task_dir(task.task_id)
            / "artifacts"
            / "rounds"
            / str(task.iteration)
        )
