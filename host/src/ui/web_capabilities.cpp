// See web_capabilities.h. The kWebGaps table is the ONE place web-degradation copy lives;
// adding a row gates every surface that asks about that id.
#include "web_capabilities.h"

#include <cerrno>
#include <cstring>
#include <string>
#ifdef __EMSCRIPTEN__
#include "engine_backend.h"
#include "ipc_client.h"
#endif

#if defined(__EMSCRIPTEN__)
#include <emscripten/emscripten.h>
#elif defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <spawn.h>
#include <sys/wait.h>
#include <thread>

extern char** environ;
#endif

namespace ui {
namespace {

struct WebGap {
    const char* id;      // ribbon cmd_id or panel surface id
    const char* reason;  // shown in the disabled tooltip / toast / panel body
};

// Keep in sync with contracts/web_parity.md (web_unsupported — do not edit; the
// integrator owns it) and with the CEF-less WASM build (CHUDWARE_ENABLE_CEF off).
constexpr WebGap kWebGaps[] = {
    // CEF-dependent: no embedded Chromium in the browser build.
    {"web_browser",
     "The in-app browser is desktop-only — the web app has no embedded Chromium. "
     "Open external links in a normal browser tab instead."},
    {"vendor_signin",
     "Vendor sign-in is desktop-only: it drives an embedded browser session the web "
     "app cannot host. Sign in from the desktop app."},
    {"account_signin",
     "Provider sign-in and API keys are unavailable in the web app: the shared engine "
     "cannot hold provider credentials per user. Use the desktop app for online AI."},
};

bool allowedExternalHttpUrl(const std::string& url) {
    if (url.size() > 16384) return false;
    const auto hasScheme = [&url](const char* scheme, size_t length) {
        if (url.size() < length) return false;
        for (size_t i = 0; i < length; ++i) {
            char c = url[i];
            if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
            if (c != scheme[i]) return false;
        }
        return true;
    };
    size_t authority = std::string::npos;
    if (hasScheme("https://", 8))
        authority = 8;
    else if (hasScheme("http://", 7))
        authority = 7;
    else
        return false;

    const size_t authorityEnd = url.find_first_of("/?#", authority);
    const size_t end = authorityEnd == std::string::npos ? url.size() : authorityEnd;
    if (end == authority || url.find('@', authority) < end) return false;
    for (unsigned char c : url)
        if (c <= 0x20 || c == 0x7f || c == '\\' || c == '"') return false;
    return true;
}

}  // namespace

const char* webGapReason(const char* surfaceId) {
    if (!webBuild() || !surfaceId) return nullptr;
#ifdef __EMSCRIPTEN__
    const auto* connection = engine::client();
    if (!connection || !engine::webCellProtocolReady(connection->serverInfo()))
        return "CAD and AI are unavailable for this workspace connection.";
#endif
    for (const WebGap& g : kWebGaps)
        if (std::strcmp(g.id, surfaceId) == 0) return g.reason;
    return nullptr;
}

bool openExternalHttpUrl(const std::string& url) {
    if (!allowedExternalHttpUrl(url)) return false;
#if defined(__EMSCRIPTEN__)
    EM_ASM({
        const url = UTF8ToString($0);
        window.open(url, '_blank', 'noopener,noreferrer');
    }, url.c_str());
    return true;
#elif defined(_WIN32)
    const int wideSize = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, url.data(),
                                              static_cast<int>(url.size()), nullptr, 0);
    if (wideSize <= 0) return false;
    std::wstring wideUrl(static_cast<size_t>(wideSize), L'\0');
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, url.data(),
                            static_cast<int>(url.size()), wideUrl.data(), wideSize) != wideSize)
        return false;

    wchar_t systemDir[MAX_PATH] = {};
    const UINT systemLen = GetSystemDirectoryW(systemDir, MAX_PATH);
    if (systemLen == 0 || systemLen >= MAX_PATH) return false;
    std::wstring executable(systemDir, systemLen);
    executable += L"\\rundll32.exe";
    std::wstring handler(systemDir, systemLen);
    handler += L"\\url.dll";
    std::wstring command = L"\"" + executable + L"\" \"" + handler +
                           L"\",FileProtocolHandler \"" + wideUrl + L"\"";
    command.push_back(L'\0');  // CreateProcessW may modify its command-line buffer.
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(executable.c_str(), command.data(), nullptr, nullptr, FALSE,
                        CREATE_NO_WINDOW, nullptr, nullptr, &startup, &process))
        return false;
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return true;
#else
#if defined(__APPLE__)
    const char* opener = "open";
#else
    const char* opener = "xdg-open";
#endif
    char* const argv[] = {
        const_cast<char*>(opener),
        const_cast<char*>(url.c_str()),
        nullptr,
    };
    pid_t child = 0;
    if (posix_spawnp(&child, opener, nullptr, nullptr, argv, environ) != 0) return false;
    std::thread([child]() {
        int status = 0;
        while (waitpid(child, &status, 0) < 0 && errno == EINTR) {}
    }).detach();
    return true;
#endif
}

}  // namespace ui
