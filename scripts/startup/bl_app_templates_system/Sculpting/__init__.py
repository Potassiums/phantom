# SPDX-FileCopyrightText: 2018-2023 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

import bpy
from bpy.app.handlers import persistent


def view_front():
    """Phantom: start every 3D view looking straight at the front of the sphere, polygroups hidden."""
    from math import radians
    from mathutils import Euler, Vector

    front = Euler((radians(90.0), 0.0, 0.0)).to_quaternion()
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            region_3d = area.spaces.active.region_3d
            region_3d.view_rotation = front
            region_3d.view_location = Vector((0.0, 0.0, 0.0))
            # Polygroups are hidden until shown (Ctrl+F).
            area.spaces.active.overlay.show_sculpt_face_sets = False


@persistent
def load_handler(_):
    bpy.ops.mesh.primitive_quad_sphere_add(segments=64)
    bpy.ops.object.mode_set(mode='SCULPT')
    view_front()


def register():
    bpy.app.handlers.load_factory_startup_post.append(load_handler)


def unregister():
    bpy.app.handlers.load_factory_startup_post.remove(load_handler)
