from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from app.agents.contracts import AgentTask, agent_task_from_dict
from app.core.atomic_files import write_json_atomic, write_text_atomic


class AgentTaskStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

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
        self.write_json(path, task)
        return path

    def load(self, task_id: str) -> AgentTask:
        self._validate_task_id(task_id)
        path = self.root / task_id / "workflow-state.json"
        if not path.is_file():
            raise FileNotFoundError(f"代理任务 {task_id} 不存在：{path}")
        return agent_task_from_dict(json.loads(path.read_text(encoding="utf-8")))

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

    def _validate_task_id(self, task_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", task_id):
            raise ValueError(f"task_id 不是安全的单段标识：{task_id!r}")
