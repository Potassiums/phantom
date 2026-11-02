/* SPDX-FileCopyrightText: 2020 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup edsculpt
 */

#include "DNA_brush_types.h"
#include "DNA_meshdata_types.h"

#include "BLI_color.hh"
#include "BLI_enumerable_thread_specific.hh"
#include "BLI_hash_c.hh"
#include "BLI_math_color_blend.hh"
#include "BLI_math_matrix.hh"
#include "BLI_math_vector.hh"
#include "BLI_vector.hh"

#include "BKE_attribute.hh"
#include "BKE_brush.hh"
#include "BKE_colorband.hh"
#include "BKE_colortools.hh"
#include "BKE_customdata.hh"
#include "BKE_mesh.hh"
#include "BKE_object_types.hh"
#include "BKE_paint.hh"
#include "BKE_paint_bvh.hh"
#include "BKE_paint_types.hh"
#include "BKE_subdiv_ccg.hh"

#include "IMB_colormanagement.hh"

#include "mesh_brush_common.hh"
#include "sculpt_automask.hh"
#include "sculpt_color.hh"
#include "sculpt_intern.hh"
#include "sculpt_smooth.hh"

#include "IMB_imbuf.hh"

#include <cmath>

namespace blender::ed::sculpt_paint::color {

template<typename Func> inline void to_static_color_type(const CPPType &type, const Func &func)
{
  if (type.is<ColorGeometry4f>()) {
    func(MPropCol());
  }
  else if (type.is<ColorGeometry4b>()) {
    func(MLoopCol());
  }
}

template<typename T> float4 to_float(const T &src);

template<> float4 to_float(const MLoopCol &src)
{
  float4 dst;
  rgba_uchar_to_float(dst, reinterpret_cast<const uchar *>(&src));
  IMB_colormanagement_srgb_to_scene_linear_v3(dst, dst);
  return dst;
}
template<> float4 to_float(const MPropCol &src)
{
  return src.color;
}

template<typename T> void from_float(const float4 &src, T &dst);

template<> void from_float(const float4 &src, MLoopCol &dst)
{
  float4 temp;
  IMB_colormanagement_scene_linear_to_srgb_v3(temp, src);
  temp[3] = src[3];
  rgba_float_to_uchar(reinterpret_cast<uchar *>(&dst), temp);
}
template<> void from_float(const float4 &src, MPropCol &dst)
{
  copy_v4_v4(dst.color, src);
}

template<typename T>
static float4 color_vert_get(const OffsetIndices<int> faces,
                             const Span<int> corner_verts,
                             const GroupedSpan<int> vert_to_face_map,
                             const GSpan color_attribute,
                             const bke::AttrDomain color_domain,
                             const int vert)
{
  const T *colors_typed = static_cast<const T *>(color_attribute.data());
  if (color_domain == bke::AttrDomain::Corner) {
    float4 r_color(0.0f);
    for (const int face : vert_to_face_map[vert]) {
      const int corner = bke::mesh::face_find_corner_from_vert(faces[face], corner_verts, vert);
      r_color += to_float(colors_typed[corner]);
    }
    return r_color / float(vert_to_face_map[vert].size());
  }
  return to_float(colors_typed[vert]);
}

template<typename T>
static void color_vert_set(const OffsetIndices<int> faces,
                           const Span<int> corner_verts,
                           const GroupedSpan<int> vert_to_face_map,
                           const GMutableSpan color_attribute,
                           const bke::AttrDomain color_domain,
                           const int vert,
                           const float4 &color)
{
  if (color_domain == bke::AttrDomain::Corner) {
    for (const int i_face : vert_to_face_map[vert]) {
      const IndexRange face = faces[i_face];
      MutableSpan<T> colors{static_cast<T *>(color_attribute.data()) + face.start(), face.size()};
      Span<int> face_verts = corner_verts.slice(face);

      for (const int i : IndexRange(face.size())) {
        if (face_verts[i] == vert) {
          from_float(color, colors[i]);
        }
      }
    }
  }
  else {
    from_float(color, static_cast<T *>(color_attribute.data())[vert]);
  }
}

float4 color_vert_get(const OffsetIndices<int> faces,
                      const Span<int> corner_verts,
                      const GroupedSpan<int> vert_to_face_map,
                      const GSpan color_attribute,
                      const bke::AttrDomain color_domain,
                      const int vert)
{
  float4 color;
  to_static_color_type(color_attribute.type(), [&](auto dummy) {
    using T = decltype(dummy);
    color = color_vert_get<T>(
        faces, corner_verts, vert_to_face_map, color_attribute, color_domain, vert);
  });
  return color;
}

float4 color_corner_get(const GSpan color_attribute, int corner_index)
{
  float4 r_color;
  to_static_color_type(color_attribute.type(), [&](auto dummy) {
    using T = decltype(dummy);
    const T *colors_typed = static_cast<const T *>(color_attribute.data());
    r_color = to_float(colors_typed[corner_index]);
  });
  return r_color;
}

void color_vert_set(const OffsetIndices<int> faces,
                    const Span<int> corner_verts,
                    const GroupedSpan<int> vert_to_face_map,
                    const bke::AttrDomain color_domain,
                    const int vert,
                    const float4 &color,
                    const GMutableSpan color_attribute)
{
  to_static_color_type(color_attribute.type(), [&](auto dummy) {
    using T = decltype(dummy);
    color_vert_set<T>(
        faces, corner_verts, vert_to_face_map, color_attribute, color_domain, vert, color);
  });
}

void swap_gathered_colors(const Span<int> indices,
                          GMutableSpan color_attribute,
                          MutableSpan<float4> r_colors)
{
  PRF_scope(ProfileCategory::Editor);
  to_static_color_type(color_attribute.type(), [&](auto dummy) {
    using T = decltype(dummy);
    T *colors_typed = static_cast<T *>(color_attribute.data());
    for (const int i : indices.index_range()) {
      T temp = colors_typed[indices[i]];
      from_float(r_colors[i], colors_typed[indices[i]]);
      r_colors[i] = to_float(temp);
    }
  });
}

void gather_colors(const GSpan color_attribute,
                   const Span<int> indices,
                   MutableSpan<float4> r_colors)
{
  to_static_color_type(color_attribute.type(), [&](auto dummy) {
    using T = decltype(dummy);
    const T *colors_typed = static_cast<const T *>(color_attribute.data());
    for (const int i : indices.index_range()) {
      r_colors[i] = to_float(colors_typed[indices[i]]);
    }
  });
}

void gather_colors_vert(const OffsetIndices<int> faces,
                        const Span<int> corner_verts,
                        const GroupedSpan<int> vert_to_face_map,
                        const GSpan color_attribute,
                        const bke::AttrDomain color_domain,
                        const Span<int> verts,
                        const MutableSpan<float4> r_colors)
{
  PRF_scope(ProfileCategory::Editor);
  if (color_domain == bke::AttrDomain::Point) {
    gather_colors(color_attribute, verts, r_colors);
  }
  else {
    to_static_color_type(color_attribute.type(), [&](auto dummy) {
      using T = decltype(dummy);
      for (const int i : verts.index_range()) {
        r_colors[i] = color_vert_get<T>(
            faces, corner_verts, vert_to_face_map, color_attribute, color_domain, verts[i]);
      }
    });
  }
}

float4 color_grids_decode(const ColorGeometry4b &color)
{
  return to_float(reinterpret_cast<const MLoopCol &>(color));
}

ColorGeometry4b color_grids_encode(const float4 &color)
{
  ColorGeometry4b result;
  from_float(color, reinterpret_cast<MLoopCol &>(result));
  return result;
}

float4 color_grids_get(const SubdivCCG &subdiv_ccg, const int vert)
{
  return color_grids_decode(subdiv_ccg.colors[vert]);
}

void gather_colors_grids(const SubdivCCG &subdiv_ccg,
                         const Span<int> grids,
                         const MutableSpan<float4> r_colors)
{
  PRF_scope(ProfileCategory::Editor);
  const int grid_area = subdiv_ccg.grid_area;
  const Span<ColorGeometry4b> colors = subdiv_ccg.colors;
  for (const int i : grids.index_range()) {
    const Span<ColorGeometry4b> src = colors.slice(int64_t(grids[i]) * grid_area, grid_area);
    MutableSpan<float4> dst = r_colors.slice(int64_t(i) * grid_area, grid_area);
    for (const int j : src.index_range()) {
      dst[j] = color_grids_decode(src[j]);
    }
  }
}

void scatter_colors_grids(const Span<float4> colors, SubdivCCG &subdiv_ccg, const Span<int> grids)
{
  PRF_scope(ProfileCategory::Editor);
  const int grid_area = subdiv_ccg.grid_area;
  MutableSpan<ColorGeometry4b> dst_colors = subdiv_ccg.colors;
  for (const int i : grids.index_range()) {
    const Span<float4> src = colors.slice(int64_t(i) * grid_area, grid_area);
    MutableSpan<ColorGeometry4b> dst = dst_colors.slice(int64_t(grids[i]) * grid_area,
                                                        grid_area);
    for (const int j : src.index_range()) {
      dst[j] = color_grids_encode(src[j]);
    }
  }
}

void swap_gathered_colors_grids(SubdivCCG &subdiv_ccg,
                                const Span<int> grids,
                                const MutableSpan<float4> r_colors)
{
  PRF_scope(ProfileCategory::Editor);
  const int grid_area = subdiv_ccg.grid_area;
  MutableSpan<ColorGeometry4b> colors = subdiv_ccg.colors;
  for (const int i : grids.index_range()) {
    MutableSpan<ColorGeometry4b> data = colors.slice(int64_t(grids[i]) * grid_area, grid_area);
    MutableSpan<float4> undo_data = r_colors.slice(int64_t(i) * grid_area, grid_area);
    for (const int j : data.index_range()) {
      const ColorGeometry4b temp = data[j];
      data[j] = color_grids_encode(undo_data[j]);
      undo_data[j] = color_grids_decode(temp);
    }
  }
}

void ensure_grid_colors(Object &object)
{
  SculptSession &ss = *object.runtime->sculpt_session;
  const bke::pbvh::Tree *pbvh = bke::object::pbvh_get(object);
  if (!pbvh || pbvh->type() != bke::pbvh::Type::Grids || ss.subdiv_ccg == nullptr) {
    return;
  }
  SubdivCCG &subdiv_ccg = *ss.subdiv_ccg;
  if (!subdiv_ccg.colors.is_empty()) {
    return;
  }
  subdiv_ccg.colors.reinitialize(subdiv_ccg.positions.size());
  MutableSpan<ColorGeometry4b> colors = subdiv_ccg.colors;

  const Mesh &mesh = *id_cast<const Mesh *>(object.data);
  const bke::GAttributeReader attribute = active_color_attribute(mesh);
  if (!attribute || !ELEM(attribute.domain, bke::AttrDomain::Point, bke::AttrDomain::Corner)) {
    colors.fill(ColorGeometry4b(255, 255, 255, 255));
    return;
  }
  const GVArraySpan base_colors(*attribute);
  const bool corner_domain = attribute.domain == bke::AttrDomain::Corner;
  const Span<int> corner_verts = mesh.corner_verts();
  const OffsetIndices<int> faces = subdiv_ccg.faces;
  const int grid_size = subdiv_ccg.grid_size;
  const int grid_area = subdiv_ccg.grid_area;
  const float grid_size_1_inv = 1.0f / float(grid_size - 1);

  /* Each grid is a bilinear patch between the face center (x = y = 0), the middle of the edge to
   * the next corner (x = n), the corner (x = y = n) and the middle of the edge from the previous
   * corner (y = n). */
  threading::parallel_for(faces.index_range(), 64, [&](const IndexRange range) {
    Vector<float4> corner_colors;
    for (const int face_index : range) {
      const IndexRange face = faces[face_index];
      corner_colors.resize(face.size());
      float4 center(0.0f);
      for (const int i : face.index_range()) {
        const int element = corner_domain ? face[i] : corner_verts[face[i]];
        corner_colors[i] = color_corner_get(base_colors, element);
        center += corner_colors[i];
      }
      center /= float(face.size());
      for (const int i : face.index_range()) {
        const float4 &corner = corner_colors[i];
        const float4 next = (corner + corner_colors[(i + 1) % face.size()]) * 0.5f;
        const float4 prev = (corner + corner_colors[(i + face.size() - 1) % face.size()]) * 0.5f;
        MutableSpan<ColorGeometry4b> grid_colors = colors.slice(int64_t(face[i]) * grid_area,
                                                                grid_area);
        for (const int y : IndexRange(grid_size)) {
          const float v = y * grid_size_1_inv;
          for (const int x : IndexRange(grid_size)) {
            const float u = x * grid_size_1_inv;
            const float4 color = center * ((1.0f - u) * (1.0f - v)) + next * (u * (1.0f - v)) +
                                 prev * ((1.0f - u) * v) + corner * (u * v);
            grid_colors[CCG_grid_xy_to_index(grid_size, x, y)] = color_grids_encode(color);
          }
        }
      }
    }
  });
}

bke::GAttributeReader active_color_attribute(const Mesh &mesh)
{
  const bke::AttributeAccessor attributes = mesh.attributes();
  const StringRef name = mesh.active_color_attribute;
  const bke::GAttributeReader colors = attributes.lookup(name);
  if (!colors) {
    return {};
  }
  const bke::AttrType data_type = bke::cpp_type_to_attribute_type(colors.varray.type());
  if (!bke::mesh::is_color_attribute({colors.domain, data_type})) {
    return {};
  }
  return colors;
}

bke::GSpanAttributeWriter active_color_attribute_for_write(Mesh &mesh)
{
  bke::MutableAttributeAccessor attributes = mesh.attributes_for_write();
  const StringRef name = mesh.active_color_attribute;
  bke::GSpanAttributeWriter colors = attributes.lookup_for_write_span(name);
  if (!colors) {
    return {};
  }
  const bke::AttrType data_type = bke::cpp_type_to_attribute_type(colors.span.type());
  if (!bke::mesh::is_color_attribute({colors.domain, data_type})) {
    colors.finish();
    return {};
  }
  return colors;
}

struct ColorPaintLocalData {
  Vector<float> factors;
  Vector<float> auto_mask;
  Vector<float3> positions;
  Vector<float> distances;
  Vector<float4> colors;
  Vector<float4> new_colors;
  Vector<float4> mix_colors;
  Vector<int> neighbor_offsets;
  Vector<int> neighbor_data;
  Vector<float3> local_positions;
  Vector<float3> normals;
};

static void do_color_smooth_task(const Depsgraph &depsgraph,
                                 const Object &object,
                                 const Span<float3> vert_positions,
                                 const Span<float3> vert_normals,
                                 const OffsetIndices<int> faces,
                                 const Span<int> corner_verts,
                                 const GroupedSpan<int> vert_to_face_map,
                                 const MeshAttributeData &attribute_data,
                                 const Brush &brush,
                                 const bke::pbvh::MeshNode &node,
                                 ColorPaintLocalData &tls,
                                 bke::GSpanAttributeWriter &color_attribute)
{
  const SculptSession &ss = *object.runtime->sculpt_session;
  const StrokeCache &cache = *ss.cache;

  const Span<int> verts = node.verts();

  tls.factors.resize(verts.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(attribute_data.hide_vert, attribute_data.mask, verts, factors);
  filter_region_clip_factors(ss, vert_positions, verts, factors);
  if (brush.flag & BRUSH_FRONTFACE) {
    calc_front_face(cache.view_normal_symm, vert_normals, verts, factors);
  }

  tls.distances.resize(verts.size());
  const MutableSpan<float> distances = tls.distances;
  calc_brush_distances(
      ss, vert_positions, verts, eBrushFalloffShape(brush.falloff_shape), distances);
  filter_distances_with_radius(cache.radius, distances, factors);
  apply_hardness_to_distances(cache, distances);
  calc_brush_strength_factors(cache, brush, distances, factors);

  auto_mask::calc_vert_factors(depsgraph, object, cache.automasking.get(), node, verts, factors);

  calc_brush_texture_factors(PaintMode::Sculpt, ss, brush, vert_positions, verts, factors);
  scale_factors(factors, cache.bstrength);

  tls.colors.resize(verts.size());
  MutableSpan<float4> colors = tls.colors;
  for (const int i : verts.index_range()) {
    colors[i] = color_vert_get(faces,
                               corner_verts,
                               vert_to_face_map,
                               color_attribute.span,
                               color_attribute.domain,
                               verts[i]);
  }

  const GroupedSpan<int> neighbors = calc_vert_neighbors(faces,
                                                         corner_verts,
                                                         vert_to_face_map,
                                                         attribute_data.hide_poly,
                                                         verts,
                                                         tls.neighbor_offsets,
                                                         tls.neighbor_data);

  tls.new_colors.resize(verts.size());
  MutableSpan<float4> new_colors = tls.new_colors;
  smooth::neighbor_color_average(faces,
                                 corner_verts,
                                 vert_to_face_map,
                                 color_attribute.span,
                                 color_attribute.domain,
                                 neighbors,
                                 new_colors);

  for (const int i : colors.index_range()) {
    blend_color_interpolate_float(new_colors[i], colors[i], new_colors[i], factors[i]);
  }

  for (const int i : verts.index_range()) {
    color_vert_set(faces,
                   corner_verts,
                   vert_to_face_map,
                   color_attribute.domain,
                   verts[i],
                   new_colors[i],
                   color_attribute.span);
  }
}

static float4 paint_brush_color(const Paint &paint, const Brush &brush, const SculptSession &ss)
{
  float3 brush_color_rgb = ss.cache->toggle_settings.invert ?
                               BKE_brush_secondary_color_get(&paint, &brush) :
                               BKE_brush_color_get(&paint, &brush);

  const std::optional<BrushColorJitterSettings> color_jitter_settings =
      BKE_brush_color_jitter_get_settings(&paint, &brush);
  if (color_jitter_settings) {
    brush_color_rgb = BKE_paint_randomize_color(*color_jitter_settings,
                                                *ss.cache->initial_hsv_jitter,
                                                ss.cache->stroke_distance,
                                                ss.cache->pressure,
                                                brush_color_rgb);
  }

  float4 brush_color(brush_color_rgb, 1.0f);

  if (brush.flag & BRUSH_USE_GRADIENT) {
    switch (brush.gradient_stroke_mode) {
      case BRUSH_GRADIENT_PRESSURE:
        BKE_colorband_evaluate(brush.gradient, ss.cache->pressure, brush_color);
        break;
      case BRUSH_GRADIENT_SPACING_REPEAT: {
        float coord = fmod(ss.cache->stroke_distance / brush.gradient_spacing, 1.0);
        BKE_colorband_evaluate(brush.gradient, coord, brush_color);
        break;
      }
      case BRUSH_GRADIENT_SPACING_CLAMP: {
        BKE_colorband_evaluate(
            brush.gradient, ss.cache->stroke_distance / brush.gradient_spacing, brush_color);
        break;
      }
    }
  }
  return brush_color;
}

static void mix_paint_colors(const SculptSession &ss,
                             const Brush &brush,
                             const float alpha,
                             const float4 &brush_color,
                             const float4 &wet_mix_sampled_color,
                             const Span<float> factors,
                             const Span<float> auto_mask,
                             const Span<float4> orig_colors,
                             const MutableSpan<float4> color_buffer,
                             const MutableSpan<float4> new_colors)
{
  for (const int i : new_colors.index_range()) {
    /* Brush paint color, brush test falloff and flow. */
    float4 paint_color = brush_color * factors[i] * ss.cache->paint_brush.flow;
    float4 wet_mix_color = wet_mix_sampled_color * factors[i] * ss.cache->paint_brush.flow;

    /* Interpolate with the wet_mix color for wet paint mixing. */
    blend_color_interpolate_float(
        paint_color, paint_color, wet_mix_color, ss.cache->paint_brush.wet_mix);
    blend_color_mix_float(color_buffer[i], color_buffer[i], paint_color);

    /* Final mix over the original color using brush alpha. We apply auto-making again
     * at this point to avoid washing out non-binary masking modes like cavity masking. */
    float automasking = auto_mask.is_empty() ? 1.0f : auto_mask[i];
    const float4 buffer_color = float4(color_buffer[i]) * alpha * automasking;

    IMB_blend_color_float(new_colors[i], orig_colors[i], buffer_color, IMB_BlendMode(brush.blend));
    new_colors[i] = math::clamp(new_colors[i], 0.0f, 1.0f);
  }
}

static void do_paint_brush_task(const Depsgraph &depsgraph,
                                Object &object,
                                const Span<float3> vert_positions,
                                const Span<float3> vert_normals,
                                const OffsetIndices<int> faces,
                                const Span<int> corner_verts,
                                const GroupedSpan<int> vert_to_face_map,
                                const MeshAttributeData &attribute_data,
                                const Paint &paint,
                                const Brush &brush,
                                const float4x4 &mat,
                                const float4 wet_mix_sampled_color,
                                bke::pbvh::MeshNode &node,
                                ColorPaintLocalData &tls,
                                const MutableSpan<float4> mix_colors,
                                bke::GSpanAttributeWriter &color_attribute)
{
  const SculptSession &ss = *object.runtime->sculpt_session;
  const StrokeCache &cache = *ss.cache;

  const float bstrength = fabsf(ss.cache->bstrength);
  const float alpha = BKE_brush_alpha_get(&paint, &brush);

  const Span<int> verts = node.verts();

  tls.factors.resize(verts.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(attribute_data.hide_vert, attribute_data.mask, verts, factors);
  filter_region_clip_factors(ss, vert_positions, verts, factors);
  if (brush.flag & BRUSH_FRONTFACE) {
    calc_front_face(cache.view_normal_symm, vert_normals, verts, factors);
  }

  float radius;

  tls.distances.resize(verts.size());
  const MutableSpan<float> distances = tls.distances;
  if (BKE_brush_has_cube_tip(&brush, PaintMode::Sculpt)) {
    tls.positions.resize(verts.size());
    calc_local_positions(vert_positions,
                         verts,
                         mat,
                         cache.location_symm,
                         cache.view_normal_symm,
                         eBrushFalloffShape(brush.falloff_shape),
                         tls.positions);
    calc_brush_cube_distances<float3>(brush, tls.positions, distances);
    radius = 1.0f;
  }
  else {
    calc_brush_distances(
        ss, vert_positions, verts, eBrushFalloffShape(brush.falloff_shape), distances);
    radius = cache.radius;
  }
  filter_distances_with_radius(radius, distances, factors);
  apply_hardness_to_distances(radius, cache.hardness, distances);
  BKE_brush_calc_curve_factors(eBrushCurvePreset(brush.curve_distance_falloff_preset),
                               brush.curve_distance_falloff,
                               distances,
                               radius,
                               factors);

  MutableSpan<float> auto_mask;
  if (cache.automasking) {
    tls.auto_mask.resize(verts.size());
    auto_mask = tls.auto_mask;
    auto_mask.fill(1.0f);
    auto_mask::calc_vert_factors(depsgraph, object, *cache.automasking, node, verts, auto_mask);
    scale_factors(factors, auto_mask);
  }

  calc_brush_texture_factors(PaintMode::Sculpt, ss, brush, vert_positions, verts, factors);
  scale_factors(factors, bstrength);

  const float density = ss.cache->paint_brush.density;
  if (density < 1.0f) {
    BLI_assert(ss.cache->paint_brush.density_seed);
    const float seed = ss.cache->paint_brush.density_seed.value_or(0.0f);
    for (const int i : verts.index_range()) {
      const float hash_noise = BLI_hash_int_01(seed * 1000 * verts[i]);
      if (hash_noise > density) {
        const float noise = density * hash_noise;
        factors[i] *= noise;
      }
    }
  }

  const float4 brush_color = paint_brush_color(paint, brush, ss);

  const Span<float4> orig_colors = orig_color_data_get_mesh(object, node);

  Array<float4, bke::pbvh::MESH_LEAF_LIMIT> color_buffer(verts.size());
  gather_data_mesh(mix_colors.as_span(), verts, color_buffer.as_mutable_span());

  tls.new_colors.resize(verts.size());
  MutableSpan<float4> new_colors = tls.new_colors;
  for (const int i : verts.index_range()) {
    new_colors[i] = color_vert_get(faces,
                                   corner_verts,
                                   vert_to_face_map,
                                   color_attribute.span,
                                   color_attribute.domain,
                                   verts[i]);
  }

  mix_paint_colors(ss,
                   brush,
                   alpha,
                   brush_color,
                   wet_mix_sampled_color,
                   factors,
                   auto_mask,
                   orig_colors,
                   color_buffer,
                   new_colors);

  scatter_data_mesh(color_buffer.as_span(), verts, mix_colors);

  for (const int i : verts.index_range()) {
    color_vert_set(faces,
                   corner_verts,
                   vert_to_face_map,
                   color_attribute.domain,
                   verts[i],
                   new_colors[i],
                   color_attribute.span);
  }
}

struct SampleWetPaintData {
  int tot_samples;
  float4 color;
};

static void do_sample_wet_paint_task(const Depsgraph &depsgraph,
                                     const Object &object,
                                     const Span<float3> vert_positions,
                                     const Span<float3> vert_normals,
                                     const OffsetIndices<int> faces,
                                     const Span<int> corner_verts,
                                     const GroupedSpan<int> vert_to_face_map,
                                     const MeshAttributeData &attribute_data,
                                     const GSpan color_attribute,
                                     const bke::AttrDomain color_domain,
                                     const Brush &brush,
                                     const bke::pbvh::MeshNode &node,
                                     ColorPaintLocalData &tls,
                                     SampleWetPaintData &swptd)
{
  PRF_scope(ProfileCategory::Editor);
  const SculptSession &ss = *object.runtime->sculpt_session;
  const StrokeCache &cache = *ss.cache;
  const float radius = cache.radius * brush.wet_paint_radius_factor;

  const Span<int> verts = node.verts();

  tls.factors.resize(verts.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(attribute_data.hide_vert, attribute_data.mask, verts, factors);
  if (brush.flag & BRUSH_FRONTFACE) {
    calc_front_face(cache.view_normal_symm, vert_normals, verts, factors);
  }
  auto_mask::calc_vert_factors(depsgraph, object, cache.automasking.get(), node, verts, factors);

  tls.distances.resize(verts.size());
  const MutableSpan<float> distances = tls.distances;
  calc_brush_distances(
      ss, vert_positions, verts, eBrushFalloffShape(brush.falloff_shape), distances);
  filter_distances_with_radius(radius, distances, factors);

  for (const int i : verts.index_range()) {
    if (factors[i] > 0.0f) {
      swptd.color +=
          color_vert_get(
              faces, corner_verts, vert_to_face_map, color_attribute, color_domain, verts[i]) *
          factors[i];
      swptd.tot_samples++;
    }
  }
}

/* -------------------------------------------------------------------- */
/** \name Phantom: Multires Polypaint
 *
 * The brushes on #SubdivCCG::colors, the same as on mesh color attributes.
 * \{ */

static float4 wet_paint_color(StrokeCache &cache, const SampleWetPaintData &swptd)
{
  float4 wet_color(0);
  if (swptd.tot_samples > 0 && is_finite_v4(swptd.color)) {
    wet_color = math::clamp(swptd.color / float(swptd.tot_samples), 0.0f, 1.0f);

    if (cache.first_time) {
      cache.paint_brush.wet_mix_prev_color = wet_color;
    }
    blend_color_interpolate_float(wet_color,
                                  wet_color,
                                  cache.paint_brush.wet_mix_prev_color,
                                  cache.paint_brush.wet_persistence);
    cache.paint_brush.wet_mix_prev_color = math::clamp(wet_color, 0.0f, 1.0f);
  }
  return wet_color;
}

/** Index into the arrays of all grid vertices, for the i-th vertex of a node's grids. */
static int grids_vert_index(const SubdivCCG &subdiv_ccg, const Span<int> grids, const int i)
{
  return grids[i / subdiv_ccg.grid_area] * subdiv_ccg.grid_area + i % subdiv_ccg.grid_area;
}

static void do_paint_brush_task_grids(const Depsgraph &depsgraph,
                                      Object &object,
                                      const Paint &paint,
                                      const Brush &brush,
                                      const float4x4 &mat,
                                      const float4 wet_mix_sampled_color,
                                      const bke::pbvh::GridsNode &node,
                                      ColorPaintLocalData &tls,
                                      const MutableSpan<float4> mix_colors)
{
  SculptSession &ss = *object.runtime->sculpt_session;
  const StrokeCache &cache = *ss.cache;
  SubdivCCG &subdiv_ccg = *ss.subdiv_ccg;

  const float bstrength = fabsf(ss.cache->bstrength);
  const float alpha = BKE_brush_alpha_get(&paint, &brush);

  const Span<int> grids = node.grids();
  const MutableSpan positions = gather_grids_positions(subdiv_ccg, grids, tls.positions);

  tls.factors.resize(positions.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(subdiv_ccg, grids, factors);
  filter_region_clip_factors(ss, positions, factors);
  if (brush.flag & BRUSH_FRONTFACE) {
    calc_front_face(cache.view_normal_symm, subdiv_ccg, grids, factors);
  }

  float radius;

  tls.distances.resize(positions.size());
  const MutableSpan<float> distances = tls.distances;
  if (BKE_brush_has_cube_tip(&brush, PaintMode::Sculpt)) {
    tls.local_positions.resize(positions.size());
    calc_local_positions(positions,
                         mat,
                         cache.location_symm,
                         cache.view_normal_symm,
                         eBrushFalloffShape(brush.falloff_shape),
                         tls.local_positions);
    calc_brush_cube_distances<float3>(brush, tls.local_positions, distances);
    radius = 1.0f;
  }
  else {
    calc_brush_distances(ss, positions, eBrushFalloffShape(brush.falloff_shape), distances);
    radius = cache.radius;
  }
  filter_distances_with_radius(radius, distances, factors);
  apply_hardness_to_distances(radius, cache.hardness, distances);
  BKE_brush_calc_curve_factors(eBrushCurvePreset(brush.curve_distance_falloff_preset),
                               brush.curve_distance_falloff,
                               distances,
                               radius,
                               factors);

  MutableSpan<float> auto_mask;
  if (cache.automasking) {
    tls.auto_mask.resize(positions.size());
    auto_mask = tls.auto_mask;
    auto_mask.fill(1.0f);
    auto_mask::calc_grids_factors(depsgraph, object, *cache.automasking, node, grids, auto_mask);
    scale_factors(factors, auto_mask);
  }

  calc_brush_texture_factors(PaintMode::Sculpt, ss, brush, positions, factors);
  scale_factors(factors, bstrength);

  const float density = ss.cache->paint_brush.density;
  if (density < 1.0f) {
    BLI_assert(ss.cache->paint_brush.density_seed);
    const float seed = ss.cache->paint_brush.density_seed.value_or(0.0f);
    for (const int i : factors.index_range()) {
      const float hash_noise = BLI_hash_int_01(seed * 1000 *
                                               grids_vert_index(subdiv_ccg, grids, i));
      if (hash_noise > density) {
        const float noise = density * hash_noise;
        factors[i] *= noise;
      }
    }
  }

  const float4 brush_color = paint_brush_color(paint, brush, ss);

  const Span<float4> orig_colors = orig_color_data_get_grids(object, node);

  tls.new_colors.resize(positions.size());
  MutableSpan<float4> new_colors = tls.new_colors;
  gather_colors_grids(subdiv_ccg, grids, new_colors);

  mix_paint_colors(ss,
                   brush,
                   alpha,
                   brush_color,
                   wet_mix_sampled_color,
                   factors,
                   auto_mask,
                   orig_colors,
                   mix_colors,
                   new_colors);

  scatter_colors_grids(new_colors, subdiv_ccg, grids);
}

static void do_sample_wet_paint_task_grids(const Depsgraph &depsgraph,
                                           const Object &object,
                                           const Brush &brush,
                                           const bke::pbvh::GridsNode &node,
                                           ColorPaintLocalData &tls,
                                           SampleWetPaintData &swptd)
{
  const SculptSession &ss = *object.runtime->sculpt_session;
  const StrokeCache &cache = *ss.cache;
  const SubdivCCG &subdiv_ccg = *ss.subdiv_ccg;
  const float radius = cache.radius * brush.wet_paint_radius_factor;

  const Span<int> grids = node.grids();
  const MutableSpan positions = gather_grids_positions(subdiv_ccg, grids, tls.positions);

  tls.factors.resize(positions.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(subdiv_ccg, grids, factors);
  if (brush.flag & BRUSH_FRONTFACE) {
    calc_front_face(cache.view_normal_symm, subdiv_ccg, grids, factors);
  }
  auto_mask::calc_grids_factors(depsgraph, object, cache.automasking.get(), node, grids, factors);

  tls.distances.resize(positions.size());
  const MutableSpan<float> distances = tls.distances;
  calc_brush_distances(ss, positions, eBrushFalloffShape(brush.falloff_shape), distances);
  filter_distances_with_radius(radius, distances, factors);

  for (const int i : factors.index_range()) {
    if (factors[i] > 0.0f) {
      swptd.color += color_grids_get(subdiv_ccg, grids_vert_index(subdiv_ccg, grids, i)) *
                     factors[i];
      swptd.tot_samples++;
    }
  }
}

static void do_paint_brush_grids(const Depsgraph &depsgraph,
                                 const Sculpt &sd,
                                 Object &ob,
                                 const Brush &brush,
                                 const float4x4 &mat,
                                 const IndexMask &node_mask)
{
  SculptSession &ss = *ob.runtime->sculpt_session;
  bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(ob);
  MutableSpan<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
  SubdivCCG &subdiv_ccg = *ss.subdiv_ccg;
  ensure_grid_colors(ob);

  /* Wet paint color sampling. */
  float4 wet_color(0);
  if (ss.cache->paint_brush.wet_mix > 0.0f) {
    threading::EnumerableThreadSpecific<ColorPaintLocalData> all_tls;
    const SampleWetPaintData swptd = threading::parallel_reduce(
        node_mask.index_range(),
        1,
        SampleWetPaintData{},
        [&](const IndexRange range, SampleWetPaintData swptd) {
          ColorPaintLocalData &tls = all_tls.local();
          node_mask.slice(range).foreach_index([&](const int i) {
            do_sample_wet_paint_task_grids(depsgraph, ob, brush, nodes[i], tls, swptd);
          });
          return swptd;
        },
        [](const SampleWetPaintData &a, const SampleWetPaintData &b) {
          SampleWetPaintData joined{};
          joined.color = a.color + b.color;
          joined.tot_samples = a.tot_samples + b.tot_samples;
          return joined;
        });
    wet_color = wet_paint_color(*ss.cache, swptd);
  }

  /* The mix buffer of each node, allocated the first time the node is painted. */
  Array<Array<float4>> &mix_colors = ss.cache->paint_brush.mix_colors_grids;
  if (mix_colors.is_empty()) {
    mix_colors.reinitialize(nodes.size());
  }
  node_mask.foreach_index([&](const int i) {
    if (mix_colors[i].is_empty()) {
      mix_colors[i] = Array<float4>(nodes[i].grids().size() * subdiv_ccg.grid_area, float4(0));
    }
  });

  threading::EnumerableThreadSpecific<ColorPaintLocalData> all_tls;
  node_mask.foreach_index(
      [&](const int i) {
        ColorPaintLocalData &tls = all_tls.local();
        do_paint_brush_task_grids(
            depsgraph, ob, sd.paint, brush, mat, wet_color, nodes[i], tls, mix_colors[i]);
      },
      exec_mode::grain_size(1));
  const Mesh &mesh = *id_cast<const Mesh *>(ob.data);
  pbvh.tag_attribute_changed(node_mask, mesh.active_color_attribute);
  subdiv_ccg.dirty.colors = true;
}

static void do_smear_brush_task_grids(const Depsgraph &depsgraph,
                                      Object &object,
                                      const Brush &brush,
                                      const bke::pbvh::GridsNode &node,
                                      ColorPaintLocalData &tls)
{
  SculptSession &ss = *object.runtime->sculpt_session;
  const StrokeCache &cache = *ss.cache;
  SubdivCCG &subdiv_ccg = *ss.subdiv_ccg;
  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
  const Span<float3> all_positions = subdiv_ccg.positions;
  const Span<ColorGeometry4b> prev_colors = ss.cache->paint_brush.prev_colors_grids;
  const float strength = ss.cache->bstrength;

  const Span<int> grids = node.grids();
  const MutableSpan positions = gather_grids_positions(subdiv_ccg, grids, tls.positions);
  tls.normals.resize(positions.size());
  const MutableSpan<float3> normals = tls.normals;
  gather_grids_normals(subdiv_ccg, grids, normals);

  tls.factors.resize(positions.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(subdiv_ccg, grids, factors);
  filter_region_clip_factors(ss, positions, factors);
  if (brush.flag & BRUSH_FRONTFACE) {
    calc_front_face(cache.view_normal_symm, normals, factors);
  }

  tls.distances.resize(positions.size());
  const MutableSpan<float> distances = tls.distances;
  calc_brush_distances(ss, positions, eBrushFalloffShape(brush.falloff_shape), distances);
  filter_distances_with_radius(cache.radius, distances, factors);
  apply_hardness_to_distances(cache, distances);
  calc_brush_strength_factors(cache, brush, distances, factors);

  auto_mask::calc_grids_factors(depsgraph, object, cache.automasking.get(), node, grids, factors);

  calc_brush_texture_factors(PaintMode::Sculpt, ss, brush, positions, factors);
  scale_factors(factors, strength);

  float3 brush_delta;
  if (brush.stroke_method == BRUSH_STROKE_ANCHORED) {
    brush_delta = ss.cache->grab_delta_symm;
  }
  else {
    brush_delta = ss.cache->location_symm - ss.cache->last_location_symm;
  }

  SubdivCCGNeighbors neighbors;
  SubdivCCGNeighbors neighbor_neighbors;

  for (const int i : factors.index_range()) {
    if (factors[i] == 0.0f) {
      continue;
    }
    const int grid = grids[i / key.grid_area];
    const int offset = i % key.grid_area;
    const SubdivCCGCoord coord{grid, short(offset % key.grid_size), short(offset / key.grid_size)};
    const int vert = coord.to_index(key);
    const float3 &no = normals[i];

    float3 current_disp;
    switch (brush.smear_deform_type) {
      case BRUSH_SMEAR_DEFORM_DRAG:
        current_disp = brush_delta;
        break;
      case BRUSH_SMEAR_DEFORM_PINCH:
        current_disp = ss.cache->location_symm - positions[i];
        break;
      case BRUSH_SMEAR_DEFORM_EXPAND:
        current_disp = positions[i] - ss.cache->location_symm;
        break;
    }

    /* Project into vertex plane. */
    current_disp += no * -math::dot(current_disp, no);

    const float3 current_disp_norm = math::normalize(current_disp);

    current_disp = current_disp_norm * strength;

    float4 accum(0);
    float totw = 0.0f;

    /* See #do_smear_brush_task for the nested iteration. */
    BKE_subdiv_ccg_neighbor_coords_get(subdiv_ccg, coord, false, neighbors);
    for (const SubdivCCGCoord neighbor : neighbors.coords) {
      const float3 &nco = all_positions[neighbor.to_index(key)];
      BKE_subdiv_ccg_neighbor_coords_get(subdiv_ccg, neighbor, false, neighbor_neighbors);
      for (const SubdivCCGCoord neighbor_neighbor_coord : neighbor_neighbors.coords) {
        const int neighbor_neighbor = neighbor_neighbor_coord.to_index(key);
        if (neighbor_neighbor == vert) {
          continue;
        }

        float3 vert_disp = all_positions[neighbor_neighbor] - positions[i];

        /* Weight by how close we are to our target distance from vd.co. */
        float w = (1.0f + fabsf(math::length(vert_disp) / strength - 1.0f));

        float len = math::distance(all_positions[neighbor_neighbor], nco);
        if (len > 0.0f) {
          len = strength / len;
        }
        else { /* Coincident point. */
          len = 1.0f;
        }

        w *= len;

        /* Project into vertex plane. */
        vert_disp += no * -math::dot(no, vert_disp);
        const float3 vert_disp_norm = math::normalize(vert_disp);

        if (math::dot(current_disp_norm, vert_disp_norm) >= 0.0f) {
          continue;
        }

        const float4 neighbor_color = color_grids_decode(prev_colors[neighbor_neighbor]);
        float color_interp = -math::dot(current_disp_norm, vert_disp_norm);

        /* Square directional weight to get a somewhat sharper result. */
        w *= color_interp * color_interp;

        accum += neighbor_color * w;
        totw += w;
      }
    }

    if (totw != 0.0f) {
      accum /= totw;
    }

    float4 col;
    blend_color_interpolate_float(col, color_grids_decode(prev_colors[vert]), accum, factors[i]);
    subdiv_ccg.colors[vert] = color_grids_encode(col);
  }
}

static void do_smear_brush_grids(const Depsgraph &depsgraph,
                                 const Brush &brush,
                                 Object &ob,
                                 const IndexMask &node_mask)
{
  SculptSession &ss = *ob.runtime->sculpt_session;
  bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(ob);
  MutableSpan<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
  SubdivCCG &subdiv_ccg = *ss.subdiv_ccg;
  ensure_grid_colors(ob);

  Array<ColorGeometry4b> &prev_colors = ss.cache->paint_brush.prev_colors_grids;
  if (prev_colors.is_empty()) {
    prev_colors = subdiv_ccg.colors;
  }
  const int grid_area = subdiv_ccg.grid_area;
  node_mask.foreach_index(
      [&](const int i) {
        for (const int grid : nodes[i].grids()) {
          const IndexRange range(int64_t(grid) * grid_area, grid_area);
          prev_colors.as_mutable_span().slice(range).copy_from(
              subdiv_ccg.colors.as_span().slice(range));
        }
      },
      exec_mode::grain_size(1));

  threading::EnumerableThreadSpecific<ColorPaintLocalData> all_tls;
  node_mask.foreach_index(
      [&](const int i) {
        ColorPaintLocalData &tls = all_tls.local();
        do_smear_brush_task_grids(depsgraph, ob, brush, nodes[i], tls);
      },
      exec_mode::grain_size(1));

  const Mesh &mesh = *id_cast<const Mesh *>(ob.data);
  pbvh.tag_attribute_changed(node_mask, mesh.active_color_attribute);
  subdiv_ccg.dirty.colors = true;
}

static void do_color_smooth_task_grids(const Depsgraph &depsgraph,
                                       const Object &object,
                                       const Brush &brush,
                                       const bke::pbvh::GridsNode &node,
                                       ColorPaintLocalData &tls)
{
  SculptSession &ss = *object.runtime->sculpt_session;
  const StrokeCache &cache = *ss.cache;
  SubdivCCG &subdiv_ccg = *ss.subdiv_ccg;
  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);

  const Span<int> grids = node.grids();
  const MutableSpan positions = gather_grids_positions(subdiv_ccg, grids, tls.positions);

  tls.factors.resize(positions.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(subdiv_ccg, grids, factors);
  filter_region_clip_factors(ss, positions, factors);
  if (brush.flag & BRUSH_FRONTFACE) {
    calc_front_face(cache.view_normal_symm, subdiv_ccg, grids, factors);
  }

  tls.distances.resize(positions.size());
  const MutableSpan<float> distances = tls.distances;
  calc_brush_distances(ss, positions, eBrushFalloffShape(brush.falloff_shape), distances);
  filter_distances_with_radius(cache.radius, distances, factors);
  apply_hardness_to_distances(cache, distances);
  calc_brush_strength_factors(cache, brush, distances, factors);

  auto_mask::calc_grids_factors(depsgraph, object, cache.automasking.get(), node, grids, factors);

  calc_brush_texture_factors(PaintMode::Sculpt, ss, brush, positions, factors);
  scale_factors(factors, cache.bstrength);

  tls.colors.resize(positions.size());
  MutableSpan<float4> colors = tls.colors;
  gather_colors_grids(subdiv_ccg, grids, colors);

  tls.new_colors.resize(positions.size());
  MutableSpan<float4> new_colors = tls.new_colors;
  SubdivCCGNeighbors neighbors;
  for (const int i : factors.index_range()) {
    if (factors[i] == 0.0f) {
      new_colors[i] = colors[i];
      continue;
    }
    const int offset = i % key.grid_area;
    const SubdivCCGCoord coord{
        grids[i / key.grid_area], short(offset % key.grid_size), short(offset / key.grid_size)};
    BKE_subdiv_ccg_neighbor_coords_get(subdiv_ccg, coord, false, neighbors);
    float4 sum(0.0f);
    for (const SubdivCCGCoord neighbor : neighbors.coords) {
      sum += color_grids_get(subdiv_ccg, neighbor.to_index(key));
    }
    new_colors[i] = math::safe_divide(sum, float(neighbors.coords.size()));
    blend_color_interpolate_float(new_colors[i], colors[i], new_colors[i], factors[i]);
  }

  scatter_colors_grids(new_colors, subdiv_ccg, grids);
}

static void do_blur_brush_grids(const Depsgraph &depsgraph,
                                const Brush &brush,
                                Object &ob,
                                const IndexMask &node_mask)
{
  SculptSession &ss = *ob.runtime->sculpt_session;
  bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(ob);
  MutableSpan<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
  ensure_grid_colors(ob);

  threading::EnumerableThreadSpecific<ColorPaintLocalData> all_tls;
  node_mask.foreach_index(
      [&](const int i) {
        ColorPaintLocalData &tls = all_tls.local();
        do_color_smooth_task_grids(depsgraph, ob, brush, nodes[i], tls);
      },
      exec_mode::grain_size(1));

  const Mesh &mesh = *id_cast<const Mesh *>(ob.data);
  pbvh.tag_attribute_changed(node_mask, mesh.active_color_attribute);
  ss.subdiv_ccg->dirty.colors = true;
}

/** \} */

void do_paint_brush(const Depsgraph &depsgraph,
                    const Sculpt &sd,
                    Object &ob,
                    const IndexMask &node_mask)
{
  PRF_scope(ProfileCategory::Editor);

  const Brush &brush = *BKE_paint_brush_for_read(&sd.paint);
  SculptSession &ss = *ob.runtime->sculpt_session;
  bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(ob);

  if (!ss.cache->paint_brush.density_seed) {
    ss.cache->paint_brush.density_seed = BLI_hash_int_01(ss.cache->location_symm[0] * 1000);
  }

  if (stroke_is_first_brush_step_of_symmetry_pass(*ss.cache)) {
    return;
  }

  float4x4 mat;

  /* If the brush is round the tip does not need to be aligned to the surface, so this saves a
   * whole iteration over the affected nodes. */
  if (BKE_brush_has_cube_tip(&brush, PaintMode::Sculpt)) {
    mat = cube_tip_init(sd, ob, brush);

    if (math::is_zero(mat)) {
      return;
    }
  }

  if (pbvh.type() == bke::pbvh::Type::Grids) {
    do_paint_brush_grids(depsgraph, sd, ob, brush, mat, node_mask);
    return;
  }
  MutableSpan<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();

  Mesh &mesh = *id_cast<Mesh *>(ob.data);
  const Span<float3> vert_positions = bke::pbvh::vert_positions_eval(depsgraph, ob);
  const Span<float3> vert_normals = bke::pbvh::vert_normals_eval(depsgraph, ob);
  const OffsetIndices<int> faces = mesh.faces();
  const Span<int> corner_verts = mesh.corner_verts();
  const GroupedSpan<int> vert_to_face_map = mesh.vert_to_face_map();
  const MeshAttributeData attribute_data(mesh);
  bke::GSpanAttributeWriter color_attribute = active_color_attribute_for_write(mesh);
  if (!color_attribute) {
    return;
  }

  /* Regular Paint mode. */

  /* Wet paint color sampling. */
  float4 wet_color(0);
  if (ss.cache->paint_brush.wet_mix > 0.0f) {
    threading::EnumerableThreadSpecific<ColorPaintLocalData> all_tls;
    const SampleWetPaintData swptd = threading::parallel_reduce(
        node_mask.index_range(),
        1,
        SampleWetPaintData{},
        [&](const IndexRange range, SampleWetPaintData swptd) {
          ColorPaintLocalData &tls = all_tls.local();
          node_mask.slice(range).foreach_index([&](const int i) {
            do_sample_wet_paint_task(depsgraph,
                                     ob,
                                     vert_positions,
                                     vert_normals,
                                     faces,
                                     corner_verts,
                                     vert_to_face_map,
                                     attribute_data,
                                     color_attribute.span,
                                     color_attribute.domain,
                                     brush,
                                     nodes[i],
                                     tls,
                                     swptd);
          });
          return swptd;
        },
        [](const SampleWetPaintData &a, const SampleWetPaintData &b) {
          SampleWetPaintData joined{};
          joined.color = a.color + b.color;
          joined.tot_samples = a.tot_samples + b.tot_samples;
          return joined;
        });

    wet_color = wet_paint_color(*ss.cache, swptd);
  }

  if (ss.cache->paint_brush.mix_colors.is_empty()) {
    ss.cache->paint_brush.mix_colors = Array<float4>(mesh.verts_num, float4(0));
  }

  threading::EnumerableThreadSpecific<ColorPaintLocalData> all_tls;
  node_mask.foreach_index(
      [&](const int i) {
        ColorPaintLocalData &tls = all_tls.local();
        do_paint_brush_task(depsgraph,
                            ob,
                            vert_positions,
                            vert_normals,
                            faces,
                            corner_verts,
                            vert_to_face_map,
                            attribute_data,
                            sd.paint,
                            brush,
                            mat,
                            wet_color,
                            nodes[i],
                            tls,
                            ss.cache->paint_brush.mix_colors,
                            color_attribute);
      },
      exec_mode::grain_size(1));
  pbvh.tag_attribute_changed(node_mask, mesh.active_color_attribute);
  color_attribute.finish();
}

static void do_smear_brush_task(const Depsgraph &depsgraph,
                                Object &object,
                                const Span<float3> vert_positions,
                                const Span<float3> vert_normals,
                                const OffsetIndices<int> faces,
                                const Span<int> corner_verts,
                                const GroupedSpan<int> vert_to_face_map,
                                const MeshAttributeData &attribute_data,
                                const Brush &brush,
                                bke::pbvh::MeshNode &node,
                                ColorPaintLocalData &tls,
                                bke::GSpanAttributeWriter &color_attribute)
{
  const SculptSession &ss = *object.runtime->sculpt_session;
  const StrokeCache &cache = *ss.cache;
  const float strength = ss.cache->bstrength;

  const Span<int> verts = node.verts();

  tls.factors.resize(verts.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(attribute_data.hide_vert, attribute_data.mask, verts, factors);
  filter_region_clip_factors(ss, vert_positions, verts, factors);
  if (brush.flag & BRUSH_FRONTFACE) {
    calc_front_face(cache.view_normal_symm, vert_normals, verts, factors);
  }

  tls.distances.resize(verts.size());
  const MutableSpan<float> distances = tls.distances;
  calc_brush_distances(
      ss, vert_positions, verts, eBrushFalloffShape(brush.falloff_shape), distances);
  filter_distances_with_radius(cache.radius, distances, factors);
  apply_hardness_to_distances(cache, distances);
  calc_brush_strength_factors(cache, brush, distances, factors);

  auto_mask::calc_vert_factors(depsgraph, object, cache.automasking.get(), node, verts, factors);

  calc_brush_texture_factors(PaintMode::Sculpt, ss, brush, vert_positions, verts, factors);
  scale_factors(factors, strength);

  float3 brush_delta;
  if (brush.stroke_method == BRUSH_STROKE_ANCHORED) {
    brush_delta = ss.cache->grab_delta_symm;
  }
  else {
    brush_delta = ss.cache->location_symm - ss.cache->last_location_symm;
  }

  Vector<int> neighbors;
  Vector<int> neighbor_neighbors;

  for (const int i : verts.index_range()) {
    if (factors[i] == 0.0f) {
      continue;
    }
    const int vert = verts[i];
    const float3 &no = vert_normals[vert];

    float3 current_disp;
    switch (brush.smear_deform_type) {
      case BRUSH_SMEAR_DEFORM_DRAG:
        current_disp = brush_delta;
        break;
      case BRUSH_SMEAR_DEFORM_PINCH:
        current_disp = ss.cache->location_symm - vert_positions[vert];
        break;
      case BRUSH_SMEAR_DEFORM_EXPAND:
        current_disp = vert_positions[vert] - ss.cache->location_symm;
        break;
    }

    /* Project into vertex plane. */
    current_disp += no * -math::dot(current_disp, no);

    const float3 current_disp_norm = math::normalize(current_disp);

    current_disp = current_disp_norm * strength;

    float4 accum(0);
    float totw = 0.0f;

    /*
     * NOTE: we have to do a nested iteration here to avoid
     * blocky artifacts on quad topologies.  The runtime cost
     * is not as bad as it seems due to neighbor iteration
     * in the sculpt code being cache bound; once the data is in
     * the cache iterating over it a few more times is not terribly
     * costly.
     */

    for (const int neighbor : vert_neighbors_get_mesh(
             faces, corner_verts, vert_to_face_map, attribute_data.hide_poly, vert, neighbors))
    {
      const float3 &nco = vert_positions[neighbor];
      for (const int neighbor_neighbor : vert_neighbors_get_mesh(faces,
                                                                 corner_verts,
                                                                 vert_to_face_map,
                                                                 attribute_data.hide_poly,
                                                                 neighbor,
                                                                 neighbor_neighbors))
      {
        if (neighbor_neighbor == vert) {
          continue;
        }

        float3 vert_disp = vert_positions[neighbor_neighbor] - vert_positions[vert];

        /* Weight by how close we are to our target distance from vd.co. */
        float w = (1.0f + fabsf(math::length(vert_disp) / strength - 1.0f));

        /* TODO: use cotangents (or at least face areas) here. */
        float len = math::distance(vert_positions[neighbor_neighbor], nco);
        if (len > 0.0f) {
          len = strength / len;
        }
        else { /* Coincident point. */
          len = 1.0f;
        }

        /* Multiply weight with edge lengths (in the future this will be
         * cotangent weights or face areas). */
        w *= len;

        /* Build directional weight. */

        /* Project into vertex plane. */
        vert_disp += no * -math::dot(no, vert_disp);
        const float3 vert_disp_norm = math::normalize(vert_disp);

        if (math::dot(current_disp_norm, vert_disp_norm) >= 0.0f) {
          continue;
        }

        const float4 &neighbor_color = ss.cache->paint_brush.prev_colors[neighbor_neighbor];
        float color_interp = -math::dot(current_disp_norm, vert_disp_norm);

        /* Square directional weight to get a somewhat sharper result. */
        w *= color_interp * color_interp;

        accum += neighbor_color * w;
        totw += w;
      }
    }

    if (totw != 0.0f) {
      accum /= totw;
    }

    float4 col = color_vert_get(
        faces, corner_verts, vert_to_face_map, color_attribute.span, color_attribute.domain, vert);
    blend_color_interpolate_float(col, ss.cache->paint_brush.prev_colors[vert], accum, factors[i]);
    color_vert_set(faces,
                   corner_verts,
                   vert_to_face_map,
                   color_attribute.domain,
                   vert,
                   col,
                   color_attribute.span);
  }
}

void do_smear_brush(const Depsgraph &depsgraph,
                    const Sculpt &sd,
                    Object &ob,
                    const IndexMask &node_mask)
{
  PRF_scope(ProfileCategory::Editor);
  const Brush &brush = *BKE_paint_brush_for_read(&sd.paint);
  SculptSession &ss = *ob.runtime->sculpt_session;
  bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(ob);

  Mesh &mesh = *id_cast<Mesh *>(ob.data);
  if (ss.cache->bstrength == 0.0f) {
    return;
  }

  if (pbvh.type() == bke::pbvh::Type::Grids) {
    do_smear_brush_grids(depsgraph, brush, ob, node_mask);
    return;
  }
  MutableSpan<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();

  const OffsetIndices<int> faces = mesh.faces();
  const Span<int> corner_verts = mesh.corner_verts();
  const GroupedSpan<int> vert_to_face_map = mesh.vert_to_face_map();
  const Span<float3> vert_positions = bke::pbvh::vert_positions_eval(depsgraph, ob);
  const Span<float3> vert_normals = bke::pbvh::vert_normals_eval(depsgraph, ob);
  const MeshAttributeData attribute_data(mesh);

  bke::GSpanAttributeWriter color_attribute = active_color_attribute_for_write(mesh);
  if (!color_attribute) {
    return;
  }

  if (ss.cache->paint_brush.prev_colors.is_empty()) {
    ss.cache->paint_brush.prev_colors = Array<float4>(mesh.verts_num);
    threading::parallel_for(IndexRange(mesh.verts_num), 1024, [&](const IndexRange range) {
      for (const int vert : range) {
        ss.cache->paint_brush.prev_colors[vert] = color_vert_get(faces,
                                                                 corner_verts,
                                                                 vert_to_face_map,
                                                                 color_attribute.span,
                                                                 color_attribute.domain,
                                                                 vert);
      }
    });
  }

  /* Smear mode. */
  node_mask.foreach_index(
      [&](const int i) {
        for (const int vert : nodes[i].verts()) {
          ss.cache->paint_brush.prev_colors[vert] = color_vert_get(faces,
                                                                   corner_verts,
                                                                   vert_to_face_map,
                                                                   color_attribute.span,
                                                                   color_attribute.domain,
                                                                   vert);
        }
      },
      exec_mode::grain_size(1));
  threading::EnumerableThreadSpecific<ColorPaintLocalData> all_tls;
  node_mask.foreach_index(
      [&](const int i) {
        ColorPaintLocalData &tls = all_tls.local();
        do_smear_brush_task(depsgraph,
                            ob,
                            vert_positions,
                            vert_normals,
                            faces,
                            corner_verts,
                            vert_to_face_map,
                            attribute_data,
                            brush,
                            nodes[i],
                            tls,
                            color_attribute);
      },
      exec_mode::grain_size(1));

  pbvh.tag_attribute_changed(node_mask, mesh.active_color_attribute);
  color_attribute.finish();
}

void do_blur_brush(const Depsgraph &depsgraph,
                   const Sculpt &sd,
                   Object &ob,
                   const IndexMask &node_mask)
{
  PRF_scope(ProfileCategory::Editor);
  const Brush &brush = *BKE_paint_brush_for_read(&sd.paint);
  SculptSession &ss = *ob.runtime->sculpt_session;
  bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(ob);

  if (!ss.cache->paint_brush.density_seed) {
    ss.cache->paint_brush.density_seed = BLI_hash_int_01(ss.cache->location_symm[0] * 1000);
  }

  if (stroke_is_first_brush_step_of_symmetry_pass(*ss.cache)) {
    return;
  }

  if (pbvh.type() == bke::pbvh::Type::Grids) {
    do_blur_brush_grids(depsgraph, brush, ob, node_mask);
    return;
  }
  MutableSpan<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();

  Mesh &mesh = *id_cast<Mesh *>(ob.data);
  const Span<float3> vert_positions = bke::pbvh::vert_positions_eval(depsgraph, ob);
  const Span<float3> vert_normals = bke::pbvh::vert_normals_eval(depsgraph, ob);
  const OffsetIndices<int> faces = mesh.faces();
  const Span<int> corner_verts = mesh.corner_verts();
  const GroupedSpan<int> vert_to_face_map = mesh.vert_to_face_map();
  const MeshAttributeData attribute_data(mesh);
  bke::GSpanAttributeWriter color_attribute = active_color_attribute_for_write(mesh);
  if (!color_attribute) {
    return;
  }

  threading::EnumerableThreadSpecific<ColorPaintLocalData> all_tls;
  node_mask.foreach_index(
      [&](const int i) {
        ColorPaintLocalData &tls = all_tls.local();
        do_color_smooth_task(depsgraph,
                             ob,
                             vert_positions,
                             vert_normals,
                             faces,
                             corner_verts,
                             vert_to_face_map,
                             attribute_data,
                             brush,
                             nodes[i],
                             tls,
                             color_attribute);
      },
      exec_mode::grain_size(1));
  pbvh.tag_attribute_changed(node_mask, mesh.active_color_attribute);
  color_attribute.finish();
}

}  // namespace blender::ed::sculpt_paint::color
