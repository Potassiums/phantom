"""Write Phantom's toolbar icons in Blender's icon geometry format (`.dat`, triangles).

The Transpose icon follows ZBrush's: a big ring with move arrows around it, the action line
through a small middle dot, and a smaller ring at the other end.

Usage: python make_tool_icons.py [output_dir]
"""
import math
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # tools/phantom/branding -> repository root
DEFAULT_OUT = REPO / "release" / "datafiles" / "icons"
# Blender's tool icons: light gray, drawn within 40..215 of a 0..255 square (y up).
COLOR = (229, 229, 229, 255)


def disc(cx, cy, r, segments=28):
    tris = []
    for i in range(segments):
        a0, a1 = 2 * math.pi * i / segments, 2 * math.pi * (i + 1) / segments
        tris.append(((cx, cy), (cx + r * math.cos(a0), cy + r * math.sin(a0)),
                     (cx + r * math.cos(a1), cy + r * math.sin(a1))))
    return tris


def ring(cx, cy, r_in, r_out, segments=32):
    tris = []
    for i in range(segments):
        a0, a1 = 2 * math.pi * i / segments, 2 * math.pi * (i + 1) / segments
        p0 = (cx + r_in * math.cos(a0), cy + r_in * math.sin(a0))
        p1 = (cx + r_out * math.cos(a0), cy + r_out * math.sin(a0))
        p2 = (cx + r_in * math.cos(a1), cy + r_in * math.sin(a1))
        p3 = (cx + r_out * math.cos(a1), cy + r_out * math.sin(a1))
        tris += [(p0, p1, p3), (p0, p3, p2)]
    return tris


def bar(x0, y0, x1, y1, width):
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    nx, ny = -dy / length * width / 2, dx / length * width / 2
    a, b = (x0 + nx, y0 + ny), (x0 - nx, y0 - ny)
    c, d = (x1 - nx, y1 - ny), (x1 + nx, y1 + ny)
    return [(a, b, c), (a, c, d)]


def arrow(cx, cy, angle, size):
    """A small arrow head pointing along `angle`, its tip at distance 0 from (cx, cy)."""
    ux, uy = math.cos(angle), math.sin(angle)
    px, py = -uy, ux
    tip = (cx, cy)
    base = (cx - ux * size, cy - uy * size)
    return [(tip, (base[0] + px * size * 0.7, base[1] + py * size * 0.7),
             (base[0] - px * size * 0.7, base[1] - py * size * 0.7))]


def transpose_icon():
    big = (92, 164)
    small = (172, 84)
    tris = []
    # The big end: outer ring, inner ring and dot, with move arrows around it.
    tris += ring(*big, 31, 38)
    tris += ring(*big, 17, 22)
    tris += disc(*big, 8)
    for angle in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
        tip = (big[0] + 52 * math.cos(angle), big[1] + 52 * math.sin(angle))
        tris += arrow(*tip, angle, 11)
    # The action line from ring to ring, with the middle dot.
    dx, dy = small[0] - big[0], small[1] - big[1]
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    tris += bar(big[0] + ux * 38, big[1] + uy * 38, small[0] - ux * 25, small[1] - uy * 25, 6)
    mid = ((big[0] + small[0]) / 2, (big[1] + small[1]) / 2)
    tris += disc(*mid, 9)
    # The small end: ring and dot.
    tris += ring(*small, 19, 25)
    tris += disc(*small, 8)
    return tris


def write_icon(path, tris):
    coords = bytearray()
    colors = bytearray()
    for tri in tris:
        for x, y in tri:
            coords += struct.pack("BB", max(0, min(255, round(x))), max(0, min(255, round(y))))
            colors += bytes(COLOR)
    with open(path, "wb") as fh:
        fh.write(b"VCO\x00" + bytes((255, 255, 0, 0)) + coords + colors)
    print(f"{path.name}: {len(tris)} triangles")


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    write_icon(out / "phantom.transpose.dat", transpose_icon())


if __name__ == "__main__":
    main()
