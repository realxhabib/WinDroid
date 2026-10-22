"""Screen Text, an example plugin (PLUGINS.md walks through it). A side panel glued to the app's window
lists the text on the app's screen, read with Windows' own OCR; click a line and the app gets a tap
right there.

To try it, copy this folder into %LOCALAPPDATA%\\AndroidDesktop\\plugins, restart WinDroid, turn on
"Screen Text (example)" in Settings > Plugins and open Settings or Clock.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PIL import ImageChops

from androiddesktop.log import log
from androiddesktop.overlay import side_panel
from androiddesktop.plugins import AppHandle, Geometry, Plugin
from androiddesktop.winapi import light_theme
from androiddesktop.winocr import Line, recognize

PAGE = Path(__file__).with_name("panel.html")
WIDTH = 300  # the panel's width at 100% scaling
GAP = 6  # between the window and the panel
EVERY = 1.0  # seconds between looks at the screen


class PanelApi:
    """What the panel's page can call, as window.pywebview.api.<method>(...). Runs on pywebview's thread."""

    def __init__(self, plugin: ScreenText) -> None:
        self._plugin = plugin

    def tap(self, index: int) -> None:
        self._plugin.tap_line(int(index))


class ScreenText(Plugin):
    name = "screen_text"  # its key in state.json; keep it once people use the plugin
    title = "Screen Text (example)"
    packages = frozenset({"com.android.settings", "com.google.android.deskclock"})
    default_on = False  # an example: on only when turned on in Settings > Plugins

    def __init__(self, app: AppHandle) -> None:
        super().__init__(app)
        self.panel = None
        self.lines: list[Line] = []  # the last reading, in screenshot pixels
        self.done = threading.Event()

    # ------------------------------------------------------------------ hooks: the window thread, so be quick
    def start(self) -> None:
        self.panel = self.app.panel(title=self.title, url=PAGE, api=PanelApi(self), size=(WIDTH, 600),
                                    light=light_theme())
        threading.Thread(target=self.watch, daemon=True, name=f"{self.name}-{self.app.hwnd}").start()

    def geometry_changed(self, geo: Geometry) -> None:
        if self.panel:
            rect, _ = side_panel(geo.frame, geo.work, round(WIDTH * geo.scale), round(GAP * geo.scale), geo.maximized)
            self.panel.place(rect, geo.visible)

    def stop(self) -> None:
        self.done.set()
        if self.panel:
            self.panel.destroy()

    # ------------------------------------------------------------------ a thread of its own
    def watch(self) -> None:
        """Read the screen whenever it has changed, and show the lines in the panel."""
        last = None
        while not self.done.wait(EVERY):
            shot = self.app.screenshot()
            if shot is None:  # minimized, or the app is reconnecting
                continue
            if last is not None and last.size == shot.size and ImageChops.difference(last, shot).getbbox() is None:
                continue  # nothing new on screen
            last = shot
            try:
                lines = recognize(shot, 2)  # 2: read it enlarged, for small type
            except Exception as e:
                log(f"{self.name}: OCR: {e!r}")
                continue
            self.lines = [line for line in lines if len(line.text.strip()) > 1]  # icons read as a letter
            self.panel.call("show", [line.text for line in self.lines], kind="lines")

    def tap_line(self, index: int) -> None:
        """A tap in the middle of a line the panel lists (pywebview's thread; the tap goes on its own)."""
        if 0 <= index < len(self.lines):
            line = self.lines[index]
            x, y = line.x + line.w / 2, line.y + line.h / 2
            threading.Thread(target=self.app.tap, args=(x, y), daemon=True).start()


PLUGIN = ScreenText
