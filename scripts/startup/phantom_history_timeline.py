# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: the sculpt history timeline.

With Sculpt History on (a preference), every sculpt edit of a mesh is a step, from the first
stroke on each object. In Sculpt Mode a timeline bar shows them: a tick per step (taller and
orange for keyframes, where the topology changed), "Start" and "End" at its ends, and a handle at
the step the mesh is at. Click or drag it to scrub (``sculpt.phantom_history_scrub``): the mesh
follows, and releasing makes it one undo step. Sculpting after going back drops the steps after
it.

The Sculpt Mode header has History: its checkbox shows or hides the bar, and its dropdown holds
every history setting: where the bar is docked (top, bottom, left or right of the viewport),
showing it, recording, the step limit and saving in files (for every object), and deleting the
active object's history. Undo and redo move along the timeline too. Starting to sculpt from an
older step deletes the steps after it; when that is more than 25 steps, a warning in the middle of
the viewport asks first. It can stop asking until Phantom is closed or for good (Ask Before
Sculpting on Older Steps). Masking is part of the history too. The bar keeps
clear of the headers, toolbar and sidebar, gliding along as they open and close. Its own settings
are remembered between sessions (``phantom_history_bar.json`` in the user config).
"""

import json
import math
import os
import time

import blf
import bpy
import gpu
from bpy.types import Operator, Panel
from gpu_extras.batch import batch_for_shader

# Unscaled pixels.
THICKNESS = 22
MARGIN = 10
MAX_LENGTH = 640
PAD = 14
TICK = 5
TEXT_SIZE = 11
# Room left at the bottom for the mask actions bar (its height and margin).
MASK_BAR_ROOM = 42
# Room left at the right for the navigation gizmos.
GIZMO_ROOM = 70
# How quickly the bar glides to its place when panels open or close (seconds, time constant).
EASE_TIME = 0.06

BAR_COLOR = (0.105, 0.105, 0.125, 0.85)
TRACK_COLOR = (1.0, 1.0, 1.0, 0.18)
TICK_COLOR = (0.75, 0.75, 0.8, 0.55)
KEYFRAME_COLOR = (1.0, 0.62, 0.34, 0.9)    # Phantom orange.
HANDLE_COLOR = (0.55, 0.55, 1.0, 1.0)      # Phantom periwinkle.
HOVER_COLOR = (1.0, 1.0, 1.0, 0.5)
TEXT_COLOR = (0.92, 0.92, 0.95, 0.9)
DIM_TEXT_COLOR = (0.92, 0.92, 0.95, 0.5)

DOCKS = (
    ('TOP', "Top", "Dock the timeline at the top of the viewport", 'TRIA_UP'),
    ('BOTTOM', "Bottom", "Dock the timeline at the bottom of the viewport", 'TRIA_DOWN'),
    ('LEFT', "Left", "Dock the timeline along the left side of the viewport", 'TRIA_LEFT'),
    ('RIGHT', "Right", "Dock the timeline along the right side of the viewport", 'TRIA_RIGHT'),
)

ASK_MODES = (
    ('ALWAYS', "Always", "Ask before a stroke on an older step deletes the later steps"),
    ('SESSION', "Not This Session",
     "Sculpt on older steps without asking until Phantom is closed (the later steps are deleted)"),
    ('NEVER', "Never", "Sculpt on older steps without asking (the later steps are deleted)"),
)

# "ask_older": whether to ask before sculpting on an older step (False: never, remembered).
_settings = {"show": True, "dock": 'TOP', "ask_older": True}
# Asking was turned off until Phantom is closed.
_ask_off_this_session = False
_draw_handle = None
_addon_keymaps = []
# The step index under the cursor, or None.
_hover = None
# Eased insets, by (area pointer, side): [value, last update time, target].
_eased = {}


# -----------------------------------------------------------------------------
# Persistent settings

def _settings_path():
    return os.path.join(bpy.utils.user_resource('CONFIG'), "phantom_history_bar.json")


def _load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    if isinstance(data.get("show"), bool):
        _settings["show"] = data["show"]
    if data.get("dock") in {dock[0] for dock in DOCKS}:
        _settings["dock"] = data["dock"]
    if isinstance(data.get("ask_older"), bool):
        _settings["ask_older"] = data["ask_older"]


def _save_settings():
    path = _settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_settings, fh, indent=2)
    except OSError as ex:
        print("Phantom: could not save history timeline settings:", ex)


def _redraw_views():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _get_show(_self):
    return _settings["show"]


def _set_show(_self, value):
    _settings["show"] = value
    _save_settings()
    _redraw_views()


def _get_dock(_self):
    return [dock[0] for dock in DOCKS].index(_settings["dock"])


def _set_dock(_self, value):
    _settings["dock"] = DOCKS[value][0]
    _save_settings()
    _redraw_views()


def _asks_older():
    return _settings["ask_older"] and not _ask_off_this_session


def _get_ask(_self):
    if not _settings["ask_older"]:
        return 2
    return 1 if _ask_off_this_session else 0


def _set_ask(_self, value):
    global _ask_off_this_session
    mode = ASK_MODES[value][0]
    _ask_off_this_session = mode == 'SESSION'
    if _settings["ask_older"] != (mode != 'NEVER'):
        _settings["ask_older"] = mode != 'NEVER'
        _save_settings()


# -----------------------------------------------------------------------------
# State and layout

def _ui_scale():
    return bpy.context.preferences.system.ui_scale


def timeline_object(context):
    """The object whose history the timeline shows (Sculpt Mode on a mesh), or None."""
    ob = context.active_object
    if context.mode != 'SCULPT' or ob is None or ob.type != 'MESH':
        return None
    return ob


def _visible(context):
    return (_settings["show"] and context.region is not None and
            context.region.type == 'WINDOW' and timeline_object(context) is not None)


def _side_insets(area, region):
    """How far the toolbar and sidebar reach in from the left and right of the viewport (either
    can be on either side: Phantom docks the sidebar next to the tools)."""
    left = right = 0
    middle = region.x + region.width / 2
    for other in area.regions:
        if other.type not in {'TOOLS', 'UI'} or other.width <= 1 or other.height <= 1:
            continue
        if not (other.x < region.x + region.width and other.x + other.width > region.x):
            continue
        if other.x + other.width / 2 < middle:
            left = max(left, other.x + other.width - region.x)
        else:
            right = max(right, region.x + region.width - other.x)
    return left, right


def _overlap(area, region, types, side):
    """How far regions of `types` drawn over the viewport reach in from its top or bottom."""
    inset = 0
    for other in area.regions:
        if other.type not in types or other.width <= 1 or other.height <= 1:
            continue
        overlaps_x = other.x < region.x + region.width and other.x + other.width > region.x
        overlaps_y = other.y < region.y + region.height and other.y + other.height > region.y
        if not (overlaps_x and overlaps_y):
            continue
        if side == 'TOP':
            inset = max(inset, region.y + region.height - other.y)
        else:
            inset = max(inset, other.y + other.height - region.y)
    return inset


def _ease_tick():
    """Redraws while any inset is still gliding to its target."""
    _redraw_views()
    moving = any(entry[0] != entry[2] for entry in _eased.values())
    return 1.0 / 60.0 if moving else None


def _ease(area, side, target, advance):
    """`target`, reached smoothly: panels open and close at once as far as their size goes (only
    their drawing is animated), which would make the bar jump. Only drawing advances the value,
    so clicks and hover use exactly what is on screen."""
    key = (area.as_pointer(), side)
    now = time.perf_counter()
    entry = _eased.get(key)
    if entry is None:
        _eased[key] = [target, now, target]
        return target
    value, last, _old_target = entry
    entry[2] = target
    if advance:
        if value != target:
            # Frames can be far apart when nothing was moving: step at most one frame's worth.
            dt = min(now - last, 1.0 / 60.0)
            value += (target - value) * (1.0 - math.exp(-dt / EASE_TIME))
            if abs(target - value) < 0.5:
                value = target
            entry[0] = value
            if value != target and not bpy.app.timers.is_registered(_ease_tick):
                bpy.app.timers.register(_ease_tick, first_interval=1.0 / 60.0)
        entry[1] = now
    return entry[0]


class Layout:
    """Where everything goes for the current dock, in region pixels."""
    __slots__ = ("vertical", "bar", "track", "start_label", "end_label", "status")

    def __init__(self, area, region, advance=False):
        s = _ui_scale()
        dock = _settings["dock"]
        self.vertical = dock in {'LEFT', 'RIGHT'}
        t = THICKNESS * s
        m = MARGIN * s
        W, H = region.width, region.height
        left, right = _side_insets(area, region)
        top = _ease(area, 'TOP', _overlap(area, region, {'HEADER', 'TOOL_HEADER'}, 'TOP'), advance)
        bottom = _ease(area, 'BOTTOM', _overlap(
            area, region, {'ASSET_SHELF', 'ASSET_SHELF_HEADER'}, 'BOTTOM'), advance)
        left = _ease(area, 'LEFT', left, advance)
        right = _ease(area, 'RIGHT', right, advance)

        if self.vertical:
            # Shorter than the viewport's height, to stay clear of the text info at the top left.
            length = max(min(MAX_LENGTH * s * 0.6, (H - top - bottom) * 0.45), 80 * s)
            y = bottom + (H - top - bottom - length) / 2
            x = left + m if dock == 'LEFT' else W - right - GIZMO_ROOM * s - m - t
            self.bar = (x, y, t, length)
            # Oldest at the top, like reading down a list.
            self.track = (y + length - PAD * s, y + PAD * s)
            cx = x + t / 2
            self.start_label = (cx, y + length + 4 * s, 'CENTER', 'BOTTOM')
            self.end_label = (cx, y - 4 * s, 'CENTER', 'TOP')
            if dock == 'LEFT':
                self.status = (x + t + 8 * s, y + length / 2, 'LEFT', 'MIDDLE')
            else:
                self.status = (x - 8 * s, y + length / 2, 'RIGHT', 'MIDDLE')
        else:
            length = max(min(MAX_LENGTH * s, (W - left - right) * 0.6), 120 * s)
            x = left + (W - left - right - length) / 2
            if dock == 'TOP':
                y = H - top - m - t
            else:
                y = bottom + MASK_BAR_ROOM * s + m
            self.bar = (x, y, length, t)
            self.track = (x + PAD * s, x + length - PAD * s)
            cy = y + t / 2
            self.start_label = (x - 6 * s, cy, 'RIGHT', 'MIDDLE')
            self.end_label = (x + length + 6 * s, cy, 'LEFT', 'MIDDLE')
            if dock == 'TOP':
                self.status = (x + length / 2, y - 6 * s, 'CENTER', 'TOP')
            else:
                self.status = (x + length / 2, y + t + 6 * s, 'CENTER', 'BOTTOM')

    def step_pos(self, index, count):
        a, b = self.track
        return a + (b - a) * index / max(count - 1, 1)

    def index_at(self, mx, my, count):
        a, b = self.track
        pos = my if self.vertical else mx
        t = min(max((pos - a) / (b - a), 0.0), 1.0) if b != a else 0.0
        return round(t * (count - 1))

    def mid(self):
        x, y, w, h = self.bar
        return x + w / 2 if self.vertical else y + h / 2


def _hit(rect, mx, my):
    x, y, w, h = rect
    return x <= mx <= x + w and y <= my <= y + h


# -----------------------------------------------------------------------------
# Drawing

def _draw(shader, kind, coords, color):
    # Drawing text resets blending, so set it for every shape.
    gpu.state.blend_set('ALPHA')
    batch = batch_for_shader(shader, kind, {"pos": coords})
    shader.uniform_float("color", color)
    batch.draw(shader)


def _rect(x, y, w, h):
    return [(x, y), (x + w, y), (x + w, y + h), (x, y), (x + w, y + h), (x, y + h)]


def _across(lay, pos, half_width, half_length):
    """A rectangle across the track at `pos` along it."""
    m = lay.mid()
    if lay.vertical:
        return _rect(m - half_length, pos - half_width, 2 * half_length, 2 * half_width)
    return _rect(pos - half_width, m - half_length, 2 * half_width, 2 * half_length)


def _text(text, x, y, align_x, align_y, color):
    w, _h = blf.dimensions(0, text)
    cap = blf.dimensions(0, "H")[1]
    x -= {'LEFT': 0, 'CENTER': w / 2, 'RIGHT': w}[align_x]
    y -= {'BOTTOM': 0, 'MIDDLE': cap / 2, 'TOP': cap}[align_y]
    blf.position(0, round(x), round(y), 0)
    blf.color(0, *color)
    blf.draw(0, text)


def _status_text(context, history, index):
    if not context.preferences.edit.use_sculpt_history:
        return "Sculpt History off", DIM_TEXT_COLOR
    if history is None or len(history.steps) == 0:
        return "No history yet: sculpt to start", DIM_TEXT_COLOR
    steps = history.steps
    return "{:d} / {:d}  {:s}".format(index + 1, len(steps), steps[index].name), TEXT_COLOR


# -----------------------------------------------------------------------------
# Warning: sculpting on an older step

# Sculpting on an older step deletes the steps after it: ask first only when it deletes more than
# this many (fewer go without asking).
ASK_ABOVE_STEPS = 25
WARNING_FADE = 0.14
WARNING_PAD = 14
LINE_GAP = 6
BUTTON_W = 110
BUTTON_H = 22
WARNING_BORDER_COLOR = (1.0, 1.0, 1.0, 0.14)
CONTINUE_COLOR = (1.0, 0.62, 0.34, 0.30)
CONTINUE_HOVER_COLOR = (1.0, 0.62, 0.34, 0.50)
CANCEL_COLOR = (1.0, 1.0, 1.0, 0.08)
CANCEL_HOVER_COLOR = (1.0, 1.0, 1.0, 0.16)

# A stroke on an older step is held back, and the warning fades in at the middle of the viewport
# asking what to do: Keep Sculpting (the later steps are deleted and strokes go ahead), Yes, Don't
# Ask Again (the same, without asking again this session) or Cancel. `t` moves toward `target`;
# `later` is how many steps would go; `key` is the history state it asked about (it goes away
# when that changes); `buttons` are where its buttons were last drawn.
_warning = {"t": 0.0, "target": 0.0, "last": 0.0, "later": 0, "key": None, "buttons": []}
# The history state the user agreed to sculpt from.
_confirmed_key = None


def _history_key(ob, history):
    return (ob.name, history.current, len(history.steps)) if history is not None else None


def _warning_tick():
    w = _warning
    now = time.perf_counter()
    step = (now - w["last"]) / WARNING_FADE
    w["last"] = now
    if w["t"] < w["target"]:
        w["t"] = min(w["target"], w["t"] + step)
    else:
        w["t"] = max(w["target"], w["t"] - step)
    _redraw_views()
    return None if w["t"] == w["target"] else 1.0 / 60.0


def _set_warning(open_):
    w = _warning
    w["target"] = 1.0 if open_ else 0.0
    if not open_:
        w["key"] = None
        w["buttons"] = []
    if not bpy.app.timers.is_registered(_warning_tick):
        w["last"] = time.perf_counter()
        bpy.app.timers.register(_warning_tick, first_interval=0.0)


def later_steps(history):
    return len(history.steps) - 1 - history.current


def ask_older_step(ob, history):
    """Hold back a stroke on an older step: show the warning and its question."""
    _warning["later"] = later_steps(history)
    _warning["key"] = _history_key(ob, history)
    _set_warning(True)


def _stroke_on_older_step(context, event, ob, history):
    """A brush or mask stroke about to start on an older step, not agreed to yet."""
    if (not context.preferences.edit.use_sculpt_history or history is None or
            history.current >= len(history.steps) - 1):
        return False
    if _confirmed_key == _history_key(ob, history):
        return False
    # Ctrl+Shift picks polygroups, which the history doesn't keep.
    if event.ctrl and event.shift:
        return False
    # Only brush and mask strokes: tools like Transpose make their own steps.
    tool = context.workspace.tools.from_space_view3d_mode(context.mode, create=False)
    return tool is not None and tool.idname == "builtin.brush"


def _faded(color, t):
    return (*color[:3], color[3] * t)


def _draw_warning(shader, region, s):
    """The warning box, fading in (`t`) at the middle of the viewport."""
    t = _warning["t"]
    _warning["buttons"] = []
    if t <= 0.0:
        return
    later = str(_warning["later"])
    lines = [
        [("The current active Undo History is not the latest one.", TEXT_COLOR)],
        [("Storing this action to the Undo History will remove ", TEXT_COLOR),
         (later, KEYFRAME_COLOR),
         (" Undo History entries.", TEXT_COLOR)],
        [("Press 'Keep Sculpting' to continue or 'Cancel' to abort.", TEXT_COLOR)],
    ]
    cap = blf.dimensions(0, "H")[1]
    pad = WARNING_PAD * s
    gap = LINE_GAP * s
    line_h = cap + gap
    bh_ = BUTTON_H * s
    buttons = [("CONTINUE", "Keep Sculpting", CONTINUE_COLOR, CONTINUE_HOVER_COLOR),
               ("CONTINUE_SESSION", "Yes, Don't Ask Again", CONTINUE_COLOR,
                CONTINUE_HOVER_COLOR),
               ("CANCEL", "Cancel", CANCEL_COLOR, CANCEL_HOVER_COLOR)]
    widths = [max(BUTTON_W * s, blf.dimensions(0, label)[0] + pad)
              for _id, label, _c, _h in buttons]
    buttons_w = sum(widths) + gap * (len(buttons) - 1)
    text_w = max(sum(blf.dimensions(0, text)[0] for text, _c in line) for line in lines)
    w = max(text_w, buttons_w) + 2 * pad
    # Lines with a blank line between them, like the paragraphs of a message.
    h = pad + len(lines) * line_h + (len(lines) - 1) * line_h + gap + bh_ + pad
    # It grows a little as it fades in.
    grow = 0.94 + 0.06 * t
    cx, cy = region.width / 2, region.height / 2
    x0, y0 = cx - w * grow / 2, cy - h * grow / 2
    b = 1 * s
    _draw(shader, 'TRIS', _rect(x0 - b, y0 - b, w * grow + 2 * b, h * grow + 2 * b),
          _faded(WARNING_BORDER_COLOR, t))
    _draw(shader, 'TRIS', _rect(x0, y0, w * grow, h * grow), _faded((*BAR_COLOR[:3], 0.96), t))
    if t < 1.0:
        return
    y = y0 + h - pad - cap / 2
    for line in lines:
        x = x0 + pad
        for text, color in line:
            _text(text, x, y, 'LEFT', 'MIDDLE', color)
            x += blf.dimensions(0, text)[0]
        y -= 2 * line_h
    x = x0 + w - pad - buttons_w
    for (identifier, label, color, hover_color), bw_ in zip(buttons, widths):
        brect = (x, y0 + pad, bw_, bh_)
        _draw(shader, 'TRIS', _rect(*brect), hover_color if _hover == identifier else color)
        _text(label, brect[0] + bw_ / 2, brect[1] + bh_ / 2, 'CENTER', 'MIDDLE', TEXT_COLOR)
        _warning["buttons"].append((identifier, brect))
        x += bw_ + gap


def draw_callback():
    context = bpy.context
    if not _visible(context):
        return
    ob = timeline_object(context)
    history = ob.sculpt_history
    steps = history.steps if history is not None else []
    count = len(steps)

    s = _ui_scale()
    lay = Layout(context.area, context.region, advance=True)
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    blf.size(0, TEXT_SIZE * s)

    # The question is about one history state: once that changes (undo, scrub...), it's moot.
    if _warning["key"] is not None and _warning["key"] != _history_key(ob, history):
        _set_warning(False)
    _draw(shader, 'TRIS', _rect(*lay.bar), BAR_COLOR)
    a, b = lay.track
    _draw(shader, 'TRIS', _across(lay, (a + b) / 2, abs(b - a) / 2, 1 * s), TRACK_COLOR)

    if count:
        # One tick per step, fewer when they'd be under 3 pixels apart (keyframes always show).
        stride = max(1, int(3 * s * (count - 1) / max(abs(b - a), 1)) + 1)
        ticks, keyframes = [], []
        for i, step in enumerate(steps):
            if step.is_keyframe:
                keyframes += _across(lay, lay.step_pos(i, count), 1 * s, (TICK + 3) * s)
            elif i % stride == 0:
                ticks += _across(lay, lay.step_pos(i, count), 0.5 * s, TICK * s)
        if ticks:
            _draw(shader, 'TRIS', ticks, TICK_COLOR)
        if keyframes:
            _draw(shader, 'TRIS', keyframes, KEYFRAME_COLOR)
        if isinstance(_hover, int) and _hover < count:
            _draw(shader, 'TRIS', _across(lay, lay.step_pos(_hover, count), 1 * s,
                                          (THICKNESS / 2 - 2) * s), HOVER_COLOR)
        _draw(shader, 'TRIS', _across(lay, lay.step_pos(history.current, count), 2.5 * s,
                                      (THICKNESS / 2 - 2) * s), HANDLE_COLOR)

    _text("Start", *lay.start_label, DIM_TEXT_COLOR)
    _text("End", *lay.end_label, DIM_TEXT_COLOR)
    index = _hover if isinstance(_hover, int) and _hover < count else (
        history.current if count else 0)
    text, color = _status_text(context, history, index)
    _text(text, *lay.status, color)

    # The warning last, over everything.
    _draw_warning(shader, context.region, s)
    gpu.state.blend_set('NONE')


# -----------------------------------------------------------------------------
# Operators and the header dropdown

class VIEW3D_OT_phantom_history_timeline_click(Operator):
    """Scrub the sculpt history from the timeline"""
    bl_idname = "view3d.phantom_history_timeline_click"
    bl_label = "Sculpt History Timeline"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _visible(context)

    def invoke(self, context, event):
        global _confirmed_key, _ask_off_this_session
        lay = Layout(context.area, context.region)
        mx, my = event.mouse_region_x, event.mouse_region_y
        # The warning's answer buttons.
        if _warning["t"] >= 1.0:
            for identifier, rect in _warning["buttons"]:
                if _hit(rect, mx, my):
                    if identifier in {"CONTINUE", "CONTINUE_SESSION"}:
                        # Agreed: the later steps (and the redo steps into them) go right away.
                        bpy.ops.sculpt.phantom_history_truncate()
                        _confirmed_key = None
                    if identifier == "CONTINUE_SESSION":
                        _ask_off_this_session = True
                    _set_warning(False)
                    return {'FINISHED'}
        if not _hit(lay.bar, mx, my):
            ob = timeline_object(context)
            history = ob.sculpt_history
            if _stroke_on_older_step(context, event, ob, history):
                if not _asks_older() or later_steps(history) <= ASK_ABOVE_STEPS:
                    # Not asking (turned off, or only a few steps would go): the later steps go
                    # and the stroke starts from here.
                    bpy.ops.sculpt.phantom_history_truncate()
                    return {'PASS_THROUGH'}
                # Hold the stroke back until the user agrees to delete the later steps.
                ask_older_step(ob, history)
                return {'FINISHED'}
            # Let the click reach the sculpt brush.
            return {'PASS_THROUGH'}
        history = timeline_object(context).sculpt_history
        if history is None or len(history.steps) < 2:
            # On the bar, nothing to scrub: just don't sculpt.
            return {'FINISHED'}
        try:
            bpy.ops.sculpt.phantom_history_scrub(
                'INVOKE_DEFAULT', start=lay.track[0], end=lay.track[1], vertical=lay.vertical)
        except RuntimeError as ex:
            self.report({'WARNING'}, str(ex).replace("Error: ", "").strip())
        return {'FINISHED'}


class VIEW3D_OT_phantom_history_timeline_hover(Operator):
    """Track the cursor over the timeline to show the step under it"""
    bl_idname = "view3d.phantom_history_timeline_hover"
    bl_label = "Sculpt History Timeline Hover"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _visible(context)

    def invoke(self, context, event):
        global _hover
        lay = Layout(context.area, context.region)
        mx, my = event.mouse_region_x, event.mouse_region_y
        history = timeline_object(context).sculpt_history
        hover = None
        for identifier, rect in _warning["buttons"]:
            if _hit(rect, mx, my):
                hover = identifier
        if hover is None and _hit(lay.bar, mx, my) and history is not None and len(history.steps):
            hover = lay.index_at(mx, my, len(history.steps))
        if hover != _hover:
            _hover = hover
            context.region.tag_redraw()
        # Never consume the event: sculpting and the brush cursor still need it.
        return {'PASS_THROUGH'}


class VIEW3D_PT_phantom_history(Panel):
    """Sculpt history settings (the History dropdown in the Sculpt Mode header)"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "Sculpt History"
    bl_ui_units_x = 15

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        edit = context.preferences.edit
        wm = context.window_manager

        layout.label(text="Sculpt History (experimental)", icon='TIME')
        # The dock buttons get the full width (a label column would cut their names).
        row = layout.row(align=True)
        row.use_property_split = False
        row.active = wm.phantom_history_bar_show
        row.prop(wm, "phantom_history_bar_dock", expand=True)
        row = layout.row()
        row.use_property_split = False
        row.column().prop(wm, "phantom_history_bar_show", text="Show Timeline")
        row.column().prop(edit, "use_sculpt_history", text="Record")
        # Both apply to every object.
        row = layout.row()
        row.use_property_split = False
        row.column().prop(edit, "sculpt_history_limit", text="Limit")
        row.column().prop(edit, "use_sculpt_history_save", text="Save in File")
        # Sculpting on an older step deletes the later steps: ask first?
        col = layout.column(align=True)
        col.use_property_split = False
        col.label(text="Ask Before Sculpting on Older Steps")
        col.row(align=True).prop(wm, "phantom_history_ask", expand=True)

        layout.separator()
        ob = context.active_object
        history = ob.sculpt_history if ob is not None and ob.type == 'MESH' else None
        steps = len(history.steps) if history is not None else 0
        sub = layout.row()
        sub.active = False
        name = ob.name if ob is not None else "No object"
        if steps:
            sub.label(text="{:s}: step {:d} of {:d}, {:.1f} MB".format(
                name, history.current + 1, steps, history.memory_size), icon='OBJECT_DATA')
        else:
            sub.label(text="{:s}: no history yet".format(name), icon='OBJECT_DATA')
        row = layout.row()
        row.alert = True
        row.enabled = history is not None
        row.operator("object.phantom_history_delete", text="Delete History", icon='TRASH')


