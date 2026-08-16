from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.projects.git_process import run_git as run_git_process


def run_git(
    repository: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = run_git_process(["git", "-C", str(repository), *args])
    if check:
        result.check_returncode()
    return result


def create_repository(root: Path, name: str = "repository") -> Path:
    repository = root / name
    run_git_process(["git", "init", "-b", "main", str(repository)]).check_returncode()
    run_git(repository, "config", "user.email", "workloop@example.test")
    run_git(repository, "config", "user.name", "Workloop Test")
    run_git(repository, "config", "commit.gpgsign", "false")
    (repository / "app.txt").write_text("main\n", encoding="utf-8")
    policy = repository / ".workloop" / "project.toml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[permissions]",
                'protected_paths = [".workloop/project.toml"]',
                'network = "deny"',
                "",
                "[validation]",
                "timeout_seconds = 30",
                "",
                "[[validation.commands]]",
                'name = "fake-check"',
                f"argv = [{json.dumps(sys.executable)}, \"-c\", \"print('ok')\"]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    run_git(repository, "add", "app.txt", ".workloop/project.toml")
    run_git(repository, "commit", "-m", "initial")
    return repository
