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
//
// T-DP11 extension: DPStreamEntry / DPStreamModel add the design-pipeline action stream
// (advisory, audit, council, delegation, tool call, and auto-applied fix events sourced
// from ledger_events RPC).  Schema mirrors the ledger event layout from:
//   services/agent/advisor/engine.py   — advisory items (aicad.advisory/1)
//   services/agent/advisor/overseer.py — audit records (aicad.audit/1)
//   services/agent/councils.py         — council artifacts
//   services/agent/delegation.py       — delegation transitions
//   services/agent/loop.py             — trigger_fired events
//   services/agent/agent_turn.py       — agent_turn events (tool calls)
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

// ============================================================================
// T-DP11: Design-Pipeline Action Stream
// ============================================================================
// One candidate-fix within an advisory item (aicad.advisory/1 §3).
struct DPCandidateFix {
    std::string action;           // e.g. "resize_feature", "review_finding"
    std::string basis;            // why this fix was proposed
    std::string predicted_effect; // what it is expected to change
    std::string confidence;       // "kernel" | "table" | "llm"
};

// One advisory item within an aicad.advisory/1 block.
struct DPAdvisoryItem {
    std::string id;               // e.g. "adv_a1b2c3"
    std::string severity;         // "info" | "warn" | "fail" | "blocker"
    std::string check;            // check name (e.g. "thin_wall", "overlap")
    std::string finding;          // human-readable finding text
    std::vector<DPCandidateFix> candidate_fixes;
    std::string source_kind;      // "kernel" | "table" | "llm"
    std::string source_detail;
};

// One verdict within an aicad.audit/1 record.
struct DPAuditVerdict {
    std::string verdict_class;    // "ok" | geometric class (e.g. "datum")
    std::string severity;         // "ok" | "info" | "warn" | "fail" | "blocker"
    std::string finding;          // human-readable audit finding
    std::string required_action_kind; // "none" | "fix" | "reroute" | "escalate"
};

// Entry kinds for the DP action stream.
enum class DPStreamKind {
    Advisory,       // aicad.advisory/1 block from the foreman
    Audit,          // aicad.audit/1 record from the overseer
    Council,        // council artifact (aicad.advisory council synthesis)
    Delegation,     // delegation node transition
    TriggerFired,   // trigger_fired event from the advisory engine
    AgentTurn,      // agent_turn (tool calls + result)
    Other,          // any other ledger event kind
};

// One entry in the design-pipeline action stream (T-DP11).
// Directly mapped from a raw ledger event (seq/ts/actor/kind/payload).
struct DPStreamEntry {
    int         seq    = 0;
    std::string ts;             // ISO 8601, e.g. "2026-07-05T14:23:01Z"
    std::string actor;          // role id or "foreman" / "overseer" / "harness"
    std::string kind;           // raw ledger event kind
    DPStreamKind display_kind = DPStreamKind::Other;

    // Common summary line (synthesized from payload for compact display)
    std::string summary;        // e.g. "3 advisory items (1 blocker)"

    // Full detail (raw JSON of the payload) — shown in the inspector popover
    std::string detail_json;    // serialized payload for the inspect popover

    // ---- Per-kind structured data (populated selectively) ----

    // Advisory (display_kind == Advisory)
    std::string advisory_stage;
    std::vector<DPAdvisoryItem> advisory_items;
    bool advisory_user_override = false;

    // Audit (display_kind == Audit)
    std::string audit_id;
    std::string audit_stage;
    std::vector<DPAuditVerdict> audit_verdicts;
    std::string audit_model;    // provider:model
    int         audit_tokens = 0;

    // Council (display_kind == Council)
    std::string council_id;
    std::string council_topic;
    std::string council_decision; // chair synthesis decision text
    int         council_dissent_count = 0;

    // Delegation (display_kind == Delegation)
    std::string delegation_node_id;
    std::string delegation_role;
    std::string delegation_status;  // "running" | "done" | "killed" | etc.
    int         delegation_depth = 0;
    // Budget: token_budget, wall_budget_s, tokens_used, elapsed_s
    int         delegation_token_budget = 0;
    int         delegation_tokens_used  = 0;
    float       delegation_wall_budget  = 0.f;
    float       delegation_elapsed      = 0.f;

    // TriggerFired (display_kind == TriggerFired)
    std::string trigger_id;
    std::string trigger_context_summary;

    // AgentTurn (display_kind == AgentTurn)
    std::vector<std::string> tool_call_names; // list of tool names called this turn

    // UI state — not serialized
    bool expanded = false;     // whether the inspector popover is open
};

// State for the DP stream sub-panel.
enum class DPStreamState {
    Idle,       // no run_id; shows empty state
    Loading,    // RPC in flight
    Live,       // stream populated; polling continues
    Error,      // RPC error
};

struct DPStreamModel {
    DPStreamState state = DPStreamState::Idle;
    std::string   run_id;           // active mission run_id (empty = none)
    std::string   errorMsg;

    std::vector<DPStreamEntry> entries;  // newest first
    int  last_seq = -1;                  // cursor for incremental polling
    bool polling  = false;               // RPC in-flight guard

    // Populated/empty state helpers
    bool hasRun() const { return !run_id.empty(); }
    bool isEmpty() const { return entries.empty(); }
};

}} // namespace aicad::ui
