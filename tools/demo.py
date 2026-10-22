"""Demo mode, for filming WinDroid: window choreography that plays the same way take after take, and a
recording of what an AI agent does in a hidden app.

    python tools/demo.py android [--quit]       start the demo Android (below), setting it up the first time
    python tools/demo.py check                  ready to film? (WinDroid running, Android unlocked, apps)
    python tools/demo.py launcher               open WinDroid's launcher (for Ctrl+K)
    python tools/demo.py fleet [package...]     open apps one by one, each gliding into a row of windows
    python tools/demo.py snap [package package] glide two windows side by side; the apps re-lay themselves out
    python tools/demo.py agent [--minutes 20]   record the hidden apps an AI agent works in (agentrec.py)
    python tools/demo.py close                  close the windows the demo opened

--clean (with any of them) uses the demo Android: a WinDroid of its own, apart from yours, whose Android
has no Google account, so nothing of yours (your picture, places, history) can get into the video. It
shares your Android runtime, and keeps its own Android in %LOCALAPPDATA%\\AndroidDesktop\\demo.

Before filming: turn on Do Not Disturb in Windows, hide the desktop icons (right-click the desktop >
View), and open each app once to get past its welcome screen. The windows move by themselves: keep the
mouse still while a scene plays.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CLEAN = "--clean" in sys.argv or sys.argv[1:2] == ["android"]
if CLEAN:  # before androiddesktop.paths reads these
    sys.argv = [a for a in sys.argv if a != "--clean"]
    YOURS = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AndroidDesktop"
    os.environ.update(ANDROIDDESKTOP_DATA=str(YOURS / "demo"), ANDROIDDESKTOP_RUNTIME=str(YOURS),
                      ANDROIDDESKTOP_SERIAL="emulator-5640")

import win32api  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from androiddesktop import adb, agentrec, catalog, datadir, runtime, winapi  # noqa: E402
from androiddesktop.manager import APP_CLASS, agent_running, send_to_agent  # noqa: E402
from androiddesktop.paths import LOGS  # noqa: E402

START = "python tools/demo.py android" if CLEAN else "start WinDroid from the Start menu"
# Apps that show nothing personal by default. Check each one before filming anyway.
FLEET = ["com.google.android.deskclock", "com.google.android.apps.maps", "com.android.settings",
         "com.android.chrome", "com.google.android.dialer"]
SNAP = ["com.google.android.apps.maps", "com.android.settings"]
OPENED = Path(tempfile.gettempdir()) / "windroid-demo-windows.json"  # what `close` closes
FPS = 60
SWP = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE


# ------------------------------------------------------------------ windows
def labels() -> dict[str, str]:
    try:
        return {a["package"]: a["label"] for a in json.loads(catalog.CACHE.read_text(encoding="utf-8"))}
    except (OSError, ValueError, KeyError):
        return {}


def find_window(label: str) -> int:
    found: list[int] = []

    def each(hwnd: int, _) -> None:
        if (win32gui.GetClassName(hwnd) == APP_CLASS and win32gui.IsWindowVisible(hwnd)
                and win32gui.GetWindowText(hwnd) == label):
            found.append(hwnd)
    win32gui.EnumWindows(each, None)
    return found[0] if found else 0


def work_area() -> tuple[int, int, int, int]:
    monitor = win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)
    return win32api.GetMonitorInfo(monitor)["Work"]


def scale() -> float:
    return winapi.dpi_of(win32gui.GetDesktopWindow()) / 96


def remember(package: str) -> None:
    try:
        opened = json.loads(OPENED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        opened = []
    if package not in opened:
        OPENED.write_text(json.dumps(opened + [package]), encoding="utf-8")


def showing(hwnd: int) -> bool:
    """Whether the window shows its app yet: until the app's first frame it paints "Opening..." itself,
    with its view inside still hidden."""
    views: list[int] = []

    def each(child: int, _) -> bool:
        if win32gui.IsWindowVisible(child):
            views.append(child)
        return True
    win32gui.EnumChildWindows(hwnd, each, None)
    return bool(views)


def open_app(package: str, label: str, timeout: float = 30) -> int:
    """The app's window, opening it if needed, once the app is in it."""
    hwnd = find_window(label)
    if not hwnd:
        if not send_to_agent({"open": package}):
            sys.exit(f"WinDroid didn't answer: {START} first.")
        remember(package)
    end = time.monotonic() + timeout
    while not hwnd or not showing(hwnd):
        if time.monotonic() > end:
            sys.exit(f"{label} didn't open within {timeout:.0f} seconds.")
        time.sleep(0.05)
        hwnd = hwnd or find_window(label)
    time.sleep(0.4)  # its first frame
    return hwnd


