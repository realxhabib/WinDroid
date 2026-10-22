"""Recording what an AI agent does in a hidden app, for demo videos (tools/demo.py).

Off unless `tools/demo.py agent` has switched it on: it writes RECORDING (the folder to save into, and
until when), and the MCP server, which Claude Code or Claude Desktop runs, looks at it as each hidden
session starts. A recorded session gets a folder of its own:

    <folder>/<started>-<session id>/
        session.json    the app, the display size, when it started
        frames/0000.jpg the hidden screen as it opened, then after every tap, keystroke, scroll and key
        events.jsonl    each step: when (seconds from the start), what (tap x/y, type, scroll...), its frame

A video edit can then show the agent's real screens one step at a time, with a cursor going where it
tapped. Typed text is recorded as its length only, as in agent.log, and a screen Android marks secure
(a PIN or payment) is never saved: the step says "secure" instead.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime
from pathlib import Path

from .paths import DATA

RECORDING = DATA / "agent-recording.json"


def target() -> Path | None:
    """The folder to record into, if a demo has recording on right now."""
    try:
        spec = json.loads(RECORDING.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(spec, dict) or time.time() > float(spec.get("until", 0)):
        return None
    folder = spec.get("folder")
    return Path(folder) if folder else None


def switch_on(folder: Path, minutes: float) -> None:
    RECORDING.parent.mkdir(parents=True, exist_ok=True)
    RECORDING.write_text(json.dumps({"folder": str(folder), "until": time.time() + minutes * 60}), encoding="utf-8")


def switch_off() -> None:
    RECORDING.unlink(missing_ok=True)


class Recorder:
    def __init__(self, folder: Path, sid: str, package: str, label: str, size: tuple[int, int]) -> None:
        self.dir = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{sid}"
        (self.dir / "frames").mkdir(parents=True, exist_ok=True)
        self.start = time.monotonic()
        self.frames = 0
        (self.dir / "session.json").write_text(json.dumps({
            "session": sid, "package": package, "app": label, "size": list(size),
            "started": datetime.now().isoformat(timespec="seconds"),
        }, indent=1), encoding="utf-8")

    def step(self, what: str, image: dict | None = None, secure: bool = False, **details) -> None:
        """One step: what happened, and the screen after it (a helper "shot": base64 JPEG)."""
        event = {"t": round(time.monotonic() - self.start, 3), "what": what, **details}
        if secure:
            event["secure"] = True
        elif image and image.get("data"):
            name = f"{self.frames:04d}.jpg"
            (self.dir / "frames" / name).write_bytes(base64.b64decode(image["data"]))
            event["frame"] = name
            self.frames += 1
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
