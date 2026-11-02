# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: the simple sculpting view, laid out like ZBrush for iPad.

Shift+Tab in Sculpt Mode (or Simple in the 3D View header) swaps the usual Sculpt Mode interface
for a canvas-first one, so sculptors coming from ZBrush for iPad find things where they expect:

- The 3D View takes the window (it is maximized: the Properties editor, the Outliner and the status
  bar step aside) and its header, tool header, toolbar, sidebar and asset shelf slide away.
- **Top bar** (the window's top bar): Home (the File menu), Swap (back to the usual interface),
  Save, Import and Export on the left, the file's name in the middle, Undo History, Customize,
  Help and Settings on the right.
- **Left toolbar**: Tools (every Sculpt Mode tool by kind, and the brush library; it shows the
  brush while a brush tool is on), the stroke, alpha and texture, the matcap and the colors (each
  opens its picker: the matcaps a grid by kind, the colors a card with a hue ring around a square
  or triangle, hue/saturation/value sliders, swatches and Fill Object), then rings showing RGB
  Intensity, Focal Shift, Draw Size and Z Intensity (with Add or Sub) as arcs, then Undo and Redo. Drag a ring up or down to change its value (a slider shows
  beside it); click it for the same slider to drag, with the ring's options as pills next to it;
  right-click it for the Quick Menu with all four.
- **Right toolbar**: Palettes (the sidebar, on the right here) and Scene, the meshes in it (its +
  adds a primitive to sculpt, still sculpting: a gizmo places it, then Accept or Cancel; each
  mesh's trash icon deletes it, after asking), then Render,
  Perspective, Floor, Symmetry, Frame, PolyFrame, Transparency and Solo.
- **Bottom toolbar**: the quick brushes (their + slides in a row of every brush; the one picked
  joins them), then Move/Transpose, then Remesh, Dyntopo, Quad Remesh and
  Divide.
- The silhouette thumbnail sits in the top-left corner with the scene statistics under it, and the
  navigation gizmo in the top-right one, where ZBrush for iPad has its thumbnail and camera.

Clicking acts. Right-clicking, or holding the button down, gives a button's options. What a button
opens pops out beside its toolbar, not over it. Everything
eases: the regions slide away, then the toolbars slide in from their edges, their buttons one after
another. Swap or Shift+Tab reverses it.

Phantom remembers the view used last (``phantom_simple_view.json`` in the user config) and opens
Sculpt Mode in it. The usual layout is kept on the screen (a custom property), so going back
restores it exactly, also after a file saved in the simple view is opened again.
"""

import contextlib
import json
import math
import os
import random
import time

import blf
import bpy
import gpu
import numpy as np
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Menu, Operator, Panel, UIList
from gpu_extras.batch import batch_for_shader
from mathutils import Color

# On the maximized screen of the simple view.
MARKER = "phantom_simple_view"
# On the usual screen while it is replaced: its 3D View's layout (JSON).
SNAPSHOT = "phantom_simple_normal"

# Unscaled pixels.
ITEM = 38
ITEM_GAP = 6
BAR_PAD = 7
BAR_MARGIN = 10
BAR_CORNER = 12
GROUP_GAP = 12
RING_RADIUS = 14
RING_WIDTH = 2.4
ICON_SIZE = 18
# Room under the silhouette (or the top) for the viewport's text info and statistics.
TEXT_ROOM = 112
# Under the navigation gizmo: its ball plus the zoom, pan, camera and projection buttons.
GIZMO_BUTTONS_ROOM = 150
TOOLTIP_DELAY = 0.55
HOLD_TIME = 0.45
DRAG_THRESHOLD = 4

# Phantom's region slide (screen_ops.cc), and the toolbars' own easing.
REGION_SLIDE = 0.30
BAR_IN = 0.30
BAR_OUT = 0.18
STAGGER = 0.018
# When each toolbar starts sliding in: left, right, bottom.
BAR_DELAYS = (0.0, 0.04, 0.08)

BAR_COLOR = (0.105, 0.105, 0.115, 0.93)
OUTLINE_COLOR = (1.0, 1.0, 1.0, 0.06)
DIVIDER_COLOR = (1.0, 1.0, 1.0, 0.12)
HOVER_COLOR = (1.0, 1.0, 1.0, 0.09)
PRESS_COLOR = (1.0, 1.0, 1.0, 0.16)
# Phantom orange, which is also what ZBrush for iPad marks active things with.
ACCENT = (1.0, 0.62, 0.34, 1.0)
RING_TRACK = (1.0, 1.0, 1.0, 0.16)
TEXT_COLOR = (0.92, 0.92, 0.95, 0.92)
DIM_TEXT_COLOR = (0.92, 0.92, 0.95, 0.45)
TIP_COLOR = (0.07, 0.07, 0.08, 0.96)
PILL_COLOR = (1.0, 1.0, 1.0, 0.05)
PILL_TEXT_COLOR = (0.92, 0.92, 0.95, 0.72)

# Primitives the Scene popup's + adds: (kind, name, icon).
PRIMITIVES = (
    ('SPHERE', "Sphere", 'MESH_UVSPHERE'),
    ('CUBE', "Cube", 'MESH_CUBE'),
    ('CYLINDER', "Cylinder", 'MESH_CYLINDER'),
    ('CONE', "Cone", 'MESH_CONE'),
    ('TORUS', "Torus", 'MESH_TORUS'),
    ('ICO_SPHERE', "Ico Sphere", 'MESH_ICOSPHERE'),
    ('PLANE', "Plane", 'MESH_GRID'),
    ('MONKEY', "Monkey", 'MESH_MONKEY'),
)

# A ring's card (its slider, and its options as pills), unscaled pixels.
CARD_TRACK_W = 30
CARD_TRACK_H = 150
CARD_GAP = 18
CARD_PAD = 6
CARD_PILL_W = 64
# Pills tall enough to hit with a pen or a finger.
CARD_PILL_H = 32
CARD_TEXT = 10
# The ring card's column: its squares (the value, Pressure, Add and Sub, Scene) and the space around
# and between them.
CARD_SQUARE = 44
CARD_INSET = 8
# The Materials card, unscaled pixels on a 4 px grid: padding, the gap between its parts, the left
# column (the matcap in use, then the kinds) and the gap after it, a grid tile (and its label, the
# gap between tiles, the columns), the title row, the matcap-in-use box, a kind row (and the gap
# between rows), and the least height of the grid.
MAT_PAD = 16
MAT_GAP = 12
MAT_LEFT = 160
MAT_SPLIT = 16
MAT_TILE = 88
MAT_LABEL = 20
MAT_TILE_GAP = 8
MAT_COLUMNS = 3
MAT_TITLE = 24
MAT_CURRENT = 184
MAT_ROW = 32
MAT_ROW_GAP = 4
MAT_MIN_GRID = 360
# The brush strip: padding, a brush tile (and its label), the gap between tiles and under the
# header, the header, the least width, and the gap above the bottom toolbar.
STRIP_PAD = 12
STRIP_TILE = 76
STRIP_LABEL = 20
STRIP_GAP = 8
STRIP_HEADER = 20
STRIP_MIN_W = 480
STRIP_ABOVE = 12
# What the ring card's squares do, shown when hovered: by (ring, option) or by option.
CARD_TIPS = {
    ('SIZE', 'PRESSURE'): "Pen pressure: press harder for a bigger brush",
    'PRESSURE': "Pen pressure: press harder for a stronger stroke",
    'SCENE': "Scene size: the brush keeps its size on the model when you zoom (off: on the screen)",
    'ADD': "Add: the brush builds the surface up",
    'SUB': "Sub: the brush carves the surface in (Alt switches while held)",
}

# The color card, unscaled pixels on a 4 px grid: its padding, the wheel's width (the card's inside),
# a row of buttons, a section label, swatches, a randomize slider, Fill Object, the gap between
# sections, the hue ring and the gap inside it.
COLOR_PAD = 16
COLOR_INNER = 232
ROW = 32
LABEL = 20
SWATCH = 32
SWATCH_GAP = 8
SWATCH_COLUMNS = 6
SWATCH_ROWS = 3
SLIDER = 28
FILL_H = 36
SECTION_GAP = 16
RING_W = 20
WHEEL_GAP = 8
# The swatches a new palette starts with (display colors): grays, then skin, clay and a few hues.
SWATCH_DEFAULTS = (
    (1.0, 1.0, 1.0), (0.62, 0.62, 0.62), (0.25, 0.25, 0.25), (0.05, 0.05, 0.05),
    (0.93, 0.76, 0.64), (0.78, 0.53, 0.42), (0.66, 0.42, 0.33), (0.46, 0.29, 0.22),
    (0.80, 0.22, 0.20), (0.95, 0.70, 0.25), (0.30, 0.55, 0.35), (0.25, 0.42, 0.70),
)
# The color's sliders under the wheel: (key in the card's state, label). Their tracks are a bar of
# this height, after a label column this wide and before a value column this wide.
HSV_SLIDERS = (("h", "H"), ("s", "S"), ("v", "V"))
HSV_TRACK = 12
HSV_LABEL_W = 20
HSV_VALUE_W = 40

# Unscaled pixels before the file's name in the simple view's top bar, so it is in the middle of the
# window (measured; the bar's right part is added to it).
TOP_BAR_TITLE_PAD = 19

# The New Folder and Rename dialogs' width (unscaled pixels).
NAME_DIALOG_WIDTH = 280
# A folder's meshes are this many separator widths in (6 unscaled pixels each: about 16 px).
SCENE_INDENT = 2.75

PAINT_TYPES = {'PAINT', 'SMEAR'}
BRUSH_SHELF = "VIEW3D_AST_brush_sculpt"

# The toolbars' buttons, in order; None is a divider.
LEFT_KEYS = ('TOOLS', 'STROKE', 'ALPHA', 'TEXTURE', 'MATCAP', 'COLOR', None,
             'RGB', 'FOCAL', 'SIZE', 'STRENGTH', 'UNDO', 'REDO')
RIGHT_GROUPS = (('PALETTES', 'SUBTOOLS'),
                ('RENDER', 'PERSP', 'FLOOR', 'FRAME', 'POLYFRAME', 'XRAY', 'SOLO'))
BOTTOM_TOOLS = ('GIZMO', None, 'REMESH', 'DYNTOPO', 'QUAD', 'DIVIDE')
RING_KEYS = {'RGB', 'FOCAL', 'SIZE', 'STRENGTH'}
# The rings in the Quick Menu's order, with their names.
RING_NAMES = {'SIZE': "Draw Size", 'FOCAL': "Focal Shift", 'STRENGTH': "Z Intensity",
              'RGB': "RGB Intensity"}

ICONS = {
    'UNDO': 'LOOP_BACK',
    'REDO': 'LOOP_FORWARDS',
    'PALETTES': 'MENU_PANEL',
    'SUBTOOLS': 'OUTLINER_OB_MESH',
    'RENDER': 'RENDER_STILL',
    'FLOOR': 'GRID',
    'FRAME': 'FULLSCREEN_ENTER',
    'POLYFRAME': 'MOD_WIREFRAME',
    'XRAY': 'XRAY',
    'GIZMO': 'EMPTY_ARROWS',
    'REMESH': 'MOD_REMESH',
    'DYNTOPO': 'MOD_TRIANGULATE',
    'QUAD': 'MESH_GRID',
    'DIVIDE': 'MOD_SUBSURF',
    'ADD_SLOT': 'ADD',
}

TIPS = {
    'TOOLS': "Tools: {tool} ({brush}). Click for every Sculpt Mode tool and the brush library, "
             "right-click for the brush's settings",
    'STROKE': "Stroke: how the brush is laid down ({stroke}). Click for its settings",
    'ALPHA': "Alpha: the grayscale image shaping the brush. Click to pick or edit it",
    'TEXTURE': "Texture: sculpt brushes don't use a color texture (their image is the Alpha)",
    'MATCAP': "Materials: the matcap the model is shown with. Click to pick another",
    'COLOR': "Color: the paint colors. Click for the color wheel, swatches and Fill Object, "
             "right-click to swap them",
    'RGB': "RGB Intensity: how strongly paint brushes paint color. Drag up or down, click for its "
           "slider, right-click for the Quick Menu",
    'FOCAL': "Focal Shift: how soft the brush's edge is. Drag up or down, click for its slider, "
             "right-click for the Quick Menu",
    'SIZE': "Draw Size: the brush size (S). Drag up or down, click for its slider, right-click "
            "for the Quick Menu",
    'STRENGTH': "Z Intensity: how strongly the brush sculpts (Shift F), and Add or Sub (Alt "
                "reverses it while held). Drag up or down, click for its slider, right-click "
                "to switch Add and Sub",
    'UNDO': "Undo (Ctrl Z)",
    'REDO': "Redo (Ctrl Shift Z)",
    'PALETTES': "Palettes: every brush and sculpting setting, in the sidebar (N)",
    'SUBTOOLS': "Scene: the meshes in the scene, in folders. Pick one to sculpt it; add, duplicate, "
                "merge, split, boolean or align them",
    'RENDER': "Render: a picture of the viewport as it is now",
    'PERSP': "Perspective: switch between perspective and orthographic (Numpad 5)",
    'FLOOR': "Grid: show or hide the grid (limited or infinite: in Overlays)",
    'FRAME': "Frame: fit the model in the view (Home)",
    'POLYFRAME': "PolyFrame: show the polygroups and the wireframe (Ctrl F)",
    'XRAY': "Transparency: see through the model (Alt Z)",
    'SOLO': "Solo: show only the active mesh (Numpad /)",
    'GIZMO': "Move, rotate or scale the model with the gizmo or Transpose (W)",
    'REMESH': "Remesh: rebuild the mesh with even polygons. Right-click for the voxel size",
    'DYNTOPO': "Dyntopo: add detail where you sculpt. Right-click for its settings",
    'QUAD': "Quad Remesh: rebuild the mesh with clean quads",
    'DIVIDE': "Divide: subdivide the mesh one level (Ctrl D)",
    'ADD_SLOT': "Brushes: every sculpt brush in a row. Pick one to add it to these quick brushes",
}

# Buttons with options (right-click or hold): what they open.
OPTIONS = {
    'TOOLS': ('PANEL', "VIEW3D_PT_tools_brush_settings"),
    'COLOR': ('FLIP_COLORS', None),
    'RGB': ('PANEL', "VIEW3D_PT_phantom_simple_quick_menu"),
    'FOCAL': ('PANEL', "VIEW3D_PT_phantom_simple_quick_menu"),
    'SIZE': ('PANEL', "VIEW3D_PT_phantom_simple_quick_menu"),
    'STRENGTH': ('FLIP_DIRECTION', None),
    'FLOOR': ('PANEL', "VIEW3D_PT_overlay"),
    'XRAY': ('PANEL', "VIEW3D_PT_shading"),
    'REMESH': ('PANEL', "VIEW3D_PT_sculpt_voxel_remesh"),
    'DYNTOPO': ('PANEL', "VIEW3D_PT_sculpt_dyntopo"),
    'QUAD': ('PANEL', "VIEW3D_PT_sculpt_quad_remesh"),
}

_settings = {
    "on": False,
    "left_handed": False,
    "right_bar": True,
    "bottom_bar": True,
    "stats": True,
    # The color card's picker inside the hue ring: 'SQUARE' or 'TRIANGLE'.
    "color_shape": 'SQUARE',
    # The color card stays open while sculpting (and opens with the simple view).
    "color_pinned": False,
}
_draw_handle = None
_addon_keymaps = []
# Windows switching views right now: window pointer -> "ENTER" or "LEAVE".
_busy = {}
# When each of them started switching: a switch taking far longer than its animation failed part
# way (an error in a timer), and must not lock the window out of the simple view.
_busy_at = {}
BUSY_TIMEOUT = 5.0
# Windows that just left the simple view: window pointer -> time (no automatic entry right away).
_left_at = {}
# The toolbars' last slide: when it started, and 1 coming in or -1 going out.
_bars = {"start": 0.0, "dir": 1}
# The button under the cursor and since when, the pressed one, and a ring being dragged.
_hover = {"key": None, "since": 0.0, "bar": False}
_pressed = {"key": None}
_scrub = {"key": None}
# The ring whose card was opened by a click, the pill under the cursor or pressed, and whether its
# slider is being dragged.
_card = {"key": None, "hover": None, "since": 0.0, "pressed": None, "dragging": False}
# The color card: whether it is open, its hue, saturation and value (display space) and the brush
# color they were last in step with, the part under the cursor or pressed, what is being dragged,
# and whether the eyedropper waits for a click on the mesh.
_color_card = {"open": False, "h": 0.0, "s": 0.0, "v": 1.0, "linear": None, "hover": None,
               "pressed": None, "dragging": None, "picking": False}
# The Materials card: whether it is open, the kind shown, the search text and whether it is being
# typed in, how far the grid is scrolled, the part under the cursor or pressed, and a drag
# scrolling the grid (its start: cursor y and scroll).
_mat = {"open": False, "kind": "", "search": "", "focus": False, "scroll": 0.0, "hover": None,
        "pressed": None, "drag": None}
# The brush strip: whether it is open, how far it is scrolled, the brush under the cursor or
# pressed, and a drag scrolling it (its start: cursor x and scroll, and whether it moved).
_strip = {"open": False, "scroll": 0.0, "hover": None, "pressed": None, "drag": None}
# A primitive just added from the Scene popup, being placed with the gizmo: its object's name, its
# vertex positions when added (for Cancel), the tool to go back to, and the pill under the cursor
# or pressed on the placement card.
_place = {"object": None, "positions": None, "tool": None, "hover": None, "pressed": None}
# The brush circle and cursor are hidden while the cursor is over a toolbar.
_cursor = {"hidden": False, "modal": False}


# -----------------------------------------------------------------------------
# Settings

def _settings_path():
    return os.path.join(bpy.utils.user_resource('CONFIG'), "phantom_simple_view.json")


def _load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    for key, value in _settings.items():
        if isinstance(data.get(key), type(value)):
            _settings[key] = data[key]


def _save_settings():
    path = _settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_settings, fh, indent=2)
    except OSError as ex:
        print("Phantom: could not save the simple view settings:", ex)


def _set_setting(key, value):
    if _settings[key] != value:
        _settings[key] = value
        _save_settings()


# -----------------------------------------------------------------------------
# Windows, areas and regions

def _ui_scale():
    return bpy.context.preferences.system.ui_scale


def _window_by_pointer(pointer):
    wm = bpy.context.window_manager
    for window in wm.windows if wm else ():
        if window.as_pointer() == pointer:
            return window
    return None


def _region(area, region_type):
    return next((region for region in area.regions if region.type == region_type), None)


def _view3d_area(screen):
    """The screen's largest 3D View."""
    areas = [area for area in screen.areas if area.type == 'VIEW_3D']
    return max(areas, key=lambda area: area.width * area.height, default=None)


def _sculpting(window):
    ob = window.view_layer.objects.active if window.view_layer else None
    return ob is not None and ob.mode == 'SCULPT'


def is_simple_screen(screen):
    return screen is not None and bool(screen.get(MARKER))


def manages(window):
    """Whether the simple view decides about the 3D View's sidebar in this window (it is shown, being
    switched to or away from, or about to be shown)."""
    screen = window.screen
    if is_simple_screen(screen) or window.as_pointer() in _busy:
        return True
    return (_settings["on"] and _sculpting(window) and screen is not None and
            not screen.is_temporary and _view3d_area(screen) is not None)


def _motion_ok():
    prefs = bpy.context.preferences
    return prefs.system.use_region_overlap and not getattr(prefs.view, "use_reduce_motion", False)


def _regions_sliding(area):
    return any(getattr(region, "phantom_slide", 1.0) < 1.0 for region in area.regions)


def _redraw_simple():
    wm = bpy.context.window_manager
    for window in wm.windows if wm else ():
        if is_simple_screen(window.screen):
            for area in window.screen.areas:
                area.tag_redraw()


def _play(name, pitch=1.0):
    try:
        bpy.types.WindowManager.phantom_ui_sound_play(name, pitch=pitch)
    except (AttributeError, TypeError, RuntimeError):
        pass


# Region visibility, by the space's property and the snapshot's key.
REGION_PROPS = (
    ("header", "show_region_header"),
    ("tool_header", "show_region_tool_header"),
    ("toolbar", "show_region_toolbar"),
    ("ui", "show_region_ui"),
    ("asset_shelf", "show_region_asset_shelf"),
)


def _set_regions(window, area, shown, animate=True, force=False):
    """Show or hide the 3D View's regions (`shown`: snapshot key -> bool). With the area as the
    context, Phantom slides them in or out. `force`: set them even where they already read right
    (the tool header reads hidden as soon as it is hidden by the user, even while still shown)."""
    space = area.spaces.active
    region = _region(area, 'WINDOW')

    def apply():
        for key, prop in REGION_PROPS:
            if key in shown and hasattr(space, prop) and (force or getattr(space, prop) != shown[key]):
                try:
                    setattr(space, prop, shown[key])
                except AttributeError:
                    # Read-only in this mode (the asset shelf, outside the modes that have one).
                    pass

    if animate:
        with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
            apply()
    else:
        apply()


def _flip_sidebar(window, area, alignment):
    region = _region(area, 'UI')
    if region is None or region.alignment == alignment or region.alignment not in {'LEFT', 'RIGHT'}:
        return False
    with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
        bpy.ops.screen.region_flip()
    return True


# -----------------------------------------------------------------------------
# Switching views

def enter(window, area):
    """Switch `window` to the simple view, around its 3D View `area`."""
    pointer = window.as_pointer()
    screen = window.screen
    if pointer in _busy or is_simple_screen(screen) or screen.is_temporary:
        return False
    space = area.spaces.active
    sidebar = _region(area, 'UI')
    snapshot = {key: bool(getattr(space, prop, False)) for key, prop in REGION_PROPS}
    snapshot.update(
        area=list(screen.areas).index(area),
        stats=space.overlay.show_stats,
        sidebar=sidebar.alignment if sidebar is not None else 'RIGHT',
        statusbar=screen.show_statusbar,
        floor=[space.overlay.show_floor, space.overlay.show_axis_x, space.overlay.show_axis_y],
    )
    screen[SNAPSHOT] = json.dumps(snapshot)
    _busy[pointer] = "ENTER"
    _busy_at[pointer] = time.perf_counter()
    animate = _motion_ok()
    _play("toggle_on")
    _set_regions(window, area, {key: False for key, _prop in REGION_PROPS}, animate=animate)
    tries = [0]

    def maximize():
        window = _window_by_pointer(pointer)
        if window is None:
            _busy.pop(pointer, None)
            return None
        area = _view3d_area(window.screen)
        # Maximizing swaps the area's regions into a new screen: never while a region still slides.
        if area is not None and _regions_sliding(area) and tries[0] < 40:
            tries[0] += 1
            return 0.02
        _maximize(window, area)
        return None

    bpy.app.timers.register(maximize, first_interval=REGION_SLIDE + 0.03 if animate else 0.0)
    return True


def _maximize(window, area):
    pointer = window.as_pointer()
    screen = window.screen
    try:
        with bpy.context.temp_override(window=window, screen=screen, area=area,
                                       region=_region(area, 'WINDOW')):
            bpy.ops.screen.screen_full_area(use_hide_panels=False)
    except (RuntimeError, TypeError) as ex:
        print("Phantom: could not open the simple view:", ex)
    temp = window.screen
    if temp == screen:
        _busy.pop(pointer, None)
        _restore_layout(window, screen, animate=True)
        return
    temp[MARKER] = True
    temp.show_statusbar = False
    new_area = _view3d_area(temp)
    if new_area is not None:
        space = new_area.spaces.active
        _set_regions(window, new_area, {key: False for key, _prop in REGION_PROPS}, animate=False,
                     force=True)
        # The sidebar opens on the right here, as palettes do, clear of the left toolbar.
        _flip_sidebar(window, new_area, 'RIGHT')
        if _settings["stats"]:
            space.overlay.show_stats = True
    # The simple view's own grid (limited at first) right away, not at the next check.
    try:
        import phantom_viewport
        phantom_viewport.refresh_grid()
    except (ImportError, AttributeError):
        pass
    _start_bars(1)

    def done():
        _busy.pop(pointer, None)
        if _settings["color_pinned"] and not _color_card["open"]:
            _open_pinned_color_card(pointer)
        return None

    bpy.app.timers.register(done, first_interval=_bars_duration())


def leave(window, turn_off=True):
    """Go back to the usual interface. `turn_off`: Sculpt Mode opens in it from now on."""
    pointer = window.as_pointer()
    if pointer in _busy or not is_simple_screen(window.screen):
        return False
    if turn_off:
        _set_setting("on", False)
    _busy[pointer] = "LEAVE"
    _busy_at[pointer] = time.perf_counter()
    _left_at[pointer] = time.perf_counter()
    _show_cursor(bpy.context)
    _play("toggle_off")
    animate = _motion_ok()
    _start_bars(-1)
    area = _view3d_area(window.screen)
    if area is not None and area.spaces.active.show_region_ui:
        # An open sidebar (Palettes) slides away with the toolbars.
        _set_regions(window, area, {"ui": False}, animate=animate)
    tries = [0]

    def restore():
        window = _window_by_pointer(pointer)
        if window is None:
            _busy.pop(pointer, None)
            return None
        area = _view3d_area(window.screen)
        if area is not None and _regions_sliding(area) and tries[0] < 40:
            tries[0] += 1
            return 0.02
        _unmaximize(window, area)
        return None

    bpy.app.timers.register(restore, first_interval=BAR_OUT if animate else 0.0)
    return True


def _unmaximize(window, area):
    pointer = window.as_pointer()
    temp = window.screen
    if area is not None and area.spaces.active.show_region_ui:
        # The sidebar goes back hidden; the usual layout shows it again if it was on.
        area.spaces.active.show_region_ui = False
    try:
        with bpy.context.temp_override(window=window, screen=temp, area=area,
                                       region=_region(area, 'WINDOW') if area else None):
            bpy.ops.screen.back_to_previous()
    except (RuntimeError, TypeError) as ex:
        print("Phantom: could not leave the simple view:", ex)
        temp.pop(MARKER, None)
    _restore_layout(window, window.screen, animate=_motion_ok())

    def done():
        _busy.pop(pointer, None)
        return None

    bpy.app.timers.register(done, first_interval=REGION_SLIDE + 0.05)


def _restore_layout(window, screen, animate):
    """Put the usual screen's 3D View back as the snapshot says, and forget the snapshot."""
    try:
        snapshot = json.loads(screen.get(SNAPSHOT, "{}"))
    except (TypeError, ValueError):
        snapshot = {}
    screen.pop(SNAPSHOT, None)
    # Hiding it on the maximized screen hides it on this one too.
    if screen.show_statusbar != snapshot.get("statusbar", True):
        screen.show_statusbar = snapshot.get("statusbar", True)
    areas = list(screen.areas)
    index = snapshot.get("area", -1)
    area = areas[index] if 0 <= index < len(areas) and areas[index].type == 'VIEW_3D' else None
    area = area or _view3d_area(screen)
    if area is None:
        return
    space = area.spaces.active
    if "stats" in snapshot:
        space.overlay.show_stats = snapshot["stats"]
    if "floor" in snapshot:
        # The simple view's grid may have put the floor away (the limited grid stands in for it).
        overlay = space.overlay
        overlay.show_floor, overlay.show_axis_x, overlay.show_axis_y = snapshot["floor"]
    sidebar = _region(area, 'UI')
    if sidebar is not None and not space.show_region_ui:
        _flip_sidebar(window, area, snapshot.get("sidebar", sidebar.alignment))
    shown = {key: snapshot.get(key, key != "ui") for key, _prop in REGION_PROPS}
    _set_regions(window, area, shown, animate=animate)


def _watch():
    """Leave the simple view when Sculpt Mode ends, open it when Sculpt Mode starts (if it was the
    view used last), and tidy up when the maximized screen was left some other way."""
    wm = bpy.context.window_manager
    now = time.perf_counter()
    for window in wm.windows if wm else ():
        pointer = window.as_pointer()
        screen = window.screen
        if pointer in _busy and now - _busy_at.get(pointer, now) > BUSY_TIMEOUT:
            # A switch that never finished (an error part way): don't stay locked out.
            print("Phantom: the simple view's switch didn't finish; carrying on")
            _busy.pop(pointer, None)
            _busy_at.pop(pointer, None)
        if pointer in _busy or screen is None:
            continue
        sculpting = _sculpting(window)
        if is_simple_screen(screen):
            _sync_scene_rows()
            if not sculpting:
                leave(window, turn_off=False)
            continue
        if screen.get(SNAPSHOT) is not None and not screen.is_temporary:
            # Back to the usual screen without Home (e.g. Ctrl Space): put its layout back.
            _set_setting("on", False)
            _restore_layout(window, screen, animate=_motion_ok())
            continue
        if (_settings["on"] and sculpting and not screen.is_temporary and
                now - _left_at.get(pointer, -10.0) > 1.0):
            area = _view3d_area(screen)
            if area is not None:
                enter(window, area)
    return 0.25


# -----------------------------------------------------------------------------
# The toolbars' slide

def _bars_duration():
    longest = max(len(LEFT_KEYS), sum(len(group) for group in RIGHT_GROUPS))
    return BAR_DELAYS[-1] + BAR_IN + STAGGER * longest + 0.05


def _anim_tick():
    _redraw_simple()
    elapsed = time.perf_counter() - _bars["start"]
    duration = _bars_duration() if _bars["dir"] > 0 else BAR_OUT
    return 1.0 / 60.0 if elapsed < duration + 0.05 else None


def _start_bars(direction):
    _bars["start"] = time.perf_counter() if _motion_ok() else 0.0
    _bars["dir"] = direction
    if not bpy.app.timers.is_registered(_anim_tick):
        bpy.app.timers.register(_anim_tick, first_interval=0.0)


def _progress(bar, item=0):
    """How far toolbar `bar` (0 left, 1 right, 2 bottom) and its button `item` have come in: 0..1."""
    elapsed = time.perf_counter() - _bars["start"]
    if _bars["dir"] > 0:
        t = (elapsed - BAR_DELAYS[bar] - item * STAGGER) / BAR_IN
        t = min(max(t, 0.0), 1.0)
        return 1.0 - (1.0 - t) ** 3
    t = min(max(elapsed / BAR_OUT, 0.0), 1.0)
    return 1.0 - t ** 3


# -----------------------------------------------------------------------------
# Brush values

def _paint(context):
    return context.tool_settings.sculpt


def _brush(context):
    return _paint(context).brush


def _owner(context, brush, unified):
    if getattr(brush, unified, False):
        return _paint(context).unified_paint_settings
    return brush


def _is_paint_brush(brush):
    return brush is not None and getattr(brush, "sculpt_brush_type", None) in PAINT_TYPES


SIZE_MAX = 500.0


def ring_value(context, key):
    """(value, fraction of the ring, text, active) of a ring."""
    brush = _brush(context)
    if brush is None:
        return 0.0, 0.0, "", False
    if key == 'SIZE':
        size = _owner(context, brush, "use_unified_size").size
        fraction = math.log(max(size, 2) / 2.0) / math.log(SIZE_MAX / 2.0)
        return size, min(max(fraction, 0.0), 1.0), str(int(size)), True
    if key == 'FOCAL':
        return brush.hardness, brush.hardness, str(round(brush.hardness * 100)), True
    strength = _owner(context, brush, "use_unified_strength").strength
    active = _is_paint_brush(brush) if key == 'RGB' else not _is_paint_brush(brush)
    return strength, min(strength, 1.0), f"{strength:.2f}", active


def _set_ring(context, key, value):
    brush = _brush(context)
    if brush is None:
        return
    if key == 'SIZE':
        _owner(context, brush, "use_unified_size").size = int(round(min(max(value, 1), 5000)))
    elif key == 'FOCAL':
        brush.hardness = min(max(value, 0.0), 1.0)
    elif key == 'RGB' and not _is_paint_brush(brush):
        # Only paint brushes paint color.
        return
    else:
        _owner(context, brush, "use_unified_strength").strength = min(max(value, 0.0), 1.0)


def _scrubbed(key, start, dy):
    """The value after dragging `dy` pixels up from `start`."""
    s = _ui_scale()
    if key == 'SIZE':
        return max(1.0, start * 2.0 ** (dy / (90.0 * s)))
    return start + dy / (220.0 * s)


def _direction_is_add(brush):
    prop = brush.bl_rna.properties.get("direction") if brush else None
    if prop is None or not prop.enum_items:
        return None
    return brush.direction == prop.enum_items[0].identifier


# -----------------------------------------------------------------------------
# Layout

class Item:
    __slots__ = ("key", "bar", "index", "x", "y", "w", "h")

    def __init__(self, key, bar, index, x, y, w, h):
        self.key, self.bar, self.index = key, bar, index
        self.x, self.y, self.w, self.h = x, y, w, h

    def hit(self, mx, my):
        return self.x <= mx <= self.x + self.w and self.y <= my <= self.y + self.h


class Layout:
    """Where the toolbars and their buttons go, in region pixels (without the slide)."""

    def __init__(self, context, area, region):
        s = _ui_scale()
        self.s = s
        self.items = []
        self.dividers = []   # (bar, x0, y0, x1, y1)
        self.bars = []       # (bar, x, y, w, h)
        W, H = region.width, region.height
        left_inset, right_inset = _side_insets(area, region)
        top = H - _top_inset(area, region)
        margin = BAR_MARGIN * s

        # What sits in the top corners decides how high the side toolbars may go.
        sil = _silhouette_rect(context)
        if sil is not None and sil[1] > H / 2:
            corner_left = sil[1] - TEXT_ROOM * s
        else:
            corner_left = top - TEXT_ROOM * s
        corner_right = top - _gizmo_room(context, s)

        # The brush toolbar on the left and the view toolbar on the right (swapped left-handed).
        left_x = left_inset + margin
        brush_left = not _settings["left_handed"]
        self._column(0, [list(LEFT_KEYS)], left_x if brush_left else None,
                     corner_left if brush_left else corner_right, margin, W, right_inset)
        if _settings["right_bar"]:
            self._column(1, [list(group) for group in RIGHT_GROUPS], None if brush_left else left_x,
                         corner_right if brush_left else corner_left, margin, W, right_inset)

        # Bottom toolbar: the quick brushes, then the tools.
        if _settings["bottom_bar"]:
            self._bottom(context, W, margin)

    def _column(self, bar, groups, x, top_limit, margin, W, right_inset):
        """A vertical toolbar of one pill per group (`x` None: against the right edge)."""
        s = self.s
        pad = BAR_PAD * s
        heights = [2 * pad + sum((ITEM if key else ITEM_GAP) * s for key in group) +
                   ITEM_GAP * s * (len(group) - 1) for group in groups]
        total = sum(heights) + GROUP_GAP * s * (len(groups) - 1)
        # Smaller buttons when the viewport is too short for them.
        k = min(1.0, max(top_limit - margin, 1) / total) if total > 0 else 1.0
        item, gap, group_gap = ITEM * s * k, ITEM_GAP * s * k, GROUP_GAP * s * k
        width = item + 2 * pad
        if x is None:
            x = W - right_inset - margin - width
        heights = [2 * pad + (h - 2 * pad) * k for h in heights]
        total = sum(heights) + group_gap * (len(groups) - 1)
        # Centered on the viewport's height, but below whatever is in the corner above.
        y_top = min(top_limit, (top_limit + margin) / 2 + total / 2)
        y_top = max(y_top, margin + total)
        index = 0
        for group, height in zip(groups, heights):
            self.bars.append((bar, x, y_top - height, width, height))
            y = y_top - pad
            for i, key in enumerate(group):
                if i:
                    y -= gap
                if key is None:
                    self.dividers.append((bar, x + pad + item * 0.15, y - gap / 2,
                                          x + pad + item * 0.85, y - gap / 2))
                    y -= gap
                    continue
                y -= item
                self.items.append(Item(key, bar, index, x + pad, y, item, item))
                index += 1
            y_top -= height + group_gap

    def _bottom(self, context, W, margin):
        import phantom_quick_brushes as quick_brushes
        s = self.s
        slots = quick_brushes.slots()
        keys = [f"SLOT{i}" for i in range(len(slots))] + ['ADD_SLOT', None] + list(BOTTOM_TOOLS)
        pad = BAR_PAD * s
        item = ITEM * s
        gap = ITEM_GAP * s
        count = sum(1 for key in keys if key)
        dividers = len(keys) - count
        width = 2 * pad + count * item + (len(keys) - 1) * gap + dividers * gap
        # Keep clear of the side toolbars.
        side = max((x + w for bar, x, y, w, h in self.bars if x < W / 2), default=0)
        right_side = min((x for bar, x, y, w, h in self.bars if x >= W / 2), default=W)
        available = W - 2 * max(side, W - right_side) - 2 * margin
        k = min(1.0, available / width) if width > 0 else 1.0
        item *= k
        gap *= k
        width *= k
        height = item + 2 * pad
        x = (W - width) / 2
        # Above the asset shelf when the + has it open (it overlaps the viewport).
        y = margin + _bottom_inset(context.area, context.region)
        self.bars.append((2, x, y, width, height))
        cx = x + pad
        index = 0
        for i, key in enumerate(keys):
            if i:
                cx += gap
            if key is None:
                self.dividers.append((2, cx + gap / 2, y + pad + item * 0.15, cx + gap / 2,
                                      y + pad + item * 0.85))
                cx += gap
                continue
            self.items.append(Item(key, 2, index, cx, y + pad, item, item))
            index += 1
            cx += item

    def hit(self, mx, my):
        """(button or None, whether the point is on a toolbar)."""
        for item in self.items:
            if item.hit(mx, my):
                return item, True
        on_bar = any(x <= mx <= x + w and y <= my <= y + h for _bar, x, y, w, h in self.bars)
        return None, on_bar


def _side_insets(area, region):
    """How far the toolbar and sidebar reach in from either side (with their slide)."""
    space = area.spaces.active
    left = right = 0
    for other in area.regions:
        if other.type not in {'TOOLS', 'UI'} or other.width <= 1:
            continue
        if other.type == 'TOOLS' and not space.show_region_toolbar:
            continue
        if other.type == 'UI' and not space.show_region_ui:
            continue
        width = round(other.width * getattr(other, "phantom_slide", 1.0))
        if other.alignment == 'LEFT':
            left = max(left, other.x + width - region.x)
        elif other.alignment == 'RIGHT':
            right = max(right, region.x + region.width - (other.x + other.width - width))
    return max(0, left), max(0, right)


def _bottom_inset(area, region):
    """How far the asset shelf (and its tabs) reach up over the viewport."""
    if area is None or region is None or not area.spaces.active.show_region_asset_shelf:
        return 0
    inset = 0
    for other in area.regions:
        if other.type in {'ASSET_SHELF', 'ASSET_SHELF_HEADER'} and other.height > 1 and \
                other.y < region.y + region.height / 2:
            inset = max(inset, other.y + other.height - region.y)
    return max(0, inset)


def _top_inset(area, region):
    top = region.y + region.height
    inset = 0
    for other in area.regions:
        if other.type in {'HEADER', 'TOOL_HEADER'} and other.alignment == 'TOP' and other.height > 1:
            inset = max(inset, top - other.y)
    return max(0, inset)


def _gizmo_room(context, s):
    space = context.space_data
    view = context.preferences.view
    if not (space.show_gizmo and space.show_gizmo_navigate):
        return BAR_MARGIN * s
    if view.mini_axis_type == 'GIZMO':
        return (view.gizmo_size_navigate_v3d + GIZMO_BUTTONS_ROOM) * s
    if view.mini_axis_type == 'MINIMAL':
        return (view.mini_axis_size + 40) * s
    return BAR_MARGIN * s


def _silhouette_rect(context):
    try:
        import phantom_silhouette
        return phantom_silhouette.visible_rect(context)
    except (ImportError, AttributeError):
        return None


def side_reserve(area, region):
    """Pixels at the left and right of the viewport the side toolbars use (for the history bar
    docked at a side), else zeros."""
    context = bpy.context
    if not _drawable(context) or context.region.as_pointer() != region.as_pointer():
        return 0, 0
    s = _ui_scale()
    W = region.width
    left = right = 0.0
    for bar, x, _y, w, _h in Layout(context, area, region).bars:
        if bar == 2:
            continue
        p = _progress(bar)
        if x + w / 2 < W / 2:
            left = max(left, (x + w + BAR_MARGIN * s) * p)
        else:
            right = max(right, (W - x + BAR_MARGIN * s) * p)
    return round(left), round(right)


def bottom_reserve(area, region):
    """Pixels at the bottom of the viewport the bottom toolbar uses (for the mask bar), else 0."""
    context = bpy.context
    if not _drawable(context) or not _settings["bottom_bar"]:
        return 0
    s = _ui_scale()
    height = (ITEM + 2 * BAR_PAD + 2 * BAR_MARGIN) * s + _bottom_inset(area, region)
    return round(height * _progress(2))


# -----------------------------------------------------------------------------
# Thumbnails: pixels are read in timers (data may change there), textures made while drawing.

_MISSING = object()
_RETRY = object()
_pixels = {}
_textures = {}
_pending = {}
_retries = {}


def _thumbnail(key, loader):
    texture = _textures.get(key)
    if texture is not None:
        return texture
    entry = _pixels.get(key, _MISSING)
    if entry is _MISSING:
        if key not in _pending:
            _pending[key] = loader
            if not bpy.app.timers.is_registered(_load_pending):
                bpy.app.timers.register(_load_pending, first_interval=0.0)
        return None
    if entry is None:
        return None
    width, height, data = entry
    texture = gpu.types.GPUTexture((width, height), format='RGBA16F',
                                   data=gpu.types.Buffer('FLOAT', len(data), data))
    _textures[key] = texture
    return texture


def _load_pending():
    retry = False
    # A few at a time (a grid of matcaps is dozens of EXRs): the rest next time.
    deadline = time.perf_counter() + 0.012
    for key, loader in list(_pending.items()):
        if time.perf_counter() > deadline:
            _redraw_simple()
            return 0.0
        del _pending[key]
        try:
            result = loader()
        except (RuntimeError, ReferenceError, KeyError, ValueError, OSError) as ex:
            print("Phantom: could not read a thumbnail:", ex)
            result = None
        if result is _RETRY:
            _retries[key] = _retries.get(key, 0) + 1
            if _retries[key] < 20:
                _pending[key] = loader
                retry = True
            else:
                _pixels[key] = None
            continue
        _pixels[key] = result
    _redraw_simple()
    return 0.3 if retry else None


def _premultiplied(width, height, data):
    rgba = data.reshape(-1, 4)
    rgba[:, :3] *= rgba[:, 3:4]
    return width, height, data


def _preview_pixels(preview):
    if preview is None:
        return _RETRY
    width, height = preview.image_size
    if width == 0 or height == 0:
        return _RETRY
    data = np.empty(width * height * 4, dtype=np.float32)
    preview.image_pixels_float.foreach_get(data)
    if not data.any():
        return _RETRY
    return _premultiplied(width, height, data)


def _image_pixels(path, linear=False, circle=False, size=96):
    """Pixels of an image file, made smaller, read without adding an image to the blend-file (the
    matcaps are multilayer EXRs, which images don't give pixels of)."""
    import imbuf
    ibuf = imbuf.load(path)
    try:
        with ibuf.with_buffer() as buffer:
            rgba = np.array(buffer)
    finally:
        ibuf.free()
    if rgba.dtype == np.uint8:
        rgba = rgba.astype(np.float32) / 255.0
    if rgba.ndim != 3 or rgba.shape[2] < 3:
        return None
    if rgba.shape[2] == 3:
        rgba = np.concatenate((rgba, np.ones(rgba.shape[:2] + (1,), dtype=np.float32)), axis=2)
    height, width = rgba.shape[:2]
    step = max(1, max(width, height) // size)
    rgba = np.ascontiguousarray(rgba[::step, ::step, :4])
    height, width = rgba.shape[:2]
    if linear:
        rgba[..., :3] = np.clip(rgba[..., :3], 0.0, 1.0) ** (1.0 / 2.2)
        rgba[..., 3] = 1.0
    if circle:
        yy, xx = np.mgrid[0:height, 0:width]
        radius = min(width, height) / 2.0
        dist = np.hypot(xx + 0.5 - width / 2.0, yy + 0.5 - height / 2.0)
        rgba[..., 3] *= np.clip(radius - dist, 0.0, 1.0)
    data = rgba.reshape(-1).astype(np.float32)
    return _premultiplied(width, height, data)


def _brush_texture(brush):
    def load():
        b = bpy.data.brushes.get(name)
        return _preview_pixels(b.preview_ensure() if b is not None else None)
    name = brush.name
    return _thumbnail(f"brush:{name}:{brush.session_uid}", load)


def _id_texture(idblock):
    def load():
        block = getattr(bpy.data, collection).get(name)
        return _preview_pixels(block.preview_ensure() if block is not None else None)
    name = idblock.name
    collection = "textures" if isinstance(idblock, bpy.types.Texture) else "images"
    return _thumbnail(f"{collection}:{name}:{idblock.session_uid}", load)


def _matcap_texture(name):
    def load():
        for light in bpy.context.preferences.studio_lights:
            if light.name == name and light.type == 'MATCAP' and os.path.exists(light.path):
                return _image_pixels(light.path, linear=True, circle=True)
        return None
    return _thumbnail(f"matcap:{name}", load)


def _slot_texture(slot):
    import phantom_quick_brushes as quick_brushes

    def load():
        try:
            path = quick_brushes._thumbnail_path(slot)
        except (AttributeError, KeyError):
            path = ""
        if path and os.path.exists(path):
            return _image_pixels(path, size=128)
        name = _slot_label(slot)
        brush = next((b for b in bpy.data.brushes if b.name == name and b.use_paint_sculpt), None)
        return _preview_pixels(brush.preview_ensure()) if brush is not None else None
    return _thumbnail("slot:" + "|".join(slot[k] for k in ("type", "identifier", "relative")), load)


def _catalog_texture(entry):
    """A brush of the brush strip (its thumbnail cached by phantom_brush_catalog)."""
    def load():
        path = entry.get("thumbnail")
        if not path or not os.path.exists(path):
            return None
        rgba = np.load(path).astype(np.float32)
        height, width = rgba.shape[:2]
        return _premultiplied(width, height, np.ascontiguousarray(rgba).reshape(-1))
    return _thumbnail("catalog:" + entry.get("thumbnail", entry["relative"]), load)


def _forget_thumbnails(*_args):
    _pixels.clear()
    _textures.clear()
    _pending.clear()
    _retries.clear()


# -----------------------------------------------------------------------------
# Drawing

_shader = None


def _uniform_shader():
    global _shader
    if _shader is None:
        _shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    return _shader


def _draw(kind, verts, color):
    shader = _uniform_shader()
    batch = batch_for_shader(shader, kind, {"pos": verts})
    shader.uniform_float("color", color)
    gpu.state.blend_set('ALPHA')
    batch.draw(shader)


def _rounded_rect(x, y, w, h, radius, color, segments=6):
    radius = max(0.0, min(radius, w / 2.0, h / 2.0))
    corners = ((x + w - radius, y + h - radius, 0.0), (x + radius, y + h - radius, 0.5 * math.pi),
               (x + radius, y + radius, math.pi), (x + w - radius, y + radius, 1.5 * math.pi))
    outline = []
    for cx, cy, start in corners:
        for i in range(segments + 1):
            a = start + 0.5 * math.pi * i / segments
            outline.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    _draw('TRI_FAN', [(x + w / 2.0, y + h / 2.0)] + outline + [outline[0]], color)
    return outline


def _outline(points, color, width=1.0):
    gpu.state.line_width_set(width)
    # A strip back to the first point: Vulkan has no line loops, so 'LINE_LOOP' loses the closing
    # side (the right edge of a rounded rectangle).
    _draw('LINE_STRIP', list(points) + [points[0]], color)
    gpu.state.line_width_set(1.0)


def _arc(cx, cy, radius, width, a0, a1, color):
    steps = max(2, int(abs(a1 - a0) / (2 * math.pi) * 48) + 1)
    inner, outer = radius - width / 2.0, radius + width / 2.0
    verts = []
    for i in range(steps + 1):
        a = a0 + (a1 - a0) * i / steps
        c, s = math.cos(a), math.sin(a)
        verts.append((cx + inner * c, cy + inner * s))
        verts.append((cx + outer * c, cy + outer * s))
    _draw('TRI_STRIP', verts, color)


def _disc(cx, cy, radius, color):
    points = [(cx + radius * math.cos(2 * math.pi * i / 24), cy + radius * math.sin(2 * math.pi * i / 24))
              for i in range(25)]
    _draw('TRI_FAN', [(cx, cy)] + points, color)


def _line(x0, y0, x1, y1, color, width=1.0):
    gpu.state.line_width_set(width)
    _draw('LINES', [(x0, y0), (x1, y1)], color)
    gpu.state.line_width_set(1.0)


def _image(texture, x, y, w, h, alpha):
    shader = gpu.shader.from_builtin('IMAGE_COLOR')
    batch = batch_for_shader(shader, 'TRI_FAN', {
        "pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h)),
        "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1)),
    })
    shader.uniform_sampler("image", texture)
    shader.uniform_float("color", (alpha, alpha, alpha, alpha))
    gpu.state.blend_set('ALPHA_PREMULT')
    batch.draw(shader)
    gpu.state.blend_set('ALPHA')


def _text(x, y, text, size, color, align='CENTER'):
    blf.size(0, size)
    width, height = blf.dimensions(0, text)
    if align == 'CENTER':
        x -= width / 2.0
    elif align == 'RIGHT':
        x -= width
    blf.position(0, round(x), round(y), 0)
    blf.color(0, *color)
    blf.draw(0, text)
    return width, height


def _text_mid(x, cy, text, size, color, align='CENTER'):
    """Text centered on `cy` (by its capital height, so rows of text line up)."""
    blf.size(0, size)
    return _text(x, cy - blf.dimensions(0, "H")[1] / 2, text, size, color, align)


def _fit(text, size, width):
    """`text`, shortened with an ellipsis to fit `width` pixels."""
    blf.size(0, size)
    if blf.dimensions(0, text)[0] <= width:
        return text
    while text and blf.dimensions(0, text + "…")[0] > width:
        text = text[:-1]
    return text.rstrip() + "…"


@contextlib.contextmanager
def _clip(region, rect):
    """Draw only inside `rect` (region pixels): for the cards' scrolling lists."""
    x, y, w, h = rect
    gpu.state.scissor_test_set(True)
    gpu.state.scissor_set(int(region.x + x), int(region.y + y), max(0, int(w)), max(0, int(h)))
    try:
        yield
    finally:
        gpu.state.scissor_test_set(False)


_icon_names = None


def _icon(name):
    global _icon_names
    if _icon_names is None:
        prop = bpy.types.UILayout.bl_rna.functions["prop"].parameters["icon"]
        _icon_names = {item.identifier for item in prop.enum_items}
    return name if name in _icon_names else 'QUESTION'


def _draw_icon(name, cx, cy, size, alpha):
    try:
        bpy.types.WindowManager.phantom_draw_icon(_icon(name), cx - size / 2.0, cy - size / 2.0, size,
                                                  alpha=min(max(alpha, 0.0), 1.0))
    except (AttributeError, TypeError):
        pass
    gpu.state.blend_set('ALPHA')


def _none_icon(cx, cy, r, alpha):
    """An empty slot: a circle with a slash, as for no alpha or texture."""
    color = (1.0, 1.0, 1.0, 0.45 * alpha)
    _arc(cx, cy, r, 1.4, 0.0, 2 * math.pi, color)
    d = r * 0.7
    _line(cx - d, cy - d, cx + d, cy + d, color, 1.4)


def _tools_icon(cx, cy, r, alpha):
    """Tools: a target with a handle off to the side, like ZBrush for iPad's tool button."""
    color = (1.0, 1.0, 1.0, 0.92 * alpha)
    ox, oy = cx - r * 0.3, cy + r * 0.3
    _arc(ox, oy, r * 0.62, 1.4, 0.0, 2 * math.pi, color)
    _disc(ox, oy, r * 0.2, color)
    for i in range(4):
        a = i * math.pi / 2
        _line(ox + r * 0.78 * math.cos(a), oy + r * 0.78 * math.sin(a),
              ox + r * 1.0 * math.cos(a), oy + r * 1.0 * math.sin(a), color, 1.4)
    _line(ox + r * 0.5, oy - r * 0.5, cx + r * 0.62, cy - r * 0.62, color, 1.2)
    _arc(cx + r * 0.8, cy - r * 0.8, r * 0.3, 1.4, 0.0, 2 * math.pi, color)


def _stroke_icon(cx, cy, r, alpha):
    """Dots along an arc, for the stroke."""
    for i in range(6):
        a = math.radians(200 - i * 34)
        _disc(cx + r * math.cos(a), cy + r * 0.85 * math.sin(a) - r * 0.25, r * (0.09 + 0.025 * i),
              (1.0, 1.0, 1.0, 0.9 * alpha))


def _item_active(context, key):
    space = context.space_data
    ob = context.active_object
    if key == 'PERSP':
        return context.region_data is not None and context.region_data.is_perspective
    if key == 'FLOOR':
        try:
            import phantom_viewport
            return phantom_viewport.grid_shown(space, context.screen)
        except (ImportError, AttributeError):
            return space.overlay.show_floor
    if key == 'POLYFRAME':
        return space.overlay.show_sculpt_face_sets and space.overlay.show_wireframes
    if key == 'XRAY':
        return space.shading.show_xray if space.shading.type == 'SOLID' else space.shading.show_xray_wireframe
    if key == 'SOLO':
        return space.local_view is not None
    if key == 'PALETTES':
        return space.show_region_ui
    if key == 'ADD_SLOT':
        return _strip["open"]
    if key == 'DYNTOPO':
        return ob is not None and ob.use_dynamic_topology_sculpting
    if key == 'GIZMO':
        try:
            import phantom_transpose
            return phantom_transpose._transform_tool_active(context)
        except (ImportError, AttributeError):
            return False
    if key.startswith("SLOT"):
        import phantom_quick_brushes as quick_brushes
        slots = quick_brushes.slots()
        index = int(key[4:])
        return index < len(slots) and slots[index] is not None and \
            slots[index] == quick_brushes.current_reference(context)
    return False


def _item_enabled(context, key):
    ob = context.active_object
    if key == 'TEXTURE':
        return False
    if key in {'DIVIDE', 'REMESH', 'QUAD', 'DYNTOPO'}:
        return ob is not None and ob.type == 'MESH'
    return True


def _draw_item(context, item, alpha, rise):
    s = _ui_scale()
    x, y, w, h = item.x, item.y + rise, item.w, item.h
    cx, cy = x + w / 2.0, y + h / 2.0
    key = item.key
    enabled = _item_enabled(context, key)
    a = alpha * (1.0 if enabled else 0.4)

    if _pressed["key"] == key:
        _rounded_rect(x, y, w, h, 8 * s, (*PRESS_COLOR[:3], PRESS_COLOR[3] * alpha))
    elif _hover["key"] == key and enabled:
        _rounded_rect(x, y, w, h, 8 * s, (*HOVER_COLOR[:3], HOVER_COLOR[3] * alpha))

    brush = _brush(context)
    thumb = w * 0.78
    if key == 'TOOLS':
        # The brush while a brush tool is on (as the Brush button showed), else the tools mark.
        texture = _brush_texture(brush) if brush is not None and _brush_tool_on(context) else None
        if texture is not None:
            _image(texture, cx - thumb / 2, cy - thumb / 2, thumb, thumb, a)
        else:
            _tools_icon(cx, cy, w * 0.3, a)
    elif key == 'STROKE':
        _stroke_icon(cx, cy, w * 0.3, a)
    elif key in {'ALPHA', 'TEXTURE'}:
        texture = None
        if key == 'ALPHA' and brush is not None and brush.texture is not None:
            texture = _id_texture(brush.texture)
        if texture is not None:
            _image(texture, cx - thumb / 2, cy - thumb / 2, thumb, thumb, a)
        else:
            _none_icon(cx, cy, w * 0.27, a)
    elif key == 'MATCAP':
        shading = context.space_data.shading
        texture = None
        if shading.type == 'SOLID' and shading.light == 'MATCAP':
            texture = _matcap_texture(shading.studio_light)
        if texture is not None:
            _image(texture, cx - thumb / 2, cy - thumb / 2, thumb, thumb, a)
        else:
            _disc(cx, cy, w * 0.3, (0.7, 0.7, 0.72, a))
    elif key == 'COLOR':
        owner = _owner(context, brush, "use_unified_color") if brush is not None else None
        primary = tuple(owner.color) if owner is not None else (1.0, 1.0, 1.0)
        secondary = tuple(owner.secondary_color) if owner is not None else (0.0, 0.0, 0.0)
        q = w * 0.34
        _rounded_rect(cx - q * 0.55, cy - q * 1.05, q * 1.3, q * 1.3, 2 * s, (*secondary, a))
        _rounded_rect(cx - q * 1.05, cy - q * 0.45, q * 1.3, q * 1.3, 2 * s, (*primary, a))
    elif key in RING_KEYS:
        _draw_ring(context, key, cx, cy, s, alpha)
    elif key == 'PERSP':
        persp = context.region_data is not None and context.region_data.is_perspective
        _draw_icon('VIEW_PERSPECTIVE' if persp else 'VIEW_ORTHO', cx, cy, ICON_SIZE * s, a)
    elif key == 'SOLO':
        _draw_icon('SOLO_ON' if _item_active(context, key) else 'SOLO_OFF', cx, cy, ICON_SIZE * s, a)
    elif key.startswith("SLOT"):
        _draw_slot(context, int(key[4:]), x, y, w, h, a)
    else:
        _draw_icon(ICONS.get(key, 'QUESTION'), cx, cy, ICON_SIZE * s, a)

    if key not in RING_KEYS and _item_active(context, key):
        points = _rounded_rect(x, y, w, h, 8 * s, (0, 0, 0, 0))
        _outline(points, (*ACCENT[:3], alpha), max(1.5, 1.5 * s))
    if key in OPTIONS:
        # A small corner mark, as on buttons with more to them.
        m = 4 * s
        _draw('TRIS', [(x + w - 2 * s, y + 2 * s), (x + w - 2 * s, y + 2 * s + m),
                       (x + w - 2 * s - m, y + 2 * s)], (1.0, 1.0, 1.0, 0.35 * a))


def _draw_ring(context, key, cx, cy, s, alpha):
    value, fraction, text, active = ring_value(context, key)
    a = alpha * (1.0 if active else 0.45)
    radius = RING_RADIUS * s
    width = RING_WIDTH * s
    _arc(cx, cy, radius, width, 0.0, 2 * math.pi, (*RING_TRACK[:3], RING_TRACK[3] * alpha))
    if fraction > 0.0:
        # From the bottom, clockwise.
        start = -0.5 * math.pi
        _arc(cx, cy, radius, width, start, start - fraction * 2 * math.pi, (*ACCENT[:3], a))
    if key in {_scrub["key"], _hover["key"], _card["key"]}:
        _text(cx, cy - 4 * s, text, 10 * s, (*TEXT_COLOR[:3], a))
        return
    small = 7.5 * s
    if key == 'RGB':
        _text(cx, cy - 3 * s, "RGB", small, (*TEXT_COLOR[:3], a))
    elif key == 'STRENGTH':
        add = _direction_is_add(_brush(context))
        if add is not None:
            _text(cx, cy - 3 * s, "ADD" if add else "SUB", small, (*TEXT_COLOR[:3], a))
        else:
            # Brushes without Add and Sub (Grab, Smooth...): a plain center.
            _disc(cx, cy, 2.6 * s, (*TEXT_COLOR[:3], a))
    elif key == 'FOCAL':
        color = (*TEXT_COLOR[:3], a)
        _arc(cx, cy, 6.5 * s, 1.3 * s, 0.0, 2 * math.pi, color)
        _arc(cx, cy, 3.0 * s, 1.3 * s, 0.0, 2 * math.pi, color)
    elif key == 'SIZE':
        color = (*TEXT_COLOR[:3], a)
        _arc(cx, cy, 6.5 * s, 1.3 * s, 0.0, 2 * math.pi, color)
        _disc(cx, cy, 2.6 * s, color)


def _draw_slot(context, index, x, y, w, h, alpha):
    import phantom_quick_brushes as quick_brushes
    slots = quick_brushes.slots()
    slot = slots[index] if index < len(slots) else None
    cx, cy = x + w / 2.0, y + h / 2.0
    s = _ui_scale()
    if slot is None:
        _arc(cx, cy, w * 0.3, 1.2 * s, 0.0, 2 * math.pi, (1.0, 1.0, 1.0, 0.25 * alpha))
        return
    texture = _slot_texture(slot)
    size = w * 0.84
    if texture is not None:
        _image(texture, cx - size / 2, cy - size / 2, size, size, alpha)
    else:
        name = _slot_label(slot)
        _disc(cx, cy, w * 0.34, (0.55, 0.55, 0.58, alpha))
        _text(cx, cy - 3.5 * s, name[:2], 9 * s, (0.1, 0.1, 0.12, alpha))


def _slot_label(slot):
    """The brush name of a quick-brush slot (older slots may use Windows path separators)."""
    import phantom_quick_brushes as quick_brushes
    fixed = dict(slot, relative=slot["relative"].replace("\\", "/"))
    return quick_brushes.slot_name(fixed)


def _card_options(context, key):
    """A ring's options, as square buttons in its card: (id, label, on, 'TOP' above the slider or
    'BOTTOM' under it). Pressure on top for the rings it changes; under the slider, Add and Sub for
    Z Intensity and the Scene toggle for Draw Size (off: the size is in screen pixels, View)."""
    brush = _brush(context)
    if brush is None:
        return []
    options = []
    if key == 'SIZE':
        if hasattr(brush, "use_pressure_size"):
            options.append(('PRESSURE', "Pressure", brush.use_pressure_size, 'TOP'))
        owner = _owner(context, brush, "use_unified_size")
        if hasattr(owner, "use_locked_size"):
            options.append(('SCENE', "Scene", owner.use_locked_size == 'SCENE', 'BOTTOM'))
    elif key in {'STRENGTH', 'RGB'}:
        if hasattr(brush, "use_pressure_strength"):
            options.append(('PRESSURE', "Pressure", brush.use_pressure_strength, 'TOP'))
        add = _direction_is_add(brush) if key == 'STRENGTH' else None
        if add is not None:
            options += [('ADD', "Add", add, 'BOTTOM'), ('SUB', "Sub", not add, 'BOTTOM')]
    return options


def _card_geometry(context, lay, key, s):
    """Where a ring's card goes: one column beside the ring, from the top: its value in a square,
    the options above the slider, the slider, the options under it. None when the ring isn't
    shown."""
    item = next((i for i in lay.items if i.key == key), None)
    if item is None:
        return None
    region = context.region
    options = _card_options(context, key)
    pad, sq, gap = CARD_INSET * s, CARD_SQUARE * s, CARD_INSET * s
    track_h = CARD_TRACK_H * s
    top_n = sum(1 for o in options if o[3] == 'TOP')
    bottom_n = len(options) - top_n
    w = sq + 2 * pad
    h = 2 * pad + sq + (top_n + bottom_n) * (sq + gap) + track_h + 2 * gap
    left_side = item.x < region.width / 2
    x = item.x + item.w + CARD_GAP * s if left_side else item.x - CARD_GAP * s - w
    y = min(max(item.y + item.h / 2 - h / 2, BAR_MARGIN * s), region.height - h - BAR_MARGIN * s)
    sx = x + pad
    top = y + h - pad
    value = (sx, top - sq, sq, sq)
    top -= sq + gap
    pills = []
    for option, label, on, place in options:
        if place == 'TOP':
            pills.append((option, label, on, (sx, top - sq, sq, sq)))
            top -= sq + gap
    # The slider: its ends inset by the knob's radius.
    t0, t1 = top - track_h + 8 * s, top - 8 * s
    track_hit = (x, top - track_h, w, track_h)
    bottom = top - track_h - gap
    for option, label, on, place in options:
        if place == 'BOTTOM':
            pills.append((option, label, on, (sx, bottom - sq, sq, sq)))
            bottom -= sq + gap
    return {"card": (x, y, w, h), "track": (x + w / 2, t0, t1), "track_hit": track_hit,
            "value": value, "pills": pills, "left_side": left_side}


def _pressure_icon(cx, cy, r, color):
    """Pen pressure: a pen pressing into the center of two arcs."""
    s = _ui_scale()
    width = max(1.5, 1.6 * s)
    # Arcs around the pen tip, open toward the pen.
    tx, ty = cx - r * 0.25, cy - r * 0.25
    _arc(tx, ty, r * 0.75, width, math.radians(100), math.radians(350), color)
    _arc(tx, ty, r * 0.42, width, math.radians(120), math.radians(330), color)
    # The pen, from its tip toward the top right.
    a = math.radians(45)
    c, sn = math.cos(a), math.sin(a)
    _line(tx + c * r * 0.18, ty + sn * r * 0.18, tx + c * r * 1.05, ty + sn * r * 1.05, color,
          max(3.0, 0.32 * r))
    _disc(tx + c * r * 1.05, ty + sn * r * 1.05, max(1.5, 0.16 * r), color)


def _scene_icon(cx, cy, r, color):
    """Scene size: a circle with a D (the brush's diameter on the model)."""
    _arc(cx, cy, r, max(1.5, 1.6 * _ui_scale()), 0.0, 2 * math.pi, color)
    size = r * 1.1
    blf.size(0, size)
    _text(cx, cy - blf.dimensions(0, "D")[1] / 2, "D", size, color)


def _in_rect(rect, mx, my):
    x, y, w, h = rect
    return x <= mx <= x + w and y <= my <= y + h


def _draw_card(context, lay, s):
    """A ring's slider with its value on top, as ZBrush for iPad shows while dragging a ring, and
    the ring's options as pills beside it. The same card is shown while a ring is dragged and
    after it is clicked (where the slider and pills can be used)."""
    key = _scrub["key"] or _card["key"]
    g = _card_geometry(context, lay, key, s) if key else None
    if g is None:
        return
    _value, fraction, text, active = ring_value(context, key)
    points = _rounded_rect(*g["card"], BAR_CORNER * s, BAR_COLOR)
    _outline(points, OUTLINE_COLOR)
    # Dimmed like its ring when it does nothing (RGB Intensity with a sculpt brush).
    a = 1.0 if active else 0.45
    # The value, in a square on top.
    vx, vy, vw, vh = g["value"]
    _rounded_rect(vx, vy, vw, vh, 8 * s, PILL_COLOR)
    size = 12 * s
    blf.size(0, size)
    _text(vx + vw / 2, vy + vh / 2 - blf.dimensions(0, "0")[1] / 2, text, size,
          (*TEXT_COLOR[:3], TEXT_COLOR[3] * a))
    tx, y0, y1 = g["track"]
    _line(tx, y0, tx, y1, RING_TRACK, 3 * s)
    ky = y0 + (y1 - y0) * fraction
    _line(tx, y0, tx, ky, (*ACCENT[:3], a), 3 * s)
    # The knob grows while it is dragged.
    knob = 9 * s if _card["dragging"] or _scrub["key"] else 7 * s
    _disc(tx, ky, knob, (1.0, 1.0, 1.0, a))
    for option, label, on, rect in g["pills"]:
        x, y, w, h = rect
        if _card["pressed"] == option:
            fill = PRESS_COLOR
        elif _card["hover"] == option:
            fill = HOVER_COLOR
        else:
            fill = PILL_COLOR
        # Corners like the toolbar's buttons, inside the card's larger ones.
        outline = _rounded_rect(x, y, w, h, 8 * s, fill)
        if on:
            _outline(outline, ACCENT, max(1.5, 1.5 * s))
        # What is on takes the accent; the rest stays quiet.
        color = ACCENT if on else PILL_TEXT_COLOR
        cx, cy = x + w / 2, y + h / 2
        if option == 'PRESSURE':
            _pressure_icon(cx, cy, w * 0.3, color)
        elif option == 'SCENE':
            _scene_icon(cx, cy, w * 0.27, color)
        else:
            blf.size(0, 11 * s)
            _text(cx, cy - blf.dimensions(0, label)[1] / 2, label, 11 * s, color)
    _draw_card_tooltip(context, g, s)


def _draw_card_tooltip(context, g, s):
    """What a hovered square in the card does, beside the card after the tooltip delay."""
    option = _card["hover"]
    if option is None or _card["dragging"] or time.perf_counter() - _card["since"] < TOOLTIP_DELAY:
        return
    rect = next((p[3] for p in g["pills"] if p[0] == option), None)
    text = CARD_TIPS.get((_card["key"], option)) or CARD_TIPS.get(option)
    if rect is None or not text:
        return
    size = 11 * s
    blf.size(0, size)
    width, _h = blf.dimensions(0, text)
    height = 22 * s
    cx, _cy, cw, _ch = g["card"]
    x = cx + cw + 12 * s if g["left_side"] else cx - 12 * s - width - 16 * s
    y = rect[1] + rect[3] / 2 - height / 2
    _rounded_rect(x, y, width + 16 * s, height, 6 * s, TIP_COLOR)
    _text(x + 8 * s, y + 7 * s, text, size, TEXT_COLOR, align='LEFT')


# -----------------------------------------------------------------------------
# The color card: a hue ring around a saturation/value square or triangle, the two colors, swatches,
# the color randomizing and Fill Object, as ZBrush for iPad's Color popup. GPU-drawn like the ring
# card (Blender's pickers have no ring with a square or triangle inside), in the toolbars' style.

def _owner_color(context):
    brush = _brush(context)
    return _owner(context, brush, "use_unified_color") if brush is not None else None


def _to_display(linear):
    return tuple(Color(linear).from_scene_linear_to_srgb())


def _to_linear(display):
    return tuple(Color(display).from_srgb_to_scene_linear())


def _hsv_display(h, s, v):
    c = Color()
    c.hsv = (h % 1.0, s, v)
    return tuple(c)


def _color_sync(context):
    """The card's hue, saturation and value, from the brush color unless it is the one they make
    (so the hue stays put while the color is gray or black)."""
    owner = _owner_color(context)
    if owner is None:
        return
    linear = tuple(owner.color)
    if _color_card["linear"] is not None and \
            max(abs(a - b) for a, b in zip(linear, _color_card["linear"])) < 1e-4:
        return
    c = Color(_to_display(linear))
    if c.s > 1e-4 and c.v > 1e-4:
        _color_card["h"] = c.h
    if c.v > 1e-4:
        _color_card["s"] = c.s
    _color_card["v"] = c.v
    _color_card["linear"] = linear


def _color_set(context, h=None, s=None, v=None):
    owner = _owner_color(context)
    if owner is None:
        return
    for key, value in (("h", h), ("s", s), ("v", v)):
        if value is not None:
            _color_card[key] = min(max(value, 0.0), 1.0) if key != "h" else value % 1.0
    linear = _to_linear(_hsv_display(_color_card["h"], _color_card["s"], _color_card["v"]))
    owner.color = linear
    _color_card["linear"] = tuple(owner.color)


def _color_put(context, linear):
    """Make `linear` the brush color (a swatch, a swap...)."""
    owner = _owner_color(context)
    if owner is not None:
        owner.color = linear
        _color_card["linear"] = None
        _color_sync(context)


def _swatches(context, create=False):
    """The sculpt palette's colors: a palette of a few useful ones is made the first time."""
    paint = _paint(context)
    palette = paint.palette
    if palette is None and create:
        palette = bpy.data.palettes.new("Swatches")
        for display in SWATCH_DEFAULTS:
            palette.colors.new().color = _to_linear(display)
        paint.palette = palette
    return palette


def _color_geometry(context, lay, s):
    """Where everything on the color card goes (region pixels), or None without a Color button."""
    item = next((i for i in lay.items if i.key == 'COLOR'), None)
    brush = _brush(context)
    if item is None or brush is None:
        return None
    region = context.region
    palette = _swatches(context)
    count = len(palette.colors) if palette is not None else 0
    rows = min(SWATCH_ROWS, (count + 1 + SWATCH_COLUMNS - 1) // SWATCH_COLUMNS)
    inner = COLOR_INNER
    # Unscaled heights, top to bottom: title, colors, wheel, hue/saturation/value, swatches, fill.
    sections = [ROW, ROW, inner, 3 * SLIDER + 2 * 4,
                LABEL + rows * SWATCH + (rows - 1) * SWATCH_GAP, FILL_H]
    height = 2 * COLOR_PAD + sum(sections) + SECTION_GAP * (len(sections) - 1)
    # Smaller when the viewport is too short for it.
    k = min(1.0, (region.height - 2 * BAR_MARGIN * s) / (height * s))
    u = s * k
    w, h = (inner + 2 * COLOR_PAD) * u, height * u
    left_side = item.x < region.width / 2
    x = item.x + item.w + CARD_GAP * s if left_side else item.x - CARD_GAP * s - w
    y = min(max(item.y + item.h / 2 - h / 2, BAR_MARGIN * s), region.height - h - BAR_MARGIN * s)
    g = {"card": (x, y, w, h), "u": u, "buttons": [], "swatches": [], "sliders": []}
    x0, top = x + COLOR_PAD * u, y + h - COLOR_PAD * u
    iw = inner * u

    # Title, then the triangle/square switch and the eyedropper on the right.
    g["title"] = (x0, top - ROW * u / 2)
    b = ROW * u
    g["buttons"] += [('PIN', (x0 + iw - 3 * b - 16 * u, top - b, b, b)),
                     ('SWITCH', (x0 + iw - 2 * b - 8 * u, top - b, b, b)),
                     ('EYEDROPPER', (x0 + iw - b, top - b, b, b))]
    top -= (ROW + SECTION_GAP) * u
    # The two colors and the swap, the hex code on the right.
    g["buttons"] += [('PRIMARY', (x0, top - b, b, b)), ('SECONDARY', (x0 + b + 8 * u, top - b, b, b)),
                     ('SWAP', (x0 + 2 * (b + 8 * u), top - b, b, b))]
    g["hex"] = (x0 + iw, top - b / 2)
    top -= (ROW + SECTION_GAP) * u
    # The wheel.
    radius = iw / 2
    g["wheel"] = (x0 + radius, top - radius, radius, RING_W * u)
    top -= (inner + SECTION_GAP) * u
    # Hue, saturation and value of the color itself, as in any color picker.
    for key, label in HSV_SLIDERS:
        g["sliders"].append((key, label, (x0, top - SLIDER * u, iw, SLIDER * u)))
        top -= (SLIDER + 4) * u
    top -= (SECTION_GAP - 4) * u
    # Swatches, the last one adds the current color.
    g["swatch_label"] = (x0, top - LABEL * u / 2)
    top -= LABEL * u
    sw, gap = SWATCH * u, SWATCH_GAP * u
    for i in range(min(count + 1, rows * SWATCH_COLUMNS)):
        row, col = divmod(i, SWATCH_COLUMNS)
        rect = (x0 + col * (sw + gap), top - (row + 1) * sw - row * gap, sw, sw)
        g["swatches"].append((i if i < count else 'ADD', rect))
    top -= (rows * SWATCH + (rows - 1) * SWATCH_GAP + SECTION_GAP) * u
    g["buttons"].append(('FILL', (x0, top - FILL_H * u, iw, FILL_H * u)))
    return g


def _hsv_track(g, rect):
    """A color slider's bar (x0, x1, center y) inside its row."""
    u = g["u"]
    x, y, w, h = rect
    return x + HSV_LABEL_W * u, x + w - HSV_VALUE_W * u, y + h / 2


def _wheel_hit(g, mx, my):
    """'RING' or 'SHAPE' (the square or triangle) under a point, else None."""
    cx, cy, radius, ring = g["wheel"]
    d = math.hypot(mx - cx, my - cy)
    if radius - ring - 2 * g["u"] <= d <= radius + 2 * g["u"]:
        return 'RING'
    if d < radius - ring:
        return 'SHAPE'
    return None


def _triangle(g):
    """The triangle's corners: the pure hue on the right, white top left, black bottom left."""
    cx, cy, radius, ring = g["wheel"]
    r = radius - ring - WHEEL_GAP * g["u"]
    return [(cx + r * math.cos(a), cy + r * math.sin(a))
            for a in (0.0, math.radians(120), math.radians(240))]


def _square(g):
    cx, cy, radius, ring = g["wheel"]
    half = (radius - ring - WHEEL_GAP * g["u"]) / math.sqrt(2.0)
    return cx - half, cy - half, 2 * half


def _sv_at(g, mx, my):
    """Saturation and value for a point in the square or triangle (clamped into it)."""
    if _settings["color_shape"] == 'SQUARE':
        x, y, size = _square(g)
        return min(max((mx - x) / size, 0.0), 1.0), min(max((my - y) / size, 0.0), 1.0)
    (hx, hy), (wx, wy), (kx, ky) = _triangle(g)
    det = (wy - ky) * (hx - kx) + (kx - wx) * (hy - ky)
    a = ((wy - ky) * (mx - kx) + (kx - wx) * (my - ky)) / det
    b = ((ky - hy) * (mx - kx) + (hx - kx) * (my - ky)) / det
    a, b = max(a, 0.0), max(b, 0.0)
    if a + b > 1.0:
        a, b = a / (a + b), b / (a + b)
    v = a + b
    return (a / v if v > 1e-5 else _color_card["s"]), v


def _sv_pos(g):
    s_, v = _color_card["s"], _color_card["v"]
    if _settings["color_shape"] == 'SQUARE':
        x, y, size = _square(g)
        return x + s_ * size, y + v * size
    (hx, hy), (wx, wy), (kx, ky) = _triangle(g)
    a, b = s_ * v, v - s_ * v
    c = 1.0 - a - b
    return a * hx + b * wx + c * kx, a * hy + b * wy + c * ky


_smooth_shader = None


def _draw_smooth(kind, verts, colors):
    global _smooth_shader
    if _smooth_shader is None:
        _smooth_shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    batch = batch_for_shader(_smooth_shader, kind, {"pos": verts, "color": colors})
    gpu.state.blend_set('ALPHA')
    batch.draw(_smooth_shader)


def _marker(x, y, r, fill, u):
    """A picker's handle: the color in a white ring with a soft dark edge."""
    _disc(x, y, r + 2.5 * u, (0.0, 0.0, 0.0, 0.35))
    _disc(x, y, r + 1.5 * u, (1.0, 1.0, 1.0, 1.0))
    _disc(x, y, r, (*fill[:3], 1.0))


def _bucket_icon(cx, cy, r, color):
    """Fill: a tipped bucket and a drop."""
    a = math.radians(-35)
    pts = [(-0.55, 0.55), (0.45, 0.55), (0.35, -0.6), (-0.45, -0.6)]
    pts = [(cx - 0.15 * r + r * (px * math.cos(a) - py * math.sin(a)),
            cy + r * (px * math.sin(a) + py * math.cos(a))) for px, py in pts]
    _outline(pts, color, max(1.4, 0.09 * r))
    _disc(cx + 0.75 * r, cy - 0.45 * r, 0.2 * r, color)


def _shape_icon(cx, cy, r, shape, color):
    """The switch shows the shape it switches to."""
    if shape == 'SQUARE':
        _outline([(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)], color, 1.4)
    else:
        _outline([(cx + r, cy), (cx - r * 0.6, cy + r * 0.95), (cx - r * 0.6, cy - r * 0.95)],
                 color, 1.4)


def _draw_color_card(context, lay, s):
    if not _color_card["open"]:
        return
    g = _color_geometry(context, lay, s)
    owner = _owner_color(context)
    if g is None or owner is None:
        return
    _color_sync(context)
    u = g["u"]
    points = _rounded_rect(*g["card"], BAR_CORNER * s, BAR_COLOR)
    _outline(points, OUTLINE_COLOR)
    blf.size(0, 11 * u)
    lift = blf.dimensions(0, "Xg")[1] / 3

    _text(g["title"][0], g["title"][1] - lift, "Color", 13 * u, TEXT_COLOR, align='LEFT')
    _text(g["hex"][0], g["hex"][1] - lift, "#%02X%02X%02X" % tuple(
        round(min(max(c, 0.0), 1.0) * 255) for c in _to_display(tuple(owner.color))),
        11 * u, DIM_TEXT_COLOR, align='RIGHT')

    for key, (x, y, w, h) in g["buttons"]:
        cx, cy = x + w / 2, y + h / 2
        if key in {'PRIMARY', 'SECONDARY'}:
            color = owner.color if key == 'PRIMARY' else owner.secondary_color
            _rounded_rect(x, y, w, h, 8 * u, (*_to_display(tuple(color)), 1.0))
            outline = _rounded_rect(x, y, w, h, 8 * u, (0, 0, 0, 0))
            if key == 'PRIMARY':
                # The color brushes paint with.
                _outline(outline, ACCENT, max(1.5, 1.5 * s))
            else:
                _outline(outline, OUTLINE_COLOR)
            continue
        if _color_card["pressed"] == key:
            fill = PRESS_COLOR
        elif _color_card["hover"] == key:
            fill = HOVER_COLOR
        else:
            fill = PILL_COLOR if key == 'FILL' else (0, 0, 0, 0)
        outline = _rounded_rect(x, y, w, h, 8 * u, fill)
        if (key == 'EYEDROPPER' and _color_card["picking"]) or \
                (key == 'PIN' and _settings["color_pinned"]):
            _outline(outline, ACCENT, max(1.5, 1.5 * s))
        if key == 'PIN':
            _draw_icon('PINNED' if _settings["color_pinned"] else 'UNPINNED', cx, cy,
                       ICON_SIZE * u * 0.9, 0.92)
        elif key == 'SWITCH':
            other = 'TRIANGLE' if _settings["color_shape"] == 'SQUARE' else 'SQUARE'
            _shape_icon(cx, cy, 6.5 * u, other, TEXT_COLOR)
        elif key == 'EYEDROPPER':
            _draw_icon('EYEDROPPER', cx, cy, ICON_SIZE * u * 0.9, 0.92)
        elif key == 'SWAP':
            _draw_icon('ARROW_LEFTRIGHT', cx, cy, ICON_SIZE * u * 0.9, 0.92)
        elif key == 'FILL':
            label = "Fill Object"
            blf.size(0, 11 * u)
            tw = blf.dimensions(0, label)[0]
            ix = cx - (tw + 24 * u) / 2 + 8 * u
            _bucket_icon(ix, cy, 8 * u, TEXT_COLOR)
            _text(ix + 16 * u, cy - lift, label, 11 * u, TEXT_COLOR, align='LEFT')

    # The hue ring.
    cx, cy, radius, ring = g["wheel"]
    steps = 72
    verts, colors = [], []
    for i in range(steps + 1):
        a = 2 * math.pi * i / steps
        c, sn = math.cos(a), math.sin(a)
        hue = (*_hsv_display(a / (2 * math.pi), 1.0, 1.0), 1.0)
        verts += [(cx + (radius - ring) * c, cy + (radius - ring) * sn), (cx + radius * c, cy + radius * sn)]
        colors += [hue, hue]
    _draw_smooth('TRI_STRIP', verts, colors)
    h = _color_card["h"]
    hue = _hsv_display(h, 1.0, 1.0)
    pure = (*hue, 1.0)
    # The saturation/value square or triangle for that hue.
    if _settings["color_shape"] == 'SQUARE':
        x, y, size = _square(g)
        n = 12
        verts, colors = [], []
        for i in range(n):
            for j in range(n):
                quad = [(i, j), (i + 1, j), (i + 1, j + 1), (i, j), (i + 1, j + 1), (i, j + 1)]
                for qi, qj in quad:
                    verts.append((x + size * qi / n, y + size * qj / n))
                    colors.append((*_hsv_display(h, qi / n, qj / n), 1.0))
        _draw_smooth('TRIS', verts, colors)
    else:
        _draw_smooth('TRIS', _triangle(g), [pure, (1.0, 1.0, 1.0, 1.0), (0.0, 0.0, 0.0, 1.0)])
    # Handles: on the ring at the hue, and at the color in the shape.
    a = 2 * math.pi * h
    mid = radius - ring / 2
    _marker(cx + mid * math.cos(a), cy + mid * math.sin(a), ring / 2 - 3 * u, hue, u)
    _marker(*_sv_pos(g), 6 * u, _to_display(tuple(owner.color)), u)

    # Swatches.
    x, y = g["swatch_label"]
    _text(x, y - lift, "Swatches", 11 * u, DIM_TEXT_COLOR, align='LEFT')
    palette = _swatches(context)
    current = tuple(owner.color)
    for index, (x, y, w, h_) in g["swatches"]:
        if index == 'ADD':
            fill = HOVER_COLOR if _color_card["hover"] == ('SWATCH', 'ADD') else PILL_COLOR
            _rounded_rect(x, y, w, h_, 8 * u, fill)
            _draw_icon('ADD', x + w / 2, y + h_ / 2, ICON_SIZE * u * 0.8, 0.7)
            continue
        color = tuple(palette.colors[index].color)
        _rounded_rect(x, y, w, h_, 8 * u, (*_to_display(color), 1.0))
        outline = _rounded_rect(x, y, w, h_, 8 * u, (0, 0, 0, 0))
        if max(abs(p - q) for p, q in zip(color, current)) < 1e-3:
            _outline(outline, ACCENT, max(1.5, 1.5 * s))
        elif _color_card["hover"] == ('SWATCH', index):
            _outline(outline, (1.0, 1.0, 1.0, 0.5), 1.0)
        else:
            _outline(outline, OUTLINE_COLOR)

    # Hue, saturation and value: each track shows what dragging along it gives.
    hh, ss, vv = _color_card["h"], _color_card["s"], _color_card["v"]
    for key, label, rect in g["sliders"]:
        x, y, w, h_ = rect
        x0, x1, my = _hsv_track(g, rect)
        half = HSV_TRACK * u / 2
        n = 24
        verts, colors = [], []
        for i in range(n + 1):
            f = i / n
            hsv = {"h": (f, 1.0, 1.0), "s": (hh, f, vv), "v": (hh, ss, f)}[key]
            c = (*_hsv_display(*hsv), 1.0)
            px = x0 + (x1 - x0) * f
            verts += [(px, my - half), (px, my + half)]
            colors += [c, c]
        _draw_smooth('TRI_STRIP', verts, colors)
        # Round ends in the end colors.
        for f, px in ((0.0, x0), (1.0, x1)):
            hsv = {"h": (f, 1.0, 1.0), "s": (hh, f, vv), "v": (hh, ss, f)}[key]
            _disc(px, my, half, (*_hsv_display(*hsv), 1.0))
        value = _color_card[key]
        here = {"h": (hh, 1.0, 1.0), "s": (hh, ss, vv), "v": (hh, ss, vv)}[key]
        r = (7 if _color_card["dragging"] == ('SLIDER', key) else 6) * u
        _marker(x0 + (x1 - x0) * value, my, r, _hsv_display(*here), u)
        _text(x, my - lift, label, 11 * u, DIM_TEXT_COLOR, align='LEFT')
        text = f"{round(value * 360)}°" if key == "h" else f"{round(value * 100)}%"
        _text(x + w, my - lift, text, 11 * u, TEXT_COLOR, align='RIGHT')


def _color_target(g, mx, my):
    """What on the color card is under a point: ('BUTTON', key), ('SWATCH', index or 'ADD'),
    ('SLIDER', prop), ('RING' or 'SHAPE', None), or None."""
    for key, rect in g["buttons"]:
        if _in_rect(rect, mx, my):
            return key
    for index, rect in g["swatches"]:
        if _in_rect(rect, mx, my):
            return ('SWATCH', index)
    for prop, _label, rect in g["sliders"]:
        if _in_rect(rect, mx, my):
            return ('SLIDER', prop)
    return _wheel_hit(g, mx, my)


def _open_pinned_color_card(pointer):
    """The pinned color card, back when the simple view opens."""
    window = _window_by_pointer(pointer)
    area = _view3d_area(window.screen) if window is not None else None
    if area is None:
        return
    region = _region(area, 'WINDOW')
    try:
        with bpy.context.temp_override(window=window, screen=window.screen, area=area,
                                       region=region):
            bpy.ops.view3d.phantom_simple_color_card('INVOKE_DEFAULT')
    except RuntimeError:
        pass


def _close_color_card(context):
    _color_card.update(open=False, hover=None, pressed=None, dragging=None, picking=False)
    if context.area is not None:
        context.area.tag_redraw()


def _fill_object(context):
    """Fill the whole mesh with the brush color (the Color Filter's Fill), and show the colors."""
    owner = _owner_color(context)
    ob = context.active_object
    if owner is None or ob is None or ob.type != 'MESH':
        return
    shading = context.space_data.shading
    if shading.type == 'SOLID' and shading.color_type != 'VERTEX':
        shading.color_type = 'VERTEX'
    bpy.ops.sculpt.color_filter('EXEC_DEFAULT', type='FILL', strength=1.0,
                                fill_color=tuple(owner.color))


# -----------------------------------------------------------------------------
# The Materials card: the matcap in use and the kinds on the left, a search field and the matcaps
# (scrolling under it) on the right, beside the toolbar and centered on it, as ZBrush for iPad's
# Materials menu. GPU-drawn like the color card, so the left column and the search field stay put.

def _matcap_kinds(matcaps):
    """The kinds there are matcaps of, in the menu's order, with their counts."""
    counts = {}
    for _light, kinds in matcaps:
        for kind in kinds:
            counts[kind] = counts.get(kind, 0) + 1
    order = ["Favorites", "All"] + [kind for kind, _icon, _w in MATCAP_KINDS] + ["Other", "User"]
    return [(kind, counts[kind]) for kind in order if counts.get(kind) or kind == "All"]


def _matcap_shown(context):
    """The matcaps the grid shows: those of the kind, or every one matching the search."""
    matcaps = _matcaps(context)
    search = _mat["search"].strip().lower()
    if search:
        return [light.name for light, _k in matcaps if search in _matcap_label(light.name).lower()]
    return [light.name for light, kinds in matcaps if _mat["kind"] in kinds]


def _mat_geometry(context, lay, s):
    item = next((i for i in lay.items if i.key == 'MATCAP'), None)
    if item is None:
        return None
    region = context.region
    bar = next((b for b in lay.bars if b[0] == item.bar and b[2] <= item.y <= b[2] + b[4]), None)
    matcaps = _matcaps(context)
    kinds = _matcap_kinds(matcaps)
    pad, gap = MAT_PAD * s, MAT_GAP * s
    left_w = MAT_LEFT * s
    tile_w, tile_h, tile_gap = MAT_TILE * s, (MAT_TILE + MAT_LABEL) * s, MAT_TILE_GAP * s
    grid_w = MAT_COLUMNS * tile_w + (MAT_COLUMNS - 1) * tile_gap
    w = pad + left_w + MAT_SPLIT * s + grid_w + pad
    left_h = MAT_CURRENT * s + gap + len(kinds) * (MAT_ROW + MAT_ROW_GAP) * s
    h = pad + MAT_TITLE * s + gap + max(left_h, MAT_MIN_GRID * s) + pad
    h = min(h, region.height - 2 * BAR_MARGIN * s)
    left_side = item.x < region.width / 2
    x = item.x + item.w + CARD_GAP * s if left_side else item.x - CARD_GAP * s - w
    # Centered on the toolbar (the one the Materials button is in), kept inside the viewport.
    center = bar[2] + bar[4] / 2 if bar is not None else item.y + item.h / 2
    y = min(max(center - h / 2, BAR_MARGIN * s), region.height - h - BAR_MARGIN * s)
    g = {"card": (x, y, w, h), "kinds": [], "tiles": [], "kind_list": kinds}
    top = y + h - pad
    g["title"] = (x + pad, top - MAT_TITLE * s / 2)
    top -= (MAT_TITLE + MAT_GAP) * s
    lx = x + pad
    g["current"] = (lx, top - MAT_CURRENT * s, left_w, MAT_CURRENT * s)
    ky = top - MAT_CURRENT * s - gap
    for kind, count in kinds:
        g["kinds"].append((kind, count, (lx, ky - MAT_ROW * s, left_w, MAT_ROW * s)))
        ky -= (MAT_ROW + MAT_ROW_GAP) * s
    gx = lx + left_w + MAT_SPLIT * s
    # The search field lines up with the top of the matcap in use; the grid scrolls under it.
    g["search"] = (gx, top - MAT_ROW * s, grid_w, MAT_ROW * s)
    grid_top = top - MAT_ROW * s - gap
    g["grid"] = (gx, y + pad, grid_w, grid_top - (y + pad))
    names = _matcap_shown(context)
    rows = (len(names) + MAT_COLUMNS - 1) // MAT_COLUMNS
    content = rows * tile_h + max(0, rows - 1) * tile_gap
    g["scroll_max"] = max(0.0, content - g["grid"][3])
    _mat["scroll"] = min(max(_mat["scroll"], 0.0), g["scroll_max"])
    for i, name in enumerate(names):
        row, col = divmod(i, MAT_COLUMNS)
        ty = grid_top - (row + 1) * tile_h - row * tile_gap + _mat["scroll"]
        g["tiles"].append((name, (gx + col * (tile_w + tile_gap), ty, tile_w, tile_h)))
    return g


def _mat_target(g, mx, my):
    if _in_rect(g["search"], mx, my):
        return ('SEARCH', None)
    for kind, _count, rect in g["kinds"]:
        if _in_rect(rect, mx, my):
            return ('KIND', kind)
    if _in_rect(g["grid"], mx, my):
        for name, rect in g["tiles"]:
            if _in_rect(rect, mx, my):
                return ('TILE', name)
        return ('GRID', None)
    return None


def _draw_mat_card(context, lay, s):
    if not _mat["open"]:
        return
    g = _mat_geometry(context, lay, s)
    if g is None:
        return
    region = context.region
    shading = context.space_data.shading
    current = shading.studio_light if shading.type == 'SOLID' and shading.light == 'MATCAP' else ""
    points = _rounded_rect(*g["card"], BAR_CORNER * s, BAR_COLOR)
    _outline(points, OUTLINE_COLOR)
    _text_mid(g["title"][0], g["title"][1], "Materials", 13 * s, TEXT_COLOR, align='LEFT')

    # The matcap in use, large, with its name.
    x, y, w, h = g["current"]
    _rounded_rect(x, y, w, h, 10 * s, PILL_COLOR)
    size = w - 2 * MAT_PAD * s
    texture = _matcap_texture(current) if current else None
    if texture is not None:
        _image(texture, x + (w - size) / 2, y + h - MAT_PAD * s - size, size, size, 1.0)
    else:
        _arc(x + w / 2, y + h - MAT_PAD * s - size / 2, size / 2, 1.5 * s, 0.0, 2 * math.pi,
             (1.0, 1.0, 1.0, 0.25))
    label = _matcap_label(current) if current else "Not Solid shading"
    _text_mid(x + w / 2, y + 16 * s, _fit(label, 12 * s, w - 16 * s), 12 * s, TEXT_COLOR)

    # The kinds: the one shown gets a surface step and the accent mark (a search shows them all).
    searching = bool(_mat["search"].strip())
    for kind, count, (x, y, w, h) in g["kinds"]:
        chosen = kind == _mat["kind"] and not searching
        if chosen or _mat["hover"] == ('KIND', kind):
            _rounded_rect(x, y, w, h, 8 * s, PRESS_COLOR if chosen else HOVER_COLOR)
        if chosen:
            _rounded_rect(x, y + 8 * s, 3 * s, h - 16 * s, 1.5 * s, ACCENT)
        color = TEXT_COLOR if chosen else PILL_TEXT_COLOR
        _text_mid(x + 12 * s, y + h / 2, kind, 12 * s, color, align='LEFT')
        _text_mid(x + w - 10 * s, y + h / 2, str(count), 11 * s, DIM_TEXT_COLOR, align='RIGHT')

    # The search field.
    x, y, w, h = g["search"]
    outline = _rounded_rect(x, y, w, h, 8 * s, PILL_COLOR)
    if _mat["focus"]:
        _outline(outline, ACCENT, max(1.5, 1.5 * s))
    _draw_icon('VIEWZOOM', x + 16 * s, y + h / 2, ICON_SIZE * s * 0.85, 0.6)
    text = _mat["search"]
    tx = x + 32 * s
    if text:
        shown = text
        blf.size(0, 12 * s)
        while shown and blf.dimensions(0, shown)[0] > w - 48 * s:
            shown = shown[1:]
        width, _h = _text_mid(tx, y + h / 2, shown, 12 * s, TEXT_COLOR, align='LEFT')
    else:
        width = 0.0
        _text_mid(tx, y + h / 2, "Search", 12 * s, DIM_TEXT_COLOR, align='LEFT')
    if _mat["focus"]:
        cx = tx + width + 1 * s
        _line(cx, y + 9 * s, cx, y + h - 9 * s, TEXT_COLOR, max(1.0, s))

    # The matcaps, clipped to the grid.
    favorites = _matcap_favorites()
    grid = g["grid"]
    with _clip(region, grid):
        for name, (x, y, w, h) in g["tiles"]:
            if y > grid[1] + grid[3] or y + h < grid[1]:
                continue
            if _mat["hover"] == ('TILE', name) or name == current:
                _rounded_rect(x, y, w, h, 10 * s, HOVER_COLOR if name != current else PRESS_COLOR)
            size = MAT_TILE * s - 16 * s
            ix, iy = x + (w - size) / 2, y + h - 8 * s - size
            texture = _matcap_texture(name)
            if texture is not None:
                _image(texture, ix, iy, size, size, 1.0)
            else:
                _disc(ix + size / 2, iy + size / 2, size / 2, (1.0, 1.0, 1.0, 0.06))
            if name == current:
                _arc(ix + size / 2, iy + size / 2, size / 2 + 3 * s, 1.5 * s, 0.0, 2 * math.pi,
                     ACCENT)
            if name in favorites:
                _draw_icon('FUND', x + w - 12 * s, y + h - 12 * s, 12 * s, 0.85)
            label = _fit(_matcap_label(name), 11 * s, w - 4 * s)
            _text_mid(x + w / 2, y + MAT_LABEL * s / 2 + 2 * s, label, 11 * s,
                      TEXT_COLOR if name == current else PILL_TEXT_COLOR)
        if not g["tiles"]:
            _text_mid(grid[0] + grid[2] / 2, grid[1] + grid[3] - 24 * s,
                      "No matcaps match" if searching else "None yet", 12 * s, DIM_TEXT_COLOR)
    # Where the list is: a thin bar along the grid's right edge, while it can scroll.
    if g["scroll_max"] > 0:
        gx, gy, gw, gh = grid
        content = gh + g["scroll_max"]
        bar_h = max(24 * s, gh * gh / content)
        by = gy + gh - bar_h - (gh - bar_h) * _mat["scroll"] / g["scroll_max"]
        _rounded_rect(gx + gw + 5 * s, by, 3 * s, bar_h, 1.5 * s, (1.0, 1.0, 1.0, 0.18))


def _matcap_favorites():
    try:
        import phantom_matcaps
        return set(phantom_matcaps.settings()["favorites"])
    except (ImportError, AttributeError, KeyError):
        return set()


def _close_mat_card(context):
    _mat.update(open=False, hover=None, pressed=None, drag=None, focus=False)
    if context.area is not None:
        context.area.tag_redraw()


# -----------------------------------------------------------------------------
# The brush strip: every sculpt brush of the asset libraries in a row above the bottom toolbar,
# from the quick brushes' +. Picking one adds it to the quick brushes.

def _strip_geometry(context, lay, s):
    bottom = next((b for b in lay.bars if b[0] == 2), None)
    if bottom is None:
        return None
    import phantom_brush_catalog
    region = context.region
    entries = phantom_brush_catalog.entries()
    pad = STRIP_PAD * s
    tile_w, tile_h, gap = STRIP_TILE * s, (STRIP_TILE + STRIP_LABEL) * s, STRIP_GAP * s
    left, right = side_reserve(context.area, region)
    available = region.width - left - right - 2 * BAR_MARGIN * s
    count = len(entries) if entries else 0
    content = count * tile_w + max(0, count - 1) * gap
    w = min(available, max(content, STRIP_MIN_W * s) + 2 * pad)
    h = pad + STRIP_HEADER * s + STRIP_GAP * s + tile_h + pad
    x = left + BAR_MARGIN * s + (available - w) / 2
    y = bottom[2] + bottom[4] + STRIP_ABOVE * s
    g = {"card": (x, y, w, h), "tiles": [], "entries": entries}
    g["title"] = (x + pad, y + h - pad - STRIP_HEADER * s / 2)
    row = (x + pad, y + pad, w - 2 * pad, tile_h)
    g["row"] = row
    g["scroll_max"] = max(0.0, content - row[2])
    _strip["scroll"] = min(max(_strip["scroll"], 0.0), g["scroll_max"])
    for i, entry in enumerate(entries or ()):
        tx = row[0] + i * (tile_w + gap) - _strip["scroll"]
        g["tiles"].append((i, (tx, row[1], tile_w, tile_h)))
    return g


def _strip_target(g, mx, my):
    if not _in_rect(g["row"], mx, my):
        return None
    for i, rect in g["tiles"]:
        if _in_rect(rect, mx, my):
            return i
    return None


def _draw_strip(context, lay, s):
    if not _strip["open"]:
        return
    g = _strip_geometry(context, lay, s)
    if g is None:
        return
    import phantom_brush_catalog
    import phantom_quick_brushes as quick_brushes
    region = context.region
    points = _rounded_rect(*g["card"], BAR_CORNER * s, BAR_COLOR)
    _outline(points, OUTLINE_COLOR)
    tx, ty = g["title"]
    _text_mid(tx, ty, "Brushes", 13 * s, TEXT_COLOR, align='LEFT')
    x, y, w, h = g["card"]
    hint = "Pick one to add it to the quick brushes"
    if phantom_brush_catalog.reading():
        hint = "Reading the brush libraries…"
    _text_mid(x + w - STRIP_PAD * s, ty, hint, 11 * s, DIM_TEXT_COLOR, align='RIGHT')
    entries = g["entries"]
    if not entries:
        rx, ry, rw, rh = g["row"]
        _text_mid(rx + rw / 2, ry + rh / 2, "No brushes yet" if not phantom_brush_catalog.reading()
                  else "One moment", 12 * s, DIM_TEXT_COLOR)
        return
    current = quick_brushes.current_reference(context)
    slots = [slot for slot in quick_brushes.slots() if slot]
    in_slots = {(sl["type"], sl["identifier"], sl["relative"].replace("\\", "/")) for sl in slots}
    with _clip(region, g["row"]):
        for i, (x, y, w, h) in g["tiles"]:
            if x > g["row"][0] + g["row"][2] or x + w < g["row"][0]:
                continue
            entry = entries[i]
            ref = (entry["type"], entry["identifier"], entry["relative"])
            active = current is not None and (current["type"], current["identifier"],
                                              current["relative"].replace("\\", "/")) == ref
            if _strip["hover"] == i or _strip["pressed"] == i:
                _rounded_rect(x, y, w, h, 10 * s, PRESS_COLOR if _strip["pressed"] == i
                              else HOVER_COLOR)
            size = STRIP_TILE * s - 12 * s
            ix, iy = x + (w - size) / 2, y + h - 6 * s - size
            texture = _catalog_texture(entry)
            if texture is not None:
                _image(texture, ix, iy, size, size, 1.0)
            else:
                _disc(ix + size / 2, iy + size / 2, size / 2, (1.0, 1.0, 1.0, 0.06))
            if active:
                outline = _rounded_rect(x, y, w, h, 10 * s, (0, 0, 0, 0))
                _outline(outline, ACCENT, max(1.5, 1.5 * s))
            if ref in in_slots:
                # Already a quick brush: a small dot in the corner.
                _disc(x + w - 8 * s, y + h - 8 * s, 3 * s, ACCENT)
            _text_mid(x + w / 2, y + STRIP_LABEL * s / 2 + 2 * s,
                      _fit(entry["name"], 11 * s, w - 6 * s), 11 * s,
                      TEXT_COLOR if active else PILL_TEXT_COLOR)
    # Soft ends when there is more to scroll to.
    rx, ry, rw, rh = g["row"]
    if _strip["scroll"] > 0:
        _draw_smooth('TRIS', [(rx, ry), (rx + 24 * s, ry), (rx + 24 * s, ry + rh),
                              (rx, ry), (rx + 24 * s, ry + rh), (rx, ry + rh)],
                     [BAR_COLOR, (*BAR_COLOR[:3], 0.0), (*BAR_COLOR[:3], 0.0),
                      BAR_COLOR, (*BAR_COLOR[:3], 0.0), BAR_COLOR])
    if _strip["scroll"] < g["scroll_max"]:
        ex = rx + rw
        _draw_smooth('TRIS', [(ex - 24 * s, ry), (ex, ry), (ex, ry + rh),
                              (ex - 24 * s, ry), (ex, ry + rh), (ex - 24 * s, ry + rh)],
                     [(*BAR_COLOR[:3], 0.0), BAR_COLOR, BAR_COLOR,
                      (*BAR_COLOR[:3], 0.0), BAR_COLOR, (*BAR_COLOR[:3], 0.0)])


def _close_strip(context):
    _strip.update(open=False, hover=None, pressed=None, drag=None)
    if context.area is not None:
        context.area.tag_redraw()


def _strip_pick(context, entry):
    """Use the brush and add it to the quick brushes."""
    import phantom_quick_brushes as quick_brushes
    bpy.ops.brush.asset_activate(asset_library_type=entry["type"],
                                 asset_library_identifier=entry["identifier"],
                                 relative_asset_identifier=entry["relative"])
    ref = quick_brushes.current_reference(context)
    if ref is not None:
        quick_brushes.add(ref)
    _forget_thumbnails()


def _placing(context):
    """The primitive being placed, when it is still the mesh being sculpted."""
    ob = context.active_object
    if _place["object"] is None or ob is None or ob.name != _place["object"]:
        return None
    return ob


def _place_geometry(context, lay, s):
    """The placement card: centered above the bottom toolbar, Move, Rotate and Scale, then
    Cancel and Accept. None when nothing is being placed."""
    if _placing(context) is None:
        return None
    region = context.region
    pad = CARD_PAD * s
    pill_h = CARD_PILL_H * s
    blf.size(0, CARD_TEXT * s)
    groups = (('MOVE', "Move"), ('ROTATE', "Rotate"), ('SCALE', "Scale"), None,
              ('CANCEL', "Cancel"), ('ACCEPT', "Accept"))
    widths = [None if g is None else max(CARD_PILL_W * s, blf.dimensions(0, g[1])[0] + 24 * s)
              for g in groups]
    gap = 4 * s
    divider = 16 * s
    w = 2 * pad + sum(x for x in widths if x) + gap * 3 + divider
    h = pill_h + 2 * pad
    bottom = next((b for b in lay.bars if b[0] == 2), None)
    y = (bottom[2] + bottom[4] if bottom else 0.0) + BAR_MARGIN * s
    x = (region.width - w) / 2
    pills = []
    px = x + pad
    for g, pw in zip(groups, widths):
        if g is None:
            px += divider - gap
            continue
        pills.append((g[0], g[1], (px, y + pad, pw, pill_h)))
        px += pw + gap
    return {"card": (x, y, w, h), "pills": pills}


def _draw_place_card(context, lay, s):
    g = _place_geometry(context, lay, s)
    if g is None:
        return
    import phantom_transpose
    mode = phantom_transpose._settings["mode"]
    points = _rounded_rect(*g["card"], BAR_CORNER * s, BAR_COLOR)
    _outline(points, OUTLINE_COLOR)
    for option, label, rect in g["pills"]:
        x, y, w, h = rect
        if option == 'ACCEPT':
            # The one primary action: the accent, with dark text on it.
            fill = (1.0, 0.70, 0.46, 1.0) if _place["hover"] == option else ACCENT
        elif _place["pressed"] == option:
            fill = PRESS_COLOR
        elif _place["hover"] == option:
            fill = HOVER_COLOR
        else:
            fill = PILL_COLOR
        outline = _rounded_rect(x, y, w, h, 8 * s, fill)
        on = option == mode
        if on:
            _outline(outline, ACCENT, max(1.5, 1.5 * s))
        blf.size(0, CARD_TEXT * s)
        color = (0.09, 0.09, 0.10, 1.0) if option == 'ACCEPT' else (
            TEXT_COLOR if on else PILL_TEXT_COLOR)
        _text(x + w / 2, y + h / 2 - blf.dimensions(0, "Xg")[1] / 3, label, CARD_TEXT * s, color)


def _sculpt_tool(context):
    tool = context.workspace.tools.from_space_view3d_mode('SCULPT', create=False)
    return tool.idname if tool else None


def _end_placing(context, cancel):
    """Accept the primitive where it is, or put it back as it was added; then the tool from
    before."""
    ob = _placing(context)
    if ob is not None and cancel and _place["positions"] is not None:
        positions = _place["positions"]
        bpy.ops.object.mode_set(mode='OBJECT')
        if len(ob.data.vertices) * 3 == len(positions):
            ob.data.vertices.foreach_set("co", positions)
            ob.data.update()
        bpy.ops.object.mode_set(mode='SCULPT')
    tool = _place["tool"]
    _place.update(object=None, positions=None, tool=None, hover=None, pressed=None)
    if tool and context.mode == 'SCULPT':
        try:
            bpy.ops.wm.tool_set_by_id(name=tool)
        except RuntimeError:
            pass
    _redraw_simple()


def _new_primitive(kind, radius, location):
    """A primitive dense enough to sculpt, as the active object (in Object Mode)."""
    import bmesh
    mesh_ops = bpy.ops.mesh
    if kind == 'SPHERE':
        mesh_ops.primitive_uv_sphere_add(segments=64, ring_count=32, radius=radius,
                                         location=location)
    elif kind == 'ICO_SPHERE':
        mesh_ops.primitive_ico_sphere_add(subdivisions=5, radius=radius, location=location)
    elif kind == 'CUBE':
        mesh_ops.primitive_cube_add(size=2 * radius, location=location)
    elif kind == 'CYLINDER':
        mesh_ops.primitive_cylinder_add(vertices=48, radius=radius, depth=2 * radius,
                                        end_fill_type='TRIFAN', location=location)
    elif kind == 'CONE':
        mesh_ops.primitive_cone_add(vertices=48, radius1=radius, depth=2 * radius,
                                    end_fill_type='TRIFAN', location=location)
    elif kind == 'TORUS':
        mesh_ops.primitive_torus_add(major_segments=64, minor_segments=24, major_radius=radius,
                                     minor_radius=radius * 0.35, location=location)
    elif kind == 'PLANE':
        mesh_ops.primitive_grid_add(x_subdivisions=64, y_subdivisions=64, size=2 * radius,
                                    location=location)
    else:
        mesh_ops.primitive_monkey_add(size=2 * radius, location=location)
    ob = bpy.context.active_object
    # Even polygons to sculpt on: split the cube's faces into a grid, the sides of cylinders and
    # cones into rings, and the monkey's faces once.
    cuts = {'CUBE': 15, 'CYLINDER': 15, 'CONE': 15, 'MONKEY': 1}.get(kind)
    if cuts:
        bm = bmesh.new()
        bm.from_mesh(ob.data)
        edges = bm.edges[:]
        if kind in {'CYLINDER', 'CONE'}:
            edges = [e for e in edges if abs(e.verts[0].co.z - e.verts[1].co.z) > 1e-6]
        bmesh.ops.subdivide_edges(bm, edges=edges, cuts=cuts, use_grid_fill=True)
        bm.to_mesh(ob.data)
        bm.free()
        ob.data.update()
    return ob


def _card_act(context, key, option):
    """What clicking one of a ring card's pills does."""
    brush = _brush(context)
    if brush is None:
        return
    if option == 'SCENE':
        # View (screen pixels) unless Scene is on.
        owner = _owner(context, brush, "use_unified_size")
        owner.use_locked_size = 'VIEW' if owner.use_locked_size == 'SCENE' else 'SCENE'
    elif option == 'PRESSURE':
        prop = "use_pressure_size" if key == 'SIZE' else "use_pressure_strength"
        setattr(brush, prop, not getattr(brush, prop))
    elif option in {'ADD', 'SUB'}:
        items = [item.identifier for item in brush.bl_rna.properties["direction"].enum_items]
        brush.direction = items[0] if option == 'ADD' else items[1]


def _draw_tooltip(context, lay, s):
    key = _hover["key"]
    if key is None or _scrub["key"] or _pressed["key"]:
        return
    if time.perf_counter() - _hover["since"] < TOOLTIP_DELAY:
        return
    item = next((i for i in lay.items if i.key == key), None)
    if item is None:
        return
    text = _tooltip(context, key)
    size = 11 * s
    blf.size(0, size)
    width, _h = blf.dimensions(0, text)
    height = 22 * s
    region = context.region
    if item.bar == 2:
        x = min(max(item.x + item.w / 2 - width / 2 - 8 * s, 4 * s), region.width - width - 20 * s)
        y = item.y + item.h + 12 * s
    elif item.x < region.width / 2:
        x = item.x + item.w + 14 * s
        y = item.y + item.h / 2 - height / 2
    else:
        x = item.x - 14 * s - width - 16 * s
        y = item.y + item.h / 2 - height / 2
    _rounded_rect(x, y, width + 16 * s, height, 6 * s, TIP_COLOR)
    _text(x + 8 * s, y + 7 * s, text, size, TEXT_COLOR, align='LEFT')


def _tooltip(context, key):
    brush = _brush(context)
    if key.startswith("SLOT"):
        import phantom_quick_brushes as quick_brushes
        slots = quick_brushes.slots()
        index = int(key[4:])
        slot = slots[index] if index < len(slots) else None
        if slot is None:
            return "Empty quick brush: right-click to put the current brush here"
        return f"{_slot_label(slot)}. Right-click to replace or remove it"
    text = TIPS.get(key, key.title())
    return text.format(brush=brush.name if brush else "none", tool=_active_tool_label(context),
                       stroke=brush.stroke_method.replace("_", " ").title() if brush else "")


def _drawable(context):
    region = context.region
    return (region is not None and region.type == 'WINDOW' and context.area is not None and
            context.area.type == 'VIEW_3D' and is_simple_screen(context.screen) and
            context.mode == 'SCULPT')


def _interactive(context):
    if not _drawable(context) or context.window.as_pointer() in _busy:
        return False
    return _bars["dir"] > 0 and time.perf_counter() - _bars["start"] >= BAR_DELAYS[-1] + BAR_IN


def draw_callback():
    context = bpy.context
    if not _drawable(context):
        return
    area, region = context.area, context.region
    if region.width < 200 or region.height < 200:
        return
    s = _ui_scale()
    lay = Layout(context, area, region)
    W = region.width
    gpu.state.blend_set('ALPHA')
    for bar, x, y, w, h in lay.bars:
        p = _progress(bar)
        if p <= 0.0:
            continue
        dx, dy = _slide(bar, x, y, w, h, W, p)
        points = _rounded_rect(x + dx, y + dy, w, h, BAR_CORNER * s,
                               (*BAR_COLOR[:3], BAR_COLOR[3] * min(1.0, p * 1.5)))
        _outline(points, (*OUTLINE_COLOR[:3], OUTLINE_COLOR[3] * p))
    for bar, x0, y0, x1, y1 in lay.dividers:
        p = _progress(bar)
        dx, dy = _slide(bar, x0, y0, 0, 0, W, p)
        _line(x0 + dx, y0 + dy, x1 + dx, y1 + dy, (*DIVIDER_COLOR[:3], DIVIDER_COLOR[3] * p))
    for item in lay.items:
        p_bar = _progress(item.bar)
        if p_bar <= 0.0:
            continue
        p_item = _progress(item.bar, item.index + 1) if _bars["dir"] > 0 else p_bar
        dx, dy = _slide(item.bar, item.x, item.y, item.w, item.h, W, p_bar)
        moved = Item(item.key, item.bar, item.index, item.x + dx, item.y + dy, item.w, item.h)
        _draw_item(context, moved, p_item, -(1.0 - p_item) * 8 * s)
    _draw_card(context, lay, s)
    _draw_color_card(context, lay, s)
    _draw_mat_card(context, lay, s)
    _draw_strip(context, lay, s)
    _draw_place_card(context, lay, s)
    _draw_tooltip(context, lay, s)
    gpu.state.blend_set('NONE')


def _slide(bar, x, y, w, h, W, p):
    """Offset of a toolbar that has come in by `p`: it slides in from its own edge."""
    away = 1.0 - p
    if bar == 2:
        return 0.0, -away * (y + h + 4)
    if x + w / 2 < W / 2:
        return -away * (x + w + 4), 0.0
    return away * (W - x + 4), 0.0


# -----------------------------------------------------------------------------
# Brush circle while over the toolbars

def _hide_cursor(context):
    paint = _paint(context)
    if not _cursor["hidden"] and paint.show_brush:
        paint.show_brush = False
        _cursor["hidden"] = True
    if not _cursor["modal"]:
        context.window.cursor_modal_set('DEFAULT')
        _cursor["modal"] = context.window.as_pointer()


def _show_cursor(context):
    if _cursor["hidden"]:
        try:
            _paint(context).show_brush = True
        except AttributeError:
            pass
        _cursor["hidden"] = False
    if _cursor["modal"]:
        window = _window_by_pointer(_cursor["modal"])
        if window is not None:
            window.cursor_modal_restore()
    _cursor["modal"] = False


@bpy.app.handlers.persistent
def _restore_brush_circle(*_args):
    # Never save the brush circle hidden.
    if _cursor["hidden"]:
        try:
            bpy.context.tool_settings.sculpt.show_brush = True
        except AttributeError:
            pass
        _cursor["hidden"] = False


@bpy.app.handlers.persistent
def _on_load(*_args):
    _busy.clear()
    _busy_at.clear()
    _bars.update(start=0.0, dir=1)
    _cursor.update(hidden=False, modal=False)
    _hover["key"] = None
    _forget_thumbnails()


# -----------------------------------------------------------------------------
# Actions

def _call_panel(name):
    try:
        bpy.ops.wm.call_panel(name=name, keep_open=True)
    except RuntimeError as ex:
        print("Phantom: could not open", name, ex)


@contextlib.contextmanager
def _beside(context, key):
    """Popups opened meanwhile pop out beside the button's toolbar (to the right of the left one,
    to the left of the right one, above the bottom one), not over it."""
    wm = context.window_manager
    region = context.region
    lay = Layout(context, context.area, region)
    item = next((i for i in lay.items if i.key == key), None)
    bar = next((b for b in lay.bars if item is not None and b[0] == item.bar and
                b[1] <= item.x <= b[1] + b[3] and b[2] <= item.y <= b[2] + b[4]), None)
    if bar is None or not hasattr(wm, "phantom_popup_anchor_set"):
        yield
        return
    _bar, x, y, w, h = bar
    if item.bar == 2:
        rect, side = (item.x, y, item.x + item.w, y + h), 'UP'
    else:
        rect, side = (x, item.y, x + w, item.y + item.h), 'RIGHT' if x < region.width / 2 else 'LEFT'
    # In the toolbars' style too (graphite panel, hairlines, the orange accent), like the ring card.
    wm.phantom_popup_anchor_set(*(int(v) + (region.x if i % 2 == 0 else region.y)
                                  for i, v in enumerate(rect)), side, simple_style=True)
    try:
        yield
    finally:
        wm.phantom_popup_anchor_set(0, 0, 0, 0, 'NONE')


def _undo(context, redo):
    pointer = context.window.as_pointer()

    def run():
        window = _window_by_pointer(pointer)
        area = _view3d_area(window.screen) if window else None
        if area is None:
            return None
        with bpy.context.temp_override(window=window, screen=window.screen, area=area,
                                       region=_region(area, 'WINDOW')):
            try:
                if redo:
                    bpy.ops.ed.redo()
                else:
                    bpy.ops.ed.undo()
            except RuntimeError:
                pass
        return None

    # Undo can free data an operator still uses: run it once this one is done.
    bpy.app.timers.register(run, first_interval=0.0)


def _toggle_sidebar(context):
    space = context.space_data
    sidebar = _region(context.area, 'UI')
    if not space.show_region_ui and sidebar is not None and sidebar.alignment != 'RIGHT':
        _flip_sidebar(context.window, context.area, 'RIGHT')
    space.show_region_ui = not space.show_region_ui
    if space.show_region_ui and sidebar is not None:
        try:
            sidebar.active_panel_category = 'Tool'
        except (TypeError, AttributeError):
            pass


def _perform(context, key):
    """What clicking a button does."""
    space = context.space_data
    ob = context.active_object
    if key == 'TOOLS':
        _call_panel(VIEW3D_PT_phantom_simple_tools.__name__)
    elif key in {'STROKE', 'ALPHA'}:
        bpy.ops.paint.phantom_brush_slot(slot=key)
    elif key == 'MATCAP':
        bpy.ops.view3d.phantom_simple_matcap_card('INVOKE_DEFAULT')
    elif key == 'COLOR':
        bpy.ops.view3d.phantom_simple_color_card('INVOKE_DEFAULT')
    elif key in RING_KEYS:
        bpy.ops.view3d.phantom_simple_ring_card('INVOKE_DEFAULT', key=key)
    elif key in {'UNDO', 'REDO'}:
        _undo(context, key == 'REDO')
    elif key == 'PALETTES':
        _toggle_sidebar(context)
    elif key == 'SUBTOOLS':
        _sync_scene_rows(context)
        _call_panel(VIEW3D_PT_phantom_simple_subtools.__name__)
    elif key == 'RENDER':
        bpy.ops.render.opengl('INVOKE_DEFAULT', view_context=True)
    elif key == 'PERSP':
        bpy.ops.view3d.view_persportho()
    elif key == 'FLOOR':
        # The grid in use here: the limited one (the simple view's default) or the floor.
        import phantom_viewport
        phantom_viewport.toggle_grid(space, context.screen)
    elif key == 'FRAME':
        bpy.ops.view3d.view_all('INVOKE_DEFAULT', center=False)
    elif key == 'POLYFRAME':
        bpy.ops.sculpt.phantom_polyframe()
    elif key == 'XRAY':
        bpy.ops.view3d.toggle_xray()
    elif key == 'SOLO':
        bpy.ops.view3d.localview('INVOKE_DEFAULT')
    elif key == 'GIZMO':
        bpy.ops.sculpt.phantom_transform_tool(mode='MOVE')
    elif key == 'REMESH':
        bpy.ops.object.voxel_remesh('INVOKE_DEFAULT')
    elif key == 'DYNTOPO':
        bpy.ops.sculpt.dynamic_topology_toggle('INVOKE_DEFAULT')
    elif key == 'QUAD':
        _call_panel("VIEW3D_PT_sculpt_quad_remesh")
    elif key == 'DIVIDE':
        bpy.ops.sculpt.phantom_divide('INVOKE_DEFAULT')
    elif key == 'ADD_SLOT':
        if _strip["open"]:
            _close_strip(context)
        else:
            bpy.ops.view3d.phantom_simple_brush_strip('INVOKE_DEFAULT')
    elif key.startswith("SLOT"):
        bpy.ops.sculpt.phantom_quick_brush(slot=int(key[4:]))
    _redraw_simple()


def _has_options(key):
    return key in OPTIONS or key.startswith("SLOT")


def _open_options(context, key):
    if key.startswith("SLOT"):
        _slot_menu["slot"] = int(key[4:])
        bpy.ops.wm.call_menu(name=VIEW3D_MT_phantom_simple_slot.__name__)
        return
    kind, name = OPTIONS[key]
    if kind == 'PANEL':
        _call_panel(name)
    elif kind == 'FLIP_COLORS':
        bpy.ops.paint.brush_colors_flip()
    elif kind == 'FLIP_DIRECTION':
        brush = _brush(context)
        prop = brush.bl_rna.properties.get("direction") if brush else None
        if prop is not None and len(prop.enum_items) > 1:
            items = [item.identifier for item in prop.enum_items]
            brush.direction = items[1] if brush.direction == items[0] else items[0]
    _redraw_simple()


_slot_menu = {"slot": 0}


# -----------------------------------------------------------------------------
# Operators

class VIEW3D_OT_phantom_simple_view_toggle(Operator):
    """Switch between the simple sculpting view (laid out like ZBrush for iPad) and the usual """ \
        """interface"""
    bl_idname = "view3d.phantom_simple_view_toggle"
    bl_label = "Simple View"
    bl_options = {'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        if context.window is not None and is_simple_screen(context.window.screen):
            return "Back to the usual interface (Shift Tab)"
        return ("Switch to the simple sculpting view, laid out like ZBrush for iPad (Shift Tab). "
                "Phantom opens Sculpt Mode in it until you switch back")

    @classmethod
    def poll(cls, context):
        window = context.window
        if window is None:
            return False
        return is_simple_screen(window.screen) or context.mode == 'SCULPT'

    def execute(self, context):
        window = context.window
        if window.as_pointer() in _busy:
            return {'CANCELLED'}
        if is_simple_screen(window.screen):
            leave(window)
            return {'FINISHED'}
        screen = window.screen
        if screen.is_temporary:
            self.report({'WARNING'}, "Restore the maximized area first (Ctrl Space)")
            return {'CANCELLED'}
        area = context.area if context.area is not None and context.area.type == 'VIEW_3D' else None
        area = area or _view3d_area(screen)
        if area is None:
            return {'CANCELLED'}
        _set_setting("on", True)
        enter(window, area)
        return {'FINISHED'}


class _ToolbarModal:
    """Waits for the mouse button to be released over a toolbar button."""

    def _start(self, context, event, item):
        self.key = item.key
        self.start = (event.mouse_region_x, event.mouse_region_y)
        self.time = time.perf_counter()
        self.moved = False
        self.timer = context.window_manager.event_timer_add(0.05, window=context.window)
        _pressed["key"] = item.key
        context.area.tag_redraw()
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        context.window_manager.event_timer_remove(self.timer)
        _pressed["key"] = None
        _scrub["key"] = None
        # No tooltip beside what the button opened: not until the cursor moves to another one.
        _hover["since"] = math.inf
        context.area.tag_redraw()

    def _over_item(self, context, event):
        lay = Layout(context, context.area, context.region)
        item, _on_bar = lay.hit(event.mouse_region_x, event.mouse_region_y)
        return item is not None and item.key == self.key


class VIEW3D_OT_phantom_simple_press(_ToolbarModal, Operator):
    """Click a toolbar button, drag a ring to change its value, or hold for the button's options"""
    bl_idname = "view3d.phantom_simple_press"
    bl_label = "Simple View Toolbar"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _interactive(context)

    def invoke(self, context, event):
        lay = Layout(context, context.area, context.region)
        item, on_bar = lay.hit(event.mouse_region_x, event.mouse_region_y)
        if not on_bar:
            return {'PASS_THROUGH'}
        if item is None or not _item_enabled(context, item.key):
            # On a toolbar but not on a button: no stroke starts here.
            return {'FINISHED'}
        self.ring = item.key in RING_KEYS
        self.start_value = ring_value(context, item.key)[0] if self.ring else 0.0
        self.scrubbing = False
        return self._start(context, event, item)

    def modal(self, context, event):
        s = _ui_scale()
        if event.type == 'MOUSEMOVE':
            dx = event.mouse_region_x - self.start[0]
            dy = event.mouse_region_y - self.start[1]
            if abs(dx) > DRAG_THRESHOLD * s or abs(dy) > DRAG_THRESHOLD * s:
                self.moved = True
            if self.ring and (self.scrubbing or self.moved):
                self.scrubbing = True
                _scrub["key"] = self.key
                _set_ring(context, self.key, _scrubbed(self.key, self.start_value, dy))
                _redraw_simple()
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            held = time.perf_counter() - self.time >= HOLD_TIME
            over = self._over_item(context, event)
            self._finish(context)
            if self.scrubbing:
                return {'FINISHED'}
            if not over:
                return {'CANCELLED'}
            _play("click")
            with _beside(context, self.key):
                if held and not self.moved and _has_options(self.key) and not self.ring:
                    _open_options(context, self.key)
                else:
                    try:
                        _perform(context, self.key)
                    except RuntimeError as ex:
                        self.report({'WARNING'}, str(ex).replace("Error: ", "").strip())
            return {'FINISHED'}
        elif event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            if self.scrubbing:
                _set_ring(context, self.key, self.start_value)
            self._finish(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


class VIEW3D_OT_phantom_simple_options(_ToolbarModal, Operator):
    """A toolbar button's options"""
    bl_idname = "view3d.phantom_simple_options"
    bl_label = "Simple View Toolbar Options"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _interactive(context)

    def invoke(self, context, event):
        lay = Layout(context, context.area, context.region)
        item, on_bar = lay.hit(event.mouse_region_x, event.mouse_region_y)
        if not on_bar:
            return {'PASS_THROUGH'}
        if item is None or not _has_options(item.key) or not _item_enabled(context, item.key):
            return {'FINISHED'}
        return self._start(context, event, item)

    def modal(self, context, event):
        if event.type == 'RIGHTMOUSE' and event.value == 'RELEASE':
            over = self._over_item(context, event)
            self._finish(context)
            if over:
                # Opened on the release: opened on the press, the release would close it again.
                _play("click")
                with _beside(context, self.key):
                    _open_options(context, self.key)
            return {'FINISHED'}
        if event.type == 'ESC':
            self._finish(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


def _close_card(context):
    _card.update(key=None, hover=None, pressed=None, dragging=False)
    if context.area is not None:
        context.area.tag_redraw()


def _ring_at(key, fraction):
    """A ring's value for a point along its slider (the inverse of #ring_value's fraction)."""
    fraction = min(max(fraction, 0.0), 1.0)
    if key == 'SIZE':
        return 2.0 * (SIZE_MAX / 2.0) ** fraction
    return fraction


class VIEW3D_OT_phantom_simple_ring_card(Operator):
    """A ring's slider and options beside it: drag or click along the slider (or use the arrow """ \
        """keys or the wheel), click an option, click elsewhere to close"""
    bl_idname = "view3d.phantom_simple_ring_card"
    bl_label = "Ring Slider"
    bl_options = {'INTERNAL'}

    key: StringProperty(options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return _interactive(context)

    def invoke(self, context, event):
        if self.key not in RING_KEYS:
            return {'CANCELLED'}
        _card.update(key=self.key, hover=None, pressed=None, dragging=False)
        context.area.tag_redraw()
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _slide_to(self, context, g, my):
        _tx, y0, y1 = g["track"]
        _set_ring(context, self.key, _ring_at(self.key, (my - y0) / max(y1 - y0, 1.0)))

    def modal(self, context, event):
        if _card["key"] != self.key or not _interactive(context):
            _close_card(context)
            return {'CANCELLED', 'PASS_THROUGH'}
        s = _ui_scale()
        lay = Layout(context, context.area, context.region)
        g = _card_geometry(context, lay, self.key, s)
        if g is None:
            _close_card(context)
            return {'CANCELLED', 'PASS_THROUGH'}
        mx, my = event.mouse_region_x, event.mouse_region_y
        inside = _in_rect(g["card"], mx, my)
        pill = next((p[0] for p in g["pills"] if _in_rect(p[3], mx, my)), None)
        on_track = _in_rect(g["track_hit"], mx, my)

        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            if _card["dragging"]:
                self._slide_to(context, g, my)
                _redraw_simple()
                return {'RUNNING_MODAL'}
            if pill != _card["hover"]:
                _card.update(hover=pill, since=time.perf_counter())
                context.area.tag_redraw()
                if pill is not None:
                    # Once the delay is over, for the tooltip.
                    bpy.app.timers.register(_redraw_simple, first_interval=TOOLTIP_DELAY + 0.02)
            return {'RUNNING_MODAL'} if inside else {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if not inside:
                item, _on_bar = lay.hit(mx, my)
                _close_card(context)
                if item is not None and item.key == self.key:
                    # Clicking the ring again closes its card.
                    return {'FINISHED'}
                return {'FINISHED', 'PASS_THROUGH'}
            if on_track:
                _card["dragging"] = True
                self._slide_to(context, g, my)
                _redraw_simple()
            elif pill is not None:
                _card["pressed"] = pill
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if _card["dragging"]:
                _card["dragging"] = False
            elif _card["pressed"] is not None:
                if pill == _card["pressed"]:
                    _play("click")
                    _card_act(context, self.key, pill)
                _card["pressed"] = None
            _redraw_simple()
            return {'RUNNING_MODAL'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            _close_card(context)
            return {'CANCELLED'}
        # Without dragging: the wheel over the card, or the arrow keys (Shift: finer).
        steps = {'WHEELUPMOUSE': 1, 'WHEELDOWNMOUSE': -1, 'UP_ARROW': 1, 'RIGHT_ARROW': 1,
                 'DOWN_ARROW': -1, 'LEFT_ARROW': -1}
        if (event.type in steps and event.value in {'PRESS', 'NOTHING'} and
                (inside or not event.type.startswith('WHEEL'))):
            _value, fraction, _text, _active = ring_value(context, self.key)
            step = steps[event.type] * (0.005 if event.shift else 0.02)
            _set_ring(context, self.key, _ring_at(self.key, fraction + step))
            _redraw_simple()
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}


class VIEW3D_OT_phantom_simple_color_card(Operator):
    """The paint colors: drag the ring for the hue and the square or triangle for the shade, """ \
        """click a swatch (right-click removes it), click elsewhere to close"""
    bl_idname = "view3d.phantom_simple_color_card"
    bl_label = "Color"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _interactive(context)

    def invoke(self, context, event):
        if _owner_color(context) is None:
            return {'CANCELLED'}
        _color_card.update(open=True, linear=None, hover=None, pressed=None, dragging=None,
                           picking=False)
        _color_sync(context)
        context.area.tag_redraw()
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _drag(self, context, g, mx, my):
        what = _color_card["dragging"]
        if what == 'RING':
            cx, cy, _r, _ring = g["wheel"]
            _color_set(context, h=math.atan2(my - cy, mx - cx) / (2 * math.pi))
        elif what == 'SHAPE':
            s_, v = _sv_at(g, mx, my)
            _color_set(context, s=s_, v=v)
        elif isinstance(what, tuple):
            key = what[1]
            x0, x1, _y = _hsv_track(g, next(r for k, _l, r in g["sliders"] if k == key))
            value = min(max((mx - x0) / max(x1 - x0, 1.0), 0.0), 1.0)
            # The hue's 360 degrees are 0 again: keep the end of the track at red, not wrapped.
            _color_set(context, **{key: min(value, 0.9999) if key == "h" else value})
        _redraw_simple()

    def _act(self, context, target):
        if target == 'PIN':
            _set_setting("color_pinned", not _settings["color_pinned"])
        elif target == 'SWITCH':
            _set_setting("color_shape", 'TRIANGLE' if _settings["color_shape"] == 'SQUARE' else 'SQUARE')
        elif target == 'EYEDROPPER':
            _color_card["picking"] = not _color_card["picking"]
        elif target in {'SECONDARY', 'SWAP'}:
            bpy.ops.paint.brush_colors_flip()
            _color_card["linear"] = None
        elif target == 'FILL':
            try:
                _fill_object(context)
            except RuntimeError as ex:
                self.report({'WARNING'}, str(ex).replace("Error: ", "").strip())
        elif isinstance(target, tuple) and target[0] == 'SWATCH':
            owner = _owner_color(context)
            if target[1] == 'ADD':
                palette = _swatches(context, create=True)
                current = tuple(owner.color)
                if not any(max(abs(p - q) for p, q in zip(c.color, current)) < 1e-3
                           for c in palette.colors):
                    palette.colors.new().color = current
            else:
                _color_put(context, tuple(_swatches(context).colors[target[1]].color))
        _redraw_simple()

    def modal(self, context, event):
        if not _color_card["open"] or not _interactive(context):
            _close_color_card(context)
            return {'CANCELLED', 'PASS_THROUGH'}
        s = _ui_scale()
        lay = Layout(context, context.area, context.region)
        g = _color_geometry(context, lay, s)
        if g is None:
            _close_color_card(context)
            return {'CANCELLED', 'PASS_THROUGH'}
        mx, my = event.mouse_region_x, event.mouse_region_y
        inside = _in_rect(g["card"], mx, my)
        target = _color_target(g, mx, my) if inside else None

        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            if _color_card["dragging"]:
                self._drag(context, g, mx, my)
                return {'RUNNING_MODAL'}
            hover = target if target not in {'RING', 'SHAPE'} else None
            if isinstance(hover, tuple) and hover[0] == 'SLIDER':
                hover = None
            if hover != _color_card["hover"]:
                _color_card["hover"] = hover
                context.area.tag_redraw()
            if inside:
                _hide_cursor(context)
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if not inside:
                if _color_card["picking"]:
                    # The eyedropper: the color of the mesh under the cursor.
                    _color_card["picking"] = False
                    try:
                        bpy.ops.sculpt.sample_color('INVOKE_DEFAULT')
                    except RuntimeError as ex:
                        self.report({'WARNING'}, str(ex).replace("Error: ", "").strip())
                    _color_card["linear"] = None
                    _redraw_simple()
                    return {'RUNNING_MODAL'}
                item, _on_bar = lay.hit(mx, my)
                if item is not None and item.key == 'COLOR':
                    # Clicking the Color button again closes the card, pinned or not.
                    _close_color_card(context)
                    return {'FINISHED'}
                if _settings["color_pinned"]:
                    # Pinned: sculpt, paint or use the toolbars with the card open.
                    return {'PASS_THROUGH'}
                _close_color_card(context)
                return {'FINISHED', 'PASS_THROUGH'}
            if target in {'RING', 'SHAPE'} or (isinstance(target, tuple) and target[0] == 'SLIDER'):
                _color_card["dragging"] = target
                self._drag(context, g, mx, my)
            elif target is not None:
                _color_card["pressed"] = target
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if not inside and not _color_card["dragging"] and _color_card["pressed"] is None:
                # A click that went through (pinned): its release is someone else's.
                return {'PASS_THROUGH'}
            if _color_card["dragging"]:
                _color_card["dragging"] = None
            elif _color_card["pressed"] is not None:
                if target == _color_card["pressed"]:
                    _play("click")
                    self._act(context, target)
                _color_card["pressed"] = None
            _redraw_simple()
            return {'RUNNING_MODAL'}
        if event.type == 'RIGHTMOUSE' and event.value == 'PRESS' and isinstance(target, tuple) and \
                target[0] == 'SWATCH' and target[1] != 'ADD':
            # Right-click a swatch: remove it.
            palette = _swatches(context)
            palette.colors.remove(palette.colors[target[1]])
            _color_card["hover"] = None
            _redraw_simple()
            return {'RUNNING_MODAL'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            if _settings["color_pinned"] and not inside:
                # Pinned: Esc and right-click are for whatever is under the cursor.
                return {'PASS_THROUGH'}
            _close_color_card(context)
            return {'CANCELLED'}
        if event.type.startswith('WHEEL') and inside:
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}


class VIEW3D_OT_phantom_simple_matcap_card(Operator):
    """Matcaps by kind: click one to use it, right-click to star it, type to search"""
    bl_idname = "view3d.phantom_simple_matcap_card"
    bl_label = "Materials"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _interactive(context)

    def invoke(self, context, event):
        shading = context.space_data.shading
        current = shading.studio_light if shading.type == 'SOLID' and shading.light == 'MATCAP' \
            else ""
        if not _mat["kind"]:
            # At first: the kind of the matcap in use.
            kinds = next((k for light, k in _matcaps(context) if light.name == current), set())
            _mat["kind"] = next((kind for kind, _i, _w in MATCAP_KINDS if kind in kinds), "All")
        _mat.update(open=True, hover=None, pressed=None, drag=None, focus=False)
        context.area.tag_redraw()
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _type(self, context, event):
        """Keys while the search field is being typed in; True when one was used."""
        if event.value != 'PRESS':
            return event.type not in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'TIMER'}
        if event.type in {'RET', 'NUMPAD_ENTER'}:
            _mat["focus"] = False
        elif event.type == 'ESC':
            if _mat["search"]:
                _mat["search"] = ""
            else:
                _mat["focus"] = False
        elif event.type == 'BACK_SPACE':
            _mat["search"] = "" if event.ctrl else _mat["search"][:-1]
            _play("type_delete", random.uniform(0.94, 1.06))
        elif event.unicode and event.unicode.isprintable() and not event.ctrl and not event.alt:
            _mat["search"] += event.unicode
            _play("type", random.uniform(0.88, 1.12))
        else:
            # Other keys do nothing while typing (no shortcuts going off).
            return True
        _mat["scroll"] = 0.0
        context.area.tag_redraw()
        return True

    def modal(self, context, event):
        if not _mat["open"] or not _interactive(context):
            _close_mat_card(context)
            return {'CANCELLED', 'PASS_THROUGH'}
        s = _ui_scale()
        lay = Layout(context, context.area, context.region)
        g = _mat_geometry(context, lay, s)
        if g is None:
            _close_mat_card(context)
            return {'CANCELLED', 'PASS_THROUGH'}
        mx, my = event.mouse_region_x, event.mouse_region_y
        inside = _in_rect(g["card"], mx, my)
        target = _mat_target(g, mx, my) if inside else None

        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            drag = _mat["drag"]
            if drag is not None:
                if abs(my - drag[0]) > DRAG_THRESHOLD * s:
                    # Dragging the grid scrolls it (with a pen or a finger), not a click.
                    _mat["pressed"] = None
                _mat["scroll"] = drag[1] + (my - drag[0])
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            hover = target if target and target[0] in {'KIND', 'TILE'} else None
            if hover != _mat["hover"]:
                _mat["hover"] = hover
                context.area.tag_redraw()
            if inside:
                _hide_cursor(context)
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        if _mat["focus"] and event.type not in {'LEFTMOUSE', 'RIGHTMOUSE', 'WHEELUPMOUSE',
                                                'WHEELDOWNMOUSE', 'TIMER'}:
            if self._type(context, event):
                return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if not inside:
                item, _on_bar = lay.hit(mx, my)
                _close_mat_card(context)
                if item is not None and item.key == 'MATCAP':
                    return {'FINISHED'}
                return {'FINISHED', 'PASS_THROUGH'}
            _mat["focus"] = target is not None and target[0] == 'SEARCH'
            if target is not None and target[0] in {'KIND', 'TILE', 'GRID'}:
                _mat["pressed"] = target if target[0] != 'GRID' else None
                if target[0] in {'TILE', 'GRID'}:
                    _mat["drag"] = (my, _mat["scroll"])
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            pressed = _mat["pressed"]
            _mat.update(pressed=None, drag=None)
            if pressed is not None and pressed == target:
                _play("click")
                if pressed[0] == 'KIND':
                    _mat.update(kind=pressed[1], search="", scroll=0.0)
                else:
                    shading = context.space_data.shading
                    # Matcaps show in Solid shading with Matcap lighting: switch there if need be.
                    shading.type = 'SOLID'
                    shading.light = 'MATCAP'
                    shading.studio_light = pressed[1]
            _redraw_simple()
            return {'RUNNING_MODAL'}
        if event.type == 'RIGHTMOUSE' and event.value == 'PRESS' and target and target[0] == 'TILE':
            # Right-click a matcap: star it (or not).
            name = target[1]
            bpy.ops.view3d.phantom_matcap_favorite(name=name, remove=name in _matcap_favorites())
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            _close_mat_card(context)
            return {'CANCELLED'}
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and inside:
            step = (MAT_TILE + MAT_LABEL + MAT_TILE_GAP) * s / 2
            _mat["scroll"] += step if event.type == 'WHEELDOWNMOUSE' else -step
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}


class VIEW3D_OT_phantom_simple_brush_strip(Operator):
    """Every sculpt brush in a row: click one to use it and add it to the quick brushes"""
    bl_idname = "view3d.phantom_simple_brush_strip"
    bl_label = "Brushes"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _interactive(context)

    def invoke(self, context, event):
        _strip.update(open=True, hover=None, pressed=None, drag=None)
        # Redraw while the libraries are read (the first time, or after they changed).
        self.timer = context.window_manager.event_timer_add(0.25, window=context.window)
        context.area.tag_redraw()
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _end(self, context):
        context.window_manager.event_timer_remove(self.timer)
        _close_strip(context)

    def modal(self, context, event):
        if not _strip["open"] or not _interactive(context):
            self._end(context)
            return {'CANCELLED', 'PASS_THROUGH'}
        s = _ui_scale()
        lay = Layout(context, context.area, context.region)
        g = _strip_geometry(context, lay, s)
        if g is None:
            self._end(context)
            return {'CANCELLED', 'PASS_THROUGH'}
        if event.type == 'TIMER':
            import phantom_brush_catalog
            if phantom_brush_catalog.reading() or not g["entries"]:
                context.area.tag_redraw()
            return {'PASS_THROUGH'}
        mx, my = event.mouse_region_x, event.mouse_region_y
        inside = _in_rect(g["card"], mx, my)
        target = _strip_target(g, mx, my) if inside else None

        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            drag = _strip["drag"]
            if drag is not None:
                if abs(mx - drag[0]) > DRAG_THRESHOLD * s:
                    # Dragging the row scrolls it, not a click.
                    _strip["pressed"] = None
                _strip["scroll"] = drag[1] - (mx - drag[0])
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if target != _strip["hover"]:
                _strip["hover"] = target
                context.area.tag_redraw()
            if inside:
                _hide_cursor(context)
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if not inside:
                item, _on_bar = lay.hit(mx, my)
                self._end(context)
                if item is not None and item.key == 'ADD_SLOT':
                    # The + again closes the strip.
                    return {'FINISHED'}
                return {'FINISHED', 'PASS_THROUGH'}
            _strip.update(pressed=target, drag=(mx, _strip["scroll"]))
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            pressed = _strip["pressed"]
            _strip.update(pressed=None, drag=None)
            if pressed is not None and pressed == target:
                _play("click")
                try:
                    _strip_pick(context, g["entries"][pressed])
                except RuntimeError as ex:
                    self.report({'WARNING'}, str(ex).replace("Error: ", "").strip())
                self._end(context)
                _redraw_simple()
                return {'FINISHED'}
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._end(context)
            return {'CANCELLED'}
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and inside:
            step = 2 * (STRIP_TILE + STRIP_GAP) * s
            _strip["scroll"] += step if event.type == 'WHEELDOWNMOUSE' else -step
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}


class VIEW3D_OT_phantom_simple_hover(Operator):
    """Track the cursor over the simple view's toolbars"""
    bl_idname = "view3d.phantom_simple_hover"
    bl_label = "Simple View Hover"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _drawable(context)

    def invoke(self, context, event):
        lay = Layout(context, context.area, context.region)
        item, on_bar = lay.hit(event.mouse_region_x, event.mouse_region_y)
        key = item.key if item is not None else None
        if _card["key"] and not on_bar:
            # Over an open ring card: no brush circle there either.
            g = _card_geometry(context, lay, _card["key"], _ui_scale())
            on_bar = g is not None and _in_rect(g["card"], event.mouse_region_x, event.mouse_region_y)
        for is_open, geometry in ((_color_card["open"], _color_geometry), (_mat["open"], _mat_geometry),
                                  (_strip["open"], _strip_geometry)):
            if is_open and not on_bar:
                g = geometry(context, lay, _ui_scale())
                on_bar = g is not None and _in_rect(g["card"], event.mouse_region_x,
                                                    event.mouse_region_y)
        if not on_bar:
            # Nor over the mask or polygroup actions bar.
            try:
                import phantom_mask_bar
                on_bar = phantom_mask_bar._visible(context) and phantom_mask_bar._button_at(
                    context.area, context.region, event.mouse_region_x, event.mouse_region_y)[1]
            except (ImportError, AttributeError):
                pass
        if not on_bar:
            # Nor over the placement card.
            g = _place_geometry(context, lay, _ui_scale())
            on_bar = g is not None and _in_rect(g["card"], event.mouse_region_x, event.mouse_region_y)
        if on_bar and _interactive(context):
            _hide_cursor(context)
        elif not on_bar:
            _show_cursor(context)
        if key != _hover["key"] or on_bar != _hover["bar"]:
            _hover.update(key=key, since=time.perf_counter(), bar=on_bar)
            context.area.tag_redraw()
            if key is not None:
                bpy.app.timers.register(_redraw_simple, first_interval=TOOLTIP_DELAY + 0.02)
        # Never consume the event: sculpting needs it.
        return {'PASS_THROUGH'}


class VIEW3D_OT_phantom_simple_subtool(Operator):
    """Sculpt this mesh, or hide or show it"""
    bl_idname = "view3d.phantom_simple_subtool"
    bl_label = "Subtool"
    bl_options = {'INTERNAL', 'UNDO'}

    name: StringProperty(options={'SKIP_SAVE'})
    action: StringProperty(default='SELECT', options={'SKIP_SAVE'})

    def execute(self, context):
        ob = context.view_layer.objects.get(self.name)
        if ob is None:
            return {'CANCELLED'}
        if self.action == 'VISIBILITY':
            ob.hide_set(not ob.hide_get())
            return {'FINISHED'}
        if ob == context.active_object:
            return {'FINISHED'}
        if ob.hide_get():
            ob.hide_set(False)
        # The new mesh has to be the active one in Object Mode to start sculpting it.
        bpy.ops.object.mode_set(mode='OBJECT')
        for other in context.selected_objects:
            other.select_set(False)
        ob.select_set(True)
        context.view_layer.objects.active = ob
        bpy.ops.object.mode_set(mode='SCULPT')
        return {'FINISHED'}


class VIEW3D_OT_phantom_simple_subtool_delete(Operator):
    """Delete this mesh from the scene"""
    bl_idname = "view3d.phantom_simple_subtool_delete"
    bl_label = "Delete Mesh"
    bl_options = {'INTERNAL', 'UNDO'}

    name: StringProperty(options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return f"Delete {properties.name} from the scene"

    def invoke(self, context, event):
        # Asked first: while sculpting, undo keeps to the mesh being sculpted, so it can't bring a
        # deleted one back. In the simple view's style, like the Scene popup it comes from.
        with _beside(context, 'SUBTOOLS'):
            return context.window_manager.invoke_confirm(
                self, event, title=f"Delete {self.name}", message="It is removed from the scene",
                confirm_text="Delete", icon='WARNING')

    def execute(self, context):
        ob = context.view_layer.objects.get(self.name)
        meshes = [o for o in context.view_layer.objects if o.type == 'MESH']
        if ob is None:
            return {'CANCELLED'}
        if len(meshes) <= 1:
            self.report({'WARNING'}, "The last mesh can't be deleted: there would be nothing to sculpt")
            return {'CANCELLED'}
        if _place["object"] == ob.name:
            _place.update(object=None, positions=None, tool=None, hover=None, pressed=None)
        if ob == context.active_object:
            # Sculpting goes on with the next mesh (shown, if one is).
            others = [o for o in meshes if o != ob]
            index = meshes.index(ob)
            others.sort(key=lambda o: (o.hide_get(), abs(meshes.index(o) - index)))
            following = others[0]
            sculpting = context.mode == 'SCULPT'
            bpy.ops.object.mode_set(mode='OBJECT')
            for other in context.selected_objects:
                other.select_set(False)
            if following.hide_get():
                following.hide_set(False)
            following.select_set(True)
            context.view_layer.objects.active = following
            bpy.data.objects.remove(ob, do_unlink=True)
            if sculpting:
                bpy.ops.object.mode_set(mode='SCULPT')
        else:
            bpy.data.objects.remove(ob, do_unlink=True)
        _redraw_simple()
        return {'FINISHED'}


class VIEW3D_OT_phantom_simple_add_primitive(Operator):
    """Add this primitive to the scene and sculpt it, placing it first with the gizmo"""
    bl_idname = "view3d.phantom_simple_add_primitive"
    bl_label = "Add Primitive"
    bl_options = {'INTERNAL', 'UNDO'}

    kind: StringProperty(default='SPHERE', options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        name = next((p[1] for p in PRIMITIVES if p[0] == properties.kind), "primitive")
        return f"Add a {name.lower()} to the scene to sculpt, placed first with the gizmo"

    @classmethod
    def poll(cls, context):
        return context.mode == 'SCULPT'

    def execute(self, context):
        if _placing(context) is not None:
            # Still placing the last one: it stays where it is.
            _end_placing(context, cancel=False)
        previous = context.active_object
        tool = _sculpt_tool(context)
        # As big as half the mesh sculpted so far, at the 3D cursor.
        radius = max(previous.dimensions) / 2.0 if previous is not None and previous.type == 'MESH' \
            and max(previous.dimensions) > 0.0 else 1.0
        location = context.scene.cursor.location.copy()
        bpy.ops.object.mode_set(mode='OBJECT')
        for other in context.selected_objects:
            other.select_set(False)
        ob = _new_primitive(self.kind, radius, location)
        bpy.ops.object.mode_set(mode='SCULPT')
        positions = np.empty(len(ob.data.vertices) * 3, np.float32)
        ob.data.vertices.foreach_get("co", positions)
        _place.update(object=ob.name, positions=positions, tool=tool, hover=None, pressed=None)
        # Placed with the gizmo: Move first, Rotate and Scale on the card.
        bpy.ops.sculpt.phantom_transform_style(style='GIZMO')
        bpy.ops.sculpt.phantom_transform_tool(mode='MOVE')
        bpy.ops.view3d.phantom_simple_place('INVOKE_DEFAULT')
        _redraw_simple()
        return {'FINISHED'}


class VIEW3D_OT_phantom_simple_place(Operator):
    """Place the new primitive: move, rotate or scale it with the gizmo, then Accept (Enter) or """ \
        """Cancel (Esc) to put it back as it was added"""
    bl_idname = "view3d.phantom_simple_place"
    bl_label = "Place Primitive"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _drawable(context) and _placing(context) is not None

    def invoke(self, context, event):
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _act(self, context, option):
        _play("click")
        if option in {'MOVE', 'ROTATE', 'SCALE'}:
            bpy.ops.sculpt.phantom_transform_tool(mode=option)
            _redraw_simple()
            return None
        _end_placing(context, cancel=option == 'CANCEL')
        return {'FINISHED'}

    def modal(self, context, event):
        if not _drawable(context) or _placing(context) is None:
            # Another mesh, out of Sculpt Mode or the simple view: it stays where it is.
            if _place["object"] is not None:
                _place.update(object=None, positions=None, tool=None, hover=None, pressed=None)
            return {'CANCELLED', 'PASS_THROUGH'}
        s = _ui_scale()
        lay = Layout(context, context.area, context.region)
        g = _place_geometry(context, lay, s)
        mx, my = event.mouse_region_x, event.mouse_region_y
        inside = g is not None and _in_rect(g["card"], mx, my)
        pill = next((p[0] for p in g["pills"] if _in_rect(p[2], mx, my)), None) if g else None
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            if pill != _place["hover"]:
                _place["hover"] = pill
                context.area.tag_redraw()
            return {'RUNNING_MODAL'} if inside else {'PASS_THROUGH'}
        if event.type == 'LEFTMOUSE' and inside:
            if event.value == 'PRESS':
                _place["pressed"] = pill
                context.area.tag_redraw()
            elif event.value == 'RELEASE':
                pressed, _place["pressed"] = _place["pressed"], None
                if pill is not None and pill == pressed:
                    done = self._act(context, pill)
                    if done:
                        return done
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.value == 'PRESS' and event.type in {'RET', 'NUMPAD_ENTER'}:
            return self._act(context, 'ACCEPT')
        if event.value == 'PRESS' and event.type == 'ESC':
            return self._act(context, 'CANCEL')
        return {'PASS_THROUGH'}


class VIEW3D_OT_phantom_simple_slot(Operator):
    """Put the current brush in this quick-brush slot, or empty it"""
    bl_idname = "view3d.phantom_simple_slot"
    bl_label = "Quick Brush Slot"
    bl_options = {'INTERNAL'}

    remove: BoolProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        slot = _slot_menu["slot"]
        if self.remove:
            bpy.ops.sculpt.phantom_quick_brush_remove(slot=slot)
        else:
            bpy.ops.sculpt.phantom_quick_brush_assign(slot=slot)
        _forget_thumbnails()
        _redraw_simple()
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Pop-overs and menus

class VIEW3D_MT_phantom_simple_slot(Menu):
    bl_label = "Quick Brush"

    def draw(self, context):
        layout = self.layout
        layout.operator(VIEW3D_OT_phantom_simple_slot.bl_idname, text="Put the Current Brush Here",
                        icon='BRUSH_DATA')
        layout.operator(VIEW3D_OT_phantom_simple_slot.bl_idname, text="Remove", icon='X').remove = True


# The tool menu's categories, from a tool's id (the first that matches), in the menu's order.
TOOL_CATEGORIES = (
    ("Brushes", 'BRUSH_DATA', ()),
    ("Mask", 'MOD_MASK', ("mask",)),
    ("Hide", 'HIDE_OFF', ("hide",)),
    ("Face Sets", 'FACE_MAPS', ("face_set",)),
    ("Trim", 'MOD_BOOLEAN', ("trim", "project")),
    ("Filters", 'MODIFIER', ("filter",)),
    ("Transform", 'ORIENTATION_GLOBAL', ("move", "rotate", "scale", "transform", "transpose")),
    ("Add", 'MESH_DATA', ("_add",)),
    ("Annotate", 'GREASEPENCIL', ("annotate", "measure")),
)
_tools_menu = {"category": "Brushes", "items": []}


def _sculpt_tools(context):
    """Every tool of Sculpt Mode's toolbar, groups opened up: (tool definition, category)."""
    from bl_ui.space_toolsystem_common import ToolSelectPanelHelper
    cls = ToolSelectPanelHelper._tool_class_from_space_type('VIEW_3D')
    tools = []
    seen = set()
    for item in cls._tools_flatten_with_dynamic(cls.tools_from_context(context, mode='SCULPT'),
                                                 context=context):
        if item is None or item.idname in seen:
            continue
        seen.add(item.idname)
        idname = item.idname.lower()
        category = next((name for name, _icon, words in TOOL_CATEGORIES[1:]
                         if any(word in idname for word in words)), TOOL_CATEGORIES[0][0])
        tools.append((item, category))
    return tools


def _brush_tool_on(context):
    tool = context.workspace.tools.from_space_view3d_mode('SCULPT', create=False)
    return tool is None or tool.use_brushes


def _active_tool_label(context):
    tool = context.workspace.tools.from_space_view3d_mode('SCULPT', create=False)
    idname = tool.idname if tool is not None else ""
    try:
        item = next((item for item, _c in _sculpt_tools(context) if item.idname == idname), None)
    except (ImportError, AttributeError):
        item = None
    return item.label if item is not None else "Brush"


def _tool_category_items(_self, context):
    """The tool menu's kinds that have tools now, then All (kept referenced: Blender reads them
    after this returns)."""
    present = {category for _item, category in _sculpt_tools(context)} if context else set()
    names = [(name, icon) for name, icon, _w in TOOL_CATEGORIES if name in present]
    names.append(("All", 'PRESET'))
    _tools_menu["items"] = [(name, name, f"Show the {name.lower()} tools" if name != "All" else
                             "Show every tool", icon, i) for i, (name, icon) in enumerate(names)]
    return _tools_menu["items"]


def _tool_category_get(_self):
    return next((i for i, item in enumerate(_tools_menu["items"])
                 if item[0] == _tools_menu["category"]), 0)


def _tool_category_set(_self, value):
    if 0 <= value < len(_tools_menu["items"]):
        _tools_menu["category"] = _tools_menu["items"][value][0]


class VIEW3D_PT_phantom_simple_tools(Panel):
    """Every Sculpt Mode tool, by kind, as ZBrush for iPad's tool menu"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "Tools"
    bl_ui_units_x = 18

    def draw(self, context):
        from bl_ui.space_toolsystem_common import ToolSelectPanelHelper
        layout = self.layout
        layout.label(text="Tools")
        tools = _sculpt_tools(context)
        names = {item[0] for item in _tool_category_items(None, context)}
        current = _tools_menu["category"]
        if current not in names:
            current = _tools_menu["category"] = TOOL_CATEGORIES[0][0]
        active = context.workspace.tools.from_space_view3d_mode('SCULPT', create=False)
        active_id = active.idname if active is not None else None

        split = layout.split(factor=0.38)
        # The kinds on the left, their tools on the right; rows tall enough for a pen or a finger.
        col = split.column(align=True)
        col.scale_y = 1.5
        col.prop(context.window_manager, "phantom_simple_tool_category", expand=True)
        col = split.column(align=True)
        col.scale_y = 1.5
        for item, category in tools:
            if current != "All" and category != current:
                continue
            icon_value = ToolSelectPanelHelper._icon_value_from_icon_handle(item.icon)
            col.operator("wm.tool_set_by_id", text=item.label or item.idname, icon_value=icon_value,
                         depress=(item.idname == active_id)).name = item.idname
        if current in {TOOL_CATEGORIES[0][0], "All"}:
            # The brushes themselves (the Brush button's library used to open from the toolbar).
            col.separator()
            col.operator("wm.call_asset_shelf_popover", text="Brush Library",
                         icon='ASSET_MANAGER').name = BRUSH_SHELF


class VIEW3D_PT_phantom_simple_quick_menu(Panel):
    """The brush's values, as in ZBrush for iPad's Quick Menu"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "Quick Menu"
    bl_ui_units_x = 13

    def draw(self, context):
        layout = self.layout
        brush = _brush(context)
        layout.label(text="Quick Menu")
        if brush is None:
            layout.label(text="No brush")
            return
        layout.use_property_split = False
        col = layout.column()
        for i, key in enumerate(RING_NAMES):
            if i:
                col.separator()
            _draw_ring_setting(col, context, brush, key)


def _draw_ring_setting(col, context, brush, key):
    """A ring's name and slider (with what goes with it), for the Quick Menu or on its own."""
    from bl_ui.properties_paint_common import UnifiedPaintPanel
    col.label(text=RING_NAMES[key])
    if key == 'SIZE':
        row = col.row(align=True)
        UnifiedPaintPanel.prop_unified(row, context, brush, "size", unified_name="use_unified_size",
                                       pressure_name="use_pressure_size", text="", slider=True)
        owner = _owner(context, brush, "use_unified_size")
        if hasattr(owner, "use_locked_size"):
            col.row().prop(owner, "use_locked_size", expand=True)
    elif key == 'FOCAL':
        col.prop(brush, "hardness", text="", slider=True)
    elif key == 'STRENGTH':
        row = col.row(align=True)
        UnifiedPaintPanel.prop_unified(row, context, brush, "strength",
                                       unified_name="use_unified_strength",
                                       pressure_name="use_pressure_strength", text="", slider=True)
        if _direction_is_add(brush) is not None:
            col.row().prop(brush, "direction", expand=True)
    elif _is_paint_brush(brush):
        row = col.row(align=True)
        UnifiedPaintPanel.prop_unified(row, context, brush, "strength",
                                       unified_name="use_unified_strength", text="", slider=True)
    else:
        sub = col.column()
        sub.enabled = False
        sub.label(text="Paint brushes paint color")


# The Materials menu's kinds of matcaps, from words in their names (the first that matches), in the
# menu's order after Favorites and All; User is for matcaps installed by hand.
MATCAP_KINDS = (
    ("Clay & Skin", 'MATSHADERBALL', ("clay", "wax", "sculpy", "skin", "gorilla", "chalk", "gummy")),
    ("Metal & Shiny", 'SHADING_RENDERED', ("chrome", "metal", "gold", "reflect", "silver", "bronze",
                                           "carpaint", "shiny", "satin", "pearl", "droplet")),
    ("Sketch", 'GREASEPENCIL', ("sketch", "framer", "flat")),
    ("Basic", 'SHADING_SOLID', ("basic", "base", "ceramic", "hard_surface", "resin", "white",
                                "gray", "grey")),
    ("Studio", 'LIGHT', ("check_",)),
)
# Matcaps that are tools, not looks.
MATCAP_HIDDEN = ("brush_thumbnail",)


def _matcap_label(name):
    try:
        import phantom_matcaps
        return phantom_matcaps._short_label(name)
    except (ImportError, AttributeError):
        return os.path.splitext(name)[0]


def _matcaps(context):
    """The matcaps to pick from, by name: (studio light, the kinds it is listed under)."""
    try:
        import phantom_matcaps
        favorites = set(phantom_matcaps.settings()["favorites"])
    except (ImportError, AttributeError, KeyError):
        favorites = set()
    result = []
    for light in context.preferences.studio_lights:
        if light.type != 'MATCAP' or light.name.startswith(MATCAP_HIDDEN):
            continue
        name = light.name.lower()
        kinds = {"All", next((kind for kind, _icon, words in MATCAP_KINDS
                              if any(word in name for word in words)), "Other")}
        if light.name in favorites:
            kinds.add("Favorites")
        if light.is_user_defined:
            kinds.add("User")
        result.append((light, kinds))
    result.sort(key=lambda item: _matcap_label(item[0].name).lower())
    return result


class VIEW3D_MT_phantom_simple_add_mesh(Menu):
    bl_label = "Add Mesh"

    def draw(self, context):
        layout = self.layout
        for kind, name, icon in PRIMITIVES:
            layout.operator(VIEW3D_OT_phantom_simple_add_primitive.bl_idname, text=name,
                            icon=icon).kind = kind


# A collection is a folder of the Scene window when made there (tagged with this), or any other
# collection but Blender's default one ("Collection", which new files start with and Blender
# puts new objects in): that one isn't a folder, its meshes are just in the scene.
FOLDER_TAG = "phantom_folder"
# A folder that is closed (its meshes not listed), on the collection.
FOLDER_CLOSED = "phantom_folder_closed"
DEFAULT_COLLECTION = "Collection"


def _is_folder(collection, parent_is_scene):
    if collection.get(FOLDER_TAG):
        return True
    return not (parent_is_scene and collection.name == DEFAULT_COLLECTION)


def _scene_rows(context, all_rows=False):
    """The Scene window's rows, in order: ('OBJECT' or 'FOLDER', name, depth). Closed folders'
    meshes are left out unless `all_rows`."""
    rows = []
    in_layer = context.view_layer.objects

    def walk(layer_collection, depth, is_scene):
        for ob in layer_collection.collection.objects:
            if ob.type == 'MESH' and ob.name in in_layer:
                rows.append(('OBJECT', ob.name, depth))
        for child in layer_collection.children:
            if child.exclude:
                continue
            if _is_folder(child.collection, is_scene):
                rows.append(('FOLDER', child.collection.name, depth))
                if all_rows or not child.collection.get(FOLDER_CLOSED):
                    walk(child, depth + 1, False)
            else:
                walk(child, depth, False)

    walk(context.view_layer.layer_collection, 0, True)
    return rows


def _scene_meshes(context):
    """The scene's meshes, each once, in the Scene window's order (ZBrush's "the one below")."""
    seen, meshes = set(), []
    objects = context.view_layer.objects
    for kind, name, _depth in _scene_rows(context, all_rows=True):
        if kind == 'OBJECT' and name not in seen:
            seen.add(name)
            meshes.append(objects[name])
    return meshes


def _layer_collection(context, name):
    def find(layer_collection):
        if layer_collection.collection.name == name:
            return layer_collection
        for child in layer_collection.children:
            found = find(child)
            if found is not None:
                return found
        return None
    return find(context.view_layer.layer_collection)


def _sync_scene_rows(context=None):
    """The Scene window's list reads its rows from the window manager: refreshed here (on the
    simple view's regular check and after each change), never while drawing."""
    context = context or bpy.context
    wm = context.window_manager
    if wm is None or not hasattr(wm, "phantom_simple_scene_rows") or context.view_layer is None:
        return
    rows = _scene_rows(context)
    current = [(r.kind, r.name, r.depth) for r in wm.phantom_simple_scene_rows]
    if current == rows:
        return
    wm.phantom_simple_scene_rows.clear()
    for kind, name, depth in rows:
        item = wm.phantom_simple_scene_rows.add()
        item.kind, item.name, item.depth = kind, name, depth


def _mesh_below(context, ob, visible=True):
    meshes = _scene_meshes(context)
    if ob not in meshes:
        return None
    below = meshes[meshes.index(ob) + 1:] + meshes[:meshes.index(ob)]
    return next((o for o in below if not visible or not o.hide_get()), None)


def _sculpt(context, ob):
    """Sculpt `ob` (from Object Mode: it has to be the active object there)."""
    if context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for other in context.selected_objects:
        other.select_set(False)
    if ob.hide_get():
        ob.hide_set(False)
    ob.select_set(True)
    context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode='SCULPT')


SCENE_ACTIONS = (
    ('DUPLICATE', "Duplicate", "Copy the mesh being sculpted, and sculpt the copy"),
    ('SPLIT_PARTS', "Split to Parts", "Make each separate piece of the mesh its own object"),
    ('MERGE_DOWN', "Merge Down", "Merge the mesh below (in the list) into this one"),
    ('MERGE_VISIBLE', "Merge Visible", "Merge every shown mesh into this one"),
    ('UNION', "Union", "Join the mesh below into this one, as one solid (Boolean)"),
    ('DIFFERENCE', "Difference", "Cut the mesh below out of this one, then hide it (Boolean)"),
    ('INTERSECT', "Intersect", "Keep where this mesh and the one below overlap (Boolean)"),
    ('CENTER', "Center", "Move the mesh to the middle of the scene"),
    ('FLOOR', "On Floor", "Stand the mesh on the floor (its lowest point at the grid)"),
    ('SHOW_ALL', "Show All", "Show every mesh"),
    ('HIDE_OTHERS', "Hide Others", "Hide every mesh but this one"),
)


class VIEW3D_OT_phantom_simple_scene_action(Operator):
    """An action of the simple view's Scene window, on the mesh being sculpted"""
    bl_idname = "view3d.phantom_simple_scene_action"
    bl_label = "Scene Action"
    bl_options = {'INTERNAL', 'UNDO'}

    action: bpy.props.EnumProperty(items=[(a, n, d) for a, n, d in SCENE_ACTIONS],
                                   options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return next(d for a, _n, d in SCENE_ACTIONS if a == properties.action)

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return ob is not None and ob.type == 'MESH'

    def execute(self, context):
        ob = context.active_object
        action = self.action
        try:
            result = getattr(self, "_" + action.lower())(context, ob)
        except RuntimeError as ex:
            self.report({'WARNING'}, str(ex).replace("Error: ", "").strip())
            if context.mode != 'SCULPT' and context.view_layer.objects.active is not None:
                bpy.ops.object.mode_set(mode='SCULPT')
            return {'CANCELLED'}
        _sync_scene_rows(context)
        _redraw_simple()
        return result or {'FINISHED'}

    def _duplicate(self, context, ob):
        copy = ob.copy()
        copy.data = ob.data.copy()
        for collection in ob.users_collection:
            collection.objects.link(copy)
        _sculpt(context, copy)

    def _split_parts(self, context, ob):
        before = set(context.view_layer.objects)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.reveal()
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.separate(type='LOOSE')
        bpy.ops.object.mode_set(mode='OBJECT')
        _sculpt(context, ob)
        made = len(set(context.view_layer.objects) - before)
        self.report({'INFO'}, f"{made} new object{'s' if made != 1 else ''}" if made else
                    "It is one piece already")

    def _join(self, context, ob, others):
        if not others:
            self.report({'WARNING'}, "No other shown mesh to merge")
            return {'CANCELLED'}
        bpy.ops.object.mode_set(mode='OBJECT')
        for other in context.selected_objects:
            other.select_set(False)
        for other in others:
            other.select_set(True)
        ob.select_set(True)
        context.view_layer.objects.active = ob
        bpy.ops.object.join()
        _sculpt(context, ob)
        return None

    def _merge_down(self, context, ob):
        below = _mesh_below(context, ob)
        return self._join(context, ob, [below] if below is not None else [])

    def _merge_visible(self, context, ob):
        others = [o for o in _scene_meshes(context) if o != ob and not o.hide_get()]
        return self._join(context, ob, others)

    def _boolean(self, context, ob, operation):
        below = _mesh_below(context, ob)
        if below is None:
            self.report({'WARNING'}, "No other shown mesh below this one")
            return {'CANCELLED'}
        bpy.ops.object.mode_set(mode='OBJECT')
        modifier = ob.modifiers.new("Phantom Boolean", 'BOOLEAN')
        modifier.operation = operation
        modifier.solver = 'EXACT'
        modifier.object = below
        # On top of the stack, so only the boolean is applied (not Multires and the like).
        bpy.ops.object.modifier_move_to_index(modifier=modifier.name, index=0)
        bpy.ops.object.modifier_apply(modifier=modifier.name)
        if operation == 'UNION':
            bpy.data.objects.remove(below, do_unlink=True)
        else:
            below.hide_set(True)
        _sculpt(context, ob)
        return None

    def _union(self, context, ob):
        return self._boolean(context, ob, 'UNION')

    def _difference(self, context, ob):
        return self._boolean(context, ob, 'DIFFERENCE')

    def _intersect(self, context, ob):
        return self._boolean(context, ob, 'INTERSECT')

    def _center(self, context, ob):
        ob.location = (0.0, 0.0, 0.0)

    def _floor(self, context, ob):
        from mathutils import Vector
        lowest = min((ob.matrix_world @ Vector(corner)).z for corner in ob.bound_box)
        ob.location.z -= lowest

    def _show_all(self, context, ob):
        for other in _scene_meshes(context):
            other.hide_set(False)

    def _hide_others(self, context, ob):
        for other in _scene_meshes(context):
            if other != ob:
                other.hide_set(True)


class PhantomSimpleSceneRow(bpy.types.PropertyGroup):
    """A row of the Scene window: a mesh or a folder."""
    kind: EnumProperty(items=(('OBJECT', "Mesh", ""), ('FOLDER', "Folder", "")))
    depth: bpy.props.IntProperty()


class VIEW3D_UL_phantom_simple_scene_plain(UIList):
    """The Scene window's tree, as one list: folders and meshes start on one left edge, a folder's
    meshes a step in; each row's eye on the right, a folder's arrow before it. The mesh being
    sculpted is highlighted; clicking a mesh sculpts it, clicking a folder opens or closes it."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # Which row this is, for its right-click menu and the Delete key (see
        # `_draw_scene_row_menu` and `view3d.phantom_simple_scene_plain_delete`).
        layout.context_pointer_set("phantom_scene_row", item)
        row = layout.row(align=True)
        if item.depth:
            # One step in per folder level (16 unscaled pixels).
            row.separator(factor=SCENE_INDENT * item.depth)
        if item.kind == 'FOLDER':
            collection = bpy.data.collections.get(item.name)
            closed = bool(collection and collection.get(FOLDER_CLOSED))
            row.label(text=item.name, icon='FILE_FOLDER', translate=False)
            arrow = row.operator(VIEW3D_OT_phantom_simple_folder.bl_idname, text="",
                                 icon='RIGHTARROW' if closed else 'DOWNARROW_HLT', emboss=False)
            arrow.name, arrow.action = item.name, 'TOGGLE'
            layer = _layer_collection(context, item.name)
            hidden = layer is not None and layer.hide_viewport
            eye = row.operator(VIEW3D_OT_phantom_simple_folder.bl_idname, text="",
                               icon='HIDE_ON' if hidden else 'HIDE_OFF', emboss=False)
            eye.name, eye.action = item.name, 'VISIBILITY'
            return
        ob = context.view_layer.objects.get(item.name)
        row.label(text=item.name, icon='OUTLINER_OB_MESH', translate=False)
        visible = ob is not None and not ob.hide_get()
        eye = row.operator(VIEW3D_OT_phantom_simple_subtool.bl_idname, text="",
                           icon='HIDE_OFF' if visible else 'HIDE_ON', emboss=False)
        eye.name, eye.action = item.name, 'VISIBILITY'

    def draw_filter(self, context, layout):
        pass


def _scene_index_get(_self):
    ob = bpy.context.active_object
    rows = bpy.context.window_manager.phantom_simple_scene_rows
    if ob is None:
        return -1
    return next((i for i, r in enumerate(rows) if r.kind == 'OBJECT' and r.name == ob.name), -1)


def _scene_index_set(_self, value):
    """Clicking a row: a mesh is sculpted, a folder opens or closes (once the click is handled)."""
    rows = bpy.context.window_manager.phantom_simple_scene_rows
    if not 0 <= value < len(rows):
        return
    kind, name = rows[value].kind, rows[value].name
    window = bpy.context.window
    pointer = window.as_pointer() if window is not None else None

    def switch():
        window = _window_by_pointer(pointer)
        area = _view3d_area(window.screen) if window is not None else None
        if area is None:
            return None
        with bpy.context.temp_override(window=window, screen=window.screen, area=area,
                                       region=_region(area, 'WINDOW')):
            if kind == 'FOLDER':
                bpy.ops.view3d.phantom_simple_folder(name=name, action='TOGGLE')
            else:
                bpy.ops.view3d.phantom_simple_subtool(name=name, action='SELECT')
        _redraw_simple()
        return None

    bpy.app.timers.register(switch, first_interval=0.0)


class VIEW3D_OT_phantom_simple_folder(Operator):
    """Open or close this folder, or show or hide it"""
    bl_idname = "view3d.phantom_simple_folder"
    bl_label = "Folder"
    bl_options = {'INTERNAL'}

    name: StringProperty(options={'SKIP_SAVE'})
    action: StringProperty(default='TOGGLE', options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return "Show or hide this folder" if properties.action == 'VISIBILITY' else \
            "Open or close this folder"

    def execute(self, context):
        collection = bpy.data.collections.get(self.name)
        if collection is None:
            return {'CANCELLED'}
        if self.action == 'VISIBILITY':
            layer = _layer_collection(context, self.name)
            if layer is not None:
                layer.hide_viewport = not layer.hide_viewport
        else:
            collection[FOLDER_CLOSED] = not collection.get(FOLDER_CLOSED, False)
        _sync_scene_rows(context)
        _redraw_simple()
        return {'FINISHED'}


class _NameDialog:
    """A small dialog in the simple view's style asking for a name: its field (dark, typed in
    right away), then Cancel and Accept."""

    def _ask(self, context, event, title):
        # Next to where it was asked for (the Scene window's button), in the simple view's style.
        wm = context.window_manager
        anchor = hasattr(wm, "phantom_popup_anchor_set")
        if anchor:
            x, y = event.mouse_x, event.mouse_y
            wm.phantom_popup_anchor_set(x + 8, y - 2, x + 10, y, 'RIGHT', simple_style=True)
        try:
            return wm.invoke_props_dialog(self, width=NAME_DIALOG_WIDTH, title=title,
                                          confirm_text="Accept")
        finally:
            if anchor:
                wm.phantom_popup_anchor_set(0, 0, 0, 0, 'NONE')

    def draw(self, context):
        layout = self.layout
        layout.activate_init = True
        layout.prop(self, "name", text="")


class VIEW3D_OT_phantom_simple_new_folder(_NameDialog, Operator):
    """Make a folder (a collection) and put the mesh being sculpted in it"""
    bl_idname = "view3d.phantom_simple_new_folder"
    bl_label = "New Folder"
    bl_options = {'INTERNAL', 'UNDO'}

    name: StringProperty(name="Name", default="Folder", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        return self._ask(context, event, "New Folder")

    def execute(self, context):
        name = self.name.strip() or "Folder"
        folder = bpy.data.collections.new(name)
        folder[FOLDER_TAG] = True
        context.scene.collection.children.link(folder)
        ob = context.active_object
        if ob is not None and ob.type == 'MESH':
            # Moving it between collections takes it out of the view layer a moment: from
            # Object Mode, then sculpting it again.
            bpy.ops.object.mode_set(mode='OBJECT')
            folder.objects.link(ob)
            for collection in list(ob.users_collection):
                if collection != folder:
                    collection.objects.unlink(ob)
            _sculpt(context, ob)
        _sync_scene_rows(context)
        _redraw_simple()
        return {'FINISHED'}


class VIEW3D_OT_phantom_simple_rename(_NameDialog, Operator):
    """Rename this mesh or folder"""
    bl_idname = "view3d.phantom_simple_rename"
    bl_label = "Rename"
    bl_options = {'INTERNAL', 'UNDO'}

    name: StringProperty(name="Name", options={'SKIP_SAVE'})
    # What to rename: a mesh's or a folder's name ('' : the mesh being sculpted).
    target: StringProperty(options={'SKIP_SAVE', 'HIDDEN'})
    folder: bpy.props.BoolProperty(options={'SKIP_SAVE', 'HIDDEN'})

    def _id(self, context):
        if self.folder:
            return bpy.data.collections.get(self.target)
        if self.target:
            return bpy.data.objects.get(self.target)
        return context.active_object

    @classmethod
    def poll(cls, context):
        return True

    def invoke(self, context, event):
        block = self._id(context)
        if block is None:
            return {'CANCELLED'}
        self.name = block.name
        return self._ask(context, event, "Rename Folder" if self.folder else "Rename")

    def execute(self, context):
        block = self._id(context)
        if block is not None and self.name.strip():
            block.name = self.name.strip()
        _sync_scene_rows(context)
        _redraw_simple()
        return {'FINISHED'}


class VIEW3D_OT_phantom_simple_scene_plain_delete(Operator):
    """Delete the mesh or folder under the cursor in the Scene window (a folder's meshes stay).
    Its name follows the list type's: Delete over a row of that list runs it (see
    `handle_uilist_event`)"""
    bl_idname = "view3d.phantom_simple_scene_plain_delete"
    bl_label = "Delete"
    bl_options = {'INTERNAL', 'UNDO'}

    name: StringProperty(options={'SKIP_SAVE'})
    folder: bpy.props.BoolProperty(options={'SKIP_SAVE'})

    def invoke(self, context, event):
        row = getattr(context, "phantom_scene_row", None)
        if not self.name:
            if row is None:
                return {'PASS_THROUGH'}
            self.name, self.folder = row.name, row.kind == 'FOLDER'
        if not self.folder:
            # A mesh: the Scene window's delete, which asks first and keeps something to sculpt.
            return bpy.ops.view3d.phantom_simple_subtool_delete('INVOKE_DEFAULT', name=self.name)
        return context.window_manager.invoke_confirm(
            self, event, title=f"Delete {self.name}", message="Its meshes stay in the scene",
            confirm_text="Delete", icon='WARNING')

    def execute(self, context):
        folder = bpy.data.collections.get(self.name)
        if folder is None:
            return {'CANCELLED'}
        # Its meshes (and folders) go to where it was, then it goes.
        parents = [c for c in [context.scene.collection, *bpy.data.collections]
                   if folder.name in c.children]
        parent = parents[0] if parents else context.scene.collection
        active = context.active_object
        sculpting = context.mode == 'SCULPT'
        if sculpting:
            bpy.ops.object.mode_set(mode='OBJECT')
        for ob in list(folder.objects):
            if ob.name not in parent.objects:
                parent.objects.link(ob)
        for child in list(folder.children):
            if child.name not in parent.children:
                parent.children.link(child)
        bpy.data.collections.remove(folder)
        if sculpting and active is not None:
            _sculpt(context, active)
        _sync_scene_rows(context)
        _redraw_simple()
        return {'FINISHED'}


def _draw_scene_row_menu(self, context):
    """A Scene window row's right-click menu: rename or delete it."""
    row = getattr(context, "phantom_scene_row", None)
    if row is None:
        return
    layout = self.layout
    folder = row.kind == 'FOLDER'
    op = layout.operator(VIEW3D_OT_phantom_simple_rename.bl_idname,
                         text="Rename Folder\u2026" if folder else "Rename\u2026",
                         icon='GREASEPENCIL')
    op.target, op.folder = row.name, folder
    op = layout.operator(VIEW3D_OT_phantom_simple_scene_plain_delete.bl_idname,
                         text="Delete Folder" if folder else "Delete", icon='TRASH')
    op.name, op.folder = row.name, folder


class VIEW3D_OT_phantom_simple_scene_menu(Operator):
    """Open this menu (in the simple view's style, beside the cursor)"""
    bl_idname = "view3d.phantom_simple_scene_menu"
    bl_label = "Menu"
    bl_options = {'INTERNAL'}

    menu: StringProperty(options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return {"VIEW3D_MT_phantom_simple_add_mesh": "Add a mesh to the scene",
                "VIEW3D_MT_phantom_simple_scene_more": "Rename, show all, hide others, delete"}.get(
                    properties.menu, "")

    def invoke(self, context, event):
        wm = context.window_manager
        anchor = hasattr(wm, "phantom_popup_anchor_set")
        if anchor:
            x, y = event.mouse_x, event.mouse_y
            wm.phantom_popup_anchor_set(x + 8, y - 2, x + 10, y, 'RIGHT', simple_style=True)
        try:
            bpy.ops.wm.call_menu(name=self.menu)
        finally:
            if anchor:
                wm.phantom_popup_anchor_set(0, 0, 0, 0, 'NONE')
        return {'FINISHED'}


class VIEW3D_MT_phantom_simple_scene_more(Menu):
    bl_label = "Scene"

    def draw(self, context):
        layout = self.layout
        ob = context.active_object
        if ob is not None:
            layout.operator(VIEW3D_OT_phantom_simple_rename.bl_idname,
                            text=f"Rename \u201c{ob.name}\u201d\u2026", icon='GREASEPENCIL')
            layout.separator()
        op = VIEW3D_OT_phantom_simple_scene_action.bl_idname
        layout.operator(op, text="Show All", icon='HIDE_OFF').action = 'SHOW_ALL'
        layout.operator(op, text="Hide Others", icon='HIDE_ON').action = 'HIDE_OTHERS'
        if ob is not None:
            layout.separator()
            layout.operator(VIEW3D_OT_phantom_simple_subtool_delete.bl_idname, text="Delete",
                            icon='TRASH').name = ob.name


class VIEW3D_MT_phantom_simple_boolean(Menu):
    bl_label = "Boolean"

    def draw(self, context):
        layout = self.layout
        op = VIEW3D_OT_phantom_simple_scene_action.bl_idname
        layout.operator(op, text="Union", icon='SELECT_EXTEND').action = 'UNION'
        layout.operator(op, text="Difference", icon='SELECT_SUBTRACT').action = 'DIFFERENCE'
        layout.operator(op, text="Intersect", icon='SELECT_INTERSECT').action = 'INTERSECT'


class VIEW3D_PT_phantom_simple_subtools(Panel):
    """The meshes in the scene, in folders, and what can be done with them (as ZBrush for iPad's
    Subtools)"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "Scene"
    bl_ui_units_x = 14

    def draw(self, context):
        layout = self.layout
        op = VIEW3D_OT_phantom_simple_scene_action.bl_idname
        layout.label(text="Scene")
        # Add, duplicate, a new folder, more: bare icons spread evenly, nothing around them.
        header = layout.grid_flow(row_major=True, columns=4, even_columns=True, align=False)
        header.scale_y = 1.3
        menu = VIEW3D_OT_phantom_simple_scene_menu.bl_idname
        header.operator(menu, text="", icon='ADD', emboss=False).menu = \
            VIEW3D_MT_phantom_simple_add_mesh.__name__
        header.operator(op, text="", icon='DUPLICATE', emboss=False).action = 'DUPLICATE'
        header.operator(VIEW3D_OT_phantom_simple_new_folder.bl_idname, text="", icon='FILE_FOLDER',
                        emboss=False)
        header.operator(menu, text="", icon='THREE_DOTS', emboss=False).menu = \
            VIEW3D_MT_phantom_simple_scene_more.__name__
        layout.separator()

        wm = context.window_manager
        rows = len(wm.phantom_simple_scene_rows)
        if rows:
            layout.template_list(VIEW3D_UL_phantom_simple_scene_plain.__name__, "", wm,
                                 "phantom_simple_scene_rows", wm, "phantom_simple_scene_index",
                                 rows=rows, maxrows=rows, sort_lock=True)
        else:
            layout.label(text="No meshes")

        layout.separator()
        header, body = layout.panel("phantom_simple_scene_actions", default_closed=True)
        header.label(text="Actions")
        if body is not None:
            col = body.column(align=True)
            col.scale_y = 1.3
            col.menu("OBJECT_MT_modifier_add", text="Add Modifier", icon='MODIFIER')
            col.separator()
            col.operator(op, text="Split to Parts", icon='MOD_EXPLODE').action = 'SPLIT_PARTS'
            col.operator("sculpt.phantom_split_hidden", text="Split Hidden", icon='HIDE_ON')
            col.separator()
            col.operator(op, text="Merge Down", icon='TRIA_DOWN_BAR').action = 'MERGE_DOWN'
            col.operator(op, text="Merge Visible", icon='SELECT_EXTEND').action = 'MERGE_VISIBLE'
            col.menu(VIEW3D_MT_phantom_simple_boolean.__name__, text="Boolean", icon='MOD_BOOLEAN')
            col.separator()
            col.operator(op, text="Center", icon='PIVOT_CURSOR').action = 'CENTER'
            col.operator(op, text="On Floor", icon='TRIA_DOWN').action = 'FLOOR'


class TOPBAR_PT_phantom_simple_customize(Panel):
    """What the simple view shows"""
    bl_space_type = 'TOPBAR'
    bl_region_type = 'HEADER'
    bl_label = "Customize"
    bl_ui_units_x = 13

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        layout.label(text="Simple View")
        col = layout.column()
        col.prop(wm, "phantom_simple_right_bar", text="Right Toolbar")
        col.prop(wm, "phantom_simple_bottom_bar", text="Bottom Toolbar")
        col.prop(wm, "phantom_simple_left_handed", text="Left-Handed (Swap Toolbars)")
        col.prop(wm, "phantom_simple_stats", text="Statistics Under the Thumbnail")
        if hasattr(wm, "phantom_silhouette"):
            col.prop(wm, "phantom_silhouette", text="Silhouette Thumbnail")
            sub = col.row()
            sub.active = wm.phantom_silhouette
            sub.prop(context.preferences, "show_silhouette_always", text="Silhouette in Every Mode")
        col.prop(context.preferences.view, "show_navigate_head", text="Head Navigation Gizmo")
        layout.separator()
        sub = layout.column()
        sub.enabled = False
        sub.label(text="Quick brushes: right-click one to change it")


class TOPBAR_PT_phantom_simple_help(Panel):
    """How the simple view works"""
    bl_space_type = 'TOPBAR'
    bl_region_type = 'HEADER'
    bl_label = "Help"
    bl_ui_units_x = 17

    LINES = (
        ("Home", "the File menu"),
        ("Swap or Shift Tab", "back to the usual interface"),
        ("Click a button", "use it"),
        ("Right-click or hold", "the button's options"),
        ("Drag a ring up or down", "change Draw Size, Focal Shift, Z or RGB Intensity"),
        ("Shift", "smooth"),
        ("Ctrl", "mask (Ctrl-drag off the mesh: box mask)"),
        ("Ctrl Shift", "trim, Ctrl Shift-click: isolate a polygroup"),
        ("Alt", "Sub instead of Add"),
        ("S / Shift F", "draw size / Z intensity"),
        ("W", "move with the gizmo or Transpose"),
        ("X", "symmetry"),
        ("Ctrl D", "divide"),
        ("F3", "search for anything"),
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Simple View", icon='INFO')
        split = layout.split(factor=0.42)
        keys, what = split.column(), split.column()
        for key, text in self.LINES:
            keys.label(text=key)
            what.label(text=text)


class TOPBAR_OT_phantom_simple_file_menu(Operator):
    """The File menu: new, open, save, import, export..."""
    bl_idname = "topbar.phantom_simple_file_menu"
    bl_label = "File"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        wm = context.window_manager
        region = context.region
        if hasattr(wm, "phantom_popup_anchor_set") and region is not None:
            # Under the button, its left edge with the button's, in the toolbars' style: anchored
            # to the right of a sliver ending there, its top at the bar's bottom.
            s = _ui_scale()
            edge = event.mouse_x - 14 * s
            bottom = region.y - 4 * s
            wm.phantom_popup_anchor_set(int(edge - 8 * s), int(bottom - 2), int(edge - 6 * s),
                                        int(bottom), 'RIGHT', simple_style=True)
        try:
            bpy.ops.wm.call_menu(name="TOPBAR_MT_file")
        finally:
            if hasattr(wm, "phantom_popup_anchor_set"):
                wm.phantom_popup_anchor_set(0, 0, 0, 0, 'NONE')
        return {'FINISHED'}


def _top_bar_group(layout):
    row = layout.row(align=True)
    row.scale_x = 1.25
    return row


def draw_top_bar(layout, context, right):
    """The window's top bar in the simple view (called from TOPBAR_HT_upper_bar). Everything is in
    the bar's left part, which spans nearly the whole window: Home, Swap, Save, Import and Export,
    the file's name in the middle, then History, Customize, Help and Settings. The right part only
    shows reports and running jobs (the status bar is hidden), so it is usually empty."""
    if right:
        layout.template_reports_banner()
        layout.template_running_jobs()
        return
    row = _top_bar_group(layout)
    row.operator(TOPBAR_OT_phantom_simple_file_menu.bl_idname, text="", icon='HOME', emboss=False)
    row.operator(VIEW3D_OT_phantom_simple_view_toggle.bl_idname, text="", icon='ARROW_LEFTRIGHT',
                 emboss=False)
    row.separator()
    row.operator("wm.save_mainfile", text="", icon='FILE_TICK', emboss=False)
    row.operator("wm.call_menu", text="", icon='IMPORT', emboss=False).name = "TOPBAR_MT_file_import"
    row.operator("wm.call_menu", text="", icon='EXPORT', emboss=False).name = "TOPBAR_MT_file_export"

    # Between two spacers the header centers what is there on the bar's left part, a little left of
    # the window's middle (its right part, measured): a pad before the name makes up for both.
    # A button, not a label (a label's text sits at the left of its box). Clicking it saves under
    # another name.
    layout.separator_spacer()
    right_part = max((r.width for r in context.area.regions
                      if r.type == 'HEADER' and r.alignment == 'RIGHT'), default=0)
    layout.separator(factor=(TOP_BAR_TITLE_PAD + right_part / context.preferences.system.ui_scale)
                     / 6.0)
    path = bpy.data.filepath
    name = os.path.splitext(os.path.basename(path))[0] if path else "Untitled"
    if bpy.data.is_dirty:
        name += "*"
    layout.operator("wm.save_as_mainfile", text=name, translate=False, emboss=False)
    layout.separator_spacer()

    row = _top_bar_group(layout)
    ob = context.active_object
    if ob is not None and ob.type == 'MESH':
        # Symmetry: X, Y and Z, and its dropdown (lock, tiling, radial, symmetrize...).
        sym = row.row(align=True)
        for axis in "XYZ":
            sym.prop(ob.data, "use_mirror_" + axis.lower(), text=axis, toggle=True)
        sym.popover(panel="VIEW3D_PT_sculpt_symmetry_for_topbar", text="")
        row.separator()
    wm = context.window_manager
    if hasattr(wm, "phantom_history_bar_show"):
        # The timeline on or off, and its dropdown with every history setting.
        row.prop(wm, "phantom_history_bar_show", text="", icon='TIME', emboss=True, toggle=True)
        row.popover(panel="VIEW3D_PT_phantom_history", text="")
    else:
        row.operator("wm.call_menu", text="", icon='TIME', emboss=False).name = "TOPBAR_MT_undo_history"
    row.separator()
    row.popover(TOPBAR_PT_phantom_simple_customize.__name__, text="", icon='WINDOW')
    row.popover(TOPBAR_PT_phantom_simple_help.__name__, text="", icon='QUESTION')
    row.operator("screen.userpref_show", text="", icon='PREFERENCES', emboss=False)


def _draw_view_menu(self, context):
    if context.mode == 'SCULPT':
        self.layout.separator()
        self.layout.operator(VIEW3D_OT_phantom_simple_view_toggle.bl_idname, text="Simple View",
                             icon='FULLSCREEN_ENTER')


# -----------------------------------------------------------------------------
# Window manager properties (the Customize pop-over)

def _make_accessors(key):
    def getter(_self):
        return _settings[key]

    def setter(_self, value):
        if _settings[key] == value:
            return
        _set_setting(key, value)
        if key == "stats":
            wm = bpy.context.window_manager
            for window in wm.windows:
                if is_simple_screen(window.screen):
                    area = _view3d_area(window.screen)
                    if area is not None:
                        area.spaces.active.overlay.show_stats = value
        _redraw_simple()

    return getter, setter


_PROPS = {
    "phantom_simple_left_handed": ("left_handed", "Left-Handed",
                                   "Put the brush toolbar on the right and the view toolbar on the left"),
    "phantom_simple_right_bar": ("right_bar", "Right Toolbar", "Show the view toolbar"),
    "phantom_simple_bottom_bar": ("bottom_bar", "Bottom Toolbar",
                                  "Show the quick brushes and the remesh tools at the bottom"),
    "phantom_simple_stats": ("stats", "Statistics", "Show the scene statistics under the thumbnail"),
}


# -----------------------------------------------------------------------------
# Registration

classes = (
    TOPBAR_OT_phantom_simple_file_menu,
    VIEW3D_PT_phantom_simple_tools,
    VIEW3D_OT_phantom_simple_view_toggle,
    VIEW3D_OT_phantom_simple_press,
    VIEW3D_OT_phantom_simple_options,
    VIEW3D_OT_phantom_simple_ring_card,
    VIEW3D_OT_phantom_simple_color_card,
    VIEW3D_OT_phantom_simple_matcap_card,
    VIEW3D_OT_phantom_simple_brush_strip,
    VIEW3D_OT_phantom_simple_add_primitive,
    VIEW3D_OT_phantom_simple_subtool_delete,
    VIEW3D_OT_phantom_simple_place,
    VIEW3D_MT_phantom_simple_add_mesh,
    VIEW3D_OT_phantom_simple_hover,
    VIEW3D_OT_phantom_simple_subtool,
    VIEW3D_OT_phantom_simple_slot,
    VIEW3D_MT_phantom_simple_slot,
    VIEW3D_PT_phantom_simple_quick_menu,
    VIEW3D_OT_phantom_simple_scene_action,
    PhantomSimpleSceneRow,
    VIEW3D_UL_phantom_simple_scene_plain,
    VIEW3D_OT_phantom_simple_folder,
    VIEW3D_OT_phantom_simple_new_folder,
    VIEW3D_OT_phantom_simple_rename,
    VIEW3D_OT_phantom_simple_scene_plain_delete,
    VIEW3D_OT_phantom_simple_scene_menu,
    VIEW3D_MT_phantom_simple_scene_more,
    VIEW3D_MT_phantom_simple_boolean,
    VIEW3D_PT_phantom_simple_subtools,
    TOPBAR_PT_phantom_simple_customize,
    TOPBAR_PT_phantom_simple_help,
)


def register():
    global _draw_handle
    _load_settings()
    for prop_name, (key, name, description) in _PROPS.items():
        get, set_ = _make_accessors(key)
        setattr(bpy.types.WindowManager, prop_name,
                BoolProperty(name=name, description=description, get=get, set=set_))
    bpy.types.WindowManager.phantom_simple_scene_index = bpy.props.IntProperty(
        name="Mesh", description="The mesh being sculpted (the Scene window's list)",
        get=_scene_index_get, set=_scene_index_set)
    bpy.types.WindowManager.phantom_simple_tool_category = EnumProperty(
        name="Tools", description="Which tools the tool menu shows", items=_tool_category_items,
        get=_tool_category_get, set=_tool_category_set)
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.phantom_simple_scene_rows = bpy.props.CollectionProperty(
        type=PhantomSimpleSceneRow)
    bpy.types.VIEW3D_MT_view.append(_draw_view_menu)
    bpy.types.UI_MT_list_item_context_menu.append(_draw_scene_row_menu)
    bpy.app.handlers.load_post.append(_on_load)
    bpy.app.handlers.save_pre.append(_restore_brush_circle)
    bpy.app.handlers.load_pre.append(_restore_brush_circle)
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(draw_callback, (), 'WINDOW', 'POST_PIXEL')
    if not bpy.app.background:
        bpy.app.timers.register(_watch, first_interval=1.0, persistent=True)

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Sculpt", space_type='EMPTY')
        for idname, key, value, kwargs in (
            (VIEW3D_OT_phantom_simple_press.bl_idname, 'LEFTMOUSE', 'PRESS', {"any": True}),
            (VIEW3D_OT_phantom_simple_options.bl_idname, 'RIGHTMOUSE', 'PRESS', {"any": True}),
            (VIEW3D_OT_phantom_simple_hover.bl_idname, 'MOUSEMOVE', 'ANY', {"any": True}),
            (VIEW3D_OT_phantom_simple_view_toggle.bl_idname, 'TAB', 'PRESS', {"shift": True}),
        ):
            _addon_keymaps.append((km, km.keymap_items.new(idname, key, value, **kwargs)))


def unregister():
    global _draw_handle
    for km, kmi in _addon_keymaps:
        km.keymap_items.remove(kmi)
    _addon_keymaps.clear()
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    for timer in (_watch, _anim_tick, _load_pending):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    for handlers, fn in ((bpy.app.handlers.load_post, _on_load),
                         (bpy.app.handlers.save_pre, _restore_brush_circle),
                         (bpy.app.handlers.load_pre, _restore_brush_circle)):
        if fn in handlers:
            handlers.remove(fn)
    bpy.types.VIEW3D_MT_view.remove(_draw_view_menu)
    bpy.types.UI_MT_list_item_context_menu.remove(_draw_scene_row_menu)
    # Before its row type goes.
    del bpy.types.WindowManager.phantom_simple_scene_rows
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    for prop_name in _PROPS:
        delattr(bpy.types.WindowManager, prop_name)
    del bpy.types.WindowManager.phantom_simple_tool_category
    del bpy.types.WindowManager.phantom_simple_scene_index
    _forget_thumbnails()
