"""Windows Hello, and the optional remembered Android PIN.

verify() asks Windows Hello (face, fingerprint or the Windows PIN) whether the person at the PC is
the signed-in user: Windows.Security.Credentials.UI.UserConsentVerifier, through its desktop interop
(IUserConsentVerifierInterop.RequestVerificationForWindowAsync), so the prompt opens above the window
it's about rather than somewhere on its own. It's WinRT called with plain ctypes: no extra packages.

The PIN store keeps Android's screen-lock PIN encrypted with DPAPI for the current Windows user, for
unlocking Android with Windows Hello after a restart. Off unless the user turns it on. The PIN is
never logged.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import threading
import time
import uuid
from typing import Callable

import win32crypt

from .paths import DATA

# UserConsentVerifierAvailability / UserConsentVerificationResult
AVAILABLE, DEVICE_NOT_PRESENT, NOT_CONFIGURED, DISABLED_BY_POLICY, DEVICE_BUSY = range(5)
VERIFIED, RETRIES_EXHAUSTED, CANCELED = 0, 5, 6
RESULTS = {0: "verified", 1: "no Windows Hello device", 2: "Windows Hello isn't set up", 3: "disabled by policy",
           4: "device busy", 5: "too many tries", 6: "canceled"}

CLASS = "Windows.Security.Credentials.UI.UserConsentVerifier"
IID_INTEROP = "39e050c3-4e74-441a-8dc0-b81104df949c"  # IUserConsentVerifierInterop
IID_STATICS = "af4f3f91-564c-4ddc-b8b5-973447627c65"  # IUserConsentVerifierStatics
IID_ASYNC_INFO = "00000036-0000-0000-c000-000000000046"  # IAsyncInfo
IID_ASYNC_OPERATION = "9fc2b0bb-e446-44e2-aa61-9cab8f636af2"  # IAsyncOperation<T>
STARTED, COMPLETED = 0, 1  # AsyncStatus
RPC_E_CHANGED_MODE = -2147417850

combase = ctypes.windll.combase
combase.WindowsCreateString.argtypes = [wt.LPCWSTR, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
combase.RoGetActivationFactory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
combase.RoInitialize.argtypes = [ctypes.c_int]

_busy = threading.Lock()  # one Hello prompt at a time


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, value: str | uuid.UUID) -> GUID:
        return cls.from_buffer_copy(uuid.UUID(str(value)).bytes_le)


def pinterface_iid(signature: str) -> uuid.UUID:
    """The IID of a generic WinRT interface instance, e.g. IAsyncOperation<SomeEnum>: a version 5
    UUID of its type signature, as the Windows Runtime defines it."""
    namespace = uuid.UUID("11f47ad5-7b73-42c0-abae-878b1e16adee")
    digest = bytearray(hashlib.sha1(namespace.bytes + signature.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(digest))


# IAsyncOperation<UserConsentVerificationResult>
IID_VERIFY_OPERATION = pinterface_iid(
    f"pinterface({{{IID_ASYNC_OPERATION}}};enum(Windows.Security.Credentials.UI.UserConsentVerificationResult;i4))")


# ---------------------------------------------------------------------- a little COM by hand
def _call(obj: ctypes.c_void_p, index: int, *args: tuple[type, object]) -> None:
    """Call method `index` of a COM interface; raises OSError on a failed HRESULT."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    method = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *(t for t, _ in args))(vtable[index])
    method(obj, *(v for _, v in args))


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(obj)


def _query(obj: ctypes.c_void_p, iid: str) -> ctypes.c_void_p:
    out = ctypes.c_void_p()
    _call(obj, 0, (ctypes.c_void_p, ctypes.byref(GUID.of(iid))), (ctypes.c_void_p, ctypes.byref(out)))
    return out


def _hstring(text: str) -> ctypes.c_void_p:
    h = ctypes.c_void_p()
    hr = combase.WindowsCreateString(text, len(text), ctypes.byref(h))
    if hr < 0:
        raise OSError(hr, "WindowsCreateString")
    return h


