# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: viewport extras.

- Grid: infinite (Blender's floor) or limited, a square grid around the origin like Maya's,
  chosen in the Overlays popover. The limited grid replaces the floor and its X/Y axis lines in
  perspective views (its center lines can keep the axis colors); side views keep Blender's
  orthographic grid.
- Vertex hover: in Edit Mode with vertex selection, the vertex under the cursor grows a little
  and pulses.
- Theme: brings an existing Phantom theme up to the current edit-mode colors, once, when it
  still has the colors of an earlier Phantom default.

Settings are stored in ``phantom_viewport.json`` in the user config directory.
"""

import json
import math
import os
import time

import bmesh
import bpy
import gpu
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, IntProperty
from bpy.types import Operator
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader

DEFAULT_SETTINGS = {
    "grid": 'INFINITE',
    # Half the width of the limited grid, in grid cells (Maya's default grid is 24 wide).
    "grid_size": 12,
    # Draw the limited grid's center lines in the X/Y axis colors instead of a darker gray.
    "grid_axis_colors": False,
    # Theme migrations already done (see `_migrate_theme`).
    "theme_version": 0,
}
ENFORCE_INTERVAL = 1.0

_settings = dict(DEFAULT_SETTINGS)
_draw_handle = None
_hover_draw_handle = None
_batches = {}
_addon_keymaps = []


def _settings_path():
    return os.path.join(bpy.utils.user_resource('CONFIG'), "phantom_viewport.json")


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
        print("Phantom: could not save viewport settings:", ex)


# -----------------------------------------------------------------------------
# Limited grid

def _limited():
    return _settings["grid"] == 'LIMITED'


def _view3d_spaces():
    for screen in bpy.data.screens:
        for area in screen.areas:
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    yield space


def _apply_grid_mode(force=False):
    """The limited grid stands in for the floor and its X/Y axes; the infinite grid is the floor.
    Without `force`, only views showing the floor are changed (new views, opened files)."""
    limited = _limited()
    for space in _view3d_spaces():
        overlay = space.overlay
        if limited and (force or overlay.show_floor):
            overlay.show_floor = False
            overlay.show_axis_x = False
            overlay.show_axis_y = False
        elif not limited and force:
            overlay.show_floor = True
            overlay.show_axis_x = True
            overlay.show_axis_y = True


def _grid_batches(shader, size, spacing):
    key = (size, spacing)
    if key not in _batches:
        _batches.clear()
        extent = size * spacing
        lines = []
        for i in range(-size, size + 1):
            if i != 0:
                t = i * spacing
                lines += [(t, -extent, 0.0), (t, extent, 0.0), (-extent, t, 0.0), (extent, t, 0.0)]
        # The center lines: along X, then along Y.
        x_axis = [(-extent, 0.0, 0.0), (extent, 0.0, 0.0)]
        y_axis = [(0.0, -extent, 0.0), (0.0, extent, 0.0)]
        _batches[key] = (batch_for_shader(shader, 'LINES', {"pos": lines}),
                         batch_for_shader(shader, 'LINES', {"pos": x_axis}),
                         batch_for_shader(shader, 'LINES', {"pos": y_axis}))
    return _batches[key]


def _draw_grid():
    if not _limited():
        return
    context = bpy.context
    space = context.space_data
    region = context.region
    r3d = context.region_data
    if space is None or r3d is None or not space.overlay.show_overlays:
        return
    if r3d.view_perspective == 'ORTHO' and r3d.is_orthographic_side_view:
        return  # Side views keep Blender's orthographic grid.

    spacing = space.overlay.grid_scale
    unit_scale = context.scene.unit_settings.scale_length if context.scene else 1.0
    if unit_scale > 0.0:
        spacing /= unit_scale
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    lines, x_axis, y_axis = _grid_batches(shader, _settings["grid_size"], spacing)
    # The theme's floor color, a little stronger: this grid doesn't fade into the distance.
    theme_grid = context.preferences.themes[0].view_3d.grid
    grid = (theme_grid[0], theme_grid[1], theme_grid[2], min(1.0, theme_grid[3] * 1.8))
    scale = context.preferences.system.ui_scale

    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.depth_mask_set(False)
    shader.bind()
    shader.uniform_float("viewportSize", (region.width, region.height))
    shader.uniform_float("lineWidth", 1.0 * scale)
    shader.uniform_float("color", grid)
    lines.draw(shader)
    # The center lines are heavier: darker like Maya's, or in the axis colors.
    shader.uniform_float("lineWidth", 2.0 * scale)
    if _settings["grid_axis_colors"]:
        # As thin as the floor's axis lines.
        shader.uniform_float("lineWidth", 1.5 * scale)
        theme = context.preferences.themes[0]
        shader.uniform_float("color", _floor_axis_color(theme, theme.user_interface.axis_x))
        x_axis.draw(shader)
        shader.uniform_float("color", _floor_axis_color(theme, theme.user_interface.axis_y))
        y_axis.draw(shader)
    else:
        shader.uniform_float("color", (grid[0] * 0.45, grid[1] * 0.45, grid[2] * 0.45,
                                       min(1.0, grid[3] * 1.5)))
        x_axis.draw(shader)
        y_axis.draw(shader)
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


def _floor_axis_color(theme, axis):
    """The axis color of Blender's infinite floor (see the overlay engine's `grid_axis_x`):
    85% axis color over the grid color, shifted by the theme's axis brightness."""
    grid = theme.view_3d.grid
    offset = theme.view_3d.grid_axis_brightness * 2.0 - 1.0
    rgb = [min(1.0, max(0.0, 0.15 * grid[i] + 0.85 * axis[i] + offset)) for i in range(3)]
    return (*rgb, 0.15 * grid[3] + 0.85)


