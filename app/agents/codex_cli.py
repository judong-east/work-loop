from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from app.agents.contracts import (
    AgentAccess,
    AgentEvent,
    AgentEventType,
    AgentRequest,
    AgentResult,
)
from app.agents.runtime import AgentRuntime
from app.core.process_tree import ProcessTreeHandle, process_group_options
from app.core.redaction import redact, redact_value


@dataclass(frozen=True)
class CodexCliProfile:
    command: list[str] = field(default_factory=lambda: ["codex"])
    model: str = ""
    ignore_user_config: bool = True


def _is_workloop_controlled_argument(argument: str) -> bool:
    return argument.startswith("-")


class CodexCliRuntime(AgentRuntime):
    def __init__(self, profile: CodexCliProfile):
        if not profile.command:
            raise ValueError("Codex CLI command 不能为空。")
        if not profile.model.strip():
            raise ValueError("Codex executor model 不能为空。")
        controlled = [
            argument
            for argument in profile.command[1:]
            if _is_workloop_controlled_argument(argument)
        ]
        if controlled:
            raise ValueError(
                "Codex CLI launcher 不能覆盖 Workloop 权限参数：" + ", ".join(controlled)
            )
        self.profile = profile
        resolved = shutil.which(profile.command[0]) if len(profile.command) == 1 else None
        self.command = [resolved or profile.command[0], *profile.command[1:]]
        self._version_cache = ""
        self._running: dict[str, ProcessTreeHandle] = {}
        self._pending: set[str] = set()
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    def invoke(self, request: AgentRequest) -> AgentResult:
        started = time.monotonic()
        with self._lock:
            if request.task_id in self._pending or request.task_id in self._running:
                duplicate = AgentResult(
                    succeeded=False,
                    error=f"任务 {request.task_id} 已有 Codex 运行。",
                    error_type="policy_blocked",
                    runtime="codex-cli",
                    model=self.profile.model,
                )
            else:
                duplicate = None
                self._pending.add(request.task_id)
        if duplicate is not None:
            return self._ensure_terminal_event(duplicate, request.role)
        try:
            result = self._invoke_pending(request, started)
            return self._ensure_terminal_event(result, request.role)
        finally:
            with self._lock:
                self._pending.discard(request.task_id)
                if request.task_id not in self._running:
                    self._cancelled.discard(request.task_id)

    def _invoke_pending(self, request: AgentRequest, started: float) -> AgentResult:
        total_deadline = started + request.budget.total_timeout_seconds
        described = {**self._identity(), "config": self._runtime_config(request)}
        identity = {
            "runtime": described["runtime"],
            "runtime_version": described["runtime_version"],
            "model": described["model"],
            "runtime_config": described["config"],
        }
        rejected = self._validate_request(request, identity)
        if rejected is not None:
            return rejected
        if self._is_cancelled(request.task_id):
            return self._cancelled_result(request, identity)
        version, version_error_type, version_error = self._probe_version(
            request.task_id,
            total_deadline,
        )
        identity["runtime_version"] = version
        if version_error_type:
            if version_error_type == "user_cancelled":
                return self._cancelled_result(request, identity)
            return AgentResult(
                succeeded=False,
                error=version_error,
                error_type=version_error_type,
                **identity,
            )

        with tempfile.TemporaryDirectory(prefix="workloop-codex-") as temporary:
            temp = Path(temporary)
            schema_path = temp / "execution-result.schema.json"
            output_path = temp / "last-message.json"
            schema_path.write_text(
                json.dumps(_execution_result_schema(), ensure_ascii=False),
                encoding="utf-8",
            )
            command = self._command(request, schema_path, output_path)
            if time.monotonic() >= total_deadline:
                return AgentResult(
                    succeeded=False,
                    error="Codex 调用超时。",
                    error_type="call_timeout",
                    **identity,
                )
            environment = dict(os.environ)
            environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
            try:
                process = subprocess.Popen(
                    command,
                    cwd=request.workspace,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=environment,
                    **process_group_options(),
                )
            except OSError as error:
                return AgentResult(
                    succeeded=False,
                    error=f"无法启动 Codex CLI：{error}",
                    error_type="environment_missing",
                    **identity,
                )

            tree = ProcessTreeHandle(process)
            cancelled = self._activate_tree(request.task_id, tree)
            if cancelled:
                tree.terminate()
            try:
                return self._collect(
                    request,
                    process,
                    tree,
                    output_path,
                    identity,
                    total_deadline,
                )
            finally:
                self._deactivate_tree(request.task_id, tree)
                tree.close()

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            tree = self._running.get(task_id)
            self._cancelled.add(task_id)
        if tree is not None:
            tree.terminate()
        return True

    def _collect(
        self,
        request: AgentRequest,
        process: subprocess.Popen[str],
        tree: ProcessTreeHandle,
        output_path: Path,
        identity: dict,
        total_deadline: float,
    ) -> AgentResult:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            tree.terminate()
            return AgentResult(
                succeeded=False,
                error="Codex CLI 管道初始化失败。",
                error_type="environment_missing",
                **identity,
            )
        lines: queue.Queue[tuple[str, str | None]] = queue.Queue()
        readers = [
            self._read_stream(process.stdout, "stdout", lines),
            self._read_stream(process.stderr, "stderr", lines),
        ]
        input_errors: list[str] = []
        input_writer = self._write_stdin(process.stdin, request.instructions, input_errors)
        last_activity = time.monotonic()
        open_streams = 2
        raw_events: list[dict] = []
        events: list[AgentEvent] = []
        stderr: list[str] = []
        protocol_errors: list[str] = []
        session_id = request.session_id
        usage: dict = {}
        final_message = ""
        failure_type = ""
        provider_error = ""
        turn_completed = False

        while open_streams:
            now = time.monotonic()
            idle_deadline = last_activity + request.budget.idle_timeout_seconds
            failure_type = self._expired_budget(now, total_deadline, idle_deadline)
            if failure_type:
                tree.terminate()
                break
            try:
                source, line = lines.get(
                    timeout=min(0.1, total_deadline - now, idle_deadline - now)
                )
            except queue.Empty:
                continue
            if line is None:
                open_streams -= 1
                continue
            if source == "stderr":
                stderr.append(line)
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                excerpt = redact(line[:200], request.policy.redact_patterns)
                protocol_errors.append(f"无法解析 Codex JSONL：{error}: {excerpt}")
                continue
            if not isinstance(raw, dict):
                protocol_errors.append("Codex JSONL 事件必须是对象。")
                continue
            last_activity = time.monotonic()
            raw = redact_value(raw, request.policy.redact_patterns)
            raw_events.append(raw)
            normalized = _normalize_event(raw, request.role)
            events.extend(normalized)
            if raw.get("type") == "thread.started" and isinstance(raw.get("thread_id"), str):
                session_id = raw["thread_id"]
            if raw.get("type") == "turn.completed" and isinstance(raw.get("usage"), dict):
                usage = dict(raw["usage"])
                turn_completed = True
            if raw.get("type") in {"turn.failed", "error"}:
                provider_error = str(raw.get("message") or raw.get("error") or "Codex 运行失败。")
            item = raw.get("item")
            if (
                raw.get("type") == "item.completed"
                and isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                final_message = item["text"]

        if process.poll() is None and not failure_type:
            now = time.monotonic()
            idle_deadline = last_activity + request.budget.idle_timeout_seconds
            remaining = min(total_deadline, idle_deadline) - now
            if remaining <= 0:
                failure_type = self._expired_budget(now, total_deadline, idle_deadline)
                tree.terminate()
            else:
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    now = time.monotonic()
                    failure_type = self._expired_budget(now, total_deadline, idle_deadline)
                    tree.terminate()
        if process.poll() is None:
            tree.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        return_code = process.poll()
        for reader in readers:
            reader.join(timeout=0.1)
        input_writer.join(timeout=0.1)
        process.stdout.close()
        process.stderr.close()

        with self._lock:
            cancelled = request.task_id in self._cancelled
            self._cancelled.discard(request.task_id)
        if cancelled:
            events.append(AgentEvent(AgentEventType.CANCELLED, request.role))
            return AgentResult(
                succeeded=False,
                session_id=session_id,
                error="Codex 运行已由用户取消。",
                error_type="user_cancelled",
                final_message=final_message,
                events=events,
                raw_events=raw_events,
                usage=usage,
                **identity,
            )
        if failure_type:
            events.append(AgentEvent(AgentEventType.FAILED, request.role, {"reason": failure_type}))
            return AgentResult(
                succeeded=False,
                session_id=session_id,
                error="Codex 调用超时。" if failure_type == "call_timeout" else "Codex 长时间没有事件。",
                error_type=failure_type,
                final_message=final_message,
                events=events,
                raw_events=raw_events,
                usage=usage,
                **identity,
            )
        if protocol_errors:
            return AgentResult(
                succeeded=False,
                session_id=session_id,
                error="；".join(protocol_errors),
                error_type="structured_output_failed",
                events=events,
                raw_events=raw_events,
                usage=usage,
                **identity,
            )
        if input_errors:
            return AgentResult(
                succeeded=False,
                session_id=session_id,
                error=f"无法向 Codex stdin 写入指令：{input_errors[0]}",
                error_type="runtime_failed",
                events=events,
                raw_events=raw_events,
                usage=usage,
                **identity,
            )
        if provider_error:
            return AgentResult(
                succeeded=False,
                session_id=session_id,
                error=redact(provider_error, request.policy.redact_patterns),
                error_type="runtime_failed",
                events=events,
                raw_events=raw_events,
                usage=usage,
                **identity,
            )
        if return_code != 0:
            return AgentResult(
                succeeded=False,
                session_id=session_id,
                error=redact("\n".join(stderr).strip(), request.policy.redact_patterns)
                or f"Codex CLI 退出码 {return_code}。",
                error_type="runtime_failed",
                final_message=final_message,
                events=events,
                raw_events=raw_events,
                usage=usage,
                **identity,
            )
        if not session_id or not turn_completed:
            missing = "session" if not session_id else "turn.completed"
            return AgentResult(
                succeeded=False,
                session_id=session_id,
                error=f"Codex JSONL 缺少必需事件：{missing}。",
                error_type="structured_output_failed",
                events=events,
                raw_events=raw_events,
                usage=usage,
                **identity,
            )
        try:
            output = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return AgentResult(
                succeeded=False,
                session_id=session_id,
                error=f"Codex 最终结构化结果无效：{error}",
                error_type="structured_output_failed",
                final_message=final_message,
                events=events,
                raw_events=raw_events,
                usage=usage,
                **identity,
            )
        if not isinstance(output, dict):
            return AgentResult(
                succeeded=False,
                session_id=session_id,
                error="Codex 最终结构化结果必须是对象。",
                error_type="structured_output_failed",
                events=events,
                raw_events=raw_events,
                usage=usage,
                **identity,
            )
        output = redact_value(output, request.policy.redact_patterns)
        return AgentResult(
            succeeded=True,
            output=output,
            session_id=session_id,
            final_message=final_message,
            events=events,
            raw_events=raw_events,
            usage=usage,
            **identity,
        )

    def _read_stream(
        self,
        stream: TextIO,
        name: str,
        lines: queue.Queue[tuple[str, str | None]],
    ) -> threading.Thread:
        def read() -> None:
            try:
                for line in stream:
                    lines.put((name, line.rstrip("\r\n")))
            finally:
                lines.put((name, None))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        return reader

    def _write_stdin(
        self,
        stream: TextIO,
        instructions: str,
        errors: list[str],
    ) -> threading.Thread:
        def write() -> None:
            try:
                stream.write(instructions)
                stream.flush()
            except OSError as error:
                errors.append(str(error))
            finally:
                stream.close()

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        return writer

    def _command(self, request: AgentRequest, schema_path: Path, output_path: Path) -> list[str]:
        command = [
            *self.command,
            "--ask-for-approval",
            "never",
            "--sandbox",
            "workspace-write",
            "--config",
            "sandbox_workspace_write.network_access=false",
            "--cd",
            str(request.workspace),
            "exec",
        ]
        if request.session_id:
            command.append("resume")
        command.extend(["--json", "--model", self.profile.model])
        if self.profile.ignore_user_config:
            command.append("--ignore-user-config")
        command.extend(
            [
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
        )
        if request.session_id:
            command.append(request.session_id)
        command.append("-")
        return command

    def _validate_request(
        self,
        request: AgentRequest,
        identity: dict,
    ) -> AgentResult | None:
        if request.role != "executor" or request.access is not AgentAccess.WORKSPACE_WRITE:
            return AgentResult(
                succeeded=False,
                error="CodexCliRuntime 只接受 workspace-write executor 请求。",
                error_type="policy_blocked",
                **identity,
            )
        if request.policy.network_allowed:
            return AgentResult(
                succeeded=False,
                error="Codex 网络权限尚未获得独立人工授权。",
                error_type="permission_required",
                **identity,
            )
        if request.budget.total_timeout_seconds <= 0 or request.budget.idle_timeout_seconds <= 0:
            return AgentResult(
                succeeded=False,
                error="Codex 运行预算必须是正数。",
                error_type="budget_exhausted",
                **identity,
            )
        return None

    def _identity(self) -> dict[str, str]:
        with self._lock:
            version = self._version_cache or "unknown"
        return {
            "runtime": "codex-cli",
            "runtime_version": version,
            "model": self.profile.model,
        }

    def describe(self, request: AgentRequest) -> dict:
        return {
            "runtime": "codex-cli",
            "runtime_version": self._version_cache or "unknown",
            "model": self.profile.model,
            "config": self._runtime_config(request),
        }

    def health_check(self) -> dict:
        version, error_type, error = self._probe_version(
            "__codex_health_check__",
            time.monotonic() + 10,
            force=True,
        )
        authenticated = False
        if not error_type:
            authenticated, error_type, error = self._authentication_status()
        return {
            "available": not error_type and authenticated,
            "authenticated": authenticated,
            "runtime": "codex-cli",
            "runtime_version": version,
            "model": self.profile.model,
            "error_type": error_type,
            "error": error,
        }

    def _is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._cancelled

    def _cancelled_result(self, request: AgentRequest, identity: dict) -> AgentResult:
        return AgentResult(
            succeeded=False,
            session_id=request.session_id,
            error="Codex 运行已由用户取消。",
            error_type="user_cancelled",
            events=[AgentEvent(AgentEventType.CANCELLED, request.role)],
            **identity,
        )

    def _runtime_config(self, request: AgentRequest) -> dict:
        return {
            "approval_policy": "never",
            "sandbox": "workspace-write",
            "ignore_user_config": self.profile.ignore_user_config,
            "network_allowed": request.policy.network_allowed,
            "allowed_commands": request.policy.allowed_commands,
            "protected_paths": request.policy.protected_paths,
            "launcher": self.command,
        }

    def _probe_version(
        self,
        task_id: str,
        total_deadline: float,
        force: bool = False,
    ) -> tuple[str, str, str]:
        with self._lock:
            if self._version_cache and not force:
                return self._version_cache, "", ""
        if self._is_cancelled(task_id):
            return "unknown", "user_cancelled", "Codex 运行已由用户取消。"
        now = time.monotonic()
        if now >= total_deadline:
            return "unknown", "call_timeout", "Codex 调用超时。"
        probe_deadline = min(total_deadline, now + 10)
        try:
            process = subprocess.Popen(
                [*self.command, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **process_group_options(),
            )
        except OSError as error:
            return "unknown", "environment_missing", f"无法探测 Codex CLI 版本：{error}"
        tree = ProcessTreeHandle(process)
        cancelled = self._activate_tree(task_id, tree)
        if cancelled:
            tree.terminate()
        timed_out = False
        stdout = ""
        stderr = ""
        try:
            try:
                stdout, stderr = process.communicate(
                    timeout=max(0.0, probe_deadline - time.monotonic())
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                tree.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate(timeout=1)
        finally:
            self._deactivate_tree(task_id, tree)
            tree.close()
        if self._is_cancelled(task_id):
            return "unknown", "user_cancelled", "Codex 运行已由用户取消。"
        if timed_out:
            if time.monotonic() >= total_deadline:
                return "unknown", "call_timeout", "Codex 调用超时。"
            return "unknown", "environment_missing", "Codex CLI 版本探测超时。"
        if process.returncode != 0:
            detail = (stderr or stdout).strip()
            return (
                "unknown",
                "environment_missing",
                detail or f"Codex CLI 版本探测退出码 {process.returncode}。",
            )
        version = (stdout or stderr).strip().removeprefix("codex-cli ") or "unknown"
        with self._lock:
            self._version_cache = version
        return version, "", ""

    def _authentication_status(self) -> tuple[bool, str, str]:
        try:
            result = subprocess.run(
                [*self.command, "login", "status"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except OSError as error:
            return False, "environment_missing", f"无法检查 Codex 登录状态：{error}"
        except subprocess.TimeoutExpired:
            return False, "environment_missing", "Codex 登录状态检查超时。"
        if result.returncode != 0:
            detail = redact((result.stderr or result.stdout).strip())
            return False, "authentication_failed", detail or "Codex CLI 尚未登录。"
        return True, "", ""

    def _activate_tree(self, task_id: str, tree: ProcessTreeHandle) -> bool:
        with self._lock:
            self._running[task_id] = tree
            return task_id in self._cancelled

    def _deactivate_tree(self, task_id: str, tree: ProcessTreeHandle) -> None:
        with self._lock:
            if self._running.get(task_id) is tree:
                self._running.pop(task_id, None)

    @staticmethod
    def _expired_budget(now: float, total_deadline: float, idle_deadline: float) -> str:
        if now >= total_deadline:
            return "call_timeout"
        if now >= idle_deadline:
            return "idle_timeout"
        return ""

    @staticmethod
    def _ensure_terminal_event(result: AgentResult, role: str) -> AgentResult:
        if result.succeeded:
            expected = AgentEventType.COMPLETED
        elif result.error_type == "user_cancelled":
            expected = AgentEventType.CANCELLED
        else:
            expected = AgentEventType.FAILED
        terminal_types = {
            AgentEventType.COMPLETED,
            AgentEventType.FAILED,
            AgentEventType.CANCELLED,
        }
        matching = [event for event in result.events if event.event_type is expected]
        result.events = [
            event for event in result.events if event.event_type not in terminal_types
        ]
        result.events.append(
            matching[-1]
            if matching
            else AgentEvent(expected, role, {"reason": result.error_type or "completed"})
        )
        return result


def _normalize_event(raw: dict, role: str) -> list[AgentEvent]:
    raw_type = str(raw.get("type", ""))
    if raw_type == "thread.started":
        return [AgentEvent(AgentEventType.SESSION_STARTED, role, dict(raw), raw_type)]
    if raw_type == "turn.started":
        return [AgentEvent(AgentEventType.HEARTBEAT, role, dict(raw), raw_type)]
    if raw_type == "item.started":
        item = raw.get("item")
        item_type = item.get("type") if isinstance(item, dict) else ""
        if item_type == "agent_message":
            event_type = AgentEventType.MESSAGE_DELTA
        elif item_type in {"command_execution", "mcp_tool_call", "file_change", "web_search"}:
            event_type = AgentEventType.TOOL_STARTED
        else:
            event_type = AgentEventType.HEARTBEAT
        return [AgentEvent(event_type, role, dict(raw), raw_type)]
    if raw_type == "item.completed":
        item = raw.get("item")
        item_type = item.get("type") if isinstance(item, dict) else ""
        if item_type == "agent_message":
            event_type = AgentEventType.MESSAGE_DELTA
        elif item_type in {"command_execution", "mcp_tool_call", "file_change", "web_search"}:
            event_type = AgentEventType.TOOL_COMPLETED
        else:
            event_type = AgentEventType.HEARTBEAT
        return [AgentEvent(event_type, role, dict(raw), raw_type)]
    if raw_type == "item.updated":
        return [AgentEvent(AgentEventType.MESSAGE_DELTA, role, dict(raw), raw_type)]
    if raw_type == "turn.completed":
        return [
            AgentEvent(AgentEventType.USAGE_UPDATED, role, dict(raw.get("usage", {})), raw_type),
            AgentEvent(AgentEventType.COMPLETED, role, dict(raw), raw_type),
        ]
    if raw_type in {"turn.failed", "error"}:
        return [AgentEvent(AgentEventType.FAILED, role, dict(raw), raw_type)]
    return [AgentEvent(AgentEventType.HEARTBEAT, role, dict(raw), raw_type)]


def _execution_result_schema() -> dict:
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "completed_steps",
            "modified_files",
            "tests",
            "deviations",
            "remaining_risks",
            "next_steps",
        ],
        "properties": {
            "completed_steps": string_array,
            "modified_files": string_array,
            "tests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["command", "exit_code", "stdout", "stderr"],
                    "properties": {
                        "command": {"type": "string"},
                        "exit_code": {"type": "integer"},
                        "stdout": {"type": "string"},
                        "stderr": {"type": "string"},
                    },
                },
            },
            "deviations": string_array,
            "remaining_risks": string_array,
            "next_steps": string_array,
        },
    }
