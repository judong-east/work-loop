from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol

from app.core.contracts import new_id
from app.domain.collaboration import CollaborationTask, Goal, TaskGraph, WorkspaceAccess
from app.domain.models import ContextState, Project, WorkflowNode


class _Gateway(Protocol):
    def complete(self, *, model_alias: str, node: WorkflowNode, context: ContextState) -> dict[str, Any]: ...


class _Collaboration(Protocol):
    def list_roles(self) -> Iterable[Any]: ...
    def create_task(self, project_id: str, value: dict[str, Any]) -> CollaborationTask: ...
    def save_goal(self, goal: Goal) -> Goal: ...
    def coordinate(self, project_id: str) -> dict[str, Any]: ...
    def project_state(self, project_id: str) -> dict[str, Any]: ...


_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

DECOMPOSE_INSTRUCTIONS = (
    "你是任务规划师。把用户给出的大目标拆分为少量可独立执行的协同子任务。"
    "只返回一个 JSON 对象，不要 markdown 围栏，格式为："
    '{"summary": "一句话概括拆分思路", "subtasks": [{"ref": "1", "title": "子任务名", '
    '"description": "目标、范围与完成条件", "role_id": "负责角色ID", '
    '"depends_on": ["其他子任务的ref"], "priority": 50}]}。'
    "规则：每个子任务由恰好一个角色负责，role_id 只能取自 available_roles；"
    "ref 在方案内唯一；depends_on 只能引用其他子任务的 ref，禁止自引用或成环；"
    "description 必须包含可验证的完成条件；写入类角色（workspace_access=write）的任务应尽量少且靠后。"
)


@dataclass(frozen=True)
class PlanItem:
    ref: str
    title: str
    description: str
    role_id: str
    depends_on: tuple[str, ...] = ()
    priority: int = 50


@dataclass(frozen=True)
class GoalPlan:
    summary: str
    items: tuple[PlanItem, ...]

    @classmethod
    def from_output(cls, output: dict[str, Any], *, max_items: int) -> "GoalPlan":
        if not isinstance(output, dict):
            raise ValueError("拆分输出必须是 JSON 对象")
        summary = str(output.get("summary", ""))
        raw_items = output.get("subtasks", [])
        if not isinstance(raw_items, list):
            raise ValueError("subtasks 必须是数组")
        if not raw_items:
            raise ValueError("拆分结果为空：模型没有返回任何子任务")
        if len(raw_items) > max_items:
            raise ValueError(f"子任务数量超过上限 {max_items}（实际 {len(raw_items)}）")
        items: list[PlanItem] = []
        for index, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, dict):
                raise ValueError("每个子任务必须是对象")
            ref = str(raw.get("ref", str(index))).strip()
            if not _REF_PATTERN.fullmatch(ref):
                raise ValueError(f"子任务 ref 不合法: {ref!r}")
            title = str(raw.get("title", "")).strip()
            description = str(raw.get("description", "")).strip()
            role_id = str(raw.get("role_id", "")).strip()
            depends_raw = raw.get("depends_on", [])
            if not isinstance(depends_raw, list):
                raise ValueError(f"子任务 {ref} 的 depends_on 必须是数组")
            depends_on = tuple(str(item).strip() for item in depends_raw)
            try:
                priority = int(raw.get("priority", 50))
            except (TypeError, ValueError) as error:
                raise ValueError(f"子任务 {ref} 的 priority 必须是整数") from error
            if not title or not description or not role_id:
                raise ValueError(f"子任务 {ref} 缺少 title、description 或 role_id")
            if len(title) > 200 or len(description) > 50_000:
                raise ValueError(f"子任务 {ref} 的标题或描述过长")
            if ref in depends_on:
                raise ValueError(f"子任务 {ref} 不能依赖自身")
            items.append(PlanItem(ref, title, description, role_id, depends_on, priority))
        if len({item.ref for item in items}) != len(items):
            raise ValueError("子任务 ref 必须唯一")
        return cls(summary, tuple(items))


