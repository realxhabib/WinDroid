"""Pairing iPhones and iPads, and checking every connection from them.

Nothing works until a device is paired. Pairing makes a secret that only the PC and that device know,
and that never crosses the network, even over plain HTTP:

1. The device makes an X25519 key pair and sends its public key (POST /api/pair/start); the PC
   answers with its own. Both work out the same shared key, which someone listening on the Wi-Fi can't.
2. The device proves it holds the one-time token from the QR code on the PC's screen: an HMAC with the
   token over both public keys (POST /api/pair/finish). The token sits in the URL's #fragment, which a
   browser never sends, and a man in the middle who swapped the keys can't make that proof.
   Without a token (a new Home Screen icon gets storage of its own, so it starts out unpaired), the
   user approves the device in the launcher instead, after checking that both show the same 6-digit
   code. The code comes from the shared key and both public keys, so a swapped key shows up as a
   different code.
3. The device secret is an HMAC of the shared key; each side works it out for itself.

Every WebSocket then starts with a challenge-response on that secret, in both directions, and every
message from the device carries an HMAC with a key for that connection and a sequence number, so
nothing can be replayed or slipped in.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

OFFER_SECS = 300  # a QR code's token
UPGRADE_SECS = 120  # the token a paired page hands to its https:// twin
PENDING_SECS = 300  # a key exchange waiting for its proof or approval
APPROVAL_SECS = 120  # how long the launcher asks
MAX_OFFERS, MAX_PENDING, MAX_APPROVALS = 4, 16, 3
STARTS_PER_MINUTE = 12  # pairing attempts per IP address
DEFAULT_NAME = "iPhone or iPad"


class PairingError(Exception):
    """Something to show on the device, as is."""


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text: object, size: int | None = None) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,200}", text):
        raise PairingError("Malformed request.")
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if size is not None and len(data) != size:
        raise PairingError("Malformed request.")
    return data


def mac(key: bytes, *parts: str) -> bytes:
    """HMAC-SHA256 over the parts joined by "|". The web app computes the same (wdcrypto.js)."""
    return hmac.new(key, "|".join(parts).encode("utf-8"), hashlib.sha256).digest()


def transcript(pid: str, device_pub: bytes, server_pub: bytes) -> str:
    return f"{pid}|{b64e(device_pub)}|{b64e(server_pub)}"


def pair_proof(token: bytes, pid: str, device_pub: bytes, server_pub: bytes) -> bytes:
    return mac(token, "windroid-pair-v1", transcript(pid, device_pub, server_pub))


def sas_code(shared: bytes, pid: str, device_pub: bytes, server_pub: bytes) -> str:
    n = int.from_bytes(mac(shared, "windroid-sas-v1", transcript(pid, device_pub, server_pub))[:4], "big")
    return f"{n % 1_000_000:06d}"


def device_secret(shared: bytes, device_id: str) -> bytes:
    return mac(shared, "windroid-device-v1", device_id)


def auth_proof(secret: bytes, nonce: str, cnonce: str) -> bytes:
    return mac(secret, "windroid-auth-v1", nonce, cnonce)


def server_proof(secret: bytes, nonce: str, cnonce: str) -> bytes:
    return mac(secret, "windroid-server-v1", nonce, cnonce)


def session_key(secret: bytes, nonce: str, cnonce: str) -> bytes:
    return mac(secret, "windroid-session-v1", nonce, cnonce)


def message_mac(key: bytes, seq: int, body: str) -> str:
    return b64e(mac(key, "windroid-msg-v1", str(seq), body)[:16])


def seal(key: bytes, seq: int, body: str) -> str:
    """A message as the device sends it: "<seq>.<mac>.<json>"."""
    return f"{seq}.{message_mac(key, seq, body)}.{body}"


class MessageVerifier:
    """Checks the device's messages on one connection: right key, sequence numbers going up."""

    def __init__(self, key: bytes) -> None:
        self.key = key
        self.seq = 0

    def open(self, frame: str) -> str:
        m = re.match(r"(\d{1,15})\.([A-Za-z0-9_-]{22})\.", frame)
        if not m:
            raise PairingError("unsigned message")
        seq, body = int(m[1]), frame[m.end():]
        if seq <= self.seq:
            raise PairingError("replayed message")
        if not hmac.compare_digest(m[2], message_mac(self.key, seq, body)):
            raise PairingError("bad message signature")
        self.seq = seq
        return body


def clean_name(name: object) -> str:
    text = "".join(c for c in re.sub(r"\s+", " ", str(name or "")) if c.isprintable()).strip()
    return text[:40] or DEFAULT_NAME


