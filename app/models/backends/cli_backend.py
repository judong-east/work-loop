from __future__ import annotations

import subprocess
import time

from app.core.contracts import ModelProfile, ModelRequest, ModelResponse
from app.models.backends.base import ModelBackend


class CliBackend(ModelBackend):
    """通过本机 CLI 子进程调用模型；shell=False，模板仅替换 {prompt} 与 {model}。"""

    def invoke(self, profile: ModelProfile, request: ModelRequest) -> ModelResponse:
        command = [
            part.replace("{prompt}", request.prompt).replace("{model}", profile.model)
            for part in profile.command
        ]
        start = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=profile.timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return self._failure(profile, start, f"调用超时（{profile.timeout_seconds}s）。")
        except OSError as error:
            return self._failure(profile, start, f"无法启动命令：{error}")

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            return self._failure(profile, start, f"退出码 {completed.returncode}：{stderr}")

        text = (completed.stdout or "").strip()
        if not text:
            return self._failure(profile, start, "模型返回空输出。")

        return ModelResponse(
            text=text, profile_name=profile.name, model=profile.model,
            duration_seconds=round(time.monotonic() - start, 3), succeeded=True,
        )

    def _failure(self, profile: ModelProfile, start: float, error: str) -> ModelResponse:
        return ModelResponse(
            text="", profile_name=profile.name, model=profile.model,
            duration_seconds=round(time.monotonic() - start, 3), succeeded=False, error=error,
        )
