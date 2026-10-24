"""
phantom_zbrush_io.py (from zbrush_io.py) — read ZBrush .ztl / .ZPR geometry. Single file, stdlib + numpy only.

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
    16  faces    4 x u32; 0xFFFFFFFF in slot 4 marks a triangle
    32  per-face UV, 8 floats — NOT exported, mapping unverified
    4   per-face code (group handle), 8 per-point block — not exported

Objects
    Subtools are delimited in file order: every stride-12 array starts a new object and
    its faces are the first stride-16 array after it.  Point arrays may hold several
    subdivision levels concatenated, level 0 first, so truncate to max_index + 1.
    Face arrays that runs cannot pair fall back to a best-coherence point array.

Names
    Each subtool is a type 1004 record: payload starts "ZTool" + 40-byte tool type + a
    source path, followed by a type 12 record holding the name.  Objects are named by
    containment in that record's byte range.  A path-only name uses its basename.

Transforms: none applied and none found; placement is already in the point coordinates.
"""

from __future__ import annotations

import mmap
import os
import struct

import numpy as np

# ---------------------------------------------------------------- constants

TYPE6 = b'\x03\x80\x01\x00\x06\x00'
ZTOOL = b'\x03\x80\x01\x00\xec\x03'
NAME_REC = b'\x03\x80\x01\x00\x0c\x00'

INT_DEQUANT = 2.0 ** -20
RLE_TAGS = (1, 4, 5, 6)
TRI_SENTINEL = 0xFFFFFFFF


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
                out.append({'off': i, 'end': i + size, 'byteLen': blen,
                            'stride': stride, 'aux': aux, 'payload': pay})
        i = b.find(TYPE6, i + 1)
    return out


def decode_array(b, rec):
    """Decode one array to bytes. Raises UnsupportedCodec."""
    mem = parse_mem(b, rec['payload'] + 8)
    data = decode_blocks(mem, rec['stride'])
    if len(data) < rec['byteLen']:
        raise UnsupportedCodec(f'short decode {len(data)} < {rec["byteLen"]}')
    return data[:rec['byteLen']]


def read_names(b):
    """[(start, end, name)] for every ZTool record."""
    out, i = [], b.find(ZTOOL)
    while i != -1:
        size = _u32(b, i + 6)
        if 14 < size < len(b):
            end = i + size
            strings, j = [], b.find(NAME_REC, i)
            while j != -1 and j < end:
                nsz = _u32(b, j + 6)
                if 14 <= nsz <= 300:
                    raw = bytes(b[j + 10:j + 10 + nsz - 14]).lstrip(b'\0')
                    s = raw.split(b'\0')[0]
                    if s and all(32 <= c < 127 for c in s):
                        strings.append(s.decode('latin-1'))
                j = b.find(NAME_REC, j + 1)
            plain = [s for s in strings if '\\' not in s and '/' not in s]
            name = plain[0] if plain else None
            if name is None and strings:
                name = os.path.splitext(strings[0].replace('\\', '/').split('/')[-1])[0] or None
            out.append((i, end, name))
        i = b.find(ZTOOL, i + 1)
    return out


# ---------------------------------------------------------------- meshes

def _coherence(P, Q, tri, sample=1200):
    qs = Q[:sample][~tri[:sample]]
    if len(qs) < 2:
        return None
    e = np.concatenate([np.linalg.norm(P[qs[:, i]] - P[qs[:, (i + 1) % 4]], axis=1)
                        for i in range(4)])
    rng = np.random.default_rng(0)
    r = np.linalg.norm(P[rng.integers(0, len(P), 1000)] -
                       P[rng.integers(0, len(P), 1000)], axis=1)
    return float(e.mean() / r.mean()) if r.mean() else None


