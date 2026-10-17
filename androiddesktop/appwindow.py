"""One Android app as its own sleek, frameless Windows window.

The window draws a slim bar (back, icon, title, pin, minimize, maximize, close) that takes on the
app's own top color, so it reads as part of the app rather than a Windows title bar. It keeps the
native frame underneath: rounded corners, shadow, resize edges, Aero Snap and the Windows 11 Snap
Layouts flyout on the maximize button.

The bar's "..." button holds what else there is for the app: Pin to Start, a desktop shortcut,
Android's App info, Uninstall, and the app's plugins and Send to iPhone or iPad where there are those.

Below the bar sits the app itself, on an Android display made just for this app that follows the
window's size, so resizing the window resizes the app, which re-lays itself out like a desktop app.
That display is one of the emulator's own, its frames drawn straight onto a child window of ours
(emuview.py), when the emulator allows (up to seven windows). Otherwise it's scrcpy's: a virtual
display, video-encoded inside Android and shown by a scrcpy window re-parented as a borderless child.

If the app's display goes away without being asked to (scrcpy crashed, or Android itself did), the
window doesn't vanish: it shows "Reconnecting..." and starts the app again on a fresh display, booting
Android again if needed. If that keeps failing it says so and offers "Try again".

The phone button sends the app to a paired iPhone or iPad (remote/hub.py). While it's there the window
lets go of its own display (scrcpy's, or its emulator display) and shows "On iPhone" with Bring back,
which opens the app in the window again and pulls it home.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import re
import shlex
import subprocess
import threading
import time
from typing import TYPE_CHECKING

import win32api
import win32con
import win32gui
import win32process
from PIL import Image, ImageDraw, ImageFilter

from . import adb, children, emuview, state, winapi
from .catalog import App
from .log import log
from .paths import LOGS, SERIAL, android_env
from .runtime import SCRCPY_EXE

if TYPE_CHECKING:
    from .manager import WindowManager
    from .protected import Protected
    from .remote.hub import Link

WM_APP_EMBED = win32con.WM_APP + 1  # lparam: the window showing the app (scrcpy's, or emuview's)
WM_APP_EXITED = win32con.WM_APP + 2
WM_APP_FAILED = win32con.WM_APP + 3
WM_DPICHANGED = 0x02E0
WM_NCMOUSELEAVE = 0x02A2
WM_MOUSELEAVE = 0x02A3
HTMAXBUTTON = 9

TIMER_SAMPLE, TIMER_SPLASH, TIMER_FADE, TIMER_KILL, TIMER_RETRY = 1, 2, 3, 4, 5

STARTING, RUNNING, RECONNECTING, FAILED = "starting", "running", "reconnecting", "failed"
REMOTE = "remote"  # the app is on an iPhone or iPad: no scrcpy of our own
RETRY_DELAYS = (1, 2, 4, 8, 15)  # seconds before each reconnect attempt
STABLE_SECS = 30  # a connection that lasted this long starts the attempt count afresh

BAR_DIP = 30
CAPTION_BTN_DIP = 46
SMALL_BTN_DIP = 40
TOP_RESIZE_DIP = 5
DEFAULT_SIZE_DIP = (412, 860)
MIN_SIZE_DIP = (280, 220)

GLYPHS = {"back": "", "pin": "", "pinned": "", "min": "", "max": "",
          "restore": "", "close": "", "more": ""}
GLYPHS["send"] = ""  # CellPhone
TOOLTIPS = {"back": "Back", "pin": "Keep on top", "min": "Minimize", "max": "Maximize", "close": "Close",
            "more": "More", "send": "Send to iPhone or iPad"}
TIMER_HAND_OFF = 6  # scrcpy got its WM_CLOSE when the app moved to a phone; kill it if it lingers
CLOSE_RED = (196, 43, 28)
_LAUNCH_LOCK = threading.Lock()


# state.json "scrcpy_quality": how scrcpy windows (the fallback) encode the app's video inside Android.
# (scale of the window's size, bit rate, frames per second). Lower costs Android less CPU, looks softer.
SCRCPY_QUALITY = {
    "high": (1.0, "12M", 60),
    "balanced": (0.75, "6M", 60),
    "low": (0.5, "3M", 30),
}


def scrcpy_quality() -> tuple[float, str, int]:
    return SCRCPY_QUALITY.get(state.get("scrcpy_quality") or "high", SCRCPY_QUALITY["high"])


def scrcpy_args(package: str, size: tuple[int, int], density: int, pos: tuple[int, int],
                bg: tuple[int, int, int], quality: tuple[float, str, int] = SCRCPY_QUALITY["high"]) -> list[str]:
    """scrcpy's command line: one app on a virtual display of its own, its window at a screen position."""
    (w, h), (x, y) = size, pos
    scale, bit_rate, fps = quality
    # --max-size caps the video, not the display: the app still lays out for the whole window.
    smaller = [f"--max-size={max(64, round(max(w, h) * scale) // 8 * 8)}"] if scale < 1 else []
    return [str(SCRCPY_EXE), "-s", SERIAL, f"--new-display={w}x{h}/{density}", "--flex-display",
            f"--start-app={package}", "--no-vd-system-decorations", "--keep-active",
            "--no-audio",  # the emulator already plays Android's sound on the PC
            "--window-borderless", f"--window-title=androiddesktop:{package}",
            f"--window-x={x}", f"--window-y={y}",  # size comes from --new-display
            "--no-window-aspect-ratio-lock",
            # A real (virtual) hardware keyboard: Android keeps its on-screen keyboard away and
            # shortcuts behave like on a PC.
            "--keyboard=uhid", "--display-ime-policy=local",
            # Right-click and the mouse's back button go Back; Shift+right-click is a real right-click.
            "--mouse-bind=b+b+:++++",
            "--video-codec=h264", f"--video-bit-rate={bit_rate}", f"--max-fps={fps}", *smaller,
            "--background-color=%02x%02x%02x" % bg[:3], "--no-terminal-title"]


