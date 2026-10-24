/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup bke
 *
 * Phantom: the dynamic remesher.
 *
 * Stages:
 * - A: validate the input (empty, hidden, fully masked, non-manifold) and collect triangles.
 * - B: unsigned narrow-band distance on a uniform grid, with the closest triangle per sample.
 * - D: inside/outside: the samples next to the surface form a barrier that is dilated to close
 *      holes, the outside is flooded from the grid border, eroded back, and grown into the
 *      barrier samples that face outwards. Faces inside overlapping shells are dropped.
 * - C: dual contouring: one vertex per sign-changing cell (QEF, clamped to the cell), a quad
 *      across every sign-changing grid edge.
 * - E: closest-point projection onto the source, faded out across creases.
 *
 * Every step is deterministic regardless of thread count: the distance band uses an atomic
 * minimum over (distance, triangle) keys, and output elements are numbered by slab prefix sums.
 */

#include <algorithm>
#include <atomic>
#include <cmath>

#include "BLI_array.hh"
#include "BLI_array_utils.hh"
#include "BLI_index_mask.hh"
#include "BLI_kdopbvh.hh"
#include "BLI_math_base.hh"
#include "BLI_math_geom_c.hh"
#include "BLI_math_vector.hh"
#include "BLI_offset_indices.hh"
#include "BLI_sort.hh"
#include "BLI_task.hh"
#include "BLI_time.hh"
#include "BLI_vector.hh"

#include "BKE_attribute.hh"
#include "BKE_mesh.hh"
#include "BKE_mesh_remesh_dynamic.hh" /* own include */
#include "BKE_report.hh"

namespace blender {

namespace dynamic_remesh {

/** A uniform lattice of samples. Cells are the cubes between eight samples. */
struct Grid {
  float3 origin;
  float cell;
  int3 dims;

  int64_t size() const
  {
    return int64_t(dims.x) * int64_t(dims.y) * int64_t(dims.z);
  }
  int64_t index(const int x, const int y, const int z) const
  {
    return x + int64_t(dims.x) * (y + int64_t(dims.y) * z);
  }
  float3 position(const int x, const int y, const int z) const
  {
    return origin + float3(float(x), float(y), float(z)) * cell;
  }
};

/** Per-sample state bits. */
enum : uint8_t {
  /** Within half a cell diagonal of the surface: every cell the surface passes through. */
  SAMPLE_BARRIER = 1 << 0,
  /** The barrier dilated by the closing radius; the outside flood stops here. */
  SAMPLE_BLOCK = 1 << 1,
  SAMPLE_OUTSIDE = 1 << 2,
  /** Barrier sample on the outer side of its closest triangle. */
  SAMPLE_FACES_OUT = 1 << 3,
  SAMPLE_TMP = 1 << 4,
};

static constexpr uint64_t KEY_EMPTY = UINT64_MAX;

/** Distances are positive, so their bits sort like the values; ties go to the lower triangle. */
static uint64_t band_key(const float dist_sq, const int tri)
{
  uint32_t bits;
  memcpy(&bits, &dist_sq, sizeof(bits));
  return (uint64_t(bits) << 32) | uint32_t(tri);
}

static float key_dist_sq(const uint64_t key)
{
  const uint32_t bits = uint32_t(key >> 32);
  float dist_sq;
  memcpy(&dist_sq, &bits, sizeof(dist_sq));
  return dist_sq;
}

static int key_tri(const uint64_t key)
{
  return int(key & 0xFFFFFFFFu);
}

static void atomic_min(uint64_t &dst, const uint64_t value)
{
  std::atomic_ref<uint64_t> ref(dst);
  uint64_t current = ref.load(std::memory_order_relaxed);
  while (value < current &&
         !ref.compare_exchange_weak(current, value, std::memory_order_relaxed))
  {
  }
}

/** Calls `fn(start, step, count)` for every line of samples along `axis`, in parallel. */
template<typename Fn> static void foreach_line(const Grid &grid, const int axis, const Fn &fn)
{
  const int3 d = grid.dims;
  const int64_t step = axis == 0 ? 1 : (axis == 1 ? int64_t(d.x) : int64_t(d.x) * d.y);
  const int a = axis == 0 ? d.y : d.x;
  const int b = axis == 2 ? d.y : d.z;
  const int count = d[axis];
  threading::parallel_for(IndexRange(int64_t(a) * b), 256, [&](const IndexRange range) {
    for (const int64_t line : range) {
      const int u = int(line % a);
      const int v = int(line / a);
      const int64_t start = axis == 0 ? grid.index(0, u, v) :
                            axis == 1 ? grid.index(u, 0, v) :
                                        grid.index(u, v, 0);
      fn(start, step, count);
    }
  });
}

/** Dilates the `bit` of `state` by a cube of `radius` samples, as three separable passes. */
static void dilate(const Grid &grid, MutableSpan<uint8_t> state, const uint8_t bit, const int radius)
{
  if (radius <= 0) {
    return;
  }
  for (const int axis : IndexRange(3)) {
    threading::parallel_for(state.index_range(), 1 << 16, [&](const IndexRange range) {
      for (const int64_t i : range) {
        state[i] = (state[i] & ~SAMPLE_TMP) | ((state[i] & bit) ? SAMPLE_TMP : 0);
      }
    });
    foreach_line(grid, axis, [&](const int64_t start, const int64_t step, const int count) {
      for (int i = 0; i < count; i++) {
        const int lo = std::max(0, i - radius);
        const int hi = std::min(count - 1, i + radius);
        for (int j = lo; j <= hi; j++) {
          if (state[start + j * step] & SAMPLE_TMP) {
            state[start + i * step] |= bit;
            break;
          }
        }
      }
    });
  }
}

/**
 * Grows #SAMPLE_OUTSIDE through the samples `passable` accepts with forward and backward sweeps
 * along each axis, until nothing changes. Always converges to the 6-connected flood fill.
 */
template<typename PassableFn>
static void flood_outside(const Grid &grid, MutableSpan<uint8_t> state, const PassableFn &passable)
{
  bool changed = true;
  while (changed) {
    std::atomic<bool> any_change = false;
    for (const int axis : IndexRange(3)) {
      foreach_line(grid, axis, [&](const int64_t start, const int64_t step, const int count) {
        bool line_change = false;
        for (const int dir : {1, -1}) {
          bool carry = false;
          for (int n = 0; n < count; n++) {
            const int64_t i = start + (dir == 1 ? n : count - 1 - n) * step;
            if (state[i] & SAMPLE_OUTSIDE) {
              carry = true;
            }
            else if (carry && passable(state[i])) {
              state[i] |= SAMPLE_OUTSIDE;
              line_change = true;
            }
            else {
              carry = false;
            }
          }
        }
        if (line_change) {
          any_change.store(true, std::memory_order_relaxed);
        }
      });
    }
    changed = any_change.load();
  }
}

struct BVHUserData {
  Span<float3> positions;
  Span<int3> tris;
};

static void nearest_tri_cb(void *userdata, int index, const float co[3], BVHTreeNearest *nearest)
{
  const BVHUserData &data = *static_cast<const BVHUserData *>(userdata);
  const int3 &tri = data.tris[index];
  float3 closest;
  closest_on_tri_to_point_v3(
      closest, co, data.positions[tri[0]], data.positions[tri[1]], data.positions[tri[2]]);
  const float dist_sq = math::distance_squared(float3(co), closest);
  if (dist_sq < nearest->dist_sq) {
    nearest->index = index;
    nearest->dist_sq = dist_sq;
    copy_v3_v3(nearest->co, closest);
    normal_tri_v3(
        nearest->no, data.positions[tri[0]], data.positions[tri[1]], data.positions[tri[2]]);
  }
}

/** Solves the 3x3 symmetric system `a x = b`; returns false when it is singular. */
static bool solve_3x3(const double a[3][3], const double b[3], double r_x[3])
{
  const double det = a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) -
                     a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0]) +
                     a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]);
  if (std::abs(det) < 1e-12) {
    return false;
  }
  for (int c = 0; c < 3; c++) {
    double m[3][3];
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 3; j++) {
        m[i][j] = (j == c) ? b[i] : a[i][j];
      }
    }
    r_x[c] = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) -
              m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0]) +
              m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])) /
             det;
  }
  return true;
}

