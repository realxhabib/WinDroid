"""End-to-end check of the plugin system against the real Android VM, on Settings:

    python tools/plugin_check.py

1. A test plugin attaches when Settings shows in its window, and gets the window's geometry.
2. Its screenshot of the app isn't black, and Windows OCR reads it.
3. An overlay layer sits on the app's screen and follows the window when it moves; a side panel
   (WebView2) is glued to the window's edge and follows too.
4. Minimizing hides both; restoring shows them again.
5. Text through the window's keyboard path (keys.py) reaches Android exactly, pasted and typed.
6. Closing the window stops the plugin and takes its layer and panel along.

Like smoke_test.py, the window lives in this process, so a running agent is left alone (and so is
any app it shows). Android must be running and unlocked. Exit code 0 when everything passed.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from androiddesktop import winapi  # noqa: E402

winapi.dpi_aware()

import webview  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
from PIL import Image, ImageStat  # noqa: E402

from androiddesktop import winocr  # noqa: E402
from androiddesktop.catalog import Catalog  # noqa: E402
from androiddesktop.manager import WindowManager  # noqa: E402
from androiddesktop.overlay import side_panel  # noqa: E402
from androiddesktop.panel import Panel  # noqa: E402
from androiddesktop.plugins import Plugin  # noqa: E402
from androiddesktop.vm import RUNNING, VM  # noqa: E402

PACKAGE = "com.android.settings"
TEXT = "Hey, can i say hi?"  # pasted: exact, with Android's keyboard app left out of it
TYPED = "nice to meet you"  # typed key by key
failures: list[str] = []
PAGE = Path(__file__).resolve().parent / "plugin_check_panel.html"


def step(name: str) -> None:
    print(f"- {name}", flush=True)


def check(ok: bool, what: str) -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}", flush=True)
    if not ok:
        failures.append(what)
    return ok


def wait(cond, timeout: float, every: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(every)
    return bool(cond())


class CheckPlugin(Plugin):
    name = "check"
    packages = frozenset({PACKAGE})
    instance: "CheckPlugin | None" = None

    def __init__(self, app) -> None:
        super().__init__(app)
        self.geos = []
        self.stopped = False
        self.layer = None
        self.panel = None
        CheckPlugin.instance = self

    def start(self) -> None:
        self.layer = self.app.layer()
        self.panel = self.app.panel(title="Plugin check", url=PAGE, api=None, size=(300, 600), light=True)

    def geometry_changed(self, geo) -> None:
        self.geos.append(geo)
        l, t, r, b = geo.content
        img = Image.new("RGBA", (r - l, 40), (255, 92, 138, 160))
        self.layer.update(img, l, t + 60)
        if self.panel:
            rect, _ = side_panel(geo.frame, geo.work, round(300 * geo.scale), 6, geo.maximized)
            self.panel.place(rect, geo.visible)

    def stop(self) -> None:
        self.stopped = True
        if self.panel:
            self.panel.destroy()


def field_text(app) -> str:
    """The search box's line, as OCR reads it."""
    lines = winocr.recognize(app.screenshot(), 2)
    return lines[0].text if lines else ""


def shows(app, text: str, wait: float = 6) -> str | None:
    """The box's line once it reads as text (up to the text cursor, which OCR may add a letter for),
    None if it doesn't within wait seconds: Settings searches as you type, and lags."""
    norm = lambda t: re.sub(r"[^a-z0-9]+", "", t.lower())
    end = time.monotonic() + wait
    while time.monotonic() < end:
        seen = field_text(app)
        if norm(seen)[:len(norm(text)) - 1] == norm(text)[:-1]:
            return seen
        time.sleep(0.5)
    return None


def ocr_text(im: Image.Image) -> str:
    return " ".join(ln.text for ln in winocr.recognize(im, 2))


def run(manager: WindowManager) -> None:
    try:
        _run(manager)
    except Exception as e:
        import traceback
        traceback.print_exc()
        failures.append(f"crashed: {e!r}")
    finally:
        print("\nPASSED" if not failures else f"\nFAILED: {len(failures)}", flush=True)
        for w in webview.windows:
            try:
                w.destroy()
            except Exception:
                pass