def _candidate(b, pr, fr, skipped):
    try:
        pts = decode_array(b, pr)
        P = np.frombuffer(pts, dtype='<f4').reshape(-1, 3)
    except UnsupportedCodec as e:
        skipped.append((pr['off'], str(e)))
        return None
    try:
        faces = decode_array(b, fr)
        Q = np.frombuffer(faces, dtype='<u4').reshape(-1, 4)
    except UnsupportedCodec as e:
        skipped.append((fr['off'], str(e)))
        return None
    if len(Q) == 0:
        return None
    mx = int(Q[:, :3].max())
    if mx >= len(P) or mx < 2:
        return None
    return {'points': P[:mx + 1], 'faces': Q, 'tri': Q[:, 3] == TRI_SENTINEL,
            'off': pr['off'], 'coherence': _coherence(P[:mx + 1], Q, Q[:, 3] == TRI_SENTINEL)}


def read_meshes(source, checks=False):
    """Read every mesh object from a .ztl/.ZPR path or an open mmap/bytes object.

    Returns a list of dicts: name, points (Nx3 float32), faces (Mx4 uint32), tri (M bool).
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
        arrays = find_arrays(b)
        pts_recs = [r for r in arrays if r['stride'] == 12]
        fc_recs = [r for r in arrays if r['stride'] == 16]
        skipped, meshes, used = [], [], set()

        # primary: file-order runs
        run = []
        runs = []
        for r in arrays:
            if r['stride'] == 12 and run:
                runs.append(run)
                run = []
            run.append(r)
        if run:
            runs.append(run)

        for r_ in runs:
            pf = [x for x in r_ if x['stride'] == 12]
            ff = [x for x in r_ if x['stride'] == 16]
            if not pf or not ff or ff[0]['off'] in used:
                continue
            m = _candidate(b, pf[0], ff[0], skipped)
            if m is not None:
                meshes.append(m)
                used.add(ff[0]['off'])

        # fallback: unpaired face arrays, best coherence wins
        for fr in fc_recs:
            if fr['off'] in used:
                continue
            best = None
            for pr in pts_recs:
                m = _candidate(b, pr, fr, skipped)
                if m is None or m['coherence'] is None:
                    continue
                if m['coherence'] < 0.35 and (best is None or m['coherence'] < best['coherence']):
                    best = m
            if best is not None:
                meshes.append(best)
                used.add(fr['off'])

        meshes.sort(key=lambda m: m['off'])
        zs = read_names(b)
        seen = {}
        for i, m in enumerate(meshes, 1):
            prev = [z for z in zs if z[0] < m['off']]
            base = (prev[-1][2] if prev and prev[-1][2] else f'tool_{i:02d}')
            n = seen.get(base, 0) + 1
            seen[base] = n
            m['name'] = base if n == 1 else f'{base}_{n}'

        if checks:
            for m in meshes:
                m['bbox'] = (float(np.abs(m['points']).max()),
                             float(np.linalg.norm(m['points'].max(0) - m['points'].min(0))))
        if skipped:
            meshes_skipped = skipped
        else:
            meshes_skipped = []
        for m in meshes:
            m['skipped'] = meshes_skipped
        return meshes
    finally:
        if own is not None:
            try:
                b.close()
            except Exception:
                pass
            own.close()


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

def build_blender_mesh(mesh, name=None):
    """Create a bpy Mesh datablock from an extracted mesh (fast foreach_set path)."""
    import bpy

    P, Q, tri = mesh['points'], mesh['faces'], mesh['tri']
    me = bpy.data.meshes.new(name or mesh['name'])
    nv = len(P)

    me.vertices.add(nv)
    me.vertices.foreach_set('co', np.ascontiguousarray(P, dtype=np.float32).ravel())

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
    me.loops.foreach_set('vertex_index', loop_verts)
    me.polygons.add(len(Q))
    me.polygons.foreach_set('loop_start', loop_start)
    me.update(calc_edges=True)
    me.validate(verbose=False)
    return me


def import_into_scene(path, collection=None):
    """Import every object; returns the created bpy objects. ZBrush is Z-up."""
    import bpy

    coll = collection or bpy.context.scene.collection
    created = []
    for m in read_meshes(path):
        me = build_blender_mesh(m)
        ob = bpy.data.objects.new(m['name'], me)
        coll.objects.link(ob)
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
