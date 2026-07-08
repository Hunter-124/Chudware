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

// RE-30: Source lane — determines which stages appear in the adaptive stepper.
//   Scan        : Source → Scale → Reconstruct → Model → Review → Deliver
//   Datasheet   : Source → Model → Review → Deliver  (skip Scale + Reconstruct)
//   Web         : Source → Model → Review → Deliver  (skip Scale + Reconstruct)
//   FromLibrary : seeded externally; auto-routed to Scan or Datasheet/Web
enum class SourceMode { Scan, Datasheet, Web, FromLibrary };

// The six pipeline stages in canonical order (contracts/reveng.md §2 session.stages).
// The adaptive stepper shows only the stages relevant to the active SourceMode.
enum class Stage { Source, Scale, Reconstruct, Model, Review, Deliver };
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
    Stage stage = Stage::Source;    // active center stage for the selected session

    // RE-30: active source lane (drives adaptive stepper + Sources panel).
    SourceMode sourceMode = SourceMode::Scan;

    // Async list bookkeeping.
    bool listPending = false;
    bool listedOnce = false;
    double lastListT = -100.0;
    std::string listErr;            // sticky; empty when healthy

    // RE-19: per-session image gallery (resets when selected session changes).
    std::vector<GalleryImage> galleryImages;
    int gallerySession = -1;        // selected index when gallery was last reset

    // RE-30: reveng_research_source (Web lane) async state.
    char researchQueryBuf[512] = {};
    std::string researchQuery;
    std::string researchResult;
    std::string researchErr;
    bool researchPending = false;

    // RE-30: reveng_ingest_datasheet (Datasheet lane) async state.
    std::string datasheetPath;
    std::string datasheetErr;
    bool datasheetPending = false;
};

RevEngState& S();

}  // namespace reveng
}  // namespace ui
