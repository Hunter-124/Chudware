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

namespace {
// T-G10 alias rule (mirrors engine/paths.py active_root()): a machine with an
// existing legacy AI-CAD dir (and no ShopCraft dir yet) keeps using it wholesale;
// fresh installs get ShopCraft. No destructive migration.
fs::path pick_app_dir(const fs::path& base) {
    const fs::path neu = base / "ShopCraft";
    const fs::path legacy = base / "AI-CAD";
    std::error_code ec;
    // Legacy wins whenever it exists and the new dir holds no real install marker
    // (config.json) — a stray cache-only ShopCraft dir must not flip the root
    // (mirrors engine/paths.py active_root(), hardened 2026-07-04).
    if (fs::exists(legacy, ec) && !fs::exists(neu / "config.json", ec)) return legacy;
    return neu;
}
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
    return pick_app_dir(root);
#else
    // Linux/macOS: the XDG data home (else ~/.local/share), matching the GUI's
    // platform::appdataRoot() so the C++ core and the GUI agree on the data dir.
    if (const char* xdg = std::getenv("XDG_DATA_HOME"); xdg && xdg[0])
        return pick_app_dir(fs::path(xdg));
    if (const char* home = std::getenv("HOME"); home && home[0])
        return pick_app_dir(fs::path(home) / ".local" / "share");
    return pick_app_dir(fs::path("."));
#endif
}

fs::path paths_root() { return appdata_root(); }
fs::path paths_cache() { return appdata_root() / "cache"; }
fs::path paths_config() { return appdata_root() / "config.json"; }

}  // namespace agent
}  // namespace aicad
