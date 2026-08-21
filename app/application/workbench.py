from __future__ import annotations

from pathlib import Path
from typing import Any

from app.domain.models import ModelAlias, ModelProvider, NodeDefinition, Project, Session, SessionMode, WorkflowDefinition, WorkflowNode
from app.domain.node_catalog import NodeCatalog
from app.domain.node_registry import NodeRegistry
from app.domain.orchestrator import DagOrchestrator, ModelGateway, OrchestrationEvent
from app.domain.workflow_catalog import WorkflowCatalog
from app.infrastructure.json_repository import JsonCollection
from app.infrastructure.model_gateway import OpenAICompatibleGateway
from app.infrastructure.resource_center import ResourceCenter


class WorkbenchService:
    """Application facade for projects, sessions, resources, and workflows."""

    def __init__(self, root: Path, *, gateway: ModelGateway | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry = NodeRegistry()
        self.nodes = NodeCatalog(self.root / "nodes.json", self.registry)
        self.resources = ResourceCenter(self.root / "resources")
        self.projects = JsonCollection(self.root / "projects", Project.from_dict, lambda value: value.to_dict())
        self.sessions = JsonCollection(self.root / "sessions", Session.from_dict, lambda value: value.to_dict())
        self.workflows = WorkflowCatalog(self.root / "workflows.json", self.registry)
        self.events_path = self.root / "events.jsonl"
        self.gateway = gateway or OpenAICompatibleGateway(self.resources)
        self.orchestrator = DagOrchestrator(self.registry, self.sessions, self.gateway, event_sink=self._record_event)
        self._ensure_default_workflow()

    def _ensure_default_workflow(self) -> None:
        if self.workflows.list():
            return
        self.workflows.save(
            WorkflowDefinition(
                workflow_id="default-task",
                label="默认任务流",
                description="需求梳理 → 计划制定 → 项目执行 → 审核 → 测试",
                builtin=True,
                nodes=[
                    WorkflowNode("requirement", "requirement"),
                    WorkflowNode("planning", "planning", ("requirement",)),
                    WorkflowNode("implementation", "implementation", ("planning",)),
                    WorkflowNode("review", "review", ("implementation",)),
                    WorkflowNode("testing", "testing", ("implementation", "review"), on_failure="skip"),
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

    def create_project(self, name: str, *, instructions: str = "", knowledge_refs: list[str] | None = None, default_model: str = "") -> Project:
        project = Project.create(name, instructions=instructions, knowledge_refs=knowledge_refs or [], default_model=default_model)
        return self.projects.save(project, project.project_id)

    def list_projects(self) -> list[Project]:
        return sorted(self.projects.list(), key=lambda item: item.updated_at, reverse=True)

    def get_project(self, project_id: str) -> Project:
        return self.projects.get(project_id)

    def create_session(self, project_id: str, title: str, *, mode: SessionMode = SessionMode.CHAT, workflow_id: str = "") -> Session:
        project = self.get_project(project_id)
        if mode is SessionMode.TASK:
            workflow_id = workflow_id or "default-task"
            self.workflows.get(workflow_id)
        session = Session.create(project_id, title, mode, workflow_id)
        session.context = session.context.merge({"inputs": {"project": {
            "project_id": project.project_id,
            "name": project.name,
            "instructions": project.instructions,
            "knowledge_refs": list(project.knowledge_refs),
            "default_model": project.default_model,
        }}})
        return self.sessions.save(session, session.session_id)

    def list_sessions(self, project_id: str = "") -> list[Session]:
        sessions = self.sessions.list()
        if project_id:
            sessions = [session for session in sessions if session.project_id == project_id]
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def get_session(self, session_id: str) -> Session:
        return self.sessions.get(session_id)

    def send_message(self, session_id: str, content: str) -> Session:
        session = self.get_session(session_id)
        if not content.strip():
            raise ValueError("message cannot be empty")
        session.add_message("user", content.strip())
        session.context = session.context.merge({"inputs": {"request": content.strip()}})
        if session.mode is SessionMode.CHAT:
            session.add_message("assistant", "消息已记录，等待模型运行时接入。", metadata={"mode": "chat"})
        return self.sessions.save(session, session.session_id)

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
        if not session.context.inputs.get("request"):
            latest = next((message.content for message in reversed(session.messages) if message.role == "user"), "")
            session.context = session.context.merge({"inputs": {"request": latest}})
        return self.orchestrator.run(session, workflow)

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

    def delete_model(self, alias: str) -> None:
        workflow_refs = [
            workflow.workflow_id
            for workflow in self.workflows.list()
            if any(node.model_alias == alias for node in workflow.nodes)
        ]
        node_refs = [item.node_type for item in self.registry.list() if item.default_model == alias]
        project_refs = [project.project_id for project in self.projects.list() if project.default_model == alias]
        references = workflow_refs + node_refs + project_refs
        if references:
            raise ValueError(f"model is still in use: {', '.join(references)}")
        self.resources.delete_model(alias)

    def _record_event(self, event: OrchestrationEvent) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(__import__("json").dumps(event.to_dict(), ensure_ascii=False) + "\n")
