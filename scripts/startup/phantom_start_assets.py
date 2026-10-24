# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: pick something to sculpt when the canvas is empty.

Switching to a sculpting workspace (one that starts in Sculpt Mode, such as Sculpting) with no mesh
in the scene opens a picker in the middle of the 3D view, with thumbnails four across: a quad
sphere like the Sculpting template's, and Blender's free human base meshes (bodies, heads and
parts), which ship with Phantom in the Essentials asset library
(``datafiles/assets/human_base_meshes``, CC0, by Blender Studio artists), plus Phantom's own base
meshes (``datafiles/assets/phantom/base_meshes``, see ``PHANTOM_BASE_MESHES``).

Click a thumbnail to select it and double-click, press Enter or click **Start Sculpting** to start:
the mesh is appended, framed from the front and put in Sculpt Mode. Scroll for more; Esc,
right-click or a click outside closes it. The picker is also in the 3D View's Add menu ("Base
Mesh...").

Along the top: a search field (just type: it filters the meshes by name), **Open...** (a .blend
from disk) and **Import...** (Blender's import menu: OBJ, FBX, STL, PLY, glTF...; the imported mesh
is framed and sculpted like a base mesh). Bottom left, **Show at startup** opens the picker as
soon as Phantom starts and for every new file (remembered in Phantom's config folder, in
``phantom_start.json``).
"""

import json

import math
import os
import time

import blf
import bpy
import gpu
import numpy as np
from bpy.props import EnumProperty
from bpy.app.handlers import persistent
from bpy.types import Operator
from gpu_extras.batch import batch_for_shader
from mathutils import Euler

BUNDLE = os.path.join("assets", "human_base_meshes", "human_base_meshes_bundle.blend")
BUNDLE_THUMBNAILS = os.path.join("assets", "human_base_meshes", "thumbnails")
PHANTOM_THUMBNAILS = os.path.join("assets", "phantom", "thumbnails")
WATCH_INTERVAL = 0.3
COLUMNS = 4
# Seconds the picker takes to fade in.
FADE_TIME = 0.18
# How far tiles move (share of the way, per 1/60 s) towards where the search puts them, and how
# fast they fade in and out (per 1/60 s). The others wait for the fading ones to mostly go.
TILE_EASE = 0.22
TILE_FADE = 0.12
TILE_FADE_OUT = 0.06
# Phantom's own settings for the picker, in the user config folder.
SETTINGS_FILE = "phantom_start.json"
# How long (s) to wait for an import started from the picker to bring in a mesh.
IMPORT_WAIT = 600.0

# (key, label, kind, name in the bundle, thumbnail). Kind: 'SPHERE', 'COLLECTION' or 'OBJECT'.
# The first ones are listed under "Start with", the rest under "More base meshes".
STARTERS = (
    ('QUAD_SPHERE', "Quad Sphere", 'SPHERE', "", "quad_sphere.png"),
    ('BODY_FEMALE_REALISTIC', "Female", 'COLLECTION', "Body Female - Realistic", "realistic_body_female.png"),
    ('BODY_MALE_REALISTIC', "Male", 'COLLECTION', "Body Male - Realistic", "realistic_body_male.png"),
    ('HEAD_SCULPT', "Head", 'COLLECTION', "Head (Sculpting) - Realistic", "realistic_head_sculpt.png"),
    ('BODY_FEMALE_STYLIZED_1', "Stylized Female 1", 'COLLECTION', "Body Female 01 - Stylized",
     "stylized_female_alley.png"),
    ('BODY_FEMALE_STYLIZED', "Stylized Female 2", 'COLLECTION', "Body Female - Stylized", "stylized_body_female.png"),
    ('BODY_MALE_STYLIZED', "Male Stylized", 'COLLECTION', "Body Male - Stylized", "stylized_body_male.png"),
    ('HEAD_STYLIZED', "Head Stylized", 'COLLECTION', "Head - Stylized", "stylized_head.png"),
)
MORE = (
    ('SKULL_REALISTIC', "Skull", 'OBJECT', "Skull - Realistic", "realistic_skull.png"),
    ('HEAD_ANIMATION', "Head (Animation)", 'COLLECTION', "Head (Animation) - Realistic", "realistic_head_anim.png"),
    ('HEAD_PLANAR', "Head Planar", 'OBJECT', "Head - Planar", "planar_head.png"),
    ('HEAD_GENERIC', "Head Generic", 'OBJECT', "Head - Generic Topology", "primitive_head_generic.png"),
    ('HEAD_PRIMITIVES', "Head Primitives", 'COLLECTION', "Head - Primitives", "primitive_head_realistic.png"),
    ('SKULL_PLANAR', "Skull Planar", 'OBJECT', "Skull - Planar", "planar_skull.png"),
    ('SKELETON', "Skeleton", 'COLLECTION', "Skeleton - Realistic", "realistic_skeleton.png"),
    ('BODY_FEMALE_PRIMITIVE', "Female Primitive", 'COLLECTION', "Body Female - Primitve (Realistic)",
     "primitive_body_female_realistic.png"),
    ('BODY_MALE_PRIMITIVE', "Male Primitive", 'COLLECTION', "Body Male - Primitve (Realistic)",
     "primitive_body_male_realistic.png"),
    ('BODY_FEMALE_PRIMITIVE_STYLIZED', "Female Prim. Stylized", 'COLLECTION', "Body Female - Primitive (Stylized)",
     "primitive_body_female_stylized.png"),
    ('BODY_MALE_PRIMITIVE_STYLIZED', "Male Prim. Stylized", 'COLLECTION', "Body Male - Primitve (Stylized)",
     "primitive_body_male_stylized.png"),
    ('HAND_REALISTIC', "Hand", 'OBJECT', "Hand  - Realistic", "realistic_hand.png"),
    ('HAND_STYLIZED', "Hand Stylized", 'OBJECT', "Hand - Stylized ", "stylized_hand.png"),
    ('FOOT_REALISTIC', "Foot", 'OBJECT', "Foot - Realistic", "realistic_foot.png"),
    ('FOOT_STYLIZED', "Foot Stylized", 'OBJECT', "Foot - Stylized ", "stylized_foot.png"),
    ('EYE_REALISTIC', "Eye", 'OBJECT', "Eye - Realistic", "realistic_eye.png"),
    ('EYE_STYLIZED', "Eye Stylized", 'OBJECT', "Eye - Stylized", "stylized_eye.png"),
    ('JAW_REALISTIC', "Jaw", 'OBJECT', "Jaw - Realistic", "realistic_jaw.png"),
    ('JAW_STYLIZED', "Jaw Stylized", 'OBJECT', "Jaw - Stylized ", "stylized_jaw.png"),
)
ALL = {item[0]: item for item in STARTERS + MORE}
# Base meshes that come with Phantom itself rather than the Human Base Meshes bundle: key -> .blend
# in assets/phantom (their thumbnails are in assets/phantom/thumbnails).
PHANTOM_BASE_MESHES = {
    'BODY_FEMALE_STYLIZED_1': os.path.join("assets", "phantom", "base_meshes", "stylized_female_alley.blend"),
}

# Thumbnails: pixels read from the PNGs once, and GPU textures made from them when drawing.
_thumbs = {"pixels": None, "textures": {}}
# The open picker's state.
_picker = {"open": False, "region": 0, "hover": None, "selected": 'QUAD_SPHERE', "scroll": 0.0, "target": 0.0,
           "opened": 0.0, "query": "", "button": None, "at_startup": False, "quiet_until": 0.0,
           "anim": {}}
# An import started from the picker: the meshes there were before it, and until when to wait.
_import = {"before": None, "deadline": 0.0}
# The workspace each window showed at the last check, to notice switches.
_last_workspace = {}


def _datafile(*parts):
    # ``system_resource`` only finds folders, so join the file onto the datafiles folder.
    return os.path.join(bpy.utils.system_resource('DATAFILES'), *parts)


def _settings_path():
    return os.path.join(bpy.utils.user_resource('CONFIG'), SETTINGS_FILE)


def _settings():
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def show_at_startup():
    return bool(_settings().get("show_at_startup", False))


def set_show_at_startup(value):
    data = _settings()
    data["show_at_startup"] = bool(value)
    try:
        os.makedirs(os.path.dirname(_settings_path()), exist_ok=True)
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as ex:
        print("Phantom: couldn't save the start picker settings:", ex)


def _sections():
    """The picker's sections with the items matching the search: [(label, items)], empty ones left out."""
    words = _picker["query"].lower().split()
    sections = []
    for label, items in (("Start with", STARTERS), ("More base meshes", MORE)):
        shown = [item for item in items if all(w in item[1].lower() or w in item[3].lower() for w in words)]
        if shown:
            sections.append((label, shown))
    return sections


