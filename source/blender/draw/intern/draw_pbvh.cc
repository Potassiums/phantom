/* SPDX-FileCopyrightText: 2024 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup gpu
 *
 * bke::pbvh::Tree drawing.
 * Embeds GPU meshes inside of bke::pbvh::Tree nodes, used by mesh sculpt mode.
 */

#include <algorithm>
#include <array>

#include "BLI_enumerable_thread_specific.hh"
#include "BLI_function_ref.hh"
#include "BLI_map.hh"
#include "BLI_math_geom_c.hh"
#include "BLI_math_vector_types.hh"
#include "BLI_utildefines.hh"
#include "BLI_vector.hh"

#include "DNA_object_types.h"

#include "BKE_attribute.hh"
#include "BKE_attribute_legacy_convert.hh"
#include "BKE_attribute_math.hh"
#include "BKE_customdata.hh"
#include "BKE_mesh.hh"
#include "BKE_paint.hh"
#include "BKE_paint_bvh.hh"
#include "BKE_subdiv_ccg.hh"
#include "BKE_subdiv_eval.hh"

#include "DEG_depsgraph_query.hh"

#include "GPU_attribute_convert.hh"
#include "GPU_batch.hh"
#include "GPU_vertex_buffer.hh"

#include "DRW_engine.hh"
#include "DRW_pbvh.hh"
#include "DRW_render.hh"

#include "PRF_profile.hh"

#include "atomic_ops.h"

#include "draw_pbvh_allocator.hh"
#include "draw_sculpt.hh"

#include "attribute_convert.hh"
#include "bmesh.hh"

namespace blender {

template<> struct DefaultHash<draw::pbvh::AttributeRequest> {
  uint64_t operator()(const draw::pbvh::AttributeRequest &value) const
  {
    using namespace draw::pbvh;
    if (const CustomRequest *request_type = std::get_if<CustomRequest>(&value)) {
      return get_default_hash(*request_type);
    }
    const GenericRequest &attr = std::get<GenericRequest>(value);
    return get_default_hash(attr);
  }
};

namespace draw::pbvh {

uint64_t ViewportRequest::hash() const
{
  return get_default_hash(attributes, use_coarse_grids);
}

/**
 * Whether a request draws the coarse "fast navigate" geometry. Phantom: meshes have a coarse
 * version too (#build_mesh_node_proxy).
 */
static bool use_coarse_buffers(const bke::pbvh::Tree &pbvh, const ViewportRequest &request)
{
  return request.use_coarse_grids &&
         ELEM(pbvh.type(), bke::pbvh::Type::Grids, bke::pbvh::Type::Mesh);
}

/**
 * Per-node location of its data inside a single shared GPU buffer.
 *
 * Pairs one #sizes and #offsets entry per node with the #acquired bit that gates whether that
 * range has been assigned a valid offset yet, and owns the #RangeAllocator that hands out the
 * space inside the shared buffer.
 */
struct Buffer {
  /** \brief Assigns the sub-ranges of the shared buffer to nodes. */
  RangeAllocator space;
  /** \brief The sub-range of the shared buffer that each node occupies. */
  Vector<uint32_t> offsets;
  Vector<int32_t> sizes;
  /** \brief Whether each node currently has space acquired in the shared buffer. */
  BitVector<> acquired;
  /**
   * \brief Nodes whose size might have changed and has to be recalculated. For correctness, just
   * recounting the number of elements in each node every redraw would be correct. This bit per
   * node allows avoiding that recounting for performance reasons.
   */
  BitVector<> dirty;
  /**
   * \brief The size of #space when the GPU buffer was last (re)allocated.
   *
   * When the assigned space grows past this, the underlying buffer is reallocated and batches
   * need rebuilding.
   */
  uint64_t space_limit = 0;

  /**
   * \brief Set the size of the range of node #node_index to #new_len.
   *
   * \return whether the range was changed (and possibly released).
   */
  bool update_range(const int64_t node_index, const int64_t new_len)
  {
    if (this->sizes[node_index] == new_len) {
      return false;
    }
    const bool was_acquired = this->acquired[node_index];
    if (was_acquired) {
      this->space.release(this->offsets[node_index], this->sizes[node_index]);
      this->acquired[node_index].reset();
    }
    this->sizes[node_index] = new_len;
    return was_acquired;
  }

  /**
   * \brief Acquire space for node #node_index using the previously set length.
   */
  void acquire(const int64_t node_index)
  {
    BLI_assert(!this->acquired[node_index]);
    this->offsets[node_index] = this->space.acquire(this->sizes[node_index]);
    this->acquired[node_index].set();
  }

  /** \brief Grow the per-node arrays to cover #nodes_num nodes. */
  void ensure_nodes_num(const int64_t nodes_num)
  {
    if (this->offsets.size() < nodes_num) {
      this->offsets.resize(nodes_num);
      this->sizes.resize(nodes_num);
      this->acquired.resize(nodes_num);
    }
    if (this->dirty.size() < nodes_num) {
      this->dirty.resize(nodes_num, true);
    }
  }

  /**
   * \brief Recalculate the ranges of the nodes marked dirty, and assign space to nodes that
   * don't have any yet.
   *
   * \param calc_sizes: Fills in the number of elements each node of the given mask needs.
   * \param r_changed: Receives the nodes whose range moved or resized. GPU data stored at their
   * old location is now stale.
   * \return True when the assigned space grew past the size of the current GPU buffer.
   */
  bool ensure_ranges(const int64_t nodes_num,
                     const IndexMask &node_mask,
                     const FunctionRef<void(const IndexMask &, MutableSpan<int>)> calc_sizes,
                     BitVector<> &r_changed)
  {
    this->ensure_nodes_num(nodes_num);
    r_changed.resize(std::max<int64_t>(r_changed.size(), nodes_num), false);

    IndexMaskMemory memory;
    const IndexMask dirty_mask = IndexMask::from_bits(node_mask, this->dirty, memory);
    if (!dirty_mask.is_empty()) {
      Array<int> new_sizes(dirty_mask.size());
      calc_sizes(dirty_mask, new_sizes);
      dirty_mask.foreach_index([&](const int node_index, const int pos) {
        if (this->update_range(node_index, new_sizes[pos])) {
          r_changed[node_index].set();
        }
        this->dirty[node_index].reset();
      });
      /* Assign space to nodes that lost it above, and to nodes that never had any. */
      dirty_mask.foreach_index([&](const int node_index) {
        if (!this->acquired[node_index] && this->sizes[node_index] > 0) {
          this->acquire(node_index);
        }
      });
    }

    const uint64_t size = uint64_t(this->space.size());
    if (size <= this->space_limit) {
      return false;
    }
    this->space_limit = size;
    return true;
  }

  /** \brief Mark the sizes of #node_mask as needing recalculation. */
  void tag_dirty(const IndexMask &node_mask)
  {
    /* Nodes past the end have never been visited, so they are dirty by definition. */
    this->dirty.resize(std::max(this->dirty.size(), node_mask.min_array_size()), true);
    node_mask.set_bits(this->dirty);
  }

  IndexRange index_range(int node_index) const
  {
    return IndexRange(this->offsets[node_index], this->sizes[node_index]);
  }

  /**
   * \brief The sub-range of a scratch buffer that node #node_index occupies.
   */
  IndexRange scratch_range(int node_index, int64_t scratch_offset) const
  {
    return this->acquired[node_index] ? IndexRange(scratch_offset, this->sizes[node_index]) :
                                        IndexRange(scratch_offset, 0);
  }
};

/**
 * \brief Scratch buffer offsets for modified nodes.
 *
 * \note `#offsets[n]` refers to the n-th staged node (scratch index), not to the PBVH node index.
 */
struct ScratchOffsets {
  Array<int64_t> offsets;
  int64_t total = 0;
};

/**
 * Because many sculpt mode operations skip tagging dependency graph for reevaluation for
 * performance reasons, the relevant data must be retrieved directly from the original mesh rather
 * than the evaluated copy.
 */
struct OrigMeshData {
  StringRef active_color;
  StringRef default_color;
  VectorSet<StringRef> uv_map_names;
  StringRef active_uv_map;
  StringRef default_uv_map;
  int face_set_default;
  int face_set_seed;
  bke::AttributeAccessor attributes;
  OrigMeshData(const Mesh &mesh)
      : active_color(mesh.active_color_attribute),
        default_color(mesh.default_color_attribute),
        active_uv_map(mesh.active_uv_map_name()),
        default_uv_map(mesh.default_uv_map_name()),
        face_set_default(mesh.face_sets_color_default),
        face_set_seed(mesh.face_sets_color_seed),
        attributes(mesh.attributes())
  {
    uv_map_names.add_multiple(mesh.uv_map_names().as_span().cast<StringRef>());
  }
};

/**
 * Stores the data necessary to draw the PBVH geometry. A separate `*Impl` class is used to hide
 * implementation details from the public header.
 */
class DrawCacheImpl : public DrawCache {
 public:
  struct AttributeData {
    /**
     * \brief A vertex buffer containing data for all BVH nodes.
     */
    gpu::VertBufPtr vbo;

    /**
     * A separate "dirty" bit per node. We track the dirty value separately from deleting the VBO
     * for a node in order to avoid recreating batches with new VBOs. It's also a necessary
     * addition to the flags stored in the PBVH which are cleared after it's used for drawing
     * (those aren't sufficient when multiple viewports are drawing with the same PBVH but request
     * different sets of attributes).
     */
    BitVector<> dirty_nodes;
    /**
     * Mark attribute values dirty for specific nodes. The next time the attribute is requested,
     * the values will be extracted again.
     */
    void tag_dirty(const IndexMask &node_mask);
    void ensure_nodes_capacity(int64_t nodes_num);
    void ensure_buffer_capacity(uint64_t needed_vertex_len);
  };

  /** Phantom: one batch for all nodes of a request, see #ensure_tris_shared_batch. */
  struct SharedBatch {
    Batch *batch = nullptr;
    /** The buffers the batch was set up with, to tell when they were reallocated. */
    gpu::IndexBuf *ibo = nullptr;
    Vector<gpu::VertBuf *> vbos;
  };

 private:
  /**
   * \brief Used to determine whether to use indexed VBO layouts for multires grids and meshes.
   *
   * Bit for each node. `false` means that the node contains smooth shaded vertex data (elements
   * are shared between faces). `true` means that the node is "flat shaded" (elements are stored
   * per face corner).
   */
  BitVector<> use_flat_layout_;
  /**
   * \brief Nodes whose #use_flat_layout_ bit has to be recalculated, because their face sets
   * changed or a request changed what the layout has to hold.
   */
  BitVector<> layout_dirty_;
  /**
   * \brief Whether sharp faces need the flat layout, because a request reads normals (EEVEE).
   * Phantom's sculpt shading works out normals from positions, so until then, flat shaded meshes
   * use the indexed layout too. It stays set like #mesh_force_flat_.
   */
  bool layout_uses_sharp_faces_ = false;
  /**
   * \brief Whether all mesh nodes use the flat layout, because a request needed face corner or
   * face attributes, which per-vertex data can't hold. It stays set, so that viewports with
   * different requests don't switch the layout back and forth.
   */
  bool mesh_force_flat_ = false;
  /**
   * \brief The material index for each node.
   *
   * Is used during drawing to place the node in the correct material bucket.
   */
  Array<int> material_indices_;

  /**
   * \brief Shared index buffer with space bookkeeping and the per-node subranges.
   */
  struct IBOFamily {
    /** \brief Per-node location of its data inside the shared index buffer. */
    Buffer buffer;
    /** \brief Index buffer containing all indices. */
    gpu::IndexBufPtr ibo;
    /** \brief Per node a sub-range of ibo. */
    Vector<gpu::IndexBufPtr> ibos;

    /**
     * \brief Recalculate this family's node ranges and free the sub-buffers that moved.
     *
     * \return True when the shared index buffer has to be reallocated.
     */
    bool ensure_ranges(int nodes_num,
                       const IndexMask &node_mask,
                       FunctionRef<void(const IndexMask &, MutableSpan<int>)> calc_sizes);

    /**
     * \brief Ensure the shared index buffer is large enough to hold the assigned space.
     *
     * When this requires growing (or creating) the buffer, existing data is copied over and
     * existing sub-ranges are recreated.
     */
    void ensure_capacity();

    /**
     * \brief Upload the staged indices of each node and recreate its sub-range of #ibo.
     */
    void commit_subranges(const IndexMask &node_mask,
                          const ScratchOffsets &scratch_offsets,
                          Span<uint32_t> scratch_buffer);
  };
  IBOFamily ibo_lines_;
  IBOFamily ibo_lines_coarse_;
  IBOFamily ibo_tris_;
  IBOFamily ibo_tris_coarse_;

  /** \brief Per-node location of its data inside the shared vertex buffer. */
  Buffer vbo_;

  /**
   * GPU data and per-node dirty status for all requested attributes.
   * \note Currently we do not remove "stale" attributes that haven't been requested in a while.
   */
  Map<AttributeRequest, AttributeData> attribute_vbos_;

  /**
   * \brief Cached batches for a given setup.
   *
   * Batches hold raw pointers to the shared attribute VBOs and index subranges. Those pointers
   * become stale when the underlying buffers are reallocated.
   */
  struct BatchCache {
    Vector<Batch *> batches;
    /**
     * \brief Nodes whose batch references a buffer that has since been reallocated.
     *
     * Tracked per node rather than for the whole cache, because a viewport only rebuilds the
     * batches of the nodes it draws. If all nodes are rebuilt, this can become a single boolean.
     */
    BitVector<> dirty;
  };

  /** Batches for drawing wireframe geometry. */
  BatchCache lines_batches_;
  /** Batches for drawing coarse "fast navigate" wireframe geometry. */
  BatchCache lines_batches_coarse_;
  /**
   * Batches for drawing triangles, stored separately for each combination of attributes and
   * coarse-ness. Different viewports might request different sets of attributes, and we don't want
   * to recreate the batches on every redraw.
   */
  Map<ViewportRequest, BatchCache> tris_batches_;
  /** Phantom: the batches of #ensure_tris_shared_batch and #ensure_lines_shared_batch. */
  Map<ViewportRequest, SharedBatch> tris_shared_batches_;
  SharedBatch lines_shared_batches_[2];

  /** Phantom: back-face culling, see #ensure_normal_cones. */
  Array<NormalCone> normal_cones_;
  BitVector<> normal_cones_dirty_;
  /** Whether the mesh is closed, and wound outwards (its topology doesn't change while drawn). */
  std::optional<bool> closed_;
  std::optional<bool> outward_;
  /** Whether any face is hidden, recalculated after visibility changes. */
  std::optional<bool> any_hidden_;

  /**
   * Phantom: per mesh node drawn with one vertex per vertex, the triangles of its coarse version
   * drawn while navigating with "Fast Navigate" (#build_mesh_node_proxy), as indices into the
   * node's vertices. Rebuilt when navigation starts for the nodes changed since.
   */
  Array<Array<uint3>> mesh_proxies_;
  /** The edges of #mesh_proxies_, for the wireframe: built when it is first drawn. */
  Array<Array<uint2>> mesh_proxy_lines_;
  BitVector<> mesh_proxies_dirty_;
  /** Vertices used by more than one node: proxies keep them, so neighboring nodes still meet. */
  Array<bool> mesh_seam_verts_;

  /**
   * Which nodes have GPU data that no longer matches the geometry, either because their visible
   * faces changed or because their range inside a shared buffer moved.
   *
   * \note Theoretically the dirty tag is redundant with checking for a different number of visible
   * triangles in the PBVH node on every redraw. We could do that too, but it's simpler overall to
   * just tag the node whenever there is such a topology change, and for now there is no real
   * downside.
   */
  BitVector<> dirty_topology_;

 public:
  ~DrawCacheImpl() override;

  void tag_positions_changed(const IndexMask &node_mask) override;
  void tag_visibility_changed(const IndexMask &node_mask) override;
  void tag_topology_changed(const IndexMask &node_mask) override;
  void tag_face_sets_changed(const IndexMask &node_mask) override;
  void tag_masks_changed(const IndexMask &node_mask) override;
  void tag_attribute_changed(const IndexMask &node_mask, StringRef attribute_name) override;

  Span<Batch *> ensure_tris_batches(const Object &object,
                                    const ViewportRequest &request,
                                    const IndexMask &nodes_to_update) override;

  Span<Batch *> ensure_lines_batches(const Object &object,
                                     const ViewportRequest &request,
                                     const IndexMask &nodes_to_update) override;

  Batch *ensure_tris_shared_batch(const Object &object,
                                  const ViewportRequest &request,
                                  const IndexMask &nodes_to_update,
                                  Vector<IndexRange> &r_node_indices) override;

  Batch *ensure_lines_shared_batch(const Object &object,
                                   const ViewportRequest &request,
                                   const IndexMask &nodes_to_update,
                                   Vector<IndexRange> &r_node_indices) override;

  Span<NormalCone> ensure_normal_cones(const Object &object,
                                       const IndexMask &node_mask,
                                       bool defer_changed) override;

  Span<int> ensure_material_indices(const Object &object) override;

 private:
  /**
   * Update the GPU data a triangle request needs, and get the per-node index buffers and the
   * requested vertex buffers.
   */
  Span<gpu::IndexBufPtr> prepare_tris(const Object &object,
                                      const OrigMeshData &orig_mesh_data,
                                      const ViewportRequest &request,
                                      const IndexMask &nodes_to_update,
                                      Vector<gpu::VertBuf *> &r_attr_vbos);
  /** Like #prepare_tris for wireframe geometry, which only needs positions. */
  Span<gpu::IndexBufPtr> prepare_lines(const Object &object,
                                       const OrigMeshData &orig_mesh_data,
                                       const ViewportRequest &request,
                                       const IndexMask &nodes_to_update,
                                       gpu::VertBuf *&r_position_vbo);

  /**
   * \brief Assign the ranges inside the shared vertex buffer, which every index buffer family
   * refers to.
   *
   * \return True when the shared vertex buffer has to be reallocated.
   */
  bool ensure_vert_ranges(const Object &object,
                          const OrigMeshData &orig_mesh_data,
                          const IndexMask &node_mask);

  /** Mark every cached batch as needing to be reconfigured, after a shared buffer was moved. */
  void tag_batches_dirty();

  /**
   * Free the GPU data of every node tagged in #dirty_topology_. The next time the data is
   * requested it will be rebuilt.
   */
  void free_nodes_with_changed_topology(const bke::pbvh::Tree &pbvh);

  BitSpan ensure_use_flat_layout(const Object &object, const OrigMeshData &orig_mesh_data);

  /**
   * Recalculate the layout of the nodes in #layout_dirty_, and tag the GPU data of the
   * nodes whose layout changed.
   */
  void update_layout(const Object &object, const OrigMeshData &orig_mesh_data);

  /** The vertex data of these nodes changes size and order: rebuild all of their GPU data. */
  void tag_layout_changed(const IndexMask &node_mask);

  /**
   * Rebuild the coarse triangles of the mesh nodes in \a node_mask whose geometry changed since,
   * and tag their coarse index buffers.
   */
  void update_mesh_proxies(const Object &object,
                           const OrigMeshData &orig_mesh_data,
                           const IndexMask &node_mask);
  /** Build the missing #mesh_proxy_lines_ of the nodes in \a node_mask. */
  void update_mesh_proxy_lines(const IndexMask &node_mask);
  /** Whether a mesh node draws coarse geometry from #mesh_proxies_ when navigating. */
  bool has_mesh_proxy(const int node_index) const
  {
    return node_index < mesh_proxies_.size() && !mesh_proxies_[node_index].is_empty() &&
           !use_flat_layout_[node_index];
  }

  AttributeData &ensure_attribute_data(const Object &object,
                                       const OrigMeshData &orig_mesh_data,
                                       const AttributeRequest &attr,
                                       const IndexMask &node_mask);

  Span<gpu::IndexBufPtr> ensure_tri_indices(const Object &object,
                                            const OrigMeshData &orig_mesh_data,
                                            const IndexMask &node_mask,
                                            bool coarse);

