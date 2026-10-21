"""Export Phantom's built-in default theme as the bundled "Phantom" preset.

Usage: phantom.exe --background --factory-startup --python export_theme_preset.py
"""
import re
from pathlib import Path

import _rna_xml as rna_xml
import bpy

REPO = Path(__file__).resolve().parents[3]  # tools/phantom/branding -> repository root
DEST = str(REPO / "scripts" / "presets" / "interface_theme" / "Phantom.xml")

# Same writer the "Add Preset" button uses, without its "name already exists" guard.
rna_xml.xml_file_write(bpy.context, DEST, bpy.types.USERPREF_MT_interface_theme_presets.preset_xml_map)

xml = open(DEST, encoding="utf-8").read()
view3d = re.search(r"<ThemeView3D(.*?)>", xml, re.S).group(1)
for key in ("vertex_size", "edge_width", "outline_width", "object_selected"):
    m = re.search(r"(?<![a-z_])" + key + r'="([^"]*)"', view3d)
    print("PRESET", key, m.group(1) if m else None)
