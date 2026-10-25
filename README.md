![Phantom: a Blender fork for sculptors](release/datafiles/splash.png)

Phantom
=======

Phantom is a fork of [Blender](https://www.blender.org) tuned for sculpting: ZBrush-style
masking, polygroups and Transpose, a live silhouette preview, quick brushes, a charcoal look with cozy interface sounds, and many smaller quality-of-life changes.
Everything else in Blender is still there and works as before.

**[Download Phantom 0.4.2](https://github.com/Potassiums/phantom/releases/latest)** (Windows, 64-bit).
Unzip it anywhere and run `phantom.exe`; nothing needs installing. Phantom keeps its settings in
`%APPDATA%\Phantom`, apart from any Blender install.

Phantom is in **alpha** and follows Blender's in-development code (currently 5.4): save often.

## Features
- **Start with a base mesh:** new files start empty; Sculpting asks what to start with: a quad
  sphere or one of Blender's free human base meshes (bodies, heads and more), bundled.
- **actions bar:** When masking or isolating polygroups a new contextual bar appears relating to masks or polygroups giving
  users the ability to quickly hide, extract, or split
- **Polygroups:** face sets are called polygroups throughout the interface, like ZBrush.
- **Zbrush style hotkeys** Sculpting mode now sports Zbrush style hotkeys for masking, polygroups, and more.
- **Garment workspace (experimental, very buggy):** Marvelous Designer-style patterns, sewing and
  cloth simulation (live on the GPU with NVIDIA Newton), with its own **Garment Mode** (Pin and
  Drag tools) and a Garment tab in the Properties editor. Expect bugs and crashes, and save often.
- **Transpose:** ZBrush's action line for moving, scaling and rotating the unmasked model, plus
  Move/Rotate/Scale icons (gizmo or Transpose) in the middle of the Sculpt tool header.
- **Matcap library tools:** search inside the matcap grid, favorites
  with gold stars, a default matcap for every project, four quick slots and "new!" badges.
- **Silhouette thumbnail:** a live black-on-white view of the scene, docked in the top-left
  corner with the viewport's aspect ratio. Move, resize and dock it; it follows the sidebar.
- **ZBrush-style masking:** Ctrl-drag to mask (off the mesh: a box, lasso or circle mask, chosen
  at the top of Brush Settings while masking; a box over empty space clears the mask or voxel
  remeshes), Ctrl-Alt-drag to erase, Ctrl-click to smooth (on the mesh) or invert (off
  it), Ctrl-Alt-click to sharpen. Alt inverts the brush.
- **Masks stay visible** with viewport overlays off.
  isolates a polygroup, swaps on an isolated one and shows everything off the mesh; Ctrl+F
  toggles polygroups plus wireframe (PolyFrame).
- **Transpose:** ZBrush-style action line. Draw it on the model; drag its rings to place it and
  its inner circles to move, stretch, scale, thicken or thin, rotate or twist the unmasked model.
  Each circle is labelled with what it does; hold Shift to snap rotations, and the line's
  direction while placing it, to 15 degree steps. Masks, symmetry and undo work as expected.
- **ZBrush import:** File > Import > ZBrush (.ztl, .zpr) brings in every subtool's geometry as
  its own object, standing up in Blender's axes.
- **Quad Remesh** (powered by [AutoRemesher](https://github.com/huxingyi/autoremesher))
  with its own dropdown next to Remesh; settings saved with the file.
- **Dynamic remesher (experimental):** a second remesher next to OpenVDB, picked in the Remesh
  panel and used by Ctrl+R too: evenly spaced quads, small holes closed, overlapping shells
  merged, detail projected back.
- **ClayPolish (experimental):** smooth the whole surface at once while keeping creases crisp
  (Properties > Sculpting).
- **Sculpt history (experimental):** every sculpt edit of an object is recorded; scrub back
  through them on a timeline docked to any side of the viewport. History in the Sculpt Mode
  header shows it and holds its settings.
- **Painter's color picker:** brush colors use a hue ring around a saturation/brightness
  triangle, at the top of Brush Settings.
- **Brushes:** the sidebar moves next to the tools with the Brush Asset panel (tool icon and name
  on top) and four quick-brush slots: drag brushes in or out, right-click to replace, remove or
  give a shortcut. A quick-brush shortcut takes over its key (for example C instead of Clay
  Strips).

Building
--------

Phantom builds like Blender: see Blender's
[build instructions](https://developer.blender.org/docs/handbook/building_blender/). On Windows,
`make.bat ninja` in the repository builds `phantom.exe`.

When cloning from GitHub, set `GIT_LFS_SKIP_SMUDGE=1` for the initial clone to avoid Git LFS
errors, then get Blender's large files as described in Blender's
[cloning instructions](https://developer.blender.org/docs/handbook/contributing/using_git/clone_update/#github-mirror). Phantom's branding, theme, sound and
icon generators are in [tools/phantom/branding](tools/phantom/branding).


Credits
-------

- [Blender](https://www.blender.org) by the Blender Foundation and its contributors.
- [AutoRemesher](https://github.com/huxingyi/autoremesher) by Jeremy Hu (MIT), used by Quad
  Remesh.
- [Human Base Meshes](https://www.blender.org/download/demo-files/#assets) (CC0) by Blender
  Studio, bundled as starting meshes.
- [Nvidia Newton Physics](https://github.com/newton-physics/newton) and
  [NVIDIA Warp](https://github.com/NVIDIA/warp) (Apache-2.0) for cloth simulation (not bundled:
  downloaded from the Garment sidebar).
- Recommended [ZBrush Style Matcaps for Blender](https://mandarin.gumroad.com/l/zbrush-style-matcaps-for-blender)
  free with the code **FREE**.


License
-------

Phantom, like Blender, is licensed under the GNU General Public License, Version 3.
Individual files may have a different but compatible license.

See [blender.org/about/license](https://www.blender.org/about/license) for details.