def canvas_is_empty(scene):
    return not any(ob.type == 'MESH' for ob in scene.objects)


def _play(name):
    # Phantom's interface sounds, through the garment module's player when it's there.
    try:
        import phantom_garment
        phantom_garment.play_sound(name)
    except ImportError:
        pass


def _view3d(window):
    areas = [a for a in window.screen.areas if a.type == 'VIEW_3D']
    if not areas:
        return None, None
    area = max(areas, key=lambda a: a.width * a.height)
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    return area, region


def append_base_mesh(context, key, parent_collection=None, strip_drivers=False):
    """Append a base mesh (not the quad sphere) into the scene, in the middle of the world.

    ``strip_drivers``: drop the bundle's drivers (they drive mask modifiers from a rig that isn't
    appended, so they only fail and warn), before anything evaluates them.

    Returns (the main mesh, every object added, the collection or object appended), or None.
    """
    item = ALL[key]
    path = _datafile(PHANTOM_BASE_MESHES.get(key, BUNDLE))
    if item[2] == 'SPHERE' or not os.path.exists(path):
        return None
    parent_collection = parent_collection or context.scene.collection
    kind = "collections" if item[2] == 'COLLECTION' else "objects"
    with bpy.data.libraries.load(path, link=False) as (data_from, data_to):
        setattr(data_to, kind, [item[3]] if item[3] in getattr(data_from, kind) else [])
    loaded = [block for block in getattr(data_to, kind) if block is not None]
    if not loaded:
        return None
    block = loaded[0]
    if strip_drivers:
        for ob in (block.all_objects if item[2] == 'COLLECTION' else [block]):
            if ob.animation_data is not None:
                for fcurve in list(ob.animation_data.drivers):
                    ob.animation_data.drivers.remove(fcurve)
    if item[2] == 'COLLECTION':
        parent_collection.children.link(block)
        added = list(block.all_objects)
    else:
        parent_collection.objects.link(block)
        added = [block]
    # It's the user's own copy now, not a library asset.
    block.asset_clear()
    meshes = [ob for ob in added if ob.type == 'MESH']
    if not meshes:
        return None
    # The main mesh: the one with the most vertices.
    target = max(meshes, key=lambda ob: len(ob.data.vertices))
    # The bundle lays its assets out side by side; bring this one to the middle of the world
    # (keeping its height), with its parts together.
    context.view_layer.update()
    offset = target.matrix_world.translation.copy()
    offset.z = 0.0
    for ob in added:
        if ob.parent not in added:
            ob.location -= offset
    return target, added, block


