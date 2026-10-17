"""The plugin host, typing (keys.py) and overlay placement, without Android or real windows."""
import unittest
from unittest import mock

from PIL import Image

from androiddesktop import emuview, keys, overlay
from androiddesktop.plugins import AppHandle, Geometry, Plugin, PluginHost, _DisplayKeyboard, typeable


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


class DisplayTyping(unittest.TestCase):
    """Text into an app on an emulator display: InputHelper lines and Android's clipboard, no window messages."""

    def setUp(self):
        self.surface = mock.Mock(view=7, display=12, point=(8554, "token"), slot=2, closed=False)
        self.lines, self.clips = [], []
        patches = [mock.patch.object(emuview.link, "helper", self.lines.append),
                   mock.patch.object(emuview.link, "set_clipboard", lambda point, text: self.clips.append(text)),
                   mock.patch.object(keys.win32gui, "IsWindow", return_value=True),
                   mock.patch.object(keys, "_get_clipboard", return_value=None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.kb = _DisplayKeyboard(self.surface, sleep=lambda s: None)

    def test_select_all_is_ctrl_a_on_the_display(self):
        self.assertTrue(self.kb.select_all())
        self.assertEqual(self.lines, ["key 12 0 113 0 12288", "key 12 0 29 0 12288", "key 12 1 29 0 12288",
                                      "key 12 1 113 0 0"])

    def test_paste_goes_through_androids_clipboard(self):
        self.assertTrue(self.kb.insert("Can I? \u2019k"))
        self.assertEqual(self.clips, ["Can I? 'k"])
        self.assertIn("key 12 0 50 0 12288", self.lines)  # Ctrl+V
        self.assertFalse(any(line.startswith("text") for line in self.lines))

    def test_paste_puts_the_pcs_clipboard_back_if_the_text_came_across(self):
        clip = ["mine"]
        emuview.link.set_clipboard = lambda point, text: clip.__setitem__(0, text)  # the emulator shares it
        with mock.patch.object(keys, "_get_clipboard", lambda: clip[0]), \
                mock.patch.object(keys, "_set_clipboard", lambda t: clip.__setitem__(0, t) or True):
            self.assertTrue(self.kb.paste("hello"))
        self.assertEqual(clip[0], "mine")

    def test_typing_sends_text_and_pastes_what_the_key_map_cant_type(self):
        self.assertEqual(typeable("Hi \U0001F600!"), [("keys", "Hi "), ("paste", "\U0001F600"), ("keys", "!")])
        progress = []
        self.assertTrue(self.kb.type("Hi \U0001F600", progress.append))
        texts = [line for line in self.lines if line.startswith("text")]
        self.assertEqual(texts, ["text 12 48", "text 12 69", "text 12 20"])
        self.assertEqual(self.clips, ["\U0001F600"])
        self.assertEqual(progress[-1], 1.0)

    def test_stops_when_the_display_closes(self):
        self.surface.closed = True
        self.assertFalse(self.kb.type("hello"))
        self.assertEqual(self.kb.stopped, "the window went away")
        self.assertEqual(self.lines, [])


class Handle(unittest.TestCase):
    """AppHandle picks the emulator display's paths when the window shows one, scrcpy's otherwise."""

    def handle(self, surface):
        win = mock.Mock(hwnd=1, child=7, view=7, closing=False, protected=None, surface=surface, display_id=12)
        win.app.package = "com.example"
        return AppHandle(mock.Mock(), win)

    def test_on_an_emulator_display(self):
        surface = mock.Mock(view=7, closed=False, point=(1, "t"), slot=2)
        surface.capture.return_value = Image.new("RGB", (40, 30))
        app = self.handle(surface)
        with mock.patch("androiddesktop.plugins.win32gui") as gui, \
                mock.patch("androiddesktop.plugins.winapi.capture") as printwindow, \
                mock.patch("androiddesktop.plugins.time.sleep"):
            gui.IsWindow.return_value = True
            gui.GetClientRect.return_value = (0, 0, 40, 30)
            self.assertEqual(app.screenshot().size, (40, 30))
            surface.capture.assert_called_once_with(40, 30)
            printwindow.assert_not_called()
            self.assertIsInstance(app.keyboard(), _DisplayKeyboard)
            app.click(5, 6)
        self.assertEqual(surface.touch.call_args_list, [mock.call(5, 6, True), mock.call(5, 6, False)])

    def test_in_a_scrcpy_window(self):
        app = self.handle(None)
        with mock.patch("androiddesktop.plugins.win32gui") as gui, \
                mock.patch("androiddesktop.plugins.winapi.capture") as printwindow:
            gui.IsWindow.return_value = True
            gui.GetClientRect.return_value = (0, 0, 40, 30)
            app.screenshot()
            printwindow.assert_called_once_with(7, 40, 30)
            kb = app.keyboard()
        self.assertIs(type(kb), keys.Keyboard)

    def test_a_closed_display_isnt_used(self):
        app = self.handle(mock.Mock(view=7, closed=True))
        self.assertIsNone(app._surface())


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
        self.host = PluginHost(mock.Mock(), modules=())
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


if __name__ == "__main__":
    unittest.main()
