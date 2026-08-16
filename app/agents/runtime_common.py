"""Helpers shared by the model runtime adapters (Claude, Codex, Pi, native).

Two terminal-event helpers exist on purpose: Pi-style
:func:`ensure_terminal_event` only appends when no terminal event exists,
while Claude/Codex-style :func:`normalize_terminal_event` rebuilds exactly one
terminal event of the expected kind (they may stream their own terminal
events with different reasons).
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.agents.contracts import AgentEvent, AgentEventType, AgentResult

UNSANDBOXED_OPT_IN = "WORKLOOP_ALLOW_UNSANDBOXED_EXECUTOR"

_TERMINAL_EVENT_TYPES = {
    AgentEventType.COMPLETED,
    AgentEventType.FAILED,
    AgentEventType.CANCELLED,
}


def unsandboxed_allowed() -> bool:
    return os.environ.get(UNSANDBOXED_OPT_IN, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def parse_structured_output(text: str) -> dict[str, Any]:
    """Parse a model's final text message as one JSON object.

    Tolerates markdown fences and surrounding prose; used by the text-JSON
    runtimes (Pi, native) whose providers have no schema-enforced output.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(line for line in lines if not line.strip().startswith("```")).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型最终答复不是 JSON 对象") from None
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as error:
            raise ValueError(f"模型最终答复不是合法 JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("结构化输出必须是 JSON 对象")
    return value


def append_terminal_event(
    events: list[AgentEvent],
    role: str,
    succeeded: bool,
) -> list[AgentEvent]:
    """Append a COMPLETED/FAILED event when the list has none yet."""
    if any(event.event_type in _TERMINAL_EVENT_TYPES for event in events):
        return events
    events.append(
        AgentEvent(
            AgentEventType.COMPLETED if succeeded else AgentEventType.FAILED,
            role,
            {"reason": "completed" if succeeded else "failed"},
        )
    )
    return events


def ensure_terminal_event(result: AgentResult, role: str) -> AgentResult:
    """Pi-style: leave the events alone once any terminal event exists."""
    if any(event.event_type in _TERMINAL_EVENT_TYPES for event in result.events):
        return result
    result.events.append(
        AgentEvent(
            AgentEventType.COMPLETED if result.succeeded else AgentEventType.FAILED,
            role,
            {"reason": result.error_type or "completed"},
        )
    )
    return result


def normalize_terminal_event(result: AgentResult, role: str) -> AgentResult:
    """Claude/Codex-style: end with exactly one terminal event of the expected kind."""
    if result.succeeded:
        expected = AgentEventType.COMPLETED
    elif result.error_type == "user_cancelled":
        expected = AgentEventType.CANCELLED
    else:
        expected = AgentEventType.FAILED
    matching = [event for event in result.events if event.event_type is expected]
    result.events = [
        event for event in result.events if event.event_type not in _TERMINAL_EVENT_TYPES
    ]
    result.events.append(
        matching[-1]
        if matching
        else AgentEvent(expected, role, {"reason": result.error_type or "completed"})
    )
    return result
