# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: garment making in the style of Marvelous Designer (data and simulation).

A garment is made of flat *pattern pieces* drawn in the Garment workspace's pattern view, *seams*
that join part of one piece's outline to part of another's, and an *arrangement* that places each
piece around the avatar (in front of the chest, around an arm...). From these the simulation builds
one cloth mesh, the "Garment" object:

- each placed piece is wrapped around the avatar at its arrangement point;
- its flat shape is kept as the "Flat" shape key, which the cloth uses as its rest shape, so the
  fabric wants to be the size it was drawn;
- each seam becomes loose edges between the two stretches of outline, which Blender's cloth pulls
  together as sewing springs;
- each elastic segment shrinks to a ratio of its length (Blender's cloth shrinking, weighted by the
  "Elastic" vertex group), like a gathered waistband;
- pinned areas hold still ("Pins" vertex group);
- each piece has its own fabric: its stiffness is set through the cloth's stiffness vertex groups.

Make Sculptable bakes the current frame of the simulation into a closed mesh ready for Sculpt Mode.

Pattern pieces are mesh objects in the "Garment Patterns" collection, lying flat (Z up) away from the
avatar. A piece's shape is its outline (``points``: corners and curve points, see
``phantom_garment_pattern.py``); its mesh is generated from it. Segments run between corners and are
numbered from the first corner (for a rectangle: bottom, right, top, left). A seam joins a stretch of
one segment, from ``start`` to ``end`` (fractions of its length; ``start > end`` runs backwards), to
a stretch of another.

The workspace, tools and panels are in ``phantom_garment_ui.py``.
"""

import json
import math
import os

import bmesh
import bpy
import numpy as np
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, PropertyGroup
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

import phantom_garment_pattern as pattern

PATTERN_COLLECTION = "Garment Patterns"
GARMENT_COLLECTION = "Garment"
AVATAR_COLLECTION = "Avatar"
GARMENT_OBJECT = "Garment"
SCULPT_OBJECT = "Garment Sculpt"
BOUNDARY_ATTR = "garment_boundary"
CORNER_ATTR = "garment_corner"
FLAT_KEY = "Flat"
# Where new pattern pieces go: away from the avatar, so the two views don't see each other's objects.
BOARD_ORIGIN = Vector((10.0, 0.0, 0.0))
# Frames the simulation runs for; playback stops at the last one.
SIM_FRAMES = 500
# Space left between the avatar and the pieces it wraps.
ARRANGE_CLEARANCE = 0.05
# Frames the garment is sewn together without gravity, and frames gravity then takes to ease in.
SEW_FRAMES = 18
GRAVITY_EASE = 12
# How far elastic segments are from the fabric's tension towards the seams' (0-1).
ELASTIC_STIFFNESS = 0.3
# Cloth mass per vertex for cotton at the default particle distance (see ``build_garment``).
MASS_SCALE = 0.3
# How much stiffer than the stiffest fabric seams are.
SEAM_STIFFNESS = 25.0
RECT_SEGMENT_NAMES = ("Bottom", "Right", "Top", "Left")

ARRANGEMENT_ITEMS = (
    ('NONE', "Not Placed", "Leave the piece out of the 3D garment"),
    ('CHEST_FRONT', "Chest Front", "Wrap the piece around the front of the chest"),
    ('CHEST_BACK', "Chest Back", "Wrap the piece around the back of the chest"),
    ('HIP_FRONT', "Hip Front", "Wrap the piece around the front of the hips"),
    ('HIP_BACK', "Hip Back", "Wrap the piece around the back of the hips"),
    ('ARM_LEFT', "Left Arm", "Wrap the piece around the avatar's left arm"),
    ('ARM_RIGHT', "Right Arm", "Wrap the piece around the avatar's right arm"),
    ('LEG_LEFT_FRONT', "Left Leg Front", "Wrap the piece around the front of the left leg"),
    ('LEG_LEFT_BACK', "Left Leg Back", "Wrap the piece around the back of the left leg"),
    ('LEG_RIGHT_FRONT', "Right Leg Front", "Wrap the piece around the front of the right leg"),
    ('LEG_RIGHT_BACK', "Right Leg Back", "Wrap the piece around the back of the right leg"),
)

# Physical properties for Blender's cloth (stiffness after its own presets; density in kg/m2),
# plus a display color.
FABRICS = {
    'COTTON': dict(
        name="Cotton", color=(0.42, 0.72, 0.86, 1.0), quality=8, density=0.15,
        tension=15.0, shear=15.0, bending=0.5, damping=5.0,
    ),
    'SILK': dict(
        name="Silk", color=(0.93, 0.62, 0.72, 1.0), quality=8, density=0.08,
        tension=5.0, shear=5.0, bending=0.05, damping=0.0,
    ),
    'JERSEY': dict(
        name="Jersey", color=(0.62, 0.84, 0.55, 1.0), quality=8, density=0.2,
        tension=4.0, shear=4.0, bending=0.2, damping=2.0,
    ),
    'DENIM': dict(
        name="Denim", color=(0.25, 0.37, 0.62, 1.0), quality=12, density=0.4,
        tension=40.0, shear=40.0, bending=10.0, damping=25.0,
    ),
    'LEATHER': dict(
        name="Leather", color=(0.42, 0.26, 0.17, 1.0), quality=15, density=0.6,
        tension=80.0, shear=80.0, bending=150.0, damping=25.0,
    ),
}
FABRIC_ITEMS = tuple((key, value["name"], "Make it " + value["name"].lower())
                     for key, value in FABRICS.items())

_segment_cache = {}
_avatar_cache = {}
# Signature of what the Garment object was last built from (see ``garment_signature``).
_built = {"signature": None}


# -----------------------------------------------------------------------------
# Sounds

_audio = {"device": None}


def play_sound(name, pitch=1.0):
    """Play one of Phantom's interface sounds (``datafiles/sounds/<name>.wav``), if enabled."""
    if bpy.app.background:
        return
    view = bpy.context.preferences.view
    if not getattr(view, "use_ui_sounds", False) or view.ui_sound_volume <= 0:
        return
    # ``system_resource`` only finds folders, so join the file onto the sounds folder.
    path = os.path.join(bpy.utils.system_resource('DATAFILES', path="sounds"), name + ".wav")
    if not os.path.exists(path):
        return
    try:
        import aud
        if _audio["device"] is None:
            _audio["device"] = aud.Device()
        sound = aud.Sound(path).volume(view.ui_sound_volume / 100.0)
        if pitch != 1.0:
            sound = sound.pitch(pitch)
        _audio["device"].play(sound)
    except Exception as ex:
        print("Phantom: could not play sound:", ex)


# -----------------------------------------------------------------------------
# Properties

def _request_rebuild(_self=None, _context=None):
    _built["signature"] = None


def _piece_fabric_update(self, _context):
    ob = self.id_data
    if isinstance(ob, bpy.types.Object) and ob.type == 'MESH' and self.is_piece:
        _assign_material(ob.data, fabric_material(self.fabric))
    _request_rebuild()


def _particle_distance_update(_self, context):
    for ob in pattern_pieces(context.scene):
        regenerate_piece(ob, context.scene)
    _request_rebuild()


def _avatar_poll(_self, ob):
    return ob.type == 'MESH' and not ob.phantom_garment.is_piece and ob.name != GARMENT_OBJECT


class PhantomGarmentPoint(PropertyGroup):
    co: FloatVectorProperty(size=2, unit='LENGTH')
    curve: BoolProperty(description="A curve point (the outline bends smoothly through it), not a corner")
    # A curve point's own handle (its Bezier arm towards the next point), when it has one.
    handle: FloatVectorProperty(size=2)
    has_handle: BoolProperty()


class PhantomGarmentPin(PropertyGroup):
    # A pinned rectangle, in the piece's own coordinates.
    lo: FloatVectorProperty(size=2)
    hi: FloatVectorProperty(size=2)


class PhantomGarmentPiece(PropertyGroup):
    is_piece: BoolProperty(name="Pattern Piece", default=False)
    points: CollectionProperty(type=PhantomGarmentPoint)
    pins: CollectionProperty(type=PhantomGarmentPin)
    offset: FloatVectorProperty(
        name="Offset", size=2, unit='LENGTH', update=_request_rebuild,
        description="How far the piece is moved from its arrangement point: around the body, and up")
    arrangement: EnumProperty(
        name="Arrangement", description="Where the piece starts on the avatar",
        items=ARRANGEMENT_ITEMS, default='NONE', update=_request_rebuild,
    )
    fabric: EnumProperty(
        name="Fabric", description="How this piece's fabric stretches, bends and weighs",
        items=FABRIC_ITEMS, default='COTTON', update=_piece_fabric_update,
    )
    # The first Garment pieces were rectangles described by these (see ``maintain``).
    width: FloatProperty(default=0.0, options={'HIDDEN'})
    height: FloatProperty(default=0.0, options={'HIDDEN'})


