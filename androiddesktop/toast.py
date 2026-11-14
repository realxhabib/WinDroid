"""Windows notifications (toasts) with a reply box, for Android's notifications (notifications.py).

Windows.UI.Notifications called with plain ctypes, as hello.py calls Windows Hello: no extra packages.
A toast shows under WinDroid's own AppUserModelID, which its Start menu shortcut carries (Windows only
shows toasts for an app it can find there). Clicks and replies come back through the toast's Activated
event, which reaches this process for as long as it runs, from the popup and from the notification
center alike; once WinDroid quits, its toasts there do nothing.

Every WinRT call runs on one thread of our own, in the multithreaded apartment.
"""
from __future__ import annotations

import ctypes
import queue
import threading
import traceback
from typing import Callable

from .hello import GUID, RPC_E_CHANGED_MODE, _call, _hstring, _query, _release, combase, pinterface_iid
from .log import log

MANAGER = "Windows.UI.Notifications.ToastNotificationManager"
TOAST = "Windows.UI.Notifications.ToastNotification"
XML_DOCUMENT = "Windows.Data.Xml.Dom.XmlDocument"
IID_MANAGER_STATICS = "50ac103f-d235-4598-bbef-98fe4d1a3ad4"  # IToastNotificationManagerStatics
IID_MANAGER_STATICS2 = "7ab93c52-0e48-4750-ba9d-1a4113981847"  # IToastNotificationManagerStatics2
IID_TOAST_FACTORY = "04124b20-82c6-4229-b109-fd9ed4662b53"  # IToastNotificationFactory
IID_TOAST2 = "9dfb9fd1-143a-490e-90bf-b9fba7132de7"  # IToastNotification2 (Tag, Group)
IID_XML_DOCUMENT = "f7f3a506-1e87-42d6-bcfb-b8c809fa5494"  # IXmlDocument
IID_XML_IO = "6cd0e74e-ee65-4489-9ebf-ca43e87ba637"  # IXmlDocumentIO
IID_ACTIVATED_ARGS = "e3bf92f3-c197-436f-8265-0625824f8dac"  # IToastActivatedEventArgs
IID_ACTIVATED_ARGS2 = "ab7da512-cc61-568e-81be-304ac31038fa"  # IToastActivatedEventArgs2 (UserInput)
IID_PROPERTY_VALUE = "4bd682dd-7554-40e9-9a9b-82654ede7e62"  # IPropertyValue
IID_UNKNOWN = "00000000-0000-0000-c000-000000000046"
IID_AGILE = "94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90"  # IAgileObject: call us from any thread
IID_TOAST = "997e2675-059e-4e60-8b06-1760917c8b80"  # IToastNotification
# TypedEventHandler<ToastNotification, Object>, the Activated event's delegate
IID_ACTIVATED_HANDLER = pinterface_iid(
    f"pinterface({{9de1c534-6ae1-11e0-84e1-18a905bcc53f}};rc({TOAST};{{{IID_TOAST}}});cinterface(IInspectable))")
# IMap<String, Object>, which the ValueSet of a toast's inputs is
IID_STRING_MAP = pinterface_iid("pinterface({3c2925fe-8519-45c1-aa79-197b6718c1c1};string;cinterface(IInspectable))")
E_NOINTERFACE = -2147467262
GROUP = "android"

combase.RoActivateInstance.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
combase.WindowsGetStringRawBuffer.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
combase.WindowsGetStringRawBuffer.restype = ctypes.c_void_p


def _text(h: ctypes.c_void_p) -> str:
    """An HSTRING's text; frees it."""
    if not h:
        return ""
    try:
        size = ctypes.c_uint32()
        raw = combase.WindowsGetStringRawBuffer(h, ctypes.byref(size))
        return ctypes.wstring_at(raw, size.value) if raw else ""
    finally:
        combase.WindowsDeleteString(h)


def _factory(cls: str, iid: str) -> ctypes.c_void_p:
    name = _hstring(cls)
    try:
        out = ctypes.c_void_p()
        hr = combase.RoGetActivationFactory(name, ctypes.byref(GUID.of(iid)), ctypes.byref(out))
        if hr < 0:
            raise OSError(hr, f"RoGetActivationFactory {cls}")
        return out
    finally:
        combase.WindowsDeleteString(name)


