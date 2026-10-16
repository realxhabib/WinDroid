"""The emulator's own extra displays ("multi-display"), shared by app windows (emuview.py) and
protected screens (protected.py).

`adb emu multidisplay add <slot> ...` asks the emulator for a display; its MultiDisplayProvider in
Android (system uid) creates it with exactly the flags given, and the emulator composes it on the PC
side. The emulator has slots 1 to 8, shared by everything in this process (and anything else using
the same Android, e.g. a test harness), so every user claims its slot here first.
"""
from __future__ import annotations

import re
import threading
import time

from . import adb

MAX_SLOTS = 8  # "valid ids are from 1 to 8 inclusive", says `emu multidisplay add` (emulator 37)
MIN_DP = 320  # the emulator refuses a display narrower or shorter than 320 dp

# DisplayManager.VIRTUAL_DISPLAY_FLAG_*
PUBLIC, SECURE, OWN_CONTENT_ONLY, SUPPORTS_TOUCH, ROTATES_WITH_CONTENT = 1 << 0, 1 << 2, 1 << 3, 1 << 6, 1 << 7
DESTROY_CONTENT_ON_REMOVAL, SHOULD_SHOW_SYSTEM_DECORATIONS, TRUSTED = 1 << 8, 1 << 9, 1 << 10
OWN_DISPLAY_GROUP, ALWAYS_UNLOCKED, TOUCH_FEEDBACK_DISABLED, OWN_FOCUS = 1 << 11, 1 << 12, 1 << 13, 1 << 14

_slots: set[int] = set()  # emulator displays in use by this process
_lock = threading.Lock()


class NoSlot(RuntimeError):
    pass


def parse_emulator_displays(dumpsys_display: str) -> dict[int, int]:
    """`dumpsys display`: the emulator's extra displays, slot -> Android display id."""
    found = {}
    for line in dumpsys_display.splitlines():
        m = re.search(r'DisplayInfo\{"[^"]*", displayId (\d+),.*?uniqueId '
                      r'"virtual:com\.android\.emulator\.multidisplay:(\d+)"', line)
        if m:
            found[int(m.group(2)) - 1234561] = int(m.group(1))  # slot 1 is "1234562"
    return found


def emulator_displays() -> dict[int, int]:
    return parse_emulator_displays(adb.shell("dumpsys display", timeout=10))


def emu(*args: str) -> str:
    """An emulator console command; raises if the emulator refuses it."""
    out = adb.run("emu", *args, timeout=15).stdout.strip()
    if not out.endswith("OK"):
        raise RuntimeError(f"emu {' '.join(args)}: {out or 'no answer'}")
    return out


def min_px(dpi: int) -> int:
    return -(-MIN_DP * dpi // 160)


def claim(keep_free: int = 0) -> int:
    """A free slot, now ours. keep_free: leave that many free for others (protected screens)."""
    taken = set(emulator_displays())  # another agent's too, e.g. a test harness
    with _lock:
        free = [s for s in range(1, MAX_SLOTS + 1) if s not in _slots | taken]
        if len(free) <= keep_free:
            raise NoSlot("no free emulator display")
        _slots.add(free[0])
        return free[0]


def release(slot: int) -> None:
    with _lock:
        _slots.discard(slot)


def ours() -> set[int]:
    with _lock:
        return set(_slots)


def add(slot: int, width: int, height: int, dpi: int, flags: int, timeout: float = 10) -> int:
    """Make (or resize, if it exists) the display in a claimed slot; its Android display id once it's there."""
    args = ("multidisplay", "add", str(slot), str(width), str(height), str(dpi), str(flags))
    for _ in range(4):
        try:
            emu(*args)
            break
        except RuntimeError as e:
            # A windowless emulator answers "KO: setMultiDisplay not supported" but makes the display
            # anyway, so what counts is whether it turns up. (The first request after Android starts
            # may also only wake its MultiDisplayProvider: ask again.)
            if "not supported" not in str(e):
                raise
            time.sleep(1.5)
            if emulator_displays().get(slot) is not None:
                break
    end = time.monotonic() + timeout
    while (display := emulator_displays().get(slot)) is None:
        if time.monotonic() > end:
            raise RuntimeError(f"emulator display {slot} didn't come up")
        time.sleep(0.15)
    return display


def resize(slot: int, width: int, height: int, dpi: int, flags: int) -> None:
    """Same slot and flags: the emulator resizes the display in place."""
    emu("multidisplay", "add", str(slot), str(width), str(height), str(dpi), str(flags))


def remove(slot: int) -> None:
    try:
        emu("multidisplay", "del", str(slot))
    finally:
        release(slot)
