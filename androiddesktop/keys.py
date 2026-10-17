"""Putting text into an app window through its own keyboard path, as the PC's keyboard would.

Everything is posted to scrcpy's window as key messages, which scrcpy turns into key presses of its
virtual HID keyboard (UHID). Posted messages go to that one window, so nothing can spill into another
if the user clicks elsewhere, and the window needn't be in front.

Text goes in as a paste by default: the text on the PC's clipboard, then Ctrl+V, which scrcpy passes
on only after copying the clipboard to Android. That's exact. Typing key by key isn't, quite: Android's
keyboard app (Gboard) auto-corrects and capitalises hardware keys depending on their timing, and real
keystrokes (SendInput) lost characters on their way through Windows when tested. Typing is still there
(type()): characters a US key types without Shift go in as posted keys, the rest are pasted, because
SDL lets go of a posted Shift after every batch of messages unless Windows says the physical key is
down. The user's clipboard is put back after a paste.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable

import win32api
import win32clipboard
import win32con
import win32gui

# Set 1 scan codes of the US layout: char -> (scan code, shift)
_PLAIN = {"`": 0x29, "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06, "6": 0x07, "7": 0x08, "8": 0x09,
          "9": 0x0A, "0": 0x0B, "-": 0x0C, "=": 0x0D, "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14,
          "y": 0x15, "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19, "[": 0x1A, "]": 0x1B, "a": 0x1E, "s": 0x1F,
          "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23, "j": 0x24, "k": 0x25, "l": 0x26, ";": 0x27, "'": 0x28,
          "\\": 0x2B, "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30, "n": 0x31, "m": 0x32, ",": 0x33,
          ".": 0x34, "/": 0x35, " ": 0x39}
_SHIFTED = {"~": "`", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9",
            ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".",
            "?": "/"}
US_KEYS: dict[str, tuple[int, bool]] = {c: (sc, False) for c, sc in _PLAIN.items()}
US_KEYS.update({c.upper(): (sc, True) for c, sc in _PLAIN.items() if c.isalpha()})
US_KEYS.update({c: (_PLAIN[base], True) for c, base in _SHIFTED.items()})

SC_CTRL, SC_A, SC_C, SC_V, SC_END = 0x1D, 0x1E, 0x2E, 0x2F, 0x4F
EXTENDED = {SC_END}

# Typographic characters with a plain equivalent.
PLAIN = str.maketrans({"‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"',
                       "–": "-", "—": "-", "−": "-", " ": " ", " ": " ", " ": " ",
                       "\n": " ", "\r": " ", "\t": " "})


def normalize(text: str) -> str:
    return text.translate(PLAIN).replace("…", "...")


def chunks(text: str) -> list[tuple[str, str]]:
    """("keys", run) for what US keys type without Shift, ("paste", run) for the rest."""
    out: list[tuple[str, str]] = []
    for c in normalize(text):
        kind = "keys" if c in US_KEYS and not US_KEYS[c][1] else "paste"
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + c)
        else:
            out.append((kind, c))
    return out


def key_lparam(scan: int, up: bool) -> int:
    """WM_KEYDOWN/WM_KEYUP's lParam: repeat count 1, the scan code (and the extended-key bit), and for
    a release the previous-state and transition bits."""
    lp = 1 | (scan & 0xFF) << 16 | (1 << 24 if scan in EXTENDED else 0)
    if up:
        lp |= 1 << 30 | 1 << 31
    return lp


class Keyboard:
    """Key presses and pastes posted to one window (scrcpy's view of an app). Blocking: run it on a
    worker thread. stopped says why something stopped early."""

    def __init__(self, hwnd: int, cps: float = 12.0, cancel: threading.Event | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.hwnd = hwnd
        self.cps = cps
        self.cancel = cancel or threading.Event()
        self.sleep = sleep
        self.stopped = ""

    def _ok(self) -> bool:
        if self.cancel.is_set():
            self.stopped = "canceled"
        elif not win32gui.IsWindow(self.hwnd):
            self.stopped = "the window went away"
        else:
            return True
        return False

    def _post(self, scan: int, up: bool) -> None:
        msg = win32con.WM_KEYUP if up else win32con.WM_KEYDOWN
        win32gui.PostMessage(self.hwnd, msg, win32api.MapVirtualKey(scan, 1), key_lparam(scan, up))

    def press(self, scan: int, ctrl: bool = False) -> bool:
        if not self._ok():
            return False
        if ctrl:
            self._post(SC_CTRL, False)
        self._post(scan, False)
        self.sleep(random.uniform(0.025, 0.05))  # a key is held a moment, as a finger would
        self._post(scan, True)
        if ctrl:
            self._post(SC_CTRL, True)
        self.sleep(0.04)
        return True

    def select_all(self) -> bool:
        return self.press(SC_A, ctrl=True)

    def to_end(self) -> bool:
        """Cursor to the end of the field (Ctrl+End)."""
        return self.press(SC_END, ctrl=True)

    def paste(self, text: str) -> bool:
        """Paste text through the clipboard (scrcpy copies it to Android before passing Ctrl+V on),
        then put the user's clipboard back."""
        if not self._ok():
            return False
        saved = _get_clipboard()
        if not _set_clipboard(normalize(text)):
            self.stopped = "couldn't use the clipboard"
            return False
        try:
            ok = self.press(SC_V, ctrl=True)
            self.sleep(0.6)  # until scrcpy has sent the clipboard and the keys
            return ok
        finally:
            if saved is not None:
                _set_clipboard(saved)

    def type(self, text: str, progress: Callable[[float], None] | None = None) -> bool:
        """Key by key (see the module's notes on what Android's keyboard app may change); what no
        unshifted US key types is pasted. progress(0..1) as it goes."""
        total = max(1, len(normalize(text)))
        done = 0
        for kind, run in chunks(text):
            if kind == "paste":
                if not self.paste(run):
                    return False
                done += len(run)
            else:
                for c in run:
                    if not self.press(US_KEYS[c][0]):
                        return False
                    done += 1
                    if progress:
                        progress(done / total)
                    self.sleep(max(0.0, 1 / self.cps * random.uniform(0.6, 1.4) - 0.07))
            if progress:
                progress(done / total)
        return True

    def insert(self, text: str, mode: str = "paste", progress: Callable[[float], None] | None = None) -> bool:
        """Put text in at the cursor: pasted (exact), or typed (mode "type")."""
        if mode == "type":
            return self.type(text, progress)
        ok = self.paste(text)
        if ok and progress:
            progress(1.0)
        return ok


def _open_clipboard() -> bool:
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            return True
        except Exception:
            time.sleep(0.02)
    return False


def _get_clipboard() -> str | None:
    if not _open_clipboard():
        return None
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return None
    except Exception:
        return None
    finally:
        win32clipboard.CloseClipboard()


def _set_clipboard(text: str) -> bool:
    if not _open_clipboard():
        return False
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        return True
    except Exception:
        return False
    finally:
        win32clipboard.CloseClipboard()


def post_click(hwnd: int, x: int, y: int) -> None:
    """A left click at client coordinates of a window, as its mouse messages (no need for it to be in
    front, and the real pointer stays where it is)."""
    lp = (y & 0xFFFF) << 16 | (x & 0xFFFF)
    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lp)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.06)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp)
