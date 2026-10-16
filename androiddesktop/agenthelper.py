"""Talks to helper/src/AgentHelper.java: one long-lived process inside Android, one JSON line each way.

The helper jar is the same as catalog.py's (assets/appcatalog.jar), pushed to a path of its own so an
agent running another version of Android Desktop, which pushes its own jar to the catalog's path, can't
swap it out under a running helper.
"""
from __future__ import annotations

import hashlib
import json
import queue
import subprocess
import threading
from collections import deque

from . import adb
from .catalog import HELPER
from .paths import ADB_EXE, SERIAL, android_env

DEVICE_JAR = "/data/local/tmp/androiddesktop-agent.jar"
VERSION = 2  # AgentHelper.VERSION


class HelperError(RuntimeError):
    pass


class Helper:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._replies: queue.Queue = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=20)
        self._lock = threading.Lock()
        self._id = 0
        self.on_restart = lambda: None  # a fresh helper has none of the old one's displays

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def call(self, cmd: str, timeout: float = 20, **args) -> dict:
        """Send one command and wait for its answer. Raises HelperError with the helper's own message."""
        with self._lock:
            if not self.running():
                self._start()
            return self._send(cmd, timeout, args)

    def _send(self, cmd: str, timeout: float, args: dict) -> dict:
        self._id += 1
        try:
            self._proc.stdin.write((json.dumps({"id": self._id, "cmd": cmd, **args}) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except OSError as e:
            raise HelperError(f"the Android helper stopped ({e})") from e
        while True:
            try:
                reply = self._replies.get(timeout=timeout)
            except queue.Empty:
                # Left running: a late answer is told apart by its id. A hung app can hold up a read.
                raise HelperError(f"{cmd}: Android didn't answer within {timeout:g} s") from None
            if reply is None:
                detail = " / ".join(self._stderr) or f"exit code {self._proc.poll()}"
                raise HelperError(f"the Android helper stopped: {detail}")
            if reply.get("id") == self._id:
                break
        if not reply.get("ok"):
            raise HelperError(reply.get("error") or f"{cmd} failed")
        return reply

    def _start(self) -> None:
        if not adb.online():
            raise HelperError("Android isn't running")
        self._push()
        self._replies = queue.Queue()
        self._proc = subprocess.Popen(
            [str(ADB_EXE), "-s", SERIAL, "shell", f"CLASSPATH={DEVICE_JAR} app_process / AgentHelper"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=android_env(),
            creationflags=adb.CREATE_NO_WINDOW)
        threading.Thread(target=self._read, args=(self._proc, self._replies), daemon=True,
                         name="agent-helper").start()
        threading.Thread(target=self._read_errors, args=(self._proc,), daemon=True).start()
        try:
            hello = self._send("hello", 30, {})
            if hello.get("version") != VERSION:
                raise HelperError(f"the Android helper is version {hello.get('version')}, expected {VERSION}")
        except HelperError:
            self._proc.kill()  # the next call tries afresh
            raise
        self.on_restart()

    @staticmethod
    def _push() -> None:
        local = hashlib.md5(HELPER.read_bytes()).hexdigest()
        if local not in adb.shell(f"md5sum {DEVICE_JAR} 2>/dev/null", timeout=15):
            adb.run("push", str(HELPER), DEVICE_JAR, timeout=30, check=True)

    @staticmethod
    def _read(proc: subprocess.Popen, replies: queue.Queue) -> None:
        for line in proc.stdout:
            try:
                replies.put(json.loads(line))
            except ValueError:
                pass  # not ours: app_process can print warnings
        replies.put(None)

    def _read_errors(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            self._stderr.append(line.decode("utf-8", "replace").strip())

    def close(self) -> None:
        """Ending the helper releases its displays, and the apps on them close."""
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.stdin.close()
                    self._proc.wait(5)
                except (OSError, subprocess.TimeoutExpired):
                    self._proc.kill()
            self._proc = None
