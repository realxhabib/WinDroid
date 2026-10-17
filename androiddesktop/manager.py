"""Owns every app window on one GUI thread, plus the tray icon and the single-instance IPC window.

Other processes (Start menu shortcuts, taskbar pins) find the hidden agent window by its class name
and send it a WM_COPYDATA request, e.g. {"open": "com.android.chrome"}.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import queue
import re
import threading
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import win32api
import win32con
import win32gui

from . import adb, winapi
from .appwindow import FAILED, RUNNING as WINDOW_RUNNING, AppWindow
from .log import log
from .catalog import Catalog
from .paths import INSTANCE
from .plugins import WM_HOTKEY, PluginHost
from .protected import SecureScreens
from .vm import LOCKED, RUNNING, VM

if TYPE_CHECKING:
    from .remote.hub import Hub

AGENT_CLASS = "AndroidDesktop.Agent" + INSTANCE
APP_CLASS = "AndroidDesktop.AppWindow"
WM_APP_CALL = win32con.WM_APP + 20
WM_APP_TRAY = win32con.WM_APP + 21
TRAY_OPEN, TRAY_STOP, TRAY_QUIT, TRAY_SETTINGS = 1001, 1002, 1003, 1004
APP_ICON = Path(__file__).with_name("assets") / "icon.ico"


def live_displays(stack_list: str) -> set[int] | None:
    """The displays that have an app on them, from `cmd activity stack list`. None if that wasn't a real
    answer (adb error, Android going down): Android's home screen always sits on display 0."""
    displays = {int(x) for x in re.findall(r"displayId=(\d+)", stack_list)}
    return displays if 0 in displays else None


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wt.DWORD), ("lpData", ctypes.c_void_p)]


def agent_running() -> bool:
    return bool(win32gui.FindWindow(AGENT_CLASS, None))


def send_to_agent(request: dict) -> bool:
    """From another process: hand a request to the running agent. False if none is running."""
    hwnd = win32gui.FindWindow(AGENT_CLASS, None)
    if not hwnd:
        return False
    ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: let the agent bring its window forward
    data = json.dumps(request).encode("utf-8")
    buf = ctypes.create_string_buffer(data)
    cds = COPYDATASTRUCT(1, len(data), ctypes.cast(buf, ctypes.c_void_p))
    # 0 means the agent is shutting down and won't take new work.
    return bool(ctypes.windll.user32.SendMessageW(hwnd, win32con.WM_COPYDATA, 0, ctypes.byref(cds)))


