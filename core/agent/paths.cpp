// core/agent/paths.cpp — see paths.h. Mirrors engine/paths.py exactly.
#include "agent/paths.h"

#include <cstdlib>
#include <string>

namespace fs = std::filesystem;

namespace chudware {
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
// Alias rule (mirrors engine/paths.py active_root() and app_state.cpp appdataAppDir()):
// once Chudware holds a real install marker (config.json) it wins; otherwise an existing
// legacy install dir is kept using wholesale (it holds the live venv/config — venvs do not
// survive relocation), preferring the most recent legacy name (Billet) over the oldest
// (AI-CAD). Fresh machines get Chudware. No destructive migration; the old dir is never moved.
fs::path pick_app_dir(const fs::path& base) {
    const fs::path neu = base / "Chudware";
    std::error_code ec;
    if (fs::exists(neu / "config.json", ec)) return neu;
    // Prefer a legacy dir that holds a real install marker (config.json), newest name
    // first — a stray cache-only dir must never shadow a real install.
    for (const char* legacy : {"Billet", "AI-CAD"})
        if (fs::exists(base / legacy / "config.json", ec)) return base / legacy;
    // No marker anywhere: keep an existing legacy dir wholesale if present.
    for (const char* legacy : {"Billet", "AI-CAD"})
        if (fs::exists(base / legacy, ec)) return base / legacy;
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
}  // namespace chudware
