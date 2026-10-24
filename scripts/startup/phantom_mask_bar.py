# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: floating mask and polygroup actions for Sculpt Mode.

A small bar slides up at the bottom of the 3D Viewport when there is something to act on:

- **Mask bar**, while the mesh has a mask (or hidden parts). By default:
  Hide Masked, Unhide, Split Hidden (like ZBrush) and Extract.
- **Polygroup bar**, after turning the mask into a polygroup (Ctrl+W), or isolating a
  polygroup (Ctrl+Shift-click). Its actions work on that polygroup. By default: Isolate (again: swap), Hide, Unhide and Split Hidden.
  Its "x" puts the bar away, as does picking another tool or brush; masking again brings back
  the mask bar.

The bar goes away when the mask is cleared and nothing is hidden.

Each bar is customizable: its "+" button (or right-clicking it) lists more actions to add or
remove. The choices are remembered between sessions (stored in ``phantom_mask_bar.json`` in
the user config directory).

Also here: the **Mask Shape** buttons (at the top of Brush Settings in Sculpt Mode) choose what
Ctrl-drag draws off the mesh: a box, a lasso or a circle (``sculpt.phantom_mask_circle``, drawn
from its center). Ctrl-drag on the mesh still paints the mask.
"""

import json
import math
import os
import time
from itertools import compress

import blf
import bmesh
import bpy
import gpu
from bpy.types import Operator, Panel
from gpu_extras.batch import batch_for_shader

# Unscaled pixels.
BAR_HEIGHT = 28
BAR_MARGIN = 14
BUTTON_PAD_X = 12
BUTTON_GAP = 2
CORNER = 8
TEXT_SIZE = 11
SLIDE = 12
FADE_TIME = 0.16

BAR_COLOR = (0.105, 0.105, 0.125, 0.92)
OUTLINE_COLOR = (0.55, 0.55, 1.0, 0.35)   # Phantom periwinkle.
HOVER_COLOR = (0.55, 0.55, 1.0, 0.28)
TEXT_COLOR = (0.92, 0.92, 0.95)
ACCENT_COLOR = (1.0, 0.62, 0.34)          # Phantom orange, for actions making new objects.

MASK = 'MASK'
POLYGROUP = 'POLYGROUP'


class Action:
    """An action a bar can show."""
    __slots__ = ("identifier", "label", "group", "needs", "operator", "properties", "invoke",
                 "accent", "description")

    def __init__(self, identifier, label, group, needs, operator, properties=None, *,
                 invoke=False, accent=False, description=""):
        self.identifier = identifier
        self.label = label
        self.group = group
        # What must be there for the action to be available:
        # 'MASK', 'HIDDEN' or 'POLYGROUP' (the polygroup the bar works on).
        self.needs = needs
        self.operator = operator
        # Operator properties, or a function of the polygroup returning them.
        self.properties = properties or {}
        # Invoke (for actions that ask for settings or a click first) instead of running.
        self.invoke = invoke
        self.accent = accent
        self.description = description


def _on_polygroup(**properties):
    """Properties for an operator acting on the bar's polygroup."""
    return lambda polygroup: {**properties, "active_face_set": polygroup}


_UNHIDE = dict(identifier='SHOW', label="Unhide", group="Visibility", needs='HIDDEN',
               operator="paint.hide_show_all", properties={"action": 'SHOW'},
               description="Show everything again")
_SPLIT = dict(identifier='SPLIT', label="Split Hidden", group="New Objects", needs='HIDDEN',
              operator="sculpt.phantom_split_hidden", accent=True,
              description="Separate the hidden part into its own object")

