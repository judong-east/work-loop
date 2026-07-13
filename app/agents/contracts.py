from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.contracts import Severity, new_id, utc_now


class AgentTaskStatus(str, Enum):
    PREPARING_WORKSPACE = "preparing_workspace"
    DRAFT = "draft"
    QUEUED_FOR_ANALYSIS = "queued_for_analysis"
    ANALYZING = "analyzing"
    WAITING_FOR_PLAN_APPROVAL = "waiting_for_plan_approval"
    QUEUED_FOR_EXECUTION = "queued_for_execution"
    QUEUED_FOR_RECOVERY = "queued_for_recovery"
    EXECUTING = "executing"
    VALIDATING = "validating"
    REVIEWING = "reviewing"
    REPLANNING = "replanning"
    INTERRUPTED = "interrupted"
    PAUSED = "paused"
    READY_TO_DELIVER = "ready_to_deliver"
    INTEGRATION_REQUIRED = "integration_required"
    INTEGRATING = "integrating"
    DELIVERED = "delivered"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


ALLOWED_AGENT_TASK_TRANSITIONS: dict[AgentTaskStatus, set[AgentTaskStatus]] = {
    AgentTaskStatus.DRAFT: {
        AgentTaskStatus.PREPARING_WORKSPACE,
        AgentTaskStatus.QUEUED_FOR_ANALYSIS,
        AgentTaskStatus.ANALYZING,
        AgentTaskStatus.CANCELLING,
    },
    AgentTaskStatus.PREPARING_WORKSPACE: {
        AgentTaskStatus.DRAFT,
        AgentTaskStatus.CANCELLING,
    },
    AgentTaskStatus.QUEUED_FOR_ANALYSIS: {
        AgentTaskStatus.ANALYZING,
        AgentTaskStatus.INTERRUPTED,
        AgentTaskStatus.CANCELLING,
    },
    AgentTaskStatus.ANALYZING: {
        AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
        AgentTaskStatus.INTERRUPTED,
        AgentTaskStatus.PAUSED,
        AgentTaskStatus.CANCELLING,
        AgentTaskStatus.FAILED,
    },
    AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL: {
        AgentTaskStatus.QUEUED_FOR_ANALYSIS,
        AgentTaskStatus.QUEUED_FOR_EXECUTION,
        AgentTaskStatus.EXECUTING,
        AgentTaskStatus.CANCELLING,
    },
    AgentTaskStatus.QUEUED_FOR_EXECUTION: {
        AgentTaskStatus.EXECUTING,
        AgentTaskStatus.INTERRUPTED,
        AgentTaskStatus.CANCELLING,
    },
    AgentTaskStatus.QUEUED_FOR_RECOVERY: {
        AgentTaskStatus.ANALYZING,
        AgentTaskStatus.EXECUTING,
        AgentTaskStatus.VALIDATING,
        AgentTaskStatus.REVIEWING,
        AgentTaskStatus.REPLANNING,
        AgentTaskStatus.INTERRUPTED,
        AgentTaskStatus.PAUSED,
        AgentTaskStatus.CANCELLING,
        AgentTaskStatus.FAILED,
    },
    AgentTaskStatus.EXECUTING: {
        AgentTaskStatus.VALIDATING,
        AgentTaskStatus.BLOCKED,
        AgentTaskStatus.INTERRUPTED,
        AgentTaskStatus.PAUSED,
        AgentTaskStatus.CANCELLING,
        AgentTaskStatus.FAILED,
    },
    AgentTaskStatus.VALIDATING: {
        AgentTaskStatus.REVIEWING,
        AgentTaskStatus.BLOCKED,
        AgentTaskStatus.INTERRUPTED,
        AgentTaskStatus.PAUSED,
        AgentTaskStatus.CANCELLING,
        AgentTaskStatus.FAILED,
    },
    AgentTaskStatus.REVIEWING: {
        AgentTaskStatus.EXECUTING,
        AgentTaskStatus.REPLANNING,
        AgentTaskStatus.READY_TO_DELIVER,
        AgentTaskStatus.BLOCKED,
        AgentTaskStatus.INTERRUPTED,
        AgentTaskStatus.PAUSED,
        AgentTaskStatus.CANCELLING,
        AgentTaskStatus.FAILED,
    },
    AgentTaskStatus.REPLANNING: {
        AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
        AgentTaskStatus.INTERRUPTED,
        AgentTaskStatus.PAUSED,
        AgentTaskStatus.CANCELLING,
        AgentTaskStatus.FAILED,
    },
    AgentTaskStatus.INTERRUPTED: {
        AgentTaskStatus.QUEUED_FOR_RECOVERY,
        AgentTaskStatus.CANCELLING,
    },
    AgentTaskStatus.PAUSED: {
        AgentTaskStatus.QUEUED_FOR_RECOVERY,
        AgentTaskStatus.CANCELLING,
    },
    AgentTaskStatus.READY_TO_DELIVER: {
        AgentTaskStatus.INTEGRATION_REQUIRED,
        AgentTaskStatus.DELIVERED,
    },
    AgentTaskStatus.INTEGRATION_REQUIRED: {
        AgentTaskStatus.INTEGRATING,
        AgentTaskStatus.CANCELLING,
    },
    AgentTaskStatus.INTEGRATING: {
        AgentTaskStatus.VALIDATING,
        AgentTaskStatus.INTEGRATION_REQUIRED,
        AgentTaskStatus.BLOCKED,
        AgentTaskStatus.CANCELLING,
        AgentTaskStatus.FAILED,
    },
    AgentTaskStatus.DELIVERED: set(),
    AgentTaskStatus.BLOCKED: {AgentTaskStatus.QUEUED_FOR_RECOVERY},
    AgentTaskStatus.FAILED: set(),
    AgentTaskStatus.CANCELLING: {AgentTaskStatus.CANCELLED},
    AgentTaskStatus.CANCELLED: set(),
}


