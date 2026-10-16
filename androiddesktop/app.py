"""Android Desktop: Android apps as independent Windows windows, backed by a headless Android VM.

    pythonw -m androiddesktop                  # the launcher
    pythonw -m androiddesktop open <package>   # open one app in its own window (Start menu shortcuts)
    pythonw -m androiddesktop boot             # start Android, nothing shown (for an AI agent's apps)
    pythonw -m androiddesktop stop             # close every app and shut Android down
    pythonw -m androiddesktop quit             # ...and exit Android Desktop entirely
    python  -m androiddesktop setup            # download and install the Android runtime (console)
    python  -m androiddesktop doctor           # check whether this PC can run Android
    python  -m androiddesktop mcp              # the MCP server for AI agents (mcp.py)

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
from dataclasses import asdict
from pathlib import Path

from . import winapi

winapi.dpi_aware()  # before any window exists, pywebview's included

import webview  # noqa: E402
import win32api  # noqa: E402
import win32con  # noqa: E402
from PIL import Image  # noqa: E402

from . import adb, biometrics, checks, ghost, hello, runtime, shortcuts  # noqa: E402
from .catalog import Catalog  # noqa: E402
from .log import log, log_uncaught  # noqa: E402
from .manager import TRAY_OPEN, TRAY_QUIT, TRAY_STOP, WindowManager, send_to_agent  # noqa: E402
from .paths import DATA, INSTANCE  # noqa: E402
from .vm import LOCKED, OFF, RUNNING, VM  # noqa: E402

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

    def fix(self, check_id: str) -> None:
        self._agent.fix(check_id)

    def recheck(self) -> None:
        self._agent.recheck()

    def unlock(self, pin: str, remember: bool | None = None) -> str:
        """"" once Android is unlocked, otherwise what to show under the PIN field. remember: keep
        the PIN (DPAPI-encrypted) to unlock with Windows Hello from now on (True), forget it (False),
        or leave things as they are (None: the box wasn't shown)."""
        return self._agent.unlock(pin, remember)

    def unlock_hello(self) -> str:
        return self._agent.unlock_hello(self._agent._hwnd())

    def forget_pin(self) -> None:
        self._agent.forget_pin()

    def setup_fingerprint(self) -> None:
        self._agent.setup_fingerprint()

    def search_play(self, query: str) -> None:
        q = "".join(c for c in query if c.isalnum() or c in " -_.")
        self._agent.open("com.android.vending", ["-a", "android.intent.action.VIEW", "-d", f"market://search?q={q}"])

    # iPhone & iPad (remote/)
    def remote_enable(self, on: bool) -> None:
        self._agent.remote_enable(bool(on))

    def remote_firewall(self) -> bool:
        return self._agent.remote_firewall()

    def remote_pair(self) -> dict:
        return self._agent.remote_pair()

    def remote_pair_done(self) -> None:
        if self._agent.remote:
            self._agent.remote.end_pairing()

    def remote_approve(self, pid: str, allow: bool) -> None:
        if self._agent.remote:
            self._agent.remote.pairing.approve(pid, bool(allow))

    def remote_remove(self, group: str) -> None:
        if self._agent.remote and self._agent.remote.pairing.remove(group):
            self._agent.toast("Device removed. It needs to pair again to use your apps.")

    def remote_send(self, package: str, group: str, mode: str = "move") -> None:
        if self._agent.remote and mode in ("move", "share"):
            self._agent.remote.send(package, group, mode)

    def remote_back(self, package: str) -> None:
        if self._agent.remote:
            self._agent.remote.bring_back(package)


class Agent:
    def __init__(self, show_launcher: bool) -> None:
        winapi.set_process_app_id(shortcuts.LAUNCHER_ID)
        self.vm = VM()
        self.catalog = Catalog()
        self.manager = WindowManager(self.vm, self.catalog)
        self.fingerprint = biometrics.Bridge(self.manager, self._label, self._hello_anchor)
        self.remote = None  # apps on iPhones and iPads (remote/hub.py)
        self.remote_error = ""
        try:
            from .remote.hub import Hub
            self.remote = Hub(self.manager, self.catalog.sorted, self.notice)
            self.manager.remote = self.remote
        except ImportError as e:  # an install from before remote/ (pip install -e . brings what it needs)
            self.remote_error = f"Missing a package: {e.name}. Run: pip install -e ."
            log(f"remote: unavailable: {e!r}")
        self._hello_unlocking = threading.Lock()
        self.window: webview.Window | None = None
        self.loaded = False
        self.visible = show_launcher
        self.quitting = False
        self.setup_state = {"installed": runtime.installed(), "busy": False, "progress": 0, "message": "", "error": ""}
        self.checks: list[checks.Check] = []
        self._restart_pending = False  # Windows Hypervisor Platform turned on, waiting for a restart
        self._vm_state = OFF
        self._after_unlock: list[str] = []  # apps asked for while Android was locked
        self._icon_cache: dict[str, tuple[float, str]] = {}
        self._idle_since = time.monotonic()

    # ------------------------------------------------------------------ startup
    def run(self, first_request: dict) -> None:
        log_uncaught()
        log(f"agent started (pid {os.getpid()})")
        self.manager.on_request = self.handle
        self.manager.on_tray = self.tray
        self.manager.on_notice = self.notice
        self.manager.on_unlock = self.unlock_requested
        self.manager.start()
        self.fingerprint.listeners.append(lambda: self.push(apps=False))
        self.fingerprint.on_notice = self.notice
        self.fingerprint.start()
        self.vm.listeners.append(lambda s, d: self._on_vm(s))
        if self.remote:
            self.remote.listeners.append(lambda: self.push(apps=True))
            self.remote.pairing.listeners.append(self._remote_asks)
            if self.remote.enabled:
                self.remote.start()
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
        self.recheck()
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
            winapi.set_app_identity(hwnd, shortcuts.LAUNCHER_ID, "Android Desktop", "-m androiddesktop")
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
            self.open(request["open"], adopt=request.get("adopt"))
        if request.get("boot") and self.setup_state["installed"]:
            threading.Thread(target=self.vm.ensure_running, kwargs={"wait_for_unlock": False}, daemon=True).start()
        if request.get("show"):
            self.show_launcher()
            if request.get("remote") and self.window and self.loaded:
                self.window.evaluate_js("window.ui && ui.openRemote()")
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

    def open(self, package: str, intent: list[str] | None = None, adopt: int | None = None) -> None:
        """adopt: an AI agent's hidden display (ghost.py) the app is running on, to move into the window."""
        if not self.setup_state["installed"]:
            self.show_launcher()
            return
        if self.catalog.get(package) is None and self.vm.state == LOCKED:
            # Locked Android only lists a few system apps: open it once the PIN is in.
            self._after_unlock.append(package)
            self.show_launcher()
            return
        if self.catalog.get(package) is None:
            self._refresh_catalog()
        if self.catalog.get(package) is None:
            self.notice(f"Couldn't open {package}: it isn't installed in Android.")
            return
        self.manager.open(package, intent, adopt)

    def unlock(self, pin: str, remember: bool | None = None) -> str:
        was_locked = self.vm.state == LOCKED
        if was_locked:
            log(f"unlock: PIN entered in the launcher{' (remember: on)' if remember else ''}")
        message = self.vm.unlock(pin)
        if was_locked and not message and self.vm.state == RUNNING:  # the PIN is right
            if remember:
                hello.remember_pin(pin)
                log("unlock: PIN remembered for Windows Hello")
            elif remember is False and hello.has_pin():
                self.forget_pin()
        self.push(apps=False)
        return message

    # ------------------------------------------------------------------ Windows Hello
    def _label(self, package: str) -> str:
        app = self.catalog.get(package)
        return app.label if app else package

    def _hello_anchor(self) -> int:
        """A window of ours to show Windows Hello above when no app window is asking: the launcher
        if it's open, else the app window the user was last in. 0 if none is on screen."""
        if self.visible and self._hwnd():
            return self._hwnd()
        wins = [w for w in self.manager.windows.values() if not w.closing and w.hwnd]
        win = next((w for w in wins if w.active), None) or (wins[0] if wins else None)
        return win.hwnd if win else 0

    def unlock_requested(self, win=None) -> None:
        """An app window's Unlock button: Windows Hello above that window if the PIN is remembered,
        else the launcher asks for the PIN."""
        if hello.has_pin() and self.fingerprint.available and win is not None:
            threading.Thread(target=self.unlock_hello, args=(win.hwnd,), daemon=True).start()
        else:
            self.show_launcher()

    def unlock_hello(self, hwnd: int = 0) -> str:
        """Unlock Android with the remembered PIN once Windows Hello confirms it's the user. Returns
        what to show in the launcher ("" when unlocked, or nothing to say)."""
        if self.vm.state != LOCKED or not self._hello_unlocking.acquire(blocking=False):
            return ""
        try:
            pin = hello.remembered_pin()
            if pin is None:
                self.forget_pin()
                return "Enter your PIN: the remembered one can't be read any more."
            result = hello.verify(hwnd or self._hello_anchor(), "Unlock Android")
            if result != hello.VERIFIED:
                log(f"unlock: Windows Hello: {hello.RESULTS.get(result, result)}")
                return "" if result == hello.CANCELED else f"Windows Hello didn't work ({hello.RESULTS.get(result, result)})."
            message = self.vm.unlock(pin)
            if "not the right PIN" in message:  # it was changed in Android
                self.forget_pin()
                message = "Android's PIN has changed. Enter the new one (and tick the box to use Windows Hello again)."
            self.push(apps=False)
            if message:
                self.notice(message)
            return message
        finally:
            self._hello_unlocking.release()

    def forget_pin(self) -> None:
        hello.forget_pin()
        log("unlock: remembered PIN forgotten")
        self.push(apps=False)

    def setup_fingerprint(self) -> None:
        """"Set up fingerprint": Android's own enrollment in Settings; we put the finger down for it."""
        if self.vm.state != RUNNING:
            self.toast("Start Android first" if self.vm.state != LOCKED else "Unlock Android first")
            return
        self.fingerprint.enroll()
        self.open("com.android.settings", ["-a", "android.settings.FINGERPRINT_ENROLL"])

    # ------------------------------------------------------------------ iPhone & iPad
    def remote_enable(self, on: bool) -> None:
        if self.remote:
            self.remote.enable(on)
            log(f"remote: {'on' if on else 'off'}")

    def remote_firewall(self) -> bool:
        """The "Allow" button: Windows asks for administrator rights to add our one firewall rule."""
        return bool(self.remote and self.remote.allow_firewall())

    def remote_pair(self) -> dict:
        """A fresh QR code for the launcher: the server's address and a one-time token."""
        if not self.remote:
            return {"error": self.remote_error}
        url = self.remote.pair_url()
        if not url:
            return {"error": self.remote.problem or "Not ready yet: give it a moment."}
        import segno
        return {"url": url, "qr": segno.make(url, error="m").svg_data_uri(scale=5, border=2),
                "expires": 300}

    def _remote_asks(self) -> None:
        """A device asks to be allowed in (approval pairing): say so even if the launcher is closed."""
        asks = self.remote.pairing.approvals() if self.remote else []
        if asks and not (self.visible and self.loaded):
            self.manager.balloon(f"{asks[0]['name']} wants to use your Android apps. Open WinDroid to allow it.",
                                 "iPhone or iPad")

    def power(self) -> None:
        if self.vm.state in (RUNNING, LOCKED):
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
                     "theme": "light" if winapi.light_theme() else "dark",
                     "checks": [asdict(c) for c in self.checks if c.level != checks.OK],
                     "hello": {"available": self.fingerprint.available, "enrolled": self.fingerprint.enrolled,
                               "enrolling": self.fingerprint.enrolling, "remembered": hello.has_pin()},
               "remote": self.remote.snapshot() if self.remote else {"error": self.remote_error}}
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

    def notice(self, text: str) -> None:
        """Tell the user something went wrong: in the launcher if it's open, else as a Windows notification."""
        log(f"notice: {text}")
        if self.visible and self.loaded:
            self.toast(text)
        else:
            self.manager.balloon(text)

    def toast(self, text: str) -> None:
        if self.window and self.loaded:
            try:
                self.window.evaluate_js(f"window.ui && ui.toast({json.dumps(text)})")
            except Exception:
                pass

    def _on_vm(self, vm_state: str) -> None:
        changed, self._vm_state = vm_state != self._vm_state, vm_state
        self.push(apps=False)
        if changed and vm_state in (OFF, RUNNING):  # a failed boot may have a reason worth showing
            threading.Thread(target=self.recheck, daemon=True).start()
        if vm_state == RUNNING:
            threading.Thread(target=self._now_running, daemon=True).start()
        if changed and vm_state == LOCKED and hello.has_pin():
            threading.Thread(target=self._auto_unlock, daemon=True, name="hello-unlock").start()

    def _auto_unlock(self) -> None:
        """Android started locked and the user chose to unlock it with Windows Hello: ask right away,
        above the launcher or the app window waiting for Android. Not if nothing of ours is on
        screen: a Windows Hello prompt out of nowhere would only puzzle."""
        if not self._hello_anchor() or not hello.available():
            return
        self.unlock_hello()

    def _now_running(self) -> None:
        self._refresh_catalog()
        pending, self._after_unlock = self._after_unlock, []
        for package in dict.fromkeys(pending):
            self.open(package)

    def _refresh_catalog(self) -> None:
        if self.vm.state != RUNNING:
            return
        try:
            if not self.catalog.refresh():
                self.push(apps=True)
        except Exception as e:
            log(f"catalog refresh: {e!r}")

    # ------------------------------------------------------------------ PC checks
    def recheck(self) -> list[checks.Check]:
        """Re-run the PC checks (well under a second) and show any problems in the launcher."""
        try:
            found = checks.run(self.setup_state["installed"], self.vm.state == RUNNING)
        except Exception as e:
            log(f"checks: {e!r}")
            found = []
        if self._restart_pending:
            found = [checks.Check("hypervisor", checks.BLOCKING, "Restart your PC to finish",
                                  "Windows Hypervisor Platform is turned on. It starts working after a restart.")
                     if c.id == "hypervisor" and c.level == checks.BLOCKING else c for c in found]
        for c in found:
            if c.level != checks.OK and all(c.title != old.title for old in self.checks):
                log(f"check: {c.level}: {c.title}")
        self.checks = found
        self.push()
        return found

    def fix(self, check_id: str) -> None:
        check = next((c for c in self.checks if c.id == check_id and c.fix), None)
        if check is None:
            return
        if check.fix == "open_storage":
            checks.open_storage_settings()
        elif check.fix == "enable_hypervisor":
            try:
                ok = checks.enable_hypervisor()
            except Exception as e:  # most likely "no" to Windows' admin prompt
                log(f"enable hypervisor: {e!r}")
                self.toast("Windows Hypervisor Platform wasn't turned on")
                return
            log(f"enable hypervisor: {'done, restart pending' if ok else 'failed'}")
            if ok:
                self._restart_pending = True
            else:
                self.toast("Windows couldn't turn it on. Try \"Turn Windows features on or off\".")
        self.recheck()

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
            # Not enough disk stops setup; a missing Windows feature doesn't (it can be turned on meanwhile).
            if any(c.id == "disk" and c.level == checks.BLOCKING for c in self.recheck()):
                return
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
        if self.vm.state == LOCKED:
            self.toast("Unlock Android first")
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
            if self.vm.state in (RUNNING, LOCKED):
                self.vm.refresh()  # notices an emulator that died (or was unlocked in its own window)
            busy = bool(self.manager.windows) or self.visible or bool(self.remote and self.remote.busy())
            if busy:
                self._idle_since = time.monotonic()
            if self.vm.state == RUNNING and time.monotonic() - last_refresh > 45:
                last_refresh = time.monotonic()
                self._refresh_catalog()
            idle = time.monotonic() - self._idle_since
            if not busy and self.vm.state == OFF and not self.setup_state["busy"]:
                self.quit()  # nothing open and Android is off: nothing left for us to do
            if not busy and self.vm.state in (RUNNING, LOCKED) and idle > IDLE_SHUTDOWN_MINUTES * 60:
                if self._agents_busy():
                    self._idle_since = time.monotonic()
                    continue
                log("idle: shutting Android down")
                self.quit()

    @staticmethod
    def _agents_busy() -> bool:
        """An AI agent has apps running hidden (ghost.py): Android is in use, though no window shows it."""
        try:
            return bool(ghost.agent_displays())
        except Exception:
            return False


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else ""
    if command == "setup":
        runtime.install(lambda m, f: print(f"\r{m:<70} {f * 100:5.1f}%", end="", flush=True))
        print()
        return
    if command == "doctor":
        installed = runtime.installed()
        running = installed and adb.online() and adb.booted()
        locked = running and adb.user_locked()
        print(f"Data folder:  {DATA}")
        print(f"Runtime:      {'installed' if installed else 'not installed'}")
        print(f"Android:      {'not running' if not running else 'running'}"
              f"{', LOCKED: enter your PIN in the launcher to unlock it' if locked else ''}")
        print(checks.report(installed, running))
        return
    if command == "open" and len(args) > 1:
        request = {"open": args[1]}
        if len(args) > 3 and args[2] == "--adopt" and args[3].isdigit():
            request["adopt"] = int(args[3])
    elif command in ("boot", "stop", "quit"):
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
    mutex = kernel32.CreateMutexW(None, False, "Local\\AndroidDesktop.Agent" + INSTANCE)
    deadline = time.monotonic() + 120
    while kernel32.WaitForSingleObject(mutex, 250) not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
        if send_to_agent(request) or time.monotonic() > deadline:
            return
    globals()["_mutex"] = mutex  # owned for the life of the process
    Agent(show_launcher=bool(request.get("show"))).run(request)
