"""Everything WinDroid can do, and how each feature stands right now: the data behind the
launcher's own settings page (one card per feature in ui/), its command palette (Ctrl+K) and the
What's new card.

Every card reads live state from wherever its feature keeps it: the VM, the AVD's config.ini, the
remembered PIN (hello.py), the fingerprint bridge, Windows' registry for the virtual camera, Claude's
own config files for the MCP server, agent.log. What's slow to learn (the webcams the emulator sees,
Android's screen lock, an AI agent's hidden apps) is looked up in the background or kept for a few
seconds; nothing is made up.

Parts that aren't in every build show up only when they're there: the virtual camera (camera.py),
plugins (manager.plugins), iPhone and iPad (manager.remote) and updates (agent.updater).
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable

from . import __version__, adb, emuview, ghost, hello, runtime, state
from .adb import CREATE_NO_WINDOW
from .appwindow import SCRCPY_QUALITY
from .log import log
from .paths import DATA, LOGS
from .vm import LOCKED, RUNNING

if TYPE_CHECKING:
    from .app import Agent

MCP_NAME = "androiddesktop"  # the server's name in Claude's config
WHATS_NEW_SEEN = "whats_new_seen"  # state.json: the version whose What's new card was put away
APP_WINDOWS = ("auto", "scrcpy")  # state.json "app_windows"
ANDROID_VERSIONS = {36: "16", 35: "15", 34: "14"}
CAMERA_LABELS = {"none": "No camera", "emulated": "Android's test camera", "virtualscene": "Android's test scene"}
WEBCAMS_FOR = 120  # seconds a webcam list is good for
LOCK_FOR = 20  # ...Android's screen lock
GHOSTS_FOR = 5  # ...the count of an AI agent's hidden apps
ACTIVITY_BYTES = 32 * 1024  # how much of agent.log's end to read
ACTIVITY_ENTRY = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d) (.*)$")


def optional(name: str) -> ModuleType | None:
    """A module of ours that isn't in every build (camera.py), or None."""
    try:
        return importlib.import_module(f".{name}", __package__)
    except ImportError:
        return None


def run(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=CREATE_NO_WINDOW)


# ------------------------------------------------------------------ Android
def gigabytes(ram: str) -> int | None:
    """config.ini's hw.ramSize ("8192M", "8G" or plain megabytes) in GB."""
    m = re.fullmatch(r"\s*(\d+)\s*([MG]?)B?\s*", ram or "", re.I)
    if not m:
        return None
    n = int(m.group(1))
    return n if m.group(2).upper() == "G" else round(n / 1024)


def android_card(vm_state: str, detail: str, config: dict[str, str], windows: int) -> dict:
    cores = config.get("hw.cpu.ncore", "")
    return {"state": vm_state, "detail": detail,
            "version": f"Android {ANDROID_VERSIONS.get(runtime.API_LEVEL, runtime.API_LEVEL)}",
            "play": config.get("PlayStore.enabled", "true") == "true",
            "memory": gigabytes(config.get("hw.ramSize", "")), "cores": int(cores) if cores.isdigit() else None,
            "windows": windows}


# ------------------------------------------------------------------ security
def parse_credential_type(dumpsys: str) -> str | None:
    """`dumpsys lock_settings`: the first user's screen lock ("CredentialType: Pin") as none, pin,
    password or pattern. None if Android didn't say."""
    m = re.search(r"CredentialType:\s*([A-Za-z_]+)", dumpsys)
    if not m:
        return None
    kind = m.group(1).lower()
    return {"none": "none", "pin": "pin", "password": "password", "pattern": "pattern",
            "password_or_pin": "pin"}.get(kind)


def security_card(hello_ready: bool, remembered: bool, enrolled: int | None, enrolling: bool, vm_state: str,
                  lock: str | None) -> dict:
    """lock: Android's screen lock as Android said it (None: unknown, e.g. while it's off)."""
    if vm_state == LOCKED:
        lock = "locked"  # a PIN is set and hasn't been entered since Android started
    return {"hello": hello_ready, "unlock": remembered, "fingerprints": enrolled, "enrolling": enrolling,
            "lock": lock, "running": vm_state == RUNNING}


def valid_pin(pin: str) -> bool:
    return bool(pin) and len(pin) <= 64 and all(ord(c) >= 32 for c in pin)


