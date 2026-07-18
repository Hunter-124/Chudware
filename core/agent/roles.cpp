// core/agent/roles.cpp — see roles.h. Mirrors roles.py.
#include "agent/roles.h"

#ifdef _WIN32
#include <windows.h>
#include <bcrypt.h>
#endif

#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <set>
#include <sstream>
#include <vector>

#include "agent/paths.h"

namespace fs = std::filesystem;

namespace aicad {
namespace agent {

const std::vector<std::string> kRoles = {
    "intake", "planner", "modeler", "reviewer", "fixer", "memory",
    "decomposer", "executor", "mission_planner", "reporter", "advisor"};

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
    if (t == "hole" || t == "bolt_circle") return "hole";  // hole.md documents both
    if (t == "linear_pattern" || t == "circular_pattern" || t == "pattern") return "pattern";
    if (t == "drawing") return "drawing";  // W17: 2D engineering-drawing authoring cheatsheet
    if (t == "helix") return "helix";      // one-op coil/spring path (helix.md)
    if (t == "thread") return "thread";    // one-op ISO thread cut (thread.md)
    if (t == "text") return "text";        // one-op emboss/engrave on a planar face (text.md)
    // Feature FAMILIES (prefix-matched): every sm_/weld_/surf_ type shares one cheatsheet.
    if (t.rfind("sm_", 0) == 0) return "sheetmetal";    // sheet-metal flanges/hems/jogs/flat
    if (t.rfind("weld_", 0) == 0) return "weldment";    // structural-member frames + gusset/cap
    if (t.rfind("surf_", 0) == 0) return "surfacing";   // surface bodies + thicken/knit/trim
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

std::string read_cheatsheet_shared(const std::string& prompts_dir, bool solo) {
    const char* name = solo ? "ops_cheatsheet_solo.md" : "ops_cheatsheet.md";
    return read_text(builtin_dir(prompts_dir) / "shared" / name);
}

// Map a granted tool-scope class to the on-disk cheatsheet stem (no .md).
// weldments (model-facing plural) -> weldment.md on disk.
const char* domain_sheet_stem(const std::string& cls) {
    if (cls == "sheetmetal") return "sheetmetal";
    if (cls == "weldments" || cls == "weldment") return "weldment";
    if (cls == "surfacing") return "surfacing";
    return nullptr;
}

std::string read_domain_cheatsheet(const std::string& prompts_dir, const std::string& cls) {
    const char* stem = domain_sheet_stem(cls);
    if (!stem) return "";
    return py_strip(read_text(builtin_dir(prompts_dir) / "shared" / "cheatsheets" /
                              (std::string(stem) + ".md")));
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
                                                const std::string& prompts_dir,
                                                bool solo,
                                                bool tool_scope_full,
                                                const std::set<std::string>& tool_scope_classes) {
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
        // Solo single-part runs get the slim cheatsheet; Standard/Complex keep full.
        // tool_scope_full (mid-run escalation via request_tools) forces the full sheet.
        const bool use_solo = solo && !tool_scope_full;
        std::string cs = read_cheatsheet_shared(prompts_dir, use_solo);
        // Fall back to the full sheet if the solo file is missing (older installs).
        if (cs.empty() && use_solo) cs = read_cheatsheet_shared(prompts_dir, /*solo=*/false);
        if (!cs.empty()) text = py_rstrip(text) + "\n\n---\n" + cs;
        // Append any granted domain context sheets (sheetmetal/weldments/surfacing).
        for (const std::string& cls : tool_scope_classes) {
            if (cls == "full_ops") continue;  // handled by tool_scope_full above
            std::string dom = read_domain_cheatsheet(prompts_dir, cls);
            if (dom.empty()) continue;  // missing file = silent skip
            text = py_rstrip(text) + "\n\n---\nDOMAIN CHEATSHEET (" + cls + "):\n\n" + dom;
        }
    }
    if (is_catalog_role(role)) {
        std::string hw = read_hardware_catalog(prompts_dir);
        if (!hw.empty())
            text = py_rstrip(text) + "\n\n---\nSTANDARD HARDWARE CATALOG (use these real "
                   "dimensions when the request names off-the-shelf parts):\n\n" + hw;
    }
    return {text, source};
}

#ifndef _WIN32
// Self-contained SHA-256 (public domain) for the non-Windows prompt_hash path, so
// the agent layer needs no OpenSSL dependency and the digest is bit-identical to the
// Windows BCrypt path (and the Python hashlib oracle prompt_hash mirrors).
inline uint32_t sha_ror(uint32_t x, uint32_t n) { return (x >> n) | (x << (32 - n)); }
void sha256(const unsigned char* data, size_t len, unsigned char out[32]) {
    static const uint32_t K[64] = {
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
        0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
        0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
        0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
        0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
        0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
        0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
        0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
    uint32_t h[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                     0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    std::vector<unsigned char> msg(data, data + len);
    const uint64_t bitlen = static_cast<uint64_t>(len) * 8;
    msg.push_back(0x80);
    while (msg.size() % 64 != 56) msg.push_back(0x00);
    for (int i = 7; i >= 0; --i)
        msg.push_back(static_cast<unsigned char>((bitlen >> (i * 8)) & 0xff));
    for (size_t off = 0; off < msg.size(); off += 64) {
        uint32_t w[64];
        for (int i = 0; i < 16; ++i)
            w[i] = (static_cast<uint32_t>(msg[off + i * 4]) << 24) |
                   (static_cast<uint32_t>(msg[off + i * 4 + 1]) << 16) |
                   (static_cast<uint32_t>(msg[off + i * 4 + 2]) << 8) |
                   (static_cast<uint32_t>(msg[off + i * 4 + 3]));
        for (int i = 16; i < 64; ++i) {
            uint32_t s0 = sha_ror(w[i - 15], 7) ^ sha_ror(w[i - 15], 18) ^ (w[i - 15] >> 3);
            uint32_t s1 = sha_ror(w[i - 2], 17) ^ sha_ror(w[i - 2], 19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16] + s0 + w[i - 7] + s1;
        }
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
        for (int i = 0; i < 64; ++i) {
            uint32_t S1 = sha_ror(e, 6) ^ sha_ror(e, 11) ^ sha_ror(e, 25);
            uint32_t ch = (e & f) ^ (~e & g);
            uint32_t t1 = hh + S1 + ch + K[i] + w[i];
            uint32_t S0 = sha_ror(a, 2) ^ sha_ror(a, 13) ^ sha_ror(a, 22);
            uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
            uint32_t t2 = S0 + maj;
            hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
        }
        h[0] += a; h[1] += b; h[2] += c; h[3] += d;
        h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
    }
    for (int i = 0; i < 8; ++i) {
        out[i * 4] = static_cast<unsigned char>((h[i] >> 24) & 0xff);
        out[i * 4 + 1] = static_cast<unsigned char>((h[i] >> 16) & 0xff);
        out[i * 4 + 2] = static_cast<unsigned char>((h[i] >> 8) & 0xff);
        out[i * 4 + 3] = static_cast<unsigned char>(h[i] & 0xff);
    }
}
#endif  // !_WIN32

std::string prompt_hash(const std::string& text) {
    unsigned char digest[32] = {0};
#ifdef _WIN32
    BCRYPT_ALG_HANDLE alg = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
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
#else
    sha256(reinterpret_cast<const unsigned char*>(text.data()), text.size(), digest);
#endif
    std::string hex;
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
