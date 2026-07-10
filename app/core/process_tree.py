from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path


def process_group_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class ProcessTreeHandle:
    def __init__(self, process: subprocess.Popen[str]):
        self.process = process
        self.windows_job = _WindowsJob.attach(process) if os.name == "nt" else None
        self._lock = threading.Lock()

    def terminate(self) -> None:
        if os.name == "nt":
            with self._lock:
                windows_job = self.windows_job
                self.windows_job = None
            if windows_job is not None:
                windows_job.close()
                return
            system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
            taskkill = Path(system_root) / "System32" / "taskkill.exe"
            try:
                result = subprocess.run(
                    [str(taskkill), "/PID", str(self.process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    self.process.kill()
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.process.kill()
                except OSError:
                    pass
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        with self._lock:
            windows_job = self.windows_job
            self.windows_job = None
        if windows_job is not None:
            windows_job.close()


class _WindowsJob:
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, handle: int):
        self.handle = handle
        self._lock = threading.Lock()

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
        with self._lock:
            handle = self.handle
            self.handle = 0
        if not handle:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(wintypes.HANDLE(handle))
