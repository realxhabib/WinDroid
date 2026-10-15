"""One shared log file in %LOCALAPPDATA%/AndroidDesktop/logs/androiddesktop.log."""
from __future__ import annotations

import threading
import time

from .paths import LOGS

_lock = threading.Lock()


def log(message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {message}"
    print(line, flush=True)
    with _lock:
        try:
            LOGS.mkdir(parents=True, exist_ok=True)
            with (LOGS / "androiddesktop.log").open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