  Span<gpu::IndexBufPtr> ensure_lines_indices(const Object &object,
                                              const OrigMeshData &orig_mesh_data,
                                              const IndexMask &node_mask,
                                              bool coarse);
};

void DrawCacheImpl::AttributeData::tag_dirty(const IndexMask &node_mask)
{
  /* Nodes past the end have never been extracted, so they are dirty by definition. */
  this->dirty_nodes.resize(std::max(this->dirty_nodes.size(), node_mask.min_array_size()), true);
  node_mask.set_bits(this->dirty_nodes);
}

void DrawCacheImpl::tag_positions_changed(const IndexMask &node_mask)
{
  if (DrawCacheImpl::AttributeData *data = attribute_vbos_.lookup_ptr(CustomRequest::Position)) {
    data->tag_dirty(node_mask);
  }
  if (DrawCacheImpl::AttributeData *data = attribute_vbos_.lookup_ptr(CustomRequest::Normal)) {
    data->tag_dirty(node_mask);
  }
  normal_cones_dirty_.resize(std::max(normal_cones_dirty_.size(), node_mask.min_array_size()),
                             true);
  node_mask.set_bits(normal_cones_dirty_);
  /* The coarse triangles stay valid when vertices move (#build_mesh_node_proxy). */
}

void DrawCacheImpl::tag_visibility_changed(const IndexMask &node_mask)
{
  any_hidden_.reset();
  /* Coarse triangles leave hidden faces out, and follow the layout (#tag_layout_changed). */
  mesh_proxies_dirty_.resize(std::max(mesh_proxies_dirty_.size(), node_mask.min_array_size()),
                             true);
  node_mask.set_bits(mesh_proxies_dirty_);
  /* The index buffers only reference visible faces, so their contents are stale even when the
   * number of visible faces happens to stay the same. */
  dirty_topology_.resize(std::max(dirty_topology_.size(), node_mask.min_array_size()), false);
  node_mask.set_bits(dirty_topology_);

  vbo_.tag_dirty(node_mask);
  ibo_lines_.buffer.tag_dirty(node_mask);
  ibo_lines_coarse_.buffer.tag_dirty(node_mask);
  ibo_tris_.buffer.tag_dirty(node_mask);
  ibo_tris_coarse_.buffer.tag_dirty(node_mask);
}

void DrawCacheImpl::tag_topology_changed(const IndexMask &node_mask)
{
  /* Currently the only times where topology changes are for BMesh dynamic topology, where tagging
   * a visibility update deletes all the GPU data anyway. */
  this->tag_visibility_changed(node_mask);
}

void DrawCacheImpl::tag_face_sets_changed(const IndexMask &node_mask)
{
  if (DrawCacheImpl::AttributeData *data = attribute_vbos_.lookup_ptr(CustomRequest::FaceSet)) {
    data->tag_dirty(node_mask);
  }
  /* Mesh nodes with more than one face set use the flat layout (#FlatLayoutTest). */
  layout_dirty_.resize(std::max(layout_dirty_.size(), node_mask.min_array_size()),
                            false);
  node_mask.set_bits(layout_dirty_);
}

void DrawCacheImpl::tag_masks_changed(const IndexMask &node_mask)
{
  if (DrawCacheImpl::AttributeData *data = attribute_vbos_.lookup_ptr(CustomRequest::Mask)) {
    data->tag_dirty(node_mask);
  }
}

void DrawCacheImpl::tag_attribute_changed(const IndexMask &node_mask, StringRef attribute_name)
{
  for (const auto &[data_request, data] : attribute_vbos_.items()) {
    if (const GenericRequest *request = std::get_if<GenericRequest>(&data_request)) {
      if (*request == attribute_name) {
        data.tag_dirty(node_mask);
      }
    }
  }
}

DrawCache &ensure_draw_data(std::unique_ptr<bke::pbvh::DrawCache> &ptr)
{
  if (!ptr) {
    ptr = std::make_unique<DrawCacheImpl>();
  }
  return dynamic_cast<DrawCache &>(*ptr);
}

BLI_NOINLINE static void free_ibos(const MutableSpan<gpu::IndexBufPtr> ibos,
                                   const IndexMask &node_mask)
{
  IndexMaskMemory memory;
  const IndexMask mask = IndexMask::from_intersection(node_mask, ibos.index_range(), memory);
  mask.foreach_index([&](const int node_index) { ibos[node_index].reset(); });
}

/**
 * Upload a node's index data to the shared IBO. Zero-length ranges (a leaf with no visible
 * geometry) are skipped.
 */
static void update_ibos_subrange(gpu::IndexBuf &ibo_shared,
                                 const IndexRange dst_range,
                                 const uint32_t *host_data)
{
  if (dst_range.is_empty()) {
    return;
  }
  GPU_indexbuf_update_sub(&ibo_shared,
                          uint(dst_range.start()) * sizeof(uint32_t),
                          uint(dst_range.size()) * sizeof(uint32_t),
                          host_data);
}

static ScratchOffsets build_scratch_offsets(const Buffer &buffer, const IndexMask &node_mask)
{
  ScratchOffsets result;
  if (node_mask.is_empty()) {
    return result;
  }
  result.offsets = Array<int64_t>(node_mask.size());
  int64_t offset = 0;
  node_mask.foreach_index([&](const int node_index, const int scratch_index) {
    result.offsets[scratch_index] = offset;
    if (buffer.acquired[node_index]) {
      offset += buffer.sizes[node_index];
    }
  });
  result.total = offset;
  return result;
}

/**
 * \brief Staging offsets for an initial vertex buffer build.
 *
 * Nodes are staged at their absolute VBO offsets so that the full-sized scratch buffer can be
 * promoted to the final vertex buffer.
 */
static ScratchOffsets build_vbo_initial_offsets(const Buffer &buffer, const IndexMask &node_mask)
{
  ScratchOffsets result;
  if (node_mask.is_empty()) {
    return result;
  }
  result.offsets = Array<int64_t>(node_mask.size());
  node_mask.foreach_index([&](const int node_index, const int scratch_index) {
    result.offsets[scratch_index] = buffer.offsets[node_index];
  });
  result.total = buffer.space.size();
  return result;
}

/**
 * Phantom: the scratch buffer is only copied from, so where the backend supports it (Vulkan), it is
 * the GPU's staging memory itself: the data is gathered straight into it (in parallel), with no
 * CPU-side copy and single-threaded upload afterwards, and no intermediate GPU buffer.
 */
static gpu::VertBufPtr create_vbo_scratch_buffer(const GPUVertFormat &format, const int64_t total)
{
  gpu::VertBufPtr scratch = gpu::VertBufPtr(GPU_vertbuf_create_with_format_ex(
      format, GPU_USAGE_STATIC | GPU_USAGE_FLAG_UPLOAD_SOURCE));
  scratch->allocate(uint32_t(total));
  return scratch;
}

/**
 * Copy a node's slice from an uploaded scratch buffer into the final vertex buffer.
 *
 * \param dst The final vertex buffer (GPU_USAGE_DEVICE_ONLY) to copy into.
 * \param dst_range The destination vertex range of this node inside #dst.
 * \param src_vertex Vertex offset of this node inside the scratch buffer.
 * \param scratch The uploaded scratch buffer to read from.
 */
static void update_vbo_subrange(gpu::VertBuf &dst,
                                const IndexRange dst_range,
                                const int64_t src_vertex,
                                gpu::VertBuf &scratch)
{
  if (dst_range.is_empty()) {
    return;
  }
  dst.copy_sub(scratch, uint(src_vertex), uint(dst_range.start()), uint(dst_range.size()));
}

/**
 * \brief Upload staged vertex data and commit it to the final vertex buffer.
 *
 * \note Will promote the staging buffer to the final vertex buffer for initial
 * uploads, or copy all of it into a new one when it lives in staging memory.
 */
static void finalize_vbo_update(DrawCacheImpl::AttributeData &attr_vbo,
                                const Buffer &vbo,
                                const int vert_len,
                                const IndexMask &node_mask,
                                const ScratchOffsets &scratch_offsets,
                                gpu::VertBufPtr &scratch_vbo)
{
  scratch_vbo->upload();
  if (attr_vbo.vbo == nullptr) {
    if (!scratch_vbo->is_upload_source()) {
      attr_vbo.vbo = std::move(scratch_vbo);
      return;
    }
    const uint vertex_len = scratch_vbo->vertex_len;
    attr_vbo.vbo = gpu::VertBufPtr(
        GPU_vertbuf_create_with_format_ex(scratch_vbo->format, GPU_USAGE_DEVICE_ONLY));
    attr_vbo.vbo->allocate(vertex_len);
    attr_vbo.vbo->upload();
    attr_vbo.vbo->copy_sub(*scratch_vbo, 0, 0, vertex_len);
    return;
  }
  attr_vbo.ensure_buffer_capacity(vert_len);
  node_mask.foreach_index([&](const int node_index, const int scratch_index) {
    update_vbo_subrange(*attr_vbo.vbo,
                        vbo.index_range(node_index),
                        scratch_offsets.offsets[scratch_index],
                        *scratch_vbo);
  });
}

BLI_NOINLINE static void free_batches(const MutableSpan<Batch *> batches)
{
  for (Batch *&batch : batches) {
    MEM_SAFE_DELETE(batch);
  }
}

static const GPUVertFormat &position_format()
{
  static const GPUVertFormat format = GPU_vertformat_from_attribute(
      "pos", gpu::VertAttrType::SFLOAT_32_32_32);
  return format;
}

static const GPUVertFormat &normal_format()
{
  /* Phantom: packed like Object Mode's normals, half the bytes of 16-bit components. */
  static const GPUVertFormat format = GPU_vertformat_from_attribute(
      "nor", gpu::VertAttrType::SNORM_10_10_10_2);
  return format;
}

static const GPUVertFormat &mask_format()
{
  static const GPUVertFormat format = GPU_vertformat_from_attribute("msk",
                                                                    gpu::VertAttrType::SFLOAT_32);
  return format;
}

static const GPUVertFormat &face_set_format()
{
  static const GPUVertFormat format = GPU_vertformat_from_attribute(
      "fset", gpu::VertAttrType::UNORM_8_8_8_8);
  return format;
}

static GPUVertFormat attribute_format(const OrigMeshData &orig_mesh_data,
                                      const StringRef name,
                                      const bke::AttrType data_type)
{
  GPUVertFormat format = init_format_for_attribute(data_type, "data");

  bool is_render, is_active;
  const char *prefix = "a";

  if (CD_TYPE_AS_MASK(*bke::attr_type_to_custom_data_type(data_type))) {
    prefix = "c";
    is_active = orig_mesh_data.active_color == name;
    is_render = orig_mesh_data.default_color == name;
  }
  if (data_type == bke::AttrType::Float2) {
    prefix = "u";
    is_active = orig_mesh_data.active_uv_map == name;
    is_render = orig_mesh_data.default_uv_map == name;
  }

  DRW_cdlayer_attr_aliases_add(&format, prefix, data_type, name, is_render, is_active);
  return format;
}

using PackedNormal = int1010102_norm;

inline PackedNormal normal_to_gpu(const float3 &value)
{
  return gpu::convert_normal<PackedNormal>(value);
}

template<typename T>
void extract_data_vert_mesh(const OffsetIndices<int> faces,
                            const Span<int> corner_verts,
                            const Span<T> attribute,
                            const Span<int> face_indices,
                            gpu::VertBuf &vbo,
                            const IndexRange scratch_range)
{
  PRF_scope(ProfileCategory::Draw);
  using Converter = AttributeConverter<T>;
  using VBOType = typename Converter::VBOType;
  VBOType *data = vbo.data<VBOType>().data() + scratch_range.start();
  VBOType *data_start = data;
  for (const int face : face_indices) {
    for (const int vert : corner_verts.slice(faces[face])) {
      *data = Converter::convert(attribute[vert]);
      data++;
    }
  }
  UNUSED_VARS_NDEBUG(data_start);
  BLI_assert(data == data_start + scratch_range.size());
}

/** Data for the vertices of a node that stores each vertex once (see #use_flat_layout_). */
template<typename T>
void extract_data_vert_indexed_mesh(const Span<int> verts,
                                    const Span<T> attribute,
                                    gpu::VertBuf &vbo,
                                    const IndexRange scratch_range)
{
  PRF_scope(ProfileCategory::Draw);
  using Converter = AttributeConverter<T>;
  using VBOType = typename Converter::VBOType;
  MutableSpan<VBOType> data = vbo.data<VBOType>().slice(scratch_range);
  BLI_assert(data.size() == verts.size());
  for (const int i : verts.index_range()) {
    data[i] = Converter::convert(attribute[verts[i]]);
  }
}

template<typename T>
void extract_data_face_mesh(const OffsetIndices<int> faces,
                            const Span<T> attribute,
                            const Span<int> face_indices,
                            gpu::VertBuf &vbo,
                            const IndexRange scratch_range)
{
  PRF_scope(ProfileCategory::Draw);
  using Converter = AttributeConverter<T>;
  using VBOType = typename Converter::VBOType;

  VBOType *data = vbo.data<VBOType>().data() + scratch_range.start();
  VBOType *data_start = data;
  for (const int face : face_indices) {
    const int face_size = faces[face].size();
    std::fill_n(data, face_size, Converter::convert(attribute[face]));
    data += face_size;
  }
  UNUSED_VARS_NDEBUG(data_start);
  BLI_assert(data == data_start + scratch_range.size());
}

template<typename T>
void extract_data_corner_mesh(const OffsetIndices<int> faces,
                              const Span<T> attribute,
                              const Span<int> face_indices,
                              gpu::VertBuf &vbo,
                              const IndexRange scratch_range)
{
  PRF_scope(ProfileCategory::Draw);
  using Converter = AttributeConverter<T>;
  using VBOType = typename Converter::VBOType;

  VBOType *data = vbo.data<VBOType>().data() + scratch_range.start();
  VBOType *data_start = data;
  for (const int face : face_indices) {
    for (const int corner : faces[face]) {
      *data = Converter::convert(attribute[corner]);
      data++;
    }
  }
  UNUSED_VARS_NDEBUG(data_start);
  BLI_assert(data == data_start + scratch_range.size());
}

template<typename T> const T &bmesh_cd_vert_get(const BMVert &vert, const int offset)
{
  return *static_cast<const T *>(POINTER_OFFSET(vert.head.data, offset));
}

template<typename T> const T &bmesh_cd_loop_get(const BMLoop &loop, const int offset)
{
  return *static_cast<const T *>(POINTER_OFFSET(loop.head.data, offset));
}

template<typename T> const T &bmesh_cd_face_get(const BMFace &face, const int offset)
{
  return *static_cast<const T *>(POINTER_OFFSET(face.head.data, offset));
}

template<typename T>
void extract_data_vert_bmesh(const Set<BMFace *, 0> &faces,
                             const int cd_offset,
                             gpu::VertBuf &vbo,
                             const IndexRange scratch_range)
{
  PRF_scope(ProfileCategory::Draw);
  using Converter = AttributeConverter<T>;
  using VBOType = typename Converter::VBOType;
  VBOType *data = vbo.data<VBOType>().data() + scratch_range.start();
  VBOType *data_start = data;

  for (const BMFace *face : faces) {
    if (BM_elem_flag_test(face, BM_ELEM_HIDDEN)) {
      continue;
    }
    const BMLoop *l = face->l_first;
    *data = Converter::convert(bmesh_cd_vert_get<T>(*l->prev->v, cd_offset));
    data++;
    *data = Converter::convert(bmesh_cd_vert_get<T>(*l->v, cd_offset));
    data++;
    *data = Converter::convert(bmesh_cd_vert_get<T>(*l->next->v, cd_offset));
    data++;
  }
  UNUSED_VARS_NDEBUG(data_start);
  BLI_assert(data == data_start + scratch_range.size());
}

template<typename T>
void extract_data_face_bmesh(const Set<BMFace *, 0> &faces,
                             const int cd_offset,
                             gpu::VertBuf &vbo,
                             const IndexRange scratch_range)
{
  PRF_scope(ProfileCategory::Draw);
  using Converter = AttributeConverter<T>;
  using VBOType = typename Converter::VBOType;
  VBOType *data = vbo.data<VBOType>().data() + scratch_range.start();
  VBOType *data_start = data;

  for (const BMFace *face : faces) {
    if (BM_elem_flag_test(face, BM_ELEM_HIDDEN)) {
      continue;
    }
    std::fill_n(data, 3, Converter::convert(bmesh_cd_face_get<T>(*face, cd_offset)));
    data += 3;
  }
  UNUSED_VARS_NDEBUG(data_start);
  BLI_assert(data == data_start + scratch_range.size());
}

template<typename T>
void extract_data_corner_bmesh(const Set<BMFace *, 0> &faces,
                               const int cd_offset,
                               gpu::VertBuf &vbo,
                               const IndexRange scratch_range)
{
  PRF_scope(ProfileCategory::Draw);
  using Converter = AttributeConverter<T>;
  using VBOType = typename Converter::VBOType;
  VBOType *data = vbo.data<VBOType>().data() + scratch_range.start();
  VBOType *data_start = data;

  for (const BMFace *face : faces) {
    if (BM_elem_flag_test(face, BM_ELEM_HIDDEN)) {
      continue;
    }
    const BMLoop *l = face->l_first;
    *data = Converter::convert(bmesh_cd_loop_get<T>(*l->prev, cd_offset));
    data++;
    *data = Converter::convert(bmesh_cd_loop_get<T>(*l, cd_offset));
    data++;
    *data = Converter::convert(bmesh_cd_loop_get<T>(*l->next, cd_offset));
    data++;
  }
  UNUSED_VARS_NDEBUG(data_start);
  BLI_assert(data == data_start + scratch_range.size());
}

static int count_visible_tris_bmesh(const Set<BMFace *, 0> &faces)
{
  return std::count_if(faces.begin(), faces.end(), [&](const BMFace *face) {
    return !BM_elem_flag_test_bool(face, BM_ELEM_HIDDEN);
  });
}

DrawCacheImpl::~DrawCacheImpl()
{
  free_batches(lines_batches_.batches);
  free_batches(lines_batches_coarse_.batches);
  for (BatchCache &batch_cache : tris_batches_.values()) {
    free_batches(batch_cache.batches);
  }
  for (SharedBatch &shared : tris_shared_batches_.values()) {
    MEM_SAFE_DELETE(shared.batch);
  }
  for (SharedBatch &shared : lines_shared_batches_) {
    MEM_SAFE_DELETE(shared.batch);
  }
}

void DrawCacheImpl::free_nodes_with_changed_topology(const bke::pbvh::Tree &pbvh)
{
  PRF_scope(ProfileCategory::Draw);
  /* NOTE: Theoretically we shouldn't need to free batches with a changed triangle count, but
   * currently it's the simplest way to reallocate all the GPU data while keeping everything in a
   * consistent state. */
  IndexMaskMemory memory;
  const IndexMask nodes_to_free = IndexMask::from_bits(dirty_topology_, memory);
  if (nodes_to_free.is_empty()) {
    return;
  }

  dirty_topology_.clear_and_shrink();

  free_ibos(ibo_lines_.ibos, nodes_to_free);
  free_ibos(ibo_lines_coarse_.ibos, nodes_to_free);
  free_ibos(ibo_tris_.ibos, nodes_to_free);
  free_ibos(ibo_tris_coarse_.ibos, nodes_to_free);

  if (pbvh.type() == bke::pbvh::Type::BMesh) {
    /* For BMesh, VBOs are only filled with data for visible triangles, and topology can also
     * completely change due to dynamic topology, so VBOs must be rebuilt from scratch. For other
     * types, actual topology doesn't change, and visibility changes are accounted for by the index
     * buffers. */
    for (AttributeData &data : attribute_vbos_.values()) {
      data.tag_dirty(nodes_to_free);
    }
  }
}

/** \brief The number of face corners of a node's visible faces. */
static int count_visible_corners_mesh(const OffsetIndices<int> faces,
                                      const Span<bool> hide_poly,
                                      const bke::pbvh::MeshNode &node)
{
  if (hide_poly.is_empty()) {
    return node.corners_num();
  }
  int count = 0;
  for (const int face : node.faces()) {
    if (!hide_poly[face]) {
      count += faces[face].size();
    }
  }
  return count;
}

/** \brief The number of triangles of a node's visible faces. */
static int count_visible_tris_mesh(const OffsetIndices<int> faces,
                                   const Span<bool> hide_poly,
                                   const bke::pbvh::MeshNode &node)
{
  if (hide_poly.is_empty()) {
    return poly_to_tri_count(node.faces().size(), node.corners_num());
  }
  int count = 0;
  for (const int face : node.faces()) {
    if (!hide_poly[face]) {
      count += bke::mesh::face_triangles_num(faces[face].size());
    }
  }
  return count;
}

/**
 * Phantom: the step between the grid points the coarse "fast navigate" level draws: a quarter of
 * the resolution per axis (a sixteenth of the triangles), down to one quad per grid. It used to
 * draw one quad per grid at every level, a jump in shape for little extra speed.
 */
static int grid_coarse_skip(const CCGKey &key)
{
  return key.level > 0 ? std::min(1 << (key.level - 1), 4) : 1;
}

/**
 * \brief The size of the grid "quad" area that a node displays, in quads per axis.
 */
static int grid_quads_per_axis(const CCGKey &key, const bool coarse)
{
  const int skip = coarse ? grid_coarse_skip(key) : 1;
  return (key.grid_size - 1) / skip;
}

/**
 * Phantom: the quads a node draws at the given grid step, without hidden ones. Matches the loops
 * of #create_tri_index_grids, so the index buffers are no larger than what is drawn (with the
 * coarse level they were the full size, padded with degenerate triangles).
 */
static int count_drawn_grid_quads(const BitGroupVector<> &grid_hidden,
                                  const Span<int> grids,
                                  const int grid_size,
                                  const int skip)
{
  const int per_axis = (grid_size - 1) / skip;
  if (grid_hidden.is_empty()) {
    return int(grids.size()) * per_axis * per_axis;
  }
  int count = 0;
  for (const int grid : grids) {
    const BoundedBitSpan gh = grid_hidden[grid];
    for (int y = 0; y < grid_size - skip; y += skip) {
      for (int x = 0; x < grid_size - skip; x += skip) {
        if (!BKE_paint_is_grid_face_hidden(gh, grid_size, x, y)) {
          count++;
        }
      }
    }
  }
  return count;
}

/**
 * \brief Calculate the number of vertices each node stores in the shared vertex buffer.
 */
static void calc_vert_counts(const Object &object,
                             const BitSpan use_flat_layout,
                             const IndexMask &node_mask,
                             MutableSpan<int> counts)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  switch (pbvh.type()) {
    case bke::pbvh::Type::Mesh: {
      const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
      /* Flat nodes store each face corner, the others each vertex once. */
      node_mask.foreach_index([&](const int i, const int pos) {
        counts[pos] = use_flat_layout[i] ? nodes[i].corners_num() : nodes[i].all_verts().size();
      });
      break;
    }
    case bke::pbvh::Type::Grids: {
      const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
      const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
      /* Flat shaded nodes store separate vertices for each of the four quad corners. */
      const int verts_per_grid_flat = square_i(subdiv_ccg.grid_size - 1) * 4;
      const int verts_per_grid = square_i(subdiv_ccg.grid_size);
      node_mask.foreach_index([&](const int i, const int pos) {
        counts[pos] = nodes[i].grids().size() *
                      (use_flat_layout[i] ? verts_per_grid_flat : verts_per_grid);
      });
      break;
    }
    case bke::pbvh::Type::BMesh: {
      /* BMesh vertex buffers only contain the visible triangles, stored per face corner. */
      const Span<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
      node_mask.foreach_index(
          [&](const int i, const int pos) {
            counts[pos] = count_visible_tris_bmesh(nodes[i].bm_faces_) * 3;
          },
          exec_mode::grain_size(8));
      break;
    }
  }
}

/**
 * \brief Calculate the number of indices each node needs in the shared wireframe index buffer.
 *
 * Hidden faces aren't indexed, so the counts change whenever visibility does.
 */
static void calc_lines_index_counts(const Object &object,
                                    const OrigMeshData &orig_mesh_data,
                                    const bool coarse,
                                    const IndexMask &node_mask,
                                    MutableSpan<int> counts)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  switch (pbvh.type()) {
    case bke::pbvh::Type::Mesh: {
      const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
      const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
      const OffsetIndices<int> faces = mesh.faces();
      const VArraySpan hide_poly = *orig_mesh_data.attributes.lookup<bool>(".hide_poly",
                                                                           bke::AttrDomain::Face);
      node_mask.foreach_index(
          [&](const int i, const int pos) {
            /* One line per face corner, two indices per line. */
            counts[pos] = count_visible_corners_mesh(faces, hide_poly, nodes[i]) * 2;
          },
          exec_mode::grain_size(8));
      break;
    }
    case bke::pbvh::Type::Grids: {
      const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
      const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
      const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
      /* Hidden grid elements are counted too, they are collapsed to a degenerate line instead. */
      const int quads_per_axis = grid_quads_per_axis(key, coarse);
      const int indices_per_grid = 4 * quads_per_axis * (quads_per_axis + 1);
      node_mask.foreach_index([&](const int i, const int pos) {
        counts[pos] = nodes[i].grids().size() * indices_per_grid;
      });
      break;
    }
    case bke::pbvh::Type::BMesh: {
      const Span<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
      node_mask.foreach_index(
          [&](const int i, const int pos) {
            /* Three lines per triangle, two indices per line. */
            counts[pos] = count_visible_tris_bmesh(nodes[i].bm_faces_) * 6;
          },
          exec_mode::grain_size(8));
      break;
    }
  }
}

/**
 * \brief Calculate the number of indices each node needs in the triangle index buffer.
 *
 * Hidden faces aren't indexed, so the counts change whenever visibility does.
 */
