// core/agent/copilot_modes.h — C++ consumer of contracts/copilot_modes.json.
//
// The Agent|Plan|Swarm|Edit|Ask tool allowlists were historically implemented
// twice (services/agent/copilot_mode_tools.py for the MCP/DAG path, inline mode
// checks here for the in-app run loop) and could diverge. The frozen contract
// (contracts/copilot_modes.md) makes the JSON the single machine-readable
// source BOTH runtimes load; this header is the C++ loader + the exact
// apply_mode_tools / is_tool_allowed semantics of the Python module.
//
// Resolution (mirrors roles.cpp's repo-relative convention):
//   1. $CHUDWARE_COPILOT_MODES_JSON  — explicit file (tests / parity gate)
//   2. $CHUDWARE_CONTRACTS_DIR/copilot_modes.json
//   3. contracts/copilot_modes.json relative to the CWD (repo root in dev;
//      tests run with WORKING_DIRECTORY=repo root like test_agent).
#pragma once

#include <filesystem>
#include <optional>
#include <set>
#include <string>
#include <vector>

namespace chudware {
namespace agent {

struct CopilotModes {
    std::set<std::string> modes;            // agent|plan|swarm|edit|ask
    std::string default_mode;               // "agent"
    std::set<std::string> unrestricted;     // agent, swarm (null allowlist)
    std::set<std::string> shared_defaults;  // floor UNIONed onto restricted modes
    std::set<std::string> ask_tools;
    std::set<std::string> plan_tools;
    std::set<std::string> edit_tools;
    std::set<std::string> mutating_tools;   // deny set: plan role-cards + ask
    std::set<std::string> edit_denied;      // extra deny set: edit
    std::set<std::string> swarm_extras;     // unioned onto swarm role-cards
    // Always-on MAIN-agent loadout (file handling, shell, web fetch) unioned
    // onto the advertised list AND dispatch policy for unrestricted modes
    // (agent/swarm) even when a role card narrows the tool list. Absent key
    // in the contract -> stays empty (mirrors Python's dict.get(key, ())).
    std::set<std::string> agent_defaults;
    // File-handling floor unioned onto every DAG-node/subagent role loadout
    // (parsed for parity; no C++ subagent-dispatch layer consumes it today —
    // run.cpp has no role-card tool-narrowing call site to union it into).
    std::set<std::string> subagent_defaults;

    // Load and parse the contract JSON. std::nullopt when the file is missing
    // or malformed — callers fall back to their pre-contract behavior.
    static std::optional<CopilotModes> load(const std::string& explicit_path = "");

    // The path load("") would read (resolution order in the header comment).
    static std::filesystem::path default_path();
};

// strip()+lower(); absent/unknown → default_mode. Mirrors normalize_mode().
std::string copilot_normalize_mode(const CopilotModes& cm, const std::string& mode);

// Mirrors services/agent/copilot_mode_tools.apply_mode_tools():
//   std::nullopt  — unrestricted (agent/swarm with no role card)
//   vector        — sorted allowed tool names
// plan_only forces plan semantics under any mode.
std::optional<std::vector<std::string>> copilot_apply_mode_tools(
    const CopilotModes& cm, const std::string& mode,
    const std::vector<std::string>& role_tools = {}, bool plan_only = false);

// Mirrors is_tool_allowed(): empty tool name → false; null allowlist → true.
bool copilot_tool_allowed(const CopilotModes& cm, const std::string& tool,
                          const std::string& mode,
                          const std::vector<std::string>& role_tools = {},
                          bool plan_only = false);

}  // namespace agent
}  // namespace chudware
