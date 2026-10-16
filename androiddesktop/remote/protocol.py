"""Wire formats: scrcpy's video stream and control messages, WebSocket frames, and the phone's messages.

Everything here is pure (bytes and dicts in, bytes and dicts out), so it's tested without Android or
a network.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
from dataclasses import dataclass

# ---------------------------------------------------------------------- scrcpy video stream
# scrcpy-server 5.0.1, after the 4-byte codec id, sends 12-byte headers:
#   session (top bit set): flags u32 (bit 0: the client asked for this size), width u32, height u32
#   media packet: pts_and_flags u64 (bit 62: codec config, bit 61: key frame), size u32, then data.
CODEC_H264 = b"h264"
FLAG_CONFIG = 1 << 62
FLAG_KEY = 1 << 61
PTS_MASK = (1 << 61) - 1


@dataclass(frozen=True)
class Session:
    width: int
    height: int
    resized: bool  # the size we asked for (RESIZE_DISPLAY), rather than a capture reset


@dataclass(frozen=True)
class PacketHeader:
    pts: int
    config: bool
    key: bool
    size: int


def parse_header(header: bytes) -> Session | PacketHeader:
    if len(header) != 12:
        raise ValueError("a scrcpy packet header is 12 bytes")
    if header[0] & 0x80:
        flags, width, height = struct.unpack(">III", header)
        return Session(width, height, bool(flags & 1))
    pts_flags, size = struct.unpack(">QI", header)
    return PacketHeader(pts_flags & PTS_MASK, bool(pts_flags & FLAG_CONFIG), bool(pts_flags & FLAG_KEY), size)


def avc_codec(config: bytes) -> str | None:
    """The WebCodecs codec string ("avc1.PPCCLL") from a config packet's SPS (Annex B)."""
    i = 0
    while (i := config.find(b"\x00\x00\x01", i)) >= 0:
        nal = config[i + 3:i + 7]
        if len(nal) < 4:
            return None
        if nal[0] & 0x1F == 7:
            return "avc1.%02X%02X%02X" % (nal[1], nal[2], nal[3])
        i += 3
    return None


# ---------------------------------------------------------------------- scrcpy control messages
INJECT_KEYCODE, INJECT_TEXT, INJECT_TOUCH, INJECT_SCROLL = 0, 1, 2, 3
SET_CLIPBOARD, START_APP, RESET_VIDEO, RESIZE_DISPLAY = 9, 16, 17, 21
ACTION_DOWN, ACTION_UP, ACTION_MOVE, ACTION_CANCEL = 0, 1, 2, 3  # MotionEvent; KeyEvent uses 0/1
INJECT_TEXT_MAX = 300  # chars per message, as scrcpy-server reads them
KEYCODE_BACK, KEYCODE_ENTER, KEYCODE_DEL = 4, 66, 67
META_SHIFT, META_ALT, META_CTRL, META_META = 0x1, 0x2, 0x1000, 0x10000


def key_message(action: int, keycode: int, meta: int = 0, repeat: int = 0) -> bytes:
    return struct.pack(">BBiii", INJECT_KEYCODE, action, keycode, repeat, meta)


def text_message(text: str) -> bytes:
    data = text.encode("utf-8")
    return struct.pack(">BI", INJECT_TEXT, len(data)) + data


def touch_message(action: int, pointer: int, x: int, y: int, width: int, height: int,
                  pressure: float = 1.0) -> bytes:
    """A finger (pointer >= 0) on the video, in the coordinates of a video frame width x height. scrcpy
    drops it if that isn't the size it's streaming now (a frame from before a resize)."""
    p = 0 if action == ACTION_UP else max(0, min(0xFFFF, round(pressure * 0xFFFF)))
    return struct.pack(">BBqiiHHHii", INJECT_TOUCH, action, pointer, x, y, width, height, p, 0, 0)


def scroll_message(x: int, y: int, width: int, height: int, dx: float, dy: float) -> bytes:
    """dx/dy in notches (-16..16), as a mouse wheel would give them."""
    def fixed(v: float) -> int:
        return max(-0x8000, min(0x7FFF, round(v / 16 * 0x8000)))
    return struct.pack(">BiiHHhhi", INJECT_SCROLL, x, y, width, height, fixed(dx), fixed(dy), 0)


def clipboard_message(sequence: int, text: str, paste: bool) -> bytes:
    data = text.encode("utf-8")
    return struct.pack(">BqBI", SET_CLIPBOARD, sequence, int(paste), len(data)) + data


