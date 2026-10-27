# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: pick something to sculpt when the canvas is empty.

Switching to a sculpting workspace (one that starts in Sculpt Mode, such as Sculpting) with no mesh
in the scene opens a picker in the middle of the 3D view, with thumbnails four across: a quad
sphere like the Sculpting template's, and Blender's free human base meshes (bodies, heads and
parts), which ship with Phantom in the Essentials asset library
(``datafiles/assets/human_base_meshes``, CC0, by Blender Studio artists), plus Phantom's own base
meshes (``datafiles/assets/phantom/base_meshes``, see ``PHANTOM_BASE_MESHES``).

Click a thumbnail to select it and double-click, press Enter or click **Open** to start: the mesh
is appended, framed from the front and put in Sculpt Mode. Scroll for more; Esc, right-click or a
click outside closes it. The picker is also in the 3D View's Add menu ("Base Mesh...").

Along the top: Phantom's splash image, then a search field (type to filter the meshes by name),
**Open...** (a .blend from disk) and **Import...** (Blender's import menu: OBJ, FBX, STL, PLY,
glTF...; the imported mesh is framed and sculpted like a base mesh), then the **New File** choices
of the splash screen with their icons (General, 2D Animation, Sculpting...). Down the right side:
**Recent Files**, then **Project Files** (the .blend files in the projects folder, by default
Documents\\Phantom Projects, where Save As puts files not saved yet). Their **View All** shows every
one of them in place of the base meshes, as cards with the picture saved in each (the search filters
them by name), and the arrow at the top left (or Esc) goes back. A file (in the lists or View All)
has a trash can on hover, to delete it (to the Recycle Bin, after asking), and right-clicking it
gives Open, Open File Location, Rename..., Duplicate, Remove from Recent Files and Delete...;
Blender's recent files list follows (``wm.phantom_recent_file_edit``). The file open now can't be
renamed or deleted.

Bottom left, the cogwheel has the picker's settings (remembered in Phantom's config folder, in
``phantom_start.json``): what shows **At startup**, this picker (the default; also for every new
file) or Blender's splash screen; on Windows, **Open .blend files with Phantom** registers Phantom
for .blend files (as Preferences > System does), or unregisters it, and **Show previews of .blend
files** has Explorer show what is in them instead of Phantom's file icon; and the **Projects
folder**, with Change... to pick another.
"""

import json
import sys

import math
import os
import time

import blf
import bpy
import gpu
import numpy as np
from bpy.props import EnumProperty
from bpy.app.handlers import persistent
from bpy.types import Operator
from gpu_extras.batch import batch_for_shader
from mathutils import Euler

BUNDLE = os.path.join("assets", "human_base_meshes", "human_base_meshes_bundle.blend")
BUNDLE_THUMBNAILS = os.path.join("assets", "human_base_meshes", "thumbnails")
PHANTOM_THUMBNAILS = os.path.join("assets", "phantom", "thumbnails")
SPLASH = os.path.join("assets", "phantom", "splash.png")
# The splash image's height in the picker (unscaled pixels).
BANNER_HEIGHT = 170
WATCH_INTERVAL = 0.3
COLUMNS = 4
# Seconds the picker takes to fade in.
FADE_TIME = 0.18
# How far tiles move (share of the way, per 1/60 s) towards where the search puts them, and how
# fast they fade in and out (per 1/60 s). The others wait for the fading ones to mostly go.
TILE_EASE = 0.22
TILE_FADE = 0.12
TILE_FADE_OUT = 0.06
# Phantom's own settings for the picker, in the user config folder.
SETTINGS_FILE = "phantom_start.json"
# How long (s) to wait for an import started from the picker to bring in a mesh.
IMPORT_WAIT = 600.0
# Played when the picker is closed without choosing (a click off it, Esc): the soft cancel.
CLOSE_SOUND = "transform_cancel"

# (key, label, kind, name in the bundle, thumbnail). Kind: 'SPHERE', 'COLLECTION' or 'OBJECT'.
# The first ones are listed under "Start with", the rest under "More base meshes".
STARTERS = (
    ('QUAD_SPHERE', "Quad Sphere", 'SPHERE', "", "quad_sphere.png"),
    ('BODY_FEMALE_REALISTIC', "Female", 'COLLECTION', "Body Female - Realistic", "realistic_body_female.png"),
    ('BODY_MALE_REALISTIC', "Male", 'COLLECTION', "Body Male - Realistic", "realistic_body_male.png"),
    ('HEAD_SCULPT', "Head", 'COLLECTION', "Head (Sculpting) - Realistic", "realistic_head_sculpt.png"),
    ('BODY_FEMALE_STYLIZED_1', "Stylized Female 1", 'COLLECTION', "Body Female 01 - Stylized",
     "stylized_female_alley.png"),
    ('BODY_FEMALE_STYLIZED', "Stylized Female 2", 'COLLECTION', "Body Female - Stylized", "stylized_body_female.png"),
    ('BODY_MALE_STYLIZED', "Male Stylized", 'COLLECTION', "Body Male - Stylized", "stylized_body_male.png"),
    ('HEAD_STYLIZED', "Head Stylized", 'COLLECTION', "Head - Stylized", "stylized_head.png"),
)
MORE = (
    ('SKULL_REALISTIC', "Skull", 'OBJECT', "Skull - Realistic", "realistic_skull.png"),
    ('HEAD_ANIMATION', "Head (Animation)", 'COLLECTION', "Head (Animation) - Realistic", "realistic_head_anim.png"),
    ('HEAD_PLANAR', "Head Planar", 'OBJECT', "Head - Planar", "planar_head.png"),
    ('HEAD_GENERIC', "Head Generic", 'OBJECT', "Head - Generic Topology", "primitive_head_generic.png"),
    ('HEAD_PRIMITIVES', "Head Primitives", 'COLLECTION', "Head - Primitives", "primitive_head_realistic.png"),
    ('SKULL_PLANAR', "Skull Planar", 'OBJECT', "Skull - Planar", "planar_skull.png"),
    ('SKELETON', "Skeleton", 'COLLECTION', "Skeleton - Realistic", "realistic_skeleton.png"),
    ('BODY_FEMALE_PRIMITIVE', "Female Primitive", 'COLLECTION', "Body Female - Primitve (Realistic)",
     "primitive_body_female_realistic.png"),
    ('BODY_MALE_PRIMITIVE', "Male Primitive", 'COLLECTION', "Body Male - Primitve (Realistic)",
     "primitive_body_male_realistic.png"),
    ('BODY_FEMALE_PRIMITIVE_STYLIZED', "Female Prim. Stylized", 'COLLECTION', "Body Female - Primitive (Stylized)",
     "primitive_body_female_stylized.png"),
    ('BODY_MALE_PRIMITIVE_STYLIZED', "Male Prim. Stylized", 'COLLECTION', "Body Male - Primitve (Stylized)",
     "primitive_body_male_stylized.png"),
    ('HAND_REALISTIC', "Hand", 'OBJECT', "Hand  - Realistic", "realistic_hand.png"),
    ('HAND_STYLIZED', "Hand Stylized", 'OBJECT', "Hand - Stylized ", "stylized_hand.png"),
    ('FOOT_REALISTIC', "Foot", 'OBJECT', "Foot - Realistic", "realistic_foot.png"),
    ('FOOT_STYLIZED', "Foot Stylized", 'OBJECT', "Foot - Stylized ", "stylized_foot.png"),
    ('EYE_REALISTIC', "Eye", 'OBJECT', "Eye - Realistic", "realistic_eye.png"),
    ('EYE_STYLIZED', "Eye Stylized", 'OBJECT', "Eye - Stylized", "stylized_eye.png"),
    ('JAW_REALISTIC', "Jaw", 'OBJECT', "Jaw - Realistic", "realistic_jaw.png"),
    ('JAW_STYLIZED', "Jaw Stylized", 'OBJECT', "Jaw - Stylized ", "stylized_jaw.png"),
)
ALL = {item[0]: item for item in STARTERS + MORE}
# Base meshes that come with Phantom itself rather than the Human Base Meshes bundle: key -> .blend
# in assets/phantom (their thumbnails are in assets/phantom/thumbnails).
PHANTOM_BASE_MESHES = {
    'BODY_FEMALE_STYLIZED_1': os.path.join("assets", "phantom", "base_meshes", "stylized_female_alley.blend"),
}

# Thumbnails: pixels read from the PNGs once, and GPU textures made from them when drawing; the
# pictures saved in recent .blend files, {path: (modified time, pixels or None)}, and their textures.
_thumbs = {"pixels": None, "textures": {}, "files": {}, "file_textures": {}}
# The open picker's state. ``view`` is 'STARTERS' (the base meshes) or 'RECENT' (every recent file,
# after View All); ``file`` is the recent file selected there, ``settings`` whether the cogwheel's
# settings are showing, ``menu`` a recent file's right-click menu ({"path", "x", "y"}) and ``dialog``
# the question asked about one ({"kind": 'DELETE' or 'RENAME', "path", "text", "error"}).
_picker = {"open": False, "region": 0, "hover": None, "selected": 'QUAD_SPHERE', "scroll": 0.0, "target": 0.0,
           "opened": 0.0, "query": "", "button": None, "at_startup": False, "quiet_until": 0.0,
           "anim": {}, "view": 'STARTERS', "file": None, "settings": False, "menu": None, "dialog": None}
# An import started from the picker: the meshes there were before it, and until when to wait.
_import = {"before": None, "deadline": 0.0}
# The workspace each window showed at the last check, to notice switches.
_last_workspace = {}


def _datafile(*parts):
    # ``system_resource`` only finds folders, so join the file onto the datafiles folder.
    return os.path.join(bpy.utils.system_resource('DATAFILES'), *parts)


def _settings_path():
    return os.path.join(bpy.utils.user_resource('CONFIG'), SETTINGS_FILE)


def _settings():
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_settings(data):
    try:
        os.makedirs(os.path.dirname(_settings_path()), exist_ok=True)
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as ex:
        print("Phantom: couldn't save the start picker settings:", ex)


def startup_screen():
    """What shows when Phantom starts: 'PICKER' (this picker, the default) or 'SPLASH' (Blender's
    splash screen). Read by Phantom itself too (``phantom_start_picker_on_startup``)."""
    return 'SPLASH' if _settings().get("startup_screen") == 'SPLASH' else 'PICKER'


def set_startup_screen(value):
    data = _settings()
    data["startup_screen"] = value
    # Before the choice was between the two: whether the picker showed at startup.
    data.pop("show_at_startup", None)
    _save_settings(data)


def show_at_startup():
    return startup_screen() == 'PICKER'


def set_show_at_startup(value):
    set_startup_screen('PICKER' if value else 'SPLASH')


def _documents_folder():
    """The user's Documents folder (where Windows has it, which may be OneDrive)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]
        # FOLDERID_Documents.
        folder_id = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(path)) == 0:
            try:
                return path.value
            finally:
                ctypes.windll.ole32.CoTaskMemFree(path)
    return os.path.join(os.path.expanduser("~"), "Documents")


def default_projects_folder():
    return os.path.join(_documents_folder(), "Phantom Projects")


def projects_folder():
    """Where new files are saved (Save As starts there) and the Project Files are listed from."""
    folder = _settings().get("projects_folder")
    return folder if isinstance(folder, str) and folder else default_projects_folder()


