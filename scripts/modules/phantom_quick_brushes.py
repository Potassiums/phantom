# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: quick-brush slots for Sculpt Mode.

Each slot remembers a brush asset (by its asset reference) and is shown as a square
thumbnail button under the Brush Asset panel, four per row. There are always at least
four slots; dropping brushes from the asset shelf adds more.

Slots are stored in ``phantom_quick_brushes.json`` in the user config directory, and each
brush's thumbnail is cached as a PNG next to it, so slots look right in every file.
"""

__all__ = (
    "MIN_SLOTS",
    "add",
    "assign",
    "current_reference",
    "draw",
    "remove",
    "slot_name",
    "slots",
)

import hashlib
import json
import os

import bpy

MIN_SLOTS = 4
SLOTS_PER_ROW = 4
_REQUIRED_KEYS = ("type", "identifier", "relative")

_slots = None
_previews = None


# -----------------------------------------------------------------------------
# Storage

def _config_dir():
    return bpy.utils.user_resource('CONFIG')


def _path():
    return os.path.join(_config_dir(), "phantom_quick_brushes.json")


def slots():
    """The slot list (lazily loaded): each entry is an asset reference dict or None."""
    global _slots
    if _slots is None:
        try:
            with open(_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = []
        _slots = []
        for slot in data if isinstance(data, list) else ():
            if isinstance(slot, dict) and all(isinstance(slot.get(k), str) for k in _REQUIRED_KEYS):
                _slots.append({k: slot[k] for k in _REQUIRED_KEYS})
            else:
                _slots.append(None)
        _trim()
    return _slots


def _trim():
    """Keep at least the minimum slots, and no empty slots past the minimum."""
    while len(_slots) < MIN_SLOTS:
        _slots.append(None)
    while len(_slots) > MIN_SLOTS and _slots[-1] is None:
        _slots.pop()


def _save():
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(slots(), fh, indent=2)
    except OSError as ex:
        print("Phantom: could not save quick brushes:", ex)


# -----------------------------------------------------------------------------
# Thumbnails

def _thumbnail_path(slot):
    digest = hashlib.sha1(f"{slot['type']}|{slot['identifier']}|{slot['relative']}".encode()).hexdigest()
    return os.path.join(_config_dir(), "phantom_quick_brushes", digest[:16] + ".png")


def _loaded_brush(slot):
    name = slot_name(slot)
    for brush in bpy.data.brushes:
        if brush.name == name and brush.use_paint_sculpt:
            return brush
    return None


def _cache_thumbnail(slot):
    """Save the slot brush's thumbnail as a PNG, so it shows even when the brush isn't loaded."""
    brush = _loaded_brush(slot)
    if brush is None:
        return
    preview = brush.preview_ensure()
    width, height = preview.image_size
    if width == 0 or height == 0:
        return
    path = _thumbnail_path(slot)
    image = bpy.data.images.new("phantom_quick_brush_thumbnail", width, height, alpha=True)
    try:
        # Preview pixels are display-ready, written raw into a byte image.
        image.pixels.foreach_set(preview.image_pixels_float[:])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        image.filepath_raw = path
        image.file_format = 'PNG'
        image.save()
    except (OSError, RuntimeError) as ex:
        print("Phantom: could not cache quick brush thumbnail:", ex)
    finally:
        bpy.data.images.remove(image)
    # Make the next draw load the new file.
    if _previews is not None and path in _previews:
        del _previews[path]


def _slot_icon(slot):
    """The slot's thumbnail icon: cached PNG, else the loaded brush's preview, else 0."""
    global _previews
    path = _thumbnail_path(slot)
    if os.path.exists(path):
        if _previews is None:
            import bpy.utils.previews
            _previews = bpy.utils.previews.new()
        preview = _previews.get(path) or _previews.load(path, path, 'IMAGE')
        return preview.icon_id
    brush = _loaded_brush(slot)
    if brush is not None and brush.preview:
        return brush.preview.icon_id
    return 0


# -----------------------------------------------------------------------------
# Editing

def current_reference(context):
    """Asset reference dict of the active sculpt brush, or None if it isn't an asset."""
    ref = context.tool_settings.sculpt.brush_asset_reference
    if ref is None or not ref.relative_asset_identifier:
        return None
    return {
        "type": ref.asset_library_type,
        "identifier": ref.asset_library_identifier,
        "relative": ref.relative_asset_identifier,
    }


def slot_name(slot):
    return slot["relative"].rsplit("/", 1)[-1]


def assign(index, slot):
    """Put a brush in a slot (the brush must be loaded, i.e. active, for its thumbnail)."""
    items = slots()
    while len(items) <= index:
        items.append(None)
    items[index] = slot
    _cache_thumbnail(slot)
    _save()


def add(slot):
    """Add a brush to the first empty slot, or a new slot. Returns the slot index."""
    items = slots()
    if slot in items:
        return items.index(slot)
    index = items.index(None) if None in items else len(items)
    assign(index, slot)
    return index


def remove(index):
    items = slots()
    if 0 <= index < len(items):
        if index >= MIN_SLOTS:
            del items[index]
        else:
            items[index] = None
        _trim()
        _save()


def unregister():
    global _previews
    if _previews is not None:
        import bpy.utils.previews
        bpy.utils.previews.remove(_previews)
        _previews = None


# -----------------------------------------------------------------------------
# Drawing

def draw(layout, context):
    """Draw the slots as rows of square thumbnail buttons."""
    current = current_reference(context)
    items = slots()
    col = layout.column(align=True)
    for row_start in range(0, len(items), SLOTS_PER_ROW):
        rest = col.row(align=True)
        for i in range(row_start, row_start + SLOTS_PER_ROW):
            # Icon-only buttons don't stretch in a row, so split the width into equal parts.
            remaining = row_start + SLOTS_PER_ROW - i
            if remaining > 1:
                split = rest.split(factor=1.0 / remaining, align=True)
                cell = split.column(align=True)
                rest = split.column(align=True)
            else:
                cell = rest.column(align=True)
            if i >= len(items):
                continue
            # Tall buttons draw the brush thumbnail filling the button (see Phantom's layout tweak).
            cell.scale_y = 2.8
            slot = items[i]
            if slot is None:
                cell.operator("sculpt.phantom_quick_brush", text="", icon='ADD').slot = i
                continue
            icon = _slot_icon(slot)
            kwargs = {"icon_value": icon} if icon else {"icon": 'BRUSH_DATA'}
            cell.operator(
                "sculpt.phantom_quick_brush", text="", depress=(slot == current), **kwargs,
            ).slot = i
