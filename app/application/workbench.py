from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.core.contracts import utc_now
from app.domain.collaboration import (
    CollaborationTask,
    Handoff,
    RoleProfile,
    TaskOutcome,
    TaskStatus,
    WorkspaceAccess,
)
from app.domain.models import (
    ModelAlias,
    ModelProvider,
    NodeDefinition,
    Project,
    Session,
    SessionMode,
    TaskPolicy,
    WorkflowDefinition,
    WorkflowNode,
    default_runtime_policy,
)
from app.domain.longhorizon import LongHorizonLoop
from app.domain.node_catalog import NodeCatalog
from app.domain.node_registry import NodeRegistry
from app.domain.orchestrator import DagOrchestrator, ModelGateway, OrchestrationEvent
from app.domain.strategy_presets import get_strategy_preset, infer_strategy, list_strategy_presets
from app.domain.tooling import SEARCH_TOOLS
from app.domain.workflow_catalog import WorkflowCatalog
from app.infrastructure.json_repository import JsonCollection
from app.infrastructure.model_gateway import OpenAICompatibleGateway
from app.infrastructure.resource_center import ResourceCenter
from app.infrastructure.zvec_grep import ZvecGrepClient
from app.infrastructure.workspace_runtime import WorkspaceRuntime
from .model_invocation import ModelInvocationService


CHAT_PROJECT_ID = "CHAT"
MAX_CHAT_ATTACHMENTS = 5
MAX_CHAT_ATTACHMENT_BYTES = 200_000


def _normalize_assistant_text(value: Any) -> str:
    """Remove provider-added blank lines around an assistant response.

    Several compatible endpoints prefix otherwise valid answers with one or
    more newlines.  Those newlines are meaningful to ``white-space: pre-wrap``
    in the chat UI, where they create an empty block before the first visible
    line.  Only line-break characters at the boundaries are removed so
    indentation and intentional blank lines inside the answer stay intact.
    """

    return str(value).strip("\r\n")


def _normalize_chat_tools(value: Any) -> list[str] | None:
    """Validate a per-message local-tool override.

    ``None`` deliberately means "use the project's local-search policy", while
    an empty list is a meaningful override that disables tools for this turn.
    Keeping that distinction lets the composer offer a predictable per-message
    control without changing project settings behind the user's back.
    """

    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("tools must be an array")
    names: list[str] = []
    for raw in value:
        name = str(raw).strip()
        if not name or name not in SEARCH_TOOLS:
            raise ValueError(f"unknown chat tool: {name or raw}")
        if name not in names:
            names.append(name)
    return names


