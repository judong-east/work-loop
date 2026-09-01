"""Direct tests for the workspace write boundary.

``WorkspaceRuntime._safe_target`` is the only gate between model-proposed file
writes and the filesystem.  These tests exercise it directly instead of through
the full service, so a refactor that weakens one specific defence fails here
with a precise message.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.domain.models import ContextState, Session, SessionMode, WorkflowNode
from app.infrastructure.workspace_runtime import WorkspaceRuntime


class WorkspaceEscapeTest(unittest.TestCase):
    def setUp(self):
        self.runtime = WorkspaceRuntime()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.outside = Path(self.tmp.name) / "outside"
        self.outside.mkdir()

    def _apply(self, path: str, content: str = "payload") -> dict:
        """Run one proposed write through the real implementation node path."""

        session = Session.create("project", "writes", SessionMode.TASK)
        node = WorkflowNode("build", "implementation")
        context = ContextState(inputs={"project": {
            "project_id": "p", "name": "P", "workspace_path": str(self.root),
        }})
        return self.runtime.process_output(session, node, context, {
            "changes": "test",
            "file_changes": [{"operation": "write", "path": path, "content": content}],
            "artifacts": {},
            "decisions": [],
        })

    def _rejects(self, path: str, expected: str, content: str = "payload") -> None:
        with self.assertRaises(ValueError) as caught:
            self._apply(path, content)
        self.assertIn(expected, str(caught.exception))

    def test_relative_write_inside_the_workspace_is_published(self):
        result = self._apply("src/module.py", "content")
        self.assertEqual((self.root / "src" / "module.py").read_text(encoding="utf-8"), "content")
        self.assertTrue(result["applied_files"][0]["created"])
        # Complete file text must never survive into durable context.
        self.assertNotIn("file_changes", result)

    def test_absolute_path_is_rejected(self):
        self._rejects(str(self.outside / "owned.txt"), "越出工作区")
        self.assertFalse((self.outside / "owned.txt").exists())

    def test_parent_traversal_is_rejected(self):
        self._rejects("../owned.txt", "越出工作区")
        self.assertFalse((self.root.parent / "owned.txt").exists())

    def test_nested_traversal_that_lands_outside_is_rejected(self):
        self._rejects("src/../../owned.txt", "越出工作区")
        self.assertFalse((self.root.parent / "owned.txt").exists())

    def test_repository_metadata_is_protected(self):
        for path in (".git/config", "nested/.git/hooks/pre-commit", ".workloop/state.json"):
            with self.subTest(path=path):
                self._rejects(path, "受保护")

    def test_workbench_data_directory_is_protected(self):
        self._rejects("workbench/projects/p.json", "受保护")

    def test_workbench_is_only_protected_at_the_workspace_root(self):
        # The guard is anchored: a nested directory of the same name is ordinary.
        self._apply("app/workbench/panel.py", "ok")
        self.assertTrue((self.root / "app" / "workbench" / "panel.py").is_file())

    def test_empty_path_is_rejected(self):
        self._rejects("", "invalid or duplicate file path")

    def test_duplicate_paths_in_one_batch_are_rejected(self):
        session = Session.create("project", "writes", SessionMode.TASK)
        context = ContextState(inputs={"project": {
            "project_id": "p", "name": "P", "workspace_path": str(self.root),
        }})
        with self.assertRaises(ValueError) as caught:
            self.runtime.process_output(session, WorkflowNode("build", "implementation"), context, {
                "changes": "dup",
                "file_changes": [
                    {"operation": "write", "path": "a.txt", "content": "one"},
                    {"operation": "write", "path": "a.txt", "content": "two"},
                ],
                "artifacts": {}, "decisions": [],
            })
        self.assertIn("duplicate", str(caught.exception))
        self.assertFalse((self.root / "a.txt").exists())

    def test_deletion_is_not_a_supported_operation(self):
        target = self.root / "keep.txt"
        target.write_text("keep", encoding="utf-8")
        session = Session.create("project", "writes", SessionMode.TASK)
        context = ContextState(inputs={"project": {
            "project_id": "p", "name": "P", "workspace_path": str(self.root),
        }})
        with self.assertRaises(ValueError) as caught:
            self.runtime.process_output(session, WorkflowNode("build", "implementation"), context, {
                "changes": "delete",
                "file_changes": [{"operation": "delete", "path": "keep.txt", "content": ""}],
                "artifacts": {}, "decisions": [],
            })
        self.assertIn("deletion is not supported", str(caught.exception))
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_non_utf8_file_is_not_overwritten(self):
        target = self.root / "binary.dat"
        target.write_bytes(b"\xff\xfe\x00raw")
        self._rejects("binary.dat", "non-UTF-8")
        self.assertEqual(target.read_bytes(), b"\xff\xfe\x00raw")

    def test_directory_target_is_rejected(self):
        (self.root / "pkg").mkdir()
        self._rejects("pkg", "不是普通文件")

    def test_overwrite_records_before_and_after_hashes(self):
        target = self.root / "tracked.txt"
        target.write_text("before", encoding="utf-8")
        result = self._apply("tracked.txt", "after")
        record = result["applied_files"][0]
        self.assertFalse(record["created"])
        self.assertNotEqual(record["before_sha256"], record["after_sha256"])
        self.assertEqual(target.read_text(encoding="utf-8"), "after")


class RetryDoesNotRepublishTest(unittest.TestCase):
    """A retried node must not publish two versions of the same files.

    Publishing is atomic per batch but there is no rollback across attempts, so
    a side effect followed by a failure has to be terminal rather than retried.
    """

    def test_failure_after_publish_does_not_run_the_node_again(self):
        from app.domain.node_registry import NodeDefinition, NodeRegistry
        from app.domain.orchestrator import DagOrchestrator
        from app.domain.models import WorkflowDefinition

        attempts: list[int] = []
        publishes: list[int] = []

        registry = NodeRegistry()
        registry.register(NodeDefinition("publisher", "发布", "", output_fields=("result",)))
        registry.register_handler(
            "publisher",
            lambda _payload: (attempts.append(1), {"result": "ok"})[1],
        )

        def processor(_session, _node, _context, output):
            publishes.append(1)
            raise RuntimeError("validation failed after the files were written")

        class _Store:
            def save(self, session):
                return session

        orchestrator = DagOrchestrator(
            registry,
            _Store(),
            gateway=None,
            output_processor=processor,
            max_attempts=3,
        )
        session = Session.create("project", "publish", SessionMode.TASK)
        workflow = WorkflowDefinition(
            "publish-flow", "Publish", [WorkflowNode("publisher", "publisher", on_failure="retry")]
        )
        result = orchestrator.run(session, workflow)

        self.assertEqual(result.status, "failed")
        self.assertEqual(len(publishes), 1, "the output processor must run exactly once")
        self.assertEqual(len(attempts), 1, "a post-publish failure must not re-invoke the node")

    def test_failure_before_publish_still_retries(self):
        """Retry must survive for failures that happen before any side effect."""

        from app.domain.node_registry import NodeDefinition, NodeRegistry
        from app.domain.orchestrator import DagOrchestrator
        from app.domain.models import WorkflowDefinition

        attempts: list[int] = []
        publishes: list[int] = []

        def handler(_payload):
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("transient model error")
            return {"result": "ok"}

        registry = NodeRegistry()
        registry.register(NodeDefinition("flaky", "抖动", "", output_fields=("result",)))
        registry.register_handler("flaky", handler)

        def processor(_session, _node, _context, output):
            publishes.append(1)
            return output

        class _Store:
            def save(self, session):
                return session

        orchestrator = DagOrchestrator(
            registry, _Store(), gateway=None, output_processor=processor, max_attempts=3
        )
        session = Session.create("project", "flaky", SessionMode.TASK)
        workflow = WorkflowDefinition(
            "flaky-flow", "Flaky", [WorkflowNode("flaky", "flaky", on_failure="retry")]
        )
        result = orchestrator.run(session, workflow)

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(attempts), 2, "a pre-publish failure must still retry")
        self.assertEqual(len(publishes), 1)


class WorkspaceSymlinkEscapeTest(unittest.TestCase):
    """Symlink defences need real links, which some platforms restrict."""

    def setUp(self):
        self.runtime = WorkspaceRuntime()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.outside = Path(self.tmp.name) / "outside"
        self.outside.mkdir()

    def _link(self, link: Path, target: Path, *, directory: bool) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as error:
            # Windows needs developer mode or elevation for symlinks.
            self.skipTest(f"symlinks unavailable on this platform: {error}")

    def _apply(self, path: str) -> None:
        session = Session.create("project", "writes", SessionMode.TASK)
        context = ContextState(inputs={"project": {
            "project_id": "p", "name": "P", "workspace_path": str(self.root),
        }})
        self.runtime.process_output(session, WorkflowNode("build", "implementation"), context, {
            "changes": "test",
            "file_changes": [{"operation": "write", "path": path, "content": "payload"}],
            "artifacts": {}, "decisions": [],
        })

    def test_write_through_a_symlinked_directory_is_rejected(self):
        self._link(self.root / "escape", self.outside, directory=True)
        with self.assertRaises(ValueError) as caught:
            self._apply("escape/owned.txt")
        self.assertIn("越出工作区", str(caught.exception))
        self.assertFalse((self.outside / "owned.txt").exists())

    def test_write_to_a_symlinked_file_is_rejected(self):
        victim = self.outside / "victim.txt"
        victim.write_text("original", encoding="utf-8")
        self._link(self.root / "victim.txt", victim, directory=False)
        with self.assertRaises(ValueError) as caught:
            self._apply("victim.txt")
        self.assertIn("符号链接", str(caught.exception))
        self.assertEqual(victim.read_text(encoding="utf-8"), "original")

    def test_symlink_inside_the_workspace_is_not_followed_silently(self):
        """An in-workspace link must not redirect a write to another file.

        The escape check alone would accept this path, because the resolved
        target is still inside the workspace.  Accepting it would publish
        evidence naming the link while writing to the link's target.
        """

        real = self.root / "real.txt"
        real.write_text("original", encoding="utf-8")
        self._link(self.root / "alias.txt", real, directory=False)
        with self.assertRaises(ValueError) as caught:
            self._apply("alias.txt")
        self.assertIn("符号链接", str(caught.exception))
        self.assertEqual(real.read_text(encoding="utf-8"), "original")

    def test_snapshot_does_not_follow_symlinks_out_of_the_workspace(self):
        (self.outside / "secret.txt").write_text("secret", encoding="utf-8")
        self._link(self.root / "escape", self.outside, directory=True)
        (self.root / "own.txt").write_text("own", encoding="utf-8")
        from app.domain.models import Project

        snapshot = self.runtime.snapshot(Project.from_dict({
            "project_id": "p", "name": "P", "workspace_path": str(self.root),
        }))
        paths = {item["path"] for item in snapshot["files"]}
        self.assertIn("own.txt", paths)
        self.assertNotIn("escape/secret.txt", paths)


if __name__ == "__main__":
    unittest.main()
