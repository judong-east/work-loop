from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.contracts import (
    AgentAccess,
    AgentEvent,
    AgentEventType,
    AgentRequest,
    AgentResult,
)
from app.agents.runtime import AgentRuntime
from app.agents.runtime_common import (
    UNSANDBOXED_OPT_IN,
    ensure_terminal_event,
    parse_structured_output,
    unsandboxed_allowed,
)
from app.core.process_tree import ProcessTreeHandle, process_group_options


_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
_SAFE_TOOL = re.compile(r"^[a-z][a-z0-9_-]*$")

# Unlike CodexCliRuntime (--sandbox workspace-write, network_access=false, --cd),
# Pi is launched with nothing but a working directory and a tool allow-list: its
# bash/write/edit tools can reach any path on the machine and any host on the
# network. Workloop cannot detect that either — policy checks and changes.diff
# are computed from a snapshot of the task worktree, so a write outside it leaves
# no violation, no diff and no evidence. So a write-access request whose policy
# denies network is refused unless the operator opts in explicitly, the same way
# UnsafeDirectCommandSandbox is named and gated for validation.


@dataclass(frozen=True)
class PiRpcProfile:
    command: list[str] = field(default_factory=lambda: ["pi"])
    model: str = ""
    provider: str = ""
    thinking: str = "medium"
    session_dir: Path | None = None
    config_dir: Path | None = None
    read_only_tools: tuple[str, ...] = ("read", "grep", "find", "ls")
    workspace_write_tools: tuple[str, ...] = (
        "read",
        "grep",
        "find",
        "ls",
        "edit",
        "write",
        "bash",
    )

    def validate(self) -> None:
        if not self.command:
            raise ValueError("Pi command cannot be empty")
        if any(argument.startswith("-") for argument in self.command[1:]):
            raise ValueError("Pi command arguments cannot override Workloop flags")
        if self.thinking not in _THINKING_LEVELS:
            raise ValueError("Pi thinking level is invalid")
        for tools in (self.read_only_tools, self.workspace_write_tools):
            if not all(_SAFE_TOOL.fullmatch(tool) for tool in tools):
                raise ValueError("Pi tool names are invalid")


