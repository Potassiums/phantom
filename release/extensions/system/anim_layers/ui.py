# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
The Layers panel (Dope Sheet / Timeline and Graph Editor sidebars, "Layers" tab; 3D View sidebar, "Animation"
tab), the layer list, and the "where keys go" pill in the Dope Sheet / Timeline header.
"""

import bpy

from . import editing, model


def _narrow(context):
    """The region is narrow (under ~10 widget units), as a sidebar in a small area is."""
    region = context.region
    return region is not None and region.width < 10 * 20 * context.preferences.system.ui_scale


class ANIM_LAYERS_UL_layers(bpy.types.UIList):
    """Top layer first, Base at the bottom. Double-click a name to rename it."""

    def draw_item(self, context, layout, _data, item, _icon, _active_data, _active_prop, index):
        base = item.layer_id == model.BASE_ID
        narrow = _narrow(context)
        # One flat row: the icon buttons keep their size and the name gets the rest.
        row = layout.row(align=True)
        row.active = not item.mute
        if not narrow:
            swatch = row.row(align=True)
            swatch.ui_units_x = 0.7
            swatch.prop(item, "color", text="")
        if base:
            row.label(text=item.name)
        else:
            row.prop(item, "name", text="", emboss=False,
                     icon='MOD_OPACITY' if item.blend == 'OVERRIDE' else 'NONE')
            if item.weight < 1.0 and not narrow:
                row.label(text=f"{round(item.weight * 100)}%")
        space = context.space_data
        if space is not None and space.type == 'GRAPH_EDITOR':
            # Show this layer's curves behind the active one's (the active layer's are Blender's own).
            curves = row.row(align=True)
            curves.active = index != context.scene.phantom_anim_layer_index
            curves.prop(item, "show_curves", text="", icon='GRAPH', toggle=True)
        if not base:
            row.prop(item, "mute", text="", emboss=False, icon='HIDE_ON' if item.mute else 'HIDE_OFF')
            row.prop(item, "solo", text="", emboss=False, icon='SOLO_ON' if item.solo else 'SOLO_OFF')
        else:
            row.label(text="", icon='BLANK1')
            row.label(text="", icon='BLANK1')
        row.prop(item, "lock", text="", emboss=False, icon='LOCKED' if item.lock else 'UNLOCKED')

    def filter_items(self, _context, data, propname):
        n = len(getattr(data, propname))
        return [self.bitflag_filter_item] * n, [n - 1 - i for i in range(n)]


def key_hint(scene, ob):
    """(text, icon) saying where keys on ``ob`` go, or why they don't."""
    if not editing.editing_on(scene):
        return "Layer editing is off", 'ERROR'
    have = model.layer_tracks(ob)
    if not have:
        return f"{ob.name} isn't in any layer", 'INFO'
    tr = editing.target_track(scene, ob)
    layer = model.find(scene, tr.strips[0].action.get(model.MARK)) if tr is not None else None
    if layer is None:
        return None
    active = model.active(scene)
    if active is not None and active.layer_id != layer.layer_id:
        return f"{ob.name} isn't in {active.name}: keys go on Base", 'INFO'
    soloed = any(l.solo for l in model.layers(scene) if l.layer_id != model.BASE_ID)
    if layer.lock:
        return f"{layer.name} is locked: no keys go on it", 'LOCKED'
    if layer.layer_id != model.BASE_ID:
        if layer.mute or (soloed and not layer.solo):
            return f"{layer.name} is muted: no keys go on it", 'HIDE_ON'
        if layer.weight < 1e-6:
            return "Weight is 0: raise it to key this layer", 'ERROR'
    return f"Keys go on {layer.name}", 'KEYFRAME_HLT'


def draw_layers(layout, context, rows=5):
    scene = context.scene
    if not editing.supported():
        layout.label(text="Needs Phantom's animation layer support", icon='ERROR')
        return
    ls = model.layers(scene)
    if not len(ls):
        col = layout.column()
        col.label(text="Layers sit on top of your animation.")
        col.label(text="Select what to adjust, then:")
        col.operator("anim_layers.new", text="New Layer", icon='ADD')
        return

    row = layout.row()
    row.template_list("ANIM_LAYERS_UL_layers", "", scene, "phantom_anim_layers", scene, "phantom_anim_layer_index",
                      rows=rows)
    side = row.column(align=True)
    side.operator("anim_layers.new", icon='ADD', text="")
    side.operator("anim_layers.delete", icon='REMOVE', text="")
    side.separator()
    side.menu("ANIM_LAYERS_MT_more", icon='DOWNARROW_HLT', text="")
    side.separator()
    side.operator("anim_layers.move", icon='TRIA_UP', text="").direction = 'UP'
    side.operator("anim_layers.move", icon='TRIA_DOWN', text="").direction = 'DOWN'

    layer = model.active(scene)
    if layer is not None and layer.layer_id != model.BASE_ID:
        col = layout.column(align=True)
        line = col.row(align=True)
        line.prop(layer, "weight", slider=True)
        keyed = any(model.weight_is_keyed(model.layer_tracks(ob)[layer.layer_id].strips[0])
                    for ob in model.members(scene, layer.layer_id))
        line.operator("anim_layers.key_weight", text="", icon='DECORATE_KEYFRAME' if keyed else 'DECORATE_ANIMATE')
        col.row(align=True).prop(layer, "blend", expand=True)

    line = layout.row(align=True)
    if _narrow(context):
        line.label(text="Objects")
        line.operator("anim_layers.add_objects", text="", icon='ADD')
        line.operator("anim_layers.remove_objects", text="", icon='REMOVE')
    else:
        line.label(text="Selected:")
        line.operator("anim_layers.add_objects", text="Add", icon='ADD')
        line.operator("anim_layers.remove_objects", text="Remove", icon='REMOVE')

    if not editing.editing_on(scene):
        box = layout.box()
        box.label(text="Layer editing is off: keys don't go on layers", icon='ERROR')
        box.prop(scene, "phantom_anim_layers_editing", text="Turn Layer Editing On", toggle=True)
        return
    ob = context.active_object
    if ob is not None:
        hint = key_hint(scene, ob)
        if hint is not None:
            layout.label(text=hint[0], icon=hint[1])


