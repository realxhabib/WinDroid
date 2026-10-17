"""The Hinge plugin: reading screens, following the scroll, the profile so far, drafting prompts, history.

Screens are drawn here the way Hinge for Android lays them out (cards on a near-white page, prompt
cards with a hairline border), with OCR's lines made up to match, so no Android or OCR is needed.
"""
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from androiddesktop.plugins.hinge import card_label, numbering, pops, reader, short_label, store, writer
from androiddesktop.plugins.hinge.eyes import ProfileModel
from androiddesktop.plugins.hinge.model import Card, Chat, Msg, Profile
from androiddesktop.winocr import Line

W, H = 396, 822
PAGE = (251, 252, 249)


def line(text, x, y, w, h):
    return Line(text, x, y, w, h)


def photo(draw, box, seed, top=0):
    """A stand-in photo: every row its own colours, as in a real picture (blocks would match a few rows
    off). top: how far the card is scrolled under the screen's top edge."""
    rnd = random.Random(seed)
    l, t, r, b = box
    for y in range(t - top, b):
        stops = sorted(rnd.randrange(l, r) for _ in range(10))
        colours = [(rnd.randrange(40, 220), rnd.randrange(30, 200), rnd.randrange(20, 190)) for _ in stops]
        if y >= t:
            for x0, x1, c in zip([l] + stops, stops + [r], colours + [colours[0]]):
                draw.line((x0, y, x1, y), fill=c)


def prompt(draw, box):
    draw.rounded_rectangle(box, radius=14, fill=(254, 254, 252), outline=(238, 238, 235))


def profile_screen(top=0, name="Robin"):
    """Discover scrolled down by `top` page pixels: filter chips fixed at the top, then the name and
    pronouns, photo 1, a prompt, photo 2, another prompt, and the dark tab bar at the bottom."""
    im = Image.new("RGB", (W, H), PAGE)
    d = ImageDraw.Draw(im)
    lines = []
    if name and 56 < 74 - top < 760:
        lines += [line(name, 20, 74 - top, 70, 26), line("she/her", 20, 106 - top, 43, 10)]
    page = [("photo", 130, 470, 1), ("prompt", 490, 660, "My simple pleasures|Coffee"),
            ("photo", 680, 1030, 2), ("prompt", 1050, 1220, "I know the best spot in town for|dumplings in Flushing")]
    for kind, t, b, what in page:
        t, b = t - top, b - top
        if b < 56 or t > 760:
            continue
        box = (20, max(t, 0), 375, b)  # Hinge's cards: 20 px in from both sides
        if kind == "photo":
            photo(d, box, what, top=max(0, -t))
        else:
            prompt(d, box)
            q, a = what.split("|")
            lines += [line(q, 44, t + 22, 200, 16), line(a, 44, t + 62, 220, 24)]
    d.rectangle((0, 0, W, 55), fill=(255, 255, 255))  # the chips stay put, over what scrolls under them
    d.rectangle((0, 760, W, H), fill=(26, 26, 26))  # and so does the tab bar
    lines = [ln for ln in lines if 56 <= ln.y and ln.bottom < 760] + [line("Signals", 72, 28, 43, 14)]
    return im, sorted(lines, key=lambda ln: (ln.y, ln.x))


