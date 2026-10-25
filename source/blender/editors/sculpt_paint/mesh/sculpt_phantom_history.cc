/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup edsculpt
 *
 * Phantom: operators for the sculpt history (see #BKE_phantom_history.hh): go to a step, scrub
 * the timeline, delete the history. Going to a step is one undo step, like any sculpt edit, and
 * isn't recorded in the history itself.
 */

#include <fmt/format.h>

#include "MEM_guardedalloc.h"

#include "DNA_mesh_types.h"
#include "DNA_object_types.h"

#include "BLT_translation.hh"

#include "BKE_context.hh"
#include "BKE_mesh.hh"
#include "BKE_object_types.hh"
#include "BKE_paint.hh"
#include "BKE_paint_bvh.hh"
#include "BKE_phantom_history.hh"

#include "DEG_depsgraph.hh"

#include "ED_screen.hh"
#include "ED_sculpt.hh"

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
  if (pbvh && pbvh->type() != bke::pbvh::Type::Mesh) {
    CTX_wm_operator_poll_msg_set(C, "The sculpt history doesn't work with Dyntopo or Multires");
    return false;
  }
  return true;
}

/** Puts the mesh in the state of step `index` and updates the sculpt session for it. */
static void apply_step(bContext *C, Object &ob, const int index)
{
  bke::phantom_history::ScopedPause pause;
  Mesh &mesh = *id_cast<Mesh *>(ob.data);
  const bool replaced = bke::phantom_history::apply_step(ob, index);

  bke::pbvh::Tree *pbvh = bke::object::pbvh_get(ob);
  const SculptSession *ss = ob.runtime->sculpt_session;
  /* With deform modifiers or shape keys, the sculpted positions aren't the mesh's own. */
  const bool positions_only = !replaced && pbvh && ss && !ss->deform_modifiers_active &&
                              ss->shapekey_active == nullptr;
  if (positions_only) {
    const Depsgraph &depsgraph = *CTX_data_depsgraph_pointer(C);
    IndexMaskMemory memory;
    const IndexMask node_mask = bke::pbvh::all_leaf_nodes(*pbvh, memory);
    pbvh->tag_positions_changed(node_mask);
    pbvh->update_bounds(depsgraph, ob);
    flush_update_step(C, UpdateType::Position);
    flush_update_done(C, ob, UpdateType::Position);
    return;
  }
  BKE_sculptsession_free_pbvh(ob);
  BKE_mesh_batch_cache_dirty_tag(&mesh, BKE_MESH_BATCH_DIRTY_ALL);
  DEG_id_tag_update(&ob.id, ID_RECALC_GEOMETRY);
  WM_event_add_notifier(C, NC_GEOM | ND_DATA, &mesh);
}

static void end_undo_step(Object &ob)
{
  /* The undo step is for the change of step, not a new step of the history. */
  bke::phantom_history::ScopedPause pause;
  undo::geometry_end(ob);
}

/* -------------------------------------------------------------------- */
/** \name Go to Step
 * \{ */

static wmOperatorStatus history_goto_exec(bContext *C, wmOperator *op)
{
  const Scene &scene = *CTX_data_scene(C);
  Object &ob = *CTX_data_active_object(C);
  PhantomHistory &history = *ob.phantom_history;
  const int index = std::clamp(RNA_int_get(op->ptr, "index"), 0, history.steps_num - 1);
  if (index == history.current) {
    return OPERATOR_CANCELLED;
  }
  undo::geometry_begin(scene, ob, op);
  apply_step(C, ob, index);
  end_undo_step(ob);
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
  end_undo_step(ob);
  ED_area_status_text(CTX_wm_area(C), nullptr);
  MEM_delete(static_cast<ScrubData *>(op->customdata));
  op->customdata = nullptr;
}

static wmOperatorStatus history_scrub_invoke(bContext *C, wmOperator *op, const wmEvent *event)
{
  const Scene &scene = *CTX_data_scene(C);
  Object &ob = *CTX_data_active_object(C);
  PhantomHistory &history = *ob.phantom_history;

  ScrubData *data = MEM_new<ScrubData>(__func__);
  data->start_index = history.current;
  op->customdata = data;

  undo::geometry_begin(scene, ob, op);
  const int index = scrub_index(*op, history, *event);
  if (index != history.current) {
    apply_step(C, ob, index);
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
      if (index != history.current) {
        apply_step(C, ob, index);
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

void OBJECT_OT_phantom_history_delete(wmOperatorType *ot)
{
  ot->name = "Delete Sculpt History";
  ot->idname = "OBJECT_OT_phantom_history_delete";
  ot->description = "Delete the object's sculpt history (the mesh stays as it is)";

  ot->invoke = WM_operator_confirm;
  ot->exec = history_delete_exec;
  ot->poll = history_delete_poll;

  ot->flag = OPTYPE_REGISTER;
}

/** \} */

}  // namespace blender::ed::sculpt_paint
