"""Hinge Matcher for Android Desktop: a dock beside Hinge's window and pop-ups on it.

It reads the profile or chat on screen (screenshots of the window plus Windows OCR: nothing inside
Android), asks Claude which card to like and for three openers (writer.py), and shows them in a dock
glued to the window's edge and as a glow around the picked card with an idea bubble that follows the
card as you scroll. Clicking an idea, or Alt+Enter on the window, types it into Hinge's comment or
message box through the window's own keyboard path. It never presses Like, Send or Skip: you do.

Flow on Discover: the ideas come in as soon as a profile shows; tap the ♥ on the glowing card (or
any card) and the idea you pick goes into the comment box. Click an idea before opening the box and
it waits ("armed") until the box opens.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

import win32con
import win32gui

from ... import state
from ...log import log
from ...paths import LOGS
from ...panel import Panel
from ...overlay import side_panel
from ...winapi import light_theme
from ...winocr import recognize
from .. import AppHandle, Geometry, Plugin
from . import reader, store, writer
from .eyes import Eyes, ProfileModel, _similar
from .model import Chat
from .pops import Peek, Pulse, Sprite, bubble_sprite, chat_spot, glow_sprite, place_on_card
from .reader import CHAT, CHAT_PROFILE, OTHER, PROFILE, SHEET, Screen, skip_button

DOCK = Path(__file__).with_name("dock") / "index.html"
DOCK_DIP, TUCKED_DIP, GAP_DIP = 344, 46, 6
NEXT, PREV, INSERT, TOGGLE = 1, 2, 3, 4
MOD_ALT = 0x0001
# (Alt+Q: scrcpy has no Q shortcut; its Alt+H is Home, Alt+B Back.)
HOTKEYS = {NEXT: (MOD_ALT, win32con.VK_RIGHT), PREV: (MOD_ALT, win32con.VK_LEFT), INSERT: (MOD_ALT, win32con.VK_RETURN),
           TOGGLE: (MOD_ALT, ord("Q"))}
KEY_NAMES = {NEXT: "Alt+→", PREV: "Alt+←", INSERT: "Alt+Enter", TOGGLE: "Alt+Q"}
ARMED_FOR = 90  # seconds an idea waits for the comment box
SCAN_STEP = 0.42  # of the scrolling part per step: a card up to 0.58 of the screen tall is whole on one
# screen or another, and the capture loop can follow a step even in one frame (up to 0.45)
SCAN_STEPS = 40
SCAN_EVERY = 5.0  # seconds: no more often than this, whatever looks like a new profile
AUTO_DRAFTS = 3  # drafts per profile as more of it shows, before the user's own "New ideas"
REMEMBER, REMEMBER_FOR = 12, 30 * 60  # profiles whose ideas are kept, and for how long (seconds)


def debug(message: str) -> None:
    """With ANDROIDDESKTOP_PLUGIN_DEBUG set: what the scan and the reader do, step by step, in
    logs/hinge-debug.log. Never the config; names and card ids only, not what the cards say."""
    if os.environ.get("ANDROIDDESKTOP_PLUGIN_DEBUG"):
        try:
            with (LOGS / "hinge-debug.log").open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {message}\n")
        except OSError:
            pass


def numbering(cards) -> dict[str, int]:
    """Each card's number among its sort by where it is on the page (Photo 1 is the first photo), for
    labels: ids can be out of order until the whole profile has been read."""
    counts: dict[str, int] = {}
    out = {}
    for c in sorted(cards, key=lambda c: c.top):
        counts[c.kind] = counts.get(c.kind, 0) + 1
        out[c.id] = counts[c.kind]
    return out


def card_label(card, n: int | None = None) -> str:
    n = n if n is not None else card.id.rsplit("_", 1)[-1]
    if card.kind in ("prompt", "text"):
        q = card.text.split(":", 1)[0].strip()
        return f"prompt “{q[:38]}{'…' if len(q) > 38 else ''}”" if q else f"prompt {n}"
    return {"photo": f"photo {n}", "vitals": "details", "video": f"video {n}"}.get(card.kind, f"card {n}")


def short_label(card, n: int | None = None) -> str:
    """"Photo 2", "Prompt 1", "Details": for the dock's row of cards."""
    n = n if n is not None else card.id.rsplit("_", 1)[-1]
    return "Details" if card.kind == "vitals" else f"{card.kind.capitalize()} {n}"


class DockApi:
    """What the dock's JavaScript can call (pywebview runs each call on a thread of its own)."""

    def __init__(self, plugin: HingeMatcher) -> None:
        self._p = plugin

    def ready(self) -> None:
        self._p.push()

    def select(self, i: int) -> None:
        self._p.select(int(i), from_dock=True)

    def preview(self, i: int) -> None:
        self._p.set_preview(int(i))

    def go_to(self, card_id: str) -> None:
        self._p.go_to(str(card_id))

    def insert(self, i: int, text: str) -> None:
        self._p.insert(int(i), str(text))

    def regenerate(self, hint: str = "") -> None:
        self._p.regenerate(str(hint or ""))

    def tone(self, warmth: float, length: float) -> None:
        self._p.set_tone(float(warmth), float(length))

    def skip(self) -> None:
        self._p.skip()

    def pause(self, paused: bool) -> None:
        self._p.set_paused(bool(paused))

    def bubble(self, show: bool) -> None:
        self._p.set_bubble(bool(show))

    def tuck(self, tucked: bool) -> None:
        self._p.set_tucked(bool(tucked))

    def focus_app(self) -> None:
        self._p.app.focus()