# Every action each bar can show, in the order they are drawn.
CATALOGS = {
    MASK: (
        Action('HIDE', "Hide Masked", "Visibility", 'MASK', "paint.hide_show_masked",
               {"action": 'HIDE'}, description="Hide the masked area"),
        Action(**_UNHIDE),
        Action(**_SPLIT),
        Action('EXTRACT', "Extract", "New Objects", 'MASK', "sculpt.paint_mask_extract",
               invoke=True, accent=True,
               description="Make a new object from the masked area, with a thickness"),
        Action('SLICE', "Slice", "New Objects", 'MASK', "sculpt.paint_mask_slice",
               {"new_object": True}, accent=True,
               description="Cut the masked area off into its own object, filling the holes"),
        Action('INVERT', "Invert", "Mask", 'MASK', "paint.mask_flood_fill", {"mode": 'INVERT'},
               description="Invert the mask"),
        Action('CLEAR', "Clear", "Mask", 'MASK', "paint.mask_flood_fill",
               {"mode": 'VALUE', "value": 0.0}, description="Clear the mask"),
        Action('SMOOTH', "Smooth", "Mask", 'MASK', "sculpt.mask_filter",
               {"filter_type": 'SMOOTH'}, description="Smooth the mask's edges"),
        Action('SHARPEN', "Sharpen", "Mask", 'MASK', "sculpt.mask_filter",
               {"filter_type": 'SHARPEN'}, description="Sharpen the mask's edges"),
        Action('GROW', "Grow", "Mask", 'MASK', "sculpt.mask_filter",
               {"filter_type": 'GROW'}, description="Grow the mask"),
        Action('SHRINK', "Shrink", "Mask", 'MASK', "sculpt.mask_filter",
               {"filter_type": 'SHRINK'}, description="Shrink the mask"),
        Action('CONTRAST_UP', "Contrast +", "Mask", 'MASK', "sculpt.mask_filter",
               {"filter_type": 'CONTRAST_INCREASE'}, description="Increase the mask's contrast"),
        Action('CONTRAST_DOWN', "Contrast -", "Mask", 'MASK', "sculpt.mask_filter",
               {"filter_type": 'CONTRAST_DECREASE'}, description="Decrease the mask's contrast"),
        Action('POLYGROUP', "To Polygroup", "Polygroups", 'MASK',
               "sculpt.phantom_mask_to_face_set",
               description="Turn the masked area into a polygroup and clear the mask"),
    ),
    POLYGROUP: (
        Action('ISOLATE', "Isolate", "Visibility", 'POLYGROUP',
               "sculpt.face_set_change_visibility", _on_polygroup(mode='ISOLATE', over_mesh=True),
               description="Show only this polygroup. Again: show everything else instead"),
        Action('HIDE', "Hide", "Visibility", 'POLYGROUP', "sculpt.face_set_change_visibility",
               _on_polygroup(mode='HIDE_ACTIVE'), description="Hide this polygroup"),
        Action(**_UNHIDE),
        Action(**_SPLIT),
        Action('EXTRACT', "Extract", "New Objects", 'POLYGROUP', "sculpt.face_set_extract",
               invoke=True, accent=True,
               description="Click a polygroup to make a new object from it"),
        Action('DELETE', "Delete", "Geometry", 'POLYGROUP', "sculpt.face_set_edit",
               _on_polygroup(mode='DELETE_GEOMETRY'),
               description="Delete the faces of this polygroup"),
        Action('FAIR', "Fair", "Geometry", 'POLYGROUP', "sculpt.face_set_edit",
               _on_polygroup(mode='FAIR_POSITIONS'),
               description="Make this polygroup the smoothest possible patch"),
        Action('GROW', "Grow", "Polygroup", 'POLYGROUP', "sculpt.face_set_edit",
               _on_polygroup(mode='GROW'), description="Grow this polygroup by one face"),
        Action('SHRINK', "Shrink", "Polygroup", 'POLYGROUP', "sculpt.face_set_edit",
               _on_polygroup(mode='SHRINK'), description="Shrink this polygroup by one face"),
        Action('COLORS', "New Colors", "Polygroup", 'POLYGROUP',
               "sculpt.face_sets_randomize_colors", description="Give the polygroups new colors"),
        Action('POLYFRAME', "PolyFrame", "Polygroup", 'POLYGROUP', "sculpt.phantom_polyframe",
               description="Show or hide polygroups with the wireframe"),
    ),
}
DEFAULTS = {
    MASK: ('HIDE', 'SHOW', 'SPLIT', 'EXTRACT'),
    POLYGROUP: ('ISOLATE', 'HIDE', 'SHOW', 'SPLIT'),
}
TITLES = {MASK: "Mask Actions Bar", POLYGROUP: "Polygroup Actions Bar"}
# Settings keys, per bar.
SETTINGS_KEYS = {MASK: "actions", POLYGROUP: "polygroup_actions"}
# Buttons every bar ends with: customize it, and (polygroup bar) put it away.
CUSTOMIZE = 'CUSTOMIZE'
DISMISS = 'DISMISS'

_settings = {key: list(DEFAULTS[bar]) for bar, key in SETTINGS_KEYS.items()}
_settings["mask_shape"] = 'BOX'

# What Ctrl-drag draws off the mesh: (identifier, label, description, toolbar icon).
MASK_SHAPES = (
    ('BOX', "Box", "Ctrl-drag off the mesh draws a box mask", "ops.sculpt.border_mask"),
    ('LASSO', "Lasso", "Ctrl-drag off the mesh draws a lasso mask", "ops.sculpt.lasso_mask"),
    ('CIRCLE', "Circle", "Ctrl-drag off the mesh draws a circle mask, from its center",
     "ops.generic.select_circle"),
)
_draw_handle = None
_addon_keymaps = []
_audio = {"device": None}
# The polygroup each object's polygroup bar works on, by object name, with the tool and brush
# in use then: `(polygroup, tool, brush)`.
_polygroups = {}
# Whether the mesh really has a mask / hidden faces, rechecked after each operator or stroke:
# `(key, (has_mask, has_hidden))`.
_state_cache = [None, None]
# The polygroup found isolated (alone visible, or alone hidden) at the last check, by object
# name: the polygroup bar comes up when this changes.
_isolated = {}

