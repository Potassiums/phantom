# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: Extract, from the mask actions bar.

Clicking Extract makes a new object from the masked faces right away, while Sculpt Mode stays on
the masked mesh. A panel in the viewport shapes it live:

- **Scale**: its size, around its middle.
- **Inflate**: pushes it out along its normals (or in, below zero).
- **Thickness**: a solid shell, grown outward from the sculpt (or inward, below zero); 0 leaves a
  single-sided surface.
- **Smoothness**: smooths the edge of the extracted shape (or sharpens it, below zero).

Every slider starts in the middle, at "nothing applied" (scale 1, the others 0), and fills from
there. Clicking a value types an exact one (Enter applies it, Esc keeps the old one, Tab goes on to
the next); typed values can go past the slider's ends.

**Accept** (or Enter) makes it a plain mesh, ready to sculpt, with polygroups: one for all of it,
or with a thickness one each for its outside, its inside (the side on the sculpt) and its rim.
Their colors are never the masked mesh's.
**Cancel** (or Esc) deletes it. The sliders are modifiers until then (Smooth on the edge,
Displace, Solidify), so each change shows at once.
"""

import math
import time
from collections import namedtuple

import blf
import bmesh
import bpy
import gpu
from bpy.types import Operator
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

# Unscaled pixels.
PANEL_W = 300
PAD = 12
ROW_H = 22
LABEL_W = 78
VALUE_W = 44
TRACK_H = 4
KNOB_R = 6
# A drag this close to the middle snaps to "nothing applied".
SNAP = 5
BUTTON_H = 24
TITLE_SIZE = 12
TEXT_SIZE = 11
CORNER = 4
# Room left below for the mask actions bar.
BAR_ROOM = 56
FADE_TIME = 0.14

FIELD_COLOR = (0.0, 0.0, 0.0, 0.35)
FIELD_OUTLINE_COLOR = (0.55, 0.55, 1.0, 0.8)
# The value boxes show they can be clicked to type in.
FIELD_IDLE_COLOR = (0.0, 0.0, 0.0, 0.18)
FIELD_IDLE_OUTLINE_COLOR = (1.0, 1.0, 1.0, 0.22)
FIELD_HOVER_OUTLINE_COLOR = (0.55, 0.55, 1.0, 0.65)
PANEL_COLOR = (0.105, 0.105, 0.125, 0.94)
OUTLINE_COLOR = (0.55, 0.55, 1.0, 0.35)   # Phantom periwinkle.
TRACK_COLOR = (1.0, 1.0, 1.0, 0.14)
FILL_COLOR = (0.55, 0.55, 1.0, 0.85)
KNOB_COLOR = (0.92, 0.92, 0.95, 1.0)
TEXT_COLOR = (0.92, 0.92, 0.95, 1.0)
DIM_TEXT_COLOR = (0.92, 0.92, 0.95, 0.6)
ACCEPT_COLOR = (1.0, 0.62, 0.34, 0.45)    # Phantom orange.
ACCEPT_HOVER_COLOR = (1.0, 0.62, 0.34, 0.65)
CANCEL_COLOR = (1.0, 1.0, 1.0, 0.08)
CANCEL_HOVER_COLOR = (1.0, 1.0, 1.0, 0.16)

# `neutral` is "nothing applied": where each slider starts, in the middle of its range, and what it
# fills from. Typed values may go from `typed_min` to `typed_max`.
Slider = namedtuple("Slider", "key label min max neutral fmt typed_min typed_max")
SLIDERS = (
    Slider("scale", "Scale", 0.25, 1.75, 1.0, "{:.2f}", 0.01, 10.0),
    Slider("inflate", "Inflate", -1.0, 1.0, 0.0, "{:+.2f}", -10.0, 10.0),
    Slider("thickness", "Thickness", -1.0, 1.0, 0.0, "{:+.2f}", -10.0, 10.0),
    Slider("smoothness", "Smoothness", -1.0, 1.0, 0.0, "{:+.2f}", -1.0, 1.0),
)
SLIDER = {slider.key: slider for slider in SLIDERS}
# Inflate and thickness at 1.0, as a share of the masked mesh's size.
INFLATE_AT_ONE = 0.05
THICKNESS_AT_ONE = 0.1
# Smoothing iterations at smoothness 1.0, and how many rings in from the edge it reaches.
SMOOTH_ITERATIONS = 30
EDGE_RINGS = (1.0, 0.6, 0.3)
MASK_THRESHOLD = 0.5

EDGE_GROUP = "Extract Edge"
MOD_SMOOTH = "Extract Smoothness"
MOD_INFLATE = "Extract Inflate"
MOD_THICKNESS = "Extract Thickness"
# Polygroups of an accepted extract.
POLYGROUP_OUTSIDE = 1
POLYGROUP_INSIDE = 2
POLYGROUP_RIM = 3
# Polygroup colors, as `BKE_paint_face_set_overlay_color_get` picks their hues (keep in step).
GOLDEN_RATIO_CONJUGATE = 0.618033988749895
DEFAULT_HUE = 0.0235  # Scarlet, the default polygroup's.
RED_BAND = 0.06


def _hue_distance(a, b):
    d = abs(a - b) % 1.0
    return min(d, 1.0 - d)


def polygroup_hue(face_set, default_face_set, seed):
    """The hue a polygroup is drawn in."""
    if face_set == default_face_set:
        return DEFAULT_HUE
    hue = GOLDEN_RATIO_CONJUGATE * (face_set + math.fmod(seed, 10))
    hue -= math.floor(hue)
    from_scarlet = hue - DEFAULT_HUE
    from_scarlet -= math.floor(from_scarlet + 0.5)
    if abs(from_scarlet) < RED_BAND:
        hue += -2.0 * RED_BAND if from_scarlet < 0.0 else 2.0 * RED_BAND
        hue -= math.floor(hue)
    return hue


def mesh_polygroup_hues(mesh):
    """The hues of the polygroups a mesh shows (one when it has none)."""
    import numpy as np
    attr = mesh.attributes.get(".sculpt_face_set")
    ids = [1]
    if attr is not None and attr.domain == 'FACE' and len(attr.data):
        values = np.empty(len(attr.data), dtype=np.int32)
        attr.data.foreach_get("value", values)
        ids = [int(i) for i in np.unique(values)]
    return [polygroup_hue(i, mesh.face_sets_color_default, mesh.face_sets_color_seed) for i in ids]


def distinct_seed(ids, avoid_hues, default_face_set=0):
    """A color seed for polygroups `ids` (none of them the default) whose hues are as far as can
    be from `avoid_hues`."""
    def score(seed):
        hues = [polygroup_hue(i, default_face_set, seed) for i in ids]
        return min(_hue_distance(h, a) for h in hues for a in avoid_hues)
    # Only the seed's last digit moves the hues.
    return max(range(10), key=score)


def _ui_scale():
    return bpy.context.preferences.system.ui_scale


def _masked_faces(mesh):
    """One bool per face: all its corners masked (at least half) and not hidden. None without a
    mask on the mesh itself."""
    import numpy as np
    mask_attr = mesh.attributes.get(".sculpt_mask")
    if mask_attr is None or mask_attr.domain != 'POINT':
        return None
    masks = np.empty(len(mesh.vertices), dtype=np.float32)
    mask_attr.data.foreach_get("value", masks)
    corner_verts = np.empty(len(mesh.loops), dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", corner_verts)
    starts = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("loop_start", starts)
    unmasked = (masks[corner_verts] < MASK_THRESHOLD).astype(np.int32)
    keep = np.add.reduceat(unmasked, starts) == 0 if len(starts) else np.zeros(0, dtype=bool)
    hide = mesh.attributes.get(".hide_poly")
    if hide is not None:
        hidden = np.empty(len(mesh.polygons), dtype=bool)
        hide.data.foreach_get("value", hidden)
        keep &= ~hidden
    return keep


def _extracted_mesh(source, keep):
    """A copy of `source` with only the faces in `keep`, without mask, polygroups or hiding, and
    with its edge band in the `EDGE_GROUP` weights (returned as {vertex: weight})."""
    from itertools import compress
    mesh = source.copy()
    mesh.name = source.name + " Extract"
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    bmesh.ops.delete(bm, geom=list(compress(bm.faces, (not k for k in keep))), context='FACES')
    for elems in (bm.verts, bm.edges, bm.faces):
        for elem in elems:
            elem.hide = False
    # The edge band: the boundary and a few rings in, fading.
    bm.verts.index_update()
    weights = {}
    ring = [v for v in bm.verts if v.is_boundary]
    for weight in EDGE_RINGS:
        nxt = []
        for v in ring:
            if v.index not in weights:
                weights[v.index] = weight
                nxt += [e.other_vert(v) for e in v.link_edges]
        ring = nxt
    bm.to_mesh(mesh)
    bm.free()
    for name in (".sculpt_mask", ".sculpt_face_set"):
        attr = mesh.attributes.get(name)
        if attr is not None:
            mesh.attributes.remove(attr)
    mesh.update()
    return mesh, weights


# -----------------------------------------------------------------------------
# The panel

class _Layout:
    """Where the panel's parts are, in region pixels."""

    def __init__(self, area, region):
        s = _ui_scale()
        try:
            from phantom_mask_bar import _bottom_inset
            bottom = _bottom_inset(area, region)
        except (ImportError, AttributeError):
            bottom = 0
        w = PANEL_W * s
        pad = PAD * s
        row = ROW_H * s
        h = pad + row + len(SLIDERS) * row + pad / 2 + BUTTON_H * s + pad
        x = (region.width - w) / 2
        y = bottom + BAR_ROOM * s
        self.panel = (x, y, w, h)
        self.title = (x + pad, y + h - pad - row / 2)
        self.sliders = {}
        top = y + h - pad - row
        for i, slider in enumerate(SLIDERS):
            cy = top - row * (i + 0.5)
            track_x0 = x + pad + LABEL_W * s + KNOB_R * s
            track_x1 = x + w - pad - VALUE_W * s - KNOB_R * s - 4 * s
            self.sliders[slider.key] = {
                "label": (x + pad, cy),
                "track": (track_x0, track_x1, cy),
                "value": (x + w - pad - 4 * s, cy),
                # Clicking the number types a value; the rest of the row drags.
                "field": (x + w - pad - VALUE_W * s, cy - row / 2 + 2 * s, VALUE_W * s, row - 4 * s),
                "hit": (x + pad, cy - row / 2, w - 2 * pad - VALUE_W * s, row),
            }
        bw = (w - 3 * pad) / 2
        self.buttons = {
            "ACCEPT": (x + pad, y + pad, bw, BUTTON_H * s),
            "CANCEL": (x + 2 * pad + bw, y + pad, bw, BUTTON_H * s),
        }


