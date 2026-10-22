# Phantom changelog

Phantom is a fork of Blender tuned for sculpting. This file lists what Phantom changes; Blender's
own changes come from the Blender version each release is built on.


## 0.2.0 alpha (2026-10-22)

Built on Blender 5.4 alpha.

### New: Transpose, and Move/Rotate/Scale in Sculpt Mode

- **W** in Sculpt Mode is now Move (it used to show "Tool 'builtin.select_box' not found").
- **Five icons in the middle of the tool header:** Move, Rotate and Scale, then Gizmo or Transpose.
  Switching to any other tool hides the gizmo or action line.
- **Transpose tool** (new toolbar icon, after ZBrush's Transpose):
  - Click-drag on the model to draw an action line; its ends snap to the surface under them.
  - Drag an end's outer ring to place that end, or the middle ring (or the line) to move the line.
  - Drag an inner circle to act on the unmasked model (orange: Move, green: Scale, red: Rotate):
    - Move: the middle moves it (Shift: along the line); an end stretches/shears it.
    - Scale: an end scales it about the other end; the middle thickens or thins it across the line.
    - Rotate: an end rotates it about the other end; the middle twists it around the line.
  - Masks, symmetry and undo work as with Blender's transform tools; Transpose never moves the
    object origin or the sculpt pivot.

### New: matcaps

- **54 ZBrush-style matcaps** included ("ZB ..." in the matcap list), by Mandarin:
  [ZBrush Style Matcaps for Blender](https://mandarin.gumroad.com/l/zbrush-style-matcaps-for-blender).
- **Search inside the matcap grid:** type straight away to filter it (fuzzy matching); also a
  search button next to the matcap thumbnail.
- **Favorites:** right-click a matcap > Add to Favorites; favorites get a gold star in the grid,
  and a star toggle next to the search filters the grid to favorites (it keeps the grid open).
- **Set as Default Matcap** (right-click): new projects start with it, and files still on
  Blender's default matcap switch to it.
- **Quick Matcaps:** four clickable slots with thumbnails in the Shading popover; right-click a
  matcap > Add to Quick Slot, or right-click a slot to replace or clear it.
- **"new!" badges:** matcaps you add get a green frame and tab until you use them.

### New: Sculpt Mode

- **Ctrl+W** turns the masked area into a face set and clears the mask (replaces Grow Face Set).
- **Ctrl+Shift-click** isolates the face set under the cursor, swaps to the other face sets when
  clicking an isolated one, and shows everything when clicking off the mesh. Trimming moved to
  **Ctrl+Shift-drag**.
- **Quick-brush shortcuts take over their key:** giving a quick brush a shortcut switches off any
  other Sculpt shortcut on that key (for example C for Clay Strips), and brings it back when the
  quick-brush shortcut is removed.
- **C samples color** from the mesh's own color data while a paint or smear brush is active.
- **Masks stay visible with viewport overlays off** (no more "Viewport overlays are disabled").
- **Properties editor in Sculpt Mode** shows only Render, Output, Scene, World, Object,
  Modifiers, Data, Material, Texture and a new Sculpting tab (coming soon); the full list comes
  back in other modes.

### New: viewport

- **Limited grid** (Overlays > Guides > Infinite / Limited): a square, Maya-style grid around the
  origin with Grid Size and an Axis Colors option matching the infinite floor's colors. The
  Guides panel is reordered: Grid, Floor and Axes; Infinite/Limited; Scale and Subdivisions;
  Grid Size and Axis Colors; then 3D Cursor and Annotations.
- **Vertex hover:** in Edit Mode with vertex selection, the vertex a click would select grows a
  little and pulses (same pick distance and occlusion as click-select).
- **Halloween edit-mode colors:** pumpkin-orange selection, witch-purple active element,
  purple-black unselected cage, slime-green seams, purple sharp edges (also in the UV editor).
  Existing Phantom themes still on the old edit colors are updated once.

### New: sounds

- Loop cut: placing a loop plays the standard soft confirm; adding or removing loops with the
  wheel plays soft rising and falling "plips".

### Changes

- The navigation gizmo's side label (Front, Right...) uses the viewport text style.
- The statistics sit right under a docked silhouette thumbnail again.
- The silhouette thumbnail stays below transparent headers when docked.
- Matcap right-click menu is narrower (quick slots moved into a submenu).

### Fixes

- Transpose: dragging an end circle in Move mode raised a Python error.
- Transpose: drags didn't record undo steps; the "Affect Only Locations" setting is no longer
  switched off by Transpose.
- Clicking the favorites filter in the matcap grid could crash.


## 0.1.0 alpha (2026-10-21)

The first public release, built on Blender 5.4 alpha.

### Look and feel

- Phantom name, ghost icon, splash and separate settings folder (`%APPDATA%\Phantom`).
- Charcoal theme with blue-purple accents and periwinkle selection; 3 px outlines, 6 px vertices,
  2 px edges.
- Cozy interface sounds (Preferences > Interface > Sounds): clicks, toggles, menus,
  confirm/cancel, grab and place, undo/redo, typing, outliner, loop cut, edge slide, knife, mode
  switch, remesh, voxel size measuring tick, applying modifiers.
- Motion: tool icons collapse and cascade when switching modes; popup menus fade and grow in;
  dropdowns and submenus slide out; the N sidebar slides.
- Navigation gizmo label showing which side you face.

### Sculpt Mode

- Silhouette thumbnail (live, black on white): move, resize, dock next to the tools; on and docked
  for new users.
- ZBrush-style masking with Ctrl and Ctrl+Alt (brush, box mask, clear, invert, smooth, sharpen),
  and empty-space Ctrl-drag voxel remesh.
- Sidebar on the left with Brush Asset and four quick-brush slots (drag in/out, right-click
  replace/remove/shortcut); live brush preview for Shift/Ctrl/trim.
- Focal Shift slider with a matching cursor ring; Stabilize Stroke toggle; X for symmetry;
  Ctrl+Shift for the selected trim tool; Ctrl+F PolyFrame; asset shelf toggle.
- Quad Remesh (AutoRemesher) with its own dropdown and saved settings.
- File > New > Sculpting faces the front of the sphere.

### Viewport

- Per-object matcaps.
