#pragma once
// Framework-agnostic view-model for the T-W17 Agent Inspector panel (Pillar 20
// §2.15). Pure structs, no ImGui/GL includes — the ui/imgui backend renders from
// these. Populated entirely from a REPLAYED run directory (ledger events.jsonl +
// advisory/preflight/audit/delegation/failure records); no live backend needed.
//
// Schemas mirrored: aicad.ledger event log, aicad.advisory/1 (§3),
// aicad.preflight/1 (§10), aicad.audit/1 (§11), aicad.delegation/1 (§12),
// aicad.failure/1 (§5).

#include <string>
#include <vector>

namespace aicad::ui {

enum class InspectorState {
    Idle,     // no run loaded
    Loading,  // reading a run dir
    Ready,    // events parsed
    Error,    // load failed / malformed
    Empty,    // run dir loaded but has no turns (empty-state)
};

// One tool call inside a turn, with its preflight verdict (if any).
struct InspectorToolCall {
    std::string tool;
    std::string args_digest;
    std::string preflight_id;   // e.g. "pf_0002" ("" if none)
    std::string preflight_verdict;  // pass|warn|fail ("" if unresolved)
    std::string preflight_detail;   // first non-pass check detail, for the row
};

// The verbatim context injected at the start of a turn (troubleshooting core).
struct InspectorInjected {
    std::string ledger_render;  // verbatim ledger_render text
    std::string advisory;       // verbatim [ADVISORY]..[/ADVISORY] block ("" if none)
    std::string playbook_step;  // verbatim playbook step ("" if none)
};

// One agent turn: who, what stage, what was injected, tool calls, outcome.
struct InspectorTurn {
    int         turn = 0;
    std::string actor;   // role id / "foreman" / "human"
    std::string stage;
    InspectorInjected injected;
    std::vector<InspectorToolCall> tool_calls;
    std::string status;      // ok|blocked|... from turn.end
    std::string audit_ref;   // audits/aud_*.json if this turn produced an audit
    std::string failure_ref; // failures/fc_*.json if a failure surfaced this turn
};

// A delegation-tree node (§12) for the tree view.
struct InspectorDelegationNode {
    std::string id;
    std::string parent;   // "" for root
    std::string role;
    std::string brief;
    std::string model;    // provider/id chip
    std::string approval; // policy + approver
    std::string status;   // pending|approved|running|done|failed|revoked
    int         tokens_granted = 0;
    int         tokens_spent = 0;
    int         attempts = 0;
    int         depth = 0;  // computed from parent chain, for indentation
};

// A failure card (§5) with its attempt history.
struct InspectorFailureAttempt {
    int         n = 0;
    std::string fix_applied;   // compact "param=value"
    std::string result;        // same|improved|worse|new_failure
    std::string delta;         // compact status
};
struct InspectorFailureCard {
    std::string id;
    std::string klass;   // rebuild|solver|...
    std::string stage;
    std::string what;
    std::string why;
    std::vector<InspectorFailureAttempt> attempts;
    int         max_attempts = 3;
};

// An audit record (§11) rendered as review findings (source=llm → suggestions).
struct InspectorAuditVerdict {
    std::string klass;    // plan_gap|bad_fix|ok|...
    std::string finding;
    std::string action;   // required_action.kind + detail
    std::string severity; // info|action|blocker
};
struct InspectorAudit {
    std::string id;
    std::string trigger;
    std::string stage;
    std::string model;    // provider/id
    std::vector<InspectorAuditVerdict> verdicts;
};

struct AgentInspectorModel {
    InspectorState state = InspectorState::Idle;
    std::string    run_id;
    std::string    run_dir;   // absolute path loaded
    std::string    brief_title;
    std::string    brief_summary;
    std::string    errorMsg;

    std::vector<InspectorTurn>            turns;
    std::vector<InspectorDelegationNode>  delegation;
    std::vector<InspectorFailureCard>     failures;
    std::vector<InspectorAudit>           audits;

    // Filters (applied at render time).
    std::string filterActor;    // "" = all
    std::string filterStage;    // "" = all
    std::string filterSeverity; // "" = all (matches advisory/audit/preflight worst)

    int selectedTurn = -1;  // index into turns for the detail pane
};

}  // namespace aicad::ui