class PhantomGarmentSeam(PropertyGroup):
    piece_a: PointerProperty(type=bpy.types.Object)
    segment_a: IntProperty(min=0)
    start_a: FloatProperty(default=0.0)
    end_a: FloatProperty(default=1.0)
    piece_b: PointerProperty(type=bpy.types.Object)
    segment_b: IntProperty(min=0)
    start_b: FloatProperty(default=0.0)
    end_b: FloatProperty(default=1.0)
    # The first Garment seams were whole segments, possibly reversed (see ``maintain``).
    reverse_a: BoolProperty(options={'HIDDEN'})
    reverse_b: BoolProperty(options={'HIDDEN'})
    ranged: BoolProperty(default=False, options={'HIDDEN'})


class PhantomGarmentElastic(PropertyGroup):
    piece: PointerProperty(type=bpy.types.Object)
    segment: IntProperty(min=0)
    ratio: FloatProperty(
        name="Ratio", description="Length the segment pulls in to, as a share of its drawn length",
        default=0.6, min=0.2, max=1.0, subtype='FACTOR', update=_request_rebuild,
    )


class PhantomGarmentSettings(PropertyGroup):
    avatar: PointerProperty(
        name="Avatar", description="The body the garment is draped on",
        type=bpy.types.Object, poll=_avatar_poll, update=_request_rebuild,
    )
    fabric: EnumProperty(
        name="New Pieces", description="Fabric for newly drawn pieces",
        items=FABRIC_ITEMS, default='COTTON',
    )
    particle_distance: FloatProperty(
        name="Particle Distance",
        description="Average distance between the points of the fabric: larger is faster, smaller is finer",
        default=0.02, min=0.005, max=0.1, unit='LENGTH', update=_particle_distance_update,
    )
    skin_offset: FloatProperty(
        name="Skin Offset", description="Invisible gap kept between the avatar and the fabric",
        default=0.006, min=0.0, max=0.05, unit='LENGTH', update=_request_rebuild,
    )
    sewing_force: FloatProperty(
        name="Sewing Force",
        description="How hard seams may pull together: too weak and pieces fall before they are sewn, "
                    "too strong and they jump (0 for no limit)",
        default=20.0, min=0.0, max=1000.0, update=_request_rebuild,
    )
    solver: EnumProperty(
        name="Solver", description="What simulates the cloth",
        items=(
            ('GPU', "GPU (Newton)", "NVIDIA Newton on the graphics card: live, and you can drag the cloth while "
             "it simulates (needs an NVIDIA GPU and the GPU cloth add-on)"),
            ('BLENDER', "Blender Cloth", "Blender's cloth simulation, played on the timeline (works everywhere)"),
        ),
        default='GPU', update=_request_rebuild,
    )
    seams: CollectionProperty(type=PhantomGarmentSeam)
    elastics: CollectionProperty(type=PhantomGarmentElastic)


# -----------------------------------------------------------------------------
# Pattern pieces

def pattern_pieces(scene):
    return [ob for ob in scene.objects if ob.type == 'MESH' and ob.phantom_garment.is_piece]


def ensure_collection(scene, name):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
    if coll not in scene.collection.children_recursive:
        scene.collection.children.link(coll)
    return coll


def fabric_material(fabric):
    info = FABRICS[fabric]
    name = "Garment " + info["name"]
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_backface_culling = False
        mat.roughness = 0.9
        mat.diffuse_color = info["color"]
    return mat


def _assign_material(mesh, mat):
    if len(mesh.materials):
        if mesh.materials[0] != mat:
            mesh.materials[0] = mat
    else:
        mesh.materials.append(mat)


def piece_points(ob):
    """The piece's outline as [(x, y, curve)] in its own coordinates."""
    return [(p.co[0], p.co[1], (p.handle[0], p.handle[1]) if p.curve and p.has_handle else p.curve)
            for p in ob.phantom_garment.points]


def parse_curve(curve):
    """A point's curve flag or handle, from JSON."""
    if isinstance(curve, (list, tuple)):
        return (float(curve[0]), float(curve[1]))
    return bool(curve)


def set_piece_points(ob, points):
    coll = ob.phantom_garment.points
    while len(coll) > len(points):
        coll.remove(len(coll) - 1)
    while len(coll) < len(points):
        coll.add()
    for item, (x, y, curve) in zip(coll, points):
        item.co = (x, y)
        handle = pattern.handle_of(curve)
        item.curve = bool(curve)
        item.has_handle = handle is not None
        if handle is not None:
            item.handle = handle


def regenerate_piece(ob, scene):
    """Rebuild the piece's mesh from its outline."""
    verts, faces, order, corner = pattern.build_piece(piece_points(ob), scene.phantom_garment.particle_distance)
    mesh = ob.data
    mesh.clear_geometry()
    if len(verts):
        mesh.from_pydata([(x, y, 0.0) for x, y in verts], [], faces)
        boundary = mesh.attributes.get(BOUNDARY_ATTR) or mesh.attributes.new(BOUNDARY_ATTR, 'INT', 'POINT')
        boundary.data.foreach_set("value", order.tolist())
        corners = mesh.attributes.get(CORNER_ATTR) or mesh.attributes.new(CORNER_ATTR, 'BOOLEAN', 'POINT')
        corners.data.foreach_set("value", corner.tolist())
    mesh.update()
    _segment_cache.clear()
    _request_rebuild()


def add_piece(context, world_points, name="Pattern"):
    """Add a pattern piece from an outline in board (world) coordinates: [(x, y, curve)]."""
    scene = context.scene
    world_points = pattern.counter_clockwise(world_points)
    xy = np.array([(p[0], p[1]) for p in world_points])
    center = 0.5 * (xy.min(axis=0) + xy.max(axis=0))
    coll = ensure_collection(scene, PATTERN_COLLECTION)
    count = len(pattern_pieces(scene)) + 1
    mesh = bpy.data.meshes.new("%s %d" % (name, count))
    ob = bpy.data.objects.new("%s %d" % (name, count), mesh)
    props = ob.phantom_garment
    props.is_piece = True
    props.fabric = scene.phantom_garment.fabric
    set_piece_points(ob, [(p[0] - center[0], p[1] - center[1], p[2]) for p in world_points])
    ob.location = (center[0], center[1], 0.0)
    regenerate_piece(ob, scene)
    _assign_material(mesh, fabric_material(props.fabric))
    coll.objects.link(ob)
    for other in context.view_layer.objects:
        if other is not None and other.select_get():
            other.select_set(False)
    ob.select_set(True)
    context.view_layer.objects.active = ob
    return ob


def piece_segments(ob):
    """The piece's outline as lists of vertex indices, one per segment, in outline order.

    Each list runs from one corner to the next and includes both corners.
    """
    mesh = ob.data
    key = (mesh.as_pointer(), len(mesh.vertices), len(mesh.polygons))
    cached = _segment_cache.get(key)
    if cached is not None:
        return cached
    boundary = mesh.attributes.get(BOUNDARY_ATTR)
    corner_attr = mesh.attributes.get(CORNER_ATTR)
    if boundary is None or corner_attr is None:
        return []
    order = np.zeros(len(mesh.vertices), dtype=np.int64)
    boundary.data.foreach_get("value", order)
    flags = np.zeros(len(mesh.vertices), dtype=bool)
    corner_attr.data.foreach_get("value", flags)
    loop = [int(i) for i in np.argsort(order) if order[i] >= 0]
    if not loop:
        return []
    cut = [i for i, v in enumerate(loop) if flags[v]] or [0]
    segments = []
    for k, i in enumerate(cut):
        j = cut[k + 1] if k + 1 < len(cut) else len(loop)
        segments.append(loop[i:j] + [loop[j % len(loop)]])
    _segment_cache[key] = segments
    return segments


def segment_name(ob, index, short=False):
    points = piece_points(ob) if ob else []
    if len(points) == 4 and not any(p[2] for p in points) and index < 4:
        return RECT_SEGMENT_NAMES[index]
    return ("S%d" if short else "Segment %d") % (index + 1)


def piece_local_points(ob):
    mesh = ob.data
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


def to_world(ob, local):
    mat = np.array(ob.matrix_world)
    return local @ mat[:3, :3].T + mat[:3, 3]


def piece_world_points(ob):
    """World-space coordinates of the piece's vertices (N x 3)."""
    return to_world(ob, piece_local_points(ob))


def outline_world(ob):
    """The piece's smooth outline in world space, one polyline (N x 3) per segment."""
    out = []
    for poly in pattern.outline_segments(piece_points(ob)):
        out.append(to_world(ob, np.column_stack((poly, np.zeros(len(poly))))))
    return out


