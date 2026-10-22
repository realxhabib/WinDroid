"""The plugin host, typing (keys.py) and overlay placement, without Android or real windows."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from androiddesktop import keys, overlay
from androiddesktop import plugins
from androiddesktop.plugins import Geometry, Plugin, PluginHost


class Typing(unittest.TestCase):
    def test_every_printable_ascii_character_has_a_us_key(self):
        for code in range(32, 127):
            self.assertIn(chr(code), keys.US_KEYS, repr(chr(code)))
        self.assertEqual(keys.US_KEYS["A"], (0x1E, True))
        self.assertEqual(keys.US_KEYS["?"], (0x35, True))  # the / key, shifted

    def test_typographic_characters_become_plain_ones(self):
        self.assertEqual(keys.normalize("it\u2019s \u201cfine\u201d \u2014 ok\u2026\nyes"), "it's \"fine\" - ok... yes")

    def test_only_unshifted_keys_are_typed(self):
        """SDL drops a posted Shift: capitals, symbols and what no US key types are pasted instead."""
        self.assertEqual(keys.chunks("hi \U0001F600 there"), [("keys", "hi "), ("paste", "\U0001F600"),
                                                            ("keys", " there")])
        self.assertEqual(keys.chunks("can I come?"), [("keys", "can "), ("paste", "I"), ("keys", " come"),
                                                     ("paste", "?")])
        self.assertEqual(keys.chunks(""), [])

    def test_key_messages(self):
        self.assertEqual(keys.key_lparam(0x1E, False), 1 | 0x1E << 16)
        self.assertEqual(keys.key_lparam(0x1E, True), 1 | 0x1E << 16 | 1 << 30 | 1 << 31)
        self.assertEqual(keys.key_lparam(keys.SC_END, False) >> 24 & 1, 1)  # End is an extended key

    def keyboard(self):
        kb = keys.Keyboard(7, sleep=lambda s: None)
        posted, pasted = [], []
        kb._post = lambda scan, up: posted.append((scan, up))
        kb.paste = lambda text: pasted.append(text) or True
        return kb, posted, pasted

    def test_typing_posts_keys_and_pastes_the_rest(self):
        kb, posted, pasted = self.keyboard()
        with mock.patch.object(keys.win32gui, "IsWindow", return_value=True):
            progress = []
            self.assertTrue(kb.type("hi I", progress.append))
        self.assertEqual(posted, [(0x23, False), (0x23, True), (0x17, False), (0x17, True), (0x39, False), (0x39, True)])
        self.assertEqual(pasted, ["I"])
        self.assertEqual(progress[-1], 1.0)

    def test_pasting_is_the_default(self):
        kb, posted, pasted = self.keyboard()
        with mock.patch.object(keys.win32gui, "IsWindow", return_value=True):
            self.assertTrue(kb.insert("Can I? \u2019k"))
        self.assertEqual(pasted, ["Can I? \u2019k"])
        self.assertEqual(posted, [])

    def test_select_all_is_ctrl_a(self):
        kb, posted, _ = self.keyboard()
        with mock.patch.object(keys.win32gui, "IsWindow", return_value=True):
            kb.select_all()
        self.assertEqual(posted, [(keys.SC_CTRL, False), (keys.SC_A, False), (keys.SC_A, True), (keys.SC_CTRL, True)])

    def test_stops_when_the_window_goes(self):
        kb, posted, _ = self.keyboard()
        with mock.patch.object(keys.win32gui, "IsWindow", side_effect=[True, True, False]):
            self.assertFalse(kb.type("hello"))
        self.assertEqual(kb.stopped, "the window went away")
        self.assertEqual(len(posted), 4)

    def test_paste_puts_the_clipboard_back(self):
        kb = keys.Keyboard(7, sleep=lambda s: None)
        kb._post = lambda scan, up: None
        clip = ["mine"]
        with mock.patch.object(keys.win32gui, "IsWindow", return_value=True), \
                mock.patch.object(keys, "_get_clipboard", lambda: clip[0]), \
                mock.patch.object(keys, "_set_clipboard", lambda t: clip.__setitem__(0, t) or True) as _:
            seen = []
            real = kb.press
            kb.press = lambda scan, ctrl=False: seen.append(clip[0]) or real(scan, ctrl)
            self.assertTrue(kb.paste("hello \u2014 you"))
        self.assertEqual(seen, ["hello - you"])  # on the clipboard while Ctrl+V went in
        self.assertEqual(clip[0], "mine")


class Placement(unittest.TestCase):
    WORK = (0, 0, 1920, 1040)

    def test_panel_goes_right_of_the_window_when_it_fits(self):
        rect, side = overlay.side_panel((500, 100, 900, 900), self.WORK, 344, 6, False)
        self.assertEqual((rect, side), ((906, 100, 1250, 900), "right"))

    def test_panel_goes_left_when_the_right_is_full(self):
        rect, side = overlay.side_panel((1500, 100, 1900, 900), self.WORK, 344, 6, False)
        self.assertEqual((rect, side), ((1150, 100, 1494, 900), "left"))

    def test_panel_sits_inside_a_maximized_window(self):
        rect, side = overlay.side_panel((-8, -8, 1928, 1048), self.WORK, 344, 6, True)
        self.assertEqual(side, "inside")
        self.assertEqual(rect[2], 1914)

    def test_panel_stays_on_the_work_area(self):
        rect, _ = overlay.side_panel((500, -20, 900, 1200), self.WORK, 344, 6, False)
        self.assertEqual((rect[1], rect[3]), (0, 1040))

    def test_premultiplied_alpha(self):
        im = Image.new("RGBA", (1, 1), (200, 100, 50, 128))
        b, g, r, a = overlay.premultiply(im)
        self.assertEqual((r, g, b, a), (100, 50, 25, 128))

    def test_geometry_maps_view_pixels_to_the_screen(self):
        geo = Geometry((100, 100, 500, 900), (101, 131, 499, 899), (0, 0, 1920, 1040), 1.5, True, False)
        self.assertEqual(geo.size, (398, 768))
        self.assertEqual(geo.to_screen(10, 20.4), (111, 151))


class Recorder(Plugin):
    name = "recorder"
    packages = frozenset({"com.example"})

    def __init__(self, app):
        super().__init__(app)
        self.calls = []

    def start(self):
        self.calls.append("start")

    def geometry_changed(self, geo):
        self.calls.append("geometry")

    def app_lost(self):
        self.calls.append("lost")

    def app_back(self):
        self.calls.append("back")

    def stop(self):
        self.calls.append("stop")


class FakeWin:
    def __init__(self, package, hwnd):
        self.app = mock.Mock(package=package, label=package)
        self.hwnd = hwnd


class Host(unittest.TestCase):
    def setUp(self):
        self.host = PluginHost(mock.Mock(), modules=(), folders=[])
        self.host.classes = [Recorder]
        patches = [mock.patch("androiddesktop.plugins.AppHandle.geometry", return_value=None),
                   mock.patch.object(PluginHost, "_watch_foreground"),
                   mock.patch("androiddesktop.plugins.log"),
                   mock.patch("androiddesktop.plugins.state.get", return_value={})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_attaches_to_its_package_only(self):
        self.host.shown(FakeWin("com.other", 1))
        self.assertEqual(self.host.instances, {})
        win = FakeWin("com.example", 2)
        self.host.shown(win)
        (plugin,) = self.host.instances[2]
        self.assertEqual(plugin.calls, ["start", "geometry"])

    def test_lifecycle(self):
        win = FakeWin("com.example", 3)
        self.host.shown(win)
        plugin = self.host.instances[3][0]
        self.host.moved(win)
        self.host.lost(win)
        self.host.shown(win)  # scrcpy reconnected: the same plugin carries on
        self.host.closed(win)
        self.host.closed(win)  # WM_CLOSE and WM_DESTROY both report it
        self.assertEqual(plugin.calls, ["start", "geometry", "geometry", "lost", "back", "geometry", "stop"])
        self.assertNotIn(3, self.host.instances)

    def test_a_plugin_can_be_turned_off(self):
        with mock.patch("androiddesktop.plugins.state.get", return_value={"recorder": False}):
            self.host.shown(FakeWin("com.example", 4))
        self.assertEqual(self.host.instances, {})

    def test_a_failing_plugin_doesnt_take_the_window_down(self):
        class Broken(Recorder):
            name = "broken"

            def geometry_changed(self, geo):
                raise RuntimeError("boom")

        self.host.classes = [Broken, Recorder]
        self.host.shown(FakeWin("com.example", 5))
        self.host.moved(FakeWin("com.example", 5))
        self.assertEqual(self.host.instances[5][1].calls, ["start", "geometry", "geometry"])



NOTES = """from androiddesktop.plugins import Plugin
from .words import WORD