def set_projects_folder(folder):
    data = _settings()
    data["projects_folder"] = os.path.normpath(folder)
    _save_settings(data)
    _tell_projects_folder()


def _tell_projects_folder():
    # Phantom's Save As uses it for files not saved yet (the folder is made then if it's missing).
    bpy.types.WindowManager.phantom_projects_folder_set(projects_folder())


def project_files():
    """The .blend files in the projects folder and the folders in it, newest first."""
    folder = projects_folder()
    found = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.is_file() and entry.name.lower().endswith(".blend"):
                found.append((entry.stat().st_mtime, entry.path))
            elif entry.is_dir():
                for sub in os.scandir(entry.path):
                    if sub.is_file() and sub.name.lower().endswith(".blend"):
                        found.append((sub.stat().st_mtime, sub.path))
        except OSError:
            continue
    return [path for _mtime, path in sorted(found, reverse=True)]


def _grid_files():
    """The files View All shows: the recent ones or the project files."""
    return (_picker.get("projects") if _picker["view"] == 'PROJECTS' else _picker.get("recent")) or []


def _sections():
    """The picker's sections with the items matching the search: [(label, items)], empty ones left out."""
    words = _picker["query"].lower().split()
    if _picker["view"] != 'STARTERS':
        files = [path for path in _grid_files() if all(w in bpy.path.basename(path).lower() for w in words)]
        return [("", [(("FILE", path),) for path in files])] if files else []
    sections = []
    for label, items in (("Start with", STARTERS), ("More base meshes", MORE)):
        shown = [item for item in items if all(w in item[1].lower() or w in item[3].lower() for w in words)]
        if shown:
            sections.append((label, shown))
    return sections


# How many New File choices the header shows (like the splash screen), and recent files.
NEW_FILE_LIMIT = 6
RECENT_LIMIT = 12
# Rows of each list down the right side: recent files, then project files.
RECENT_ROWS = 3
PROJECT_ROWS = 5


# The splash screen's icon for each app template (see `TOPBAR_MT_file_new.draw_ex`).
NEW_FILE_ICONS = {
    "2D_Animation": 'GREASEPENCIL_LAYER_GROUP',
    "Sculpting": 'SCULPTMODE_HLT',
    "Storyboarding": 'GREASEPENCIL',
    "VFX": 'TRACKER',
    "Video_Editing": 'SEQUENCE',
}


def new_file_choices():
    """The splash screen's New File choices: (app template, label); "" is General."""
    from bl_ui.space_topbar import TOPBAR_MT_file_new
    templates = TOPBAR_MT_file_new.app_template_paths()[:NEW_FILE_LIMIT - 1]
    return [("", "General")] + [(t, bpy.path.display_name(t)) for t in templates]


def new_file_icon(template):
    return NEW_FILE_ICONS.get(template, 'FILE_NEW')


def recent_files():
    """Blender's recent files that still exist, newest first."""
    path = os.path.join(bpy.utils.user_resource('CONFIG'), "recent-files.txt")
    try:
        with open(path, encoding="utf-8") as f:
            files = [line.strip() for line in f if line.strip()]
    except OSError:
        return []
    return [f for f in files if os.path.isfile(f)]


def _load_file_pixels(paths):
    """Read the pictures saved in .blend files (as the File Browser shows them) into
    ``_thumbs["files"]``, again for files changed since."""
    import bpy.utils.previews
    previews = None
    try:
        for path in paths:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            cached = _thumbs["files"].get(path)
            if cached is not None and cached[0] == mtime:
                continue
            if previews is None:
                previews = bpy.utils.previews.new()
            preview = previews.load(path, path, 'BLEND')
            width, height = preview.image_size
            data = None
            if width and height:
                data = np.empty(width * height * 4, dtype=np.float32)
                preview.image_pixels_float.foreach_get(data)
            _thumbs["files"][path] = (mtime, (width, height, data) if data is not None else None)
            _thumbs["file_textures"].pop(path, None)
    finally:
        if previews is not None:
            bpy.utils.previews.remove(previews)


def _file_texture(path):
    """GPU texture of a recent file's picture, or None when it has none."""
    texture = _thumbs["file_textures"].get(path)
    if texture is None:
        entry = _thumbs["files"].get(path)
        if entry is None or entry[1] is None:
            return None
        width, height, data = entry[1]
        texture = gpu.types.GPUTexture((width, height), format='RGBA16F',
                                       data=gpu.types.Buffer('FLOAT', len(data), data))
        _thumbs["file_textures"][path] = texture
    return texture


# A recent file's right-click menu: (action, label, icon); None is a separator.
FILE_MENU = (
    ('OPEN', "Open", 'FILE_FOLDER'),
    ('LOCATION', "Open File Location", 'FOLDER_REDIRECT'),
    None,
    ('RENAME', "Rename...", 'GREASEPENCIL'),
    ('DUPLICATE', "Duplicate", 'DUPLICATE'),
    None,
    ('FORGET', "Remove from Recent Files", 'X'),
    ('DELETE', "Delete...", 'TRASH'),
)
# What a file's name can't have (Windows' rule, the strictest).
BAD_NAME_CHARS = set('<>:"/\\|?*')


def _is_open_file(path):
    return bool(bpy.data.filepath) and os.path.normcase(os.path.abspath(path)) == \
        os.path.normcase(os.path.abspath(bpy.data.filepath))


def _edit_recent(action, path, new_path=""):
    """Keep Blender's recent files list in step with a file renamed, duplicated or deleted."""
    bpy.ops.wm.phantom_recent_file_edit(action=action, filepath=path, new_filepath=new_path)


def delete_file(path):
    """Move a file to the Recycle Bin (on Windows; elsewhere it's deleted) and take it off the recent
    files. Returns an error message, or None."""
    if _is_open_file(path):
        return "It's the file open now"
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                        ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]
        FO_DELETE = 3
        FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
        op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=os.path.abspath(path) + "\0",
                             fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI)
        if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) != 0 or op.fAnyOperationsAborted:
            return "Windows couldn't move it to the Recycle Bin"
    else:
        try:
            os.remove(path)
        except OSError as ex:
            return ex.strerror or str(ex)
    _edit_recent('REMOVE', path)
    return None


def rename_file(path, name):
    """Rename a file (``name`` without .blend) and its place in the recent files. Returns
    (new path, None), or (None, an error message)."""
    name = name.strip()
    if name.lower().endswith(".blend"):
        name = name[:-len(".blend")].rstrip()
    if not name:
        return None, "Type a name"
    if BAD_NAME_CHARS & set(name) or name.endswith("."):
        return None, "A name can't have any of  < > : \" / \\ | ? *"
    if _is_open_file(path):
        return None, "It's the file open now"
    new_path = os.path.join(os.path.dirname(path), name + ".blend")
    if new_path == path:
        return path, None
    # A different file by that name (not this one with its letters' case changed).
    if os.path.exists(new_path) and os.path.normcase(new_path) != os.path.normcase(path):
        return None, "There's already a file called %s" % (name + ".blend")
    try:
        os.rename(path, new_path)
    except OSError as ex:
        return None, ex.strerror or str(ex)
    _edit_recent('REPLACE', path, new_path)
    return new_path, None


def duplicate_file(path):
    """Copy a file next to itself ("<name> copy.blend", then "copy 2"...) and list the copy after it in
    the recent files. Returns (copy's path, None), or (None, an error message)."""
    import shutil
    stem = os.path.splitext(path)[0]
    new_path, number = stem + " copy.blend", 2
    while os.path.exists(new_path):
        new_path, number = "%s copy %d.blend" % (stem, number), number + 1
    try:
        shutil.copy2(path, new_path)
    except OSError as ex:
        return None, ex.strerror or str(ex)
    if path in recent_files():
        _edit_recent('ADD_AFTER', path, new_path)
    return new_path, None


def show_in_folder(path):
    """Open the file's folder with the file selected (Explorer), or just the folder elsewhere."""
    import subprocess
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.abspath(path)])
    else:
        bpy.ops.wm.path_open(filepath=os.path.dirname(path))


def _tile_label(key):
    return bpy.path.basename(key[1]) if isinstance(key, tuple) else ALL[key][1]


def blend_files_open_with_phantom():
    """Whether Windows opens .blend files with this Phantom (None when it can't be told: not
    Windows). Its file type is `phantom.<Blender major>.<minor>` (see `BLENDER_WIN_APPID`)."""
    if sys.platform != "win32":
        return None
    import winreg
    app_id = "phantom.%d.%d" % bpy.app.version[:2]
    # The user's own choice wins over the one for all users.
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"Software\Classes\.blend") as key:
                value, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        if value:
            return value == app_id
    return False


# Explorer's thumbnail provider shell extension, and Phantom's (BlendThumb.dll, as Blender's).
THUMBNAIL_PROVIDER = "{e357fccd-a995-4576-b01f-234630154e96}"
BLENDTHUMB_CLSID = "{D45F043D-F17F-4e8a-8435-70971D9FA46D}"


def _blendthumb_dll():
    return os.path.join(os.path.dirname(bpy.app.binary_path), "BlendThumb.dll")


def blend_previews_shown():
    """Whether Explorer shows previews of .blend files through this Phantom's thumbnail handler
    (None when it can't be told: not Windows, or the handler isn't installed)."""
    if sys.platform != "win32" or not os.path.isfile(_blendthumb_dll()):
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            "Software\\Classes\\.blend\\ShellEx\\" + THUMBNAIL_PROVIDER) as key:
            clsid = winreg.QueryValueEx(key, "")[0]
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            "Software\\Classes\\CLSID\\" + clsid + "\\InprocServer32") as key:
            dll = winreg.QueryValueEx(key, "")[0]
    except OSError:
        return False
    return clsid.lower() == BLENDTHUMB_CLSID.lower() and \
        os.path.normcase(os.path.abspath(dll)) == os.path.normcase(_blendthumb_dll())


def set_blend_previews(show):
    """Show previews of .blend files in Explorer (Phantom's thumbnail handler, for this user), or
    Phantom's file icon. Returns whether it worked."""
    import ctypes
    import subprocess
    regsvr32 = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "regsvr32.exe")
    args = [regsvr32, "/s", _blendthumb_dll()] if show else [regsvr32, "/u", "/s", _blendthumb_dll()]
    try:
        subprocess.run(args, check=False, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as ex:
        print("Phantom: couldn't change .blend previews:", ex)
        return False
    # Explorer redraws .blend files with their previews, or the icon.
    SHCNE_ASSOCCHANGED = 0x08000000
    ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, 0, None, None)
    return blend_previews_shown() == show


def _get_blend_previews(_self):
    return bool(blend_previews_shown())


def _set_blend_previews(_self, value):
    set_blend_previews(value)
    if _picker["open"]:
        _picker["previews"] = blend_previews_shown()


def canvas_is_empty(scene):
    return not any(ob.type == 'MESH' for ob in scene.objects)


def _play(name):
    # Phantom's interface sounds, through the garment module's player when it's there.
    try:
        import phantom_garment
        phantom_garment.play_sound(name)
    except ImportError:
        pass


def _view3d(window):
    areas = [a for a in window.screen.areas if a.type == 'VIEW_3D']
    if not areas:
        return None, None
    area = max(areas, key=lambda a: a.width * a.height)
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    return area, region