class Reading(unittest.TestCase):
    def test_profile_cards(self):
        im, lines = profile_screen()
        scr = reader.classify(im, lines)
        self.assertEqual(scr.kind, reader.PROFILE)
        self.assertEqual((scr.name, scr.pronouns), ("Robin", "she/her"))
        self.assertEqual(scr.band, (56, 760))
        kinds = [c.kind for c in scr.cards]
        self.assertEqual(kinds, ["photo", "prompt", "photo"])
        p = scr.cards[1]
        self.assertEqual(p.text, "My simple pleasures: Coffee")
        self.assertEqual((p.question, p.answer), ("My simple pleasures", "Coffee"))
        self.assertTrue(abs(p.rect[1] - 490) <= 2 and abs(p.rect[3] - 660) <= 2, p.rect)
        self.assertEqual((p.rect[0], p.rect[2]), (20, 376))
        self.assertTrue(scr.cards[2].clipped_bottom)

    def test_like_sheet(self):
        im = Image.new("RGB", (W, H), (255, 255, 255))
        lines = [line("Add a comment", 40, 600, 120, 16), line("Send like", 150, 700, 90, 18)]
        scr = reader.classify(im, lines)
        self.assertEqual(scr.kind, reader.SHEET)
        self.assertIsNotNone(scr.box)
        l, t, r, b = scr.box
        self.assertTrue(t < 600 and b > 616 and l < 40 and r > 300)
        self.assertEqual(scr.send, (142, 692, 248, 726))

    def test_chat_and_its_messages(self):
        im = Image.new("RGB", (W, H), (254, 254, 252))
        d = ImageDraw.Draw(im)
        d.rectangle((0, 88, 198, 91), fill=(30, 30, 30))  # the Chat tab is the active one
        d.rounded_rectangle((60, 200, 300, 250), radius=14, fill=(238, 238, 238))  # theirs, after their photo
        d.rounded_rectangle((150, 300, 380, 350), radius=14, fill=(110, 60, 160))  # the user's
        lines = [line("Juniper", 125, 14, 62, 15), line("Chat", 84, 64, 30, 11), line("Profile", 278, 64, 38, 11),
                 line("so what's your go-to spot?", 72, 215, 200, 16),
                 line("the dumpling place on 8th", 165, 315, 200, 16),
                 line("Send a message", 36, 780, 116, 16)]
        scr = reader.classify(im, lines)
        self.assertEqual(scr.kind, reader.CHAT)
        self.assertEqual(scr.name, "Juniper")
        self.assertEqual([(m.who, m.text) for m in scr.chat.messages],
                         [("them", "so what's your go-to spot?"), ("me", "the dumpling place on 8th")])
        self.assertIsNotNone(scr.box)

    def test_a_short_message_the_whole_screen_ocr_missed(self):
        """Alone on the whole screen, a "Hey" can go unread: its bubble is read again on its own. Hinge's
        notes (who liked what, "double tap") aren't messages; the like says how it started."""
        im = Image.new("RGB", (W, H), (254, 254, 252))
        d = ImageDraw.Draw(im)
        d.rectangle((0, 88, 198, 91), fill=(30, 30, 30))  # the Chat tab is the active one
        d.rounded_rectangle((174, 566, 374, 606), radius=14, fill=(237, 226, 220))  # the like, the user's side
        d.ellipse((12, 662, 52, 702), fill=(60, 80, 100))  # their picture
        d.rounded_rectangle((58, 660, 110, 702), radius=14, fill=(238, 238, 238))  # their bubble
        d.text((70, 674), "Hey", fill=(26, 26, 26))
        lines = [line("Juniper", 125, 14, 62, 15), line("Chat", 84, 64, 30, 11), line("Profile", 278, 64, 38, 11),
                 line("You liked Juniper's photo.", 186, 580, 174, 14), line("Yesterday 11:40PM", 140, 637, 118, 11),
                 line("Double tap to like a message", 108, 720, 180, 11), line("Send a message", 27, 780, 126, 16)]
        crops = []

        def ocr(crop, scale):  # reads whatever it's given as the "Hey" at its padded spot
            crops.append(crop.size)
            return [Line("Hey", 40 + 10, 40 + 12, 22, 12)]
        scr = reader.classify(im, lines, ocr=ocr)
        self.assertEqual(scr.kind, reader.CHAT)
        self.assertEqual([(m.who, m.text) for m in scr.chat.messages], [("them", "Hey")])
        self.assertEqual(scr.chat.context, "You liked Juniper's photo.")
        self.assertEqual(len(crops), 1)  # only the bubble nothing was read in
        hey = next(ln for ln in scr.lines if ln.text == "Hey")
        self.assertTrue(58 < hey.x < 110 and 660 < hey.cy < 702, (hey.x, hey.cy))

    def test_hinges_own_cards_and_the_name(self):
        self.assertTrue(reader._PROMO.search("Learn more: Most Compatible We think you and Robin should meet."))
        name, pron = reader.profile_head([line("Robin", 20, 74, 70, 26), line("she/her", 20, 106, 43, 10)], [], (56, 760))
        self.assertEqual((name.text, pron), ("Robin", "she/her"))

    def test_what_looks_like_a_name(self):
        for name in ("Rue", "Lou Ann", "Noelle", "Rose-Ellen"):
            self.assertTrue(reader.looks_like_name(name), name)
        for text in ("Fashion Institute of Technology", "27 | 5' 6\"", "", "she/her is a whole sentence here"):
            self.assertFalse(reader.looks_like_name(text), text)

    def test_names_as_ocr_reads_them(self):
        self.assertEqual(reader.tidy_name("Noel I e"), "Noelle")
        self.assertEqual(reader.tidy_name("WilI"), "Will")
        self.assertEqual(reader.tidy_name("Lou Ann"), "Lou Ann")
        self.assertEqual(reader.tidy_name("< Juniper"), "Juniper")
        self.assertEqual(reader.tidy_name("Wren"), "Wren")

    def test_chat_profile_tab(self):
        im, lines = profile_screen(top=0, name="")
        d = ImageDraw.Draw(im)
        d.rectangle((0, 0, W, 100), fill=(255, 255, 255))
        d.rectangle((198, 88, 396, 91), fill=(30, 30, 30))  # Profile is the active tab
        lines = [ln for ln in lines if ln.y > 110] + [line("Juniper", 125, 14, 62, 15), line("Chat", 84, 64, 30, 11),
                                                      line("Profile", 278, 64, 38, 11)]
        scr = reader.classify(im, sorted(lines, key=lambda ln: ln.y))
        self.assertEqual(scr.kind, reader.CHAT_PROFILE)
        self.assertIn("My simple pleasures: Coffee", [c.text for c in scr.cards])

    def test_nothing_known(self):
        scr = reader.classify(Image.new("RGB", (W, H), (20, 20, 20)), [line("Control which matches", 80, 380, 200, 20)])
        self.assertEqual(scr.kind, reader.OTHER)