def draw_header_item(layout, context):
    """History in the Sculpt Mode header: a checkbox to show the timeline, and the dropdown."""
    row = layout.row(align=True)
    row.prop(context.window_manager, "phantom_history_bar_show", text="")
    row.popover(panel=VIEW3D_PT_phantom_history.__name__, text="History")


def _draw_view_menu(self, context):
    if context.mode == 'SCULPT':
        self.layout.separator()
        self.layout.prop(context.window_manager, "phantom_history_bar_show",
                         text="History Timeline")


classes = (
    VIEW3D_OT_phantom_history_timeline_click,
    VIEW3D_OT_phantom_history_timeline_hover,
    VIEW3D_PT_phantom_history,
)


def register():
    global _draw_handle
    _load_settings()
    WM = bpy.types.WindowManager
    WM.phantom_history_bar_show = bpy.props.BoolProperty(
        name="Show Timeline",
        description="Show the sculpt history timeline in the 3D Viewport in Sculpt Mode",
        get=_get_show, set=_set_show)
    WM.phantom_history_bar_dock = bpy.props.EnumProperty(
        name="Dock", description="Where the sculpt history timeline sits in the viewport",
        items=[(i, l, d, icon, n) for n, (i, l, d, icon) in enumerate(DOCKS)],
        get=_get_dock, set=_set_dock)
    WM.phantom_history_ask = bpy.props.EnumProperty(
        name="Ask Before Sculpting on Older Steps",
        description="Whether a stroke on an older step of the sculpt history asks before it "
        "deletes the later steps",
        items=[(i, l, d, n) for n, (i, l, d) in enumerate(ASK_MODES)],
        get=_get_ask, set=_set_ask)
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_MT_view.append(_draw_view_menu)
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(draw_callback, (), 'WINDOW', 'POST_PIXEL')

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Sculpt", space_type='EMPTY')
        kmi = km.keymap_items.new(VIEW3D_OT_phantom_history_timeline_click.bl_idname,
                                  'LEFTMOUSE', 'PRESS', any=True)
        _addon_keymaps.append((km, kmi))
        kmi = km.keymap_items.new(VIEW3D_OT_phantom_history_timeline_hover.bl_idname,
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
    if bpy.app.timers.is_registered(_ease_tick):
        bpy.app.timers.unregister(_ease_tick)
    bpy.types.VIEW3D_MT_view.remove(_draw_view_menu)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.phantom_history_bar_show
    del bpy.types.WindowManager.phantom_history_bar_dock
    del bpy.types.WindowManager.phantom_history_ask
