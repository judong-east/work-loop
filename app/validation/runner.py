from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.agents.contracts import ExecutionPlan, ValidationResult
from app.core.contracts import utc_now
from app.projects.contracts import ProjectPolicy, ValidationCommand


_INHERITED_ENVIRONMENT = {
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
}
_SENSITIVE_PREFIX = re.compile(
    r'''(?ix)["']?(?:api[_-]?key|password|secret|token)["']?\s*[:=]\s*'''
)
_BEARER_TOKEN = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
_OPENAI_STYLE_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_SENSITIVE_KEY = re.compile(r"(?i)(?:api[_-]?key|password|secret|token)")


@dataclass
class ProcessOutcome:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    timed_out: bool = False


@dataclass
class SandboxedCommandOutcome(ProcessOutcome):
    sandbox: str = ""
    inherited_names: list[str] = field(default_factory=list)


class CommandSandbox(Protocol):
    def run(
        self,
        command: ValidationCommand,
        workspace: Path,
        timeout_seconds: int,
        network: str,
    ) -> SandboxedCommandOutcome: ...


def minimal_environment() -> dict[str, str]:
    return {
        name: value
        for name in sorted(_INHERITED_ENVIRONMENT)
        if (value := os.environ.get(name)) is not None
    }


class ProcessTreeRunner:
    def run(
        self,
        argv: list[str],
        workspace: Path,
        timeout_seconds: int,
        environment: dict[str, str],
    ) -> ProcessOutcome:
        options: dict[str, object] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        try:
            process = subprocess.Popen(
                argv,
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                **options,
            )
        except OSError as error:
            return ProcessOutcome(exit_code=None, error=str(error))
        windows_job = _WindowsJob.attach(process) if os.name == "nt" else None

        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            return ProcessOutcome(
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired:
            self._terminate_tree(process, windows_job)
            windows_job = None
            stdout, stderr = process.communicate()
            return ProcessOutcome(
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )
        finally:
            if windows_job is not None:
                windows_job.close()

    def _terminate_tree(
        self,
        process: subprocess.Popen[str],
        windows_job: "_WindowsJob | None",
    ) -> None:
        if os.name == "nt":
            if windows_job is not None:
                windows_job.close()
                return
            system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
            taskkill = Path(system_root) / "System32" / "taskkill.exe"
            try:
                result = subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    process.kill()
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class _WindowsJob:
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, handle: int):
        self.handle = handle

    @classmethod
    def attach(cls, process: subprocess.Popen[str]) -> "_WindowsJob | None":
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            class BasicLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class ExtendedLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimitInformation),
                    ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            information = ExtendedLimitInformation()
            information.BasicLimitInformation.LimitFlags = cls.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                handle,
                cls.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(information),
                ctypes.sizeof(information),
            ):
                kernel32.CloseHandle(handle)
                return None
            process_handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
            if not kernel32.AssignProcessToJobObject(handle, process_handle):
                kernel32.CloseHandle(handle)
                return None
            return cls(int(handle))
        except (AttributeError, OSError):
            return None

    def close(self) -> None:
        if not self.handle:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(wintypes.HANDLE(self.handle))
        self.handle = 0


