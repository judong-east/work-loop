from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.agents.contracts import AgentTaskStatus, ExecutionPlan, ValidationResult
from app.agents.fake_runtime import ScriptedFakeRuntime
from app.agents.workflow import AgentWorkflow
from app.projects.git_worktree import GitWorktreeService
from tests.git_support import create_repository, run_git


class PassingValidator:
    def validate(self, task_id: str, workspace: Path, plan: ExecutionPlan, policy) -> ValidationResult:
        return ValidationResult(passed=True)


class FailOnceBranchDeleteService(GitWorktreeService):
    def __init__(self):
        self.failed = False

    def _git(self, repository: Path, *args: str) -> str:
        if args[:2] == ("branch", "-D") and not self.failed:
            self.failed = True
            raise ValueError("injected branch delete failure")
        return super()._git(repository, *args)


class FailOnceAfterPrepareService(GitWorktreeService):
    def __init__(self):
        self.failed = False

    def ensure_prepared(self, project, prepared):
        result = super().ensure_prepared(project, prepared)
        if not self.failed:
            self.failed = True
            raise ValueError("injected post-prepare failure")
        return result


class PartialDirectoryPrepareService(GitWorktreeService):
    def ensure_prepared(self, project, prepared):
        prepared.path.mkdir(parents=True, exist_ok=True)
        (prepared.path / "partial.txt").write_text("partial\n", encoding="utf-8")
        raise ValueError("injected partial directory failure")


