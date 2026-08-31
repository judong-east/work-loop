from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.contracts import new_id, utc_now


_PROTOCOL_ALIASES = {
    "openai": "openai",
    "openai_chat": "openai",
    "claude": "claude",
    "anthropic": "claude",
}


def _protocol(value: Any) -> str:
    return _PROTOCOL_ALIASES.get(str(value).strip().lower(), str(value).strip().lower())


class SessionMode(str, Enum):
    CHAT = "chat"
    TASK = "task"


@dataclass
class TaskPolicy:
    """Durable governance metadata for a task session.

    Policy is intentionally separate from workflow topology: it records why a
    task is being run and where human/quality gates stand without changing the
    provider, model, or node contracts already used by the workbench.
    """

    task_type: str = "feature"
    complexity: str = "M"
    risk: str = "medium"
    strategy: str = "guided-develop"
    current_phase: str = "analysis"
    next_action: str = ""
    gate: str = ""
    gate_status: str = "open"
    revision: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        if self.complexity not in {"S", "M", "L", "XL"}:
            raise ValueError("complexity must be S, M, L, or XL")
        if self.risk not in {"low", "medium", "high"}:
            raise ValueError("risk must be low, medium, or high")
        if self.gate_status not in {"open", "blocked", "approved"}:
            raise ValueError("gate_status must be open, blocked, or approved")
        if not self.task_type.strip() or not self.strategy.strip():
            raise ValueError("task_type and strategy are required")
        # Import lazily so the model module remains the dependency root while
        # strategy presets stay easy to extend independently.
        from .strategy_presets import STRATEGY_PRESETS
        if self.strategy not in STRATEGY_PRESETS:
            raise ValueError(f"unknown strategy: {self.strategy}")
        if self.revision < 0:
            raise ValueError("policy revision cannot be negative")
        if not isinstance(self.history, list):
            raise ValueError("policy history must be a list")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "task_type": self.task_type,
            "complexity": self.complexity,
            "risk": self.risk,
            "strategy": self.strategy,
            "current_phase": self.current_phase,
            "next_action": self.next_action,
            "gate": self.gate,
            "gate_status": self.gate_status,
            "revision": self.revision,
            "history": [dict(item) for item in self.history[-50:] if isinstance(item, dict)],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "TaskPolicy":
        value = value if isinstance(value, dict) else {}
        policy = cls(
            task_type=str(value.get("task_type", "feature")),
            complexity=str(value.get("complexity", "M")).upper(),
            risk=str(value.get("risk", "medium")).lower(),
            strategy=str(value.get("strategy", "guided-develop")),
            current_phase=str(value.get("current_phase", "analysis")),
            next_action=str(value.get("next_action", "")),
            gate=str(value.get("gate", "")),
            gate_status=str(value.get("gate_status", "open")),
            revision=int(value.get("revision", 0)),
            history=[dict(item) for item in value.get("history", []) if isinstance(item, dict)],
        )
        policy.validate()
        return policy


@dataclass
class ModelProvider:
    """A vendor connection without exposing a secret in serialized state."""

    provider_id: str
    label: str
    base_url: str
    protocols: list[str] = field(default_factory=lambda: ["openai"])
    auth_type: str = "bearer"
    auth_header: str = ""
    auth_prefix: str = "Bearer"
    credential_ref: str = ""
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def validate(self) -> None:
        if not self.provider_id or not self.provider_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("provider_id must be a safe identifier")
        if not self.label.strip():
            raise ValueError("provider label cannot be empty")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("provider base_url must be an http(s) URL")
        if not self.protocols:
            raise ValueError("provider must expose at least one protocol")
        self.protocols = list(dict.fromkeys(_protocol(item) for item in self.protocols))
        unsupported = set(self.protocols) - {"openai", "claude"}
        if unsupported:
            raise ValueError(f"unsupported provider protocols: {', '.join(sorted(unsupported))}")
        if self.auth_type not in {"bearer", "api_key", "token", "basic", "custom_header", "query_param", "none"}:
            raise ValueError("unsupported provider auth_type")
        if self.auth_type == "custom_header":
            safe_header = self.auth_header.replace("-", "")
            if not safe_header or not safe_header.isalnum():
                raise ValueError("custom auth header must be a safe HTTP header name")
        if self.auth_type == "query_param":
            safe_parameter = self.auth_header.replace("-", "").replace("_", "")
            if not safe_parameter or not safe_parameter.isalnum():
                raise ValueError("query auth parameter must be a safe name")
        if self.auth_type == "basic":
            username = str(self.metadata.get("username", ""))
            if not username or any(char in username for char in ":\r\n"):
                raise ValueError("basic auth username is required and cannot contain a colon or newline")
        if any(char in self.auth_prefix for char in "\r\n"):
            raise ValueError("auth prefix cannot contain newlines")
        if self.auth_type == "token" and self.auth_prefix.strip().lower() == "bearer":
            self.auth_prefix = "Token"

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "base_url": self.base_url.rstrip("/"),
            "protocols": list(self.protocols),
            "auth_type": self.auth_type,
            "auth_header": self.auth_header,
            "auth_prefix": self.auth_prefix,
            "credential_ref": self.credential_ref,
            "enabled": self.enabled,
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ModelProvider":
        protocols = [_protocol(item) for item in value.get("protocols", ["openai"])]
        auth_type = str(value.get("auth_type", "")).strip()
        if not auth_type:
            auth_type = "api_key" if protocols and protocols[0] == "claude" else "bearer"
        auth_header = str(value.get("auth_header", ""))
        metadata = dict(value.get("metadata", {}))
        if auth_type == "query_param" and not auth_header:
            auth_header = str(metadata.get("query_param", "api_key"))
        provider = cls(
            provider_id=str(value["provider_id"]),
            label=str(value.get("label", value["provider_id"])),
            base_url=str(value["base_url"]),
            protocols=protocols,
            auth_type=auth_type,
            auth_header=auth_header,
            auth_prefix=str(value.get("auth_prefix", "Bearer" if auth_type == "bearer" else "Token" if auth_type == "token" else "")),
            credential_ref=str(value.get("credential_ref", "")),
            enabled=bool(value.get("enabled", True)),
            metadata=metadata,
            schema_version=int(value.get("schema_version", 1)),
        )
        provider.validate()
        return provider


