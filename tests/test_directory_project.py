from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.agents.fake_runtime import ScriptedFakeRuntime
from app.agents.workflow import AgentWorkflow
from app.projects.git_delivery import GitDelivery
from tests.git_support import create_repository


class DirectoryProjectTest(unittest.TestCase):
    def test_plain_directory_uses_external_managed_repository_and_creates_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "plain-project"
            source.mkdir()
            (source / "app.txt").write_text("source\n", encoding="utf-8")
            workflow = AgentWorkflow(root / "workloop-data", ScriptedFakeRuntime({}))

            project = workflow.register_project("Plain project", source)
            task = workflow.create_task("Edit app", "Update app.txt", project.project_id)

            self.assertEqual(project.workspace_mode, "directory")
            self.assertEqual(project.source_directory, str(source.resolve()))
            self.assertNotEqual(Path(project.repository), source.resolve())
            self.assertTrue((Path(project.repository) / ".git").exists())
            self.assertTrue((Path(project.repository) / ".workloop" / "project.toml").is_file())
            self.assertFalse((source / ".git").exists())
            self.assertFalse((source / ".workloop").exists())
            self.assertTrue(task.source_digest)
            self.assertEqual((Path(task.workspace) / "app.txt").read_text("utf-8"), "source\n")

    def test_subdirectory_of_git_repository_is_treated_as_plain_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = create_repository(root)
            source = parent / "demo"
            source.mkdir()
            (source / "note.txt").write_text("demo\n", encoding="utf-8")
            workflow = AgentWorkflow(root / "workloop-data", ScriptedFakeRuntime({}))

            project = workflow.register_project("Nested folder", source)

            self.assertEqual(project.workspace_mode, "directory")
            self.assertEqual(project.source_directory, str(source.resolve()))

    def test_directory_delivery_applies_only_managed_changes_and_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "plain-project"
            source.mkdir()
            (source / "keep.txt").write_text("before\n", encoding="utf-8")
            workflow = AgentWorkflow(root / "workloop-data", ScriptedFakeRuntime({}))
            project = workflow.register_project("Plain project", source)
            service = workflow.directory_projects
            repository = Path(project.repository)
            git = GitDelivery()
            base = git.head(repository)
            expected = service.digest(source)
            (repository / "keep.txt").write_text("after\n", encoding="utf-8")
            (repository / "new.txt").write_text("new\n", encoding="utf-8")
            delivered = git.commit_all(repository, "managed change")

            changed = service.apply_changes(project, base, delivered, expected)

            self.assertEqual((source / "keep.txt").read_text("utf-8"), "after\n")
            self.assertEqual((source / "new.txt").read_text("utf-8"), "new\n")
            self.assertEqual(changed, ["keep.txt", "new.txt"])

            delivered_digest = service.digest(source)
            (source / "external.txt").write_text("external\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "外部修改"):
                service.apply_changes(project, delivered, delivered, delivered_digest)


if __name__ == "__main__":
    unittest.main()
