/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup bke
 *
 * Phantom: Multires polypaint. The colors of every grid are stored in the base mesh's
 * #CD_GRID_PAINT_COLOR layer at the level they were painted at, and resampled to the level of the
 * #SubdivCCG being sculpted.
 */

#include <cstring>

#include "DNA_mesh_types.h"
#include "DNA_meshdata_types.h"

#include "BLI_array.hh"
#include "BLI_math_base.hh"
#include "BLI_math_vector.hh"
#include "BLI_task.hh"

#include "BKE_customdata.hh"
#include "BKE_subdiv.hh"
#include "BKE_subdiv_ccg.hh"

#include "MEM_guardedalloc.h"

namespace blender {

static_assert(sizeof(MLoopCol) == sizeof(ColorGeometry4b));

static float4 to_float4(const MLoopCol &color)
{
  return float4(color.r, color.g, color.b, color.a);
}

static float4 to_float4(const ColorGeometry4b &color)
{
  return float4(color.r, color.g, color.b, color.a);
}

static uchar to_uchar(const float value)
{
  return uchar(math::clamp(value + 0.5f, 0.0f, 255.0f));
}

ColorGeometry4b BKE_grid_paint_color_sample(const GridPaintColor &grid,
                                            const float u,
                                            const float v)
{
  if (grid.data == nullptr || grid.level < 1) {
    return ColorGeometry4b(255, 255, 255, 255);
  }
  const int size = bke::subdiv::grid_size_from_level(grid.level);
  const float fx = math::clamp(u, 0.0f, 1.0f) * (size - 1);
  const float fy = math::clamp(v, 0.0f, 1.0f) * (size - 1);
  const int x = std::min(int(fx), size - 2);
  const int y = std::min(int(fy), size - 2);
  const float tx = fx - x;
  const float ty = fy - y;
  const MLoopCol *data = grid.data;
  const float4 bottom = math::interpolate(
      to_float4(data[y * size + x]), to_float4(data[y * size + x + 1]), tx);
  const float4 top = math::interpolate(
      to_float4(data[(y + 1) * size + x]), to_float4(data[(y + 1) * size + x + 1]), tx);
  const float4 color = math::interpolate(bottom, top, ty);
  return ColorGeometry4b(
      to_uchar(color.x), to_uchar(color.y), to_uchar(color.z), to_uchar(color.w));
}

void BKE_subdiv_ccg_colors_from_mesh(SubdivCCG &subdiv_ccg, const Mesh &coarse_mesh)
{
  const GridPaintColor *grid_colors = static_cast<const GridPaintColor *>(
      CustomData_get_layer(&coarse_mesh.corner_data, CD_GRID_PAINT_COLOR));
  if (grid_colors == nullptr || coarse_mesh.corners_num != subdiv_ccg.grids_num ||
      subdiv_ccg.level < 1)
  {
    subdiv_ccg.colors = {};
    return;
  }
  const int level = subdiv_ccg.level;
  const int size = subdiv_ccg.grid_size;
  const int area = subdiv_ccg.grid_area;
  const float size_1_inv = 1.0f / float(size - 1);
  subdiv_ccg.colors.reinitialize(int64_t(subdiv_ccg.grids_num) * area);
  MutableSpan<ColorGeometry4b> colors = subdiv_ccg.colors;
  threading::parallel_for(IndexRange(subdiv_ccg.grids_num), 64, [&](const IndexRange range) {
    for (const int grid : range) {
      const GridPaintColor &src = grid_colors[grid];
      MutableSpan<ColorGeometry4b> dst = colors.slice(int64_t(grid) * area, area);
      if (src.data == nullptr || src.level < 1) {
        dst.fill(ColorGeometry4b(255, 255, 255, 255));
      }
      else if (int(src.level) == level) {
        memcpy(dst.data(), src.data, sizeof(MLoopCol) * area);
      }
      else if (int(src.level) > level) {
        /* The CCG's points are a subset of the stored ones. */
        const int step = 1 << (src.level - level);
        const int src_size = bke::subdiv::grid_size_from_level(src.level);
        for (const int y : IndexRange(size)) {
          for (const int x : IndexRange(size)) {
            const MLoopCol &color = src.data[(y * step) * src_size + x * step];
            dst[y * size + x] = ColorGeometry4b(color.r, color.g, color.b, color.a);
          }
        }
      }
      else {
        for (const int y : IndexRange(size)) {
          for (const int x : IndexRange(size)) {
            dst[y * size + x] = BKE_grid_paint_color_sample(
                src, x * size_1_inv, y * size_1_inv);
          }
        }
      }
    }
  });
}

void BKE_subdiv_ccg_colors_to_mesh(SubdivCCG &subdiv_ccg,
                                   Mesh &coarse_mesh,
                                   const int top_level)
{
  subdiv_ccg.dirty.colors = false;
  if (subdiv_ccg.colors.is_empty() || coarse_mesh.corners_num != subdiv_ccg.grids_num) {
    return;
  }
  GridPaintColor *grid_colors = static_cast<GridPaintColor *>(CustomData_get_layer_for_write(
      &coarse_mesh.corner_data, CD_GRID_PAINT_COLOR, coarse_mesh.corners_num));
  if (grid_colors == nullptr) {
    grid_colors = static_cast<GridPaintColor *>(CustomData_add_layer(
        &coarse_mesh.corner_data, CD_GRID_PAINT_COLOR, CD_SET_DEFAULT, coarse_mesh.corners_num));
  }
  const int level = subdiv_ccg.level;
  const int size = subdiv_ccg.grid_size;
  const int area = subdiv_ccg.grid_area;
  const Span<ColorGeometry4b> colors = subdiv_ccg.colors;
  threading::parallel_for(IndexRange(subdiv_ccg.grids_num), 64, [&](const IndexRange range) {
    Array<float4> delta(area);
    for (const int grid : range) {
      GridPaintColor &dst = grid_colors[grid];
      const Span<ColorGeometry4b> src = colors.slice(int64_t(grid) * area, area);
      /* Levels above the top one were deleted (Delete Higher): replace them. */
      if (dst.data == nullptr || int(dst.level) <= level || int(dst.level) > top_level) {
        if (dst.data == nullptr || int(dst.level) != level) {
          MEM_SAFE_DELETE(dst.data);
          dst.data = MEM_new_array_uninitialized<MLoopCol>(size_t(area), __func__);
          dst.level = level;
        }
        memcpy(dst.data, src.data(), sizeof(MLoopCol) * area);
        continue;
      }
      /* The grid holds more detail than the CCG: add the change made at the CCG's level,
       * interpolated, so the detail in between is kept. */
      const int step = 1 << (dst.level - level);
      const int dst_size = bke::subdiv::grid_size_from_level(dst.level);
      bool changed = false;
      for (const int y : IndexRange(size)) {
        for (const int x : IndexRange(size)) {
          const float4 old_color = to_float4(dst.data[(y * step) * dst_size + x * step]);
          const float4 d = to_float4(src[y * size + x]) - old_color;
          delta[y * size + x] = d;
          changed |= !math::is_zero(d);
        }
      }
      if (!changed) {
        continue;
      }
      const float step_inv = 1.0f / float(step);
      for (const int Y : IndexRange(dst_size)) {
        const int y = std::min(Y / step, size - 2);
        const float ty = float(Y - y * step) * step_inv;
        for (const int X : IndexRange(dst_size)) {
          const int x = std::min(X / step, size - 2);
          const float tx = float(X - x * step) * step_inv;
          const float4 bottom = math::interpolate(
              delta[y * size + x], delta[y * size + x + 1], tx);
          const float4 top = math::interpolate(
              delta[(y + 1) * size + x], delta[(y + 1) * size + x + 1], tx);
          const float4 d = math::interpolate(bottom, top, ty);
          if (math::is_zero(d)) {
            continue;
          }
          MLoopCol &color = dst.data[Y * dst_size + X];
          const float4 result = to_float4(color) + d;
          color.r = to_uchar(result.x);
          color.g = to_uchar(result.y);
          color.b = to_uchar(result.z);
          color.a = to_uchar(result.w);
        }
      }
    }
  });
}

}  // namespace blender
