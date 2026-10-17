"""Reading Android Hinge from screenshots of its window, with Windows OCR. No accessibility, no adb.

The layout this relies on (Hinge for Android, 2026): a profile is a column of cards on a near-white
page, inset from both sides by the same margin. Photos fill their card; prompt cards are white with a
hairline border, the question in a small sans font and the answer in a large serif one. On Discover
a row of filter chips stays at the top and the tab bar (dark) at the bottom; the name, in large type,
and the pronouns sit above the first photo, sometimes under a "Most Compatible" banner card that
isn't part of the profile. The like sheet has an "Add a comment" box and a "Send like" button; a chat
has Chat/Profile tabs at the top and a "Send a message" box at the bottom.

Everything here works in view pixels: the screenshot's own, which are also the window's.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Callable

from PIL import Image, ImageChops, ImageFilter

from ...winocr import Line
from .model import Chat, Msg

PROFILE, SHEET, CHAT, CHAT_PROFILE, OTHER = "profile", "sheet", "chat", "chat_profile", "other"

_COMMENT = re.compile(r"^(add a comment|edit comment|add a message|say something)", re.I)
_SEND_LIKE = re.compile(r"^send (like|rose|a rose)\b", re.I)
_MESSAGE = re.compile(r"^s?end a message|^type a message|^message\b", re.I)
_TIME = re.compile(r"^(today|yesterday|mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|"
                   r"nov|dec)\b.*\d|^\d{1,2}:\d{2}\s*(am|pm)?$", re.I)
_SYSTEM = re.compile(r"^you matched|^(seen|read|delivered|sent)\b|get notifications|timing is everything|"
                     r"^enable for|failed to load|tap to retry|^signals$|^chat$|^profile$|^your turn|"
                     r"double tap to like|liked (your|\w+'s) (photo|prompt|answer|video|voice|poll)", re.I)
# Hinge's notes that say how the chat started (who liked what): the writer gets them as context.
_HOW_IT_STARTED = re.compile(r"^you matched|liked (your|\w+'s) (photo|prompt|answer|video|voice|poll)", re.I)
_NOISE = re.compile(r"^[\W_]{1,3}$")
_PROMO = re.compile(r"most compatible|should meet|learn more|^standouts?$|boost your|you're new here|"
                    r"send a rose|out of free likes", re.I)
_CHIPS = re.compile(r"^(signals|age|height|dating int|active today|new here|distance|ethnicity)", re.I)
_PRONOUNS = re.compile(r"^(she|he|they|ze|xe|any)\s*/\s*\w+", re.I)


@dataclass
class SeenCard:
    """A card as one screenshot shows it."""
    rect: tuple[int, int, int, int]  # view pixels: left, top, right, bottom (clipped to the screen)
    kind: str  # photo | prompt | text
    text: str = ""
    question: str = ""
    answer: str = ""
    clipped_top: bool = False  # cut off by the top of the scroll area
    clipped_bottom: bool = False


@dataclass
class Screen:
    kind: str
    size: tuple[int, int]
    lines: list[Line] = field(default_factory=list)
    name: str = ""
    cards: list[SeenCard] = field(default_factory=list)
    band: tuple[int, int] = (0, 0)  # the part of the screen that scrolls
    box: tuple[int, int, int, int] | None = None  # the comment or message box, found by its placeholder (empty)
    send: tuple[int, int, int, int] | None = None  # "Send like" (never pressed: the user does that)
    pronouns: str = ""
    header_name: str = ""  # the name in Discover's sticky header, shown once scrolled down
    chat: Chat | None = None
    gaps: list[tuple[int, int]] = field(default_factory=list)  # a chat's empty stretches, (top, bottom)


# ---------------------------------------------------------------------- small helpers
def _lum(c: tuple[int, ...]) -> int:
    return (c[0] * 2126 + c[1] * 7152 + c[2] * 722) // 10000


def _diff(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def _box(line: Line, pad: float = 0) -> tuple[int, int, int, int]:
    return round(line.x - pad), round(line.y - pad), round(line.right + pad), round(line.bottom + pad)


def clean(text: str) -> str:
    """OCR leaves stray quotes and bullets around; tidy spacing."""
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip("•·|")


# ---------------------------------------------------------------------- cards
def find_margin(im: Image.Image, band: tuple[int, int]) -> int | None:
    """Where the cards' left edge is: the first column that differs from the page, on most rows."""
    W = im.width
    px = im.load()
    votes: dict[int, int] = {}
    for y in range(band[0], band[1], 6):
        page = px[1, y]
        for x in range(2, W // 4):
            if _diff(px[x, y], page) > 6:
                votes[x] = votes.get(x, 0) + 1
                break
    if not votes:
        return None
    x, n = max(votes.items(), key=lambda kv: (kv[1], -kv[0]))
    return x if n >= 3 else None


def segment(im: Image.Image, band: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    """The cards in the scrolling band, as rects in view pixels (clipped to the band)."""
    im = im.convert("RGB")
    W = im.width
    margin = find_margin(im, band)
    if margin is None or margin > W // 5:
        return []
    px = im.load()
    left, right = margin, W - 1 - margin
    cols = [left, left + 1, right, right - 1]
    mid = W // 2

    def card_row(y: int) -> bool:  # a card has an edge on both sides; a line of text only on one
        page = px[1, y]
        return (_diff(px[cols[0], y], page) > 5 or _diff(px[cols[1], y], page) > 5) and \
            (_diff(px[cols[2], y], page) > 5 or _diff(px[cols[3], y], page) > 5)

    def any_row(y: int) -> bool:  # rounded corners: only the middle of the card's top/bottom shows
        page = px[1, y]
        return _diff(px[mid, y], page) > 5 or _diff(px[left + W // 8, y], page) > 5

    def page_row(y: int) -> bool:  # a gap between cards: page colour right across
        page = px[1, y]
        xs = range(left, right, max(4, (right - left) // 24))
        return sum(_diff(px[x, y], page) <= 5 for x in xs) >= 0.9 * len(xs)

    spans: list[list[int]] = []
    for y in range(band[0], band[1]):
        if card_row(y):
            # A photo with pale edges loses its sides for a while; only page colour right across
            # the row between two pieces makes them two cards.
            if spans and (y - spans[-1][1] <= 4 or not any(page_row(g) for g in range(spans[-1][1] + 1, y))):
                spans[-1][1] = y
            else:
                spans.append([y, y])
    radius = max(6, W // 22)
    out = []
    for i, (y0, y1) in enumerate(spans):
        # The sides stop where the corners round off; the top and bottom edges are a little further
        # out (a photo's, or a prompt card's hairline), short of the next card.
        above = max(band[0], spans[i - 1][1] + 3 if i else band[0])
        below = min(band[1] - 1, spans[i + 1][0] - 3 if i + 1 < len(spans) else band[1] - 1)
        top = next((y for y in range(max(above, y0 - radius - 3), y0) if any_row(y)), y0)
        bottom = next((y for y in range(min(below, y1 + radius + 3), y1, -1) if any_row(y)), y1)
        if bottom - top >= max(36, W // 10):
            out.append((left, top, right + 1, bottom + 1))
    return out


_VITAL = re.compile(r"\d+\s*'\s*\d+|\b\d{2}\b|\bcm\b|\b(?:man|woman|straight|gay|bi|christian|catholic|"
                    r"muslim|jewish|hindu|agnostic|atheist|spiritual|monogamy|non-monogamy|sometimes|socially|"
                    r"never|liberal|moderate|conservative|don't want|want children|have children|"
                    r"long-term|short-term|life partner|figuring out)\b", re.I)


def looks_like_vitals(card: SeenCard) -> bool:
    """Hinge's details card: rows of short facts (age, height, place, faith...), not a question and
    its answer."""
    hits = len(_VITAL.findall(card.text))
    return hits >= 3 or (hits >= 2 and not card.answer)


def is_photo(im: Image.Image, rect: tuple[int, int, int, int]) -> bool:
    """A text card has white padding inside its hairline, top to bottom (even a sliver of one with its
    heart showing); a photo has picture right up to its edges. Either side will do, as Hinge's X floats
    over the left of whatever card is at the bottom; the heart sits further in than the strips."""
    l, t, r, b = rect

    def white(x0: int) -> float:
        strip = im.crop((x0, t + 4, x0 + 3, max(t + 5, b - 4))).convert("L")
        return strip.point(lambda v: 255 if v >= 240 else 0).histogram()[255] / max(1, strip.width * strip.height)
    return max(white(l + 3), white(r - 6)) < 0.85


# ---------------------------------------------------------------------- screens
def tabs(lines: list[Line], H: int) -> tuple[Line | None, Line | None]:
    top = [ln for ln in lines if ln.y < H * 0.16]
    chat = next((ln for ln in top if ln.text.strip().lower() == "chat"), None)
    prof = next((ln for ln in top if ln.text.strip().lower() == "profile"), None)
    return chat, prof


def active_tab(im: Image.Image, chat: Line, prof: Line) -> str:
    """Which of Chat / Profile is underlined (the bar under the active tab is dark)."""
    px = im.convert("L").load()
    y0 = round(max(chat.bottom, prof.bottom))
    def ink(line: Line) -> int:
        cx = round(line.x + line.w / 2)
        return max(255 - px[cx, y] for y in range(y0, min(im.height, y0 + round(line.h * 2.4) + 6)))
    return CHAT if ink(chat) > ink(prof) else CHAT_PROFILE


def header_name(lines: list[Line], above: float) -> str:
    """The profile's name: the biggest text in the header (above the given y)."""
    cands = [ln for ln in lines if ln.bottom <= above and re.search(r"[A-Za-z]", ln.text)
             and ln.text.strip().lower() not in ("chat", "profile", "signals", "discover", "standouts")]
    if not cands:
        return ""
    best = max(cands, key=lambda ln: ln.h)
    return re.sub(r"^[^\w]+|[^\w']+$", "", best.text).strip()


def card_text(lines: list[Line], rect: tuple[int, int, int, int]) -> tuple[str, str]:
    """A text card's question (small type) and answer (larger), from the OCR lines inside it."""
    l, t, r, b = rect
    inside = [ln for ln in lines if ln.cy > t and ln.cy < b and ln.x >= l - 2 and ln.right <= r + 4
              and not _NOISE.match(ln.text)]
    if not inside:
        return "", ""
    small = min(ln.h for ln in inside)
    q = [ln.text for ln in inside if ln.h <= small * 1.25]
    a = [ln.text for ln in inside if ln.h > small * 1.25]
    if not a:  # all one size: the first line is the question
        q, a = [inside[0].text], [ln.text for ln in inside[1:]]
    return clean(" ".join(q)), clean(" ".join(a))


def is_placeholder(text: str) -> bool:
    """An empty comment or message box's own text."""
    text = text.strip()
    return bool(_COMMENT.match(text) or _MESSAGE.match(text))


def merge_lines(primary: list[Line], extra: list[Line]) -> list[Line]:
    """Lines OCR found in one pass plus those only another found (large type reads better at 1x,
    small type enlarged)."""
    def overlaps(a: Line, b: Line) -> bool:
        return min(a.right, b.right) - max(a.x, b.x) > 0 and min(a.bottom, b.bottom) - max(a.y, b.y) > 0
    out = list(primary) + [ln for ln in extra if not any(overlaps(ln, p) for p in primary)]
    out.sort(key=lambda ln: (round(ln.y / 6), ln.x))
    return out


def at_top(im: Image.Image, ocr: Callable[[Image.Image, float], list[Line]]) -> bool:
    """Whether Discover shows the top of a profile: its filter chips are only there (scrolled down, a
    header with the name takes their place)."""
    strip = im.crop((0, 0, im.width, round(im.height * 0.09)))
    return any(_CHIPS.match(ln.text) for ln in ocr(strip, 2))


def discover_band(im: Image.Image, lines: list[Line]) -> tuple[int, int]:
    """Discover's scrolling part: below the filter chips, above the (dark) tab bar."""
    W, H = im.size
    chips = [ln for ln in lines if ln.y < H * 0.1 and _CHIPS.match(ln.text)]
    top = round(max(ln.bottom for ln in chips) + max(ln.h for ln in chips)) if chips else round(H * 0.08)
    px = im.load()
    page = px[1, H // 2]
    bottom = H
    while bottom > H * 0.7 and _diff(px[1, bottom - 1], page) > 30 and _diff(px[W // 2, bottom - 1], page) > 30:
        bottom -= 1
    if bottom == H:
        bottom = round(H * 0.92)
    return top, bottom


def profile_head(lines: list[Line], cards: list[SeenCard], band: tuple[int, int]) -> tuple[Line | None, str]:
    """The name (the largest type on the page outside the cards) and the pronouns under it, if shown."""
    def outside(ln: Line) -> bool:
        return not any(c.rect[1] - 2 <= ln.cy <= c.rect[3] + 2 for c in cards)
    loose = [ln for ln in lines if band[0] <= ln.y < band[1] and outside(ln) and re.search(r"[A-Za-z]", ln.text)
             and not _PROMO.search(ln.text)]
    pron = next((ln for ln in loose if _PRONOUNS.match(ln.text.strip())), None)
    names = [ln for ln in loose if ln is not pron and ln.h >= 16]
    name = max(names, key=lambda ln: ln.h) if names else None
    return name, clean(pron.text) if pron else ""


def looks_like_name(text: str) -> bool:
    """A first name, maybe two words: not a line of someone's profile."""
    words = re.sub(r"[^\w' -]", "", text).split()
    return 0 < len(words) <= 3 and len(" ".join(words)) <= 24 and all(w[:1].isalpha() for w in words)


def tidy_name(text: str) -> str:
    """A name as OCR reads it in Hinge's serif: "Noel I e" is "Noelle" (a split double l)."""
    text = re.sub(r"^[^\w]+|[^\w'\- ]+$", "", text).strip()
    parts = text.split()
    if len(parts) > 1 and any(len(p) == 1 for p in parts):
        text = "".join(parts)
    return re.sub(r"(?<=[a-z])I(?=[a-z]|$)", "l", text)


def skip_button(band: tuple[int, int], dp: float) -> tuple[int, int, int, int]:
    """Where Discover's skip (✕) button floats: a 60 dp circle, 20 dp in from the left and 20 dp above
    the tab bar. Only ever pointed at, for the user to tap."""
    bottom = band[1] - round(21 * dp)
    return round(20 * dp), bottom - round(60 * dp), round(80 * dp), bottom


def text_rows(im: Image.Image, top: int, bottom: int) -> list[tuple[int, int]]:
    """The rows of dark type between top and bottom, as (top, bottom) pairs."""
    g = im.convert("L").crop((0, top, im.width, bottom)).point(lambda v: 255 if v < 150 else 0)
    inked = [g.crop((0, y, g.width, y + 1)).getbbox() is not None for y in range(g.height)]
    rows, start = [], None
    for y, ink in enumerate(inked + [False]):
        if ink and start is None:
            start = y
        elif not ink and start is not None:
            rows.append((top + start, top + y))
            start = None
    return rows


def ink_only(im: Image.Image) -> Image.Image:
    """Black type on white, with anything coloured (the purple verified badge, which OCR takes for a
    letter) turned white."""
    rgb = im.convert("RGB")
    hsv = rgb.convert("HSV")
    _, sat, _ = hsv.split()
    dark = rgb.convert("L").point(lambda v: 255 if v < 128 else 0)
    # Coloured, and the greyish anti-aliased rim around it (which would still read as a letter).
    colour = sat.point(lambda v: 255 if v >= 70 else 0).filter(ImageFilter.MaxFilter(5))
    ink = ImageChops.subtract(dark, colour)
    return ink.point(lambda v: 0 if v else 255).convert("RGB")


def read_name_strip(im: Image.Image, top: int, bottom: int, ocr: Callable[[Image.Image, float], list[Line]]
                    ) -> list[Line]:
    """OCR the rows of type in the strip where the name is, one at a time, as black on white. Windows
    OCR misses the name's large serif type (beside the verified badge) on the whole screen, but reads
    it reliably that way."""
    out = []
    for r0, r1 in text_rows(im, top, bottom):
        if r1 - r0 < 6:
            continue
        for margin in (12, 8, 18):  # Windows OCR can come back empty for one crop and read the next
            crop = im.crop((0, max(0, r0 - margin), im.width, min(im.height, r1 + margin)))
            bw = ink_only(crop)
            pad = Image.new("RGB", (bw.width + 80, bw.height + 80), (255, 255, 255))
            pad.paste(bw, (40, 40))
            y0 = max(0, r0 - margin) - 40
            found = [Line(ln.text, ln.x - 40, ln.y + y0, ln.w, ln.h, ln.words) for ln in ocr(pad, 1)]
            if found:
                out += found
                break
    return out


def classify(im: Image.Image, lines: list[Line], ocr: Callable[[Image.Image, float], list[Line]] | None = None
             ) -> Screen:
    """What's on screen. ocr: OCR for a second look at a part of it (the name), if it's needed."""
    W, H = im.size
    scr = Screen(OTHER, (W, H), lines)
    comment = next((ln for ln in lines if _COMMENT.match(ln.text)), None)
    send = next((ln for ln in lines if _SEND_LIKE.match(ln.text)), None)
    chat_tab, prof_tab = tabs(lines, H)
    message = next((ln for ln in reversed(lines) if ln.y > H * 0.6 and _MESSAGE.match(ln.text)), None)
    if send is not None or comment is not None:
        scr.kind = SHEET
        if send is not None:
            scr.send = _box(send, 8)
        if comment is not None:
            scr.box = (round(W * 0.06), round(comment.y - comment.h), round(W * 0.94), round(comment.bottom + comment.h))
        return scr
    if chat_tab is not None and prof_tab is not None:
        scr.kind = active_tab(im, chat_tab, prof_tab)
        scr.name = header_name(lines, min(chat_tab.y, prof_tab.y))
        if message is not None:
            scr.box = (round(W * 0.03), round(message.y - message.h), round(message.right + W * 0.3),
                       round(message.bottom + message.h))
        band_top = round(max(chat_tab.bottom, prof_tab.bottom) + chat_tab.h)
        band_bottom = round(message.y - message.h * 2) if message is not None else round(H * 0.9)
        scr.band = (band_top, band_bottom)
        if scr.kind == CHAT:
            if ocr is not None:
                scr.lines = lines = merge_lines(lines, read_bubbles(im, lines, scr.band, ocr))
            scr.chat = read_chat(im, lines, scr.band, scr.name)
            scr.gaps = gaps(im, scr.band)
        else:
            scr.cards = read_cards(im, lines, scr.band)
        return scr
    band = discover_band(im, lines)
    seen = read_cards(im, lines, band)
    promos = [c for c in seen if c.kind != "photo" and _PROMO.search(c.text)]
    cards = [c for c in seen if c not in promos]
    if cards:
        scr.kind = PROFILE
        scr.band = band
        head, scr.pronouns = profile_head(lines, cards, band)
        if head is not None and ocr is not None:  # again without the verified badge, which reads as a letter
            again = [ln for ln in read_name_strip(im, max(0, round(head.y) - 4), round(head.bottom) + 4, ocr)
                     if re.search(r"[A-Za-z]{2}", ln.text)]
            head = max(again, key=lambda ln: ln.h) if again else head
        first = cards[0].rect[1]
        above = max([c.rect[3] for c in promos if c.rect[3] <= first] + [band[0]])
        if head is None and ocr is not None and first - above > 24 and not cards[0].clipped_top:
            head, pron = profile_head(read_name_strip(im, above, first, ocr), cards, band)
            scr.pronouns = scr.pronouns or pron
        if head is not None:
            scr.name = tidy_name(head.text)
            cards = [c for c in cards if c.rect[1] > head.y]  # above the name: Hinge's, not theirs
        scr.cards = cards
        # The sticky header's name sits centred at the very top; text of the profile can scroll in
        # just under it.
        sticky = [ln for ln in lines if ln.bottom <= band[0] and ln.y < H * 0.06 and abs(ln.cx - W / 2) < W * 0.15
                  and not _CHIPS.match(ln.text) and not _PROMO.search(ln.text) and looks_like_name(ln.text)]
        if sticky:
            top = max(sticky, key=lambda ln: ln.h)
            if ocr is not None:  # again without the badge
                again = [ln for ln in read_name_strip(im, max(0, round(top.y) - 2), min(band[0], round(top.bottom) + 2), ocr)
                         if re.search(r"[A-Za-z]{2}", ln.text)]
                top = max(again, key=lambda ln: ln.h) if again else top
            scr.header_name = tidy_name(top.text)
        scr.kind = PROFILE if cards or head is not None else OTHER
    return scr


def read_cards(im: Image.Image, lines: list[Line], band: tuple[int, int]) -> list[SeenCard]:
    out = []
    for rect in segment(im, band):
        l, t, r, b = rect
        photo = is_photo(im, rect)
        card = SeenCard(rect, "photo" if photo else "text", clipped_top=t <= band[0] + 3,
                        clipped_bottom=b >= band[1] - 3)
        if not photo:
            q, a = card_text(lines, rect)
            card.question, card.answer = q, a
            card.text = f"{q}: {a}" if q and a else (q or a)
            card.kind = "prompt" if q and a else "text"
        out.append(card)
    return out


# ---------------------------------------------------------------------- chats
def _shade(c: tuple[int, int, int]) -> str:
    """Bubble colours: theirs grey, the user's tinted (Hinge's purple), the page white."""
    r, g, b = c[:3]
    if min(c[:3]) >= 246:
        return "page"
    if abs(r - g) < 8 and abs(g - b) < 8:
        return "grey" if r > 150 else "dark"
    return "tint"


def bubbles(im: Image.Image, band: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    """The message bubbles in the band, as rects: blocks of one flat colour, other than the page's,
    with the page around them (a flat patch of a photo has more photo around it). Their type breaks
    a row of the colour up, but the padding above and below each line of it doesn't."""
    im = im.convert("RGB")
    W, H = im.size
    px = im.load()
    step, run_min = 2, max(20, W // 18)

    def share(y: int, x0: int, x1: int, c: tuple[int, ...]) -> float:  # how much of the row is colour c
        xs = range(x0, x1, step)
        return sum(_diff(px[x, y], c) <= 6 for x in xs) / max(1, len(xs))

    found: list[list] = []  # [left, top, right, bottom, colour]
    for y in range(band[0], band[1], step):
        page = px[1, y]
        x = 0
        while x < W:
            c = px[x, y]
            if _diff(c, page) <= 6:
                x += step
                continue
            x0 = x
            while x + step < W and _diff(px[x + step, y], c) <= 4:
                x += step
            if x - x0 >= run_min:
                for f in found:
                    lo, hi = max(f[0], x0), min(f[2], x)
                    # The same bubble if the rows since its last run are still mostly its colour
                    # (a line of type); a row of page between makes it the next bubble.
                    if _diff(f[4], c) <= 6 and y - f[3] <= 32 and hi - lo > 0.5 * min(f[2] - f[0], x - x0) \
                            and all(share(g, lo, hi, c) >= 0.4 for g in range(f[3] + step, y, step)):
                        f[0], f[2], f[3] = min(f[0], x0), max(f[2], x), y
                        break
                else:
                    found.append([x0, y, x, y, c])
            x += step

    def page_side(pts: list[tuple[int, int]]) -> bool:
        return sum(_diff(px[x, y], px[1, y]) <= 6 for x, y in pts) >= 0.6 * max(1, len(pts))

    out = []
    for l, t, r, b, c in found:
        r, b = r + 1, b + 1
        if b - t < 16 or r - l < 30:
            continue
        inside = [px[x, y] for y in range(t, b, step) for x in range(l, r, step)]
        if sum(_diff(p, c) <= 6 for p in inside) < 0.6 * len(inside):
            continue
        sides = [[(x, max(0, t - 3)) for x in range(l + 8, r - 8, step)],
                 [(x, min(H - 1, b + 2)) for x in range(l + 8, r - 8, step)],
                 [(max(0, l - 3), y) for y in range(t + 6, b - 6, step)],
                 [(min(W - 1, r + 2), y) for y in range(t + 6, b - 6, step)]]
        if sum(page_side(s) for s in sides) >= 3:
            out.append((l, t, r, b))
    return out


def read_bubbles(im: Image.Image, lines: list[Line], band: tuple[int, int],
                 ocr: Callable[[Image.Image, float], list[Line]]) -> list[Line]:
    """Another look at the bubbles OCR found nothing in: alone on the whole screen, a short message
    ("Hey") can go unread. Each is read on its own, as black type on white."""
    out = []
    for l, t, r, b in bubbles(im, band):
        if any(l <= ln.cx <= r and t <= ln.cy <= b for ln in lines):
            continue
        crop = im.crop((l + 2, t + 2, r - 2, b - 2)).convert("L")
        back = crop.getpixel((0, 0))
        # Dark type on a pale bubble or pale type on a dark one: the type in black either way.
        bw = crop.point(lambda v: 0 if abs(v - back) > 60 else 255)
        if bw.point(lambda v: 255 - v).getbbox() is None:
            continue  # nothing written in it
        pad = Image.new("L", (bw.width + 80, bw.height + 80), 255)
        pad.paste(bw, (40, 40))
        for k in (1, 2):
            found = ocr(pad.convert("RGB"), k)
            if found:
                out += [Line(ln.text, ln.x - 40 + l + 2, ln.y - 40 + t + 2, ln.w, ln.h, ln.words) for ln in found]
                break
    return out


def gaps(im: Image.Image, band: tuple[int, int]) -> list[tuple[int, int]]:
    """The stretches of the band with nothing but the page on them, as (top, bottom) pairs: where a
    pop-up can go without covering a message."""
    rgb = im.convert("RGB")
    strip = rgb.crop((0, band[0], rgb.width, band[1]))
    diff = ImageChops.difference(strip, Image.new("RGB", strip.size, rgb.getpixel((1, band[0]))))
    r, g, b = diff.split()
    ink = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v > 8 else 0)
    out, start = [], None
    for y in range(ink.height + 1):
        empty = y < ink.height and ink.crop((0, y, ink.width, y + 1)).getbbox() is None
        if empty and start is None:
            start = y
        elif not empty and start is not None:
            out.append((band[0] + start, band[0] + y))
            start = None
    return out


def read_chat(im: Image.Image, lines: list[Line], band: tuple[int, int], name: str) -> Chat:
    """The messages on a chat screen: theirs start at the left, the user's end at the right."""
    W = im.width
    px = im.convert("RGB").load()
    body = [ln for ln in lines if band[0] <= ln.y and ln.bottom <= band[1] and ln.text.strip()]
    blocks: list[tuple[list[Line], str]] = []
    for ln in body:
        sx = max(1, round(ln.x - 5))
        shade = _shade(px[sx, min(im.height - 1, round(ln.cy))])
        side = "left" if ln.x < W * 0.3 and ln.right < W * 0.85 else ("right" if ln.right > W * 0.7 else "mid")
        key = f"{shade}:{side}"
        if blocks:
            prev_lines, prev_key = blocks[-1]
            prev = prev_lines[-1]
            if key == prev_key and 0 <= ln.y - prev.bottom < prev.h * 1.6:
                prev_lines.append(ln)
                continue
        blocks.append(([ln], key))
    msgs, context = [], []
    for blk, key in blocks:
        text = clean(" ".join(ln.text for ln in blk))
        shade, side = key.split(":")
        if not text or _TIME.search(text):
            continue
        if _SYSTEM.search(text):
            if _HOW_IT_STARTED.search(text):
                context.append(text)
            continue
        if side == "right" and shade != "page":
            msgs.append(Msg("me", text))
        elif side == "left" and shade != "page":
            msgs.append(Msg("them", text))
        elif shade == "page" and side == "mid":
            continue  # timestamps, centered notes
        else:
            msgs.append(Msg("?", text))
    return Chat(name=name, messages=msgs, context=" | ".join(context))


# ---------------------------------------------------------------------- scrolling
# Measuring a scroll goes by the rows with some detail in them (blank rows match anywhere), and by a
# low percentile of how far each row is off rather than the average: a video, an animated photo or a
# floating button changes or stays put whatever the page does, and mustn't outvote the rest.
THUMB_W = 96
TEXTURE = 3  # a row with less detail than this (mean step between neighbours) says nothing
QUANTILE = 0.3


def thumb(im: Image.Image, width: int = THUMB_W) -> Image.Image:
    return im.convert("L").resize((width, max(1, round(im.height * width / im.width))), Image.BILINEAR)


def row_means(im: Image.Image) -> bytes:
    """Each row's mean value."""
    return im.resize((1, im.height), Image.BOX).tobytes()


def texture(im: Image.Image) -> bytes:
    """Each row's detail: the mean difference between neighbouring pixels."""
    w = im.width
    return row_means(ImageChops.difference(im.crop((1, 0, w, im.height)), im.crop((0, 0, w - 1, im.height))))


def _err(a: Image.Image, b: Image.Image, r0: int, r1: int, s: int, detail: bytes | None = None,
         quantile: float = QUANTILE) -> float:
    """How far rows r0..r1 of a are from the same content s rows higher up in b: a low percentile over
    the rows with detail. Infinite when too few rows have any."""
    if r1 - r0 < 4:
        return float("inf")
    w = a.width
    rows = row_means(ImageChops.difference(a.crop((0, r0, w, r1)), b.crop((0, r0 - s, w, r1 - s))))
    if detail is not None:
        rows = [e for e, d in zip(rows, detail[r0:r1]) if d >= TEXTURE]
        if len(rows) < max(6, (r1 - r0) // 8):
            return float("inf")
    rows = sorted(rows)
    return float(rows[int(len(rows) * quantile)])


def scroll_shift(prev: Image.Image, cur: Image.Image, band: tuple[int, int], view_h: int,
                 max_frac: float = 0.45, search: tuple[int, int] | None = None) -> int | None:
    """How many view pixels the content moved up between two frames (scrolling down is positive),
    from their thumbnails (thumb()); None if it isn't a scroll (a new screen, a sheet opening), or the
    picture has too little detail to tell. band: the scrolling part of the view, in view pixels;
    view_h: the view's height. search: the range of moves to consider (view pixels), when it's known
    roughly how far it went."""
    if prev.size != cur.size:
        return None
    H = prev.height
    b0, b1 = round(band[0] / view_h * H), round(band[1] / view_h * H)
    span = b1 - b0
    limit = round(span * max_frac)
    lo, hi = (-limit, limit) if search is None else \
        (max(-limit, int(search[0] * H / view_h) - 1), min(limit, int(search[1] * H / view_h) + 1))
    detail = texture(prev)
    errs: dict[int, float] = {}
    for s in range(lo, hi + 1):
        r0, r1 = max(b0, b0 + s), min(b1, b1 + s)
        if r1 - r0 >= span * 0.4:
            errs[s] = _err(prev, cur, r0, r1, s, detail)
    if not errs:
        return None
    best_s = min(errs, key=lambda k: (errs[k], abs(k)))
    best_e = errs[best_s]
    if best_e > 6:
        return None
    if errs.get(0, float("inf")) <= best_e + 0.5:
        return 0  # still, or only something on it is moving (a video)
    near = [k for k, e in errs.items() if e <= best_e + 0.5 and abs(k - best_s) > 3]
    if near:
        return None  # repeating content: can't tell how far it moved
    return round(best_s * view_h / H)


def refine_shift(prev: Image.Image, cur: Image.Image, band: tuple[int, int], guess: int, reach: int = 6) -> int:
    """Pin down a shift to the pixel around the guess, on the full frames. The median row this time: a
    low percentile could take rows that happen to repeat a few pixels apart for a match."""
    a, b = prev.convert("L"), cur.convert("L")
    for detail in (texture(a), None):  # no rows with detail: every row, then
        best, best_e = guess, float("inf")
        for s in range(guess - reach, guess + reach + 1):
            r0, r1 = max(band[0], band[0] + s), min(band[1], band[1] + s)
            if r1 - r0 < 20:
                continue
            e = _err(a, b, r0, r1, s, detail, 0.5)
            if e < best_e or (e == best_e and abs(s - guess) < abs(best - guess)):
                best, best_e = s, e
        if best_e < float("inf"):
            break
    return best


def changed(prev: Image.Image, cur: Image.Image, band: tuple[int, int] | None, view_h: int) -> bool:
    """Whether the screen is different (not just a video playing on it), from thumbnails."""
    if prev.size != cur.size:
        return True
    H = prev.height
    b0, b1 = (round(band[0] / view_h * H), round(band[1] / view_h * H)) if band else (0, H)
    return _err(prev, cur, b0, b1, 0, texture(prev)) > 2 or _err(prev, cur, b0, b1, 0) > 2


def signature(im: Image.Image, rect: tuple[int, int, int, int]) -> tuple[int, ...]:
    """A tiny greyscale picture of a region, to recognise a photo card again."""
    l, t, r, b = rect
    return tuple(im.crop((l, t, r, b)).convert("L").resize((8, 8), Image.BILINEAR).tobytes())


def same_picture(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    if not a or not b or len(a) != len(b):
        return False
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a) < 14


def jpeg(im: Image.Image, rect: tuple[int, int, int, int], max_side: int = 640) -> bytes:
    crop = im.crop(rect).convert("RGB")
    crop.thumbnail((max_side, max_side))
    out = io.BytesIO()
    crop.save(out, "JPEG", quality=82)
    return out.getvalue()
