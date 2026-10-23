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

Drawing and moving points snaps: onto other points, into line with them (smart guides, shown as
cyan lines), or to a 5 mm grid. Shift keeps the line from the previous point at 15 degree steps
(straight, 45 degrees, square); Alt turns snapping off. The length and angle show by the cursor.
Mirror (M, or the header button) works across the board's center line (the middle of the avatar's
silhouette, pink): a piece drawn on one side gets a twin on the other; a Polygon started on the
line is half a piece, finished when it comes back to the line; a box across the line is centered
on it. Edit Points moves the matching point on the other side too (the twin's, or the other half
of a symmetric piece). A ghost cursor shows where the mirrored point goes.
New pieces show in the 3D view too, standing in front of the avatar, until they're placed.
- Edit Points: drag a piece's points; Ctrl-click its outline to add a curve point; right-click a
  point to switch it between corner and curve; X or Delete removes the point under the cursor.
  Curve points show handles (white): drag one to bend the curve there (like a Bezier handle); R
  over the point makes it smooth again.
- Sew (segment sewing): click a segment of one piece's outline, then a segment of another. The
  ends you click near are sewn together, so click near matching ends (both bottoms, say).
- Free Sew: click where a seam starts and where it ends on one outline, then the same on another
  (four clicks), to sew just part of a segment.
- Elastic: click a segment to make it pull in to a share of its length (green), gathering the
  fabric like a waistband; click it again to undo.
- Pin: drag a box (or click) over a piece to pin it in place (red); right-click to unpin.
- Arrange: blue arrangement points appear around the avatar (chest, hips, arms, legs); click one
  to place the pieces selected in the pattern view there.
- Drag: with GPU cloth (NVIDIA Newton), grab the cloth in the 3D view and pull it while it
  simulates, like Marvelous Designer's hand. [ and ] change the brush size.
- Move Pieces: drag a piece in the 3D view; a placed piece slides around the avatar (and up and
  down), an unplaced one moves on the board standing in front of the avatar.

The 3D view previews the sewing: each seam glows in its color on both pieces where they stand,
with threads joining the points that will meet, and so does the seam being made with Sew or Free
Sew, before it's added.

The pattern view shows the avatar's silhouette behind the pieces, at full size, to draft against.

Seams are drawn in matching colors, with a dot at the end they start from.
Space (or the Simulate button) starts and stops the simulation; Make Sculptable bakes it.
The data and simulation are in ``phantom_garment.py``, outline geometry in
``phantom_garment_pattern.py``.
"""

import json
import math
import time

import blf
import bpy
import gpu
import numpy as np
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, IntProperty
from bpy.types import Operator, Panel
from bpy_extras import view3d_utils
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
    ('DRAG', "Drag", "Grab the cloth and pull it while it simulates (GPU cloth)  |  [ ]: brush size",
     'VIEW_PAN', 10),
    ('MOVE', "Move Pieces", "Drag pattern pieces in the 3D view: around the avatar, or on the board in front of it",
     'EMPTY_ARROWS', 11),
)
TOOL_HINTS = {
    'RECTANGLE': "Rectangle: drag in the pattern view to draw a piece, click for 50 x 60 cm  |  Shift: square  |  M: mirror  |  Esc: done",
    'ELLIPSE': "Ellipse: drag in the pattern view to draw a piece, click for 30 cm  |  Shift: circle  |  M: mirror  |  Esc: done",
    'POLYGON': "Polygon: click for a corner, Ctrl-click or drag for a curve point  |  click the first point or Enter: close  |  Backspace: undo point  |  M: mirror (start on the center line for half a piece)  |  Esc: cancel",
    'EDIT': "Edit Points: drag a point  |  drag a curve point's handle to bend the curve, R: smooth again  |  Ctrl-click the outline: add a curve point  |  right-click: corner / curve  |  X: delete point  |  M: mirror  |  Esc: done",
    'SEW': "Sew: click a segment near one end, then the matching end of another segment  |  Esc: done",
    'FREE_SEW': "Free Sew: click the seam's start and end on one outline, then on the other  |  Esc: cancel",
    'ELASTIC': "Elastic: click a segment to make it elastic, click again to undo  |  Esc: done",
    'PIN': "Pin: drag a box over a piece, or click, to pin it  |  right-click: unpin  |  Esc: done",
    'ARRANGE': "Arrange: select pieces in the pattern view, then click a blue point on the avatar  |  Esc: done",
    'MOVE': "Move Pieces: drag a piece in the 3D view, around the avatar or on the board in front of it  |  right-click: cancel  |  Esc: done",
    'DRAG': "Drag: grab the cloth in the 3D view and pull it while it simulates  |  [ ]: brush size  |  Esc: done",
}
PATTERN_TOOLS = ('SELECT', 'RECTANGLE', 'ELLIPSE', 'POLYGON', 'EDIT', 'SEW', 'FREE_SEW', 'ELASTIC', 'PIN')
SLOT_LABELS = {key: name for key, name, _desc in garment.ARRANGEMENT_ITEMS if key != 'NONE'}
# Unscaled pixels.
PICK_DISTANCE = 10
POINT_RADIUS = 9
SNAP = 0.01
# Grid step for drawing and moving points (m), smart guide reach (unscaled pixels), Shift angle step.
POINT_GRID = 0.005
GUIDE_DISTANCE = 10
# How close (unscaled pixels) the mouse has to come to a point to land on it.
SNAP_DISTANCE = 14
# Points this close (m) to each other's mirror image are mirror partners.
MIRROR_TOLERANCE = 0.005
# How close (unscaled pixels) a mirrored polygon's first point (and a half piece's points) has to
# come to the mirror line to land on it.
MIRROR_LINE_REACH = 18
ANGLE_STEP = 15.0
# Unplaced pieces in the 3D view: how far in front of the avatar they stand, and their opacity.
UNPLACED_DISTANCE = 0.45
UNPLACED_ALPHA = 0.55
# Clicks this close to a segment's end (as a share of its length) count as on the corner.
CORNER_SNAP = 0.03
# Threads drawn across a seam in the 3D view's sewing preview.
SEW_PREVIEW_THREADS = 9
# How close (unscaled pixels) the mouse has to be to a piece to move it in the 3D view.
MOVE_PICK_DISTANCE = 14
SEAM_COLORS = (
    (1.0, 0.55, 0.25, 1.0), (0.35, 0.85, 1.0, 1.0), (0.75, 1.0, 0.35, 1.0), (1.0, 0.4, 0.7, 1.0),
    (1.0, 0.85, 0.3, 1.0), (0.65, 0.5, 1.0, 1.0),
)
OUTLINE_COLOR = (0.08, 0.08, 0.1, 1.0)
SELECTED_OUTLINE_COLOR = (1.0, 0.62, 0.2, 1.0)
HOVER_COLOR = (1.0, 0.95, 0.35, 1.0)
GUIDE_COLOR = (0.35, 0.85, 1.0, 0.8)
MIRROR_LINE_COLOR = (1.0, 0.55, 0.85, 0.7)
HANDLE_LINE_COLOR = (1.0, 1.0, 1.0, 0.55)
HANDLE_COLOR = (1.0, 1.0, 1.0, 0.95)
MIRROR_PREVIEW_COLOR = (1.0, 0.55, 0.85, 0.55)
MIRROR_GHOST_COLOR = (1.0, 0.55, 0.85, 0.75)
ELASTIC_COLOR = (0.3, 0.95, 0.45, 1.0)
PIN_COLOR = (1.0, 0.22, 0.2, 1.0)
POINT_COLOR = (0.2, 0.55, 1.0, 1.0)
CORNER_POINT_COLOR = (1.0, 1.0, 1.0, 1.0)
CURVE_POINT_COLOR = (1.0, 0.35, 0.3, 1.0)
BRUSH_COLOR = (1.0, 1.0, 1.0, 0.7)
SILHOUETTE_COLOR = (0.085, 0.08, 0.1, 1.0)
# Drag brush: grab only particles this far (in depth) behind the nearest one under the brush,
# so the back of the garment stays put.
DRAG_DEPTH = 0.06
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
    # Drag: the grab in progress {"anchor": Vector, "region", "rv3d"}, and the mouse for the brush circle.
    "grab": None, "mouse": None, "cursor": None, "handle": None, "handle_piece": None,
    "move": None, "move_hover": None,
    # Snapping feedback: guide lines [(a, b)], the point snapped onto, and the length/angle readout
    # (text, board position). Whether Ctrl is held, for the polygon preview.
    "guides": [], "snapped": None, "readout": None, "ctrl": False,
}
# The avatar's silhouette for the pattern view: (key, GPU batch).
_silhouette = {"key": None, "batch": None}
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


def _handle_piece(context):
    """The piece whose curve handles Edit Points shows: the one last pointed at, else the active one."""
    name = _state["handle_piece"]
    ob = bpy.data.objects.get(name) if name else None
    if ob is None or not ob.phantom_garment.is_piece:
        ob = context.active_object
    return ob if ob is not None and ob.phantom_garment.is_piece and ob in _visible_pieces(context.scene) else None


def _handle_ends(ob):
    """The ends of a piece's curve handles: [(point index, side (+1 towards the next point, -1
    towards the previous), point in world, end in world)]."""
    points = garment.piece_points(ob)
    ends = []
    for i, arm in enumerate(pattern.point_arms(points)):
        if arm is None or not points[i][2]:
            continue
        x, y, _c = points[i]
        for side in (1.0, -1.0):
            ends.append((i, side, (x, y), (x + side * arm[0], y + side * arm[1])))
    if not ends:
        return []
    world = garment.to_world(ob, np.array([(p[0], p[1], 0.0) for _i, _s, p, _e in ends] +
                                          [(e[0], e[1], 0.0) for _i, _s, _p, e in ends]))
    n = len(ends)
    return [(i, side, world[k], world[n + k]) for k, (i, side, _p, _e) in enumerate(ends)]


def _pick_handle(context, region, rv3d, mx, my):
    """The curve handle end nearest the mouse: (piece, point index, side), or None."""
    ob = _handle_piece(context)
    if ob is None:
        return None
    ends = _handle_ends(ob)
    if not ends:
        return None
    pixels = _project(region, rv3d, np.array([e for _i, _s, _p, e in ends]))
    dist = np.hypot(pixels[:, 0] - mx, pixels[:, 1] - my)
    k = int(dist.argmin())
    if dist[k] < PICK_DISTANCE * _ui_scale():
        return ob, ends[k][0], ends[k][1]
    return None


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


def _piece_3d(scene, ob, board, offset=None, shift=(0.0, 0.0)):
    """Where board points (N x 3) of piece ``ob`` are in the 3D view: wrapped around the avatar if
    it's placed (moved by ``offset``, default its own), else on the board standing in front of the
    avatar (moved by ``shift`` on the board)."""
    board = np.asarray(board, dtype=np.float64).reshape(-1, 3)
    avatar = scene.phantom_garment.avatar
    slot = ob.phantom_garment.arrangement
    frame = garment.avatar_frame(avatar) if avatar is not None and slot in SLOT_LABELS else {}
    if slot in frame:
        verts = garment.piece_world_points(ob)
        bounds = (verts.min(axis=0), verts.max(axis=0)) if len(verts) else None
        return garment.wrap_piece(board, frame[slot], ob.phantom_garment.offset if offset is None else offset, bounds)
    origin, right, up = _unplaced_layout(scene)
    origin_board = garment.BOARD_ORIGIN
    return (np.array(origin) + np.outer(board[:, 0] + shift[0] - origin_board.x, np.array(right)) +
            np.outer(board[:, 1] + shift[1] - origin_board.y, np.array(up)))


def _pick_piece_3d(context, region, rv3d, mx, my):
    """The pattern piece under the mouse in the 3D view (as it stands there), or None."""
    scene = context.scene
    best, best_dist = None, MOVE_PICK_DISTANCE * _ui_scale()
    for ob in garment.pattern_pieces(scene):
        if ob.hide_get():
            continue
        verts = garment.piece_world_points(ob)
        if not len(verts):
            continue
        pixels = _project(region, rv3d, _piece_3d(scene, ob, verts))
        dist = np.hypot(pixels[:, 0] - mx, pixels[:, 1] - my)
        if dist.min() < best_dist:
            best, best_dist = ob, float(dist.min())
    return best


def _local_xy(ob, world):
    p = ob.matrix_world.inverted() @ Vector((world[0], world[1], 0.0))
    return p.x, p.y


# -----------------------------------------------------------------------------
# The Garment workspace

def _layer_collection_index(view_layer, name):
    """Index of the named collection as ``object.hide_collection`` counts them: Blender numbers a
    list of siblings first, then goes into each sibling's children in turn (not depth first)."""
    counter = [0]

    def find(siblings):
        for lc in siblings:
            if lc.collection.name == name:
                return counter[0]
            counter[0] += 1
        for lc in siblings:
            found = find(lc.children)
            if found is not None:
                return found
        return None

    found = find([view_layer.layer_collection])
    return -1 if found is None else found


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
    # Each view shows only its own objects (local collections, kept in step by ``_sync_views``).
    space.use_local_collections = True


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
    """Add the Garment workspace, for making clothes Marvelous Designer-style (experimental: expect bugs)"""
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


