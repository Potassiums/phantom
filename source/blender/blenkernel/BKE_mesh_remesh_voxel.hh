/* SPDX-FileCopyrightText: 2019 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup bke
 */

#pragma once

namespace blender {

struct Mesh;
struct ModifierData;
struct Object;
struct ReportList;

Mesh *BKE_mesh_remesh_voxel_fix_poles(const Mesh *mesh);
Mesh *BKE_mesh_remesh_voxel(const Mesh *mesh,
                            float voxel_size,
                            float adaptivity,
                            float isovalue,
                            const Object *object,
                            ModifierData *modifier_data);
Mesh *BKE_mesh_remesh_voxel(
    const Mesh *mesh, float voxel_size, float adaptivity, float isovalue, ReportList *reports);
Mesh *BKE_mesh_remesh_quadriflow(const Mesh *mesh,
                                 int target_faces,
                                 int seed,
                                 bool preserve_sharp,
                                 bool preserve_boundary,
                                 bool adaptive_scale,
                                 void (*update_cb)(void *, float progress, int *cancel),
                                 void *update_cb_data);

/** Phantom: settings for #BKE_mesh_remesh_autoremesher. */
struct AutoRemeshSettings {
  /** Number of quads to aim for. */
  int target_quads = 5000;
  float edge_scaling = 1.0f;
  float adaptivity = 1.0f;
  float anisotropy = 1.0f;
  float sharp_edge_degrees = 90.0f;
  float smooth_normal_degrees = 0.0f;
  bool hard_surface = false;
};

/**
 * Phantom: quad remesh with AutoRemesher. Returns null when it fails or is canceled
 * (`update_cb` returning false cancels, and sets `r_canceled`).
 */
Mesh *BKE_mesh_remesh_autoremesher(const Mesh *mesh,
                                   const AutoRemeshSettings &settings,
                                   bool (*update_cb)(void *, float progress, const char *status),
                                   void *update_cb_data,
                                   bool *r_canceled);

namespace bke {
void mesh_remesh_reproject_attributes(const Mesh &src, Mesh &dst);
}

}  // namespace blender
