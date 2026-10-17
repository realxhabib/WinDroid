"""Android apps on an iPhone or iPad: which app is on which device, and keeping everything in step.

A Link is one app on a phone or tablet, in one of two modes:

- "move": the app leaves its PC window for an Android display made for the device's screen (a Mirror
  with a new flex display at the device's size and pixel density). Its window, if it had one, shows
  "On iPhone" with Bring back, and its own scrcpy stops. Rotating the device resizes that display.
  Bring back starts the window's scrcpy again, which pulls the app home (or opens a window for it).
- "share": the device mirrors the app window's own display, letterboxed, and both can use it.

Phones talk to the hub through server.Client (on the web server's loop): open an app, report their
screen size, send touches and keys. Opening an app from the phone moves it there, the same as Send
on the PC.
"""
from __future__ import annotations

import queue
import socket
import threading
import time
from typing import TYPE_CHECKING, Callable

from .. import adb, state
from ..catalog import App
from ..ghost import parse_tasks
from ..log import log
from ..paths import DATA
from ..protected import SECURE_QUERY, parse_secure_displays
from ..vm import LOCKED, RUNNING
from . import protocol
from .certs import Certificates
from .mirror import Mirror, MirrorError
from .pairing import Pairing, b64e, mac

if TYPE_CHECKING:
    from ..appwindow import AppWindow
    from ..manager import WindowManager
    from .server import Client, Lan, WebServer

QUALITY = {"smooth": 1.0, "balanced": 1.5, "sharp": 2.0}  # display pixels per CSS pixel (at most the dpr)
DEFAULT_VIEW = {"w": 430, "h": 860, "dpr": 3.0}  # until the device tells us its own
MAX_SIDE = 2048
RESTART_DELAYS = (1, 2, 4)
WATCH_SECS = 2.0
NETWORK_SECS = 10.0


def later(secs: float, fn: Callable[[], None]) -> threading.Timer:
    t = threading.Timer(secs, fn)
    t.daemon = True
    t.start()
    return t


def _firewall_allows(port: int) -> bool:
    from . import firewall
    return firewall.allowed(port)


def display_size(view: dict, scale: float) -> tuple[int, int, int]:
    """(width, height, dpi) of an Android display that fills a device's view, in which an app sees as
    many dp as the device has CSS pixels."""
    s = min(scale, max(1.0, float(view.get("dpr", 1))))
    longest = max(view["w"], view["h"])
    if longest * s > MAX_SIDE:
        s = MAX_SIDE / longest
    w = max(160, round(view["w"] * s / 2) * 2)
    h = max(160, round(view["h"] * s / 2) * 2)
    return w, h, max(80, round(160 * s))


def scale_point(x: int, y: int, frm: tuple[int, int], to: tuple[int, int]) -> tuple[int, int]:
    """A touch on a frame of one size, on the stream's current size (they differ for a moment while
    the display resizes)."""
    if frm == to or not all(frm) or not all(to):
        return x, y
    return (min(to[0] - 1, round(x * to[0] / frm[0])), min(to[1] - 1, round(y * to[1] / frm[1])))


def text_chunks(text: str) -> list[tuple[str, str]]:
    """How to type text: ("text", s) for what Android can type key by key (ASCII), ("enter", "") for
    new lines, ("paste", s) for everything else (emoji, accents), which goes in by the clipboard."""
    out: list[tuple[str, str]] = []
    for i, line in enumerate(text.split("\n")):
        if i:
            out.append(("enter", ""))
        run, kind = "", ""
        for c in line:
            k = "text" if " " <= c <= "~" else "paste"
            if k != kind and run:
                out.append((kind, run))
                run = ""
            kind = k
            run += c
            if kind == "text" and len(run) >= protocol.INJECT_TEXT_MAX:
                out.append((kind, run))
                run = ""
        if run:
            out.append((kind, run))
    return out


