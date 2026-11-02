# SPDX-FileCopyrightText: 2026 ohnsk
# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Merge Down, Bake to Base and Zero Key: commands that write layer values from the layer math.

Based on Maya Workflow Kit for Blender by ohnsk (https://github.com/5hyuns/maya-workflow-kit-for-blender,
commit bd7ee04, ``src/allset/operators/anim_layers.py``: ``merge_down``, ``zero_key``). Modified for Phantom on
2026-10-28: Merge Down merges into the layer directly below (not always Base) and solves each frame so the result
doesn't change (keyed weights, Override layers in between, quaternions); Bake to Base flattens every layer and
gives the object back a plain Action; Zero Key on an Override layer keys the value below instead of 0.

Values come from the layers' curves (``blend.py``), not from evaluating the scene, so constraints and drivers are
left out, as they are when the layers play.
"""

import math

from mathutils import Quaternion

from . import blend, model

QUATERNION = "rotation_quaternion"


# --- reading the stack ---------------------------------------------------------------------------------------------

def ordered(ob):
    """[(layer id, track)] of the object's layer tracks, bottom first."""
    have = {tr: lid for lid, tr in model.layer_tracks(ob).items()}
    return [(have[tr], tr) for tr in ob.animation_data.nla_tracks if tr in have]


def channelbag(st, create=False):
    act, slot = st.action, st.action_slot
    if act is None or slot is None:
        return None
    if create and not len(act.layers):
        act.layers.new("Layer")
    if not len(act.layers):
        return None
    if create and not len(act.layers[0].strips):
        act.layers[0].strips.new(type='KEYFRAME')
    if not len(act.layers[0].strips):
        return None
    return act.layers[0].strips[0].channelbag(slot, ensure=create)


def curves(tr):
    bag = channelbag(tr.strips[0])
    return list(bag.fcurves) if bag is not None else []


def curve(tr, path, index):
    bag = channelbag(tr.strips[0])
    return bag.fcurves.find(path, index=index) if bag is not None else None


def weight(st, frame):
    """The strip's weight at ``frame``: 1 unless its influence is animated."""
    if not st.use_animated_influence:
        return 1.0
    fc = st.fcurves.find("influence")
    if fc is not None and len(fc.keyframe_points):
        return fc.evaluate(frame)
    return st.influence


def default(ob, path, index):
    """The value the NLA starts a channel from (the property's default)."""
    owner_path, _, ident = path.rpartition(".")
    try:
        owner = ob.path_resolve(owner_path) if owner_path else ob
    except ValueError:
        return 0.0
    prop = getattr(owner, "bl_rna", None) and owner.bl_rna.properties.get(ident)
    if prop is None or prop.type not in {'FLOAT', 'INT', 'BOOLEAN'}:
        return 0.0
    if getattr(prop, "is_array", False):
        arr = prop.default_array
        return float(arr[index]) if index < len(arr) else 0.0
    return float(prop.default)


def step(tr, path, index, frame, base=False):
    """(mode, weight, value or None) of one track for a channel, as ``blend.compose`` takes it."""
    st = tr.strips[0]
    fc = curve(tr, path, index)
    if base or st.blend_type == 'REPLACE':
        mode = 'REPLACE'
    else:
        mode = 'MUL' if blend.is_scale(path) else 'ADD'
    return mode, (1.0 if base else weight(st, frame)), (fc.evaluate(frame) if fc is not None else None)


def quat_step(tr, path, frame, base=False):
    """(blend, weight, Quaternion or None) of one track for a quaternion channel."""
    st = tr.strips[0]
    fcs = [curve(tr, path, i) for i in range(4)]
    q = None
    if any(fc is not None for fc in fcs):
        ident = (1.0, 0.0, 0.0, 0.0)
        q = Quaternion([fc.evaluate(frame) if fc is not None else ident[i] for i, fc in enumerate(fcs)])
    blend_type = 'REPLACE' if base else st.blend_type
    return blend_type, (1.0 if base else weight(st, frame)), q


def groups(fcurves):
    """[(data path, indices, is quaternion)] of a set of F-curves; a quaternion's four curves go together."""
    out, seen = [], set()
    for fc in fcurves:
        path = fc.data_path
        quat = path.rsplit(".", 1)[-1] == QUATERNION
        key = (path, None) if quat else (path, fc.array_index)
        if key in seen:
            continue
        seen.add(key)
        out.append((path, (0, 1, 2, 3) if quat else (fc.array_index,), quat))
    return out


def frames(scene, tracks, path, indices):
    """Every whole frame over the scene range and the keys of these tracks' channel and weights."""
    lo, hi = scene.frame_start, scene.frame_end
    for tr in tracks:
        st = tr.strips[0]
        fcs = [curve(tr, path, i) for i in indices]
        if st.use_animated_influence:
            fcs.append(st.fcurves.find("influence"))
        for fc in fcs:
            if fc is not None and len(fc.keyframe_points):
                xs = [kp.co.x for kp in fc.keyframe_points]
                lo, hi = min(lo, min(xs)), max(hi, max(xs))
    return range(int(math.floor(lo)), int(math.ceil(hi)) + 1)


def write(st, path, index, frames_, values):
    """Replace a channel's keys on the strip's Action with one key per frame."""
    bag = channelbag(st, create=True)
    fc = bag.fcurves.find(path, index=index) or bag.fcurves.new(path, index=index)
    pts = fc.keyframe_points
    pts.clear()
    pts.add(len(frames_))
    pts.foreach_set("co", [c for f, v in zip(frames_, values) for c in (float(f), float(v))])
    for kp in pts:
        kp.interpolation = 'BEZIER'
        kp.handle_left_type = kp.handle_right_type = 'AUTO_CLAMPED'
    fc.update()