static void calc_tris_index_counts(const Object &object,
                                   const OrigMeshData &orig_mesh_data,
                                   const bool coarse,
                                   const IndexMask &node_mask,
                                   MutableSpan<int> counts)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  switch (pbvh.type()) {
    case bke::pbvh::Type::Mesh: {
      const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
      const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
      const OffsetIndices<int> faces = mesh.faces();
      const VArraySpan hide_poly = *orig_mesh_data.attributes.lookup<bool>(".hide_poly",
                                                                           bke::AttrDomain::Face);
      node_mask.foreach_index(
          [&](const int i, const int pos) {
            counts[pos] = count_visible_tris_mesh(faces, hide_poly, nodes[i]) * 3;
          },
          exec_mode::grain_size(8));
      break;
    }
    case bke::pbvh::Type::Grids: {
      const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
      const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
      const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
      const int skip = coarse ? grid_coarse_skip(key) : 1;
      node_mask.foreach_index(
          [&](const int i, const int pos) {
            /* Two triangles per visible quad, three indices per triangle. */
            counts[pos] = count_drawn_grid_quads(
                              subdiv_ccg.grid_hidden, nodes[i].grids(), key.grid_size, skip) *
                          6;
          },
          exec_mode::grain_size(8));
      break;
    }
    case bke::pbvh::Type::BMesh: {
      const Span<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
      node_mask.foreach_index(
          [&](const int i, const int pos) {
            counts[pos] = count_visible_tris_bmesh(nodes[i].bm_faces_) * 3;
          },
          exec_mode::grain_size(8));
      break;
    }
  }
}

void DrawCacheImpl::AttributeData::ensure_nodes_capacity(const int64_t nodes_num)
{
  /* Only grow, to keep the state of nodes outside the current mask. */
  if (this->dirty_nodes.size() < nodes_num) {
    this->dirty_nodes.resize(nodes_num, true);
  }
}
void DrawCacheImpl::AttributeData::ensure_buffer_capacity(const uint64_t needed_vertex_len)
{
  BLI_assert_msg(vbo != nullptr, "Can only grow an existing VBO");
  if (vbo->vertex_len < needed_vertex_len) {
    gpu::VertBufPtr new_vbo = gpu::VertBufPtr(
        GPU_vertbuf_create_with_format_ex(vbo->format, GPU_USAGE_DEVICE_ONLY));
    new_vbo->allocate(uint32_t(needed_vertex_len));
    new_vbo->upload();
    /* Preserve the data of the existing vertices by copying the used vertex data to the new
     * buffer. */
    new_vbo->copy_sub(*vbo, 0, 0, vbo->vertex_len);
    vbo.swap(new_vbo);
  }
}

static void update_positions_mesh(const Object &object,
                                  const Buffer &vbo,
                                  const int vert_len,
                                  const BitSpan use_flat_layout,
                                  const IndexMask &node_mask,
                                  const ScratchOffsets &scratch_offsets,
                                  DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
  const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
  const OffsetIndices<int> faces = mesh.faces();
  const Span<int> corner_verts = mesh.corner_verts();
  const Span<float3> vert_positions = bke::pbvh::vert_positions_eval_from_eval(object);
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(position_format(),
                                                          scratch_offsets.total);
  node_mask.foreach_index(
      [&](const int node_index, const int scratch_index) {
        const IndexRange scratch_range = vbo.scratch_range(node_index,
                                                           scratch_offsets.offsets[scratch_index]);
        if (use_flat_layout[node_index]) {
          extract_data_vert_mesh<float3>(faces,
                                         corner_verts,
                                         vert_positions,
                                         nodes[node_index].faces(),
                                         *scratch_vbo,
                                         scratch_range);
        }
        else {
          extract_data_vert_indexed_mesh<float3>(
              nodes[node_index].all_verts(), vert_positions, *scratch_vbo, scratch_range);
        }
      },
      exec_mode::grain_size(1));
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

static void update_normals_mesh(const Object &object,
                                const Buffer &vbo,
                                const int vert_len,
                                const BitSpan use_flat_layout,
                                const IndexMask &node_mask,
                                const ScratchOffsets &scratch_offsets,
                                DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
  const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
  const OffsetIndices<int> faces = mesh.faces();
  const Span<int> corner_verts = mesh.corner_verts();
  const Span<float3> vert_normals = bke::pbvh::vert_normals_eval_from_eval(object);
  const Span<float3> face_normals = bke::pbvh::face_normals_eval_from_eval(object);
  const bke::AttributeAccessor attributes = mesh.attributes();
  const VArraySpan sharp_faces = *attributes.lookup<bool>("sharp_face", bke::AttrDomain::Face);
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(normal_format(), scratch_offsets.total);
  node_mask.foreach_index(
      [&](const int node_index, const int scratch_index) {
        PackedNormal *data = scratch_vbo->data<PackedNormal>().data() +
                             scratch_offsets.offsets[scratch_index];
        PackedNormal *data_start = data;

        if (!use_flat_layout[node_index]) {
          /* Nodes that store each vertex once have no sharp faces once normals are drawn
           * (#layout_uses_sharp_faces_). */
          for (const int vert : nodes[node_index].all_verts()) {
            *data = normal_to_gpu(vert_normals[vert]);
            data++;
          }
        }
        else {
          for (const int face : nodes[node_index].faces()) {
            if (!sharp_faces.is_empty() && sharp_faces[face]) {
              const int face_size = faces[face].size();
              std::fill_n(data, face_size, normal_to_gpu(face_normals[face]));
              data += face_size;
            }
            else {
              for (const int vert : corner_verts.slice(faces[face])) {
                *data = normal_to_gpu(vert_normals[vert]);
                data++;
              }
            }
          }
        }
        UNUSED_VARS_NDEBUG(data_start);
        BLI_assert(data == data_start + vbo.sizes[node_index]);
      },
      exec_mode::grain_size(1));
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

BLI_NOINLINE static void update_masks_mesh(const Object &object,
                                           const OrigMeshData &orig_mesh_data,
                                           const Buffer &vbo,
                                           const int vert_len,
                                           const BitSpan use_flat_layout,
                                           const IndexMask &node_mask,
                                           const ScratchOffsets &scratch_offsets,
                                           DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
  const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
  const OffsetIndices<int> faces = mesh.faces();
  const Span<int> corner_verts = mesh.corner_verts();
  const VArraySpan mask = *orig_mesh_data.attributes.lookup<float>(".sculpt_mask",
                                                                   bke::AttrDomain::Point);
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(mask_format(), scratch_offsets.total);
  if (!mask.is_empty()) {
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          float *data = scratch_vbo->data<float>().data() + scratch_offsets.offsets[scratch_index];
          float *data_start = data;
          if (use_flat_layout[node_index]) {
            for (const int face : nodes[node_index].faces()) {
              for (const int vert : corner_verts.slice(faces[face])) {
                *data = mask[vert];
                data++;
              }
            }
          }
          else {
            for (const int vert : nodes[node_index].all_verts()) {
              *data = mask[vert];
              data++;
            }
          }
          UNUSED_VARS_NDEBUG(data_start);
          BLI_assert(data == data_start + vbo.sizes[node_index]);
        },
        exec_mode::grain_size(1));
  }
  else {
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          scratch_vbo->data<float>()
              .slice(scratch_offsets.offsets[scratch_index],
                     vbo.acquired[node_index] ? vbo.sizes[node_index] : 0)
              .fill(0.0f);
        },
        exec_mode::grain_size(64));
  }
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

BLI_NOINLINE static void update_face_sets_mesh(const Object &object,
                                               const OrigMeshData &orig_mesh_data,
                                               const Buffer &vbo,
                                               const int vert_len,
                                               const BitSpan use_flat_layout,
                                               const IndexMask &node_mask,
                                               const ScratchOffsets &scratch_offsets,
                                               DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
  const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
  const OffsetIndices<int> faces = mesh.faces();
  const int color_default = orig_mesh_data.face_set_default;
  const int color_seed = orig_mesh_data.face_set_seed;
  const VArraySpan face_sets = *orig_mesh_data.attributes.lookup<int>(".sculpt_face_set",
                                                                      bke::AttrDomain::Face);
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(face_set_format(),
                                                          scratch_offsets.total);
  if (!face_sets.is_empty()) {
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          uchar4 *data = scratch_vbo->data<uchar4>().data() +
                         scratch_offsets.offsets[scratch_index];
          if (!use_flat_layout[node_index]) {
            /* Nodes that store each vertex once have a single face set. */
            uchar4 fset_color;
            BKE_paint_face_set_color_get(face_sets[nodes[node_index].faces().first()],
                                         color_default,
                                         color_seed,
                                         fset_color);
            std::fill_n(data, vbo.sizes[node_index], fset_color);
            return;
          }
          uchar4 *data_start = data;
          for (const int face : nodes[node_index].faces()) {
            const int id = face_sets[face];

            uchar4 fset_color;
            BKE_paint_face_set_color_get(id, color_default, color_seed, fset_color);

            const int face_size = faces[face].size();
            std::fill_n(data, face_size, fset_color);
            data += face_size;
          }
          UNUSED_VARS_NDEBUG(data_start);
          BLI_assert(data == data_start + vbo.sizes[node_index]);
        },
        exec_mode::grain_size(1));
  }
  else {
    /* Phantom: without polygroups the mesh is all in polygroup 1. */
    uchar4 color;
    BKE_paint_face_set_color_get(1, color_default, color_seed, color);
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          scratch_vbo->data<uchar4>()
              .slice(scratch_offsets.offsets[scratch_index],
                     vbo.acquired[node_index] ? vbo.sizes[node_index] : 0)
              .fill(color);
        },
        exec_mode::grain_size(64));
  }
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

BLI_NOINLINE static void update_generic_attribute_mesh(const Object &object,
                                                       const OrigMeshData &orig_mesh_data,
                                                       const Buffer &vbo,
                                                       const int vert_len,
                                                       const StringRef name,
                                                       const BitSpan use_flat_layout,
                                                       const IndexMask &node_mask,
                                                       const ScratchOffsets &scratch_offsets,
                                                       DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
  const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
  const OffsetIndices<int> faces = mesh.faces();
  const Span<int> corner_verts = mesh.corner_verts();
  const bke::AttributeAccessor attributes = orig_mesh_data.attributes;
  const bke::GAttributeReader attr = attributes.lookup(name);
  if (!attr || attr.domain == bke::AttrDomain::Edge) {
    return;
  }
  const bke::AttrType data_type = bke::cpp_type_to_attribute_type(attr.varray.type());
  const GPUVertFormat format = attribute_format(orig_mesh_data, name, data_type);
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(format, scratch_offsets.total);
  node_mask.foreach_index(
      [&](const int node_index, const int scratch_index) {
        const IndexRange scratch_range = vbo.scratch_range(node_index,
                                                           scratch_offsets.offsets[scratch_index]);
        bke::attribute_math::to_static_type(attr.varray.type(), [&]<typename T>() {
          if constexpr (!std::is_void_v<typename AttributeConverter<T>::VBOType>) {
            const VArraySpan<T> src = attr.varray.typed<T>();
            if (!use_flat_layout[node_index]) {
              if (attr.domain == bke::AttrDomain::Point) {
                extract_data_vert_indexed_mesh<T>(
                    nodes[node_index].all_verts(), src, *scratch_vbo, scratch_range);
              }
              else {
                /* Face and corner attributes make every node use the flat layout. */
                BLI_assert_unreachable();
                using Converter = AttributeConverter<T>;
                scratch_vbo->data<typename Converter::VBOType>()
                    .slice(scratch_range)
                    .fill(Converter::convert(T()));
              }
              return;
            }
            switch (attr.domain) {
              case bke::AttrDomain::Point:
                extract_data_vert_mesh<T>(faces,
                                          corner_verts,
                                          src,
                                          nodes[node_index].faces(),
                                          *scratch_vbo,
                                          scratch_range);
                break;
              case bke::AttrDomain::Face:
                extract_data_face_mesh<T>(
                    faces, src, nodes[node_index].faces(), *scratch_vbo, scratch_range);
                break;
              case bke::AttrDomain::Corner:
                extract_data_corner_mesh<T>(
                    faces, src, nodes[node_index].faces(), *scratch_vbo, scratch_range);
                break;
              default:
                BLI_assert_unreachable();
            }
          }
        });
      },
      exec_mode::grain_size(1));
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

BLI_NOINLINE static void update_positions_grids(const Object &object,
                                                const Buffer &vbo,
                                                const int vert_len,
                                                const BitSpan use_flat_layout,
                                                const IndexMask &node_mask,
                                                const ScratchOffsets &scratch_offsets,
                                                DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
  const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
  const Span<float3> positions = subdiv_ccg.positions;
  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(position_format(),
                                                          scratch_offsets.total);
  node_mask.foreach_index(
      [&](const int node_index, const int scratch_index) {
        float3 *data = scratch_vbo->data<float3>().data() + scratch_offsets.offsets[scratch_index];
        float3 *data_start = data;
        if (use_flat_layout[node_index]) {
          const int grid_size_1 = key.grid_size - 1;
          for (const int grid : nodes[node_index].grids()) {
            const Span<float3> grid_positions = positions.slice(bke::ccg::grid_range(key, grid));
            for (int y = 0; y < grid_size_1; y++) {
              for (int x = 0; x < grid_size_1; x++) {
                *data = grid_positions[CCG_grid_xy_to_index(key.grid_size, x, y)];
                data++;
                *data = grid_positions[CCG_grid_xy_to_index(key.grid_size, x + 1, y)];
                data++;
                *data = grid_positions[CCG_grid_xy_to_index(key.grid_size, x + 1, y + 1)];
                data++;
                *data = grid_positions[CCG_grid_xy_to_index(key.grid_size, x, y + 1)];
                data++;
              }
            }
          }
        }
        else {
          for (const int grid : nodes[node_index].grids()) {
            const Span<float3> grid_positions = positions.slice(bke::ccg::grid_range(key, grid));
            std::copy_n(grid_positions.data(), grid_positions.size(), data);
            data += grid_positions.size();
          }
        }
        UNUSED_VARS_NDEBUG(data_start);
        BLI_assert(data == data_start + vbo.sizes[node_index]);
      },
      exec_mode::grain_size(1));
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

BLI_NOINLINE static void update_normals_grids(const Object &object,
                                              const OrigMeshData &orig_mesh_data,
                                              const Buffer &vbo,
                                              const int vert_len,
                                              const BitSpan use_flat_layout,
                                              const IndexMask &node_mask,
                                              const ScratchOffsets &scratch_offsets,
                                              DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
  const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
  const Span<float3> positions = subdiv_ccg.positions;
  const Span<float3> normals = subdiv_ccg.normals;
  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
  const Span<int> grid_to_face_map = subdiv_ccg.grid_to_face_map;
  const bke::AttributeAccessor attributes = orig_mesh_data.attributes;
  const VArraySpan sharp_faces = *attributes.lookup<bool>("sharp_face", bke::AttrDomain::Face);
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(normal_format(), scratch_offsets.total);
  node_mask.foreach_index(
      [&](const int node_index, const int scratch_index) {
        PackedNormal *data = scratch_vbo->data<PackedNormal>().data() +
                             scratch_offsets.offsets[scratch_index];
        PackedNormal *data_start = data;

        if (use_flat_layout[node_index]) {
          const int grid_size_1 = key.grid_size - 1;
          for (const int grid : nodes[node_index].grids()) {
            const Span<float3> grid_positions = positions.slice(bke::ccg::grid_range(key, grid));
            const Span<float3> grid_normals = normals.slice(bke::ccg::grid_range(key, grid));
            if (!sharp_faces.is_empty() && sharp_faces[grid_to_face_map[grid]]) {
              for (int y = 0; y < grid_size_1; y++) {
                for (int x = 0; x < grid_size_1; x++) {
                  float3 no;
                  normal_quad_v3(no,
                                 grid_positions[CCG_grid_xy_to_index(key.grid_size, x, y + 1)],
                                 grid_positions[CCG_grid_xy_to_index(key.grid_size, x + 1, y + 1)],
                                 grid_positions[CCG_grid_xy_to_index(key.grid_size, x + 1, y)],
                                 grid_positions[CCG_grid_xy_to_index(key.grid_size, x, y)]);
                  std::fill_n(data, 4, normal_to_gpu(no));
                  data += 4;
                }
              }
            }
            else {
              for (int y = 0; y < grid_size_1; y++) {
                for (int x = 0; x < grid_size_1; x++) {
                  std::fill_n(data,
                              4,
                              normal_to_gpu(
                                  grid_normals[CCG_grid_xy_to_index(key.grid_size, x, y)]));
                  data += 4;
                }
              }
            }
          }
        }
        else {
          /* The non-flat VBO layout does not support sharp faces. */
          for (const int grid : nodes[node_index].grids()) {
            for (const float3 &normal : normals.slice(bke::ccg::grid_range(key, grid))) {
              *data = normal_to_gpu(normal);
              data++;
            }
          }
        }
        UNUSED_VARS_NDEBUG(data_start);
        BLI_assert(data == data_start + vbo.sizes[node_index]);
      },
      exec_mode::grain_size(1));
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

BLI_NOINLINE static void update_masks_grids(const Object &object,
                                            const Buffer &vbo,
                                            const int vert_len,
                                            const BitSpan use_flat_layout,
                                            const IndexMask &node_mask,
                                            const ScratchOffsets &scratch_offsets,
                                            DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
  const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
  const Span<float> masks = subdiv_ccg.masks;
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(mask_format(), scratch_offsets.total);
  if (!masks.is_empty()) {
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          float *data = scratch_vbo->data<float>().data() + scratch_offsets.offsets[scratch_index];
          float *data_start = data;
          if (use_flat_layout[node_index]) {
            const int grid_size_1 = key.grid_size - 1;
            for (const int grid : nodes[node_index].grids()) {
              const Span<float> grid_masks = masks.slice(bke::ccg::grid_range(key, grid));
              for (int y = 0; y < grid_size_1; y++) {
                for (int x = 0; x < grid_size_1; x++) {
                  *data = grid_masks[CCG_grid_xy_to_index(key.grid_size, x, y)];
                  data++;
                  *data = grid_masks[CCG_grid_xy_to_index(key.grid_size, x + 1, y)];
                  data++;
                  *data = grid_masks[CCG_grid_xy_to_index(key.grid_size, x + 1, y + 1)];
                  data++;
                  *data = grid_masks[CCG_grid_xy_to_index(key.grid_size, x, y + 1)];
                  data++;
                }
              }
            }
          }
          else {
            for (const int grid : nodes[node_index].grids()) {
              const Span<float> grid_masks = masks.slice(bke::ccg::grid_range(key, grid));
              std::copy_n(grid_masks.data(), grid_masks.size(), data);
              data += grid_masks.size();
            }
          }
          UNUSED_VARS_NDEBUG(data_start);
          BLI_assert(data == data_start + vbo.sizes[node_index]);
        },
        exec_mode::grain_size(1));
  }
  else {
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          scratch_vbo->data<float>()
              .slice(scratch_offsets.offsets[scratch_index],
                     vbo.acquired[node_index] ? vbo.sizes[node_index] : 0)
              .fill(0.0f);
        },
        exec_mode::grain_size(64));
  }
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

BLI_NOINLINE static void update_face_sets_grids(const Object &object,
                                                const OrigMeshData &orig_mesh_data,
                                                const Buffer &vbo,
                                                const int vert_len,
                                                const BitSpan use_flat_layout,
                                                const IndexMask &node_mask,
                                                const ScratchOffsets &scratch_offsets,
                                                DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
  const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
  const int color_default = orig_mesh_data.face_set_default;
  const int color_seed = orig_mesh_data.face_set_seed;
  const Span<int> grid_to_face_map = subdiv_ccg.grid_to_face_map;
  const bke::AttributeAccessor attributes = orig_mesh_data.attributes;
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(face_set_format(),
                                                          scratch_offsets.total);
  if (const VArray<int> face_sets = *attributes.lookup<int>(".sculpt_face_set",
                                                            bke::AttrDomain::Face))
  {
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          const Span<int> grids = nodes[node_index].grids();
          const int verts_per_grid = use_flat_layout[node_index] ?
                                         square_i(key.grid_size - 1) * 4 :
                                         square_i(key.grid_size);
          uchar4 *data = scratch_vbo->data<uchar4>().data() +
                         scratch_offsets.offsets[scratch_index];
          uchar4 *data_start = data;
          for (const int j : grids.index_range()) {
            uchar4 color;
            const int fset = face_sets[grid_to_face_map[grids[j]]];
            BKE_paint_face_set_color_get(fset, color_default, color_seed, color);

            std::fill_n(data, verts_per_grid, color);
            data += verts_per_grid;
          }
          UNUSED_VARS_NDEBUG(data_start);
          BLI_assert(data == data_start + vbo.sizes[node_index]);
        },
        exec_mode::grain_size(1));
  }
  else {
    uchar4 color;
    BKE_paint_face_set_color_get(1, color_default, color_seed, color);
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          scratch_vbo->data<uchar4>()
              .slice(scratch_offsets.offsets[scratch_index],
                     vbo.acquired[node_index] ? vbo.sizes[node_index] : 0)
              .fill(color);
        },
        exec_mode::grain_size(1));
  }
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

static void calc_quad_uv(const SubdivCCG &subdiv_ccg,
                         const int uv_map_index,
                         const float grid_size_1_inv,
                         const int ptex_face_index,
                         const int grid_start,
                         const int corner,
                         const int x,
                         const int y,
                         const int offset,
                         MutableSpan<float2> result)
{
  const float grid_u = x * grid_size_1_inv;
  const float grid_v = y * grid_size_1_inv;
  float u;
  float v;
  bke::subdiv::rotate_grid_to_quad(corner, grid_u, grid_v, &u, &v);
  bke::subdiv::eval_face_varying(
      subdiv_ccg.subdiv, uv_map_index, ptex_face_index, u, v, result[grid_start + offset]);
}

static void calc_non_quad_uv(const SubdivCCG &subdiv_ccg,
                             const int uv_map_index,
                             const float grid_size_1_inv,
                             const int ptex_face_index,
                             const int grid_start,
                             const int x,
                             const int y,
                             const int offset,
                             MutableSpan<float2> result)
{
  const float u = 1.0f - (y * grid_size_1_inv);
  const float v = 1.0f - (x * grid_size_1_inv);
  bke::subdiv::eval_face_varying(
      subdiv_ccg.subdiv, uv_map_index, ptex_face_index, u, v, result[grid_start + offset]);
}