class ANIM_LAYERS_MT_more(bpy.types.Menu):
    bl_label = "Layer Options"

    def draw(self, context):
        layout = self.layout
        layout.operator("anim_layers.new", text="New Empty Layer", icon='ADD').with_selected = False
        layout.operator("anim_layers.duplicate", icon='DUPLICATE').with_keys = True
        layout.operator("anim_layers.duplicate", text="Duplicate Without Keys").with_keys = False
        layout.operator("anim_layers.extract", icon='EXPORT')
        layout.operator("anim_layers.merge_down", icon='TRIA_DOWN_BAR')
        layout.separator()
        layout.operator("anim_layers.key_weight", icon='DECORATE_KEYFRAME').value = 'CURRENT'
        layout.operator("anim_layers.key_weight", text="Key Weight at 0").value = 'ZERO'
        layout.operator("anim_layers.key_weight", text="Key Weight at 1").value = 'ONE'
        layout.operator("anim_layers.zero_key", icon='KEYFRAME')
        layout.separator()
        layout.operator("anim_layers.empty", icon='TRASH')
        layout.operator("anim_layers.delete_empty")
        layout.separator()
        layout.operator("anim_layers.bake_to_base", icon='NLA_PUSHDOWN')
        layout.separator()
        layout.prop(context.scene, "phantom_anim_layers_show_hint")
        layout.prop(context.scene, "phantom_anim_layers_editing")


class _LayersPanel:
    bl_region_type = 'UI'
    bl_label = "Animation Layers"

    def draw(self, context):
        draw_layers(self.layout, context)


class ANIM_LAYERS_PT_dopesheet(_LayersPanel, bpy.types.Panel):
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_category = "Layers"


class ANIM_LAYERS_PT_graph(_LayersPanel, bpy.types.Panel):
    bl_space_type = 'GRAPH_EDITOR'
    bl_category = "Layers"


class ANIM_LAYERS_PT_view3d(_LayersPanel, bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_category = "Animation"

    def draw(self, context):
        draw_layers(self.layout, context, rows=4)


EXPORT_TIPS = ("Or keep the layers and set",
               "glTF: Animation > Mode: Scene",
               "FBX: Animation > untick NLA",
               "Strips and All Actions")


class _ExportPanel:
    bl_region_type = 'UI'
    bl_label = "Exporting"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return len(getattr(context.scene, "phantom_anim_layers", ())) > 1

    def draw(self, _context):
        layout = self.layout
        layout.label(text="Any exporter works after:")
        layout.operator("anim_layers.bake_to_base", icon='NLA_PUSHDOWN')
        col = layout.column(align=True)
        for line in EXPORT_TIPS:
            col.label(text=line)


class ANIM_LAYERS_PT_dopesheet_export(_ExportPanel, bpy.types.Panel):
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_category = "Layers"
    bl_parent_id = "ANIM_LAYERS_PT_dopesheet"


class ANIM_LAYERS_PT_graph_export(_ExportPanel, bpy.types.Panel):
    bl_space_type = 'GRAPH_EDITOR'
    bl_category = "Layers"
    bl_parent_id = "ANIM_LAYERS_PT_graph"


class ANIM_LAYERS_PT_popover(bpy.types.Panel):
    """The layer list from the Dope Sheet / Timeline header"""
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_region_type = 'HEADER'
    bl_label = "Animation Layers"
    bl_ui_units_x = 14

    def draw(self, context):
        draw_layers(self.layout, context, rows=4)


def draw_header_pill(self, context):
    scene = context.scene
    if len(getattr(scene, "phantom_anim_layers", ())) < 2:
        return
    layer = model.active(scene)
    text = layer.name if layer is not None else ""
    if not editing.editing_on(scene):
        text = "Layers off"
    self.layout.popover(panel="ANIM_LAYERS_PT_popover", text=text, icon='NLA')


classes = (ANIM_LAYERS_UL_layers, ANIM_LAYERS_MT_more, ANIM_LAYERS_PT_dopesheet, ANIM_LAYERS_PT_graph,
           ANIM_LAYERS_PT_view3d, ANIM_LAYERS_PT_dopesheet_export, ANIM_LAYERS_PT_graph_export,
           ANIM_LAYERS_PT_popover)


def register():
    bpy.types.DOPESHEET_HT_header.append(draw_header_pill)


def unregister():
    bpy.types.DOPESHEET_HT_header.remove(draw_header_pill)
