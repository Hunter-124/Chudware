// core/agent/paths.cpp — see paths.h. Mirrors engine/paths.py exactly.
#include "agent/paths.h"

#include <cstdlib>
#include <string>

namespace fs = std::filesystem;

namespace aicad {
namespace agent {

namespace {
#ifdef _WIN32
// os.path.expanduser(r"~\AppData\Roaming") — expand "~" to the user profile.
fs::path expand_home_roaming() {
    const char* up = std::getenv("USERPROFILE");
    std::string home;
    if (up && up[0]) {
        home = up;
    } else {
        const char* hd = std::getenv("HOMEDRIVE");
        const char* hp = std::getenv("HOMEPATH");
        if (hd && hp) home = std::string(hd) + hp;
    }
    // Python builds "~\AppData\Roaming" then expands "~"; the tail is appended verbatim.
    return fs::path(home) / "AppData" / "Roaming";
}
#endif
}  // namespace

fs::path appdata_root() {
#ifdef _WIN32
    const char* base = std::getenv("APPDATA");
    fs::path root;
    if (base && base[0]) {
        root = fs::path(base);
    } else {
        root = expand_home_roaming();
    }
    return root / "AI-CAD";
#else
    // Linux/macOS: the XDG data home (else ~/.local/share), matching the host's
    // bootstrap appdataRoot() so the C++ core and the GUI agree on the data dir.
    if (const char* xdg = std::getenv("XDG_DATA_HOME"); xdg && xdg[0])
        return fs::path(xdg) / "AI-CAD";
    if (const char* home = std::getenv("HOME"); home && home[0])
        return fs::path(home) / ".local" / "share" / "AI-CAD";
    return fs::path(".") / "AI-CAD";
#endif
}

fs::path paths_root() { return appdata_root(); }
fs::path paths_cache() { return appdata_root() / "cache"; }
fs::path paths_config() { return appdata_root() / "config.json"; }

}  // namespace agent
}  // namespace aicad
