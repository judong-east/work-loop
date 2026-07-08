from __future__ import annotations

from pathlib import Path

from app.core.contracts import ContextPack, ContextSection, SourceRef, TaskState
from app.tools.files import iter_text_files, read_text_or_none

MAX_FILE_CHARS = 20000
TRUNCATED_MARK = "\n（内容过长，已截断）"


def _truncate(content: str) -> str:
    if len(content) <= MAX_FILE_CHARS:
        return content
    return content[:MAX_FILE_CHARS] + TRUNCATED_MARK


class ContextPackBuilder:
    """Builds a provenance-aware context pack for a specific workflow purpose."""

    def build_from_text(
        self,
        task: TaskState,
        purpose: str,
        raw_text: str,
        source_uri: str = "input://inline",
        context_files: list[Path] | None = None,
    ) -> ContextPack:
        text = raw_text.strip()
        pack = ContextPack(task_id=task.task_id, purpose=purpose)

        if not text and not context_files:
            pack.missing_context.append("缺少需求描述或输入材料。")
            return pack

        if text:
            pack.sections.append(
                ContextSection(
                    name="task_input",
                    content=text,
                    source_refs=[SourceRef(uri=source_uri, title="Inline task input", trust_level="user")],
                    confidence=0.85,
                    tags=["task", "requirement"],
                )
            )

            if len(text) < 20:
                pack.missing_context.append("输入过短，难以判断目标、约束和验收标准。")

            # 验收标准等需求启发式只看内联需求文本，避免被代码文件内容干扰
            if "验收" not in text and "测试" not in text and "通过标准" not in text:
                pack.missing_context.append("缺少明确验收标准。")

            conflict_markers = ["冲突", "不一致", "待确认", "不确定"]
            if any(marker in text for marker in conflict_markers):
                pack.conflicts.append("输入中包含冲突或待确认信号，需要人工确认。")

        for entry in context_files or []:
            self._add_file_sections(pack, Path(entry))

        return pack

    def _add_file_sections(self, pack: ContextPack, entry: Path) -> None:
        if entry.is_file():
            content = read_text_or_none(entry)
            if content is None:
                pack.missing_context.append(f"上下文文件 {entry} 不是可读文本文件。")
            else:
                self._append_file_section(pack, entry.name, entry, content)
        elif entry.is_dir():
            found = False
            for relative, content in iter_text_files(entry):
                self._append_file_section(pack, relative, entry / relative, content)
                found = True
            if not found:
                pack.missing_context.append(f"上下文目录 {entry} 中没有可读文本文件。")
        else:
            pack.missing_context.append(f"上下文路径 {entry} 不存在。")

    def _append_file_section(self, pack: ContextPack, name: str, path: Path, content: str) -> None:
        pack.sections.append(
            ContextSection(
                name=f"file:{name}",
                content=_truncate(content),
                source_refs=[SourceRef(uri=path.resolve().as_uri(), title=name, trust_level="user")],
                confidence=0.85,
                tags=["file"],
            )
        )
