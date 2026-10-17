"""Secrets at rest, encrypted with DPAPI for the current Windows user (like hello.py's PIN store)."""
from __future__ import annotations

import win32crypt

_ENTROPY = b"WinDroid remote screens"


def protect(data: bytes) -> bytes:
    return win32crypt.CryptProtectData(data, "WinDroid", _ENTROPY, None, None, 0x1)  # CRYPTPROTECT_UI_FORBIDDEN


def unprotect(blob: bytes) -> bytes:
    return win32crypt.CryptUnprotectData(blob, _ENTROPY, None, None, 0x1)[1]
