# SPDX-FileCopyrightText: 2026 ohnsk
# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
How layers combine, and the layer value a key needs.

Based on Maya Workflow Kit for Blender by ohnsk (https://github.com/5hyuns/maya-workflow-kit-for-blender,
commit bd7ee04, ``src/allset/operators/anim_layers.py``: ``compose``, ``solve``, ``_quat_power``,
``compose_quaternion``, ``key_quaternion``). Modified for Phantom on 2026-10-28: the solve takes the layer's
index and returns None when there is no answer, the quaternion solve is its own function, and quaternion Add
steps normalize what they get, as Blender's NLA does.

Blender's NLA does this itself when keys are set (tweak mode with the upper stack evaluated), so these are used
where Blender doesn't: checking results, and merging or baking layers.

Steps are ``(mode, weight, value)`` bottom first. ``mode`` is REPLACE (Base, Override layers), ADD (Add layers)
or MUL (Add layers on scale channels, which multiply). A value of None means that layer has no curve for the
channel.
"""

from mathutils import Quaternion

SCALE_PATHS = {"scale", "delta_scale"}


def is_scale(data_path):
    return data_path.rsplit(".", 1)[-1] in SCALE_PATHS


def compose(steps, start):
    """The result of ``steps`` over ``start`` (the value below the first step)."""
    acc = start
    for mode, w, x in steps:
        if x is None:
            continue
        if mode == 'REPLACE':
            acc = (1.0 - w) * acc + w * x
        elif mode == 'MUL':
            acc = acc * (x ** w if x > 0 else 0.0)
        else:
            acc = acc + w * x
    return acc


def solve(steps, target, k, start=0.0):
    """The value step ``k`` needs so the result is ``target``, or None when no value can give it (a weight of
    0, or a full-weight Override above)."""
    below = compose(steps[:k], start)
    # The steps above are each a scale and an offset of what they get: result = a * acc + b.
    a, b = 1.0, 0.0
    for mode, w, x in steps[k + 1:]:
        if x is None:
            continue
        if mode == 'REPLACE':
            a, b = a * (1.0 - w), b * (1.0 - w) + w * x
        elif mode == 'MUL':
            f = x ** w if x > 0 else 0.0
            a, b = a * f, b * f
        else:
            b = b + w * x
    mode, w, _x = steps[k]
    if abs(a) < 1e-12 or w < 1e-12:
        return None
    acc = (target - b) / a
    if mode == 'REPLACE':
        return (acc - (1.0 - w) * below) / w
    if mode == 'MUL':
        return (acc / below) ** (1.0 / w) if below and acc / below > 0 else None
    return (acc - below) / w


def quat_power(q, w):
    axis, angle = q.to_axis_angle()
    return Quaternion(axis, angle * w)


def compose_quaternion(steps, start=(1.0, 0.0, 0.0, 0.0)):
    """Quaternion steps ``(blend, weight, Quaternion or None)``, blend REPLACE or COMBINE. Like Blender's NLA,
    an Add (Combine) step normalizes what it gets."""
    acc = Quaternion(start)
    for blend, w, q in steps:
        if q is None:
            continue
        if blend == 'REPLACE':
            acc = Quaternion([(1.0 - w) * a + w * b for a, b in zip(acc, q)])
        else:
            acc = acc.normalized() @ quat_power(q.normalized(), w)
    return acc


def solve_quaternion(steps, target, k):
    """The quaternion step ``k`` needs so the result is ``target`` (None: no answer). Exact unless a partial
    Override sits above an Add step being solved; Blender's own solve has the same limit."""
    blend, w, _q = steps[k]
    if w < 1e-12:
        return None
    upper = Quaternion()
    for b, uw, q in steps[k + 1:]:
        if q is not None and b != 'REPLACE':
            upper = upper @ quat_power(q.normalized(), uw)
    acc = Quaternion(target) @ upper.inverted()
    below = compose_quaternion(steps[:k])
    if blend == 'REPLACE':
        if below.dot(acc) < 0.0:
            acc = -acc
        return Quaternion([(a - (1.0 - w) * b) / w for a, b in zip(acc, below)])
    return quat_power(below.normalized().inverted() @ acc, 1.0 / w)