def _sync_views(window):
    """Show each view only its own objects: the pattern view the pieces, the 3D view the avatar
    and the garment. Collections made after the views were set up start out visible everywhere,
    so this is kept up to date rather than done once."""
    view_layer = window.view_layer
    names = (garment.PATTERN_COLLECTION, garment.GARMENT_COLLECTION, garment.AVATAR_COLLECTION)
    layer_collections = {}
    stack = list(view_layer.layer_collection.children)
    while stack:
        lc = stack.pop()
        if lc.collection.name in names:
            layer_collections[lc.collection.name] = lc
        stack += list(lc.children)
    if not layer_collections:
        return
    for area in window.screen.areas:
        space = area.spaces.active
        if area.type != 'VIEW_3D' or not space.use_local_collections:
            continue
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        if region is None:
            continue
        pattern_view = is_pattern_view(space)
        with bpy.context.temp_override(window=window, area=area, region=region):
            for name, lc in layer_collections.items():
                if lc.exclude or lc.hide_viewport:
                    # Hidden everywhere by the user: leave it alone.
                    continue
                wanted = (name == garment.PATTERN_COLLECTION) == pattern_view
                if lc.visible_get() != wanted:
                    index = _layer_collection_index(view_layer, name)
                    if index > 0:
                        bpy.ops.object.hide_collection(collection_index=index, toggle=True)


# -----------------------------------------------------------------------------
# Tools

def _set_hint(context, text):
    ws = context.workspace
    if ws is not None:
        ws.status_text_set(text)


def _clear_state():
    _state.update(rect=None, hover=None, first=None, slot=None, poly=[], poly_mouse=None,
                  poly_press=None, point=None, drag=None, free=[], grab=None, mouse=None,
                  guides=[], snapped=None, readout=None, ctrl=False, cursor=None, handle=None,
                  move=None, move_hover=None)


def _snap(point, step=SNAP):
    return Vector((round(point.x / step) * step, round(point.y / step) * step, 0.0))


def _snap_targets(scene, exclude=(), extra=()):
    """Board positions of every outline point (except those in ``exclude``, (piece name, index)
    pairs), plus ``extra`` points (x, y)."""
    targets = list(extra)
    for ob in _visible_pieces(scene):
        points = garment.piece_points(ob)
        if not points:
            continue
        world = garment.to_world(ob, np.array([(x, y, 0.0) for x, y, _c in points]))
        for i, p in enumerate(world):
            if (ob.name, i) not in exclude:
                targets.append((float(p[0]), float(p[1])))
    return np.array(targets, dtype=np.float64).reshape(-1, 2)


def _snap_point(context, event, region, rv3d, mx, my, anchor=None, exclude=(), extra=(), axis_x=None):
    """The board point under the mouse, snapped, and the snapping feedback in ``_state``.

    Alt: no snapping. Shift (with an ``anchor``, the previous point): the direction from the anchor
    goes in 15 degree steps. Otherwise: onto a nearby point, else into line with points (smart
    guides) or onto the mirror line at ``axis_x``, else onto the grid.
    """
    raw = _region_to_board(region, rv3d, mx, my)
    _state.update(guides=[], snapped=None, readout=None)
    point = Vector((raw.x, raw.y, 0.0))
    scale = _ui_scale()
    if not event.alt:
        targets = _snap_targets(context.scene, exclude, extra)
        pixels = _project(region, rv3d, np.c_[targets, np.zeros(len(targets))]) if len(targets) else np.zeros((0, 2))
        if event.shift and anchor is not None:
            d = point - anchor
            if d.length > 1e-6:
                step = math.radians(ANGLE_STEP)
                angle = round(math.atan2(d.y, d.x) / step) * step
                direction = Vector((math.cos(angle), math.sin(angle), 0.0))
                length = round(d.dot(direction) / POINT_GRID) * POINT_GRID
                point = anchor + direction * length
                _state["guides"] = [(anchor.copy(), anchor + direction * max(length * 1.5, length + 0.1))]
        else:
            near = np.hypot(pixels[:, 0] - mx, pixels[:, 1] - my) if len(pixels) else np.zeros(0)
            if len(near) and near.min() < SNAP_DISTANCE * scale:
                x, y = targets[int(near.argmin())]
                point = Vector((x, y, 0.0))
                _state["snapped"] = point.copy()
            else:
                point = Vector((round(point.x / POINT_GRID) * POINT_GRID, round(point.y / POINT_GRID) * POINT_GRID, 0.0))
                reach = GUIDE_DISTANCE * scale
                axis_dx = np.inf
                if axis_x is not None:
                    axis_dx = abs(_project(region, rv3d, np.array([(axis_x, raw.y, 0.0)]))[0][0] - mx)
                if len(pixels):
                    dx = np.abs(pixels[:, 0] - mx)
                    dy = np.abs(pixels[:, 1] - my)
                    if dx.min() < reach and dx.min() <= axis_dx:
                        i = int(dx.argmin())
                        point.x = targets[i][0]
                        _state["guides"].append((Vector((targets[i][0], targets[i][1], 0.0)), None))
                    if dy.min() < reach:
                        i = int(dy.argmin())
                        point.y = targets[i][1]
                        _state["guides"].append((Vector((targets[i][0], targets[i][1], 0.0)), None))
                    # A guide runs from its point to where the snapped point ended up.
                    _state["guides"] = [(a, point.copy()) for a, _b in _state["guides"]]
                if axis_dx < reach and (not len(pixels) or axis_dx < np.abs(pixels[:, 0] - mx).min()):
                    point.x = axis_x
                    _state["guides"].append((Vector((axis_x, point.y - 0.15, 0.0)), Vector((axis_x, point.y + 0.15, 0.0))))
    _state["cursor"] = point.copy()
    if anchor is not None:
        d = point - anchor
        if d.length > 1e-6:
            _state["readout"] = ("%.1f cm   %.0f\u00b0" % (d.length * 100.0, math.degrees(math.atan2(d.y, d.x)) % 360.0),
                                 point.copy())
    return point


