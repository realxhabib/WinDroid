"""See-through, click-through layers over an app window, for plugins (plugins/__init__.py).

A Layer is a top-level layered window with per-pixel alpha (UpdateLayeredWindow), owned by the app
window. Being owned keeps it above that window and only that window, hides it while the window is
minimized and destroys it with the window, all done by Windows. WS_EX_TRANSPARENT lets every click
through to the app below, and WS_EX_NOACTIVATE keeps the keyboard where it was.

Layers live on the window thread (WindowManager), like the app windows.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from typing import Callable

import win32api
import win32con
import win32gui
from PIL import Image, ImageChops

from .winapi import BITMAPINFOHEADER, gdi32, user32

CLASS = "AndroidDesktop.Layer"
_registered = False


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                       ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
ULW_ALPHA = 2


def premultiplied(image: Image.Image) -> Image.Image:
    """An "RGBA" image whose bytes are BGRA with premultiplied alpha, for Layer.update(premultiplied=True).
    Crop it like any image; don't draw on it."""
    return Image.frombytes("RGBA", image.size, premultiply(image))


def premultiply(image: Image.Image) -> bytes:
    """BGRA bytes with the color multiplied by alpha, as UpdateLayeredWindow wants them."""
    rgba = image.convert("RGBA")
    r, g, b, a = rgba.split()
    return Image.merge("RGBA", (ImageChops.multiply(b, a), ImageChops.multiply(g, a), ImageChops.multiply(r, a),
                                a)).tobytes()


_timers: dict[int, Callable[[], None]] = {}


def _proc(hwnd: int, msg: int, wp: int, lp: int) -> int:
    if msg == win32con.WM_NCHITTEST:
        return -1  # HTTRANSPARENT
    if msg == win32con.WM_MOUSEACTIVATE:
        return 3  # MA_NOACTIVATE
    if msg == win32con.WM_TIMER and hwnd in _timers:
        try:
            _timers[hwnd]()
        except Exception:
            pass
        return 0
    if msg == win32con.WM_DESTROY:
        _timers.pop(hwnd, None)
    return win32gui.DefWindowProc(hwnd, msg, wp, lp)


def _register() -> None:
    global _registered
    if not _registered:
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = CLASS
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpfnWndProc = _proc
        win32gui.RegisterClass(wc)
        _registered = True


class Layer:
    """One click-through picture above an app window, at screen coordinates."""

    def __init__(self, owner: int) -> None:
        _register()
        ex = (win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_TOOLWINDOW |
              win32con.WS_EX_NOACTIVATE)
        self.hwnd = win32gui.CreateWindowEx(ex, CLASS, "", win32con.WS_POPUP, 0, 0, 0, 0, owner, 0,
                                            win32api.GetModuleHandle(None), None)
        self.alpha = 255
        self.shown = False
        self.pos = (0, 0)
        self.size = (0, 0)

    def update(self, image: Image.Image | None, x: int, y: int, alpha: int | None = None,
               premultiplied: bool = False) -> None:
        """Show image (RGBA) with its top-left at screen (x, y); None hides the layer. premultiplied: the
        image's bytes are already BGRA with premultiplied alpha (see premultiplied()), which saves
        redoing that for every frame of a picture that only moves or gets cropped."""
        if not win32gui.IsWindow(self.hwnd):
            return
        if image is None or image.width <= 0 or image.height <= 0:
            self.hide()
            return
        if alpha is not None:
            self.alpha = max(0, min(255, alpha))
        w, h = image.size
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bits = ctypes.c_void_p()
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bitmap = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        try:
            if not bitmap:
                return
            data = image.tobytes() if premultiplied else premultiply(image)
            ctypes.memmove(bits, data, len(data))
            old = gdi32.SelectObject(mem, bitmap)
            blend = BLENDFUNCTION(0, 0, self.alpha, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
            user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wt.POINT(x, y)), ctypes.byref(wt.SIZE(w, h)),
                                       mem, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
            gdi32.SelectObject(mem, old)
        finally:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)
        self.pos, self.size = (x, y), (w, h)
        if not self.shown:
            win32gui.SetWindowPos(self.hwnd, 0, 0, 0, 0, 0, win32con.SWP_SHOWWINDOW | win32con.SWP_NOACTIVATE |
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER)
            self.shown = True

    def fade(self, alpha: int) -> None:
        """Change only the layer's overall opacity: cheap enough for every animation frame."""
        alpha = max(0, min(255, alpha))
        if alpha == self.alpha or not self.shown or not win32gui.IsWindow(self.hwnd):
            self.alpha = alpha
            return
        self.alpha = alpha
        blend = BLENDFUNCTION(0, 0, alpha, 1)
        user32.UpdateLayeredWindow(self.hwnd, None, None, None, None, None, 0, ctypes.byref(blend), ULW_ALPHA)

    def move(self, x: int, y: int) -> None:
        if self.shown and (x, y) != self.pos and win32gui.IsWindow(self.hwnd):
            win32gui.SetWindowPos(self.hwnd, 0, x, y, 0, 0, win32con.SWP_NOSIZE | win32con.SWP_NOZORDER |
                                  win32con.SWP_NOACTIVATE)
            self.pos = (x, y)

    def every(self, ms: int, fn: Callable[[], None] | None) -> None:
        """Call fn every ms milliseconds on the window thread (None stops it), e.g. for animation."""
        if not win32gui.IsWindow(self.hwnd):
            return
        if fn is None:
            _timers.pop(self.hwnd, None)
            user32.KillTimer(self.hwnd, 1)
        else:
            _timers[self.hwnd] = fn
            user32.SetTimer(self.hwnd, 1, ms, None)

    def hide(self) -> None:
        if self.shown and win32gui.IsWindow(self.hwnd):
            win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
        self.shown = False

    def destroy(self) -> None:
        if win32gui.IsWindow(self.hwnd):
            win32gui.DestroyWindow(self.hwnd)
        self.shown = False


def set_owner(hwnd: int, owner: int) -> None:
    """Make another top-level window (a WebView2 panel, say) owned by an app window: it then stays
    above it, hides while it's minimized and has no taskbar button of its own."""
    user32.SetWindowLongPtrW(hwnd, -8, owner)  # GWLP_HWNDPARENT
    ex = user32.GetWindowLongPtrW(hwnd, win32con.GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, win32con.GWL_EXSTYLE, (ex & ~win32con.WS_EX_APPWINDOW) | win32con.WS_EX_TOOLWINDOW)


def side_panel(frame: tuple[int, int, int, int], work: tuple[int, int, int, int], width: int, gap: int,
               maximized: bool) -> tuple[tuple[int, int, int, int], str]:
    """Where a panel glued to an app window's edge goes, as (left, top, right, bottom) and the side:
    right of the window if it fits on its monitor, else left, else over the window's right edge."""
    l, t, r, b = frame
    wl, wt_, wr, wb = work
    top, bottom = max(t, wt_), min(b, wb)
    if bottom - top < 120:
        top, bottom = t, b
    if not maximized:
        if r + gap + width <= wr:
            return (r + gap, top, r + gap + width, bottom), "right"
        if l - gap - width >= wl:
            return (l - gap - width, top, l - gap, bottom), "left"
    right = min(r, wr) - gap
    return (right - width, top, right, bottom), "inside"
