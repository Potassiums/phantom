"""
phantom_zbrush_io.py (from zbrush_io.py) — read ZBrush .ztl / .ZPR geometry. Single file, stdlib + numpy only.
Inside Phantom the array decoding runs in C++ (the built-in `_phantom_zbrush` module, blocks in
parallel); elsewhere the Python decoder below gives the same bytes.

VERIFIED LAYOUT (see notes at the bottom for what is and is not proven)
-----------------------------------------------------------------------
Container
    header 92 bytes: banner[50], ..., magic u32 @0x4C = extension reversed ('LTZ','RPZ'),
                     version u32 @0x50, date[8] @0x54.  First record @0x5C.
    record:  u16 0x8003 | u16 p3(=1) | u16 type | u32 size | payload | u32 0x8803
             size includes header and trailer; children live in [+10, +size-4).

Arrays are `type 6` records found by scanning for the 6-byte signature
    03 80 01 00 06 00
anywhere in the file — some live inside payloads that do not begin with a record
(e.g. type 92 starts with a GUID), so a record-chain walk will miss them.

    type 6 payload: u32 byteLen | u16 stride | u16 aux | .mem container

.mem container
    u32 1 | u32 blockSize | u32 nBlocks | nBlocks x u32 sizes | u32 0 | blocks
    block = u32 codecTag + payload.   blockSize = 131072 - (131072 % stride).

Codecs
    tag 1        byte-plane RLE (PackBits variant)
    tag 3        half-float array, u32 count + f16, element-major (no transpose)
    tag 4/5/6    byte-plane RLE, then int32 * 2**-20 -> float32
    Some blocks carry a u32 prefix before the tag; retried at offset 4.

RLE control byte:  0 = end | 1..127 = repeat next byte n times | 0x80..0xFF = literal run of (256-c)

Transposition is PER BLOCK (byte-plane, at the element stride), not over the whole stream.

Array meanings (by stride)
    12  points   3 x float32
    16  faces    4 x u32; 0xFFFFFFFF in slot 4 marks a triangle. Some stride-16 arrays hold floats
                 (4 per point), so a face array is one whose values are all indices.
    32  per-face UV, 8 floats — NOT exported, mapping unverified
    4   per-face polygroup id, one u32 per base face
    8   per-point block (polypaint colour and flags) — not exported

Subtools
    In 2025 files every subtool is a type-92 record holding its point arrays, followed by a type-19
    record holding its faces, UVs and polygroups.  The type-92 record holds, in order:
        the base level's points (not always present) + a stride-8 block,
        the top subdivision level's points (when the subtool has levels) + a stride-8 block,
        sometimes one more stride-12 array that is not geometry (the same 61,013 points in several
        subtools of one file): pairing it with the faces gave spiky, broken meshes.
    Faces are stored for the base level only.  Pairing: the point arrays since the previous face
    array go with the next face array by count, base = max index + 1 (a few unused points
    allowed), top = base + what L Catmull-Clark levels add; anything else is ignored.  Older
    projects can hold several arrays of one size: the exact, coherent one wins.  A top level must
    also pass a check of its first points against the faces (older files keep a lower level of
    the same size in another order).  Faces left unpaired fall back to the best-coherence point
    array.

Undo history
    A project's type-123 record holds its undo history, with whole earlier states of meshes (up
    to 10x more "subtools" than the project has).  Those are skipped, unless they are all there is.

Subdivision (verified on every vertex of a 3-level, 241,620 -> 15,461,760 point subtool)
    Level k+1 = level k's points, then per face in order: the points of its edges not seen yet in
    corner order, then its face point.  Face (v0, v1, v2, v3)'s children follow in corner order and
    child k keeps v_k in slot k: (v0,e0,c,e3), (e0,v1,e1,c), (c,e1,v2,e2), (e3,c,e2,v3), with e_k the
    point of edge (v_k, v_k+1) and c the face point; a triangle's three children likewise.
    The top level's point array holds every level's points in that order.

Names
    Each subtool's name is a type-12 record just before its points (a path gives its basename).
    Each tool is a type 1004 record: payload starts "ZTool" + 40-byte tool type + a source path,
    followed by a type 12 record holding the tool's name, the fallback for its subtools.

Transforms: none applied and none found; placement is already in the point coordinates.
"""

from __future__ import annotations

import bisect
import mmap
import os
import struct

import numpy as np

try:
    import _phantom_zbrush as _native
except ImportError:
    _native = None

# ---------------------------------------------------------------- constants

TYPE6 = b'\x03\x80\x01\x00\x06\x00'
ZTOOL = b'\x03\x80\x01\x00\xec\x03'
NAME_REC = b'\x03\x80\x01\x00\x0c\x00'
UNDO_REC = b'\x03\x80\x01\x00\x7b\x00'

INT_DEQUANT = 2.0 ** -20
RLE_TAGS = (1, 4, 5, 6)
TRI_SENTINEL = 0xFFFFFFFF
MAX_LEVELS = 8


class UnsupportedCodec(Exception):
    """A block uses a codec this reader does not implement. Array is skipped."""


class UnsupportedFormat(Exception):
    """The file is a different (legacy) ZBrush container."""


def _u16(b, o):
    return struct.unpack_from('<H', b, o)[0]


def _u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


# ---------------------------------------------------------------- container

def read_header(b):
    """Diagnostics only: 92-byte file header."""
    return {
        'banner': bytes(b[0:50]).split(b'\0')[0].decode('latin-1'),
        'magic': bytes(b[0x4C:0x50]).rstrip(b'\0').decode('latin-1'),
        'version': _u32(b, 0x50),
        'date': bytes(b[0x54:0x5C]),
    }


