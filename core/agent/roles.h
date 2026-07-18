// core/agent/roles.h — C++ port of agent/orchestrator/roles.py.
//
// P52: role registry + editable-prompt resolution. The BUILTIN prompts dir is
// product DATA (not under the binary's control at runtime); it is supplied via
// the AI_CAD_PROMPTS_DIR env var (default "agent/prompts"), and a per-call
// override `prompts_dir` lets the parity gate point C++ at the same files Python
// reads. The user OVERRIDE dir is %APPDATA%/AI-CAD/prompts/<role>.md.
#pragma once

#include <set>
#include <string>
#include <utility>
#include <vector>
#include <nlohmann/json.hpp>

namespace aicad {
namespace agent {

using nlohmann::json;

extern const std::vector<std::string> kRoles;  // 8 roles, in order.

// (text, source). source is the absolute path the text came from. Ops roles get
// the ops cheatsheet appended. `prompts_dir` overrides AI_CAD_PROMPTS_DIR.
// When `solo` is true, modeler/fixer get the slim ops_cheatsheet_solo.md instead
// of the full ~28KB sheet (Standard/Complex keep the full sheet). When
// `tool_scope_full` is true the full sheet is forced even on Solo. Each entry in
// `tool_scope_classes` appends shared/cheatsheets/<class>.md when the file exists
// (sheetmetal.md / weldment.md / surfacing.md — weldments maps to weldment.md).
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
}  // namespace aicad