def start_app_message(package: str) -> bytes:
    data = package.encode("utf-8")
    if len(data) > 255:
        raise ValueError("package name too long")
    return struct.pack(">BB", START_APP, len(data)) + data


def reset_video_message() -> bytes:
    return bytes([RESET_VIDEO])


def resize_message(width: int, height: int) -> bytes:
    return struct.pack(">BHH", RESIZE_DISPLAY, width, height)


# Keys a phone or tablet keyboard sends that type nothing, as Android key codes (KeyboardEvent.key).
KEYS = {
    "Backspace": 67, "Delete": 112, "Enter": 66, "Tab": 61, "Escape": 111, "ArrowLeft": 21,
    "ArrowUp": 19, "ArrowRight": 22, "ArrowDown": 20, "Home": 122, "End": 123, "PageUp": 92,
    "PageDown": 93, "Insert": 124,
    **{f"F{n}": 130 + n for n in range(1, 13)},
}
# Letters and digits by key code, for shortcuts such as Ctrl+C (KeyboardEvent.code).
CODES = {**{f"Key{c}": 29 + i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")},
         **{f"Digit{d}": 7 + d for d in range(10)}}


# ---------------------------------------------------------------------- WebSocket (RFC 6455)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 1 << 20  # what a phone may send us in one message


class WebSocketError(Exception):
    pass


def ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key.strip() + WS_GUID).encode("ascii")).digest()).decode("ascii")


