from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


class TaskStatus(str, Enum):
    CREATED = "created"
    CONTEXT_BUILDING = "context_building"
    CLARIFICATION_REQUIRED = "clarification_required"
    POLICY_BLOCKED = "policy_blocked"
    READY_FOR_PLAN = "ready_for_plan"
    READY_FOR_IMPLEMENTATION = "ready_for_implementation"
    VALIDATION = "validation"
    DONE = "done"
    FAILED = "failed"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class DecisionAction(str, Enum):
    CONTINUE = "continue"
    REQUEST_HUMAN_INPUT = "request_human_input"
    BLOCK = "block"
    COMPLETE = "complete"


@dataclass
class SourceRef:
    uri: str
    title: str = ""
    trust_level: str = "unknown"
    captured_at: str = field(default_factory=utc_now)


@dataclass
class ContextSection:
    name: str
    content: str
    source_refs: list[SourceRef] = field(default_factory=list)
    confidence: float = 0.0
    tags: list[str] = field(default_factory=list)


@dataclass
class ContextPack:
    task_id: str
    purpose: str
    sections: list[ContextSection] = field(default_factory=list)
    missing_context: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    context_id: str = field(default_factory=lambda: new_id("CTX"))
    created_at: str = field(default_factory=utc_now)


@dataclass
class PolicyBoundary:
    allow_paths: list[str] = field(default_factory=list)
    deny_paths: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    restricted_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    max_iterations: int = 5
    min_context_confidence: float = 0.65
    require_human_for_conflicts: bool = True
    distinct_model_roles: list[list[str]] = field(default_factory=lambda: [["executor", "reviewer"]])


@dataclass
class PolicyCheck:
    passed: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_human: bool = False


@dataclass
class EvaluationIssue:
    message: str
    severity: Severity = Severity.WARNING
    issue_type: str = "general"
    suggested_action: str = ""


@dataclass
class EvaluationResult:
    evaluator: str
    status: str
    score: float
    issues: list[EvaluationIssue] = field(default_factory=list)
    blocking: bool = False
    created_at: str = field(default_factory=utc_now)


@dataclass
class DecisionResult:
    action: DecisionAction
    reason: str
    confidence: float
    next_state: TaskStatus
    required_inputs: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)


@dataclass
class CallbackEvent:
    task_id: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: new_id("EVT"))
    created_at: str = field(default_factory=utc_now)


@dataclass
class TaskState:
    title: str
    goal: str
    task_id: str = field(default_factory=lambda: new_id("TASK"))
    status: TaskStatus = TaskStatus.CREATED
    inputs: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    context_refs: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    evaluations: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    risk_level: str = "medium"
    iteration: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def transition(self, status: TaskStatus) -> None:
        self.status = status
        self.updated_at = utc_now()


def to_plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    return value


def source_ref_from_dict(data: dict[str, Any]) -> SourceRef:
    return SourceRef(**data)


def context_section_from_dict(data: dict[str, Any]) -> ContextSection:
    refs = [source_ref_from_dict(item) for item in data.get("source_refs", [])]
    return ContextSection(
        name=data["name"],
        content=data["content"],
        source_refs=refs,
        confidence=float(data.get("confidence", 0.0)),
        tags=list(data.get("tags", [])),
    )


def context_pack_from_dict(data: dict[str, Any]) -> ContextPack:
    return ContextPack(
        task_id=data["task_id"],
        purpose=data["purpose"],
        sections=[context_section_from_dict(item) for item in data.get("sections", [])],
        missing_context=list(data.get("missing_context", [])),
        conflicts=list(data.get("conflicts", [])),
        context_id=data.get("context_id", new_id("CTX")),
        created_at=data.get("created_at", utc_now()),
    )


def evaluation_issue_from_dict(data: dict[str, Any]) -> EvaluationIssue:
    return EvaluationIssue(
        message=data["message"],
        severity=Severity(data.get("severity", Severity.WARNING.value)),
        issue_type=data.get("issue_type", "general"),
        suggested_action=data.get("suggested_action", ""),
    )


def evaluation_result_from_dict(data: dict[str, Any]) -> EvaluationResult:
    return EvaluationResult(
        evaluator=data["evaluator"],
        status=data["status"],
        score=float(data["score"]),
        issues=[evaluation_issue_from_dict(item) for item in data.get("issues", [])],
        blocking=bool(data.get("blocking", False)),
        created_at=data.get("created_at", utc_now()),
    )


@dataclass
class FileChange:
    path: str  # 相对 workspace 的正斜杠路径
    content: str = ""  # action=delete 时忽略
    action: str = "write"  # write | delete


@dataclass
class CodeReviewIssue:
    file: str
    message: str
    line: int = 0
    severity: Severity = Severity.WARNING
    suggestion: str = ""


@dataclass
class CodeReviewResult:
    verdict: str  # pass | revise | block
    issues: list[CodeReviewIssue] = field(default_factory=list)
    summary: str = ""


@dataclass
class ModelProfile:
    name: str
    provider: str
    model: str
    command: list[str] = field(default_factory=list)
    timeout_seconds: int = 300


@dataclass
class ModelRequest:
    task_id: str
    role: str
    prompt: str


@dataclass
class ModelResponse:
    text: str
    profile_name: str
    model: str
    duration_seconds: float
    succeeded: bool
    error: str = ""


@dataclass
class ModelRoutingConfig:
    profiles: dict[str, ModelProfile] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)


@dataclass
class ExperienceRecord:
    text: str
    kind: str = "manual"  # review_pattern | clarification | manual
    status: str = "pending"  # pending | approved | rejected
    source_task: str = ""
    experience_id: str = field(default_factory=lambda: new_id("EXP"))
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


def experience_record_from_dict(data: dict[str, Any]) -> ExperienceRecord:
    return ExperienceRecord(
        text=data["text"],
        kind=data.get("kind", "manual"),
        status=data.get("status", "pending"),
        source_task=data.get("source_task", ""),
        experience_id=data.get("experience_id", new_id("EXP")),
        created_at=data.get("created_at", utc_now()),
        updated_at=data.get("updated_at", utc_now()),
    )


def task_state_from_dict(data: dict[str, Any]) -> TaskState:
    return TaskState(
        title=data["title"],
        goal=data["goal"],
        task_id=data["task_id"],
        status=TaskStatus(data.get("status", TaskStatus.CREATED.value)),
        inputs=list(data.get("inputs", [])),
        artifacts=dict(data.get("artifacts", {})),
        context_refs=list(data.get("context_refs", [])),
        decisions=list(data.get("decisions", [])),
        evaluations=list(data.get("evaluations", [])),
        events=list(data.get("events", [])),
        risk_level=data.get("risk_level", "medium"),
        iteration=int(data.get("iteration", 0)),
        created_at=data.get("created_at", utc_now()),
        updated_at=data.get("updated_at", utc_now()),
    )


