from __future__ import annotations

import os
import platform
import shutil
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
from app.core.process_tree import ProcessTreeHandle, process_group_options
from app.core.redaction import redact
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
                **process_group_options(),
            )
        except OSError as error:
            return ProcessOutcome(exit_code=None, error=str(error))
        tree = ProcessTreeHandle(process)

        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            return ProcessOutcome(
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired:
            tree.terminate()
            stdout, stderr = process.communicate()
            return ProcessOutcome(
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )
        finally:
            tree.close()


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
