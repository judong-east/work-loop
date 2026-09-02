from __future__ import annotations

import base64
import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

from app.domain.context_compaction import estimate_tokens
from app.domain.models import ContextState, ModelAlias, ModelProvider, WorkflowNode
from app.domain.tooling import ToolExecutor, ToolSpec

from .model_protocols import ModelProtocol, RoundRunner, protocol_for
from .resource_center import ResourceCenter


_OUTPUT_CONTRACTS = {
    "requirement": {"understanding": "string", "acceptance_criteria": ["string"], "open_questions": ["string"]},
    "planning": {"steps": ["string"], "risks": ["string"], "artifacts": {}},
    "implementation": {
        "changes": "string",
        "file_changes": [{"operation": "write", "path": "relative/path", "content": "complete file text"}],
        "artifacts": {},
        "decisions": ["string"],
    },
    "review": {"verdict": "pass|revise|blocked", "issues": [], "decisions": ["string"]},
    "testing": {"checks": [], "risks": ["string"], "decisions": ["string"]},
    "tool": {"result": "any"},
    # Long-horizon loop roles; the executor episode reuses the implementation
    # contract so its file_changes flow through the atomic publish path.
    "longhorizon_manager": {
        "route": "execute|done|blocked|ask",
        "task_state": {"completed": ["string"], "incomplete": ["string"], "risks": ["string"], "untrusted": ["string"]},
        "task_contract": "string",
        "subtask": "string",
        "acceptance_criteria": ["string"],
        "related_rounds": [1],
        "question": "string",
    },
    "longhorizon_auditor": {
        "status": "complete|incomplete|blocked",
        "integrity": "clean|suspect|violation",
        "contract_audit": "aligned|unknown|needs_revision",
        "facts": ["string"],
        "gaps": ["string"],
        "blocking_constraints": ["string"],
        "state_update": "string",
    },
}


_TOOL_PROMPT_SUFFIX = (
    " You may call the enabled local workspace search tools before returning the final {closing}. "
    "Use zvec_grep_rg for known strings, symbols, paths, and regular expressions. "
    "Use zvec_grep_search when wording or location is unknown or cross-file semantic discovery is required. "
    "For mixed tasks, discover semantically and then verify with exact search. "
    "Search results are evidence only and never prove that a file was changed."
)

_ELISION_NOTICE = "\n\n[earlier tool rounds were elided to respect the context budget]"


class _CurlStreamResponse:
    """Small file-like adapter around the system curl streaming process.

    Some Windows endpoint-control policies deny sockets opened by a
    PyInstaller/Python executable while allowing the signed system
    ``curl.exe`` client.  The gateway normally stays on urllib; this adapter
    is only created after that process-level access-denied failure.
    """

    def __init__(self, process: subprocess.Popen[bytes], temp_dir: tempfile.TemporaryDirectory[str]):
        self._process = process
        self._temp_dir = temp_dir
        self._stdout = process.stdout
        self._stderr = process.stderr
        self._closed = False

    def __enter__(self) -> "_CurlStreamResponse":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_value, traceback
        if self._closed:
            return
        self._closed = True
        if exc_type is not None and self._process.poll() is None:
            self._process.terminate()
        if self._stdout is not None:
            self._stdout.close()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)
        stderr = b""
        if self._stderr is not None:
            stderr = self._stderr.read() or b""
            self._stderr.close()
        self._temp_dir.cleanup()
        if exc_type is None and self._process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"curl.exe 请求失败（退出码 {self._process.returncode}）"
                + (f": {detail[:300]}" if detail else "")
            )

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        if self._stdout is None:
            raise StopIteration
        line = self._stdout.readline()
        if not line:
            raise StopIteration
        return line

    def read(self, size: int = -1) -> bytes:
        if self._stdout is None:
            return b""
        return self._stdout.read(size)