class AgentAccess(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


@dataclass
class AgentPolicy:
    allowed_commands: list[list[str]] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    timeout_seconds: int = 300
    network_allowed: bool = False
    redact_patterns: list[str] = field(default_factory=list)


@dataclass
class AgentBudget:
    total_timeout_seconds: float = 1800
    idle_timeout_seconds: float = 120
    max_cost_usd: float | None = None


@dataclass
class TaskBudget:
    total_timeout_seconds: float = 7200
    call_timeout_seconds: float = 1800
    idle_timeout_seconds: float = 120
    max_cost_usd: float | None = None
    max_iterations: int = 3
    consumed_active_seconds: float = 0.0
    consumed_cost_usd: float = 0.0

    def validate(self) -> None:
        if (
            self.total_timeout_seconds <= 0
            or self.call_timeout_seconds <= 0
            or self.idle_timeout_seconds <= 0
        ):
            raise ValueError("任务时间预算必须是正数。")
        if self.max_cost_usd is not None and self.max_cost_usd <= 0:
            raise ValueError("任务费用预算必须是正数。")
        if self.max_iterations <= 0:
            raise ValueError("任务最大返修轮次必须大于 0。")
        if self.consumed_active_seconds < 0 or self.consumed_cost_usd < 0:
            raise ValueError("任务已消耗预算不能为负数。")


class AgentEventType(str, Enum):
    SESSION_STARTED = "session_started"
    MESSAGE_DELTA = "message_delta"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    USAGE_UPDATED = "usage_updated"
    HEARTBEAT = "heartbeat"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class AgentEvent:
    event_type: AgentEventType
    role: str
    data: dict[str, Any] = field(default_factory=dict)
    raw_type: str = ""
    at: str = field(default_factory=utc_now)
    schema_version: int = 1


class ReviewVerdict(str, Enum):
    PASS = "pass"
    REVISE_CODE = "revise_code"
    REPLAN = "replan"
    BLOCKED = "blocked"


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串。")
    return value


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} 必须是字符串数组。")
    return list(value)


@dataclass
class ExecutionPlan:
    requirement_understanding: str
    non_goals: list[str]
    files_and_symbols: list[str]
    steps: list[str]
    constraints: list[str]
    acceptance_criteria: list[str]
    required_tests: list[str]
    risks: list[str]
    open_questions: list[str]
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionPlan":
        if not isinstance(data, dict):
            raise ValueError("规划结果必须是对象。")
        plan = cls(
            requirement_understanding=_string(data, "requirement_understanding"),
            non_goals=_string_list(data, "non_goals"),
            files_and_symbols=_string_list(data, "files_and_symbols"),
            steps=_string_list(data, "steps"),
            constraints=_string_list(data, "constraints"),
            acceptance_criteria=_string_list(data, "acceptance_criteria"),
            required_tests=_string_list(data, "required_tests"),
            risks=_string_list(data, "risks"),
            open_questions=_string_list(data, "open_questions"),
        )
        if not plan.steps:
            raise ValueError("steps 不能为空。")
        if not plan.acceptance_criteria:
            raise ValueError("acceptance_criteria 不能为空。")
        if not plan.required_tests:
            raise ValueError("required_tests 至少包含一项项目策略允许的确定性验证。")
        if len(set(plan.acceptance_criteria)) != len(plan.acceptance_criteria):
            raise ValueError("acceptance_criteria 不能包含重复项。")
        return plan


