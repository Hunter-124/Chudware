// Provider brand identity for the Settings -> Providers UI (colors + a small drawn badge).
//
// A provider's baseline "logo" is a rounded brand-colored badge with a short ASCII mark
// (font-safe) — always available, offline, no committed assets. On top of that, when enabled,
// providerLogosTick() fetches the provider's real brand favicon at runtime (direct HTTPS to a
// PNG-returning favicon resolver — a background non-agent fetch, no Chudcode/agent involvement),
// caches it in the user's appdata, and drawProviderLogo() renders that texture when
// ready — falling back to the badge until then / when disabled / for unknown domains.
#pragma once

#include <string>

#include "imgui.h"
struct AppState;  // core/app_state.h (providerLogosTick needs st.rpc + appdata paths)

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

// Per-frame driver (GL thread): for provider ids drawn since the previous tick, load a cached
// favicon from appdata or kick a ONE-SHOT background fetch, then upload it as a GL texture.
// Throttled to a few fetches per frame; no-op while disabled. Call once per frame from the
// main loop AFTER the UI body — never from a conditionally-drawn panel, or every other call
// site is starved of textures until the user happens to open that panel.
void providerLogosTick(AppState& st);

// Runtime favicon toggle (config.json agent.provider_logos, default true). When off, no network
// fetch happens and the list shows badges only; turning it off also drops cached textures.
bool providerLogosEnabled();
void setProviderLogosEnabled(bool on);

}  // namespace ui
