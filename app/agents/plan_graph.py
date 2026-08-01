from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.agents.contracts import ExecutionPlan
from app.core.contracts import new_id, utc_now


class PlanNodeKind(str, Enum):
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    VALIDATION = "validation"
    REVIEW = "review"
    INTEGRATION = "integration"
    DELIVERY = "delivery"
    CUSTOM = "custom"


class PlanNodeAccess(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


@dataclass(frozen=True)
class ModelBinding:
    provider: str = ""
    model: str = ""
    thinking: str = "medium"

    def validate(self) -> None:
        if self.provider and not re.fullmatch(r"[A-Za-z0-9_.-]+", self.provider):
            raise ValueError("model.provider is invalid")
        if self.thinking not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("model.thinking is invalid")
        if len(self.model) > 200:
            raise ValueError("model.model is too long")

    @classmethod
    def from_dict(cls, data: Any) -> "ModelBinding":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("node.model must be an object")
        binding = cls(
            provider=str(data.get("provider", "")).strip(),
            model=str(data.get("model", "")).strip(),
            thinking=str(data.get("thinking", "medium")).strip() or "medium",
        )
        binding.validate()
        return binding

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "thinking": self.thinking,
        }


@dataclass(frozen=True)
class TerminalTarget:
    kind: str = "local"
    cwd: str = ""
    worktree: str = "task"
    network: str = "deny"

    def validate(self) -> None:
        if self.kind not in {"local", "wsl", "ssh", "container"}:
            raise ValueError("terminal.kind is invalid")
        if self.network not in {"deny", "allow"}:
            raise ValueError("terminal.network is invalid")
        if len(self.cwd) > 1000 or len(self.worktree) > 200:
            raise ValueError("terminal path is too long")

    @classmethod
    def from_dict(cls, data: Any) -> "TerminalTarget":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("node.terminal must be an object")
        target = cls(
            kind=str(data.get("kind", "local")).strip() or "local",
            cwd=str(data.get("cwd", "")).strip(),
            worktree=str(data.get("worktree", "task")).strip() or "task",
            network=str(data.get("network", "deny")).strip() or "deny",
        )
        target.validate()
        return target

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "cwd": self.cwd,
            "worktree": self.worktree,
            "network": self.network,
        }


