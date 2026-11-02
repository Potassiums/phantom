/* SPDX-FileCopyrightText: 2020 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup edsculpt
 */

#include "MEM_guardedalloc.h"

#include "BLI_array_utils.hh"
#include "BLI_enumerable_thread_specific.hh"
#include "BLI_listbase.hh"
#include "BLI_math_matrix.hh"
#include "BLI_math_matrix_c.hh"
#include "BLI_math_rotation_c.hh"
#include "BLI_math_vector_c.hh"
#include "BLI_math_vector_types.hh"
#include "BLI_span.hh"

#include "BLT_translation.hh"

#include "BKE_attribute.hh"
#include "BKE_brush.hh"
#include "BKE_context.hh"
#include "BKE_kelvinlet.h"
#include "BKE_layer.hh"
#include "BKE_lib_id.hh"
#include "BKE_mesh.h"
#include "BKE_object.hh"
#include "BKE_mesh.hh"
#include "BKE_object_types.hh"
#include "BKE_paint.hh"
#include "BKE_paint_bvh.hh"
#include "BKE_paint_types.hh"
#include "BKE_subdiv_ccg.hh"
#include "BKE_wm_runtime.hh"

#include "DEG_depsgraph.hh"

#include "WM_api.hh"
#include "WM_types.hh"

#include "ED_screen.hh"
#include "ED_sculpt.hh"
#include "ED_undo.hh"
#include "ED_view3d.hh"

#include "../paint_intern.hh"
#include "mesh_brush_common.hh"
#include "paint_mask.hh"
#include "sculpt_filter.hh"
#include "sculpt_intern.hh"
#include "sculpt_undo.hh"

#include "RNA_access.hh"
#include "RNA_define.hh"

#include "bmesh.hh"

#include <cmath>
#include <cstdlib>

