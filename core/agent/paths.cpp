// core/agent/paths.cpp — see paths.h. Mirrors services/paths.py exactly.
#include "agent/paths.h"

#include <cstdlib>
#include <set>
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
// Copy-if-missing tree merge used by the one-time legacy migration below. Best-effort:
// returns false if anything couldn't be copied (caller then keeps the legacy root for
// this boot rather than stranding data).
bool copy_tree_if_missing(const fs::path& src, const fs::path& dst) {
    std::error_code ec;
    if (fs::is_regular_file(src, ec)) {
        if (fs::exists(dst, ec)) return true;
        fs::create_directories(dst.parent_path(), ec);
        ec.clear();
        fs::copy_file(src, dst, ec);
        if (ec && fs::exists(dst, ec)) ec.clear();  // benign race with a concurrent copy
        return !ec;
    }
    fs::create_directories(dst, ec);
    bool ok = !ec;
    for (const auto& de : fs::directory_iterator(src, ec))
        ok = copy_tree_if_missing(de.path(), dst / de.path().filename()) && ok;
    if (ec) ok = false;
    return ok;
}

// Chudware is the unconditional data dir (mirrors app_state.cpp appdataAppDir() and
// services/paths.py active_root()).
fs::path pick_app_dir(const fs::path& base) {
    const fs::path neu = base / "Chudware";
    std::error_code ec;
    if (fs::exists(neu / "config.json", ec)) return neu;
    // Pre-rename install present (marker: config.json)? Copy it forward once —
    // copy-if-missing, skipping venvs / re-provisionable payloads; the old dir is
    // never moved or deleted. Only a FAILED copy keeps the legacy root in use.
    static const std::set<std::string> kSkip = {
        "runtime", "mcp-venv", "torch-venv", "models", "llamacpp", "cache", "prof",
        "cef_cache", "cef_cache_daemon", "cef_cache_gui",
    };
    for (const char* legacy : {"Billet", "AI-CAD"}) {
        const fs::path old = base / legacy;
        if (!fs::exists(old / "config.json", ec)) continue;
        bool ok = true;
        for (const auto& de : fs::directory_iterator(old, ec)) {
            if (kSkip.count(de.path().filename().string())) continue;
            ok = copy_tree_if_missing(de.path(), neu / de.path().filename()) && ok;
        }
        if (ec) ok = false;
        return ok ? neu : old;
    }
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
