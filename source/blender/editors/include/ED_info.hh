/* SPDX-FileCopyrightText: 2009 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup editors
 */

#pragma once

namespace blender {

struct Main;
struct Scene;
struct View3D;
struct ViewLayer;
struct ARegion;
struct rcti;
struct wmWindowManager;

/* `info_stats.cc` */

void ED_info_stats_clear(wmWindowManager *wm, ViewLayer *view_layer);
const char *ED_info_statusbar_string_ex(Main *bmain,
                                        Scene *scene,
                                        ViewLayer *view_layer,
                                        const char statusbar_flag);
const char *ED_info_statusbar_string(Main *bmain, Scene *scene, ViewLayer *view_layer);

const char *ED_info_statistics_string(Main *bmain, Scene *scene, ViewLayer *view_layer);

void ED_view3d_local_stats_free(View3D *v3d);

/**
 * \param v3d_local: Pass this argument to calculate view-port local statistics.
 * Note that this must only be used for local-view, otherwise report specific statistics
 * will be written into the global scene statistics giving incorrect results.
 */
/**
 * Phantom: the session's and the project's work time, drawn under the statistics (seconds,
 * negative hides a row).
 */
void ED_info_phantom_work_time_set(int session_seconds, int project_seconds, bool show_seconds);

/**
 * Phantom: when the file was last saved, under the statistics (empty: not shown), with a save
 * reminder icon after it when `warning`.
 */
void ED_info_phantom_saved_set(const char *text, bool warning);
/** Phantom: the 3D View region the statistics are drawn in next (for the reminder's rect). */
void ED_info_phantom_stats_region_set(const ARegion *region);
/** Phantom: where the save reminder icon was drawn in `region` (region pixels). */
bool ED_info_phantom_save_warning_rect(const ARegion *region, rcti *r_rect);

void ED_info_draw_stats(Main *bmain,
                        Scene *scene,
                        ViewLayer *view_layer,
                        View3D *v3d_local,
                        int x,
                        int *y,
                        int height);

}  // namespace blender
