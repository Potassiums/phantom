# SPDX-FileCopyrightText: 2026 ohnsk
# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
The layer list and each object's NLA tracks.

Based on Maya Workflow Kit for Blender by ohnsk (https://github.com/5hyuns/maya-workflow-kit-for-blender,
commit bd7ee04, ``src/allset/operators/anim_layers.py``). Modified for Phantom on 2026-10-28: layers are found by
a stable id instead of their name, strips don't sync their length, the strip window is far wider, tracks are
inserted in place instead of rebuilt, and editing goes through NLA tweak mode (``editing.py``).

Layers belong to the scene (``Scene.phantom_anim_layers``, bottom first, Base always first). An object in a layer
has one NLA track for it: one strip over ``WIDE`` at 1:1 time, playing that layer's own Action, which carries the
layer's id in its ``MARK`` property. Base is the object's original animation on the bottom track (Replace). An Add
layer is a Combine strip, an Override layer a Replace strip, and the layer's weight is the strip's influence.
"""

import uuid

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, StringProperty

BASE_ID = "base"
BASE_NAME = "Base"
MARK = "phantom_anim_layer"          # Action ID property: the id of the layer it belongs to
OWNER = "phantom_anim_layer_owner"   # Action ID property: the name of the object it animates (see editing.fix_copies)
WIDE = (-100000.0, 100000.0)         # every layer strip covers this, so keys anywhere in it play
BASE_COLOR = (0.6, 0.6, 0.6)
PALETTE = ((0.95, 0.55, 0.2), (0.25, 0.75, 0.75), (0.9, 0.4, 0.65), (0.5, 0.8, 0.3), (0.4, 0.55, 0.95),
           (0.95, 0.8, 0.25), (0.65, 0.45, 0.9), (0.9, 0.3, 0.3))


# --- the scene's layer list ------------------------------------------------------------------------------------

def layers(scene):
    return scene.phantom_anim_layers


def find(scene, layer_id):
    return next((l for l in layers(scene) if l.layer_id == layer_id), None)


def index_of(scene, layer_id):
    return next((i for i, l in enumerate(layers(scene)) if l.layer_id == layer_id), None)


def active(scene):
    ls = layers(scene)
    i = scene.phantom_anim_layer_index
    return ls[i] if 0 <= i < len(ls) else None


def ensure_list(scene):
    """The layer list, with Base first."""
    ls = layers(scene)
    if not len(ls) or ls[0].layer_id != BASE_ID:
        base = ls.add()
        base.layer_id = BASE_ID
        base.name = BASE_NAME
        base.color = BASE_COLOR
        ls.move(len(ls) - 1, 0)
    return ls


def next_color(scene):
    """The first palette colour no layer has yet (then round the palette again)."""
    used = {tuple(round(c, 3) for c in l.color) for l in layers(scene)}
    for color in PALETTE:
        if tuple(round(c, 3) for c in color) not in used:
            return color
    return PALETTE[(len(layers(scene)) - 1) % len(PALETTE)]


def unique_name(scene, stem="Layer"):
    """``Layer N``, one past the highest number used."""
    taken = [l.name[len(stem):].strip() for l in layers(scene) if l.name.startswith(stem)]
    numbers = [int(t) for t in taken if t.isdigit()]
    return f"{stem} {max(numbers, default=0) + 1}"


def new_layer(scene, name=None):
    """A new layer on top of the stack (no objects yet)."""
    ls = ensure_list(scene)
    color = next_color(scene)
    layer = ls.add()
    layer.layer_id = uuid.uuid4().hex[:12]
    layer.name = name or unique_name(scene)
    layer.color = color
    return layer


# --- an object's tracks ------------------------------------------------------------------------------------------

def layer_tracks(ob):
    """{layer id: NLA track} of ``ob``."""
    ad = getattr(ob, "animation_data", None)
    if ad is None:
        return {}
    out = {}
    for tr in ad.nla_tracks:
        if len(tr.strips) == 1 and tr.strips[0].action is not None:
            lid = tr.strips[0].action.get(MARK)
            if lid:
                out[lid] = tr
    return out


def is_layered(ob):
    return BASE_ID in layer_tracks(ob)


def layered_objects(scene):
    return [ob for ob in scene.objects if is_layered(ob)]


def members(scene, layer_id):
    return [ob for ob in scene.objects if layer_id in layer_tracks(ob)]


def _new_action(ob, layer):
    act = bpy.data.actions.new(f"{layer.name}_{ob.name}")
    act[MARK] = layer.layer_id
    act[OWNER] = ob.name
    slot = act.slots.new(id_type=ob.id_type, name=ob.name)
    return act, slot


def _add_track(ob, layer, act, slot, after=None):
    """A track for ``layer`` playing ``act``, right above ``after`` (on top when None)."""
    ad = ob.animation_data_create()
    tr = ad.nla_tracks.new(prev=after)
    tr.name = layer.name
    st = tr.strips.new(layer.name, int(WIDE[0]), act)
    if slot is not None:
        st.action_slot = slot
    st.use_sync_length = False          # leaving tweak mode would otherwise shrink the strip to its keys
    st.use_auto_blend = False
    st.blend_in = st.blend_out = 0.0
    st.action_frame_start, st.action_frame_end = WIDE
    st.frame_start, st.frame_end = WIDE
    base = layer.layer_id == BASE_ID
    st.extrapolation = 'HOLD' if base else 'HOLD_FORWARD'
    st.blend_type = 'REPLACE' if base or layer.blend == 'OVERRIDE' else 'COMBINE'
    return tr


def _track_below(scene, ob, layer_id):
    """The object's track of the nearest layer below ``layer_id`` (Base at worst)."""
    have = layer_tracks(ob)
    below = None
    for l in layers(scene):
        if l.layer_id == layer_id:
            break
        if l.layer_id in have:
            below = have[l.layer_id]
    return below


