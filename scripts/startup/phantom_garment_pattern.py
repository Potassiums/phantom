# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: pattern piece geometry for the Garment workspace (no Blender data here, only math).

A piece's outline is a closed list of points ``(x, y, curve)`` in counter-clockwise order. Points
with ``curve`` False are *corners*: the outline is split into *segments* at corners, and is sharp
there. Points with ``curve`` True shape the segment between two corners: the segment is a smooth
(centripetal Catmull-Rom) curve through them. With no corners at all, the outline is one closed,
smooth segment (an ellipse, say).

``build_piece`` meshes an outline into evenly sized triangles about ``spacing`` apart: the outline
is resampled at that spacing, the inside is filled with a triangular lattice, and a constrained
Delaunay triangulation joins them.
"""

import math

import numpy as np
from mathutils import Vector
from mathutils.geometry import delaunay_2d_cdt

# Samples per curve span when drawing or measuring outlines.
CURVE_SAMPLES = 12


def _centripetal(p0, p1, p2, p3, n):
    """``n`` points of the centripetal Catmull-Rom span from ``p1`` to ``p2`` (excluding ``p2``)."""
    def knot(t, a, b):
        return t + max(np.linalg.norm(b - a), 1e-9) ** 0.5

    t0 = 0.0
    t1 = knot(t0, p0, p1)
    t2 = knot(t1, p1, p2)
    t3 = knot(t2, p2, p3)
    out = []
    for t in np.linspace(t1, t2, n, endpoint=False):
        a1 = (t1 - t) / (t1 - t0) * p0 + (t - t0) / (t1 - t0) * p1
        a2 = (t2 - t) / (t2 - t1) * p1 + (t - t1) / (t2 - t1) * p2
        a3 = (t3 - t) / (t3 - t2) * p2 + (t - t2) / (t3 - t2) * p3
        b1 = (t2 - t) / (t2 - t0) * a1 + (t - t0) / (t2 - t0) * a2
        b2 = (t3 - t) / (t3 - t1) * a2 + (t - t1) / (t3 - t1) * a3
        out.append((t2 - t) / (t2 - t1) * b1 + (t - t1) / (t2 - t1) * b2)
    return out


def corner_indices(points):
    return [i for i, p in enumerate(points) if not p[2]]


def outline_segments(points, samples=CURVE_SAMPLES):
    """Dense polylines (arrays of 2D points), one per segment, each from its corner to the next
    (both included). Segment ``k`` starts at the ``k``-th corner."""
    n = len(points)
    if n < 2:
        return []
    pts = np.array([(p[0], p[1]) for p in points], dtype=np.float64)
    corners = corner_indices(points)
    if not corners:
        loop = []
        for i in range(n):
            loop += _centripetal(pts[i - 1], pts[i], pts[(i + 1) % n], pts[(i + 2) % n], samples)
        loop.append(pts[0])
        return [np.array(loop)]
    segments = []
    for k, c in enumerate(corners):
        nxt = corners[(k + 1) % len(corners)]
        chain = [c]
        i = (c + 1) % n
        while True:
            chain.append(i)
            if i == nxt:
                break
            i = (i + 1) % n
        q = pts[chain]
        if len(q) == 2:
            segments.append(q.copy())
            continue
        # Phantom end points mirror the neighbours, so the curve leaves each corner towards the
        # first curve point.
        ext = np.vstack((2.0 * q[0] - q[1], q, 2.0 * q[-1] - q[-2]))
        poly = []
        for j in range(1, len(ext) - 2):
            poly += _centripetal(ext[j - 1], ext[j], ext[j + 1], ext[j + 2], samples)
        poly.append(q[-1])
        segments.append(np.array(poly))
    return segments


def outline_spans(points, samples=CURVE_SAMPLES):
    """Dense polylines between consecutive outline points: [(i, polyline from point i to i+1)]."""
    n = len(points)
    corners = corner_indices(points)
    spans = []
    for k, poly in enumerate(outline_segments(points, samples)):
        if not corners:
            chain = list(range(n)) + [0]
        else:
            c, nxt = corners[k], corners[(k + 1) % len(corners)]
            chain = [c]
            i = (c + 1) % n
            while True:
                chain.append(i)
                if i == nxt:
                    break
                i = (i + 1) % n
        if len(chain) == 2:
            spans.append((chain[0], poly))
            continue
        for j in range(len(chain) - 1):
            spans.append((chain[j], poly[j * samples:(j + 1) * samples + 1]))
    return spans


def sub_polyline(poly, start, end):
    """The stretch of a polyline between arc-length fractions ``start`` and ``end`` (in that
    direction)."""
    lo, hi = min(start, end), max(start, end)
    params = arc_params(poly)
    inner = poly[(params > lo) & (params < hi)]
    out = np.vstack((point_at(poly, lo)[None], inner, point_at(poly, hi)[None]))
    return out[::-1] if start > end else out


def polyline_length(poly):
    return float(np.linalg.norm(np.diff(poly, axis=0), axis=1).sum()) if len(poly) > 1 else 0.0


def arc_params(poly):
    d = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    s = np.concatenate(([0.0], np.cumsum(d)))
    return s / s[-1] if s[-1] > 0.0 else np.linspace(0.0, 1.0, len(poly))


def point_at(poly, t):
    """The point at arc-length fraction ``t`` (0-1) along a polyline."""
    params = arc_params(poly)
    return np.array([np.interp(t, params, poly[:, k]) for k in range(poly.shape[1])])


def project(poly, point):
    """(distance, arc-length fraction) of the nearest point on a polyline."""
    a, b = poly[:-1], poly[1:]
    ab = b - a
    length2 = np.maximum((ab * ab).sum(axis=1), 1e-18)
    t = np.clip(((point - a) * ab).sum(axis=1) / length2, 0.0, 1.0)
    nearest = a + ab * t[:, None]
    dist = np.linalg.norm(nearest - point, axis=1)
    i = int(dist.argmin())
    lengths = np.sqrt(length2)
    total = lengths.sum()
    along = (lengths[:i].sum() + lengths[i] * t[i]) / total if total > 0.0 else 0.0
    return float(dist[i]), float(along)


def resample(poly, spacing):
    """Evenly spaced points along a polyline, both ends included."""
    length = polyline_length(poly)
    count = max(1, int(round(length / spacing)))
    params = arc_params(poly)
    t = np.linspace(0.0, 1.0, count + 1)
    return np.column_stack((np.interp(t, params, poly[:, 0]), np.interp(t, params, poly[:, 1])))


def signed_area(points):
    p = np.array([(q[0], q[1]) for q in points], dtype=np.float64)
    if len(p) < 3:
        return 0.0
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


def _inside(points, polygon):
    """Even-odd test of many points against a polygon (both N x 2 arrays)."""
    px, py = points[:, 0:1], points[:, 1:2]
    x1, y1 = polygon[:, 0], polygon[:, 1]
    x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
    crosses = (y1 > py) != (y2 > py)
    with np.errstate(divide='ignore', invalid='ignore'):
        x_at = (x2 - x1) * (py - y1) / (y2 - y1) + x1
    return (crosses & (px < x_at)).sum(axis=1) % 2 == 1


def _distance_to_loop(points, polygon):
    a = polygon
    b = np.roll(polygon, -1, axis=0)
    ab = b - a
    length2 = np.maximum((ab * ab).sum(axis=1), 1e-18)
    best = np.full(len(points), np.inf)
    # In chunks, to keep the point x edge arrays small.
    for start in range(0, len(points), 2048):
        p = points[start:start + 2048, None, :]
        t = np.clip(((p - a) * ab).sum(axis=2) / length2, 0.0, 1.0)
        d = np.linalg.norm(a + ab * t[..., None] - p, axis=2)
        best[start:start + 2048] = d.min(axis=1)
    return best


def build_piece(points, spacing):
    """Mesh an outline. Returns (verts N x 2, faces, boundary order per vertex or -1, corner flags).

    Boundary vertices are numbered along the outline starting at the first corner, so segment
    ``k`` runs between the ``k``-th and ``k+1``-th flagged corners.
    """
    segments = outline_segments(points)
    boundary, corner = [], []
    for poly in segments:
        samples = resample(poly, spacing)
        for j, p in enumerate(samples[:-1]):
            boundary.append(p)
            corner.append(j == 0)
    boundary = np.array(boundary)
    nb = len(boundary)
    if nb < 3:
        return np.zeros((0, 2)), [], np.zeros(0, dtype=np.int64), np.zeros(0, dtype=bool)

    lo, hi = boundary.min(axis=0), boundary.max(axis=0)
    dy = spacing * math.sqrt(3.0) / 2.0
    rows = []
    for r, y in enumerate(np.arange(lo[1] + dy * 0.5, hi[1], dy)):
        xs = np.arange(lo[0] + (spacing * 0.5 if r % 2 else 0.0), hi[0], spacing)
        rows.append(np.column_stack((xs, np.full(len(xs), y))))
    interior = np.vstack(rows) if rows else np.zeros((0, 2))
    if len(interior):
        keep = _inside(interior, boundary)
        interior = interior[keep]
    if len(interior):
        interior = interior[_distance_to_loop(interior, boundary) > 0.55 * spacing]

    coords = np.vstack((boundary, interior))
    edges = [(i, (i + 1) % nb) for i in range(nb)]
    result = delaunay_2d_cdt([Vector(c) for c in coords], edges, [list(range(nb))], 1, 1e-9, True)
    out_verts, _out_edges, out_faces, orig_verts = result[0], result[1], result[2], result[3]

    verts = np.array([(v.x, v.y) for v in out_verts], dtype=np.float64)
    order = np.full(len(verts), -1, dtype=np.int64)
    flags = np.zeros(len(verts), dtype=bool)
    for i, orig in enumerate(orig_verts):
        for o in orig:
            if o < nb:
                order[i] = o
                flags[i] = corner[o]
                break
    faces = []
    for f in out_faces:
        f = list(f)
        a, b, c = verts[f[0]], verts[f[1]], verts[f[2]]
        if (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) < 0.0:
            f.reverse()
        faces.append(f)
    return verts, faces, order, flags


def rectangle(width, height):
    w, h = width / 2.0, height / 2.0
    return [(-w, -h, False), (w, -h, False), (w, h, False), (-w, h, False)]


def ellipse(width, height, count=8):
    return [(0.5 * width * math.cos(2.0 * math.pi * i / count),
             0.5 * height * math.sin(2.0 * math.pi * i / count), True) for i in range(count)]


def counter_clockwise(points):
    """The points in counter-clockwise order (reversed if they were drawn clockwise)."""
    if signed_area(points) >= 0.0:
        return list(points)
    # Keep the first point first, so the first corner (and segment 0) stays where it was drawn.
    return [points[0]] + list(reversed(points[1:]))


def register():
    pass


def unregister():
    pass