/** Field gradient at a sample, with central differences (one-sided at the grid border). */
static float3 sample_gradient(const Grid &grid, const Span<float> field, const int3 p)
{
  float3 g;
  for (const int axis : IndexRange(3)) {
    int3 lo = p;
    int3 hi = p;
    lo[axis] = std::max(0, p[axis] - 1);
    hi[axis] = std::min(grid.dims[axis] - 1, p[axis] + 1);
    const float span = float(hi[axis] - lo[axis]) * grid.cell;
    g[axis] = span > 0.0f ?
                  (field[grid.index(hi.x, hi.y, hi.z)] - field[grid.index(lo.x, lo.y, lo.z)]) /
                      span :
                  0.0f;
  }
  return g;
}

static const int3 CORNER_OFFSETS[8] = {
    {0, 0, 0}, {1, 0, 0}, {0, 1, 0}, {1, 1, 0}, {0, 0, 1}, {1, 0, 1}, {0, 1, 1}, {1, 1, 1}};
/** The 12 cell edges, as pairs of corners differing along one axis. */
static const int CELL_EDGES[12][2] = {{0, 1},
                                      {2, 3},
                                      {4, 5},
                                      {6, 7},
                                      {0, 2},
                                      {1, 3},
                                      {4, 6},
                                      {5, 7},
                                      {0, 4},
                                      {1, 5},
                                      {2, 6},
                                      {3, 7}};

/**
 * Whether the corners of a cell with this inside mask (bit per corner) give dual contouring a
 * single surface disc: the inside corners connected along cell edges, and the outside ones too.
 * Without that the cell's one vertex joins several sheets, making non-manifold edges/vertices.
 */
static bool cell_config_is_manifold(const uint8_t inside_mask)
{
  for (const bool inside : {true, false}) {
    const uint8_t set = inside ? inside_mask : uint8_t(~inside_mask);
    if (set == 0) {
      continue;
    }
    /* Flood the set from its lowest corner. */
    uint8_t reached = set & uint8_t(-int(set));
    bool grew = true;
    while (grew) {
      grew = false;
      for (const auto &edge : CELL_EDGES) {
        const uint8_t a = 1 << edge[0];
        const uint8_t b = 1 << edge[1];
        if ((set & a) && (set & b) && bool(reached & a) != bool(reached & b)) {
          reached |= a | b;
          grew = true;
        }
      }
    }
    if (reached != set) {
      return false;
    }
  }
  return true;
}