# Appear/disappear animation: `_alpha` moves towards `_target`.
_alpha = 0.0
_target = 0.0
_fade_last_time = 0.0
_hover = None
# The bar drawn last, and the one being customized.
_shown_bar = None
_customizing = MASK


# -----------------------------------------------------------------------------
# Persistent settings

def _settings_path():
    return os.path.join(bpy.utils.user_resource('CONFIG'), "phantom_mask_bar.json")


def _load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    for bar, key in SETTINGS_KEYS.items():
        chosen = data.get(key)
        if isinstance(chosen, list):
            known = {action.identifier for action in CATALOGS[bar]}
            _settings[key] = [a for a in chosen if a in known]
    if data.get("mask_shape") in {shape[0] for shape in MASK_SHAPES}:
        _settings["mask_shape"] = data["mask_shape"]


def _save_settings():
    path = _settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_settings, fh, indent=2)
    except OSError as ex:
        print("Phantom: could not save mask actions bar settings:", ex)


def shown_actions(bar):
    """The actions on a bar, in catalog order."""
    chosen = set(_settings[SETTINGS_KEYS[bar]])
    return [action for action in CATALOGS[bar] if action.identifier in chosen]


def set_action_shown(bar, identifier, shown):
    key = SETTINGS_KEYS[bar]
    chosen = [a for a in _settings[key] if a != identifier]
    if shown:
        chosen.append(identifier)
    _settings[key] = chosen
    _save_settings()
    _redraw_views()


# -----------------------------------------------------------------------------
# State

def _ui_scale():
    return bpy.context.preferences.system.ui_scale


def mask_state(context):
    """(has_mask, has_hidden) for the mesh being sculpted, or None outside Sculpt Mode."""
    ob = context.active_object
    if context.mode != 'SCULPT' or ob is None or ob.type != 'MESH':
        return None
    attributes = ob.data.attributes
    # Erasing or clearing a mask can leave it all zero, so look at the values, but only once
    # per stroke or edit (each one pushes an undo step), not on every redraw. They are read where
    # sculpting keeps them: with Multires or Dyntopo that isn't the mesh's own attributes.
    key = (ob.as_pointer(), ob.data.as_pointer(),
           attributes.get(".sculpt_mask") is not None, attributes.get(".hide_poly") is not None,
           bpy.types.WindowManager.phantom_undo_push_count())
    if _state_cache[0] != key:
        _state_cache[0] = key
        flags = bpy.types.WindowManager.phantom_sculpt_mask_state()
        _state_cache[1] = (bool(flags & 1), bool(flags & 2))
        _check_isolated(ob, attributes.get(".hide_poly") if _state_cache[1][1] else None)
    return _state_cache[1]


def _isolated_polygroup(mesh, hide):
    """The polygroup that is alone visible, or else alone hidden, or None."""
    import numpy as np
    face_sets = mesh.attributes.get(".sculpt_face_set")
    if hide is None or face_sets is None or len(face_sets.data) != len(hide.data):
        return None
    hidden = np.empty(len(hide.data), dtype=bool)
    hide.data.foreach_get("value", hidden)
    ids = np.empty(len(face_sets.data), dtype=np.int32)
    face_sets.data.foreach_get("value", ids)
    for faces in (ids[~hidden], ids[hidden]):
        if len(faces) and (faces == faces[0]).all():
            return int(faces[0])
    return None


def _check_isolated(ob, hide):
    """Isolating a polygroup (Ctrl+Shift-click, or the polygroup bar) brings up its bar. Only
    a change does, so a dismissed bar or another tool keeps it away while sculpting on."""
    polygroup = _isolated_polygroup(ob.data, hide)
    if polygroup == _isolated.get(ob.name):
        return
    _isolated[ob.name] = polygroup
    if polygroup is not None:
        set_current_polygroup(ob, polygroup)


@bpy.app.handlers.persistent
def _forget_state(*_args):
    """Undo and redo change the mask without adding an operator: look again."""
    _state_cache[0] = None


def _tool_and_brush(context):
    tool = context.workspace.tools.from_space_view3d_mode('SCULPT', create=False)
    brush = context.tool_settings.sculpt.brush
    return (tool.idname if tool else ""), (brush.name if brush else "")


def current_polygroup(context):
    """The polygroup the polygroup bar works on for the active object, or None. Picking
    another tool or brush puts the polygroup bar away."""
    ob = context.active_object
    entry = _polygroups.get(ob.name) if ob is not None else None
    if entry is None:
        return None
    polygroup, tool, brush = entry
    if (tool, brush) != _tool_and_brush(context):
        del _polygroups[ob.name]
        return None
    return polygroup


def set_current_polygroup(ob, polygroup):
    """Show the polygroup bar for `ob`, working on `polygroup` (None puts it away)."""
    if polygroup is None:
        _polygroups.pop(ob.name, None)
    else:
        _polygroups[ob.name] = (polygroup, *_tool_and_brush(bpy.context))
    _redraw_views()