class GoalDecomposer:
    """Split one large goal into dependency-ordered role-owned subtasks.

    The planning model proposes a ref-based draft plan; this service validates
    the plan against the configured roles and the DAG rules, then materializes
    durable tasks grouped under a Goal record.
    """

    def __init__(
        self,
        *,
        gateway: _Gateway,
        collaboration: _Collaboration,
        project_loader: Callable[[str], Project],
        project_context: Callable[[Project], dict[str, Any]],
        workspace_snapshot: Callable[[Project], dict[str, Any]],
        max_subtasks_limit: int = 20,
    ):
        self.gateway = gateway
        self.collaboration = collaboration
        self.project_loader = project_loader
        self.project_context = project_context
        self.workspace_snapshot = workspace_snapshot
        self.max_subtasks_limit = max(1, max_subtasks_limit)

    def decompose(
        self,
        project_id: str,
        goal: str,
        *,
        max_subtasks: int = 8,
        subtasks: list[dict[str, Any]] | None = None,
        auto_coordinate: bool = False,
    ) -> dict[str, Any]:
        if not goal.strip():
            raise ValueError("goal cannot be empty")
        max_subtasks = max(1, min(int(max_subtasks), self.max_subtasks_limit))
        project = self.project_loader(project_id)
        roles = {role.role_id: role for role in self.collaboration.list_roles()}
        if not roles:
            raise ValueError("请先在角色池中配置至少一个角色。")
        if subtasks is None:
            plan = self._ask_model(project, goal, roles, max_subtasks)
        else:
            plan = GoalPlan.from_output(
                {"summary": "", "subtasks": subtasks}, max_items=self.max_subtasks_limit
            )
        self._validate_plan(plan, roles)
        goal_record = self.collaboration.save_goal(Goal.create(project_id, goal, summary=plan.summary))
        created: dict[str, str] = {}
        for item in self._topological(plan.items):
            task = self.collaboration.create_task(project_id, {
                "title": item.title,
                "description": item.description,
                "role_id": item.role_id,
                "depends_on": [created[dep] for dep in item.depends_on],
                "priority": item.priority,
                "goal_id": goal_record.goal_id,
            })
            created[item.ref] = task.task_id
        goal_record.task_ids = [created[item.ref] for item in plan.items]
        goal_record = self.collaboration.save_goal(goal_record)
        state = (
            self.collaboration.coordinate(project_id)
            if auto_coordinate
            else self.collaboration.project_state(project_id)
        )
        return {"goal": goal_record.to_dict(), "task_ids": list(created.values()), "state": state}

    def _ask_model(
        self,
        project: Project,
        goal: str,
        roles: dict[str, Any],
        max_subtasks: int,
    ) -> GoalPlan:
        model_alias = ""
        planner = next(
            (role for role in roles.values() if role.node_type == "planning" and role.model_alias),
            None,
        )
        if planner is not None:
            model_alias = planner.model_alias
        elif project.default_model:
            model_alias = project.default_model
        role_descriptions = [
            {
                "role_id": role.role_id,
                "label": role.label,
                "node_type": role.node_type,
                "workspace_access": role.workspace_access.value
                if isinstance(role.workspace_access, WorkspaceAccess)
                else str(role.workspace_access),
                "model_alias": role.model_alias,
                "capabilities": list(role.capabilities),
            }
            for role in roles.values()
        ]
        node = WorkflowNode(
            f"decompose-{new_id('N')}",
            "tool",
            model_alias=model_alias,
            prompt_template=DECOMPOSE_INSTRUCTIONS,
            config={"_output_fields": ["summary", "subtasks"]},
        )
        context = ContextState().merge({"inputs": {
            "request": goal,
            "project": self.project_context(project),
            "workspace": self.workspace_snapshot(project),
            "available_roles": role_descriptions,
            "max_subtasks": max_subtasks,
        }})
        output = self.gateway.complete(model_alias=model_alias, node=node, context=context)
        try:
            return GoalPlan.from_output(output, max_items=max_subtasks)
        except ValueError as error:
            raise ValueError(f"拆分计划不合法: {error}\n模型原始输出: {json.dumps(output, ensure_ascii=False)[:2000]}") from error

    @staticmethod
    def _validate_plan(plan: GoalPlan, roles: dict[str, Any]) -> None:
        for item in plan.items:
            if item.role_id not in roles:
                raise ValueError(
                    f"子任务 {item.ref} 引用了未知角色 {item.role_id}；可用角色：{', '.join(sorted(roles))}"
                )
        drafts = [
            CollaborationTask(
                task_id=f"plan-{item.ref}",
                project_id="PLAN",
                title=item.title,
                description=item.description,
                role_id=item.role_id,
                depends_on=tuple(f"plan-{dep}" for dep in item.depends_on),
                priority=item.priority,
            )
            for item in plan.items
        ]
        for draft in drafts:
            draft.validate()
        # Unknown refs and cycles are both rejected by the graph rules.
        TaskGraph.validate(drafts)

    @staticmethod
    def _topological(items: tuple[PlanItem, ...]) -> list[PlanItem]:
        by_ref = {item.ref: item for item in items}
        remaining = {
            item.ref: {dep for dep in item.depends_on if dep in by_ref}
            for item in items
        }
        ordered: list[PlanItem] = []
        while remaining:
            ready = [ref for ref, deps in remaining.items() if not deps]
            if not ready:
                raise ValueError("拆分计划存在循环依赖")
            for ref in sorted(ready):
                ordered.append(by_ref[ref])
                del remaining[ref]
            for deps in remaining.values():
                deps.difference_update(ready)
        return ordered