def side_vertices(ob, segment, start, end, spacing):
    """Vertex indices along a stretch of a segment, in sewing order (``start`` to ``end``)."""
    segments = piece_segments(ob)
    if segment >= len(segments):
        return []
    verts = segments[segment]
    co = piece_local_points(ob)[verts][:, :2]
    params = pattern.arc_params(co)
    length = pattern.polyline_length(co)
    lo, hi = min(start, end), max(start, end)
    tol = 0.5 * spacing / length if length > 0.0 else 0.0
    chosen = [v for v, t in zip(verts, params) if lo - tol <= t <= hi + tol]
    if len(chosen) < 2:
        chosen = [verts[int(np.abs(params - lo).argmin())], verts[int(np.abs(params - hi).argmin())]]
        if chosen[0] == chosen[1]:
            return []
    return chosen[::-1] if start > end else chosen


def pinned_vertices(ob):
    pins = ob.phantom_garment.pins
    if not len(pins):
        return np.zeros(len(ob.data.vertices), dtype=bool)
    co = piece_local_points(ob)
    mask = np.zeros(len(co), dtype=bool)
    for pin in pins:
        mask |= ((co[:, 0] >= pin.lo[0]) & (co[:, 0] <= pin.hi[0]) &
                 (co[:, 1] >= pin.lo[1]) & (co[:, 1] <= pin.hi[1]))
    return mask


def maintain(scene):
    """Housekeeping, run by the Garment workspace's timer.

    - Forget seams, elastics and the avatar whose objects were deleted. Our pointers count as users,
      so deleting a piece only unlinks it from the scene; dropping the pointers lets it go for good
      (and keeps the depsgraph from building objects outside the scene).
    - Bring pieces and seams from files made with the first Garment version up to date.

    Returns True if anything changed.
    """
    settings = scene.phantom_garment
    in_scene = set(scene.objects)
    changed = False
    if settings.avatar is not None and settings.avatar not in in_scene:
        settings.avatar = None
        changed = True
    for i in reversed(range(len(settings.seams))):
        seam = settings.seams[i]
        if seam.piece_a not in in_scene or seam.piece_b not in in_scene:
            settings.seams.remove(i)
            changed = True
        elif not seam.ranged:
            seam.start_a, seam.end_a = (1.0, 0.0) if seam.reverse_a else (0.0, 1.0)
            seam.start_b, seam.end_b = (1.0, 0.0) if seam.reverse_b else (0.0, 1.0)
            seam.ranged = True
            changed = True
    for i in reversed(range(len(settings.elastics))):
        if settings.elastics[i].piece not in in_scene:
            settings.elastics.remove(i)
            changed = True
    for ob in pattern_pieces(scene):
        props = ob.phantom_garment
        if not len(props.points) and props.width > 0.0:
            set_piece_points(ob, pattern.rectangle(props.width, props.height))
            regenerate_piece(ob, scene)
            _assign_material(ob.data, fabric_material(props.fabric))
            changed = True
    return changed


def valid_elastics(scene):
    pieces = set(pattern_pieces(scene))
    for elastic in scene.phantom_garment.elastics:
        if elastic.piece in pieces and elastic.segment < len(piece_segments(elastic.piece)):
            yield elastic


def valid_seams(scene):
    pieces = set(pattern_pieces(scene))
    for seam in scene.phantom_garment.seams:
        if seam.piece_a in pieces and seam.piece_b in pieces:
            if (seam.segment_a < len(piece_segments(seam.piece_a)) and
                    seam.segment_b < len(piece_segments(seam.piece_b))):
                yield seam


def seam_sides(seam):
    return ((seam.piece_a, seam.segment_a, seam.start_a, seam.end_a),
            (seam.piece_b, seam.segment_b, seam.start_b, seam.end_b))


def remap_after_reshape(scene, ob, old_points):
    """Keep seams and elastics on ``ob`` attached after its corners changed (a point was added,
    removed, or turned into a corner or a curve point), which renumbers its segments.

    Each seam end is placed on the new outline where it was on the old one.
    """
    old = pattern.outline_segments(old_points)
    new = pattern.outline_segments(piece_points(ob))
    if not old or not new:
        return

    def locate(point, only=None):
        best = None
        for k, poly in enumerate(new):
            if only is not None and k != only:
                continue
            dist, along = pattern.project(poly, point)
            if best is None or dist < best[0]:
                best = (dist, k, along)
        return best[1], best[2]

    def remap(segment, start, end):
        if segment >= len(old):
            return segment, start, end
        poly = old[segment]
        k, _ = locate(pattern.point_at(poly, 0.5 * (start + end)))
        _, s = locate(pattern.point_at(poly, start), k)
        _, e = locate(pattern.point_at(poly, end), k)
        return k, s, e

    settings = scene.phantom_garment
    for seam in settings.seams:
        if seam.piece_a == ob:
            seam.segment_a, seam.start_a, seam.end_a = remap(seam.segment_a, seam.start_a, seam.end_a)
        if seam.piece_b == ob:
            seam.segment_b, seam.start_b, seam.end_b = remap(seam.segment_b, seam.start_b, seam.end_b)
    for elastic in settings.elastics:
        if elastic.piece == ob:
            elastic.segment = remap(elastic.segment, 0.0, 1.0)[0]


# -----------------------------------------------------------------------------
# Arrangement

class _Slot:
    """Where a piece wraps: a cylinder of elliptic section around ``axis`` (the piece's up),
    with the piece's middle facing ``facing``. The piece's top edge goes ``top`` along the axis
    from ``center`` (so tops hang from the shoulders and skirts from the waist), or its middle
    goes at ``center`` if ``top`` is None."""

    def __init__(self, center, axis, facing, ra, rb, top=None):
        self.center = Vector(center)
        self.axis = Vector(axis).normalized()
        facing = Vector(facing)
        facing = (facing - self.axis * facing.dot(self.axis)).normalized()
        self.facing = facing
        self.ra = ra
        self.rb = rb
        self.top = top

    def point(self):
        return self.center + self.facing * (self.rb + ARRANGE_CLEARANCE)


