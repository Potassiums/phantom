# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: matcap tools.

- Per-object matcaps: with Matcap lighting in Solid shading, objects can use their own matcap
  instead of the viewport's one. The choice is stored on the object as the ``phantom_matcap``
  custom property (saved with the file) and drawn by the Workbench engine.
- Search: a button next to the matcap thumbnail (the matcap grid has its own search field too).
- Right-click a matcap (in the grid or its thumbnail) to make it the default matcap for every
  project, add it to the favorites (starred, and a filter in the grid) or to a quick slot.
- Matcaps added since they were last seen get a "new!" badge in the grid until used.
- Four quick slots in the Shading popover switch matcaps with one click.

Favorites, the default matcap, quick slots and the matcaps already seen are stored in
``phantom_matcaps.json`` in the user config directory.
"""

import json
import os

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import Operator

PROPERTY = "phantom_matcap"
QUICK_SLOTS = 4
# Matcaps Blender starts new files with (its default, or the first one when that is missing).
FACTORY_MATCAPS = {"basic_1.exr", "basic_bright.exr"}
# Context member the thumbnail grid sets on each item (see `template_icon_view`).
ITEM_CONTEXT = "phantom_icon_view_item"
WATCH_INTERVAL = 1.0


def _shading(context):
    from bl_ui.space_view3d import VIEW3D_PT_shading
    return VIEW3D_PT_shading.get_shading(context)


def _targets(context):
    objects = list(context.selected_objects)
    if context.active_object is not None and context.active_object not in objects:
        objects.append(context.active_object)
    return objects


def _refresh(objects, context):
    for ob in objects:
        ob.update_tag()
    screen = context.screen
    for area in screen.areas if screen else ():
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def _label(name):
    return os.path.splitext(name)[0]


def _short_label(name):
    """Name for the small quick-slot buttons, without the pack prefixes."""
    label = _label(name)
    for prefix in ("ZB ", "Matcap ", "MatCap "):
        if label.startswith(prefix):
            label = label[len(prefix):]
    return label


# -----------------------------------------------------------------------------
# Settings

_DEFAULT_SETTINGS = {"favorites": [], "default": "", "quick": [""] * QUICK_SLOTS, "seen": None}
_settings = None


def _settings_path():
    return os.path.join(bpy.utils.user_resource('CONFIG'), "phantom_matcaps.json")


def settings():
    global _settings
    if _settings is None:
        _settings = json.loads(json.dumps(_DEFAULT_SETTINGS))
        try:
            with open(_settings_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if isinstance(data.get("favorites"), list):
            _settings["favorites"] = [n for n in data["favorites"] if isinstance(n, str)]
        if isinstance(data.get("default"), str):
            _settings["default"] = data["default"]
        if isinstance(data.get("quick"), list):
            quick = [n if isinstance(n, str) else "" for n in data["quick"]][:QUICK_SLOTS]
            _settings["quick"] = quick + [""] * (QUICK_SLOTS - len(quick))
        if isinstance(data.get("seen"), list):
            _settings["seen"] = [n for n in data["seen"] if isinstance(n, str)]
    return _settings


def _save():
    path = _settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(settings(), fh, indent=2)
    except OSError as ex:
        print("Phantom: could not save matcap settings:", ex)


def _matcap_names(context=None):
    prefs = (context or bpy.context).preferences
    return {light.name for light in prefs.studio_lights if light.type == 'MATCAP'}


def _new_matcaps(names):
    seen = settings()["seen"]
    return set() if seen is None else names - set(seen)


def _push_tags(names=None):
    """Tell the thumbnail grid which matcaps to star and which are new."""
    names = _matcap_names() if names is None else names
    data = settings()
    bpy.types.WindowManager.phantom_icon_view_tags_set(
        favorites="\n".join(data["favorites"]),
        new_items="\n".join(sorted(_new_matcaps(names))))


def _mark_seen(names):
    data = settings()
    data["seen"] = sorted(set(data["seen"] or ()) | set(names))
    _save()


def _redraw_all():
    wm = bpy.context.window_manager
    for window in wm.windows if wm else ():
        for area in window.screen.areas:
            area.tag_redraw()


# -----------------------------------------------------------------------------
# Per-object matcaps

class OBJECT_OT_phantom_matcap_apply(Operator):
    """Give the selected objects the matcap picked above, instead of following the viewport"""
    bl_idname = "object.phantom_matcap_apply"
    bl_label = "Apply Matcap to Selected"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        shading = _shading(context)
        return shading is not None and shading.light == 'MATCAP' and _targets(context)

    def execute(self, context):
        matcap = _shading(context).studio_light
        objects = _targets(context)
        for ob in objects:
            ob[PROPERTY] = matcap
        _refresh(objects, context)
        self.report({'INFO'}, f"Matcap \"{matcap}\" applied to {len(objects)} object(s)")
        return {'FINISHED'}


class OBJECT_OT_phantom_matcap_clear(Operator):
    """Make the selected objects follow the viewport's matcap again"""
    bl_idname = "object.phantom_matcap_clear"
    bl_label = "Use Viewport Matcap"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(PROPERTY in ob for ob in _targets(context))

    def execute(self, context):
        objects = [ob for ob in _targets(context) if PROPERTY in ob]
        for ob in objects:
            del ob[PROPERTY]
        _refresh(objects, context)
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Search