def ease(t: float) -> float:
    """Ease in and out (cubic): starts and lands softly, like Windows' own animations."""
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def glide(hwnd: int, rect: tuple[int, int, int, int], seconds: float = 0.7) -> None:
    """Move and resize a window smoothly to rect (left, top, width, height), in screen pixels. A size that
    changes makes Android lay the app out again, so the size moves at a third of the frame rate."""
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    start = (l, t, r - l, b - t)
    resizing = start[2:] != rect[2:]
    frames = max(1, round(seconds * FPS))
    size = start[2:]
    for i in range(1, frames + 1):
        k = ease(i / frames)
        x, y, w, h = (round(a + (b - a) * k) for a, b in zip(start, rect))
        if not resizing or i % 3 == 0 or i == frames:
            size = (w, h)
        win32gui.SetWindowPos(hwnd, 0, x, y, *size, SWP)
        time.sleep(1 / FPS)


def raise_window(hwnd: int) -> None:
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)


def row(n: int) -> list[tuple[int, int, int, int]]:
    """n phone-shaped slots in one row, centred on the work area."""
    left, top, right, bottom = work_area()
    gap = round(24 * scale())
    width = min((right - left - gap * (n + 1)) // n, round((bottom - top) * 0.8 / 2.05))
    height = round(width * 2.05)
    x0 = left + (right - left - (width * n + gap * (n - 1))) // 2
    y = top + (bottom - top - height) // 2
    return [(x0 + i * (width + gap), y, width, height) for i in range(n)]


def halves() -> list[tuple[int, int, int, int]]:
    left, top, right, bottom = work_area()
    gap = round(12 * scale())
    width = (right - left - gap * 3) // 2
    return [(left + gap, top + gap, width, bottom - top - gap * 2),
            (left + gap * 2 + width, top + gap, width, bottom - top - gap * 2)]


# ------------------------------------------------------------------ scenes
def android(args: argparse.Namespace) -> int:
    if args.quit:
        print("Shutting the demo Android down." if send_to_agent({"quit": True}) else "The demo Android isn't running.")
        return 0
    if not all(p.marker.exists() for p in runtime.PACKAGES):
        sys.exit("Set up WinDroid first: the demo Android uses its Android runtime.")
    if not runtime.avd_config().exists():
        runtime.create_avd()
        print("Made the demo Android. Its first start takes a couple of minutes.")
    if not agent_running():
        subprocess.Popen([str(Path(sys.executable).with_name("pythonw.exe")), "-m", "androiddesktop", "boot"],
                         cwd=ROOT, close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    print("Starting the demo Android...", flush=True)
    end = time.monotonic() + 300
    while not (agent_running() and adb.online() and adb.booted()):
        if time.monotonic() > end:
            sys.exit(f"The demo Android didn't start within 5 minutes. Its logs: {LOGS}")
        time.sleep(2)
    print("The demo Android is ready. Run the scenes with --clean, e.g. python tools/demo.py --clean fleet")
    print("Close your own WinDroid while filming (tray icon > Quit): two Androids need a lot of memory.")
    return 0


def check(_: argparse.Namespace) -> int:
    problems = []
    if not agent_running():
        problems.append(f"WinDroid isn't running: {START}.")
    elif not adb.online():
        problems.append(f"Android isn't running: {START}.")
    else:
        if adb.user_locked():
            problems.append("Android is locked: unlock it in the launcher.")
        elif adb.keyguard_showing():
            problems.append("Android's lock screen is still up: unlock it in the launcher.")
    names = labels()
    missing = [p for p in FLEET + SNAP if p not in names]
    if missing:
        problems.append(f"Not installed in Android (pick others with `fleet <package>...`): {', '.join(sorted(set(missing)))}")
    left, top, right, bottom = work_area()
    print(f"Screen work area: {right - left} x {bottom - top} px at {scale():.0%} scaling")
    print("Before each take: Do Not Disturb on, desktop icons hidden, nothing personal in the apps.")
    for p in problems:
        print(f"  - {p}")
    print("Ready to film." if not problems else "Not ready yet.")
    return 1 if problems else 0


def launcher(_: argparse.Namespace) -> int:
    if not send_to_agent({"show": True}):
        sys.exit(f"WinDroid isn't running: {START}.")
    return 0


def fleet(args: argparse.Namespace) -> int:
    packages = args.packages or FLEET
    names = labels()
    time.sleep(args.delay)
    slots = row(len(packages))
    for package, slot in zip(packages, slots):
        label = names.get(package)
        if not label:
            print(f"Skipping {package}: not installed in Android.")
            continue
        hwnd = open_app(package, label)
        raise_window(hwnd)
        glide(hwnd, slot, args.speed)
        time.sleep(args.pause)
    return 0


def snap(args: argparse.Namespace) -> int:
    packages = args.packages or SNAP
    if len(packages) != 2:
        sys.exit("snap takes two packages.")
    names = labels()
    windows = [open_app(p, names.get(p, p)) for p in packages]
    time.sleep(max(args.delay, args.pause))
    for hwnd, half in zip(windows, halves()):
        raise_window(hwnd)
        glide(hwnd, half, args.speed * 1.3)
        time.sleep(args.pause)
    return 0


def agent(args: argparse.Namespace) -> int:
    folder = Path(args.folder).expanduser()
    agentrec.switch_on(folder, args.minutes)
    print(f"Recording AI agent sessions into {folder} for the next {args.minutes:.0f} minutes.")
    env = {k: v for k, v in os.environ.items() if k.startswith("ANDROIDDESKTOP_")}
    entry = {"command": sys.executable, "args": ["-m", "androiddesktop", "mcp"], **({"env": env} if env else {})}
    print("Claude must use this checkout's MCP server (WinDroid 0.1.1 doesn't record). For Claude Desktop, add")
    print("this under \"mcpServers\" in %APPDATA%\\Claude\\claude_desktop_config.json, then restart it:")
    print(f'  "windroid-demo": {json.dumps(entry)}')
    print("Then ask Claude, for example:")
    print('  "Using WinDroid, get me directions to the airport in Maps, then show it to me."')
    print("Every hidden app it opens gets a folder: its screen after each step, and what it did.")
    print("Press Ctrl+C to stop recording.")
    seen: dict[Path, int] = {}
    end = time.monotonic() + args.minutes * 60
    try:
        while time.monotonic() < end:
            for d in sorted(folder.glob("*/events.jsonl")):
                steps = sum(1 for _ in d.open(encoding="utf-8"))
                if seen.get(d) != steps:
                    seen[d] = steps
                    print(f"  {d.parent.name}: {steps} step{'s' * (steps != 1)}")
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        agentrec.switch_off()
    print(f"Recording off. {len(seen)} session{'s' * (len(seen) != 1)} in {folder}")
    return 0


def close(_: argparse.Namespace) -> int:
    try:
        opened = json.loads(OPENED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        opened = []
    names = labels()
    for package in opened:
        hwnd = find_window(names.get(package, package))
        if hwnd:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    OPENED.unlink(missing_ok=True)
    print(f"Closed {len(opened)} window{'s' * (len(opened) != 1)} the demo opened.")
    return 0


def main() -> int:
    winapi.dpi_aware()
    datadir.ensure()  # from inside a packaged app (Claude Desktop) it mustn't fork the data folder
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clean", action="store_true", help="use the demo Android (see android)")
    sub = parser.add_subparsers(dest="scene", required=True)
    p = sub.add_parser("android", help="start the demo Android: no Google account, nothing of yours")
    p.add_argument("--quit", action="store_true", help="shut it down")
    p.set_defaults(run=android)
    sub.add_parser("check").set_defaults(run=check)
    sub.add_parser("launcher", help="open WinDroid's launcher (for Ctrl+K)").set_defaults(run=launcher)
    for name, run, help_ in (("fleet", fleet, "packages to open, left to right"),
                             ("snap", snap, "the two packages to put side by side")):
        p = sub.add_parser(name)
        p.add_argument("packages", nargs="*", help=help_)
        p.add_argument("--speed", type=float, default=0.7, help="seconds each glide takes")
        p.add_argument("--pause", type=float, default=0.6, help="seconds between steps")
        p.add_argument("--delay", type=float, default=0, help="seconds before it starts: time to get in place")
        p.set_defaults(run=run)
    p = sub.add_parser("agent")
    p.add_argument("--minutes", type=float, default=20)
    p.add_argument("--folder", default=str(Path.home() / "Videos" / "WinDroid demo" / "agent"))
    p.set_defaults(run=agent)
    sub.add_parser("close").set_defaults(run=close)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
