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

Click a thumbnail to select it and double-click (or press Enter) to start: the mesh is appended,
framed from the front and put in Sculpt Mode. Scroll for more; Esc, right-click or a click
outside closes it. The picker is also in the 3D View's Add menu ("Base Mesh...").
"""

import math
import os
import time

import blf
import bpy
import gpu
import numpy as np
from bpy.props import EnumProperty
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
           "opened": 0.0}
# The workspace each window showed at the last check, to notice switches.
_last_workspace = {}


def _datafile(*parts):
    # ``system_resource`` only finds folders, so join the file onto the datafiles folder.
    return os.path.join(bpy.utils.system_resource('DATAFILES'), *parts)


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
    return target


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
        self.header = 64 * s
        self.section = 30 * s
        self.width = COLUMNS * self.tile + (COLUMNS - 1) * self.gap + 2 * self.pad
        row_h = self.thumb + self.label + self.gap
        rows = sum(-(-len(items) // COLUMNS) for items in (STARTERS, MORE))
        self.content = 2 * self.section + rows * row_h + self.pad
        self.height = min(self.header + self.content, region.height * 0.8)
        self.x = (region.width - self.width) / 2.0
        self.y = (region.height - self.height) / 2.0
        self.view_top = self.y + self.height - self.header
        self.view_h = self.height - self.header
        self.max_scroll = max(0.0, self.content - self.view_h)
        self.scroll = min(max(scroll, 0.0), self.max_scroll)
        # Tiles: [(key, x, y)], y being the tile's bottom; sections: [(label, y)].
        self.tiles, self.sections = [], []
        top = self.view_top + self.scroll
        for label, items in (("Start with", STARTERS), ("More base meshes", MORE)):
            top -= self.section
            self.sections.append((label, top + 8 * s))
            for i, item in enumerate(items):
                row, col = divmod(i, COLUMNS)
                self.tiles.append((item[0], self.x + self.pad + col * (self.tile + self.gap),
                                   top - (row + 1) * row_h + self.gap))
            top -= -(-len(items) // COLUMNS) * row_h

    def inside(self, mx, my):
        return self.x <= mx <= self.x + self.width and self.y <= my <= self.y + self.height

    def tile_at(self, mx, my):
        if not (self.y <= my <= self.view_top):
            return None
        for key, tx, ty in self.tiles:
            if tx <= mx <= tx + self.tile and ty <= my <= ty + self.thumb + self.label:
                return key
        return None


class PHANTOM_OT_start_picker(Operator):
    """Choose a base mesh to start sculpting: a quad sphere or one of Blender's human base meshes"""
    bl_idname = "phantom.start_picker"
    bl_label = "Base Mesh"

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
                       scroll=0.0, target=0.0, opened=time.perf_counter())
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
        self._close(context)
        with context.temp_override(area=self.area, region=self.region):
            bpy.ops.phantom.start_asset('EXEC_DEFAULT', choice=key)
        return {'FINISHED'}

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
            if abs(delta) > 0.5 or time.perf_counter() - _picker["opened"] < FADE_TIME + 0.05:
                _picker["scroll"] += delta * 0.35
                self.area.tag_redraw()
            return {'PASS_THROUGH'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            _play("cancel")
            self._close(context)
            return {'CANCELLED'}
        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            return self._start(context, _picker["selected"])
        if not over_area:
            return {'PASS_THROUGH'}
        if event.type == 'MOUSEMOVE':
            hover = layout.tile_at(mx, my)
            if hover != _picker["hover"]:
                _picker["hover"] = hover
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
    _text(x + w / 2.0, y + h - 32 * s, "What would you like to sculpt?", 17 * s, (1.0, 1.0, 1.0, ease))
    _text(x + w / 2.0, y + h - 52 * s, "Double-click to start", 11 * s, (0.7, 0.7, 0.75, ease))

    # The scrolling part, clipped to the panel below its title.
    gpu.state.scissor_test_set(True)
    gpu.state.scissor_set(int(x), int(y + 4 * s), int(w), max(1, int(layout.view_h - 4 * s)))
    for label, ly in layout.sections:
        _text(x + layout.pad, ly - rise, label, 12 * s, (0.75, 0.75, 0.8, ease), align='LEFT')
    tile_h = layout.thumb + layout.label
    for key, tx, ty in layout.tiles:
        ty -= rise
        if ty > layout.view_top or ty + tile_h < y:
            continue
        hovered, selected = key == _picker["hover"], key == _picker["selected"]
        if selected:
            _rounded_rect(tx - 2 * s, ty - 2 * s, layout.tile + 4 * s, tile_h + 4 * s, 9 * s, (0.48, 0.46, 1.0, ease))
        _rounded_rect(tx, ty, layout.tile, tile_h, 8 * s,
                      (0.24, 0.24, 0.28, ease) if hovered else (0.17, 0.17, 0.2, ease))
        texture = _texture(key)
        if texture is not None:
            inset = (layout.tile - layout.thumb) / 2.0
            _image(texture, tx + inset, ty + layout.label, layout.thumb, layout.thumb, ease)
        _text(tx + layout.tile / 2.0, ty + 7 * s, ALL[key][1], 11 * s,
              (1.0, 1.0, 1.0, ease) if hovered or selected else (0.82, 0.82, 0.86, ease))
    gpu.state.scissor_test_set(False)

    if layout.max_scroll > 0.0:
        track_h = layout.view_h - 16 * s
        bar_h = max(30 * s, track_h * layout.view_h / layout.content)
        bar_y = y + 8 * s + (track_h - bar_h) * (1.0 - layout.scroll / layout.max_scroll)
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
        area, region = _view3d(window)
        if area is None:
            continue
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.phantom.start_picker('INVOKE_DEFAULT')
    return WATCH_INTERVAL


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


def unregister():
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
    bpy.types.VIEW3D_MT_add.remove(draw_add_menu)
    _thumbs["textures"].clear()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
