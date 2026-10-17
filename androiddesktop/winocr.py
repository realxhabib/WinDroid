"""Windows' built-in OCR (Windows.Media.Ocr), offline, called with plain ctypes like hello.py.

recognize() takes a PIL image and returns its lines of text with their boxes in the image's pixels.
A phone-sized screenshot takes 0.1 to 0.3 s. Small text reads much better enlarged first, so `scale`
enlarges the image for the engine (boxes still come back in the original's pixels).
"""
from __future__ import annotations

import ctypes
import threading
import time
from dataclasses import dataclass, field

from PIL import Image

from .hello import COMPLETED, GUID, IID_ASYNC_INFO, RPC_E_CHANGED_MODE, STARTED, _call, _hstring, _query, _release, combase

IID_OCR_STATICS = "5bffa85a-3384-3540-9940-699120d428a8"  # IOcrEngineStatics
IID_BITMAP_STATICS = "df0385db-672f-4a9d-806e-c2442f343e86"  # ISoftwareBitmapStatics
IID_BUFFER_FACTORY = "71af914d-c10f-484b-bc50-14bc623b3a27"  # IBufferFactory
IID_BUFFER = "905a0fe0-bc53-11df-8c49-001e4fc686da"  # IBuffer
IID_BUFFER_BYTES = "905a0fef-bc53-11df-8c49-001e4fc686da"  # IBufferByteAccess
BGRA8 = 87  # BitmapPixelFormat.Bgra8
MAX_SIDE = 2600  # OcrEngine.MaxImageDimension on Windows 10 and 11

combase.WindowsGetStringRawBuffer.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
combase.WindowsGetStringRawBuffer.restype = ctypes.c_void_p

_lock = threading.Lock()
_engine = ctypes.c_void_p()
_bitmaps = ctypes.c_void_p()
_buffers = ctypes.c_void_p()


class _Rect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float), ("w", ctypes.c_float), ("h", ctypes.c_float)]


@dataclass
class Word:
    text: str
    x: float
    y: float
    w: float
    h: float


@dataclass
class Line:
    text: str
    x: float
    y: float
    w: float
    h: float
    words: list[Word] = field(default_factory=list)

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def cx(self) -> float:
        return self.x + self.w / 2


def _string(h: ctypes.c_void_p) -> str:
    if not h:
        return ""
    n = ctypes.c_uint32()
    raw = combase.WindowsGetStringRawBuffer(h, ctypes.byref(n))
    text = ctypes.wstring_at(raw, n.value) if raw else ""
    combase.WindowsDeleteString(h)
    return text


def _factory(class_name: str, iid: str) -> ctypes.c_void_p:
    name = _hstring(class_name)
    try:
        out = ctypes.c_void_p()
        hr = combase.RoGetActivationFactory(name, ctypes.byref(GUID.of(iid)), ctypes.byref(out))
        if hr < 0:
            raise OSError(hr, f"RoGetActivationFactory({class_name})")
        return out
    finally:
        combase.WindowsDeleteString(name)


def _init() -> None:
    """Once per process: the engine for the user's languages and the factories recognize() needs."""
    global _engine, _bitmaps, _buffers
    if _engine:
        return
    statics = _factory("Windows.Media.Ocr.OcrEngine", IID_OCR_STATICS)
    try:
        engine = ctypes.c_void_p()
        _call(statics, 10, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(engine)))  # TryCreateFromUserProfileLanguages
        if not engine:
            raise RuntimeError("Windows OCR isn't available: no OCR language is installed")
    finally:
        _release(statics)
    _bitmaps = _factory("Windows.Graphics.Imaging.SoftwareBitmap", IID_BITMAP_STATICS)
    _buffers = _factory("Windows.Storage.Streams.Buffer", IID_BUFFER_FACTORY)
    _engine = engine


def _bitmap(image: Image.Image) -> ctypes.c_void_p:
    """A SoftwareBitmap (BGRA8) holding a copy of the image."""
    data = image.convert("RGBA").tobytes("raw", "BGRA")
    buffer = bytes_access = ctypes.c_void_p()
    try:
        buffer = ctypes.c_void_p()
        _call(_buffers, 6, (ctypes.c_uint32, len(data)), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(buffer)))
        ibuffer = _query(buffer, IID_BUFFER)
        _release(buffer)
        buffer = ibuffer
        bytes_access = _query(buffer, IID_BUFFER_BYTES)
        target = ctypes.c_void_p()
        _call(bytes_access, 3, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(target)))  # IBufferByteAccess::Buffer
        ctypes.memmove(target, data, len(data))
        _call(buffer, 8, (ctypes.c_uint32, len(data)))  # put_Length
        bitmap = ctypes.c_void_p()
        _call(_bitmaps, 9, (ctypes.c_void_p, buffer), (ctypes.c_int, BGRA8), (ctypes.c_int32, image.width),
              (ctypes.c_int32, image.height), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(bitmap)))
        return bitmap
    finally:
        _release(bytes_access)
        _release(buffer)


