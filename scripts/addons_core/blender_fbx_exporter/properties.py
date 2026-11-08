"""RNA properties for the FBX Template Exporter add-on.

Everything the user can tweak lives in a single ``FBXT_Properties`` group that
is attached to the scene as ``scene.fbxt_settings``.  Keeping the settings on
the scene (instead of the add-on preferences) means they are saved inside the
.blend file and can differ per project.
"""

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

# --------------------------------------------------------------------------- #
#  Axis tables
# --------------------------------------------------------------------------- #

AXIS_ITEMS = (
    ('X', 'X', 'Positive X'),
    ('-X', '-X', 'Negative X'),
    ('Y', 'Y', 'Positive Y'),
    ('-Y', '-Y', 'Negative Y'),
    ('Z', 'Z', 'Positive Z'),
    ('-Z', '-Z', 'Negative Z'),
)

#: Forward / up axis pairs handed to ``bpy.ops.export_scene.fbx``.
ORIENTATION_PRESETS = {
    'BLENDER': ('-Z', 'Y'),
    'UNITY': ('Z', 'Y'),
    'UNREAL': ('X', 'Z'),
    'MAX': ('Y', 'Z'),
    'MAYA': ('Z', 'Y'),
}

#: How each orientation preset wants scale to be written to the FBX.
#: Unreal needs every node (objects *and* bones) to keep a scale of 1, 1, 1:
#: the Global Scale then has to travel in the FBX file unit factor
#: ("FBX All") instead of the node transforms.  Blender keeps its own
#: default behaviour ("All Local"), the other engines are left untouched.
ORIENTATION_SCALE_PRESETS = {
    'BLENDER': 'FBX_SCALE_NONE',
    'UNREAL': 'FBX_SCALE_BAKE',
}

ORIENTATION_PRESET_ITEMS = (
    ('BLENDER', 'Blender (default)', 'Forward -Z, Up Y - matches Blender\'s own FBX defaults'),
    ('UNITY', 'Unity', 'Forward Z, Up Y - left handed, Y up'),
    ('UNREAL', 'Unreal Engine', 'Forward X, Up Z - Z up, X forward, objects and bones keep scale 1, 1, 1 (Global Scale goes to the FBX file scale)'),
    ('MAX', '3ds Max (Z-up)', 'Forward Y, Up Z - Z up scenes'),
    ('MAYA', 'Maya (Y-up)', 'Forward Z, Up Y - Y up scenes'),
    ('CUSTOM', 'Custom', 'Use the forward/up axes selected below'),
)

#: Primary / secondary bone axis pairs (the FBX bone "roll" basis).
BONE_PRESETS = {
    'DEFAULT': ('Y', 'X'),
    'X_PRIMARY': ('X', 'Y'),
    'Z_PRIMARY': ('Z', 'Y'),
}

BONE_PRESET_ITEMS = (
    ('DEFAULT', 'Default (Y / X)', 'Blender\'s default bone basis - good for Mixamo and most engines'),
    ('X_PRIMARY', 'X primary / Y secondary', 'Bones point along X - common for 3ds Max Biped style rigs'),
    ('Z_PRIMARY', 'Z primary / Y secondary', 'Bones point along Z'),
    ('CUSTOM', 'Custom', 'Use the bone axes selected below'),
)

SCALE_ITEMS = (
    ('FBX_SCALE_NONE', 'All Local', 'Scale is written into each object/bone node transform - the FBX file scale stays 1.0'),
    ('FBX_SCALE_UNITS', 'FBX Units Scale', 'Custom scale is written into the object node transforms, unit conversion into the FBX file scale'),
    ('FBX_SCALE_CUSTOM', 'FBX Custom Scale', 'Unit conversion is written into the object node transforms, the custom scale into the FBX file scale'),
    ('FBX_SCALE_ALL', 'FBX All', 'Everything goes through the FBX file scale - objects and bones keep a scale of 1, 1, 1 and the size is scaled by the Global Scale'),

    ('FBX_SCALE_BAKE', 'Bake Units (cm data)', 'Scales are baked into vertices, translations and animations, FBX scale = 1.0 - all nodes import as scale 1, 1, 1 in Unreal Engine'),
)

PATH_MODE_ITEMS = (
    ('AUTO', 'Auto', 'Use relative paths when possible'),
    ('ABSOLUTE', 'Absolute', 'Store absolute paths'),
    ('RELATIVE', 'Relative', 'Store relative paths'),
    ('MATCH', 'Match', 'Match the existing paths'),
    ('STRIP', 'Strip Path', 'Store file names only'),
    ('COPY', 'Copy', 'Copy textures next to the FBX'),
)

