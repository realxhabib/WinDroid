"""Start menu and desktop shortcuts. Each Android app's shortcut carries the same AppUserModelID as
its window, so Windows treats the shortcut, the running window and a taskbar pin as one app."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pythoncom
from win32com.propsys import propsys, pscon
from win32com.shell import shell, shellcon

from .catalog import App
from .winapi import pythonw

PROGRAMS = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
APPS_FOLDER = PROGRAMS / "Android apps"
MAIN_ICON = Path(__file__).with_name("assets") / "icon.ico"
LAUNCHER_ID = "AndroidDesktop.Launcher"


def app_id(package: str) -> str:
    return f"AndroidDesktop.App.{package}"[:128]


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip() or "App"


def _desktop() -> Path:
    return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, 0, 0))


def create(lnk: Path, args: str, icon: Path, aumid: str, description: str) -> None:
    pythoncom.CoInitialize()
    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.SetPath(pythonw())
    link.SetArguments(args)
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
    create(path, f"-m androiddesktop open {app.package}", app.ico, app_id(app.package), f"{app.label} (Android)")
    return True


def desktop_shortcut(app: App) -> Path:
    path = _desktop() / f"{_safe(app.label)}.lnk"
    create(path, f"-m androiddesktop open {app.package}", app.ico, app_id(app.package), f"{app.label} (Android)")
    return path


def ensure_launcher_shortcut() -> None:
    path = PROGRAMS / "Android Desktop.lnk"
    if not path.exists():
        create(path, "-m androiddesktop", MAIN_ICON, LAUNCHER_ID, "Android apps in their own windows")