def _hit(rect, mx, my):
    x, y, w, h = rect
    return x <= mx <= x + w and y <= my <= y + h


def _rounded_rect(x, y, w, h, r, segments=6):
    r = min(r, w / 2, h / 2)
    corners = ((x + w - r, y + h - r, 0.0), (x + r, y + h - r, 0.5 * math.pi),
               (x + r, y + r, math.pi), (x + w - r, y + r, 1.5 * math.pi))
    outline = []
    for cx, cy, start in corners:
        for i in range(segments + 1):
            a = start + 0.5 * math.pi * i / segments
            outline.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    center = (x + w / 2, y + h / 2)
    tris = []
    for i in range(len(outline)):
        tris += (center, outline[i], outline[(i + 1) % len(outline)])
    return tris, outline


def _rect(x, y, w, h):
    return [(x, y), (x + w, y), (x + w, y + h), (x, y), (x + w, y + h), (x, y + h)]


def _shape(shader, kind, coords, color, alpha):
    if kind == 'LINE_LOOP':
        # Closed by hand: the GPU module doesn't draw a loop's closing segment.
        kind, coords = 'LINE_STRIP', list(coords) + [coords[0]]
    gpu.state.blend_set('ALPHA')
    batch = batch_for_shader(shader, kind, {"pos": coords})
    shader.uniform_float("color", (*color[:3], color[3] * alpha))
    batch.draw(shader)