SMOOTH_ITEMS = (
    ('OFF', 'Normals Only', 'Export normals without split smoothing'),
    ('FACE', 'Face', 'Split by face'),
    ('EDGE', 'Edge', 'Split by edge'),
)

SCOPE_ITEMS = (
    ('SELECTED', 'Selected', 'Export only the selected objects'),
    ('SCENE', 'Whole Scene', 'Export every object in the scene'),
)

ANIM_MODE_ITEMS = (
    ('NONE', 'No Animation', 'Export geometry and rig only'),
    ('EMBED', 'Inside the FBX', 'Bake the animation into the same FBX file'),
    ('SEPARATE', 'Separate FBX per Action', 'Write one FBX file per action'),
)

EMBED_SOURCE_ITEMS = (
    ('ALL_ACTIONS', 'All Actions', 'Bake every action linked to the exported objects'),
    ('NLA', 'NLA Strips', 'Bake the NLA strips as separate takes'),
    ('ACTIVE', 'Active Action', 'Bake only the action currently assigned'),
)

ACTION_SOURCE_ITEMS = (
    ('LINKED', 'Linked Actions', 'Active action plus actions stored in NLA tracks'),
    ('ALL', 'All Actions in File', 'Every action in the .blend, assigned to the exported rig'),
)

OVERWRITE_ITEMS = (
    ('OVERWRITE', 'Overwrite', 'Replace existing files'),
    ('UNIQUE', 'Keep Both', 'Add a numeric suffix instead of overwriting'),
)

FRAME_RANGE_ITEMS = (
    ('AUTO', 'Automatic', 'Per-action files use the action range, embedded animation uses the scene range (previous behaviour)'),
    ('ACTION', 'Action Range', 'Bake the frame range of the action that is being exported'),
    ('SCENE', 'Scene Range', 'Bake the current start/end frame of the scene'),
    ('CUSTOM', 'Custom', 'Use the explicit start/end frames selected below'),
)


# --------------------------------------------------------------------------- #
#  Update helpers - keep preset <-> axis pairs in sync without feedback loops
# --------------------------------------------------------------------------- #

_guard = False

#: Blender 4.5 started warning when a DIR_PATH holds a "//" path unless the
#: property opts in; older versions do not know the option at all.
_PATH_OPTIONS = ({'PATH_SUPPORTS_BLEND_RELATIVE'}
                 if bpy.app.version >= (4, 5, 0) else set())


def _tag_redraw(context):
    """Ask the UI to redraw so the file-name preview stays live."""
    screen = getattr(context, 'screen', None)
    if not screen:
        return
    for area in screen.areas:
        area.tag_redraw()


def _apply_orientation_preset(self, context):
    global _guard
    if _guard:
        return
    axes = ORIENTATION_PRESETS.get(self.orientation_preset)
    scale = ORIENTATION_SCALE_PRESETS.get(self.orientation_preset)
    if not axes and not scale:
        return
    _guard = True
    try:
        if axes:
            self.axis_forward, self.axis_up = axes
        if scale:
            self.apply_scale_options = scale
    finally:
        _guard = False


def _mark_orientation_custom(self, context):
    global _guard
    if _guard:
        return
    _guard = True
    try:
        self.orientation_preset = 'CUSTOM'
    finally:
        _guard = False
    _schedule_save()


def _apply_bone_preset(self, context):
    global _guard
    if _guard:
        return
    axes = BONE_PRESETS.get(self.bone_preset)
    if not axes:
        return
    _guard = True
    try:
        self.primary_bone_axis, self.secondary_bone_axis = axes
    finally:
        _guard = False


def _mark_bone_custom(self, context):
    global _guard
    if _guard:
        return
    _guard = True
    try:
        self.bone_preset = 'CUSTOM'
    finally:
        _guard = False
    _schedule_save()


def _update_preview(self, context):
    _tag_redraw(context)
    # Every visible setting tweak also refreshes the persisted config.
    _schedule_save()


def _update_orientation_preset(self, context):
    _apply_orientation_preset(self, context)
    _schedule_save()


def _update_bone_preset(self, context):
    _apply_bone_preset(self, context)
    _schedule_save()


def _schedule_save():
    """Queue the debounced config save (deferred import to avoid a cycle)."""
    try:
        from . import config
        config.schedule_save()
    except Exception:
        pass


def apply_config(settings, values):
    """Apply values loaded from the on-disk config into a settings group."""
    try:
        from . import config
        return config.apply_to_settings(settings, values or {})
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
#  Settings group
# --------------------------------------------------------------------------- #

