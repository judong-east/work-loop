from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from app.core.process_tree import ProcessTreeHandle, process_group_options


GIT_COMMAND_TIMEOUT_SECONDS = 60


def git_executable() -> str:
    """Prefer Git for Windows' native binary over the cmd.exe launcher."""
    resolved = shutil.which("git")
    if not resolved or Path(resolved).suffix.lower() != ".exe":
        return resolved or "git"
    path = Path(resolved)
    if path.parent.name.lower() == "cmd":
        native = path.parent.parent / "mingw64" / "bin" / path.name
        if native.is_file():
            return str(native)
    return resolved


def run_git(
    argv: Sequence[str],
    *,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run a non-interactive Git command with a bounded execution time."""
    command = list(argv)
    if command and command[0].lower() == "git":
        command[0] = git_executable()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
            **process_group_options(),
        )
    except OSError as error:
        raise ValueError(f"无法启动 Git：{error}") from error

    tree = ProcessTreeHandle(process)
    try:
        try:
            stdout, stderr = process.communicate(timeout=GIT_COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            tree.terminate()
            try:
                stdout, stderr = process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    stdout, stderr = process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    stdout = "" if text else b""
                    stderr = "" if text else b""
            raise ValueError(
                f"Git 命令超时（{GIT_COMMAND_TIMEOUT_SECONDS} 秒）：{' '.join(command[1:])}"
            ) from error
    finally:
        tree.close()

    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
