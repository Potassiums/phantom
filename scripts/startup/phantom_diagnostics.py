# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: Python crash diagnostics.

If Phantom crashes, Blender's crash report shows where in C++ it happened, but not which Python
code was running. Python's ``faulthandler`` fills that gap: on a crash it writes the Python
traceback of every thread to ``phantom_python_crash.txt`` in the temporary folder, even when
Python's own state is too broken for Blender to report. Blender's crash handler then appends the
latest session of that log to its own ``.crash.txt``, so one report holds both; the crash dialog's
"Save Report to Desktop" copies it to the desktop.

Each session adds a header line, so the last section is the latest run. On Windows,
``faulthandler`` also logs access violations that are handled and recovered from (e.g. inside a
graphics driver), usually with "<no Python frame>": the entry at the end is the crash. The file
is cleared when it grows past ``MAX_SIZE``.
"""

import faulthandler
import os
import sys
import tempfile
import time

import bpy

FILENAME = "phantom_python_crash.txt"
MAX_SIZE = 1024 * 1024

# Kept open for the whole session: faulthandler writes to it when Phantom crashes.
_file = None


def log_path():
    """In Blender's temporary folder, where its crash report goes: the crash handler appends this
    log's latest session to the report (see ``crashlog_append_phantom_python``)."""
    session = bpy.app.tempdir.rstrip("\\/")
    base = os.path.dirname(session) if session else tempfile.gettempdir()
    return os.path.join(base, FILENAME)


def register():
    global _file
    if _file is not None or bpy.app.background:
        return
    path = log_path()
    try:
        mode = "w" if os.path.exists(path) and os.path.getsize(path) > MAX_SIZE else "a"
        _file = open(path, mode, encoding="utf-8")
        version = bpy.app.version_string
        commit = bpy.app.build_hash.decode("utf-8", "replace")
        _file.write(f"\n# Phantom session {time.strftime('%Y-%m-%d %H:%M:%S')}, "
                    f"Blender {version}, {commit}, Python {sys.version.split()[0]}\n")
        _file.flush()
        faulthandler.enable(file=_file, all_threads=True)
    except OSError as ex:
        print("Phantom: could not set up the Python crash log:", ex)
        _file = None


def unregister():
    global _file
    if _file is None:
        return
    faulthandler.disable()
    _file.close()
    _file = None
