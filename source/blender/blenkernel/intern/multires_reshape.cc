/* SPDX-FileCopyrightText: 2020 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup bke
 */

#include "DNA_mesh_types.h"
#include "DNA_meshdata_types.h"
#include "DNA_modifier_types.h"

#include "BKE_attribute.hh"
#include "BKE_customdata.hh"
#include "BKE_global.hh"
#include "BKE_lib_id.hh"
#include "BKE_mesh.hh"
#include "BKE_modifier.hh"
#include "BKE_multires.hh"
#include "BKE_object.hh"
#include "BKE_subdiv.hh"
#include "BKE_subdiv_foreach.hh"
#include "BKE_subdiv_mesh.hh"

#include "BLI_bit_vector.hh"
#include "BLI_task.hh"

#include "DEG_depsgraph_query.hh"

#include "multires_reshape.hh"

#include <algorithm>
#include <optional>

namespace blender {

/**
 * Store the original grids, which #multires_reshape_smooth_object_grids_with_details needs to
 * propagate existing details to the higher levels.
 *
 * Storing them duplicates the entire #CD_MDISPS layer, so skip it when the smoothing is going to
 * be skipped as well, which is the case when displacement is assigned at the top level.
 */
static void multiresModifier_reshape_store_original_grids_for_smoothing(
    MultiresReshapeContext *reshape_context)
{
  if (reshape_context->top.level == reshape_context->reshape.level) {
    return;
  }
  multires_reshape_store_original_grids(reshape_context);
}

/* -------------------------------------------------------------------- */
/** \name Reshape from object
 * \{ */

static bool multiresModifier_reshapeFromVertcos(Depsgraph *depsgraph,
                                                Object *object,
                                                MultiresModifierData *mmd,
                                                Span<float3> positions)
{
  MultiresReshapeContext reshape_context;
  if (!multires_reshape_context_create_from_object(&reshape_context, depsgraph, object, mmd)) {
    return false;
  }
  multiresModifier_reshape_store_original_grids_for_smoothing(&reshape_context);
  multires_reshape_ensure_grids(id_cast<Mesh *>(object->data), reshape_context.top.level);
  if (!multires_reshape_assign_final_coords_from_vertcos(&reshape_context, positions)) {
    multires_reshape_context_free(&reshape_context);
    return false;
  }
  multires_reshape_smooth_object_grids_with_details(&reshape_context);
  multires_reshape_object_grids_to_tangent_displacement(&reshape_context);
  multires_reshape_context_free(&reshape_context);
  return true;
}

bool multiresModifier_reshapeFromObject(Depsgraph *depsgraph,
                                        MultiresModifierData *mmd,
                                        Object *dst,
                                        Object *src)
{
  const Object *ob_eval = DEG_get_evaluated(depsgraph, src);
  if (!ob_eval) {
    return false;
  }
  const Mesh *src_mesh_eval = BKE_object_get_evaluated_mesh(ob_eval);
  if (!src_mesh_eval) {
    return false;
  }

  return multiresModifier_reshapeFromVertcos(depsgraph, dst, mmd, src_mesh_eval->vert_positions());
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Reshape from modifier
 * \{ */

bool multiresModifier_reshapeFromDeformModifier(Depsgraph *depsgraph,
                                                Object *object,
                                                MultiresModifierData *mmd,
                                                ModifierData *deform_md)
{
  MultiresModifierData highest_mmd = dna::shallow_copy(*mmd);
  highest_mmd.sculptlvl = highest_mmd.totlvl;
  highest_mmd.lvl = highest_mmd.totlvl;
  highest_mmd.renderlvl = highest_mmd.totlvl;

  /* Create mesh for the multires, ignoring any further modifiers (leading
   * deformation modifiers will be applied though). */
  Mesh *multires_mesh = BKE_multires_create_mesh(depsgraph, object, &highest_mmd);
  Array<float3> deformed_verts(multires_mesh->vert_positions());

  /* Apply deformation modifier on the multires, */
  ModifierEvalContext modifier_ctx{};
  modifier_ctx.depsgraph = depsgraph;
  modifier_ctx.object = object;
  modifier_ctx.flag = MOD_APPLY_USECACHE | MOD_APPLY_IGNORE_SIMPLIFY;

  const bool deform_success = BKE_modifier_deform_verts(
      deform_md, &modifier_ctx, multires_mesh, deformed_verts);
  BKE_id_free(nullptr, multires_mesh);
  if (!deform_success) {
    return false;
  }

  /* Reshaping */
  bool result = multiresModifier_reshapeFromVertcos(
      depsgraph, object, &highest_mmd, deformed_verts);

  return result;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Reshape from grids
 * \{ */

bool multiresModifier_reshapeFromCCG(const int tot_level, Mesh *coarse_mesh, SubdivCCG *subdiv_ccg)
{
  MultiresReshapeContext reshape_context;
  if (!multires_reshape_context_create_from_ccg(
          &reshape_context, subdiv_ccg, coarse_mesh, tot_level))
  {
    return false;
  }

  multires_ensure_external_read(coarse_mesh, reshape_context.top.level);

  multiresModifier_reshape_store_original_grids_for_smoothing(&reshape_context);
  multires_reshape_ensure_grids(coarse_mesh, reshape_context.top.level);
  if (!multires_reshape_assign_final_coords_from_ccg(&reshape_context, subdiv_ccg)) {
    multires_reshape_context_free(&reshape_context);
    return false;
  }
  multires_reshape_smooth_object_grids_with_details(&reshape_context);
  multires_reshape_object_grids_to_tangent_displacement(&reshape_context);
  multires_reshape_context_free(&reshape_context);
  return true;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Phantom: Divide by One Catmull-Clark Step
 *
 * Adding a level used to place the new points on the limit surface of the whole previous level:
 * an OpenSubdiv cage built from every point of it, mostly single-threaded, which took about a
 * minute at 100M faces. Instead, the new level is one Catmull-Clark step of the previous one,
 * computed on the grids in parallel.
 *
 * The step is applied to the displacement in object space rather than to the positions, so a
 * grid without displacement stays on the base mesh's limit surface, as it did before. Details
 * come out very slightly different: one step smooths them a little less than the limit does.
 *
 * The difference shrinks about four times with every level, so it is only used where the limit
 * surface is slow, from about 4M faces (the new level) up, where it is far below anything
 * visible; lower levels stay exactly as before. Only for closed manifold base meshes with
 * consistent winding and no creases, one level at a time. Debug value 7373 always uses the limit
 * surface and 7374 the step wherever it can (for comparisons and tests).
 * \{ */

namespace {

/**
 * Adjacency of the grids across base edges. Grid `g`'s `x == n` side lies on the base edge
 * from its corner to the next one, `y == n` on the edge from the previous corner; with
 * `n = grid_size - 1`, the points match as `(n, j)` of `g` = `(j, n)` of `grid_x[g]`, and
 * `(i, n)` of `g` = `(n, i)` of `grid_y[g]`. Within a face, `(i, 0)` of a grid is `(0, i)` of
 * the next one.
 */
struct DivideTopology {
  Array<int> grid_x;
  Array<int> grid_y;
  /** A corner of every vertex that has any, or -1. */
  Array<int> vert_corner;
};

int divide_next_corner(const MultiresReshapeContext &context, const int corner)
{
  const IndexRange face = context.base_faces[context.base_corner_to_face[corner]];
  return corner + 1 == face.one_after_last() ? face.start() : corner + 1;
}

int divide_prev_corner(const MultiresReshapeContext &context, const int corner)
{
  const IndexRange face = context.base_faces[context.base_corner_to_face[corner]];
  return corner == face.start() ? face.last() : corner - 1;
}

std::optional<DivideTopology> divide_topology(const MultiresReshapeContext &context)
{
  const Span<int> corner_verts = context.base_corner_verts;
  const Span<int> corner_edges = context.base_corner_edges;
  const int corners_num = corner_verts.size();

  Array<int2> edge_corners(context.base_edges.size(), int2(-1));
  for (const int corner : IndexRange(corners_num)) {
    int2 &corners = edge_corners[corner_edges[corner]];
    if (corners[0] == -1) {
      corners[0] = corner;
    }
    else if (corners[1] == -1) {
      corners[1] = corner;
    }
    else {
      return std::nullopt;
    }
  }

  DivideTopology topology;
  topology.grid_x.reinitialize(corners_num);
  topology.grid_y.reinitialize(corners_num);
  for (const int corner : IndexRange(corners_num)) {
    const int2 corners = edge_corners[corner_edges[corner]];
    if (corners[1] == -1) {
      return std::nullopt;
    }
    const int other = corners[0] == corner ? corners[1] : corners[0];
    const int next = divide_next_corner(context, corner);
    /* The other face must run along the edge the other way. */
    if (corner_verts[other] != corner_verts[next] ||
        corner_verts[divide_next_corner(context, other)] != corner_verts[corner])
    {
      return std::nullopt;
    }
    topology.grid_x[corner] = divide_next_corner(context, other);
    topology.grid_y[next] = other;
  }

  /* Every vertex must have a single fan of faces around it. */
  const int verts_num = context.base_positions.size();
  Array<int> vert_corners_num(verts_num, 0);
  topology.vert_corner.reinitialize(verts_num);
  topology.vert_corner.fill(-1);
  for (const int corner : IndexRange(corners_num)) {
    const int vert = corner_verts[corner];
    vert_corners_num[vert]++;
    if (topology.vert_corner[vert] == -1) {
      topology.vert_corner[vert] = corner;
    }
  }
  for (const int vert : IndexRange(verts_num)) {
    const int start = topology.vert_corner[vert];
    if (start == -1) {
      continue;
    }
    int corner = start;
    int fan_size = 0;
    do {
      corner = topology.grid_x[corner];
      fan_size++;
    } while (corner != start && fan_size <= vert_corners_num[vert]);
    if (fan_size != vert_corners_num[vert]) {
      return std::nullopt;
    }
  }
  return topology;
}

bool divide_has_creases(const MultiresReshapeContext &context)
{
  const bke::AttributeAccessor attributes = context.base_mesh->attributes();
  const auto any = [&](const StringRef name, const bke::AttrDomain domain) {
    const VArraySpan<float> creases = *attributes.lookup<float>(name, domain);
    return std::any_of(creases.begin(), creases.end(), [](const float crease) {
      return crease > 0.0f;
    });
  };
  return any("crease_vert", bke::AttrDomain::Point) || any("crease_edge", bke::AttrDomain::Edge);
}

/** The new position of a vertex of valence `k` (Catmull-Clark), from the sum of the face
 * points and the sum of the edge neighbors around it. */
float3 divide_vertex_point(const float3 &vert,
                           const float3 &face_points_sum,
                           const float3 &edge_neighbors_sum,
                           const int k)
{
  const float k_inv = 1.0f / float(k);
  /* (Q + 2R + (k - 3)S) / k, where R is the average of the edge midpoints. */
  const float3 q = face_points_sum * k_inv;
  const float3 r = (vert * float(k) + edge_neighbors_sum) * (0.5f * k_inv);
  return (q + r * 2.0f + vert * float(k - 3)) * k_inv;
}

/** Face point of a grid cell, summed so grids that share it agree to the bit. */
float3 divide_face_point(const float3 &p00, const float3 &p10, const float3 &p01, const float3 &p11)
{
  return ((p00 + p11) + (p10 + p01)) * 0.25f;
}

/**
 * Add one Catmull-Clark level to the grids; false (and nothing changed) when the base mesh
 * needs the general method.
 */
bool divide_by_catmull_clark_step(const MultiresReshapeContext &context)
{
  if (G.debug_value == 7373) {
    return false;
  }
  constexpr int64_t min_faces = int64_t(1) << 22;
  if (context.top.level != context.reshape.level + 1 || context.subdiv->settings.is_simple ||
      divide_has_creases(context))
  {
    return false;
  }
  const int old_size = bke::subdiv::grid_size_from_level(context.reshape.level);
  const int n = old_size - 1;
  const int new_size = context.top.grid_size;
  const int grids_num = context.num_grids;
  MDisps *mdisps = context.mdisps;
  if (new_size != 2 * n + 1 || mdisps == nullptr) {
    return false;
  }
  if (int64_t(grids_num) * (new_size - 1) * (new_size - 1) < min_faces && G.debug_value != 7374) {
    return false;
  }
  for (const int grid : IndexRange(grids_num)) {
    if (mdisps[grid].disps == nullptr || mdisps[grid].totdisp != old_size * old_size) {
      return false;
    }
  }
  const std::optional<DivideTopology> topology = divide_topology(context);
  if (!topology) {
    return false;
  }
  const Span<int> grid_x = topology->grid_x;
  const Span<int> grid_y = topology->grid_y;

  const auto convert_space = [&](const int size, const bool to_tangent) {
    const float step = 1.0f / float(size - 1);
    threading::parallel_for(IndexRange(grids_num), 16, [&](const IndexRange range) {
      for (const int grid : range) {
        float3 *disps = reinterpret_cast<float3 *>(mdisps[grid].disps);
        for (const int y : IndexRange(size)) {
          for (const int x : IndexRange(size)) {
            const GridCoord coord{grid, float(x) * step, float(y) * step};
            float3 P;
            float3x3 tangent_matrix;
            multires_reshape_evaluate_base_mesh_limit_at_grid(&context, &coord, P, tangent_matrix);
            float3 &disp = disps[y * size + x];
            disp = math::transform_direction(
                to_tangent ? math::invert(tangent_matrix) : tangent_matrix, disp);
          }
        }
      }
    });
  };

  /* The previous level's displacement in object space, kept aside while the grids are
   * reallocated for the new level. */
  convert_space(old_size, false);
  Array<float(*)[3]> old_disps(grids_num);
  for (const int grid : IndexRange(grids_num)) {
    old_disps[grid] = mdisps[grid].disps;
    mdisps[grid].disps = nullptr;
    mdisps[grid].totdisp = 0;
  }
  GridPaintMask *masks = context.grid_paint_masks;
  Array<float *> old_masks;
  Array<int> old_mask_sizes;
  if (masks) {
    old_masks.reinitialize(grids_num);
    old_mask_sizes.reinitialize(grids_num);
    for (const int grid : IndexRange(grids_num)) {
      old_masks[grid] = nullptr;
      if (masks[grid].level < context.top.level) {
        old_masks[grid] = masks[grid].data;
        old_mask_sizes[grid] = bke::subdiv::grid_size_from_level(masks[grid].level);
        masks[grid].data = nullptr;
        masks[grid].level = 0;
      }
    }
  }
  multires_reshape_ensure_grids(context.base_mesh, context.top.level);

  const auto old_point = [&](const int grid, const int x, const int y) -> const float3 & {
    return reinterpret_cast<const float3 *>(old_disps[grid])[y * old_size + x];
  };

  /* Everything but the face centers and the base vertices, with the grid padded by one row of
   * its neighbors' points on each side, so all of it is regular. */
  const int pad_size = n + 3;
  threading::parallel_for(IndexRange(grids_num), 16, [&](const IndexRange range) {
    Array<float3> pad(pad_size * pad_size, float3(0.0f));
    Array<float3> face_points(pad_size * pad_size, float3(0.0f));
    const auto P = [&](const int x, const int y) -> float3 & {
      return pad[(y + 1) * pad_size + x + 1];
    };
    const auto F = [&](const int x, const int y) -> float3 & {
      return face_points[(y + 1) * pad_size + x + 1];
    };
    for (const int grid : range) {
      const int next = divide_next_corner(context, grid);
      const int prev = divide_prev_corner(context, grid);
      for (const int y : IndexRange(old_size)) {
        for (const int x : IndexRange(old_size)) {
          P(x, y) = old_point(grid, x, y);
        }
      }
      for (const int i : IndexRange(old_size)) {
        P(i, -1) = old_point(next, 1, i);
        P(-1, i) = old_point(prev, i, 1);
        P(n + 1, i) = old_point(grid_x[grid], i, n - 1);
        P(i, n + 1) = old_point(grid_y[grid], n - 1, i);
      }
      /* Diagonal neighbors of the edge midpoints on the base edges. */
      P(n + 1, -1) = old_point(divide_prev_corner(context, grid_x[grid]), n - 1, 1);
      P(-1, n + 1) = old_point(grid_x[prev], 1, n - 1);

      for (int y = -1; y <= n; y++) {
        for (int x = -1; x <= n; x++) {
          F(x, y) = divide_face_point(P(x, y), P(x + 1, y), P(x, y + 1), P(x + 1, y + 1));
        }
      }

      float3 *dst = reinterpret_cast<float3 *>(mdisps[grid].disps);
      const auto D = [&](const int x, const int y) -> float3 & { return dst[y * new_size + x]; };
      for (const int j : IndexRange(old_size)) {
        for (const int i : IndexRange(old_size)) {
          if (i < n && j < n) {
            D(2 * i + 1, 2 * j + 1) = F(i, j);
          }
          if (i < n) {
            D(2 * i + 1, 2 * j) = ((P(i, j) + P(i + 1, j)) + (F(i, j - 1) + F(i, j))) * 0.25f;
          }
          if (j < n) {
            D(2 * i, 2 * j + 1) = ((P(i, j) + P(i, j + 1)) + (F(i - 1, j) + F(i, j))) * 0.25f;
          }
          if ((i == 0 && j == 0) || (i == n && j == n)) {
            continue;
          }
          const float3 edges = (P(i - 1, j) + P(i + 1, j)) + (P(i, j - 1) + P(i, j + 1));
          const float3 diagonals = (P(i - 1, j - 1) + P(i + 1, j + 1)) +
                                   (P(i + 1, j - 1) + P(i - 1, j + 1));
          D(2 * i, 2 * j) = P(i, j) * (9.0f / 16.0f) + edges * (3.0f / 32.0f) +
                            diagonals * (1.0f / 64.0f);
        }
      }

      if (masks && old_masks[grid]) {
        const float *old_mask = old_masks[grid];
        const int old_mask_size = old_mask_sizes[grid];
        float *mask = masks[grid].data;
        if (old_mask_size == old_size) {
          /* Linear, like the displacement of a simple subdivision. */
          const auto M = [&](const int x, const int y) { return old_mask[y * old_size + x]; };
          for (const int y : IndexRange(new_size)) {
            for (const int x : IndexRange(new_size)) {
              const int x0 = x / 2, y0 = y / 2, x1 = (x + 1) / 2, y1 = (y + 1) / 2;
              mask[y * new_size + x] = (M(x0, y0) + M(x1, y0) + M(x0, y1) + M(x1, y1)) * 0.25f;
            }
          }
        }
        else {
          const float scale = float(old_mask_size - 1) / float(new_size - 1);
          for (const int y : IndexRange(new_size)) {
            for (const int x : IndexRange(new_size)) {
              mask[y * new_size + x] =
                  old_mask[lround(float(y) * scale) * old_mask_size + lround(float(x) * scale)];
            }
          }
        }
      }
    }
  });

  /* Face centers. */
  const OffsetIndices faces = context.base_faces;
  threading::parallel_for(faces.index_range(), 256, [&](const IndexRange range) {
    for (const int face : range) {
      const IndexRange grids = faces[face];
      const float3 &center = old_point(grids.start(), 0, 0);
      float3 face_points_sum(0.0f);
      float3 edge_neighbors_sum(0.0f);
      for (const int grid : grids) {
        face_points_sum += divide_face_point(old_point(grid, 0, 0),
                                             old_point(grid, 1, 0),
                                             old_point(grid, 0, 1),
                                             old_point(grid, 1, 1));
        edge_neighbors_sum += old_point(grid, 1, 0);
      }
      const float3 new_center = divide_vertex_point(
          center, face_points_sum, edge_neighbors_sum, grids.size());
      for (const int grid : grids) {
        reinterpret_cast<float3 *>(mdisps[grid].disps)[0] = new_center;
      }
    }
  });

  /* Base vertices: their grids are found by going around the fan. */
  const Span<int> vert_corner = topology->vert_corner;
  const int last = new_size * new_size - 1;
  threading::parallel_for(vert_corner.index_range(), 256, [&](const IndexRange range) {
    for (const int vert : range) {
      const int start = vert_corner[vert];
      if (start == -1) {
        continue;
      }
      const float3 &old_vert = old_point(start, n, n);
      float3 face_points_sum(0.0f);
      float3 edge_neighbors_sum(0.0f);
      int k = 0;
      int grid = start;
      do {
        face_points_sum += divide_face_point(old_point(grid, n - 1, n - 1),
                                             old_point(grid, n, n - 1),
                                             old_point(grid, n - 1, n),
                                             old_point(grid, n, n));
        edge_neighbors_sum += old_point(grid, n, n - 1);
        k++;
        grid = grid_x[grid];
      } while (grid != start);
      const float3 new_vert = divide_vertex_point(
          old_vert, face_points_sum, edge_neighbors_sum, k);
      do {
        reinterpret_cast<float3 *>(mdisps[grid].disps)[last] = new_vert;
        grid = grid_x[grid];
      } while (grid != start);
    }
  });

  threading::parallel_for(IndexRange(grids_num), 1024, [&](const IndexRange range) {
    for (const int grid : range) {
      MEM_delete(old_disps[grid]);
      if (masks && old_masks[grid]) {
        MEM_delete(old_masks[grid]);
      }
    }
  });

  convert_space(new_size, true);
  return true;
}

}  // namespace

/** \} */

/* -------------------------------------------------------------------- */
/** \name Subdivision
 * \{ */

void multiresModifier_subdivide(Object *object,
                                MultiresModifierData *mmd,
                                const MultiresSubdivideModeType mode)
{
  const int top_level = mmd->totlvl + 1;
  multiresModifier_subdivide_to_level(object, mmd, top_level, mode);
}

void multiresModifier_subdivide_to_level(Object *object,
                                         MultiresModifierData *mmd,
                                         const int top_level,
                                         const MultiresSubdivideModeType mode)
{
  if (top_level <= mmd->totlvl) {
    return;
  }

  Mesh *coarse_mesh = id_cast<Mesh *>(object->data);
  if (coarse_mesh->corners_num == 0) {
    /* If there are no loops in the mesh implies there is no CD_MDISPS as well. So can early output
     * from here as there is nothing to subdivide. */
    return;
  }

  MultiresReshapeContext reshape_context;

  /* There was no multires at all, all displacement is at 0. Can simply make sure all mdisps grids
   * are allocated at a proper level and return. */
  const bool has_mdisps = CustomData_has_layer(&coarse_mesh->corner_data, CD_MDISPS);
  if (!has_mdisps) {
    CustomData_add_layer(
        &coarse_mesh->corner_data, CD_MDISPS, CD_SET_DEFAULT, coarse_mesh->corners_num);
  }

  /* NOTE: Subdivision happens from the top level of the existing multires modifier. If it is set
   * to 0 and there is mdisps layer it would mean that the modifier went out of sync with the data.
   * This happens when, for example, linking modifiers from one object to another.
   *
   * In such cases simply ensure grids to be the proper level.
   *
   * If something smarter is needed it is up to the operators which does data synchronization, so
   * that the mdisps layer is also synchronized. */
  if (!has_mdisps || top_level == 1 || mmd->totlvl == 0) {
    multires_reshape_ensure_grids(coarse_mesh, top_level);
    if (ELEM(mode, MultiresSubdivideModeType::Linear, MultiresSubdivideModeType::Simple)) {
      multires_subdivide_create_tangent_displacement_linear_grids(object, mmd);
    }
    else {
      multires_set_tot_level(object, mmd, top_level);
    }
    return;
  }

  multires_flush_sculpt_updates(object);

  if (!multires_reshape_context_create_from_modifier(&reshape_context, object, mmd, top_level)) {
    return;
  }

  if (mode == MultiresSubdivideModeType::CatmullClark &&
      divide_by_catmull_clark_step(reshape_context))
  {
    multires_reshape_context_free(&reshape_context);
    multires_set_tot_level(object, mmd, top_level);
    return;
  }

  multires_reshape_store_original_grids(&reshape_context);
  multires_reshape_ensure_grids(coarse_mesh, reshape_context.top.level);
  multires_reshape_assign_final_elements_from_orig_mdisps(&reshape_context);

  /* Free original grids which makes it so smoothing with details thinks all the details were
   * added against base mesh's limit surface. This is similar behavior to as if we've done all
   * displacement in sculpt mode at the old top level and then propagated to the new top level. */
  multires_reshape_free_original_grids(&reshape_context);

  if (ELEM(mode, MultiresSubdivideModeType::Linear, MultiresSubdivideModeType::Simple)) {
    multires_reshape_smooth_object_grids(&reshape_context, mode);
  }
  else {
    multires_reshape_smooth_object_grids_with_details(&reshape_context);
  }

  multires_reshape_object_grids_to_tangent_displacement(&reshape_context);
  multires_reshape_context_free(&reshape_context);

  multires_set_tot_level(object, mmd, top_level);
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Apply base
 * \{ */

void multiresModifier_base_apply(Depsgraph *depsgraph,
                                 Object *object,
                                 MultiresModifierData *mmd,
                                 const ApplyBaseMode mode)
{
  multires_force_sculpt_rebuild(object);

  MultiresReshapeContext reshape_context;
  if (!multires_reshape_context_create_from_object(&reshape_context, depsgraph, object, mmd)) {
    return;
  }

  multires_reshape_store_original_grids(&reshape_context);

  /* At this point base_mesh is object's mesh, the subdiv is initialized to the deformed state of
   * the base mesh.
   * Store coordinates of top level grids in object space which will define true shape we would
   * want to reshape to after modifying the base mesh. */
  multires_reshape_assign_final_coords_from_mdisps(&reshape_context);

  /* For modifying base mesh we only want to consider deformation caused by multires displacement
   * and ignore all deformation which might be caused by deformation modifiers leading the multires
   * one.
   * So refine the subdiv to the original mesh vertices positions, which will also need to make
   * it so object space displacement is re-evaluated for them (as in, can not re-use any knowledge
   * from the final coordinates in the object space ). */
  multires_reshape_apply_base_refine_from_base(&reshape_context);

  /* Modify original mesh coordinates. This happens in two steps:
   * - Coordinates are set to their final location, where they are intended to be in the final
   *   result.
   * - Heuristic moves them a bit, kind of canceling out the effect of subsurf (so then when
   *   multires modifier applies subsurf vertices are placed at the desired location). */
  multires_reshape_apply_base_update_mesh_coords(&reshape_context);
  if (mode == ApplyBaseMode::ForSubdivision) {
    multires_reshape_apply_base_refit_base_mesh(&reshape_context);
  }
  multires_reshape_apply_base_update_shape_key(&reshape_context);

  /* Reshape to the stored final state.
   * Not that the base changed, so the subdiv is to be refined to the new positions. Unfortunately,
   * this can not be done foe entirely cheap: if there were deformation modifiers prior to the
   * multires they need to be re-evaluated for the new base mesh. */
  multires_reshape_apply_base_refine_from_deform(&reshape_context);
  multires_reshape_object_grids_to_tangent_displacement(&reshape_context);

  multires_reshape_context_free(&reshape_context);
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Phantom: Delete Lower
 * \{ */

namespace {

/** Where every face corner of a subdivided mesh is, as a PTEX coordinate of the base mesh. */
struct CornerPtexContext {
  MutableSpan<PTexCoord> coords;
};

}  // namespace

static bool corner_ptex_foreach_topology_info(const bke::subdiv::ForeachContext *foreach_context,
                                              const int /*num_vertices*/,
                                              const int /*num_edges*/,
                                              const int num_loops,
                                              const int /*num_faces*/,
                                              const Span<int> /*subdiv_face_offset*/)
{
  const CornerPtexContext &context = *static_cast<const CornerPtexContext *>(
      foreach_context->user_data);
  return num_loops == context.coords.size();
}

static void corner_ptex_foreach_loop(const bke::subdiv::ForeachContext *foreach_context,
                                     void * /*tls*/,
                                     const int ptex_face_index,
                                     const float u,
                                     const float v,
                                     const int /*coarse_loop_index*/,
                                     const int /*coarse_face_index*/,
                                     const int /*coarse_corner*/,
                                     const int subdiv_loop_index,
                                     const int /*subdiv_vert_index*/,
                                     const int /*subdiv_edge_index*/)
{
  CornerPtexContext &context = *static_cast<CornerPtexContext *>(foreach_context->user_data);
  context.coords[subdiv_loop_index] = {ptex_face_index, u, v};
}

bool multiresModifier_del_lower_levels(Depsgraph *depsgraph,
                                       Object *object,
                                       MultiresModifierData *mmd,
                                       const int level)
{
  using namespace bke;
  const int top_level = mmd->totlvl;
  if (level <= 0 || level > top_level) {
    return false;
  }
  Mesh *mesh = id_cast<Mesh *>(object->data);

  /* Sculpted grids go into the displacement first. */
  multires_force_sculpt_rebuild(object);
  multires_ensure_external_read(mesh, top_level);
  multires_reshape_ensure_grids(mesh, top_level);

  MultiresReshapeContext old_context;
  if (!multires_reshape_context_create_from_object(&old_context, depsgraph, object, mmd)) {
    return false;
  }
  /* The shape at the top level, in object space, in place of the displacement. Every level shows
   * this shape at its own grid points (lower levels sample the top level's displacement). */
  multires_reshape_assign_final_coords_from_mdisps(&old_context);
  const bool has_masks = old_context.grid_paint_masks != nullptr;

  /* The new base mesh: the base mesh subdivided to `level`, for its topology and attributes. Its
   * positions are the shape at that level, set below. */
  subdiv::ToMeshSettings mesh_settings;
  mesh_settings.resolution = (1 << level) + 1;
  mesh_settings.use_optimal_display = false;
  Mesh *new_mesh = subdiv::subdiv_to_mesh(
      old_context.subdiv, &mesh_settings, old_context.base_mesh);

  auto cancel = [&]() {
    if (new_mesh) {
      BKE_id_free(nullptr, new_mesh);
    }
    multires_reshape_object_grids_to_tangent_displacement(&old_context);
    multires_reshape_context_free(&old_context);
    return false;
  };
  if (new_mesh == nullptr) {
    return cancel();
  }

  /* Where each corner of the new base mesh is on the old grids. */
  Array<PTexCoord> corner_ptex(new_mesh->corners_num);
  CornerPtexContext corner_context{corner_ptex};
  subdiv::ForeachContext foreach_context{};
  foreach_context.topology_info = corner_ptex_foreach_topology_info;
  foreach_context.loop = corner_ptex_foreach_loop;
  foreach_context.user_data = &corner_context;
  if (!subdiv::foreach_subdiv_geometry(
          old_context.subdiv, &foreach_context, &mesh_settings, old_context.base_mesh))
  {
    return cancel();
  }

  /* Every new face lies on one old grid: the grid it is on, and its corners' coordinates there,
   * on the grid points of `level`. Corners on the edge of a grid are moved a little towards the
   * face's center to find the grid, then snapped back. */
  const OffsetIndices<int> new_faces = new_mesh->faces();
  const float lattice = float(subdiv::grid_size_from_level(level) - 1);
  Array<int> face_grid(new_faces.size());
  Array<float2> corner_uv(new_mesh->corners_num);
  std::atomic<bool> valid = true;
  threading::parallel_for(new_faces.index_range(), 1024, [&](const IndexRange range) {
    for (const int face : range) {
      const IndexRange corners = new_faces[face];
      if (corners.size() != 4) {
        valid = false;
        continue;
      }
      PTexCoord center{corner_ptex[corners.first()].ptex_face_index, 0.0f, 0.0f};
      for (const int corner : corners) {
        if (corner_ptex[corner].ptex_face_index != center.ptex_face_index) {
          valid = false;
        }
        center.u += corner_ptex[corner].u * 0.25f;
        center.v += corner_ptex[corner].v * 0.25f;
      }
      const GridCoord center_grid = multires_reshape_ptex_coord_to_grid(&old_context, &center);
      face_grid[face] = center_grid.grid_index;
      for (const int corner : corners) {
        PTexCoord nudged = corner_ptex[corner];
        nudged.u += (center.u - nudged.u) * 1e-3f;
        nudged.v += (center.v - nudged.v) * 1e-3f;
        const GridCoord grid_coord = multires_reshape_ptex_coord_to_grid(&old_context, &nudged);
        if (grid_coord.grid_index != center_grid.grid_index) {
          valid = false;
        }
        corner_uv[corner] = float2(std::round(grid_coord.u * lattice) / lattice,
                                   std::round(grid_coord.v * lattice) / lattice);
      }
    }
  });
  if (!valid) {
    return cancel();
  }

  auto final_element = [&](const int grid, const float2 uv) {
    const GridCoord grid_coord{grid, uv.x, uv.y};
    return multires_reshape_grid_element_for_grid_coord(&old_context, &grid_coord);
  };

  /* The new base mesh's positions (and its mask, when no levels are left). */
  const Span<int> new_corner_verts = new_mesh->corner_verts();
  MutableSpan<float3> new_positions = new_mesh->vert_positions_for_write();
  Array<float> new_vert_masks(has_masks ? new_mesh->verts_num : 0);
  BitVector<> vert_done(new_mesh->verts_num, false);
  for (const int face : new_faces.index_range()) {
    for (const int corner : new_faces[face]) {
      const int vert = new_corner_verts[corner];
      if (vert_done[vert]) {
        continue;
      }
      vert_done[vert].set();
      const ReshapeGridElement element = final_element(face_grid[face], corner_uv[corner]);
      new_positions[vert] = *element.displacement;
      if (has_masks) {
        new_vert_masks[vert] = *element.mask;
      }
    }
  }
  new_mesh->tag_positions_changed();

  /* The shape at the new top level, for the grids of the new base mesh. A new face's grid for
   * corner `k` runs from the face's center (0, 0) to the corner (1, 1), with (1, 0) half way to
   * the next corner and (0, 1) half way to the previous one; those points are on the old grid. */
  const int new_levels = top_level - level;
  const int new_grid_size = subdiv::grid_size_from_level(new_levels);
  const int new_grid_area = new_grid_size * new_grid_size;
  Array<float3> new_grid_positions(new_levels > 0 ? int64_t(new_mesh->corners_num) *
                                                        new_grid_area :
                                                    0);
  Array<float> new_grid_masks(new_levels > 0 && has_masks ? new_grid_positions.size() : 0);
  if (new_levels > 0) {
    const float step = 1.0f / float(new_grid_size - 1);
    threading::parallel_for(new_faces.index_range(), 256, [&](const IndexRange range) {
      for (const int face : range) {
        const IndexRange corners = new_faces[face];
        const Span<float2> uv = corner_uv.as_span().slice(corners);
        const float2 center = (uv[0] + uv[1] + uv[2] + uv[3]) * 0.25f;
        for (const int k : IndexRange(4)) {
          const float2 corner = uv[k];
          const float2 next_mid = (uv[k] + uv[(k + 1) % 4]) * 0.5f;
          const float2 prev_mid = (uv[k] + uv[(k + 3) % 4]) * 0.5f;
          const int64_t grid_start = int64_t(corners[k]) * new_grid_area;
          for (const int y : IndexRange(new_grid_size)) {
            const float gv = float(y) * step;
            for (const int x : IndexRange(new_grid_size)) {
              const float gu = float(x) * step;
              const float2 point = center * ((1.0f - gu) * (1.0f - gv)) +
                                   next_mid * (gu * (1.0f - gv)) + corner * (gu * gv) +
                                   prev_mid * ((1.0f - gu) * gv);
              const ReshapeGridElement element = final_element(face_grid[face], point);
              const int64_t index = grid_start + y * new_grid_size + x;
              new_grid_positions[index] = *element.displacement;
              if (has_masks) {
                new_grid_masks[index] = *element.mask;
              }
            }
          }
        }
      }
    });
  }
  multires_reshape_context_free(&old_context);

  /* Replace the base mesh (its grids go with it). */
  BKE_mesh_nomain_to_mesh(new_mesh, mesh, object);
  new_mesh = nullptr;

  auto lower = [&](char &value) {
    value = char(std::clamp(int(value) - level, 0, new_levels));
  };
  mmd->totlvl = char(new_levels);
  lower(mmd->lvl);
  lower(mmd->sculptlvl);
  lower(mmd->renderlvl);

  if (new_levels == 0) {
    if (has_masks) {
      bke::SpanAttributeWriter<float> masks =
          mesh->attributes_for_write().lookup_or_add_for_write_only_span<float>(
              ".sculpt_mask", bke::AttrDomain::Point);
      masks.span.copy_from(new_vert_masks);
      masks.finish();
    }
  }
  else {
    CustomData_add_layer(&mesh->corner_data, CD_MDISPS, CD_SET_DEFAULT, mesh->corners_num);
    if (has_masks) {
      CustomData_add_layer(
          &mesh->corner_data, CD_GRID_PAINT_MASK, CD_SET_DEFAULT, mesh->corners_num);
    }
    multires_reshape_ensure_grids(mesh, new_levels);
    MultiresReshapeContext new_context;
    if (!multires_reshape_context_create_from_base_mesh(&new_context, depsgraph, object, mmd)) {
      return false;
    }
    threading::parallel_for(IndexRange(mesh->corners_num), 256, [&](const IndexRange range) {
      for (const int grid : range) {
        for (const int y : IndexRange(new_grid_size)) {
          for (const int x : IndexRange(new_grid_size)) {
            const GridCoord grid_coord{grid,
                                       float(x) / float(new_grid_size - 1),
                                       float(y) / float(new_grid_size - 1)};
            const ReshapeGridElement element = multires_reshape_grid_element_for_grid_coord(
                &new_context, &grid_coord);
            const int64_t index = int64_t(grid) * new_grid_area + y * new_grid_size + x;
            *element.displacement = new_grid_positions[index];
            if (element.mask) {
              *element.mask = new_grid_masks[index];
            }
          }
        }
      }
    });
    multires_reshape_object_grids_to_tangent_displacement(&new_context);
    multires_reshape_context_free(&new_context);
  }

  multires_force_sculpt_rebuild(object);
  return true;
}

/** \} */

}  // namespace blender
