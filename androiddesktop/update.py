"""Updates for the installed WinDroid. At most once a day, ask GitHub for the latest release of
realxhabib/WinDroid; the launcher offers a newer one. On the user's word: download its installer,
check it against the SHA-256 published with it, start it, and quit (closing the app windows and
shutting Android down) so it can replace the program. The installer waits for that, puts the new
version in place and starts WinDroid again.

A source checkout never updates itself: that's git's job.

A release carries WinDroid-Setup-<version>.exe and WinDroid-Setup-<version>.exe.sha256 (tools/build_installer.py
makes both; .github/workflows/release.yml publishes them).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import __version__, state
from .log import log
from .paths import DATA

REPO = "realxhabib/WinDroid"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY = 24 * 3600
UPDATES = DATA / "updates"
INSTALLER = re.compile(r"WinDroid-Setup-(\d+(?:\.\d+)*)\.exe")
# What the installer is told: show its progress, ask nothing, and start WinDroid again when it's done.
INSTALLER_ARGS = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/relaunch=1"]

IDLE, AVAILABLE, DOWNLOADING, INSTALLING, FAILED = "idle", "available", "downloading", "installing", "failed"


@dataclass(frozen=True)
class Release:
    version: str
    page: str  # the release on GitHub, for its notes
    installer: str  # download URL
    size: int
    sha256: str  # URL of the .sha256 file published with it


def version_key(version: str) -> tuple[int, ...]:
    """"v1.2.10" -> (1, 2, 10); () if it isn't a version."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def newer(version: str, than: str = __version__) -> bool:
    a, b = version_key(version), version_key(than)
    return bool(a) and a > b


def parse_release(data: dict) -> Release | None:
    """GitHub's /releases/latest answer -> the installer to offer, if it has one with its checksum."""
    if data.get("draft") or data.get("prerelease"):
        return None
    assets = {a.get("name", ""): a for a in data.get("assets") or []}
    for name, asset in assets.items():
        m = INSTALLER.fullmatch(name)
        checksum = assets.get(name + ".sha256")
        if m and checksum and version_key(m.group(1)) == version_key(data.get("tag_name", "")):
            return Release(m.group(1), data.get("html_url", ""), asset["browser_download_url"],
                           int(asset.get("size") or 0), checksum["browser_download_url"])
    return None


def parse_sha256(text: str) -> str:
    """A .sha256 file ("<hex>  <file name>", or just the hex) -> the hex digest, lowercase."""
    m = re.match(r"\s*([0-9a-fA-F]{64})\b", text)
    if not m:
        raise ValueError("not a SHA-256 checksum file")
    return m.group(1).lower()


def _get(url: str, timeout: float = 20):
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        "User-Agent": f"WinDroid/{__version__}", "Accept": "application/vnd.github+json"}), timeout=timeout)


def latest() -> Release | None:
    """The latest release's installer, from GitHub. None if there's no release (yet)."""
    try:
        with _get(LATEST) as r:
            return parse_release(json.load(r))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


class Updater:
    """The launcher's view: state() for the UI, check() now and then, install() when the user says so."""

    def __init__(self, enabled: bool, on_change: Callable[[], None] = lambda: None,
                 on_ready: Callable[[], None] = lambda: None) -> None:
        """enabled: only the installed WinDroid updates itself. on_ready: the installer is running;
        quit so it can replace the program."""
        self.enabled = enabled
        self.on_change = on_change
        self.on_ready = on_ready
        self.status = IDLE
        self.release: Release | None = None
        self.progress = 0.0
        self.error = ""
        self._lock = threading.Lock()

    def snapshot(self) -> dict | None:
        if self.status == IDLE or self.release is None:
            return None
        return {"state": self.status, "version": self.release.version, "page": self.release.page,
                "progress": self.progress, "error": self.error, "current": __version__}

    def _set(self, status: str, error: str = "") -> None:
        self.status, self.error = status, error
        self.on_change()

    # ------------------------------------------------------------------ checking
    def check(self, force: bool = False) -> None:
        """Ask GitHub if a day has passed since the last time (any WinDroid process may have asked);
        otherwise go by that answer."""
        if not self.enabled or self.status in (DOWNLOADING, INSTALLING):
            return
        cached = state.get("update") or {}
        if force or time.time() - float(cached.get("checked", 0)) > CHECK_EVERY:
            try:
                release = latest()
            except Exception as e:  # offline or rate-limited: try again at the next check
                log(f"update: couldn't check: {e!r}")
                return
            state.put("update", {"checked": time.time(), "release": asdict(release) if release else None})
        else:
            try:
                release = Release(**cached["release"]) if cached.get("release") else None
            except TypeError:
                release = None
        if release and newer(release.version):
            if self.release != release:
                log(f"update: WinDroid {release.version} is available (this is {__version__})")
            self.release = release
            if self.status == IDLE:
                self._set(AVAILABLE)
        _clean_up()

    # ------------------------------------------------------------------ installing
    def install(self) -> None:
        """Download, verify and start the installer (on its own thread), then on_ready()."""
        with self._lock:
            if self.release is None or self.status in (DOWNLOADING, INSTALLING):
                return
            self.progress = 0.0
            self._set(DOWNLOADING)
        threading.Thread(target=self._install, args=(self.release,), daemon=True, name="update").start()

    def _install(self, release: Release) -> None:
        try:
            path = self._download(release)
            log(f"update: starting the installer for {release.version}")
            subprocess.Popen([str(path), *INSTALLER_ARGS], cwd=str(path.parent), close_fds=True,
                             creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        except Exception as e:
            log(f"update: failed: {e!r}")
            self._set(FAILED, f"The update didn't download: {e}" if not isinstance(e, ChecksumError) else str(e))
            return
        self._set(INSTALLING)
        self.on_ready()

    def _download(self, release: Release) -> Path:
        with _get(release.sha256) as r:
            expected = parse_sha256(r.read(4096).decode("utf-8", "replace"))
        UPDATES.mkdir(parents=True, exist_ok=True)
        path = UPDATES / f"WinDroid-Setup-{release.version}.exe"
        part = path.with_suffix(".part")
        h = hashlib.sha256()
        done = 0
        with _get(release.installer, timeout=60) as r, part.open("wb") as f:
            total = int(r.headers.get("Content-Length") or release.size or 0)
            last = 0.0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if total and time.monotonic() - last > 0.25:
                    last = time.monotonic()
                    self.progress = min(done / total, 1.0)
                    self.on_change()
        if h.hexdigest() != expected:
            part.unlink(missing_ok=True)
            raise ChecksumError("The downloaded update didn't match its published checksum, so it wasn't "
                                "installed. Try again later.")
        part.replace(path)
        self.progress = 1.0
        return path


class ChecksumError(Exception):
    pass


def _clean_up() -> None:
    """Installers of earlier updates (one may still be finishing: it's skipped, and goes next time)."""
    for f in UPDATES.glob("*") if UPDATES.is_dir() else []:
        try:
            f.unlink()
        except OSError:
            pass
