from __future__ import annotations

import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.projects.contracts import Project


@dataclass
class PreparedWorktree:
    task_id: str
    path: Path
    base_commit: str
    target_branch: str
    task_branch: str


class GitWorktreeService:
    def inspect(self, repository: Path, default_branch: str = "") -> tuple[Path, str]:
        requested = Path(repository).resolve()
        top_level = Path(self._git(requested, "rev-parse", "--show-toplevel")).resolve()
        branch = default_branch.strip() or self._git(top_level, "branch", "--show-current")
        if not branch:
            raise ValueError("仓库处于 detached HEAD，必须明确指定默认目标分支。")
        self._git(top_level, "rev-parse", "--verify", f"refs/heads/{branch}")
        return top_level, branch

    def plan(self, project: Project, task_id: str, workspace: Path) -> PreparedWorktree:
        repository = Path(project.repository)
        self._require_clean(repository)
        base_commit = self._git(repository, "rev-parse", f"refs/heads/{project.default_branch}")
        task_branch = f"workloop/{task_id.lower()}"
        target = Path(workspace).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError(f"任务 worktree 路径已存在：{target}")
        return PreparedWorktree(
            task_id=task_id,
            path=target,
            base_commit=base_commit,
            target_branch=project.default_branch,
            task_branch=task_branch,
        )

    def ensure_prepared(
        self,
        project: Project,
        prepared: PreparedWorktree,
        allow_task_changes: bool = False,
    ) -> PreparedWorktree:
        repository = Path(project.repository)
        target = prepared.path.resolve()
        self._validate_task_branch(prepared.task_id, prepared.task_branch)
        expected_ref = f"refs/heads/{prepared.task_branch}"
        registered = self._registered_worktrees(repository)
        if target in registered:
            if registered[target] != expected_ref:
                raise ValueError(
                    f"任务 worktree 分支不匹配：期望 {expected_ref}，实际 {registered[target] or 'detached'}。"
                )
            actual_commit = self._git(target, "rev-parse", "HEAD")
            if actual_commit != prepared.base_commit:
                raise ValueError(
                    f"任务 worktree 基线不匹配：期望 {prepared.base_commit}，实际 {actual_commit}。"
                )
            if not allow_task_changes:
                self._require_clean(target)
            return prepared
        if target.exists():
            raise ValueError(f"任务路径存在但不是项目已注册的 worktree：{target}")

        if self._branch_exists(repository, expected_ref):
            branch_commit = self._git(repository, "rev-parse", expected_ref)
            if branch_commit != prepared.base_commit:
                raise ValueError(
                    f"任务分支基线不匹配：期望 {prepared.base_commit}，实际 {branch_commit}。"
                )
            self._git(repository, "worktree", "add", str(target), prepared.task_branch)
        else:
            self._git(
                repository,
                "worktree",
                "add",
                "-b",
                prepared.task_branch,
                str(target),
                prepared.base_commit,
            )
        self._require_clean(target)
        return prepared

    def remove(self, project: Project, prepared: PreparedWorktree) -> None:
        repository = Path(project.repository)
        target = prepared.path.resolve()
        task_branch = prepared.task_branch
        self._validate_task_branch(prepared.task_id, task_branch)
        registered = self._registered_worktrees(repository)
        if not task_branch.startswith("workloop/"):
            raise ValueError(f"拒绝删除非 Workloop 任务分支：{task_branch}")
        if target in registered:
            expected_ref = f"refs/heads/{task_branch}"
            if registered[target] != expected_ref:
                raise ValueError(
                    f"任务 worktree 分支不匹配：期望 {expected_ref}，实际 {registered[target] or 'detached'}。"
                )
            self._git(repository, "worktree", "remove", "--force", str(target))
        elif target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()

        branch_ref = f"refs/heads/{task_branch}"
        if self._branch_exists(repository, branch_ref):
            self._git(repository, "branch", "-D", task_branch)

    def _validate_task_branch(self, task_id: str, task_branch: str) -> None:
        expected = f"workloop/{task_id.lower()}"
        if task_branch != expected:
            raise ValueError(f"任务身份与分支不匹配：期望 {expected}，实际 {task_branch}。")

    def _registered_worktrees(self, repository: Path) -> dict[Path, str]:
        output = self._git(repository, "worktree", "list", "--porcelain")
        worktrees: dict[Path, str] = {}
        current: Path | None = None
        branch = ""
        for line in [*output.splitlines(), ""]:
            if line.startswith("worktree "):
                if current is not None:
                    worktrees[current] = branch
                current = Path(line.removeprefix("worktree ")).resolve()
                branch = ""
            elif line.startswith("branch "):
                branch = line.removeprefix("branch ")
            elif not line and current is not None:
                worktrees[current] = branch
                current = None
                branch = ""
        return worktrees

    def _branch_exists(self, repository: Path, branch_ref: str) -> bool:
        try:
            self._git(repository, "show-ref", "--verify", "--quiet", branch_ref)
        except ValueError:
            return False
        return True

    def _require_clean(self, repository: Path) -> None:
        status = self._git(repository, "status", "--porcelain", "--untracked-files=all")
        if status:
            raise ValueError("目标项目存在未提交修改，不能创建任务 worktree。")

    def _git(self, repository: Path, *args: str) -> str:
        trusted_repository = Path(repository).resolve()
        try:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    f"safe.directory={trusted_repository}",
                    "-C",
                    str(trusted_repository),
                    *args,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except OSError as error:
            raise ValueError(f"无法启动 Git：{error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ValueError(f"Git 命令失败（{' '.join(args)}）：{detail}")
        return result.stdout.strip()