def bar_for(context):
    """Which bar to show ('MASK', 'POLYGROUP' or None) and the mask state."""
    state = mask_state(context)
    if state is None:
        return None, None
    has_mask, has_hidden = state
    if has_mask:
        return MASK, state
    if current_polygroup(context) is not None:
        return POLYGROUP, state
    return (MASK if has_hidden else None), state


def _available(action, state, context):
    """Whether the action can do anything now."""
    has_mask, has_hidden = state
    if action.needs == 'MASK':
        return has_mask
    if action.needs == 'HIDDEN':
        return has_hidden
    return current_polygroup(context) is not None


def _play_sound(name):
    """Play one of Phantom's interface sounds (``datafiles/sounds/<name>.wav``), if enabled."""
    view = bpy.context.preferences.view
    if bpy.app.background or not getattr(view, "use_ui_sounds", False) or view.ui_sound_volume <= 0:
        return
    path = bpy.utils.system_resource('DATAFILES', path=os.path.join("sounds", name + ".wav"))
    if not path or not os.path.exists(path):
        return
    try:
        import aud
        if _audio["device"] is None:
            _audio["device"] = aud.Device()
        _audio["device"].play(aud.Sound(path).volume(view.ui_sound_volume / 100.0))
    except Exception as ex:
        print("Phantom: could not play sound:", ex)


# -----------------------------------------------------------------------------
# Layout

def _bottom_inset(area, region):
    """Height of the asset shelf where it overlaps the viewport's bottom."""
    inset = 0
    for other in area.regions:
        if other.type not in {'ASSET_SHELF', 'ASSET_SHELF_HEADER'} or other.width <= 1:
            continue
        if other.height > 1 and other.y < region.y + region.height and other.y + other.height > region.y:
            inset = max(inset, other.y + other.height - region.y)
    return inset


def _text_size():
    return TEXT_SIZE * _ui_scale()


def bar_layout(area, region, bar=None):
    """The bar's rectangle and its buttons as ``(identifier, label, x, y, w, h)``."""
    bar = bar or _shown_bar or MASK
    s = _ui_scale()
    blf.size(0, _text_size())
    items = [(action.identifier, action.label) for action in shown_actions(bar)]
    items.append((CUSTOMIZE, "+"))
    if bar == POLYGROUP:
        items.append((DISMISS, "×"))
    widths = [blf.dimensions(0, label)[0] + 2 * BUTTON_PAD_X * s for _id, label in items]
    h = BAR_HEIGHT * s
    w = sum(widths) + BUTTON_GAP * s * (len(items) + 1)
    x = round((region.width - w) / 2)
    y = round(_bottom_inset(area, region) + BAR_MARGIN * s - (1.0 - _alpha) * SLIDE * s)
    buttons = []
    bx = x + BUTTON_GAP * s
    for (identifier, label), bw in zip(items, widths):
        buttons.append((identifier, label, bx, y + BUTTON_GAP * s, bw, h - 2 * BUTTON_GAP * s))
        bx += bw + BUTTON_GAP * s
    return (x, y, w, h), buttons


def _hit(rect, mx, my):
    x, y, w, h = rect
    return x <= mx <= x + w and y <= my <= y + h


def _button_at(area, region, mx, my):
    bar, buttons = bar_layout(area, region)
    if not _hit(bar, mx, my):
        return None, False
    for identifier, _label, x, y, w, h in buttons:
        if _hit((x, y, w, h), mx, my):
            return identifier, True
    return None, True


def _action(bar, identifier):
    return next((a for a in CATALOGS[bar] if a.identifier == identifier), None)


# -----------------------------------------------------------------------------
# Drawing

def _rounded_rect(x, y, w, h, r, segments=6):
    """Triangles and outline of a rounded rectangle (a fan around its center)."""
    r = min(r, w / 2, h / 2)
    corners = ((x + w - r, y + h - r, 0.0), (x + r, y + h - r, 0.5 * math.pi),
               (x + r, y + r, math.pi), (x + w - r, y + r, 1.5 * math.pi))
    outline = []
    for cx, cy, start in corners:
        for i in range(segments + 1):
            a = start + 0.5 * math.pi * i / segments
            outline.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    center = (x + w / 2, y + h / 2)
    tris = []
    for i in range(len(outline)):
        tris += (center, outline[i], outline[(i + 1) % len(outline)])
    return tris, outline


def _draw_shape(shader, kind, coords, color):
    batch = batch_for_shader(shader, kind, {"pos": coords})
    shader.uniform_float("color", color)
    batch.draw(shader)


def _update_shown_bar(bar):
    """Fade the bar in or out; switching bars slides the new one up."""
    global _shown_bar, _alpha
    if bar is not None and _shown_bar is not None and bar != _shown_bar:
        _alpha = 0.0
        _set_target(0.0)
    if bar is not None:
        _shown_bar = bar
    _set_target(1.0 if bar is not None else 0.0)


