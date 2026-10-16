"""Protected screens: Android screens marked FLAG_SECURE, shown and used in an app window anyway.

Apps mark some screens secure: Play's install and purchase sheet, Google's "use a passkey from another
device" QR code, PIN pads. SurfaceFlinger blacks such a window out on any display that isn't secure
itself and, on Android 16, also drops every touch and key sent to it (logcat: "Dropping MOTION event
... {DROP_INPUT | SENSITIVE_FOR_PRIVACY}"). scrcpy's virtual displays are made by the shell user,
which may not make a secure one (that takes CAPTURE_SECURE_VIDEO_OUTPUT), so in an app window such a
screen went black and ignored every click.

The emulator's own multi-display service runs as the system user, though, and makes secure displays
on request (`adb emu multidisplay add`), which the emulator draws on the PC side, where FLAG_SECURE
doesn't apply. So when a secure window turns up on an app's display, the app's tasks move to such a
display of the same size and the window shows that display instead: frames from `adb emu screenrecord
screenshot`, clicks and keys passed on with `input`. That's a few frames a second rather than video,
plenty for a confirmation sheet or a QR code. Once the secure screen is gone, the tasks move back and
scrcpy takes over again.

The secure display also shows Android's secondary home screen, under the app: a display with no task
on it counts as asleep, and a task moved onto a sleeping display stays hidden.

All this is for scrcpy windows. App windows on the emulator's own displays (emuview.py) need none
of it: those displays are secure already. And it only looks when Android says a window got focus or
an activity came to the front (its event log), or now and then to be sure.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import threading
import time
import traceback
from typing import TYPE_CHECKING

import win32api
import win32clipboard
import win32con
import win32gui
from PIL import Image, ImageDraw

from . import adb, emudisplays, winapi
from .appwindow import RUNNING as WINDOW_RUNNING, mix, on_color
from .catalog import DEVICE_HELPER, HELPER
from .emudisplays import emu, emulator_displays
from .log import log
from .paths import ADB_EXE, DATA, SERIAL, android_env
from .vm import RUNNING

if TYPE_CHECKING:
    from .appwindow import AppWindow
    from .manager import WindowManager

CLASS = "AndroidDesktop.Protected"
WM_APP_FRAME = win32con.WM_APP + 30
WM_APP_STATE = win32con.WM_APP + 31
WM_XBUTTONUP = 0x020C
TIMER_RESIZE, TIMER_MOVE = 1, 2
MOVE_EVERY = 0.03  # seconds between forwarded drag moves
SHOTS = DATA / "protected"  # the emulator saves its screenshots here, one folder per display
LOOK_EVERY = 1.5  # seconds between looks while a secure screen is up, and after Android's window events
LOOK_IDLE = 15.0  # ...and otherwise, in case a window turned secure without any event
# Android's event log: a window got input focus, an activity came to the front.
EVENTS = "logcat -b events -v brief -T 1 -s input_focus:I wm_set_resumed_activity:I"

# DisplayManager.VIRTUAL_DISPLAY_FLAG_*: what scrcpy asks for, plus SECURE and system decorations.
FLAGS = (1 << 0      # PUBLIC
         | 1 << 2    # SECURE: FLAG_SECURE windows are drawn and get input
         | 1 << 3    # OWN_CONTENT_ONLY
         | 1 << 6    # SUPPORTS_TOUCH
         | 1 << 7    # ROTATES_WITH_CONTENT
         | 1 << 9    # SHOULD_SHOW_SYSTEM_DECORATIONS: Android puts a home screen on it, keeping it awake
         | 1 << 10   # TRUSTED
         | 1 << 11   # OWN_DISPLAY_GROUP
         | 1 << 12   # ALWAYS_UNLOCKED
         | 1 << 13   # TOUCH_FEEDBACK_DISABLED
         | 1 << 14)  # OWN_FOCUS

OPENING, SHOWING, FAILED, ENDING = "opening", "showing", "failed", "ending"

# Displays with a FLAG_SECURE window on screen, as "mDisplayId=N" lines.
SECURE_QUERY = ("dumpsys window windows | awk '/^  Window #/{s=0} /mDisplayId=/{d=$1} "
                "/ fl=.*[ =]SECURE( |$)/{s=1} /isOnScreen=true/{if (s) print d}' | sort -u")

# Keys that type nothing, as Android key codes. Characters arrive as WM_CHAR and are typed as text.
VK_TO_KEYCODE = {
    win32con.VK_BACK: 67, win32con.VK_DELETE: 112, win32con.VK_RETURN: 66, win32con.VK_TAB: 61,
    win32con.VK_ESCAPE: 111, win32con.VK_LEFT: 21, win32con.VK_UP: 19, win32con.VK_RIGHT: 22,
    win32con.VK_DOWN: 20, win32con.VK_HOME: 122, win32con.VK_END: 123, win32con.VK_PRIOR: 92,
    win32con.VK_NEXT: 93,
}
KEYCODE_BACK, KEYCODE_SPACE, KEYCODE_SHIFT_LEFT, KEYCODE_CTRL_LEFT, KEYCODE_A = 4, 62, 59, 113, 29

_reset_lock = threading.Lock()
_views: dict[int, Protected] = {}
_class_registered = False


# ---------------------------------------------------------------------- reading what Android says
def parse_secure_displays(out: str) -> set[int]:
    """SECURE_QUERY's answer: the displays showing a FLAG_SECURE window."""
    return {int(n) for n in re.findall(r"mDisplayId=(\d+)", out)}


