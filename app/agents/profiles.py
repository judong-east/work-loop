from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.core.atomic_files import write_json_atomic
from app.models.config import load_routing_config


ROLE_RUNTIME = {
    "planner": ("claude_code", "read_only"),
    "executor": ("codex_cli", "workspace_write"),
    "reviewer": ("claude_code", "read_only"),
}


@dataclass(frozen=True)
class AgentProfile:
    role: str
    runtime: str
    model: str
    access: str


def load_agent_profiles(path: Path) -> dict[str, AgentProfile]:
    config_path = Path(path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    raw_roles = data.get("roles", {})
    if not isinstance(raw_roles, dict):
        raise ValueError("AgentProfile roles 必须是对象。")
    profiles: dict[str, AgentProfile] = {}
    for role, (runtime, access) in ROLE_RUNTIME.items():
        raw = raw_roles.get(role)
        if not isinstance(raw, dict):
            raise ValueError(f"AgentProfile 缺少角色 {role}。")
        if "command" in raw:
            raise ValueError("AgentProfile 禁止配置任意 command 模板。")
        selected_runtime = str(raw.get("runtime", ""))
        selected_access = str(raw.get("access", ""))
        if selected_runtime != runtime or selected_access != access:
            raise ValueError(
                f"角色 {role} 必须使用 runtime={runtime}、access={access}。"
            )
        profiles[role] = AgentProfile(
            role=role,
            runtime=runtime,
            model=str(raw.get("model", "")).strip(),
            access=access,
        )
    return profiles


def migrate_legacy_profiles(source: Path, destination: Path) -> dict:
    routing = load_routing_config(source)
    roles = {}
    for role, (runtime, access) in ROLE_RUNTIME.items():
        profile_name = routing.roles.get(role, routing.roles["default"])
        legacy = routing.profiles[profile_name]
        roles[role] = {
            "runtime": runtime,
            "model": legacy.model,
            "access": access,
        }
    payload = {
        "schema_version": 1,
        "roles": roles,
        "migration": {
            "source": str(Path(source)),
            "unsafe_templates_discarded": True,
        },
    }
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(destination, payload)
    return payload
