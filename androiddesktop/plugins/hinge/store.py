"""The plugin's settings and history.

Settings come from Hinge Matcher's config.json when there is one (%USERPROFILE%\\hinge-matcher), so
the iPhone version and this one share the user's profile ("me"), voice and model; otherwise from
%LOCALAPPDATA%\\AndroidDesktop\\plugins\\hinge\\config.json. The file may hold an API key: it's read,
never logged or shown.

History is Hinge Matcher's append-only history.jsonl (data/ beside the config): what was inserted,
sent and skipped, so openers don't repeat, whichever phone they went out from.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from ...paths import DATA as APP_DATA

PLUGIN_DATA = APP_DATA / "plugins" / "hinge"
DEFAULTS = {
    "backend": "auto",
    "model": "claude-opus-5-5",
    "effort": "medium",
    "android_effort": "low",  # openers here: a few seconds rather than the iPhone version's "effort"
    "reply_effort": "low",
    "insert": "paste",  # how an idea goes into Hinge's box: "paste" (exact) or "type" (key by key; see keys.py)
    "typing_cps": 12,
    "read_whole_profile": True,  # scroll through each new profile (and back) to read all of it first
    "me": {},
}
_lock = threading.Lock()


def home() -> Path:
    """Hinge Matcher's folder if it's there, else the plugin's own."""
    env = os.environ.get("HINGE_MATCHER_HOME")
    for p in ([Path(env)] if env else []) + [Path.home() / "hinge-matcher"]:
        if (p / "config.json").is_file():
            return p
    return PLUGIN_DATA


def config_path() -> Path:
    return home() / "config.json"


def load_config() -> dict:
    """Settings over the defaults. Never log the result: it may hold an API key."""
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        cfg.update(json.loads(config_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def _history() -> Path:
    h = home()
    folder = h / "data" if h != PLUGIN_DATA else PLUGIN_DATA
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "history.jsonl"


def log(event: str, **fields: object) -> None:
    rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, "via": "android", **fields}
    with _lock, _history().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _records() -> list[dict]:
    try:
        text = _history().read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def recent_openers(n: int = 30) -> list[str]:
    """Openers used lately: sent ones (iPhone) and inserted ones (here, where the user taps Send)."""
    return [r["comment"] for r in _records() if r.get("event") in ("sent", "inserted") and r.get("comment")][-n:]


def recent_replies(n: int = 15) -> list[str]:
    return [r["text"] for r in _records()
            if r.get("event") in ("replied", "inserted_reply") and r.get("text")][-n:]


def background(name: str) -> str:
    """What we saw on their profile when we wrote the opener for them (if we did)."""
    if not name:
        return ""
    for r in reversed(_records()):
        if r.get("event") in ("sent", "inserted") and (r.get("name") or "").strip().lower() == name.strip().lower():
            det = "; ".join(f"{k}: {v}" for k, v in (r.get("details") or {}).items())
            parts = [f"Details: {det}" if det else "",
                     f"The user liked their {r.get('kind', 'card')}: {r.get('card', '')}".strip(),
                     f"The user's opener: {r.get('comment', '')}"]
            return "\n".join(p for p in parts if p)
    return ""
