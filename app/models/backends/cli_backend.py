from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app.core.contracts import ModelProfile, ModelRequest, ModelResponse
from app.models.backends.base import ModelBackend


_PROCESS_LOCK = threading.Lock()
_TASK_PROCESSES: dict[str, list[subprocess.Popen]] = {}
_CANCELLED_TASKS: set[str] = set()


def clear_task_cancel(task_id: str) -> None:
    with _PROCESS_LOCK:
        _CANCELLED_TASKS.discard(task_id)


def cancel_task_processes(task_id: str) -> int:
    with _PROCESS_LOCK:
        _CANCELLED_TASKS.add(task_id)
        processes = list(_TASK_PROCESSES.get(task_id, []))

    killed = 0
    for process in processes:
        if process.poll() is not None:
            continue
        process.kill()
        killed += 1
    return killed


def _task_cancelled(task_id: str) -> bool:
    with _PROCESS_LOCK:
        return task_id in _CANCELLED_TASKS


def _register_process(task_id: str, process: subprocess.Popen) -> None:
    with _PROCESS_LOCK:
        _TASK_PROCESSES.setdefault(task_id, []).append(process)


def _unregister_process(task_id: str, process: subprocess.Popen) -> None:
    with _PROCESS_LOCK:
        processes = _TASK_PROCESSES.get(task_id)
        if not processes:
            return
        if process in processes:
            processes.remove(process)
        if not processes:
            _TASK_PROCESSES.pop(task_id, None)


class CliBackend(ModelBackend):
    """通过本机 CLI 子进程调用模型；shell=False，模板仅替换 {prompt} 与 {model}。"""

    def invoke(self, profile: ModelProfile, request: ModelRequest) -> ModelResponse:
        command = [
            part.replace("{prompt}", request.prompt).replace("{model}", profile.model)
            for part in profile.command
        ]
        command[0] = _resolve_command(command[0])
        start = time.monotonic()
        if _task_cancelled(request.task_id):
            return self._failure(profile, start, "调用已中断。")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                shell=False,
            )
        except OSError as error:
            return self._failure(
                profile, start,
                f"无法启动命令 {command[0]}：{error}。请确认该 CLI 已安装并在 PATH 中。",
            )

        _register_process(request.task_id, process)
        try:
            stdout, stderr = process.communicate(timeout=profile.timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return self._failure(profile, start, f"调用超时（{profile.timeout_seconds}s）。")
        finally:
            _unregister_process(request.task_id, process)

        if _task_cancelled(request.task_id):
            return self._failure(profile, start, "调用已中断。")

        if process.returncode != 0:
            stderr = (stderr or "").strip()
            return self._failure(profile, start, f"退出码 {process.returncode}：{stderr}")

        text = (stdout or "").strip()
        if not text:
            return self._failure(profile, start, "模型返回空输出。")

        return ModelResponse(
            text=text, profile_name=profile.name, model=profile.model,
            duration_seconds=round(time.monotonic() - start, 3), succeeded=True,
        )

    def _failure(self, profile: ModelProfile, start: float, error: str) -> ModelResponse:
        return ModelResponse(
            text="", profile_name=profile.name, model=profile.model,
            duration_seconds=round(time.monotonic() - start, 3), succeeded=False, error=error,
        )


def _resolve_command(command_name: str) -> str:
    # Windows 上 npm 全局命令常先命中 .cmd shim。部分 shim 会派生真实 .exe 后退出，
    # 子进程继承 stdout/stderr 管道时会让 communicate() 在超时后继续等 EOF。
    # 能解析出真实 .exe 时直接运行它，让 Python 能可靠控制超时和清理。
    resolved = shutil.which(command_name) or command_name
    path = Path(resolved)
    if path.suffix.lower() not in (".cmd", ".bat"):
        return resolved
    return _target_exe_from_cmd(path) or resolved


def _target_exe_from_cmd(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    base = str(path.parent)
    for line in text.splitlines():
        expanded = line.replace("%dp0%", base).replace("%~dp0", base + "\\")
        match = re.search(r'"([^"\r\n]+\.exe)"', expanded, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = Path(match.group(1))
        if candidate.is_file():
            return str(candidate)
    return None
