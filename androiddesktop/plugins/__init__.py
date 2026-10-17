"""Plugins: companions that attach to a particular app's window.

A plugin names the Android packages it's for. When one of those apps shows in its window, the host
makes an instance for that window and hands it an AppHandle, its view of the window:

- geometry(): where the window is and where Android's display is drawn inside it, in screen pixels,
  with the DPI scale and whether it's showing. The host calls the plugin's geometry_changed() as the
  window moves, resizes, changes monitor (DPI), minimizes or comes back.
- screenshot(): the app's screen as the window draws it, captured on the PC: the frame the window
  shows on an emulator display (emuview.py), or PrintWindow of scrcpy's window. Android takes no part
  in it, so the app can't notice.
- keyboard() / click(): put text into the app, or click in it, through the window's own keyboard and
  mouse path: on an emulator display, the display's own text and key input and Android's clipboard
  (_DisplayKeyboard); in a scrcpy window, key messages posted to it (keys.py). drag() / fling(): move
  a finger on the app's screen, as a mouse drag in the window does.
- layer(): a click-through overlay above the window (overlay.py), and panel(): a WebView2 side panel
  glued to the window's edge. Both are owned by the window: they keep above it, hide with it while
  it's minimized and go away with it.

Hooks run on the window thread, so they must return quickly; slow work goes on threads of the
plugin's own, which come back with app.call(fn). Plugins are on unless state.json's "plugins" says
{"<name>": false}.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import importlib
import queue
import random
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

import win32api
import win32con
import win32gui
from PIL import Image

from .. import adb, emuview, keys, overlay, state, winapi
from ..log import log
from ..paths import ADB_EXE, SERIAL, android_env

if TYPE_CHECKING:
    from ..appwindow import AppWindow
    from ..manager import WindowManager

BUILTIN = ("androiddesktop.plugins.hinge",)  # modules with a PLUGIN class
DWMWA_EXTENDED_FRAME_BOUNDS = 9
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0
WM_HOTKEY = 0x0312

_WinEventProc = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)
winapi.user32.SetWinEventHook.restype = wt.HANDLE
winapi.user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, _WinEventProc, wt.DWORD, wt.DWORD, wt.DWORD]


@dataclass(frozen=True)
class Geometry:
    frame: tuple[int, int, int, int]  # the window as drawn (no invisible resize borders), screen pixels
    content: tuple[int, int, int, int]  # where the app's display is drawn
    work: tuple[int, int, int, int]  # the work area of the window's monitor
    scale: float  # DPI / 96
    visible: bool  # on screen and showing the app (not minimized, hidden or reconnecting)
    maximized: bool

    @property
    def size(self) -> tuple[int, int]:
        l, t, r, b = self.content
        return r - l, b - t

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """A point on the app's screen (screenshot pixels) in screen coordinates."""
        return round(self.content[0] + x), round(self.content[1] + y)


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r),
                                                  ctypes.sizeof(r)) == 0:
        return r.left, r.top, r.right, r.bottom
    return win32gui.GetWindowRect(hwnd)


