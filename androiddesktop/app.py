"""Android Desktop: Android apps as independent Windows windows, backed by a headless Android VM.

    pythonw -m androiddesktop                  # the launcher
    pythonw -m androiddesktop open <package>   # open one app in its own window (Start menu shortcuts)
    pythonw -m androiddesktop stop             # close every app and shut Android down
    pythonw -m androiddesktop quit             # ...and exit Android Desktop entirely
    python  -m androiddesktop setup            # download and install the Android runtime (console)

One agent process owns everything: the VM, the app windows, the tray icon and the launcher. Later
invocations hand their request to it and exit.
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

from . import winapi

winapi.dpi_aware()  # before any window exists, pywebview's included

import webview  # noqa: E402
import win32api  # noqa: E402
import win32con  # noqa: E402
from PIL import Image  # noqa: E402

from . import adb, runtime, shortcuts  # noqa: E402
from .catalog import Catalog  # noqa: E402
from .log import log  # noqa: E402
from .manager import TRAY_OPEN, TRAY_QUIT, TRAY_STOP, WindowManager, send_to_agent  # noqa: E402
from .paths import DATA  # noqa: E402
from .vm import OFF, RUNNING, VM  # noqa: E402

UI = Path(__file__).with_name("ui") / "index.html"
IDLE_SHUTDOWN_MINUTES = 10
LAUNCHER_SIZE = (460, 680)


class Api:
    """Called from the launcher's JavaScript (each call on its own thread)."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def get_state(self) -> dict:
        return self._agent.snapshot()

    def open_app(self, package: str) -> None:
        self._agent.open(package)

    def power(self) -> None:
        self._agent.power()

    def hide(self) -> None:
        self._agent.hide_launcher()

    def minimize(self) -> None:
        self._agent.window.minimize()

    def setup(self) -> None:
        self._agent.setup()

    def install_apk(self) -> None:
        self._agent.install_apk()

    def toggle_start(self, package: str) -> None:
        app = self._agent.catalog.get(package)
        if app:
            added = shortcuts.toggle_start(app)
            self._agent.toast(f"{app.label} {'pinned to' if added else 'removed from'} Start")
            self._agent.push(apps=True)

    def desktop_shortcut(self, package: str) -> None:
        app = self._agent.catalog.get(package)
        if app:
            shortcuts.desktop_shortcut(app)
            self._agent.toast(f"Added {app.label} to your desktop")

    def uninstall(self, package: str) -> None:
        self._agent.uninstall(package)

    def search_play(self, query: str) -> None:
        q = "".join(c for c in query if c.isalnum() or c in " -_.")
        self._agent.open("com.android.vending", ["-a", "android.intent.action.VIEW", "-d", f"market://search?q={q}"])


