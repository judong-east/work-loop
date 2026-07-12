from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.agents.contracts import (
    AgentEvent,
    AgentEventType,
    AgentResult,
    ExecutionPlan,
    ReviewResult,
)


PLANNER_ROLE = "planner"
REVIEWER_ROLE = "reviewer"


def execution_plan_schema() -> dict[str, Any]:
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "requirement_understanding",
            "non_goals",
            "files_and_symbols",
            "steps",
            "constraints",
            "acceptance_criteria",
            "required_tests",
            "risks",
            "open_questions",
        ],
        "properties": {
            "requirement_understanding": {"type": "string"},
            "non_goals": string_array,
            "files_and_symbols": string_array,
            "steps": {**string_array, "minItems": 1},
            "constraints": string_array,
            "acceptance_criteria": {**string_array, "minItems": 1, "uniqueItems": True},
            "required_tests": {**string_array, "minItems": 1},
            "risks": string_array,
            "open_questions": string_array,
        },
    }


def review_result_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "acceptance", "issues", "recommended_tests", "summary"],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["pass", "revise_code", "replan", "blocked"],
            },
            "acceptance": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion", "passed"],
                    "properties": {
                        "criterion": {"type": "string"},
                        "passed": {"type": "boolean"},
                    },
                },
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "file",
                        "line",
                        "severity",
                        "message",
                        "suggestion",
                        "evidence",
                    ],
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer", "minimum": 0},
                        "severity": {
                            "type": "string",
                            "enum": ["info", "warning", "blocker"],
                        },
                        "message": {"type": "string", "minLength": 1},
                        "suggestion": {"type": "string"},
                        "evidence": {"type": "string", "minLength": 1},
                    },
                },
            },
            "recommended_tests": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
        },
    }


def schema_for_role(role: str) -> dict[str, Any]:
    if role == PLANNER_ROLE:
        return execution_plan_schema()
    if role == REVIEWER_ROLE:
        return review_result_schema()
    raise ValueError(f"Claude Code 不支持角色 {role}。")


def validate_structured_output(role: str, output: Any) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise ValueError("结构化结果必须是对象。")
    if role == PLANNER_ROLE:
        expected = set(execution_plan_schema()["required"])
        _require_exact_keys(output, expected, "ExecutionPlan")
        ExecutionPlan.from_dict(output)
    elif role == REVIEWER_ROLE:
        expected = set(review_result_schema()["required"])
        _require_exact_keys(output, expected, "ReviewResult")
        acceptance = output.get("acceptance")
        if isinstance(acceptance, list):
            for index, item in enumerate(acceptance):
                if isinstance(item, dict):
                    _require_exact_keys(
                        item,
                        {"criterion", "passed"},
                        f"ReviewResult.acceptance[{index}]",
                    )
        issues = output.get("issues")
        if isinstance(issues, list):
            for index, item in enumerate(issues):
                if isinstance(item, dict):
                    _require_exact_keys(
                        item,
                        {"file", "line", "severity", "message", "suggestion", "evidence"},
                        f"ReviewResult.issues[{index}]",
                    )
        ReviewResult.from_dict(output)
    else:
        raise ValueError(f"Claude Code 不支持角色 {role}。")
    return dict(output)


def _is_native_planner_output(output: Any) -> bool:
    if not isinstance(output, dict) or not isinstance(output.get("title"), str):
        return False
    tasks = output.get("tasks")
    plan = output.get("plan")
    nested_steps = plan.get("steps") if isinstance(plan, dict) else None
    return isinstance(tasks, list) or isinstance(nested_steps, list) or isinstance(
        output.get("steps"), list
    )


def _normalize_native_reviewer_pass(
    output: Any,
    acceptance_criteria: list[str],
) -> dict[str, Any] | None:
    if (
        not isinstance(output, dict)
        or output.get("approved") is not True
        or not acceptance_criteria
        or not isinstance(output.get("summary"), str)
    ):
        return None
    test_results = output.get("test_results")
    if not isinstance(test_results, list) or any(
        not isinstance(item, dict) or item.get("status") != "passed"
        for item in test_results
    ):
        return None
    return {
        "verdict": "pass",
        "acceptance": [
            {"criterion": criterion, "passed": True}
            for criterion in acceptance_criteria
        ],
        "issues": [],
        "recommended_tests": [],
        "summary": output["summary"],
    }


