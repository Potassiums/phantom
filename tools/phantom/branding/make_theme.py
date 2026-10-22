"""Apply the Phantom palette to Blender's built-in default theme.

Always starts from the upstream file in git (HEAD of `upstream/main` merge-base),
so it can be re-run after tweaking the palette below.

Usage: python make_theme.py
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # tools/phantom/branding -> repository root
THEME = "release/datafiles/userdef/userdef_default_theme.c"

# --- Palette -----------------------------------------------------------------
PERIWINKLE = "8c8cff"   # selected objects / nodes
ICE = "bcc8ff"          # active object
PURPLE = "6870e8"       # UI highlight, purple-ish blue (Blender's 4772b3)
ORANGE = "ff8a3d"       # playhead
# Edit-mode selection, Halloween style: pumpkin orange selection, witch-purple active
# element, slime-green seams and a purple-black unselected cage.
PUMPKIN = "ff6a00"
PUMPKIN_EDGE = "ff7f1f"
PUMPKIN_EDGE_MODE = "ff9a3d"
WITCH = "b77dff"
CAGE = "1a0f24"
SLIME = "7dff3a"

# Straight swaps anywhere in the file (RRGGBB, alpha preserved).
GLOBAL_SWAPS = {
    "4772b3": PURPLE,
    "3b5689": "4d4a9c",   # info selected, outliner active
    "334d80": "4a4790",   # outliner active
    "1d314d": "2c2a4d",   # outliner selected row
    "223a5b": "312e5c",   # spreadsheet selected row
    "71a8ff": "a9b4ff",   # text cursor
    "6fa9e6": "9aa6ff",   # link
}

# Per-section overrides: {section: {field: RRGGBBAA}}.
SECTION_FIELDS = {
    "anim": {"playhead": ORANGE + "ff"},
    "space_view3d": {
        "select": PERIWINKLE + "ff",
        "active": ICE + "ff",
        "vertex_select": PUMPKIN + "ff",
        "edge_select": PUMPKIN_EDGE + "ff",
        "edge_mode_select": PUMPKIN_EDGE_MODE + "ff",
        "face_select": PUMPKIN + "38",
        "face_mode_select": PUMPKIN_EDGE + "38",
        "editmesh_active": WITCH + "66",
        "vertex": CAGE + "ff",
        "edge": CAGE + "ff",
        "wire_edit": CAGE + "ff",
        "seam": SLIME + "ff",
        "sharp": WITCH + "ff",
        "gp_vertex_select": PUMPKIN + "ff",
    },
    "space_image": {
        "vertex_select": PUMPKIN + "ff",
        "edge_select": PUMPKIN_EDGE + "ff",
        "face_select": PUMPKIN + "38",
        "editmesh_active": WITCH + "66",
    },
    "space_outliner": {"selected_object": PERIWINKLE + "ff", "active_object": ICE + "ff"},
    "space_spreadsheet": {"selected_object": PERIWINKLE + "ff", "active_object": ICE + "ff"},
    "space_node": {"select": PERIWINKLE + "ff"},
}


def tint_gray(hex6):
    """Turn Blender's dark neutral grays into a deeper, sleek charcoal with a hint of cool."""
    r, g, b = (int(hex6[i:i + 2], 16) for i in (0, 2, 4))
    if r == g == b and 0x10 <= r <= 0x60:
        v = round(r * 0.88)
        r, g, b = v, min(v + 1, 255), min(v + 4, 255)
        return f"{r:02x}{g:02x}{b:02x}"
    return hex6


# Non-color per-section values: {section: {field: C literal}}.
SECTION_VALUES = {
    "space_view3d": {
        "outline_width": "3",   # Selected/active object outline (px).
        "vertex_size": "6",
        "edge_width": "2",
    },
}

# Extra fields inserted at the end of a section: {section: [C initializer lines]}.
# Widgets get a soft top-to-bottom gradient; the viewport gets a charcoal gradient.
WIDGET_GRADIENT = [".shaded = 1,", ".shadetop = 10,", ".shadedown = -8,"]
SECTION_INSERTS = {
    name: WIDGET_GRADIENT
    for name in ("wcol_regular", "wcol_tool", "wcol_toolbar_item", "wcol_radio", "wcol_option",
                 "wcol_toggle", "wcol_num", "wcol_numslider", "wcol_menu", "wcol_pulldown",
                 "wcol_tab", "wcol_box")
}
SECTION_INSERTS["space_view3d"] = [".background_type = TH_BACKGROUND_GRADIENT_LINEAR,"]

# Viewport gradient: bottom (back) darker charcoal, top (back_grad) lifted charcoal.
SECTION_FIELDS_EXTRA = {
    "space_view3d": {"back": "26272cff", "back_grad": "3b3d45ff"},
}


for _sec, _fields in SECTION_FIELDS_EXTRA.items():
    SECTION_FIELDS.setdefault(_sec, {}).update(_fields)


def main():
    src = subprocess.run(
        ["git", "-C", str(REPO), "show", f"upstream/main:{THEME}"],
        capture_output=True, text=True, check=True).stdout

    out = []
    section_stack = []
    for line in src.splitlines():
        m = re.match(r"\s*\.(\w+) = \{\s*$", line)
        if m:
            section_stack.append(m.group(1))
        elif re.match(r"\s*\},?\s*$", line) and section_stack:
            closing = section_stack.pop()
            indent = re.match(r"\s*", line).group(0) + "  "
            out.extend(indent + extra for extra in SECTION_INSERTS.get(closing, ()))

        mv = re.match(r"(\s*)\.(\w+) = ([^,{]+),\s*$", line)
        if mv and section_stack and "RGBA" not in line:
            value = SECTION_VALUES.get(section_stack[-1], {}).get(mv.group(2))
            if value is not None:
                line = f"{mv.group(1)}.{mv.group(2)} = {value},"

        def repl(mm):
            field, rgb, alpha = mm.group(1), mm.group(2).lower(), mm.group(3)
            for sec in section_stack:
                override = SECTION_FIELDS.get(sec, {}).get(field)
                if override:
                    return f".{field} = RGBA(0x{override})"
            rgb = GLOBAL_SWAPS.get(rgb, rgb)
            return f".{field} = RGBA(0x{tint_gray(rgb)}{alpha})"

        line = re.sub(r"\.(\w+) = RGBA\(0x([0-9a-fA-F]{6})([0-9a-fA-F]{2})\)", repl, line)
        out.append(line)

    text = "\n".join(out) + "\n"
    (REPO / THEME).write_text(text, newline="\n")
    print("Phantom theme written to", THEME)


if __name__ == "__main__":
    main()
