from __future__ import annotations

from pathlib import Path
from typing import Iterator

# 上下文注入与工作区播种共用的文件遍历规则
PRUNE_DIRS = {".git", ".idea", ".vscode", "__pycache__", "node_modules", ".venv", "venv"}


def read_text_or_none(path: Path) -> str | None:
    """读取文本文件内容；二进制或不可读文件返回 None。"""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def iter_text_files(root: Path) -> Iterator[tuple[str, str]]:
    """递归产出 (相对正斜杠路径, 文本内容)，跳过剪枝目录与二进制文件。"""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in PRUNE_DIRS for part in relative.parts):
            continue
        content = read_text_or_none(path)
        if content is None:
            continue
        yield relative.as_posix(), content