def spawn(context, key):
    """Add a starter to the scene, frame it, and sculpt it. Returns the object to sculpt."""
    item = ALL[key]
    if context.object is not None and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in context.view_layer.objects:
        if ob is not None and ob.select_get():
            ob.select_set(False)

    if item[2] == 'SPHERE':
        bpy.ops.mesh.primitive_quad_sphere_add(segments=64)
        target = context.active_object
        added = [target]
    else:
        appended = append_base_mesh(context, key)
        if appended is None:
            return None
        target, added, _block = appended

    _frame_and_sculpt(context, target, added)
    return target


def _frame_and_sculpt(context, target, added):
    """Make ``target`` active, face its front framed in the 3D view, and sculpt it."""
    for ob in added:
        try:
            ob.select_set(True)
        except RuntimeError:
            pass
    context.view_layer.objects.active = target
    area, region = _view3d(context.window)
    if area is not None:
        # Face the front of the new mesh, framed, every time.
        region_3d = area.spaces.active.region_3d
        region_3d.view_rotation = Euler((math.radians(90.0), 0.0, 0.0)).to_quaternion()
        with context.temp_override(area=area, region=region):
            bpy.ops.view3d.view_selected()
    for ob in added:
        if ob != target:
            try:
                ob.select_set(False)
            except RuntimeError:
                pass
    bpy.ops.object.mode_set(mode='SCULPT')


class PHANTOM_OT_start_asset(Operator):
    """Add a base mesh and sculpt it: a quad sphere or one of Blender's human base meshes"""
    bl_idname = "phantom.start_asset"
    bl_label = "Add Base Mesh"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    choice: EnumProperty(
        name="Start With",
        items=[(item[0], item[1], "") for item in STARTERS + MORE],
        default='QUAD_SPHERE',
    )

    def execute(self, context):
        ob = spawn(context, self.choice)
        if ob is None:
            self.report({'ERROR'}, "Couldn't add the base mesh (are Phantom's assets installed?)")
            _play("error")
            return {'CANCELLED'}
        _play("mode_switch")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# The picker: a panel drawn in the middle of the 3D view.

def _load_pixels():
    """Thumbnail pixels, {key: (width, height, float array)}, read once."""
    if _thumbs["pixels"] is not None:
        return _thumbs["pixels"]
    pixels = {}
    for key, item in ALL.items():
        folder = PHANTOM_THUMBNAILS if item[2] == 'SPHERE' or key in PHANTOM_BASE_MESHES else BUNDLE_THUMBNAILS
        path = _datafile(folder, item[4])
        if not os.path.exists(path):
            continue
        image = bpy.data.images.load(path, check_existing=False)
        try:
            width, height = image.size
            data = np.empty(width * height * 4, dtype=np.float32)
            image.pixels.foreach_get(data)
            # Premultiply: the PNGs' transparent pixels are white in some and black in others.
            rgba = data.reshape(-1, 4)
            rgba[:, :3] *= rgba[:, 3:4]
            pixels[key] = (width, height, data)
        finally:
            bpy.data.images.remove(image)
    _thumbs["pixels"] = pixels
    return pixels


