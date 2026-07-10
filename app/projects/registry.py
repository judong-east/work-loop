from __future__ import annotations

import json
import re
from pathlib import Path

from app.core.atomic_files import write_json_atomic
from app.projects.contracts import Project, project_from_dict


class ProjectRegistry:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def add(self, project: Project) -> Project:
        self._validate_project_id(project.project_id)
        write_json_atomic(self.root / f"{project.project_id}.json", project)
        return project

    def get(self, project_id: str) -> Project:
        self._validate_project_id(project_id)
        path = self.root / f"{project_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"项目 {project_id} 不存在：{path}")
        return project_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _validate_project_id(self, project_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", project_id):
            raise ValueError(f"project_id 不是安全的单段标识：{project_id!r}")
