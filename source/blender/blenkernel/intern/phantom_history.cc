/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup bke
 */

#include <cstring>

#include "MEM_guardedalloc.h"

#include "BLI_implicit_sharing.hh"
#include "BLI_index_mask.hh"
#include "BLI_map.hh"
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
/** The face sets (polygroups) attribute, on the faces of the (base) mesh. */
static constexpr StringRef FACE_SET_ATTRIBUTE = ".sculpt_face_set";
/**
 * The face set the history gives the faces of a mesh without face sets: the one sculpting fills
 * them with when it adds the attribute (#face_set::ensure_face_sets_mesh), so that is no change.
 */
static constexpr int DEFAULT_FACE_SET = 1;

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

/**
 * A keyframe's arrays shared with the mesh they were taken from instead of copied: they only take
 * memory of their own once the mesh's arrays change (sculpting copies the positions on its first
 * write anyway, as the evaluated mesh shares them too).
 */
struct SharedArrays {
  const ImplicitSharingInfo *positions = nullptr;
  const ImplicitSharingInfo *face_offsets = nullptr;
  const ImplicitSharingInfo *corner_verts = nullptr;
};

/**
 * What #PhantomHistory.runtime points to.
 *
 * The next edit is compared to the shape and mask at the current step. Rather than a copy of
 * them, this keeps for every point the number of the last step since the current keyframe that
 * changed it (-1 for none), and a value is read from that step (#CurrentStep): four bytes per
 * point instead of sixteen, and nothing to copy when the history starts.
 */
struct Runtime {
  /** Whether the arrays below are for the current step. */
  bool compare_valid = false;
  Layout layout;
  Array<int> last_position_step;
  /** Empty when no mask changed since the keyframe. */
  Array<int> last_mask_step;
  /** Whether the current keyframe has face sets (#PHANTOM_HISTORY_STEP_FACE_SETS). */
  bool face_sets_known = false;
  int face_sets_num = 0;
  /** The face sets at the current step; empty when every face has #DEFAULT_FACE_SET. */
  Array<int> face_sets;
  int face_sets_color_seed = 0;
  int face_sets_color_default = 0;
  /**
   * The mesh is known to be in the current step's shape apart from the points edits touch, since
   * it was last compared in full, in this sculpt session (#record).
   */
  bool in_sync = false;

  /** By step number: keyframes with arrays shared with a mesh. */
  Map<int, SharedArrays> shared_arrays;
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
  /** The face sets of the (base) mesh's faces; empty when it has none. */
  Span<int> face_sets;
  VArraySpan<int> mesh_face_sets;
  int face_sets_num = 0;
  int face_sets_color_seed = 0;
  int face_sets_color_default = 0;
};

static void read_face_sets(const Mesh &mesh, State &state)
{
  state.face_sets_num = mesh.faces_num;
  state.face_sets_color_seed = mesh.face_sets_color_seed;
  state.face_sets_color_default = mesh.face_sets_color_default;
  if (const VArray<int> face_sets = *mesh.attributes().lookup<int>(FACE_SET_ATTRIBUTE,
                                                                     AttrDomain::Face))
  {
    state.mesh_face_sets = face_sets;
    state.face_sets = state.mesh_face_sets;
  }
}

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
  read_face_sets(*id_cast<const Mesh *>(ob.data), state);
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

static int face_set_at(const Span<int> face_sets, const int64_t i)
{
  return face_sets.is_empty() ? DEFAULT_FACE_SET : face_sets[i];
}

