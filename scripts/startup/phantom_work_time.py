# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: how long you have worked, under the viewport statistics (an option of Statistics):

- Session: since the file was opened (or started) in this run of Phantom.
- Project: all the time spent on the file, saved in it (a custom property of its scenes).
- Saved: how long ago the file was last saved. With unsaved changes and no save for 30 minutes
  or an hour (the reminder), a warning icon follows it: its tooltip reminds you to save, and
  clicking it saves (an invisible gizmo over the icon, for the tooltip).

Only active time counts: after five minutes without any input (mouse, keys, tablet) the clocks
stop until you are back, so a file left open overnight doesn't count the night.
"""

import os
import time

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty
from bpy.types import Gizmo, GizmoGroup, Operator

# Saved on every (local) scene of the file, the same value: whichever scene stays keeps it.
PROJECT_PROP = "phantom_project_time"
# Seconds without input before the clocks stop.
IDLE_LIMIT = 300.0
# Every few seconds, or every second while the seconds show.
TICK = 5.0
TICK_SECONDS = 1.0
# A longer gap between ticks (a computer that slept) is not counted.
MAX_STEP = 60.0

_state = {"session": 0.0, "project": 0.0, "last": None, "shown": None,
          # Wall-clock times: the last save (None: never), and when the file was opened.
          "saved_at": None, "opened_at": time.time()}

# The save reminder: after how long without saving (seconds) the warning shows.
REMIND_AFTER = {'MIN30': 30 * 60, 'HOUR': 60 * 60}


def _settings():
    import phantom_simple_view
    return phantom_simple_view._settings


def _get_shown(_self):
    return bool(_settings().get("work_time", False))


def _set_shown(_self, value):
    import phantom_simple_view
    phantom_simple_view._set_setting("work_time", bool(value))
    _publish(force=True)


def _get_seconds(_self):
    return bool(_settings().get("work_time_seconds", False))


def _set_seconds(_self, value):
    import phantom_simple_view
    phantom_simple_view._set_setting("work_time_seconds", bool(value))
    _publish(force=True)


def _get_saved(_self):
    return bool(_settings().get("save_time", False))


def _set_saved(_self, value):
    import phantom_simple_view
    phantom_simple_view._set_setting("save_time", bool(value))
    _publish(force=True)


_REMIND_ITEMS = (
    ('OFF', "Off", "No reminder"),
    ('MIN30', "30 Minutes", "Remind after 30 minutes without saving"),
    ('HOUR', "1 Hour", "Remind after an hour without saving"),
)


def _get_remind(_self):
    value = _settings().get("save_remind", 'MIN30')
    return next((i for i, item in enumerate(_REMIND_ITEMS) if item[0] == value), 1)


def _set_remind(_self, value):
    import phantom_simple_view
    phantom_simple_view._set_setting("save_remind", _REMIND_ITEMS[value][0])
    _publish(force=True)


def _tick_interval():
    s = _settings()
    shown = s.get("work_time", False) or s.get("save_time", False)
    return TICK_SECONDS if shown and s.get("work_time_seconds", False) else TICK


def _ago(seconds, with_seconds):
    """'just now', '12m ago', '1h 05m ago', '3d ago' (with the seconds: '45s ago'...)."""
    seconds = int(max(seconds, 0))
    minutes, hours, days = seconds // 60, seconds // 3600, seconds // 86400
    if days >= 1:
        return f"{days}d ago"
    if with_seconds:
        if hours:
            return f"{hours}h {minutes % 60:02d}m {seconds % 60:02d}s ago"
        if minutes:
            return f"{minutes}m {seconds % 60:02d}s ago"
        return f"{seconds}s ago"
    if hours:
        return f"{hours}h {minutes % 60:02d}m ago"
    if minutes:
        return f"{minutes}m ago"
    return "just now"


def save_warning():
    """Whether the save reminder shows: unsaved changes, and no save for longer than the
    reminder's time (counted from the last save, or from opening the file if later)."""
    s = _settings()
    after = REMIND_AFTER.get(s.get("save_remind", 'MIN30'))
    if not s.get("save_time", False) or after is None or not bpy.data.is_dirty:
        return False
    since = max(_state["saved_at"] or 0.0, _state["opened_at"])
    return time.time() - since > after


def _saved_text(with_seconds):
    if _state["saved_at"] is None:
        return "not yet"
    return _ago(time.time() - _state["saved_at"], with_seconds)


def _scenes():
    return [scene for scene in bpy.data.scenes if scene.library is None]


def _project_saved():
    return max((float(scene.get(PROJECT_PROP, 0.0)) for scene in _scenes()), default=0.0)


