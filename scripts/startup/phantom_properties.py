# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: the Properties editor's Sculpting tab (shown in Sculpt Mode, along with a shorter list
of tabs). It has ClayPolish (the sculpt history's settings are in the Sculpt Mode header), and
previews Sculpt Layers, still to come, with its tools shown but not usable yet.
"""

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, PointerProperty
from bpy.types import Panel, PropertyGroup


def _coming_soon(layout):
    row = layout.row()
    row.alignment = 'LEFT'
    row.label(text="Coming soon", icon='TIME')


class PhantomSculptingPanel:
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "phantom_sculpt"


class PROPERTIES_PT_phantom_sculpting(PhantomSculptingPanel, Panel):
    bl_label = "Sculpting"
    bl_options = {'HIDE_HEADER'}

    def draw(self, _context):
        layout = self.layout
        col = layout.column()
        col.label(text="Sculpting", icon='SCULPTMODE_HLT')
        sub = col.column()
        sub.enabled = False
        sub.label(text="ZBrush-style sculpting tools, on their way.")


class PROPERTIES_PT_phantom_sculpt_layers(PhantomSculptingPanel, Panel):
    """Sculpt Layers: non-destructive layers of sculpting, like ZBrush's Layers"""
    bl_label = "Sculpt Layers"

    def draw(self, _context):
        layout = self.layout
        _coming_soon(layout)
        col = layout.column()
        col.enabled = False
        col.label(text="Sculpt on layers you can mix, mute and record.")

        row = col.row()
        box = row.box()
        for name, icon in (("Wrinkles", 'HIDE_OFF'), ("Pores", 'HIDE_OFF'),
                           ("Base", 'HIDE_OFF')):
            line = box.row()
            line.label(text=name, icon='LAYER_ACTIVE' if name == "Wrinkles" else 'LAYER_USED')
            line.label(text="", icon=icon)
        side = row.column(align=True)
        side.operator("wm.call_menu", text="", icon='ADD')
        side.operator("wm.call_menu", text="", icon='REMOVE')
        side.separator()
        side.operator("wm.call_menu", text="", icon='TRIA_UP')
        side.operator("wm.call_menu", text="", icon='TRIA_DOWN')

        col.separator()
        sub = col.column(align=True)
        sub.operator("wm.call_menu", text="Record", icon='REC')
        row = sub.row(align=True)
        row.operator("wm.call_menu", text="Bake Layer")
        row.operator("wm.call_menu", text="Merge Down")


class PhantomClayPolishSettings(PropertyGroup):
    """ClayPolish settings, saved with the scene (copied onto ``sculpt.phantom_clay_polish``)."""
    angle_preservation: BoolProperty(
        name="Angle Preservation",
        description="Keep edges by their sharpness. When off, everything is smoothed alike",
        default=True,
    )
    angle_ratio: FloatProperty(
        name="Angle Ratio",
        description="How much of the angle range counts as an edge to keep: higher keeps gentler edges too",
        default=0.5, min=0.0, max=1.0, subtype='FACTOR',
    )
    sharpness: FloatProperty(
        name="Sharpness",
        description="How strongly kept edges are sharpened after each polish",
        default=0.4, min=0.0, max=1.0, subtype='FACTOR',
    )
    softness: FloatProperty(
        name="Softness",
        description="Width of the sharpening, from crisp (0) to broad (1)",
        default=0.3, min=0.0, max=1.0, subtype='FACTOR',
    )
    repeat_polish: IntProperty(
        name="Polish",
        description="Number of polish passes",
        default=2, min=0, max=100, soft_max=10,
    )
    repeat_sharpen: IntProperty(
        name="Sharpen",
        description="Number of sharpen passes after each polish pass",
        default=1, min=0, max=100, soft_max=10,
    )
    inflate_outer: FloatProperty(
        name="Outer",
        description="Push convex areas outwards afterwards, to make up for lost volume",
        default=0.0, min=0.0, max=1.0, subtype='FACTOR',
    )
    inflate_inner: FloatProperty(
        name="Inner",
        description="Push concave areas inwards afterwards, to deepen them again",
        default=0.0, min=0.0, max=1.0, subtype='FACTOR',
    )


class PROPERTIES_PT_phantom_claypolish(PhantomSculptingPanel, Panel):
    """ClayPolish: smooth the whole surface and keep its edges crisp"""
    bl_label = "ClayPolish"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.phantom_clay_polish

        col = layout.column()
        col.prop(settings, "angle_preservation")
        sub = col.column()
        sub.active = settings.angle_preservation
        sub.prop(settings, "angle_ratio")

        col = layout.column()
        col.prop(settings, "sharpness")
        col.prop(settings, "softness")

        col = layout.column(heading="Repeats", align=True)
        col.prop(settings, "repeat_polish")
        col.prop(settings, "repeat_sharpen")

        col = layout.column(heading="Inflate", align=True)
        col.prop(settings, "inflate_outer")
        col.prop(settings, "inflate_inner")

        props = layout.operator("sculpt.phantom_clay_polish", text="ClayPolish", icon='SHADERFX')
        for name in CLAY_POLISH_SETTINGS:
            setattr(props, name, getattr(settings, name))


# ClayPolish settings copied onto the operator.
CLAY_POLISH_SETTINGS = (
    "angle_preservation",
    "angle_ratio",
    "sharpness",
    "softness",
    "repeat_polish",
    "repeat_sharpen",
    "inflate_outer",
    "inflate_inner",
)

classes = (
    PhantomClayPolishSettings,
    PROPERTIES_PT_phantom_sculpting,
    PROPERTIES_PT_phantom_sculpt_layers,
    PROPERTIES_PT_phantom_claypolish,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.phantom_clay_polish = PointerProperty(type=PhantomClayPolishSettings)


def unregister():
    del bpy.types.Scene.phantom_clay_polish
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
