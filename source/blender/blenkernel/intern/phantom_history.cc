/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup bke
 */

#include <cstring>

#include "MEM_guardedalloc.h"

#include "BLI_index_mask.hh"
#include "BLI_listbase.hh"
#include "BLI_string.hh"
#include "BLI_offset_indices.hh"
#include "BLI_task.hh"

#include "BLT_translation.hh"

#include "DNA_mesh_types.h"
#include "DNA_object_types.h"
#include "DNA_userdef_types.h"

#include "BKE_attribute.hh"
#include "BKE_main.hh"
#include "BKE_mesh.hh"
#include "BKE_object_types.hh"
#include "BKE_paint.hh"
#include "BKE_phantom_history.hh"
#include "BKE_report.hh"
#include "BKE_subdiv_ccg.hh"

#include "BLO_read_write.hh"

namespace blender::bke::phantom_history {

/** The mask attribute sculpting writes. */
static constexpr StringRef MASK_ATTRIBUTE = ".sculpt_mask";

/** What the points are: mesh vertices (with faces), or Multires grid points. */
struct Layout {
  int points_num = 0;
  int faces_num = 0;
  int corners_num = 0;
  /** Grids: their number and size (0 for a mesh). */
  int grids_num = 0;
  int grid_size = 0;

  bool is_grids() const
  {
    return grids_num > 0;
  }
  bool operator==(const Layout &other) const = default;
};

static Layout step_layout(const PhantomHistoryStep &keyframe)
{
  return {keyframe.verts_num,
          keyframe.faces_num,
          keyframe.corners_num,
          keyframe.grids_num,
          keyframe.grid_size};
}

/** What #PhantomHistory.runtime points to: the shape and mask at the current step. */
struct Runtime {
  Array<float3> positions;
  Array<float> masks;
  Layout layout;
};

/** Where the object's sculpted points are now: its mesh, or its Multires grids in Sculpt Mode. */
struct State {
  Layout layout;
  Span<float3> positions;
  /** Empty when nothing is masked. */
  Span<float> masks;
  /** Owns the mesh's masks for `masks`. */
  VArraySpan<float> mesh_masks;
  const Mesh *mesh = nullptr;
  SubdivCCG *subdiv_ccg = nullptr;
};

static SubdivCCG *object_grids(const Object &ob)
{
  const SculptSession *ss = ob.runtime->sculpt_session;
  if (ob.mode & OB_MODE_SCULPT && ss && ss->bm == nullptr && ss->subdiv_ccg &&
      ss->subdiv_ccg->grids_num > 0)
  {
    return ss->subdiv_ccg;
  }
  return nullptr;
}

static bool read_state(const Object &ob, State &state)
{
  if (ob.type != OB_MESH) {
    return false;
  }
  if (SubdivCCG *subdiv_ccg = object_grids(ob)) {
    state.subdiv_ccg = subdiv_ccg;
    state.positions = subdiv_ccg->positions;
    state.masks = subdiv_ccg->masks;
    state.layout = {int(state.positions.size()), 0, 0, subdiv_ccg->grids_num, subdiv_ccg->grid_size};
    return true;
  }
  const Mesh &mesh = *id_cast<const Mesh *>(ob.data);
  state.mesh = &mesh;
  state.positions = mesh.vert_positions();
  if (const VArray<float> masks = *mesh.attributes().lookup<float>(MASK_ATTRIBUTE,
                                                                     AttrDomain::Point))
  {
    state.mesh_masks = masks;
    state.masks = state.mesh_masks;
  }
  state.layout = {mesh.verts_num, mesh.faces_num, mesh.corners_num, 0, 0};
  return true;
}

static float mask_at(const Span<float> masks, const int64_t i)
{
  return masks.is_empty() ? 0.0f : masks[i];
}

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
  MEM_SAFE_DELETE(step.masks);
  MEM_SAFE_DELETE(step.mask_delta_indices);
  MEM_SAFE_DELETE(step.mask_delta_values);
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
  return !U.sculpt_history_off && ob.type == OB_MESH;
}

int step_limit()
{
  /* Zero (preferences from before the setting) is the default; -1 keeps every step. */
  if (U.sculpt_history_limit == 0) {
    return 100;
  }
  return U.sculpt_history_limit < 0 ? 0 : U.sculpt_history_limit;
}

