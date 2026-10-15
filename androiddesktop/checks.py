"""Can this PC run Android? Checked before setup and before every boot, so a missing Windows feature or
a full disk shows up as a clear message with a fix instead of a vague failure later.

    python -m androiddesktop doctor     # print every check

The decide_* functions hold the rules and take plain numbers, so tests can feed them any PC.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .adb import CREATE_NO_WINDOW
from .paths import DATA, EMULATOR_EXE, android_env
from .runtime import PACKAGES, avd_dir

OK, WARNING, BLOCKING = "ok", "warning", "blocking"
GB = 1 << 30

ANDROID_RAM = 4 * GB  # what the VM itself takes
RECOMMENDED_RAM = int(7.5 * GB)  # "8 GB" PCs report a little less: some is reserved for hardware
LOW_DISK = 2 * GB
FIRST_BOOT = 3 * GB  # Android's own storage after its first start


@dataclass
class Check:
    id: str
    level: str  # OK, WARNING (Android still runs) or BLOCKING (it can't until this is fixed)
    title: str
    detail: str = ""
    fix: str = ""  # an action for the launcher's button, see FIXES in app.py
    fix_label: str = ""


def gb(n: float) -> str:
    return f"{n / GB:.1f}".rstrip("0").rstrip(".")


# ------------------------------------------------------------------ virtualization
def decide_hypervisor(dll_present: bool, hypervisor_running: bool | None,
                      accel: tuple[int, str] | None) -> Check:
    """dll_present: Windows Hypervisor Platform is installed. hypervisor_running: what that platform
    says (None: couldn't ask). accel: the emulator's own verdict (exit code, message), once installed."""
    if accel and accel[0] == 0:
        return Check("hypervisor", OK, "Virtualization is ready", accel[1])
    if not dll_present:
        return Check("hypervisor", BLOCKING, "Turn on Windows Hypervisor Platform",
                     "Android runs in a virtual machine, which needs this Windows feature. Turning it on "
                     "takes a minute, then a restart.", "enable_hypervisor", "Turn it on")
    if hypervisor_running is False:
        return Check("hypervisor", BLOCKING, "Restart to finish turning on virtualization",
                     "Windows Hypervisor Platform is on but not running yet. Restart your PC. If this "
                     "message stays, turn on virtualization (Intel VT-x or AMD SVM) in your PC's "
                     "UEFI/BIOS settings.")
    if accel:  # the platform looks fine, but the emulator still can't use it
        return Check("hypervisor", BLOCKING, "Android can't use virtualization",
                     accel[1] or "The Android Emulator can't use hardware acceleration on this PC.")
    return Check("hypervisor", OK, "Virtualization is ready", "Windows Hypervisor Platform is running.")


def parse_accel(output: str) -> str:
    """`emulator -accel-check` prints "accel:", a status code, the message, then "accel"."""
    lines = [line.strip() for line in output.splitlines()]
    lines = [line for line in lines if line and line not in ("accel:", "accel") and not line.isdigit()]
    return " ".join(lines)


def _system32() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def _hypervisor_running() -> bool | None:
    try:
        dll = ctypes.WinDLL(str(_system32() / "WinHvPlatform.dll"))
    except OSError:
        return None
    present, written = ctypes.c_int(0), ctypes.c_uint32(0)
    # WHvCapabilityCodeHypervisorPresent = 0; needs no admin rights
    hr = dll.WHvGetCapability(0, ctypes.byref(present), ctypes.sizeof(present), ctypes.byref(written))
    return bool(present.value) if hr == 0 else None


def _accel_check() -> tuple[int, str] | None:
    if not EMULATOR_EXE.exists():
        return None
    try:
        r = subprocess.run([str(EMULATOR_EXE), "-accel-check"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30, env=android_env(),
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode, parse_accel(r.stdout + r.stderr)


def hypervisor() -> Check:
    return decide_hypervisor((_system32() / "WinHvPlatform.dll").exists(), _hypervisor_running(), _accel_check())


# ------------------------------------------------------------------ memory
class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64)]


def _memory() -> tuple[int, int]:
    m = _MemoryStatus()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullTotalPhys, m.ullAvailPhys


def decide_memory(total: int, available: int, android_running: bool) -> Check:
    if total < RECOMMENDED_RAM:
        return Check("memory", WARNING, f"This PC has {gb(total)} GB of memory",
                     "Android uses about 5 GB while it runs, so it and your other apps may feel slow.")
    if not android_running and available < ANDROID_RAM:
        return Check("memory", WARNING, "Memory is running low",
                     f"{gb(available)} GB is free and Android needs about {gb(ANDROID_RAM)} GB. Close some "
                     "apps if it's slow to start.")
    return Check("memory", OK, f"{gb(total)} GB of memory", f"{gb(available)} GB free")


# ------------------------------------------------------------------ disk
def space_needed() -> int:
    """What setup still needs: each missing package's download plus its unpacked files (about twice the
    download), plus Android's own storage on its first start."""
    need = sum(2 * p.size for p in PACKAGES if not p.marker.exists())
    if not (avd_dir() / "userdata-qemu.img.qcow2").exists():
        need += FIRST_BOOT
    return need


def decide_disk(free: int, installed: bool, needed: int, drive: str) -> Check:
    if not installed and free < needed:
        return Check("disk", BLOCKING, "Not enough free disk space",
                     f"Setting up Android needs about {gb(needed)} GB free on drive {drive}; "
                     f"{gb(free)} GB is free.", "open_storage", "Free up space")
    if installed and free < LOW_DISK:
        return Check("disk", WARNING, f"Drive {drive} is almost full",
                     f"Only {gb(free)} GB is free. Android may not be able to save your apps' data.",
                     "open_storage", "Free up space")
    return Check("disk", OK, f"{gb(free)} GB free on drive {drive}")


# ------------------------------------------------------------------ all together
def run(installed: bool, android_running: bool = False) -> list[Check]:
    total, available = _memory()
    free = shutil.disk_usage(DATA.anchor).free
    return [hypervisor(), decide_memory(total, available, android_running),
            decide_disk(free, installed, space_needed(), DATA.drive)]


def blocking(installed: bool = True) -> Check | None:
    """The first problem that stops Android from starting (or setup from running), if any."""
    return next((c for c in run(installed) if c.level == BLOCKING), None)


def report(installed: bool, android_running: bool) -> str:
    marks = {OK: "ok  ", WARNING: "warn", BLOCKING: "FAIL"}
    lines = []
    for c in run(installed, android_running):
        lines.append(f"[{marks[c.level]}] {c.title}")
        if c.detail:
            lines.append(f"       {c.detail}")
    return "\n".join(lines)


# ------------------------------------------------------------------ fixes
def enable_hypervisor() -> bool:
    """Turn on Windows Hypervisor Platform. Windows asks for admin rights; raises pywintypes.error if the
    user says no. True once Windows accepted the change, which takes effect after a restart."""
    import win32con
    import win32event
    import win32process
    from win32com.shell import shell, shellcon

    info = shell.ShellExecuteEx(
        fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, lpVerb="runas", lpFile="powershell.exe",
        lpParameters="-NoProfile -Command Enable-WindowsOptionalFeature -Online "
                     "-FeatureName HypervisorPlatform -All -NoRestart",
        nShow=win32con.SW_HIDE)
    process = info["hProcess"]
    win32event.WaitForSingleObject(process, win32event.INFINITE)
    return win32process.GetExitCodeProcess(process) == 0


def open_storage_settings() -> None:
    os.startfile("ms-settings:storagesense")
