"""Windows Hello for Android's fingerprint prompts.

Android in the VM has a fingerprint sensor of its own, a virtual one: the emulator console's
`finger touch <id>` puts finger <id> on it, `finger remove` lifts it. So once a fingerprint is
enrolled in Android (with finger FINGER), a fingerprint prompt can be passed from the PC: when one
comes up, Windows Hello asks the user, above the app window that's asking, and if it's them we touch
the sensor with the enrolled finger.

What's waiting for a finger: Android's BiometricScheduler, in `dumpsys fingerprint`, runs one client
at a time: an authentication (a BiometricPrompt in an app, Play purchases, passkeys, the lock
screen) or an enrollment (Settings adding a fingerprint). Its owner is the package that asked. The
scheduler logs each operation as it's added, starts and finishes, so we look only then: logcat,
filtered to the scheduler's own tags inside Android, wakes us. (Not on every line that mentions
biometrics: apps and SystemUI ask BiometricService "can I authenticate?" all the time, and every new
Java process logs its biometrics flags.)
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import win32gui

from . import adb, hello
from .log import log
from .vm import RUNNING

if TYPE_CHECKING:
    from .appwindow import AppWindow
    from .manager import WindowManager

FINGER = 1  # the finger we enroll and touch with
LOCK_SCREEN = "com.android.systemui"  # owner of the lock screen's fingerprint listening
ENROLL_WINDOW = 10 * 60  # seconds "Set up fingerprint" keeps touching for enrollment
HEALTH = 10.0  # seconds between checks that the log watcher still runs (no dumpsys unless something was logged)
# The scheduler's operations and enrollment changes (system_server), and the prompt dialog (SystemUI).
LOG_TAGS = ("BiometricScheduler", "BiometricSchedulerOperation", "BiometricStateCallback", "AuthSession",
            "AuthController")
LOGCAT = "logcat -v brief -T 1 -s " + " ".join(f"{tag}:V" for tag in LOG_TAGS)
WAKE = re.compile(r"^[VDIWEF]/(" + "|".join(LOG_TAGS) + r")\b")

# BiometricSchedulerOperation states (State: N)
WAITING_IN_QUEUE, WAITING_FOR_COOKIE, STARTED = 0, 1, 2


# ---------------------------------------------------------------------- reading what Android says
@dataclass(frozen=True)
class Operation:
    """What the fingerprint sensor is doing for whom."""
    seq: int  # the scheduler's id for this client: one per prompt or enrollment
    kind: str  # "auth", "enroll" or "other"
    owner: str  # the package that asked
    started: bool  # waiting for a finger now (a BiometricPrompt client first waits for its dialog)


def parse_operation(dumpsys: str) -> Operation | None:
    """The scheduler's current operation in `dumpsys fingerprint`, e.g.
    "Current operation: {[12] com.android...FingerprintAuthenticationClient, proto=3, owner=com.foo,
    cookie=0, requestId=4, userId=0}, State: 2". None if it's idle."""
    m = re.search(r"^Current operation: \{\[(\d+)\] ([\w.$]+),(.*)$", dumpsys, re.M)
    if not m:
        return None
    seq, cls, rest = int(m.group(1)), m.group(2).rsplit(".", 1)[-1], m.group(3)
    kind = "auth" if cls.endswith("AuthenticationClient") else "enroll" if cls.endswith("EnrollClient") else "other"
    owner = re.search(r"owner=([\w.]+)", rest)
    state = re.search(r"State: (\w+)", rest)
    started = state is None or state.group(1) in (str(STARTED), "STATE_STARTED")
    return Operation(seq, kind, owner.group(1) if owner else "", started)


def parse_enrolled(dumpsys: str) -> int | None:
    """How many fingerprints are enrolled, from `dumpsys fingerprint`'s stats:
    {"service":"FingerprintProvider/default","prints":[{"id":0,"count":1,...}]}. None if not listed."""
    m = re.search(r'"prints":\[(.*?)\]', dumpsys)
    if not m:
        return None
    counts = re.findall(r'"count":(\d+)', m.group(1))
    return sum(map(int, counts)) if counts else None


def parse_prompt_display(windows: str) -> int | None:
    """`dumpsys window windows`: the display of SystemUI's BiometricPrompt dialog, e.g.
    "  Window #8 Window{2899270 u0 BiometricPrompt}:" then "    mDisplayId=0 mSession=...". None if
    there's none."""
    m = re.search(r" BiometricPrompt\}:\s*\n\s*mDisplayId=(\d+)", windows)
    return int(m.group(1)) if m else None


