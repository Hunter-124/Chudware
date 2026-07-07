// cef/cef_platform_windows.cpp — Windows half of the CEF platform seam.
// SCAFFOLDING (docs/cef_webview.md phase 2); compiled only with AI_CAD_ENABLE_CEF.
#include "cef_platform.h"

#include <windows.h>
#include <shlobj.h>   // SHGetFolderPathA, CSIDL_LOCAL_APPDATA

#include <cstdlib>
#include <string>

namespace ui::cef_platform {

CefMainArgs main_args(int /*argc*/, char** /*argv*/) {
    // Windows CEF ignores argc/argv and takes the module handle.
    return CefMainArgs(::GetModuleHandle(nullptr));
}

std::string executable_dir() {
    char buf[MAX_PATH];
    const DWORD n = ::GetModuleFileNameA(nullptr, buf, MAX_PATH);
    if (n == 0 || n == MAX_PATH) return ".";
    std::string p(buf, n);
    const auto slash = p.find_last_of("\\/");
    return slash == std::string::npos ? std::string(".") : p.substr(0, slash);
}

std::string helper_path() {
    return executable_dir() + "\\ai_cad_cef_helper.exe";
}

std::string cache_root() {
    char buf[MAX_PATH];
    if (SUCCEEDED(::SHGetFolderPathA(nullptr, CSIDL_LOCAL_APPDATA, nullptr, 0, buf)))
        return std::string(buf) + "\\ai-cad\\cef_cache";
    if (const char* la = std::getenv("LOCALAPPDATA"); la && la[0])
        return std::string(la) + "\\ai-cad\\cef_cache";
    return executable_dir() + "\\cef_cache";   // last-resort fallback
}

void configure_settings(CefSettings& settings) {
    // TODO(cef): for the sandbox, allocate cef_sandbox_info via
    // cef_sandbox_win.h and thread it through CefExecuteProcess/CefInitialize.
    // Scaffolding runs unsandboxed to match the Linux single-binary layout.
    settings.no_sandbox = 1;
}

}  // namespace ui::cef_platform