def _publish(force=False):
    """Hand the times to the statistics drawing; redraw the 3D Views when what they show
    changes (minutes, or seconds)."""
    wm = bpy.context.window_manager
    if wm is None or not hasattr(wm, "phantom_work_time_set"):
        return
    if _settings().get("work_time", False):
        shown = (int(_state["session"]), int(_state["project"]))
    else:
        shown = (-1, -1)
    seconds = bool(_settings().get("work_time_seconds", False))
    wm.phantom_work_time_set(session=shown[0], project=shown[1], seconds=seconds)
    saved, warning = "", False
    if _settings().get("save_time", False) and hasattr(wm, "phantom_saved_set"):
        saved, warning = _saved_text(seconds), save_warning()
    if hasattr(wm, "phantom_saved_set"):
        wm.phantom_saved_set(text=saved, warning=warning)
    if warning and not _state.get("warned", False):
        # A soft chime as the reminder shows (once, until you save).
        try:
            bpy.types.WindowManager.phantom_ui_sound_play("save_reminder")
        except (AttributeError, TypeError, RuntimeError):
            pass
    _state["warned"] = warning
    unit = 1 if seconds else 60
    what = (seconds, saved, warning) + tuple(v // unit if v >= 0 else -1 for v in shown)
    if force or what != _state["shown"]:
        _state["shown"] = what
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()


def _tick():
    now = time.monotonic()
    last = _state["last"]
    _state["last"] = now
    wm = bpy.context.window_manager
    if last is None or wm is None:
        return _tick_interval()
    step = now - last
    idle = wm.phantom_idle_seconds() if hasattr(wm, "phantom_idle_seconds") else 0.0
    if idle < IDLE_LIMIT and 0.0 < step < MAX_STEP:
        _state["session"] += step
        # The larger of what is saved and what we counted: an undo can bring back an older value.
        _state["project"] = max(_state["project"], _project_saved()) + step
        for scene in _scenes():
            scene[PROJECT_PROP] = _state["project"]
    _publish()
    return _tick_interval()


@persistent
def _on_load(_filepath):
    # A new session for this file; its project time from the file.
    _state["session"] = 0.0
    _state["project"] = _project_saved()
    _state["last"] = time.monotonic()
    # Saved when the file was written last (none for a new file).
    path = bpy.data.filepath
    try:
        _state["saved_at"] = os.path.getmtime(path) if path else None
    except OSError:
        _state["saved_at"] = None
    _state["opened_at"] = time.time()
    _publish(force=True)


@persistent
def _on_save(_filepath):
    _state["saved_at"] = time.time()
    _publish(force=True)


class WM_OT_phantom_save_reminder(Operator):
    """Remember to save: click to save now (Ctrl S)"""
    bl_idname = "wm.phantom_save_reminder"
    bl_label = "Remember to Save"
    bl_options = {'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        if _state["saved_at"] is None:
            return "Remember to save: this file hasn't been saved yet. Click to save it now (Ctrl S)"
        return ("Remember to save: your last save was " + _ago(time.time() - _state["saved_at"], False) +
                ". Click to save now (Ctrl S)")

    def execute(self, context):
        bpy.ops.wm.save_mainfile('INVOKE_DEFAULT')
        return {'FINISHED'}


class VIEW3D_GT_phantom_save_reminder(Gizmo):
    """Over the save reminder icon under the statistics: its tooltip and click (draws nothing,
    the icon is drawn with the statistics)."""
    bl_idname = "VIEW3D_GT_phantom_save_reminder"

    def draw(self, context):
        pass

    def test_select(self, context, location):
        wm = context.window_manager
        if not hasattr(wm, "phantom_save_warning_rect") or context.region is None:
            return -1
        x0, y0, x1, y1 = wm.phantom_save_warning_rect(context.region)
        x, y = location
        return 0 if x1 > x0 and x0 <= x <= x1 and y0 <= y <= y1 else -1


class VIEW3D_GGT_phantom_save_reminder(GizmoGroup):
    bl_idname = "VIEW3D_GGT_phantom_save_reminder"
    bl_label = "Save Reminder"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE'}

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return space is not None and space.overlay.show_stats and save_warning()

    def setup(self, context):
        gz = self.gizmos.new(VIEW3D_GT_phantom_save_reminder.bl_idname)
        gz.target_set_operator(WM_OT_phantom_save_reminder.bl_idname)
        gz.use_draw_hover = False


_classes = (WM_OT_phantom_save_reminder, VIEW3D_GT_phantom_save_reminder,
            VIEW3D_GGT_phantom_save_reminder)


def register():
    bpy.types.WindowManager.phantom_show_work_time = BoolProperty(
        name="Session and Project Time",
        description="Under the statistics: how long you have worked on this file in this session, "
        "and in all (saved with the file). Only active time counts",
        get=_get_shown, set=_set_shown,
    )
    bpy.types.WindowManager.phantom_show_work_time_seconds = BoolProperty(
        name="Show Seconds",
        description="Show the seconds of the session and project time too",
        get=_get_seconds, set=_set_seconds,
    )
    bpy.types.WindowManager.phantom_show_save_time = BoolProperty(
        name="Last Saved",
        description="Under the statistics: how long ago the file was last saved",
        get=_get_saved, set=_set_saved,
    )
    bpy.types.WindowManager.phantom_save_remind = EnumProperty(
        name="Save Reminder",
        description="A warning icon beside Last Saved when there are unsaved changes and no save "
        "for this long",
        items=_REMIND_ITEMS, get=_get_remind, set=_set_remind,
    )
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.app.handlers.load_post.append(_on_load)
    bpy.app.handlers.save_post.append(_on_save)
    _state["last"] = time.monotonic()
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=TICK, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    if _on_save in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(_on_save)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.phantom_show_save_time
    del bpy.types.WindowManager.phantom_save_remind
    del bpy.types.WindowManager.phantom_show_work_time
    del bpy.types.WindowManager.phantom_show_work_time_seconds