@dataclass
class ExecutionTestResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionTestResult":
        if not isinstance(data, dict):
            raise ValueError("每项测试结果必须是对象。")
        exit_code = data.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise ValueError("测试结果 exit_code 必须是整数。")
        return cls(
            command=_string(data, "command"),
            exit_code=exit_code,
            stdout=_string(data, "stdout"),
            stderr=_string(data, "stderr"),
        )


@dataclass
class ExecutionResult:
    completed_steps: list[str]
    modified_files: list[str]
    tests: list[ExecutionTestResult]
    deviations: list[str]
    remaining_risks: list[str]
    next_steps: list[str]
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionResult":
        if not isinstance(data, dict):
            raise ValueError("执行结果必须是对象。")
        raw_tests = data.get("tests")
        if not isinstance(raw_tests, list):
            raise ValueError("tests 必须是测试结果数组。")
        return cls(
            completed_steps=_string_list(data, "completed_steps"),
            modified_files=_string_list(data, "modified_files"),
            tests=[ExecutionTestResult.from_dict(item) for item in raw_tests],
            deviations=_string_list(data, "deviations"),
            remaining_risks=_string_list(data, "remaining_risks"),
            next_steps=_string_list(data, "next_steps"),
        )


@dataclass
class AcceptanceResult:
    criterion: str
    passed: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AcceptanceResult":
        if not isinstance(data, dict) or not isinstance(data.get("passed"), bool):
            raise ValueError("每项验收结果必须包含 criterion 字符串和 passed 布尔值。")
        return cls(criterion=_string(data, "criterion"), passed=data["passed"])


@dataclass
class ReviewIssue:
    file: str
    line: int
    severity: Severity
    message: str
    suggestion: str
    evidence: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewIssue":
        if not isinstance(data, dict):
            raise ValueError("每项审核问题必须是对象。")
        line = data.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 0:
            raise ValueError("审核问题 line 必须是非负整数。")
        try:
            severity = Severity(_string(data, "severity"))
        except ValueError as error:
            raise ValueError("审核问题 severity 必须是 info、warning 或 blocker。") from error
        message = _string(data, "message").strip()
        evidence = _string(data, "evidence").strip()
        if not message or not evidence:
            raise ValueError("审核问题必须包含非空 message 和 evidence。")
        return cls(
            file=_string(data, "file"),
            line=line,
            severity=severity,
            message=message,
            suggestion=_string(data, "suggestion"),
            evidence=evidence,
        )


@dataclass
class ReviewResult:
    verdict: ReviewVerdict
    acceptance: list[AcceptanceResult]
    issues: list[ReviewIssue]
    recommended_tests: list[str]
    summary: str
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewResult":
        if not isinstance(data, dict):
            raise ValueError("审核结果必须是对象。")
        try:
            verdict = ReviewVerdict(data.get("verdict"))
        except ValueError as error:
            raise ValueError("verdict 必须是 pass、revise_code、replan 或 blocked。") from error
        raw_acceptance = data.get("acceptance")
        raw_issues = data.get("issues")
        if not isinstance(raw_acceptance, list):
            raise ValueError("acceptance 必须是数组。")
        if not isinstance(raw_issues, list):
            raise ValueError("issues 必须是对象数组。")
        result = cls(
            verdict=verdict,
            acceptance=[AcceptanceResult.from_dict(item) for item in raw_acceptance],
            issues=[ReviewIssue.from_dict(item) for item in raw_issues],
            recommended_tests=_string_list(data, "recommended_tests"),
            summary=_string(data, "summary"),
        )
        if result.verdict is ReviewVerdict.REVISE_CODE and not result.issues:
            raise ValueError("revise_code 必须包含至少一个可执行审核问题。")
        return result

    def validate_pass(self, plan: ExecutionPlan) -> None:
        if self.verdict is not ReviewVerdict.PASS:
            return
        criteria = [item.criterion for item in self.acceptance]
        duplicates = sorted({criterion for criterion in criteria if criteria.count(criterion) > 1})
        if duplicates:
            raise ValueError(f"审核不能通过：验收结论重复：{', '.join(duplicates)}")
        outcomes = {item.criterion: item.passed for item in self.acceptance}
        missing = [criterion for criterion in plan.acceptance_criteria if criterion not in outcomes]
        failed = [criterion for criterion in plan.acceptance_criteria if outcomes.get(criterion) is False]
        unexpected = [criterion for criterion in outcomes if criterion not in plan.acceptance_criteria]
        blockers = [issue.message for issue in self.issues if issue.severity is Severity.BLOCKER]
        if missing or failed or unexpected or blockers:
            detail = "；".join(
                part
                for part in (
                    f"缺少验收结论：{', '.join(missing)}" if missing else "",
                    f"验收未通过：{', '.join(failed)}" if failed else "",
                    f"存在未知验收项：{', '.join(unexpected)}" if unexpected else "",
                    f"仍有阻断问题：{', '.join(blockers)}" if blockers else "",
                )
                if part
            )
            raise ValueError(f"审核不能通过：{detail}")


