/* SPDX-FileCopyrightText: 2023 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup draw_engine
 */

#include "../eevee/eevee_lut.hh" /* TODO: find somewhere to share blue noise Table. */

#include "BKE_idprop.hh"
#include "BKE_studiolight.h"

#include "BLI_math_matrix_c.hh"
#include "BLI_math_rotation_c.hh"
#include "BLI_math_vector_c.hh"

#include "GPU_batch_utils.hh"
#include "IMB_imbuf.hh"
#include "IMB_imbuf_types.hh"

#include "draw_common_c.hh"
#include "workbench_private.hh"

namespace blender::workbench {

/**
 * Append a matcap's diffuse and specular layers at `size` (resized if needed, since all layers
 * of the array share one size). A missing specular image becomes a black layer.
 */
static void append_matcap_layers(Vector<float> &buffer, StudioLight &studio_light, int2 size)
{
  BKE_studiolight_ensure_flag(&studio_light,
                              STUDIOLIGHT_MATCAP_DIFFUSE_GPUTEXTURE |
                                  STUDIOLIGHT_MATCAP_SPECULAR_GPUTEXTURE);
  const int64_t layer_len = int64_t(size.x) * size.y * 4;
  for (ImBuf *ibuf : {studio_light.matcap_diffuse.ibuf, studio_light.matcap_specular.ibuf}) {
    if (ibuf == nullptr || ibuf->float_data() == nullptr) {
      buffer.append_n_times(0.0f, layer_len);
    }
    else if (ibuf->x == size.x && ibuf->y == size.y) {
      buffer.extend(ibuf->float_data(), layer_len);
    }
    else {
      ImBuf *scaled = IMB_dupImBuf(ibuf);
      IMB_scale(scaled, size, IMBScaleFilter::Bilinear, false);
      buffer.extend(scaled->float_data(), layer_len);
      IMB_freeImBuf(scaled);
    }
  }
}

int SceneResources::object_matcap_layer(const Object &object)
{
  const IDProperty *prop = object.id.properties ?
                               IDP_GetPropertyTypeFromGroup(
                                   object.id.properties, "phantom_matcap", IDP_STRING) :
                               nullptr;
  if (prop == nullptr || IDP_string_get(prop)[0] == '\0') {
    return 0;
  }
  const std::string name = IDP_string_get(prop);
  int index = object_matcaps.first_index_of_try(name);
  if (index == -1) {
    /* Layers are packed in 6 bits, two texture layers each. */
    if (object_matcaps.size() >= 31) {
      return 0;
    }
    object_matcaps.append(name);
    index = object_matcaps.size() - 1;
  }
  return index + 1;
}

void SceneResources::update_matcap_tx()
{
  StudioLight *global = matcap_light;
  ImBuf *global_diffuse = global ? (BKE_studiolight_ensure_flag(
                                        global, STUDIOLIGHT_MATCAP_DIFFUSE_GPUTEXTURE),
                                    global->matcap_diffuse.ibuf) :
                                   nullptr;
  if (global_diffuse == nullptr || global_diffuse->float_data() == nullptr) {
    if (!matcap_tx.is_valid()) {
      matcap_tx.ensure_2d_array(
          gpu::TextureFormat::SFLOAT_16_16_16_16, int2(1), 2, GPU_TEXTURE_USAGE_SHADER_READ);
    }
    return;
  }

  std::string key = global->name;
  for (const std::string &name : object_matcaps) {
    key += "|" + name;
  }
  if (key == matcap_key && matcap_tx.is_valid()) {
    return;
  }

  const int2 size(global_diffuse->x, global_diffuse->y);
  Vector<float> buffer;
  append_matcap_layers(buffer, *global, size);
  for (const std::string &name : object_matcaps) {
    StudioLight *light = BKE_studiolight_find(name.c_str(), STUDIOLIGHT_TYPE_MATCAP);
    /* A matcap that no longer exists falls back to the viewport's one. */
    append_matcap_layers(buffer, light ? *light : *global, size);
  }
  matcap_tx = Texture("matcap_tx",
                      gpu::TextureFormat::SFLOAT_16_16_16_16,
                      GPU_TEXTURE_USAGE_SHADER_READ,
                      size,
                      2 * (1 + object_matcaps.size()),
                      buffer.data());
  matcap_key = key;
}

static float4x4 get_world_shading_rotation_matrix(float studiolight_rot_z)
{
  float4x4 V = draw::View::default_get().viewmat();
  float R[4][4];
  axis_angle_to_mat4_single(R, 'Z', -studiolight_rot_z);
  mul_m4_m4m4(R, V.ptr(), R);
  swap_v3_v3(R[2], R[1]);
  negate_v3(R[2]);
  return float4x4(R);
}

static SolidLightData get_light_data_from_studio_solidlight(const SolidLight *sl,
                                                            const float4x4 &world_shading_rotation)
{
  SolidLightData light = {};
  if (sl && sl->flag) {
    float3 direction = math::transform_direction(world_shading_rotation, float3(sl->vec));
    light.direction = float4(direction, 0.0f);
    /* We should pre-divide the power by PI but that makes the lights really dim. */
    light.specular_color = float4(float3(sl->spec), 0.0f);
    light.diffuse_color_wrap = float4(float3(sl->col), sl->smooth);
  }
  else {
    light.direction = float4(1.0f, 0.0f, 0.0f, 0.0f);
    light.specular_color = float4(0.0f);
    light.diffuse_color_wrap = float4(0.0f);
  }
  return light;
}

void SceneResources::load_jitter_tx(int total_samples)
{
  float4 jitter[jitter_tx_size][jitter_tx_size];

  const float total_samples_inv = 1.0f / total_samples;

  /* Create blue noise jitter texture */
  for (int x = 0; x < 64; x++) {
    for (int y = 0; y < 64; y++) {
      float phi = eevee::lut::blue_noise[y][x][0] * 2.0f * M_PI;
      /* This rotate the sample per pixels */
      jitter[y][x].x = math::cos(phi);
      jitter[y][x].y = math::sin(phi);
      /* This offset the sample along its direction axis (reduce banding) */
      float bn = eevee::lut::blue_noise[y][x][1] - 0.5f;
      bn = clamp_f(bn, -0.499f, 0.499f); /* fix fireflies */
      jitter[y][x].z = bn * total_samples_inv;
      jitter[y][x].w = eevee::lut::blue_noise[y][x][1];
    }
  }

  jitter_tx.free();
  jitter_tx.ensure_2d(gpu::TextureFormat::SFLOAT_16_16_16_16,
                      int2(jitter_tx_size),
                      GPU_TEXTURE_USAGE_SHADER_READ,
                      jitter[0][0]);
}

void SceneResources::init(const SceneState &scene_state, const DRWContext *ctx)
{
  const View3DShading &shading = scene_state.shading;

  world_buf.viewport_size = ctx->viewport_size_get();
  world_buf.viewport_size_inv = 1.0f / world_buf.viewport_size;
  world_buf.xray_mode = scene_state.xray_mode;
  world_buf.xray_alpha = shading.xray_alpha;
  world_buf.background_color = scene_state.background_color;
  world_buf.object_outline_color = float4(float3(shading.object_outline_color), 1.0f);
  world_buf.ui_scale = ctx->is_image_render() ? 1.0f : U.pixelsize;
  world_buf.matcap_orientation = (shading.flag & V3D_SHADING_MATCAP_FLIP_X) != 0;

  StudioLight *studio_light = nullptr;
  matcap_light = nullptr;
  if (U.edit_studio_light && shading.light == V3D_LIGHTING_STUDIO) {
    /* Do not use this for MATCAP.
     * matcap is also stored as StudioLight data but it needs its selected matcap texture loaded
     * through the normal matcap path. */
    studio_light = BKE_studiolight_studio_edit_get();
  }
  else {
    if (shading.light == V3D_LIGHTING_MATCAP) {
      studio_light = BKE_studiolight_find(shading.matcap, STUDIOLIGHT_TYPE_MATCAP);
    }
    /* The matcap texture itself is built in #update_matcap_tx at the end of sync. */
    matcap_light = studio_light;
    /* If matcaps are missing, use this as fallback. */
    if (studio_light == nullptr) {
      studio_light = BKE_studiolight_find(shading.studio_light, STUDIOLIGHT_TYPE_STUDIO);
    }
  }
  if (!matcap_tx.is_valid()) {
    matcap_tx.ensure_2d_array(
        gpu::TextureFormat::SFLOAT_16_16_16_16, int2(1), 2, GPU_TEXTURE_USAGE_SHADER_READ);
  }

  float4x4 world_shading_rotation = float4x4::identity();
  if (shading.flag & V3D_SHADING_WORLD_ORIENTATION) {
    world_shading_rotation = get_world_shading_rotation_matrix(shading.studiolight_rot_z);
  }

  for (int i = 0; i < 4; i++) {
    SolidLight *sl = (studio_light) ? &studio_light->light[i] : nullptr;
    world_buf.lights[i] = get_light_data_from_studio_solidlight(sl, world_shading_rotation);
  }

  if (studio_light != nullptr) {
    world_buf.ambient_color = float4(float3(studio_light->light_ambient), 0.0f);
    world_buf.use_specular = shading.flag & V3D_SHADING_SPECULAR_HIGHLIGHT &&
                             studio_light->flag & STUDIOLIGHT_SPECULAR_HIGHLIGHT_PASS;
  }
  else {
    world_buf.ambient_color = float4(1.0f, 1.0f, 1.0f, 0.0f);
    world_buf.use_specular = false;
  }

  /* TODO(@pragma37): volumes_do */

  cavity.init(scene_state, *this);

  if (scene_state.draw_dof && !jitter_tx.is_valid()) {
    /* We don't care about total_samples in this case */
    load_jitter_tx(1);
  }

  world_buf.push_update();

  for (int i : IndexRange(6)) {
    if (i < scene_state.clip_planes.size()) {
      clip_planes_buf[i] = scene_state.clip_planes[i];
    }
    else {
      clip_planes_buf[i] = float4(0);
    }
  }

  clip_planes_buf.push_update();

  if (missing_texture.gpu.texture == nullptr) {
    missing_texture.gpu.texture = GPU_texture_create_error(2, false);
    missing_texture.name = "Missing Texture";
  }

  dummy_texture_tx.ensure_2d(gpu::TextureFormat::UNORM_8_8_8_8,
                             int2(1),
                             GPU_TEXTURE_USAGE_SHADER_READ,
                             float4(0.0f, 0.0f, 0.0f, 0.0f));
  dummy_tile_array_tx.ensure_2d_array(gpu::TextureFormat::UNORM_8_8_8_8,
                                      int2(1),
                                      1,
                                      GPU_TEXTURE_USAGE_SHADER_READ,
                                      float4(0.0f, 0.0f, 0.0f, 0.0f));
  dummy_tile_data_tx.ensure_1d_array(gpu::TextureFormat::UNORM_8_8_8_8,
                                     1,
                                     1,
                                     GPU_TEXTURE_USAGE_SHADER_READ,
                                     float4(0.0f, 0.0f, 0.0f, 0.0f));

  if (volume_cube_batch == nullptr) {
    volume_cube_batch = make_batch(GPU_batch_unit_cube());
  }
}

}  // namespace blender::workbench