def _factory(iid: str) -> ctypes.c_void_p:
    hr = combase.RoInitialize(1)  # multithreaded; fine if this thread already chose
    if hr < 0 and hr != RPC_E_CHANGED_MODE:
        raise OSError(hr, "RoInitialize")
    name = _hstring(CLASS)
    try:
        out = ctypes.c_void_p()
        hr = combase.RoGetActivationFactory(name, ctypes.byref(GUID.of(iid)), ctypes.byref(out))
        if hr < 0:
            raise OSError(hr, "RoGetActivationFactory")
        return out
    finally:
        combase.WindowsDeleteString(name)


def _result(operation: ctypes.c_void_p, timeout: float, wanted: Callable[[], bool] | None = None) -> int | None:
    """Wait for an IAsyncOperation<enum> and return its value; None if it didn't complete in time or
    stopped being wanted (it's canceled then), or ended without a result."""
    info = _query(operation, IID_ASYNC_INFO)
    try:
        status = ctypes.c_int(STARTED)
        end = next_check = time.monotonic() + timeout
        if wanted:
            next_check = time.monotonic() + 1
        while True:
            _call(info, 7, (ctypes.POINTER(ctypes.c_int), ctypes.byref(status)))  # get_Status
            if status.value != STARTED:
                break
            now = time.monotonic()
            if now > end or (wanted and now >= next_check and not wanted()):
                _call(info, 9)  # Cancel
                return None
            if now >= next_check:
                next_check = now + 1
            time.sleep(0.05)
        if status.value != COMPLETED:
            return None
        value = ctypes.c_int()
        _call(operation, 8, (ctypes.POINTER(ctypes.c_int), ctypes.byref(value)))  # GetResults
        return value.value
    finally:
        _release(info)


# ---------------------------------------------------------------------- Windows Hello
def availability() -> int:
    """AVAILABLE if Windows Hello is set up for this user, else why not."""
    statics = operation = ctypes.c_void_p()
    try:
        statics = _factory(IID_STATICS)
        operation = ctypes.c_void_p()
        _call(statics, 6, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(operation)))  # CheckAvailabilityAsync
        result = _result(operation, 15)
        return DEVICE_BUSY if result is None else result
    except OSError:
        return DEVICE_NOT_PRESENT
    finally:
        _release(operation)
        _release(statics)


def available() -> bool:
    return availability() == AVAILABLE


def verify(hwnd: int, message: str, timeout: float = 120, wanted: Callable[[], bool] | None = None) -> int:
    """Ask Windows Hello whether the signed-in user is here, with the prompt above window hwnd.
    Returns VERIFIED, CANCELED or another UserConsentVerificationResult. Blocks; one at a time.
    wanted: asked about once a second; the prompt goes away (CANCELED) once it says False."""
    with _busy:
        interop = operation = ctypes.c_void_p()
        text = _hstring(message[:256])
        try:
            interop = _factory(IID_INTEROP)
            operation = ctypes.c_void_p()
            _call(interop, 6, (wt.HWND, hwnd), (ctypes.c_void_p, text),  # RequestVerificationForWindowAsync
                  (ctypes.c_void_p, ctypes.byref(GUID.of(IID_VERIFY_OPERATION))),
                  (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(operation)))
            result = _result(operation, timeout, wanted)
            return CANCELED if result is None else result
        finally:
            _release(operation)
            _release(interop)
            combase.WindowsDeleteString(text)


# ---------------------------------------------------------------------- the remembered PIN
PIN_FILE = DATA / "android-pin.bin"
_ENTROPY = b"AndroidDesktop: Android screen-lock PIN"


def remember_pin(pin: str) -> None:
    """Keep the PIN, encrypted with DPAPI for this Windows user (readable only when signed in as them)."""
    blob = win32crypt.CryptProtectData(pin.encode("utf-8"), "Android Desktop", _ENTROPY, None, None, 0x1)
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = PIN_FILE.with_suffix(".tmp")
    tmp.write_bytes(blob)
    tmp.replace(PIN_FILE)


def remembered_pin() -> str | None:
    try:
        blob = PIN_FILE.read_bytes()
    except OSError:
        return None
    try:
        _, data = win32crypt.CryptUnprotectData(blob, _ENTROPY, None, None, 0x1)  # CRYPTPROTECT_UI_FORBIDDEN
        return data.decode("utf-8")
    except Exception:  # another user's, or damaged
        return None


def has_pin() -> bool:
    return PIN_FILE.exists()


def forget_pin() -> None:
    PIN_FILE.unlink(missing_ok=True)
