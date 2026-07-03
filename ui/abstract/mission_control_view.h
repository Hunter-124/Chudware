#pragma once
// Mission-control view-model (T-F17): pure structs, no ImGui/GL/GLFW.
// The panel (ui/imgui/src/ui/mission_control_panel.h) projects dag.* events
// from events.jsonl into these structs and renders from them.  Style exemplar:
// ui/abstract/artifact_view.h (same no-ImGui rule).
//
// Contract binding (contracts/agent_events.md §3):
//   DagNodeRow  ← dag.node.* events
//   EventEntry  ← every event in the stream (agent.thought / tool_call / etc.)
//   DagRunView  ← dag.run.started + aggregate state

#include <string>
#include <vector>

namespace aicad::ui {

// -------------------------------------------------------------------------
// DAG node row (one row in the run tree)
// -------------------------------------------------------------------------

enum class DagNodeStatus {
    Pending,    // not yet reached
    Running,    // dag.node.started received, no done/failed/paused yet
    Done,       // dag.node.done
    Failed,     // dag.node.failed
    Paused,     // dag.node.paused (gate / manual / fail)
    Skipped,    // dag.run.done node_states shows "skipped"
};

enum class DagNodePauseReason {
    None,
    Gate,       // autonomy=approve gate fired
    Manual,     // autonomy=manual node
    Fail,       // on_fail=pause
};

struct DagNodeRow {
    std::string nodeId;
    std::string nodeType;   // "design" | "verify" | "gate" | …
    std::string nodeName;   // human label
    DagNodeStatus status = DagNodeStatus::Pending;
    DagNodePauseReason pauseReason = DagNodePauseReason::None;
    std::string outputSummary;  // ≤120 chars from dag.node.done
    std::string errorText;      // from dag.node.failed
    bool expanded = false;      // whether the event sub-list is open in the UI
};

// -------------------------------------------------------------------------
// Event stream entry (one row in the live log / thought trail)
// -------------------------------------------------------------------------

enum class EventEntryKind {
    RunStarted,
    NodeStarted,
    NodeDone,
    NodeFailed,
    NodePaused,
    NodeApprovalRequired,
    NodeManualRequired,
    RunDone,
    AgentThought,
    AgentToolCall,
    AgentArtifact,
    AgentQuestion,
    AgentAnswer,
    MissionBriefProposed,
    MissionBriefSigned,
    Unknown,
};

struct EventEntry {
    EventEntryKind kind = EventEntryKind::Unknown;
    std::string kindStr;        // raw kind string from the JSONL
    std::string nodeId;         // empty for run-level events
    std::string ts;             // ISO-8601 timestamp
    std::string headline;       // one-line display text (panel formats this)
    std::string detail;         // optional secondary text (tool args digest, etc.)
    bool isThought = false;     // true for agent.thought (collapsible)
};

// -------------------------------------------------------------------------
// Steering state (what the user can do right now)
// -------------------------------------------------------------------------

enum class SteeringMode {
    None,       // no active run
    Running,    // run in progress — pause available
    Paused,     // gate/manual — approve/reject/inject available
    Done,       // terminal — read-only
};

struct SteeringState {
    SteeringMode mode = SteeringMode::None;
    std::string  runId;
    std::string  pendingNodeId;   // set when mode == Paused (the node waiting)
    bool         canApprove  = false;
    bool         canReject   = false;
    bool         canInject   = false;
    bool         canPause    = false;
    std::string  injectBuffer;    // instruction text being composed
    // Autonomy dial: mirrors aicad.dag/1 §6 AutonomyLevel
    // 0=auto, 1=approve, 2=manual
    int          autonomyLevel = 0;
};

// -------------------------------------------------------------------------
// Full run view — the top-level model the panel renders from
// -------------------------------------------------------------------------

struct DagRunView {
    std::string runId;
    std::string graphName;
    std::string status;         // "pending" | "running" | "paused" | "done" | "failed"
    std::vector<DagNodeRow> nodes;
    std::vector<EventEntry> events;    // ordered chronologically
    SteeringState steering;

    bool hasRun = false;        // false = empty state (no run loaded/started)
};

// -------------------------------------------------------------------------
// Replay entry (one item in the "past runs" list)
// -------------------------------------------------------------------------

struct ReplayEntry {
    std::string runId;
    std::string label;          // short display string
    std::string jsonlPath;      // absolute path to events.jsonl
    long long   mtimeMs = 0;    // file mtime for staleness detection
};

}  // namespace aicad::ui