# ------------------------------------------------------------------ camera
def camera_name(name: str) -> str:
    """A webcam's name as people know it: Windows adds " (Windows Virtual Camera)" to virtual ones."""
    return re.sub(r"\s*\(Windows Virtual Camera\)$", "", name)


def camera_card(webcams: list[tuple[str, str]] | None, config: dict[str, str], camera: ModuleType | None) -> dict:
    """webcams: (name, label) as the emulator lists them; None while still looking."""
    names = {label: camera_name(name) for name, label in webcams or []}
    front = config.get("hw.camera.front", "")
    android = names.get(front) or CAMERA_LABELS.get(front) or (front if front else None)
    virtual = None
    if camera is not None:
        try:
            installed = camera.installed_dll() is not None
            virtual = {"installed": installed, "current": installed and camera.up_to_date()}
        except Exception as e:
            log(f"features: virtual camera: {e!r}")
            virtual = {"installed": False, "current": False}
    return {"webcams": None if webcams is None else [camera_name(name) for name, _ in webcams], "android": android,
            "virtual": virtual}


# ------------------------------------------------------------------ AI agents (MCP)
def claude_code_config() -> Path:
    return Path.home() / ".claude.json"


def claude_desktop_config() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude" / "claude_desktop_config.json"


def mcp_command() -> list[str]:
    """How an MCP client starts our server: with a console Python (an MCP server talks over stdin and
    stdout), or the installed console program."""
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return [str(exe.with_name("windroid-cli.exe")), "mcp"]
    console = exe.with_name("python.exe")
    return [str(console if console.exists() else exe), "-m", "androiddesktop", "mcp"]


def server_entry(command: list[str]) -> dict:
    return {"command": command[0], "args": command[1:]}


def desktop_snippet(command: list[str]) -> str:
    """What goes in Claude Desktop's claude_desktop_config.json."""
    return json.dumps({"mcpServers": {MCP_NAME: server_entry(command)}}, indent=2)


def registered(config: dict) -> dict | None:
    """Our server in a Claude config (Claude Code's user scope, or Claude Desktop's), if it's there."""
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    entry = servers.get(MCP_NAME) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def same_command(entry: dict | None, command: list[str]) -> bool:
    """entry starts this copy of WinDroid (not, say, another checkout's Python)."""
    if not entry:
        return False
    try:
        same_exe = os.path.normcase(os.path.abspath(entry.get("command", ""))) == os.path.normcase(command[0])
    except (TypeError, ValueError):
        return False
    return same_exe and list(entry.get("args") or []) == command[1:]


def find_claude() -> str | None:
    """Claude Code's command line, where its installer puts it if it isn't on PATH."""
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude.exe"
    return str(local) if local.exists() else None


def parse_activity(text: str, limit: int = 6) -> list[dict]:
    """agent.log's last entries, newest first: {"date", "time", "text"} with each entry's first line
    (a crash's traceback follows its entry on lines of its own)."""
    entries = []
    for line in text.splitlines():
        m = ACTIVITY_ENTRY.match(line)
        if m:
            entries.append({"date": m.group(1), "time": m.group(2), "text": m.group(3)})
    return entries[::-1][:limit]


def tail(path: Path, size: int = ACTIVITY_BYTES) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - size))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


# ------------------------------------------------------------------ plugins (plugins/, when in this build)
def plugin_classes(host: Any) -> list:
    if host is None:
        return []
    load = getattr(host, "_load", None)
    return list(load() if load else getattr(host, "classes", None) or [])


def plugins_card(host: Any) -> list[dict] | None:
    """None when this build has no plugins at all."""
    if host is None:
        return None
    return [{"name": c.name, "title": c.title or c.name, "packages": sorted(c.packages), "on": bool(host.enabled(c))}
            for c in plugin_classes(host)]


def plugins_for(host: Any, package: str) -> list[dict]:
    """The plugins for one app, for its window's menu."""
    return [p for p in plugins_card(host) or [] if package in p["packages"]]


