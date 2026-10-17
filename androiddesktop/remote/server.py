"""The web server iPhones and iPads connect to: HTTP and HTTPS on one port, on the home network only.

It listens on the PC's own address on its home Wi-Fi or Ethernet (a private address, on a network
Windows calls Private), never on all interfaces, and it hangs up on anyone outside that subnet. The
first byte of a connection says whether it's TLS (https://, needed for WebCodecs) or plain HTTP (the
fallback that needs no certificate). Pages, scripts and the pairing API are open; everything about
the user's apps needs a paired device: the WebSocket (video and input) starts with a challenge-
response (pairing.py), and an app's page and icon live at a capability URL a paired device learns
over that WebSocket.

One asyncio loop on a thread of its own does all the network I/O, TLS included.
"""
from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PIL import Image

from ..log import log
from . import pairing, protocol
from .pairing import MessageVerifier, PairingError, b64e

if TYPE_CHECKING:
    from .hub import Hub

PORT = 47821
WEB = Path(__file__).with_name("web")
STATIC = {"app.js": "text/javascript", "wdcrypto.js": "text/javascript", "style.css": "text/css"}
HEAD_LIMIT = 16 * 1024
BODY_LIMIT = 16 * 1024
IDLE_SECS = 60
AUTH_SECS = 10
WRITE_LIMIT = 1_500_000  # bytes waiting to go to a phone before video frames are dropped
ICON = Path(__file__).resolve().parents[1] / "assets" / "icon.ico"
# For developing the web app on this PC: also listen on 127.0.0.1, which browsers count as a secure
# context (WebCodecs works there without a certificate). Loopback never reaches the network.
LOOPBACK = os.environ.get("ANDROIDDESKTOP_REMOTE_LOOPBACK") == "1"


# ---------------------------------------------------------------------- the home network
@dataclass(frozen=True)
class Lan:
    ip: str
    network: ipaddress.IPv4Network
    adapter: str
    private: bool  # Windows' network category: Private (or a domain), not Public

    def __contains__(self, address: str) -> bool:
        try:
            return ipaddress.ip_address(address) in self.network
        except ValueError:
            return False


def choose_lan(candidates: list[Lan], outbound: str | None) -> Lan | None:
    """The address to listen on: a private one on a Private network, the one the PC's internet traffic
    leaves from if that's among them."""
    usable = [c for c in candidates if c.private]
    for c in usable:
        if c.ip == outbound:
            return c
    return usable[0] if usable else None


def outbound_ip() -> str | None:
    """The address the PC's traffic to the internet leaves from (a UDP "connect" sends nothing)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def lan_candidates() -> list[Lan]:
    """Private IPv4 addresses on adapters with a gateway, with Windows' network category for each."""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        categories: dict[int, int] = {}
        try:
            std = win32com.client.GetObject(r"winmgmts:root\StandardCimv2")
            for p in std.ExecQuery("SELECT InterfaceIndex, NetworkCategory FROM MSFT_NetConnectionProfile"):
                categories[int(p.InterfaceIndex)] = int(p.NetworkCategory)  # 0 public, 1 private, 2 domain
        except Exception:
            pass
        wmi = win32com.client.GetObject(r"winmgmts:root\cimv2")
        found = []
        for a in wmi.ExecQuery("SELECT InterfaceIndex, IPAddress, IPSubnet, DefaultIPGateway, Description "
                               "FROM Win32_NetworkAdapterConfiguration WHERE IPEnabled = TRUE"):
            if not a.DefaultIPGateway:
                continue
            for ip, mask in zip(a.IPAddress or (), a.IPSubnet or ()):
                if ":" in ip:
                    continue
                address = ipaddress.ip_address(ip)
                if not address.is_private or address.is_link_local or address.is_loopback:
                    continue
                found.append(Lan(ip, ipaddress.ip_network(f"{ip}/{mask}", strict=False), str(a.Description),
                                 categories.get(int(a.InterfaceIndex), 0) != 0))
        return found
    finally:
        pythoncom.CoUninitialize()


