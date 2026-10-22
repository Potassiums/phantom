# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: the Garment workspace (Marvelous Designer-style garment making).

The workspace has two 3D views side by side:

- on the left, the *pattern view*: looking straight down (rotation locked) at the flat pattern
  pieces, where they are drawn and sewn;
- on the right, the 3D view with the avatar and the simulated garment.

Tools (in the views' tool headers and the Garment sidebar tab):

- Rectangle / Ellipse: click-drag in the pattern view to draw a piece (click once for a default size).
- Polygon: click to place corners, Ctrl-click (or click-drag) to place curve points; click the first
  point or press Enter to close the piece. Backspace removes the last point.
- Edit Points: drag a piece's points; Ctrl-click its outline to add a curve point; right-click a
  point to switch it between corner and curve; X or Delete removes the point under the cursor.
- Sew (segment sewing): click a segment of one piece's outline, then a segment of another. The
  ends you click near are sewn together, so click near matching ends (both bottoms, say).
- Free Sew: click where a seam starts and where it ends on one outline, then the same on another
  (four clicks), to sew just part of a segment.
- Elastic: click a segment to make it pull in to a share of its length (green), gathering the
  fabric like a waistband; click it again to undo.
- Pin: drag a box (or click) over a piece to pin it in place (red); right-click to unpin.
- Arrange: blue arrangement points appear around the avatar (chest, hips, arms, legs); click one
  to place the pieces selected in the pattern view there.

Seams are drawn in matching colors, with a dot at the end they start from.
Space (or the Simulate button) starts and stops the simulation; Make Sculptable bakes it.
The data and simulation are in ``phantom_garment.py``, outline geometry in
``phantom_garment_pattern.py``.
"""

import json
import time

import blf
import bpy
import gpu
import numpy as np
from bpy.app.handlers import persistent
from bpy.props import EnumProperty
from bpy.types import Operator, Panel
from gpu_extras.batch import batch_for_shader
from mathutils import Quaternion, Vector

import phantom_garment as garment
import phantom_garment_pattern as pattern

WORKSPACE_NAME = "Garment"
WORKSPACE_TAG = "phantom_garment"
TOOL_ITEMS = (
    ('SELECT', "Select", "Select and move pattern pieces", 'RESTRICT_SELECT_OFF', 0),
    ('RECTANGLE', "Rectangle", "Draw a rectangular pattern piece (drag in the pattern view)", 'MESH_PLANE', 1),
    ('ELLIPSE', "Ellipse", "Draw an elliptical pattern piece (drag in the pattern view)", 'MESH_CIRCLE', 2),
    ('POLYGON', "Polygon", "Draw a pattern piece point by point: click for corners, Ctrl-click for curves",
     'CURVE_BEZCURVE', 3),
    ('EDIT', "Edit Points", "Move, add and remove the points of a piece's outline", 'VERTEXSEL', 4),
    ('SEW', "Sew", "Sew a segment of one piece to a segment of another (click near matching ends)",
     'AUTOMERGE_OFF', 5),
    ('FREE_SEW', "Free Sew", "Sew part of a segment: click where the seam starts and ends on each piece",
     'AUTOMERGE_ON', 6),
    ('ELASTIC', "Elastic", "Click a segment to make it elastic, gathering the fabric (click again to undo)",
     'FORCE_HARMONIC', 7),
    ('PIN', "Pin", "Drag a box over a piece to pin it in place (right-click to unpin)", 'PINNED', 8),
    ('ARRANGE', "Arrange", "Click an arrangement point on the avatar to place the selected pieces",
     'PIVOT_BOUNDBOX', 9),
)
TOOL_HINTS = {
    'RECTANGLE': "Rectangle: drag in the pattern view to draw a piece, click for 50 x 60 cm  |  Shift: square  |  Esc: done",
    'ELLIPSE': "Ellipse: drag in the pattern view to draw a piece, click for 30 cm  |  Shift: circle  |  Esc: done",
    'POLYGON': "Polygon: click for a corner, Ctrl-click or drag for a curve point  |  click the first point or Enter: close  |  Backspace: undo point  |  Esc: cancel",
    'EDIT': "Edit Points: drag a point  |  Ctrl-click the outline: add a curve point  |  right-click: corner / curve  |  X: delete point  |  Esc: done",
    'SEW': "Sew: click a segment near one end, then the matching end of another segment  |  Esc: done",
    'FREE_SEW': "Free Sew: click the seam's start and end on one outline, then on the other  |  Esc: cancel",
    'ELASTIC': "Elastic: click a segment to make it elastic, click again to undo  |  Esc: done",
    'PIN': "Pin: drag a box over a piece, or click, to pin it  |  right-click: unpin  |  Esc: done",
    'ARRANGE': "Arrange: select pieces in the pattern view, then click a blue point on the avatar  |  Esc: done",
}
PATTERN_TOOLS = ('SELECT', 'RECTANGLE', 'ELLIPSE', 'POLYGON', 'EDIT', 'SEW', 'FREE_SEW', 'ELASTIC', 'PIN')
SLOT_LABELS = {key: name for key, name, _desc in garment.ARRANGEMENT_ITEMS if key != 'NONE'}
# Unscaled pixels.
PICK_DISTANCE = 10
POINT_RADIUS = 9
SNAP = 0.01
# Clicks this close to a segment's end (as a share of its length) count as on the corner.
CORNER_SNAP = 0.03
SEAM_COLORS = (
    (1.0, 0.55, 0.25, 1.0), (0.35, 0.85, 1.0, 1.0), (0.75, 1.0, 0.35, 1.0), (1.0, 0.4, 0.7, 1.0),
    (1.0, 0.85, 0.3, 1.0), (0.65, 0.5, 1.0, 1.0),
)
OUTLINE_COLOR = (0.08, 0.08, 0.1, 1.0)
SELECTED_OUTLINE_COLOR = (1.0, 0.62, 0.2, 1.0)
HOVER_COLOR = (1.0, 0.95, 0.35, 1.0)
ELASTIC_COLOR = (0.3, 0.95, 0.45, 1.0)
PIN_COLOR = (1.0, 0.22, 0.2, 1.0)
POINT_COLOR = (0.2, 0.55, 1.0, 1.0)
CORNER_POINT_COLOR = (1.0, 1.0, 1.0, 1.0)
CURVE_POINT_COLOR = (1.0, 0.35, 0.3, 1.0)
WATCH_INTERVAL = 0.25
# While dragging a point, rebuild the piece's mesh at most this often (seconds).
EDIT_REBUILD_INTERVAL = 0.06

# The running tool's in-progress state.
_state = {
    "running": False, "rect": None, "hover": None, "first": None, "slot": None,
    # Polygon: board points so far [(x, y, curve)], and where the mouse is.
    "poly": [], "poly_mouse": None, "poly_press": None,
    # Edit Points: the point under the cursor (piece name, index) and the drag in progress.
    "point": None, "drag": None,
    # Free Sew: picks so far [(piece name, segment, along)].
    "free": [],
}
_handles = []
_creating = {"value": False}
# Sidebars already switched to the Garment tab (so the user's later choice of tab sticks).
_tabbed = set()


# -----------------------------------------------------------------------------
# Helpers

def is_garment_workspace(workspace):
    return workspace is not None and bool(workspace.get(WORKSPACE_TAG))


def is_pattern_view(space):
    """The pattern view is the Garment workspace's 3D view with its rotation locked."""
    return (space is not None and space.type == 'VIEW_3D' and space.region_3d is not None and
            space.region_3d.lock_rotation)


def _ui_scale():
    return bpy.context.preferences.system.ui_scale


def _redraw(window=None):
    windows = [window] if window else bpy.context.window_manager.windows
    for win in windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _view_under_mouse(window, x, y):
    """(area, window region) of the 3D view under the mouse, or (area, None) over its header
    or sidebars, or (None, None)."""
    for area in window.screen.areas:
        if area.type != 'VIEW_3D':
            continue
        if not (area.x <= x < area.x + area.width and area.y <= y < area.y + area.height):
            continue
        main = None
        for region in area.regions:
            if not (region.x <= x < region.x + region.width and region.y <= y < region.y + region.height):
                continue
            if region.type == 'WINDOW':
                main = region
            elif region.width > 1 and region.height > 1:
                return area, None
        return area, main
    return None, None


def _project(region, rv3d, points):
    """World points (N x 3) to region pixels (N x 2)."""
    mat = np.array(rv3d.perspective_matrix)
    h = np.c_[points, np.ones(len(points))] @ mat.T
    ndc = h[:, :2] / h[:, 3:4]
    return np.c_[(ndc[:, 0] + 1.0) * 0.5 * region.width, (ndc[:, 1] + 1.0) * 0.5 * region.height]


def _region_to_board(region, rv3d, mx, my):
    """The point on the pattern board (Z = 0) under region pixel (mx, my)."""
    from bpy_extras import view3d_utils
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (mx, my))
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, (mx, my))
    if abs(direction.z) < 1e-8:
        return Vector((origin.x, origin.y, 0.0))
    return origin + direction * (-origin.z / direction.z)


