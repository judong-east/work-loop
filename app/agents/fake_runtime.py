from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.contracts import AgentAccess, AgentRequest, AgentResult
from app.agents.runtime import AgentRuntime
from app.core.contracts import new_id


@dataclass
class FakeAgentStep:
    output: dict[str, Any] = field(default_factory=dict)
    writes: dict[str, str | bytes | None] = field(default_factory=dict)
    succeeded: bool = True
    error: str = ""
    session_id: str = ""


class ScriptedFakeRuntime(AgentRuntime):
    """Deterministic runtime used to exercise orchestration without model calls."""

    def __init__(self, scripts: dict[str, list[FakeAgentStep]]):
        self.scripts = {role: list(steps) for role, steps in scripts.items()}
        self.requests: list[AgentRequest] = []

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        steps = self.scripts.get(request.role, [])
        if not steps:
            return AgentResult(succeeded=False, error=f"FakeRuntime 没有 {request.role} 的脚本步骤。")

        step = steps.pop(0)
        if step.writes and request.access is not AgentAccess.WORKSPACE_WRITE:
            return AgentResult(
                succeeded=False,
                session_id=step.session_id or request.session_id,
                error=f"{request.role} 以只读权限运行，不能修改工作区。",
                runtime="fake",
                runtime_version="1",
                model="scripted",
            )
        if step.succeeded:
            self._apply_writes(request.workspace, step.writes)
        session_id = step.session_id or request.session_id or new_id(f"SESSION-{request.role.upper()}")
        return AgentResult(
            succeeded=step.succeeded,
            output=dict(step.output),
            session_id=session_id,
            error=step.error,
            runtime="fake",
            runtime_version="1",
            model="scripted",
        )

    def _apply_writes(self, workspace: Path, writes: dict[str, str | bytes | None]) -> None:
        root = workspace.resolve()
        for relative, content in writes.items():
            target = (root / relative).resolve()
            try:
                target.relative_to(root)
            except ValueError as error:
                raise ValueError(f"FakeRuntime 写入路径逃逸工作区：{relative}") from error
            if content is None:
                if target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")

    def describe(self, request: AgentRequest) -> dict:
        return {
            "runtime": "fake",
            "runtime_version": "1",
            "model": "scripted",
            "config": {},
        }
