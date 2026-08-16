from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.atomic_files import write_json_atomic


class WorkflowNodeKind(str, Enum):
    PLANNER = "planner"
    PLAN_APPROVAL = "plan_approval"
    EXECUTOR = "executor"
    VALIDATION = "validation"
    REVIEWER = "reviewer"
    DELIVERY = "delivery"


# Nodes that invoke an LLM agent and can carry rich instructions.
AGENT_NODE_KINDS = {
    WorkflowNodeKind.PLANNER,
    WorkflowNodeKind.EXECUTOR,
    WorkflowNodeKind.REVIEWER,
}
# Kinds that may appear multiple times in a workflow (flexible middle section).
MULTI_INSTANCE_KINDS = {
    WorkflowNodeKind.EXECUTOR,
    WorkflowNodeKind.VALIDATION,
    WorkflowNodeKind.REVIEWER,
}
# Kinds that must appear at least once.
REQUIRED_NODE_KINDS = [
    WorkflowNodeKind.PLANNER,
    WorkflowNodeKind.EXECUTOR,
    WorkflowNodeKind.VALIDATION,
    WorkflowNodeKind.REVIEWER,
    WorkflowNodeKind.DELIVERY,
]


@dataclass(frozen=True)
class WorkflowNode:
    node_id: str
    kind: WorkflowNodeKind
    label: str
    instructions: str = ""


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_id: str
    label: str
    nodes: list[WorkflowNode]
    description: str = ""
    builtin: bool = False
    schema_version: int = 1

    @property
    def requires_plan_approval(self) -> bool:
        return any(node.kind is WorkflowNodeKind.PLAN_APPROVAL for node in self.nodes)

    def node(self, kind: WorkflowNodeKind) -> WorkflowNode:
        return next(node for node in self.nodes if node.kind is kind)

    def nodes_of(self, kind: WorkflowNodeKind) -> list[WorkflowNode]:
        """Return all nodes matching *kind*, preserving workflow order."""
        return [node for node in self.nodes if node.kind is kind]

    def instructions_for(self, kind: WorkflowNodeKind) -> str:
        return self.node(kind).instructions.strip()


def _node(node_id: str, kind: WorkflowNodeKind, label: str) -> WorkflowNode:
    return WorkflowNode(node_id=node_id, kind=kind, label=label)


BUILTIN_WORKFLOWS: dict[str, WorkflowDefinition] = {
    "guarded": WorkflowDefinition(
        workflow_id="guarded",
        label="标准审批",
        description="计划经人工批准后执行，审核通过后等待确认交付。",
        builtin=True,
        nodes=[
            _node("plan", WorkflowNodeKind.PLANNER, "Claude 规划"),
            _node("approve", WorkflowNodeKind.PLAN_APPROVAL, "批准计划"),
            _node("execute", WorkflowNodeKind.EXECUTOR, "Codex 执行"),
            _node("validate", WorkflowNodeKind.VALIDATION, "确定性验证"),
            _node("review", WorkflowNodeKind.REVIEWER, "Claude 审核"),
            _node("deliver", WorkflowNodeKind.DELIVERY, "确认交付"),
        ],
    ),
    # Default preset: no plan-approval gate; the only human gate is delivery.
    "quick": WorkflowDefinition(
        workflow_id="quick",
        label="快速",
        description="计划无待澄清问题时自动执行，完成后一键确认交付。",
        builtin=True,
        nodes=[
            _node("plan", WorkflowNodeKind.PLANNER, "Claude 规划"),
            _node("execute", WorkflowNodeKind.EXECUTOR, "Codex 执行"),
            _node("validate", WorkflowNodeKind.VALIDATION, "确定性验证"),
            _node("review", WorkflowNodeKind.REVIEWER, "Claude 审核"),
            _node("deliver", WorkflowNodeKind.DELIVERY, "确认交付"),
        ],
    ),
}

# The no-approval preset was originally shipped as "autopilot"; persisted
# references still resolve to the same definition under its new name.
WORKFLOW_ALIASES = {"autopilot": "quick"}