class HingeMatcher(Plugin):
    name = "hinge"
    title = "Hinge Matcher"
    packages = frozenset({"co.hinge.app"})

    def __init__(self, app: AppHandle) -> None:
        super().__init__(app)
        self.cfg = store.load_config()  # never logged: it may hold an API key
        # Ideas within a few seconds: low effort unless android_effort says otherwise (the shared
        # config's "effort" is the iPhone version's, which writes while it scrolls the profile itself).
        self.writer = writer.Writer({**self.cfg, "effort": self.cfg.get("android_effort") or "low"})
        self.lock = threading.RLock()
        self.mode = "idle"  # profile | chat | idle
        self.screen: Screen | None = None
        self.model = ProfileModel()
        self.profile_gen = 0
        self.chat: Chat | None = None
        self.backgrounds: dict[str, str] = {}  # chat name -> their profile, read on the chat's Profile tab
        self.draft: dict | None = None
        self.reply: dict | None = None
        self.drafted_at = -1  # model.grew when the draft was asked for
        self.drafted_kinds = (0, 0)  # (text cards, photos) it was asked for with
        self.auto_drafts = 0
        self.job = 0
        self.drafting = False
        self.error = ""
        self.current = 0
        self.preview = -1
        self.tone = writer.Tone()
        self.hint = ""
        self.avoid: list[str] = []
        self.touched = False  # the user picked or previewed an idea: don't swap the ideas under them
        self.inserted: int | None = None
        self.armed: tuple[int, str, float] | None = None
        self.skip_armed = False
        self.typing: int | None = None
        self.message, self.message_kind = "", ""
        self.boxes: dict[str, tuple[int, int, int, int]] = {}  # screen kind -> its text box, once seen empty
        self.paused = False
        self.tucked = False
        self.scanning = False  # scrolling through the profile to read all of it
        self.returning = False  # ...on its way back to the top
        self.going: str | None = None  # the card it's scrolling Hinge to (clicked in the dock)
        self.couldnt_drag = False
        self.go_lock = threading.Lock()
        self.scan_gen = 0  # a new profile, or the same one laid out again (a resize): a scan of its own
        self.scan_tries = 0  # scans cut short on this layout, to try again
        self.size: tuple[int, int] | None = None  # the view size the profile was read at
        self.resized_at = 0.0
        self.last_profile_at = 0.0  # when the profile was last on screen
        self.draft_whole = False  # the draft was written from the whole profile: no need for another
        self.memory: dict[str, dict] = {}  # name -> a profile's ideas, for when it shows again
        self.recalled = -1  # the profile_gen whose ideas came from memory
        self.scanned_at = 0.0
        self.scan_again = -1  # the scan_gen whose scan was cut short by the app being away, to finish
        self.jump_wait = 1.5  # seconds a fling may take to get going
        self.still = False
        self.scrolled = threading.Event()
        self.read_done = threading.Event()
        self.hotkeys_on = False
        self.geo: Geometry | None = None
        self.dock: Panel | None = None
        self.glow: Sprite | None = None
        self.bubble: Sprite | None = None
        self.pulse: Pulse | None = None
        self.peek: Peek | None = None
        self.show_bubble = bool(state.get("hinge_bubble", True))
        self._sprite_keys: dict[str, object] = {}
        self.eyes = Eyes(self.app.screenshot, self._on_scroll, self._on_screen, self._on_moving)

    # ------------------------------------------------------------------ lifecycle (window thread)
    def start(self) -> None:
        self.glow = Sprite(self.app.hwnd)
        self.bubble = Sprite(self.app.hwnd)
        self.pulse = Pulse(self.glow)
        self.peek = Peek(self.bubble)
        try:
            self.dock = self.app.panel(title="Hinge Matcher", url=DOCK, api=DockApi(self),
                                       size=(DOCK_DIP, 700), light=light_theme())
        except Exception as e:
            log(f"hinge: no dock ({e})")
        self.eyes.start()
        threading.Thread(target=self.writer.prewarm, daemon=True).start()
        log(f"hinge: watching {self.app.package} ({self.writer.backend})")
        self.foreground_changed(win32gui.GetForegroundWindow())

    def stop(self) -> None:
        self.eyes.stop.set()
        self.eyes.poke()
        self._hotkeys(False)
        if self.pulse:
            self.pulse.stop()
        for s in (self.glow, self.bubble):
            if s:
                s.destroy()
        if self.dock:
            self.dock.destroy()
        writer.close()

    def app_lost(self) -> None:
        self.eyes.paused = True
        self._hide_pops()
        if self.dock:
            self.dock.place(None, False)

    def app_back(self) -> None:
        self.eyes.paused = self.paused
        self.eyes.poke()

    def geometry_changed(self, geo: Geometry) -> None:
        self.geo = geo
        if self.dock:
            width = round((TUCKED_DIP if self.tucked else DOCK_DIP) * geo.scale)
            rect, _ = side_panel(geo.frame, geo.work, width, round(GAP_DIP * geo.scale), geo.maximized)
            self.dock.place(rect, geo.visible)
        self.render_pops()

    def foreground_changed(self, hwnd: int) -> None:
        mine = hwnd and (hwnd == self.app.hwnd or (self.dock is not None and hwnd == self.dock.hwnd))
        self._hotkeys(bool(mine) and not self.paused)

    def _hotkeys(self, on: bool) -> None:
        """Alt+arrows and Alt+Enter belong to the plugin only while Hinge or its dock is in front."""
        if on and not self.hotkeys_on:
            failed = self.app.hotkeys(self, HOTKEYS)
            if failed:
                log(f"hinge: hotkeys taken by another program: {[KEY_NAMES[k] for k in failed]}")
            self.hotkeys_on = True
        elif not on and self.hotkeys_on:
            self.app.drop_hotkeys(self)
            self.hotkeys_on = False

    def hotkey(self, key_id: int) -> None:
        if key_id == TOGGLE:
            self.set_bubble(not self.show_bubble)
            return
        opts = self.options()
        if not opts:
            return
        if key_id in (NEXT, PREV):
            self.select((self.current + (1 if key_id == NEXT else -1)) % len(opts))
        elif key_id == INSERT:
            i = min(self.current, len(opts) - 1)
            self.insert(i, self.option_text(opts[i]))

    # ------------------------------------------------------------------ what the eyes see (eyes thread)
    def _on_moving(self) -> None:
        pass

    def _on_scroll(self, dy: int) -> None:
        with self.lock:
            if self.mode == "profile" and self.screen is not None and self.screen.kind == PROFILE:
                self.model.offset += dy
        self.scrolled.set()
        self.app.call(self.render_pops)

    def _on_screen(self, scr: Screen, im, jumped: bool) -> None:
        debug(f"screen {scr.kind} name={scr.name!r} header={scr.header_name!r} band={scr.band} jumped={jumped} "
              f"cards={[(c.kind, c.rect[1], c.rect[3], c.clipped_top, c.clipped_bottom) for c in scr.cards]}")
        start_draft = start_reply = start_scan = False
        insert_now = None
        with self.lock:
            prev_kind = self.screen.kind if self.screen else None
            self.screen = scr
            if scr.box:
                self.boxes[scr.kind] = scr.box
            if scr.kind == PROFILE:
                now = time.monotonic()
                self.last_profile_at = now
                fresh = self.mode != "profile" and prev_kind not in (SHEET, PROFILE)
                # A new size: the window was resized (or moved to a screen of another scale), and Hinge
                # laid the same profile out again.
                old_size, self.size = self.size, scr.size
                resized = not fresh and old_size is not None and scr.size != old_size
                if resized and self.model.profile.cards:
                    self._relayout(scr, im, old_size)
                    if not self.model.complete:
                        start_scan = self._want_scan(scr, now, every=0)
                elif self.returning and not fresh:
                    pass
                elif fresh or not self.model.observe(scr, im, jumped and not (self.scanning or self.going),
                                                     measured=self.scanning):
                    self._new_profile()
                    self.model.observe(scr, im)
                    start_scan = self._want_scan(scr, now)
                self.mode = "profile"
                self.skip_armed = self.skip_armed and not jumped
                if not self.scanning and self.scan_again == self.scan_gen and not self.paused \
                        and now - self.scanned_at > 2:
                    self.scan_again = -1
                    self.scanning = start_scan = True
                    self.scanned_at = now
                self._recall()
                if self._content() and (self.draft is None and not self.drafting):
                    start_draft = True  # from what's on screen; a scan follows with all of it
                elif (not self.scanning and not self.model.complete and self.draft is not None
                      and not self.draft_whole and not self.drafting and not self.touched
                      and self.inserted is None and self.model.grew > self.drafted_at and self._grown_enough()):
                    start_draft = True
            elif scr.kind == SHEET:
                if self.mode != "profile":
                    self.mode = "profile"
                if self.armed and time.monotonic() - self.armed[2] < ARMED_FOR and self.typing is None:
                    insert_now = self.armed[:2]
                    self.armed = None
            elif scr.kind in (CHAT, CHAT_PROFILE):
                if self.mode != "chat":
                    self.reply, self.avoid, self.hint, self.inserted = None, [], "", None
                self.mode = "chat"
                if scr.kind == CHAT and scr.chat is not None:
                    new_chat = self.chat is None or scr.chat.name != self.chat.name
                    if new_chat:
                        self.reply, self.avoid, self.hint, self.inserted, self.message = None, [], "", None, ""
                        self.chat = scr.chat
                        start_reply = self.typing is None
                    elif self._moved_on(self.chat, scr.chat):
                        self.chat = scr.chat
                        self.avoid, self.hint, self.inserted, self.message = [], "", None, ""
                        if scr.chat.messages[-1].who == "me":  # the user sent it: wait for them
                            self.reply = None
                            self.job += 1
                            self.drafting = False
                            self.message, self.message_kind = "Sent. Reply ideas come once they answer.", ""
                        elif self.typing is None:
                            start_reply = True
                elif scr.kind == CHAT_PROFILE and scr.name:
                    texts = [c.text for c in scr.cards if c.text]
                    if texts:
                        known = self.backgrounds.get(scr.name, "")
                        merged = known.split("\n") if known else []
                        merged += [t for t in texts if t not in merged]
                        self.backgrounds[scr.name] = "\n".join(merged[-12:])
        self.read_done.set()
        if start_draft:
            self.request_draft()
        if start_scan:
            threading.Thread(target=self._scan, args=(self.scan_gen,), daemon=True, name="hinge-scan").start()
        if start_reply:
            self.request_reply()
        if insert_now:
            self.insert(*insert_now)
        self.push()
        self.app.call(self.render_pops)

    def _content(self) -> bool:
        return any(c.text or c.image for c in self.model.profile.cards)

    def _kinds(self) -> tuple[int, int]:
        cards = self.model.profile.cards
        return (sum(1 for c in cards if c.kind != "photo" and c.text), sum(1 for c in cards if c.image))

    def _grown_enough(self) -> bool:
        """Re-draft as more of the profile comes in (a new prompt, or two more photos), a few times per
        profile, until the user starts using the ideas."""
        texts, photos = self._kinds()
        was_texts, was_photos = self.drafted_kinds
        return self.auto_drafts < AUTO_DRAFTS and (texts > was_texts or photos >= was_photos + 2)

    @staticmethod
    def _moved_on(old: Chat, new: Chat) -> bool:
        """A new message at the bottom of the chat (not just scrolling up through it)."""
        if not new.messages:
            return False
        last = new.messages[-1]
        return not any(m.who == last.who and m.text == last.text for m in old.messages)

    def _new_profile(self) -> None:
        self._remember()
        self.model = ProfileModel()
        self.profile_gen += 1
        self.scan_gen += 1  # a scan of the last one stops
        self.scanning, self.scan_tries = False, 0
        self.draft, self.avoid, self.hint, self.draft_whole = None, [], "", False
        self.inserted, self.armed, self.skip_armed, self.touched = None, None, False, False
        self.current, self.preview, self.drafted_at = 0, -1, -1
        self.drafted_kinds, self.auto_drafts = (0, 0), 0
        self.message = ""
        self.job += 1  # a draft for the last profile is no use now
        self.drafting = False

    def _relayout(self, scr: Screen, im, old: tuple[int, int]) -> None:
        """The same profile, laid out again at a new size: all that's been read, and the ideas, stay as
        they are. Where its cards are now is scaled to the new width and put right as each shows (a
        profile not read through yet is read again, once the size has settled)."""
        self.model.relayout(old, scr.size)
        self.model.observe(scr, im)
        self.scan_gen += 1  # a scan under way was measuring the old layout: it stops
        self.scanning, self.scan_tries = False, 0
        self.resized_at = time.monotonic()
        self.boxes.clear()

    def _renamed(self, changes: dict[str, str], draft: dict | None) -> None:
        """Cards given new ids (renumber()): the draft's targets follow."""
        if changes and draft:
            for d in [draft, *(draft.get("options") or [])]:
                if d.get("target_id") in changes:
                    d["target_id"] = changes[d["target_id"]]

    def _want_scan(self, scr: Screen, now: float, every: float = SCAN_EVERY) -> bool:
        """Start reading the whole profile, if that's on and it's been long enough since the last time."""
        if self.cfg.get("read_whole_profile", True) and not self.paused and scr.cards and now - self.scanned_at > every:
            self.scanning = True
            self.scanned_at = now
            return True
        return False

    def _remember(self) -> None:
        """Keep the profile's ideas, for when it shows again (back from a chat, the window reopened):
        by name, the last few, for a while."""
        name = self.model.profile.name
        if name and self.draft:
            self.memory.pop(name, None)
            self.memory[name] = {
                "at": time.monotonic(), "texts": self._texts(), "draft": self.draft, "avoid": list(self.avoid),
                "hint": self.hint, "current": self.current, "inserted": self.inserted, "touched": self.touched,
                "whole": self.draft_whole}
            while len(self.memory) > REMEMBER:
                self.memory.pop(next(iter(self.memory)))

    def _texts(self) -> list[str]:
        """What the profile's text cards say, those read whole."""
        return [c.text for c in self.model.profile.cards if c.kind != "photo" and c.text and not c.partial]

    def _recall(self) -> None:
        """A profile seen a little while ago is back: its ideas, not new ones. Dropped again if its cards
        turn out to say something else (somebody else of the same name)."""
        name = self.model.profile.name
        mem = self.memory.get(name) if name else None
        if mem is None or time.monotonic() - mem["at"] > REMEMBER_FOR:
            return
        known = mem["texts"]
        other = any(max((_similar(t, k) for k in known), default=1.0) < 0.75 for t in self._texts())
        if self.recalled == self.profile_gen:
            if other:  # not them after all: ideas of their own
                self.memory.pop(name, None)
                self.recalled = -1
                self.draft, self.avoid, self.hint, self.draft_whole = None, [], "", False
                self.current, self.inserted, self.touched, self.auto_drafts = 0, None, False, 0
            return
        if other or self.touched or self.inserted is not None or self.armed is not None:
            return
        self.draft, self.avoid, self.hint = mem["draft"], list(mem["avoid"]), mem["hint"]
        self.current, self.inserted, self.touched = mem["current"], mem["inserted"], mem["touched"]
        self.draft_whole = mem["whole"]
        self.job += 1  # whatever was being written for it isn't needed
        self.drafting = False
        self.drafted_at, self.auto_drafts = self.model.grew, AUTO_DRAFTS
        self.recalled = self.profile_gen

    # ------------------------------------------------------------------ reading the whole profile (worker thread)
    def _scan_on(self, gen: int) -> bool:
        """Whether the scan should go on: the same profile and layout, still on screen (a passing frame
        of something else doesn't count), nobody else at it."""
        scr = self.screen
        if not (self.scanning and gen == self.scan_gen and not self.paused and scr is not None
                and self.app.geometry().visible):
            return False
        return scr.kind == PROFILE or (scr.kind == OTHER and time.monotonic() - self.last_profile_at < 2.0)

    def _band(self) -> tuple[int, int]:
        h = self.app.geometry().size[1]
        return self.screen.band if self.screen and self.screen.band != (0, 0) else (round(h * 0.08), round(h * 0.92))

    def _finger(self) -> tuple[int, int, int]:
        """Where a finger goes for the scan: in the page's left margin, beside the cards, where nothing
        takes a touch (a video, a heart, the X), and how tall the scrolling part is."""
        w = self.app.geometry().size[0]
        top, bottom = self._band()
        cards = self.screen.cards if self.screen else []
        x = max(2, cards[0].rect[0] // 2) if cards and cards[0].rect[0] >= 6 else round(w * 0.025)
        return x, top, bottom - top

    def _step(self, gen: int, read: bool) -> int | None:
        """Drag the page up a little less than half a screen, and (with read) read the screen there.
        Returns how far the page went down (view pixels, measured on the pictures before and after),
        None if the scan should stop. Sets self.still to whether nothing moved (the end)."""
        if not self._scan_on(gen):
            return None
        t0 = time.monotonic()
        moved = self._drag(round(SCAN_STEP * self._finger()[2]), lambda: self._scan_on(gen))
        if moved is None:
            if self.couldnt_drag:
                self.scan_again = gen  # couldn't (a secure screen had the app): try again once it's back
            return None
        t2 = time.monotonic()
        if read:
            self.read_done.clear()
            self.eyes.read_now()
            self.read_done.wait(2.0)
        t3 = time.monotonic()
        self.timings.append((t2 - t0, t3 - t2))
        debug(f"step: moved {moved} (offset {self.model.offset}), drag+settle/read {t2 - t0:.2f}/{t3 - t2:.2f}, "
              f"cards {[c.id for c in self.model.profile.cards]}")
        return moved if self._scan_on(gen) else None

    def _more_below(self) -> bool:
        """Whether the profile goes on below the screen: its last card on screen is cut off at the
        bottom (at the end, it stops short of the bottom with page under it)."""
        scr = self.screen
        return scr is not None and scr.kind == PROFILE and bool(scr.cards) and scr.cards[-1].clipped_bottom

    def _drag(self, want: int, alive: Callable[[], bool]) -> int | None:
        """Drag the page `want` view pixels on (down the profile; negative: back up), with a finger in its
        margin, and measure how far it went on the pictures before and after. Returns that, None if it
        couldn't (self.couldnt_drag) or alive() says to stop. Sets self.still to whether nothing moved."""
        self.couldnt_drag = False
        x, top, span = self._finger()
        shot = self.app.screenshot()
        before = self.model.offset
        y = top + round(span * (0.78 if want > 0 else 0.22))  # room for the finger to go the whole way
        debug(f"touch: drag at ({x}, {y}) by {-want}, screen {self.screen.kind if self.screen else None}")
        if shot is None or not self.app.drag(x, y, -want):
            self.couldnt_drag = True
            return None
        after = self._moved_picture(alive, shot, want)
        if after is None or not alive():
            return None
        band = self._band()
        self.still = not reader.changed(reader.thumb(shot), reader.thumb(after), None, after.height)
        moved = 0
        if not self.still:
            # A drag moves the page at most its own length.
            search = (0, want + 12) if want > 0 else (want - 12, 0)
            dy = reader.scroll_shift(reader.thumb(shot), reader.thumb(after), band, after.height, max_frac=0.6,
                                     search=search)
            if dy is None:
                moved = self.model.offset - before  # what the frame-by-frame count saw
            else:
                moved = reader.refine_shift(shot, after, band, dy, reach=8)
            with self.lock:
                self.model.offset = before + moved
        return moved

    # ------------------------------------------------------------------ going to a card (worker thread)
    def go_to(self, card_id: str) -> None:
        """Scroll Hinge to one of the profile's cards, clicked in the dock: its top just under the top of
        the screen (or as far as the page goes). Only ever scrolls, as the scan does."""
        debug(f"dock: go to {card_id}, mode {self.mode}, screen {self.screen.kind if self.screen else None}")
        with self.lock:
            card = self.model.profile.card(card_id)
            if self.mode != "profile" or card is None or self.screen is None or self.screen.kind != PROFILE:
                return
            why = "Still reading the profile: one moment" if self.scanning else \
                "Paused: press play to go to it" if self.paused else ""
            if not why:
                self.going = card_id
                gen = self.profile_gen
        if why:
            if self.dock:
                self.dock.call("ui.toast", why)
            return
        threading.Thread(target=self._go, args=(gen, card_id), daemon=True, name="hinge-go").start()

    def _go(self, gen: int, card_id: str) -> None:
        """Steered by what's on screen after each move (which of the profile's cards are there, and
        where), not by the map of the page alone, which can be off: the first card is the top of the
        profile; a card on screen is moved to just under the top; one that isn't, a step its way."""
        def alive() -> bool:  # the same profile on screen, nobody else scrolling it, no newer click
            scr = self.screen
            return (self.going == card_id and gen == self.profile_gen and not self.scanning and not self.paused
                    and scr is not None and scr.kind == PROFILE and self.app.geometry().visible)
        with self.go_lock:  # one at a time: a newer click waits for this one to see it's not wanted
            try:
                s = self.app.geometry().scale
                slop = round(8 * s)  # Android lets a finger move this far before the page follows it
                way = turns = stalls = 0
                for _ in range(16):
                    if not alive():
                        return
                    band = self._band()
                    limit = round(SCAN_STEP * (band[1] - band[0]))
                    with self.lock:
                        order = {c.id: i for i, c in enumerate(self.model.profile.cards)}
                        here = dict(self.model.on_screen)
                        target = self.model.profile.card(card_id)
                    if target is None:
                        return
                    if order[card_id] == 0:  # the first: the top of the profile, with the name
                        self._to_top(alive)
                        return
                    rect = here.get(card_id)
                    if rect is not None:
                        t, b = rect[1], rect[3]
                        cut = t <= band[0] + 3  # its top is above the screen
                        if cut and target.partial:
                            want = -round(limit * 0.6)  # by how much isn't known: a step up
                        else:
                            want = round((b - target.height if cut else t) - band[0] - 10 * s)
                        if abs(want) <= round(14 * s):
                            return  # there
                    else:
                        seen = [order[i] for i in here if i in order]
                        if not seen:
                            return  # nothing known on screen to steer by
                        want = limit if order[card_id] > max(seen) else -limit
                    turn = 1 if want > 0 else -1
                    turns += way != 0 and turn != way
                    if turns > 2:
                        return  # back and forth: as near as it gets
                    way = turn
                    drag = max(-limit, min(limit, want + turn * slop))
                    moved = self._drag(drag, alive)
                    if moved is None:
                        return
                    stalls = stalls + 1 if self.still or abs(moved) < 4 else 0
                    if stalls >= 2:
                        return  # the end of the page
                    if not self.still and abs(drag) - abs(moved) > 0 and abs(moved) < abs(want):
                        slop = min(round(40 * s), abs(drag) - abs(moved))
                    self.read_done.clear()  # a fresh look at what's where now
                    self.eyes.read_now()
                    self.read_done.wait(2.0)
            finally:
                with self.lock:
                    if self.going == card_id:
                        self.going = None
                self.app.call(self.render_pops)

    def _shift(self, a, b, h: int, search: tuple[int, int] | None = None) -> int | None:
        """How far the page moved between two thumbnails (0: it didn't; a video playing on it doesn't
        count), None if the picture changed some other way."""
        return reader.scroll_shift(a, b, self._band(), h, max_frac=0.95, search=search)

    def _until_still(self, alive: Callable[[], bool], shot):
        """Screenshots until two in a row show the page in the same place (a video may play on): the last
        one, None if alive() says to stop."""
        cur = reader.thumb(shot)
        end = time.monotonic() + 3
        while time.monotonic() < end:
            time.sleep(0.1)
            nxt_shot = self.app.screenshot()
            if nxt_shot is None or not alive():
                return None
            nxt = reader.thumb(nxt_shot)
            if self._shift(cur, nxt, nxt_shot.height) == 0:
                return nxt_shot
            shot, cur = nxt_shot, nxt
        return shot

    def _moved_picture(self, alive: Callable[[], bool], before, step: int):
        """The screen once the page has moved (up to step; negative: up) and come to rest, or as it is if
        it didn't move within the wait: Hinge can take half a second to act on a touch while a video
        plays."""
        first = reader.thumb(before)
        search = (3, step + 12) if step > 0 else (step - 12, -3)
        shot, end = before, time.monotonic() + self.jump_wait
        while time.monotonic() < end:
            time.sleep(0.05)
            shot = self.app.screenshot()
            if shot is None or not alive():
                return None
            if self._shift(first, reader.thumb(shot), shot.height, search=search):
                return self._until_still(alive, shot)
        return shot

    def _to_top(self, alive: Callable[[], bool]) -> bool | None:
        """Fling the page back to the top, until the filter chips show (they're only there at the top).
        Returns whether it had to move, None if it couldn't (self.couldnt_drag) or alive() says to stop."""
        self.couldnt_drag = False
        for flings in range(14):
            if not alive():
                return None
            shot = self.app.screenshot()
            if shot is None:
                return None
            if reader.at_top(shot, recognize):
                return flings > 0
            x, top, span = self._finger()
            debug(f"touch: fling at ({x}, {top + round(span * 0.25)}) by {round(span * 0.5)}")
            if not self.app.fling(x, top + round(span * 0.25), round(span * 0.5)):
                self.couldnt_drag = True
                return None
            time.sleep(min(0.6, self.jump_wait))  # it takes a moment to start, then coasts
            if self._until_still(alive, self.app.screenshot() or shot) is None:
                return None
        return None  # never got there

    def _settle(self) -> None:
        """Wait until the page has been still for a moment (the frame-by-frame count goes quiet)."""
        for _ in range(10):
            self.scrolled.clear()
            if not self.scrolled.wait(0.15):
                break

    def _scan(self, gen: int) -> None:
        """Read the whole profile: to the top (if not there already), down to the end a little under
        half a screen at a time reading each one, then back to the top. Once it's all read, a draft
        from all of it. Only ever scrolls the page, with a finger on its left side. Stops if the
        profile goes away, the like sheet opens, or something else moves the page."""
        t0 = time.monotonic()
        self.timings = []
        self.eyes.hurry = True
        self.push()
        self.app.call(self.render_pops)
        reason = "done"
        read_all = False
        pgen = self.profile_gen
        alive = lambda: self._scan_on(gen)  # noqa: E731
        try:
            # 0. Being resized: once the size has settled (each new size starts a scan of its own).
            while time.monotonic() - self.resized_at < 1.0:
                if not alive():
                    reason = "stopped"
                    return
                time.sleep(0.2)
            # 1. To the top. If it wasn't there, start over from the top, so the cards come in order.
            moved_up = self._to_top(alive)
            if moved_up is None:
                if self.couldnt_drag:
                    self.scan_again = gen  # a secure screen had the app: again once it's back
                reason = "stopped"
                return
            with self.lock:
                if moved_up:
                    old = self.model
                    self.model = ProfileModel()
                    self.model.profile.name, self.model.names = old.profile.name, dict(old.names)
                    self.model.profile.details = dict(old.profile.details)
                self.model.offset = 0
            self.eyes.calm()
            self.read_done.clear()
            self.eyes.read_now()
            self.read_done.wait(2.0)
            # 2. Down to the end, reading each screen.
            stalls = retries = 0
            for _ in range(SCAN_STEPS):
                moved = self._step(gen, read=True)
                if moved is None:
                    reason = "stopped"
                    return
                if moved < -40:
                    reason = "moved the wrong way"
                    return
                stalls = stalls + 1 if self.still or abs(moved) < 20 else 0  # (a video at the end keeps changing)
                if stalls >= 2:
                    if not self._more_below():
                        break
                    # A card cut off at the bottom: not the end, the drags just didn't take (an
                    # emulator too busy to keep up with them). Again, after a moment.
                    if retries >= 2:
                        reason = "the page wouldn't move"
                        return  # not all read: what's seen as the user scrolls still counts
                    retries, stalls = retries + 1, 0
                    time.sleep(1.0)
            read_all = True
            with self.lock:
                self.model.complete = True  # from here on, scrolling only moves the pop-ups
                # Numbered as on the page now. (A draft from all of it, or one remembered from such a
                # draft, was written with those numbers already.)
                self._renamed(self.model.renumber(), None if self.draft_whole else self.draft)
                self.scanning = False  # all read: write the draft while it goes back up
                # (Not if the ideas were written from all of it already: the same profile, laid out
                # again at another size, or back again.)
                redo = (not self.touched and self.inserted is None and self._content() and not self.draft_whole
                        and pgen == self.profile_gen)
            if redo:
                self.request_draft()
            with self.lock:
                self.scanning = gen == self.scan_gen
            # 3. Back to the top. Nothing is read on the way: the page flies past faster than it can
            #    be followed, and it's all read already.
            self.returning = True
            try:
                self._to_top(alive)
            finally:
                with self.lock:
                    self.returning = False
                    self.model.offset = 0
            self.eyes.calm()
        except Exception as e:
            reason = f"failed: {e!r}"
        finally:
            self.eyes.hurry = False
            with self.lock:
                if gen == self.scan_gen:
                    self.scanning = False
                want_draft = (not read_all and pgen == self.profile_gen and self.draft is None and not self.drafting
                              and self._content())
                # Cut short by something passing (not the user moving the page): once more, in a moment.
                again = (not read_all and reason == "stopped" and gen == self.scan_gen and not self.paused
                         and self.scan_again != gen and self.scan_tries < 2)
                if again:
                    self.scan_tries += 1
            steps = self.timings
            avg = [sum(x) / len(steps) for x in zip(*steps)] if steps else []
            log(f"hinge: read the profile in {time.monotonic() - t0:.1f}s ({len(self.model.profile.cards)} cards, "
                f"{reason}; {len(steps)} steps, avg drag+settle/read " + "/".join(f"{x:.2f}" for x in avg) + ")")
            if want_draft:
                self.request_draft()
            if again:
                threading.Thread(target=self._scan_later, args=(gen,), daemon=True, name="hinge-scan").start()
            self.push()
            self.app.call(self.render_pops)

    def _scan_later(self, gen: int, wait: float = 2.0) -> None:
        """The scan again, after a moment, if the profile is on screen then and nothing's changed."""
        time.sleep(wait)
        with self.lock:
            scr = self.screen
            if self.scanning or gen != self.scan_gen or self.paused or scr is None or scr.kind != PROFILE:
                return
            self.scanning = True
            self.scanned_at = time.monotonic()
        self._scan(gen)

    # ------------------------------------------------------------------ drafting (worker threads)
    def request_draft(self, auto: bool = True) -> None:
        with self.lock:
            self.job += 1
            job, gen = self.job, self.profile_gen
            self.drafting = True
            self.error = ""
            self.drafted_at = self.model.grew
            self.drafted_kinds = self._kinds()
            self.auto_drafts += 1 if auto else 0
            prof = self.model.profile
            snapshot = type(prof)(prof.name, dict(prof.details), [type(c)(**vars(c)) for c in prof.cards])
            screens = list(self.model.screens)
            hint, avoid, tone = self.hint, list(self.avoid), writer.Tone(self.tone.warmth, self.tone.length)
            whole = self.model.complete
            ids = (self.model, self.model.renamed)  # the ids it's written with
        self.push()
        threading.Thread(target=self._draft, args=(job, gen, snapshot, screens, hint, avoid, tone, whole, ids),
                         daemon=True, name="hinge-draft").start()

    def _draft(self, job, gen, prof, screens, hint, avoid, tone, whole=False, ids=None) -> None:
        t = time.monotonic()
        try:
            out = self.writer.draft(prof, screens, store.recent_openers(), hint, avoid, tone)
            err = ""
        except Exception as e:
            out, err = None, str(e)
            log(f"hinge: draft failed: {e!r}")
        with self.lock:
            if job != self.job or gen != self.profile_gen:
                return  # a newer request, or another profile
            self.drafting = False
            if out is not None:
                if ids and ids[0] is self.model and ids[1] != self.model.renamed:
                    self._renamed(self.model.last_renames, out)  # renumbered while it was being written
                self.draft = out
                self.draft_whole = whole
                self.current = out.get("best", 0)
                self.preview = -1
                self.error = ""
                log(f"hinge: {len(out.get('options') or [])} openers in {time.monotonic() - t:.1f}s "
                    f"({len(prof.cards)} cards)")
            else:
                self.error = err
        self.push()
        self.app.call(self.render_pops)

    def request_reply(self) -> None:
        with self.lock:
            if self.chat is None:
                return
            self.job += 1
            job = self.job
            chat = self.chat
            self.drafting = True
            self.error = ""
            hint, avoid, tone = self.hint, list(self.avoid), writer.Tone(self.tone.warmth, self.tone.length)
            background = self.backgrounds.get(chat.name, "") or store.background(chat.name)
        self.push()
        threading.Thread(target=self._reply, args=(job, chat, background, hint, avoid, tone), daemon=True,
                         name="hinge-reply").start()

    def _reply(self, job, chat, background, hint, avoid, tone) -> None:
        t = time.monotonic()
        try:
            out = self.writer.reply(chat, background, store.recent_replies(), hint, avoid, tone)
            err = ""
        except Exception as e:
            out, err = None, str(e)
            log(f"hinge: reply draft failed: {e!r}")
        with self.lock:
            if job != self.job or self.mode != "chat" or self.chat is None or self.chat.name != chat.name:
                return
            self.drafting = False
            if out is not None:
                self.reply = out
                self.current = out.get("best", 0)
                self.preview = -1
                self.inserted = None
                log(f"hinge: {len(out.get('options') or [])} replies in {time.monotonic() - t:.1f}s")
            else:
                self.error = err
        self.push()
        self.app.call(self.render_pops)

    def regenerate(self, hint: str) -> None:
        with self.lock:
            self.hint = hint
            self.avoid = (self.avoid + [self.option_text(o) for o in self.options()])[-12:]
            self.touched = False
            mode = self.mode
        if mode == "profile":
            self.request_draft(auto=False)
        elif mode == "chat":
            self.request_reply()

    def set_tone(self, warmth: float, length: float) -> None:
        with self.lock:
            self.tone = writer.Tone(max(-1.0, min(1.0, warmth)), max(-1.0, min(1.0, length)))
            hint = self.hint
        self.regenerate(hint)

    # ------------------------------------------------------------------ ideas
    def options(self) -> list[dict]:
        with self.lock:
            src = self.draft if self.mode == "profile" else self.reply if self.mode == "chat" else None
            return list((src or {}).get("options") or [])

    @staticmethod
    def option_text(o: dict) -> str:
        return (o.get("comment") or o.get("text") or "").strip()

    def select(self, i: int, from_dock: bool = False) -> None:
        with self.lock:
            n = len(self.options())
            if not n:
                return
            self.current = i % n
            self.touched = True
            current = self.current
        if self.dock and not from_dock:
            self.dock.call("ui.current", current)
        self.app.call(self.render_pops)

    def set_preview(self, i: int) -> None:
        with self.lock:
            self.preview = i
            if i >= 0:
                self.touched = True
        self.app.call(self.render_pops)

    def skip(self) -> None:
        with self.lock:
            if self.mode != "profile":
                return
            self.skip_armed = True
            prof = self.model.profile
            store.log("skipped", name=prof.name, details=prof.details,
                      reason=(self.draft or {}).get("skip_reason", "") or "the user skipped")
            self.message, self.message_kind = "Tap ✕ in Hinge to pass on them.", ""
        self.push()
        self.app.call(self.render_pops)

    def set_bubble(self, show: bool) -> None:
        """Show or hide the idea bubble on Hinge (the glow stays): the dock's eye, or Alt+Q. Kept."""
        self.show_bubble = show
        state.put("hinge_bubble", show)
        if self.dock:
            self.dock.call("ui.toast", f"Ideas on Hinge {'shown' if show else 'hidden'}: {KEY_NAMES[TOGGLE]} to "
                                       f"{'hide' if show else 'show'} them")
        self.push()
        self.app.call(self.render_pops)

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.eyes.paused = paused
        if not paused:
            self.eyes.poke()
        self.app.call(lambda: (self.render_pops(), self.foreground_changed(win32gui.GetForegroundWindow())))
        self.push()

    def set_tucked(self, tucked: bool) -> None:
        self.tucked = tucked
        self.push()
        self.app.call(lambda: self.geo and self.geometry_changed(self.app.geometry()))

    # ------------------------------------------------------------------ filling in (worker thread)
    def insert(self, i: int, text: str) -> None:
        text = " ".join(text.split())
        debug(f"insert {i}, mode {self.mode}, screen {self.screen.kind if self.screen else None}")
        if not text:
            return
        with self.lock:
            if self.typing is not None:
                return
            scr = self.screen
            mode = self.mode
            if mode == "profile" and (scr is None or scr.kind != SHEET):
                self.armed = (i, text, time.monotonic())
                self.current = i
                self.message, self.message_kind = ("Tap ♥ on the glowing card in Hinge: this goes into the "
                                                   "comment box as soon as it opens."), ""
                self.push()
                self.app.call(self.render_pops)
                return
            if mode == "chat" and (scr is None or scr.kind != CHAT):
                self.message, self.message_kind = "Open the Chat tab in Hinge to fill in a reply.", ""
                self.push()
                return
            box = scr.box or self.boxes.get(scr.kind)
            if box is None:
                self.message, self.message_kind = "Can't find Hinge's text box on screen.", "error"
                self.push()
                return
            self.typing, self.current, self.armed = i, i, None
            self.message = ""
            replace = scr.box is None  # no placeholder: there's text in it (an idea filled in before)
            send = scr.send
        self.push()
        threading.Thread(target=self._type, args=(i, text, box, send, replace, mode), daemon=True,
                         name="hinge-type").start()

    def _type(self, i: int, text: str, box, send, replace: bool, mode: str) -> None:
        ok = False
        try:
            kb = self.app.keyboard(cps=float(self.cfg.get("typing_cps") or 12))
            if kb is None:
                raise RuntimeError("Hinge isn't showing")
            l, t, r, b = box
            x, y = (l + r) // 2, (t + b) // 2
            if send and send[0] <= x <= send[2] and send[1] <= y <= send[3]:
                raise RuntimeError("the text box overlaps the Send button")  # never press it
            self._tap(x, y)  # the text field itself: puts the cursor in it
            time.sleep(0.35)
            if replace and not kb.select_all():
                raise RuntimeError(kb.stopped)
            last = [0.0]

            def progress(p: float) -> None:
                if p - last[0] >= 0.05 or p >= 1:
                    last[0] = p
                    if self.dock:
                        self.dock.call("ui.typing", [i, p], kind=None)
            how = str(self.cfg.get("insert") or "paste")
            ok = kb.insert(text, how, progress)
            if not ok:
                raise RuntimeError(f"stopped partway: {kb.stopped}")
            if not self._box_filled(box):  # a field that wasn't ready yet: once more
                self._tap(x, y)
                time.sleep(0.5)
                kb.select_all()
                ok = kb.insert(text, how, progress)
                if not (ok and self._box_filled(box)):
                    ok = False
                    raise RuntimeError("Hinge's box stayed empty. Click in it, then try again")
            self.app.focus()  # Hinge to the front, for the user's tap on Send
        except Exception as e:
            log(f"hinge: typing failed: {e!r}")
            with self.lock:
                self.message, self.message_kind = f"Couldn't fill it in: {e}", "error"
        with self.lock:
            self.typing = None
            if ok:
                self.inserted = i
                self.touched = True
                if mode == "profile":
                    prof = self.model.profile
                    opts = self.options()
                    card = prof.card(opts[i].get("target_id", "")) if i < len(opts) else None
                    store.log("inserted", name=prof.name, details=prof.details, comment=text,
                              kind=card.kind if card else "", card=card.text if card else "")
                    self.message = "Filled in. Read it over, then tap Send like in Hinge."
                else:
                    store.log("inserted_reply", name=self.chat.name if self.chat else "", text=text)
                    self.message = "Filled in. Tap send in Hinge when you're happy with it."
                self.message_kind = "good"
        self.push()
        self.eyes.poke()
        self.app.call(self.render_pops)

    def _tap(self, x: int, y: int) -> None:
        """Into the text box: a touch on Hinge's display (a chat's box takes no posted click, and only
        the like sheet's focuses itself), or the window's own click while a secure screen has it."""
        debug(f"touch: tap at ({x}, {y}), screen {self.screen.kind if self.screen else None}")
        if not self.app.tap(x, y):
            self.app.click(x, y)

    def _box_filled(self, box, wait: float = 3.0) -> bool:
        """Whether the text box shows something other than its placeholder, within wait seconds (a
        paste goes through Android's clipboard first, which takes a moment)."""
        l, t, r, b = box
        end = time.monotonic() + wait
        while True:
            shot = self.app.screenshot()
            if shot is not None:
                text = " ".join(ln.text for ln in recognize(shot.crop((max(0, l), max(0, t), r, b)), 2)).strip()
                if text and not reader.is_placeholder(text):
                    return True
            if time.monotonic() > end:
                return False
            time.sleep(0.4)

    # ------------------------------------------------------------------ the dock
    def snapshot(self) -> dict:
        with self.lock:
            mode = self.mode
            opts = self.options()
            src = self.draft if mode == "profile" else self.reply if mode == "chat" else None
            prof = self.model.profile
            order = sorted(prof.cards, key=lambda c: c.top)  # as on the page
            nums = numbering(order)
            labels = {c.id: card_label(c, nums[c.id]) for c in order}
            st = self.status()
            snap: dict = {
                "mode": mode, "status": st, "paused": self.paused, "tucked": self.tucked,
                "theme": "light" if light_theme() else "dark",
                "drafting": self.drafting, "current": self.current, "inserted": self.inserted,
                "best": (src or {}).get("best", 0), "skip_armed": self.skip_armed,
                "tone": {"warmth": self.tone.warmth, "length": self.tone.length},
                "keys": {"next": KEY_NAMES[NEXT], "prev": KEY_NAMES[PREV], "insert": KEY_NAMES[INSERT],
                         "toggle": KEY_NAMES[TOGGLE]},
                "bubble": self.show_bubble,
                "box": bool(self.screen and self.screen.kind in (SHEET, CHAT)),
                "options": [{"text": self.option_text(o), "angle": o.get("angle", ""),
                             "target_id": o.get("target_id", ""),
                             "target_label": labels.get(o.get("target_id", ""), "")} for o in opts],
                "message": self.message or (f"Claude couldn't write ideas: {self.error}" if self.error else ""),
                "message_kind": self.message_kind if self.message else ("error" if self.error else ""),
            }
            if mode == "profile":
                snap["profile"] = {"name": prof.name, "details": prof.details,
                                   "cards": [{"id": c.id, "label": short_label(c, nums[c.id]), "text": c.text}
                                             for c in order]}
                snap["more"] = bool(prof.cards) and not self.model.complete
                if self.draft:
                    d = self.draft
                    snap["draft"] = {"summary": d.get("summary", ""), "recommend": d.get("recommend", "like"),
                                     "skip_reason": d.get("skip_reason", ""), "target_id": d.get("target_id", ""),
                                     "target_label": labels.get(d.get("target_id", ""), ""),
                                     "target_why": d.get("target_why", "")}
            elif mode == "chat":
                chat = self.chat or Chat()
                snap["chat"] = {"name": chat.name, "count": len(chat.messages)}
                if self.reply:
                    snap["reply"] = {k: self.reply.get(k, "") for k in
                                     ("read", "stage", "recommend", "wait_reason", "needs_info")}
            else:
                snap["idle"] = {"title": "Open a profile or a chat",
                                "text": "On Discover, ideas show up here and on the card they're for. "
                                        "In a chat, you get reply ideas."}
            return snap

    def status(self) -> dict:
        if self.paused:
            return {"kind": "paused", "text": "Paused"}
        if self.typing is not None:
            return {"kind": "typing", "text": "Filling in"}
        if self.scanning:
            return {"kind": "busy", "text": "Reading", "detail": "scrolling through the profile to read all of it"}
        if self.drafting:
            return {"kind": "busy", "text": "Writing", "detail": self.writer.describe()}
        if self.error:
            return {"kind": "error", "text": "Problem"}
        if self.mode == "idle":
            return {"kind": "", "text": "Watching"}
        return {"kind": "ready", "text": "Ready", "detail": self.writer.describe()}

    def push(self) -> None:
        if self.dock:
            try:
                self.dock.call("ui.update", self.snapshot(), kind="update")
            except Exception:
                log(f"hinge: dock update failed:\n{traceback.format_exc().rstrip()}")

    # ------------------------------------------------------------------ the pop-ups (window thread)
    def _hide_pops(self) -> None:
        for s in (self.glow, self.bubble):
            if s:
                s.hide()
        if self.pulse:
            self.pulse.stop()
        if self.peek:
            self.peek.stop()

    def render_pops(self) -> None:
        if self.glow is None or self.bubble is None:
            return
        geo = self.app.geometry()
        self.geo = geo
        with self.lock:
            plan = self._plan(geo)
        if plan is None or not geo.visible:
            self._hide_pops()
            return
        target, label, idea, head, foot, edge, avoid = plan
        s = geo.scale
        W, H = geo.size
        clip = self.screen.band if self.screen and self.screen.band != (0, 0) else None  # not over fixed bars
        in_chat = self.screen is not None and self.screen.kind == CHAT
        if in_chat and clip is not None:
            clip = (clip[0], H)  # the message box, under the messages, may have its glow
        if target is not None:
            l, t, r, b = target
            button = bool(self.skip_armed and self.screen and self.screen.kind == PROFILE)
            key = ("glow", r - l, b - t, round(s * 100), label, button)
            if self._sprite_keys.get("glow") != key:
                img, m = glow_sprite(r - l, b - t, s, label, round_=button)
                self.glow.set(img)
                self._glow_m = m
                self._sprite_keys["glow"] = key
            m = self._glow_m
            self.glow.place(l - m, t - m, geo.content, True, clip)
            self.pulse.start()
        else:
            self.glow.hide()
            self.pulse.stop()
        on_card = target is not None and edge is None and self.screen is not None and \
            self.screen.kind == PROFILE and not self.skip_armed
        if idea and self.show_bubble and W - round(24 * s) >= round(100 * s):  # (no room in a tiny window)
            width = min(round(300 * s), W - round(24 * s))
            if on_card:  # two thirds of the card, against its right side: her words start on the left
                width = max(round(170 * s), round((target[2] - target[0]) * 0.66))

            def sprite(tail: str) -> tuple[int, int]:  # (its margin, its height): the same for any tail
                key = ("bubble", idea, head, foot, width, round(s * 100), tail)
                if self._sprite_keys.get("bubble") != key:
                    img, m = bubble_sprite(idea, head, foot, width, s, dark=not light_theme(), tail=tail)
                    self.bubble.set(img)
                    self._bubble_m = m
                    self._bubble_h = img.height
                    self._bubble_w = img.width
                    self._sprite_keys["bubble"] = key
                return self._bubble_m, self._bubble_h
            m, bh = sprite("none" if on_card else "up" if edge == "top" else "down")
            if target is not None and edge is None:
                l, t, r, b = target
                inset = round(12 * s)
                band = self.screen.band if self.screen and self.screen.band != (0, 0) else (0, H)
                if on_card:  # on the card, clear of her words and the heart as far as it can be
                    bx, by = place_on_card(target, (self._bubble_w - 2 * m, bh - 2 * m), avoid, band, s)
                    bx, by = bx - m, by - m
                elif in_chat:  # clear of the messages, the latest above all
                    top, near = chat_spot(self.screen.gaps, band, t, bh - 2 * m, s)
                    if not near:
                        m, bh = sprite("none")  # not by the box: nothing to point at
                    bx, by = l + inset - m, top - m
                else:  # above the comment box, or the skip button
                    by = t - bh + m - round(4 * s)
                    bx = l + inset - m
            else:  # the card is off screen: a note at the edge it's beyond
                band = self.screen.band if self.screen and self.screen.band != (0, 0) else (0, H)
                bx = round(12 * s) - m
                by = band[0] + round(6 * s) - m if edge == "top" else band[1] - bh + m - round(6 * s)
            self.bubble.place(bx, by, geo.content, True, clip)
            if self.peek:
                self.peek.start()
        else:
            self.bubble.hide()
            if self.peek:
                self.peek.stop()

    def _plan(self, geo: Geometry):
        """(target rect in view pixels or None, glow label, idea text, bubble head, bubble foot, edge,
        text boxes on the target to keep the bubble off). edge: None to put the bubble on the target (a
        card, or above a box or button), "top" or "bottom" for a note at that edge of the screen when
        the card is off it."""
        if self.paused or self.mode == "idle" or self.screen is None or self.scanning:
            return None
        scr = self.screen
        opts = self.options()
        idx = self.preview if 0 <= self.preview < len(opts) else min(self.current, max(len(opts) - 1, 0))
        foot = f"{KEY_NAMES[PREV]} {KEY_NAMES[NEXT]} change   {KEY_NAMES[INSERT]} fill in" if self.hotkeys_on else ""
        head = f"{opts[idx].get('angle', 'idea')}  {idx + 1}/{len(opts)}" if opts else ""
        if self.typing is not None:
            head, foot = "filling in…", ""
        if scr.kind in (SHEET, CHAT):
            box = scr.box or self.boxes.get(scr.kind)
            if box is None or not opts:
                return None
            return box, "", self.option_text(opts[idx]), head, foot, None, []
        if self.mode != "profile" or scr.kind != PROFILE:
            return None
        if self.skip_armed:
            band = scr.band if scr.band != (0, 0) else (0, geo.size[1])
            dp = geo.scale * float(state.get("density", 1.0))  # the window's Android density, in dp per pixel
            return skip_button(band, dp), "", "Tap the X to pass on them. That one's yours to press.", "skip", "", None, []
        if not opts:
            return None
        card = self.model.profile.card(opts[idx].get("target_id", "")) or \
            self.model.profile.card((self.draft or {}).get("target_id", ""))
        if card is None:
            return None
        off = self.model.offset
        rect = (round(card.left), round(card.top - off), round(card.right), round(card.bottom - off))
        band = scr.band if scr.band != (0, 0) else (0, geo.size[1])
        showing = min(rect[3], band[1]) - max(rect[1], band[0])
        label = "Claude's pick" if idx == (self.draft or {}).get("best", 0) else opts[idx].get("angle", "")
        idea = self.option_text(opts[idx])
        if self.armed:
            head, foot = "tap ♥ on this card", "the comment goes in when the box opens"
        if showing < min(140 * geo.scale, 0.4 * (rect[3] - rect[1])):  # (mostly) off screen
            where = card_label(card, numbering(self.model.profile.cards)[card.id])
            if rect[1] < band[0]:
                return None, "", f"↑ {where}: {idea}", head, foot, "top", []
            return None, "", f"↓ {where}: {idea}", head, foot, "bottom", []
        avoid = [(x0, y0 - off, x1, y1 - off) for x0, y0, x1, y1 in card.boxes]
        return rect, label, idea, head, foot, None, avoid


PLUGIN = HingeMatcher
