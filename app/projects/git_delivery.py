from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


class GitDelivery:
    def head(self, repository: Path, reference: str = "HEAD") -> str:
        return self._git(repository, "rev-parse", reference)

    def current_branch(self, repository: Path) -> str:
        return self._git(repository, "branch", "--show-current")

    def worktree_marker(self, repository: Path) -> str:
        return f"gitdir: {self._git(repository, 'rev-parse', '--git-dir')}\n"

    def is_clean(self, repository: Path) -> bool:
        return not self._git(
            repository,
            "status",
            "--porcelain",
            "--untracked-files=all",
        )

    def commit_all(self, workspace: Path, message: str) -> str:
        if self.is_clean(workspace):
            return self.head(workspace)
        self._git(workspace, "add", "-A")
        self._git(
            workspace,
            "-c",
            "user.name=Workloop",
            "-c",
            "user.email=workloop@localhost",
            "commit",
            "-m",
            message,
        )
        return self.head(workspace)

    def rebase(self, workspace: Path, target_branch: str) -> tuple[bool, str]:
        result = self._run(workspace, "rebase", f"refs/heads/{target_branch}")
        detail = (result.stderr or result.stdout).strip()
        return result.returncode == 0, detail

    def reset_mixed(self, workspace: Path, commit: str) -> None:
        self._git(workspace, "reset", "--mixed", commit)

    def changed_files(self, repository: Path, base: str, commit: str) -> list[str]:
        output = self._git(repository, "diff", "--name-only", "-z", base, commit)
        return sorted(path for path in output.split("\0") if path)

    def snapshot(self, repository: Path, commit: str) -> dict[str, str]:
        result = subprocess.run(
            ["git", "-C", str(repository), "archive", "--format=tar", commit],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(f"无法读取 Git 提交 {commit}：{detail}")
        files: dict[str, str] = {}
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"Git archive 包含不安全路径：{member.name}")
                relative = path.as_posix().rstrip("/")
                if not relative or member.isdir():
                    continue
                if member.issym():
                    files[relative] = f"（符号链接，目标：{member.linkname}）"
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                data = extracted.read()
                try:
                    files[relative] = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                except UnicodeDecodeError:
                    digest = hashlib.sha256(data).hexdigest()
                    files[relative] = f"（非文本文件，sha256:{digest}）"
        return files

    def deliver(
        self,
        repository: Path,
        target_branch: str,
        task_branch: str,
        task_commit: str,
        strategy: str,
    ) -> str:
        if strategy not in {"merge", "cherry-pick"}:
            raise ValueError("交付策略必须是 merge 或 cherry-pick。")
        if self.current_branch(repository) != target_branch:
            raise ValueError(f"真实项目必须检出目标分支 {target_branch} 后才能交付。")
        if not self.is_clean(repository):
            raise ValueError("真实项目存在未提交修改，不能交付。")
        if strategy == "merge":
            self._git(repository, "merge", "--ff-only", task_branch)
        else:
            self._git(repository, "cherry-pick", task_commit)
        return self.head(repository)

    def _git(self, repository: Path, *args: str) -> str:
        result = self._run(repository, *args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ValueError(f"Git 命令失败（{' '.join(args)}）：{detail}")
        return result.stdout.strip()

    @staticmethod
    def _run(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-c",
                    "core.longpaths=true",
                    "-c",
                    f"safe.directory={repository.resolve()}",
                    "-C",
                    str(repository),
                    *args,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except OSError as error:
            raise ValueError(f"无法启动 Git：{error}") from error