static void calc_node_uvs_for_flat_layout(const SubdivCCG &subdiv_ccg,
                                          const int uv_map_index,
                                          Span<int> grids,
                                          MutableSpan<float2> result)
{
  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
  const int grid_size_1 = key.grid_size - 1;
  const int grid_area_1 = grid_size_1 * grid_size_1;
  const int verts_per_grid = grid_area_1 * 4;

  const Span<int> face_ptex_offset = bke::subdiv::face_ptex_offset_get(subdiv_ccg.subdiv);
  const float grid_size_1_inv = 1.0f / (key.grid_size - 1);

  threading::parallel_for(grids.index_range(), 1024, [&](const IndexRange range) {
    for (const int i : range) {
      const int grid = grids[i];
      const int face_index = subdiv_ccg.grid_to_face_map[grid];
      const IndexRange face = subdiv_ccg.faces[face_index];
      const int corner = grid - face.start();
      if (face.size() == 4) {
        const int ptex_face_index = face_ptex_offset[face_index];
        for (int y = 0; y < grid_size_1; y++) {
          for (int x = 0; x < grid_size_1; x++) {
            const int start = verts_per_grid * i + CCG_grid_xy_to_index(grid_size_1, x, y) * 4;
            calc_quad_uv(subdiv_ccg,
                         uv_map_index,
                         grid_size_1_inv,
                         ptex_face_index,
                         start,
                         corner,
                         x,
                         y,
                         0,
                         result);
            calc_quad_uv(subdiv_ccg,
                         uv_map_index,
                         grid_size_1_inv,
                         ptex_face_index,
                         start,
                         corner,
                         x + 1,
                         y,
                         1,
                         result);
            calc_quad_uv(subdiv_ccg,
                         uv_map_index,
                         grid_size_1_inv,
                         ptex_face_index,
                         start,
                         corner,
                         x + 1,
                         y + 1,
                         2,
                         result);
            calc_quad_uv(subdiv_ccg,
                         uv_map_index,
                         grid_size_1_inv,
                         ptex_face_index,
                         start,
                         corner,
                         x,
                         y + 1,
                         3,
                         result);
          }
        }
      }
      else {
        const int ptex_face_index = face_ptex_offset[face_index] + corner;
        for (int y = 0; y < grid_size_1; y++) {
          for (int x = 0; x < grid_size_1; x++) {
            const int start = verts_per_grid * i + CCG_grid_xy_to_index(grid_size_1, x, y) * 4;
            calc_non_quad_uv(subdiv_ccg,
                             uv_map_index,
                             grid_size_1_inv,
                             ptex_face_index,
                             start,
                             x,
                             y,
                             0,
                             result);
            calc_non_quad_uv(subdiv_ccg,
                             uv_map_index,
                             grid_size_1_inv,
                             ptex_face_index,
                             start,
                             x + 1,
                             y,
                             1,
                             result);
            calc_non_quad_uv(subdiv_ccg,
                             uv_map_index,
                             grid_size_1_inv,
                             ptex_face_index,
                             start,
                             x + 1,
                             y + 1,
                             2,
                             result);
            calc_non_quad_uv(subdiv_ccg,
                             uv_map_index,
                             grid_size_1_inv,
                             ptex_face_index,
                             start,
                             x,
                             y + 1,
                             3,
                             result);
          }
        }
      }
    }
  });
}

static void calc_node_uvs_for_standard_layout(const SubdivCCG &subdiv_ccg,
                                              const int uv_map_index,
                                              Span<int> grids,
                                              MutableSpan<float2> result)
{
  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
  const int grid_size = key.grid_size;
  const int grid_area = key.grid_area;

  const Span<int> face_ptex_offset = bke::subdiv::face_ptex_offset_get(subdiv_ccg.subdiv);
  const float grid_size_1_inv = 1.0f / (grid_size - 1);
  threading::parallel_for(grids.index_range(), 1024, [&](const IndexRange range) {
    for (const int i : range) {
      const int grid = grids[i];
      const int face_index = subdiv_ccg.grid_to_face_map[grid];
      const IndexRange face = subdiv_ccg.faces[face_index];
      const int corner = grid - face.start();
      if (face.size() == 4) {
        const int ptex_face_index = face_ptex_offset[face_index];
        for (int y = 0; y < grid_size; y++) {
          const float grid_v = y * grid_size_1_inv;
          for (int x = 0; x < grid_size; x++) {
            const float grid_u = x * grid_size_1_inv;
            float u;
            float v;
            bke::subdiv::rotate_grid_to_quad(corner, grid_u, grid_v, &u, &v);
            const int element = i * grid_area + CCG_grid_xy_to_index(grid_size, x, y);
            bke::subdiv::eval_face_varying(
                subdiv_ccg.subdiv, uv_map_index, ptex_face_index, u, v, result[element]);
          }
        }
      }
      else {
        const int ptex_face_index = face_ptex_offset[face_index] + corner;
        for (int y = 0; y < grid_size; y++) {
          const float u = 1.0f - (y * grid_size_1_inv);
          for (int x = 0; x < grid_size; x++) {
            const float v = 1.0f - (x * grid_size_1_inv);
            const int element = i * grid_area + CCG_grid_xy_to_index(grid_size, x, y);
            bke::subdiv::eval_face_varying(
                subdiv_ccg.subdiv, uv_map_index, ptex_face_index, u, v, result[element]);
          }
        }
      }
    }
  });
}

static void calc_node_uvs(const SubdivCCG &subdiv_ccg,
                          const int uv_map_index,
                          const bke::pbvh::GridsNode &node,
                          const bool use_flat_layout,
                          MutableSpan<float2> result)
{
  const Span<int> grids = node.grids();
  if (use_flat_layout) {
    calc_node_uvs_for_flat_layout(subdiv_ccg, uv_map_index, grids, result);
  }
  if (!use_flat_layout) {
    calc_node_uvs_for_standard_layout(subdiv_ccg, uv_map_index, grids, result);
  }
}

BLI_NOINLINE static void fill_uvs_grids(const Object &object,
                                        const OrigMeshData &orig_mesh_data,
                                        const Buffer &vbo,
                                        const BitSpan use_flat_layout,
                                        const IndexMask &node_mask,
                                        const StringRef name,
                                        const ScratchOffsets &scratch_offsets,
                                        const gpu::VertBufPtr &scratch_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
  const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
  node_mask.foreach_index(
      [&](const int node_index, const int scratch_index) {
        float2 *data = scratch_vbo->data<float2>().data() + scratch_offsets.offsets[scratch_index];

        const int uv_channel = orig_mesh_data.uv_map_names.index_of_try(name);
        if (uv_channel == -1) [[unlikely]] {
          BLI_assert_msg(uv_channel != -1,
                         "Unable to find appropriate FVar channel for given UV map");
          const int64_t dst_len = vbo.acquired[node_index] ? vbo.sizes[node_index] : 0;
          std::fill_n(data, dst_len, float2(0));
        }
        else {
          calc_node_uvs(subdiv_ccg,
                        uv_channel,
                        nodes[node_index],
                        use_flat_layout[node_index],
                        {data, vbo.acquired[node_index] ? vbo.sizes[node_index] : 0});
        }
      },
      exec_mode::grain_size(1));
}

static float4 quad_weights_from_grid(const int corner,
                                     const int x,
                                     const int y,
                                     const float grid_size_1_inv)
{
  const float grid_u = x * grid_size_1_inv;
  const float grid_v = y * grid_size_1_inv;
  float u;
  float v;
  bke::subdiv::rotate_grid_to_quad(corner, grid_u, grid_v, &u, &v);
  return bke::subdiv::quad_weights_from_uv(u, v);
}

static float4 quad_weights_from_irregular_corner(const int x,
                                                 const int y,
                                                 const float grid_size_1_inv)
{
  const float u = 1.0f - (y * grid_size_1_inv);
  const float v = 1.0f - (x * grid_size_1_inv);

  return bke::subdiv::quad_weights_from_uv(u, v);
}

template<typename T>
static void interpolate_vert_data_for_flat_layout(
    const SubdivCCG &subdiv_ccg,
    const Span<int> corner_verts,
    const Span<int> grids,
    const Span<T> src,
    MutableSpan<typename AttributeConverter<T>::VBOType> vbo)
{
  using Converter = AttributeConverter<T>;
  using VBOType = typename Converter::VBOType;

  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
  const int grid_size_1 = key.grid_size - 1;
  const int grid_area_1 = grid_size_1 * grid_size_1;
  const int verts_per_grid = grid_area_1 * 4;
  const float grid_size_1_inv = 1.0f / (key.grid_size - 1);
  MutableSpan<VBOType> dst = vbo;

  threading::parallel_for(grids.index_range(), 1024, [&](const IndexRange range) {
    for (const int i : range) {
      const int grid = grids[i];
      const int face_index = subdiv_ccg.grid_to_face_map[grid];
      const IndexRange face = subdiv_ccg.faces[face_index];
      const int corner = grid - face.start();
      if (face.size() == 4) {
        Span<int> face_verts = corner_verts.slice(face);
        for (int y = 0; y < grid_size_1; y++) {
          for (int x = 0; x < grid_size_1; x++) {
            const int start = verts_per_grid * i + CCG_grid_xy_to_index(grid_size_1, x, y) * 4;
            dst[start] = Converter::convert(
                bke::attribute_math::mix4(quad_weights_from_grid(corner, x, y, grid_size_1_inv),
                                          src[face_verts[0]],
                                          src[face_verts[1]],
                                          src[face_verts[2]],
                                          src[face_verts[3]]));
            dst[start + 1] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_grid(corner, x + 1, y, grid_size_1_inv),
                src[face_verts[0]],
                src[face_verts[1]],
                src[face_verts[2]],
                src[face_verts[3]]));
            dst[start + 2] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_grid(corner, x + 1, y + 1, grid_size_1_inv),
                src[face_verts[0]],
                src[face_verts[1]],
                src[face_verts[2]],
                src[face_verts[3]]));
            dst[start + 3] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_grid(corner, x, y + 1, grid_size_1_inv),
                src[face_verts[0]],
                src[face_verts[1]],
                src[face_verts[2]],
                src[face_verts[3]]));
          }
        }
      }
      else {
        const std::array<T, 4> values =
            bke::subdiv::interpolate_vert_data_for_subdivided_irregular_face<T>(
                corner_verts, face, grid, src);
        for (int y = 0; y < grid_size_1; y++) {
          for (int x = 0; x < grid_size_1; x++) {
            const int start = verts_per_grid * i + CCG_grid_xy_to_index(grid_size_1, x, y) * 4;
            dst[start] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_irregular_corner(x, y, grid_size_1_inv),
                values[0],
                values[1],
                values[2],
                values[3]));
            dst[start + 1] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_irregular_corner(x + 1, y, grid_size_1_inv),
                values[0],
                values[1],
                values[2],
                values[3]));
            dst[start + 2] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_irregular_corner(x + 1, y + 1, grid_size_1_inv),
                values[0],
                values[1],
                values[2],
                values[3]));
            dst[start + 3] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_irregular_corner(x, y + 1, grid_size_1_inv),
                values[0],
                values[1],
                values[2],
                values[3]));
          }
        }
      }
    }
  });
}

template<typename T>
static void interpolate_vert_data_for_standard_layout(
    const SubdivCCG &subdiv_ccg,
    const Span<int> corner_verts,
    const Span<int> grids,
    const Span<T> src,
    MutableSpan<typename AttributeConverter<T>::VBOType> vbo)
{
  using Converter = AttributeConverter<T>;
  using VBOType = typename Converter::VBOType;

  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
  const int grid_size = key.grid_size;
  const int grid_area = key.grid_area;

  const float grid_size_1_inv = 1.0f / (grid_size - 1);
  MutableSpan<VBOType> dst = vbo;
  threading::parallel_for(grids.index_range(), 1024, [&](const IndexRange range) {
    for (const int i : range) {
      const int grid = grids[i];
      const int face_index = subdiv_ccg.grid_to_face_map[grid];
      const IndexRange face = subdiv_ccg.faces[face_index];
      const int corner = grid - face.start();
      if (face.size() == 4) {
        const Span<int> face_verts = corner_verts.slice(face);
        for (int y = 0; y < grid_size; y++) {
          for (int x = 0; x < grid_size; x++) {
            const int element = i * grid_area + CCG_grid_xy_to_index(grid_size, x, y);
            dst[element] = Converter::convert(
                bke::attribute_math::mix4(quad_weights_from_grid(corner, x, y, grid_size_1_inv),
                                          src[face_verts[0]],
                                          src[face_verts[1]],
                                          src[face_verts[2]],
                                          src[face_verts[3]]));
          }
        }
      }
      else {
        const std::array<T, 4> values =
            bke::subdiv::interpolate_vert_data_for_subdivided_irregular_face<T>(
                corner_verts, face, grid, src);
        for (int y = 0; y < grid_size; y++) {
          for (int x = 0; x < grid_size; x++) {
            const int element = i * grid_area + CCG_grid_xy_to_index(grid_size, x, y);
            dst[element] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_irregular_corner(x, y, grid_size_1_inv),
                values[0],
                values[1],
                values[2],
                values[3]));
          }
        }
      }
    }
  });
}

BLI_NOINLINE static void calc_generic_vert_attribute_grids(const Object &object,
                                                           const OrigMeshData &orig_mesh_data,
                                                           const Buffer &vbo,
                                                           const BitSpan use_flat_layout,
                                                           const IndexMask &node_mask,
                                                           const StringRef name,
                                                           const ScratchOffsets &scratch_offsets,
                                                           const gpu::VertBufPtr &scratch_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
  const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
  const bke::AttributeAccessor attributes = orig_mesh_data.attributes;
  const GVArraySpan attr = *attributes.lookup(name);

  const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
  const Span<int> corner_verts = mesh.corner_verts();

  node_mask.foreach_index(
      [&](const int node_index, const int scratch_index) {
        bke::attribute_math::to_static_type(attr.type(), [&]<typename T>() {
          if constexpr (!std::is_void_v<typename AttributeConverter<T>::VBOType>) {
            const int64_t dst_len = vbo.acquired[node_index] ? vbo.sizes[node_index] : 0;
            MutableSpan<typename AttributeConverter<T>::VBOType> dst =
                scratch_vbo->data<typename AttributeConverter<T>::VBOType>().slice(
                    scratch_offsets.offsets[scratch_index], dst_len);
            if (use_flat_layout[node_index]) {
              interpolate_vert_data_for_flat_layout<T>(
                  subdiv_ccg, corner_verts, nodes[node_index].grids(), attr.typed<T>(), dst);
            }
            else {
              interpolate_vert_data_for_standard_layout<T>(
                  subdiv_ccg, corner_verts, nodes[node_index].grids(), attr.typed<T>(), dst);
            }
          }
        });
      },
      exec_mode::grain_size(1));
}

template<typename T>
static void interpolate_corner_data_for_flat_layout(
    const SubdivCCG &subdiv_ccg,
    const Span<int> grids,
    const Span<T> src,
    MutableSpan<typename AttributeConverter<T>::VBOType> vbo)
{
  using Converter = AttributeConverter<T>;
  using VBOType = typename Converter::VBOType;

  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
  const int grid_size_1 = key.grid_size - 1;
  const int grid_area_1 = grid_size_1 * grid_size_1;
  const int verts_per_grid = grid_area_1 * 4;
  const float grid_size_1_inv = 1.0f / (key.grid_size - 1);
  MutableSpan<VBOType> dst = vbo;

  threading::parallel_for(grids.index_range(), 1024, [&](const IndexRange range) {
    for (const int i : range) {
      const int grid = grids[i];
      const int face_index = subdiv_ccg.grid_to_face_map[grid];
      const IndexRange face = subdiv_ccg.faces[face_index];
      const int corner = grid - face.start();
      if (face.size() == 4) {
        for (int y = 0; y < grid_size_1; y++) {
          for (int x = 0; x < grid_size_1; x++) {
            const int start = verts_per_grid * i + CCG_grid_xy_to_index(grid_size_1, x, y) * 4;
            dst[start] = Converter::convert(
                bke::attribute_math::mix4(quad_weights_from_grid(corner, x, y, grid_size_1_inv),
                                          src[face[0]],
                                          src[face[1]],
                                          src[face[2]],
                                          src[face[3]]));
            dst[start + 1] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_grid(corner, x + 1, y, grid_size_1_inv),
                src[face[0]],
                src[face[1]],
                src[face[2]],
                src[face[3]]));
            dst[start + 2] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_grid(corner, x + 1, y + 1, grid_size_1_inv),
                src[face[0]],
                src[face[1]],
                src[face[2]],
                src[face[3]]));
            dst[start + 3] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_grid(corner, x, y + 1, grid_size_1_inv),
                src[face[0]],
                src[face[1]],
                src[face[2]],
                src[face[3]]));
          }
        }
      }
      else {
        const std::array<T, 4> values =
            bke::subdiv::interpolate_corner_data_for_subdivided_irregular_face<T>(face, grid, src);
        for (int y = 0; y < grid_size_1; y++) {
          for (int x = 0; x < grid_size_1; x++) {
            const int start = verts_per_grid * i + CCG_grid_xy_to_index(grid_size_1, x, y) * 4;
            dst[start] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_irregular_corner(x, y, grid_size_1_inv),
                values[0],
                values[1],
                values[2],
                values[3]));
            dst[start + 1] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_irregular_corner(x + 1, y, grid_size_1_inv),
                values[0],
                values[1],
                values[2],
                values[3]));
            dst[start + 2] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_irregular_corner(x + 1, y + 1, grid_size_1_inv),
                values[0],
                values[1],
                values[2],
                values[3]));
            dst[start + 3] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_irregular_corner(x, y + 1, grid_size_1_inv),
                values[0],
                values[1],
                values[2],
                values[3]));
          }
        }
      }
    }
  });
}

template<typename T>
static void interpolate_corner_data_for_standard_layout(
    const SubdivCCG &subdiv_ccg,
    const Span<int> grids,
    const Span<T> src,
    MutableSpan<typename AttributeConverter<T>::VBOType> vbo)
{
  using Converter = AttributeConverter<T>;
  using VBOType = typename Converter::VBOType;

  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
  const int grid_size = key.grid_size;
  const int grid_area = key.grid_area;

  const float grid_size_1_inv = 1.0f / (grid_size - 1);
  MutableSpan<VBOType> dst = vbo;
  threading::parallel_for(grids.index_range(), 1024, [&](const IndexRange range) {
    for (const int i : range) {
      const int grid = grids[i];
      const int face_index = subdiv_ccg.grid_to_face_map[grid];
      const IndexRange face = subdiv_ccg.faces[face_index];
      const int corner = grid - face.start();
      if (face.size() == 4) {
        for (int y = 0; y < grid_size; y++) {
          for (int x = 0; x < grid_size; x++) {
            const int element = i * grid_area + CCG_grid_xy_to_index(grid_size, x, y);
            dst[element] = Converter::convert(
                bke::attribute_math::mix4(quad_weights_from_grid(corner, x, y, grid_size_1_inv),
                                          src[face[0]],
                                          src[face[1]],
                                          src[face[2]],
                                          src[face[3]]));
          }
        }
      }
      else {
        const std::array<T, 4> values =
            bke::subdiv::interpolate_corner_data_for_subdivided_irregular_face<T>(face, grid, src);
        for (int y = 0; y < grid_size; y++) {
          for (int x = 0; x < grid_size; x++) {
            const int element = i * grid_area + CCG_grid_xy_to_index(grid_size, x, y);
            dst[element] = Converter::convert(bke::attribute_math::mix4(
                quad_weights_from_irregular_corner(x, y, grid_size_1_inv),
                values[0],
                values[1],
                values[2],
                values[3]));
          }
        }
      }
    }
  });
}

BLI_NOINLINE static void calc_generic_corner_attribute_grids(const Object &object,
                                                             const OrigMeshData &orig_mesh_data,
                                                             const Buffer &vbo,
                                                             const BitSpan use_flat_layout,
                                                             const IndexMask &node_mask,
                                                             const StringRef name,
                                                             const ScratchOffsets &scratch_offsets,
                                                             const gpu::VertBufPtr &scratch_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
  const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
  const bke::AttributeAccessor attributes = orig_mesh_data.attributes;
  const GVArraySpan attr = *attributes.lookup(name);

  node_mask.foreach_index(
      [&](const int node_index, const int scratch_index) {
        bke::attribute_math::to_static_type(attr.type(), [&]<typename T>() {
          if constexpr (!std::is_void_v<typename AttributeConverter<T>::VBOType>) {
            const int64_t dst_len = vbo.acquired[node_index] ? vbo.sizes[node_index] : 0;
            MutableSpan<typename AttributeConverter<T>::VBOType> dst =
                scratch_vbo->data<typename AttributeConverter<T>::VBOType>().slice(
                    scratch_offsets.offsets[scratch_index], dst_len);
            if (use_flat_layout[node_index]) {
              interpolate_corner_data_for_flat_layout<T>(
                  subdiv_ccg, nodes[node_index].grids(), attr.typed<T>(), dst);
            }
            else {
              interpolate_corner_data_for_standard_layout<T>(
                  subdiv_ccg, nodes[node_index].grids(), attr.typed<T>(), dst);
            }
          }
        });
      },
      exec_mode::grain_size(1));
}

template<typename T>
static void fill_face_data_grids(const SubdivCCG &subdiv_ccg,
                                 const bool use_flat_layout,
                                 const Span<int> grids,
                                 const Span<T> src,
                                 MutableSpan<typename AttributeConverter<T>::VBOType> vbo)
{
  using Converter = AttributeConverter<T>;
  using VBOType = typename Converter::VBOType;

  const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
  const int verts_per_grid = use_flat_layout ? (key.grid_size - 1) * (key.grid_size - 1) * 4 :
                                               key.grid_area;
  VBOType *dst = vbo.data();
  for (const int i : grids.index_range()) {
    const int face_index = subdiv_ccg.grid_to_face_map[grids[i]];
    std::fill_n(dst, verts_per_grid, Converter::convert(src[face_index]));
    dst += verts_per_grid;
  }
}

BLI_NOINLINE static void fill_generic_face_attribute_grids(const Object &object,
                                                           const OrigMeshData &orig_mesh_data,
                                                           const Buffer &vbo,
                                                           const BitSpan use_flat_layout,
                                                           const IndexMask &node_mask,
                                                           const StringRef name,
                                                           const ScratchOffsets &scratch_offsets,
                                                           const gpu::VertBufPtr &scratch_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
  const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
  const bke::AttributeAccessor attributes = orig_mesh_data.attributes;
  const GVArraySpan attr = *attributes.lookup(name);

  node_mask.foreach_index(
      [&](const int node_index, const int scratch_index) {
        bke::attribute_math::to_static_type(attr.type(), [&]<typename T>() {
          if constexpr (!std::is_void_v<typename AttributeConverter<T>::VBOType>) {
            const int64_t dst_len = vbo.acquired[node_index] ? vbo.sizes[node_index] : 0;
            MutableSpan<typename AttributeConverter<T>::VBOType> dst =
                scratch_vbo->data<typename AttributeConverter<T>::VBOType>().slice(
                    scratch_offsets.offsets[scratch_index], dst_len);
            fill_face_data_grids<T>(subdiv_ccg,
                                    use_flat_layout[node_index],
                                    nodes[node_index].grids(),
                                    attr.typed<T>(),
                                    dst);
          }
        });
      },
      exec_mode::grain_size(1));
}

/** Phantom: Multires polypaint, #SubdivCCG::colors in the buffer format of a color attribute. */
template<typename T>
static typename AttributeConverter<T>::VBOType grid_color_to_vbo(const ColorGeometry4b &color);
template<> ushort4 grid_color_to_vbo<ColorGeometry4b>(const ColorGeometry4b &color)
{
  return AttributeConverter<ColorGeometry4b>::convert(color);
}
template<> ColorGeometry4f grid_color_to_vbo<ColorGeometry4f>(const ColorGeometry4b &color)
{
  float3 rgb = {BLI_color_from_srgb_table[color.r],
                BLI_color_from_srgb_table[color.g],
                BLI_color_from_srgb_table[color.b]};
  IMB_colormanagement_rec709_to_scene_linear(rgb, rgb);
  return ColorGeometry4f(rgb.x, rgb.y, rgb.z, color.a * (1.0f / 255.0f));
}

