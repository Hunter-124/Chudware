#pragma once
// Framework-agnostic view-model for the T-DP17 Agent DAG widget.
// Pure structs — no ImGui / GL includes.  Coordinates stored as plain float pairs
// (not ImVec2) so ui_style_check / the no-ImGui-in-abstract rule holds.
//
// Populated from:
//   • aicad.mission event streams   (DagNodeStatus + DagNodeRow in mission_control_view.h)
//   • aicad.buildgraph/1            (build-order DAG; opaque here — reuse DagNodeRow shape)
//   • aicad.delegation/1            (delegation tree from agent_inspector_view.h)
//
// The panel (ui/imgui/src/ui/agent_dag_panel.h) owns the seed-from-JSONL logic
// and renders from these structs.

#include <string>
#include <vector>

namespace aicad::ui {

// -----------------------------------------------------------------------
// Per-node status (mirrors DagNodeStatus in mission_control_view.h)
// -----------------------------------------------------------------------

enum class DagWidgetNodeStatus {
    Queued,    // not yet started
    Running,   // currently executing
    Blocked,   // waiting on dependency / gate
    Done,      // completed successfully
    Failed,    // terminal failure
    Skipped,   // skipped by run logic
};

// -----------------------------------------------------------------------
// Node kind — drives shape + colour differentiation
// -----------------------------------------------------------------------

enum class DagWidgetNodeKind {
    Mission,    // top-level run / phase
    BuildOp,    // individual CAD operation (buildgraph)
    Delegation, // sub-agent spawned by delegation tree
    Gate,       // approval gate node
    Verify,     // verification step
};

// -----------------------------------------------------------------------
// One node in the widget graph
// -----------------------------------------------------------------------

struct DagWidgetNode {
    std::string id;
    std::string label;          // display name (≤ 32 chars for compact)
    std::string role;           // e.g. "feature_architect", "foreman", ""
    std::string model;          // provider/model chip, "" if unknown
    std::string currentTool;    // most recent tool call ("" when idle)
    std::string ledgerSlice;    // one-liner ledger context
    std::string statusLabel;    // human-readable status suffix

    DagWidgetNodeStatus status = DagWidgetNodeStatus::Queued;
    DagWidgetNodeKind   kind   = DagWidgetNodeKind::Mission;

    int  tokensGranted = 0;
    int  tokensSpent   = 0;
    int  depth         = 0;   // layer for layout (longest-path depth from roots)
    int  slotInLayer   = 0;   // position within layer (filled by layout pass)

    // Layout output (filled by layoutDag() in the panel; kept here so the panel
    // can query it for hit-testing without a second map lookup).
    float cx = 0.f;            // centre x in canvas space
    float cy = 0.f;            // centre y in canvas space

    bool selected = false;
};

// -----------------------------------------------------------------------
// Directed edge
// -----------------------------------------------------------------------

struct DagWidgetEdge {
    std::string fromId;
    std::string toId;
};

// -----------------------------------------------------------------------
// Full widget model
// -----------------------------------------------------------------------

enum class DagWidgetMode {
    Compact,   // graph only, small — for overlay copilot column
    Full,      // graph + detail pane — for AI-Agent workspace
};

struct AgentDagModel {
    std::vector<DagWidgetNode> nodes;
    std::vector<DagWidgetEdge> edges;

    std::string selectedNodeId;   // "" = nothing selected
    std::string runId;
    std::string missionName;

    bool hasData = false;   // false = empty-state placeholder
};

}  // namespace aicad::ui
