/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup geo
 *
 * Phantom: polish and sharpen.
 *
 * - Feature weights: an edge's protection ramps with its dihedral angle; a vertex takes the most
 *   protected of its edges, so a crease line is kept rather than smeared.
 * - Polish: Laplacian smoothing scaled by how unprotected the vertex is: tangential relaxation
 *   (the surface slides instead of shrinking) plus Taubin lambda|mu smoothing across it (removes
 *   noise without shrinking).
 * - Sharpen: unsharp masking along the normal against a 1-3 ring average, on protected
 *   vertices: it restores the detail the last polish pass removed, clamped per pass to stop
 *   spikes. Passes interleave: P S P S...
 * - Inflate: a final normal displacement, split by the sign of the local curvature.
 *
 * Every pass reads one buffer and writes another, and the normals are those of the input (kept
 * on purpose: cheaper, and the weighting stays stable), so the result is independent of
 * threading.
 */

#include <algorithm>

#include "BLI_array.hh"
#include "BLI_math_base.hh"
#include "BLI_math_vector.hh"
#include "BLI_math_vector_c.hh"
#include "BLI_task.hh"

#include "BKE_mesh.hh"
#include "BKE_mesh_mapping.hh"

#include "GEO_mesh_polish.hh"

namespace blender::geometry {

/** Relaxation factor of the polish pass; above about 0.8 the iteration oscillates. */
static constexpr float POLISH_LAMBDA = 0.5f;
/** Taubin's inflating factor, slightly larger than lambda (pass band around 0.1). */
static constexpr float TAUBIN_MU = -0.53f;
/** Taubin lambda|mu step pairs in one polish pass. */
static constexpr int TAUBIN_STEPS = 3;
/** Inflate displacement at 1.0, as a fraction of the mean edge length. */
static constexpr float INFLATE_SCALE = 0.5f;
/** Largest sharpen displacement per pass, as a fraction of the mean edge length. */
static constexpr float SHARPEN_CLAMP = 0.25f;

static float smoothstep(const float lo, const float hi, const float x)
{
  if (hi <= lo) {
    return x > lo ? 1.0f : 0.0f;
  }
  const float t = std::clamp((x - lo) / (hi - lo), 0.0f, 1.0f);
  return t * t * (3.0f - 2.0f * t);
}

void mesh_polish(const Mesh &mesh,
                 const Span<float3> positions,
                 const Span<float> influence,
                 const PolishSettings &settings,
                 MutableSpan<float3> r_positions)
{
  const int verts_num = positions.size();
  r_positions.copy_from(positions);
  if (mesh.edges_num == 0 || mesh.faces_num == 0) {
    return;
  }

  const Span<int2> edges = mesh.edges();
  const OffsetIndices<int> faces = mesh.faces();
  const Span<int> corner_verts = mesh.corner_verts();
  const Span<int> corner_edges = mesh.corner_edges();

  /* One-ring adjacency, built once and reused by every pass. */
  Array<int> vert_to_edge_offsets, vert_to_edge_indices;
  const GroupedSpan<int> vert_to_edge = bke::mesh::build_vert_to_edge_map(
      edges, verts_num, vert_to_edge_offsets, vert_to_edge_indices);
  Array<int> edge_to_face_offsets, edge_to_face_indices;
  const GroupedSpan<int> edge_to_face = bke::mesh::build_edge_to_face_map(
      faces, corner_edges, edges.size(), edge_to_face_offsets, edge_to_face_indices);

  Array<float3> face_normals(faces.size());
  bke::mesh::normals_calc_faces(positions, faces, corner_verts, face_normals);
  Array<float3> vert_normals(verts_num);
  bke::mesh::normals_calc_verts(
      positions, faces, corner_verts, mesh.vert_to_face_map(), face_normals, vert_normals);

  /* Dihedral angles; edges without exactly two faces pin their vertices. */
  Array<float> edge_angles(edges.size(), 0.0f);
  Array<bool> pinned(verts_num, false);
  for (const int edge : edges.index_range()) {
    const Span<int> edge_faces = edge_to_face[edge];
    if (edge_faces.size() != 2) {
      pinned[edges[edge][0]] = true;
      pinned[edges[edge][1]] = true;
      continue;
    }
    edge_angles[edge] = angle_normalized_v3v3(face_normals[edge_faces[0]],
                                              face_normals[edge_faces[1]]);
  }

  /* Per-vertex movability: 0 pins exactly. */
  Array<float> factors(verts_num);
  threading::parallel_for(factors.index_range(), 4096, [&](const IndexRange range) {
    for (const int vert : range) {
      factors[vert] = (pinned[vert] || vert_to_edge[vert].is_empty()) ?
                          0.0f :
                          std::clamp(influence[vert], 0.0f, 1.0f);
    }
  });

  /* Protection per vertex: the most protected incident edge. */
  Array<float> protection(verts_num, 0.0f);
  if (settings.angle_preservation) {
    float max_angle = 0.0f;
    for (const float angle : edge_angles) {
      max_angle = std::max(max_angle, angle);
    }
    const float hi = max_angle * (1.0f - std::clamp(settings.angle_ratio, 0.0f, 1.0f));
    threading::parallel_for(protection.index_range(), 4096, [&](const IndexRange range) {
      for (const int vert : range) {
        float w = 0.0f;
        for (const int edge : vert_to_edge[vert]) {
          w = std::max(w, smoothstep(0.0f, hi, edge_angles[edge]));
        }
        protection[vert] = w;
      }
    });
  }

  double edge_length_sum = 0.0;
  for (const int2 &edge : edges) {
    edge_length_sum += math::distance(positions[edge[0]], positions[edge[1]]);
  }
  const float mean_edge_length = float(edge_length_sum / edges.size());

  const auto neighbor_mean = [&](const Span<float3> src, const int vert) {
    float3 sum(0.0f);
    for (const int edge : vert_to_edge[vert]) {
      sum += src[bke::mesh::edge_other_vert(edges[edge], vert)];
    }
    return sum / float(vert_to_edge[vert].size());
  };

  Array<float3> src(positions);
  Array<float3> dst(verts_num);

  /* One smoothing step: the tangential part relaxes the spacing (`tangent_lambda`), the normal
   * part smooths the shape (`normal_lambda`, negative for Taubin's inflating step). */
  const auto smooth_step = [&](const float tangent_lambda, const float normal_lambda) {
    threading::parallel_for(src.index_range(), 1024, [&](const IndexRange range) {
      for (const int vert : range) {
        const float factor = factors[vert] * (1.0f - protection[vert]);
        if (factor == 0.0f) {
          dst[vert] = src[vert];
          continue;
        }
        const float3 &normal = vert_normals[vert];
        const float3 delta = neighbor_mean(src, vert) - src[vert];
        const float along_normal = math::dot(delta, normal);
        const float3 tangential = delta - normal * along_normal;
        dst[vert] = src[vert] +
                    (tangential * tangent_lambda + normal * (along_normal * normal_lambda)) *
                        factor;
      }
    });
    std::swap(src, dst);
  };

  /* Tangential smoothing alone slides the surface but can't flatten noise across it, so the
   * normal part uses Taubin's lambda|mu steps: noise is removed without shrinking the shape. */
  const auto polish_pass = [&]() {
    for ([[maybe_unused]] const int i : IndexRange(TAUBIN_STEPS)) {
      smooth_step(POLISH_LAMBDA, POLISH_LAMBDA);
      smooth_step(0.0f, TAUBIN_MU);
    }
  };

  /* A wider kernel is the neighbor mean applied repeatedly (1-3 rings). */
  const int rings = 1 + int(std::round(std::clamp(settings.softness, 0.0f, 1.0f) * 2.0f));
  Array<float3> tmp(verts_num);
  const auto smoothed = [&](const Span<float3> from, MutableSpan<float3> r_base) {
    r_base.copy_from(from);
    for ([[maybe_unused]] const int ring : IndexRange(rings)) {
      threading::parallel_for(r_base.index_range(), 1024, [&](const IndexRange range) {
        for (const int vert : range) {
          tmp[vert] = vert_to_edge[vert].is_empty() ? r_base[vert] : neighbor_mean(r_base, vert);
        }
      });
      r_base.copy_from(tmp);
    }
  };

  /* The detail (offset from the smoothed surface, along the normal) before the polish pass. */
  Array<float> reference_detail(verts_num);
  Array<float3> base(verts_num);
  const auto store_reference = [&]() {
    smoothed(src, base);
    threading::parallel_for(src.index_range(), 1024, [&](const IndexRange range) {
      for (const int vert : range) {
        reference_detail[vert] = math::dot(src[vert] - base[vert], vert_normals[vert]);
      }
    });
  };

  /* Unsharp masking against the reference: on protected vertices, put back the detail the
   * polish took away (all of it at sharpness 1), along the normal only. It restores rather
   * than exaggerates, so repeating it converges instead of growing spikes. */
  const float max_sharpen = SHARPEN_CLAMP * mean_edge_length;
  const auto sharpen_pass = [&]() {
    smoothed(src, base);
    threading::parallel_for(src.index_range(), 1024, [&](const IndexRange range) {
      for (const int vert : range) {
        const float factor = factors[vert] * protection[vert] * settings.sharpness;
        if (factor == 0.0f) {
          dst[vert] = src[vert];
          continue;
        }
        const float3 &normal = vert_normals[vert];
        const float lost = reference_detail[vert] - math::dot(src[vert] - base[vert], normal);
        dst[vert] = src[vert] + normal * std::clamp(lost * factor, -max_sharpen, max_sharpen);
      }
    });
    std::swap(src, dst);
  };

  for ([[maybe_unused]] const int i : IndexRange(std::max(settings.repeat_polish, 0))) {
    if (settings.repeat_sharpen > 0) {
      store_reference();
    }
    polish_pass();
    for ([[maybe_unused]] const int j : IndexRange(std::max(settings.repeat_sharpen, 0))) {
      sharpen_pass();
    }
  }

  if (settings.inflate_outer != 0.0f || settings.inflate_inner != 0.0f) {
    const float scale = INFLATE_SCALE * mean_edge_length;
    threading::parallel_for(src.index_range(), 1024, [&](const IndexRange range) {
      for (const int vert : range) {
        if (factors[vert] == 0.0f) {
          dst[vert] = src[vert];
          continue;
        }
        const float3 &normal = vert_normals[vert];
        const bool convex = math::dot(src[vert] - neighbor_mean(src, vert), normal) > 0.0f;
        const float amount = convex ? settings.inflate_outer : -settings.inflate_inner;
        dst[vert] = src[vert] + normal * (amount * scale * factors[vert]);
      }
    });
    std::swap(src, dst);
  }

  r_positions.copy_from(src);
}

}  // namespace blender::geometry
