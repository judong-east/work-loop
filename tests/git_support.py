from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def create_repository(root: Path, name: str = "repository") -> Path:
    repository = root / name
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    run_git(repository, "config", "user.email", "workloop@example.test")
    run_git(repository, "config", "user.name", "Workloop Test")
    run_git(repository, "config", "commit.gpgsign", "false")
    (repository / "app.txt").write_text("main\n", encoding="utf-8")
    run_git(repository, "add", "app.txt")
    run_git(repository, "commit", "-m", "initial")
    return repository