class Agent:
    def __init__(self, show_launcher: bool) -> None:
        winapi.set_process_app_id(shortcuts.LAUNCHER_ID)
        self.vm = VM()
        self.catalog = Catalog()
        self.manager = WindowManager(self.vm, self.catalog)
        self.window: webview.Window | None = None
        self.loaded = False
        self.visible = show_launcher
        self.quitting = False
        self.setup_state = {"installed": runtime.installed(), "busy": False, "progress": 0, "message": "", "error": ""}
        self._icon_cache: dict[str, tuple[float, str]] = {}
        self._idle_since = time.monotonic()

    # ------------------------------------------------------------------ startup
    def run(self, first_request: dict) -> None:
        log(f"agent started (pid {os.getpid()})")
        self.manager.on_request = self.handle
        self.manager.on_tray = self.tray
        self.manager.start()
        self.vm.listeners.append(lambda s, d: self._on_vm(s))
        self.catalog.listeners.append(lambda: self.push(apps=True))
        self.manager.listeners.append(lambda: self.push(apps=True))
        self.window = webview.create_window(
            "Android Desktop", url=str(UI), js_api=Api(self), width=LAUNCHER_SIZE[0], height=LAUNCHER_SIZE[1],
            frameless=True, easy_drag=False, resizable=False, hidden=not self.visible,
            background_color="#f3f5f9" if winapi.light_theme() else "#0b0d11")
        self.window.events.loaded += self._on_loaded
        self.window.events.shown += self._style_window
        self.window.events.closing += self._on_closing
        threading.Thread(target=self._background, args=(first_request,), daemon=True, name="startup").start()
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(DATA / "webview"))
        os._exit(0)

    def _background(self, first_request: dict) -> None:
        if self.setup_state["installed"]:
            try:
                shortcuts.ensure_launcher_shortcut()
            except Exception as e:
                log(f"launcher shortcut: {e!r}")
            self.vm.refresh()
            if self.vm.state == RUNNING:
                self._refresh_catalog()
            elif self.visible:
                threading.Thread(target=self.vm.ensure_running, daemon=True).start()  # boots in seconds
        self.handle(first_request)
        threading.Thread(target=self._housekeeping, daemon=True, name="housekeeping").start()

    def _style_window(self) -> None:
        hwnd = self._hwnd()
        if hwnd:
            # Frameless WinForms windows come out a little short of the requested size: set it exactly,
            # centred on the monitor's work area.
            scale = winapi.dpi_of(hwnd) / 96
            w, h = round(LAUNCHER_SIZE[0] * scale), round(LAUNCHER_SIZE[1] * scale)
            mon = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
            left, top, right, bottom = win32api.GetMonitorInfo(mon)["Work"]
            winapi.user32.SetWindowPos(hwnd, 0, left + (right - left - w) // 2, top + (bottom - top - h) // 2,
                                       w, h, 0x0014)  # SWP_NOZORDER | SWP_NOACTIVATE
            winapi.dwm_int(hwnd, winapi.DWMWA_WINDOW_CORNER_PREFERENCE, winapi.DWMWCP_ROUND)
            winapi.set_app_identity(hwnd, shortcuts.LAUNCHER_ID, "Android Desktop", shortcuts.MAIN_ICON, "-m androiddesktop")
            winapi.set_window_icon(hwnd, shortcuts.MAIN_ICON, winapi.dpi_of(hwnd))

    def _hwnd(self) -> int:
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:
            return 0

    def _on_loaded(self) -> None:
        self.loaded = True
        self.push(apps=True)

    def _on_closing(self) -> bool:
        if self.quitting:
            return True
        self.hide_launcher()  # Alt+F4 hides, like the close button; Quit lives in the tray
        return False

    # ------------------------------------------------------------------ requests
    def handle(self, request: dict) -> None:
        log(f"request: {request}")
        if request.get("open"):
            self.open(request["open"])
        if request.get("show"):
            self.show_launcher()
        if request.get("stop"):
            self.shutdown()
        if request.get("quit"):
            self.quit()

    def tray(self, command: int) -> None:
        if command == TRAY_OPEN:
            self.show_launcher()
        elif command == TRAY_STOP:
            threading.Thread(target=self.shutdown, daemon=True).start()
        elif command == TRAY_QUIT:
            threading.Thread(target=self.quit, daemon=True).start()

    def open(self, package: str, intent: list[str] | None = None) -> None:
        if not self.setup_state["installed"]:
            self.show_launcher()
            return
        if self.catalog.get(package) is None:
            self._refresh_catalog()
        if self.catalog.get(package) is None:
            self.toast("That app isn't installed in Android.")
            return
        self.manager.open(package, intent)

    def power(self) -> None:
        if self.vm.state == RUNNING:
            self.shutdown()
        elif self.vm.state == OFF:
            threading.Thread(target=self.vm.ensure_running, daemon=True).start()

    def shutdown(self) -> None:
        self.manager.close_all()
        self.vm.stop()

    def quit(self) -> None:
        log("quitting")
        self.quitting = True
        self.manager.accepting = False  # new launches wait for us to finish, then start fresh
        self.shutdown()
        self.manager.remove_tray()
        try:
            self.window.destroy()
        finally:
            os._exit(0)

    # ------------------------------------------------------------------ launcher window
    def show_launcher(self) -> None:
        self.visible = True
        if self.window:
            self.window.show()
            self.window.restore()
            hwnd = self._hwnd()
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        if self.setup_state["installed"] and self.vm.state == OFF:
            threading.Thread(target=self.vm.ensure_running, daemon=True).start()
        else:
            threading.Thread(target=self._refresh_catalog, daemon=True).start()

    def hide_launcher(self) -> None:
        self.visible = False
        self._idle_since = time.monotonic()
        if self.window:
            self.window.hide()

    # ------------------------------------------------------------------ state for the UI
    def _icon_uri(self, path: Path, px: int = 112) -> str:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        cached = self._icon_cache.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]
        buf = io.BytesIO()
        Image.open(path).convert("RGBA").resize((px, px), Image.LANCZOS).save(buf, "PNG", optimize=True)
        uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        self._icon_cache[str(path)] = (mtime, uri)
        return uri

    def snapshot(self, apps: bool = True) -> dict:
        out: dict = {"vm": {"state": self.vm.state, "detail": self.vm.detail}, "setup": dict(self.setup_state),
                     "theme": "light" if winapi.light_theme() else "dark"}
        if apps:
            running = set(self.manager.running())
            out["apps"] = [{"pkg": a.package, "label": a.label, "system": a.system, "icon": self._icon_uri(a.icon),
                            "running": a.package in running, "inStart": shortcuts.in_start(a), "v": a.updated}
                           for a in self.catalog.sorted()]
            play = self.catalog.get("com.android.vending")
            if play:
                out["play"] = self._icon_uri(play.icon, 48)
        return out

    def push(self, apps: bool = False) -> None:
        if not (self.window and self.loaded):
            return
        try:
            self.window.evaluate_js(f"window.ui && ui.update({json.dumps(self.snapshot(apps))})")
        except Exception as e:
            log(f"push: {e!r}")

    def toast(self, text: str) -> None:
        if self.window and self.loaded:
            try:
                self.window.evaluate_js(f"window.ui && ui.toast({json.dumps(text)})")
            except Exception:
                pass

    def _on_vm(self, vm_state: str) -> None:
        self.push(apps=False)
        if vm_state == RUNNING:
            threading.Thread(target=self._refresh_catalog, daemon=True).start()

    def _refresh_catalog(self) -> None:
        if self.vm.state != RUNNING:
            return
        try:
            if not self.catalog.refresh():
                self.push(apps=True)
        except Exception as e:
            log(f"catalog refresh: {e!r}")

    # ------------------------------------------------------------------ setup & app management
    def setup(self) -> None:
        if self.setup_state["busy"]:
            return

        def progress(message: str, fraction: float) -> None:
            self.setup_state.update(message=message, progress=max(0.0, fraction))
            now = time.monotonic()
            if now - getattr(progress, "last", 0) > 0.25 or fraction >= 1:
                progress.last = now
                self.push(apps=False)

        def work() -> None:
            self.setup_state.update(busy=True, error="", progress=0, message="Preparing...")
            self.push()
            try:
                runtime.install(progress)
                self.setup_state.update(installed=True, busy=False)
                shortcuts.ensure_launcher_shortcut()
                self.push(apps=True)
                self.vm.ensure_running()
            except Exception as e:
                log(f"setup failed: {e!r}")
                self.setup_state.update(busy=False, error=f"Setup didn't finish: {e}. Check your connection and try again.")
                self.push()

        threading.Thread(target=work, daemon=True, name="setup").start()

    def install_apk(self) -> None:
        paths = self.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True,
                                               file_types=("Android apps (*.apk)", "All files (*.*)"))
        if not paths:
            return
        if not self.vm.ensure_running():
            self.toast("Android isn't running")
            return
        for path in paths:
            name = Path(path).name
            self.toast(f"Installing {name}...")
            result = adb.run("install", "-r", "-g", path, timeout=600)
            ok = "Success" in result.stdout
            self.toast(f"Installed {name}" if ok else f"Couldn't install {name}")
            if not ok:
                log(f"install {name}: {result.stdout} {result.stderr}")
        self._refresh_catalog()

    def uninstall(self, package: str) -> None:
        app = self.catalog.get(package)
        if not app or app.system:
            return
        if not self.window.create_confirmation_dialog("Uninstall", f"Uninstall {app.label} from Android?"):
            return
        if package in self.manager.windows:
            self.manager.call(self.manager.windows[package].close)
        result = adb.run("uninstall", package, timeout=120)
        if "Success" in result.stdout:
            path = shortcuts.start_path(app)
            path.unlink(missing_ok=True)
            self.toast(f"Uninstalled {app.label}")
        else:
            self.toast(f"Couldn't uninstall {app.label}")
        self._refresh_catalog()

    # ------------------------------------------------------------------ housekeeping
    def _housekeeping(self) -> None:
        """Keep the app list fresh (Play Store installs) and power Android down when nobody needs it."""
        last_refresh = time.monotonic()
        while not self.quitting:
            time.sleep(5)
            busy = bool(self.manager.windows) or self.visible
            if busy:
                self._idle_since = time.monotonic()
            if self.vm.state == RUNNING and time.monotonic() - last_refresh > 45:
                last_refresh = time.monotonic()
                self._refresh_catalog()
            idle = time.monotonic() - self._idle_since
            if not busy and self.vm.state == OFF and not self.setup_state["busy"]:
                self.quit()  # nothing open and Android is off: nothing left for us to do
            if not busy and self.vm.state == RUNNING and idle > IDLE_SHUTDOWN_MINUTES * 60:
                log("idle: shutting Android down")
                self.quit()


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else ""
    if command == "setup":
        runtime.install(lambda m, f: print(f"\r{m:<70} {f * 100:5.1f}%", end="", flush=True))
        print()
        return
    if command == "open" and len(args) > 1:
        request = {"open": args[1]}
    elif command in ("stop", "quit"):
        request = {command: True}
    else:
        request = {"show": True}
    if send_to_agent(request):
        return
    if command in ("stop", "quit"):
        return
    # One agent at a time. The running agent owns this mutex; if one is still starting up (or shutting
    # Android down), hand it the request once it answers, or wait for it to exit and take over.
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    mutex = kernel32.CreateMutexW(None, False, "Local\\AndroidDesktop.Agent")
    deadline = time.monotonic() + 120
    while kernel32.WaitForSingleObject(mutex, 250) not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
        if send_to_agent(request) or time.monotonic() > deadline:
            return
    globals()["_mutex"] = mutex  # owned for the life of the process
    Agent(show_launcher=bool(request.get("show"))).run(request)