@dataclass
class DeliveryReport:
    requirement_summary: str
    acceptance: list[AcceptanceResult]
    modified_files: list[str]
    implementation_summary: list[str]
    validation_evidence: list[dict[str, Any]]
    review_verdict: str
    review_summary: str
    known_risks: list[str]
    human_next_steps: list[str]
    task_branch: str
    target_branch: str
    target_commit: str
    task_commit: str
    generated_at: str = field(default_factory=utc_now)
    schema_version: int = 1


def delivery_report_from_dict(data: dict[str, Any]) -> DeliveryReport:
    if not isinstance(data, dict):
        raise ValueError("DeliveryReport 必须是对象。")
    raw_acceptance = data.get("acceptance")
    evidence = data.get("validation_evidence")
    if not isinstance(raw_acceptance, list):
        raise ValueError("DeliveryReport acceptance 必须是数组。")
    if not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence):
        raise ValueError("DeliveryReport validation_evidence 必须是对象数组。")
    return DeliveryReport(
        requirement_summary=_string(data, "requirement_summary"),
        acceptance=[AcceptanceResult.from_dict(item) for item in raw_acceptance],
        modified_files=_string_list(data, "modified_files"),
        implementation_summary=_string_list(data, "implementation_summary"),
        validation_evidence=[dict(item) for item in evidence],
        review_verdict=_string(data, "review_verdict"),
        review_summary=_string(data, "review_summary"),
        known_risks=_string_list(data, "known_risks"),
        human_next_steps=_string_list(data, "human_next_steps"),
        task_branch=_string(data, "task_branch"),
        target_branch=_string(data, "target_branch"),
        target_commit=_string(data, "target_commit"),
        task_commit=_string(data, "task_commit"),
        generated_at=str(data.get("generated_at", utc_now())),
        schema_version=int(data.get("schema_version", 1)),
    )


@dataclass
class AgentRequest:
    task_id: str
    role: str
    instructions: str
    workspace: Path
    access: AgentAccess
    policy: AgentPolicy = field(default_factory=AgentPolicy)
    budget: AgentBudget = field(default_factory=AgentBudget)
    session_id: str = ""


@dataclass
class AgentResult:
    succeeded: bool
    output: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    error: str = ""
    error_type: str = ""
    final_message: str = ""
    events: list[AgentEvent] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    runtime: str = ""
    runtime_version: str = ""
    model: str = ""
    runtime_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationResult":
        if not isinstance(data, dict) or not isinstance(data.get("passed"), bool):
            raise ValueError("验证结果必须包含 passed 布尔值。")
        checks = data.get("checks", [])
        if not isinstance(checks, list) or not all(isinstance(item, dict) for item in checks):
            raise ValueError("验证结果 checks 必须是对象数组。")
        return cls(
            passed=data["passed"],
            checks=[dict(item) for item in checks],
            error=str(data.get("error", "")),
            schema_version=int(data.get("schema_version", 1)),
        )