def _wait(operation: ctypes.c_void_p, timeout: float) -> ctypes.c_void_p:
    """Wait for an IAsyncOperation<OcrResult> and return its result."""
    info = _query(operation, IID_ASYNC_INFO)
    try:
        status = ctypes.c_int(STARTED)
        end = time.monotonic() + timeout
        while True:
            _call(info, 7, (ctypes.POINTER(ctypes.c_int), ctypes.byref(status)))  # get_Status
            if status.value != STARTED:
                break
            if time.monotonic() > end:
                _call(info, 9)  # Cancel
                raise TimeoutError("OCR took too long")
            time.sleep(0.005)
        if status.value != COMPLETED:
            raise OSError(f"OCR failed (status {status.value})")
        result = ctypes.c_void_p()
        _call(operation, 8, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(result)))  # GetResults
        return result
    finally:
        _release(info)


def _items(view: ctypes.c_void_p) -> list[ctypes.c_void_p]:
    """The elements of an IVectorView<T> (each one needs releasing)."""
    n = ctypes.c_uint32()
    _call(view, 7, (ctypes.POINTER(ctypes.c_uint32), ctypes.byref(n)))  # get_Size
    out = []
    for i in range(n.value):
        item = ctypes.c_void_p()
        _call(view, 6, (ctypes.c_uint32, i), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(item)))  # GetAt
        out.append(item)
    return out


def _text_of(obj: ctypes.c_void_p, index: int) -> str:
    h = ctypes.c_void_p()
    _call(obj, index, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(h)))
    return _string(h)


def _read(work: Image.Image, k: float, timeout: float) -> list[Line]:
    """One recognition (the engine takes them one at a time)."""
    bitmap = operation = result = lines_view = ctypes.c_void_p()
    lines: list[Line] = []
    try:
        bitmap = _bitmap(work)
        operation = ctypes.c_void_p()
        _call(_engine, 6, (ctypes.c_void_p, bitmap), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(operation)))
        result = _wait(operation, timeout)
        lines_view = ctypes.c_void_p()
        _call(result, 6, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(lines_view)))  # get_Lines
        for line in _items(lines_view):
            words_view = ctypes.c_void_p()
            try:
                _call(line, 6, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(words_view)))  # get_Words
                words = []
                for word in _items(words_view):
                    try:
                        r = _Rect()
                        _call(word, 6, (ctypes.POINTER(_Rect), ctypes.byref(r)))  # get_BoundingRect
                        words.append(Word(_text_of(word, 7), r.x / k, r.y / k, r.w / k, r.h / k))
                    finally:
                        _release(word)
                if words:
                    x0, y0 = min(w.x for w in words), min(w.y for w in words)
                    x1, y1 = max(w.x + w.w for w in words), max(w.y + w.h for w in words)
                    lines.append(Line(_text_of(line, 7).strip(), x0, y0, x1 - x0, y1 - y0, words))
            finally:
                _release(words_view)
                _release(line)
    finally:
        for obj in (lines_view, result, operation, bitmap):
            _release(obj)
    return lines


def recognize(image: Image.Image, scale: float = 1.0, timeout: float = 10) -> list[Line]:
    """The lines of text in an image, top to bottom, with boxes in the image's own pixels."""
    k = min(scale, MAX_SIDE / max(image.size))
    work = image.resize((max(1, round(image.width * k)), max(1, round(image.height * k))), Image.LANCZOS) \
        if abs(k - 1) > 0.01 else image
    hr = combase.RoInitialize(1)  # multithreaded; fine if this thread already chose
    if hr < 0 and hr != RPC_E_CHANGED_MODE:
        raise OSError(hr, "RoInitialize")
    with _lock:  # one at a time: a second RecognizeAsync while one runs fails
        _init()
        lines = _read(work, k, timeout)
    lines.sort(key=lambda ln: (round(ln.y / 6), ln.x))
    return lines


def available() -> bool:
    try:
        recognize(Image.new("RGB", (40, 20), "white"))
        return True
    except Exception:
        return False