def _mirror_on(context):
    return context.window_manager.phantom_garment_mirror


def _mirror_axis():
    """The mirror line's x on the board: its center, where the avatar silhouette's middle is."""
    return garment.BOARD_ORIGIN.x


def _mirror_link(scene, ob, index):
    """What mirrors a piece's point in Edit Points' mirror mode.

    Across the board's center line first: a point there stays on it, and a point with a mirror
    image (on another piece, or the other half of this one) moves with it. Pieces away from the line
    mirror across their own middle instead. Returns {"partner": (piece, index) or None,
    "on_axis": bool, "world": bool}.
    """
    points = garment.piece_points(ob)
    x, y, _c = points[index]
    wx, wy = garment.to_world(ob, np.array([(x, y, 0.0)]))[0][:2]
    ax = _mirror_axis()
    if abs(wx - ax) < MIRROR_TOLERANCE:
        return {"partner": None, "on_axis": True, "world": True}
    target = (2.0 * ax - wx, wy)
    best, best_dist = None, MIRROR_TOLERANCE
    for other in _visible_pieces(scene):
        other_points = garment.piece_points(other)
        if not other_points:
            continue
        world = garment.to_world(other, np.array([(u, v, 0.0) for u, v, _d in other_points]))
        for j, q in enumerate(world):
            dist = math.hypot(q[0] - target[0], q[1] - target[1])
            if dist < best_dist and (other, j) != (ob, index):
                best, best_dist = (other, j), dist
    if best is not None:
        return {"partner": best, "on_axis": False, "world": True}
    partner, on_axis = _mirror_partner(points, index)
    return {"partner": (ob, partner) if partner is not None else None, "on_axis": on_axis, "world": False}


def _mirror_board(link, ob, board):
    """Where a mirrored point goes, in the partner's coordinates, for a point at ``board``."""
    partner_ob = link["partner"][0]
    if link["world"]:
        return _local_xy(partner_ob, Vector((2.0 * _mirror_axis() - board.x, board.y, 0.0)))
    x, y = _local_xy(ob, board)
    return -x, y


def _piece_near(scene, board, reach=0.01):
    """The visible piece whose outline passes within ``reach`` of ``board`` (or None)."""
    best, best_dist = None, reach
    for ob in _visible_pieces(scene):
        local = np.array(_local_xy(ob, board))
        for _i, span in pattern.outline_spans(garment.piece_points(ob)):
            dist = pattern.project(span, local)[0]
            if dist < best_dist:
                best, best_dist = ob, dist
    return best


def _mirror_partner(points, index):
    """For Edit Points' mirror mode: (index of the point mirroring ``index`` across the piece's
    middle (x = 0) or None, whether the point is on the middle itself)."""
    x, y, _c = points[index]
    if abs(x) < MIRROR_TOLERANCE:
        return None, True
    best, best_dist = None, MIRROR_TOLERANCE
    for j, (u, v, _d) in enumerate(points):
        dist = math.hypot(u + x, v - y)
        if j != index and dist < best_dist:
            best, best_dist = j, dist
    return best, False


def _mirror_outline(poly, axis_x):
    """A half outline drawn from the mirror line (``axis_x``) and its mirror image, as one
    closed outline. Points on the line aren't doubled."""
    full = list(poly)
    for x, y, curve in reversed(poly):
        if abs(x - axis_x) > 1e-6:
            full.append((2.0 * axis_x - x, y, pattern.mirror_curve(curve)))
    # Where the outline crosses the line straight (a level hem, say), the point there would only
    # split the edge in two: drop it, so the two halves make one seamless edge.
    n = len(full)
    keep = []
    for i, (x, y, curve) in enumerate(full):
        if abs(x - axis_x) < 1e-6 and not curve:
            px, py, _a = full[i - 1]
            qx, qy, _b = full[(i + 1) % n]
            if abs((px - x) * (qy - y) - (py - y) * (qx - x)) < 1e-9 and (px - x) * (qx - x) < 0.0:
                continue
        keep.append((x, y, curve))
    return keep if len(keep) >= 3 else full


def _mirror_plan(points, axis_x):
    """What a closed outline drawn in mirror mode becomes: ('MERGE', outline) when it runs along
    the mirror line (two points in a row on it) with the rest on one side: it and its mirror image
    as one piece, with no seam down the middle; ('TWIN', outline of the twin) when it's all on one
    side; else ('ONE', None)."""
    n = len(points)
    on = [abs(x - axis_x) < 1e-6 for x, _y, _c in points]
    sides = {x > axis_x for (x, _y, _c), o in zip(points, on) if not o}
    if len(sides) != 1:
        return 'ONE', None
    for i in range(n):
        j = (i + 1) % n
        if on[i] and on[j]:
            # The fold runs from point i to point j; the rest of the outline goes from j round to i.
            chain = [points[(j + k) % n] for k in range(n)]
            return 'MERGE', _mirror_outline(chain, axis_x)
    return 'TWIN', [(2.0 * axis_x - x, y, pattern.mirror_curve(c)) for x, y, c in reversed(points)]


def _insert_on_outline(points, local, curve=True):
    """Insert ``local`` (x, y) into the outline span it is nearest to. Returns its index."""
    spans = pattern.outline_spans(points)
    best = min(spans, key=lambda span: pattern.project(span[1], np.array(local))[0])
    points.insert(best[0] + 1, (float(local[0]), float(local[1]), curve))
    return best[0] + 1


def _points_json(points):
    def curve(c):
        handle = pattern.handle_of(c)
        return [round(handle[0], 6), round(handle[1], 6)] if handle is not None else bool(c)
    return json.dumps([[round(x, 6), round(y, 6), curve(c)] for x, y, c in points])


