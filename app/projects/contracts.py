from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.contracts import new_id, utc_now


@dataclass
class Project:
    name: str
    repository: str
    default_branch: str
    config_path: str = ".workloop/project.toml"
    project_id: str = field(default_factory=lambda: new_id("PROJECT"))
    created_at: str = field(default_factory=utc_now)
    schema_version: int = 1


def project_from_dict(data: dict[str, Any]) -> Project:
    return Project(
        name=str(data["name"]),
        repository=str(data["repository"]),
        default_branch=str(data["default_branch"]),
        config_path=str(data.get("config_path", ".workloop/project.toml")),
        project_id=str(data["project_id"]),
        created_at=str(data.get("created_at", utc_now())),
        schema_version=int(data.get("schema_version", 1)),
    )