def draw_callback():
    context = bpy.context
    region = context.region
    bar, state = bar_for(context)
    _update_shown_bar(bar)
    if region is None or _alpha <= 0.001 or state is None or _shown_bar is None:
        return

    s = _ui_scale()
    rect, buttons = bar_layout(context.area, region)
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')

    tris, outline = _rounded_rect(*rect, CORNER * s)
    _draw_shape(shader, 'TRIS', tris, (*BAR_COLOR[:3], BAR_COLOR[3] * _alpha))
    gpu.state.line_width_set(max(1.0, s))
    _draw_shape(shader, 'LINE_LOOP', outline, (*OUTLINE_COLOR[:3], OUTLINE_COLOR[3] * _alpha))
    gpu.state.line_width_set(1.0)

    blf.size(0, _text_size())
    # Center labels on the height of capitals, not on descenders.
    cap_h = blf.dimensions(0, "H")[1]
    for identifier, label, x, y, w, h in buttons:
        action = _action(_shown_bar, identifier)
        on = action is None or _available(action, state, context)
        if on and identifier == _hover:
            tris, _outline = _rounded_rect(x, y, w, h, (CORNER - BUTTON_GAP) * s)
            _draw_shape(shader, 'TRIS', tris, (*HOVER_COLOR[:3], HOVER_COLOR[3] * _alpha))
        color = ACCENT_COLOR if action and action.accent else TEXT_COLOR
        text_w = blf.dimensions(0, label)[0]
        blf.position(0, round(x + (w - text_w) / 2), round(y + (h - cap_h) / 2), 0)
        blf.color(0, *color, _alpha * (1.0 if on else 0.35))
        blf.draw(0, label)

    gpu.state.blend_set('NONE')


# -----------------------------------------------------------------------------
# Show/hide animation

def _redraw_views():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _fade_step():
    global _alpha, _fade_last_time
    now = time.perf_counter()
    step = (now - _fade_last_time) / FADE_TIME
    _fade_last_time = now
    _alpha = min(_target, _alpha + step) if _alpha < _target else max(_target, _alpha - step)
    _redraw_views()
    return None if _alpha == _target else 1.0 / 60.0


def _set_target(target):
    global _target, _fade_last_time
    if target == _target:
        return
    _target = target
    if not bpy.app.timers.is_registered(_fade_step):
        _fade_last_time = time.perf_counter()
        bpy.app.timers.register(_fade_step, first_interval=0.0)


def _visible(context):
    return _alpha > 0.5 and context.region is not None and context.region.type == 'WINDOW'


def _open_customize():
    global _customizing
    _customizing = _shown_bar or MASK
    bpy.ops.wm.call_panel(name=VIEW3D_PT_phantom_mask_bar.__name__, keep_open=True)


class _OpenOnRelease:
    """Mix-in: open the customize popover once the mouse button is released. Opened on the
    press, the release lands outside the popover and closes it straight away."""

    def _wait_for_release(self, context, event):
        self.button = event.type
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == self.button and event.value == 'RELEASE':
            _open_customize()
            return {'FINISHED'}
        if event.type == 'ESC':
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


# -----------------------------------------------------------------------------
# Operators

class VIEW3D_OT_phantom_mask_bar_hover(Operator):
    """Track the cursor to highlight the actions under it"""
    bl_idname = "view3d.phantom_mask_bar_hover"
    bl_label = "Mask Actions Hover"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _visible(context)

    def invoke(self, context, event):
        global _hover
        identifier, _on_bar = _button_at(
            context.area, context.region, event.mouse_region_x, event.mouse_region_y)
        if identifier != _hover:
            _hover = identifier
            context.region.tag_redraw()
        # Never consume the event: sculpting and the brush cursor still need it.
        return {'PASS_THROUGH'}


class VIEW3D_OT_phantom_mask_bar_click(_OpenOnRelease, Operator):
    """Run the action under the cursor"""
    bl_idname = "view3d.phantom_mask_bar_click"
    bl_label = "Mask Actions"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _visible(context)

    def invoke(self, context, event):
        identifier, on_bar = _button_at(
            context.area, context.region, event.mouse_region_x, event.mouse_region_y)
        if not on_bar:
            # Not over the bar: let the click reach the sculpt brush.
            return {'PASS_THROUGH'}
        bar, state = bar_for(context)
        action = _action(bar, identifier) if bar else None
        if identifier == CUSTOMIZE:
            _play_sound("click")
            return self._wait_for_release(context, event)
        if identifier == DISMISS:
            _play_sound("click")
            set_current_polygroup(context.active_object, None)
            return {'FINISHED'}
        if action is None or not _available(action, state, context):
            # On the bar but not on an available action: swallow the click, don't sculpt.
            return {'FINISHED'}

        _play_sound("click")
        module, name = action.operator.split(".")
        operator = getattr(getattr(bpy.ops, module), name)
        properties = action.properties
        if callable(properties):
            properties = properties(current_polygroup(context))
        try:
            # `True`: each action makes its own undo step, as when run from a menu.
            operator('INVOKE_DEFAULT' if action.invoke else 'EXEC_DEFAULT', True, **properties)
        except RuntimeError as ex:
            self.report({'WARNING'}, str(ex).replace("Error: ", "").strip())
        _redraw_views()
        return {'FINISHED'}