/**
 * Makes every cell's sign configuration manifold (see #cell_config_is_manifold) by turning the
 * outside corner nearest the surface inside until it is. Only ever adding inside makes it
 * converge, and it suits the remesher, which merges what touches. Cells sharing samples are
 * never processed at once (even and odd slabs alternate), so the result doesn't depend on
 * threading.
 */
static void make_signs_manifold(const Grid &grid, MutableSpan<float> field, const float min_inside)
{
  static const Array<bool> manifold_configs = []() {
    Array<bool> configs(256);
    for (const int mask : IndexRange(256)) {
      configs[mask] = cell_config_is_manifold(uint8_t(mask));
    }
    return configs;
  }();

  const int3 cells = grid.dims - int3(1);
  bool changed = true;
  while (changed) {
    std::atomic<bool> any_change = false;
    for (const int parity : {0, 1}) {
      const int slabs = (cells.z - parity + 1) / 2;
      threading::parallel_for(IndexRange(slabs), 1, [&](const IndexRange range) {
        for (const int slab : range) {
          const int z = slab * 2 + parity;
          for (int y = 0; y < cells.y; y++) {
            for (int x = 0; x < cells.x; x++) {
              int64_t corners[8];
              uint8_t mask = 0;
              for (const int c : IndexRange(8)) {
                const int3 p = int3(x, y, z) + CORNER_OFFSETS[c];
                corners[c] = grid.index(p.x, p.y, p.z);
                mask |= field[corners[c]] < 0.0f ? (1 << c) : 0;
              }
              while (!manifold_configs[mask]) {
                int nearest = -1;
                for (const int c : IndexRange(8)) {
                  if (!(mask & (1 << c)) &&
                      (nearest == -1 || field[corners[c]] < field[corners[nearest]]))
                  {
                    nearest = c;
                  }
                }
                field[corners[nearest]] = -std::max(field[corners[nearest]], min_inside);
                mask |= 1 << nearest;
                any_change.store(true, std::memory_order_relaxed);
              }
            }
          }
        }
      });
    }
    changed = any_change.load();
  }
}

/**
 * Fills enclosed outside pockets of up to `max_size` samples (bubbles where shells meet, also
 * left by #make_signs_manifold pinching off thin channels). Larger cavities are deliberate
 * (hollow models) and stay.
 */
static void fill_small_pockets(const Grid &grid,
                               MutableSpan<float> field,
                               const float min_inside,
                               const int max_size)
{
  /* #SAMPLE_TMP marks outside samples, #SAMPLE_OUTSIDE those connected to the grid border
   * (reached with the sweeps, which need no memory), #SAMPLE_BLOCK those already in a pocket. */
  Array<uint8_t> state(field.size(), NoInitialization());
  threading::parallel_for(state.index_range(), 1 << 16, [&](const IndexRange range) {
    for (const int64_t i : range) {
      state[i] = field[i] >= 0.0f ? SAMPLE_TMP : 0;
    }
  });
  /* The padding keeps the border outside. */
  state[0] |= SAMPLE_OUTSIDE;
  flood_outside(grid, state, [](const uint8_t s) { return bool(s & SAMPLE_TMP); });

  Vector<int64_t> stack;
  Vector<int64_t> pocket;
  for (const int64_t seed : state.index_range()) {
    if (state[seed] != SAMPLE_TMP) {
      continue;
    }
    pocket.clear();
    state[seed] |= SAMPLE_BLOCK;
    stack.append(seed);
    while (!stack.is_empty()) {
      const int64_t i = stack.pop_last();
      pocket.append(i);
      const int3 p(int(i % grid.dims.x),
                   int((i / grid.dims.x) % grid.dims.y),
                   int(i / (int64_t(grid.dims.x) * grid.dims.y)));
      for (const int axis : IndexRange(3)) {
        for (const int dir : {-1, 1}) {
          int3 n = p;
          n[axis] += dir;
          if (n[axis] < 0 || n[axis] >= grid.dims[axis]) {
            continue;
          }
          const int64_t ni = grid.index(n.x, n.y, n.z);
          if (state[ni] == SAMPLE_TMP) {
            state[ni] |= SAMPLE_BLOCK;
            stack.append(ni);
          }
        }
      }
    }
    if (pocket.size() <= max_size) {
      for (const int64_t i : pocket) {
        field[i] = -std::max(field[i], min_inside);
      }
    }
  }
}