class _GarmentTool:
    """What every garment tool operator does (mixed into ``PHANTOM_OT_garment_tool`` and the
    per-tool operators: subclassing a registered operator would confuse Blender)."""

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
        self._release_grab()
        _clear_state()
        _state["running"] = False
        context.window_manager.phantom_garment_tool = 'SELECT'
        context.window.cursor_modal_restore()
        _set_hint(context, None)
        _redraw()

    def _busy(self):
        return bool(_state["rect"] or _state["first"] or _state["poly"] or _state["drag"] or _state["free"] or
                    _state["grab"])

    def _cancel(self, context):
        self._cancel_drag(context)
        self._release_grab()
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

        if (event.type == 'M' and event.value == 'PRESS' and pattern_view and tool in {'POLYGON', 'EDIT', 'RECTANGLE', 'ELLIPSE'}
                and not (event.ctrl or event.alt or event.shift or event.oskey)):
            wm.phantom_garment_mirror = not wm.phantom_garment_mirror
            garment.play_sound("toggle_on" if wm.phantom_garment_mirror else "toggle_off")
            _redraw()
            return {'RUNNING_MODAL'}

        if tool == 'ARRANGE':
            return self._arrange(context, event, region, rv3d, pattern_view, mx, my)
        if tool == 'DRAG':
            return self._drag(context, event, region, rv3d, pattern_view, mx, my)
        if tool == 'MOVE':
            return self._move(context, event, region, rv3d, pattern_view, mx, my)
        if not pattern_view:
            context.window.cursor_modal_restore()
            if _state["cursor"] is not None:
                _state["cursor"] = None
                _redraw()
            return {'PASS_THROUGH'}
        if event.type == 'MOUSEMOVE':
            _state["cursor"] = _region_to_board(region, rv3d, mx, my)
            if _mirror_on(context):
                _redraw(context.window)
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
        if tool == 'PIN':
            point = _snap(_region_to_board(region, rv3d, mx, my))
        else:
            # Corners land on other pieces' points and line up with them, like the Polygon tool.
            point = _snap_point(context, event, region, rv3d, mx, my,
                                axis_x=_mirror_axis() if _mirror_on(context) else None)
            if event.type == 'MOUSEMOVE' and _state["rect"] is None:
                _redraw(context.window)
        if tool == 'PIN' and event.type == 'RIGHTMOUSE' and event.value == 'PRESS':
            bpy.ops.phantom.garment_remove_pins('EXEC_DEFAULT', True, at=point.xy, use_at=True)
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
            _state.update(rect=None, guides=[], snapped=None)
            width, height = abs(b.x - a.x), abs(b.y - a.y)
            small = width < 0.03 or height < 0.03
            if tool == 'PIN':
                if small:
                    half = context.scene.phantom_garment.particle_distance
                    a, b = a - Vector((half, half, 0.0)), a + Vector((half, half, 0.0))
                lo = (min(a.x, b.x), min(a.y, b.y))
                hi = (max(a.x, b.x), max(a.y, b.y))
                bpy.ops.phantom.garment_add_pin('EXEC_DEFAULT', True, lo=lo, hi=hi)
            else:
                if small:
                    width, height = (0.3, 0.3) if tool == 'ELLIPSE' else (0.5, 0.6)
                    center = a
                else:
                    center = (a + b) / 2.0
                twin = None
                if _mirror_on(context):
                    ax = _mirror_axis()
                    if abs(center.x - ax) < width / 2.0 + 1e-6:
                        # Across the line: centered on it, as wide as its farther side.
                        width = 2.0 * max(abs(center.x - width / 2.0 - ax), abs(center.x + width / 2.0 - ax))
                        center = Vector((ax, center.y, 0.0))
                    else:
                        twin = Vector((2.0 * ax - center.x, center.y, 0.0))
                bpy.ops.phantom.garment_add_rectangle('EXEC_DEFAULT', True, width=width, height=height, location=center.xy,
                                                      ellipse=tool == 'ELLIPSE')
                if twin is not None:
                    bpy.ops.phantom.garment_add_rectangle('EXEC_DEFAULT', True, width=width, height=height, location=twin.xy,
                                                          ellipse=tool == 'ELLIPSE')
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}

    # Polygon: click corners, Ctrl-click or drag for curve points.
    def _polygon(self, context, event, region, rv3d, mx, my, _tool):
        context.window.cursor_modal_set('CROSSHAIR')
        poly = _state["poly"]
        anchor = Vector((poly[-1][0], poly[-1][1], 0.0)) if poly else None
        mirror = _mirror_on(context)
        axis_x = _mirror_axis() if mirror else None
        # Started on the mirror line: half a piece, mirrored into a whole one.
        half = mirror and bool(poly) and abs(poly[0][0] - axis_x) < 1e-6
        # Every point drawn so far is a target (and, mirrored, its image), so lines come out straight.
        extra = [(x, y) for x, y, _c in poly]
        if mirror:
            extra += [(2.0 * axis_x - x, y) for x, y, _c in poly if abs(x - axis_x) > 1e-6]
        point = _snap_point(context, event, region, rv3d, mx, my, anchor=anchor, extra=extra, axis_x=axis_x)
        if mirror and not event.alt:
            # The mirror line pulls harder than other snapping: points meant for it have to land
            # exactly on it, or the piece would come out one-sided (or with a seam down the middle).
            line_px = _project(region, rv3d, np.array([(axis_x, point.y, 0.0), (point.x, point.y, 0.0)]))
            if abs(line_px[0][0] - line_px[1][0]) < MIRROR_LINE_REACH * _ui_scale():
                point = Vector((axis_x, point.y, 0.0))
                _state.update(cursor=point.copy(), snapped=point.copy())
        _state["ctrl"] = event.ctrl
        if event.type in {'LEFT_CTRL', 'RIGHT_CTRL', 'LEFT_SHIFT', 'RIGHT_SHIFT', 'LEFT_ALT', 'RIGHT_ALT'}:
            # Ctrl (curve), Shift (angles) and Alt (no snapping) change the preview straight away.
            _state["poly_mouse"] = point
            _redraw(context.window)
            return {'RUNNING_MODAL'} if poly else {'PASS_THROUGH'}

        def near_first():
            if len(poly) < (2 if half else 3):
                return False
            first = _project(region, rv3d, np.array([(poly[0][0], poly[0][1], 0.0)]))[0]
            return np.hypot(first[0] - mx, first[1] - my) < PICK_DISTANCE * _ui_scale()

        def close():
            points = list(_state["poly"])
            plan, other = ('ONE', None)
            if half:
                points = _mirror_outline(points, axis_x)
            elif mirror:
                plan, other = _mirror_plan(points, axis_x)
                if plan == 'MERGE':
                    # Along the line: joined with its mirror image, seamlessly.
                    points = other
            _state.update(poly=[], poly_press=None, poly_mouse=None)
            bpy.ops.phantom.garment_add_polygon('EXEC_DEFAULT', True, points=_points_json(points))
            if plan == 'TWIN':
                bpy.ops.phantom.garment_add_polygon('EXEC_DEFAULT', True, points=_points_json(other))
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
            _state.update(poly_press=None, guides=[], snapped=None)
            poly.append((press["point"].x, press["point"].y, press["curve"]))
            if half and len(poly) >= 3 and abs(poly[-1][0] - axis_x) < 1e-6:
                # Back on the mirror line: the half is done.
                close()
                return {'RUNNING_MODAL'}
            garment.play_sound("measure_tick" if press["curve"] else "click")
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        if (event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS'
                and len(poly) >= (2 if half else 3)):
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
        if drag is not None and drag.get("kind") == 'HANDLE':
            return self._drag_handle(context, event, region, rv3d, mx, my, drag)
        if drag is not None:
            ob = bpy.data.objects.get(drag["piece"])
            if ob is None:
                _state["drag"] = None
                return {'RUNNING_MODAL'}
            if event.type in {'MOUSEMOVE', 'LEFT_SHIFT', 'RIGHT_SHIFT', 'LEFT_ALT', 'RIGHT_ALT'}:
                if event.type == 'MOUSEMOVE':
                    drag["mouse"] = (mx, my)
                mx, my = drag.get("mouse", (mx, my))
                original = drag["original"]
                px, py, _c = original[(drag["index"] - 1) % len(original)]
                anchor = Vector(garment.to_world(ob, np.array([(px, py, 0.0)]))[0])
                link = drag.get("link")
                partner = link["partner"] if link else None
                partner_ob = partner[0] if partner and partner[0].name in bpy.data.objects else None
                exclude = {(ob.name, drag["index"])}
                if partner_ob is not None:
                    exclude.add((partner_ob.name, partner[1]))
                axis_x = None
                if link:
                    axis_x = _mirror_axis() if link["world"] else float(garment.to_world(ob, np.array([(0.0, py, 0.0)]))[0][0])
                board = _snap_point(context, event, region, rv3d, mx, my, anchor=anchor, exclude=exclude,
                                    axis_x=axis_x)
                if link and link["on_axis"]:
                    # A point on the mirror line stays on it.
                    if link["world"]:
                        board.x = _mirror_axis()
                        x, y = _local_xy(ob, board)
                    else:
                        x, y = 0.0, _local_xy(ob, board)[1]
                else:
                    x, y = _local_xy(ob, board)
                ob.phantom_garment.points[drag["index"]].co = (x, y)
                if partner_ob is not None:
                    partner_ob.phantom_garment.points[partner[1]].co = _mirror_board(link, ob, board)
                now = time.perf_counter()
                if now - drag["rebuilt"] > EDIT_REBUILD_INTERVAL:
                    garment.regenerate_piece(ob, scene)
                    if partner_ob is not None and partner_ob is not ob:
                        garment.regenerate_piece(partner_ob, scene)
                    drag["rebuilt"] = now
                _redraw(context.window)
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                _state.update(drag=None, guides=[], snapped=None, readout=None)
                link = drag.get("link")
                twin = link["partner"][0] if link and link["partner"] else None
                if twin is not None and twin is not ob and twin.name in bpy.data.objects:
                    twin_points = garment.piece_points(twin)
                    garment.set_piece_points(twin, drag["twin_original"])
                    bpy.ops.phantom.garment_set_points('EXEC_DEFAULT', True, piece=twin.name, points=_points_json(twin_points))
                points = garment.piece_points(ob)
                garment.set_piece_points(ob, drag["original"])
                bpy.ops.phantom.garment_set_points('EXEC_DEFAULT', True, piece=ob.name, points=_points_json(points))
                garment.play_sound("transform_confirm")
                _redraw(context.window)
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            point = _pick_point(scene, region, rv3d, mx, my)
            handle = None if point else _pick_handle(context, region, rv3d, mx, my)
            hover = None if point or handle else _pick_segment(scene, region, rv3d, mx, my)
            if point:
                _state["handle_piece"] = point[0].name
            elif hover:
                _state["handle_piece"] = hover[0].name
            if point != _state["point"] or hover != _state["hover"] or handle != _state["handle"]:
                _state["point"], _state["hover"], _state["handle"] = point, hover, handle
                _redraw(context.window)
            context.window.cursor_modal_set('HAND' if point or handle else ('CROSSHAIR' if hover else 'DEFAULT'))
            return {'PASS_THROUGH'}
        point = _pick_point(scene, region, rv3d, mx, my)
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and not point and not event.ctrl:
            handle = _pick_handle(context, region, rv3d, mx, my)
            if handle:
                ob, index, side = handle
                _state["drag"] = {"kind": 'HANDLE', "piece": ob.name, "index": index, "side": side,
                                  "original": garment.piece_points(ob), "region": region, "rv3d": rv3d,
                                  "rebuilt": 0.0}
                if _mirror_on(context):
                    link = _mirror_link(scene, ob, index)
                    _state["drag"]["link"] = link
                    if link["partner"] and link["partner"][0] is not ob:
                        _state["drag"]["twin_original"] = garment.piece_points(link["partner"][0])
                garment.play_sound("grab_start")
                return {'RUNNING_MODAL'}
        if (event.type == 'R' and event.value == 'PRESS' and point
                and pattern.handle_of(garment.piece_points(point[0])[point[1]][2]) is not None):
            # Back to the automatic, smooth handle.
            ob, index = point
            points = garment.piece_points(ob)
            x, y, _c = points[index]
            points[index] = (x, y, True)
            bpy.ops.phantom.garment_set_points('EXEC_DEFAULT', True, piece=ob.name, points=_points_json(points))
            garment.play_sound("toggle_off")
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if point and not event.ctrl:
                ob, index = point
                original = garment.piece_points(ob)
                _state["drag"] = {"piece": ob.name, "index": index, "original": original,
                                  "region": region, "rv3d": rv3d, "rebuilt": 0.0}
                if _mirror_on(context):
                    link = _mirror_link(scene, ob, index)
                    _state["drag"]["link"] = link
                    if link["partner"] and link["partner"][0] is not ob:
                        _state["drag"]["twin_original"] = garment.piece_points(link["partner"][0])
                garment.play_sound("grab_start")
                return {'RUNNING_MODAL'}
            hover = _pick_segment(scene, region, rv3d, mx, my)
            if event.ctrl and hover:
                ob = hover[0]
                points = garment.piece_points(ob)
                board = _region_to_board(region, rv3d, mx, my)
                local = _local_xy(ob, board)
                index = _insert_on_outline(points, local)
                if _mirror_on(context):
                    mirrored = Vector((2.0 * _mirror_axis() - board.x, board.y, 0.0))
                    twin = _piece_near(scene, mirrored) if abs(board.x - _mirror_axis()) > MIRROR_TOLERANCE else None
                    if twin is ob:
                        if _insert_on_outline(points, _local_xy(ob, mirrored)) <= index:
                            index += 1
                    elif twin is not None:
                        twin_points = garment.piece_points(twin)
                        _insert_on_outline(twin_points, _local_xy(twin, mirrored))
                        bpy.ops.phantom.garment_set_points('EXEC_DEFAULT', True, piece=twin.name, points=_points_json(twin_points))
                    elif abs(local[0]) > MIRROR_TOLERANCE:
                        # Away from the line: the other half of this piece.
                        if _insert_on_outline(points, (-local[0], local[1])) <= index:
                            index += 1
                bpy.ops.phantom.garment_set_points('EXEC_DEFAULT', True, piece=ob.name, points=_points_json(points))
                garment.play_sound("loop_add")
                _state["point"] = (ob, index)
                _redraw(context.window)
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        if event.type == 'RIGHTMOUSE' and event.value == 'PRESS' and point:
            ob, index = point
            old = garment.piece_points(ob)
            new = list(old)
            x, y, curve = new[index]
            new[index] = (x, y, not curve)
            partner = _mirror_link(scene, ob, index)["partner"] if _mirror_on(context) else None
            if partner is not None and partner[0] is ob and bool(old[partner[1]][2]) == bool(curve):
                u, v, _c = new[partner[1]]
                new[partner[1]] = (u, v, not curve)
            elif partner is not None:
                twin_old = garment.piece_points(partner[0])
                twin_new = list(twin_old)
                u, v, c = twin_new[partner[1]]
                if bool(c) == bool(curve):
                    twin_new[partner[1]] = (u, v, not curve)
                    bpy.ops.phantom.garment_set_points('EXEC_DEFAULT', True, piece=partner[0].name, points=_points_json(twin_new),
                                                       old_points=_points_json(twin_old))
            bpy.ops.phantom.garment_set_points('EXEC_DEFAULT', True, piece=ob.name, points=_points_json(new), old_points=_points_json(old))
            garment.play_sound("toggle_off" if curve else "toggle_on")
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        if event.type in {'X', 'DEL'} and event.value == 'PRESS' and point:
            ob, index = point
            old = garment.piece_points(ob)
            remove = {index}
            partner = _mirror_link(scene, ob, index)["partner"] if _mirror_on(context) else None
            if partner is not None and partner[0] is ob:
                remove.add(partner[1])
            if len(old) - len(remove) < 3:
                garment.play_sound("error")
                return {'RUNNING_MODAL'}
            if partner is not None and partner[0] is not ob:
                twin_old = garment.piece_points(partner[0])
                if len(twin_old) > 3:
                    twin_new = twin_old[:partner[1]] + twin_old[partner[1] + 1:]
                    bpy.ops.phantom.garment_set_points('EXEC_DEFAULT', True, 
                        piece=partner[0].name, points=_points_json(twin_new),
                        old_points="" if twin_old[partner[1]][2] else _points_json(twin_old))
            new = [p for i, p in enumerate(old) if i not in remove]
            corner_removed = any(not old[i][2] for i in remove)
            bpy.ops.phantom.garment_set_points('EXEC_DEFAULT', True, piece=ob.name, points=_points_json(new),
                                               old_points=_points_json(old) if corner_removed else "")
            garment.play_sound("loop_remove")
            _state["point"] = None
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def _drag_handle(self, context, event, region, rv3d, mx, my, drag):
        """Edit Points: dragging a curve point's handle end (both arms turn together)."""
        scene = context.scene
        ob = bpy.data.objects.get(drag["piece"])
        if ob is None:
            _state["drag"] = None
            return {'RUNNING_MODAL'}
        index = drag["index"]
        if event.type == 'MOUSEMOVE':
            x, y = ob.phantom_garment.points[index].co
            lx, ly = _local_xy(ob, _region_to_board(region, rv3d, mx, my))
            arm = (drag["side"] * (lx - x), drag["side"] * (ly - y))
            point = ob.phantom_garment.points[index]
            point.has_handle = True
            point.handle = arm
            link = drag.get("link")
            partner = link["partner"] if link else None
            if partner is not None and partner[0].name in bpy.data.objects:
                twin, j = partner
                if link["world"]:
                    # Mirror the arm in the world, into the twin's own coordinates.
                    world_arm = ob.matrix_world.to_3x3() @ Vector((arm[0], arm[1], 0.0))
                    local = twin.matrix_world.to_3x3().inverted_safe() @ Vector((world_arm.x, -world_arm.y, 0.0))
                    mirrored = (local.x, local.y)
                else:
                    mirrored = pattern.mirror_curve(arm)
                twin_point = twin.phantom_garment.points[j]
                twin_point.curve = True
                twin_point.has_handle = True
                twin_point.handle = mirrored
            now = time.perf_counter()
            if now - drag["rebuilt"] > EDIT_REBUILD_INTERVAL:
                garment.regenerate_piece(ob, scene)
                if partner is not None and partner[0] is not ob and partner[0].name in bpy.data.objects:
                    garment.regenerate_piece(partner[0], scene)
                drag["rebuilt"] = now
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            _state.update(drag=None)
            link = drag.get("link")
            twin = link["partner"][0] if link and link["partner"] else None
            if twin is not None and twin is not ob and twin.name in bpy.data.objects:
                twin_points = garment.piece_points(twin)
                garment.set_piece_points(twin, drag["twin_original"])
                bpy.ops.phantom.garment_set_points('EXEC_DEFAULT', True, piece=twin.name, points=_points_json(twin_points))
            points = garment.piece_points(ob)
            garment.set_piece_points(ob, drag["original"])
            bpy.ops.phantom.garment_set_points('EXEC_DEFAULT', True, piece=ob.name, points=_points_json(points))
            garment.play_sound("transform_confirm")
            _redraw(context.window)
        return {'RUNNING_MODAL'}

    def _move(self, context, event, region, rv3d, pattern_view, mx, my):
        """Move Pieces: drag a piece in the 3D view (placed: around the avatar; else on the board)."""
        move = _state["move"]
        if pattern_view and move is None:
            context.window.cursor_modal_restore()
            return {'PASS_THROUGH'}
        if move is None:
            if event.type == 'MOUSEMOVE':
                hover = _pick_piece_3d(context, region, rv3d, mx, my)
                name = hover.name if hover else None
                if name != _state["move_hover"]:
                    _state["move_hover"] = name
                    _redraw()
                context.window.cursor_modal_set('HAND' if hover else 'DEFAULT')
                return {'PASS_THROUGH'}
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                ob = _pick_piece_3d(context, region, rv3d, mx, my)
                if ob is None:
                    return {'PASS_THROUGH'}
                verts = garment.piece_world_points(ob)
                anchor = Vector(_piece_3d(context.scene, ob, verts).mean(axis=0))
                _state["move"] = {"piece": ob.name, "anchor": anchor, "region": region, "rv3d": rv3d,
                                  "offset": tuple(ob.phantom_garment.offset), "delta": (0.0, 0.0)}
                context.window.cursor_modal_set('SCROLL_XY')
                garment.play_sound("grab_start")
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        ob = bpy.data.objects.get(move["piece"])
        if ob is None:
            _state["move"] = None
            return {'RUNNING_MODAL'}
        if event.type == 'MOUSEMOVE':
            region, rv3d = move["region"], move["rv3d"]
            x, y = event.mouse_x - region.x, event.mouse_y - region.y
            where = view3d_utils.region_2d_to_location_3d(region, rv3d, (x, y), move["anchor"])
            delta = where - move["anchor"]
            scene = context.scene
            slot = ob.phantom_garment.arrangement
            avatar = scene.phantom_garment.avatar
            frame = garment.avatar_frame(avatar) if avatar is not None and slot in SLOT_LABELS else {}
            if slot in frame:
                entry = frame[slot]
                right = (-entry.facing).cross(entry.axis)
                move["delta"] = (delta.dot(right), delta.dot(entry.axis))
            else:
                _origin, right, up = _unplaced_layout(scene)
                move["delta"] = (delta.dot(right), delta.dot(up))
            _redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            _state["move"] = None
            dx, dy = move["delta"]
            slot = ob.phantom_garment.arrangement
            avatar = context.scene.phantom_garment.avatar
            if avatar is not None and slot in garment.avatar_frame(avatar):
                ox, oy = move["offset"]
                ob.phantom_garment.offset = (ox + dx, oy + dy)
            else:
                ob.location.x += dx
                ob.location.y += dy
            bpy.ops.ed.undo_push(message="Move Piece")
            garment.play_sound("transform_confirm")
            context.window.cursor_modal_set('HAND')
            _redraw()
            return {'RUNNING_MODAL'}
        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            _state["move"] = None
            garment.play_sound("toggle_off")
            _redraw()
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}

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
                bpy.ops.phantom.garment_toggle_elastic('EXEC_DEFAULT', True, piece=ob.name, segment=index)
                _redraw(context.window)
                return {'RUNNING_MODAL'}
            pick = (ob.name, index, along > 0.5)
            first = _state["first"]
            if first is None:
                _state["first"] = pick
                garment.play_sound("measure_tick")
            else:
                _state["first"] = None
                bpy.ops.phantom.garment_add_seam('EXEC_DEFAULT', True, 
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
                bpy.ops.phantom.garment_add_seam('EXEC_DEFAULT', True, 
                    piece_a=a1[0], segment_a=a1[1], start_a=a1[2], end_a=a2[2],
                    piece_b=b1[0], segment_b=b1[1], start_b=b1[2], end_b=b2[2])
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        if event.type == 'RIGHTMOUSE' and event.value == 'PRESS' and picks:
            self._cancel(context)
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'} if picks else {'PASS_THROUGH'}

    # Drag: pull the simulating cloth (GPU cloth only).
    def _release_grab(self):
        if _state["grab"] is not None:
            import phantom_garment_newton as gpu_cloth
            sim = gpu_cloth.simulation()
            if sim is not None:
                sim.release_drag()
            _state["grab"] = None

    def _drag(self, context, event, region, rv3d, pattern_view, mx, my):
        import phantom_garment_newton as gpu_cloth
        from bpy_extras import view3d_utils
        wm = context.window_manager
        if pattern_view and _state["grab"] is None:
            _state["mouse"] = None
            return {'PASS_THROUGH'}
        if event.type in {'LEFT_BRACKET', 'RIGHT_BRACKET'} and event.value == 'PRESS':
            step = 1.0 / 1.2 if event.type == 'LEFT_BRACKET' else 1.2
            wm.phantom_garment_drag_radius = int(min(400, max(8, wm.phantom_garment_drag_radius * step)))
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        if event.type == 'MOUSEMOVE':
            _state["mouse"] = (mx, my, region.as_pointer())
            grab = _state["grab"]
            if grab is not None:
                target = view3d_utils.region_2d_to_location_3d(grab["region"], grab["rv3d"], (mx, my), grab["anchor"])
                sim = gpu_cloth.simulation()
                if sim is not None:
                    sim.set_drag(delta=tuple(target - grab["anchor"]))
            _redraw(context.window)
            return {'RUNNING_MODAL'} if grab is not None else {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            scene = context.scene
            if not garment.newton_active(scene):
                self.report({'WARNING'}, "Dragging the cloth needs GPU cloth (Simulation panel)")
                garment.play_sound("error")
                return {'RUNNING_MODAL'}
            if gpu_cloth.simulation() is None or garment.garment_needs_build(scene):
                garment.build_garment(scene)
            sim = gpu_cloth.simulation()
            if sim is None:
                return {'RUNNING_MODAL'}
            positions = sim.positions()
            pixels = _project(region, rv3d, positions)
            radius = wm.phantom_garment_drag_radius * _ui_scale()
            dist = np.hypot(pixels[:, 0] - mx, pixels[:, 1] - my)
            under = dist < radius
            if not under.any():
                return {'PASS_THROUGH'}
            # Only the side of the cloth facing the view.
            view = np.array(rv3d.view_matrix)
            depth = -(positions @ view[2, :3] + view[2, 3])
            nearest = depth[under].min()
            chosen = under & (depth < nearest + DRAG_DEPTH)
            weights = np.where(chosen, (1.0 - (dist / radius) ** 2) ** 2, 0.0)
            anchor = Vector((positions[chosen] * weights[chosen, None]).sum(axis=0) / weights[chosen].sum())
            sim.set_drag(weights=weights, grab=positions, delta=(0.0, 0.0, 0.0))
            _state["grab"] = {"anchor": anchor, "region": region, "rv3d": rv3d}
            if not gpu_cloth.running():
                gpu_cloth.start()
            garment.play_sound("grab_start")
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and _state["grab"] is not None:
            self._release_grab()
            garment.play_sound("transform_confirm")
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'} if _state["grab"] is not None else {'PASS_THROUGH'}

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
            bpy.ops.phantom.garment_arrange('EXEC_DEFAULT', True, slot=slot)
            _redraw(context.window)
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}


class PHANTOM_OT_garment_tool(_GarmentTool, Operator):
    """Choose a garment tool (the header buttons use one operator per tool, named after it)"""
    bl_idname = "phantom.garment_tool"
    bl_label = "Garment Tool"
    bl_options = {'INTERNAL'}

    tool: EnumProperty(items=TOOL_ITEMS)


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


def _silhouette_batch(avatar):
    """The avatar seen from the front, laid on the pattern board at full size (cached)."""
    key = (avatar.name, tuple(round(x, 4) for row in avatar.matrix_world for x in row), len(avatar.data.vertices))
    if _silhouette["key"] == key:
        return _silhouette["batch"]
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = avatar.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        co = np.empty(len(mesh.vertices) * 3)
        mesh.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        mat = np.array(avatar.matrix_world)
        co = co @ mat[:3, :3].T + mat[:3, 3]
        tris = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("vertices", tris)
    finally:
        evaluated.to_mesh_clear()
    lo, hi = co.min(axis=0), co.max(axis=0)
    middle = 0.5 * (lo + hi)
    board = np.column_stack((co[:, 0] - middle[0] + garment.BOARD_ORIGIN.x,
                             co[:, 2] - middle[2] + garment.BOARD_ORIGIN.y,
                             np.full(len(co), -0.004)))
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'TRIS', {"pos": board.astype(np.float32)}, indices=tris.reshape(-1, 3))
    _silhouette.update(key=key, batch=batch)
    return batch