class Shell:
    """One `adb shell` kept open, for commands that come often (a scroll per step of a scan): about
    25 ms each, where a fresh `adb shell` takes a quarter of a second."""
    DONE = "__plugin_done__"

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()

    def _start(self) -> None:
        self.proc = subprocess.Popen([str(ADB_EXE), "-s", SERIAL, "shell"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                     errors="replace", env=android_env(), creationflags=adb.CREATE_NO_WINDOW)
        proc = self.proc
        lines = self.lines = queue.Queue()

        def read() -> None:
            for line in proc.stdout:
                lines.put(line)
        threading.Thread(target=read, daemon=True, name="plugin-shell").start()

    def run(self, command: str, timeout: float = 5) -> str:
        """Run a command; its output. Raises if the shell doesn't answer in time."""
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            self.proc.stdin.write(f"{command}; echo {self.DONE}\n")
            self.proc.stdin.flush()
            out = []
            while True:
                try:
                    line = self.lines.get(timeout=timeout)
                except queue.Empty:
                    self.close()
                    raise TimeoutError(f"adb shell: no answer to {command!r}")
                if line.strip() == self.DONE:
                    return "".join(out)
                out.append(line)

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


class AppHandle:
    """A plugin's view of one app window. Methods say which thread they're for."""

    def __init__(self, host: PluginHost, win: AppWindow) -> None:
        self._host = host
        self._win = win
        self.package = win.app.package
        self.label = win.app.label
        self.hwnd = win.hwnd
        self._shell = Shell()

    # ------------------------------------------------------------------ any thread
    @property
    def display_id(self) -> int | None:
        return self._win.display_id

    @property
    def view(self) -> int:
        """The child window showing the app: emuview's or scrcpy's, or protected.py's during a secure screen."""
        return self._win.view

    def _surface(self) -> emuview.Surface | None:
        """The app's emulator display, when the window shows one rather than scrcpy's window."""
        surface = self._win.surface
        return surface if surface is not None and not surface.closed and surface.view == self._win.child else None

    @property
    def protected(self) -> bool:
        return self._win.protected is not None

    def showing(self) -> bool:
        w = self._win
        return bool(w.child and not w.closing and win32gui.IsWindow(w.hwnd) and w.view and win32gui.IsWindow(w.view))

    def geometry(self) -> Geometry:
        w = self._win
        frame = _rect(w.hwnd) if win32gui.IsWindow(w.hwnd) else (0, 0, 0, 0)
        view = w.view if self.showing() else 0
        if view:
            content = win32gui.GetWindowRect(view)
        else:
            l, t, r, b = frame
            content = (l, t + w.bar, r, b)
        try:
            mon = win32api.MonitorFromWindow(w.hwnd, win32con.MONITOR_DEFAULTTONEAREST)
            work = tuple(win32api.GetMonitorInfo(mon)["Work"])
        except Exception:
            work = frame
        visible = bool(view and win32gui.IsWindowVisible(w.hwnd) and not win32gui.IsIconic(w.hwnd))
        return Geometry(frame, content, work, w.scale, visible, bool(winapi.user32.IsZoomed(w.hwnd)))

    def screenshot(self) -> Image.Image | None:
        """The app's screen as the window shows it, in view pixels. None while it isn't showing."""
        if not self.showing():
            return None
        view = self.view
        _, _, w, h = win32gui.GetClientRect(view)
        surface = self._surface()
        if surface is not None:  # Direct3D draws it: PrintWindow sees nothing, the frame is right here
            return surface.capture(w, h)
        return winapi.capture(view, w, h)

    def foreground(self) -> bool:
        return win32gui.GetForegroundWindow() == self._win.hwnd

    def call(self, fn: Callable[[], None]) -> None:
        """Run fn on the window thread."""
        self._host.manager.call(fn)

    # ------------------------------------------------------------------ worker threads
    def keyboard(self, cps: float = 12.0, cancel: threading.Event | None = None) -> keys.Keyboard | None:
        """Keys and pastes for the app's focused text field (keys.py), posted to the window's view: the
        window needn't be in front. During a secure screen the view is protected.py's, which types
        what it's sent as WM_CHAR. None while the app isn't showing."""
        if not self.showing():
            return None
        surface = self._surface()
        if surface is not None:
            return _DisplayKeyboard(surface, cps, cancel)
        if self._win.protected is not None:
            return _CharKeyboard(self.view, cps, cancel)
        return keys.Keyboard(self.view, cps, cancel)

    def click(self, x: int, y: int) -> None:
        """A left click on the app's screen at view pixels (x, y), as the window's own mouse input."""
        if not self.showing():
            return
        surface = self._surface()
        if surface is not None:  # a touch on its display, as the view's own click makes
            surface.wake()
            emuview.link.touch(surface.point, surface.slot, int(x), int(y), True)
            time.sleep(0.06)
            emuview.link.touch(surface.point, surface.slot, int(x), int(y), False)
            return
        keys.post_click(self.view, int(x), int(y))

    def _touch(self, commands: list[str]) -> bool:
        display = self._win.display_id
        if not self.showing() or display is None or self._win.protected is not None:
            return False
        surface = self._surface()
        if surface is not None:
            surface.wake()  # stream while the page moves: an idle display shows a new frame only every POLL_SECS
        try:
            self._shell.run(" && ".join(f"input touchscreen -d {display} {c}" for c in commands))
            return True
        except Exception:
            return False

    def tap(self, x: int, y: int) -> bool:
        """A finger's tap at view pixel (x, y), on the app's display: what puts the cursor in a text
        field for sure (a posted click doesn't always reach the app). Worker thread; False if it
        couldn't (the app isn't showing, or a secure screen has it)."""
        return self._touch([f"tap {int(x)} {int(y)}"])

    def drag(self, x: int, y: int, dy: int) -> bool:
        """Drag a finger from view pixel (x, y) by dy and hold it still a moment before lifting: the
        page follows the finger and stops with it (no fling), as a slow drag on a phone does. Starts
        moving at once, so it's never a tap. Worker thread; False if the app isn't showing (or a
        secure screen has it)."""
        x, y, dy = int(x), int(y), int(dy)
        if not self._touch([f"motionevent DOWN {x} {y}", f"motionevent MOVE {x} {y + dy}"]):
            return False
        time.sleep(0.08)  # still for longer than Android's 40 ms: the lift carries no speed, so no fling
        return self._touch([f"motionevent UP {x} {y + dy}"])

    def fling(self, x: int, y: int, dy: int) -> bool:
        """A quick swipe from view pixel (x, y) by dy: the page flies on past the finger."""
        x, y, dy = int(x), int(y), int(dy)
        return self._touch([f"swipe {x} {y} {x} {y + dy} 60"])

    def release(self) -> None:
        """The window is gone: let go of what the handle holds."""
        self._shell.close()

    def focus(self) -> None:
        """Bring the app window forward with the keyboard in the app."""
        self.call(lambda: self._win.focus() if win32gui.IsWindow(self._win.hwnd) else None)

    def hotkeys(self, plugin: Plugin, wanted: dict[int, tuple[int, int]]) -> list[int]:
        """Window thread: see PluginHost.register_hotkeys."""
        return self._host.register_hotkeys(plugin, wanted)

    def drop_hotkeys(self, plugin: Plugin) -> None:
        self._host.unregister_hotkeys(plugin)

    # ------------------------------------------------------------------ window thread
    def layer(self) -> overlay.Layer:
        return overlay.Layer(self._win.hwnd)

    def panel(self, **kwargs: Any) -> Any:
        """A side panel (WebView2) owned by this window; see PluginHost.panel_factory."""
        if self._host.panel_factory is None:
            raise RuntimeError("no panels here (the agent's WebView isn't running)")
        return self._host.panel_factory(self, **kwargs)


class _CharKeyboard(keys.Keyboard):
    """protected.py's view: it passes WM_CHAR on as `input text` (no Ctrl+A, no clipboard)."""

    def select_all(self) -> bool:
        return True  # what's put in adds to the field

    def to_end(self) -> bool:
        return True

    def insert(self, text: str, mode: str = "paste", progress: Callable[[float], None] | None = None) -> bool:
        text = keys.normalize(text)
        for i, c in enumerate(text):
            if not self._ok():
                return False
            win32gui.PostMessage(self.hwnd, win32con.WM_CHAR, ord(c), 0)
            if progress:
                progress((i + 1) / len(text))
            self.sleep(0.03)
        return True

    paste = type = insert


class _DisplayKeyboard(keys.Keyboard):
    """An app on an emulator display (emuview.py): keys and text go to InputHelper with the display's id,
    as the view's own keyboard does, and a paste puts the text on Android's clipboard (gRPC) before
    Ctrl+V. Nothing is posted to a window, so nothing depends on Windows' keyboard state."""

    def __init__(self, surface: emuview.Surface, cps: float = 12.0, cancel: threading.Event | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(surface.view, cps, cancel, sleep)
        self.surface = surface

    def _ok(self) -> bool:
        if self.surface.closed and not self.cancel.is_set():
            self.stopped = "the window went away"
            return False
        return super()._ok()

    def _key(self, keycode: int, down: bool, meta: int) -> None:
        emuview.link.helper(f"key {self.surface.display} {0 if down else 1} {keycode} 0 {meta}")

    def press_code(self, keycode: int, ctrl: bool = False) -> bool:
        """An Android key press (KeyEvent.KEYCODE_*), with Ctrl held if asked."""
        if not self._ok():
            return False
        self.surface.wake()
        meta = emuview.META_CTRL if ctrl else 0
        if ctrl:
            self._key(emuview.KEYCODE_CTRL_LEFT, True, meta)
        self._key(keycode, True, meta)
        self.sleep(random.uniform(0.025, 0.05))
        self._key(keycode, False, meta)
        if ctrl:
            self._key(emuview.KEYCODE_CTRL_LEFT, False, 0)
        self.sleep(0.04)
        return True

    def select_all(self) -> bool:
        return self.press_code(emuview.KEYCODE_A, ctrl=True)

    def to_end(self) -> bool:
        return self.press_code(emuview.KEYCODE_MOVE_END, ctrl=True)

    def paste(self, text: str) -> bool:
        if not self._ok():
            return False
        text = keys.normalize(text)
        saved = keys._get_clipboard()
        emuview.link.set_clipboard(self.surface.point, text)
        try:
            ok = self.press_code(emuview.KEYCODE_V, ctrl=True)
            self.sleep(0.6)  # until Android has pasted
            return ok
        finally:
            # The emulator shares Android's clipboard with the PC's: if the text came across, put
            # the user's back.
            if saved is not None and saved != text and keys._get_clipboard() == text:
                keys._set_clipboard(saved)

    def _text(self, run: str) -> None:
        emuview.link.helper(f"text {self.surface.display} {run.encode('utf-8').hex()}")

    def type(self, text: str, progress: Callable[[float], None] | None = None) -> bool:
        """Character by character, as the PC's keyboard types into the view (Android's key map types
        what a US keyboard can, Shift included); anything else (emoji, accents) is pasted."""
        total = max(1, len(keys.normalize(text)))
        done = 0
        for kind, run in typeable(text):
            if kind == "paste":
                if not self.paste(run):
                    return False
                done += len(run)
            else:
                for c in run:
                    if not self._ok():
                        return False
                    self.surface.wake()
                    self._text(c)
                    done += 1
                    if progress:
                        progress(done / total)
                    self.sleep(max(0.0, 1 / self.cps * random.uniform(0.6, 1.4)))
            if progress:
                progress(done / total)
        return True


def typeable(text: str) -> list[tuple[str, str]]:
    """("keys", run) for what Android's virtual key map types (printable ASCII), ("paste", run) for the rest."""
    out: list[tuple[str, str]] = []
    for c in keys.normalize(text):
        kind = "keys" if " " <= c <= "~" else "paste"
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + c)
        else:
            out.append((kind, c))
    return out


class Plugin:
    """Base class. One instance per app window it's attached to."""

    name = ""  # short id, used in state.json
    title = ""
    packages: frozenset[str] = frozenset()

    def __init__(self, app: AppHandle) -> None:
        self.app = app

    def start(self) -> None:
        """The app is showing in its window for the first time."""

    def geometry_changed(self, geo: Geometry) -> None:
        """The window moved, resized, changed DPI, was minimized or restored, or its view changed."""

    def foreground_changed(self, hwnd: int) -> None:
        """Another window came to the front (any window: compare with app.hwnd)."""

    def hotkey(self, key_id: int) -> None:
        """A hotkey the plugin registered with host.hotkeys() was pressed."""

    def app_lost(self) -> None:
        """The app stopped showing (scrcpy reconnecting); app_back() follows if it returns."""

    def app_back(self) -> None:
        """The app shows again after app_lost()."""

    def stop(self) -> None:
        """The window is going away: close everything."""


class PluginHost:
    """Attaches plugins to app windows. Every method runs on the window thread."""

    def __init__(self, manager: WindowManager, modules: tuple[str, ...] = BUILTIN) -> None:
        self.manager = manager
        self.modules = modules
        self.classes: list[type[Plugin]] | None = None
        self.instances: dict[int, list[Plugin]] = {}  # app window hwnd -> its plugins
        self.panel_factory: Callable[..., Any] | None = None  # set by the agent, which runs WebView2
        self._hook = None
        self._hook_proc = None
        self._hotkeys: dict[int, tuple[Plugin, int]] = {}  # global id -> (plugin, its own id)
        self._next_hotkey = 0xB000

    def _load(self) -> list[type[Plugin]]:
        if self.classes is None:
            self.classes = []
            for name in self.modules:
                try:
                    self.classes.append(importlib.import_module(name).PLUGIN)
                except Exception:
                    log(f"plugins: can't load {name}:\n{traceback.format_exc().rstrip()}")
        return self.classes

    @staticmethod
    def enabled(cls: type[Plugin]) -> bool:
        return bool((state.get("plugins") or {}).get(cls.name, True))

    def _each(self, win: AppWindow, what: str, *args: Any) -> None:
        for p in self.instances.get(win.hwnd, []):
            try:
                getattr(p, what)(*args)
            except Exception:
                log(f"plugin {p.name}: {what} failed:\n{traceback.format_exc().rstrip()}")

    # ------------------------------------------------------------------ from AppWindow
    def shown(self, win: AppWindow) -> None:
        """The app's view was embedded: start its plugins, or tell them it's back."""
        if win.hwnd in self.instances:
            self._each(win, "app_back")
            self.moved(win)
            return
        wanted = [c for c in self._load() if win.app.package in c.packages and self.enabled(c)]
        if not wanted:
            return
        self._watch_foreground()
        self.instances[win.hwnd] = []
        for cls in wanted:
            try:
                plugin = cls(AppHandle(self, win))
                self.instances[win.hwnd].append(plugin)
                log(f"plugin {cls.name}: attached to {win.app.package}")
                plugin.start()
            except Exception:
                log(f"plugin {cls.name}: start failed:\n{traceback.format_exc().rstrip()}")
        self.moved(win)

    def moved(self, win: AppWindow) -> None:
        plugins = self.instances.get(win.hwnd)
        if plugins:
            geo = plugins[0].app.geometry()
            self._each(win, "geometry_changed", geo)

    def lost(self, win: AppWindow) -> None:
        self._each(win, "app_lost")

    def closed(self, win: AppWindow) -> None:
        self._each(win, "stop")
        for p in self.instances.pop(win.hwnd, []):
            self.unregister_hotkeys(p)
            p.app.release()

    # ------------------------------------------------------------------ foreground and hotkeys
    def _watch_foreground(self) -> None:
        if self._hook:
            return

        def changed(_hook: Any, _event: int, hwnd: int, *_: Any) -> None:
            for plugins in list(self.instances.values()):
                for p in plugins:
                    try:
                        p.foreground_changed(hwnd or 0)
                    except Exception:
                        log(f"plugin {p.name}: foreground_changed failed:\n{traceback.format_exc().rstrip()}")

        self._hook_proc = _WinEventProc(changed)
        self._hook = winapi.user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
                                                   self._hook_proc, 0, 0, WINEVENT_OUTOFCONTEXT)

    def register_hotkeys(self, plugin: Plugin, keys_: dict[int, tuple[int, int]]) -> list[int]:
        """Register {plugin's id: (MOD_* flags, virtual key)} as system-wide hotkeys, delivered to
        plugin.hotkey(id). Returns the ids that couldn't be had (another program holds them). Meant for
        while the plugin's window is in front: unregister_hotkeys() when it isn't."""
        failed = []
        for own_id, (mods, vk) in keys_.items():
            gid = self._next_hotkey
            self._next_hotkey += 1
            if winapi.user32.RegisterHotKey(self.manager.agent_hwnd, gid, mods | 0x4000, vk):  # MOD_NOREPEAT
                self._hotkeys[gid] = (plugin, own_id)
            else:
                failed.append(own_id)
        return failed

    def unregister_hotkeys(self, plugin: Plugin) -> None:
        for gid, (p, _) in list(self._hotkeys.items()):
            if p is plugin:
                winapi.user32.UnregisterHotKey(self.manager.agent_hwnd, gid)
                del self._hotkeys[gid]

    def on_hotkey(self, gid: int) -> None:
        """WM_HOTKEY on the agent window."""
        entry = self._hotkeys.get(gid)
        if entry:
            plugin, own_id = entry
            try:
                plugin.hotkey(own_id)
            except Exception:
                log(f"plugin {plugin.name}: hotkey failed:\n{traceback.format_exc().rstrip()}")
