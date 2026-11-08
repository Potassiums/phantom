"""FBX Template Exporter - a Blender add-on.

Exports the selected objects or the whole scene to FBX with:

* a user-editable file-name template plus a live preview of the result,
* selectable scene forward/up axes (Blender, Unity, Unreal, Max, Maya, custom),
* selectable bone (primary/secondary) axes for rigged exports,
* animation embedded in the FBX or split into one FBX per action.

The add-on can be enabled/disabled like any other in
``Edit > Preferences > Add-ons``.
"""

bl_info = {
    'name': 'FBX Template Exporter',
    'author': 'ZBrush/Ghidra workspace tooling',
    'version': (1, 0, 0),
    'blender': (3, 6, 0),
    'location': 'View3D > Sidebar (N) > FBX Export; File > Export > FBX (Template)',
    'description': (
        'Template-based FBX export with selectable scene and bone orientation, '
        'plus per-action animation files'
    ),
    'category': 'Import-Export',
}

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import AddonPreferences

from . import operators, properties, ui

__all__ = ('register', 'unregister')

#: ``__package__`` is the add-on module path; also works when the folder is
#: renamed by the user.
_ADDON_ID = __package__ or __name__


class FBXT_AddonPreferences(AddonPreferences):
    bl_idname = _ADDON_ID

    default_output_dir: StringProperty(
        name='Default Folder',
        description='Folder used when the exporter settings are reset',
        subtype='DIR_PATH',
        default='//fbx_export/',
        options=properties._PATH_OPTIONS,
    )
    default_orientation_preset: EnumProperty(
        name='Default Scene Axes',
        description='Scene orientation used when the exporter settings are reset',
        items=properties.ORIENTATION_PRESET_ITEMS,
        default='BLENDER',
    )
    default_bone_preset: EnumProperty(
        name='Default Bone Axes',
        description='Bone orientation used when the exporter settings are reset',
        items=properties.BONE_PRESET_ITEMS,
        default='DEFAULT',
    )

    def draw(self, context):
        ui.draw_preferences(self.layout, self)


CLASSES = (
    properties.FBXT_Properties,
    FBXT_AddonPreferences,
    operators.FBXT_OT_export,
    operators.FBXT_OT_export_as,
    operators.FBXT_OT_insert_token,
    operators.FBXT_OT_open_output_dir,
    operators.FBXT_OT_reset_settings,
    ui.FBXT_MT_token_menu,
    ui.FBXT_PT_main,
    ui.FBXT_PT_naming,
    ui.FBXT_PT_orientation,
    ui.FBXT_PT_rig,
    ui.FBXT_PT_animation,
    ui.FBXT_PT_advanced,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.fbxt_settings = bpy.props.PointerProperty(
        type=properties.FBXT_Properties)
    bpy.types.TOPBAR_MT_file_export.append(ui.menu_func)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(ui.menu_func)
    del bpy.types.Scene.fbxt_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()