def _text(text, x, y, align, size, color, alpha):
    blf.size(0, size)
    w, _h = blf.dimensions(0, text)
    cap = blf.dimensions(0, "H")[1]
    x -= {'LEFT': 0, 'CENTER': w / 2, 'RIGHT': w}[align]
    blf.position(0, round(x), round(y - cap / 2), 0)
    blf.color(0, *color[:3], color[3] * alpha)
    blf.draw(0, text)


def _draw(op):
    context = bpy.context
    if context.area != op.area or context.region is None or context.region.type != 'WINDOW':
        return
    s = _ui_scale()
    alpha = min(1.0, (time.perf_counter() - op.opened) / FADE_TIME)
    lay = _Layout(context.area, context.region)
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    tris, outline = _rounded_rect(*lay.panel, CORNER * s)
    _shape(shader, 'TRIS', tris, PANEL_COLOR, alpha)
    _shape(shader, 'LINE_LOOP', outline, OUTLINE_COLOR, alpha)
    _text("Extract", *lay.title, 'LEFT', TITLE_SIZE * s, TEXT_COLOR, alpha)
    for slider in SLIDERS:
        key, lo, hi = slider.key, slider.min, slider.max
        parts = lay.sliders[key]
        value = op.values[key]
        active = key in {op.hover, op.dragging, op.editing}
        _text(slider.label, *parts["label"], 'LEFT', TEXT_SIZE * s,
              TEXT_COLOR if active else DIM_TEXT_COLOR, alpha)
        x0, x1, cy = parts["track"]
        # Typed values can be past the ends: the knob stops there.
        t = min(max((value - lo) / (hi - lo), 0.0), 1.0)
        th = TRACK_H * s
        _shape(shader, 'TRIS', _rounded_rect(x0, cy - th / 2, x1 - x0, th, th / 2)[0],
               TRACK_COLOR, alpha)
        # Filled from "nothing applied" in the middle, so each shows which way it goes.
        neutral = (slider.neutral - lo) / (hi - lo)
        a, b = sorted((x0 + (x1 - x0) * neutral, x0 + (x1 - x0) * t))
        if b - a > 0.5:
            _shape(shader, 'TRIS', _rounded_rect(a, cy - th / 2, b - a, th, th / 2)[0],
                   FILL_COLOR, alpha)
        kx = x0 + (x1 - x0) * t
        knob = [(kx + KNOB_R * s * math.cos(2 * math.pi * i / 16),
                 cy + KNOB_R * s * math.sin(2 * math.pi * i / 16)) for i in range(16)]
        fan = []
        for i in range(16):
            fan += ((kx, cy), knob[i], knob[(i + 1) % 16])
        _shape(shader, 'TRIS', fan, KNOB_COLOR, alpha)
        fx, fy, fw, fh = parts["field"]
        tris, outline = _rounded_rect(fx, fy, fw, fh, 3 * s)
        if op.editing == key:
            _shape(shader, 'TRIS', tris, FIELD_COLOR, alpha)
            _shape(shader, 'LINE_LOOP', outline, FIELD_OUTLINE_COLOR, alpha)
            text = op.edit_text + ("" if op.edit_fresh else "|")
            if op.edit_fresh:
                # All selected: typing replaces it.
                blf.size(0, TEXT_SIZE * s)
                tw = blf.dimensions(0, text)[0]
                vx, vy = parts["value"]
                _shape(shader, 'TRIS', _rect(vx - tw - 1 * s, fy + 3 * s, tw + 2 * s, fh - 6 * s),
                       (0.55, 0.55, 1.0, 0.45), alpha)
            _text(text, *parts["value"], 'RIGHT', TEXT_SIZE * s, TEXT_COLOR, alpha)
        else:
            _shape(shader, 'TRIS', tris, FIELD_IDLE_COLOR, alpha)
            _shape(shader, 'LINE_LOOP', outline, FIELD_HOVER_OUTLINE_COLOR
                   if op.hover == ("FIELD", key) else FIELD_IDLE_OUTLINE_COLOR, alpha)
            _text(slider.fmt.format(value), *parts["value"], 'RIGHT', TEXT_SIZE * s,
                  TEXT_COLOR if op.hover == ("FIELD", key) else DIM_TEXT_COLOR if
                  abs(value - slider.neutral) < 1e-9 else TEXT_COLOR, alpha)
    for identifier, label, color, hover_color in (
            ("ACCEPT", "Accept", ACCEPT_COLOR, ACCEPT_HOVER_COLOR),
            ("CANCEL", "Cancel", CANCEL_COLOR, CANCEL_HOVER_COLOR)):
        x, y, w, h = lay.buttons[identifier]
        _shape(shader, 'TRIS', _rounded_rect(x, y, w, h, 4 * s)[0],
               hover_color if op.hover == identifier else color, alpha)
        _text(label, x + w / 2, y + h / 2, 'CENTER', TEXT_SIZE * s, TEXT_COLOR, alpha)
    gpu.state.blend_set('NONE')


