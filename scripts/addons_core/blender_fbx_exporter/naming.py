"""File-name templating for the FBX Template Exporter.

The template is a normal Python format string, so ``{object}_{frame:04d}``
works.  Unknown tokens are left untouched (and reported by the UI) instead of
raising, so the user can keep typing.

Only this module decides how an output path is built - the UI preview and the
exporter call the very same function, which is what makes the preview exact.
"""

import datetime
import os
import re

import bpy

#: ``(token, description)`` pairs shown in the "insert token" menu.
TOKEN_LIST = (
    ('{object}', 'Name of the exported object'),
    ('{object_type}', 'Object type: mesh, armature, empty, ...'),
    ('{scene}', 'Scene name'),
    ('{collection}', 'First collection the object belongs to'),
    ('{action}', 'Action name (per-action export)'),
    ('{frame}', 'Current frame'),
    ('{frame_start}', 'First frame of the exported range'),
    ('{frame_end}', 'Last frame of the exported range'),
    ('{frame_count}', 'Number of frames in the range'),
    ('{fps}', 'Scene frame rate'),
    ('{blend}', 'Name of the .blend file'),
    ('{index}', 'Running export index (per-action export)'),
    ('{date}', 'Date as YYYY-MM-DD'),
    ('{time}', 'Time as HH-MM-SS'),
    ('{timestamp}', 'Date and time as YYYYMMDD-HHMMSS'),
)

_FIELD_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)(?:[!:][^{}]*)?\}')
_ILLEGAL_RE = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_UNDERSCORES_RE = re.compile(r'_{2,}')

#: Windows refuses to create files with these stems.
_RESERVED = {
    'CON', 'PRN', 'AUX', 'NUL',
    *{'COM%d' % i for i in range(1, 10)},
    *{'LPT%d' % i for i in range(1, 10)},
}

MAX_COMPONENT = 120


class _SafeDict(dict):
    """``format_map`` helper that leaves unknown ``{tokens}`` alone."""

    def __missing__(self, key):
        return '{' + key + '}'


# --------------------------------------------------------------------------- #
#  Context
# --------------------------------------------------------------------------- #

def build_context(scene, obj=None, action=None, index=0):
    """Build the token -> value mapping used by the template."""
    now = datetime.datetime.now()
    blend = 'untitled'
    if bpy.data.filepath:
        blend = os.path.splitext(os.path.basename(bpy.data.filepath))[0]

    collection = ''
    if obj is not None and getattr(obj, 'users_collection', None):
        collection = obj.users_collection[0].name

    frame_start = int(scene.frame_start)
    frame_end = int(scene.frame_end)

    return {
        'object': obj.name if obj is not None else scene.name,
        'object_type': obj.type.lower() if obj is not None else 'scene',
        'scene': scene.name,
        'collection': collection,
        'action': action.name if action is not None else '',
        'frame': int(scene.frame_current),
        'frame_start': frame_start,
        'frame_end': frame_end,
        'frame_count': max(0, frame_end - frame_start + 1),
        'fps': int(scene.render.fps),
        'blend': blend,
        'index': int(index),
        'date': now.strftime('%Y-%m-%d'),
        'time': now.strftime('%H-%M-%S'),
        'timestamp': now.strftime('%Y%m%d-%H%M%S'),
    }


def unknown_tokens(template, context):
    """Tokens used in ``template`` that ``context`` does not provide."""
    known = set(context)
    return sorted({m.group(1) for m in _FIELD_RE.finditer(template)
                   if m.group(1) not in known})


# --------------------------------------------------------------------------- #
#  Rendering / sanitising
# --------------------------------------------------------------------------- #

def render(template, context):
    """Expand ``template``; malformed templates are returned verbatim."""
    try:
        return template.format_map(_SafeDict(context))
    except (ValueError, IndexError, TypeError, AttributeError):
        return template


def sanitize_component(name):
    """Make one path component safe on Windows, macOS and Linux."""
    name = _ILLEGAL_RE.sub('_', str(name))
    name = _UNDERSCORES_RE.sub('_', name)
    name = name.strip(' _-')
    if not name:
        return ''
    if name.split('.')[0].upper() in _RESERVED:
        name = '_' + name
    if len(name) > MAX_COMPONENT:
        root, ext = os.path.splitext(name)
        name = root[:MAX_COMPONENT - len(ext)] + ext
    return name


def tidy_relative_path(text):
    """Turn a rendered template into a clean ``a/b/c`` relative path."""
    text = str(text).replace('\\', '/')
    parts = []
    for part in text.split('/'):
        part = sanitize_component(part)
        if part and part not in ('.', '..'):
            parts.append(part)
    return '/'.join(parts)


def resolve_output_dir(settings):
    """Absolute output folder for the current settings."""
    raw = settings.output_dir or '//'
    if raw.startswith('//') and not bpy.data.filepath:
        # "//" cannot be resolved without a saved .blend - fall back to the
        # user's home folder instead of Blender's working directory.
        raw = os.path.join(os.path.expanduser('~'), raw[2:])
    path = bpy.path.abspath(raw)
    if not path:
        path = bpy.path.abspath('//') or os.path.expanduser('~')
    return os.path.normpath(path)


def build_output_path(settings, scene, obj=None, action=None, index=0,
                      out_dir=None, per_action=False):
    """Full path of the FBX file for one export run.

    ``action`` only feeds the ``{action}`` token.  When ``per_action`` is set
    (one file per action) and the template does not mention ``{action}``, the
    action name is appended automatically so the files never collide.
    """
    context = build_context(scene, obj, action, index)
    template = settings.filename_template or '{object}'
    relative = render(template, context)

    if per_action and action is not None and '{action' not in template:
        suffix = sanitize_component(action.name)
        if suffix:
            relative = '%s_%s' % (relative.rstrip(' _-'), suffix)

    relative = tidy_relative_path(relative) or 'untitled'
    if not relative.lower().endswith('.fbx'):
        relative += '.fbx'

    base = out_dir or resolve_output_dir(settings)
    return os.path.join(base, *relative.split('/'))


def resolve_collision(path, mode):
    """Return ``path``, or a ``_001`` variant when "Keep Both" is selected."""
    if mode == 'OVERWRITE' or not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    counter = 1
    candidate = '%s_%03d%s' % (root, counter, ext)
    while os.path.exists(candidate):
        counter += 1
        candidate = '%s_%03d%s' % (root, counter, ext)
    return candidate


def preview(settings, scene, obj=None, action=None, index=0, per_action=False):
    """``(file name, folder, warnings)`` for the UI preview."""
    path = build_output_path(settings, scene, obj, action, index,
                             per_action=per_action)
    context = build_context(scene, obj, action, index)
    warnings = []
    missing = unknown_tokens(settings.filename_template or '', context)
    if missing:
        warnings.append('Unknown token(s): ' + ', '.join('{%s}' % t for t in missing))
    if not (settings.filename_template or '').strip():
        warnings.append('Template is empty - using "untitled".')
    if settings.output_dir.startswith('//') and not bpy.data.filepath:
        warnings.append('The .blend file is not saved yet - "//" resolves to an absolute path.')
    return os.path.basename(path), os.path.dirname(path), warnings
