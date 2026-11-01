/* SPDX-FileCopyrightText: 2022 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup draw
 */

#pragma once

#include <variant>

#include "BLI_index_mask_fwd.hh"
#include "BLI_string_ref.hh"
#include "BLI_vector.hh"

#include "BKE_paint_bvh.hh"

#include "DNA_customdata_types.h"

namespace blender {

namespace draw {
struct Batch;
}  // namespace draw
struct Object;
namespace bke {
enum class AttrDomain : int8_t;
namespace pbvh {
class Node;
class DrawCache;
class Tree;
}  // namespace pbvh
}  // namespace bke

namespace draw::pbvh {

using GenericRequest = std::string;

enum class CustomRequest : int8_t {
  Position,
  Normal,
  Mask,
  FaceSet,
};

using AttributeRequest = std::variant<CustomRequest, GenericRequest>;

struct ViewportRequest {
  Vector<AttributeRequest> attributes;
  bool use_coarse_grids;
  friend bool operator==(const ViewportRequest &a, const ViewportRequest &b) = default;
  uint64_t hash() const;
};

/**
 * Phantom: every triangle normal of a node is within the cone's angle of #axis. #sin_angle is the
 * sine of that angle, or more than 1 when the cone covers a half space or more (no culling).
 */
struct NormalCone {
  float3 axis = float3(0.0f);
  float sin_angle = 2.0f;
};

class DrawCache : public bke::pbvh::DrawCache {
 public:
  ~DrawCache() override = default;
  /**
   * Recalculate and copy data as necessary to prepare batches for drawing triangles for a
   * specific combination of attributes.
   * \note Calls within the drawing of a single viewport must use the same nodes_to_update mask.
   */
  virtual Span<Batch *> ensure_tris_batches(const Object &object,
                                            const ViewportRequest &request,
                                            const IndexMask &nodes_to_update) = 0;
  /**
   * Recalculate and copy data as necessary to prepare batches for drawing wireframe geometry for a
   * specific combination of attributes.
   * \note Calls within the drawing of a single viewport must use the same nodes_to_update mask.
   */
  virtual Span<Batch *> ensure_lines_batches(const Object &object,
                                             const ViewportRequest &request,
                                             const IndexMask &nodes_to_update) = 0;

  /**
   * Phantom: like #ensure_tris_batches, but one batch for all nodes. Each node draws its part of
   * the batch's index buffer, given in \a r_node_indices (empty for nodes without geometry), so a
   * pass can draw all of them with one multi-draw call.
   */
  virtual Batch *ensure_tris_shared_batch(const Object &object,
                                          const ViewportRequest &request,
                                          const IndexMask &nodes_to_update,
                                          Vector<IndexRange> &r_node_indices) = 0;
  /** Phantom: like #ensure_tris_shared_batch, for wireframe geometry. */
  virtual Batch *ensure_lines_shared_batch(const Object &object,
                                           const ViewportRequest &request,
                                           const IndexMask &nodes_to_update,
                                           Vector<IndexRange> &r_node_indices) = 0;

  /**
   * Phantom: per leaf node, the cone holding the normals of all its triangles, in object space
   * (see #NormalCone), up to date for the nodes in \a node_mask. Empty when back faces may show and
   * can't be culled: meshes that aren't closed and wound outwards, hidden faces, dynamic topology.
   * With \a defer_changed, nodes whose positions changed get no cone (they aren't culled) and are
   * calculated in a later call: while sculpting, they are under the brush, facing the viewer.
   */
  virtual Span<NormalCone> ensure_normal_cones(const Object &object,
                                               const IndexMask &node_mask,
                                               bool defer_changed) = 0;

  /**
   * Return the material index for each node (all faces in a node should have the same material
   * index, as ensured by the BVH building process).
   */
  virtual Span<int> ensure_material_indices(const Object &object) = 0;
};

DrawCache &ensure_draw_data(std::unique_ptr<bke::pbvh::DrawCache> &ptr);

}  // namespace draw::pbvh
}  // namespace blender
