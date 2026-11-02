# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Keys go on the active layer: every layered object sits in NLA tweak mode on the active layer's track (Base when
it isn't in that layer), with the tracks above still evaluated (``AnimData.use_tweak_mode_upper_stack``,
Phantom). Blender then solves every key it sets through the whole stack, and the Graph Editor, Dope Sheet and
Timeline show that layer's keys.

Also here: putting objects back on their layer when something takes them out of tweak mode (the Action Editor's
unlink, the NLA editor's Tab), and giving a duplicated object its own layer Actions (Blender's duplicate shares
NLA strip Actions with the original).
"""

from contextlib import contextmanager

import bpy
from bpy.app.handlers import persistent

from . import model


def supported():
    """This Blender has Phantom's upper-stack tweak mode."""
    return "use_tweak_mode_upper_stack" in bpy.types.AnimData.bl_rna.properties


def editing_on(scene):
    return scene.phantom_anim_layers_editing


def target_track(scene, ob):
    """The track ``ob`` keys onto: the active layer's, or Base when it isn't in that layer."""
    have = model.layer_tracks(ob)
    layer = model.active(scene)
    if layer is not None and layer.layer_id in have:
        return have[layer.layer_id]
    return have.get(model.BASE_ID)


def tweaked_layer_id(ob):
    ad = ob.animation_data
    if ad is None or not ad.use_tweak_mode or ad.action is None:
        return None
    return ad.action.get(model.MARK)


def leave(ob):
    ad = ob.animation_data
    if ad is not None and ad.use_tweak_mode:
        ad.use_tweak_mode = False


def put_on(ob, tr):
    """Tweak ``tr`` (with the upper stack) on ``ob``, unless it already does."""
    ad = ob.animation_data
    act = tr.strips[0].action
    if ad.use_tweak_mode:
        if ad.action == act and ad.use_tweak_mode_upper_stack:
            return
        ad.use_tweak_mode = False
    # Tweak mode picks the active track's active (or selected) strip.
    for t in ad.nla_tracks:
        sel = t == tr
        if t.select != sel:
            t.select = sel
        for s in t.strips:
            if s.select != sel:
                s.select = sel
    ad.nla_tracks.active = tr
    ad.use_tweak_mode_upper_stack = True
    ad.use_tweak_mode = True


def refresh(scene):
    """Put every layered object of ``scene`` on its layer (or out of tweak mode while editing is off)."""
    if not supported():
        return
    obs = model.layered_objects(scene)
    _watch[scene.name] = [ob.name for ob in obs]
    for ob in obs:
        if not editing_on(scene):
            leave(ob)
            continue
        tr = target_track(scene, ob)
        if tr is not None:
            put_on(ob, tr)


@contextmanager
def suspended(scene, objects=None):
    """Take objects out of tweak mode for changes to their tracks; back on their layers afterwards."""
    obs = model.layered_objects(scene) if objects is None else list(objects)
    for ob in obs:
        leave(ob)
    try:
        yield
    finally:
        refresh(scene)


# --- duplicates ---------------------------------------------------------------------------------------------------

def fix_copies(scene):
    """Give every layered object its own layer Actions. Blender's duplicate copies only the Action being tweaked,
    so a copy otherwise shares Base and the other layers with the original (keying one would move both). The
    object named in an Action's ``OWNER`` keeps it; the others get copies. Returns the objects changed."""
    users = {}
    for ob in model.layered_objects(scene):
        for lid, tr in model.layer_tracks(ob).items():
            users.setdefault(tr.strips[0].action, []).append((ob, lid))
    to_fix = {}
    for act, uses in users.items():
        if len(uses) < 2:
            if uses and act.get(model.OWNER) != uses[0][0].name:
                act[model.OWNER] = uses[0][0].name          # follows a renamed object
            continue
        keeper = next((u for u in uses if u[0].name == act.get(model.OWNER)), uses[0])
        for ob, lid in uses:
            if (ob, lid) != keeper:
                to_fix.setdefault(ob, []).append(lid)
    if not to_fix:
        return []
    with suspended(scene, to_fix):
        for ob, lids in to_fix.items():
            have = model.layer_tracks(ob)
            for lid in lids:
                st = have[lid].strips[0]
                old, slot = st.action, st.action_slot
                copy = old.copy()
                copy[model.OWNER] = ob.name
                layer = model.find(scene, lid)
                copy.name = f"{layer.name if layer else lid}_{ob.name}"
                st.action = copy
                if slot is not None:
                    st.action_slot = next((s for s in copy.slots if s.identifier == slot.identifier), None)
    return list(to_fix)


# --- watching ------------------------------------------------------------------------------------------------------

_watch = {}             # scene name -> names of its layered objects (refreshed by refresh())
_state = {"objects": -1}


def _repair():
    """Timer: copies get their own Actions, and layered objects go back on their layers."""
    for scene in bpy.data.scenes:
        if not len(getattr(scene, "phantom_anim_layers", ())):
            continue
        try:
            fix_copies(scene)
            model.sync(scene)
            refresh(scene)
        except (RuntimeError, ReferenceError) as ex:
            print("Animation Layers: couldn't put objects back on their layers:", ex)
    _state["objects"] = len(bpy.data.objects)
    return None


def _schedule():
    if not bpy.app.timers.is_registered(_repair):
        bpy.app.timers.register(_repair, first_interval=0.0)


def needs_repair(scene):
    """An object was added (maybe a duplicate), or a layered object left tweak mode while editing is on."""
    if len(bpy.data.objects) != _state["objects"]:
        return True
    if not editing_on(scene) or not supported():
        return False
    for name in _watch.get(scene.name, ()):
        ob = scene.objects.get(name)
        if ob is None:
            return True
        ad = ob.animation_data
        if ad is None or not ad.use_tweak_mode:
            return True
    return False


@persistent
def _on_depsgraph(scene, _depsgraph):
    if len(getattr(scene, "phantom_anim_layers", ())) and needs_repair(scene):
        _schedule()


@persistent
def _on_load(_dummy):
    _watch.clear()
    _state["objects"] = -1
    _schedule()


@persistent
def _on_undo(_scene, *_args):
    # Undo brings back the tweak state with the data; only the names we watch may be stale.
    _watch.clear()
    _state["objects"] = len(bpy.data.objects)
    for scene in bpy.data.scenes:
        if len(getattr(scene, "phantom_anim_layers", ())):
            _watch[scene.name] = [ob.name for ob in model.layered_objects(scene)]


HANDLERS = (("depsgraph_update_post", _on_depsgraph), ("load_post", _on_load), ("undo_post", _on_undo),
            ("redo_post", _on_undo))


def register():
    for name, fn in HANDLERS:
        getattr(bpy.app.handlers, name).append(fn)
    _schedule()


def unregister():
    for name, fn in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if fn in handlers:
            handlers.remove(fn)
    if bpy.app.timers.is_registered(_repair):
        bpy.app.timers.unregister(_repair)
    _watch.clear()
