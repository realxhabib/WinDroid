"""A small client for the Android Emulator's gRPC control API (EmulatorController), with no
dependencies: HTTP/2 over a local socket, protobuf by hand. It covers what app windows need: the
frames of a display (streamScreenshot, written into shared memory), and touch, wheel and key input.

The emulator serves gRPC on the port given with `-grpc <port>`; with `-grpc-use-token` every call
must carry the console's auth token (the same one `adb emu` uses), which keeps other programs out.
"""
from __future__ import annotations

import socket
import struct
import threading
from typing import Callable

SERVICE = "/android.emulation.control.EmulatorController/"

# HTTP/2 (RFC 9113) frame types and flags
DATA, HEADERS, RST_STREAM, SETTINGS, PING, GOAWAY, WINDOW_UPDATE, CONTINUATION = 0, 1, 3, 4, 6, 7, 8, 9
END_STREAM, ACK, END_HEADERS, PADDED, PRIORITY = 0x1, 0x1, 0x4, 0x8, 0x20
SETTINGS_HEADER_TABLE_SIZE, SETTINGS_ENABLE_PUSH, SETTINGS_INITIAL_WINDOW_SIZE, SETTINGS_MAX_FRAME_SIZE = 1, 2, 4, 5
PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
WINDOW = (1 << 31) - 1  # what we let the emulator send before it hears back from us
MAX_FRAME = 1 << 20

# ImageFormat.ImgFormat, ImageTransport.TransportChannel, KeyboardEvent enums
PNG, RGBA8888, RGB888 = 0, 1, 2
MMAP = 1
KEYDOWN, KEYUP, KEYPRESS = 0, 1, 2
CODE_USB, CODE_EVDEV, CODE_XKB, CODE_WIN = 0, 1, 2, 3


class GrpcError(Exception):
    pass


# ---------------------------------------------------------------------- protobuf by hand
def varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 64  # negative int32/int64 are ten bytes on the wire
    out = bytearray()
    while n > 0x7F:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def pb(*fields: tuple[int, int | bytes | str | None]) -> bytes:
    """A protobuf message from (field number, value) pairs: ints are varints, bytes and str are
    length-delimited (a nested message is its encoded bytes). None and 0 are left out, as proto3 does."""
    out = bytearray()
    for num, value in fields:
        if value is None or value == 0 and not isinstance(value, (bytes, str)):
            continue
        if isinstance(value, int):
            out += varint(num << 3) + varint(value)
        else:
            data = value.encode() if isinstance(value, str) else value
            out += varint(num << 3 | 2) + varint(len(data)) + data
    return bytes(out)


