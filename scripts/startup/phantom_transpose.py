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

The line takes the color of the mode (Move orange, Rotate red, Scale green) and stays on screen
when switching between them, also when Transpose was picked from the toolbar.

Holding Shift while drawing the line or placing an end snaps its on-screen direction to 15 degree
steps too. Each inner circle is labelled with what it does in the current mode, and notches
between the circles mark the line. While rotating or snapping, the dragged circle shows the
angle, and a measuring tick sounds as it changes (like adjusting the voxel remesh size).

The deformation itself is done by ``sculpt.phantom_transform`` (Blender's sculpt transform with a
matrix), so masks, symmetry and undo work as with the gizmo tools.

Above the gizmo (after ZBrush's) sits a row of icons, in the simple view and the usual interface:

- **Transform types** (gear): a card with the gizmo's other tools, as in ZBrush, where Phantom has
  them: the primitives, Move, Rotate, Scale, Stretch and Twist (Transpose), Smooth and Inflate
  (mesh filters), Divide, Remesh, Quad Remesh and Union.
- **Sticky** (pin): after each move, rotate or scale the gizmo's pivot and axes go back to where
  they were; the mesh stays where it was moved to.
- **Center** (GPS): the pivot on the mesh's center of volume (of its unmasked part when masked).
- **Home**: the pivot to the world origin.
- **Reset rotation** (spin): the gizmo back to the world axes.
- **Lock**: locked, the gizmo moves the mesh (Home and Reset rotation take it along); unlocked, it
  moves only its pivot. Holding Alt as a drag starts unlocks it for that drag.
- **All visible** (list): move every visible mesh, not only the one sculpted.

The gizmo's axes turn with the mesh it rotated (or with the pivot alone, unlocked). Moving the
pivot never moves the mesh, and every change of the pivot or the meshes can be undone.
"""

import json
import math
import os
import time

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
# Colors per mode: the line, its notches and the inner circles take the mode's color, the outer
# rings a lighter one, so switching between Move, Rotate and Scale shows on the line.
MODE_COLORS = {'MOVE': (1.0, 0.54, 0.24, 1.0), 'SCALE': (0.49, 1.0, 0.23, 1.0),
               'ROTATE': (1.0, 0.3, 0.42, 1.0)}
RING_LIGHTEN = 0.45
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

_settings = {"style": 'GIZMO', "mode": 'MOVE',
             # The gizmo's options (its icon row): see #phantom_gizmo_options_set in C.
             "sticky": False, "locked": True, "all_visible": False}
GIZMO_OPTIONS = ("sticky", "locked", "all_visible")
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
    for key in GIZMO_OPTIONS:
        if isinstance(data.get(key), bool):
            _settings[key] = data[key]


def _push_gizmo_options():
    """Hand the gizmo's options to the sculpt transform (C), which follows them."""
    set_options = getattr(bpy.types.WindowManager, "phantom_sculpt_gizmo_options", None)
    if set_options is not None:
        set_options(sticky=_settings["sticky"], all_visible=_settings["all_visible"],
                    unlocked=not _settings["locked"])


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


def _style_in_use(context):
    """The style of the transform tool in use (also when it was picked from the toolbar), else the
    one chosen last."""
    idname = _active_tool(context)
    if idname == TRANSPOSE_TOOL:
        return 'TRANSPOSE'
    if idname in GIZMO_TOOLS.values():
        return 'GIZMO'
    return _settings["style"]


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
        # Switching modes keeps the style in use: Transpose stays, with its action line.
        _settings["style"] = _style_in_use(context)
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
    style_in_use = _style_in_use(context)
    for style, _name, _desc, icon, _value in STYLE_ITEMS:
        row.operator(SCULPT_OT_phantom_transform_style.bl_idname, text="", icon=icon,
                     depress=style_in_use == style).style = style


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
    mode_color = MODE_COLORS[_settings["mode"]]
    ring_color = (*(c + (1.0 - c) * RING_LIGHTEN for c in mode_color[:3]), 1.0)

    gpu.state.blend_set('ALPHA')
    lines = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    lines.bind()
    lines.uniform_float("viewportSize", (region.width, region.height))
    # The line, outlined so it reads on any surface.
    for width, color in ((4.0, OUTLINE_COLOR), (2.0, mode_color)):
        lines.uniform_float("lineWidth", width * s)
        lines.uniform_float("color", color)
        batch_for_shader(lines, 'LINES', {"pos": [a, b]}).draw(lines)
    # Notches between the circles.
    ticks = _notches(a, b, s)
    if ticks:
        for width, color in ((4.0, OUTLINE_COLOR), (2.0, mode_color)):
            lines.uniform_float("lineWidth", width * s)
            lines.uniform_float("color", color)
            batch_for_shader(lines, 'LINES', {"pos": ticks}).draw(lines)
    # Outer rings.
    for center in (a, b, m):
        for width, color in ((4.0, OUTLINE_COLOR), (2.0, ring_color)):
            lines.uniform_float("lineWidth", width * s)
            lines.uniform_float("color", color)
            batch_for_shader(lines, 'LINE_STRIP', {"pos": _circle(center, OUTER_RADIUS * s)}).draw(lines)
    # Inner circles, in the mode's color.
    fill = gpu.shader.from_builtin('UNIFORM_COLOR')
    fill.bind()
    for center in (a, b, m):
        for radius, color in ((INNER_RADIUS * s + 1.5 * s, OUTLINE_COLOR), (INNER_RADIUS * s, mode_color)):
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
# Gizmo icon row
#
# Drawn above the gizmo in the simple view's look (its toolbars' graphite, hairline, corners,
# button size and hover; see phantom_simple_view): Phantom orange marks only what is on.

# The row's buttons, in groups: (key, tooltip). Tooltips name the state; see #_row_tip.
ROW_GROUPS = (('TYPES',), ('STICKY', 'CENTER', 'HOME', 'SPIN'), ('LOCK', 'ALL'))
# Unscaled pixels: between the gizmo's arrows and the row, and the row's distance from the
# region's edges.
ROW_ABOVE = 12
ROW_MARGIN = 8
# The gizmo's arrows (with their tips) reach about this many times the Gizmo Size preference
# from its center: the row keeps clear of them, or they would take its clicks.
GIZMO_REACH = 1.12
# The Transform Types card, unscaled pixels on a 4 px grid: padding, title row, section label
# row, primitive tile, the gap between tiles and pills, a pill, its columns, and the caption row.
TYPES_PAD = 16
TYPES_TITLE = 24
TYPES_LABEL = 20
TYPES_TILE = 64
TYPES_GAP = 8
TYPES_PILL = 32
TYPES_COLUMNS = 4
TYPES_CAPTION = 20

# The tools of ZBrush's Transform Types that Phantom has: (key, label, what it is and does).
TYPES = (
    ('MOVE', "Move", "Move the model with the gizmo (ZBrush: Offset)"),
    ('ROTATE', "Rotate", "Rotate the model with the gizmo"),
    ('SCALE', "Scale", "Scale the model with the gizmo"),
    ('STRETCH', "Stretch", "Transpose: drag an end circle to stretch the model along the line"),
    ('TWIST', "Twist", "Transpose Rotate: drag the middle circle along the line to twist"),
    ('SMOOTH', "Smooth", "Mesh filter: drag left or right in the viewport to smooth everything "
                         "unmasked"),
    ('INFLATE', "Inflate", "Mesh filter: drag left or right to inflate or deflate the unmasked "
                           "model"),
    ('DIVIDE', "Subdivide", "Divide: subdivide the mesh one level (Ctrl D)"),
    ('REMESH', "Remesh", "Voxel remesh, even polygons all over (ZBrush: Remesh by DynaMesh)"),
    ('QUAD', "Quad Remesh", "Rebuild the mesh with clean quads (ZBrush: Remesh by ZRemesher)"),
    ('UNION', "Union", "Join the mesh below into this one as one solid (ZBrush: Remesh by "
                       "Union)"),
)

# The button under the cursor and since when, the one pressed, and whether the Transform Types
# card is open with the item under the cursor or pressed there.
_row = {"hover": None, "since": 0.0, "pressed": None}
_types = {"open": False, "hover": None, "pressed": None}


def _sv():
    import phantom_simple_view
    return phantom_simple_view


def _row_on(key):
    """Whether a button shows as on (the accent)."""
    if key == 'TYPES':
        return _types["open"]
    if key == 'STICKY':
        return _settings["sticky"]
    if key == 'LOCK':
        # Unlocked changes what the gizmo does: that is what stands out.
        return not _settings["locked"]
    if key == 'ALL':
        return _settings["all_visible"]
    return False


def _row_icon(key):
    """Blender's icon for a button, or None for the ones drawn here (#_draw_row_icon)."""
    return {
        'TYPES': 'PREFERENCES',
        'STICKY': 'PINNED' if _settings["sticky"] else 'UNPINNED',
        'HOME': 'HOME',
        'SPIN': 'FILE_REFRESH',
    }.get(key)


def _bar(x0, y0, x1, y1, width, color):
    """A straight stroke `width` pixels wide (as a quad: line widths vary between GPU backends)."""
    d = Vector((x1 - x0, y1 - y0))
    if d.length < 1e-6:
        return
    n = Vector((-d.y, d.x)).normalized() * (width / 2.0)
    _sv()._draw('TRI_FAN', [(x0 + n.x, y0 + n.y), (x1 + n.x, y1 + n.y), (x1 - n.x, y1 - n.y),
                            (x0 - n.x, y0 - n.y)], color)


def _draw_row_icon(key, cx, cy, size, s):
    """The icons drawn here, as big as Blender's around them: a lock (Blender's is small), a map
    pin (Center, ZBrush's GPS) and a checklist (All visible: one row checked for this mesh only,
    all of them for every visible mesh)."""
    sv = _sv()
    color = (1.0, 1.0, 1.0, 0.92)
    r = size / 2.0
    stroke = max(1.5, 1.6 * s)
    if key == 'LOCK':
        # The body, then the shackle over it: closed when locked, its left side lifted out of
        # the body when unlocked.
        body_w, body_h = r * 1.3, r * 0.95
        body_y = cy - r * 0.8
        sv._rounded_rect(cx - body_w / 2, body_y, body_w, body_h, 2.0 * s, color)
        top = body_y + body_h
        shackle_r = r * 0.42
        center_y = top + r * (0.18 if _settings["locked"] else 0.42)
        sv._arc(cx, center_y, shackle_r, stroke, 0.0, math.pi, color)
        _bar(cx + shackle_r, center_y, cx + shackle_r, top, stroke, color)
        left_end = top if _settings["locked"] else center_y - r * 0.12
        _bar(cx - shackle_r, center_y, cx - shackle_r, left_end, stroke, color)
        return
    if key == 'CENTER':
        head_y, head_r = cy + r * 0.28, r * 0.52
        # The head's ring, open where the point leaves it, and the point.
        a0, a1 = math.radians(-38.0), math.radians(218.0)
        sv._arc(cx, head_y, head_r, stroke, a0, a1, color)
        tip = (cx, cy - r * 0.95)
        for a in (a0, a1):
            _bar(cx + head_r * math.cos(a), head_y + head_r * math.sin(a), *tip, stroke, color)
        sv._disc(cx, head_y, head_r * 0.36, color)
        return
    box = r * 0.42
    for i, row_y in enumerate((cy + r * 0.62, cy, cy - r * 0.62)):
        bx = cx - r
        checked = i == 0 or _settings["all_visible"]
        if checked:
            sv._rounded_rect(bx, row_y - box / 2, box, box, 1.5 * s, color)
        else:
            sv._outline(sv._rounded_rect(bx, row_y - box / 2, box, box, 1.5 * s, (0, 0, 0, 0)),
                        color, max(1.0, 1.2 * s))
        _bar(cx - r * 0.3, row_y, cx + r, row_y, stroke, color)


def _row_tip(key):
    locked = _settings["locked"]
    if key == 'TYPES':
        return "Transform types: primitives and the gizmo's other tools"
    if key == 'STICKY':
        return ("Sticky on: the gizmo goes back to where it was after each move, rotate or scale"
                if _settings["sticky"] else
                "Sticky off: the gizmo stays with the mesh. Click to have it go back after each "
                "move")
    if key == 'CENTER':
        return "Center: put the gizmo on the mesh's center of volume (unmasked part when masked)"
    if key == 'HOME':
        return ("Home: move the mesh and the gizmo to the world origin" if locked else
                "Home: put the gizmo at the world origin")
    if key == 'SPIN':
        return ("Reset rotation: turn the mesh and the gizmo back to the world axes" if locked else
                "Reset rotation: turn the gizmo back to the world axes")
    if key == 'LOCK':
        return ("Locked: the gizmo moves the mesh. Click to move only the gizmo (or hold Alt)"
                if locked else
                "Unlocked: the gizmo moves only itself, not the mesh. Click to lock")
    if _settings["all_visible"]:
        return "All visible meshes move with the gizmo. Click to move only this one"
    return "Only this mesh moves with the gizmo. Click to move every visible mesh"


def _row_shown(context):
    """Whether the row goes above the gizmo here: a gizmo tool in Sculpt Mode, gizmos shown."""
    region, space = context.region, context.space_data
    if (context.mode != 'SCULPT' or region is None or region.type != 'WINDOW' or space is None or
            space.type != 'VIEW_3D' or context.active_object is None):
        return False
    if not (space.show_gizmo and space.show_gizmo_tool):
        return False
    return _active_tool(context) in GIZMO_TOOLS.values()


def _row_layout(context):
    """The row's rectangle, its buttons as ``(key, (x, y, w, h))`` and its dividers as
    ``(x, y0, y1)``, or None when it isn't shown."""
    if not _row_shown(context):
        return None
    pivot_at = getattr(bpy.types.WindowManager, "phantom_sculpt_pivot", None)
    if pivot_at is None:
        return None
    region, rv3d = context.region, context.region_data
    center = _to_screen(region, rv3d, Vector(pivot_at()))
    if center is None:
        return None
    sv = _sv()
    s = _ui_scale(context)
    item, gap, pad = sv.ITEM * s, sv.ITEM_GAP * s, sv.BAR_PAD * s
    group_gap = sv.GROUP_GAP * s
    count = sum(len(group) for group in ROW_GROUPS)
    w = (2 * pad + count * item + gap * (count - len(ROW_GROUPS)) +
         group_gap * (len(ROW_GROUPS) - 1))
    h = item + 2 * pad
    reach = context.preferences.view.gizmo_size * GIZMO_REACH * s
    margin = ROW_MARGIN * s
    top = region.height - margin
    try:
        top -= sv._top_inset(context.area, region)
    except (AttributeError, TypeError):
        pass
    y = center.y + reach + ROW_ABOVE * s
    if y + h > top:
        # No room above: below the gizmo.
        y = center.y - reach - ROW_ABOVE * s - h
    y = round(max(margin, min(y, top - h)))
    x = round(max(margin, min(center.x - w / 2, region.width - margin - w)))
    buttons, dividers = [], []
    bx = x + pad
    for index, group in enumerate(ROW_GROUPS):
        if index:
            dividers.append((bx - gap + group_gap / 2, y + h * 0.3, y + h * 0.7))
            bx += group_gap - gap
        for key in group:
            buttons.append((key, (bx, y + pad, item, item)))
            bx += item + gap
    return {"rect": (x, y, w, h), "buttons": buttons, "dividers": dividers}


def _in(rect, mx, my):
    x, y, w, h = rect
    return x <= mx <= x + w and y <= my <= y + h


def _types_layout(context, row):
    """The Transform Types card: above the row (else below), with the primitives as tiles,
    the tools as pills in columns, and a caption saying what the one under the cursor does."""
    sv = _sv()
    s = _ui_scale(context)
    region = context.region
    pad, gap = TYPES_PAD * s, TYPES_GAP * s
    tile, pill = TYPES_TILE * s, TYPES_PILL * s
    tiles_n = len(sv.PRIMITIVES)
    inner_w = tiles_n * tile + (tiles_n - 1) * gap
    rows = -(-len(TYPES) // TYPES_COLUMNS)
    pill_w = (inner_w - (TYPES_COLUMNS - 1) * gap) / TYPES_COLUMNS
    w = inner_w + 2 * pad
    h = (2 * pad + TYPES_TITLE * s + 2 * (TYPES_LABEL * s + gap) + tile + 2 * gap +
         rows * pill + (rows - 1) * gap + gap + TYPES_CAPTION * s)
    rx, ry, rw, rh = row["rect"]
    margin = ROW_MARGIN * s
    y = ry + rh + gap
    if y + h > region.height - margin:
        y = ry - gap - h
    y = max(margin, min(y, region.height - margin - h))
    x = max(margin, min(rx + rw / 2 - w / 2, region.width - margin - w))
    top = y + h - pad
    title_y = top - TYPES_TITLE * s
    label1_y = title_y - gap - TYPES_LABEL * s
    tiles_top = label1_y - gap
    items = []
    for i, (kind, name, icon) in enumerate(sv.PRIMITIVES):
        items.append(("PRIM_" + kind, name, icon,
                      (x + pad + i * (tile + gap), tiles_top - tile, tile, tile)))
    label2_y = tiles_top - tile - 2 * gap - TYPES_LABEL * s
    pills_top = label2_y - gap
    for i, (key, label, _desc) in enumerate(TYPES):
        col, line = i % TYPES_COLUMNS, i // TYPES_COLUMNS
        items.append((key, label, None, (x + pad + col * (pill_w + gap),
                                         pills_top - (line + 1) * pill - line * gap, pill_w, pill)))
    return {"rect": (x, y, w, h), "title": (x + pad, title_y + TYPES_TITLE * s / 2),
            "labels": ((x + pad, label1_y + TYPES_LABEL * s / 2, "Primitives"),
                       (x + pad, label2_y + TYPES_LABEL * s / 2, "Tools")),
            "caption": (x + pad, y + pad + TYPES_CAPTION * s / 2), "items": items}


def _types_caption(key):
    if key is None:
        return "Add a primitive to sculpt, or pick a tool"
    if key.startswith("PRIM_"):
        name = next((p[1] for p in _sv().PRIMITIVES if "PRIM_" + p[0] == key), "primitive")
        return f"Add a {name.lower()} to sculpt, placed first with the gizmo"
    return next((d for k, _l, d in TYPES if k == key), "")


def _draw_row():
    context = bpy.context
    row = _row_layout(context)
    if row is None:
        if _types["open"] and context.region is not None and context.region.type == 'WINDOW':
            _types.update(open=False, hover=None, pressed=None)
        return
    sv = _sv()
    s = _ui_scale(context)
    gpu.state.blend_set('ALPHA')
    outline = sv._panel(*row["rect"], sv.BAR_CORNER * s, shadow=False)
    sv._outline(outline, sv.OUTLINE_COLOR)
    for x, y0, y1 in row["dividers"]:
        sv._line(x, y0, x, y1, sv.DIVIDER_COLOR)
    for key, rect in row["buttons"]:
        x, y, w, h = rect
        if _row_on(key):
            # On: a step of the accent under the icon, and its hairline.
            points = sv._rounded_rect(x, y, w, h, 8 * s, (*sv.ACCENT[:3], 0.18))
            sv._outline(points, sv.ACCENT, max(1.0, 1.5 * s))
        elif _row["pressed"] == key:
            sv._rounded_rect(x, y, w, h, 8 * s, sv.PRESS_COLOR)
        elif _row["hover"] == key:
            sv._rounded_rect(x, y, w, h, 8 * s, sv.HOVER_COLOR)
        icon = _row_icon(key)
        if icon is None:
            _draw_row_icon(key, x + w / 2, y + h / 2, sv.ICON_SIZE * s, s)
        else:
            sv._draw_icon(icon, x + w / 2, y + h / 2, sv.ICON_SIZE * s, 0.92)
    if _types["open"]:
        _draw_types(context, row, s)
    else:
        _draw_row_tip(context, row, s)
    gpu.state.blend_set('NONE')


def _draw_row_tip(context, row, s):
    sv = _sv()
    key = _row["hover"]
    if key is None or _row["pressed"] or time.perf_counter() - _row["since"] < sv.TOOLTIP_DELAY:
        return
    rect = next((r for k, r in row["buttons"] if k == key), None)
    if rect is None:
        return
    text = _row_tip(key)
    size = 11 * s
    blf.size(0, size)
    width = blf.dimensions(0, text)[0] + 16 * s
    height = 24 * s
    region = context.region
    x = min(max(rect[0] + rect[2] / 2 - width / 2, 4 * s), region.width - width - 4 * s)
    rx, ry, rw, rh = row["rect"]
    y = ry + rh + 8 * s
    if y + height > region.height - 4 * s:
        y = ry - 8 * s - height
    sv._rounded_rect(x, y, width, height, 6 * s, sv.TIP_COLOR)
    sv._text_mid(x + 8 * s, y + height / 2, text, size, sv.TEXT_COLOR, align='LEFT')


def _draw_types(context, row, s):
    sv = _sv()
    g = _types_layout(context, row)
    points = sv._panel(*g["rect"], sv.BAR_CORNER * s)
    sv._outline(points, sv.OUTLINE_COLOR)
    x, cy = g["title"]
    sv._text_mid(x, cy, "Transform types", 13 * s, sv.TEXT_COLOR, align='LEFT')
    for lx, ly, label in g["labels"]:
        sv._text_mid(lx, ly, label, 11 * s, sv.PILL_TEXT_COLOR, align='LEFT')
    active = _active_tool(context)
    for key, label, icon, rect in g["items"]:
        x, y, w, h = rect
        on = (key in GIZMO_TOOLS and active == GIZMO_TOOLS[key])
        if _types["pressed"] == key:
            fill = sv.PRESS_COLOR
        elif _types["hover"] == key:
            fill = sv.HOVER_COLOR
        else:
            fill = sv.PILL_COLOR
        points = sv._rounded_rect(x, y, w, h, 8 * s, fill)
        if on:
            sv._outline(points, sv.ACCENT, max(1.0, 1.5 * s))
        if icon is not None:
            sv._draw_icon(icon, x + w / 2, y + h / 2 + 8 * s, 24 * s, 0.92)
            sv._text_mid(x + w / 2, y + 12 * s, sv._fit(label, 10 * s, w - 8 * s), 10 * s,
                         sv.PILL_TEXT_COLOR)
        else:
            sv._text_mid(x + w / 2, y + h / 2, label, 11 * s,
                         sv.TEXT_COLOR if on else sv.PILL_TEXT_COLOR)
    x, cy = g["caption"]
    rx, _ry, rw, _rh = g["rect"]
    text = sv._fit(_types_caption(_types["hover"]), 11 * s, rw - 2 * TYPES_PAD * s)
    sv._text_mid(x, cy, text, 11 * s, sv.PILL_TEXT_COLOR, align='LEFT')


def _row_redraw():
    _redraw(bpy.context)
    return None


def _play_click():
    play = getattr(bpy.types.WindowManager, "phantom_ui_sound_play", None)
    if play is not None:
        play("click")


def _row_act(context, key):
    """What clicking one of the row's buttons does."""
    if key == 'TYPES':
        _types.update(open=not _types["open"], hover=None, pressed=None)
    elif key in {'STICKY', 'ALL'}:
        option = "sticky" if key == 'STICKY' else "all_visible"
        _settings[option] = not _settings[option]
    elif key == 'LOCK':
        _settings["locked"] = not _settings["locked"]
    else:
        action = {'CENTER': 'CENTER', 'HOME': 'HOME', 'SPIN': 'SPIN'}[key]
        # `True`: its own undo step, as when run from a menu (Python calls skip undo by default).
        bpy.ops.sculpt.phantom_gizmo('EXEC_DEFAULT', True, action=action)
        return
    _save_settings()
    _push_gizmo_options()


def _types_act(context, key):
    """What clicking an item of the Transform Types card does."""
    _types.update(open=False, hover=None, pressed=None)
    if key.startswith("PRIM_"):
        # `True`: each makes its own undo step, as when run from a menu.
        bpy.ops.view3d.phantom_simple_add_primitive('EXEC_DEFAULT', True, kind=key[5:])
    elif key in GIZMO_TOOLS:
        bpy.ops.sculpt.phantom_transform_style(style='GIZMO')
        bpy.ops.sculpt.phantom_transform_tool(mode=key)
    elif key in {'STRETCH', 'TWIST'}:
        bpy.ops.sculpt.phantom_transform_style(style='TRANSPOSE')
        bpy.ops.sculpt.phantom_transform_tool(mode='MOVE' if key == 'STRETCH' else 'ROTATE')
    elif key in {'SMOOTH', 'INFLATE'}:
        bpy.ops.wm.tool_set_by_id(name="builtin.mesh_filter")
        tool = context.workspace.tools.from_space_view3d_mode('SCULPT', create=False)
        if tool is not None:
            tool.operator_properties("sculpt.mesh_filter").type = key
    elif key == 'DIVIDE':
        bpy.ops.sculpt.phantom_divide('INVOKE_DEFAULT', True)
    elif key == 'REMESH':
        bpy.ops.object.voxel_remesh('INVOKE_DEFAULT', True)
    elif key == 'QUAD':
        bpy.ops.wm.call_panel(name="VIEW3D_PT_sculpt_quad_remesh", keep_open=True)
    elif key == 'UNION':
        bpy.ops.view3d.phantom_simple_scene_action('EXEC_DEFAULT', True, action='UNION')


def _hit(context, mx, my):
    """What is under the cursor: ('ROW', key), ('TYPES', key), ('CARD', None) for the card
    outside its items, ('BAR', None) for the row outside its buttons, or None."""
    row = _row_layout(context)
    if row is None:
        return None
    if _types["open"]:
        g = _types_layout(context, row)
        if _in(g["rect"], mx, my):
            key = next((k for k, _l, _i, r in g["items"] if _in(r, mx, my)), None)
            return ('TYPES', key) if key else ('CARD', None)
    if _in(row["rect"], mx, my):
        key = next((k for k, r in row["buttons"] if _in(r, mx, my)), None)
        return ('ROW', key) if key else ('BAR', None)
    return None


class SCULPT_OT_phantom_gizmo_row_hover(Operator):
    """Track the cursor over the icons above the gizmo"""
    bl_idname = "sculpt.phantom_gizmo_row_hover"
    bl_label = "Gizmo Icons Hover"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _row_shown(context)

    def invoke(self, context, event):
        hit = _hit(context, event.mouse_region_x, event.mouse_region_y)
        row_key = hit[1] if hit and hit[0] == 'ROW' else None
        types_key = hit[1] if hit and hit[0] == 'TYPES' else None
        if row_key != _row["hover"] or types_key != _types["hover"]:
            _row.update(hover=row_key, since=time.perf_counter())
            _types["hover"] = types_key
            context.area.tag_redraw()
            if row_key is not None:
                bpy.app.timers.register(_row_redraw, first_interval=_sv().TOOLTIP_DELAY + 0.02)
        # Never consume the event: the gizmo and sculpting need it.
        return {'PASS_THROUGH'}


class SCULPT_OT_phantom_gizmo_row_click(Operator):
    """Use the icons above the gizmo, or the Transform Types card"""
    bl_idname = "sculpt.phantom_gizmo_row_click"
    bl_label = "Gizmo Icons"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _row_shown(context)

    def invoke(self, context, event):
        hit = _hit(context, event.mouse_region_x, event.mouse_region_y)
        if hit is None:
            if _types["open"]:
                # A click elsewhere puts the card away (and does nothing else).
                _types.update(open=False, hover=None, pressed=None)
                context.area.tag_redraw()
                return {'FINISHED'}
            return {'PASS_THROUGH'}
        self.hit = hit
        if hit[1] is None:
            # On the row or the card but not on a button: nothing starts here.
            return {'FINISHED'}
        (_row if hit[0] == 'ROW' else _types)["pressed"] = hit[1]
        context.area.tag_redraw()
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            _row["pressed"] = _types["pressed"] = None
            # No tooltip right after a click: not until the cursor moves to another button.
            _row["since"] = math.inf
            over = _hit(context, event.mouse_region_x, event.mouse_region_y) == self.hit
            if over:
                _play_click()
                try:
                    if self.hit[0] == 'ROW':
                        _row_act(context, self.hit[1])
                    else:
                        _types_act(context, self.hit[1])
                except RuntimeError as ex:
                    self.report({'WARNING'}, str(ex).replace("Error: ", "").strip())
            _redraw(context)
            return {'FINISHED'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            _row["pressed"] = _types["pressed"] = None
            _redraw(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


class SCULPT_OT_phantom_gizmo_types_close(Operator):
    """Put the Transform Types card away"""
    bl_idname = "sculpt.phantom_gizmo_types_close"
    bl_label = "Close Transform Types"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _types["open"]

    def invoke(self, context, event):
        _types.update(open=False, hover=None, pressed=None)
        _redraw(context)
        return {'FINISHED'}


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
    SCULPT_OT_phantom_gizmo_row_hover,
    SCULPT_OT_phantom_gizmo_row_click,
    SCULPT_OT_phantom_gizmo_types_close,
)
_row_handle = None
_addon_keymaps = []


def register():
    global _draw_handle, _row_handle
    _load_settings()
    _push_gizmo_options()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.utils.register_tool(PHANTOM_TL_transpose, after={"builtin.transform"})
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_PIXEL')
    _row_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_row, (), 'WINDOW', 'POST_PIXEL')
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Sculpt", space_type='EMPTY')
        for idname, key, value in (
                (SCULPT_OT_phantom_gizmo_row_click.bl_idname, 'LEFTMOUSE', 'PRESS'),
                (SCULPT_OT_phantom_gizmo_row_hover.bl_idname, 'MOUSEMOVE', 'ANY'),
                (SCULPT_OT_phantom_gizmo_types_close.bl_idname, 'ESC', 'PRESS')):
            _addon_keymaps.append((km, km.keymap_items.new(idname, key, value, any=True)))
        # Alt-drag on the gizmo moves only its pivot (see `init_transform` in C). Blender's gizmo
        # keymaps leave Alt out; the transform gizmo uses this one.
        km = kc.keymaps.new(name="Generic Gizmo Maybe Drag", space_type='EMPTY',
                            region_type='WINDOW')
        _addon_keymaps.append((km, km.keymap_items.new("gizmogroup.gizmo_tweak", 'LEFTMOUSE',
                                                       'CLICK_DRAG', alt=True)))
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post, bpy.app.handlers.load_post):
        handlers.append(_forget_line)


def unregister():
    global _draw_handle, _row_handle
    for km, kmi in _addon_keymaps:
        km.keymap_items.remove(kmi)
    _addon_keymaps.clear()
    if _row_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_row_handle, 'WINDOW')
        _row_handle = None
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
