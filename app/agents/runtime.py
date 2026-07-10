from __future__ import annotations

from abc import ABC, abstractmethod

from app.agents.contracts import AgentRequest, AgentResult


class AgentRuntime(ABC):
    @abstractmethod
    def invoke(self, request: AgentRequest) -> AgentResult:
        """Run one agent turn and return a normalized result."""
