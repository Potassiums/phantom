# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: floating silhouette thumbnail for Sculpt Mode.

Shows the 3D Viewport with everything drawn in it pure black on white (read from
the viewport's depth, so it is live and costs no extra render), in a small panel with the
viewport's own aspect ratio. New users start with it on, docked in the top-left corner. Drag
the panel to move it, drag its corner grip to resize it. The grip fades in while the cursor is
over the panel.

Drag it next to the tool-bar to dock it on the left: it then sits beside the
tool-bar (and the sidebar, when that is on the left), moving over when they
open. The viewport's text info and statistics stay visible: below the panel when
it is docked in the top-left corner, beside it when docked lower over the text.

Whether it is on, its size, position and docking are remembered between sessions
(stored in ``phantom_silhouette.json`` in the user config directory).
"""

import json
import math
import os
import time

import bpy
import gpu
from bpy.props import BoolProperty, IntProperty
from bpy.types import Operator
from gpu_extras.batch import batch_for_shader

# Panel geometry is stored in unscaled pixels and multiplied by the UI scale.
DEFAULT_SETTINGS = {
    "enabled": True,
    "width": 260,
    # Unused: the height follows the viewport's aspect ratio. Kept so old settings files load.
    "height": 200,
    # From the region's right and top edges: below the navigation gizmo,
    # clear of the zoom/pan buttons.
    "margin_x": 70,
    "margin_y": 150,
    # "LEFT" when docked next to the tool-bar, "" when floating.
    "dock": "LEFT",
    # Docked: distance from the top (0 = top-left corner, statistics go below it).
    "dock_top": 0,
}
MIN_SIZE = 80
GRIP_SIZE = 14
GRIP_FADE_TIME = 0.15
SAVE_DELAY = 0.5
# Unscaled pixels: how close to the tools the panel must be dragged to dock,
# how close to the top to snap into the corner, and the gap around a docked panel.
DOCK_SNAP = 40
DOCK_TOP_SNAP = 24
DOCK_GAP = 8

BORDER_COLOR = (0.55, 0.55, 1.0)   # Phantom periwinkle.
GRIP_COLOR = (1.0, 0.54, 0.24)     # Phantom orange.

_settings = dict(DEFAULT_SETTINGS)
_draw_handle = None
_shader = None
_addon_keymaps = []

# Hover feedback: the grip fades towards `_hover_target`.
_hover_target = 0.0
_grip_alpha = 0.0
_fade_last_time = 0.0


# -----------------------------------------------------------------------------
# Persistent settings

def _always_enabled():
    """Whether Phantom should remember brushes (the preference added for this feature)."""
    prefs = getattr(bpy.context, "preferences", None)
    return bool(getattr(prefs, "show_silhouette_always", False))

def _settings_path():
    return os.path.join(bpy.utils.user_resource('CONFIG'), "phantom_silhouette.json")


def _load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    for key, default in DEFAULT_SETTINGS.items():
        if isinstance(data.get(key), type(default)):
            _settings[key] = data[key]


def _save_settings():
    path = _settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_settings, fh, indent=2)
    except OSError as ex:
        print("Phantom: could not save silhouette thumbnail settings:", ex)
    return None


def _schedule_save():
    """Save shortly after the last change, so dragging doesn't write on every mouse move."""
    if bpy.app.timers.is_registered(_save_settings):
        bpy.app.timers.unregister(_save_settings)
    bpy.app.timers.register(_save_settings, first_interval=SAVE_DELAY, persistent=True)


def _redraw_views():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _make_accessors(key):
    def getter(_self):
        return _settings[key]

    def setter(_self, value):
        if _settings[key] == value:
            return
        _settings[key] = value
        _schedule_save()
        _redraw_views()

    return getter, setter


# -----------------------------------------------------------------------------
# Drawing

def _ui_scale():
    return bpy.context.preferences.system.ui_scale


def _enabled(context):
    if _always_enabled():
        _settings["enabled"]
    else:
        return context.mode == 'SCULPT' and _settings["enabled"]


def _docked():
    return _settings["dock"] == "LEFT"


def _left_inset(area, region, return_sliding=False):
    """Pixels of the region covered by the tool-bar and a left sidebar (overlapping regions).

    A region sliding in or out (e.g. the sidebar after pressing N) counts with its visible
    width, so a docked panel moves along with it."""
    space = area.spaces.active
    inset = 0
    sliding = False
    for other in area.regions:
        if other.type not in {'TOOLS', 'UI'} or other.alignment != 'LEFT' or other.width <= 1:
            continue
        if other.type == 'TOOLS' and not space.show_region_toolbar:
            continue
        if other.type == 'UI' and not space.show_region_ui:
            continue
        slide = other.phantom_slide
        sliding = sliding or slide < 1.0
        inset = max(inset, other.x + round(other.width * slide) - region.x)
    inset = max(0, inset)
    return (inset, sliding) if return_sliding else inset


def _top_inset(area, region):
    """Pixels at the top of the region covered by headers drawn over it (region overlap)."""
    region_top = region.y + region.height
    inset = 0
    for other in area.regions:
        if other.type in {'HEADER', 'TOOL_HEADER'} and other.alignment == 'TOP' and other.height > 1:
            inset = max(inset, region_top - other.y)
    return max(0, inset)


# Docked panels slide (instead of jumping) when the tool-bar or sidebar opens or closes.
INSET_SLIDE_TIME = 0.06  # Seconds, time constant of the ease.
_inset_state = {}  # Area pointer -> [shown inset, last update time].


def _slide_redraw():
    _redraw_views()
    return None


def _animated_inset(area, region):
    target, sliding = _left_inset(area, region, return_sliding=True)
    now = time.perf_counter()
    state = _inset_state.get(area.as_pointer())
    if state is None or sliding:
        # A sliding region already animates: follow it exactly.
        _inset_state[area.as_pointer()] = [float(target), now]
        return target
    value, last = state
    value += (target - value) * (1.0 - math.exp(-min(now - last, 0.1) / INSET_SLIDE_TIME))
    if abs(target - value) < 0.5:
        value = float(target)
    elif not bpy.app.timers.is_registered(_slide_redraw):
        bpy.app.timers.register(_slide_redraw, first_interval=1.0 / 60.0)
    state[0], state[1] = value, now
    return round(value)


def _view_aspect(region):
    return region.width / max(region.height, 1)


def panel_rect(area, region):
    """Return (x, y, width, height) of the panel in region pixel space (y up).

    The height follows the viewport's aspect ratio, so the silhouette fills the panel.
    """
    s = _ui_scale()
    aspect = _view_aspect(region)
    w = max(MIN_SIZE, round(_settings["width"] * s))
    w = min(w, region.width - 2, round((region.height - 2) * aspect))
    h = max(1, round(w / aspect))
    if _docked():
        x = _animated_inset(area, region) + round(DOCK_GAP * s)
        y = (region.height - _top_inset(area, region) -
             round((DOCK_GAP + _settings["dock_top"]) * s) - h)
    else:
        x = region.width - round(_settings["margin_x"] * s) - w
        y = region.height - round(_settings["margin_y"] * s) - h
    x = min(max(x, 1), region.width - w - 1)
    y = min(max(y, 1), region.height - h - 1)
    return x, y, w, h


# Docked lower than this fraction of the viewport height, the panel is clear of the text.
TEXT_AREA_FRACTION = 0.45


def _info_offsets_for(area, region, rect):
    """(x, y) the viewport text info must move by to stay visible next to a docked panel."""
    if not _docked():
        return 0, 0
    x, y, w, h = rect
    gap = round(DOCK_GAP * _ui_scale())
    if _settings["dock_top"] == 0:
        # In the top-left corner: the text goes below the panel. The text already starts below
        # the headers drawn over the viewport, so measure from there.
        return 0, region.height - _top_inset(area, region) - y + gap
    if y + h > region.height * (1.0 - TEXT_AREA_FRACTION):
        # Docked lower down but over the text: the text goes to the right of the panel.
        return x + w + gap - _left_inset(area, region), 0
    return 0, 0


def _set_info_offsets(region, offsets):
    offset_x, offset_y = offsets
    if region.phantom_info_offset_x != offset_x or region.phantom_info_offset != offset_y:
        region.phantom_info_offset_x = offset_x
        region.phantom_info_offset = offset_y
        # The text is drawn before this callback; redraw once more so it moves now.
        bpy.app.timers.register(lambda: _redraw_views() or None, first_interval=0.0)


def _silhouette_shader():
    """Black where the viewport drew geometry (depth < 1), white elsewhere."""
    global _shader
    if _shader is None:
        iface = gpu.types.GPUStageInterfaceInfo("phantom_silhouette_iface")
        iface.smooth('VEC2', "uv")
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', "ModelViewProjectionMatrix")
        # Where the viewport image sits inside the panel: origin xy, size zw (panel UVs).
        info.push_constant('VEC4', "image_rect")
        info.sampler(0, 'FLOAT_2D', "depth_tx")
        info.vertex_in(0, 'VEC2', "pos")
        info.vertex_in(1, 'VEC2', "texco")
        info.vertex_out(iface)
        info.fragment_out(0, 'VEC4', "fragColor")
        info.vertex_source(
            "void main() {"
            "  uv = texco;"
            "  gl_Position = ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0);"
            "}")
        info.fragment_source(
            "void main() {"
            "  vec2 t = (uv - image_rect.xy) / image_rect.zw;"
            "  if (any(lessThan(t, vec2(0.0))) || any(greaterThan(t, vec2(1.0)))) {"
            "    fragColor = vec4(1.0);"
            "    return;"
            "  }"
            "  float depth = texture(depth_tx, t).r;"
            "  fragColor = (depth < 1.0) ? vec4(0.0, 0.0, 0.0, 1.0) : vec4(1.0);"
            "}")
        _shader = gpu.shader.create_from_info(info)
    return _shader


def _image_rect(region, w, h):
    """Fit the whole viewport inside the panel without stretching (white margins)."""
    view_aspect = region.width / region.height
    panel_aspect = w / h
    if view_aspect > panel_aspect:
        size_y = panel_aspect / view_aspect
        return (0.0, (1.0 - size_y) / 2.0, 1.0, size_y)
    size_x = view_aspect / panel_aspect
    return ((1.0 - size_x) / 2.0, 0.0, size_x, 1.0)


def _draw_silhouette(depth, region, x, y, w, h):
    shader = _silhouette_shader()
    batch = batch_for_shader(shader, 'TRI_FAN', {
        "pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h)),
        "texco": ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
    })
    shader.bind()
    shader.uniform_float(
        "ModelViewProjectionMatrix",
        gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
    shader.uniform_float("image_rect", _image_rect(region, w, h))
    shader.uniform_sampler("depth_tx", depth)
    batch.draw(shader)


def _grip_triangle(x, y, w, g):
    """The resize grip: lower-left when floating (anchored top-right), lower-right when docked."""
    if _docked():
        return ((x + w, y), (x + w - g, y), (x + w, y + g))
    return ((x, y), (x + g, y), (x, y + g))


def _draw_frame(x, y, w, h, grip_alpha):
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(max(1.0, _ui_scale()))

    # The border is always faintly visible and brightens with the grip.
    border = batch_for_shader(
        shader, 'LINE_LOOP', {"pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h))})
    shader.uniform_float("color", (*BORDER_COLOR, 0.35 + 0.65 * grip_alpha))
    border.draw(shader)

    if grip_alpha > 0.001:
        g = GRIP_SIZE * _ui_scale()
        grip = batch_for_shader(shader, 'TRIS', {"pos": _grip_triangle(x, y, w, g)})
        shader.uniform_float("color", (*GRIP_COLOR, grip_alpha))
        grip.draw(shader)

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')


def draw_callback():
    context = bpy.context
    region = context.region
    if region is None:
        return
    if not _enabled(context):
        _set_info_offsets(region, (0, 0))
        return
    if region.width < MIN_SIZE or region.height < MIN_SIZE:
        return

    x, y, w, h = panel_rect(context.area, region)
    _set_info_offsets(region, _info_offsets_for(context.area, region, (x, y, w, h)))

    # The silhouette is read from the depth of the viewport just drawn, so it always matches
    # the view exactly, live while sculpting, with no second render.
    depth = gpu.state.phantom_viewport_depth_get()
    if depth is None:
        return
    _draw_silhouette(depth, region, x, y, w, h)
    _draw_frame(x, y, w, h, _grip_alpha)


# -----------------------------------------------------------------------------
# Hover fade

def _fade_step():
    """Timer: move the grip alpha towards its target, redrawing until it gets there."""
    global _grip_alpha, _fade_last_time
    now = time.perf_counter()
    step = (now - _fade_last_time) / GRIP_FADE_TIME
    _fade_last_time = now
    if _grip_alpha < _hover_target:
        _grip_alpha = min(_hover_target, _grip_alpha + step)
    else:
        _grip_alpha = max(_hover_target, _grip_alpha - step)
    _redraw_views()
    return None if _grip_alpha == _hover_target else 1.0 / 60.0


def _set_hover(hovering):
    global _hover_target, _fade_last_time
    target = 1.0 if hovering else 0.0
    if target == _hover_target:
        return
    _hover_target = target
    if not bpy.app.timers.is_registered(_fade_step):
        _fade_last_time = time.perf_counter()
        bpy.app.timers.register(_fade_step, first_interval=0.0)


class VIEW3D_OT_phantom_silhouette_hover(Operator):
    """Track the cursor so the thumbnail's resize grip can fade in on hover"""
    bl_idname = "view3d.phantom_silhouette_hover"
    bl_label = "Silhouette Thumbnail Hover"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _enabled(context) and context.region and context.region.type == 'WINDOW'

    def invoke(self, context, event):
        x, y, w, h = panel_rect(context.area, context.region)
        mx, my = event.mouse_region_x, event.mouse_region_y
        _set_hover(x <= mx <= x + w and y <= my <= y + h)
        # Never consume the event: sculpting and the brush cursor still need it.
        return {'PASS_THROUGH'}


# -----------------------------------------------------------------------------
# Move / resize

class VIEW3D_OT_phantom_silhouette_drag(Operator):
    """Move or resize the silhouette thumbnail"""
    bl_idname = "view3d.phantom_silhouette_drag"
    bl_label = "Move/Resize Silhouette Thumbnail"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _enabled(context) and context.region and context.region.type == 'WINDOW'

    def invoke(self, context, event):
        x, y, w, h = panel_rect(context.area, context.region)
        mx, my = event.mouse_region_x, event.mouse_region_y
        if not (x <= mx <= x + w and y <= my <= y + h):
            # Not over the thumbnail: let the click reach the sculpt brush.
            return {'PASS_THROUGH'}

        grip = GRIP_SIZE * _ui_scale() * 1.5
        on_grip_x = (x + w - mx) <= grip if _docked() else (mx - x) <= grip
        self.resizing = on_grip_x and (my - y) <= grip
        self.start_mouse = (mx, my)
        self.start_rect = (x, y, w, h)
        self.start = dict(_settings)
        _set_hover(True)
        context.window.cursor_modal_set('SCROLL_XY' if self.resizing else 'HAND')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _resize(self, context, dx, dy):
        s = _ui_scale()
        _x, _y, w, h = self.start_rect
        # Floating panels grow from the lower-left grip, docked ones from the lower-right.
        # The aspect ratio is locked, so follow whichever direction the grip moved further.
        from_x = w + dx if _docked() else w - dx
        from_y = (h - dy) * _view_aspect(context.region)
        new_w = from_x if abs(from_x - w) >= abs(from_y - w) else from_y
        _settings["width"] = round(max(MIN_SIZE, new_w) / s)

    def _move(self, context, dx, dy):
        s = _ui_scale()
        region = context.region
        x, y, w, h = self.start_rect
        x += dx
        y += dy
        if x <= _left_inset(context.area, region) + DOCK_SNAP * s:
            # Close to the tools: dock on the left, snapping into the top-left corner.
            top = region.height - _top_inset(context.area, region)
            dock_top = max(0, round((top - (y + h)) / s) - DOCK_GAP)
            _settings["dock"] = "LEFT"
            _settings["dock_top"] = 0 if dock_top < DOCK_TOP_SNAP else dock_top
        else:
            _settings["dock"] = ""
            _settings["margin_x"] = round((region.width - (x + w)) / s)
            _settings["margin_y"] = round((region.height - (y + h)) / s)

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            dx = event.mouse_region_x - self.start_mouse[0]
            dy = event.mouse_region_y - self.start_mouse[1]
            if self.resizing:
                self._resize(context, dx, dy)
            else:
                self._move(context, dx, dy)
            _schedule_save()
            _redraw_views()
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            context.window.cursor_modal_restore()
            return {'FINISHED'}

        if event.type in {'RIGHTMOUSE', 'ESC'}:
            _settings.update(self.start)
            _schedule_save()
            _redraw_views()
            context.window.cursor_modal_restore()
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}


def draw_header_toggle(self, context):
    if not _always_enabled():
        if context.mode != 'SCULPT':
            return

    wm = context.window_manager
    self.layout.prop(
        wm, "phantom_silhouette", text="",
        icon='GHOST_ENABLED' if wm.phantom_silhouette else 'GHOST_DISABLED')


classes = (
    VIEW3D_OT_phantom_silhouette_hover,
    VIEW3D_OT_phantom_silhouette_drag,
)

_PROPS = {
    "phantom_silhouette": "enabled",
    "phantom_silhouette_width": "width",
    "phantom_silhouette_height": "height",
    "phantom_silhouette_margin_x": "margin_x",
    "phantom_silhouette_margin_y": "margin_y",
}


def register():
    global _draw_handle
    _load_settings()

    WM = bpy.types.WindowManager
    get, set_ = _make_accessors("enabled")
    WM.phantom_silhouette = BoolProperty(
        name="Silhouette Thumbnail",
        description="Show a floating black-on-white silhouette of the scene while sculpting",
        get=get, set=set_,
    )
    for prop_name, key in _PROPS.items():
        if key != "enabled":
            get, set_ = _make_accessors(key)
            setattr(WM, prop_name, IntProperty(get=get, set=set_))

    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.VIEW3D_HT_header.append(draw_header_toggle)
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(draw_callback, (), 'WINDOW', 'POST_PIXEL')

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Sculpt", space_type='EMPTY')
        kmi = km.keymap_items.new(VIEW3D_OT_phantom_silhouette_drag.bl_idname,
                                  'LEFTMOUSE', 'PRESS', any=True)
        _addon_keymaps.append((km, kmi))
        kmi = km.keymap_items.new(VIEW3D_OT_phantom_silhouette_hover.bl_idname,
                                  'MOUSEMOVE', 'ANY', any=True)
        _addon_keymaps.append((km, kmi))


def unregister():
    global _draw_handle
    for km, kmi in _addon_keymaps:
        km.keymap_items.remove(kmi)
    _addon_keymaps.clear()

    for timer in (_save_settings, _fade_step, _slide_redraw):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)

    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None

    bpy.types.VIEW3D_HT_header.remove(draw_header_toggle)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    WM = bpy.types.WindowManager
    for name in _PROPS:
        delattr(WM, name)