class Bubble(unittest.TestCase):
    CARD = (20, 100, 376, 500)

    def test_against_the_right_side_below_her_words(self):
        words = [(44, 122, 240, 138), (44, 160, 300, 220)]  # her question, and her answer in big type
        x, y = pops.place_on_card(self.CARD, (230, 90), words, (56, 760), 1.0)
        self.assertEqual(x + 230, 376 - 12)  # right-aligned
        self.assertGreater(y, 220)  # under the answer...
        self.assertLessEqual(y + 90, 500 - 68)  # ...and above the heart

    def test_as_little_in_the_way_as_it_can_be(self):
        words = [(30, 110, 370, 490)]  # text everywhere: it still goes somewhere on the card
        x, y = pops.place_on_card(self.CARD, (230, 90), words, (56, 760), 1.0)
        self.assertTrue(100 <= y and y + 90 <= 500)

    def test_only_on_what_shows(self):
        x, y = pops.place_on_card(self.CARD, (230, 90), [], (56, 300), 1.0)  # the card's bottom is off screen
        self.assertLessEqual(y + 90, 300)

    def test_in_a_chat_clear_of_the_messages(self):
        band, box_top = (86, 748), 764
        # Room under the last message: right above the box, the tail pointing at it.
        top, near = pops.chat_spot([(92, 274), (600, 748)], band, box_top, 100, 1.0)
        self.assertTrue(near)
        self.assertEqual(top + 100, box_top - 4)
        # None there: the lowest empty stretch it fits in, short of the messages.
        top, near = pops.chat_spot([(92, 274), (609, 637), (733, 748)], band, box_top, 100, 1.0)
        self.assertFalse(near)
        self.assertTrue(92 <= top and top + 100 <= 274, top)
        # None anywhere (a long chat): at the top, over the oldest.
        self.assertEqual(pops.chat_spot([(609, 637)], band, box_top, 100, 1.0), (90, False))

    def test_a_chats_empty_stretches(self):
        im = Image.new("RGB", (W, H), (254, 254, 252))
        ImageDraw.Draw(im).rounded_rectangle((58, 300, 200, 340), radius=14, fill=(238, 238, 238))
        self.assertEqual(reader.gaps(im, (86, 748)), [(86, 300), (341, 748)])


