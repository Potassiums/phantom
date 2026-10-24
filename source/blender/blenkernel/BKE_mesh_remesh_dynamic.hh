/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup bke
 *
 * Phantom: the dynamic remesher, an alternative to the OpenVDB voxel remesher. It builds a
 * narrow-band signed distance field on a uniform grid, closes small holes, extracts a
 * quad-dominant surface with dual contouring and projects it back onto the source for detail.
 */

#pragma once

namespace blender {

struct Mesh;
struct ReportList;

/** Settings for #BKE_mesh_remesh_dynamic. Distances are in object space. */
struct DynamicRemeshSettings {
  /** Grid cell size, which is the target edge length of the new mesh. */
  float target_edge_length = 0.1f;
  /** Width of the narrow band of exact distances, in cells. */
  int band_cells = 3;
  /** Close holes and gaps narrower than `2 * close_radius_cells` cells. */
  bool close_holes = true;
  int close_radius_cells = 1;
  /** Project the new vertices onto the source surface to recover detail below the cell size. */
  bool project = true;
  /** Normal difference (0-2) where projection starts fading out, to avoid crossing creases. */
  float crease_threshold = 0.5f;
  /** The grid is never larger than this along any axis (the cell size grows instead). */
  int max_grid = 512;
};

/** Measurements of a dynamic remesh, for reports and logs. */
struct DynamicRemeshStats {
  /** Cell size actually used (larger than requested when the grid was capped). */
  float cell_size = 0.0f;
  int grid_dims[3] = {0, 0, 0};
  /** Point-to-surface distance of the new vertices to the source, sampled. */
  float error_mean = 0.0f;
  float error_p95 = 0.0f;
  /** Stage timings in seconds: validate, field, closing/sign, extract, project. */
  double time_validate = 0.0;
  double time_field = 0.0;
  double time_sign = 0.0;
  double time_extract = 0.0;
  double time_project = 0.0;
};

/**
 * Remesh to a uniform density. Hidden faces are left out. Returns null (with an error in
 * `reports`) when there is nothing to remesh or the result would be empty.
 */
Mesh *BKE_mesh_remesh_dynamic(const Mesh *mesh,
                              const DynamicRemeshSettings &settings,
                              ReportList *reports,
                              DynamicRemeshStats *r_stats = nullptr);

}  // namespace blender
