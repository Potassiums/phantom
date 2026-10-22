# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: the Properties editor's Sculpting tab (shown in Sculpt Mode, along with a shorter list
of tabs). It previews the sculpting features to come, Sculpt Layers and ClayPolish, with their
tools shown but not usable yet.
"""

import bpy
from bpy.types import Panel


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


class PROPERTIES_PT_phantom_claypolish(PhantomSculptingPanel, Panel):
    """ClayPolish: sharpen and smooth the whole surface at once, like ZBrush's ClayPolish"""
    bl_label = "ClayPolish"

    def draw(self, _context):
        layout = self.layout
        _coming_soon(layout)
        col = layout.column()
        col.enabled = False
        col.label(text="Polish the whole surface: crisp edges, smooth planes.")

        col.operator("wm.call_menu", text="ClayPolish", icon='SHADERFX')
        col.separator()
        flow = col.column(align=True)
        for label in ("Angle", "Sharp Edges", "Smooth Planes", "Iterations"):
            flow.operator("wm.call_menu", text=label + ":  —", emboss=True)
        col.separator()
        row = col.row(align=True)
        row.operator("wm.call_menu", text="Use Mask", icon='MOD_MASK')
        row.operator("wm.call_menu", text="Polygroup Edges", icon='FACESEL')


classes = (
    PROPERTIES_PT_phantom_sculpting,
    PROPERTIES_PT_phantom_sculpt_layers,
    PROPERTIES_PT_phantom_claypolish,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
