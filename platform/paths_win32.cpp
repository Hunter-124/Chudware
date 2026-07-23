#include "paths.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shlobj.h>

#include <cstdlib>
#include <filesystem>
#include <string>

namespace platform {
namespace {

std::string narrow(const std::wstring& w) {
    if (w.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    if (n <= 0) return {};
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), s.data(), n, nullptr, nullptr);
    return s;
}

}  // namespace

std::string exeDir() {
    wchar_t buf[MAX_PATH];
    DWORD n = GetModuleFileNameW(nullptr, buf, MAX_PATH);
    std::wstring w(buf, n);
    size_t s = w.find_last_of(L"\\/");
    if (s != std::wstring::npos) w = w.substr(0, s);
    return narrow(w);
}

std::string appdataDir() {
    const char* a = std::getenv("APPDATA");
    return std::string(a ? a : ".");
}

std::string appdataRoot() {
    // T-G10 alias rule (mirrors engine/paths.py active_root()): a machine with an
    // existing legacy Chudware dir (and no Chudware dir yet) keeps using it wholesale —
    // it may hold the live runtime venv/config, and venvs do not survive relocation.
    // Fresh installs get Chudware. No destructive migration.
    static const std::string cached = [] {
        const std::string base = appdataDir();
        const std::string neu = joinPath(base, "Chudware");
        const std::string legacy = joinPath(base, "Chudware");
        std::error_code ec;
        if (!std::filesystem::exists(neu, ec) && std::filesystem::exists(legacy, ec))
            return legacy;
        return neu;
    }();
    return cached;
}

std::string configPath() { return joinPath(appdataRoot(), "config.json"); }

std::string cacheDir() { return joinPath(appdataRoot(), "cache"); }

std::string tempDir() {
    const char* t = std::getenv("TEMP");
    if (t && *t) return std::string(t);
    t = std::getenv("TMP");
    if (t && *t) return std::string(t);
    return ".";
}

std::string imguiIniPath() { return joinPath(appdataRoot(), "imgui_gui_v3.ini"); }

std::string documentsDir() {
    PWSTR docs = nullptr;
    std::string out;
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_Documents, 0, nullptr, &docs)) && docs)
        out = narrow(docs);
    if (docs) CoTaskMemFree(docs);
    if (!out.empty()) return out;
    const char* up = std::getenv("USERPROFILE");
    if (up && *up) return joinPath(up, "Documents");
    return appdataDir();
}

std::string resourcesDir() { return joinPath(exeDir(), "resources"); }

std::string assetsDir() { return joinPath(exeDir(), "assets"); }

std::string joinPath(const std::string& a, const std::string& b) {
    if (a.empty()) return b;
    if (b.empty()) return a;
    char last = a.back();
    if (last == '/' || last == '\\') return a + b;
    return a + "/" + b;
}

std::string appdataDisplayLabel() { return "%APPDATA%\\Chudware (legacy Chudware dir honored)"; }

}  // namespace platform
