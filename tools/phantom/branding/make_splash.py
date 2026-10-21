"""Render Phantom's 1000x500 splash image (Blender draws the version text on top).

Usage: python make_splash.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent
REPO = Path(__file__).resolve().parents[3]  # tools/phantom/branding -> repository root
DEST = REPO / "release" / "datafiles" / "splash.png"
W, H = 1000, 500
# Bahnschrift ships with Windows 10 and later.
FONT = r"C:\Windows\Fonts\bahnschrift.ttf"


def vertical_gradient(top, bottom):
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        t = y / (H - 1)
        c = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
        for x in range(W):
            px[x, y] = c
    return img


def glow(size, center, radius, color, blur):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = center
    rx, ry = radius
    d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=color)
    return layer.filter(ImageFilter.GaussianBlur(blur))


def main():
    img = vertical_gradient((0x34, 0x3a, 0x4b), (0x1c, 0x20, 0x2a)).convert("RGBA")

    # Lavender haze behind the ghost, orange ember along the bottom edge.
    img.alpha_composite(glow((W, H), (300, 230), (230, 200), (140, 134, 255, 70), 90))
    img.alpha_composite(glow((W, H), (500, 540), (520, 70), (255, 138, 61, 120), 60))

    ghost = Image.open(HERE / "phantom_source.webp").convert("RGBA")
    # Crop the ghost out of its app tile (tile spans ~4%-96%; ghost ~17%-78% x, 14%-86% y).
    gw, gh = ghost.size
    ghost = ghost.crop((int(gw * 0.16), int(gh * 0.13), int(gw * 0.80), int(gh * 0.88)))
    # Knock out the tile's dark background so only the ghost remains.
    px = ghost.load()
    for y in range(ghost.height):
        for x in range(ghost.width):
            r, g, b, a = px[x, y]
            lum = max(r, g, b)
            if lum < 100:
                px[x, y] = (r, g, b, 0)
            elif lum < 150:
                px[x, y] = (r, g, b, int(a * (lum - 100) / 50))
    gh_target = 360
    ghost = ghost.resize((int(ghost.width * gh_target / ghost.height), gh_target), Image.LANCZOS)
    img.alpha_composite(ghost, (300 - ghost.width // 2, 60))

    d = ImageDraw.Draw(img)
    title = ImageFont.truetype(FONT, 118)
    title.set_variation_by_axes([700, 75])  # weight, width (condensed)
    sub = ImageFont.truetype(FONT, 22)
    d.text((500, 170), "PHANTOM", font=title, fill=(232, 236, 255, 255))
    d.rectangle((504, 310, 640, 314), fill=(255, 138, 61, 255))
    d.text((504, 330), "a Blender fork for sculptors", font=sub, fill=(170, 176, 204, 255))

    img.convert("RGB").save(DEST)
    print("wrote", DEST)


if __name__ == "__main__":
    main()