@dataclass
class ModelAlias:
    alias: str
    provider_id: str
    model: str
    capabilities: list[str] = field(default_factory=lambda: ["general"])
    protocol: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    enabled: bool = True
    schema_version: int = 1

    def validate(self) -> None:
        if not self.alias or not self.alias.replace("-", "").replace("_", "").isalnum():
            raise ValueError("model alias must be a safe identifier")
        if not self.provider_id.strip() or not self.model.strip():
            raise ValueError("model alias requires provider_id and model")
        self.protocol = _protocol(self.protocol) if self.protocol else ""
        if self.protocol and self.protocol not in {"openai", "claude"}:
            raise ValueError("model protocol must be openai or claude")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "alias": self.alias,
            "provider_id": self.provider_id,
            "model": self.model,
            "capabilities": list(self.capabilities),
            "protocol": self.protocol,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "enabled": self.enabled,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ModelAlias":
        alias = cls(
            alias=str(value["alias"]),
            provider_id=str(value["provider_id"]),
            model=str(value["model"]),
            capabilities=[str(item) for item in value.get("capabilities", ["general"])],
            protocol=_protocol(value.get("protocol", "")) if value.get("protocol") else "",
            temperature=(None if value.get("temperature") is None else float(value["temperature"])),
            max_tokens=(None if value.get("max_tokens") is None else int(value["max_tokens"])),
            enabled=bool(value.get("enabled", True)),
            schema_version=int(value.get("schema_version", 1)),
        )
        alias.validate()
        return alias


@dataclass
class Project:
    project_id: str
    name: str
    instructions: str = ""
    knowledge_refs: list[str] = field(default_factory=list)
    default_model: str = ""
    workspace_path: str = ""
    validation_commands: list[list[str]] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    schema_version: int = 1

    def validate(self) -> None:
        if not self.project_id.strip() or not self.name.strip():
            raise ValueError("project_id and name are required")
        if len(self.instructions) > 50_000:
            raise ValueError("project instructions are too long")
        if not isinstance(self.knowledge_refs, list) or not all(
            isinstance(item, str) for item in self.knowledge_refs
        ):
            raise ValueError("knowledge_refs must be a list of strings")
        if self.workspace_path:
            workspace = Path(self.workspace_path).expanduser()
            if not workspace.is_absolute():
                raise ValueError("workspace_path must be absolute")
            if not workspace.is_dir():
                raise ValueError(f"workspace_path is not a directory: {workspace}")
            self.workspace_path = str(workspace.resolve())
        if not isinstance(self.validation_commands, list):
            raise ValueError("validation_commands must be a list")
        if len(self.validation_commands) > 20:
            raise ValueError("a project can define at most 20 validation commands")
        for command in self.validation_commands:
            if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
                raise ValueError("each validation command must be a non-empty argv list")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "project_id": self.project_id,
            "name": self.name,
            "instructions": self.instructions,
            "knowledge_refs": list(self.knowledge_refs),
            "default_model": self.default_model,
            "workspace_path": self.workspace_path,
            "validation_commands": [list(command) for command in self.validation_commands],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def create(cls, name: str, **kwargs: Any) -> "Project":
        project = cls(project_id=new_id("PROJECT"), name=name.strip(), **kwargs)
        project.validate()
        return project

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Project":
        raw_knowledge_refs = value.get("knowledge_refs", [])
        if not isinstance(raw_knowledge_refs, list):
            raise ValueError("knowledge_refs must be a list")
        raw_commands = value.get("validation_commands", [])
        if not isinstance(raw_commands, list):
            raise ValueError("validation_commands must be a list")
        project = cls(
            project_id=str(value["project_id"]),
            name=str(value["name"]),
            instructions=str(value.get("instructions", "")),
            knowledge_refs=list(raw_knowledge_refs),
            default_model=str(value.get("default_model", "")),
            workspace_path=str(value.get("workspace_path", "")),
            validation_commands=[
                list(command) if isinstance(command, list) else command
                for command in raw_commands
            ],
            created_at=str(value.get("created_at", utc_now())),
            updated_at=str(value.get("updated_at", value.get("created_at", utc_now()))),
            schema_version=int(value.get("schema_version", 1)),
        )
        project.validate()
        return project