def ensure_base(scene, ob):
    """Move the object's own animation onto its Base track. Call with the object out of tweak mode."""
    have = layer_tracks(ob)
    if BASE_ID in have:
        return have[BASE_ID]
    base = ensure_list(scene)[0]
    ad = ob.animation_data_create()
    act, slot = ad.action, ad.action_slot
    if act is None:
        act, slot = _new_action(ob, base)
    else:
        other_users = act.users - 1 - (1 if act.use_fake_user else 0)
        if other_users > 0 or act.get(MARK) is not None:
            # Shared with other objects (or another object's layer): Base gets a copy, the others keep theirs.
            act = act.copy()
            if slot is not None:
                slot = next((s for s in act.slots if s.identifier == slot.identifier), None)
        act[MARK] = BASE_ID
        act[OWNER] = ob.name
        ad.action = None
    return _add_track(ob, base, act, slot)


def add_object(scene, ob, layer):
    """Put ``ob`` in ``layer`` (and on Base). Call with the object out of tweak mode."""
    ensure_base(scene, ob)
    if layer.layer_id == BASE_ID or layer.layer_id in layer_tracks(ob):
        return
    act, slot = _new_action(ob, layer)
    _add_track(ob, layer, act, slot, after=_track_below(scene, ob, layer.layer_id))


def remove_object(ob, layer_id):
    """Take ``ob`` out of a layer, deleting its keys on it. Base stays. Call with the object out of tweak
    mode."""
    tr = layer_tracks(ob).get(layer_id)
    if tr is None or layer_id == BASE_ID:
        return
    act = tr.strips[0].action
    ob.animation_data.nla_tracks.remove(tr)
    if act is not None and act.users == 0:
        bpy.data.actions.remove(act)


def unlayer(ob):
    """Take ``ob`` out of the layers: its layer tracks go, and Base becomes its plain Action again. Call with the
    object out of tweak mode."""
    have = layer_tracks(ob)
    for lid in list(have):
        if lid != BASE_ID:
            remove_object(ob, lid)
    base = layer_tracks(ob).get(BASE_ID)
    if base is None:
        return
    st = base.strips[0]
    act, slot = st.action, st.action_slot
    ad = ob.animation_data
    ad.action = act
    if slot is not None:
        ad.action_slot = slot
    ad.nla_tracks.remove(base)
    for key in (MARK, OWNER):
        if key in act:
            del act[key]