# Kept alive here: Blender doesn't hold on to the strings of dynamic enum items.
_search_items = []


def _matcap_search_items(_self, context):
    """Every matcap by name, without the file extension. (No icons: the matcap previews are
    only drawable inside the matcap grid itself.)"""
    _search_items.clear()
    for index, name in enumerate(sorted(_matcap_names(context), key=str.lower)):
        _search_items.append((name, _label(name), "", index))
    return _search_items


class VIEW3D_OT_phantom_matcap_search(Operator):
    """Search the matcaps by name (typos and partial words are fine)"""
    bl_idname = "view3d.phantom_matcap_search"
    bl_label = "Search Matcaps"
    bl_options = {'REGISTER'}
    bl_property = "matcap"

    matcap: EnumProperty(name="Matcap", items=_matcap_search_items)

    @classmethod
    def poll(cls, context):
        shading = _shading(context)
        return shading is not None and shading.type == 'SOLID'

    def invoke(self, context, _event):
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        shading = _shading(context)
        if shading is None:
            return {'CANCELLED'}
        shading.light = 'MATCAP'
        shading.studio_light = self.matcap
        _refresh([], context)
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Default, favorites and quick slots

class VIEW3D_OT_phantom_matcap_set_default(Operator):
    """Use this matcap in every project: new files, and files still using Blender's default"""
    bl_idname = "view3d.phantom_matcap_set_default"
    bl_label = "Set as Default Matcap"
    bl_options = {'INTERNAL'}

    name: StringProperty(options={'SKIP_SAVE'})
    clear: BoolProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        settings()["default"] = "" if self.clear else self.name
        _save()
        shading = _shading(context)
        if not self.clear and shading is not None and shading.light == 'MATCAP':
            shading.studio_light = self.name
        if self.clear:
            self.report({'INFO'}, "Default matcap cleared")
        else:
            self.report({'INFO'}, f"\"{_label(self.name)}\" is now the default matcap")
        _refresh([], context)
        return {'FINISHED'}


class VIEW3D_OT_phantom_matcap_favorite(Operator):
    """Star this matcap, so the matcap grid can show only your favorites"""
    bl_idname = "view3d.phantom_matcap_favorite"
    bl_label = "Favorite Matcap"
    bl_options = {'INTERNAL'}

    name: StringProperty(options={'SKIP_SAVE'})
    remove: BoolProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        favorites = settings()["favorites"]
        if self.remove:
            if self.name in favorites:
                favorites.remove(self.name)
        elif self.name not in favorites:
            favorites.append(self.name)
        _save()
        _push_tags()
        _refresh([], context)
        return {'FINISHED'}


