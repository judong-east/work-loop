"""Native harness runtime: Workloop itself is the agent harness.

Instead of driving a vendor CLI subprocess (Claude Code, Codex CLI, Pi) whose
fixed tool set and harness behavior constrain the model, this runtime talks
directly to an OpenAI-compatible chat-completions endpoint and runs the
tool-calling loop in-process. The model decides autonomously which tools to
use; Workloop implements the tools (see ``harness_tools``), confines file
access to the task worktree, persists the message history as a resumable
session, and enforces budgets and cancellation — the "Model + Harness =
Agent" split referenced in
``docs/superpowers/specs/2026-08-16-native-harness-runtime.md``.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.agents.contracts import (
    AgentAccess,
    AgentEvent,
    AgentEventType,
    AgentRequest,
    AgentResult,
)
from app.agents.harness_tools import ToolContext, execute_tool, tools_for
from app.agents.pi_rpc import _parse_json_object, _unsandboxed_allowed
from app.agents.runtime import AgentRuntime
from app.core.atomic_files import write_json_atomic

_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
_SESSION_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

# transport(payload, timeout_seconds) -> response dict. Injectable so tests
# script multi-round tool-calling without network access.
Transport = Callable[[dict[str, Any], float], dict[str, Any]]


@dataclass(frozen=True)
class NativeHarnessProfile:
    model: str
    base_url: str = ""
    api_key_env: str = "WORKLOOP_NATIVE_API_KEY"
    provider: str = ""
    thinking: str = "medium"
    request_timeout_seconds: float = 120.0
    max_tool_rounds: int = 60
    max_tokens: int = 0
    session_dir: Path | None = None
    transport: Transport | None = None

    def validate(self) -> None:
        if not self.model.strip():
            raise ValueError("Native harness model is required")
        if self.thinking not in _THINKING_LEVELS:
            raise ValueError("Native harness thinking level is invalid")
        if self.request_timeout_seconds <= 0:
            raise ValueError("Native harness request timeout must be positive")
        if self.max_tool_rounds <= 0:
            raise ValueError("Native harness max tool rounds must be positive")
        if self.max_tokens < 0:
            raise ValueError("Native harness max tokens cannot be negative")


def _read_key_file(path_text: str) -> str:
    path = Path(path_text.strip())
    if not path.is_file():
        return ""
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for name in ("apiKey", "api_key", "key", "token"):
                value = data.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    except json.JSONDecodeError:
        pass
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and not line.startswith("sk-"):
            line = line.split("=", 1)[1]
        return line.strip().strip("\"'")
    return ""


def resolve_api_key(api_key_env: str) -> str:
    """Key from the named env var, else from WORKLOOP_NATIVE_KEY_FILE.

    The key is never written to the catalog, task state, or logs; only the
    env var name is recorded in runtime identity.
    """
    key = os.environ.get(api_key_env, "").strip()
    if key:
        return key
    key_file = os.environ.get("WORKLOOP_NATIVE_KEY_FILE", "").strip()
    return _read_key_file(key_file) if key_file else ""


def urllib_transport(base_url: str, api_key: str) -> Transport:
    url = base_url.rstrip("/") + "/chat/completions"

    def call(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"模型 API 返回 HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"无法连接模型 API {url}: {error.reason}") from error
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"模型 API 返回的不是 JSON: {body[:500]}") from error
        if not isinstance(decoded, dict):
            raise RuntimeError("模型 API 响应必须是 JSON 对象。")
        return decoded

    return call


@dataclass
class _ActiveRun:
    cancel_event: threading.Event


class NativeHarnessRuntime(AgentRuntime):
    """Run one autonomous model turn through the in-process tool loop."""

    def __init__(self, profile: NativeHarnessProfile):
        profile.validate()
        self.profile = profile
        self._active: dict[str, _ActiveRun] = {}
        self._lock = threading.Lock()

    def invoke(self, request: AgentRequest) -> AgentResult:
        identity = self.describe(request)
        active = _ActiveRun(threading.Event())
        with self._lock:
            self._active[request.task_id] = active
        try:
            return self._run(request, active, identity)
        finally:
            with self._lock:
                self._active.pop(request.task_id, None)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            active = self._active.get(task_id)
        if active is None:
            return False
        active.cancel_event.set()
        return True

    def describe(self, request: AgentRequest) -> dict[str, Any]:
        model = str(getattr(request, "model", "") or self.profile.model)
        provider = str(getattr(request, "provider", "") or self.profile.provider)
        base_url = self._base_url()
        shell_allowed = self._shell_allowed(request)
        tools = tools_for(request.access, request.policy, shell_allowed)
        return {
            "runtime": "native-harness",
            "runtime_version": "",
            "model": f"{provider}/{model}" if provider and model else model,
            "config": {
                "provider": provider,
                "model": model,
                "thinking": str(getattr(request, "thinking", "") or self.profile.thinking),
                "base_url": base_url,
                "api_key_env": self._api_key_env(),
                "tools": [tool.name for tool in tools],
                # Unlike PiRpcRuntime, file tools are confined to the task
                # worktree in-process; only run_command leaves it, so that one
                # tool alone carries the unsandboxed gate.
                "files_confined_to_worktree": True,
                "shell_tool": "allowed" if shell_allowed else "gated",
                "unsandboxed_opt_in": _unsandboxed_allowed(),
                "session_dir": str(self._session_dir(request)),
            },
        }

    def health_check(self) -> dict[str, Any]:
        if not self._base_url():
            return {
                "available": False,
                "runtime": "native-harness",
                "error": "未配置 base_url（WORKLOOP_NATIVE_BASE_URL 或模型条目 base_url）。",
            }
        if not resolve_api_key(self._api_key_env()):
            return {
                "available": False,
                "runtime": "native-harness",
                "error": (
                    f"未配置 API key（环境变量 {self._api_key_env()} 或 "
                    "WORKLOOP_NATIVE_KEY_FILE 指向的密钥文件）。"
                ),
            }
        return {"available": True, "runtime": "native-harness", "error": ""}

    # -- internals ---------------------------------------------------------

    def _run(
        self,
        request: AgentRequest,
        active: _ActiveRun,
        identity: dict[str, Any],
    ) -> AgentResult:
        workspace = Path(request.workspace).resolve()
        if not workspace.is_dir():
            return self._failure(request, "任务工作区不存在", "environment_missing", identity)
        base_url = self._base_url()
        if not base_url:
            return self._failure(
                request,
                "原生 Harness 未配置 base_url（设置 WORKLOOP_NATIVE_BASE_URL 或模型条目 base_url）。",
                "environment_missing",
                identity,
            )
        api_key = resolve_api_key(self._api_key_env())
        if not api_key:
            return self._failure(
                request,
                f"原生 Harness 未配置 API key（环境变量 {self._api_key_env()} 或 WORKLOOP_NATIVE_KEY_FILE）。",
                "environment_missing",
                identity,
            )
        shell_allowed = self._shell_allowed(request)
        tools = tools_for(request.access, request.policy, shell_allowed)
        transport = self.profile.transport or urllib_transport(base_url, api_key)

        events: list[AgentEvent] = [AgentEvent(AgentEventType.SESSION_STARTED, request.role, {})]
        raw_events: list[dict[str, Any]] = []
        usage_total = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "total_tokens": 0}

        session_path, messages, resumed = self._load_session(request)
        if not resumed:
            messages = [
                {"role": "system", "content": self._system_prompt(request)},
                {"role": "user", "content": request.instructions},
            ]

        model = str(getattr(request, "model", "") or self.profile.model)
        started = time.monotonic()
        last_progress = started
        total_deadline = started + request.budget.total_timeout_seconds
        rounds = 0

        while True:
            if active.cancel_event.is_set():
                return self._cancelled(request, identity, events, raw_events, usage_total)
            now = time.monotonic()
            if now >= total_deadline:
                return self._failure(
                    request, "原生 Harness 调用超时", "call_timeout", identity,
                    events, raw_events, usage_total,
                )
            if now - last_progress >= request.budget.idle_timeout_seconds:
                return self._failure(
                    request, "原生 Harness 在空闲期限内没有任何进展", "idle_timeout", identity,
                    events, raw_events, usage_total,
                )
            rounds += 1
            if rounds > self.profile.max_tool_rounds:
                return self._failure(
                    request,
                    f"模型连续 {self.profile.max_tool_rounds} 轮工具调用仍未完成，已停止。",
                    "tool_round_limit",
                    identity, events, raw_events, usage_total,
                )

            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "tools": [tool.definition() for tool in tools],
                "stream": False,
            }
            if self.profile.max_tokens > 0:
                payload["max_tokens"] = self.profile.max_tokens
            call_timeout = min(
                self.profile.request_timeout_seconds,
                max(1.0, total_deadline - time.monotonic()),
            )
            error_holder: list[str] = []
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue()

            def call() -> None:
                try:
                    response_queue.put(transport(payload, call_timeout))
                except Exception as error:  # noqa: BLE001 - surfaced below
                    error_holder.append(str(error))

            worker = threading.Thread(target=call, name="native-harness-api", daemon=True)
            worker.start()
            while worker.is_alive():
                if active.cancel_event.is_set():
                    return self._cancelled(request, identity, events, raw_events, usage_total)
                worker.join(timeout=0.1)
            if error_holder:
                return self._failure(
                    request, error_holder[0], "protocol_error", identity,
                    events, raw_events, usage_total,
                )
            try:
                response = response_queue.get_nowait()
            except queue.Empty:
                return self._failure(
                    request, "模型 API 调用没有返回结果", "protocol_error", identity,
                    events, raw_events, usage_total,
                )
            raw_events.append(response)
            self._accumulate_usage(response, usage_total)
            last_progress = time.monotonic()

            message, finish_reason = self._assistant_message(response)
            if message is None:
                return self._failure(
                    request,
                    f"模型 API 响应缺少 choices[0].message（finish_reason={finish_reason}）。",
                    "protocol_error", identity, events, raw_events, usage_total,
                )
            if finish_reason == "length":
                return self._failure(
                    request,
                    "模型输出被 max_tokens 截断（finish_reason=length）；"
                    "请提高 WORKLOOP_NATIVE_MAX_TOKENS 或模型条目的 max_tokens。",
                    "protocol_error", identity, events, raw_events, usage_total,
                )
            if finish_reason == "content_filter":
                return self._failure(
                    request, "模型输出被内容过滤拦截。", "protocol_error",
                    identity, events, raw_events, usage_total,
                )

            tool_calls = message.get("tool_calls") or []
            content = message.get("content")
            text_content = content if isinstance(content, str) else ""
            assistant_entry: dict[str, Any] = {"role": "assistant", "content": text_content}
            if tool_calls:
                assistant_entry["tool_calls"] = tool_calls
            messages.append(assistant_entry)
            self._save_session(session_path, request, model, messages)

            if not tool_calls:
                final_message = text_content.strip()
                events.append(AgentEvent(AgentEventType.MESSAGE_DELTA, request.role, {"text": final_message}))
                if not final_message:
                    return self._failure(
                        request, "模型没有返回最终答复文本。", "protocol_error",
                        identity, events, raw_events, usage_total,
                    )
                try:
                    output = _parse_json_object(final_message)
                except ValueError as error:
                    return AgentResult(
                        succeeded=False,
                        error=str(error),
                        error_type="structured_output_failed",
                        final_message=final_message,
                        events=self._terminal(events, request.role, False),
                        raw_events=raw_events,
                        usage=usage_total,
                        runtime="native-harness",
                        runtime_version=identity.get("runtime_version", ""),
                        model=identity.get("model", ""),
                        runtime_config=identity.get("config", {}),
                    )
                return AgentResult(
                    succeeded=True,
                    output=output,
                    session_id=str(session_path),
                    final_message=final_message,
                    events=self._terminal(events, request.role, True),
                    raw_events=raw_events,
                    usage=usage_total,
                    runtime="native-harness",
                    runtime_version=identity.get("runtime_version", ""),
                    model=identity.get("model", ""),
                    runtime_config=identity.get("config", {}),
                )

            ctx = ToolContext(
                workspace=workspace,
                policy=request.policy,
                access=request.access,
                cancel_event=active.cancel_event,
            )
            for call_entry in tool_calls:
                if active.cancel_event.is_set():
                    return self._cancelled(request, identity, events, raw_events, usage_total)
                call_id = str(call_entry.get("id") or "")
                function = call_entry.get("function") or {}
                name = str(function.get("name") or "")
                arguments = str(function.get("arguments") or "")
                events.append(
                    AgentEvent(
                        AgentEventType.TOOL_STARTED,
                        request.role,
                        {"id": call_id, "name": name, "arguments": arguments[:2000]},
                    )
                )
                result_text = execute_tool(tools, name, arguments, ctx)
                events.append(
                    AgentEvent(
                        AgentEventType.TOOL_COMPLETED,
                        request.role,
                        {"id": call_id, "name": name, "result_chars": len(result_text)},
                    )
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result_text or "(空结果)",
                    }
                )
            self._save_session(session_path, request, model, messages)
            last_progress = time.monotonic()

    def _system_prompt(self, request: AgentRequest) -> str:
        lines = [
            "你在 Workloop 的原生 Harness 中运行（Model + Harness = Agent）："
            "由你自主决定用哪些工具、按什么顺序完成下面的任务。",
            f"任务工作区：{request.workspace}",
        ]
        if request.access is AgentAccess.READ_ONLY:
            lines.append("本次是只读任务：只用读取、列目录和搜索工具调研，禁止修改任何文件。")
        else:
            lines.append("你可以读取和修改工作区内的文件；所有文件工具都被限制在工作区内。")
            if not self._shell_allowed(request):
                lines.append(
                    "本次没有提供 shell 工具；请通过文件工具完成修改，验证由系统在执行后统一运行。"
                )
        lines.append(
            "完成后，最终答复只输出任务要求的那个 JSON 对象，不要 Markdown 代码围栏或解释文字。"
        )
        return "\n".join(lines)

    def _shell_allowed(self, request: AgentRequest) -> bool:
        # File tools are confined in-process, so unlike PiRpcRuntime the whole
        # write request is not refused; only the unconfineable shell tool is
        # withheld unless the operator opted in or the policy allows network.
        if request.policy.network_allowed:
            return True
        return _unsandboxed_allowed()

    def _base_url(self) -> str:
        return self.profile.base_url.strip() or os.environ.get("WORKLOOP_NATIVE_BASE_URL", "").strip()

    def _api_key_env(self) -> str:
        return (
            self.profile.api_key_env.strip()
            or os.environ.get("WORKLOOP_NATIVE_API_KEY_ENV", "").strip()
            or "WORKLOOP_NATIVE_API_KEY"
        )

    def _session_dir(self, request: AgentRequest) -> Path:
        if self.profile.session_dir is not None:
            return Path(self.profile.session_dir).resolve()
        return Path(request.workspace).resolve().parent / ".workloop-native-sessions" / request.task_id

    def _load_session(
        self, request: AgentRequest
    ) -> tuple[Path, list[dict[str, Any]], bool]:
        session_dir = self._session_dir(request)
        candidates: list[Path] = []
        if request.session_id.strip():
            direct = Path(request.session_id.strip())
            if direct.is_absolute():
                candidates.append(direct)
            candidates.append(session_dir / direct.name)
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                break
            messages = data.get("messages") if isinstance(data, dict) else None
            if isinstance(messages, list) and messages and all(isinstance(m, dict) for m in messages):
                return candidate, list(messages), True
            break
        key = request.session_key or request.role
        safe = _SESSION_SAFE.sub("-", key.strip()) or "session"
        session_path = session_dir / f"{safe}.json"
        return session_path, [], False

    def _save_session(
        self,
        path: Path,
        request: AgentRequest,
        model: str,
        messages: list[dict[str, Any]],
    ) -> None:
        write_json_atomic(
            path,
            {
                "schema_version": 1,
                "task_id": request.task_id,
                "role": request.role,
                "model": model,
                "messages": messages,
            },
        )

    @staticmethod
    def _assistant_message(response: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return None, str(response.get("finish_reason", ""))
        message = choices[0].get("message")
        if not isinstance(message, dict):
            return None, str(choices[0].get("finish_reason", ""))
        return message, str(choices[0].get("finish_reason", ""))

    @staticmethod
    def _accumulate_usage(response: dict[str, Any], total: dict[str, int]) -> None:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return

        def count(value: Any) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        total["input_tokens"] += count(usage.get("prompt_tokens"))
        total["output_tokens"] += count(usage.get("completion_tokens"))
        total["total_tokens"] += count(usage.get("total_tokens")) or (
            count(usage.get("prompt_tokens")) + count(usage.get("completion_tokens"))
        )
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            total["cached_input_tokens"] += count(details.get("cached_tokens"))

    @staticmethod
    def _terminal(events: list[AgentEvent], role: str, succeeded: bool) -> list[AgentEvent]:
        events.append(
            AgentEvent(
                AgentEventType.COMPLETED if succeeded else AgentEventType.FAILED,
                role,
                {"reason": "completed" if succeeded else "failed"},
            )
        )
        return events

    @staticmethod
    def _cancelled(
        request: AgentRequest,
        identity: dict[str, Any],
        events: list[AgentEvent],
        raw_events: list[dict[str, Any]],
        usage: dict[str, Any],
    ) -> AgentResult:
        events.append(AgentEvent(AgentEventType.CANCELLED, request.role, {"reason": "user_cancelled"}))
        return AgentResult(
            succeeded=False,
            error="代理运行已由用户取消。",
            error_type="user_cancelled",
            events=events,
            raw_events=raw_events,
            usage=usage,
            runtime="native-harness",
            runtime_version=identity.get("runtime_version", ""),
            model=identity.get("model", ""),
            runtime_config=identity.get("config", {}),
        )

    @staticmethod
    def _failure(
        request: AgentRequest,
        error: str,
        error_type: str,
        identity: dict[str, Any],
        events: list[AgentEvent] | None = None,
        raw_events: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> AgentResult:
        events = list(events or [])
        events.append(
            AgentEvent(AgentEventType.FAILED, request.role, {"reason": error_type, "error": error})
        )
        return AgentResult(
            succeeded=False,
            error=error,
            error_type=error_type,
            events=events,
            raw_events=list(raw_events or []),
            usage=dict(usage or {}),
            runtime="native-harness",
            runtime_version=identity.get("runtime_version", ""),
            model=identity.get("model", ""),
            runtime_config=identity.get("config", {}),
        )