def avatar_frame(avatar):
    """The avatar's arrangement points: {slot: _Slot}.

    The torso is measured at chest and hip height by casting rays out from its middle (so arms
    beside it don't count); arms are found reaching out sideways, legs below the hips.
    """
    mesh = avatar.data
    key = (avatar.name, tuple(tuple(r) for r in avatar.matrix_world), len(mesh.vertices))
    cached = _avatar_cache.get(key)
    if cached is not None:
        return cached
    co = piece_world_points(avatar)
    if not len(co):
        return {}
    tree = BVHTree.FromPolygons([tuple(p) for p in co], [tuple(p.vertices) for p in mesh.polygons])
    zmin, zmax = co[:, 2].min(), co[:, 2].max()
    height = zmax - zmin
    full_body = height > 1.0
    cx = 0.5 * (co[:, 0].min() + co[:, 0].max())
    up = Vector((0.0, 0.0, 1.0))
    front, back = Vector((0.0, -1.0, 0.0)), Vector((0.0, 1.0, 0.0))

    def reach(origin, direction, limit=1.0):
        hit = tree.ray_cast(Vector(origin), Vector(direction), limit)
        return hit[3] if hit[0] is not None else 0.0

    frame = {}
    torso = {}
    # Where the pieces' top edges go on a full body: the shoulders and the waist.
    tops = {"CHEST": zmin + 0.83 * height, "HIP": zmin + 0.645 * height}
    for name, (body, part) in (("CHEST", (0.72, 0.65)), ("HIP", (0.555, 0.35))):
        zc = zmin + height * (body if full_body else part)
        band = co[np.abs(co[:, 2] - zc) < max(0.03, 0.03 * height)]
        if len(band) < 4:
            band = co
        cy = 0.5 * (band[:, 1].min() + band[:, 1].max())
        center = Vector((cx, cy, zc))
        half_width = max(reach(center, (1, 0, 0)), reach(center, (-1, 0, 0))) or float(np.abs(band[:, 0] - cx).max())
        half_depth = max(reach(center, (0, 1, 0)), reach(center, (0, -1, 0))) or float(np.abs(band[:, 1] - cy).max())
        torso[name] = (center, half_width, half_depth)
        top = tops[name] - zc if full_body else None
        frame[name + "_FRONT"] = _Slot(center, up, front, half_width, half_depth, top)
        frame[name + "_BACK"] = _Slot(center, up, back, half_width, half_depth, top)

    if full_body:
        chest_center, chest_width, _depth = torso["CHEST"]
        hip_center, hip_width, _depth = torso["HIP"]
        for side_name, side in (("LEFT", 1.0), ("RIGHT", -1.0)):
            # Arm: the vertices reaching out beyond the chest, above the legs.
            out = side * (co[:, 0] - cx)
            arm = co[(out > chest_width * 1.15) & (co[:, 2] > zmin + 0.4 * height)]
            if len(arm) >= 12:
                arm_out = side * (arm[:, 0] - cx)
                hand = arm[arm_out >= arm_out.max() - 0.06].mean(axis=0)
                root = arm[arm_out <= arm_out.min() + 0.05].mean(axis=0)
                shoulder = Vector((cx + side * chest_width, root[1], max(root[2], chest_center.z)))
                axis = shoulder - Vector(hand)
                middle = shoulder.lerp(Vector(hand), 0.45)
                # Half the width across, in a few directions: unlike the distance from the
                # middle, it doesn't depend on the middle being the centre of the arm.
                normal = axis.normalized()
                across_axis = normal.cross(front).normalized()
                widths = []
                for i in range(4):
                    angle = math.pi * i / 4
                    direction = (front * math.cos(angle) + across_axis * math.sin(angle))
                    direction = (direction - normal * direction.dot(normal)).normalized()
                    d1, d2 = reach(middle, direction, 0.25), reach(middle, -direction, 0.25)
                    if d1 > 0.0 and d2 > 0.0:
                        widths.append(0.5 * (d1 + d2))
                radius = float(np.median(widths)) if widths else 0.05
                # Sleeves start at the shoulder.
                frame["ARM_" + side_name] = _Slot(middle, axis, front, radius, radius, 0.45 * axis.length)
            # Leg: below the hips, on this side, measured by rays from its middle (the middle of
            # this side's vertices at that height; hands hanging further out don't count).
            zl = zmin + 0.28 * height
            band = co[np.abs(co[:, 2] - zl) < max(0.01, 0.01 * height)]
            out = side * (band[:, 0] - cx)
            leg = band[(out > 0.0) & (out < hip_width * 1.4)]
            if len(leg) >= 8:
                guess = Vector((0.5 * (leg[:, 0].min() + leg[:, 0].max()), 0.5 * (leg[:, 1].min() + leg[:, 1].max()), zl))
            else:
                guess = Vector((cx + side * hip_width * 0.5, hip_center.y, zl))
            right, left = reach(guess, (1, 0, 0), 0.3), reach(guess, (-1, 0, 0), 0.3)
            ahead, behind = reach(guess, (0, -1, 0), 0.3), reach(guess, (0, 1, 0), 0.3)
            if min(right, left, ahead, behind) > 0.0:
                center = guess + Vector(((right - left) / 2.0, (behind - ahead) / 2.0, 0.0))
                ra, rb = (right + left) / 2.0, (ahead + behind) / 2.0
                # Trouser legs start at the crotch.
                top = zmin + 0.47 * height - zl
                frame["LEG_%s_FRONT" % side_name] = _Slot(center, up, front, ra, rb, top)
                frame["LEG_%s_BACK" % side_name] = _Slot(center, up, back, ra, rb, top)
    _avatar_cache.clear()
    _avatar_cache[key] = frame
    return frame


def arrangement_point(avatar, slot):
    """Where the arrangement point for ``slot`` is drawn: on the surface the piece wraps."""
    entry = avatar_frame(avatar).get(slot)
    return entry.point() if entry else None


def _ellipse_angle(u, ra, rb):
    """The angle on an ellipse (radii ``ra`` across, ``rb`` deep, measured from the front)
    at arc length ``u`` from the front."""
    t = np.linspace(0.0, math.pi, 512)
    dx = ra * np.cos(t)
    dy = rb * np.sin(t)
    speed = np.sqrt(dx * dx + dy * dy)
    arc = np.concatenate(([0.0], np.cumsum(0.5 * (speed[1:] + speed[:-1]) * np.diff(t))))
    return np.sign(u) * np.interp(np.abs(u), arc, t)


def wrap_piece(points, slot, offset=(0.0, 0.0), bounds=None):
    """Wrap a piece (flat world points, N x 3) around an arrangement slot, moved by ``offset``
    (around the body, up). ``bounds`` (lo, hi): the piece's, when ``points`` are only some of its
    points. Returns N x 3."""
    lo, hi = bounds if bounds is not None else (points.min(axis=0), points.max(axis=0))
    middle = 0.5 * (lo + hi)
    u = points[:, 0] - middle[0] + offset[0]
    v = points[:, 1] - (middle[1] if slot.top is None else hi[1] - slot.top) + offset[1]
    ra = slot.ra + ARRANGE_CLEARANCE
    rb = slot.rb + ARRANGE_CLEARANCE
    t = _ellipse_angle(u, ra, rb)
    # Seen from outside, the piece's +X runs to the viewer's right.
    right = np.array((-slot.facing).cross(slot.axis))
    facing = np.array(slot.facing)
    axis = np.array(slot.axis)
    return (np.array(slot.center) + np.outer(ra * np.sin(t), right) + np.outer(rb * np.cos(t), facing) +
            np.outer(v, axis))


# -----------------------------------------------------------------------------
# Simulation (Blender cloth)

def _sew_pairs(seq_a, seq_b, flat):
    """Sewing edges matching two vertex sequences by their position along the seam."""
    ta = pattern.arc_params(flat[seq_a][:, :2])
    tb = pattern.arc_params(flat[seq_b][:, :2])
    pairs = set()
    for i, t in enumerate(ta):
        pairs.add((seq_a[i], seq_b[int(np.abs(tb - t).argmin())]))
    for j, t in enumerate(tb):
        pairs.add((seq_a[int(np.abs(ta - t).argmin())], seq_b[j]))
    return [p for p in pairs if p[0] != p[1]]


def garment_signature(scene):
    """Everything the Garment object is built from; a change means it has to be rebuilt."""
    settings = scene.phantom_garment
    parts = [settings.solver, settings.avatar.name if settings.avatar else "",
             tuple(round(x, 4) for row in settings.avatar.matrix_world for x in row) if settings.avatar else (),
             round(settings.particle_distance, 5), round(settings.skin_offset, 5),
             round(settings.sewing_force, 3)]
    for ob in pattern_pieces(scene):
        props = ob.phantom_garment
        mat = ob.matrix_world.to_3x3()
        parts.append((ob.name, props.arrangement, props.fabric, len(ob.data.vertices), tuple(props.offset),
                      tuple(round(x, 4) for p in props.points for x in (p.co[0], p.co[1], p.curve, p.has_handle, *p.handle)),
                      tuple(round(x, 4) for p in props.pins for x in (*p.lo, *p.hi)),
                      tuple(round(x, 4) for row in mat for x in row)))
    for seam in valid_seams(scene):
        parts.append(tuple((side[0].name, side[1], round(side[2], 4), round(side[3], 4)) for side in seam_sides(seam)))
    for elastic in valid_elastics(scene):
        parts.append((elastic.piece.name, elastic.segment, round(elastic.ratio, 3)))
    return repr(parts)


def newton_active(scene):
    """True when the garment is simulated with NVIDIA Newton (chosen, and installed)."""
    if scene.phantom_garment.solver != 'GPU':
        return False
    import phantom_garment_newton
    return phantom_garment_newton.available()


def garment_needs_build(scene):
    return _built["signature"] != garment_signature(scene) or bpy.data.objects.get(GARMENT_OBJECT) is None


def remove_garment():
    import phantom_garment_newton
    phantom_garment_newton.discard()
    ob = bpy.data.objects.get(GARMENT_OBJECT)
    if ob is not None:
        mesh = ob.data
        bpy.data.objects.remove(ob)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def _weights(group, values, lo, hi):
    """Fill a stiffness vertex group so each vertex gets ``value`` between ``lo`` and ``hi``."""
    span = hi - lo
    for value, indices in values.items():
        weight = (value - lo) / span if span > 0.0 else 0.0
        if weight > 0.0:
            group.add(indices, min(1.0, weight), 'REPLACE')


class GarmentData:
    """What a garment is made of, ready for a simulator: the placed pieces as one mesh."""

    def __init__(self):
        self.placed = []          # Pattern piece objects, in order.
        self.fabrics = []         # Fabric keys used, in FABRICS order (material slots).
        self.offsets = {}         # Piece -> index of its first vertex.
        self.verts3d = None       # N x 3: wrapped around the avatar.
        self.flat = None          # N x 3: the flat pattern (Z = 0), the rest shape.
        self.faces = []           # Vertex index lists.
        self.materials = []       # Per face: index into fabrics.
        self.by_fabric = {}       # Fabric -> vertex indices.
        self.area = {}            # Fabric -> pattern area (m2).
        self.pins = []            # Pinned vertex indices.
        self.sew = []             # (i, j) vertex pairs to sew together.
        self.shrink = {}          # Vertex index -> how much its elastic segment pulls in (0-1).
        self.elastic = []         # (vertex indices along an elastic segment, ratio).