def _grid_get(_self):
    return 1 if _limited() else 0


def _grid_set(_self, value):
    mode = 'LIMITED' if value == 1 else 'INFINITE'
    if mode != _settings["grid"]:
        _settings["grid"] = mode
        _save_settings()
        _apply_grid_mode(force=True)


def _grid_size_get(_self):
    return _settings["grid_size"]


def _grid_size_set(_self, value):
    _settings["grid_size"] = value
    _save_settings()


def _grid_axis_colors_get(_self):
    return _settings["grid_axis_colors"]


def _grid_axis_colors_set(_self, value):
    _settings["grid_axis_colors"] = value
    _save_settings()


def grid_limited():
    return _limited()


def draw_grid_mode(layout, context):
    """The Infinite/Limited switch (drawn by the Overlays popover's Guides panel)."""
    layout.row(align=True).prop(context.window_manager, "phantom_grid", expand=True)


def draw_grid_options(layout, context):
    """Options of the limited grid (drawn by the Overlays popover's Guides panel)."""
    if _limited():
        wm = context.window_manager
        layout.prop(wm, "phantom_grid_size")
        layout.prop(wm, "phantom_grid_axis_colors")


@persistent
def _on_load(_):
    _apply_grid_mode()


def _enforce():
    # New 3D views (split areas, new windows) start with the floor on.
    if _limited():
        _apply_grid_mode()
    return ENFORCE_INTERVAL


# -----------------------------------------------------------------------------
# Vertex hover

PULSE_PERIOD = 0.9         # Seconds.
PULSE_FPS = 30

_hover = {"object": None, "vert": -1}
_pulse_start = [0.0]


def _hover_clear():
    if _hover["vert"] != -1:
        _hover["vert"] = -1
        _redraw_views()


def _redraw_views():
    wm = bpy.context.window_manager
    for window in wm.windows if wm else ():
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _update_hover(context, mx, my):
    """The vertex a click here would select (Blender's own search: pick distance, occlusion)."""
    ob = context.edit_object
    if (ob is None or ob.type != 'MESH' or not context.tool_settings.mesh_select_mode[0] or
            context.region_data is None):
        _hover_clear()
        return
    vert = bpy.types.WindowManager.phantom_edit_vertex_at(mouse_x=int(mx), mouse_y=int(my))
    if vert != _hover["vert"] or ob.name != _hover["object"]:
        _hover["object"], _hover["vert"] = ob.name, vert
        if vert != -1:
            _pulse_start[0] = time.perf_counter()
            if not bpy.app.timers.is_registered(_pulse):
                bpy.app.timers.register(_pulse, first_interval=1.0 / PULSE_FPS)
        _redraw_views()


def _pulse():
    if _hover["vert"] == -1:
        return None
    _redraw_views()
    return 1.0 / PULSE_FPS


class VIEW3D_OT_phantom_vertex_hover(Operator):
    """Track the vertex under the cursor for the hover highlight"""
    bl_idname = "view3d.phantom_vertex_hover"
    bl_label = "Vertex Hover"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH' and context.area and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        if context.region and context.region.type == 'WINDOW':
            _update_hover(context, event.mouse_region_x, event.mouse_region_y)
        return {'PASS_THROUGH'}


def _draw_hover():
    if _hover["vert"] == -1:
        return
    context = bpy.context
    ob = context.edit_object
    if ob is None or ob.name != _hover["object"] or context.mode != 'EDIT_MESH':
        return
    bm = bmesh.from_edit_mesh(ob.data)
    if _hover["vert"] >= len(bm.verts):
        return
    bm.verts.ensure_lookup_table()
    vert = bm.verts[_hover["vert"]]
    if vert.hide:
        return
    co = view3d_utils.location_3d_to_region_2d(context.region, context.region_data,
                                               ob.matrix_world @ vert.co)
    if co is None:
        return
    theme = context.preferences.themes[0].view_3d
    phase = (time.perf_counter() - _pulse_start[0]) / PULSE_PERIOD
    pulse = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)   # 0 -> 1 -> 0, starting small.
    base = theme.vertex_size * context.preferences.system.ui_scale
    color = tuple(theme.vertex_select) if vert.select else tuple(theme.vertex)

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    shader.bind()
    # A soft glow in the selection color, breathing with the pulse.
    shader.uniform_float("color", (*theme.vertex_select, 0.18 + 0.14 * pulse))
    _disc(shader, co, 0.5 * base * (2.4 + 0.6 * pulse))
    # The vertex itself, a little bigger.
    shader.uniform_float("color", (*color[:3], 1.0))
    _disc(shader, co, 0.5 * base * (1.45 + 0.2 * pulse))
    gpu.state.blend_set('NONE')


