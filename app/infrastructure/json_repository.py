from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from app.core.atomic_files import write_json_atomic


T = TypeVar("T")


class JsonCollection(Generic[T]):
    """Small typed JSON collection used by the new application layer.

    Each aggregate is stored in its own file.  A corrupted record is skipped by
    ``list`` but surfaced by ``get`` so one bad session does not hide the rest of
    a project workspace.
    """

    def __init__(self, root: Path, decode: Callable[[dict[str, Any]], T], encode: Callable[[T], dict[str, Any]], prefix: str = ""):
        self.root = Path(root)
        self.decode = decode
        self.encode = encode
        self.prefix = prefix
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, value: T, key: str | None = None) -> T:
        if key is None:
            key = str(getattr(value, "session_id", "") or getattr(value, "project_id", "") or getattr(value, "id", ""))
            if not key:
                raise ValueError("a key is required for this record")
        self._validate_key(key)
        with self._lock:
            write_json_atomic(self.root / f"{self.prefix}{key}.json", self.encode(value))
        return value

    def get(self, key: str) -> T:
        self._validate_key(key)
        with self._lock:
            path = self.root / f"{self.prefix}{key}.json"
            if not path.is_file():
                raise FileNotFoundError(key)
            try:
                return self.decode(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
                raise ValueError(f"invalid record: {path}") from error

    def list(self) -> list[T]:
        with self._lock:
            values: list[T] = []
            for path in sorted(self.root.glob(f"{self.prefix}*.json")):
                try:
                    values.append(self.decode(json.loads(path.read_text(encoding="utf-8"))))
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    continue
            return values

    def delete(self, key: str) -> None:
        self._validate_key(key)
        with self._lock:
            path = self.root / f"{self.prefix}{key}.json"
            if path.exists():
                path.unlink()

    @staticmethod
    def _validate_key(key: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", key):
            raise ValueError(f"unsafe record key: {key!r}")