def _visible_pieces(scene):
    return [ob for ob in garment.pattern_pieces(scene) if ob.visible_get()]


def _pick_segment(scene, region, rv3d, mx, my, only=None):
    """The outline segment nearest the mouse: (piece, segment index, position 0-1 along it).

    ``only`` = (piece, segments) restricts the search to those segments (with no distance limit).
    """
    best, best_dist = None, (np.inf if only else PICK_DISTANCE * _ui_scale())
    mouse = np.array((mx, my), dtype=np.float64)
    for ob in _visible_pieces(scene):
        if only and ob != only[0]:
            continue
        for index, poly in enumerate(garment.outline_world(ob)):
            if only and index not in only[1]:
                continue
            dist, along = pattern.project(_project(region, rv3d, poly), mouse)
            if dist < best_dist:
                best, best_dist = (ob, index, along), dist
    return best


def _pick_point(scene, region, rv3d, mx, my):
    """The outline point nearest the mouse: (piece, index)."""
    best, best_dist = None, PICK_DISTANCE * _ui_scale()
    for ob in _visible_pieces(scene):
        points = garment.piece_points(ob)
        if not points:
            continue
        world = garment.to_world(ob, np.array([(x, y, 0.0) for x, y, _c in points]))
        pixels = _project(region, rv3d, world)
        dist = np.hypot(pixels[:, 0] - mx, pixels[:, 1] - my)
        i = int(dist.argmin())
        if dist[i] < best_dist:
            best, best_dist = (ob, i), dist[i]
    return best


def _pick_slot(scene, region, rv3d, mx, my):
    avatar = scene.phantom_garment.avatar
    if avatar is None:
        return None
    best, best_dist = None, POINT_RADIUS * 2.0 * _ui_scale()
    for slot in SLOT_LABELS:
        point = garment.arrangement_point(avatar, slot)
        if point is None:
            continue
        pixel = _project(region, rv3d, np.array([point]))[0]
        dist = float(np.hypot(pixel[0] - mx, pixel[1] - my))
        if dist < best_dist:
            best, best_dist = slot, dist
    return best


def _stretch(ob, segment, start=0.0, end=1.0):
    """World polyline of a stretch of a piece's segment, from ``start`` to ``end``."""
    polys = garment.outline_world(ob)
    if segment >= len(polys):
        return np.zeros((0, 3))
    return pattern.sub_polyline(polys[segment], start, end)


def _local_xy(ob, world):
    p = ob.matrix_world.inverted() @ Vector((world[0], world[1], 0.0))
    return p.x, p.y


# -----------------------------------------------------------------------------
# The Garment workspace

def _layer_collection_index(view_layer, name):
    """Index of the named collection as ``object.hide_collection`` counts them."""
    index = 0
    stack = [view_layer.layer_collection]
    while stack:
        lc = stack.pop(0)
        if lc.collection.name == name:
            return index
        index += 1
        stack[0:0] = list(lc.children)
    return -1


def _setup_view(window, area, pattern):
    space = area.spaces.active
    rv3d = space.region_3d
    region = next(r for r in area.regions if r.type == 'WINDOW')
    space.show_region_ui = not pattern
    space.overlay.show_cursor = False
    if pattern:
        rv3d.view_perspective = 'ORTHO'
        rv3d.view_rotation = Quaternion((1.0, 0.0, 0.0, 0.0))
        rv3d.view_location = garment.BOARD_ORIGIN
        rv3d.view_distance = 1.6
        rv3d.lock_rotation = True
        space.show_region_toolbar = False
        space.overlay.show_object_origins = False
        space.shading.type = 'SOLID'
        space.shading.light = 'FLAT'
        space.shading.color_type = 'MATERIAL'
        space.shading.background_type = 'VIEWPORT'
        space.shading.background_color = (0.035, 0.032, 0.042)
    else:
        rv3d.view_perspective = 'PERSP'
        rv3d.view_rotation = Quaternion((0.766, 0.6, 0.15, 0.19)).normalized()
        rv3d.view_location = (0.0, 0.0, 1.0)
        rv3d.view_distance = 3.4
        space.shading.color_type = 'MATERIAL'
    # Each view shows only its own objects (local collections).
    scene = window.scene
    view_layer = window.view_layer
    for name in (garment.PATTERN_COLLECTION, garment.GARMENT_COLLECTION, garment.AVATAR_COLLECTION):
        garment.ensure_collection(scene, name)
    space.use_local_collections = True
    hide = ((garment.GARMENT_COLLECTION, garment.AVATAR_COLLECTION) if pattern
            else (garment.PATTERN_COLLECTION,))
    with bpy.context.temp_override(window=window, area=area, region=region):
        for name in hide:
            index = _layer_collection_index(view_layer, name)
            if index > 0:
                bpy.ops.object.hide_collection(collection_index=index, toggle=True)


