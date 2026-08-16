from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from app.projects.git_process import run_git


class GitProcessTest(unittest.TestCase):
    def test_runner_sets_capture_text_and_timeout_defaults(self) -> None:
        process = mock.Mock(returncode=0)
        process.communicate.return_value = ("", "")
        with mock.patch(
            "app.projects.git_process.git_executable", return_value="git"
        ), mock.patch(
            "app.projects.git_process.subprocess.Popen", return_value=process
        ) as popen:
            result = run_git(["git", "status"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(popen.call_args.kwargs["text"], True)
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")
        self.assertEqual(process.communicate.call_args.kwargs["timeout"], 60)

    def test_runner_uses_binary_output_for_archive_commands(self) -> None:
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (b"", b"")
        with mock.patch(
            "app.projects.git_process.git_executable", return_value="git"
        ), mock.patch(
            "app.projects.git_process.subprocess.Popen", return_value=process
        ) as popen:
            run_git(["git", "archive"], text=False)

        self.assertFalse(popen.call_args.kwargs["text"])
        self.assertIsNone(popen.call_args.kwargs["encoding"])
        self.assertIsNone(popen.call_args.kwargs["errors"])

    def test_runner_turns_timeout_into_actionable_value_error(self) -> None:
        process = mock.Mock(returncode=None)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="git", timeout=60),
            ("", ""),
        ]
        with mock.patch(
            "app.projects.git_process.git_executable", return_value="git"
        ), mock.patch(
            "app.projects.git_process.subprocess.Popen", return_value=process
        ), mock.patch(
            "app.projects.git_process.ProcessTreeHandle"
        ):
            with self.assertRaisesRegex(ValueError, "Git 命令超时"):
                run_git(["git", "worktree", "list"])


if __name__ == "__main__":
    unittest.main()