template<typename T>
static void fill_grid_colors(const SubdivCCG &subdiv_ccg,
                             const Span<int> grids,
                             const bool use_flat_layout,
                             MutableSpan<typename AttributeConverter<T>::VBOType> dst)
{
  const int grid_size = subdiv_ccg.grid_size;
  const int grid_area = subdiv_ccg.grid_area;
  const Span<ColorGeometry4b> colors = subdiv_ccg.colors;
  int64_t i = 0;
  if (use_flat_layout) {
    const int grid_size_1 = grid_size - 1;
    for (const int grid : grids) {
      const Span<ColorGeometry4b> grid_colors = colors.slice(int64_t(grid) * grid_area,
                                                             grid_area);
      for (int y = 0; y < grid_size_1; y++) {
        for (int x = 0; x < grid_size_1; x++) {
          dst[i++] = grid_color_to_vbo<T>(grid_colors[CCG_grid_xy_to_index(grid_size, x, y)]);
          dst[i++] = grid_color_to_vbo<T>(
              grid_colors[CCG_grid_xy_to_index(grid_size, x + 1, y)]);
          dst[i++] = grid_color_to_vbo<T>(
              grid_colors[CCG_grid_xy_to_index(grid_size, x + 1, y + 1)]);
          dst[i++] = grid_color_to_vbo<T>(
              grid_colors[CCG_grid_xy_to_index(grid_size, x, y + 1)]);
        }
      }
    }
  }
  else {
    for (const int grid : grids) {
      for (const ColorGeometry4b &color : colors.slice(int64_t(grid) * grid_area, grid_area)) {
        dst[i++] = grid_color_to_vbo<T>(color);
      }
    }
  }
  BLI_assert(i == dst.size());
}

BLI_NOINLINE static void update_generic_attribute_grids(const Object &object,
                                                        const OrigMeshData &orig_mesh_data,
                                                        const Buffer &vbo,
                                                        const int vert_len,
                                                        const StringRef name,
                                                        const BitSpan use_flat_layout,
                                                        const IndexMask &node_mask,
                                                        const ScratchOffsets &scratch_offsets,
                                                        DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::AttributeAccessor attributes = orig_mesh_data.attributes;
  const bke::GAttributeReader attr = attributes.lookup(name);
  if (attr.domain == bke::AttrDomain::Edge) {
    return;
  }

  const bke::AttrType data_type = (!attr) ? bke::AttrType::Float3 :
                                            bke::cpp_type_to_attribute_type(attr.varray.type());
  const StringRef format_name = (!attr) ? "Dummy" : name;
  const GPUVertFormat format = attribute_format(orig_mesh_data, format_name, data_type);
  const bool is_uvs = attr && attr.domain == bke::AttrDomain::Corner &&
                      data_type == bke::AttrType::Float2;
  /* Phantom: Multires polypaint replaces the base mesh's color attribute in Sculpt Mode. */
  const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
  const bool use_grid_colors = attr && !subdiv_ccg.colors.is_empty() &&
                               ELEM(attr.domain,
                                    bke::AttrDomain::Point,
                                    bke::AttrDomain::Corner) &&
                               ELEM(data_type,
                                    bke::AttrType::ColorFloat,
                                    bke::AttrType::ColorByte) &&
                               ELEM(name,
                                    orig_mesh_data.active_color,
                                    orig_mesh_data.default_color);
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(format, scratch_offsets.total);

  if (use_grid_colors) {
    const Span<bke::pbvh::GridsNode> nodes =
        bke::object::pbvh_get(object)->nodes<bke::pbvh::GridsNode>();
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          const int64_t dst_len = vbo.acquired[node_index] ? vbo.sizes[node_index] : 0;
          if (dst_len == 0) {
            return;
          }
          const Span<int> grids = nodes[node_index].grids();
          if (data_type == bke::AttrType::ColorByte) {
            fill_grid_colors<ColorGeometry4b>(
                subdiv_ccg,
                grids,
                use_flat_layout[node_index],
                scratch_vbo->data<ushort4>().slice(scratch_offsets.offsets[scratch_index],
                                                   dst_len));
          }
          else {
            fill_grid_colors<ColorGeometry4f>(
                subdiv_ccg,
                grids,
                use_flat_layout[node_index],
                scratch_vbo->data<ColorGeometry4f>().slice(scratch_offsets.offsets[scratch_index],
                                                           dst_len));
          }
        },
        exec_mode::grain_size(1));
  }
  else if (!attr) {
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          const int64_t dst_len = vbo.acquired[node_index] ? vbo.sizes[node_index] : 0;
          scratch_vbo->data<float3>()
              .slice(scratch_offsets.offsets[scratch_index], dst_len)
              .fill(float3(0.0f));
        },
        exec_mode::grain_size(1));
  }
  else if (is_uvs) {
    fill_uvs_grids(object,
                   orig_mesh_data,
                   vbo,
                   use_flat_layout,
                   node_mask,
                   name,
                   scratch_offsets,
                   scratch_vbo);
  }
  else {
    switch (attr.domain) {
      case bke::AttrDomain::Point:
        calc_generic_vert_attribute_grids(object,
                                          orig_mesh_data,
                                          vbo,
                                          use_flat_layout,
                                          node_mask,
                                          name,
                                          scratch_offsets,
                                          scratch_vbo);
        break;
      case bke::AttrDomain::Corner:
        calc_generic_corner_attribute_grids(object,
                                            orig_mesh_data,
                                            vbo,
                                            use_flat_layout,
                                            node_mask,
                                            name,
                                            scratch_offsets,
                                            scratch_vbo);
        break;
      case bke::AttrDomain::Face:
        fill_generic_face_attribute_grids(object,
                                          orig_mesh_data,
                                          vbo,
                                          use_flat_layout,
                                          node_mask,
                                          name,
                                          scratch_offsets,
                                          scratch_vbo);
        break;
      default:
        BLI_assert_unreachable();
    }
  }
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

BLI_NOINLINE static void update_positions_bmesh(const Object &object,
                                                const Buffer &vbo,
                                                const int vert_len,
                                                const IndexMask &node_mask,
                                                const ScratchOffsets &scratch_offsets,
                                                DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(position_format(),
                                                          scratch_offsets.total);
  node_mask.foreach_index(
      [&](const int node_index, const int scratch_index) {
        float3 *data = scratch_vbo->data<float3>().data() + scratch_offsets.offsets[scratch_index];
        float3 *data_start = data;
        for (const BMFace *face :
             BKE_pbvh_bmesh_node_faces(&const_cast<bke::pbvh::BMeshNode &>(nodes[node_index])))
        {
          if (BM_elem_flag_test(face, BM_ELEM_HIDDEN)) {
            continue;
          }
          const BMLoop *l = face->l_first;
          *data = l->prev->v->co;
          data++;
          *data = l->v->co;
          data++;
          *data = l->next->v->co;
          data++;
        }
        UNUSED_VARS_NDEBUG(data_start);
        BLI_assert(data == data_start + vbo.sizes[node_index]);
      },
      exec_mode::grain_size(1));
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

BLI_NOINLINE static void update_normals_bmesh(const Object &object,
                                              const Buffer &vbo,
                                              const int vert_len,
                                              const IndexMask &node_mask,
                                              const ScratchOffsets &scratch_offsets,
                                              DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(normal_format(), scratch_offsets.total);
  node_mask.foreach_index(
      [&](const int node_index, const int scratch_index) {
        PackedNormal *data = scratch_vbo->data<PackedNormal>().data() +
                             scratch_offsets.offsets[scratch_index];
        PackedNormal *data_start = data;
        for (const BMFace *face :
             BKE_pbvh_bmesh_node_faces(&const_cast<bke::pbvh::BMeshNode &>(nodes[node_index])))
        {
          if (BM_elem_flag_test(face, BM_ELEM_HIDDEN)) {
            continue;
          }
          if (BM_elem_flag_test(face, BM_ELEM_SMOOTH)) {
            const BMLoop *l = face->l_first;
            *data = normal_to_gpu(l->prev->v->no);
            data++;
            *data = normal_to_gpu(l->v->no);
            data++;
            *data = normal_to_gpu(l->next->v->no);
            data++;
          }
          else {
            std::fill_n(data, 3, normal_to_gpu(face->no));
            data += 3;
          }
        }
        UNUSED_VARS_NDEBUG(data_start);
        BLI_assert(data == data_start + vbo.sizes[node_index]);
      },
      exec_mode::grain_size(1));
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

BLI_NOINLINE static void update_masks_bmesh(const Object &object,
                                            const Buffer &vbo,
                                            const int vert_len,
                                            const IndexMask &node_mask,
                                            const ScratchOffsets &scratch_offsets,
                                            DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
  const BMesh &bm = *object.runtime->sculpt_session->bm;
  const int cd_offset = CustomData_get_offset_named(&bm.vdata, CD_PROP_FLOAT, ".sculpt_mask");
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(mask_format(), scratch_offsets.total);
  if (cd_offset != -1) {
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          float *data = scratch_vbo->data<float>().data() + scratch_offsets.offsets[scratch_index];
          float *data_start = data;
          for (const BMFace *face :
               BKE_pbvh_bmesh_node_faces(&const_cast<bke::pbvh::BMeshNode &>(nodes[node_index])))
          {
            if (BM_elem_flag_test(face, BM_ELEM_HIDDEN)) {
              continue;
            }
            const BMLoop *l = face->l_first;
            *data = bmesh_cd_vert_get<float>(*l->prev->v, cd_offset);
            data++;
            *data = bmesh_cd_vert_get<float>(*l->v, cd_offset);
            data++;
            *data = bmesh_cd_vert_get<float>(*l->next->v, cd_offset);
            data++;
          }
          UNUSED_VARS_NDEBUG(data_start);
          BLI_assert(data == data_start + vbo.sizes[node_index]);
        },
        exec_mode::grain_size(1));
  }
  else {
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          const int64_t dst_len = vbo.acquired[node_index] ? vbo.sizes[node_index] : 0;
          scratch_vbo->data<float>()
              .slice(scratch_offsets.offsets[scratch_index], dst_len)
              .fill(0.0f);
        },
        exec_mode::grain_size(64));
  }
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

BLI_NOINLINE static void update_face_sets_bmesh(const Object &object,
                                                const OrigMeshData &orig_mesh_data,
                                                const Buffer &vbo,
                                                const int vert_len,
                                                const IndexMask &node_mask,
                                                const ScratchOffsets &scratch_offsets,
                                                DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
  const BMesh &bm = *object.runtime->sculpt_session->bm;
  const int color_default = orig_mesh_data.face_set_default;
  const int color_seed = orig_mesh_data.face_set_seed;
  const int offset = CustomData_get_offset_named(&bm.pdata, CD_PROP_INT32, ".sculpt_face_set");
  if (node_mask.is_empty()) {
    return;
  }
  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(face_set_format(),
                                                          scratch_offsets.total);
  if (offset != -1) {
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          uchar4 *data = scratch_vbo->data<uchar4>().data() +
                         scratch_offsets.offsets[scratch_index];
          uchar4 *data_start = data;
          for (const BMFace *face :
               BKE_pbvh_bmesh_node_faces(&const_cast<bke::pbvh::BMeshNode &>(nodes[node_index])))
          {
            if (BM_elem_flag_test(face, BM_ELEM_HIDDEN)) {
              continue;
            }
            uchar4 color;
            const int fset = bmesh_cd_face_get<int>(*face, offset);
            BKE_paint_face_set_color_get(fset, color_default, color_seed, color);
            std::fill_n(data, 3, color);
            data += 3;
          }
          UNUSED_VARS_NDEBUG(data_start);
          BLI_assert(data == data_start + vbo.sizes[node_index]);
        },
        exec_mode::grain_size(1));
  }
  else {
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          const int64_t dst_len = vbo.acquired[node_index] ? vbo.sizes[node_index] : 0;
          uchar4 color;
          BKE_paint_face_set_color_get(1, color_default, color_seed, color);
          scratch_vbo->data<uchar4>()
              .slice(scratch_offsets.offsets[scratch_index], dst_len)
              .fill(color);
        },
        exec_mode::grain_size(64));
  }
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

BLI_NOINLINE static void update_generic_attribute_bmesh(const Object &object,
                                                        const OrigMeshData &orig_mesh_data,
                                                        const Buffer &vbo,
                                                        const int vert_len,
                                                        const StringRef name,
                                                        const IndexMask &node_mask,
                                                        const ScratchOffsets &scratch_offsets,
                                                        DrawCacheImpl::AttributeData &attr_vbo)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
  const BMesh &bm = *object.runtime->sculpt_session->bm;
  const BMDataLayerLookup attr = BM_data_layer_lookup(bm, name);
  if (attr.domain == bke::AttrDomain::Edge) {
    return;
  }

  if (node_mask.is_empty()) {
    return;
  }

  const bke::AttrType attr_type = attr ? attr.type : bke::AttrType::Float3;
  const GPUVertFormat format = attribute_format(orig_mesh_data, name, attr_type);
  if (!attr) {
    gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(format, scratch_offsets.total);
    node_mask.foreach_index(
        [&](const int node_index, const int scratch_index) {
          const int64_t dst_len = vbo.acquired[node_index] ? vbo.sizes[node_index] : 0;
          scratch_vbo->data<float3>()
              .slice(scratch_offsets.offsets[scratch_index], dst_len)
              .fill(float3(0.0f));
        },
        exec_mode::grain_size(1));
    finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
    return;
  }

  gpu::VertBufPtr scratch_vbo = create_vbo_scratch_buffer(format, scratch_offsets.total);
  node_mask.foreach_index(
      [&](const int node_index, const int scratch_index) {
        const IndexRange scratch_range = vbo.scratch_range(node_index,
                                                           scratch_offsets.offsets[scratch_index]);
        bke::attribute_math::to_static_type(attr.type, [&]<typename T>() {
          const auto &faces = BKE_pbvh_bmesh_node_faces(
              &const_cast<bke::pbvh::BMeshNode &>(nodes[node_index]));
          if constexpr (!std::is_void_v<typename AttributeConverter<T>::VBOType>) {
            switch (attr.domain) {
              case bke::AttrDomain::Point:
                extract_data_vert_bmesh<T>(faces, attr.offset, *scratch_vbo, scratch_range);
                break;
              case bke::AttrDomain::Face:
                extract_data_face_bmesh<T>(faces, attr.offset, *scratch_vbo, scratch_range);
                break;
              case bke::AttrDomain::Corner:
                extract_data_corner_bmesh<T>(faces, attr.offset, *scratch_vbo, scratch_range);
                break;
              default:
                BLI_assert_unreachable();
            }
          }
        });
      },
      exec_mode::grain_size(1));
  finalize_vbo_update(attr_vbo, vbo, vert_len, node_mask, scratch_offsets, scratch_vbo);
}

static void create_lines_index_faces(const OffsetIndices<int> faces,
                                     const Span<int> corner_verts,
                                     const Span<bool> hide_poly,
                                     const bke::pbvh::MeshNode &node,
                                     const bool use_flat_layout,
                                     const int64_t vbo_offset,
                                     const int64_t ibo_len,
                                     uint32_t *host_data)
{
  const Span<int> face_indices = node.faces();
  MutableSpan<uint2> data = MutableSpan<uint32_t>(host_data, ibo_len).cast<uint2>();

  if (!use_flat_layout) {
    /* The node stores each vertex once: index the vertices of each face corner. */
    int line_index = 0;
    for (const int face_index : face_indices) {
      if (!hide_poly.is_empty() && hide_poly[face_index]) {
        continue;
      }
      const Span<int> face_verts = corner_verts.slice(faces[face_index]);
      const uint first = uint(node.vert_indices_.index_of(face_verts.first()));
      uint prev = first;
      for (const int i : face_verts.index_range().drop_front(1)) {
        const uint vert = uint(node.vert_indices_.index_of(face_verts[i]));
        data[line_index] = uint2(prev, vert) + uint(vbo_offset);
        line_index++;
        prev = vert;
      }
      data[line_index] = uint2(prev, first) + uint(vbo_offset);
      line_index++;
    }
    return;
  }

  int node_corner_offset = 0;
  int line_index = 0;
  for (const int face_index : face_indices) {
    const int face_size = faces[face_index].size();
    if (!hide_poly.is_empty() && hide_poly[face_index]) {
      node_corner_offset += face_size;
      continue;
    }
    for (const int i : IndexRange(face_size)) {
      const int next = (i == face_size - 1) ? 0 : i + 1;
      data[line_index] = uint2(i, next) + (node_corner_offset + int(vbo_offset));
      line_index++;
    }

    node_corner_offset += face_size;
  }
}

/* NOTE: This is a temporary solution. The index buffers are only needed to offset the vertices
 * into the shared (per-object) vertex buffers. Once multi draw indirect is used (each draw can
 * specify its own base vertex) these index buffers can be removed. */
static void create_tri_index_bmesh(const Set<BMFace *, 0> &faces,
                                   const int64_t vbo_offset,
                                   const int64_t ibo_len,
                                   uint32_t *host_data)
{
  MutableSpan<uint3> data = MutableSpan<uint32_t>(host_data, ibo_len).cast<uint3>();

  int tri_index = 0;
  int vert_index = vbo_offset;

  for (const BMFace *face : faces) {
    if (BM_elem_flag_test(face, BM_ELEM_HIDDEN)) {
      continue;
    }

    data[tri_index] = uint3(vert_index, vert_index + 1, vert_index + 2);
    tri_index++;

    vert_index += 3;
  }
}

/* NOTE: Temporary solution, see the note on #create_tri_index_bmesh. */
static void create_lines_index_bmesh(const Set<BMFace *, 0> &faces,
                                     const int64_t vbo_offset,
                                     const int64_t ibo_len,
                                     uint32_t *host_data)
{
  MutableSpan<uint2> data = MutableSpan<uint32_t>(host_data, ibo_len).cast<uint2>();

  int line_index = 0;
  int vert_index = vbo_offset;

  for (const BMFace *face : faces) {
    if (BM_elem_flag_test(face, BM_ELEM_HIDDEN)) {
      continue;
    }

    data[line_index] = uint2(vert_index, vert_index + 1);
    line_index++;
    data[line_index] = uint2(vert_index + 1, vert_index + 2);
    line_index++;
    data[line_index] = uint2(vert_index + 2, vert_index);
    line_index++;

    vert_index += 3;
  }
}

static int create_tri_index_grids(const Span<int> grid_indices,
                                  const BitGroupVector<> &grid_hidden,
                                  const int gridsize,
                                  const int skip,
                                  const int totgrid,
                                  const int64_t vbo_offset,
                                  MutableSpan<uint3> data)
{
  int tri_index = 0;
  int offset = vbo_offset;
  const int grid_vert_len = gridsize * gridsize;
  for (int i = 0; i < totgrid; i++, offset += grid_vert_len) {
    uint v0, v1, v2, v3;

    const BoundedBitSpan gh = grid_hidden.is_empty() ? BoundedBitSpan() :
                                                       grid_hidden[grid_indices[i]];

    for (int y = 0; y < gridsize - skip; y += skip) {
      for (int x = 0; x < gridsize - skip; x += skip) {
        /* Skip hidden grid face */
        if (!gh.is_empty() && BKE_paint_is_grid_face_hidden(gh, gridsize, x, y)) {
          continue;
        }
        /* Indices in a Clockwise QUAD disposition. */
        v0 = offset + CCG_grid_xy_to_index(gridsize, x, y);
        v1 = offset + CCG_grid_xy_to_index(gridsize, x + skip, y);
        v2 = offset + CCG_grid_xy_to_index(gridsize, x + skip, y + skip);
        v3 = offset + CCG_grid_xy_to_index(gridsize, x, y + skip);

        data[tri_index] = uint3(v0, v2, v1);
        tri_index++;
        data[tri_index] = uint3(v0, v3, v2);
        tri_index++;
      }
    }
  }

  return tri_index;
}

static int create_tri_index_grids_flat_layout(const Span<int> grid_indices,
                                              const BitGroupVector<> &grid_hidden,
                                              const int gridsize,
                                              const int skip,
                                              const int totgrid,
                                              const int64_t vbo_offset,
                                              MutableSpan<uint3> data)
{
  int tri_index = 0;
  int offset = vbo_offset;
  const int grid_vert_len = square_uint(gridsize - 1) * 4;
  for (int i = 0; i < totgrid; i++, offset += grid_vert_len) {
    const BoundedBitSpan gh = grid_hidden.is_empty() ? BoundedBitSpan() :
                                                       grid_hidden[grid_indices[i]];

    uint v0, v1, v2, v3;
    for (int y = 0; y < gridsize - skip; y += skip) {
      for (int x = 0; x < gridsize - skip; x += skip) {
        /* Skip hidden grid face */
        if (!gh.is_empty() && BKE_paint_is_grid_face_hidden(gh, gridsize, x, y)) {
          continue;
        }

        v0 = (y * (gridsize - 1) + x) * 4;

        if (skip > 1) {
          v1 = (y * (gridsize - 1) + x + skip - 1) * 4;
          v2 = ((y + skip - 1) * (gridsize - 1) + x + skip - 1) * 4;
          v3 = ((y + skip - 1) * (gridsize - 1) + x) * 4;
        }
        else {
          v1 = v2 = v3 = v0;
        }

        /* VBO data are in a Clockwise QUAD disposition.  Note
         * that vertices might be in different quads if we're
         * building a coarse index buffer.
         */
        v0 += offset;
        v1 += offset + 1;
        v2 += offset + 2;
        v3 += offset + 3;

        data[tri_index] = uint3(v0, v2, v1);
        tri_index++;
        data[tri_index] = uint3(v0, v3, v2);
        tri_index++;
      }
    }
  }
  return tri_index;
}

static void create_lines_index_grids(const Span<int> grid_indices,
                                     int display_gridsize,
                                     const BitGroupVector<> &grid_hidden,
                                     const int gridsize,
                                     const int skip,
                                     const int totgrid,
                                     const int64_t vbo_offset,
                                     MutableSpan<uint2> data)
{
  int line_index = 0;
  int offset = vbo_offset;
  const int grid_vert_len = gridsize * gridsize;
  for (int i = 0; i < totgrid; i++, offset += grid_vert_len) {
    uint v0, v1, v2, v3;
    bool grid_visible = false;

    const BoundedBitSpan gh = grid_hidden.is_empty() ? BoundedBitSpan() :
                                                       grid_hidden[grid_indices[i]];

    for (int y = 0; y < gridsize - skip; y += skip) {
      for (int x = 0; x < gridsize - skip; x += skip) {
        /* Skip hidden grid face */
        if (!gh.is_empty() && BKE_paint_is_grid_face_hidden(gh, gridsize, x, y)) {
          continue;
        }
        /* Indices in a Clockwise QUAD disposition. */
        v0 = offset + CCG_grid_xy_to_index(gridsize, x, y);
        v1 = offset + CCG_grid_xy_to_index(gridsize, x + skip, y);
        v2 = offset + CCG_grid_xy_to_index(gridsize, x + skip, y + skip);
        v3 = offset + CCG_grid_xy_to_index(gridsize, x, y + skip);

        data[line_index] = uint2(v0, v1);
        line_index++;
        data[line_index] = uint2(v0, v3);
        line_index++;

        if (y / skip + 2 == display_gridsize) {
          data[line_index] = uint2(v2, v3);
          line_index++;
        }
        grid_visible = true;
      }

      if (grid_visible) {
        data[line_index] = uint2(v1, v2);
        line_index++;
      }
    }
  }
}

