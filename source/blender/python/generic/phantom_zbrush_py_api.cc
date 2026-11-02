/* SPDX-FileCopyrightText: 2026 Phantom Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup pygen
 *
 * Phantom: the `_phantom_zbrush` module, the hot loops of the ZBrush (.ztl / .zpr) importer in
 * `scripts/modules/phantom_zbrush_io.py`:
 * - `decode_array`: a ZBrush array is a `.mem` container of independent blocks, each byte-plane
 *   RLE (or half-float) coded; decoding them in Python took minutes on large projects, here the
 *   blocks decode in parallel. The layout is documented in the Python module, which keeps a pure
 *   Python fallback with the same results.
 * - `mesh_set_faces`: faces and corners of meshes with tens of millions of corners.
 * - `multires_add_levels`: all Multires levels at once, for the top level to be reshaped.
 */

#include <Python.h>

#include <algorithm>
#include <cstring>
#include <string>
#include <vector>

#include "BLI_math_half.hh"
#include "BLI_task.hh"
#include "BLI_utildefines.hh"

#include "DNA_customdata_types.h"
#include "DNA_mesh_types.h"
#include "DNA_modifier_types.h"
#include "DNA_object_types.h"

#include "BKE_customdata.hh"
#include "BKE_modifier.hh"
#include "BKE_multires.hh"

#include "phantom_zbrush_py_api.hh"
#include "py_capi_utils.hh"

