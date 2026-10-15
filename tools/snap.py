"""Dev helper: capture one window (by exact title) with PrintWindow, so tests never grab other apps.

    python tools/snap.py "Clock" out.png
"""
import ctypes
import sys

import win32gui
import win32ui
from PIL import Image

ctypes.windll.user32.SetProcessDPIAware()


def snap(title: str, out: str) -> None:
    hwnd = win32gui.FindWindow(None, title)
    if not hwnd:
        sys.exit(f"no window titled {title!r}")
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    src = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(src)
    mem = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    mem.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
    info, bits = bmp.GetInfo(), bmp.GetBitmapBits(True)
    Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1).save(out)
    win32gui.DeleteObject(bmp.GetHandle())
    mem.DeleteDC(); mfc.DeleteDC(); win32gui.ReleaseDC(hwnd, src)
    print(f"{title}: {w}x{h} at ({l},{t}) -> {out}")


if __name__ == "__main__":
    snap(sys.argv[1], sys.argv[2])
