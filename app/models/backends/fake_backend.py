from __future__ import annotations

from app.core.contracts import ModelProfile, ModelRequest, ModelResponse
from app.models.backends.base import ModelBackend

DEFAULT_CHANGES = (
    '{"changes": [{"path": "result.md", "action": "write", '
    '"content": "# 执行结果\\n按计划完成。\\n"}], "notes": "离线示例变更"}'
)
DEFAULT_REVIEW = '{"verdict": "pass", "issues": []}'


class FakeBackend(ModelBackend):
    """离线测试后端：按角色返回预置应答或预置失败；应答给 list 时按调用顺序消费。"""

    def __init__(self, responses: dict[str, str | list[str]] | None = None, failures: set[str] | None = None):
        self.responses = responses or {}
        self.failures = failures or set()
        self.requests: list[ModelRequest] = []
        self._cursor: dict[str, int] = {}

    def _next_response(self, role: str) -> str | None:
        if role not in self.responses:
            return None
        value = self.responses[role]
        if isinstance(value, str):
            return value
        if not value:
            return None
        # 序列应答：逐次前进，耗尽后停在最后一个，支撑多轮循环测试
        index = min(self._cursor.get(role, 0), len(value) - 1)
        self._cursor[role] = index + 1
        return value[index]

    def invoke(self, profile: ModelProfile, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.role in self.failures:
            return ModelResponse(
                text="", profile_name=profile.name, model=profile.model,
                duration_seconds=0.0, succeeded=False, error="预置失败",
            )
        text = self._next_response(request.role)
        if text is None:
            if request.role == "executor":
                text = DEFAULT_CHANGES
            elif request.role == "reviewer":
                text = DEFAULT_REVIEW
            else:
                text = f"fake response for {request.role}"
        return ModelResponse(
            text=text, profile_name=profile.name, model=profile.model,
            duration_seconds=0.0, succeeded=True,
        )
