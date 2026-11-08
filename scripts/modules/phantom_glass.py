# SPDX-FileCopyrightText: 2026 Phantom Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Phantom: frosted glass and soft shadows for the simple sculpting view's bars and windows.

Once per redraw of the 3D Viewport, `prepare()` takes what it shows so far (the scene and the
overlays drawn over it, from `gpu.state.phantom_viewport_backdrop_get`), shrinks it to a quarter
and blurs it (a Gaussian, in two passes). `glass()` then fills a rounded rectangle with that blur,
and `shadow()` draws a soft shadow under one. Without the backdrop (another kind of region, or an
older build) both quietly do nothing and the caller's own fill stands alone.
"""

import bpy
import gpu
from mathutils import Matrix
from gpu_extras.batch import batch_for_shader

# The blur's strength, in region pixels (the standard deviation of the Gaussian), and how much the
# backdrop is shrunk before blurring (the blur runs at that resolution).
BLUR_SIGMA = 24.0
# How dark and how colorful what shows through the glass stays (1: as it is), for readability.
DIM = 0.55
SATURATION = 0.75
DOWNSCALE = 4

_state = {"shaders": None, "size": None, "a": None, "b": None, "ready": False, "region": None}


def _shaders():
    if _state["shaders"] is not None:
        return _state["shaders"]

    def make(name, vert, frag, setup):
        iface = gpu.types.GPUStageInterfaceInfo(name + "_iface")
        iface.smooth('VEC2', "v_uv")
        info = gpu.types.GPUShaderCreateInfo()
        info.vertex_in(0, 'VEC2', "pos")
        info.vertex_out(iface)
        info.fragment_out(0, 'VEC4', "frag_color")
        setup(info)
        info.vertex_source(vert)
        info.fragment_source(frag)
        return gpu.shader.create_from_info(info)

    full_vert = "void main() { v_uv = pos * 0.5 + 0.5; gl_Position = vec4(pos, 0.0, 1.0); }"

    def compose_setup(info):
        info.sampler(0, 'FLOAT_2D', "scene_tx")
        info.sampler(1, 'FLOAT_2D', "overlay_tx")
        info.push_constant('VEC2', "texel")
        info.push_constant('FLOAT', "dim")
        info.push_constant('FLOAT', "saturation")

    # The scene is in linear colors (the view transform comes later, Standard in Solid shading):
    # into display colors, then the overlays over it. Four bilinear taps average a 4x4 block.
    compose_frag = """
vec3 to_display(vec3 c)
{
  c = clamp(c, 0.0, 1.0);
  return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
}
vec4 sample_at(vec2 uv)
{
  vec4 scene = texture(scene_tx, uv);
  /* The overlays are stored sRGB-encoded: sampling decodes them to linear (premultiplied). */
  vec4 over = texture(overlay_tx, uv);
  vec3 over_display = over.a > 0.0 ? to_display(over.rgb / over.a) * over.a : vec3(0.0);
  return vec4(over_display + to_display(scene.rgb) * (1.0 - over.a), 1.0);
}
void main()
{
  vec2 d = texel;
  vec4 c = 0.25 * (sample_at(v_uv + vec2(-d.x, -d.y)) + sample_at(v_uv + vec2(d.x, -d.y)) +
                   sample_at(v_uv + vec2(-d.x, d.y)) + sample_at(v_uv + vec2(d.x, d.y)));
  /* Darker and a little muted, so text over the glass reads whatever is behind it. */
  float luma = dot(c.rgb, vec3(0.2126, 0.7152, 0.0722));
  frag_color = vec4(mix(vec3(luma), c.rgb, saturation) * dim, 1.0);
}
"""

    def blur_setup(info):
        info.sampler(0, 'FLOAT_2D', "source_tx")
        info.push_constant('VEC2', "step_uv")

    # Separable Gaussian, 13 taps (sigma about 4 texels at the shrunk size).
    blur_frag = """
void main()
{
  const float w[7] = float[7](0.1585, 0.1465, 0.1156, 0.0779, 0.0448, 0.0220, 0.0092);
  vec4 sum = texture(source_tx, v_uv) * w[0];
  for (int i = 1; i < 7; i++) {
    sum += texture(source_tx, v_uv + step_uv * float(i)) * w[i];
    sum += texture(source_tx, v_uv - step_uv * float(i)) * w[i];
  }
  frag_color = vec4(sum.rgb / (w[0] + 2.0 * (w[1] + w[2] + w[3] + w[4] + w[5] + w[6])), 1.0);
}
"""

    def glass_setup(info):
        info.sampler(0, 'FLOAT_2D', "blur_tx")
        info.push_constant('MAT4', "ModelViewProjectionMatrix")
        info.push_constant('VEC2', "region_size")
        info.push_constant('FLOAT', "alpha")

    glass_vert = """
void main()
{
  v_uv = pos / region_size;
  gl_Position = ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0);
}
"""
    # The blur is in display colors; the overlays it is drawn into are sRGB-encoded, so it goes
    # back to linear (the hardware encodes it again), as Blender's own UI shaders do.
    glass_frag = """
vec3 to_linear(vec3 c)
{
  return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c));
}
void main()
{
  frag_color = vec4(to_linear(texture(blur_tx, v_uv).rgb), 1.0) * alpha;
}
"""

    def shadow_setup(info):
        info.push_constant('MAT4', "ModelViewProjectionMatrix")
        info.push_constant('VEC4', "box")
        info.push_constant('FLOAT', "radius")
        info.push_constant('FLOAT', "softness")
        info.push_constant('FLOAT', "alpha")

    shadow_vert = """
void main()
{
  v_uv = pos;
  gl_Position = ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0);
}
"""
    # A rounded box's distance, faded smoothly over `softness` outside it (premultiplied black).
    shadow_frag = """