@dataclass
class Device:
    id: str
    name: str
    group: str  # one phone can hold several keys: Safari over http, over https, each Home Screen icon
    secret: bytes = field(repr=False)
    created: float
    via: str = "qr"  # qr, approved, upgrade
    last_seen: float = 0.0
    view: dict | None = None  # the last screen size it reported: {"w", "h", "dpr"} in CSS pixels


@dataclass
class Offer:
    token: bytes = field(repr=False)
    expires: float
    kind: str = "qr"
    group: str | None = None


@dataclass
class Pending:
    id: str
    device_pub: bytes
    server_pub: bytes
    shared: bytes = field(repr=False)
    name: str
    ip: str
    expires: float
    state: str = "new"  # new -> approve -> approved / denied; or straight to done with a token
    code: str = ""
    device: str = ""


class Pairing:
    """The paired devices (kept in a file, secrets DPAPI-encrypted) and the pairings in progress."""

    def __init__(self, path: Path, protect: Callable[[bytes], bytes] | None = None,
                 unprotect: Callable[[bytes], bytes] | None = None, clock: Callable[[], float] = time.time) -> None:
        if protect is None or unprotect is None:
            from . import vault
            protect, unprotect = vault.protect, vault.unprotect
        self.path = path
        self._protect, self._unprotect, self._clock = protect, unprotect, clock
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._key = b""
        self._offers: list[Offer] = []
        self._pending: dict[str, Pending] = {}
        self._starts: dict[str, deque] = {}
        self.listeners: list[Callable[[], None]] = []
        self._load()

    # ------------------------------------------------------------------ storage
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        try:
            self._key = self._unprotect(base64.b64decode(data["key"]))
        except Exception:
            return  # another Windows user's file, or damaged: start afresh
        for d in data.get("devices", []):
            try:
                secret = self._unprotect(base64.b64decode(d["secret"]))
                self._devices[d["id"]] = Device(d["id"], d["name"], d["group"], secret, d["created"],
                                                d.get("via", "qr"), d.get("last_seen", 0.0), d.get("view"))
            except Exception:
                continue

    def _save(self) -> None:
        devices = [{"id": d.id, "name": d.name, "group": d.group, "created": d.created, "via": d.via,
                    "last_seen": d.last_seen, "view": d.view,
                    "secret": base64.b64encode(self._protect(d.secret)).decode("ascii")}
                   for d in self._devices.values()]
        data = {"version": 1, "key": base64.b64encode(self._protect(self.key)).decode("ascii"), "devices": devices}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def key(self) -> bytes:
        """This PC's own secret, for app links (see Hub.cap)."""
        with self._lock:
            if not self._key:
                self._key = secrets.token_bytes(32)
                self._save()
            return self._key

    def _changed(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    def _expire(self) -> None:
        now = self._clock()
        self._offers = [o for o in self._offers if o.expires > now]
        gone = [p for p in self._pending.values() if p.expires <= now]
        for p in gone:
            del self._pending[p.id]
        if any(p.state == "approve" for p in gone):
            self._changed()

    # ------------------------------------------------------------------ pairing
    def offer(self, kind: str = "qr", group: str | None = None) -> str:
        """A one-time token: in a QR code (kind qr), or for an already paired page's https twin (upgrade)."""
        with self._lock:
            self._expire()
            token = secrets.token_bytes(16)
            ttl = OFFER_SECS if kind == "qr" else UPGRADE_SECS
            self._offers.append(Offer(token, self._clock() + ttl, kind, group))
            self._offers = self._offers[-MAX_OFFERS:]
            return b64e(token)

    def withdraw(self, token: str) -> None:
        """The QR code was taken off the screen."""
        with self._lock:
            raw = b64d(token)
            self._offers = [o for o in self._offers if not hmac.compare_digest(o.token, raw)]

    def start(self, device_pub: str, name: object, ip: str) -> dict:
        with self._lock:
            self._expire()
            now = self._clock()
            starts = self._starts.setdefault(ip, deque())
            while starts and starts[0] < now - 60:
                starts.popleft()
            if len(starts) >= STARTS_PER_MINUTE or len(self._pending) >= MAX_PENDING:
                raise PairingError("Too many pairing attempts. Wait a minute and try again.")
            starts.append(now)
            dpub = b64d(device_pub, 32)
            priv = X25519PrivateKey.generate()
            spub = priv.public_key().public_bytes_raw()
            try:
                shared = priv.exchange(X25519PublicKey.from_public_bytes(dpub))
            except ValueError as e:  # an all-zero shared key: a bad public key
                raise PairingError("Malformed request.") from e
            pid = b64e(secrets.token_bytes(9))
            self._pending[pid] = Pending(pid, dpub, spub, shared, clean_name(name), ip, now + PENDING_SECS)
            return {"id": pid, "pub": b64e(spub)}

    def finish(self, pid: object, proof: object = None) -> dict:
        """With a proof: pair now, if it matches a live token. Without: ask the user in the launcher."""
        with self._lock:
            self._expire()
            p = self._pending.get(pid) if isinstance(pid, str) else None
            if p is None or p.state != "new":
                raise PairingError("This pairing attempt has expired. Start again.")
            if proof is None:
                if sum(x.state == "approve" for x in self._pending.values()) >= MAX_APPROVALS:
                    raise PairingError("Other devices are waiting to be allowed. Try again in a minute.")
                p.state, p.code = "approve", sas_code(p.shared, p.id, p.device_pub, p.server_pub)
                p.expires = self._clock() + APPROVAL_SECS
                self._changed()
                return {"pending": True}
            given = b64d(proof, 32)
            for offer in self._offers:
                if hmac.compare_digest(given, pair_proof(offer.token, p.id, p.device_pub, p.server_pub)):
                    self._offers.remove(offer)
                    del self._pending[p.id]
                    group = offer.group if offer.kind == "upgrade" and offer.group else None
                    device = self._add(p, "upgrade" if group else "qr", group)
                    return {"device": device.id, "name": device.name}
            del self._pending[p.id]  # one try per key exchange
            raise PairingError("That QR code has expired or was already used. Show a new one in WinDroid on your PC.")

    def _add(self, p: Pending, via: str, group: str | None) -> Device:
        device_id = b64e(secrets.token_bytes(9))
        name = p.name
        if group:
            name = next((d.name for d in self._devices.values() if d.group == group), name)
        device = Device(device_id, name, group or b64e(secrets.token_bytes(6)),
                        device_secret(p.shared, device_id), self._clock(), via, self._clock())
        self._devices[device_id] = device
        self._save()
        self._changed()
        return device

    def status(self, pid: object) -> dict:
        with self._lock:
            self._expire()
            p = self._pending.get(pid) if isinstance(pid, str) else None
            if p is None:
                return {"state": "expired"}
            out = {"state": p.state}
            if p.state == "approved":
                out["device"] = p.device
                out["name"] = self._devices[p.device].name if p.device in self._devices else p.name
            return out

    def approvals(self) -> list[dict]:
        with self._lock:
            self._expire()
            return [{"id": p.id, "name": p.name, "code": p.code, "ip": p.ip, "expires": p.expires}
                    for p in self._pending.values() if p.state == "approve"]

    def approve(self, pid: str, allow: bool) -> bool:
        with self._lock:
            self._expire()
            p = self._pending.get(pid)
            if p is None or p.state != "approve":
                return False
            if allow:
                p.device = self._add(p, "approved", None).id
                p.state = "approved"
            else:
                p.state = "denied"
            p.expires = self._clock() + 60  # long enough for the device to hear the answer
            self._changed()
            return True

    # ------------------------------------------------------------------ devices
    def get(self, device_id: object) -> Device | None:
        with self._lock:
            return self._devices.get(device_id) if isinstance(device_id, str) else None

    def devices(self) -> list[Device]:
        with self._lock:
            return sorted(self._devices.values(), key=lambda d: d.created)

    def groups(self) -> list[dict]:
        """Paired devices as the user knows them: one entry per phone or tablet."""
        out: dict[str, dict] = {}
        for d in self.devices():
            g = out.setdefault(d.group, {"group": d.group, "name": d.name, "created": d.created, "last_seen": 0.0,
                                         "keys": 0})
            g["keys"] += 1
            g["last_seen"] = max(g["last_seen"], d.last_seen)
        return list(out.values())

    def remove(self, group: str) -> bool:
        with self._lock:
            gone = [d.id for d in self._devices.values() if d.group == group]
            for device_id in gone:
                del self._devices[device_id]
            if gone:
                self._save()
                self._changed()
            return bool(gone)

    def authenticate(self, device_id: object, nonce: str, cnonce: object, proof: object) -> Device | None:
        """The device's answer to our challenge; None if it isn't a paired device or got it wrong."""
        device = self.get(device_id)
        if device is None or not isinstance(cnonce, str) or not 16 <= len(cnonce) <= 64:
            return None
        try:
            given = b64d(proof, 32)
        except PairingError:
            return None
        if not hmac.compare_digest(given, auth_proof(device.secret, nonce, cnonce)):
            return None
        return device

    def seen(self, device_id: str, view: dict | None = None) -> None:
        with self._lock:
            d = self._devices.get(device_id)
            if d is None:
                return
            d.last_seen = self._clock()
            if view:
                d.view = {"w": view["w"], "h": view["h"], "dpr": view["dpr"]}
            self._save()


def nonce() -> str:
    return b64e(os.urandom(18))
