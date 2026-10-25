# Phantom changelog

Phantom is a fork of Blender tuned for sculpting. This file lists what Phantom changes; Blender's
own changes come from the Blender version each release is built on.


## 0.4.2 alpha (2026-10-25)

Built on Blender 5.4 alpha, on top of 0.4.1. An incremental update to sculpt history, which is
**still experimental** and still being tested.

- **History follows undo and redo:** Ctrl+Z and Ctrl+Shift+Z move the timeline along with the
  mesh, all the way back to the start of the history and forward again.
- **Sculpting on an older step asks first:** a stroke on an older step of the timeline is held
  back, and a question slides out from under the timeline: **Keep Sculpting** deletes the later
  steps right away (they can't be redone) and carries on from there; **Cancel** leaves everything
  as it was.
- **Undo stays in step with the timeline** through any mix of strokes, undo, redo, jumps on the
  timeline and Keep Sculpting. Before, Keep Sculpting dropped one undo step too many (Ctrl+Z then
  skipped a stroke or left Sculpt Mode), and after jumping back on the timeline, undo could bring
  back a shape whose steps had been deleted, so the timeline stopped following.
- **History settings are all in the History dropdown and apply to every object:** **Record**,
  the **Limit** of steps kept, and **Save in File**. The per-object record option is gone.
  **Delete History** (in red) asks before deleting the active object's history.

## 0.4.1 alpha (2026-10-24)

Built on Blender 5.4 alpha, on top of 0.4.0.

- **Sculpt history (still experimental) is on by default:** every mesh object starts recording
  with its first sculpt edit, nothing to turn on (switch it off in the History dropdown).
- **History in the Sculpt Mode header:** its checkbox shows or hides the timeline, and its
  dropdown holds all the history settings: where the timeline is docked (**Top**, **Bottom**,
  **Left** or **Right** of the viewport, running across or down), **Show Timeline**,
  **Record**, and the object's own history (record it, step limit, save in the file, delete).
  The History panel in Properties > Sculpting is gone. **View > History Timeline** also shows
  or hides it.
- **The timeline** is always there in Sculpt Mode (it says when there's no history yet), labels
  its **Start** and **End**, and glides out of the way as the toolbar, sidebar or asset shelf
  open and close.
- **Brush size is on S** in Sculpt Mode, like ZBrush's Draw Size (F no longer changes it; S no
  longer picks the Smooth brush, Shift smooths). Shift-F is still strength.
- **A painter's color picker for brushes:** a hue ring around a saturation and brightness
  triangle (pure color pointing right, white at the top, black at the bottom). Drag the ring for
  the hue and the triangle for the shade; a drag keeps editing what it started on. Used for
  brush colors only, in Brush Settings and the color popup.
- **The color picker leads Brush Settings** for brushes that paint color, with the primary and
  secondary colors right under it; the separate Color Picker panel is merged in.


## 0.4.0 alpha (2026-10-24)

Built on Blender 5.4 alpha, on top of 0.3.2.

### Experimental

These three are new and **experimental**: expect rough edges, and save often. Each is off or
optional unless you choose it.

- **Dynamic remesher** (experimental): a new remesher next to OpenVDB. Pick it with **Method**
  at the top of the Remesh panel (sculpt header, or Properties > Data > Remesh); the choice is a
  preference, so it stays the same for every object and file, and Ctrl+R and box-masking empty
  space use it too. It builds a narrow-band distance field, closes holes and gaps up to
  **Close Radius** cells wide, merges overlapping shells into one, extracts an evenly spaced,
  quad-only, watertight surface with dual contouring, and projects it back onto the original
  for detail smaller than the **Edge Length** (faded out across creases). The result is the
  same on any number of threads. Not yet: progressive preview while remeshing.
- **ClayPolish** (experimental): Properties > Sculpting > ClayPolish polishes the whole mesh at
  once, typically right after a remesh: it smooths away noise and stair-stepping without
  shrinking (tangential and Taubin smoothing), keeps creases by their angle (**Angle
  Preservation**, **Angle Ratio**), restores the edges the polish softened (**Sharpness**,
  **Softness**), and can inflate convex or concave areas afterwards. Masked and hidden parts
  don't move. Settings are saved with the file. Not with Dyntopo or Multires.
- **Sculpt history and timeline** (experimental): turn on **History** in Properties >
  Sculpting to record every sculpt edit of the object (strokes, filters, remeshes, ClayPolish).
  In Sculpt Mode a timeline at the top of the viewport shows the steps (orange ticks where the
  topology changed); click or drag it to scrub, and the mesh follows exactly. Sculpting after
  going back drops the steps after it. Set a **Limit** (100 steps by default; older steps are
  deleted past it), choose whether the history is **saved in the file**, pause it, or delete
  it. Only moved vertices are stored between steps, so it stays small. Not with Dyntopo or
  Multires; scrubbing across a remesh drops attributes like UVs, as the remesh did. Ctrl+Z is
  still Blender's undo.

### Other changes

- **Stroke, Alpha and Texture thumbnails in the Brush Asset panel:** next to the brush
  thumbnail, like ZBrush's brush tray. Each shows the brush's current stroke method, alpha
  image or color texture; click one to open its settings and change it. In Sculpt Mode the
  Alpha is the brush texture and the Texture tile is greyed out, since sculpt brushes have no
  color texture; in Texture Paint, Alpha is the Texture Mask.
- **Focal Shift ring covers the whole brush:** the inner cursor ring now sits where the brush
  stops being full strength, so it runs from a tiny ring at the center (Focal Shift 0) out to the
  brush's edge (1), instead of only from halfway out. The Focal Shift slider also adjusts in
  finer steps (0.001).
- **Import ZBrush tools and projects:** File > Import > ZBrush (.ztl, .zpr) brings in the
  geometry of every subtool as its own mesh object, in a collection named after the file, turned
  from ZBrush's axes to Blender's (standing up, facing the front view). Also in the base mesh
  picker's Import. Polygroups, UVs, masks and subdivision levels are not read yet, and older
  files (version 1005, such as ZBrush's own LightBox tools) aren't supported.
- **The Transpose line goes away** when you switch to another workspace tab or object, leave
  Sculpt Mode, undo, redo or open a file, instead of lingering on the model.
- **Fixed a crash when undoing** the first stroke (or Transpose) on a mesh started from the base
  mesh picker: adding the mesh now makes its own undo step, so Ctrl+Z goes back to the plain
  mesh instead of to before it existed.
- **Transpose:** hold **Shift** while rotating (from an end, or twisting from the middle) to
  snap to 15 degree steps; the dragged circle shows the angle. Shift also snaps the line's
  direction to 15 degree steps while you draw it or place an end. Each inner circle is now
  labelled with what it does in the current mode (Stretch / Move, Scale / Thicken, Rotate /
  Twist), and notches between the circles mark the line. A measuring tick sounds as you
  rotate, scale, move or place the line, like adjusting the voxel remesh size.
- **Transpose with Shift held first:** holding Shift before clicking a circle (or before drawing
  the line) now snaps too; before, Shift-click smoothed instead.
- **Lasso and circle masking:** Ctrl-drag off the mesh can now draw a **Lasso** or a **Circle**
  (dragged out from its center) as well as a **Box**. Pick the shape with the Mask Shape buttons
  at the top of Brush Settings, shown while the Mask brush is selected or Ctrl is held. Ctrl-Alt
  erases with any shape; a lasso or circle over empty space clears the mask. Ctrl-drag on the
  mesh still paints the mask.
- **Shift smoothing shows its settings:** while Shift is held, the Brush Settings panel shows
  the brush Shift smooths with (Smooth, or Blur for paint brushes) so you can adjust its
  strength and other settings; your own brush comes back when you let go.


## 0.3.2 alpha (2026-10-23)

Built on Blender 5.4 alpha, on top of 0.3.1. A hotfix to help track down crashes some users
see when they start sculpting.

- **One crash report with everything:** if Phantom crashes, its crash report now ends with the
  Python code that was running (from Python's crash tracer), so one file has both the C++ and
  the Python side. The crash dialog has a new **Save Report to Desktop** button that saves that
  file to your desktop with the date in its name, ready to send. (The Python log is also kept on
  its own as `phantom_python_crash.txt` in the temporary folder.)


## 0.3.1 alpha (2026-10-23)

Built on Blender 5.4 alpha, on top of 0.3.0.

- **The actions bar works with Multires and Dyntopo:** it reads the mask and hidden faces where
  sculpting keeps them (the Multires grids or the Dyntopo mesh), so it appears when you mask a
  Multires object again. It's also quicker to check on plain meshes.
- **Base mesh picker:** a search field along the top (just type to filter, Esc clears), **Open...**
  for a .blend from disk and **Import...** (OBJ, FBX, STL, PLY, glTF...; the imported mesh is
  framed and sculpted like a base mesh). Bottom left, **Show at startup** opens the picker when
  Phantom starts and for every new file; bottom right, **Start Sculpting** accepts the selection.
  The window keeps its size while you search: the meshes that don't match fade away, and the rest
  slide into place.
- **Garment Mode** (experimental): a new mode in the mode menu, with its own shirt icon. It's
  Object Mode with the garment tools: **Pin** (click the cloth to pin it, right-click to unpin)
  and **Drag** (pull the GPU cloth while it simulates) join Object Mode's tools in the toolbar.
  The Garment workspace switches to it on entering, and back to Object Mode on leaving.
- **Garment tab in the Properties editor** (shirt icon, in the Garment workspace): the solver
  (GPU (Newton) or Blender Cloth), Simulate, Reset and Make Sculptable, then **Fabric** (fabric,
  sewing force, the new **Cloth Resolution** and skin offset), **Elastic** and **Seams** (the
  seam list, to flip or remove seams).
- **A tidier Garment sidebar:** only Avatar (with the Drag brush size) and Piece. The duplicate
  tool buttons and the simulation settings are gone; the pattern view's header has Mirror and the
  **avatar silhouette** toggle (a figure icon) side by side, and the 3D view's header a toggle for
  the sewing preview.


## 0.3.0 alpha (2026-10-23)

Built on Blender 5.4 alpha, on top of 0.2.3.

### Sculpting

- **Start with a base mesh:** new files start with an empty scene. Switching to a sculpting
  workspace with nothing to sculpt opens a picker with thumbnails (double-click to choose): a
  quad sphere, Female, Male, Head, Stylized Female 1 (new, made for Phantom), Stylized Female 2,
  Male Stylized, Head Stylized, and more of Blender's free human base meshes (CC0, bundled). It
  frames the model from the front and starts sculpting. Also in Add > Base Mesh... and the Asset
  Browser's Essentials library.
- **Polygroups are hidden by default;** Ctrl+F shows them with the wireframe, and again hides both.
- **X symmetry toggles without a hitch** (about 390 ms to 18 ms on a dense model).
- **Leaving Sculpt Mode on Multires models is faster** (about 440 ms to 155 ms on the realistic
  bodies): the subdivision built for sculpting is reused instead of rebuilt.
- Fixed a crash when Blender rebuilt its scene graph with an object whose material list was
  missing.

### Garment workspace (experimental)

The Garment workspace is **experimental and buggy**: expect rough edges and crashes, and save
often.

- **Live GPU cloth** with NVIDIA Newton (Style3D solver) and Warp: the sidebar's Install GPU Cloth
  button downloads them (not bundled; NVIDIA GPU needed). Seams are welded, the step runs as a
  CUDA graph. Blender's cloth stays as the fallback.
- **Drag brush:** grab the cloth in the 3D view and pull it while it simulates ([ ] for size).
- **Avatar choices:** Add / Change Avatar offers the mannequin and the bundled bodies and heads.
  The pattern view shows the avatar's front silhouette, at full size, to draft against.
- **Snapping:** points land on other points (also the polygon's own and box corners), line up with
  them (smart guides), snap to the center line, or keep 15 degree steps with Shift; the length and
  angle show by the cursor. Alt turns snapping off.
- **Mirror mode** (M, or the header button): a piece drawn on one side of the center line gets a
  twin on the other; started on the line it's half a piece, finished when it comes back; a piece
  drawn along the line joins its mirror image with no seam down the middle. Edit Points moves the
  matching point too, and a ghost cursor shows where the mirrored point goes. **Mirror Copy** in
  the Piece panel adds a flipped copy (a left sleeve from a right one).
- **Curve handles:** in Edit Points, curve points have Bezier-style handles to bend the curve; R
  makes a point smooth again.
- **Sewing preview:** the 3D view shows each seam softly glowing on both pieces with threads
  joining the points that meet, and previews the seam being made before it's added.
- **Move Pieces:** drag pieces in the 3D view, around the avatar or on the board in front of it.
- New pieces show in the 3D view, standing in front of the avatar, until they're placed.
- The tools have their own names in tooltips and menus, and the Polygon tool previews curve points
  live while Ctrl is held.
- Undo works for every tool's edits (Ctrl+Z used to skip over them).
- Fixed: the avatar flickering in and out with the bundled bodies; missing leg placement points
  on the realistic male; driver warnings from the stylized bodies.


## 0.2.3 alpha (2026-10-22)

Built on Blender 5.4 alpha, on top of 0.2.2.

- **The actions bar hides when the mask is cleared**, also when erasing or inverting leaves the
  mask empty, and comes back with undo.
- **The polygroup bar goes away** when you pick another tool or brush.
- **Isolating a polygroup shows the polygroup bar:** Ctrl+Shift-click a polygroup and the bar
  switches to polygroup actions for it (Hide, Unhide, Split Hidden...).
- **Sculpting tab:** the Properties editor's Sculpting tab previews **Sculpt Layers** and
  **ClayPolish**, marked "Coming soon".


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
