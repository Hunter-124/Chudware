// cef/cef_platform.h — the ENTIRE OS-specific surface of the CEF backend.
//
// SCAFFOLDING (docs/cef_webview.md phase 2). CEF itself is the cross-platform
// engine; the only genuinely per-OS bits are collected here so Linux and Windows
// stay at parity through one small seam. If something new turns out to be
// OS-specific, it belongs in this header + the two cef_platform_{linux,windows}
// implementations — never scattered through the render/app code.
#pragma once

#include <string>

#include "include/cef_app.h"  // CefMainArgs, CefSettings, CefRefPtr

namespace ui::cef_platform {

// Build CefMainArgs from the process entry point. POSIX takes argc/argv; Windows
// takes the HINSTANCE. main()/wWinMain() pass what they have; the .cpp for the
// other platform ignores the unused parameter.
CefMainArgs main_args(int argc, char** argv);

// Absolute path to the directory holding the AI-CAD executable — CEF resources
// (*.pak, locales/, libcef) and the subprocess helper are staged next to it.
std::string executable_dir();

// Absolute path to the subprocess helper binary next to the app
// (ai_cad_cef_helper[.exe]). Written into CefSettings.browser_subprocess_path.
std::string helper_path();

// Absolute, writable base dir for CEF's own cache/cookie tree, set as
// CefSettings.root_cache_path. CEF 149 REQUIRES every per-site
// CefRequestContextSettings.cache_path to be a child of this dir. Deliberately
// SEPARATE from the Playwright profile dirs (services/browser/profiles) for now:
// two Chromium engines must not share one user-data-dir until the cookie-interop
// pass lands (docs/cef_webview.md). Linux: $XDG_DATA_HOME|~/.local/share/ai-cad/
// cef_cache; Windows: %LOCALAPPDATA%\ai-cad\cef_cache.
std::string cache_root();

// Apply OS-specific CefSettings knobs (e.g. Windows sandbox info, Linux Ozone /
// no-zygote flags). Cross-platform settings (windowless rendering, cache path,
// cookie crypto) are set by cef_app, not here.
void configure_settings(CefSettings& settings);

}  // namespace ui::cef_platform
