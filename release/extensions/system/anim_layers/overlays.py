# SPDX-FileCopyrightText: 2026 ohnsk
# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Seeing layers: other layers' curves in the Graph Editor, and the "keys go on" hint in the 3D View.

The Graph Editor shows and edits the active layer (the Action being tweaked). The layers with Show Curves on are
drawn behind it, dimmed in their colour; clicking one of their curves makes that layer active, so its curves become
the editable ones.

Based on Maya Workflow Kit for Blender by ohnsk (https://github.com/5hyuns/maya-workflow-kit-for-blender, commit
bd7ee04, ``src/allset/ui/graph_layers.py``: ``normalization``, ``polyline``, ``lines``, ``hit`` and the pick
operator). Modified for Phantom on 2026-10-28: layers are shown by their Show Curves toggle instead of a multiple
selection, curves are drawn in the layer's colour, the unit mapping reads the property's unit, the curves of
unselected bones are left out like the Graph Editor does, and a click goes to the layer only when its curve is
nearer than the active layer's.
"""

import math

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import bake, editing, model

ALPHA = 0.55            # the other layers' curves: see-through, so the active layer's own read first
WIDTH = 1.5             # their line width, in pixels at UI scale 1
PICK_PX = 8             # how near a click must be to a curve, in pixels at UI scale 1


# --- what is drawn -------------------------------------------------------------------------------------------------

def value_factor(scene, ob, fc):
    """The Graph Editor's display factor: rotations in degrees while the scene's rotation unit is degrees."""
    if scene.unit_settings.system_rotation != 'DEGREES':
        return 1.0
    owner_path, _, ident = fc.data_path.rpartition(".")
    try:
        owner = ob.path_resolve(owner_path) if owner_path else ob
    except ValueError:
        return 1.0
    prop = getattr(owner, "bl_rna", None) and owner.bl_rna.properties.get(ident)
    if prop is not None and getattr(prop, "unit", None) == 'ROTATION':
        return 180.0 / math.pi
    return 1.0


def normalization(fc):
    """(offset, factor) of View > Normalize: value' = (value + offset) * factor (keys and handles span -1..1)."""
    ys = []
    for kp in fc.keyframe_points:
        ys.append(kp.co.y)
        if kp.interpolation == 'BEZIER':
            ys += [kp.handle_left.y, kp.handle_right.y]
    if not ys:
        return 0.0, 1.0
    lo, hi = min(ys), max(ys)
    rng = hi - lo
    if rng <= 1.192e-7:
        return -lo, 1.0
    return -lo - rng / 2.0, 2.0 / rng


def polyline(fc, x0, x1, n, factor=1.0, offset=0.0):
    """``n`` + 1 points of the curve from frame ``x0`` to ``x1``, in Graph Editor view space."""
    n = max(1, int(n))
    step = (x1 - x0) / n
    return [(x0 + i * step, (fc.evaluate(x0 + i * step) + offset) * factor) for i in range(n + 1)]


def _bone_name(path):
    if not path.startswith('pose.bones["'):
        return None
    end = path.find('"]', 12)
    return bpy.utils.unescape_identifier(path[12:end]) if end > 0 else None


def _shown_curve(ob, fc, selected_bones):
    """Leave out hidden curves, and the curves of unselected bones when only selected ones are shown."""
    if fc.hide:
        return False
    if selected_bones is None:
        return True
    bone = _bone_name(fc.data_path)
    return bone is None or bone in selected_bones


def _selected_bones(ob, only_selected):
    if not only_selected or ob.type != 'ARMATURE' or ob.mode != 'POSE':
        return None
    return {pb.name for pb in ob.pose.bones if getattr(pb, "select", False)}


