# Animation Layers

Layer animation on top of your keys. Your original animation is **Base**; each layer above it adds to it
(**Add**) or replaces it as much as its weight (**Override**). Layers have a **weight** (which can be keyed),
**mute**, **solo** and **lock**. It's in the sidebar of the Dope Sheet, Timeline and Graph Editor ("Layers" tab)
and of the 3D View ("Animation" tab), and the Dope Sheet / Timeline header shows the layer keys go on.

1. Select what you want to adjust and click **New Layer** (+). The new layer is active.
2. Pose and key as usual (I, Auto Keying): the keys go on the active layer, and Base doesn't change.
3. Drag **Weight** to dial the layer in or out; key it to fade it over time.
4. **Mute** to compare, **Solo** to see one layer over Base, **Lock** to protect a layer.

Keys on an object that isn't in the active layer go on its Base. A locked or muted layer takes no keys.

The ⌄ menu next to the list has the rest:

- **Duplicate Layer** (with or without its keys) and **Extract Selected Objects** (their keys move to a new
  layer right above; nothing moves on screen).
- **Merge Down**: the active layer goes into the layer below it, keyed on every frame so nothing moves.
- **Key Weight** (as it is, at 0 or at 1) and **Zero Key** (a "no change" key on the active layer).
- **Empty Layer** and **Delete Empty Layers**.
- **Bake to Base**: every layer is baked into each object's own animation and the layers are removed, ready
  to export. Muted layers are left out.

## Seeing layers

Each layer has a colour (the swatch on its row). In the Graph Editor's Layers tab, the graph button on a row
draws that layer's curves behind the active layer's, see-through in its colour; click one of those curves to make
that layer active. The 3D View shows which layer keys go on at its bottom left in Object and Pose modes (turn it
off in the ⌄ menu: Show Layer in 3D View).

## Exporting

After **Bake to Base**, any exporter and mode works. To export without baking, use one that samples the
scene: glTF with Animation > Mode set to **Scene**, or FBX with Animation > **NLA Strips** and **All
Actions** unticked. Other exporter modes write each layer on its own.

## How it works

Each layer is an NLA track on every object in it, playing that layer's own Action (Add = Combine, Override =
Replace, weight = strip influence). Layered objects stay in NLA tweak mode on the active layer with the tracks
above still evaluated, so Blender solves each key through the whole stack. That needs Phantom's
`AnimData.use_tweak_mode_upper_stack` and its locked / muted layer keying checks. Files keep plain NLA data, so
they play the same in Blender.

## Credit and license

The layer model and math are based on the animation layers of
[Maya Workflow Kit for Blender](https://github.com/5hyuns/maya-workflow-kit-for-blender) by ohnsk
(commit bd7ee04, 0.6.2), GPL-3.0-or-later, modified for Phantom from 2026-10-28. This extension is
GPL-3.0-or-later too; see `LICENSE`.