def _normalize_chat_attachments(value: Any) -> list[dict[str, Any]]:
    """Normalize bounded text attachments received from the web composer."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("attachments must be an array")
    if len(value) > MAX_CHAT_ATTACHMENTS:
        raise ValueError(f"最多支持 {MAX_CHAT_ATTACHMENTS} 个附件")

    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("attachment must be an object")
        raw_name = str(item.get("name", "")).strip().replace("\\", "/")
        name = raw_name.rsplit("/", 1)[-1][:240]
        if not name:
            raise ValueError("attachment name is required")
        mime_type = str(item.get("mime_type", "application/octet-stream")).strip()[:120]
        content = str(item.get("content", ""))
        try:
            size = int(item.get("size", len(content.encode("utf-8"))))
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid attachment size: {name}") from error
        encoded_size = len(content.encode("utf-8"))
        if size < 0 or size > MAX_CHAT_ATTACHMENT_BYTES or encoded_size > MAX_CHAT_ATTACHMENT_BYTES:
            raise ValueError(f"附件 {name} 超过 {MAX_CHAT_ATTACHMENT_BYTES // 1000} KB 限制")
        normalized.append({
            "name": name,
            "mime_type": mime_type or "application/octet-stream",
            "size": size,
            "content": content,
        })
    return normalized


def _chat_request_with_attachments(content: str, attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return content
    blocks: list[str] = []
    for item in attachments:
        if item["content"]:
            blocks.append(f"\n\n[附件：{item['name']}]\n{item['content']}")
        else:
            blocks.append(f"\n\n[附件：{item['name']}（未读取文本内容）]")
    return content + "".join(blocks)


class WorkbenchService:
    """Application facade for projects, sessions, resources, and workflows."""

    def __init__(
        self,
        root: Path,
        *,
        gateway: ModelGateway | None = None,
        workspace_runtime: WorkspaceRuntime | None = None,
        search_client: ZvecGrepClient | None = None,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry = NodeRegistry()
        self.nodes = NodeCatalog(self.root / "nodes.json", self.registry)
        self.resources = ResourceCenter(self.root / "resources")
        self.projects = JsonCollection(self.root / "projects", Project.from_dict, lambda value: value.to_dict())
        self.sessions = JsonCollection(self.root / "sessions", Session.from_dict, lambda value: value.to_dict())
        self.workflows = WorkflowCatalog(self.root / "workflows.json", self.registry)
        self.events_path = self.root / "events.jsonl"
        self._event_lock = threading.Lock()
        self.gateway = gateway or OpenAICompatibleGateway(self.resources)
        self.search_client = search_client or ZvecGrepClient()
        self.model_invocation = ModelInvocationService(
            self.gateway,
            search_client=self.search_client,
            event_sink=self._record_event,
        )
        self.workspace_runtime = workspace_runtime or WorkspaceRuntime()
        self.registry.register_handler("testing", self.workspace_runtime.validate)
        self.longhorizon = LongHorizonLoop(
            self.gateway,
            self.workspace_runtime,
            invoker=self.model_invocation,
            event_sink=self._record_event,
            store=self.sessions,
        )
        self.registry.register_handler("long_horizon", self.longhorizon.run)
        self.orchestrator = DagOrchestrator(
            self.registry,
            self.sessions,
            self.gateway,
            event_sink=self._record_event,
            output_processor=self.workspace_runtime.process_output,
            invoker=self.model_invocation,
        )
        self._ensure_default_workflow()

    def _ensure_default_workflow(self) -> None:
        self.workflows.save(
            WorkflowDefinition(
                workflow_id="default-task",
                label="默认任务流",
                description="需求梳理 → 计划制定 → 项目执行 → 测试 → 审核",
                builtin=True,
                nodes=[
                    WorkflowNode("requirement", "requirement"),
                    WorkflowNode("planning", "planning", ("requirement",)),
                    WorkflowNode("implementation", "implementation", ("planning",)),
                    WorkflowNode("testing", "testing", ("implementation",), on_failure="human"),
                    WorkflowNode("review", "review", ("implementation", "testing"), on_failure="human"),
                ],
            )
        )
        self.workflows.save(
            WorkflowDefinition(
                workflow_id="long-horizon-task",
                label="长时程任务流",
                description="计划制定 → Manager/Executor/Auditor 多轮循环执行 → 测试 → 审核",
                builtin=True,
                nodes=[
                    WorkflowNode("planning", "planning"),
                    WorkflowNode("long_horizon", "long_horizon", ("planning",), on_failure="human"),
                    WorkflowNode("testing", "testing", ("long_horizon",), on_failure="human"),
                    WorkflowNode("review", "review", ("long_horizon", "testing"), on_failure="human"),
                ],
            )
        )

    def list_node_types(self) -> list[dict[str, Any]]:
        return [
            {
                "node_type": item.node_type,
                "label": item.label,
                "description": item.description,
                "input_fields": list(item.input_fields),
                "output_fields": list(item.output_fields),
                "capabilities": list(item.capabilities),
                "default_model": item.default_model,
                "builtin": item.builtin,
            }
            for item in self.registry.list()
        ]

    def create_project(
        self,
        name: str,
        *,
        instructions: str = "",
        knowledge_refs: list[str] | None = None,
        default_model: str = "",
        workspace_path: str = "",
        validation_commands: list[list[str]] | None = None,
        runtime_policy: dict[str, Any] | None = None,
    ) -> Project:
        if default_model and default_model not in {item.alias for item in self.resources.list_models()}:
            raise ValueError(f"unknown model alias: {default_model}")
        project = Project.create(
            name,
            instructions=instructions,
            knowledge_refs=knowledge_refs or [],
            default_model=default_model,
            workspace_path=workspace_path,
            validation_commands=validation_commands or [],
            runtime_policy=dict(runtime_policy) if runtime_policy is not None else default_runtime_policy(),
        )
        return self.projects.save(project, project.project_id)

    def list_projects(self) -> list[Project]:
        return sorted(self.projects.list(), key=lambda item: item.updated_at, reverse=True)

    def get_project(self, project_id: str) -> Project:
        return self.projects.get(project_id)

    def delete_project(self, project_id: str) -> dict[str, Any]:
        """Delete a project and its conversation sessions.

        Project deletion is intentionally explicit and scoped to persisted
        project-owned records.  Workspace files are never touched.  A running
        session is rejected so an in-flight model request cannot lose its
        persistence target halfway through a response.
        """

        self.get_project(project_id)
        sessions = [item for item in self.sessions.list() if item.project_id == project_id]
        running = [item.session_id for item in sessions if item.status == "running"]
        if running:
            raise ValueError("项目仍有正在运行的会话，请稍后再删除。")
        for session in sessions:
            self.sessions.delete(session.session_id)
        self.projects.delete(project_id)
        return {"project_id": project_id, "deleted_sessions": len(sessions)}

    def create_chat_session(self, title: str = "新的会话") -> Session:
        """Create a persisted conversation that does not belong to a project.

        Chat-only sessions still use the normal session and model-invocation
        pipeline, but carry a private synthetic project context so the rest of
        the runtime can keep its existing contracts.  The synthetic project is
        never written to the project collection or shown in the project list.
        """
        session = Session.create(CHAT_PROJECT_ID, title, SessionMode.CHAT)
        project = self._chat_project()
        session.context = session.context.merge({"inputs": {
            "project": self._project_context(project),
            "workspace": self.workspace_runtime.snapshot(project),
        }})
        return self.sessions.save(session, session.session_id)

    def update_project(self, project_id: str, value: dict[str, Any]) -> Project:
        current = self.get_project(project_id)
        default_model = str(value.get("default_model", current.default_model))
        if default_model and default_model not in {item.alias for item in self.resources.list_models()}:
            raise ValueError(f"unknown model alias: {default_model}")
        project = Project(
            project_id=current.project_id,
            name=str(value.get("name", current.name)).strip(),
            instructions=str(value.get("instructions", current.instructions)),
            knowledge_refs=value.get("knowledge_refs", current.knowledge_refs),
            default_model=default_model,
            workspace_path=str(value.get("workspace_path", current.workspace_path)),
            validation_commands=value.get("validation_commands", current.validation_commands),
            runtime_policy=dict(value.get("runtime_policy", current.runtime_policy)),
            created_at=current.created_at,
            updated_at=utc_now(),
            schema_version=current.schema_version,
        )
        project.validate()
        return self.projects.save(project, project.project_id)

    def create_session(
        self,
        project_id: str,
        title: str,
        *,
        mode: SessionMode = SessionMode.CHAT,
        workflow_id: str = "",
        policy: TaskPolicy | dict[str, Any] | None = None,
    ) -> Session:
        project = self.get_project(project_id)
        if mode is SessionMode.TASK:
            workflow_id = workflow_id or "default-task"
            self.workflows.get(workflow_id)
        if isinstance(policy, TaskPolicy):
            task_policy = policy
        else:
            task_policy = TaskPolicy.from_dict(policy)
        task_policy.validate()
        session = Session.create(project_id, title, mode, workflow_id, policy=task_policy)
        session.context = session.context.merge({"inputs": {
            "project": self._project_context(project),
            "workspace": self.workspace_runtime.snapshot(project),
        }})
        return self.sessions.save(session, session.session_id)

    def list_strategies(self) -> list[dict[str, Any]]:
        return list_strategy_presets()

    def strategy_for(self, text: str) -> dict[str, Any]:
        return get_strategy_preset(infer_strategy(text))

    def update_policy(self, session_id: str, value: dict[str, Any]) -> Session:
        session = self.get_session(session_id)
        current = session.policy.to_dict()
        current.update({key: value[key] for key in value if key in current and key != "history"})
        if "history" in value:
            current["history"] = value["history"]
        session.policy = TaskPolicy.from_dict(current)
        session.policy.revision += 1
        session.updated_at = utc_now()
        return self.sessions.save(session, session.session_id)

    def approve_policy(self, session_id: str) -> Session:
        session = self.get_session(session_id)
        session.policy.gate_status = "approved"
        session.policy.gate = ""
        if session.status in {"waiting_for_human", "needs_replan"}:
            session.status = "idle"
        session.policy.revision += 1
        return self.sessions.save(session, session.session_id)

    def replan_policy(self, session_id: str, *, reason: str = "") -> Session:
        session = self.get_session(session_id)
        session.policy.gate_status = "open"
        session.policy.gate = ""
        session.policy.revision += 1
        session.policy.next_action = reason.strip() or "重新规划任务"
        session.status = "idle"
        return self.sessions.save(session, session.session_id)

    def list_sessions(
        self,
        project_id: str = "",
        *,
        include_collaboration: bool = False,
    ) -> list[Session]:
        sessions = self.sessions.list()
        if project_id:
            sessions = [session for session in sessions if session.project_id == project_id]
        if not include_collaboration:
            sessions = [session for session in sessions if session.purpose == "conversation"]
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def get_session(self, session_id: str) -> Session:
        return self.sessions.get(session_id)

    def delete_session(self, session_id: str) -> None:
        session = self.get_session(session_id)
        if session.purpose != "conversation":
            raise ValueError("协同任务会话由任务记录管理，不能在会话列表中删除。")
        self.sessions.delete(session_id)

    def send_message(
        self,
        session_id: str,
        content: str,
        *,
        model_alias: str | None = None,
        tools: list[str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Session:
        session = self.get_session(session_id)
        if not content.strip():
            raise ValueError("message cannot be empty")
        selected_tools = _normalize_chat_tools(tools)
        normalized_attachments = _normalize_chat_attachments(attachments)
        if session.mode is not SessionMode.CHAT and (selected_tools is not None or normalized_attachments):
            raise ValueError("工具和附件仅支持普通对话")
        normalized = content.strip()
        attachment_meta = [
            {key: item[key] for key in ("name", "mime_type", "size")}
            for item in normalized_attachments
        ]
        session.add_message(
            "user",
            normalized,
            metadata={"attachments": attachment_meta} if attachment_meta else None,
        )
        project = self._session_project(session)
        selected_model = self._message_model_alias(session, model_alias)
        session.context = session.context.merge({"inputs": {
            "request": _chat_request_with_attachments(normalized, normalized_attachments),
            "project": self._project_context(project),
            "workspace": self.workspace_runtime.snapshot(project),
            "model_alias": selected_model,
        }})
        if session.mode is SessionMode.CHAT:
            node = WorkflowNode(
                "chat", "tool", model_alias=selected_model or project.default_model,
                prompt_template="直接回答用户请求；result 字段必须是可展示给用户的完整文本。",
                config={"tools": selected_tools} if selected_tools is not None else {},
            )
            started = time.monotonic()
            output = self.model_invocation.invoke(
                session=session,
                node=node,
                context=session.context,
                model_alias=selected_model,
                output_fields=("result",),
            )
            elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
            model_name = str(output.get("model") or selected_model or project.default_model)
            session.add_message(
                "assistant", _normalize_assistant_text(output.get("result", output)),
                metadata={"mode": "chat", "model": model_name, "elapsed_ms": elapsed_ms},
            )
        return self.sessions.save(session, session.session_id)

    def send_message_stream(
        self,
        session_id: str,
        content: str,
        *,
        model_alias: str | None = None,
        tools: list[str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Return an iterator for a chat SSE session without changing the JSON API.

        The user message and running state are persisted before model work
        starts.  The assistant message remains atomic: partial deltas are only
        sent to the client, and the complete ``result`` is written after the
        gateway emits ``done``.
        """

        session = self.get_session(session_id)
        if not content.strip():
            raise ValueError("message cannot be empty")
        if session.mode is not SessionMode.CHAT:
            raise ValueError("流式消息接口只支持普通对话会话")
        selected_tools = _normalize_chat_tools(tools)
        normalized_attachments = _normalize_chat_attachments(attachments)
        normalized = content.strip()
        attachment_meta = [
            {key: item[key] for key in ("name", "mime_type", "size")}
            for item in normalized_attachments
        ]
        session.add_message(
            "user",
            normalized,
            metadata={"attachments": attachment_meta} if attachment_meta else None,
        )
        project = self._session_project(session)
        selected_model = self._message_model_alias(session, model_alias)
        session.context = session.context.merge({"inputs": {
            "request": _chat_request_with_attachments(normalized, normalized_attachments),
            "project": self._project_context(project),
            "workspace": self.workspace_runtime.snapshot(project),
            "model_alias": selected_model,
        }})
        session.status = "running"
        self.sessions.save(session, session.session_id)

        node = WorkflowNode(
            "chat", "tool", model_alias=selected_model or project.default_model,
            prompt_template="直接回答用户请求；输出可直接展示给用户的完整文本。",
            config={"tools": selected_tools} if selected_tools is not None else {},
        )

        def events() -> Iterator[dict[str, Any]]:
            assistant_parts: list[str] = []
            done = False
            started = time.monotonic()
            try:
                yield {"type": "start", "session_id": session.session_id}
                for event in self.model_invocation.invoke_stream(
                    session=session,
                    node=node,
                    context=session.context,
                    model_alias=selected_model,
                    output_fields=("result",),
                ):
                    event_type = str(event.get("type", ""))
                    if event_type == "text_delta":
                        assistant_parts.append(str(event.get("text", "")))
                        yield event
                        continue
                    if event_type != "done":
                        yield event
                        continue

                    output = event.get("output", {})
                    if not isinstance(output, dict):
                        output = {"result": output}
                    result = output.get("result", "")
                    assistant_text = _normalize_assistant_text(result if result != "" else "".join(assistant_parts))
                    elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
                    model_name = str(output.get("model") or selected_model or project.default_model)
                    session.add_message(
                        "assistant",
                        assistant_text,
                        metadata={"mode": "chat", "model": model_name, "elapsed_ms": elapsed_ms},
                    )
                    session.status = "idle"
                    saved = self.sessions.save(session, session.session_id)
                    done = True
                    yield {
                        "type": "done",
                        "output": output,
                        "model": model_name,
                        "elapsed_ms": elapsed_ms,
                        "session": saved.to_dict(),
                    }
                if not done:
                    raise RuntimeError("流式模型未返回最终结果")
            except BaseException:
                session.status = "idle"
                try:
                    self.sessions.save(session, session.session_id)
                except OSError:
                    pass
                raise

        return events()

    def save_workflow(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        try:
            workflow.builtin = self.workflows.get(workflow.workflow_id).builtin
        except KeyError:
            workflow.builtin = False
        aliases = {item.alias for item in self.resources.list_models()}
        missing = sorted({node.model_alias for node in workflow.nodes if node.model_alias and node.model_alias not in aliases})
        if missing:
            raise ValueError(f"unknown model aliases: {', '.join(missing)}")
        return self.workflows.save(workflow)

    def list_workflows(self) -> list[WorkflowDefinition]:
        return self.workflows.list()

    def delete_workflow(self, workflow_id: str) -> None:
        workflow = self.workflows.get(workflow_id)
        if workflow.builtin:
            raise ValueError(f"cannot delete built-in workflow: {workflow_id}")
        if any(session.workflow_id == workflow_id for session in self.sessions.list()):
            raise ValueError("workflow is still used by sessions")
        self.workflows.delete(workflow_id)

    def save_node(self, value: dict[str, Any]) -> NodeDefinition:
        definition = NodeDefinition(
            node_type=str(value["node_type"]),
            label=str(value.get("label", value["node_type"])),
            description=str(value.get("description", "")),
            input_fields=tuple(str(item) for item in value.get("input_fields", [])),
            output_fields=tuple(str(item) for item in value.get("output_fields", [])),
            capabilities=tuple(str(item) for item in value.get("capabilities", ["general"])),
            default_model=str(value.get("default_model", "")),
            builtin=False,
        )
        if definition.default_model and definition.default_model not in {item.alias for item in self.resources.list_models()}:
            raise ValueError(f"unknown model alias: {definition.default_model}")
        return self.nodes.save(definition)

    def delete_node(self, node_type: str) -> None:
        definition = self.registry.get(node_type)
        if definition.builtin:
            raise ValueError(f"cannot delete built-in node: {node_type}")
        references = [
            workflow.workflow_id
            for workflow in self.workflows.list()
            if any(node.node_type == node_type for node in workflow.nodes)
        ]
        if references:
            raise ValueError(f"node is still used by workflows: {', '.join(references)}")
        self.nodes.delete(node_type)

    def run_task(self, session_id: str, workflow: WorkflowDefinition | None = None) -> Session:
        session = self.get_session(session_id)
        if session.mode is not SessionMode.TASK:
            raise ValueError("only task sessions can run a workflow")
        if workflow is None:
            workflow = self.workflows.get(session.workflow_id)
        project = self.get_project(session.project_id)
        session.context = session.context.merge({"inputs": {
            "project": self._project_context(project),
            "workspace": self.workspace_runtime.snapshot(project),
        }})
        if not session.context.inputs.get("request"):
            latest = next((message.content for message in reversed(session.messages) if message.role == "user"), "")
            session.context = session.context.merge({"inputs": {"request": latest}})
        return self.orchestrator.run(session, workflow)

    def workspace_status(self, project_id: str) -> dict[str, Any]:
        return self.workspace_runtime.snapshot(self.get_project(project_id))

    def search_status(self, project_id: str) -> dict[str, Any]:
        """Return local zvec-grep readiness without starting an index job."""

        project = self.get_project(project_id)
        if not project.workspace_path:
            return {
                "ready": False,
                "root": "",
                "error": "项目尚未配置 workspace_path。",
                "backend": "zvec-grep",
                "local_only": True,
            }
        result = self.search_client.health(project.workspace_path)
        result["backend"] = "zvec-grep"
        result["local_only"] = True
        return result

    def validate_role(self, role: RoleProfile) -> None:
        role.validate()
        self.registry.get(role.node_type)
        self.validate_model_alias(role.model_alias)
        if role.node_type == "implementation" and role.workspace_access is not WorkspaceAccess.WRITE:
            raise ValueError("implementation roles require write workspace access")
        if role.node_type == "long_horizon" and role.workspace_access is not WorkspaceAccess.WRITE:
            raise ValueError("long_horizon roles require write workspace access")
        if role.node_type == "testing" and role.workspace_access is not WorkspaceAccess.VALIDATE:
            raise ValueError("testing roles require validate workspace access")

    def validate_model_alias(self, alias: str) -> None:
        if not alias:
            raise ValueError("model alias is required")
        try:
            self.resources.resolve(alias)
        except KeyError as error:
            raise ValueError(f"unknown model alias: {alias}") from error

    def default_model_for_node(self, node_type: str) -> str:
        definition = self.registry.get(node_type)
        models = self.resources.list_models()
        for capability in definition.capabilities:
            alias = self.resources.default_alias(capability)
            if alias and any(
                item.alias == alias and capability in item.capabilities
                for item in models
            ):
                return alias
        return self.resources.default_alias()

    def execute_role_task(
        self,
        task: CollaborationTask,
        role: RoleProfile,
        handoffs: list[Handoff],
    ) -> TaskOutcome:
        self.validate_role(role)
        project = self.get_project(task.project_id)
        effective_model = role.model_alias
        policy = TaskPolicy.from_dict({
            "task_type": role.node_type,
            "strategy": infer_strategy(task.description),
            "complexity": "M",
            "risk": "medium",
        })
        session = Session.create(
            project.project_id,
            task.title,
            SessionMode.TASK,
            workflow_id=f"role-{role.role_id}",
            policy=policy,
            purpose="collaboration",
        )
        session.add_message("user", task.description, metadata={"task_id": task.task_id})
        session.context = session.context.merge({"inputs": {
            "request": task.description,
            "project": self._project_context(project),
            "workspace": self.workspace_runtime.snapshot(project),
            "collaboration": {
                "task": task.to_dict(),
                "role": role.to_dict(),
                "handoffs": [item.to_dict() for item in handoffs],
            },
        }})
        self.sessions.save(session, session.session_id)
        workflow = WorkflowDefinition(
            workflow_id=f"role-{role.role_id}",
            label=f"{role.label} · {task.title}",
            description="由协同调度器生成的单角色执行单元。",
            nodes=[WorkflowNode(
                node_id=task.task_id,
                node_type=role.node_type,
                model_alias=effective_model,
                prompt_template=role.instructions,
                on_failure="human",
            )],
        )
        result = self.orchestrator.run(session, workflow)
        status = {
            "completed": TaskStatus.COMPLETED,
            "waiting_for_human": TaskStatus.BLOCKED,
            "needs_replan": TaskStatus.BLOCKED,
        }.get(result.status, TaskStatus.FAILED)
        error = result.context.errors[-1] if result.context.errors else ""
        return TaskOutcome(status, result.session_id, self._collaboration_result(result), error)

    def resource_status(self) -> dict[str, Any]:
        return {
            "providers": [item.to_dict() for item in self.resources.list_providers()],
            "models": [item.to_dict() for item in self.resources.list_models()],
            "health": self.resources.health(),
        }

    def save_provider(self, value: dict[str, Any]) -> ModelProvider:
        provider = ModelProvider.from_dict(value)
        return self.resources.save_provider(provider, api_key=str(value.get("api_key", "")))

    def save_model(self, value: dict[str, Any]) -> ModelAlias:
        return self.resources.save_model(ModelAlias.from_dict(value))

    def discover_provider_models(self, provider_id: str, protocol: str = "") -> dict[str, Any]:
        provider = next((item for item in self.resources.list_providers() if item.provider_id == provider_id), None)
        if provider is None:
            raise KeyError(provider_id)
        selected_protocol = protocol or provider.protocols[0]
        discover = getattr(self.gateway, "list_models", None)
        if not callable(discover):
            raise ValueError("当前模型网关不支持自动获取模型列表。")
        return {
            "provider_id": provider.provider_id,
            "protocol": selected_protocol,
            "models": discover(provider.provider_id, selected_protocol),
        }

    def test_provider(self, provider_id: str) -> dict[str, Any]:
        provider = next((item for item in self.resources.list_providers() if item.provider_id == provider_id), None)
        if provider is None:
            raise KeyError(provider_id)
        if not provider.enabled:
            result = {"ok": False, "error_type": "provider_disabled", "error": "供应商已停用。", "checks": []}
        else:
            enabled = [item for item in self.resources.list_models() if item.provider_id == provider_id and item.enabled]
            representatives: dict[str, ModelAlias] = {}
            for model in enabled:
                representatives.setdefault(model.protocol or provider.protocols[0], model)
        if provider.enabled and not representatives:
            result = {"ok": False, "error_type": "no_models", "error": "供应商没有可测试的启用模型。", "checks": []}
        elif provider.enabled:
            probe = getattr(self.gateway, "probe", None)
            if not callable(probe):
                raise ValueError("configured model gateway does not support health checks")
            checks = [probe(model.alias) for model in representatives.values()]
            result = {
                "ok": all(item.get("ok") for item in checks),
                "error_type": next((str(item.get("error_type", "")) for item in checks if not item.get("ok")), ""),
                "error": next((str(item.get("error", "")) for item in checks if not item.get("ok")), ""),
                "checks": checks,
            }
        self.resources.record_health(provider_id, result)
        return {"provider_id": provider_id, **result}

    def delete_provider(self, provider_id: str) -> None:
        self.resources.delete_provider(provider_id)

    def ensure_model_deletable(self, alias: str) -> None:
        self.resources.resolve(alias)
        workflow_refs = [
            workflow.workflow_id
            for workflow in self.workflows.list()
            if any(node.model_alias == alias for node in workflow.nodes)
        ]
        node_refs = [item.node_type for item in self.registry.list() if item.default_model == alias]
        project_refs = [project.project_id for project in self.projects.list() if project.default_model == alias]
        references = []
        if workflow_refs:
            references.append(f"工作流 {', '.join(workflow_refs)}")
        if node_refs:
            references.append(f"节点 {', '.join(node_refs)}")
        if project_refs:
            references.append(f"项目 {', '.join(project_refs)}")
        if references:
            raise ValueError(f"模型仍被以下配置使用：{'；'.join(references)}。请先解除引用。")

    def delete_model(self, alias: str) -> None:
        self.ensure_model_deletable(alias)
        self.resources.delete_model(alias)

    def _record_event(self, event: OrchestrationEvent) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self._event_lock, self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append an ad-hoc orchestration event (background jobs, failures)."""
        self._record_event(OrchestrationEvent(event_type, session_id="", payload=payload))

    def read_events(self, *, after: int = 0, limit: int = 500) -> dict[str, Any]:
        """Tail the append-only event log; ``next`` is the follow-up cursor."""
        after = max(0, int(after))
        limit = max(1, int(limit))
        events: list[dict[str, Any]] = []
        total = 0
        next_cursor = after
        if self.events_path.is_file():
            with self._event_lock:
                lines = self.events_path.read_text(encoding="utf-8").splitlines()
            total = len(lines)
            window = lines[after:after + limit]
            next_cursor = after + len(window)
            for line in window:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    events.append(value)
        return {"events": events, "next": next_cursor, "total": total}

    @staticmethod
    def _collaboration_result(session: Session) -> dict[str, Any]:
        facts = dict(session.context.facts)
        checks = facts.get("checks")
        if isinstance(checks, list):
            facts["checks"] = [
                {
                    key: value
                    for key, value in check.items()
                    if key not in {"stdout", "stderr"}
                }
                for check in checks
                if isinstance(check, dict)
            ]
        result = {
            "facts": facts,
            "artifacts": dict(session.context.artifacts),
            "decisions": list(session.context.decisions[-30:]),
        }
        if len(json.dumps(result, ensure_ascii=False)) > 200_000:
            return {
                "facts": {"available_fields": sorted(facts)},
                "artifacts": dict(session.context.artifacts),
                "decisions": list(session.context.decisions[-10:]),
            }
        return result

    @staticmethod
    def _project_context(project: Project) -> dict[str, Any]:
        return {
            "project_id": project.project_id,
            "name": project.name,
            "instructions": project.instructions,
            "knowledge_refs": list(project.knowledge_refs),
            "default_model": project.default_model,
            "workspace_path": project.workspace_path,
            "validation_commands": [list(command) for command in project.validation_commands],
            "runtime_policy": dict(project.runtime_policy),
        }

    @staticmethod
    def _chat_project() -> Project:
        return Project(project_id=CHAT_PROJECT_ID, name="普通对话")

    def _session_project(self, session: Session) -> Project:
        if session.project_id == CHAT_PROJECT_ID:
            return self._chat_project()
        return self.get_project(session.project_id)

    def _message_model_alias(
        self,
        session: Session,
        model_alias: str | None,
    ) -> str:
        """Resolve an optional per-conversation model without changing projects."""

        if model_alias is None:
            selected = str(session.context.inputs.get("model_alias", "")).strip()
        else:
            selected = str(model_alias).strip()
        if selected:
            self.validate_model_alias(selected)
        return selected
