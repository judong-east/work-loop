import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desktop import WindowControls


FOLDER_DIALOG = object()
FAKE_WEBVIEW = SimpleNamespace(FileDialog=SimpleNamespace(FOLDER=FOLDER_DIALOG))


class FakeWindow:
    def __init__(self, selected):
        self.selected = selected
        self.calls = []

    def create_file_dialog(self, **kwargs):
        self.calls.append(kwargs)
        return self.selected


class WindowControlsTest(unittest.TestCase):
    def test_choose_workspace_returns_selected_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            selected = Path(tmp) / "selected"
            selected.mkdir()
            window = FakeWindow((str(selected),))
            controls = WindowControls()
            controls._window = window

            with patch.dict(sys.modules, {"webview": FAKE_WEBVIEW}):
                result = controls.choose_workspace(tmp)

            self.assertEqual(result, str(selected.resolve()))
            self.assertEqual(window.calls, [{
                "dialog_type": FOLDER_DIALOG,
                "directory": str(Path(tmp).resolve()),
            }])

    def test_choose_workspace_keeps_existing_value_when_cancelled(self):
        controls = WindowControls()
        controls._window = FakeWindow(None)

        with patch.dict(sys.modules, {"webview": FAKE_WEBVIEW}):
            self.assertEqual(controls.choose_workspace(), "")


if __name__ == "__main__":
    unittest.main()
