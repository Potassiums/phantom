"""Export the Phantom icon to every size the build needs.

Usage: python make_icons.py [source_image]
"""
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).parent
REPO = Path(__file__).resolve().parents[3]  # tools/phantom/branding -> repository root
OUT = HERE / "out"
ICO_DEST = REPO / "release" / "windows" / "icons" / "winblender.ico"
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
PNG_SIZES = [16, 32, 48, 64, 128, 256, 512, 1024]


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "phantom_source.webp"
    im = Image.open(src).convert("RGBA")
    OUT.mkdir(exist_ok=True)

    for size in PNG_SIZES:
        im.resize((size, size), Image.LANCZOS).save(OUT / f"phantom_{size}.png")

    # Windows embeds each size separately; Pillow downsamples from the largest.
    im.resize((256, 256), Image.LANCZOS).save(
        OUT / "phantom.ico", sizes=[(s, s) for s in ICO_SIZES])
    # The Windows executable icon.
    im.resize((256, 256), Image.LANCZOS).save(ICO_DEST, sizes=[(s, s) for s in ICO_SIZES])

    print(f"Wrote {len(PNG_SIZES)} PNGs and phantom.ico to {OUT}, and {ICO_DEST}")


if __name__ == "__main__":
    main()