class VIEW3D_OT_phantom_matcap_quick(Operator):
    bl_idname = "view3d.phantom_matcap_quick"
    bl_label = "Quick Matcap"
    bl_options = {'INTERNAL'}

    slot: IntProperty(options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        name = settings()["quick"][properties.slot]
        if not name:
            return "Put the current matcap in this quick slot"
        return f"Use {_label(name)}.\nRight-click to replace or clear this slot"

    def execute(self, context):
        shading = _shading(context)
        if shading is None:
            return {'CANCELLED'}
        quick = settings()["quick"]
        if not quick[self.slot]:
            if shading.light != 'MATCAP':
                return {'CANCELLED'}
            quick[self.slot] = shading.studio_light
            _save()
        else:
            shading.light = 'MATCAP'
            shading.studio_light = quick[self.slot]
        _refresh([], context)
        return {'FINISHED'}


class VIEW3D_OT_phantom_matcap_quick_assign(Operator):
    """Put this matcap in a quick slot"""
    bl_idname = "view3d.phantom_matcap_quick_assign"
    bl_label = "Set Quick Slot"
    bl_options = {'INTERNAL'}

    slot: IntProperty(options={'SKIP_SAVE'})
    # Empty: the viewport's current matcap. With `clear`, empties the slot.
    name: StringProperty(options={'SKIP_SAVE'})
    clear: BoolProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        name = self.name
        if not name and not self.clear:
            shading = _shading(context)
            if shading is None or shading.light != 'MATCAP':
                return {'CANCELLED'}
            name = shading.studio_light
        settings()["quick"][self.slot] = "" if self.clear else name
        _save()
        _refresh([], context)
        return {'FINISHED'}


# The matcap last right-clicked, for the "Add to Quick Slot" submenu (submenus don't get the
# grid item's context).
_menu_item = [""]


class VIEW3D_MT_phantom_matcap_quick_slots(bpy.types.Menu):
    bl_label = "Add to Quick Slot"

    def draw(self, _context):
        layout = self.layout
        name = _menu_item[0]
        quick = settings()["quick"]
        for slot in range(QUICK_SLOTS):
            current = quick[slot]
            text = f"Slot {slot + 1}" + (f"  ({_short_label(current)})" if current else "")
            op = layout.operator(VIEW3D_OT_phantom_matcap_quick_assign.bl_idname, text=text,
                                 icon='CHECKMARK' if current == name else 'BLANK1')
            op.slot = slot
            op.name = name


def _draw_button_context_menu(self, context):
    layout = self.layout

    # A quick slot: replace or clear it.
    props = getattr(context, "button_operator", None)
    if props is not None and props.rna_type.identifier == "VIEW3D_OT_phantom_matcap_quick":
        layout.separator()
        op = layout.operator(VIEW3D_OT_phantom_matcap_quick_assign.bl_idname,
                             text="Replace with Current Matcap", icon='FILE_REFRESH')
        op.slot = props.slot
        if settings()["quick"][props.slot]:
            op = layout.operator(VIEW3D_OT_phantom_matcap_quick_assign.bl_idname,
                                 text="Clear Quick Slot", icon='X')
            op.slot = props.slot
            op.clear = True
        return

    # A matcap in the thumbnail grid, or the thumbnail itself.
    name = getattr(context, ITEM_CONTEXT, None)
    prop = getattr(context, "button_prop", None)
    pointer = getattr(context, "button_pointer", None)
    if (not name or prop is None or prop.identifier != "studio_light" or
            getattr(pointer, "light", None) != 'MATCAP'):
        return
    data = settings()
    layout.separator()
    if data["default"] == name:
        op = layout.operator(VIEW3D_OT_phantom_matcap_set_default.bl_idname,
                             text="Stop Using as Default Matcap", icon='CHECKBOX_HLT')
        op.clear = True
    else:
        op = layout.operator(VIEW3D_OT_phantom_matcap_set_default.bl_idname,
                             text="Set as Default Matcap", icon='CHECKBOX_DEHLT')
    op.name = name
    favorite = name in data["favorites"]
    op = layout.operator(VIEW3D_OT_phantom_matcap_favorite.bl_idname,
                         text="Remove from Favorites" if favorite else "Add to Favorites",
                         icon='SOLO_ON' if favorite else 'SOLO_OFF')
    op.name = name
    op.remove = favorite
    _menu_item[0] = name
    layout.menu(VIEW3D_MT_phantom_matcap_quick_slots.__name__, icon='PRESET')


def _draw_quick_slots(layout, context, shading):
    names = _matcap_names(context)
    layout.label(text="Quick Matcaps")
    # A box sets the slots apart from the rest of the popover.
    grid = layout.box().grid_flow(row_major=True, columns=QUICK_SLOTS, even_columns=True,
                                  even_rows=True, align=False)
    for slot, name in enumerate(settings()["quick"]):
        col = grid.column(align=True)
        if name and name in names:
            icon = bpy.types.UILayout.enum_item_icon(shading, "studio_light", name)
            props = col.phantom_operator_preview(
                VIEW3D_OT_phantom_matcap_quick.bl_idname, icon_value=icon, scale=3.0,
                depress=(shading.studio_light == name))
            props.slot = slot
            label = col.row()
            label.alignment = 'CENTER'
            label.label(text=_short_label(name))
        else:
            sub = col.column(align=True)
            sub.scale_y = 3.0
            sub.operator(VIEW3D_OT_phantom_matcap_quick.bl_idname, text="", icon='ADD').slot = slot
            label = col.row()
            label.alignment = 'CENTER'
            label.label(text="Empty")
            label.enabled = False


def draw_shading_lighting(self, context):
    shading = _shading(context)
    if shading is None or shading.type != 'SOLID' or shading.light != 'MATCAP':
        return
    layout = self.layout
    _draw_quick_slots(layout, context, shading)
    col = layout.column(align=True)
    col.label(text="Per Object")
    row = col.row(align=True)
    row.operator(OBJECT_OT_phantom_matcap_apply.bl_idname, text="Apply to Selected", icon='MATSHADERBALL')
    row.operator(OBJECT_OT_phantom_matcap_clear.bl_idname, text="Use Viewport", icon='LOOP_BACK')
    active = context.active_object
    if active is not None and PROPERTY in active:
        col.label(text=f"{active.name}: {active[PROPERTY]}", icon='INFO')


# -----------------------------------------------------------------------------
# Default matcap on load, "new!" tracking

def _apply_default_matcap():
    default = settings()["default"]
    if not default or default not in _matcap_names():
        return
    for screen in bpy.data.screens:
        for area in screen.areas:
            for space in area.spaces:
                if space.type != 'VIEW_3D':
                    continue
                shading = space.shading
                if shading.type == 'SOLID' and shading.light == 'MATCAP' and \
                        shading.studio_light in FACTORY_MATCAPS:
                    shading.studio_light = default


@persistent
def _on_load(_):
    _apply_default_matcap()


_watch_state = {"names": None}


def _watch():
    """Notice matcaps added or removed, and clear "new!" from matcaps once used."""
    wm = bpy.context.window_manager
    if wm is None:
        return WATCH_INTERVAL
    names = _matcap_names()
    data = settings()
    if data["seen"] is None:
        # First run: what ships with Phantom isn't new.
        _mark_seen(names)
    changed = names != _watch_state["names"]
    new = _new_matcaps(names)
    if new:
        used = set()
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    shading = area.spaces.active.shading
                    if shading.light == 'MATCAP' and shading.studio_light in new:
                        used.add(shading.studio_light)
        if used:
            _mark_seen(used)
            changed = True
    if changed:
        _watch_state["names"] = names
        _push_tags(names)
        _redraw_all()
    return WATCH_INTERVAL


classes = (
    VIEW3D_MT_phantom_matcap_quick_slots,
    VIEW3D_OT_phantom_matcap_search,
    VIEW3D_OT_phantom_matcap_set_default,
    VIEW3D_OT_phantom_matcap_favorite,
    VIEW3D_OT_phantom_matcap_quick,
    VIEW3D_OT_phantom_matcap_quick_assign,
    OBJECT_OT_phantom_matcap_apply,
    OBJECT_OT_phantom_matcap_clear,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_PT_shading_lighting.append(draw_shading_lighting)
    bpy.types.UI_MT_button_context_menu.append(_draw_button_context_menu)
    bpy.app.handlers.load_post.append(_on_load)
    if not bpy.app.background:
        bpy.app.timers.register(_watch, first_interval=0.5, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
    bpy.app.handlers.load_post.remove(_on_load)
    bpy.types.UI_MT_button_context_menu.remove(_draw_button_context_menu)
    bpy.types.VIEW3D_PT_shading_lighting.remove(draw_shading_lighting)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