def parse_app_displays(stack_list: str, package: str) -> list[int]:
    """`cmd activity stack list`: the displays where package's activity is on top of a visible task,
    then those where it is anywhere, first seen first."""
    top, anywhere = [], []
    display = None
    for line in stack_list.splitlines():
        m = re.match(r"RootTask id=\d+ .*?displayId=(\d+)", line)
        if m:
            display = int(m.group(1))
            continue
        task = re.match(r"\s+taskId=\d+: (\S+)", line)
        if not task or display is None:
            continue
        top_activity = re.search(r"topActivity=ComponentInfo\{([^/}]+)/", line)
        if top_activity and top_activity.group(1) == package and "visible=true" in line:
            top.append(display)
        elif task.group(1).split("/")[0] == package or (top_activity and top_activity.group(1) == package):
            anywhere.append(display)
    return list(dict.fromkeys(top + anywhere))


# ---------------------------------------------------------------------- device actions
def fingerprint_state() -> tuple[Operation | None, int | None]:
    out = adb.shell("dumpsys fingerprint", timeout=10)
    return parse_operation(out), parse_enrolled(out)


def touch() -> bool:
    """Put the enrolled finger on Android's sensor, then lift it."""
    ok = adb.run("emu", "finger", "touch", str(FINGER), timeout=10).stdout.strip().endswith("OK")
    time.sleep(0.25)
    adb.run("emu", "finger", "remove", timeout=10)
    return ok


