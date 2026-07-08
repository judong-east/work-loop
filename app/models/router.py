from __future__ import annotations

from app.core.contracts import ModelProfile, ModelRoutingConfig


class ModelRouter:
    """按角色解析模型配置；未配置的角色回落 default。"""

    def __init__(self, config: ModelRoutingConfig):
        self.config = config

    def resolve(self, role: str) -> tuple[ModelProfile, bool]:
        profile_name = self.config.roles.get(role)
        fallback = profile_name is None
        if fallback:
            profile_name = self.config.roles["default"]
        return self.config.profiles[profile_name], fallback
