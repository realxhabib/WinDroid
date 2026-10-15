"""Draws the Android Desktop icon (assets/icon.png and icon.ico): a green-to-blue rounded square with
a white app window, the same mark as the launcher's header.

    python tools/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parents[1] / "androiddesktop" / "assets"
S = 1024  # drawn large, scaled down for crisp edges


def gradient(size: int) -> Image.Image:
    top, bottom = (0x5B, 0xF0, 0xA0), (0x2C, 0x8C, 0xFF)
    g = Image.new("RGB", (size, size))
    px = g.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
    return g


def draw() -> Image.Image:
    small = gradient(64).resize((S, S), Image.BICUBIC)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=round(S * 9 / 32), fill=255)
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(small, (0, 0), mask)
    d = ImageDraw.Draw(icon)
    u = S / 32  # the launcher's SVG is drawn on a 32-unit grid
    stroke = round(2 * u)
    d.rounded_rectangle((7.5 * u, 9 * u, 24.5 * u, 23 * u), radius=round(3.2 * u), outline="white", width=stroke)
    d.line((7.5 * u, 13.2 * u, 24.5 * u, 13.2 * u), fill="white", width=stroke)
    for cx in (10.6, 13.2):
        r = 0.9 * u
        d.ellipse((cx * u - r, 11.1 * u - r, cx * u + r, 11.1 * u + r), fill="white")
    return icon


def main() -> None:
    icon = draw()
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon.resize((256, 256), Image.LANCZOS).save(ASSETS / "icon.png")
    icon.save(ASSETS / "icon.ico", sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    print("wrote", ASSETS / "icon.png", ASSETS / "icon.ico")


if __name__ == "__main__":
    main()
