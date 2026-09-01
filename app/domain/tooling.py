"""Small provider-neutral tool contracts used by the model invocation layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def for_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def for_claude(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


ToolExecutor = Callable[[str, dict[str, Any]], dict[str, Any]]


SEARCH_TOOLS: dict[str, ToolSpec] = {
    "zvec_grep_search": ToolSpec(
        "zvec_grep_search",
        "Search the local indexed workspace semantically or with ranked lexical retrieval.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "fts": {"type": "array", "items": {"type": "string"}},
                "globs": {"type": "array", "items": {"type": "string"}},
                "file_types": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "freshness": {"type": "string", "enum": ["eventual", "wait_for_fresh"]},
                "fuse": {"type": "boolean"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    "zvec_grep_rg": ToolSpec(
        "zvec_grep_rg",
        "Search the local workspace for exact text or regular expressions.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "globs": {"type": "array", "items": {"type": "string"}},
                "file_types": {"type": "array", "items": {"type": "string"}},
                "literal": {"type": "boolean"},
                "ignore_case": {"type": "boolean"},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 20},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    ),
}
