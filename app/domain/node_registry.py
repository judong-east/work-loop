from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .models import NodeDefinition


NodeHandler = Callable[[dict[str, Any]], dict[str, Any]]


def built_in_nodes() -> list[NodeDefinition]:
    return [
        NodeDefinition(
            "requirement", "需求梳理", "将自然语言需求整理成可执行的结构化事实。",
            ("request", "project"), ("understanding", "acceptance_criteria", "open_questions"),
            ("planning", "general"), builtin=True,
        ),
        NodeDefinition(
            "planning", "计划制定", "把需求拆成有依赖关系的执行计划。",
            ("understanding", "acceptance_criteria"), ("steps", "risks", "artifacts"),
            ("planning", "reasoning"), builtin=True,
        ),
        NodeDefinition(
            "implementation", "项目执行", "依据共享上下文执行代码或内容变更。",
            ("steps", "artifacts"), ("changes", "artifacts", "decisions"),
            ("implementation", "coding"), builtin=True,
        ),
        NodeDefinition(
            "review", "代码审核", "对上游输出和验收条件进行独立审核。",
            ("changes", "acceptance_criteria"), ("verdict", "issues", "decisions"),
            ("review", "critical"), builtin=True,
        ),
        NodeDefinition(
            "testing", "纠错测试", "生成或运行边界条件测试并回写风险。",
            ("changes", "steps"), ("checks", "risks", "decisions"),
            ("testing", "general"), builtin=True,
        ),
        NodeDefinition(
            "tool", "工具节点", "由配置绑定外部工具或确定性函数。",
            (), ("result",), ("general",), builtin=True,
        ),
    ]


class NodeRegistry:
    """Registry for built-in and user-defined node contracts.

    Configuration is JSON first.  A deliberately small YAML reader is included
    for the common list/object syntax so custom nodes remain dependency-free.
    Executable handlers are registered in Python and never loaded from config.
    """

    def __init__(self, definitions: list[NodeDefinition] | None = None):
        self._definitions: dict[str, NodeDefinition] = {}
        self._handlers: dict[str, NodeHandler] = {}
        for definition in definitions or built_in_nodes():
            self.register(definition)

    def register(self, definition: NodeDefinition, handler: NodeHandler | None = None) -> None:
        if not definition.node_type.replace("-", "").replace("_", "").isalnum():
            raise ValueError("node_type must be a safe identifier")
        if definition.node_type in self._definitions and self._definitions[definition.node_type].builtin:
            raise ValueError(f"cannot replace built-in node: {definition.node_type}")
        self._definitions[definition.node_type] = definition
        if handler is not None:
            self._handlers[definition.node_type] = handler

    def register_handler(self, node_type: str, handler: NodeHandler) -> None:
        if node_type not in self._definitions:
            raise KeyError(node_type)
        self._handlers[node_type] = handler

    def get(self, node_type: str) -> NodeDefinition:
        try:
            return self._definitions[node_type]
        except KeyError as error:
            raise KeyError(f"unknown node type: {node_type}") from error

    def handler(self, node_type: str) -> NodeHandler | None:
        return self._handlers.get(node_type)

    def list(self) -> list[NodeDefinition]:
        return list(self._definitions.values())

    def unregister(self, node_type: str) -> None:
        definition = self.get(node_type)
        if definition.builtin:
            raise ValueError(f"cannot delete built-in node: {node_type}")
        del self._definitions[node_type]
        self._handlers.pop(node_type, None)

    def __contains__(self, node_type: str) -> bool:
        return node_type in self._definitions

    def load_file(self, path: Path) -> list[NodeDefinition]:
        raw = _load_config(path)
        items = raw.get("nodes", raw) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ValueError("node config must contain a nodes list")
        loaded: list[NodeDefinition] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("each node definition must be an object")
            definition = NodeDefinition(
                node_type=str(item["node_type"]),
                label=str(item.get("label", item["node_type"])),
                description=str(item.get("description", "")),
                input_fields=tuple(str(x) for x in item.get("input_fields", [])),
                output_fields=tuple(str(x) for x in item.get("output_fields", [])),
                capabilities=tuple(str(x) for x in item.get("capabilities", ["general"])),
                default_model=str(item.get("default_model", "")),
                builtin=False,
            )
            self.register(definition)
            loaded.append(definition)
        return loaded


def _load_config(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _simple_yaml(text)


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "Null", "~"}:
        return None
    if (value.startswith("[") and value.endswith("]")) or (value.startswith("{") and value.endswith("}")):
        try:
            return json.loads(value.replace("'", '"'))
        except json.JSONDecodeError:
            pass
    if (value.startswith("\"") and value.endswith("\"")) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def _simple_yaml(text: str) -> Any:
    """Parse the small YAML subset used by node catalogs.

    It supports a top-level ``nodes:`` list with scalar fields and inline JSON
    arrays.  Invalid or more complex YAML fails clearly instead of silently
    executing an unsafe configuration.
    """
    result: dict[str, Any] = {}
    current: dict[str, Any] | None = None
    list_key = ""
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith("-"):
            key = stripped[:-1].strip()
            result[key] = []
            list_key = key
            current = None
            continue
        if stripped.startswith("-"):
            if not list_key:
                raise ValueError("YAML list has no parent key")
            if current is None or ":" in stripped[1:]:
                current = {}
                result.setdefault(list_key, []).append(current)
                rest = stripped[1:].strip()
                if rest:
                    key, sep, value = rest.partition(":")
                    if not sep:
                        raise ValueError("YAML list item must be an object")
                    current[key.strip()] = _scalar(value)
            else:
                result[list_key].append(_scalar(stripped[1:]))
            continue
        if current is None:
            raise ValueError("unsupported YAML structure")
        key, sep, value = stripped.partition(":")
        if not sep:
            raise ValueError("YAML field must contain ':'")
        current[key.strip()] = _scalar(value)
    return result
