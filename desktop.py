"""Desktop entrypoint: run the V2 workbench inside a native frameless window.

The V2 HTTP server keeps serving the same API on an ephemeral local port;
pywebview hosts the workbench UI in the OS webview (WebView2 on Windows) with
a custom title bar so the packaged exe behaves like a first-class desktop
tool instead of a browser tab.
"""
from __future__ import annotations

import threading
from pathlib import Path


def _data_root() -> Path:
    data_dir = Path.home() / ".workloop"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


class WindowControls:
    """Exposed to the UI as ``window.pywebview.api`` for desktop integrations."""

    def __init__(self):
        self._window = None
        self._maximized = False

    def attach(self, window) -> None:
        self._window = window
        # Track the real window state via pywebview events so the toggle stays
        # correct even when the OS changes it (Win+Up, taskbar, ...).
        window.events.maximized += lambda: setattr(self, "_maximized", True)
        window.events.restored += lambda: setattr(self, "_maximized", False)

    def minimize(self) -> None:
        self._window.minimize()

    def toggle_maximize(self) -> None:
        if self._maximized:
            self._window.restore()
        else:
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
