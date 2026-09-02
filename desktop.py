"""Desktop entrypoint: run the V2 workbench inside a native frameless window.

The V2 HTTP server keeps serving the same API on an ephemeral local port;
pywebview hosts the workbench UI in the OS webview (WebView2 on Windows) with
a custom title bar so the packaged exe behaves like a first-class desktop
tool instead of a browser tab.
"""
from __future__ import annotations

import threading
import ctypes
import sys
from pathlib import Path


_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_MONITOR_DEFAULTTONEAREST = 0x00000002


class _Win32Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _Win32MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _Win32Rect),
        ("rcWork", _Win32Rect),
        ("dwFlags", ctypes.c_ulong),
    ]


def _data_root() -> Path:
    data_dir = Path.home() / ".workloop"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


class WindowControls:
    """Exposed to the UI as ``window.pywebview.api`` for desktop integrations."""

    def __init__(self):
        self._window = None
        self._maximized = False
        self._restore_bounds = None

    def attach(self, window) -> None:
        self._window = window
        # Track the real window state via pywebview events so the toggle stays
        # correct even when the OS changes it (Win+Up, taskbar, ...).
        window.events.maximized += self._on_native_maximized
        window.events.restored += self._on_native_restored

    def _native_handle(self):
        """Return pywebview's WinForms HWND when the desktop backend exposes it."""
        if sys.platform != "win32" or self._window is None:
            return None
        native = getattr(self._window, "native", None)
        handle = getattr(native, "Handle", None)
        to_int32 = getattr(handle, "ToInt32", None)
        if not callable(to_int32):
            return None
        try:
            return int(to_int32())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _window_rect(hwnd):
        if not hwnd:
            return None
        rect = _Win32Rect()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    @staticmethod
    def _work_area(hwnd):
        if not hwnd:
            return None
        user32 = ctypes.windll.user32
        monitor = user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = _Win32MonitorInfo()
        info.cbSize = ctypes.sizeof(_Win32MonitorInfo)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        rect = info.rcWork
        return rect.left, rect.top, rect.right, rect.bottom

    @staticmethod
    def _set_window_rect(hwnd, bounds):
        if not hwnd or not bounds:
            return False
        left, top, right, bottom = bounds
        return bool(ctypes.windll.user32.SetWindowPos(
            hwnd,
            0,
            int(left),
            int(top),
            max(1, int(right - left)),
            max(1, int(bottom - top)),
            _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
        ))

    def _set_work_area_maximized(self):
        """Fit a frameless window to the monitor work area, below the taskbar."""
        hwnd = self._native_handle()
        return self._set_window_rect(hwnd, self._work_area(hwnd))

    def _on_native_maximized(self) -> None:
        self._maximized = True
        # WinForms maximizes a borderless form to the full monitor. Re-apply
        # the monitor work area so the Windows taskbar remains visible.
        self._set_work_area_maximized()

    def _on_native_restored(self) -> None:
        self._maximized = False

    def minimize(self) -> None:
        self._window.minimize()

    def toggle_maximize(self) -> None:
        if self._maximized:
            if self._restore_bounds and self._set_window_rect(
                self._native_handle(), self._restore_bounds
            ):
                self._maximized = False
                return
            self._window.restore()
        else:
            hwnd = self._native_handle()
            current_bounds = self._window_rect(hwnd)
            if current_bounds and self._set_work_area_maximized():
                self._restore_bounds = current_bounds
                self._maximized = True
                return
            self._window.maximize()

    def close(self) -> None:
        self._window.destroy()

    def choose_workspace(self, directory: str = "") -> str:
        """Open the native folder picker and return the selected absolute path."""
        if self._window is None:
            return ""

        import webview

        initial_directory = ""
        if directory:
            candidate = Path(directory).expanduser()
            if candidate.is_dir():
                initial_directory = str(candidate.resolve())
            elif candidate.parent.is_dir():
                initial_directory = str(candidate.parent.resolve())

        selected = self._window.create_file_dialog(
            dialog_type=webview.FileDialog.FOLDER,
            directory=initial_directory,
        )
        if not selected:
            return ""
        return str(Path(selected[0]).resolve())


def main() -> None:
    from app.web.server import make_server

    # Port 0 binds an ephemeral free port so concurrent copies never clash.
    server = make_server(_data_root(), 0, open_browser=False)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        import webview
    except ImportError:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")
        print(f"Workloop 已启动（未安装 pywebview，回退为浏览器模式）：http://127.0.0.1:{port}")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return

    controls = WindowControls()
    window = webview.create_window(
        "Workloop 工作台",
        f"http://127.0.0.1:{port}/?desktop=1",
        js_api=controls,
        width=1440,
        height=900,
        min_size=(1100, 700),
        frameless=True,
        easy_drag=False,
        background_color="#eef2f5",
    )
    controls.attach(window)
    webview.start()


if __name__ == "__main__":
    main()
