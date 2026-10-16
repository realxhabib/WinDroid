"""The apps installed in Android, with real labels and icons.

assets/appcatalog.jar (built from helper/src by tools/build_helper.py) runs inside Android through
app_process and asks PackageManager directly, so icons look exactly as Android draws them.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

from . import adb
from .paths import DATA, ICONS, INSTANCE

HELPER = Path(__file__).with_name("assets") / "appcatalog.jar"
DEVICE_HELPER = "/data/local/tmp/appcatalog.jar"
DEVICE_ICONS = "/data/local/tmp/androiddesktop-icons"
ICON_SIZE = 256
CACHE = DATA / f"apps{INSTANCE}.json"

# Phone plumbing that makes no sense on a PC.
HIDDEN = {"com.android.stk", "com.google.android.apps.safetyhub"}


@dataclass
class App:
    package: str
    activity: str
    label: str
    system: bool
    updated: int

    @property
    def icon(self) -> Path:
        return ICONS / f"{self.package}.png"

    @property
    def ico(self) -> Path:
        """Multi-size .ico for the taskbar, Alt+Tab and Start menu shortcuts."""
        path = ICONS / f"{self.package}.ico"
        if not path.exists() and self.icon.exists():
            Image.open(self.icon).convert("RGBA").save(path, sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 256)])
        return path


def parse_list(output: str) -> dict[str, App]:
    """The helper's `list` output: one app per line, "package, activity, system (1/0), last update, label"
    separated by tabs."""
    apps: dict[str, App] = {}
    for line in output.splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) < 5 or parts[0] in HIDDEN:
            continue
        pkg, activity, system, updated, label = parts[0], parts[1], parts[2] == "1", int(parts[3] or 0), parts[4]
        apps[pkg] = App(pkg, activity, label, system, updated)
    return apps


class Catalog:
    def __init__(self) -> None:
        self.apps: dict[str, App] = {}
        self._lock = threading.Lock()
        self.listeners: list = []
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            for item in json.loads(CACHE.read_text(encoding="utf-8")):
                self.apps[item["package"]] = App(**item)
        except (OSError, ValueError, TypeError, KeyError):
            self.apps = {}

    def _save_cache(self) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps([asdict(a) for a in self.apps.values()], indent=1), encoding="utf-8")

    def sorted(self) -> list[App]:
        return sorted(self.apps.values(), key=lambda a: a.label.casefold())

    def get(self, package: str) -> App | None:
        return self.apps.get(package)

    # ------------------------------------------------------------------ refresh from the device
    def _helper(self, *args: str, timeout: float = 60) -> str:
        return adb.shell(f"CLASSPATH={DEVICE_HELPER} app_process / AppCatalog " + " ".join(args), timeout=timeout)

    def refresh(self) -> bool:
        """Re-read the app list; render icons only for new or updated apps. Returns True if anything changed."""
        with self._lock:
            if adb.user_locked() is not False:
                # Locked (or can't tell): Android only lists its few "direct boot" apps until the PIN is
                # entered. Don't mistake that for the user's apps being gone.
                return False
            adb.run("push", str(HELPER), DEVICE_HELPER, timeout=30)
            fresh = parse_list(self._helper("list"))
            if not fresh:
                return False  # the helper failed; keep what we had
            stale = [a.package for a in fresh.values()
                     if not a.icon.exists() or self.apps.get(a.package, None) is None
                     or self.apps[a.package].updated != a.updated]
            if stale:
                self._render_icons(stale)
            changed = set(fresh) != set(self.apps) or bool(stale) or any(
                self.apps[p].label != fresh[p].label for p in fresh if p in self.apps)
            for gone in set(self.apps) - set(fresh):
                for f in (ICONS / f"{gone}.png", ICONS / f"{gone}.ico"):
                    f.unlink(missing_ok=True)
            self.apps = fresh
            self._save_cache()
        if changed:
            for fn in list(self.listeners):
                try:
                    fn()
                except Exception:
                    pass
        return changed

    def _render_icons(self, packages: list[str]) -> None:
        ICONS.mkdir(parents=True, exist_ok=True)
        adb.shell(f"rm -rf {DEVICE_ICONS}")
        for i in range(0, len(packages), 40):  # keep the command line short
            self._helper("icons", DEVICE_ICONS, str(ICON_SIZE), *packages[i:i + 40], timeout=120)
        adb.run("pull", f"{DEVICE_ICONS}/.", str(ICONS), timeout=120)
        for pkg in packages:
            (ICONS / f"{pkg}.ico").unlink(missing_ok=True)  # rebuilt from the new png on demand
        adb.shell(f"rm -rf {DEVICE_ICONS}")
