# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: keymap options, shown in Preferences > Keymap for the Blender and Industry Compatible
keymaps. Each one turns a group of Phantom's controls on (the default) or back to the keymap's
original bindings.
"""

from bpy.props import BoolProperty

# (option, property name, label, description). The option names are the ones the keymap data
# files check (`params.phantom`).
OPTIONS = (
    ("masking", "phantom_masking", "ZBrush Masking",
     "Ctrl-drag masks (box off the mesh, clear or remesh over empty space), Ctrl-click smooths or "
     "inverts the mask, Ctrl-Alt erases or sharpens it, and Alt inverts the brush.\n"
     "Off: the keymap's own mask and invert keys"),
    ("face_sets", "phantom_face_sets", "ZBrush Polygroups",
     "Ctrl-W turns the masked area into a polygroup, Ctrl-Shift-click isolates a polygroup, "
     "Ctrl-F shows polygroups with the wireframe (PolyFrame).\n"
     "Off: the keymap's own Ctrl-W and Ctrl-F"),
    ("trim", "phantom_trim", "Ctrl-Shift Trim",
     "Ctrl-Shift-drag trims with the trim tool selected in the toolbar"),
    ("symmetry_x", "phantom_symmetry_x", "X Toggles Symmetry",
     "X turns X-axis symmetry on or off.\n"
     "Off: X flips the brush colors"),
    ("w_move", "phantom_w_move", "Move / Transpose Keys",
     "W is Move (E Rotate and R Scale in the Industry Compatible keymap), with the gizmo or the "
     "Transpose action line as chosen in the tool header"),
    ("c_sample", "phantom_c_sample", "C Samples Paint Color",
     "With a paint or smear brush active, C samples the mesh's color"),
    ("ctrl_d_divide", "phantom_ctrl_d_divide", "Ctrl-D Divides",
     "Ctrl-D subdivides the mesh with a Multiresolution modifier, adding one if needed, like "
     "ZBrush's Divide.\n"
     "Off: the keymap's own Ctrl-D"),
)
# Not a keymap item: quick-brush shortcuts switching off other Sculpt shortcuts on their key.
QUICK_BRUSH_TAKEOVER = "phantom_quick_brush_takeover"


def add_properties(prefs_cls, update_fn):
    """Add the Phantom options to a keymap's `KeyConfigPreferences` class (before registering)."""
    annotations = prefs_cls.__dict__.get("__annotations__")
    if annotations is None:
        annotations = {}
        prefs_cls.__annotations__ = annotations
    for _option, prop, label, description in OPTIONS:
        annotations[prop] = BoolProperty(name=label, description=description, default=True,
                                         update=update_fn)
    annotations[QUICK_BRUSH_TAKEOVER] = BoolProperty(
        name="Quick Brush Keys Take Over",
        description=(
            "Giving a quick brush a shortcut switches off other Sculpt shortcuts on that key "
            "(for example C for Clay Strips), and brings them back when it is removed"
        ),
        default=True,
    )


def enabled(prefs):
    """The Phantom options that are on, for the keymap data's `Params(phantom=...)`."""
    return frozenset(option for option, prop, _label, _desc in OPTIONS if getattr(prefs, prop, True))


def quick_brush_takeover(context):
    """Whether quick-brush shortcuts take over their key, in the active keymap."""
    keyconfig = context.window_manager.keyconfigs.active
    prefs = keyconfig.preferences if keyconfig else None
    return getattr(prefs, QUICK_BRUSH_TAKEOVER, True)


def draw(prefs, layout):
    """The "Phantom" section of a keymap's preferences."""
    col = layout.column()
    col.label(text="Phantom Sculpting")
    sub = col.column(align=True)
    for _option, prop, _label, _desc in OPTIONS:
        sub.prop(prefs, prop)
    sub.prop(prefs, QUICK_BRUSH_TAKEOVER)
