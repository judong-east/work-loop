from __future__ import annotations

from typing import Any


# These presets describe governance defaults, not model routing.  A workflow
# still owns its nodes and provider/model bindings; a strategy only controls
# how a task is staged and which human gates are recommended.
STRATEGY_PRESETS: dict[str, dict[str, Any]] = {
    "direct-fix": {
        "label": "直接修复",
        "task_type": "bugfix",
        "complexity_min": "S",
        "phases": ["analysis", "implementation", "testing"],
        "recommended_workflow_id": "default-task",
        "requires_approval": False,
        "required_capabilities": ["coding", "testing"],
    },
    "quick-implement": {
        "label": "快速实现",
        "task_type": "feature",
        "complexity_min": "S",
        "phases": ["analysis", "implementation", "testing"],
        "recommended_workflow_id": "default-task",
        "requires_approval": False,
        "required_capabilities": ["coding"],
    },
    "guided-develop": {
        "label": "引导开发",
        "task_type": "feature",
        "complexity_min": "M",
        "phases": ["analysis", "planning", "implementation", "review", "testing"],
        "recommended_workflow_id": "default-task",
        "requires_approval": False,
        "required_capabilities": ["planning", "coding", "review"],
    },
    "full-collaborate": {
        "label": "完整协作",
        "task_type": "feature",
        "complexity_min": "L",
        "phases": ["analysis", "planning", "implementation", "review", "testing", "delivery"],
        "recommended_workflow_id": "default-task",
        "requires_approval": True,
        "required_capabilities": ["planning", "coding", "review", "testing"],
    },
    "debug-investigate": {
        "label": "调试调查",
        "task_type": "bugfix",
        "complexity_min": "M",
        "phases": ["analysis", "reproduction", "implementation", "testing"],
        "recommended_workflow_id": "default-task",
        "requires_approval": False,
        "required_capabilities": ["testing", "coding"],
    },
    "refactor-safely": {
        "label": "安全重构",
        "task_type": "refactor",
        "complexity_min": "M",
        "phases": ["analysis", "planning", "implementation", "review", "testing"],
        "recommended_workflow_id": "default-task",
        "requires_approval": True,
        "required_capabilities": ["planning", "coding", "review", "testing"],
    },
    "review-audit": {
        "label": "审查审计",
        "task_type": "review",
        "complexity_min": "S",
        "phases": ["analysis", "review", "testing"],
        "recommended_workflow_id": "default-task",
        "requires_approval": False,
        "required_capabilities": ["review"],
    },
    "long-horizon": {
        "label": "长时程开发",
        "task_type": "feature",
        "complexity_min": "L",
        "phases": ["planning", "loop-execute", "audit", "testing", "review"],
        "recommended_workflow_id": "long-horizon-task",
        "requires_approval": True,
        "required_capabilities": ["planning", "coding", "review", "testing"],
    },
    "git-action": {
        "label": "版本操作",
        "task_type": "git",
        "complexity_min": "S",
        "phases": ["analysis", "implementation", "review"],
        "recommended_workflow_id": "default-task",
        "requires_approval": True,
        "required_capabilities": ["coding", "review"],
    },
}


def list_strategy_presets() -> list[dict[str, Any]]:
    return [{"strategy": key, **dict(value)} for key, value in STRATEGY_PRESETS.items()]


def get_strategy_preset(strategy_id: str) -> dict[str, Any]:
    try:
        return {"strategy": strategy_id, **dict(STRATEGY_PRESETS[strategy_id])}
    except KeyError as error:
        raise ValueError(f"unknown strategy: {strategy_id}") from error


def infer_strategy(text: str) -> str:
    value = (text or "").lower()
    if any(token in value for token in ("bug", "修复", "报错", "异常", "崩溃")):
        return "debug-investigate"
    if any(token in value for token in ("重构", "refactor", "迁移")):
        return "refactor-safely"
    if any(token in value for token in ("审查", "审核", "audit", "review")):
        return "review-audit"
    if any(token in value for token in ("提交", "合并", "commit", "git")):
        return "git-action"
    return "guided-develop"
