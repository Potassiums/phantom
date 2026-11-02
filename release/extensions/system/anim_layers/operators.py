# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Layer operators. Each one is a single undo step."""

import bpy
from bpy.props import BoolProperty, EnumProperty

from . import bake, editing, model


def _selected(context):
    return list(context.selected_editable_objects)


class _LayerOp:
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return editing.supported() and context.scene is not None


class _ActiveLayerOp(_LayerOp):
    """Needs an active layer other than Base."""

    @classmethod
    def poll(cls, context):
        if not super().poll(context):
            return False
        layer = model.active(context.scene)
        return layer is not None and layer.layer_id != model.BASE_ID


class ANIM_LAYERS_OT_new(_LayerOp, bpy.types.Operator):
    """Add a layer on top, with the selected objects in it. Keys you set go on it"""
    bl_idname = "anim_layers.new"
    bl_label = "New Layer"

    with_selected: BoolProperty(name="With Selected Objects", default=True, options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, props):
        if props.with_selected:
            return "Add a layer on top, with the selected objects in it. Keys you set go on it"
        return "Add an empty layer on top (add objects to it later)"

    def execute(self, context):
        scene = context.scene
        obs = _selected(context) if self.with_selected else []
        with editing.suspended(scene, model.layered_objects(scene) + obs):
            layer = model.new_layer(scene)
            for ob in obs:
                model.add_object(scene, ob, layer)
            model.sync(scene)
        scene.phantom_anim_layer_index = model.index_of(scene, layer.layer_id)
        if self.with_selected and not obs:
            self.report({'INFO'}, "The new layer is empty: select objects and use Add Selected")
        return {'FINISHED'}


class ANIM_LAYERS_OT_delete(_ActiveLayerOp, bpy.types.Operator):
    """Delete the active layer and its keys"""
    bl_idname = "anim_layers.delete"
    bl_label = "Delete Layer"

    def execute(self, context):
        scene = context.scene
        layer = model.active(scene)
        lid = layer.layer_id
        i = scene.phantom_anim_layer_index
        with editing.suspended(scene):
            for ob in model.members(scene, lid):
                model.remove_object(ob, lid)
            model.layers(scene).remove(i)
            scene.phantom_anim_layer_index = max(0, i - 1)
        return {'FINISHED'}


class ANIM_LAYERS_OT_add_objects(_LayerOp, bpy.types.Operator):
    """Put the selected objects in the active layer"""
    bl_idname = "anim_layers.add_objects"
    bl_label = "Add Selected"

    @classmethod
    def poll(cls, context):
        return super().poll(context) and model.active(context.scene) is not None

    def execute(self, context):
        scene = context.scene
        layer = model.active(scene)
        obs = [ob for ob in _selected(context) if layer.layer_id not in model.layer_tracks(ob)]
        if not obs:
            self.report({'INFO'}, f"The selected objects are already in {layer.name}")
            return {'CANCELLED'}
        with editing.suspended(scene, model.layered_objects(scene) + obs):
            for ob in obs:
                model.add_object(scene, ob, layer)
            model.sync(scene)
        return {'FINISHED'}


class ANIM_LAYERS_OT_remove_objects(_ActiveLayerOp, bpy.types.Operator):
    """Take the selected objects out of the active layer. Their keys on it are deleted"""
    bl_idname = "anim_layers.remove_objects"
    bl_label = "Remove Selected"

    def execute(self, context):
        scene = context.scene
        layer = model.active(scene)
        obs = [ob for ob in _selected(context) if layer.layer_id in model.layer_tracks(ob)]
        if not obs:
            self.report({'INFO'}, f"None of the selected objects are in {layer.name}")
            return {'CANCELLED'}
        with editing.suspended(scene):
            for ob in obs:
                model.remove_object(ob, layer.layer_id)
        return {'FINISHED'}


class ANIM_LAYERS_OT_move(_ActiveLayerOp, bpy.types.Operator):
    """Move the active layer up or down the stack (Base stays at the bottom)"""
    bl_idname = "anim_layers.move"
    bl_label = "Move Layer"

    direction: EnumProperty(items=(('UP', "Up", ""), ('DOWN', "Down", "")), options={'SKIP_SAVE'})

    def execute(self, context):
        scene = context.scene
        ls = model.layers(scene)
        i = scene.phantom_anim_layer_index
        j = i + 1 if self.direction == 'UP' else i - 1
        if not 1 <= j < len(ls):
            return {'CANCELLED'}
        layer_id = ls[i].layer_id
        with editing.suspended(scene):
            ls.move(i, j)
            layer = model.find(scene, layer_id)
            for ob in model.members(scene, layer_id):
                model.move_track(scene, ob, layer)
            scene.phantom_anim_layer_index = j
        return {'FINISHED'}