class ProjectWorktreeTest(unittest.TestCase):
    def test_git_commands_trust_only_the_registered_repository(self) -> None:
        repository = Path("relative-repository").resolve()
        completed = Mock(returncode=0, stdout="ok\n", stderr="")

        with patch(
            "app.projects.git_worktree.subprocess.run",
            return_value=completed,
        ) as run:
            output = GitWorktreeService()._git(repository, "rev-parse", "HEAD")

        self.assertEqual(output, "ok")
        self.assertEqual(
            run.call_args.args[0],
            [
                "git",
                "-c",
                f"safe.directory={repository}",
                "-C",
                str(repository),
                "rev-parse",
                "HEAD",
            ],
        )

    def test_preparing_task_can_cancel_unregistered_partial_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = create_repository(root)
            workloop_root = root / "workloop-data"
            workflow = AgentWorkflow(
                workloop_root,
                runtime=ScriptedFakeRuntime({}),
                validator=PassingValidator(),
                git_worktrees=PartialDirectoryPrepareService(),
            )
            project = workflow.register_project("示例项目", repository, "main")

            with self.assertRaisesRegex(ValueError, "partial directory"):
                workflow.create_task("取消半成品", "准备阶段永久失败", project.project_id)

            state_file = next((workloop_root / "tasks").glob("*/workflow-state.json"))
            preparing = workflow.get_task(state_file.parent.name)
            self.assertEqual(preparing.status, AgentTaskStatus.PREPARING_WORKSPACE)
            partial_path = Path(preparing.workspace)
            self.assertTrue(partial_path.is_dir())

            cancelled = workflow.cancel_task(preparing.task_id)

            self.assertEqual(cancelled.status, AgentTaskStatus.CANCELLED)
            self.assertFalse(partial_path.exists())

    def test_task_cannot_cancel_another_tasks_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = create_repository(root)
            workflow = AgentWorkflow(
                root / "workloop-data",
                runtime=ScriptedFakeRuntime({}),
                validator=PassingValidator(),
            )
            project = workflow.register_project("示例项目", repository, "main")
            first = workflow.create_task("任务 A", "不能删除 B", project.project_id)
            second = workflow.create_task("任务 B", "必须保留", project.project_id)
            first.workspace = second.workspace
            first.task_branch = second.task_branch
            workflow.store.save(first)

            with self.assertRaisesRegex(ValueError, "任务身份"):
                workflow.cancel_task(first.task_id)

            self.assertTrue(Path(second.workspace).is_dir())
            self.assertEqual(
                run_git(Path(second.workspace), "branch", "--show-current").stdout.strip(),
                second.task_branch,
            )

    def test_data_root_inside_repository_is_rejected_without_dirtying_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = create_repository(Path(tmp))
            workflow = AgentWorkflow(
                repository,
                runtime=ScriptedFakeRuntime({}),
                validator=PassingValidator(),
            )

            with self.assertRaisesRegex(ValueError, "数据根"):
                workflow.register_project("错误布局", repository, "main")

            self.assertEqual(run_git(repository, "status", "--porcelain").stdout.strip(), "")

    def test_task_creation_recovers_when_worktree_exists_but_final_state_was_not_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = create_repository(root)
            git_worktrees = FailOnceAfterPrepareService()
            workloop_root = root / "workloop-data"
            workflow = AgentWorkflow(
                workloop_root,
                runtime=ScriptedFakeRuntime({}),
                validator=PassingValidator(),
                git_worktrees=git_worktrees,
            )
            project = workflow.register_project("示例项目", repository, "main")

            with self.assertRaisesRegex(ValueError, "post-prepare"):
                workflow.create_task("恢复创建", "worktree 已创建后中断", project.project_id)

            state_files = list((workloop_root / "tasks").glob("*/workflow-state.json"))
            self.assertEqual(len(state_files), 1)
            interrupted = workflow.get_task(state_files[0].parent.name)
            self.assertEqual(interrupted.status, AgentTaskStatus.PREPARING_WORKSPACE)
            self.assertTrue(Path(interrupted.workspace).is_dir())

            (Path(interrupted.workspace) / "app.txt").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "未提交修改"):
                workflow.resume_task_creation(interrupted.task_id)
            self.assertEqual(
                workflow.get_task(interrupted.task_id).status,
                AgentTaskStatus.PREPARING_WORKSPACE,
            )
            run_git(Path(interrupted.workspace), "restore", "app.txt")

            restored = workflow.resume_task_creation(interrupted.task_id)

            self.assertEqual(restored.status, AgentTaskStatus.DRAFT)
            self.assertEqual(
                run_git(Path(restored.workspace), "branch", "--show-current").stdout.strip(),
                restored.task_branch,
            )
            worktrees = [
                line
                for line in run_git(repository, "worktree", "list", "--porcelain").stdout.splitlines()
                if line.startswith("worktree ")
            ]
            self.assertEqual(len(worktrees), 2)

    def test_cancel_retries_after_worktree_was_removed_but_branch_delete_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = create_repository(root)
            git_worktrees = FailOnceBranchDeleteService()
            workflow = AgentWorkflow(
                root / "workloop-data",
                runtime=ScriptedFakeRuntime({}),
                validator=PassingValidator(),
                git_worktrees=git_worktrees,
            )
            project = workflow.register_project("示例项目", repository, "main")
            task = workflow.create_task("可恢复取消", "模拟部分清理", project.project_id)

            with self.assertRaisesRegex(ValueError, "injected"):
                workflow.cancel_task(task.task_id)

            interrupted = workflow.get_task(task.task_id)
            self.assertEqual(interrupted.status, AgentTaskStatus.CANCELLING)
            self.assertFalse(Path(task.workspace).exists())

            cancelled = workflow.cancel_task(task.task_id)

            self.assertEqual(cancelled.status, AgentTaskStatus.CANCELLED)
            branch = run_git(
                repository,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{task.task_branch}",
                check=False,
            )
            self.assertNotEqual(branch.returncode, 0)

    def test_task_requires_registered_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflow = AgentWorkflow(
                Path(tmp),
                runtime=ScriptedFakeRuntime({}),
                validator=PassingValidator(),
            )

            with self.assertRaisesRegex(ValueError, "project_id"):
                workflow.create_task("无项目任务", "不允许目录旁路", project_id="")

    def test_cancelling_draft_removes_worktree_without_touching_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = create_repository(root)
            workflow = AgentWorkflow(
                root / "workloop-data",
                runtime=ScriptedFakeRuntime({}),
                validator=PassingValidator(),
            )
            project = workflow.register_project("示例项目", repository, "main")
            task = workflow.create_task("取消任务", "不要影响主目录", project_id=project.project_id)
            workspace = workflow.workspace_path(task.task_id)
            (workspace / "app.txt").write_text("task-only\n", encoding="utf-8")

            cancelled = workflow.cancel_task(task.task_id)

            self.assertEqual(cancelled.status, AgentTaskStatus.CANCELLED)
            self.assertFalse(workspace.exists())
            self.assertEqual((repository / "app.txt").read_text(encoding="utf-8"), "main\n")
            self.assertEqual(run_git(repository, "status", "--porcelain").stdout.strip(), "")
            branch = run_git(
                repository,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{task.task_branch}",
                check=False,
            )
            self.assertNotEqual(branch.returncode, 0)

    def test_dirty_repository_cannot_create_task_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = create_repository(root)
            workloop_root = root / "workloop-data"
            workflow = AgentWorkflow(
                workloop_root,
                runtime=ScriptedFakeRuntime({}),
                validator=PassingValidator(),
            )
            project = workflow.register_project("示例项目", repository, "main")
            (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "未提交修改"):
                workflow.create_task("不应创建", "仓库不干净", project_id=project.project_id)

            self.assertEqual(list((workloop_root / "tasks").rglob("workflow-state.json")), [])
            worktree_lines = [
                line
                for line in run_git(repository, "worktree", "list", "--porcelain").stdout.splitlines()
                if line.startswith("worktree ")
            ]
            self.assertEqual(len(worktree_lines), 1)

    def test_registered_project_creates_isolated_task_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = create_repository(root)
            workloop_root = root / "workloop-data"
            workflow = AgentWorkflow(
                workloop_root,
                runtime=ScriptedFakeRuntime({}),
                validator=PassingValidator(),
            )

            project = workflow.register_project(
                name="示例项目",
                repository=repository,
                default_branch="main",
            )
            task = workflow.create_task(
                "隔离修改",
                "修改 app.txt",
                project_id=project.project_id,
            )

            base_commit = run_git(repository, "rev-parse", "main").stdout.strip()
            workspace = workflow.workspace_path(task.task_id)
            self.assertEqual(task.project_id, project.project_id)
            self.assertEqual(task.base_commit, base_commit)
            self.assertEqual(task.target_branch, "main")
            self.assertEqual(run_git(workspace, "branch", "--show-current").stdout.strip(), task.task_branch)
            self.assertEqual((workspace / "app.txt").read_text(encoding="utf-8"), "main\n")

            (workspace / "app.txt").write_text("task\n", encoding="utf-8")

            self.assertEqual((repository / "app.txt").read_text(encoding="utf-8"), "main\n")
            reloaded = AgentWorkflow(
                workloop_root,
                runtime=ScriptedFakeRuntime({}),
                validator=PassingValidator(),
            )
            restored_project = reloaded.get_project(project.project_id)
            restored = reloaded.get_task(task.task_id)
            self.assertEqual(restored_project.repository, str(repository.resolve()))
            self.assertEqual(restored_project.default_branch, "main")
            self.assertEqual(restored_project.config_path, ".workloop/project.toml")
            self.assertEqual(restored.base_commit, base_commit)
            self.assertEqual(restored.task_branch, task.task_branch)
            self.assertEqual(restored.workspace, str(workspace.resolve()))


if __name__ == "__main__":
    unittest.main()