def _setup_screen(window):
    screen = window.screen
    views = [a for a in screen.areas if a.type == 'VIEW_3D']
    if not views:
        return
    area = max(views, key=lambda a: a.width * a.height)
    region = next(r for r in area.regions if r.type == 'WINDOW')
    with bpy.context.temp_override(window=window, screen=screen, area=area, region=region):
        bpy.ops.screen.area_split(direction='VERTICAL', factor=0.45)
    views = sorted((a for a in screen.areas if a.type == 'VIEW_3D'), key=lambda a: a.x)
    left = [a for a in views if a.y == area.y and a.height == area.height]
    if len(left) < 2:
        left = views
    _setup_view(window, left[0], pattern=True)
    _setup_view(window, left[-1], pattern=False)


def create_workspace(window, activate=False):
    """Add the Garment workspace (made from Layout) to the window's file.

    Workspace changes take effect on the next event-loop pass, so this runs as a series of timer
    steps.
    """
    if _creating["value"] or garment_workspace() is not None:
        return
    _creating["value"] = True
    steps = {"n": 0, "original": window.workspace}
    source = bpy.data.workspaces.get("Layout") or window.workspace

    def step():
        try:
            n = steps["n"]
            steps["n"] += 1
            if n == 0:
                if window.workspace != source:
                    window.workspace = source
                return 0.05
            if n == 1:
                with bpy.context.temp_override(window=window):
                    bpy.ops.workspace.duplicate()
                return 0.05
            if n == 2:
                ws = window.workspace
                if ws == source or is_garment_workspace(ws):
                    _creating["value"] = False
                    return None
                ws.name = WORKSPACE_NAME
                ws[WORKSPACE_TAG] = True
                ws.object_mode = 'OBJECT'
                _setup_screen(window)
                return 0.05
            if not activate and steps["original"] is not None:
                window.workspace = steps["original"]
        except (ReferenceError, RuntimeError) as ex:
            print("Phantom: could not create the Garment workspace:", ex)
        _creating["value"] = False
        return None

    bpy.app.timers.register(step, first_interval=0.0)


def garment_workspace():
    return next((ws for ws in bpy.data.workspaces if is_garment_workspace(ws)), None)


class PHANTOM_OT_garment_workspace_add(Operator):
    """Add the Garment workspace, for making clothes Marvelous Designer-style"""
    bl_idname = "phantom.garment_workspace_add"
    bl_label = "Garment"

    def execute(self, context):
        ws = garment_workspace()
        if ws is not None:
            context.window.workspace = ws
        else:
            create_workspace(context.window, activate=True)
        garment.play_sound("mode_switch")
        return {'FINISHED'}


def draw_workspace_menu(self, _context):
    self.layout.separator()
    self.layout.operator(PHANTOM_OT_garment_workspace_add.bl_idname, icon='MOD_CLOTH')


@persistent
def _on_load(_):
    bpy.app.timers.register(_ensure_workspace, first_interval=0.3)


def _ensure_workspace():
    # New files (the startup file) get the Garment tab; saved files keep the tabs they were saved with.
    wm = bpy.context.window_manager
    if bpy.data.filepath or not wm or not wm.windows or garment_workspace() is not None:
        return None
    create_workspace(wm.windows[0])
    return None


# -----------------------------------------------------------------------------
# Tools

def _set_hint(context, text):
    ws = context.workspace
    if ws is not None:
        ws.status_text_set(text)


def _clear_state():
    _state.update(rect=None, hover=None, first=None, slot=None, poly=[], poly_mouse=None,
                  poly_press=None, point=None, drag=None, free=[])


def _snap(point, step=SNAP):
    return Vector((round(point.x / step) * step, round(point.y / step) * step, 0.0))


def _points_json(points):
    return json.dumps([[round(x, 6), round(y, 6), bool(c)] for x, y, c in points])


