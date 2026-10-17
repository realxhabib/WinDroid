"""Installs the Android runtime: Google's emulator, an Android 16 Google Play system image, adb and
scrcpy. Downloads are checksum-verified, extracted into DATA, and the zips removed to save disk.

    python -m androiddesktop setup
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .adb import CREATE_NO_WINDOW
from .paths import AVD_HOME, AVD_NAME, DATA, DOWNLOADS, EMULATOR_EXE, SCRCPY_HOME, SDK, android_env

Progress = Callable[[str, float], None]  # (message, fraction 0..1 or -1 for indeterminate)

SCRCPY_VERSION = "5.0.1"
API_LEVEL = 36
IMAGE_TAG = "google_apis_playstore"
IMAGE_REL = Path("system-images") / f"android-{API_LEVEL}" / IMAGE_TAG / "x86_64"


@dataclass(frozen=True)
class Package:
    name: str
    url: str
    size: int
    digest: str  # sha1 (Google repository) or sha256 (scrcpy release)
    root: Path  # the zip's single top-level folder lands here
    marker: Path  # exists once installed

    @property
    def zip_path(self) -> Path:
        return DOWNLOADS / self.url.rsplit("/", 1)[1]


PACKAGES = [
    Package("adb", "https://dl.google.com/android/repository/platform-tools_r37.0.1-win.zip",
            8044989, "e03e78b1d80b396f1c3358e31251cb31740e1110",
            SDK / "platform-tools", SDK / "platform-tools" / "adb.exe"),
    Package("scrcpy", f"https://github.com/Genymobile/scrcpy/releases/download/v{SCRCPY_VERSION}/"
                      f"scrcpy-win64-v{SCRCPY_VERSION}.zip",
            11264634, "b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5",
            SCRCPY_HOME, SCRCPY_HOME / "scrcpy.exe"),
    Package("Android Emulator", "https://dl.google.com/android/repository/emulator-windows_x64-16428233.zip",
            455342868, "488ed747e82de7e9bb5247becd1ac043c7e5e85d",
            SDK / "emulator", SDK / "emulator" / "emulator.exe"),
    Package("Android 16 (Google Play)",
            "https://dl.google.com/android/repository/sys-img/google_apis_playstore/x86_64-36_r07.zip",
            1924689206, "16fa3c441d29fde6c9eea2f766eecf77032d68b4",
            SDK / IMAGE_REL, SDK / IMAGE_REL / "system.img"),
]

SCRCPY_EXE = SCRCPY_HOME / "scrcpy.exe"


def installed() -> bool:
    return all(p.marker.exists() for p in PACKAGES) and avd_config().exists()


def _verify(path: Path, digest: str, progress: Progress, label: str) -> bool:
    h = hashlib.sha1() if len(digest) == 40 else hashlib.sha256()
    total, done = path.stat().st_size, 0
    with path.open("rb") as f:
        while chunk := f.read(1 << 22):
            h.update(chunk)
            done += len(chunk)
            progress(f"Checking {label}...", done / total)
    return h.hexdigest() == digest


def _download(pkg: Package, progress: Progress) -> None:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    part = pkg.zip_path.with_suffix(".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(pkg.url, headers={"Range": f"bytes={have}-"} if have else {})
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and r.status != 206:  # server ignored the range: start over
            have = 0
        with part.open("ab" if have else "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                have += len(chunk)
                progress(f"Downloading {pkg.name} ({have >> 20} of {pkg.size >> 20} MB)...", have / pkg.size)
    part.replace(pkg.zip_path)


def _extract(pkg: Package, progress: Progress) -> None:
    """Extract next to the target, then swap it in, so a half-extracted package never looks installed."""
    staging = pkg.root.with_name(pkg.root.name + ".extracting")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with zipfile.ZipFile(pkg.zip_path) as z:
        members = z.infolist()
        total = sum(m.file_size for m in members) or 1
        done = 0
        for m in members:
            z.extract(m, staging)
            done += m.file_size
            progress(f"Unpacking {pkg.name}...", done / total)
    (top,) = [p for p in staging.iterdir()]  # every package zip has one top-level folder
    shutil.rmtree(pkg.root, ignore_errors=True)
    pkg.root.parent.mkdir(parents=True, exist_ok=True)
    top.replace(pkg.root)
    shutil.rmtree(staging, ignore_errors=True)


def install(progress: Progress = lambda m, f: print(m, end="\r")) -> None:
    for pkg in PACKAGES:
        if pkg.marker.exists():
            continue
        if not (pkg.zip_path.exists() and _verify(pkg.zip_path, pkg.digest, progress, pkg.name)):
            pkg.zip_path.unlink(missing_ok=True)
            _download(pkg, progress)
            if not _verify(pkg.zip_path, pkg.digest, progress, pkg.name):
                pkg.zip_path.unlink(missing_ok=True)
                raise RuntimeError(f"{pkg.name} download is corrupt; please try again.")
        _extract(pkg, progress)
        pkg.zip_path.unlink(missing_ok=True)  # the system image alone is 1.9 GB
    # The emulator only accepts an SDK root that has a platforms folder.
    (SDK / "platforms").mkdir(exist_ok=True)
    create_avd()
    progress("Android is installed.", 1.0)


# ------------------------------------------------------------------ virtual device
def avd_dir() -> Path:
    return AVD_HOME / f"{AVD_NAME}.avd"


def avd_config() -> Path:
    return avd_dir() / "config.ini"


AVD_SETTINGS = {
    "AvdId": AVD_NAME,
    "avd.ini.displayname": "Android Desktop",
    "abi.type": "x86_64",
    "hw.cpu.arch": "x86_64",
    # Cores and memory (hw.cpu.ncore, hw.ramSize): vm_size(), for the PC, before every start.
    "vm.heapSize": "512M",
    "disk.dataPartition.size": "8G",
    "image.sysdir.1": str(IMAGE_REL) + "\\",
    "tag.id": IMAGE_TAG,
    "tag.ids": IMAGE_TAG,
    "tag.display": "Google Play",
    "tag.displaynames": "Google Play",
    "PlayStore.enabled": "true",
    "target": f"android-{API_LEVEL}",
    # The main display is never shown (every app gets its own virtual display), so keep it cheap.
    "hw.lcd.width": "720",
    "hw.lcd.height": "1280",
    "hw.lcd.density": "240",
    "hw.initialOrientation": "portrait",
    # A hardware keyboard: Android then keeps its on-screen keyboard out of the way, like on a PC.
    "hw.keyboard": "yes",
    "hw.mainKeys": "no",
    "hw.gpu.enabled": "yes",
    "hw.gpu.mode": "auto",
    "hw.audioInput": "yes",
    "hw.audioOutput": "yes",
    # The PC's webcam, as both of Android's cameras. Re-picked before every start (cameras()), so
    # a webcam that was unplugged or added is noticed, and a PC without one gets none.
    "hw.camera.back": "webcam0",
    "hw.camera.front": "webcam0",
    "hw.sdCard": "no",
    "fastboot.forceColdBoot": "yes",  # see vm.py: snapshots are off
    "showDeviceFrame": "no",
}


def _read_config() -> dict[str, str]:
    existing = {}
    if avd_config().exists():
        for line in avd_config().read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep:
                existing[k.strip()] = v.strip()
    return existing


def vm_config() -> dict[str, str]:
    """The AVD's settings as they stand: what Android runs with (or starts with next)."""
    return _read_config()


