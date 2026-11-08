"""Core export logic for the FBX Template Exporter.

The add-on never re-implements FBX writing: it drives Blender's own
``bpy.ops.export_scene.fbx`` and only adds the templating, the scope handling
and the "one file per action" splitting on top.
"""

import contextlib
import math
import os

import bpy

from . import naming

#: Object types the FBX exporter understands.  The exact enum differs between
#: Blender versions, so the set is intersected with the live RNA below.
_ALL_OBJECT_TYPES = {'EMPTY', 'CAMERA', 'LIGHT', 'ARMATURE', 'MESH', 'OTHER'}


def _object_type_items():
    """Identifiers accepted by the ``object_types`` flag set of the exporter."""
    try:
        rna = bpy.ops.export_scene.fbx.get_rna_type()
        prop = rna.properties['object_types']
        return {item.identifier for item in prop.enum_items}
    except Exception:
        return set(_ALL_OBJECT_TYPES)


# --------------------------------------------------------------------------- #
#  Operator property filtering
# --------------------------------------------------------------------------- #

def _fbx_operator_properties():
    """Identifiers accepted by ``export_scene.fbx`` in this Blender version.

    The FBX exporter gained and lost options over the years (and will keep
    doing so), so every keyword is filtered against the live RNA instead of
    being passed blindly.
    """
    try:
        rna = bpy.ops.export_scene.fbx.get_rna_type()
        return {prop.identifier for prop in rna.properties}
    except Exception:
        pass
    try:
        return {prop.identifier for prop in bpy.types.EXPORT_SCENE_OT_fbx.bl_rna.properties}
    except Exception:
        return set()


def export_fbx(filepath, kwargs):
    """Call the FBX exporter, dropping keywords this Blender does not know."""
    accepted = _fbx_operator_properties()
    if accepted:
        kwargs = {key: value for key, value in kwargs.items() if key in accepted}
    return bpy.ops.export_scene.fbx(filepath=filepath, **kwargs)


def _ensure_parent(path):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)


# --------------------------------------------------------------------------- #
#  Scene inspection
# --------------------------------------------------------------------------- #

def _ensure_object_mode(context):
    obj = getattr(context, 'object', None)
    if obj is None or obj.mode == 'OBJECT':
        return
    try:
        bpy.ops.object.mode_set(mode='OBJECT')
    except RuntimeError:
        pass


def _in_view_layer(context, obj):
    try:
        return obj.name in context.view_layer.objects
    except ReferenceError:
        return False


def _is_hidden(obj):
    if obj.hide_viewport or obj.hide_get():
        return True
    return not obj.visible_get()


def gather_objects(context, settings):
    """Objects that take part in the export, in a stable order."""
    scene = context.scene

    if settings.export_scope == 'SELECTED':
        roots = list(context.selected_objects)
        if not roots:
            return []
        objects = []
        seen = set()
        for root in roots:
            candidates = [root]
            if settings.include_children:
                candidates.extend(root.children_recursive)
            for obj in candidates:
                if obj.name in seen:
                    continue
                seen.add(obj.name)
                objects.append(obj)
    else:
        objects = list(scene.objects)

    objects = [o for o in objects if _in_view_layer(context, o)]
    if settings.only_visible:
        objects = [o for o in objects if not _is_hidden(o)]
    return objects


def primary_object(context, objects):
    """Object used for the ``{object}`` token in single-file exports."""
    active = getattr(context, 'active_object', None)
    if active is not None and active in objects:
        return active
    if getattr(context, 'object', None) in objects:
        return context.object
    return objects[0] if objects else None


def active_action(objects):
    for obj in objects:
        anim = obj.animation_data
        if anim is not None and anim.action is not None:
            return anim.action
    return None


def collect_action_pairs(objects, settings):
    """``(object, action)`` pairs that get their own FBX file."""
    pairs = []
    seen = set()

    def add(obj, action):
        if action is None:
            return
        key = (obj.name, action.name)
        if key in seen:
            return
        seen.add(key)
        pairs.append((obj, action))

    animated = [o for o in objects if o.animation_data is not None]
    for obj in animated:
        anim = obj.animation_data
        if anim.action is not None:
            add(obj, anim.action)
        for track in anim.nla_tracks:
            for strip in track.strips:
                add(obj, strip.action)

    if settings.separate_action_source == 'ALL':
        targets = animated or objects
        already = {action.name for _, action in pairs}
        for action in bpy.data.actions:
            if action.name in already:
                continue
            if getattr(action, 'id_root', 'OBJECT') not in ('OBJECT', ''):
                continue
            for obj in targets:
                add(obj, action)
                already.add(action.name)
                break

    pairs.sort(key=lambda item: (item[0].name, item[1].name))
    return pairs


# --------------------------------------------------------------------------- #
#  Temporary state
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def _selected_only(context, objects):
    """Select exactly ``objects`` for the duration of the block."""
    view_layer = context.view_layer
    previous = {obj.name for obj in context.selected_objects}
    previous_active = view_layer.objects.active
    try:
        for obj in view_layer.objects:
            try:
                obj.select_set(False)
            except RuntimeError:
                pass
        for obj in objects:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass
        if objects:
            view_layer.objects.active = objects[0]
        yield
    finally:
        for obj in view_layer.objects:
            try:
                obj.select_set(obj.name in previous)
            except RuntimeError:
                pass
        view_layer.objects.active = previous_active


def _select_matching_slot(anim, action, obj):
    """Blender 4.4+ slotted actions: make sure a slot for ``obj`` is active."""
    slots = getattr(action, 'slots', None)
    if not slots:
        return
    try:
        wanted = obj.id_type
        candidates = [s for s in slots
                      if getattr(s, 'target_id_type', wanted) == wanted]
        if not candidates:
            return
        current = getattr(anim, 'action_slot', None)
        if current is not None and current in candidates:
            return
        anim.action_slot = candidates[0]
    except Exception:
        pass


