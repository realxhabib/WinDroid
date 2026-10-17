"""Thin adb wrapper: always our adb, always our VM, never a console window."""
from __future__ import annotations

import subprocess

from . import children
from .paths import ADB_EXE, SERIAL, android_env

CREATE_NO_WINDOW = 0x08000000


def run(*args: str, timeout: float = 30, check: bool = False, serial: bool = True) -> subprocess.CompletedProcess:
    cmd = [str(ADB_EXE), *(["-s", SERIAL] if serial else []), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=check, env=android_env(), creationflags=CREATE_NO_WINDOW)


def shell(command: str, timeout: float = 30) -> str:
    return run("shell", command, timeout=timeout).stdout


def start(*args: str, **popen_args) -> subprocess.Popen:
    """A long-lived adb, e.g. an `adb shell` streaming logcat. It ends when this process does, however
    that ends (children.py)."""
    return children.popen([str(ADB_EXE), "-s", SERIAL, *args], env=android_env(), creationflags=CREATE_NO_WINDOW,
                          **popen_args)


def online() -> bool:
    try:
        return run("get-state", timeout=5).stdout.strip() == "device"
    except (subprocess.TimeoutExpired, OSError):
        return False


def responsive() -> bool:
    """A paused or hung emulator still shows up in `adb devices` but never answers a shell."""
    try:
        return "ok" in shell("echo ok", timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return False


def booted() -> bool:
    try:
        return shell("getprop sys.boot_completed", timeout=5).strip() == "1"
    except (subprocess.TimeoutExpired, OSError):
        return False


def parse_user_locked(ce_available: str, user_state: str) -> bool | None:
    """ce_available: `getprop sys.user.0.ce_available`; user_state: `am get-started-user-state 0`."""
    if ce_available.strip() == "true":
        return False
    state = user_state.strip()
    if state.startswith("RUNNING_UNLOCKED"):
        return False
    if state.startswith(("RUNNING_LOCKED", "RUNNING_UNLOCKING", "BOOTING")):
        return True
    return None


def user_locked() -> bool | None:
    """True while Android's user is locked: a screen-lock PIN is set and hasn't been entered since boot
    ("direct boot"). Its apps' data stays encrypted until then, and only a few system apps can run.
    None if we couldn't tell."""
    try:
        ce_available = shell("getprop sys.user.0.ce_available", timeout=5)
        if ce_available.strip() == "true":
            return False
        return parse_user_locked(ce_available, shell("am get-started-user-state 0", timeout=15))
    except (subprocess.TimeoutExpired, OSError):
        return None


def start_server() -> None:
    try:
        run("start-server", serial=False, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        pass