class Scrolling(unittest.TestCase):
    def test_finds_how_far_it_scrolled(self):
        band = (66, 760)
        a, _ = profile_screen(0)
        for dy in (37, 150, -0):
            b, _ = profile_screen(dy)
            coarse = reader.scroll_shift(reader.thumb(a), reader.thumb(b), band, H)
            self.assertIsNotNone(coarse)
            self.assertEqual(reader.refine_shift(a, b, band, coarse), dy)

    def test_a_playing_video_isnt_a_scroll(self):
        band = (56, 760)
        a, _ = profile_screen(0)

        def playing(im, seed):  # the second photo is a video: new pixels every frame
            im = im.copy()
            photo(ImageDraw.Draw(im), (20, 680, 375, 759), seed)
            return im
        b = playing(a, 99)
        self.assertEqual(reader.scroll_shift(reader.thumb(a), reader.thumb(b), band, H), 0)
        c, _ = profile_screen(60)
        c = playing(c.crop((0, 0, W, H)), 7)
        coarse = reader.scroll_shift(reader.thumb(a), reader.thumb(c), band, H)
        self.assertEqual(reader.refine_shift(a, c, band, coarse), 60)

    def test_a_different_screen_isnt_a_scroll(self):
        a, _ = profile_screen(0)
        b = Image.new("RGB", (W, H), (40, 40, 60))
        self.assertIsNone(reader.scroll_shift(reader.thumb(a), reader.thumb(b), (66, 760), H))