class JpegFeed:
    """Decodes a link's stream on the PC and hands JPEG frames to devices that don't decode video
    themselves (no trusted certificate, so no WebCodecs). A device gets the next frame once it says
    it drew the last one, so a slow one skips frames instead of falling behind."""

    QUALITY = 72

    def __init__(self, link: Link) -> None:
        from .h264 import Decoder
        self.link = link
        self.decoder = Decoder()
        self.q: queue.Queue = queue.Queue()
        self.frame = None
        self.sent_ms = 0.0
        self.seq = 0
        self._jpeg: tuple[int, bytes] = (-1, b"")
        threading.Thread(target=self._run, daemon=True, name=f"jpeg-{link.package}").start()

    def feed(self, data: bytes, sent_ms: float) -> None:
        self.q.put((data, sent_ms))

    def kick(self) -> None:
        self.q.put(())

    def stop(self) -> None:
        self.q.put(None)

    def _run(self) -> None:
        try:
            while (item := self.q.get()) is not None:
                if item:
                    frames = self.decoder.decode(item[0])
                    if frames:
                        self.frame, self.sent_ms, self.seq = frames[-1], item[1], self.seq + 1
                    if not self.q.empty():
                        continue  # catch up before spending time on a JPEG nobody will see
                self._send()
        except Exception as e:
            log(f"remote: {self.link.package}: JPEG feed: {e!r}")
        finally:
            self.decoder.close()

    def _send(self) -> None:
        if self.frame is None:
            return
        now = time.monotonic()
        for c in self.link.watchers("jpeg"):
            if c.jpeg_seq >= self.seq or (c.jpeg_busy and now - c.jpeg_busy < 1.0):
                continue
            if self._jpeg[0] != self.seq:
                self._jpeg = (self.seq, self.frame.jpeg(self.QUALITY))
            c.jpeg_busy, c.jpeg_seq = now, self.seq
            c.send_video(protocol.VIDEO_JPEG, self.sent_ms, self._jpeg[1])


