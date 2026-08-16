from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

from app.projects.contracts import Project
from app.projects.git_delivery import GitDelivery
from app.projects.git_process import run_git


def _default_policy() -> str:
    argv = ", ".join(json.dumps(value) for value in [sys.executable, "-c", "print('ok')"])
    return f"""schema_version = 1

[permissions]
protected_paths = [".workloop/project.toml"]
network = "deny"

[validation]
timeout_seconds = 300

[[validation.commands]]
name = "workloop-check"
argv = [{argv}]
"""


class DirectoryProjectService:
    """Back ordinary folders with an internal Git repository outside the source."""

    def __init__(self, root: Path, git: GitDelivery | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.git = git or GitDelivery()

    def initialize(self, project: Project) -> bool:
        source = self.source(project)
        repository = Path(project.repository).resolve()
        self._require_separate_roots(source, repository)
        if repository.exists():
            raise ValueError(f"Workloop 管理目录已存在：{repository}")
        repository.mkdir(parents=True)
        self._copy_source(source, repository)
        policy = self._safe_relative(repository, project.config_path)
        managed_policy = not policy.is_file()
        if managed_policy:
            policy.parent.mkdir(parents=True, exist_ok=True)
            policy.write_text(_default_policy(), encoding="utf-8")
        try:
            self._run(repository, "init", "-b", project.default_branch)
            self._commit(repository, "workloop: import directory baseline")
        except Exception:
            shutil.rmtree(repository.parent, ignore_errors=True)
            raise
        return managed_policy

    def sync_source(self, project: Project) -> tuple[str, str]:
        source = self.source(project)
        repository = Path(project.repository).resolve()
        self._require_separate_roots(source, repository)
        if not (repository / ".git").exists():
            raise ValueError(f"Workloop 管理仓库不存在：{repository}")
        self._copy_source(source, repository, preserve_git=True)
        policy = self._safe_relative(repository, project.config_path)
        if project.managed_policy and not policy.is_file():
            policy.parent.mkdir(parents=True, exist_ok=True)
            policy.write_text(_default_policy(), encoding="utf-8")
        commit = self._commit(repository, "workloop: sync source directory")
        return self.digest(source), commit

    def apply_changes(
        self,
        project: Project,
        base_commit: str,
        delivered_commit: str,
        expected_source_digest: str,
    ) -> list[str]:
        source = self.source(project)
        actual_digest = self.digest(source)
        if actual_digest != expected_source_digest:
            raise ValueError(
                "源目录在任务创建后发生了外部修改；为避免覆盖，请重新创建任务或先同步这些修改。"
            )
        repository = Path(project.repository).resolve()
        changed = self.git.changed_files(repository, base_commit, delivered_commit)
        applied: list[str] = []
        for relative in changed:
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts:
                raise ValueError(f"交付包含不安全路径：{relative}")
            if project.managed_policy and relative == project.config_path.replace("\\", "/"):
                continue
            managed_path = repository.joinpath(*pure.parts)
            source_path = source.joinpath(*pure.parts)
            if managed_path.is_symlink():
                source_path.parent.mkdir(parents=True, exist_ok=True)
                if source_path.exists() or source_path.is_symlink():
                    self._remove(source_path)
                source_path.symlink_to(os.readlink(managed_path), target_is_directory=managed_path.is_dir())
            elif managed_path.is_file():
                source_path.parent.mkdir(parents=True, exist_ok=True)
                if source_path.is_dir():
                    shutil.rmtree(source_path)
                shutil.copy2(managed_path, source_path)
            elif source_path.exists() or source_path.is_symlink():
                self._remove(source_path)
                self._prune_empty_parents(source_path.parent, source)
            applied.append(relative)
        return applied

    def digest(self, root: Path) -> str:
        root = Path(root).resolve()
        digest = hashlib.sha256()
        for path in self._files(root):
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            if path.is_symlink():
                digest.update(b"link\0")
                digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            else:
                digest.update(b"file\0")
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def source(project: Project) -> Path:
        source = Path(project.source_directory or project.repository).expanduser().resolve()
        if not source.is_dir():
            raise ValueError(f"项目目录不存在或不可访问：{source}")
        return source

    def _copy_source(self, source: Path, repository: Path, preserve_git: bool = False) -> None:
        if preserve_git:
            for child in repository.iterdir():
                if child.name == ".git":
                    continue
                self._remove(child)
        shutil.copytree(
            source,
            repository,
            dirs_exist_ok=True,
            symlinks=True,
            ignore=lambda _directory, names: {".git"} if ".git" in names else set(),
        )

    @staticmethod
    def _files(root: Path) -> list[Path]:
        files: list[Path] = []
        for current, directories, names in os.walk(root, followlinks=False):
            directories[:] = sorted(name for name in directories if name != ".git")
            base = Path(current)
            for name in sorted(names):
                if name == ".git":
                    continue
                files.append(base / name)
            for name in directories:
                candidate = base / name
                if candidate.is_symlink():
                    files.append(candidate)
        return sorted(set(files), key=lambda path: path.relative_to(root).as_posix())

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    @staticmethod
    def _safe_relative(root: Path, relative: str) -> Path:
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ValueError(f"项目配置路径越出管理仓库：{relative}") from error
        return target

    @staticmethod
    def _require_separate_roots(source: Path, repository: Path) -> None:
        try:
            repository.relative_to(source)
        except ValueError:
            return
        raise ValueError("Workloop 管理仓库必须位于源目录之外。")

    def _commit(self, repository: Path, message: str) -> str:
        self._run(repository, "add", "-A")
        status = self._run(repository, "status", "--porcelain", "--untracked-files=all").stdout
        if status.strip():
            self._run(
                repository,
                "-c",
                "user.name=Workloop",
                "-c",
                "user.email=workloop@localhost",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                message,
            )
        return self.git.head(repository)

    @staticmethod
    def _run(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
        result = run_git(
            [
                "git",
                "-c",
                "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null",
                "-c",
                "core.longpaths=true",
                "-c",
                f"safe.directory={repository.resolve()}",
                "-C",
                str(repository),
                *args,
            ],
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ValueError(f"内部 Git 命令失败（{' '.join(args)}）：{detail}")
        return result

    @staticmethod
    def _prune_empty_parents(path: Path, root: Path) -> None:
        while path != root:
            try:
                path.rmdir()
            except OSError:
                break
            path = path.parent
