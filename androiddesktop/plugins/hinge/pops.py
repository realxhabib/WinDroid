"""The pop-ups on the Hinge window: a glow around a card and an idea bubble beside it.

Each is a click-through layer (overlay.py) holding a picture drawn once; following a scroll only moves
it, and where it reaches past the app's screen (under the window's bar, say) it's cropped. A gentle
pulse runs on the glow's opacity alone, which costs next to nothing. All on the window thread.
"""
from __future__ import annotations

import math
import time

import win32api
from PIL import Image, ImageDraw, ImageFilter

from ...overlay import Layer, premultiplied
from ...winapi import font

VIOLET = (155, 107, 255)
PINK = (255, 92, 138)
INK = (24, 22, 30)
PAPER = (255, 255, 255)


def _gradient(w: int, h: int, a: tuple, b: tuple) -> Image.Image:
    """A diagonal violet-to-pink fill."""
    small = Image.new("RGB", (2, 2))
    small.putdata([a, tuple((x + y) // 2 for x, y in zip(a, b)), tuple((x + y) // 2 for x, y in zip(a, b)), b])
    return small.resize((max(w, 1), max(h, 1)), Image.BILINEAR)


def glow_sprite(w: int, h: int, scale: float, label: str = "", round_: bool = False) -> tuple[Image.Image, int]:
    """A glowing rounded frame for a w x h card (round_: a circle, for a button), and how far it
    reaches past it on each side."""
    s = scale
    m = round(18 * s)
    radius = min(w, h) // 2 if round_ else round(16 * s)
    W, H = w + 2 * m, h + 2 * m
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((m, m, m + w - 1, m + h - 1), radius=radius, outline=255, width=max(2, round(5 * s)))
    halo = mask.filter(ImageFilter.GaussianBlur(round(9 * s))).point(lambda v: min(255, v * 2))
    line = Image.new("L", (W, H), 0)
    ImageDraw.Draw(line).rounded_rectangle((m, m, m + w - 1, m + h - 1), radius=radius, outline=255,
                                           width=max(2, round(3 * s)))
    alpha = Image.eval(halo, lambda v: v * 3 // 4)
    alpha.paste(255, (0, 0), line)
    out = _gradient(W, H, VIOLET, PINK).convert("RGBA")
    out.putalpha(alpha)
    if label:
        f = font("text", max(9, round(11.5 * s)))
        pad = round(7 * s)
        tw = round(ImageDraw.Draw(out).textlength(label, font=f))
        cw, ch = tw + 2 * pad + round(14 * s), round(22 * s)
        chip = _gradient(cw, ch, VIOLET, PINK).convert("RGBA")
        cm = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(cm).rounded_rectangle((0, 0, cw - 1, ch - 1), radius=ch // 2, fill=255)
        chip.putalpha(cm)
        cd = ImageDraw.Draw(chip)
        cd.text((pad, ch / 2), "♥", font=f, fill=PAPER, anchor="lm")
        cd.text((pad + round(14 * s), ch / 2), label, font=f, fill=PAPER, anchor="lm")
        out.alpha_composite(chip, (m + round(12 * s), max(0, m - ch // 2)))
    return out, m


def place_on_card(card: tuple[int, int, int, int], size: tuple[int, int], avoid: list[tuple[int, int, int, int]],
                  rows: tuple[int, int], scale: float) -> tuple[int, int]:
    """Where a bubble of size (w, h) goes on a card (all in view pixels): against its right side, as low
    as it fits without covering the card's text (avoid) or its heart (bottom right), within the rows on
    screen. With no such place, the one covering the least."""
    s = scale
    l, t, r, b = card
    w, h = size
    inset, heart = round(12 * s), round(68 * s)
    x = r - inset - w
    top, bottom = max(t, rows[0]) + inset, min(b, rows[1]) - inset
    blocked = [(x0 - 4, y0 - 4, x1 + 4, y1 + 4) for x0, y0, x1, y1 in avoid]
    blocked.append((r - heart, b - heart, r, b))

    def covered(y: int) -> int:
        return sum(max(0, min(x + w, x1) - max(x, x0)) * max(0, min(y + h, y1) - max(y, y0))
                   for x0, y0, x1, y1 in blocked)
    candidates = list(range(bottom - h, top - 1, -max(4, round(6 * s)))) or [top]
    best = min(candidates, key=lambda y: (covered(y), -y))
    return x, best


def chat_spot(gaps: list[tuple[int, int]], band: tuple[int, int], box_top: int, height: int,
              scale: float) -> tuple[int, bool]:
    """Where a bubble of the given height goes in a chat (view pixels): its top, in the lowest empty
    stretch above the message box it fits in, so it covers no message (the latest above all); and
    whether that's right above the box, for its tail to point at. With no such stretch, at the top,
    over the oldest messages on screen."""
    room = round(4 * scale)
    best: tuple[int, bool] | None = None
    for g0, g1 in gaps:
        near = g1 >= band[1] - 2  # empty down to the bottom of the messages: the box is just under it
        bottom = box_top - room if near else g1 - room
        if bottom - height >= g0 + room and (best is None or bottom > best[0]):
            best = (bottom, near)
    if best is None:
        return band[0] + room, False
    return best[0] - height, best[1]


def _wrap(text: str, f, width: int, draw: ImageDraw.ImageDraw, max_lines: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=f) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=f) > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def bubble_sprite(text: str, head: str, foot: str, width: int, scale: float, dark: bool = False,
                  tail: str = "down") -> tuple[Image.Image, int]:
    """A speech bubble with an idea: head (angle, 1/3) above the text, key hints below; its tail points
    down (at what's below it), up, or there's none ("none": the bubble sits on its card). Returns the
    picture and the shadow margin around the bubble."""
    s = scale
    m = round(14 * s)
    pad = round(12 * s)
    tail_h = round(9 * s)
    body_f = font("text", max(10, round(14 * s)))
    small_f = font("text", max(8, round(10.5 * s)))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = _wrap(text, body_f, width - 2 * pad, probe, 4)
    lh = round(19 * s)
    h = pad + round(15 * s) + len(lines) * lh + (round(20 * s) if foot else 0) + pad - round(4 * s)
    W, H = width + 2 * m, h + tail_h + 2 * m
    top = m + (tail_h if tail == "up" else 0)
    bg = (32, 30, 40) if dark else PAPER
    fg = (242, 240, 247) if dark else INK
    sub = (170, 165, 185) if dark else (110, 104, 124)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((m, top + round(4 * s), m + width, top + h + round(4 * s)),
                                             radius=round(14 * s), fill=120)
    shadow = shadow.filter(ImageFilter.GaussianBlur(round(8 * s)))
    out.putalpha(shadow)
    shape = Image.new("L", (W, H), 0)
    sd = ImageDraw.Draw(shape)
    sd.rounded_rectangle((m, top, m + width, top + h), radius=round(14 * s), fill=255)
    tx = m + round(28 * s)
    if tail == "down":
        sd.polygon([(tx - tail_h, top + h - 1), (tx + tail_h, top + h - 1), (tx, top + h + tail_h)], fill=255)
    elif tail == "up":
        sd.polygon([(tx - tail_h, top + 1), (tx + tail_h, top + 1), (tx, top - tail_h)], fill=255)
    fill = Image.new("RGBA", (W, H), (*bg, 255))
    out.paste(fill, (0, 0), shape)
    # A gradient rim, so it reads as the glow's.
    rim = Image.new("L", (W, H), 0)
    ImageDraw.Draw(rim).rounded_rectangle((m, top, m + width, top + h), radius=round(14 * s), outline=255,
                                          width=max(1, round(1.5 * s)))
    grad = _gradient(W, H, VIOLET, PINK).convert("RGBA")
    out.paste(grad, (0, 0), rim)
    d = ImageDraw.Draw(out)
    y = top + pad
    hx = m + pad
    d.text((hx, y), "♥", font=small_f, fill=PINK)
    d.text((hx + round(14 * s), y), head.upper(), font=small_f, fill=VIOLET)
    y += round(15 * s)
    for ln in lines:
        d.text((hx, y), ln, font=body_f, fill=fg)
        y += lh
    if foot:
        d.text((hx, y + round(4 * s)), foot, font=small_f, fill=sub)
    return out, m


class Sprite:
    """One picture in its own layer, placed in view pixels and cropped to the app's screen."""

    def __init__(self, owner: int) -> None:
        self.layer = Layer(owner)
        self.image: Image.Image | None = None  # premultiplied
        self.at = (0, 0)  # where the picture's top-left goes, in view pixels
        self._shown_key = None

    def set(self, image: Image.Image | None) -> None:
        self.image = premultiplied(image) if image is not None else None
        self._shown_key = None

    def place(self, x: int, y: int, content: tuple[int, int, int, int], visible: bool,
              clip: tuple[int, int] | None = None) -> None:
        """Put the picture's top-left at view pixel (x, y); content: the view's rect on screen; clip:
        the rows of the view it may cover (the scrolling part), if not all of them."""
        self.at = (x, y)
        if self.image is None or not visible:
            self.layer.hide()
            self._shown_key = None
            return
        cl, ct, cr, cb = content
        if clip is not None:
            ct, cb = max(ct, content[1] + clip[0]), min(cb, content[1] + clip[1])
        sx, sy = content[0] + x, content[1] + y
        w, h = self.image.size
        # The part that's on the app's screen.
        x0, y0 = max(sx, cl), max(sy, ct)
        x1, y1 = min(sx + w, cr), min(sy + h, cb)
        if x1 - x0 < 4 or y1 - y0 < 4:
            self.layer.hide()
            self._shown_key = None
            return
        crop = (x0 - sx, y0 - sy, x1 - sx, y1 - sy)
        key = (crop, id(self.image))
        if key == self._shown_key and self.layer.shown:
            self.layer.move(x0, y0)
            return
        part = self.image if crop == (0, 0, w, h) else self.image.crop(crop)
        self.layer.update(part, x0, y0, premultiplied=True)
        self._shown_key = key

    def fade(self, alpha: int) -> None:
        self.layer.fade(alpha)

    def hide(self) -> None:
        self.layer.hide()
        self._shown_key = None

    def destroy(self) -> None:
        self.layer.destroy()


class Peek:
    """Fades a sprite nearly away while the mouse is over it, so what it covers can be read."""

    def __init__(self, sprite: Sprite, lo: int = 30) -> None:
        self.sprite = sprite
        self.lo = lo
        self.alpha = 255.0
        self.running = False

    def start(self) -> None:
        if not self.running:
            self.running = True
            self.alpha = 255.0
            self.sprite.layer.every(40, self._tick)

    def stop(self) -> None:
        if self.running:
            self.running = False
            self.sprite.layer.every(0, None)
            self.sprite.fade(255)

    def _tick(self) -> None:
        layer = self.sprite.layer
        x, y = win32api.GetCursorPos()
        (lx, ly), (w, h) = layer.pos, layer.size
        over = layer.shown and lx <= x < lx + w and ly <= y < ly + h
        self.alpha += ((self.lo if over else 255) - self.alpha) * 0.35
        self.sprite.fade(round(self.alpha))


class Pulse:
    """Breathes a sprite's opacity between lo and 255, and fades it in when it first shows."""

    def __init__(self, sprite: Sprite, lo: int = 150, period: float = 1.6) -> None:
        self.sprite = sprite
        self.lo = lo
        self.period = period
        self.t0 = time.monotonic()
        self.running = False

    def start(self) -> None:
        if not self.running:
            self.t0 = time.monotonic()
            self.running = True
            self.sprite.layer.every(33, self._tick)

    def stop(self) -> None:
        if self.running:
            self.running = False
            self.sprite.layer.every(0, None)

    def _tick(self) -> None:
        t = time.monotonic() - self.t0
        fade_in = min(1.0, t / 0.35)
        wave = (math.sin(t * 2 * math.pi / self.period) + 1) / 2
        self.sprite.fade(round((self.lo + (255 - self.lo) * wave) * fade_in))
