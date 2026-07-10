from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.agents.contracts import AgentTask, agent_task_from_dict
from app.core.contracts import to_plain


class AgentTaskStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        path = self.root / task_id
        for child in ("workspace", "artifacts/plans", "artifacts/rounds", "artifacts/runs", "logs"):
            (path / child).mkdir(parents=True, exist_ok=True)
        return path

    def workspace_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "workspace"

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

    def write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(to_plain(data), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    def write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)

    def _validate_task_id(self, task_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", task_id):
            raise ValueError(f"task_id 不是安全的单段标识：{task_id!r}")