static void create_lines_index_grids_flat_layout(const Span<int> grid_indices,
                                                 int display_gridsize,
                                                 const BitGroupVector<> &grid_hidden,
                                                 const int gridsize,
                                                 const int skip,
                                                 const int totgrid,
                                                 const int64_t vbo_offset,
                                                 MutableSpan<uint2> data)
{
  int line_index = 0;
  int offset = vbo_offset;
  const int grid_vert_len = square_uint(gridsize - 1) * 4;
  for (int i = 0; i < totgrid; i++, offset += grid_vert_len) {
    bool grid_visible = false;
    const BoundedBitSpan gh = grid_hidden.is_empty() ? BoundedBitSpan() :
                                                       grid_hidden[grid_indices[i]];

    uint v0, v1, v2, v3;
    for (int y = 0; y < gridsize - skip; y += skip) {
      for (int x = 0; x < gridsize - skip; x += skip) {
        /* Skip hidden grid face */
        if (!gh.is_empty() && BKE_paint_is_grid_face_hidden(gh, gridsize, x, y)) {
          continue;
        }

        v0 = (y * (gridsize - 1) + x) * 4;

        if (skip > 1) {
          v1 = (y * (gridsize - 1) + x + skip - 1) * 4;
          v2 = ((y + skip - 1) * (gridsize - 1) + x + skip - 1) * 4;
          v3 = ((y + skip - 1) * (gridsize - 1) + x) * 4;
        }
        else {
          v1 = v2 = v3 = v0;
        }

        /* VBO data are in a Clockwise QUAD disposition.  Note
         * that vertices might be in different quads if we're
         * building a coarse index buffer.
         */
        v0 += offset;
        v1 += offset + 1;
        v2 += offset + 2;
        v3 += offset + 3;

        data[line_index] = uint2(v0, v1);
        line_index++;
        data[line_index] = uint2(v0, v3);
        line_index++;

        if (y / skip + 2 == display_gridsize) {
          data[line_index] = uint2(v2, v3);
          line_index++;
        }
        grid_visible = true;
      }

      if (grid_visible) {
        data[line_index] = uint2(v1, v2);
        line_index++;
      }
    }
  }
}

static Array<int> calc_material_indices(const Object &object, const OrigMeshData &orig_mesh_data)
{
  const SculptSession &ss = *object.runtime->sculpt_session;
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  switch (pbvh.type()) {
    case bke::pbvh::Type::Mesh: {
      const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
      const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
      const bke::AttributeAccessor attributes = mesh.attributes();
      const VArray material_indices = *attributes.lookup<int>("material_index",
                                                              bke::AttrDomain::Face);
      if (!material_indices) {
        return {};
      }
      Array<int> node_materials(nodes.size());
      threading::parallel_for(nodes.index_range(), 64, [&](const IndexRange range) {
        for (const int i : range) {
          const Span<int> face_indices = nodes[i].faces();
          if (face_indices.is_empty()) {
            continue;
          }
          node_materials[i] = material_indices[face_indices.first()];
        }
      });
      return node_materials;
    }
    case bke::pbvh::Type::Grids: {
      const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
      /* Use original mesh data because evaluated mesh is empty. */
      const bke::AttributeAccessor attributes = orig_mesh_data.attributes;
      const VArray material_indices = *attributes.lookup<int>("material_index",
                                                              bke::AttrDomain::Face);
      if (!material_indices) {
        return {};
      }
      Array<int> node_materials(nodes.size());
      const SubdivCCG &subdiv_ccg = *ss.subdiv_ccg;
      const Span<int> grid_faces = subdiv_ccg.grid_to_face_map;
      threading::parallel_for(nodes.index_range(), 64, [&](const IndexRange range) {
        for (const int i : range) {
          const Span<int> grids = nodes[i].grids();
          if (grids.is_empty()) {
            continue;
          }
          node_materials[i] = material_indices[grid_faces[grids.first()]];
        }
      });
      return node_materials;
    }
    case bke::pbvh::Type::BMesh:
      return {};
  }
  BLI_assert_unreachable();
  return {};
}

/**
 * Whether a request has face corner or face attributes, which mesh nodes can only store with the
 * flat layout.
 */
static bool request_needs_flat_layout(const OrigMeshData &orig_mesh_data,
                                      const ViewportRequest &request)
{
  for (const AttributeRequest &attr : request.attributes) {
    if (const GenericRequest *name = std::get_if<GenericRequest>(&attr)) {
      const std::optional<bke::AttributeMetaData> meta_data =
          orig_mesh_data.attributes.lookup_meta_data(*name);
      if (meta_data && ELEM(meta_data->domain, bke::AttrDomain::Corner, bke::AttrDomain::Face)) {
        return true;
      }
    }
  }
  return false;
}

static bool request_has_normals(const ViewportRequest &request)
{
  return request.attributes.contains(CustomRequest::Normal);
}

/**
 * Decides which nodes store their vertex data per face corner (see #use_flat_layout_). Data
 * stored once per vertex can't hold what differs between the faces around a vertex: the normals
 * of sharp faces (only while something draws with normals: Phantom's sculpt shading works them out
 * from positions), the colors of different face sets on meshes, and face corner or face attributes
 * of meshes (#request_needs_flat_layout).
 */
class FlatLayoutTest {
  const bke::pbvh::Tree &pbvh_;
  const bool mesh_force_flat_;
  const VArraySpan<bool> sharp_faces_;
  const VArraySpan<int> face_sets_;
  Span<int> grid_to_face_map_;

 public:
  FlatLayoutTest(const Object &object,
                 const OrigMeshData &orig_mesh_data,
                 const bool mesh_force_flat,
                 const bool use_sharp_faces)
      : pbvh_(*bke::object::pbvh_get(object)),
        mesh_force_flat_(mesh_force_flat),
        sharp_faces_(use_sharp_faces ?
                         *orig_mesh_data.attributes.lookup<bool>("sharp_face",
                                                                 bke::AttrDomain::Face) :
                         VArray<bool>()),
        face_sets_(pbvh_.type() == bke::pbvh::Type::Mesh ?
                       *orig_mesh_data.attributes.lookup<int>(".sculpt_face_set",
                                                              bke::AttrDomain::Face) :
                       VArray<int>())
  {
    if (pbvh_.type() == bke::pbvh::Type::Grids) {
      grid_to_face_map_ = object.runtime->sculpt_session->subdiv_ccg->grid_to_face_map;
    }
  }

  bool operator()(const int node_index) const
  {
    switch (pbvh_.type()) {
      case bke::pbvh::Type::Mesh: {
        if (mesh_force_flat_) {
          return true;
        }
        const Span<int> faces = pbvh_.nodes<bke::pbvh::MeshNode>()[node_index].faces();
        if (faces.is_empty()) {
          return false;
        }
        if (!sharp_faces_.is_empty() && std::any_of(faces.begin(), faces.end(), [&](const int f) {
              return sharp_faces_[f];
            }))
        {
          return true;
        }
        if (!face_sets_.is_empty()) {
          const int face_set = face_sets_[faces.first()];
          return std::any_of(
              faces.begin(), faces.end(), [&](const int f) { return face_sets_[f] != face_set; });
        }
        return false;
      }
      case bke::pbvh::Type::Grids: {
        if (sharp_faces_.is_empty()) {
          return false;
        }
        const Span<int> grids = pbvh_.nodes<bke::pbvh::GridsNode>()[node_index].grids();
        return std::any_of(grids.begin(), grids.end(), [&](const int grid) {
          return sharp_faces_[grid_to_face_map_[grid]];
        });
      }
      case bke::pbvh::Type::BMesh:
        return false;
    }
    BLI_assert_unreachable();
    return false;
  }
};

static BitVector<> calc_use_flat_layout(const Object &object,
                                        const OrigMeshData &orig_mesh_data,
                                        const bool mesh_force_flat,
                                        const bool use_sharp_faces)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  if (pbvh.type() == bke::pbvh::Type::BMesh) {
    return {};
  }
  const FlatLayoutTest test(object, orig_mesh_data, mesh_force_flat, use_sharp_faces);
  /* Use boolean array instead of #BitVector for parallelized writing. */
  Array<bool> use_flat_layout(pbvh.nodes_num(), false);
  threading::parallel_for(IndexRange(pbvh.nodes_num()), 64, [&](const IndexRange range) {
    for (const int i : range) {
      use_flat_layout[i] = test(i);
    }
  });
  return BitVector<>(use_flat_layout);
}

static void create_tri_index_mesh(const OffsetIndices<int> faces,
                                  const Span<int> corner_verts,
                                  const Span<int3> corner_tris,
                                  const Span<bool> hide_poly,
                                  const bke::pbvh::MeshNode &node,
                                  const bool use_flat_layout,
                                  const int64_t vbo_offset,
                                  const int64_t ibo_len,
                                  uint32_t *host_data)
{
  const Span<int> face_indices = node.faces();

  MutableSpan<uint3> data = MutableSpan<uint32_t>(host_data, ibo_len).cast<uint3>();

  if (!use_flat_layout) {
    /* The node stores each vertex once: index the vertex of each triangle corner. */
    int tri_index = 0;
    for (const int face_index : face_indices) {
      if (!hide_poly.is_empty() && hide_poly[face_index]) {
        continue;
      }
      for (const int3 &tri :
           corner_tris.slice(bke::mesh::face_triangles_range(faces, face_index)))
      {
        for (int i : IndexRange(3)) {
          data[tri_index][i] = uint(node.vert_indices_.index_of(corner_verts[tri[i]]) +
                                    vbo_offset);
        }
        tri_index++;
      }
    }
    return;
  }

  int tri_index = 0;
  int node_corner_offset = 0;
  for (const int face_index : face_indices) {
    const IndexRange face = faces[face_index];
    if (!hide_poly.is_empty() && hide_poly[face_index]) {
      node_corner_offset += face.size();
      continue;
    }
    for (const int3 &tri : corner_tris.slice(bke::mesh::face_triangles_range(faces, face_index))) {
      for (int i : IndexRange(3)) {
        const int corner = tri[i];
        const int index_in_face = corner - face.first();
        data[tri_index][i] = node_corner_offset + index_in_face + int(vbo_offset);
      }
      tri_index++;
    }
    node_corner_offset += face.size();
  }
}

static void create_tri_index_grids(const CCGKey &key,
                                   const BitGroupVector<> &grid_hidden,
                                   const bool do_coarse,
                                   const Span<int> grid_indices,
                                   const bool use_flat_layout,
                                   const int64_t vbo_offset,
                                   const int64_t ibo_len,
                                   uint32_t *host_data)
{
  int gridsize = key.grid_size;
  int totgrid = grid_indices.size();
  const int skip = do_coarse ? grid_coarse_skip(key) : 1;

  uint visible_quad_len = count_drawn_grid_quads(grid_hidden, grid_indices, key.grid_size, skip);
  UNUSED_VARS_NDEBUG(visible_quad_len);
  BLI_assert(uint(ibo_len) == 2 * visible_quad_len * 3);

  MutableSpan<uint3> data = MutableSpan<uint32_t>(host_data, ibo_len).cast<uint3>();
  data.fill(uint3(0));

  int tri_count;
  if (use_flat_layout) {
    tri_count = create_tri_index_grids_flat_layout(
        grid_indices, grid_hidden, gridsize, skip, totgrid, vbo_offset, data);
  }
  else {
    tri_count = create_tri_index_grids(
        grid_indices, grid_hidden, gridsize, skip, totgrid, vbo_offset, data);
  }
  BLI_assert(tri_count * 3 <= uint(ibo_len));
  UNUSED_VARS(tri_count);
}

static void create_lines_index_grids(const CCGKey &key,
                                     const BitGroupVector<> &grid_hidden,
                                     const bool do_coarse,
                                     const Span<int> grid_indices,
                                     const bool use_flat_layout,
                                     const int64_t vbo_offset,
                                     const int64_t ibo_len,
                                     uint32_t *host_data)
{
  int gridsize = key.grid_size;
  int display_gridsize = gridsize;
  int totgrid = grid_indices.size();
  int skip = 1;

  if (do_coarse) {
    skip = grid_coarse_skip(key);
    display_gridsize = (gridsize - 1) / skip + 1;
  }

  const int quads_per_axis = (gridsize - 1) / skip;
  UNUSED_VARS_NDEBUG(quads_per_axis);
  BLI_assert(uint(ibo_len) == 4 * totgrid * quads_per_axis * (quads_per_axis + 1));
  MutableSpan<uint2> data = MutableSpan<uint32_t>(host_data, ibo_len).cast<uint2>();
  /* The buffer might contain hidden elements which are not initialized but still accounted. We
   * don't count them to skip from allocation, so must fill that gaps by 0 to hide redundant edges.
   */
  data.fill(uint2(0));

  if (use_flat_layout) {
    create_lines_index_grids_flat_layout(
        grid_indices, display_gridsize, grid_hidden, gridsize, skip, totgrid, vbo_offset, data);
  }
  else {
    create_lines_index_grids(
        grid_indices, display_gridsize, grid_hidden, gridsize, skip, totgrid, vbo_offset, data);
  }
}

Span<gpu::IndexBufPtr> DrawCacheImpl::ensure_lines_indices(const Object &object,
                                                           const OrigMeshData &orig_mesh_data,
                                                           const IndexMask &node_mask,
                                                           const bool coarse)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  IBOFamily &ibo_family = coarse ? ibo_lines_coarse_ : ibo_lines_;
  ibo_family.ensure_capacity();
  const Buffer &ibo_buffer = ibo_family.buffer;
  Vector<gpu::IndexBufPtr> &ibos = ibo_family.ibos;
  ibos.resize(pbvh.nodes_num());

  /* Whenever a node's visible triangle count has changed the index buffers are freed, so we only
   * recalculate null IBOs here. A new mask is recalculated for more even task distribution between
   * threads. */
  IndexMaskMemory memory;
  const IndexMask nodes_to_calculate = IndexMask::from_predicate(
      node_mask, memory, [&](const int node_index) {
        return !ibos[node_index] && ibo_buffer.acquired[node_index];
      });

  /* Build scratch offsets for the dirty node family, and the scratch buffer to stage into. */
  const ScratchOffsets scratch_offsets = build_scratch_offsets(ibo_buffer, nodes_to_calculate);
  Array<uint32_t> scratch_buffer(uint32_t(scratch_offsets.total));

  switch (pbvh.type()) {
    case bke::pbvh::Type::Mesh: {
      const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
      const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
      const OffsetIndices<int> faces = mesh.faces();
      const Span<int> corner_verts = mesh.corner_verts();
      const bke::AttributeAccessor attributes = orig_mesh_data.attributes;
      const VArraySpan hide_poly = *attributes.lookup<bool>(".hide_poly", bke::AttrDomain::Face);
      nodes_to_calculate.foreach_index(
          [&](const int node_index, const int scratch_index) {
            if (coarse && this->has_mesh_proxy(node_index)) {
              /* Phantom: the edges of the node's coarse triangles (#build_mesh_node_proxy). */
              const Span<uint2> lines = mesh_proxy_lines_[node_index];
              MutableSpan<uint2> data = MutableSpan<uint32_t>(
                                            scratch_buffer.data() +
                                                scratch_offsets.offsets[scratch_index],
                                            ibo_buffer.sizes[node_index])
                                            .cast<uint2>();
              for (const int e : lines.index_range()) {
                data[e] = lines[e] + uint(vbo_.offsets[node_index]);
              }
              return;
            }
            create_lines_index_faces(faces,
                                     corner_verts,
                                     hide_poly,
                                     nodes[node_index],
                                     use_flat_layout_[node_index],
                                     vbo_.offsets[node_index],
                                     ibo_buffer.sizes[node_index],
                                     scratch_buffer.data() +
                                         scratch_offsets.offsets[scratch_index]);
          },
          exec_mode::grain_size(1));
      break;
    }
    case bke::pbvh::Type::Grids: {
      const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
      const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
      const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
      nodes_to_calculate.foreach_index(
          [&](const int node_index, const int scratch_index) {
            create_lines_index_grids(key,
                                     subdiv_ccg.grid_hidden,
                                     coarse,
                                     nodes[node_index].grids(),
                                     use_flat_layout_[node_index],
                                     vbo_.offsets[node_index],
                                     ibo_buffer.sizes[node_index],
                                     scratch_buffer.data() +
                                         scratch_offsets.offsets[scratch_index]);
          },
          exec_mode::grain_size(1));
      break;
    }
    case bke::pbvh::Type::BMesh: {
      const Span<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
      nodes_to_calculate.foreach_index(
          [&](const int node_index, const int scratch_index) {
            const Set<BMFace *, 0> &faces = BKE_pbvh_bmesh_node_faces(
                &const_cast<bke::pbvh::BMeshNode &>(nodes[node_index]));
            create_lines_index_bmesh(faces,
                                     vbo_.offsets[node_index],
                                     ibo_buffer.sizes[node_index],
                                     scratch_buffer.data() +
                                         scratch_offsets.offsets[scratch_index]);
          },
          exec_mode::grain_size(1));
      break;
    }
  }

  ibo_family.commit_subranges(nodes_to_calculate, scratch_offsets, scratch_buffer);
  return ibos;
}

BitSpan DrawCacheImpl::ensure_use_flat_layout(const Object &object,
                                              const OrigMeshData &orig_mesh_data)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  if (use_flat_layout_.size() != pbvh.nodes_num()) {
    use_flat_layout_ = calc_use_flat_layout(
        object, orig_mesh_data, mesh_force_flat_, layout_uses_sharp_faces_);
    layout_dirty_.clear_and_shrink();
  }
  else if (!layout_dirty_.is_empty()) {
    if (pbvh.type() != bke::pbvh::Type::BMesh) {
      this->update_layout(object, orig_mesh_data);
    }
    layout_dirty_.clear_and_shrink();
  }
  return use_flat_layout_;
}

void DrawCacheImpl::update_layout(const Object &object, const OrigMeshData &orig_mesh_data)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const FlatLayoutTest test(object, orig_mesh_data, mesh_force_flat_, layout_uses_sharp_faces_);
  IndexMaskMemory memory;
  const IndexMask dirty = IndexMask::from_bits(
      IndexRange(std::min<int64_t>(layout_dirty_.size(), pbvh.nodes_num())),
      layout_dirty_,
      memory);
  const IndexMask changed = IndexMask::from_predicate(
      dirty, memory, [&](const int i) { return test(i) != use_flat_layout_[i].test(); });
  if (changed.is_empty()) {
    return;
  }
  changed.foreach_index([&](const int i) { use_flat_layout_[i].set(!use_flat_layout_[i].test()); });
  this->tag_layout_changed(changed);
}

void DrawCacheImpl::tag_layout_changed(const IndexMask &node_mask)
{
  /* The index buffers and the sizes of the node ranges follow the layout. */
  this->tag_visibility_changed(node_mask);
  for (AttributeData &data : attribute_vbos_.values()) {
    data.tag_dirty(node_mask);
  }
}

DrawCacheImpl::AttributeData &DrawCacheImpl::ensure_attribute_data(
    const Object &object,
    const OrigMeshData &orig_mesh_data,
    const AttributeRequest &attr,
    const IndexMask &node_mask)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  AttributeData &attr_vbo = attribute_vbos_.lookup_or_add_default(attr);
  attr_vbo.ensure_nodes_capacity(node_mask.min_array_size());

  /* Compute mask that needs to be recomputed. */
  IndexMaskMemory memory;
  const IndexMask dirty_mask = IndexMask::from_bits(
      node_mask.slice_content(attr_vbo.dirty_nodes.index_range()), attr_vbo.dirty_nodes, memory);

  const ScratchOffsets scratch_offsets = (attr_vbo.vbo == nullptr) ?
                                             build_vbo_initial_offsets(vbo_, dirty_mask) :
                                             build_scratch_offsets(vbo_, dirty_mask);

  switch (pbvh.type()) {
    case bke::pbvh::Type::Mesh: {
      if (const CustomRequest *request_type = std::get_if<CustomRequest>(&attr)) {
        switch (*request_type) {
          case CustomRequest::Position:
            update_positions_mesh(object,
                                  vbo_,
                                  vbo_.space.size(),
                                  use_flat_layout_,
                                  dirty_mask,
                                  scratch_offsets,
                                  attr_vbo);
            break;
          case CustomRequest::Normal:
            update_normals_mesh(object,
                                vbo_,
                                vbo_.space.size(),
                                use_flat_layout_,
                                dirty_mask,
                                scratch_offsets,
                                attr_vbo);
            break;
          case CustomRequest::Mask:
            update_masks_mesh(object,
                              orig_mesh_data,
                              vbo_,
                              vbo_.space.size(),
                              use_flat_layout_,
                              dirty_mask,
                              scratch_offsets,
                              attr_vbo);
            break;
          case CustomRequest::FaceSet:
            update_face_sets_mesh(object,
                                  orig_mesh_data,
                                  vbo_,
                                  vbo_.space.size(),
                                  use_flat_layout_,
                                  dirty_mask,
                                  scratch_offsets,
                                  attr_vbo);
            break;
        }
      }
      else {
        update_generic_attribute_mesh(object,
                                      orig_mesh_data,
                                      vbo_,
                                      vbo_.space.size(),
                                      std::get<GenericRequest>(attr),
                                      use_flat_layout_,
                                      dirty_mask,
                                      scratch_offsets,
                                      attr_vbo);
      }
      break;
    }
    case bke::pbvh::Type::Grids: {
      if (const CustomRequest *request_type = std::get_if<CustomRequest>(&attr)) {
        switch (*request_type) {
          case CustomRequest::Position:
            update_positions_grids(object,
                                   vbo_,
                                   vbo_.space.size(),
                                   use_flat_layout_,
                                   dirty_mask,
                                   scratch_offsets,
                                   attr_vbo);
            break;
          case CustomRequest::Normal:
            update_normals_grids(object,
                                 orig_mesh_data,
                                 vbo_,
                                 vbo_.space.size(),
                                 use_flat_layout_,
                                 dirty_mask,
                                 scratch_offsets,
                                 attr_vbo);
            break;
          case CustomRequest::Mask:
            update_masks_grids(object,
                               vbo_,
                               vbo_.space.size(),
                               use_flat_layout_,
                               dirty_mask,
                               scratch_offsets,
                               attr_vbo);
            break;
          case CustomRequest::FaceSet:
            update_face_sets_grids(object,
                                   orig_mesh_data,
                                   vbo_,
                                   vbo_.space.size(),
                                   use_flat_layout_,
                                   dirty_mask,
                                   scratch_offsets,
                                   attr_vbo);
            break;
        }
      }
      else {
        update_generic_attribute_grids(object,
                                       orig_mesh_data,
                                       vbo_,
                                       vbo_.space.size(),
                                       std::get<GenericRequest>(attr),
                                       use_flat_layout_,
                                       dirty_mask,
                                       scratch_offsets,
                                       attr_vbo);
      }
      break;
    }
    case bke::pbvh::Type::BMesh: {
      if (const CustomRequest *request_type = std::get_if<CustomRequest>(&attr)) {
        switch (*request_type) {
          case CustomRequest::Position:
            update_positions_bmesh(
                object, vbo_, vbo_.space.size(), dirty_mask, scratch_offsets, attr_vbo);
            break;
          case CustomRequest::Normal:
            update_normals_bmesh(
                object, vbo_, vbo_.space.size(), dirty_mask, scratch_offsets, attr_vbo);
            break;
          case CustomRequest::Mask:
            update_masks_bmesh(
                object, vbo_, vbo_.space.size(), dirty_mask, scratch_offsets, attr_vbo);
            break;
          case CustomRequest::FaceSet:
            update_face_sets_bmesh(object,
                                   orig_mesh_data,
                                   vbo_,
                                   vbo_.space.size(),
                                   dirty_mask,
                                   scratch_offsets,
                                   attr_vbo);
            break;
        }
      }
      else {
        update_generic_attribute_bmesh(object,
                                       orig_mesh_data,
                                       vbo_,
                                       vbo_.space.size(),
                                       std::get<GenericRequest>(attr),
                                       dirty_mask,
                                       scratch_offsets,
                                       attr_vbo);
      }
      break;
    }
  }

  /* TODO: It would be good to deallocate the bit vector if all of the bits have been reset to
   * avoid unnecessary processing in subsequent redraws. */
  dirty_mask.foreach_index_optimized<int>(
      [&](const int node_index) { attr_vbo.dirty_nodes[node_index].reset(); });

  return attr_vbo;
}

