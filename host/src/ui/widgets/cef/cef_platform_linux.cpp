// cef/cef_platform_linux.cpp — Linux half of the CEF platform seam.
// SCAFFOLDING (docs/cef_webview.md phase 2); compiled only with AI_CAD_ENABLE_CEF.
#include "cef_platform.h"

#include <limits.h>
#include <unistd.h>

#include <cstdlib>
#include <string>

namespace ui::cef_platform {

CefMainArgs main_args(int argc, char** argv) {
    return CefMainArgs(argc, argv);
}

std::string executable_dir() {
    char buf[PATH_MAX];
    const ssize_t n = ::readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n <= 0) return ".";
    buf[n] = '\0';
    std::string p(buf);
    const auto slash = p.find_last_of('/');
    return slash == std::string::npos ? std::string(".") : p.substr(0, slash);
}

std::string helper_path() {
    return executable_dir() + "/ai_cad_cef_helper";
}

std::string cache_root() {
    // AI_CAD_CEF_CACHE_ROOT lets a --cef-shot diagnostic run alongside the live
    // app: the process singleton is keyed on root_cache_path, so a distinct dir
    // dodges "CefInitialize FAILED / Opening in existing browser session".
    if (const char* ov = std::getenv("AI_CAD_CEF_CACHE_ROOT"); ov && ov[0])
        return std::string(ov);
    if (const char* xdg = std::getenv("XDG_DATA_HOME"); xdg && xdg[0])
        return std::string(xdg) + "/ai-cad/cef_cache";
    if (const char* home = std::getenv("HOME"); home && home[0])
        return std::string(home) + "/.local/share/ai-cad/cef_cache";
    return executable_dir() + "/cef_cache";   // last-resort fallback
}

void configure_settings(CefSettings& settings) {
    // Ozone/Wayland: OSR is windowless so no native window is created, but the
    // browser process still needs a working Ozone platform. no_sandbox keeps the
    // single-binary layout simple for now (revisit for release hardening).
    settings.no_sandbox = 1;
    // TODO(cef): if the zygote/helper handshake misbehaves on this distro, set
    // --no-zygote via CefCommandLine in cef_app instead of here.
}

}  // namespace ui::cef_platform
