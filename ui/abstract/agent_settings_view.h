#pragma once
// Framework-agnostic view-model for the T-W17 Agent Settings panel (Pillar 20
// §2.15, routing.yaml editor). Pure structs, no ImGui/GL includes. Mirrors the
// `view` JSON emitted by services/agent/routing.py (schema aicad.routing/1, §14).
// The panel reads a view, edits a structured model, and writes back through
// routing.py — never free text. Offline/shot mode seeds from a committed
// routing view fixture.

#include <string>
#include <vector>

namespace aicad::ui {

enum class SettingsState {
    Idle,     // not loaded
    Loading,  // reading routing via CLI
    Ready,    // view populated
    Error,    // load/parse failed
};

// One configured model on a source.
struct SettingsModel {
    std::string id;
    std::string tier;   // frontier|strong|local_large|local|cheap
};

// One model source (add/edit/remove target).
struct SettingsSource {
    std::string id;
    std::string kind;   // anthropic|openai|openrouter|lmstudio|llamacpp|ollama
    std::vector<SettingsModel> models;
};

// One editable role assignment row: ordered preference list + live resolution.
struct SettingsAssignment {
    std::string role;                 // includes "overseer" and "default"
    std::vector<std::string> prefs;   // ordered tokens (drag-reorderable)
    std::string resolved_model;       // what it resolves to now ("" if unmet)
    std::string resolved_source;
    std::vector<std::string> warnings; // warn-never-block advisories
};

struct AgentSettingsModel {
    SettingsState state = SettingsState::Idle;
    std::vector<SettingsSource>     sources;
    std::vector<SettingsAssignment> assignments;   // ordered; overseer first, default last
    std::string approval_depth1 = "auto";          // auto|overseer|human
    std::string approval_deeper = "overseer";
    // Non-routing toggles (auto-coincident, preflight, advisory verbosity).
    bool auto_coincident = true;
    bool preflight_enabled = true;
    std::string advisory_verbosity = "normal";     // quiet|normal|verbose
    // Inline validation feedback.
    std::string validationError;   // "" when the current model validates
    std::string statusText;        // footer status
    std::string errorMsg;
};

// Allowed source kinds (NO consumer-OAuth), mirrored from routing.py so the GUI
// offers exactly this set. Kept as data, not hardcoded role logic.
inline const std::vector<std::string>& settingsAllowedKinds() {
    static const std::vector<std::string> k = {
        "anthropic", "openai", "openrouter", "lmstudio", "llamacpp", "ollama"};
    return k;
}

// Symbolic preference tokens the drag-order UI offers.
inline const std::vector<std::string>& settingsSymbolicTokens() {
    static const std::vector<std::string> t = {
        "strongest", "strong", "cheap_local", "cheapest"};
    return t;
}

}  // namespace aicad::ui
