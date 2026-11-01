/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#pragma once

/** \file
 * \ingroup bke
 *
 * Phantom: per-object sculpt history (#Object.phantom_history), scrubbed on a timeline.
 *
 * Every sculpt edit of a mesh (each stroke, mask, filter, remesh...) adds a step. The first step
 * and every step that changes the topology (remesh, another Multires level) are keyframes holding
 * the whole shape and mask; other steps hold only the points that moved and the points whose mask
 * changed, with their exact new values. Any step is rebuilt bit-exactly from the keyframe before
 * it and the deltas after that. The points are the mesh's vertices, or with Multires in Sculpt
 * Mode the grid points at the level being sculpted.
 *
 * Making a step current (scrubbing) and then editing drops the steps after it. The history is
 * saved in the .blend file (optional), and an unreadable history is dropped with a warning
 * instead of failing the file load. All of it runs on the main thread, like the undo pushes
 * that drive it.
 */

#include "BLI_array.hh"
#include "BLI_bit_span.hh"
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
 *
 * `touched` (one bit per point, optional) holds every point the edit may have changed: only those
 * are compared, rather than the whole mesh. The first edit recorded in a sculpt session (and after
 * undo moved the history) still compares every point, to catch changes made outside of it.
 */
void record(Object &ob, StringRef name, bool topology_changed, BitSpan touched = {});

/** The object's sculpt session ended: the mesh may change before the next edit is recorded. */
void session_ended(Object &ob);

/** While alive, #begin_edit and #record do nothing. */
class ScopedPause {
 public:
  ScopedPause();
  ~ScopedPause();
};

/** The shape and mask at a step. */
struct StepShape {
  Array<float3> positions;
  Array<float> masks;
  /** The keyframe the step builds on, which has its topology. */
  int keyframe = 0;
};
StepShape step_shape(const PhantomHistory &history, int index);

/** Whether anything is masked at step `index`. */
bool step_has_mask(const PhantomHistory &history, int index);

/** The size of the Multires grids step `index` was sculpted on, or 0 for the mesh itself. */
int step_grid_size(const PhantomHistory &history, int index);
/** The number of Multires grids of step `index` (one per base mesh face corner), or 0. */
int step_grids_num(const PhantomHistory &history, int index);
/** Whether step `index` is of the mesh (not grids) with the same faces as `mesh`. */
bool step_topology_matches(const PhantomHistory &history, int index, const Mesh &mesh);
/**
 * Multires: the last step at or before `index` of the mesh itself (not grids), whose positions the
 * base mesh had at step `index` (sculpting the grids doesn't move it); -1 when there is none.
 */
int last_mesh_step(const PhantomHistory &history, int index);
/** Give `mesh` the positions of step `index` (of the mesh, same faces); false when it had them. */
bool write_mesh_positions(const PhantomHistory &history, int index, Mesh &mesh);

/**
 * Whether the mesh (or its Multires grids in Sculpt Mode) is exactly in the shape and mask of the
 * current step. Undo can put it elsewhere: reloading a whole-scene step from before the history
 * moved without undo steps.
 */
bool is_at_current(const Object &ob);

/** The index of the step with this cursor (#cursor_get), or -1 when it is gone. */
int find_cursor(const PhantomHistory &history, int cursor);

enum class ApplyResult {
  /** The step's positions and masks were written in place (the caller updates the session). */
  Positions,
  /** The mesh was replaced (attributes other than positions and masks are lost, like after a
   * remesh). */
  Topology,
  /**
   * Nothing changed: the step is of Multires grids at another subdivision level, or of the mesh
   * while Multires is sculpted (or the other way around), or it is masked and the grids have no
   * mask layer yet.
   */
  Failed,
};

/**
 * Put the mesh (or, for Multires in Sculpt Mode, its grids) in the state of step `index` and make
 * it current.
 */
ApplyResult apply_step(Object &ob, int index);

/** Delete the steps after the current one (sculpting from an older step replaces them). */
void truncate_after_current(PhantomHistory &history);

/** Drop the oldest steps past the limit, never the current one. */
void apply_limit(PhantomHistory &history);

int64_t memory_bytes(const PhantomHistory &history);

void blend_write(BlendWriter &writer, const PhantomHistory &history);
/** Reads the history of `ob`; an invalid one is removed with a warning. */
void blend_read(BlendDataReader &reader, Object &ob);

}  // namespace blender::bke::phantom_history
