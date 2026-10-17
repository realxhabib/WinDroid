"""The Windows Firewall rule that lets iPhones and iPads on the home network reach WinDroid.

One inbound rule, for this program and our port only, from the local subnet only, on Private networks
only. Adding it takes administrator rights, so it's only ever added when the user clicks the button
that says so (Windows then asks). Without it Windows would show its own "allow access?" prompt the
first time we listen, which offers to open the program to every port.
"""
from __future__ import annotations

import subprocess

import win32api

from .. import adb

RULE = "WinDroid (iPhone and iPad)"


def program() -> str:
    """The executable that listens: the process image itself (in a venv that's the base pythonw.exe,
    not the venv's launcher), which is what Windows Firewall matches."""
    return win32api.GetModuleFileName(None)


def rule_text() -> str:
    try:
        return subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={RULE}", "verbose"],
                              capture_output=True, text=True, timeout=15, creationflags=adb.CREATE_NO_WINDOW,
                              errors="replace").stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse_allows(text: str, exe: str, port: int) -> bool:
    """Whether `netsh ... show rule` lists an enabled inbound allow rule for exe and port."""
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            name, sep, value = line.partition(":")
            if sep:
                fields[name.strip().lower()] = value.strip()
        if (fields.get("enabled", "").lower() == "yes" and fields.get("direction", "").lower() == "in"
                and fields.get("action", "").lower() == "allow" and fields.get("protocol", "") in ("TCP", "Any")
                and fields.get("program", "").lower() == exe.lower()
                and fields.get("localport", "") in (str(port), "Any")):
            return True
    return False


def allowed(port: int) -> bool:
    return parse_allows(rule_text(), program(), port)


def add_rule_args(exe: str, port: int) -> list[str]:
    return ["advfirewall", "firewall", "add", "rule", f"name={RULE}", "dir=in", "action=allow",
            f"program={exe}", "protocol=TCP", f"localport={port}", "remoteip=localsubnet", "profile=private",
            "enable=yes", "description=Lets your iPhone or iPad on your home Wi-Fi use Android apps from WinDroid."]


def allow(port: int) -> bool:
    """Add the rule (replacing an old one), through Windows' administrator prompt. False if the user said no."""
    from win32com.shell import shell, shellcon
    import win32con
    import win32event
    import win32process
    exe = program()
    delete = subprocess.list2cmdline(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={RULE}"])
    add = subprocess.list2cmdline(["netsh", *add_rule_args(exe, port)])
    try:
        info = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, lpVerb="runas", lpFile="cmd.exe",
                                    lpParameters=f"/d /c {delete} >nul 2>&1 & {add}", nShow=win32con.SW_HIDE)
    except Exception:  # "No" in the administrator prompt
        return False
    handle = info["hProcess"]
    win32event.WaitForSingleObject(handle, 60_000)
    code = win32process.GetExitCodeProcess(handle)
    win32api.CloseHandle(handle)
    return code == 0 and allowed(port)