def _texture(key):
    """GPU texture of a thumbnail (made while drawing, where the GPU is available)."""
    texture = _thumbs["textures"].get(key)
    if texture is None:
        entry = (_thumbs["pixels"] or {}).get(key)
        if entry is None:
            return None
        width, height, data = entry
        texture = gpu.types.GPUTexture((width, height), format='RGBA16F',
                                       data=gpu.types.Buffer('FLOAT', len(data), data))
        _thumbs["textures"][key] = texture
    return texture


def _rounded_rect(x, y, w, h, radius, color, segments=6):
    radius = max(0.0, min(radius, w / 2.0, h / 2.0))
    corners = ((x + w - radius, y + h - radius, 0.0), (x + radius, y + h - radius, 0.5 * math.pi),
               (x + radius, y + radius, math.pi), (x + w - radius, y + radius, 1.5 * math.pi))
    outline = []
    for cx, cy, start in corners:
        for i in range(segments + 1):
            a = start + 0.5 * math.pi * i / segments
            outline.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    verts = [(x + w / 2.0, y + h / 2.0)] + outline + [outline[0]]
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'TRI_FAN', {"pos": verts})
    shader.uniform_float("color", color)
    batch.draw(shader)


def _image(texture, x, y, w, h, alpha):
    shader = gpu.shader.from_builtin('IMAGE_COLOR')
    batch = batch_for_shader(shader, 'TRI_FAN', {
        "pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h)),
        "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1)),
    })
    shader.uniform_sampler("image", texture)
    shader.uniform_float("color", (alpha, alpha, alpha, alpha))
    # Text drawing changes the blend mode, so set it for every image.
    gpu.state.blend_set('ALPHA_PREMULT')
    batch.draw(shader)
    gpu.state.blend_set('ALPHA')


def _text(x, y, text, size, color, align='CENTER'):
    font = 0
    blf.size(font, size)
    width, _height = blf.dimensions(font, text)
    blf.color(font, *color)
    blf.position(font, x - width / 2.0 if align == 'CENTER' else x, y, 0)
    blf.draw(font, text)


