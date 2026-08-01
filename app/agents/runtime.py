from __future__ import annotations

from abc import ABC, abstractmethod
import threading
from typing import Iterable

from app.agents.contracts import AgentRequest, AgentResult


class AgentRuntime(ABC):
    @abstractmethod
    def invoke(self, request: AgentRequest) -> AgentResult:
        """Run one agent turn and return a normalized result."""

    def cancel(self, task_id: str) -> bool:
        """Cancel an active task run when supported."""
        return False

    def describe(self, request: AgentRequest) -> dict:
        """Return the effective runtime identity persisted before invocation."""
        return {"runtime": type(self).__name__, "runtime_version": "", "model": "", "config": {}}

    def health_check(self) -> dict:
        """Return an environment-level availability snapshot for this runtime."""
        return {"available": True, "runtime": type(self).__name__, "error": ""}


class RoleRoutedRuntime(AgentRuntime):
    def __init__(self, runtimes: dict[str, AgentRuntime]):
        if not runtimes:
            raise ValueError("角色 Runtime 路由不能为空。")
        self.runtimes = dict(runtimes)
        self._pending: dict[str, AgentRuntime] = {}
        self._active: dict[str, AgentRuntime] = {}
        self._lock = threading.Lock()

    def invoke(self, request: AgentRequest) -> AgentResult:
        runtime = self._runtime(request.role)
        with self._lock:
            self._pending.pop(request.task_id, None)
            self._active[request.task_id] = runtime
        try:
            return runtime.invoke(request)
        finally:
            with self._lock:
                self._active.pop(request.task_id, None)

    def describe(self, request: AgentRequest) -> dict:
        runtime = self._runtime(request.role)
        with self._lock:
            self._pending[request.task_id] = runtime
        try:
            return runtime.describe(request)
        except Exception:
            with self._lock:
                self._pending.pop(request.task_id, None)
            raise

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            runtime = self._active.get(task_id) or self._pending.pop(task_id, None)
        return runtime.cancel(task_id) if runtime is not None else False

    def health_check(self) -> dict:
        return {
            role: runtime.health_check()
            for role, runtime in self.runtimes.items()
        }

    def _runtime(self, role: str) -> AgentRuntime:
        try:
            return self.runtimes[role]
        except KeyError as error:
            raise ValueError(f"角色 {role} 没有配置 AgentRuntime。") from error


class NodeRoutedRuntime(RoleRoutedRuntime):
    """Role-routed runtime that also honors a per-node override map.

    When an ``AgentRequest`` carries a ``node_id`` present in ``node_runtimes``,
    that runtime is used for the call; otherwise routing falls back to the
    role-based map (the ``RoleRoutedRuntime`` behavior). This is the control
    plane for per-node model/runtime selection: a plan node can target its own
    runtime (for example a Pi session with a specific model) while the default
    roles keep their configured runtime. The fallback keeps every role covered.
    """

    def __init__(
        self,
        runtimes: dict[str, AgentRuntime],
        node_runtimes: dict[str, AgentRuntime] | None = None,
    ):
        super().__init__(runtimes)
        self.node_runtimes: dict[str, AgentRuntime] = dict(node_runtimes or {})

    def invoke(self, request: AgentRequest) -> AgentResult:
        runtime = self._runtime_for(request)
        with self._lock:
            self._pending.pop(request.task_id, None)
            self._active[request.task_id] = runtime
        try:
            return runtime.invoke(request)
        finally:
            with self._lock:
                self._active.pop(request.task_id, None)

    def describe(self, request: AgentRequest) -> dict:
        runtime = self._runtime_for(request)
        with self._lock:
            self._pending[request.task_id] = runtime
        try:
            return runtime.describe(request)
        except Exception:
            with self._lock:
                self._pending.pop(request.task_id, None)
            raise

    def health_check(self) -> dict:
        result = super().health_check()
        for node_id, runtime in self.node_runtimes.items():
            result[f"node:{node_id}"] = runtime.health_check()
        return result

    def _runtime_for(self, request: AgentRequest) -> AgentRuntime:
        if request.node_id and request.node_id in self.node_runtimes:
            return self.node_runtimes[request.node_id]
        return self._runtime(request.role)

    def configured_node_ids(self) -> Iterable[str]:
        return self.node_runtimes.keys()