static bool all_default_face_sets(const Span<int> face_sets)
{
  return std::all_of(
      face_sets.begin(), face_sets.end(), [](const int i) { return i == DEFAULT_FACE_SET; });
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

static Runtime &runtime_get(PhantomHistory &history)
{
  if (history.runtime == nullptr) {
    history.runtime = MEM_new<Runtime>(__func__);
  }
  return *static_cast<Runtime *>(history.runtime);
}

/** Free the runtime data, after the steps (which release their shared arrays). */
static void free_runtime(PhantomHistory &history)
{
  MEM_delete(static_cast<Runtime *>(history.runtime));
  history.runtime = nullptr;
}

/** The current step changed: the comparison data is rebuilt when it is next needed. */
static void invalidate_compare(PhantomHistory &history)
{
  if (history.runtime) {
    Runtime &runtime = *static_cast<Runtime *>(history.runtime);
    runtime.compare_valid = false;
    runtime.in_sync = false;
    runtime.last_position_step = {};
    runtime.last_mask_step = {};
    runtime.face_sets = {};
  }
}

static SharedArrays *shared_arrays(PhantomHistory &history, const PhantomHistoryStep &step)
{
  if (history.runtime == nullptr || !(step.flag & PHANTOM_HISTORY_STEP_KEYFRAME)) {
    return nullptr;
  }
  return static_cast<Runtime *>(history.runtime)->shared_arrays.lookup_ptr(step.number);
}

static void free_step(PhantomHistory &history, PhantomHistoryStep &step)
{
  SharedArrays *shared = shared_arrays(history, step);
  if (shared && shared->positions) {
    shared->positions->remove_user_and_delete_if_last();
    step.positions = nullptr;
  }
  else {
    MEM_SAFE_DELETE(step.positions);
  }
  if (shared) {
    if (shared->face_offsets) {
      shared->face_offsets->remove_user_and_delete_if_last();
    }
    else {
      MEM_SAFE_DELETE(step.face_offsets);
    }
    if (shared->corner_verts) {
      shared->corner_verts->remove_user_and_delete_if_last();
    }
    else {
      MEM_SAFE_DELETE(step.corner_verts);
    }
    static_cast<Runtime *>(history.runtime)->shared_arrays.remove(step.number);
    step.face_offsets = nullptr;
    step.corner_verts = nullptr;
  }
  else {
    MEM_SAFE_DELETE(step.face_offsets);
    MEM_SAFE_DELETE(step.corner_verts);
  }
  MEM_SAFE_DELETE(step.delta_indices);
  MEM_SAFE_DELETE(step.delta_positions);
  MEM_SAFE_DELETE(step.masks);
  MEM_SAFE_DELETE(step.mask_delta_indices);
  MEM_SAFE_DELETE(step.mask_delta_values);
  MEM_SAFE_DELETE(step.face_sets);
  MEM_SAFE_DELETE(step.face_set_delta_indices);
  MEM_SAFE_DELETE(step.face_set_delta_values);
}

void free(PhantomHistory *history)
{
  if (history == nullptr) {
    return;
  }
  for (const int i : IndexRange(history->steps_num)) {
    free_step(*history, history->steps[i]);
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
        invalidate_compare(*history);
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

/**
 * The face sets at step `index`, from its keyframe (which has face sets) and the deltas after it;
 * empty when every face has #DEFAULT_FACE_SET.
 */
static Array<int> step_face_sets(const PhantomHistory &history,
                                 const int keyframe_index,
                                 const int index)
{
  const PhantomHistoryStep &keyframe = history.steps[keyframe_index];
  bool any_delta = false;
  for (int i = keyframe_index + 1; i <= index; i++) {
    any_delta |= history.steps[i].face_set_delta_num > 0;
  }
  if (keyframe.face_sets == nullptr && !any_delta) {
    return {};
  }
  Array<int> face_sets(keyframe.face_sets_num);
  if (keyframe.face_sets) {
    face_sets.as_mutable_span().copy_from({keyframe.face_sets, keyframe.face_sets_num});
  }
  else {
    face_sets.fill(DEFAULT_FACE_SET);
  }
  for (int i = keyframe_index + 1; i <= index; i++) {
    const PhantomHistoryStep &step = history.steps[i];
    scatter<int>({step.face_set_delta_indices, step.face_set_delta_num},
                 {step.face_set_delta_values, step.face_set_delta_num},
                 face_sets);
  }
  return face_sets;
}

/** The comparison data's face sets become those of the current step. */
static void start_face_sets_compare(const PhantomHistory &history, Runtime &runtime)
{
  const int keyframe_index = keyframe_of(history, history.current);
  const PhantomHistoryStep &keyframe = history.steps[keyframe_index];
  const PhantomHistoryStep &current = history.steps[history.current];
  runtime.face_sets_known = keyframe.flag & PHANTOM_HISTORY_STEP_FACE_SETS;
  runtime.face_sets_num = keyframe.face_sets_num;
  runtime.face_sets = runtime.face_sets_known ?
                          step_face_sets(history, keyframe_index, history.current) :
                          Array<int>();
  runtime.face_sets_color_seed = current.face_sets_color_seed;
  runtime.face_sets_color_default = current.face_sets_color_default;
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
  shape.has_face_sets = keyframe.flag & PHANTOM_HISTORY_STEP_FACE_SETS;
  if (shape.has_face_sets) {
    shape.face_sets = step_face_sets(history, shape.keyframe, index);
    shape.face_sets_color_seed = history.steps[index].face_sets_color_seed;
    shape.face_sets_color_default = history.steps[index].face_sets_color_default;
  }
  return shape;
}

static void fill_parallel(MutableSpan<int> values, const int value)
{
  threading::parallel_for(values.index_range(), 1 << 16, [&](const IndexRange range) {
    values.slice(range).fill(value);
  });
}

/** For each point, the number of the last of `steps` (in order) changing it, or -1. */
static void scatter_numbers(const Span<PhantomHistoryStep> steps,
                            const bool masks,
                            MutableSpan<int> last_step)
{
  for (const PhantomHistoryStep &step : steps) {
    const Span<int> indices = masks ? Span<int>(step.mask_delta_indices, step.mask_delta_num) :
                                      Span<int>(step.delta_indices, step.delta_num);
    threading::parallel_for(indices.index_range(), 4096, [&](const IndexRange range) {
      for (const int i : indices.slice(range)) {
        last_step[i] = step.number;
      }
    });
  }
}

/** The mesh is at the current step, a keyframe: nothing changed since it. */
static void start_compare_at_keyframe(PhantomHistory &history, const Layout &layout)
{
  Runtime &runtime = runtime_get(history);
  runtime.layout = layout;
  runtime.last_position_step.reinitialize(layout.points_num);
  fill_parallel(runtime.last_position_step, -1);
  runtime.last_mask_step = {};
  start_face_sets_compare(history, runtime);
  runtime.compare_valid = true;
  runtime.in_sync = true;
}

static Runtime &ensure_compare(PhantomHistory &history)
{
  Runtime &runtime = runtime_get(history);
  if (runtime.compare_valid) {
    return runtime;
  }
  const int keyframe = keyframe_of(history, history.current);
  runtime.layout = step_layout(history.steps[keyframe]);
  const Span<PhantomHistoryStep> since = Span(history.steps, history.steps_num)
                                             .slice(IndexRange::from_begin_end(
                                                 keyframe + 1, history.current + 1));
  runtime.last_position_step.reinitialize(runtime.layout.points_num);
  fill_parallel(runtime.last_position_step, -1);
  scatter_numbers(since, false, runtime.last_position_step);
  runtime.last_mask_step = {};
  if (std::any_of(since.begin(), since.end(), [](const PhantomHistoryStep &step) {
        return step.mask_delta_num > 0;
      }))
  {
    runtime.last_mask_step.reinitialize(runtime.layout.points_num);
    fill_parallel(runtime.last_mask_step, -1);
    scatter_numbers(since, true, runtime.last_mask_step);
  }
  start_face_sets_compare(history, runtime);
  runtime.compare_valid = true;
  runtime.in_sync = false;
  return runtime;
}

/** Reads the positions and masks of the current step (#Runtime). */
class CurrentStep {
  const PhantomHistory &history_;
  const Runtime &runtime_;
  const PhantomHistoryStep &keyframe_;
  int first_number_;
  Array<int> index_by_number_;

  /** The step `number`, or null when it was folded into the first keyframe (#apply_limit). */
  const PhantomHistoryStep *step(const int number) const
  {
    const int offset = number - first_number_;
    if (offset < 0 || offset >= index_by_number_.size() || index_by_number_[offset] == -1) {
      return nullptr;
    }
    const PhantomHistoryStep &step = history_.steps[index_by_number_[offset]];
    return (step.flag & PHANTOM_HISTORY_STEP_KEYFRAME) ? nullptr : &step;
  }

 public:
  CurrentStep(const PhantomHistory &history, const Runtime &runtime)
      : history_(history),
        runtime_(runtime),
        keyframe_(history.steps[keyframe_of(history, history.current)])
  {
    first_number_ = history.steps[0].number;
    index_by_number_.reinitialize(history.steps[history.steps_num - 1].number - first_number_ + 1);
    index_by_number_.fill(-1);
    for (const int i : IndexRange(history.steps_num)) {
      index_by_number_[history.steps[i].number - first_number_] = i;
    }
  }

  float3 position(const int64_t i) const
  {
    const int number = runtime_.last_position_step[i];
    const PhantomHistoryStep *step = number == -1 ? nullptr : this->step(number);
    if (step == nullptr) {
      return reinterpret_cast<const float3 *>(keyframe_.positions)[i];
    }
    const int *indices = step->delta_indices;
    const int j = int(std::lower_bound(indices, indices + step->delta_num, int(i)) - indices);
    return reinterpret_cast<const float3 *>(step->delta_positions)[j];
  }

  float mask(const int64_t i) const
  {
    const int number = runtime_.last_mask_step.is_empty() ? -1 : runtime_.last_mask_step[i];
    const PhantomHistoryStep *step = number == -1 ? nullptr : this->step(number);
    if (step == nullptr) {
      return keyframe_.masks ? keyframe_.masks[i] : 0.0f;
    }
    const int *indices = step->mask_delta_indices;
    const int j = int(std::lower_bound(indices, indices + step->mask_delta_num, int(i)) -
                      indices);
    return step->mask_delta_values[j];
  }
};

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

static void set_keyframe(PhantomHistory &history, PhantomHistoryStep &step, const State &state)
{
  const Layout &layout = state.layout;
  step.flag |= PHANTOM_HISTORY_STEP_KEYFRAME;
  step.verts_num = layout.points_num;
  step.faces_num = layout.faces_num;
  step.corners_num = layout.corners_num;
  step.grids_num = layout.grids_num;
  step.grid_size = layout.grid_size;
  SharedArrays shared;
  const GAttributeReader positions = state.mesh ? state.mesh->attributes().lookup("position") :
                                                  GAttributeReader{};
  if (positions && positions.sharing_info && positions.varray.is_span()) {
    step.positions = static_cast<float *>(
        const_cast<void *>(positions.varray.get_internal_span().data()));
    positions.sharing_info->add_user();
    shared.positions = positions.sharing_info;
  }
  else {
    step.positions = MEM_new_array_uninitialized<float>(size_t(layout.points_num) * 3, __func__);
    /* In parallel: a large copy (240 MB at 20 million points). */
    const Span<float> src_positions = state.positions.cast<float>();
    MutableSpan<float> dst_positions(step.positions, src_positions.size());
    threading::parallel_for(src_positions.index_range(), 1 << 18, [&](const IndexRange range) {
      dst_positions.slice(range).copy_from(src_positions.slice(range));
    });
  }
  if (state.mesh && layout.faces_num > 0 && state.mesh->runtime->face_offsets_sharing_info) {
    const ImplicitSharingInfo *sharing_info = nullptr;
    implicit_sharing::copy_shared_pointer(const_cast<int *>(state.mesh->face_offset_indices),
                                          state.mesh->runtime->face_offsets_sharing_info,
                                          &step.face_offsets,
                                          &sharing_info);
    shared.face_offsets = sharing_info;
  }
  else {
    step.face_offsets = MEM_new_array_uninitialized<int>(size_t(layout.faces_num) + 1, __func__);
    step.face_offsets[0] = 0;
    if (state.mesh && layout.faces_num > 0) {
      memcpy(step.face_offsets,
             state.mesh->face_offsets().data(),
             sizeof(int) * (layout.faces_num + 1));
    }
  }
  const GAttributeReader corner_verts = state.mesh && layout.corners_num > 0 ?
                                            state.mesh->attributes().lookup(".corner_vert") :
                                            GAttributeReader{};
  if (corner_verts && corner_verts.sharing_info && corner_verts.varray.is_span()) {
    step.corner_verts = static_cast<int *>(
        const_cast<void *>(corner_verts.varray.get_internal_span().data()));
    corner_verts.sharing_info->add_user();
    shared.corner_verts = corner_verts.sharing_info;
  }
  else {
    step.corner_verts = MEM_new_array_uninitialized<int>(size_t(layout.corners_num), __func__);
    if (state.mesh && layout.corners_num > 0) {
      memcpy(step.corner_verts, state.mesh->corner_verts().data(), sizeof(int) * layout.corners_num);
    }
  }
  if (shared.positions || shared.face_offsets || shared.corner_verts) {
    runtime_get(history).shared_arrays.add_overwrite(step.number, shared);
  }
  if (any_masked(state.masks)) {
    step.masks = MEM_new_array_uninitialized<float>(size_t(layout.points_num), __func__);
    MutableSpan<float>(step.masks, layout.points_num).copy_from(state.masks);
  }
  step.flag |= PHANTOM_HISTORY_STEP_FACE_SETS;
  step.face_sets_num = state.face_sets_num;
  if (!all_default_face_sets(state.face_sets)) {
    step.face_sets = MEM_new_array_uninitialized<int>(size_t(state.face_sets_num), __func__);
    MutableSpan<int>(step.face_sets, state.face_sets_num).copy_from(state.face_sets);
  }
  step.face_sets_color_seed = state.face_sets_color_seed;
  step.face_sets_color_default = state.face_sets_color_default;
}


void session_ended(Object &ob)
{
  if (ob.phantom_history && ob.phantom_history->runtime) {
    static_cast<Runtime *>(ob.phantom_history->runtime)->in_sync = false;
  }
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
  set_keyframe(history, append_step(history, IFACE_("Start")), state);
  history.current = 0;
  start_compare_at_keyframe(history, state.layout);
}

void truncate_after_current(PhantomHistory &history)
{
  if (history.current >= history.steps_num - 1) {
    return;
  }
  for (int i = history.current + 1; i < history.steps_num; i++) {
    free_step(history, history.steps[i]);
  }
  history.steps_num = history.current + 1;
  /* Give the memory of the dropped steps' slots back too. */
  PhantomHistoryStep *steps = MEM_new_array<PhantomHistoryStep>(history.steps_num, __func__);
  memcpy(steps, history.steps, sizeof(PhantomHistoryStep) * history.steps_num);
  MEM_SAFE_DELETE(history.steps);
  history.steps = steps;
}

/** Before writing a keyframe's positions: they are its own, not shared with a mesh. */
static void ensure_positions_owned(PhantomHistory &history, PhantomHistoryStep &step)
{
  SharedArrays *shared = shared_arrays(history, step);
  if (shared == nullptr || shared->positions == nullptr || shared->positions->is_mutable()) {
    return;
  }
  float *positions = MEM_new_array_uninitialized<float>(size_t(step.verts_num) * 3, __func__);
  std::copy_n(step.positions, size_t(step.verts_num) * 3, positions);
  shared->positions->remove_user_and_delete_if_last();
  shared->positions = nullptr;
  step.positions = positions;
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
      free_step(history, first);
      erase_step(history, 0);
    }
    else {
      /* Fold the second step into the first keyframe, which then has its shape and mask. */
      ensure_positions_owned(history, first);
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
      if (second.face_set_delta_num > 0 && (first.flag & PHANTOM_HISTORY_STEP_FACE_SETS)) {
        if (first.face_sets == nullptr) {
          first.face_sets = MEM_new_array_uninitialized<int>(size_t(first.face_sets_num),
                                                             __func__);
          MutableSpan<int>(first.face_sets, first.face_sets_num).fill(DEFAULT_FACE_SET);
        }
        scatter<int>({second.face_set_delta_indices, second.face_set_delta_num},
                     {second.face_set_delta_values, second.face_set_delta_num},
                     {first.face_sets, first.face_sets_num});
      }
      first.face_sets_color_seed = second.face_sets_color_seed;
      first.face_sets_color_default = second.face_sets_color_default;
      /* The comparison data reads values of the folded step from the keyframe (#CurrentStep), and
       * the keyframe's shared arrays go by its number. */
      if (history.runtime) {
        Runtime &runtime = *static_cast<Runtime *>(history.runtime);
        if (const std::optional<SharedArrays> shared = runtime.shared_arrays.pop_try(
                first.number))
        {
          runtime.shared_arrays.add_overwrite(second.number, *shared);
        }
      }
      first.number = second.number;
      memcpy(first.name, second.name, sizeof(first.name));
      free_step(history, second);
      erase_step(history, 1);
    }
    history.current--;
  }
}

void record(Object &ob,
            const StringRef name,
            bool topology_changed,
            const BitSpan touched,
            const bool face_sets_edited)
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
    set_keyframe(history, append_step(history, name), state);
    history.current = 0;
    start_compare_at_keyframe(history, state.layout);
    return;
  }

  Runtime &runtime = ensure_compare(history);
  const Span<float3> positions = state.positions;
  topology_changed |= state.layout != runtime.layout;
  if (runtime.face_sets_known) {
    topology_changed |= state.face_sets_num != runtime.face_sets_num;
  }
  else if (face_sets_edited) {
    /* The current keyframe is from a history saved without face sets: a keyframe starts them. */
    topology_changed = true;
  }

  IndexMaskMemory memory;
  IndexMask moved;
  IndexMask remasked;
  IndexMask refaced;
  bool colors_changed = false;
  if (!topology_changed) {
    /* Only the points the edit touched can differ, once the rest is known to match. */
    const bool compare_all = !runtime.in_sync || touched.size() != positions.size();
    const IndexMask candidates = compare_all ? IndexMask(positions.index_range()) :
                                               IndexMask::from_bits(touched, memory);
    /* Face sets only change in face set edits (and outside of the sculpt session). */
    if (runtime.face_sets_known && (face_sets_edited || compare_all)) {
      const Span<int> face_sets = state.face_sets;
      const Span<int> current_face_sets = runtime.face_sets;
      if (!(face_sets.is_empty() && current_face_sets.is_empty())) {
        refaced = IndexMask::from_predicate(
            IndexRange(state.face_sets_num), memory, [&](const int64_t i) {
              return face_set_at(face_sets, i) != face_set_at(current_face_sets, i);
            });
      }
      colors_changed = state.face_sets_color_seed != runtime.face_sets_color_seed ||
                       state.face_sets_color_default != runtime.face_sets_color_default;
    }
    /* Compare bits, so the step restores exactly. */
    const CurrentStep current_step(history, runtime);
    moved = IndexMask::from_predicate(candidates, memory, [&](const int64_t i) {
      const float3 position = current_step.position(i);
      return memcmp(&positions[i], &position, sizeof(float3)) != 0;
    });
    /* Without a mask now or at the current step, no mask changed (the usual case). */
    const bool unmasked = state.masks.is_empty() && runtime.last_mask_step.is_empty() &&
                          history.steps[keyframe_of(history, history.current)].masks == nullptr;
    if (!unmasked) {
      remasked = IndexMask::from_predicate(candidates, memory, [&](const int64_t i) {
        const float mask = mask_at(state.masks, i);
        const float current = current_step.mask(i);
        return memcmp(&mask, &current, sizeof(float)) != 0;
      });
    }
    runtime.in_sync = true;
    if (moved.is_empty() && remasked.is_empty() && refaced.is_empty() && !colors_changed) {
      /* A visibility edit, or nothing changed: nothing the history keeps. */
      return;
    }
  }

  /* Editing from the middle of the history drops the steps after it. */
  truncate_after_current(history);

  PhantomHistoryStep &step = append_step(history, name);
  if (topology_changed) {
    set_keyframe(history, step, state);
    /* (The comparison data is of the current step.) */
    history.current = history.steps_num - 1;
    start_compare_at_keyframe(history, state.layout);
  }
  else {
    step.delta_num = int(moved.size());
    step.delta_indices = MEM_new_array_uninitialized<int>(moved.size(), __func__);
    step.delta_positions = MEM_new_array_uninitialized<float>(moved.size() * 3, __func__);
    moved.to_indices<int>({step.delta_indices, step.delta_num});
    float3 *values = reinterpret_cast<float3 *>(step.delta_positions);
    MutableSpan<int> last_position_step = runtime.last_position_step;
    const int number = step.number;
    moved.foreach_index(
        [&](const int64_t i, const int64_t pos) {
          values[pos] = positions[i];
          last_position_step[i] = number;
        },
        exec_mode::grain_size(4096));
    if (!remasked.is_empty()) {
      step.mask_delta_num = int(remasked.size());
      step.mask_delta_indices = MEM_new_array_uninitialized<int>(remasked.size(), __func__);
      step.mask_delta_values = MEM_new_array_uninitialized<float>(remasked.size(), __func__);
      remasked.to_indices<int>({step.mask_delta_indices, step.mask_delta_num});
      if (runtime.last_mask_step.is_empty()) {
        runtime.last_mask_step.reinitialize(runtime.layout.points_num);
        fill_parallel(runtime.last_mask_step, -1);
      }
      MutableSpan<int> last_mask_step = runtime.last_mask_step;
      float *mask_values = step.mask_delta_values;
      remasked.foreach_index(
          [&](const int64_t i, const int64_t pos) {
            mask_values[pos] = mask_at(state.masks, i);
            last_mask_step[i] = number;
          },
          exec_mode::grain_size(4096));
    }
    if (!refaced.is_empty()) {
      step.face_set_delta_num = int(refaced.size());
      step.face_set_delta_indices = MEM_new_array_uninitialized<int>(refaced.size(), __func__);
      step.face_set_delta_values = MEM_new_array_uninitialized<int>(refaced.size(), __func__);
      refaced.to_indices<int>({step.face_set_delta_indices, step.face_set_delta_num});
      if (runtime.face_sets.is_empty()) {
        runtime.face_sets.reinitialize(runtime.face_sets_num);
        runtime.face_sets.fill(DEFAULT_FACE_SET);
      }
      MutableSpan<int> current_face_sets = runtime.face_sets;
      int *face_set_values = step.face_set_delta_values;
      refaced.foreach_index(
          [&](const int64_t i, const int64_t pos) {
            face_set_values[pos] = face_set_at(state.face_sets, i);
            current_face_sets[i] = face_set_values[pos];
          },
          exec_mode::grain_size(4096));
    }
    step.face_sets_color_seed = state.face_sets_color_seed;
    step.face_sets_color_default = state.face_sets_color_default;
    runtime.face_sets_color_seed = state.face_sets_color_seed;
    runtime.face_sets_color_default = state.face_sets_color_default;
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

int step_grid_size(const PhantomHistory &history, const int index)
{
  return history.steps[keyframe_of(history, index)].grid_size;
}

int step_grids_num(const PhantomHistory &history, const int index)
{
  return history.steps[keyframe_of(history, index)].grids_num;
}

static bool same_topology(const PhantomHistoryStep &keyframe, const Mesh &mesh)
{
  if (keyframe.grids_num > 0 || keyframe.verts_num != mesh.verts_num ||
      keyframe.faces_num != mesh.faces_num || keyframe.corners_num != mesh.corners_num)
  {
    return false;
  }
  return Span<int>(keyframe.face_offsets, keyframe.faces_num + 1) == mesh.face_offsets() &&
         Span<int>(keyframe.corner_verts, keyframe.corners_num) == mesh.corner_verts();
}

bool step_topology_matches(const PhantomHistory &history, const int index, const Mesh &mesh)
{
  return same_topology(history.steps[keyframe_of(history, index)], mesh);
}

int last_mesh_step(const PhantomHistory &history, const int index)
{
  for (int i = index; i >= 0; i--) {
    if (history.steps[keyframe_of(history, i)].grids_num == 0) {
      return i;
    }
  }
  return -1;
}

bool write_mesh_positions(const PhantomHistory &history, const int index, Mesh &mesh)
{
  const StepShape shape = step_shape(history, index);
  const Span<float3> positions = mesh.vert_positions();
  if (positions.size() != shape.positions.size() ||
      memcmp(positions.data(), shape.positions.data(), positions.size_in_bytes()) == 0)
  {
    return false;
  }
  mesh.vert_positions_for_write().copy_from(shape.positions);
  mesh.tag_positions_changed();
  return true;
}

/**
 * Gives `mesh` the face sets (and their colors) of `shape`, when its keyframe has them and the
 * mesh has its faces. True when anything changed.
 */
static bool write_mesh_face_sets(Mesh &mesh,
                                 const StepShape &shape,
                                 const PhantomHistoryStep &keyframe)
{
  if (!shape.has_face_sets || keyframe.face_sets_num != mesh.faces_num) {
    return false;
  }
  bool changed = mesh.face_sets_color_seed != shape.face_sets_color_seed ||
                 mesh.face_sets_color_default != shape.face_sets_color_default;
  mesh.face_sets_color_seed = shape.face_sets_color_seed;
  mesh.face_sets_color_default = shape.face_sets_color_default;
  MutableAttributeAccessor attributes = mesh.attributes_for_write();
  {
    const VArray<int> current = *attributes.lookup<int>(FACE_SET_ATTRIBUTE, AttrDomain::Face);
    if (!current) {
      /* A mesh without face sets has the default one (#DEFAULT_FACE_SET) everywhere. */
      if (shape.face_sets.is_empty()) {
        return changed;
      }
    }
    else {
      const VArraySpan<int> current_span(current);
      const bool same = !threading::parallel_reduce(
          IndexRange(mesh.faces_num),
          4096,
          false,
          [&](const IndexRange range, bool differs) {
            for (const int64_t i : range) {
              if (differs) {
                break;
              }
              differs = current_span[i] != face_set_at(shape.face_sets, i);
            }
            return differs;
          },
          [](const bool a, const bool b) { return a || b; });
      if (same) {
        return changed;
      }
    }
  }
  SpanAttributeWriter<int> writer = attributes.lookup_or_add_for_write_only_span<int>(
      FACE_SET_ATTRIBUTE, AttrDomain::Face);
  if (!writer) {
    return changed;
  }
  if (shape.face_sets.is_empty()) {
    writer.span.fill(DEFAULT_FACE_SET);
  }
  else {
    writer.span.copy_from(shape.face_sets);
  }
  writer.finish();
  return true;
}

ApplyResult apply_step(Object &ob, int index, bool *r_face_sets_changed)
{
  if (r_face_sets_changed) {
    *r_face_sets_changed = false;
  }
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
  else {
    const bool same_keyframe = keyframe_index == keyframe_of(*history, history->current) &&
                               layout == state.layout;
    /* Across a topology change the mesh is replaced. (A keyframe with the mesh's own faces, like
     * the steps of the base mesh before and after Multires levels, is written in place.) */
    if (!same_keyframe && !same_topology(keyframe, *state.mesh)) {
      result = ApplyResult::Topology;
    }
  }

  StepShape shape = step_shape(*history, index);
  bool face_sets_changed = false;
  if (state.subdiv_ccg) {
    SubdivCCG &subdiv_ccg = *state.subdiv_ccg;
    subdiv_ccg.positions.as_mutable_span().copy_from(shape.positions);
    if (!subdiv_ccg.masks.is_empty()) {
      subdiv_ccg.masks.as_mutable_span().copy_from(shape.masks);
    }
    /* The face sets are the base mesh's. */
    face_sets_changed = write_mesh_face_sets(*id_cast<Mesh *>(ob.data), shape, keyframe);
  }
  else if (result == ApplyResult::Positions) {
    /* Within the current keyframe's span the topology is the mesh's: write positions, masks and
     * face sets only, which keeps all other attributes. */
    Mesh &mesh = *id_cast<Mesh *>(ob.data);
    mesh.vert_positions_for_write().copy_from(shape.positions);
    mesh.tag_positions_changed();
    write_mesh_masks(mesh, shape.masks);
    face_sets_changed = write_mesh_face_sets(mesh, shape, keyframe);
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
    write_mesh_face_sets(*new_mesh, shape, keyframe);
    BKE_mesh_nomain_to_mesh(new_mesh, mesh, &ob);
    face_sets_changed = true;
  }
  if (r_face_sets_changed) {
    *r_face_sets_changed = face_sets_changed;
  }

  history->current = index;
  /* The mesh is at the step now: the comparison data is rebuilt (and the mesh compared in full)
   * when the next edit is recorded. */
  invalidate_compare(*history);
  return result;
}

bool is_at_current(const Object &ob)
{
  PhantomHistory *history = ob.phantom_history;
  State state;
  if (history == nullptr || history->steps_num == 0 || !read_state(ob, state)) {
    return true;
  }
  const Runtime &runtime = ensure_compare(*history);
  if (state.layout != runtime.layout) {
    return false;
  }
  if (runtime.face_sets_known) {
    if (state.face_sets_num != runtime.face_sets_num ||
        state.face_sets_color_seed != runtime.face_sets_color_seed ||
        state.face_sets_color_default != runtime.face_sets_color_default)
    {
      return false;
    }
    const Span<int> face_sets = state.face_sets;
    const Span<int> current_face_sets = runtime.face_sets;
    for (const int64_t i : IndexRange(state.face_sets_num)) {
      if (face_set_at(face_sets, i) != face_set_at(current_face_sets, i)) {
        return false;
      }
    }
  }
  const CurrentStep current_step(*history, runtime);
  const Span<float3> positions = state.positions;
  return !threading::parallel_reduce(
      positions.index_range(),
      4096,
      false,
      [&](const IndexRange range, bool differs) {
        for (const int64_t i : range) {
          if (differs) {
            break;
          }
          const float3 position = current_step.position(i);
          const float mask = mask_at(state.masks, i);
          const float current_mask = current_step.mask(i);
          differs = memcmp(&positions[i], &position, sizeof(float3)) != 0 ||
                    memcmp(&mask, &current_mask, sizeof(float)) != 0;
        }
        return differs;
      },
      [](const bool a, const bool b) { return a || b; });
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
    if (step.face_sets) {
      bytes += sizeof(int) * int64_t(step.face_sets_num);
    }
    bytes += sizeof(int) * 2 * int64_t(step.face_set_delta_num);
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
      if (step.face_sets) {
        writer.write_int32_array(step.face_sets_num, step.face_sets);
      }
    }
    else {
      writer.write_int32_array(step.delta_num, step.delta_indices);
      writer.write_float_array(int64_t(step.delta_num) * 3, step.delta_positions);
      writer.write_int32_array(step.mask_delta_num, step.mask_delta_indices);
      writer.write_float_array(step.mask_delta_num, step.mask_delta_values);
      writer.write_int32_array(step.face_set_delta_num, step.face_set_delta_indices);
      writer.write_int32_array(step.face_set_delta_num, step.face_set_delta_values);
    }
  }
}