def _read_varint(buf: memoryview, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if b < 0x80:
            return n, i
        shift += 7


def unpb(data: bytes | memoryview) -> dict[int, list]:
    """A protobuf message's fields: number -> values (ints, or memoryviews for length-delimited ones)."""
    buf = memoryview(data)
    out: dict[int, list] = {}
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(buf, i)
        elif wire == 2:
            size, i = _read_varint(buf, i)
            value, i = buf[i:i + size], i + size
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise GrpcError(f"unsupported protobuf wire type {wire}")
        out.setdefault(num, []).append(value)
    return out


def first(fields: dict[int, list], num: int, default=0):
    values = fields.get(num)
    return values[0] if values else default


# ---------------------------------------------------------------------- HPACK, as little as we need
def _hpack_int(n: int, prefix: int, flags: int = 0) -> bytes:
    limit = (1 << prefix) - 1
    if n < limit:
        return bytes([flags | n])
    out = bytearray([flags | limit])
    n -= limit
    while n >= 0x80:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _hpack_str(s: str) -> bytes:
    data = s.encode()
    return _hpack_int(len(data), 7) + data


def encode_headers(headers: list[tuple[str, str]]) -> bytes:
    """Literal header fields without indexing, no Huffman: the simplest valid HPACK."""
    return b"".join(b"\x00" + _hpack_str(name) + _hpack_str(value) for name, value in headers)


# ---------------------------------------------------------------------- calls
class Call:
    """One gRPC call on a Channel. Messages arrive on the channel's reader thread: keep on_message quick."""

    def __init__(self, channel: Channel, stream: int, on_message: Callable[[memoryview], None] | None,
                 on_end: Callable[[str], None] | None) -> None:
        self.channel = channel
        self.stream = stream
        self.on_message = on_message
        self.on_end = on_end
        self.send_window = channel.peer_initial_window
        self.done = threading.Event()
        self.error = ""  # why the call ended, if not cleanly
        self.messages: list[bytes] = []  # unary calls keep their answer here
        self._buf = bytearray()
        self._unacked = 0
        self._closed_local = False

    def send(self, message: bytes, end: bool = False) -> None:
        """Send one request message (client streaming)."""
        if self.done.is_set() or self._closed_local:
            raise GrpcError(self.error or "call ended")
        self._closed_local = end
        self.channel.send_data(self, b"\x00" + struct.pack(">I", len(message)) + message, end)

    def close(self) -> None:
        """Half-close a client stream, or cancel a server stream."""
        if self.done.is_set():
            return
        if not self._closed_local:
            self._closed_local = True
            try:
                self.channel.send_data(self, b"", True)
            except (OSError, GrpcError):
                pass

    def cancel(self) -> None:
        if not self.done.is_set():
            try:
                self.channel.frame(RST_STREAM, 0, self.stream, struct.pack(">I", 8))  # CANCEL
            except OSError:
                pass
            self.channel.finish(self, "canceled")

    # reader thread
    def _data(self, payload: memoryview) -> None:
        self._buf += payload
        self._unacked += len(payload)
        while len(self._buf) >= 5:
            size = struct.unpack_from(">I", self._buf, 1)[0]
            if len(self._buf) < 5 + size:
                break
            if self._buf[0]:
                raise GrpcError("compressed gRPC message")  # we never offer compression
            message = bytes(self._buf[5:5 + size])
            del self._buf[:5 + size]
            if self.on_message:
                self.on_message(memoryview(message))
            else:
                self.messages.append(message)


class Channel:
    """An HTTP/2 connection to the emulator's gRPC port, shared by any number of calls."""

    def __init__(self, port: int, token: str = "", host: str = "127.0.0.1", timeout: float = 5) -> None:
        self.port = port
        self.token = token
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(None)
        self._rfile = self.sock.makefile("rb", buffering=1 << 18)
        self._send_lock = threading.Lock()
        self._cv = threading.Condition()
        self._calls: dict[int, Call] = {}
        self._next_stream = 1
        self.peer_initial_window = 65535
        self.peer_window = 65535  # connection-level send window
        self.peer_max_frame = 16384
        self.closed = threading.Event()
        self.error = ""
        self._unacked = 0
        settings = b"".join(struct.pack(">HI", k, v) for k, v in (
            (SETTINGS_HEADER_TABLE_SIZE, 0),  # no dynamic table: we never need to decode its headers
            (SETTINGS_ENABLE_PUSH, 0),
            (SETTINGS_INITIAL_WINDOW_SIZE, WINDOW),
            (SETTINGS_MAX_FRAME_SIZE, MAX_FRAME)))
        self.sock.sendall(PREFACE + self._pack(SETTINGS, 0, 0, settings) +
                          self._pack(WINDOW_UPDATE, 0, 0, struct.pack(">I", WINDOW - 65535)))
        threading.Thread(target=self._read, daemon=True, name=f"grpc-{port}").start()

    # ------------------------------------------------------------------ sending
    @staticmethod
    def _pack(kind: int, flags: int, stream: int, payload: bytes = b"") -> bytes:
        return struct.pack(">I", len(payload))[1:] + bytes([kind, flags]) + struct.pack(">I", stream) + payload

    def frame(self, kind: int, flags: int, stream: int, payload: bytes = b"") -> None:
        with self._send_lock:
            self.sock.sendall(self._pack(kind, flags, stream, payload))

    def send_data(self, call: Call, data: bytes, end: bool) -> None:
        view = memoryview(data)
        while True:
            with self._cv:
                while not (len(view) == 0 or min(self.peer_window, call.send_window) > 0):
                    if call.done.is_set() or self.closed.is_set():
                        raise GrpcError(call.error or self.error or "call ended")
                    self._cv.wait(1)
                n = min(len(view), self.peer_window, call.send_window, self.peer_max_frame)
                self.peer_window -= n
                call.send_window -= n
            last = n == len(view)
            self.frame(DATA, END_STREAM if end and last else 0, call.stream, bytes(view[:n]))
            view = view[n:]
            if last:
                return

    def call(self, method: str, request: bytes | None = None, on_message: Callable[[memoryview], None] | None = None,
             on_end: Callable[[str], None] | None = None) -> Call:
        """Start a call. With a request, it's sent and the request side closed (unary or server
        streaming); without one, send messages with call.send() (client streaming)."""
        if self.closed.is_set():
            raise GrpcError(self.error or "not connected")
        headers = [(":method", "POST"), (":scheme", "http"), (":path", SERVICE + method),
                   (":authority", f"localhost:{self.port}"), ("content-type", "application/grpc"),
                   ("te", "trailers")]
        if self.token:
            headers.append(("authorization", f"Bearer {self.token}"))
        with self._cv:
            stream = self._next_stream
            self._next_stream += 2
            call = self._calls[stream] = Call(self, stream, on_message, on_end)
        self.frame(HEADERS, END_HEADERS, stream, encode_headers(headers))
        if request is not None:
            call.send(request, end=True)
        return call

    def unary(self, method: str, request: bytes = b"", timeout: float = 10) -> dict[int, list]:
        call = self.call(method, request)
        if not call.done.wait(timeout):
            call.cancel()
            raise GrpcError(f"{method}: no answer")
        if not call.messages:
            raise GrpcError(f"{method}: {call.error or 'no answer'}")
        return unpb(call.messages[0])

    def close(self) -> None:
        if not self.closed.is_set():
            try:
                self.frame(GOAWAY, 0, 0, struct.pack(">II", 0, 0))
            except OSError:
                pass
        self._fail("closed")
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ receiving
    def finish(self, call: Call, error: str = "") -> None:
        with self._cv:
            if self._calls.pop(call.stream, None) is None:
                return
            self._cv.notify_all()
        call.error = error
        call.done.set()
        if call.on_end:
            try:
                call.on_end(error)
            except Exception:
                pass

    def _fail(self, error: str) -> None:
        if not self.closed.is_set():
            self.error = error
            self.closed.set()
        for call in list(self._calls.values()):
            self.finish(call, error)

    def _read_exact(self, n: int) -> bytes:
        data = self._rfile.read(n)
        if len(data) != n:
            raise ConnectionError("connection closed")
        return data

    def _read(self) -> None:
        try:
            while True:
                head = self._read_exact(9)
                length = int.from_bytes(head[:3], "big")
                kind, flags = head[3], head[4]
                stream = int.from_bytes(head[5:9], "big") & 0x7FFFFFFF
                payload = memoryview(self._read_exact(length)) if length else memoryview(b"")
                self._frame_in(kind, flags, stream, payload)
        except (OSError, ValueError, GrpcError) as e:
            self._fail(str(e) or type(e).__name__)

    def _frame_in(self, kind: int, flags: int, stream: int, payload: memoryview) -> None:
        call = self._calls.get(stream) if stream else None
        if kind == DATA:
            if flags & PADDED:
                payload = payload[1:len(payload) - payload[0]]
            self._unacked += len(payload)
            if self._unacked > WINDOW // 2:
                self.frame(WINDOW_UPDATE, 0, 0, struct.pack(">I", self._unacked))
                self._unacked = 0
            if call:
                call._data(payload)
                if call._unacked > WINDOW // 2 and not flags & END_STREAM:
                    self.frame(WINDOW_UPDATE, 0, stream, struct.pack(">I", call._unacked))
                    call._unacked = 0
                if flags & END_STREAM:
                    self.finish(call)
        elif kind in (HEADERS, CONTINUATION):
            # Response headers, or trailers that end the call. Their contents (grpc-status) may be
            # Huffman-coded; an ended call that brought no message is error enough for us.
            if call and kind == HEADERS and flags & END_STREAM:
                self.finish(call, "" if call.messages or call.on_message else "ended without an answer")
        elif kind == RST_STREAM:
            if call:
                code = struct.unpack(">I", payload[:4])[0] if len(payload) >= 4 else 0
                self.finish(call, f"reset by the emulator (HTTP/2 error {code})")
        elif kind == SETTINGS:
            if flags & ACK:
                return
            for i in range(0, len(payload) - 5, 6):
                key, value = struct.unpack_from(">HI", payload, i)
                if key == SETTINGS_INITIAL_WINDOW_SIZE:
                    with self._cv:
                        delta = value - self.peer_initial_window
                        self.peer_initial_window = value
                        for c in self._calls.values():
                            c.send_window += delta
                        self._cv.notify_all()
                elif key == SETTINGS_MAX_FRAME_SIZE:
                    self.peer_max_frame = value
            self.frame(SETTINGS, ACK, 0)
        elif kind == PING:
            if not flags & ACK:
                self.frame(PING, ACK, 0, bytes(payload))
        elif kind == WINDOW_UPDATE:
            increment = struct.unpack(">I", payload[:4])[0] & 0x7FFFFFFF
            with self._cv:
                if stream == 0:
                    self.peer_window += increment
                elif call:
                    call.send_window += increment
                self._cv.notify_all()
        elif kind == GOAWAY:
            code = struct.unpack(">I", payload[4:8])[0] if len(payload) >= 8 else 0
            raise GrpcError(f"the emulator closed the connection (HTTP/2 error {code})")


# ---------------------------------------------------------------------- EmulatorController
def image_format(display: int, fmt: int = RGBA8888, width: int = 0, height: int = 0, handle: str = "") -> bytes:
    transport = pb((1, MMAP), (2, handle)) if handle else None
    return pb((1, fmt), (3, width), (4, height), (5, display), (6, transport))


def parse_image(message: memoryview) -> tuple[int, int, int, int, memoryview | None]:
    """An Image: (width, height, rotation, seq, pixels); pixels is None with shared-memory transport."""
    f = unpb(message)
    fmt = unpb(first(f, 1, b""))
    rotation = unpb(first(fmt, 2, b""))
    pixels = first(f, 4, None)
    return (first(fmt, 3) or first(f, 2), first(fmt, 4) or first(f, 3), first(rotation, 1), first(f, 5),
            pixels if pixels is not None and len(pixels) else None)


def touch(x: int, y: int, identifier: int = 0, pressure: int = 1) -> bytes:
    """One finger. pressure 0 lifts it. NEVER_EXPIRE: we always lift what we put down."""
    return pb((1, x), (2, y), (3, identifier), (4, pressure), (7, 1))


def touch_event(display: int, *touches: bytes) -> bytes:
    """An InputEvent with a TouchEvent."""
    return pb((2, b"".join(pb((1, t)) for t in touches) + pb((2, display))))


def mouse_event(display: int, x: int, y: int, buttons: int = 0) -> bytes:
    return pb((3, pb((1, x), (2, y), (3, buttons), (4, display))))


def wheel_event(display: int, dy: int, dx: int = 0) -> bytes:
    """dy: 120 per notch, positive away from the user (scrolls up)."""
    return pb((6, pb((1, dx), (2, dy), (3, display))))


def key_event(key: str = "", keycode: int = 0, kind: int = KEYPRESS, code_type: int = CODE_EVDEV,
              text: str = "") -> bytes:
    return pb((1, pb((1, code_type), (2, kind), (3, keycode), (4, key or None), (5, text or None))))
