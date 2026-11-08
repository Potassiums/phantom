"""Operators for the FBX Template Exporter."""

import os

import bpy
from bpy.props import StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper

from . import exporter, naming, properties


class FBXT_OT_export(Operator):
    """Export the selection or the scene to FBX using the current template"""

    bl_idname = 'fbx_template.export'
    bl_label = 'Export FBX'
    bl_description = 'Export using the settings in the FBX Export sidebar panel'
    bl_options = {'REGISTER'}

    filepath: StringProperty(
        name='File Path',
        description='Optional explicit destination (used by File > Export)',
        subtype='FILE_PATH',
        default='',
        options={'SKIP_SAVE'},
    )

    def execute(self, context):
        settings = context.scene.fbxt_settings
        try:
            written = exporter.run_export(
                context, settings, filepath_override=self.filepath or None)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:  # pragma: no cover - defensive
            self.report({'ERROR'}, 'FBX export failed: %s' % exc)
            return {'CANCELLED'}

        if not written:
            self.report({'WARNING'}, 'Nothing was exported')
            return {'CANCELLED'}

        if len(written) == 1:
            self.report({'INFO'}, 'Exported %s' % os.path.basename(written[0]))
        else:
            self.report({'INFO'}, 'Exported %d files' % len(written))
        return {'FINISHED'}


class FBXT_OT_export_as(Operator, ExportHelper):
    """Export to an explicitly chosen file"""

    bl_idname = 'fbx_template.export_as'
    bl_label = 'Export FBX (Template)'
    bl_description = 'Choose a destination file and export with the current settings'
    bl_options = {'REGISTER'}

    filename_ext = '.fbx'
    filter_glob: StringProperty(default='*.fbx', options={'HIDDEN'})

    def invoke(self, context, event):
        settings = context.scene.fbxt_settings
        try:
            name = naming.preview(
                settings, context.scene,
                exporter.primary_object(
                    context, exporter.gather_objects(context, settings) or []),
                exporter.active_action(
                    exporter.gather_objects(context, settings) or []),
                0, per_action=settings.anim_mode == 'SEPARATE')[0]
        except Exception:
            name = 'export.fbx'
        self.filepath = os.path.join(naming.resolve_output_dir(settings), name)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        settings = context.scene.fbxt_settings
        try:
            written = exporter.run_export(
                context, settings, filepath_override=self.filepath)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:  # pragma: no cover - defensive
            self.report({'ERROR'}, 'FBX export failed: %s' % exc)
            return {'CANCELLED'}
        self.report({'INFO'}, 'Exported %d file(s)' % len(written))
        return {'FINISHED'}


class FBXT_OT_insert_token(Operator):
    """Append a token to the file-name template"""

    bl_idname = 'fbx_template.insert_token'
    bl_label = 'Insert Token'
    bl_description = 'Append this token to the file-name template'
    bl_options = {'REGISTER', 'UNDO'}

    token: StringProperty(default='{object}')

    def execute(self, context):
        settings = context.scene.fbxt_settings
        settings.filename_template = settings.filename_template + self.token
        return {'FINISHED'}


class FBXT_OT_open_output_dir(Operator):
    """Open the export folder in the system file browser"""

    bl_idname = 'fbx_template.open_output_dir'
    bl_label = 'Open Folder'
    bl_description = 'Open the export folder'
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.scene.fbxt_settings
        folder = naming.resolve_output_dir(settings)
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as exc:
                self.report({'ERROR'}, 'Cannot create folder: %s' % exc)
                return {'CANCELLED'}
        try:
            bpy.ops.wm.path_open(filepath=folder)
        except RuntimeError as exc:
            self.report({'ERROR'}, 'Cannot open folder: %s' % exc)
            return {'CANCELLED'}
        return {'FINISHED'}


class FBXT_OT_reset_settings(Operator):
    """Restore the exporter settings to their defaults"""

    bl_idname = 'fbx_template.reset_settings'
    bl_label = 'Reset Settings'
    bl_description = 'Restore every exporter setting on this scene to its default'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        prefs = context.preferences.addons.get(__package__)
        overrides = None
        if prefs is not None:
            overrides = {
                'output_dir': prefs.preferences.default_output_dir,
                'orientation_preset': prefs.preferences.default_orientation_preset,
                'bone_preset': prefs.preferences.default_bone_preset,
            }
        properties.apply_defaults(context.scene.fbxt_settings, overrides)
        return {'FINISHED'}