namespace blender {

/* -------------------------------------------------------------------- */
/** \name Decoding
 * \{ */

static constexpr double INT_DEQUANT = 1.0 / double(1 << 20);

static bool read_u32(const uint8_t *data, const size_t size, const size_t offset, uint32_t *r_value)
{
  if (offset > size || size - offset < 4) {
    return false;
  }
  std::memcpy(r_value, data + offset, 4);
  return true;
}

static bool is_rle_tag(const uint32_t tag)
{
  return ELEM(tag, 1, 4, 5, 6);
}

/** PackBits variant: 0 ends, 1..127 repeats the next byte, 0x80..0xFF copies 256 - c bytes. */
static size_t rle_decode(const uint8_t *src, const size_t n, uint8_t *out, const size_t expected)
{
  size_t i = 0;
  size_t o = 0;
  while (i < n && o < expected) {
    const uint8_t c = src[i++];
    if (c == 0) {
      break;
    }
    if (c < 0x80) {
      if (i >= n) {
        break;
      }
      const size_t count = std::min<size_t>(c, expected - o);
      std::memset(out + o, src[i], count);
      o += count;
      i++;
    }
    else {
      const size_t count = 256 - c;
      const size_t copy = std::min(std::min(count, n - i), expected - o);
      std::memcpy(out + o, src + i, copy);
      o += copy;
      i += count;
    }
  }
  return o;
}

struct Block {
  uint32_t tag;
  const uint8_t *payload;
  size_t size;
  std::vector<uint8_t> out;
};

/**
 * Decode the `.mem` container at `offset` to `byte_len` bytes of element-major data in `r_data`.
 * On failure returns false with a message in `r_error`.
 */
static bool decode_mem(const uint8_t *data,
                       const size_t size,
                       const size_t offset,
                       const size_t stride,
                       const size_t byte_len,
                       uint8_t *r_data,
                       std::string &r_error)
{
  uint32_t block_size = 0;
  if (!read_u32(data, size, offset + 4, &block_size)) {
    r_error = "truncated .mem header";
    return false;
  }
  std::vector<uint32_t> sizes;
  size_t p = offset + 12;
  while (true) {
    uint32_t s;
    if (!read_u32(data, size, p, &s)) {
      r_error = "truncated .mem block table";
      return false;
    }
    p += 4;
    if (s == 0 || sizes.size() > 1000000) {
      break;
    }
    sizes.push_back(s);
  }

  std::vector<Block> blocks(sizes.size());
  for (const int i : IndexRange(sizes.size())) {
    Block &block = blocks[i];
    if (!read_u32(data, size, p, &block.tag)) {
      r_error = "truncated .mem block";
      return false;
    }
    const size_t begin = std::min(p + 4, size);
    const size_t end = std::min(std::max(p + size_t(sizes[i]), begin), size);
    block.payload = data + begin;
    block.size = end - begin;
    p += sizes[i];
    /* Some blocks carry a u32 prefix before the codec tag. */
    if (!is_rle_tag(block.tag) && block.tag != 3 && block.size >= 8) {
      uint32_t alt;
      std::memcpy(&alt, block.payload, 4);
      if (is_rle_tag(alt) || alt == 3) {
        block.tag = alt;
        block.payload += 4;
        block.size -= 4;
      }
    }
    if (!is_rle_tag(block.tag) && block.tag != 3) {
      r_error = "codec tag " + std::to_string(block.tag);
      return false;
    }
  }

  threading::parallel_for(IndexRange(blocks.size()), 1, [&](const IndexRange range) {
    std::vector<uint8_t> plane;
    for (const int i : range) {
      Block &block = blocks[i];
      if (block.tag == 3) {
        uint32_t count = 0;
        if (block.size >= 4) {
          std::memcpy(&count, block.payload, 4);
        }
        count = uint32_t(std::min<size_t>(count, block.size >= 4 ? (block.size - 4) / 2 : 0));
        block.out.resize(size_t(count) * 4);
        float *dst = reinterpret_cast<float *>(block.out.data());
        for (const uint32_t e : IndexRange(count)) {
          uint16_t h;
          std::memcpy(&h, block.payload + 4 + e * 2, 2);
          dst[e] = math::half_to_float(h);
        }
        continue;
      }
      plane.resize(block_size);
      const size_t len = rle_decode(block.payload, block.size, plane.data(), block_size);
      if (stride <= 1) {
        block.out.assign(plane.begin(), plane.begin() + len);
        continue;
      }
      /* Byte-plane transpose per block: dst[lane + e * stride] = src[lane * n + e]. */
      const size_t n = len / stride;
      block.out.resize(n * stride);
      for (const size_t lane : IndexRange(stride)) {
        const uint8_t *src = plane.data() + lane * n;
        uint8_t *dst = block.out.data() + lane;
        for (size_t e = 0; e < n; e++) {
          dst[e * stride] = src[e];
        }
      }
    }
  });

  size_t total = 0;
  bool quantised = false;
  for (const Block &block : blocks) {
    total += block.out.size();
    quantised = quantised || ELEM(block.tag, 4, 5, 6);
  }
  if (quantised && (stride % 4 || total % 4)) {
    r_error = "int dequant needs 4-byte alignment (stride " + std::to_string(stride) + ")";
    return false;
  }
  if (total < byte_len) {
    r_error = "short decode " + std::to_string(total) + " < " + std::to_string(byte_len);
    return false;
  }
  size_t o = 0;
  for (const Block &block : blocks) {
    if (o >= byte_len) {
      break;
    }
    const size_t copy = std::min(block.out.size(), byte_len - o);
    std::memcpy(r_data + o, block.out.data(), copy);
    o += copy;
  }
  if (quantised) {
    /* Fixed point int32 * 2^-20, in place. */
    const size_t count = byte_len / 4;
    threading::parallel_for(IndexRange(count), 1 << 16, [&](const IndexRange range) {
      for (const int64_t e : range) {
        int32_t v;
        std::memcpy(&v, r_data + e * 4, 4);
        const float f = float(v) * float(INT_DEQUANT);
        std::memcpy(r_data + e * 4, &f, 4);
      }
    });
  }
  return true;
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Python Functions
 * \{ */

PyDoc_STRVAR(
    /* Wrap. */
    py_phantom_zbrush_decode_array_doc,
    ".. function:: decode_array(buffer, offset, stride, byte_len)\n"
    "\n"
    "   Decode the ZBrush ``.mem`` container at ``offset`` of ``buffer`` (an ``mmap`` or\n"
    "   ``bytes`` of the whole file) to ``byte_len`` bytes of element-major data.\n"
    "\n"
    "   :raises ValueError: for unsupported codecs or truncated data.\n"
    "   :rtype: bytes\n");
static PyObject *py_phantom_zbrush_decode_array(PyObject * /*self*/, PyObject *args)
{
  PyObject *buffer_ob;
  Py_ssize_t offset, stride, byte_len;
  if (!PyArg_ParseTuple(args, "Onnn:decode_array", &buffer_ob, &offset, &stride, &byte_len)) {
    return nullptr;
  }
  if (offset < 0 || stride <= 0 || byte_len < 0) {
    PyErr_SetString(PyExc_ValueError, "decode_array: invalid offset, stride or length");
    return nullptr;
  }
  Py_buffer view;
  if (PyObject_GetBuffer(buffer_ob, &view, PyBUF_SIMPLE) == -1) {
    return nullptr;
  }
  /* Decode straight into the result: nothing else refers to it until it is returned. */
  PyObject *result = PyBytes_FromStringAndSize(nullptr, byte_len);
  if (result == nullptr) {
    PyBuffer_Release(&view);
    return nullptr;
  }
  uint8_t *dst = reinterpret_cast<uint8_t *>(PyBytes_AS_STRING(result));
  std::string error;
  bool ok;
  Py_BEGIN_ALLOW_THREADS;
  ok = decode_mem(static_cast<const uint8_t *>(view.buf),
                  size_t(view.len),
                  size_t(offset),
                  size_t(stride),
                  size_t(byte_len),
                  dst,
                  error);
  Py_END_ALLOW_THREADS;
  PyBuffer_Release(&view);
  if (!ok) {
    Py_DECREF(result);
    PyErr_SetString(PyExc_ValueError, error.c_str());
    return nullptr;
  }
  return result;
}

PyDoc_STRVAR(
    /* Wrap. */
    py_phantom_zbrush_multires_add_levels_doc,
    ".. function:: multires_add_levels(object, modifier, levels)\n"
    "\n"
    "   Give a new Multires modifier (no levels yet) ``levels`` levels at once, without\n"
    "   displacement: the top level is the base mesh's limit surface, to be shaped with\n"
    "   ``bpy.ops.object.multires_reshape``. ``multires_subdivide`` adds one level per call and\n"
    "   smooths every level it adds, which an import that reshapes the top level doesn't need.\n"
    "   Tag the object for an update afterwards.\n"
    "\n"
    "   :param object: The mesh object.\n"
    "   :type object: :class:`bpy.types.Object`\n"
    "   :param modifier: The name of its Multires modifier.\n"
    "   :type modifier: str\n"
    "   :param levels: The number of levels.\n"
    "   :type levels: int\n");
static PyObject *py_phantom_zbrush_multires_add_levels(PyObject * /*self*/, PyObject *args)
{
  PyObject *py_object;
  const char *modifier_name;
  int levels;
  if (!PyArg_ParseTuple(
          args, "Osi:multires_add_levels", &py_object, &modifier_name, &levels))
  {
    return nullptr;
  }
  Object *object = static_cast<Object *>(PyC_RNA_AsPointer(py_object, "Object"));
  if (object == nullptr) {
    return nullptr;
  }
  if (object->type != OB_MESH) {
    PyErr_SetString(PyExc_TypeError, "multires_add_levels: expected a mesh object");
    return nullptr;
  }
  ModifierData *md = BKE_modifiers_findby_name(object, modifier_name);
  if (md == nullptr || md->type != eModifierType_Multires) {
    PyErr_Format(PyExc_ValueError, "multires_add_levels: no Multires modifier \"%s\"", modifier_name);
    return nullptr;
  }
  MultiresModifierData *mmd = reinterpret_cast<MultiresModifierData *>(md);
  const Mesh *mesh = id_cast<const Mesh *>(object->data);
  if (mmd->totlvl != 0 || CustomData_has_layer(&mesh->corner_data, CD_MDISPS)) {
    PyErr_SetString(PyExc_ValueError, "multires_add_levels: the modifier already has levels");
    return nullptr;
  }
  if (levels < 1 || levels > 16) {
    PyErr_SetString(PyExc_ValueError, "multires_add_levels: levels must be 1 to 16");
    return nullptr;
  }
  /* Without displacement yet this only allocates the grids of the top level. */
  multiresModifier_subdivide_to_level(object, mmd, levels, MultiresSubdivideModeType::CatmullClark);
  Py_RETURN_NONE;
}

/** Get a buffer of exactly `count` int32 values, or raise. */
static bool get_int_buffer(PyObject *buffer_ob, const int64_t count, const char *what, Py_buffer *r_view)
{
  if (PyObject_GetBuffer(buffer_ob, r_view, PyBUF_SIMPLE) == -1) {
    return false;
  }
  if (r_view->len != Py_ssize_t(count * sizeof(int))) {
    PyErr_Format(PyExc_ValueError,
                 "mesh_set_faces: %s needs %zd int32 values, got %zd bytes",
                 what,
                 Py_ssize_t(count),
                 r_view->len);
    PyBuffer_Release(r_view);
    return false;
  }
  return true;
}

PyDoc_STRVAR(
    /* Wrap. */
    py_phantom_zbrush_mesh_set_faces_doc,
    ".. function:: mesh_set_faces(mesh, loop_start, corner_verts)\n"
    "\n"
    "   Set the faces of a mesh whose faces and corners were added (``polygons.add``,\n"
    "   ``loops.add``) from int32 buffers: each face's first corner and each corner's vertex.\n"
    "   ``foreach_set`` takes seconds for tens of millions of corners. Call\n"
    "   ``mesh.update(calc_edges=True)`` afterwards.\n");
static PyObject *py_phantom_zbrush_mesh_set_faces(PyObject * /*self*/, PyObject *args)
{
  PyObject *py_mesh, *py_loop_start, *py_corner_verts;
  if (!PyArg_ParseTuple(args, "OOO:mesh_set_faces", &py_mesh, &py_loop_start, &py_corner_verts))
  {
    return nullptr;
  }
  Mesh *mesh = static_cast<Mesh *>(PyC_RNA_AsPointer(py_mesh, "Mesh"));
  if (mesh == nullptr) {
    return nullptr;
  }
  Py_buffer starts_view, verts_view;
  if (!get_int_buffer(py_loop_start, mesh->faces_num, "loop_start", &starts_view)) {
    return nullptr;
  }
  if (!get_int_buffer(py_corner_verts, mesh->corners_num, "corner_verts", &verts_view)) {
    PyBuffer_Release(&starts_view);
    return nullptr;
  }
  const Span<int> starts(static_cast<const int *>(starts_view.buf), mesh->faces_num);
  const Span<int> verts(static_cast<const int *>(verts_view.buf), mesh->corners_num);
  /* Check first, so a mistake raises instead of leaving a mesh other code can't handle. */
  const char *error = nullptr;
  for (const int64_t i : starts.index_range()) {
    const int next = i + 1 < starts.size() ? starts[i + 1] : mesh->corners_num;
    if (starts[i] < 0 || starts[i] >= next) {
      error = "mesh_set_faces: loop_start must increase from 0 to below the corner count";
      break;
    }
  }
  if (error == nullptr && !starts.is_empty() && starts[0] != 0) {
    error = "mesh_set_faces: loop_start must start at 0";
  }
  for (const int v : verts) {
    if (error != nullptr) {
      break;
    }
    if (v < 0 || v >= mesh->verts_num) {
      error = "mesh_set_faces: corner vertex out of range";
    }
  }
  if (error == nullptr && mesh->faces_num > 0) {
    MutableSpan<int> offsets = mesh->face_offsets_for_write();
    offsets.drop_back(1).copy_from(starts);
    offsets.last() = mesh->corners_num;
    mesh->corner_verts_for_write().copy_from(verts);
    mesh->tag_topology_changed();
  }
  PyBuffer_Release(&starts_view);
  PyBuffer_Release(&verts_view);
  if (error != nullptr) {
    PyErr_SetString(PyExc_ValueError, error);
    return nullptr;
  }
  Py_RETURN_NONE;
}

static PyMethodDef M_phantom_zbrush_methods[] = {
    {"mesh_set_faces",
     static_cast<PyCFunction>(py_phantom_zbrush_mesh_set_faces),
     METH_VARARGS,
     py_phantom_zbrush_mesh_set_faces_doc},
    {"decode_array",
     static_cast<PyCFunction>(py_phantom_zbrush_decode_array),
     METH_VARARGS,
     py_phantom_zbrush_decode_array_doc},
    {"multires_add_levels",
     static_cast<PyCFunction>(py_phantom_zbrush_multires_add_levels),
     METH_VARARGS,
     py_phantom_zbrush_multires_add_levels_doc},
    {nullptr, nullptr, 0, nullptr},
};

PyDoc_STRVAR(
    /* Wrap. */
    M_phantom_zbrush_doc,
    "Phantom: native helpers for reading ZBrush files (used by ``phantom_zbrush_io``).");
static PyModuleDef M_phantom_zbrush_module_def = {
    /*m_base*/ PyModuleDef_HEAD_INIT,
    /*m_name*/ "_phantom_zbrush",
    /*m_doc*/ M_phantom_zbrush_doc,
    /*m_size*/ 0,
    /*m_methods*/ M_phantom_zbrush_methods,
    /*m_slots*/ nullptr,
    /*m_traverse*/ nullptr,
    /*m_clear*/ nullptr,
    /*m_free*/ nullptr,
};

PyMODINIT_FUNC BPyInit_phantom_zbrush()
{
  return PyModule_Create(&M_phantom_zbrush_module_def);
}

/** \} */

}  // namespace blender