def _activate(cls: str) -> ctypes.c_void_p:
    name = _hstring(cls)
    try:
        out = ctypes.c_void_p()
        hr = combase.RoActivateInstance(name, ctypes.byref(out))
        if hr < 0:
            raise OSError(hr, f"RoActivateInstance {cls}")
        return out
    finally:
        combase.WindowsDeleteString(name)


def _with_hstrings(obj: ctypes.c_void_p, index: int, *texts: str, out: ctypes.c_void_p | None = None) -> None:
    """Call a method that takes these strings (and maybe returns an object into out)."""
    handles = [_hstring(t) for t in texts]
    try:
        args = [(ctypes.c_void_p, h) for h in handles]
        if out is not None:
            args.append((ctypes.POINTER(ctypes.c_void_p), ctypes.byref(out)))
        _call(obj, index, *args)
    finally:
        for h in handles:
            combase.WindowsDeleteString(h)


# ---------------------------------------------------------------------- the Activated handler, by hand
_QI = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
_REF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_INVOKE = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


class _Vtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", _QI), ("AddRef", _REF), ("Release", _REF), ("Invoke", _INVOKE)]


class _Handler(ctypes.Structure):
    _fields_ = [("vtbl", ctypes.POINTER(_Vtbl))]


class Toaster:
    """Shows, replaces and removes toasts by tag. on_activated(tag, arguments, reply text) runs on a
    thread of its own when the user clicks one or sends a reply."""

    def __init__(self, app_id: str, on_activated: Callable[[str, str, str], None]) -> None:
        self.app_id = app_id
        self.on_activated = on_activated
        self._jobs: queue.Queue = queue.Queue()
        self._toasts: dict[str, ctypes.c_void_p] = {}  # tag -> IToastNotification, kept so its event fires
        self._notifier = ctypes.c_void_p()
        self._statics = ctypes.c_void_p()
        self._toast_factory = ctypes.c_void_p()
        self._ok = True
        # One handler for every toast, alive as long as we are: Windows may hold it after a toast goes.
        self._funcs = _Vtbl(_QI(self._qi), _REF(lambda this: 2), _REF(lambda this: 1), _INVOKE(self._invoke))
        self._handler = _Handler(ctypes.pointer(self._funcs))
        threading.Thread(target=self._run, daemon=True, name="toasts").start()

    # ------------------------------------------------------------------ public, from any thread
    def show(self, tag: str, xml: str) -> None:
        self._jobs.put(lambda: self._show(tag, xml))

    def remove(self, tag: str) -> None:
        self._jobs.put(lambda: self._remove(tag))

    def clear(self, wait: float = 0) -> None:
        """Take down every toast of ours. wait: seconds to wait until they're gone."""
        done = threading.Event()

        def job() -> None:
            try:
                self._clear()
            finally:
                done.set()
        self._jobs.put(job)
        if wait:
            done.wait(wait)

    # ------------------------------------------------------------------ the WinRT thread
    def _run(self) -> None:
        hr = combase.RoInitialize(1)
        if hr < 0 and hr != RPC_E_CHANGED_MODE:
            log(f"notifications: RoInitialize failed ({hr:#x})")
            return
        try:
            self._statics = _factory(MANAGER, IID_MANAGER_STATICS)
            self._toast_factory = _factory(TOAST, IID_TOAST_FACTORY)
            _with_hstrings(self._statics, 7, self.app_id, out=self._notifier)  # CreateToastNotifierWithId
            self._clear()  # left by a WinDroid that ended without taking them down: clicks would do nothing
        except OSError as e:
            log(f"notifications: Windows notifications unavailable: {e!r}")
            self._ok = False
        while True:
            job = self._jobs.get()
            if not self._ok:
                continue  # (a waiting clear() gives up after its wait)
            try:
                job()
            except Exception:
                log(f"notifications: toast:\n{traceback.format_exc().rstrip()}")

    def _show(self, tag: str, xml: str) -> None:
        doc = _activate(XML_DOCUMENT)
        io = document = toast = toast2 = ctypes.c_void_p()
        try:
            io = _query(doc, IID_XML_IO)
            _with_hstrings(io, 6, xml)  # LoadXml
            document = _query(doc, IID_XML_DOCUMENT)
            toast = ctypes.c_void_p()
            _call(self._toast_factory, 6, (ctypes.c_void_p, document), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(toast)))
            toast2 = _query(toast, IID_TOAST2)
            _with_hstrings(toast2, 6, tag)  # put_Tag: a toast with the same tag and group replaces this one
            _with_hstrings(toast2, 8, GROUP)  # put_Group
            token = ctypes.c_int64()
            _call(toast, 11, (ctypes.c_void_p, ctypes.addressof(self._handler)),
                  (ctypes.POINTER(ctypes.c_int64), ctypes.byref(token)))  # add_Activated
            _call(self._notifier, 6, (ctypes.c_void_p, toast))  # Show
            old = self._toasts.pop(tag, None)
            self._toasts[tag] = toast
            toast = ctypes.c_void_p()  # kept
            _release(old)
        finally:
            for obj in (toast2, toast, document, io, doc):
                _release(obj)

    def _history(self) -> ctypes.c_void_p:
        statics2 = _query(self._statics, IID_MANAGER_STATICS2)
        try:
            history = ctypes.c_void_p()
            _call(statics2, 6, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(history)))  # get_History
            return history
        finally:
            _release(statics2)

    def _remove(self, tag: str) -> None:
        _release(self._toasts.pop(tag, None))
        history = self._history()
        try:
            _with_hstrings(history, 8, tag, GROUP, self.app_id)  # RemoveGroupedTagWithId
        except OSError:
            pass  # already gone: the user dismissed it
        finally:
            _release(history)

    def _clear(self) -> None:
        for toast in self._toasts.values():
            _release(toast)
        self._toasts.clear()
        history = self._history()
        try:
            _with_hstrings(history, 7, GROUP, self.app_id)  # RemoveGroupWithId
        except OSError:
            pass
        finally:
            _release(history)

    # ------------------------------------------------------------------ the handler's methods
    def _qi(self, this: int, riid, out) -> int:
        wanted = bytes(riid.contents)
        if wanted in (bytes(GUID.of(IID_ACTIVATED_HANDLER)), bytes(GUID.of(IID_UNKNOWN)), bytes(GUID.of(IID_AGILE))):
            out[0] = this
            return 0
        out[0] = None
        return E_NOINTERFACE

    def _invoke(self, this: int, sender: int, args: int) -> int:
        """Windows' thread: a click or a reply. Read what we need, then let it go."""
        try:
            tag = arguments = reply = ""
            if sender:
                toast2 = _query(ctypes.c_void_p(sender), IID_TOAST2)
                try:
                    h = ctypes.c_void_p()
                    _call(toast2, 7, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(h)))  # get_Tag
                    tag = _text(h)
                finally:
                    _release(toast2)
            if args:
                arguments, reply = _activation(ctypes.c_void_p(args))
            threading.Thread(target=self.on_activated, args=(tag, arguments, reply), daemon=True).start()
        except Exception:
            log(f"notifications: activation:\n{traceback.format_exc().rstrip()}")
        return 0


def _activation(args: ctypes.c_void_p) -> tuple[str, str]:
    """A toast's activation: its arguments, and what was typed in its reply box ("" if none)."""
    first = _query(args, IID_ACTIVATED_ARGS)
    try:
        h = ctypes.c_void_p()
        _call(first, 6, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(h)))  # get_Arguments
        arguments = _text(h)
    finally:
        _release(first)
    reply = ""
    second = values = found = value = ctypes.c_void_p()
    try:
        second = _query(args, IID_ACTIVATED_ARGS2)
        values = ctypes.c_void_p()
        _call(second, 6, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(values)))  # get_UserInput
        string_map = _query(values, IID_STRING_MAP)
        try:
            found = ctypes.c_void_p()
            _with_hstrings(string_map, 6, "reply", out=found)  # Lookup
        finally:
            _release(string_map)
        if found:
            value = _query(found, IID_PROPERTY_VALUE)
            h = ctypes.c_void_p()
            _call(value, 19, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(h)))  # GetString
            reply = _text(h)
    except OSError:
        pass  # no reply box, or nothing in it
    finally:
        for obj in (value, found, values, second):
            _release(obj)
    return arguments, reply
