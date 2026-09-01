"""Local-only adapter for the zvec-grep CLI.

The search engine remains an independently maintained zvec-grep installation;
Workloop owns only argument validation, project-root scoping, process limits,
and a stable result envelope for model tools.  No remote endpoint or remote
embedding provider is used by this adapter.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ZvecGrepError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchResult:
    route: str
    output: str
    freshness: str = "direct"
    truncated: bool = False
    degraded: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": "zvec-grep",
            "route": self.route,
            "output": self.output,
            "freshness": self.freshness,
            "truncated": self.truncated,
            "degraded": self.degraded,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ZvecGrepConfig:
    command: tuple[str, ...] = ("zg",)
    timeout_seconds: float = 30.0
    # Matches the gateway's per-tool-result ceiling.  A larger value here would
    # be misleading: the transcript bound clips the result again before it
    # reaches the model, discarding whatever exceeded that ceiling.
    max_output_chars: int = 12_000
    max_semantic_limit: int = 20
    max_exact_limit: int = 200

    def validate(self) -> None:
        if not self.command:
            raise ValueError("zvec-grep command cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("zvec-grep timeout must be positive")
        if self.max_output_chars <= 0:
            raise ValueError("zvec-grep max_output_chars must be positive")
        if self.max_semantic_limit <= 0 or self.max_exact_limit <= 0:
            raise ValueError("zvec-grep result limits must be positive")


class ZvecGrepClient:
    """Run zvec-grep against a trusted local project root."""

    def __init__(self, config: ZvecGrepConfig | None = None):
        self.config = config or ZvecGrepConfig()
        self.config.validate()

    def available(self) -> bool:
        """Report whether the local zvec-grep executable can be launched.

        This is a cheap PATH lookup used to decide whether the search tools may
        be advertised to a model.  Advertising a tool whose backend is absent
        costs one failed model round per attempt, so the check belongs before
        the tools are offered rather than inside each tool call.
        """

        executable = self.config.command[0]
        return shutil.which(executable) is not None or Path(executable).is_file()

    def health(self, root: str | Path) -> dict[str, Any]:
        workspace = self._root(root)
        try:
            result = self._run(
                ["status", "--mode", "direct", "--check-ready"],
                workspace,
                timeout=min(self.config.timeout_seconds, 10),
            )
        except ZvecGrepError as error:
            return {"ready": False, "root": str(workspace), "error": str(error)}
        return {
            "ready": result.returncode == 0,
            "root": str(workspace),
            "error": (result.stderr or "").strip()[:500] if result.returncode else "",
        }

    def semantic_search(
        self,
        *,
        root: str | Path,
        query: str,
        fts: list[str] | None = None,
        globs: list[str] | None = None,
        file_types: list[str] | None = None,
        limit: int = 10,
        freshness: str = "eventual",
        fuse: bool = False,
    ) -> dict[str, Any]:
        workspace = self._root(root)
        query = self._text(query, "query", 2_000)
        if freshness not in {"eventual", "wait_for_fresh"}:
            raise ValueError("freshness must be eventual or wait_for_fresh")
        limit = self._limit(limit, self.config.max_semantic_limit)
        argv = [
            *self.config.command,
            "query",
            query,
            "--limit",
            str(limit),
            "--preview",
            "short",
            "--mode",
            "direct",
        ]
        if freshness == "wait_for_fresh":
            argv.extend(["--refresh", "wait"])
        else:
            # Workloop never schedules an index refresh implicitly.  The
            # index is maintained by the separately installed local zvec-grep
            # process, which keeps ordinary model calls free of embedding or
            # network side effects.
            argv.extend(["--refresh", "off"])
        if fuse:
            argv.append("--fuse")
        for item in fts or []:
            argv.extend(["--fts", self._text(item, "fts", 500)])
        for item in globs or []:
            argv.extend(["--glob", self._glob(item)])
        for item in file_types or []:
            argv.extend(["--type", self._text(item, "file_type", 100)])
        completed = self._run(argv, workspace)
        return self._result("semantic", completed, freshness=freshness)

    def exact_search(
        self,
        *,
        root: str | Path,
        pattern: str,
        path: str = "",
        globs: list[str] | None = None,
        file_types: list[str] | None = None,
        literal: bool = False,
        ignore_case: bool = False,
        context_lines: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        workspace = self._root(root)
        pattern = self._text(pattern, "pattern", 2_000)
        relative = self._relative_path(workspace, path)
        limit = self._limit(limit, self.config.max_exact_limit)
        context_lines = max(0, min(int(context_lines), 20))
        # zvec-grep's managed-rg route parses the argv remainder without a
        # shell.  Build it from validated structured arguments so the model
        # never supplies an arbitrary command.
        rg_args = ["-n", "--color=never"]
        if literal:
            rg_args.append("-F")
        if ignore_case:
            rg_args.append("-i")
        if context_lines:
            rg_args.extend(["-C", str(context_lines)])
        # -m bounds matches per file; the adapter also applies a global output
        # byte/character limit below.
        rg_args.extend(["-m", str(limit)])
        for item in globs or []:
            rg_args.extend(["-g", self._glob(item)])
        for item in file_types or []:
            rg_args.extend(["-t", self._text(item, "file_type", 100)])
        rg_args.extend(["--", pattern, relative or "."])
        # ``--rg`` is a CLI remainder route (the CLI itself invokes rg), so
        # pass validated arguments directly instead of constructing a shell
        # command string.  This mirrors ``zg query --rg -i ...`` and keeps the
        # model from injecting a shell pipeline.
        argv = [
            *self.config.command,
            "query",
            "--preview",
            "short",
            "--mode",
            "direct",
            "--rg",
            *rg_args,
        ]
        completed = self._run(argv, workspace)
        return self._result("exact", completed, limit=limit)

    def _run(self, argv: list[str], workspace: Path, *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        executable = shutil.which(argv[0])
        if executable is None and not Path(argv[0]).is_file():
            raise ZvecGrepError(f"zvec-grep executable not found: {argv[0]}")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {
                "PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
                "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
                "ZVEC_GREP_HOME", "ZVEC_GREP_EMBEDDING", "ZVEC_GREP_MODEL_CACHE",
                "ZVEC_GREP_DEVICE", "ZVEC_GREP_MCP_TOOLSET",
            }
        }
        environment["ZVEC_GREP_MODE"] = "direct"
        environment["ZVEC_GREP_ALLOW_REMOTE_EMBEDDING"] = "0"
        try:
            return subprocess.run(
                argv,
                cwd=str(workspace),
                env=environment,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ZvecGrepError(f"zvec-grep timed out after {timeout or self.config.timeout_seconds:g}s") from error
        except OSError as error:
            raise ZvecGrepError(f"unable to run zvec-grep: {error}") from error

    def _result(
        self,
        route: str,
        completed: subprocess.CompletedProcess[str],
        *,
        limit: int = 0,
        freshness: str = "direct",
    ) -> dict[str, Any]:
        output = (completed.stdout or "").strip()
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip()[:1_000] or output[:1_000]
            raise ZvecGrepError(f"zvec-grep {route} search failed: {detail}")
        truncated = len(output) > self.config.max_output_chars
        if truncated:
            output = output[: self.config.max_output_chars] + "\n…[zvec-grep output truncated]"
        return SearchResult(
            route=route,
            output=output,
            freshness=freshness,
            truncated=truncated,
            reason=f"limit={limit}" if limit else "",
        ).to_dict()

    @staticmethod
    def _root(root: str | Path) -> Path:
        value = Path(root).expanduser()
        if not value.is_absolute():
            raise ValueError("zvec-grep root must be absolute")
        value = value.resolve()
        if not value.is_dir():
            raise ValueError(f"zvec-grep root is not a directory: {value}")
        return value

    @staticmethod
    def _text(value: Any, name: str, limit: int) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{name} cannot be empty")
        if len(text) > limit:
            raise ValueError(f"{name} is too long")
        if "\x00" in text or "\r" in text or "\n" in text:
            raise ValueError(f"{name} contains invalid control characters")
        return text

    @staticmethod
    def _glob(value: Any) -> str:
        text = ZvecGrepClient._text(value, "glob", 500)
        if text.startswith("/") or text.startswith("\\") or ".." in Path(text).parts:
            raise ValueError("glob must be workspace-relative")
        return text

    @classmethod
    def _relative_path(cls, root: Path, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        candidate = Path(text)
        if candidate.is_absolute():
            raise ValueError("search path must be workspace-relative")
        resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError("search path escapes the workspace") from error
        # Check the unresolved path components as well as the resolved target.
        # ``Path.resolve`` removes the symlink marker, so checking only the
        # resolved path would otherwise allow a link inside the workspace to
        # redirect a search outside the project.
        current = root
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("search path cannot be a symlink")
        return resolved.relative_to(root).as_posix()

    @staticmethod
    def _limit(value: Any, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("search limit must be an integer") from error
        if number <= 0 or number > maximum:
            raise ValueError(f"search limit must be between 1 and {maximum}")
        return number
