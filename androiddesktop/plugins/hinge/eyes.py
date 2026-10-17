"""Watching Hinge's window: what's on screen, and how far it has scrolled.

A worker thread captures the window's view a few times a second (PC side: Android doesn't take
part). Between frames it measures the scroll (reader.scroll_shift), so the pop-ups follow the cards
as you scroll. When the picture holds still, it reads the screen (OCR) and folds what it sees into the
profile: every card gets a place on the page (view pixels plus the scroll so far), its text, and the
best crop of each photo.
"""
from __future__ import annotations

import difflib
import threading
import time
import traceback
from typing import Callable

from PIL import Image, ImageChops, ImageStat

from ...log import log
from ...winocr import recognize
from . import reader
from .model import Card, Profile
from .reader import Screen

FAST, SLOW = 0.1, 0.3  # seconds between frames while things move / while they don't
HURRY = 0.05  # ...and during the profile scan, which waits on every step


def _similar(a: str, b: str) -> float:
    norm = lambda s: " ".join("".join(c for c in s.lower() if c.isalnum() or c == " ").split())
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


class ProfileModel:
    """One profile as read so far, cards placed on the page."""

    def __init__(self) -> None:
        self.profile = Profile()
        self.offset = 0  # page y of the view's top: grows as you scroll down
        self.counts: dict[str, int] = {}
        self.names: dict[str, int] = {}  # each reading of the name, and how often it came up
        self.grew = 0  # bumped whenever a card is added or gets its text or photo
        self.screens: list[bytes] = []  # a few small JPEGs of the screen, for when a photo wasn't cropped
        self.complete = False  # read from top to bottom (the scan): what's seen later only places it
        self.on_screen: list[tuple[str, tuple[int, int, int, int]]] = []  # (card id, view rect), last screen
        self.renamed = 0  # how many times renumber() changed ids
        self.last_renames: dict[str, str] = {}

    def _new_id(self, kind: str) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return f"{kind}_{self.counts[kind]}"

    def renumber(self) -> dict[str, str]:
        """Ids by place on the page (photo_1 the first photo, prompt_2 the second prompt). They're given
        as cards are first seen, which can be out of order: a scroll that skipped one, a sliver taken
        for the other sort, a prompt that turned out to be the details. Returns old id -> new, for
        those that changed."""
        counts: dict[str, int] = {}
        changes: dict[str, str] = {}
        for c in sorted(self.profile.cards, key=lambda c: c.top):
            counts[c.kind] = counts.get(c.kind, 0) + 1
            if f"{c.kind}_{counts[c.kind]}" != c.id:
                changes[c.id] = f"{c.kind}_{counts[c.kind]}"
        for c in self.profile.cards:
            c.id = changes.get(c.id, c.id)
        self.counts = counts
        self.on_screen = [(changes.get(i, i), r) for i, r in self.on_screen]
        if changes:
            self.renamed += 1
            self.last_renames = changes
        return changes

    def relayout(self, old: tuple[int, int], new: tuple[int, int]) -> None:
        """The view changed size: everything read stays; where the cards are is scaled to the new width
        (photos keep their shape; text cards wrap differently, so they come out near enough) and put
        right as each is seen again. Only taller or shorter, it's the same layout."""
        if old[0] == new[0] or not self.profile.cards:
            return
        margin = min(c.left for c in self.profile.cards)
        k = (new[0] - 2 * margin) / max(1, old[0] - 2 * margin)
        wider = new[0] - old[0]
        for c in self.profile.cards:
            c.top, c.bottom, c.right = c.top * k, c.bottom * k, c.right + wider
            c.boxes = [(x0, y0 * k, x1 + wider, y1 * k) for x0, y0, x1, y1 in c.boxes]
        self.offset *= k

    def _shift(self, below: float, dy: float, but: Card | None = None) -> None:
        """Move the cards from page y `below` down by dy (all but `but`)."""
        for o in self.profile.cards:
            if o is not but and o.top >= below - 1:
                o.top, o.bottom = o.top + dy, o.bottom + dy
                o.boxes = [(x0, y0 + dy, x1, y1 + dy) for x0, y0, x1, y1 in o.boxes]

    def _replace(self, scr: Screen, anchors: list[Card | None]) -> None:
        """Put the cards this screen shows, known by their text or picture, where it shows them, and the
        ones below along with them: after a resize, the map of the page is an estimate until each card
        is seen again. (Otherwise they're where they were: nothing moves.)"""
        for seen, card in sorted(((s, c) for s, c in zip(scr.cards, anchors) if c is not None),
                                 key=lambda p: p[0].rect[1]):
            l, t, r, b = seen.rect
            if not seen.clipped_top and abs(t + self.offset - card.top) >= 2:
                self._shift(card.top, t + self.offset - card.top)
            if not (seen.clipped_top or seen.clipped_bottom) and abs(b + self.offset - card.bottom) >= 2:
                dh = b + self.offset - card.bottom
                self._shift(card.bottom, dh, but=card)
                card.bottom += dh
            card.left, card.right = l, r
            if card.kind != "photo" and not (seen.clipped_top or seen.clipped_bottom):
                boxes = [(round(ln.x), round(ln.y + self.offset), round(ln.right), round(ln.bottom + self.offset))
                         for ln in scr.lines if t <= ln.cy <= b and ln.x >= l - 2 and ln.right <= r + 4]
                card.boxes = boxes or card.boxes

    def _by_text(self, seen: reader.SeenCard) -> Card | None:
        best, score = None, 0.82
        if seen.kind != "photo" and seen.text:
            for c in self.profile.cards:
                if c.kind != "photo" and c.text:
                    s = _similar(seen.text, c.text)
                    if s > score:
                        best, score = c, s
        return best

    def _by_picture(self, seen: reader.SeenCard, sig: tuple[int, ...], top: float) -> Card | None:
        """A photo that looks the same, about the same size, near where the scroll says it should be."""
        h = seen.rect[3] - seen.rect[1]
        for c in self.profile.cards:
            if c.kind == "photo" and c.sig and abs(c.top - top) < 250 and abs(c.height - h) < 0.12 * h \
                    and reader.same_picture(sig, c.sig):
                return c
        return None

    def _by_place(self, seen: reader.SeenCard, top: float, bottom: float) -> Card | None:
        """The known card this one mostly overlaps on the page: of the same sort, or of any sort when
        it's nearly all one (a sliver of a card can pass for the other sort)."""
        photo = seen.kind == "photo"
        for c in self.profile.cards:
            overlap = min(c.bottom, bottom) - max(c.top, top)
            least = min(c.height, bottom - top)
            if overlap > (0.5 if (c.kind == "photo") == photo else 0.85) * least:
                return c
        return None

    def observe(self, scr: Screen, im: Image.Image, jumped: bool = False, measured: bool = False) -> bool:
        """Fold a settled screen into the profile. False if it's somebody else's profile: another name
        in the header, or (after a change that wasn't a scroll) no card we know. measured: the scroll
        offset was measured step by step (the profile scan): keep it, and know photos by their place
        (two photos can look alike in a thumbnail, and that would move everything after them)."""
        sigs = [reader.signature(im, s.rect) if s.kind == "photo" and not (s.clipped_top or s.clipped_bottom) else ()
                for s in scr.cards]
        # What can be recognised (text, or a look-alike photo nearby) pins down the scroll.
        anchors = [self._by_text(s) or (self._by_picture(s, sig, s.rect[1] + self.offset) if sig and not measured
                                        else None)
                   for s, sig in zip(scr.cards, sigs)]
        if self.profile.cards:
            seen_name = scr.header_name or scr.name
            if seen_name and self.profile.name and reader.looks_like_name(seen_name) \
                    and _similar(seen_name, self.profile.name) < 0.6:
                return False
            known = [(s, c) for s, c in zip(scr.cards, anchors) if c is not None]
            if jumped and not known:
                return False
            corrections = sorted(c.top - (s.rect[1] + self.offset) for s, c in known if not s.clipped_top)
            if corrections and not measured:
                self.offset += round(corrections[len(corrections) // 2])
        matches = [(s, a or self._by_place(s, s.rect[1] + self.offset, s.rect[3] + self.offset))
                   for s, a in zip(scr.cards, anchors)]
        self.on_screen = [(c.id, s.rect) for s, c in matches if c is not None]
        # The name as most often read (OCR has off frames), the sticky header's plain type counting double.
        for name, weight in ((scr.header_name, 2), (scr.name, 1)):
            if name and reader.looks_like_name(name):
                self.names[name] = self.names.get(name, 0) + weight
        if self.names:
            self.profile.name = max(self.names, key=self.names.get)
        if scr.pronouns:
            self.profile.details.setdefault("Pronouns", scr.pronouns)
        if self.complete:
            self._replace(scr, anchors)
            return True  # nothing more to learn: where things are is all that's wanted
        changed = False
        here = []
        for (seen, card), sig in zip(matches, sigs):
            l, t, r, b = seen.rect
            top, bottom = t + self.offset, b + self.offset
            if card is None:
                kind = seen.kind
                if kind == "text":
                    kind = "vitals" if reader.looks_like_vitals(seen) else "prompt"
                card = Card(self._new_id(kind), kind, top, bottom, l, r)
                self.profile.cards.append(card)
                self.profile.cards.sort(key=lambda c: c.top)
                changed = True
            other_sort = (seen.kind == "photo") != (card.kind == "photo")
            if other_sort and card.partial and bottom - top > card.height * 1.3:
                # What it was taken for came from a sliver; this sees much more of it.
                card.kind = "photo" if seen.kind == "photo" else "prompt"
                card.id = self._new_id(card.kind)
                card.text, card.boxes, card.image, card.seen, card.sig = "", [], None, 0.0, ()
                other_sort = False
                changed = True
            # Grow the card to what this screen shows of it.
            if not (seen.clipped_top or seen.clipped_bottom):
                card.partial = False
            if not seen.clipped_top:
                card.top = top
            if not seen.clipped_bottom:
                card.bottom = bottom
            card.left, card.right = l, r
            if other_sort:
                pass  # a sliver of a card taken for the other sort: it adds nothing
            elif seen.kind == "photo":
                frac = (b - t) / max(1.0, card.height)
                if card.image is None or frac > card.seen + 0.05:
                    card.image = reader.jpeg(im, seen.rect)
                    card.seen = min(1.0, frac)
                    changed = True
                if sig:
                    card.sig = sig
            else:
                # Where its lines of text are, so the bubble can keep off them.
                lines = [ln for ln in scr.lines if t <= ln.cy <= b and ln.x >= l - 2 and ln.right <= r + 4]
                boxes = [(round(ln.x), round(ln.y + self.offset), round(ln.right), round(ln.bottom + self.offset))
                         for ln in lines]
                if boxes and (not card.boxes or not (seen.clipped_top or seen.clipped_bottom)):
                    card.boxes = boxes
                elif boxes:
                    card.boxes = [x for x in card.boxes if x not in boxes] + boxes
                if seen.text and seen.text != card.text and \
                        (len(seen.text) > len(card.text) + 2 or not (seen.clipped_top or seen.clipped_bottom)):
                    card.text = seen.text
                    if reader.looks_like_vitals(seen):
                        card.kind = "vitals"
                    elif seen.kind == "prompt" and card.kind != "prompt":
                        card.kind = "prompt"
                    changed = True
            here.append((card.id, seen.rect))
        self.on_screen = here
        if changed:
            self.grew += 1
            if len(self.screens) < 4:
                self.screens.append(reader.jpeg(im, (0, 0, im.width, im.height), 720))
        return True


class Eyes:
    """The capture loop. on_scroll(dy) and on_screen(screen, image) are called on its thread."""

    def __init__(self, capture: Callable[[], Image.Image | None], on_scroll: Callable[[int], None],
                 on_screen: Callable[[Screen, Image.Image, bool], None], on_moving: Callable[[], None]) -> None:
        self.capture = capture
        self.on_scroll = on_scroll
        self.on_screen = on_screen
        self.on_moving = on_moving
        self.paused = False
        self.force = False
        self.unjump = False
        self.hurry = False  # look at full speed (during the profile scan)
        self.band: tuple[int, int] | None = None  # the scrolling band of the last screen read
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="hinge-eyes")

    def start(self) -> None:
        self.thread.start()

    def poke(self) -> None:
        """Look again now (after typing, a window change...)."""
        self.wake.set()

    def calm(self) -> None:
        """Forget that the picture jumped (the scan's own big scrolls aren't a new profile)."""
        self.unjump = True

    def read_now(self) -> None:
        """Read the screen at the next frame, still or not (the profile scan, after each step)."""
        self.force = True
        self.wake.set()

    def _run(self) -> None:
        prev_full: Image.Image | None = None
        prev: Image.Image | None = None
        analysed: Image.Image | None = None
        still = 0
        moving = False
        jumped = False  # the picture changed other than by scrolling since the last read
        dirty = False  # something on it changed without the page moving: a video, a message, typing
        last_read = 0.0
        while not self.stop.is_set():
            self.wake.wait(HURRY if self.hurry else FAST if moving or still < 3 or self.force else SLOW)
            self.wake.clear()
            if self.paused:
                prev = analysed = None
                continue
            try:
                im = self.capture()
            except Exception as e:
                log(f"hinge: capture: {e!r}")
                im = None
            if im is None or im.width < 50 or im.height < 50:
                prev = None
                continue
            t = reader.thumb(im)
            band = self.band or (round(im.height * 0.08), round(im.height * 0.92))
            dy: int | None = 0
            if prev is None or prev.size != t.size or prev_full is None:
                dy = None
            elif ImageStat.Stat(ImageChops.difference(prev, t)).mean[0] >= 0.5:
                dy = reader.scroll_shift(prev, t, band, im.height)
                if dy:
                    dy = reader.refine_shift(prev_full, im, band, dy)
                    self.on_scroll(dy)
                elif dy == 0:
                    dirty = True
            if dy is None:
                jumped = True
            if dy is None or dy:
                still = 0
                if not moving:
                    self.on_moving()
                moving = True
            else:
                still += 1
                moving = False
            prev, prev_full = t, im
            if self.unjump:
                jumped = self.unjump = False
            now = time.monotonic()
            due = still == 2 and (analysed is None or analysed.size != t.size or
                                  ImageStat.Stat(ImageChops.difference(analysed, t)).mean[0] > 0.8)
            due = due or (dirty and still >= 2 and now - last_read > 2.0)  # (a playing video: every 2 s)
            if self.force or due:
                self.force = False
                analysed, last_read, dirty = t, now, False
                try:
                    lines = reader.merge_lines(recognize(im, 2), recognize(im, 1))
                    scr = reader.classify(im, lines, ocr=recognize)
                    self.band = scr.band if scr.band != (0, 0) else None
                    self.on_screen(scr, im, jumped)
                    jumped = False
                except Exception:
                    log(f"hinge: reading the screen failed:\n{traceback.format_exc().rstrip()}")
        log("hinge: eyes closed")

