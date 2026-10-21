# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: Sculpt Mode layout.

- The 3D Viewport sidebar (N panel) moves to the left, next to the tool-bar, and shows
  the Tool tab while sculpting. Leaving Sculpt Mode puts it back as it was.
- Quick-brush slot buttons (drawn by ``VIEW3D_PT_tools_brush_quick``).
"""

import bpy
from bpy.props import IntProperty, StringProperty
from bpy.types import Operator

import phantom_quick_brushes as quick_brushes

SYNC_INTERVAL = 0.2

# Areas whose sidebar Phantom moved: area pointer -> (was shown, previous tab).
_moved = {}
# Areas still waiting to switch to the Tool tab (it can only be set once the region drew).
_pending_tool_tab = set()


def _slot_at(index):
    slots = quick_brushes.slots()
    return slots[index] if 0 <= index < len(slots) else None


def _activate(slot):
    return bpy.ops.brush.asset_activate(
        asset_library_type=slot["type"],
        asset_library_identifier=slot["identifier"],
        relative_asset_identifier=slot["relative"],
    )


def _redraw(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def _assign_current(operator, context, index):
    current = quick_brushes.current_reference(context)
    if current is None:
        operator.report({'WARNING'}, "The current brush is not an asset, so it can't be saved")
        return {'CANCELLED'}
    quick_brushes.assign(index, current)
    _redraw(context)
    return {'FINISHED'}


class SCULPT_OT_phantom_quick_brush(Operator):
    bl_idname = "sculpt.phantom_quick_brush"
    bl_label = "Quick Brush"
    bl_options = {'INTERNAL'}

    slot: IntProperty(options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        slot = _slot_at(properties.slot)
        if slot is None:
            return "Save the current brush to this slot"
        return (
            f"Use {quick_brushes.slot_name(slot)}.\n"
            "Right-click to replace, remove or assign a shortcut. "
            "Drag into the viewport to remove"
        )

    def execute(self, context):
        # Also runs from keyboard shortcuts assigned to a slot.
        slot = _slot_at(self.slot)
        if slot is None:
            return _assign_current(self, context, self.slot)
        return _activate(slot)


class SCULPT_OT_phantom_quick_brush_assign(Operator):
    """Put the current brush in this quick-brush slot"""
    bl_idname = "sculpt.phantom_quick_brush_assign"
    bl_label = "Assign Current Brush"
    bl_options = {'INTERNAL'}

    slot: IntProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        return _assign_current(self, context, self.slot)


class SCULPT_OT_phantom_quick_brush_remove(Operator):
    """Remove this brush from the quick brushes"""
    bl_idname = "sculpt.phantom_quick_brush_remove"
    bl_label = "Remove Brush"
    bl_options = {'INTERNAL'}

    slot: IntProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        quick_brushes.remove(self.slot)
        _redraw(context)
        return {'FINISHED'}


class SCULPT_OT_phantom_quick_brush_add(Operator):
    """Add a brush asset to the quick brushes (used when dropping a brush on the sidebar)"""
    bl_idname = "sculpt.phantom_quick_brush_add"
    bl_label = "Add Quick Brush"
    bl_options = {'INTERNAL'}

    asset_library_type: StringProperty(options={'SKIP_SAVE'})
    asset_library_identifier: StringProperty(options={'SKIP_SAVE'})
    relative_asset_identifier: StringProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        slot = {
            "type": self.asset_library_type,
            "identifier": self.asset_library_identifier,
            "relative": self.relative_asset_identifier,
        }
        # Load the brush briefly so its thumbnail can be cached, then go back to the brush
        # that was in use.
        previous = quick_brushes.current_reference(context)
        if _activate(slot) != {'FINISHED'}:
            return {'CANCELLED'}
        quick_brushes.add(slot)
        if previous is not None and previous != slot:
            _activate(previous)
        _redraw(context)
        return {'FINISHED'}


def _draw_button_context_menu(self, context):
    """Right-click menu entries for quick-brush slots."""
    props = getattr(context, "button_operator", None)
    if props is None or props.rna_type.identifier != "SCULPT_OT_phantom_quick_brush":
        return
    layout = self.layout
    layout.separator()
    layout.operator(
        SCULPT_OT_phantom_quick_brush_assign.bl_idname, icon='BRUSH_DATA').slot = props.slot
    if _slot_at(props.slot) is not None:
        layout.operator(
            SCULPT_OT_phantom_quick_brush_remove.bl_idname, icon='X').slot = props.slot


# -----------------------------------------------------------------------------
# Sidebar on the left while sculpting

def _sidebar(area):
    return next((region for region in area.regions if region.type == 'UI'), None)


def _flip(window, area, region):
    with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
        bpy.ops.screen.region_flip()


# The tab can be read-only (no tabs drawn yet) or not offer "Tool" yet; retry next sync.
_TAB_ERRORS = (TypeError, AttributeError)


def _set_tool_tab(region):
    try:
        if region.active_panel_category != 'Tool':
            region.active_panel_category = 'Tool'
    except _TAB_ERRORS:
        return False
    return True


def _sync_sidebars():
    wm = bpy.context.window_manager
    if wm is None:
        return SYNC_INTERVAL

    seen = set()
    for window in wm.windows:
        active = window.view_layer.objects.active
        sculpting = active is not None and active.mode == 'SCULPT'
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            key = area.as_pointer()
            seen.add(key)
            region = _sidebar(area)
            space = area.spaces.active
            if region is None:
                continue

            if sculpting and key not in _moved and region.alignment == 'RIGHT':
                try:
                    previous_tab = region.active_panel_category
                except _TAB_ERRORS:
                    previous_tab = None
                _moved[key] = (space.show_region_ui, previous_tab)
                _flip(window, area, region)
                space.show_region_ui = True
                _pending_tool_tab.add(key)
            elif not sculpting and key in _moved:
                was_shown, previous_tab = _moved.pop(key)
                _pending_tool_tab.discard(key)
                if region.alignment == 'LEFT':
                    _flip(window, area, region)
                space.show_region_ui = was_shown
                if previous_tab and previous_tab != 'UNSUPPORTED':
                    try:
                        if region.active_panel_category != previous_tab:
                            region.active_panel_category = previous_tab
                    except _TAB_ERRORS:
                        pass

            if key in _pending_tool_tab and region.width > 1 and _set_tool_tab(region):
                _pending_tool_tab.discard(key)

    # Forget areas that were closed.
    for key in list(_moved):
        if key not in seen:
            del _moved[key]
            _pending_tool_tab.discard(key)
    return SYNC_INTERVAL


classes = (
    SCULPT_OT_phantom_quick_brush,
    SCULPT_OT_phantom_quick_brush_assign,
    SCULPT_OT_phantom_quick_brush_remove,
    SCULPT_OT_phantom_quick_brush_add,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.UI_MT_button_context_menu.append(_draw_button_context_menu)
    if not bpy.app.background:
        bpy.app.timers.register(_sync_sidebars, first_interval=SYNC_INTERVAL, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_sync_sidebars):
        bpy.app.timers.unregister(_sync_sidebars)
    bpy.types.UI_MT_button_context_menu.remove(_draw_button_context_menu)
    quick_brushes.unregister()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
