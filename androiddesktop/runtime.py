"""Installs the Android runtime: Google's emulator, an Android 16 Google Play system image, adb and
scrcpy. Downloads are checksum-verified, extracted into DATA, and the zips removed to save disk.

    python -m androiddesktop setup
"""
from __future__ import annotations

import hashlib
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .paths import AVD_HOME, AVD_NAME, DATA, DOWNLOADS, SCRCPY_HOME, SDK

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
    "hw.cpu.ncore": "4",
    "hw.ramSize": "4096M",
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
    "hw.camera.back": "none",
    "hw.camera.front": "none",
    "hw.sdCard": "no",
    "fastboot.forceColdBoot": "yes",  # see vm.py: snapshots are off
    "showDeviceFrame": "no",
}


def create_avd() -> None:
    d = avd_dir()
    d.mkdir(parents=True, exist_ok=True)
    existing = {}
    if avd_config().exists():
        for line in avd_config().read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep:
                existing[k.strip()] = v.strip()
    existing.update(AVD_SETTINGS)
    avd_config().write_text("".join(f"{k}={v}\n" for k, v in existing.items()), encoding="utf-8")
    (AVD_HOME / f"{AVD_NAME}.ini").write_text(
        f"avd.ini.encoding=UTF-8\npath={d}\npath.rel=avd\\{d.name}\ntarget=android-{API_LEVEL}\n",
        encoding="utf-8")


def disk_usage() -> int:
    return sum(f.stat().st_size for f in DATA.rglob("*") if f.is_file()) if DATA.exists() else 0