def _run(manager: WindowManager) -> None:
    step("Settings opens with the plugin attached")
    manager.open(PACKAGE)
    if not check(wait(lambda: CheckPlugin.instance is not None and CheckPlugin.instance.geos, 40), "plugin attached"):
        return
    p = CheckPlugin.instance
    app = p.app
    win = manager.windows[PACKAGE]
    time.sleep(2.5)  # let Settings draw
    geo = app.geometry()
    child = win32gui.GetWindowRect(win.child)
    check(geo.content == child, f"content rect {geo.content} is scrcpy's window {child}")
    check(geo.visible, "visible")

    step("Screenshot and OCR")
    wait(lambda: "search" in ocr_text(app.screenshot()).lower(), 10, 0.5)
    im = app.screenshot()
    ok = im is not None and ImageStat.Stat(im.convert("L")).stddev[0] > 5
    check(ok, f"screenshot of {im.size if im else None} has a picture")
    text = ocr_text(im) if ok else ""
    check("search" in text.lower(), f"OCR reads the screen ({text[:60]!r}...)")

    step("Layer and panel follow the window")
    wait(lambda: p.panel.hwnd and win32gui.IsWindowVisible(p.panel.hwnd), 15)
    check(win32gui.IsWindowVisible(p.layer.hwnd), "layer shown")
    check(bool(p.panel.hwnd) and win32gui.IsWindowVisible(p.panel.hwnd), "panel shown")
    l, t, r, b = win32gui.GetWindowRect(win.hwnd)
    before = len(p.geos)
    manager.call(lambda: win32gui.SetWindowPos(win.hwnd, 0, l + 60, t + 40, 0, 0,
                                               win32con.SWP_NOSIZE | win32con.SWP_NOZORDER))
    check(wait(lambda: len(p.geos) > before, 3), "geometry_changed after a move")
    time.sleep(0.5)
    geo = app.geometry()
    lx, ly, _, _ = win32gui.GetWindowRect(p.layer.hwnd)
    check((lx, ly) == (geo.content[0], geo.content[1] + 60), f"layer at {(lx, ly)} follows the content {geo.content[:2]}")
    pr = win32gui.GetWindowRect(p.panel.hwnd)
    check(abs(pr[0] - (geo.frame[2] + 6)) <= 2 or pr[2] <= geo.frame[0], f"panel {pr} glued to the frame {geo.frame}")
    owner = winapi.user32.GetWindow(p.panel.hwnd, 4)  # GW_OWNER
    check(owner == win.hwnd, "panel owned by the app window")

    step("Minimize hides, restore shows")
    manager.call(lambda: win32gui.ShowWindow(win.hwnd, win32con.SW_MINIMIZE))
    time.sleep(1.0)
    check(not win32gui.IsWindowVisible(p.layer.hwnd), "layer hidden while minimized")
    check(not win32gui.IsWindowVisible(p.panel.hwnd), "panel hidden while minimized")
    manager.call(lambda: win32gui.ShowWindow(win.hwnd, win32con.SW_RESTORE))
    time.sleep(1.2)
    check(win32gui.IsWindowVisible(p.layer.hwnd), "layer back")
    check(win32gui.IsWindowVisible(p.panel.hwnd), "panel back")

    step("Text through the window's keyboard")
    im = app.screenshot()
    lines = winocr.recognize(im, 2)
    search = next((ln for ln in lines if re.search(r"search", ln.text, re.I)), None)
    if not check(search is not None, "found Settings' search bar"):
        return
    app.click(round(search.x + search.w / 2), round(search.y + search.h / 2))
    time.sleep(5)  # the search screen opens, and its box takes the keyboard a moment later
    kb = app.keyboard()
    check(kb.insert(TEXT), f"pasted {kb.stopped}")
    seen = shows(app, TEXT)
    check(seen is not None and "can i" in seen, f"Android has {TEXT!r}, lowercase i and all (OCR: {seen!r})")
    kb.select_all()
    check(kb.insert(TYPED, mode="type"), f"typed {kb.stopped}")
    seen = shows(app, TYPED)
    check(seen is not None, f"select all + typing replaces it with {TYPED!r} (OCR: {field_text(app)!r})")

    step("Closing stops the plugin")
    layer = p.layer.hwnd
    manager.call(win.close)
    check(wait(lambda: p.stopped, 10), "plugin stopped")
    check(wait(lambda: not win32gui.IsWindow(layer), 10), "layer gone with the window")
    check(wait(lambda: p.panel.closed, 5), "panel closed")


def main() -> None:
    vm = VM()
    vm.refresh()
    if vm.state != RUNNING:
        sys.exit("Android isn't running (or is locked): start it from Android Desktop first.")
    PAGE.write_text("<!doctype html><body style='background:#b07bff;font:14px Segoe UI;color:#fff;padding:12px'>"
                    "Plugin check panel</body>", encoding="utf-8")
    manager = WindowManager(vm, Catalog(), ipc=False)
    manager.plugins.modules = ()
    manager.plugins.classes = [CheckPlugin]
    manager.plugins.panel_factory = lambda app, **kw: Panel(app.hwnd, **kw)
    manager.start()
    holder = webview.create_window("plugin-check", html="<p>", hidden=True)
    threading.Thread(target=run, args=(manager,), daemon=True).start()
    webview.start(gui="edgechromium", private_mode=True)
    PAGE.unlink(missing_ok=True)
    del holder
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
