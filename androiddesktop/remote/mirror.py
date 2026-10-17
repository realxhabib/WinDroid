"""One scrcpy server inside Android, streaming one display to us and taking input for it.

The same server the app windows use (scrcpy-server 5.0.1), started by us directly with `app_process`,
without scrcpy's own window: its H.264 comes over an adb tunnel and goes on to a phone, and the
phone's touches go back on its control socket. It either makes a display of its own for an app (a
"flex" display, which RESIZE_DISPLAY resizes when the phone rotates) or mirrors an existing display,
the app window's, when the phone shows the same app as the PC.

The server injects input only on the display it streams, so nothing the phone sends can reach any
other app or display.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time
from typing import Callable

from .. import adb
from ..log import log
from ..paths import ADB_EXE, LOGS, SCRCPY_HOME, SERIAL, android_env
from ..runtime import SCRCPY_VERSION
from . import protocol

SERVER = SCRCPY_HOME / "scrcpy-server"
DEVICE_DIR = "/data/local/tmp"


class MirrorError(Exception):
    pass


_job = None
_job_lock = threading.Lock()


def _tie_to_us(proc: subprocess.Popen) -> None:
    """Put the server's adb process in a job that closes with our process, so it never outlives us (it
    would keep the phone's display, and the app on it, alive with nobody streaming it)."""
    global _job
    try:
        import win32api
        import win32con
        import win32job
        with _job_lock:
            if _job is None:
                job = win32job.CreateJobObject(None, "")
                info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
                info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
                _job = job
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid)
        try:
            win32job.AssignProcessToJobObject(_job, handle)
        finally:
            win32api.CloseHandle(handle)
    except Exception as e:  # best effort: stop() still ends it normally
        log(f"remote: couldn't tie the stream process to ours: {e!r}")


def server_args(scid: str, jar: str, *, new_display: tuple[int, int, int] | None = None,
                display: int | None = None, bit_rate: int = 8_000_000, max_fps: int = 60) -> list[str]:
    """The server's command line inside Android (adb shell ...)."""
    args = [f"CLASSPATH={jar}", "app_process", "/", "com.genymobile.scrcpy.Server", SCRCPY_VERSION,
            f"scid={scid}", "log_level=info", "tunnel_forward=true", "audio=false", "control=true",
            "video_codec=h264", f"video_bit_rate={bit_rate}", f"max_fps={max_fps}",
            "send_device_meta=false", "clipboard_autosync=false", "power_on=false", "cleanup=true"]
    if new_display:
        w, h, dpi = new_display
        # Content stays if the server goes away (an adb hiccup): Android moves the app to its main
        # display, and the next server's START_APP brings it back.
        args += [f"new_display={w}x{h}/{dpi}", "flex_display=true", "vd_system_decorations=false",
                 "vd_destroy_content=false", "keep_active=true", "display_ime_policy=local"]
    elif display is not None:
        args.append(f"display_id={display}")
    else:
        raise ValueError("a new display or an existing one")
    return args


def parse_display_announcement(line: str) -> int | None:
    m = re.search(r"New display: .*\(id=(\d+)\)", line)
    return int(m.group(1)) if m else None


