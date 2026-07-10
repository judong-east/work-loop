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


@dataclass(frozen=True)
class ValidationCommand:
    name: str
    argv: list[str]


@dataclass(frozen=True)
class ProjectPolicy:
    validation_commands: list[ValidationCommand]
    protected_paths: list[str]
    timeout_seconds: int
    network: str = "deny"
    redact_patterns: list[str] = field(default_factory=list)
    schema_version: int = 1

    def required_commands(self, names: list[str]) -> list[ValidationCommand]:
        by_name = {command.name: command for command in self.validation_commands}
        missing = [name for name in names if name not in by_name]
        if missing:
            raise ValueError(
                f"验证命令未获项目策略允许：{', '.join(missing)}"
            )
        return [by_name[name] for name in names]


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