def _draw_silhouette(context):
    avatar = context.scene.phantom_garment.avatar
    if avatar is None or avatar.type != 'MESH' or not context.window_manager.phantom_garment_show_silhouette:
        return
    batch = _silhouette_batch(avatar)
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader.uniform_float("color", SILHOUETTE_COLOR)
    # Behind the pieces (they lie at Z = 0, the silhouette just below).
    gpu.state.depth_test_set('LESS_EQUAL')
    batch.draw(shader)
    gpu.state.depth_test_set('NONE')


def _draw_pattern_view(context):
    scene = context.scene
    tool = context.window_manager.phantom_garment_tool
    _draw_silhouette(context)
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
        handle_piece = _handle_piece(context)
        if handle_piece is not None:
            hovered = _state["handle"]
            drag = _state["drag"]
            ends = _handle_ends(handle_piece)
            for _i, _side, base, end in ends:
                _polyline(_lift([base, end], 0.005), HANDLE_LINE_COLOR, 1.5)
            for i, side, _base, end in ends:
                active = bool(hovered and hovered[0] is handle_piece and hovered[1:] == (i, side)) or bool(
                    drag and drag.get("kind") == 'HANDLE' and drag["piece"] == handle_piece.name
                    and (drag["index"], drag["side"]) == (i, side))
                _points(_lift([end], 0.006), (0.0, 0.0, 0.0, 0.8), 9.0 if active else 7.0)
                _points(_lift([end], 0.007), HOVER_COLOR if active else HANDLE_COLOR, 7.0 if active else 5.0)
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
            # Ctrl previews the next point as a curve point.
            preview.append((mouse.x, mouse.y, _state["ctrl"]))
        if context.window_manager.phantom_garment_mirror and len(preview) >= 2:
            axis_x = _mirror_axis()
            shape = None
            if abs(poly[0][0] - axis_x) < 1e-6:
                # Half a piece: the whole as it will come out.
                shape = np.vstack(pattern.outline_segments(_mirror_outline(preview, axis_x)))
            elif len(preview) >= 3:
                # Joined with its mirror image along the line, or its twin (none across the line).
                plan, other = _mirror_plan(preview, axis_x)
                if other is not None:
                    shape = np.vstack(pattern.outline_segments(other))
            else:
                shape = np.c_[2.0 * axis_x - np.array([p[0] for p in preview]), [p[1] for p in preview]]
            if shape is not None:
                _polyline(_lift(np.c_[shape, np.zeros(len(shape))], 0.004), MIRROR_PREVIEW_COLOR, 2.0)
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
    mirror = context.window_manager.phantom_garment_mirror
    if mirror and tool in {'POLYGON', 'EDIT', 'RECTANGLE', 'ELLIPSE'}:
        axis_x = _mirror_axis()
        _polyline(_lift([(axis_x, -5.0, 0.0), (axis_x, 5.0, 0.0)], 0.003), MIRROR_LINE_COLOR, 1.0)
        rect = _state["rect"]
        if rect:
            a, b = rect
            corners = [(2.0 * axis_x - x, y, 0.0) for x, y in ((a.x, a.y), (b.x, a.y), (b.x, b.y), (a.x, b.y), (a.x, a.y))]
            _polyline(_lift(corners, 0.004), MIRROR_PREVIEW_COLOR, 2.0)
    if mirror and tool == 'EDIT':
        drag = _state["drag"]
        target = (bpy.data.objects.get(drag["piece"]), drag["index"]) if drag else _state["point"]
        if target and target[0] is not None and target[0].name in bpy.data.objects:
            ob, index = target
            if index < len(garment.piece_points(ob)):
                link = drag.get("link") if drag else _mirror_link(scene, ob, index)
                partner = link["partner"] if link else None
                if link and not link["world"]:
                    # Mirrored across the piece's own middle.
                    ys = [p[1] for p in garment.piece_points(ob)]
                    axis = garment.to_world(ob, np.array([(0.0, min(ys) - 0.05, 0.0), (0.0, max(ys) + 0.05, 0.0)]))
                    _polyline(_lift(axis, 0.006), MIRROR_LINE_COLOR, 1.0)
                if partner is not None and partner[0].name in bpy.data.objects:
                    partner_points = garment.piece_points(partner[0])
                    if partner[1] < len(partner_points):
                        x, y, _c = partner_points[partner[1]]
                        _points(_lift(garment.to_world(partner[0], np.array([(x, y, 0.0)])), 0.006),
                                MIRROR_PREVIEW_COLOR, 12.0)
    cursor = _state["cursor"]
    if mirror and cursor is not None and tool in {'POLYGON', 'EDIT', 'RECTANGLE', 'ELLIPSE'}:
        # The ghost cursor: where the mirror image of the cursor goes.
        ghost = [(2.0 * _mirror_axis() - cursor.x, cursor.y, 0.0)]
        _points(_lift(ghost, 0.008), (0.0, 0.0, 0.0, 0.35), 15.0)
        _points(_lift(ghost, 0.009), MIRROR_GHOST_COLOR, 11.0)
        _points(_lift(ghost, 0.01), (1.0, 1.0, 1.0, 0.9), 4.0)
    if tool in {'POLYGON', 'EDIT', 'RECTANGLE', 'ELLIPSE'}:
        for a, b in _state["guides"]:
            if b is not None:
                _polyline(_lift([a, b], 0.006), GUIDE_COLOR, 1.0)
        if _state["snapped"] is not None:
            _points(_lift([_state["snapped"]], 0.007), GUIDE_COLOR, 14.0)
    gpu.state.blend_set('NONE')