@dataclass
class SessionMessage:
    role: str
    content: str
    message_id: str = field(default_factory=lambda: new_id("MSG"))
    node_id: str = ""
    created_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        if self.role not in {"system", "user", "assistant", "tool", "event"}:
            raise ValueError(f"unsupported message role: {self.role}")
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "node_id": self.node_id,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SessionMessage":
        return cls(
            message_id=str(value.get("message_id", new_id("MSG"))),
            role=str(value["role"]),
            content=str(value.get("content", "")),
            node_id=str(value.get("node_id", "")),
            created_at=str(value.get("created_at", utc_now())),
            metadata=dict(value.get("metadata", {})),
        )


@dataclass
class ContextState:
    """Structured cross-node state; raw conversation history never flows implicitly."""

    facts: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    decisions: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    version: int = 1

    def merge(self, patch: dict[str, Any]) -> "ContextState":
        if not isinstance(patch, dict):
            raise ValueError("node output must be an object")
        facts_patch = patch.get("facts", {})
        artifacts_patch = patch.get("artifacts", {})
        decisions_patch = patch.get("decisions", [])
        inputs_patch = patch.get("inputs", {})
        errors_patch = patch.get("errors", [])
        if not isinstance(facts_patch, dict):
            raise ValueError("node output facts must be an object")
        if not isinstance(artifacts_patch, dict):
            raise ValueError("node output artifacts must be an object")
        if not isinstance(decisions_patch, list):
            raise ValueError("node output decisions must be a list")
        if not isinstance(inputs_patch, dict):
            raise ValueError("node output inputs must be an object")
        if not isinstance(errors_patch, list):
            raise ValueError("node output errors must be a list")
        facts = dict(self.facts)
        facts.update(facts_patch)
        # Node contracts may expose a field directly (for example ``result``)
        # or group it under ``facts``.  Keep both forms available to downstream
        # nodes while reserving envelope keys for the structured state buckets.
        reserved = {"facts", "artifacts", "decisions", "inputs", "errors"}
        facts.update({key: value for key, value in patch.items() if key not in reserved})
        artifacts = dict(self.artifacts)
        artifacts.update({str(k): str(v) for k, v in artifacts_patch.items()})
        decisions = list(dict.fromkeys(self.decisions + [str(item) for item in decisions_patch]))
        inputs = dict(self.inputs)
        inputs.update(inputs_patch)
        errors = self.errors + [str(item) for item in errors_patch]
        return ContextState(facts, artifacts, decisions, inputs, errors, self.version + 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "facts": dict(self.facts),
            "artifacts": dict(self.artifacts),
            "decisions": list(self.decisions),
            "inputs": dict(self.inputs),
            "errors": list(self.errors),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ContextState":
        return cls(
            facts=dict(value.get("facts", {})),
            artifacts={str(k): str(v) for k, v in dict(value.get("artifacts", {})).items()},
            decisions=[str(item) for item in value.get("decisions", [])],
            inputs=dict(value.get("inputs", {})),
            errors=[str(item) for item in value.get("errors", [])],
            version=int(value.get("version", 1)),
        )


@dataclass
class Session:
    session_id: str
    project_id: str
    title: str
    mode: SessionMode = SessionMode.CHAT
    purpose: str = "conversation"
    messages: list[SessionMessage] = field(default_factory=list)
    context: ContextState = field(default_factory=ContextState)
    workflow_id: str = ""
    policy: TaskPolicy = field(default_factory=TaskPolicy)
    status: str = "idle"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    schema_version: int = 1

    @classmethod
    def create(
        cls,
        project_id: str,
        title: str,
        mode: SessionMode = SessionMode.CHAT,
        workflow_id: str = "",
        *,
        policy: TaskPolicy | dict[str, Any] | None = None,
        purpose: str = "conversation",
    ) -> "Session":
        task_policy = policy if isinstance(policy, TaskPolicy) else TaskPolicy.from_dict(policy)
        return cls(
            new_id("SESSION"), project_id, title.strip() or "未命名会话", mode,
            purpose=purpose, workflow_id=workflow_id, policy=task_policy,
        )

    def add_message(self, role: str, content: str, *, node_id: str = "", metadata: dict[str, Any] | None = None) -> SessionMessage:
        message = SessionMessage(role=role, content=content, node_id=node_id, metadata=metadata or {})
        message.to_dict()
        self.messages.append(message)
        self.updated_at = utc_now()
        return message

    def to_dict(self) -> dict[str, Any]:
        if not self.project_id.strip():
            raise ValueError("session project_id is required")
        if self.purpose not in {"conversation", "collaboration"}:
            raise ValueError("unsupported session purpose")
        return {
            "session_id": self.session_id,
            "project_id": self.project_id,
            "title": self.title,
            "mode": self.mode.value,
            "purpose": self.purpose,
            "messages": [item.to_dict() for item in self.messages],
            "context": self.context.to_dict(),
            "workflow_id": self.workflow_id,
            "policy": self.policy.to_dict(),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Session":
        return cls(
            session_id=str(value["session_id"]),
            project_id=str(value["project_id"]),
            title=str(value.get("title", "未命名会话")),
            mode=SessionMode(value.get("mode", SessionMode.CHAT.value)),
            purpose=str(value.get("purpose", "conversation")),
            messages=[SessionMessage.from_dict(item) for item in value.get("messages", [])],
            context=ContextState.from_dict(dict(value.get("context", {}))),
            workflow_id=str(value.get("workflow_id", "")),
            policy=TaskPolicy.from_dict(value.get("policy")),
            status=str(value.get("status", "idle")),
            created_at=str(value.get("created_at", utc_now())),
            updated_at=str(value.get("updated_at", value.get("created_at", utc_now()))),
            schema_version=int(value.get("schema_version", 1)),
        )


@dataclass(frozen=True)
class NodeDefinition:
    node_type: str
    label: str
    description: str
    input_fields: tuple[str, ...] = ()
    output_fields: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ("general",)
    default_model: str = ""
    builtin: bool = False

    def validate_output(self, output: dict[str, Any]) -> None:
        if not isinstance(output, dict):
            raise ValueError(f"node {self.node_type} output must be an object")
        facts = output.get("facts", {}) if isinstance(output.get("facts", {}), dict) else {}
        missing = [field for field in self.output_fields if field not in output and field not in facts]
        if missing:
            raise ValueError(f"node {self.node_type} output missing: {', '.join(missing)}")


@dataclass(frozen=True)
class WorkflowNode:
    node_id: str
    node_type: str
    depends_on: tuple[str, ...] = ()
    model_alias: str = ""
    prompt_template: str = ""
    on_failure: str = "human"
    config: dict[str, Any] = field(default_factory=dict)
    position: tuple[float, float] = (0.0, 0.0)


@dataclass
class WorkflowDefinition:
    workflow_id: str
    label: str
    nodes: list[WorkflowNode]
    description: str = ""
    builtin: bool = False
    schema_version: int = 1

    def validate(self, registry: Any) -> None:
        if not self.workflow_id or not self.nodes:
            raise ValueError("workflow_id and nodes are required")
        ids = [node.node_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("workflow node ids must be unique")
        node_ids = set(ids)
        for node in self.nodes:
            if node.node_type not in registry:
                raise ValueError(f"unknown node type: {node.node_type}")
            unknown = set(node.depends_on) - node_ids
            if unknown:
                raise ValueError(f"node {node.node_id} depends on unknown nodes: {sorted(unknown)}")
            if node.on_failure not in {"retry", "human", "skip", "replan"}:
                raise ValueError(f"unsupported failure policy: {node.on_failure}")


@dataclass
class NodeRun:
    node_id: str
    attempt: int = 1
    status: str = "pending"
    model_alias: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "attempt": self.attempt,
            "status": self.status,
            "model_alias": self.model_alias,
            "output": dict(self.output),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NodeRun":
        return cls(
            node_id=str(value["node_id"]),
            attempt=int(value.get("attempt", 1)),
            status=str(value.get("status", "pending")),
            model_alias=str(value.get("model_alias", "")),
            output=dict(value.get("output", {})),
            error=str(value.get("error", "")),
            started_at=str(value.get("started_at", "")),
            finished_at=str(value.get("finished_at", "")),
        )
