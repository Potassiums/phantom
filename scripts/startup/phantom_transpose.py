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
    around the line. Hold Shift to snap the angle to 15 degree steps.

Holding Shift while drawing the line or placing an end snaps its on-screen direction to 15 degree
steps too. Each inner circle is labelled with what it does in the current mode, and notches
between the circles mark the line. While rotating or snapping, the dragged circle shows the
angle, and a measuring tick sounds as it changes (like adjusting the voxel remesh size).

The deformation itself is done by ``sculpt.phantom_transform`` (Blender's sculpt transform with a
matrix), so masks, symmetry and undo work as with the gizmo tools.
"""

import json
import math
import os

import blf
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
LABEL_COLOR = (1.0, 1.0, 1.0, 0.95)
# Shift snaps rotations to this step.
ROTATION_SNAP = math.radians(15.0)
# Notches on each half of the line, between the circles, and their half length (unscaled pixels).
NOTCHES = 3
NOTCH_SIZE = 7
# Measuring ticks: one per this many on-screen pixels of length or movement, per this rotation,
# and per this scale ratio (4%, as when adjusting the voxel remesh size).
TICK_PIXELS = 20
TICK_ANGLE = math.radians(5.0)
TICK_SCALE = 1.04
LABEL_SIZE = 10
# What dragging each inner circle does, per mode: (end, middle).
LABELS = {
    'MOVE': ("Stretch", "Move"),
    'SCALE': ("Scale", "Thicken"),
    'ROTATE': ("Rotate", "Twist"),
}

_settings = {"style": 'GIZMO', "mode": 'MOVE'}
# The action line, in world space: {"a": Vector, "b": Vector} or None.
_line = {"value": None}
# What the line was drawn on: it goes away in another workspace, on another object, out of
# Sculpt Mode, and on undo, redo or loading a file (see #_forget_line).
_owner = {"object": None, "workspace": None}
# While dragging an inner circle: which one ("A", "B", "M") and what to show beside it.
_feedback = {"handle": None, "text": ""}
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


def _play_tick():
    """Phantom's measuring tick, through the interface's own sound player."""
    play = getattr(bpy.types.WindowManager, "phantom_ui_sound_play", None)
    if play is not None:
        play("measure_tick")


def _snap_direction(origin, mouse):
    """`mouse` moved so the on-screen direction from `origin` is a multiple of 15 degrees,
    and that angle (from the screen's x axis)."""
    offset = mouse - origin
    angle = _snapped(math.atan2(offset.y, offset.x), True)
    return origin + Vector((math.cos(angle), math.sin(angle))) * offset.length, angle


def _snapped(angle, snap):
    """`angle`, snapped to #ROTATION_SNAP steps when `snap` (Shift held)."""
    return round(angle / ROTATION_SNAP) * ROTATION_SNAP if snap else angle


def _angle_text(label, angle, snapped):
    text = f"{label} {math.degrees(angle):.0f}\u00b0"
    return text + "  (snap)" if snapped else text


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

    def _tick(self, step):
        """Play the measuring tick when `step` changes (not for the first one)."""
        if self.tick_step is not None and step != self.tick_step:
            _play_tick()
        self.tick_step = step

    def invoke(self, context, event):
        mouse = (event.mouse_region_x, event.mouse_region_y)
        self.start_mouse = Vector(mouse)
        self.tick_step = None
        picked = _pick(context, mouse)
        line = _line["value"]
        if picked is None:
            hit = _surface_hit(context, mouse)
            if hit is None:
                return {'PASS_THROUGH'}
            self.action = 'CREATE'
            self.previous = line
            _line["value"] = {"a": hit.copy(), "b": hit.copy()}
            _owner.update(object=context.active_object.as_pointer(),
                          workspace=context.workspace.as_pointer())
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
        if event.type in {'LEFT_SHIFT', 'RIGHT_SHIFT'} and event.value in {'PRESS', 'RELEASE'}:
            # Snapping turns on or off right away, without waiting for the mouse to move.
            self._update(context, event)
            _redraw(context)
            return {'RUNNING_MODAL'}
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
        _feedback.update(handle=None, text="")
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

        s = _ui_scale(context)
        if self.action in {'CREATE', 'MOVE_END'}:
            # Placing an end: Shift snaps the line's on-screen direction to 15 degree steps.
            if self.action == 'CREATE':
                key, fixed = "b", line["a"]
            else:
                key = self.handle.lower()
                fixed = self.start_line["b" if key == "a" else "a"]
            fixed_screen = _to_screen(region, rv3d, fixed)
            angle = None
            if event.shift and fixed_screen is not None and (mouse - fixed_screen).length > 1.0:
                mouse, angle = _snap_direction(fixed_screen, mouse)
            if self.action == 'CREATE':
                # The end snaps to the model under it, else stays at the start's depth.
                hit = _surface_hit(context, mouse)
                line["b"] = hit if hit is not None else _at_depth(region, rv3d, mouse, line["a"])
            else:
                line[key] = _at_depth(region, rv3d, mouse, self.start_line[key])
            handle = "B" if key == "b" else "A"
            if angle is not None:
                _feedback.update(handle=handle, text=f"{math.degrees(angle) % 360.0:.0f}\u00b0  (snap)")
            else:
                _feedback.update(handle=None, text="")
            length = (mouse - fixed_screen).length if fixed_screen is not None else 0.0
            self._tick((int(length / (TICK_PIXELS * s)), angle))
            return

        start = self.start_line
        if self.action == 'MOVE_LINE':
            mid = (start["a"] + start["b"]) / 2.0
            delta = _at_depth(region, rv3d, mouse, mid) - _at_depth(region, rv3d, self.start_mouse, mid)
            line["a"], line["b"] = start["a"] + delta, start["b"] + delta
            self._tick(int((mouse - self.start_mouse).length / (TICK_PIXELS * s)))
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
                self._tick(int(delta.length / axis.length * 20.0))
            elif mode == 'SCALE':
                factor = max(0.01, 1.0 + along)
                matrix = _about(a0, identity + (identity - _outer(n, n)) * (factor - 1.0))
                self._tick(math.floor(math.log(factor) / math.log(TICK_SCALE)))
            else:
                angle = _snapped(along * math.pi, event.shift)
                self._tick(math.floor(angle / TICK_ANGLE + 1e-6))
                matrix = _about(a0, Matrix.Rotation(angle, 3, n))
                _feedback.update(handle="M", text=_angle_text("Twist", angle, event.shift))
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
                self._tick(int((p1 - p0).length / d0.length * 20.0))
            elif mode == 'SCALE':
                factor = max(0.01, d1.length / d0.length)
                matrix = _about(o, identity * factor)
                line[key] = o + d0 * factor
                self._tick(math.floor(math.log(factor) / math.log(TICK_SCALE)))
            else:
                if d1.length < 1e-9:
                    return
                axis = d0.cross(d1)
                if axis.length < 1e-12:
                    rotation, angle = identity, 0.0
                else:
                    angle = _snapped(d0.angle(d1), event.shift)
                    rotation = Matrix.Rotation(angle, 3, axis.normalized())
                matrix = _about(o, rotation)
                line[key] = o + rotation @ d0
                self._tick(math.floor(angle / TICK_ANGLE + 1e-6))
                _feedback.update(handle=self.handle, text=_angle_text("Rotate", angle, event.shift))
            origin = o

        ob = context.active_object
        world = ob.matrix_world
        to_local = world.inverted_safe()
        bpy.ops.sculpt.phantom_transform(action='UPDATE', matrix=to_local @ matrix @ world,
                                         origin=to_local @ origin)


# -----------------------------------------------------------------------------
# Drawing

def _notches(a, b, s):
    """Short ticks across the line, evenly spaced between the circles (#NOTCHES per half)."""
    axis = b - a
    length = axis.length
    if length < 1e-6:
        return []
    u = axis / length
    perp = Vector((-u.y, u.x))
    half = NOTCH_SIZE * s
    gap = OUTER_RADIUS * s
    ticks = []
    # Each half runs between the edges of two rings.
    for start, end in ((0.0, length / 2.0), (length / 2.0, length)):
        inner_start, inner_end = start + gap, end - gap
        if inner_end - inner_start < 4.0 * half:
            continue
        for i in range(1, NOTCHES + 1):
            t = inner_start + (inner_end - inner_start) * i / (NOTCHES + 1)
            point = a + u * t
            ticks += [point - perp * half, point + perp * half]
    return ticks


def _draw_label(text, center, offset, s):
    """Text centered beside a circle, `offset` away from its center, with a shadow."""
    blf.size(0, LABEL_SIZE * s)
    width, height = blf.dimensions(0, text)
    # Grow away from the circle: left of it ends at the offset, right of it starts there, above
    # or below it is centered (and in between, in between).
    direction = offset.normalized() if offset.length > 1e-6 else Vector((0.0, 1.0))
    x = center.x + offset.x - width * (1.0 - direction.x) / 2.0
    y = center.y + offset.y - height * (1.0 - direction.y) / 2.0
    blf.enable(0, blf.SHADOW)
    blf.shadow(0, 3, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(0, 1, -1)
    blf.position(0, round(x), round(y), 0)
    blf.color(0, *LABEL_COLOR)
    blf.draw(0, text)
    blf.disable(0, blf.SHADOW)


def _circle(center, radius, segments=32):
    return [(center.x + radius * math.cos(2.0 * math.pi * i / segments),
             center.y + radius * math.sin(2.0 * math.pi * i / segments)) for i in range(segments + 1)]


@bpy.app.handlers.persistent
def _forget_line(*_args):
    """Undo, redo or a new file: the line no longer matches the model."""
    _line["value"] = None
    _feedback.update(handle=None, text="")


def _line_belongs(context):
    """Whether the line was drawn here: same workspace and object, still sculpting with
    Transpose."""
    ob = context.active_object
    return (context.mode == 'SCULPT' and ob is not None and
            ob.as_pointer() == _owner["object"] and
            context.workspace.as_pointer() == _owner["workspace"] and
            _active_tool(context) == TRANSPOSE_TOOL)


def _draw():
    context = bpy.context
    line = _line["value"]
    if line is None:
        return
    if not _line_belongs(context):
        # Another tool, workspace or object, or out of Sculpt Mode: the action line goes away.
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
    # Notches between the circles.
    ticks = _notches(a, b, s)
    if ticks:
        for width, color in ((4.0, OUTLINE_COLOR), (2.0, LINE_COLOR)):
            lines.uniform_float("lineWidth", width * s)
            lines.uniform_float("color", color)
            batch_for_shader(lines, 'LINES', {"pos": ticks}).draw(lines)
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

    # Labels: what each inner circle does, on the side of the line facing up. While dragging, only
    # the dragged circle, with its angle when rotating.
    axis = b - a
    perp = Vector((-axis.y, axis.x)).normalized() if axis.length > 1e-6 else Vector((0.0, 1.0))
    if perp.y < 0.0:
        perp = -perp
    offset = perp * ((OUTER_RADIUS + 6) * s)
    end_label, middle_label = LABELS[_settings["mode"]]
    labels = {"A": end_label, "B": end_label, "M": middle_label}
    centers = {"A": a, "B": b, "M": m}
    if _feedback["handle"] is not None:
        _draw_label(_feedback["text"] or labels[_feedback["handle"]], centers[_feedback["handle"]],
                    offset, s)
    else:
        for key, center in centers.items():
            _draw_label(labels[key], center, offset, s)
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
        # Shift held before clicking snaps too (else Shift-click would smooth instead).
        ("sculpt.phantom_transpose", {"type": 'LEFTMOUSE', "value": 'PRESS', "shift": True}, None),
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
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post, bpy.app.handlers.load_post):
        handlers.append(_forget_line)


def unregister():
    global _draw_handle
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post, bpy.app.handlers.load_post):
        if _forget_line in handlers:
            handlers.remove(_forget_line)
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
