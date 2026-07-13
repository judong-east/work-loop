from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.memory.experience_store import ExperienceStore


class ExperienceStoreTest(unittest.TestCase):
    def test_suggestions_are_pending_and_deduplicated_across_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(Path(tmp))

            record = store.suggest("  Keep   changes scoped  ", "review_pattern", "TASK-1")
            store.reject(record.experience_id)
            duplicate = store.suggest("keep changes scoped", "review_pattern", "TASK-2")

            self.assertEqual(record.text, "Keep changes scoped")
            self.assertIsNone(duplicate)
            self.assertEqual(store.list_all()[0].status, "rejected")

    def test_status_updates_append_history_and_latest_record_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ExperienceStore(root)
            record = store.suggest("Review permissions", "clarification", "TASK-1")

            approved = store.approve(record.experience_id)

            lines = (root / "experience.jsonl").read_text("utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["status"], "pending")
            self.assertEqual(json.loads(lines[1])["status"], "approved")
            self.assertEqual(store.approved(), [approved])

    def test_manual_experience_is_approved_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(Path(tmp))

            record = store.add_manual("Prefer deterministic validation")

            self.assertEqual(record.kind, "manual")
            self.assertEqual(record.status, "approved")
            with self.assertRaisesRegex(ValueError, "已存在"):
                store.add_manual("prefer deterministic validation")


if __name__ == "__main__":
    unittest.main()
