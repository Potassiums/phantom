# SPDX-FileCopyrightText: 2026 ohnsk
# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tests the Animation Layers extension (release/extensions/system/anim_layers) and the Phantom C++ it needs
(AnimData.use_tweak_mode_upper_stack, no keys on a locked or muted layer).

The value checks follow the animation layer tests of Maya Workflow Kit for Blender by ohnsk
(https://github.com/5hyuns/maya-workflow-kit-for-blender, commit bd7ee04: tests/test_step180_anim_layers.py,
test_step188_anim_layer_followups.py, test_step194_layer_display.py), modified for Phantom on 2026-10-28: keys go
through Blender's own keying in tweak mode, and results are checked against the layer math of ``blend.py``.

phantom -b --factory-startup --python tests/python/phantom_anim_layers.py
"""

import math
import os
import sys
import tempfile
import unittest

import addon_utils
import bpy
from mathutils import Euler, Quaternion

MODULE = "bl_ext.system.anim_layers"
addon_utils.enable(MODULE, default_set=True, handle_error=lambda ex: (_ for _ in ()).throw(ex))

from bl_ext.system.anim_layers import blend, editing, model  # noqa: E402

TOL = 1e-4
CHANNELS = (("location", 0), ("rotation_euler", 1), ("scale", 0))


def _view3d():
    area = next(a for a in bpy.context.window.screen.areas if a.type == 'VIEW_3D')
    region = next(r for r in area.regions if r.type == 'WINDOW')
    return bpy.context.temp_override(window=bpy.context.window, area=area, region=region,
                                     space_data=area.spaces.active)


def _borrowed(kind):
    """An area of the window turned into ``kind``: (area, its old type)."""
    for a in bpy.context.window.screen.areas:
        if a.type != 'VIEW_3D':
            old = a.type
            a.type = kind
            return a, old
    raise RuntimeError("no area to borrow")


def _select(*obs):
    for ob in bpy.context.scene.objects:
        ob.select_set(ob in obs)
    bpy.context.view_layer.objects.active = obs[0] if obs else None


def _linear(ob):
    from bpy_extras.anim_utils import action_get_channelbag_for_slot
    for tr in model.layer_tracks(ob).values():
        bag = action_get_channelbag_for_slot(tr.strips[0].action, tr.strips[0].action_slot)
        for fc in (bag.fcurves if bag else ()):
            for kp in fc.keyframe_points:
                kp.interpolation = 'LINEAR'
    ad = ob.animation_data
    if ad is not None and ad.action is not None and not ad.use_tweak_mode:
        bag = action_get_channelbag_for_slot(ad.action, ad.action_slot)
        for fc in (bag.fcurves if bag else ()):
            for kp in fc.keyframe_points:
                kp.interpolation = 'LINEAR'


def _curve(ob, layer_id, path, index):
    from bpy_extras.anim_utils import action_get_channelbag_for_slot
    st = model.layer_tracks(ob)[layer_id].strips[0]
    bag = action_get_channelbag_for_slot(st.action, st.action_slot)
    return bag.fcurves.find(path, index=index) if bag else None


def _key_at(fc, frame):
    for kp in (fc.keyframe_points if fc is not None else ()):
        if abs(kp.co.x - frame) < 1e-4:
            return kp.co.y
    return None


def _evaluated(ob, path, index=None, frame=None):
    if frame is not None:
        bpy.context.scene.frame_set(int(frame))
    else:
        bpy.context.view_layer.update()
    v = ob.evaluated_get(bpy.context.evaluated_depsgraph_get()).path_resolve(path)
    return float(v[index]) if index is not None else v.copy()


def _steps(ob, path, index, frame):
    """[(layer id, mode, weight, value or None)] bottom first, from the object's tracks (muted ones left out)."""
    out = []
    have = {tr: lid for lid, tr in model.layer_tracks(ob).items()}
    for tr in ob.animation_data.nla_tracks:
        if tr not in have or tr.mute:
            continue
        lid = have[tr]
        st = tr.strips[0]
        fc = _curve(ob, lid, path, index)
        if lid == model.BASE_ID or not st.use_animated_influence:
            w = 1.0          # without animated influence the strip plays fully (its stored influence reads 0)
        elif model.weight_is_keyed(st):
            w = st.fcurves.find("influence").evaluate(frame)
        else:
            w = st.influence
        mode = 'REPLACE' if st.blend_type == 'REPLACE' else ('MUL' if blend.is_scale(path) else 'ADD')
        out.append((lid, mode, w, fc.evaluate(frame) if fc is not None else None))
    return out


def _oracle(ob, path, index, frame):
    return blend.compose([s[1:] for s in _steps(ob, path, index, frame)], 1.0 if blend.is_scale(path) else 0.0)


def _set_active(scene, layer):
    scene.phantom_anim_layer_index = model.index_of(scene, layer.layer_id)


