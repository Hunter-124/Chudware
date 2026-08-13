// core/agent/roles.h — native role-prompt registry.
//
// Developer builds read authored prompts from CHUDWARE_PROMPTS_DIR (or the
// per-call prompts_dir). Packaged builds ignore those built-in paths and read the
// generated protected-assets string table. Both modes preserve user overrides at
// %APPDATA%/Chudware/prompts/<role>.md.
#pragma once

#include <set>
#include <string>
#include <utility>
#include <vector>
#include <nlohmann/json.hpp>

namespace chudware {
namespace agent {

using nlohmann::json;

extern const std::vector<std::string> kRoles;  // canonical order; generated/dev extras follow.

// (text, source). Ops roles get the ops cheatsheet appended. In developer builds
// `prompts_dir` overrides CHUDWARE_PROMPTS_DIR; packaged builds ignore it. When
// `solo` is true, modeler/fixer get the slim ops_cheatsheet_solo.md instead of the
// full sheet. `tool_scope_full` forces the full sheet. Entries in
// `tool_scope_classes` append the matching generated/developer cheatsheet.
std::pair<std::string, std::string> load_prompt(const std::string& role,
                                                const std::string& prompts_dir = "",
                                                bool solo = false,
                                                bool tool_scope_full = false,
                                                const std::set<std::string>& tool_scope_classes = {});

// sha256(text utf-8).hexdigest()[:12].
std::string prompt_hash(const std::string& text);

// Per-role records for the GUI editor: {role, path, override_path, text, overridden}.
json get_prompts_v2(const std::string& prompts_dir = "");

// Editable cheat-sheet markdown for feature_type (stripped), or "" if none.
std::string load_cheatsheet(const std::string& feature_type, const std::string& prompts_dir = "");

}  // namespace agent
}  // namespace chudware
