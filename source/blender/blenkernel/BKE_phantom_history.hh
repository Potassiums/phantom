/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

/** \file
 * \ingroup bke
 *
 * Phantom: per-object sculpt history (#Object.phantom_history), scrubbed on a timeline.
 *
 * Every sculpt edit of a mesh (each stroke, filter, remesh...) adds a step. The first step and
 * every step that changes the topology (remesh) are keyframes holding the whole mesh shape; other
 * steps hold only the vertices that moved, with their exact new positions. Any step is rebuilt
 * bit-exactly from the keyframe before it and the deltas after that.
 *
 * Making a step current (scrubbing) and then editing drops the steps after it. The history is
 * saved in the .blend file (optional), and an unreadable history is dropped with a warning
 * instead of failing the file load. All of it runs on the main thread, like the undo pushes
 * that drive it.
 */

#include "BLI_array.hh"
#include "BLI_math_vector_types.hh"
#include "BLI_string_ref.hh"

namespace blender {
struct BlendDataReader;
struct BlendWriter;
struct Main;
struct Mesh;
struct Object;
struct PhantomHistory;
}  // namespace blender

namespace blender::bke::phantom_history {

/** Give the object a history (recording, saved in the file) if it has none; returns it. */
PhantomHistory &ensure(Object &ob);
/** Delete the object's history entirely. */
void remove(Object &ob);
void free(PhantomHistory *history);

/** Whether edits of this object are recorded now (the sculpt history preference, for meshes). */
bool is_recording(const Object &ob);

/** Steps kept for each object (the preference); 0 keeps them all. */
int step_limit();
/** Apply the step limit to every object's history (after the preference changed). */
void apply_limit_to_all(Main &bmain);

/**
 * The current step as a stable number (#PhantomHistoryStep.number plus one, so 0 is "no
 * history"), for undo steps to remember where the history was.
 */
int cursor_get(const Object &ob);
/**
 * Make the step `cursor` (from #cursor_get) current without touching the mesh: undo and redo
 * have just put the mesh in that state themselves. Steps since dropped are skipped.
 */
void cursor_set(Object &ob, int cursor);

/**
 * Before an edit: make the mesh as it is now the first step, if there is none yet. Nothing is
 * recorded while a #ScopedPause is alive (history operations restoring steps).
 */
void begin_edit(Object &ob, StringRef name);
/**
 * After an edit: add a step with what changed since the current step (nothing when nothing
 * moved). `topology_changed` makes it a keyframe. Steps after the current one are dropped first,
 * and the oldest ones after, past the limit.
 */
void record(Object &ob, StringRef name, bool topology_changed);

/** While alive, #begin_edit and #record do nothing. */
class ScopedPause {
 public:
  ScopedPause();
  ~ScopedPause();
};

/** The mesh shape at a step. */
struct StepShape {
  Array<float3> positions;
  /** The keyframe the step builds on, which has its topology. */
  int keyframe = 0;
};
StepShape step_shape(const PhantomHistory &history, int index);

/**
 * Put the mesh in the state of step `index` and make it current. Returns true when the topology
 * was replaced (attributes other than positions are then lost, like after a remesh), false when
 * only positions were written (the caller updates the sculpt session either way).
 */
bool apply_step(Object &ob, int index);

/** Delete the steps after the current one (sculpting from an older step replaces them). */
void truncate_after_current(PhantomHistory &history);

/** Drop the oldest steps past the limit, never the current one. */
void apply_limit(PhantomHistory &history);

int64_t memory_bytes(const PhantomHistory &history);

void blend_write(BlendWriter &writer, const PhantomHistory &history);
/** Reads the history of `ob`; an invalid one is removed with a warning. */
void blend_read(BlendDataReader &reader, Object &ob);

}  // namespace blender::bke::phantom_history
