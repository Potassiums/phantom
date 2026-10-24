# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: File > Import > ZBrush (.ztl, .zpr).

Reads the geometry of ZBrush tools and projects with ``phantom_zbrush_io`` (the reader, in
``scripts/modules``): every subtool becomes a mesh object, named after it, in a new collection
named after the file. ZBrush points Y down; by default the meshes are turned to Blender's Z up
(X stays, ZBrush's depth becomes Blender's Y), standing on their feet and facing the front view.

Only geometry is read: polygroups, UVs, masks and subdivision levels are not (yet). Older files
(version 1005, like ZBrush's own LightBox tools) are not supported and say so.
"""

import os

import bpy
from bpy.props import BoolProperty, CollectionProperty, StringProperty
from bpy.types import Operator, OperatorFileListElement
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix

# ZBrush (Y down, Z into the screen) to Blender (Z up, -Y towards the front view).
ZBRUSH_TO_BLENDER = Matrix(((1.0, 0.0, 0.0, 0.0),
                            (0.0, 0.0, 1.0, 0.0),
                            (0.0, -1.0, 0.0, 0.0),
                            (0.0, 0.0, 0.0, 1.0)))


class IMPORT_SCENE_OT_phantom_zbrush(Operator, ImportHelper):
    """Import the geometry of ZBrush tools and projects: every subtool becomes a mesh object"""
    bl_idname = "import_scene.phantom_zbrush"
    bl_label = "Import ZBrush"
    bl_options = {'REGISTER', 'UNDO', 'PRESET'}

    filename_ext = ".ztl"
    filter_glob: StringProperty(default="*.ztl;*.ZTL;*.zpr;*.ZPR", options={'HIDDEN'})
    files: CollectionProperty(type=OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})
    directory: StringProperty(subtype='DIR_PATH', options={'HIDDEN', 'SKIP_SAVE'})

    convert_axes: BoolProperty(
        name="ZBrush Orientation",
        description="Turn the meshes from ZBrush's Y-down axes to Blender's Z up, standing on their feet",
        default=True,
    )

    def draw(self, _context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "convert_axes")

    def _paths(self):
        if self.files and self.directory:
            return [os.path.join(self.directory, f.name) for f in self.files if f.name]
        return [self.filepath]

    def execute(self, context):
        import phantom_zbrush_io as zbrush_io

        if context.object is not None and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for ob in context.selected_objects:
            ob.select_set(False)

        wm = context.window_manager
        added = []
        for path in self._paths():
            name = os.path.splitext(os.path.basename(path))[0]
            wm.progress_begin(0, 1)
            try:
                meshes = zbrush_io.read_meshes(path)
            except zbrush_io.UnsupportedFormat as ex:
                self.report({'WARNING'}, f"{name}: {ex}")
                continue
            except (OSError, ValueError, zbrush_io.UnsupportedCodec) as ex:
                self.report({'ERROR'}, f"{name}: could not read the file ({ex})")
                continue
            finally:
                wm.progress_end()
            if not meshes:
                self.report({'WARNING'}, f"{name}: no geometry this reader can decode (an older "
                            "ZBrush file version?)")
                continue
            collection = bpy.data.collections.new(name)
            context.scene.collection.children.link(collection)
            wm.progress_begin(0, len(meshes))
            for i, mesh in enumerate(meshes):
                me = zbrush_io.build_blender_mesh(mesh)
                if self.convert_axes:
                    me.transform(ZBRUSH_TO_BLENDER)
                ob = bpy.data.objects.new(mesh['name'], me)
                collection.objects.link(ob)
                ob.select_set(True)
                added.append(ob)
                wm.progress_update(i + 1)
            wm.progress_end()

        if not added:
            return {'CANCELLED'}
        # The biggest subtool is the one to work on.
        context.view_layer.objects.active = max(added, key=lambda ob: len(ob.data.vertices))
        self.report({'INFO'}, f"Imported {len(added)} subtool{'s' if len(added) != 1 else ''}")
        return {'FINISHED'}


def menu_func_import(self, _context):
    self.layout.operator(IMPORT_SCENE_OT_phantom_zbrush.bl_idname, text="ZBrush (.ztl, .zpr)")


classes = (
    IMPORT_SCENE_OT_phantom_zbrush,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
