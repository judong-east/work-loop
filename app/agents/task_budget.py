"""Per-task budget accounting shared by the workflow stages.

``agent_budget`` derives the per-call budget from what the task has already
consumed; ``task_budget_error`` / ``task_budget_overrun`` classify budget
exhaustion (>= for pausing before the next call, > for rejecting a finished
call); ``usage_tokens`` normalizes provider usage dicts onto the
input/output/cached token names Workloop records.
"""

from __future__ import annotations

from typing import Any

from app.agents.contracts import AgentBudget, AgentTask


def agent_budget(task: AgentTask) -> AgentBudget:
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


def task_budget_error(task: AgentTask) -> str:
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


def task_budget_overrun(task: AgentTask) -> str:
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


def usage_tokens(usage: dict[str, Any]) -> tuple[int, int, int, int]:
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
