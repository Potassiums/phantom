/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup bke
 */

#include <cstring>

#include "MEM_guardedalloc.h"

#include "BLI_index_mask.hh"
#include "BLI_string.hh"
#include "BLI_offset_indices.hh"
#include "BLI_task.hh"

#include "BLT_translation.hh"

#include "DNA_mesh_types.h"
#include "DNA_object_types.h"
#include "DNA_userdef_types.h"

#include "BKE_mesh.hh"
#include "BKE_phantom_history.hh"
#include "BKE_report.hh"

#include "BLO_read_write.hh"

namespace blender::bke::phantom_history {

/** What #PhantomHistory.runtime points to: the shape at the current step. */
struct Runtime {
  Array<float3> positions;
  int faces_num = 0;
  int corners_num = 0;
};

/** Number of live #ScopedPause. */
static int pause_count = 0;

ScopedPause::ScopedPause()
{
  pause_count++;
}

ScopedPause::~ScopedPause()
{
  pause_count--;
}

static Mesh *object_mesh(Object &ob)
{
  return ob.type == OB_MESH ? id_cast<Mesh *>(ob.data) : nullptr;
}

static Span<float3> step_positions(const PhantomHistoryStep &step)
{
  return {reinterpret_cast<const float3 *>(step.positions), step.verts_num};
}

static void free_runtime(PhantomHistory &history)
{
  MEM_delete(static_cast<Runtime *>(history.runtime));
  history.runtime = nullptr;
}

static void free_step(PhantomHistoryStep &step)
{
  MEM_SAFE_DELETE(step.positions);
  MEM_SAFE_DELETE(step.face_offsets);
  MEM_SAFE_DELETE(step.corner_verts);
  MEM_SAFE_DELETE(step.delta_indices);
  MEM_SAFE_DELETE(step.delta_positions);
}

void free(PhantomHistory *history)
{
  if (history == nullptr) {
    return;
  }
  for (const int i : IndexRange(history->steps_num)) {
    free_step(history->steps[i]);
  }
  MEM_SAFE_DELETE(history->steps);
  free_runtime(*history);
  MEM_delete(history);
}

PhantomHistory &ensure(Object &ob)
{
  if (ob.phantom_history == nullptr) {
    ob.phantom_history = MEM_new<PhantomHistory>(__func__);
    ob.phantom_history->flag = PHANTOM_HISTORY_RECORD | PHANTOM_HISTORY_SAVE;
  }
  return *ob.phantom_history;
}

void remove(Object &ob)
{
  free(ob.phantom_history);
  ob.phantom_history = nullptr;
}

bool is_recording(const Object &ob)
{
  /* The preference turns it on for every object; an object's own history can be paused. */
  return !U.sculpt_history_off && ob.type == OB_MESH &&
         (ob.phantom_history == nullptr || (ob.phantom_history->flag & PHANTOM_HISTORY_RECORD));
}

static int keyframe_of(const PhantomHistory &history, const int index)
{
  for (int i = index; i > 0; i--) {
    if (history.steps[i].flag & PHANTOM_HISTORY_STEP_KEYFRAME) {
      return i;
    }
  }
  return 0;
}

StepShape step_shape(const PhantomHistory &history, const int index)
{
  StepShape shape;
  shape.keyframe = keyframe_of(history, index);
  shape.positions = step_positions(history.steps[shape.keyframe]);
  MutableSpan<float3> positions = shape.positions;
  for (int i = shape.keyframe + 1; i <= index; i++) {
    const PhantomHistoryStep &step = history.steps[i];
    const Span<int> indices(step.delta_indices, step.delta_num);
    const Span<float3> values(reinterpret_cast<const float3 *>(step.delta_positions),
                              step.delta_num);
    threading::parallel_for(indices.index_range(), 4096, [&](const IndexRange range) {
      for (const int j : range) {
        positions[indices[j]] = values[j];
      }
    });
  }
  return shape;
}

static Runtime &ensure_runtime(PhantomHistory &history)
{
  if (history.runtime == nullptr) {
    StepShape shape = step_shape(history, history.current);
    const PhantomHistoryStep &keyframe = history.steps[shape.keyframe];
    Runtime *runtime = MEM_new<Runtime>(__func__);
    runtime->positions = std::move(shape.positions);
    runtime->faces_num = keyframe.faces_num;
    runtime->corners_num = keyframe.corners_num;
    history.runtime = runtime;
  }
  return *static_cast<Runtime *>(history.runtime);
}

/** Appends an empty step (the array grows by one; it is never long). */
static PhantomHistoryStep &append_step(PhantomHistory &history, const StringRef name)
{
  PhantomHistoryStep *steps = MEM_new_array<PhantomHistoryStep>(history.steps_num + 1, __func__);
  if (history.steps_num > 0) {
    memcpy(steps, history.steps, sizeof(PhantomHistoryStep) * history.steps_num);
  }
  MEM_SAFE_DELETE(history.steps);
  history.steps = steps;
  PhantomHistoryStep &step = steps[history.steps_num++];
  step.number = history.next_number++;
  name.copy_utf8_truncated(step.name);
  return step;
}

/** Removes step `index`, already freed. */
static void erase_step(PhantomHistory &history, const int index)
{
  memmove(history.steps + index,
          history.steps + index + 1,
          sizeof(PhantomHistoryStep) * (history.steps_num - index - 1));
  history.steps_num--;
}

static void set_keyframe(PhantomHistoryStep &step, const Mesh &mesh)
{
  step.flag |= PHANTOM_HISTORY_STEP_KEYFRAME;
  step.verts_num = mesh.verts_num;
  step.faces_num = mesh.faces_num;
  step.corners_num = mesh.corners_num;
  step.positions = MEM_new_array_uninitialized<float>(size_t(mesh.verts_num) * 3, __func__);
  MutableSpan<float>(step.positions, int64_t(mesh.verts_num) * 3)
      .copy_from(mesh.vert_positions().cast<float>());
  step.face_offsets = MEM_new_array_uninitialized<int>(size_t(mesh.faces_num) + 1, __func__);
  if (mesh.faces_num > 0) {
    memcpy(step.face_offsets, mesh.face_offsets().data(), sizeof(int) * (mesh.faces_num + 1));
  }
  else {
    step.face_offsets[0] = 0;
  }
  step.corner_verts = MEM_new_array_uninitialized<int>(size_t(mesh.corners_num), __func__);
  if (mesh.corners_num > 0) {
    memcpy(step.corner_verts, mesh.corner_verts().data(), sizeof(int) * mesh.corners_num);
  }
}

static void set_runtime_from_mesh(PhantomHistory &history, const Mesh &mesh)
{
  free_runtime(history);
  Runtime *runtime = MEM_new<Runtime>(__func__);
  runtime->positions = mesh.vert_positions();
  runtime->faces_num = mesh.faces_num;
  runtime->corners_num = mesh.corners_num;
  history.runtime = runtime;
}

void begin_edit(Object &ob, const StringRef /*name*/)
{
  if (pause_count > 0 || !is_recording(ob)) {
    return;
  }
  const Mesh *mesh = object_mesh(ob);
  if (mesh == nullptr) {
    return;
  }
  /* The first edit of an object starts its history. */
  PhantomHistory &history = ensure(ob);
  if (history.steps_num > 0) {
    return;
  }
  set_keyframe(append_step(history, IFACE_("Start")), *mesh);
  history.current = 0;
  set_runtime_from_mesh(history, *mesh);
}

void apply_limit(PhantomHistory &history)
{
  if (history.limit <= 0) {
    return;
  }
  while (history.steps_num > std::max(history.limit, 1) && history.current > 0) {
    PhantomHistoryStep &first = history.steps[0];
    PhantomHistoryStep &second = history.steps[1];
    if (second.flag & PHANTOM_HISTORY_STEP_KEYFRAME) {
      free_step(first);
      erase_step(history, 0);
    }
    else {
      /* Fold the second step into the first keyframe, which then has its shape. */
      MutableSpan<float3> positions(reinterpret_cast<float3 *>(first.positions), first.verts_num);
      for (const int i : IndexRange(second.delta_num)) {
        positions[second.delta_indices[i]] = reinterpret_cast<const float3 *>(
            second.delta_positions)[i];
      }
      first.number = second.number;
      memcpy(first.name, second.name, sizeof(first.name));
      free_step(second);
      erase_step(history, 1);
    }
    history.current--;
  }
}

void record(Object &ob, const StringRef name, bool topology_changed)
{
  if (pause_count > 0 || !is_recording(ob)) {
    return;
  }
  const Mesh *mesh = object_mesh(ob);
  if (mesh == nullptr) {
    return;
  }
  PhantomHistory &history = ensure(ob);
  if (history.steps_num == 0) {
    /* Nothing to compare to: the mesh as it is now is where the history starts. */
    set_keyframe(append_step(history, name), *mesh);
    history.current = 0;
    set_runtime_from_mesh(history, *mesh);
    return;
  }

  Runtime &runtime = ensure_runtime(history);
  const Span<float3> positions = mesh->vert_positions();
  topology_changed |= positions.size() != runtime.positions.size() ||
                      mesh->faces_num != runtime.faces_num ||
                      mesh->corners_num != runtime.corners_num;

  IndexMaskMemory memory;
  IndexMask moved;
  if (!topology_changed) {
    /* Compare bits, so the step restores exactly. */
    moved = IndexMask::from_predicate(positions.index_range(), memory, [&](const int64_t i) {
      return memcmp(&positions[i], &runtime.positions[i], sizeof(float3)) != 0;
    });
    if (moved.is_empty()) {
      /* A mask or face set edit: no new shape. */
      return;
    }
  }

  /* Editing from the middle of the history drops the steps after it. */
  for (int i = history.current + 1; i < history.steps_num; i++) {
    free_step(history.steps[i]);
  }
  history.steps_num = history.current + 1;

  PhantomHistoryStep &step = append_step(history, name);
  if (topology_changed) {
    set_keyframe(step, *mesh);
    set_runtime_from_mesh(history, *mesh);
  }
  else {
    step.delta_num = int(moved.size());
    step.delta_indices = MEM_new_array_uninitialized<int>(moved.size(), __func__);
    step.delta_positions = MEM_new_array_uninitialized<float>(moved.size() * 3, __func__);
    moved.to_indices<int>({step.delta_indices, step.delta_num});
    float3 *values = reinterpret_cast<float3 *>(step.delta_positions);
    moved.foreach_index([&](const int64_t i, const int64_t pos) {
      values[pos] = positions[i];
      runtime.positions[i] = positions[i];
    });
  }
  history.current = history.steps_num - 1;
  apply_limit(history);
}

bool apply_step(Object &ob, int index)
{
  PhantomHistory *history = ob.phantom_history;
  Mesh *mesh = object_mesh(ob);
  if (history == nullptr || history->steps_num == 0 || mesh == nullptr) {
    return false;
  }
  index = std::clamp(index, 0, history->steps_num - 1);
  StepShape shape = step_shape(*history, index);
  const PhantomHistoryStep &keyframe = history->steps[shape.keyframe];

  /* Within the current keyframe's span the topology is the mesh's: write positions only, which
   * keeps all other attributes. Across a topology change, replace the mesh. */
  const bool same_topology = shape.keyframe == keyframe_of(*history, history->current) &&
                             keyframe.verts_num == mesh->verts_num &&
                             keyframe.faces_num == mesh->faces_num &&
                             keyframe.corners_num == mesh->corners_num;
  if (same_topology) {
    mesh->vert_positions_for_write().copy_from(shape.positions);
    mesh->tag_positions_changed();
  }
  else {
    Mesh *new_mesh = BKE_mesh_new_nomain(
        keyframe.verts_num, 0, keyframe.faces_num, keyframe.corners_num);
    BKE_mesh_copy_parameters(new_mesh, mesh);
    new_mesh->vert_positions_for_write().copy_from(shape.positions);
    if (keyframe.faces_num > 0) {
      new_mesh->face_offsets_for_write().copy_from({keyframe.face_offsets, keyframe.faces_num + 1});
    }
    new_mesh->corner_verts_for_write().copy_from({keyframe.corner_verts, keyframe.corners_num});
    mesh_calc_edges(*new_mesh, false, false);
    BKE_mesh_nomain_to_mesh(new_mesh, mesh, &ob);
  }

  free_runtime(*history);
  Runtime *runtime = MEM_new<Runtime>(__func__);
  runtime->positions = std::move(shape.positions);
  runtime->faces_num = keyframe.faces_num;
  runtime->corners_num = keyframe.corners_num;
  history->runtime = runtime;
  history->current = index;
  return !same_topology;
}

int64_t memory_bytes(const PhantomHistory &history)
{
  int64_t bytes = sizeof(PhantomHistory) + sizeof(PhantomHistoryStep) * history.steps_num;
  for (const int i : IndexRange(history.steps_num)) {
    const PhantomHistoryStep &step = history.steps[i];
    if (step.positions) {
      bytes += sizeof(float3) * int64_t(step.verts_num) + sizeof(int) * (step.faces_num + 1) +
               sizeof(int) * int64_t(step.corners_num);
    }
    bytes += (sizeof(int) + sizeof(float3)) * int64_t(step.delta_num);
  }
  return bytes;
}

void blend_write(BlendWriter &writer, const PhantomHistory &history)
{
  /* Steps also go into undo steps (memory files), or undoing out of Sculpt Mode would lose
   * them; unchanged ones are shared between undo steps. */
  const bool write_steps = (history.flag & PHANTOM_HISTORY_SAVE) || writer.is_undo();
  PhantomHistory history_copy = history;
  history_copy.runtime = nullptr;
  if (!write_steps) {
    history_copy.steps = nullptr;
    history_copy.steps_num = 0;
    history_copy.current = 0;
  }
  writer.write_struct_at_address(&history, &history_copy);
  if (!write_steps || history.steps_num == 0) {
    return;
  }
  writer.write_struct_array(history.steps_num, history.steps);
  for (const int i : IndexRange(history.steps_num)) {
    const PhantomHistoryStep &step = history.steps[i];
    if (step.flag & PHANTOM_HISTORY_STEP_KEYFRAME) {
      writer.write_float_array(int64_t(step.verts_num) * 3, step.positions);
      writer.write_int32_array(int64_t(step.faces_num) + 1, step.face_offsets);
      writer.write_int32_array(step.corners_num, step.corner_verts);
    }
    else {
      writer.write_int32_array(step.delta_num, step.delta_indices);
      writer.write_float_array(int64_t(step.delta_num) * 3, step.delta_positions);
    }
  }
}

/** Reads every array of the step (so all its pointers are valid or null), then checks them. */
static bool read_step(BlendDataReader &reader, PhantomHistoryStep &step, const int keyframe_verts)
{
  bool ok = true;
  if (step.flag & PHANTOM_HISTORY_STEP_KEYFRAME) {
    ok &= step.verts_num >= 0 && step.faces_num >= 0 && step.corners_num >= 0;
    ok &= BLO_read_array(&reader, &step.positions, int64_t(std::max(step.verts_num, 0)) * 3);
    ok &= BLO_read_array(&reader, &step.face_offsets, int64_t(std::max(step.faces_num, 0)) + 1);
    ok &= BLO_read_array(&reader, &step.corner_verts, std::max(step.corners_num, 0));
    step.delta_indices = nullptr;
    step.delta_positions = nullptr;
    if (!ok || step.positions == nullptr || step.face_offsets == nullptr ||
        (step.corners_num > 0 && step.corner_verts == nullptr))
    {
      return false;
    }
    /* Faces must cover the corners in order, and corners must use existing vertices. */
    if (step.face_offsets[0] != 0 || step.face_offsets[step.faces_num] != step.corners_num) {
      return false;
    }
    for (const int i : IndexRange(step.faces_num)) {
      if (step.face_offsets[i + 1] - step.face_offsets[i] < 3) {
        return false;
      }
    }
    for (const int i : IndexRange(step.corners_num)) {
      if (step.corner_verts[i] < 0 || step.corner_verts[i] >= step.verts_num) {
        return false;
      }
    }
    return true;
  }
  ok &= step.delta_num >= 0;
  ok &= BLO_read_array(&reader, &step.delta_indices, std::max(step.delta_num, 0));
  ok &= BLO_read_array(&reader, &step.delta_positions, int64_t(std::max(step.delta_num, 0)) * 3);
  step.positions = nullptr;
  step.face_offsets = nullptr;
  step.corner_verts = nullptr;
  if (!ok || (step.delta_num > 0 && (!step.delta_indices || !step.delta_positions))) {
    return false;
  }
  for (const int i : IndexRange(step.delta_num)) {
    if (step.delta_indices[i] < 0 || step.delta_indices[i] >= keyframe_verts) {
      return false;
    }
  }
  return true;
}

void blend_read(BlendDataReader &reader, Object &ob)
{
  BLO_read_struct(&reader, PhantomHistory, &ob.phantom_history);
  PhantomHistory *history = ob.phantom_history;
  if (history == nullptr) {
    return;
  }
  history->runtime = nullptr;

  bool ok = history->steps_num >= 0;
  if (history->steps_num > 0) {
    ok &= BLO_read_array(&reader, &history->steps, history->steps_num);
    if (history->steps == nullptr) {
      history->steps_num = 0;
      ok = false;
    }
  }
  else {
    history->steps = nullptr;
    history->steps_num = 0;
  }

  /* Read all steps even after a failure, so freeing only meets valid pointers. */
  int keyframe_verts = -1;
  for (const int i : IndexRange(history->steps_num)) {
    PhantomHistoryStep &step = history->steps[i];
    if (i == 0 && !(step.flag & PHANTOM_HISTORY_STEP_KEYFRAME)) {
      /* The first step must be a keyframe; treat it as one to read it safely. */
      step.flag |= PHANTOM_HISTORY_STEP_KEYFRAME;
      ok = false;
    }
    if (step.flag & PHANTOM_HISTORY_STEP_KEYFRAME) {
      keyframe_verts = step.verts_num;
    }
    ok &= read_step(reader, step, keyframe_verts);
  }
  if (history->steps_num > 0) {
    ok &= history->current >= 0 && history->current < history->steps_num;
  }
  else {
    history->current = 0;
  }

  if (!ok) {
    BLO_reportf_wrap(BLO_read_data_reports(&reader),
                     RPT_WARNING,
                     RPT_("The sculpt history of \"%s\" could not be read and was removed"),
                     ob.id.name + 2);
    free(history);
    ob.phantom_history = nullptr;
  }
}

}  // namespace blender::bke::phantom_history
