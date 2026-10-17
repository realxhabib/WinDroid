"""Ghost sessions: Android apps an AI agent runs out of sight, reads, operates, and can hand to the user.

A session's app runs on a virtual display of its own that AgentHelper (helper/src/AgentHelper.java)
makes inside Android: no scrcpy, no window, nothing on the PC's screen. The agent reads the display as
an accessibility tree (uitree.py) and, if it asks, a screenshot from the display's own frames. It taps,
swipes and presses keys with `input -d <display>`, and types with accessibility actions, which reach a
text field without stealing anyone's keyboard focus.

show_to_user turns a session into an ordinary app window: the agent process (app.py) opens one for the
app, scrcpy's launch there brings the app's task over, and AppWindow.adopt moves any task still left
behind. The user picks up exactly where the agent left off, and the session goes on in that window.

Screens marked FLAG_SECURE (PIN pads, passwords, payment sheets) are the user's: while one is up the
session reports "secure screen: needs the user", shows nothing of it and refuses input, and
show_to_user hands it over.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import adb, runtime, uitree
from .agenthelper import DEVICE_JAR, Helper, HelperError
from .catalog import App, parse_list
from .log import agent_log
from .protected import SECURE_QUERY, parse_root_tasks, parse_secure_displays, text_commands

SIZE = (720, 1520)  # a phone's 360x760 dp; small enough that a screenshot needs no scaling
DPI = 320
DISPLAY_PREFIX = "androiddesktop-agent:"  # our displays' names, as `dumpsys display` lists them
KEYS = {"back": 4, "home": 3, "enter": 66}
# After an action: how long the display has to start reacting, how long it must then be still, and the
# most we wait in all.
CHANGE_MS, QUIET_MS, SETTLE_MS = 800, 300, 3000
SECURE = ("secure screen: needs the user. Android marks this screen private (a PIN, password or payment "
          "screen), so it can't be read or operated here. Call show_to_user to hand it to the user, and "
          "read_screen again once they're done.")
LOCKED = ("Android is locked: the user has to unlock it in WinDroid (with their PIN) before apps "
          "can run.")


class GhostError(Exception):
    """Something the agent should be told, in words it can act on."""


@dataclass
class Task:
    root: int
    display: int
    packages: set[str]


def parse_tasks(stack_list: str) -> list[Task]:
    """`cmd activity stack list`: each root task, its display, and the apps in it."""
    tasks = []
    for block in re.split(r"(?m)^(?=RootTask id=)", stack_list):
        m = re.match(r"RootTask id=(\d+) .*?displayId=(\d+)", block)
        if m:
            tasks.append(Task(int(m[1]), int(m[2]), set(re.findall(r"taskId=\d+: ([\w.]+)/", block))))
    return tasks


def parse_agent_displays(dumpsys_display: str) -> dict[int, str]:
    """`dumpsys display`: our hidden displays, display id -> the app on it."""
    return {int(d): pkg for pkg, d in re.findall(
        r'DisplayInfo\{"' + re.escape(DISPLAY_PREFIX) + r'([^"]*)", displayId (\d+)', dumpsys_display)}


def agent_displays() -> dict[int, str]:
    """Hidden displays in use by agents now, in any process: Android isn't idle while there are some."""
    return parse_agent_displays(adb.shell("dumpsys display", timeout=10))


def clamp(point: tuple[int, int], size: tuple[int, int]) -> tuple[int, int]:
    return min(max(point[0], 0), size[0] - 1), min(max(point[1], 0), size[1] - 1)


def visible_center(bounds: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int]:
    """The middle of the part of an element that's on the display."""
    left, top = max(bounds[0], 0), max(bounds[1], 0)
    right, bottom = min(bounds[2], size[0]), min(bounds[3], size[1])
    if right <= left or bottom <= top:
        raise GhostError("That element is off the display; scroll to it first.")
    return (left + right) // 2, (top + bottom) // 2


