from __future__ import annotations

from app.core.contracts import ModelProfile, ModelRequest, ModelResponse
from app.models.backends.base import ModelBackend

DEFAULT_REVIEW = '{"verdict": "pass", "issues": []}'


class FakeBackend(ModelBackend):
    """离线测试后端：按角色返回预置应答或预置失败。"""

    def __init__(self, responses: dict[str, str] | None = None, failures: set[str] | None = None):
        self.responses = responses or {}
        self.failures = failures or set()
        self.requests: list[ModelRequest] = []

    def invoke(self, profile: ModelProfile, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.role in self.failures:
            return ModelResponse(
                text="", profile_name=profile.name, model=profile.model,
                duration_seconds=0.0, succeeded=False, error="预置失败",
            )
        if request.role in self.responses:
            text = self.responses[request.role]
        elif request.role == "reviewer":
            text = DEFAULT_REVIEW
        else:
            text = f"fake response for {request.role}"
        return ModelResponse(
            text=text, profile_name=profile.name, model=profile.model,
            duration_seconds=0.0, succeeded=True,
        )
