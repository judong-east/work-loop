from __future__ import annotations

from app.core.contracts import ContextPack, ContextSection, SourceRef, TaskState


class ContextPackBuilder:
    """Builds a provenance-aware context pack for a specific workflow purpose."""

    def build_from_text(
        self,
        task: TaskState,
        purpose: str,
        raw_text: str,
        source_uri: str = "input://inline",
    ) -> ContextPack:
        text = raw_text.strip()
        pack = ContextPack(task_id=task.task_id, purpose=purpose)

        if not text:
            pack.missing_context.append("缺少需求描述或输入材料。")
            return pack

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

        if "验收" not in text and "测试" not in text and "通过标准" not in text:
            pack.missing_context.append("缺少明确验收标准。")

        conflict_markers = ["冲突", "不一致", "待确认", "不确定"]
        if any(marker in text for marker in conflict_markers):
            pack.conflicts.append("输入中包含冲突或待确认信号，需要人工确认。")

        return pack

