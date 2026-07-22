// core/sourcing/config.cpp — user-config location + loading for the sourcing providers.
// Mirrors engine/paths.py (via agent/paths.cpp, same pick_app_dir alias rule) so the
// C++ providers read the SAME config.json the Python engine reads. Never hard-fails.
#include "sourcing/sourcing.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>

namespace fs = std::filesystem;

namespace aicad {
namespace sourcing {

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
    return fs::path(home) / "AppData" / "Roaming";
}
#endif

// T-G10 alias rule (mirrors engine/paths.py active_root()): an existing legacy AI-CAD
// dir wins while the new Billet dir holds no config.json; fresh installs get Billet.
fs::path pick_app_dir(const fs::path& base) {
    const fs::path neu = base / "Billet";
    const fs::path legacy = base / "AI-CAD";
    std::error_code ec;
    if (fs::exists(legacy, ec) && !fs::exists(neu / "config.json", ec)) return legacy;
    return neu;
}

fs::path appdata_root() {
#ifdef _WIN32
    const char* base = std::getenv("APPDATA");
    fs::path root = (base && base[0]) ? fs::path(base) : expand_home_roaming();
    return pick_app_dir(root);
#else
    if (const char* xdg = std::getenv("XDG_DATA_HOME"); xdg && xdg[0])
        return pick_app_dir(fs::path(xdg));
    if (const char* home = std::getenv("HOME"); home && home[0])
        return pick_app_dir(fs::path(home) / ".local" / "share");
    return pick_app_dir(fs::path("."));
#endif
}

}  // namespace

std::string config_path() { return (appdata_root() / "config.json").string(); }

std::string vendor_parts_dir() { return (appdata_root() / "vendor_parts").string(); }

json load_config() {
    std::error_code ec;
    const fs::path p = config_path();
    if (!fs::exists(p, ec)) return json::object();
    try {
        std::ifstream in(p);
        std::stringstream ss;
        ss << in.rdbuf();
        json data = json::parse(ss.str());
        return data.is_object() ? data : json::object();
    } catch (...) {  // user-editable config; never hard-fail on parse
        return json::object();
    }
}

}  // namespace sourcing
}  // namespace aicad