def _insert_above(scene, src_id, name):
    """A new list entry right above layer ``src_id``. Adding to the list moves its entries in memory, so callers
    look entries up again by id afterwards."""
    ls = layers(scene)
    color = next_color(scene)
    entry = ls.add()
    entry.layer_id = uuid.uuid4().hex[:12]
    entry.name = name
    entry.color = color
    new_id = entry.layer_id
    ls.move(len(ls) - 1, index_of(scene, src_id) + 1)
    return find(scene, new_id)


def _copy_name(scene, name, suffix):
    names = {l.name for l in layers(scene)}
    stem = f"{name} {suffix}"
    if stem not in names:
        return stem
    n = 2
    while f"{stem} {n}" in names:
        n += 1
    return f"{stem} {n}"


def duplicate_layer(scene, layer_id, with_keys=True):
    """A copy of a layer right above it: the same objects, blend and weight, with copies of its keys (and keyed
    weight) or empty. Call with the members out of tweak mode. Returns the new layer's id."""
    src = find(scene, layer_id)
    blend_mode, w, name = src.blend, src.weight, src.name
    new = _insert_above(scene, layer_id, _copy_name(scene, name, "copy"))
    new.blend, new.weight = blend_mode, w
    new_id = new.layer_id
    for ob in members(scene, layer_id):
        tr = layer_tracks(ob)[layer_id]
        new = find(scene, new_id)
        if with_keys:
            act = tr.strips[0].action.copy()
            act[MARK] = new_id
            act[OWNER] = ob.name
            act.name = f"{new.name}_{ob.name}"
            old_slot = tr.strips[0].action_slot
            slot = next((s for s in act.slots if old_slot and s.identifier == old_slot.identifier), None)
        else:
            act, slot = _new_action(ob, new)
        state = strip_state(tr)
        new_tr = _add_track(ob, new, act, slot, after=tr)
        st = new_tr.strips[0]
        if with_keys and state["keys"]:
            fc = weight_curve(st)
            for co, hl, hr, interp, tl, trt in state["keys"]:
                kp = fc.keyframe_points.insert(co[0], co[1], options={'FAST'})
                kp.interpolation = interp
                kp.handle_left_type, kp.handle_right_type = tl, trt
                kp.handle_left, kp.handle_right = hl, hr
            fc.update()
        else:
            set_weight(st, w, scene.frame_current)
    return new_id


def extract(scene, layer_id, objects):
    """Move these members' keys out of a layer into a new layer right above it, with the same blend and weight
    (the result doesn't change). Returns the new layer's id, or None when none of them is in the layer."""
    obs = [ob for ob in objects if layer_id in layer_tracks(ob)]
    if not obs or layer_id == BASE_ID:
        return None
    src = find(scene, layer_id)
    blend_mode, w, name = src.blend, src.weight, src.name
    new = _insert_above(scene, layer_id, _copy_name(scene, name, "extract"))
    new.blend, new.weight = blend_mode, w
    new_id, new_name = new.layer_id, new.name
    for ob in obs:
        tr = layer_tracks(ob)[layer_id]
        act = tr.strips[0].action
        act[MARK] = new_id
        act.name = f"{new_name}_{ob.name}"
        tr.name = new_name
        tr.strips[0].name = new_name
    return new_id


def empty(scene, layer_id):
    """Take every object out of a layer (deleting its keys on it); the layer stays."""
    for ob in members(scene, layer_id):
        remove_object(ob, layer_id)


