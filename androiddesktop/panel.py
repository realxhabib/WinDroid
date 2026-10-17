"""A plugin's side panel: a frameless WebView2 window (pywebview, like the launcher) glued to an app
window's edge and owned by it, so it stacks, minimizes and closes with it.

pywebview runs its windows on the agent's main thread; a Panel can be made, moved and talked to from
any thread. Placement goes through Win32 in physical pixels (asynchronously, so the window thread
never waits on WebView2's), as pywebview's own move() works in logical ones.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import webview
import win32con
import win32gui

from . import overlay, winapi
from .log import log

SWP_ASYNCWINDOWPOS = 0x4000


def inline(page: Path) -> str:
    """A page with its stylesheets and scripts (files beside it) folded in. pywebview serves local
    files from one folder, the launcher's, so a panel's page comes as HTML of its own."""
    html = page.read_text(encoding="utf-8")
    def read(m: re.Match) -> str:
        return (page.parent / m.group(1)).read_text(encoding="utf-8")

    html = re.sub(r'<link rel="stylesheet" href="([^"]+)">', lambda m: f"<style>\n{read(m)}\n</style>", html)
    return re.sub(r'<script src="([^"]+)"></script>', lambda m: f"<script>\n{read(m)}\n</script>", html)


class Panel:
    def __init__(self, owner: int, title: str, url: Path, api: Any, size: tuple[int, int], light: bool) -> None:
        self.owner = owner
        self.hwnd = 0
        self.loaded = threading.Event()
        self.closed = False
        self._rect: tuple[int, int, int, int] | None = None
        self._visible = False
        self._pending: dict[str, str] = {}  # scripts waiting for the page, latest per kind
        self._lock = threading.Lock()
        self.window = webview.create_window(
            title, html=inline(url), js_api=api, width=size[0], height=size[1], frameless=True, easy_drag=False,
            resizable=False, hidden=True, background_color="#f3f5f9" if light else "#0b0d11")
        self.window.events.loaded += self._on_loaded
        self.window.events.closed += self._on_closed
        threading.Thread(target=self._adopt, daemon=True, name=f"panel-{title}").start()

    def _adopt(self) -> None:
        """Once WinForms has made the window: tie it to the app window and put it in place."""
        for _ in range(200):
            try:
                self.hwnd = int(self.window.native.Handle.ToInt64())
                break
            except Exception:
                threading.Event().wait(0.05)
        if not self.hwnd:
            log(f"panel {self.window.title}: no window")
            return
        overlay.set_owner(self.hwnd, self.owner)
        winapi.dwm_int(self.hwnd, winapi.DWMWA_WINDOW_CORNER_PREFERENCE, winapi.DWMWCP_ROUND)
        self._apply()

    def _on_loaded(self) -> None:
        self.loaded.set()
        with self._lock:
            pending, self._pending = list(self._pending.values()), {}
        for script in pending:
            self._eval(script)

    def _on_closed(self) -> None:
        self.closed = True

    def _eval(self, script: str) -> None:
        try:
            self.window.evaluate_js(script)
        except Exception as e:
            log(f"panel {self.window.title}: {e!r}")

    def call(self, fn: str, data: Any, kind: str | None = None) -> None:
        """Run window.<fn>(data) in the page, once it's loaded. Calls of the same kind made before
        that collapse to the last one."""
        if self.closed:
            return
        script = f"window.{fn.replace('.', '?.')}?.({json.dumps(data)})"
        with self._lock:
            if not self.loaded.is_set():
                self._pending[kind or fn] = script
                return
        threading.Thread(target=self._eval, args=(script,), daemon=True).start()

    def place(self, rect: tuple[int, int, int, int] | None, visible: bool) -> None:
        """Put the panel at rect (screen pixels: left, top, right, bottom), shown or hidden."""
        self._rect, self._visible = rect, visible and rect is not None
        self._apply()

    def _apply(self) -> None:
        if not self.hwnd or self.closed or not win32gui.IsWindow(self.hwnd):
            return
        # Asynchronous both: WinForms' thread may be busy, and the window thread mustn't wait on it.
        shown = bool(win32gui.IsWindowVisible(self.hwnd))
        if self._visible and self._rect:
            l, t, r, b = self._rect
            flags = win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER | SWP_ASYNCWINDOWPOS
            winapi.user32.SetWindowPos(self.hwnd, 0, l, t, r - l, b - t, flags)
            # The WebView fills the window. WinForms docks it so only when it shows the window itself,
            # which a panel never asks of it (it would take the focus): left alone it can sit off to
            # one side, so the panel shows nothing but its background.
            child = win32gui.GetWindow(self.hwnd, win32con.GW_CHILD)
            if child:
                winapi.user32.SetWindowPos(child, 0, 0, 0, r - l, b - t, flags)
            if not shown:
                winapi.user32.ShowWindowAsync(self.hwnd, win32con.SW_SHOWNOACTIVATE)
        elif shown:
            winapi.user32.ShowWindowAsync(self.hwnd, win32con.SW_HIDE)

    def foreground(self) -> bool:
        return bool(self.hwnd) and win32gui.GetForegroundWindow() == self.hwnd

    def destroy(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.window.destroy()
            except Exception:
                pass
