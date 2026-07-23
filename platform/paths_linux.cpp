#include "paths.h"

#include <cstdlib>
#include <filesystem>
#include <string>

#if defined(__APPLE__)
#  include <mach-o/dyld.h>
#else
#  include <unistd.h>
#endif

namespace platform {

std::string exeDir() {
    char buf[4096];
#if defined(__APPLE__)
    uint32_t sz = (uint32_t)sizeof(buf);
    if (_NSGetExecutablePath(buf, &sz) != 0) return ".";
#else
    ssize_t n = readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n <= 0) return ".";
    buf[n] = '\0';
#endif
    std::string p(buf);
    size_t slash = p.find_last_of('/');
    return slash == std::string::npos ? "." : p.substr(0, slash);
}

std::string appdataDir() {
    if (const char* x = std::getenv("XDG_DATA_HOME")) {
        if (*x) return std::string(x);
    }
    if (const char* h = std::getenv("HOME")) {
        if (*h) return std::string(h) + "/.local/share";
    }
    return ".";
}

std::string appdataRoot() {
    // T-G10 alias rule (mirrors engine/paths.py active_root()): a machine with an
    // existing legacy Chudware dir (and no Chudware dir yet) keeps using it wholesale —
    // it may hold the live mcp-venv/config, and venvs do not survive relocation.
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
    const char* t = std::getenv("TMPDIR");
    if (t && *t) return std::string(t);
    t = std::getenv("TEMP");
    if (t && *t) return std::string(t);
    return "/tmp";
}

std::string imguiIniPath() { return joinPath(appdataRoot(), "imgui_gui_v3.ini"); }

std::string documentsDir() {
    if (const char* x = std::getenv("XDG_DOCUMENTS_DIR")) {
        if (*x) return std::string(x);
    }
    if (const char* h = std::getenv("HOME")) {
        if (*h) return joinPath(h, "Documents");
    }
    return appdataDir();
}

std::string resourcesDir() { return joinPath(exeDir(), "resources"); }

std::string assetsDir() { return joinPath(exeDir(), "assets"); }

std::string joinPath(const std::string& a, const std::string& b) {
    if (a.empty()) return b;
    if (b.empty()) return a;
    char last = a.back();
    if (last == '/') return a + b;
    return a + "/" + b;
}

std::string appdataDisplayLabel() {
    return "$XDG_DATA_HOME/Chudware (or ~/.local/share/Chudware; legacy Chudware dir honored)";
}

}  // namespace platform