class OpenAICompatibleGateway:
    """Call OpenAI Chat Completions or Claude Messages resource models."""

    def __init__(self, resources: ResourceCenter, *, timeout_seconds: float = 120):
        self.resources = resources
        self.timeout_seconds = timeout_seconds

    def complete(self, *, model_alias: str, node: WorkflowNode, context: ContextState) -> dict[str, Any]:
        alias, provider, model, protocol, credential = self._resolve(model_alias, node, context)
        raw = self._request_json(
            provider,
            model,
            protocol,
            credential,
            self._messages(node, context),
            max_tokens=self._reserve_tokens(node),
        )
        output = self._json_object(protocol.text(raw))
        output.setdefault("model", alias)
        return output

    @staticmethod
    def _reserve_tokens(node: WorkflowNode) -> int | None:
        """Output allowance the compaction boundary reserved for this call."""

        if not isinstance(node.config, dict):
            return None
        raw = node.config.get("_reserve_tokens")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def complete_with_tools(
        self,
        *,
        model_alias: str,
        node: WorkflowNode,
        context: ContextState,
        tools: list[ToolSpec],
        tool_executor: ToolExecutor,
        max_rounds: int = 8,
        transcript_budget_tokens: int = 0,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run a bounded provider-native tool loop and return final JSON output.

        ``complete`` remains unchanged for compatibility with existing fake
        gateways and callers.  Only the invocation service opts into this
        method when the node/project runtime policy exposes tools.

        ``transcript_budget_tokens`` is the per-request input budget computed by
        the compaction boundary.  It is re-applied before every round, because
        the growing tool transcript would otherwise exceed the very window the
        compactor prepared for.

        The loop body is shared with ``stream_complete_with_tools``; this method
        only drains the public events that a blocking caller cannot observe.
        """

        if not tools:
            return self.complete(model_alias=model_alias, node=node, context=context), []
        loop = self._tool_loop(
            model_alias=model_alias,
            node=node,
            context=context,
            tools=tools,
            tool_executor=tool_executor,
            max_rounds=max_rounds,
            transcript_budget_tokens=transcript_budget_tokens,
            stream=False,
        )
        while True:
            try:
                next(loop)
            except StopIteration as done:
                output, tool_events, _alias = done.value
                return output, tool_events

    def stream_complete_with_tools(
        self,
        *,
        model_alias: str,
        node: WorkflowNode,
        context: ContextState,
        tools: list[ToolSpec] | None = None,
        tool_executor: ToolExecutor | None = None,
        max_rounds: int = 8,
        transcript_budget_tokens: int = 0,
    ) -> Iterator[dict[str, Any]]:
        """Stream provider text while keeping structured/tool results atomic.

        The public stream events deliberately stay provider-neutral.  Text is
        emitted as ``text_delta`` events; provider-native tool call fragments
        are buffered until their arguments form a complete call, executed, and
        returned as ``tool_call``/``tool_result`` events before the next round.
        Chat callers set ``_stream_plain_text`` on the internal node config so
        the visible response is not a half-generated JSON envelope.  Other
        node types retain the existing JSON contract and only emit deltas as
        diagnostics until the final object is complete.

        The loop body is shared with ``complete_with_tools``, so a fix to
        budgeting or tool handling can no longer land in only one of them.
        """

        output, tool_events, alias = yield from self._tool_loop(
            model_alias=model_alias,
            node=node,
            context=context,
            tools=list(tools or []),
            tool_executor=tool_executor,
            max_rounds=max_rounds,
            transcript_budget_tokens=transcript_budget_tokens,
            stream=True,
        )
        yield {
            "type": "done",
            "output": output,
            "model": alias,
            "tool_events": tool_events,
        }

    def _tool_loop(
        self,
        *,
        model_alias: str,
        node: WorkflowNode,
        context: ContextState,
        tools: list[ToolSpec],
        tool_executor: ToolExecutor | None,
        max_rounds: int,
        transcript_budget_tokens: int,
        stream: bool,
    ) -> Generator[dict[str, Any], None, tuple[dict[str, Any], list[dict[str, Any]], str]]:
        """Run the bounded provider-native tool loop shared by both call paths.

        Public events are always yielded; a blocking caller simply discards
        them.  ``stream`` selects the transport for one round, which is the only
        difference between the two paths that survives here.
        """

        if max_rounds <= 0:
            raise ValueError("max_rounds must be positive")
        alias, provider, model, protocol, credential = self._resolve(model_alias, node, context)
        messages = self._messages(node, context)
        if tools:
            closing = "response" if self._plain_text(node) else "JSON"
            messages[0]["content"] = str(messages[0]["content"]) + _TOOL_PROMPT_SUFFIX.format(
                closing=closing
            )
        provider_tools = [protocol.tool_schema(item) for item in tools]
        allowed = {item.name for item in tools}
        executor = tool_executor or (
            lambda _name, _arguments: {"error": "tool executor is not configured"}
        )
        tool_events: list[dict[str, Any]] = []
        reserve_tokens = self._reserve_tokens(node)
        # The tool schemas travel in the same request as the messages, so they
        # must be charged against the input budget rather than silently added on
        # top of it.
        message_budget = self._message_budget(transcript_budget_tokens, provider_tools)

        for round_index in range(1, max_rounds + 1):
            messages = self._fit_transcript(messages, message_budget, protocol)
            result = yield from self._run_round(
                protocol, provider, model, credential, messages,
                tools=provider_tools, max_tokens=reserve_tokens, stream=stream,
            )
            if not result.calls:
                return self._finalize(node, alias, result.text), tool_events, alias
            # Both providers require the assistant tool-call turn to precede its
            # results, so it is echoed back exactly as the provider produced it.
            messages.append(protocol.assistant_turn(result))
            pending: list[tuple[str, dict[str, Any]]] = []
            for call in result.calls:
                event, outcome = yield from self._run_tool(
                    call, round_index, allowed=allowed, executor=executor
                )
                tool_events.append(event)
                pending.append((str(call.get("id", "")), outcome))
            messages.extend(protocol.tool_result_turns(pending))

        # The round budget is exhausted.  Rather than discarding every round of
        # reasoning and evidence, withdraw the tools and require a final answer
        # built from what has already been gathered.
        closing = (
            "请仅依据已获得的证据给出最终回答；若证据不足，请显式说明缺口。"
            if self._plain_text(node)
            else "请仅依据已获得的证据返回最终 JSON 结果；若证据不足，请在结果中显式说明缺口。"
        )
        messages = self._append_user_turn(
            # The final request carries no tools, so the full budget applies.
            self._fit_transcript(messages, transcript_budget_tokens, protocol),
            f"工具调用轮次上限（{max_rounds}）已用尽，不能再调用工具。{closing}",
        )
        result = yield from self._run_round(
            protocol, provider, model, credential, messages,
            tools=[], max_tokens=reserve_tokens, stream=stream,
        )
        output = self._finalize(node, alias, result.text)
        output.setdefault("tool_rounds_exhausted", True)
        return output, tool_events, alias

    def _run_round(
        self,
        protocol: ModelProtocol,
        provider: ModelProvider,
        model: ModelAlias,
        credential: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        max_tokens: int | None,
        stream: bool,
    ) -> RoundRunner:
        """Run one model turn over either transport.

        This is a generator in both branches because of the ``yield from``; the
        blocking branch simply never yields, which is what lets one loop body
        serve the blocking and streaming callers.
        """

        if stream:
            packets = self._request_stream(
                provider, model, protocol, credential, messages,
                tools=tools or None, max_tokens=max_tokens,
            )
            return (yield from protocol.parse_stream(packets))
        raw = self._request_json(
            provider, model, protocol, credential, messages,
            tools=tools or None, max_tokens=max_tokens,
        )
        return protocol.parse_response(raw)

    def _run_tool(
        self,
        call: dict[str, Any],
        round_index: int,
        *,
        allowed: set[str],
        executor: ToolExecutor,
    ) -> Generator[dict[str, Any], None, tuple[dict[str, Any], dict[str, Any]]]:
        """Execute one requested tool call and report it as public events."""

        name = str(call.get("name", ""))
        arguments = call.get("arguments")
        event: dict[str, Any] = {
            "round": round_index,
            "call_id": str(call.get("id", "")),
            "name": name,
            "arguments": arguments,
        }
        yield {"type": "tool_call", **event}
        if name not in allowed:
            result: dict[str, Any] = {"error": f"tool is not enabled for this node: {name}"}
            event["error"] = result["error"]
        elif not isinstance(arguments, dict):
            result = {"error": "tool arguments must be a JSON object"}
            event["error"] = result["error"]
        else:
            try:
                result = executor(name, arguments)
                if not isinstance(result, dict):
                    result = {"result": result}
            except Exception as error:  # tool errors are fed back to the model
                result = {"error": str(error)}
                event["error"] = result["error"]
        result = self._bounded_tool_result(result)
        event["result"] = result
        yield {"type": "tool_result", "event": event}
        return event, result

    @staticmethod
    def _plain_text(node: WorkflowNode) -> bool:
        """Chat callers opt out of the JSON envelope for the visible answer."""

        config = node.config if isinstance(node.config, dict) else {}
        return bool(config.get("_stream_plain_text"))

    def _finalize(self, node: WorkflowNode, alias: str, text: str) -> dict[str, Any]:
        output = {"result": text} if self._plain_text(node) else self._json_object(text)
        output.setdefault("model", alias)
        return output

    @staticmethod
    def _access_denied(error: BaseException) -> bool:
        """Return whether a network failure is the Windows access-denied case."""

        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if getattr(current, "winerror", None) == 5:
                return True
            args = getattr(current, "args", ())
            if args and args[0] in {5, 13}:
                return True
            current = getattr(current, "reason", None)
            if not isinstance(current, BaseException):
                break
        text = str(error).lower()
        return "winerror 5" in text or "拒绝访问" in text or "access is denied" in text

    @staticmethod
    def _probe_error_type(status: int, detail: str) -> str:
        lowered = detail.lower()
        if status == 429:
            return "rate_limited"
        if any(token in lowered for token in ("model_not_found", "model not found", "no available channel")):
            return "model_not_found"
        if status in {401, 403}:
            if any(token in lowered for token in ("quota", "credit limit", "insufficient_user_quota", "余额")):
                return "quota_exceeded"
            return "authentication_failed"
        return "http_error"

    @staticmethod
    def _curl_executable() -> str:
        if sys.platform != "win32":
            return ""
        return shutil.which("curl.exe") or shutil.which("curl") or ""

    @staticmethod
    def _curl_creation_flags() -> int:
        # Do not flash a console window for the desktop app.  The attribute is
        # absent on non-Windows test hosts, hence the defensive lookup.
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @staticmethod
    def _curl_headers(request: urllib.request.Request) -> bytes:
        # Credentials are deliberately written to curl's stdin instead of the
        # command line, where they would be visible in process listings.
        lines = [f"{name}: {value}" for name, value in request.header_items()]
        return ("\n".join(lines) + "\n").encode("utf-8") if lines else b"\n"

    def _curl_request(self, request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        executable = self._curl_executable()
        if not executable:
            raise RuntimeError(
                "Python 进程被 Windows 拒绝联网，且系统未找到 curl.exe；"
                "请允许 Workloop.exe 出站访问 HTTPS。"
            )
        with tempfile.TemporaryDirectory(prefix="workloop-curl-") as temp_dir:
            root = Path(temp_dir)
            response_path = root / "response.body"
            body_path: Path | None = None
            if request.data is not None:
                body_path = root / "request.body"
                body_path.write_bytes(request.data)
            command = [
                executable,
                "--silent",
                "--show-error",
                "--location",
                "--compressed",
                "--request",
                request.get_method(),
                "--url",
                request.full_url,
                "--header",
                "@-",
                "--output",
                str(response_path),
                "--write-out",
                "%{http_code}",
                "--max-time",
                str(max(1, int(timeout))),
            ]
            if body_path is not None:
                command.extend(["--data-binary", f"@{body_path}"])
            try:
                completed = subprocess.run(
                    command,
                    input=self._curl_headers(request),
                    capture_output=True,
                    timeout=max(5, timeout + 5),
                    check=False,
                    creationflags=self._curl_creation_flags(),
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise RuntimeError(f"curl.exe 无法启动：{error}") from error
            status_text = completed.stdout.decode("ascii", errors="ignore").strip()
            match = re.search(r"(\d{3})$", status_text)
            body = response_path.read_bytes() if response_path.is_file() else b""
            if match is None or match.group(1) == "000":
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    "curl.exe 无法连接供应商"
                    + (f": {detail[:300]}" if detail else f"（退出码 {completed.returncode}）")
                )
            return int(match.group(1)), body

    def _read_response(self, request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        """Read one response, falling back to system curl on WinError 5."""

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(getattr(response, "status", 200)), response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if not self._access_denied(error):
                raise
            return self._curl_request(request, timeout)

    def _stream_response(self, request: urllib.request.Request, timeout: float):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if not self._access_denied(error):
                raise
            return self._curl_stream(request, timeout)

    def _curl_stream(self, request: urllib.request.Request, timeout: float) -> _CurlStreamResponse:
        executable = self._curl_executable()
        if not executable:
            raise RuntimeError(
                "Python 进程被 Windows 拒绝联网，且系统未找到 curl.exe；"
                "请允许 Workloop.exe 出站访问 HTTPS。"
            )
        temp_dir = tempfile.TemporaryDirectory(prefix="workloop-curl-stream-")
        root = Path(temp_dir.name)
        body_path: Path | None = None
        if request.data is not None:
            body_path = root / "request.body"
            body_path.write_bytes(request.data)
        command = [
            executable,
            "--silent",
            "--show-error",
            "--location",
            "--compressed",
            "--no-buffer",
            "--request",
            request.get_method(),
            "--url",
            request.full_url,
            "--header",
            "@-",
            "--output",
            "-",
            "--max-time",
            str(max(1, int(timeout))),
        ]
        if body_path is not None:
            command.extend(["--data-binary", f"@{body_path}"])
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=self._curl_creation_flags(),
            )
            if process.stdin is not None:
                process.stdin.write(self._curl_headers(request))
                process.stdin.close()
        except (OSError, subprocess.SubprocessError) as error:
            temp_dir.cleanup()
            raise RuntimeError(f"curl.exe 无法启动：{error}") from error
        return _CurlStreamResponse(process, temp_dir)

    def _request_stream(
        self,
        provider: ModelProvider,
        model: ModelAlias,
        protocol: ModelProtocol,
        credential: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        payload = protocol.payload(model, messages, tools=tools, max_tokens=max_tokens, stream=True)
        request = urllib.request.Request(
            self._request_url(provider, credential, protocol),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=self._headers(provider, credential, protocol, stream=True),
        )
        try:
            with self._stream_response(request, self.timeout_seconds) as response:
                yield from self._iter_sse_events(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            if error.code in {400, 404, 405, 422}:
                # Some local OpenAI-compatible servers reject the ``stream``
                # flag instead of ignoring it.  Retry once through the proven
                # one-shot JSON path so the caller still receives a valid SSE
                # envelope (one delta followed by done).
                try:
                    raw = self._request_json(
                        provider,
                        model,
                        protocol,
                        credential,
                        messages,
                        tools=tools,
                        max_tokens=max_tokens,
                    )
                    yield {"event": "json", "data": raw}
                    return
                except Exception:  # noqa: BLE001 - retain the original stream error
                    pass
            raise RuntimeError(f"模型流式接口返回 HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RuntimeError(f"无法连接模型流式接口: {error}") from error

    @staticmethod
    def _iter_sse_events(response) -> Iterator[dict[str, Any]]:
        """Parse SSE records and tolerate local endpoints returning one JSON body."""

        event_name = ""
        data_lines: list[str] = []
        saw_sse = False
        raw_lines: list[str] = []

        def emit_data() -> dict[str, Any] | None:
            if not data_lines:
                return None
            raw_data = "\n".join(data_lines)
            data_lines.clear()
            if raw_data.strip() == "[DONE]":
                return {"event": event_name or "done", "data": None}
            try:
                parsed = json.loads(raw_data)
            except json.JSONDecodeError as error:
                raise ValueError("模型 SSE data 不是合法 JSON") from error
            return {"event": event_name, "data": parsed}

        try:
            response_iter = iter(response)
        except TypeError:
            response_iter = iter([response.read()])
        for raw_line in response_iter:
            if isinstance(raw_line, bytes):
                chunk = raw_line.decode("utf-8", errors="replace")
            else:
                chunk = str(raw_line)
            # HTTPResponse normally iterates by line, while lightweight local
            # adapters and tests may yield a whole SSE record at once.
            for line in chunk.splitlines():
                if not line:
                    packet = emit_data()
                    if packet is not None:
                        saw_sse = True
                        yield packet
                    event_name = ""
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    saw_sse = True
                    event_name = line[6:].lstrip()
                    continue
                if line.startswith("data:"):
                    saw_sse = True
                    data_lines.append(line[5:].lstrip())
                    continue
                if not saw_sse:
                    raw_lines.append(line)

        packet = emit_data()
        if packet is not None:
            yield packet
        if not saw_sse and raw_lines:
            raw_data = "\n".join(raw_lines).strip()
            try:
                parsed = json.loads(raw_data)
            except json.JSONDecodeError as error:
                raise ValueError("模型响应既不是 SSE 也不是合法 JSON") from error
            yield {"event": "json", "data": parsed}

    @staticmethod
    def _bounded_tool_result(result: dict[str, Any], limit: int = 12_000) -> dict[str, Any]:
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded) <= limit:
            return result
        # Preserve the structured envelope and clip only the payload field, so
        # route/freshness/degraded survive truncation instead of collapsing into
        # one escaped JSON string.
        if isinstance(result.get("output"), str):
            preserved = {
                key: value for key, value in result.items()
                if key != "output" and not isinstance(value, (dict, list))
            }
            overhead = len(json.dumps({**preserved, "output": "", "truncated": True}, ensure_ascii=False, default=str))
            room = max(200, limit - overhead)
            return {
                **preserved,
                "truncated": True,
                "output": result["output"][:room] + "\n…[tool result truncated]",
            }
        return {
            "truncated": True,
            "output": encoded[:limit] + "\n…[tool result truncated]",
        }

    @staticmethod
    def _message_budget(transcript_budget_tokens: int, provider_tools: list[dict[str, Any]]) -> int:
        """Input budget left for messages once the tool schemas are charged."""

        if transcript_budget_tokens <= 0:
            return transcript_budget_tokens
        overhead = estimate_tokens(provider_tools) if provider_tools else 0
        return max(1, transcript_budget_tokens - overhead)

    @staticmethod
    def _append_user_turn(messages: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
        """Append a user instruction without creating two adjacent user turns.

        Claude rejects consecutive same-role turns, and after transcript
        trimming the last message may already be a user turn.
        """

        if messages and messages[-1].get("role") == "user":
            last = dict(messages[-1])
            content = last.get("content")
            if isinstance(content, list):
                last["content"] = [*content, {"type": "text", "text": text}]
            else:
                last["content"] = f"{content}\n\n{text}"
            return [*messages[:-1], last]
        return [*messages, {"role": "user", "content": text}]

    @staticmethod
    def _fit_transcript(
        messages: list[dict[str, Any]],
        budget_tokens: int,
        protocol: ModelProtocol,
    ) -> list[dict[str, Any]]:
        """Drop the oldest completed tool rounds until the request fits.

        A tool loop appends an assistant tool-call message plus its results on
        every round.  Without this, request size grows without bound across
        rounds and silently exceeds the window the compactor just budgeted for.
        The system prompt and the original user payload are never dropped, and
        assistant/tool-result pairing is preserved so neither provider rejects
        the request.
        """

        if budget_tokens <= 0 or estimate_tokens(messages) <= budget_tokens:
            return messages
        prefix_len = 0
        for message in messages:
            if message.get("role") in ("system", "user") and prefix_len < 2:
                prefix_len += 1
                continue
            break
        head = protocol.elide(messages[:prefix_len], _ELISION_NOTICE)
        rounds: list[list[dict[str, Any]]] = []
        for message in messages[prefix_len:]:
            # An assistant message starts a new round; its tool results follow,
            # so dropping a round removes the pair together and never orphans a
            # tool result from its tool call.
            if message.get("role") == "assistant" or not rounds:
                rounds.append([message])
            else:
                rounds[-1].append(message)
        while rounds and estimate_tokens(head + [m for r in rounds for m in r]) > budget_tokens:
            rounds.pop(0)
        if not rounds:
            # Even one round does not fit; keep the prefix and the notice so the
            # model is told why its evidence is missing.
            return head
        return head + [message for round_ in rounds for message in round_]

    def summarize_context(
        self,
        *,
        model_alias: str,
        summary_input: str,
        previous_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate a structured compaction summary without tools."""

        if len(summary_input) > 60_000:
            summary_input = summary_input[:60_000] + "\n…[summary input truncated]"
        provider, model, protocol, credential = self._connection(model_alias)
        instructions = (
            "Summarize the supplied durable workflow context for a later model. "
            "Return one JSON object only, without markdown. Preserve user goals, "
            "constraints, gates, verified progress, open questions, searched files, "
            "and modified files. Do not invent facts."
        )
        payload = {
            "context": summary_input,
            "previous_summary": previous_summary,
        }
        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        raw = self._request_json(
            provider,
            model,
            protocol,
            credential,
            messages,
            max_tokens=min(model.max_tokens or 1_500, 1_500),
        )
        return self._json_object(protocol.text(raw))

    def _connection(
        self, model_alias: str
    ) -> tuple[ModelProvider, ModelAlias, ModelProtocol, str]:
        """Resolve one alias to its provider, protocol adapter, and credential."""

        provider, model = self.resources.resolve(model_alias)
        credential = self.resources.credential(provider.provider_id)
        if provider.auth_type != "none" and not credential:
            raise ValueError(f"供应商 {provider.label} 尚未配置认证凭据。")
        return provider, model, protocol_for(model.protocol or provider.protocols[0]), credential

    def _resolve(
        self,
        model_alias: str,
        node: WorkflowNode,
        context: ContextState,
    ) -> tuple[str, ModelProvider, ModelAlias, ModelProtocol, str]:
        project = context.inputs.get("project", {})
        project_default = str(project.get("default_model", "")) if isinstance(project, dict) else ""
        alias = model_alias or project_default or self.resources.default_alias(node.node_type)
        if not alias:
            raise ValueError(f"节点 {node.node_id} 没有可用模型，请先在资源中心登记模型别名。")
        provider, model, protocol, credential = self._connection(alias)
        return alias, provider, model, protocol, credential

    def _request_json(
        self,
        provider: ModelProvider,
        model: ModelAlias,
        protocol: ModelProtocol,
        credential: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload = protocol.payload(model, messages, tools=tools, max_tokens=max_tokens)
        request = urllib.request.Request(
            self._request_url(provider, credential, protocol),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=self._headers(provider, credential, protocol),
        )
        try:
            status, body = self._read_response(request, self.timeout_seconds)
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as error:
            raise RuntimeError(f"无法连接模型接口: {error}") from error
        if status >= 400:
            detail = body.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"模型接口返回 HTTP {status}: {detail}")
        try:
            raw = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("模型接口响应不是合法 JSON") from error
        if not isinstance(raw, dict):
            raise ValueError("模型接口响应必须是 JSON 对象")
        return raw

    def probe(self, model_alias: str) -> dict[str, Any]:
        provider, model = self.resources.resolve(model_alias)
        protocol = protocol_for(model.protocol or provider.protocols[0])
        credential = self.resources.credential(provider.provider_id)
        if provider.auth_type != "none" and not credential:
            return {
                "ok": False, "alias": model.alias, "protocol": protocol.name,
                "error_type": "authentication_missing", "error": "尚未配置认证凭据。",
            }
        # A probe deliberately sends the minimum both protocols accept: no
        # system turn, no temperature.  Reusing the full payload builder would
        # add fields that some models reject, turning a reachable endpoint into
        # a false negative.
        payload = {
            "model": model.model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 1,
        }
        request = urllib.request.Request(
            self._request_url(provider, credential, protocol),
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=self._headers(provider, credential, protocol),
        )
        started = time.monotonic()
        try:
            status, body = self._read_response(request, min(self.timeout_seconds, 20))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:300]
            return {
                "ok": False, "alias": model.alias, "protocol": protocol.name,
                "error_type": self._probe_error_type(error.code, detail), "error": f"HTTP {error.code}: {detail}",
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return {
                "ok": False, "alias": model.alias, "protocol": protocol.name,
                "error_type": "connection_failed", "error": str(error),
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        except RuntimeError as error:
            return {
                "ok": False, "alias": model.alias, "protocol": protocol.name,
                "error_type": "connection_failed", "error": str(error),
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        if status >= 400:
            detail = body.decode("utf-8", errors="replace")[:300]
            return {
                "ok": False, "alias": model.alias, "protocol": protocol.name,
                "error_type": self._probe_error_type(status, detail), "error": f"HTTP {status}: {detail}",
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        return {
            "ok": True, "alias": model.alias, "protocol": protocol.name,
            "error_type": "", "error": "",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }

    def list_models(self, provider_id: str, protocol: str = "") -> list[str]:
        provider = next(
            (item for item in self.resources.list_providers() if item.provider_id == provider_id),
            None,
        )
        if provider is None:
            raise KeyError(provider_id)
        selected_protocol = protocol or provider.protocols[0]
        if selected_protocol not in provider.protocols:
            raise ValueError(f"供应商 {provider.label} 不支持协议 {selected_protocol}。")
        credential = self.resources.credential(provider.provider_id)
        if provider.auth_type != "none" and not credential:
            raise ValueError(f"供应商 {provider.label} 尚未配置认证凭据。")

        adapter = protocol_for(selected_protocol)
        request = urllib.request.Request(
            self._models_url(provider, credential, adapter),
            method="GET",
            headers=self._headers(provider, credential, adapter),
        )
        try:
            status, body = self._read_response(request, min(self.timeout_seconds, 20))
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as error:
            raise RuntimeError(f"无法连接供应商模型列表接口: {error}") from error
        if status >= 400:
            detail = body.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"获取模型列表失败（HTTP {status}）: {detail}")
        try:
            raw = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("供应商模型列表不是合法 JSON。") from error
        return self._model_ids(raw)

    @staticmethod
    def _messages(node: WorkflowNode, context: ContextState) -> list[dict[str, Any]]:
        contract = _OUTPUT_CONTRACTS.get(node.node_type)
        if contract is None:
            fields = node.config.get("_output_fields", [])
            contract = {str(field): "any" for field in fields} or {"result": "any"}
        public_config = {key: value for key, value in node.config.items() if not key.startswith("_")}
        context_pack = node.config.get("_context_pack")
        context_view = node.config.get("_context_view")
        shared_context = context.to_dict()
        if isinstance(context_view, dict) and isinstance(context_view.get("shared_context"), dict):
            shared_context = context_view["shared_context"]
        user_payload: dict[str, Any] = {
            "node_id": node.node_id,
            "node_type": node.node_type,
            "instructions": node.prompt_template,
            "config": public_config,
            "shared_context": shared_context,
        }
        if isinstance(context_pack, dict):
            # The authoritative state is already carried by
            # ``shared_context``.  Keep the diagnostic copy in the internal
            # node config, but omit its duplicate from the wire payload.
            wire_context_pack = copy.deepcopy(context_pack)
            wire_context_pack.pop("shared_context", None)
            user_payload["context_pack"] = wire_context_pack
        if isinstance(node.config, dict) and node.config.get("_stream_plain_text"):
            system_content = (
                "You are one node in a durable multi-agent workflow. "
                "Return the user-facing answer as plain text only, without markdown fences or a JSON envelope. "
                "Never claim a file was changed unless the request is actually supported by the workflow."
            )
        else:
            system_content = (
                "You are one node in a durable multi-agent workflow. "
                "Return one JSON object only, without markdown fences. "
                "Never claim a file was changed unless it is included in file_changes. "
                "File paths must be workspace-relative and file contents must be complete. "
                f"Required output shape: {json.dumps(contract, ensure_ascii=False)}"
            )
        return [
            {
                "role": "system",
                "content": system_content,
            },
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False),
            },
        ]

    @staticmethod
    def _headers(
        provider: ModelProvider,
        credential: str,
        protocol: ModelProtocol,
        *,
        stream: bool = False,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            **protocol.headers(),
        }
        if provider.auth_type == "none":
            return headers
        if provider.auth_type == "bearer":
            prefix = provider.auth_prefix.strip() or "Bearer"
            headers["Authorization"] = f"{prefix} {credential}".strip()
        elif provider.auth_type == "api_key":
            # RouterToken exposes an OpenAI-compatible surface but its chat
            # endpoint authenticates with Bearer even though ``/v1/models``
            # also accepts x-api-key.  Keep the provider form compatible with
            # other API-key vendors while making the known RouterToken host
            # work for both discovery and actual model requests.
            host = (urllib.parse.urlsplit(provider.base_url).hostname or "").lower()
            if host in {"api.tokenrouter.com", "api.tokenrouter.io"}:
                headers["Authorization"] = f"Bearer {credential}"
            else:
                headers["x-api-key"] = credential
        elif provider.auth_type == "token":
            prefix = provider.auth_prefix.strip()
            if not prefix or prefix.lower() == "bearer":
                prefix = "Token"
            headers["Authorization"] = f"{prefix} {credential}".strip()
        elif provider.auth_type == "basic":
            username = str(provider.metadata.get("username", ""))
            encoded = base64.b64encode(f"{username}:{credential}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        elif provider.auth_type == "custom_header":
            value = f"{provider.auth_prefix.strip()} {credential}".strip()
            headers[provider.auth_header] = value
        return headers

    @classmethod
    def _request_url(cls, provider: ModelProvider, credential: str, protocol: ModelProtocol) -> str:
        return cls._apply_query_auth(protocol.endpoint(provider.base_url), provider, credential)

    @classmethod
    def _models_url(cls, provider: ModelProvider, credential: str, protocol: ModelProtocol) -> str:
        return cls._apply_query_auth(
            protocol.models_endpoint(provider.base_url), provider, credential
        )

    @staticmethod
    def _apply_query_auth(endpoint: str, provider: ModelProvider, credential: str) -> str:
        if provider.auth_type != "query_param":
            return endpoint
        parts = urllib.parse.urlsplit(endpoint)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        query.append((provider.auth_header, credential))
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment))

    @staticmethod
    def _model_ids(raw: Any) -> list[str]:
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("data", raw.get("models", []))
        else:
            items = []
        if not isinstance(items, list):
            raise ValueError("供应商模型列表缺少 data 或 models 数组。")

        models: list[str] = []
        for item in items:
            if isinstance(item, str):
                model_id = item
            elif isinstance(item, dict):
                model_id = next(
                    (str(item[key]) for key in ("id", "model", "name") if item.get(key)),
                    "",
                )
            else:
                model_id = ""
            model_id = model_id.strip()
            if model_id and model_id not in models:
                models.append(model_id)
        return models

    @staticmethod
    def _json_object(content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("模型没有返回合法 JSON 对象") from error
        if not isinstance(value, dict):
            raise ValueError("模型输出必须是 JSON 对象")
        return value
