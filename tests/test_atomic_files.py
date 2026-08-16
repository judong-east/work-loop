from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.atomic_files import write_json_atomic, write_text_atomic


class AtomicFilesTest(unittest.TestCase):
    def test_json_write_flushes_before_publishing_valid_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "state.json"
            with mock.patch("app.core.atomic_files.os.fsync") as fsync:
                write_json_atomic(path, {"message": "完成", "count": 2})

            self.assertEqual(
                json.loads(path.read_text("utf-8")),
                {"message": "完成", "count": 2},
            )
            fsync.assert_called_once()
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_text_write_flushes_and_replaces_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.txt"
            path.write_text("old", encoding="utf-8")
            with mock.patch("app.core.atomic_files.os.fsync") as fsync:
                write_text_atomic(path, "new\n")

            self.assertEqual(path.read_text("utf-8"), "new\n")
            fsync.assert_called_once()


if __name__ == "__main__":
    unittest.main()
