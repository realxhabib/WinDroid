"""The Hinge plugin's controller, fed synthetic screens: drafts, the pop-ups' plan, filling in, chats.
Claude, the windows and the keyboard are stand-ins."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from androiddesktop.plugins import Geometry
from androiddesktop.plugins.hinge import HingeMatcher, reader, writer
from androiddesktop.plugins.hinge.model import Chat, Msg
from androiddesktop.winocr import Line
from tests.test_hinge import H, W, profile_screen

DRAFT = {"summary": "Brooklyn, food", "recommend": "like", "skip_reason": "", "target_id": "prompt_1",
         "target_why": "one-word answer", "best": 0,
         "options": [{"comment": "just 'coffee'? which order", "angle": "funny", "target_id": "prompt_1"},
                     {"comment": "nice photo, where is it", "angle": "cute", "target_id": "photo_1"}]}
REPLY = {"read": "warm", "stage": "rapport", "recommend": "reply", "wait_reason": "", "needs_info": "", "best": 0,
         "options": [{"text": "thursday?", "angle": "cute"}]}


class FakeApp:
    package = "co.hinge.app"
    hwnd = 1

    def __init__(self):
        self.typed = []
        self.clicks = []
        self.taps = []

    def geometry(self):
        return Geometry((0, 0, W, H + 30), (0, 30, W, H + 30), (0, 0, 1920, 1040), 1.0, True, False)

    def call(self, fn):
        fn()

    def drag(self, x, y, dy):
        return False  # nothing to scroll (see ScrollingApp)

    def fling(self, x, y, dy):
        return False

    def screenshot(self):
        return None

    def focus(self):
        pass

    def click(self, x, y):
        self.clicks.append((x, y))

    def tap(self, x, y):
        self.taps.append((x, y))
        return True

    def keyboard(self, cps=12.0, cancel=None):
        app = self

        class K:
            stopped = ""

            def select_all(self):
                app.typed.append("<select all>")
                return True

            def insert(self, text, mode="paste", progress=None):
                app.typed.append(text)
                if progress:
                    progress(1.0)
                return True
        return K()


class ScrollingApp(FakeApp):
    """Hinge showing the synthetic profile: a drag moves it with the finger (less Android's touch slop),
    a fling all the way, and each move or read reaches the plugin the way the capture loop would."""
    SLOP, END = 8, 520  # pixels; how far down the profile goes
    current = None  # the one on screen, for reader.at_top (the stand-in screens have no real type to OCR)

    def __init__(self):
        super().__init__()
        self.top = 0
        self.plugin = None
        self.drags = []
        ScrollingApp.current = self

    def _move(self, new):
        new = max(0, min(self.END, new))
        dy, self.top = new - self.top, new
        if dy:
            self.plugin._on_scroll(dy)

    def drag(self, x, y, dy):
        self.drags.append((x, y, dy))
        self._move(self.top - dy - (self.SLOP if dy < 0 else -self.SLOP))
        return True

    def fling(self, x, y, dy):
        self._move(self.top - dy * 10)
        return True

    def read(self):
        im, lines = profile_screen(self.top)
        self.plugin._on_screen(reader.classify(im, lines), im, False)

    def screenshot(self):
        return profile_screen(self.top)[0]


class Controller(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        (Path(self.dir.name) / "config.json").write_text(json.dumps({"backend": "claude-cli"}), encoding="utf-8")
        for p in (mock.patch.dict(os.environ, {"HINGE_MATCHER_HOME": self.dir.name}),
                  mock.patch("androiddesktop.plugins.hinge.log"),
                  mock.patch("androiddesktop.plugins.hinge.writer.Writer.draft", return_value=json.loads(json.dumps(DRAFT))),
                  mock.patch("androiddesktop.plugins.hinge.writer.Writer.reply", return_value=json.loads(json.dumps(REPLY))),
                  mock.patch("threading.Thread", SyncThread),
                  mock.patch("androiddesktop.plugins.hinge.HingeMatcher._scan_later"),  # (see its own test)
                  mock.patch("androiddesktop.plugins.hinge.reader.at_top",
                             lambda im, ocr: ScrollingApp.current is None or ScrollingApp.current.top == 0)):
            p.start()
            self.addCleanup(p.stop)
        ScrollingApp.current = None
        self.app = FakeApp()
        self.p = HingeMatcher(self.app)
        self.p.glow = mock.Mock()
        self.p.bubble = mock.Mock()
        self.p.pulse = mock.Mock()
        self.p._box_filled = lambda box, wait=3.0: True  # (the stand-in screens have no real type to read)
        self.plans = []
        real = self.p._plan
        self.p._plan = lambda geo: self.plans.append(real(geo)) or self.plans[-1]

    def see(self, im, lines, jumped=False):
        self.p._on_screen(reader.classify(im, lines), im, jumped)

    def test_a_profile_gets_ideas_and_the_glow_goes_on_the_picked_card(self):
        self.see(*profile_screen(0))
        snap = self.p.snapshot()
        self.assertEqual(snap["mode"], "profile")
        self.assertEqual([o["text"] for o in snap["options"]], ["just 'coffee'? which order", "nice photo, where is it"])
        self.assertEqual(snap["draft"]["target_label"], "prompt “My simple pleasures”")
        target, label, idea, head, foot, edge, avoid = self.plans[-1]
        self.assertTrue(abs(target[1] - 490) <= 3, target)
        self.assertEqual((label, idea, edge), ("Claude's pick", "just 'coffee'? which order", None))
        self.assertTrue(head.startswith("funny"))
        # Her question and answer, for the bubble to keep off: where the card's lines are now.
        self.assertEqual(len(avoid), 2)
        self.assertTrue(all(target[1] < y0 < target[3] for _, y0, _, _ in avoid))

    def test_the_glow_follows_the_scroll(self):
        self.see(*profile_screen(0))
        self.p._on_scroll(100)
        target = self.plans[-1][0]
        self.assertTrue(abs(target[1] - 390) <= 3, target)
        self.p._on_scroll(500)  # the card is above the screen now: a note at the top edge instead
        target, _, idea, _, _, edge, _ = self.plans[-1]
        self.assertIsNone(target)
        self.assertEqual(edge, "top")
        self.assertIn("↑", idea)

    def test_preview_and_cycling(self):
        self.see(*profile_screen(0))
        self.p.set_preview(1)
        self.assertEqual(self.plans[-1][2], "nice photo, where is it")
        self.p.set_preview(-1)
        self.p.hotkey(1)  # Alt+Right
        self.assertEqual(self.p.current, 1)
        self.p.hotkey(1)
        self.assertEqual(self.p.current, 0)

    def test_an_idea_clicked_early_waits_for_the_comment_box(self):
        self.see(*profile_screen(0))
        self.p.insert(0, "just 'coffee'? which order")
        self.assertIsNotNone(self.p.armed)
        self.assertEqual(self.app.typed, [])
        sheet = Image.new("RGB", (W, H), (255, 255, 255))
        self.see(sheet, [Line("Add a comment", 40, 600, 120, 16), Line("Send like", 150, 700, 90, 18)], jumped=True)
        self.assertEqual(self.app.typed, ["just 'coffee'? which order"])
        x, y = self.app.taps[-1]
        self.assertTrue(600 <= y <= 616 and not (142 <= x <= 248 and 692 <= y <= 726))  # the box, not Send
        self.assertEqual(self.p.inserted, 0)
        self.assertIn("Send like", self.p.message)

    def test_filling_in_again_replaces_what_it_typed(self):
        self.see(*profile_screen(0))
        sheet = Image.new("RGB", (W, H), (255, 255, 255))
        self.see(sheet, [Line("Add a comment", 40, 600, 120, 16), Line("Send like", 150, 700, 90, 18)], jumped=True)
        self.p.insert(0, "first")
        self.see(sheet, [Line("first", 40, 600, 40, 16), Line("Send like", 150, 700, 90, 18)])
        self.p.insert(1, "second")
        self.assertEqual(self.app.typed, ["first", "<select all>", "second"])

    def test_back_from_the_sheet_is_the_same_profile(self):
        self.see(*profile_screen(0))
        draft = self.p.draft
        sheet = Image.new("RGB", (W, H), (255, 255, 255))
        self.see(sheet, [Line("Add a comment", 40, 600, 120, 16), Line("Send like", 150, 700, 90, 18)], jumped=True)
        self.see(*profile_screen(0), jumped=True)
        self.assertIs(self.p.draft, draft)

    def test_the_next_profile_starts_over(self):
        self.see(*profile_screen(0, name="Robin"))
        gen = self.p.profile_gen
        self.see(*profile_screen(0, name="Marisol"), jumped=True)
        self.assertEqual(self.p.profile_gen, gen + 1)
        self.assertEqual(self.p.model.profile.name, "Marisol")

    def test_skip_points_at_hinge_and_never_taps(self):
        self.see(*profile_screen(0))
        self.p.skip()
        self.assertTrue(self.p.skip_armed)
        self.assertEqual((self.app.clicks, self.app.taps), ([], []))
        self.assertEqual(self.app.typed, [])

    def test_a_reply_goes_into_the_chats_box(self):
        """A chat's box takes focus only from a touch on it (it doesn't focus itself, as the like
        sheet's does)."""
        chat = Chat("Juniper", [Msg("them", "Hey")])
        self.p._on_screen(reader.Screen(reader.CHAT, (W, H), chat=chat, box=(12, 764, 271, 812), band=(86, 748)),
                          None, False)
        self.p.insert(0, "thursday?")
        self.assertEqual(self.app.typed, ["thursday?"])
        x, y = self.app.taps[0]
        self.assertTrue(12 <= x <= 271 and 764 <= y <= 812, (x, y))
        self.assertEqual(self.p.inserted, 0)

    def test_chat_replies_come_when_they_write(self):
        chat = Chat("Juniper", [Msg("them", "hi"), Msg("me", "hey")])
        scr = reader.Screen(reader.CHAT, (W, H), chat=chat, box=(12, 764, 271, 812), band=(86, 748))
        with mock.patch.object(self.p, "request_reply", wraps=self.p.request_reply) as asked:
            self.p._on_screen(scr, None, False)
            self.assertEqual(asked.call_count, 1)  # a new chat
            self.assertEqual(self.p.snapshot()["options"][0]["text"], "thursday?")
            sent = Chat("Juniper", chat.messages + [Msg("me", "thursday?")])
            self.p._on_screen(reader.Screen(reader.CHAT, (W, H), chat=sent, box=None, band=(86, 748)), None, False)
            self.assertEqual(asked.call_count, 1)  # the user's own message: wait
            self.assertIsNone(self.p.reply)
            answered = Chat("Juniper", sent.messages + [Msg("them", "yes!")])
            self.p._on_screen(reader.Screen(reader.CHAT, (W, H), chat=answered, band=(86, 748)), None, False)
            self.assertEqual(asked.call_count, 2)

    def test_in_a_chat_the_idea_keeps_off_the_messages(self):
        chat = Chat("Juniper", [Msg("them", "Hey")])
        # Messages from 274 down to 733, an empty top, and the box under them.
        scr = reader.Screen(reader.CHAT, (W, H), chat=chat, box=(12, 764, 271, 812), band=(86, 748),
                            gaps=[(92, 274), (609, 637), (733, 748)])
        self.p._on_screen(scr, None, False)
        x, y = self.p.bubble.place.call_args[0][:2]
        m, h = self.p._bubble_m, self.p._bubble_h
        self.assertTrue(92 <= y + m and y + h - m <= 274, (y, h))
        clip = self.p.bubble.place.call_args[0][4]
        self.assertEqual(clip, (86, H))  # the box's glow isn't cut off

    def test_a_new_profile_is_read_to_the_end_before_the_draft(self):
        app = ScrollingApp()
        p = HingeMatcher(app)
        app.plugin = p
        p.glow, p.bubble, p.pulse = mock.Mock(), mock.Mock(), mock.Mock()
        p._settle = lambda: None  # the fake page moves at once
        p.jump_wait = 0.05
        p.eyes = mock.Mock(read_now=app.read)
        with mock.patch("time.sleep"):
            app.read()
        drafted = [[c.id for c in call.args[0].cards] for call in writer.Writer.draft.call_args_list]
        self.assertEqual([c.id for c in p.model.profile.cards], ["photo_1", "prompt_1", "photo_2", "prompt_2"])
        self.assertIn("dumplings in Flushing", p.model.profile.card("prompt_2").text)
        # Ideas at once from what showed, then again from the whole profile.
        self.assertEqual(drafted, [["photo_1", "prompt_1", "photo_2"], ["photo_1", "prompt_1", "photo_2", "prompt_2"]])
        self.assertEqual(app.top, 0)  # and back at the top
        self.assertFalse(p.scanning)
        for x, y, dy in app.drags:  # the finger stays in the margin, above the X (bottom left)
            self.assertLess(x, 20)
            self.assertLess(y, 680)
            self.assertLess(abs(dy), 0.45 * (760 - 56))

    def scanning_app(self, ignored):
        """A ScrollingApp whose first `ignored` drags don't move the page (an emulator too busy to keep
        up), and a plugin on it."""
        app = ScrollingApp()
        real = app.drag

        def drag(x, y, dy):
            if len(app.drags) < ignored:
                app.drags.append((x, y, dy))
                return True
            return real(x, y, dy)
        app.drag = drag
        p = HingeMatcher(app)
        app.plugin = p
        p.glow, p.bubble, p.pulse = mock.Mock(), mock.Mock(), mock.Mock()
        p._settle = lambda: None
        p.jump_wait = 0.05
        p.eyes = mock.Mock(read_now=app.read)
        return app, p

    def test_drags_that_dont_take_arent_the_end_of_the_profile(self):
        app, p = self.scanning_app(ignored=2)
        with mock.patch("time.sleep"):
            app.read()
        self.assertEqual([c.id for c in p.model.profile.cards], ["photo_1", "prompt_1", "photo_2", "prompt_2"])
        self.assertTrue(p.model.complete)

    def test_a_read_cut_short_goes_again_and_a_passing_frame_doesnt_cut_it(self):
        self.see(*profile_screen(0))  # (the stand-in app has no screenshots: the scan stops at once)
        HingeMatcher._scan_later.assert_called_once_with(self.p.scan_gen)
        self.assertEqual(self.p.scan_tries, 1)
        p = self.p
        p.scanning = True
        p.screen = reader.Screen(reader.OTHER, (W, H))  # a frame of something else, just now
        p.last_profile_at = time.monotonic()
        self.assertTrue(p._scan_on(p.scan_gen))
        p.last_profile_at -= 3  # and for longer
        self.assertFalse(p._scan_on(p.scan_gen))

    def test_a_resize_keeps_the_ideas(self):
        self.see(*profile_screen(0))
        self.p.model.complete, self.p.draft_whole = True, True
        draft, gen, drafts = self.p.draft, self.p.profile_gen, writer.Writer.draft.call_count
        im, lines = profile_screen(0)
        wide = Image.new("RGB", (W + 120, H), (251, 252, 249))  # the window made wider: laid out again
        wide.paste(im, (60, 0))
        moved = [Line(ln.text, ln.x + 60, ln.y, ln.w, ln.h) for ln in lines]
        self.see(wide, moved, jumped=True)
        self.assertIs(self.p.draft, draft)
        self.assertEqual(self.p.profile_gen, gen)
        self.assertEqual(writer.Writer.draft.call_count, drafts)
        self.assertEqual(self.p.model.profile.name, "Robin")

    def test_a_profile_back_again_gets_its_ideas_back(self):
        writer.Writer.draft.side_effect = lambda *a, **k: json.loads(json.dumps(DRAFT))  # a new one each time
        self.see(*profile_screen(0, name="Robin"))
        self.p.select(1)
        draft, drafts = self.p.draft, writer.Writer.draft.call_count
        self.see(*profile_screen(0, name="Marisol"), jumped=True)  # the next one...
        self.assertIsNot(self.p.draft, draft)
        self.see(*profile_screen(0, name="Robin"), jumped=True)  # ...and back
        self.assertIs(self.p.draft, draft)
        self.assertEqual(self.p.current, 1)
        self.assertEqual(writer.Writer.draft.call_count, drafts + 1)  # only Marisol's

    def test_a_page_that_wont_move_isnt_called_read(self):
        app, p = self.scanning_app(ignored=99)
        with mock.patch("time.sleep"):
            app.read()
        self.assertFalse(p.model.complete)  # what the user scrolls to still gets read
        self.assertFalse(p.scanning)
        self.assertEqual(len(app.drags), 6)  # twice, and two more tries

    def test_a_profile_opened_halfway_down_is_read_from_the_top(self):
        app = ScrollingApp()
        app.top = 400
        p = HingeMatcher(app)
        app.plugin = p
        p.glow, p.bubble, p.pulse = mock.Mock(), mock.Mock(), mock.Mock()
        p._settle = lambda: None  # the fake page moves at once
        p.jump_wait = 0.05
        p.eyes = mock.Mock(read_now=app.read)
        with mock.patch("time.sleep"):
            app.read()
        self.assertEqual([c.id for c in p.model.profile.cards], ["photo_1", "prompt_1", "photo_2", "prompt_2"])
        self.assertEqual((app.top, p.model.offset), (0, 0))

    def test_the_scan_stops_when_the_like_sheet_opens(self):
        app = ScrollingApp()
        p = HingeMatcher(app)
        app.plugin = p
        p.glow, p.bubble, p.pulse = mock.Mock(), mock.Mock(), mock.Mock()
        p._settle = lambda: None  # the fake page moves at once
        p.jump_wait = 0.05
        sheet = Image.new("RGB", (W, H), (255, 255, 255))
        reads = []

        def read():
            reads.append(1)
            if len(reads) == 3:  # the user tapped a heart meanwhile
                p._on_screen(reader.classify(sheet, [Line("Add a comment", 40, 600, 120, 16)]), sheet, True)
            else:
                app.read()
        p.eyes = mock.Mock(read_now=read)
        with mock.patch("time.sleep"):
            app.read()
        self.assertFalse(p.scanning)
        self.assertEqual(len(reads), 3)

    def test_scrolling_a_profile_read_through_changes_nothing(self):
        app = ScrollingApp()
        p = HingeMatcher(app)
        app.plugin = p
        p.glow, p.bubble, p.pulse = mock.Mock(), mock.Mock(), mock.Mock()
        p._settle = lambda: None
        p.jump_wait = 0.05
        p.eyes = mock.Mock(read_now=app.read)
        with mock.patch("time.sleep"):
            app.read()
        self.assertTrue(p.model.complete)
        cards, drafts = [c.id for c in p.model.profile.cards], writer.Writer.draft.call_count
        for top in (100, 300, 520, 200, 0):  # the user scrolls about
            app._move(top)
            app.read()
        self.assertEqual([c.id for c in p.model.profile.cards], cards)
        self.assertEqual(writer.Writer.draft.call_count, drafts)

    def test_a_card_clicked_in_the_dock_is_scrolled_to(self):
        app = ScrollingApp()
        p = HingeMatcher(app)
        app.plugin = p
        p.glow, p.bubble, p.pulse = mock.Mock(), mock.Mock(), mock.Mock()
        p._settle = lambda: None
        p.jump_wait = 0.05
        p.eyes = mock.Mock(read_now=app.read)
        with mock.patch("time.sleep"):
            app.read()  # (reads it all, and back to the top)
            self.assertTrue(p.model.complete)
            app.END = 900  # room below for the card to come right up
            photo2 = p.model.profile.card("photo_2")
            p.go_to("photo_2")
            self.assertTrue(56 <= photo2.top - app.top <= 56 + 24, app.top)  # just under the top
            self.assertEqual(p.model.offset, app.top)  # and the pop-ups know where it is
            self.assertIsNone(p.going)
            p.go_to("prompt_1")  # back up
            prompt1 = p.model.profile.card("prompt_1")
            self.assertTrue(56 <= prompt1.top - app.top <= 56 + 24, app.top)
            p.go_to("photo_1")  # the first card: the top of the profile, name and all
            self.assertEqual(app.top, 0)
        self.assertTrue(all(abs(dy) <= round(0.42 * 704) for _, _, dy in app.drags))  # small enough to follow
        self.assertEqual(app.typed + app.clicks + app.taps, [])  # only ever scrolled

    def test_no_scrolling_to_a_card_while_reading_the_profile(self):
        self.see(*profile_screen(0))
        self.p.scanning = True
        self.p.dock = mock.Mock()
        with mock.patch.object(self.app, "drag") as drag:
            self.p.go_to("prompt_1")
        drag.assert_not_called()
        self.assertIn("Still reading", self.p.dock.call.call_args[0][1])

    def test_the_bubble_can_be_hidden(self):
        self.see(*profile_screen(0))
        with mock.patch("androiddesktop.plugins.hinge.state.put") as put:
            self.p.hotkey(4)  # Alt+Q
        put.assert_called_with("hinge_bubble", False)
        self.assertFalse(self.p.show_bubble)
        self.p.bubble.hide.assert_called()
        self.assertFalse(self.p.snapshot()["bubble"])
        self.p.set_bubble(True)
        self.assertTrue(self.p.snapshot()["bubble"])

    def test_dock_snapshot_is_json(self):
        self.see(*profile_screen(0))
        json.dumps(self.p.snapshot())


class SyncThread:
    """threading.Thread that runs its target at start(), so the tests needn't wait."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        if self.target:
            self.target(*self.args, **self.kwargs)


if __name__ == "__main__":
    unittest.main()
