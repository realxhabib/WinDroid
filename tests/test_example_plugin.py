"""The example plugin in examples/plugins/screen_text (PLUGINS.md), loaded the way WinDroid loads plugins."""
import sys
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from androiddesktop import plugins
from androiddesktop.plugins import Geometry
from androiddesktop.winocr import Line

FOLDER = Path(__file__).resolve().parents[1] / "examples" / "plugins" / "screen_text"


class FakeApp:
    hwnd = 7

    def __init__(self, shots):
        self.shots = list(shots)
        self.plugin = None
        self.tap = mock.Mock()

    def screenshot(self):
        if not self.shots:
            self.plugin.done.set()
            return None
        return self.shots.pop(0)


class ScreenTextTest(unittest.TestCase):
    def setUp(self):
        self.module = plugins.import_plugin(FOLDER)
        self.addCleanup(sys.modules.pop, "windroid_plugins.screen_text", None)

    def make(self, shots=()):
        app = FakeApp(shots)
        plugin = self.module.PLUGIN(app)
        app.plugin = plugin
        plugin.panel = mock.Mock()
        return app, plugin

    def test_what_it_is(self):
        cls = self.module.PLUGIN
        self.assertEqual((cls.name, cls.default_on), ("screen_text", False))
        self.assertEqual(cls.packages, {"com.android.settings", "com.google.android.deskclock"})
        self.assertTrue(self.module.PAGE.is_file())

    def test_reads_the_screen_when_it_changes(self):
        a, b = Image.new("RGB", (40, 80), "white"), Image.new("RGB", (40, 80), "black")
        app, plugin = self.make([a, a.copy(), b, None])
        readings = iter([[Line("Network", 4, 10, 30, 8)], [Line("Display", 4, 20, 30, 8), Line("Sound", 4, 40, 20, 8)]])
        with mock.patch.object(self.module, "EVERY", 0), \
                mock.patch.object(self.module, "recognize", side_effect=lambda shot, scale: next(readings)) as ocr:
            plugin.watch()
        self.assertEqual(ocr.call_count, 2)  # the unchanged screen isn't read again
        self.assertEqual([c.args[1] for c in plugin.panel.call.call_args_list], [["Network"], ["Display", "Sound"]])

    def test_a_click_taps_the_middle_of_its_line(self):
        app, plugin = self.make()
        plugin.lines = [Line("Network", 4, 10, 30, 8)]
        with mock.patch.object(self.module.threading, "Thread") as thread:
            plugin.tap_line(0)
            plugin.tap_line(5)  # not a line (any more): nothing
        thread.assert_called_once_with(target=app.tap, args=(19.0, 14.0), daemon=True)

    def test_the_panel_stays_beside_the_window(self):
        app, plugin = self.make()
        geo = Geometry(frame=(100, 100, 500, 900), content=(108, 140, 492, 892), work=(0, 0, 1920, 1040),
                       scale=1.0, visible=True, maximized=False)
        plugin.geometry_changed(geo)
        plugin.panel.place.assert_called_once_with((506, 100, 806, 900), True)


if __name__ == "__main__":
    unittest.main()
