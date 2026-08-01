from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.atomic_files import write_json_atomic
from app.core.contracts import new_id, utc_now


@dataclass(frozen=True)
class ContextPack:
    task_id: str
    node_id: str
    summary: str
    facts: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    source_sessions: list[str] = field(default_factory=list)
    version: int = 1
    pack_id: str = field(default_factory=lambda: new_id("CONTEXT"))
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "node_id": self.node_id,
            "summary": self.summary,
            "facts": list(self.facts),
            "decisions": list(self.decisions),
            "constraints": list(self.constraints),
            "inputs": list(self.inputs),
            "artifacts": list(self.artifacts),
            "open_questions": list(self.open_questions),
            "source_sessions": list(self.source_sessions),
            "version": self.version,
            "pack_id": self.pack_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ContextPack":
        if not isinstance(data, dict):
            raise ValueError("context pack must be an object")
        summary = str(data.get("summary", "")).strip()
        if not summary:
            raise ValueError("context pack summary cannot be empty")
        values = {}
        for key in (
            "facts",
            "decisions",
            "constraints",
            "inputs",
            "artifacts",
            "open_questions",
            "source_sessions",
        ):
            raw = data.get(key, [])
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise ValueError(f"context pack {key} must be an array of strings")
            values[key] = list(raw)
        return cls(
            task_id=str(data.get("task_id", "")),
            node_id=str(data.get("node_id", "")),
            summary=summary,
            version=int(data.get("version", 1)),
            pack_id=str(data.get("pack_id", new_id("CONTEXT"))),
            created_at=str(data.get("created_at", utc_now())),
            **values,
        )


class ContextLedger:
    """Persist durable node handoffs without copying full chat histories."""

    def __init__(self, tasks_root: Path):
        self.tasks_root = Path(tasks_root)

    def write(self, pack: ContextPack) -> str:
        if not pack.task_id or not pack.node_id:
            raise ValueError("context pack task_id and node_id are required")
        reference = f"artifacts/context/{pack.node_id}/{pack.version}.json"
        write_json_atomic(self.tasks_root / pack.task_id / reference, pack.to_dict())
        return reference

    def read(self, task_id: str, reference: str) -> ContextPack:
        path = self.tasks_root / task_id / reference
        return ContextPack.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def read_ref(self, task_id: str, reference: str) -> ContextPack | None:
        """Like :meth:`read` but returns ``None`` when the pack is missing."""
        if not reference:
            return None
        path = self.tasks_root / task_id / reference
        if not path.is_file():
            return None
        return ContextPack.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def merge(
        self,
        task_id: str,
        node_id: str,
        packs: list[ContextPack],
        summary: str,
        inputs: list[str] | None = None,
    ) -> ContextPack:
        def unique(values: list[str]) -> list[str]:
            return list(dict.fromkeys(item for item in values if item))

        merged = ContextPack(
            task_id=task_id,
            node_id=node_id,
            summary=summary.strip(),
            facts=unique([item for pack in packs for item in pack.facts]),
            decisions=unique([item for pack in packs for item in pack.decisions]),
            constraints=unique([item for pack in packs for item in pack.constraints]),
            inputs=unique((inputs or []) + [item for pack in packs for item in pack.inputs]),
            artifacts=unique([item for pack in packs for item in pack.artifacts]),
            open_questions=unique([item for pack in packs for item in pack.open_questions]),
            source_sessions=unique([item for pack in packs for item in pack.source_sessions]),
            version=max((pack.version for pack in packs), default=0) + 1,
        )
        self.write(merged)
        return merged
