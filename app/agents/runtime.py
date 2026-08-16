from __future__ import annotations

from abc import ABC, abstractmethod
import threading

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
    """Route by request role, with describe-time pending registration.

    Subclasses override :meth:`_runtime_for` to redirect specific requests
    (for example by model profile) while the role map stays the fallback.
    """

    def __init__(self, runtimes: dict[str, AgentRuntime]):
        if not runtimes:
            raise ValueError("角色 Runtime 路由不能为空。")
        self.runtimes = dict(runtimes)
        self._pending: dict[str, AgentRuntime] = {}
        self._active: dict[str, AgentRuntime] = {}
        self._lock = threading.Lock()

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

    def _runtime_for(self, request: AgentRequest) -> AgentRuntime:
        return self._runtime(request.role)


class ProfileRoutedRuntime(RoleRoutedRuntime):
    """Route composed nodes by model profile, with roles only as a legacy fallback."""

    def __init__(
        self,
        runtimes: dict[str, AgentRuntime],
        profile_runtimes: dict[str, AgentRuntime],
    ):
        super().__init__(runtimes)
        if not profile_runtimes:
            raise ValueError("模型 Profile Runtime 路由不能为空。")
        self.profile_runtimes = dict(profile_runtimes)

    def health_check(self) -> dict:
        result = super().health_check()
        for profile_id, runtime in self.profile_runtimes.items():
            result[f"profile:{profile_id}"] = runtime.health_check()
        return result

    def _runtime_for(self, request: AgentRequest) -> AgentRuntime:
        if request.model_profile_id:
            try:
                return self.profile_runtimes[request.model_profile_id]
            except KeyError as error:
                raise ValueError(
                    f"模型配置 {request.model_profile_id} 没有可用 AgentRuntime。"
                ) from error
        return self._runtime(request.role)
