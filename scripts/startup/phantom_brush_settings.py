# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: sculpt brushes remember their settings between sessions.

A brush's settings live in the .blend file, and Phantom usually starts from a new file, so
changes to a brush (size, strength, spacing, falloff, alpha, ...) are lost when Phantom is
closed. This module mirrors the settings of every sculpt brush into
``phantom_brush_settings.json`` in the user config directory, and puts them back as brushes are
loaded, so a brush feels the same in every file.

Only simple values are stored (numbers, booleans, text, choices and their small arrays), plus
the brush's texture slots and the names of their textures. Everything else in a brush (its
preview, asset metadata and so on) is left alone.
"""

import atexit
import json
import os
import time

import bpy
from bpy.app.handlers import persistent

FILE_NAME = "phantom_brush_settings.json"
FORMAT_VERSION = 1
# How often loaded brushes are checked for changes, and how long changes wait before the file
# is written (a slider drag changes the brush on every mouse move).
POLL_INTERVAL = 0.5
SAVE_DELAY = 2.0

# The brush's identity, and data owned by Blender rather than the user.
_SKIP_PROPS = {
    "name", "name_full", "library", "asset_data", "preview", "has_unsaved_changes",
    "users", "use_fake_user", "use_extra_user", "original", "is_evaluated",
    "is_linked", "is_library_indirect", "is_embedded_data", "is_runtime_data",
    "session_uid", "tag", "id_data", "rna_type", "output_node",
    # Which paint modes the brush is offered in is asset placement, not brush behaviour.
    "use_paint_sculpt", "use_paint_uv_sculpt", "use_paint_vertex", "use_paint_weight",
    "use_paint_image", "use_paint_grease_pencil", "use_vertex_grease_pencil",
    "use_paint_sculpt_curves",
}
# Pointers to other data-blocks, stored by name and restored when that data-block is loaded.
_ID_PROPS = ("texture", "mask_texture")
# Pointers to a brush's own settings: their properties are stored like the brush's own.
_CHILD_TYPES = {}
_SIMPLE_TYPES = {'BOOLEAN', 'INT', 'FLOAT', 'ENUM', 'STRING'}
_TOOLS_PROPS = { 'use_unified_size', 'size', 'strength' }

_data = {}
_tools = { "loaded" : False }
_seen = {}
_dirty = False
_last_save = 0.0
_settings_path = None


# -----------------------------------------------------------------------------
# Storage

def _path():
    global _settings_path
    if _settings_path is None:
        _settings_path = os.path.join(bpy.utils.user_resource('CONFIG'), FILE_NAME)
    
    print(_settings_path)
    return _settings_path


def _load():
    global _data
    global _tools
    try:
        with open(_path(), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    version = payload.get("version", 0)
    if not isinstance(version, int) or version > FORMAT_VERSION:
        return
    brushes = payload.get("brushes")
    if isinstance(brushes, dict):
        _data = {
            name: settings for name, settings in brushes.items()
            if isinstance(name, str) and isinstance(settings, dict)
        }

    tools = payload.get("tool_settings")

    if isinstance(tools, dict):
        _tools = { }
        
        for key, value in tools.items():
            _tools[key] = value
            print(key, value)
        _tools["loaded"] = False

def _save():
    global _dirty
    payload = { "version": FORMAT_VERSION, "brushes": _data, "tool_settings": _tools }
    path = _path()
    temp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(temp, path)
    except OSError as ex:
        print("Phantom: could not save brush settings:", ex)
        return
    _dirty = False


# -----------------------------------------------------------------------------
# Reading and writing a brush's settings

def _simple_value(struct, prop):
    """A property's value as JSON data, or None when it can't be stored."""
    try:
        value = getattr(struct, prop.identifier)
    except (AttributeError, TypeError, ValueError):
        return None
    try:
        if prop.type == 'ENUM':
            # Some enums are context dependent and have no active item outside their mode.
            return str(value) or None
        if prop.type == 'STRING':
            return str(value)
        if prop.is_array:
            if prop.type == 'BOOLEAN':
                return [bool(item) for item in value]
            if prop.type == 'INT':
                return [int(item) for item in value]
            return [float(item) for item in value]
        if prop.type == 'BOOLEAN':
            return bool(value)
        if prop.type == 'INT':
            return int(value)
        return float(value)
    except (TypeError, ValueError):
        return None


def _collect_props(struct):
    props = {}
    for prop in struct.bl_rna.properties:
        ident = prop.identifier
        if ident.startswith("_") or ident in _SKIP_PROPS or ident in _ID_PROPS:
            continue
        if prop.type == 'POINTER':
            fixed = prop.fixed_type
            if fixed is None or fixed.identifier not in _CHILD_TYPES:
                continue
            try:
                child = getattr(struct, ident)
            except (AttributeError, TypeError, ValueError):
                continue
            if child is not None:
                child_props = _collect_props(child)
                if child_props:
                    props[ident] = child_props
            continue
        if prop.is_readonly or prop.type not in _SIMPLE_TYPES:
            continue
        value = _simple_value(struct, prop)
        if value is not None:
            props[ident] = value
    return props


def _collect(brush):
    textures = {}
    for ident in _ID_PROPS:
        texture = getattr(brush, ident, None)
        textures[ident] = texture.name if texture is not None else None
    return {"props": _collect_props(brush), "textures": textures}


def _apply_props(struct, props):
    for ident, value in props.items():
        prop = struct.bl_rna.properties.get(ident)
        if prop is None:
            continue
        if prop.type == 'POINTER':
            fixed = prop.fixed_type
            if (fixed is None or fixed.identifier not in _CHILD_TYPES or
                    not isinstance(value, dict)):
                continue
            try:
                child = getattr(struct, ident)
            except (AttributeError, TypeError, ValueError):
                continue
            if child is not None:
                _apply_props(child, value)
            continue
        if prop.is_readonly or prop.type not in _SIMPLE_TYPES:
            continue
        try:
            if prop.type == 'BOOLEAN':
                if prop.is_array:
                    setattr(struct, ident, [bool(item) for item in value])
                else:
                    setattr(struct, ident, bool(value))
            elif prop.type == 'INT':
                if prop.is_array:
                    setattr(struct, ident, [int(item) for item in value])
                else:
                    setattr(struct, ident, int(value))
            elif prop.type == 'FLOAT':
                if prop.is_array:
                    setattr(struct, ident, [float(item) for item in value])
                else:
                    setattr(struct, ident, float(value))
            else:
                setattr(struct, ident, str(value))
        except (AttributeError, TypeError, ValueError):
            # A setting from an older Phantom (or another Blender version) may not fit.
            continue


def _apply(brush, settings):
    props = settings.get("props")
    if isinstance(props, dict):
        _apply_props(brush, props)
    textures = settings.get("textures")
    if not isinstance(textures, dict):
        return
    for ident in _ID_PROPS:
        name = textures.get(ident)
        try:
            if name is None:
                setattr(brush, ident, None)
            elif isinstance(name, str):
                texture = bpy.data.textures.get(name)
                if texture is not None:
                    setattr(brush, ident, texture)
        except (AttributeError, TypeError, ValueError):
            continue


# -----------------------------------------------------------------------------
# Keeping the JSON in step with the brushes in the app

def _sculpt_brushes():
    # for brush in bpy.data.brushes:
    #     json_formatted_str = json.dumps(brush.__dict__)
    #     print(json_formatted_str)
        
    return [brush for brush in bpy.data.brushes if brush.use_paint_sculpt]


def _update_brushes():
    global _dirty
    present = set()
    for brush in _sculpt_brushes():
        name = brush.name
        present.add(name)
        try:
            snapshot = _collect(brush)
        except Exception as ex:  # A broken datablock shouldn't stop the others. 
            print("Phantom: could not read brush settings:", ex)
            continue
        if name not in _seen:
            # A brush just loaded (or a new file just opened): put its saved settings back.
            stored = _data.get(name)
            if isinstance(stored, dict):
                _apply(brush, stored)
                snapshot = _collect(brush)
        elif snapshot != _seen[name]:
            _data[name] = snapshot
            _dirty = True
        _seen[name] = snapshot
    for name in list(_seen):
        if name not in present:
            del _seen[name]

def _update_tool_settings():
    if "loaded" not in _tools or _tools["loaded"] == False:
        return
    
    for key in _TOOLS_PROPS:
        _tools[key] = getattr(bpy.context.scene.tool_settings.sculpt.unified_paint_settings, key)


def _load_tool_settings():
    if not hasattr(bpy.context, 'scene'):
        return 1.0

    for key in _TOOLS_PROPS:
        if key in _tools:
            value = _tools[key]
            setattr(bpy.context.scene.tool_settings.sculpt.unified_paint_settings, key, value)

    _tools["loaded"] = True
    bpy.app.timers.unregister(_load_tool_settings)

def _tick():
    global _last_save
    try:
        _update_brushes()
    except Exception as ex:  # Keep the timer alive, whatever a brush does.
        print("Phantom: could not update brush settings:", ex)

    try:
        _update_tool_settings()
    except Exception as ex: 
        print("Phantom: could not update tool settings", ex)

    now = time.monotonic()

    if _dirty and now - _last_save >= SAVE_DELAY:
        # _save()
        _last_save = now
    
    # for property in dir(bpy.data.brushes):
    #     print(property)

    return POLL_INTERVAL


# @persistent
# def _on_load(*_args):
    # Brushes of the file that just loaded are new to this session: restore their settings.
    # _seen.clear()


@persistent
def _on_exit():
    _save()


def register():
    _load()
    _seen.clear()
    # bpy.app.handlers.load_post.append(_on_load)
    atexit.register(_on_exit)
    if not bpy.app.background:
        bpy.app.timers.register(_tick, first_interval=POLL_INTERVAL, persistent=True)
    
    if not bpy.app.background:
        bpy.app.timers.register(_load_tool_settings, first_interval = 1.0, persistent = False)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)

    if bpy.app.timers.is_registered(_load_tool_settings):
        bpy.app.timers.unregister(_load_tool_settings)
    # if _on_load in bpy.app.handlers.load_post:
    #     bpy.app.handlers.load_post.remove(_on_load)
    # Phantom calls unregister() on exit, before atexit runs: save what the last timer tick
    # may not have written yet.
    _save()
    atexit.unregister(_on_exit)
