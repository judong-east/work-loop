from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.contracts import TaskState, task_state_from_dict, to_plain, utc_now


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._initialized: set[str] = set()

    def task_dir(self, task_id: str) -> Path:
        path = self.root / task_id
        if task_id not in self._initialized:
            for child in [
                "inputs",
                "contexts",
                "artifacts",
                "evaluations",
                "decisions",
                "callbacks",
                "logs",
            ]:
                (path / child).mkdir(parents=True, exist_ok=True)
            self._initialized.add(task_id)
        return path

    def save_task(self, task: TaskState) -> Path:
        path = self.task_dir(task.task_id) / "state.json"
        self.write_json(path, task)
        return path

    def load_task(self, task_id: str) -> TaskState:
        path = self.root / task_id / "state.json"
        if not path.exists():
            raise FileNotFoundError(f"任务 {task_id} 不存在：{path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return task_state_from_dict(data)

    def append_audit(self, task_id: str, record_type: str, payload: dict[str, Any]) -> None:
        audit_path = self.task_dir(task_id) / "logs" / "audit.jsonl"
        record = {
            "time": utc_now(),
            "type": record_type,
            "payload": to_plain(payload),
        }
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(to_plain(data), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

