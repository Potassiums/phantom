# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: the Properties editor's Sculpting tab (shown in Sculpt Mode, along with a shorter list
of tabs). Its settings are still to come.
"""

import bpy
from bpy.types import Panel


class PROPERTIES_PT_phantom_sculpting(Panel):
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "phantom_sculpt"
    bl_label = "Sculpting"
    bl_options = {'HIDE_HEADER'}

    def draw(self, _context):
        layout = self.layout
        col = layout.column()
        col.alignment = 'CENTER'
        col.separator(factor=2.0)
        col.label(text="Sculpting", icon='SCULPTMODE_HLT')
        col.label(text="Coming soon")
        col.separator(factor=2.0)


classes = (
    PROPERTIES_PT_phantom_sculpting,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