def _unplaced_layout(scene):
    """Where unplaced pieces stand in the 3D view: (origin, right, up) mapping the pattern board
    onto a wall in front of the avatar, at full size."""
    avatar = scene.phantom_garment.avatar
    if avatar is not None and avatar.type == 'MESH':
        corners = np.array([avatar.matrix_world @ Vector(c) for c in avatar.bound_box])
        lo, hi = corners.min(axis=0), corners.max(axis=0)
        origin = Vector(((lo[0] + hi[0]) / 2.0, lo[1] - UNPLACED_DISTANCE, (lo[2] + hi[2]) / 2.0))
    else:
        origin = Vector((0.0, -UNPLACED_DISTANCE, 1.0))
    return origin, Vector((1.0, 0.0, 0.0)), Vector((0.0, 0.0, 1.0))


def _draw_unplaced(context):
    scene = context.scene
    frame_slots = SLOT_LABELS
    # Not visible_get(): the 3D view hides the pattern collection (local collections) on purpose.
    pieces = [ob for ob in garment.pattern_pieces(scene)
              if not ob.hide_get() and ob.phantom_garment.arrangement not in frame_slots]
    if not pieces:
        return
    origin, right, up = _unplaced_layout(scene)
    board = garment.BOARD_ORIGIN
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.depth_test_set('LESS_EQUAL')
    for ob in pieces:
        mesh = ob.data
        if not len(mesh.polygons):
            continue
        world = garment.piece_world_points(ob)
        placed = (np.array(origin) + np.outer(world[:, 0] - board.x, np.array(right)) +
                  np.outer(world[:, 1] - board.y, np.array(up)))
        # Pattern meshes are all triangles, so their corners are the triangles (no need to
        # calculate loop triangles, which can't be done while drawing).
        tris = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", tris)
        color = list(garment.FABRICS[ob.phantom_garment.fabric]["color"])
        color[3] = UNPLACED_ALPHA
        batch = batch_for_shader(shader, 'TRIS', {"pos": placed.astype(np.float32)}, indices=tris.reshape(-1, 3))
        shader.uniform_float("color", color)
        batch.draw(shader)
        for poly in garment.outline_world(ob):
            outline = (np.array(origin) + np.outer(poly[:, 0] - board.x, np.array(right)) +
                       np.outer(poly[:, 1] - board.y, np.array(up)))
            _polyline(outline, (0.05, 0.05, 0.08, 0.8), 1.5)
    gpu.state.depth_test_set('NONE')