class CodexCommandSandbox:
    """Run trusted project commands inside Codex's OS sandbox without invoking a model."""

    def __init__(self, processes: ProcessTreeRunner | None = None, executable: str = "codex"):
        self.processes = processes or ProcessTreeRunner()
        self.executable = executable

    def run(
        self,
        command: ValidationCommand,
        workspace: Path,
        timeout_seconds: int,
        network: str,
    ) -> SandboxedCommandOutcome:
        environment = minimal_environment()
        executable = shutil.which(self.executable, path=environment.get("PATH"))
        if not executable:
            return SandboxedCommandOutcome(
                exit_code=None,
                error="找不到 Codex CLI，无法在受限沙箱中运行验证。",
                sandbox="codex-cli",
                inherited_names=sorted(environment),
            )
        if network != "deny":
            return SandboxedCommandOutcome(
                exit_code=None,
                error="验证命令请求网络权限，必须先经过独立人工授权。",
                sandbox="codex-cli",
                inherited_names=sorted(environment),
            )
        canary_timeout = max(3, min(timeout_seconds, 10))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            network_script = (
                "import socket, sys\n"
                "try:\n"
                f"    connection = socket.create_connection(('127.0.0.1', {port}), timeout=1)\n"
                "except OSError:\n"
                "    sys.exit(0)\n"
                "else:\n"
                "    connection.close()\n"
                "    print('NETWORK_OPEN')\n"
                "    sys.exit(86)\n"
            )
            network_check = self.processes.run(
                self._sandbox_argv(workspace, [sys.executable, "-c", network_script]),
                workspace,
                canary_timeout,
                environment,
            )
        failure = self._canary_failure(network_check, "网络隔离")
        if failure:
            return SandboxedCommandOutcome(
                exit_code=None,
                stdout=network_check.stdout,
                stderr=network_check.stderr,
                error=failure,
                timed_out=network_check.timed_out,
                sandbox="codex-cli",
                inherited_names=sorted(environment),
            )

        canary_path = ""
        try:
            canary = workspace.parent / f".workloop-read-canary-{uuid4().hex}"
            canary.write_bytes(b"outside-workspace")
            canary_path = str(canary)
            file_script = (
                "import sys\n"
                "from pathlib import Path\n"
                "try:\n"
                f"    Path({canary_path!r}).read_bytes()\n"
                "except OSError:\n"
                "    sys.exit(0)\n"
                "else:\n"
                "    print('OUTSIDE_READ_OPEN')\n"
                "    sys.exit(86)\n"
            )
            file_check = self.processes.run(
                self._sandbox_argv(workspace, [sys.executable, "-c", file_script]),
                workspace,
                canary_timeout,
                environment,
            )
        finally:
            if canary_path:
                Path(canary_path).unlink(missing_ok=True)
        failure = self._canary_failure(file_check, "工作区外读取隔离")
        if failure:
            return SandboxedCommandOutcome(
                exit_code=None,
                stdout=file_check.stdout,
                stderr=file_check.stderr,
                error=failure,
                timed_out=file_check.timed_out,
                sandbox="codex-cli",
                inherited_names=sorted(environment),
            )

        outcome = self.processes.run(
            self._sandbox_argv(workspace, command.argv),
            workspace,
            timeout_seconds,
            environment,
        )
        return SandboxedCommandOutcome(
            **outcome.__dict__,
            sandbox="codex-cli",
            inherited_names=sorted(environment),
        )

    def _sandbox_argv(self, workspace: Path, command: list[str]) -> list[str]:
        executable = shutil.which(self.executable, path=minimal_environment().get("PATH"))
        if not executable:
            return []
        return [
            executable,
            "sandbox",
            "--include-managed-config",
            "-c",
            'permissions.workloop-validation.default_permissions="workspace-write"',
            "-P",
            "workloop-validation",
            "--sandbox-state-disable-network",
            "-C",
            str(workspace),
            "--",
            *command,
        ]

    def _canary_failure(self, outcome: ProcessOutcome, boundary: str) -> str:
        if outcome.exit_code == 86:
            return f"{boundary}健康检查失败：Codex 沙箱未落实项目策略。"
        if outcome.timed_out:
            return f"{boundary}健康检查超时，拒绝运行验证。"
        if outcome.error or outcome.exit_code != 0:
            detail = outcome.error or outcome.stderr.strip() or f"退出码 {outcome.exit_code}"
            return f"无法验证{boundary}：{detail}"
        return ""


class UnsafeDirectCommandSandbox:
    """Direct runner for controlled tests; production code must use CodexCommandSandbox."""

    def __init__(self, processes: ProcessTreeRunner | None = None):
        self.processes = processes or ProcessTreeRunner()

    def run(
        self,
        command: ValidationCommand,
        workspace: Path,
        timeout_seconds: int,
        network: str,
    ) -> SandboxedCommandOutcome:
        environment = minimal_environment()
        outcome = self.processes.run(command.argv, workspace, timeout_seconds, environment)
        return SandboxedCommandOutcome(
            **outcome.__dict__,
            sandbox="unsafe-test-direct",
            inherited_names=sorted(environment),
        )


