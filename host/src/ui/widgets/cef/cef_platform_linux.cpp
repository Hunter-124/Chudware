// cef/cef_platform_linux.cpp — Linux half of the CEF platform seam.
// SCAFFOLDING (docs/cef_webview.md phase 2); compiled only with CHUDWARE_ENABLE_CEF.
#include "cef_platform.h"

#include <cstdlib>
#include <string>

#include "util/platform_paths.h"

namespace ui::cef_platform {

CefMainArgs main_args(int argc, char** argv) {
    return CefMainArgs(argc, argv);
}

std::string executable_dir() {
    const std::string p = chudware::platform::executablePath();
    const auto slash = p.find_last_of('/');
    return slash == std::string::npos ? std::string(".") : p.substr(0, slash);
}

std::string helper_path() {
    return executable_dir() + "/chudware_cef_helper";
}

namespace {
bool g_daemon_process = false;
}  // namespace

void set_daemon_process(bool daemon) { g_daemon_process = daemon; }

std::string cache_root() {
    // CHUDWARE_CEF_CACHE_ROOT lets a --cef-shot diagnostic run alongside the live
    // app: the process singleton is keyed on root_cache_path, so a distinct dir
    // dodges "CefInitialize FAILED / Opening in existing browser session".
    if (const char* ov = std::getenv("CHUDWARE_CEF_CACHE_ROOT"); ov && ov[0])
        return std::string(ov);
    // GUI vs daemon: separate ProcessSingleton roots (see cef_platform.h).
    const char* leaf = g_daemon_process ? "cef_cache_daemon" : "cef_cache_gui";
    if (const char* xdg = std::getenv("XDG_DATA_HOME"); xdg && xdg[0])
        return std::string(xdg) + "/chudware/" + leaf;
    if (const char* home = std::getenv("HOME"); home && home[0])
        return std::string(home) + "/.local/share/chudware/" + leaf;
    return executable_dir() + "/" + leaf;   // last-resort fallback
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