def menu_items(app: App, in_start: bool, pinned: bool, plugins: list[dict],
               phones: list[tuple[str, str]] | None) -> list:
    """The bar's "..." menu: (action, label, checked) for each item, (label, [items]) for a submenu,
    None for a separator. plugins: features.plugins_for(); phones: the iPhone and iPad hub's choices
    for the app, None without the hub."""
    items: list = []
    if phones is not None:
        items += [("Send to iPhone or iPad", [(f"send:{action}", label, False) for action, label in phones]), None]
    for p in plugins:
        items.append((f"plugin:{p['name']}={'off' if p['on'] else 'on'}", p["title"], p["on"]))
    if plugins:
        items.append(None)
    items += [("pin", "Keep on top", pinned),
              ("start", "Remove from Start" if in_start else "Pin to Start", False),
              ("desktop", "Add a desktop shortcut", False),
              ("info", "App info", False)]
    if not app.system:
        items.append(("uninstall", f"Uninstall {app.label}...", False))
    items += [None, ("settings", "Android Desktop settings", False)]
    return items


def parse_display_id(line: str) -> int | None:
    """scrcpy announces the virtual display it made: "[server] INFO: New display: 396x822/160 (id=3)"."""
    m = re.search(r"New display: .*\(id=(\d+)\)", line)
    return int(m.group(1)) if m else None


def mix(a: tuple, b: tuple, t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a[:3], b[:3]))


def luminance(c: tuple) -> float:
    return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255


def on_color(bg: tuple) -> tuple[int, int, int]:
    return (28, 28, 30) if luminance(bg) > 0.58 else (250, 250, 252)


