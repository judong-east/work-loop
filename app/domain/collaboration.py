from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from app.core.contracts import new_id, utc_now


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkspaceAccess(str, Enum):
    READ = "read"
    WRITE = "write"
    VALIDATE = "validate"


def _safe_id(value: str, label: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise ValueError(f"{label} must be a safe identifier")
    return value


@dataclass
class RoleProfile:
    role_id: str
    label: str
    node_type: str
    instructions: str = ""
    model_alias: str = ""
    capabilities: tuple[str, ...] = ("general",)
    workspace_access: WorkspaceAccess = WorkspaceAccess.READ
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    schema_version: int = 1

    def validate(self) -> None:
        self.role_id = _safe_id(self.role_id, "role_id")
        self.node_type = _safe_id(self.node_type, "node_type")
        if not self.label.strip():
            raise ValueError("role label is required")
        if len(self.instructions) > 50_000:
            raise ValueError("role instructions are too long")
        if not self.capabilities or not all(isinstance(item, str) and item for item in self.capabilities):
            raise ValueError("role capabilities must contain non-empty strings")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "role_id": self.role_id,
            "label": self.label,
            "node_type": self.node_type,
            "instructions": self.instructions,
            "model_alias": self.model_alias,
            "capabilities": list(self.capabilities),
            "workspace_access": self.workspace_access.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RoleProfile":
        capabilities = value.get("capabilities", ["general"])
        if not isinstance(capabilities, (list, tuple)):
            raise ValueError("role capabilities must be a list")
        role = cls(
            role_id=str(value["role_id"]),
            label=str(value.get("label", value["role_id"])),
            node_type=str(value.get("node_type", "tool")),
            instructions=str(value.get("instructions", "")),
            model_alias=str(value.get("model_alias", "")),
            capabilities=tuple(str(item) for item in capabilities),
            workspace_access=WorkspaceAccess(str(value.get("workspace_access", "read"))),
            created_at=str(value.get("created_at", utc_now())),
            updated_at=str(value.get("updated_at", value.get("created_at", utc_now()))),
            schema_version=int(value.get("schema_version", 1)),
        )
        role.validate()
        return role


@dataclass
class CollaborationTask:
    task_id: str
    project_id: str
    title: str
    description: str
    role_id: str
    depends_on: tuple[str, ...] = ()
    priority: int = 50
    goal_id: str = ""
    model_alias: str = ""
    status: TaskStatus = TaskStatus.PENDING
    session_id: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    schema_version: int = 1

    def validate(self) -> None:
        self.task_id = _safe_id(self.task_id, "task_id")
        self.project_id = _safe_id(self.project_id, "project_id")
        self.role_id = _safe_id(self.role_id, "role_id")
        if not self.title.strip() or not self.description.strip():
            raise ValueError("task title and description are required")
        if len(self.title) > 200 or len(self.description) > 50_000:
            raise ValueError("task content is too long")
        if self.task_id in self.depends_on or len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("task dependencies must be unique and cannot reference itself")
        for dependency in self.depends_on:
            _safe_id(dependency, "dependency task_id")
        if self.goal_id:
            _safe_id(self.goal_id, "goal_id")
        if self.model_alias:
            _safe_id(self.model_alias, "model_alias")
        if not 0 <= self.priority <= 100:
            raise ValueError("task priority must be between 0 and 100")
        if len(json.dumps(self.result, ensure_ascii=False)) > 250_000:
            raise ValueError("task result is too large")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "title": self.title,
            "description": self.description,
            "role_id": self.role_id,
            "depends_on": list(self.depends_on),
            "priority": self.priority,
            "goal_id": self.goal_id,
            "model_alias": self.model_alias,
            "status": self.status.value,
            "session_id": self.session_id,
            "result": dict(self.result),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def create(
        cls,
        project_id: str,
        title: str,
        description: str,
        role_id: str,
        *,
        depends_on: Iterable[str] = (),
        priority: int = 50,
        goal_id: str = "",
        model_alias: str = "",
    ) -> "CollaborationTask":
        task = cls(
            task_id=new_id("TASK"),
            project_id=project_id,
            title=title.strip(),
            description=description.strip(),
            role_id=role_id,
            depends_on=tuple(depends_on),
            priority=priority,
            goal_id=goal_id,
            model_alias=model_alias,
        )
        task.validate()
        return task

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CollaborationTask":
        dependencies = value.get("depends_on", [])
        if not isinstance(dependencies, (list, tuple)):
            raise ValueError("task dependencies must be a list")
        result = value.get("result", {})
        if not isinstance(result, dict):
            raise ValueError("task result must be an object")
        task = cls(
            task_id=str(value["task_id"]),
            project_id=str(value["project_id"]),
            title=str(value["title"]),
            description=str(value.get("description", value["title"])),
            role_id=str(value["role_id"]),
            depends_on=tuple(str(item) for item in dependencies),
            priority=int(value.get("priority", 50)),
            goal_id=str(value.get("goal_id", "")),
            model_alias=str(value.get("model_alias", "")),
            status=TaskStatus(str(value.get("status", "pending"))),
            session_id=str(value.get("session_id", "")),
            result=dict(result),
            error=str(value.get("error", "")),
            created_at=str(value.get("created_at", utc_now())),
            updated_at=str(value.get("updated_at", value.get("created_at", utc_now()))),
            schema_version=int(value.get("schema_version", 1)),
        )
        task.validate()
        return task


@dataclass
class Handoff:
    message_id: str
    project_id: str
    from_task_id: str
    to_task_id: str
    content: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    schema_version: int = 1

    def validate(self) -> None:
        self.message_id = _safe_id(self.message_id, "message_id")
        self.project_id = _safe_id(self.project_id, "project_id")
        self.from_task_id = _safe_id(self.from_task_id, "from_task_id")
        self.to_task_id = _safe_id(self.to_task_id, "to_task_id")
        if not self.content.strip() or len(self.content) > 10_000:
            raise ValueError("handoff content is required and must be concise")
        if len(json.dumps(self.payload, ensure_ascii=False)) > 250_000:
            raise ValueError("handoff payload is too large")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "message_id": self.message_id,
            "project_id": self.project_id,
            "from_task_id": self.from_task_id,
            "to_task_id": self.to_task_id,
            "content": self.content,
            "payload": dict(self.payload),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def create(cls, source: CollaborationTask, target: CollaborationTask) -> "Handoff":
        if source.project_id != target.project_id:
            raise ValueError("handoff tasks must belong to the same project")
        handoff = cls(
            message_id=new_id("HANDOFF"),
            project_id=source.project_id,
            from_task_id=source.task_id,
            to_task_id=target.task_id,
            content=f"{source.title} 已完成，结果交接给 {target.title}。",
            payload=dict(source.result),
        )
        handoff.validate()
        return handoff

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Handoff":
        payload = value.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("handoff payload must be an object")
        handoff = cls(
            message_id=str(value["message_id"]),
            project_id=str(value["project_id"]),
            from_task_id=str(value["from_task_id"]),
            to_task_id=str(value["to_task_id"]),
            content=str(value["content"]),
            payload=dict(payload),
            created_at=str(value.get("created_at", utc_now())),
            schema_version=int(value.get("schema_version", 1)),
        )
        handoff.validate()
        return handoff


@dataclass
class Goal:
    """A large goal that decomposition splits into role-owned subtasks."""

    goal_id: str
    project_id: str
    goal: str
    summary: str = ""
    task_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    schema_version: int = 1

    def validate(self) -> None:
        self.goal_id = _safe_id(self.goal_id, "goal_id")
        self.project_id = _safe_id(self.project_id, "project_id")
        if not self.goal.strip():
            raise ValueError("goal text is required")
        if len(self.goal) > 50_000:
            raise ValueError("goal text is too long")
        if len(self.summary) > 2_000:
            raise ValueError("goal summary is too long")
        if not isinstance(self.task_ids, list) or not all(isinstance(item, str) and item for item in self.task_ids):
            raise ValueError("goal task_ids must be a list of task ids")
        for task_id in self.task_ids:
            _safe_id(task_id, "goal task_id")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "goal_id": self.goal_id,
            "project_id": self.project_id,
            "goal": self.goal,
            "summary": self.summary,
            "task_ids": list(self.task_ids),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def create(cls, project_id: str, goal: str, *, summary: str = "") -> "Goal":
        record = cls(goal_id=new_id("GOAL"), project_id=project_id, goal=goal.strip(), summary=summary.strip())
        record.validate()
        return record

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Goal":
        record = cls(
            goal_id=str(value["goal_id"]),
            project_id=str(value["project_id"]),
            goal=str(value["goal"]),
            summary=str(value.get("summary", "")),
            task_ids=[str(item) for item in value.get("task_ids", [])],
            created_at=str(value.get("created_at", utc_now())),
            schema_version=int(value.get("schema_version", 1)),
        )
        record.validate()
        return record


@dataclass(frozen=True)
class TaskOutcome:
    status: TaskStatus
    session_id: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class TaskGraph:
    @staticmethod
    def validate(tasks: Iterable[CollaborationTask]) -> None:
        values = list(tasks)
        for task in values:
            task.validate()
        if len({task.project_id for task in values}) > 1:
            raise ValueError("all tasks in a graph must belong to one project")
        ids = {task.task_id for task in values}
        if len(ids) != len(values):
            raise ValueError("task ids must be unique")
        for task in values:
            missing = set(task.depends_on) - ids
            if missing:
                raise ValueError(f"task {task.task_id} has unknown dependencies: {sorted(missing)}")

        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {task.task_id: task for task in values}

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task dependencies must form a DAG")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in by_id[task_id].depends_on:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in ids:
            visit(task_id)

    @staticmethod
    def ready(tasks: Iterable[CollaborationTask]) -> list[CollaborationTask]:
        values = list(tasks)
        completed = {task.task_id for task in values if task.status is TaskStatus.COMPLETED}
        return sorted(
            (
                task for task in values
                if task.status is TaskStatus.PENDING and set(task.depends_on) <= completed
            ),
            key=lambda task: (-task.priority, task.created_at, task.task_id),
        )