# ---------------------------------------------------------------------- the bridge
class Bridge:
    """Watches Android's fingerprint sensor: answers prompts with Windows Hello, and puts the finger
    down for "Set up fingerprint"."""

    def __init__(self, manager: WindowManager, label: Callable[[str], str],
                 fallback_window: Callable[[], int]) -> None:
        """label: an app's name from its package; fallback_window: a window to show Windows Hello
        above when no app window is asking (the launcher's)."""
        self.manager = manager
        self.label = label
        self.fallback_window = fallback_window
        self.available = False  # Windows Hello is set up on this PC
        self.enrolled: int | None = None  # fingerprints enrolled in Android; None until known
        self.enrolling_until = 0.0  # "Set up fingerprint" is under way until then
        self.listeners: list[Callable[[], None]] = []
        self.on_notice: Callable[[str], None] = log  # something the user should hear about
        self._wake = threading.Event()
        self._handled: set[int] = set()  # operations we've asked Windows Hello about
        self._touched: dict[int, int] = {}  # enrollment touches given, per operation
        self._logcat: subprocess.Popen | None = None
        self.current: Operation | None = None  # what the sensor was doing at the last look
        self._spawn = lambda fn, *args: threading.Thread(target=fn, args=args, daemon=True, name="hello").start()

    def start(self) -> None:
        threading.Thread(target=self._watch, daemon=True, name="fingerprint").start()

    def enroll(self) -> None:
        """'Set up fingerprint': touch the sensor whenever Android's enrollment asks, for a while."""
        self.enrolling_until = time.monotonic() + ENROLL_WINDOW
        self._wake.set()
        self._changed()

    @property
    def enrolling(self) -> bool:
        return time.monotonic() < self.enrolling_until

    def _changed(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    # ------------------------------------------------------------------ watching
    def _watch(self) -> None:
        try:
            self.available = hello.available()
        except Exception as e:
            log(f"fingerprint: Windows Hello: {e!r}")
        log(f"fingerprint: Windows Hello {'available' if self.available else 'not available'}")
        self._changed()
        while True:
            if self.manager.vm.state != RUNNING:
                self._stop_logcat()
                time.sleep(2)
                continue
            if self._ensure_logcat():
                self._wake.set()  # look once: something may have started before we listened
            if not self._wake.wait(HEALTH):
                continue
            self._wake.clear()
            time.sleep(0.15)  # let a burst of log lines settle
            try:
                self._check()
            except Exception as e:
                log(f"fingerprint: {e!r}")
                time.sleep(2)

    def _ensure_logcat(self) -> bool:
        """Start the log watcher if it isn't running. True if it was just (re)started."""
        if self._logcat and self._logcat.poll() is None:
            return False
        # -T 1: from now on. -s with the tags: logd itself drops everything else.
        self._logcat = adb.start("shell", LOGCAT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._read_logcat, args=(self._logcat,), daemon=True, name="fingerprint-log").start()
        return True

    def _read_logcat(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            if WAKE.search(line):
                self._wake.set()

    def _stop_logcat(self) -> None:
        if self._logcat and self._logcat.poll() is None:
            self._logcat.kill()
        self._logcat = None

    def _check(self) -> None:
        op, enrolled = fingerprint_state()
        self.current = op
        if enrolled != self.enrolled:
            before, self.enrolled = self.enrolled, enrolled
            log(f"fingerprint: {enrolled} enrolled in Android")
            if before is not None and enrolled is not None and enrolled > before:
                if self.enrolling:
                    self.on_notice("Fingerprint added. When an Android app asks for it, confirm with Windows Hello.")
                self.enrolling_until = 0.0  # done
            self._changed()
        if op is None:
            return
        if op.kind == "enroll" and self.enrolling:
            self._enroll_touch(op)
        elif op.kind == "auth" and op.started and op.seq not in self._handled and self.available and enrolled:
            self._handled.add(op.seq)
            self._spawn(self._prompt, op)  # the watcher keeps watching, to see the prompt go away

    def _enroll_touch(self, op: Operation) -> None:
        """Android's enrollment wants the finger several times: give it once per check, until it's done."""
        given = self._touched.get(op.seq, 0)
        if given >= 30:
            return
        self._touched[op.seq] = given + 1
        if given == 0:
            log(f"fingerprint: enrolling for {op.owner}: touching the sensor")
        touch()
        time.sleep(0.4)
        self._wake.set()  # look again right away

    # ------------------------------------------------------------------ prompts
    def _prompt(self, op: Operation) -> None:
        if op.owner == LOCK_SCREEN:
            what, window = "Unlock Android", None
        else:
            what, window = f"Confirm it's you in {self.label(op.owner)} (Android)", self._window_for(op.owner)
        hwnd = window.hwnd if window else self.fallback_window()
        if not hwnd:  # nothing of ours on screen: a Windows Hello prompt out of nowhere would only puzzle
            log(f"fingerprint: {op.owner} asks for a fingerprint, but no window of ours is open")
            return
        log(f"fingerprint: {op.owner} asks for a fingerprint (operation {op.seq}); asking Windows Hello")
        # Asked about once a second: answered from the watcher's last look, which the scheduler's
        # log lines keep current, rather than a dumpsys each time.
        result = hello.verify(hwnd, what, wanted=lambda: self.current is not None and self.current.seq == op.seq)
        if not self._still_asking(op):
            log("fingerprint: the prompt went away meanwhile")
            return
        if result != hello.VERIFIED:
            log(f"fingerprint: Windows Hello: {hello.RESULTS.get(result, result)}")
            self._cancel(op)
            return
        touch()
        log(f"fingerprint: Windows Hello verified; touched the sensor for {op.owner}")

    @staticmethod
    def _still_asking(op: Operation) -> bool:
        try:
            now, _ = fingerprint_state()
        except Exception:
            return True  # can't tell: keep asking
        return now is not None and now.seq == op.seq

    def _cancel(self, op: Operation) -> None:
        """Windows Hello said no: say no in Android too, if its dialog is where nobody sees it.
        Android draws BiometricPrompt on its main display, which no window shows; left alone it
        keeps the app waiting until another app comes to the front."""
        if op.owner == LOCK_SCREEN:
            return
        display = parse_prompt_display(adb.shell("dumpsys window windows", timeout=10))
        shown = {d for w in list(self.manager.windows.values())
                 for d in (w.display_id, w.protected.display if w.protected else None) if d is not None}
        if display is None or display in shown:
            return
        adb.shell(f"input -d {display} keyevent 4", timeout=10)  # Back: the dialog's own cancel
        time.sleep(0.5)
        if self._still_asking(op):
            # Behind Android's own lock screen (still up on the main display after a PIN unlock from
            # the launcher) the dialog gets no keys. Android ends it when the app leaves the front.
            log(f"fingerprint: Android's prompt for {op.owner} stays until the app gives up or leaves the front")
        else:
            log(f"fingerprint: canceled Android's prompt for {op.owner}")

    def _window_for(self, package: str) -> AppWindow | None:
        """The app window showing package, else the one the user is in."""
        wins = [w for w in list(self.manager.windows.values()) if not w.closing and w.hwnd]
        try:
            displays = parse_app_displays(adb.shell("cmd activity stack list", timeout=10), package)
        except Exception:
            displays = []
        for display in displays:
            for w in wins:
                if display in (w.display_id, w.protected.display if w.protected else None):
                    return w
        for w in wins:
            if w.app.package == package:
                return w
        fg = win32gui.GetForegroundWindow()
        return next((w for w in wins if w.hwnd == fg), None) or next((w for w in wins if w.active), None)