class Model(unittest.TestCase):
    def test_cards_add_up_as_you_scroll(self):
        m = ProfileModel()
        im, lines = profile_screen(0)
        self.assertTrue(m.observe(reader.classify(im, lines), im))
        self.assertEqual([c.id for c in m.profile.cards], ["photo_1", "prompt_1", "photo_2"])
        self.assertIsNotNone(m.profile.card("photo_1").image)
        m.offset = 400  # what the scroll measurement said
        im, lines = profile_screen(400)
        self.assertTrue(m.observe(reader.classify(im, lines), im))
        ids = [c.id for c in m.profile.cards]
        self.assertEqual(ids, ["photo_1", "prompt_1", "photo_2", "prompt_2"])
        p2 = m.profile.card("prompt_2")
        self.assertEqual(p2.text, "I know the best spot in town for: dumplings in Flushing")
        self.assertTrue(abs(p2.top - 1050) <= 3, p2.top)

    def test_a_measured_scroll_that_drifted_is_put_right(self):
        m = ProfileModel()
        im, lines = profile_screen(0)
        m.observe(reader.classify(im, lines), im)
        m.offset = 380  # 20 px off
        im, lines = profile_screen(400)
        m.observe(reader.classify(im, lines), im)
        self.assertTrue(abs(m.offset - 400) <= 3, m.offset)

    def test_a_sliver_taken_for_text_becomes_the_photo_it_was(self):
        m = ProfileModel()
        im, lines = profile_screen(0)
        scr = reader.classify(im, lines)
        m.observe(scr, im)
        # Next: only the top of the next card shows, and it passed for a text card...
        sliver = reader.SeenCard((20, 700, 376, 760), "text", clipped_bottom=True)
        m.observe(reader.Screen(reader.PROFILE, (W, H), [], cards=[sliver], band=(56, 760)), im, measured=True)
        wrong = [c for c in m.profile.cards if c.top == 700]
        self.assertEqual(len(wrong), 1)
        # ...then the whole of it shows: a photo, and the same card.
        whole = reader.SeenCard((20, 700, 376, 1060), "photo")
        m.observe(reader.Screen(reader.PROFILE, (W, H), [], cards=[whole], band=(56, 1100)), im, measured=True)
        card = next(c for c in m.profile.cards if c.top == 700)
        self.assertEqual(card.kind, "photo")
        self.assertEqual(len([c for c in m.profile.cards if c.top == 700]), 1)

    def test_someone_else(self):
        m = ProfileModel()
        im, lines = profile_screen(0, name="Robin")
        m.observe(reader.classify(im, lines), im)
        im2, lines2 = profile_screen(0, name="Marisol")
        self.assertFalse(m.observe(reader.classify(im2, lines2), im2))

    def test_numbered_as_on_the_page(self):
        """Ids go by when a card was first seen; once all of it's read, by where it is."""
        m = ProfileModel()
        m.profile.cards = [Card("photo_1", "photo", 130, 470, 20, 376), Card("prompt_1", "prompt", 490, 660, 20, 376),
                           Card("vitals_1", "vitals", 670, 700, 20, 376),  # first taken for a prompt...
                           Card("photo_3", "photo", 680, 1030, 20, 376), Card("photo_2", "photo", 1050, 1220, 20, 376)]
        m.profile.cards[2].id = "prompt_2"  # ...and kept that id
        changes = m.renumber()
        self.assertEqual([c.id for c in m.profile.cards], ["photo_1", "prompt_1", "vitals_1", "photo_2", "photo_3"])
        self.assertEqual(changes, {"prompt_2": "vitals_1", "photo_3": "photo_2", "photo_2": "photo_3"})
        self.assertEqual(numbering(m.profile.cards[::-1])["photo_3"], 3)

    def test_a_resize_keeps_what_was_read_and_puts_cards_right_as_they_show(self):
        m = ProfileModel()
        p1 = Card("prompt_1", "prompt", 490, 660, 20, 376, "My simple pleasures: Coffee", partial=False)
        ph = Card("photo_2", "photo", 680, 1030, 20, 376)
        p2 = Card("prompt_2", "prompt", 1050, 1220, 20, 376, "I know the best spot in town for: dumplings",
                  partial=False)
        m.profile.cards = [Card("photo_1", "photo", 130, 470, 20, 376), p1, ph, p2]
        m.complete = True
        m.relayout((396, 822), (516, 822))  # wider: photos grow with it, text cards about so
        self.assertAlmostEqual(ph.height, 350 * 476 / 356, delta=1)
        self.assertEqual(p1.right, 496)
        # In the new layout both prompts show: the photo between them came out taller than guessed.
        seen = [reader.SeenCard((20, 100, 496, 270), "prompt", p1.text, "My simple pleasures", "Coffee"),
                reader.SeenCard((20, 760, 496, 822), "prompt", p2.text, clipped_bottom=True)]
        lines = [line("My simple pleasures", 44, 122, 200, 16), line("Coffee", 44, 162, 100, 24)]
        m.observe(reader.Screen(reader.PROFILE, (516, 822), lines, cards=seen, band=(56, 822)),
                  Image.new("RGB", (516, 822), PAGE))
        self.assertEqual(p1.top - m.offset, 100)
        self.assertEqual(p2.top - m.offset, 760)
        self.assertAlmostEqual(ph.top - p1.bottom, 20 * 476 / 356, delta=1)  # moved along with the prompt above
        self.assertAlmostEqual(ph.height, 350 * 476 / 356, delta=1)
        self.assertEqual(len(m.profile.cards), 4)  # nothing new, nothing lost
        self.assertEqual(p1.boxes[0][1] - m.offset, 122)

    def test_labels(self):
        self.assertEqual(card_label(Card("prompt_2", "prompt", 0, 1, 0, 1, "My simple pleasures: Coffee")),
                         "prompt “My simple pleasures”")
        self.assertEqual(card_label(Card("photo_3", "photo", 0, 1, 0, 1)), "photo 3")
        self.assertEqual(short_label(Card("photo_3", "photo", 0, 1, 0, 1)), "Photo 3")
        self.assertEqual(short_label(Card("vitals_1", "vitals", 0, 1, 0, 1)), "Details")


PROFILE = Profile("Robin", {"Age": "29"}, [Card("photo_1", "photo", 0, 1, 0, 1, image=b"\xff\xd8jpeg"),
                                         Card("prompt_1", "prompt", 0, 1, 0, 1, "My simple pleasures: Coffee")])


