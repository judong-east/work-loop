from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from app.core.contracts import utc_now
from app.domain.collaboration import (
    CollaborationTask,
    Handoff,
    RoleProfile,
    TaskGraph,
    TaskOutcome,
    TaskStatus,
    WorkspaceAccess,
)
from app.infrastructure.collaboration_repository import CollaborationRepository


RoleValidator = Callable[[RoleProfile], None]
TaskExecutor = Callable[[CollaborationTask, RoleProfile, list[Handoff]], TaskOutcome]
ProjectValidator = Callable[[str], Any]


_DEFAULT_ROLES = (
    {
        "role_id": "analyst",
        "label": "需求分析师",
        "node_type": "requirement",
        "instructions": "澄清目标、范围、约束和可验证的验收条件。",
        "capabilities": ["analysis", "planning"],
        "workspace_access": "read",
    },
    {
        "role_id": "architect",
        "label": "架构师",
        "node_type": "planning",
        "instructions": "基于需求与上游交接，设计模块边界、执行步骤和风险控制。",
        "capabilities": ["planning", "reasoning"],
        "workspace_access": "read",
    },
    {
        "role_id": "developer",
        "label": "开发者",
        "node_type": "implementation",
        "instructions": "实现分配的代码任务，只提交完整、必要且可验证的文件变更。",
        "capabilities": ["coding", "implementation"],
        "workspace_access": "write",
    },
    {
        "role_id": "tester",
        "label": "测试工程师",
        "node_type": "testing",
        "instructions": "运行项目配置的验证命令，记录可复现的验证证据。",
        "capabilities": ["testing"],
        "workspace_access": "validate",
    },
    {
        "role_id": "reviewer",
        "label": "审核员",
        "node_type": "review",
        "instructions": "独立检查需求、实现和验证证据；不满足条件时阻塞交付。",
        "capabilities": ["review", "critical"],
        "workspace_access": "read",
    },
)


