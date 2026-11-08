"""Sidebar UI and menu entries for the FBX Template Exporter."""

import bpy

from . import exporter, naming


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def _settings(context):
    return context.scene.fbxt_settings


def _preview_object(context, settings):
    """Object whose name feeds the live preview."""
    if settings.export_scope == 'SELECTED':
        obj = context.active_object
        if obj is None and context.selected_objects:
            obj = context.selected_objects[0]
        return obj
    return context.active_object


def _draw_preview(layout, context, settings):
    objects = exporter.gather_objects(context, settings) or []
    obj = _preview_object(context, settings)
    action = None
    per_action = settings.anim_mode == 'SEPARATE'

    if per_action:
        pairs = exporter.collect_action_pairs(objects, settings)
        if pairs:
            obj, action = pairs[0]
    elif settings.anim_mode == 'EMBED':
        action = exporter.active_action(objects)

    try:
        name, folder, warnings = naming.preview(
            settings, context.scene, obj, action, 0, per_action=per_action)
    except Exception as exc:  # never let the UI explode
        layout.label(text='Preview unavailable: %s' % exc, icon='ERROR')
        return

    box = layout.box()
    box.label(text='File Name Preview', icon='FILE_TICK')
    col = box.column(align=True)
    row = col.row()
    row.label(text=name or '(empty)', icon='FILE')
    sub = col.row()
    sub.enabled = False
    sub.label(text=folder)

    if per_action:
        count = len(exporter.collect_action_pairs(objects, settings))
        note = box.column(align=True)
        note.label(text='%d action file(s) will be written' % count,
                   icon='ACTION' if count else 'ERROR')
        if count > 1:
            note.label(text='first file shown above')

    for warning in warnings:
        box.label(text=warning, icon='ERROR')


def _draw_animation_summary(layout, context, settings):
    if settings.anim_mode != 'SEPARATE':
        return
    objects = exporter.gather_objects(context, settings) or []
    pairs = exporter.collect_action_pairs(objects, settings)
    col = layout.column(align=True)
    if not pairs:
        col.label(text='No actions found - a single static FBX will be written',
                  icon='INFO')
        return
    col.label(text='Actions: %d' % len(pairs), icon='ACTION')
    for obj, action in pairs[:6]:
        col.label(text='   %s  /  %s' % (obj.name, action.name))
    if len(pairs) > 6:
        col.label(text='   ... and %d more' % (len(pairs) - 6))


# --------------------------------------------------------------------------- #
#  Panels
# --------------------------------------------------------------------------- #

class FBXT_PT_main(bpy.types.Panel):
    bl_idname = 'FBXT_PT_main'
    bl_label = 'FBX Exporter'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'FBX Export'

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)

        col = layout.column(align=True)
        col.prop(settings, 'export_scope', expand=True)
        row = col.row(align=True)
        row.prop(settings, 'include_children')
        row.prop(settings, 'only_visible')

        layout.separator()
        _draw_preview(layout, context, settings)

        layout.separator()
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator('fbx_template.export', icon='EXPORT')
        row = layout.row(align=True)
        row.operator('fbx_template.open_output_dir', icon='FILE_FOLDER')
        row.operator('fbx_template.reset_settings', icon='LOOP_BACK')


class FBXT_PT_naming(bpy.types.Panel):
    bl_idname = 'FBXT_PT_naming'
    bl_label = 'Output & Naming'
    bl_parent_id = 'FBXT_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)

        layout.prop(settings, 'output_dir', text='Folder')

        row = layout.row(align=True)
        row.prop(settings, 'filename_template', text='Template')
        row.menu('FBXT_MT_token_menu', text='', icon='ADD')

        col = layout.column(align=True)
        col.label(text='Tokens: object, action, scene, frame, date ...', icon='INFO')

        layout.prop(settings, 'overwrite_mode')


class FBXT_PT_orientation(bpy.types.Panel):
    bl_idname = 'FBXT_PT_orientation'
    bl_label = 'Scene Orientation'
    bl_parent_id = 'FBXT_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)

        layout.prop(settings, 'orientation_preset')
        if settings.orientation_preset == 'UNREAL':
            layout.label(text='Preset keeps objects and bones at scale 1, 1, 1',
                         icon='INFO')
        col = layout.column(align=True)
        col.prop(settings, 'axis_forward')
        col.prop(settings, 'axis_up')

        layout.separator()
        col = layout.column(align=True)
        col.prop(settings, 'apply_scale_options')
        col.prop(settings, 'global_scale')
        col.prop(settings, 'apply_unit_scale')
        col.prop(settings, 'bake_space_transform')


class FBXT_PT_rig(bpy.types.Panel):
    bl_idname = 'FBXT_PT_rig'
    bl_label = 'Rig & Bones'
    bl_parent_id = 'FBXT_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)

        col = layout.column(align=True)
        col.prop(settings, 'include_armatures')
        col.prop(settings, 'deform_bones_only')
        col.prop(settings, 'add_leaf_bones')
        col.prop(settings, 'armature_nodetype')

        layout.separator()
        layout.prop(settings, 'bone_preset')
        col = layout.column(align=True)
        col.prop(settings, 'primary_bone_axis')
        col.prop(settings, 'secondary_bone_axis')


class FBXT_PT_animation(bpy.types.Panel):
    bl_idname = 'FBXT_PT_animation'
    bl_label = 'Animation'
    bl_parent_id = 'FBXT_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)

        layout.prop(settings, 'anim_mode', expand=True)

        if settings.anim_mode == 'EMBED':
            layout.prop(settings, 'embed_source')
        elif settings.anim_mode == 'SEPARATE':
            layout.prop(settings, 'separate_action_source')
            _draw_animation_summary(layout, context, settings)

        if settings.anim_mode != 'NONE':
            col = layout.column(align=True)
            col.prop(settings, 'bake_all_bones')
            col.prop(settings, 'force_startend')
            col.prop(settings, 'bake_step')
            col.prop(settings, 'simplify')


class FBXT_PT_advanced(bpy.types.Panel):
    bl_idname = 'FBXT_PT_advanced'
    bl_label = 'Mesh & Textures'
    bl_parent_id = 'FBXT_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)

        col = layout.column(align=True)
        col.prop(settings, 'apply_modifiers')
        col.prop(settings, 'triangulate')
        col.prop(settings, 'smooth_type')

        layout.separator()
        col = layout.column(align=True)
        col.prop(settings, 'path_mode')
        col.prop(settings, 'embed_textures')


# --------------------------------------------------------------------------- #
#  Menus
# --------------------------------------------------------------------------- #

class FBXT_MT_token_menu(bpy.types.Menu):
    bl_idname = 'FBXT_MT_token_menu'
    bl_label = 'Insert Token'

    def draw(self, context):
        layout = self.layout
        for token, _description in naming.TOKEN_LIST:
            op = layout.operator('fbx_template.insert_token', text=token)
            op.token = token


def menu_func(self, context):
    self.layout.operator('fbx_template.export_as', text='FBX (Template)...')


# --------------------------------------------------------------------------- #
#  Add-on preferences
# --------------------------------------------------------------------------- #

def draw_preferences(layout, prefs):
    col = layout.column()
    col.prop(prefs, 'default_output_dir')
    col.separator()
    col.prop(prefs, 'default_orientation_preset')
    col.prop(prefs, 'default_bone_preset')
    col.separator()
    box = layout.box()
    box.label(text='The exporter lives in the 3D Viewport sidebar (N) > "FBX Export".',
              icon='INFO')
    box.label(text='Disable it at any time in Preferences > Add-ons.')