def parse_root_tasks(stack_list: str, display: int) -> list[int]:
    """`cmd activity stack list`: the root tasks on a display, top first."""
    return [int(t) for t, d in re.findall(r"RootTask id=(\d+) .*?displayId=(\d+)", stack_list) if int(d) == display]


def parse_metrics(out: str) -> tuple[int, int, int]:
    """`wm size; wm density`: width, height and density (an override, if set, is listed last)."""
    w, h = re.findall(r"size: (\d+)x(\d+)", out)[-1]
    return int(w), int(h), int(re.findall(r"density: (\d+)", out)[-1])


def text_commands(text: str) -> list[tuple[str, ...]]:
    """`input` arguments that type text. `input text` takes ASCII only and turns "%s" into a space,
    so spaces and % go on their own."""
    out: list[tuple[str, ...]] = []
    run = ""
    for c in text:
        if c in " %":
            if run:
                out.append(("text", shlex.quote(run)))
                run = ""
            out.append(("keyevent", str(KEYCODE_SPACE)) if c == " " else ("text", "%"))
        elif 32 < ord(c) < 127:
            run += c
    if run:
        out.append(("text", shlex.quote(run)))
    return out


# ---------------------------------------------------------------------- device queries
def secure_displays() -> set[int]:
    return parse_secure_displays(adb.shell(SECURE_QUERY, timeout=10))


def root_tasks(display: int) -> list[int]:
    return parse_root_tasks(adb.shell("cmd activity stack list", timeout=10), display)


def display_metrics(display: int) -> tuple[int, int, int]:
    return parse_metrics(adb.shell(f"wm size -d {display}; wm density -d {display}", timeout=10))


def reset_secure_state() -> None:
    """Undo a sticky "every layer is secure" state SurfaceFlinger can get into after a screenshot of
    a secure layer, which turns every app window black and deaf (see helper/src/SecureReset.java)."""
    with _reset_lock:
        command = f"CLASSPATH={DEVICE_HELPER} app_process / SecureReset"
        out = adb.shell(command, timeout=15)
        if "ok" not in out:  # an older helper, or none yet since Android started
            adb.run("push", str(HELPER), DEVICE_HELPER, timeout=30)
            out = adb.shell(command, timeout=15)
        if "ok" not in out:
            log(f"protected: SecureReset: {out.strip() or 'no answer'}")


def reset_secure_state_soon() -> None:
    """After moving a task: Android snapshots the tasks it moved a moment later, and a snapshot of
    one with a secure window is what sets the state off."""
    def run() -> None:
        for delay in (0.7, 1.5):
            time.sleep(delay)
            try:
                reset_secure_state()
            except Exception as e:
                log(f"protected: SecureReset: {e!r}")
    threading.Thread(target=run, daemon=True, name="secure-reset").start()