def delete_empty(scene):
    """Delete every layer but Base that has no objects. Returns their names."""
    ls = layers(scene)
    active_id = active(scene).layer_id if active(scene) is not None else BASE_ID
    gone = [(l.layer_id, l.name) for l in ls if l.layer_id != BASE_ID and not members(scene, l.layer_id)]
    for lid, _name in gone:
        ls.remove(index_of(scene, lid))
    i = index_of(scene, active_id)
    scene.phantom_anim_layer_index = i if i is not None else 0
    return [name for _lid, name in gone]


def prune(scene):
    """After objects left the layers: delete layers nobody is in, and the whole list when no object is layered."""
    if not layered_objects(scene):
        layers(scene).clear()
        scene.phantom_anim_layer_index = 0
        return
    delete_empty(scene)


def strip_state(tr):
    """What a track rebuild must keep: the Action, slot, blend, mute / lock and the weight with its keys."""
    st = tr.strips[0]
    keys = []
    fc = st.fcurves.find("influence") if st.use_animated_influence else None
    if fc is not None:
        keys = [(tuple(kp.co), tuple(kp.handle_left), tuple(kp.handle_right), kp.interpolation,
                 kp.handle_left_type, kp.handle_right_type) for kp in fc.keyframe_points]
    return {"action": st.action, "slot": st.action_slot, "mute": tr.mute, "lock": tr.lock,
            "animated": st.use_animated_influence, "influence": st.influence, "keys": keys}


def move_track(scene, ob, layer):
    """Put the object's track of ``layer`` where the list says (after a reorder). Call with the object out of
    tweak mode."""
    tr = layer_tracks(ob).get(layer.layer_id)
    if tr is None or layer.layer_id == BASE_ID:
        return
    state = strip_state(tr)
    act = state["action"]
    keep = act.use_fake_user
    act.use_fake_user = True             # not freed while it has no track
    ob.animation_data.nla_tracks.remove(tr)
    tr = _add_track(ob, layer, act, state["slot"], after=_track_below(scene, ob, layer.layer_id))
    act.use_fake_user = keep
    tr.mute, tr.lock = state["mute"], state["lock"]
    st = tr.strips[0]
    if state["animated"]:
        fc = weight_curve(st)
        for co, hl, hr, interp, tl, trt in state["keys"]:
            kp = fc.keyframe_points.insert(co[0], co[1], options={'FAST'})
            kp.interpolation = interp
            kp.handle_left_type, kp.handle_right_type = tl, trt
            kp.handle_left, kp.handle_right = hl, hr
        fc.update()
        st.influence = state["influence"]


# --- weight -----------------------------------------------------------------------------------------------------

def weight_curve(st):
    """The strip's influence curve (the layer's keyed weight), without the key Blender adds when animated
    influence is switched on."""
    if not st.use_animated_influence:
        keep = st.influence
        st.use_animated_influence = True
        fc = st.fcurves.find("influence")
        if fc is not None:
            fc.keyframe_points.clear()
        st.influence = keep
    return st.fcurves.find("influence")


def weight_is_keyed(st):
    fc = st.fcurves.find("influence") if st.use_animated_influence else None
    return fc is not None and len(fc.keyframe_points) > 0


def set_weight(st, w, frame, key=False):
    """Write a layer's weight to one strip: a static influence, or (``key``) a key at ``frame`` when the weight
    is keyed. A keyed weight is left alone otherwise."""
    if weight_is_keyed(st):
        if not key:
            return
        fc = st.fcurves.find("influence")
        kp = fc.keyframe_points.insert(frame, w)
        kp.handle_left_type = kp.handle_right_type = 'AUTO_CLAMPED'
        fc.update()
        return
    if abs(w - 1.0) < 1e-9 and not st.use_animated_influence:
        return
    weight_curve(st)
    st.influence = w


def key_weight(scene, layer, frame):
    """Key the layer's current weight at ``frame`` on every member."""
    for ob in members(scene, layer.layer_id):
        st = layer_tracks(ob)[layer.layer_id].strips[0]
        fc = weight_curve(st)
        kp = fc.keyframe_points.insert(frame, layer.weight)
        kp.handle_left_type = kp.handle_right_type = 'AUTO_CLAMPED'
        fc.update()
        ob.update_tag(refresh={'TIME'})