class FBXT_Properties(PropertyGroup):
    """All exporter settings, stored per scene."""

    # -- what to export ---------------------------------------------------- #
    export_scope: EnumProperty(
        name='Export',
        description='What should be written to the FBX file',
        items=SCOPE_ITEMS,
        default='SELECTED',
        update=_update_preview,
    )
    include_children: BoolProperty(
        name='Include Children',
        description='Also export the children of the selected objects (meshes parented to a rig, etc.)',
        default=True,
        update=_update_preview,
    )
    only_visible: BoolProperty(
        name='Visible Only',
        description='Skip objects that are hidden in the viewport',
        default=False,
    )

    # -- output ------------------------------------------------------------ #
    output_dir: StringProperty(
        name='Folder',
        description='Destination folder. Relative paths (starting with //) are resolved against the .blend file',
        subtype='DIR_PATH',
        default='//fbx_export/',
        options=_PATH_OPTIONS,
        update=_update_preview,
    )
    filename_template: StringProperty(
        name='Template',
        description='File-name template - use {object}, {action}, {scene}, {frame}, ... see the token menu',
        default='{object}_{action}',
        update=_update_preview,
    )
    overwrite_mode: EnumProperty(
        name='If Exists',
        description='What to do when the target file already exists',
        items=OVERWRITE_ITEMS,
        default='OVERWRITE',
        update=_update_preview,
    )

    # -- scene orientation -------------------------------------------------- #
    orientation_preset: EnumProperty(
        name='Preset',
        description='Starting point for the scene forward/up axes',
        items=ORIENTATION_PRESET_ITEMS,
        default='BLENDER',
        update=_update_orientation_preset,
    )
    axis_forward: EnumProperty(
        name='Forward',
        description='Forward axis written to the FBX file',
        items=AXIS_ITEMS,
        default='-Z',
        update=_mark_orientation_custom,
    )
    axis_up: EnumProperty(
        name='Up',
        description='Up axis written to the FBX file',
        items=AXIS_ITEMS,
        default='Y',
        update=_mark_orientation_custom,
    )

    # -- rig / bones -------------------------------------------------------- #
    include_armatures: BoolProperty(
        name='Include Armatures',
        description='Export armature objects (disable only for static meshes)',
        default=True,
        update=_update_preview,
    )
    deform_bones_only: BoolProperty(
        name='Deform Bones Only',
        description='Skip bones that do not deform geometry',
        default=False,
        update=_update_preview,
    )
    add_leaf_bones: BoolProperty(
        name='Add Leaf Bones',
        description='Add an extra bone at the tip of every chain (needed by some engines)',
        default=False,
        update=_update_preview,
    )
    bone_preset: EnumProperty(
        name='Bone Preset',
        description='Starting point for the bone axis basis',
        items=BONE_PRESET_ITEMS,
        default='DEFAULT',
        update=_update_bone_preset,
    )
    primary_bone_axis: EnumProperty(
        name='Primary Bone Axis',
        description='Direction a bone points to inside the FBX file',
        items=AXIS_ITEMS,
        default='Y',
        update=_mark_bone_custom,
    )
    secondary_bone_axis: EnumProperty(
        name='Secondary Bone Axis',
        description='Secondary (roll) axis of the exported bones',
        items=AXIS_ITEMS,
        default='X',
        update=_mark_bone_custom,
    )
    armature_nodetype: EnumProperty(
        name='Armature Node',
        description='How the armature object itself is written',
        items=(
            ('NULL', 'Null', 'Write the armature as a null node'),
            ('ROOT', 'Root', 'Write the armature as the root node'),
        ),
        default='NULL',
        update=_update_preview,
    )

    # -- animation ---------------------------------------------------------- #
    anim_mode: EnumProperty(
        name='Animation',
        description='How animation is exported',
        items=ANIM_MODE_ITEMS,
        default='EMBED',
        update=_update_preview,
    )
    embed_source: EnumProperty(
        name='Source',
        description='Which animation data to bake into the FBX',
        items=EMBED_SOURCE_ITEMS,
        default='ALL_ACTIONS',
        update=_update_preview,
    )
    separate_action_source: EnumProperty(
        name='Actions',
        description='Which actions get their own FBX file',
        items=ACTION_SOURCE_ITEMS,
        default='LINKED',
        update=_update_preview,
    )
    bake_all_bones: BoolProperty(
        name='Bake All Bones',
        description='Bake every bone, not only the animated ones',
        default=True,
        update=_update_preview,
    )
    force_startend: BoolProperty(
        name='Force Start/End Keys',
        description='Always write a key at the first and last frame',
        default=True,
        update=_update_preview,
    )
    bake_step: FloatProperty(
        name='Sampling Rate',
        description='Frame step used when baking (1 = every frame)',
        default=1.0,
        min=0.01,
        soft_max=10.0,
        update=_update_preview,
    )
    simplify: FloatProperty(
        name='Simplify',
        description='Remove keys that do not change the result (0 = keep everything)',
        default=0.0,
        min=0.0,
        soft_max=10.0,
        update=_update_preview,
    )
    frame_range_source: EnumProperty(
        name='Frame Range',
        description='Range to use when a single action is exported',
        items=FRAME_RANGE_ITEMS,
        default='AUTO',
        update=_update_preview,
    )
    anim_frame_start: IntProperty(
        name='Custom Start',
        description='First frame to bake (used when Frame Range is Custom)',
        default=1,
        min=-1000000,
        soft_min=0,
        max=1000000,
        soft_max=1048576,
        update=_update_preview,
    )
    anim_frame_end: IntProperty(
        name='Custom End',
        description='Last frame to bake (used when Frame Range is Custom)',
        default=250,
        min=-1000000,
        soft_min=0,
        max=1000000,
        soft_max=1048576,
        update=_update_preview,
    )

    # -- mesh / misc -------------------------------------------------------- #
    apply_modifiers: BoolProperty(
        name='Apply Modifiers',
        description='Export the evaluated mesh (modifiers applied)',
        default=True,
        update=_update_preview,
    )
    triangulate: BoolProperty(
        name='Triangulate',
        description='Triangulate faces on export',
        default=False,
        update=_update_preview,
    )
    smooth_type: EnumProperty(
        name='Smoothing',
        description='How smoothing information is written',
        items=SMOOTH_ITEMS,
        default='OFF',
        update=_update_preview,
    )
    path_mode: EnumProperty(
        name='Path Mode',
        description='How texture paths are stored',
        items=PATH_MODE_ITEMS,
        default='AUTO',
        update=_update_preview,
    )
    embed_textures: BoolProperty(
        name='Embed Textures',
        description='Embed textures inside the FBX file',
        default=False,
        update=_update_preview,
    )

    # -- units -------------------------------------------------------------- #
    global_scale: FloatProperty(
        name='Global Scale',
        description='Scale applied to everything on export',
        default=1.0,
        min=0.0001,
        soft_max=100.0,
        update=_update_preview,
    )
    apply_unit_scale: BoolProperty(
        name='Apply Unit Scale',
        description='Convert Blender units to FBX units',
        default=True,
        update=_update_preview,
    )
    apply_scale_options: EnumProperty(
        name='Scale Mode',
        description='How the global scale is applied',
        items=SCALE_ITEMS,
        default='FBX_SCALE_NONE',
        update=_update_preview,
    )
    bake_space_transform: BoolProperty(
        name='Bake Space Transform',
        description='Bake the axis conversion into the mesh data instead of the root node',
        default=False,
        update=_update_preview,
    )


