"""Android's notifications as Windows notifications: helper/src/NotificationWatch.java reports them,
toast.py shows them. Clicking one opens the app's window and runs the notification's own tap action
there (a chat opens on that conversation); a messaging notification gets a reply box.

Only what would make the phone itself buzz or pop up shows on Windows: nothing that was already there
when WinDroid started, no ongoing ones (music, downloads, navigation), no silent channels, no group
summaries, and nothing from an app whose window is in front.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape, quoteattr

from . import adb, state
from .catalog import HELPER
from .log import log
from .paths import DATA

DEVICE_JAR = "/data/local/tmp/androiddesktop-notify.jar"
VERSION = 1  # NotificationWatch.VERSION
SETTING = "android_notifications"  # state.json: False turns them off
PICTURES = DATA / "notification-pictures"  # senders' photos while their toast is up

# android.app.Notification.FLAG_*
ONGOING, ONLY_ALERT_ONCE, FOREGROUND_SERVICE, GROUP_SUMMARY = 0x2, 0x8, 0x40, 0x200
IMPORTANCE_DEFAULT = 3  # below this, Android shows it without a sound or a heads-up
QUIET_CATEGORIES = {"service", "progress", "transport", "sys", "status", "navigation", "stopwatch", "location_sharing"}
REPLY_ECHO_SECS = 15
SETTLE_SECS = 30  # after the reader starts: apps re-post their notifications as Android boots
SYSTEM_PACKAGES = {"android", "com.android.systemui", "com.android.shell"}


def enabled() -> bool:
    return state.get(SETTING, True) is not False


def worth_showing(n: dict) -> bool:
    """Whether Android would have interrupted for this notification: a sound or a heads-up."""
    flags = int(n.get("flags") or 0)
    if n.get("initial") or n.get("package") in SYSTEM_PACKAGES:
        return False
    if flags & (ONGOING | FOREGROUND_SERVICE | GROUP_SUMMARY):
        return False
    if n.get("update") and flags & ONLY_ALERT_ONCE:
        return False
    if "importance" in n:
        if int(n["importance"]) < IMPORTANCE_DEFAULT:
            return False
    elif int(n.get("priority") or 0) < 0:  # importance unknown: the pre-channels hint
        return False
    if (n.get("category") or "") in QUIET_CATEGORIES:
        return False
    return bool((n.get("title") or "").strip() or (n.get("text") or "").strip())


def listable(n: dict) -> bool:
    """Whether a notification belongs in WinDroid's list of Android's notifications: what Android's
    own shade lists as a notification, silent ones included, but not ongoing ones (music, downloads)."""
    flags = int(n.get("flags") or 0)
    if n.get("package") in SYSTEM_PACKAGES or flags & (ONGOING | FOREGROUND_SERVICE | GROUP_SUMMARY):
        return False
    if int(n.get("importance", 1)) < 1:  # IMPORTANCE_NONE: Android doesn't show it at all
        return False
    return bool((n.get("title") or "").strip() or (n.get("text") or "").strip())


def entry(n: dict, label: str, previous: dict | None = None) -> dict:
    """One notification as the launcher lists it."""
    picture = f"data:image/png;base64,{n['icon']}" if n.get("icon") else (previous or {}).get("picture", "")
    return {"key": n["key"], "pkg": n.get("package", ""), "app": label, "title": (n.get("title") or "").strip(),
            "text": (n.get("text") or "").strip()[:1000], "conversation": n.get("conversation") or "",
            "time": int(n.get("postTime") or 0), "canOpen": bool(n.get("canOpen", True)),
            "canReply": bool(n.get("canReply")), "replyLabel": n.get("replyLabel") or "", "picture": picture}


def tag_for(key: str) -> str:
    """A toast tag for an Android notification key (Windows allows 64 characters; keys run longer)."""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:32]


def toast_xml(n: dict, app_label: str, picture: Path | None, app_icon: Path | None) -> str:
    """The toast for one Android notification: its title and text, the app's name, the sender's photo
    (or the app's icon), and a reply box when Android offers one."""
    title = (n.get("title") or "").strip() or app_label
    text = (n.get("text") or "").strip()
    attribution = app_label
    if n.get("conversation") and n["conversation"] != title:
        attribution = f"{app_label} · {n['conversation']}"
    lines = [f"<text>{escape(title[:200])}</text>"]
    if text:
        lines.append(f"<text>{escape(text[:1000])}</text>")
    lines.append(f'<text placement="attribution">{escape(attribution[:100])}</text>')
    image = picture or app_icon
    if image and image.exists():
        crop = ' hint-crop="circle"' if picture else ""
        lines.append(f'<image placement="appLogoOverride"{crop} src={quoteattr(image.resolve().as_uri())}/>')
    actions = ""
    if n.get("canReply"):
        hint = (n.get("replyLabel") or "").strip() or "Reply"
        actions = ("<actions>"
                   f'<input id="reply" type="text" placeHolderContent={quoteattr(hint[:60])}/>'
                   '<action content="Send" arguments="reply" hint-inputId="reply" activationType="foreground"/>'
                   "</actions>")
    return (f'<toast launch="open" activationType="foreground"><visual><binding template="ToastGeneric">'
            f'{"".join(lines)}</binding></visual>{actions}</toast>')


class Watch:
    """The NotificationWatch process inside Android: its events, and commands to it."""

    def __init__(self, on_event: Callable[[dict], None]) -> None:
        self.on_event = on_event
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._id = 0
        self._waiting: dict[int, tuple[threading.Event, list]] = {}

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.running():
                return
            self._push()
            proc = self._proc = adb.start("shell", f"CLASSPATH={DEVICE_JAR} app_process / NotificationWatch",
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._read, args=(proc,), daemon=True, name="notification-watch").start()

    @staticmethod
    def _push() -> None:
        local = hashlib.md5(HELPER.read_bytes()).hexdigest()
        if local not in adb.shell(f"md5sum {DEVICE_JAR} 2>/dev/null", timeout=15):
            adb.run("push", str(HELPER), DEVICE_JAR, timeout=30, check=True)

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue  # not ours: app_process can print warnings
            if "id" in message and "event" not in message:
                waiter = self._waiting.pop(message["id"], None)
                if waiter:
                    waiter[1].append(message)
                    waiter[0].set()
                continue
            try:
                self.on_event(message)
            except Exception as e:
                log(f"notifications: {e!r}")
        for event, _ in list(self._waiting.values()):
            event.set()
        self._waiting.clear()

    def call(self, cmd: str, timeout: float = 10, **args) -> None:
        """Send a command and wait for it to be done. Raises RuntimeError with Android's reason."""
        with self._lock:
            if not self.running():
                raise RuntimeError("Android's notifications aren't being read right now")
            self._id += 1
            done, answer = threading.Event(), []
            self._waiting[self._id] = (done, answer)
            try:
                self._proc.stdin.write((json.dumps({"id": self._id, "cmd": cmd, **args}) + "\n").encode("utf-8"))
                self._proc.stdin.flush()
            except OSError as e:
                raise RuntimeError(f"the notification reader stopped ({e})") from e
        if not done.wait(timeout) or not answer:
            raise RuntimeError("Android didn't answer")
        if not answer[0].get("ok"):
            raise RuntimeError(answer[0].get("error") or f"{cmd} failed")

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(3)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()


class Notifications:
    """For the agent: runs the watch while Android is up, shows toasts, and acts on clicks and replies.

    label(package) names an app; icon(package) is its icon file; in_front(package) says whether its
    window is the one in front; open(package, then) opens its window and calls then(display) once the
    app shows in it; notice(text) tells the user something went wrong. on_change() runs when the
    list of Android's notifications (items()) changes."""

    def __init__(self, label: Callable[[str], str], icon: Callable[[str], Path | None],
                 in_front: Callable[[str], bool], open: Callable[[str, Callable[[int], None]], None],
                 notice: Callable[[str], None], app_id: str) -> None:
        self.label, self.icon, self.in_front, self.open, self.notice = label, icon, in_front, open, notice
        self.app_id = app_id
        self.watch = Watch(self._event)
        self.toaster = None  # toast.Toaster, made on first use (Windows only)
        self._shown: dict[str, str] = {}  # toast tag -> Android key, for toasts on screen
        self._packages: dict[str, str] = {}  # Android key -> package
        self._wanted = False
        self._replied: dict[str, float] = {}  # Android key -> when the user replied from Windows
        self._active: dict[str, dict] = {}  # Android key -> entry(), for the launcher's list
        self.on_change: Callable[[], None] = lambda: None
        self._lock = threading.Lock()
        self._errors = 0
        self._started = 0.0  # when the reader said hello

    # ------------------------------------------------------------------ lifecycle
    def android_up(self) -> None:
        """Android is running: read its notifications, if the user wants them on Windows."""
        with self._lock:
            if not enabled() or self._wanted:
                return
            self._wanted = True
        threading.Thread(target=self._keep_watching, daemon=True, name="notifications").start()

    def android_down(self, wait: float = 0) -> None:
        """Android stopped (or WinDroid is quitting): its toasts go too. wait: seconds to wait for them."""
        with self._lock:
            self._wanted = False
        self.watch.stop()
        self._forget_all(wait)

    def set_enabled(self, on: bool) -> None:
        state.put(SETTING, bool(on))
        log(f"notifications: Android notifications on Windows: {'on' if on else 'off'}")
        if not on:
            self.android_down()

    def _keep_watching(self) -> None:
        """Start the watch, and again if it ends while wanted (Android's shell can drop)."""
        delay = 2.0
        while True:
            with self._lock:
                if not self._wanted:
                    return
            if not self.watch.running():
                try:
                    self.watch.start()
                    delay = 2.0
                except Exception as e:
                    log(f"notifications: couldn't start reading them: {e!r}")
                    delay = min(delay * 2, 60)
                time.sleep(delay)
            else:
                time.sleep(2)

    # ------------------------------------------------------------------ Android's side
    def _event(self, n: dict) -> None:
        kind = n.get("event")
        if kind == "hello":
            if n.get("version") != VERSION:
                log(f"notifications: the reader is version {n.get('version')}, expected {VERSION}")
            else:
                log("notifications: reading Android's notifications")
            self._started = time.monotonic()
        elif kind == "posted":
            self._packages[n["key"]] = n.get("package", "")
            if listable(n):
                self._active[n["key"]] = entry(n, self.label(n.get("package", "")), self._active.get(n["key"]))
                self._changed()
            elif self._active.pop(n["key"], None) is not None:
                self._changed()
            # An app updates its notification with the reply just sent: no need to pop that up.
            echo = time.monotonic() - self._replied.get(n["key"], -60) < REPLY_ECHO_SECS
            # Right after Android starts, apps post again what they had before: list it, don't pop it up.
            settling = time.monotonic() - self._started < SETTLE_SECS
            if worth_showing(n) and not echo and not settling and not self.in_front(n.get("package", "")):
                self._show(n)
        elif kind == "removed":
            self._packages.pop(n["key"], None)
            self._replied.pop(n["key"], None)
            if self._active.pop(n["key"], None) is not None:
                self._changed()
            tag = tag_for(n["key"])
            if self._shown.pop(tag, None) is not None:
                self._toaster().remove(tag)
                self._picture(tag).unlink(missing_ok=True)
        elif kind == "error":
            self._errors += 1
            if self._errors <= 3:  # once a second otherwise
                log(f"notifications: Android: {n.get('error')}")

    def _show(self, n: dict) -> None:
        package, tag = n.get("package", ""), tag_for(n["key"])
        picture = None
        if n.get("icon"):
            try:
                picture = self._picture(tag)
                picture.parent.mkdir(parents=True, exist_ok=True)
                picture.write_bytes(base64.b64decode(n["icon"]))
            except (OSError, ValueError):
                picture = None
        self._shown[tag] = n["key"]
        self._toaster().show(tag, toast_xml(n, self.label(package), picture, self.icon(package)))
        # The app and when only: what it said stays out of the log.
        log(f"notifications: shown one from {package}")

    def _changed(self) -> None:
        try:
            self.on_change()
        except Exception as e:
            log(f"notifications: {e!r}")

    def items(self) -> list[dict]:
        """Android's notifications now, newest first."""
        return sorted(self._active.values(), key=lambda e: e["time"], reverse=True)

    @staticmethod
    def _picture(tag: str) -> Path:
        return PICTURES / f"{tag}.png"

    def _forget_all(self, wait: float = 0) -> None:
        if self._shown and self.toaster:
            self.toaster.clear(wait)
        self._shown.clear()
        self._packages.clear()
        if self._active:
            self._active.clear()
            self._changed()
        for f in PICTURES.glob("*.png") if PICTURES.exists() else []:
            f.unlink(missing_ok=True)

    def _toaster(self):
        if self.toaster is None:
            from .toast import Toaster
            self.toaster = Toaster(self.app_id, self._activated)
        return self.toaster

    # ------------------------------------------------------------------ the user's side
    def _activated(self, tag: str, arguments: str, reply: str) -> None:
        """A toast clicked, or a reply sent from it."""
        key = self._shown.get(tag)
        if key is None:
            return  # Android dropped it meanwhile
        if arguments == "reply":
            problem = self.reply(key, reply)
            if problem:
                self.notice(problem)
        else:
            self.open_key(key)

    def reply(self, key: str, text: str) -> str:
        """Answer a notification through its reply action. "" once sent, else what went wrong."""
        if not text.strip():
            return ""
        package = self._packages.get(key, "")
        try:
            self._replied[key] = time.monotonic()
            self.watch.call("reply", key=key, text=text)
            log(f"notifications: replied to one from {package}")
            return ""
        except RuntimeError as e:
            return f"Couldn't send that reply to {self.label(package)}: {e}"

    def open_key(self, key: str) -> bool:
        """Open the notification's app in its window, on what the notification is about."""
        package = self._packages.get(key, "")
        if not package:
            return False

        def tap(display: int) -> None:
            try:
                self.watch.call("open", key=key, display=display)
            except RuntimeError as e:
                log(f"notifications: {package}: tap action: {e}")  # the window shows the app anyway
        self.open(package, tap)
        return True
