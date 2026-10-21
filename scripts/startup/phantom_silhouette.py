# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: floating silhouette thumbnail for Sculpt Mode.

Draws the scene from the current view with every object pure black on white,
in a small panel anchored to the top-right of the 3D Viewport. Drag the panel
to move it, drag its corner grip to resize it. The grip fades in while the
cursor is over the panel.

Drag it next to the tool-bar to dock it on the left: it then sits beside the
tool-bar (and the sidebar, when that is on the left), moving over when they
open. Docked in the top-left corner, the viewport's text info and statistics
move down below it.

Whether it is on, its size, position and docking are remembered between sessions
(stored in ``phantom_silhouette.json`` in the user config directory).
"""

import json
import os
import time

import bpy
import gpu
from bpy.props import BoolProperty, IntProperty
from bpy.types import Operator
from gpu_extras.batch import batch_for_shader
from gpu_extras.presets import draw_texture_2d

# Panel geometry is stored in unscaled pixels and multiplied by the UI scale.
DEFAULT_SETTINGS = {
    "enabled": False,
    "width": 260,
    "height": 200,
    # From the region's right and top edges: below the navigation gizmo,
    # clear of the zoom/pan buttons.
    "margin_x": 70,
    "margin_y": 150,
    # "LEFT" when docked next to the tool-bar, "" when floating.
    "dock": "",
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
_offscreen = None
_drawing = False
_addon_keymaps = []

# Hover feedback: the grip fades towards `_hover_target`.
_hover_target = 0.0
_grip_alpha = 0.0
_fade_last_time = 0.0


# -----------------------------------------------------------------------------
# Persistent settings

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
        if key == "enabled" and not value:
            _free_offscreen()
        _schedule_save()
        _redraw_views()

    return getter, setter


# -----------------------------------------------------------------------------
# Drawing

def _ui_scale():
    return bpy.context.preferences.system.ui_scale


def _enabled(context):
    return context.mode == 'SCULPT' and _settings["enabled"]


def _docked():
    return _settings["dock"] == "LEFT"


def _left_inset(area, region):
    """Pixels of the region covered by the tool-bar and a left sidebar (overlapping regions)."""
    space = area.spaces.active
    inset = 0
    for other in area.regions:
        if other.type not in {'TOOLS', 'UI'} or other.alignment != 'LEFT' or other.width <= 1:
            continue
        if other.type == 'TOOLS' and not space.show_region_toolbar:
            continue
        if other.type == 'UI' and not space.show_region_ui:
            continue
        inset = max(inset, other.x + other.width - region.x)
    return max(0, inset)


def panel_rect(area, region):
    """Return (x, y, width, height) of the panel in region pixel space (y up)."""
    s = _ui_scale()
    w = max(MIN_SIZE, round(_settings["width"] * s))
    h = max(MIN_SIZE, round(_settings["height"] * s))
    w = min(w, region.width - 2)
    h = min(h, region.height - 2)
    if _docked():
        x = _left_inset(area, region) + round(DOCK_GAP * s)
        y = region.height - round((DOCK_GAP + _settings["dock_top"]) * s) - h
    else:
        x = region.width - round(_settings["margin_x"] * s) - w
        y = region.height - round(_settings["margin_y"] * s) - h
    x = min(max(x, 1), region.width - w - 1)
    y = min(max(y, 1), region.height - h - 1)
    return x, y, w, h


def _info_offset_for(region, rect):
    """Room the viewport text needs to sit below a panel docked in the top-left corner."""
    if not (_docked() and _settings["dock_top"] == 0):
        return 0
    _x, y, _w, _h = rect
    return region.height - y + round(DOCK_GAP * _ui_scale())


def _set_info_offset(region, offset):
    if region.phantom_info_offset != offset:
        region.phantom_info_offset = offset
        # The text is drawn before this callback; redraw once more so it moves now.
        bpy.app.timers.register(lambda: _redraw_views() or None, first_interval=0.0)


def _projection_for_panel(rv3d, region, w, h):
    """Keep the viewport's vertical field of view, adapted to the panel's aspect ratio."""
    proj = rv3d.window_matrix.copy()
    fac = (region.width / region.height) / (w / h)
    proj[0] = proj[0] * fac
    return proj


def _ensure_offscreen(w, h):
    global _offscreen
    if _offscreen is None or _offscreen.width != w or _offscreen.height != h:
        if _offscreen is not None:
            _offscreen.free()
        _offscreen = gpu.types.GPUOffScreen(w, h)
    return _offscreen


def _free_offscreen():
    global _offscreen
    if _offscreen is not None:
        _offscreen.free()
        _offscreen = None


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
    global _drawing
    context = bpy.context
    # The off-screen render can call region callbacks again; never recurse.
    if _drawing:
        return
    region = context.region
    if region is None:
        return
    if not _enabled(context):
        _set_info_offset(region, 0)
        return
    rv3d = context.region_data
    space = context.space_data
    if rv3d is None or region.width < MIN_SIZE or region.height < MIN_SIZE:
        return

    x, y, w, h = panel_rect(context.area, region)
    _set_info_offset(region, _info_offset_for(region, (x, y, w, h)))
    # Creating an off-screen buffer unbinds the viewport's frame-buffer,
    # so remember it and draw the panel back into it explicitly.
    region_fb = gpu.state.active_framebuffer_get()
    ofs = _ensure_offscreen(w, h)

    _drawing = True
    try:
        ofs.draw_view3d(
            context.scene,
            context.view_layer,
            space,
            region,
            rv3d.view_matrix,
            _projection_for_panel(rv3d, region, w, h),
            silhouette=True,
        )
    finally:
        _drawing = False

    with region_fb.bind():
        draw_texture_2d(ofs.texture_color, (x, y), w, h)
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

    def _resize(self, dx, dy):
        s = _ui_scale()
        _x, _y, w, h = self.start_rect
        # Floating panels grow from the lower-left grip, docked ones from the lower-right.
        new_w = w + dx if _docked() else w - dx
        _settings["width"] = round(max(MIN_SIZE, new_w) / s)
        _settings["height"] = round(max(MIN_SIZE, h - dy) / s)

    def _move(self, context, dx, dy):
        s = _ui_scale()
        region = context.region
        x, y, w, h = self.start_rect
        x += dx
        y += dy
        if x <= _left_inset(context.area, region) + DOCK_SNAP * s:
            # Close to the tools: dock on the left, snapping into the top-left corner.
            dock_top = max(0, round((region.height - (y + h)) / s) - DOCK_GAP)
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
                self._resize(dx, dy)
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

    for timer in (_save_settings, _fade_step):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)

    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    _free_offscreen()

    bpy.types.VIEW3D_HT_header.remove(draw_header_toggle)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    WM = bpy.types.WindowManager
    for name in _PROPS:
        delattr(WM, name)
