# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Multires polypaint: colors painted in Sculpt Mode on a mesh with a Multires modifier are stored per
grid point (the base mesh's grid color layer), at the resolution of the sculpted level.

- A Multires sphere (with triangles at the poles) gets a random mask per grid point and the Color
  Filter's Fill (what the simple view's Fill Object runs) in red: the colors follow the mask, so
  they differ point by point at the top level, not just per base vertex.
- Leaving Sculpt Mode shows them in Object Mode (the subdivided mesh's color attribute).
- Undo of the fill brings the previous colors back.
- They survive saving and opening the file.
- Filling at a lower sculpt level keeps the top level's detail (the change is added to it).
- A plain mesh (no Multires) still fills its color attribute as before.

phantom -b --factory-startup --python tests/python/phantom_multires_polypaint.py

Prints what it checks; exits 1 when a check fails.
"""

import os
import sys
import tempfile

import bpy
import numpy as np

LEVELS = 3
TOL = 1.5 / 255.0


def fail(message):
    print("FAIL:", message, flush=True)
    sys.exit(1)


def check(condition, message):
    if not condition:
        fail(message)
    print("ok:", message, flush=True)


def evaluated_colors(ob):
    """The colors of the subdivided mesh in Object Mode (at the viewport level)."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    me = ob.evaluated_get(depsgraph).data
    attribute = me.color_attributes.active_color
    if attribute is None or attribute.domain != 'POINT':
        fail(f"no point color attribute on the evaluated mesh ({attribute})")
    colors = np.empty(len(me.vertices) * 4, dtype=np.float32)
    attribute.data.foreach_get('color', colors)
    return colors.reshape(-1, 4)


def make_multires_sphere(name):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=6, location=(0, 0, 0))
    ob = bpy.context.active_object
    ob.name = name
    mmd = ob.modifiers.new("Multires", 'MULTIRES')
    for _ in range(LEVELS):
        bpy.ops.object.multires_subdivide(modifier=mmd.name, mode='CATMULL_CLARK')
    mmd.levels = LEVELS
    mmd.sculpt_levels = LEVELS
    return ob


def fill(color, undo=False):
    args = ('EXEC_DEFAULT', True) if undo else ('EXEC_DEFAULT',)
    result = bpy.ops.sculpt.color_filter(*args, type='FILL', strength=1.0, fill_color=color)
    check(result == {'FINISHED'}, f"color filter fill {color} runs")


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # Background mode has no undo stack until a push creates one, and sculpt operators need it.
    bpy.ops.ed.undo_push(message="Start")

    # Plain mesh: the fill writes the base color attribute, as before.
    bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=6, location=(4, 0, 0))
    plain = bpy.context.active_object
    bpy.ops.object.mode_set(mode='SCULPT')
    fill((0.0, 1.0, 0.0))
    bpy.ops.object.mode_set(mode='OBJECT')
    attribute = plain.data.color_attributes.active_color
    colors = np.empty(len(plain.data.vertices) * 4, dtype=np.float32)
    attribute.data.foreach_get('color', colors)
    colors = colors.reshape(-1, 4)
    check(np.allclose(colors[:, :3], (0.0, 1.0, 0.0), atol=TOL), "plain mesh filled green")

    ob = make_multires_sphere("Polypaint")
    for other in bpy.context.view_layer.objects:
        other.select_set(other == ob)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode='SCULPT')
    bpy.ops.sculpt.mask_init(mode='RANDOM_PER_VERTEX')
    fill((1.0, 0.0, 0.0))
    bpy.ops.object.mode_set(mode='OBJECT')

    painted = evaluated_colors(ob)
    base_verts = len(ob.data.vertices)
    check(len(painted) > base_verts * 16, f"{len(painted)} subdivided points, {base_verts} base")
    check(np.allclose(painted[:, 0], 1.0, atol=TOL), "red channel is full everywhere")
    distinct = len(np.unique(np.round(painted[:, 1] * 255.0)))
    check(distinct > 128, f"fill follows the per-point mask ({distinct} distinct values)")
    check(painted[:, 1].min() < 0.1 and painted[:, 1].max() > 0.9, "unmasked red, masked white")
    check(np.allclose(ob.data.color_attributes.active_color.data[0].color[:3], 1.0, atol=TOL),
          "base mesh color attribute untouched (white)")

    # Undo of a color stroke on the grids.
    bpy.ops.object.mode_set(mode='SCULPT')
    bpy.ops.ed.undo_push(message="Before blue")
    fill((0.0, 0.0, 1.0), undo=True)
    bpy.ops.ed.undo()
    bpy.ops.object.mode_set(mode='OBJECT')
    check(np.allclose(evaluated_colors(ob), painted, atol=TOL), "undo restores the colors")

    # Save and open.
    path = os.path.join(tempfile.mkdtemp(prefix="phantom_polypaint_"), "polypaint.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    bpy.ops.wm.open_mainfile(filepath=path)
    bpy.ops.ed.undo_push(message="Opened")
    ob = bpy.data.objects["Polypaint"]
    check(np.allclose(evaluated_colors(ob), painted, atol=TOL), "colors survive save and open")

    # Sculpting a lower level: the level's change is added to the top level's detail.
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    ob.modifiers["Multires"].sculpt_levels = LEVELS - 1
    bpy.ops.object.mode_set(mode='SCULPT')
    bpy.ops.paint.mask_flood_fill(mode='VALUE', value=0.0)
    fill((0.0, 0.0, 1.0))
    bpy.ops.object.mode_set(mode='OBJECT')
    lower = evaluated_colors(ob)
    check(len(lower) == len(painted), "top level kept")
    check(np.allclose(lower[:, 0], 0.0, atol=TOL), "red removed everywhere at the top level")
    check(lower[:, 2].mean() > 0.8, f"mostly blue (mean {lower[:, 2].mean():.2f})")
    # The fill only set the lower level's points: the points in between keep their difference to
    # them, so the top level's green still follows the old per-point mask.
    correlation = np.corrcoef(lower[:, 1], painted[:, 1])[0, 1]
    check(correlation > 0.3, f"top level detail kept (correlation {correlation:.2f})")

    print("PASS")


main()
