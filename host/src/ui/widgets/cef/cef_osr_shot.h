// cef/cef_osr_shot.h — headless OSR render-to-PNG verification hook.
// SCAFFOLDING (docs/cef_webview.md phase 2); available only with AI_CAD_ENABLE_CEF.
#pragma once

#include <string>

namespace ui::cef {

// Create a windowless CEF browser, navigate to `url`, capture the first painted
// frame, and write it to `out_path` as a PNG. Requires initialize() to have
// succeeded. Returns true on a non-empty capture. Proves the OSR pipeline with
// no display/GL — the basis of `AI-CAD --cef-shot`.
bool render_url_to_png(const std::string& url, int w, int h, const std::string& out_path);

}  // namespace ui::cef
