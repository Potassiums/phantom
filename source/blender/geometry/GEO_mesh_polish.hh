/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

/** \file
 * \ingroup geo
 *
 * Phantom: polish and sharpen, a whole-mesh cleanup that smooths away noise while keeping (and
 * restoring) creases, then compensates the volume. Typically run right after a remesh.
 */

#include "BLI_math_vector_types.hh"
#include "BLI_span.hh"

namespace blender {
struct Mesh;
}

namespace blender::geometry {

struct PolishSettings {
  /** Protect edges by their dihedral angle; when off, polish smooths everywhere. */
  bool angle_preservation = true;
  /**
   * Where the protection ramp ends, as a fraction of the sharpest edge's angle removed from the
   * top: 0 ramps over the whole range, higher protects gentler edges too.
   */
  float angle_ratio = 0.5f;
  /** Strength of the sharpening (unsharp mask) on protected edges. */
  float sharpness = 0.4f;
  /** Widens the sharpening kernel from one ring of neighbors (0) to three (1). */
  float softness = 0.3f;
  /** Sharpen passes after each polish pass. */
  int repeat_sharpen = 1;
  int repeat_polish = 2;
  /** Displacement along the normal after the passes, on convex and concave areas (0-1). */
  float inflate_outer = 0.0f;
  float inflate_inner = 0.0f;
};

/**
 * Polishes `positions` (the mesh's own or deformed ones, matching its topology) into
 * `r_positions`. `influence` is 0-1 per vertex: 0 pins a vertex exactly (masked, hidden), and it
 * scales every displacement otherwise. Boundary and non-manifold vertices are pinned. The result
 * doesn't depend on the number of threads.
 */
void mesh_polish(const Mesh &mesh,
                 Span<float3> positions,
                 Span<float> influence,
                 const PolishSettings &settings,
                 MutableSpan<float3> r_positions);

}  // namespace blender::geometry
