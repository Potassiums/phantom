# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: turn on the extensions that ship with Phantom.

They're in Phantom's System extensions repository (``release/extensions/system``, installed into
``<version>/extensions/system``), so they're always installed, but an extension has to be turned
on. Each one in ``BUNDLED`` is turned on the first time Phantom starts with it; after that it's the
user's choice (Edit > Preferences > Add-ons), remembered in ``phantom_extensions.json`` in
Phantom's config folder so it isn't turned back on.

Bundled:
- **Maya Scene IO** (``maya_scene_importer``, GPL-3.0-or-later, by Shibashish Das,
  https://github.com/MenuuTUX/maya-scene-io): File > Import / Export > Maya Scene (.ma, and .mb
  through a local Maya install), and a Maya IO tab in the 3D view's sidebar.
- **Animation Layers** (``anim_layers``, GPL-3.0-or-later, Phantom's, based on the animation layers
  of Maya Workflow Kit for Blender by ohnsk, https://github.com/5hyuns/maya-workflow-kit-for-blender):
  a Layers tab in the Dope Sheet, Timeline and Graph Editor sidebars and in the 3D view's Animation tab.
"""

import json
import os

import addon_utils
import bpy

# Extension IDs (their folder in the System repository).
BUNDLED = (
    "maya_scene_importer",
    "anim_layers",
)
SETTINGS_FILE = "phantom_extensions.json"


def _settings_path():
    return os.path.join(bpy.utils.user_resource('CONFIG'), SETTINGS_FILE)


def _turned_on_before():
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        done = data.get("turned_on", []) if isinstance(data, dict) else []
        return set(done) if isinstance(done, list) else set()
    except (OSError, ValueError):
        return set()


def _remember(turned_on):
    try:
        os.makedirs(os.path.dirname(_settings_path()), exist_ok=True)
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump({"turned_on": sorted(turned_on)}, f, indent=2)
    except OSError as ex:
        print("Phantom: couldn't save the bundled extensions settings:", ex)


def _turn_on_new():
    if bpy.app.factory_startup:
        # Factory settings: on for this session, nothing saved or remembered.
        for ext in BUNDLED:
            module = "bl_ext.system." + ext
            if not addon_utils.check(module)[1]:
                addon_utils.enable(module, default_set=True)
        return None
    done = _turned_on_before()
    changed = False
    for ext in BUNDLED:
        if ext in done:
            continue
        module = "bl_ext.system." + ext
        if not addon_utils.check(module)[1]:
            if addon_utils.enable(module, default_set=True, persistent=True) is None:
                print("Phantom: couldn't turn on the bundled extension", ext)
                continue
        done.add(ext)
        changed = True
    if changed:
        _remember(done)
        prefs = bpy.context.preferences
        # Keep the choice: saved with the preferences on quit, or now when they aren't saved then
        # (nothing else has changed this early).
        if prefs.use_preferences_save:
            prefs.is_dirty = True
        else:
            bpy.ops.wm.save_userpref()
    return None


def register():
    # After the add-ons and the extension repositories are set up.
    bpy.app.timers.register(_turn_on_new, first_interval=0.1)


def unregister():
    if bpy.app.timers.is_registered(_turn_on_new):
        bpy.app.timers.unregister(_turn_on_new)