@dataclass
class AgentTask:
    title: str
    requirement: str
    project_id: str = ""
    workflow_id: str = "guarded"
    workflow: dict[str, Any] = field(default_factory=dict)
    base_commit: str = ""
    target_branch: str = ""
    task_branch: str = ""
    workspace: str = ""
    delivery_base_commit: str = ""
    task_commit: str = ""
    delivery_target_commit: str = ""
    delivered_commit: str = ""
    integration_count: int = 0
    task_id: str = field(default_factory=lambda: new_id("TASK"))
    status: AgentTaskStatus = AgentTaskStatus.DRAFT
    plan_version: int = 0
    approved_plan_version: int = 0
    iteration: int = 0
    plan_iteration: int = 0
    run_count: int = 0
    queue_position: int = 0
    active_operation: str = ""
    recovery_mode: str = ""
    interrupted_status: str = ""
    pause_reason: str = ""
    budget: TaskBudget = field(default_factory=TaskBudget)
    sessions: dict[str, str] = field(default_factory=dict)
    clarifications: list[dict[str, str]] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    transitions: list[dict[str, str]] = field(default_factory=list)
    error: str = ""
    schema_version: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def transition(self, status: AgentTaskStatus, reason: str = "") -> None:
        if status not in ALLOWED_AGENT_TASK_TRANSITIONS[self.status]:
            raise ValueError(f"不允许任务从 {self.status.value} 迁移到 {status.value}。")
        previous = self.status
        at = utc_now()
        self.status = status
        self.updated_at = at
        self.transitions.append(
            {"from": previous.value, "to": status.value, "reason": reason, "at": at}
        )


def agent_task_from_dict(data: dict[str, Any]) -> AgentTask:
    return AgentTask(
        title=str(data["title"]),
        requirement=str(data["requirement"]),
        project_id=str(data.get("project_id", "")),
        workflow_id=str(data.get("workflow_id", "guarded")),
        workflow=dict(data.get("workflow", {})) if isinstance(data.get("workflow", {}), dict) else {},
        base_commit=str(data.get("base_commit", "")),
        target_branch=str(data.get("target_branch", "")),
        task_branch=str(data.get("task_branch", "")),
        workspace=str(data.get("workspace", "")),
        delivery_base_commit=str(data.get("delivery_base_commit", "")),
        task_commit=str(data.get("task_commit", "")),
        delivery_target_commit=str(data.get("delivery_target_commit", "")),
        delivered_commit=str(data.get("delivered_commit", "")),
        integration_count=int(data.get("integration_count", 0)),
        task_id=str(data["task_id"]),
        status=AgentTaskStatus(data.get("status", AgentTaskStatus.DRAFT.value)),
        plan_version=int(data.get("plan_version", 0)),
        approved_plan_version=int(data.get("approved_plan_version", 0)),
        iteration=int(data.get("iteration", 0)),
        plan_iteration=int(data.get("plan_iteration", data.get("iteration", 0))),
        run_count=int(data.get("run_count", 0)),
        queue_position=int(data.get("queue_position", 0)),
        active_operation=str(data.get("active_operation", "")),
        recovery_mode=str(data.get("recovery_mode", "")),
        interrupted_status=str(data.get("interrupted_status", "")),
        pause_reason=str(data.get("pause_reason", "")),
        budget=task_budget_from_dict(data.get("budget", {})),
        sessions={str(key): str(value) for key, value in data.get("sessions", {}).items()},
        clarifications=[
            {str(key): str(value) for key, value in item.items()}
            for item in data.get("clarifications", [])
            if isinstance(item, dict)
        ],
        artifacts={str(key): str(value) for key, value in data.get("artifacts", {}).items()},
        transitions=[
            {str(key): str(value) for key, value in item.items()}
            for item in data.get("transitions", [])
            if isinstance(item, dict)
        ],
        error=str(data.get("error", "")),
        schema_version=int(data.get("schema_version", 1)),
        created_at=str(data.get("created_at", utc_now())),
        updated_at=str(data.get("updated_at", utc_now())),
    )


def task_budget_from_dict(data: Any) -> TaskBudget:
    source = data if isinstance(data, dict) else {}
    budget = TaskBudget(
        total_timeout_seconds=float(source.get("total_timeout_seconds", 7200)),
        call_timeout_seconds=float(source.get("call_timeout_seconds", 1800)),
        idle_timeout_seconds=float(source.get("idle_timeout_seconds", 120)),
        max_cost_usd=(
            float(source["max_cost_usd"])
            if source.get("max_cost_usd") is not None
            else None
        ),
        max_iterations=int(source.get("max_iterations", 3)),
        consumed_active_seconds=float(source.get("consumed_active_seconds", 0.0)),
        consumed_cost_usd=float(source.get("consumed_cost_usd", 0.0)),
    )
    budget.validate()
    return budget