class ANIM_LAYERS_OT_key_weight(_ActiveLayerOp, bpy.types.Operator):
    """Key the active layer's weight at the current frame, to fade the layer in and out over time"""
    bl_idname = "anim_layers.key_weight"
    bl_label = "Key Weight"

    value: EnumProperty(
        items=(('CURRENT', "Current Weight", "Key the weight as it is"),
               ('ZERO', "Weight 0", "Set the weight to 0 and key it"),
               ('ONE', "Weight 1", "Set the weight to 1 and key it")),
        default='CURRENT', options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, props):
        if props.value == 'ZERO':
            return "Set the active layer's weight to 0 and key it at the current frame"
        if props.value == 'ONE':
            return "Set the active layer's weight to 1 and key it at the current frame"
        return "Key the active layer's weight at the current frame, to fade the layer in and out over time"

    def execute(self, context):
        scene = context.scene
        layer = model.active(scene)
        if not model.members(scene, layer.layer_id):
            self.report({'INFO'}, f"{layer.name} has no objects yet")
            return {'CANCELLED'}
        if layer.lock:
            self.report({'WARNING'}, f"{layer.name} is locked")
            return {'CANCELLED'}
        if self.value != 'CURRENT':
            layer.weight = 0.0 if self.value == 'ZERO' else 1.0
        model.key_weight(scene, layer, scene.frame_current)
        return {'FINISHED'}


class ANIM_LAYERS_OT_duplicate(_ActiveLayerOp, bpy.types.Operator):
    """Copy the active layer, right above it, with the same objects and copies of its keys"""
    bl_idname = "anim_layers.duplicate"
    bl_label = "Duplicate Layer"

    with_keys: BoolProperty(name="With Keys", default=True, options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, props):
        if props.with_keys:
            return "Copy the active layer, right above it, with the same objects and copies of its keys"
        return "Add an empty layer right above the active one, with the same objects, blend and weight"

    def execute(self, context):
        scene = context.scene
        layer_id = model.active(scene).layer_id
        with editing.suspended(scene):
            new_id = model.duplicate_layer(scene, layer_id, with_keys=self.with_keys)
            model.sync(scene)
            scene.phantom_anim_layer_index = model.index_of(scene, new_id)
        return {'FINISHED'}


class ANIM_LAYERS_OT_extract(_ActiveLayerOp, bpy.types.Operator):
    """Move the selected objects' keys on the active layer into a new layer right above it (nothing moves)"""
    bl_idname = "anim_layers.extract"
    bl_label = "Extract Selected Objects"

    def execute(self, context):
        scene = context.scene
        layer = model.active(scene)
        layer_id, name = layer.layer_id, layer.name
        obs = [ob for ob in _selected(context) if layer_id in model.layer_tracks(ob)]
        if not obs:
            self.report({'INFO'}, f"None of the selected objects are in {name}")
            return {'CANCELLED'}
        with editing.suspended(scene):
            new_id = model.extract(scene, layer_id, obs)
            model.sync(scene)
            scene.phantom_anim_layer_index = model.index_of(scene, new_id)
        return {'FINISHED'}


class ANIM_LAYERS_OT_merge_down(_ActiveLayerOp, bpy.types.Operator):
    """Merge the active layer into the layer below it (keyed on every frame, so nothing moves) and remove it"""
    bl_idname = "anim_layers.merge_down"
    bl_label = "Merge Down"

    def execute(self, context):
        scene = context.scene
        ls = model.layers(scene)
        i = scene.phantom_anim_layer_index
        layer, below = ls[i], ls[i - 1]
        layer_id, name, below_name = layer.layer_id, layer.name, below.name
        for l in (layer, below):
            if l.lock:
                self.report({'WARNING'}, f"{l.name} is locked")
                return {'CANCELLED'}
        if below.layer_id != model.BASE_ID and below.mute:
            self.report({'WARNING'}, f"{below_name} is muted: unmute it to merge into it")
            return {'CANCELLED'}
        with editing.suspended(scene):
            bake.merge_down(scene, layer_id)
            ls.remove(model.index_of(scene, layer_id))
            scene.phantom_anim_layer_index = i - 1
        self.report({'INFO'}, f"Merged {name} into {below_name}")
        return {'FINISHED'}