# --- writing the list onto the tracks ------------------------------------------------------------------------------

def _lock_curves(act, lock):
    """Lock the layer's curves too, so its keys can't be edited in the Graph Editor or Dope Sheet."""
    for layer in getattr(act, "layers", ()):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", ()):
                for fc in bag.fcurves:
                    if fc.lock != lock:
                        fc.lock = lock


def sync(scene, weight_changed=None):
    """Write every layer's weight, mute, solo, lock and blend onto its members' tracks. A keyed weight is only
    keyed again for the layer whose weight just changed (``weight_changed``, a layer id)."""
    ls = layers(scene)
    if not len(ls):
        return
    soloed = {l.layer_id for l in ls if l.solo and l.layer_id != BASE_ID}
    by_id = {l.layer_id: l for l in ls}
    frame = scene.frame_current
    for ob in layered_objects(scene):
        for lid, tr in layer_tracks(ob).items():
            layer = by_id.get(lid)
            if layer is None:
                continue
            base = lid == BASE_ID
            mute = not base and (layer.mute or (bool(soloed) and lid not in soloed))
            if tr.mute != mute:
                tr.mute = mute
            if tr.lock != layer.lock:
                tr.lock = layer.lock
            st = tr.strips[0]
            blend = 'REPLACE' if base or layer.blend == 'OVERRIDE' else 'COMBINE'
            if st.blend_type != blend:
                st.blend_type = blend
            if not base:
                set_weight(st, layer.weight, frame, key=lid == weight_changed)
            _lock_curves(st.action, layer.lock)
        ob.update_tag(refresh={'TIME'})


def rename(scene, layer):
    for ob in members(scene, layer.layer_id):
        tr = layer_tracks(ob)[layer.layer_id]
        tr.name = layer.name
        tr.strips[0].name = layer.name
        tr.strips[0].action.name = f"{layer.name}_{ob.name}"


# --- the layer entry ---------------------------------------------------------------------------------------------

def _sync_update(self, context):
    from . import editing
    sync(context.scene)
    editing.refresh(context.scene)


def _weight_update(self, context):
    sync(context.scene, weight_changed=self.layer_id)


def _name_update(self, context):
    if self.layer_id:
        rename(context.scene, self)


def _redraw_graph(_self, context):
    for window in getattr(context.window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type in {'GRAPH_EDITOR', 'VIEW_3D'}:
                area.tag_redraw()


class ANIM_LAYERS_PG_layer(bpy.types.PropertyGroup):
    name: StringProperty(name="Name", update=_name_update)
    layer_id: StringProperty(options={'HIDDEN'})
    weight: FloatProperty(
        name="Weight", default=1.0, min=0.0, max=1.0, subtype='FACTOR', update=_weight_update,
        description="How much of this layer you see. When the weight is keyed, changing it keys it here")
    mute: BoolProperty(name="Mute", update=_sync_update, description="Hide this layer")
    solo: BoolProperty(name="Solo", update=_sync_update,
                       description="Show only the soloed layers over Base")
    lock: BoolProperty(name="Lock", update=_sync_update, description="Protect this layer: no keys go on it")
    blend: EnumProperty(
        name="Blend", update=_sync_update,
        items=(('ADD', "Add", "This layer's keys are added on top of what's below"),
               ('OVERRIDE', "Override", "This layer replaces what's below, as much as its weight")),
        default='ADD')
    color: FloatVectorProperty(
        name="Color", subtype='COLOR_GAMMA', size=3, min=0.0, max=1.0, default=BASE_COLOR,
        update=_redraw_graph, description="The layer's colour (its curves in the Graph Editor are drawn in it)")
    show_curves: BoolProperty(
        name="Show Curves", update=_redraw_graph,
        description="Also draw this layer's curves in the Graph Editor, dimmed in its colour, while another layer "
                    "is active. Click one to make this layer active")


classes = (ANIM_LAYERS_PG_layer,)