class _Layout:
    """Where everything goes, in region pixels, for a region and a scroll position."""

    def __init__(self, region, scroll):
        s = bpy.context.preferences.system.ui_scale
        self.s = s
        self.tile = 148 * s
        self.thumb = 124 * s
        self.label = 22 * s
        self.gap = 10 * s
        self.pad = 20 * s
        self.header = 104 * s
        self.footer = 56 * s
        self.section = 30 * s
        self.width = COLUMNS * self.tile + (COLUMNS - 1) * self.gap + 2 * self.pad
        row_h = self.thumb + self.label + self.gap
        sections = _sections()
        rows = sum(-(-len(items) // COLUMNS) for _label, items in sections)
        self.content = max(len(sections) * self.section + rows * row_h + self.pad, 80 * s)
        # The window's size doesn't follow the search: it's sized for everything.
        all_rows = sum(-(-len(items) // COLUMNS) for items in (STARTERS, MORE))
        full = 2 * self.section + all_rows * row_h + self.pad
        self.height = min(self.header + full + self.footer, region.height * 0.85)
        self.x = (region.width - self.width) / 2.0
        self.y = (region.height - self.height) / 2.0
        self.view_top = self.y + self.height - self.header
        self.view_bottom = self.y + self.footer
        self.view_h = self.view_top - self.view_bottom
        self.max_scroll = max(0.0, self.content - self.view_h)
        self.scroll = min(max(scroll, 0.0), self.max_scroll)
        # The bar along the top: the search field, Open and Import; along the bottom: the startup
        # toggle and Start Sculpting. Each (x, y, w, h).
        bar_y, bar_h, button_w = self.y + self.height - 94 * s, 30 * s, 92 * s
        right = self.x + self.width - self.pad
        self.buttons = {
            'IMPORT': (right - button_w, bar_y, button_w, bar_h),
            'OPEN': (right - 2 * button_w - 8 * s, bar_y, button_w, bar_h),
            'SEARCH': (self.x + self.pad, bar_y, right - 2 * button_w - 16 * s - self.x - self.pad, bar_h),
            'STARTUP': (self.x + self.pad, self.y + 14 * s, 150 * s, 28 * s),
            'ACCEPT': (right - 150 * s, self.y + 12 * s, 150 * s, 32 * s),
        }
        # Tiles: [(key, x, y)], y being the tile's bottom; sections: [(label, y)]. The content's top
        # (``content_top``) moves with the scroll; tile animation is kept relative to it.
        self.tiles, self.sections = [], []
        top = self.view_top + self.scroll
        self.content_top = top
        for label, items in sections:
            top -= self.section
            self.sections.append((label, top + 8 * s))
            for i, item in enumerate(items):
                row, col = divmod(i, COLUMNS)
                self.tiles.append((item[0], self.x + self.pad + col * (self.tile + self.gap),
                                   top - (row + 1) * row_h + self.gap))
            top -= -(-len(items) // COLUMNS) * row_h

    def inside(self, mx, my):
        return self.x <= mx <= self.x + self.width and self.y <= my <= self.y + self.height

    def button_at(self, mx, my):
        for name, (bx, by, bw, bh) in self.buttons.items():
            if bx <= mx <= bx + bw and by <= my <= by + bh:
                return name
        return None

    def tile_at(self, mx, my):
        if not (self.view_bottom <= my <= self.view_top):
            return None
        for key, tx, ty in self.tiles:
            if tx <= mx <= tx + self.tile and ty <= my <= ty + self.thumb + self.label:
                return key
        return None


def _animate(layout):
    """Move the tiles' drawn positions and opacity a step towards where the search puts them.
    Tiles that no longer match fade out where they are. Returns True while anything is moving."""
    anim = _picker["anim"]
    targets = {key: (x - layout.x, y - layout.content_top) for key, x, y in layout.tiles}
    moving = False
    # Tiles leaving fade out where they are first; then the rest move up into the gaps.
    waiting = any(state[2] > 0.3 for key, state in anim.items() if key not in targets)
    for key, (tx, ty) in targets.items():
        state = anim.get(key)
        if state is None:
            # New on screen (or back from a search): appear where it belongs.
            anim[key] = state = [tx, ty, 0.0]
        x, y, alpha = state
        ease = 0.0 if waiting and alpha >= 1.0 else TILE_EASE
        nx, ny = x + (tx - x) * ease, y + (ty - y) * ease
        nalpha = min(1.0, alpha + TILE_FADE)
        if abs(tx - nx) < 0.5 and abs(ty - ny) < 0.5:
            nx, ny = tx, ty
        moving |= (nx, ny, nalpha) != (x, y, alpha)
        state[:] = [nx, ny, nalpha]
    for key in list(anim):
        if key not in targets:
            anim[key][2] -= TILE_FADE_OUT
            moving = True
            if anim[key][2] <= 0.0:
                del anim[key]
    return moving


class PHANTOM_OT_start_picker(Operator):
    """Choose a base mesh to start sculpting: a quad sphere or one of Blender's human base meshes"""
    bl_idname = "phantom.start_picker"
    bl_label = "Base Mesh"

    at_startup: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, _event):
        area = context.area
        if area is None or area.type != 'VIEW_3D':
            area, _region = _view3d(context.window)
        if area is None or _picker["open"]:
            return {'CANCELLED'}
        self.area = area
        self.region = next(r for r in area.regions if r.type == 'WINDOW')
        self.last_press = (None, 0.0)
        _load_pixels()
        _picker.update(open=True, region=self.region.as_pointer(), hover=None, selected='QUAD_SPHERE',
                       scroll=0.0, target=0.0, opened=time.perf_counter(), query="", button=None,
                       at_startup=self.at_startup, anim={})
        # Everything starts where it belongs, fully shown.
        layout = _Layout(self.region, 0.0)
        _picker["anim"] = {key: [x - layout.x, y - layout.content_top, 1.0] for key, x, y in layout.tiles}
        self.handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_PIXEL')
        self.timer = context.window_manager.event_timer_add(1.0 / 60.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        _play("menu_open")
        area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _close(self, context):
        bpy.types.SpaceView3D.draw_handler_remove(self.handle, 'WINDOW')
        context.window_manager.event_timer_remove(self.timer)
        _picker["open"] = False
        self.area.tag_redraw()

    def _start(self, context, key):
        if key is None:
            _play("error")
            return {'RUNNING_MODAL'}
        self._close(context)
        window = context.window
        sculpting = bpy.data.workspaces.get("Sculpting")
        if _picker["at_startup"] and window.workspace.object_mode != 'SCULPT' and sculpting is not None:
            # Opened at startup: sculpt in the Sculpting workspace, once it's shown.
            _picker["quiet_until"] = time.perf_counter() + 2.0
            window.workspace = sculpting

            def later():
                area, region = _view3d(window)
                if area is not None:
                    with bpy.context.temp_override(window=window, area=area, region=region):
                        bpy.ops.phantom.start_asset('EXEC_DEFAULT', True, choice=key)
                return None
            bpy.app.timers.register(later, first_interval=0.05)
            return {'FINISHED'}
        # `True`: push the operator's undo step (Python operator calls skip undo by default).
        # Without it, undoing the first stroke went back to before the mesh existed and crashed.
        with context.temp_override(area=self.area, region=self.region):
            bpy.ops.phantom.start_asset('EXEC_DEFAULT', True, choice=key)
        return {'FINISHED'}

    def _open(self, context):
        self._close(context)
        with context.temp_override(window=context.window, area=self.area, region=self.region):
            bpy.ops.wm.open_mainfile('INVOKE_DEFAULT')
        return {'FINISHED'}

    def _import(self, context):
        self._close(context)
        # Sculpt whatever mesh the import brings in (see _watch_import).
        _import.update(before={ob.name for ob in context.scene.objects}, deadline=time.perf_counter() + IMPORT_WAIT)
        if not bpy.app.timers.is_registered(_watch_import):
            bpy.app.timers.register(_watch_import, first_interval=0.5)
        with context.temp_override(window=context.window, area=self.area, region=self.region):
            bpy.ops.wm.call_menu(name="TOPBAR_MT_file_import")
        return {'FINISHED'}

    def _set_query(self, query):
        if query == _picker["query"]:
            return
        _picker.update(query=query, scroll=0.0, target=0.0)
        shown = [item[0] for _label, items in _sections() for item in items]
        if _picker["selected"] not in shown:
            _picker["selected"] = shown[0] if shown else None
        self.area.tag_redraw()

    def modal(self, context, event):
        region = self.region
        mx, my = event.mouse_x - region.x, event.mouse_y - region.y
        layout = _Layout(region, _picker["scroll"])
        over_area = (self.area.x <= event.mouse_x < self.area.x + self.area.width and
                     self.area.y <= event.mouse_y < self.area.y + self.area.height)

        if event.type == 'TIMER':
            # Ease the scroll towards where the wheel sent it, and keep the fade-in going.
            target = min(max(_picker["target"], 0.0), layout.max_scroll)
            _picker["target"] = target
            delta = target - _picker["scroll"]
            moving = _animate(layout)
            if abs(delta) > 0.5 or time.perf_counter() - _picker["opened"] < FADE_TIME + 0.05:
                _picker["scroll"] += delta * 0.35
                moving = True
            if moving:
                self.area.tag_redraw()
            return {'PASS_THROUGH'}
        if event.type == 'ESC' and event.value == 'PRESS' and _picker["query"]:
            # Esc clears the search first, then closes.
            self._set_query("")
            return {'RUNNING_MODAL'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            _play("cancel")
            self._close(context)
            return {'CANCELLED'}
        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            return self._start(context, _picker["selected"])
        # Typing goes into the search field.
        if event.value == 'PRESS' and event.type == 'BACK_SPACE':
            self._set_query("" if event.ctrl else _picker["query"][:-1])
            return {'RUNNING_MODAL'}
        if (event.value == 'PRESS' and event.unicode and event.unicode.isprintable()
                and not (event.ctrl or event.alt or event.oskey)):
            self._set_query((_picker["query"] + event.unicode).lstrip())
            return {'RUNNING_MODAL'}
        if not over_area:
            return {'PASS_THROUGH'}
        if event.type == 'MOUSEMOVE':
            hover = layout.tile_at(mx, my)
            button = layout.button_at(mx, my)
            if hover != _picker["hover"] or button != _picker["button"]:
                _picker.update(hover=hover, button=button)
                self.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            step = (layout.thumb + layout.label) * 0.6
            _picker["target"] += -step if event.type == 'WHEELUPMOUSE' else step
            return {'RUNNING_MODAL'}
        if event.type == 'TRACKPADPAN':
            _picker["target"] += event.mouse_y - event.mouse_prev_y
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'DOUBLE_CLICK':
            key = layout.tile_at(mx, my)
            return self._start(context, key) if key else {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if not layout.inside(mx, my):
                _play("cancel")
                self._close(context)
                return {'CANCELLED'}
            button = layout.button_at(mx, my)
            if button == 'ACCEPT':
                return self._start(context, _picker["selected"])
            if button == 'OPEN':
                return self._open(context)
            if button == 'IMPORT':
                return self._import(context)
            if button == 'STARTUP':
                set_show_at_startup(not show_at_startup())
                _play("toggle_on" if show_at_startup() else "toggle_off")
                self.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if button == 'SEARCH':
                return {'RUNNING_MODAL'}
            key = layout.tile_at(mx, my)
            now = time.perf_counter()
            # A second press on the same tile in time is a double-click too (Blender only reports
            # DOUBLE_CLICK for some presses).
            double_time = context.preferences.inputs.mouse_double_click_time / 1000.0
            if key and key == self.last_press[0] and now - self.last_press[1] < double_time:
                return self._start(context, key)
            self.last_press = (key, now)
            if key and key != _picker["selected"]:
                _picker["selected"] = key
                _play("click")
                self.area.tag_redraw()
            return {'RUNNING_MODAL'}
        # Keep clicks and keys from reaching the view behind the picker.
        return {'RUNNING_MODAL'}


def _draw():
    region = bpy.context.region
    if not _picker["open"] or region is None or region.as_pointer() != _picker["region"]:
        return
    layout = _Layout(region, _picker["scroll"])
    s = layout.s
    fade = min(1.0, (time.perf_counter() - _picker["opened"]) / FADE_TIME)
    ease = 1.0 - (1.0 - fade) ** 3
    gpu.state.blend_set('ALPHA')

    # Dim the view; the panel rises slightly into place as it fades in.
    _rounded_rect(0, 0, region.width, region.height, 0, (0.0, 0.0, 0.0, 0.35 * ease))
    rise = (1.0 - ease) * 12 * s
    x, y, w, h = layout.x, layout.y - rise, layout.width, layout.height
    _rounded_rect(x - s, y - s, w + 2 * s, h + 2 * s, 12 * s, (0.0, 0.0, 0.0, 0.45 * ease))
    _rounded_rect(x, y, w, h, 11 * s, (0.125, 0.125, 0.145, 0.98 * ease))
    _text(x + w / 2.0, y + h - 36 * s, "What would you like to sculpt?", 17 * s, (1.0, 1.0, 1.0, ease))

    def button(name, label, primary=False, enabled=True):
        bx, by, bw, bh = layout.buttons[name]
        by -= rise
        hovered = _picker["button"] == name and enabled
        if primary:
            color = (0.56, 0.54, 1.0, ease) if hovered else (0.48, 0.46, 1.0, ease if enabled else 0.35 * ease)
        else:
            color = (0.3, 0.3, 0.35, ease) if hovered else (0.22, 0.22, 0.26, ease)
        _rounded_rect(bx, by, bw, bh, 6 * s, color)
        _text(bx + bw / 2.0, by + bh / 2.0 - 4 * s, label, 12 * s, (1.0, 1.0, 1.0, ease if enabled else 0.5 * ease))

    # Search field: what's typed, or a hint.
    sx, sy, sw, sh = layout.buttons['SEARCH']
    sy -= rise
    _rounded_rect(sx, sy, sw, sh, 6 * s, (0.09, 0.09, 0.11, ease))
    query = _picker["query"]
    if query:
        _text(sx + 12 * s, sy + sh / 2.0 - 4 * s, query, 12 * s, (1.0, 1.0, 1.0, ease), align='LEFT')
        blf.size(0, 12 * s)
        caret_x = sx + 12 * s + blf.dimensions(0, query)[0] + 2 * s
        if int(time.perf_counter() * 2.0) % 2 == 0:
            _rounded_rect(caret_x, sy + 7 * s, 1.5 * s, sh - 14 * s, 0, (1.0, 1.0, 1.0, 0.8 * ease))
    else:
        _text(sx + 12 * s, sy + sh / 2.0 - 4 * s, "Search: just type", 12 * s, (0.55, 0.55, 0.6, ease), align='LEFT')
    button('OPEN', "Open...")
    button('IMPORT', "Import...")

    # Bottom: the startup toggle on the left, Start Sculpting on the right.
    tx_, ty_, tw_, th_ = layout.buttons['STARTUP']
    ty_ -= rise
    on = show_at_startup()
    box = 16 * s
    bx_, by_ = tx_, ty_ + (th_ - box) / 2.0
    _rounded_rect(bx_, by_, box, box, 4 * s, (0.48, 0.46, 1.0, ease) if on else
                  ((0.32, 0.32, 0.37, ease) if _picker["button"] == 'STARTUP' else (0.22, 0.22, 0.26, ease)))
    if on:
        _text(bx_ + box / 2.0, by_ + 3 * s, "\u2713", 13 * s, (1.0, 1.0, 1.0, ease))
    _text(bx_ + box + 8 * s, ty_ + th_ / 2.0 - 4 * s, "Show at startup", 12 * s, (0.85, 0.85, 0.9, ease), align='LEFT')
    button('ACCEPT', "Start Sculpting", primary=True, enabled=_picker["selected"] is not None)

    # The scrolling part, clipped between the bars.
    gpu.state.scissor_test_set(True)
    gpu.state.scissor_set(int(x), int(layout.view_bottom), int(w), max(1, int(layout.view_h)))
    if not layout.tiles and not _picker["anim"]:
        _text(x + w / 2.0, layout.view_top - 50 * s, "Nothing called \"%s\"" % query, 12 * s, (0.7, 0.7, 0.75, ease))
    for label, ly in layout.sections:
        _text(x + layout.pad, ly - rise, label, 12 * s, (0.75, 0.75, 0.8, ease), align='LEFT')
    tile_h = layout.thumb + layout.label
    shown = {key for key, _x, _y in layout.tiles}
    # Leaving tiles underneath the ones staying.
    for key, (ax, ay, alpha) in sorted(_picker["anim"].items(), key=lambda item: item[0] in shown):
        tx, ty = layout.x + ax, layout.content_top + ay - rise
        a = ease * max(0.0, min(1.0, alpha))
        if a <= 0.0 or ty > layout.view_top or ty + tile_h < layout.view_bottom:
            continue
        hovered, selected = key == _picker["hover"], key == _picker["selected"]
        if selected:
            _rounded_rect(tx - 2 * s, ty - 2 * s, layout.tile + 4 * s, tile_h + 4 * s, 9 * s, (0.48, 0.46, 1.0, a))
        _rounded_rect(tx, ty, layout.tile, tile_h, 8 * s,
                      (0.24, 0.24, 0.28, a) if hovered else (0.17, 0.17, 0.2, a))
        texture = _texture(key)
        if texture is not None:
            inset = (layout.tile - layout.thumb) / 2.0
            _image(texture, tx + inset, ty + layout.label, layout.thumb, layout.thumb, a)
        _text(tx + layout.tile / 2.0, ty + 7 * s, ALL[key][1], 11 * s,
              (1.0, 1.0, 1.0, a) if hovered or selected else (0.82, 0.82, 0.86, a))
    gpu.state.scissor_test_set(False)

    if layout.max_scroll > 0.0:
        track_h = layout.view_h - 16 * s
        bar_h = max(30 * s, track_h * layout.view_h / layout.content)
        bar_y = layout.view_bottom - rise + 8 * s + (track_h - bar_h) * (1.0 - layout.scroll / layout.max_scroll)
        _rounded_rect(x + w - 9 * s, bar_y, 4 * s, bar_h, 2 * s, (1.0, 1.0, 1.0, 0.25 * ease))
    gpu.state.blend_set('NONE')


def draw_add_menu(self, context):
    if context.mode == 'OBJECT':
        self.layout.separator()
        self.layout.operator_context = 'INVOKE_DEFAULT'
        self.layout.operator(PHANTOM_OT_start_picker.bl_idname, text="Base Mesh...", icon='OUTLINER_OB_ARMATURE')


def _watch():
    """Offer the picker when a window switches to a sculpting workspace with an empty canvas."""
    wm = bpy.context.window_manager
    for window in wm.windows if wm else ():
        workspace = window.workspace
        key = window.as_pointer()
        previous = _last_workspace.get(key)
        _last_workspace[key] = workspace.name if workspace else None
        if workspace is None or previous is None or previous == workspace.name:
            continue
        if workspace.object_mode != 'SCULPT' or not canvas_is_empty(window.scene):
            continue
        if time.perf_counter() < _picker["quiet_until"]:
            # The picker itself switched workspaces to sculpt what was picked.
            continue
        area, region = _view3d(window)
        if area is None:
            continue
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.phantom.start_picker('INVOKE_DEFAULT')
    return WATCH_INTERVAL


def _watch_import():
    """After Import... in the picker: sculpt the mesh the import brought in."""
    if _import["before"] is None or time.perf_counter() > _import["deadline"]:
        _import["before"] = None
        return None
    context = bpy.context
    window = context.window or (context.window_manager.windows[0] if context.window_manager.windows else None)
    if window is None:
        return 0.5
    new = [ob for ob in window.scene.objects if ob.name not in _import["before"] and ob.type == 'MESH']
    if not new:
        return 0.5
    _import["before"] = None
    target = max(new, key=lambda ob: len(ob.data.vertices))
    area, region = _view3d(window)
    if area is None:
        return None
    with context.temp_override(window=window, area=area, region=region):
        if context.object is not None and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for ob in window.view_layer.objects:
            if ob is not None and ob.select_get():
                ob.select_set(False)
        _frame_and_sculpt(bpy.context, target, new)
    _play("mode_switch")
    return None


def _open_at_startup():
    wm = bpy.context.window_manager
    window = wm.windows[0] if wm and wm.windows else None
    if window is None or _picker["open"] or not canvas_is_empty(window.scene):
        return None
    area, region = _view3d(window)
    if area is not None:
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.phantom.start_picker('INVOKE_DEFAULT', at_startup=True)
    return None


@persistent
def _on_load(*_args):
    # Phantom starting, or File > New: offer the picker if that's what the user chose.
    if bpy.app.background or bpy.data.filepath or not show_at_startup():
        return
    bpy.app.timers.register(_open_at_startup, first_interval=0.4)


classes = (
    PHANTOM_OT_start_asset,
    PHANTOM_OT_start_picker,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_MT_add.append(draw_add_menu)
    if not bpy.app.background:
        bpy.app.timers.register(_watch, first_interval=WATCH_INTERVAL, persistent=True)
    bpy.app.handlers.load_post.append(_on_load)


def unregister():
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    bpy.types.VIEW3D_MT_add.remove(draw_add_menu)
    _thumbs["textures"].clear()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
