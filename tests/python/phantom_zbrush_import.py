# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Opens a ZBrush file with File > Import > ZBrush and checks every imported subtool against the
file's own data (read again with ``phantom_zbrush_io``):

- the base mesh has the file's vertex and face counts, its exact faces (corner by corner) and points,
  Blender's ``validate()`` finds nothing to fix but ZBrush's own duplicate faces, and it has no
  spikes (faces paired with the wrong points connect far-apart points: long edges, high coherence);
- polygroups became face sets (as many as the file has);
- subtools with subdivision levels have a Multires modifier with as many levels, whose top level
  has ZBrush's top-level vertex and face counts and shape (every vertex).

phantom -b --factory-startup --python tests/python/phantom_zbrush_import.py -- FILE.zpr [--no-levels]

Prints one line per subtool and the timings; exits 1 when a check fails.
"""

import os
import sys
import time

import bpy
import numpy as np

import phantom_zbrush_io as zbrush_io
from phantom_zbrush_import import ZBRUSH_TO_BLENDER

# Faces paired with the wrong points connect random points: coherence (mean edge / mean distance
# between random points) near 1 and median edges a sizeable part of the mesh. Real subtools in the
# test files stay under 0.25 and 0.05.
MAX_COHERENCE = 0.35
MAX_MEDIAN_EDGE = 0.15          # of the bounding box diagonal
MIN_FACES_EDGE_CHECK = 200      # low-poly primitives (ZBrush's star) have long edges of their own
POSITION_TOL = 1e-5             # of the bounding box diagonal


def mesh_arrays(me):
    co = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get('co', co)
    loops = np.empty(len(me.loops), dtype=np.int32)
    me.loops.foreach_get('vertex_index', loops)
    sizes = np.empty(len(me.polygons), dtype=np.int32)
    me.polygons.foreach_get('loop_total', sizes)
    edges = np.empty(len(me.edges) * 2, dtype=np.int64)
    me.edges.foreach_get('vertices', edges)
    return co.reshape(-1, 3), loops, sizes, edges.reshape(-1, 2)


def check_object(ob, m, matrix, levels):
    """Returns (problems, info) for one imported subtool."""
    problems = []
    me = ob.data
    co, loops, sizes, edges = mesh_arrays(me)
    Q, tri = m['faces'], m['tri']
    if len(co) != len(m['points']):
        problems.append(f"{len(co)} vertices, the file has {len(m['points'])}")
    if len(sizes) != len(Q):
        problems.append(f"{len(sizes)} faces, the file has {len(Q)}")
    if not problems:
        keep = np.ones(Q.shape, dtype=bool)
        keep[tri, 3] = False
        if not np.array_equal(sizes, np.where(tri, 3, 4)) or not np.array_equal(loops, Q[keep]):
            problems.append("faces differ from the file's")
        P = m['points'] @ np.asarray(matrix, dtype=np.float32)[:3, :3].T if matrix is not None \
            else m['points']
        diag = float(np.linalg.norm(P.max(0) - P.min(0))) or 1.0
        err = float(np.abs(co - P).max()) / diag
        if err > POSITION_TOL:
            problems.append(f"points moved by {err:.1e} of the size")
    diag = float(np.linalg.norm(co.max(0) - co.min(0))) or 1.0
    lengths = np.linalg.norm(co[edges[:, 0]] - co[edges[:, 1]], axis=1)
    median_edge = float(np.median(lengths)) / diag if len(lengths) else 0.0
    coherence = m['coherence']
    if len(sizes) >= MIN_FACES_EDGE_CHECK and (
            (coherence is not None and coherence > MAX_COHERENCE) or median_edge > MAX_MEDIAN_EDGE):
        problems.append(f"spiky: coherence {coherence}, median edge {median_edge:.3f} of the size")

    # ZBrush allows duplicate faces (the same corners twice), which validate() deletes; anything
    # else it would fix is a problem.
    keep = np.ones(Q.shape, dtype=bool)
    keep[tri, 3] = False
    corners = np.sort(np.where(keep, Q.astype(np.int64), -1), axis=1)
    duplicates = len(Q) - len(np.unique(corners, axis=0))
    copy = me.copy()
    if copy.validate(verbose=False) and not (
            duplicates and len(copy.vertices) == len(co) and
            len(copy.polygons) == len(sizes) - duplicates):
        problems.append("validate() changed the mesh")
    bpy.data.meshes.remove(copy)

    groups = m.get('groups')
    face_sets = me.attributes.get(".sculpt_face_set")
    n_groups = len(np.unique(groups)) if groups is not None else 0
    if groups is not None:
        if face_sets is None:
            problems.append("no face sets")
        else:
            fs = np.empty(len(me.polygons), dtype=np.int32)
            face_sets.data.foreach_get('value', fs)
            if len(np.unique(fs)) != n_groups:
                problems.append(f"{len(np.unique(fs))} face sets, the file has {n_groups} polygroups")

    info = f"v {len(co):>9,} f {len(sizes):>9,} groups {n_groups:>3}"
    if duplicates:
        info += f" ({duplicates} duplicate faces, as in ZBrush)"
    md = next((md for md in ob.modifiers if md.type == 'MULTIRES'), None)
    want_levels = m['levels'] if levels else 0
    if want_levels and md is None:
        problems.append(f"no Multires for its {want_levels} levels")
    elif md is not None and md.total_levels != want_levels:
        problems.append(f"Multires has {md.total_levels} levels, the file {want_levels}")
    elif md is not None:
        P_top = m['top_points']
        if matrix is not None:
            P_top = P_top @ np.asarray(matrix, dtype=np.float32)[:3, :3].T
        shown = md.levels
        md.levels = md.total_levels
        dg = bpy.context.evaluated_depsgraph_get()
        ob_eval = ob.evaluated_get(dg)
        top = ob_eval.to_mesh()
        top_co = np.empty(len(top.vertices) * 3, dtype=np.float32)
        top.vertices.foreach_get('co', top_co)
        top_co = top_co.reshape(-1, 3)
        top_faces = len(top.polygons)
        ob_eval.to_mesh_clear()
        md.levels = shown
        corners = 4 * len(Q) - int(np.count_nonzero(tri))
        want_faces = corners * 4 ** (want_levels - 1)
        if len(top_co) != len(P_top) or top_faces != want_faces:
            problems.append(f"top level has {len(top_co):,} vertices / {top_faces:,} faces, ZBrush "
                            f"{len(P_top):,} / {want_faces:,}")
        else:
            corner_edges = np.empty(len(me.loops), dtype=np.int64)
            me.loops.foreach_get('edge_index', corner_edges)
            order = zbrush_io.blender_multires_order(Q, tri, len(co), want_levels, corner_edges,
                                                     edges)
            want = np.empty_like(P_top)
            want[:len(co)] = P_top[:len(co)]
            want[order] = P_top[len(co):]
            top_diag = float(np.linalg.norm(want.max(0) - want.min(0))) or 1.0
            err = float(np.linalg.norm(top_co - want, axis=1).max()) / top_diag
            if err > POSITION_TOL:
                problems.append(f"top level differs from ZBrush's by {err:.1e} of the size")
        info += f" | Multires {md.total_levels}: v {len(top_co):>11,} f {top_faces:>11,}"
    return problems, info


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        print(__doc__)
        sys.exit(2)
    levels = "--no-levels" not in argv
    failed = 0
    for path in paths:
        print(f"== {os.path.basename(path)} ({os.path.getsize(path) / 1e6:.0f} MB)")
        for ob in list(bpy.data.objects):
            bpy.data.objects.remove(ob)
        t = time.perf_counter()
        result = bpy.ops.import_scene.phantom_zbrush(filepath=path, subdivision_levels=levels)
        t_import = time.perf_counter() - t
        if result != {'FINISHED'}:
            print(f"  FAIL import returned {result}")
            failed += 1
            continue
        t = time.perf_counter()
        meshes = zbrush_io.read_meshes(path)
        t_read = time.perf_counter() - t
        # The import links the subtools in file order.
        collection = bpy.data.collections[os.path.splitext(os.path.basename(path))[0]]
        objects = list(collection.objects)
        if len(objects) != len(meshes) or len({m['name'] for m in meshes}) != len(meshes):
            print(f"  FAIL {len(objects)} objects for {len(meshes)} subtools (names "
                  f"{len({m['name'] for m in meshes})} unique)")
            failed += 1
        t = time.perf_counter()
        bad = 0
        for ob, m in zip(objects, meshes):
            problems, info = check_object(ob, m, ZBRUSH_TO_BLENDER, levels)
            if ob.name != m['name']:
                problems.append(f"named {ob.name}")
            print(f"  {'FAIL' if problems else 'ok  '} {m['name'][:32]:32s} {info}"
                  + (f"  <- {'; '.join(problems)}" if problems else ""))
            bad += bool(problems)
        failed += bad
        print(f"  {len(meshes)} subtools, {bad} failed. Import {t_import:.1f} s (reading alone "
              f"{t_read:.1f} s); checks {time.perf_counter() - t:.1f} s")
    print("PASS" if not failed else f"FAIL ({failed})")
    sys.exit(1 if failed else 0)


main()
