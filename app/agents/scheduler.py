from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from app.agents.contracts import AgentTask, AgentTaskStatus, TaskBudget
from app.agents.workflow import AgentWorkflow
from app.core.atomic_files import write_json_atomic
from app.core.contracts import new_id, utc_now


class QueueOperation(str, Enum):
    ANALYZE = "analyze"
    EXECUTE = "execute"
    RECOVER = "recover"


class QueueEntryStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


@dataclass
class QueueEntry:
    task_id: str
    operation: QueueOperation
    sequence: int
    status: QueueEntryStatus = QueueEntryStatus.QUEUED
    recovery_mode: str = ""
    entry_id: str = field(default_factory=lambda: new_id("QUEUE"))
    enqueued_at: str = field(default_factory=utc_now)
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    schema_version: int = 1


@dataclass
class QueueState:
    entries: list[QueueEntry] = field(default_factory=list)
    next_sequence: int = 1
    schema_version: int = 1


class AgentQueueStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "queue-state.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self) -> QueueState:
        if not self.path.is_file():
            return QueueState()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        entries = [
            QueueEntry(
                task_id=str(item["task_id"]),
                operation=QueueOperation(item["operation"]),
                sequence=int(item["sequence"]),
                status=QueueEntryStatus(item.get("status", "queued")),
                recovery_mode=str(item.get("recovery_mode", "")),
                entry_id=str(item["entry_id"]),
                enqueued_at=str(item.get("enqueued_at", "")),
                started_at=str(item.get("started_at", "")),
                finished_at=str(item.get("finished_at", "")),
                error=str(item.get("error", "")),
                schema_version=int(item.get("schema_version", 1)),
            )
            for item in data.get("entries", [])
            if isinstance(item, dict)
        ]
        return QueueState(
            entries=entries,
            next_sequence=int(data.get("next_sequence", 1)),
            schema_version=int(data.get("schema_version", 1)),
        )

    def save(self, state: QueueState) -> None:
        write_json_atomic(self.path, state)


