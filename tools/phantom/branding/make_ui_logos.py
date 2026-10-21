"""Generate Phantom's monochrome in-app logos (Blender's icon SVGs are white-filled).

Writes:
  release/datafiles/icons_svg/blender.svg             (small menu logo)
  release/datafiles/icons_svg/blender_logo_large.svg  (ghost + PHANTOM wordmark)
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # tools/phantom/branding -> repository root
SRC = REPO / "release" / "datafiles" / "icons_svg"

# Ghost outline traced from the app icon, in a 1024 box, with the eyes as holes.
GHOST = (
    "M560 188 C688 188 764 276 772 404 C800 520 818 650 798 756 "
    "C792 794 772 812 744 800 C716 790 700 770 670 780 "
    "C640 790 626 822 590 832 C552 842 522 806 486 800 "
    "C452 794 430 792 408 774 C364 754 300 724 254 694 "
    "C224 676 228 640 262 630 C304 616 334 598 348 556 "
    "C364 498 348 420 362 356 C384 252 462 188 560 188 Z "
    "M503 378 A25 32 0 1 0 553 378 A25 32 0 1 0 503 378 Z "
    "M623 378 A25 32 0 1 0 673 378 A25 32 0 1 0 623 378 Z"
)
GHOST_CENTER = (523, 517)


def ghost(cx, cy, scale):
    gx, gy = GHOST_CENTER
    return (f'<path fill="#fff" fill-rule="evenodd" '
            f'transform="translate({cx} {cy}) scale({scale}) translate({-gx} {-gy})" '
            f'd="{GHOST}"/>')


# Angular block letters: width 420, cap height 350..1250, stroke 170.
T, TOP, BOT, W = 170, 350, 1250, 420


def letters(x):
    r, m = x + W, x + W / 2
    return {
        "P": f"M{x} {TOP} H{r} V850 H{x+T} V{BOT} H{x} Z "
             f"M{x+T} {TOP+T} H{r-T} V{850-T} H{x+T} Z",
        "H": f"M{x} {TOP} H{x+T} V715 H{r-T} V{TOP} H{r} V{BOT} H{r-T} V885 "
             f"H{x+T} V{BOT} H{x} Z",
        "A": f"M{x} {BOT} V470 L{x+120} {TOP} H{r-120} L{r} 470 V{BOT} H{r-T} V885 "
             f"H{x+T} V{BOT} Z M{x+T} 540 H{r-T} V715 H{x+T} Z",
        "N": f"M{x} {BOT} V{TOP} H{x+T} L{r-T} 960 V{TOP} H{r} V{BOT} H{r-T} "
             f"L{x+T} 640 V{BOT} Z",
        "T": f"M{x} {TOP} H{r} V{TOP+T} H{m+T/2} V{BOT} H{m-T/2} V{TOP+T} H{x} Z",
        "O": f"M{x+100} {TOP} H{r-100} L{r} 450 V1150 L{r-100} {BOT} H{x+100} "
             f"L{x} 1150 V450 Z M{x+T} {TOP+T} H{r-T} V{BOT-T} H{x+T} Z",
        "M": f"M{x} {BOT} V{TOP} H{x+T} L{m} 640 L{r-T} {TOP} H{r} V{BOT} H{r-T} "
             f"V650 L{m} 900 L{x+T} 650 V{BOT} Z",
    }


def wordmark(x0, step):
    paths = []
    for i, ch in enumerate("PHANTOM"):
        paths.append(letters(x0 + i * step)[ch])
    return f'<path fill="#fff" fill-rule="evenodd" d="{" ".join(paths)}"/>'


def main():
    small = (
        '<svg height="1500" viewBox="0 0 1800 1500" width="1800" '
        'xmlns="http://www.w3.org/2000/svg">'
        f'{ghost(900, 750, 2.1)}</svg>\n'
    )
    large = (
        '<svg height="1600" viewBox="0 0 5236.3638 1600" width="5236.3638" '
        'xmlns="http://www.w3.org/2000/svg">'
        f'{ghost(760, 800, 2.1)}{wordmark(1700, 500)}</svg>\n'
    )
    (SRC / "blender.svg").write_text(small, newline="\n")
    (SRC / "blender_logo_large.svg").write_text(large, newline="\n")
    print("wrote", SRC / "blender.svg", "and", SRC / "blender_logo_large.svg")


if __name__ == "__main__":
    main()
