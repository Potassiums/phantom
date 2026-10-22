# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: saved settings for Quad Remesh (AutoRemesher).

The Quad Remesh dropdown in the Sculpt Mode header edits these, and its button runs
``object.auto_remesh`` with them. They are stored on the scene, so they are saved with the file.
"""

import math

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, PointerProperty
from bpy.types import PropertyGroup

# Settings copied onto the operator, in the order they are drawn.
SETTINGS = (
    "target_quads",
    "edge_scaling",
    "adaptivity",
    "anisotropy",
    "sharp_edge_angle",
    "smooth_normal_angle",
    "preserve_attributes",
    "smooth_normals",
)


class PhantomQuadRemeshSettings(PropertyGroup):
    target_quads: IntProperty(
        name="Target Quads",
        description="Approximate number of quads in the new mesh",
        default=5000, min=10, soft_min=100, soft_max=200000,
    )
    edge_scaling: FloatProperty(
        name="Edge Scaling",
        description="Make the quads larger than the target count asks for",
        default=1.0, min=1.0, max=4.0,
    )
    adaptivity: FloatProperty(
        name="Adaptivity",
        description="Use smaller quads in curved areas (0 gives uniform quads)",
        default=1.0, min=0.0, max=1.0, subtype='FACTOR',
    )
    anisotropy: FloatProperty(
        name="Anisotropy",
        description="Stretch quads along flatter directions, such as along tubes and ridges (0 gives square quads)",
        default=1.0, min=0.0, max=1.0, subtype='FACTOR',
    )
    sharp_edge_angle: FloatProperty(
        name="Sharp Edge",
        description="Edges sharper than this angle are kept as feature lines",
        default=math.radians(90.0), min=math.radians(30.0), max=math.radians(180.0), subtype='ANGLE',
    )
    smooth_normal_angle: FloatProperty(
        name="Smooth Normal",
        description="Follow the smooth surface instead of the flat faces up to this angle (0 follows the faces exactly)",
        default=0.0, min=0.0, max=math.radians(180.0), subtype='ANGLE',
    )
    preserve_attributes: BoolProperty(
        name="Preserve Attributes",
        description="Reproject attributes onto the new mesh",
        default=False,
    )
    smooth_normals: BoolProperty(
        name="Smooth Normals",
        description="Set the output mesh normals to smooth",
        default=False,
    )


def draw_settings(layout, context):
    """Draw the saved settings and the button that runs Quad Remesh with them."""
    settings = context.scene.phantom_quad_remesh
    layout.use_property_split = True
    layout.use_property_decorate = False

    col = layout.column()
    col.prop(settings, "target_quads")
    col.prop(settings, "edge_scaling")

    col = layout.column()
    col.prop(settings, "adaptivity")
    col.prop(settings, "anisotropy")

    col = layout.column()
    col.prop(settings, "sharp_edge_angle")
    col.prop(settings, "smooth_normal_angle")

    col = layout.column(heading="Preserve", align=True)
    col.prop(settings, "preserve_attributes", text="Attributes")
    col.prop(settings, "smooth_normals")

    props = layout.operator("object.auto_remesh", text="Quad Remesh", icon='MESH_GRID')
    for name in SETTINGS:
        setattr(props, name, getattr(settings, name))
    props.show_popup = False


classes = (
    PhantomQuadRemeshSettings,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.phantom_quad_remesh = PointerProperty(type=PhantomQuadRemeshSettings)


def unregister():
    del bpy.types.Scene.phantom_quad_remesh
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