class Link:
    def __init__(self, hub: Hub, app: App, mode: str, group: str, win: AppWindow | None) -> None:
        self.hub, self.app, self.mode, self.group, self.win = hub, app, mode, group, win
        self.package, self.label, self.cap = app.package, app.label, hub.cap(app.package)
        self.mirror: Mirror | None = None
        self.display: int | None = None
        self.size = (0, 0)
        self.config = b""
        self.codec: str | None = None
        self.clients: list[Client] = []
        self.state = "starting"  # starting, live, returning, ended
        self.secure = False
        self.jpeg: JpegFeed | None = None
        self.view = dict(hub.view_for(group))
        self.quality = hub.quality(group)
        self.requested: tuple[int, int] | None = None
        self.started = time.monotonic()
        self.misses = 0
        self._key_at = 0.0
        self._key_timer: threading.Timer | None = None
        self._resize_timer: threading.Timer | None = None
        self._restarts = 0
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return f"<Link {self.package} {self.mode} display {self.display} {self.state}>"

    @property
    def live(self) -> bool:
        return self.state == "live" and self.mirror is not None and self.mirror.alive

    def watchers(self, decoder: str | None = None) -> list[Client]:
        with self._lock:
            return [c for c in self.clients if decoder is None or c.decoder == decoder]

    def info(self) -> dict:
        return {"t": "app", "app": self.cap, "label": self.label, "state": self.state, "mode": self.mode,
                "device": self.hub.group_name(self.group), "secure": self.secure}

    def broadcast(self, obj: dict) -> None:
        for c in self.watchers():
            c.send_json(obj)

    # ------------------------------------------------------------------ the stream
    def start(self) -> None:
        """Worker thread: start streaming (and, to move, bring the app over to its new display)."""
        if self.mode == "move":
            w, h, dpi = display_size(self.view, QUALITY[self.quality])
            mirror = Mirror(self.package, new_display=(w, h, dpi), on_session=self._session,
                            on_packet=self._packet, on_exit=self._exited)
            source = self.win.display_id if self.win is not None else None
            mirror.start()
            self.mirror, self.display, self.requested = mirror, mirror.display, (w, h)
            mirror.start_app(self.package)
            self._wait_for_app(source)
        else:
            if self.win is None or self.win.display_id is None:
                raise MirrorError("the app window isn't showing the app")
            mirror = Mirror(self.package, display=self.win.display_id, on_session=self._session,
                            on_packet=self._packet, on_exit=self._exited)
            mirror.start()
            self.mirror, self.display = mirror, mirror.display
        if self.win is not None and self.win.remote is self:
            self.win.display_id = self.display  # its Back button goes where the app is
        self.state = "live"
        self.started = time.monotonic()
        self.broadcast(self.info())

    def _wait_for_app(self, source: int | None) -> None:
        """Until the app's task is on our display; tasks it left on its window's display follow."""
        end = time.monotonic() + 10
        while time.monotonic() < end:
            tasks = parse_tasks(adb.shell("cmd activity stack list", timeout=10))
            here = [t for t in tasks if t.display == self.display]
            if any(self.package in t.packages for t in here):
                left = [t.root for t in tasks if source is not None and t.display == source]
                for root in reversed(left):  # bottom first, so they keep their order
                    adb.shell(f"am display move-stack {root} {self.display}", timeout=10)
                if left:
                    from ..protected import reset_secure_state_soon
                    reset_secure_state_soon()
                return
            time.sleep(0.2)
        log(f"remote: {self.package}: the app didn't arrive on display {self.display}")

    def _session(self, s: protocol.Session) -> None:
        self.size = (s.width, s.height)

    def _packet(self, header: protocol.PacketHeader, data: bytes) -> None:
        if header.config:
            self.config = data
            codec = protocol.avc_codec(data)
            if codec:
                self.codec = codec
                self.broadcast({"t": "video", "codec": codec, "w": self.size[0], "h": self.size[1]})
            return
        sent = time.time() * 1000
        kind = protocol.VIDEO_KEY if header.key else protocol.VIDEO_DELTA
        payload = self.config + data if header.key else data
        for c in self.watchers("webcodecs"):
            c.send_video(kind, sent, payload)
        if self.jpeg is not None:
            self.jpeg.feed(payload, sent)

    def keyframe(self) -> None:
        """A new key frame (with its config), for a device that just joined or fell behind."""
        with self._lock:
            now = time.monotonic()
            wait = self._key_at + 0.4 - now
            if wait > 0:
                if self._key_timer is None:
                    self._key_timer = later(wait, self._timed_keyframe)
                return
            self._key_at = now
        if self.mirror:
            self.mirror.reset_video()

    def _timed_keyframe(self) -> None:
        with self._lock:
            self._key_timer = None
        self.keyframe()

    def _exited(self, why: str) -> None:
        """The stream ended without us asking (Android hiccup or restart): start another."""
        if self.state != "live":
            return
        self.state = "starting"
        self.broadcast(self.info())
        threading.Thread(target=self._restart, daemon=True, name=f"remote-restart-{self.package}").start()

    def _restart(self) -> None:
        for delay in RESTART_DELAYS:
            time.sleep(delay)
            if self.state != "starting":
                return
            if self.hub.manager.vm.state != RUNNING:
                continue
            try:
                self.start()
                self.keyframe()
                return
            except Exception as e:
                log(f"remote: {self.package}: restart failed: {e!r}")
        self.hub.end(self, "error", bring_back=True)  # the window's scrcpy finds the app wherever it is

    def stop(self) -> None:
        for t in (self._key_timer, self._resize_timer):
            if t:
                t.cancel()
        if self.mirror:
            self.mirror.stop()
        if self.jpeg:
            self.jpeg.stop()
            self.jpeg = None

    # ------------------------------------------------------------------ devices
    def attach(self, client: Client) -> None:
        with self._lock:
            if client not in self.clients:
                self.clients.append(client)
        client.link, client.need_key, client.jpeg_seq, client.jpeg_busy = self, True, 0, 0
        client.send_json(self.info())
        if self.codec:
            client.send_json({"t": "video", "codec": self.codec, "w": self.size[0], "h": self.size[1]})
        if client.decoder == "jpeg" and self.jpeg is None:
            try:
                self.jpeg = JpegFeed(self)
            except Exception as e:
                log(f"remote: {self.package}: no JPEG decoder: {e!r}")
                client.send_json({"t": "error", "message": "This PC can't make JPEG frames: " + str(e)})
        if self.mode == "move" and client.group != self.group:
            self.group = client.group  # the newest device to open it is the one it fits
            self.hub.changed()
        if client.view and self.mode == "move" and client.group == self.group:
            self.set_view(client.view)
        self.keyframe()
        if self.jpeg:
            self.jpeg.kick()

    def detach(self, client: Client) -> None:
        with self._lock:
            if client in self.clients:
                self.clients.remove(client)
            jpeg_left = any(c.decoder == "jpeg" for c in self.clients)
        if client.link is self:
            client.link = None
        if not jpeg_left and self.jpeg:
            self.jpeg.stop()
            self.jpeg = None

    def set_view(self, view: dict) -> None:
        """The owner's screen changed (rotation, Safari's toolbars): resize the display, once it settles."""
        self.view = dict(view)
        if self.mode != "move":
            return
        with self._lock:
            if self._resize_timer:
                self._resize_timer.cancel()
            self._resize_timer = later(0.25, self._resize)

    def _resize(self) -> None:
        w, h, dpi = display_size(self.view, QUALITY[self.quality])
        if self.mirror and self.live and (w, h) != self.requested and self.mirror.dpi_matches(dpi):
            self.requested = (w, h)
            log(f"remote: {self.package}: display {self.display} -> {w}x{h}")
            self.mirror.resize(w, h)
        elif self.live and self.mirror and not self.mirror.dpi_matches(dpi):
            self.requality(self.quality)  # a different pixel density: that takes a new display

    def requality(self, quality: str) -> None:
        """New pixel density for the display: a new display, and the app moves over to it."""
        self.quality = quality
        if self.mode != "move" or not self.live:
            return
        old = self.mirror
        self.state = "starting"
        self.broadcast(self.info())

        def swap() -> None:
            try:
                self.start()  # START_APP pulls the app over from the old display
                if old:
                    old.stop()
                self.keyframe()
            except Exception as e:
                log(f"remote: {self.package}: new quality: {e!r}")
                self.mirror = old
                self.state = "live" if old and old.alive else "ended"
                self.broadcast(self.info())
        threading.Thread(target=swap, daemon=True).start()

    # ------------------------------------------------------------------ input
    def touch(self, client: Client, m: dict) -> None:
        if not self.live or self.secure:
            return
        frm = (m["w"], m["h"])
        w, h = self.size if all(self.size) else frm
        for action, pid, x, y, pressure in m["points"]:
            x, y = scale_point(x, y, frm, (w, h))
            self.mirror.send(protocol.touch_message(action, pid, x, y, w, h, pressure))

    def scroll(self, m: dict) -> None:
        if self.live and not self.secure:
            w, h = self.size if all(self.size) else (m["w"], m["h"])
            x, y = scale_point(m["x"], m["y"], (m["w"], m["h"]), (w, h))
            self.mirror.send(protocol.scroll_message(x, y, w, h, m["dx"], m["dy"]))

    def key(self, keycode: int, meta: int) -> None:
        if self.live and not self.secure:
            self.mirror.tap_keys(keycode, meta)

    def text(self, text: str) -> None:
        if not self.live or self.secure:
            return
        for kind, chunk in text_chunks(text):
            if kind == "enter":
                self.mirror.tap_keys(protocol.KEYCODE_ENTER)
            elif kind == "text":
                self.mirror.send(protocol.text_message(chunk))
            else:
                self.hub.paste_seq += 1
                self.mirror.send(protocol.clipboard_message(self.hub.paste_seq, chunk, paste=True))


