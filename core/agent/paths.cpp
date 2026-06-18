// core/agent/paths.cpp — see paths.h. Mirrors engine/paths.py exactly.
#include "agent/paths.h"

#include <cstdlib>
#include <string>

namespace fs = std::filesystem;

namespace aicad {
namespace agent {

namespace {
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
}  // namespace

fs::path appdata_root() {
    const char* base = std::getenv("APPDATA");
    fs::path root;
    if (base && base[0]) {
        root = fs::path(base);
    } else {
        root = expand_home_roaming();
    }
    return root / "AI-CAD";
}

fs::path paths_root() { return appdata_root(); }
fs::path paths_cache() { return appdata_root() / "cache"; }
fs::path paths_config() { return appdata_root() / "config.json"; }

}  // namespace agent
}  // namespace aicad
