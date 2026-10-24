# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: Stroke, Alpha and Texture thumbnails beside the brush thumbnail in the Brush Asset
panel, like ZBrush's brush tray. Clicking one opens its settings to edit.

- **Stroke**: how the stroke lays down dabs (Space, Dots, Drag Dot...), with its settings.
- **Alpha**: the grayscale image that shapes the brush. In Sculpt Mode that is the brush's
  texture (Blender stamps it like a ZBrush alpha); in Texture Paint it is the texture mask.
- **Texture**: the color image a paint brush stamps (Texture Paint). Sculpt brushes don't use a
  color texture, so there the tile is greyed out.
"""

import bpy
from bpy.props import EnumProperty
from bpy.types import Operator

# Tile height, in buttons.
TILE_SCALE = 1.9

STROKE_LABELS = {
    'DOTS': "Dots", 'DRAG_DOT': "Drag Dot", 'SPACE': "Space", 'AIRBRUSH': "Airbrush",
    'ANCHORED': "Anchored", 'LINE': "Line", 'CURVE': "Curve",
}


def _sculpting(context):
    return context.mode == 'SCULPT'


def _slots(brush, context):
    """{tile: texture slot} for this mode: which brush texture each tile shows and edits."""
    if _sculpting(context):
        return {'ALPHA': brush.texture_slot, 'TEXTURE': None}
    return {'ALPHA': brush.mask_texture_slot, 'TEXTURE': brush.texture_slot}


def _panel(slot, context):
    if slot == 'STROKE':
        return "VIEW3D_PT_tools_brush_stroke"
    if slot == 'ALPHA' and not _sculpting(context):
        return "VIEW3D_PT_tools_mask_texture"
    return "VIEW3D_PT_tools_brush_texture"


class PAINT_OT_phantom_brush_slot(Operator):
    """Edit the brush's stroke, alpha or texture"""
    bl_idname = "paint.phantom_brush_slot"
    bl_label = "Brush Slot"
    bl_options = {'INTERNAL'}

    slot: EnumProperty(items=(
        ('STROKE', "Stroke", "How the stroke lays down the brush"),
        ('ALPHA', "Alpha", "The grayscale image shaping the brush"),
        ('TEXTURE', "Texture", "The color image a paint brush stamps"),
    ))

    @classmethod
    def description(cls, context, properties):
        if properties.slot == 'STROKE':
            return "Stroke: how the brush is laid down along the stroke (spacing, dots, drag...)"
        if properties.slot == 'ALPHA':
            return "Alpha: the grayscale image shaping the brush. Click to pick or edit it"
        if _sculpting(context):
            return ("Texture: sculpt brushes don't use a color texture. The brush's image is its "
                    "Alpha")
        return "Texture: the color image the brush stamps. Click to pick or edit it"

    def execute(self, context):
        bpy.ops.wm.call_panel(name=_panel(self.slot, context), keep_open=True)
        return {'FINISHED'}


def _tile(layout, slot, label, texture=None, text=None, enabled=True):
    col = layout.column(align=True)
    col.enabled = enabled
    icon = layout.icon(texture) if texture is not None else 0
    if icon:
        col.phantom_operator_preview(PAINT_OT_phantom_brush_slot.bl_idname, icon_value=icon,
                                     scale=TILE_SCALE).slot = slot
    else:
        sub = col.column(align=True)
        sub.scale_y = TILE_SCALE
        # An empty slot is just the texture icon: "None" doesn't fit a narrow tile.
        sub.operator(PAINT_OT_phantom_brush_slot.bl_idname, text=text or "",
                     icon='NONE' if text else 'TEXTURE').slot = slot
    name = col.row()
    name.alignment = 'CENTER'
    name.label(text=label)


def draw_tiles(layout, context, brush):
    """The Stroke, Alpha and Texture tiles for `brush`, stacked in `layout`."""
    col = layout.column(align=False)
    _tile(col, 'STROKE', "Stroke", text=STROKE_LABELS.get(brush.stroke_method, brush.stroke_method.title()))
    slots = _slots(brush, context)
    alpha = slots['ALPHA'].texture if slots['ALPHA'] is not None else None
    _tile(col, 'ALPHA', "Alpha", texture=alpha)
    texture_slot = slots['TEXTURE']
    _tile(col, 'TEXTURE', "Texture", texture=texture_slot.texture if texture_slot else None,
          enabled=texture_slot is not None)


def wanted(context):
    """Whether the Brush Asset panel shows the tiles (3D View sculpting or painting)."""
    return (context.area is not None and context.area.type == 'VIEW_3D' and
            context.mode in {'SCULPT', 'PAINT_TEXTURE'})


classes = (
    PAINT_OT_phantom_brush_slot,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