class Notes(Plugin):
    name = "notes"
    title = "Notes " + WORD
    packages = frozenset({"com.example.notes"})


PLUGIN = Notes
"""


class Installed(unittest.TestCase):
    """Plugins installed apart from WinDroid: a folder each in AndroidDesktop's plugins folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.installed = self.root / "plugins"
        for p in [mock.patch.object(plugins, "PLUGINS", self.installed), mock.patch.object(plugins, "log"),
                  mock.patch.dict(os.environ, {"ANDROIDDESKTOP_PLUGINS": ""})]:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: [sys.modules.pop(m) for m in list(sys.modules) if m.startswith("windroid_plugins.")])

    def plugin(self, root, folder, init, files=None):
        (root / folder).mkdir(parents=True)
        (root / folder / "__init__.py").write_text(init, encoding="utf-8")
        for name, body in (files or {}).items():
            (root / folder / name).write_text(body, encoding="utf-8")

    def test_none_installed(self):
        self.assertEqual(plugins.plugin_folders(), [])
        self.assertEqual(PluginHost(mock.Mock())._load(), [])

    def test_loads_a_plugin_package_with_its_own_modules(self):
        self.plugin(self.installed, "notes", NOTES, {"words.py": 'WORD = "helper"'})
        (self.installed / "not-a-plugin").mkdir()
        (self.installed / "data-only").mkdir()
        (self.installed / "data-only" / "config.json").write_text("{}", encoding="utf-8")
        classes = PluginHost(mock.Mock())._load()
        self.assertEqual([(c.name, c.title, c.__module__) for c in classes],
                         [("notes", "Notes helper", "windroid_plugins.notes")])

    def test_a_broken_plugin_is_skipped(self):
        self.plugin(self.installed, "broken", "raise RuntimeError('not today')")
        self.plugin(self.installed, "notes", NOTES, {"words.py": 'WORD = "ok"'})
        classes = PluginHost(mock.Mock())._load()
        self.assertEqual([c.name for c in classes], ["notes"])
        self.assertNotIn("windroid_plugins.broken", sys.modules)
        self.assertIn("can't load", plugins.log.call_args_list[0].args[0])

    def test_a_checkout_being_worked_on_comes_first(self):
        checkout = self.root / "checkout"
        self.plugin(self.installed, "notes", NOTES, {"words.py": 'WORD = "installed"'})
        self.plugin(checkout, "notes", NOTES, {"words.py": 'WORD = "checkout"'})
        with mock.patch.dict(os.environ, {"ANDROIDDESKTOP_PLUGINS": str(checkout)}):
            self.assertEqual(plugins.plugin_folders(), [checkout / "notes"])
            self.assertEqual(PluginHost(mock.Mock())._load()[0].title, "Notes checkout")


if __name__ == "__main__":
    unittest.main()
