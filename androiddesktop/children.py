"""Child processes that end with ours, however ours ends: Quit, a crash, os._exit or Task Manager.

Windows doesn't end a process's children when it ends, so an agent that quit would leave its logcat
watcher running, and a killed one its app windows' scrcpy too. A job object does: this process holds
the only handle to one made with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. When the process ends, for any
reason, Windows closes the handle and ends everything in the job.

Only children started with popen() go in: long-lived ones that are no use without us. adb.start's (the
fingerprint and secure-screen watchers' logcat; InputHelper's, AgentHelper's and secure screens' adb
shells) and scrcpy. Not the emulator, which outlives a crashed agent so the next one can take it over.
And JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK keeps their own children out: the adb server an adb client
starts if none is running is every adb's on the PC.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import threading

from .log import log

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
kernel32.CreateJobObjectW.restype = wt.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
kernel32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
ntdll.NtResumeProcess.restype = ctypes.c_long  # NTSTATUS
ntdll.NtResumeProcess.argtypes = [wt.HANDLE]

CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROCESS_TERMINATE, PROCESS_SET_QUOTA, PROCESS_SUSPEND_RESUME = 0x0001, 0x0100, 0x0800


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", ctypes.c_uint64 * 6),  # IO_COUNTERS
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


_lock = threading.Lock()
_job: int | None = None  # made on first use; 0 if Windows wouldn't


def _the_job() -> int:
    global _job
    with _lock:
        if _job is None:
            _job = _make_job()
        return _job


def _make_job() -> int:
    """Never closed: the handle lives as long as this process, and its closing is the point."""
    job = kernel32.CreateJobObjectW(None, None)
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
    if job and kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                                ctypes.byref(info), ctypes.sizeof(info)):
        return job
    log(f"children: no job object ({ctypes.WinError(ctypes.get_last_error())}): children may outlive us")
    if job:
        kernel32.CloseHandle(job)
    return 0


def popen(args: list[str], **kwargs) -> subprocess.Popen:
    """subprocess.Popen for a child that must not outlive this process. It starts suspended and runs only
    once it's in the job, so there's no moment it could be left behind."""
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_SUSPENDED
    proc = subprocess.Popen(args, **kwargs)
    handle = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_SET_QUOTA | PROCESS_SUSPEND_RESUME, False, proc.pid)
    try:
        job = _the_job()
        if job and not kernel32.AssignProcessToJobObject(job, handle):
            log(f"children: {args[0]} isn't in the job ({ctypes.WinError(ctypes.get_last_error())}): "
                "it may outlive us")
        resumed = ntdll.NtResumeProcess(handle) >= 0
    finally:
        kernel32.CloseHandle(handle)
    if not resumed:  # it would sit there suspended forever
        proc.kill()
        raise OSError(f"couldn't start {args[0]}")
    return proc
