from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from app.agents.claude_protocol import ClaudeProtocolState, schema_for_role
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


_READ_ONLY_TOOLS = ("Read", "Glob", "Grep")
_SUPPORTED_ROLES = {"planner", "reviewer"}
_AUTH_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
}


@dataclass(frozen=True)
class ClaudeCodeProfile:
    command: list[str] = field(default_factory=lambda: ["claude"])
    model: str = ""


def _is_workloop_controlled_argument(argument: str) -> bool:
    return argument.startswith("-")


class ClaudeCodeRuntime(AgentRuntime):
    def __init__(self, profile: ClaudeCodeProfile):
        if not profile.command:
            raise ValueError("Claude Code command 不能为空。")
        if not profile.model.strip():
            raise ValueError("Claude model 不能为空。")
        controlled = [
            argument
            for argument in profile.command[1:]
            if _is_workloop_controlled_argument(argument)
        ]
        if controlled:
            raise ValueError(
                "Claude Code launcher 不能覆盖 Workloop 权限参数："
                + ", ".join(controlled)
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
                    error=f"任务 {request.task_id} 已有 Claude Code 运行。",
                    error_type="policy_blocked",
                    runtime="claude-code",
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
                error=redact(version_error, request.policy.redact_patterns),
                error_type=version_error_type,
                **identity,
            )
        if self._is_cancelled(request.task_id):
            return self._cancelled_result(request, identity)
        if time.monotonic() >= total_deadline:
            return AgentResult(
                succeeded=False,
                error="Claude Code 调用超时。",
                error_type="call_timeout",
                **identity,
            )

        command = self._command(request)
        environment = dict(os.environ)
        environment.update(self._authentication_environment())
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
                error=f"无法启动 Claude Code：{error}",
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

    @staticmethod
    def _review_acceptance_criteria(request: AgentRequest) -> list[str]:
        if request.role != "reviewer":
            return []
        start = request.instructions.find("{")
        if start < 0:
            return []
        try:
            payload = json.loads(request.instructions[start:])
        except json.JSONDecodeError:
            return []
        plan = payload.get("plan") if isinstance(payload, dict) else None
        criteria = plan.get("acceptance_criteria") if isinstance(plan, dict) else None
        if not isinstance(criteria, list) or not all(
            isinstance(item, str) and item for item in criteria
        ):
            return []
        return list(criteria)

    def _collect(
        self,
        request: AgentRequest,
        process: subprocess.Popen[str],
        tree: ProcessTreeHandle,
        identity: dict,
        total_deadline: float,
    ) -> AgentResult:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            tree.terminate()
            return AgentResult(
                succeeded=False,
                error="Claude Code 管道初始化失败。",
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
        state = ClaudeProtocolState(
            request.role,
            acceptance_criteria=self._review_acceptance_criteria(request),
        )
        stderr: list[str] = []
        last_activity = time.monotonic()
        open_streams = 2
        failure_type = ""

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
                state.protocol_errors.append(
                    f"无法解析 Claude stream-json：{error}: {excerpt}"
                )
                continue
            if not isinstance(raw, dict):
                state.protocol_errors.append("Claude stream-json 事件必须是对象。")
                continue
            last_activity = time.monotonic()
            state.consume(redact_value(raw, request.policy.redact_patterns))

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
            return AgentResult(
                succeeded=False,
                session_id=state.session_id or request.session_id,
                error="Claude Code 运行已由用户取消。",
                error_type="user_cancelled",
                final_message=state.final_message,
                events=state.events,
                raw_events=state.raw_events,
                usage=state.usage,
                **identity,
            )
        if failure_type:
            return AgentResult(
                succeeded=False,
                session_id=state.session_id or request.session_id,
                error=(
                    "Claude Code 调用超时。"
                    if failure_type == "call_timeout"
                    else "Claude Code 长时间没有事件。"
                ),
                error_type=failure_type,
                final_message=state.final_message,
                events=state.events,
                raw_events=state.raw_events,
                usage=state.usage,
                **identity,
            )
        if input_errors:
            return AgentResult(
                succeeded=False,
                session_id=state.session_id or request.session_id,
                error=f"无法向 Claude Code stdin 写入指令：{input_errors[0]}",
                error_type="runtime_failed",
                events=state.events,
                raw_events=state.raw_events,
                usage=state.usage,
                **identity,
            )

        result = state.finish(
            return_code=return_code,
            stderr=redact("\n".join(stderr), request.policy.redact_patterns),
        )
        result.runtime = identity["runtime"]
        result.runtime_version = identity["runtime_version"]
        result.model = identity["model"]
        result.runtime_config = identity["runtime_config"]
        if request.session_id and result.session_id != request.session_id:
            result.succeeded = False
            result.output = {}
            result.error = (
                f"Claude 恢复后的 session 不一致：{request.session_id} != "
                f"{result.session_id or '<missing>'}。"
            )
            result.error_type = "structured_output_failed"
        maximum = request.budget.max_cost_usd
        actual = result.usage.get("total_cost_usd")
        if (
            result.succeeded
            and maximum is not None
            and isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and actual > maximum
        ):
            result.succeeded = False
            result.output = {}
            result.error = f"Claude Code 费用 {actual} USD 超过预算 {maximum} USD。"
            result.error_type = "budget_exhausted"
        return result

    def _command(self, request: AgentRequest) -> list[str]:
        settings = {
            "disableAllHooks": True,
            "enableAllProjectMcpServers": False,
            "enabledPlugins": {},
        }
        command = [*self.command]
        if request.session_id:
            command.extend(["--resume", request.session_id])
        command.extend(
            [
                "--print",
                "--input-format",
                "text",
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                self.profile.model,
                "--permission-mode",
                "dontAsk",
                "--tools",
                ",".join(_READ_ONLY_TOOLS),
                "--strict-mcp-config",
                "--mcp-config",
                json.dumps({"mcpServers": {}}, separators=(",", ":")),
                "--disable-slash-commands",
                "--no-chrome",
            ]
        )
        command.extend(
            [
                "--setting-sources",
                "",
                "--settings",
                json.dumps(settings, separators=(",", ":")),
            ]
        )
        command.extend(
            [
                "--json-schema",
                json.dumps(schema_for_role(request.role), separators=(",", ":")),
            ]
        )
        if request.budget.max_cost_usd is not None:
            command.extend(["--max-budget-usd", str(request.budget.max_cost_usd)])
        return command

    def _validate_request(
        self,
        request: AgentRequest,
        identity: dict,
    ) -> AgentResult | None:
        if request.role not in _SUPPORTED_ROLES or request.access is not AgentAccess.READ_ONLY:
            return AgentResult(
                succeeded=False,
                error="ClaudeCodeRuntime 只接受只读 planner 或 reviewer 请求。",
                error_type="policy_blocked",
                **identity,
            )
        if request.policy.network_allowed:
            return AgentResult(
                succeeded=False,
                error="Claude Code 网络权限尚未获得独立人工授权。",
                error_type="permission_required",
                **identity,
            )
        if request.budget.total_timeout_seconds <= 0 or request.budget.idle_timeout_seconds <= 0:
            return AgentResult(
                succeeded=False,
                error="Claude Code 运行预算必须是正数。",
                error_type="budget_exhausted",
                **identity,
            )
        if request.budget.max_cost_usd is not None and request.budget.max_cost_usd <= 0:
            return AgentResult(
                succeeded=False,
                error="Claude Code 费用预算必须是正数。",
                error_type="budget_exhausted",
                **identity,
            )
        return None

    def describe(self, request: AgentRequest) -> dict:
        return {
            "runtime": "claude-code",
            "runtime_version": self._version_cache or "unknown",
            "model": self.profile.model,
            "config": self._runtime_config(request),
        }

    def health_check(self) -> dict:
        version, error_type, error = self._probe_version(
            "__claude_health_check__",
            time.monotonic() + 10,
            force=True,
        )
        authenticated = False
        auth_method = ""
        api_provider = ""
        if not error_type:
            authenticated, auth_method, api_provider, error_type, error = (
                self._authentication_status()
            )
        return {
            "available": not error_type and authenticated,
            "authenticated": authenticated,
            "auth_method": auth_method,
            "api_provider": api_provider,
            "runtime": "claude-code",
            "runtime_version": version,
            "model": self.profile.model,
            "error_type": error_type,
            "error": error,
        }

    def _runtime_config(self, request: AgentRequest) -> dict:
        return {
            "role": request.role,
            "permission_mode": "dontAsk",
            "tools": list(_READ_ONLY_TOOLS),
            "setting_sources": [],
            "authentication_env_keys": sorted(self._authentication_environment()),
            "strict_mcp_config": True,
            "mcp_servers": [],
            "hooks_enabled": False,
            "plugins_enabled": False,
            "network_allowed": request.policy.network_allowed,
            "max_cost_usd": request.budget.max_cost_usd,
            "launcher": self.command,
        }

    def _identity(self) -> dict[str, str]:
        with self._lock:
            version = self._version_cache or "unknown"
        return {
            "runtime": "claude-code",
            "runtime_version": version,
            "model": self.profile.model,
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
            return "unknown", "user_cancelled", "Claude Code 运行已由用户取消。"
        now = time.monotonic()
        if now >= total_deadline:
            return "unknown", "call_timeout", "Claude Code 调用超时。"
        probe_limit = now + 10
        budget_limited = total_deadline <= probe_limit
        probe_deadline = min(total_deadline, probe_limit)
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
            return "unknown", "environment_missing", f"无法探测 Claude Code 版本：{error}"
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
            return "unknown", "user_cancelled", "Claude Code 运行已由用户取消。"
        if timed_out:
            if budget_limited:
                return "unknown", "call_timeout", "Claude Code 调用超时。"
            return "unknown", "environment_missing", "Claude Code 版本探测超时。"
        if process.returncode != 0:
            detail = (stderr or stdout).strip()
            return (
                "unknown",
                "environment_missing",
                detail or f"Claude Code 版本探测退出码 {process.returncode}。",
            )
        full_version = (stdout or stderr).strip()
        version = full_version.removesuffix(" (Claude Code)").strip() or "unknown"
        with self._lock:
            self._version_cache = version
        return version, "", ""

    def _authentication_status(self) -> tuple[bool, str, str, str, str]:
        environment = dict(os.environ)
        environment.update(self._authentication_environment())
        try:
            result = subprocess.run(
                [*self.command, "auth", "status"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                env=environment,
            )
        except OSError as error:
            return False, "", "", "environment_missing", f"无法检查 Claude 登录状态：{error}"
        except subprocess.TimeoutExpired:
            return False, "", "", "environment_missing", "Claude 登录状态检查超时。"
        detail = (result.stdout or result.stderr).strip()
        if result.returncode != 0:
            return (
                False,
                "",
                "",
                "authentication_failed",
                redact(detail) or "Claude Code 尚未登录。",
            )
        try:
            status = json.loads(detail)
        except json.JSONDecodeError as error:
            return (
                False,
                "",
                "",
                "authentication_failed",
                f"Claude auth status 输出无效：{error}",
            )
        if not isinstance(status, dict) or status.get("loggedIn") is not True:
            return (
                False,
                str(status.get("authMethod", "")) if isinstance(status, dict) else "",
                str(status.get("apiProvider", "")) if isinstance(status, dict) else "",
                "authentication_failed",
                "Claude Code 尚未登录。",
            )
        return (
            True,
            str(status.get("authMethod", "")),
            str(status.get("apiProvider", "")),
            "",
            "",
        )

    @staticmethod
    def _authentication_environment() -> dict[str, str]:
        selected = {
            key: value
            for key in _AUTH_ENV_KEYS
            if (value := os.environ.get(key))
        }
        settings_path = Path.home() / ".claude" / "settings.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return selected
        raw_environment = settings.get("env", {}) if isinstance(settings, dict) else {}
        if not isinstance(raw_environment, dict):
            return selected
        for key in _AUTH_ENV_KEYS:
            value = raw_environment.get(key)
            if key not in selected and isinstance(value, str) and value:
                selected[key] = value
        return selected

    def _is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._cancelled

    def _cancelled_result(self, request: AgentRequest, identity: dict) -> AgentResult:
        return AgentResult(
            succeeded=False,
            session_id=request.session_id,
            error="Claude Code 运行已由用户取消。",
            error_type="user_cancelled",
            events=[AgentEvent(AgentEventType.CANCELLED, request.role)],
            **identity,
        )

    def _activate_tree(self, task_id: str, tree: ProcessTreeHandle) -> bool:
        with self._lock:
            self._running[task_id] = tree
            return task_id in self._cancelled

    def _deactivate_tree(self, task_id: str, tree: ProcessTreeHandle) -> None:
        with self._lock:
            if self._running.get(task_id) is tree:
                self._running.pop(task_id, None)

    @staticmethod
    def _read_stream(
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

    @staticmethod
    def _write_stdin(
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
