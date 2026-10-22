# Phantom changelog

Phantom is a fork of Blender tuned for sculpting. This file lists what Phantom changes; Blender's
own changes come from the Blender version each release is built on.


## 0.2.2 alpha (2026-10-22)

Built on Blender 5.4 alpha.

### For ZBrush users

- **Face sets are called polygroups** everywhere in the interface: menus, tools, tooltips,
  brush names, reports and search ("polygroup" finds them). Files, scripts and the Python API
  still use Blender's names (`face_sets`), so nothing breaks.
- **Ctrl+D divides** in Sculpt Mode: it subdivides one level with a Multires modifier, adding the
  modifier (first in the stack) when the object has none, and sculpts on the new level. Undo
  steps back a level. Also in Sculpt > Divide (Multires). In the Industry Compatible keymap it
  replaces Ctrl+D voxel remesh / detail flood fill. It's an option in Preferences > Keymap
  ("Ctrl-D Divides").
- **Mask actions bar:** while the mesh has a mask or hidden parts, a small bar slides up at the
  bottom of the viewport with **Hide Masked**, **Unhide**, **Split Hidden** and **Extract**.
  Split Hidden separates the hidden part into its own object (named "<object> Split"), like
  ZBrush, and keeps sculpting the visible part; it is also in the Sculpt menu, and one undo puts
  the mesh back together. Extract is Blender's Mask Extract, with its settings.
- **Customize the bar:** its **+** button (or right-clicking it) lists more mask actions to add
  or remove: Slice, Invert, Clear, Smooth, Sharpen, Grow, Shrink, Contrast +/- and To
  Polygroup. The choice is remembered between sessions.
- **Polygroup bar:** after Ctrl+W turns the mask into a polygroup, the bar switches to
  polygroup actions for that polygroup: **Isolate** (again: swap), **Hide**, **Unhide** and
  **Split Hidden**, with Extract, Delete, Fair, Grow, Shrink, New Colors and PolyFrame to add.
  It is customized separately from the mask bar; its **x** puts it away, and masking again
  brings back the mask bar.

### New: Garment workspace (early preview)

A Marvelous Designer-style workspace for making clothes, simulated with Blender's cloth. Add
it from the workspace **+** menu (Garment). A flat pattern view sits beside the 3D view.

- **Patterns:** Rectangle, Ellipse and Polygon tools (corners and curve points), and Edit
  Points to drag, add or delete points and switch them between corner and curve.
- **Sewing:** sew edge segments, Free Sew for part of an edge, and elastic segments.
- **Arrange:** click points on the avatar (chest, hips, arms, legs) to hang pieces from the
  shoulders, waist, a shoulder or the crotch; **Add Mannequin** gives a default body.
- **Fabric:** pins, a fabric per piece (Cotton, Silk, Jersey, Denim, Leather), particle
  distance and sewing force.
- **Space** simulates (seams close before gravity eases in). **Make Sculptable** bakes a welded,
  thickened mesh and switches to the Sculpting workspace.


## 0.2.1 alpha (2026-10-22)

A hotfix on top of 0.2.0, built on Blender 5.4 alpha.

### Keymap options

- **Phantom's sculpt controls are now options** in Preferences > Keymap, under "Phantom
  Sculpting". Each one can be switched off to get the keymap's original keys back:
  - ZBrush Masking (Ctrl / Ctrl+Alt mask, Alt inverts the brush)
  - ZBrush Face Sets (Ctrl+W mask to face set, Ctrl+Shift-click isolate, Ctrl+F PolyFrame)
  - Ctrl+Shift Trim
  - X Toggles Symmetry
  - Move / Transpose Keys
  - C Samples Paint Color
  - Quick Brush Keys Take Over (a quick brush's shortcut switches off other Sculpt shortcuts on
    that key)
- **Industry Compatible keymap:** Phantom's sculpt controls work there too. X toggles X symmetry;
  masking uses Ctrl and Ctrl+Alt (Alt navigates in this keymap) and the brush inverts with
  Ctrl+Shift+Alt; Ctrl+W, Ctrl+Shift-click and Ctrl+F work as in the default keymap, with the face
  sets pie on Alt+W; W, E and R are Move, Rotate and Scale with the gizmo or Transpose; C samples
  paint color.

### Fixes and polish

- **The silhouette thumbnail shows only geometry.** Cameras, lights and the floor grid no longer
  appear in it.
- **Tabs pop up with a small bounce when hovered** and settle back when the cursor leaves
  (workspace tabs, Properties editor tabs, sidebar tabs).


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

- **ZBrush-style matcaps** pair well with these tools: get Mandarin's
  [ZBrush Style Matcaps for Blender](https://mandarin.gumroad.com/l/zbrush-style-matcaps-for-blender)
  ($2, or free with the code FREE) and install them in Preferences > Lights > MatCaps > Install.
  (The first 0.2.0 download bundled them; they were removed, as they're Mandarin's to share.)
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