def collect_garment(scene):
    """The placed pieces, seams, pins and elastics as one mesh. Returns (GarmentData or None,
    message)."""
    settings = scene.phantom_garment
    avatar = settings.avatar
    spacing = settings.particle_distance
    frame = avatar_frame(avatar) if avatar else {}
    placed = [ob for ob in pattern_pieces(scene) if ob.phantom_garment.arrangement in frame]
    if avatar is None or not placed:
        return None, "Choose an avatar first" if avatar is None else "Place a pattern piece on the avatar first"

    data = GarmentData()
    data.placed = placed
    data.fabrics = sorted({ob.phantom_garment.fabric for ob in placed}, key=list(FABRICS).index)
    data.by_fabric = {fabric: [] for fabric in data.fabrics}
    data.area = {fabric: 0.0 for fabric in data.fabrics}
    verts3d, flat = [], []
    total = 0
    for ob in placed:
        props = ob.phantom_garment
        points = piece_world_points(ob)
        data.offsets[ob] = total
        verts3d.append(wrap_piece(points, frame[props.arrangement], props.offset))
        flat.append(np.column_stack((points[:, 0], points[:, 1], np.zeros(len(points)))))
        data.faces += [[total + i for i in poly.vertices] for poly in ob.data.polygons]
        data.materials += [data.fabrics.index(props.fabric)] * len(ob.data.polygons)
        data.by_fabric[props.fabric] += range(total, total + len(points))
        data.area[props.fabric] += (sum(poly.area for poly in ob.data.polygons) *
                                    abs(ob.matrix_world.to_3x3().determinant()))
        data.pins += [total + int(i) for i in np.nonzero(pinned_vertices(ob))[0]]
        total += len(points)
    data.verts3d = np.concatenate(verts3d)
    data.flat = np.concatenate(flat)

    for seam in valid_seams(scene):
        sides = seam_sides(seam)
        if any(side[0] not in data.offsets for side in sides):
            continue
        seqs = [[data.offsets[ob] + i for i in side_vertices(ob, segment, start, end, spacing)]
                for ob, segment, start, end in sides]
        if all(len(seq) >= 2 for seq in seqs):
            data.sew += _sew_pairs(seqs[0], seqs[1], data.flat)

    for elastic in valid_elastics(scene):
        if elastic.piece in data.offsets:
            indices = [data.offsets[elastic.piece] + i for i in piece_segments(elastic.piece)[elastic.segment]]
            data.elastic.append((indices, elastic.ratio))
            for index in indices:
                data.shrink[index] = max(data.shrink.get(index, 0.0), 1.0 - elastic.ratio)
    return data, ""


def garment_object(scene, mesh):
    """Put ``mesh`` on the Garment object (made if needed), cleared of modifiers and groups."""
    # Keep the same object and swap its mesh: deleting and adding an object on every change is
    # needless churn for the depsgraph and the view layer.
    ob = bpy.data.objects.get(GARMENT_OBJECT)
    if ob is None or ob.type != 'MESH':
        ob = bpy.data.objects.new(GARMENT_OBJECT, mesh)
    else:
        ob.modifiers.clear()
        ob.vertex_groups.clear()
        if ob.data.shape_keys is not None:
            ob.shape_key_clear()
        old = ob.data
        ob.data = mesh
        if old.users == 0:
            bpy.data.meshes.remove(old)
    coll = ensure_collection(scene, GARMENT_COLLECTION)
    if ob.name not in coll.objects:
        coll.objects.link(ob)
    mesh.name = GARMENT_OBJECT
    try:
        # Make Sculptable hides it; a rebuilt garment should show again.
        ob.hide_set(False)
    except RuntimeError:
        pass
    return ob


def garment_mesh(data, sewing_edges=True):
    """A new mesh of the garment, wrapped around the avatar."""
    mesh = bpy.data.meshes.new(GARMENT_OBJECT)
    mesh.from_pydata(data.verts3d.tolist(), data.sew if sewing_edges else [], data.faces)
    for fabric in data.fabrics:
        mesh.materials.append(fabric_material(fabric))
    mesh.polygons.foreach_set("material_index", data.materials)
    mesh.update()
    mesh.shade_smooth()
    return mesh


def build_garment(scene):
    """(Re)build the Garment cloth object from the placed pieces and their seams.

    Returns (object or None, message).
    """
    settings = scene.phantom_garment
    avatar = settings.avatar
    _built["signature"] = garment_signature(scene)
    data, message = collect_garment(scene)
    import phantom_garment_newton
    phantom_garment_newton.stop()
    if data is None:
        remove_garment()
        return None, message
    if newton_active(scene):
        ob, message = phantom_garment_newton.build(scene, data)
        if ob is not None:
            return ob, message
        # The GPU cloth couldn't be built: fall back to Blender's cloth.
    phantom_garment_newton.discard()

    fabrics, by_fabric, area, pins, shrink = data.fabrics, data.by_fabric, data.area, data.pins, data.shrink
    spacing = settings.particle_distance
    flat = data.flat
    ob = garment_object(scene, garment_mesh(data))

    ob.shape_key_add(name="Basis", from_mix=False)
    key = ob.shape_key_add(name=FLAT_KEY, from_mix=False)
    key.data.foreach_set("co", flat.ravel())
    # Only the cloth's rest shape: the mesh itself stays wrapped around the avatar.
    key.value = 0.0
    pin_group = ob.vertex_groups.new(name="Pins")
    if pins:
        pin_group.add(pins, 1.0, 'REPLACE')
    shrink_max = max(shrink.values(), default=0.0)
    if shrink_max > 0.0:
        group = ob.vertex_groups.new(name="Elastic")
        for index, amount in shrink.items():
            group.add([index], amount / shrink_max, 'REPLACE')

    # Fabrics: the cloth has one minimum and one maximum per stiffness, and a vertex group picks
    # each vertex's place between them. Seams use the maximum tension, so it is set well above
    # every fabric.
    info = [FABRICS[f] for f in fabrics]
    tension = {f["tension"]: [] for f in info}
    shear = {f["shear"]: [] for f in info}
    bending = {f["bending"]: [] for f in info}
    for fabric in fabrics:
        tension[FABRICS[fabric]["tension"]] += by_fabric[fabric]
        shear[FABRICS[fabric]["shear"]] += by_fabric[fabric]
        bending[FABRICS[fabric]["bending"]] += by_fabric[fabric]
    t_lo, seam_tension = min(tension), max(tension) * SEAM_STIFFNESS
    s_lo, s_hi = min(shear), max(shear)
    b_lo, b_hi = min(bending), max(bending)
    tension_group = ob.vertex_groups.new(name="Fabric Tension")
    _weights(tension_group, tension, t_lo, seam_tension)
    # Elastic bands are much stiffer than the fabric they gather, or they would stretch and let go.
    if shrink:
        tension_group.add(list(shrink), ELASTIC_STIFFNESS, 'REPLACE')
    _weights(ob.vertex_groups.new(name="Fabric Shear"), shear, s_lo, s_hi)
    _weights(ob.vertex_groups.new(name="Fabric Bending"), bending, b_lo, b_hi)

    mod = ob.modifiers.new("Cloth", 'CLOTH')
    s = mod.settings
    s.rest_shape_key = key
    s.use_sewing_springs = True
    s.sewing_force_max = settings.sewing_force
    s.vertex_group_mass = "Pins"
    if shrink_max > 0.0:
        s.vertex_group_shrink = "Elastic"
        s.shrink_min = 0.0
        s.shrink_max = shrink_max
    s.quality = max(f["quality"] for f in info)
    # Blender's cloth mass is per vertex, and its solver and presets are tuned for about 0.3 kg
    # per vertex: with real fabric weights (a twentieth of a gram) its implicit solve struggles and
    # cloth falls in slow motion. So cotton at the default 2 cm particle distance gets 0.3, other
    # fabrics in proportion to their weight, and finer meshes in proportion to their vertex area.
    density = sum(FABRICS[f]["density"] * area[f] for f in fabrics) / max(1e-9, sum(area.values()))
    s.mass = MASS_SCALE * (density / FABRICS['COTTON']["density"]) * (spacing / 0.02) ** 2
    s.tension_stiffness = s.compression_stiffness = t_lo
    s.tension_stiffness_max = s.compression_stiffness_max = seam_tension
    s.vertex_group_structural_stiffness = "Fabric Tension"
    s.shear_stiffness = s_lo
    s.shear_stiffness_max = s_hi
    s.vertex_group_shear_stiffness = "Fabric Shear"
    s.bending_stiffness = b_lo
    s.bending_stiffness_max = b_hi
    s.vertex_group_bending = "Fabric Bending"
    s.tension_damping = s.compression_damping = s.shear_damping = float(np.mean([f["damping"] for f in info]))
    s.air_damping = 1.0
    cs = mod.collision_settings
    cs.use_collision = True
    cs.distance_min = 0.004
    cs.collision_quality = 3
    cs.use_self_collision = True
    cs.self_distance_min = 0.003
    mod.point_cache.frame_start = 1
    mod.point_cache.frame_end = SIM_FRAMES
    # Sew first, then drape: no gravity while the seams close and elastics pull in, then ease it in.
    weights = s.effector_weights
    for frame, gravity in ((1, 0.0), (SEW_FRAMES, 0.0), (SEW_FRAMES + GRAVITY_EASE, 1.0)):
        weights.gravity = gravity
        weights.keyframe_insert("gravity", frame=frame)

    if not any(m.type == 'COLLISION' for m in avatar.modifiers):
        avatar.modifiers.new("Collision", 'COLLISION')
    avatar.collision.thickness_outer = max(0.001, settings.skin_offset)
    avatar.collision.cloth_friction = 5.0
    avatar.collision.use_culling = True

    scene.frame_start = 1
    scene.frame_end = SIM_FRAMES
    # Cloth needs every frame in order: skipping frames would stop the simulation.
    scene.sync_mode = 'NONE'
    scene.frame_set(1)
    return ob, ""


