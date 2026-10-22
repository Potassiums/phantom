/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include "autoremesher_capi.hh"

#include <AutoRemesher/AutoRemesher>

#include <exception>

namespace autoremesher {

namespace {

/** Thrown from the progress handler to unwind out of the remesher when canceled. */
struct CancelException : std::exception {};

struct ProgressContext {
  const ProgressFn *fn;
};

void progress_handler(void *tag, const float progress, const char *status)
{
  const ProgressContext *context = static_cast<const ProgressContext *>(tag);
  if (*context->fn && !(*context->fn)(progress, status)) {
    throw CancelException();
  }
}

}  // namespace

Status remesh(const std::vector<std::array<float, 3>> &positions,
              const std::vector<std::array<int, 3>> &triangles,
              const Params &params,
              const ProgressFn &progress,
              Result &r_result)
{
  std::vector<AutoRemesher::Vector3> vertices;
  vertices.reserve(positions.size());
  for (const std::array<float, 3> &co : positions) {
    vertices.emplace_back(co[0], co[1], co[2]);
  }
  std::vector<std::vector<size_t>> tris;
  tris.reserve(triangles.size());
  for (const std::array<int, 3> &tri : triangles) {
    tris.push_back({size_t(tri[0]), size_t(tri[1]), size_t(tri[2])});
  }

  ProgressContext context{&progress};
  try {
    AutoRemesher::AutoRemesher remesher(vertices, tris);
    if (params.target_quads > 0) {
      /* The AutoRemesher app asks for two triangles per target quad as well. */
      remesher.setTargetTriangleCount(size_t(params.target_quads) * 2);
    }
    if (params.edge_scaling > 0.0f) {
      remesher.setScaling(params.edge_scaling);
    }
    remesher.setModelType(params.hard_surface ? AutoRemesher::ModelType::HardSurface :
                                                AutoRemesher::ModelType::Organic);
    remesher.setGradientAdaptivity(params.adaptivity);
    remesher.setAnisotropy(params.anisotropy);
    remesher.setSharpEdgeDegrees(params.sharp_edge_degrees);
    remesher.setSmoothNormalDegrees(params.smooth_normal_degrees);
    remesher.setTag(&context);
    remesher.setProgressHandler(progress_handler);

    if (!remesher.remesh()) {
      return Status::Failed;
    }

    const std::vector<AutoRemesher::Vector3> &out_vertices = remesher.remeshedVertices();
    const std::vector<std::vector<size_t>> &out_faces = remesher.remeshedQuads();
    r_result.positions.clear();
    r_result.face_sizes.clear();
    r_result.corner_verts.clear();
    r_result.positions.reserve(out_vertices.size());
    for (const AutoRemesher::Vector3 &v : out_vertices) {
      r_result.positions.push_back({float(v.x()), float(v.y()), float(v.z())});
    }
    for (const std::vector<size_t> &face : out_faces) {
      if (face.size() < 3) {
        continue;
      }
      r_result.face_sizes.push_back(int(face.size()));
      for (const size_t v : face) {
        r_result.corner_verts.push_back(int(v));
      }
    }
  }
  catch (const CancelException &) {
    return Status::Canceled;
  }
  catch (const std::exception &) {
    return Status::Failed;
  }

  return r_result.face_sizes.empty() ? Status::Failed : Status::Success;
}

}  // namespace autoremesher
