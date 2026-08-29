from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

from app.core.atomic_files import write_text_atomic
from app.core.process_tree import ProcessTreeHandle, process_group_options
from app.domain.models import ContextState, Project, Session, WorkflowNode


_IGNORED_DIRECTORIES = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv",
    "node_modules", "dist", "build", "coverage", "__pycache__", "workbench",
}
_INHERITED_ENVIRONMENT = {
    "COMSPEC", "LANG", "LC_ALL", "PATH", "PATHEXT", "SYSTEMROOT",
    "TEMP", "TMP", "USERPROFILE", "WINDIR",
}


class WorkspaceRuntime:
    """The V2-native boundary for reading, changing, and validating a workspace.

    Models never receive an unrestricted shell.  They propose complete file
    writes in structured output; this class validates every path and publishes
    each file atomically.  Validation commands are explicit project-owned argv
    arrays and are executed without a command shell.
    """

    def __init__(
        self,
        *,
        max_snapshot_files: int = 40,
        max_snapshot_chars: int = 160_000,
        max_change_files: int = 50,
        max_change_chars: int = 1_000_000,
        validation_timeout_seconds: int = 300,
    ):
        self.max_snapshot_files = max_snapshot_files
        self.max_snapshot_chars = max_snapshot_chars
        self.max_change_files = max_change_files
        self.max_change_chars = max_change_chars
        self.validation_timeout_seconds = validation_timeout_seconds

    def snapshot(self, project: Project) -> dict[str, Any]:
        if not project.workspace_path:
            return {"configured": False, "root": "", "files": [], "truncated": False}
        root = self._workspace(project.workspace_path)
        files: list[dict[str, Any]] = []
        used = 0
        truncated = False
        for path in self._files(root):
            if len(files) >= self.max_snapshot_files or used >= self.max_snapshot_chars:
                truncated = True
                break
            relative = path.relative_to(root).as_posix()
            try:
                size = path.stat().st_size
                remaining = self.max_snapshot_chars - used
                with path.open("rb") as stream:
                    raw = stream.read(remaining + 1)
            except OSError:
                continue
            if b"\0" in raw[:4096]:
                files.append({"path": relative, "binary": True, "size": size})
                continue
            text = raw.decode("utf-8", errors="replace")
            excerpt = text[:remaining]
            files.append({
                "path": relative,
                "content": excerpt,
                "size": size,
                "truncated": size > len(raw) or len(excerpt) < len(text),
            })
            used += len(excerpt)
            if size > len(raw) or len(excerpt) < len(text):
                truncated = True
                break
        return {
            "configured": True,
            "root": str(root),
            "files": files,
            "truncated": truncated,
        }

    def process_output(
        self,
        session: Session,
        node: WorkflowNode,
        context: ContextState,
        output: dict[str, Any],
    ) -> dict[str, Any]:
        del session
        if node.node_type != "implementation":
            return output
        changes = output.get("file_changes", [])
        if changes in (None, ""):
            changes = []
        if not isinstance(changes, list):
            raise ValueError("implementation file_changes must be a list")
        if len(changes) > self.max_change_files:
            raise ValueError(f"implementation exceeds {self.max_change_files} file changes")
        if not changes:
            processed = {**output, "applied_files": []}
            processed.pop("file_changes", None)
            return processed

        project = context.inputs.get("project", {})
        workspace_path = str(project.get("workspace_path", "")) if isinstance(project, dict) else ""
        if not workspace_path:
            raise ValueError("项目没有配置工作区，不能应用模型生成的文件变更。")
        root = self._workspace(workspace_path)
        total_chars = 0
        seen: set[str] = set()
        prepared: list[tuple[str, Path, str, bool, str]] = []
        for index, change in enumerate(changes, start=1):
            if not isinstance(change, dict):
                raise ValueError(f"file_changes[{index}] must be an object")
            operation = str(change.get("operation", "write"))
            if operation != "write":
                raise ValueError("V2 only permits atomic write operations; deletion is not supported")
            relative = str(change.get("path", "")).replace("\\", "/").strip()
            if not relative or relative in seen:
                raise ValueError(f"invalid or duplicate file path: {relative!r}")
            seen.add(relative)
            content = change.get("content")
            if not isinstance(content, str):
                raise ValueError(f"file change content must be text: {relative}")
            total_chars += len(content)
            if total_chars > self.max_change_chars:
                raise ValueError(f"implementation exceeds {self.max_change_chars} characters")
            target = self._safe_target(root, relative)
            existed = target.is_file()
            try:
                previous = target.read_bytes().decode("utf-8") if existed else ""
            except UnicodeDecodeError as error:
                raise ValueError(f"refusing to overwrite non-UTF-8 file: {relative}") from error
            prepared.append((relative, target, content, existed, previous))

        published: list[tuple[Path, bool, str]] = []
        try:
            for _relative, target, content, existed, previous in prepared:
                write_text_atomic(target, content)
                published.append((target, existed, previous))
        except Exception:
            for target, existed, previous in reversed(published):
                if existed:
                    write_text_atomic(target, previous)
                else:
                    target.unlink(missing_ok=True)
            raise

        applied: list[dict[str, Any]] = []
        for relative, _target, content, existed, previous in prepared:
            applied.append({
                "path": relative,
                "created": not existed,
                "before_sha256": self._digest(previous),
                "after_sha256": self._digest(content),
                "characters": len(content),
            })
        processed = {**output, "applied_files": applied}
        # Complete file contents are execution input, not durable task context.
        # Persist hashes and paths as evidence without duplicating source text in
        # session events or downstream model prompts.
        processed.pop("file_changes", None)
        processed["inputs"] = {
            "workspace": self.snapshot(Project.from_dict(project)),
        }
        return processed

    def validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        context = payload["context"]
        project = context.inputs.get("project", {})
        if not isinstance(project, dict) or not project.get("workspace_path"):
            return {
                "checks": [{"name": "workspace", "status": "skipped", "detail": "项目未配置工作区"}],
                "risks": ["没有执行真实验证命令"],
                "decisions": [],
            }
        root = self._workspace(str(project["workspace_path"]))
        commands = project.get("validation_commands", [])
        if not isinstance(commands, list) or not commands:
            return {
                "checks": [{"name": "configuration", "status": "skipped", "detail": "项目未配置验证命令"}],
                "risks": ["文件已写入，但没有自动化验证证据"],
                "decisions": [],
            }
        checks = [self._run_command(root, command, index) for index, command in enumerate(commands, start=1)]
        failed = [item for item in checks if item["status"] != "passed"]
        output: dict[str, Any] = {
            "checks": checks,
            "risks": [item["detail"] for item in failed],
            "decisions": ["所有项目验证命令均通过"] if not failed else [],
        }
        if failed:
            output["gate"] = {
                "name": "quality_review",
                "status": "blocked",
                "reason": f"{len(failed)} 个验证命令失败",
            }
        return output

    def _run_command(self, root: Path, command: Any, index: int) -> dict[str, Any]:
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            return {
                "name": f"command-{index}", "argv": [], "status": "failed",
                "exit_code": None, "detail": "验证命令不是合法 argv 数组", "stdout": "", "stderr": "",
            }
        environment = {
            name: value for name in sorted(_INHERITED_ENVIRONMENT)
            if (value := os.environ.get(name)) is not None
        }
        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **process_group_options(),
            )
            tree = ProcessTreeHandle(process)
            try:
                stdout, stderr = process.communicate(timeout=self.validation_timeout_seconds)
            except subprocess.TimeoutExpired:
                tree.terminate()
                stdout, stderr = process.communicate()
                return {
                    "name": f"command-{index}", "argv": list(command), "status": "failed",
                    "exit_code": None, "detail": f"超过 {self.validation_timeout_seconds} 秒",
                    "stdout": stdout[-12_000:], "stderr": stderr[-12_000:],
                }
            finally:
                tree.close()
            passed = process.returncode == 0
            return {
                "name": f"command-{index}",
                "argv": list(command),
                "status": "passed" if passed else "failed",
                "exit_code": process.returncode,
                "detail": "通过" if passed else f"退出码 {process.returncode}",
                "stdout": stdout[-12_000:],
                "stderr": stderr[-12_000:],
            }
        except OSError as error:
            return {
                "name": f"command-{index}", "argv": list(command), "status": "failed",
                "exit_code": None, "detail": str(error), "stdout": "", "stderr": "",
            }

    @staticmethod
    def _workspace(value: str) -> Path:
        root = Path(value).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"工作区不存在或不可访问：{root}")
        return root

    @staticmethod
    def _safe_target(root: Path, relative: str) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            raise ValueError(f"文件路径越出工作区：{relative}")
        lowered = {part.lower() for part in candidate.parts}
        if (
            not candidate.parts
            or lowered & {".git", ".hg", ".svn", ".workloop"}
            or candidate.parts[0].lower() == "workbench"
        ):
            raise ValueError(f"文件路径受保护：{relative}")
        target = (root / candidate).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ValueError(f"文件路径越出工作区：{relative}") from error
        current = target.parent
        while current != root:
            if current.is_symlink():
                raise ValueError(f"文件路径经过符号链接：{relative}")
            current = current.parent
        if target.is_symlink() or target.is_dir():
            raise ValueError(f"目标不是普通文件：{relative}")
        return target

    @staticmethod
    def _files(root: Path) -> list[Path]:
        values: list[Path] = []
        for current, directories, names in os.walk(root, followlinks=False):
            directories[:] = sorted(
                name for name in directories
                if name not in _IGNORED_DIRECTORIES and not (Path(current) / name).is_symlink()
            )
            base = Path(current)
            for name in sorted(names):
                path = base / name
                if not path.is_symlink():
                    values.append(path)
        return values

    @staticmethod
    def _digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
