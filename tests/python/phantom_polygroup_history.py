# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Polygroups (face sets) in the sculpt history and in undo.

On a plain mesh and on a Multires mesh (face sets live on the base mesh's faces), in Sculpt Mode:

- Mask to Polygroup (Ctrl W: a face set from the mask, then the mask cleared), Grow, Shrink,
  Init from loose parts / normals, Create from visible, and New Colors (randomize colors) each add
  one step to the sculpt history, the mask clear a mask step.
- Undo goes back through every step, restoring the face sets (and their colors) and the mask;
  redo goes forward again.
- Going to each step on the timeline (Go to History Step) restores its face sets, colors and mask,
  and undoing such a jump comes back.

phantom -b --factory-startup --python tests/python/phantom_polygroup_history.py

Prints what it checks; exits 1 when a check fails.
"""

import sys

import bpy
import numpy as np


def fail(message):
    print("FAIL:", message, flush=True)
    sys.exit(1)


def check(condition, message):
    if not condition:
        fail(message)
    print("ok:", message, flush=True)


def face_sets(ob):
    """The face set of each (base) mesh face: 1 everywhere without the attribute."""
    me = ob.data
    attribute = me.attributes.get(".sculpt_face_set")
    values = np.ones(len(me.polygons), dtype=np.int32)
    if attribute is not None:
        attribute.data.foreach_get("value", values)
    return values


def mask_sum(ob):
    """A checksum of the mesh's mask (plain meshes: Multires masks are in the grids)."""
    me = ob.data
    attribute = me.attributes.get(".sculpt_mask")
    if attribute is None:
        return 0.0
    values = np.zeros(len(me.vertices), dtype=np.float32)
    attribute.data.foreach_get("value", values)
    return float(values.sum())


def snapshot(ob, with_mask):
    me = ob.data
    return {
        "face_sets": face_sets(ob),
        "seed": me.face_sets_color_seed if hasattr(me, "face_sets_color_seed") else None,
        "default": me.face_sets_color_default if hasattr(me, "face_sets_color_default") else None,
        "mask": mask_sum(ob) if with_mask else None,
    }


def same(a, b):
    return (np.array_equal(a["face_sets"], b["face_sets"]) and a["seed"] == b["seed"] and
            a["default"] == b["default"] and a["mask"] == b["mask"])


def describe(a, b):
    differing = int(np.count_nonzero(a["face_sets"] != b["face_sets"])) \
        if a["face_sets"].shape == b["face_sets"].shape else "size"
    return (f"{differing} faces differ, seed {a['seed']} vs {b['seed']}, "
            f"default {a['default']} vs {b['default']}, mask {a['mask']} vs {b['mask']}")


def history(ob):
    h = ob.sculpt_history
    return (len(h.steps), h.current) if h is not None else (0, -1)


def run(op, **properties):
    """Run a sculpt operator as from a menu: its own undo step."""
    result = op('EXEC_DEFAULT', True, **properties)
    check(result == {'FINISHED'}, f"{op.idname_py()} {properties} runs")


def test(ob, label):
    # The base mesh's mask (set in Object Mode) masks one half; Multires grids get no mask from it,
    # so they are masked with a mask filter instead (in Sculpt Mode, as a history step).
    # Undo back to the start reloads the scene (a global undo step): look the object up again.
    object_name = ob.name

    def obj():
        return bpy.data.objects[object_name]

    multires = any(md.type == 'MULTIRES' for md in ob.modifiers)
    bpy.context.view_layer.objects.active = ob
    for o in bpy.context.view_layer.objects:
        o.select_set(o == ob)
    if not multires:
        me = ob.data
        positions = np.empty(len(me.vertices) * 3, dtype=np.float32)
        me.vertices.foreach_get("co", positions)
        mask = (positions.reshape(-1, 3)[:, 0] > 0.0).astype(np.float32)
        attribute = me.attributes.new(".sculpt_mask", 'FLOAT', 'POINT')
        attribute.data.foreach_set("value", mask)
    bpy.ops.object.mode_set(mode='SCULPT')
    bpy.ops.ed.undo_push(message=f"Sculpt {label}")
    with_mask = not multires

    # The steps: (name, snapshot after it, history length after it).
    snaps = [("start", snapshot(obj(), with_mask))]
    steps_before = history(obj())[0]

    def added(steps_added):
        """The first edit also adds the history's first step ("Start", the mesh before it)."""
        return steps_added + (1 if steps_before == 0 else 0)

    def after(name, history_steps_added=1):
        nonlocal steps_before
        steps, current = history(obj())
        check(steps == steps_before + added(history_steps_added) and current == steps - 1,
              f"{label}: {name} adds {history_steps_added} history step(s) ({steps} steps)")
        steps_before = steps
        snaps.append((name, snapshot(obj(), with_mask)))

    if multires:
        # Mask steps between the face set steps (the grids' mask, which these checks don't read).
        run(bpy.ops.paint.mask_flood_fill, mode='VALUE', value=1.0)
        after("mask all")
        run(bpy.ops.sculpt.face_sets_init, mode='LOOSE_PARTS')
        after("init loose parts")
        run(bpy.ops.paint.mask_flood_fill, mode='VALUE', value=0.0)
        after("clear mask")
        run(bpy.ops.sculpt.face_sets_init, mode='NORMALS', threshold=0.5)
        after("init from normals")
    else:
        # Ctrl W: the face set step, then the mask step.
        run(bpy.ops.sculpt.phantom_mask_to_face_set)
        steps, _current = history(obj())
        check(steps == steps_before + added(2), f"{label}: Mask to Polygroup adds 2 history steps")
        steps_before = steps
        snaps.append(("mask to polygroup", snapshot(obj(), with_mask)))
        # (Undo takes both steps: the polygroup is checked after the first undo below too.)

    sets = face_sets(obj())
    newest = int(sets.max())
    check(len(np.unique(sets)) > 1, f"{label}: there are several polygroups ({len(np.unique(sets))})")
    run(bpy.ops.sculpt.face_set_edit, mode='GROW', active_face_set=newest)
    after("grow")
    run(bpy.ops.sculpt.face_set_edit, mode='SHRINK', active_face_set=newest)
    run(bpy.ops.sculpt.face_set_edit, mode='SHRINK', active_face_set=newest)
    # (Two steps: snapshots only after the second, the first is checked through undo order.)
    steps, _current = history(obj())
    check(steps == steps_before + added(2), f"{label}: two shrinks add 2 history steps")
    steps_before = steps
    snaps.append(("shrink x2", snapshot(obj(), with_mask)))
    run(bpy.ops.sculpt.face_sets_randomize_colors)
    after("new colors")
    check(snaps[-1][1]["seed"] != snaps[-2][1]["seed"], f"{label}: New Colors changed the seed")
    run(bpy.ops.sculpt.face_sets_create, mode='VISIBLE')
    after("create from visible")
    check(len(np.unique(face_sets(obj()))) == 1, f"{label}: one polygroup after Create from Visible")

    # Undo back to the start: each snapshot comes back on its way (two undos for the
    # two-step entries).
    undo_counts = {"mask to polygroup": 2, "shrink x2": 2}
    # (Undoing the first sculpt step loads the whole-scene step from before Sculpt Mode; with
    # Multires its grids have no mask layer then, which the redo of "mask all" adds back.)
    for i in range(len(snaps) - 1, 0, -1):
        name = snaps[i][0]
        for _ in range(undo_counts.get(name, 1)):
            bpy.ops.ed.undo()
        expected = snaps[i - 1][1]
        now = snapshot(obj(), with_mask)
        check(same(now, expected),
              f"{label}: undo of '{name}' restores '{snaps[i - 1][0]}'" +
              ("" if same(now, expected) else f" ({describe(now, expected)})"))
    # Redo to the end.
    for i in range(1, len(snaps)):
        name = snaps[i][0]
        for _ in range(undo_counts.get(name, 1)):
            bpy.ops.ed.redo()
        now = snapshot(obj(), with_mask)
        check(same(now, snaps[i][1]),
              f"{label}: redo of '{name}'" +
              ("" if same(now, snaps[i][1]) else f" ({describe(now, snaps[i][1])})"))

    # The timeline: each snapshot's step. Snapshots are taken at the end of their entry, so their
    # history index is counted from the end.
    steps, current = history(obj())
    check(current == steps - 1, f"{label}: redo brought the history to its last step")
    index_of = {}
    index = steps - 1
    for i in range(len(snaps) - 1, -1, -1):
        index_of[i] = index
        index -= undo_counts.get(snaps[i][0], 1) if i > 0 else 0
    start_index = index_of[0]
    order = list(range(len(snaps)))
    order = order[::2] + order[1::2][::-1] + [len(snaps) - 1]
    for i in order:
        name, expected = snaps[i]
        if index_of[i] != history(obj())[1]:
            run(bpy.ops.sculpt.phantom_history_goto, index=index_of[i])
        now = snapshot(obj(), with_mask)
        check(same(now, expected),
              f"{label}: timeline step {index_of[i]} ('{name}') restores its polygroups" +
              ("" if same(now, expected) else f" ({describe(now, expected)})"))
    # Undo of a jump comes back to where it left.
    run(bpy.ops.sculpt.phantom_history_goto, index=start_index)
    bpy.ops.ed.undo()
    now = snapshot(obj(), with_mask)
    check(same(now, snaps[-1][1]), f"{label}: undoing a timeline jump comes back")

    bpy.ops.object.mode_set(mode='OBJECT')


def saved_and_reopened(object_name):
    """The history's polygroups survive saving and opening the file."""
    import os
    import tempfile

    def ob():
        return bpy.data.objects[object_name]

    bpy.context.view_layer.objects.active = ob()
    bpy.ops.object.mode_set(mode='SCULPT')
    steps = len(ob().sculpt_history.steps)
    expected = []
    for index in range(steps):
        if index != ob().sculpt_history.current:
            run(bpy.ops.sculpt.phantom_history_goto, index=index)
        expected.append(snapshot(ob(), False))
    bpy.ops.object.mode_set(mode='OBJECT')
    path = os.path.join(tempfile.gettempdir(), "phantom_polygroup_history.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    bpy.ops.ed.undo_push(message="Opened")
    check(ob().sculpt_history is not None and len(ob().sculpt_history.steps) == steps,
          f"reopened: the history has its {steps} steps")
    for o in bpy.context.view_layer.objects:
        o.select_set(o == ob())
    bpy.context.view_layer.objects.active = ob()
    bpy.ops.object.mode_set(mode='SCULPT')
    for index in reversed(range(steps)):
        if index != ob().sculpt_history.current:
            run(bpy.ops.sculpt.phantom_history_goto, index=index)
        now = snapshot(ob(), False)
        check(same(now, expected[index]), f"reopened: step {index} restores its polygroups" +
              ("" if same(now, expected[index]) else f" ({describe(now, expected[index])})"))
    bpy.ops.object.mode_set(mode='OBJECT')
    os.remove(path)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # Background mode has no undo stack until a push creates one, and sculpt operators need it.
    bpy.ops.ed.undo_push(message="Start")

    # Two loose parts, so Init from Loose Parts makes two polygroups.
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, location=(0, 0, 0))
    plain = bpy.context.active_object
    plain.name = "Plain"
    test(plain, "plain mesh")
    saved_and_reopened("Plain")

    bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=6, location=(4, 0, 0))
    a = bpy.context.active_object
    bpy.ops.mesh.primitive_cube_add(size=0.5, location=(5.5, 0, 0))
    b = bpy.context.active_object
    for o in bpy.context.view_layer.objects:
        o.select_set(o in {a, b})
    bpy.context.view_layer.objects.active = a
    bpy.ops.object.join()
    multires = bpy.context.active_object
    multires.name = "Multires"
    mmd = multires.modifiers.new("Multires", 'MULTIRES')
    for _ in range(2):
        bpy.ops.object.multires_subdivide(modifier=mmd.name, mode='CATMULL_CLARK')
    mmd.levels = 2
    mmd.sculpt_levels = 2
    bpy.ops.ed.undo_push(message="Multires")
    test(multires, "Multires")

    print("ALL OK", flush=True)


try:
    main()
except SystemExit:
    raise
except BaseException:
    import traceback
    traceback.print_exc()
    sys.exit(1)
sys.exit(0)