class AnimLayersCase(unittest.TestCase):
    """Scene reset and helpers shared by the test classes."""

    def setUp(self):
        if bpy.context.object is not None and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        scene = bpy.context.scene
        scene.phantom_anim_layers.clear()
        scene.phantom_anim_layer_index = 0
        scene.phantom_anim_layers_editing = True
        for ob in list(bpy.data.objects):
            bpy.data.objects.remove(ob)
        for coll in (bpy.data.actions, bpy.data.armatures):
            for idb in list(coll):
                coll.remove(idb)
        scene.frame_start, scene.frame_end = 1, 24
        scene.frame_set(1)
        scene.tool_settings.use_keyframe_insert_auto = False
        self.scene = scene

    def cube(self, name="Cube"):
        """Base: location.x 0 -> 10, rotation Y 0 -> 90, scale X 1 -> 2 over frames 1-10 (linear)."""
        ob = bpy.data.objects.new(name, None)
        self.scene.collection.objects.link(ob)
        ob.rotation_mode = 'XYZ'
        for f, tx, ry, sx in ((1, 0.0, 0.0, 1.0), (10, 10.0, 90.0, 2.0)):
            ob.location.x, ob.rotation_euler.y, ob.scale.x = tx, math.radians(ry), sx
            ob.keyframe_insert("location", index=0, frame=f)
            ob.keyframe_insert("rotation_euler", index=1, frame=f)
            ob.keyframe_insert("scale", index=0, frame=f)
        _linear(ob)
        return ob

    def layer(self, layer_id):
        return model.find(self.scene, layer_id)

    def new_layer(self, *obs, blend_mode='ADD'):
        _select(*obs)
        self.assertEqual(bpy.ops.anim_layers.new(), {'FINISHED'})
        layer = model.active(self.scene)
        layer.blend = blend_mode
        return layer

    def key(self, ob, path, index, frame, value):
        """Set the pose and key it with Blender's own keying; the result must be the pose."""
        self.scene.frame_set(frame)
        getattr(ob, path)[index] = value
        self.assertTrue(ob.keyframe_insert(path, index=index, frame=frame), f"{path}[{index}] not keyed")
        self.assertAlmostEqual(_evaluated(ob, path, index, frame=frame), value, delta=TOL)



