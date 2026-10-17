"""The Android Desktop Camera: a Windows virtual camera that shows Android the webcam's whole picture.

Android's camera driver in Google's emulator image presents every camera as a phone's: a 1280x960
sensor mounted at 90 degrees. The emulator turns each webcam frame the way a phone held upright would
and keeps only a 720x960 strip from its left side, which Android turns back upright: a 16:9 webcam
shows Android a tall slice of its picture. This camera (helper/camera, built into
assets/AndroidDesktopCamera.dll by tools/build_camera.py) offers exactly 1280x960 and draws the PC's
first physical webcam into that strip, letterboxed, so Android shows the whole picture, upright.

It's a Windows 11 virtual camera: a media source that Windows' Frame Server service loads. Installing
it takes an administrator, once: the DLL goes to Program Files, where the service can read it, and is
registered for every user of the PC. It reads the webcam only while something streams from it.
runtime.pick_cameras() gives it to Android whenever it's there.

    python -m androiddesktop camera                 # is it installed?
    python -m androiddesktop camera install         # asks Windows for administrator rights
    python -m androiddesktop camera uninstall
    python -m androiddesktop camera test on|off     # a test pattern instead of the webcam
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import winreg
from pathlib import Path

from .log import log

NAME = "Android Desktop Camera"  # FRIENDLY_NAME in AndroidDesktopCamera.cpp
CLSID = "{509ADBEB-2D1F-421D-A0CD-5159F879304A}"
DLL = Path(__file__).with_name("assets") / "AndroidDesktopCamera.dll"
INSTALL_DIR = Path(os.environ.get("ProgramFiles") or r"C:\Program Files") / "Android Desktop"
TEST_FLAG = Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "AndroidDesktop" / "camera-test-pattern"
MOVEFILE_DELAY_UNTIL_REBOOT = 4


def is_ours(name: str) -> bool:
    """The camera's name as Windows lists it: Windows adds " (Windows Virtual Camera)"."""
    return name == NAME or name.startswith(NAME + " (")


def install_name(data: bytes) -> str:
    """Each build gets a file of its own, so an update never overwrites the one Frame Server may still
    have loaded."""
    return f"AndroidDesktopCamera-{hashlib.sha256(data).hexdigest()[:12]}.dll"


def installed_dll() -> Path | None:
    """The registered camera DLL, when the camera is installed."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Classes\CLSID\{CLSID}\InprocServer32") as key:
            path = Path(winreg.QueryValue(key, None))
    except OSError:
        return None
    return path if path.is_file() else None


def up_to_date() -> bool:
    installed = installed_dll()
    return bool(installed and installed.name == install_name(DLL.read_bytes()))


def _call(path: Path, export: str) -> None:
    lib = ctypes.WinDLL(str(path))
    try:
        hr = getattr(lib, export)()
    finally:
        ctypes.windll.kernel32.FreeLibrary(ctypes.c_void_p(lib._handle))
    if hr < 0:
        raise OSError(f"{export} failed (0x{hr & 0xFFFFFFFF:08X})")


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # still loaded by Frame Server: gone at the next restart
        ctypes.windll.kernel32.MoveFileExW(str(path), None, MOVEFILE_DELAY_UNTIL_REBOOT)


def install() -> None:
    """Copy the DLL to Program Files and register it, which creates the camera. Administrator only."""
    data = DLL.read_bytes()
    target = INSTALL_DIR / install_name(data)
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(data)
    _call(target, "DllRegisterServer")
    for old in INSTALL_DIR.glob("AndroidDesktopCamera-*.dll"):
        if old != target:
            _remove(old)


def uninstall() -> None:
    """Remove the camera and unregister and delete the DLL. Administrator only."""
    installed = installed_dll()
    if installed:
        _call(installed, "DllUnregisterServer")
    for old in INSTALL_DIR.glob("AndroidDesktopCamera-*.dll"):
        _remove(old)
    try:
        INSTALL_DIR.rmdir()
    except OSError:
        pass


def test_pattern(on: bool) -> None:
    """The camera reads this when a stream starts (it can't see the user's files or registry)."""
    if on:
        TEST_FLAG.parent.mkdir(parents=True, exist_ok=True)
        TEST_FLAG.touch()
    else:
        TEST_FLAG.unlink(missing_ok=True)


def _as_admin(command: str) -> int:
    """Run `camera <command>` as administrator (Windows asks the user) and wait: its exit code."""
    if ctypes.windll.shell32.IsUserAnAdmin():
        return main([command, "--elevated"])
    import pywintypes
    import win32con
    import win32event
    import win32process
    from win32com.shell import shell, shellcon
    try:
        info = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, lpVerb="runas", lpFile=sys.executable,
                                    lpParameters=f"-m androiddesktop camera {command} --elevated",
                                    nShow=win32con.SW_HIDE)
    except pywintypes.error:  # the user said no
        return 2
    win32event.WaitForSingleObject(info["hProcess"], win32event.INFINITE)
    return win32process.GetExitCodeProcess(info["hProcess"])


def main(args: list[str]) -> int:
    command = args[0] if args else "status"
    if "--elevated" in args:  # the administrator half of install/uninstall
        try:
            install() if command == "install" else uninstall()
            log(f"camera: {command}ed {installed_dll() or ''}".rstrip())
            return 0
        except OSError as e:
            log(f"camera: {command} failed: {e}")
            return 1
    if command in ("install", "uninstall"):
        code = _as_admin(command)
        if code == 0:
            print("Installed: Android uses the camera from its next start." if command == "install" else
                  "Removed: Android uses the webcam itself again from its next start.")
        elif code == 2:
            print("Canceled: Windows didn't get administrator rights.")
        else:
            print(f"That didn't work (exit code {code}); see androiddesktop.log.")
        return code
    if command == "test" and len(args) > 1 and args[1] in ("on", "off"):
        test_pattern(args[1] == "on")
        print(f"Test pattern {args[1]}: from the next time an app opens the camera.")
        return 0
    installed = installed_dll()
    print(f"{NAME}: " + ("not installed (python -m androiddesktop camera install)" if not installed else
                         f"installed, {installed}" + ("" if up_to_date() else
                                                       " (outdated: run camera install again)")))
    if TEST_FLAG.exists():
        print("Showing the test pattern (python -m androiddesktop camera test off).")
    return 0