def curves_to_draw(scene, ob, active=False, only_selected=False):
    """[(layer, F-curve)] of ``ob``: its Show Curves layers other than the one it's tweaking (``active``: only the
    tweaked one, for picking)."""
    tweaked = editing.tweaked_layer_id(ob)
    bones = _selected_bones(ob, only_selected)
    out = []
    for lid, tr in model.layer_tracks(ob).items():
        layer = model.find(scene, lid)
        if layer is None:
            continue
        if active != (lid == tweaked) or (not active and not layer.show_curves):
            continue
        out += [(layer, fc) for fc in bake.curves(tr) if _shown_curve(ob, fc, bones) and len(fc.keyframe_points)]
    return out


def lines(scene, objects, x0, x1, n, normalize=False, active=False, only_selected=False):
    """[(rgba, object, layer id, points)] to draw (or, with ``active``, the active layer's curves to pick
    against), bottom layer first."""
    out = []
    order = {l.layer_id: i for i, l in enumerate(model.layers(scene))}
    for ob in objects:
        items = curves_to_draw(scene, ob, active, only_selected)
        items.sort(key=lambda item: order.get(item[0].layer_id, 0))
        for layer, fc in items:
            offset, factor = normalization(fc) if normalize else (0.0, value_factor(scene, ob, fc))
            rgba = (*layer.color, ALPHA)
            out.append((rgba, ob, layer.layer_id, polyline(fc, x0, x1, n, factor, offset)))
    return out


def shown_objects(context, space):
    """The layered objects the Graph Editor shows."""
    scene = context.scene
    if space.dopesheet.show_only_selected:
        obs = context.selected_objects
    else:
        obs = [ob for ob in scene.objects if ob.visible_get()]
    return [ob for ob in obs if model.is_layered(ob)]