# ---------------------------------------------------------------------- HTTP bits
class HttpError(Exception):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes = b""


@dataclass
class Response:
    status: int
    body: bytes = b""
    type: str = "text/plain; charset=utf-8"
    headers: dict[str, str] | None = None
    cache: bool = False


REASONS = {200: "OK", 204: "No Content", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 413: "Payload Too Large", 421: "Misdirected Request", 429: "Too Many Requests",
           500: "Internal Server Error"}


def parse_head(head: bytes) -> Request:
    try:
        text = head.decode("latin-1")
        line, *rest = text.split("\r\n")
        method, target, version = line.split(" ")
    except ValueError as e:
        raise HttpError(400, "bad request line") from e
    if not version.startswith("HTTP/1.") or not target.startswith("/"):
        raise HttpError(400, "bad request line")
    headers: dict[str, str] = {}
    for h in rest:
        if not h:
            continue
        name, sep, value = h.partition(":")
        if not sep or not name or name != name.strip():
            raise HttpError(400, "bad header")
        headers[name.lower()] = value.strip()
    parts = urllib.parse.urlsplit(target)
    query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query, keep_blank_values=True).items()}
    return Request(method, urllib.parse.unquote(parts.path), query, headers)


def response_bytes(r: Response, keep_alive: bool, extra: dict[str, str]) -> bytes:
    headers = {"Content-Type": r.type, "Content-Length": str(len(r.body)),
               "Cache-Control": "max-age=300" if r.cache else "no-store",
               "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
               "Connection": "keep-alive" if keep_alive else "close", **extra, **(r.headers or {})}
    head = f"HTTP/1.1 {r.status} {REASONS.get(r.status, 'OK')}\r\n" + "".join(
        f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    return head.encode("latin-1") + r.body


def json_response(obj: object, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(status, json.dumps(obj).encode("utf-8"), "application/json", headers)


# ---------------------------------------------------------------------- WebSocket connection
class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.closed = False

    async def recv(self) -> str:
        """The next text message; pings are answered, a close or binary message ends things."""
        parts: list[bytes] = []
        while True:
            final, opcode, code, extra = protocol.ws_header(await self.reader.readexactly(2))
            length, mask = protocol.ws_length(code, await self.reader.readexactly(extra))
            payload = protocol.ws_unmask(await self.reader.readexactly(length), mask)
            if opcode == protocol.OP_PING:
                self.send(protocol.OP_PONG, payload[:125])
                continue
            if opcode == protocol.OP_PONG:
                continue
            if opcode == protocol.OP_CLOSE:
                raise ConnectionResetError("closed by the phone")
            if opcode not in (protocol.OP_TEXT, protocol.OP_CONT) or (opcode == protocol.OP_CONT) != bool(parts):
                raise protocol.WebSocketError("unexpected frame")
            parts.append(payload)
            if sum(len(p) for p in parts) > protocol.MAX_MESSAGE:
                raise protocol.WebSocketError("message too big")
            if final:
                return b"".join(parts).decode("utf-8")

    def send(self, opcode: int, payload: bytes) -> None:
        if not self.closed:
            self.writer.write(protocol.ws_frame(opcode, payload))

    def send_json(self, obj: object) -> None:
        self.send(protocol.OP_TEXT, json.dumps(obj, separators=(",", ":")).encode("utf-8"))

    def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.closed:
            self.send(protocol.OP_CLOSE, code.to_bytes(2, "big") + reason.encode("utf-8")[:120])
            self.closed = True
            self.writer.close()

    @property
    def buffered(self) -> int:
        return self.writer.transport.get_write_buffer_size() if self.writer.transport else 0


class Client:
    """One phone or tablet connection, after it proved it's a paired device. The hub calls it from any
    thread; everything that touches the socket hops onto the server's loop."""

    def __init__(self, server: WebServer, ws: WebSocket, device: pairing.Device, secure: bool, peer: str,
                 verifier: MessageVerifier) -> None:
        self.server, self.ws, self.device, self.secure, self.peer = server, ws, device, secure, peer
        self.verifier = verifier
        self.loop = server.loop
        self.decoder: str | None = None  # "webcodecs" or "jpeg", once it opens an app
        self.link = None  # hub.Link it's watching
        self.view: dict | None = None
        self.need_key = True
        self.jpeg_busy = False  # a JPEG frame sent and not acknowledged yet
        self.jpeg_seq = 0
        self.connected = time.monotonic()
        self.bad = 0

    @property
    def group(self) -> str:
        return self.device.group

    def __repr__(self) -> str:
        return f"<{self.device.name} {self.device.id[:4]} {self.peer}>"

    def send_json(self, obj: dict) -> None:
        self.loop.call_soon_threadsafe(self.ws.send_json, obj)

    def send_video(self, kind: int, sent_ms: float, data: bytes) -> None:
        self.loop.call_soon_threadsafe(self._video, kind, sent_ms, data)

    def _video(self, kind: int, sent_ms: float, data: bytes) -> None:
        if self.ws.closed:
            return
        backlog = self.ws.buffered
        if kind == protocol.VIDEO_DELTA:
            if self.need_key:
                return
            if backlog > WRITE_LIMIT:  # the Wi-Fi can't keep up: skip to the next key frame
                self.need_key = True
                self.server.hub.want_keyframe(self)
                return
        elif kind == protocol.VIDEO_KEY:
            if backlog > 2 * WRITE_LIMIT:
                self.need_key = True
                return
            self.need_key = False
        self.ws.send(protocol.OP_BINARY, protocol.video_message(kind, sent_ms, data))

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.loop.call_soon_threadsafe(self.ws.close, code, reason)


# ---------------------------------------------------------------------- the server
class WebServer:
    def __init__(self, hub: Hub, lan: Lan, port: int = PORT) -> None:
        self.hub, self.lan, self.port = hub, lan, port
        self.loop = asyncio.SelectorEventLoop()  # add_reader, to peek at a connection's first byte
        self.ssl = None
        self._listeners: list[socket.socket] = []
        self._thread: threading.Thread | None = None
        self._tasks: set[asyncio.Task] = set()
        self._icons: dict[tuple[str, int], tuple[float, bytes]] = {}
        self.pc_name = hub.pc_name

    @property
    def host(self) -> str:
        return f"{self.lan.ip}:{self.port}"

    @property
    def hosts(self) -> tuple[str, ...]:
        """Host headers we answer to."""
        own = (self.host, self.lan.ip)
        return own + ((f"127.0.0.1:{self.port}",) if LOOPBACK else ())

    def url(self, secure: bool = False) -> str:
        return f"{'https' if secure else 'http'}://{self.host}"

    def start(self) -> None:
        import ssl
        cert, key, password = self.hub.certs.server(self.lan.ip)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(cert), str(key), password=password)
        ctx.set_alpn_protocols(["http/1.1"])
        self.ssl = ctx
        for ip in (self.lan.ip, "127.0.0.1") if LOOPBACK else (self.lan.ip,):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # nobody else may bind our port while we have it
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((ip, self.port))  # this one address: never 0.0.0.0
            s.listen(32)
            s.setblocking(False)
            self._listeners.append(s)
        self._thread = threading.Thread(target=self._run, daemon=True, name="remote-web")
        self._thread.start()
        log(f"remote: listening on {self.url()} and {self.url(True)} ({self.lan.adapter})")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        for s in self._listeners:
            self.loop.create_task(self._accept(s))
        self.loop.run_forever()

    def stop(self) -> None:
        def shutdown() -> None:
            for t in list(self._tasks):
                t.cancel()
            for s in self._listeners:
                s.close()
            self.loop.call_later(0.2, self.loop.stop)
        if self._thread and self._thread.is_alive():
            self.loop.call_soon_threadsafe(shutdown)
            self._thread.join(3)
        else:
            for s in self._listeners:
                s.close()

    def call(self, fn: Callable[[], None]) -> None:
        self.loop.call_soon_threadsafe(fn)

    # ------------------------------------------------------------------ connections
    async def _accept(self, listener: socket.socket) -> None:
        loopback = listener.getsockname()[0] == "127.0.0.1"
        while True:
            try:
                sock, addr = await self.loop.sock_accept(listener)
            except (OSError, asyncio.CancelledError):
                return
            if not (addr[0] in self.lan or (loopback and addr[0] == "127.0.0.1")):
                log(f"remote: refused a connection from {addr[0]} (not on {self.lan.network})")
                sock.close()
                continue
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            task = self.loop.create_task(self._connection(sock, addr[0]))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _first_byte(self, sock: socket.socket) -> bytes:
        ready = self.loop.create_future()
        self.loop.add_reader(sock.fileno(), lambda: ready.done() or ready.set_result(None))
        try:
            await asyncio.wait_for(ready, 15)
        finally:
            self.loop.remove_reader(sock.fileno())
        return sock.recv(1, socket.MSG_PEEK)

    async def _connection(self, sock: socket.socket, peer: str) -> None:
        writer = None
        try:
            first = await self._first_byte(sock)
            if not first:
                sock.close()
                return
            tls = first == b"\x16"  # a TLS ClientHello
            reader = asyncio.StreamReader(limit=HEAD_LIMIT * 2, loop=self.loop)
            proto = asyncio.StreamReaderProtocol(reader, loop=self.loop)
            transport, _ = await self.loop.connect_accepted_socket(
                lambda: proto, sock, ssl=self.ssl if tls else None, ssl_handshake_timeout=10 if tls else None)
            writer = asyncio.StreamWriter(transport, proto, reader, self.loop)
            await self._http(reader, writer, tls, peer)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError,
                asyncio.CancelledError):
            pass
        except Exception as e:
            log(f"remote: connection from {peer}: {e!r}")
        finally:
            if writer:
                writer.close()
            else:
                sock.close()

    async def _http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, tls: bool, peer: str) -> None:
        while True:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), IDLE_SECS)
            except asyncio.LimitOverrunError:
                writer.write(response_bytes(Response(413, b"too big"), False, {}))
                return
            req = None
            try:
                req = parse_head(head[:-4])
                self._check_host(req)
                length = int(req.headers.get("content-length", "0") or 0)
                if length > BODY_LIMIT or length < 0:
                    raise HttpError(413, "too big")
                if length:
                    req.body = await asyncio.wait_for(reader.readexactly(length), 15)
                if req.path == "/ws" and req.headers.get("upgrade", "").lower() == "websocket":
                    await self._websocket(reader, writer, req, tls, peer)
                    return
                res = self.route(req, tls, peer)
            except HttpError as e:
                res = Response(e.status, str(e).encode("utf-8"))
            except ValueError:
                res = Response(400, b"bad request")
            keep = req is not None and req.headers.get("connection", "").lower() != "close"
            host = req.headers.get("host") if req is not None and req.headers.get("host") in self.hosts else None
            writer.write(response_bytes(res, keep, self._security_headers(tls, host)))
            await writer.drain()
            if not keep:
                return

    def _check_host(self, req: Request) -> None:
        """Only requests for our own address: a page elsewhere can't reach us through DNS rebinding."""
        if req.headers.get("host") not in self.hosts:
            raise HttpError(421, "wrong host")

    def _security_headers(self, tls: bool, host: str | None = None) -> dict[str, str]:
        host = host or self.host
        other = f"{'http' if tls else 'https'}://{host}"
        csp = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
               f"connect-src 'self' ws://{host} wss://{host} {other}; manifest-src 'self'; "
               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        return {"Content-Security-Policy": csp, "X-Frame-Options": "DENY"}

    # ------------------------------------------------------------------ pages and the pairing API
    def route(self, req: Request, tls: bool, peer: str) -> Response:
        path = req.path
        if req.method == "OPTIONS" and path == "/api/ping":
            return Response(204, headers=self._cors(req))
        if req.method == "POST":
            if path == "/api/pair/start":
                body = self._json(req)
                return self._pairing(lambda: self.hub.pairing.start(body.get("pub"), body.get("name"), peer))
            if path == "/api/pair/finish":
                body = self._json(req)
                res = self._pairing(lambda: self.hub.pairing.finish(body.get("id"), body.get("proof")))
                outcome = json.loads(res.body)
                log(f"remote: pairing from {peer}: " + (f"paired {outcome['name']!r}" if "device" in outcome else
                                                        "asks to be allowed" if outcome.get("pending") else
                                                        f"refused ({outcome.get('error')})"))
                return res
            raise HttpError(405 if path.startswith("/api/") else 404, "not here")
        if req.method != "GET":
            raise HttpError(405, "GET or POST")
        if path == "/api/ping":
            return json_response({"pc": self.pc_name, "secure": tls}, headers=self._cors(req))
        if path == "/api/pair/status":
            return json_response(self.hub.pairing.status(req.query.get("id")))
        host = req.headers.get("host") or self.host
        if path in ("/", "/index.html"):
            return self._page(None, host)
        if path.startswith("/s/") and path[3:] in STATIC:
            return Response(200, (WEB / path[3:]).read_bytes(), STATIC[path[3:]] + "; charset=utf-8", cache=False)
        if path == "/icon.png":
            return Response(200, self._icon(None, 180), "image/png", cache=True)
        if path == "/manifest.webmanifest":
            return json_response(self._manifest(None))
        if path in ("/windroid.mobileconfig", "/trust"):
            return Response(200, self.hub.certs.mobileconfig(), "application/x-apple-aspen-config")
        if path.startswith("/a/"):
            parts = path.split("/")  # "", "a", cap, rest
            app = self.hub.app_for_cap(parts[2]) if len(parts) >= 4 else None
            if app is None:
                raise HttpError(404, "no such app")
            rest = "/".join(parts[3:])
            if rest == "":
                return self._page(app, host)
            if rest == "icon.png":
                return Response(200, self._icon(app, 180), "image/png", cache=True)
            if rest == "icon-512.png":
                return Response(200, self._icon(app, 512), "image/png", cache=True)
            if rest == "manifest.webmanifest":
                return Response(200, json.dumps(self._manifest(app)).encode("utf-8"), "application/manifest+json")
        raise HttpError(404, "not found")

    def _cors(self, req: Request) -> dict[str, str]:
        """The http:// page asks the https:// one whether the certificate is trusted yet, and vice versa."""
        origin = req.headers.get("origin", "")
        if origin in {f"{scheme}://{h}" for scheme in ("http", "https") for h in self.hosts if ":" in h}:
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        return {}

    @staticmethod
    def _json(req: Request) -> dict:
        try:
            body = json.loads(req.body or b"{}")
        except ValueError as e:
            raise HttpError(400, "JSON wanted") from e
        if not isinstance(body, dict):
            raise HttpError(400, "JSON object wanted")
        return body

    @staticmethod
    def _pairing(fn: Callable[[], dict]) -> Response:
        try:
            return json_response(fn())
        except PairingError as e:
            return json_response({"error": str(e)}, 400)

    def _page(self, app, host: str | None = None) -> Response:
        html = (WEB / "app.html").read_text(encoding="utf-8")
        if app is None:
            title, base, config = "WinDroid", "/", {"app": None}
        else:
            cap = self.hub.cap(app.package)
            title, base, config = app.label, f"/a/{cap}/", {"app": cap, "label": app.label}
        fp = self.hub.certs.fingerprint()[:23] if self.hub.certs else ""
        config.update(pc=self.pc_name, host=host if host in self.hosts else self.host, version=1, fp=fp)
        esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
                         .replace('"', "&quot;"))
        html = (html.replace("{{TITLE}}", esc(title)).replace("{{BASE}}", esc(base))
                .replace("{{CONFIG}}", esc(json.dumps(config))))
        return Response(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _manifest(self, app) -> dict:
        if app is None:
            name, base = "WinDroid", "/"
        else:
            name, base = app.label, f"/a/{self.hub.cap(app.package)}/"
        icons = [{"src": f"{base}icon.png", "sizes": "180x180", "type": "image/png"}]
        if app is not None:
            icons.append({"src": f"{base}icon-512.png", "sizes": "512x512", "type": "image/png"})
        return {"name": name, "short_name": name[:12], "id": base, "start_url": base, "scope": base,
                "display": "standalone", "background_color": "#000000", "theme_color": "#000000", "icons": icons}

    def _icon(self, app, size: int) -> bytes:
        """An app's icon for the Home Screen: on a white square (iOS fills transparency with black and
        rounds the corners itself)."""
        src = app.icon if app is not None else ICON
        key = (str(src), size)
        try:
            mtime = src.stat().st_mtime
        except OSError:
            mtime = 0
        cached = self._icons.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        canvas = Image.new("RGB", (size, size), (255, 255, 255))
        try:
            img = Image.open(src)
            if src.suffix == ".ico":
                img.size = max(img.ico.sizes())
            img = img.convert("RGBA")
            inner = round(size * 0.84)
            img = img.resize((inner, inner), Image.LANCZOS)
            canvas.paste(img, ((size - inner) // 2, (size - inner) // 2), img)
        except (OSError, ValueError):
            pass
        buf = io.BytesIO()
        canvas.save(buf, "PNG", optimize=True)
        self._icons[key] = (mtime, buf.getvalue())
        return buf.getvalue()

    # ------------------------------------------------------------------ the WebSocket
    async def _websocket(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, req: Request,
                         tls: bool, peer: str) -> None:
        if req.headers.get("origin") not in {f"{'https' if tls else 'http'}://{h}" for h in self.hosts if ":" in h}:
            writer.write(response_bytes(Response(403, b"wrong origin"), False, {}))
            return
        key = req.headers.get("sec-websocket-key", "")
        if req.headers.get("sec-websocket-version") != "13" or len(key) < 16:
            writer.write(response_bytes(Response(400, b"bad websocket request"), False, {}))
            return
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {protocol.ws_accept(key)}\r\n\r\n").encode("ascii"))
        ws = WebSocket(reader, writer)
        nonce = pairing.nonce()
        ws.send_json({"t": "hello", "v": 1, "nonce": nonce, "pc": self.pc_name})
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), AUTH_SECS))
        except (ValueError, asyncio.TimeoutError):
            ws.close(4400, "no auth")
            return
        device = (self.hub.pairing.authenticate(m.get("device"), nonce, m.get("cnonce"), m.get("proof"))
                  if isinstance(m, dict) and m.get("t") == "auth" else None)
        if device is None:
            log(f"remote: {peer} failed to authenticate")
            ws.send_json({"t": "denied", "message": "This device isn't paired with this PC (any more)."})
            ws.close(4401, "not paired")
            return
        cnonce = m["cnonce"]
        client = Client(self, ws, device, tls, peer,
                        MessageVerifier(pairing.session_key(device.secret, nonce, cnonce)))
        ws.send_json({"t": "welcome", "proof": b64e(pairing.server_proof(device.secret, nonce, cnonce)),
                      "name": device.name, "secure": tls})
        self.hub.client_connected(client)
        last = time.monotonic()
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(ws.recv(), 20)
                except asyncio.TimeoutError:
                    if time.monotonic() - last > 45:
                        break  # the phone went away without saying so (Wi-Fi off, asleep)
                    ws.send(protocol.OP_PING, b"")
                    continue
                last = time.monotonic()
                try:
                    message = protocol.parse_message(client.verifier.open(frame))
                except PairingError as e:
                    log(f"remote: {client}: {e}; hanging up")
                    break
                except protocol.BadMessage as e:
                    client.bad += 1
                    if client.bad in (1, 10, 100):
                        log(f"remote: {client}: bad message ({e})")
                    if client.bad > 500:
                        break
                    continue
                self.hub.client_message(client, message)
        except (asyncio.IncompleteReadError, ConnectionError, protocol.WebSocketError, UnicodeDecodeError):
            pass
        finally:
            ws.close()
            self.hub.client_gone(client)