def garment_pinned_indices():
    ob = bpy.data.objects.get(GARMENT_OBJECT)
    group = ob.vertex_groups.get("Pins") if ob else None
    if group is None:
        return []
    index = group.index
    return [v.index for v in ob.data.vertices if any(g.group == index for g in v.groups)]


# -----------------------------------------------------------------------------
# Mannequin

def _superellipse_ring(bm, z, half_width, half_depth, segments=32, power=2.6):
    ring = []
    for i in range(segments):
        a = 2.0 * math.pi * i / segments
        c, s = math.cos(a), math.sin(a)
        x = half_width * math.copysign(abs(c) ** (2.0 / power), c)
        y = half_depth * math.copysign(abs(s) ** (2.0 / power), s)
        ring.append(bm.verts.new((x, y, z)))
    return ring


def build_mannequin_mesh(mesh):
    """A simple full-body mannequin (about 1.76 m, A-pose), standing on the origin, facing -Y."""
    bm = bmesh.new()
    # Torso from the crotch to the neck: (height, half width, half depth).
    profile = (
        (0.80, 0.12, 0.085), (0.86, 0.165, 0.11), (0.92, 0.185, 0.125), (0.99, 0.178, 0.118),
        (1.06, 0.155, 0.102), (1.12, 0.14, 0.095), (1.20, 0.15, 0.105), (1.28, 0.165, 0.118),
        (1.36, 0.175, 0.115), (1.42, 0.18, 0.10), (1.46, 0.14, 0.08), (1.485, 0.06, 0.055),
        (1.56, 0.05, 0.05),
    )
    rings = [_superellipse_ring(bm, z, w, d) for z, w, d in profile]
    for lower, upper in zip(rings, rings[1:]):
        n = len(lower)
        for i in range(n):
            bm.faces.new((lower[i], lower[(i + 1) % n], upper[(i + 1) % n], upper[i]))
    bm.faces.new(list(reversed(rings[0])))
    bm.faces.new(rings[-1])

    head = Matrix.Translation((0.0, -0.01, 1.66)) @ Matrix.Diagonal((0.085, 0.1, 0.115, 1.0))
    bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=16, radius=1.0, matrix=head)

    def limb(start, end, r_start, r_end):
        start, end = Vector(start), Vector(end)
        axis = end - start
        rot = axis.to_track_quat('Z', 'Y').to_matrix().to_4x4()
        mat = Matrix.Translation((start + end) / 2.0) @ rot
        bmesh.ops.create_cone(bm, cap_ends=True, segments=16, radius1=r_start, radius2=r_end,
                              depth=axis.length, matrix=mat)

    for side in (-1.0, 1.0):
        limb((side * 0.09, 0.0, 0.88), (side * 0.11, 0.0, 0.04), 0.09, 0.05)
        limb((side * 0.15, 0.0, 1.40), (side * 0.48, 0.0, 0.94), 0.05, 0.032)
    bm.to_mesh(mesh)
    bm.free()
    mesh.shade_smooth()


def add_mannequin(context):
    scene = context.scene
    mesh = bpy.data.meshes.new("Mannequin")
    build_mannequin_mesh(mesh)
    mat = bpy.data.materials.get("Mannequin") or bpy.data.materials.new("Mannequin")
    mat.diffuse_color = (0.55, 0.5, 0.48, 1.0)
    mat.roughness = 0.7
    mesh.materials.append(mat)
    ob = bpy.data.objects.new("Mannequin", mesh)
    ensure_collection(scene, AVATAR_COLLECTION).objects.link(ob)
    sub = ob.modifiers.new("Smooth", 'SUBSURF')
    sub.levels = sub.render_levels = 1
    scene.phantom_garment.avatar = ob
    return ob


# The bodies to dress, besides the mannequin: Phantom's sculpting base meshes.
AVATAR_BASE_MESHES = (
    'BODY_FEMALE_REALISTIC', 'BODY_MALE_REALISTIC', 'HEAD_SCULPT', 'BODY_FEMALE_STYLIZED_1',
    'BODY_FEMALE_STYLIZED', 'BODY_MALE_STYLIZED', 'HEAD_STYLIZED',
)


_avatar_enum = []


def _avatar_items(_self, _context):
    # Kept in a module list: Blender needs the strings to outlive the call.
    if not _avatar_enum:
        import phantom_start_assets as assets
        _avatar_enum.append(('MANNEQUIN', "Mannequin", "A simple, light mannequin"))
        for key in AVATAR_BASE_MESHES:
            name = assets.ALL[key][1]
            _avatar_enum.append((key, name, "Dress the %s base mesh" % name))
    return _avatar_enum


def clear_avatars(scene):
    """Remove the avatars we added before (everything in the Avatar collection)."""
    coll = bpy.data.collections.get(AVATAR_COLLECTION)
    if coll is None:
        return
    for ob in list(coll.all_objects):
        bpy.data.objects.remove(ob)
    for child in list(coll.children_recursive):
        bpy.data.collections.remove(child)


def add_avatar(context, choice):
    """Replace the avatar with the mannequin or one of the base meshes. Returns the new avatar."""
    scene = context.scene
    clear_avatars(scene)
    if choice == 'MANNEQUIN':
        return add_mannequin(context)
    import phantom_start_assets as assets
    appended = assets.append_base_mesh(context, choice, ensure_collection(scene, AVATAR_COLLECTION),
                                       strip_drivers=True)
    if appended is None:
        return None
    target, added, _block = appended
    for ob in added:
        # The collisions and silhouette only need the body's shape, not its sculpting detail.
        for mod in ob.modifiers:
            if mod.type == 'MULTIRES':
                mod.levels = min(mod.levels, 1)
            elif mod.type == 'SUBSURF':
                mod.levels = min(mod.levels, 1)
        ob.select_set(False)
    scene.phantom_garment.avatar = target
    return target


# -----------------------------------------------------------------------------
# Make Sculptable

def bake_garment(context, thickness):
    """Turn the current frame of the simulation into a closed, sculptable mesh."""
    scene = context.scene
    garment = bpy.data.objects.get(GARMENT_OBJECT)
    if garment is None:
        return None
    depsgraph = context.evaluated_depsgraph_get()
    mesh = bpy.data.meshes.new_from_object(garment.evaluated_get(depsgraph))
    bm = bmesh.new()
    bm.from_mesh(mesh)
    # Close the seams: weld the vertices the sewing springs pulled together, then drop the springs.
    sewn = list({v for e in bm.edges if e.is_wire for v in e.verts})
    weld = scene.phantom_garment.particle_distance * 0.75
    if sewn:
        bmesh.ops.remove_doubles(bm, verts=sewn, dist=weld)
    wires = [e for e in bm.edges if e.is_wire]
    if wires:
        bmesh.ops.delete(bm, geom=wires, context='EDGES')
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    if thickness > 0.0:
        result = bmesh.ops.solidify(bm, geom=bm.faces[:], thickness=thickness)
        del result
    bm.to_mesh(mesh)
    bm.free()
    mesh.shade_smooth()
    old = bpy.data.objects.get(SCULPT_OBJECT)
    name = SCULPT_OBJECT if old is None else SCULPT_OBJECT + ".001"
    mesh.name = name
    ob = bpy.data.objects.new(name, mesh)
    ensure_collection(scene, GARMENT_COLLECTION).objects.link(ob)
    for other in context.view_layer.objects:
        if other is not None and other.select_get():
            other.select_set(False)
    garment.hide_set(True)
    ob.select_set(True)
    context.view_layer.objects.active = ob
    return ob


# -----------------------------------------------------------------------------
# Operators

