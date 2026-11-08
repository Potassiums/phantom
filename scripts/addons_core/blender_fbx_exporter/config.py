"""Config persistence for the FBX Template Exporter.

The exporter keeps its settings per scene, but the user should not have to
re-enter the Unreal preset, the output folder and friends every session.
This module stores the current settings as JSON inside the user's Blender
configuration folder and restores them when a fresh scene starts.

Triggers:

* explicit save (``fbx_template.save_config`` operator),
* an automatic, debounced save shortly after any setting changes,
* restoring the last config into a fresh scene (``load_post`` handler,
  see :func:`apply_last_config_to_scene`).
"""

import json
import os

import bpy

#: Debounce state of the deferred auto-save.
_scheduled = False


def config_path():
    """Full path of the JSON config file in the user's config folder."""
    try:
        base = bpy.utils.user_resource('CONFIG', create=True)
    except TypeError:  # Blender < 2.9 style - no ``create`` argument
        base = bpy.utils.user_resource('CONFIG')
    return os.path.join(base, 'fbx_template_exporter.json')


# --------------------------------------------------------------------------- #
#  Serialization
# --------------------------------------------------------------------------- #

def _rna_cast(prop, value):
    """Coerce ``value`` into the type of an RNA property, or None if not valid."""
    ptype = prop.type
    try:
        if ptype in ('BOOL', 'BOOLEAN'):
            return bool(value)
        if ptype == 'INT':
            return int(float(value))
        if ptype == 'FLOAT':
            return float(value)
        return str(value)  # STRING, ENUM (stored by identifier)
    except (TypeError, ValueError):
        return None


def serialize(settings):
    """All primitive settings of a :class:`FBXT_Properties` group as a dict."""
    data = {}
    for prop in settings.bl_rna.properties:
        key = prop.identifier
        if key == 'rna_type' or prop.type in ('POINTER', 'COLLECTION'):
            continue
        try:
            cast = _rna_cast(prop, getattr(settings, key))
        except Exception:
            continue
        if cast is not None:
            data[key] = cast
    return data


def save_config(settings, path=None):
    """Write the current settings to disk. Returns True on success."""
    path = path or config_path()
    try:
        data = {'version': 1, 'settings': serialize(settings)}
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def load_config(path=None):
    """The stored settings dict, or None if there is no valid config file."""
    path = path or config_path()
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception:
        return None
    if isinstance(data, dict):
        return data.get('settings')
    return None


def apply_to_settings(settings, values):
    """Apply a values dict to a settings group. Returns the applied count."""
    # Deferred import: properties imports config lazily too (see properties.py).
    from . import properties

    if not values:
        return 0
    previous_guard = properties._guard
    properties._guard = True  # suppress the preset/preview update callbacks
    applied = 0
    try:
        for key, value in values.items():
            prop = settings.bl_rna.properties.get(key)
            if prop is None or prop.type in ('POINTER', 'COLLECTION'):
                continue
            cast = _rna_cast(prop, value)
            if cast is None:
                continue
            if prop.type == 'ENUM':
                try:
                    valid = {item.identifier for item in prop.enum_items}
                except Exception:
                    valid = None
                if valid and cast not in valid:
                    continue
            try:
                setattr(settings, key, cast)
                applied += 1
            except (AttributeError, TypeError, ValueError):
                continue
    finally:
        properties._guard = previous_guard
    # Normalize preset-dependent values once (axes, scale mode, bone axes).
    properties._apply_orientation_preset(settings, None)
    properties._apply_bone_preset(settings, None)
    return applied


# --------------------------------------------------------------------------- #
#  Restore on start
# --------------------------------------------------------------------------- #

def _auto_apply_enabled():
    """Honour the add-on preference toggle (default: on)."""
    try:
        prefs = bpy.context.preferences.addons.get(__package__)
        if prefs is not None and not prefs.preferences.remember_last_config:
            return False
    except Exception:
        pass
    return True


def apply_last_config_to_scene(scene=None):
    """Restore the last saved config into a scene that has no settings yet.

    A .blend that already carries exporter settings of its own always wins,
    so project files are never clobbered by the last-used config.
    """
    if not _auto_apply_enabled():
        return False
    if scene is None:
        try:
            scene = bpy.context.scene
        except Exception:
            return False
    if scene is None or 'fbxt_settings' in scene.keys():
        return False
    settings = getattr(scene, 'fbxt_settings', None)
    if settings is None:
        return False
    values = load_config()
    if not values:
        return False
    apply_to_settings(settings, values)
    return True


# --------------------------------------------------------------------------- #
#  Debounced auto-save on every setting change
# --------------------------------------------------------------------------- #

def _save_from_timer():
    global _scheduled
    _scheduled = False
    try:
        scene = bpy.context.scene
    except Exception:
        return None
    settings = getattr(scene, 'fbxt_settings', None)
    if settings is not None:
        save_config(settings)


def schedule_save():
    """Queue one debounced config save for shortly after the edit storm."""
    global _scheduled
    if _scheduled or not _auto_apply_enabled():
        return
    try:
        bpy.app.timers.register(_save_from_timer, first_interval=0.5)
        _scheduled = True
    except Exception:
        pass
