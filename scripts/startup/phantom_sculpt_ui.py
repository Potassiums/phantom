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
# Retry missing quick-brush icons for this many sync intervals (about 10 seconds).
ICON_RETRY_LIMIT = 50
_icon_retries = 0
_addon_keymaps = []

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


def _redraw_sidebars(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'UI':
                        region.tag_redraw()


class SCULPT_OT_phantom_key_watch(Operator):
    """Track Shift/Ctrl so the Brush Asset panel can show the brush or tool they switch to"""
    bl_idname = "sculpt.phantom_key_watch"
    bl_label = "Watch Modifier Keys"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'SCULPT'

    def invoke(self, context, event):
        held = quick_brushes.held
        if held["shift"] != event.shift or held["ctrl"] != event.ctrl:
            held["shift"] = event.shift
            held["ctrl"] = event.ctrl
            _redraw_sidebars(context)
        # Never consume the event.
        return {'PASS_THROUGH'}


class SCULPT_OT_phantom_trim(Operator):
    """Trim with the trim tool selected in the tool-bar (box, lasso, line or polyline)"""
    bl_idname = "sculpt.phantom_trim"
    bl_label = "Trim"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'SCULPT'

    def invoke(self, context, event):
        tool = quick_brushes.selected_trim_tool(context)
        op_id = quick_brushes.TRIM_OPERATORS[tool.idname if tool else "builtin.box_trim"]
        category, name = op_id.split(".")
        operator = getattr(getattr(bpy.ops, category), name)
        op_props = operator.get_rna_type().properties

        # Use the settings of that trim tool (mode, shape, solver...).
        kwargs = {}
        tool_ref = context.workspace.tools.from_space_view3d_mode('SCULPT', create=False)
        if tool_ref is not None:
            props = tool_ref.operator_properties(op_id)
            for prop in props.bl_rna.properties:
                if prop.is_hidden or prop.is_readonly or prop.type in {'COLLECTION', 'POINTER'}:
                    continue
                value = getattr(props, prop.identifier)
                kwargs[prop.identifier] = tuple(value) if getattr(prop, "is_array", False) else value
        # Start the gesture right away from this press.
        if "wait_for_input" in op_props:
            kwargs["wait_for_input"] = False

        result = operator('INVOKE_DEFAULT', **kwargs)
        return {'FINISHED'} if result & {'RUNNING_MODAL', 'FINISHED'} else {'CANCELLED'}


class SCULPT_OT_phantom_polyframe(Operator):
    """Show or hide face sets (polygroups) together with the wireframe, like ZBrush's PolyFrame"""
    bl_idname = "sculpt.phantom_polyframe"
    bl_label = "PolyFrame"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'SCULPT' and context.space_data and context.space_data.type == 'VIEW_3D'

    def execute(self, context):
        overlay = context.space_data.overlay
        show = not (overlay.show_sculpt_face_sets and overlay.show_wireframes)
        overlay.show_sculpt_face_sets = show
        overlay.show_wireframes = show
        if show:
            # Every edge, like ZBrush's polyframe.
            overlay.wireframe_threshold = 1.0
        return {'FINISHED'}


def _draw_header_asset_shelf_toggle(self, context):
    if context.mode != 'SCULPT':
        return
    self.layout.prop(context.space_data, "show_region_asset_shelf", text="", icon='ASSET_MANAGER')


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

    # Quick-brush icons wait for the asset libraries to load: retry for a while.
    global _icon_retries
    if quick_brushes.needs_redraw and _icon_retries < ICON_RETRY_LIMIT:
        quick_brushes.needs_redraw = False
        _icon_retries += 1
        _redraw_sidebars(bpy.context)
    elif not quick_brushes.needs_redraw:
        _icon_retries = 0
    return SYNC_INTERVAL


class SCULPT_OT_phantom_mask_to_face_set(Operator):
    """Turn the masked area into a new face set and clear the mask"""
    bl_idname = "sculpt.phantom_mask_to_face_set"
    bl_label = "Mask to Face Set"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'SCULPT'

    def execute(self, context):
        # Both steps make their own sculpt undo step.
        if bpy.ops.sculpt.face_sets_create(mode='MASKED') != {'FINISHED'}:
            return {'CANCELLED'}
        bpy.ops.paint.mask_flood_fill(mode='VALUE', value=0.0)
        # The new polygroup has the highest number: the polygroup bar works on it.
        import numpy as np
        import phantom_mask_bar
        ob = context.active_object
        face_sets = ob.data.attributes.get(".sculpt_face_set")
        if face_sets is not None and len(face_sets.data):
            values = np.empty(len(face_sets.data), dtype=np.int32)
            face_sets.data.foreach_get("value", values)
            phantom_mask_bar.set_current_polygroup(ob, int(values.max()))
        return {'FINISHED'}


class SCULPT_OT_phantom_divide(Operator):
    """Subdivide the mesh one level, like ZBrush's Divide. Adds a Multiresolution modifier """         """first when the object has none"""
    bl_idname = "sculpt.phantom_divide"
    bl_label = "Divide"
    bl_options = {'REGISTER', 'UNDO'}

    mode: bpy.props.EnumProperty(
        name="Mode",
        items=(
            ('CATMULL_CLARK', "Smooth", "Smooth the shape while subdividing"),
            ('SIMPLE', "Simple", "Subdivide without smoothing the shape"),
            ('LINEAR', "Linear", "Subdivide without smoothing the shape or the UVs"),
        ),
        default='CATMULL_CLARK',
    )

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return context.mode == 'SCULPT' and ob is not None and ob.type == 'MESH'

    def execute(self, context):
        ob = context.active_object
        if ob.use_dynamic_topology_sculpting:
            self.report({'WARNING'}, "Turn off Dyntopo to divide")
            return {'CANCELLED'}
        multires = next((md for md in ob.modifiers if md.type == 'MULTIRES'), None)
        if multires is None:
            if bpy.ops.object.modifier_add(type='MULTIRES') != {'FINISHED'}:
                return {'CANCELLED'}
            multires = ob.modifiers[-1]
            # Multires works on the original mesh, so it goes first in the stack.
            if len(ob.modifiers) > 1:
                bpy.ops.object.modifier_move_to_index(modifier=multires.name, index=0)
        if bpy.ops.object.multires_subdivide(modifier=multires.name, mode=self.mode) != {'FINISHED'}:
            return {'CANCELLED'}
        # Sculpt on the new level, like ZBrush.
        multires.sculpt_levels = multires.total_levels
        multires.levels = max(multires.levels, multires.total_levels)
        self.report({'INFO'}, f"Divided to level {multires.total_levels}")
        return {'FINISHED'}


def _draw_sculpt_menu_divide(self, context):
    layout = self.layout
    layout.separator()
    layout.operator(SCULPT_OT_phantom_divide.bl_idname, text="Divide (Multires)")


# Brushes that paint color: with one of them active, C samples the mesh's color.
COLOR_BRUSH_TYPES = {'PAINT', 'SMEAR'}


class SCULPT_OT_phantom_sample_color(Operator):
    """Pick the mesh's own color under the cursor for the paint brush"""
    bl_idname = "sculpt.phantom_sample_color"
    bl_label = "Sample Mesh Color"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        # Failing the poll lets C fall through to its other shortcuts (brushes).
        if context.mode != 'SCULPT':
            return False
        brush = context.tool_settings.sculpt.brush
        return brush is not None and brush.sculpt_brush_type in COLOR_BRUSH_TYPES

    def invoke(self, context, event):
        # "merged=False" reads the raw color attribute, not what the viewport shows.
        result = bpy.ops.paint.sample_color('INVOKE_DEFAULT', merged=False)
        return {'FINISHED'} if result & {'RUNNING_MODAL', 'FINISHED'} else {'CANCELLED'}


# -----------------------------------------------------------------------------
# Quick-brush shortcuts take over their key

QUICK_BRUSH_OPERATOR = "sculpt.phantom_quick_brush"
# Never switched off for a quick-brush key: these only act in a narrow case and pass the key on.
SHORTCUT_KEEP = {QUICK_BRUSH_OPERATOR, "sculpt.phantom_sample_color", "sculpt.phantom_key_watch"}
SHORTCUT_SYNC_INTERVAL = 1.0


def _shortcut_key(kmi):
    return (kmi.type, kmi.value, kmi.any, kmi.shift, kmi.ctrl, kmi.alt, kmi.oskey, kmi.key_modifier)


def _shortcut_id(kmi):
    """Identify a keymap item across sessions: operator, key and its settings."""
    props = []
    if kmi.properties is not None:
        for prop in kmi.properties.bl_rna.properties:
            if prop.identifier != "rna_type" and kmi.properties.is_property_set(prop.identifier):
                props.append(f"{prop.identifier}={getattr(kmi.properties, prop.identifier)!r}")
    return "|".join((kmi.idname, repr(_shortcut_key(kmi)), *sorted(props)))


def _sync_shortcuts():
    """Switch off Sculpt shortcuts that a quick-brush shortcut uses (C -> Clay Strips, ...),
    and switch them back on once no quick brush uses that key any more."""
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.user if wm else None
    km = kc.keymaps.get("Sculpt") if kc else None
    if km is None:
        return SHORTCUT_SYNC_INTERVAL

    # With the keymap option off (Preferences > Keymap), nothing is taken over and anything
    # switched off before comes back.
    import phantom_keymap
    taken = {
        _shortcut_key(kmi) for kmi in km.keymap_items
        if kmi.idname == QUICK_BRUSH_OPERATOR and kmi.active
    } if phantom_keymap.quick_brush_takeover(bpy.context) else set()
    disabled = quick_brushes.disabled_shortcuts()
    changed = False
    for kmi in km.keymap_items:
        if kmi.idname in SHORTCUT_KEEP:
            continue
        key = _shortcut_key(kmi)
        if kmi.active and key in taken:
            disabled.add(_shortcut_id(kmi))
            kmi.active = False
            changed = True
        elif not kmi.active and key not in taken and disabled:
            ident = _shortcut_id(kmi)
            if ident in disabled:
                disabled.discard(ident)
                kmi.active = True
                changed = True
    if changed:
        quick_brushes.save_disabled_shortcuts(disabled)
    return SHORTCUT_SYNC_INTERVAL


classes = (
    SCULPT_OT_phantom_quick_brush,
    SCULPT_OT_phantom_quick_brush_assign,
    SCULPT_OT_phantom_quick_brush_remove,
    SCULPT_OT_phantom_quick_brush_add,
    SCULPT_OT_phantom_key_watch,
    SCULPT_OT_phantom_trim,
    SCULPT_OT_phantom_polyframe,
    SCULPT_OT_phantom_mask_to_face_set,
    SCULPT_OT_phantom_sample_color,
    SCULPT_OT_phantom_divide,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.UI_MT_button_context_menu.append(_draw_button_context_menu)
    bpy.types.VIEW3D_MT_sculpt.append(_draw_sculpt_menu_divide)
    bpy.types.VIEW3D_HT_header.append(_draw_header_asset_shelf_toggle)
    if not bpy.app.background:
        bpy.app.timers.register(_sync_sidebars, first_interval=SYNC_INTERVAL, persistent=True)
        bpy.app.timers.register(_sync_shortcuts, first_interval=SHORTCUT_SYNC_INTERVAL, persistent=True)

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Sculpt", space_type='EMPTY')
        for key in ('LEFT_SHIFT', 'RIGHT_SHIFT', 'LEFT_CTRL', 'RIGHT_CTRL', 'MOUSEMOVE'):
            kmi = km.keymap_items.new(SCULPT_OT_phantom_key_watch.bl_idname, key, 'ANY', any=True)
            _addon_keymaps.append((km, kmi))


def unregister():
    for km, kmi in _addon_keymaps:
        km.keymap_items.remove(kmi)
    _addon_keymaps.clear()
    for timer in (_sync_sidebars, _sync_shortcuts):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    bpy.types.VIEW3D_HT_header.remove(_draw_header_asset_shelf_toggle)
    bpy.types.VIEW3D_MT_sculpt.remove(_draw_sculpt_menu_divide)
    bpy.types.UI_MT_button_context_menu.remove(_draw_button_context_menu)
    quick_brushes.unregister()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