class AppWindow:
    def __init__(self, manager: WindowManager, app: App, intent: list[str] | None = None,
                 adopt: int | None = None) -> None:
        self.manager = manager
        self.app = app
        self.intent = intent
        self.adopting = adopt  # a hidden display to bring the app in from, once it shows (adopt())
        self.hwnd = 0
        self.child = 0
        self.proc: subprocess.Popen | None = None
        self.display_id: int | None = None
        self.light = winapi.light_theme()
        self.bg = (243, 243, 243) if self.light else (32, 32, 32)
        self._fade_from = self._fade_to = self.bg
        self._fade_t = 1.0
        self.hover: str | None = None
        self.pressed: str | None = None
        self.active = True
        self.pinned = False
        self.closing = False
        self.status = STARTING
        self.error = ""
        self.protected: Protected | None = None  # a FLAG_SECURE screen, shown via protected.py
        self.surface: emuview.Surface | None = None  # the app on an emulator display, instead of scrcpy
        self.remote: Link | None = None  # the app is on an iPhone or iPad (remote/hub.py)
        self._gen = 0  # one per scrcpy launch; messages from an older launch are ignored
        self._shown = 0  # the launch whose window is embedded
        self._handled = 0  # the last launch whose end we've dealt with
        self._attempt = 0
        self._embedded_at = 0.0  # when the app last appeared; 0 if it never has
        self._splash_button: tuple[str, tuple[int, int, int, int]] | None = None  # (action, client rect)
        self.scale = 1.0
        self.opened_at = time.monotonic()
        self._phase = 0.0
        self._icons: dict[int, Image.Image] = {}

    # ------------------------------------------------------------------ geometry
    @property
    def bar(self) -> int:
        return round(BAR_DIP * self.scale)

    def _client(self) -> tuple[int, int]:
        _, _, w, h = win32gui.GetClientRect(self.hwnd)
        return w, h

    def _buttons(self, width: int) -> list[tuple[str, int, int]]:
        s = self.scale
        cap, small = round(CAPTION_BTN_DIP * s), round(SMALL_BTN_DIP * s)
        out = [("back", 0, small)]
        x = width
        right = [("close", cap), ("max", cap), ("min", cap), ("pin", small), ("more", small)]
        if self.manager.remote is not None:  # iPhone & iPad is available
            right.append(("send", small))
        for name, w in right:
            out.append((name, x - w, x))
            x -= w
        return out

    def _button_at(self, x: int, y: int) -> str | None:
        if self._splash_button and not self.child:
            name, (l, t, r, b) = self._splash_button
            if l <= x < r and t <= y < b:
                return name
        if y < 0 or y >= self.bar:
            return None
        for name, x0, x1 in self._buttons(self._client()[0]):
            if x0 <= x < x1:
                return name
        return None

    def _initial_rect(self) -> tuple[int, int, int, int, bool]:
        saved = (state.get("windows") or {}).get(self.app.package)
        if saved:
            x, y, w, h, maximized = saved
            mon = win32api.MonitorFromRect((x, y, x + w, y + h), win32con.MONITOR_DEFAULTTONULL)
            if mon:
                return x, y, w, h, bool(maximized)
        mon = win32api.MonitorFromPoint(win32api.GetCursorPos(), win32con.MONITOR_DEFAULTTONEAREST)
        info = win32api.GetMonitorInfo(mon)
        left, top, right, bottom = info["Work"]
        dpi = ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(mon.handle, 0, ctypes.byref(dpi), ctypes.byref(ctypes.c_uint()))
        s = (dpi.value or 96) / 96
        w = min(round(DEFAULT_SIZE_DIP[0] * s), right - left - 40)
        h = min(round(DEFAULT_SIZE_DIP[1] * s), bottom - top - round(40 * s))
        step = round(28 * s) * (len(self.manager.windows) % 6)
        x = left + (right - left - w) // 2 + step
        y = top + (bottom - top - h) // 2 + step
        return x, y, w, h, False

    def _save_rect(self) -> None:
        try:
            placement = win32gui.GetWindowPlacement(self.hwnd)
            l, t, r, b = placement[4]
            maximized = placement[1] == win32con.SW_SHOWMAXIMIZED
            windows = dict(state.get("windows") or {})
            windows[self.app.package] = [l, t, r - l, b - t, maximized]
            state.put("windows", windows)
        except Exception:
            pass

    # ------------------------------------------------------------------ lifecycle
    def create(self) -> None:
        x, y, w, h, maximized = self._initial_rect()
        style = win32con.WS_OVERLAPPEDWINDOW | win32con.WS_CLIPCHILDREN
        self.manager.creating = self
        self.hwnd = win32gui.CreateWindowEx(0, self.manager.window_class, self.app.label, style, x, y, w, h,
                                            0, 0, win32api.GetModuleHandle(None), None)
        self.manager.creating = None
        self.manager.by_hwnd[self.hwnd] = self  # messages sent during CreateWindowEx don't always reach us
        self.scale = winapi.dpi_of(self.hwnd) / 96
        winapi.set_app_identity(self.hwnd, f"AndroidDesktop.App.{self.app.package}"[:128], self.app.label,
                                ["open", self.app.package])
        winapi.set_window_icon(self.hwnd, self.app.ico, winapi.dpi_of(self.hwnd))
        winapi.dwm_int(self.hwnd, winapi.DWMWA_WINDOW_CORNER_PREFERENCE, winapi.DWMWCP_ROUND)
        winapi.dwm_int(self.hwnd, winapi.DWMWA_USE_IMMERSIVE_DARK_MODE, 0 if self.light else 1)
        self._apply_border()
        win32gui.SetWindowPos(self.hwnd, 0, 0, 0, 0, 0, win32con.SWP_FRAMECHANGED | win32con.SWP_NOMOVE |
                              win32con.SWP_NOSIZE | win32con.SWP_NOZORDER)
        win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWMAXIMIZED if maximized else win32con.SW_SHOWNORMAL)
        self.focus()
        ctypes.windll.user32.SetTimer(self.hwnd, TIMER_SPLASH, 33, None)
        ctypes.windll.user32.SetTimer(self.hwnd, TIMER_SAMPLE, 350, None)
        self._start()

    def focus(self) -> None:
        if win32gui.IsIconic(self.hwnd):
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(self.hwnd)
        except Exception:
            # Windows refuses focus to background processes; flash the taskbar button instead.
            ctypes.windll.user32.FlashWindow(self.hwnd, True)

    def close(self) -> None:
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)

    def _start(self) -> None:
        """(Re)launch scrcpy for this app on a worker thread."""
        self._gen += 1
        self.error = ""
        threading.Thread(target=self._launch, args=(self._gen,), daemon=True,
                         name=f"launch-{self.app.package}").start()

    def retry(self) -> None:
        """The "Try again" button, or opening the app again from the launcher."""
        if self.status != FAILED or self.closing:
            return
        self._attempt = 0
        self.status = RECONNECTING if self._embedded_at else STARTING
        self._splash_button = None
        winapi.user32.SetTimer(self.hwnd, TIMER_SPLASH, 33, None)
        win32gui.InvalidateRect(self.hwnd, None, False)
        self._start()

    def _density(self) -> int:
        return round(160 * self.scale * float(state.get("density", 1.0)))

    def _launch(self, gen: int) -> None:
        """Worker thread: make sure Android is up, then start this app on a virtual display of its own."""
        try:
            if not self.manager.vm.ensure_running():
                raise RuntimeError(self.manager.vm.detail or "Android didn't start.")
            cw, ch = self._client()
            w, h = cw, max(ch - self.bar, 1)
            x, y = win32gui.ClientToScreen(self.hwnd, (0, self.bar))
            density = self._density()
            point = emuview.link.usable()
            if point and self.manager.plugins.wants(self.app.package):
                point = None  # plugins read the app from scrcpy's window and type through its keyboard
            if point and adb.keyguard_showing() is not False:
                # Android's own lock screen is up (or it didn't say): until it's gone, apps on the
                # emulator's displays don't draw, while scrcpy's displays are "always unlocked".
                log(f"{self.app.package}: using scrcpy: Android's lock screen is up")
                point = None
            if point and self._open_surface(gen, point, (w, h), density):
                return
            if self.closing or gen != self._gen:
                return
            args = scrcpy_args(self.app.package, (w, h), density, (x, y), self.bg, scrcpy_quality())
            (LOGS / "windows").mkdir(parents=True, exist_ok=True)
            # One at a time: every scrcpy pushes its server to the same path on the device, and
            # simultaneous starts trip over each other ("Server connection failed").
            # scrcpy opens its window before its server is up, so wait for the server's display.
            with _LAUNCH_LOCK:
                if self.closing or gen != self._gen:
                    return
                self.display_id = None
                # Ends with us, however we end: without its window, scrcpy is no use to anyone.
                self.proc = children.popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           env=android_env(), creationflags=adb.CREATE_NO_WINDOW, text=True,
                                           encoding="utf-8", errors="replace")
                threading.Thread(target=self._read_output, args=(self.proc, gen), daemon=True).start()
                end = time.monotonic() + 30
                while self.display_id is None and self._shown != gen:
                    if time.monotonic() > end or self.proc.poll() is not None:
                        raise RuntimeError(self.error or "The app didn't start.")
                    time.sleep(0.03)
            while not (found := self._scrcpy_windows()):
                if self._shown == gen:
                    return  # the adopt timer embedded it first
                if time.monotonic() > end or self.proc.poll() is not None:
                    raise RuntimeError(self.error or "The app window didn't appear.")
                time.sleep(0.03)
            win32gui.PostMessage(self.hwnd, WM_APP_EMBED, gen, found[0])
        except Exception as e:
            log(f"{self.app.package}: launch failed: {e!r}")
            if gen == self._gen:
                self.error = str(e)
            if self.hwnd and win32gui.IsWindow(self.hwnd):
                win32gui.PostMessage(self.hwnd, WM_APP_FAILED, gen, 0)

    def _open_surface(self, gen: int, point: tuple[int, str], size: tuple[int, int], density: int) -> bool:
        """Worker thread: the app on an emulator display of its own (emuview.py). True once it shows,
        or the window went away meanwhile; False to use scrcpy instead."""
        surface = self.surface = emuview.Surface(self, point, gen)
        try:
            self.display_id = surface.open(self.app, size, density)
            attached = threading.Event()
            problem: list[Exception] = []

            def attach() -> None:  # on the window thread, which owns the view
                try:
                    if self.closing or gen != self._gen:
                        raise emuview.Unusable("closed")
                    cw, ch = self._client()
                    surface.attach(self.hwnd, (0, self.bar, cw, max(ch - self.bar, 1)))
                except Exception as e:
                    problem.append(e)
                finally:
                    attached.set()
            self.manager.call(attach)
            if not attached.wait(15):
                raise RuntimeError("the window didn't answer")
            if problem:
                raise problem[0]
            if not surface.first_frame.wait(emuview.FIRST_FRAME_SECS):
                log(f"{self.app.package}: no frame from emulator display {surface.slot} yet")
            elif surface.frame != surface.size:
                # A windowless emulator streams its extra displays at the main display's size.
                emuview.link.disable(point, f"frames are {surface.frame}, the display {surface.size}")
                raise emuview.Unusable("wrong frame size")
            log(f"{self.app.package}: on emulator display {surface.slot} (display {self.display_id}, "
                f"{surface.size[0]}x{surface.size[1]}/{density})")
            win32gui.PostMessage(self.hwnd, WM_APP_EMBED, gen, surface.view)
            return True
        except Exception as e:
            if self.surface is surface:
                self.surface, self.display_id = None, None
            surface.close()
            self.manager.call(surface.destroy_view)
            if self.closing or gen != self._gen:
                return True
            log(f"{self.app.package}: using scrcpy: {e if isinstance(e, emuview.Unusable) else repr(e)}")
            return False

    def surface_lost(self, surface: emuview.Surface) -> None:
        """Any thread: the emulator display's frames ended for good (Android or the emulator went away)."""
        if surface is self.surface and self.hwnd and win32gui.IsWindow(self.hwnd):
            win32gui.PostMessage(self.hwnd, WM_APP_EXITED, surface.gen, 0)

    def _drop_surface(self) -> None:
        """Window thread: close the emulator display (and the app on it) behind the scenes."""
        surface, self.surface = self.surface, None
        if surface:
            # Not a daemon: quitting waits for it, so the display doesn't linger.
            threading.Thread(target=surface.close, name=f"close-{self.app.package}").start()
            surface.destroy_view()

    def _read_output(self, proc: subprocess.Popen, gen: int) -> None:
        # The first launch starts the log afresh; reconnects append, so the reason for a crash survives.
        path = LOGS / "windows" / f"{self.app.package}.log"
        with path.open("w" if gen == 1 else "a", encoding="utf-8") as out:
            out.write(f"--- {time.strftime('%H:%M:%S')} launch {gen}\n")
            for line in proc.stdout:
                out.write(line)
                out.flush()
                display = parse_display_id(line)
                if display is not None and gen == self._gen:
                    self.display_id = display
                elif "ERROR" in line and gen == self._gen and not self.child:
                    self.error = line.split("ERROR:", 1)[-1].strip()
        proc.wait()
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            win32gui.PostMessage(self.hwnd, WM_APP_EXITED, gen, 0)

    def _lost(self, gen: int, exited: bool) -> None:
        """scrcpy ended or never got going. Close the window if that was wanted; otherwise reconnect."""
        if gen != self._gen or gen <= self._handled:
            return  # an older launch, or this one's end already handled (failure and exit both report)
        if not exited and self._shown == gen and self.proc and self.proc.poll() is None:
            log(f"{self.app.package}: ignoring a late launch failure; the app is showing")
            return
        self._handled = gen
        if self.protected:
            self.protected.end(remove=self.closing)
        if self.closing:
            win32gui.DestroyWindow(self.hwnd)
            return
        proc, was_running, on_surface = self.proc, self.status == RUNNING, self.surface is not None
        if proc and proc.poll() is None:
            proc.kill()
        self._drop_surface()
        self.child, self.display_id = 0, None
        self.manager.plugins.lost(self)
        if not (self.manager.accepting and self.manager.vm.keep_running):
            # Android was shut down on purpose, or we're quitting: nothing to come back to.
            win32gui.DestroyWindow(self.hwnd)
            return
        now = time.monotonic()
        if was_running:
            gone = "its emulator display went away" if on_surface else                 f"scrcpy exited (code {proc.poll() if proc else None})"
            log(f"{self.app.package}: {gone} after {now - self._embedded_at:.0f}s; reconnecting")
            if now - self._embedded_at > STABLE_SECS:
                self._attempt = 0
        if self._embedded_at and self._attempt < len(RETRY_DELAYS):  # it worked before: try again
            delay = RETRY_DELAYS[self._attempt]
            self._attempt += 1
            self.status = RECONNECTING
            winapi.user32.SetTimer(self.hwnd, TIMER_RETRY, delay * 1000, None)
            winapi.user32.SetTimer(self.hwnd, TIMER_SPLASH, 33, None)
        else:
            if self._embedded_at:
                self.error = "Couldn't reconnect to Android."
                log(f"{self.app.package}: giving up after {self._attempt} reconnect attempts")
            else:
                self.error = self.error or "The app couldn't start. See the logs folder."
            self.status = FAILED
            winapi.user32.KillTimer(self.hwnd, TIMER_SPLASH)
        win32gui.InvalidateRect(self.hwnd, None, False)
        self.manager.changed()

    def _scrcpy_windows(self) -> list[int]:
        """scrcpy's video window(s) still floating at top level. SDL also makes short-lived helper
        windows, so match its class and the exact title we gave it."""
        if not self.proc:
            return []
        title = f"androiddesktop:{self.app.package}"
        found: list[int] = []

        def visit(hwnd: int, _: object) -> bool:
            try:
                if (win32gui.GetClassName(hwnd) == "SDL_app" and win32gui.GetWindowText(hwnd) == title
                        and win32process.GetWindowThreadProcessId(hwnd)[1] == self.proc.pid):
                    found.append(hwnd)
            except win32gui.error:
                pass  # closed while we looked
            return True

        win32gui.EnumWindows(visit, None)
        return found

    def _adopt(self) -> None:
        """If SDL recreated its window (renderer change), pull the new one in too."""
        if self.closing or not self.proc or self.proc.poll() is not None:
            return
        for hwnd in self._scrcpy_windows():  # top-level only: an embedded window isn't listed
            self._embed(hwnd)

    def _embed(self, child: int) -> None:
        if not win32gui.IsWindow(child) or self.closing:
            return
        if child == self.child and winapi.user32.GetAncestor(child, 1) == self.hwnd:
            return  # already ours (the adopt timer got there first)
        u = winapi.user32
        style = u.GetWindowLongPtrW(child, win32con.GWL_STYLE)
        style &= ~(win32con.WS_POPUP | win32con.WS_CAPTION | win32con.WS_THICKFRAME | win32con.WS_SYSMENU |
                   win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX)
        u.SetWindowLongPtrW(child, win32con.GWL_STYLE, style | win32con.WS_CHILD | win32con.WS_CLIPSIBLINGS)
        ex = u.GetWindowLongPtrW(child, win32con.GWL_EXSTYLE)
        ex &= ~(win32con.WS_EX_APPWINDOW | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_WINDOWEDGE |
                win32con.WS_EX_CLIENTEDGE)
        u.SetWindowLongPtrW(child, win32con.GWL_EXSTYLE, ex)
        u.SetParent(child, self.hwnd)
        if u.GetAncestor(child, 1) != self.hwnd:  # GA_PARENT: SDL may still be busy creating it; retry next tick
            log(f"{self.app.package}: re-parenting {child:#x} didn't take yet")
            return
        self.child = child
        log(f"{self.app.package}: {'reconnected' if self.status == RECONNECTING else 'embedded'} "
            f"{child:#x} (display {self.display_id})")
        self.status = RUNNING
        self._shown = self._gen
        self._embedded_at = self.opened_at = time.monotonic()
        self._splash_button = None
        self._layout(frame_changed=True)
        win32gui.ShowWindow(child, win32con.SW_SHOW)
        u.KillTimer(self.hwnd, TIMER_SPLASH)
        if self.intent:
            self.run_intent(self.intent)
            self.intent = None
        adopted = self.adopting
        if adopted is not None:
            self.adopt(adopted)
        if win32gui.GetForegroundWindow() == self.hwnd:
            win32gui.SetFocus(child)
        win32gui.InvalidateRect(self.hwnd, None, False)
        if self.manager.remote:
            self.manager.remote.window_embedded(self, adopted)
        self.manager.plugins.shown(self)
        self.manager.changed()

    def run_intent(self, intent: list[str]) -> None:
        """Start an activity inside this window, e.g. a Play Store search."""
        if self.display_id is not None:
            command = f"am start --display {self.display_id} " + shlex.join(intent)
            threading.Thread(target=adb.shell, args=(command,), daemon=True).start()

    def adopt(self, display: int) -> None:
        """Take in the app as an AI agent left it on a hidden display of its own (ghost.py). scrcpy's launch
        of the app here usually brings its task over already (Android moves a running app's task to the
        display it's started on); any task still left there moves now."""
        if self.display_id is None or not self.child:
            self.adopting = display  # once the app shows
            return
        self.adopting = None
        threading.Thread(target=self._take_tasks, args=(display, self.display_id), daemon=True,
                         name=f"adopt-{self.app.package}").start()

    def go_remote(self, link: Link) -> None:
        """The app is on a phone or tablet now (the hub moved its task to a display of its own): stop our
        scrcpy, whose display is empty, and show where the app went."""
        if self.closing:
            return
        self.remote = link
        if self.status != REMOTE:
            self._handled = self._gen  # scrcpy's exit is on purpose, not a crash to recover from
            if self.protected:
                self.protected.end()
            self._drop_surface()  # on an emulator display: it's empty now the app has moved
            child, self.child = self.child, 0
            if child and win32gui.IsWindow(child):
                win32gui.ShowWindow(child, win32con.SW_HIDE)
                win32gui.PostMessage(child, win32con.WM_CLOSE, 0, 0)
                winapi.user32.SetTimer(self.hwnd, TIMER_HAND_OFF, 3000, None)
            self.status = REMOTE
            self.error = ""
            log(f"{self.app.package}: on {link.hub.group_name(link.group)} (display {link.display})")
        self.display_id = link.display  # Back in the bar goes where the app is
        winapi.user32.SetTimer(self.hwnd, TIMER_SPLASH, 1000, None)  # "waiting for" turns into "is on"
        win32gui.InvalidateRect(self.hwnd, None, False)
        self.manager.changed()

    def come_back(self, display: int | None) -> None:
        """Bring back: scrcpy on a fresh display again; its launch of the app pulls the task over from the
        phone's display, and adopt() moves anything left behind."""
        if self.closing or self.status != REMOTE:
            return
        self.remote = None
        self.status = RECONNECTING
        self.adopting, self.display_id = display, None
        self._attempt = 0
        winapi.user32.SetTimer(self.hwnd, TIMER_SPLASH, 33, None)
        win32gui.InvalidateRect(self.hwnd, None, False)
        self._start()

    def _shared(self) -> bool:
        hub = self.manager.remote
        return bool(hub and hub.link_for(self.app.package))

    def _send_menu(self) -> None:
        """The phone button: send the app to a paired device (or share it), bring it back, or pair one."""
        hub = self.manager.remote
        if hub is None:
            return
        choices = hub.choices(self.app.package)
        menu = win32gui.CreatePopupMenu()
        for i, (action, label) in enumerate(choices):
            if action == "pair" and i:
                win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, 100 + i, label)
        x0 = next(x for name, x, _ in self._buttons(self._client()[0]) if name == "send")
        x, y = win32gui.ClientToScreen(self.hwnd, (x0, self.bar))
        cmd = win32gui.TrackPopupMenu(menu, win32con.TPM_RETURNCMD | win32con.TPM_LEFTALIGN | win32con.TPM_TOPALIGN,
                                      x, y, 0, self.hwnd, None)
        win32gui.DestroyMenu(menu)
        if cmd and 0 <= cmd - 100 < len(choices):
            hub.window_choice(self, choices[cmd - 100][0])

    def _take_tasks(self, source: int, target: int) -> None:
        from .protected import parse_root_tasks, reset_secure_state_soon  # protected.py imports this module
        try:
            tasks = parse_root_tasks(adb.shell("cmd activity stack list", timeout=10), source)
            for task in reversed(tasks):  # bottom first, so they keep their order
                adb.shell(f"am display move-stack {task} {target}", timeout=10)
            reset_secure_state_soon()  # moving a secure screen can black out every window (protected.py)
            moved = f", moved tasks {tasks}" if tasks else ""
            log(f"{self.app.package}: adopted from display {source}{moved}")
        except Exception as e:
            log(f"{self.app.package}: adopting from display {source}: {e!r}")

    def _layout(self, frame_changed: bool = False) -> None:
        if not self.child or not win32gui.IsWindow(self.child):
            return  # none yet, or scrcpy just died and its window with it (the exit message follows)
        cw, ch = self._client()
        flags = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | (win32con.SWP_FRAMECHANGED if frame_changed else 0)
        win32gui.SetWindowPos(self.child, 0, 0, self.bar, cw, max(ch - self.bar, 1), flags)
        if self.surface:
            self.surface.resize(cw, max(ch - self.bar, 1), self._density())
        if self.protected:
            self.protected.place()
        self.manager.plugins.moved(self)

    @property
    def view(self) -> int:
        """The window showing the app: scrcpy's, or protected.py's while a secure screen is up."""
        return self.protected.hwnd if self.protected and self.protected.hwnd else self.child

    def _child_misplaced(self) -> bool:
        cw, ch = self._client()
        l, t, r, b = win32gui.GetWindowRect(self.child)
        x, y = win32gui.ScreenToClient(self.hwnd, (l, t))
        return (x, y, r - l, b - t) != (0, self.bar, cw, max(ch - self.bar, 1))

    # ------------------------------------------------------------------ color
    def _sample(self) -> None:
        """Match the bar to the app's own top color, so window and app read as one surface."""
        if not self.child or not win32gui.IsWindow(self.child) or win32gui.IsIconic(self.hwnd):
            return
        if self._child_misplaced():  # scrcpy sized its own window once the video started
            self._layout()
        cw, _ = self._client()
        img = self.surface.capture(cw, 3) if self.surface else winapi.capture(self.view, cw, 3)
        if img is None:
            return
        colors = img.crop((0, 1, cw, 2)).getcolors(cw)
        if not colors:
            return
        count, color = max(colors)
        if count < cw * 0.25:
            return  # no clear background color (an image header, say): keep the current one
        if color != self._fade_to:
            self._fade_from, self._fade_to, self._fade_t = self.bg, color, 0.0
            winapi.user32.SetTimer(self.hwnd, TIMER_FADE, 16, None)

    def _fade_step(self) -> None:
        self._fade_t = min(1.0, self._fade_t + 0.14)
        t = 1 - (1 - self._fade_t) ** 3  # ease-out
        self.bg = mix(self._fade_from, self._fade_to, t)
        self._apply_border()
        self._repaint_bar()
        if self._fade_t >= 1.0:
            winapi.user32.KillTimer(self.hwnd, TIMER_FADE)

    def _apply_border(self) -> None:
        border = mix(self.bg, (0, 0, 0) if luminance(self.bg) > 0.5 else (255, 255, 255), 0.12)
        winapi.dwm_int(self.hwnd, winapi.DWMWA_BORDER_COLOR, winapi.colorref(border))

    # ------------------------------------------------------------------ drawing
    def _icon(self, px: int) -> Image.Image | None:
        if px not in self._icons:
            try:
                self._icons[px] = Image.open(self.app.icon).convert("RGBA").resize((px, px), Image.LANCZOS)
            except OSError:
                return None
        return self._icons[px]

    def _render_bar(self, width: int) -> Image.Image:
        s, h = self.scale, self.bar
        bg = self.bg
        fg = on_color(bg)
        dim = mix(bg, fg, 0.42)
        im = Image.new("RGB", (max(width, 1), h), bg)
        d = ImageDraw.Draw(im)
        maximized = bool(winapi.user32.IsZoomed(self.hwnd))
        glyph_font = winapi.font("icons", max(8, round(10 * s)))
        for name, x0, x1 in self._buttons(width):
            hot, down = self.hover == name, self.pressed == name
            color = fg if self.active else dim
            if name == "close" and (hot or down):
                d.rectangle((x0, 0, x1, h), fill=mix(CLOSE_RED, bg, 0.12 if down else 0))
                color = (255, 255, 255)
            elif hot or down:
                d.rectangle((x0, 0, x1, h), fill=mix(bg, fg, 0.15 if down else 0.09))
            if name == "back" and self.display_id is None:
                color = mix(bg, fg, 0.25)
            if name == "send" and (self.remote is not None or self._shared()):  # lit while on a device
                color = (96, 160, 255) if luminance(bg) < 0.5 else (0, 95, 204)
            glyph = GLYPHS["restore" if name == "max" and maximized else
                           "pinned" if name == "pin" and self.pinned else name]
            d.text(((x0 + x1) / 2, h / 2), glyph, font=glyph_font, fill=color, anchor="mm")
        x = round(SMALL_BTN_DIP * s) + round(2 * s)
        icon = self._icon(round(16 * s))
        if icon:
            if not self.active:
                icon = Image.blend(Image.new("RGBA", icon.size, (*bg, 0)), icon, 0.6)
            im.paste(icon, (x, (h - icon.height) // 2), icon)
            x += icon.width + round(8 * s)
        title_font = winapi.font("text", max(9, round(12 * s)))
        room = self._buttons(width)[-1][1] - x - round(10 * s)
        title = self.app.label
        while title and d.textlength(title, font=title_font) > room:
            title = title[:-2] + "…" if len(title) > 2 else ""
        d.text((x, h / 2), title, font=title_font, fill=fg if self.active else dim, anchor="lm")
        return im

    def _render_splash(self, width: int, height: int) -> Image.Image:
        s = self.scale
        bg = self.bg
        fg = on_color(bg)
        im = Image.new("RGB", (max(width, 1), max(height, 1)), bg)
        cx, cy = width // 2, int(height * 0.42)
        size = round(76 * s)
        icon = self._icon(size)
        if icon:
            shadow = Image.new("RGBA", (size + round(40 * s),) * 2, (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle(
                (round(20 * s), round(26 * s), round(20 * s) + size, round(26 * s) + size),
                radius=round(size * 0.24), fill=(0, 0, 0, 70 if luminance(bg) > 0.5 else 140))
            shadow = shadow.filter(ImageFilter.GaussianBlur(round(10 * s)))
            im.paste(shadow, (cx - shadow.width // 2, cy - shadow.height // 2), shadow)
            # A gentle breathing scale while Android gets ready.
            breathe = 1 + 0.035 * math.sin(self._phase * 2.2)
            px = round(size * breathe)
            im.paste(icon.resize((px, px), Image.LANCZOS), (cx - px // 2, cy - px // 2),
                     icon.resize((px, px), Image.LANCZOS))
        d = ImageDraw.Draw(im)
        text_font = winapi.font("text", max(9, round(13 * s)))
        button = None  # (action, label)
        if self.status == REMOTE and self.remote is not None:
            where = self.remote.hub.group_name(self.remote.group)
            message = (f"{self.app.label} is on your {where}." if self.remote.clients else
                       f"{self.app.label} is waiting for your {where}: open WinDroid there.")
            button = ("bringback", "Bring back")
        elif self.status == FAILED:
            message, button = self.error, ("retry", "Try again")
        elif self.manager.vm.state == "locked":
            message, button = f"Android is locked. Unlock it to open {self.app.label}.", ("unlock", "Unlock")
        elif self.manager.vm.state != "running":
            message = self.manager.vm.detail or "Starting Android..."
        elif self.status == RECONNECTING:
            message = f"Bringing {self.app.label} back..." if self.adopting is not None else "Reconnecting..."
        else:
            message = f"Opening {self.app.label}..."
        ty = cy + size // 2 + round(30 * s)
        d.text((cx, ty), message, font=text_font, fill=mix(bg, fg, 0.7), anchor="mm")
        if button:
            name, label = button
            bw = round(d.textlength(label, font=text_font) + 32 * s)
            bh = round(32 * s)
            bx, by = cx - bw // 2, ty + round(22 * s)
            hot = self.hover == name
            d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=round(8 * s),
                                fill=mix(bg, fg, 0.16 if hot else 0.1), outline=mix(bg, fg, 0.2))
            d.text((cx, by + bh / 2), label, font=text_font, fill=fg, anchor="mm")
            self._splash_button = (name, (bx, by + self.bar, bx + bw, by + bh + self.bar))  # client coordinates
        else:
            # Indeterminate progress: a short pill gliding along a hairline track.
            tw, th = round(120 * s), max(2, round(3 * s))
            tx, py = cx - tw // 2, ty + round(22 * s)
            d.rounded_rectangle((tx, py, tx + tw, py + th), radius=th, fill=mix(bg, fg, 0.12))
            seg = round(tw * 0.32)
            pos = (math.sin(self._phase * 1.6) + 1) / 2
            sx = tx + round((tw - seg) * pos)
            d.rounded_rectangle((sx, py, sx + seg, py + th), radius=th, fill=mix(bg, fg, 0.65))
            self._splash_button = None
        return im

    def _repaint_bar(self) -> None:
        w, _ = self._client()
        win32gui.InvalidateRect(self.hwnd, (0, 0, w, self.bar), False)

    def _paint(self) -> None:
        hdc, ps = win32gui.BeginPaint(self.hwnd)
        try:
            w, h = self._client()
            winapi.blit(hdc, self._render_bar(w), 0, 0)
            if not self.child:
                winapi.blit(hdc, self._render_splash(w, h - self.bar), 0, self.bar)
        finally:
            win32gui.EndPaint(self.hwnd, ps)

    # ------------------------------------------------------------------ actions
    def _act(self, name: str) -> None:
        if name == "back" and self.surface:
            self.surface.back()
        elif name == "back" and self.display_id is not None:
            display = self.protected.input_display if self.protected else self.display_id
            threading.Thread(target=adb.shell, args=(f"input -d {display} keyevent 4",), daemon=True).start()
        elif name == "pin":
            self.pinned = not self.pinned
            win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST if self.pinned else win32con.HWND_NOTOPMOST,
                                  0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        elif name == "min":
            win32gui.ShowWindow(self.hwnd, win32con.SW_MINIMIZE)
        elif name == "max":
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE if winapi.user32.IsZoomed(self.hwnd) else win32con.SW_MAXIMIZE)
        elif name == "close":
            self.close()
        elif name == "retry":
            self.retry()
        elif name == "unlock":
            self.manager.on_unlock(self)
        elif name == "send":
            self._send_menu()
        elif name == "bringback" and self.manager.remote:
            self.manager.remote.bring_back(self.app.package)
        elif name == "more":
            self._repaint_bar()
            self._more_menu()
            self.hover = None
        self._repaint_bar()

    def _more_menu(self) -> None:
        """The "..." button: everything else there is for this app (menu_items)."""
        from . import features, shortcuts  # features.py imports this module
        hub = getattr(self.manager, "remote", None)  # iPhone & iPad, in builds that have it
        try:
            in_start = shortcuts.in_start(self.app)
        except Exception:
            in_start = False
        items = menu_items(self.app, in_start, self.pinned,
                           features.plugins_for(getattr(self.manager, "plugins", None), self.app.package),
                           hub.choices(self.app.package) if hub else None)
        actions: list[str] = []

        def add(menu: int, entries: list) -> None:
            for item in entries:
                if item is None:
                    win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
                elif isinstance(item[1], list):
                    sub = win32gui.CreatePopupMenu()
                    add(sub, item[1])
                    win32gui.AppendMenu(menu, win32con.MF_POPUP, sub, item[0].replace("&", "&&"))
                else:
                    action, label, checked = item
                    actions.append(action)
                    flags = win32con.MF_STRING | (win32con.MF_CHECKED if checked else 0)
                    win32gui.AppendMenu(menu, flags, 100 + len(actions) - 1, label.replace("&", "&&"))
        menu = win32gui.CreatePopupMenu()
        add(menu, items)
        x0 = next(x for name, x, _ in self._buttons(self._client()[0]) if name == "more")
        x, y = win32gui.ClientToScreen(self.hwnd, (x0, self.bar))
        cmd = win32gui.TrackPopupMenu(menu, win32con.TPM_RETURNCMD | win32con.TPM_LEFTALIGN | win32con.TPM_TOPALIGN,
                                      x, y, 0, self.hwnd, None)
        win32gui.DestroyMenu(menu)  # and its submenus
        if cmd and 0 <= cmd - 100 < len(actions):
            self._menu_action(actions[cmd - 100], hub)

    def _menu_action(self, action: str, hub=None) -> None:
        if action == "pin":
            self._act("pin")
        elif action.startswith("send:") and hub:
            hub.window_choice(self, action[len("send:"):])
        elif action == "uninstall":
            answer = win32api.MessageBox(self.hwnd, f"Uninstall {self.app.label} from Android? Its data goes too.",
                                         "Uninstall", win32con.MB_OKCANCEL | win32con.MB_ICONWARNING)
            if answer == win32con.IDOK:
                self.manager.on_window_action(self, "uninstall")
        else:
            self.manager.on_window_action(self, action)

    def _set_hover(self, name: str | None) -> None:
        if name != self.hover:
            if {"retry", "unlock", "bringback"} & {name, self.hover}:
                win32gui.InvalidateRect(self.hwnd, None, False)
            self.hover = name
            self._repaint_bar()

    # ------------------------------------------------------------------ window procedure
    def wndproc(self, hwnd: int, msg: int, wp: int, lp: int) -> int:
        if msg == win32con.WM_NCCALCSIZE and wp:
            rect = ctypes.cast(lp, ctypes.POINTER(wt.RECT)).contents
            top = rect.top
            win32gui.DefWindowProc(hwnd, msg, wp, lp)  # standard left/right/bottom resize frame
            rect.top = top  # ...but no caption and no top border: our bar is the top edge
            if winapi.user32.IsZoomed(hwnd):
                rect.top += winapi.frame_thickness(winapi.dpi_of(hwnd))
            return 0
        if msg == win32con.WM_NCHITTEST:
            hit = win32gui.DefWindowProc(hwnd, msg, wp, lp)
            if hit != win32con.HTCLIENT:
                return hit
            x, y = win32gui.ScreenToClient(hwnd, (ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short(lp >> 16 & 0xFFFF).value))
            if not winapi.user32.IsZoomed(hwnd) and y < round(TOP_RESIZE_DIP * self.scale):
                corner = round(16 * self.scale)
                w = self._client()[0]
                return win32con.HTTOPLEFT if x < corner else win32con.HTTOPRIGHT if x >= w - corner else win32con.HTTOP
            button = self._button_at(x, y)
            if button == "max":
                return HTMAXBUTTON  # lets Windows 11 show its Snap Layouts flyout
            if button:
                return win32con.HTCLIENT
            return win32con.HTCAPTION if y < self.bar else win32con.HTCLIENT
        if msg == win32con.WM_PAINT:
            self._paint()
            return 0
        if msg == win32con.WM_ERASEBKGND:
            return 1
        if msg == win32con.WM_SIZE:
            self._layout()
            if wp == win32con.SIZE_MINIMIZED:
                self.manager.plugins.moved(self)
            win32gui.InvalidateRect(hwnd, None, False)
            return 0
        if msg == win32con.WM_MOVE:
            self.manager.plugins.moved(self)
            return 0
        if msg == win32con.WM_ACTIVATE:
            self.active = (wp & 0xFFFF) != win32con.WA_INACTIVE
            if self.active and self.surface:
                self.surface.wake()
            if self.active and self.child:
                winapi.user32.SetFocus(self.view)
            self._repaint_bar()
            return 0
        if msg == win32con.WM_SETFOCUS and self.child:
            winapi.user32.SetFocus(self.view)
            return 0
        if msg == win32con.WM_PARENTNOTIFY and self.child and (wp & 0xFFFF) in (
                win32con.WM_LBUTTONDOWN, win32con.WM_RBUTTONDOWN, win32con.WM_MBUTTONDOWN):
            winapi.user32.SetFocus(self.view)  # a click into the app gives it the keyboard
            return 0
        if msg == win32con.WM_MOUSEMOVE:
            x, y = ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short(lp >> 16 & 0xFFFF).value
            self._set_hover(self._button_at(x, y))
            winapi.track_leave(hwnd)
            return 0
        if msg == WM_MOUSELEAVE:
            if self.hover != "max":
                self._set_hover(None)
            return 0
        if msg == win32con.WM_NCMOUSEMOVE:
            self._set_hover("max" if wp == HTMAXBUTTON else None)
            winapi.track_leave(hwnd, nonclient=True)
            return win32gui.DefWindowProc(hwnd, msg, wp, lp)
        if msg == WM_NCMOUSELEAVE:
            if self.hover == "max":
                self._set_hover(None)
            return 0
        if msg == win32con.WM_LBUTTONDOWN:
            x, y = ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short(lp >> 16 & 0xFFFF).value
            self.pressed = self._button_at(x, y)
            if self.pressed:
                win32gui.SetCapture(hwnd)
                self._repaint_bar()
            return 0
        if msg == win32con.WM_LBUTTONUP:
            x, y = ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short(lp >> 16 & 0xFFFF).value
            pressed, self.pressed = self.pressed, None
            win32gui.ReleaseCapture()
            if pressed and pressed == self._button_at(x, y):
                self._act(pressed)
            self._repaint_bar()
            return 0
        if msg == win32con.WM_NCLBUTTONDOWN and wp == HTMAXBUTTON:
            self.pressed = "max"
            self._repaint_bar()
            return 0
        if msg == win32con.WM_NCLBUTTONUP and wp == HTMAXBUTTON:
            if self.pressed == "max":
                self._act("max")
            self.pressed = None
            return 0
        if msg == win32con.WM_TIMER:
            if wp == TIMER_SAMPLE:
                self._adopt()
                self._sample()
            elif wp == TIMER_FADE:
                self._fade_step()
            elif wp == TIMER_SPLASH:
                self._phase += 0.033 * 2.4
                win32gui.InvalidateRect(hwnd, None, False)
            elif wp == TIMER_RETRY:
                winapi.user32.KillTimer(hwnd, TIMER_RETRY)
                if not self.closing and self.status == RECONNECTING:
                    self._start()
            elif wp == TIMER_KILL:
                winapi.user32.KillTimer(hwnd, TIMER_KILL)
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
                win32gui.DestroyWindow(hwnd)
            elif wp == TIMER_HAND_OFF:
                winapi.user32.KillTimer(hwnd, TIMER_HAND_OFF)
                if self.status == REMOTE and self.proc and self.proc.poll() is None:
                    self.proc.kill()
            return 0
        if msg == win32con.WM_GETMINMAXINFO:
            info = ctypes.cast(lp, ctypes.POINTER(winapi.MINMAXINFO)).contents
            info.ptMinTrackSize.x = round(MIN_SIZE_DIP[0] * self.scale)
            info.ptMinTrackSize.y = round(MIN_SIZE_DIP[1] * self.scale)
            return 0
        if msg == WM_DPICHANGED:
            self.scale = (wp & 0xFFFF) / 96
            self._icons.clear()
            r = ctypes.cast(lp, ctypes.POINTER(wt.RECT)).contents
            win32gui.SetWindowPos(hwnd, 0, r.left, r.top, r.right - r.left, r.bottom - r.top,
                                  win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
            return 0
        if msg == WM_APP_EMBED:
            if wp == self._gen:
                self._embed(lp)
            return 0
        if msg in (WM_APP_FAILED, WM_APP_EXITED):
            self._lost(wp, exited=msg == WM_APP_EXITED)
            return 0
        if msg == win32con.WM_CLOSE:
            self._save_rect()
            if self.surface:
                self.closing = True
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                self._drop_surface()
                win32gui.DestroyWindow(hwnd)
                return 0
            if self.proc and self.proc.poll() is None and self.child:
                if not self.closing:
                    self.closing = True
                    self.manager.plugins.closed(self)
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)  # feels instant; scrcpy tidies up behind
                    win32gui.PostMessage(self.child, win32con.WM_CLOSE, 0, 0)
                    winapi.user32.SetTimer(hwnd, TIMER_KILL, 3000, None)
                return 0
            self.closing = True
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
            win32gui.DestroyWindow(hwnd)
            return 0
        if msg == win32con.WM_DESTROY:
            for t in (TIMER_SAMPLE, TIMER_SPLASH, TIMER_FADE, TIMER_KILL, TIMER_RETRY, TIMER_HAND_OFF):
                winapi.user32.KillTimer(hwnd, t)
            if self.protected:
                self.protected.end(remove=True)
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
            self._drop_surface()
            if self.manager.remote:
                self.manager.remote.window_closed(self)  # an app on a phone stays there
            self.manager.plugins.closed(self)
            self.manager.closed(self)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wp, lp)