def view_lines(context, region, space, active=False):
    v2d = region.view2d
    x0, _y = v2d.region_to_view(0, 0)
    x1, _y = v2d.region_to_view(region.width, 0)
    n = min(1500, max(16, region.width // 3))
    return lines(context.scene, shown_objects(context, space), x0, x1, n,
                 normalize=bool(space.use_normalization), active=active,
                 only_selected=space.dopesheet.show_only_selected)


def _shows(context):
    space = context.space_data
    scene = context.scene
    return (space is not None and space.type == 'GRAPH_EDITOR' and space.mode == 'FCURVES'
            and len(getattr(scene, "phantom_anim_layers", ())) > 1
            and any(l.show_curves for l in scene.phantom_anim_layers))


def draw_graph():
    context = bpy.context
    region = context.region
    if region is None or region.type != 'WINDOW':
        return
    try:
        if not _shows(context):
            return
        items = view_lines(context, region, context.space_data)
    except (AttributeError, ReferenceError):
        return
    if not items:
        return
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
    shader.uniform_float("lineWidth", WIDTH * context.preferences.system.ui_scale)
    for rgba, _ob, _lid, pts in items:
        batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": pts})
        shader.uniform_float("color", rgba)
        batch.draw(shader)
    gpu.state.blend_set('NONE')


# --- picking -------------------------------------------------------------------------------------------------------

def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    d2 = dx * dx + dy * dy
    t = 0.0 if d2 <= 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / d2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def nearest(items, to_region, mx, my):
    """(distance in region pixels, object, layer id) of the drawn curve nearest the mouse, or None."""
    best = None
    for _rgba, ob, lid, pts in items:
        prev = None
        for p in pts:
            q = to_region(*p)
            if prev is not None:
                d = _seg_dist(mx, my, prev[0], prev[1], q[0], q[1])
                if best is None or d < best[0]:
                    best = (d, ob, lid)
            prev = q
    return best


def hit(items, to_region, mx, my, limit=PICK_PX, active_items=()):
    """(object, layer id) of the other layer's curve under the mouse: within ``limit`` pixels and nearer than any
    of the active layer's curves (``active_items``), so clicks on the editable curves still reach them."""
    got = nearest(items, to_region, mx, my)
    if got is None or got[0] > limit:
        return None
    mine = nearest(active_items, to_region, mx, my)
    if mine is not None and mine[0] <= got[0]:
        return None
    return got[1], got[2]


class ANIM_LAYERS_OT_graph_pick(bpy.types.Operator):
    """Click another layer's curve in the Graph Editor to make that layer active (its curves become editable)"""
    bl_idname = "anim_layers.graph_pick"
    bl_label = "Pick Layer Curve"
    bl_options = {'INTERNAL', 'UNDO'}

    @classmethod
    def poll(cls, context):
        region = context.region
        return region is not None and region.type == 'WINDOW' and _shows(context)

    def invoke(self, context, event):
        try:
            region = context.region
            space = context.space_data
            v2d = region.view2d

            def to_region(x, y):
                return v2d.view_to_region(x, y, clip=False)
            limit = PICK_PX * context.preferences.system.ui_scale
            got = hit(view_lines(context, region, space), to_region, event.mouse_region_x, event.mouse_region_y,
                      limit, view_lines(context, region, space, active=True))
        except (AttributeError, ReferenceError):
            return {'PASS_THROUGH'}
        if got is None:
            return {'PASS_THROUGH'}
        scene = context.scene
        scene.phantom_anim_layer_index = model.index_of(scene, got[1])
        return {'FINISHED'}


# --- the 3D View hint ----------------------------------------------------------------------------------------------

def hint_text(scene, ob):
    """(text, layer colour) of the 3D View hint for the active object, or None."""
    if ob is None or len(getattr(scene, "phantom_anim_layers", ())) < 2:
        return None
    if not model.is_layered(ob) or not editing.supported():
        return None
    from .ui import key_hint
    hint = key_hint(scene, ob)
    if hint is None:
        return None
    lid = editing.tweaked_layer_id(ob)
    layer = model.find(scene, lid) if lid else None
    color = tuple(layer.color) if layer is not None else (0.8, 0.8, 0.8)
    return hint[0], color


def draw_hint():
    context = bpy.context
    scene = context.scene
    space = context.space_data
    region = context.region
    if (region is None or space is None or not getattr(scene, "phantom_anim_layers_show_hint", False)
            or not space.overlay.show_overlays or not space.overlay.show_text
            or context.mode not in {'OBJECT', 'POSE'}):
        return
    got = hint_text(scene, context.active_object)
    if got is None:
        return
    import blf
    text, color = got
    scale = context.preferences.system.ui_scale
    x = 20 * scale
    for r in context.area.regions:      # step past the toolbar when it overlaps the view
        if r.type == 'TOOLS' and r.width > 1 and context.preferences.system.use_region_overlap:
            x += r.width
    font = 0
    blf.size(font, 12 * scale)
    blf.color(font, *color, 1.0)
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.8)
    blf.position(font, x, 24 * scale, 0)
    blf.draw(font, text)
    blf.disable(font, blf.SHADOW)


# --- registration ----------------------------------------------------------------------------------------------------

classes = (ANIM_LAYERS_OT_graph_pick,)
_handles = []
_keymaps = []


def register():
    _handles.append((bpy.types.SpaceGraphEditor, bpy.types.SpaceGraphEditor.draw_handler_add(
        draw_graph, (), 'WINDOW', 'POST_VIEW')))
    _handles.append((bpy.types.SpaceView3D, bpy.types.SpaceView3D.draw_handler_add(
        draw_hint, (), 'WINDOW', 'POST_PIXEL')))
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is not None:          # none in background mode
        km = kc.keymaps.new(name="Graph Editor", space_type='GRAPH_EDITOR')
        _keymaps.append((km, km.keymap_items.new(ANIM_LAYERS_OT_graph_pick.bl_idname, 'LEFTMOUSE', 'PRESS')))


def unregister():
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()
    for space, handle in _handles:
        space.draw_handler_remove(handle, 'WINDOW')
    _handles.clear()