def append_base_mesh(context, key, parent_collection=None, strip_drivers=False):
    """Append a base mesh (not the quad sphere) into the scene, in the middle of the world.

    ``strip_drivers``: drop the bundle's drivers (they drive mask modifiers from a rig that isn't
    appended, so they only fail and warn), before anything evaluates them.

    Returns (the main mesh, every object added, the collection or object appended), or None.
    """
    item = ALL[key]
    path = _datafile(PHANTOM_BASE_MESHES.get(key, BUNDLE))
    if item[2] == 'SPHERE' or not os.path.exists(path):
        return None
    parent_collection = parent_collection or context.scene.collection
    kind = "collections" if item[2] == 'COLLECTION' else "objects"
    with bpy.data.libraries.load(path, link=False) as (data_from, data_to):
        setattr(data_to, kind, [item[3]] if item[3] in getattr(data_from, kind) else [])
    loaded = [block for block in getattr(data_to, kind) if block is not None]
    if not loaded:
        return None
    block = loaded[0]
    if strip_drivers:
        for ob in (block.all_objects if item[2] == 'COLLECTION' else [block]):
            if ob.animation_data is not None:
                for fcurve in list(ob.animation_data.drivers):
                    ob.animation_data.drivers.remove(fcurve)
    if item[2] == 'COLLECTION':
        parent_collection.children.link(block)
        added = list(block.all_objects)
    else:
        parent_collection.objects.link(block)
        added = [block]
    # It's the user's own copy now, not a library asset.
    block.asset_clear()
    meshes = [ob for ob in added if ob.type == 'MESH']
    if not meshes:
        return None
    # The main mesh: the one with the most vertices.
    target = max(meshes, key=lambda ob: len(ob.data.vertices))
    # The bundle lays its assets out side by side; bring this one to the middle of the world
    # (keeping its height), with its parts together.
    context.view_layer.update()
    offset = target.matrix_world.translation.copy()
    offset.z = 0.0
    for ob in added:
        if ob.parent not in added:
            ob.location -= offset
    return target, added, block


def spawn(context, key):
    """Add a starter to the scene, frame it, and sculpt it. Returns the object to sculpt."""
    item = ALL[key]
    if context.object is not None and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in context.view_layer.objects:
        if ob is not None and ob.select_get():
            ob.select_set(False)

    if item[2] == 'SPHERE':
        bpy.ops.mesh.primitive_quad_sphere_add(segments=64)
        target = context.active_object
        added = [target]
    else:
        appended = append_base_mesh(context, key)
        if appended is None:
            return None
        target, added, _block = appended

    _frame_and_sculpt(context, target, added)
    return target


def _frame_and_sculpt(context, target, added):
    """Make ``target`` active, face its front framed in the 3D view, and sculpt it."""
    for ob in added:
        try:
            ob.select_set(True)
        except RuntimeError:
            pass
    context.view_layer.objects.active = target
    area, region = _view3d(context.window)
    if area is not None:
        # Face the front of the new mesh, framed, every time.
        region_3d = area.spaces.active.region_3d
        region_3d.view_rotation = Euler((math.radians(90.0), 0.0, 0.0)).to_quaternion()
        with context.temp_override(area=area, region=region):
            bpy.ops.view3d.view_selected()
    for ob in added:
        if ob != target:
            try:
                ob.select_set(False)
            except RuntimeError:
                pass
    bpy.ops.object.mode_set(mode='SCULPT')


class PHANTOM_OT_start_asset(Operator):
    """Add a base mesh and sculpt it: a quad sphere or one of Blender's human base meshes"""
    bl_idname = "phantom.start_asset"
    bl_label = "Add Base Mesh"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    choice: EnumProperty(
        name="Start With",
        items=[(item[0], item[1], "") for item in STARTERS + MORE],
        default='QUAD_SPHERE',
    )

    def execute(self, context):
        ob = spawn(context, self.choice)
        if ob is None:
            self.report({'ERROR'}, "Couldn't add the base mesh (are Phantom's assets installed?)")
            _play("error")
            return {'CANCELLED'}
        _play("mode_switch")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# The picker: a panel drawn in the middle of the 3D view.

def _load_pixels():
    """Thumbnail pixels, {key: (width, height, float array)}, read once."""
    if _thumbs["pixels"] is not None:
        return _thumbs["pixels"]
    pixels = {}
    for key, item in ALL.items():
        folder = PHANTOM_THUMBNAILS if item[2] == 'SPHERE' or key in PHANTOM_BASE_MESHES else BUNDLE_THUMBNAILS
        path = _datafile(folder, item[4])
        if not os.path.exists(path):
            continue
        image = bpy.data.images.load(path, check_existing=False)
        try:
            width, height = image.size
            data = np.empty(width * height * 4, dtype=np.float32)
            image.pixels.foreach_get(data)
            # Premultiply: the PNGs' transparent pixels are white in some and black in others.
            rgba = data.reshape(-1, 4)
            rgba[:, :3] *= rgba[:, 3:4]
            pixels[key] = (width, height, data)
        finally:
            bpy.data.images.remove(image)
    path = _datafile(SPLASH)
    if os.path.exists(path):
        image = bpy.data.images.load(path, check_existing=False)
        try:
            width, height = image.size
            data = np.empty(width * height * 4, dtype=np.float32)
            image.pixels.foreach_get(data)
            pixels["SPLASH"] = (width, height, data)
        finally:
            bpy.data.images.remove(image)
    _thumbs["pixels"] = pixels
    return pixels


def _texture(key):
    """GPU texture of a thumbnail (made while drawing, where the GPU is available)."""
    texture = _thumbs["textures"].get(key)
    if texture is None:
        entry = (_thumbs["pixels"] or {}).get(key)
        if entry is None:
            return None
        width, height, data = entry
        texture = gpu.types.GPUTexture((width, height), format='RGBA16F',
                                       data=gpu.types.Buffer('FLOAT', len(data), data))
        _thumbs["textures"][key] = texture
    return texture


def _rounded_rect(x, y, w, h, radius, color, segments=6):
    radius = max(0.0, min(radius, w / 2.0, h / 2.0))
    corners = ((x + w - radius, y + h - radius, 0.0), (x + radius, y + h - radius, 0.5 * math.pi),
               (x + radius, y + radius, math.pi), (x + w - radius, y + radius, 1.5 * math.pi))
    outline = []
    for cx, cy, start in corners:
        for i in range(segments + 1):
            a = start + 0.5 * math.pi * i / segments
            outline.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    verts = [(x + w / 2.0, y + h / 2.0)] + outline + [outline[0]]
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'TRI_FAN', {"pos": verts})
    shader.uniform_float("color", color)
    # Text drawing turns blending off, which made see-through shapes after it solid.
    gpu.state.blend_set('ALPHA')
    batch.draw(shader)


def _image(texture, x, y, w, h, alpha, u0=0.0, u1=1.0):
    """Draw a texture (from `u0` to `u1` of its width) over the rectangle."""
    shader = gpu.shader.from_builtin('IMAGE_COLOR')
    batch = batch_for_shader(shader, 'TRI_FAN', {
        "pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h)),
        "texCoord": ((u0, 0), (u1, 0), (u1, 1), (u0, 1)),
    })
    shader.uniform_sampler("image", texture)
    shader.uniform_float("color", (alpha, alpha, alpha, alpha))
    # Text drawing changes the blend mode, so set it for every image.
    gpu.state.blend_set('ALPHA_PREMULT')
    batch.draw(shader)
    gpu.state.blend_set('ALPHA')


def _text(x, y, text, size, color, align='CENTER'):
    font = 0
    blf.size(font, size)
    width, _height = blf.dimensions(font, text)
    blf.color(font, *color)
    blf.position(font, x - width / 2.0 if align == 'CENTER' else x, y, 0)
    blf.draw(font, text)


