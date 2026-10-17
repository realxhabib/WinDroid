"""App windows on the emulator's own displays: Android's frames without any video encoding.

The fallback, scrcpy, makes a virtual display for each app, and Android encodes it to H.264 in software
(media.swcodec) for scrcpy to decode and draw: most of a CPU core inside Android for each window that
moves, and a few frames of lag. The emulator can make displays of its own, though (emudisplays.py),
which it composes on the PC's GPU anyway. Their frames come straight from the emulator's gRPC API
(emugrpc.py: streamScreenshot, written into shared memory) and go onto the window with Direct3D
(d3d.py), 1:1. Nothing is encoded or decoded.

Touches go to the emulator's touchscreen for that display (gRPC). Keys, text and the mouse wheel go to
InputHelper (helper/src/InputHelper.java), which injects them with the display's id: the emulator has
just one keyboard, which Android would send to whichever display has focus.

The emulator's displays are secure, so FLAG_SECURE screens (Play's purchase sheet, PIN pads) show and
take input as they are: protected.py is for scrcpy windows only.

The emulator wakes every screenshot stream whenever any of its displays draws, and reads each one's
display back from the GPU, changed or not: with a few windows open, a scrolling one would slow down
for all the others. So only the window in use, and windows whose app is drawing something new, stream;
the others look for something new a few times a second instead (Surface.wake, _poll).

This needs an emulator started with -grpc and with its (hidden) Qt window, as vm.py starts it: a
windowless emulator streams its extra displays at the main display's size. Without one, with every
display slot taken, or without Direct3D, app windows use scrcpy.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

import win32api
import win32clipboard
import win32con
import win32gui
from PIL import Image

from . import adb, emudisplays, emugrpc, state, winapi
from .catalog import HELPER
from .emudisplays import (ALWAYS_UNLOCKED, DESTROY_CONTENT_ON_REMOVAL, OWN_CONTENT_ONLY, OWN_DISPLAY_GROUP,
                          OWN_FOCUS, PUBLIC, SECURE, SUPPORTS_TOUCH, TOUCH_FEEDBACK_DISABLED,
                          TRUSTED)
from .log import log
from .paths import SERIAL

if TYPE_CHECKING:
    from .appwindow import AppWindow
    from .catalog import App

# scrcpy's flags for its app displays, plus SECURE. DESTROY_CONTENT_ON_REMOVAL: closing the window
# closes the app, as with scrcpy. Not ROTATES_WITH_CONTENT: the emulator hands over the display's
# physical picture, so an app asking for landscape in a portrait window (or a portrait-only app in a
# wide one) would show sideways. Without it Android shows such an app upright, letterboxed, and
# shaping the window to suit lets it fill the window.
FLAGS = (PUBLIC | SECURE | OWN_CONTENT_ONLY | SUPPORTS_TOUCH | DESTROY_CONTENT_ON_REMOVAL | TRUSTED |
         OWN_DISPLAY_GROUP | ALWAYS_UNLOCKED | TOUCH_FEEDBACK_DISABLED | OWN_FOCUS)
KEEP_FREE = 1  # display slots left for protected screens of scrcpy windows
DEVICE_INPUT_JAR = "/data/local/tmp/androiddesktop-input.jar"  # its own copy: another checkout may push appcatalog.jar
INPUT_VERSION = 3
VIEW_CLASS = "AndroidDesktop.EmuView"
FIRST_FRAME_SECS = 8
RESIZE_DEBOUNCE_MS = 60
HOVER_EVERY = 1 / 60  # seconds between forwarded mouse moves (no button down)
LIVE_SECS = 2.0  # a window not in use stops streaming once its app hasn't drawn anything new for this long...
POLL_SECS = 0.33  # ...and looks for something new this often instead
TIMER_RESIZE = 1
WM_XBUTTONUP, WM_MOUSEHWHEEL, WM_MOUSELEAVE = 0x020C, 0x020E, 0x02A3
LAUNCH_FLAGS = 0x10200000  # FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_RESET_TASK_IF_NEEDED, as a launcher starts apps

user32, kernel32, msvcrt = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.CDLL("msvcrt")
msvcrt.memcmp.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
kernel32.CreateFileMappingW.restype = wt.HANDLE
kernel32.CreateFileMappingW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR]
kernel32.MapViewOfFile.restype = ctypes.c_void_p
kernel32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]

# Windows virtual keys -> Android key codes, for keys that type nothing, and letters and digits for
# shortcuts (Ctrl+C). Characters are typed as text (WM_CHAR), so the PC's keyboard layout applies.
VK_TO_KEYCODE = {
    win32con.VK_BACK: 67, win32con.VK_DELETE: 112, win32con.VK_RETURN: 66, win32con.VK_TAB: 61,
    win32con.VK_ESCAPE: 111, win32con.VK_LEFT: 21, win32con.VK_UP: 19, win32con.VK_RIGHT: 22,
    win32con.VK_DOWN: 20, win32con.VK_HOME: 122, win32con.VK_END: 123, win32con.VK_PRIOR: 92,
    win32con.VK_NEXT: 93, win32con.VK_INSERT: 124, win32con.VK_SPACE: 62, win32con.VK_APPS: 82,
    win32con.VK_LSHIFT: 59, win32con.VK_RSHIFT: 60, win32con.VK_LCONTROL: 113, win32con.VK_RCONTROL: 114,
    win32con.VK_LMENU: 57, win32con.VK_RMENU: 58, win32con.VK_LWIN: 117, win32con.VK_RWIN: 118,
    win32con.VK_CAPITAL: 115, win32con.VK_NUMLOCK: 143, win32con.VK_SCROLL: 116,
    win32con.VK_VOLUME_MUTE: 164, win32con.VK_VOLUME_DOWN: 25, win32con.VK_VOLUME_UP: 24,
    win32con.VK_MEDIA_PLAY_PAUSE: 85, win32con.VK_MEDIA_NEXT_TRACK: 87, win32con.VK_MEDIA_PREV_TRACK: 88,
    **{win32con.VK_F1 + i: 131 + i for i in range(12)},
    **{ord("A") + i: 29 + i for i in range(26)},
    **{ord("0") + i: 7 + i for i in range(10)},
}
MODIFIERS = {win32con.VK_SHIFT, win32con.VK_CONTROL, win32con.VK_MENU, win32con.VK_LSHIFT, win32con.VK_RSHIFT,
             win32con.VK_LCONTROL, win32con.VK_RCONTROL, win32con.VK_LMENU, win32con.VK_RMENU, win32con.VK_LWIN,
             win32con.VK_RWIN}
KEYCODE_BACK, KEYCODE_A, KEYCODE_V, KEYCODE_MOVE_END, KEYCODE_CTRL_LEFT = 4, 29, 50, 123, 113
META_CTRL = 0x3000  # META_CTRL_ON | META_CTRL_LEFT_ON
# KeyEvent.META_*: (virtual key, flags while it's down)
META = ((win32con.VK_LSHIFT, 0x41), (win32con.VK_RSHIFT, 0x81), (win32con.VK_LCONTROL, 0x3000),
        (win32con.VK_RCONTROL, 0x5000), (win32con.VK_LMENU, 0x12), (win32con.VK_RMENU, 0x22),
        (win32con.VK_LWIN, 0x30000), (win32con.VK_RWIN, 0x50000))
LOCKS = ((win32con.VK_CAPITAL, 0x100000), (win32con.VK_NUMLOCK, 0x200000), (win32con.VK_SCROLL, 0x400000))


class Unusable(RuntimeError):
    """The emulator's displays can't show this window: use scrcpy instead."""


# ---------------------------------------------------------------------- finding the emulator's gRPC
def parse_discovery(text: str) -> dict[str, str]:
    """An emulator discovery file (avd/running/pid_N.ini): key=value lines."""
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def console_port() -> int | None:
    m = re.fullmatch(r"emulator-(\d+)", SERIAL)
    return int(m.group(1)) if m else None


def _alive(pid: int) -> bool:
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = wt.DWORD()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return code.value == 259  # STILL_ACTIVE


def endpoint() -> tuple[int, str] | None:
    """Our emulator's gRPC port and token, from the file it advertises itself in. None if it serves
    no gRPC, or runs without a window (its extra displays then stream at the wrong size)."""
    port = console_port()
    folder = Path(tempfile.gettempdir()) / "avd" / "running"
    found = []
    for path in folder.glob("pid_*.ini"):
        try:
            info = parse_discovery(path.read_text(encoding="utf-8", errors="replace"))
            pid = int(path.stem[4:])
        except (OSError, ValueError):
            continue
        if info.get("port.serial") == str(port) and _alive(pid):
            found.append((path.stat().st_mtime, info))
    if not found:
        return None
    info = max(found, key=lambda f: f[0])[1]
    if "grpc.port" not in info or '"-no-window"' in info.get("cmdline", ""):
        return None
    return int(info["grpc.port"]), info.get("grpc.token", "")


# ---------------------------------------------------------------------- shared by all windows
class Link:
    """The emulator's gRPC endpoint and the input helper, shared by every app window."""

    def __init__(self) -> None:
        self.disabled = ""  # why emulator displays aren't used for the rest of this Android session
        self._disabled_for: tuple[int, str] | None = None
        self._grpc_lock = threading.Lock()
        self._helper_lock = threading.Lock()
        self._channel: emugrpc.Channel | None = None
        self._touches: emugrpc.Call | None = None
        self._helper: subprocess.Popen | None = None

    def usable(self) -> tuple[int, str] | None:
        """The endpoint to use for a new window, if emulator displays may be used now."""
        if (state.get("app_windows") or "auto") == "scrcpy":
            return None
        found = endpoint()
        if found is None:
            return None
        if self._disabled_for == found:
            return None
        return found

    def disable(self, point: tuple[int, str], reason: str) -> None:
        log(f"emulator displays: not used until Android restarts: {reason}")
        self._disabled_for, self.disabled = point, reason

    # ------------------------------------------------------------------ touches, clipboard (gRPC)
    def _control(self, point: tuple[int, str]) -> emugrpc.Channel:
        """The connection for input, to this emulator (a restarted one has a new token). Hold _grpc_lock."""
        channel = self._channel
        if channel is None or channel.closed.is_set() or (channel.port, channel.token) != point:
            if channel:
                channel.close()
            self._channel, self._touches = emugrpc.Channel(*point), None
        return self._channel

    def touch(self, point: tuple[int, str], slot: int, x: int, y: int, down: bool) -> None:
        event = emugrpc.touch_event(slot, emugrpc.touch(x, y, 0, 1 if down else 0))
        for attempt in range(2):
            try:
                with self._grpc_lock:
                    channel = self._control(point)
                    if self._touches is None or self._touches.done.is_set():
                        self._touches = channel.call("streamInputEvent")
                    self._touches.send(event)
                return
            except (OSError, emugrpc.GrpcError) as e:
                with self._grpc_lock:
                    self._touches = None
                if attempt:
                    log(f"emulator displays: touch: {e!r}")

    def set_clipboard(self, point: tuple[int, str], text: str) -> None:
        try:
            with self._grpc_lock:
                channel = self._control(point)
            channel.unary("setClipboard", emugrpc.pb((1, text)), timeout=3)
        except (OSError, emugrpc.GrpcError) as e:
            log(f"emulator displays: clipboard: {e!r}")

    # ------------------------------------------------------------------ keys, text, wheel (InputHelper)
    def helper(self, line: str) -> None:
        with self._helper_lock:
            for attempt in range(2):
                proc = self._helper
                if proc is None or proc.poll() is not None:
                    try:
                        proc = self._helper = self._start_helper()
                    except Exception as e:
                        log(f"emulator displays: input helper: {e!r}")
                        return
                try:
                    proc.stdin.write((line + "\n").encode())
                    proc.stdin.flush()
                    return
                except OSError:
                    self._helper = None

    @staticmethod
    def _start_helper() -> subprocess.Popen:
        for attempt in range(2):
            # It ends when its stdin does, i.e. when we let go of it or this process ends.
            proc = adb.start("shell", f"CLASSPATH={DEVICE_INPUT_JAR} app_process / InputHelper",
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            first = proc.stdout.readline().decode(errors="replace").strip()
            if first == f"ready {INPUT_VERSION}":
                threading.Thread(target=Link._drain, args=(proc,), daemon=True, name="input-helper").start()
                return proc
            proc.kill()
            if attempt == 0:  # not there yet, or an older one: (re)install it
                adb.run("push", str(HELPER), DEVICE_INPUT_JAR, timeout=30)
        raise RuntimeError(f"InputHelper didn't start: {first or 'no answer'}")

    @staticmethod
    def _drain(proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            log(f"emulator displays: {line.decode(errors='replace').strip()}")

    def close(self) -> None:
        """Android is going away, or we are."""
        with self._helper_lock:
            if self._helper and self._helper.poll() is None:
                self._helper.kill()
            self._helper = None
        with self._grpc_lock:
            if self._channel:
                self._channel.close()
            self._channel = self._touches = None
        self._disabled_for, self.disabled = None, ""


link = Link()


def meta_state() -> int:
    state_ = 0
    for vk, flags in META:
        if win32api.GetKeyState(vk) < 0:
            state_ |= flags
    for vk, flags in LOCKS:
        if win32api.GetKeyState(vk) & 1:
            state_ |= flags
    return state_


def key_of(vk: int, lp: int) -> int:
    """The virtual key, told left from right for Shift, Ctrl and Alt."""
    if vk == win32con.VK_SHIFT:
        return user32.MapVirtualKeyW(lp >> 16 & 0xFF, 3) or win32con.VK_LSHIFT  # MAPVK_VSC_TO_VK_EX
    extended = lp >> 24 & 1
    if vk == win32con.VK_CONTROL:
        return win32con.VK_RCONTROL if extended else win32con.VK_LCONTROL
    if vk == win32con.VK_MENU:
        return win32con.VK_RMENU if extended else win32con.VK_LMENU
    return vk


# ---------------------------------------------------------------------- one window's display
_views: dict[int, Surface] = {}
_class_registered = False
_reported: set[tuple[int, str]] = set()


class Surface:
    """An app window's emulator display: the display itself, its frames on a child window of ours,
    and the input that goes back. Opened on a worker thread, attached on the window thread."""

    def __init__(self, win: AppWindow, point: tuple[int, str], gen: int) -> None:
        self.win = win
        self.point = point  # gRPC (port, token)
        self.gen = gen  # the window's launch this belongs to
        self.slot = 0
        self.display: int | None = None  # Android's id for it
        self.size = (0, 0)  # the display's size now, in pixels
        self.density = 160
        self.view = 0
        self.frame = (0, 0)  # the size of the frame in shared memory
        self.frames = 0
        self.first_frame = threading.Event()
        self.closed = False
        self._renderer = None
        self._mapping = 0
        self._pixels = 0
        self._capacity = 0
        self._handle = ""
        self._channel: emugrpc.Channel | None = None
        self._stream: emugrpc.Call | None = None
        self._stream_token: object | None = None  # the stream whose frames count; None while not streaming
        self._mode_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._shown: ctypes.Array | None = None  # our copy of the frame on screen
        self._last_change = 0.0
        self._wake_until = 0.0
        self._restarts = 0
        self._want: tuple[tuple[int, int], int] | None = None
        self._resizing = False
        self._down = False
        self._last = (0, 0)
        self._hovering = False
        self._hovered = 0.0
        self._keys_down: set[int] = set()
        self._repeats: dict[int, int] = {}
        self._high_surrogate = ""
        self._close_lock = threading.Lock()

    # ------------------------------------------------------------------ opening (worker thread)
    def display_size(self, width: int, height: int, density: int) -> tuple[int, int]:
        """The display for a view of this size: as big, but no smaller than the emulator allows (a
        smaller window then shows its top left), nor bigger than our shared memory."""
        low = emudisplays.min_px(density)
        cap_w, cap_h = self._screen()
        return min(max(width, low), cap_w), min(max(height, low), cap_h)

    @staticmethod
    def _screen() -> tuple[int, int]:
        return user32.GetSystemMetrics(78) + 64, user32.GetSystemMetrics(79) + 64  # SM_C[XY]VIRTUALSCREEN

    def open(self, app: App, view_size: tuple[int, int], density: int) -> int:
        """Make the display and start the app on it. Returns Android's id for the display."""
        try:
            self.slot = emudisplays.claim(KEEP_FREE)
        except emudisplays.NoSlot as e:
            raise Unusable(str(e)) from None
        self.density = density
        self.size = self.display_size(*view_size, density)
        self.display = emudisplays.add(self.slot, *self.size, density, FLAGS)
        if self.closed:  # the window closed meanwhile, maybe before the display was there to remove
            emudisplays.remove(self.slot)
            raise Unusable("closed")
        link.helper(f"ime {self.display}")  # an on-screen keyboard, if one shows, shows in the window
        out = adb.shell(f"am start --display {self.display} -a android.intent.action.MAIN "
                        f"-c android.intent.category.LAUNCHER -f {LAUNCH_FLAGS} -n {app.package}/{app.activity}",
                        timeout=20)
        if "Error" in out:
            raise RuntimeError(out.strip().splitlines()[-1])
        return self.display

    # ------------------------------------------------------------------ the view (window thread)
    def attach(self, parent: int, rect: tuple[int, int, int, int]) -> int:
        """Our child window for the frames, hidden until the first frame is there, and the stream."""
        global _class_registered
        from .d3d import Renderer
        hinst = win32api.GetModuleHandle(None)
        if not _class_registered:
            wc = win32gui.WNDCLASS()
            wc.lpszClassName = VIEW_CLASS
            wc.hInstance = hinst
            wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
            wc.lpfnWndProc = _view_proc
            win32gui.RegisterClass(wc)
            _class_registered = True
        x, y, w, h = rect
        self.view = win32gui.CreateWindowEx(0, VIEW_CLASS, "", win32con.WS_CHILD | win32con.WS_CLIPSIBLINGS,
                                            x, y, w, h, parent, 0, hinst, None)
        _views[self.view] = self
        try:
            self._renderer = Renderer(self.view, w, h)
        except OSError as e:
            raise Unusable(f"Direct3D: {e}") from None
        self._renderer.background = tuple(c / 255 for c in self.win.bg[:3]) + (1.0,)
        cap_w, cap_h = self._screen()
        self._capacity = cap_w * cap_h * 4
        name = f"androiddesktop-{os.getpid()}-{self.slot}-{id(self) & 0xFFFFFF:x}"
        # A pagefile-backed mapping; the emulator opens "SHM_" + the handle we pass.
        self._mapping = kernel32.CreateFileMappingW(wt.HANDLE(-1), None, 0x04, 0, self._capacity, "SHM_" + name)
        self._pixels = kernel32.MapViewOfFile(self._mapping, 0xF001F, 0, 0, self._capacity) if self._mapping else 0
        if not self._pixels:
            raise Unusable(f"shared memory: error {ctypes.GetLastError()}")
        self._handle = name
        self._go_live()  # the app is starting: it draws
        threading.Thread(target=self._poll, daemon=True, name=f"frames-{self.slot}").start()
        return self.view

    # ------------------------------------------------------------------ frames
    def _connect(self) -> emugrpc.Channel:
        if self._channel is None or self._channel.closed.is_set():
            self._channel = emugrpc.Channel(*self.point)
        return self._channel

    @property
    def live(self) -> bool:
        return self._stream_token is not None

    def wake(self) -> None:
        """Any thread: the user is at this window: stream, for a while at least."""
        self._wake_until = time.monotonic() + LIVE_SECS
        if not self.live and not self.closed:
            self._go_live()

    def _go_live(self) -> None:
        with self._mode_lock:
            if self.live or self.closed:
                return
            token = self._stream_token = object()
            try:
                self._stream = self._connect().call(
                    "streamScreenshot", emugrpc.image_format(self.slot, emugrpc.RGBA8888, handle=self._handle),
                    on_message=lambda m: self._on_stream_frame(token, m), on_end=lambda e: self._on_end(token, e))
                return
            except (OSError, emugrpc.GrpcError) as e:
                error = f"can't connect: {e!r}"
        self._on_end(token, error)

    def _go_idle(self) -> None:
        with self._mode_lock:
            call, self._stream, self._stream_token = self._stream, None, None
        if call:
            call.cancel()

    def _keep_live(self) -> bool:
        return (self.win.active or self._down or time.monotonic() < self._wake_until) and \
            not win32gui.IsIconic(self.win.hwnd)

    def _on_stream_frame(self, token: object, message: memoryview) -> None:
        """The stream's thread: a frame is in shared memory."""
        if token is not self._stream_token:
            return
        width, height, _, _, _ = emugrpc.parse_image(message)
        self._restarts = 0
        if not self._take(width, height) and not self._keep_live() and \
                time.monotonic() - self._last_change > LIVE_SECS:
            self._go_idle()

    def _take(self, width: int, height: int) -> bool:
        """Show the frame in shared memory, if it's new. True if it was."""
        size = width * height * 4
        if self.closed or not size or size > self._capacity:
            return False
        with self._frame_lock:
            if self.closed:
                return False
            if (width, height) == self.frame and msvcrt.memcmp(self._shown, self._pixels, size) == 0:
                return False
            if self._shown is None or len(self._shown) < size:
                self._shown = ctypes.create_string_buffer(size)
            # Our own copy: the emulator may write the next frame into shared memory any time.
            ctypes.memmove(self._shown, self._pixels, size)
            self.frame = (width, height)
            self.frames += 1
            self._last_change = time.monotonic()
            self._renderer.draw(ctypes.addressof(self._shown), width, height)
        self.first_frame.set()
        return True

    def _poll(self) -> None:
        """Thread: while not streaming, look for a new frame a few times a second."""
        request = emugrpc.image_format(self.slot, emugrpc.RGBA8888, handle=self._handle)
        while not self.closed:
            time.sleep(POLL_SECS)
            if self.live or self.closed or win32gui.IsIconic(self.win.hwnd):
                continue
            try:
                reply = self._connect().unary("getScreenshot", request, timeout=5)
            except (OSError, emugrpc.GrpcError) as e:
                if not self.closed and not self._display_exists():
                    log(f"{self.win.app.package}: emulator display {self.slot} lost: {e}")
                    self.win.surface_lost(self)
                    return
                continue
            fmt = emugrpc.unpb(emugrpc.first(reply, 1, b""))
            if self._take(emugrpc.first(fmt, 3), emugrpc.first(fmt, 4)):
                self._go_live()

    def _display_exists(self) -> bool:
        try:
            return emudisplays.emulator_displays().get(self.slot) == self.display
        except Exception:
            return True  # can't tell: don't give up on it

    def _on_end(self, token: object, error: str) -> None:
        if self.closed or token is not self._stream_token:
            return  # closing, or a stream we stopped on purpose
        with self._mode_lock:
            self._stream, self._stream_token = None, None
        # The emulator is still there and so is the display: just stream again.
        if self._restarts < 3 and self.display is not None:
            self._restarts += 1
            time.sleep(0.2 * self._restarts)
            if self._display_exists():
                log(f"{self.win.app.package}: frames stopped ({error or 'ended'}); streaming again")
                self._go_live()
                return
        log(f"{self.win.app.package}: emulator display {self.slot} lost: {error or 'stream ended'}")
        self.win.surface_lost(self)

    def redraw(self) -> None:
        with self._frame_lock:
            if self._renderer and self._shown is not None and self.frame[0]:
                self._renderer.draw(ctypes.addressof(self._shown), *self.frame)

    def capture(self, width: int, height: int) -> Image.Image | None:
        """The top left of the frame on screen, e.g. for the window bar's color."""
        with self._frame_lock:
            fw, fh = self.frame
            if not fw or self._shown is None:
                return None
            w, h = min(width, fw), min(height, fh)
            data = ctypes.string_at(self._shown, fw * 4 * h)
        return Image.frombuffer("RGBA", (fw, h), data, "raw", "RGBA", 0, 1).crop((0, 0, w, h)).convert("RGB")

    # ------------------------------------------------------------------ size
    def resize(self, width: int, height: int, density: int) -> None:
        """Window thread: the view's new size (and density, if the window moved to another monitor).
        The view follows at once; the display, after a short pause in the resizing."""
        if self._renderer:
            self._renderer.resize(width, height)
        target = (self.display_size(width, height, density), density)
        if target != (self.size, self.density) and self.view:
            self._want = target
            user32.SetTimer(self.view, TIMER_RESIZE, RESIZE_DEBOUNCE_MS, None)

    def _resize_display(self) -> None:
        user32.KillTimer(self.view, TIMER_RESIZE)
        if self._resizing or self._want is None or self.closed:
            if self._want is not None and not self.closed:
                user32.SetTimer(self.view, TIMER_RESIZE, RESIZE_DEBOUNCE_MS, None)
            return
        (size, density), self._want = self._want, None
        self._resizing = True

        def work() -> None:
            try:
                emudisplays.resize(self.slot, *size, density, FLAGS)
                self.size, self.density = size, density
            except Exception as e:
                log(f"{self.win.app.package}: resizing the display: {e}")
            finally:
                self._resizing = False
        threading.Thread(target=work, daemon=True, name=f"resize-{self.slot}").start()

    # ------------------------------------------------------------------ closing
    def close(self) -> None:
        """Any thread: stop the frames and take the display away (and the app with it)."""
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
        self._go_idle()
        if self._channel:
            self._channel.close()
        if self.slot:
            try:
                emudisplays.remove(self.slot)
            except Exception as e:
                if "invalid display id" not in str(e):  # it's gone already (Android went away)
                    log(f"{self.win.app.package}: removing emulator display {self.slot}: {e}")
        with self._frame_lock:  # after this, no frame reads the shared memory
            if self._renderer:
                self._renderer.close()
            if self._pixels:
                kernel32.UnmapViewOfFile(self._pixels)
                self._pixels = 0
        if self._mapping:
            kernel32.CloseHandle(self._mapping)
            self._mapping = 0

    def destroy_view(self) -> None:
        """Window thread."""
        if self.view:
            _views.pop(self.view, None)
            if win32gui.IsWindow(self.view):
                win32gui.DestroyWindow(self.view)
            self.view = 0

    # ------------------------------------------------------------------ input (window thread)
    def _point(self, lp: int) -> tuple[int, int]:
        return self._clamp(ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short(lp >> 16 & 0xFFFF).value)

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        """A point in the view, on the display: the frame is drawn 1:1 from the top left."""
        w, h = self.frame if self.frame[0] else self.size
        return min(max(x, 0), w - 1), min(max(y, 0), h - 1)

    def _touch(self, x: int, y: int, down: bool) -> None:
        self._last = (x, y)
        link.touch(self.point, self.slot, x, y, down)

    def _hover(self, x: int, y: int) -> None:
        """The mouse moved over the view without a button down: Android shows hover states, as with scrcpy."""
        now = time.monotonic()
        if not self._hovering:
            winapi.track_leave(self.view)
            self._hovering = True
        elif now - self._hovered < HOVER_EVERY:
            return
        self._hovered, self._last = now, (x, y)
        link.helper(f"hover {self.display} {x} {y}")

    def _key(self, keycode: int, down: bool, repeat: int = 0) -> None:
        link.helper(f"key {self.display} {0 if down else 1} {keycode} {repeat} {meta_state()}")

    def back(self) -> None:
        self._key(KEYCODE_BACK, True)
        self._key(KEYCODE_BACK, False)

    def _key_down(self, vk: int, lp: int) -> bool:
        vk = key_of(vk, lp)
        ctrl = win32api.GetKeyState(win32con.VK_CONTROL) < 0
        alt = win32api.GetKeyState(win32con.VK_MENU) < 0
        if vk == win32con.VK_F4 and alt:
            return False  # Alt+F4 closes the window, as anywhere
        char = user32.MapVirtualKeyW(vk, 2) & 0x7FFFFFFF  # MAPVK_VK_TO_CHAR; the top bit marks dead keys
        if char >= 32 and vk not in MODIFIERS and ctrl == alt:
            return True  # it types a character (AltGr too): WM_CHAR brings it, in the PC's layout
        code = VK_TO_KEYCODE.get(vk)
        if code is None:
            return False
        repeated = bool(lp >> 30 & 1)
        if repeated and vk in MODIFIERS:
            return True
        if code == KEYCODE_V and ctrl and not alt:  # paste: Android gets the PC's clipboard first
            self._sync_clipboard()
        repeat = self._repeats.get(vk, -1) + 1 if repeated else 0
        self._repeats[vk] = repeat
        self._keys_down.add(vk)
        self._key(code, True, repeat)
        return True

    def _key_up(self, vk: int, lp: int) -> bool:
        vk = key_of(vk, lp)
        if vk not in self._keys_down:
            return vk not in MODIFIERS and vk in VK_TO_KEYCODE
        self._keys_down.discard(vk)
        self._repeats.pop(vk, None)
        self._key(VK_TO_KEYCODE[vk], False)
        return True

    def release_keys(self) -> None:
        """Focus went elsewhere: let go of whatever Android still thinks is held down."""
        for vk in list(self._keys_down):
            self._keys_down.discard(vk)
            self._key(VK_TO_KEYCODE[vk], False)
        self._repeats.clear()

    def _char(self, code: int) -> None:
        if 0xD800 <= code < 0xDC00:  # the first half of a character outside the BMP (emoji)
            self._high_surrogate = chr(code)
            return
        text = (self._high_surrogate + chr(code)).encode("utf-16", "surrogatepass").decode("utf-16") \
            if self._high_surrogate else chr(code)
        self._high_surrogate = ""
        if code >= 32 and code != 127:
            link.helper(f"text {self.display} {text.encode('utf-8').hex()}")

    def _sync_clipboard(self) -> None:
        try:
            win32clipboard.OpenClipboard()
            try:
                text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            return
        link.set_clipboard(self.point, text)

    def _wheel(self, wp: int, lp: int, horizontal: bool) -> None:
        x, y = self._clamp(*win32gui.ScreenToClient(self.view, (ctypes.c_short(lp & 0xFFFF).value,
                                                                ctypes.c_short(lp >> 16 & 0xFFFF).value)))
        notches = ctypes.c_short(wp >> 16 & 0xFFFF).value / 120
        v, h = (0, notches) if horizontal else (notches, 0)
        link.helper(f"scroll {self.display} {x} {y} {v:g} {h:g}")

    def wndproc(self, hwnd: int, msg: int, wp: int, lp: int) -> int:
        if msg == win32con.WM_PAINT:
            _, ps = win32gui.BeginPaint(hwnd)
            win32gui.EndPaint(hwnd, ps)
            self.redraw()
            return 0
        if msg == win32con.WM_ERASEBKGND:
            return 1
        if msg == win32con.WM_TIMER and wp == TIMER_RESIZE:
            self._resize_display()
            return 0
        if msg in (win32con.WM_LBUTTONDOWN, win32con.WM_RBUTTONDOWN, win32con.WM_MOUSEWHEEL, WM_MOUSEHWHEEL,
                   win32con.WM_KEYDOWN, win32con.WM_SYSKEYDOWN, win32con.WM_CHAR):
            self.wake()
        if msg == win32con.WM_LBUTTONDOWN:
            win32gui.SetFocus(hwnd)
            win32gui.SetCapture(hwnd)
            self._down = True
            self._touch(*self._point(lp), True)
            return 0
        if msg == win32con.WM_MOUSEMOVE:
            if self._down:
                self._touch(*self._point(lp), True)
            else:
                self._hover(*self._point(lp))
            return 0
        if msg == WM_MOUSELEAVE:
            if self._hovering:
                self._hovering = False
                link.helper(f"leave {self.display} {self._last[0]} {self._last[1]}")
            return 0
        if msg == win32con.WM_LBUTTONUP:
            if self._down:
                self._down = False
                win32gui.ReleaseCapture()
                self._touch(*self._point(lp), False)
            return 0
        if msg == win32con.WM_CAPTURECHANGED:
            if self._down:
                self._down = False
                self._touch(*self._last, False)
            return 0
        if msg in (win32con.WM_RBUTTONDOWN, win32con.WM_MBUTTONDOWN, win32con.WM_MBUTTONUP):
            win32gui.SetFocus(hwnd)
            return 0
        if msg == win32con.WM_RBUTTONUP:  # right-click goes Back, as in scrcpy windows...
            self.back()
            return 0
        if msg == WM_XBUTTONUP:  # ...and so does the mouse's back button
            if wp >> 16 & 0xFFFF == 1:
                self.back()
            return 1
        if msg in (win32con.WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            self._wheel(wp, lp, msg == WM_MOUSEHWHEEL)
            return 0
        if msg in (win32con.WM_KEYDOWN, win32con.WM_SYSKEYDOWN):
            if self._key_down(wp, lp):
                return 0
            if wp == win32con.VK_F4 and msg == win32con.WM_SYSKEYDOWN:
                win32gui.PostMessage(self.win.hwnd, win32con.WM_CLOSE, 0, 0)
                return 0
        if msg in (win32con.WM_KEYUP, win32con.WM_SYSKEYUP):
            if self._key_up(wp, lp) or msg == win32con.WM_SYSKEYUP:
                return 0  # (a lone Alt would otherwise open the window menu)
        if msg == win32con.WM_CHAR:
            ctrl_only = win32api.GetKeyState(win32con.VK_CONTROL) < 0 and win32api.GetKeyState(win32con.VK_MENU) >= 0
            if not ctrl_only:
                self._char(wp)
            return 0
        if msg == win32con.WM_SYSCHAR and wp != ord(" "):
            return 0  # Alt+letter went to Android as a key; no beep for a missing menu
        if msg == win32con.WM_KILLFOCUS:
            self.release_keys()
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wp, lp)


def _view_proc(hwnd: int, msg: int, wp: int, lp: int) -> int:
    view = _views.get(hwnd)
    if view is None:
        return win32gui.DefWindowProc(hwnd, msg, wp, lp)
    try:
        return view.wndproc(hwnd, msg, wp, lp)
    except Exception as e:
        if (msg, type(e).__name__) not in _reported:  # once per kind: WM_PAINT would repeat it all the time
            _reported.add((msg, type(e).__name__))
            log(f"emuview: message {msg:#06x} failed:\n{traceback.format_exc().rstrip()}")
        return win32gui.DefWindowProc(hwnd, msg, wp, lp)