@dataclass(frozen=True)
class PlanNode:
    node_id: str
    title: str
    kind: PlanNodeKind
    depends_on: list[str] = field(default_factory=list)
    instructions: str = ""
    model: ModelBinding = field(default_factory=ModelBinding)
    terminal: TerminalTarget = field(default_factory=TerminalTarget)
    access: PlanNodeAccess = PlanNodeAccess.READ_ONLY
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    on_failure: str = "human"
    enabled: bool = True

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.node_id):
            raise ValueError(f"invalid plan node id: {self.node_id}")
        if not self.title.strip() or len(self.title) > 160:
            raise ValueError("plan node title must be non-empty and <= 160 characters")
        if len(self.instructions) > 8000:
            raise ValueError("plan node instructions are too long")
        if self.on_failure not in {"retry", "human", "skip", "replan"}:
            raise ValueError("plan node on_failure is invalid")
        self.model.validate()
        self.terminal.validate()
        if self.kind in {PlanNodeKind.IMPLEMENTATION, PlanNodeKind.INTEGRATION}:
            if self.access is not PlanNodeAccess.WORKSPACE_WRITE:
                raise ValueError(f"write node must use workspace_write: {self.node_id}")
        elif self.access is not PlanNodeAccess.READ_ONLY:
            raise ValueError(f"read-only node cannot request workspace write: {self.node_id}")

    @classmethod
    def from_dict(cls, data: Any) -> "PlanNode":
        if not isinstance(data, dict):
            raise ValueError("plan node must be an object")
        try:
            kind = PlanNodeKind(str(data.get("kind", "custom")))
            access = PlanNodeAccess(str(data.get("access", "read_only")))
        except ValueError as error:
            raise ValueError("plan node kind/access is invalid") from error
        node = cls(
            node_id=str(data.get("node_id", data.get("id", ""))).strip(),
            title=str(data.get("title", data.get("label", ""))).strip(),
            kind=kind,
            depends_on=[str(item) for item in data.get("depends_on", [])],
            instructions=str(data.get("instructions", "")),
            model=ModelBinding.from_dict(data.get("model")),
            terminal=TerminalTarget.from_dict(data.get("terminal")),
            access=access,
            inputs=[str(item) for item in data.get("inputs", [])],
            outputs=[str(item) for item in data.get("outputs", [])],
            on_failure=str(data.get("on_failure", "human")),
            enabled=bool(data.get("enabled", True)),
        )
        node.validate()
        return node

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "kind": self.kind.value,
            "depends_on": list(self.depends_on),
            "instructions": self.instructions,
            "model": self.model.to_dict(),
            "terminal": self.terminal.to_dict(),
            "access": self.access.value,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "on_failure": self.on_failure,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class PlanGraph:
    requirement_summary: str
    nodes: list[PlanNode]
    graph_id: str = field(default_factory=lambda: new_id("GRAPH"))
    version: int = 1
    status: str = "draft"
    created_at: str = field(default_factory=utc_now)
    approved_at: str = ""

    def validate(self) -> None:
        if not self.requirement_summary.strip():
            raise ValueError("plan graph requirement_summary cannot be empty")
        if not self.nodes:
            raise ValueError("plan graph must contain at least one node")
        by_id: dict[str, PlanNode] = {}
        for node in self.nodes:
            node.validate()
            if node.node_id in by_id:
                raise ValueError(f"duplicate plan node: {node.node_id}")
            by_id[node.node_id] = node
        for node in self.nodes:
            missing = [item for item in node.depends_on if item not in by_id]
            if missing:
                raise ValueError(f"node {node.node_id} depends on missing nodes: {missing}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("plan graph contains a dependency cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in by_id[node_id].depends_on:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in by_id:
            visit(node_id)

    @classmethod
    def from_dict(cls, data: Any) -> "PlanGraph":
        if not isinstance(data, dict):
            raise ValueError("plan graph must be an object")
        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, list):
            raise ValueError("plan graph nodes must be an array")
        graph = cls(
            requirement_summary=str(data.get("requirement_summary", "")),
            nodes=[PlanNode.from_dict(item) for item in raw_nodes],
            graph_id=str(data.get("graph_id", new_id("GRAPH"))),
            version=int(data.get("version", 1)),
            status=str(data.get("status", "draft")),
            created_at=str(data.get("created_at", utc_now())),
            approved_at=str(data.get("approved_at", "")),
        )
        graph.validate()
        if graph.status not in {"draft", "approved", "running", "completed", "blocked"}:
            raise ValueError("plan graph status is invalid")
        return graph

    @classmethod
    def from_execution_plan(cls, plan: ExecutionPlan) -> "PlanGraph":
        nodes: list[PlanNode] = [
            PlanNode(
                node_id="planning",
                title="Approved execution plan",
                kind=PlanNodeKind.PLANNING,
                instructions=plan.requirement_understanding,
                access=PlanNodeAccess.READ_ONLY,
                outputs=["execution-plan"],
            )
        ]
        previous = "planning"
        for index, step in enumerate(plan.steps, start=1):
            node_id = f"step-{index}"
            nodes.append(
                PlanNode(
                    node_id=node_id,
                    title=step,
                    kind=PlanNodeKind.IMPLEMENTATION,
                    depends_on=[previous],
                    instructions=step,
                    access=PlanNodeAccess.WORKSPACE_WRITE,
                    inputs=["execution-plan"],
                    outputs=[f"step-{index}-changes"],
                )
            )
            previous = node_id
        nodes.append(
            PlanNode(
                node_id="review",
                title="Review completed implementation",
                kind=PlanNodeKind.REVIEW,
                depends_on=[previous],
                instructions="Review the implementation against the acceptance criteria.",
                access=PlanNodeAccess.READ_ONLY,
                inputs=["execution-plan", "workspace-diff"],
                outputs=["review-result"],
            )
        )
        graph = cls(requirement_summary=plan.requirement_understanding, nodes=nodes)
        graph.validate()
        return graph

    def ready(self, completed: set[str], failed: set[str] | None = None) -> list[PlanNode]:
        failed = failed or set()
        return [
            node
            for node in self.nodes
            if node.enabled
            and node.node_id not in completed
            and node.node_id not in failed
            and all(dependency in completed for dependency in node.depends_on)
        ]

    def node(self, node_id: str) -> PlanNode | None:
        """Return the node with ``node_id`` or ``None`` if it is not in the graph."""
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None

    def implementation_nodes(self) -> list[PlanNode]:
        """Enabled nodes whose execution mutates the workspace."""
        return [
            node
            for node in self.nodes
            if node.enabled
            and node.kind in {PlanNodeKind.IMPLEMENTATION, PlanNodeKind.INTEGRATION}
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "version": self.version,
            "status": self.status,
            "requirement_summary": self.requirement_summary,
            "nodes": [node.to_dict() for node in self.nodes],
            "created_at": self.created_at,
            "approved_at": self.approved_at,
        }
