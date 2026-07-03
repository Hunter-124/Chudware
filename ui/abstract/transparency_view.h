// T-U8 Model & telemetry transparency panel — pure view-model (no ImGui/GL includes).
// Rendered by ui/imgui/src/ui/transparency_panel.cpp.
//
// Design rules (Erik-ratified):
//   - Consent default is OFF; changing tier requires explicit user action.
//   - Canary opt-in is OFF by default; the toggle only adds/removes the local user from
//     the opt_in list — it never auto-promotes a canary build to stable.
//   - Which-brain is display-only; the UI shows honest "not serving" state when no routes
//     are active (e.g. offline or H9 table is empty).
//   - Sidecar readout is informational only — no write or delete actions.
#pragma once

#include <string>
#include <vector>

namespace aicad { namespace ui {

// ---- Serving route row -------------------------------------------------------

// One entry from serving_status routes[], displayed as a "which brain" row.
struct ServingRouteRow {
    std::string insertion_point;    // e.g. "copilot", "reviewer"
    std::string provider;           // e.g. "vllm", "openai"
    std::string model;              // base model name / slug
    std::string adapter_version;    // fine-tune adapter tag (may be empty = base model)
    float       canary_pct  = 0.f;  // 0–100 canary traffic fraction
    int         opt_in_count = 0;   // how many users are in the early-adopter list
    bool        user_opted_in = false; // is the local machine ID in the opt_in list?
    bool        is_canary    = false;  // true when canary_pct > 0
};

// ---- Telemetry sidecar entry ------------------------------------------------

// One discovered sidecar directory (T-Q2 crash bundles or T-H7 logging).
struct SidecarEntry {
    std::string label;     // human-readable name, e.g. "Crash bundles (T-Q2)"
    std::string path;      // absolute path to the directory
    bool        exists     = false;
    bool        is_empty   = false;
    int         file_count = 0;    // number of files/bundles (0 when !exists)
};

// ---- Panel state machine ----------------------------------------------------

enum class TransparencyPanelState {
    Idle,       // no data yet
    Loading,    // serving_status RPC in flight
    Ready,      // routes + sidecar data populated
    Error,      // errorMsg set
};

// ---- Panel model ------------------------------------------------------------

struct TransparencyModel {
    TransparencyPanelState state = TransparencyPanelState::Idle;

    // Which-brain data — empty when H9 serving table has no routes or worker offline.
    std::vector<ServingRouteRow> routes;
    bool                         serving_reachable = false; // worker was reachable

    // Telemetry sidecar readout (T-Q2 crash bundles + T-H7 logging dirs).
    std::vector<SidecarEntry> sidecars;
    bool                      sidecars_scanned = false;

    // Consent tier (mirrors first_run.h currentConsentIndex / setConsentIndex).
    // Read fresh from config.json each panel open; held here for frame coherence.
    int consent_index = 0;  // 0=off, 1=meta, 2=full

    // Offline mode — when true, consent is forced off and the radio buttons are disabled.
    bool offline_mode = false;

    std::string errorMsg;
    std::string machine_id;  // stable local identifier used as canary opt_in user_id
};

}} // namespace aicad::ui
