from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.contracts import to_plain


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(to_plain(data), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        _replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_with_retry(temporary: Path, path: Path) -> None:
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.01 * (2**attempt))
