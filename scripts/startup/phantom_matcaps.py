# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: per-object matcaps.

With Matcap lighting in Solid shading, objects can use their own matcap instead of the
viewport's one. The choice is stored on the object as the ``phantom_matcap`` custom property
(saved with the file) and drawn by the Workbench engine.
"""

import bpy
from bpy.types import Operator

PROPERTY = "phantom_matcap"


def _shading(context):
    from bl_ui.space_view3d import VIEW3D_PT_shading
    return VIEW3D_PT_shading.get_shading(context)


def _targets(context):
    objects = list(context.selected_objects)
    if context.active_object is not None and context.active_object not in objects:
        objects.append(context.active_object)
    return objects


def _refresh(objects, context):
    for ob in objects:
        ob.update_tag()
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


class OBJECT_OT_phantom_matcap_apply(Operator):
    """Give the selected objects the matcap picked above, instead of following the viewport"""
    bl_idname = "object.phantom_matcap_apply"
    bl_label = "Apply Matcap to Selected"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        shading = _shading(context)
        return shading is not None and shading.light == 'MATCAP' and _targets(context)

    def execute(self, context):
        matcap = _shading(context).studio_light
        objects = _targets(context)
        for ob in objects:
            ob[PROPERTY] = matcap
        _refresh(objects, context)
        self.report({'INFO'}, f"Matcap \"{matcap}\" applied to {len(objects)} object(s)")
        return {'FINISHED'}


class OBJECT_OT_phantom_matcap_clear(Operator):
    """Make the selected objects follow the viewport's matcap again"""
    bl_idname = "object.phantom_matcap_clear"
    bl_label = "Use Viewport Matcap"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(PROPERTY in ob for ob in _targets(context))

    def execute(self, context):
        objects = [ob for ob in _targets(context) if PROPERTY in ob]
        for ob in objects:
            del ob[PROPERTY]
        _refresh(objects, context)
        return {'FINISHED'}


def draw_shading_lighting(self, context):
    shading = _shading(context)
    if shading is None or shading.type != 'SOLID' or shading.light != 'MATCAP':
        return
    layout = self.layout
    col = layout.column(align=True)
    col.label(text="Per Object")
    row = col.row(align=True)
    row.operator(OBJECT_OT_phantom_matcap_apply.bl_idname, text="Apply to Selected", icon='MATSHADERBALL')
    row.operator(OBJECT_OT_phantom_matcap_clear.bl_idname, text="Use Viewport", icon='LOOP_BACK')
    active = context.active_object
    if active is not None and PROPERTY in active:
        col.label(text=f"{active.name}: {active[PROPERTY]}", icon='INFO')


classes = (
    OBJECT_OT_phantom_matcap_apply,
    OBJECT_OT_phantom_matcap_clear,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_PT_shading_lighting.append(draw_shading_lighting)


def unregister():
    bpy.types.VIEW3D_PT_shading_lighting.remove(draw_shading_lighting)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
