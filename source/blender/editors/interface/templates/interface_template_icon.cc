/* SPDX-FileCopyrightText: 2024 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup edinterface
 */

#include "BLI_string_ref.hh"

#include "BLT_translation.hh"

#include "BKE_screen.hh"

#include "RNA_access.hh"

#include "ED_screen.hh"

#include "UI_interface_layout.hh"
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

  /* arg_litem is malloced, can be freed by parent button */
  args = *(static_cast<IconViewMenuArgs *>(arg_litem));
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
    Button *search_but = uiDefBut(block,
                                  ButtonType::Text,
                                  "",
                                  0,
                                  h + int(0.3f * U.widget_unit),
                                  short(columns * w),
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
    block_emboss_set(block, EmbossType::Pulldown);
  }
  if (icon_view_filter[0] != '\0') {
    string_search::StringSearch<const EnumPropertyItem> search;
    for (int a = 0; item[a].identifier; a++) {
      search.add(item[a].name, &item[a]);
    }
    shown = search.query(icon_view_filter);
  }
  else {
    for (int a = 0; item[a].identifier; a++) {
      shown.append(&item[a]);
    }
  }

  for (const int a : shown.index_range()) {
    const int x = (a % 8) * w;
    const int y = -(a / 8) * h;

    const int icon = shown[a]->icon;
    const int value = shown[a]->value;
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
  }

  block_bounds_set_normal(block, 0.3f * U.widget_unit);
  block_direction_set(block, UI_DIR_DOWN);

  if (free) {
    MEM_delete(item);
  }

  return block;
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

  if (free_items) {
    MEM_delete(items);
  }
}

}  // namespace blender::ui
