from __future__ import annotations

from statistics import mean

from app.core.contracts import (
    ContextPack,
    EvaluationIssue,
    EvaluationResult,
    Severity,
)


class ContextEvaluator:
    def evaluate(self, context: ContextPack) -> EvaluationResult:
        issues: list[EvaluationIssue] = []

        if not context.sections:
            issues.append(
                EvaluationIssue(
                    message="上下文包没有任何可用片段。",
                    severity=Severity.BLOCKER,
                    issue_type="empty_context",
                    suggested_action="补充需求描述、文档或访谈材料。",
                )
            )

        for missing in context.missing_context:
            issues.append(
                EvaluationIssue(
                    message=missing,
                    severity=Severity.WARNING,
                    issue_type="missing_context",
                    suggested_action="补充材料或让系统生成澄清问题。",
                )
            )

        for conflict in context.conflicts:
            issues.append(
                EvaluationIssue(
                    message=conflict,
                    severity=Severity.BLOCKER,
                    issue_type="context_conflict",
                    suggested_action="请求人工确认冲突规则。",
                )
            )

        confidences = [section.confidence for section in context.sections]
        score = mean(confidences) if confidences else 0.0
        blocking = any(issue.severity == Severity.BLOCKER for issue in issues)
        status = "failed" if blocking else "warning" if issues else "passed"

        return EvaluationResult(
            evaluator="context_evaluator",
            status=status,
            score=round(score, 3),
            issues=issues,
            blocking=blocking,
        )


class DeliveryGateEvaluator:
    def evaluate(self, test_passed: bool, has_delivery_note: bool) -> EvaluationResult:
        issues: list[EvaluationIssue] = []
        if not test_passed:
            issues.append(
                EvaluationIssue(
                    message="测试或验证未通过，不能交付。",
                    severity=Severity.BLOCKER,
                    issue_type="validation_failed",
                    suggested_action="修复失败项后重新验证。",
                )
            )
        if not has_delivery_note:
            issues.append(
                EvaluationIssue(
                    message="缺少交付说明、风险说明或回滚信息。",
                    severity=Severity.BLOCKER,
                    issue_type="missing_delivery_artifact",
                    suggested_action="生成交付文档后重新评测。",
                )
            )

        blocking = bool(issues)
        return EvaluationResult(
            evaluator="delivery_gate_evaluator",
            status="failed" if blocking else "passed",
            score=0.0 if blocking else 1.0,
            issues=issues,
            blocking=blocking,
        )

