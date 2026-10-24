# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: the sculpt history timeline.

With the sculpt history on (Properties > Sculpting > History), every sculpt edit of the mesh is
a step. In Sculpt Mode a timeline at the top of the 3D Viewport shows them: a tick per step
(taller and orange for keyframes, where the topology changed), and a handle at the step the mesh
is at. Click or drag on it to scrub (``sculpt.phantom_history_scrub``): the mesh follows, and
releasing makes it one undo step. Sculpting after going back drops the steps after it.
"""

import blf
import bpy
import gpu
from bpy.types import Operator
from gpu_extras.batch import batch_for_shader

# Unscaled pixels.
HEIGHT = 22
MARGIN_TOP = 10
MAX_WIDTH = 640
PAD_X = 14
TICK = 5
TEXT_SIZE = 11

BAR_COLOR = (0.105, 0.105, 0.125, 0.85)
TRACK_COLOR = (1.0, 1.0, 1.0, 0.18)
TICK_COLOR = (0.75, 0.75, 0.8, 0.55)
KEYFRAME_COLOR = (1.0, 0.62, 0.34, 0.9)    # Phantom orange.
HANDLE_COLOR = (0.55, 0.55, 1.0, 1.0)      # Phantom periwinkle.
HOVER_COLOR = (1.0, 1.0, 1.0, 0.5)
TEXT_COLOR = (0.92, 0.92, 0.95, 0.9)

_draw_handle = None
_addon_keymaps = []
_hover = None


def _ui_scale():
    return bpy.context.preferences.system.ui_scale


def history_for(context):
    """The sculpt history to show, or None."""
    ob = context.active_object
    if context.mode != 'SCULPT' or ob is None or ob.type != 'MESH':
        return None
    history = ob.sculpt_history
    if history is None or len(history.steps) < 2:
        return None
    return history


def _top_inset(area, region):
    """Height of the headers drawn over the viewport's top (with region overlap)."""
    top = region.y + region.height
    inset = 0
    for other in area.regions:
        if other.type not in {'HEADER', 'TOOL_HEADER'} or other.height <= 1 or other.width <= 1:
            continue
        if other.y < top and other.y + other.height > region.y:
            inset = max(inset, top - other.y)
    return inset


def layout(area, region):
    """The bar's rectangle and the x range of its track (region pixels)."""
    s = _ui_scale()
    w = min(MAX_WIDTH * s, region.width * 0.6)
    h = HEIGHT * s
    x = round((region.width - w) / 2)
    y = round(region.height - _top_inset(area, region) - MARGIN_TOP * s - h)
    return (x, y, w, h), (x + PAD_X * s, x + w - PAD_X * s)


def _step_x(track, index, count):
    x0, x1 = track
    return x0 + (x1 - x0) * index / max(count - 1, 1)


def _index_at(track, mx, count):
    x0, x1 = track
    t = min(max((mx - x0) / max(x1 - x0, 1.0), 0.0), 1.0)
    return round(t * (count - 1))


def _hit(rect, mx, my):
    x, y, w, h = rect
    return x <= mx <= x + w and y <= my <= y + h


def _draw(shader, kind, coords, color):
    batch = batch_for_shader(shader, kind, {"pos": coords})
    shader.uniform_float("color", color)
    batch.draw(shader)


def _rect(x, y, w, h):
    return [(x, y), (x + w, y), (x + w, y + h), (x, y), (x + w, y + h), (x, y + h)]


def draw_callback():
    context = bpy.context
    region = context.region
    if region is None or region.type != 'WINDOW':
        return
    history = history_for(context)
    if history is None:
        return

    s = _ui_scale()
    steps = history.steps
    count = len(steps)
    rect, track = layout(context.area, region)
    x, y, w, h = rect
    mid = y + h / 2
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')

    _draw(shader, 'TRIS', _rect(*rect), BAR_COLOR)
    _draw(shader, 'TRIS', _rect(track[0], mid - 1 * s, track[1] - track[0], 2 * s), TRACK_COLOR)

    # One tick per step, fewer when they would be under 3 pixels apart (keyframes always show).
    stride = max(1, int(3 * s * (count - 1) / max(track[1] - track[0], 1)) + 1)
    ticks, keyframes = [], []
    for i, step in enumerate(steps):
        if step.is_keyframe:
            sx = _step_x(track, i, count)
            keyframes += _rect(sx - 1 * s, mid - (TICK + 3) * s, 2 * s, 2 * (TICK + 3) * s)
        elif i % stride == 0:
            sx = _step_x(track, i, count)
            ticks += _rect(sx - 0.5 * s, mid - TICK * s, 1 * s, 2 * TICK * s)
    if ticks:
        _draw(shader, 'TRIS', ticks, TICK_COLOR)
    if keyframes:
        _draw(shader, 'TRIS', keyframes, KEYFRAME_COLOR)

    if _hover is not None and _hover < count:
        hx = _step_x(track, _hover, count)
        _draw(shader, 'TRIS', _rect(hx - 1 * s, y + 2 * s, 2 * s, h - 4 * s), HOVER_COLOR)

    cx = _step_x(track, history.current, count)
    _draw(shader, 'TRIS', _rect(cx - 2.5 * s, y + 2 * s, 5 * s, h - 4 * s), HANDLE_COLOR)

    # The step under the cursor (or the current one), below the bar.
    index = _hover if _hover is not None and _hover < count else history.current
    label = "{:d} / {:d}  {:s}".format(index + 1, count, steps[index].name)
    blf.size(0, TEXT_SIZE * s)
    tw, th = blf.dimensions(0, label)
    blf.position(0, round(x + (w - tw) / 2), round(y - th - 6 * s), 0)
    blf.color(0, *TEXT_COLOR)
    blf.draw(0, label)

    gpu.state.blend_set('NONE')


class VIEW3D_OT_phantom_history_timeline_click(Operator):
    """Scrub the sculpt history from the timeline"""
    bl_idname = "view3d.phantom_history_timeline_click"
    bl_label = "Sculpt History Timeline"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return (context.region is not None and context.region.type == 'WINDOW' and
                history_for(context) is not None)

    def invoke(self, context, event):
        rect, track = layout(context.area, context.region)
        if not _hit(rect, event.mouse_region_x, event.mouse_region_y):
            # Not on the timeline: let the click reach the sculpt brush.
            return {'PASS_THROUGH'}
        try:
            bpy.ops.sculpt.phantom_history_scrub('INVOKE_DEFAULT', x_min=track[0], x_max=track[1])
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
        return (context.region is not None and context.region.type == 'WINDOW' and
                history_for(context) is not None)

    def invoke(self, context, event):
        global _hover
        history = history_for(context)
        rect, track = layout(context.area, context.region)
        hover = None
        if _hit(rect, event.mouse_region_x, event.mouse_region_y):
            hover = _index_at(track, event.mouse_region_x, len(history.steps))
        if hover != _hover:
            _hover = hover
            context.region.tag_redraw()
        # Never consume the event: sculpting and the brush cursor still need it.
        return {'PASS_THROUGH'}


classes = (
    VIEW3D_OT_phantom_history_timeline_click,
    VIEW3D_OT_phantom_history_timeline_hover,
)


def register():
    global _draw_handle
    for cls in classes:
        bpy.utils.register_class(cls)
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
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