def _disc(shader, center, radius, segments=24):
    points = [center]
    for i in range(segments + 1):
        angle = 2.0 * math.pi * i / segments
        points.append((center.x + radius * math.cos(angle), center.y + radius * math.sin(angle)))
    batch_for_shader(shader, 'TRI_FAN', {"pos": points}).draw(shader)


# -----------------------------------------------------------------------------
# Theme migration

def _close(color, hex6):
    target = [int(hex6[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    return all(abs(a - b) < 0.02 for a, b in zip(color, target))


def _set(color_prop_owner, name, hex8):
    rgba = [int(hex8[i:i + 2], 16) / 255.0 for i in (0, 2, 4, 6)]
    value = getattr(color_prop_owner, name)
    setattr(color_prop_owner, name, rgba[:len(value)])


def _migrate_theme():
    """Version 1: Halloween edit-mode colors, if the theme still has the earlier Phantom ones."""
    if _settings["theme_version"] >= 1:
        return
    prefs = bpy.context.preferences
    theme = prefs.themes[0]
    view = theme.view_3d
    if _close(view.vertex_select, "ff8a3d"):
        for name, hex8 in (
                ("vertex_select", "ff6a00ff"), ("edge_select", "ff7f1fff"),
                ("edge_mode_select", "ff9a3dff"), ("face_select", "ff6a0038"),
                ("face_mode_select", "ff7f1f38"), ("editmesh_active", "b77dff66"),
                ("vertex", "1a0f24ff"), ("edge", "1a0f24ff"), ("wire_edit", "1a0f24ff"),
                ("seam", "7dff3aff"), ("sharp", "b77dffff")):
            _set(view, name, hex8)
        image = theme.image_editor
        for name, hex8 in (("vertex_select", "ff6a00ff"), ("edge_select", "ff7f1fff"),
                           ("face_select", "ff6a0038"), ("editmesh_active", "b77dff66")):
            _set(image, name, hex8)
        prefs.is_dirty = True
    _settings["theme_version"] = 1
    _save_settings()


def _startup():
    _migrate_theme()
    _apply_grid_mode()
    return None


def register():
    global _draw_handle, _hover_draw_handle
    _load_settings()
    bpy.utils.register_class(VIEW3D_OT_phantom_vertex_hover)
    WM = bpy.types.WindowManager
    WM.phantom_grid = EnumProperty(
        name="Grid",
        items=(('INFINITE', "Infinite", "Blender's floor grid, reaching the horizon"),
               ('LIMITED', "Limited", "A square grid around the origin, like Maya's")),
        get=_grid_get, set=_grid_set,
    )
    WM.phantom_grid_size = IntProperty(
        name="Grid Size", description="Grid cells from the center to each edge of the limited grid",
        min=1, soft_max=100, get=_grid_size_get, set=_grid_size_set,
    )
    WM.phantom_grid_axis_colors = BoolProperty(
        name="Axis Colors", description="Draw the limited grid's center lines in the X and Y axis colors",
        get=_grid_axis_colors_get, set=_grid_axis_colors_set,
    )
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_grid, (), 'WINDOW', 'POST_VIEW')
    _hover_draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_hover, (), 'WINDOW', 'POST_PIXEL')
    bpy.app.handlers.load_post.append(_on_load)
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Mesh", space_type='EMPTY')
        kmi = km.keymap_items.new(VIEW3D_OT_phantom_vertex_hover.bl_idname, 'MOUSEMOVE', 'ANY', any=True)
        _addon_keymaps.append((km, kmi))
    if not bpy.app.background:
        bpy.app.timers.register(_startup, first_interval=0.5)
        bpy.app.timers.register(_enforce, first_interval=ENFORCE_INTERVAL, persistent=True)


def unregister():
    global _draw_handle, _hover_draw_handle
    for km, kmi in _addon_keymaps:
        km.keymap_items.remove(kmi)
    _addon_keymaps.clear()
    for timer in (_startup, _enforce, _pulse):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    bpy.app.handlers.load_post.remove(_on_load)
    for handle in (_draw_handle, _hover_draw_handle):
        if handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
    _draw_handle = _hover_draw_handle = None
    bpy.utils.unregister_class(VIEW3D_OT_phantom_vertex_hover)
    del bpy.types.WindowManager.phantom_grid_axis_colors
    del bpy.types.WindowManager.phantom_grid_size
    del bpy.types.WindowManager.phantom_grid