class PHANTOM_OT_garment_add_rectangle(Operator):
    """Add a rectangular pattern piece"""
    bl_idname = "phantom.garment_add_rectangle"
    bl_label = "Add Rectangle Pattern"
    bl_options = {'REGISTER', 'UNDO'}

    width: FloatProperty(name="Width", default=0.5, min=0.01, unit='LENGTH')
    height: FloatProperty(name="Height", default=0.6, min=0.01, unit='LENGTH')
    location: FloatVectorProperty(name="Location", size=2, unit='LENGTH', default=(BOARD_ORIGIN.x, BOARD_ORIGIN.y))
    ellipse: BoolProperty(name="Ellipse", description="Make an ellipse instead")

    def execute(self, context):
        shape = (pattern.ellipse if self.ellipse else pattern.rectangle)(self.width, self.height)
        x, y = self.location
        add_piece(context, [(px + x, py + y, curve) for px, py, curve in shape])
        play_sound("loop_add")
        return {'FINISHED'}


class PHANTOM_OT_garment_add_polygon(Operator):
    """Add a pattern piece from an outline (corners and curve points)"""
    bl_idname = "phantom.garment_add_polygon"
    bl_label = "Add Pattern"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    points: StringProperty(description="JSON list of [x, y, curve] in board coordinates")

    def execute(self, context):
        try:
            points = [(float(x), float(y), parse_curve(c)) for x, y, c in json.loads(self.points)]
        except (ValueError, TypeError):
            return {'CANCELLED'}
        if len(points) < 3 or abs(pattern.signed_area(points)) < 1e-4:
            self.report({'WARNING'}, "That outline is too small to make a piece")
            play_sound("error")
            return {'CANCELLED'}
        add_piece(context, points)
        play_sound("loop_add")
        return {'FINISHED'}


class PHANTOM_OT_garment_set_points(Operator):
    """Change a pattern piece's outline"""
    bl_idname = "phantom.garment_set_points"
    bl_label = "Edit Pattern"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    piece: StringProperty()
    points: StringProperty(description="JSON list of [x, y, curve] in the piece's coordinates")
    old_points: StringProperty(description="The outline before the change, when its corners changed")

    def execute(self, context):
        ob = bpy.data.objects.get(self.piece)
        if ob is None:
            return {'CANCELLED'}
        points = [(float(x), float(y), parse_curve(c)) for x, y, c in json.loads(self.points)]
        if len(points) < 3:
            self.report({'WARNING'}, "A piece needs at least three points")
            play_sound("error")
            return {'CANCELLED'}
        set_piece_points(ob, points)
        regenerate_piece(ob, context.scene)
        if self.old_points:
            old = [(float(x), float(y), parse_curve(c)) for x, y, c in json.loads(self.old_points)]
            remap_after_reshape(context.scene, ob, old)
        return {'FINISHED'}


class PHANTOM_OT_garment_add_seam(Operator):
    """Sew a stretch of one pattern piece's outline to a stretch of another's"""
    bl_idname = "phantom.garment_add_seam"
    bl_label = "Add Seam"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    piece_a: StringProperty()
    segment_a: IntProperty(min=0)
    start_a: FloatProperty(default=0.0)
    end_a: FloatProperty(default=1.0)
    piece_b: StringProperty()
    segment_b: IntProperty(min=0)
    start_b: FloatProperty(default=0.0)
    end_b: FloatProperty(default=1.0)

    def execute(self, context):
        a = bpy.data.objects.get(self.piece_a)
        b = bpy.data.objects.get(self.piece_b)
        same = a == b and self.segment_a == self.segment_b and \
            max(self.start_a, self.end_a) > min(self.start_b, self.end_b) and \
            max(self.start_b, self.end_b) > min(self.start_a, self.end_a)
        if a is None or b is None or same or abs(self.end_a - self.start_a) < 0.02 or abs(self.end_b - self.start_b) < 0.02:
            self.report({'WARNING'}, "Pick two different stretches of outline")
            play_sound("error")
            return {'CANCELLED'}
        seam = context.scene.phantom_garment.seams.add()
        seam.ranged = True
        seam.piece_a, seam.segment_a, seam.start_a, seam.end_a = a, self.segment_a, self.start_a, self.end_a
        seam.piece_b, seam.segment_b, seam.start_b, seam.end_b = b, self.segment_b, self.start_b, self.end_b
        _request_rebuild()
        play_sound("confirm")
        return {'FINISHED'}


class PHANTOM_OT_garment_remove_seam(Operator):
    """Remove this seam"""
    bl_idname = "phantom.garment_remove_seam"
    bl_label = "Remove Seam"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    index: IntProperty()

    def execute(self, context):
        seams = context.scene.phantom_garment.seams
        if 0 <= self.index < len(seams):
            seams.remove(self.index)
            _request_rebuild()
            play_sound("loop_remove")
        return {'FINISHED'}


class PHANTOM_OT_garment_flip_seam(Operator):
    """Reverse the direction of this seam (use it when the sewing lines cross)"""
    bl_idname = "phantom.garment_flip_seam"
    bl_label = "Flip Seam"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    index: IntProperty()

    def execute(self, context):
        seams = context.scene.phantom_garment.seams
        if 0 <= self.index < len(seams):
            seam = seams[self.index]
            seam.start_b, seam.end_b = seam.end_b, seam.start_b
            _request_rebuild()
            play_sound("click")
        return {'FINISHED'}


class PHANTOM_OT_garment_toggle_elastic(Operator):
    """Make a pattern segment elastic (it pulls in, gathering the fabric), or not"""
    bl_idname = "phantom.garment_toggle_elastic"
    bl_label = "Toggle Elastic"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    piece: StringProperty()
    segment: IntProperty(min=0)

    def execute(self, context):
        ob = bpy.data.objects.get(self.piece)
        if ob is None:
            return {'CANCELLED'}
        elastics = context.scene.phantom_garment.elastics
        for i, elastic in enumerate(elastics):
            if elastic.piece == ob and elastic.segment == self.segment:
                elastics.remove(i)
                play_sound("toggle_off")
                break
        else:
            elastic = elastics.add()
            elastic.piece, elastic.segment = ob, self.segment
            play_sound("toggle_on")
        _request_rebuild()
        return {'FINISHED'}


class PHANTOM_OT_garment_add_pin(Operator):
    """Pin part of a pattern piece, so it holds still in the simulation"""
    bl_idname = "phantom.garment_add_pin"
    bl_label = "Pin"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    lo: FloatVectorProperty(size=2, description="Corner of the pinned box on the board")
    hi: FloatVectorProperty(size=2, description="Opposite corner of the pinned box on the board")

    def execute(self, context):
        added = 0
        board = [Vector((x, y, 0.0)) for x in (self.lo[0], self.hi[0]) for y in (self.lo[1], self.hi[1])]
        for ob in pattern_pieces(context.scene):
            inverse = ob.matrix_world.inverted()
            local = np.array([(inverse @ p).xy for p in board])
            lo, hi = local.min(axis=0), local.max(axis=0)
            co = piece_local_points(ob)
            if not np.any((co[:, 0] >= lo[0]) & (co[:, 0] <= hi[0]) & (co[:, 1] >= lo[1]) & (co[:, 1] <= hi[1])):
                continue
            pin = ob.phantom_garment.pins.add()
            pin.lo, pin.hi = lo, hi
            added += 1
        if not added:
            return {'CANCELLED'}
        _request_rebuild()
        play_sound("measure_tick")
        return {'FINISHED'}


class PHANTOM_OT_garment_remove_pins(Operator):
    """Remove the pins under a point, or all of a piece's pins"""
    bl_idname = "phantom.garment_remove_pins"
    bl_label = "Remove Pins"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    piece: StringProperty(description="Remove all of this piece's pins")
    at: FloatVectorProperty(size=2, description="Remove pins containing this board point")
    use_at: BoolProperty()

    def execute(self, context):
        removed = 0
        for ob in pattern_pieces(context.scene):
            pins = ob.phantom_garment.pins
            if self.piece and ob.name != self.piece:
                continue
            local = (ob.matrix_world.inverted() @ Vector((self.at[0], self.at[1], 0.0))).xy
            for i in reversed(range(len(pins))):
                pin = pins[i]
                if not self.use_at or (pin.lo[0] <= local.x <= pin.hi[0] and pin.lo[1] <= local.y <= pin.hi[1]):
                    pins.remove(i)
                    removed += 1
        if not removed:
            return {'CANCELLED'}
        _request_rebuild()
        play_sound("loop_remove")
        return {'FINISHED'}


class PHANTOM_OT_garment_fabric_to_all(Operator):
    """Use this fabric for every pattern piece"""
    bl_idname = "phantom.garment_fabric_to_all"
    bl_label = "Use for All Pieces"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        fabric = context.scene.phantom_garment.fabric
        for ob in pattern_pieces(context.scene):
            ob.phantom_garment.fabric = fabric
        play_sound("confirm")
        return {'FINISHED'}


