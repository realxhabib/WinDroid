"""Small persistent settings (window positions, preferences) in state.json."""
from __future__ import annotations

import json
import threading
from typing import Any

from .paths import DATA, STATE

_lock = threading.Lock()
_data: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _data
    if _data is None:
        try:
            _data = json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _data = {}
    return _data


def get(key: str, default: Any = None) -> Any:
    with _lock:
        return _load().get(key, default)


def put(key: str, value: Any) -> None:
    with _lock:
        _load()[key] = value
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_data, indent=1), encoding="utf-8")
        tmp.replace(STATE)