Span<gpu::IndexBufPtr> DrawCacheImpl::ensure_tri_indices(const Object &object,
                                                         const OrigMeshData &orig_mesh_data,
                                                         const IndexMask &node_mask,
                                                         const bool coarse)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  IBOFamily &ibo_family = coarse ? ibo_tris_coarse_ : ibo_tris_;
  ibo_family.ensure_capacity();
  const Buffer &ibo_buffer = ibo_family.buffer;
  Vector<gpu::IndexBufPtr> &ibos = ibo_family.ibos;
  ibos.resize(pbvh.nodes_num());

  /* Whenever a node's visible triangle count has changed the index buffers are freed, so we only
   * recalculate null IBOs here. A new mask is recalculated for more even task distribution between
   * threads. */
  IndexMaskMemory memory;
  const IndexMask nodes_to_calculate = IndexMask::from_predicate(
      node_mask, memory, [&](const int node_index) {
        return !ibos[node_index] && ibo_buffer.acquired[node_index];
      });

  /* Build scratch offsets for the dirty node family, and the scratch buffer to stage into. */
  const ScratchOffsets scratch_offsets = build_scratch_offsets(ibo_buffer, nodes_to_calculate);
  Array<uint32_t> scratch_buffer(uint32_t(scratch_offsets.total));

  switch (pbvh.type()) {
    case bke::pbvh::Type::Mesh: {
      const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
      const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
      const OffsetIndices<int> faces = mesh.faces();
      const Span<int> corner_verts = mesh.corner_verts();
      const Span<int3> corner_tris = mesh.corner_tris();
      const bke::AttributeAccessor attributes = orig_mesh_data.attributes;
      const VArraySpan hide_poly = *attributes.lookup<bool>(".hide_poly", bke::AttrDomain::Face);
      nodes_to_calculate.foreach_index(
          [&](const int node_index, const int scratch_index) {
            if (coarse && this->has_mesh_proxy(node_index)) {
              /* Phantom: the node's coarse triangles (#build_mesh_node_proxy). */
              const Span<uint3> proxy = mesh_proxies_[node_index];
              MutableSpan<uint3> data = MutableSpan<uint32_t>(
                                            scratch_buffer.data() +
                                                scratch_offsets.offsets[scratch_index],
                                            ibo_buffer.sizes[node_index])
                                            .cast<uint3>();
              for (const int t : proxy.index_range()) {
                data[t] = proxy[t] + uint(vbo_.offsets[node_index]);
              }
              return;
            }
            create_tri_index_mesh(faces,
                                  corner_verts,
                                  corner_tris,
                                  hide_poly,
                                  nodes[node_index],
                                  use_flat_layout_[node_index],
                                  vbo_.offsets[node_index],
                                  ibo_buffer.sizes[node_index],
                                  scratch_buffer.data() + scratch_offsets.offsets[scratch_index]);
          },
          exec_mode::grain_size(1));
      break;
    }
    case bke::pbvh::Type::Grids: {
      /* Like meshes, multires grids use indexed vertex buffers because when there are no flat
       * faces, vertices can be shared between neighboring quads. This results in a 4x decrease in
       * the amount of data uploaded. Theoretically it also means freeing VBOs because of
       * visibility changes is unnecessary.
       *
       * TODO: With the "flat layout" and no hidden faces, the index buffers are unnecessary, we
       * should avoid creating them in that case. */
      const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
      const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
      const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
      nodes_to_calculate.foreach_index(
          [&](const int node_index, const int scratch_index) {
            create_tri_index_grids(key,
                                   subdiv_ccg.grid_hidden,
                                   coarse,
                                   nodes[node_index].grids(),
                                   use_flat_layout_[node_index],
                                   vbo_.offsets[node_index],
                                   ibo_buffer.sizes[node_index],
                                   scratch_buffer.data() + scratch_offsets.offsets[scratch_index]);
          },
          exec_mode::grain_size(1));
      break;
    }
    case bke::pbvh::Type::BMesh: {
      const Span<bke::pbvh::BMeshNode> nodes = pbvh.nodes<bke::pbvh::BMeshNode>();
      nodes_to_calculate.foreach_index(
          [&](const int node_index, const int scratch_index) {
            const Set<BMFace *, 0> &faces = BKE_pbvh_bmesh_node_faces(
                &const_cast<bke::pbvh::BMeshNode &>(nodes[node_index]));
            create_tri_index_bmesh(faces,
                                   vbo_.offsets[node_index],
                                   ibo_buffer.sizes[node_index],
                                   scratch_buffer.data() + scratch_offsets.offsets[scratch_index]);
          },
          exec_mode::grain_size(1));
      break;
    }
  }

  ibo_family.commit_subranges(nodes_to_calculate, scratch_offsets, scratch_buffer);
  return ibos;
}

void DrawCacheImpl::IBOFamily::commit_subranges(const IndexMask &node_mask,
                                                const ScratchOffsets &scratch_offsets,
                                                const Span<uint32_t> scratch_buffer)
{
  node_mask.foreach_index([&](const int node_index, const int scratch_index) {
    const IndexRange dst_range = buffer.index_range(node_index);
    update_ibos_subrange(
        *ibo, dst_range, scratch_buffer.data() + scratch_offsets.offsets[scratch_index]);
    ibos[node_index] = gpu::IndexBufPtr(
        GPU_indexbuf_create_subrange(ibo.get(), uint(dst_range.start()), uint(dst_range.size())));
  });
}

void DrawCacheImpl::IBOFamily::ensure_capacity()
{
  const uint32_t needed_len = uint32_t(buffer.space.size());
  if (ibo && ibo->index_len_get() >= needed_len) {
    return;
  }

  /* Create new buffer. */
  gpu::IndexBufPtr new_ibo = gpu::IndexBufPtr(
      GPU_indexbuf_build_on_device(std::max(needed_len, 1u)));
  GPU_indexbuf_use(new_ibo.get());
  /* Copy over data from previous buffer. */
  if (ibo) {
    new_ibo->copy_sub(*ibo, 0, 0, ibo->index_len_get());
    /* Rebuild all subranges. */
    for (gpu::IndexBufPtr &ibo_sub : ibos) {
      if (ibo_sub) {
        gpu::IndexBufPtr new_sub = gpu::IndexBufPtr(GPU_indexbuf_create_subrange(
            new_ibo.get(), ibo_sub->index_start_get(), ibo_sub->index_len_get()));
        ibo_sub.swap(new_sub);
      }
    }
  }
  ibo.swap(new_ibo);
}

/**
 * Nodes split by dynamic topology are no longer leaves, but still have space assigned in the
 * shared buffers. Include them in range updates, where their size of zero releases that space.
 */
static IndexMask add_split_bmesh_nodes(const bke::pbvh::Tree &pbvh,
                                       const IndexMask &node_mask,
                                       IndexMaskMemory &memory)
{
  if (pbvh.type() != bke::pbvh::Type::BMesh) {
    return node_mask;
  }
  const IndexMask internal_nodes =
      bke::pbvh::all_leaf_nodes(pbvh, memory).complement(IndexRange(pbvh.nodes_num()), memory);
  return IndexMask::from_union(node_mask, internal_nodes, memory);
}

Span<gpu::IndexBufPtr> DrawCacheImpl::prepare_tris(const Object &object,
                                                   const OrigMeshData &orig_mesh_data,
                                                   const ViewportRequest &request,
                                                   const IndexMask &nodes_to_update,
                                                   Vector<gpu::VertBuf *> &r_attr_vbos)
{
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const bool coarse = use_coarse_buffers(pbvh, request);

  if (pbvh.type() == bke::pbvh::Type::Mesh && !mesh_force_flat_ &&
      request_needs_flat_layout(orig_mesh_data, request))
  {
    /* Changing the layout moves the data of every node, so it must happen before batches are
     * given to a draw engine in this frame. Requests with face corner or face attributes come
     * from the render engine (Workbench texture or attribute colors, EEVEE materials), which
     * syncs objects before the overlays. */
    mesh_force_flat_ = true;
    layout_dirty_.resize(pbvh.nodes_num());
    layout_dirty_.fill(true);
  }
  if (pbvh.type() != bke::pbvh::Type::BMesh && !layout_uses_sharp_faces_ &&
      request_has_normals(request))
  {
    /* The same applies: normals are requested by EEVEE, the render engine. */
    layout_uses_sharp_faces_ = true;
    layout_dirty_.resize(pbvh.nodes_num());
    layout_dirty_.fill(true);
  }

  IndexMaskMemory memory;
  const IndexMask range_mask = add_split_bmesh_nodes(pbvh, nodes_to_update, memory);
  IBOFamily &ibo_family = coarse ? ibo_tris_coarse_ : ibo_tris_;
  bool grown = this->ensure_vert_ranges(object, orig_mesh_data, range_mask);
  const bool mesh_coarse = coarse && pbvh.type() == bke::pbvh::Type::Mesh;
  if (mesh_coarse) {
    this->update_mesh_proxies(object, orig_mesh_data, nodes_to_update);
  }
  grown |= ibo_family.ensure_ranges(
      pbvh.nodes_num(), range_mask, [&](const IndexMask &mask, MutableSpan<int> sizes) {
        calc_tris_index_counts(object, orig_mesh_data, coarse, mask, sizes);
        if (mesh_coarse) {
          /* Nodes with coarse triangles draw those instead. */
          mask.foreach_index([&](const int i, const int pos) {
            if (this->has_mesh_proxy(i)) {
              sizes[pos] = int(mesh_proxies_[i].size()) * 3;
            }
          });
        }
      });
  if (grown) {
    this->tag_batches_dirty();
  }

  this->free_nodes_with_changed_topology(pbvh);

  const Span<gpu::IndexBufPtr> ibos = this->ensure_tri_indices(
      object, orig_mesh_data, nodes_to_update, coarse);

  for (const AttributeRequest &attr : request.attributes) {
    this->ensure_attribute_data(object, orig_mesh_data, attr, nodes_to_update);
  }

  /* Collect VBO spans in a different loop because #ensure_attribute_data invalidates the allocated
   * arrays when its map is changed. */
  for (const AttributeRequest &attr : request.attributes) {
    if (const AttributeData *attr_data = attribute_vbos_.lookup_ptr(attr)) {
      r_attr_vbos.append(attr_data->vbo.get());
    }
  }
  return ibos;
}

Span<Batch *> DrawCacheImpl::ensure_tris_batches(const Object &object,
                                                 const ViewportRequest &request,
                                                 const IndexMask &nodes_to_update)
{
  PRF_scope(ProfileCategory::Draw);
  const Object &object_orig = *DEG_get_original(&object);
  const OrigMeshData orig_mesh_data{*id_cast<const Mesh *>(object_orig.data)};
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);

  Vector<gpu::VertBuf *> attr_vbos;
  const Span<gpu::IndexBufPtr> ibos = this->prepare_tris(
      object, orig_mesh_data, request, nodes_to_update, attr_vbos);

  /* Except for the first iteration of the draw loop, we only need to rebuild batches for nodes
   * with changed topology (visible triangle count). Rebuilds reconfigure the batch in place (the
   * passes that draw them cache the raw batch pointers until submit, so freeing them mid-frame
   * would leave those passes with dangling pointers). */
  BatchCache &batch_cache = tris_batches_.lookup_or_add_default(request);
  Vector<Batch *> &batches = batch_cache.batches;
  batches.resize(pbvh.nodes_num(), nullptr);
  batch_cache.dirty.resize(pbvh.nodes_num(), true);
  nodes_to_update.foreach_index(
      [&](const int node_index) {
        const bool has_ibo = !ibos.is_empty() && ibos[node_index] != nullptr;
        if (!batches[node_index] || batch_cache.dirty[node_index] ||
            batches[node_index]->gpu_batch()->elem_() !=
                (has_ibo ? ibos[node_index].get() : nullptr))
        {
          if (!has_ibo) {
            /* No geometry for this node. Clear the batch so it doesn't reference a freed index
             * buffer, without freeing it as passes may still reference it. */
            Batch::clear(batches[node_index]);
            return;
          }
          if (!batches[node_index]) {
            batches[node_index] = MEM_new<Batch>(__func__, GPU_batch_calloc());
          }
          Batch::clear(batches[node_index]);
          GPU_batch_init(
              batches[node_index]->gpu_batch(), GPU_PRIM_TRIS, nullptr, ibos[node_index].get());
          for (gpu::VertBuf *vbo : attr_vbos) {
            GPU_batch_vertbuf_add(batches[node_index]->gpu_batch(), vbo, false);
          }
        }
      },
      exec_mode::grain_size(64));
  /* Clear the tags in a separate serial loop, since neighboring bits share the same word. */
  nodes_to_update.foreach_index([&](const int node_index) {
    if (!ibos.is_empty() && ibos[node_index]) {
      batch_cache.dirty[node_index].reset();
    }
  });

  return batches;
}

Span<gpu::IndexBufPtr> DrawCacheImpl::prepare_lines(const Object &object,
                                                    const OrigMeshData &orig_mesh_data,
                                                    const ViewportRequest &request,
                                                    const IndexMask &nodes_to_update,
                                                    gpu::VertBuf *&r_position_vbo)
{
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const bool coarse = use_coarse_buffers(pbvh, request);

  IndexMaskMemory memory;
  const IndexMask range_mask = add_split_bmesh_nodes(pbvh, nodes_to_update, memory);
  IBOFamily &ibo_family = coarse ? ibo_lines_coarse_ : ibo_lines_;
  bool grown = this->ensure_vert_ranges(object, orig_mesh_data, range_mask);
  const bool mesh_coarse = coarse && pbvh.type() == bke::pbvh::Type::Mesh;
  if (mesh_coarse) {
    this->update_mesh_proxies(object, orig_mesh_data, nodes_to_update);
    this->update_mesh_proxy_lines(nodes_to_update);
  }
  grown |= ibo_family.ensure_ranges(
      pbvh.nodes_num(), range_mask, [&](const IndexMask &mask, MutableSpan<int> sizes) {
        calc_lines_index_counts(object, orig_mesh_data, coarse, mask, sizes);
        if (mesh_coarse) {
          /* Nodes with coarse triangles draw their edges instead. */
          mask.foreach_index([&](const int i, const int pos) {
            if (this->has_mesh_proxy(i)) {
              sizes[pos] = int(mesh_proxy_lines_[i].size()) * 2;
            }
          });
        }
      });
  if (grown) {
    this->tag_batches_dirty();
  }

  this->free_nodes_with_changed_topology(pbvh);

  const AttributeData &position = this->ensure_attribute_data(
      object, orig_mesh_data, CustomRequest::Position, nodes_to_update);
  r_position_vbo = position.vbo.get();
  return this->ensure_lines_indices(object, orig_mesh_data, nodes_to_update, coarse);
}

Span<Batch *> DrawCacheImpl::ensure_lines_batches(const Object &object,
                                                  const ViewportRequest &request,
                                                  const IndexMask &nodes_to_update)
{
  PRF_scope(ProfileCategory::Draw);
  const Object &object_orig = *DEG_get_original(&object);
  const OrigMeshData orig_mesh_data(*id_cast<const Mesh *>(object_orig.data));
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const bool coarse = use_coarse_buffers(pbvh, request);

  gpu::VertBuf *position_vbo = nullptr;
  const Span<gpu::IndexBufPtr> lines = this->prepare_lines(
      object, orig_mesh_data, request, nodes_to_update, position_vbo);

  /* See the note on #ensure_tris_batches for why the batches are reconfigured in place. */
  BatchCache &batch_cache = coarse ? lines_batches_coarse_ : lines_batches_;
  Vector<Batch *> &batches = batch_cache.batches;
  batches.resize(pbvh.nodes_num(), nullptr);
  batch_cache.dirty.resize(pbvh.nodes_num(), true);
  nodes_to_update.foreach_index(
      [&](const int node_index) {
        if (lines[node_index] == nullptr) {
          /* See the same case in #ensure_tris_batches. */
          Batch::clear(batches[node_index]);
          return;
        }
        if (!batches[node_index] || batch_cache.dirty[node_index] ||
            batches[node_index]->gpu_batch()->elem_() != lines[node_index].get())
        {
          if (!batches[node_index]) {
            batches[node_index] = MEM_new<Batch>(__func__, GPU_batch_calloc());
          }
          Batch::clear(batches[node_index]);
          GPU_batch_init(
              batches[node_index]->gpu_batch(), GPU_PRIM_LINES, nullptr, lines[node_index].get());
          GPU_batch_vertbuf_add(batches[node_index]->gpu_batch(), position_vbo, false);
        }
      },
      exec_mode::grain_size(64));
  /* Clear the tags in a separate serial loop, since neighboring bits share the same word. */
  nodes_to_update.foreach_index([&](const int node_index) {
    if (lines[node_index]) {
      batch_cache.dirty[node_index].reset();
    }
  });

  return batches;
}

/**
 * Phantom: point a shared batch at the whole index buffer of a family and at the given vertex
 * buffers (any of them may have been reallocated since the last frame), and list each node's part
 * of the index buffer.
 */
static Batch *update_shared_batch(DrawCacheImpl::SharedBatch &shared,
                                  const GPUPrimType prim_type,
                                  const Span<gpu::IndexBufPtr> node_ibos,
                                  gpu::IndexBuf *ibo,
                                  const Span<gpu::VertBuf *> vbos,
                                  const int nodes_num,
                                  Vector<IndexRange> &r_node_indices)
{
  r_node_indices.clear();
  r_node_indices.resize(nodes_num, IndexRange());
  for (const int node_index : node_ibos.index_range()) {
    if (const gpu::IndexBuf *node_ibo = node_ibos[node_index].get()) {
      r_node_indices[node_index] = IndexRange(node_ibo->index_start_get(),
                                              node_ibo->index_len_get());
    }
  }
  if (ibo == nullptr) {
    return nullptr;
  }
  if (!shared.batch) {
    shared.batch = MEM_new<Batch>(__func__, GPU_batch_calloc());
  }
  if (shared.ibo != ibo || shared.vbos.as_span() != vbos) {
    Batch::clear(shared.batch);
    GPU_batch_init(shared.batch->gpu_batch(), prim_type, nullptr, ibo);
    for (gpu::VertBuf *vbo : vbos) {
      GPU_batch_vertbuf_add(shared.batch->gpu_batch(), vbo, false);
    }
    shared.ibo = ibo;
    shared.vbos = vbos;
  }
  return shared.batch;
}

Batch *DrawCacheImpl::ensure_tris_shared_batch(const Object &object,
                                               const ViewportRequest &request,
                                               const IndexMask &nodes_to_update,
                                               Vector<IndexRange> &r_node_indices)
{
  PRF_scope(ProfileCategory::Draw);
  const Object &object_orig = *DEG_get_original(&object);
  const OrigMeshData orig_mesh_data{*id_cast<const Mesh *>(object_orig.data)};
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const bool coarse = use_coarse_buffers(pbvh, request);

  Vector<gpu::VertBuf *> attr_vbos;
  const Span<gpu::IndexBufPtr> ibos = this->prepare_tris(
      object, orig_mesh_data, request, nodes_to_update, attr_vbos);
  const IBOFamily &ibo_family = coarse ? ibo_tris_coarse_ : ibo_tris_;
  return update_shared_batch(tris_shared_batches_.lookup_or_add_default(request),
                             GPU_PRIM_TRIS,
                             ibos,
                             ibo_family.ibo.get(),
                             attr_vbos,
                             pbvh.nodes_num(),
                             r_node_indices);
}

Batch *DrawCacheImpl::ensure_lines_shared_batch(const Object &object,
                                                const ViewportRequest &request,
                                                const IndexMask &nodes_to_update,
                                                Vector<IndexRange> &r_node_indices)
{
  PRF_scope(ProfileCategory::Draw);
  const Object &object_orig = *DEG_get_original(&object);
  const OrigMeshData orig_mesh_data(*id_cast<const Mesh *>(object_orig.data));
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const bool coarse = use_coarse_buffers(pbvh, request);

  gpu::VertBuf *position_vbo = nullptr;
  const Span<gpu::IndexBufPtr> lines = this->prepare_lines(
      object, orig_mesh_data, request, nodes_to_update, position_vbo);
  const IBOFamily &ibo_family = coarse ? ibo_lines_coarse_ : ibo_lines_;
  return update_shared_batch(lines_shared_batches_[coarse ? 1 : 0],
                             GPU_PRIM_LINES,
                             lines,
                             ibo_family.ibo.get(),
                             {position_vbo},
                             pbvh.nodes_num(),
                             r_node_indices);
}

/* -------------------------------------------------------------------- */
/** \name Phantom: coarse mesh triangles for navigation
 * \{ */

/**
 * The coarse version of a mesh node drawn while navigating: its vertices are snapped to the one
 * nearest the center of each cell of a grid about three edges wide, and triangles that collapse
 * are dropped. Vertices shared with other nodes stay, so the coarse nodes still meet their
 * neighbors (coarse or not) without cracks. Indices are into the node's vertices
 * (#MeshNode::all_verts), which the node's vertex buffer stores in the indexed layout. Empty when
 * the node should draw all of its triangles instead.
 *
 * The positions only choose the cells: after the vertices move, the triangles still cover the
 * surface without cracks, so they are kept until the visible faces change.
 */