class PersistentAgentScheduler:
    """Persistent FIFO scheduler with one local execution slot."""

    def __init__(self, workflow: AgentWorkflow):
        self.workflow = workflow
        self.store = AgentQueueStore(workflow.root / "queue")
        self._state_lock = threading.RLock()
        self._execution_slot = threading.Lock()
        self.scan_startup()

    def enqueue_analysis(self, task_id: str) -> QueueEntry:
        return self._enqueue(
            task_id,
            QueueOperation.ANALYZE,
            AgentTaskStatus.DRAFT,
            AgentTaskStatus.QUEUED_FOR_ANALYSIS,
        )

    def enqueue_execution(self, task_id: str) -> QueueEntry:
        plan = self.workflow.get_plan(task_id)
        if plan.open_questions:
            raise ValueError("计划仍有未决问题，不能进入执行队列。")
        return self._enqueue(
            task_id,
            QueueOperation.EXECUTE,
            AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            AgentTaskStatus.QUEUED_FOR_EXECUTION,
        )

    def answer_clarification(self, task_id: str, answer: str) -> QueueEntry:
        with self._state_lock:
            self.workflow.record_clarification(task_id, answer)
            return self._enqueue(
                task_id,
                QueueOperation.ANALYZE,
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
                AgentTaskStatus.QUEUED_FOR_ANALYSIS,
            )

    def resume(self, task_id: str) -> QueueEntry:
        return self._enqueue_recovery(task_id, "resume")

    def rerun(self, task_id: str) -> QueueEntry:
        return self._enqueue_recovery(task_id, "rerun")

    def update_budget(self, task_id: str, budget: TaskBudget) -> AgentTask:
        budget.validate()
        with self._state_lock:
            task = self.workflow.get_task(task_id)
            if task.status not in {AgentTaskStatus.PAUSED, AgentTaskStatus.INTERRUPTED}:
                raise ValueError("只有 paused/interrupted 任务可以调整冻结预算。")
            if budget.total_timeout_seconds <= task.budget.consumed_active_seconds:
                raise ValueError("新的总时间预算必须大于已消耗时间。")
            if (
                budget.max_cost_usd is not None
                and budget.max_cost_usd <= task.budget.consumed_cost_usd
            ):
                raise ValueError("新的费用预算必须大于已消耗费用。")
            budget.consumed_active_seconds = task.budget.consumed_active_seconds
            budget.consumed_cost_usd = task.budget.consumed_cost_usd
            task.budget = budget
            self.workflow.store.save(task)
            return task

    def pending(self) -> list[QueueEntry]:
        return self._entries_with_status(QueueEntryStatus.QUEUED)

    def running(self) -> list[QueueEntry]:
        return self._entries_with_status(QueueEntryStatus.RUNNING)

    def interrupted(self) -> list[QueueEntry]:
        return self._entries_with_status(QueueEntryStatus.INTERRUPTED)

    def paused(self) -> list[QueueEntry]:
        return self._entries_with_status(QueueEntryStatus.PAUSED)

    def run_next(self) -> AgentTask | None:
        if not self._execution_slot.acquire(blocking=False):
            return None
        try:
            with self._state_lock:
                state = self.store.load()
                if any(entry.status is QueueEntryStatus.RUNNING for entry in state.entries):
                    return None
                queued = sorted(
                    (
                        entry
                        for entry in state.entries
                        if entry.status is QueueEntryStatus.QUEUED
                    ),
                    key=lambda item: item.sequence,
                )
                if not queued:
                    return None
                entry = queued[0]
                entry.status = QueueEntryStatus.RUNNING
                entry.started_at = utc_now()
                self.store.save(state)
                task = self.workflow.get_task(entry.task_id)
                task.queue_position = 0
                task.active_operation = entry.operation.value
                self.workflow.store.save(task)
                self._refresh_positions(state)

            try:
                result = self._dispatch(entry)
            except Exception as error:
                self._interrupt_entry(entry.entry_id, str(error))
                raise
            else:
                if result.status is AgentTaskStatus.FAILED:
                    status = QueueEntryStatus.FAILED
                elif result.status is AgentTaskStatus.PAUSED:
                    status = QueueEntryStatus.PAUSED
                elif result.status is AgentTaskStatus.CANCELLED:
                    status = QueueEntryStatus.CANCELLED
                else:
                    status = QueueEntryStatus.COMPLETED
                self._finish_entry(entry.entry_id, status, result.error)
                return self.workflow.get_task(entry.task_id)
        finally:
            self._execution_slot.release()

    def terminate(self, task_id: str) -> AgentTask:
        task = self.workflow.cancel_task(task_id)
        with self._state_lock:
            state = self.store.load()
            for entry in state.entries:
                if entry.task_id == task_id and entry.status in {
                    QueueEntryStatus.QUEUED,
                    QueueEntryStatus.RUNNING,
                    QueueEntryStatus.INTERRUPTED,
                }:
                    entry.status = QueueEntryStatus.CANCELLED
                    entry.finished_at = utc_now()
            self.store.save(state)
            self._refresh_positions(state)
        return task

    def scan_startup(self) -> list[str]:
        interrupted_ids: list[str] = []
        with self._state_lock:
            state = self.store.load()
            self._reconcile_queued_state(state)
            by_task = {entry.task_id: entry for entry in state.entries}
            for entry in state.entries:
                if entry.status is not QueueEntryStatus.RUNNING:
                    continue
                task = self.workflow.get_task(entry.task_id)
                if self._is_active_task(task):
                    entry.status = QueueEntryStatus.INTERRUPTED
                    entry.finished_at = utc_now()
                    entry.error = "服务重启时运行仍未终结。"
                    self._interrupt_task(entry.task_id, entry.operation)
                    interrupted_ids.append(entry.task_id)
                else:
                    entry.status = self._terminal_queue_status(task)
                    entry.finished_at = utc_now()
                    entry.error = task.error
                    task.active_operation = ""
                    task.queue_position = 0
                    self.workflow.store.save(task)

            for task_id in self._task_ids():
                running_paths = self._running_agent_runs(task_id)
                validation_paths = self._running_validation_runs(task_id)
                task = self.workflow.get_task(task_id)
                active = self._is_running_phase(task)
                if not running_paths and not validation_paths and not active:
                    continue
                operation = self._operation_for_task(task)
                existing = by_task.get(task_id)
                if existing is None or existing.status not in {
                    QueueEntryStatus.RUNNING,
                    QueueEntryStatus.INTERRUPTED,
                }:
                    entry = QueueEntry(
                        task_id=task_id,
                        operation=operation,
                        sequence=state.next_sequence,
                        status=QueueEntryStatus.INTERRUPTED,
                        started_at="",
                        finished_at=utc_now(),
                        error="服务重启时发现未终结 AgentRun。",
                    )
                    state.next_sequence += 1
                    state.entries.append(entry)
                    by_task[task_id] = entry
                elif existing.status is QueueEntryStatus.RUNNING:
                    existing.status = QueueEntryStatus.INTERRUPTED
                if task.status is not AgentTaskStatus.INTERRUPTED:
                    self._interrupt_task(task_id, operation)
                if task_id not in interrupted_ids:
                    interrupted_ids.append(task_id)
                for path in running_paths:
                    self._interrupt_run(path)
                for path in validation_paths:
                    self._interrupt_validation_run(path)

            self.store.save(state)
            self._refresh_positions(state)
        return interrupted_ids

    def _reconcile_queued_state(self, state: QueueState) -> None:
        queued_by_task = {
            entry.task_id: entry
            for entry in state.entries
            if entry.status is QueueEntryStatus.QUEUED
        }
        expected_status = {
            QueueOperation.ANALYZE: AgentTaskStatus.QUEUED_FOR_ANALYSIS,
            QueueOperation.EXECUTE: AgentTaskStatus.QUEUED_FOR_EXECUTION,
            QueueOperation.RECOVER: AgentTaskStatus.QUEUED_FOR_RECOVERY,
        }
        previous_statuses = {
            QueueOperation.ANALYZE: {
                AgentTaskStatus.DRAFT,
            },
            QueueOperation.EXECUTE: {
                AgentTaskStatus.WAITING_FOR_PLAN_APPROVAL,
            },
            QueueOperation.RECOVER: {
                AgentTaskStatus.INTERRUPTED,
                AgentTaskStatus.PAUSED,
            },
        }
        for task_id, entry in queued_by_task.items():
            task = self.workflow.get_task(task_id)
            expected = expected_status[entry.operation]
            if task.status is expected:
                continue
            if task.status in previous_statuses[entry.operation]:
                task.transition(expected, reason="startup_queue_reconcile")
                task.active_operation = entry.operation.value
                if entry.operation is QueueOperation.RECOVER:
                    task.recovery_mode = entry.recovery_mode or "resume"
                self.workflow.store.save(task)
                continue
            entry.status = self._terminal_queue_status(task)
            entry.finished_at = utc_now()
            entry.error = task.error

        for task_id in self._task_ids():
            task = self.workflow.get_task(task_id)
            if task.status not in {
                AgentTaskStatus.QUEUED_FOR_ANALYSIS,
                AgentTaskStatus.QUEUED_FOR_EXECUTION,
                AgentTaskStatus.QUEUED_FOR_RECOVERY,
            } or task_id in queued_by_task:
                continue
            operation = {
                AgentTaskStatus.QUEUED_FOR_ANALYSIS: QueueOperation.ANALYZE,
                AgentTaskStatus.QUEUED_FOR_EXECUTION: QueueOperation.EXECUTE,
                AgentTaskStatus.QUEUED_FOR_RECOVERY: QueueOperation.RECOVER,
            }[task.status]
            entry = QueueEntry(
                task_id=task_id,
                operation=operation,
                recovery_mode=task.recovery_mode or (
                    "resume" if operation is QueueOperation.RECOVER else ""
                ),
                sequence=state.next_sequence,
            )
            state.next_sequence += 1
            state.entries.append(entry)

    def _enqueue(
        self,
        task_id: str,
        operation: QueueOperation,
        expected: AgentTaskStatus,
        queued_status: AgentTaskStatus,
    ) -> QueueEntry:
        with self._state_lock:
            state = self.store.load()
            self._require_not_active(state, task_id)
            task = self.workflow.get_task(task_id)
            if task.status is not expected:
                raise ValueError(
                    f"任务 {task_id} 状态为 {task.status.value}，不能排队 {operation.value}。"
                )
            task.transition(queued_status, reason=f"queued_{operation.value}")
            task.active_operation = operation.value
            entry = QueueEntry(
                task_id=task_id,
                operation=operation,
                sequence=state.next_sequence,
            )
            state.next_sequence += 1
            state.entries.append(entry)
            self.workflow.store.save(task)
            self.store.save(state)
            self._refresh_positions(state)
            return entry

    def _enqueue_recovery(self, task_id: str, mode: str) -> QueueEntry:
        with self._state_lock:
            state = self.store.load()
            self._require_not_active(state, task_id)
            task = self.workflow.get_task(task_id)
            legacy_validation_block = (
                task.status is AgentTaskStatus.BLOCKED
                and task.error.startswith("验证 ")
            )
            if task.status not in {AgentTaskStatus.INTERRUPTED, AgentTaskStatus.PAUSED} and not legacy_validation_block:
                raise ValueError(f"任务 {task_id} 不是可恢复的 interrupted/paused 状态。")
            if legacy_validation_block:
                task.interrupted_status = AgentTaskStatus.VALIDATING.value
                task.pause_reason = "validation_failed"
            if not task.interrupted_status:
                raise ValueError(f"任务 {task_id} 缺少中断阶段。")
            for previous in state.entries:
                if (
                    previous.task_id == task_id
                    and previous.status
                    in {QueueEntryStatus.INTERRUPTED, QueueEntryStatus.PAUSED}
                ):
                    previous.status = QueueEntryStatus.RESOLVED
                    previous.error = (
                        previous.error + f"；用户选择 {mode}。"
                    ).lstrip("；")
            task.transition(
                AgentTaskStatus.QUEUED_FOR_RECOVERY,
                reason=f"queued_recovery_{mode}",
            )
            task.active_operation = QueueOperation.RECOVER.value
            task.recovery_mode = mode
            entry = QueueEntry(
                task_id=task_id,
                operation=QueueOperation.RECOVER,
                recovery_mode=mode,
                sequence=state.next_sequence,
            )
            state.next_sequence += 1
            state.entries.append(entry)
            self.workflow.store.save(task)
            self.store.save(state)
            self._refresh_positions(state)
            return entry

    def _dispatch(self, entry: QueueEntry) -> AgentTask:
        if entry.operation is QueueOperation.ANALYZE:
            return self.workflow.analyze(entry.task_id)
        if entry.operation is QueueOperation.EXECUTE:
            return self.workflow.approve_plan(entry.task_id)
        return self.workflow.resume_interrupted(
            entry.task_id,
            rerun=entry.recovery_mode == "rerun",
        )

    def _finish_entry(
        self,
        entry_id: str,
        status: QueueEntryStatus,
        error: str,
    ) -> None:
        with self._state_lock:
            state = self.store.load()
            entry = next(item for item in state.entries if item.entry_id == entry_id)
            entry.status = status
            entry.finished_at = utc_now()
            entry.error = error
            task = self.workflow.get_task(entry.task_id)
            task.queue_position = 0
            task.active_operation = ""
            task.recovery_mode = ""
            self.workflow.store.save(task)
            self.store.save(state)
            self._refresh_positions(state)

    def _interrupt_entry(self, entry_id: str, error: str) -> None:
        with self._state_lock:
            state = self.store.load()
            entry = next(item for item in state.entries if item.entry_id == entry_id)
            task = self.workflow.get_task(entry.task_id)
            recoverable = task.status in {
                AgentTaskStatus.QUEUED_FOR_ANALYSIS,
                AgentTaskStatus.QUEUED_FOR_EXECUTION,
                AgentTaskStatus.QUEUED_FOR_RECOVERY,
                AgentTaskStatus.ANALYZING,
                AgentTaskStatus.EXECUTING,
                AgentTaskStatus.VALIDATING,
                AgentTaskStatus.REVIEWING,
                AgentTaskStatus.REPLANNING,
            }
            entry.status = (
                QueueEntryStatus.INTERRUPTED
                if recoverable
                else QueueEntryStatus.FAILED
            )
            entry.finished_at = utc_now()
            entry.error = error
            if recoverable:
                self._interrupt_task(entry.task_id, entry.operation)
            self.store.save(state)
            self._refresh_positions(state)

    def _refresh_positions(self, state: QueueState) -> None:
        queued = sorted(
            (entry for entry in state.entries if entry.status is QueueEntryStatus.QUEUED),
            key=lambda item: item.sequence,
        )
        positions = {entry.task_id: index for index, entry in enumerate(queued, start=1)}
        for task_id in self._task_ids():
            task = self.workflow.get_task(task_id)
            position = positions.get(task_id, 0)
            if task.queue_position != position:
                task.queue_position = position
                self.workflow.store.save(task)

    def _interrupt_task(self, task_id: str, operation: QueueOperation) -> None:
        task = self.workflow.get_task(task_id)
        previous = task.status
        if previous is AgentTaskStatus.INTERRUPTED:
            return
        if previous in {
            AgentTaskStatus.QUEUED_FOR_ANALYSIS,
            AgentTaskStatus.QUEUED_FOR_EXECUTION,
            AgentTaskStatus.QUEUED_FOR_RECOVERY,
        }:
            interrupted_status = {
                QueueOperation.ANALYZE: AgentTaskStatus.ANALYZING.value,
                QueueOperation.EXECUTE: AgentTaskStatus.EXECUTING.value,
                QueueOperation.RECOVER: task.interrupted_status,
            }[operation]
        else:
            interrupted_status = previous.value
        task.transition(AgentTaskStatus.INTERRUPTED, reason="startup_scan")
        task.interrupted_status = interrupted_status
        task.active_operation = operation.value
        task.queue_position = 0
        task.error = "服务重启中断了正在运行的阶段。"
        self.workflow.store.save(task)

    def _interrupt_run(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "interrupted"
        data["finished_at"] = utc_now()
        data["error_type"] = "interrupted"
        data["error"] = "服务重启时 AgentRun 仍处于 running。"
        self.workflow.store.write_json(path, data)

    def _interrupt_validation_run(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "interrupted"
        data["finished_at"] = utc_now()
        data["error"] = "服务重启时 ValidationRun 仍处于 running。"
        self.workflow.store.write_json(path, data)

    def _running_agent_runs(self, task_id: str) -> list[Path]:
        run_dir = self.workflow.store.task_dir(task_id) / "artifacts" / "runs"
        paths = []
        for path in run_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("status") == "running":
                paths.append(path)
        return paths

    def _running_validation_runs(self, task_id: str) -> list[Path]:
        round_root = self.workflow.store.task_dir(task_id) / "artifacts" / "rounds"
        paths = []
        for path in round_root.glob("*/validation-run.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("status") == "running":
                paths.append(path)
        return paths

    def _task_ids(self) -> list[str]:
        return sorted(
            path.parent.name
            for path in self.workflow.store.root.glob("*/workflow-state.json")
        )

    def _operation_for_task(self, task: AgentTask) -> QueueOperation:
        if task.status in {
            AgentTaskStatus.ANALYZING,
            AgentTaskStatus.QUEUED_FOR_ANALYSIS,
        } or (
            task.status is AgentTaskStatus.INTERRUPTED
            and task.interrupted_status == AgentTaskStatus.ANALYZING.value
        ):
            return QueueOperation.ANALYZE
        return QueueOperation.EXECUTE

    @staticmethod
    def _is_active_task(task: AgentTask) -> bool:
        return task.status in {
            AgentTaskStatus.QUEUED_FOR_ANALYSIS,
            AgentTaskStatus.QUEUED_FOR_EXECUTION,
            AgentTaskStatus.QUEUED_FOR_RECOVERY,
            AgentTaskStatus.ANALYZING,
            AgentTaskStatus.EXECUTING,
            AgentTaskStatus.VALIDATING,
            AgentTaskStatus.REVIEWING,
            AgentTaskStatus.REPLANNING,
        }

    @staticmethod
    def _is_running_phase(task: AgentTask) -> bool:
        return task.status in {
            AgentTaskStatus.ANALYZING,
            AgentTaskStatus.EXECUTING,
            AgentTaskStatus.VALIDATING,
            AgentTaskStatus.REVIEWING,
            AgentTaskStatus.REPLANNING,
        }

    @staticmethod
    def _terminal_queue_status(task: AgentTask) -> QueueEntryStatus:
        if task.status is AgentTaskStatus.FAILED:
            return QueueEntryStatus.FAILED
        if task.status is AgentTaskStatus.PAUSED:
            return QueueEntryStatus.PAUSED
        if task.status is AgentTaskStatus.CANCELLED:
            return QueueEntryStatus.CANCELLED
        return QueueEntryStatus.COMPLETED

    def _entries_with_status(self, status: QueueEntryStatus) -> list[QueueEntry]:
        with self._state_lock:
            return sorted(
                (
                    entry
                    for entry in self.store.load().entries
                    if entry.status is status
                ),
                key=lambda item: item.sequence,
            )

    @staticmethod
    def _require_not_active(state: QueueState, task_id: str) -> None:
        if any(
            entry.task_id == task_id
            and entry.status
            in {
                QueueEntryStatus.QUEUED,
                QueueEntryStatus.RUNNING,
            }
            for entry in state.entries
        ):
            raise ValueError(f"任务 {task_id} 已在队列中。")
