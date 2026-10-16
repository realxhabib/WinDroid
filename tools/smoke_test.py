"""End-to-end check of app windows against the real Android VM:

    python tools/smoke_test.py [package]            # default: Clock
    python tools/smoke_test.py --scrcpy [package]   # the scrcpy fallback, without emulator displays

1. Android is up (it boots if needed) and unlocked.
2. The app opens in its own window, on an emulator display (emuview.py) or scrcpy's video, and it
   draws a picture.
3. Resizing the window resizes the app.
4. The window's display going away (scrcpy killed, or the emulator display removed, as if Android
   crashed) makes the window reconnect, with the app on a fresh display. On an emulator display,
   losing the frame stream alone just streams again.
5. Closing the window ends scrcpy or the emulator display, and Android's display goes away.

The windows live in this process, so the new window code is tested even while an agent runs, and a
running agent is left alone. With ANDROIDDESKTOP_SERIAL pointing at a test emulator, start that
emulator first: this never boots one then. Typing isn't covered: that needs real keyboard input. Exit
code 0 when everything passed.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from androiddesktop import adb, emudisplays, emuview, runtime, winapi  # noqa: E402

winapi.dpi_aware()

import win32con  # noqa: E402
import win32gui  # noqa: E402
from PIL import ImageStat  # noqa: E402

from androiddesktop.appwindow import RUNNING, AppWindow  # noqa: E402
from androiddesktop.catalog import Catalog  # noqa: E402
from androiddesktop.manager import WindowManager  # noqa: E402
from androiddesktop.paths import EMULATOR_PORT, SERIAL  # noqa: E402
from androiddesktop.vm import LOCKED, VM  # noqa: E402

failures: list[str] = []


def step(name: str) -> None:
    print(f"\n{name}", flush=True)


def check(ok: object, what: str) -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}", flush=True)
    if not ok:
        failures.append(what)
    return bool(ok)


def wait(condition, timeout: float, interval: float = 0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if result := condition():
            return result
        time.sleep(interval)
    return condition()


def embedded(win: AppWindow) -> bool:
    return bool(win.status == RUNNING and win.child and winapi.user32.GetAncestor(win.child, 1) == win.hwnd)


def draws_picture(win: AppWindow) -> bool:
    """Not one flat color (a blank or black window)."""
    if not win.child:
        return False
    _, _, w, h = win32gui.GetClientRect(win.child)
    img = win.surface.capture(w, h) if win.surface else winapi.capture(win.child, w, h)
    return img is not None and ImageStat.Stat(img.convert("L")).stddev[0] > 4


def fills_window(win: AppWindow) -> bool:
    _, _, cw, ch = win32gui.GetClientRect(win.hwnd)
    l, t, r, b = win32gui.GetWindowRect(win.child)
    x, y = win32gui.ScreenToClient(win.hwnd, (l, t))
    return (x, y, r - l, b - t) == (0, win.bar, cw, ch - win.bar)


def root_tasks() -> list[str]:
    return adb.shell("cmd activity stack list", timeout=10).split("RootTask")[1:]


def app_on(package: str, display: int | None) -> bool:
    return any(f"displayId={display} " in task and package in task for task in root_tasks())


def display_size(display: int | None) -> tuple[int, int] | None:
    for task in root_tasks():
        if m := re.search(rf"bounds=\[0,0\]\[(\d+),(\d+)\] displayId={display} ", task):
            return int(m.group(1)), int(m.group(2))
    return None


def run(package: str) -> None:
    if not runtime.installed():
        check(False, "Android is set up (run: python -m androiddesktop setup)")
        return
    step("Android")
    started = time.monotonic()
    vm = VM()
    if SERIAL != f"emulator-{EMULATOR_PORT}":
        if not check(adb.online() and adb.booted(), f"the test Android {SERIAL} is up (start it first)"):
            return
        vm._boot = lambda: vm._set("off", "not booting the user's Android for a test")  # never, here
    up = vm.ensure_running(timeout=900, wait_for_unlock=False)
    if vm.state == LOCKED:
        check(False, "Android is unlocked (it's locked: enter the PIN in the launcher, then run this again)")
        return
    if not check(up, f"Android is up after {time.monotonic() - started:.0f}s" + (f" ({vm.detail})" if vm.detail else "")):
        return
    catalog = Catalog()
    if catalog.get(package) is None:
        catalog.refresh()
    if not check(catalog.get(package), f"{package} is installed"):
        return
    manager = WindowManager(vm, catalog, ipc=False)
    manager.start()

    step(f"Open {package}")
    started = time.monotonic()
    manager.open(package)
    win = wait(lambda: manager.windows.get(package), 5)
    if not check(win, "a window appears"):
        return
    try:
        if not check(wait(lambda: embedded(win), 90), f"the app is embedded in it ({time.monotonic() - started:.1f}s)"):
            return
        check(wait(lambda: draws_picture(win), 15), "it draws a picture")
        check(wait(lambda: app_on(package, win.display_id), 10), f"the app runs on its own display ({win.display_id})")

        step("Resize")
        if winapi.user32.IsZoomed(win.hwnd):
            win32gui.ShowWindow(win.hwnd, win32con.SW_RESTORE)
        original = win32gui.GetWindowRect(win.hwnd)
        before = wait(lambda: display_size(win.display_id), 5)
        l, t, r, b = original
        win32gui.SetWindowPos(win.hwnd, 0, l, t, r - l + 160, b - t - 120,
                              win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        check(wait(lambda: fills_window(win), 5), "the app fills the resized window")
        after = wait(lambda: (size := display_size(win.display_id)) != before and size, 10, 0.5)
        check(after, f"Android's display follows ({before} -> {after})")
        win32gui.SetWindowPos(win.hwnd, 0, l, t, r - l, b - t, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)

        if win.surface:
            step("The emulator display's frames stop")
            surface = win.surface
            surface.wake()
            channel = surface._channel
            channel.close()
            check(wait(lambda: surface.live and surface._channel is not channel, 10), "they stream again on their own")
            frames = surface.frames
            adb.shell(f"input -d {win.display_id} swipe 200 600 200 300 300")
            check(wait(lambda: surface.frames > frames, 10), "and the app's next frames show")
            check(embedded(win) and win.display_id == surface.display, "in the same window, on the same display")
        step("The display goes away (as if Android crashed)" if win.surface else "scrcpy crashes")
        gen, display, started = win._gen, win.display_id, time.monotonic()
        if win.surface:
            emudisplays.emu("multidisplay", "del", str(win.surface.slot))
        else:
            win.proc.kill()
        check(wait(lambda: win.status != RUNNING, 5, 0.02), f"the window notices ({win.status})")
        check(win32gui.IsWindow(win.hwnd), "and stays open")
        check(wait(lambda: embedded(win) and win._gen != gen, 60), f"the app is back ({time.monotonic() - started:.1f}s)")
        check(win.display_id != display, f"on a fresh display ({display} -> {win.display_id})")
        check(wait(lambda: app_on(package, win.display_id), 10), "with the app running on it")
        check(wait(lambda: draws_picture(win), 15), "drawing a picture")
        time.sleep(6)  # long enough for the app-exit watcher to have closed it by mistake
        check(manager.windows.get(package) is win and embedded(win), "it stays open")
    finally:
        step("Close")
        hwnd, proc, display, surface = win.hwnd, win.proc, win.display_id, win.surface
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        check(wait(lambda: not win32gui.IsWindow(hwnd), 10), "the window closes")
        if surface:
            check(wait(lambda: surface.slot not in emudisplays.emulator_displays(), 10, 0.5),
                  f"the emulator display ({surface.slot}) is gone")
        else:
            check(proc is None or wait(lambda: proc.poll() is not None, 10), "scrcpy ends")
        check(wait(lambda: display_size(display) is None, 10, 0.5), "Android removes the display")


def main() -> int:
    args = sys.argv[1:]
    if "--scrcpy" in args:
        args.remove("--scrcpy")
        emuview.link.usable = lambda: None
    run(args[0] if args else "com.google.android.deskclock")
    print("\nFAILED:\n  " + "\n  ".join(failures) if failures else "\nAll checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