/** Dual contouring vertex of a sign-changing cell, clamped to the cell. */
static float3 cell_vertex(const Grid &grid, const Span<float> field, const int3 cell)
{
  float3 points[12];
  float3 normals[12];
  int num = 0;
  for (const auto &edge : CELL_EDGES) {
    const int3 c0 = cell + CORNER_OFFSETS[edge[0]];
    const int3 c1 = cell + CORNER_OFFSETS[edge[1]];
    const float f0 = field[grid.index(c0.x, c0.y, c0.z)];
    const float f1 = field[grid.index(c1.x, c1.y, c1.z)];
    if ((f0 < 0.0f) == (f1 < 0.0f)) {
      continue;
    }
    const float t = (f0 == f1) ? 0.5f : std::clamp(f0 / (f0 - f1), 0.0f, 1.0f);
    points[num] = math::interpolate(grid.position(c0.x, c0.y, c0.z),
                                    grid.position(c1.x, c1.y, c1.z),
                                    t);
    float3 n = math::interpolate(
        sample_gradient(grid, field, c0), sample_gradient(grid, field, c1), t);
    if (math::length_squared(n) < 1e-20f) {
      /* Flat field (clamped distances): fall back to the edge direction. */
      n = float3(c1 - c0) * (f1 > f0 ? 1.0f : -1.0f);
    }
    normals[num] = math::normalize(n);
    num++;
  }

  float3 mass_point(0.0f);
  for (int i = 0; i < num; i++) {
    mass_point += points[i];
  }
  mass_point /= float(std::max(num, 1));

  /* Minimize the QEF around the mass point; the small regularization pulls under-determined
   * directions (flat areas, edges) towards it instead of letting them run off. */
  const double reg = 0.05;
  double a[3][3] = {{reg, 0, 0}, {0, reg, 0}, {0, 0, reg}};
  double b[3] = {0, 0, 0};
  for (int i = 0; i < num; i++) {
    const float3 &n = normals[i];
    const double d = math::dot(n, points[i] - mass_point);
    for (int r = 0; r < 3; r++) {
      for (int c = 0; c < 3; c++) {
        a[r][c] += double(n[r]) * n[c];
      }
      b[r] += double(n[r]) * d;
    }
  }
  double x[3];
  float3 vert = mass_point;
  if (solve_3x3(a, b, x)) {
    vert += float3(float(x[0]), float(x[1]), float(x[2]));
  }
  /* Unclamped QEF vertices are the classic source of spikes. */
  const float3 lo = grid.position(cell.x, cell.y, cell.z);
  return math::clamp(vert, lo, lo + float3(grid.cell));
}

/**
 * Numbers elements in slab order, independent of threading: `count_fn(z)` counts the elements of
 * slab `z`, then `fill_fn(z, first_index)` writes them. Returns the total.
 */
template<typename CountFn, typename FillFn>
static int slab_prefix(const int slabs, const CountFn &count_fn, const FillFn &fill_fn)
{
  Array<int> offsets(slabs + 1, 0);
  threading::parallel_for(IndexRange(slabs), 1, [&](const IndexRange range) {
    for (const int z : range) {
      offsets[z] = count_fn(z);
    }
  });
  const OffsetIndices<int> slab_offsets = offset_indices::accumulate_counts_to_offsets(offsets);
  threading::parallel_for(IndexRange(slabs), 1, [&](const IndexRange range) {
    for (const int z : range) {
      fill_fn(z, slab_offsets[z].start());
    }
  });
  return slab_offsets.total_size();
}

/**
 * Puts the edges in a fixed order (by their sorted vertex pair): #mesh_calc_edges numbers them
 * depending on threading, and later operations that walk neighbors in edge order would then
 * give slightly different results.
 */
static void sort_edges(Mesh &mesh)
{
  MutableSpan<int2> edges = mesh.edges_for_write();
  Array<int> order(edges.size());
  array_utils::fill_index_range<int>(order);
  const auto key = [&](const int edge) {
    const int2 &e = edges[edge];
    return (uint64_t(uint32_t(std::min(e[0], e[1]))) << 32) | uint32_t(std::max(e[0], e[1]));
  };
  parallel_sort(order.begin(), order.end(), [&](const int a, const int b) {
    return key(a) < key(b);
  });
  Array<int> new_index(edges.size());
  Array<int2> sorted(edges.size());
  threading::parallel_for(order.index_range(), 4096, [&](const IndexRange range) {
    for (const int i : range) {
      const int2 &e = edges[order[i]];
      sorted[i] = int2(std::min(e[0], e[1]), std::max(e[0], e[1]));
      new_index[order[i]] = i;
    }
  });
  edges.copy_from(sorted);
  MutableSpan<int> corner_edges = mesh.corner_edges_for_write();
  threading::parallel_for(corner_edges.index_range(), 4096, [&](const IndexRange range) {
    for (const int corner : range) {
      corner_edges[corner] = new_index[corner_edges[corner]];
    }
  });
}

}  // namespace dynamic_remesh

