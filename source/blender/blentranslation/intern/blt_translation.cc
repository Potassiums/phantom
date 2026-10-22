/* SPDX-FileCopyrightText: 2011 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup blt
 *
 * Manages translation files and provides translation functions.
 * (which are optional and can be disabled as a preference).
 */

#include <cstdlib>
#include <cstring>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>

#include "BLI_utildefines.hh"

#include "BLT_translation.hh"

#include "DNA_userdef_types.h" /* For user settings. */

#ifdef WITH_PYTHON
#  include "BPY_extern.hh"
#endif

#ifdef WITH_INTERNATIONAL
#  include "BLI_threads.hh"
#  include "messages.hh"
#endif /* WITH_INTERNATIONAL */

namespace blender {

bool BLT_is_default_context(const StringRef msgctxt)
{
  /* We use the "short" test, a more complete one could be:
   * return (!msgctxt || !msgctxt[0] || STREQ(msgctxt, BLT_I18NCONTEXT_DEFAULT_BPYRNA))
   */
  /* NOTE: trying without the void string check for now, it *should* not be necessary... */
  return (msgctxt.is_empty() || msgctxt[0] == BLT_I18NCONTEXT_DEFAULT_BPYRNA[0]);
}

static std::optional<StringRefNull> pgettext(StringRef msgctxt, const StringRef msgid)
{
#ifdef WITH_INTERNATIONAL
  if (msgid.is_empty()) {
    return std::nullopt;
  }
  if (BLT_is_default_context(msgctxt)) {
    msgctxt = BLT_I18NCONTEXT_DEFAULT;
  }
  if (const std::optional<StringRefNull> translation = locale::translate(0, msgctxt, msgid)) {
    return translation;
  }
#  ifdef WITH_PYTHON
  return BPY_app_translations_py_pgettext(msgctxt, msgid);
#  else
  return std::nullopt;
#  endif
#else
  UNUSED_VARS(msgctxt, msgid);
  return std::nullopt;
#endif
}

const char *BLT_pgettext(const char *msgctxt, const char *msgid)
{
  const std::optional<StringRefNull> translation = pgettext(msgctxt, msgid);
  if (!translation) {
    return msgid;
  }
  return translation->c_str();
}

StringRef BLT_pgettext(StringRef msgctxt, StringRef msgid)
{
  const std::optional<StringRefNull> translation = pgettext(msgctxt, msgid);
  if (!translation) {
    return msgid;
  }
  return *translation;
}

bool BLT_translate()
{
#ifdef WITH_INTERNATIONAL
  return BLI_thread_is_main();
#else
  return false;
#endif
}

bool BLT_translate_iface()
{
#ifdef WITH_INTERNATIONAL
  return BLT_translate() && (U.transopts & USER_TR_IFACE);
#else
  return false;
#endif
}

bool BLT_translate_tooltips()
{
#ifdef WITH_INTERNATIONAL
  return BLT_translate() && (U.transopts & USER_TR_TOOLTIPS);
#else
  return false;
#endif
}

bool BLT_translate_reports()
{
#ifdef WITH_INTERNATIONAL
  return BLT_translate() && (U.transopts & USER_TR_REPORTS);
#else
  return false;
#endif
}

bool BLT_translate_new_dataname()
{
#ifdef WITH_INTERNATIONAL
  return BLT_translate() && (U.transopts & USER_TR_NEWDATANAME);
#else
  return false;
#endif
}

/* -------------------------------------------------------------------- */
/** \name Phantom: ZBrush Terms
 * \{ */

static bool is_letter(const char c)
{
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
}

/** Length of a whole-word "face set" (any case of the initials) at `i`, else 0. */
static int face_set_at(const StringRef text, const int64_t i)
{
  if (text.size() - i < 8 || !ELEM(text[i], 'F', 'f') || (i > 0 && is_letter(text[i - 1]))) {
    return 0;
  }
  if (text.substr(i + 1, 4) != "ace " || !ELEM(text[i + 5], 'S', 's') ||
      text.substr(i + 6, 2) != "et")
  {
    return 0;
  }
  /* "Face Sets" keeps its "s"; "face setting" isn't a face set. */
  int64_t end = i + 8;
  if (end < text.size() && text[end] == 's') {
    end++;
  }
  return (end < text.size() && is_letter(text[end])) ? 0 : 8;
}

static std::optional<std::string> phantom_terms_replace(const StringRef text)
{
  std::optional<std::string> result;
  int64_t copied = 0;
  for (int64_t i = 0; i + 8 <= text.size(); i++) {
    if (face_set_at(text, i) == 0) {
      continue;
    }
    if (!result) {
      result.emplace();
    }
    result->append(text.data() + copied, size_t(i - copied));
    result->append(text[i] == 'F' ? "Polygroup" : "polygroup");
    copied = i + 8;
    i += 7;
  }
  if (result) {
    result->append(text.data() + copied, size_t(text.size() - copied));
  }
  return result;
}

/** Renamed texts by original, kept until exit so callers can hold on to them. */
static const std::string *phantom_terms_lookup(const StringRef text)
{
  /* Most texts have no "ace " at all: skip the lock and the copy for those. */
  if (text.find("ace ") == StringRef::not_found) {
    return nullptr;
  }
  static std::mutex mutex;
  static std::unordered_map<std::string, std::optional<std::string>> cache;
  std::lock_guard lock(mutex);
  auto [it, inserted] = cache.try_emplace(std::string(text));
  if (inserted) {
    it->second = phantom_terms_replace(text);
  }
  return it->second ? &*it->second : nullptr;
}

const char *BLT_phantom_terms(const char *text)
{
  if (text == nullptr) {
    return text;
  }
  const std::string *renamed = phantom_terms_lookup(text);
  return renamed ? renamed->c_str() : text;
}

StringRef BLT_phantom_terms(StringRef text)
{
  const std::string *renamed = phantom_terms_lookup(text);
  return renamed ? StringRef(*renamed) : text;
}

/** \} */

template<typename StringT> StringT translate_do(StringT msgctxt, StringT msgid)
{
#ifdef WITH_INTERNATIONAL
  if (BLT_translate()) {
    return BLT_pgettext(msgctxt, msgid);
  }

  return msgid;

#else
  (void)msgctxt;
  return msgid;
#endif
}

const char *BLT_translate_do(const char *msgctxt, const char *msgid)
{
  return translate_do(msgctxt, msgid);
}

StringRef BLT_translate_do(StringRef msgctxt, StringRef msgid)
{
  return translate_do(msgctxt, msgid);
}

template<typename StringT> StringT translate_do_iface(StringT msgctxt, StringT msgid)
{
#ifdef WITH_INTERNATIONAL
  if (BLT_translate_iface()) {
    return BLT_phantom_terms(BLT_pgettext(msgctxt, msgid));
  }

  return BLT_phantom_terms(msgid);

#else
  (void)msgctxt;
  return BLT_phantom_terms(msgid);
#endif
}

const char *BLT_translate_do_iface(const char *msgctxt, const char *msgid)
{
  return translate_do_iface(msgctxt, msgid);
}

StringRef BLT_translate_do_iface(StringRef msgctxt, StringRef msgid)
{
  return translate_do_iface(msgctxt, msgid);
}

template<typename StringT> StringT translate_do_tooltip(StringT msgctxt, StringT msgid)
{
#ifdef WITH_INTERNATIONAL
  if (BLT_translate_tooltips()) {
    return BLT_phantom_terms(BLT_pgettext(msgctxt, msgid));
  }

  return BLT_phantom_terms(msgid);

#else
  (void)msgctxt;
  return BLT_phantom_terms(msgid);
#endif
}

const char *BLT_translate_do_tooltip(const char *msgctxt, const char *msgid)
{
  return translate_do_tooltip(msgctxt, msgid);
}

StringRef BLT_translate_do_tooltip(StringRef msgctxt, StringRef msgid)
{
  return translate_do_tooltip(msgctxt, msgid);
}

template<typename StringT> StringT translate_do_report(StringT msgctxt, StringT msgid)
{
#ifdef WITH_INTERNATIONAL
  if (BLT_translate_reports()) {
    return BLT_phantom_terms(BLT_pgettext(msgctxt, msgid));
  }

  return BLT_phantom_terms(msgid);

#else
  (void)msgctxt;
  return BLT_phantom_terms(msgid);
#endif
}

const char *BLT_translate_do_report(const char *msgctxt, const char *msgid)
{
  return translate_do_report(msgctxt, msgid);
}

StringRef BLT_translate_do_report(StringRef msgctxt, StringRef msgid)
{
  return translate_do_report(msgctxt, msgid);
}

template<typename StringT> StringT translate_do_new_dataname(StringT msgctxt, StringT msgid)
{
#ifdef WITH_INTERNATIONAL
  if (BLT_translate_new_dataname()) {
    return BLT_pgettext(msgctxt, msgid);
  }

  return msgid;

#else
  (void)msgctxt;
  return msgid;
#endif
}

const char *BLT_translate_do_new_dataname(const char *msgctxt, const char *msgid)
{
  return translate_do_new_dataname(msgctxt, msgid);
}

StringRef BLT_translate_do_new_dataname(StringRef msgctxt, StringRef msgid)
{
  return translate_do_new_dataname(msgctxt, msgid);
}

}  // namespace blender
