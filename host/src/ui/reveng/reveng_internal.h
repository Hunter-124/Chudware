// Reverse Engineering workspace — internal store + shared types (contracts/reveng.md).
// GL-thread only: the store is a plain file-static singleton (no mutex), mutated only from
// draw-path rpc callbacks that the worker drains on the GL thread. Never do a SYNC worker
// call in a draw path (known freeze bug); all session data arrives via ASYNC st.rpc.call.
#pragma once

#include <string>
#include <vector>

// RE-19: GalleryImage is defined in image_gallery.h; included below to avoid
// a circular dependency (image_gallery.h must NOT include reveng_internal.h).
#include "image_gallery.h"

namespace ui {
namespace reveng {

// The six pipeline stages, in order (contracts/reveng.md §2 session.stages + ribbon groups
// Session/Sources/Scale/Reconstruct/Model/Deliver). Center empty-states + the bottom
// stepper are driven by these.
enum class Stage { Capture, Scale, Reconstruct, Model, Review, Deliver };
constexpr int kStageCount = 6;

// Per-stage badge state mirrored from session.stages values ("idle"|"pending"|"done"|...).
struct StageState {
    std::string badge = "idle";  // raw contract badge string
};

// One row in the session rail — the summary subset of aicad.reveng_session/1 the list tool
// returns. Kept intentionally thin; the detail view fetches the full manifest on demand.
struct SessionSummary {
    std::string session_id;
    std::string name;
    std::string status;   // capturing|reconstructing|modeling|review|done|error
    bool approximate = false;
    int images = 0;
};

// File-static store for the whole workspace. Async-load bookkeeping mirrors the parts /
// ai-workspace idiom (throttle by ImGui::GetTime, pending flag, listedOnce, sticky error).
struct RevEngState {
    std::vector<SessionSummary> sessions;
    int selected = -1;              // index into sessions, or -1
    Stage stage = Stage::Capture;   // active center stage for the selected session

    // Async list bookkeeping.
    bool listPending = false;
    bool listedOnce = false;
    double lastListT = -100.0;
    std::string listErr;            // sticky; empty when healthy

    // RE-19: per-session image gallery (resets when selected session changes).
    std::vector<GalleryImage> galleryImages;
    int gallerySession = -1;        // selected index when gallery was last reset
};

RevEngState& S();

}  // namespace reveng
}  // namespace ui