def _write_config(settings: dict[str, str]) -> None:
    avd_config().write_text("".join(f"{k}={v}\n" for k, v in settings.items()), encoding="utf-8")


def create_avd() -> None:
    d = avd_dir()
    d.mkdir(parents=True, exist_ok=True)
    existing = _read_config()
    existing.update(AVD_SETTINGS)
    existing.update(vm_size())
    existing.update(cameras())
    _write_config(existing)
    (AVD_HOME / f"{AVD_NAME}.ini").write_text(
        f"avd.ini.encoding=UTF-8\npath={d}\npath.rel=avd\\{d.name}\ntarget=android-{API_LEVEL}\n",
        encoding="utf-8")


# ------------------------------------------------------------------ size
MAX_CORES = 6  # "Emulator does not support more than 6 cores" (emulator 37)


def pick_vm_size(ram_mb: int, threads: int) -> dict[str, str]:
    """Cores and memory for Android, from the PC's. With 8 GB Android keeps a dozen apps in memory
    without killing any; with 4 it swaps and kills (measured with 14 apps: 735 MB swapped and one
    app killed at 4 GB, 292 MB swapped and none at 8). Six cores start apps a little faster than four.
    A PC with less to spare keeps more for Windows; never less than the 4 cores and 4 GB of before."""
    ram = 8192 if ram_mb >= 24 * 1024 else 6144 if ram_mb >= 14 * 1024 else 4096
    cores = min(MAX_CORES, max(4, threads // 2))
    return {"hw.cpu.ncore": str(cores), "hw.ramSize": f"{ram}M"}


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def vm_size() -> dict[str, str]:
    status = _MemoryStatus(dwLength=ctypes.sizeof(_MemoryStatus))
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return pick_vm_size(status.ullTotalPhys // (1 << 20), os.cpu_count() or 4)


def prepare_config(settings: dict[str, str], size: dict[str, str]) -> dict[str, str]:
    """The AVD's settings for the next start: the size for this PC, and none of the extra displays
    the emulator saved at its last exit (hw.displayN.*): app windows make their own (emuview.py)."""
    out = {k: v for k, v in settings.items() if not re.fullmatch(r"hw\.display\d+\.\w+", k)}
    out.update(size)
    return out


def configure_vm() -> dict[str, str]:
    """Before Android starts (changes apply from the next start): its size, and no leftover displays."""
    if not avd_config().exists():
        return {}
    settings = _read_config()
    wanted = prepare_config(settings, vm_size())
    if wanted != settings:
        _write_config(wanted)
    return {k: wanted[k] for k in ("hw.cpu.ncore", "hw.ramSize")}


# ------------------------------------------------------------------ webcam
def parse_webcams(listing: str) -> list[tuple[str, str]]:
    """`emulator -webcam-list`: (name, label) of each webcam, e.g. ("MX Brio", "webcam0")."""
    return re.findall(r"Camera '(.*)' can be specified by label as '(webcam\d+)'", listing)


def pick_cameras(webcams: list[tuple[str, str]]) -> dict[str, str]:
    """Both of Android's cameras show the PC's first webcam: on a PC every camera faces the user, and
    apps that want the "back" camera (QR code scanners, say) then get the good one too. None if
    there's no webcam: the emulator would fail to open one that isn't there."""
    label = webcams[0][1] if webcams else "none"
    return {"hw.camera.front": label, "hw.camera.back": label}


def webcams() -> list[tuple[str, str]]:
    """The webcams plugged in right now, as the emulator sees them (none if we can't tell)."""
    try:
        out = subprocess.run([str(EMULATOR_EXE), "-webcam-list"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30, env=android_env(),
                             creationflags=CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    return parse_webcams(out)


def cameras() -> dict[str, str]:
    """The camera settings for the webcams plugged in right now (none if we can't tell)."""
    return pick_cameras(webcams())


def configure_cameras() -> dict[str, str]:
    """Before Android starts: point its cameras at the webcams there are now. Changes take effect
    at the next start of Android, so this runs right before one."""
    if not avd_config().exists():
        return {}
    settings = _read_config()
    picked = cameras()
    if any(settings.get(k) != v for k, v in picked.items()):
        settings.update(picked)
        _write_config(settings)
    return picked


def disk_usage() -> int:
    return sum(f.stat().st_size for f in DATA.rglob("*") if f.is_file()) if DATA.exists() else 0