class PHANTOM_OT_garment_arrange(Operator):
    """Place the selected pattern pieces on the avatar"""
    bl_idname = "phantom.garment_arrange"
    bl_label = "Arrange"
    bl_options = {'REGISTER', 'UNDO'}

    slot: EnumProperty(items=ARRANGEMENT_ITEMS)
    piece: StringProperty(description="The piece to place (default: the selected pieces)")

    def execute(self, context):
        if self.piece:
            pieces = [bpy.data.objects.get(self.piece)]
        else:
            pieces = [ob for ob in context.selected_objects if ob.phantom_garment.is_piece]
        pieces = [ob for ob in pieces if ob is not None and ob.phantom_garment.is_piece]
        if not pieces:
            self.report({'WARNING'}, "Select a pattern piece in the pattern view first")
            play_sound("error")
            return {'CANCELLED'}
        for ob in pieces:
            ob.phantom_garment.arrangement = self.slot
            ob.phantom_garment.offset = (0.0, 0.0)
        play_sound("transform_confirm")
        return {'FINISHED'}


class PHANTOM_OT_garment_add_mannequin(Operator):
    """Add a simple mannequin and use it as the avatar"""
    bl_idname = "phantom.garment_add_mannequin"
    bl_label = "Add Mannequin"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        add_mannequin(context)
        play_sound("loop_add")
        return {'FINISHED'}


MIRRORED_SLOTS = {
    'ARM_LEFT': 'ARM_RIGHT', 'ARM_RIGHT': 'ARM_LEFT',
    'LEG_LEFT_FRONT': 'LEG_RIGHT_FRONT', 'LEG_RIGHT_FRONT': 'LEG_LEFT_FRONT',
    'LEG_LEFT_BACK': 'LEG_RIGHT_BACK', 'LEG_RIGHT_BACK': 'LEG_LEFT_BACK',
}


class PHANTOM_OT_garment_mirror_copy(Operator):
    """Add a mirrored copy of the selected pieces beside them (a left sleeve from a right one, say)"""
    bl_idname = "phantom.garment_mirror_copy"
    bl_label = "Mirror Copy"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(ob.phantom_garment.is_piece for ob in context.selected_objects)

    def execute(self, context):
        pieces = [ob for ob in context.selected_objects if ob.phantom_garment.is_piece]
        copies = []
        for ob in pieces:
            points = [(-x, y, pattern.mirror_curve(c)) for x, y, c in reversed(piece_points(ob))]
            world = to_world(ob, np.array([(x, y, 0.0) for x, y, _c in points]))
            xs = world[:, 0]
            # Beside the original, with a little gap.
            shift = (xs.max() - xs.min()) + 0.05
            name = ob.name.rsplit(" ", 1)[0] if ob.name.rsplit(" ", 1)[-1].isdigit() else ob.name
            copy = add_piece(context, [(float(p[0]) + shift, float(p[1]), c) for p, (_x, _y, c) in zip(world, points)],
                             name=name)
            copy.phantom_garment.fabric = ob.phantom_garment.fabric
            copy.phantom_garment.arrangement = MIRRORED_SLOTS.get(ob.phantom_garment.arrangement,
                                                                  ob.phantom_garment.arrangement)
            copies.append(copy)
        for ob in copies:
            ob.select_set(True)
        play_sound("loop_add")
        return {'FINISHED'}


class PHANTOM_OT_garment_add_avatar(Operator):
    """Dress a mannequin or one of the base meshes, replacing the avatar added before"""
    bl_idname = "phantom.garment_add_avatar"
    bl_label = "Add Avatar"
    bl_options = {'REGISTER', 'UNDO'}
    bl_property = "choice"

    choice: EnumProperty(name="Avatar", items=_avatar_items)

    def execute(self, context):
        if context.object is not None and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        if add_avatar(context, self.choice) is None:
            self.report({'WARNING'}, "Couldn't find that base mesh")
            return {'CANCELLED'}
        play_sound("loop_add")
        return {'FINISHED'}


def _playing(context):
    screen = context.screen
    return bool(screen and screen.is_animation_playing)


class PHANTOM_OT_garment_simulate(Operator):
    """Start or stop simulating the garment (Space)"""
    bl_idname = "phantom.garment_simulate"
    bl_label = "Simulate"

    @classmethod
    def poll(cls, context):
        ws = context.workspace
        return ws is not None and ws.get("phantom_garment") and context.screen is not None

    def execute(self, context):
        scene = context.scene
        if newton_active(scene):
            import phantom_garment_newton as gpu_cloth
            if gpu_cloth.running():
                gpu_cloth.stop()
                play_sound("toggle_off")
                return {'FINISHED'}
            if garment_needs_build(scene) or gpu_cloth.simulation() is None:
                ob, message = build_garment(scene)
                if ob is None:
                    self.report({'WARNING'}, message)
                    play_sound("error")
                    # Handled: don't let Space go on to play the timeline.
                    return {'FINISHED'}
            gpu_cloth.start()
            play_sound("toggle_on")
            return {'FINISHED'}
        if _playing(context):
            bpy.ops.screen.animation_cancel(restore_frame=False)
            play_sound("toggle_off")
            return {'FINISHED'}
        if garment_needs_build(scene):
            ob, message = build_garment(scene)
            if ob is None:
                self.report({'WARNING'}, message)
                play_sound("error")
                return {'FINISHED'}
        elif scene.frame_current >= SIM_FRAMES:
            scene.frame_set(1)
        bpy.ops.screen.animation_play()
        play_sound("toggle_on")
        return {'FINISHED'}


class PHANTOM_OT_garment_reset(Operator):
    """Rebuild the garment from its patterns and go back to the start of the simulation"""
    bl_idname = "phantom.garment_reset"
    bl_label = "Reset Simulation"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if _playing(context):
            bpy.ops.screen.animation_cancel(restore_frame=False)
        build_garment(context.scene)
        play_sound("undo")
        return {'FINISHED'}


def simulating(context):
    """True while the garment is simulating (Newton running, or Blender cloth playing)."""
    import phantom_garment_newton as gpu_cloth
    return gpu_cloth.running() or _playing(context)


class PHANTOM_OT_garment_make_sculptable(Operator):
    """Bake the garment as it is now into a closed mesh, and sculpt it"""
    bl_idname = "phantom.garment_make_sculptable"
    bl_label = "Make Sculptable"
    bl_options = {'REGISTER', 'UNDO'}

    thickness: FloatProperty(
        name="Thickness", description="Give the fabric this thickness (0 keeps it a single sheet)",
        default=0.002, min=0.0, max=0.05, unit='LENGTH',
    )
    sculpt: BoolProperty(name="Sculpt", description="Switch to the Sculpting workspace", default=True)

    @classmethod
    def poll(cls, context):
        return bpy.data.objects.get(GARMENT_OBJECT) is not None

    def execute(self, context):
        import phantom_garment_newton as gpu_cloth
        gpu_cloth.stop()
        if _playing(context):
            bpy.ops.screen.animation_cancel(restore_frame=False)
        ob = bake_garment(context, self.thickness)
        if ob is None:
            return {'CANCELLED'}
        play_sound("mode_switch")
        sculpting = bpy.data.workspaces.get("Sculpting")
        if self.sculpt and sculpting is not None and context.window is not None:
            context.window.workspace = sculpting
        return {'FINISHED'}


classes = (
    PhantomGarmentPoint,
    PhantomGarmentPin,
    PhantomGarmentPiece,
    PhantomGarmentSeam,
    PhantomGarmentElastic,
    PhantomGarmentSettings,
    PHANTOM_OT_garment_add_rectangle,
    PHANTOM_OT_garment_add_polygon,
    PHANTOM_OT_garment_set_points,
    PHANTOM_OT_garment_add_seam,
    PHANTOM_OT_garment_remove_seam,
    PHANTOM_OT_garment_flip_seam,
    PHANTOM_OT_garment_toggle_elastic,
    PHANTOM_OT_garment_add_pin,
    PHANTOM_OT_garment_remove_pins,
    PHANTOM_OT_garment_fabric_to_all,
    PHANTOM_OT_garment_arrange,
    PHANTOM_OT_garment_add_mannequin,
    PHANTOM_OT_garment_add_avatar,
    PHANTOM_OT_garment_mirror_copy,
    PHANTOM_OT_garment_simulate,
    PHANTOM_OT_garment_reset,
    PHANTOM_OT_garment_make_sculptable,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.phantom_garment = PointerProperty(type=PhantomGarmentPiece)
    bpy.types.Scene.phantom_garment = PointerProperty(type=PhantomGarmentSettings)


def unregister():
    del bpy.types.Scene.phantom_garment
    del bpy.types.Object.phantom_garment
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
