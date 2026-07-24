// core/agent/copilot_modes.cpp — see copilot_modes.h. Loads the frozen
// contracts/copilot_modes.json and applies the exact Python semantics.
#include "agent/copilot_modes.h"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <sstream>

#include <nlohmann/json.hpp>

namespace fs = std::filesystem;

namespace chudware {
namespace agent {

namespace {

std::string py_strip(const std::string& s) {
    const char* ws = " \t\r\n\f\v";
    const size_t a = s.find_first_not_of(ws);
    if (a == std::string::npos) return "";
    const size_t b = s.find_last_not_of(ws);
    return s.substr(a, b - a + 1);
}

std::string py_strip_lower(std::string s) {
    s = py_strip(s);
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    return s;
}

bool read_set(const nlohmann::json& j, const char* key, std::set<std::string>& out) {
    if (!j.contains(key) || !j[key].is_array()) return false;
    for (const auto& v : j[key]) {
        if (!v.is_string()) return false;
        out.insert(v.get<std::string>());
    }
    return true;
}

}  // namespace

fs::path CopilotModes::default_path() {
    if (const char* env = std::getenv("CHUDWARE_COPILOT_MODES_JSON"); env && env[0])
        return fs::path(env);
    if (const char* dir = std::getenv("CHUDWARE_CONTRACTS_DIR"); dir && dir[0])
        return fs::path(dir) / "copilot_modes.json";
    // Repo-relative fallback — same CWD convention as roles.cpp's agent/prompts.
    return fs::path("contracts") / "copilot_modes.json";
}

std::optional<CopilotModes> CopilotModes::load(const std::string& explicit_path) {
    const fs::path path = explicit_path.empty() ? default_path() : fs::path(explicit_path);
    std::ifstream fh(path, std::ios::binary);
    if (!fh) return std::nullopt;
    std::ostringstream ss;
    ss << fh.rdbuf();
    const nlohmann::json j = nlohmann::json::parse(ss.str(), nullptr, false);
    if (j.is_discarded() || !j.is_object()) return std::nullopt;

    CopilotModes cm;
    if (!read_set(j, "modes", cm.modes) || !read_set(j, "unrestricted_modes", cm.unrestricted) ||
        !read_set(j, "shared_default_tools", cm.shared_defaults) ||
        !read_set(j, "mutating_tools", cm.mutating_tools) ||
        !read_set(j, "edit_denied", cm.edit_denied) ||
        !read_set(j, "swarm_extra_tools", cm.swarm_extras))
        return std::nullopt;
    if (!j.contains("mode_tools") || !j["mode_tools"].is_object()) return std::nullopt;
    const auto& mt = j["mode_tools"];
    if (!read_set(mt, "ask", cm.ask_tools) || !read_set(mt, "plan", cm.plan_tools) ||
        !read_set(mt, "edit", cm.edit_tools))
        return std::nullopt;
    cm.default_mode = j.value("default_mode", std::string("agent"));
    // Optional keys (older contracts may omit): absent -> empty set, mirroring
    // Python's ``_CONTRACT.get(key, ())`` fallback in copilot_mode_tools.py.
    read_set(j, "agent_default_tools", cm.agent_defaults);
    read_set(j, "subagent_default_tools", cm.subagent_defaults);
    if (cm.default_mode.empty() || !cm.modes.count(cm.default_mode)) return std::nullopt;
    return cm;
}

std::string copilot_normalize_mode(const CopilotModes& cm, const std::string& mode) {
    const std::string m = py_strip_lower(mode.empty() ? cm.default_mode : mode);
    return cm.modes.count(m) ? m : cm.default_mode;
}

std::optional<std::vector<std::string>> copilot_apply_mode_tools(
    const CopilotModes& cm, const std::string& mode,
    const std::vector<std::string>& role_tools, bool plan_only) {
    std::string m = copilot_normalize_mode(cm, mode);
    if (plan_only) m = "plan";

    std::set<std::string> role_set;
    for (const auto& t : role_tools)
        if (!t.empty()) role_set.insert(t);

    if (m == "swarm") {
        if (role_set.empty()) return std::nullopt;
        role_set.insert(cm.swarm_extras.begin(), cm.swarm_extras.end());
        role_set.insert(cm.shared_defaults.begin(), cm.shared_defaults.end());
        role_set.insert(cm.agent_defaults.begin(), cm.agent_defaults.end());
        return std::vector<std::string>(role_set.begin(), role_set.end());
    }
    if (m == "agent") {
        if (role_set.empty()) return std::nullopt;
        role_set.insert(cm.shared_defaults.begin(), cm.shared_defaults.end());
        role_set.insert(cm.agent_defaults.begin(), cm.agent_defaults.end());
        return std::vector<std::string>(role_set.begin(), role_set.end());
    }

    std::set<std::string> allowed;
    if (m == "plan") {
        // Agent-broad minus geometry commit mutators; shared floor re-added below.
        allowed = role_set.empty() ? cm.plan_tools : role_set;
        for (const auto& t : cm.mutating_tools) allowed.erase(t);
    } else {
        // Restricted modes: ask / edit — mode list ∩ role (or mode list).
        const std::set<std::string>& mode_set = (m == "ask") ? cm.ask_tools : cm.edit_tools;
        if (role_set.empty()) {
            allowed = mode_set;
        } else {
            for (const auto& t : role_set)
                if (mode_set.count(t)) allowed.insert(t);
        }
        if (m == "edit")
            for (const auto& t : cm.edit_denied) allowed.erase(t);
        if (m == "ask")
            for (const auto& t : cm.mutating_tools) allowed.erase(t);
    }
    // Shared floor always wins for restricted modes (doc lifecycle + advise +
    // web + meta enable_tools / list_tool_categories).
    allowed.insert(cm.shared_defaults.begin(), cm.shared_defaults.end());
    return std::vector<std::string>(allowed.begin(), allowed.end());
}

bool copilot_tool_allowed(const CopilotModes& cm, const std::string& tool,
                          const std::string& mode,
                          const std::vector<std::string>& role_tools, bool plan_only) {
    const std::string name = py_strip(tool);  // Python strips only — no lowercase
    if (name.empty()) return false;
    const auto allowed = copilot_apply_mode_tools(cm, mode, role_tools, plan_only);
    if (!allowed) return true;
    return std::find(allowed->begin(), allowed->end(), name) != allowed->end();
}

}  // namespace agent
}  // namespace chudware
