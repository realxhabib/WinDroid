"""Small Win32 helpers: DPI, DWM styling, taskbar identity, icons, drawing PIL images into a DC, and
the command line that starts WinDroid itself."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

import pythoncom
import win32con
import win32gui
from PIL import Image, ImageFont
from win32com.propsys import propsys, pscon

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi
shell32 = ctypes.windll.shell32

user32.GetDpiForWindow.argtypes = [wt.HWND]
user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, ctypes.c_uint]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.SetParent.argtypes = [wt.HWND, wt.HWND]
user32.SetParent.restype = wt.HWND
gdi32.SetDIBitsToDevice.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD, wt.DWORD, ctypes.c_int,
                                    ctypes.c_int, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWCP_ROUND = 2


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD), ("hwndTrack", wt.HWND), ("dwHoverTime", wt.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", wt.POINT), ("ptMaxSize", wt.POINT), ("ptMaxPosition", wt.POINT),
                ("ptMinTrackSize", wt.POINT), ("ptMaxTrackSize", wt.POINT)]


def dpi_aware() -> None:
    """Per-monitor v2, so windows stay sharp and sized right on every monitor."""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def dpi_of(hwnd: int) -> int:
    return user32.GetDpiForWindow(hwnd) or 96


def frame_thickness(dpi: int) -> int:
    """How far a maximized window hangs over the monitor edge (SM_CXFRAME + SM_CXPADDEDBORDER)."""
    return user32.GetSystemMetricsForDpi(32, dpi) + user32.GetSystemMetricsForDpi(92, dpi)


def dwm_int(hwnd: int, attr: int, value: int) -> None:
    v = ctypes.c_int(value)
    dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))


def colorref(rgb: tuple[int, int, int]) -> int:
    return rgb[0] | (rgb[1] << 8) | (rgb[2] << 16)


def track_leave(hwnd: int, nonclient: bool = False) -> None:
    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), 0x2 | (0x10 if nonclient else 0), hwnd, 0)
    user32.TrackMouseEvent(ctypes.byref(tme))


def blit(hdc: int, image: Image.Image, x: int = 0, y: int = 0) -> None:
    """Draw a PIL image into a device context."""
    w, h = image.size
    bits = image.convert("RGBA").tobytes("raw", "BGRA")
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    gdi32.SetDIBitsToDevice(hdc, x, y, w, h, 0, 0, 0, h, bits, ctypes.byref(bmi), 0)


def capture(hwnd: int, width: int, height: int) -> Image.Image | None:
    """PrintWindow the top-left width x height of a window (works for GPU-rendered windows too)."""
    if width <= 0 or height <= 0:
        return None
    hdc = gdi32.CreateCompatibleDC(None)
    bits = ctypes.c_void_p()
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
    hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    if not hbm:
        gdi32.DeleteDC(hdc)
        return None
    old = gdi32.SelectObject(hdc, hbm)
    try:
        if not user32.PrintWindow(hwnd, hdc, 2):  # PW_RENDERFULLCONTENT
            return None
        raw = ctypes.string_at(bits, width * height * 4)
        return Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(hbm)
        gdi32.DeleteDC(hdc)


def load_icon(path: Path, size: int) -> int:
    try:
        return win32gui.LoadImage(0, str(path), win32con.IMAGE_ICON, size, size, win32con.LR_LOADFROMFILE)
    except Exception:
        return 0


def set_window_icon(hwnd: int, ico: Path, dpi: int) -> None:
    if not ico.exists():
        return
    small = load_icon(ico, user32.GetSystemMetricsForDpi(49, dpi))  # SM_CXSMICON
    big = load_icon(ico, user32.GetSystemMetricsForDpi(11, dpi))  # SM_CXICON
    if small:
        win32gui.SendMessage(hwnd, win32con.WM_SETICON, win32con.ICON_SMALL, small)
    if big:
        win32gui.SendMessage(hwnd, win32con.WM_SETICON, win32con.ICON_BIG, big)


FROZEN = getattr(sys, "frozen", False)  # the installed WinDroid (built by PyInstaller), not a source checkout
GUI_EXE = "WinDroid.exe"  # the installed program; windroid-cli.exe beside it is the same with a console


def command(*args: str) -> list[str]:
    """The command line that runs WinDroid with these arguments, with no console window: the installed
    WinDroid.exe, or pythonw -m androiddesktop in a source checkout."""
    exe = Path(sys.executable)
    if FROZEN:
        return [str(exe.with_name(GUI_EXE)), *args]
    w = exe.with_name("pythonw.exe")
    return [str(w if w.exists() else exe), "-m", "androiddesktop", *args]


def console_exe() -> bool:
    """Running as the installed windroid-cli.exe, which has a console (WinDroid.exe has none)."""
    return FROZEN and Path(sys.executable).name.lower() != GUI_EXE.lower()


def set_app_identity(hwnd: int, app_id: str, name: str, relaunch: list[str]) -> None:
    """Give a window its own taskbar button (grouping, pin-to-taskbar relaunch with these arguments). The
    button shows the window's own icon (set_window_icon). No RelaunchIconResource: Windows 11's taskbar
    draws a blank page for one that points into %LOCALAPPDATA% (only %TEMP% and system files worked when
    tested), and with none set it uses the window's icon."""
    try:
        store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(app_id, pythoncom.VT_LPWSTR))
        store.SetValue(pscon.PKEY_AppUserModel_RelaunchCommand,
                       propsys.PROPVARIANTType(subprocess.list2cmdline(command(*relaunch)), pythoncom.VT_LPWSTR))
        store.SetValue(pscon.PKEY_AppUserModel_RelaunchDisplayNameResource,
                       propsys.PROPVARIANTType(name, pythoncom.VT_LPWSTR))
        store.Commit()
    except Exception:
        pass


def set_process_app_id(app_id: str) -> None:
    shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))


# ------------------------------------------------------------------ fonts
_FONT_DIR = Path(r"C:\Windows\Fonts")
_fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(kind: str, px: int) -> ImageFont.FreeTypeFont:
    """kind: 'text' (Segoe UI Variable) or 'icons' (Segoe Fluent Icons, falling back to MDL2)."""
    key = (kind, px)
    if key not in _fonts:
        names = ["SegUIVar.ttf", "segoeui.ttf"] if kind == "text" else ["SegoeIcons.ttf", "segmdl2.ttf"]
        for name in names:
            try:
                _fonts[key] = ImageFont.truetype(str(_FONT_DIR / name), px)
                break
            except OSError:
                continue
        else:
            _fonts[key] = ImageFont.load_default(px)
    return _fonts[key]


def light_theme() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "AppsUseLightTheme")[0])
    except OSError:
        return False


def copy_text(text: str) -> None:
    """Put text on the Windows clipboard."""
    import win32clipboard
    for _ in range(10):  # another program may have it open for a moment
        try:
            win32clipboard.OpenClipboard(0)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise OSError("the clipboard is busy")
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()