def set_plugin(host: Any, windows: list, name: str, on: bool, call: Callable[[Callable[[], None]], None]) -> bool:
    """Turn a plugin on or off (state.json "plugins"), in the windows open now too: their plugins are
    stopped and the ones that are on start again. False if there's no such plugin."""
    classes = [c for c in plugin_classes(host) if c.name == name]
    if not classes:
        return False
    wanted = dict(state.get("plugins") or {})
    wanted[name] = bool(on)
    state.put("plugins", wanted)
    log(f"plugin {name}: {'on' if on else 'off'}")
    packages = set().union(*(c.packages for c in classes))
    for win in windows:
        if win.app.package in packages and win.child and not win.closing:
            def restart(win=win) -> None:  # on the window thread, as the host's hooks are
                host.closed(win)
                host.shown(win)
            call(restart)
    return True


# ------------------------------------------------------------------ iPhone & iPad (remote/, when in this build)
def phones_card(hub: Any) -> dict | None:
    if hub is None:
        return None
    snap = hub.snapshot()
    return {"enabled": bool(snap.get("enabled")), "problem": snap.get("problem") or "",
            "devices": [{"name": d.get("name") or "iPhone or iPad", "online": bool(d.get("online")),
                         "apps": list(d.get("apps") or [])} for d in snap.get("devices") or []],
            "asking": len(snap.get("approvals") or [])}


# ------------------------------------------------------------------ performance
def performance_card(app_windows: str, quality: str, vm_state: str, endpoint: bool, disabled: str,
                     windows: list) -> dict:
    """How app windows are drawn now. endpoint: the emulator's gRPC is there (emuview.endpoint());
    disabled: why emulator displays were turned off for this Android session, if they were."""
    on_emulator = sum(1 for w in windows if getattr(w, "surface", None) is not None)
    on_scrcpy = sum(1 for w in windows if getattr(w, "surface", None) is None and getattr(w, "child", 0))
    if app_windows == "scrcpy":
        mode, why = "scrcpy", "You chose video for every window."
    elif vm_state != RUNNING:
        mode, why = "emulator", "Up to seven windows on the emulator's own displays; more through video."
    elif disabled:
        mode, why = "scrcpy", f"Video until Android restarts: {disabled}."
    elif not endpoint:
        mode, why = "scrcpy", "Video until Android restarts: an older version of WinDroid started it."
    else:
        mode, why = "emulator", "Up to seven windows on the emulator's own displays; more through video."
    return {"setting": app_windows if app_windows in APP_WINDOWS else "auto", "mode": mode, "why": why,
            "quality": quality if quality in SCRCPY_QUALITY else "high",
            "windows": {"emulator": on_emulator, "scrcpy": on_scrcpy}}


# ------------------------------------------------------------------ updates (update.py, when in this build)
def updates_card(updater: Any) -> dict:
    out = {"version": __version__, "available": updater is not None, "managed": False, "offer": None}
    if updater is not None:
        out["managed"] = bool(getattr(updater, "enabled", True))
        out["offer"] = updater.snapshot()
    return out


# ------------------------------------------------------------------ What's new
def whats_new(seen: str | None, version: str = __version__) -> dict | None:
    """The What's new card, after the first run and after each update, until it's put away."""
    if seen == version:
        return None
    return {"version": version, "first": not seen}