class CollaborationService:
    """Coordinate role-bound tasks over a durable dependency graph.

    Read-only roles in the same dependency wave may execute concurrently.
    Workspace writers and validators execute serially, so one project never has
    overlapping mutations or validation against a partially written tree.
    """

    def __init__(
        self,
        root: Path,
        *,
        validate_role: RoleValidator,
        execute_task: TaskExecutor,
        validate_project: ProjectValidator,
        max_workers: int = 4,
    ):
        self.repository = CollaborationRepository(root)
        self.validate_role = validate_role
        self.execute_task = execute_task
        self.validate_project = validate_project
        self.max_workers = max(1, min(max_workers, 8))
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._ensure_default_roles()

    def list_roles(self) -> list[RoleProfile]:
        order = {value["role_id"]: index for index, value in enumerate(_DEFAULT_ROLES)}
        return sorted(
            self.repository.list_roles(),
            key=lambda role: (order.get(role.role_id, len(order)), role.label, role.role_id),
        )

    def save_role(self, value: dict[str, Any]) -> RoleProfile:
        role_id = str(value.get("role_id", ""))
        try:
            current = self.repository.roles.get(role_id).to_dict()
        except FileNotFoundError:
            current = {}
        role = RoleProfile.from_dict({**current, **value, "updated_at": utc_now()})
        self.validate_role(role)
        return self.repository.save_role(role)

    def delete_role(self, role_id: str) -> None:
        self.repository.roles.get(role_id)
        references = [task.task_id for task in self.repository.tasks.list() if task.role_id == role_id]
        if references:
            raise ValueError(f"role is still used by tasks: {', '.join(references)}")
        self.repository.roles.delete(role_id)

    def roles_using_model(self, model_alias: str) -> list[str]:
        return [role.role_id for role in self.list_roles() if role.model_alias == model_alias]

    def roles_using_node(self, node_type: str) -> list[str]:
        return [role.role_id for role in self.list_roles() if role.node_type == node_type]

    def list_tasks(self, project_id: str) -> list[CollaborationTask]:
        self.validate_project(project_id)
        return self.repository.list_tasks(project_id)

    def create_task(self, project_id: str, value: dict[str, Any]) -> CollaborationTask:
        self.validate_project(project_id)
        role_id = str(value.get("role_id", ""))
        self.repository.roles.get(role_id)
        dependencies = value.get("depends_on", [])
        if not isinstance(dependencies, (list, tuple)):
            raise ValueError("task dependencies must be a list")
        task = CollaborationTask.create(
            project_id,
            str(value.get("title", "")),
            str(value.get("description", "")),
            role_id,
            depends_on=(str(item) for item in dependencies),
            priority=int(value.get("priority", 50)),
        )
        existing = self.repository.list_tasks(project_id)
        TaskGraph.validate([*existing, task])
        return self.repository.save_task(task)

    def delete_task(self, task_id: str) -> None:
        task = self.repository.tasks.get(task_id)
        if task.status is TaskStatus.RUNNING:
            raise ValueError("cannot delete a running task")
        dependents = [
            item.task_id for item in self.repository.list_tasks(task.project_id)
            if task_id in item.depends_on
        ]
        if dependents:
            raise ValueError(f"task is still a dependency of: {', '.join(dependents)}")
        for handoff in self.repository.list_handoffs(task.project_id):
            if task_id in {handoff.from_task_id, handoff.to_task_id}:
                self.repository.handoffs.delete(handoff.message_id)
        self.repository.tasks.delete(task_id)

    def retry_task(self, task_id: str) -> CollaborationTask:
        task = self.repository.tasks.get(task_id)
        if task.status not in {TaskStatus.BLOCKED, TaskStatus.FAILED}:
            raise ValueError("only blocked or failed tasks can be retried")
        task.status = TaskStatus.PENDING
        task.error = ""
        task.result = {}
        task.session_id = ""
        task.updated_at = utc_now()
        return self.repository.save_task(task)

    def project_state(self, project_id: str) -> dict[str, Any]:
        tasks = self.list_tasks(project_id)
        counts = {status.value: 0 for status in TaskStatus}
        for task in tasks:
            counts[task.status.value] += 1
        return {
            "project_id": project_id,
            "tasks": [task.to_dict() for task in tasks],
            "handoffs": [item.to_dict() for item in self.repository.list_handoffs(project_id)],
            "counts": counts,
        }

    def coordinate(self, project_id: str) -> dict[str, Any]:
        self.validate_project(project_id)
        lock = self._lock_for(project_id)
        if not lock.acquire(blocking=False):
            raise ValueError("this project is already being coordinated")
        try:
            self._recover_interrupted(project_id)
            while True:
                tasks = self.repository.list_tasks(project_id)
                TaskGraph.validate(tasks)
                ready = TaskGraph.ready(tasks)
                if not ready:
                    break
                self._run_wave(ready)
            self._block_failed_dependencies(project_id)
            return self.project_state(project_id)
        finally:
            lock.release()

    def _run_wave(self, tasks: list[CollaborationTask]) -> None:
        roles = {role.role_id: role for role in self.list_roles()}
        read_only = [task for task in tasks if roles[task.role_id].workspace_access is WorkspaceAccess.READ]
        serialized = [task for task in tasks if roles[task.role_id].workspace_access is not WorkspaceAccess.READ]

        if len(read_only) == 1:
            self._execute_one(read_only[0], roles[read_only[0].role_id])
        elif read_only:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(read_only))) as pool:
                futures = [pool.submit(self._execute_one, task, roles[task.role_id]) for task in read_only]
                for future in futures:
                    future.result()
        for task in serialized:
            self._execute_one(task, roles[task.role_id])

    def _execute_one(self, task: CollaborationTask, role: RoleProfile) -> None:
        task.status = TaskStatus.RUNNING
        task.error = ""
        task.updated_at = utc_now()
        self.repository.save_task(task)
        incoming = self._incoming_handoffs(task)
        try:
            outcome = self.execute_task(task, role, incoming)
        except Exception as error:  # task state must remain recoverable
            outcome = TaskOutcome(TaskStatus.FAILED, error=str(error))
        if not isinstance(outcome, TaskOutcome) or outcome.status not in {
            TaskStatus.COMPLETED,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
        }:
            outcome = TaskOutcome(TaskStatus.FAILED, error="executor returned an invalid terminal status")
        try:
            task.status = outcome.status
            task.session_id = outcome.session_id
            task.result = dict(outcome.result)
            task.error = outcome.error
            task.updated_at = utc_now()
            task.validate()
        except (TypeError, ValueError) as error:
            task.status = TaskStatus.FAILED
            task.session_id = ""
            task.result = {}
            task.error = f"invalid executor outcome: {error}"
            task.updated_at = utc_now()
        self.repository.save_task(task)
        if task.status is TaskStatus.COMPLETED:
            self._publish_handoffs(task)

    def _incoming_handoffs(self, task: CollaborationTask) -> list[Handoff]:
        handoffs = [
            item for item in self.repository.list_handoffs(task.project_id)
            if item.to_task_id == task.task_id
        ]
        existing = {item.from_task_id for item in handoffs}
        for dependency_id in task.depends_on:
            if dependency_id in existing:
                continue
            source = self.repository.tasks.get(dependency_id)
            if source.status is TaskStatus.COMPLETED:
                handoff = Handoff.create(source, task)
                self.repository.save_handoff(handoff)
                handoffs.append(handoff)
        return sorted(handoffs, key=lambda item: (item.created_at, item.message_id))

    def _publish_handoffs(self, source: CollaborationTask) -> None:
        tasks = self.repository.list_tasks(source.project_id)
        existing = {
            (item.from_task_id, item.to_task_id)
            for item in self.repository.list_handoffs(source.project_id)
        }
        for target in tasks:
            pair = (source.task_id, target.task_id)
            if source.task_id in target.depends_on and pair not in existing:
                self.repository.save_handoff(Handoff.create(source, target))

    def _recover_interrupted(self, project_id: str) -> None:
        for task in self.repository.list_tasks(project_id):
            if task.status is TaskStatus.RUNNING:
                task.status = TaskStatus.PENDING
                task.error = "previous coordination was interrupted"
                task.updated_at = utc_now()
                self.repository.save_task(task)

    def _block_failed_dependencies(self, project_id: str) -> None:
        while True:
            tasks = self.repository.list_tasks(project_id)
            failed = {
                task.task_id for task in tasks
                if task.status in {TaskStatus.BLOCKED, TaskStatus.FAILED}
            }
            blocked = [
                task for task in tasks
                if task.status is TaskStatus.PENDING and set(task.depends_on) & failed
            ]
            if not blocked:
                return
            for task in blocked:
                task.status = TaskStatus.BLOCKED
                task.error = "dependency did not complete"
                task.updated_at = utc_now()
                self.repository.save_task(task)

    def _lock_for(self, project_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(project_id, threading.Lock())

    def _ensure_default_roles(self) -> None:
        existing = {role.role_id for role in self.repository.list_roles()}
        for value in _DEFAULT_ROLES:
            if value["role_id"] not in existing:
                self.save_role(dict(value))