class PHANTOM_OT_garment_tool(Operator):
    """Choose a garment tool"""
    bl_idname = "phantom.garment_tool"
    bl_label = "Garment Tool"

    tool: EnumProperty(items=TOOL_ITEMS)

    @classmethod
    def description(cls, _context, properties):
        return next(item[2] for item in TOOL_ITEMS if item[0] == properties.tool)

    def invoke(self, context, _event):
        wm = context.window_manager
        tool = 'SELECT' if wm.phantom_garment_tool == self.tool else self.tool
        wm.phantom_garment_tool = tool
        _clear_state()
        garment.play_sound("click")
        _set_hint(context, TOOL_HINTS.get(tool))
        _redraw()
        if tool == 'SELECT' or _state["running"]:
            return {'FINISHED'}
        _state["running"] = True
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        self._cancel_drag(context)
        _clear_state()
        _state["running"] = False
        context.window_manager.phantom_garment_tool = 'SELECT'
        context.window.cursor_modal_restore()
        _set_hint(context, None)
        _redraw()

    def _busy(self):
        return bool(_state["rect"] or _state["first"] or _state["poly"] or _state["drag"] or _state["free"])

    def _cancel(self, context):
        self._cancel_drag(context)
        _state.update(rect=None, first=None, poly=[], poly_press=None, free=[])
        garment.play_sound("cancel")
        _redraw()

    def _cancel_drag(self, context):
        drag = _state["drag"]
        if drag:
            ob = bpy.data.objects.get(drag["piece"])
            if ob is not None:
                garment.set_piece_points(ob, drag["original"])
                garment.regenerate_piece(ob, context.scene)
            _state["drag"] = None

    def modal(self, context, event):
        wm = context.window_manager
        tool = wm.phantom_garment_tool
        if tool == 'SELECT' or not is_garment_workspace(context.workspace):
            self._finish(context)
            return {'FINISHED', 'PASS_THROUGH'}

        if event.type == 'ESC' and event.value == 'PRESS':
            if self._busy():
                self._cancel(context)
                return {'RUNNING_MODAL'}
            garment.play_sound("toggle_off")
            self._finish(context)
            return {'FINISHED'}

        area, region = _view_under_mouse(context.window, event.mouse_x, event.mouse_y)
        drag = _state["drag"]
        if drag is not None:
            # Keep following the mouse even off the view.
            region, rv3d, pattern_view = drag["region"], drag["rv3d"], True
        elif region is None:
            if _state["rect"] is not None:
                return {'RUNNING_MODAL'}
            if _state["hover"] or _state["slot"] or _state["point"]:
                _state.update(hover=None, slot=None, point=None)
                _redraw()
            context.window.cursor_modal_restore()
            return {'PASS_THROUGH'}
        else:
            space = area.spaces.active
            rv3d = space.region_3d
            pattern_view = is_pattern_view(space)
        mx, my = event.mouse_x - region.x, event.mouse_y - region.y

        if tool == 'ARRANGE':
            return self._arrange(context, event, region, rv3d, pattern_view, mx, my)
        if not pattern_view:
            context.window.cursor_modal_restore()
            return {'PASS_THROUGH'}
        handler = {
            'RECTANGLE': self._box, 'ELLIPSE': self._box, 'PIN': self._box,
            'POLYGON': self._polygon, 'EDIT': self._edit,
            'SEW': self._sew, 'ELASTIC': self._sew, 'FREE_SEW': self._free_sew,
        }.get(tool)
        if handler is None:
            return {'PASS_THROUGH'}
        return handler(context, event, region, rv3d, mx, my, tool)

    # Rectangle, Ellipse and Pin: drag a box.
    def _box(self, context, event, region, rv3d, mx, my, tool):
        context.window.cursor_modal_set('CROSSHAIR')
        point = _snap(_region_to_board(region, rv3d, mx, my))
        if tool == 'PIN' and event.type == 'RIGHTMOUSE' and event.value == 'PRESS':
            bpy.ops.phantom.garment_remove_pins(at=point.xy, use_at=True)
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            _state["rect"] = [point, point.copy()]
            garment.play_sound("grab_start")
            return {'RUNNING_MODAL'}
        rect = _state["rect"]
        if rect is None:
            return {'PASS_THROUGH'}
        if event.type in {'MOUSEMOVE', 'LEFT_SHIFT', 'RIGHT_SHIFT'}:
            if event.shift and tool != 'PIN':
                d = point - rect[0]
                side = max(abs(d.x), abs(d.y))
                point = rect[0] + Vector((side if d.x >= 0 else -side, side if d.y >= 0 else -side, 0.0))
            rect[1] = point
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            a, b = rect
            _state["rect"] = None
            width, height = abs(b.x - a.x), abs(b.y - a.y)
            small = width < 0.03 or height < 0.03
            if tool == 'PIN':
                if small:
                    half = context.scene.phantom_garment.particle_distance
                    a, b = a - Vector((half, half, 0.0)), a + Vector((half, half, 0.0))
                lo = (min(a.x, b.x), min(a.y, b.y))
                hi = (max(a.x, b.x), max(a.y, b.y))
                bpy.ops.phantom.garment_add_pin(lo=lo, hi=hi)
            else:
                if small:
                    width, height = (0.3, 0.3) if tool == 'ELLIPSE' else (0.5, 0.6)
                    center = a
                else:
                    center = (a + b) / 2.0
                bpy.ops.phantom.garment_add_rectangle(width=width, height=height, location=center.xy,
                                                      ellipse=tool == 'ELLIPSE')
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}

    # Polygon: click corners, Ctrl-click or drag for curve points.
    def _polygon(self, context, event, region, rv3d, mx, my, _tool):
        context.window.cursor_modal_set('CROSSHAIR')
        point = _snap(_region_to_board(region, rv3d, mx, my), SNAP / 2.0)
        poly = _state["poly"]

        def near_first():
            if len(poly) < 3:
                return False
            first = _project(region, rv3d, np.array([(poly[0][0], poly[0][1], 0.0)]))[0]
            return np.hypot(first[0] - mx, first[1] - my) < PICK_DISTANCE * _ui_scale()

        def close():
            points = list(_state["poly"])
            _state.update(poly=[], poly_press=None, poly_mouse=None)
            bpy.ops.phantom.garment_add_polygon(points=_points_json(points))
            _redraw(context.window)

        if event.type == 'MOUSEMOVE':
            _state["poly_mouse"] = point
            press = _state["poly_press"]
            if press and np.hypot(press["pixel"][0] - mx, press["pixel"][1] - my) > 6 * _ui_scale():
                press["curve"] = True
            _redraw(context.window)
            return {'RUNNING_MODAL'} if poly else {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if near_first():
                close()
                return {'RUNNING_MODAL'}
            _state["poly_press"] = {"point": point, "pixel": (mx, my), "curve": event.ctrl}
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and _state["poly_press"]:
            press = _state["poly_press"]
            _state["poly_press"] = None
            poly.append((press["point"].x, press["point"].y, press["curve"]))
            garment.play_sound("measure_tick" if press["curve"] else "click")
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS' and len(poly) >= 3:
            close()
            return {'RUNNING_MODAL'}
        if event.type == 'BACK_SPACE' and event.value == 'PRESS' and poly:
            poly.pop()
            garment.play_sound("type_delete")
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'} if poly else {'PASS_THROUGH'}

    # Edit Points.
    def _edit(self, context, event, region, rv3d, mx, my, _tool):
        scene = context.scene
        drag = _state["drag"]
        if drag is not None:
            ob = bpy.data.objects.get(drag["piece"])
            if ob is None:
                _state["drag"] = None
                return {'RUNNING_MODAL'}
            if event.type == 'MOUSEMOVE':
                board = _snap(_region_to_board(region, rv3d, mx, my), SNAP / 2.0)
                x, y = _local_xy(ob, board)
                ob.phantom_garment.points[drag["index"]].co = (x, y)
                now = time.perf_counter()
                if now - drag["rebuilt"] > EDIT_REBUILD_INTERVAL:
                    garment.regenerate_piece(ob, scene)
                    drag["rebuilt"] = now
                _redraw(context.window)
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                _state["drag"] = None
                points = garment.piece_points(ob)
                garment.set_piece_points(ob, drag["original"])
                bpy.ops.phantom.garment_set_points(piece=ob.name, points=_points_json(points))
                garment.play_sound("transform_confirm")
                _redraw(context.window)
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            point = _pick_point(scene, region, rv3d, mx, my)
            hover = None if point else _pick_segment(scene, region, rv3d, mx, my)
            if point != _state["point"] or hover != _state["hover"]:
                _state["point"], _state["hover"] = point, hover
                _redraw(context.window)
            context.window.cursor_modal_set('HAND' if point else ('CROSSHAIR' if hover else 'DEFAULT'))
            return {'PASS_THROUGH'}
        point = _pick_point(scene, region, rv3d, mx, my)
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if point and not event.ctrl:
                ob, index = point
                _state["drag"] = {"piece": ob.name, "index": index, "original": garment.piece_points(ob),
                                  "region": region, "rv3d": rv3d, "rebuilt": 0.0}
                garment.play_sound("grab_start")
                return {'RUNNING_MODAL'}
            hover = _pick_segment(scene, region, rv3d, mx, my)
            if event.ctrl and hover:
                ob = hover[0]
                points = garment.piece_points(ob)
                local = np.array(_local_xy(ob, _region_to_board(region, rv3d, mx, my)))
                spans = pattern.outline_spans(points)
                best = min(spans, key=lambda span: pattern.project(span[1], local)[0])
                points.insert(best[0] + 1, (float(local[0]), float(local[1]), True))
                bpy.ops.phantom.garment_set_points(piece=ob.name, points=_points_json(points))
                garment.play_sound("loop_add")
                _state["point"] = (ob, best[0] + 1)
                _redraw(context.window)
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        if event.type == 'RIGHTMOUSE' and event.value == 'PRESS' and point:
            ob, index = point
            old = garment.piece_points(ob)
            new = list(old)
            x, y, curve = new[index]
            new[index] = (x, y, not curve)
            bpy.ops.phantom.garment_set_points(piece=ob.name, points=_points_json(new), old_points=_points_json(old))
            garment.play_sound("toggle_off" if curve else "toggle_on")
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        if event.type in {'X', 'DEL'} and event.value == 'PRESS' and point:
            ob, index = point
            old = garment.piece_points(ob)
            if len(old) <= 3:
                garment.play_sound("error")
                return {'RUNNING_MODAL'}
            new = old[:index] + old[index + 1:]
            bpy.ops.phantom.garment_set_points(piece=ob.name, points=_points_json(new),
                                               old_points="" if old[index][2] else _points_json(old))
            garment.play_sound("loop_remove")
            _state["point"] = None
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    # Sew (segment) and Elastic.
    def _sew(self, context, event, region, rv3d, mx, my, tool):
        if event.type == 'MOUSEMOVE':
            hover = _pick_segment(context.scene, region, rv3d, mx, my)
            if hover != _state["hover"]:
                _state["hover"] = hover
                _redraw(context.window)
            context.window.cursor_modal_set('CROSSHAIR' if hover else 'DEFAULT')
            return {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            hover = _pick_segment(context.scene, region, rv3d, mx, my)
            if hover is None:
                return {'PASS_THROUGH'}
            ob, index, along = hover
            if tool == 'ELASTIC':
                bpy.ops.phantom.garment_toggle_elastic(piece=ob.name, segment=index)
                _redraw(context.window)
                return {'RUNNING_MODAL'}
            pick = (ob.name, index, along > 0.5)
            first = _state["first"]
            if first is None:
                _state["first"] = pick
                garment.play_sound("measure_tick")
            else:
                _state["first"] = None
                bpy.ops.phantom.garment_add_seam(
                    piece_a=first[0], segment_a=first[1], start_a=float(first[2]), end_a=float(not first[2]),
                    piece_b=pick[0], segment_b=pick[1], start_b=float(pick[2]), end_b=float(not pick[2]))
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        if event.type == 'RIGHTMOUSE' and event.value == 'PRESS' and _state["first"]:
            self._cancel(context)
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    # Free Sew: four clicks.
    def _free_sew(self, context, event, region, rv3d, mx, my, _tool):
        picks = _state["free"]
        only = None
        if len(picks) in (1, 3):
            name, segment, along = picks[-1]
            ob = bpy.data.objects.get(name)
            if ob is not None:
                # A click on a corner belongs to either segment meeting there: let the next click decide.
                count = len(garment.piece_segments(ob))
                segments = {segment}
                if along <= CORNER_SNAP:
                    segments.add((segment - 1) % count)
                elif along >= 1.0 - CORNER_SNAP:
                    segments.add((segment + 1) % count)
                only = (ob, segments)
        if event.type == 'MOUSEMOVE':
            hover = _pick_segment(context.scene, region, rv3d, mx, my, only)
            if hover != _state["hover"]:
                _state["hover"] = hover
                _redraw(context.window)
            context.window.cursor_modal_set('CROSSHAIR' if hover else 'DEFAULT')
            return {'RUNNING_MODAL'} if picks else {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            hover = _pick_segment(context.scene, region, rv3d, mx, my, only)
            if hover is None:
                return {'RUNNING_MODAL'} if picks else {'PASS_THROUGH'}
            ob, index, along = hover
            if len(picks) in (1, 3) and picks[-1][1] != index:
                # The previous click was on the corner where this segment starts or ends.
                name, segment, previous = picks[-1]
                picks[-1] = (name, index, 1.0 if previous <= CORNER_SNAP else 0.0)
            picks.append((ob.name, index, along))
            if len(picks) < 4:
                garment.play_sound("measure_tick", 1.0 + 0.08 * len(picks))
            else:
                a1, a2, b1, b2 = picks
                _state["free"] = []
                bpy.ops.phantom.garment_add_seam(
                    piece_a=a1[0], segment_a=a1[1], start_a=a1[2], end_a=a2[2],
                    piece_b=b1[0], segment_b=b1[1], start_b=b1[2], end_b=b2[2])
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        if event.type == 'RIGHTMOUSE' and event.value == 'PRESS' and picks:
            self._cancel(context)
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'} if picks else {'PASS_THROUGH'}

    def _arrange(self, context, event, region, rv3d, pattern_view, mx, my):
        if pattern_view:
            return {'PASS_THROUGH'}
        if event.type == 'MOUSEMOVE':
            slot = _pick_slot(context.scene, region, rv3d, mx, my)
            if slot != _state["slot"]:
                _state["slot"] = slot
                if slot:
                    garment.play_sound("slide_tick")
                _redraw(context.window)
            context.window.cursor_modal_set('HAND' if slot else 'DEFAULT')
            return {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            slot = _pick_slot(context.scene, region, rv3d, mx, my)
            if slot is None:
                return {'PASS_THROUGH'}
            bpy.ops.phantom.garment_arrange(slot=slot)
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}


# -----------------------------------------------------------------------------
# Drawing

def _polyline(points, color, width):
    if len(points) < 2:
        return
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": [tuple(p) for p in points]})
    shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
    shader.uniform_float("lineWidth", width * _ui_scale())
    shader.uniform_float("color", color)
    batch.draw(shader)


def _points(points, color, size):
    if not len(points):
        return
    shader = gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'POINTS', {"pos": [tuple(p) for p in points]})
    gpu.state.point_size_set(size * _ui_scale())
    shader.uniform_float("color", color)
    batch.draw(shader)


def _lift(points, z=0.002):
    points = np.array(points, dtype=np.float64).reshape(-1, 3)
    points[:, 2] += z
    return points


def _draw_pattern_view(context):
    scene = context.scene
    tool = context.window_manager.phantom_garment_tool
    gpu.state.blend_set('ALPHA')
    selected = set(context.selected_objects)
    pieces = _visible_pieces(scene)
    for ob in pieces:
        color = SELECTED_OUTLINE_COLOR if ob in selected else OUTLINE_COLOR
        for poly in garment.outline_world(ob):
            _polyline(_lift(poly), color, 2.0)
    for elastic in garment.valid_elastics(scene):
        _polyline(_lift(_stretch(elastic.piece, elastic.segment), 0.003), ELASTIC_COLOR, 6.0)
    for i, seam in enumerate(garment.valid_seams(scene)):
        color = SEAM_COLORS[i % len(SEAM_COLORS)]
        for ob, segment, start, end in garment.seam_sides(seam):
            points = _lift(_stretch(ob, segment, start, end), 0.003)
            _polyline(points, color, 4.0)
            _points(points[:1], color, 9.0)
    for ob in pieces:
        pins = ob.phantom_garment.pins
        if not len(pins):
            continue
        for pin in pins:
            corners = [(pin.lo[0], pin.lo[1], 0.0), (pin.hi[0], pin.lo[1], 0.0), (pin.hi[0], pin.hi[1], 0.0),
                       (pin.lo[0], pin.hi[1], 0.0), (pin.lo[0], pin.lo[1], 0.0)]
            _polyline(_lift(garment.to_world(ob, np.array(corners)), 0.003), (1.0, 0.25, 0.2, 0.6), 1.0)
        mask = garment.pinned_vertices(ob)
        if mask.any():
            _points(_lift(garment.piece_world_points(ob)[mask], 0.004), PIN_COLOR, 4.0)

    if tool == 'EDIT':
        for ob in pieces:
            points = garment.piece_points(ob)
            world = garment.to_world(ob, np.array([(x, y, 0.0) for x, y, _c in points]))
            corners = [p for p, q in zip(world, points) if not q[2]]
            curves = [p for p, q in zip(world, points) if q[2]]
            _points(_lift(corners, 0.005), (0.0, 0.0, 0.0, 0.8), 10.0)
            _points(_lift(corners, 0.005), CORNER_POINT_COLOR, 7.0)
            _points(_lift(curves, 0.005), CURVE_POINT_COLOR, 7.0)
        point = _state["point"]
        if point and point[0].name in bpy.data.objects:
            ob, index = point
            points = garment.piece_points(ob)
            if index < len(points):
                x, y, _c = points[index]
                _points(_lift(garment.to_world(ob, np.array([(x, y, 0.0)])), 0.006), HOVER_COLOR, 12.0)

    hover = _state["hover"]
    hover_valid = hover and hover[0].name in bpy.data.objects
    if tool in {'SEW', 'ELASTIC'} and hover_valid:
        ob, index, along = hover
        points = _lift(_stretch(ob, index, *((1.0, 0.0) if along > 0.5 else (0.0, 1.0))), 0.004)
        _polyline(points, ELASTIC_COLOR if tool == 'ELASTIC' else HOVER_COLOR, 5.0)
        if tool == 'SEW':
            _points(points[:1], HOVER_COLOR, 11.0)
            first = _state["first"]
            if first and first[0] in bpy.data.objects:
                start = _lift(_stretch(bpy.data.objects[first[0]], first[1], *((1.0, 0.0) if first[2] else (0.0, 1.0))), 0.004)
                _polyline((start[0], points[0]), (1.0, 1.0, 1.0, 0.8), 1.5)
                _polyline((start[-1], points[-1]), (1.0, 1.0, 1.0, 0.4), 1.5)
    elif tool == 'EDIT' and hover_valid and _state["point"] is None:
        ob, index, along = hover
        _points(_lift([pattern.point_at(garment.outline_world(ob)[index], along)], 0.006), (1.0, 1.0, 1.0, 0.5), 8.0)
    first = _state["first"]
    if tool == 'SEW' and first and first[0] in bpy.data.objects:
        points = _lift(_stretch(bpy.data.objects[first[0]], first[1], *((1.0, 0.0) if first[2] else (0.0, 1.0))), 0.004)
        _polyline(points, HOVER_COLOR, 5.0)
        _points(points[:1], HOVER_COLOR, 11.0)

    if tool == 'FREE_SEW':
        picks = [p for p in _state["free"] if p[0] in bpy.data.objects]
        marks = []
        for k in (0, 2):
            if len(picks) > k:
                ob = bpy.data.objects[picks[k][0]]
                end = picks[k + 1][2] if len(picks) > k + 1 else (
                    hover[2] if hover_valid and hover[0] == ob and hover[1] == picks[k][1] else None)
                if end is not None:
                    stretch = _lift(_stretch(ob, picks[k][1], picks[k][2], end), 0.004)
                    _polyline(stretch, HOVER_COLOR, 5.0)
                    _points(stretch[:1], HOVER_COLOR, 11.0)
        for name, index, along in picks:
            marks.append(pattern.point_at(garment.outline_world(bpy.data.objects[name])[index], along))
        if hover_valid:
            marks.append(pattern.point_at(garment.outline_world(hover[0])[hover[1]], hover[2]))
        _points(_lift(marks, 0.006), HOVER_COLOR, 9.0)

    rect = _state["rect"]
    if rect:
        a, b = rect
        if tool == 'ELLIPSE':
            center, size = (a + b) / 2.0, b - a
            shape = pattern.outline_segments(pattern.ellipse(abs(size.x), abs(size.y)))[0]
            _polyline(_lift(np.c_[shape + np.array(center.xy), np.zeros(len(shape))], 0.004), HOVER_COLOR, 2.0)
        else:
            corners = [(a.x, a.y, 0.0), (b.x, a.y, 0.0), (b.x, b.y, 0.0), (a.x, b.y, 0.0), (a.x, a.y, 0.0)]
            _polyline(_lift(corners, 0.004), PIN_COLOR if tool == 'PIN' else HOVER_COLOR, 2.0)

    poly = _state["poly"]
    if tool == 'POLYGON' and poly:
        preview = list(poly)
        mouse = _state["poly_mouse"]
        press = _state["poly_press"]
        if press:
            preview.append((press["point"].x, press["point"].y, press["curve"]))
        elif mouse is not None:
            preview.append((mouse.x, mouse.y, False))
        if len(preview) >= 2:
            segments = pattern.outline_segments(preview)
            shape = np.vstack(segments)
            line = np.c_[shape, np.zeros(len(shape))]
            _polyline(_lift(line, 0.004), HOVER_COLOR, 2.0)
        corners = [(x, y, 0.0) for x, y, c in poly if not c]
        curves = [(x, y, 0.0) for x, y, c in poly if c]
        _points(_lift(corners, 0.005), CORNER_POINT_COLOR, 8.0)
        _points(_lift(curves, 0.005), CURVE_POINT_COLOR, 8.0)
        _points(_lift([(poly[0][0], poly[0][1], 0.0)], 0.006), HOVER_COLOR, 11.0)
    gpu.state.blend_set('NONE')


def _draw_3d_view(context):
    gpu.state.blend_set('ALPHA')
    pinned = garment.garment_pinned_indices()
    ob = bpy.data.objects.get(garment.GARMENT_OBJECT)
    if pinned and ob is not None and ob.visible_get():
        mesh = ob.evaluated_get(context.evaluated_depsgraph_get()).data
        if len(mesh.vertices) == len(ob.data.vertices):
            mat = ob.matrix_world
            _points([mat @ mesh.vertices[i].co for i in pinned], PIN_COLOR, 4.0)
    avatar = context.scene.phantom_garment.avatar
    if context.window_manager.phantom_garment_tool == 'ARRANGE' and avatar is not None:
        gpu.state.depth_test_set('NONE')
        for slot in SLOT_LABELS:
            point = garment.arrangement_point(avatar, slot)
            if point is None:
                continue
            hovered = slot == _state["slot"]
            _points([point], (1.0, 1.0, 1.0, 0.9), (POINT_RADIUS + 3) * 2 if hovered else POINT_RADIUS * 2)
            _points([point], HOVER_COLOR if hovered else POINT_COLOR,
                    POINT_RADIUS * 2 if hovered else (POINT_RADIUS - 2) * 2)
    gpu.state.blend_set('NONE')


def _draw_view():
    context = bpy.context
    if not is_garment_workspace(context.workspace):
        return
    if is_pattern_view(context.space_data):
        _draw_pattern_view(context)
    else:
        _draw_3d_view(context)


def _text(x, y, text, size=12.0, color=(1.0, 1.0, 1.0, 0.85)):
    font = 0
    blf.size(font, size * _ui_scale())
    width, height = blf.dimensions(font, text)
    blf.color(font, *color)
    blf.position(font, x - width / 2.0, y - height / 2.0, 0)
    blf.draw(font, text)


def _draw_labels():
    context = bpy.context
    if not is_garment_workspace(context.workspace):
        return
    region, rv3d = context.region, context.region_data
    if rv3d is None:
        return
    if is_pattern_view(context.space_data):
        for ob in _visible_pieces(context.scene):
            props = ob.phantom_garment
            center = _project(region, rv3d, np.array([ob.matrix_world.translation]))[0]
            _text(center[0], center[1] + 8 * _ui_scale(), ob.name, 12.0, (0.05, 0.05, 0.08, 0.9))
            details = garment.FABRICS[props.fabric]["name"]
            if props.arrangement != 'NONE':
                details = SLOT_LABELS.get(props.arrangement, "") + "  ·  " + details
            _text(center[0], center[1] - 8 * _ui_scale(), details, 10.0, (0.05, 0.05, 0.08, 0.6))
        rect = _state["rect"]
        tool = context.window_manager.phantom_garment_tool
        if rect and tool != 'PIN':
            a, b = rect
            mid = _project(region, rv3d, np.array([(a + b) / 2.0]))[0]
            _text(mid[0], mid[1], "%.0f x %.0f cm" % (abs(b.x - a.x) * 100.0, abs(b.y - a.y) * 100.0), 14.0)
    elif context.window_manager.phantom_garment_tool == 'ARRANGE':
        avatar = context.scene.phantom_garment.avatar
        slot = _state["slot"]
        if avatar is not None and slot:
            point = garment.arrangement_point(avatar, slot)
            if point is not None:
                pixel = _project(region, rv3d, np.array([point]))[0]
                _text(pixel[0], pixel[1] + 22 * _ui_scale(), SLOT_LABELS[slot], 13.0)


# -----------------------------------------------------------------------------
# Header and sidebar

def _tool_buttons(layout, context, tools, text=False):
    wm = context.window_manager
    row = layout.row(align=True)
    groups = ({'SELECT'}, {'RECTANGLE', 'ELLIPSE', 'POLYGON'}, {'EDIT'}, {'SEW', 'FREE_SEW', 'ELASTIC'}, {'PIN'})
    last_group = None
    for key, name, _desc, icon, _n in TOOL_ITEMS:
        if key not in tools:
            continue
        group = next((i for i, g in enumerate(groups) if key in g), None)
        if last_group is not None and group != last_group:
            row.separator(factor=0.6)
        last_group = group
        row.operator(PHANTOM_OT_garment_tool.bl_idname, text=name if text else "", icon=icon,
                     depress=wm.phantom_garment_tool == key).tool = key


def _simulate_button(layout, context, text=True):
    playing = bool(context.screen and context.screen.is_animation_playing)
    layout.operator(garment.PHANTOM_OT_garment_simulate.bl_idname,
                    text=("Stop" if playing else "Simulate") if text else "",
                    icon='PAUSE' if playing else 'PLAY', depress=playing)


def draw_header(self, context):
    if not is_garment_workspace(context.workspace):
        return
    layout = self.layout
    layout.separator()
    if is_pattern_view(context.space_data):
        _tool_buttons(layout, context, PATTERN_TOOLS)
    else:
        _tool_buttons(layout, context, ('ARRANGE',), text=True)
        layout.separator()
        _simulate_button(layout, context)


class GarmentPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Garment"

    @classmethod
    def poll(cls, context):
        return is_garment_workspace(context.workspace)


class VIEW3D_PT_phantom_garment_avatar(GarmentPanel, Panel):
    bl_label = "Avatar"

    def draw(self, context):
        settings = context.scene.phantom_garment
        layout = self.layout
        layout.prop(settings, "avatar", text="")
        if settings.avatar is None:
            layout.operator(garment.PHANTOM_OT_garment_add_mannequin.bl_idname, icon='OUTLINER_OB_ARMATURE')
        layout.prop(settings, "skin_offset")


class VIEW3D_PT_phantom_garment_patterns(GarmentPanel, Panel):
    bl_label = "Patterns"

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        _tool_buttons(col, context, PATTERN_TOOLS[:5])
        _tool_buttons(col, context, PATTERN_TOOLS[5:] + ('ARRANGE',))
        pieces = garment.pattern_pieces(context.scene)
        if not pieces:
            layout.label(text="Draw a piece in the pattern view", icon='INFO')
            return
        col = layout.column(align=True)
        for ob in pieces:
            row = col.row(align=True)
            row.label(text=ob.name, icon='MESH_PLANE' if ob != context.active_object else 'RADIOBUT_ON')
            row.prop(ob.phantom_garment, "arrangement", text="")


class VIEW3D_PT_phantom_garment_piece(GarmentPanel, Panel):
    bl_label = "Piece"

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return super().poll(context) and ob is not None and ob.phantom_garment.is_piece

    def draw(self, context):
        ob = context.active_object
        props = ob.phantom_garment
        layout = self.layout
        layout.prop(ob, "name", text="")
        layout.prop(props, "fabric")
        layout.prop(props, "arrangement", text="Place")
        row = layout.row(align=True)
        row.label(text="%d points, %d pins" % (len(props.points), len(props.pins)), icon='PINNED')
        if len(props.pins):
            op = row.operator(garment.PHANTOM_OT_garment_remove_pins.bl_idname, text="", icon='X')
            op.piece = ob.name


def _stretch_label(ob, segment, start, end):
    label = "%s %s" % (ob.name, garment.segment_name(ob, segment, short=True))
    lo, hi = min(start, end), max(start, end)
    if lo > 0.01 or hi < 0.99:
        label += " %d-%d%%" % (round(lo * 100), round(hi * 100))
    return label


class VIEW3D_PT_phantom_garment_sewing(GarmentPanel, Panel):
    bl_label = "Sewing"

    def draw(self, context):
        layout = self.layout
        seams = context.scene.phantom_garment.seams
        if not len(seams):
            layout.label(text="Use Sew or Free Sew to join pieces", icon='INFO')
            return
        col = layout.column(align=True)
        for i, seam in enumerate(seams):
            row = col.row(align=True)
            if seam.piece_a is None or seam.piece_b is None:
                row.label(text="(missing piece)", icon='ERROR')
            else:
                row.label(text=_stretch_label(seam.piece_a, seam.segment_a, seam.start_a, seam.end_a) + "  -  " +
                          _stretch_label(seam.piece_b, seam.segment_b, seam.start_b, seam.end_b))
            row.operator(garment.PHANTOM_OT_garment_flip_seam.bl_idname, text="", icon='ARROW_LEFTRIGHT').index = i
            row.operator(garment.PHANTOM_OT_garment_remove_seam.bl_idname, text="", icon='X').index = i


class VIEW3D_PT_phantom_garment_elastic(GarmentPanel, Panel):
    bl_label = "Elastic"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        elastics = list(garment.valid_elastics(context.scene))
        if not elastics:
            layout.label(text="Use Elastic to gather a segment", icon='INFO')
            return
        col = layout.column(align=True)
        for elastic in elastics:
            row = col.row(align=True)
            row.prop(elastic, "ratio", text="%s %s" % (elastic.piece.name, garment.segment_name(elastic.piece, elastic.segment)))
            op = row.operator(garment.PHANTOM_OT_garment_toggle_elastic.bl_idname, text="", icon='X')
            op.piece, op.segment = elastic.piece.name, elastic.segment


class VIEW3D_PT_phantom_garment_fabric(GarmentPanel, Panel):
    bl_label = "Fabric"

    def draw(self, context):
        settings = context.scene.phantom_garment
        layout = self.layout
        row = layout.row(align=True)
        row.prop(settings, "fabric", text="")
        row.operator(garment.PHANTOM_OT_garment_fabric_to_all.bl_idname, text="", icon='DUPLICATE')
        layout.prop(settings, "particle_distance")
        layout.prop(settings, "sewing_force")


class VIEW3D_PT_phantom_garment_simulation(GarmentPanel, Panel):
    bl_label = "Simulation"

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.scale_y = 1.4
        _simulate_button(col, context)
        row = layout.row(align=True)
        row.operator(garment.PHANTOM_OT_garment_reset.bl_idname, text="Reset", icon='FILE_REFRESH')
        row.operator(garment.PHANTOM_OT_garment_make_sculptable.bl_idname, icon='SCULPTMODE_HLT')


# -----------------------------------------------------------------------------
# Keeping the garment up to date

def _show_garment_tab(window):
    # A sidebar only knows its tabs once it has been drawn, so this can't be done at setup.
    for area in window.screen.areas:
        if area.type != 'VIEW_3D' or area.as_pointer() in _tabbed:
            continue
        for region in area.regions:
            if region.type == 'UI' and region.width > 1:
                try:
                    region.active_panel_category = "Garment"
                except (TypeError, ValueError, AttributeError):
                    continue
                _tabbed.add(area.as_pointer())


def _watch():
    wm = bpy.context.window_manager
    for window in wm.windows if wm else ():
        if not is_garment_workspace(window.workspace):
            continue
        _show_garment_tab(window)
        if window.screen.is_animation_playing or _state["drag"]:
            continue
        scene = window.scene
        garment.maintain(scene)
        if garment.garment_signature(scene) != garment._built["signature"]:
            garment.build_garment(scene)
            _redraw(window)
    return WATCH_INTERVAL


def _stop_playback():
    wm = bpy.context.window_manager
    for window in wm.windows if wm else ():
        if is_garment_workspace(window.workspace) and window.screen.is_animation_playing:
            with bpy.context.temp_override(window=window):
                bpy.ops.screen.animation_cancel(restore_frame=False)
            garment.play_sound("toggle_off")
    return None


@persistent
def _on_frame_change(scene, _depsgraph=None):
    if scene.frame_current >= garment.SIM_FRAMES and bpy.data.objects.get(garment.GARMENT_OBJECT):
        if not bpy.app.timers.is_registered(_stop_playback):
            bpy.app.timers.register(_stop_playback, first_interval=0.0)


# -----------------------------------------------------------------------------
# Registration

classes = (
    PHANTOM_OT_garment_workspace_add,
    PHANTOM_OT_garment_tool,
    VIEW3D_PT_phantom_garment_avatar,
    VIEW3D_PT_phantom_garment_patterns,
    VIEW3D_PT_phantom_garment_piece,
    VIEW3D_PT_phantom_garment_sewing,
    VIEW3D_PT_phantom_garment_elastic,
    VIEW3D_PT_phantom_garment_fabric,
    VIEW3D_PT_phantom_garment_simulation,
)
_keymaps = []


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.phantom_garment_tool = EnumProperty(
        name="Garment Tool", items=[item[:3] + (item[4],) for item in TOOL_ITEMS], default='SELECT')
    bpy.types.VIEW3D_HT_tool_header.append(draw_header)
    bpy.types.TOPBAR_MT_workspace_menu.append(draw_workspace_menu)
    _handles.append((bpy.types.SpaceView3D.draw_handler_add(_draw_view, (), 'WINDOW', 'POST_VIEW'), 'WINDOW'))
    _handles.append((bpy.types.SpaceView3D.draw_handler_add(_draw_labels, (), 'WINDOW', 'POST_PIXEL'), 'WINDOW'))
    bpy.app.handlers.load_post.append(_on_load)
    bpy.app.handlers.frame_change_post.append(_on_frame_change)
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="3D View", space_type='VIEW_3D')
        _keymaps.append((km, km.keymap_items.new(garment.PHANTOM_OT_garment_simulate.bl_idname, 'SPACE', 'PRESS')))
    if not bpy.app.background:
        bpy.app.timers.register(_watch, first_interval=WATCH_INTERVAL, persistent=True)
        bpy.app.timers.register(_ensure_workspace, first_interval=0.5)


def unregister():
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()
    bpy.app.handlers.frame_change_post.remove(_on_frame_change)
    bpy.app.handlers.load_post.remove(_on_load)
    for handle, region in _handles:
        bpy.types.SpaceView3D.draw_handler_remove(handle, region)
    _handles.clear()
    bpy.types.TOPBAR_MT_workspace_menu.remove(draw_workspace_menu)
    bpy.types.VIEW3D_HT_tool_header.remove(draw_header)
    del bpy.types.WindowManager.phantom_garment_tool
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