# --- Merge Down ------------------------------------------------------------------------------------------------------

def _merged_scalar(ob, scene, lower, below, below_is_base, layer, path, index):
    start = default(ob, path, index)
    fr = frames(scene, lower + [below, layer], path, (index,))
    values = []
    for f in fr:
        low = [step(t, path, index, f, base=_is_base(ob, t)) for t in lower]
        b = step(below, path, index, f, base=below_is_base)
        target = blend.compose(low + [b, step(layer, path, index, f)], start)
        x = blend.solve(low + [(b[0], b[1], None)], target, len(low), start)
        values.append(x if x is not None else (b[2] if b[2] is not None else target))
    return fr, values


def _merged_quaternion(ob, scene, lower, below, below_is_base, layer, path):
    fr = frames(scene, lower + [below, layer], path, (0, 1, 2, 3))
    values, prev = [], None
    for f in fr:
        low = [quat_step(t, path, f, base=_is_base(ob, t)) for t in lower]
        b = quat_step(below, path, f, base=below_is_base)
        target = blend.compose_quaternion(low + [b, quat_step(layer, path, f)])
        x = blend.solve_quaternion(low + [(b[0], b[1], None)], target, len(low))
        if x is None:
            x = b[2] if b[2] is not None else target
        if prev is not None and prev.dot(x) < 0.0:
            x = -x                         # keep the curves on one side, so keys don't flip between frames
        values.append(x)
        prev = x
    return fr, values


def _is_base(ob, tr):
    return tr.strips[0].action.get(model.MARK) == model.BASE_ID


def merge_down(scene, layer_id):
    """Merge layer ``layer_id`` into the layer below it on each member (Base when the object isn't in the layer
    below in the list), keyed on every frame so the result doesn't change, then remove it. Call with the members
    out of tweak mode. Returns the number of objects merged."""
    count = 0
    for ob in model.members(scene, layer_id):
        order = ordered(ob)
        k = next(i for i, (lid, _tr) in enumerate(order) if lid == layer_id)
        layer = order[k][1]
        below_id, below = order[k - 1]
        below_is_base = below_id == model.BASE_ID
        if not layer.mute:
            lower = [tr for _lid, tr in order[:k - 1] if not tr.mute]
            for path, indices, quat in groups(curves(layer)):
                if quat:
                    fr, values = _merged_quaternion(ob, scene, lower, below, below_is_base, layer, path)
                    for i in range(4):
                        write(below.strips[0], path, i, fr, [q[i] for q in values])
                else:
                    fr, values = _merged_scalar(ob, scene, lower, below, below_is_base, layer, path, indices[0])
                    write(below.strips[0], path, indices[0], fr, values)
        model.remove_object(ob, layer_id)
        ob.update_tag(refresh={'TIME'})
        count += 1
    return count


# --- Bake to Base ------------------------------------------------------------------------------------------------------

def bake_to_base(scene, ob):
    """Bake every layer the object plays into its Base Action, then take the object out of the layers: Base becomes
    its plain Action again (what exporters expect). Muted layers are left out. Call with the object out of tweak
    mode. Returns the number of muted layers left out."""
    order = ordered(ob)
    base = order[0][1]
    playing = [tr for _lid, tr in order if not tr.mute]
    skipped = sum(1 for _lid, tr in order[1:] if tr.mute)
    fcurves = [fc for tr in playing[1:] for fc in curves(tr)]
    for path, indices, quat in groups(fcurves):
        fr = frames(scene, playing, path, indices)
        if quat:
            values, prev = [], None
            for f in fr:
                q = blend.compose_quaternion([quat_step(t, path, f, base=(t == base)) for t in playing])
                if prev is not None and prev.dot(q) < 0.0:
                    q = -q
                values.append(q)
                prev = q
            for i in range(4):
                write(base.strips[0], path, i, fr, [q[i] for q in values])
        else:
            index = indices[0]
            start = default(ob, path, index)
            values = [blend.compose([step(t, path, index, f, base=(t == base)) for t in playing], start) for f in fr]
            write(base.strips[0], path, index, fr, values)
    model.unlayer(ob)
    ob.update_tag(refresh={'TIME'})
    return skipped


# --- Zero Key ------------------------------------------------------------------------------------------------------

def zero_key(scene, layer_id, objects, frame):
    """Key "no change" on every channel the layer has, on these members, at ``frame``: 0 (1 for scale, identity
    for quaternions) on an Add layer, the value below on an Override layer. Returns the number of keys set."""
    n = 0
    for ob in objects:
        order = ordered(ob)
        k = next((i for i, (lid, _tr) in enumerate(order) if lid == layer_id), None)
        if k is None:
            continue
        tr = order[k][1]
        override = tr.strips[0].blend_type == 'REPLACE'
        lower = [t for _lid, t in order[:k] if not t.mute]
        for path, indices, quat in groups(curves(tr)):
            if quat:
                q = (blend.compose_quaternion([quat_step(t, path, frame, base=_is_base(ob, t)) for t in lower])
                     if override else Quaternion())
                values = list(q)
            else:
                index = indices[0]
                if override:
                    values = [blend.compose([step(t, path, index, frame, base=_is_base(ob, t)) for t in lower],
                                            default(ob, path, index))]
                else:
                    values = [1.0 if blend.is_scale(path) else 0.0]
            bag = channelbag(tr.strips[0], create=True)
            for index, value in zip(indices, values):
                fc = bag.fcurves.find(path, index=index) or bag.fcurves.new(path, index=index)
                fc.keyframe_points.insert(frame, value)
                fc.update()
                n += 1
        ob.update_tag(refresh={'TIME'})
    return n
