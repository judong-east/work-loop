from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.contracts import ModelProfile, ModelRequest, ModelResponse


class ModelBackend(ABC):
    @abstractmethod
    def invoke(self, profile: ModelProfile, request: ModelRequest) -> ModelResponse:
        """调用模型并返回响应；实现不得抛出调用失败异常，失败以 succeeded=False 表达。"""