def workflow_from_dict(data: Any, *, builtin: bool = False) -> WorkflowDefinition:
    if not isinstance(data, dict):
        raise ValueError("工作流必须是对象。")
    workflow_id = str(data.get("workflow_id", "")).strip()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", workflow_id):
        raise ValueError("workflow_id 必须是 2-64 位小写字母、数字、下划线或连字符。")
    label = str(data.get("label", "")).strip()
    if not label or len(label) > 80:
        raise ValueError("工作流名称不能为空且不能超过 80 个字符。")
    description = str(data.get("description", "")).strip()
    if len(description) > 300:
        raise ValueError("工作流说明不能超过 300 个字符。")
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list):
        raise ValueError("工作流 nodes 必须是数组。")

    nodes: list[WorkflowNode] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_nodes, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 个工作流节点必须是对象。")
        node_id = str(raw.get("node_id", raw.get("id", ""))).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", node_id):
            raise ValueError(f"第 {index} 个节点的 node_id 非法。")
        if node_id in seen_ids:
            raise ValueError(f"工作流节点 ID 重复：{node_id}。")
        seen_ids.add(node_id)
        try:
            kind = WorkflowNodeKind(str(raw.get("kind", "")))
        except ValueError as error:
            raise ValueError(f"第 {index} 个节点类型不受支持。") from error
        node_label = str(raw.get("label", "")).strip()
        if not node_label or len(node_label) > 80:
            raise ValueError(f"第 {index} 个节点名称不能为空且不能超过 80 个字符。")
        instructions = str(raw.get("instructions", "")).strip()
        if len(instructions) > 4000:
            raise ValueError(f"节点附加指令不能超过 4000 个字符：{node_id}。")
        nodes.append(
            WorkflowNode(
                node_id=node_id,
                kind=kind,
                label=node_label,
                instructions=instructions,
            )
        )

    # ---- Structural validation (flexible) ----
    kinds = [node.kind for node in nodes]

    # Every required kind must appear at least once.
    for required in REQUIRED_NODE_KINDS:
        if required not in kinds:
            raise ValueError(f"工作流必须至少包含一个 {required.value} 节点。")

    # plan_approval is optional but at most one.
    if kinds.count(WorkflowNodeKind.PLAN_APPROVAL) > 1:
        raise ValueError("工作流最多包含一个 plan_approval 节点。")

    # planner must be the first node; delivery must be the last.
    if kinds[0] is not WorkflowNodeKind.PLANNER:
        raise ValueError("工作流的第一个节点必须是 planner。")
    if kinds[-1] is not WorkflowNodeKind.DELIVERY:
        raise ValueError("工作流的最后一个节点必须是 delivery。")

    # plan_approval may only appear between planner and the first executor.
    if WorkflowNodeKind.PLAN_APPROVAL in kinds:
        approval_index = kinds.index(WorkflowNodeKind.PLAN_APPROVAL)
        if approval_index != 1:
            raise ValueError("plan_approval 节点必须紧跟在 planner 之后。")

    # Kinds that are NOT multi-instance must appear at most once.
    for kind in set(kinds):
        count = kinds.count(kind)
        if count > 1 and kind not in MULTI_INSTANCE_KINDS:
            raise ValueError(f"节点类型 {kind.value} 只能出现一次。")

    # Delivery evidence must describe the final writable state. This is a
    # partial-order safety constraint, not a fixed pipeline: nodes may otherwise
    # be reordered and repeated freely in the middle section.
    last_executor = max(
        index for index, kind in enumerate(kinds) if kind is WorkflowNodeKind.EXECUTOR
    )
    validation_after_write = [
        index
        for index, kind in enumerate(kinds)
        if kind is WorkflowNodeKind.VALIDATION and index > last_executor
    ]
    if not validation_after_write:
        raise ValueError("最后一个 executor 之后必须至少有一个 validation 节点。")
    last_validation = max(validation_after_write)
    if not any(
        kind is WorkflowNodeKind.REVIEWER and index > last_validation
        for index, kind in enumerate(kinds)
    ):
        raise ValueError("最后一个 validation 之后必须至少有一个 reviewer 节点。")
    return WorkflowDefinition(
        workflow_id=workflow_id,
        label=label,
        description=description,
        nodes=nodes,
        builtin=builtin,
        schema_version=int(data.get("schema_version", 1)),
    )


class WorkflowCatalog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def list_all(self) -> list[WorkflowDefinition]:
        workflows = dict(BUILTIN_WORKFLOWS)
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(f"无法读取工作流目录：{error}") from error
            raw_items = data.get("workflows", []) if isinstance(data, dict) else []
            if not isinstance(raw_items, list):
                raise ValueError("工作流目录 workflows 必须是数组。")
            for raw in raw_items:
                workflow = workflow_from_dict(raw)
                if workflow.workflow_id in BUILTIN_WORKFLOWS:
                    raise ValueError(f"自定义工作流不能覆盖内置工作流：{workflow.workflow_id}。")
                workflows[workflow.workflow_id] = workflow
        return list(workflows.values())

    def get(self, workflow_id: str) -> WorkflowDefinition:
        resolved = WORKFLOW_ALIASES.get(workflow_id, workflow_id)
        selected = next(
            (item for item in self.list_all() if item.workflow_id == resolved),
            None,
        )
        if selected is None:
            raise ValueError(f"工作流不存在：{workflow_id}。")
        return selected

    def save(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        if workflow.workflow_id in BUILTIN_WORKFLOWS:
            raise ValueError("不能修改内置工作流。")
        custom = [item for item in self.list_all() if not item.builtin]
        by_id = {item.workflow_id: item for item in custom}
        by_id[workflow.workflow_id] = workflow
        write_json_atomic(self.path, {"schema_version": 1, "workflows": list(by_id.values())})
        return workflow

    def delete(self, workflow_id: str) -> None:
        if workflow_id in BUILTIN_WORKFLOWS:
            raise ValueError("不能删除内置工作流。")
        custom = [item for item in self.list_all() if not item.builtin]
        by_id = {item.workflow_id: item for item in custom}
        if workflow_id not in by_id:
            raise ValueError(f"工作流不存在：{workflow_id}。")
        del by_id[workflow_id]
        write_json_atomic(self.path, {"schema_version": 1, "workflows": list(by_id.values())})
