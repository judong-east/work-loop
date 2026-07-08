from __future__ import annotations

from app.core.contracts import (
    DecisionAction,
    DecisionResult,
    EvaluationResult,
    PolicyCheck,
    Severity,
    TaskState,
    TaskStatus,
)


class DecisionEngine:
    def decide_after_context(
        self,
        task: TaskState,
        evaluation: EvaluationResult,
        policy_check: PolicyCheck,
    ) -> DecisionResult:
        if not policy_check.passed:
            return DecisionResult(
                action=DecisionAction.BLOCK,
                reason="边界策略阻断：" + "；".join(policy_check.issues),
                confidence=0.98,
                next_state=TaskStatus.POLICY_BLOCKED,
                required_inputs=policy_check.issues,
            )

        if policy_check.requires_human:
            return DecisionResult(
                action=DecisionAction.REQUEST_HUMAN_INPUT,
                reason="边界策略要求人工确认：" + "；".join(policy_check.warnings),
                confidence=0.95,
                next_state=TaskStatus.CLARIFICATION_REQUIRED,
                required_inputs=policy_check.warnings,
            )

        blockers = [issue for issue in evaluation.issues if issue.severity is Severity.BLOCKER]
        if blockers:
            return DecisionResult(
                action=DecisionAction.REQUEST_HUMAN_INPUT,
                reason="评测发现阻断问题，需要人工确认。",
                confidence=0.9,
                next_state=TaskStatus.CLARIFICATION_REQUIRED,
                required_inputs=[issue.message for issue in blockers],
            )

        if evaluation.issues:
            return DecisionResult(
                action=DecisionAction.CONTINUE,
                reason="存在非阻断问题，但可以进入下一步并保留风险记录。",
                confidence=0.78,
                next_state=TaskStatus.READY_FOR_PLAN,
                required_inputs=[issue.message for issue in evaluation.issues],
            )

        return DecisionResult(
            action=DecisionAction.CONTINUE,
            reason="上下文评测通过，边界策略允许继续。",
            confidence=0.92,
            next_state=TaskStatus.READY_FOR_PLAN,
        )