def _glow_line(points, color, core=True):
    """A softly glowing line: a faint halo, then the line itself."""
    r, g, b = color[:3]
    _polyline(points, (r, g, b, 0.07), 8.0)
    if core:
        _polyline(points, (r, g, b, 0.8), 2.0)


def _draw_sew_lines(scene, side_a, side_b, color):
    """How two stretches will be sewn: both glowing where the pieces stand in the 3D view, with
    threads joining the points that meet."""
    (ob_a, board_a), (ob_b, board_b) = side_a, side_b
    if len(board_a) < 2 or len(board_b) < 2:
        return
    line_a = _piece_3d(scene, ob_a, board_a)
    line_b = _piece_3d(scene, ob_b, board_b)
    _glow_line(line_a, color)
    _glow_line(line_b, color)
    threads = []
    for t in np.linspace(0.0, 1.0, SEW_PREVIEW_THREADS):
        threads += [pattern.point_at(line_a, t), pattern.point_at(line_b, t)]
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'LINES', {"pos": [tuple(p) for p in threads]})
    shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
    shader.uniform_float("lineWidth", 1.0 * _ui_scale())
    shader.uniform_float("color", (color[0], color[1], color[2], 0.4))
    batch.draw(shader)
    _points(threads[0::2] + threads[1::2], (color[0], color[1], color[2], 0.6), 4.0)


def _draw_sew_preview(context):
    """The seams, and the one being made, as a live preview of how the pieces will be sewn."""
    scene = context.scene
    tool = context.window_manager.phantom_garment_tool
    import phantom_garment_newton as gpu_cloth
    simulating = gpu_cloth.running() or context.screen.is_animation_playing
    gpu.state.depth_test_set('NONE')
    if not simulating or tool in {'SEW', 'FREE_SEW'}:
        for i, seam in enumerate(garment.valid_seams(scene)):
            sides = [(ob, _stretch(ob, segment, start, end)) for ob, segment, start, end in garment.seam_sides(seam)]
            _draw_sew_lines(scene, sides[0], sides[1], SEAM_COLORS[i % len(SEAM_COLORS)])
    # The seam being made: the first pick and the stretch under the mouse.
    hover = _state["hover"]
    hover_valid = hover and hover[0].name in bpy.data.objects
    if tool == 'SEW' and hover_valid and _state["first"] and _state["first"][0] in bpy.data.objects:
        first = _state["first"]
        ob_a = bpy.data.objects[first[0]]
        side_a = (ob_a, _stretch(ob_a, first[1], *((1.0, 0.0) if first[2] else (0.0, 1.0))))
        ob_b, index, along = hover
        side_b = (ob_b, _stretch(ob_b, index, *((1.0, 0.0) if along > 0.5 else (0.0, 1.0))))
        _draw_sew_lines(scene, side_a, side_b, HOVER_COLOR)
    elif tool == 'FREE_SEW' and hover_valid:
        picks = [p for p in _state["free"] if p[0] in bpy.data.objects]
        if len(picks) == 3 and hover[0].name == picks[2][0] and hover[1] == picks[2][1]:
            ob_a, ob_b = bpy.data.objects[picks[0][0]], bpy.data.objects[picks[2][0]]
            side_a = (ob_a, _stretch(ob_a, picks[0][1], picks[0][2], picks[1][2]))
            side_b = (ob_b, _stretch(ob_b, picks[2][1], picks[2][2], hover[2]))
            _draw_sew_lines(scene, side_a, side_b, HOVER_COLOR)


def _draw_move_preview(context):
    """Move Pieces: the piece under the mouse, or the one being moved where it's going."""
    scene = context.scene
    move = _state["move"]
    name = move["piece"] if move else _state["move_hover"]
    ob = bpy.data.objects.get(name) if name else None
    if ob is None or not ob.phantom_garment.is_piece:
        return
    gpu.state.depth_test_set('NONE')
    for poly in garment.outline_world(ob):
        if move:
            dx, dy = move["delta"]
            ox, oy = move["offset"]
            line = _piece_3d(scene, ob, poly, offset=(ox + dx, oy + dy), shift=(dx, dy))
        else:
            line = _piece_3d(scene, ob, poly)
        _glow_line(line, HOVER_COLOR)