class DeterministicValidator:
    def __init__(self, sandbox: CommandSandbox | None = None):
        self.sandbox = sandbox or CodexCommandSandbox()

    def validate(
        self,
        task_id: str,
        workspace: Path,
        plan: ExecutionPlan,
        policy: ProjectPolicy,
    ) -> ValidationResult:
        del task_id
        checks = [
            self._run(workspace, command, policy)
            for command in policy.required_commands(plan.required_tests)
        ]
        failures = [check for check in checks if check["exit_code"] != 0 or check["error"]]
        error = "；".join(
            str(check["error"] or f"验证 {check['name']} 退出码 {check['exit_code']}")
            for check in failures
        )
        return ValidationResult(passed=not failures, checks=checks, error=error)

    def _run(
        self,
        workspace: Path,
        command: ValidationCommand,
        policy: ProjectPolicy,
    ) -> dict[str, object]:
        started_at = utc_now()
        started = time.monotonic()
        outcome = self.sandbox.run(
            command,
            workspace,
            policy.timeout_seconds,
            policy.network,
        )
        if outcome.timed_out:
            error = f"验证 {command.name} 超时（{policy.timeout_seconds} 秒）"
        elif outcome.error:
            error = f"无法运行验证 {command.name}：{outcome.error}"
        elif outcome.exit_code != 0:
            error = f"验证 {command.name} 退出码 {outcome.exit_code}"
        else:
            error = ""
        return {
            "schema_version": 1,
            "name": command.name,
            "command": list(command.argv),
            "working_directory": str(workspace),
            "started_at": started_at,
            "finished_at": utc_now(),
            "duration_seconds": time.monotonic() - started,
            "exit_code": outcome.exit_code,
            "stdout": redact(outcome.stdout, policy.redact_patterns),
            "stderr": redact(outcome.stderr, policy.redact_patterns),
            "error": redact(error, policy.redact_patterns),
            "timed_out": outcome.timed_out,
            "environment": {
                "platform": platform.system(),
                "network": policy.network,
                "sandbox": outcome.sandbox,
                "inherited_names": outcome.inherited_names,
            },
        }


def redact(text: str, project_patterns: list[str] | None = None) -> str:
    redacted = _redact_json_lines(text)
    redacted = _redact_sensitive_assignments(redacted)
    redacted = _BEARER_TOKEN.sub(r"\1[REDACTED]", redacted)
    redacted = _OPENAI_STYLE_TOKEN.sub("[REDACTED]", redacted)
    for pattern in project_patterns or []:
        redacted = _redact_project_pattern(redacted, pattern)
    return redacted


def _redact_sensitive_assignments(text: str) -> str:
    output: list[str] = []
    position = 0
    while match := _SENSITIVE_PREFIX.search(text, position):
        output.append(text[position : match.end()])
        value_start = match.end()
        if value_start >= len(text):
            output.append("[REDACTED]")
            position = value_start
            break

        quote = text[value_start] if text[value_start] in {'"', "'"} else ""
        if quote:
            index = value_start + 1
            escaped = False
            while index < len(text):
                character = text[index]
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    break
                index += 1
            output.append(quote + "[REDACTED]")
            if index < len(text) and text[index] == quote:
                output.append(quote)
                index += 1
            position = index
            continue

        index = value_start
        while index < len(text) and text[index] not in " \t\r\n,;}":
            index += 1
        output.append("[REDACTED]")
        position = index
    output.append(text[position:])
    return "".join(output)


def _redact_json_lines(text: str) -> str:
    output: list[str] = []
    for segment in text.splitlines(keepends=True):
        body = segment.rstrip("\r\n")
        ending = segment[len(body) :]
        stripped = body.strip()
        try:
            value = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            output.append(segment)
            continue
        if not isinstance(value, (dict, list)):
            output.append(segment)
            continue
        prefix = body[: len(body) - len(body.lstrip())]
        output.append(
            prefix
            + json.dumps(_redact_json_value(value), ensure_ascii=False, separators=(",", ":"))
            + ending
        )
    return "".join(output)


def _redact_json_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _redact_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    return value


def _redact_project_pattern(text: str, pattern: str) -> str:
    if not pattern.endswith("*"):
        return text.replace(pattern, "[REDACTED]")
    prefix = pattern[:-1]
    if not prefix:
        return "\n".join("[REDACTED]" for _ in text.split("\n"))
    lines: list[str] = []
    for line in text.split("\n"):
        index = line.find(prefix)
        lines.append(line if index < 0 else line[:index] + "[REDACTED]")
    return "\n".join(lines)