class Hub:
    def __init__(self, manager: WindowManager, apps: Callable[[], list[App]], notice: Callable[[str], None]) -> None:
        self.manager = manager
        self._apps = apps
        self.notice = notice
        self.pc_name = socket.gethostname()
        self.pairing = Pairing(DATA / "remote" / "devices.json")
        self.certs = Certificates(DATA / "remote", self.pc_name)
        self.server: WebServer | None = None
        self.lan: Lan | None = None
        self.problem = ""  # why phones can't reach us, for the launcher
        self.links: dict[str, Link] = {}
        self.clients: list[Client] = []
        self.listeners: list[Callable[[], None]] = []
        self.paste_seq = 0
        self.qr: str | None = None  # the token in the QR code on screen, if any
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._running = False
        self.firewall: bool | None = None  # our inbound rule is in place (firewall.py); None: not checked
        self.check_firewall: Callable[[int], bool] = _firewall_allows
        self.pairing.listeners.append(self.changed)
        self.pairing.listeners.append(self._drop_removed)

    # ------------------------------------------------------------------ settings and state
    @property
    def enabled(self) -> bool:
        return bool((state.get("remote") or {}).get("enabled"))

    def _settings(self) -> dict:
        return dict(state.get("remote") or {})

    def quality(self, group: str) -> str:
        q = (self._settings().get("quality") or {}).get(group, "balanced")
        return q if q in QUALITY else "balanced"

    def view_for(self, group: str) -> dict:
        views = [d for d in self.pairing.devices() if d.group == group and d.view]
        return max(views, key=lambda d: d.last_seen).view if views else DEFAULT_VIEW

    def group_name(self, group: str) -> str:
        return next((g["name"] for g in self.pairing.groups() if g["group"] == group), "your device")

    def changed(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    def cap(self, package: str) -> str:
        """An app's address on the phone: unguessable, so the network can't learn which apps are installed."""
        return b64e(mac(self.pairing.key, "windroid-app-v1", package)[:12])

    def app_for_cap(self, cap: str) -> App | None:
        return next((a for a in self._apps() if self.cap(a.package) == cap), None)

    def busy(self) -> bool:
        """An app is on a device: Android is in use though no window shows it."""
        return any(link.state != "ended" for link in self.links.values())

    def link_for(self, package: str) -> Link | None:
        link = self.links.get(package)
        return link if link and link.state != "ended" else None

    # ------------------------------------------------------------------ the server's life
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._network_loop, daemon=True, name="remote-network").start()
        threading.Thread(target=self._watch_loop, daemon=True, name="remote-watch").start()

    def enable(self, on: bool) -> None:
        settings = self._settings()
        settings["enabled"] = bool(on)
        state.put("remote", settings)
        if on:
            self.start()
        else:
            self.end_pairing()
            for link in list(self.links.values()):
                self.end(link, "pc", bring_back=True)
        self._wake.set()
        self.changed()

    def _network_loop(self) -> None:
        from .server import WebServer, choose_lan, lan_candidates, outbound_ip
        while True:
            want = None
            if self.enabled:
                try:
                    candidates = lan_candidates()
                    want = choose_lan(candidates, outbound_ip())
                    from .server import PORT
                    if not self.firewall:  # once it's there, it stays (until the user removes it)
                        self.firewall = self.check_firewall(PORT)
                    if want is None:
                        self.problem = ("Your Wi-Fi is set to Public in Windows, so phones can't reach this PC. "
                                        "Set it to Private in Settings > Network & internet."
                                        if candidates else "This PC isn't on a home network (Wi-Fi or Ethernet).")
                    elif not self.firewall:
                        # Listening without our rule would make Windows offer to open the program to
                        # every port; the launcher asks the user to add the narrow rule instead.
                        self.problem = "Windows Firewall needs to let your iPhone or iPad in."
                        want = None
                    else:
                        self.problem = ""
                except Exception as e:
                    log(f"remote: network check: {e!r}")
                    want = self.lan
            if want != self.lan or (want and self.server is None):
                if self.server:
                    self.server.stop()
                    self.server = None
                self.lan = want
                if want:
                    try:
                        self.server = WebServer(self, want)
                        self.server.start()
                    except Exception as e:
                        log(f"remote: can't listen on {want.ip}: {e!r}")
                        self.problem = f"Couldn't listen on {want.ip}: {e}"
                        self.server, self.lan = None, None
                self.changed()
            self._wake.wait(NETWORK_SECS)
            self._wake.clear()

    def allow_firewall(self) -> bool:
        """The launcher's "Allow" button: Windows asks for administrator rights to add our rule."""
        from . import firewall
        from .server import PORT
        ok = firewall.allow(PORT)
        log(f"remote: firewall rule {'added' if ok else 'not added'}")
        self._wake.set()
        return ok

    def url(self, secure: bool = False) -> str | None:
        return self.server.url(secure) if self.server else None

    def pair_url(self) -> str | None:
        """For the QR code: a fresh one-time token, in the fragment (browsers never send that part)."""
        if not self.server:
            return None
        if self.qr:
            self.pairing.withdraw(self.qr)
        self.qr = self.pairing.offer()
        return f"{self.server.url()}/#pair={self.qr}"

    def end_pairing(self) -> None:
        if self.qr:
            self.pairing.withdraw(self.qr)
            self.qr = None

    # ------------------------------------------------------------------ from the PC
    def choices(self, package: str) -> list[tuple[str, str]]:
        """What the window's phone button offers: (action, label)."""
        link = self.link_for(package)
        if link:
            where = self.group_name(link.group)
            return [("back", f"Stop showing on {where}" if link.mode == "share" else f"Bring back from {where}")]
        groups = self.pairing.groups()
        out = [(f"move:{g['group']}", f"Move to {g['name']}") for g in groups]
        out += [(f"share:{g['group']}", f"Show on {g['name']} too") for g in groups]
        return out + [("pair", "Pair an iPhone or iPad...")]

    def send(self, package: str, group: str, mode: str = "move", client: Client | None = None) -> None:
        """Put an app on a device. Runs on a worker thread; the device hears about it when it's there."""
        threading.Thread(target=self._send, args=(package, group, mode, client), daemon=True,
                         name=f"remote-send-{package}").start()

    def _send(self, package: str, group: str, mode: str, client: Client | None) -> None:
        def tell(message: str) -> None:
            if client:
                client.send_json({"t": "error", "message": message})
            else:
                self.notice(message)
        with self._lock:
            existing = self.link_for(package)
            if existing and existing.mode == mode and (mode == "share" or existing.group == group):
                if client:
                    existing.attach(client)
                return
        if existing:
            self.end(existing, "pc", bring_back=existing.mode == "move" and mode == "share")
        app = next((a for a in self._apps() if a.package == package), None)
        if app is None:
            tell("That app isn't installed in Android any more.")
            return
        vm = self.manager.vm
        if vm.state == LOCKED:
            tell("Android is locked. Unlock it on your PC first.")
            return
        if vm.state != RUNNING:
            if client:
                client.send_json({"t": "status", "message": "Starting Android on your PC..."})
            if not vm.ensure_running(wait_for_unlock=False) or vm.state != RUNNING:
                tell("Android didn't start on your PC." if vm.state != LOCKED else "Android is locked. Unlock it on your PC first.")
                return
        win = self.manager.windows.get(package)
        if win is not None and win.closing:
            win = None
        if mode == "share" and (win is None or win.remote is not None or win.display_id is None):
            tell(f"Open {app.label} on your PC first, then share it.")
            return
        link = Link(self, app, mode, group, win)
        with self._lock:
            self.links[package] = link
        self.changed()
        if client:
            link.attach(client)
        try:
            link.start()
        except Exception as e:
            log(f"remote: {package}: couldn't start: {e!r}")
            link.stop()
            with self._lock:
                if self.links.get(package) is link:
                    del self.links[package]
            link.state = "ended"
            link.broadcast({"t": "error", "message": f"{app.label} couldn't start on this device: {e}"})
            if not client:
                self.notice(f"Couldn't send {app.label}: {e}")
            self.changed()
            return
        if mode == "move" and win is not None:
            self.manager.call(lambda: win.go_remote(link))
        # Every page this device has open switches to the app.
        for c in self.clients_of(group):
            if c is not client and c.link is not link:
                c.send_json({"t": "go", "app": link.cap, "label": link.label})
        log(f"remote: {package} {'moved to' if mode == 'move' else 'shared with'} {self.group_name(group)} "
            f"(display {link.display})")
        self.changed()

    def bring_back(self, package: str) -> None:
        link = self.link_for(package)
        if link:
            self.end(link, "pc", bring_back=True)

    def end(self, link: Link, reason: str, bring_back: bool = False) -> None:
        """reason: "pc" (it's back on the PC), "closed" (the app finished), "error"."""
        with self._lock:
            if link.state in ("ended", "returning"):
                return
            if bring_back and link.mode == "move" and link.display is not None:
                link.state = "returning"
            else:
                link.state = "ended"
                if self.links.get(link.package) is link:
                    del self.links[link.package]
        link.broadcast({"t": "state", "app": link.cap, "state": reason, "label": link.label})
        if link.state == "returning":
            win, display = link.win, link.display
            if win is not None and win.remote is link:
                self.manager.call(lambda: win.come_back(display))
            else:
                self.manager.open(link.package, adopt=display)
            # The window's scrcpy pulls the app over; window_embedded ends the link then. If it never
            # does, end it anyway after a while.
            later(45, lambda: self._finish(link))
        else:
            link.stop()
            win = link.win
            if reason == "closed" and win is not None and win.remote is link:
                self.manager.call(win.close)
        self.changed()

    def _finish(self, link: Link) -> None:
        with self._lock:
            if link.state == "ended":
                return
            link.state = "ended"
            if self.links.get(link.package) is link:
                del self.links[link.package]
        for c in link.watchers():
            link.detach(c)
        link.stop()
        self.changed()

    # ------------------------------------------------------------------ from the app windows
    def window_embedded(self, win: AppWindow, adopted: int | None) -> None:
        """A window is showing its app again (first time, after a reconnect, or after Bring back)."""
        link = self.links.get(win.app.package)
        if link is None:
            return
        if link.state == "returning" and adopted == link.display:
            later(2.0, lambda: self._finish(link))  # adopt() moves stragglers meanwhile
        elif link.mode == "share" and link.win is win and win.display_id != link.display:
            log(f"remote: {link.package}: window display changed to {win.display_id}; following")
            if link.mirror:
                link.mirror.stop()
            link.state = "starting"
            threading.Thread(target=link._restart, daemon=True).start()

    def window_closed(self, win: AppWindow) -> None:
        link = self.links.get(win.app.package)
        if link is None or link.win is not win:
            return
        if link.mode == "share":
            self.end(link, "closed")
        else:
            link.win = None  # the placeholder went; the app stays on the device

    def window_choice(self, win: AppWindow, action: str) -> None:
        if action == "back":
            self.bring_back(win.app.package)
        elif action == "pair":  # off the window thread, as requests from other processes are
            threading.Thread(target=self.manager.on_request, args=({"show": True, "remote": "pair"},),
                             daemon=True).start()
        elif ":" in action:
            mode, group = action.split(":", 1)
            self.send(win.app.package, group, mode)

    # ------------------------------------------------------------------ watching links
    def _watch_loop(self) -> None:
        """Every couple of seconds: is each moved app still on its display (or did it finish, or did the
        PC take it back), and is a secure screen (FLAG_SECURE) up on it?"""
        while True:
            time.sleep(WATCH_SECS)
            links = [link for link in list(self.links.values()) if link.state == "live"]
            if not links or self.manager.vm.state != RUNNING:
                continue
            try:
                out = adb.shell(f"cmd activity stack list; echo ===; {SECURE_QUERY}", timeout=10)
                stacks, _, secure_out = out.partition("===")
                tasks, secure = parse_tasks(stacks), parse_secure_displays(secure_out)
            except Exception:
                continue
            if not any(t.display == 0 for t in tasks):
                continue  # not a real answer (see manager.live_displays)
            for link in links:
                on = link.display in secure
                if on != link.secure:
                    link.secure = on
                    link.broadcast({"t": "secure", "on": on})
                if link.mode != "move" or time.monotonic() - link.started < 6:
                    continue
                mine = [t for t in tasks if link.package in t.packages]
                if any(t.display == link.display for t in mine):
                    link.misses = 0
                    continue
                link.misses += 1
                if link.misses >= 2:
                    elsewhere = [t for t in mine if t.display not in (0, link.display)]
                    log(f"remote: {link.package}: gone from display {link.display} "
                        f"({'opened elsewhere' if elsewhere else 'closed'})")
                    self.end(link, "pc" if elsewhere else "closed")

    # ------------------------------------------------------------------ devices
    def clients_of(self, group: str) -> list[Client]:
        with self._lock:
            return [c for c in self.clients if c.group == group]

    def client_connected(self, client: Client) -> None:
        with self._lock:
            self.clients.append(client)
        log(f"remote: {client} connected ({'https' if client.secure else 'http'})")
        self.pairing.seen(client.device.id)
        self.changed()

    def _drop_removed(self) -> None:
        """A device the user removed in the launcher loses its connections at once, not at its next one."""
        for c in list(self.clients):
            if self.pairing.get(c.device.id) is None:
                c.send_json({"t": "denied", "message": "This device was removed on the PC. Pair it again to use it."})
                c.close(4401, "removed")

    def client_gone(self, client: Client) -> None:
        with self._lock:
            if client in self.clients:
                self.clients.remove(client)
        if client.link:
            client.link.detach(client)
        log(f"remote: {client} disconnected")
        self.changed()

    def want_keyframe(self, client: Client) -> None:
        if client.link:
            client.link.keyframe()

    def client_message(self, client: Client, m: dict) -> None:
        """On the web server's loop: quick work only; anything slow goes to a thread."""
        t = m["t"]
        link = client.link
        if t == "touch":
            if link:
                link.touch(client, m)
        elif t == "scroll":
            if link:
                link.scroll(m)
        elif t == "key":
            if link:
                link.key(m["keycode"], m["meta"])
        elif t == "text":
            if link:
                link.text(m["s"])
        elif t == "nav":
            if link and m["k"] == "back":
                link.key(protocol.KEYCODE_BACK, 0)
        elif t == "ack":
            client.jpeg_busy = 0
            if link and link.jpeg:
                link.jpeg.kick()
        elif t == "keyframe":
            if link:
                link.keyframe()
        elif t == "ping":
            client.send_json({"t": "pong", "c": m["c"], "s": time.time() * 1000})
        elif t == "view":
            client.view = m
            if link and link.mode == "move" and link.group == client.group:
                link.set_view(m)
        elif t == "open":
            self._open(client, m)
        elif t == "apps":
            client.send_json({"t": "apps", "apps": self._app_list(client)})
        elif t == "back_to_pc":
            if link:
                self.bring_back(link.package)
        elif t == "upgrade":
            client.send_json({"t": "upgrade", "token": self.pairing.offer("upgrade", group=client.group)})
        elif t == "quality":
            settings = self._settings()
            settings.setdefault("quality", {})[client.group] = m["q"]
            state.put("remote", settings)
            if link and link.mode == "move" and link.group == client.group:
                link.requality(m["q"])
        elif t == "close":
            if link:
                link.detach(client)

    def _open(self, client: Client, m: dict) -> None:
        app = self.app_for_cap(m["app"])
        if app is None:
            client.send_json({"t": "error", "message": "That app isn't on this PC any more."})
            return
        client.decoder = m["decoder"]
        if "view" in m:
            client.view = m["view"]
            threading.Thread(target=self.pairing.seen, args=(client.device.id, m["view"]), daemon=True).start()
        if client.link and client.link.package != app.package:
            client.link.detach(client)
        link = self.link_for(app.package)
        if link and link.state in ("live", "starting"):
            link.attach(client)
            return
        client.send_json({"t": "app", "app": self.cap(app.package), "label": app.label, "state": "starting",
                          "mode": "move", "device": client.device.name, "secure": False})
        self.send(app.package, client.group, "move", client)

    def _app_list(self, client: Client) -> list[dict]:
        out = []
        for a in sorted(self._apps(), key=lambda a: a.label.casefold()):
            link = self.link_for(a.package)
            where = ""
            if link:
                where = "here" if link.group == client.group else self.group_name(link.group)
            elif a.package in self.manager.windows:
                where = "pc"
            cap = self.cap(a.package)
            out.append({"app": cap, "label": a.label, "where": where, "icon": f"/a/{cap}/icon.png"})
        return out

    # ------------------------------------------------------------------ for the launcher
    def snapshot(self) -> dict:
        devices = []
        for g in self.pairing.groups():
            online = bool(self.clients_of(g["group"]))
            devices.append({**g, "online": online,
                            "apps": [link.label for link in self.links.values()
                                     if link.group == g["group"] and link.state != "ended"]})
        return {"enabled": self.enabled, "listening": self.server is not None, "url": self.url(),
                "problem": self.problem, "firewall": self.firewall, "pc": self.pc_name, "devices": devices,
                "approvals": self.pairing.approvals(),
                "links": {p: {"device": self.group_name(link.group), "mode": link.mode, "state": link.state}
                          for p, link in self.links.items() if link.state != "ended"},
                "fingerprint": self.certs.fingerprint() if self.enabled else ""}