void apply_limit_to_all(Main &bmain)
{
  for (Object &ob : bmain.objects) {
    if (ob.phantom_history) {
      apply_limit(*ob.phantom_history);
    }
  }
}

int cursor_get(const Object &ob)
{
  const PhantomHistory *history = ob.phantom_history;
  if (history == nullptr || history->steps_num == 0) {
    return 0;
  }
  return history->steps[history->current].number + 1;
}

void cursor_set(Object &ob, const int cursor)
{
  PhantomHistory *history = ob.phantom_history;
  if (history == nullptr || cursor <= 0) {
    return;
  }
  for (const int i : IndexRange(history->steps_num)) {
    if (history->steps[i].number + 1 == cursor) {
      if (i != history->current) {
        history->current = i;
        /* The compared-against shape is rebuilt from the new current step when needed. */
        free_runtime(*history);
      }
      return;
    }
  }
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

template<typename T>
static void scatter(const Span<int> indices, const Span<T> values, MutableSpan<T> dst)
{
  threading::parallel_for(indices.index_range(), 4096, [&](const IndexRange range) {
    for (const int j : range) {
      dst[indices[j]] = values[j];
    }
  });
}

static Span<float3> delta_positions(const PhantomHistoryStep &step)
{
  return {reinterpret_cast<const float3 *>(step.delta_positions), step.delta_num};
}

StepShape step_shape(const PhantomHistory &history, const int index)
{
  StepShape shape;
  shape.keyframe = keyframe_of(history, index);
  const PhantomHistoryStep &keyframe = history.steps[shape.keyframe];
  shape.positions = step_positions(keyframe);
  shape.masks.reinitialize(keyframe.verts_num);
  if (keyframe.masks) {
    shape.masks.as_mutable_span().copy_from({keyframe.masks, keyframe.verts_num});
  }
  else {
    shape.masks.fill(0.0f);
  }
  for (int i = shape.keyframe + 1; i <= index; i++) {
    const PhantomHistoryStep &step = history.steps[i];
    scatter({step.delta_indices, step.delta_num}, delta_positions(step), shape.positions.as_mutable_span());
    scatter<float>({step.mask_delta_indices, step.mask_delta_num},
                   {step.mask_delta_values, step.mask_delta_num},
                   shape.masks);
  }
  return shape;
}

static void set_runtime(PhantomHistory &history, StepShape &&shape, const Layout &layout)
{
  free_runtime(history);
  Runtime *runtime = MEM_new<Runtime>(__func__);
  runtime->positions = std::move(shape.positions);
  runtime->masks = std::move(shape.masks);
  runtime->layout = layout;
  history.runtime = runtime;
}

static Runtime &ensure_runtime(PhantomHistory &history)
{
  if (history.runtime == nullptr) {
    StepShape shape = step_shape(history, history.current);
    const Layout layout = step_layout(history.steps[shape.keyframe]);
    set_runtime(history, std::move(shape), layout);
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

static bool any_masked(const Span<float> masks)
{
  return std::any_of(masks.begin(), masks.end(), [](const float m) { return m != 0.0f; });
}

static void set_keyframe(PhantomHistoryStep &step, const State &state)
{
  const Layout &layout = state.layout;
  step.flag |= PHANTOM_HISTORY_STEP_KEYFRAME;
  step.verts_num = layout.points_num;
  step.faces_num = layout.faces_num;
  step.corners_num = layout.corners_num;
  step.grids_num = layout.grids_num;
  step.grid_size = layout.grid_size;
  step.positions = MEM_new_array_uninitialized<float>(size_t(layout.points_num) * 3, __func__);
  MutableSpan<float>(step.positions, int64_t(layout.points_num) * 3)
      .copy_from(state.positions.cast<float>());
  step.face_offsets = MEM_new_array_uninitialized<int>(size_t(layout.faces_num) + 1, __func__);
  step.face_offsets[0] = 0;
  step.corner_verts = MEM_new_array_uninitialized<int>(size_t(layout.corners_num), __func__);
  if (state.mesh) {
    if (layout.faces_num > 0) {
      memcpy(step.face_offsets,
             state.mesh->face_offsets().data(),
             sizeof(int) * (layout.faces_num + 1));
    }
    if (layout.corners_num > 0) {
      memcpy(step.corner_verts, state.mesh->corner_verts().data(), sizeof(int) * layout.corners_num);
    }
  }
  if (any_masked(state.masks)) {
    step.masks = MEM_new_array_uninitialized<float>(size_t(layout.points_num), __func__);
    MutableSpan<float>(step.masks, layout.points_num).copy_from(state.masks);
  }
}

static void set_runtime_from_state(PhantomHistory &history, const State &state)
{
  StepShape shape;
  shape.positions = state.positions;
  shape.masks.reinitialize(state.layout.points_num);
  if (state.masks.is_empty()) {
    shape.masks.fill(0.0f);
  }
  else {
    shape.masks.as_mutable_span().copy_from(state.masks);
  }
  set_runtime(history, std::move(shape), state.layout);
}

void begin_edit(Object &ob, const StringRef /*name*/)
{
  if (pause_count > 0 || !is_recording(ob)) {
    return;
  }
  /* The first edit of an object starts its history. */
  if (ob.phantom_history && ob.phantom_history->steps_num > 0) {
    return;
  }
  State state;
  if (!read_state(ob, state)) {
    return;
  }
  PhantomHistory &history = ensure(ob);
  set_keyframe(append_step(history, IFACE_("Start")), state);
  history.current = 0;
  set_runtime_from_state(history, state);
}

void truncate_after_current(PhantomHistory &history)
{
  if (history.current >= history.steps_num - 1) {
    return;
  }
  for (int i = history.current + 1; i < history.steps_num; i++) {
    free_step(history.steps[i]);
  }
  history.steps_num = history.current + 1;
  /* Give the memory of the dropped steps' slots back too. */
  PhantomHistoryStep *steps = MEM_new_array<PhantomHistoryStep>(history.steps_num, __func__);
  memcpy(steps, history.steps, sizeof(PhantomHistoryStep) * history.steps_num);
  MEM_SAFE_DELETE(history.steps);
  history.steps = steps;
}

void apply_limit(PhantomHistory &history)
{
  const int limit = step_limit();
  if (limit <= 0) {
    return;
  }
  while (history.steps_num > std::max(limit, 1) && history.current > 0) {
    PhantomHistoryStep &first = history.steps[0];
    PhantomHistoryStep &second = history.steps[1];
    if (second.flag & PHANTOM_HISTORY_STEP_KEYFRAME) {
      free_step(first);
      erase_step(history, 0);
    }
    else {
      /* Fold the second step into the first keyframe, which then has its shape and mask. */
      scatter({second.delta_indices, second.delta_num},
              delta_positions(second),
              MutableSpan<float3>(reinterpret_cast<float3 *>(first.positions), first.verts_num));
      if (second.mask_delta_num > 0) {
        if (first.masks == nullptr) {
          first.masks = MEM_new_array<float>(size_t(first.verts_num), __func__);
        }
        scatter<float>({second.mask_delta_indices, second.mask_delta_num},
                       {second.mask_delta_values, second.mask_delta_num},
                       {first.masks, first.verts_num});
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
  State state;
  if (!read_state(ob, state)) {
    return;
  }
  PhantomHistory &history = ensure(ob);
  if (history.steps_num == 0) {
    /* Nothing to compare to: the mesh as it is now is where the history starts. */
    set_keyframe(append_step(history, name), state);
    history.current = 0;
    set_runtime_from_state(history, state);
    return;
  }

  Runtime &runtime = ensure_runtime(history);
  const Span<float3> positions = state.positions;
  topology_changed |= state.layout != runtime.layout;

  IndexMaskMemory memory;
  IndexMask moved;
  IndexMask remasked;
  if (!topology_changed) {
    /* Compare bits, so the step restores exactly. */
    moved = IndexMask::from_predicate(positions.index_range(), memory, [&](const int64_t i) {
      return memcmp(&positions[i], &runtime.positions[i], sizeof(float3)) != 0;
    });
    remasked = IndexMask::from_predicate(positions.index_range(), memory, [&](const int64_t i) {
      const float mask = mask_at(state.masks, i);
      return memcmp(&mask, &runtime.masks[i], sizeof(float)) != 0;
    });
    if (moved.is_empty() && remasked.is_empty()) {
      /* A face set or visibility edit: nothing the history keeps. */
      return;
    }
  }

  /* Editing from the middle of the history drops the steps after it. */
  truncate_after_current(history);

  PhantomHistoryStep &step = append_step(history, name);
  if (topology_changed) {
    set_keyframe(step, state);
    set_runtime_from_state(history, state);
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
    if (!remasked.is_empty()) {
      step.mask_delta_num = int(remasked.size());
      step.mask_delta_indices = MEM_new_array_uninitialized<int>(remasked.size(), __func__);
      step.mask_delta_values = MEM_new_array_uninitialized<float>(remasked.size(), __func__);
      remasked.to_indices<int>({step.mask_delta_indices, step.mask_delta_num});
      remasked.foreach_index([&](const int64_t i, const int64_t pos) {
        step.mask_delta_values[pos] = mask_at(state.masks, i);
        runtime.masks[i] = step.mask_delta_values[pos];
      });
    }
  }
  history.current = history.steps_num - 1;
  apply_limit(history);
}

bool step_has_mask(const PhantomHistory &history, const int index)
{
  const int keyframe = keyframe_of(history, index);
  if (history.steps[keyframe].masks) {
    return true;
  }
  for (int i = keyframe + 1; i <= index; i++) {
    if (history.steps[i].mask_delta_num > 0) {
      return true;
    }
  }
  return false;
}

static void write_mesh_masks(Mesh &mesh, const Span<float> masks)
{
  MutableAttributeAccessor attributes = mesh.attributes_for_write();
  if (!any_masked(masks)) {
    attributes.remove(MASK_ATTRIBUTE);
    return;
  }
  SpanAttributeWriter<float> writer = attributes.lookup_or_add_for_write_only_span<float>(
      MASK_ATTRIBUTE, AttrDomain::Point);
  writer.span.copy_from(masks);
  writer.finish();
}

ApplyResult apply_step(Object &ob, int index)
{
  PhantomHistory *history = ob.phantom_history;
  State state;
  if (history == nullptr || history->steps_num == 0 || !read_state(ob, state)) {
    return ApplyResult::Failed;
  }
  index = std::clamp(index, 0, history->steps_num - 1);
  const int keyframe_index = keyframe_of(*history, index);
  const PhantomHistoryStep &keyframe = history->steps[keyframe_index];
  const Layout layout = step_layout(keyframe);

  ApplyResult result = ApplyResult::Positions;
  if (layout.is_grids() || state.layout.is_grids()) {
    /* Grids are written in place: the step must be at the subdivision level being sculpted. */
    if (layout != state.layout) {
      return ApplyResult::Failed;
    }
    if (step_has_mask(*history, index) && state.subdiv_ccg->masks.is_empty()) {
      /* The caller adds the grids' mask layer first. */
      return ApplyResult::Failed;
    }
  }
  else if (!(keyframe_index == keyframe_of(*history, history->current) && layout == state.layout))
  {
    /* Across a topology change the mesh is replaced. */
    result = ApplyResult::Topology;
  }

  StepShape shape = step_shape(*history, index);
  if (state.subdiv_ccg) {
    SubdivCCG &subdiv_ccg = *state.subdiv_ccg;
    subdiv_ccg.positions.as_mutable_span().copy_from(shape.positions);
    if (!subdiv_ccg.masks.is_empty()) {
      subdiv_ccg.masks.as_mutable_span().copy_from(shape.masks);
    }
  }
  else if (result == ApplyResult::Positions) {
    /* Within the current keyframe's span the topology is the mesh's: write positions and masks
     * only, which keeps all other attributes. */
    Mesh &mesh = *id_cast<Mesh *>(ob.data);
    mesh.vert_positions_for_write().copy_from(shape.positions);
    mesh.tag_positions_changed();
    write_mesh_masks(mesh, shape.masks);
  }
  else {
    Mesh *mesh = id_cast<Mesh *>(ob.data);
    Mesh *new_mesh = BKE_mesh_new_nomain(
        keyframe.verts_num, 0, keyframe.faces_num, keyframe.corners_num);
    BKE_mesh_copy_parameters(new_mesh, mesh);
    new_mesh->vert_positions_for_write().copy_from(shape.positions);
    if (keyframe.faces_num > 0) {
      new_mesh->face_offsets_for_write().copy_from({keyframe.face_offsets, keyframe.faces_num + 1});
    }
    new_mesh->corner_verts_for_write().copy_from({keyframe.corner_verts, keyframe.corners_num});
    mesh_calc_edges(*new_mesh, false, false);
    write_mesh_masks(*new_mesh, shape.masks);
    BKE_mesh_nomain_to_mesh(new_mesh, mesh, &ob);
  }

  set_runtime(*history, std::move(shape), layout);
  history->current = index;
  return result;
}

int find_cursor(const PhantomHistory &history, const int cursor)
{
  for (const int i : IndexRange(history.steps_num)) {
    if (history.steps[i].number + 1 == cursor) {
      return i;
    }
  }
  return -1;
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
    if (step.masks) {
      bytes += sizeof(float) * int64_t(step.verts_num);
    }
    bytes += (sizeof(int) + sizeof(float3)) * int64_t(step.delta_num);
    bytes += (sizeof(int) + sizeof(float)) * int64_t(step.mask_delta_num);
  }
  return bytes;
}

void blend_write(BlendWriter &writer, const PhantomHistory &history)
{
  /* Steps also go into undo steps (memory files), or undoing out of Sculpt Mode would lose
   * them; unchanged ones are shared between undo steps. */
  const bool write_steps = !U.sculpt_history_nosave || writer.is_undo();
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
      if (step.masks) {
        writer.write_float_array(step.verts_num, step.masks);
      }
    }
    else {
      writer.write_int32_array(step.delta_num, step.delta_indices);
      writer.write_float_array(int64_t(step.delta_num) * 3, step.delta_positions);
      writer.write_int32_array(step.mask_delta_num, step.mask_delta_indices);
      writer.write_float_array(step.mask_delta_num, step.mask_delta_values);
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
    /* Only written when something is masked (and not at all by files from before masks). */
    if (step.masks) {
      ok &= BLO_read_array(&reader, &step.masks, std::max(step.verts_num, 0));
    }
    step.delta_indices = nullptr;
    step.delta_positions = nullptr;
    step.mask_delta_indices = nullptr;
    step.mask_delta_values = nullptr;
    step.mask_delta_num = 0;
    if (!ok || step.positions == nullptr || step.face_offsets == nullptr ||
        (step.corners_num > 0 && step.corner_verts == nullptr))
    {
      return false;
    }
    if (step.grids_num != 0 || step.grid_size != 0) {
      /* Grids: no faces, and as many points as the grids hold. */
      return step.grids_num > 0 && step.grid_size > 1 && step.faces_num == 0 &&
             step.corners_num == 0 &&
             int64_t(step.grids_num) * step.grid_size * step.grid_size == step.verts_num;
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
  ok &= step.delta_num >= 0 && step.mask_delta_num >= 0;
  ok &= BLO_read_array(&reader, &step.delta_indices, std::max(step.delta_num, 0));
  ok &= BLO_read_array(&reader, &step.delta_positions, int64_t(std::max(step.delta_num, 0)) * 3);
  ok &= BLO_read_array(&reader, &step.mask_delta_indices, std::max(step.mask_delta_num, 0));
  ok &= BLO_read_array(&reader, &step.mask_delta_values, std::max(step.mask_delta_num, 0));
  step.positions = nullptr;
  step.face_offsets = nullptr;
  step.corner_verts = nullptr;
  step.masks = nullptr;
  if (!ok || (step.delta_num > 0 && (!step.delta_indices || !step.delta_positions)) ||
      (step.mask_delta_num > 0 && (!step.mask_delta_indices || !step.mask_delta_values)))
  {
    return false;
  }
  for (const int i : IndexRange(step.delta_num)) {
    if (step.delta_indices[i] < 0 || step.delta_indices[i] >= keyframe_verts) {
      return false;
    }
  }
  for (const int i : IndexRange(step.mask_delta_num)) {
    if (step.mask_delta_indices[i] < 0 || step.mask_delta_indices[i] >= keyframe_verts) {
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