#: Values restored by the "Reset Settings" operator.
DEFAULTS = {
    'export_scope': 'SELECTED',
    'include_children': True,
    'only_visible': False,
    'output_dir': '//fbx_export/',
    'filename_template': '{object}_{action}',
    'overwrite_mode': 'OVERWRITE',
    'orientation_preset': 'BLENDER',
    'axis_forward': '-Z',
    'axis_up': 'Y',
    'include_armatures': True,
    'deform_bones_only': False,
    'add_leaf_bones': False,
    'bone_preset': 'DEFAULT',
    'primary_bone_axis': 'Y',
    'secondary_bone_axis': 'X',
    'armature_nodetype': 'NULL',
    'anim_mode': 'EMBED',
    'embed_source': 'ALL_ACTIONS',
    'separate_action_source': 'LINKED',
    'bake_all_bones': True,
    'force_startend': True,
    'bake_step': 1.0,
    'simplify': 0.0,
    'frame_range_source': 'AUTO',
    'anim_frame_start': 1,
    'anim_frame_end': 250,
    'apply_modifiers': True,
    'triangulate': False,
    'smooth_type': 'OFF',
    'path_mode': 'AUTO',
    'embed_textures': False,
    'global_scale': 1.0,
    'apply_unit_scale': True,
    'apply_scale_options': 'FBX_SCALE_NONE',
    'bake_space_transform': False,
}


def apply_defaults(settings, overrides=None):
    """Reset ``settings`` to the built-in defaults (optionally overridden)."""
    global _guard
    values = dict(DEFAULTS)
    if overrides:
        values.update({k: v for k, v in overrides.items() if v})
    _guard = True
    try:
        for key, value in values.items():
            try:
                setattr(settings, key, value)
            except (AttributeError, TypeError, ValueError):
                pass
    finally:
        _guard = False
    # Let the presets drive their axis pairs again.
    _apply_orientation_preset(settings, None)
    _apply_bone_preset(settings, None)