# ---------------------------------------------------------------- codec

def rle_decode(src, expected):
    """PackBits variant used by every block. Truncates to `expected` bytes."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        i += 1
        if c == 0:
            break
        if c < 0x80:                      # repeat next byte c times
            if i >= n:
                break
            out += bytes((src[i],)) * c
            i += 1
        else:                             # literal run of 256-c bytes
            k = 256 - c
            out += src[i:i + k]
            i += k
        if len(out) >= expected:
            break
    return bytes(out[:expected])


def _unscatter(data, stride):
    """byte-plane transpose: dst[lane + e*stride] = src[lane*n + e]."""
    n = len(data) // stride
    dst = bytearray(n * stride)
    for lane in range(stride):
        dst[lane::stride] = data[lane * n:(lane + 1) * n]
    return bytes(dst)


def _dequant_ints(data, stride):
    if stride % 4 or len(data) % 4:
        raise UnsupportedCodec(f'int dequant needs 4-byte alignment (stride {stride})')
    v = np.frombuffer(data, dtype='<i4').astype(np.float32)
    return (v * np.float32(INT_DEQUANT)).tobytes()


def parse_mem(b, off):
    """Read a .mem container header. Returns dict with sizes and blocks."""
    v1, block_size, n_blocks = _u32(b, off), _u32(b, off + 4), _u32(b, off + 8)
    p = off + 12
    sizes = []
    while True:
        s = _u32(b, p)
        p += 4
        if s == 0 or len(sizes) > 1_000_000:
            break
        sizes.append(s)
    blocks = []
    for s in sizes:
        tag = _u32(b, p)
        blocks.append((tag, bytes(b[p + 4:p + s])))
        p += s
    return {'v1': v1, 'blockSize': block_size, 'nBlocks': n_blocks,
            'sizes': sizes, 'blocks': blocks}


def decode_blocks(mem, stride):
    """Decode all blocks of one .mem container to element-major bytes."""
    out = bytearray()
    quantised = False
    for tag, payload in mem['blocks']:
        if tag not in RLE_TAGS and tag != 3 and len(payload) >= 8:
            alt = _u32(payload, 0)        # some blocks carry a u32 prefix
            if alt in RLE_TAGS or alt == 3:
                tag, payload = alt, payload[4:]
        if tag in RLE_TAGS:
            blk = rle_decode(payload, mem['blockSize'])
            out += _unscatter(blk, stride) if stride > 1 else blk
            quantised = quantised or tag in (4, 5, 6)
        elif tag == 3:
            cnt = _u32(payload, 0)
            out += np.frombuffer(payload[4:4 + cnt * 2], dtype='<f2').astype('<f4').tobytes()
        else:
            raise UnsupportedCodec(f'codec tag {tag}')
    data = bytes(out)
    if quantised:
        data = _dequant_ints(data, stride)
    return data


# ---------------------------------------------------------------- arrays

def find_arrays(b):
    """All type-6 records in the file, in offset order."""
    out, i = [], b.find(TYPE6)
    while i != -1:
        size = _u32(b, i + 6)
        pay = i + 10
        if 14 <= size < len(b) and pay + 8 < len(b):
            blen, stride, aux = _u32(b, pay), _u16(b, pay + 4), _u16(b, pay + 6)
            if 0 < stride <= 0x400 and blen > 0 and blen % stride == 0:
                out.append({'off': i, 'end': i + size, 'byteLen': blen, 'n': blen // stride,
                            'stride': stride, 'aux': aux, 'payload': pay})
        i = b.find(TYPE6, i + 1)
    return out


def find_records(b, signature):
    """(start, end) of every record with this 6-byte signature whose trailer checks out."""
    out, i = [], b.find(signature)
    while i != -1:
        size = _u32(b, i + 6)
        if 14 <= size and i + size <= len(b) and _u32(b, i + size - 4) == 0x8803:
            out.append((i, i + size))
        i = b.find(signature, i + 1)
    return out


def decode_array(b, rec):
    """Decode one array to bytes. Raises UnsupportedCodec."""
    if _native is not None:
        try:
            return _native.decode_array(b, rec['payload'] + 8, rec['stride'], rec['byteLen'])
        except ValueError as ex:
            raise UnsupportedCodec(str(ex)) from None
    mem = parse_mem(b, rec['payload'] + 8)
    data = decode_blocks(mem, rec['stride'])
    if len(data) < rec['byteLen']:
        raise UnsupportedCodec(f'short decode {len(data)} < {rec["byteLen"]}')
    return data[:rec['byteLen']]


def _name_string(b, j):
    """The printable string of the type-12 record at `j`, or None."""
    nsz = _u32(b, j + 6)
    if not 14 <= nsz <= 300:
        return None
    raw = bytes(b[j + 10:j + 10 + nsz - 14]).lstrip(b'\0')
    s = raw.split(b'\0')[0]
    if not s or not all(32 <= c < 127 for c in s):
        return None
    return s.decode('latin-1')


def _basename(s):
    return os.path.splitext(s.replace('\\', '/').split('/')[-1])[0] or None


def read_names(b):
    """[(start, end, name)] for every ZTool record."""
    out, i = [], b.find(ZTOOL)
    while i != -1:
        size = _u32(b, i + 6)
        if 14 < size < len(b):
            end = i + size
            strings, j = [], b.find(NAME_REC, i)
            while j != -1 and j < end:
                s = _name_string(b, j)
                if s is not None:
                    strings.append(s)
                    if '\\' not in s and '/' not in s:
                        break             # the first plain string is the name: skip the rest
                j = b.find(NAME_REC, j + 1)
            plain = [s for s in strings if '\\' not in s and '/' not in s]
            name = plain[0] if plain else None
            if name is None and strings:
                name = _basename(strings[0])
            out.append((i, end, name))
        i = b.find(ZTOOL, i + 1)
    return out


def read_name_records(b, ranges):
    """{offset: name} for the type-12 records inside the given (start, end) byte ranges."""
    out = {}
    for lo, hi in ranges:
        j = b.find(NAME_REC, lo, hi)
        while j != -1:
            s = _name_string(b, j)
            if s is not None:
                out[j] = s
            j = b.find(NAME_REC, j + 1, hi)
    return out


# ---------------------------------------------------------------- subdivision levels

def _corner_edges(Q, tri):
    """Per face corner k, the edge (v_k, v_k+1); `valid` masks the 4th corner of triangles."""
    nxt = np.roll(Q, -1, axis=1)
    nxt[tri, 2] = Q[tri, 0]
    valid = np.ones(Q.shape, dtype=bool)
    valid[tri, 3] = False
    return nxt, valid


def count_edges(Q, tri, nv):
    nxt, valid = _corner_edges(Q, tri)
    a, c = Q[valid].astype(np.int64), nxt[valid].astype(np.int64)
    return len(np.unique(np.minimum(a, c) * nv + np.maximum(a, c)))


def level_vertex_counts(Q, tri, nv, levels=MAX_LEVELS):
    """S[L - 1]: how many points L subdivision levels add to a base level."""
    faces = len(Q)
    corners = 4 * faces - int(np.count_nonzero(tri))
    edges = count_edges(Q, tri, nv)
    out, added = [], 0
    for _ in range(levels):
        added += edges + faces
        out.append(added)
        edges, faces = 2 * edges + corners, corners
        corners = 4 * faces
    return out


def _subdivide_step(nv, Q, tri, children=True):
    """One level of ZBrush's subdivision topology.

    Returns (new vertex count, child faces (F', 4) int32 in ZBrush's order, parent face of each
    child, child number k, and per new point its face and slot: 0..3 = edge of corner k, 4 = face).
    Without `children` only the points are returned (the child entries are None).
    """
    F = len(Q)
    nxt, valid = _corner_edges(Q, tri)
    key = (np.minimum(Q, nxt).astype(np.int64) * nv + np.maximum(Q, nxt)).ravel()
    vk = np.flatnonzero(valid.ravel())
    _, first, inverse = np.unique(key[vk], return_index=True, return_inverse=True)
    corner_first = vk[first][inverse.ravel()]
    del key, first, inverse
    # Events per face: its 4 corners' edges (when first seen), then its face point.
    events = np.zeros((F, 5), dtype=bool)
    events.reshape(-1)[np.arange(F) * 5 + 4] = True
    new_edge = vk[corner_first == vk]
    events[new_edge // 4, new_edge % 4] = True
    ev = np.flatnonzero(events.ravel())
    nv2 = nv + len(ev)
    ev_face, ev_slot = (ev // 5).astype(np.int32), (ev % 5).astype(np.int8)
    if not children:
        return nv2, None, None, None, ev_face, ev_slot
    del ev
    ids = (np.cumsum(events.ravel(), dtype=np.int64) - 1 + nv).reshape(F, 5)
    corner_id = np.full(F * 4, -1, dtype=np.int64)
    corner_id[vk] = ids[:, :4].ravel()[corner_first]
    del corner_first
    e = corner_id.reshape(F, 4)
    e_prev = np.roll(e, 1, axis=1)
    e_prev[tri, 0] = e[tri, 2]
    c = np.broadcast_to(ids[:, 4:5], (F, 4))
    # Child k = (v_k, e_k, c, e_k-1) turned so v_k sits in slot k.
    elems = np.stack([Q, e, c, e_prev], axis=2)               # (F, k, element)
    k = np.arange(4)
    slot_elem = (k[None, :] - k[:, None]) % 4                 # [k, slot] -> element
    kids = np.take_along_axis(elems, np.broadcast_to(slot_elem, (F, 4, 4)), axis=2)
    kids = kids.reshape(-1, 4)[valid.ravel()].astype(np.int32)
    parent = np.repeat(np.arange(F, dtype=np.int32), np.where(tri, 3, 4))
    kid_k = np.broadcast_to(k, (F, 4))[valid].astype(np.int8)
    return nv2, kids, parent, kid_k, ev_face, ev_slot


def top_level_faces(Q, tri, nv, levels):
    """The faces of ZBrush's top subdivision level (quads), and each one's base face."""
    Q = Q.astype(np.int32)
    base_face = np.arange(len(Q), dtype=np.int32)
    for _ in range(levels):
        nv, Q, parent, _k, _f, _s = _subdivide_step(nv, Q, tri)
        base_face = base_face[parent]
        tri = np.zeros(len(Q), dtype=bool)
    return nv, Q, base_face


def blender_multires_order(Q, tri, nv, levels, corner_edges, edges):
    """For the points ZBrush's top level adds, their index in Blender's Multires top level.

    Blender numbers a subdivided mesh: base points, then per base edge its inner points from
    edge[0], then per base face its inner points (a quad's grid row by row from corner 0; a
    triangle's centre, then per corner its grid). `corner_edges` and `edges` are the base mesh's.
    Every new point is located in its base face (quads, coordinates 0..N) or in a triangle
    corner's ptex face (0..N/2) as it is created, then numbered.
    """
    N = 1 << levels
    M = N >> 1
    F = len(Q)
    Q = Q.astype(np.int32)
    sizes = np.where(tri, 3, 4)
    loop_start = np.zeros(F, dtype=np.int64)
    np.cumsum(sizes[:-1], out=loop_start[1:])
    inner = np.where(tri, 1 + 3 * (M - 1) * M, (N - 1) ** 2).astype(np.int64)
    inner_off = np.zeros(F, dtype=np.int64)
    np.cumsum(inner[:-1], out=inner_off[1:])
    n_edges = len(edges)
    edge_base = nv
    inner_base = nv + n_edges * (N - 1)

    # Per face of the current level: base face, triangle corner (-1 for quads), corner coordinates.
    unit = np.array([(0, 0), (N, 0), (N, N), (0, N)], dtype=np.int32)
    frame = np.arange(F, dtype=np.int32)
    ptex = np.full(F, -1, dtype=np.int8)
    coords = np.broadcast_to(unit, (F, 4, 2)).copy()
    pts_frame, pts_ptex, pts_xy = [], [], []
    k4 = np.arange(4)
    cur_nv, cur_Q, cur_tri = nv, Q, tri
    for level in range(levels):
        nv2, kids, parent, kid_k, ev_face, ev_slot = _subdivide_step(
            cur_nv, cur_Q, cur_tri, children=level + 1 < levels)
        # Element coordinates per face: v_k, e_k, c, e_k-1 (as in _subdivide_step).
        C = coords
        C_next = np.roll(C, -1, axis=1)
        if level == 0:
            C_next[cur_tri, 2] = C[cur_tri, 0]
        centre = C.sum(axis=1) // np.where(cur_tri, 3, 4)[:, None]
        mid = (C + C_next) // 2
        # The points this level adds.
        is_face = ev_slot == 4
        xy = np.where(is_face[:, None], centre[ev_face], mid[ev_face, np.minimum(ev_slot, 3)])
        fr = frame[ev_face]
        pt = ptex[ev_face].copy()
        if level == 0:
            # Triangles: edge points of corner k in ptex face k at (M, 0); the centre in ptex 0.
            on_tri = cur_tri[ev_face]
            pt[on_tri] = np.where(is_face[on_tri], 0, ev_slot[on_tri])
            xy[on_tri] = np.where(is_face[on_tri, None], (M, M), (M, 0))
        pts_frame.append(fr)
        pts_ptex.append(pt)
        pts_xy.append(xy.astype(np.int32))
        if level + 1 == levels:
            break
        # Corner coordinates of the children, in the same slot order as their points.
        elems = np.stack([C, mid, np.broadcast_to(centre[:, None], C.shape),
                          np.roll(mid, 1, axis=1)], axis=2)       # (F, k, element, 2)
        sel = (k4[None, :] - kid_k[:, None]) % 4                  # (kid, slot) -> element
        coords = elems[parent[:, None], kid_k[:, None], sel]      # (kid, slot, 2)
        if level == 0 and cur_tri.any():
            t = cur_tri[parent]
            tri_local = np.array([(0, 0), (M, 0), (M, M), (0, M)], dtype=np.int32)
            coords[t] = tri_local[sel[t]]
        frame = frame[parent]
        ptex = np.where(cur_tri[parent], kid_k, ptex[parent]).astype(np.int8)
        del elems, sel, C, C_next, mid, centre
        cur_nv, cur_Q, cur_tri = nv2, kids, np.zeros(len(kids), dtype=bool)

    f = np.concatenate(pts_frame)
    pt = np.concatenate(pts_ptex).astype(np.int64)
    xy = np.concatenate(pts_xy)
    del pts_frame, pts_ptex, pts_xy
    x, y = xy[:, 0].astype(np.int64), xy[:, 1].astype(np.int64)
    out = np.full(len(f), -1, dtype=np.int64)

    def on_edge(sel, corner, from_vert, dist):
        e = corner_edges[loop_start[f[sel]] + corner]
        t = np.where(edges[e, 0] == from_vert, dist, N - dist)
        out[sel] = edge_base + e * (N - 1) + t - 1

    quad = pt < 0
    qx, qy = np.where(quad, x, -1), np.where(quad, y, -1)
    for corner, sel, dist in ((0, (qy == 0) & (qx > 0) & (qx < N), x),
                              (1, (qx == N) & (qy > 0) & (qy < N), y),
                              (2, (qy == N) & (qx > 0) & (qx < N), N - x),
                              (3, (qx == 0) & (qy > 0) & (qy < N), N - y)):
        s = np.flatnonzero(sel)
        on_edge(s, corner, Q[f[s], corner], dist[s])
    s = np.flatnonzero((qx > 0) & (qx < N) & (qy > 0) & (qy < N))
    out[s] = inner_base + inner_off[f[s]] + (y[s] - 1) * (N - 1) + (x[s] - 1)

    # Triangle corner k's ptex face: (0, 0) at v_k, x towards v_k+1, y towards v_k-1, (M, M) the
    # centre. Its y == M line is corner k-1's x == M line.
    on_tri = ~quad
    k = np.where(on_tri, pt, 0)
    s = np.flatnonzero(on_tri & (y == 0) & (x > 0))
    on_edge(s, k[s], Q[f[s], k[s]], x[s])
    s = np.flatnonzero(on_tri & (x == 0) & (y > 0))
    on_edge(s, (k[s] + 2) % 3, Q[f[s], k[s]], y[s])
    centre = on_tri & (x == M) & (y == M)
    s = np.flatnonzero(centre)
    out[s] = inner_base + inner_off[f[s]]
    move = on_tri & ~centre & (y == M) & (x > 0)
    k = np.where(move, (k + 2) % 3, k)
    x, y = np.where(move, M, x), np.where(move, x, y)
    s = np.flatnonzero(on_tri & ~centre & (x > 0) & (y > 0) & (y < M))
    out[s] = (inner_base + inner_off[f[s]] + 1 + k[s] * ((M - 1) * M) + (y[s] - 1) * M + (x[s] - 1))

    total = inner_base + int(inner.sum())
    if (out < nv).any() or total != nv + len(out) or \
            np.count_nonzero(np.bincount(out - nv, minlength=len(out)) != 1):
        raise ValueError("subdivision levels do not map onto Blender's Multires")
    return out


# ---------------------------------------------------------------- meshes

def _coherence(P, Q, tri, sample=1200):
    """Mean edge length over the mean distance between random points: near 1 for faces paired
    with the wrong points, a few hundredths for real meshes."""
    qs, ts = Q[:sample], tri[:sample]
    if len(qs) < 2:
        return None
    nxt, valid = _corner_edges(qs, ts)
    e = np.linalg.norm(P[qs[valid]] - P[nxt[valid]], axis=1)
    rng = np.random.default_rng(0)
    r = np.linalg.norm(P[rng.integers(0, len(P), 1000)] -
                       P[rng.integers(0, len(P), 1000)], axis=1)
    return float(e.mean() / r.mean()) if r.mean() else None


class _Arrays:
    """Decodes each array once."""

    def __init__(self, b):
        self.b = b
        self.cache = {}
        self.skipped = []

    def get(self, rec):
        off = rec['off']
        if off not in self.cache:
            try:
                self.cache[off] = decode_array(self.b, rec)
            except UnsupportedCodec as e:
                self.skipped.append((off, str(e)))
                self.cache[off] = None
        return self.cache[off]

    def points(self, rec):
        data = self.get(rec)
        return None if data is None else np.frombuffer(data, dtype='<f4').reshape(-1, 3)

    def faces(self, rec):
        """(faces, tri, points referenced) when `rec` holds faces, else None."""
        data = self.get(rec)
        if not data:
            return None
        Q = np.frombuffer(data, dtype='<u4').reshape(-1, 4)
        tri = Q[:, 3] == TRI_SENTINEL
        mx = int(Q[:, :3].max())
        quad_last = Q[~tri, 3]
        if len(quad_last):
            mx = max(mx, int(quad_last.max()))
        if mx < 2 or mx >= 0x7FFFFFFF:
            return None
        return Q, tri, mx + 1

    def release(self, rec):
        self.cache.pop(rec['off'], None)


def _levels_plausible(P_top, Q, tri, v0, faces=2000):
    """Whether `P_top` holds subdivision levels of these faces in ZBrush's order: its first new
    points lie near the middle of their edges and faces. A prefix of the faces numbers a prefix
    of the new points, so a few faces are enough."""
    Qs, ts = Q[:faces].astype(np.int64), tri[:faces]
    _, _, _, _, ev_face, ev_slot = _subdivide_step(v0, Qs, ts, children=False)
    nxt, _ = _corner_edges(Qs, ts)
    edge = ev_slot < 4
    slot = np.where(edge, ev_slot, 0)           # face points: any real corner (not a sentinel)
    a, b = Qs[ev_face, slot], nxt[ev_face, slot]
    corners = np.where(ts[ev_face, None], np.concatenate([Qs[ev_face, :3], Qs[ev_face, :1]], 1),
                       Qs[ev_face])
    mid = np.where(edge[:, None], (P_top[a] + P_top[b]) / 2, P_top[corners].mean(axis=1))
    size = np.where(edge, np.linalg.norm(P_top[a] - P_top[b], axis=1),
                    np.linalg.norm(P_top[corners[:, 0]] - P_top[corners[:, 2]], axis=1))
    new = P_top[v0:v0 + len(ev_face)]
    if len(new) < len(ev_face):
        return False
    # Measured on 573 files: real levels 0.005 to 0.15 (0.69 for a 6-face cube); arrays that only
    # have the right size (another mesh, or a lower level in another order) 2 and up.
    return float(np.median(np.linalg.norm(new - mid, axis=1) / (size + 1e-12))) < 1.0


def _best_points(arrays, candidates, Q, tri, nv0):
    """The base level among point arrays that fit the faces. Older projects keep several
    (undo or layer states, some of other meshes): prefer a coherent one of exactly the size the
    faces use, then the most coherent."""
    if len(candidates) < 2:
        return candidates[0] if candidates else None
    best = None
    for r in candidates:
        P = arrays.points(r)
        if P is None:
            continue
        coh = _coherence(P, Q, tri)
        key = (coh is None or coh >= 0.35, r['n'] != nv0, 1.0 if coh is None else coh)
        if best is None or key < best[0]:
            best = (key, r)
    return None if best is None else best[1]


def _pair(arrays, pending, Q, tri, nv0, levels):
    """Match the point arrays read since the previous faces to these faces.

    Returns (base points, top points or None, levels, first point array) or None.
    """
    F = len(Q)
    # One level up has more than base + faces points, so this range holds only the base level.
    candidates = [r for r in pending if nv0 <= r['n'] < nv0 + F]
    top = P_top = None
    bigger = [r for r in pending if r['n'] >= nv0 + F]
    if bigger and (levels or not candidates):
        added = level_vertex_counts(Q, tri, nv0)
        # The highest level first: older files also keep a lower level, not in this order.
        for r in sorted(bigger, key=lambda r: -r['n']):
            for level, a in enumerate(added, 1):
                v0 = r['n'] - a
                if nv0 <= v0 < nv0 + F and (not candidates or
                                             any(c['n'] == v0 for c in candidates)):
                    P = arrays.points(r)
                    if P is not None and _levels_plausible(P, Q, tri, v0):
                        top, P_top = (r, level, v0), P
                    break
            if top is not None:
                break
    if top is not None:
        candidates = [c for c in candidates if c['n'] == top[2]]
    base = _best_points(arrays, candidates, Q, tri, nv0)
    P0 = arrays.points(base) if base is not None else None
    if P0 is None and top is not None:
        # The subtool keeps only its top level: its first points are the base level's.
        P0 = P_top[:top[2]]
    if P0 is None:
        return None
    first = min(r['off'] for r in (base, top[0] if top else None) if r is not None)
    if top is None or not levels:
        return P0, None, 0, first
    return P0, P_top, top[1], first


def _pair_meshes(arrays, recs, levels):
    """Pair the face arrays among `recs` with their point arrays; returns the meshes."""
    meshes, unpaired, pending = [], [], []
    i = 0
    while i < len(recs):
        r = recs[i]
        i += 1
        if r['stride'] == 12:
            pending.append(r)
            continue
        if r['stride'] != 16:
            continue
        faces = arrays.faces(r)
        if faces is None:
            continue
        Q, tri, nv0 = faces
        groups, end = None, r['end']
        # The per-face arrays that follow (UVs, polygroups), up to the next subtool's points.
        while i < len(recs) and recs[i]['stride'] not in (12, 16):
            g = recs[i]
            if g['stride'] == 4 and g['n'] == len(Q) and groups is None:
                data = arrays.get(g)
                groups = None if data is None else np.frombuffer(data, dtype='<u4')
            end = g['end']
            i += 1
        paired = _pair(arrays, pending, Q, tri, nv0, levels)
        if paired is None:
            unpaired.append((r, Q, tri, nv0, groups, end))
        else:
            P0, P_top, n_levels, first = paired
            meshes.append({'points': P0, 'faces': Q, 'tri': tri, 'groups': groups,
                           'levels': n_levels, 'top_points': P_top, 'off': first, 'end': end})
        for p in pending:
            arrays.release(p)
        pending = []

    # Fallback: unpaired faces, the best coherence among the point arrays that fit.
    pts_recs = [r for r in recs if r['stride'] == 12]
    for fr, Q, tri, nv0, groups, end in unpaired:
        best = None
        for pr in pts_recs:
            if pr['n'] < nv0:
                continue
            P = arrays.points(pr)
            if P is None:
                continue
            coh = _coherence(P[:nv0], Q, tri)
            if coh is not None and coh < 0.35 and (best is None or coh < best[0]):
                best = (coh, pr, P[:nv0])
            arrays.release(pr)
        if best is not None:
            meshes.append({'points': best[2], 'faces': Q, 'tri': tri, 'groups': groups,
                           'levels': 0, 'top_points': None, 'off': best[1]['off'], 'end': end})
    return meshes


def read_meshes(source, checks=False, levels=True):
    """Read every mesh object from a .ztl/.ZPR path or an open mmap/bytes object.

    Returns a list of dicts: name, points (Nx3 float32, the base level), faces (Mx4 uint32), tri (M
    bool), groups (M uint32 polygroup ids or None), levels (subdivision levels above the base) and
    top_points (the top level's points, ZBrush order, or None). `levels=False` skips the levels.
    """
    own = None
    if isinstance(source, (str, bytes, os.PathLike)) and not isinstance(source, (bytes, bytearray)):
        f = open(source, 'rb')
        own = f
        b = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    elif isinstance(source, (bytes, bytearray)):
        b = bytes(source)
    else:
        b = source

    try:
        if len(b) < 0x5C:
            raise UnsupportedFormat('file too short for a ZBrush header')
        recs = find_arrays(b)
        arrays = _Arrays(b)
        # A project's undo history (type 123) holds earlier states of its meshes, which ZBrush
        # doesn't show; reading them added hundreds of stale, overlapping copies to some projects.
        undo = find_records(b, UNDO_REC)
        meshes = []
        if undo:
            meshes = _pair_meshes(arrays, [r for r in recs if not any(
                s < r['off'] < e for s, e in undo)], levels)
        if not meshes:
            meshes = _pair_meshes(arrays, recs, levels)

        meshes.sort(key=lambda m: m['off'])
        _name_meshes(b, meshes)
        for m in meshes:
            m['coherence'] = _coherence(m['points'], m['faces'], m['tri'])
            m['skipped'] = arrays.skipped
        if checks:
            for m in meshes:
                m['bbox'] = (float(np.abs(m['points']).max()),
                             float(np.linalg.norm(m['points'].max(0) - m['points'].min(0))))
        return meshes
    finally:
        if own is not None:
            try:
                b.close()
            except Exception:
                pass
            own.close()


def _name_meshes(b, meshes):
    """Name each mesh after the last name record between the previous mesh (or the start of its
    tool) and its points, else after its tool; duplicates get a number."""
    tools = read_names(b)
    tool_starts = [z[0] for z in tools]
    gaps, lo = [], 0
    for m in meshes:
        t = bisect.bisect_left(tool_starts, m['off']) - 1
        gaps.append((max(lo, tool_starts[t]) if t >= 0 else lo, m['off'], t))
        lo = m['end']
    records = read_name_records(b, [g[:2] for g in gaps])
    offsets = sorted(records)
    for i, (m, (lo, hi, t)) in enumerate(zip(meshes, gaps), 1):
        name = None
        j = bisect.bisect_left(offsets, hi) - 1
        while j >= 0 and offsets[j] >= lo:
            s = records[offsets[j]]
            name = _basename(s) if ('\\' in s or '/' in s) else s
            if name:
                break
            j -= 1
        if not name:
            name = tools[t][2] if t >= 0 and tools[t][2] else f'tool_{i:02d}'
        m['name'] = name
    # Repeated names get a number no other subtool has.
    taken = {m['name'] for m in meshes}
    seen = set()
    for m in meshes:
        name = m['name']
        if name in seen:
            n = 2
            while f'{name}_{n}' in taken:
                n += 1
            name = f'{name}_{n}'
            taken.add(name)
            m['name'] = name
        seen.add(name)


# ---------------------------------------------------------------- OBJ output

def write_obj(meshes, path, chunk=200_000):
    base = 0
    with open(path, 'w', buffering=1 << 20) as f:
        f.write('# ZBrush geometry extracted by zbrush_io.py\n')
        for m in meshes:
            P, Q, tri = m['points'], m['faces'], m['tri']
            f.write(f"\no {m['name']}\n")
            buf = []
            for p in P.astype(np.float64):
                buf.append(f'v {p[0]:.6f} {p[1]:.6f} {p[2]:.6f}\n')
                if len(buf) >= chunk:
                    f.write(''.join(buf))
                    buf.clear()
            f.write(''.join(buf))
            buf = []
            for i in range(len(Q)):
                q = Q[i]
                if tri[i]:
                    buf.append(f'f {q[0]+1+base} {q[1]+1+base} {q[2]+1+base}\n')
                else:
                    buf.append(f'f {q[0]+1+base} {q[1]+1+base} {q[2]+1+base} {q[3]+1+base}\n')
                if len(buf) >= chunk:
                    f.write(''.join(buf))
                    buf.clear()
            f.write(''.join(buf))
            base += len(P)


# ---------------------------------------------------------------- Blender

def build_blender_mesh(mesh, name=None, points=None, faces=None, tri=None, groups=None):
    """Create a bpy Mesh datablock from an extracted mesh (fast foreach_set path).

    `points`, `faces`, `tri` and `groups` override the mesh's own (to build its top level).
    """
    import bpy

    P = mesh['points'] if points is None else points
    Q = mesh['faces'] if faces is None else faces
    tri = mesh['tri'] if tri is None else tri
    groups = mesh.get('groups') if groups is None else groups
    me = bpy.data.meshes.new(name or mesh['name'])
    nv = len(P)

    me.vertices.add(nv)
    # The position attribute takes the whole array at once (vertices' 'co' goes per vertex).
    me.attributes['position'].data.foreach_set('vector',
                                               np.ascontiguousarray(P, dtype=np.float32).ravel())

    # Phantom: vectorised (a per-face Python loop took minutes on 20 million faces). Each face
    # keeps its 4 corners, minus the sentinel of triangles, in order.
    sizes = np.where(tri, 3, 4).astype(np.int32)
    loop_start = np.zeros(len(Q), dtype=np.int32)
    if len(Q):
        np.cumsum(sizes[:-1], out=loop_start[1:])
    keep = np.ones(Q.shape, dtype=bool)
    keep[tri, 3] = False
    loop_verts = Q[keep].astype(np.int32)
    me.loops.add(len(loop_verts))
    me.polygons.add(len(Q))
    if _native is not None and hasattr(_native, 'mesh_set_faces'):
        _native.mesh_set_faces(me, loop_start, loop_verts)
    else:
        me.loops.foreach_set('vertex_index', loop_verts)
        me.polygons.foreach_set('loop_start', loop_start)
    del loop_verts
    me.update(calc_edges=True)
    if groups is not None and len(groups) == len(Q):
        # ZBrush polygroup ids become face sets 1, 2, ... in order of appearance.
        _, first, inverse = np.unique(groups, return_index=True, return_inverse=True)
        rank = np.empty(len(first), dtype=np.int32)
        rank[np.argsort(first)] = np.arange(1, len(first) + 1, dtype=np.int32)
        attr = me.attributes.new(".sculpt_face_set", 'INT', 'FACE')
        attr.data.foreach_set('value', rank[inverse.ravel()])
    return me


def add_multires_levels(ob, mesh, matrix=None, max_viewport_faces=None):
    """Give `ob` (built from `mesh`'s base level) Multires levels shaped like ZBrush's top level.

    `matrix` (4x4) is the transform the base mesh got. Returns the modifier. Raises ValueError
    (and removes the modifier) when it can't be done.
    """
    import bpy

    levels, P_top = mesh['levels'], mesh['top_points']
    if matrix is not None:
        m = np.asarray(matrix, dtype=np.float32)
        P_top = P_top @ m[:3, :3].T + m[:3, 3]
    me = ob.data
    corner_edges = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get('edge_index', corner_edges)
    edges = np.empty(len(me.edges) * 2, dtype=np.int64)
    me.edges.foreach_get('vertices', edges)
    edges = edges.reshape(-1, 2)
    order = blender_multires_order(mesh['faces'], mesh['tri'], len(me.vertices), levels,
                                   corner_edges, edges)
    positions = np.empty((len(P_top), 3), dtype=np.float32)
    nv = len(me.vertices)
    positions[:nv] = P_top[:nv]
    positions[order] = P_top[nv:]
    del order

    md = ob.modifiers.new("Multires", 'MULTIRES')
    src_me = bpy.data.meshes.new(ob.name + "_top")
    src = bpy.data.objects.new(src_me.name, src_me)
    try:
        src_me.vertices.add(len(positions))
        src_me.attributes['position'].data.foreach_set('vector', positions.ravel())
        del positions
        for coll in ob.users_collection[:1]:
            coll.objects.link(src)
        with bpy.context.temp_override(object=ob, active_object=ob,
                                       selected_objects=[ob, src],
                                       selected_editable_objects=[ob, src]):
            if _native is not None:
                # All levels at once, unshaped: the reshape below sets every level.
                _native.multires_add_levels(ob, md.name, levels)
                ob.update_tag(refresh={'DATA'})
            else:
                for _ in range(levels):
                    if bpy.ops.object.multires_subdivide(modifier=md.name,
                                                         mode='LINEAR') != {'FINISHED'}:
                        raise ValueError("could not subdivide")
            md.levels = md.sculpt_levels = md.render_levels = md.total_levels
            if bpy.ops.object.multires_reshape(modifier=md.name) != {'FINISHED'}:
                raise ValueError("could not shape the top level")
    except Exception:
        ob.modifiers.remove(md)
        raise
    finally:
        bpy.data.objects.remove(src)
        bpy.data.meshes.remove(src_me)
    if max_viewport_faces is not None:
        corners = len(me.loops)
        level = md.total_levels
        while level > 0 and corners * 4 ** (level - 1) > max_viewport_faces:
            level -= 1
        md.levels = level
    return md


def import_into_scene(path, collection=None):
    """Import every object; returns the created bpy objects. ZBrush is Z-up."""
    import bpy

    coll = collection or bpy.context.scene.collection
    created = []
    for m in read_meshes(path):
        me = build_blender_mesh(m)
        ob = bpy.data.objects.new(m['name'], me)
        coll.objects.link(ob)
        if m['levels']:
            add_multires_levels(ob, m)
        created.append(ob)
    return created


# ---------------------------------------------------------------- CLI

if __name__ == '__main__':
    import sys
    import time

    def peak_mb():
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                            ('PeakWorkingSetSize', ctypes.c_size_t),
                            ('WorkingSetSize', ctypes.c_size_t),
                            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                            ('PagefileUsage', ctypes.c_size_t),
                            ('PeakPagefileUsage', ctypes.c_size_t)]
            k32 = ctypes.WinDLL('kernel32')
            psapi = ctypes.WinDLL('psapi')
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC),
                                                   wintypes.DWORD]
            c = PMC()
            c.cb = ctypes.sizeof(PMC)
            if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return 0.0
            return c.PeakWorkingSetSize / (1024 * 1024)
        except Exception:
            return 0.0

    def file_version(path):
        try:
            with open(path, 'rb') as f:
                head = f.read(0x5C)
            return _u32(head, 0x50) if len(head) >= 0x5C else -1
        except OSError:
            return -1

    def one(path, out=None):
        t = time.time()
        try:
            meshes = read_meshes(path, checks=True)
        except UnsupportedFormat as e:
            print(f'  LEGACY  {os.path.basename(path):40s} {os.path.getsize(path):>12,}  {e}')
            return 'legacy'
        except Exception as e:
            print(f'  FAIL    {os.path.basename(path):40s} {os.path.getsize(path):>12,}  '
                  f'{type(e).__name__}: {e}')
            return 'fail'
        if not meshes:
            v = file_version(path)
            if v < 1006:
                print(f'  LEGACY  {os.path.basename(path):40s} {os.path.getsize(path):>12,}  '
                      f'version {v}; no arrays decoded with the current layout')
                return 'legacy'
            print(f'  EMPTY   {os.path.basename(path):40s} {os.path.getsize(path):>12,}  '
                  f'{time.time()-t:.1f}s  version {v}')
            return 'empty'
        v = sum(len(m['points']) for m in meshes)
        f_ = sum(len(m['faces']) for m in meshes)
        print(f'  OK      {os.path.basename(path):40s} {os.path.getsize(path):>12,}  '
              f'{time.time()-t:6.1f}s  {len(meshes):>3} obj {v:>10,} v {f_:>10,} f  '
              f'peak {peak_mb():7.0f} MB')
        if out:
            write_obj(meshes, out)
        return 'ok'

    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        print('usage: zbrush_io.py FILE [OUT.obj] [--stats]')
        print('       zbrush_io.py DIR  [--stats]      (batch over *.ztl/*.zpr)')
        raise SystemExit(1)

    src = args[0]
    if os.path.isdir(src):
        files = []
        for root, _, names in os.walk(src):
            for n in names:
                if n.lower().endswith(('.ztl', '.zpr')):
                    files.append(os.path.join(root, n))
        files.sort(key=os.path.getsize)
        print(f'{len(files)} files in {src}')
        tally = {'ok': 0, 'empty': 0, 'fail': 0, 'legacy': 0}
        for p in files:
            tally[one(p)] += 1
        print(f"\n{tally['ok']} ok, {tally['empty']} empty, {tally['legacy']} legacy, "
              f"{tally['fail']} failed, {len(files)} total")
        raise SystemExit(0)

    stats_only = '--stats' in sys.argv
    out = next((a for a in args[1:] if not a.startswith('--')), None)
    print(f'== {os.path.basename(src)}  ({os.path.getsize(src):,} bytes)')
    one(src, out if not stats_only else None)
    print(f'   peak {peak_mb():.0f} MB')