class WindowManager:
    def __init__(self, vm: VM, catalog: Catalog, ipc: bool = True) -> None:
        """ipc=False: a test harness's manager. It takes no requests from other processes and has no tray
        icon, so it never competes with a running agent."""
        self.vm = vm
        self.ipc = ipc
        self.catalog = catalog
        self.windows: dict[str, AppWindow] = {}
        self.by_hwnd: dict[int, AppWindow] = {}
        self.creating: AppWindow | None = None
        self.window_class = APP_CLASS
        self.agent_hwnd = 0
        self.on_request: Callable[[dict], None] = lambda r: None
        self.accepting = True  # False once the agent starts quitting
        self.on_tray: Callable[[int], None] = lambda cmd: None
        self.on_notice: Callable[[str], None] = lambda text: log(text)  # something the user should hear about
        self.on_unlock: Callable[[AppWindow], None] = lambda win: None  # a window's "Unlock" button
        self.remote: Hub | None = None  # apps on iPhones and iPads (remote/hub.py), when the agent has it
        self.on_window_action: Callable[[AppWindow, str], None] = lambda win, action: None  # its "..." menu
        self.listeners: list[Callable[[], None]] = []
        self._calls: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._empty = threading.Event()
        self._empty.set()
        self._reported: set[tuple[int, str]] = set()
        self.plugins = PluginHost(self)

    # ------------------------------------------------------------------ thread plumbing
    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="windows").start()
        self._ready.wait()
        threading.Thread(target=self._watch_tasks, daemon=True, name="task-watch").start()
        SecureScreens(self).start()

    def call(self, fn: Callable[[], None]) -> None:
        """Run fn on the GUI thread."""
        self._calls.put(fn)
        win32gui.PostMessage(self.agent_hwnd, WM_APP_CALL, 0, 0)

    def _run(self) -> None:
        hinst = win32api.GetModuleHandle(None)
        app = win32gui.WNDCLASS()
        app.lpszClassName = APP_CLASS
        app.hInstance = hinst
        app.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW | win32con.CS_DBLCLKS
        app.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        app.lpfnWndProc = self._app_proc
        win32gui.RegisterClass(app)
        agent = win32gui.WNDCLASS()
        agent.lpszClassName = AGENT_CLASS if self.ipc else AGENT_CLASS + ".Harness"
        agent.hInstance = hinst
        agent.lpfnWndProc = self._agent_proc
        win32gui.RegisterClass(agent)
        self.agent_hwnd = win32gui.CreateWindowEx(0, agent.lpszClassName, "WinDroid", win32con.WS_POPUP,
                                                  0, 0, 0, 0, 0, 0, hinst, None)
        if self.ipc:
            self._add_tray()
        self._ready.set()
        win32gui.PumpMessages()

    def _app_proc(self, hwnd: int, msg: int, wp: int, lp: int) -> int:
        win = self.by_hwnd.get(hwnd)
        if win is None and self.creating is not None:
            win = self.by_hwnd[hwnd] = self.creating
            win.hwnd = hwnd
        if win is None:
            return win32gui.DefWindowProc(hwnd, msg, wp, lp)
        try:
            return win.wndproc(hwnd, msg, wp, lp)
        except Exception as e:
            # Under pythonw nobody would see this. Log each kind once: WM_PAINT would repeat it 30x a second.
            if (msg, type(e).__name__) not in self._reported:
                self._reported.add((msg, type(e).__name__))
                log(f"{win.app.package}: message {msg:#06x} failed:\n{traceback.format_exc().rstrip()}")
            return win32gui.DefWindowProc(hwnd, msg, wp, lp)

    def _agent_proc(self, hwnd: int, msg: int, wp: int, lp: int) -> int:
        if msg == WM_APP_CALL:
            while not self._calls.empty():
                try:
                    self._calls.get_nowait()()
                except Exception:
                    log(f"window thread:\n{traceback.format_exc().rstrip()}")
            return 0
        if msg == win32con.WM_COPYDATA:
            if not self.accepting:
                return 0
            cds = ctypes.cast(lp, ctypes.POINTER(COPYDATASTRUCT)).contents
            try:
                request = json.loads(ctypes.string_at(cds.lpData, cds.cbData).decode("utf-8"))
                threading.Thread(target=self.on_request, args=(request,), daemon=True).start()
            except ValueError:
                pass
            return 1
        if msg == WM_HOTKEY:
            self.plugins.on_hotkey(wp)
            return 0
        if msg == WM_APP_TRAY:
            event = lp & 0xFFFF
            if event in (win32con.WM_LBUTTONUP, win32con.NIN_SELECT if hasattr(win32con, "NIN_SELECT") else 0x400):
                self.on_tray(TRAY_OPEN)
            elif event in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
                self._tray_menu()
            return 0
        if msg == win32con.WM_COMMAND:
            self.on_tray(wp & 0xFFFF)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wp, lp)

    # ------------------------------------------------------------------ tray
    def _add_tray(self) -> None:
        icon = winapi.load_icon(APP_ICON, 32) if APP_ICON.exists() else win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (self.agent_hwnd, 1, flags, WM_APP_TRAY, icon, "WinDroid"))

    def balloon(self, text: str, title: str = "WinDroid") -> None:
        """A Windows notification from the tray icon, for when the launcher isn't open."""
        if not self.ipc:
            return

        def show() -> None:
            icon = winapi.load_icon(APP_ICON, 32) if APP_ICON.exists() else 0
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, (self.agent_hwnd, 1, win32gui.NIF_INFO, WM_APP_TRAY,
                                                            icon, "WinDroid", text[:255], 10, title[:63],
                                                            win32gui.NIIF_INFO))
        self.call(show)

    def remove_tray(self) -> None:
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.agent_hwnd, 1))
        except Exception:
            pass

    def _tray_menu(self) -> None:
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, TRAY_OPEN, "Open WinDroid")
        win32gui.AppendMenu(menu, win32con.MF_STRING, TRAY_SETTINGS, "Settings")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        flags = win32con.MF_STRING | (0 if self.vm.state in (RUNNING, LOCKED) else win32con.MF_GRAYED)
        win32gui.AppendMenu(menu, flags, TRAY_STOP, "Shut down Android")
        win32gui.AppendMenu(menu, win32con.MF_STRING, TRAY_QUIT, "Quit")
        x, y = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(self.agent_hwnd)
        win32gui.TrackPopupMenu(menu, win32con.TPM_RIGHTBUTTON | win32con.TPM_BOTTOMALIGN, x, y, 0, self.agent_hwnd, None)
        win32gui.PostMessage(self.agent_hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)

    # ------------------------------------------------------------------ windows
    def open(self, package: str, intent: list[str] | None = None, adopt: int | None = None) -> None:
        """Open (or bring forward) an app's window; intent: optional `am start` arguments to run in it;
        adopt: a display the app is running on hidden (an AI agent's, ghost.py), to bring it in from."""
        self.call(lambda: self._open(package, intent, adopt))

    def _open(self, package: str, intent: list[str] | None, adopt: int | None = None) -> None:
        if package in self.windows and not self.windows[package].closing:
            win = self.windows[package]
            win.focus()
            if win.status == FAILED:
                win.retry()
            if intent:
                win.run_intent(intent)
            if adopt is not None:
                win.adopt(adopt)
            return
        app = self.catalog.get(package)
        if app is None:
            self.on_notice(f"Couldn't open {package}: it isn't in Android's app list.")
            return
        win = AppWindow(self, app, intent, adopt)
        self.windows[package] = win
        self._empty.clear()
        win.create()
        self.changed()

    def closed(self, win: AppWindow) -> None:
        self.by_hwnd.pop(win.hwnd, None)
        if self.windows.get(win.app.package) is win:
            del self.windows[win.app.package]
        if not self.windows:
            self._empty.set()
        self.changed()

    def close_all(self, timeout: float = 8) -> bool:
        for win in list(self.windows.values()):
            self.call(win.close)
        return self._empty.wait(timeout)

    def running(self) -> list[str]:
        return [p for p, w in self.windows.items() if not w.closing]

    def changed(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    # ------------------------------------------------------------------ app exit detection
    def _watch_tasks(self) -> None:
        """Close a window once its app has finished (e.g. Back on the app's first screen): its virtual
        display then has no task left and would only show the last frame."""
        misses: dict[int, int] = {}
        while True:
            time.sleep(1.5)
            # Only windows showing their app: one that's (re)connecting has no display to judge yet.
            wins = [w for w in list(self.windows.values())
                    if w.status == WINDOW_RUNNING and w.child and w.display_id is not None and not w.closing]
            misses = {d: n for d, n in misses.items() if d in {w.display_id for w in wins}}
            if not wins or self.vm.state != RUNNING:
                continue
            try:
                live = live_displays(adb.shell("cmd activity stack list", timeout=10))
            except Exception:
                continue
            if live is None:
                continue  # no verdict this round, rather than "every app exited"
            for w in wins:
                # While a secure screen is up, the app lives on protected.py's display instead.
                here = w.protected.display if w.protected and w.protected.display is not None else w.display_id
                if here in live or time.monotonic() - w.opened_at < 8:
                    misses[w.display_id] = 0
                    continue
                misses[w.display_id] = misses.get(w.display_id, 0) + 1
                if misses[w.display_id] >= 2:
                    self.call(w.close)
