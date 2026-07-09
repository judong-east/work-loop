from __future__ import annotations

import json
from pathlib import Path

from app.core.contracts import ModelProfile, ModelRoutingConfig


def load_routing_config(path: Path) -> ModelRoutingConfig:
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(
            f"模型配置文件 {config_path} 不存在。离线试跑可用 models_smoke.json，"
            "真实模型请参照 README 创建 models.json。"
        )
    data = json.loads(config_path.read_text(encoding="utf-8"))

    profiles: dict[str, ModelProfile] = {}
    for item in data.get("profiles", []):
        profile = ModelProfile(
            name=item["name"],
            provider=item["provider"],
            model=item["model"],
            command=list(item.get("command", [])),
            timeout_seconds=int(item.get("timeout_seconds", 300)),
        )
        if profile.name in profiles:
            raise ValueError(f"模型配置名重复：{profile.name}。")
        if profile.timeout_seconds <= 0:
            raise ValueError(f"模型配置 {profile.name} 的 timeout_seconds 必须大于 0。")
        if profile.provider == "cli":
            if not profile.command:
                raise ValueError(f"CLI 模型配置 {profile.name} 缺少 command。")
            if not any("{prompt}" in part for part in profile.command):
                raise ValueError(f"CLI 模型配置 {profile.name} 的 command 缺少 {{prompt}} 占位符。")
        profiles[profile.name] = profile

    roles = {str(key): str(value) for key, value in data.get("roles", {}).items()}
    if "default" not in roles:
        raise ValueError("roles 必须包含 default 兜底角色。")
    for role, profile_name in roles.items():
        if profile_name not in profiles:
            raise ValueError(f"角色 {role} 引用了不存在的模型配置 {profile_name}。")

    return ModelRoutingConfig(profiles=profiles, roles=roles)
