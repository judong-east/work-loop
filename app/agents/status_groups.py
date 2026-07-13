from __future__ import annotations

from app.agents.contracts import AgentTaskStatus


TASK_STATUS_GROUPS: dict[str, frozenset[AgentTaskStatus]] = {
    "running": frozenset(
        {
            AgentTaskStatus.QUEUED_FOR_ANALYSIS,
            AgentTaskStatus.QUEUED_FOR_EXECUTION,
            AgentTaskStatus.QUEUED_FOR_RECOVERY,
            AgentTaskStatus.ANALYZING,
            AgentTaskStatus.EXECUTING,
            AgentTaskStatus.VALIDATING,
            AgentTaskStatus.REVIEWING,
            AgentTaskStatus.REPLANNING,
            AgentTaskStatus.INTEGRATING,
        }
    ),
    "waiting_for_human": frozenset(
        {
            AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            AgentTaskStatus.INTERRUPTED,
            AgentTaskStatus.PAUSED,
            AgentTaskStatus.INTEGRATION_REQUIRED,
        }
    ),
    "failed": frozenset({AgentTaskStatus.FAILED}),
    "blocked": frozenset({AgentTaskStatus.BLOCKED}),
    "ready_to_deliver": frozenset({AgentTaskStatus.READY_TO_DELIVER}),
}

TASK_GROUP_PRIORITY = {
    "running": 0,
    "waiting_for_human": 1,
    "failed": 2,
    "blocked": 3,
    "ready_to_deliver": 4,
    "other": 5,
}


def task_status_group(status: AgentTaskStatus) -> str:
    for group, statuses in TASK_STATUS_GROUPS.items():
        if status in statuses:
            return group
    return "other"


def task_status_priority(status: AgentTaskStatus) -> int:
    return TASK_GROUP_PRIORITY[task_status_group(status)]