# ------------------------------------------------------------------ everything together
class Features:
    """The launcher's settings page, for one agent: live status (snapshot()) and the cards' actions."""

    def __init__(self, agent: Agent, run: Callable[..., subprocess.CompletedProcess] = run) -> None:
        self.agent = agent
        self._run = run
        self._lock = threading.Lock()
        self._webcams: list[tuple[str, str]] | None = None
        self._webcams_at = 0.0
        self._looking = False  # a webcam scan is under way
        self._screen_lock: tuple[float, str | None] = (0.0, None)
        self._ghosts: tuple[float, int] = (0.0, 0)
        self._failed: set[str] = set()

    # ------------------------------------------------------------------ status
    def snapshot(self) -> dict:
        a = self.agent
        try:
            config = runtime.vm_config()
        except OSError:
            config = {}
        windows = [w for w in a.manager.windows.values() if not w.closing]
        manager = a.manager
        parts: dict[str, Callable[[], Any]] = {
            "android": lambda: android_card(a.vm.state, a.vm.detail, config, len(windows)),
            "security": lambda: security_card(
                a.fingerprint.available, hello.has_pin(), a.fingerprint.enrolled, a.fingerprint.enrolling,
                a.vm.state, self.screen_lock()),
            "camera": lambda: camera_card(self.webcams(), config, optional("camera")),
            "agents": self.agents,
            "plugins": lambda: plugins_card(getattr(manager, "plugins", None)),
            "phones": lambda: phones_card(getattr(manager, "remote", None)),
            "performance": lambda: performance_card(
                state.get("app_windows") or "auto", state.get("scrcpy_quality") or "high", a.vm.state,
                emuview.endpoint() is not None, emuview.link.disabled, windows),
            "updates": lambda: updates_card(getattr(a, "updater", None)),
            "whatsNew": lambda: whats_new(state.get(WHATS_NEW_SEEN)),
        }
        out: dict[str, Any] = {"installed": a.setup_state["installed"]}
        for name, part in parts.items():
            try:
                out[name] = part()
            except Exception:
                out[name] = {"error": "Couldn't read this right now."}
                if name not in self._failed:  # once: the page asks every few seconds
                    self._failed.add(name)
                    log(f"features: {name}:\n{traceback.format_exc().rstrip()}")
        return out

    def webcams(self) -> list[tuple[str, str]] | None:
        """The webcams the emulator sees (it takes a second or two to ask: in the background)."""
        if not runtime.installed():
            return []
        with self._lock:
            stale = time.monotonic() - self._webcams_at > WEBCAMS_FOR
            if stale and not self._looking:
                self._looking = True
                threading.Thread(target=self._look_for_webcams, daemon=True, name="webcams").start()
            return self._webcams

    def _look_for_webcams(self) -> None:
        try:
            found = runtime.webcams()
        finally:
            with self._lock:
                self._looking = False
        with self._lock:
            self._webcams, self._webcams_at = found, time.monotonic()

    def screen_lock(self) -> str | None:
        if self.agent.vm.state != RUNNING:
            return None
        at, kind = self._screen_lock
        if time.monotonic() - at > LOCK_FOR:
            try:
                kind = parse_credential_type(adb.shell("dumpsys lock_settings", timeout=10))
            except Exception:
                kind = None
            self._screen_lock = (time.monotonic(), kind)
        return kind

    def agents(self) -> dict:
        command = mcp_command()
        code = registered(self._read_json(claude_code_config()))
        desktop_path = claude_desktop_config()
        desktop = registered(self._read_json(desktop_path))
        return {"command": subprocess.list2cmdline(command),
                "claudeCode": {"installed": find_claude() is not None, "connected": code is not None,
                               "current": same_command(code, command)},
                "claudeDesktop": {"installed": desktop_path.parent.exists(), "connected": desktop is not None,
                                  "current": same_command(desktop, command)},
                "hidden": self.hidden_apps(), "activity": parse_activity(tail(LOGS / "agent.log"))}

    def hidden_apps(self) -> int:
        """Apps an AI agent is running hidden right now (ghost.py)."""
        if self.agent.vm.state != RUNNING:
            return 0
        at, count = self._ghosts
        if time.monotonic() - at > GHOSTS_FOR:
            try:
                count = len(ghost.agent_displays())
            except Exception:
                count = 0
            self._ghosts = (time.monotonic(), count)
        return count

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ Android
    def run_checks(self) -> list[dict]:
        """Settings > Android > Run checks: what `doctor` prints, every check with its verdict."""
        return [asdict(c) for c in self.agent.recheck()]

    # ------------------------------------------------------------------ security
    def hello_unlock(self, on: bool, pin: str = "") -> str:
        """Unlock with Windows Hello on (it needs Android's PIN, checked by Android first) or off.
        "" when done, else what to tell the user. The PIN never goes into a log."""
        a = self.agent
        if not on:
            a.forget_pin()
            return ""
        if not a.fingerprint.available:
            return "Windows Hello isn't set up on this PC. Set it up in Windows Settings > Accounts > Sign-in options."
        if not valid_pin(pin):
            return "Enter the PIN or password you use to unlock Android."
        if a.vm.state == LOCKED:
            return a.unlock(pin, True)  # unlocks Android as well
        if a.vm.state != RUNNING:
            return "Start Android first: it checks the PIN."
        try:
            out = adb.shell("locksettings verify --old " + shlex.quote(pin), timeout=30)
        except subprocess.TimeoutExpired:
            return "Android didn't answer. Try again."
        # Android's answers repeat the PIN: never log them.
        if "has no password" in out:
            return "Android has no screen lock, so there's nothing to unlock."
        if "verified successfully" not in out:
            if "throttled" in out.lower():
                return "Too many wrong tries. Wait 30 seconds, then try again."
            log("unlock: wrong PIN entered in settings")
            return "That's not Android's PIN. Try again."
        hello.remember_pin(pin)
        log("unlock: PIN remembered for Windows Hello (from settings)")
        self._screen_lock = (0.0, None)
        a.push()
        return ""

    # ------------------------------------------------------------------ camera
    def camera(self, command: str) -> str:
        """Install or remove the virtual camera. Windows asks for administrator rights; this waits."""
        camera = optional("camera")
        if camera is None or command not in ("install", "uninstall"):
            return "The virtual camera isn't part of this version."
        log(f"features: camera {command}")
        code = camera.main([command])
        with self._lock:
            self._webcams_at = 0.0  # the webcams change with it
        if code == 0:
            return ("Installed. Android uses it from its next start." if command == "install" else
                    "Removed. Android uses the webcam itself from its next start.")
        if code == 2:
            return "Canceled: Windows didn't get administrator rights."
        return "That didn't work. See androiddesktop.log."

    # ------------------------------------------------------------------ AI agents
    def connect_claude(self) -> str:
        """Register our MCP server with Claude Code, for every project (claude mcp add --scope user)."""
        claude = find_claude()
        if claude is None:
            return "Claude Code isn't installed. Copy the Claude Desktop setup instead, or install Claude Code."
        command = mcp_command()
        try:
            if registered(self._read_json(claude_code_config())):  # e.g. another copy's: this one replaces it
                self._run([claude, "mcp", "remove", "--scope", "user", MCP_NAME])
            r = self._run([claude, "mcp", "add", "--scope", "user", MCP_NAME, "--", *command])
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"features: claude mcp add: {e!r}")
            return "Claude Code didn't answer. Try again."
        if r.returncode != 0:
            log(f"features: claude mcp add: {r.returncode} {(r.stderr or r.stdout).strip()[:300]}")
            return "Claude Code couldn't add it. See androiddesktop.log."
        log("features: connected to Claude Code")
        return ""

    def disconnect_claude(self) -> str:
        claude = find_claude()
        if claude is None:
            return "Claude Code isn't installed."
        try:
            r = self._run([claude, "mcp", "remove", "--scope", "user", MCP_NAME])
        except (OSError, subprocess.TimeoutExpired):
            return "Claude Code didn't answer. Try again."
        log(f"features: disconnected from Claude Code ({r.returncode})")
        return "" if r.returncode == 0 else "Claude Code couldn't remove it."

    # ------------------------------------------------------------------ settings in state.json
    def set_plugin(self, name: str, on: bool) -> bool:
        m = self.agent.manager
        return set_plugin(getattr(m, "plugins", None), list(m.windows.values()), name, on, m.call)

    @staticmethod
    def set_app_windows(mode: str) -> bool:
        """New windows from now on: the emulator's displays when it can ("auto"), or video ("scrcpy")."""
        if mode not in APP_WINDOWS:
            return False
        state.put("app_windows", mode)
        log(f"features: app windows: {mode}")
        return True

    @staticmethod
    def set_quality(quality: str) -> bool:
        """How windows shown through scrcpy encode their video, from their next start."""
        if quality not in SCRCPY_QUALITY:
            return False
        state.put("scrcpy_quality", quality)
        log(f"features: scrcpy quality: {quality}")
        return True

    def check_updates(self) -> str:
        updater = getattr(self.agent, "updater", None)
        if updater is None:
            return "Updates come with the installer."
        if not getattr(updater, "enabled", True):
            return "This copy runs from source: update it with git."
        updater.check(force=True)
        offer = updater.snapshot()
        return f"WinDroid {offer['version']} is available." if offer else "You have the latest version."

    @staticmethod
    def dismiss_whats_new() -> None:
        state.put(WHATS_NEW_SEEN, __version__)

    @staticmethod
    def open_folder(which: str) -> None:
        folder = {"logs": LOGS, "data": DATA}.get(which)
        if folder is not None:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)