Mesh *BKE_mesh_remesh_dynamic(const Mesh *mesh,
                              const DynamicRemeshSettings &settings,
                              ReportList *reports,
                              DynamicRemeshStats *r_stats)
{
  using namespace dynamic_remesh;
  DynamicRemeshStats stats;
  double time = BLI_time_now_seconds();
  const auto lap = [&](double &r_time) {
    const double now = BLI_time_now_seconds();
    r_time = now - time;
    time = now;
  };

  /* ---- Stage A: validate and normalize. ---- */

  if (mesh->faces_num == 0) {
    BKE_report(reports, RPT_ERROR, "The mesh is empty, there is nothing to remesh");
    return nullptr;
  }
  const bke::AttributeAccessor attributes = mesh->attributes();
  if (const VArray<float> mask = *attributes.lookup<float>(".sculpt_mask", bke::AttrDomain::Point))
  {
    bool all_masked = true;
    for (const int i : mask.index_range()) {
      if (mask[i] < 1.0f) {
        all_masked = false;
        break;
      }
    }
    if (all_masked) {
      BKE_report(reports, RPT_ERROR, "The mask covers the whole mesh, clear it to remesh");
      return nullptr;
    }
  }

  const Span<float3> positions = mesh->vert_positions();
  const Span<int3> corner_tris = mesh->corner_tris();
  const Span<int> tri_faces = mesh->corner_tri_faces();
  const Span<int> corner_verts = mesh->corner_verts();
  const VArray<bool> hide_poly = *attributes.lookup_or_default<bool>(
      ".hide_poly", bke::AttrDomain::Face, false);

  float3 bb_min(FLT_MAX), bb_max(-FLT_MAX);
  for (const float3 &co : positions) {
    bb_min = math::min(bb_min, co);
    bb_max = math::max(bb_max, co);
  }
  const float diagonal = math::distance(bb_min, bb_max);
  if (!(diagonal > 0.0f)) {
    BKE_report(reports, RPT_ERROR, "The mesh has no area, there is nothing to remesh");
    return nullptr;
  }
  const float min_area = math::square(1e-6f * diagonal);

  IndexMaskMemory memory;
  const IndexMask tri_mask = IndexMask::from_predicate(
      corner_tris.index_range(), memory, [&](const int64_t i) {
        if (hide_poly[tri_faces[i]]) {
          return false;
        }
        const int3 &tri = corner_tris[i];
        return area_tri_v3(positions[corner_verts[tri[0]]],
                           positions[corner_verts[tri[1]]],
                           positions[corner_verts[tri[2]]]) > min_area;
      });
  if (tri_mask.is_empty()) {
    BKE_report(reports,
               RPT_ERROR,
               hide_poly.is_single() ? "The mesh has no area, there is nothing to remesh" :
                                       "Everything is hidden, there is nothing to remesh");
    return nullptr;
  }
  Array<int3> tris(tri_mask.size());
  tri_mask.foreach_index([&](const int64_t i, const int64_t pos) {
    const int3 &tri = corner_tris[i];
    tris[pos] = int3(corner_verts[tri[0]], corner_verts[tri[1]], corner_verts[tri[2]]);
  });

  if (!hide_poly.is_single()) {
    bb_min = float3(FLT_MAX);
    bb_max = float3(-FLT_MAX);
    for (const int3 &tri : tris) {
      for (const int v : {tri[0], tri[1], tri[2]}) {
        bb_min = math::min(bb_min, positions[v]);
        bb_max = math::max(bb_max, positions[v]);
      }
    }
  }

  /* Non-manifold input is remeshed anyway (the field doesn't care), but say so. */
  {
    const Span<int> corner_edges = mesh->corner_edges();
    const OffsetIndices<int> faces = mesh->faces();
    Array<uint8_t> edge_faces(mesh->edges_num, 0);
    bool non_manifold = false;
    for (const int face : faces.index_range()) {
      if (hide_poly[face]) {
        continue;
      }
      for (const int edge : corner_edges.slice(faces[face])) {
        if (edge_faces[edge] < 3 && ++edge_faces[edge] == 3) {
          non_manifold = true;
        }
      }
    }
    if (non_manifold) {
      BKE_report(reports,
                 RPT_WARNING,
                 "The mesh has non-manifold edges, the remesh result may need checking");
    }
  }

  lap(stats.time_validate);

  /* ---- Stage B: narrow-band distance field. ---- */

  const int close_radius = settings.close_holes ? std::clamp(settings.close_radius_cells, 1, 8) :
                                                  0;
  const int band_cells = std::max(settings.band_cells, 2);
  /* The grid border must stay clear of the (dilated) barrier so the outside flood can start. */
  const int pad = 2 + close_radius;
  const float3 bb_size = bb_max - bb_min;
  Grid grid;
  grid.cell = std::max(settings.target_edge_length, diagonal * 1e-5f);
  const auto grid_dims = [&](const float cell) {
    return int3(math::ceil((bb_size + float3(2.0f * pad * cell)) / cell)) + int3(1);
  };
  grid.dims = grid_dims(grid.cell);
  const int max_grid = std::max(settings.max_grid, 2 * pad + 8);
  if (math::reduce_max(grid.dims) > max_grid) {
    grid.cell = math::reduce_max(bb_size) / float(max_grid - 2 * pad - 2);
    grid.dims = grid_dims(grid.cell);
    while (math::reduce_max(grid.dims) > max_grid) {
      grid.cell *= 1.01f;
      grid.dims = grid_dims(grid.cell);
    }
    BKE_reportf(reports,
                RPT_WARNING,
                "Remesh resolution limited by memory: edge length raised to %.4g",
                grid.cell);
  }
  grid.origin = bb_min - float3(pad * grid.cell);
  stats.cell_size = grid.cell;
  stats.grid_dims[0] = grid.dims.x;
  stats.grid_dims[1] = grid.dims.y;
  stats.grid_dims[2] = grid.dims.z;

  const int64_t samples_num = grid.size();
  Array<uint64_t> keys(samples_num, NoInitialization());
  threading::parallel_for(keys.index_range(), 1 << 16, [&](const IndexRange range) {
    keys.as_mutable_span().slice(range).fill(KEY_EMPTY);
  });

  const float band = band_cells * grid.cell;
  const float band_sq = band * band;
  threading::parallel_for(tris.index_range(), 256, [&](const IndexRange range) {
    for (const int64_t t : range) {
      const float3 &v0 = positions[tris[t][0]];
      const float3 &v1 = positions[tris[t][1]];
      const float3 &v2 = positions[tris[t][2]];
      const float3 lo = (math::min(v0, math::min(v1, v2)) - float3(band) - grid.origin) /
                        grid.cell;
      const float3 hi = (math::max(v0, math::max(v1, v2)) + float3(band) - grid.origin) /
                        grid.cell;
      const int3 imin = math::max(int3(math::ceil(lo)), int3(0));
      const int3 imax = math::min(int3(math::floor(hi)), grid.dims - int3(1));
      for (int z = imin.z; z <= imax.z; z++) {
        for (int y = imin.y; y <= imax.y; y++) {
          for (int x = imin.x; x <= imax.x; x++) {
            const float3 p = grid.position(x, y, z);
            float3 closest;
            closest_on_tri_to_point_v3(closest, p, v0, v1, v2);
            const float dist_sq = math::distance_squared(p, closest);
            if (dist_sq <= band_sq) {
              atomic_min(keys[grid.index(x, y, z)], band_key(dist_sq, int(t)));
            }
          }
        }
      }
    }
  });

  lap(stats.time_field);

  /* ---- Stage D: inside/outside, with morphological closing. ---- */

  const Span<float3> vert_normals = mesh->vert_normals();
  /* Every sample in a cell the surface passes through is within half a cell diagonal of it, so
   * the barrier is a closed shell the flood can't leak through. */
  const float barrier = grid.cell * 0.87f;
  const float barrier_sq = barrier * barrier;
  const float on_surface_sq = math::square(1e-4f * grid.cell);
  Array<uint8_t> state(samples_num, NoInitialization());
  /* For barrier samples facing out: the axis directions (bits for -x, +x, -y, +y, -z, +z) that
   * lead away from the surface, the only ones the outside may reach them from. */
  Array<uint8_t> away_dirs(samples_num, NoInitialization());
  threading::parallel_for(IndexRange(grid.dims.z), 1, [&](const IndexRange range) {
    for (const int z : range) {
      for (int y = 0; y < grid.dims.y; y++) {
        for (int x = 0; x < grid.dims.x; x++) {
          const int64_t i = grid.index(x, y, z);
          const uint64_t key = keys[i];
          uint8_t s = 0;
          uint8_t dirs = 0;
          if (key != KEY_EMPTY && key_dist_sq(key) <= barrier_sq) {
            s = SAMPLE_BARRIER | SAMPLE_BLOCK;
            const int3 &tri = tris[key_tri(key)];
            const float3 p = grid.position(x, y, z);
            float3 closest, bary;
            closest_on_tri_to_point_v3(
                closest, bary, p, positions[tri[0]], positions[tri[1]], positions[tri[2]]);
            float3 normal;
            if (math::reduce_min(bary) > 1e-4f) {
              /* Inside the triangle: its own normal is exact. */
              normal_tri_v3(normal, positions[tri[0]], positions[tri[1]], positions[tri[2]]);
            }
            else {
              /* On an edge or corner: the angle-weighted vertex normals stand in for the
               * pseudo-normal there. */
              normal = vert_normals[tri[0]] * bary[0] + vert_normals[tri[1]] * bary[1] +
                       vert_normals[tri[2]] * bary[2];
            }
            /* Samples on the surface (common when faces line up with the grid) count as inside:
             * deciding them by rounding noise makes checkerboard grid faces, and those make
             * non-manifold edges. */
            if (key_dist_sq(key) > on_surface_sq && math::dot(p - closest, normal) > 0.0f) {
              s |= SAMPLE_FACES_OUT;
              const float3 away = math::normalize(p - closest);
              for (const int axis : IndexRange(3)) {
                if (away[axis] < -0.25f) {
                  dirs |= 1 << (axis * 2);
                }
                else if (away[axis] > 0.25f) {
                  dirs |= 1 << (axis * 2 + 1);
                }
              }
            }
          }
          state[i] = s;
          away_dirs[i] = dirs;
        }
      }
    }
  });

  dilate(grid, state, SAMPLE_BLOCK, close_radius);

  /* Seed the flood with the grid border, which the padding keeps clear of the block. */
  for (const int axis : IndexRange(3)) {
    foreach_line(grid, axis, [&](const int64_t start, const int64_t step, const int count) {
      for (const int64_t i : {start, start + (count - 1) * step}) {
        if (!(state[i] & SAMPLE_BLOCK)) {
          state[i] |= SAMPLE_OUTSIDE;
        }
      }
    });
  }
  flood_outside(grid, state, [](const uint8_t s) { return !(s & SAMPLE_BLOCK); });
  /* Erode the solid back (by growing the outside) so closing doesn't thicken the model. It
   * stops short of the barrier, which is resolved below. */
  dilate(grid, state, SAMPLE_OUTSIDE, close_radius);
  threading::parallel_for(state.index_range(), 1 << 16, [&](const IndexRange range) {
    for (const int64_t i : range) {
      if (state[i] & SAMPLE_BARRIER) {
        state[i] &= ~SAMPLE_OUTSIDE;
      }
    }
  });
  /* Barrier samples facing out are outside only when the outside is right next to them, on the
   * side they face: faces buried in overlapping shells don't make cavities, the shells merge
   * (where shells meet, samples inside one shell can face out of the other's buried faces, but
   * towards the inside). Two steps cross any barrier layer (each step along the best axis gains
   * at least cell / sqrt(3) of distance). */
  for ([[maybe_unused]] const int step : IndexRange(2)) {
    threading::parallel_for(IndexRange(grid.dims.z), 1, [&](const IndexRange range) {
      for (const int z : range) {
        for (int y = 0; y < grid.dims.y; y++) {
          for (int x = 0; x < grid.dims.x; x++) {
            const int64_t i = grid.index(x, y, z);
            if ((state[i] & (SAMPLE_BARRIER | SAMPLE_FACES_OUT | SAMPLE_OUTSIDE)) !=
                (SAMPLE_BARRIER | SAMPLE_FACES_OUT))
            {
              continue;
            }
            const int3 p(x, y, z);
            for (const int axis : IndexRange(3)) {
              for (const int dir : {-1, 1}) {
                if (!(away_dirs[i] & (1 << (axis * 2 + (dir == 1))))) {
                  continue;
                }
                int3 n = p;
                n[axis] += dir;
                if (n[axis] >= 0 && n[axis] < grid.dims[axis] &&
                    (state[grid.index(n.x, n.y, n.z)] & SAMPLE_OUTSIDE))
                {
                  state[i] |= SAMPLE_TMP;
                }
              }
            }
          }
        }
      }
    });
    threading::parallel_for(state.index_range(), 1 << 16, [&](const IndexRange range) {
      for (const int64_t i : range) {
        if (state[i] & SAMPLE_TMP) {
          state[i] = (state[i] & ~SAMPLE_TMP) | SAMPLE_OUTSIDE;
        }
      }
    });
  }

  const float min_inside = 1e-6f * grid.cell;
  Array<float> field(samples_num, NoInitialization());
  threading::parallel_for(field.index_range(), 1 << 16, [&](const IndexRange range) {
    for (const int64_t i : range) {
      const float dist = keys[i] == KEY_EMPTY ? band : std::sqrt(key_dist_sq(keys[i]));
      /* Inside must stay negative on the surface too (-0 would read as outside). */
      field[i] = (state[i] & SAMPLE_OUTSIDE) ? dist : -std::max(dist, min_inside);
    }
  });
  keys = {};
  state = {};
  away_dirs = {};

  make_signs_manifold(grid, field, min_inside);
  fill_small_pockets(grid, field, min_inside, 512);

  lap(stats.time_sign);

  /* ---- Stage C: dual contouring. ---- */

  const int3 cells = grid.dims - int3(1);
  const auto inside = [&](const int x, const int y, const int z) {
    return field[grid.index(x, y, z)] < 0.0f;
  };
  const auto cell_is_mixed = [&](const int x, const int y, const int z) {
    const bool first = inside(x, y, z);
    for (const int3 &o : CORNER_OFFSETS) {
      if (inside(x + o.x, y + o.y, z + o.z) != first) {
        return true;
      }
    }
    return false;
  };

  /* Indexed by the cell's lower sample (the last z slice has no cells and stays unset). */
  Array<int> cell_verts(samples_num, NoInitialization());
  const int verts_num = slab_prefix(
      cells.z,
      [&](const int z) {
        int count = 0;
        for (int y = 0; y < cells.y; y++) {
          for (int x = 0; x < cells.x; x++) {
            count += cell_is_mixed(x, y, z);
          }
        }
        return count;
      },
      [&](const int z, int index) {
        for (int y = 0; y < grid.dims.y; y++) {
          for (int x = 0; x < grid.dims.x; x++) {
            const bool mixed = x < cells.x && y < cells.y && cell_is_mixed(x, y, z);
            cell_verts[grid.index(x, y, z)] = mixed ? index++ : -1;
          }
        }
      });
  if (verts_num == 0) {
    BKE_report(reports, RPT_ERROR, "Remesh produced no surface, try a smaller edge length");
    return nullptr;
  }

  /* Quads across the sign-changing grid edges, oriented so their normals point outwards. Along
   * `axis`, the four cells around an edge are ordered so (u, v) is right-handed with it. */
  const int3 axis_u[3] = {{0, 1, 0}, {0, 0, 1}, {1, 0, 0}};
  const int3 axis_v[3] = {{0, 0, 1}, {1, 0, 0}, {0, 1, 0}};
  const int3 axis_dir[3] = {{1, 0, 0}, {0, 1, 0}, {0, 0, 1}};
  const auto edge_quad = [&](const int axis, const int x, const int y, const int z, int r_quad[4]) {
    const int3 p(x, y, z);
    const int3 q = p + axis_dir[axis];
    if (q[axis] >= grid.dims[axis]) {
      return false;
    }
    const int3 u = axis_u[axis];
    const int3 v = axis_v[axis];
    /* The four cells around the edge must exist. */
    if (math::dot(p, u) < 1 || math::dot(p, v) < 1 || math::dot(p, u) >= math::dot(cells, u) ||
        math::dot(p, v) >= math::dot(cells, v))
    {
      return false;
    }
    const bool in0 = inside(p.x, p.y, p.z);
    if (in0 == inside(q.x, q.y, q.z)) {
      return false;
    }
    if (r_quad) {
      const int3 c[4] = {p - u - v, p - v, p, p - u};
      for (int i = 0; i < 4; i++) {
        r_quad[i] = cell_verts[grid.index(c[i].x, c[i].y, c[i].z)];
      }
      if (!in0) {
        std::swap(r_quad[1], r_quad[3]);
      }
    }
    return true;
  };

  Array<int> quad_verts;
  const int faces_num = [&]() {
    Array<int> counts(grid.dims.z + 1, 0);
    threading::parallel_for(IndexRange(grid.dims.z), 1, [&](const IndexRange range) {
      for (const int z : range) {
        int count = 0;
        for (int y = 0; y < grid.dims.y; y++) {
          for (int x = 0; x < grid.dims.x; x++) {
            for (const int axis : IndexRange(3)) {
              count += edge_quad(axis, x, y, z, nullptr);
            }
          }
        }
        counts[z] = count;
      }
    });
    const OffsetIndices<int> offsets = offset_indices::accumulate_counts_to_offsets(counts);
    quad_verts.reinitialize(int64_t(offsets.total_size()) * 4);
    threading::parallel_for(IndexRange(grid.dims.z), 1, [&](const IndexRange range) {
      for (const int z : range) {
        int face = offsets[z].start();
        for (int y = 0; y < grid.dims.y; y++) {
          for (int x = 0; x < grid.dims.x; x++) {
            for (const int axis : IndexRange(3)) {
              if (edge_quad(axis, x, y, z, &quad_verts[int64_t(face) * 4])) {
                face++;
              }
            }
          }
        }
      }
    });
    return offsets.total_size();
  }();

  Mesh *result = BKE_mesh_new_nomain(verts_num, 0, faces_num, faces_num * 4);
  BKE_mesh_copy_parameters(result, mesh);
  MutableSpan<float3> result_positions = result->vert_positions_for_write();
  threading::parallel_for(IndexRange(cells.z), 1, [&](const IndexRange range) {
    for (const int z : range) {
      for (int y = 0; y < cells.y; y++) {
        for (int x = 0; x < cells.x; x++) {
          const int vert = cell_verts[grid.index(x, y, z)];
          if (vert != -1) {
            result_positions[vert] = cell_vertex(grid, field, int3(x, y, z));
          }
        }
      }
    }
  });
  cell_verts = {};
  field = {};
  offset_indices::fill_constant_group_size(4, 0, result->face_offsets_for_write());
  result->corner_verts_for_write().copy_from(quad_verts);
  quad_verts = {};
  bke::mesh_calc_edges(*result, false, false);
  sort_edges(*result);

  lap(stats.time_extract);

  /* ---- Stage E: detail projection, and the error against the source. ---- */

  BVHTree *tree = BLI_bvhtree_new(int(tris.size()), 0.0f, 4, 6);
  for (const int64_t t : tris.index_range()) {
    const float3 co[3] = {
        positions[tris[t][0]], positions[tris[t][1]], positions[tris[t][2]]};
    BLI_bvhtree_insert(tree, int(t), &co[0].x, 3);
  }
  BLI_bvhtree_balance(tree);
  BVHUserData bvh_data{positions, tris};

  if (settings.project) {
    const Array<float3> normals(result->vert_normals());
    /* Vertices farther than this are on patches closing a hole; they keep their place. */
    const float max_dist = 2.0f * grid.cell;
    const float crease_lo = std::clamp(settings.crease_threshold, 0.0f, 2.0f);
    const float crease_hi = std::min(2.0f * crease_lo, 2.0f);
    threading::parallel_for(result_positions.index_range(), 512, [&](const IndexRange range) {
      for (const int64_t i : range) {
        BVHTreeNearest nearest;
        nearest.index = -1;
        nearest.dist_sq = max_dist * max_dist;
        BLI_bvhtree_find_nearest(tree, result_positions[i], &nearest, nearest_tri_cb, &bvh_data);
        if (nearest.index == -1) {
          continue;
        }
        /* Full projection where the normals agree, none across a crease. */
        const float diff = math::distance(normals[i], float3(nearest.no));
        float weight = 1.0f;
        if (diff >= crease_hi) {
          weight = 0.0f;
        }
        else if (diff > crease_lo) {
          const float t = (diff - crease_lo) / (crease_hi - crease_lo);
          weight = 1.0f - t * t * (3.0f - 2.0f * t);
        }
        result_positions[i] = math::interpolate(result_positions[i], float3(nearest.co), weight);
      }
    });
    result->tag_positions_changed();
  }

  /* Mean and 95th percentile point-to-surface distance, over up to 50k evenly spread vertices
   * (vertex order follows the grid, so even spacing is spatially stratified). */
  {
    const int samples = std::min(50000, verts_num);
    Array<float> errors(samples);
    threading::parallel_for(errors.index_range(), 512, [&](const IndexRange range) {
      for (const int64_t s : range) {
        const int vert = int(int64_t(s) * verts_num / samples);
        BVHTreeNearest nearest;
        nearest.index = -1;
        nearest.dist_sq = FLT_MAX;
        BLI_bvhtree_find_nearest(
            tree, result_positions[vert], &nearest, nearest_tri_cb, &bvh_data);
        errors[s] = nearest.index == -1 ? 0.0f : std::sqrt(nearest.dist_sq);
      }
    });
    double sum = 0.0;
    for (const float e : errors) {
      sum += e;
    }
    stats.error_mean = float(sum / samples);
    const int p95 = std::min(samples - 1, int(samples * 0.95f));
    std::nth_element(errors.begin(), errors.begin() + p95, errors.end());
    stats.error_p95 = errors[p95];
  }
  BLI_bvhtree_free(tree);

  lap(stats.time_project);

  if (r_stats) {
    *r_stats = stats;
  }
  return result;
}

}  // namespace blender