class _Layout:
    """Where everything goes, in region pixels, for a region and a scroll position."""

    def __init__(self, region, scroll):
        s = bpy.context.preferences.system.ui_scale
        self.s = s
        self.tile = 148 * s
        self.thumb = self.thumb_w = 124 * s
        self.label = 22 * s
        self.gap = 10 * s
        self.pad = 20 * s
        self.banner = BANNER_HEIGHT * s
        self.header = self.banner + 150 * s
        self.footer = 56 * s
        self.section = 30 * s
        starter_row_h = self.thumb + self.label + self.gap
        grid_view = _picker["view"] != 'STARTERS'
        # The base meshes on the left, the recent and project files down the right; View All puts
        # every recent (or project) file in their place, across the whole width, as cards as wide as
        # the pictures saved in them (a picture of the window).
        self.grid_w = COLUMNS * self.tile + (COLUMNS - 1) * self.gap + 2 * self.pad
        self.side_w = 250 * s
        self.width = self.grid_w + self.side_w
        self.content_w = self.width if grid_view else self.grid_w
        if grid_view:
            self.tile = 206 * s
            self.thumb_w = self.tile - 12 * s
            self.thumb = round(self.thumb_w * 135 / 256)
            self.label = 26 * s
            self.tile_h = 6 * s + self.thumb + self.label
            columns = int((self.content_w - 2 * self.pad + self.gap) // (self.tile + self.gap))
        else:
            self.tile_h = self.thumb + self.label
            columns = COLUMNS
        grid_left = (self.content_w - (columns * self.tile + (columns - 1) * self.gap)) / 2.0
        row_h = self.tile_h + self.gap
        sections = _sections()
        rows = sum(-(-len(items) // columns) for _label, items in sections)
        # View All's cards start a little below its title, so the selected one's outline shows.
        self.card_top = 4 * s if grid_view else 0.0
        self.content = max(len(sections) * self.section + rows * row_h + self.pad + self.card_top, 80 * s)
        # The window's size doesn't follow the search or the view: it's sized for every base mesh.
        all_rows = sum(-(-len(items) // COLUMNS) for items in (STARTERS, MORE))
        full = 2 * self.section + all_rows * starter_row_h + self.pad
        self.height = min(self.header + full + self.footer, region.height * 0.85)
        self.x = (region.width - self.width) / 2.0
        self.y = (region.height - self.height) / 2.0
        self.view_top = self.y + self.height - self.header
        # View All: the back arrow and the title stay above the scrolling files, where (and as large
        # as) the base meshes' "Start with" is, so the files start as high as the base meshes do.
        self.back_title = (self.x + self.pad, self.view_top - 22 * s)
        if grid_view:
            self.view_top -= self.section
        self.view_bottom = self.y + self.footer
        self.view_h = self.view_top - self.view_bottom
        self.max_scroll = max(0.0, self.content - self.view_h)
        self.scroll = min(max(scroll, 0.0), self.max_scroll)
        # Along the top: the New File row, then the search field, Open and Import; along the
        # bottom: the settings and Open. Each (x, y, w, h).
        top = self.y + self.height - self.banner
        self.title_y = top - 34 * s
        bar_y, bar_h, button_w = top - 88 * s, 30 * s, 92 * s
        right = self.x + self.width - self.pad
        self.buttons = {
            'IMPORT': (right - button_w, bar_y, button_w, bar_h),
            'OPEN': (right - 2 * button_w - 8 * s, bar_y, button_w, bar_h),
            'SEARCH': (self.x + self.pad, bar_y, right - 2 * button_w - 16 * s - self.x - self.pad, bar_h),
            'SETTINGS': (self.x + self.pad, self.y + 12 * s, 32 * s, 32 * s),
            'ACCEPT': (right - 150 * s, self.y + 12 * s, 150 * s, 32 * s),
        }
        if grid_view:
            self.buttons['BACK'] = (self.x + self.pad - 5 * s, self.back_title[1] - 7 * s, 22 * s, 22 * s)
        # The cogwheel's settings, above it: [(kind, name, label, row)] and the box around them;
        # ``settings_names`` are their buttons.
        self.settings, self.settings_names = [], []
        if _picker["settings"]:
            inner, box_w = 10 * s, 360 * s
            rows = [('STARTUP', None, "At startup", 38 * s)]
            if _picker.get("associated") is not None:
                rows.append(('CHECK', 'ASSOCIATE', "Open .blend files with Phantom", 30 * s))
            if _picker.get("previews") is not None:
                rows.append(('CHECK', 'PREVIEWS', "Show previews of .blend files", 30 * s))
            rows.append(('FOLDER', 'PROJECTS_FOLDER', "Projects folder", 50 * s))
            box_h = sum(row[3] for row in rows) + 2 * inner
            bx, by = self.x + self.pad, self.y + 50 * s
            self.settings_box = (bx, by, box_w, box_h)
            row_top = by + box_h - inner
            for kind, name, label, height in rows:
                row_top -= height
                row = (bx + inner, row_top, box_w - 2 * inner, height)
                self.settings.append((kind, name, label, row))
                if kind == 'STARTUP':
                    each, bh = 104 * s, 26 * s
                    right_ = row[0] + row[2]
                    self.buttons['STARTUP_SPLASH'] = (right_ - each, row_top + (height - bh) / 2.0, each, bh)
                    self.buttons['STARTUP_PICKER'] = (right_ - 2 * each - 4 * s, row_top + (height - bh) / 2.0, each, bh)
                    self.settings_names += ['STARTUP_PICKER', 'STARTUP_SPLASH']
                elif kind == 'CHECK':
                    self.buttons[name] = row
                    self.settings_names.append(name)
                else:
                    bw_, bh = 84 * s, 26 * s
                    self.buttons[name] = (row[0] + row[2] - bw_, row_top + (height - bh) / 2.0, bw_, bh)
                    self.settings_names.append(name)
        # The New File row: a label, then the choices sharing the width.
        new_y, new_h = top - 130 * s, 32 * s
        self.new_label = (self.x + self.pad, new_y + new_h / 2.0)
        choices = _picker.get("new_files") or new_file_choices()
        left = self.x + self.pad + 72 * s
        each = (right - left - (len(choices) - 1) * 6 * s) / max(len(choices), 1)
        for i, (template, label) in enumerate(choices):
            self.buttons[("NEW", template, label)] = (left + i * (each + 6 * s), new_y, each, new_h)
        # Down the right of the base meshes: the recent files, then the project files, each with
        # View All by its title. Their rows: ``recent`` and ``projects``, [(path, rect)].
        side_x = self.x + self.grid_w
        self.recent, self.projects = [], []
        self.recent_title = (side_x, self.view_top - 22 * s)
        self.projects_title = None
        if not grid_view:
            row_h_side, trash = 26 * s, 20 * s

            def side_list(kind, files, top, limit):
                listed = []
                for i, path in enumerate(files[:max(0, limit)]):
                    rect = (side_x - 6 * s, top - (i + 1) * row_h_side, self.side_w - self.pad, row_h_side - 2 * s)
                    listed.append((path, rect))
                    # The trash can first, so it wins the click over its row.
                    if not _is_open_file(path):
                        self.buttons[("TRASH", path, kind)] = (rect[0] + rect[2] - trash - 3 * s,
                                                               rect[1] + (rect[3] - trash) / 2.0, trash, trash)
                    self.buttons[(kind, path)] = rect
                return listed

            files = _picker["recent"] if _picker.get("recent") is not None else recent_files()
            top = self.view_top - 34 * s
            self.recent = side_list("RECENT", files, top, RECENT_ROWS)
            if files:
                self.buttons['VIEW_ALL'] = (right - 70 * s, self.recent_title[1] - 7 * s, 70 * s, 22 * s)
            top -= max(len(self.recent), 1) * row_h_side + 22 * s
            self.projects_title = (side_x, top - 12 * s)
            projects = _picker.get("projects") or []
            if projects:
                self.buttons['VIEW_ALL_PROJECTS'] = (right - 70 * s, top - 19 * s, 70 * s, 22 * s)
            fit = int((top - 24 * s - (self.view_bottom + 22 * s)) // row_h_side)
            self.projects = side_list("PROJECT", projects, top - 24 * s, min(PROJECT_ROWS, fit))
        # Tiles: [(key, x, y)], y being the tile's bottom; sections: [(label, y)]. The content's top
        # (``content_top``) moves with the scroll; tile animation is kept relative to it.
        self.tiles, self.sections = [], []
        top = self.view_top + self.scroll - self.card_top
        self.content_top = top
        for label, items in sections:
            if label:
                top -= self.section
                self.sections.append((label, top + 8 * s))
            for i, item in enumerate(items):
                row, col = divmod(i, columns)
                self.tiles.append((item[0], self.x + grid_left + col * (self.tile + self.gap),
                                   top - (row + 1) * row_h + self.gap))
            top -= -(-len(items) // columns) * row_h
        # View All: a trash can in each file's top right corner (shown on hover).
        trash = 26 * s
        for key, tx, ty in self.tiles:
            if isinstance(key, tuple) and not _is_open_file(key[1]):
                rect = (tx + self.tile - trash - 12 * s, ty + self.tile_h - trash - 12 * s, trash, trash)
                if rect[1] >= self.view_bottom and rect[1] + trash <= self.view_top:
                    self.buttons[("TRASH", key[1], "TILE")] = rect

        # A recent file's right-click menu, from where it was opened, kept on screen.
        self.menu_items, self.menu_box = [], None
        menu = _picker.get("menu")
        if menu:
            row, gap, inner, menu_w = 26 * s, 9 * s, 5 * s, 230 * s
            menu_h = sum(row if item else gap for item in FILE_MENU) + 2 * inner
            mx = min(max(menu["x"], 4 * s), region.width - menu_w - 4 * s)
            my = min(max(menu["y"] - menu_h, 4 * s), region.height - menu_h - 4 * s)
            self.menu_box = (mx, my, menu_w, menu_h)
            open_now = _is_open_file(menu["path"])
            top = my + menu_h - inner
            for item in FILE_MENU:
                if item is None:
                    top -= gap
                    self.menu_items.append((None, top + gap / 2.0))
                    continue
                top -= row
                action = item[0]
                enabled = not (open_now and action in {'RENAME', 'DELETE'})
                rect = (mx + inner, top, menu_w - 2 * inner, row)
                self.menu_items.append((item, rect, enabled))
                if enabled:
                    self.buttons[("MENU", action)] = rect

        # Delete or Rename asked in the middle of the picker.
        self.dialog_box = self.dialog_field = None
        dialog = _picker.get("dialog")
        if dialog:
            dw, dh = 400 * s, (168 if dialog["kind"] == 'RENAME' else 140) * s
            dx, dy = self.x + (self.width - dw) / 2.0, self.y + (self.height - dh) / 2.0
            self.dialog_box = (dx, dy, dw, dh)
            if dialog["kind"] == 'RENAME':
                self.dialog_field = (dx + 20 * s, dy + 66 * s, dw - 40 * s, 30 * s)
            bw, bh = 100 * s, 30 * s
            self.buttons['DIALOG_OK'] = (dx + dw - 20 * s - bw, dy + 18 * s, bw, bh)
            self.buttons['DIALOG_CANCEL'] = (dx + dw - 28 * s - 2 * bw, dy + 18 * s, bw, bh)

    def inside(self, mx, my):
        return self.x <= mx <= self.x + self.width and self.y <= my <= self.y + self.height

    def in_settings(self, mx, my):
        if not self.settings:
            return False
        bx, by, bw, bh = self.settings_box
        return bx <= mx <= bx + bw and by <= my <= by + bh

    def _over(self, box, mx, my):
        return box is not None and box[0] <= mx <= box[0] + box[2] and box[1] <= my <= box[1] + box[3]

    def covered(self, mx, my):
        """Whether something drawn over the picker (a dialog, a menu, the settings) is there, or a
        dialog is showing at all (it keeps everything else from being clicked)."""
        return (self.dialog_box is not None or self._over(self.menu_box, mx, my) or
                self.in_settings(mx, my))

    def button_at(self, mx, my):
        # What's on top first: the dialog (nothing else while it shows), the menu, the settings.
        if self.dialog_box is not None:
            names = ('DIALOG_OK', 'DIALOG_CANCEL')
        elif self._over(self.menu_box, mx, my):
            names = [n for n in self.buttons if isinstance(n, tuple) and n[0] == "MENU"]
        elif self.in_settings(mx, my):
            names = self.settings_names
        else:
            names = self.buttons
        return next((name for name in names if self._hit(name, mx, my)), None)

    def _hit(self, name, mx, my):
        bx, by, bw, bh = self.buttons[name]
        return bx <= mx <= bx + bw and by <= my <= by + bh

    def tile_at(self, mx, my):
        if not (self.view_bottom <= my <= self.view_top) or self.covered(mx, my):
            return None
        for key, tx, ty in self.tiles:
            if tx <= mx <= tx + self.tile and ty <= my <= ty + self.tile_h:
                return key
        return None


def _animate(layout):
    """Move the tiles' drawn positions and opacity a step towards where the search puts them.
    Tiles that no longer match fade out where they are. Returns True while anything is moving."""
    anim = _picker["anim"]
    targets = {key: (x - layout.x, y - layout.content_top) for key, x, y in layout.tiles}
    moving = False
    # Tiles leaving fade out where they are first; then the rest move up into the gaps.
    waiting = any(state[2] > 0.3 for key, state in anim.items() if key not in targets)
    for key, (tx, ty) in targets.items():
        state = anim.get(key)
        if state is None:
            # New on screen (or back from a search): appear where it belongs.
            anim[key] = state = [tx, ty, 0.0]
        x, y, alpha = state
        ease = 0.0 if waiting and alpha >= 1.0 else TILE_EASE
        nx, ny = x + (tx - x) * ease, y + (ty - y) * ease
        nalpha = min(1.0, alpha + TILE_FADE)
        if abs(tx - nx) < 0.5 and abs(ty - ny) < 0.5:
            nx, ny = tx, ty
        moving |= (nx, ny, nalpha) != (x, y, alpha)
        state[:] = [nx, ny, nalpha]
    for key in list(anim):
        if key not in targets:
            anim[key][2] -= TILE_FADE_OUT
            moving = True
            if anim[key][2] <= 0.0:
                del anim[key]
    return moving


class PHANTOM_OT_start_picker(Operator):
    """Choose a base mesh to start sculpting: a quad sphere or one of Blender's human base meshes"""
    bl_idname = "phantom.start_picker"
    bl_label = "Base Mesh"

    at_startup: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, _event):
        area = context.area
        if area is None or area.type != 'VIEW_3D':
            area, _region = _view3d(context.window)
        if area is None or _picker["open"]:
            return {'CANCELLED'}
        self.area = area
        self.region = next(r for r in area.regions if r.type == 'WINDOW')
        self.last_press = (None, 0.0)
        self.import_pressed = False
        _load_pixels()
        _picker.update(open=True, region=self.region.as_pointer(), hover=None, selected='QUAD_SPHERE',
                       scroll=0.0, target=0.0, opened=time.perf_counter(), query="", button=None,
                       at_startup=self.at_startup, anim={}, view='STARTERS', file=None, settings=False,
                       # Read once while the picker is open (the layout is made on every redraw).
                       recent=recent_files(), projects=project_files(), new_files=new_file_choices(),
                       associated=blend_files_open_with_phantom(), previews=blend_previews_shown())
        # Everything starts where it belongs, fully shown.
        layout = _Layout(self.region, 0.0)
        _picker["anim"] = {key: [x - layout.x, y - layout.content_top, 1.0] for key, x, y in layout.tiles}
        self.handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_PIXEL')
        self.timer = context.window_manager.event_timer_add(1.0 / 60.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        _play("menu_open")
        area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _close(self, context):
        bpy.types.SpaceView3D.draw_handler_remove(self.handle, 'WINDOW')
        context.window_manager.event_timer_remove(self.timer)
        _picker["open"] = False
        self.area.tag_redraw()

    def _start(self, context, key):
        if key is None:
            _play("error")
            return {'RUNNING_MODAL'}
        self._close(context)
        window = context.window
        sculpting = bpy.data.workspaces.get("Sculpting")
        if _picker["at_startup"] and window.workspace.object_mode != 'SCULPT' and sculpting is not None:
            # Opened at startup: sculpt in the Sculpting workspace, once it's shown.
            _picker["quiet_until"] = time.perf_counter() + 2.0
            window.workspace = sculpting

            def later():
                area, region = _view3d(window)
                if area is not None:
                    with bpy.context.temp_override(window=window, area=area, region=region):
                        bpy.ops.phantom.start_asset('EXEC_DEFAULT', True, choice=key)
                return None
            bpy.app.timers.register(later, first_interval=0.05)
            return {'FINISHED'}
        # `True`: push the operator's undo step (Python operator calls skip undo by default).
        # Without it, undoing the first stroke went back to before the mesh existed and crashed.
        with context.temp_override(area=self.area, region=self.region):
            bpy.ops.phantom.start_asset('EXEC_DEFAULT', True, choice=key)
        return {'FINISHED'}

    def _open(self, context):
        self._close(context)
        with context.temp_override(window=context.window, area=self.area, region=self.region):
            bpy.ops.wm.open_mainfile('INVOKE_DEFAULT')
        return {'FINISHED'}

    def _new_file(self, context, app_template):
        """A new file from the splash screen's choices (it asks about unsaved changes first)."""
        self._close(context)
        _play("click")
        with context.temp_override(window=context.window, area=self.area, region=self.region):
            bpy.ops.wm.read_homefile('INVOKE_DEFAULT', app_template=app_template)
        return {'FINISHED'}

    def _open_recent(self, context, path):
        """Open a recent file (it asks about unsaved changes first)."""
        self._close(context)
        _play("click")
        with context.temp_override(window=context.window, area=self.area, region=self.region):
            bpy.ops.wm.open_mainfile('INVOKE_DEFAULT', filepath=path, display_file_selector=False)
        return {'FINISHED'}

    def _associate(self, context, on):
        """Make Windows open .blend files with Phantom, or stop it (as Preferences > System)."""
        with context.temp_override(window=context.window, area=self.area, region=self.region):
            try:
                if on:
                    bpy.ops.preferences.associate_blend('EXEC_DEFAULT')
                else:
                    bpy.ops.preferences.unassociate_blend('EXEC_DEFAULT')
            except RuntimeError as ex:
                self.report({'WARNING'}, str(ex).replace("Error: ", "").strip())
        _picker["associated"] = blend_files_open_with_phantom()
        _play("toggle_on" if _picker["associated"] else "toggle_off")
        self.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _import(self, context):
        self._close(context)
        # Sculpt whatever mesh the import brings in (see _watch_import).
        _import.update(before={ob.name for ob in context.scene.objects}, deadline=time.perf_counter() + IMPORT_WAIT)
        if not bpy.app.timers.is_registered(_watch_import):
            bpy.app.timers.register(_watch_import, first_interval=0.5)
        with context.temp_override(window=context.window, area=self.area, region=self.region):
            bpy.ops.wm.call_menu(name="TOPBAR_MT_file_import")
        return {'FINISHED'}

    def _set_query(self, query):
        if query == _picker["query"]:
            return
        _picker.update(query=query, scroll=0.0, target=0.0)
        shown = [item[0] for _label, items in _sections() for item in items]
        selected = "file" if _picker["view"] != 'STARTERS' else "selected"
        if _picker[selected] not in shown:
            _picker[selected] = shown[0] if shown else None
        self.area.tag_redraw()

    def _set_view(self, view):
        """Show every recent file ('RECENT') or project file ('PROJECTS') in place of the base meshes,
        or the base meshes again ('STARTERS')."""
        # The search starts over; the tiles fade in where they belong.
        _picker.update(view=view, query="", scroll=0.0, target=0.0, anim={}, hover=None)
        if view != 'STARTERS':
            files = _grid_files()
            _load_file_pixels(files)
            if _picker["file"] not in [("FILE", path) for path in files]:
                _picker["file"] = ("FILE", files[0]) if files else None
        _play("click")
        self.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _accept(self, context, key):
        """Start sculpting a base mesh, or open a recent file."""
        if isinstance(key, tuple):
            return self._open_recent(context, key[1])
        return self._start(context, key)

    def _refresh_recent(self, select=None):
        """Read the recent and project files again after one was renamed, duplicated or deleted."""
        _picker.update(recent=recent_files(), projects=project_files())
        if _picker["view"] != 'STARTERS':
            _load_file_pixels(_grid_files())
        keys = [("FILE", path) for path in _grid_files()]
        if select is not None and ("FILE", select) in keys:
            _picker["file"] = ("FILE", select)
        elif _picker["file"] not in keys:
            _picker["file"] = keys[0] if keys else None
        _picker["button"] = None
        self.area.tag_redraw()

    @staticmethod
    def _file_at(layout, mx, my):
        """The recent file under the mouse: a View All tile, or a row of the list."""
        key = layout.tile_at(mx, my)
        if isinstance(key, tuple):
            return key[1]
        button = layout.button_at(mx, my)
        if isinstance(button, tuple) and button[0] in {"RECENT", "PROJECT", "TRASH"}:
            return button[1]
        return None

    def _ask(self, kind, path):
        name = bpy.path.basename(path)
        _picker.update(dialog={"kind": kind, "path": path, "error": "",
                               "text": name[:-len(".blend")] if name.lower().endswith(".blend") else name},
                       menu=None, settings=False, button=None)
        _play("menu_open")
        self.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _file_action(self, context, action, path):
        """Do what was picked in a recent file's right-click menu."""
        if action == 'OPEN':
            return self._open_recent(context, path)
        if action in {'RENAME', 'DELETE'}:
            return self._ask(action, path)
        _play("click")
        if action == 'LOCATION':
            show_in_folder(path)
        elif action == 'DUPLICATE':
            new_path, error = duplicate_file(path)
            if error:
                self.report({'WARNING'}, "Couldn't duplicate %s: %s" % (bpy.path.basename(path), error))
            self._refresh_recent(select=new_path)
        elif action == 'FORGET':
            _edit_recent('REMOVE', path)
            self._refresh_recent()
        self.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _dialog_confirm(self):
        dialog = _picker["dialog"]
        if dialog["kind"] == 'DELETE':
            new_path, error = None, delete_file(dialog["path"])
        else:
            new_path, error = rename_file(dialog["path"], dialog["text"])
        if error:
            dialog["error"] = error
            _play("error")
        else:
            _picker["dialog"] = None
            _play("click")
            self._refresh_recent(select=new_path)
        self.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _dialog_event(self, context, event, layout, mx, my):
        """Events while Delete or Rename asks: nothing else in the picker can be used."""
        dialog = _picker["dialog"]
        if event.type == 'MOUSEMOVE':
            button = layout.button_at(mx, my)
            if button != _picker["button"]:
                _picker["button"] = button
                self.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}
        button = layout.button_at(mx, my) if event.type == 'LEFTMOUSE' else None
        if event.type in {'RET', 'NUMPAD_ENTER'} or button == 'DIALOG_OK':
            return self._dialog_confirm()
        if (event.type in {'ESC', 'RIGHTMOUSE'} or button == 'DIALOG_CANCEL' or
                (event.type == 'LEFTMOUSE' and not layout._over(layout.dialog_box, mx, my))):
            _picker.update(dialog=None, button=None)
            _play("cancel")
            self.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if dialog["kind"] == 'RENAME':
            text = dialog["text"]
            if event.type == 'BACK_SPACE':
                text = "" if event.ctrl else text[:-1]
            elif event.type == 'V' and event.ctrl:
                text += context.window_manager.clipboard.splitlines()[0] if context.window_manager.clipboard else ""
            elif event.unicode and event.unicode.isprintable() and not (event.ctrl or event.alt or event.oskey):
                text += event.unicode
            if text != dialog["text"]:
                dialog.update(text=text, error="")
                self.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        region = self.region
        mx, my = event.mouse_x - region.x, event.mouse_y - region.y
        layout = _Layout(region, _picker["scroll"])
        over_area = (self.area.x <= event.mouse_x < self.area.x + self.area.width and
                     self.area.y <= event.mouse_y < self.area.y + self.area.height)

        if event.type == 'TIMER':
            # Ease the scroll towards where the wheel sent it, and keep the fade-in going.
            target = min(max(_picker["target"], 0.0), layout.max_scroll)
            _picker["target"] = target
            delta = target - _picker["scroll"]
            moving = _animate(layout)
            # The search field's cursor blinks twice a second.
            blink = int(time.perf_counter() * 2.0)
            if blink != _picker.get("blink"):
                _picker["blink"] = blink
                moving = True
            if abs(delta) > 0.5 or time.perf_counter() - _picker["opened"] < FADE_TIME + 0.05:
                _picker["scroll"] += delta * 0.35
                moving = True
            if moving:
                self.area.tag_redraw()
            return {'PASS_THROUGH'}
        if _picker["dialog"]:
            return self._dialog_event(context, event, layout, mx, my)
        if _picker["menu"]:
            # The right-click menu: a click on one of its actions does it; any other click, or Esc,
            # closes it. Nothing else happens while it's open.
            if event.type == 'MOUSEMOVE':
                button = layout.button_at(mx, my)
                if button != _picker["button"]:
                    _picker["button"] = button
                    self.area.tag_redraw()
            elif event.value == 'PRESS' and event.type in {'LEFTMOUSE', 'RIGHTMOUSE', 'ESC'}:
                button = layout.button_at(mx, my) if event.type == 'LEFTMOUSE' else None
                path = _picker["menu"]["path"]
                _picker.update(menu=None, button=None)
                self.area.tag_redraw()
                if isinstance(button, tuple) and button[0] == "MENU":
                    return self._file_action(context, button[1], path)
            return {'RUNNING_MODAL'}
        if event.type == 'RIGHTMOUSE' and event.value == 'PRESS' and over_area:
            # Right-click on a recent file: what can be done with it.
            path = self._file_at(layout, mx, my)
            if path is not None:
                _picker.update(menu={"path": path, "x": mx, "y": my}, settings=False, button=None)
                if _picker["view"] != 'STARTERS':
                    _picker["file"] = ("FILE", path)
                _play("menu_open")
                self.area.tag_redraw()
                return {'RUNNING_MODAL'}
        if event.type == 'ESC' and event.value == 'PRESS' and _picker["settings"]:
            # Esc closes the settings first, then clears the search, then goes back from View All,
            # then closes.
            _picker["settings"] = False
            self.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'ESC' and event.value == 'PRESS' and _picker["query"]:
            self._set_query("")
            return {'RUNNING_MODAL'}
        if event.type == 'ESC' and event.value == 'PRESS' and _picker["view"] != 'STARTERS':
            return self._set_view('STARTERS')
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            _play(CLOSE_SOUND)
            self._close(context)
            return {'CANCELLED'}
        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            return self._accept(context, _picker["file" if _picker["view"] != 'STARTERS' else "selected"])
        # Typing goes into the search field.
        if event.value == 'PRESS' and event.type == 'BACK_SPACE':
            self._set_query("" if event.ctrl else _picker["query"][:-1])
            return {'RUNNING_MODAL'}
        if (event.value == 'PRESS' and event.unicode and event.unicode.isprintable()
                and not (event.ctrl or event.alt or event.oskey)):
            self._set_query((_picker["query"] + event.unicode).lstrip())
            return {'RUNNING_MODAL'}
        if not over_area:
            return {'PASS_THROUGH'}
        if event.type == 'MOUSEMOVE':
            hover = layout.tile_at(mx, my)
            button = layout.button_at(mx, my)
            if hover != _picker["hover"] or button != _picker["button"]:
                _picker.update(hover=hover, button=button)
                self.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            step = (layout.thumb + layout.label) * 0.6
            _picker["target"] += -step if event.type == 'WHEELUPMOUSE' else step
            return {'RUNNING_MODAL'}
        if event.type == 'TRACKPADPAN':
            _picker["target"] += event.mouse_y - event.mouse_prev_y
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and self.import_pressed:
            self.import_pressed = False
            if layout.button_at(mx, my) == 'IMPORT':
                return self._import(context)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'DOUBLE_CLICK':
            key = layout.tile_at(mx, my)
            return self._accept(context, key) if key else {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if not layout.inside(mx, my):
                _play(CLOSE_SOUND)
                self._close(context)
                return {'CANCELLED'}
            button = layout.button_at(mx, my)
            if _picker["settings"] and button != 'SETTINGS' and button not in layout.settings_names:
                # A click away from the settings closes them.
                _picker["settings"] = False
                self.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if button == 'SETTINGS':
                _picker["settings"] = not _picker["settings"]
                _play("menu_open" if _picker["settings"] else "click")
                self.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if button == 'VIEW_ALL':
                return self._set_view('RECENT')
            if button == 'VIEW_ALL_PROJECTS':
                return self._set_view('PROJECTS')
            if button == 'BACK':
                return self._set_view('STARTERS')
            if button == 'ACCEPT':
                return self._accept(context, _picker["file" if _picker["view"] != 'STARTERS' else "selected"])
            if button == 'OPEN':
                return self._open(context)
            if button == 'IMPORT':
                self.import_pressed = True
                return {'RUNNING_MODAL'}
            if button in {'STARTUP_PICKER', 'STARTUP_SPLASH'}:
                set_startup_screen(button[len("STARTUP_"):])
                _play("click")
                self.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if button == 'PROJECTS_FOLDER':
                # A folder browser (its own window); the picker stays open and lists the new folder.
                _play("click")
                with context.temp_override(window=context.window, area=self.area, region=self.region):
                    bpy.ops.phantom.projects_folder('INVOKE_DEFAULT')
                return {'RUNNING_MODAL'}
            if button == 'SEARCH':
                return {'RUNNING_MODAL'}
            if button == 'ASSOCIATE' and _picker.get("associated") is not None:
                return self._associate(context, not _picker["associated"])
            if button == 'PREVIEWS' and _picker.get("previews") is not None:
                set_blend_previews(not _picker["previews"])
                _picker["previews"] = blend_previews_shown()
                _play("toggle_on" if _picker["previews"] else "toggle_off")
                self.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if isinstance(button, tuple) and button[0] == "NEW":
                return self._new_file(context, button[1])
            if isinstance(button, tuple) and button[0] == "TRASH":
                return self._ask('DELETE', button[1])
            if isinstance(button, tuple) and button[0] in {"RECENT", "PROJECT"}:
                return self._open_recent(context, button[1])
            key = layout.tile_at(mx, my)
            now = time.perf_counter()
            # A second press on the same tile in time is a double-click too (Blender only reports
            # DOUBLE_CLICK for some presses).
            double_time = context.preferences.inputs.mouse_double_click_time / 1000.0
            if key and key == self.last_press[0] and now - self.last_press[1] < double_time:
                return self._accept(context, key)
            self.last_press = (key, now)
            selected = "file" if isinstance(key, tuple) else "selected"
            if key and key != _picker[selected]:
                _picker[selected] = key
                _play("click")
                self.area.tag_redraw()
            return {'RUNNING_MODAL'}
        # Keep clicks and keys from reaching the view behind the picker.
        return {'RUNNING_MODAL'}


def _draw():
    region = bpy.context.region
    if not _picker["open"] or region is None or region.as_pointer() != _picker["region"]:
        return
    layout = _Layout(region, _picker["scroll"])
    s = layout.s
    fade = min(1.0, (time.perf_counter() - _picker["opened"]) / FADE_TIME)
    ease = 1.0 - (1.0 - fade) ** 3
    gpu.state.blend_set('ALPHA')

    # Dim the view; the panel rises slightly into place as it fades in.
    _rounded_rect(0, 0, region.width, region.height, 0, (0.0, 0.0, 0.0, 0.35 * ease))
    rise = (1.0 - ease) * 12 * s
    x, y, w, h = layout.x, layout.y - rise, layout.width, layout.height
    _rounded_rect(x - s, y - s, w + 2 * s, h + 2 * s, 12 * s, (0.0, 0.0, 0.0, 0.45 * ease))
    _rounded_rect(x, y, w, h, 11 * s, (0.125, 0.125, 0.145, 0.98 * ease))
    # The splash image across the top: at full height in the middle, its edge columns stretched
    # out to the sides (its background is a gradient from top to bottom).
    splash = _texture("SPLASH")
    if splash is not None:
        bh = layout.banner
        by = y + h - bh
        iw = bh * splash.width / splash.height
        ix = x + (w - iw) / 2.0
        edge = 1.0 / splash.width
        _image(splash, x, by, ix - x + 1, bh, ease, 0.0, edge)
        _image(splash, ix + iw - 1, by, x + w - ix - iw + 1, bh, ease, 1.0 - edge, 1.0)
        _image(splash, ix, by, iw, bh, ease)
    _text(x + w / 2.0, layout.title_y - rise, "Where would you like to start?", 17 * s, (1.0, 1.0, 1.0, ease))

    def button(name, label, primary=False, enabled=True):
        bx, by, bw, bh = layout.buttons[name]
        by -= rise
        hovered = _picker["button"] == name and enabled
        if primary:
            color = (0.56, 0.54, 1.0, ease) if hovered else (0.48, 0.46, 1.0, ease if enabled else 0.35 * ease)
        else:
            color = (0.3, 0.3, 0.35, ease) if hovered else (0.22, 0.22, 0.26, ease)
        _rounded_rect(bx, by, bw, bh, 6 * s, color)
        _text(bx + bw / 2.0, by + bh / 2.0 - 4 * s, label, 12 * s, (1.0, 1.0, 1.0, ease if enabled else 0.5 * ease))

    # Search field: what's typed, or a hint.
    sx, sy, sw, sh = layout.buttons['SEARCH']
    sy -= rise
    _rounded_rect(sx, sy, sw, sh, 6 * s, (0.09, 0.09, 0.11, ease))
    query = _picker["query"]
    if query:
        _text(sx + 12 * s, sy + sh / 2.0 - 4 * s, query, 12 * s, (1.0, 1.0, 1.0, ease), align='LEFT')
        blf.size(0, 12 * s)
        caret_x = sx + 12 * s + blf.dimensions(0, query)[0] + 2 * s
        if int(time.perf_counter() * 2.0) % 2 == 0:
            _rounded_rect(caret_x, sy + 7 * s, 1.5 * s, sh - 14 * s, 0, (1.0, 1.0, 1.0, 0.8 * ease))
    else:
        # Ready to type: a blinking cursor at the start, and what it's for.
        if int(time.perf_counter() * 2.0) % 2 == 0:
            _rounded_rect(sx + 12 * s, sy + 7 * s, 1.5 * s, sh - 14 * s, 0, (1.0, 1.0, 1.0, 0.8 * ease))
        _text(sx + 18 * s, sy + sh / 2.0 - 4 * s, "Search", 12 * s, (0.55, 0.55, 0.6, ease), align='LEFT')
    button('OPEN', "Open...")
    button('IMPORT', "Import...")

    # New File, like the splash screen.
    lx, ly = layout.new_label
    _text(lx, ly - rise - 4 * s, "New File", 12 * s, (0.65, 0.65, 0.7, ease), align='LEFT')
    icon_size = 16 * s
    for name in layout.buttons:
        if not (isinstance(name, tuple) and name[0] == "NEW"):
            continue
        bx, by, bw, bh = layout.buttons[name]
        by -= rise
        hovered = _picker["button"] == name
        _rounded_rect(bx, by, bw, bh, 6 * s, (0.3, 0.3, 0.35, ease) if hovered else (0.22, 0.22, 0.26, ease))
        blf.size(0, 12 * s)
        label_w = blf.dimensions(0, name[2])[0]
        left = bx + (bw - (icon_size + 6 * s + label_w)) / 2.0
        bpy.types.WindowManager.phantom_draw_icon(
            new_file_icon(name[1]), left, by + (bh - icon_size) / 2.0, icon_size, alpha=ease)
        gpu.state.blend_set('ALPHA')
        _text(left + icon_size + 6 * s, by + bh / 2.0 - 4 * s, name[2], 12 * s, (1.0, 1.0, 1.0, ease), align='LEFT')

    def checkbox(name, label, on):
        tx_, ty_, tw_, th_ = layout.buttons[name]
        ty_ -= rise
        box = 16 * s
        bx_, by_ = tx_ + 6 * s, ty_ + (th_ - box) / 2.0
        if _picker["button"] == name:
            _rounded_rect(tx_, ty_, tw_, th_, 5 * s, (0.24, 0.24, 0.28, ease))
        _rounded_rect(bx_, by_, box, box, 4 * s, (0.48, 0.46, 1.0, ease) if on else (0.26, 0.26, 0.3, ease))
        if on:
            _text(bx_ + box / 2.0, by_ + 3 * s, "\u2713", 13 * s, (1.0, 1.0, 1.0, ease))
        _text(bx_ + box + 8 * s, ty_ + th_ / 2.0 - 4 * s, label, 12 * s, (0.85, 0.85, 0.9, ease), align='LEFT')

    def trash_button(name, alpha):
        if name not in layout.buttons:
            return
        bx, by, bw, bh = layout.buttons[name]
        by -= rise
        hovered = _picker["button"] == name
        _rounded_rect(bx, by, bw, bh, 5 * s, (0.85, 0.22, 0.22, alpha) if hovered else (0.08, 0.08, 0.1, 0.75 * alpha))
        icon_size = 14 * s
        bpy.types.WindowManager.phantom_draw_icon(
            'TRASH', bx + (bw - icon_size) / 2.0, by + (bh - icon_size) / 2.0, icon_size, alpha=alpha)
        gpu.state.blend_set('ALPHA')

    def link(name, label):
        bx, by, bw, bh = layout.buttons[name]
        by -= rise
        hovered = _picker["button"] == name
        if hovered:
            _rounded_rect(bx, by, bw, bh, 5 * s, (0.24, 0.24, 0.28, ease))
        _text(bx + bw / 2.0, by + bh / 2.0 - 4 * s, label, 11 * s,
              (0.62, 0.6, 1.0, ease) if hovered else (0.52, 0.5, 0.95, ease))

    def side_list(kind, rows):
        menu_path = (_picker["menu"] or {}).get("path")
        for path, (bx, by, bw, bh) in rows:
            by -= rise
            hovered = _picker["button"] in {(kind, path), ("TRASH", path, kind)} or path == menu_path
            if hovered:
                _rounded_rect(bx, by, bw, bh, 5 * s, (0.24, 0.24, 0.28, ease))
            name = bpy.path.basename(path)
            blf.size(0, 12 * s)
            # Room for the trash can when it shows.
            room = bw - (40 if hovered else 16) * s
            while len(name) > 4 and blf.dimensions(0, name)[0] > room:
                name = name[:-4] + "..."
            _text(bx + 8 * s, by + bh / 2.0 - 4 * s, name, 12 * s,
                  (1.0, 1.0, 1.0, ease) if hovered else (0.85, 0.85, 0.9, ease), align='LEFT')
            if hovered:
                trash_button(("TRASH", path, kind), ease)

    def fit_path(folder, room, size):
        blf.size(0, size)
        while len(folder) > 4 and blf.dimensions(0, folder)[0] > room:
            folder = "..." + folder[4:]
        return folder

    grid_view = _picker["view"] != 'STARTERS'
    hovered_path = None
    if grid_view:
        # View All: the way back to the base meshes at the top left, then the title (and where the
        # project files are).
        bx, by, bw, bh = layout.buttons['BACK']
        by -= rise
        if _picker["button"] == 'BACK':
            _rounded_rect(bx, by, bw, bh, 5 * s, (0.3, 0.3, 0.35, ease))
        icon_size = 14 * s
        bpy.types.WindowManager.phantom_draw_icon(
            'BACK', bx + (bw - icon_size) / 2.0, by + (bh - icon_size) / 2.0, icon_size, alpha=ease)
        gpu.state.blend_set('ALPHA')
        tx, ty = layout.back_title
        title = "Project Files" if _picker["view"] == 'PROJECTS' else "Recent Files"
        _text(tx + 24 * s, ty - rise, title, 12 * s, (0.75, 0.75, 0.8, ease), align='LEFT')
        if _picker["view"] == 'PROJECTS':
            blf.size(0, 12 * s)
            px = tx + 24 * s + blf.dimensions(0, title)[0] + 12 * s
            _text(px, ty - rise, fit_path(projects_folder(), x + w - layout.pad - px, 11 * s), 11 * s,
                  (0.55, 0.55, 0.6, ease), align='LEFT')
        if isinstance(_picker["hover"], tuple):
            hovered_path = _picker["hover"][1]
    else:
        # Recent files down the right side, then project files, with View All by their titles.
        rx, ry = layout.recent_title
        _text(rx, ry - rise, "Recent Files", 12 * s, (0.75, 0.75, 0.8, ease), align='LEFT')
        if not layout.recent:
            _text(rx, ry - rise - 30 * s, "None yet", 11 * s, (0.55, 0.55, 0.6, ease), align='LEFT')
        if 'VIEW_ALL' in layout.buttons:
            link('VIEW_ALL', "View All")
        side_list("RECENT", layout.recent)
        px, py = layout.projects_title
        _text(px, py - rise, "Project Files", 12 * s, (0.75, 0.75, 0.8, ease), align='LEFT')
        if 'VIEW_ALL_PROJECTS' in layout.buttons:
            link('VIEW_ALL_PROJECTS', "View All")
        if not layout.projects:
            _text(px, py - rise - 30 * s, "Files you save go here", 11 * s, (0.55, 0.55, 0.6, ease), align='LEFT')
        side_list("PROJECT", layout.projects)
        # The hovered file's folder, along the bottom of the column.
        if isinstance(_picker["button"], tuple) and _picker["button"][0] in {"RECENT", "PROJECT", "TRASH"}:
            folder = fit_path(os.path.dirname(_picker["button"][1]), layout.side_w - layout.pad - 8 * s, 10 * s)
            _text(rx, layout.view_bottom - rise + 4 * s, folder, 10 * s, (0.55, 0.55, 0.6, ease), align='LEFT')

    # Bottom: the cogwheel for the picker's settings on the left, Open on the right.
    bx, by, bw, bh = layout.buttons['SETTINGS']
    by -= rise
    if _picker["settings"] or _picker["button"] == 'SETTINGS':
        _rounded_rect(bx, by, bw, bh, 6 * s, (0.3, 0.3, 0.35, ease))
    icon_size = 16 * s
    bpy.types.WindowManager.phantom_draw_icon(
        'PREFERENCES', bx + (bw - icon_size) / 2.0, by + (bh - icon_size) / 2.0, icon_size, alpha=ease)
    gpu.state.blend_set('ALPHA')
    if hovered_path:
        # View All: the hovered file's folder, beside the cogwheel.
        folder = os.path.dirname(hovered_path)
        room = layout.buttons['ACCEPT'][0] - (bx + bw + 24 * s)
        blf.size(0, 10 * s)
        while len(folder) > 4 and blf.dimensions(0, folder)[0] > room:
            folder = "..." + folder[4:]
        _text(bx + bw + 12 * s, by + bh / 2.0 - 4 * s, folder, 10 * s, (0.55, 0.55, 0.6, ease), align='LEFT')
    selected = _picker["file"] if grid_view else _picker["selected"]
    button('ACCEPT', "Open", primary=True, enabled=selected is not None)

    # The scrolling part, clipped between the bars.
    gpu.state.scissor_test_set(True)
    gpu.state.scissor_set(int(x), int(layout.view_bottom), int(layout.content_w), max(1, int(layout.view_h)))
    if not layout.tiles and not _picker["anim"]:
        empty = ("Nothing called \"%s\"" % query if query else
                 "No project files yet" if _picker["view"] == 'PROJECTS' else "No recent files")
        _text(x + layout.content_w / 2.0, layout.view_top - 50 * s, empty, 12 * s, (0.7, 0.7, 0.75, ease))
    for label, ly in layout.sections:
        _text(x + layout.pad, ly - rise, label, 12 * s, (0.75, 0.75, 0.8, ease), align='LEFT')
    tile_h = layout.tile_h
    shown = {key for key, _x, _y in layout.tiles}
    # Leaving tiles underneath the ones staying.
    for key, (ax, ay, alpha) in sorted(_picker["anim"].items(), key=lambda item: item[0] in shown):
        tx, ty = layout.x + ax, layout.content_top + ay - rise
        a = ease * max(0.0, min(1.0, alpha))
        if a <= 0.0 or ty > layout.view_top or ty + tile_h < layout.view_bottom:
            continue
        hovered = key == _picker["hover"]
        selected = key == _picker["file" if isinstance(key, tuple) else "selected"]
        if selected:
            _rounded_rect(tx - 2 * s, ty - 2 * s, layout.tile + 4 * s, tile_h + 4 * s, 9 * s, (0.48, 0.46, 1.0, a))
        _rounded_rect(tx, ty, layout.tile, tile_h, 8 * s,
                      (0.24, 0.24, 0.28, a) if hovered else (0.17, 0.17, 0.2, a))
        if isinstance(key, tuple):
            # A file: the picture saved with it, as large as the card's picture area allows (all of
            # it), or the file icon.
            px, py = tx + (layout.tile - layout.thumb_w) / 2.0, ty + layout.label
            _rounded_rect(px, py, layout.thumb_w, layout.thumb, 4 * s, (0.1, 0.1, 0.12, a))
            texture = _file_texture(key[1])
            if texture is not None:
                fit = min(layout.thumb_w / texture.width, layout.thumb / texture.height)
                iw, ih = texture.width * fit, texture.height * fit
                _image(texture, px + (layout.thumb_w - iw) / 2.0, py + (layout.thumb - ih) / 2.0, iw, ih, a)
            else:
                icon_size = 48 * s
                bpy.types.WindowManager.phantom_draw_icon(
                    'FILE_BLEND', px + (layout.thumb_w - icon_size) / 2.0,
                    py + (layout.thumb - icon_size) / 2.0, icon_size, alpha=a * 0.6)
                gpu.state.blend_set('ALPHA')
        else:
            texture = _texture(key)
            if texture is not None:
                inset = (layout.tile - layout.thumb) / 2.0
                _image(texture, tx + inset, ty + layout.label, layout.thumb, layout.thumb, a)
        if isinstance(key, tuple) and (hovered or (_picker["menu"] or {}).get("path") == key[1]):
            trash_button(("TRASH", key[1], "TILE"), a)
        name = _tile_label(key)
        blf.size(0, 11 * s)
        while len(name) > 4 and blf.dimensions(0, name)[0] > layout.tile - 12 * s:
            name = name[:-4] + "..."
        _text(tx + layout.tile / 2.0, ty + (layout.label - 11 * s) / 2.0 + 1 * s, name, 11 * s,
              (1.0, 1.0, 1.0, a) if hovered or selected else (0.82, 0.82, 0.86, a))
    gpu.state.scissor_test_set(False)

    if layout.max_scroll > 0.0:
        track_h = layout.view_h - 16 * s
        bar_h = max(30 * s, track_h * layout.view_h / layout.content)
        bar_y = layout.view_bottom - rise + 8 * s + (track_h - bar_h) * (1.0 - layout.scroll / layout.max_scroll)
        _rounded_rect(x + layout.content_w - 9 * s, bar_y, 4 * s, bar_h, 2 * s, (1.0, 1.0, 1.0, 0.25 * ease))

    # The cogwheel's settings, over everything.
    if layout.settings:
        bx, by, bw, bh = layout.settings_box
        by -= rise
        _rounded_rect(bx - s, by - s, bw + 2 * s, bh + 2 * s, 9 * s, (0.0, 0.0, 0.0, 0.5 * ease))
        _rounded_rect(bx, by, bw, bh, 8 * s, (0.16, 0.16, 0.19, ease))
        values = {'ASSOCIATE': _picker.get("associated"), 'PREVIEWS': _picker.get("previews")}
        for kind, name, label, (rx_, ry_, rw_, rh_) in layout.settings:
            ry_ -= rise
            if kind == 'CHECK':
                checkbox(name, label, bool(values[name]))
                continue
            if kind == 'STARTUP':
                _text(rx_ + 6 * s, ry_ + rh_ / 2.0 - 4 * s, label, 12 * s, (0.85, 0.85, 0.9, ease), align='LEFT')
                current = startup_screen()
                for choice, text in (('PICKER', "Start Picker"), ('SPLASH', "Splash Screen")):
                    name_ = 'STARTUP_' + choice
                    bx, by, bw, bh = layout.buttons[name_]
                    by -= rise
                    on = current == choice
                    hovered = _picker["button"] == name_
                    _rounded_rect(bx, by, bw, bh, 6 * s, (0.48, 0.46, 1.0, ease) if on else
                                  ((0.3, 0.3, 0.35, ease) if hovered else (0.22, 0.22, 0.26, ease)))
                    _text(bx + bw / 2.0, by + bh / 2.0 - 4 * s, text, 11 * s, (1.0, 1.0, 1.0, ease))
                continue
            # The projects folder: where it is, and Change...
            bx, by, bw, bh = layout.buttons[name]
            by -= rise
            _text(rx_ + 6 * s, ry_ + rh_ - 18 * s, label, 12 * s, (0.85, 0.85, 0.9, ease), align='LEFT')
            _text(rx_ + 6 * s, ry_ + 9 * s, fit_path(projects_folder(), bx - rx_ - 16 * s, 10 * s), 10 * s,
                  (0.55, 0.55, 0.6, ease), align='LEFT')
            hovered = _picker["button"] == name
            _rounded_rect(bx, by, bw, bh, 6 * s, (0.3, 0.3, 0.35, ease) if hovered else (0.22, 0.22, 0.26, ease))
            _text(bx + bw / 2.0, by + bh / 2.0 - 4 * s, "Change...", 11 * s, (1.0, 1.0, 1.0, ease))

    # A recent file's right-click menu.
    if layout.menu_box is not None:
        bx, by, bw, bh = layout.menu_box
        _rounded_rect(bx - s, by - s, bw + 2 * s, bh + 2 * s, 8 * s, (0.0, 0.0, 0.0, 0.55 * ease))
        _rounded_rect(bx, by, bw, bh, 7 * s, (0.16, 0.16, 0.19, ease))
        icon_size = 14 * s
        for entry in layout.menu_items:
            if entry[0] is None:
                _rounded_rect(bx + 10 * s, entry[1], bw - 20 * s, max(1.0, s), 0, (1.0, 1.0, 1.0, 0.1 * ease))
                continue
            (action, label, icon), (ix, iy, iw, ih), enabled = entry
            hovered = _picker["button"] == ("MENU", action)
            if hovered:
                _rounded_rect(ix, iy, iw, ih, 5 * s, (0.85, 0.22, 0.22, ease) if action == 'DELETE' else
                              (0.48, 0.46, 1.0, ease))
            alpha = ease if enabled else 0.35 * ease
            bpy.types.WindowManager.phantom_draw_icon(icon, ix + 8 * s, iy + (ih - icon_size) / 2.0, icon_size,
                                                      alpha=alpha)
            gpu.state.blend_set('ALPHA')
            _text(ix + 30 * s, iy + ih / 2.0 - 4 * s, label, 12 * s,
                  (1.0, 1.0, 1.0, alpha) if hovered else (0.88, 0.88, 0.92, alpha), align='LEFT')

    # Delete or Rename, asked over a dimmed picker.
    dialog = _picker["dialog"]
    if dialog and layout.dialog_box is not None:
        _rounded_rect(x, y, w, h, 11 * s, (0.0, 0.0, 0.0, 0.65 * ease))
        dx, dy, dw, dh = layout.dialog_box
        _rounded_rect(dx - s, dy - s, dw + 2 * s, dh + 2 * s, 11 * s, (0.0, 0.0, 0.0, 0.6 * ease))
        _rounded_rect(dx, dy, dw, dh, 10 * s, (0.15, 0.15, 0.18, ease))
        name = bpy.path.basename(dialog["path"])
        blf.size(0, 14 * s)
        while len(name) > 4 and blf.dimensions(0, name)[0] > dw - 120 * s:
            name = name[:-4] + "..."
        if dialog["kind"] == 'DELETE':
            title = "Delete %s?" % name
            note = ("It goes to the Recycle Bin, where it can be restored." if sys.platform == "win32"
                    else "It's deleted for good.")
            ok, ok_color = "Delete", (0.85, 0.22, 0.22)
        else:
            title = "Rename %s" % name
            note = None
            ok, ok_color = "Rename", (0.48, 0.46, 1.0)
        _text(dx + 20 * s, dy + dh - 34 * s, title, 14 * s, (1.0, 1.0, 1.0, ease), align='LEFT')
        if note:
            _text(dx + 20 * s, dy + dh - 62 * s, note, 12 * s, (0.7, 0.7, 0.75, ease), align='LEFT')
        if layout.dialog_field is not None:
            fx, fy, fw, fh = layout.dialog_field
            _rounded_rect(fx - s, fy - s, fw + 2 * s, fh + 2 * s, 7 * s, (0.48, 0.46, 1.0, ease))
            _rounded_rect(fx, fy, fw, fh, 6 * s, (0.09, 0.09, 0.11, ease))
            text, suffix = dialog["text"], ".blend"
            blf.size(0, 12 * s)
            # The end of a long name, where the typing is.
            while len(text) > 1 and blf.dimensions(0, text + suffix)[0] > fw - 30 * s:
                text = text[1:]
            _text(fx + 10 * s, fy + fh / 2.0 - 4 * s, text, 12 * s, (1.0, 1.0, 1.0, ease), align='LEFT')
            caret_x = fx + 10 * s + blf.dimensions(0, text)[0] + 1 * s
            _text(caret_x + 2 * s, fy + fh / 2.0 - 4 * s, suffix, 12 * s, (0.5, 0.5, 0.55, ease), align='LEFT')
            if int(time.perf_counter() * 2.0) % 2 == 0:
                _rounded_rect(caret_x, fy + 7 * s, 1.5 * s, fh - 14 * s, 0, (1.0, 1.0, 1.0, 0.8 * ease))
        if dialog["error"]:
            ey = dy + 56 * s if layout.dialog_field is None else dy + 104 * s
            _text(dx + 20 * s, ey, dialog["error"], 11 * s, (1.0, 0.45, 0.42, ease), align='LEFT')
        for name_, label, color in (('DIALOG_CANCEL', "Cancel", None), ('DIALOG_OK', ok, ok_color)):
            bx, by, bw, bh = layout.buttons[name_]
            hovered = _picker["button"] == name_
            if color is None:
                fill = (0.3, 0.3, 0.35, ease) if hovered else (0.22, 0.22, 0.26, ease)
            else:
                lift = 0.08 if hovered else 0.0
                fill = (min(1.0, color[0] + lift), min(1.0, color[1] + lift), min(1.0, color[2] + lift), ease)
            _rounded_rect(bx, by, bw, bh, 6 * s, fill)
            _text(bx + bw / 2.0, by + bh / 2.0 - 4 * s, label, 12 * s, (1.0, 1.0, 1.0, ease))
    gpu.state.blend_set('NONE')


class PHANTOM_OT_projects_folder(Operator):
    """Choose the folder new files are saved in and Project Files are listed from"""
    bl_idname = "phantom.projects_folder"
    bl_label = "Choose Projects Folder"
    bl_options = {'INTERNAL'}

    directory: bpy.props.StringProperty(subtype='DIR_PATH')
    filter_folder: bpy.props.BoolProperty(default=True, options={'HIDDEN'})

    def invoke(self, context, _event):
        folder = projects_folder()
        os.makedirs(folder, exist_ok=True)
        self.directory = folder + os.sep
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.directory:
            return {'CANCELLED'}
        set_projects_folder(bpy.path.abspath(self.directory))
        if _picker["open"]:
            _picker["projects"] = project_files()
            if _picker["view"] == 'PROJECTS':
                _load_file_pixels(_picker["projects"])
            for window in context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
        return {'FINISHED'}


def draw_add_menu(self, context):
    if context.mode == 'OBJECT':
        self.layout.separator()
        self.layout.operator_context = 'INVOKE_DEFAULT'
        self.layout.operator(PHANTOM_OT_start_picker.bl_idname, text="Base Mesh...", icon='OUTLINER_OB_ARMATURE')


def _watch():
    """Offer the picker when a window switches to a sculpting workspace with an empty canvas."""
    wm = bpy.context.window_manager
    for window in wm.windows if wm else ():
        workspace = window.workspace
        key = window.as_pointer()
        previous = _last_workspace.get(key)
        _last_workspace[key] = workspace.name if workspace else None
        if workspace is None or previous is None or previous == workspace.name:
            continue
        if workspace.object_mode != 'SCULPT' or not canvas_is_empty(window.scene):
            continue
        if time.perf_counter() < _picker["quiet_until"]:
            # The picker itself switched workspaces to sculpt what was picked.
            continue
        area, region = _view3d(window)
        if area is None:
            continue
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.phantom.start_picker('INVOKE_DEFAULT')
    return WATCH_INTERVAL


def _watch_import():
    """After Import... in the picker: sculpt the mesh the import brought in."""
    if _import["before"] is None or time.perf_counter() > _import["deadline"]:
        _import["before"] = None
        return None
    context = bpy.context
    window = context.window or (context.window_manager.windows[0] if context.window_manager.windows else None)
    if window is None:
        return 0.5
    new = [ob for ob in window.scene.objects if ob.name not in _import["before"] and ob.type == 'MESH']
    if not new:
        return 0.5
    _import["before"] = None
    target = max(new, key=lambda ob: len(ob.data.vertices))
    area, region = _view3d(window)
    if area is None:
        return None
    with context.temp_override(window=window, area=area, region=region):
        if context.object is not None and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for ob in window.view_layer.objects:
            if ob is not None and ob.select_get():
                ob.select_set(False)
        _frame_and_sculpt(bpy.context, target, new)
    _play("mode_switch")
    return None


def _open_at_startup():
    wm = bpy.context.window_manager
    window = wm.windows[0] if wm and wm.windows else None
    if window is None or _picker["open"] or not canvas_is_empty(window.scene):
        return None
    area, region = _view3d(window)
    if area is not None:
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.phantom.start_picker('INVOKE_DEFAULT', at_startup=True)
    return None


@persistent
def _on_load(*_args):
    # Phantom starting, or File > New: offer the picker if that's what the user chose.
    if bpy.app.background or bpy.data.filepath or not show_at_startup():
        return
    bpy.app.timers.register(_open_at_startup, first_interval=0.4)


classes = (
    PHANTOM_OT_start_asset,
    PHANTOM_OT_start_picker,
    PHANTOM_OT_projects_folder,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.phantom_blend_previews = bpy.props.BoolProperty(
        name="Show Previews of .blend Files",
        description="Have Windows Explorer show what is in .blend files (a picture saved with "
        "them) instead of Phantom's file icon",
        get=_get_blend_previews, set=_set_blend_previews)
    bpy.types.VIEW3D_MT_add.append(draw_add_menu)
    _tell_projects_folder()
    if not bpy.app.background:
        bpy.app.timers.register(_watch, first_interval=WATCH_INTERVAL, persistent=True)
    bpy.app.handlers.load_post.append(_on_load)


def unregister():
    del bpy.types.WindowManager.phantom_blend_previews
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    bpy.types.VIEW3D_MT_add.remove(draw_add_menu)
    _thumbs["textures"].clear()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