class AnimLayersTest(AnimLayersCase):

    # --- the basics -------------------------------------------------------------------------------------------

    def test_new_layer_from_selected(self):
        ob = self.cube()
        before = [_evaluated(ob, p, i, frame=6) for p, i in CHANNELS]
        layer = self.new_layer(ob)
        self.assertEqual([l.name for l in model.layers(self.scene)], ["Base", "Layer 1"])
        self.assertEqual(list(model.layer_tracks(ob)), [model.BASE_ID, layer.layer_id])
        ad = ob.animation_data
        self.assertTrue(ad.use_tweak_mode and ad.use_tweak_mode_upper_stack)
        self.assertEqual(editing.tweaked_layer_id(ob), layer.layer_id)
        self.assertFalse(self.scene.is_nla_tweakmode, "the scene flag stays off (NLA editing keeps working)")
        st = model.layer_tracks(ob)[layer.layer_id].strips[0]
        self.assertEqual((st.frame_start, st.frame_end), model.WIDE)
        self.assertFalse(st.use_sync_length)
        self.assertEqual(before, [_evaluated(ob, p, i, frame=6) for p, i in CHANNELS])

    def test_keys_go_on_the_active_layer_exactly(self):
        """Kit step 180: two Add layers and an Override; every key gives the posed value, the raw keys are the
        layer math's, and the result at frames 1-10 follows the layer math through weight, mute, solo and
        blend changes."""
        ob = self.cube()
        # Layer entries are looked up by id each time: adding to the list can move them in memory.
        l1 = self.new_layer(ob).layer_id
        l2 = self.new_layer(ob).layer_id
        l3 = self.new_layer(ob, blend_mode='OVERRIDE').layer_id
        self.layer(l3).weight = 0.5
        for lid, keys in ((l1, [("location", 0, 1, 5.0), ("location", 0, 10, 15.0), ("rotation_euler", 1, 1, 0.2),
                                ("scale", 0, 1, 2.0)]),
                          (l2, [("location", 0, 5, 9.0), ("rotation_euler", 1, 10, 1.2)]),
                          (l3, [("location", 0, 1, 2.0), ("scale", 0, 10, 3.0)]),
                          (model.BASE_ID, [("location", 0, 7, 4.0)])):
            layer = self.layer(lid)
            _set_active(self.scene, layer)
            for path, idx, frame, value in keys:
                steps = _steps(ob, path, idx, frame)
                k = [s[0] for s in steps].index(layer.layer_id)
                start = value if layer.layer_id == model.BASE_ID else (1.0 if blend.is_scale(path) else 0.0)
                want = blend.solve([s[1:] for s in steps], value, k, start)
                self.key(ob, path, idx, frame, value)
                raw = _key_at(_curve(ob, layer.layer_id, path, idx), frame)
                self.assertAlmostEqual(raw, want, delta=1e-3, msg=f"{layer.name} {path}[{idx}] at {frame}")

        def check(label):
            for path, idx in CHANNELS:
                for f in range(1, 11):
                    self.assertAlmostEqual(_evaluated(ob, path, idx, frame=f), _oracle(ob, path, idx, f),
                                           delta=TOL, msg=f"{label}: {path}[{idx}] at {f}")
        check("all layers")
        self.layer(l1).weight = 0.5
        check("Layer 1 at weight 0.5")
        self.layer(l2).mute = True
        check("Layer 2 muted")
        self.layer(l2).mute = False
        self.layer(l1).solo = True
        self.assertTrue(model.layer_tracks(ob)[l2].mute and not model.layer_tracks(ob)[l1].mute)
        check("Layer 1 soloed")
        self.layer(l1).solo = False
        self.layer(l3).blend = 'ADD'
        check("Layer 3 switched to Add")

    def test_keys_on_an_object_outside_the_layer_go_on_base(self):
        a, b = self.cube("A"), self.cube("B")
        self.new_layer(a, b)
        layer = self.new_layer(a)
        self.assertEqual(editing.tweaked_layer_id(a), layer.layer_id)
        self.assertEqual(editing.tweaked_layer_id(b), model.BASE_ID)
        self.key(b, "location", 0, 4, 20.0)
        self.assertIsNotNone(_key_at(_curve(b, model.BASE_ID, "location", 0), 4))

    def test_locked_and_muted_layers_take_no_keys(self):
        ob = self.cube()
        layer = self.new_layer(ob)
        self.key(ob, "location", 0, 3, 6.0)
        for prop in ("lock", "mute"):
            setattr(layer, prop, True)
            self.scene.frame_set(5)
            ob.location.x = 9.0
            self.assertFalse(ob.keyframe_insert("location", index=0, frame=5), prop)
            self.assertFalse(ob.keyframe_insert("location", index=2, frame=5), f"{prop}: a new channel")
            self.assertIsNone(_key_at(_curve(ob, layer.layer_id, "location", 0), 5))
            if prop == "lock":
                self.assertTrue(all(fc.lock for fc in [_curve(ob, layer.layer_id, "location", 0)]))
            setattr(layer, prop, False)
        self.assertFalse(_curve(ob, layer.layer_id, "location", 0).lock)
        self.key(ob, "location", 0, 5, 9.0)

    def test_weight_and_keyed_weight(self):
        """Kit step 204 (weight keys): a static weight, then keyed 0 -> 1; changing a keyed weight keys it; other
        changes leave the weight keys alone."""
        ob = self.cube()
        layer = self.new_layer(ob)
        self.key(ob, "location", 0, 1, 4.0)          # the layer adds 4 everywhere (one key)
        layer.weight = 0.25
        st = model.layer_tracks(ob)[layer.layer_id].strips[0]
        self.assertAlmostEqual(st.influence, 0.25)
        self.assertAlmostEqual(_evaluated(ob, "location", 0, frame=6), _oracle(ob, "location", 0, 6), delta=TOL)
        self.scene.frame_set(1)
        layer.weight = 0.0
        self.assertEqual(bpy.ops.anim_layers.key_weight(), {'FINISHED'})
        self.scene.frame_set(10)
        layer.weight = 1.0                           # keyed now: changing it keys it here
        fc = st.fcurves.find("influence")
        self.assertEqual([(round(k.co.x), round(k.co.y, 3)) for k in fc.keyframe_points], [(1, 0.0), (10, 1.0)])
        layer.mute = True
        layer.mute = False
        self.assertEqual(len(fc.keyframe_points), 2, "a mute toggle must not key the weight")
        for f in (1, 4, 10):
            self.assertAlmostEqual(_evaluated(ob, "location", 0, frame=f), _oracle(ob, "location", 0, f), delta=TOL)

    def test_rename_keeps_the_layer(self):
        ob = self.cube()
        layer = self.new_layer(ob)
        layer.name = "Arm fix"
        tr = model.layer_tracks(ob)[layer.layer_id]
        self.assertEqual(tr.name, "Arm fix")
        self.assertEqual(tr.strips[0].action.name, "Arm fix_Cube")
        tr.name = "renamed in the NLA editor"
        self.assertIn(layer.layer_id, model.layer_tracks(ob))
        self.key(ob, "location", 0, 2, 3.0)

    def test_move_reorders_tracks_and_results(self):
        ob = self.cube()
        l1 = self.new_layer(ob, blend_mode='OVERRIDE').layer_id
        self.layer(l1).weight = 0.5
        self.key(ob, "location", 0, 1, 1.0)
        l2 = self.new_layer(ob).layer_id
        self.key(ob, "location", 0, 1, 6.0)
        _set_active(self.scene, self.layer(l1))
        self.assertEqual(bpy.ops.anim_layers.move(direction='UP'), {'FINISHED'})
        self.assertEqual([l.layer_id for l in model.layers(self.scene)], [model.BASE_ID, l2, l1])
        order = [lid for tr in ob.animation_data.nla_tracks for lid, t in model.layer_tracks(ob).items() if t == tr]
        self.assertEqual(order, [model.BASE_ID, l2, l1])
        self.assertEqual(editing.tweaked_layer_id(ob), l1)
        self.assertAlmostEqual(model.layer_tracks(ob)[l1].strips[0].influence, 0.5)
        for f in (1, 5, 10):
            self.assertAlmostEqual(_evaluated(ob, "location", 0, frame=f), _oracle(ob, "location", 0, f), delta=TOL)
        self.assertEqual(bpy.ops.anim_layers.move(direction='UP'), {'CANCELLED'}, "already on top")

    def test_delete_and_remove_objects(self):
        a, b = self.cube("A"), self.cube("B")
        base_only = _evaluated(a, "location", 0, frame=5)
        layer = self.new_layer(a, b)
        self.key(a, "location", 0, 5, 30.0)
        name = model.layer_tracks(a)[layer.layer_id].strips[0].action.name
        _select(b)
        self.assertEqual(bpy.ops.anim_layers.remove_objects(), {'FINISHED'})
        self.assertNotIn(layer.layer_id, model.layer_tracks(b))
        self.assertEqual(bpy.ops.anim_layers.delete(), {'FINISHED'})
        self.assertEqual([l.layer_id for l in model.layers(self.scene)], [model.BASE_ID])
        self.assertNotIn(name, bpy.data.actions)
        self.assertEqual(editing.tweaked_layer_id(a), model.BASE_ID)
        self.assertAlmostEqual(_evaluated(a, "location", 0, frame=5), base_only, delta=TOL)

    # --- keying paths -------------------------------------------------------------------------------------------

    def test_insert_keyframe_operator_and_auto_keying(self):
        """Kit step 188: Auto Keying on a layered object keys the active layer."""
        ob = self.cube()
        layer = self.new_layer(ob)
        bpy.context.preferences.edit.key_insert_channels = {'LOCATION'}
        self.scene.frame_set(4)
        ob.location.x = 12.0
        _select(ob)
        with _view3d():
            self.assertEqual(bpy.ops.anim.keyframe_insert(), {'FINISHED'})
        self.assertAlmostEqual(_evaluated(ob, "location", 0, frame=4), 12.0, delta=TOL)
        prefs = bpy.context.preferences.edit
        prefs.use_keyframe_insert_available = False
        prefs.use_keyframe_insert_needed = False
        prefs.use_auto_keyframe_insert_needed = False
        self.scene.tool_settings.use_keyframe_insert_auto = True
        self.scene.frame_set(8)
        before = _evaluated(ob, "location", 0, frame=8)
        with _view3d():
            bpy.ops.transform.translate(value=(1.0, 0.0, 0.0))
        self.scene.tool_settings.use_keyframe_insert_auto = False
        self.assertAlmostEqual(_evaluated(ob, "location", 0, frame=8), before + 1.0, delta=TOL)
        self.assertIsNotNone(_key_at(_curve(ob, layer.layer_id, "location", 0), 8))
        self.assertIsNone(_key_at(_curve(ob, model.BASE_ID, "location", 0), 8))

    def test_bone_quaternion_on_an_add_layer(self):
        arm = bpy.data.objects.new("Rig", bpy.data.armatures.new("Rig"))
        self.scene.collection.objects.link(arm)
        _select(arm)
        with _view3d():
            bpy.ops.object.mode_set(mode='EDIT')
            b = arm.data.edit_bones.new("arm")
            b.head, b.tail = (0, 0, 0), (0, 0, 1)
            bpy.ops.object.mode_set(mode='OBJECT')
        pb = arm.pose.bones["arm"]
        pb.rotation_mode = 'QUATERNION'
        for f, q in ((1, Quaternion()), (10, Quaternion((0, 0, 1), math.radians(90)))):
            pb.rotation_quaternion = q
            pb.keyframe_insert("rotation_quaternion", frame=f)
        self.new_layer(arm)
        self.scene.frame_set(5)
        target = Quaternion(Euler((math.radians(20), math.radians(-30), math.radians(45))))
        pb.rotation_quaternion = target
        self.assertTrue(pb.keyframe_insert("rotation_quaternion", frame=5))
        got = Quaternion(_evaluated(arm, 'pose.bones["arm"].rotation_quaternion', frame=5)).normalized()
        self.assertLess(math.degrees(got.rotation_difference(target).angle), 0.01)

    def test_keys_far_out_of_the_frame_range(self):
        ob = self.cube()
        self.new_layer(ob)
        self.key(ob, "location", 0, -1500, 7.0)
        self.key(ob, "location", 0, 5000, -3.0)

    # --- staying on the layer -------------------------------------------------------------------------------------

    def test_layer_editing_off_and_on(self):
        ob = self.cube()
        layer = self.new_layer(ob)
        self.scene.phantom_anim_layers_editing = False
        self.assertFalse(ob.animation_data.use_tweak_mode)
        self.scene.phantom_anim_layers_editing = True
        self.assertEqual(editing.tweaked_layer_id(ob), layer.layer_id)

    def test_back_on_the_layer_after_unlink(self):
        """The Action Editor's unlink (X) leaves tweak mode; the repair puts the object back on its layer."""
        ob = self.cube()
        layer = self.new_layer(ob)
        area, old = _borrowed('DOPESHEET_EDITOR')
        space = area.spaces.active
        old_mode = space.mode
        try:
            space.mode = 'ACTION'
            region = next(r for r in area.regions if r.type == 'WINDOW')
            with bpy.context.temp_override(window=bpy.context.window, area=area, region=region, space_data=space):
                bpy.ops.action.unlink()
        finally:
            space.mode = old_mode
            area.type = old
        self.assertFalse(ob.animation_data.use_tweak_mode)
        self.assertTrue(editing.needs_repair(self.scene))
        editing._repair()                 # what the depsgraph handler's timer runs (timers don't run headless)
        self.assertEqual(editing.tweaked_layer_id(ob), layer.layer_id)

    def test_duplicate_gets_its_own_actions(self):
        ob = self.cube()
        layer = self.new_layer(ob)
        self.key(ob, "location", 0, 3, 5.0)
        _select(ob)
        with _view3d():
            bpy.ops.object.duplicate()
        dup = bpy.context.view_layer.objects.active
        self.assertNotEqual(dup, ob)
        self.assertTrue(editing.needs_repair(self.scene))
        editing._repair()
        mine = {tr.strips[0].action for tr in model.layer_tracks(ob).values()}
        theirs = {tr.strips[0].action for tr in model.layer_tracks(dup).values()}
        self.assertFalse(mine & theirs, "the copy shares layer Actions with the original")
        self.assertEqual(editing.tweaked_layer_id(dup), layer.layer_id)
        before = _evaluated(ob, "location", 0, frame=3)
        _set_active(self.scene, model.layers(self.scene)[0])
        self.key(dup, "location", 0, 3, 40.0)
        self.assertAlmostEqual(_evaluated(ob, "location", 0, frame=3), before, delta=TOL)

    def test_save_and_reload(self):
        ob = self.cube()
        layer_id = self.new_layer(ob).layer_id
        self.key(ob, "location", 0, 6, 8.0)
        path = os.path.join(tempfile.mkdtemp(), "layers.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
        bpy.ops.wm.open_mainfile(filepath=path)       # every Python reference to the old file's data is stale now
        self.scene = bpy.context.scene
        ob = bpy.data.objects["Cube"]
        self.assertEqual([l.name for l in model.layers(self.scene)], ["Base", "Layer 1"])
        self.assertEqual(editing.tweaked_layer_id(ob), layer_id)
        self.assertTrue(ob.animation_data.use_tweak_mode_upper_stack)
        self.assertAlmostEqual(_evaluated(ob, "location", 0, frame=6), 8.0, delta=TOL)
        self.key(ob, "location", 0, 9, 2.0)

    def test_bake_action_refuses_a_layered_object(self):
        ob = self.cube()
        self.new_layer(ob)
        _select(ob)
        try:
            with _view3d():
                res = bpy.ops.nla.bake(frame_start=1, frame_end=10, bake_types={'OBJECT'}, visual_keying=True,
                                       use_current_action=True)
        except RuntimeError as ex:
            res = {'CANCELLED'}
            self.assertIn("editing an NLA strip or animation layer", str(ex))
        self.assertEqual(res, {'CANCELLED'})


class AnimLayersPhase2Test(AnimLayersCase):
    """Merge Down, Bake to Base, Duplicate, Extract, Zero Key, weight keys, Empty / Delete Empty (kit steps 194 and
    204: merging and extracting keep the result, a duplicate adds its layer again, zero keys)."""

    FRAMES = list(range(-3, 31, 3)) + [7, 13]

    def snapshot(self, ob):
        return [[round(_evaluated(ob, p, i, frame=f), 4) for p, i in CHANNELS] for f in self.FRAMES]

    def assert_same(self, before, after, label):
        for f, a, b in zip(self.FRAMES, before, after):
            for (p, i), x, y in zip(CHANNELS, a, b):
                self.assertAlmostEqual(x, y, delta=2e-3, msg=f"{label}: {p}[{i}] at frame {f}")

    def stack(self):
        """Base; Layer 1 (Add); Layer 2 (Override 0.5); Layer 3 (Add, weight keyed 0 -> 1)."""
        ob = self.cube()
        l1 = self.new_layer(ob).layer_id
        self.key(ob, "location", 0, 3, 6.0)
        self.key(ob, "rotation_euler", 1, 8, 0.4)
        l2 = self.new_layer(ob, blend_mode='OVERRIDE').layer_id
        self.layer(l2).weight = 0.5
        self.key(ob, "location", 0, 5, 1.0)
        self.key(ob, "scale", 0, 9, 2.5)
        l3 = self.new_layer(ob).layer_id
        self.key(ob, "location", 0, 6, 9.0)
        self.scene.frame_set(1)
        self.layer(l3).weight = 0.0
        bpy.ops.anim_layers.key_weight()
        self.scene.frame_set(10)
        self.layer(l3).weight = 1.0
        return ob, l1, l2, l3

    def test_merge_down_keeps_the_result(self):
        ob, l1, l2, l3 = self.stack()
        before = self.snapshot(ob)
        for lid, into in ((l3, l2), (l2, l1), (l1, model.BASE_ID)):
            name = model.layer_tracks(ob)[lid].strips[0].action.name
            _set_active(self.scene, self.layer(lid))
            self.assertEqual(bpy.ops.anim_layers.merge_down(), {'FINISHED'})
            self.assertIsNone(self.layer(lid))
            self.assertNotIn(lid, model.layer_tracks(ob))
            self.assertNotIn(name, bpy.data.actions)
            self.assertEqual(model.active(self.scene).layer_id, into)
            self.assertEqual(editing.tweaked_layer_id(ob), into)
            self.assert_same(before, self.snapshot(ob), f"after merging into {into}")
        self.assertEqual([l.layer_id for l in model.layers(self.scene)], [model.BASE_ID])

    def test_merging_a_muted_layer_drops_it(self):
        ob, l1, l2, l3 = self.stack()
        self.layer(l3).mute = True
        before = self.snapshot(ob)
        _set_active(self.scene, self.layer(l3))
        self.assertEqual(bpy.ops.anim_layers.merge_down(), {'FINISHED'})
        self.assert_same(before, self.snapshot(ob), "muted layer merged")

    def test_merge_refuses_a_locked_layer(self):
        ob, l1, l2, l3 = self.stack()
        self.layer(l2).lock = True
        _set_active(self.scene, self.layer(l3))
        self.assertEqual(bpy.ops.anim_layers.merge_down(), {'CANCELLED'})
        self.assertIsNotNone(self.layer(l3))

    def test_merge_down_bone_quaternions(self):
        arm = bpy.data.objects.new("Rig", bpy.data.armatures.new("Rig"))
        self.scene.collection.objects.link(arm)
        _select(arm)
        with _view3d():
            bpy.ops.object.mode_set(mode='EDIT')
            b = arm.data.edit_bones.new("arm")
            b.head, b.tail = (0, 0, 0), (0, 0, 1)
            bpy.ops.object.mode_set(mode='OBJECT')
        pb = arm.pose.bones["arm"]
        pb.rotation_mode = 'QUATERNION'
        path = 'pose.bones["arm"].rotation_quaternion'
        for f, q in ((1, Quaternion()), (10, Quaternion((0, 0, 1), math.radians(90)))):
            pb.rotation_quaternion = q
            pb.keyframe_insert("rotation_quaternion", frame=f)
        l1 = self.new_layer(arm).layer_id
        for f, e in ((2, (20, 0, 0)), (9, (0, -30, 10))):
            self.scene.frame_set(f)
            pb.rotation_quaternion = Quaternion(Euler([math.radians(a) for a in e])) @ pb.rotation_quaternion
            pb.keyframe_insert("rotation_quaternion", frame=f)
        l2 = self.new_layer(arm, blend_mode='OVERRIDE').layer_id
        self.layer(l2).weight = 0.5
        self.scene.frame_set(5)
        pb.rotation_quaternion = Quaternion(Euler((0.3, 0.2, -0.4)))
        pb.keyframe_insert("rotation_quaternion", frame=5)
        frames = range(-2, 16)
        before = [Quaternion(_evaluated(arm, path, frame=f)).normalized() for f in frames]
        for lid in (l2, l1):
            _set_active(self.scene, self.layer(lid))
            self.assertEqual(bpy.ops.anim_layers.merge_down(), {'FINISHED'})
            for f, q in zip(frames, before):
                got = Quaternion(_evaluated(arm, path, frame=f)).normalized()
                self.assertLess(math.degrees(got.rotation_difference(q).angle), 0.05, f"frame {f}")

    def test_bake_to_base_then_export(self):
        ob, l1, l2, l3 = self.stack()
        self.new_layer(ob)
        self.key(ob, "location", 0, 4, 30.0)
        model.find(self.scene, model.active(self.scene).layer_id).mute = True     # left out of the bake
        before = self.snapshot(ob)
        self.assertEqual(bpy.ops.anim_layers.bake_to_base('EXEC_DEFAULT'), {'FINISHED'})
        ad = ob.animation_data
        self.assertFalse(model.layer_tracks(ob))
        self.assertEqual(len(ad.nla_tracks), 0)
        self.assertFalse(ad.use_tweak_mode)
        self.assertIsNotNone(ad.action)
        self.assertNotIn(model.MARK, ad.action)
        self.assertEqual(len(model.layers(self.scene)), 0)
        self.assert_same(before, self.snapshot(ob), "baked to Base")
        # The point of baking: the exporter's default mode now carries the result.
        path = os.path.join(tempfile.mkdtemp(), "baked.glb")
        _select(ob)
        bpy.ops.export_scene.gltf(filepath=path, export_format='GLB', use_selection=True)
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        bpy.ops.import_scene.gltf(filepath=path)
        imported = next(o for o in self.scene.objects if o.animation_data is not None)
        for f, row in zip(self.FRAMES, before):
            if 1 <= f <= 24:
                self.scene.frame_set(f)
                x = imported.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation.x
                self.assertAlmostEqual(x, row[0], delta=2e-3, msg=f"glTF location.x at {f}")
        # A new layer later starts from the baked Action again.
        imported.name = "Again"
        self.new_layer(imported)
        self.assertTrue(model.is_layered(imported))

    def test_duplicate_layer(self):
        ob = self.cube()
        lid = self.new_layer(ob).layer_id
        self.key(ob, "location", 0, 1, 4.0)            # the layer adds 4 at every frame
        before = _evaluated(ob, "location", 0, frame=6)
        self.assertEqual(bpy.ops.anim_layers.duplicate(with_keys=True), {'FINISHED'})
        copy = model.active(self.scene)
        self.assertEqual(copy.name, "Layer 1 copy")
        self.assertEqual([l.layer_id for l in model.layers(self.scene)][1:], [lid, copy.layer_id])
        self.assertEqual(editing.tweaked_layer_id(ob), copy.layer_id)
        self.assertAlmostEqual(_evaluated(ob, "location", 0, frame=6), before + 4.0, delta=TOL)
        _set_active(self.scene, self.layer(lid))
        self.assertEqual(bpy.ops.anim_layers.duplicate(with_keys=False), {'FINISHED'})
        empty = model.active(self.scene)
        self.assertEqual(empty.name, "Layer 1 copy 2")
        self.assertEqual([l.layer_id for l in model.layers(self.scene)][1:3], [lid, empty.layer_id])
        self.assertEqual(_curve(ob, empty.layer_id, "location", 0), None)
        self.assertAlmostEqual(_evaluated(ob, "location", 0, frame=6), before + 4.0, delta=TOL)

    def test_extract_selected_objects(self):
        a, b = self.cube("A"), self.cube("B")
        lid = self.new_layer(a, b, blend_mode='OVERRIDE').layer_id
        self.layer(lid).weight = 0.7
        self.key(a, "location", 0, 4, 12.0)
        self.key(b, "location", 0, 4, -6.0)
        before = (self.snapshot(a), self.snapshot(b))
        _select(a)
        self.assertEqual(bpy.ops.anim_layers.extract(), {'FINISHED'})
        new = model.active(self.scene)
        self.assertEqual(new.name, "Layer 1 extract")
        self.assertEqual((new.blend, round(new.weight, 3)), ('OVERRIDE', 0.7))
        self.assertIn(new.layer_id, model.layer_tracks(a))
        self.assertNotIn(lid, model.layer_tracks(a))
        self.assertIn(lid, model.layer_tracks(b))
        self.assert_same(before[0], self.snapshot(a), "extracted object")
        self.assert_same(before[1], self.snapshot(b), "object left in the layer")

    def test_zero_key(self):
        ob = self.cube()
        self.new_layer(ob)
        self.key(ob, "location", 0, 1, 5.0)            # Add layer: +5 everywhere
        base_at_10 = 10.0
        _select(ob)
        self.scene.frame_set(10)
        self.assertEqual(bpy.ops.anim_layers.zero_key(), {'FINISHED'})
        self.assertAlmostEqual(_evaluated(ob, "location", 0, frame=10), base_at_10, delta=TOL)
        self.assertAlmostEqual(_evaluated(ob, "location", 0, frame=1), 5.0, delta=TOL)
        # Override layer: a zero key shows what's below (Base, since the Add layer is zero there).
        over = self.new_layer(ob, blend_mode='OVERRIDE').layer_id
        self.key(ob, "location", 0, 1, -20.0)
        self.scene.frame_set(10)
        self.assertEqual(bpy.ops.anim_layers.zero_key(), {'FINISHED'})
        self.assertAlmostEqual(_evaluated(ob, "location", 0, frame=10), base_at_10, delta=TOL)
        self.layer(over).lock = True
        self.assertEqual(bpy.ops.anim_layers.zero_key(), {'CANCELLED'})

    def test_key_weight_at_0_and_1(self):
        ob = self.cube()
        lid = self.new_layer(ob).layer_id
        self.key(ob, "location", 0, 1, 3.0)
        self.scene.frame_set(2)
        self.assertEqual(bpy.ops.anim_layers.key_weight(value='ZERO'), {'FINISHED'})
        self.scene.frame_set(12)
        self.assertEqual(bpy.ops.anim_layers.key_weight(value='ONE'), {'FINISHED'})
        fc = model.layer_tracks(ob)[lid].strips[0].fcurves.find("influence")
        self.assertEqual([(round(k.co.x), round(k.co.y, 3)) for k in fc.keyframe_points], [(2, 0.0), (12, 1.0)])

    def test_empty_and_delete_empty(self):
        ob = self.cube()
        keep = self.new_layer(ob).layer_id
        self.key(ob, "location", 0, 2, 4.0)
        gone = self.new_layer().layer_id                 # no objects
        third = self.new_layer(ob).layer_id
        _set_active(self.scene, self.layer(third))
        self.assertEqual(bpy.ops.anim_layers.empty(), {'FINISHED'})
        self.assertNotIn(third, model.layer_tracks(ob))
        self.assertEqual(bpy.ops.anim_layers.delete_empty(), {'FINISHED'})
        self.assertEqual([l.layer_id for l in model.layers(self.scene)], [model.BASE_ID, keep])
        self.assertIsNone(self.layer(gone))
        self.assertEqual(bpy.ops.anim_layers.delete_empty(), {'CANCELLED'})

    def test_ui_icons_exist(self):
        import inspect
        import re
        from bl_ext.system.anim_layers import ui
        icons = set(bpy.types.UILayout.bl_rna.functions["operator"].parameters["icon"].enum_items.keys())
        used = set(re.findall(r"icon='([A-Z0-9_]+)'", inspect.getsource(ui)))
        self.assertFalse(used - icons, f"unknown icons {used - icons}")


class AnimLayersPhase3Test(AnimLayersCase):
    """Layer colours, other layers' curves in the Graph Editor and picking them, the 3D View hint (kit step 204:
    the Graph Editor lines of other layers, normalized lines, picking a drawn curve)."""

    def two_layers(self):
        a, b = self.cube("A"), self.cube("B")
        l1 = self.new_layer(a, b).layer_id
        self.key(a, "location", 0, 3, 7.0)
        self.key(a, "rotation_euler", 1, 6, 0.5)
        self.key(b, "location", 0, 4, -2.0)
        l2 = self.new_layer(a, b).layer_id
        self.key(a, "location", 0, 8, 1.0)
        return a, b, l1, l2

    def test_layer_colours(self):
        from bl_ext.system.anim_layers import model as m
        ob = self.cube()
        l1 = self.new_layer(ob).layer_id
        l2 = self.new_layer(ob).layer_id
        rounded = [tuple(round(c, 3) for c in self.layer(lid).color) for lid in (m.BASE_ID, l1, l2)]
        self.assertEqual(rounded, [m.BASE_COLOR, m.PALETTE[0], m.PALETTE[1]])
        bpy.ops.anim_layers.duplicate()
        self.assertEqual(tuple(round(c, 3) for c in model.active(self.scene).color), m.PALETTE[2])
        _set_active(self.scene, self.layer(l1))
        bpy.ops.anim_layers.delete()
        self.new_layer(ob)
        self.assertEqual(tuple(round(c, 3) for c in model.active(self.scene).color), m.PALETTE[0], "a freed colour")

    def test_graph_lines_of_layers_with_show_curves(self):
        from bl_ext.system.anim_layers import overlays
        a, b, l1, l2 = self.two_layers()
        self.assertEqual(model.active(self.scene).layer_id, l2)
        self.assertEqual(overlays.lines(self.scene, [a, b], 1, 24, 23), [], "nothing shown yet")
        self.layer(l1).show_curves = True
        self.layer(l2).show_curves = True              # the active layer is Blender's own: never drawn by us
        items = overlays.lines(self.scene, [a, b], 1.0, 24.0, 23)
        self.assertEqual({(ob.name, lid) for _c, ob, lid, _p in items}, {("A", l1), ("B", l1)})
        self.assertEqual(len(items), 3, "A: location.x + rotation Y, B: location.x")
        color = tuple(self.layer(l1).color)
        for rgba, ob, lid, pts in items:
            self.assertEqual(len(pts), 24)
            self.assertEqual(rgba[:3], color)
            self.assertLess(rgba[3], 1.0)
            curves = [fc for fc in bpy.data.actions[f"Layer 1_{ob.name}"].layers[0].strips[0].channelbags[0].fcurves]
            self.assertTrue(any(all(abs(y - fc.evaluate(x) * overlays.value_factor(self.scene, ob, fc)) < 1e-4
                                    for x, y in pts) for fc in curves), (ob.name, pts[:3]))
        rot = [p for _c, ob, _l, p in items if ob.name == "A" and max(abs(y) for _x, y in p) > 5]
        self.assertTrue(rot, "the rotation curve is drawn in degrees")
        _set_active(self.scene, self.layer(l1))
        self.assertEqual({lid for _c, _ob, lid, _p in overlays.lines(self.scene, [a, b], 1, 24, 23)}, {l2})
        active = overlays.lines(self.scene, [a], 1, 24, 23, active=True)
        self.assertEqual({lid for _c, _ob, lid, _p in active}, {l1}, "picking compares with the active layer")

    def test_normalized_lines_span_minus_one_to_one(self):
        from bl_ext.system.anim_layers import overlays
        a, _b, l1, l2 = self.two_layers()
        _set_active(self.scene, self.layer(l1))
        self.key(a, "location", 0, 12, -3.0)           # Layer 1's location.x now has two different keys
        _linear(a)
        _set_active(self.scene, self.layer(l2))
        self.layer(l1).show_curves = True
        items = [i for i in overlays.lines(self.scene, [a], 1.0, 24.0, 230, normalize=True) if i[1] == a]
        ys = [y for _c, _ob, _l, pts in items for _x, y in pts]
        self.assertAlmostEqual(min(ys), -1.0, delta=1e-3)
        self.assertAlmostEqual(max(ys), 1.0, delta=1e-3)

    def test_hidden_and_unselected_bone_curves_are_left_out(self):
        from bl_ext.system.anim_layers import overlays
        a, b, l1, l2 = self.two_layers()
        self.layer(l1).show_curves = True
        for fc in bpy.data.actions["Layer 1_A"].layers[0].strips[0].channelbags[0].fcurves:
            fc.hide = True
        self.assertEqual({ob.name for _c, ob, _l, _p in overlays.lines(self.scene, [a, b], 1, 24, 23)}, {"B"})
        arm = bpy.data.objects.new("Rig", bpy.data.armatures.new("Rig"))
        self.scene.collection.objects.link(arm)
        _select(arm)
        with _view3d():
            bpy.ops.object.mode_set(mode='EDIT')
            for name, z in (("one", 0), ("two", 1)):
                bone = arm.data.edit_bones.new(name)
                bone.head, bone.tail = (0, 0, z), (0, 0, z + 1)
            bpy.ops.object.mode_set(mode='OBJECT')
        for pb in arm.pose.bones:
            pb.keyframe_insert("location", index=0, frame=1)
        _set_active(self.scene, self.layer(l1))
        bpy.ops.anim_layers.add_objects()
        self.scene.frame_set(5)
        for pb in arm.pose.bones:
            pb.location.x = 1.0
            pb.keyframe_insert("location", index=0, frame=5)
        _set_active(self.scene, self.layer(l2))
        with _view3d():
            bpy.ops.object.mode_set(mode='POSE')
        for pb in arm.pose.bones:
            pb.select = pb.name == "two"
        drawn = overlays.lines(self.scene, [arm], 1, 24, 23, only_selected=True)
        self.assertEqual(len(drawn), 1, "only the selected bone's curve")
        self.assertEqual(len(overlays.lines(self.scene, [arm], 1, 24, 23)), 2)
        with _view3d():
            bpy.ops.object.mode_set(mode='OBJECT')

    def test_picking_a_drawn_curve(self):
        from bl_ext.system.anim_layers import overlays
        a, b, l1, l2 = self.two_layers()
        self.layer(l1).show_curves = True
        items = overlays.lines(self.scene, [a], 1.0, 24.0, 23)
        mine = overlays.lines(self.scene, [a], 1.0, 24.0, 23, active=True)

        def to_region(x, y):
            return x * 10.0, y * 10.0
        x, y = items[0][3][5]
        self.assertEqual(overlays.hit(items, to_region, x * 10.0, y * 10.0 + 3), (a, l1))
        self.assertIsNone(overlays.hit(items, to_region, x * 10.0, y * 10.0 + 30), "too far")
        mx, my = mine[0][3][5]
        self.assertIsNone(overlays.hit(items, to_region, mx * 10.0, my * 10.0, limit=1e9, active_items=mine),
                          "a click on the active layer's curve stays with Blender")

    def test_3d_view_hint(self):
        from bl_ext.system.anim_layers import overlays
        a, b, l1, l2 = self.two_layers()
        self.assertEqual(overlays.hint_text(self.scene, a), ("Keys go on Layer 2", tuple(self.layer(l2).color)))
        self.layer(l2).lock = True
        self.assertEqual(overlays.hint_text(self.scene, a)[0], "Layer 2 is locked: no keys go on it")
        loose = bpy.data.objects.new("Loose", None)
        self.scene.collection.objects.link(loose)
        self.assertIsNone(overlays.hint_text(self.scene, loose))


if __name__ == "__main__":
    # Drop all arguments before "--", or everything if the delimiter is absent. Keep the executable path.
    unittest.main(argv=sys.argv[:1] + (sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []))
