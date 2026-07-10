from __future__ import annotations

import json
import re
from typing import Any


_SENSITIVE_PREFIX = re.compile(
    r'''(?ix)["']?(?:api[_-]?key|password|secret|token)["']?\s*[:=]\s*'''
)
_BEARER_TOKEN = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
_OPENAI_STYLE_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
def redact(text: str, project_patterns: list[str] | None = None) -> str:
    redacted = _redact_json_lines(text)
    redacted = _redact_sensitive_assignments(redacted)
    redacted = _BEARER_TOKEN.sub(r"\1[REDACTED]", redacted)
    redacted = _OPENAI_STYLE_TOKEN.sub("[REDACTED]", redacted)
    for pattern in project_patterns or []:
        redacted = _redact_project_pattern(redacted, pattern)
    return redacted


def redact_value(value: Any, project_patterns: list[str] | None = None) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if _is_sensitive_key(str(key))
                else redact_value(item, project_patterns)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, project_patterns) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, project_patterns) for item in value]
    if isinstance(value, str):
        return redact(value, project_patterns)
    return value


def _is_sensitive_key(key: str) -> bool:
    expanded = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", key)
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", expanded)
    words = [word for word in re.split(r"[^a-z0-9]+", expanded.lower()) if word]
    if any(word in {"password", "secret", "token", "apikey"} for word in words):
        return True
    return any(left == "api" and right == "key" for left, right in zip(words, words[1:]))


def _redact_sensitive_assignments(text: str) -> str:
    output: list[str] = []
    position = 0
    while match := _SENSITIVE_PREFIX.search(text, position):
        output.append(text[position : match.end()])
        value_start = match.end()
        if value_start >= len(text):
            output.append("[REDACTED]")
            position = value_start
            break

        quote = text[value_start] if text[value_start] in {'"', "'"} else ""
        if quote:
            index = value_start + 1
            escaped = False
            while index < len(text):
                character = text[index]
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    break
                index += 1
            output.append(quote + "[REDACTED]")
            if index < len(text) and text[index] == quote:
                output.append(quote)
                index += 1
            position = index
            continue

        index = value_start
        while index < len(text) and text[index] not in " \t\r\n,;}":
            index += 1
        output.append("[REDACTED]")
        position = index
    output.append(text[position:])
    return "".join(output)


def _redact_json_lines(text: str) -> str:
    output: list[str] = []
    for segment in text.splitlines(keepends=True):
        body = segment.rstrip("\r\n")
        ending = segment[len(body) :]
        stripped = body.strip()
        try:
            value = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            output.append(segment)
            continue
        if not isinstance(value, (dict, list)):
            output.append(segment)
            continue
        prefix = body[: len(body) - len(body.lstrip())]
        output.append(
            prefix
            + json.dumps(redact_value(value), ensure_ascii=False, separators=(",", ":"))
            + ending
        )
    return "".join(output)


def _redact_project_pattern(text: str, pattern: str) -> str:
    if not pattern.endswith("*"):
        return text.replace(pattern, "[REDACTED]")
    prefix = pattern[:-1]
    if not prefix:
        return "\n".join("[REDACTED]" for _ in text.split("\n"))
    lines: list[str] = []
    for line in text.split("\n"):
        index = line.find(prefix)
        lines.append(line if index < 0 else line[:index] + "[REDACTED]")
    return "\n".join(lines)
