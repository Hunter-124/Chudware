// core/agent/roles.cpp — see roles.h. Mirrors roles.py.
#include "agent/roles.h"

#include <windows.h>
#include <bcrypt.h>

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <sstream>

#include "agent/paths.h"

namespace fs = std::filesystem;

namespace aicad {
namespace agent {

const std::vector<std::string> kRoles = {
    "intake", "planner", "modeler", "reviewer", "fixer", "memory",
    "decomposer", "executor", "mission_planner", "reporter"};

namespace {

const std::vector<std::string> kOpsRoles = {"modeler", "fixer"};

// Roles that get the full standard-hardware catalog appended (shared/cheatsheets/hardware.md).
// These are the deciding roles — they choose which off-the-shelf parts to use and what
// pockets/clearance-holes/counterbores to cut, so they need real dimensions. The ops roles
// (modeler/fixer) deliberately do NOT get it: they already carry the large ops_cheatsheet and
// would blow the per-call context cap; they receive the concrete dims through the plan instead.
const std::vector<std::string> kCatalogRoles = {"planner", "mission_planner"};

// _CHEATSHEET_BY_TYPE map (roles.py).
std::string cheatsheet_name(const std::string& feature_type) {
    std::string t;
    for (char c : feature_type) t += static_cast<char>(std::tolower((unsigned char)c));
    if (t == "box" || t == "sphere") return "box";
    if (t == "cylinder" || t == "cone") return "cylinder";
    if (t == "sketch") return "sketch";
    if (t == "extrude") return "extrude";
    if (t == "revolve") return "revolve";
    if (t == "loft") return "loft";
    if (t == "sweep") return "sweep";
    if (t == "mirror") return "mirror";
    if (t == "fillet") return "fillet";
    if (t == "chamfer") return "chamfer";
    if (t == "shell") return "shell";
    if (t == "hole") return "hole";
    if (t == "linear_pattern" || t == "circular_pattern" || t == "pattern") return "pattern";
    if (t == "drawing") return "drawing";  // W17: 2D engineering-drawing authoring cheatsheet
    return "";
}

bool py_is_space(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}
std::string py_strip(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && py_is_space(s[a])) ++a;
    while (b > a && py_is_space(s[b - 1])) --b;
    return s.substr(a, b - a);
}
std::string py_rstrip(const std::string& s) {
    size_t b = s.size();
    while (b > 0 && py_is_space(s[b - 1])) --b;
    return s.substr(0, b);
}

// _read(path): read whole file as text; "" on failure. (No retry needed offline.)
std::string read_text(const fs::path& path) {
    std::ifstream fh(path, std::ios::binary);
    if (!fh) return "";
    std::ostringstream ss;
    ss << fh.rdbuf();
    return ss.str();
}

fs::path builtin_dir(const std::string& prompts_dir) {
    if (!prompts_dir.empty()) return fs::path(prompts_dir);
    const char* env = std::getenv("AI_CAD_PROMPTS_DIR");
    if (env && env[0]) return fs::path(env);
    return fs::path("agent") / "prompts";
}

fs::path override_dir() { return paths_root() / "prompts"; }

fs::path builtin_path(const std::string& role, const std::string& prompts_dir) {
    return builtin_dir(prompts_dir) / (role + ".md");
}
fs::path override_path(const std::string& role) {
    return override_dir() / (role + ".md");
}

std::string read_cheatsheet_shared(const std::string& prompts_dir) {
    return read_text(builtin_dir(prompts_dir) / "shared" / "ops_cheatsheet.md");
}

std::string read_hardware_catalog(const std::string& prompts_dir) {
    return py_strip(read_text(builtin_dir(prompts_dir) / "shared" / "cheatsheets" / "hardware.md"));
}

bool is_ops_role(const std::string& role) {
    for (const auto& r : kOpsRoles)
        if (r == role) return true;
    return false;
}

bool is_catalog_role(const std::string& role) {
    for (const auto& r : kCatalogRoles)
        if (r == role) return true;
    return false;
}

bool is_role(const std::string& role) {
    for (const auto& r : kRoles)
        if (r == role) return true;
    return false;
}

}  // namespace

std::pair<std::string, std::string> load_prompt(const std::string& role,
                                                const std::string& prompts_dir) {
    if (!is_role(role)) throw std::runtime_error("unknown role " + role);
    fs::path ov = override_path(role);
    std::string ov_text;
    std::error_code ec;
    if (fs::exists(ov, ec)) ov_text = py_strip(read_text(ov));
    std::string text, source;
    if (!ov_text.empty()) {
        text = ov_text;
        source = ov.string();
    } else {
        fs::path bp = builtin_path(role, prompts_dir);
        text = read_text(bp);
        source = bp.string();
        if (py_strip(text).empty())
            throw std::runtime_error("role prompt unreadable/empty: " + source);
    }
    if (is_ops_role(role)) {
        std::string cs = read_cheatsheet_shared(prompts_dir);
        if (!cs.empty()) text = py_rstrip(text) + "\n\n---\n" + cs;
    }
    if (is_catalog_role(role)) {
        std::string hw = read_hardware_catalog(prompts_dir);
        if (!hw.empty())
            text = py_rstrip(text) + "\n\n---\nSTANDARD HARDWARE CATALOG (use these real "
                   "dimensions when the request names off-the-shelf parts):\n\n" + hw;
    }
    return {text, source};
}

std::string prompt_hash(const std::string& text) {
    BCRYPT_ALG_HANDLE alg = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    unsigned char digest[32] = {0};
    std::string hex;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0) == 0) {
        if (BCryptCreateHash(alg, &hash, nullptr, 0, nullptr, 0, 0) == 0) {
            BCryptHashData(hash,
                           reinterpret_cast<PUCHAR>(const_cast<char*>(text.data())),
                           static_cast<ULONG>(text.size()), 0);
            BCryptFinishHash(hash, digest, sizeof(digest), 0);
            BCryptDestroyHash(hash);
        }
        BCryptCloseAlgorithmProvider(alg, 0);
    }
    static const char* hexd = "0123456789abcdef";
    for (unsigned char b : digest) {
        hex += hexd[b >> 4];
        hex += hexd[b & 0xF];
    }
    return hex.substr(0, 12);
}

json get_prompts_v2(const std::string& prompts_dir) {
    json out = json::array();
    std::error_code ec;
    for (const std::string& role : kRoles) {
        fs::path bpath = builtin_path(role, prompts_dir);
        fs::path opath = override_path(role);
        std::string ov_text;
        if (fs::exists(opath, ec)) ov_text = py_strip(read_text(opath));
        bool overridden = !ov_text.empty();
        std::string text = overridden ? ov_text : read_text(bpath);
        out.push_back(json{
            {"role", role},
            {"path", bpath.string()},
            {"override_path", opath.string()},
            {"text", text},
            {"overridden", overridden},
        });
    }
    return out;
}

std::string load_cheatsheet(const std::string& feature_type, const std::string& prompts_dir) {
    std::string name = cheatsheet_name(feature_type);
    if (name.empty()) return "";
    return py_strip(read_text(builtin_dir(prompts_dir) / "shared" / "cheatsheets" / (name + ".md")));
}

}  // namespace agent
}  // namespace aicad
