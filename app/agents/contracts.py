from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.contracts import Severity, new_id, utc_now


class AgentTaskStatus(str, Enum):
    PREPARING_WORKSPACE = "preparing_workspace"
    DRAFT = "draft"
    ANALYZING = "analyzing"
    WAITING_FOR_PLAN_APPROVAL = "waiting_for_plan_approval"
    EXECUTING = "executing"
    VALIDATING = "validating"
    REVIEWING = "reviewing"
    READY_TO_DELIVER = "ready_to_deliver"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


ALLOWED_AGENT_TASK_TRANSITIONS: dict[AgentTaskStatus, set[AgentTaskStatus]] = {
    AgentTaskStatus.DRAFT: {
        AgentTaskStatus.PREPARING_WORKSPACE,
        AgentTaskStatus.ANALYZING,
        AgentTaskStatus.CANCELLING,
    },
    AgentTaskStatus.PREPARING_WORKSPACE: {
        AgentTaskStatus.DRAFT,
        AgentTaskStatus.CANCELLING,
    },
    AgentTaskStatus.ANALYZING: {
        AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
        AgentTaskStatus.FAILED,
    },
    AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL: {AgentTaskStatus.EXECUTING},
    AgentTaskStatus.EXECUTING: {
        AgentTaskStatus.VALIDATING,
        AgentTaskStatus.BLOCKED,
        AgentTaskStatus.FAILED,
    },
    AgentTaskStatus.VALIDATING: {
        AgentTaskStatus.REVIEWING,
        AgentTaskStatus.BLOCKED,
        AgentTaskStatus.FAILED,
    },
    AgentTaskStatus.REVIEWING: {
        AgentTaskStatus.EXECUTING,
        AgentTaskStatus.READY_TO_DELIVER,
        AgentTaskStatus.BLOCKED,
        AgentTaskStatus.FAILED,
    },
    AgentTaskStatus.READY_TO_DELIVER: set(),
    AgentTaskStatus.BLOCKED: set(),
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
class AgentRequest:
    task_id: str
    role: str
    instructions: str
    workspace: Path
    access: AgentAccess
    policy: AgentPolicy = field(default_factory=AgentPolicy)
    session_id: str = ""


@dataclass
class AgentResult:
    succeeded: bool
    output: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ValidationResult:
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    schema_version: int = 1


@dataclass
class AgentTask:
    title: str
    requirement: str
    project_id: str = ""
    base_commit: str = ""
    target_branch: str = ""
    task_branch: str = ""
    workspace: str = ""
    task_id: str = field(default_factory=lambda: new_id("TASK"))
    status: AgentTaskStatus = AgentTaskStatus.DRAFT
    plan_version: int = 0
    approved_plan_version: int = 0
    iteration: int = 0
    run_count: int = 0
    sessions: dict[str, str] = field(default_factory=dict)
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
        base_commit=str(data.get("base_commit", "")),
        target_branch=str(data.get("target_branch", "")),
        task_branch=str(data.get("task_branch", "")),
        workspace=str(data.get("workspace", "")),
        task_id=str(data["task_id"]),
        status=AgentTaskStatus(data.get("status", AgentTaskStatus.DRAFT.value)),
        plan_version=int(data.get("plan_version", 0)),
        approved_plan_version=int(data.get("approved_plan_version", 0)),
        iteration=int(data.get("iteration", 0)),
        run_count=int(data.get("run_count", 0)),
        sessions={str(key): str(value) for key, value in data.get("sessions", {}).items()},
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
