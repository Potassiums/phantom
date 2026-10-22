/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup wm
 *
 * Phantom: interface sound effects (clicks, toggles, confirm, cancel, typing...).
 * Sounds are WAV files in `datafiles/sounds/`, played through Blender's audio device.
 */

#include "DNA_userdef_types.h"

#include "BLI_path_utils.hh"
#include "BLI_rand.hh"
#include "BLI_time.hh"

#include "BKE_appdir.hh"
#include "BKE_global.hh"
#include "BKE_sound.hh"

#include "WM_api.hh"

namespace blender {

static const char *ui_sound_filename(const WMUISound sound)
{
  switch (sound) {
    case WMUISound::Click:
      return "click.wav";
    case WMUISound::ToggleOn:
      return "toggle_on.wav";
    case WMUISound::ToggleOff:
      return "toggle_off.wav";
    case WMUISound::MenuOpen:
      return "menu_open.wav";
    case WMUISound::Confirm:
      return "confirm.wav";
    case WMUISound::Cancel:
      return "cancel.wav";
    case WMUISound::Error:
      return "error.wav";
    case WMUISound::GrabStart:
      return "grab_start.wav";
    case WMUISound::TransformConfirm:
      return "transform_confirm.wav";
    case WMUISound::TransformCancel:
      return "transform_cancel.wav";
    case WMUISound::Type:
      return "type.wav";
    case WMUISound::TypeDelete:
      return "type_delete.wav";
    case WMUISound::TypeEnter:
      return "type_enter.wav";
    case WMUISound::OutlinerSelect:
      return "outliner_select.wav";
    case WMUISound::LoopCut:
      return "loop_cut.wav";
    case WMUISound::SlideTick:
      return "slide_tick.wav";
    case WMUISound::KnifeCut:
      return "knife_cut.wav";
    case WMUISound::Undo:
      return "undo.wav";
    case WMUISound::Redo:
      return "redo.wav";
    case WMUISound::ModeSwitch:
      return "mode_switch.wav";
    case WMUISound::MeasureTick:
      return "measure_tick.wav";
    case WMUISound::LoopAdd:
      return "loop_add.wav";
    case WMUISound::LoopRemove:
      return "loop_remove.wav";
  }
  return nullptr;
}

/**
 * Ignore a repeat of the same sound within this many seconds, so a burst of events
 * (e.g. dragging across several toggles) doesn't stack into a buzz.
 * Typing and slide ticks are allowed to repeat faster.
 */
static double ui_sound_min_interval(const WMUISound sound)
{
  switch (sound) {
    case WMUISound::Type:
    case WMUISound::TypeDelete:
      return 0.015;
    case WMUISound::SlideTick:
    case WMUISound::MeasureTick:
    case WMUISound::LoopAdd:
    case WMUISound::LoopRemove:
      return 0.03;
    default:
      return 0.04;
  }
}

void WM_ui_sound_play(const WMUISound sound, const float pitch)
{
  if (G.background || !U.ui_sounds || U.ui_sound_volume <= 0) {
    return;
  }

  static double last_play_time[WM_UI_SOUND_COUNT] = {};
  const double now = BLI_time_now_seconds();
  double &last = last_play_time[int(sound)];
  if (now - last < ui_sound_min_interval(sound)) {
    return;
  }
  last = now;

  const std::optional<std::string> dir = BKE_appdir_folder_id(BLENDER_DATAFILES, "sounds");
  if (!dir) {
    return;
  }
  char filepath[FILE_MAX];
  BLI_path_join(filepath, sizeof(filepath), dir->c_str(), ui_sound_filename(sound));
  BKE_sound_play_ui(filepath, float(U.ui_sound_volume) / 100.0f, pitch);
}

void WM_ui_sound_play_varied(const WMUISound sound, const float spread)
{
  static RandomNumberGenerator rng = RandomNumberGenerator::from_random_seed();
  WM_ui_sound_play(sound, 1.0f + spread * (2.0f * rng.get_float() - 1.0f));
}

}  // namespace blender
