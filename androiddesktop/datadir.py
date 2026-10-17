"""Keep the data folder where every program on the PC sees it.

Apps installed as packages (from the Microsoft Store, or MSIX), the Claude desktop app among them, get
file system virtualization: when one of them, or anything it starts, creates a new file or folder
directly in %LOCALAPPDATA%, Windows writes it into that app's private copy
(%LOCALAPPDATA%\\Packages\\<app>\\LocalCache\\Local) instead, and only that app's own processes see it.
Inside a folder that already exists for real, their writes go to the real folder.

So if WinDroid's very first run happens inside such an app (Claude Desktop starting the MCP
server, say), %LOCALAPPDATA%\\AndroidDesktop only exists in that app's private copy. Started from the
Start menu, WinDroid then finds an empty folder and offers setup again, and the taskbar can't
read the app icons in it.

ensure() runs before anything is written: it makes sure the real folder exists, creating it with a
helper started through Explorer (which runs outside any package) when this process is redirected. A
private copy left by an earlier run is moved into the real folder the same way.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .paths import DATA

HELPER_COMMAND = "_datadir"  # `python -m androiddesktop _datadir <create|move> <result file>`


def local_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def private_copies(root: Path | None = None) -> list[Path]:
    """Copies of the data folder in packaged apps' private storage."""
    root = root or local_root()
    try:
        return sorted(p for p in (root / "Packages").glob(f"*/LocalCache/Local/{DATA.name}") if p.is_dir())
    except OSError:
        return []


_redirected: bool | None = None


def redirected() -> bool:
    """Whether this process's new files in %LOCALAPPDATA% land in a packaged app's private copy."""
    global _redirected
    if _redirected is None:
        name = f".androiddesktop-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        probe = local_root() / name
        try:
            probe.write_bytes(b"")
            _redirected = any((root / name).exists() for root in
                              (local_root() / "Packages").glob("*/LocalCache/Local"))
        except OSError:
            _redirected = False
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
    return _redirected


def merge_into(source: Path, target: Path) -> list[str]:
    """Move everything in source into target. Same-drive moves are renames. Where both have a folder
    the contents are merged; where both have a file, target's is kept and source's stays put.
    Returns what couldn't be moved (in use, or present in both)."""
    left: list[str] = []
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        dest = target / item.name
        try:
            if not dest.exists():
                shutil.move(str(item), str(dest))
            elif item.is_dir() and dest.is_dir():
                left += [f"{item.name}/{name}" for name in merge_into(item, dest)]
            else:
                left.append(item.name)
        except OSError as e:
            left.append(f"{item.name} ({e.strerror or e})")
    if not left:
        try:
            source.rmdir()
        except OSError:
            pass
    return left


def tidy(target: Path = DATA, root: Path | None = None) -> str | None:
    """In a process that isn't redirected: create the real folder and move private copies into it.
    Returns a problem to report, if any."""
    target.mkdir(parents=True, exist_ok=True)
    problems = []
    for copy in private_copies(root):
        left = merge_into(copy, target)
        if left:
            problems.append(f"{copy}: couldn't move {', '.join(left[:5])}"
                            f"{' and more' if len(left) > 5 else ''}")
    return "; ".join(problems) or None


def _outside(command: str, timeout: float = 120) -> str | None:
    """Run `androiddesktop _datadir <command>` through Explorer, outside any package, and wait for it.
    The shortcut and the result live in %TEMP%, which exists for real, so both sides see them."""
    import pythoncom
    from win32com.shell import shell

    from .winapi import pythonw
    work = Path(tempfile.gettempdir())
    tag = uuid.uuid4().hex[:8]
    result, lnk = work / f"androiddesktop-datadir-{tag}.txt", work / f"androiddesktop-datadir-{tag}.lnk"
    pythoncom.CoInitialize()
    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                      shell.IID_IShellLink)
    link.SetPath(pythonw())
    link.SetArguments(f'-m androiddesktop {HELPER_COMMAND} {command} "{result}"')
    link.SetWorkingDirectory(str(Path.home()))
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(lnk), 0)
    try:
        subprocess.run(["explorer.exe", str(lnk)], timeout=30)  # Explorer's exit code means nothing
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if result.exists():
                text = result.read_text(encoding="utf-8").strip()
                return text if text != "ok" else None
            time.sleep(0.2)
        return "the helper started through Explorer didn't answer"
    finally:
        for f in (lnk, result):
            try:
                f.unlink()
            except OSError:
                pass


def ensure() -> str | None:
    """Make sure the data folder exists for real and holds everything. Returns a problem, if any."""
    if DATA.parent != local_root():
        return None  # ANDROIDDESKTOP_DATA put it elsewhere, e.g. a test install's own folder
    if not redirected():
        if DATA.exists() and not private_copies():
            return None
        return tidy()
    if not private_copies() and DATA.exists():
        return None  # it exists for real: our writes inside it go to the real folder too
    return _outside("tidy")


def helper(args: list[str]) -> None:
    """`androiddesktop _datadir tidy <result file>`, started through Explorer by _outside()."""
    result = Path(args[1]) if len(args) > 1 else None
    try:
        problem = tidy() if args and args[0] == "tidy" else f"unknown datadir command {args!r}"
    except Exception as e:  # report it, whatever it is: the caller waits for an answer
        problem = repr(e)
    if result:
        result.write_text(problem or "ok", encoding="utf-8")
    elif problem:
        print(problem, file=sys.stderr)