static Array<uint3> build_mesh_node_proxy(const OffsetIndices<int> faces,
                                          const Span<int> corner_verts,
                                          const Span<int3> corner_tris,
                                          const Span<bool> hide_poly,
                                          const Span<float3> positions,
                                          const Span<bool> seam_verts,
                                          const bke::pbvh::MeshNode &node)
{
  const Span<int> verts = node.all_verts();
  if (verts.is_empty()) {
    return {};
  }
  /* The cell size, from the average edge length of a sample of the node's triangles. */
  double edge_sum = 0.0;
  int edges_num = 0;
  for (const int face : node.faces()) {
    for (const int3 &tri : corner_tris.slice(bke::mesh::face_triangles_range(faces, face))) {
      edge_sum += math::distance(positions[corner_verts[tri[0]]], positions[corner_verts[tri[1]]]);
      edges_num++;
    }
    if (edges_num >= 256) {
      break;
    }
  }
  const float cell = edges_num > 0 ? float(edge_sum / edges_num) * 3.0f : 0.0f;
  if (!(cell > 0.0f)) {
    return {};
  }
  float3 min(std::numeric_limits<float>::max());
  float3 max(std::numeric_limits<float>::lowest());
  for (const int vert : verts) {
    min = math::min(min, positions[vert]);
    max = math::max(max, positions[vert]);
  }
  /* A dense grid over the node's bounds: far cheaper than hashing, and small for nodes of a few
   * thousand faces. Nodes too large for it (long thin ones) draw all of their triangles. */
  const int3 dims = int3(math::floor((max - min) / cell)) + 1;
  const int64_t cells_num = int64_t(dims.x) * dims.y * dims.z;
  if (cells_num > (1 << 18)) {
    return {};
  }
  auto cell_of = [&](const float3 &position, int3 &r_cell) {
    r_cell = math::clamp(int3((position - min) / cell), int3(0), dims - 1);
    return (int64_t(r_cell.z) * dims.y + r_cell.y) * dims.x + r_cell.x;
  };
  /* The vertex nearest the center of each cell represents it. */
  Array<int> best(cells_num, -1);
  Array<float> best_distance(cells_num);
  for (const int i : verts.index_range()) {
    const float3 &position = positions[verts[i]];
    int3 c;
    const int64_t index = cell_of(position, c);
    const float distance = math::distance_squared(position, (float3(c) + 0.5f) * cell + min);
    if (best[index] == -1 || distance < best_distance[index]) {
      best[index] = i;
      best_distance[index] = distance;
    }
  }

  Vector<uint3> coarse;
  for (const int face : node.faces()) {
    if (!hide_poly.is_empty() && hide_poly[face]) {
      continue;
    }
    for (const int3 &tri : corner_tris.slice(bke::mesh::face_triangles_range(faces, face))) {
      /* Compare the snapped vertices by their index in the mesh, and only look up the node's
       * index of the vertices of the triangles that remain. */
      int3 snapped;
      int3 local(-1);
      for (const int corner : IndexRange(3)) {
        const int vert = corner_verts[tri[corner]];
        if (seam_verts[vert]) {
          snapped[corner] = vert;
        }
        else {
          int3 c;
          local[corner] = best[cell_of(positions[vert], c)];
          snapped[corner] = verts[local[corner]];
        }
      }
      if (snapped[0] == snapped[1] || snapped[1] == snapped[2] || snapped[2] == snapped[0]) {
        continue;
      }
      for (const int corner : IndexRange(3)) {
        if (local[corner] == -1) {
          local[corner] = node.vert_indices_.index_of(snapped[corner]);
        }
      }
      coarse.append(uint3(local));
    }
  }
  return Array<uint3>(coarse.as_span());
}

void DrawCacheImpl::update_mesh_proxies(const Object &object,
                                        const OrigMeshData &orig_mesh_data,
                                        const IndexMask &node_mask)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
  const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
  if (mesh_proxies_.size() != nodes.size()) {
    mesh_proxies_.reinitialize(nodes.size());
    mesh_proxy_lines_.reinitialize(nodes.size());
    mesh_proxies_dirty_.clear_and_shrink();
  }
  mesh_proxies_dirty_.resize(nodes.size(), true);
  IndexMaskMemory memory;
  const IndexMask dirty = IndexMask::from_bits(node_mask, mesh_proxies_dirty_, memory);
  if (dirty.is_empty()) {
    return;
  }
  if (mesh_seam_verts_.size() != mesh.verts_num) {
    /* A node lists the vertices of its faces that another node owns as "shared". */
    mesh_seam_verts_.reinitialize(mesh.verts_num);
    mesh_seam_verts_.fill(false);
    for (const bke::pbvh::MeshNode &node : nodes) {
      for (const int vert : node.shared_verts()) {
        mesh_seam_verts_[vert] = true;
      }
    }
  }
  const OffsetIndices<int> faces = mesh.faces();
  const Span<int> corner_verts = mesh.corner_verts();
  const Span<int3> corner_tris = mesh.corner_tris();
  const Span<float3> positions = bke::pbvh::vert_positions_eval_from_eval(object);
  const VArraySpan hide_poly = *orig_mesh_data.attributes.lookup<bool>(".hide_poly",
                                                                       bke::AttrDomain::Face);
  dirty.foreach_index(
      [&](const int i) {
        mesh_proxies_[i] = use_flat_layout_[i] ? Array<uint3>() :
                                                 build_mesh_node_proxy(faces,
                                                                       corner_verts,
                                                                       corner_tris,
                                                                       hide_poly,
                                                                       positions,
                                                                       mesh_seam_verts_,
                                                                       nodes[i]);
        /* Rebuilt with the wireframe (#update_mesh_proxy_lines). */
        mesh_proxy_lines_[i] = {};
      },
      exec_mode::grain_size(1));
  dirty.foreach_index([&](const int i) { mesh_proxies_dirty_[i].reset(); });
  /* The coarse index buffers of these nodes change size and contents. */
  ibo_tris_coarse_.buffer.tag_dirty(dirty);
  free_ibos(ibo_tris_coarse_.ibos, dirty);
  ibo_lines_coarse_.buffer.tag_dirty(dirty);
  free_ibos(ibo_lines_coarse_.ibos, dirty);
}

void DrawCacheImpl::update_mesh_proxy_lines(const IndexMask &node_mask)
{
  PRF_scope(ProfileCategory::Draw);
  IndexMaskMemory memory;
  const IndexMask missing = IndexMask::from_predicate(node_mask, memory, [&](const int i) {
    return this->has_mesh_proxy(i) && mesh_proxy_lines_[i].is_empty();
  });
  if (missing.is_empty()) {
    return;
  }
  missing.foreach_index(
      [&](const int i) {
        /* Every edge of every triangle, without the duplicates. */
        Vector<uint64_t> edges;
        edges.reserve(mesh_proxies_[i].size() * 3);
        for (const uint3 &tri : mesh_proxies_[i]) {
          for (const int corner : IndexRange(3)) {
            const uint a = tri[corner];
            const uint b = tri[(corner + 1) % 3];
            edges.append((uint64_t(std::min(a, b)) << 32) | uint64_t(std::max(a, b)));
          }
        }
        std::sort(edges.begin(), edges.end());
        edges.resize(std::unique(edges.begin(), edges.end()) - edges.begin());
        Array<uint2> &lines = mesh_proxy_lines_[i];
        lines.reinitialize(edges.size());
        for (const int e : edges.index_range()) {
          lines[e] = uint2(uint(edges[e] >> 32), uint(edges[e] & 0xFFFFFFFFu));
        }
      },
      exec_mode::grain_size(1));
  ibo_lines_coarse_.buffer.tag_dirty(missing);
  free_ibos(ibo_lines_coarse_.ibos, missing);
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Phantom: back-face culling of nodes
 * \{ */

/** A 64-bit mix (SplitMix64's finalizer): close inputs give unrelated outputs. */
static uint64_t closed_check_hash(uint64_t x)
{
  x += 0x9E3779B97F4A7C15ull;
  x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ull;
  x = (x ^ (x >> 27)) * 0x94D049BB133111EBull;
  return x ^ (x >> 31);
}

/**
 * Whether every edge of the mesh joins exactly two faces that run along it in opposite
 * directions. If the faces are also wound outwards (#ensure_normal_cones checks that), every back
 * face seen from outside lies behind a front face and can be skipped.
 *
 * Counting the faces of every edge means scattered writes to a table as big as the edges, which
 * is slow on big meshes. Instead the corners are read once, in order, adding up hashes: of each
 * corner's edge, where exactly two uses per edge sum to twice the edges' own sum; and of the edge
 * times the difference of the hashes of the vertices the corner runs from and to along it, which
 * cancels when the two uses run in opposite directions. A mesh that isn't closed would need a
 * 64-bit hash collision to pass.
 */
static bool mesh_is_closed(const Mesh &mesh)
{
  PRF_scope(ProfileCategory::Draw);
  const int edges_num = mesh.edges_num;
  const OffsetIndices<int> faces = mesh.faces();
  const Span<int> corner_verts = mesh.corner_verts();
  const Span<int> corner_edges = mesh.corner_edges();
  if (faces.is_empty()) {
    return false;
  }
  constexpr uint64_t edge_salt = 0x5851F42D4C957F2Dull;
  const auto edge_hash = [](const int edge) {
    return closed_check_hash(uint64_t(edge) ^ edge_salt);
  };
  using Sums = std::array<uint64_t, 2>;
  const Sums corner_sums = threading::parallel_reduce(
      faces.index_range(),
      4096,
      Sums{0, 0},
      [&](const IndexRange range, Sums sums) {
        for (const int face : range) {
          const IndexRange face_corners = faces[face];
          const uint64_t first_vert = closed_check_hash(uint64_t(corner_verts[face_corners[0]]));
          uint64_t from = first_vert;
          for (const int corner : face_corners) {
            const uint64_t to = corner == face_corners.last() ?
                                    first_vert :
                                    closed_check_hash(uint64_t(corner_verts[corner + 1]));
            const uint64_t edge = edge_hash(corner_edges[corner]);
            sums[0] += (from - to) * edge;
            sums[1] += edge;
            from = to;
          }
        }
        return sums;
      },
      [](const Sums &a, const Sums &b) { return Sums{a[0] + b[0], a[1] + b[1]}; });
  if (corner_sums[0] != 0) {
    return false;
  }
  const uint64_t edges_sum = threading::parallel_reduce(
      IndexRange(edges_num),
      65536,
      uint64_t(0),
      [&](const IndexRange range, uint64_t sum) {
        for (const int edge : range) {
          sum += edge_hash(edge);
        }
        return sum;
      },
      std::plus<>());
  return corner_sums[1] == edges_sum * 2;
}

/** Six times the signed volume the mesh's triangles enclose: positive when wound outwards. */
static double mesh_volume(const Mesh &mesh)
{
  const Span<float3> positions = mesh.vert_positions();
  const Span<int> corner_verts = mesh.corner_verts();
  const Span<int3> corner_tris = mesh.corner_tris();
  return threading::parallel_reduce(
      corner_tris.index_range(),
      65536,
      0.0,
      [&](const IndexRange range, double sum) {
        for (const int3 &tri : corner_tris.slice(range)) {
          const float3 &a = positions[corner_verts[tri[0]]];
          const float3 &b = positions[corner_verts[tri[1]]];
          const float3 &c = positions[corner_verts[tri[2]]];
          sum += double(math::dot(a, math::cross(b, c)));
        }
        return sum;
      },
      std::plus<>());
}

static bool any_face_hidden(const Object &object, const OrigMeshData &orig_mesh_data)
{
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  if (pbvh.type() == bke::pbvh::Type::Grids) {
    const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
    if (!subdiv_ccg.grid_hidden.is_empty()) {
      return true;
    }
  }
  const VArray<bool> hide_poly = *orig_mesh_data.attributes.lookup<bool>(".hide_poly",
                                                                         bke::AttrDomain::Face);
  if (!hide_poly) {
    return false;
  }
  const std::optional<bool> single = hide_poly.get_if_single();
  if (single) {
    return *single;
  }
  const VArraySpan<bool> span(hide_poly);
  return threading::parallel_reduce(
      span.index_range(),
      65536,
      false,
      [&](const IndexRange range, const bool init) {
        return init || span.slice(range).contains(true);
      },
      std::logical_or<>());
}

/**
 * Finds the cone around a given axis that holds a node's triangle normals. The axis comes from the
 * cached normals, which is cheap; the angle comes from the triangles as drawn, so a folded face
 * can't make the cone too narrow (which would cull a node that shows, and open a hole).
 */
struct NormalConeBuilder {
  float3 axis = float3(0.0f);
  /** The smallest squared cosine between a triangle normal and the axis so far. */
  float min_cos_sq = 1.0f;
  bool any = false;
  bool valid;

  explicit NormalConeBuilder(const float3 &axis_sum)
  {
    const float len = math::length(axis_sum);
    valid = len > 1e-6f;
    if (valid) {
      axis = axis_sum / len;
    }
  }

  /**
   * Add a triangle by its (unnormalized) normal. Returns false once the triangles span a half
   * space or more, after which the node can't be culled whatever else it holds.
   */
  bool add(const float3 &normal)
  {
    const float len_sq = math::length_squared(normal);
    if (len_sq <= 1e-40f) {
      return true;
    }
    const float d = math::dot(normal, axis);
    if (d <= 0.0f) {
      valid = false;
      return false;
    }
    min_cos_sq = std::min(min_cos_sq, d * d / len_sq);
    any = true;
    return true;
  }

  NormalCone finish() const
  {
    NormalCone cone;
    if (!valid || !any) {
      return cone;
    }
    /* A little margin for rounding: culling a node that shows would open a hole. */
    const float min_cos = std::sqrt(min_cos_sq) - 1e-3f;
    if (min_cos <= 0.0f) {
      return cone;
    }
    cone.axis = axis;
    cone.sin_angle = std::sqrt(1.0f - min_cos * min_cos);
    return cone;
  }
};

/**
 * \param r_volume: When given, six times the signed volume under the node's triangles is added to
 * it (#mesh_volume), and all of them are visited.
 */
static NormalCone calc_mesh_node_cone(const OffsetIndices<int> faces,
                                      const Span<int> corner_verts,
                                      const Span<int3> corner_tris,
                                      const Span<float3> positions,
                                      const Span<float3> face_normals,
                                      const bke::pbvh::MeshNode &node,
                                      double *r_volume)
{
  float3 axis(0.0f);
  for (const int face : node.faces()) {
    axis += face_normals[face];
  }
  NormalConeBuilder builder(axis);
  double volume = 0.0;
  for (const int face : node.faces()) {
    for (const int3 &tri : corner_tris.slice(bke::mesh::face_triangles_range(faces, face))) {
      const float3 &a = positions[corner_verts[tri[0]]];
      const float3 normal = math::cross(positions[corner_verts[tri[1]]] - a,
                                        positions[corner_verts[tri[2]]] - a);
      if (r_volume) {
        volume += double(math::dot(a, normal));
        builder.add(normal);
      }
      else if (!builder.add(normal)) {
        return NormalCone();
      }
    }
  }
  if (r_volume) {
    *r_volume += volume;
  }
  return builder.finish();
}

static NormalCone calc_grids_node_cone(const CCGKey &key,
                                       const Span<float3> positions,
                                       const Span<float3> normals,
                                       const bke::pbvh::GridsNode &node)
{
  float3 axis(0.0f);
  if (!normals.is_empty()) {
    for (const int grid : node.grids()) {
      for (const float3 &normal : normals.slice(bke::ccg::grid_range(key, grid))) {
        axis += normal;
      }
    }
  }
  if (!node.grids().is_empty() && key.grid_size > 1) {
    /* The grids' normals may point either way relative to the drawn winding: follow the latter. */
    const Span<float3> grid_positions = positions.slice(bke::ccg::grid_range(key, node.grids()[0]));
    const float3 &p00 = grid_positions[CCG_grid_xy_to_index(key.grid_size, 0, 0)];
    const float3 &p11 = grid_positions[CCG_grid_xy_to_index(key.grid_size, 1, 1)];
    const float3 &p10 = grid_positions[CCG_grid_xy_to_index(key.grid_size, 1, 0)];
    const float3 &p01 = grid_positions[CCG_grid_xy_to_index(key.grid_size, 0, 1)];
    if (math::dot(axis, math::cross(p11 - p00, p10 - p00) + math::cross(p01 - p00, p11 - p00)) <
        0.0f)
    {
      axis = -axis;
    }
  }
  NormalConeBuilder builder(axis);
  for (const int grid : node.grids()) {
    const Span<float3> grid_positions = positions.slice(bke::ccg::grid_range(key, grid));
    for (int y = 0; y < key.grid_size - 1; y++) {
      for (int x = 0; x < key.grid_size - 1; x++) {
        const float3 &p00 = grid_positions[CCG_grid_xy_to_index(key.grid_size, x, y)];
        const float3 &p10 = grid_positions[CCG_grid_xy_to_index(key.grid_size, x + 1, y)];
        const float3 &p11 = grid_positions[CCG_grid_xy_to_index(key.grid_size, x + 1, y + 1)];
        const float3 &p01 = grid_positions[CCG_grid_xy_to_index(key.grid_size, x, y + 1)];
        /* Both ways of splitting the quad, since either may be drawn, wound like the drawn
         * triangles (#create_tri_index_grids): the grid's x/y order runs clockwise. */
        if (!builder.add(math::cross(p11 - p00, p10 - p00)) ||
            !builder.add(math::cross(p01 - p00, p11 - p00)) ||
            !builder.add(math::cross(p01 - p00, p10 - p00)) ||
            !builder.add(math::cross(p01 - p10, p11 - p10)))
        {
          return NormalCone();
        }
      }
    }
  }
  return builder.finish();
}

Span<NormalCone> DrawCacheImpl::ensure_normal_cones(const Object &object,
                                                    const IndexMask &node_mask,
                                                    const bool defer_changed)
{
  PRF_scope(ProfileCategory::Draw);
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  if (pbvh.type() == bke::pbvh::Type::BMesh) {
    return {};
  }
  const Object &object_orig = *DEG_get_original(&object);
  const Mesh &mesh_orig = *id_cast<const Mesh *>(object_orig.data);
  const OrigMeshData orig_mesh_data{mesh_orig};
  if (!closed_) {
    closed_ = mesh_is_closed(mesh_orig);
  }
  if (!*closed_ || (outward_ && !*outward_)) {
    return {};
  }
  if (!any_hidden_) {
    any_hidden_ = any_face_hidden(object, orig_mesh_data);
  }
  if (*any_hidden_) {
    return {};
  }

  if (normal_cones_.size() != pbvh.nodes_num()) {
    normal_cones_.reinitialize(pbvh.nodes_num());
    normal_cones_dirty_.clear_and_shrink();
  }
  normal_cones_dirty_.resize(pbvh.nodes_num(), true);
  IndexMaskMemory memory;
  /* The first time, every node's cone: for a mesh, the same pass over its triangles finds which
   * way they are wound. */
  const bool find_winding = !outward_;
  const IndexMask dirty = find_winding ?
                              bke::pbvh::all_leaf_nodes(pbvh, memory) :
                              IndexMask::from_bits(node_mask, normal_cones_dirty_, memory);
  if (dirty.is_empty()) {
    return normal_cones_;
  }
  if (defer_changed && !find_winding) {
    dirty.foreach_index([&](const int i) { normal_cones_[i] = NormalCone(); });
    return normal_cones_;
  }
  switch (pbvh.type()) {
    case bke::pbvh::Type::Mesh: {
      const Span<bke::pbvh::MeshNode> nodes = pbvh.nodes<bke::pbvh::MeshNode>();
      const Mesh &mesh = DRW_object_get_data_for_drawing<Mesh>(object);
      const OffsetIndices<int> faces = mesh.faces();
      const Span<int> corner_verts = mesh.corner_verts();
      const Span<int3> corner_tris = mesh.corner_tris();
      const Span<float3> positions = bke::pbvh::vert_positions_eval_from_eval(object);
      const Span<float3> face_normals = bke::pbvh::face_normals_eval_from_eval(object);
      threading::EnumerableThreadSpecific<double> volumes;
      dirty.foreach_index(
          [&](const int i) {
            normal_cones_[i] = calc_mesh_node_cone(faces,
                                                   corner_verts,
                                                   corner_tris,
                                                   positions,
                                                   face_normals,
                                                   nodes[i],
                                                   find_winding ? &volumes.local() : nullptr);
          },
          exec_mode::grain_size(1));
      if (find_winding) {
        double volume = 0.0;
        for (const double node_volume : volumes) {
          volume += node_volume;
        }
        outward_ = volume > 0.0;
      }
      break;
    }
    case bke::pbvh::Type::Grids: {
      const Span<bke::pbvh::GridsNode> nodes = pbvh.nodes<bke::pbvh::GridsNode>();
      const SubdivCCG &subdiv_ccg = *object.runtime->sculpt_session->subdiv_ccg;
      const CCGKey key = BKE_subdiv_ccg_key_top_level(subdiv_ccg);
      dirty.foreach_index(
          [&](const int i) {
            normal_cones_[i] = calc_grids_node_cone(
                key, subdiv_ccg.positions, subdiv_ccg.normals, nodes[i]);
          },
          exec_mode::grain_size(1));
      if (find_winding) {
        /* The base mesh is small, and wound like its grids. */
        outward_ = mesh_volume(mesh_orig) > 0.0;
      }
      break;
    }
    case bke::pbvh::Type::BMesh:
      break;
  }
  dirty.foreach_index([&](const int i) { normal_cones_dirty_[i].reset(); });
  if (!*outward_) {
    return {};
  }
  return normal_cones_;
}

/** \} */

Span<int> DrawCacheImpl::ensure_material_indices(const Object &object)
{
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const Object &object_orig = *DEG_get_original(&object);
  const OrigMeshData orig_mesh_data(*id_cast<const Mesh *>(object_orig.data));
  if (orig_mesh_data.attributes.contains("material_index") &&
      material_indices_.size() != pbvh.nodes_num())
  {
    material_indices_ = calc_material_indices(object, orig_mesh_data);
  }
  return material_indices_;
}

bool DrawCacheImpl::IBOFamily::ensure_ranges(
    const int nodes_num,
    const IndexMask &node_mask,
    const FunctionRef<void(const IndexMask &, MutableSpan<int>)> calc_sizes)
{
  PRF_scope(ProfileCategory::Draw);
  BitVector<> moved;
  const bool grown = buffer.ensure_ranges(nodes_num, node_mask, calc_sizes, moved);
  /* A moved sub-range only invalidates this family's own index buffers. The other families are
   * deliberately left alone: another draw engine may have retrieved their batches earlier in this
   * same frame, and freeing an index buffer out from under one of those leaves the pass that
   * cached the batch pointer referencing freed memory. */
  IndexMaskMemory memory;
  free_ibos(ibos, IndexMask::from_bits(moved, memory));
  return grown;
}

bool DrawCacheImpl::ensure_vert_ranges(const Object &object,
                                       const OrigMeshData &orig_mesh_data,
                                       const IndexMask &node_mask)
{
  const bke::pbvh::Tree &pbvh = *bke::object::pbvh_get(object);
  const BitSpan use_flat_layout = this->ensure_use_flat_layout(object, orig_mesh_data);
  /* Every index buffer stores offsets into the shared vertex buffer, and for BMesh the attribute
   * values are stored per visible triangle, so a moved vertex range invalidates all of them. That
   * is recorded globally in #dirty_topology_. This only affects the first request in the drawing
   * of a frame; the nodes a later request could still move are the ones it does not share with the
   * earlier request, which have not been given to a draw engine. */
  return vbo_.ensure_ranges(
      pbvh.nodes_num(),
      node_mask,
      [&](const IndexMask &mask, MutableSpan<int> sizes) {
        calc_vert_counts(object, use_flat_layout, mask, sizes);
      },
      dirty_topology_);
}

void DrawCacheImpl::tag_batches_dirty()
{
  /* Batches reference the shared buffers with raw pointers, so a reallocation invalidates all of
   * them, including the batches of viewports that aren't drawing right now. */
  for (BatchCache &batch_cache : tris_batches_.values()) {
    batch_cache.dirty.fill(true);
  }
  lines_batches_.dirty.fill(true);
  lines_batches_coarse_.dirty.fill(true);
}

}  // namespace draw::pbvh
}  // namespace blender
