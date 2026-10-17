"""Puts RGBA frames on a window with Direct3D 11, through plain ctypes COM calls.

The emulator hands frames over as RGBA (shared memory, see emuview.py), which is Direct3D's own
R8G8B8A8 format, so a frame goes from memory into the swap chain's back buffer in one copy
(UpdateSubresource) and is shown with Present: no conversion, no shaders, no scaling. GDI would need
the bytes swapped to BGRA first, which costs more than the whole Direct3D path.

The back buffer always has the window's size. A frame of another size (the window was just resized
and Android hasn't caught up yet) is drawn 1:1 from the top left, the rest filled with the window's
background, so text never goes blurry.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

UINT = ctypes.c_uint
HRESULT = ctypes.HRESULT


class DXGI_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", UINT), ("Denominator", UINT)]


class DXGI_MODE_DESC(ctypes.Structure):
    _fields_ = [("Width", UINT), ("Height", UINT), ("RefreshRate", DXGI_RATIONAL), ("Format", UINT),
                ("ScanlineOrdering", UINT), ("Scaling", UINT)]


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", UINT), ("Quality", UINT)]


class DXGI_SWAP_CHAIN_DESC(ctypes.Structure):
    _fields_ = [("BufferDesc", DXGI_MODE_DESC), ("SampleDesc", DXGI_SAMPLE_DESC), ("BufferUsage", UINT),
                ("BufferCount", UINT), ("OutputWindow", wt.HWND), ("Windowed", wt.BOOL), ("SwapEffect", UINT),
                ("Flags", UINT)]


class D3D11_BOX(ctypes.Structure):
    _fields_ = [("left", UINT), ("top", UINT), ("front", UINT), ("right", UINT), ("bottom", UINT), ("back", UINT)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


IID_ID3D11Texture2D = GUID(0x6F15AAF2, 0xD208, 0x4E89, (ctypes.c_ubyte * 8)(0x9A, 0xB4, 0x48, 0x95, 0x35, 0xD3, 0x4F, 0x9C))
DXGI_FORMAT_R8G8B8A8_UNORM = 28
DXGI_USAGE_RENDER_TARGET_OUTPUT = 0x20
DXGI_SWAP_EFFECT_DISCARD = 0
D3D_DRIVER_TYPE_HARDWARE = 1
D3D11_SDK_VERSION = 7
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20

# vtable slots
RELEASE = 2
SC_PRESENT, SC_GET_BUFFER, SC_RESIZE_BUFFERS = 8, 9, 13
DEV_CREATE_RTV = 9
CTX_UPDATE_SUBRESOURCE, CTX_CLEAR_RTV = 48, 50

_d3d11 = None


def _method(obj: ctypes.c_void_p, index: int, *argtypes):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[RELEASE])(obj)


class Renderer:
    """A swap chain on one window. Thread-safe: frames may come from any thread."""

    def __init__(self, hwnd: int, width: int, height: int) -> None:
        global _d3d11
        if _d3d11 is None:
            _d3d11 = ctypes.WinDLL("d3d11")
        self.hwnd = hwnd
        self.size = (max(width, 1), max(height, 1))
        self.background = (0.0, 0.0, 0.0, 1.0)
        self._lock = threading.Lock()
        self._pending: tuple[int, int] | None = None
        self.swapchain, self.device, self.context = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        self._rtv = ctypes.c_void_p()
        desc = DXGI_SWAP_CHAIN_DESC()
        desc.BufferDesc.Width, desc.BufferDesc.Height = self.size
        desc.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM
        desc.SampleDesc.Count = 1
        desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT
        desc.BufferCount = 1
        desc.OutputWindow = hwnd
        desc.Windowed = True
        desc.SwapEffect = DXGI_SWAP_EFFECT_DISCARD
        level = UINT()
        _d3d11.D3D11CreateDeviceAndSwapChain.restype = HRESULT
        _d3d11.D3D11CreateDeviceAndSwapChain(
            None, D3D_DRIVER_TYPE_HARDWARE, None, D3D11_CREATE_DEVICE_BGRA_SUPPORT, None, 0, D3D11_SDK_VERSION,
            ctypes.byref(desc), ctypes.byref(self.swapchain), ctypes.byref(self.device), ctypes.byref(level),
            ctypes.byref(self.context))
        self._present = _method(self.swapchain, SC_PRESENT, UINT, UINT)
        self._get_buffer = _method(self.swapchain, SC_GET_BUFFER, UINT, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
        self._resize_buffers = _method(self.swapchain, SC_RESIZE_BUFFERS, UINT, UINT, UINT, UINT, UINT)
        self._create_rtv = _method(self.device, DEV_CREATE_RTV, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_void_p))
        vtable = ctypes.cast(self.context, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        self._update = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, UINT, ctypes.POINTER(D3D11_BOX),
                                          ctypes.c_void_p, UINT, UINT)(vtable[CTX_UPDATE_SUBRESOURCE])
        self._clear = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_float * 4))(vtable[CTX_CLEAR_RTV])
        self._backbuffer = ctypes.c_void_p()
        self._acquire()

    def _acquire(self) -> None:
        self._get_buffer(self.swapchain, 0, ctypes.byref(IID_ID3D11Texture2D), ctypes.byref(self._backbuffer))
        self._create_rtv(self.device, self._backbuffer, None, ctypes.byref(self._rtv))

    def _drop_buffers(self) -> None:
        _release(self._rtv)
        _release(self._backbuffer)
        self._rtv, self._backbuffer = ctypes.c_void_p(), ctypes.c_void_p()

    def resize(self, width: int, height: int) -> None:
        """The window's new size; takes effect with the next frame."""
        self._pending = (max(width, 1), max(height, 1))

    def draw(self, pixels: int, width: int, height: int, pitch: int | None = None) -> None:
        """Show a width x height RGBA frame at address `pixels` (rows top first, `pitch` bytes apart)."""
        with self._lock:
            if self.swapchain is None:
                return
            if self._pending and self._pending != self.size:
                self._drop_buffers()
                self.size = self._pending
                self._resize_buffers(self.swapchain, 1, self.size[0], self.size[1], DXGI_FORMAT_R8G8B8A8_UNORM, 0)
                self._acquire()
            self._pending = None
            bw, bh = self.size
            w, h = min(width, bw), min(height, bh)
            if w < bw or h < bh or not pixels:
                self._clear(self.context, self._rtv, ctypes.byref((ctypes.c_float * 4)(*self.background)))
            if pixels and w > 0 and h > 0:
                box = D3D11_BOX(0, 0, 0, w, h, 1)
                self._update(self.context, self._backbuffer, 0, ctypes.byref(box), pixels, pitch or width * 4, 0)
            self._present(self.swapchain, 0, 0)

    def close(self) -> None:
        with self._lock:
            if self.swapchain is None:
                return
            self._drop_buffers()
            for obj in (self.context, self.swapchain, self.device):
                _release(obj)
            self.swapchain = None
