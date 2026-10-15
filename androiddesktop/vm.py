"""The Android VM: Google's emulator running headless. Nothing of Android's own screen is ever shown;
every app gets a virtual display of its own (see appwindow.py).

Android always boots fresh (about half a minute) and is powered off cleanly, so its storage is always
consistent. Quick-boot snapshots would start faster, but restoring them headless proved unreliable
(hangs, or silently falling back to a cold boot), so they're off.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Callable

from . import adb
from .log import log
from .paths import AVD_HOME, AVD_NAME, EMULATOR_EXE, EMULATOR_PORT, LOGS, android_env

OFF, STARTING, RUNNING, STOPPING = "off", "starting", "running", "stopping"

# Applied on every boot (cheap and idempotent), so a reset device still behaves like a desktop.
DESKTOP_TWEAKS = "; ".join([
    # Let every app resize with its window, like a desktop app.
    "settings put global force_resizable_activities 1",
    "settings put global enable_non_resizable_multi_window 1",
    # Nobody looks at the main display: never sleep or lock it.
    "settings put system screen_off_timeout 2147483647",
    "svc power stayon true",
    "locksettings set-disabled true",
    "input keyevent KEYCODE_WAKEUP",
    "wm dismiss-keyguard",
    # A PC keyboard is attached: keep the on-screen keyboard out of the way.
    "settings put secure show_ime_with_hard_keyboard 0",
])


class VM:
    def __init__(self) -> None:
        self.state = OFF
        self.detail = ""
        self.listeners: list[Callable[[str, str], None]] = []
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._proc: subprocess.Popen | None = None

    # ------------------------------------------------------------------ state
    def _set(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail
        if state == RUNNING:
            self._ready.set()
        else:
            self._ready.clear()
        for fn in list(self.listeners):
            try:
                fn(state, detail)
            except Exception:
                pass

    def refresh(self) -> str:
        """Pick up a VM left running by an earlier session."""
        if self.state == OFF and adb.online() and adb.booted():
            self._set(RUNNING)
        return self.state

    # ------------------------------------------------------------------ start / stop
    def ensure_running(self, timeout: float = 300) -> bool:
        """Start the VM if needed and block until Android has booted."""
        with self._lock:
            if self.refresh() in (OFF, STOPPING):
                threading.Thread(target=self._boot, daemon=True, name="vm-boot").start()
        return self._ready.wait(timeout)

    def _boot(self) -> None:
        self._set(STARTING, "Starting Android...")
        adb.start_server()
        if adb.online() and not self._answers_within(15):
            self._kill_stale()
        if not adb.online():
            shutil.rmtree(AVD_HOME / f"{AVD_NAME}.avd" / "snapshots", ignore_errors=True)
            LOGS.mkdir(parents=True, exist_ok=True)
            out = (LOGS / "emulator.log").open("w", encoding="utf-8", errors="replace")
            log("vm: starting the emulator")
            self._proc = subprocess.Popen(
                [str(EMULATOR_EXE), "-avd", AVD_NAME, "-port", str(EMULATOR_PORT), "-no-window",
                 "-no-boot-anim", "-no-snapshot", "-gpu", "host", "-no-metrics",
                 # Keep guest RAM in memory instead of a 4 GB file that only quick-boot would use.
                 "-feature", "-QuickbootFileBacked"],
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=android_env(),
                creationflags=adb.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        started = time.monotonic()
        while not adb.booted():
            if self._proc and self._proc.poll() is not None:
                self._set(OFF, "Android stopped unexpectedly. See emulator.log.")
                return
            waited = time.monotonic() - started
            # About half a minute; the very first boot sets Android up and takes a couple of minutes.
            self._set(STARTING, "Starting Android..." if waited < 45 else "Starting Android (the first start takes a few minutes)...")
            time.sleep(1)
        adb.shell(DESKTOP_TWEAKS, timeout=30)
        log(f"vm: Android ready after {time.monotonic() - started:.0f}s")
        self._set(RUNNING)

    @staticmethod
    def _answers_within(seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if adb.responsive():
                return True
            time.sleep(1)
        return False

    def _kill_stale(self) -> None:
        """Our emulator is there but not answering (hung, or paused mid-snapshot): start over."""
        log("vm: emulator not answering, restarting it")
        self._set(STARTING, "Restarting Android...")
        self._power_off()

    @staticmethod
    def _power_off() -> None:
        try:
            adb.run("emu", "kill", timeout=15)
        except subprocess.TimeoutExpired:
            pass
        for _ in range(30):
            if not adb.online():
                return
            time.sleep(0.5)
        log("vm: emulator ignored 'emu kill', ending its processes")
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'qemu-system*' -or "
                        "$_.Name -eq 'emulator.exe') -and $_.CommandLine -like '*-avd " + AVD_NAME + "*' } | "
                        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                       creationflags=adb.CREATE_NO_WINDOW, timeout=60, capture_output=True)
        for _ in range(20):
            if not adb.online():
                return
            time.sleep(0.5)

    def stop(self) -> None:
        """Power Android off the way a phone does, so everything is written to its storage. Close app
        windows first. Falls back to pulling the plug if Android doesn't respond."""
        with self._lock:
            if not adb.online():
                self._set(OFF)
                return
            self._set(STOPPING, "Shutting down Android...")
            if self.state != OFF and adb.responsive():
                try:
                    adb.shell("sync; reboot -p", timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                for _ in range(40):
                    if not adb.online():
                        break
                    time.sleep(0.5)
            if adb.online():
                self._power_off()
            if self._proc:
                try:
                    self._proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
            self._set(OFF)
            log("vm: off")