def swipe_for(direction: str, bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """A swipe within `bounds` that shows what lies further in `direction` (so "down" drags up)."""
    left, top, right, bottom = bounds
    cx, cy = (left + right) // 2, (top + bottom) // 2
    dx, dy = (right - left) * 3 // 10, (bottom - top) * 3 // 10
    return {"down": (cx, cy + dy, cx, cy - dy), "up": (cx, cy - dy, cx, cy + dy),
            "right": (cx + dx, cy, cx - dx, cy), "left": (cx - dx, cy, cx + dx, cy)}[direction]


def describe(n: uitree.Node) -> str:
    label = n.label or n.hint or uitree.short_res(n.res)
    return f'{uitree.short_class(n.cls)} "{label}"' if label else f"{uitree.short_class(n.cls)} #{n.id}"


def ask_agent(request: dict, argv: list[str]) -> None:
    """Hand a request to WinDroid's agent process (it owns the windows and the VM), starting it if it
    isn't running, the way a Start menu shortcut does."""
    from .manager import send_to_agent  # the windowing side: only needed here
    from .winapi import command
    if send_to_agent(request):
        return
    subprocess.Popen(command(*argv), cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


@dataclass
class Session:
    id: str
    package: str
    label: str
    display: int  # where the app is now
    ghost: int | None  # our hidden display; None once the app is in a window the user sees
    size: tuple[int, int] = SIZE
    windows: list[uitree.Window] = field(default_factory=list)  # as last read

    @property
    def hidden(self) -> bool:
        return self.ghost is not None


class Sessions:
    """The agent's sessions, for one MCP server. Not thread-safe: the server handles one call at a time."""

    def __init__(self, helper: Helper | None = None) -> None:
        self.helper = helper or Helper()
        self.helper.on_restart = self._helper_restarted
        self.sessions: dict[str, Session] = {}
        self.ended: dict[str, str] = {}  # sessions that ended on their own, and why
        self._apps: dict[str, App] = {}
        self._count = 0

    # ------------------------------------------------------------------ apps
    def apps(self, refresh: bool = False) -> dict[str, App]:
        if refresh or not self._apps:
            self.ensure_android()
            self.helper.call("hello")  # puts the helper jar on the device, AppCatalog included
            self._apps = parse_list(adb.shell(f"CLASSPATH={DEVICE_JAR} app_process / AppCatalog list", timeout=60))
        return self._apps

    def find_app(self, name: str) -> App:
        """By package, or by the name the user sees (any case; a unique partial match will do)."""
        for refresh in (False, True):
            apps = self.apps(refresh)
            if name in apps:
                return apps[name]
            key = name.casefold().strip()
            exact = [a for a in apps.values() if a.label.casefold() == key]
            partial = [a for a in apps.values() if key in a.label.casefold() or key in a.package.casefold()]
            if exact or len(partial) == 1:
                return (exact or partial)[0]
            if len(partial) > 1:
                names = ", ".join(f"{a.label} ({a.package})" for a in partial[:8])
                raise GhostError(f"{name!r} could be any of: {names}. Say which, by package.")
        raise GhostError(f"No app called {name!r} is installed. list_apps shows what is.")

    # ------------------------------------------------------------------ Android itself
    def ensure_android(self) -> None:
        if not (adb.online() and adb.booted()):
            if not runtime.installed():
                raise GhostError("WinDroid isn't set up yet: ask the user to open it and click Set up "
                                 "Android (a one-time download).")
            agent_log("Android isn't running: asking WinDroid to start it")
            ask_agent({"boot": True}, ["boot"])
            end = time.monotonic() + 300
            while not (adb.online() and adb.booted()):
                if time.monotonic() > end:
                    raise GhostError("Android didn't start within 5 minutes. Ask the user to open WinDroid "
                                     "and check what it says.")
                time.sleep(2)
        if adb.user_locked():
            raise GhostError(LOCKED)

    # ------------------------------------------------------------------ sessions
    def get(self, sid: str, follow: bool = True) -> Session:
        if sid in self.sessions:
            s = self.sessions[sid]
            if s.hidden and not self.helper.running():
                self.helper.call("hello")  # a new helper: the old one's displays, and their apps, are gone
                return self.get(sid, follow)
            if follow and not s.hidden:
                self._follow(s)
            return s
        if sid in self.ended:
            raise GhostError(f"Session {sid} has ended: {self.ended[sid]}")
        raise GhostError(f"No session {sid!r}. start_session starts one.")

    def _follow(self, s: Session) -> None:
        """A session in the user's window goes where its app goes: protected.py moves a secure screen to a
        display of its own, a reconnecting window gets a new one, and closing the window closes the app."""
        ours = {x.ghost for x in self.sessions.values() if x.hidden}
        displays = [t.display for t in parse_tasks(adb.shell("cmd activity stack list", timeout=10))
                    if s.package in t.packages and t.display not in ours | {0}]
        if s.display in displays:
            return
        if not displays:
            del self.sessions[s.id]
            self.ended[s.id] = f"{s.label} isn't open any more (the user closed its window)."
            agent_log(f"{s.id}: {s.package} is gone from the user's windows; session ended")
            raise GhostError(f"Session {s.id} has ended: {self.ended[s.id]}")
        agent_log(f"{s.id}: {s.package} moved from display {s.display} to {displays[0]}")
        s.display, s.windows = displays[0], []
        s.size = self._display_size(s.display)

    def start(self, name: str, visible: bool = False) -> Session:
        self.ensure_android()
        app = self.find_app(name)
        mine = {s.ghost for s in self.sessions.values() if s.hidden}
        for s in self.sessions.values():
            if s.package == app.package:
                raise GhostError(f"{app.label} already has a session: {s.id}.")
        open_in = [t.display for t in parse_tasks(adb.shell("cmd activity stack list", timeout=10))
                   if app.package in t.packages and t.display not in mine | {0}]
        if open_in and set(open_in) & set(agent_displays()):
            raise GhostError(f"Another agent is using {app.label}.")
        if open_in and not visible:
            # Starting it on a display of ours would pull its task out from under the user.
            raise GhostError(f"{app.label} is open in the user's window. Use start_session with visible=true to "
                             "work in that window, or ask the user to close it.")
        if open_in:  # in the user's window already: work there
            session = Session(self._new_id(app), app.package, app.label, open_in[0], None,
                              size=self._display_size(open_in[0]))
        else:
            session = self._start_visible(app) if visible else self._start_hidden(app)
        self.sessions[session.id] = session
        how = "joined in the user's window" if open_in else "started in a window" if visible else "started hidden"
        agent_log(f"{session.id}: {app.package} {how} on display {session.display}")
        return session

    def _new_id(self, app: App) -> str:
        self._count += 1
        return f"{re.sub(r'[^a-z0-9]+', '-', app.label.casefold()).strip('-') or 'app'}-{self._count}"

    def _start_hidden(self, app: App) -> Session:
        display = self.helper.call("create", name=DISPLAY_PREFIX + app.package, width=SIZE[0], height=SIZE[1],
                                   dpi=DPI)["display"]
        try:
            out = adb.shell(f"am start -W --display {display} -n {app.package}/{app.activity}", timeout=60)
            if "Status: ok" not in out:
                raise GhostError(f"{app.label} didn't start: {out.strip()[-300:]}")
            end = time.monotonic() + 10
            while not parse_root_tasks(adb.shell("cmd activity stack list", timeout=10), display):
                if time.monotonic() > end:
                    raise GhostError(f"{app.label} started, but not on its hidden display.")
                time.sleep(0.25)
        except Exception:
            self._release(display)
            raise
        self._settle(display, since=-1)  # it has started drawing: wait for it to finish
        return Session(self._new_id(app), app.package, app.label, display, display)

    def _start_visible(self, app: App) -> Session:
        ask_agent({"open": app.package}, ["open", app.package])
        task = self._wait_for_window(app.package, set(), 60)
        if task is None:
            raise GhostError(f"{app.label}'s window didn't open within a minute.")
        return Session(self._new_id(app), app.package, app.label, task.display, None,
                       size=self._display_size(task.display))

    def _wait_for_window(self, package: str, leaving: set[int], timeout: float) -> Task | None:
        """The app's task on a window's display, once nothing of it is left on `leaving`."""
        end = time.monotonic() + timeout
        ours = {s.ghost for s in self.sessions.values() if s.hidden}
        while time.monotonic() < end:
            tasks = parse_tasks(adb.shell("cmd activity stack list", timeout=10))
            there = [t for t in tasks if package in t.packages and t.display not in ours | leaving | {0}]
            if there and not any(t.display in leaving for t in tasks):
                return there[0]
            time.sleep(0.5)
        return None

    @staticmethod
    def _display_size(display: int) -> tuple[int, int]:
        m = re.findall(r"size: (\d+)x(\d+)", adb.shell(f"wm size -d {display}", timeout=10))
        return (int(m[-1][0]), int(m[-1][1])) if m else SIZE

    def end(self, sid: str) -> str:
        s = self.get(sid, follow=False)
        del self.sessions[sid]
        if not s.hidden:
            agent_log(f"{sid}: ended; {s.package} stays in the user's window")
            return f"Session {sid} ended. {s.label} stays open in the user's window."
        for root in parse_root_tasks(adb.shell("cmd activity stack list", timeout=10), s.ghost):
            adb.shell(f"am stack remove {root}", timeout=10)
        self._release(s.ghost)
        agent_log(f"{sid}: ended; {s.package} closed, display {s.ghost} released")
        return f"Session {sid} ended and {s.label} closed."

    def close(self) -> None:
        for sid in list(self.sessions):
            try:
                self.end(sid)
            except Exception as e:
                agent_log(f"{sid}: ending: {e!r}")
        self.helper.close()

    def _release(self, display: int) -> None:
        try:
            self.helper.call("release", display=display)
        except HelperError as e:
            agent_log(f"releasing display {display}: {e}")

    def _helper_restarted(self) -> None:
        """A new helper has none of the old one's displays: their apps closed with them."""
        for sid, s in list(self.sessions.items()):
            if s.hidden:
                del self.sessions[sid]
                self.ended[sid] = "Android's helper restarted, and the hidden app closed with it. Start a new session."
                agent_log(f"{sid}: lost with the helper")

    # ------------------------------------------------------------------ reading
    def _look(self, s: Session) -> tuple[bool, list[uitree.Window]]:
        """Whether a secure screen is up, and the display's windows. Both at once: each is an adb round trip."""
        secure: dict = {}

        def check() -> None:
            try:
                secure["on"] = s.display in parse_secure_displays(adb.shell(SECURE_QUERY, timeout=10))
            except Exception as e:
                secure["error"] = e

        t = threading.Thread(target=check, daemon=True)
        t.start()
        try:
            windows = uitree.parse(self.helper.call("tree", display=s.display))
        finally:
            t.join()
        if "error" in secure:  # can't tell: don't risk showing or touching a secure screen
            raise GhostError(f"Couldn't check for a secure screen: {secure['error']!r}")
        return secure["on"], windows

    def read(self, sid: str, screenshot: bool = False) -> tuple[dict, dict | None]:
        """The screen as compact JSON, and a screenshot ({"data": base64, "format": "jpeg"}) if asked for."""
        s = self.get(sid)
        secure, windows = self._look(s)
        if secure:
            s.windows = []
            return {"session": s.id, "app": s.label, "secure": True, "message": SECURE}, None
        s.windows = windows
        screen: dict = {"session": s.id, "app": s.label, "package": s.package, "hidden": s.hidden,
                        "size": list(s.size), "windows": uitree.compact(windows)}
        if not windows:
            screen["note"] = ("Nothing on screen: the app has closed (Back from its first screen ends it) or is "
                              "still starting.")
        image = None
        if screenshot:
            if s.hidden:
                try:
                    image = self.helper.call("shot", display=s.ghost, format="jpeg", quality=75)
                except HelperError as e:
                    screen["screenshot"] = f"Not available: {e}"
            else:
                screen["screenshot"] = "Not available while the app is in the user's window; the tree is."
        return screen, image

    def wait_for(self, sid: str, text: str, timeout: float = 10) -> dict:
        end = time.monotonic() + max(0.0, min(timeout, 120))
        while True:
            s = self.get(sid)  # a session in the user's window may move meanwhile
            secure, windows = self._look(s)
            if secure:
                s.windows = []
                return {"found": False, "secure": True, "message": SECURE}
            s.windows = windows
            found = uitree.matches(windows, text)
            if found:
                return {"found": True, "elements": [uitree.element(replace(n, children=[])) for n in found[:5]]}
            if time.monotonic() >= end:
                return {"found": False, "message": f"{text!r} didn't appear within {timeout:g} s."}
            time.sleep(0.25)

    # ------------------------------------------------------------------ acting
    def _node(self, s: Session, element: int) -> uitree.Node:
        node = uitree.nodes(s.windows).get(element)
        if node is None:
            raise GhostError(f"No element {element} on the last screen read. Call read_screen and use its ids.")
        return node

    def _refuse_if_secure(self, s: Session) -> None:
        if s.display in parse_secure_displays(adb.shell(SECURE_QUERY, timeout=10)):
            raise GhostError(SECURE)

    def _input(self, s: Session, args: str) -> None:
        out = adb.shell(f"input -d {s.display} {args}", timeout=15)
        if "Exception" in out or "Error" in out:
            raise GhostError(f"input {args}: {out.strip()[-300:]}")

    def _mark(self, s: Session) -> int | None:
        """Before an action: the display's accessibility event count, to tell its reaction by."""
        try:
            return self.helper.call("mark", display=s.display).get("events", 0)
        except HelperError:
            return None  # no UiAutomation (another client has it)

    def _settle(self, display: int, since: int | None) -> None:
        """After an action: wait for the display to react to it, then to be still."""
        if since is not None:
            try:
                self.helper.call("settle", display=display, since=since, change=CHANGE_MS, quiet=QUIET_MS,
                                 timeout=SETTLE_MS)
                return
            except HelperError:
                pass
        time.sleep(QUIET_MS / 1000)  # can't tell: just give it a moment

    def tap(self, sid: str, element: int | None = None, x: int | None = None, y: int | None = None) -> str:
        s = self.get(sid)
        self._refuse_if_secure(s)
        if element is not None:
            node = self._node(s, element)
            x, y = visible_center(node.bounds, s.size)
            what = describe(node)
        elif x is not None and y is not None:
            x, y = clamp((int(x), int(y)), s.size)
            what = "the screen"
        else:
            raise GhostError("tap needs an element_id, or x and y.")
        mark = self._mark(s)
        self._input(s, f"tap {x} {y}")
        self._settle(s.display, mark)
        return f"Tapped {what} at ({x}, {y})."

    def type_text(self, sid: str, text: str, element: int | None = None, submit: bool = False) -> str:
        s = self.get(sid)
        self._refuse_if_secure(s)
        args: dict = {"display": s.display, "action": "setText", "text": text}
        target = None
        if element is not None:
            node = self._node(s, element)
            # The agent may name a field's row or label: the text goes to the field inside it.
            target = next((n for n in node.walk() if n.flags & uitree.EDITABLE), node)
            args.update(index=target.id, bounds=list(target.bounds))
        mark = self._mark(s)
        try:
            done = self.helper.call("act", **args).get("done")
        except HelperError as e:
            if target is not None or "no text field" not in str(e):
                raise GhostError(str(e)) from e
            done = self._type_keys(s, text)  # no field has focus: type it as keys, as a keyboard would
        if not done:
            raise GhostError("The field didn't take the text. Tap it first, or read_screen to find the field.")
        where = describe(target) if target else "the focused field"
        if submit:
            self._enter(s, target)
        self._settle(s.display, mark)
        return f"Typed {len(text)} characters into {where}{' and pressed Enter' if submit else ''}."

    def _type_keys(self, s: Session, text: str) -> bool:
        commands = text_commands(text)
        if not commands or any(ord(c) > 126 for c in text):
            raise GhostError("No text field has focus. Tap one, or pass its element_id.")
        for args in commands:
            self._input(s, " ".join(args))
        return True

    def _enter(self, s: Session, target: uitree.Node | None) -> None:
        """The keyboard's action key (Search, Go, Send) if the field has one, else Enter."""
        args: dict = {"display": s.display, "action": "imeEnter"}
        if target is not None:
            args.update(index=target.id, bounds=list(target.bounds))
        try:
            if self.helper.call("act", **args).get("done"):
                return
        except HelperError:
            pass
        self._input(s, f"keyevent {KEYS['enter']}")

    def press(self, sid: str, key: str) -> str:
        s = self.get(sid)
        if key not in KEYS:
            raise GhostError(f"press takes one of: {', '.join(KEYS)}.")
        self._refuse_if_secure(s)
        mark = self._mark(s)
        if key == "enter":
            self._enter(s, None)
        else:
            self._input(s, f"keyevent {KEYS[key]}")
        self._settle(s.display, mark)
        return f"Pressed {key}."

    def scroll(self, sid: str, direction: str, element: int | None = None) -> str:
        s = self.get(sid)
        if direction not in ("up", "down", "left", "right"):
            raise GhostError("scroll direction is one of: up, down, left, right.")
        self._refuse_if_secure(s)
        node = self._node(s, element) if element is not None else uitree.scroll_target(s.windows)
        bounds = (0, 0, *s.size)
        if node is not None:
            left, top, right, bottom = node.bounds
            bounds = (max(left, 0), max(top, 0), min(right, s.size[0]), min(bottom, s.size[1]))
        x1, y1, x2, y2 = swipe_for(direction, bounds)
        mark = self._mark(s)
        self._input(s, f"swipe {x1} {y1} {x2} {y2} 400")
        self._settle(s.display, mark)
        return f"Scrolled {direction}{' in ' + describe(node) if node else ''}."

    # ------------------------------------------------------------------ handing over
    def show(self, sid: str) -> str:
        s = self.get(sid)
        if not s.hidden:
            return f"{s.label} is already in a window on the user's desktop."
        ghost = s.ghost
        agent_log(f"{sid}: showing {s.package} to the user")
        ask_agent({"open": s.package, "adopt": ghost}, ["open", s.package, "--adopt", str(ghost)])
        task = self._wait_for_window(s.package, {ghost}, 60)
        if task is None:
            raise GhostError(f"The window didn't take {s.label} within a minute; it's still running hidden in "
                             f"session {sid}.")
        s.display, s.ghost, s.windows = task.display, None, []
        s.size = self._display_size(task.display)
        self._release(ghost)
        agent_log(f"{sid}: {s.package} now in a window (display {task.display}); display {ghost} released")
        return (f"{s.label} is now in a window on the user's desktop, just as you left it. The session goes on "
                f"there: read_screen and the other tools still work, on what the user sees.")

