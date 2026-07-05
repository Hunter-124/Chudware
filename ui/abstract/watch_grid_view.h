#pragma once
// Framework-agnostic view-model for the T-W17 multi-viewport Watch Grid panel
// (Pillar 20 §2.14/§2.15). Pure structs, no ImGui/GL includes. Fed from the
// per-doc workspace event streams via workspace_status (T-W16 shape); renders
// from a committed fixture's workspace_status.json + render PNGs with no backend.

#include <string>
#include <vector>

namespace aicad::ui {

enum class WatchState {
    Idle,     // no workspace loaded
    Ready,    // docs populated
    Error,    // load failed
    Empty,    // workspace has no docs
};

// One watched document tile: latest render + status/owner/model chips.
struct WatchTile {
    std::string doc_id;
    std::string title;
    std::string owner;          // agent/role holding the lease
    std::string model;          // model chip (e.g. qwen2.5-coder-7b)
    std::string node_status;    // pending|building|done|failed|skipped (buildgraph)
    std::string lease;          // write|read
    std::string render_abs_path; // absolute path to latest render PNG ("" if none)
};

struct WatchGridModel {
    WatchState  state = WatchState::Idle;
    std::string run_id;
    std::string errorMsg;
    std::vector<WatchTile> tiles;
    int selectedTile = -1;   // click-through target
};

}  // namespace aicad::ui
