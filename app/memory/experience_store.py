from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from app.core.contracts import (
    ExperienceRecord,
    experience_record_from_dict,
    to_plain,
    utc_now,
)


_VALID_STATUSES = {"pending", "approved", "rejected"}
_VALID_KINDS = {"review_pattern", "clarification", "manual"}


class ExperienceStore:
    """Append-only JSONL store whose latest record for an id is authoritative."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "experience.jsonl"
        self._lock = threading.RLock()

    def list_all(self) -> list[ExperienceRecord]:
        with self._lock:
            return list(self._fold().values())

    def approved(self) -> list[ExperienceRecord]:
        return [record for record in self.list_all() if record.status == "approved"]

    def suggest(
        self,
        text: str,
        kind: str,
        source_task: str = "",
    ) -> ExperienceRecord | None:
        normalized = self._normalize_text(text)
        self._validate_kind(kind)
        with self._lock:
            if any(
                self._normalize_text(record.text).casefold() == normalized.casefold()
                for record in self._fold().values()
            ):
                return None
            record = ExperienceRecord(
                text=normalized,
                kind=kind,
                status="pending",
                source_task=source_task,
            )
            self._append(record)
            return record

    def add_manual(self, text: str) -> ExperienceRecord:
        normalized = self._normalize_text(text)
        with self._lock:
            duplicate = next(
                (
                    record
                    for record in self._fold().values()
                    if self._normalize_text(record.text).casefold() == normalized.casefold()
                ),
                None,
            )
            if duplicate is not None:
                raise ValueError(f"经验已存在：{duplicate.experience_id}")
            record = ExperienceRecord(
                text=normalized,
                kind="manual",
                status="approved",
            )
            self._append(record)
            return record

    def approve(self, experience_id: str) -> ExperienceRecord:
        return self._set_status(experience_id, "approved")

    def reject(self, experience_id: str) -> ExperienceRecord:
        return self._set_status(experience_id, "rejected")

    def _set_status(self, experience_id: str, status: str) -> ExperienceRecord:
        if status not in _VALID_STATUSES:
            raise ValueError(f"未知经验状态：{status}")
        with self._lock:
            record = self._fold().get(experience_id)
            if record is None:
                raise FileNotFoundError(f"经验 {experience_id} 不存在。")
            record.status = status
            record.updated_at = utc_now()
            self._append(record)
            return record

    def _fold(self) -> dict[str, ExperienceRecord]:
        records: dict[str, ExperienceRecord] = {}
        if not self.path.exists():
            return records
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"经验库第 {line_number} 行不是有效 JSON：{error}"
                ) from error
            if not isinstance(data, dict):
                raise ValueError(f"经验库第 {line_number} 行必须是对象。")
            record = experience_record_from_dict(data)
            if record.status not in _VALID_STATUSES:
                raise ValueError(f"经验 {record.experience_id} 状态无效：{record.status}")
            self._validate_kind(record.kind)
            records[record.experience_id] = record
        return records

    def _append(self, record: ExperienceRecord) -> None:
        payload = json.dumps(to_plain(record), ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _normalize_text(text: str) -> str:
        normalized = " ".join(str(text).split())
        if not normalized:
            raise ValueError("经验内容不能为空。")
        return normalized

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in _VALID_KINDS:
            raise ValueError(f"未知经验类型：{kind}")
