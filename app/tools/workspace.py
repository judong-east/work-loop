from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from app.core.contracts import FileChange, PolicyBoundary, PolicyCheck
from app.policy.policy_checker import PolicyChecker
from app.tools.files import iter_text_files

CHANGE_ACTIONS = {"write", "delete"}


class WorkspaceSnapshot(dict[str, str]):
    def __init__(
        self,
        files: dict[str, str],
        raw_text: dict[str, str],
        digests: dict[str, str],
    ):
        super().__init__(files)
        self.raw_text = raw_text
        self.digests = digests


def _is_escaping(path: str) -> bool:
    # 沙箱只接受相对路径：根锚定（含 UNC）、盘符、.. 段一律视为逃逸。
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized:
        return True
    parts = normalized.split("/")
    return ".." in parts or not any(part for part in parts if part not in ("", "."))


class Workspace:
    """任务沙箱工作区：executor 的文件变更只允许落在 root 之内。"""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def validate(self, changes: list[FileChange], policy: PolicyBoundary, checker: PolicyChecker) -> PolicyCheck:
        # 先全量校验、后写入，任一违规即整批拒绝，保证不产生半套变更。
        issues: list[str] = []
        for change in changes:
            if change.action not in CHANGE_ACTIONS:
                issues.append(f"变更 {change.path} 的动作 {change.action} 不合法，只允许 write/delete。")
                continue
            if _is_escaping(change.path):
                issues.append(f"变更路径 {change.path} 越出沙箱范围。")
                continue
            path_check = checker.check_path(policy, change.path)
            issues.extend(path_check.issues)
        return PolicyCheck(passed=not issues, issues=issues)

    def apply(self, changes: list[FileChange]) -> list[str]:
        applied: list[str] = []
        for change in changes:
            relative = change.path.replace("\\", "/")
            target = self.root / relative
            if change.action == "delete":
                if target.exists():
                    target.unlink()
                    applied.append(relative)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(change.content, encoding="utf-8")
                applied.append(relative)
        return applied

    def seed(self, source: Path) -> list[str]:
        """把真实目录的文本文件播种进沙箱，作为 executor 的修改基线。"""
        seeded: list[str] = []
        for relative, content in iter_text_files(source):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            seeded.append(relative)
        return seeded

    def changes_since(self, base: dict[str, str]) -> list[FileChange]:
        """对比基线快照与当前状态，产出可交付的变更集。"""
        current = self.snapshot()
        changes: list[FileChange] = []
        for path in sorted(set(base) | set(current)):
            if path not in current:
                changes.append(FileChange(path=path, action="delete"))
            elif self._changed(base, current, path):
                changes.append(FileChange(path=path, content=current[path]))
        return changes

    def snapshot(self) -> dict[str, str]:
        files: dict[str, str] = {}
        raw_text: dict[str, str] = {}
        digests: dict[str, str] = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            if path.is_symlink():
                try:
                    target = path.readlink().as_posix()
                except OSError as error:
                    target = f"unreadable:{type(error).__name__}"
                identity = f"（符号链接，目标：{target}）"
                files[relative] = identity
                raw_text[relative] = identity
                digests[relative] = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                continue
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
                decoded = data.decode("utf-8")
                files[relative] = decoded.replace("\r\n", "\n").replace("\r", "\n")
                raw_text[relative] = decoded
                digests[relative] = hashlib.sha256(data).hexdigest()
            except UnicodeDecodeError:
                digest = hashlib.sha256(data).hexdigest()
                identity = f"（非文本文件，sha256:{digest}）"
                files[relative] = identity
                raw_text[relative] = identity
                digests[relative] = digest
            except OSError as error:
                identity = f"（文件不可读：{type(error).__name__}）"
                files[relative] = identity
                raw_text[relative] = identity
                digests[relative] = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return WorkspaceSnapshot(files, raw_text, digests)

    def diff(self, before: dict[str, str], after: dict[str, str]) -> str:
        chunks: list[str] = []
        for path in sorted(set(before) | set(after)):
            if not self._changed(before, after, path):
                continue
            if isinstance(before, WorkspaceSnapshot) and isinstance(after, WorkspaceSnapshot):
                old = before.raw_text.get(path, "")
                new = after.raw_text.get(path, "")
            else:
                old, new = before.get(path, ""), after.get(path, "")
            lines = difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{path}" if path in before else "/dev/null",
                tofile=f"b/{path}" if path in after else "/dev/null",
            )
            chunks.append("".join(lines))
        return "\n".join(chunks)

    def _changed(self, before: dict[str, str], after: dict[str, str], path: str) -> bool:
        if path not in before or path not in after:
            return path in before or path in after
        if isinstance(before, WorkspaceSnapshot) and isinstance(after, WorkspaceSnapshot):
            return before.digests.get(path) != after.digests.get(path)
        return before.get(path) != after.get(path)
