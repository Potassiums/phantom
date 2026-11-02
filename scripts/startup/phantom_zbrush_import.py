# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: File > Import > ZBrush (.ztl, .zpr).

Reads the geometry of ZBrush tools and projects with ``phantom_zbrush_io`` (the reader, in
``scripts/modules``): every subtool becomes a mesh object, named after it, in a new collection
named after the file. ZBrush points Y down; by default the meshes are turned to Blender's Z up
(X stays, ZBrush's depth becomes Blender's Y), standing on their feet and facing the front view.

Subdivision levels become Multires levels (the base mesh is ZBrush's lowest level, the top level
has ZBrush's shape) and polygroups become face sets. UVs, masks and polypaint are not read (yet).
Older files (version 1005, like ZBrush's own LightBox tools) are not supported and say so.
"""

import os
import time

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
    subdivision_levels: BoolProperty(
        name="Subdivision Levels",
        description="Bring subtools' subdivision levels as Multires levels, shaped like ZBrush's highest "
        "level (off: only the lowest level, faster)",
        default=True,
    )
    polygroups: BoolProperty(
        name="Polygroups",
        description="Turn polygroups into face sets",
        default=True,
    )

    def draw(self, _context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "convert_axes")
        layout.prop(self, "subdivision_levels")
        layout.prop(self, "polygroups")

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

        try:
            from phantom_sculpt_ui import OBJECT_MODE_FACES_MAX
        except ImportError:
            OBJECT_MODE_FACES_MAX = 1 << 25

        wm = context.window_manager
        added = []
        flat = []
        timings = {'read': 0.0, 'mesh': 0.0, 'levels': 0.0}
        for path in self._paths():
            name = os.path.splitext(os.path.basename(path))[0]
            wm.progress_begin(0, 1)
            t = time.perf_counter()
            try:
                meshes = zbrush_io.read_meshes(path, levels=self.subdivision_levels)
            except zbrush_io.UnsupportedFormat as ex:
                self.report({'WARNING'}, f"{name}: {ex}")
                continue
            except (OSError, ValueError, zbrush_io.UnsupportedCodec) as ex:
                self.report({'ERROR'}, f"{name}: could not read the file ({ex})")
                continue
            finally:
                wm.progress_end()
            timings['read'] += time.perf_counter() - t
            if not meshes:
                self.report({'WARNING'}, f"{name}: no geometry this reader can decode (an older "
                            "ZBrush file version?)")
                continue
            collection = bpy.data.collections.new(name)
            context.scene.collection.children.link(collection)
            wm.progress_begin(0, len(meshes))
            matrix = ZBRUSH_TO_BLENDER if self.convert_axes else None
            for i, mesh in enumerate(meshes):
                t = time.perf_counter()
                if not self.polygroups:
                    mesh['groups'] = None
                me = zbrush_io.build_blender_mesh(mesh)
                if matrix is not None:
                    me.transform(matrix)
                ob = bpy.data.objects.new(mesh['name'], me)
                collection.objects.link(ob)
                ob.select_set(True)
                added.append(ob)
                timings['mesh'] += time.perf_counter() - t
                if mesh['levels']:
                    t = time.perf_counter()
                    try:
                        zbrush_io.add_multires_levels(ob, mesh, matrix=matrix,
                                                      max_viewport_faces=OBJECT_MODE_FACES_MAX)
                    except ValueError:
                        flat.append(ob.name)
                    timings['levels'] += time.perf_counter() - t
                mesh['top_points'] = None       # free it as soon as it is used
                wm.progress_update(i + 1)
            wm.progress_end()

        if not added:
            return {'CANCELLED'}
        # The biggest subtool is the one to work on.
        context.view_layer.objects.active = max(added, key=lambda ob: len(ob.data.vertices))
        if flat:
            self.report({'WARNING'}, f"Imported only the lowest level of {', '.join(flat)}: its "
                        "subdivision levels did not match Multires")
        self.report({'INFO'}, f"Imported {len(added)} subtool{'s' if len(added) != 1 else ''} in "
                    f"{sum(timings.values()):.1f} s (read {timings['read']:.1f}, meshes "
                    f"{timings['mesh']:.1f}, subdivision levels {timings['levels']:.1f})")
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
