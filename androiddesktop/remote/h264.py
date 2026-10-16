"""H.264 decoding on the PC, for phones that show JPEG frames instead of decoding the video themselves.

Uses the FFmpeg that ships with scrcpy (avcodec/avutil DLLs in SCRCPY_HOME) through ctypes, so nothing
new is installed. Only the long-stable public API and the first fields of AVPacket and AVFrame are
touched (data/size; data/linesize/width/height/format), which FFmpeg has kept in place for years.
"""
from __future__ import annotations

import ctypes
import io
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..paths import SCRCPY_HOME

AV_CODEC_ID_H264 = 27
AVERROR_EAGAIN = -11  # AVERROR(EAGAIN) on Windows
YUV420P, YUVJ420P = 0, 12
# Offsets into AVPacket (buf, pts, dts, then data and size) and AVFrame (data[8], linesize[8],
# extended_data, width, height, nb_samples, format).
PKT_DATA, PKT_SIZE = 24, 32
FRAME_LINESIZE, FRAME_WIDTH, FRAME_HEIGHT, FRAME_FORMAT = 64, 104, 108, 116

_lib: tuple[ctypes.CDLL, ctypes.CDLL] | None = None
_lib_lock = threading.Lock()


class DecoderError(Exception):
    pass


def _dll(folder: Path, prefix: str) -> Path:
    found = sorted(folder.glob(f"{prefix}-*.dll"))
    if not found:
        raise DecoderError(f"{prefix} isn't in {folder}")
    return found[-1]


def _load() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    global _lib
    with _lib_lock:
        if _lib is None:
            os.add_dll_directory(str(SCRCPY_HOME))
            avutil = ctypes.CDLL(str(_dll(SCRCPY_HOME, "avutil")))
            avcodec = ctypes.CDLL(str(_dll(SCRCPY_HOME, "avcodec")))
            vp, i, cp = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
            for name, res, args in (("avcodec_find_decoder", vp, [i]), ("avcodec_alloc_context3", vp, [vp]),
                                    ("avcodec_open2", i, [vp, vp, vp]), ("avcodec_send_packet", i, [vp, vp]),
                                    ("avcodec_receive_frame", i, [vp, vp]), ("avcodec_free_context", None, [vp]),
                                    ("av_packet_alloc", vp, []), ("av_packet_free", None, [vp])):
                fn = getattr(avcodec, name)
                fn.restype, fn.argtypes = res, args
            for name, res, args in (("av_frame_alloc", vp, []), ("av_frame_free", None, [vp]),
                                    ("av_frame_unref", None, [vp]), ("av_opt_set", i, [vp, cp, cp, i]),
                                    ("av_opt_set_int", i, [vp, cp, ctypes.c_int64, i])):
                fn = getattr(avutil, name)
                fn.restype, fn.argtypes = res, args
            _lib = avcodec, avutil
        return _lib


@dataclass
class Frame:
    width: int
    height: int
    planes: tuple[bytes, bytes, bytes]  # Y, Cb, Cr
    strides: tuple[int, int, int]

    def image(self) -> Image.Image:
        w, h = self.width, self.height
        cw, ch = (w + 1) // 2, (h + 1) // 2
        y = Image.frombuffer("L", (w, h), self.planes[0], "raw", "L", self.strides[0], 1)
        u = Image.frombuffer("L", (cw, ch), self.planes[1], "raw", "L", self.strides[1], 1)
        v = Image.frombuffer("L", (cw, ch), self.planes[2], "raw", "L", self.strides[2], 1)
        return Image.merge("YCbCr", (y, u.resize((w, h), Image.BILINEAR), v.resize((w, h), Image.BILINEAR)))

    def jpeg(self, quality: int = 70) -> bytes:
        buf = io.BytesIO()
        self.image().save(buf, "JPEG", quality=quality, subsampling="4:2:0")
        return buf.getvalue()


class Decoder:
    """Annex-B H.264 in (config packets included), YUV 4:2:0 frames out, one frame per packet."""

    def __init__(self) -> None:
        self._avcodec, self._avutil = _load()
        codec = self._avcodec.avcodec_find_decoder(AV_CODEC_ID_H264)
        if not codec:
            raise DecoderError("this FFmpeg has no H.264 decoder")
        self._ctx = ctypes.c_void_p(self._avcodec.avcodec_alloc_context3(codec))
        # A frame out for every packet in: no frame reordering, no frame threads holding frames back.
        self._avutil.av_opt_set(self._ctx, b"flags", b"+low_delay", 1)
        self._avutil.av_opt_set_int(self._ctx, b"threads", 2, 1)
        self._avutil.av_opt_set(self._ctx, b"thread_type", b"slice", 1)
        if self._avcodec.avcodec_open2(self._ctx, codec, None) < 0:
            raise DecoderError("couldn't open the H.264 decoder")
        self._pkt = ctypes.c_void_p(self._avcodec.av_packet_alloc())
        self._frame = ctypes.c_void_p(self._avutil.av_frame_alloc())

    def decode(self, data: bytes) -> list[Frame]:
        if not self._ctx:
            raise DecoderError("decoder closed")
        buf = ctypes.create_string_buffer(data, len(data))
        base = self._pkt.value
        ctypes.c_void_p.from_address(base + PKT_DATA).value = ctypes.addressof(buf)
        ctypes.c_int.from_address(base + PKT_SIZE).value = len(data)
        try:
            # FFmpeg copies the data: the packet has no buffer of its own (buf is NULL).
            if self._avcodec.avcodec_send_packet(self._ctx, self._pkt) < 0:
                return []  # a broken packet: the next key frame repairs the picture
        finally:
            ctypes.c_void_p.from_address(base + PKT_DATA).value = None
            ctypes.c_int.from_address(base + PKT_SIZE).value = 0
        frames = []
        while self._avcodec.avcodec_receive_frame(self._ctx, self._frame) == 0:
            try:
                frames.append(self._copy())
            finally:
                self._avutil.av_frame_unref(self._frame)
        return frames

    def _copy(self) -> Frame:
        base = self._frame.value
        fmt = ctypes.c_int.from_address(base + FRAME_FORMAT).value
        if fmt not in (YUV420P, YUVJ420P):
            raise DecoderError(f"unexpected pixel format {fmt}")
        w = ctypes.c_int.from_address(base + FRAME_WIDTH).value
        h = ctypes.c_int.from_address(base + FRAME_HEIGHT).value
        data = (ctypes.c_void_p * 3).from_address(base)
        lines = (ctypes.c_int * 3).from_address(base + FRAME_LINESIZE)
        ch = (h + 1) // 2
        planes = (ctypes.string_at(data[0], lines[0] * h), ctypes.string_at(data[1], lines[1] * ch),
                  ctypes.string_at(data[2], lines[2] * ch))
        return Frame(w, h, planes, (lines[0], lines[1], lines[2]))

    def close(self) -> None:
        if self._ctx:
            self._avutil.av_frame_free(ctypes.byref(self._frame))
            self._avcodec.av_packet_free(ctypes.byref(self._pkt))
            self._avcodec.avcodec_free_context(ctypes.byref(self._ctx))
            self._ctx = ctypes.c_void_p()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