namespace blender::ed::sculpt_paint {

/* Phantom: the gizmo's options, see #phantom_gizmo_options_set. */
static struct {
  bool sticky = false;
  bool all_visible = false;
  bool unlocked = false;
} gizmo_options;

void phantom_gizmo_options_set(const bool sticky, const bool all_visible, const bool unlocked)
{
  gizmo_options.sticky = sticky;
  gizmo_options.all_visible = all_visible;
  gizmo_options.unlocked = unlocked;
}

/** Whether the transform moves only the pivot, not the mesh. */
static bool pivot_only(const ToolSettings &ts, const SculptSession &ss)
{
  return (ts.transform_flag & SCE_XFORM_SCULPT_PIVOT) || ss.phantom_pivot_only;
}

/**
 * Phantom: the other visible meshes, which the gizmo moves along with this one while
 * "all visible" is on. Objects whose parent moves too are left to follow it.
 */
static void collect_other_objects(bContext *C, Object &active)
{
  SculptSession &ss = *active.runtime->sculpt_session;
  ss.phantom_others.clear();
  Main *bmain = CTX_data_main(C);
  const Scene *scene = CTX_data_scene(C);
  ViewLayer *view_layer = CTX_data_view_layer(C);
  const View3D *v3d = CTX_wm_view3d(C);
  BKE_view_layer_synced_ensure(*bmain, scene, view_layer);
  Set<const Object *> moving = {&active};
  Vector<Object *> candidates;
  for (Base &base : *BKE_view_layer_object_bases_get(view_layer)) {
    Object *other = base.object;
    if (other == &active || other->type != OB_MESH || !BKE_base_is_visible(v3d, &base) ||
        !BKE_id_is_editable(bmain, &other->id))
    {
      continue;
    }
    candidates.append(other);
    moving.add(other);
  }
  UndoStack *ustack = ED_undo_stack_get();
  for (Object *other : candidates) {
    bool parent_moves = false;
    for (const Object *parent = other->parent; parent; parent = parent->parent) {
      parent_moves |= moving.contains(parent);
    }
    if (parent_moves) {
      continue;
    }
    /* Changed "in the future" of an earlier global undo step, so that one reloads it. */
    ED_undosys_stack_memfile_id_changed_tag(ustack, &other->id);
    ss.phantom_others.append({other, other->object_to_world(), other->object_to_world()});
  }
}

/** Move the other meshes by `local`, the object-space transform of the mesh sculpted. */
static void update_other_objects(const Object &ob, const float4x4 &local)
{
  SculptSession &ss = *ob.runtime->sculpt_session;
  const float4x4 &obmat = ob.object_to_world();
  const float4x4 world = obmat * local * math::invert(obmat);
  for (SculptSession::PhantomOtherObject &other : ss.phantom_others) {
    other.after = world * other.before;
    BKE_object_apply_mat4(other.object, other.after.ptr(), false, true);
    DEG_id_tag_update(&other.object->id, ID_RECALC_TRANSFORM);
  }
}

void init_transform(
    bContext *C, Object &ob, const float mval_fl[2], const char *undo_name, const bool gizmo)
{
  const Scene &scene = *CTX_data_scene(C);
  ToolSettings &ts = *CTX_data_tool_settings(C);
  Sculpt &sd = *(ts.sculpt);
  SculptSession &ss = *ob.runtime->sculpt_session;
  Depsgraph *depsgraph = CTX_data_ensure_evaluated_depsgraph(C);

  ss.init_pivot_pos = ss.pivot_pos;
  ss.init_pivot_rot = ss.pivot_rot;
  ss.init_pivot_scale = ss.pivot_scale;

  ss.prev_pivot_pos = ss.pivot_pos;
  ss.prev_pivot_rot = ss.pivot_rot;
  ss.prev_pivot_scale = ss.pivot_scale;

  /* Phantom: the gizmo moves only its pivot while unlocked, or when Alt is held as it starts. */
  ss.phantom_gizmo = gizmo;
  ss.phantom_pivot_only = false;
  ss.phantom_others.clear();
  if (gizmo) {
    const wmWindow *win = CTX_wm_window(C);
    const bool alt = win && win->runtime->eventstate &&
                     (win->runtime->eventstate->modifier & KM_ALT);
    ss.phantom_pivot_only = gizmo_options.unlocked || alt;
  }

  /* Pivot only transformations push their undo step at the end, if the pivot moved. */
  if (pivot_only(ts, ss)) {
    return;
  }

  BKE_sculptsession_update_for_edit(depsgraph, &ob, false);
  undo::push_begin_ex(scene, ob, undo_name);

  vert_random_access_ensure(ob);

  filter::cache_init(C, ob, sd, undo::Type::Position, mval_fl, 5.0, 1.0f);

  if (sd.transform_mode == SCULPT_TRANSFORM_MODE_RADIUS_ELASTIC) {
    ss.filter_cache->transform_displacement_mode = TransformDisplacementMode::Incremental;
  }
  else {
    ss.filter_cache->transform_displacement_mode = TransformDisplacementMode::Original;
  }

  if (gizmo && gizmo_options.all_visible) {
    collect_other_objects(C, ob);
  }
}

/**
 * Phantom: an undo step for moving only the pivot, from `before_*` to where it is now. None when
 * it didn't move.
 */
static void push_pivot_step(bContext *C,
                            Object &ob,
                            const float3 &before_pos,
                            const float4 &before_rot,
                            const char *name)
{
  SculptSession &ss = *ob.runtime->sculpt_session;
  if (ss.pivot_pos == before_pos && ss.pivot_rot == before_rot) {
    return;
  }
  const float3 after_pos = ss.pivot_pos;
  const float4 after_rot = ss.pivot_rot;
  ss.pivot_pos = before_pos;
  ss.pivot_rot = before_rot;
  Depsgraph *depsgraph = CTX_data_ensure_evaluated_depsgraph(C);
  bke::object::pbvh_ensure(*depsgraph, ob);
  undo::push_begin_ex(*CTX_data_scene(C), ob, name);
  ss.pivot_pos = after_pos;
  ss.pivot_rot = after_rot;
  undo::push_end(ob);
}

static void finish_pivot_change(bContext *C, Object &ob)
{
  SculptSession &ss = *ob.runtime->sculpt_session;
  ARegion *region = CTX_wm_region(C);

  /* Update the viewport navigation rotation origin. */
  Paint *paint = BKE_paint_get_active_from_context(C);
  bke::paint::stroke_set_location(*paint, ss.pivot_pos);

  ED_region_tag_redraw(region);
  WM_event_add_notifier(C, NC_GEOM | ND_SELECT, ob.data);
}

static std::array<float4x4, 8> transform_matrices_init(const SculptSession &ss,
                                                       const ePaintSymmetryFlags symm,
                                                       const TransformDisplacementMode t_mode)
{
  std::array<float4x4, 8> mats;

  /* Phantom: an explicit matrix (Transpose), mirrored into the other symmetry areas. */
  if (ss.phantom_transform_matrix) {
    for (int i = 0; i < PAINT_SYMM_AREAS; i++) {
      float4x4 mirror = float4x4::identity();
      for (int axis = 0; axis < 3; axis++) {
        const int bit = 1 << axis;
        if ((symm & bit) && ((i ^ ss.phantom_transform_area) & bit)) {
          mirror[axis][axis] = -1.0f;
        }
      }
      mats[i] = ss.phantom_transform_whole ? *ss.phantom_transform_matrix :
                                             mirror * *ss.phantom_transform_matrix * mirror;
    }
    return mats;
  }

  float3 final_pivot_pos, d_t, d_s;
  float d_r[4];
  float t_mat[4][4], r_mat[4][4], s_mat[4][4], pivot_mat[4][4], pivot_imat[4][4],
      transform_mat[4][4];

  float start_pivot_pos[3], start_pivot_rot[4], start_pivot_scale[3];
  switch (t_mode) {
    case TransformDisplacementMode::Original:
      copy_v3_v3(start_pivot_pos, ss.init_pivot_pos);
      copy_v4_v4(start_pivot_rot, ss.init_pivot_rot);
      copy_v3_v3(start_pivot_scale, ss.init_pivot_scale);
      break;
    case TransformDisplacementMode::Incremental:
      copy_v3_v3(start_pivot_pos, ss.prev_pivot_pos);
      copy_v4_v4(start_pivot_rot, ss.prev_pivot_rot);
      copy_v3_v3(start_pivot_scale, ss.prev_pivot_scale);
      break;
  }

  for (int i = 0; i < PAINT_SYMM_AREAS; i++) {
    ePaintSymmetryAreas v_symm = ePaintSymmetryAreas(i);

    copy_v3_v3(final_pivot_pos, ss.pivot_pos);

    unit_m4(pivot_mat);

    unit_m4(t_mat);
    unit_m4(r_mat);
    unit_m4(s_mat);

    /* Translation matrix. */
    sub_v3_v3v3(d_t, ss.pivot_pos, start_pivot_pos);
    d_t = flip_v3_by_symm_area(d_t, symm, v_symm, ss.init_pivot_pos);
    translate_m4(t_mat, d_t[0], d_t[1], d_t[2]);

    /* Rotation matrix. */
    sub_qt_qtqt(d_r, ss.pivot_rot, start_pivot_rot);
    normalize_qt(d_r);
    flip_quat_by_symm_area(d_r, symm, v_symm, ss.init_pivot_pos);
    quat_to_mat4(r_mat, d_r);

    /* Scale matrix. */
    sub_v3_v3v3(d_s, ss.pivot_scale, start_pivot_scale);
    add_v3_fl(d_s, 1.0f);
    size_to_mat4(s_mat, d_s);

    /* Pivot matrix. */
    final_pivot_pos = flip_v3_by_symm_area(final_pivot_pos, symm, v_symm, start_pivot_pos);
    translate_m4(pivot_mat, final_pivot_pos[0], final_pivot_pos[1], final_pivot_pos[2]);
    invert_m4_m4(pivot_imat, pivot_mat);

    /* Final transform matrix. */
    mul_m4_m4m4(transform_mat, r_mat, t_mat);
    mul_m4_m4m4(transform_mat, transform_mat, s_mat);
    mul_m4_m4m4(mats[i].ptr(), transform_mat, pivot_imat);
    mul_m4_m4m4(mats[i].ptr(), pivot_mat, mats[i].ptr());
  }

  return mats;
}

static constexpr float transform_mirror_max_distance_eps = 0.00002f;

struct TransformLocalData {
  Vector<float3> positions;
  Vector<float> factors;
  Vector<float3> translations;
};

BLI_NOINLINE static void calc_symm_area_transform_translations(
    const Span<float3> positions,
    const std::array<float4x4, 8> &transform_mats,
    const MutableSpan<float3> translations)
{
  for (const int i : positions.index_range()) {
    const ePaintSymmetryAreas symm_area = get_vertex_symm_area(positions[i]);
    const float3 transformed = math::transform_point(transform_mats[symm_area], positions[i]);
    translations[i] = transformed - positions[i];
  }
}

BLI_NOINLINE static void filter_translations_with_symmetry(const Span<float3> positions,
                                                           const ePaintSymmetryFlags symm,
                                                           const MutableSpan<float3> translations)
{
  if ((symm & (PAINT_SYMM_X | PAINT_SYMM_Y | PAINT_SYMM_Z)) == 0) {
    return;
  }
  for (const int i : positions.index_range()) {
    if ((symm & PAINT_SYMM_X) && (std::abs(positions[i].x) < transform_mirror_max_distance_eps)) {
      translations[i].x = 0.0f;
    }
    if ((symm & PAINT_SYMM_Y) && (std::abs(positions[i].y) < transform_mirror_max_distance_eps)) {
      translations[i].y = 0.0f;
    }
    if ((symm & PAINT_SYMM_Z) && (std::abs(positions[i].z) < transform_mirror_max_distance_eps)) {
      translations[i].z = 0.0f;
    }
  }
}

static void transform_node_mesh(const Sculpt &sd,
                                const std::array<float4x4, 8> &transform_mats,
                                const MeshAttributeData &attribute_data,
                                const bke::pbvh::MeshNode &node,
                                Object &object,
                                TransformLocalData &tls,
                                const PositionDeformData &position_data)
{
  SculptSession &ss = *object.runtime->sculpt_session;

  const Span<int> verts = node.verts();
  const OrigPositionData orig_data = orig_position_data_get_mesh(object, node);

  tls.factors.resize(verts.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(attribute_data.hide_vert, attribute_data.mask, verts, factors);

  tls.translations.resize(verts.size());
  const MutableSpan<float3> translations = tls.translations;
  calc_symm_area_transform_translations(orig_data.positions, transform_mats, translations);
  scale_translations(translations, factors);

  const ePaintSymmetryFlags symm = mesh_symmetry_xyz_get(object);
  filter_translations_with_symmetry(orig_data.positions, symm, translations);

  clip_and_lock_translations(sd, ss, position_data.eval, verts, translations);
  position_data.deform(translations, verts);
}

static void transform_node_grids(const Sculpt &sd,
                                 const std::array<float4x4, 8> &transform_mats,
                                 const bke::pbvh::GridsNode &node,
                                 Object &object,
                                 TransformLocalData &tls)
{
  SculptSession &ss = *object.runtime->sculpt_session;
  SubdivCCG &subdiv_ccg = *ss.subdiv_ccg;
  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);

  const Span<int> grids = node.grids();
  const int grid_verts_num = grids.size() * key.grid_area;

  const OrigPositionData orig_data = orig_position_data_get_grids(object, node);

  tls.factors.resize(grid_verts_num);
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(subdiv_ccg, grids, factors);

  tls.translations.resize(grid_verts_num);
  const MutableSpan<float3> translations = tls.translations;
  calc_symm_area_transform_translations(orig_data.positions, transform_mats, translations);

  scale_translations(translations, factors);

  const ePaintSymmetryFlags symm = mesh_symmetry_xyz_get(object);
  filter_translations_with_symmetry(orig_data.positions, symm, translations);

  clip_and_lock_translations(sd, ss, orig_data.positions, translations);
  apply_translations(translations, grids, subdiv_ccg);
}

static void transform_node_bmesh(const Sculpt &sd,
                                 const std::array<float4x4, 8> &transform_mats,
                                 bke::pbvh::BMeshNode &node,
                                 Object &object,
                                 TransformLocalData &tls)
{
  SculptSession &ss = *object.runtime->sculpt_session;

  const Set<BMVert *, 0> &verts = BKE_pbvh_bmesh_node_unique_verts(&node);

  Array<float3> orig_positions(verts.size());
  Array<float3> orig_normals(verts.size());
  orig_position_data_gather_bmesh(*ss.bm_log, verts, orig_positions, orig_normals);

  tls.factors.resize(verts.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(*ss.bm, verts, factors);

  tls.translations.resize(verts.size());
  const MutableSpan<float3> translations = tls.translations;
  calc_symm_area_transform_translations(orig_positions, transform_mats, translations);

  scale_translations(translations, factors);

  const ePaintSymmetryFlags symm = mesh_symmetry_xyz_get(object);
  filter_translations_with_symmetry(orig_positions, symm, translations);

  clip_and_lock_translations(sd, ss, orig_positions, translations);
  apply_translations(translations, verts);
}

static void sculpt_transform_all_vertices(const Depsgraph &depsgraph, const Sculpt &sd, Object &ob)
{
  undo::restore_position_from_undo_step(depsgraph, ob);

  SculptSession &ss = *ob.runtime->sculpt_session;
  const ePaintSymmetryFlags symm = mesh_symmetry_xyz_get(ob);

  std::array<float4x4, 8> transform_mats = transform_matrices_init(
      ss, symm, ss.filter_cache->transform_displacement_mode);

  /* Regular transform applies all symmetry passes at once as it is split by symmetry areas
   * (each vertex can only be transformed once by the transform matrix of its area). */
  bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(ob);
  const IndexMask &node_mask = ss.filter_cache->node_mask;

  threading::EnumerableThreadSpecific<TransformLocalData> all_tls;
  switch (pbvh.type()) {
    case bke::pbvh::Type::Mesh: {
      Mesh &mesh = *id_cast<Mesh *>(ob.data);
      const MeshAttributeData attribute_data(mesh);
      MutableSpan<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
      const PositionDeformData position_data(depsgraph, ob);
      node_mask.foreach_index(
          [&](const int i) {
            TransformLocalData &tls = all_tls.local();
            transform_node_mesh(
                sd, transform_mats, attribute_data, nodes[i], ob, tls, position_data);
            bke::pbvh::update_node_bounds_mesh(position_data.eval, nodes[i]);
          },
          exec_mode::grain_size(1));
      break;
    }
    case bke::pbvh::Type::Grids: {
      SubdivCCG &subdiv_ccg = *ob.runtime->sculpt_session->subdiv_ccg;
      MutableSpan<float3> positions = subdiv_ccg.positions;
      MutableSpan<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
      node_mask.foreach_index(
          [&](const int i) {
            TransformLocalData &tls = all_tls.local();
            transform_node_grids(sd, transform_mats, nodes[i], ob, tls);
            bke::pbvh::update_node_bounds_grids(subdiv_ccg.grid_area, positions, nodes[i]);
          },
          exec_mode::grain_size(1));
      break;
    }
    case bke::pbvh::Type::BMesh: {
      MutableSpan<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
      node_mask.foreach_index(
          [&](const int i) {
            TransformLocalData &tls = all_tls.local();
            transform_node_bmesh(sd, transform_mats, nodes[i], ob, tls);
            bke::pbvh::update_node_bounds_bmesh(nodes[i]);
          },
          exec_mode::grain_size(1));
      break;
    }
  }
  pbvh.tag_positions_changed(node_mask);
  pbvh.flush_bounds_to_parents();
}

BLI_NOINLINE static void calc_transform_translations(const float4x4 &elastic_transform_mat,
                                                     const Span<float3> positions,
                                                     const MutableSpan<float3> r_translations)
{
  for (const int i : positions.index_range()) {
    const float3 transformed = math::transform_point(elastic_transform_mat, positions[i]);
    r_translations[i] = transformed - positions[i];
  }
}

BLI_NOINLINE static void apply_kelvinet_to_translations(const KelvinletParams &params,
                                                        const float3 &elastic_transform_pivot,
                                                        const Span<float3> positions,
                                                        const MutableSpan<float3> translations)
{
  for (const int i : positions.index_range()) {
    BKE_kelvinlet_grab_triscale(
        translations[i], &params, positions[i], elastic_transform_pivot, translations[i]);
  }
}

static void elastic_transform_node_mesh(const Sculpt &sd,
                                        const KelvinletParams &params,
                                        const float4x4 &elastic_transform_mat,
                                        const float3 &elastic_transform_pivot,
                                        const MeshAttributeData &attribute_data,
                                        const bke::pbvh::MeshNode &node,
                                        Object &object,
                                        TransformLocalData &tls,
                                        const PositionDeformData &position_data)
{
  const SculptSession &ss = *object.runtime->sculpt_session;

  const Span<int> verts = node.verts();
  const MutableSpan positions = gather_data_mesh(position_data.eval, verts, tls.positions);

  /* TODO: Using the factors array is unnecessary when there are no hidden vertices and no mask. */
  tls.factors.resize(verts.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(attribute_data.hide_vert, attribute_data.mask, verts, factors);
  scale_factors(factors, 20.0f);

  tls.translations.resize(verts.size());
  const MutableSpan<float3> translations = tls.translations;
  calc_transform_translations(elastic_transform_mat, positions, translations);
  apply_kelvinet_to_translations(params, elastic_transform_pivot, positions, translations);

  scale_translations(translations, factors);

  clip_and_lock_translations(sd, ss, position_data.eval, verts, translations);
  position_data.deform(translations, verts);
}

static void elastic_transform_node_grids(const Sculpt &sd,
                                         const KelvinletParams &params,
                                         const float4x4 &elastic_transform_mat,
                                         const float3 &elastic_transform_pivot,
                                         const bke::pbvh::GridsNode &node,
                                         Object &object,
                                         TransformLocalData &tls)
{
  SculptSession &ss = *object.runtime->sculpt_session;
  SubdivCCG &subdiv_ccg = *ss.subdiv_ccg;

  const Span<int> grids = node.grids();
  const MutableSpan positions = gather_grids_positions(subdiv_ccg, grids, tls.positions);

  /* TODO: Using the factors array is unnecessary when there are no hidden vertices and no mask. */
  tls.factors.resize(positions.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(subdiv_ccg, grids, factors);
  scale_factors(factors, 20.0f);

  tls.translations.resize(positions.size());
  const MutableSpan<float3> translations = tls.translations;
  calc_transform_translations(elastic_transform_mat, positions, translations);
  apply_kelvinet_to_translations(params, elastic_transform_pivot, positions, translations);

  scale_translations(translations, factors);

  clip_and_lock_translations(sd, ss, positions, translations);
  apply_translations(translations, grids, subdiv_ccg);
}

static void elastic_transform_node_bmesh(const Sculpt &sd,
                                         const KelvinletParams &params,
                                         const float4x4 &elastic_transform_mat,
                                         const float3 &elastic_transform_pivot,
                                         bke::pbvh::BMeshNode &node,
                                         Object &object,
                                         TransformLocalData &tls)
{
  SculptSession &ss = *object.runtime->sculpt_session;

  const Set<BMVert *, 0> &verts = BKE_pbvh_bmesh_node_unique_verts(&node);
  const MutableSpan positions = gather_bmesh_positions(verts, tls.positions);

  tls.factors.resize(verts.size());
  const MutableSpan<float> factors = tls.factors;
  fill_factor_from_hide_and_mask(*ss.bm, verts, factors);
  scale_factors(factors, 20.0f);

  tls.translations.resize(verts.size());
  const MutableSpan<float3> translations = tls.translations;
  calc_transform_translations(elastic_transform_mat, positions, translations);
  apply_kelvinet_to_translations(params, elastic_transform_pivot, positions, translations);

  scale_translations(translations, factors);

  clip_and_lock_translations(sd, ss, positions, translations);
  apply_translations(translations, verts);
}

static void transform_radius_elastic(const Depsgraph &depsgraph,
                                     const Sculpt &sd,
                                     Object &ob,
                                     const float transform_radius)
{
  SculptSession &ss = *ob.runtime->sculpt_session;
  BLI_assert(ss.filter_cache->transform_displacement_mode ==
             TransformDisplacementMode::Incremental);

  const ePaintSymmetryFlags symm = mesh_symmetry_xyz_get(ob);

  std::array<float4x4, 8> transform_mats = transform_matrices_init(
      ss, symm, ss.filter_cache->transform_displacement_mode);

  bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(ob);
  const IndexMask &node_mask = ss.filter_cache->node_mask;

  KelvinletParams params;
  /* TODO(pablodp606): These parameters can be exposed if needed as transform strength and volume
   * preservation like in the elastic deform brushes. Setting them to the same default as elastic
   * deform triscale grab because they work well in most cases. */
  const float force = 1.0f;
  const float shear_modulus = 1.0f;
  const float poisson_ratio = 0.4f;
  BKE_kelvinlet_init_params(&params, transform_radius, force, shear_modulus, poisson_ratio);

  threading::EnumerableThreadSpecific<TransformLocalData> all_tls;
  for (ePaintSymmetryFlags symmpass = PAINT_SYMM_NONE; symmpass <= symm; symmpass++) {
    if (!is_symmetry_iteration_valid(symmpass, symm)) {
      continue;
    }

    const float3 elastic_transform_pivot = symmetry_flip(ss.pivot_pos, symmpass);

    const int symm_area = get_vertex_symm_area(elastic_transform_pivot);
    float4x4 elastic_transform_mat = transform_mats[symm_area];
    switch (pbvh.type()) {
      case bke::pbvh::Type::Mesh: {
        Mesh &mesh = *id_cast<Mesh *>(ob.data);
        MutableSpan<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
        const PositionDeformData position_data(depsgraph, ob);
        const MeshAttributeData attribute_data(mesh);
        node_mask.foreach_index(
            [&](const int i) {
              TransformLocalData &tls = all_tls.local();
              elastic_transform_node_mesh(sd,
                                          params,
                                          elastic_transform_mat,
                                          elastic_transform_pivot,
                                          attribute_data,
                                          nodes[i],
                                          ob,
                                          tls,
                                          position_data);
              bke::pbvh::update_node_bounds_mesh(position_data.eval, nodes[i]);
            },
            exec_mode::grain_size(1));
        break;
      }
      case bke::pbvh::Type::Grids: {
        SubdivCCG &subdiv_ccg = *ob.runtime->sculpt_session->subdiv_ccg;
        MutableSpan<float3> positions = subdiv_ccg.positions;
        MutableSpan<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
        node_mask.foreach_index(
            [&](const int i) {
              TransformLocalData &tls = all_tls.local();
              elastic_transform_node_grids(
                  sd, params, elastic_transform_mat, elastic_transform_pivot, nodes[i], ob, tls);
              bke::pbvh::update_node_bounds_grids(subdiv_ccg.grid_area, positions, nodes[i]);
            },
            exec_mode::grain_size(1));
        break;
      }
      case bke::pbvh::Type::BMesh: {
        MutableSpan<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
        node_mask.foreach_index(
            [&](const int i) {
              TransformLocalData &tls = all_tls.local();
              elastic_transform_node_bmesh(
                  sd, params, elastic_transform_mat, elastic_transform_pivot, nodes[i], ob, tls);
              bke::pbvh::update_node_bounds_bmesh(nodes[i]);
            },
            exec_mode::grain_size(1));
        break;
      }
    }
  }
  pbvh.tag_positions_changed(node_mask);
  pbvh.flush_bounds_to_parents();
}

void update_modal_transform(bContext *C, Object &ob)
{
  ToolSettings &ts = *CTX_data_tool_settings(C);
  Sculpt &sd = *(ts.sculpt);
  SculptSession &ss = *ob.runtime->sculpt_session;
  Depsgraph *depsgraph = CTX_data_depsgraph_pointer(C);

  if (!pivot_only(ts, ss)) {
    vert_random_access_ensure(ob);
    BKE_sculptsession_update_for_edit(depsgraph, &ob, false);

    switch (sd.transform_mode) {
      case SCULPT_TRANSFORM_MODE_ALL_VERTICES: {
        sculpt_transform_all_vertices(*depsgraph, sd, ob);
        break;
      }
      case SCULPT_TRANSFORM_MODE_RADIUS_ELASTIC: {
        const Brush &brush = *BKE_paint_brush_for_read(&sd.paint);
        float transform_radius;

        if (BKE_brush_use_locked_size(&sd.paint, &brush)) {
          transform_radius = BKE_brush_unprojected_radius_get(&sd.paint, &brush);
        }
        else {
          ViewContext vc = ED_view3d_viewcontext_init(C, depsgraph);

          transform_radius = paint_calc_object_space_radius(
              vc, ss.init_pivot_pos, BKE_brush_radius_get(&sd.paint, &brush));
        }

        transform_radius_elastic(*depsgraph, sd, ob, transform_radius);
        break;
      }
    }
    if (!ss.phantom_others.is_empty()) {
      /* The other meshes move as the whole of this one would, from where they started. */
      const std::array<float4x4, 8> mats = transform_matrices_init(
          ss, PAINT_SYMM_NONE, TransformDisplacementMode::Original);
      update_other_objects(ob, mats[0]);
    }
  }
  else {
    /* Intentional no-op */
  }

  copy_v3_v3(ss.prev_pivot_pos, ss.pivot_pos);
  copy_v4_v4(ss.prev_pivot_rot, ss.pivot_rot);
  copy_v3_v3(ss.prev_pivot_scale, ss.pivot_scale);

  if (!pivot_only(ts, ss)) {
    flush_update_step(C, UpdateType::Position);
  }
}

void cancel_modal_transform(bContext *C, Object &ob)
{
  /* Canceling "Elastic" transforms (due to its #TransformDisplacementMode::Incremental nature),
   * requires restoring positions from undo. For "All Vertices" there is no benefit in using the
   * transform system to update to original positions either. */
  Depsgraph &depsgraph = *CTX_data_depsgraph_pointer(C);
  SculptSession &ss = *ob.runtime->sculpt_session;
  if (pivot_only(*CTX_data_tool_settings(C), ss)) {
    return;
  }
  undo::restore_position_from_undo_step(depsgraph, ob);
  /* Phantom: the other meshes back where they were. */
  update_other_objects(ob, float4x4::identity());
  ss.phantom_others.clear();

  bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(ob);
  bke::pbvh::update_normals(depsgraph, ob, pbvh);
  pbvh.update_bounds(depsgraph, ob);
}

void end_transform(bContext *C, Object &ob)
{
  ToolSettings &ts = *CTX_data_tool_settings(C);
  SculptSession &ss = *ob.runtime->sculpt_session;
  const bool gizmo = ss.phantom_gizmo;
  ss.phantom_gizmo = false;
  if (pivot_only(ts, ss)) {
    BLI_assert(ss.filter_cache == nullptr);
    ss.phantom_pivot_only = false;
    /* Phantom: moving the pivot can be undone too. */
    push_pivot_step(C, ob, ss.init_pivot_pos, ss.init_pivot_rot, IFACE_("Move Gizmo Pivot"));
    finish_pivot_change(C, ob);
    return;
  }
  MEM_delete(ss.filter_cache);
  ss.filter_cache = nullptr;
  /* Phantom: sticky, the gizmo's pivot and axes go back to where they were, the mesh stays. */
  if (gizmo && gizmo_options.sticky) {
    ss.pivot_pos = ss.init_pivot_pos;
    ss.pivot_rot = ss.init_pivot_rot;
  }
  if (!ss.phantom_others.is_empty()) {
    undo::store_other_objects(ss.phantom_others);
    ss.phantom_others.clear();
    WM_event_add_notifier(C, NC_OBJECT | ND_TRANSFORM, nullptr);
  }
  undo::push_end(ob);
  flush_update_done(C, ob, UpdateType::Position);
  if (gizmo && gizmo_options.sticky) {
    finish_pivot_change(C, ob);
  }
}

/* -------------------------------------------------------------------- */
/** \name Phantom: transform by matrix (Transpose)
 *
 * Lets a tool (the Transpose action line, written in Python) drive the sculpt transform with any
 * matrix: BEGIN starts it (with an undo step), each UPDATE deforms the original positions by the
 * matrix (mask, hidden and symmetry handled as for the Transform tools), END keeps the result and
 * CANCEL restores it.
 * \{ */

enum class PhantomTransformAction { Begin = 0, Update = 1, End = 2, Cancel = 3 };

static wmOperatorStatus phantom_transform_exec(bContext *C, wmOperator *op)
{
  Object &ob = *CTX_data_active_object(C);
  SculptSession &ss = *ob.runtime->sculpt_session;
  ToolSettings &ts = *CTX_data_tool_settings(C);
  const PhantomTransformAction action = PhantomTransformAction(RNA_enum_get(op->ptr, "action"));

  switch (action) {
    case PhantomTransformAction::Begin: {
      if (ss.filter_cache) {
        return OPERATOR_CANCELLED;
      }
      /* Pivot-only transforms skip the undo step and cache that deforming needs. */
      ts.transform_flag &= ~SCE_XFORM_SCULPT_PIVOT;
      const float mval[2] = {0.0f, 0.0f};
      init_transform(C, ob, mval, IFACE_("Transpose"), false);
      ss.filter_cache->transform_displacement_mode = TransformDisplacementMode::Original;
      return OPERATOR_FINISHED;
    }
    case PhantomTransformAction::Update: {
      if (ss.filter_cache == nullptr) {
        return OPERATOR_CANCELLED;
      }
      float matrix[4][4];
      RNA_float_get_array(op->ptr, "matrix", &matrix[0][0]);
      float3 origin;
      RNA_float_get_array(op->ptr, "origin", origin);
      ss.phantom_transform_matrix = float4x4(matrix);
      ss.phantom_transform_area = get_vertex_symm_area(origin);

      const Sculpt &sd = *ts.sculpt;
      Depsgraph *depsgraph = CTX_data_depsgraph_pointer(C);
      vert_random_access_ensure(ob);
      BKE_sculptsession_update_for_edit(depsgraph, &ob, false);
      sculpt_transform_all_vertices(*depsgraph, sd, ob);
      ss.phantom_transform_matrix.reset();
      flush_update_step(C, UpdateType::Position);
      return OPERATOR_FINISHED;
    }
    case PhantomTransformAction::End:
    case PhantomTransformAction::Cancel: {
      if (ss.filter_cache == nullptr) {
        return OPERATOR_CANCELLED;
      }
      if (action == PhantomTransformAction::Cancel) {
        cancel_modal_transform(C, ob);
      }
      end_transform(C, ob);
      return OPERATOR_FINISHED;
    }
  }
  return OPERATOR_CANCELLED;
}

void SCULPT_OT_phantom_transform(wmOperatorType *ot)
{
  ot->name = "Transform by Matrix";
  ot->idname = "SCULPT_OT_phantom_transform";
  ot->description = "Deform the unmasked mesh by a matrix (used by the Transpose tool)";

  ot->exec = phantom_transform_exec;
  ot->poll = sculpt_mode_poll_view3d;

  ot->flag = OPTYPE_INTERNAL;

  static const EnumPropertyItem actions[] = {
      {int(PhantomTransformAction::Begin), "BEGIN", 0, "Begin", ""},
      {int(PhantomTransformAction::Update), "UPDATE", 0, "Update", ""},
      {int(PhantomTransformAction::End), "END", 0, "End", ""},
      {int(PhantomTransformAction::Cancel), "CANCEL", 0, "Cancel", ""},
      {0, nullptr, 0, nullptr, nullptr},
  };
  RNA_def_enum(ot->srna, "action", actions, int(PhantomTransformAction::Update), "Action", "");
  static const float identity[16] = {
      1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f};
  RNA_def_float_matrix(ot->srna,
                       "matrix",
                       4,
                       4,
                       identity,
                       -FLT_MAX,
                       FLT_MAX,
                       "Matrix",
                       "Object-space transform of the original positions",
                       -FLT_MAX,
                       FLT_MAX);
  RNA_def_float_vector(ot->srna,
                       "origin",
                       3,
                       nullptr,
                       -FLT_MAX,
                       FLT_MAX,
                       "Origin",
                       "Object-space point on the side the transform was made (for symmetry)",
                       -FLT_MAX,
                       FLT_MAX);
}

/** \} */

enum class PivotPositionMode {
  Origin = 0,
  Unmasked = 1,
  MaskBorder = 2,
  ActiveVert = 3,
  CursorSurface = 4,
};

static EnumPropertyItem prop_sculpt_pivot_position_types[] = {
    {int(PivotPositionMode::Origin),
     "ORIGIN",
     0,
     "Origin",
     "Sets the pivot to the origin of the sculpt"},
    {int(PivotPositionMode::Unmasked),
     "UNMASKED",
     0,
     "Unmasked",
     "Sets the pivot position to the average position of the unmasked vertices"},
    {int(PivotPositionMode::MaskBorder),
     "BORDER",
     0,
     "Mask Border",
     "Sets the pivot position to the center of the border of the mask"},
    {int(PivotPositionMode::ActiveVert),
     "ACTIVE",
     0,
     "Active Vertex",
     "Sets the pivot position to the active vertex position"},
    {int(PivotPositionMode::CursorSurface),
     "SURFACE",
     0,
     "Surface",
     "Sets the pivot position to the surface under the cursor"},
    {0, nullptr, 0, nullptr, nullptr},
};

static bool set_pivot_depends_on_cursor(bContext & /*C*/, wmOperatorType & /*ot*/, PointerRNA *ptr)
{
  if (!ptr) {
    return true;
  }
  const PivotPositionMode mode = PivotPositionMode(RNA_enum_get(ptr, "mode"));
  return ELEM(mode, PivotPositionMode::CursorSurface, PivotPositionMode::ActiveVert);
}

struct AveragePositionAccumulation {
  double3 position;
  double weight_total;
};

static AveragePositionAccumulation combine_average_position_accumulation(
    const AveragePositionAccumulation &a, const AveragePositionAccumulation &b)
{
  return AveragePositionAccumulation{a.position + b.position, a.weight_total + b.weight_total};
}

BLI_NOINLINE static void accumulate_weighted_average_position(const Span<float3> positions,
                                                              const Span<float> factors,
                                                              AveragePositionAccumulation &total)
{
  BLI_assert(positions.size() == factors.size());

  for (const int i : positions.index_range()) {
    total.position += double3(positions[i] * factors[i]);
    total.weight_total += factors[i];
  }
}

static float3 average_unmasked_position(const Depsgraph &depsgraph,
                                        const Object &object,
                                        const float3 &pivot,
                                        const ePaintSymmetryFlags symm)
{
  const SculptSession &ss = *object.runtime->sculpt_session;
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);

  IndexMaskMemory memory;
  const IndexMask node_mask = bke::pbvh::search_nodes(
      pbvh, memory, [&](const bke::pbvh::Node &node) {
        return !node_fully_masked_or_hidden(node);
      });

  struct LocalData {
    Vector<float> factors;
    Vector<float3> positions;
  };

  threading::EnumerableThreadSpecific<LocalData> all_tls;
  switch (pbvh.type()) {
    case bke::pbvh::Type::Mesh: {
      const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
      const Mesh &mesh = *id_cast<const Mesh *>(object.data);
      const MeshAttributeData attribute_data(mesh);
      const Span<float3> vert_positions = bke::pbvh::vert_positions_eval(depsgraph, object);
      const AveragePositionAccumulation total = threading::parallel_reduce(
          node_mask.index_range(),
          1,
          AveragePositionAccumulation{},
          [&](const IndexRange range, AveragePositionAccumulation sum) {
            LocalData &tls = all_tls.local();
            threading::isolate_task([&]() {
              node_mask.slice(range).foreach_index([&](const int i) {
                const Span<int> verts = nodes[i].verts();

                tls.positions.resize(verts.size());
                const MutableSpan<float3> positions = tls.positions;
                array_utils::gather(vert_positions, verts, positions);

                tls.factors.resize(verts.size());
                const MutableSpan<float> factors = tls.factors;
                fill_factor_from_hide_and_mask(
                    attribute_data.hide_vert, attribute_data.mask, verts, factors);
                filter_verts_outside_symmetry_area(positions, pivot, symm, factors);

                accumulate_weighted_average_position(positions, factors, sum);
              });
            });
            return sum;
          },
          combine_average_position_accumulation);
      return float3(math::safe_divide(total.position, total.weight_total));
    }
    case bke::pbvh::Type::Grids: {
      const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
      const SubdivCCG &subdiv_ccg = *ss.subdiv_ccg;
      const AveragePositionAccumulation total = threading::parallel_reduce(
          node_mask.index_range(),
          1,
          AveragePositionAccumulation{},
          [&](const IndexRange range, AveragePositionAccumulation sum) {
            LocalData &tls = all_tls.local();
            node_mask.slice(range).foreach_index([&](const int i) {
              const Span<int> grids = nodes[i].grids();
              const MutableSpan positions = gather_grids_positions(
                  subdiv_ccg, grids, tls.positions);

              tls.factors.resize(positions.size());
              const MutableSpan<float> factors = tls.factors;
              fill_factor_from_hide_and_mask(subdiv_ccg, grids, factors);
              filter_verts_outside_symmetry_area(positions, pivot, symm, factors);

              accumulate_weighted_average_position(positions, factors, sum);
            });
            return sum;
          },
          combine_average_position_accumulation);
      return float3(math::safe_divide(total.position, total.weight_total));
    }
    case bke::pbvh::Type::BMesh: {
      const Span<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
      const AveragePositionAccumulation total = threading::parallel_reduce(
          node_mask.index_range(),
          1,
          AveragePositionAccumulation{},
          [&](const IndexRange range, AveragePositionAccumulation sum) {
            LocalData &tls = all_tls.local();
            node_mask.slice(range).foreach_index([&](const int i) {
              const Set<BMVert *, 0> &verts = BKE_pbvh_bmesh_node_unique_verts(
                  &const_cast<bke::pbvh::BMeshNode &>(nodes[i]));
              const MutableSpan positions = gather_bmesh_positions(verts, tls.positions);

              tls.factors.resize(verts.size());
              const MutableSpan<float> factors = tls.factors;
              fill_factor_from_hide_and_mask(*ss.bm, verts, factors);
              filter_verts_outside_symmetry_area(positions, pivot, symm, factors);

              accumulate_weighted_average_position(positions, factors, sum);
            });
            return sum;
          },
          combine_average_position_accumulation);
      return float3(math::safe_divide(total.position, total.weight_total));
    }
  }
  BLI_assert_unreachable();
  return float3(0);
}

BLI_NOINLINE static void mask_border_weight_calc(const Span<float> masks,
                                                 const MutableSpan<float> factors)
{
  constexpr float threshold = 0.2f;

  for (const int i : masks.index_range()) {
    if (std::abs(masks[i] - 0.5f) > threshold) {
      factors[i] = 0.0f;
    }
  }
};

static float3 average_mask_border_position(const Depsgraph &depsgraph,
                                           const Object &object,
                                           const float3 &pivot,
                                           const ePaintSymmetryFlags symm)
{
  const SculptSession &ss = *object.runtime->sculpt_session;
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);

  IndexMaskMemory memory;
  const IndexMask node_mask = bke::pbvh::search_nodes(
      pbvh, memory, [&](const bke::pbvh::Node &node) {
        return !node_fully_masked_or_hidden(node);
      });

  struct LocalData {
    Vector<float> factors;
    Vector<float> masks;
    Vector<float3> positions;
  };

  threading::EnumerableThreadSpecific<LocalData> all_tls;
  switch (pbvh.type()) {
    case bke::pbvh::Type::Mesh: {
      const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
      const Mesh &mesh = *id_cast<const Mesh *>(object.data);
      const Span<float3> vert_positions = bke::pbvh::vert_positions_eval(depsgraph, object);
      const bke::AttributeAccessor attributes = mesh.attributes();
      const VArraySpan mask_attr = *attributes.lookup_or_default<float>(
          ".sculpt_mask", bke::AttrDomain::Point, 0.0f);
      const VArraySpan hide_vert = *attributes.lookup<bool>(".hide_vert", bke::AttrDomain::Point);
      const AveragePositionAccumulation total = threading::parallel_reduce(
          node_mask.index_range(),
          1,
          AveragePositionAccumulation{},
          [&](const IndexRange range, AveragePositionAccumulation sum) {
            LocalData &tls = all_tls.local();
            node_mask.slice(range).foreach_index([&](const int i) {
              const Span<int> verts = nodes[i].verts();
              MutableSpan positions = gather_data_mesh(vert_positions, verts, tls.positions);
              MutableSpan masks = gather_data_mesh(mask_attr, verts, tls.masks);

              tls.factors.resize(verts.size());
              const MutableSpan<float> factors = tls.factors;
              fill_factor_from_hide(hide_vert, verts, factors);

              mask_border_weight_calc(masks, factors);
              filter_verts_outside_symmetry_area(positions, pivot, symm, factors);

              accumulate_weighted_average_position(positions, factors, sum);
            });
            return sum;
          },
          combine_average_position_accumulation);
      return float3(math::safe_divide(total.position, total.weight_total));
    }
    case bke::pbvh::Type::Grids: {
      const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
      const SubdivCCG &subdiv_ccg = *ss.subdiv_ccg;
      const AveragePositionAccumulation total = threading::parallel_reduce(
          node_mask.index_range(),
          1,
          AveragePositionAccumulation{},
          [&](const IndexRange range, AveragePositionAccumulation sum) {
            LocalData &tls = all_tls.local();
            node_mask.slice(range).foreach_index([&](const int i) {
              const Span<int> grids = nodes[i].grids();
              const MutableSpan positions = gather_grids_positions(
                  subdiv_ccg, grids, tls.positions);

              tls.masks.resize(positions.size());
              const MutableSpan<float> masks = tls.masks;
              mask::gather_mask_grids(subdiv_ccg, grids, masks);

              tls.factors.resize(positions.size());
              const MutableSpan<float> factors = tls.factors;
              fill_factor_from_hide(subdiv_ccg, grids, factors);
              mask_border_weight_calc(masks, factors);
              filter_verts_outside_symmetry_area(positions, pivot, symm, factors);

              accumulate_weighted_average_position(positions, factors, sum);
            });
            return sum;
          },
          combine_average_position_accumulation);
      return float3(math::safe_divide(total.position, total.weight_total));
    }
    case bke::pbvh::Type::BMesh: {
      const Span<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
      const AveragePositionAccumulation total = threading::parallel_reduce(
          node_mask.index_range(),
          1,
          AveragePositionAccumulation{},
          [&](const IndexRange range, AveragePositionAccumulation sum) {
            LocalData &tls = all_tls.local();
            node_mask.slice(range).foreach_index([&](const int i) {
              const Set<BMVert *, 0> &verts = BKE_pbvh_bmesh_node_unique_verts(
                  &const_cast<bke::pbvh::BMeshNode &>(nodes[i]));
              const MutableSpan positions = gather_bmesh_positions(verts, tls.positions);

              tls.masks.resize(verts.size());
              const MutableSpan<float> masks = tls.masks;
              mask::gather_mask_bmesh(*ss.bm, verts, masks);

              tls.factors.resize(verts.size());
              const MutableSpan<float> factors = tls.factors;
              fill_factor_from_hide(verts, factors);
              mask_border_weight_calc(masks, factors);
              filter_verts_outside_symmetry_area(positions, pivot, symm, factors);

              accumulate_weighted_average_position(positions, factors, sum);
            });
            return sum;
          },
          combine_average_position_accumulation);
      return float3(math::safe_divide(total.position, total.weight_total));
    }
  }
  BLI_assert_unreachable();
  return float3(0);
}

static wmOperatorStatus set_pivot_position_exec(bContext *C, wmOperator *op)
{
  Object &ob = *CTX_data_active_object(C);
  SculptSession &ss = *ob.runtime->sculpt_session;
  Depsgraph *depsgraph = CTX_data_ensure_evaluated_depsgraph(C);
  const ePaintSymmetryFlags symm = mesh_symmetry_xyz_get(ob);

  const PivotPositionMode mode = PivotPositionMode(RNA_enum_get(op->ptr, "mode"));

  const View3D *v3d = CTX_wm_view3d(C);
  const Base *base = CTX_data_active_base(C);
  if (!BKE_base_is_visible(v3d, base)) {
    return OPERATOR_CANCELLED;
  }

  BKE_sculptsession_update_for_edit(depsgraph, &ob, false);

  switch (mode) {
    case PivotPositionMode::Origin:
      ss.pivot_pos = float3(0.0f);
      break;
    case PivotPositionMode::Unmasked:
      ss.pivot_pos = average_unmasked_position(*depsgraph, ob, ss.pivot_pos, symm);
      break;
    case PivotPositionMode::MaskBorder:
      ss.pivot_pos = average_mask_border_position(*depsgraph, ob, ss.pivot_pos, symm);
      break;
    case PivotPositionMode::ActiveVert: {
      const float2 mval(RNA_float_get(op->ptr, "mouse_x"), RNA_float_get(op->ptr, "mouse_y"));
      if (cursor_geometry_info_update(C, mval, false)) {
        ss.pivot_pos = ss.active_vert_position(*depsgraph, ob);
      }
      break;
    }
    case PivotPositionMode::CursorSurface: {
      const float2 mval(RNA_float_get(op->ptr, "mouse_x"), RNA_float_get(op->ptr, "mouse_y"));
      if (std::optional<float3> stroke_location = stroke_get_location_bvh(C, mval, false)) {
        ss.pivot_pos = *stroke_location;
      }
      break;
    }
  }

  finish_pivot_change(C, ob);

  return OPERATOR_FINISHED;
}

static wmOperatorStatus set_pivot_position_invoke(bContext *C,
                                                  wmOperator *op,
                                                  const wmEvent *event)
{
  RNA_float_set(op->ptr, "mouse_x", event->mval[0]);
  RNA_float_set(op->ptr, "mouse_y", event->mval[1]);
  return set_pivot_position_exec(C, op);
}

static bool set_pivot_position_poll_property(const bContext * /*C*/,
                                             wmOperator *op,
                                             const PropertyRNA *prop)
{
  if (STRPREFIX(RNA_property_identifier(prop), "mouse_")) {
    const PivotPositionMode mode = PivotPositionMode(RNA_enum_get(op->ptr, "mode"));
    return ELEM(mode, PivotPositionMode::CursorSurface, PivotPositionMode::ActiveVert);
  }
  return true;
}

void SCULPT_OT_set_pivot_position(wmOperatorType *ot)
{
  ot->name = "Set Pivot Position";
  ot->idname = "SCULPT_OT_set_pivot_position";
  ot->description = "Sets the sculpt transform pivot position";

  ot->invoke = set_pivot_position_invoke;
  ot->exec = set_pivot_position_exec;
  ot->poll = sculpt_mode_poll_view3d;
  ot->depends_on_cursor = set_pivot_depends_on_cursor;
  ot->poll_property = set_pivot_position_poll_property;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;
  RNA_def_enum(ot->srna,
               "mode",
               prop_sculpt_pivot_position_types,
               int(PivotPositionMode::Unmasked),
               "Mode",
               "");

  RNA_def_float(ot->srna,
                "mouse_x",
                0.0f,
                0.0f,
                FLT_MAX,
                "Mouse Position X",
                "Position of the mouse used for \"Surface\" and \"Active Vertex\" mode",
                0.0f,
                10000.0f);
  RNA_def_float(ot->srna,
                "mouse_y",
                0.0f,
                0.0f,
                FLT_MAX,
                "Mouse Position Y",
                "Position of the mouse used for \"Surface\" and \"Active Vertex\" mode",
                0.0f,
                10000.0f);
}

/* -------------------------------------------------------------------- */
/** \name Phantom: gizmo pivot actions
 *
 * The icons above the gizmo (see `phantom_transpose.py`): put its pivot on the mesh's center, at
 * the world origin, or back to the world axes. While the gizmo is locked, Home and the axes
 * reset take the mesh along (back to where the gizmo moved and turned it from); unlocked, only
 * the pivot moves.
 * \{ */

enum class GizmoAction { Center = 0, Home = 1, Spin = 2 };

/**
 * The mesh's center of volume (else of its surface), or the average of its unmasked vertices
 * when part of it is masked or hidden, or with Multires or Dyntopo.
 */
static float3 mesh_center(const Depsgraph &depsgraph, Object &ob)
{
  const SculptSession &ss = *ob.runtime->sculpt_session;
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(ob);
  if (pbvh.type() == bke::pbvh::Type::Mesh) {
    const Mesh &mesh = *id_cast<const Mesh *>(ob.data);
    const bke::AttributeAccessor attributes = mesh.attributes();
    bool partial = false;
    if (const VArray mask = *attributes.lookup<float>(".sculpt_mask", bke::AttrDomain::Point)) {
      const VArraySpan<float> values(mask);
      partial |= std::any_of(values.begin(), values.end(), [](const float m) { return m > 0.0f; });
    }
    if (const VArray hide = *attributes.lookup<bool>(".hide_vert", bke::AttrDomain::Point)) {
      const VArraySpan<bool> values(hide);
      partial |= std::any_of(values.begin(), values.end(), [](const bool h) { return h; });
    }
    float3 center;
    if (!partial &&
        (BKE_mesh_center_of_volume(&mesh, center) || BKE_mesh_center_of_surface(&mesh, center)))
    {
      return center;
    }
  }
  return average_unmasked_position(depsgraph, ob, ss.pivot_pos, PAINT_SYMM_NONE);
}

static wmOperatorStatus phantom_gizmo_exec(bContext *C, wmOperator *op)
{
  static const char *undo_names[] = {
      N_("Center Gizmo"), N_("Gizmo to World Origin"), N_("Reset Gizmo Rotation")};
  Object &ob = *CTX_data_active_object(C);
  SculptSession &ss = *ob.runtime->sculpt_session;
  ToolSettings &ts = *CTX_data_tool_settings(C);
  const GizmoAction action = GizmoAction(RNA_enum_get(op->ptr, "action"));
  const char *undo_name = IFACE_(undo_names[int(action)]);
  if (ss.filter_cache) {
    /* In the middle of another transform. */
    return OPERATOR_CANCELLED;
  }
  Depsgraph *depsgraph = CTX_data_ensure_evaluated_depsgraph(C);
  BKE_sculptsession_update_for_edit(depsgraph, &ob, false);

  const float3 before_pos = ss.pivot_pos;
  const float4 before_rot = ss.pivot_rot;
  float3 pos = before_pos;
  float4 rot = before_rot;
  switch (action) {
    case GizmoAction::Center:
      pos = mesh_center(*depsgraph, ob);
      break;
    case GizmoAction::Home:
      pos = math::transform_point(ob.world_to_object(), float3(0.0f));
      break;
    case GizmoAction::Spin:
      rot = float4(1.0f, 0.0f, 0.0f, 0.0f);
      break;
  }
  if (pos == before_pos && rot == before_rot) {
    return OPERATOR_CANCELLED;
  }

  const bool move_mesh = action != GizmoAction::Center && !gizmo_options.unlocked &&
                         !(ts.transform_flag & SCE_XFORM_SCULPT_PIVOT);
  if (!move_mesh) {
    ss.pivot_pos = pos;
    ss.pivot_rot = rot;
    push_pivot_step(C, ob, before_pos, before_rot, undo_name);
    finish_pivot_change(C, ob);
    return OPERATOR_FINISHED;
  }

  /* The mesh moves from the pivot as it is to the pivot as it will be. */
  float4x4 local;
  if (action == GizmoAction::Home) {
    local = math::from_location<float4x4>(pos - before_pos);
  }
  else {
    float4 delta;
    sub_qt_qtqt(delta, rot, before_rot);
    normalize_qt(delta);
    float4x4 rotation;
    quat_to_mat4(rotation.ptr(), delta);
    local = math::from_location<float4x4>(before_pos) * rotation *
            math::from_location<float4x4>(-before_pos);
  }

  const float mval[2] = {0.0f, 0.0f};
  ts.transform_flag &= ~SCE_XFORM_SCULPT_PIVOT;
  init_transform(C, ob, mval, undo_name, false);
  if (gizmo_options.all_visible) {
    collect_other_objects(C, ob);
  }
  /* The whole mesh moves: symmetry would mirror it into itself. */
  Mesh &mesh = *id_cast<Mesh *>(ob.data);
  const eMeshSymmetryType symmetry = mesh.symmetry;
  mesh.symmetry = eMeshSymmetryType(symmetry & ~ME_SYMMETRY_XYZ);
  ss.phantom_transform_matrix = local;
  ss.phantom_transform_whole = true;
  sculpt_transform_all_vertices(*depsgraph, *ts.sculpt, ob);
  ss.phantom_transform_matrix.reset();
  ss.phantom_transform_whole = false;
  mesh.symmetry = symmetry;
  flush_update_step(C, UpdateType::Position);
  update_other_objects(ob, local);

  ss.pivot_pos = pos;
  ss.pivot_rot = rot;
  end_transform(C, ob);
  finish_pivot_change(C, ob);
  return OPERATOR_FINISHED;
}

void SCULPT_OT_phantom_gizmo(wmOperatorType *ot)
{
  ot->name = "Gizmo Pivot";
  ot->idname = "SCULPT_OT_phantom_gizmo";
  ot->description =
      "Put the gizmo's pivot on the mesh's center, at the world origin, or back to the world "
      "axes (while it is locked, the mesh comes along)";

  ot->exec = phantom_gizmo_exec;
  ot->poll = sculpt_mode_poll_view3d;

  ot->flag = OPTYPE_INTERNAL;

  static const EnumPropertyItem actions[] = {
      {int(GizmoAction::Center),
       "CENTER",
       0,
       "Center",
       "Put the pivot on the center of the mesh's volume (of its unmasked part when masked)"},
      {int(GizmoAction::Home),
       "HOME",
       0,
       "Home",
       "Put the pivot at the world origin, the mesh along with it while the gizmo is locked"},
      {int(GizmoAction::Spin),
       "SPIN",
       0,
       "Reset Rotation",
       "Turn the gizmo back to the world axes, the mesh along with it while the gizmo is locked"},
      {0, nullptr, 0, nullptr, nullptr},
  };
  RNA_def_enum(ot->srna, "action", actions, int(GizmoAction::Center), "Action", "");
}

/** \} */

}  // namespace blender::ed::sculpt_paint
