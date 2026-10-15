"""The Android VM: Google's emulator running headless. Nothing of Android's own screen is ever shown;
every app gets a virtual display of its own (see appwindow.py).

Android always boots fresh (about half a minute) and is powered off cleanly, so its storage is always
consistent. Quick-boot snapshots would start faster, but restoring them headless proved unreliable
(hangs, or silently falling back to a cold boot), so they're off.

If the emulator dies under us, refresh() notices and the next ensure_running() boots a fresh one; app
windows call it when they reconnect, so a crash heals itself.

If a screen-lock PIN is set in Android, it boots LOCKED: until the PIN is entered (unlock()), apps' data
stays encrypted and only a few system apps can start. App windows wait for the unlock.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import threading
import time
from typing import Callable

from . import adb, checks
from .log import log
from .paths import AVD_HOME, AVD_NAME, EMULATOR_EXE, EMULATOR_PORT, LOGS, android_env

OFF, STARTING, RUNNING, STOPPING, LOCKED = "off", "starting", "running", "stopping", "locked"
LOCKED_DETAIL = "Android is locked. Enter your PIN to unlock it."
BOOT_TIMEOUT = 15 * 60  # the very first boot sets Android up and takes a few minutes

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
        self.keep_running = False  # True from ensure_running() until stop(): an exit is then a crash
        self.listeners: list[Callable[[str, str], None]] = []
        self._lock = threading.Lock()
        self._unlocking = threading.Lock()
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
        """Pick up a VM left running by an earlier session, notice one that died under us, and one that
        was unlocked some other way (e.g. in the emulator's own window)."""
        if self.state == OFF and adb.online() and adb.booted():
            if adb.user_locked():
                self._set(LOCKED, LOCKED_DETAIL)
            else:
                self._set(RUNNING)
        elif self.state in (RUNNING, LOCKED) and self._died():
            log("vm: Android stopped unexpectedly")
            self._set(OFF, "Android stopped unexpectedly.")
        elif self.state == LOCKED and adb.user_locked() is False:
            log("vm: Android was unlocked")
            self._set(RUNNING)
        return self.state

    def _died(self) -> bool:
        if self._proc is not None and self._proc.poll() is not None:
            return True
        if adb.online():
            return False
        time.sleep(2)  # not just an adb hiccup?
        return not adb.online()

    # ------------------------------------------------------------------ start / stop
    def ensure_running(self, timeout: float = 300, wait_for_unlock: bool = True) -> bool:
        """Start the VM if needed and block until Android has booted and is unlocked. False if it couldn't
        start. While it's locked, the timeout is on hold: we're waiting for the user, not for Android."""
        with self._lock:
            self.keep_running = True
            if self.refresh() == OFF:
                self._set(STARTING, "Starting Android...")
                threading.Thread(target=self._boot, daemon=True, name="vm-boot").start()
        end = time.monotonic() + timeout
        while not self._ready.wait(0.5):
            if self.state == LOCKED:
                if not wait_for_unlock:
                    return False
                end = time.monotonic() + timeout
            if self.state == OFF or time.monotonic() > end:
                return False  # the boot gave up (its reason is in self.detail)
        return True

    def _boot(self) -> None:
        problem = checks.blocking()
        if problem:  # e.g. Windows Hypervisor Platform is off: say so instead of failing vaguely
            log(f"vm: can't start: {problem.title}")
            self._set(OFF, problem.title)
            return
        adb.start_server()
        if adb.online() and not self._answers_within(15):
            self._kill_stale()
        if not adb.online():
            if self._proc and self._proc.poll() is None:  # ours, but adb lost it: start over
                self._proc.kill()
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
                log(f"vm: the emulator exited while starting (code {self._proc.returncode})")
                self._proc = None
                accel = checks.hypervisor()
                self._set(OFF, accel.title if accel.level == checks.BLOCKING else
                          "Android stopped unexpectedly. See emulator.log.")
                return
            waited = time.monotonic() - started
            if waited > BOOT_TIMEOUT:
                log("vm: Android didn't finish starting, giving up")
                self._power_off()
                self._set(OFF, "Android didn't finish starting. Try again, or see emulator.log.")
                return
            # About half a minute; the very first boot sets Android up and takes a couple of minutes.
            self._set(STARTING, "Starting Android..." if waited < 45 else "Starting Android (the first start takes a few minutes)...")
            time.sleep(1)
        adb.shell(DESKTOP_TWEAKS, timeout=30)
        if adb.user_locked():
            log(f"vm: Android started after {time.monotonic() - started:.0f}s, locked (a screen-lock PIN is set)")
            self._set(LOCKED, LOCKED_DETAIL)
            return
        log(f"vm: Android ready after {time.monotonic() - started:.0f}s")
        self._set(RUNNING)

    def unlock(self, pin: str) -> str:
        """Unlock Android with its screen-lock PIN or password, as on a phone after a restart. Returns ""
        once unlocked, otherwise what to tell the user. Wrong tries count towards Android's own lockout.
        The PIN never goes into a log (Android's error message repeats it, so that isn't logged either)."""
        if self.state != LOCKED:
            return ""
        if not pin or len(pin) > 64 or any(ord(c) < 32 for c in pin):
            return "Enter the PIN or password you use to unlock Android."
        with self._unlocking:
            try:
                out = adb.shell("locksettings verify --old " + shlex.quote(pin), timeout=30)
            except subprocess.TimeoutExpired:
                return "Android didn't answer. Try again."
            if "verified successfully" not in out:
                if "throttled" in out.lower():
                    log("vm: unlock: too many wrong tries, Android asks to wait")
                    return "Too many wrong tries. Wait 30 seconds, then try again."
                log("vm: unlock: wrong PIN")
                return "That's not the right PIN. Try again."
            log("vm: PIN accepted, unlocking")
            for _ in range(60):  # Android unlocks the user's storage in the background
                if adb.user_locked() is False:
                    break
                time.sleep(0.25)
            else:
                log("vm: still locked after the PIN was accepted")
                return "Android accepted the PIN but is still unlocking. Try again in a moment."
            adb.shell("input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard", timeout=15)
            log("vm: unlocked")
            self._set(RUNNING)
            return ""

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
            self.keep_running = False
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