def _draw_3d_view(context):
    gpu.state.blend_set('ALPHA')
    _draw_unplaced(context)
    if context.window_manager.phantom_garment_show_seams:
        _draw_sew_preview(context)
    if context.window_manager.phantom_garment_tool == 'MOVE':
        _draw_move_preview(context)
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
        readout = _state["readout"]
        if readout is not None and context.window_manager.phantom_garment_tool in {'POLYGON', 'EDIT'}:
            text, at = readout
            pixel = _project(region, rv3d, np.array([at]))[0]
            _text(pixel[0] + 40 * _ui_scale(), pixel[1] + 18 * _ui_scale(), text, 12.0, GUIDE_COLOR)
        rect = _state["rect"]
        tool = context.window_manager.phantom_garment_tool
        if rect and tool != 'PIN':
            a, b = rect
            mid = _project(region, rv3d, np.array([(a + b) / 2.0]))[0]
            _text(mid[0], mid[1], "%.0f x %.0f cm" % (abs(b.x - a.x) * 100.0, abs(b.y - a.y) * 100.0), 14.0)
    elif context.window_manager.phantom_garment_tool == 'DRAG':
        mouse = _state["mouse"]
        if mouse is not None and mouse[2] == region.as_pointer():
            radius = context.window_manager.phantom_garment_drag_radius * _ui_scale()
            circle = [(mouse[0] + radius * math.cos(a), mouse[1] + radius * math.sin(a), 0.0)
                      for a in np.linspace(0.0, 2.0 * math.pi, 49)]
            gpu.state.blend_set('ALPHA')
            _polyline(circle, BRUSH_COLOR if _state["grab"] is None else HOVER_COLOR, 1.5)
            gpu.state.blend_set('NONE')
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

def _tool_operator(key):
    return "phantom.garment_tool_" + key.lower()


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
        row.operator(_tool_operator(key), text=name if text else "", icon=icon,
                     depress=wm.phantom_garment_tool == key)


def _simulate_button(layout, context, text=True):
    playing = garment.simulating(context)
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
        layout.separator()
        layout.prop(context.window_manager, "phantom_garment_mirror", text="", icon='MOD_MIRROR')
    else:
        _tool_buttons(layout, context, ('ARRANGE', 'MOVE', 'DRAG'), text=True)
        layout.separator()
        _simulate_button(layout, context)


class GarmentPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Garment"

    @classmethod
    def poll(cls, context):
        return is_garment_workspace(context.workspace)


class VIEW3D_PT_phantom_garment_experimental(GarmentPanel, Panel):
    bl_label = "Experimental"
    bl_options = {'HIDE_HEADER'}

    def draw(self, _context):
        col = self.layout.box().column(align=True)
        col.alert = True
        col.label(text="Garment is experimental", icon='ERROR')
        col.alert = False
        col.label(text="Expect bugs and crashes: save often.")


class VIEW3D_PT_phantom_garment_avatar(GarmentPanel, Panel):
    bl_label = "Avatar"

    def draw(self, context):
        settings = context.scene.phantom_garment
        layout = self.layout
        layout.prop(settings, "avatar", text="")
        layout.operator_menu_enum(garment.PHANTOM_OT_garment_add_avatar.bl_idname, "choice",
                                  text="Add Avatar" if settings.avatar is None else "Change Avatar",
                                  icon='OUTLINER_OB_ARMATURE')
        layout.prop(settings, "skin_offset")


class VIEW3D_PT_phantom_garment_patterns(GarmentPanel, Panel):
    bl_label = "Patterns"

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        row.prop(context.window_manager, "phantom_garment_show_silhouette", text="Avatar Silhouette")
        row.prop(context.window_manager, "phantom_garment_mirror", text="Mirror", icon='MOD_MIRROR')
        col = layout.column(align=True)
        _tool_buttons(col, context, PATTERN_TOOLS[:5])
        _tool_buttons(col, context, PATTERN_TOOLS[5:] + ('ARRANGE', 'MOVE', 'DRAG'))
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
        layout.operator(garment.PHANTOM_OT_garment_mirror_copy.bl_idname, icon='MOD_MIRROR')
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
        layout.prop(context.window_manager, "phantom_garment_show_seams", icon='LIGHT_SUN')
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
        import phantom_garment_newton as gpu_cloth
        layout = self.layout
        settings = context.scene.phantom_garment
        layout.prop(settings, "solver", expand=True)
        if settings.solver == 'GPU':
            if gpu_cloth.installing():
                layout.label(text=gpu_cloth.install_status(), icon='SORTTIME')
            elif not gpu_cloth.installed():
                box = layout.box()
                box.label(text="GPU cloth isn't installed", icon='INFO')
                box.operator(gpu_cloth.PHANTOM_OT_garment_install_gpu.bl_idname, icon='IMPORT')
                if gpu_cloth.install_status():
                    box.label(text=gpu_cloth.install_status())
            elif not gpu_cloth.available():
                layout.label(text=gpu_cloth._state["error"] or "GPU cloth isn't available", icon='ERROR')
            else:
                sim = gpu_cloth.simulation()
                if sim is not None and gpu_cloth.running():
                    layout.label(text="%d particles, %.1f ms a frame" % (sim.count, sim.step_ms), icon='SOLO_ON')
                row = layout.row(align=True)
                row.prop(context.window_manager, "phantom_garment_drag_radius", text="Drag Brush")
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
        _sync_views(window)
        import phantom_garment_newton as gpu_cloth
        if window.screen.is_animation_playing or _state["drag"] or gpu_cloth.running():
            continue
        scene = window.scene
        garment.maintain(scene)
        if garment.garment_signature(scene) != garment._built["signature"]:
            garment.build_garment(scene)
            _redraw(window)
    return WATCH_INTERVAL


@persistent
def _after_undo(*_args):
    # Undo and loading replace the objects: drop what the tools were pointing at.
    _clear_state()
    garment._segment_cache.clear()


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

def _make_tool_operator(key, name, description):
    """An operator choosing one tool (so tooltips and search show the tool's own name)."""
    return type("PHANTOM_OT_garment_tool_" + key.lower(), (_GarmentTool, Operator), {
        "__doc__": description,
        "bl_idname": _tool_operator(key),
        "bl_label": name,
        "bl_description": description,
        "__annotations__": {"tool": EnumProperty(items=TOOL_ITEMS, default=key, options={'HIDDEN'})},
    })


TOOL_OPERATORS = tuple(_make_tool_operator(key, name, desc) for key, name, desc, _icon, _n in TOOL_ITEMS)

classes = (
    PHANTOM_OT_garment_workspace_add,
    PHANTOM_OT_garment_tool,
    *TOOL_OPERATORS,
    VIEW3D_PT_phantom_garment_experimental,
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
    bpy.types.WindowManager.phantom_garment_drag_radius = IntProperty(
        name="Drag Brush", description="Size of the drag brush, in pixels ([ and ] while dragging)",
        default=60, min=8, max=400, subtype='PIXEL')
    bpy.types.WindowManager.phantom_garment_show_silhouette = BoolProperty(
        name="Avatar Silhouette", description="Show the avatar's silhouette behind the pieces in the pattern view",
        default=True)
    bpy.types.WindowManager.phantom_garment_show_seams = BoolProperty(
        name="Sewing Preview", description="Show in the 3D view how the pieces will be sewn together: the seams "
        "glowing where the pieces stand, with threads joining the points that meet",
        default=True, update=lambda _self, _context: _redraw())
    bpy.types.WindowManager.phantom_garment_mirror = BoolProperty(
        name="Mirror", description="Draw and edit symmetric pieces across the board's center line: new pieces get a "
        "twin on the other side (or, started on the line, are mirrored into one), and Edit Points moves the matching "
        "point too (M)",
        default=False, update=lambda _self, _context: _redraw())
    bpy.types.VIEW3D_HT_tool_header.append(draw_header)
    bpy.types.TOPBAR_MT_workspace_menu.append(draw_workspace_menu)
    _handles.append((bpy.types.SpaceView3D.draw_handler_add(_draw_view, (), 'WINDOW', 'POST_VIEW'), 'WINDOW'))
    _handles.append((bpy.types.SpaceView3D.draw_handler_add(_draw_labels, (), 'WINDOW', 'POST_PIXEL'), 'WINDOW'))
    # The Garment tab is in Phantom's startup file (made at runtime, it would switch workspaces at
    # startup, closing the splash); the workspace "+" menu adds it to older files.
    bpy.app.handlers.frame_change_post.append(_on_frame_change)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post, bpy.app.handlers.load_post):
        handlers.append(_after_undo)
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        # In "Frames", where Space plays the timeline: that keymap comes before the 3D View's.
        # Simulate only polls true in the Garment workspace, so Space plays the timeline elsewhere.
        km = kc.keymaps.new(name="Frames", space_type='EMPTY')
        _keymaps.append((km, km.keymap_items.new(garment.PHANTOM_OT_garment_simulate.bl_idname, 'SPACE', 'PRESS')))
    if not bpy.app.background:
        bpy.app.timers.register(_watch, first_interval=WATCH_INTERVAL, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()
    bpy.app.handlers.frame_change_post.remove(_on_frame_change)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post, bpy.app.handlers.load_post):
        if _after_undo in handlers:
            handlers.remove(_after_undo)
    for handle, region in _handles:
        bpy.types.SpaceView3D.draw_handler_remove(handle, region)
    _handles.clear()
    bpy.types.TOPBAR_MT_workspace_menu.remove(draw_workspace_menu)
    bpy.types.VIEW3D_HT_tool_header.remove(draw_header)
    del bpy.types.WindowManager.phantom_garment_show_silhouette
    del bpy.types.WindowManager.phantom_garment_mirror
    del bpy.types.WindowManager.phantom_garment_show_seams
    del bpy.types.WindowManager.phantom_garment_drag_radius
    del bpy.types.WindowManager.phantom_garment_tool
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
