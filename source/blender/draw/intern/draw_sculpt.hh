/* SPDX-FileCopyrightText: 2023 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup draw
 */

#pragma once

#include "BLI_enum_flags.hh"
#include "BLI_index_range.hh"
#include "BLI_math_vector_types.hh"
#include "BLI_vector.hh"

namespace blender {

struct GPUMaterial;
struct Object;
struct View3D;

namespace draw {

struct Batch;

struct SculptBatch {
  Batch *batch;
  int material_slot;
  int debug_index;
  /**
   * Phantom: with #SCULPT_BATCH_MULTI_DRAW, #batch holds the whole mesh, shared by all nodes, and
   * this is the node's part of its index buffer. Draw with #draw, which uses it: the nodes then
   * share one multi-draw call in #PassMain passes.
   */
  IndexRange indices = {};
  bool use_indices = false;
  float3 debug_color();

  template<typename PassT, typename HandleT>
  void draw(PassT &pass, const HandleT &handle, const uint custom_id = 0) const
  {
    if (use_indices) {
      pass.draw(batch, uint(-1), uint(indices.size()), uint(indices.start()), handle, custom_id);
    }
    else {
      pass.draw(batch, handle, custom_id);
    }
  }
};

enum SculptBatchFeature {
  SCULPT_BATCH_DEFAULT = 0,
  SCULPT_BATCH_WIREFRAME = 1 << 0,
  SCULPT_BATCH_MASK = 1 << 1,
  SCULPT_BATCH_FACE_SET = 1 << 2,
  SCULPT_BATCH_VERTEX_COLOR = 1 << 3,
  SCULPT_BATCH_UV = 1 << 4,
  /** Phantom: leave normals out, for shaders that don't read them (they cost uploads). */
  SCULPT_BATCH_NO_NORMAL = 1 << 5,
  /**
   * Phantom: one batch for all nodes, each drawing a range of it (#SculptBatch::draw). Not for
   * code that draws the batch itself (EEVEE, expanded selection draws).
   */
  SCULPT_BATCH_MULTI_DRAW = 1 << 6,
};
ENUM_OPERATORS(SculptBatchFeature);

/** Used by engines that don't use GPUMaterials, like the Workbench and Overlay engines. */
Vector<SculptBatch> sculpt_batches_get(const Object *ob, SculptBatchFeature features);

/** Used by EEVEE. */
Vector<SculptBatch> sculpt_batches_per_material_get(const Object *ob,
                                                    Span<const GPUMaterial *> materials);

/**
 * Phantom: whether Workbench tints sculpt meshes drawn from their #bke::pbvh::Tree with their
 * polygroup colors and mask in its own pass, rather than the overlay engine drawing all of their
 * triangles again to tint them. Debug value 7372 turns it off, for comparisons.
 */
bool sculpt_tint_in_workbench(const View3D *v3d);

/** Phantom: whether a sculpt object has a mask to show (from its original data). */
bool sculpt_has_mask(const Object &object);

}  // namespace draw
}  // namespace blender