@contextlib.contextmanager
def _action_override(obj, action, scene):
    """Temporarily make ``action`` the only animation on ``obj``."""
    anim = obj.animation_data
    if anim is None:
        anim = obj.animation_data_create()

    previous_action = anim.action
    previous_slot = getattr(anim, 'action_slot', None)
    previous_mutes = [(track, track.mute) for track in anim.nla_tracks]
    previous_range = (scene.frame_start, scene.frame_end)

    try:
        anim.action = action
        _select_matching_slot(anim, action, obj)
        for track, _mute in previous_mutes:
            track.mute = True

        start, end = _action_range(action, scene)
        scene.frame_start = start
        scene.frame_end = end
        yield
    finally:
        scene.frame_start, scene.frame_end = previous_range
        for track, mute in previous_mutes:
            track.mute = mute
        try:
            anim.action = previous_action
            if previous_slot is not None:
                anim.action_slot = previous_slot
        except Exception:
            pass


def _action_range(action, scene):
    try:
        start, end = action.frame_range
    except Exception:
        start, end = scene.frame_start, scene.frame_end
    if end <= start:
        end = start + 1
    return int(math.floor(start)), int(math.ceil(end))


# --------------------------------------------------------------------------- #
#  Export arguments
# --------------------------------------------------------------------------- #

def build_kwargs(settings, use_selection):
    """Translate the add-on settings into ``export_scene.fbx`` keywords."""
    object_types = set(_ALL_OBJECT_TYPES) & _object_type_items()
    if not settings.include_armatures:
        object_types.discard('ARMATURE')

    kwargs = {
        # scene / selection
        'check_existing': False,
        'use_selection': use_selection,
        'use_visible': bool(settings.only_visible and not use_selection),
        'use_active_collection': False,
        'object_types': object_types,

        # orientation
        'axis_forward': settings.axis_forward,
        'axis_up': settings.axis_up,
        'bake_space_transform': settings.bake_space_transform,
        'global_scale': settings.global_scale,
        'apply_unit_scale': settings.apply_unit_scale,
        'apply_scale_options': settings.apply_scale_options,

        # rig
        'add_leaf_bones': settings.add_leaf_bones,
        'primary_bone_axis': settings.primary_bone_axis,
        'secondary_bone_axis': settings.secondary_bone_axis,
        'use_armature_deform_only': settings.deform_bones_only,
        'armature_nodetype': settings.armature_nodetype,

        # mesh / textures
        'use_mesh_modifiers': settings.apply_modifiers,
        'use_triangles': settings.triangulate,
        'mesh_smooth_type': settings.smooth_type,
        'path_mode': settings.path_mode,
        'embed_textures': settings.embed_textures,

        # batching is handled by this add-on
        'batch_mode': 'OFF',
    }

    mode = settings.anim_mode
    kwargs['bake_anim'] = mode != 'NONE'
    if mode == 'NONE':
        kwargs.update(
            bake_anim_use_all_actions=False,
            bake_anim_use_nla_strips=False,
        )
    else:
        source = settings.embed_source if mode == 'EMBED' else 'ACTIVE'
        kwargs.update(
            bake_anim_use_all_actions=(source == 'ALL_ACTIONS'),
            bake_anim_use_nla_strips=(source == 'NLA'),
            bake_anim_use_all_bones=settings.bake_all_bones,
            bake_anim_force_startend_keying=settings.force_startend,
            bake_anim_step=settings.bake_step,
            bake_anim_simplify_factor=settings.simplify,
        )
    return kwargs


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #

def run_export(context, settings, filepath_override=None):
    """Export according to ``settings``; returns the list of written files."""
    scene = context.scene
    _ensure_object_mode(context)

    objects = gather_objects(context, settings)
    if not objects:
        raise RuntimeError(
            'Nothing to export - select at least one object, '
            'or switch "Export" to "Whole Scene".'
        )

    out_dir = os.path.dirname(filepath_override) if filepath_override else None
    if out_dir is None:
        out_dir = naming.resolve_output_dir(settings)
    os.makedirs(out_dir, exist_ok=True)

    written = []

    # ---- one file per action ------------------------------------------- #
    if settings.anim_mode == 'SEPARATE':
        pairs = collect_action_pairs(objects, settings)
        if pairs:
            kwargs = build_kwargs(settings, use_selection=True)
            with _selected_only(context, objects):
                for index, (obj, action) in enumerate(pairs):
                    path = naming.build_output_path(
                        settings, scene, obj, action, index, out_dir,
                        per_action=True)
                    path = naming.resolve_collision(path, settings.overwrite_mode)
                    _ensure_parent(path)
                    with _action_override(obj, action, scene):
                        export_fbx(path, dict(kwargs))
                    written.append(path)
            return written

    # ---- single file (static or embedded animation) --------------------- #
    obj = primary_object(context, objects)
    action = active_action(objects) if settings.anim_mode != 'NONE' else None

    if filepath_override:
        path = filepath_override
        if not path.lower().endswith('.fbx'):
            path += '.fbx'
    else:
        path = naming.build_output_path(settings, scene, obj, action, 0, out_dir)
        path = naming.resolve_collision(path, settings.overwrite_mode)

    _ensure_parent(path)

    if settings.export_scope == 'SELECTED':
        kwargs = build_kwargs(settings, use_selection=True)
        with _selected_only(context, objects):
            export_fbx(path, kwargs)
    else:
        export_fbx(path, build_kwargs(settings, use_selection=False))

    written.append(path)
    return written
