// Provider brand identity for the Settings -> Providers UI (colors + a small drawn badge).
//
// The bundled UI font has no brand-logo glyphs, and shipping raster logos would bloat the
// binary + carry trademark baggage, so a provider's "logo" here is a rounded brand-colored
// badge with a short ASCII mark (font-safe) — enough to make the list scannable and branded.
// Unknown ids get a stable hashed color + first-letter mark so nothing renders blank.
#pragma once

#include <string>

#include "imgui.h"

namespace ui {

// Brand accent color (RGB, 0..1, 3 floats) for an OMP provider id ("anthropic", "openai",
// "google", "xai", "kimi-code", ...). Stable hashed fallback for unknown ids. Never null.
const float* providerBrandColor(const std::string& providerId);

// Short 1-2 char ASCII mark drawn inside the badge ("A" anthropic, "O" openai, "G" gemini,
// "X" xai, ...). Never null; falls back to the first uppercased id letter.
const char* providerBrandMark(const std::string& providerId);

// Draw a filled rounded brand badge (brand color + white mark) of side `side` px with its
// top-left at `pos`, via `dl`. Cursor is untouched — the caller reserves layout space.
void drawProviderLogo(ImDrawList* dl, ImVec2 pos, float side, const std::string& providerId);

}  // namespace ui