# -----------------------------------------------------------------------------
# The operator

class SCULPT_OT_phantom_extract(Operator):
    """Make a new object from the masked area and shape it (scale, inflate, thickness, """ \
        """smoothness) before keeping it"""
    bl_idname = "sculpt.phantom_extract"
    bl_label = "Extract"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return context.mode == 'SCULPT' and ob is not None and ob.type == 'MESH'

    def invoke(self, context, event):
        source = context.active_object
        if source.use_dynamic_topology_sculpting:
            self.report({'WARNING'}, "Turn off Dyntopo to extract")
            return {'CANCELLED'}
        if any(m.type == 'MULTIRES' and m.show_viewport for m in source.modifiers):
            self.report({'WARNING'}, "Extract works on the mesh's own mask, not with Multires yet")
            return {'CANCELLED'}
        keep = _masked_faces(source.data)
        if keep is None or not keep.any():
            self.report({'WARNING'}, "Mask part of the mesh first")
            return {'CANCELLED'}

        mesh, weights = _extracted_mesh(source.data, keep)
        # Its origin in the middle of what was extracted, so Scale grows it in place.
        center = sum((v.co for v in mesh.vertices), Vector()) / len(mesh.vertices)
        mesh.transform(Matrix.Translation(-center))
        ob = bpy.data.objects.new(source.name + " Extract", mesh)
        for collection in source.users_collection:
            collection.objects.link(ob)
        group = ob.vertex_groups.new(name=EDGE_GROUP)
        for index, weight in weights.items():
            group.add([index], weight, 'REPLACE')
        smooth = ob.modifiers.new(MOD_SMOOTH, 'SMOOTH')
        smooth.vertex_group = EDGE_GROUP
        smooth.factor = 0.5
        inflate = ob.modifiers.new(MOD_INFLATE, 'DISPLACE')
        inflate.direction = 'NORMAL'
        inflate.mid_level = 0.0
        thickness = ob.modifiers.new(MOD_THICKNESS, 'SOLIDIFY')
        # The extracted surface stays on the sculpt; a positive thickness grows the shell outward.
        thickness.offset = 1.0
        thickness.use_even_offset = True
        thickness.use_rim = True
        # Solidify tells its faces apart only by material: the shell's are moved up by the
        # number of slots and the rim's by twice that (it needs the slots to exist). Accept turns
        # that into polygroups and puts the materials back.
        self.material_slots = len(mesh.materials)
        self.slot_step = max(self.material_slots, 1)
        while len(mesh.materials) < 3 * self.slot_step:
            mesh.materials.append(None)
        thickness.material_offset = self.slot_step
        thickness.material_offset_rim = 2 * self.slot_step

        self.source_name = source.name
        self.object_name = ob.name
        self.base_matrix = source.matrix_world @ Matrix.Translation(center)
        # Sizes in the object's own units: inflate and thickness follow the masked mesh's size.
        scale = max(source.matrix_world.to_scale())
        self.size = max(max(source.dimensions), 1e-6) / max(scale, 1e-6)
        self.values = {slider.key: slider.neutral for slider in SLIDERS}
        self._apply_values()

        self.area = context.area
        self.hover = None
        self.dragging = None
        # The value being typed: its slider, the text so far, and whether it is still all
        # selected (the first key replaces it).
        self.editing = None
        self.edit_text = ""
        self.edit_fresh = False
        self.opened = time.perf_counter()
        self.handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (self,), 'WINDOW', 'POST_PIXEL')
        self.timer = context.window_manager.event_timer_add(1.0 / 60.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _object(self):
        return bpy.data.objects.get(self.object_name)

    def _apply_values(self):
        ob = self._object()
        if ob is None:
            return
        v = self.values
        ob.matrix_world = self.base_matrix @ Matrix.Scale(v["scale"], 4)
        # Smoothing below zero sharpens the edge instead.
        iterations = round(abs(v["smoothness"]) * SMOOTH_ITERATIONS)
        smooth = ob.modifiers[MOD_SMOOTH]
        smooth.iterations = max(iterations, 1)
        smooth.factor = 0.5 if v["smoothness"] >= 0.0 else -0.5
        smooth.show_viewport = iterations > 0
        ob.modifiers[MOD_INFLATE].strength = v["inflate"] * INFLATE_AT_ONE * self.size
        # Thickness below zero grows the shell inward; 0 is a single-sided surface.
        thickness = ob.modifiers[MOD_THICKNESS]
        thickness.thickness = v["thickness"] * THICKNESS_AT_ONE * self.size
        thickness.show_viewport = v["thickness"] != 0.0

    def _set_from_mouse(self, key, mx):
        lay = _Layout(self.area, self._region())
        x0, x1, _cy = lay.sliders[key]["track"]
        slider = SLIDER[key]
        t = min(max((mx - x0) / (x1 - x0), 0.0), 1.0)
        neutral_x = x0 + (x1 - x0) * (slider.neutral - slider.min) / (slider.max - slider.min)
        if abs(mx - neutral_x) <= SNAP * _ui_scale():
            # Near the middle: exactly "nothing applied" (thickness 0 is single-sided only then).
            self.values[key] = slider.neutral
        else:
            self.values[key] = slider.min + (slider.max - slider.min) * t
        self._apply_values()

    def _start_editing(self, key):
        self.editing = key
        self.edit_text = "{:g}".format(round(self.values[key], 4))
        self.edit_fresh = True

    def _finish_editing(self, apply):
        """Use the typed value (when it reads as a number), and stop typing."""
        key = self.editing
        self.editing = None
        if not apply or key is None:
            return
        try:
            value = float(self.edit_text.replace(",", "."))
        except ValueError:
            return
        if not math.isfinite(value):
            return
        slider = SLIDER[key]
        self.values[key] = min(max(value, slider.typed_min), slider.typed_max)
        self._apply_values()

    def _type(self, context, event):
        """A key while typing a value. Every key goes to the field, none to sculpting."""
        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}
        if event.type in {'RET', 'NUMPAD_ENTER'}:
            self._finish_editing(True)
        elif event.type == 'ESC':
            self._finish_editing(False)
        elif event.type == 'TAB':
            # On to the next slider's value (Shift: the one before).
            keys = [slider.key for slider in SLIDERS]
            step = -1 if event.shift else 1
            nxt = keys[(keys.index(self.editing) + step) % len(keys)]
            self._finish_editing(True)
            self._start_editing(nxt)
        elif event.type == 'BACK_SPACE':
            self.edit_text = "" if self.edit_fresh else self.edit_text[:-1]
            self.edit_fresh = False
        elif event.type == 'DEL':
            self.edit_text = ""
            self.edit_fresh = False
        elif event.type == 'V' and (event.ctrl or event.oskey):
            self.edit_text = ("" if self.edit_fresh else self.edit_text) + \
                context.window_manager.clipboard.strip()[:16]
            self.edit_fresh = False
        elif event.unicode and event.unicode in "0123456789.,-+" and not (event.ctrl or event.oskey):
            if self.edit_fresh:
                self.edit_text = ""
            self.edit_fresh = False
            if len(self.edit_text) < 16:
                self.edit_text += event.unicode
        self.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _region(self):
        return next(r for r in self.area.regions if r.type == 'WINDOW')

    def _end(self, context):
        bpy.types.SpaceView3D.draw_handler_remove(self.handle, 'WINDOW')
        context.window_manager.event_timer_remove(self.timer)
        self.area.tag_redraw()

    def _accept(self, context):
        """Bake the modifiers and the scale into a plain mesh, ready to sculpt."""
        ob = self._object()
        self._end(context)
        if ob is None:
            return {'CANCELLED'}
        depsgraph = context.evaluated_depsgraph_get()
        mesh = bpy.data.meshes.new_from_object(ob.evaluated_get(depsgraph), depsgraph=depsgraph)
        mesh.transform(Matrix.Scale(self.values["scale"], 4))
        self._set_polygroups(mesh)
        old = ob.data
        ob.modifiers.clear()
        ob.vertex_groups.clear()
        ob.data = mesh
        mesh.name = old.name
        bpy.data.meshes.remove(old)
        ob.matrix_world = self.base_matrix
        # Undo must take the new object away, and only steps holding the whole scene do that (the
        # sculpt steps before don't). So: an undo step of the scene without it, then (when the
        # operator finishes) the one with it. Cancel leaves no step behind this way.
        collections = list(ob.users_collection)
        for collection in collections:
            collection.objects.unlink(ob)
        bpy.ops.ed.undo_push(message="Before Extract")
        for collection in collections:
            collection.objects.link(ob)
        _play("confirm")
        self.report({'INFO'}, f"Extracted \"{ob.name}\"")
        return {'FINISHED'}

    def _set_polygroups(self, mesh):
        """Polygroups for the baked mesh: one, or with a thickness its outside, inside and rim
        (from the material offsets the shell and rim got), and its own materials back."""
        import numpy as np
        faces = len(mesh.polygons)
        materials = np.zeros(faces, dtype=np.int32)
        attr = mesh.attributes.get("material_index")
        if attr is not None:
            attr.data.foreach_get("value", materials)
        part = materials // self.slot_step
        polygroups = np.full(faces, POLYGROUP_OUTSIDE, dtype=np.int32)
        thickness = self.values["thickness"]
        if thickness != 0.0:
            # Solidify gives its shell material to the inner layer (behind the surface's normals,
            # toward the sculpt), whichever way it grows.
            polygroups[part == 1] = POLYGROUP_INSIDE
            polygroups[part == 2] = POLYGROUP_RIM
        face_sets = mesh.attributes.get(".sculpt_face_set")
        if face_sets is None:
            face_sets = mesh.attributes.new(".sculpt_face_set", 'INT', 'FACE')
        face_sets.data.foreach_set("value", polygroups)
        # Colors of their own: none is the default (scarlet) polygroup, and their hues keep away
        # from the masked mesh's.
        source = bpy.data.objects.get(self.source_name)
        avoid = mesh_polygroup_hues(source.data) if source is not None else [DEFAULT_HUE]
        mesh.face_sets_color_default = 0
        mesh.face_sets_color_seed = distinct_seed(sorted(set(polygroups.tolist())), avoid)
        if attr is not None:
            attr.data.foreach_set("value", materials % self.slot_step)
        while len(mesh.materials) > self.material_slots:
            mesh.materials.pop()
        mesh.update()

    def _cancel(self, context):
        ob = self._object()
        self._end(context)
        if ob is not None:
            mesh = ob.data
            bpy.data.objects.remove(ob)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        return {'CANCELLED'}

    def modal(self, context, event):
        source = bpy.data.objects.get(self.source_name)
        if (self._object() is None or source is None or source.mode != 'SCULPT' or
                context.view_layer.objects.active != source):
            # The masked mesh is no longer sculpted (another object, mode or file): drop it.
            return self._cancel(context)
        if event.type == 'TIMER':
            if time.perf_counter() - self.opened < FADE_TIME * 2:
                self.area.tag_redraw()
            return {'PASS_THROUGH'}
        # Typing a value: keys go to it wherever the mouse is (clicks and navigation still work).
        if self.editing is not None and event.type not in {
                'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'LEFTMOUSE', 'MIDDLEMOUSE', 'RIGHTMOUSE',
                'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
            return self._type(context, event)
        if context.area != self.area:
            return {'PASS_THROUGH'}
        region = self._region()
        mx, my = event.mouse_x - region.x, event.mouse_y - region.y
        lay = _Layout(self.area, region)

        if event.type == 'MOUSEMOVE':
            if self.dragging is not None:
                self._set_from_mouse(self.dragging, mx)
                self.area.tag_redraw()
                return {'RUNNING_MODAL'}
            hover = next((k for k, r in lay.buttons.items() if _hit(r, mx, my)), None)
            if hover is None:
                hover = next((("FIELD", k) for k, p in lay.sliders.items()
                              if _hit(p["field"], mx, my)), None)
            if hover is None:
                hover = next((k for k, p in lay.sliders.items() if _hit(p["hit"], mx, my)), None)
            if hover != self.hover:
                self.hover = hover
                self.area.tag_redraw()
            return {'PASS_THROUGH'}

        if event.type == 'LEFTMOUSE':
            if event.value == 'RELEASE' and self.dragging is not None:
                self.dragging = None
                self.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value == 'PRESS':
                # A click anywhere else ends typing, keeping the typed value.
                clicked_field = next((k for k, p in lay.sliders.items()
                                      if _hit(p["field"], mx, my)), None)
                if self.editing is not None and clicked_field != self.editing:
                    self._finish_editing(True)
                if clicked_field is not None:
                    if self.editing != clicked_field:
                        self._start_editing(clicked_field)
                    self.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                for identifier, rect in lay.buttons.items():
                    if _hit(rect, mx, my):
                        return self._accept(context) if identifier == "ACCEPT" else self._cancel(context)
                for key, parts in lay.sliders.items():
                    if _hit(parts["hit"], mx, my):
                        self.dragging = key
                        self._set_from_mouse(key, mx)
                        self.area.tag_redraw()
                        return {'RUNNING_MODAL'}
                if _hit(lay.panel, mx, my):
                    return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}

        if event.value == 'PRESS':
            if event.type in {'RET', 'NUMPAD_ENTER'}:
                return self._accept(context)
            if event.type == 'ESC' or (event.type == 'Z' and (event.ctrl or event.oskey)):
                # Undo while extracting takes back the extraction itself.
                return self._cancel(context)
        return {'PASS_THROUGH'}


def _play(name):
    try:
        from phantom_mask_bar import _play_sound
        _play_sound(name)
    except (ImportError, AttributeError):
        pass


classes = (
    SCULPT_OT_phantom_extract,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