class ANIM_LAYERS_OT_bake_to_base(_LayerOp, bpy.types.Operator):
    """Bake every layer into the objects' own animation and remove the layers, ready to export"""
    bl_idname = "anim_layers.bake_to_base"
    bl_label = "Bake to Base"

    selected_only: BoolProperty(name="Selected Objects Only", default=False,
                                description="Bake only the selected objects; the others keep their layers")

    @classmethod
    def poll(cls, context):
        return super().poll(context) and bool(model.layered_objects(context.scene))

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, title="Bake to Base", confirm_text="Bake")

    def draw(self, _context):
        col = self.layout.column()
        col.label(text="The layers are baked into each object's Base animation")
        col.label(text="(channels they change get a key on every frame),")
        col.label(text="then removed. Muted layers are left out.")
        col.separator()
        col.prop(self, "selected_only")

    def execute(self, context):
        scene = context.scene
        obs = model.layered_objects(scene)
        if self.selected_only:
            obs = [ob for ob in obs if ob.select_get()]
        if not obs:
            self.report({'INFO'}, "No selected object has layers")
            return {'CANCELLED'}
        skipped = 0
        with editing.suspended(scene, obs):
            for ob in obs:
                skipped += bake.bake_to_base(scene, ob)
            model.prune(scene)
        text = f"Baked {len(obs)} object(s) to Base"
        if skipped:
            text += f"; {skipped} muted layer(s) left out"
        self.report({'INFO'}, text)
        return {'FINISHED'}


class ANIM_LAYERS_OT_zero_key(_ActiveLayerOp, bpy.types.Operator):
    """Key "no change" on the active layer at the current frame, for the selected objects in it"""
    bl_idname = "anim_layers.zero_key"
    bl_label = "Zero Key"

    def execute(self, context):
        scene = context.scene
        layer = model.active(scene)
        if layer.lock or layer.mute:
            self.report({'WARNING'}, f"{layer.name} is {'locked' if layer.lock else 'muted'}")
            return {'CANCELLED'}
        obs = [ob for ob in _selected(context) if layer.layer_id in model.layer_tracks(ob)]
        if not obs:
            self.report({'INFO'}, f"Select objects in {layer.name}")
            return {'CANCELLED'}
        if not bake.zero_key(scene, layer.layer_id, obs, scene.frame_current):
            self.report({'INFO'}, f"{layer.name} has no keys on the selected objects yet")
            return {'CANCELLED'}
        return {'FINISHED'}


class ANIM_LAYERS_OT_empty(_ActiveLayerOp, bpy.types.Operator):
    """Take every object out of the active layer, deleting its keys on it. The layer stays"""
    bl_idname = "anim_layers.empty"
    bl_label = "Empty Layer"

    def execute(self, context):
        scene = context.scene
        with editing.suspended(scene):
            model.empty(scene, model.active(scene).layer_id)
        return {'FINISHED'}


class ANIM_LAYERS_OT_delete_empty(_LayerOp, bpy.types.Operator):
    """Delete every layer that has no objects"""
    bl_idname = "anim_layers.delete_empty"
    bl_label = "Delete Empty Layers"

    def execute(self, context):
        scene = context.scene
        with editing.suspended(scene):
            gone = model.delete_empty(scene)
        self.report({'INFO'}, f"Deleted {len(gone)} empty layer(s)" if gone else "No empty layers")
        return {'FINISHED'} if gone else {'CANCELLED'}


classes = (ANIM_LAYERS_OT_new, ANIM_LAYERS_OT_delete, ANIM_LAYERS_OT_add_objects, ANIM_LAYERS_OT_remove_objects,
           ANIM_LAYERS_OT_move, ANIM_LAYERS_OT_key_weight, ANIM_LAYERS_OT_duplicate, ANIM_LAYERS_OT_extract,
           ANIM_LAYERS_OT_merge_down, ANIM_LAYERS_OT_bake_to_base, ANIM_LAYERS_OT_zero_key, ANIM_LAYERS_OT_empty,
           ANIM_LAYERS_OT_delete_empty)