void main()
{
  vec2 center = box.xy + box.zw * 0.5;
  vec2 q = abs(v_uv - center) - (box.zw * 0.5 - radius);
  float d = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - radius;
  float t = clamp(1.0 - d / softness, 0.0, 1.0);
  frag_color = vec4(0.0, 0.0, 0.0, alpha * t * t * (3.0 - 2.0 * t));
}
"""
    _state["shaders"] = {
        "compose": make("phantom_glass_compose", full_vert, compose_frag, compose_setup),
        "blur": make("phantom_glass_blur", full_vert, blur_frag, blur_setup),
        "glass": make("phantom_glass_fill", glass_vert, glass_frag, glass_setup),
        "shadow": make("phantom_glass_shadow", shadow_vert, shadow_frag, shadow_setup),
    }
    return _state["shaders"]


def _fullscreen(shader):
    batch = batch_for_shader(shader, 'TRI_FAN', {"pos": ((-1, -1), (1, -1), (1, 1), (-1, 1))})
    batch.draw(shader)


def prepare(region):
    """Blur what the region shows so far (call first thing in its draw callback)."""
    _state["ready"] = False
    getter = getattr(gpu.state, "phantom_viewport_backdrop_get", None)
    if getter is None:
        return False
    try:
        textures = getter()
        if textures is None:
            return False
        scene_tx, overlay_tx = textures
        shaders = _shaders()
    except Exception as ex:  # A driver without some feature: no glass rather than no view.
        print("Phantom: no glass:", ex)
        _state["shaders"] = {}
        return False
    if not shaders:
        return False
    w = max(1, overlay_tx.width // DOWNSCALE)
    h = max(1, overlay_tx.height // DOWNSCALE)
    if _state["size"] != (w, h):
        _state["a"] = gpu.types.GPUOffScreen(w, h)
        _state["b"] = gpu.types.GPUOffScreen(w, h)
        _state["size"] = (w, h)
    a, b = _state["a"], _state["b"]
    gpu.state.blend_set('NONE')
    with gpu.matrix.push_pop(), gpu.matrix.push_pop_projection():
        gpu.matrix.load_identity()
        gpu.matrix.load_projection_matrix(Matrix.Identity(4))
        with a.bind():
            shader = shaders["compose"]
            shader.bind()
            shader.uniform_sampler("scene_tx", scene_tx)
            shader.uniform_sampler("overlay_tx", overlay_tx)
            shader.uniform_float("texel", (1.0 / overlay_tx.width, 1.0 / overlay_tx.height))
            shader.uniform_float("dim", DIM)
            shader.uniform_float("saturation", SATURATION)
            _fullscreen(shader)
        shader = shaders["blur"]
        spread = BLUR_SIGMA / DOWNSCALE / 4.0
        for source, target, step in ((a, b, (spread / w, 0.0)), (b, a, (0.0, spread / h))):
            with target.bind():
                shader.bind()
                shader.uniform_sampler("source_tx", source.texture_color)
                shader.uniform_float("step_uv", step)
                _fullscreen(shader)
    gpu.state.blend_set('ALPHA')
    _state["ready"] = True
    _state["region"] = (region.width, region.height)
    _state["region_ptr"] = region.as_pointer()
    # The simple view's popups (menus, the Scene window) draw it behind them too.
    publish = getattr(gpu.state, "phantom_backdrop_publish", None)
    if publish is not None:
        publish(a.texture_color, region.x, region.y, region.x + region.width,
                region.y + region.height)
    return True


def ready():
    """Whether the region being drawn has its blurred backdrop (prepared this redraw)."""
    region = bpy.context.region
    return (_state["ready"] and region is not None and
            region.as_pointer() == _state.get("region_ptr"))


def unprepare():
    """No glass for the region being drawn (it falls back to plain fills)."""
    _state["ready"] = False


def _mvp():
    return gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix()


def glass(outline, center, alpha=1.0):
    """Fill the shape (its outline, as the simple view's rounded rectangles give it) with the
    blurred backdrop."""
    if not ready() or alpha <= 0.0:
        return
    shader = _state["shaders"]["glass"]
    shader.bind()
    shader.uniform_sampler("blur_tx", _state["a"].texture_color)
    shader.uniform_float("ModelViewProjectionMatrix", _mvp())
    shader.uniform_float("region_size", _state["region"])
    shader.uniform_float("alpha", alpha)
    gpu.state.blend_set('ALPHA_PREMULT')
    batch = batch_for_shader(shader, 'TRI_FAN', {"pos": [center] + list(outline) + [outline[0]]})
    batch.draw(shader)
    gpu.state.blend_set('ALPHA')


def shadow(x, y, w, h, radius, softness, alpha, offset=0.0):
    """A soft shadow under the rounded rectangle (x, y, w, h), `offset` pixels lower."""
    shaders = _state["shaders"] if _state["shaders"] else None
    if shaders is None:
        try:
            shaders = _shaders()
        except Exception:
            _state["shaders"] = {}
            return
    if not shaders or alpha <= 0.0:
        return
    y -= offset
    shader = shaders["shadow"]
    shader.bind()
    shader.uniform_float("ModelViewProjectionMatrix", _mvp())
    shader.uniform_float("box", (x, y, w, h))
    shader.uniform_float("radius", radius)
    shader.uniform_float("softness", softness)
    shader.uniform_float("alpha", alpha)
    gpu.state.blend_set('ALPHA_PREMULT')
    pad = softness
    quad = ((x - pad, y - pad), (x + w + pad, y - pad), (x + w + pad, y + h + pad), (x - pad, y + h + pad))
    batch_for_shader(shader, 'TRI_FAN', {"pos": quad}).draw(shader)
    gpu.state.blend_set('ALPHA')
