"""Start menu and desktop shortcuts. Each Android app's shortcut carries the same AppUserModelID as
its window, so Windows treats the shortcut, the running window and a taskbar pin as one app.

Shortcuts run winapi.command(): the installed WinDroid.exe, or pythonw from a source checkout. The
installed WinDroid's own Start menu entry is the installer's; a source checkout makes its own."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pythoncom
from win32com.propsys import propsys, pscon
from win32com.shell import shell, shellcon

from . import winapi
from .catalog import App

PROGRAMS = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
APPS_FOLDER = PROGRAMS / "Android apps"
MAIN_ICON = Path(__file__).with_name("assets") / "icon.ico"
LAUNCHER_ID = "AndroidDesktop.Launcher"  # the app's first name; kept so taskbar pins stay pinned
LAUNCHER = PROGRAMS / "WinDroid.lnk"  # the installer makes the same one
OLD_LAUNCHER = PROGRAMS / "Android Desktop.lnk"  # a source checkout's, before the app was WinDroid
APP_ID_PREFIX = "AndroidDesktop.App."


def app_id(package: str) -> str:
    return f"{APP_ID_PREFIX}{package}"[:128]


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip() or "App"


def _desktop() -> Path:
    return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, 0, 0))


def create(lnk: Path, args: list[str], icon: Path, aumid: str, description: str) -> None:
    """A shortcut that runs WinDroid with these arguments."""
    pythoncom.CoInitialize()
    cmd = winapi.command(*args)
    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.SetPath(cmd[0])
    link.SetArguments(subprocess.list2cmdline(cmd[1:]))
    link.SetWorkingDirectory(str(Path.home()))
    link.SetDescription(description)
    if icon.exists():
        link.SetIconLocation(str(icon), 0)
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(aumid, pythoncom.VT_LPWSTR))
    store.Commit()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(lnk), 0)


def start_path(app: App) -> Path:
    return APPS_FOLDER / f"{_safe(app.label)}.lnk"


def in_start(app: App) -> bool:
    return start_path(app).exists()


def toggle_start(app: App) -> bool:
    """Returns True if the app is now in the Start menu."""
    path = start_path(app)
    if path.exists():
        path.unlink()
        if APPS_FOLDER.exists() and not any(APPS_FOLDER.iterdir()):
            APPS_FOLDER.rmdir()
        return False
    create(path, ["open", app.package], app.ico, app_id(app.package), f"{app.label} (Android)")
    return True


def desktop_shortcut(app: App) -> Path:
    path = _desktop() / f"{_safe(app.label)}.lnk"
    create(path, ["open", app.package], app.ico, app_id(app.package), f"{app.label} (Android)")
    return path


def ensure_launcher_shortcut() -> None:
    """A source checkout puts the launcher in the Start menu. The installed WinDroid's installer does
    that (and its uninstaller takes it away), so then there's nothing to do."""
    if winapi.FROZEN:
        return
    if OLD_LAUNCHER.exists():
        OLD_LAUNCHER.unlink()
    if not LAUNCHER.exists():
        create(LAUNCHER, [], MAIN_ICON, LAUNCHER_ID, "Android apps in their own windows")


def _target(lnk: Path) -> tuple[str, str]:
    """A shortcut's program and AppUserModelID."""
    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(lnk), 0)
    program = link.GetPath(shell.SLGP_RAWPATH)[0]
    aumid = link.QueryInterface(propsys.IID_IPropertyStore).GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
    return program, aumid or ""


def remove_ours() -> list[Path]:
    """For the uninstaller: delete the app shortcuts this WinDroid made, in the Start menu and on the
    desktop (an Android app's AppUserModelID, running this program). Returns what was deleted."""
    pythoncom.CoInitialize()
    program = os.path.normcase(winapi.command()[0])
    removed = []
    for folder in (APPS_FOLDER, _desktop()):
        for lnk in folder.glob("*.lnk") if folder.is_dir() else []:
            try:
                target, aumid = _target(lnk)
                if aumid.startswith(APP_ID_PREFIX) and os.path.normcase(target) == program:
                    lnk.unlink()
                    removed.append(lnk)
            except Exception:  # not a shortcut we can read: not ours to touch
                continue
    if APPS_FOLDER.is_dir() and not any(APPS_FOLDER.iterdir()):
        APPS_FOLDER.rmdir()
    return removed
