# SPDX-FileCopyrightText: 2026 ohnsk
# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Animation Layers: layer animation on top of your keys, with weight, mute, solo and lock.

Layers are NLA tracks (see ``model.py``); the active layer is NLA tweak mode with the tracks above still playing
(``editing.py``), so Blender's own keying (I, Auto Keying, Insert Keyframe menus, keying sets) puts keys on it.
The layer model and math are based on Maya Workflow Kit for Blender by ohnsk
(https://github.com/5hyuns/maya-workflow-kit-for-blender, commit bd7ee04), GPL-3.0-or-later.
"""

import bpy
from bpy.props import BoolProperty, CollectionProperty, IntProperty

from . import editing, model, operators, overlays, ui

classes = (*model.classes, *operators.classes, *overlays.classes, *ui.classes)


def _refresh(scene, _context):
    editing.refresh(scene)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.phantom_anim_layers = CollectionProperty(type=model.ANIM_LAYERS_PG_layer)
    bpy.types.Scene.phantom_anim_layer_index = IntProperty(
        name="Active Layer", default=0, min=0, update=_refresh,
        description="The layer keys go on")
    bpy.types.Scene.phantom_anim_layers_editing = BoolProperty(
        name="Layer Editing", default=True, update=_refresh,
        description="Keys go on the active layer. Turn off to leave the layers as they are (they still play)")
    bpy.types.Scene.phantom_anim_layers_show_hint = BoolProperty(
        name="Show Layer in 3D View", default=True,
        description="Show which layer keys go on, at the bottom left of the 3D View (Object and Pose modes)")
    editing.register()
    overlays.register()
    ui.register()


def unregister():
    ui.unregister()
    overlays.unregister()
    editing.unregister()
    del bpy.types.Scene.phantom_anim_layers_show_hint
    del bpy.types.Scene.phantom_anim_layers_editing
    del bpy.types.Scene.phantom_anim_layer_index
    del bpy.types.Scene.phantom_anim_layers
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