class VIEW3D_OT_phantom_mask_bar_customize(_OpenOnRelease, Operator):
    """Choose which actions the bar shows"""
    bl_idname = "view3d.phantom_mask_bar_customize"
    bl_label = "Customize Mask Actions"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _visible(context)

    def invoke(self, context, event):
        _identifier, on_bar = _button_at(
            context.area, context.region, event.mouse_region_x, event.mouse_region_y)
        if not on_bar:
            # Not over the bar: the usual right-click menu.
            return {'PASS_THROUGH'}
        return self._wait_for_release(context, event)


class VIEW3D_OT_phantom_mask_bar_reset(Operator):
    """Show the default actions on the bar"""
    bl_idname = "view3d.phantom_mask_bar_reset"
    bl_label = "Reset to Defaults"
    bl_options = {'INTERNAL'}

    bar: bpy.props.EnumProperty(items=((MASK, "Mask", ""), (POLYGROUP, "Polygroup", "")))

    def execute(self, context):
        _settings[SETTINGS_KEYS[self.bar]] = list(DEFAULTS[self.bar])
        _save_settings()
        _redraw_views()
        return {'FINISHED'}


class VIEW3D_PT_phantom_mask_bar(Panel):
    """A bar's choice of actions (shown as a popover)"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "Mask Actions Bar"
    bl_ui_units_x = 9

    def draw(self, context):
        layout = self.layout
        bar = _customizing
        layout.label(text=TITLES[bar])
        wm = context.window_manager
        group = None
        col = None
        for action in CATALOGS[bar]:
            if action.group != group:
                group = action.group
                layout.separator(factor=0.5)
                sub = layout.row()
                sub.enabled = False
                sub.label(text=group)
                col = layout.column(align=True)
            col.prop(wm, _prop_name(bar, action.identifier))
        layout.separator()
        layout.operator(VIEW3D_OT_phantom_mask_bar_reset.bl_idname, icon='LOOP_BACK').bar = bar


# -----------------------------------------------------------------------------
# Mask shapes

def _mask_shape_icon(icon):
    from bl_ui.space_toolsystem_common import ToolSelectPanelHelper
    return ToolSelectPanelHelper._icon_value_from_icon_handle(icon)


def masking_now(context):
    """Whether to show the Mask Shape buttons: with the Mask brush selected, or while Ctrl (the
    mask modifier) is held without Shift (Ctrl-Shift trims)."""
    import phantom_quick_brushes
    held = phantom_quick_brushes.held
    if held["ctrl"] and not held["shift"]:
        return True
    brush = context.tool_settings.sculpt.brush
    return brush is not None and brush.sculpt_brush_type == 'MASK'


def draw_mask_shape_buttons(layout, context):
    """The Mask Shape buttons: square icons choosing what Ctrl-drag draws off the mesh."""
    current = _settings["mask_shape"]
    row = layout.row(align=True)
    row.alignment = 'LEFT'
    row.label(text="Mask Shape")
    buttons = row.row(align=True)
    buttons.scale_x = 1.4
    buttons.scale_y = 1.4
    for identifier, _label, _description, icon in MASK_SHAPES:
        buttons.operator(SCULPT_OT_phantom_mask_shape_set.bl_idname, text="",
                         icon_value=_mask_shape_icon(icon),
                         depress=identifier == current).shape = identifier


class SCULPT_OT_phantom_mask_shape_set(Operator):
    """Choose what Ctrl-drag draws off the mesh"""
    bl_idname = "sculpt.phantom_mask_shape_set"
    bl_label = "Mask Shape"
    bl_options = {'INTERNAL'}

    shape: bpy.props.EnumProperty(items=[(i, l, d) for i, l, d, _icon in MASK_SHAPES])

    @classmethod
    def description(cls, _context, properties):
        return next(d for i, _l, d, _icon in MASK_SHAPES if i == properties.shape)

    def execute(self, context):
        context.window_manager.phantom_mask_shape = self.shape
        _play_sound("click")
        return {'FINISHED'}


def _get_mask_shape(_self):
    return [shape[0] for shape in MASK_SHAPES].index(_settings["mask_shape"])


def _set_mask_shape(_self, value):
    _settings["mask_shape"] = MASK_SHAPES[value][0]
    _save_settings()
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def _circle_points(center, radius, segments=64):
    return [(center.x + radius * math.cos(2.0 * math.pi * i / segments),
             center.y + radius * math.sin(2.0 * math.pi * i / segments)) for i in range(segments)]


def _circle_is_empty(context, center, radius):
    """Sample rays across the circle: True when none of them hits the sculpted object."""
    from bpy_extras import view3d_utils
    region, rv3d = context.region, context.region_data
    depsgraph = context.evaluated_depsgraph_get()
    target = context.active_object
    steps = max(2, min(15, int(radius * 2 / 8)))
    for yi in range(steps):
        for xi in range(steps):
            x = center.x - radius + 2.0 * radius * (xi + 0.5) / steps
            y = center.y - radius + 2.0 * radius * (yi + 0.5) / steps
            if (x - center.x) ** 2 + (y - center.y) ** 2 > radius * radius:
                continue
            origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (x, y))
            direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, (x, y))
            hit, _loc, _nor, _index, ob, _matrix = context.scene.ray_cast(depsgraph, origin, direction)
            if hit and ob is not None and ob.original == target:
                return False
    return True


class SCULPT_OT_phantom_mask_circle(Operator):
    """Mask inside a circle, dragged out from its center; a circle over empty space clears the
    mask"""
    bl_idname = "sculpt.phantom_mask_circle"
    bl_label = "Mask Circle"
    bl_options = {'INTERNAL'}

    erase: bpy.props.BoolProperty(name="Erase", description="Remove the mask instead of adding it",
                                  options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.mode == 'SCULPT' and context.region is not None and context.region.type == 'WINDOW'

    def invoke(self, context, event):
        from mathutils import Vector
        region = context.region
        # Started from a drag: the circle's center is where the button went down.
        self.center = Vector((event.mouse_prev_press_x - region.x, event.mouse_prev_press_y - region.y))
        self.radius = (Vector((event.mouse_region_x, event.mouse_region_y)) - self.center).length
        self.region = region
        self.handle = bpy.types.SpaceView3D.draw_handler_add(self._draw, (), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        region.tag_redraw()
        return {'RUNNING_MODAL'}

    def _draw(self):
        if bpy.context.region != self.region:
            return
        s = _ui_scale()
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        points = _circle_points(self.center, self.radius)
        fill = [self.center] + points + [points[0]]
        tris = []
        for i in range(1, len(fill) - 1):
            tris += (fill[0], fill[i], fill[i + 1])
        _draw_shape(shader, 'TRIS', tris, (1.0, 1.0, 1.0, 0.06) if self.erase else (0.0, 0.0, 0.0, 0.12))
        gpu.state.line_width_set(max(1.0, 2.0 * s))
        _draw_shape(shader, 'LINE_LOOP', points, (0.0, 0.0, 0.0, 0.6))
        gpu.state.line_width_set(max(1.0, s))
        _draw_shape(shader, 'LINE_LOOP', points, (1.0, 1.0, 1.0, 0.9))
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')

    def _end(self, context):
        bpy.types.SpaceView3D.draw_handler_remove(self.handle, 'WINDOW')
        self.region.tag_redraw()

    def modal(self, context, event):
        from mathutils import Vector
        if event.type == 'MOUSEMOVE':
            self.radius = (Vector((event.mouse_region_x, event.mouse_region_y)) - self.center).length
            self.region.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self._end(context)
            return self._apply(context)
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._end(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _apply(self, context):
        # Ignore stray clicks.
        if self.radius < 4.0 * _ui_scale():
            return {'CANCELLED'}
        if _circle_is_empty(context, self.center, self.radius):
            # Empty space: clear the mask, if there is one (as the lasso does).
            has_mask = bpy.types.WindowManager.phantom_sculpt_mask_state() & 1
            if self.erase or has_mask:
                bpy.ops.paint.mask_flood_fill('EXEC_DEFAULT', True, mode='VALUE', value=0.0)
            return {'FINISHED'}
        path = [{"name": "", "loc": point, "time": 0.0} for point in _circle_points(self.center, self.radius)]
        bpy.ops.paint.mask_lasso_gesture('EXEC_DEFAULT', path=path, mode='VALUE',
                                         value=0.0 if self.erase else 1.0)
        return {'FINISHED'}


def _keep_faces(mesh, keep):
    """Delete the faces of `mesh` not in `keep` (one bool per face) and show everything."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    remove = list(compress(bm.faces, (not k for k in keep)))
    bmesh.ops.delete(bm, geom=remove, context='FACES')
    for elems in (bm.verts, bm.edges, bm.faces):
        for elem in elems:
            elem.hide = False
    bm.to_mesh(mesh)
    bm.free()
    # The mask chose what was split; it has done its job.
    mask = mesh.attributes.get(".sculpt_mask")
    if mask is not None:
        mesh.attributes.remove(mask)
    mesh.update()