# ---------------------------------------------------------------------- watcher
class SecureScreens:
    """Watches the app windows' displays for secure windows and hands each one to a Protected."""

    def __init__(self, manager: WindowManager) -> None:
        self.manager = manager
        self._wake = threading.Event()
        self._events: subprocess.Popen | None = None

    def start(self) -> None:
        threading.Thread(target=self._watch, daemon=True, name="secure-watch").start()

    def _watch(self) -> None:
        tidy = self.manager.ipc  # a test harness leaves the agent's displays alone
        due, follow_ups = 0.0, 0
        while True:
            if self._wake.wait(max(0.0, due - time.monotonic())):
                self._wake.clear()
                time.sleep(0.3)  # let the new window settle
                follow_ups = 2  # and look again a little later: secure windows may take a moment
            due = time.monotonic() + LOOK_EVERY
            if self.manager.vm.state != RUNNING:
                tidy = self.manager.ipc
                self._stop_events()
                continue
            # Windows on the emulator's own displays need none of this: those displays are secure.
            wins = [w for w in list(self.manager.windows.values()) if not w.closing and w.status == WINDOW_RUNNING
                    and w.child and w.display_id is not None and w.surface is None]
            if not wins and not tidy:
                self._stop_events()
                continue
            try:
                if tidy:
                    self._remove_strays()
                    tidy = False
                if not wins:
                    continue
                self._start_events()
                secure = secure_displays()
                for w in wins:
                    self._check(w, secure)
            except Exception as e:
                log(f"protected: watch: {e!r}")
            if follow_ups:
                follow_ups -= 1
            elif not any(w.protected for w in wins):
                due = time.monotonic() + LOOK_IDLE

    def _start_events(self) -> None:
        if self._events and self._events.poll() is None:
            return
        self._events = proc = adb.start("shell", EVENTS, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        stdin=subprocess.DEVNULL)
        threading.Thread(target=self._read_events, args=(proc,), daemon=True, name="secure-events").start()

    def _read_events(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            if not line.startswith(b"-"):  # "--------- beginning of events"
                self._wake.set()

    def _stop_events(self) -> None:
        if self._events and self._events.poll() is None:
            self._events.kill()
        self._events = None

    def _check(self, w: AppWindow, secure: set[int]) -> None:
        p = w.protected
        if p is None:
            if w.display_id in secure:
                log(f"{w.app.package}: secure window on display {w.display_id}")
                self.manager.call(lambda: Protected.begin(w))
            return
        if p.state not in (SHOWING, FAILED):
            return
        if w.display_id != p.source or (p.display if p.state == SHOWING else p.source) not in secure:
            p.misses += 1
            # Wait a moment, as one secure screen often follows another, unless the app is gone.
            if p.misses >= 2 or (p.state == SHOWING and not p.app_tasks()):
                self.manager.call(p.end)
        else:
            p.misses = 0

    @staticmethod
    def _remove_strays() -> None:
        """Emulator displays (app windows', secure screens') left over from an agent that didn't get to
        tidy up."""
        stray = set(emulator_displays()) - emudisplays.ours()
        for slot in stray:
            log(f"protected: removing leftover emulator display {slot}")
            try:
                emu("multidisplay", "del", str(slot))
            except Exception as e:
                log(f"protected: {e}")


# ---------------------------------------------------------------------- one protected screen
class Protected:
    """An app window's secure screen, moved to a secure emulator display and shown through a view of
    our own that takes the place of scrcpy's window. Lives on the window thread; adb work runs on
    helper threads."""

    @classmethod
    def begin(cls, win: AppWindow) -> None:
        if win.protected is None and not win.closing and win.child and win.display_id is not None:
            win.protected = cls(win)

    def __init__(self, win: AppWindow) -> None:
        self.win = win
        self.source = win.display_id  # scrcpy's display
        self.display: int | None = None  # the secure display's Android id, once it exists
        self.slot = 0  # ...and its number in the emulator
        self.density = 160
        self.size = (0, 0)
        self.home: set[int] = set()  # root tasks the secure display has of its own (its home screen)
        self.state = OPENING
        self.misses = 0
        self.frame: Image.Image | None = None
        self.message = "Opening a protected screen..."
        self._lock = threading.Lock()  # opening and closing take turns
        self._box = (0, 0, 1.0)  # where the frame sits in the view: x, y, scale
        self._badge_until = 0.0
        self._shell: subprocess.Popen | None = None
        self._down = False
        self._last_move = 0.0
        self._pending_move: tuple[int, int] | None = None
        self.hwnd = self._create_view()
        threading.Thread(target=self._open, daemon=True, name=f"protect-{win.app.package}").start()

    @property
    def input_display(self) -> int:
        """Where the window's Back button should go."""
        return self.display if self.state == SHOWING and self.display is not None else self.source

    def app_tasks(self) -> list[int]:
        """The app's root tasks on the secure display, top first."""
        return [t for t in root_tasks(self.display) if t not in self.home] if self.display is not None else []

    # ------------------------------------------------------------------ opening and closing
    def _open(self) -> None:
        """Helper thread: make a secure display like scrcpy's and move the app's tasks onto it."""
        with self._lock:
            try:
                w, h, self.density = display_metrics(self.source)
                self.slot = emudisplays.claim()
                self.size = (w, h)
                emudisplays.add(self.slot, w, h, self.density, FLAGS)
                end = time.monotonic() + 10
                while (display := emulator_displays().get(self.slot)) is None or not root_tasks(display):
                    if time.monotonic() > end:
                        raise RuntimeError("the secure display didn't come up")
                    time.sleep(0.25)
                self.display = display
                self.home = set(root_tasks(display))
                tasks = root_tasks(self.source)
                if self.state != OPENING:  # the window is closing: _close tidies up
                    return
                if not tasks:  # the app finished meanwhile
                    self.state = ENDING
                    self._drop_display()
                    return
                for task in reversed(tasks):  # bottom first, so they keep their order
                    adb.shell(f"am display move-stack {task} {display}", timeout=10)
                if not set(tasks) <= set(self.app_tasks()):
                    raise RuntimeError(f"tasks {tasks} didn't move to display {display}")
                reset_secure_state_soon()
                self._shell = subprocess.Popen([str(ADB_EXE), "-s", SERIAL, "shell"], stdin=subprocess.PIPE,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               env=android_env(), creationflags=adb.CREATE_NO_WINDOW)
                log(f"{self.win.app.package}: secure screen on display {display} (emulator display "
                    f"{self.slot}, {w}x{h}/{self.density}), tasks {tasks}")
                if self.state == OPENING:
                    self.state = SHOWING
                    threading.Thread(target=self._frames, daemon=True, name=f"frames-{self.slot}").start()
            except Exception as e:
                log(f"{self.win.app.package}: can't show the secure screen: {e!r}")
                try:
                    self._return_tasks(remove=False)
                except Exception as e2:
                    log(f"{self.win.app.package}: moving the app back: {e2!r}")
                self._drop_display()
                if self.state == OPENING:
                    self.state = FAILED
                    self.message = ("Android keeps this screen private, so it can't be shown or used here. "
                                    "Close the window to leave it, then open the app again.")
            finally:
                self._post(WM_APP_STATE)

    def end(self, remove: bool = False) -> None:
        """Window thread: back to scrcpy. remove: the window is closing, so close the app's tasks too,
        the way scrcpy's display takes its apps with it."""
        if self.state == ENDING:
            return
        self.state = ENDING
        if self._shell:
            self._shell.kill()
        # Not a daemon: quitting waits for it, so the app's tasks and the display don't linger.
        threading.Thread(target=self._close, args=(remove,), name=f"unprotect-{self.win.app.package}").start()
        if remove or not self.win.child:
            self._finish()  # nothing to go back to: don't wait

    def _close(self, remove: bool) -> None:
        with self._lock:
            try:
                self._return_tasks(remove)
            except Exception as e:
                log(f"{self.win.app.package}: leaving the secure screen: {e!r}")
            self._drop_display()
        self._post(WM_APP_STATE)

    def _return_tasks(self, remove: bool) -> None:
        tasks = self.app_tasks()
        if not tasks:
            return
        back = not remove and self.win.display_id == self.source
        for task in reversed(tasks):
            if remove:
                adb.shell(f"am stack remove {task}", timeout=10)
            elif back:
                adb.shell(f"am display move-stack {task} {self.source}", timeout=10)
        reset_secure_state_soon()
        # Tasks still on the secure display move to Android's main screen when it goes away.
        log(f"{self.win.app.package}: tasks {tasks} "
            f"{'closed' if remove else f'back on display {self.source}' if back else 'left to Android'}")

    def _drop_display(self) -> None:
        if not self.slot:
            return
        try:
            emudisplays.remove(self.slot)
        except Exception as e:
            log(f"protected: {e}")
        self.slot, self.display = 0, None

    def _finish(self) -> None:
        """Window thread: take the view down and show scrcpy again."""
        if self.win.protected is self:
            self.win.protected = None
        if self.hwnd:
            _views.pop(self.hwnd, None)
            if win32gui.IsWindow(self.hwnd):
                win32gui.DestroyWindow(self.hwnd)
            self.hwnd = 0
        if not self.win.closing and self.win.child and win32gui.IsWindow(self.win.child):
            win32gui.ShowWindow(self.win.child, win32con.SW_SHOW)
            if self.win.active:
                winapi.user32.SetFocus(self.win.child)

    def _post(self, msg: int) -> None:
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, msg, 0, 0)
            except win32gui.error:
                pass

    # ------------------------------------------------------------------ frames
    def _frames(self) -> None:
        """Helper thread: screenshots of the secure display, as fast as the emulator takes them."""
        slot = self.slot
        folder = SHOTS / str(slot)
        folder.mkdir(parents=True, exist_ok=True)
        # The console splits its arguments at spaces, and a user name may have one.
        target = win32api.GetShortPathName(str(folder))
        while self.state == SHOWING:
            if win32gui.IsIconic(self.win.hwnd):
                time.sleep(0.3)
                continue
            for old in folder.glob("*.png"):
                old.unlink(missing_ok=True)
            try:
                emu("screenrecord", "screenshot", "--display", str(slot), target)
                shot = next(folder.glob("*.png"), None)
                if shot:
                    with Image.open(shot) as im:
                        self.frame = im.convert("RGB")
                    if not self._badge_until:
                        self._badge_until = time.monotonic() + 4
                    self._post(WM_APP_FRAME)
            except Exception as e:
                if self.state == SHOWING:
                    log(f"protected: screenshot failed: {e!r}")
                    time.sleep(0.5)

    # ------------------------------------------------------------------ the view
    def _create_view(self) -> int:
        global _class_registered
        hinst = win32api.GetModuleHandle(None)
        if not _class_registered:
            wc = win32gui.WNDCLASS()
            wc.lpszClassName = CLASS
            wc.hInstance = hinst
            wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
            wc.lpfnWndProc = _view_proc
            win32gui.RegisterClass(wc)
            _class_registered = True
        x, y, w, h = self._area()
        hwnd = win32gui.CreateWindowEx(0, CLASS, "", win32con.WS_CHILD | win32con.WS_CLIPSIBLINGS,
                                       x, y, w, h, self.win.hwnd, 0, hinst, None)
        _views[hwnd] = self
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
        # scrcpy only shows black now; hide it, as its GPU surface could draw over the view.
        win32gui.ShowWindow(self.win.child, win32con.SW_HIDE)
        if self.win.active:
            winapi.user32.SetFocus(hwnd)
        return hwnd

    def _area(self) -> tuple[int, int, int, int]:
        _, _, cw, ch = win32gui.GetClientRect(self.win.hwnd)
        return 0, self.win.bar, cw, max(ch - self.win.bar, 1)

    def place(self) -> None:
        """Follow the app window's layout; once the window settles, resize the secure display to match."""
        if not self.hwnd:
            return
        x, y, w, h = self._area()
        win32gui.SetWindowPos(self.hwnd, 0, x, y, w, h, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        if self.state == SHOWING and (w, h) != self.size:
            winapi.user32.SetTimer(self.hwnd, TIMER_RESIZE, 400, None)

    def _resize(self) -> None:
        _, _, w, h = self._area()
        if self.state != SHOWING or (w, h) == self.size or w < 50 or h < 50:
            return
        self.size = (w, h)
        slot, density = self.slot, self.density

        def resize() -> None:
            try:
                emudisplays.resize(slot, w, h, density, FLAGS)
            except Exception as e:
                log(f"protected: {e}")
        threading.Thread(target=resize, daemon=True).start()

    def _render(self) -> Image.Image:
        _, _, w, h = win32gui.GetClientRect(self.hwnd)
        bg = self.win.bg
        fg = on_color(bg)
        s = self.win.scale
        im = Image.new("RGB", (max(w, 1), max(h, 1)), bg)
        d = ImageDraw.Draw(im)
        frame = self.frame
        if frame is not None and self.state == SHOWING:
            fw, fh = frame.size
            k = min(w / fw, h / fh)
            size = (max(1, round(fw * k)), max(1, round(fh * k)))
            x, y = (w - size[0]) // 2, (h - size[1]) // 2
            im.paste(frame if size == frame.size else frame.resize(size, Image.BILINEAR), (x, y))
            self._box = (x, y, k)
            if time.monotonic() < self._badge_until:  # say why it's a little less smooth than usual
                text_font = winapi.font("text", max(9, round(12 * s)))
                label = "Protected screen"
                pw, ph = round(d.textlength(label, font=text_font) + 24 * s), round(26 * s)
                px, py = (w - pw) // 2, h - ph - round(16 * s)
                d.rounded_rectangle((px, py, px + pw, py + ph), radius=ph // 2, fill=(40, 40, 44))
                d.text((w / 2, py + ph / 2), label, font=text_font, fill=(240, 240, 242), anchor="mm")
            return im
        text_font = winapi.font("text", max(9, round(13 * s)))
        lines, line = [], ""
        for word in self.message.split():
            if line and d.textlength(f"{line} {word}", font=text_font) > w - 48 * s:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        lines.append(line)
        step = round(20 * s)
        top = h * 0.42 - step * (len(lines) - 1) / 2
        for i, text in enumerate(lines):
            d.text((w / 2, top + i * step), text, font=text_font, fill=mix(bg, fg, 0.7), anchor="mm")
        return im

    # ------------------------------------------------------------------ input
    def _input(self, *args: object, source: str = "") -> None:
        """Run `input` on the secure display, through one long-lived adb shell so clicks keep their order."""
        shell = self._shell
        if self.state != SHOWING or not shell or shell.poll() is not None:
            return
        try:
            words = ["input", source, "-d", self.display, *args] if source else ["input", "-d", self.display, *args]
            shell.stdin.write(" ".join(map(str, words)).encode() + b"\n")
            shell.stdin.flush()
        except OSError:
            pass

    def _point(self, x: int, y: int) -> tuple[int, int]:
        """A point in the view, on the secure display."""
        bx, by, k = self._box
        frame = self.frame
        fw, fh = frame.size if frame else self.size
        return min(max(round((x - bx) / k), 0), fw - 1), min(max(round((y - by) / k), 0), fh - 1)

    @staticmethod
    def _xy(lp: int) -> tuple[int, int]:
        x, y = lp & 0xFFFF, lp >> 16 & 0xFFFF
        return x - (x & 0x8000) * 2, y - (y & 0x8000) * 2  # signed

    def _type(self, text: str) -> None:
        for args in text_commands(text):
            self._input(*args)

    def _key(self, vk: int) -> bool:
        alt = win32api.GetKeyState(win32con.VK_MENU) < 0
        ctrl = win32api.GetKeyState(win32con.VK_CONTROL) < 0 and not alt  # Ctrl+Alt is AltGr: a character
        if ctrl and vk == ord("V"):  # paste the PC's clipboard
            try:
                win32clipboard.OpenClipboard()
                try:
                    text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
                self._type(text)
            except Exception:
                pass
            return True
        if ctrl and ord("A") <= vk <= ord("Z"):
            self._input("keycombination", KEYCODE_CTRL_LEFT, KEYCODE_A + vk - ord("A"))
            return True
        code = VK_TO_KEYCODE.get(vk)
        if code is None:
            return False
        if vk == win32con.VK_TAB and win32api.GetKeyState(win32con.VK_SHIFT) < 0:
            self._input("keycombination", KEYCODE_SHIFT_LEFT, code)
        else:
            self._input("keyevent", code)
        return True

    def _move(self) -> None:
        if self._down and self._pending_move:
            self._input("motionevent", "MOVE", *self._pending_move)
            self._pending_move, self._last_move = None, time.monotonic()

    def wndproc(self, hwnd: int, msg: int, wp: int, lp: int) -> int:
        if msg == win32con.WM_PAINT:
            hdc, ps = win32gui.BeginPaint(hwnd)
            try:
                winapi.blit(hdc, self._render())
            finally:
                win32gui.EndPaint(hwnd, ps)
            return 0
        if msg == win32con.WM_PRINTCLIENT:  # the window's bar takes its color from here
            winapi.blit(wp, self._render())
            return 0
        if msg == win32con.WM_ERASEBKGND:
            return 1
        if msg == WM_APP_FRAME:
            win32gui.InvalidateRect(hwnd, None, False)
            return 0
        if msg == WM_APP_STATE:
            if self.state == ENDING:
                self._finish()
            else:
                self.place()
                win32gui.InvalidateRect(hwnd, None, False)
            return 0
        if msg == win32con.WM_TIMER:
            winapi.user32.KillTimer(hwnd, wp)
            if wp == TIMER_RESIZE:
                self._resize()
            elif wp == TIMER_MOVE:
                self._move()
            return 0
        if msg == win32con.WM_LBUTTONDOWN:
            winapi.user32.SetFocus(hwnd)
            win32gui.SetCapture(hwnd)
            self._down = True
            self._input("motionevent", "DOWN", *self._point(*self._xy(lp)))
            return 0
        if msg == win32con.WM_MOUSEMOVE and self._down:
            # Forward drags at a steady pace; a flood of moves would only queue up in adb.
            self._pending_move = self._point(*self._xy(lp))
            if time.monotonic() - self._last_move >= MOVE_EVERY:
                self._move()
            else:
                winapi.user32.SetTimer(hwnd, TIMER_MOVE, round(MOVE_EVERY * 1000), None)
            return 0
        if msg == win32con.WM_LBUTTONUP and self._down:
            self._down = False
            win32gui.ReleaseCapture()
            self._input("motionevent", "UP", *self._point(*self._xy(lp)))
            return 0
        if msg == win32con.WM_CAPTURECHANGED and self._down:
            self._down = False
            self._input("motionevent", "CANCEL", 0, 0)
            return 0
        if msg == win32con.WM_RBUTTONUP:  # like the app windows: right-click goes Back...
            self._input("keyevent", KEYCODE_BACK)
            return 0
        if msg == WM_XBUTTONUP:  # ...and so does the mouse's back button
            if wp >> 16 & 0xFFFF == 1:
                self._input("keyevent", KEYCODE_BACK)
            return 1
        if msg == win32con.WM_MOUSEWHEEL:
            x, y = win32gui.ScreenToClient(hwnd, self._xy(lp))
            notches = ((wp >> 16 & 0xFFFF) - (wp >> 16 & 0x8000) * 2) / 120
            self._input("scroll", *self._point(x, y), "--axis", f"VSCROLL,{notches:g}", source="mouse")
            return 0
        if msg == win32con.WM_KEYDOWN and self._key(wp):
            return 0
        if msg == win32con.WM_CHAR:
            ctrl_only = win32api.GetKeyState(win32con.VK_CONTROL) < 0 and win32api.GetKeyState(win32con.VK_MENU) >= 0
            if wp >= 32 and wp != 127 and not ctrl_only:
                self._type(chr(wp))
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wp, lp)


_reported: set[tuple[int, str]] = set()


def _view_proc(hwnd: int, msg: int, wp: int, lp: int) -> int:
    view = _views.get(hwnd)
    if view is None:
        return win32gui.DefWindowProc(hwnd, msg, wp, lp)
    try:
        return view.wndproc(hwnd, msg, wp, lp)
    except Exception as e:
        if (msg, type(e).__name__) not in _reported:  # once per kind: WM_PAINT would repeat it all the time
            _reported.add((msg, type(e).__name__))
            log(f"protected: message {msg:#06x} failed:\n{traceback.format_exc().rstrip()}")
        return win32gui.DefWindowProc(hwnd, msg, wp, lp)
