/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup edsculpt
 *
 * Phantom: operators for the sculpt history (see #BKE_phantom_history.hh): go to a step, scrub
 * the timeline, delete the history. Going to a step is one undo step, like any sculpt edit, and
 * isn't recorded in the history itself: undoing it puts the mesh back in the step it left
 * (#undo::history_jump_begin).
 */

#include <fmt/format.h>

#include "MEM_guardedalloc.h"

#include "DNA_mesh_types.h"
#include "DNA_object_types.h"

#include "BLT_translation.hh"

#include "BKE_context.hh"
#include "BKE_mesh.hh"
#include "BKE_multires.hh"
#include "BKE_report.hh"
#include "BKE_object_types.hh"
#include "BKE_paint.hh"
#include "BKE_paint_bvh.hh"
#include "BKE_phantom_history.hh"

#include "DEG_depsgraph.hh"

#include "BKE_undo_system.hh"

#include "ED_screen.hh"
#include "ED_sculpt.hh"
#include "ED_undo.hh"

#include "UI_interface_icons.hh"

#include "RNA_access.hh"
#include "RNA_define.hh"

#include "WM_api.hh"
#include "WM_types.hh"

#include "sculpt_intern.hh"
#include "sculpt_undo.hh"

namespace blender::ed::sculpt_paint {

static PhantomHistory *active_history(bContext *C)
{
  Object *ob = CTX_data_active_object(C);
  if (ob == nullptr || ob->type != OB_MESH || ob->phantom_history == nullptr ||
      ob->phantom_history->steps_num == 0)
  {
    return nullptr;
  }
  return ob->phantom_history;
}

static bool history_poll(bContext *C)
{
  if (!sculpt_mode_poll(C) || active_history(C) == nullptr) {
    return false;
  }
  const Object &ob = *CTX_data_active_object(C);
  const bke::pbvh::Tree *pbvh = bke::object::pbvh_get(ob);
  if (pbvh && pbvh->type() == bke::pbvh::Type::BMesh) {
    CTX_wm_operator_poll_msg_set(C, "The sculpt history doesn't work with Dyntopo");
    return false;
  }
  return true;
}

/**
 * Puts the mesh (or its Multires grids) in the state of step `index` and updates the sculpt
 * session for it. False when the step can't be shown now (another subdivision level).
 */
static bool apply_step(bContext *C, Object &ob, int index)
{
  using bke::phantom_history::ApplyResult;
  bke::phantom_history::ScopedPause pause;
  PhantomHistory &history = *ob.phantom_history;
  index = std::clamp(index, 0, history.steps_num - 1);
  Depsgraph &depsgraph = *CTX_data_ensure_evaluated_depsgraph(C);
  SculptSession *ss = ob.runtime->sculpt_session;
  if (ss && ss->subdiv_ccg && ss->subdiv_ccg->masks.is_empty() &&
      bke::phantom_history::step_has_mask(history, index))
  {
    /* The grids get a mask layer to hold the step's mask. */
    if (MultiresModifierData *mmd = BKE_sculpt_multires_active(CTX_data_scene(C), &ob)) {
      BKE_sculpt_mask_layers_ensure(&depsgraph, CTX_data_main(C), &ob, mmd);
    }
  }

  const ApplyResult result = bke::phantom_history::apply_step(ob, index);
  if (result == ApplyResult::Failed) {
    return false;
  }
  Mesh &mesh = *id_cast<Mesh *>(ob.data);
  bke::pbvh::Tree *pbvh = bke::object::pbvh_get(ob);
  ss = ob.runtime->sculpt_session;
  /* With deform modifiers or shape keys, the sculpted positions aren't the mesh's own. */
  const bool in_place = result == ApplyResult::Positions && pbvh && ss &&
                        (pbvh->type() == bke::pbvh::Type::Grids ||
                         (!ss->deform_modifiers_active && ss->shapekey_active == nullptr));
  if (in_place) {
    IndexMaskMemory memory;
    const IndexMask node_mask = bke::pbvh::all_leaf_nodes(*pbvh, memory);
    if (pbvh->type() == bke::pbvh::Type::Grids) {
      bke::pbvh::update_mask_grids(*ss->subdiv_ccg, node_mask, *pbvh);
      multires_mark_as_modified(&depsgraph, &ob, MULTIRES_COORDS_MODIFIED);
    }
    else {
      bke::pbvh::update_mask_mesh(mesh, node_mask, *pbvh);
    }
    pbvh->tag_positions_changed(node_mask);
    pbvh->tag_masks_changed(node_mask);
    pbvh->update_bounds(depsgraph, ob);
    flush_update_step(C, UpdateType::Position);
    flush_update_done(C, ob, UpdateType::Position);
    flush_update_step(C, UpdateType::Mask);
    flush_update_done(C, ob, UpdateType::Mask);
    return true;
  }
  BKE_sculptsession_free_pbvh(ob);
  BKE_mesh_batch_cache_dirty_tag(&mesh, BKE_MESH_BATCH_DIRTY_ALL);
  DEG_id_tag_update(&ob.id, ID_RECALC_GEOMETRY);
  WM_event_add_notifier(C, NC_GEOM | ND_DATA, &mesh);
  return true;
}

static void report_other_level(wmOperator *op)
{
  BKE_report(op->reports,
             RPT_WARNING,
             "That history step was sculpted at another Multires level (or without Multires)");
}

/** Starts the undo step of a jump on the timeline. */
static void jump_begin(bContext *C, Object &ob, const wmOperator *op)
{
  /* The undo step needs the sculpt session's PBVH. */
  bke::object::pbvh_ensure(*CTX_data_ensure_evaluated_depsgraph(C), ob);
  undo::history_jump_begin(*CTX_data_scene(C), ob, op);
}

namespace undo {

void history_apply_cursor(bContext *C, const StringRef object_name, const int cursor)
{
  Object *ob = CTX_data_active_object(C);
  if (ob == nullptr || StringRef(ob->id.name) != object_name || ob->phantom_history == nullptr) {
    return;
  }
  const int index = bke::phantom_history::find_cursor(*ob->phantom_history, cursor);
  if (index != -1) {
    apply_step(C, *ob, index);
    WM_main_add_notifier(NC_OBJECT | ND_DRAW, ob);
  }
}

}  // namespace undo

/* -------------------------------------------------------------------- */
/** \name Go to Step
 * \{ */

static wmOperatorStatus history_goto_exec(bContext *C, wmOperator *op)
{
  Object &ob = *CTX_data_active_object(C);
  PhantomHistory &history = *ob.phantom_history;
  const int index = std::clamp(RNA_int_get(op->ptr, "index"), 0, history.steps_num - 1);
  if (index == history.current) {
    return OPERATOR_CANCELLED;
  }
  jump_begin(C, ob, op);
  const bool applied = apply_step(C, ob, index);
  undo::history_jump_end(ob);
  if (!applied) {
    report_other_level(op);
  }
  WM_event_add_notifier(C, NC_OBJECT | ND_DRAW, &ob);
  return OPERATOR_FINISHED;
}

void SCULPT_OT_phantom_history_goto(wmOperatorType *ot)
{
  ot->name = "Go to History Step";
  ot->idname = "SCULPT_OT_phantom_history_goto";
  ot->description = "Bring the mesh back to a step of its sculpt history";

  ot->exec = history_goto_exec;
  ot->poll = history_poll;

  ot->flag = OPTYPE_REGISTER | OPTYPE_UNDO;

  RNA_def_int(ot->srna, "index", 0, 0, INT_MAX, "Index", "Step to go to", 0, 1000);
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Scrub
 * \{ */

struct ScrubData {
  int start_index;
};

static int scrub_index(const wmOperator &op, const PhantomHistory &history, const wmEvent &event)
{
  /* The timeline runs from `start` to `end` along x, or y for a bar docked to a side. */
  const float start = RNA_float_get(op.ptr, "start");
  const float end = RNA_float_get(op.ptr, "end");
  const bool vertical = RNA_boolean_get(op.ptr, "vertical");
  if (history.steps_num < 2 || start == end) {
    return history.current;
  }
  const float pos = float(vertical ? event.mval[1] : event.mval[0]);
  const float t = std::clamp((pos - start) / (end - start), 0.0f, 1.0f);
  return int(std::round(t * float(history.steps_num - 1)));
}

static void scrub_status(bContext *C, const PhantomHistory &history)
{
  const PhantomHistoryStep &step = history.steps[history.current];
  const std::string text = fmt::format(fmt::runtime(IFACE_("History: step {} of {} ({})")),
                                       history.current + 1,
                                       history.steps_num,
                                       step.name);
  ED_area_status_text(CTX_wm_area(C), text.c_str());
}

static void scrub_end(bContext *C, wmOperator *op)
{
  Object &ob = *CTX_data_active_object(C);
  undo::history_jump_end(ob);
  ED_area_status_text(CTX_wm_area(C), nullptr);
  MEM_delete(static_cast<ScrubData *>(op->customdata));
  op->customdata = nullptr;
}

static wmOperatorStatus history_scrub_invoke(bContext *C, wmOperator *op, const wmEvent *event)
{
  Object &ob = *CTX_data_active_object(C);
  PhantomHistory &history = *ob.phantom_history;

  ScrubData *data = MEM_new<ScrubData>(__func__);
  data->start_index = history.current;
  op->customdata = data;

  jump_begin(C, ob, op);
  const int index = scrub_index(*op, history, *event);
  if (index != history.current && !apply_step(C, ob, index)) {
    report_other_level(op);
  }
  scrub_status(C, history);
  WM_event_add_modal_handler(C, op);
  return OPERATOR_RUNNING_MODAL;
}

static wmOperatorStatus history_scrub_modal(bContext *C, wmOperator *op, const wmEvent *event)
{
  Object &ob = *CTX_data_active_object(C);
  PhantomHistory &history = *ob.phantom_history;
  switch (event->type) {
    case MOUSEMOVE: {
      const int index = scrub_index(*op, history, *event);
      if (index != history.current && apply_step(C, ob, index)) {
        scrub_status(C, history);
        WM_event_add_notifier(C, NC_OBJECT | ND_DRAW, &ob);
      }
      return OPERATOR_RUNNING_MODAL;
    }
    case LEFTMOUSE:
      if (event->val == KM_RELEASE) {
        scrub_end(C, op);
        return OPERATOR_FINISHED;
      }
      return OPERATOR_RUNNING_MODAL;
    case EVT_ESCKEY:
    case RIGHTMOUSE: {
      const ScrubData &data = *static_cast<ScrubData *>(op->customdata);
      if (data.start_index != history.current) {
        apply_step(C, ob, data.start_index);
      }
      scrub_end(C, op);
      WM_event_add_notifier(C, NC_OBJECT | ND_DRAW, &ob);
      return OPERATOR_CANCELLED;
    }
    default:
      return OPERATOR_RUNNING_MODAL;
  }
}

void SCULPT_OT_phantom_history_scrub(wmOperatorType *ot)
{
  ot->name = "Scrub History";
  ot->idname = "SCULPT_OT_phantom_history_scrub";
  ot->description = "Drag along the timeline to go through the sculpt history";

  ot->invoke = history_scrub_invoke;
  ot->modal = history_scrub_modal;
  ot->poll = history_poll;

  /* The undo step is pushed by hand, when the drag ends. */
  ot->flag = OPTYPE_BLOCKING;

  PropertyRNA *prop = RNA_def_float(ot->srna,
                                    "start",
                                    0.0f,
                                    -FLT_MAX,
                                    FLT_MAX,
                                    "Start",
                                    "Where the first step is on the timeline (region pixels)",
                                    0,
                                    1);
  RNA_def_property_flag(prop, PropertyFlag(PROP_HIDDEN | PROP_SKIP_SAVE));
  prop = RNA_def_float(ot->srna,
                       "end",
                       1.0f,
                       -FLT_MAX,
                       FLT_MAX,
                       "End",
                       "Where the last step is on the timeline (region pixels)",
                       0,
                       1);
  RNA_def_property_flag(prop, PropertyFlag(PROP_HIDDEN | PROP_SKIP_SAVE));
  prop = RNA_def_boolean(
      ot->srna, "vertical", false, "Vertical", "The timeline runs along y (docked to a side)");
  RNA_def_property_flag(prop, PropertyFlag(PROP_HIDDEN | PROP_SKIP_SAVE));
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Delete
 * \{ */

static bool history_delete_poll(bContext *C)
{
  const Object *ob = CTX_data_active_object(C);
  return ob && ob->phantom_history;
}

static wmOperatorStatus history_delete_exec(bContext *C, wmOperator * /*op*/)
{
  Object &ob = *CTX_data_active_object(C);
  bke::phantom_history::remove(ob);
  WM_event_add_notifier(C, NC_OBJECT | ND_DRAW, &ob);
  WM_file_tag_modified();
  return OPERATOR_FINISHED;
}

static wmOperatorStatus history_delete_invoke(bContext *C,
                                              wmOperator *op,
                                              const wmEvent * /*event*/)
{
  const Object &ob = *CTX_data_active_object(C);
  const std::string message = fmt::format(
      fmt::runtime(IFACE_("Are you sure you want to delete history for {}?")), ob.id.name + 2);
  return WM_operator_confirm_ex(
      C, op, IFACE_("Delete History"), message.c_str(), IFACE_("Confirm"), ui::AlertIcon::Warning);
}

void OBJECT_OT_phantom_history_delete(wmOperatorType *ot)
{
  ot->name = "Delete Sculpt History";
  ot->idname = "OBJECT_OT_phantom_history_delete";
  ot->description = "Delete the object's sculpt history (the mesh stays as it is)";

  ot->invoke = history_delete_invoke;
  ot->exec = history_delete_exec;
  ot->poll = history_delete_poll;

  ot->flag = OPTYPE_REGISTER;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Delete the Later Steps
 * \{ */

static wmOperatorStatus history_truncate_exec(bContext *C, wmOperator * /*op*/)
{
  Object &ob = *CTX_data_active_object(C);
  PhantomHistory &history = *ob.phantom_history;
  if (history.current >= history.steps_num - 1) {
    return OPERATOR_CANCELLED;
  }
  const int current = bke::phantom_history::cursor_get(ob);
  bke::phantom_history::truncate_after_current(history);
  undo::history_steps_deleted(ob, current);
  WM_event_add_notifier(C, NC_OBJECT | ND_DRAW, &ob);
  WM_file_tag_modified();
  return OPERATOR_FINISHED;
}

void SCULPT_OT_phantom_history_truncate(wmOperatorType *ot)
{
  ot->name = "Delete Later History Steps";
  ot->idname = "SCULPT_OT_phantom_history_truncate";
  ot->description =
      "Delete the sculpt history steps after the current one, to sculpt on from this step";

  ot->exec = history_truncate_exec;
  ot->poll = history_poll;

  ot->flag = OPTYPE_REGISTER;
}

/** \} */

}  // namespace blender::ed::sculpt_paint
