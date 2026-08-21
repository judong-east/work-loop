from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from app.core.atomic_files import write_json_atomic

from .models import NodeDefinition
from .node_registry import NodeRegistry


class NodeCatalog:
    """Durable catalog for declarative user-defined node contracts."""

    def __init__(self, path: Path, registry: NodeRegistry):
        self.path = Path(path)
        self.registry = registry
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for definition in self.list():
            self.registry.register(definition)

    def list(self) -> list[NodeDefinition]:
        return [self._from_dict(item) for item in self._read().get("nodes", [])]

    def save(self, definition: NodeDefinition) -> NodeDefinition:
        if definition.builtin:
            raise ValueError("custom node cannot be marked as built-in")
        with self._lock:
            self.registry.register(definition)
            items = [item for item in self.list() if item.node_type != definition.node_type]
            items.append(definition)
            write_json_atomic(
                self.path,
                {"schema_version": 1, "nodes": [self._to_dict(item) for item in items]},
            )
        return definition

    def delete(self, node_type: str) -> None:
        with self._lock:
            definition = self.registry.get(node_type)
            if definition.builtin:
                raise ValueError(f"cannot delete built-in node: {node_type}")
            items = [item for item in self.list() if item.node_type != node_type]
            if len(items) == len(self.list()):
                raise KeyError(node_type)
            write_json_atomic(
                self.path,
                {"schema_version": 1, "nodes": [self._to_dict(item) for item in items]},
            )
            self.registry.unregister(node_type)

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": 1, "nodes": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read node catalog: {self.path}") from error
        return value if isinstance(value, dict) else {"nodes": []}

    @staticmethod
    def _to_dict(definition: NodeDefinition) -> dict[str, Any]:
        return {
            "node_type": definition.node_type,
            "label": definition.label,
            "description": definition.description,
            "input_fields": list(definition.input_fields),
            "output_fields": list(definition.output_fields),
            "capabilities": list(definition.capabilities),
            "default_model": definition.default_model,
            "builtin": False,
        }

    @staticmethod
    def _from_dict(value: dict[str, Any]) -> NodeDefinition:
        return NodeDefinition(
            node_type=str(value["node_type"]),
            label=str(value.get("label", value["node_type"])),
            description=str(value.get("description", "")),
            input_fields=tuple(str(item) for item in value.get("input_fields", [])),
            output_fields=tuple(str(item) for item in value.get("output_fields", [])),
            capabilities=tuple(str(item) for item in value.get("capabilities", ["general"])),
            default_model=str(value.get("default_model", "")),
            builtin=False,
        )
