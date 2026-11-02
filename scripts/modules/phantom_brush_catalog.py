# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: every sculpt brush of the asset libraries, with its thumbnail, for the simple view's brush
strip (the + of the quick brushes).

Reading brushes means loading them, which the open file must not get: a background Phantom reads
the libraries' blend-files instead (Essentials' sculpt brushes and every blend-file of the custom
libraries) and writes each sculpt brush's thumbnail (``.npy``, the preview's float pixels) and an
index (``index.json``) to ``phantom_brush_catalog`` in the user config directory. The index keeps
the files' sizes and dates, so it is read again only when a library changes.
"""

import hashlib
import json
import os
import subprocess
import sys

import bpy

ESSENTIALS_SCULPT = "brushes/essentials_brushes-mesh_sculpt.blend"
INDEX = "index.json"

_state = {"process": None, "entries": None, "signature": None}


def cache_dir():
    return os.path.join(bpy.utils.user_resource('CONFIG'), "phantom_brush_catalog")


def _files():
    """The blend-files to read: (library type, library identifier, library root, file)."""
    files = []
    essentials = bpy.utils.system_resource('DATAFILES', path="assets")
    path = os.path.join(essentials, *ESSENTIALS_SCULPT.split("/"))
    if os.path.exists(path):
        files.append(('ESSENTIALS', "", essentials, path))
    for library in bpy.context.preferences.filepaths.asset_libraries:
        root = bpy.path.abspath(library.path)
        if not os.path.isdir(root):
            continue
        for folder, _dirs, names in os.walk(root):
            for name in sorted(names):
                if name.endswith(".blend"):
                    files.append(('CUSTOM', library.name, root, os.path.join(folder, name)))
    return files


def _signature(files):
    h = hashlib.sha1()
    for kind, identifier, _root, path in files:
        try:
            stat = os.stat(path)
        except OSError:
            continue
        h.update(f"{kind}|{identifier}|{path}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())
    return h.hexdigest()


def _read_index():
    try:
        with open(os.path.join(cache_dir(), INDEX), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def entries():
    """The brushes, as dicts (type, identifier, relative, name, thumbnail), or None while the
    libraries are being read for the first time. Starts reading them when they changed."""
    process = _state["process"]
    if process is not None:
        if process.poll() is None:
            return _state["entries"]
        _state["process"] = None
        _state["entries"] = None
    if _state["entries"] is not None:
        return _state["entries"]
    files = _files()
    signature = _signature(files)
    index = _read_index()
    if index is not None and index.get("signature") == signature:
        _state["entries"] = index.get("brushes", [])
        return _state["entries"]
    if index is not None:
        # Show what was there while the libraries are read again.
        _state["entries"] = index.get("brushes", [])
    if _state["signature"] == signature:
        # Already read once for these files and it failed: don't keep trying.
        _state["entries"] = _state["entries"] or []
        return _state["entries"]
    _state["signature"] = signature
    _start(files, signature)
    return _state["entries"]


def reading():
    return _state["process"] is not None and _state["process"].poll() is None


def refresh():
    """Read the libraries again next time (e.g. after one was added)."""
    _state["entries"] = None


def _start(files, signature):
    os.makedirs(cache_dir(), exist_ok=True)
    job = os.path.join(cache_dir(), "job.json")
    with open(job, "w", encoding="utf-8") as fh:
        json.dump({"files": files, "signature": signature, "out": cache_dir()}, fh)
    script = ("import sys; sys.path.insert(0, %r); import phantom_brush_catalog as c; c._work(%r)"
              % (os.path.dirname(__file__), job))
    try:
        _state["process"] = subprocess.Popen(
            [bpy.app.binary_path, "--background", "--factory-startup", "--python-expr", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as ex:
        print("Phantom: could not read the brush libraries:", ex)


def _work(job_path):
    """In the background Phantom: read the brushes and write their thumbnails and the index."""
    import numpy as np
    with open(job_path, encoding="utf-8") as fh:
        job = json.load(fh)
    out = job["out"]
    thumbs = os.path.join(out, "thumbnails")
    os.makedirs(thumbs, exist_ok=True)
    brushes = []
    for kind, identifier, root, path in job["files"]:
        try:
            with bpy.data.libraries.load(path, assets_only=True) as (data_from, data_to):
                data_to.brushes = list(data_from.brushes)
        except (OSError, RuntimeError) as ex:
            print("Phantom: could not read", path, ex)
            continue
        relative_file = os.path.relpath(path, root).replace("\\", "/")
        for brush in data_to.brushes:
            if brush is None or not getattr(brush, "use_paint_sculpt", False):
                continue
            relative = f"{relative_file}/Brush/{brush.name}"
            key = hashlib.sha1(f"{kind}|{identifier}|{relative}".encode()).hexdigest()[:16]
            thumbnail = ""
            preview = brush.preview
            if preview is not None and preview.image_size[0] > 0:
                width, height = preview.image_size
                data = np.empty(width * height * 4, dtype=np.float32)
                preview.image_pixels_float.foreach_get(data)
                thumbnail = os.path.join(thumbs, key + ".npy")
                np.save(thumbnail, data.reshape(height, width, 4).astype(np.float16))
            brushes.append({"type": kind, "identifier": identifier, "relative": relative,
                            "name": brush.name, "thumbnail": thumbnail})
    brushes.sort(key=lambda b: (b["type"] != 'ESSENTIALS', b["identifier"], b["name"].lower()))
    with open(os.path.join(out, INDEX + ".tmp"), "w", encoding="utf-8") as fh:
        json.dump({"signature": job["signature"], "brushes": brushes}, fh, indent=1)
    os.replace(os.path.join(out, INDEX + ".tmp"), os.path.join(out, INDEX))
    sys.stdout.flush()
