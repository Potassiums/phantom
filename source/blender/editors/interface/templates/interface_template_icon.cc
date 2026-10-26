/* SPDX-FileCopyrightText: 2024 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup edinterface
 */

#include "BLI_listbase_iterator.hh"
#include "BLI_math_vector_c.hh"
#include "BLI_set.hh"
#include "BLI_string_ref.hh"

#include "BLT_translation.hh"

#include "DNA_screen_types.h"
#include "DNA_windowmanager_types.h"

#include "BKE_context.hh"
#include "BKE_global.hh"
#include "BKE_main.hh"
#include "BKE_screen.hh"

#include "RNA_access.hh"

#include "ED_screen.hh"

#include "WM_api.hh"

#include "UI_interface_layout.hh"
#include "UI_resources.hh"
#include "UI_string_search.hh"
#include "interface_intern.hh"

namespace blender::ui {

struct IconViewMenuArgs {
  PointerRNA ptr;
  PropertyRNA *prop;
  bool show_labels;
  float icon_scale;
};

/** Phantom: grids with more items than this get a search field (e.g. the matcap picker). */
static constexpr int ICON_VIEW_SEARCH_MIN_ITEMS = 16;
/** Phantom: text of the grid's search field, kept while the popup is open. */
static char icon_view_filter[UI_MAX_NAME_STR] = "";
/** Phantom: show only favorite items (toggle next to the search field). */
static bool icon_view_favorites_only = false;
/** Phantom: item identifiers marked as favorites (star) or new ("new!" badge), from Python. */
static Set<std::string> icon_view_favorites;
static Set<std::string> icon_view_new;
/** Phantom: context member holding a grid item's identifier, for its right-click menu. */
static constexpr const char *ICON_VIEW_ITEM_CONTEXT = "phantom_icon_view_item";

static void icon_view_tags_parse(const StringRef text, Set<std::string> &r_set)
{
  r_set.clear();
  int64_t start = 0;
  while (start <= text.size()) {
    int64_t end = text.find('\n', start);
    if (end == StringRef::not_found) {
      end = text.size();
    }
    if (end > start) {
      r_set.add(text.substr(start, end - start));
    }
    start = end + 1;
  }
}

/** Phantom: the grid popup last opened (may have closed since: only compared, never read). */
static const ARegion *icon_view_popup_region = nullptr;

void clear_icon_view_set()
{
  icon_view_favorites = Set<std::string>();
  icon_view_new = Set<std::string>();
}

void template_icon_view_tags_set(const StringRef favorites, const StringRef new_items)
{
  icon_view_tags_parse(favorites, icon_view_favorites);
  icon_view_tags_parse(new_items, icon_view_new);

  /* Rebuild the grid if it is open, so a new star shows right away. */
  wmWindowManager *wm = G_MAIN ? G_MAIN->wm.first() : nullptr;
  if (wm == nullptr || icon_view_popup_region == nullptr) {
    return;
  }
  for (wmWindow &win : wm->windows) {
    bScreen *screen = WM_window_get_active_screen(&win);
    if (screen == nullptr) {
      continue;
    }
    for (ARegion &region : screen->regionbase) {
      if (&region == icon_view_popup_region && region.regiondata) {
        static_cast<PopupBlockHandle *>(region.regiondata)->can_refresh = true;
        ED_region_tag_refresh_ui(&region);
      }
    }
  }
}

static void icon_view_item_context_set(Block *block, Button *but, const StringRef identifier)
{
  bContextStore *ctx = CTX_store_add(block->contexts, ICON_VIEW_ITEM_CONTEXT, identifier);
  ctx->used = true;
  but->context = ctx;
}

/** Phantom: a filled rounded box in a fixed color, drawn under or over grid items. */
static void icon_view_box_add(
    Block *block, const int x, const int y, const int w, const int h, const uchar color[4])
{
  Button *box = uiDefBut(
      block, ButtonType::Roundbox, "", x, y, w, h, nullptr, 0.0f, 0.0f, std::nullopt);
  copy_v4_v4_uchar(box->col, color);
}

/** Phantom: rebuild the grid popup (in `region_v`) for the new search text. */
static void icon_view_filter_update(bContext * /*C*/, void *region_v, void * /*arg2*/)
{
  ARegion *region = static_cast<ARegion *>(region_v);
  if (PopupBlockHandle *handle = static_cast<PopupBlockHandle *>(region->regiondata)) {
    /* Block-button popups don't normally rebuild; this one does, for the filter. */
    handle->can_refresh = true;
    ED_region_tag_refresh_ui(region);
  }
}

/* ID Search browse menu, open */
static Block *icon_view_menu_cb(bContext *C, ARegion *region, void *arg_litem)
{
  static IconViewMenuArgs args;

  /* A popup being rebuilt already has its block: that happens while typing in its search
   * field (Phantom), which keeps its text. A new popup starts with an empty search. */
  const bool is_refresh = !region->runtime->uiblocks.is_empty();
  if (!is_refresh) {
    icon_view_filter[0] = '\0';
  }
  icon_view_popup_region = region;

  /* arg_litem is malloced, can be freed by parent button. Phantom: so only read it when the
   * popup opens; rebuilds (search, favorites filter) reuse the copy, as the parent button may
   * have been redrawn (and its argument freed) since. */
  if (!is_refresh) {
    args = *(static_cast<IconViewMenuArgs *>(arg_litem));
  }
  const int w = UI_UNIT_X * (args.icon_scale);
  const int h = UI_UNIT_X * (args.icon_scale + args.show_labels);

  Block *block = block_begin(C, region, "_popup", EmbossType::Pulldown);
  block_flag_enable(block, BLOCK_LOOP);
  block_theme_style_set(block, BLOCK_THEME_STYLE_POPUP);

  bool free;
  int items_num;
  const EnumPropertyItem *item;
  RNA_property_enum_items(C, &args.ptr, args.prop, &item, &items_num, &free);

  /* Phantom: a search field above big grids, filtering the items as you type. */
  Vector<const EnumPropertyItem *> shown;
  if (items_num > ICON_VIEW_SEARCH_MIN_ITEMS) {
    const int columns = std::min(items_num, 8);
    block_emboss_set(block, EmbossType::Emboss);
    const int search_y = h + int(0.3f * U.widget_unit);
    Button *search_but = uiDefBut(block,
                                  ButtonType::Text,
                                  "",
                                  0,
                                  search_y,
                                  short(columns * w - UI_UNIT_X),
                                  UI_UNIT_Y,
                                  icon_view_filter,
                                  0,
                                  sizeof(icon_view_filter),
                                  "");
    button_flag_enable(search_but, BUT_TEXTEDIT_UPDATE | BUT_VALUE_CLEAR);
    button_flag_disable(search_but, BUT_UNDO);
    def_but_icon(search_but, ICON_VIEWZOOM, UI_HAS_ICON);
    button_placeholder_set(search_but, IFACE_("Search"));
    button_func_set(search_but, icon_view_filter_update, region, nullptr);
    /* Type right away while the matcaps stay clickable, like the asset shelf's search. */
    button_flag_enable(search_but, BUT_FORCE_SEMI_MODAL_ACTIVE);

    Button *favorites_but = uiDefIconButBit(block,
                                            ButtonType::Toggle,
                                            1,
                                            icon_view_favorites_only ? ICON_SOLO_ON : ICON_SOLO_OFF,
                                            columns * w - UI_UNIT_X,
                                            search_y,
                                            UI_UNIT_X,
                                            UI_UNIT_Y,
                                            &icon_view_favorites_only,
                                            0,
                                            0,
                                            TIP_("Show only favorite items"));
    button_flag_disable(favorites_but, BUT_UNDO);
    button_flag_enable(favorites_but, BUT_KEEP_MENU_OPEN);
    button_func_set(favorites_but, icon_view_filter_update, region, nullptr);
    block_emboss_set(block, EmbossType::Pulldown);
  }
  const bool use_favorites_filter = items_num > ICON_VIEW_SEARCH_MIN_ITEMS &&
                                    icon_view_favorites_only;
  if (icon_view_filter[0] != '\0') {
    string_search::StringSearch<const EnumPropertyItem> search;
    for (int a = 0; item[a].identifier; a++) {
      if (!use_favorites_filter || icon_view_favorites.contains(item[a].identifier)) {
        search.add(item[a].name, &item[a]);
      }
    }
    shown = search.query(icon_view_filter);
  }
  else {
    for (int a = 0; item[a].identifier; a++) {
      if (!use_favorites_filter || icon_view_favorites.contains(item[a].identifier)) {
        shown.append(&item[a]);
      }
    }
  }

  /* Phantom: "new!" items get a green frame and tab; favorites a star. */
  static const uchar new_green[4] = {46, 204, 64, 255};

  for (const int a : shown.index_range()) {
    const int x = (a % 8) * w;
    const int y = -(a / 8) * h;

    const int icon = shown[a]->icon;
    const int value = shown[a]->value;
    const bool is_new = icon_view_new.contains(shown[a]->identifier);
    const bool is_favorite = icon_view_favorites.contains(shown[a]->identifier);
    if (is_new) {
      /* A green frame: green box, with the popup's background inset over it. */
      const int frame = std::max(2, int(2.0f * UI_SCALE_FAC));
      const bTheme *btheme = theme::theme_get();
      block_emboss_set(block, EmbossType::Emboss);
      icon_view_box_add(block, x, y, w, h, new_green);
      icon_view_box_add(block,
                        x + frame,
                        y + frame,
                        w - 2 * frame,
                        h - 2 * frame,
                        btheme->tui.wcol_menu_back.inner);
      block_emboss_set(block, EmbossType::Pulldown);
    }
    Button *but;
    if (args.show_labels) {
      but = uiDefIconTextButR_prop(block,
                                   ButtonType::Row,
                                   icon,
                                   shown[a]->name,
                                   x,
                                   y,
                                   w,
                                   h,
                                   &args.ptr,
                                   args.prop,
                                   -1,
                                   0,
                                   value,
                                   std::nullopt);
    }
    else {
      but = uiDefIconButR_prop(block,
                               ButtonType::Row,
                               icon,
                               x,
                               y,
                               w,
                               h,
                               &args.ptr,
                               args.prop,
                               -1,
                               0,
                               value,
                               std::nullopt);
    }
    def_but_icon(but, icon, UI_HAS_ICON | BUT_ICON_PREVIEW);
    icon_view_item_context_set(block, but, shown[a]->identifier);

    if (is_new || is_favorite) {
      block_emboss_set(block, EmbossType::Emboss);
      const int badge_h = int(UI_UNIT_Y * 0.8f);
      if (is_new) {
        static const uchar white[4] = {255, 255, 255, 255};
        const int tab_w = int(UI_UNIT_X * 1.6f);
        icon_view_box_add(block, x, y + h - badge_h, tab_w, badge_h, new_green);
        Button *label = uiDefBut(block,
                                 ButtonType::Label,
                                 IFACE_("new!"),
                                 x,
                                 y + h - badge_h,
                                 tab_w,
                                 badge_h,
                                 nullptr,
                                 0.0f,
                                 0.0f,
                                 std::nullopt);
        label->drawflag |= BUT_TEXT_LEFT;
        copy_v4_v4_uchar(label->col, white);
      }
      if (is_favorite) {
        /* A gold star with a black outline: black copies nudged around it, then the gold one. */
        static const uchar black[4] = {0, 0, 0, 255};
        static const uchar gold[4] = {255, 196, 30, 255};
        const int star_x = x + w - UI_UNIT_X;
        const int star_y = y + h - UI_UNIT_Y;
        const int nudge = std::max(1, int(1.2f * UI_SCALE_FAC));
        const int offsets[5][2] = {{-nudge, 0}, {nudge, 0}, {0, -nudge}, {0, nudge}, {0, 0}};
        for (int i = 0; i < 5; i++) {
          Button *star = uiDefIconBut(block,
                                      ButtonType::Label,
                                      ICON_SOLO_ON,
                                      star_x + offsets[i][0],
                                      star_y + offsets[i][1],
                                      UI_UNIT_X,
                                      UI_UNIT_Y,
                                      nullptr,
                                      0.0f,
                                      0.0f,
                                      std::nullopt);
          copy_v4_v4_uchar(star->col, i < 4 ? black : gold);
        }
      }
      block_emboss_set(block, EmbossType::Pulldown);
    }
  }

  block_bounds_set_normal(block, 0.3f * U.widget_unit);
  block_direction_set(block, UI_DIR_DOWN);

  if (free) {
    MEM_delete(item);
  }

  return block;
}

PointerRNA template_operator_preview(Layout *layout,
                                     const StringRefNull opname,
                                     const int icon_value,
                                     const float scale,
                                     const bool depress)
{
  wmOperatorType *ot = WM_operatortype_find(opname.c_str(), false);
  if (ot == nullptr || ot->srna == nullptr) {
    RNA_warning("operator not found: %s", opname.c_str());
    return {};
  }
  Layout &col = layout->column(true);
  col.scale_y_set(scale);
  const PointerRNA ptr = col.op(
      ot, "", icon_value, col.operator_context(), depress ? ITEM_O_DEPRESS : UI_ITEM_NONE);
  if (icon_value) {
    if (Button *but = col.block()->last_but()) {
      /* Drawn as a preview, filling the button, like the thumbnail of #template_icon_view. */
      def_but_icon(but, icon_value, UI_HAS_ICON | BUT_ICON_PREVIEW);
    }
  }
  return ptr;
}

void template_icon(Layout *layout, int icon_value, float icon_scale)
{
  Block *block = layout->absolute().block();
  Button *but = uiDefIconBut(block,
                             ButtonType::Label,
                             ICON_X,
                             0,
                             0,
                             UI_UNIT_X * icon_scale,
                             UI_UNIT_Y * icon_scale,
                             nullptr,
                             0.0,
                             0.0,
                             "");
  def_but_icon(but, icon_value, UI_HAS_ICON | BUT_ICON_PREVIEW);
}

void template_icon_view(Layout *layout,
                        PointerRNA *ptr,
                        const StringRefNull propname,
                        bool show_labels,
                        float icon_scale,
                        float icon_scale_popup)
{
  PropertyRNA *prop = RNA_struct_find_property(ptr, propname.c_str());

  if (!prop || RNA_property_type(prop) != PROP_ENUM) {
    RNA_warning("property of type Enum not found: %s.%s",
                RNA_struct_identifier(ptr->type),
                propname.c_str());
    return;
  }

  Block *block = layout->absolute().block();

  int tot_items;
  bool free_items;
  const EnumPropertyItem *items;
  RNA_property_enum_items(
      static_cast<bContext *>(block->evil_C), ptr, prop, &items, &tot_items, &free_items);
  const int value = RNA_property_enum_get(ptr, prop);
  int icon = ICON_NONE;
  RNA_enum_icon_from_value(items, value, &icon);

  Button *but;
  if (RNA_property_editable(ptr, prop)) {
    IconViewMenuArgs *cb_args = MEM_new<IconViewMenuArgs>(__func__);
    cb_args->ptr = *ptr;
    cb_args->prop = prop;
    cb_args->show_labels = show_labels;
    cb_args->icon_scale = icon_scale_popup;

    but = uiDefBlockButN(block,
                         icon_view_menu_cb,
                         cb_args,
                         "",
                         0,
                         0,
                         UI_UNIT_X * icon_scale,
                         UI_UNIT_Y * icon_scale,
                         "");
  }
  else {
    but = uiDefIconBut(block,
                       ButtonType::Label,
                       ICON_X,
                       0,
                       0,
                       UI_UNIT_X * icon_scale,
                       UI_UNIT_Y * icon_scale,
                       nullptr,
                       0.0,
                       0.0,
                       "");
  }

  def_but_icon(but, icon, UI_HAS_ICON | BUT_ICON_PREVIEW);

  /* Phantom: let the right-click menu act on the shown item (e.g. "Set as Default Matcap"). */
  const char *identifier = nullptr;
  if (RNA_enum_identifier(items, value, &identifier)) {
    icon_view_item_context_set(block, but, identifier);
  }

  if (free_items) {
    MEM_delete(items);
  }
}

}  // namespace blender::ui
