"""One shared log file in %LOCALAPPDATA%/AndroidDesktop/logs/androiddesktop.log, and agent.log beside it
for what AI agents do (ghost.py, mcp.py)."""
from __future__ import annotations

import sys
import threading
import time
import traceback

from .paths import LOGS

_lock = threading.Lock()


def log(message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {message}"
    print(line, flush=True)
    _write("androiddesktop.log", line)


def agent_log(message: str) -> None:
    """An AI agent's action, in agent.log. Never printed: an MCP server's stdout is its protocol."""
    _write("agent.log", f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}")


def _write(name: str, line: str) -> None:
    with _lock:
        try:
            LOGS.mkdir(parents=True, exist_ok=True)
            with (LOGS / name).open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def log_uncaught() -> None:
    """Send uncaught exceptions, from any thread, to the log: under pythonw there's no console, so a
    thread that dies would otherwise leave no trace."""
    def report(where: str, exc_type, exc, tb) -> None:
        if exc_type is not SystemExit:
            log(f"uncaught exception in {where}:\n" + "".join(traceback.format_exception(exc_type, exc, tb)).rstrip())

    sys.excepthook = lambda t, e, tb: report("main thread", t, e, tb)
    threading.excepthook = lambda a: report(f"thread {a.thread.name if a.thread else '?'}",
                                            a.exc_type, a.exc_value, a.exc_traceback)