/** Reads every array of the step (so all its pointers are valid or null), then checks them. */
static bool read_step(BlendDataReader &reader,
                      PhantomHistoryStep &step,
                      const int keyframe_verts,
                      const int keyframe_faces)
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
    /* Only written when there are face sets, by files from after face sets were recorded. */
    if (step.flag & PHANTOM_HISTORY_STEP_FACE_SETS) {
      ok &= step.face_sets_num >= 0;
      if (step.face_sets) {
        ok &= BLO_read_array(&reader, &step.face_sets, std::max(step.face_sets_num, 0));
        ok &= step.face_sets != nullptr;
      }
    }
    else {
      step.face_sets = nullptr;
      step.face_sets_num = 0;
    }
    step.delta_indices = nullptr;
    step.delta_positions = nullptr;
    step.mask_delta_indices = nullptr;
    step.mask_delta_values = nullptr;
    step.mask_delta_num = 0;
    step.face_set_delta_indices = nullptr;
    step.face_set_delta_values = nullptr;
    step.face_set_delta_num = 0;
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
  ok &= step.face_set_delta_num >= 0;
  ok &= BLO_read_array(
      &reader, &step.face_set_delta_indices, std::max(step.face_set_delta_num, 0));
  ok &= BLO_read_array(&reader, &step.face_set_delta_values, std::max(step.face_set_delta_num, 0));
  step.positions = nullptr;
  step.face_offsets = nullptr;
  step.corner_verts = nullptr;
  step.masks = nullptr;
  step.face_sets = nullptr;
  if (!ok || (step.delta_num > 0 && (!step.delta_indices || !step.delta_positions)) ||
      (step.mask_delta_num > 0 && (!step.mask_delta_indices || !step.mask_delta_values)) ||
      (step.face_set_delta_num > 0 &&
       (!step.face_set_delta_indices || !step.face_set_delta_values)))
  {
    return false;
  }
  for (const int i : IndexRange(step.face_set_delta_num)) {
    /* (No faces when the keyframe has no face sets: no face set deltas either.) */
    if (step.face_set_delta_indices[i] < 0 || step.face_set_delta_indices[i] >= keyframe_faces) {
      return false;
    }
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
  int keyframe_faces = 0;
  for (const int i : IndexRange(history->steps_num)) {
    PhantomHistoryStep &step = history->steps[i];
    if (i == 0 && !(step.flag & PHANTOM_HISTORY_STEP_KEYFRAME)) {
      /* The first step must be a keyframe; treat it as one to read it safely. */
      step.flag |= PHANTOM_HISTORY_STEP_KEYFRAME;
      ok = false;
    }
    if (step.flag & PHANTOM_HISTORY_STEP_KEYFRAME) {
      keyframe_verts = step.verts_num;
      keyframe_faces = (step.flag & PHANTOM_HISTORY_STEP_FACE_SETS) ?
                           std::max(step.face_sets_num, 0) :
                           0;
    }
    ok &= read_step(reader, step, keyframe_verts, keyframe_faces);
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
