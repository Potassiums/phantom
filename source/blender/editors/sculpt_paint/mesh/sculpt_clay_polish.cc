/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup edsculpt
 *
 * Phantom: ClayPolish, the polish and sharpen pass over the whole mesh (see #GEO_mesh_polish.hh).
 * Masked and hidden vertices don't move.
 */

#include "BLI_array.hh"
#include "BLI_enumerable_thread_specific.hh"
#include "BLI_task.hh"
#include "BLI_vector.hh"

#include "DNA_mesh_types.h"
#include "DNA_object_types.h"
#include "DNA_scene_types.h"

#include "BKE_attribute.hh"
#include "BKE_context.hh"
#include "BKE_mesh.hh"
#include "BKE_object_types.hh"
#include "BKE_paint.hh"
#include "BKE_paint_bvh.hh"
#include "BKE_report.hh"

#include "DEG_depsgraph.hh"

#include "GEO_mesh_polish.hh"

#include "RNA_access.hh"
#include "RNA_define.hh"

#include "WM_api.hh"
#include "WM_types.hh"

#include "mesh_brush_common.hh"
#include "sculpt_intern.hh"
#include "sculpt_undo.hh"

namespace blender::ed::sculpt_paint {

static geometry::PolishSettings polish_settings_from_op(const wmOperator &op)
{
  geometry::PolishSettings settings;
  settings.angle_preservation = RNA_boolean_get(op.ptr, "angle_preservation");
  settings.angle_ratio = RNA_float_get(op.ptr, "angle_ratio");
  settings.sharpness = RNA_float_get(op.ptr, "sharpness");
  settings.softness = RNA_float_get(op.ptr, "softness");
  settings.repeat_polish = RNA_int_get(op.ptr, "repeat_polish");
  settings.repeat_sharpen = RNA_int_get(op.ptr, "repeat_sharpen");
  settings.inflate_outer = RNA_float_get(op.ptr, "inflate_outer");
  settings.inflate_inner = RNA_float_get(op.ptr, "inflate_inner");
  return settings;
}

static wmOperatorStatus clay_polish_exec(bContext *C, wmOperator *op)
{
  const Scene &scene = *CTX_data_scene(C);
  Object &ob = *CTX_data_active_object(C);
  Depsgraph *depsgraph = CTX_data_ensure_evaluated_depsgraph(C);
  BKE_sculptsession_update_for_edit(depsgraph, &ob, false);

  bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(ob);
  if (pbvh.type() != bke::pbvh::Type::Mesh) {
    BKE_report(op->reports,
               RPT_ERROR,
               pbvh.type() == bke::pbvh::Type::BMesh ?
                   "ClayPolish doesn't work with Dyntopo, turn it off first" :
                   "ClayPolish doesn't work on Multires, apply it first");
    return OPERATOR_CANCELLED;
  }

  const Sculpt &sd = *CTX_data_tool_settings(C)->sculpt;
  const SculptSession &ss = *ob.runtime->sculpt_session;
  const Mesh &mesh = *id_cast<Mesh *>(ob.data);
  const PositionDeformData position_data(*depsgraph, ob);
  const Span<float3> positions = position_data.eval;

  /* Masked and hidden vertices are pinned: they act as the boundary of the polish. */
  const bke::AttributeAccessor attributes = mesh.attributes();
  const VArraySpan mask = *attributes.lookup<float>(".sculpt_mask", bke::AttrDomain::Point);
  const VArraySpan hide_vert = *attributes.lookup<bool>(".hide_vert", bke::AttrDomain::Point);
  Array<float> influence(positions.size());
  threading::parallel_for(influence.index_range(), 4096, [&](const IndexRange range) {
    for (const int vert : range) {
      const bool hidden = !hide_vert.is_empty() && hide_vert[vert];
      influence[vert] = hidden ? 0.0f : (mask.is_empty() ? 1.0f : 1.0f - mask[vert]);
    }
  });

  Array<float3> new_positions(positions.size());
  geometry::mesh_polish(mesh, positions, influence, polish_settings_from_op(*op), new_positions);

  IndexMaskMemory memory;
  const IndexMask node_mask = bke::pbvh::all_leaf_nodes(pbvh, memory);
  MutableSpan<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();

  undo::push_begin(scene, ob, op);
  undo::push_nodes(*depsgraph, ob, node_mask, undo::Type::Position);

  threading::EnumerableThreadSpecific<Vector<float3>> all_translations;
  node_mask.foreach_index(
      [&](const int i) {
        Vector<float3> &translations = all_translations.local();
        const Span<int> verts = nodes[i].verts();
        translations.resize(verts.size());
        for (const int j : verts.index_range()) {
          translations[j] = new_positions[verts[j]] - positions[verts[j]];
        }
        /* Symmetry clipping and axis locks, like any sculpt deformation. */
        clip_and_lock_translations(sd, ss, positions, verts, translations);
        position_data.deform(translations, verts);
      },
      exec_mode::grain_size(1));

  pbvh.tag_positions_changed(node_mask);
  pbvh.update_bounds(*depsgraph, ob);
  flush_update_step(C, UpdateType::Position);
  flush_update_done(C, ob, UpdateType::Position);
  undo::push_end(ob);

  return OPERATOR_FINISHED;
}

void SCULPT_OT_phantom_clay_polish(wmOperatorType *ot)
{
  ot->name = "ClayPolish";
  ot->idname = "SCULPT_OT_phantom_clay_polish";
  ot->description =
      "Polish the whole surface: smooth away noise and keep creases crisp, leaving masked and "
      "hidden areas untouched";

  ot->exec = clay_polish_exec;
  ot->poll = sculpt_mode_poll;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;

  const geometry::PolishSettings defaults;
  RNA_def_boolean(ot->srna,
                  "angle_preservation",
                  defaults.angle_preservation,
                  "Angle Preservation",
                  "Keep edges by their sharpness. When off, everything is smoothed alike");
  RNA_def_float_factor(ot->srna,
                       "angle_ratio",
                       defaults.angle_ratio,
                       0.0f,
                       1.0f,
                       "Angle Ratio",
                       "How much of the angle range counts as an edge to keep: higher keeps "
                       "gentler edges too",
                       0.0f,
                       1.0f);
  RNA_def_float_factor(ot->srna,
                       "sharpness",
                       defaults.sharpness,
                       0.0f,
                       1.0f,
                       "Sharpness",
                       "How strongly kept edges are sharpened after each polish",
                       0.0f,
                       1.0f);
  RNA_def_float_factor(ot->srna,
                       "softness",
                       defaults.softness,
                       0.0f,
                       1.0f,
                       "Softness",
                       "Width of the sharpening, from crisp (0) to broad (1)",
                       0.0f,
                       1.0f);
  RNA_def_int(ot->srna,
              "repeat_polish",
              defaults.repeat_polish,
              0,
              100,
              "Polish Repeats",
              "Number of polish passes",
              0,
              10);
  RNA_def_int(ot->srna,
              "repeat_sharpen",
              defaults.repeat_sharpen,
              0,
              100,
              "Sharpen Repeats",
              "Number of sharpen passes after each polish pass",
              0,
              10);
  RNA_def_float_factor(ot->srna,
                       "inflate_outer",
                       defaults.inflate_outer,
                       0.0f,
                       1.0f,
                       "Inflate Outer",
                       "Push convex areas outwards afterwards, to make up for lost volume",
                       0.0f,
                       1.0f);
  RNA_def_float_factor(ot->srna,
                       "inflate_inner",
                       defaults.inflate_inner,
                       0.0f,
                       1.0f,
                       "Inflate Inner",
                       "Push concave areas inwards afterwards, to deepen them again",
                       0.0f,
                       1.0f);
}

}  // namespace blender::ed::sculpt_paint