class Mirror:
    def __init__(self, label: str, *, new_display: tuple[int, int, int] | None = None, display: int | None = None,
                 on_session: Callable[[protocol.Session], None] = lambda s: None,
                 on_packet: Callable[[protocol.PacketHeader, bytes], None] = lambda h, d: None,
                 on_exit: Callable[[str], None] = lambda why: None) -> None:
        self.label = label
        self.new_display = new_display
        self.display = display  # set from the server's announcement for a new display
        self.on_session, self.on_packet, self.on_exit = on_session, on_packet, on_exit
        self.scid = "%08x" % (int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF)
        self.jar = f"{DEVICE_DIR}/androiddesktop-remote-{self.scid}.jar"  # the server deletes it on exit
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self._video: socket.socket | None = None
        self._control: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._stopping = False
        self._exited = False
        self._error = ""

    # ------------------------------------------------------------------ lifecycle
    def start(self, timeout: float = 20) -> None:
        LOGS.mkdir(parents=True, exist_ok=True)
        pushed = adb.run("push", str(SERVER), self.jar, timeout=30)
        if pushed.returncode != 0:
            raise MirrorError(f"couldn't push the scrcpy server: {(pushed.stderr or pushed.stdout).strip()[-200:]}")
        args = server_args(self.scid, self.jar, new_display=self.new_display,
                           display=None if self.new_display else self.display)
        self.proc = subprocess.Popen([str(ADB_EXE), "-s", SERIAL, "shell", *args], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=android_env(),
                                     creationflags=adb.CREATE_NO_WINDOW, text=True, encoding="utf-8",
                                     errors="replace")
        _tie_to_us(self.proc)
        threading.Thread(target=self._read_output, daemon=True, name=f"mirror-out-{self.label}").start()
        try:
            out = adb.run("forward", "tcp:0", f"localabstract:scrcpy_{self.scid}", timeout=10).stdout.strip()
            if not out.isdigit():
                raise MirrorError(f"adb forward failed: {out[-200:]}")
            self.port = int(out)
            end = time.monotonic() + timeout
            self._video = self._connect(end, dummy=True)
            self._control = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            self._control.settimeout(None)
            self._control.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            codec = self._read(self._video, 4)
            if codec != protocol.CODEC_H264:
                raise MirrorError(f"the server sent codec {codec!r}")
            while self.new_display and self.display is None:
                if time.monotonic() > end or self.proc.poll() is not None:
                    raise MirrorError(self._error or "the server made no display")
                time.sleep(0.02)
        except Exception:
            self.stop()
            raise
        threading.Thread(target=self._read_video, daemon=True, name=f"mirror-video-{self.label}").start()
        threading.Thread(target=self._drain_control, daemon=True, name=f"mirror-ctl-{self.label}").start()
        log(f"remote: {self.label}: streaming display {self.display} (scid {self.scid})")

    def _connect(self, end: float, dummy: bool) -> socket.socket:
        """The tunnel accepts before the server listens; the server's first byte says it's really there."""
        while True:
            if self.proc and self.proc.poll() is not None:
                raise MirrorError(self._error or "the scrcpy server exited")
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.settimeout(0.5)
            try:
                if not dummy or s.recv(1):
                    s.settimeout(None)
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    return s
            except (socket.timeout, OSError):
                pass
            s.close()
            if time.monotonic() > end:
                raise MirrorError("the scrcpy server didn't answer")
            time.sleep(0.1)

    def stop(self) -> None:
        self._stopping = True
        for s in (self._video, self._control):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        if self.port:
            try:
                adb.run("forward", "--remove", f"tcp:{self.port}", timeout=5)
            except Exception:
                pass
            self.port = 0

    def dpi_matches(self, dpi: int) -> bool:
        return self.new_display is None or self.new_display[2] == dpi

    @property
    def alive(self) -> bool:
        return not self._exited and self.proc is not None and self.proc.poll() is None

    def _ended(self, why: str) -> None:
        if self._exited:
            return
        self._exited = True
        if not self._stopping:  # not asked to: tell the owner
            log(f"remote: {self.label}: stream ended: {why}")
            self.stop()
            self.on_exit(why)

    # ------------------------------------------------------------------ reading
    @staticmethod
    def _read(s: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise EOFError("connection closed")
            buf += chunk
        return bytes(buf)

    def _read_output(self) -> None:
        path = LOGS / "remote.log"
        try:
            with path.open("a", encoding="utf-8") as out:
                out.write(f"--- {time.strftime('%H:%M:%S')} {self.label} (scid {self.scid})\n")
                for line in self.proc.stdout:
                    out.write(line)
                    out.flush()
                    display = parse_display_announcement(line)
                    if display is not None:
                        self.display = display
                    elif "ERROR" in line or "Exception" in line:
                        self._error = self._error or line.split("ERROR:", 1)[-1].strip()
        except Exception:
            pass
        self._ended(self._error or "the scrcpy server exited")

    def _read_video(self) -> None:
        try:
            while True:
                header = protocol.parse_header(self._read(self._video, 12))
                if isinstance(header, protocol.Session):
                    self.on_session(header)
                    continue
                self.on_packet(header, self._read(self._video, header.size))
        except Exception as e:
            self._ended(f"video: {e!r}")

    def _drain_control(self) -> None:
        """Device messages (clipboard, acks) we don't use; read them so the server never blocks."""
        try:
            while self._control.recv(65536):
                pass
        except OSError:
            pass

    # ------------------------------------------------------------------ input
    def send(self, message: bytes) -> bool:
        if not self._control or self._exited:
            return False
        try:
            with self._send_lock:
                self._control.sendall(message)
            return True
        except OSError as e:
            self._ended(f"control: {e!r}")
            return False

    def start_app(self, package: str) -> bool:
        return self.send(protocol.start_app_message(package))

    def reset_video(self) -> bool:
        return self.send(protocol.reset_video_message())

    def resize(self, width: int, height: int) -> bool:
        return self.send(protocol.resize_message(width, height))

    def tap_keys(self, keycode: int, meta: int = 0) -> bool:
        return (self.send(protocol.key_message(0, keycode, meta)) and
                self.send(protocol.key_message(1, keycode, meta)))
