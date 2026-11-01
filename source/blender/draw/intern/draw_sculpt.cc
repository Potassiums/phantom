/* SPDX-FileCopyrightText: 2023 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup draw
 */

#include "draw_sculpt.hh"

#include "DNA_mesh_types.h"
#include "DNA_scene_types.h"
#include "draw_attributes.hh"
#include "draw_context_private.hh"
#include "draw_view.hh"

#include "BKE_attribute.hh"
#include "BKE_attribute_legacy_convert.hh"
#include "BKE_customdata.hh"
#include "BKE_material.hh"
#include "BKE_object.hh"
#include "BKE_object_types.hh"
#include "BKE_paint.hh"
#include "BKE_subdiv_ccg.hh"

#include "BLI_math_matrix.hh"

#include "DEG_depsgraph_query.hh"
#include "DNA_view3d_types.h"

#include "bmesh_class.hh"

#include "ED_view3d.hh"

#include "DRW_pbvh.hh"
#include "DRW_render.hh"

namespace blender::draw {

float3 SculptBatch::debug_color()
{
  static float3 colors[9] = {
      {1.0f, 0.2f, 0.2f},
      {0.2f, 1.0f, 0.2f},
      {0.2f, 0.2f, 1.0f},
      {1.0f, 1.0f, 0.2f},
      {0.2f, 1.0f, 1.0f},
      {1.0f, 0.2f, 1.0f},
      {1.0f, 0.7f, 0.2f},
      {0.2f, 1.0f, 0.7f},
      {0.7f, 0.2f, 1.0f},
  };

  return colors[debug_index % 9];
}

/**
 * Phantom: the visible nodes minus those whose triangles all face away from the viewer. On a
 * closed mesh wound outwards and seen from outside, those are behind front faces, so the picture
 * stays the same. Not done where back faces can show or matter: X-ray, clipping regions, a near
 * plane cutting the mesh, selection.
 */
static IndexMask cull_back_facing_nodes(const Object &ob,
                                        const bke::pbvh::Tree &pbvh,
                                        pbvh::DrawCache &draw_data,
                                        const IndexMask &visible_nodes,
                                        const float4 &near_plane,
                                        IndexMaskMemory &memory)
{
  const DRWContext *drwctx = DRW_context_get();
  const View3D *v3d = drwctx->v3d;
  const RegionView3D *rv3d = drwctx->rv3d;
  /* Debug value 7371 turns the culling off, to compare. */
  if (!v3d || !rv3d || XRAY_ENABLED(v3d) || RV3D_CLIPPING_ENABLED(v3d, rv3d) ||
      drwctx->is_select() || G.debug_value == 7371)
  {
    return visible_nodes;
  }
  /* The whole mesh must be in front of the near plane, or its inside shows through the cut. */
  const Bounds<float3> bounds = bke::pbvh::bounds_get(pbvh);
  float3 nearest;
  for (const int axis : IndexRange(3)) {
    nearest[axis] = near_plane[axis] < 0.0f ? bounds.max[axis] : bounds.min[axis];
  }
  if (math::dot(near_plane.xyz(), nearest) + near_plane.w <= 0.0f) {
    return visible_nodes;
  }
  /* While sculpting, the nodes the brush changes face the viewer: leave their cones for later. */
  const bool painting = rv3d->rflag & RV3D_PAINTING;
  const Span<pbvh::NormalCone> cones = draw_data.ensure_normal_cones(
      ob, visible_nodes, painting);
  if (cones.is_empty()) {
    return visible_nodes;
  }

  const View &view = View::default_get();
  const float4x4 &world_to_object = ob.world_to_object();
  const bool is_persp = view.is_persp();
  const float3 eye = math::transform_point(world_to_object, view.location());
  const float3 view_dir = math::normalize(
      math::transform_direction(world_to_object, -view.forward()));
  auto node_bounds = [&](const int i) -> const Bounds<float3> & {
    switch (pbvh.type()) {
      case bke::pbvh::Type::Mesh:
        return pbvh.nodes<bke::pbvh::MeshNode>()[i].bounds();
      case bke::pbvh::Type::Grids:
        return pbvh.nodes<bke::pbvh::GridsNode>()[i].bounds();
      case bke::pbvh::Type::BMesh:
        break;
    }
    return pbvh.nodes<bke::pbvh::BMeshNode>()[i].bounds();
  };
  return IndexMask::from_predicate(visible_nodes, memory, [&](const int i) {
    const pbvh::NormalCone &cone = cones[i];
    if (cone.sin_angle > 1.0f) {
      return true;
    }
    if (!is_persp) {
      /* Every triangle faces away when the view direction is within 90 degrees minus the cone's
       * angle of the axis. */
      return math::dot(cone.axis, view_dir) <= cone.sin_angle;
    }
    /* The same for every direction from the eye to a point of the node's bounding sphere. */
    const Bounds<float3> &b = node_bounds(i);
    const float3 center = math::midpoint(b.min, b.max);
    const float radius = math::distance(b.min, b.max) * 0.5f;
    const float3 to_center = center - eye;
    const float distance = math::length(to_center);
    return math::dot(cone.axis, to_center) <=
           cone.sin_angle * distance + radius * (1.0f + cone.sin_angle);
  });
}

static Vector<SculptBatch> sculpt_batches_get_ex(const Object *ob,
                                                 const bool use_wire,
                                                 const Span<pbvh::AttributeRequest> attrs,
                                                 const bool multi_draw = false)
{
  /* pbvh::Tree should always exist for non-empty meshes, created by depsgraph eval. */
  bke::pbvh::Tree *pbvh = ob->runtime->sculpt_session ?
                              const_cast<bke::pbvh::Tree *>(bke::object::pbvh_get(*ob)) :
                              nullptr;
  if (!pbvh) {
    return {};
  }

  /* TODO(Miguel Pozo): Don't use global context. */
  const DRWContext *drwctx = DRW_context_get();
  RegionView3D *rv3d = drwctx->rv3d;
  const bool navigating = rv3d && (rv3d->rflag & RV3D_NAVIGATING);

  Paint *paint = nullptr;
  if (drwctx->evil_C != nullptr) {
    paint = BKE_paint_get_active_from_context(drwctx->evil_C);
  }

  /* TODO: take into account partial redraw for clipping planes. */
  /* Frustum planes to show only visible pbvh::Tree nodes. */
  std::array<float4, 6> draw_frustum_planes = View::default_get().frustum_planes_get();
  /* Transform clipping planes to object space. Transforming a plane with a
   * 4x4 matrix is done by multiplying with the transpose inverse.
   * The inverse cancels out here since we transform by inverse(obmat). */
  float4x4 tmat = math::transpose(ob->object_to_world());
  for (int i : IndexRange(draw_frustum_planes.size())) {
    draw_frustum_planes[i] = tmat * draw_frustum_planes[i];
  }

  /* Fast mode to show low poly multires while navigating. */
  bool fast_mode = false;
  if (paint && (paint->flags & PAINT_FAST_NAVIGATE)) {
    fast_mode = navigating;
    /* Phantom: meshes without multires have a coarse version too, used when opted into. */
    if (pbvh->type() == bke::pbvh::Type::Mesh && !(paint->flags & PAINT_FAST_NAVIGATE_MESH)) {
      fast_mode = false;
    }
  }

  /* Update draw buffers only for visible nodes while painting.
   * But do update them otherwise so navigating stays smooth. */
  bool update_only_visible = rv3d && !(rv3d->rflag & RV3D_PAINTING);
  if (paint && (paint->flags & PAINT_SCULPT_DELAY_UPDATES)) {
    update_only_visible = true;
  }

  bke::pbvh::update_normals_from_eval(*const_cast<Object *>(ob), *pbvh);

  pbvh::DrawCache &draw_data = pbvh::ensure_draw_data(pbvh->draw_data);

  IndexMaskMemory memory;
  const IndexMask visible_nodes = bke::pbvh::search_nodes(
      *pbvh, memory, [&](const bke::pbvh::Node &node) {
        return !BKE_pbvh_node_fully_hidden_get(node) &&
               bke::pbvh::node_frustum_contain_aabb(node, draw_frustum_planes);
      });

  const IndexMask drawn_nodes = cull_back_facing_nodes(
      *ob, *pbvh, draw_data, visible_nodes, draw_frustum_planes[4], memory);

  /* In the future we expect that update_only_visible nodes will be removed to support GPU driven
   * pipelines better. Only visible nodes is an CPU based optimization but does not provide the GPU
   * with the actual data it needs.
   *
   * Phantom: while navigating with the coarse geometry, only the nodes drawn need it, so it is
   * built (#DrawCache) for the nodes facing the view, and for others as they come into view. */
  const IndexMask nodes_to_update = fast_mode ? drawn_nodes :
                                    update_only_visible ?
                                                visible_nodes :
                                                bke::pbvh::all_leaf_nodes(*pbvh, memory);

  Span<Batch *> batches;
  /* Phantom: with multi-draw, one batch for all nodes and each node's range of its indices. */
  Batch *shared_batch = nullptr;
  Vector<IndexRange> node_indices;
  if (multi_draw) {
    shared_batch = use_wire ?
                       draw_data.ensure_lines_shared_batch(
                           *ob, {{}, fast_mode}, nodes_to_update, node_indices) :
                       draw_data.ensure_tris_shared_batch(
                           *ob, {attrs, fast_mode}, nodes_to_update, node_indices);
    if (shared_batch == nullptr) {
      return {};
    }
  }
  else if (use_wire) {
    batches = draw_data.ensure_lines_batches(*ob, {{}, fast_mode}, nodes_to_update);
  }
  else {
    batches = draw_data.ensure_tris_batches(*ob, {attrs, fast_mode}, nodes_to_update);
  }

  const Span<int> material_indices = draw_data.ensure_material_indices(*ob);

  const int max_material = std::max(0, BKE_object_material_count_eval(ob) - 1);
  Vector<SculptBatch> result_batches;
  result_batches.reserve(drawn_nodes.size());
  drawn_nodes.foreach_index([&](const int i, const int pos) {
    SculptBatch batch = {};
    if (multi_draw) {
      if (node_indices[i].is_empty()) {
        return;
      }
      batch.batch = shared_batch;
      batch.indices = node_indices[i];
      batch.use_indices = true;
    }
    else {
      /* Skip nodes without any visible geometry, for example when all faces are hidden but some
       * vertices are not. */
      if (batches[i] == nullptr || batches[i]->gpu_batch()->elem_() == nullptr) {
        return;
      }
      batch.batch = batches[i];
    }
    batch.material_slot = material_indices.is_empty() ?
                              0 :
                              std::clamp(material_indices[i], 0, max_material);
    batch.debug_index = pos;
    result_batches.append(batch);
  });

  return result_batches;
}

Vector<SculptBatch> sculpt_batches_get(const Object *ob, SculptBatchFeature features)
{
  Vector<pbvh::AttributeRequest, 16> attrs;

  attrs.append(pbvh::CustomRequest::Position);
  if (!(features & SCULPT_BATCH_NO_NORMAL)) {
    attrs.append(pbvh::CustomRequest::Normal);
  }
  if (features & SCULPT_BATCH_MASK) {
    attrs.append(pbvh::CustomRequest::Mask);
  }
  if (features & SCULPT_BATCH_FACE_SET) {
    attrs.append(pbvh::CustomRequest::FaceSet);
  }

  const Mesh *mesh = BKE_object_get_original_mesh(ob);
  if (features & SCULPT_BATCH_VERTEX_COLOR) {
    if (const char *name = mesh->active_color_attribute) {
      attrs.append(pbvh::GenericRequest(name));
    }
  }

  if (features & SCULPT_BATCH_UV) {
    const StringRef uv_name = mesh->active_uv_map_name();
    if (!uv_name.is_empty()) {
      attrs.append(pbvh::GenericRequest(uv_name));
    }
  }

  return sculpt_batches_get_ex(
      ob, features & SCULPT_BATCH_WIREFRAME, attrs, features & SCULPT_BATCH_MULTI_DRAW);
}

Vector<SculptBatch> sculpt_batches_per_material_get(const Object *ob,
                                                    Span<const GPUMaterial *> materials)
{
  BLI_assert(ob->type == OB_MESH);
  const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(*ob);

  VectorSet<std::string> draw_attrs;
  DRW_MeshCDMask cd_needed;
  DRW_mesh_get_attributes(*ob, mesh, materials, &draw_attrs, &cd_needed);

  Vector<pbvh::AttributeRequest, 16> attrs;

  attrs.append(pbvh::CustomRequest::Position);
  attrs.append(pbvh::CustomRequest::Normal);

  for (const StringRef name : draw_attrs) {
    attrs.append(pbvh::GenericRequest(name));
  }

  for (const StringRef name : cd_needed.uv) {
    attrs.append(pbvh::GenericRequest(name));
  }

  return sculpt_batches_get_ex(ob, false, attrs);
}

bool sculpt_tint_in_workbench(const View3D *v3d)
{
  /* Solid shading is always drawn by Workbench. */
  return v3d && v3d->shading.type == OB_SOLID && G.debug_value != 7372;
}

bool sculpt_has_mask(const Object &object)
{
  const SculptSession *ss = object.runtime->sculpt_session;
  const bke::pbvh::Tree *pbvh = bke::object::pbvh_get(object);
  if (!ss || !pbvh) {
    return false;
  }
  /* The original data: sculpt mode skips depsgraph updates of the evaluated data. */
  const Object &object_orig = *DEG_get_original(&object);
  switch (pbvh->type()) {
    case bke::pbvh::Type::Mesh:
      return DRW_object_get_data_for_drawing<Mesh>(object_orig).attributes().contains(
          ".sculpt_mask");
    case bke::pbvh::Type::Grids:
      return !ss->subdiv_ccg->masks.is_empty();
    case bke::pbvh::Type::BMesh:
      return CustomData_has_layer_named(&ss->bm->vdata, CD_PROP_FLOAT, ".sculpt_mask");
  }
  return false;
}

}  // namespace blender::draw
