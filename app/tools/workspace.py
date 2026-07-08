from __future__ import annotations

import difflib
from pathlib import Path

from app.core.contracts import FileChange, PolicyBoundary, PolicyCheck
from app.policy.policy_checker import PolicyChecker
from app.tools.files import iter_text_files

CHANGE_ACTIONS = {"write", "delete"}


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
            elif base.get(path) != current[path]:
                changes.append(FileChange(path=path, content=current[path]))
        return changes

    def snapshot(self) -> dict[str, str]:
        files: dict[str, str] = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root).as_posix()
            try:
                files[relative] = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                files[relative] = "（非文本文件，内容未纳入快照）"
        return files

    def diff(self, before: dict[str, str], after: dict[str, str]) -> str:
        chunks: list[str] = []
        for path in sorted(set(before) | set(after)):
            old, new = before.get(path, ""), after.get(path, "")
            if old == new:
                continue
            lines = difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{path}" if path in before else "/dev/null",
                tofile=f"b/{path}" if path in after else "/dev/null",
            )
            chunks.append("".join(lines))
        return "\n".join(chunks)
