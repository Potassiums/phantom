/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * Phantom: a small interface to AutoRemesher that keeps its headers out of Blender code.
 */

#pragma once

#include <array>
#include <functional>
#include <vector>

namespace autoremesher {

struct Params {
  /** Number of quads to aim for (0 lets AutoRemesher choose from the mesh size). */
  int target_quads = 5000;
  /** Larger values make bigger quads, 1..4. */
  float edge_scaling = 1.0f;
  /** Finer quads in curved areas, 0..1. */
  float adaptivity = 1.0f;
  /** Quads stretched along flat directions, 0..1. */
  float anisotropy = 1.0f;
  /** Edges sharper than this dihedral angle are kept as features. */
  float sharp_edge_degrees = 90.0f;
  /** Follow the input's smooth normals up to this angle (0 = faceted). */
  float smooth_normal_degrees = 0.0f;
  bool hard_surface = false;
};

struct Result {
  std::vector<std::array<float, 3>> positions;
  /** Vertex count of each face (mostly 4). */
  std::vector<int> face_sizes;
  std::vector<int> corner_verts;
};

/**
 * Progress callback: fraction 0..1 and the current step name. Called from worker threads.
 * Return false to cancel.
 */
using ProgressFn = std::function<bool(float progress, const char *status)>;

enum class Status { Success, Failed, Canceled };

Status remesh(const std::vector<std::array<float, 3>> &positions,
              const std::vector<std::array<int, 3>> &triangles,
              const Params &params,
              const ProgressFn &progress,
              Result &r_result);

}  // namespace autoremesher