def ws_frame(opcode: int, payload: bytes = b"") -> bytes:
    """A server frame: final, unmasked."""
    n = len(payload)
    if n < 126:
        head = struct.pack(">BB", 0x80 | opcode, n)
    elif n < 1 << 16:
        head = struct.pack(">BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack(">BBQ", 0x80 | opcode, 127, n)
    return head + payload


def ws_header(first: bytes) -> tuple[bool, int, int, int]:
    """From a frame's first two bytes: (final, opcode, length code, extra header bytes to read).
    Frames from a browser must be masked."""
    b0, b1 = first[0], first[1]
    if b0 & 0x70:
        raise WebSocketError("reserved bits set")
    if not b1 & 0x80:
        raise WebSocketError("client frames must be masked")
    code = b1 & 0x7F
    extra = (2 if code == 126 else 8 if code == 127 else 0) + 4
    return bool(b0 & 0x80), b0 & 0x0F, code, extra


def ws_length(code: int, extra: bytes) -> tuple[int, bytes]:
    """The payload length and masking key from the extended header."""
    if code == 126:
        length, mask = struct.unpack(">H", extra[:2])[0], extra[2:6]
    elif code == 127:
        length, mask = struct.unpack(">Q", extra[:8])[0], extra[8:12]
    else:
        length, mask = code, extra[:4]
    if length > MAX_MESSAGE:
        raise WebSocketError("message too big")
    return length, mask


def ws_unmask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    if not n:
        return b""
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


def ws_client_frame(opcode: int, payload: bytes, mask: bytes = b"\x01\x02\x03\x04", final: bool = True) -> bytes:
    """A browser's (masked) frame, for tests and the self-check."""
    n = len(payload)
    b0 = (0x80 if final else 0) | opcode
    if n < 126:
        head = struct.pack(">BB", b0, 0x80 | n)
    elif n < 1 << 16:
        head = struct.pack(">BBH", b0, 0x80 | 126, n)
    else:
        head = struct.pack(">BBQ", b0, 0x80 | 127, n)
    return head + mask + ws_unmask(payload, mask)


# ---------------------------------------------------------------------- video to the phone
# Binary WebSocket messages: kind u8, the PC's clock when the packet arrived from Android (ms since the
# epoch, f64), then the data. Key frames carry the codec config (SPS/PPS) in front, so each one
# decodes on its own.
VIDEO_KEY, VIDEO_DELTA, VIDEO_JPEG = 1, 2, 3


def video_message(kind: int, sent_ms: float, data: bytes) -> bytes:
    return struct.pack(">Bd", kind, sent_ms) + data


# ---------------------------------------------------------------------- the phone's messages
class BadMessage(Exception):
    pass


MAX_POINTERS = 10


def _int(m: dict, key: str, lo: int, hi: int) -> int:
    v = m.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise BadMessage(f"{key}: a number is needed")
    v = int(v)
    if not lo <= v <= hi:
        raise BadMessage(f"{key}: out of range")
    return v


def _num(m: dict, key: str, lo: float, hi: float, default: float | None = None) -> float:
    v = m.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise BadMessage(f"{key}: a number is needed")
    return float(max(lo, min(hi, v)))


def _str(m: dict, key: str, limit: int, default: str | None = None) -> str:
    v = m.get(key, default)
    if not isinstance(v, str) or len(v) > limit:
        raise BadMessage(f"{key}: text of up to {limit} characters is needed")
    return v


TOUCH_ACTIONS = {"down": ACTION_DOWN, "up": ACTION_UP, "move": ACTION_MOVE, "cancel": ACTION_CANCEL}


def parse_message(text: str) -> dict:
    """A phone's message, checked and normalized. Raises BadMessage for anything malformed, so the rest
    of the code only ever sees what it expects."""
    try:
        m = json.loads(text)
    except ValueError as e:
        raise BadMessage("not JSON") from e
    if not isinstance(m, dict) or not isinstance(m.get("t"), str):
        raise BadMessage("no message type")
    t = m["t"]
    if t == "touch":
        w, h = _int(m, "w", 1, 8192), _int(m, "h", 1, 8192)
        points = m.get("p")
        if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTERS:
            raise BadMessage("p: a list of touches is needed")
        out = []
        for p in points:
            if not isinstance(p, list) or len(p) not in (4, 5) or p[0] not in TOUCH_ACTIONS:
                raise BadMessage("p: [action, id, x, y(, pressure)] each")
            pid, x, y = (_int({"v": v}, "v", -(1 << 20), 1 << 20) for v in p[1:4])
            if not 0 <= pid < 64:
                raise BadMessage("touch id out of range")
            pressure = _num({"v": p[4]}, "v", 0, 1) if len(p) == 5 else 1.0
            out.append((TOUCH_ACTIONS[p[0]], pid, min(max(x, 0), w - 1), min(max(y, 0), h - 1), pressure))
        return {"t": t, "w": w, "h": h, "points": out}
    if t == "scroll":
        w, h = _int(m, "w", 1, 8192), _int(m, "h", 1, 8192)
        return {"t": t, "w": w, "h": h, "x": _int(m, "x", 0, w - 1), "y": _int(m, "y", 0, h - 1),
                "dx": _num(m, "dx", -16, 16, 0), "dy": _num(m, "dy", -16, 16, 0)}
    if t == "key":
        key = _str(m, "key", 32)
        code = _str(m, "code", 32, "")
        meta = 0
        for flag, bit in (("shift", META_SHIFT), ("alt", META_ALT), ("ctrl", META_CTRL), ("meta", META_META)):
            if m.get(flag) is True:
                meta |= bit
        keycode = KEYS.get(key)
        if keycode is None and meta & (META_CTRL | META_META):
            keycode = CODES.get(code)
        if keycode is None:
            raise BadMessage("not a key we pass on")
        if meta & META_META:  # Cmd on an iPad keyboard does what Ctrl does on Android
            meta = (meta & ~META_META) | META_CTRL
        return {"t": t, "keycode": keycode, "meta": meta}
    if t == "text":
        text = _str(m, "s", 2000)
        text = "".join(c for c in text if c == "\n" or c >= " ")
        if not text:
            raise BadMessage("no text")
        return {"t": t, "s": text}
    if t == "nav":
        k = m.get("k")
        if k not in ("back", "home"):
            raise BadMessage("nav: back or home")
        return {"t": t, "k": k}
    if t == "view":
        return {"t": t, "w": _int(m, "w", 120, 4096), "h": _int(m, "h", 120, 4096),
                "dpr": _num(m, "dpr", 1, 4, 2)}
    if t == "open":
        out = {"t": t, "app": _str(m, "app", 64), "decoder": m.get("decoder")}
        if out["decoder"] not in ("webcodecs", "jpeg"):
            raise BadMessage("decoder: webcodecs or jpeg")
        if "view" in m:
            if not isinstance(m["view"], dict):
                raise BadMessage("view: an object")
            out["view"] = parse_message(json.dumps({**m["view"], "t": "view"}))
        return out
    if t == "quality":
        if m.get("q") not in ("smooth", "balanced", "sharp"):
            raise BadMessage("q: smooth, balanced or sharp")
        return {"t": t, "q": m["q"]}
    if t == "ping":
        return {"t": t, "c": _num(m, "c", 0, 1e15)}
    if t in ("apps", "ack", "keyframe", "back_to_pc", "upgrade", "close"):
        return {"t": t}
    raise BadMessage(f"unknown message type {t!r}")