class SCULPT_OT_phantom_split_hidden(Operator):
    """Separate the hidden part of the mesh into its own object, like ZBrush's Split Hidden. """ \
        """The visible part stays selected"""
    bl_idname = "sculpt.phantom_split_hidden"
    bl_label = "Split Hidden"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        state = mask_state(context)
        return state is not None and state[1]

    def execute(self, context):
        import numpy as np
        ob = context.active_object
        if ob.use_dynamic_topology_sculpting:
            self.report({'WARNING'}, "Turn off Dyntopo to split hidden parts")
            return {'CANCELLED'}

        # A step before splitting, for undo to go back to (like Mask Extract): the sculpt undo
        # steps before it don't hold the whole scene.
        bpy.ops.ed.undo_push(message=self.bl_label)
        # Leave Sculpt Mode so its changes are in the mesh.
        bpy.ops.object.mode_set(mode='OBJECT')
        mesh = ob.data
        hidden = np.zeros(len(mesh.polygons), dtype=bool)
        attribute = mesh.attributes.get(".hide_poly")
        if attribute is not None:
            attribute.data.foreach_get("value", hidden)
        if not hidden.any() or hidden.all():
            bpy.ops.object.mode_set(mode='SCULPT')
            self.report({'WARNING'}, "Hide part of the mesh first" if not hidden.any() else
                        "Everything is hidden: nothing would be left")
            return {'CANCELLED'}

        split = ob.copy()
        split.data = mesh.copy()
        split.name = ob.name + " Split"
        split.data.name = mesh.name + " Split"
        for collection in ob.users_collection:
            collection.objects.link(split)

        _keep_faces(mesh, ~hidden)
        _keep_faces(split.data, hidden)
        split.select_set(False)
        ob.select_set(True)
        context.view_layer.objects.active = ob
        bpy.ops.object.mode_set(mode='SCULPT')

        _play_sound("confirm")
        self.report({'INFO'}, f"Split hidden part into \"{split.name}\"")
        return {'FINISHED'}