class Drafting(unittest.TestCase):
    def test_prompts_carry_nothing_personal(self):
        """Who the user is comes from their config, never from the code (this repo is shared)."""
        for text in (writer.SYSTEM, writer.REPLY_SYSTEM):
            for word in ("doodle", "marriage", "faith", "church", "he cooks", "his strength"):
                self.assertNotIn(word, text.lower())

    def test_persona_from_config(self):
        text = writer.persona_text({"me": {"voice": "dry, lowercase", "interests": ["climbing", "cooking"]}})
        self.assertIn("How they text: dry, lowercase", text)
        self.assertIn("climbing, cooking", text)
        self.assertIn("not filled in", writer.persona_text({}))

    def test_tone(self):
        self.assertEqual(writer.Tone().hint(), "")
        self.assertIn("playful", writer.Tone(-0.5, 0).hint())
        self.assertIn("fully sincere", writer.Tone(1, 0).hint())
        self.assertIn("short", writer.Tone(0, -1).hint())
        self.assertIn("longer", writer.Tone(0, 1).hint())

    def test_what_claude_is_sent(self):
        blocks = writer.build_content(PROFILE, [], ["old opener"], hint="mention I cook", avoid=["shown before"],
                                      tone=writer.Tone(-1, 0))
        self.assertEqual(blocks[0], {"type": "text", "text": "Card photo_1:"})
        self.assertEqual(blocks[1]["type"], "image")
        text = blocks[-1]["text"]
        for want in ("Profile: Robin", "Age: 29", "- prompt_1 (prompt): My simple pleasures: Coffee", "old opener",
                     "shown before", "mention I cook", "fully playful"):
            self.assertIn(want, text)

    def test_reply_content(self):
        chat = Chat("Juniper", [Msg("them", "go-to spot?"), Msg("me", "dumplings")], "you liked their prompt")
        text = writer.build_reply_content(chat, "Details: 29", [], tone=writer.Tone(0, -1))[0]["text"]
        self.assertIn("them: go-to spot?", text)
        self.assertIn("user: dumplings", text)
        self.assertIn("4-10 words", text)

    def test_answers_are_kept_to_real_cards(self):
        out = writer.tidy_draft({"target_id": "photo_9", "best": 5, "options": [
            {"comment": "a", "angle": "witty", "target_id": "prompt_1"},
            {"comment": " ", "angle": "funny", "target_id": "prompt_1"},
            {"comment": "b", "angle": "cute", "target_id": "nope"}]}, PROFILE)
        self.assertEqual([o["comment"] for o in out["options"]], ["a", "b"])
        self.assertEqual(out["best"], 1)
        self.assertEqual(out["options"][1]["target_id"], "prompt_1")
        self.assertEqual(out["target_id"], "prompt_1")

    def test_backend_choice(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(writer.Writer({"backend": "auto"}).backend, "claude-cli")
            with mock.patch.object(writer, "_has_anthropic", return_value=True):
                self.assertEqual(writer.Writer({"backend": "auto", "api_key": "k"}).backend, "api")
            with mock.patch.object(writer, "_has_anthropic", return_value=False):
                self.assertEqual(writer.Writer({"backend": "api", "api_key": "k"}).backend, "claude-cli")


class History(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        home = Path(self.dir.name)
        (home / "config.json").write_text(json.dumps({"model": "m", "me": {"voice": "v"}}), encoding="utf-8")
        p = mock.patch.dict(os.environ, {"HINGE_MATCHER_HOME": str(home)})
        p.start()
        self.addCleanup(p.stop)

    def test_config_over_defaults(self):
        cfg = store.load_config()
        self.assertEqual(cfg["model"], "m")
        self.assertEqual(cfg["me"], {"voice": "v"})
        self.assertEqual(cfg["typing_cps"], store.DEFAULTS["typing_cps"])

    def test_openers_dont_repeat_across_phones(self):
        store.log("sent", comment="from the iPhone", name="A")
        store.log("inserted", comment="from Android", name="Robin", kind="prompt", card="My simple pleasures: Coffee",
                  details={"Age": "29"})
        store.log("skipped", name="B")
        self.assertEqual(store.recent_openers(), ["from the iPhone", "from Android"])
        bg = store.background("robin")
        self.assertIn("Age: 29", bg)
        self.assertIn("from Android", bg)
        self.assertEqual(store.background("nobody"), "")
        store.log("inserted_reply", text="sure, thursday?")
        self.assertEqual(store.recent_replies(), ["sure, thursday?"])


if __name__ == "__main__":
    unittest.main()