def _require_exact_keys(output: dict[str, Any], expected: set[str], name: str) -> None:
    actual = set(output)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"缺少字段：{', '.join(missing)}")
        if unexpected:
            details.append(f"未知字段：{', '.join(unexpected)}")
        raise ValueError(f"{name} 字段无效：{'；'.join(details)}")


@dataclass
class ClaudeProtocolState:
    role: str
    acceptance_criteria: list[str] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    session_id: str = ""
    final_message: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    protocol_errors: list[str] = field(default_factory=list)
    provider_error: str = ""
    terminal_count: int = 0
    terminal_succeeded: bool | None = None

    def __post_init__(self) -> None:
        schema_for_role(self.role)

    def consume(self, raw: dict[str, Any]) -> None:
        if not isinstance(raw, dict):
            self.protocol_errors.append("Claude stream-json 事件必须是对象。")
            return
        self.raw_events.append(raw)
        raw_type = raw.get("type")
        if not isinstance(raw_type, str) or not raw_type:
            self.protocol_errors.append("Claude stream-json 事件缺少 type。")
            return
        self._capture_session(raw)

        if raw_type == "system" and raw.get("subtype") == "init":
            if not self.session_id:
                self.protocol_errors.append("Claude init 事件缺少 session_id。")
            self.events.append(
                AgentEvent(AgentEventType.SESSION_STARTED, self.role, dict(raw), raw_type)
            )
            return
        if raw_type == "assistant":
            self._consume_assistant(raw, raw_type)
            return
        if raw_type == "user":
            self._consume_user(raw, raw_type)
            return
        if raw_type == "result":
            self._consume_result(raw, raw_type)
            return

        self.events.append(AgentEvent(AgentEventType.HEARTBEAT, self.role, dict(raw), raw_type))

    def _capture_session(self, raw: dict[str, Any]) -> None:
        session_id = raw.get("session_id")
        if session_id is None:
            return
        if not isinstance(session_id, str) or not session_id:
            self.protocol_errors.append("Claude session_id 必须是非空字符串。")
            return
        if self.session_id and self.session_id != session_id:
            self.protocol_errors.append(
                f"Claude session 不一致：{self.session_id} != {session_id}。"
            )
            return
        self.session_id = session_id

    def _consume_assistant(self, raw: dict[str, Any], raw_type: str) -> None:
        message = raw.get("message")
        if not isinstance(message, dict):
            self.protocol_errors.append("Claude assistant 事件缺少 message 对象。")
            return
        content = message.get("content")
        if not isinstance(content, list):
            self.protocol_errors.append("Claude assistant message.content 必须是数组。")
            return
        emitted = False
        for block in content:
            if not isinstance(block, dict):
                self.protocol_errors.append("Claude content block 必须是对象。")
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                self.final_message = block["text"]
                self.events.append(
                    AgentEvent(AgentEventType.MESSAGE_DELTA, self.role, dict(block), raw_type)
                )
                emitted = True
            elif block_type == "tool_use":
                self.events.append(
                    AgentEvent(AgentEventType.TOOL_STARTED, self.role, dict(block), raw_type)
                )
                emitted = True
        usage = message.get("usage")
        if isinstance(usage, dict):
            self.usage.update(usage)
            self.events.append(
                AgentEvent(AgentEventType.USAGE_UPDATED, self.role, dict(usage), raw_type)
            )
            emitted = True
        if not emitted:
            self.events.append(AgentEvent(AgentEventType.HEARTBEAT, self.role, dict(raw), raw_type))

    def _consume_user(self, raw: dict[str, Any], raw_type: str) -> None:
        message = raw.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        emitted = False
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.events.append(
                        AgentEvent(AgentEventType.TOOL_COMPLETED, self.role, dict(block), raw_type)
                    )
                    emitted = True
        if not emitted:
            self.events.append(AgentEvent(AgentEventType.HEARTBEAT, self.role, dict(raw), raw_type))

    def _consume_result(self, raw: dict[str, Any], raw_type: str) -> None:
        self.terminal_count += 1
        result_text = raw.get("result")
        if isinstance(result_text, str):
            self.final_message = result_text
        usage = raw.get("usage")
        if isinstance(usage, dict):
            self.usage.update(usage)
        total_cost = raw.get("total_cost_usd")
        if isinstance(total_cost, (int, float)) and not isinstance(total_cost, bool):
            self.usage["total_cost_usd"] = float(total_cost)
        if self.usage:
            self.events.append(
                AgentEvent(AgentEventType.USAGE_UPDATED, self.role, dict(self.usage), raw_type)
            )

        succeeded = raw.get("is_error") is False and raw.get("subtype") == "success"
        self.terminal_succeeded = succeeded
        if succeeded:
            structured = raw.get("structured_output")
            if structured is None and isinstance(result_text, str):
                try:
                    text = result_text.strip()
                    if text.startswith("```") and text.endswith("```"):
                        lines = text.splitlines()
                        text = "\n".join(lines[1:-1]).strip()
                    structured = json.loads(text)
                except json.JSONDecodeError as error:
                    self.protocol_errors.append(
                        f"Claude 结构化文本不是合法 JSON：{error}"
                    )
            try:
                self.output = validate_structured_output(self.role, structured)
            except ValueError as error:
                if self.role == "planner" and _is_native_planner_output(structured):
                    self.output = dict(structured)
                elif self.role == "reviewer" and (
                    normalized := _normalize_native_reviewer_pass(
                        structured,
                        self.acceptance_criteria,
                    )
                ) is not None:
                    self.output = normalized
                else:
                    self.protocol_errors.append(f"Claude 结构化结果无效：{error}")
            self.events.append(
                AgentEvent(AgentEventType.COMPLETED, self.role, dict(raw), raw_type)
            )
            return

        error_value = raw.get("errors") or raw.get("error") or result_text or raw.get("subtype")
        if isinstance(error_value, list):
            self.provider_error = "; ".join(str(item) for item in error_value)
        else:
            self.provider_error = str(error_value or "Claude Code 运行失败。")
        self.events.append(AgentEvent(AgentEventType.FAILED, self.role, dict(raw), raw_type))

    def finish(self, return_code: int | None, stderr: str) -> AgentResult:
        if self.protocol_errors:
            return self._terminal_result(
                succeeded=False,
                error="；".join(self.protocol_errors),
                error_type="structured_output_failed",
            )
        if self.terminal_count != 1:
            return self._terminal_result(
                succeeded=False,
                error=f"Claude stream-json 必须恰好包含一个 result，实际为 {self.terminal_count}。",
                error_type="structured_output_failed",
            )
        if not self.session_id:
            return self._terminal_result(
                succeeded=False,
                error="Claude stream-json 缺少 session_id。",
                error_type="structured_output_failed",
            )
        if self.terminal_succeeded is not True:
            return self._terminal_result(
                succeeded=False,
                error=self.provider_error or stderr.strip() or "Claude Code 运行失败。",
                error_type="runtime_failed",
            )
        if return_code != 0:
            return self._terminal_result(
                succeeded=False,
                error=stderr.strip() or f"Claude Code 退出码 {return_code}。",
                error_type="runtime_failed",
            )
        if not self.output:
            return self._terminal_result(
                succeeded=False,
                error="Claude result 缺少结构化结果。",
                error_type="structured_output_failed",
            )
        return self._terminal_result(succeeded=True)

    def _terminal_result(
        self,
        succeeded: bool,
        error: str = "",
        error_type: str = "",
    ) -> AgentResult:
        expected = AgentEventType.COMPLETED if succeeded else AgentEventType.FAILED
        terminals = {
            AgentEventType.COMPLETED,
            AgentEventType.FAILED,
            AgentEventType.CANCELLED,
        }
        matching = [event for event in self.events if event.event_type is expected]
        events = [event for event in self.events if event.event_type not in terminals]
        events.append(
            matching[-1]
            if matching
            else AgentEvent(expected, self.role, {"reason": error_type or "completed"})
        )
        return AgentResult(
            succeeded=succeeded,
            output=dict(self.output) if succeeded else {},
            session_id=self.session_id,
            error=error,
            error_type=error_type,
            final_message=self.final_message,
            events=events,
            raw_events=list(self.raw_events),
            usage=dict(self.usage),
        )