def _draw_sculpt_menu(self, context):
    self.layout.operator(SCULPT_OT_phantom_split_hidden.bl_idname)


def _prop_name(bar, identifier):
    prefix = "phantom_mask_bar_" if bar == MASK else "phantom_polygroup_bar_"
    return prefix + identifier.lower()


def _make_accessors(bar, identifier):
    def get(_self):
        return identifier in _settings[SETTINGS_KEYS[bar]]

    def set_(_self, value):
        set_action_shown(bar, identifier, value)

    return get, set_


def _register_properties():
    WM = bpy.types.WindowManager
    WM.phantom_mask_shape = bpy.props.EnumProperty(
        name="Mask Shape", description="What Ctrl-drag draws off the mesh",
        items=[(i, l, d, n) for n, (i, l, d, _icon) in enumerate(MASK_SHAPES)],
        get=_get_mask_shape, set=_set_mask_shape)
    for bar, actions in CATALOGS.items():
        for action in actions:
            get, set_ = _make_accessors(bar, action.identifier)
            setattr(WM, _prop_name(bar, action.identifier), bpy.props.BoolProperty(
                name=action.label, description=action.description, get=get, set=set_))


def _unregister_properties():
    del bpy.types.WindowManager.phantom_mask_shape
    for bar, actions in CATALOGS.items():
        for action in actions:
            delattr(bpy.types.WindowManager, _prop_name(bar, action.identifier))


classes = (
    SCULPT_OT_phantom_mask_shape_set,
    SCULPT_OT_phantom_mask_circle,
    VIEW3D_OT_phantom_mask_bar_hover,
    VIEW3D_OT_phantom_mask_bar_click,
    VIEW3D_OT_phantom_mask_bar_customize,
    VIEW3D_OT_phantom_mask_bar_reset,
    VIEW3D_PT_phantom_mask_bar,
    SCULPT_OT_phantom_split_hidden,
)


def register():
    global _draw_handle
    _load_settings()
    _register_properties()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_MT_sculpt.append(_draw_sculpt_menu)
    bpy.app.handlers.undo_post.append(_forget_state)
    bpy.app.handlers.redo_post.append(_forget_state)
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(draw_callback, (), 'WINDOW', 'POST_PIXEL')

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Sculpt", space_type='EMPTY')
        kmi = km.keymap_items.new(VIEW3D_OT_phantom_mask_bar_click.bl_idname,
                                  'LEFTMOUSE', 'PRESS', any=True)
        _addon_keymaps.append((km, kmi))
        kmi = km.keymap_items.new(VIEW3D_OT_phantom_mask_bar_customize.bl_idname,
                                  'RIGHTMOUSE', 'PRESS', any=True)
        _addon_keymaps.append((km, kmi))
        kmi = km.keymap_items.new(VIEW3D_OT_phantom_mask_bar_hover.bl_idname,
                                  'MOUSEMOVE', 'ANY', any=True)
        _addon_keymaps.append((km, kmi))


def unregister():
    global _draw_handle
    for km, kmi in _addon_keymaps:
        km.keymap_items.remove(kmi)
    _addon_keymaps.clear()
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    if bpy.app.timers.is_registered(_fade_step):
        bpy.app.timers.unregister(_fade_step)
    bpy.types.VIEW3D_MT_sculpt.remove(_draw_sculpt_menu)
    bpy.app.handlers.undo_post.remove(_forget_state)
    bpy.app.handlers.redo_post.remove(_forget_state)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    _unregister_properties()
