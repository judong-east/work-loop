from __future__ import annotations

import json
from pathlib import Path

from app.core.contracts import ExperienceRecord, experience_record_from_dict, to_plain, utc_now

# 评审优先的经验库（借鉴 memory-lane 的两条原则）：
# - append-only JSONL：只追加不改写，按 experience_id 折叠、同 id 最新一条胜出，历史全部保留；
# - 自动捕获的经验默认 pending，只有人工批准（approved）的经验才会注入后续任务。
EXPERIENCE_STATUSES = {"pending", "approved", "rejected"}
EXPERIENCE_KINDS = {"review_pattern", "clarification", "manual"}
MAX_TEXT_CHARS = 2000


def _normalized_key(text: str) -> str:
    return "".join(text.split()).lower()


class ExperienceStore:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "experience.jsonl"

    def list_all(self) -> list[ExperienceRecord]:
        if not self.path.exists():
            return []
        folded: dict[str, ExperienceRecord] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                folded_record = experience_record_from_dict(json.loads(stripped))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue  # 坏行跳过但保留在文件里供诊断
            folded[folded_record.experience_id] = folded_record
        return list(folded.values())

    def pending(self) -> list[ExperienceRecord]:
        return [record for record in self.list_all() if record.status == "pending"]

    def approved(self) -> list[ExperienceRecord]:
        return [record for record in self.list_all() if record.status == "approved"]

    def suggest(self, text: str, kind: str, source_task: str = "") -> ExperienceRecord | None:
        """登记一条候选经验；内容与已有记录（含已驳回）重复时不再登记，返回 None。"""
        if kind not in EXPERIENCE_KINDS:
            raise ValueError(f"未知经验类型 {kind}。")
        cleaned = text.strip()[:MAX_TEXT_CHARS]
        if not cleaned:
            return None
        key = _normalized_key(cleaned)
        # 与全部历史（含 rejected）去重：驳回过的模式不应反复回到评审队列
        for record in self.list_all():
            if _normalized_key(record.text) == key:
                return None
        record = ExperienceRecord(text=cleaned, kind=kind, source_task=source_task)
        self._append(record)
        return record

    def add_manual(self, text: str) -> ExperienceRecord:
        """人工录入的经验视为已批准（作者即评审人）。"""
        cleaned = text.strip()[:MAX_TEXT_CHARS]
        if not cleaned:
            raise ValueError("经验内容不能为空。")
        record = ExperienceRecord(text=cleaned, kind="manual", status="approved")
        self._append(record)
        return record

    def approve(self, experience_id: str) -> ExperienceRecord:
        return self._set_status(experience_id, "approved")

    def reject(self, experience_id: str) -> ExperienceRecord:
        return self._set_status(experience_id, "rejected")

    def _set_status(self, experience_id: str, status: str) -> ExperienceRecord:
        if status not in EXPERIENCE_STATUSES:
            raise ValueError(f"未知状态 {status}。")
        for record in self.list_all():
            if record.experience_id == experience_id:
                record.status = status
                record.updated_at = utc_now()
                self._append(record)
                return record
        raise FileNotFoundError(f"经验 {experience_id} 不存在。")

    def _append(self, record: ExperienceRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(to_plain(record), ensure_ascii=False) + "\n")
