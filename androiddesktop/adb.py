"""Thin adb wrapper: always our adb, always our VM, never a console window."""
from __future__ import annotations

import subprocess

from .paths import ADB_EXE, SERIAL, android_env

CREATE_NO_WINDOW = 0x08000000


def run(*args: str, timeout: float = 30, check: bool = False, serial: bool = True) -> subprocess.CompletedProcess:
    cmd = [str(ADB_EXE), *(["-s", SERIAL] if serial else []), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=check, env=android_env(), creationflags=CREATE_NO_WINDOW)


def shell(command: str, timeout: float = 30) -> str:
    return run("shell", command, timeout=timeout).stdout


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


def start_server() -> None:
    try:
        run("start-server", serial=False, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        pass
