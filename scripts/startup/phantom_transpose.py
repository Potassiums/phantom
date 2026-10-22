# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: Move, Rotate and Scale in Sculpt Mode, with a gizmo or with a ZBrush-style Transpose
action line.

W (and the icons in the middle of the tool header) picks Move, Rotate or Scale; two more icons
choose between Blender's gizmo tools and Transpose. Both deform the unmasked part of the mesh.

Transpose (after ZBrush's): click-drag on the model to draw an action line; its endpoints snap to
the surface under them. Each end and the middle have an outer ring and an inner circle:

- Drag an outer ring to place that end (or, for the middle ring or the line itself, the whole
  line) in the plane of the screen.
- Drag an inner circle to act on the model:
  - Move: the middle moves it (Shift: along the line); an end stretches/shears it relative to the
    other end.
  - Scale: an end scales it about the other end; the middle, dragged towards an end, thickens or
    thins it across the line.
  - Rotate: an end rotates it about the other end; the middle, dragged along the line, twists it
    around the line.

The deformation itself is done by ``sculpt.phantom_transform`` (Blender's sculpt transform with a
matrix), so masks, symmetry and undo work as with the gizmo tools.
"""

import json
import math
import os

import bpy
import gpu
from bpy.props import EnumProperty
from bpy.types import Operator, WorkSpaceTool
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

TRANSPOSE_TOOL = "phantom.transpose"
GIZMO_TOOLS = {'MOVE': "builtin.move", 'ROTATE': "builtin.rotate", 'SCALE': "builtin.scale"}
MODE_ITEMS = (
    ('MOVE', "Move", "Move the model (W)", 'EMPTY_ARROWS', 0),
    ('ROTATE', "Rotate", "Rotate the model", 'FILE_REFRESH', 1),
    ('SCALE', "Scale", "Scale the model", 'FULLSCREEN_ENTER', 2),
)
STYLE_ITEMS = (
    ('GIZMO', "Gizmo", "Move, rotate and scale with Blender's gizmo", 'GIZMO', 0),
    ('TRANSPOSE', "Transpose", "Move, rotate and scale with a ZBrush-style action line", 'DRIVER_DISTANCE', 1),
)
# Unscaled pixels: inner circle, outer ring, and how close to the line counts as on it.
INNER_RADIUS = 8
OUTER_RADIUS = 17
LINE_PICK = 6
# Colors: line, outer rings, and the inner circles per mode.
LINE_COLOR = (1.0, 0.54, 0.24, 1.0)
RING_COLOR = (1.0, 0.83, 0.3, 1.0)
INNER_COLORS = {'MOVE': (1.0, 0.54, 0.24, 1.0), 'SCALE': (0.49, 1.0, 0.23, 1.0),
                'ROTATE': (1.0, 0.3, 0.42, 1.0)}
OUTLINE_COLOR = (0.0, 0.0, 0.0, 0.6)

_settings = {"style": 'GIZMO', "mode": 'MOVE'}
# The action line, in world space: {"a": Vector, "b": Vector} or None.
_line = {"value": None}
_draw_handle = None


# -----------------------------------------------------------------------------
# Settings

def _settings_path():
    return os.path.join(bpy.utils.user_resource('CONFIG'), "phantom_transpose.json")


def _load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    if data.get("style") in {'GIZMO', 'TRANSPOSE'}:
        _settings["style"] = data["style"]
    if data.get("mode") in GIZMO_TOOLS:
        _settings["mode"] = data["mode"]


def _save_settings():
    path = _settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_settings, fh, indent=2)
    except OSError as ex:
        print("Phantom: could not save transpose settings:", ex)


def _active_tool(context):
    tool = context.workspace.tools.from_space_view3d_mode('SCULPT', create=False)
    return tool.idname if tool else ""


def _activate(context):
    """Switch to the tool for the current mode and style."""
    if _settings["style"] == 'TRANSPOSE':
        bpy.ops.wm.tool_set_by_id(name=TRANSPOSE_TOOL)
    else:
        bpy.ops.wm.tool_set_by_id(name=GIZMO_TOOLS[_settings["mode"]])
    _redraw(context)


def _redraw(context):
    screen = context.screen
    for area in screen.areas if screen else ():
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def _transform_tool_active(context):
    idname = _active_tool(context)
    return idname == TRANSPOSE_TOOL or idname in GIZMO_TOOLS.values()


class SCULPT_OT_phantom_transform_tool(Operator):
    """Move, rotate or scale the model, with the gizmo or the Transpose action line"""
    bl_idname = "sculpt.phantom_transform_tool"
    bl_label = "Transform Tool"
    bl_options = {'INTERNAL'}

    mode: EnumProperty(items=MODE_ITEMS, options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return next(item[2] for item in MODE_ITEMS if item[0] == properties.mode)

    @classmethod
    def poll(cls, context):
        return context.mode == 'SCULPT'

    def execute(self, context):
        _settings["mode"] = self.mode
        _save_settings()
        _activate(context)
        return {'FINISHED'}


class SCULPT_OT_phantom_transform_style(Operator):
    """Choose between the gizmo and the Transpose action line for Move, Rotate and Scale"""
    bl_idname = "sculpt.phantom_transform_style"
    bl_label = "Transform Style"
    bl_options = {'INTERNAL'}

    style: EnumProperty(items=STYLE_ITEMS, options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return next(item[2] for item in STYLE_ITEMS if item[0] == properties.style)

    @classmethod
    def poll(cls, context):
        return context.mode == 'SCULPT'

    def execute(self, context):
        _settings["style"] = self.style
        _save_settings()
        _activate(context)
        return {'FINISHED'}


def draw_transform_icons(layout, context):
    """Five small icons in the middle of the Sculpt Mode tool header (drawn by the header)."""
    active = _active_tool(context)
    row = layout.row(align=True)
    for mode, _name, _desc, icon, _value in MODE_ITEMS:
        on = active == TRANSPOSE_TOOL and _settings["mode"] == mode or active == GIZMO_TOOLS[mode]
        row.operator(SCULPT_OT_phantom_transform_tool.bl_idname, text="", icon=icon,
                     depress=on).mode = mode
    row = layout.row(align=True)
    for style, _name, _desc, icon, _value in STYLE_ITEMS:
        row.operator(SCULPT_OT_phantom_transform_style.bl_idname, text="", icon=icon,
                     depress=_settings["style"] == style).style = style


# -----------------------------------------------------------------------------
# Transpose geometry

def _ui_scale(context):
    return context.preferences.system.ui_scale


def _to_screen(region, rv3d, co):
    return view3d_utils.location_3d_to_region_2d(region, rv3d, co)


def _at_depth(region, rv3d, mouse, depth_point):
    return view3d_utils.region_2d_to_location_3d(region, rv3d, mouse, depth_point)


def _surface_hit(context, mouse):
    region, rv3d = context.region, context.region_data
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse)
    depsgraph = context.evaluated_depsgraph_get()
    hit, location, _normal, _index, ob, _matrix = context.scene.ray_cast(depsgraph, origin, direction)
    if hit and ob is not None and ob.original == context.active_object:
        return location
    return None


def _pick(context, mouse):
    """What is under the mouse: ("A"|"B"|"M", "INNER"|"RING"), ("LINE", None) or None."""
    line = _line["value"]
    if line is None:
        return None
    region, rv3d = context.region, context.region_data
    s = _ui_scale(context)
    points = {"A": line["a"], "B": line["b"], "M": (line["a"] + line["b"]) / 2.0}
    screen = {key: _to_screen(region, rv3d, co) for key, co in points.items()}
    if any(co is None for co in screen.values()):
        return None
    m = Vector(mouse)
    best = None
    for key in ("A", "B", "M"):
        d = (screen[key] - m).length
        if d <= INNER_RADIUS * s:
            return key, "INNER"
        if d <= (OUTER_RADIUS + 4) * s and (best is None or d < best[0]):
            best = (d, key)
    if best is not None:
        return best[1], "RING"
    # On the line itself.
    a, b = screen["A"], screen["B"]
    ab = b - a
    if ab.length > 1e-6:
        t = max(0.0, min(1.0, (m - a).dot(ab) / ab.length_squared))
        if (a + ab * t - m).length <= LINE_PICK * s:
            return "LINE", None
    return None


def _about(point, linear):
    """`linear` (3x3) applied about `point`, as a 4x4 matrix."""
    return Matrix.Translation(point) @ linear.to_4x4() @ Matrix.Translation(-point)


def _outer(u, v):
    return Matrix(((u.x * v.x, u.x * v.y, u.x * v.z),
                   (u.y * v.x, u.y * v.y, u.y * v.z),
                   (u.z * v.x, u.z * v.y, u.z * v.z)))


# -----------------------------------------------------------------------------
# Transpose operator

class SCULPT_OT_phantom_transpose(Operator):
    """Draw a Transpose action line on the model, or use the one shown"""
    bl_idname = "sculpt.phantom_transpose"
    bl_label = "Transpose"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'SCULPT' and context.active_object is not None and
                context.region is not None and context.region.type == 'WINDOW')

    def invoke(self, context, event):
        mouse = (event.mouse_region_x, event.mouse_region_y)
        self.start_mouse = Vector(mouse)
        picked = _pick(context, mouse)
        line = _line["value"]
        if picked is None:
            hit = _surface_hit(context, mouse)
            if hit is None:
                return {'PASS_THROUGH'}
            self.action = 'CREATE'
            self.previous = line
            _line["value"] = {"a": hit.copy(), "b": hit.copy()}
        else:
            key, part = picked
            self.start_line = {"a": line["a"].copy(), "b": line["b"].copy()}
            if key == "LINE" or (key == "M" and part == "RING"):
                self.action = 'MOVE_LINE'
            elif part == "RING":
                self.action = 'MOVE_END'
                self.handle = key
            else:
                self.action = 'TRANSFORM'
                self.handle = key
                # The sculpt transform skips deforming while "Affect Only Locations" is on; it is
                # switched off for the drag and restored afterwards.
                tool_settings = context.tool_settings
                self.pivot_only = tool_settings.use_transform_pivot_point_align
                tool_settings.use_transform_pivot_point_align = False
                # `True`: let the transform make its undo step (Python calls skip undo by default).
                if bpy.ops.sculpt.phantom_transform('EXEC_DEFAULT', True, action='BEGIN') != {'FINISHED'}:
                    tool_settings.use_transform_pivot_point_align = self.pivot_only
                    return {'CANCELLED'}
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            self._update(context, event)
            _redraw(context)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self._finish(context, cancel=False)
            return {'FINISHED'}
        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._finish(context, cancel=True)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _finish(self, context, cancel):
        if self.action == 'TRANSFORM':
            bpy.ops.sculpt.phantom_transform('EXEC_DEFAULT', True, action='CANCEL' if cancel else 'END')
            context.tool_settings.use_transform_pivot_point_align = self.pivot_only
            if cancel:
                _line["value"] = self.start_line
        elif self.action == 'CREATE':
            line = _line["value"]
            region, rv3d = context.region, context.region_data
            a, b = _to_screen(region, rv3d, line["a"]), _to_screen(region, rv3d, line["b"])
            # A click without a drag leaves no line.
            if cancel or a is None or b is None or (a - b).length < 4 * _ui_scale(context):
                _line["value"] = self.previous if cancel else None
        elif cancel:
            _line["value"] = self.start_line
        _redraw(context)

    def _update(self, context, event):
        region, rv3d = context.region, context.region_data
        mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        line = _line["value"]

        if self.action == 'CREATE':
            # The end snaps to the model under it, else stays at the start's depth.
            hit = _surface_hit(context, mouse)
            line["b"] = hit if hit is not None else _at_depth(region, rv3d, mouse, line["a"])
            return

        start = self.start_line
        if self.action == 'MOVE_LINE':
            mid = (start["a"] + start["b"]) / 2.0
            delta = _at_depth(region, rv3d, mouse, mid) - _at_depth(region, rv3d, self.start_mouse, mid)
            line["a"], line["b"] = start["a"] + delta, start["b"] + delta
            return
        if self.action == 'MOVE_END':
            key = self.handle.lower()
            line[key] = _at_depth(region, rv3d, mouse, start[key])
            return
        self._transform(context, event, mouse)

    def _transform(self, context, event, mouse):
        region, rv3d = context.region, context.region_data
        line = _line["value"]
        a0, b0 = self.start_line["a"], self.start_line["b"]
        mode = _settings["mode"]
        axis = b0 - a0
        if axis.length < 1e-9:
            return
        n = axis.normalized()
        identity = Matrix.Identity(3)

        if self.handle == "M":
            mid = (a0 + b0) / 2.0
            # Along the line on screen, as a fraction of its on-screen length (A -> B positive).
            sa, sb = _to_screen(region, rv3d, a0), _to_screen(region, rv3d, b0)
            along = 0.0
            if sa is not None and sb is not None and (sb - sa).length > 1e-6:
                screen_axis = sb - sa
                along = (mouse - self.start_mouse).dot(screen_axis) / screen_axis.length_squared
            if mode == 'MOVE':
                delta = _at_depth(region, rv3d, mouse, mid) - _at_depth(region, rv3d, self.start_mouse, mid)
                if event.shift:
                    delta = n * delta.dot(n)
                matrix = Matrix.Translation(delta)
                line["a"], line["b"] = a0 + delta, b0 + delta
            elif mode == 'SCALE':
                factor = max(0.01, 1.0 + along)
                matrix = _about(a0, identity + (identity - _outer(n, n)) * (factor - 1.0))
            else:
                matrix = _about(a0, Matrix.Rotation(along * math.pi, 3, n))
            origin = mid
        else:
            key, other = ("a", "b") if self.handle == "A" else ("b", "a")
            p0, o = self.start_line[key], self.start_line[other]
            p1 = _at_depth(region, rv3d, mouse, p0)
            d0, d1 = p0 - o, p1 - o
            if mode == 'MOVE':
                # Stretch/shear: the dragged end follows the mouse, the other end stays.
                matrix = _about(o, identity + _outer(p1 - p0, d0) * (1.0 / d0.length_squared))
                line[key] = p1
            elif mode == 'SCALE':
                factor = max(0.01, d1.length / d0.length)
                matrix = _about(o, identity * factor)
                line[key] = o + d0 * factor
            else:
                if d1.length < 1e-9:
                    return
                rotation = d0.rotation_difference(d1).to_matrix()
                matrix = _about(o, rotation)
                line[key] = o + d1.normalized() * d0.length
            origin = o

        ob = context.active_object
        world = ob.matrix_world
        to_local = world.inverted_safe()
        bpy.ops.sculpt.phantom_transform(action='UPDATE', matrix=to_local @ matrix @ world,
                                         origin=to_local @ origin)


# -----------------------------------------------------------------------------
# Drawing

def _circle(center, radius, segments=32):
    return [(center.x + radius * math.cos(2.0 * math.pi * i / segments),
             center.y + radius * math.sin(2.0 * math.pi * i / segments)) for i in range(segments + 1)]


def _draw():
    context = bpy.context
    line = _line["value"]
    if line is None or context.mode != 'SCULPT':
        return
    if _active_tool(context) != TRANSPOSE_TOOL:
        # Switched to another tool: the action line goes away.
        _line["value"] = None
        return
    region, rv3d = context.region, context.region_data
    a, b = _to_screen(region, rv3d, line["a"]), _to_screen(region, rv3d, line["b"])
    if a is None or b is None:
        return
    m = (a + b) / 2.0
    s = _ui_scale(context)
    inner_color = INNER_COLORS[_settings["mode"]]

    gpu.state.blend_set('ALPHA')
    lines = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    lines.bind()
    lines.uniform_float("viewportSize", (region.width, region.height))
    # The line, outlined so it reads on any surface.
    for width, color in ((4.0, OUTLINE_COLOR), (2.0, LINE_COLOR)):
        lines.uniform_float("lineWidth", width * s)
        lines.uniform_float("color", color)
        batch_for_shader(lines, 'LINES', {"pos": [a, b]}).draw(lines)
    # Outer rings.
    for center in (a, b, m):
        for width, color in ((4.0, OUTLINE_COLOR), (2.0, RING_COLOR)):
            lines.uniform_float("lineWidth", width * s)
            lines.uniform_float("color", color)
            batch_for_shader(lines, 'LINE_STRIP', {"pos": _circle(center, OUTER_RADIUS * s)}).draw(lines)
    # Inner circles, in the mode's color.
    fill = gpu.shader.from_builtin('UNIFORM_COLOR')
    fill.bind()
    for center in (a, b, m):
        for radius, color in ((INNER_RADIUS * s + 1.5 * s, OUTLINE_COLOR), (INNER_RADIUS * s, inner_color)):
            fill.uniform_float("color", color)
            batch_for_shader(fill, 'TRI_FAN', {"pos": [center] + _circle(center, radius)}).draw(fill)
    gpu.state.blend_set('NONE')


# -----------------------------------------------------------------------------
# Tool

class PHANTOM_TL_transpose(WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'SCULPT'
    bl_idname = TRANSPOSE_TOOL
    bl_label = "Transpose"
    bl_description = (
        "ZBrush-style action line: click-drag on the model to draw it, drag its rings to place "
        "it, drag its inner circles to move, rotate or scale the unmasked model"
    )
    bl_icon = "phantom.transpose"  # release/datafiles/icons, see make_tool_icons.py
    bl_keymap = (
        ("sculpt.phantom_transpose", {"type": 'LEFTMOUSE', "value": 'PRESS'}, None),
    )


classes = (
    SCULPT_OT_phantom_transform_tool,
    SCULPT_OT_phantom_transform_style,
    SCULPT_OT_phantom_transpose,
)


def register():
    global _draw_handle
    _load_settings()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.utils.register_tool(PHANTOM_TL_transpose, after={"builtin.transform"})
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    try:
        bpy.utils.unregister_tool(PHANTOM_TL_transpose)
    except (Exception, SystemError):
        # Blender's `unregister_tool` trips over tuple keymaps (`bl_keymap`) when removing the
        # tool's keymap; this only happens on quit, so there is nothing left to clean up.
        pass
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
