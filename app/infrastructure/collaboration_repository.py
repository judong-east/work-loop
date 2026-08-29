from __future__ import annotations

from pathlib import Path

from app.domain.collaboration import CollaborationTask, Handoff, RoleProfile

from .json_repository import JsonCollection


class CollaborationRepository:
    """Typed persistence for collaboration aggregates."""

    def __init__(self, root: Path):
        root = Path(root) / "collaboration"
        self.roles = JsonCollection(
            root / "roles", RoleProfile.from_dict, lambda value: value.to_dict()
        )
        self.tasks = JsonCollection(
            root / "tasks", CollaborationTask.from_dict, lambda value: value.to_dict()
        )
        self.handoffs = JsonCollection(
            root / "handoffs", Handoff.from_dict, lambda value: value.to_dict()
        )

    def list_roles(self) -> list[RoleProfile]:
        return sorted(self.roles.list(), key=lambda role: (role.label, role.role_id))

    def save_role(self, role: RoleProfile) -> RoleProfile:
        return self.roles.save(role, role.role_id)

    def list_tasks(self, project_id: str) -> list[CollaborationTask]:
        return sorted(
            (task for task in self.tasks.list() if task.project_id == project_id),
            key=lambda task: (-task.priority, task.created_at, task.task_id),
        )

    def save_task(self, task: CollaborationTask) -> CollaborationTask:
        return self.tasks.save(task, task.task_id)

    def list_handoffs(self, project_id: str) -> list[Handoff]:
        return sorted(
            (item for item in self.handoffs.list() if item.project_id == project_id),
            key=lambda item: (item.created_at, item.message_id),
        )

    def save_handoff(self, handoff: Handoff) -> Handoff:
        return self.handoffs.save(handoff, handoff.message_id)

