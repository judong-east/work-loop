from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from app.agents.contracts import AgentTask, agent_task_from_dict
from app.core.atomic_files import write_json_atomic, write_text_atomic
from app.core.contracts import utc_now


class AgentTaskStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._events_lock = threading.Lock()
        self._last_change_keys: dict[str, tuple] = {}

    def task_dir(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        path = self.root / task_id
        for child in ("artifacts/plans", "artifacts/rounds", "artifacts/runs", "logs"):
            (path / child).mkdir(parents=True, exist_ok=True)
        return path

    def workspace_location(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "workspace"

    def workspace_path(self, task_id: str) -> Path:
        path = self.workspace_location(task_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, task: AgentTask) -> Path:
        path = self.task_dir(task.task_id) / "workflow-state.json"
        created = not path.exists()
        self.write_json(path, task)
        self._emit_change_event(task, created)
        return path

    def load(self, task_id: str) -> AgentTask:
        self._validate_task_id(task_id)
        path = self.root / task_id / "workflow-state.json"
        if not path.is_file():
            raise FileNotFoundError(f"代理任务 {task_id} 不存在：{path}")
        task = agent_task_from_dict(json.loads(path.read_text(encoding="utf-8")))
        # Seed the change detector so the next save only emits when this
        # process actually observes a transition, not because it just loaded.
        self._last_change_keys.setdefault(task.task_id, self._change_key(task))
        return task

    def list_all(self) -> list[AgentTask]:
        tasks: list[AgentTask] = []
        for path in sorted(self.root.glob("*/workflow-state.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                tasks.append(agent_task_from_dict(data))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
                continue
        return tasks

    def write_json(self, path: Path, data: Any) -> None:
        write_json_atomic(path, data)

    def write_text(self, path: Path, text: str) -> None:
        write_text_atomic(path, text)

    def delete(self, task_id: str) -> None:
        self._validate_task_id(task_id)
        path = self.root / task_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        with self._events_lock:
            self._last_change_keys.pop(task_id, None)

    # ---- append-only event log ----

    def append_event(self, task_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Append one JSON line to the task's trajectory log.

        The log is the seed of an event-sourced history: every meaningful
        lifecycle change lands here exactly once, in order, and anything that
        needs the past (live streaming, audit, future replay) reads it.
        """
        self._validate_task_id(task_id)
        with self._events_lock:
            path = self.task_dir(task_id) / "logs" / "events.jsonl"
            record = {
                "seq": self._event_count(path) + 1,
                "at": utc_now(),
                **event,
            }
            with path.open("a", encoding="utf-8") as stream:
                # A crash-torn tail (no trailing newline) must not swallow this
                # record onto the same corrupt line: terminate it first.
                if self._ends_without_newline(path):
                    stream.write("\n")
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return record

    def read_events(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        self._validate_task_id(task_id)
        path = self.root / task_id / "logs" / "events.jsonl"
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn tail line never fails the whole stream
                if isinstance(record, dict) and int(record.get("seq", 0)) > after:
                    events.append(record)
        return events

    @staticmethod
    def _ends_without_newline(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with path.open("rb") as probe:
            probe.seek(-1, 2)
            return probe.read(1) != b"\n"

    @staticmethod
    def _event_count(path: Path) -> int:
        if not path.is_file():
            return 0
        with path.open("r", encoding="utf-8") as stream:
            return sum(1 for line in stream if line.strip())

    @staticmethod
    def _change_key(task: AgentTask) -> tuple:
        return (
            task.status.value,
            task.error,
            task.pause_reason,
            task.workflow_cursor,
            task.iteration,
            task.plan_iteration,
            task.revision_target_node_id,
        )

    def _emit_change_event(self, task: AgentTask, created: bool) -> None:
        key = self._change_key(task)
        previous = self._last_change_keys.get(task.task_id)
        self._last_change_keys[task.task_id] = key
        if created:
            self.append_event(
                task.task_id,
                {
                    "type": "task_created",
                    "status": task.status.value,
                    "title": task.title,
                },
            )
            return
        if previous is None or previous == key:
            return
        self.append_event(
            task.task_id,
            {
                "type": "task_changed",
                "status": task.status.value,
                "workflow_cursor": task.workflow_cursor,
                "iteration": task.iteration,
                "error": task.error,
                "pause_reason": task.pause_reason,
            },
        )

    def _validate_task_id(self, task_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", task_id):
            raise ValueError(f"task_id 不是安全的单段标识：{task_id!r}")