class PiRpcRuntime(AgentRuntime):
    """Run one controlled Pi session over the documented JSONL RPC protocol."""

    def __init__(self, profile: PiRpcProfile):
        profile.validate()
        self.profile = profile
        self._active: dict[str, tuple[subprocess.Popen, ProcessTreeHandle]] = {}
        self._lock = threading.Lock()

    def invoke(self, request: AgentRequest) -> AgentResult:
        identity = self.describe(request)
        process: subprocess.Popen | None = None
        tree: ProcessTreeHandle | None = None
        if (
            request.access is AgentAccess.WORKSPACE_WRITE
            and not request.policy.network_allowed
            and not unsandboxed_allowed()
        ):
            return self._failure(
                request,
                "项目策略拒绝网络访问，但 PiRpcRuntime 无法在操作系统层面隔离写入或"
                f"网络。要在无沙箱条件下执行，请显式设置 {UNSANDBOXED_OPT_IN}=1，"
                "或改用 CodexCliRuntime 执行写节点。",
                "sandbox_unavailable",
                identity,
            )
        try:
            command = self._command(request)
            workspace = Path(request.workspace).resolve()
            if not workspace.is_dir():
                return self._failure(
                    request,
                    "Pi workspace does not exist",
                    "environment_missing",
                    identity,
                )
            session_dir = self._session_dir(request)
            session_dir.mkdir(parents=True, exist_ok=True)
            environment = dict(os.environ)
            environment.update(
                {
                    "PI_SKIP_VERSION_CHECK": "1",
                    "PI_TELEMETRY": "0",
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                }
            )
            if self.profile.config_dir is not None:
                config_dir = Path(self.profile.config_dir).resolve()
                config_dir.mkdir(parents=True, exist_ok=True)
                environment["PI_CODING_AGENT_DIR"] = str(config_dir)

            process = subprocess.Popen(
                command,
                cwd=workspace,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=environment,
                **process_group_options(),
            )
            tree = ProcessTreeHandle(process)
            with self._lock:
                self._active[request.task_id] = (process, tree)

            result = self._run_rpc(request, process, tree, identity)
            return ensure_terminal_event(result, request.role)
        except FileNotFoundError as error:
            return self._failure(request, f"Pi executable not found: {error}", "environment_missing", identity)
        except OSError as error:
            return self._failure(request, f"Unable to start Pi: {error}", "environment_missing", identity)
        except Exception as error:  # noqa: BLE001 - runtime errors become persisted results
            return self._failure(request, f"Pi RPC failed: {error}", "runtime_error", identity)
        finally:
            with self._lock:
                self._active.pop(request.task_id, None)
            if process is not None:
                self._stop_process(process, tree)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            active = self._active.get(task_id)
        if active is None:
            return False
        _, tree = active
        tree.terminate()
        return True

    def describe(self, request: AgentRequest) -> dict[str, Any]:
        model = str(getattr(request, "model", "") or self.profile.model)
        provider = str(getattr(request, "provider", "") or self.profile.provider)
        return {
            "runtime": "pi-rpc",
            "runtime_version": "",
            "model": f"{provider}/{model}" if provider and model else model,
            "config": {
                "provider": provider,
                "model": model,
                "thinking": str(getattr(request, "thinking", "") or self.profile.thinking),
                "tools": list(self._tools(request)),
                "session_dir": str(self._session_dir(request)),
                # Recorded so a run record never implies isolation Pi does not
                # provide: the worktree is a working directory, not a boundary.
                "sandbox": "none",
                "network_enforced": False,
                "unsandboxed_opt_in": unsandboxed_allowed(),
            },
        }

    def health_check(self) -> dict[str, Any]:
        executable = shutil.which(self.profile.command[0])
        if executable is None and not Path(self.profile.command[0]).is_file():
            return {
                "available": False,
                "runtime": "pi-rpc",
                "error": f"Pi executable not found: {self.profile.command[0]}",
            }
        return {"available": True, "runtime": "pi-rpc", "error": ""}

    def _run_rpc(
        self,
        request: AgentRequest,
        process: subprocess.Popen,
        tree: ProcessTreeHandle,
        identity: dict[str, Any],
    ) -> AgentResult:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            return self._failure(request, "Pi RPC streams are unavailable", "environment_missing", identity)

        records: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
        self._start_reader(process.stdout, "stdout", records)
        self._start_reader(process.stderr, "stderr", records)
        stderr_chunks: list[str] = []
        raw_events: list[dict[str, Any]] = []
        normalized: list[AgentEvent] = []
        text_chunks: list[str] = []
        last_message = ""
        prompt_id = f"workloop-{request.task_id}-{int(time.time() * 1000)}"
        self._send(process, {"id": prompt_id, "type": "prompt", "message": request.instructions})

        started = time.monotonic()
        last_activity = started
        total_deadline = started + request.budget.total_timeout_seconds
        idle_deadline = started + request.budget.idle_timeout_seconds
        settled = False
        saw_agent_end = False
        stdout_closed = False
        prompt_rejected = ""

        while True:
            now = time.monotonic()
            if now >= total_deadline:
                tree.terminate()
                return self._failure(
                    request,
                    "Pi call timed out",
                    "call_timeout",
                    identity,
                    normalized,
                    raw_events,
                    stderr_chunks,
                )
            if now >= idle_deadline:
                tree.terminate()
                return self._failure(
                    request,
                    "Pi produced no event before the idle deadline",
                    "idle_timeout",
                    identity,
                    normalized,
                    raw_events,
                    stderr_chunks,
                )
            if prompt_rejected:
                tree.terminate()
                return self._failure(
                    request,
                    prompt_rejected,
                    "protocol_error",
                    identity,
                    normalized,
                    raw_events,
                    stderr_chunks,
                )
            if stdout_closed and (settled or saw_agent_end or process.poll() is not None):
                break

            try:
                # Clamp: the deadline checks above use `now`, but time passes
                # before this call, so a raw difference can go negative and
                # queue.get would raise ValueError instead of timing out.
                source, raw = records.get(
                    timeout=max(0.0, min(0.1, total_deadline - now, idle_deadline - now))
                )
            except queue.Empty:
                continue
            if raw is None:
                if source == "stdout":
                    stdout_closed = True
                continue
            if source == "stderr":
                stderr_chunks.append(raw.decode("utf-8", errors="replace"))
                continue

            last_activity = time.monotonic()
            idle_deadline = last_activity + request.budget.idle_timeout_seconds
            event = self._decode(raw)
            raw_events.append(event)
            event_type = str(event.get("type", ""))
            if event_type == "response" and event.get("id") == prompt_id:
                if event.get("success") is not True:
                    prompt_rejected = str(event.get("error") or "Pi rejected the prompt")
                continue
            last_message = self._consume_event(event, request.role, normalized, text_chunks) or last_message
            if event_type == "agent_settled":
                settled = True
            elif event_type == "agent_end" and event.get("willRetry") is not True:
                saw_agent_end = True

            if settled:
                break

        state = self._query(process, records, "get_state", total_deadline, raw_events)
        stats = self._query(process, records, "get_session_stats", total_deadline, raw_events)
        final_message = "".join(text_chunks).strip() or last_message.strip()
        if not final_message:
            return self._failure(
                request,
                "Pi returned no assistant message",
                "protocol_error",
                identity,
                normalized,
                raw_events,
                stderr_chunks,
            )
        try:
            output = parse_structured_output(final_message)
        except ValueError as error:
            return self._failure(
                request,
                str(error),
                "structured_output_failed",
                identity,
                normalized,
                raw_events,
                stderr_chunks,
                final_message=final_message,
            )

        session_id = str(state.get("sessionFile") or state.get("sessionId") or request.session_id)
        state_model = state.get("model") if isinstance(state.get("model"), dict) else {}
        selected_model = str(state_model.get("id") or identity.get("model") or "")
        usage = self._usage_from_stats(stats)
        return AgentResult(
            succeeded=True,
            output=output,
            session_id=session_id,
            final_message=final_message,
            events=normalized,
            raw_events=raw_events,
            usage=usage,
            runtime="pi-rpc",
            runtime_version="",
            model=selected_model,
            runtime_config=identity.get("config", {}),
        )

    def _command(self, request: AgentRequest) -> list[str]:
        model = str(getattr(request, "model", "") or self.profile.model).strip()
        if not model:
            raise ValueError("Pi model is required")
        provider = str(getattr(request, "provider", "") or self.profile.provider).strip()
        thinking = str(getattr(request, "thinking", "") or self.profile.thinking).strip()
        if thinking not in _THINKING_LEVELS:
            raise ValueError("Pi thinking level is invalid")
        command = [*self.profile.command, "--mode", "rpc", "--model", model, "--thinking", thinking]
        if provider:
            command.extend(["--provider", provider])
        tools = self._tools(request)
        if tools:
            command.extend(["--tools", ",".join(tools)])
        if request.session_id:
            command.extend(["--session", request.session_id])
        else:
            command.extend(["--session-dir", str(self._session_dir(request))])
        return command

    def _session_dir(self, request: AgentRequest) -> Path:
        if self.profile.session_dir is not None:
            return Path(self.profile.session_dir).resolve()
        return (Path(request.workspace).resolve().parent / ".workloop-pi-sessions" / request.task_id)

    def _tools(self, request: AgentRequest) -> tuple[str, ...]:
        requested = getattr(request, "tools", None)
        if requested:
            tools = tuple(str(item) for item in requested)
        elif request.access is AgentAccess.READ_ONLY:
            tools = self.profile.read_only_tools
        else:
            tools = self.profile.workspace_write_tools
        if not all(_SAFE_TOOL.fullmatch(tool) for tool in tools):
            raise ValueError("Pi tool names are invalid")
        return tools

    @staticmethod
    def _start_reader(stream, source: str, records: queue.Queue) -> None:
        def read() -> None:
            try:
                while True:
                    raw = stream.readline()
                    if not raw:
                        break
                    records.put((source, raw))
            finally:
                records.put((source, None))

        threading.Thread(target=read, name=f"pi-rpc-{source}", daemon=True).start()

    @staticmethod
    def _decode(raw: bytes | str) -> dict[str, Any]:
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = raw
        text = text[:-1] if text.endswith("\n") else text
        text = text[:-1] if text.endswith("\r") else text
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("Pi RPC record must be an object")
        return value

    @staticmethod
    def _usage_from_stats(stats: Any) -> dict[str, Any]:
        """Build the AgentResult.usage dict from a get_session_stats response.

        Pi 0.83 returns ``tokens`` as an object and ``cost`` as an aggregated
        scalar number (``usageTotals.cost``). The dict-with-total branch is kept
        as a defensive fallback in case a future version nests it.
        """
        usage = dict(stats.get("tokens") or {}) if isinstance(stats, dict) else {}
        cost = stats.get("cost") if isinstance(stats, dict) else None
        if isinstance(cost, bool):
            pass  # bool is an int subclass; treat as "no cost reported"
        elif isinstance(cost, (int, float)):
            usage["total_cost_usd"] = float(cost)
        elif isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
            usage["total_cost_usd"] = float(cost["total"])
        return usage

    def _query(
        self,
        process: subprocess.Popen,
        records: queue.Queue,
        command: str,
        deadline: float,
        raw_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if process.stdin is None:
            return {}
        request_id = f"workloop-query-{command}-{int(time.time() * 1000)}"
        self._send(process, {"id": request_id, "type": command})
        while time.monotonic() < deadline:
            try:
                source, raw = records.get(
                    timeout=max(0.0, min(0.1, deadline - time.monotonic()))
                )
            except queue.Empty:
                continue
            if source != "stdout" or raw is None:
                continue
            event = self._decode(raw)
            raw_events.append(event)
            if event.get("type") == "response" and event.get("id") == request_id:
                if event.get("success") is not True:
                    return {}
                data = event.get("data")
                return data if isinstance(data, dict) else {}
        return {}

    @staticmethod
    def _send(process: subprocess.Popen, payload: dict[str, Any]) -> None:
        if process.stdin is None:
            raise ValueError("Pi RPC stdin is unavailable")
        process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        process.stdin.flush()

    @staticmethod
    def _consume_event(
        event: dict[str, Any],
        role: str,
        normalized: list[AgentEvent],
        text_chunks: list[str],
    ) -> str:
        raw_type = str(event.get("type", ""))
        if raw_type in {"agent_start", "session_start"}:
            normalized.append(AgentEvent(AgentEventType.SESSION_STARTED, role, dict(event), raw_type))
        elif raw_type == "message_update":
            delta = event.get("assistantMessageEvent")
            data = delta if isinstance(delta, dict) else event
            normalized.append(AgentEvent(AgentEventType.MESSAGE_DELTA, role, dict(data), raw_type))
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text_chunks.append(str(delta.get("delta", "")))
        elif raw_type in {"tool_execution_start", "bash_execution_update"}:
            normalized.append(AgentEvent(AgentEventType.TOOL_STARTED, role, dict(event), raw_type))
        elif raw_type == "tool_execution_end":
            normalized.append(AgentEvent(AgentEventType.TOOL_COMPLETED, role, dict(event), raw_type))
        elif raw_type == "compaction_end":
            normalized.append(AgentEvent(AgentEventType.HEARTBEAT, role, dict(event), raw_type))
        elif raw_type in {"agent_settled", "agent_end"}:
            return _message_text(event.get("messages", []))
        elif raw_type in {"extension_error", "auto_retry_end"}:
            normalized.append(AgentEvent(AgentEventType.HEARTBEAT, role, dict(event), raw_type))
        return ""

    @staticmethod
    def _stop_process(process: subprocess.Popen, tree: ProcessTreeHandle | None) -> None:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None and tree is not None:
            tree.terminate()
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if tree is not None:
            tree.close()

    @staticmethod
    def _failure(
        request: AgentRequest,
        error: str,
        error_type: str,
        identity: dict[str, Any],
        events: list[AgentEvent] | None = None,
        raw_events: list[dict[str, Any]] | None = None,
        stderr_chunks: list[str] | None = None,
        final_message: str = "",
    ) -> AgentResult:
        detail = error
        if stderr_chunks:
            stderr = "".join(stderr_chunks).strip()
            if stderr:
                detail = f"{error}: {stderr[-4000:]}"
        return AgentResult(
            succeeded=False,
            error=detail,
            error_type=error_type,
            final_message=final_message,
            events=list(events or []),
            raw_events=list(raw_events or []),
            runtime="pi-rpc",
            runtime_version=identity.get("runtime_version", ""),
            model=identity.get("model", ""),
            runtime_config=identity.get("config", {}),
        )


def _message_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if parts:
                return "".join(parts)
    return ""
