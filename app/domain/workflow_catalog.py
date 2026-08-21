from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from app.core.atomic_files import write_json_atomic

from .models import WorkflowDefinition, WorkflowNode
from .node_registry import NodeRegistry


class WorkflowCatalog:
    """Durable catalog for user-composable DAG definitions."""

    def __init__(self, path: Path, registry: NodeRegistry):
        self.path = Path(path)
        self.registry = registry
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[WorkflowDefinition]:
        raw = self._read()
        return [self._from_dict(item) for item in raw.get("workflows", [])]

    def get(self, workflow_id: str) -> WorkflowDefinition:
        for workflow in self.list():
            if workflow.workflow_id == workflow_id:
                return workflow
        raise KeyError(workflow_id)

    def save(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        with self._lock:
            workflow.validate(self.registry)
            self.validate_dag(workflow)
            raw = self._read()
            workflows = [item for item in raw.get("workflows", []) if item.get("workflow_id") != workflow.workflow_id]
            workflows.append(self._to_dict(workflow))
            write_json_atomic(self.path, {"schema_version": 1, "workflows": workflows})
            return workflow

    def delete(self, workflow_id: str) -> None:
        with self._lock:
            raw = self._read()
            workflows = [item for item in raw.get("workflows", []) if item.get("workflow_id") != workflow_id]
            write_json_atomic(self.path, {"schema_version": 1, "workflows": workflows})

    def validate_dag(self, workflow: WorkflowDefinition) -> list[str]:
        ids = {node.node_id for node in workflow.nodes}
        indegree = {node.node_id: 0 for node in workflow.nodes}
        outgoing = {node.node_id: [] for node in workflow.nodes}
        for node in workflow.nodes:
            for dependency in node.depends_on:
                if dependency not in ids:
                    raise ValueError(f"unknown dependency: {dependency}")
                indegree[node.node_id] += 1
                outgoing[dependency].append(node.node_id)
        queue = [node_id for node_id, count in indegree.items() if count == 0]
        order: list[str] = []
        while queue:
            node_id = queue.pop(0)
            order.append(node_id)
            for child in outgoing[node_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(order) != len(ids):
            raise ValueError("workflow dependencies must form a DAG")
        return order

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": 1, "workflows": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read workflow catalog: {self.path}") from error
        return value if isinstance(value, dict) else {"workflows": []}

    @staticmethod
    def _to_dict(workflow: WorkflowDefinition) -> dict[str, Any]:
        return {
            "workflow_id": workflow.workflow_id,
            "label": workflow.label,
            "description": workflow.description,
            "builtin": workflow.builtin,
            "schema_version": workflow.schema_version,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "node_type": node.node_type,
                    "depends_on": list(node.depends_on),
                    "model_alias": node.model_alias,
                    "prompt_template": node.prompt_template,
                    "on_failure": node.on_failure,
                    "config": dict(node.config),
                    "position": list(node.position),
                }
                for node in workflow.nodes
            ],
        }

    @staticmethod
    def _from_dict(value: dict[str, Any]) -> WorkflowDefinition:
        return WorkflowDefinition(
            workflow_id=str(value["workflow_id"]),
            label=str(value.get("label", value["workflow_id"])),
            description=str(value.get("description", "")),
            builtin=bool(value.get("builtin", False)),
            schema_version=int(value.get("schema_version", 1)),
            nodes=[
                WorkflowNode(
                    node_id=str(node["node_id"]),
                    node_type=str(node["node_type"]),
                    depends_on=tuple(str(item) for item in node.get("depends_on", [])),
                    model_alias=str(node.get("model_alias", "")),
                    prompt_template=str(node.get("prompt_template", "")),
                    on_failure=str(node.get("on_failure", "human")),
                    config=dict(node.get("config", {})),
                    position=tuple(float(item) for item in node.get("position", [0, 0]))[:2],
                )
                for node in value.get("nodes", [])
            ],
        )
