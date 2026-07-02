#pragma once
// Framework-agnostic view-model for the universal preview pane + Copilot artifact cards
// (Pillar 15 / T-F16). Backends (ui/imgui/, future ui/qt/) render from these structs; they
// never talk to the deliverables service directly — previews arrive over the worker RPC seam
// (render_document / list_artifacts, contract deliverables/1 §5.4/§5.5) and are projected into
// these types. No ImGui / GL / GLFW includes here (ui/abstract rule).
//
// Contract shape this mirrors (do not diverge without a deliverables/2 bump):
//   - ArtifactCard  ← the §3 artifact record ({artifact_id, kind, path, created_ts, run_id}).
//   - PreviewModel  ← the §5.4 render_document envelope ({kind, pages[], resource_link}) or a
//                     §6 failure reason projected to a typed placeholder state (never a blank).

#include <string>
#include <vector>

namespace aicad::ui {

// One downscaled preview page produced by render_document (≤1024 px long edge, <1 MB).
struct PreviewPage {
    int index = 0;          // 1-based page index (contract pages[].index)
    std::string imagePath;  // absolute on-disk path to the PNG the backend loads as a texture
    int w = 0;              // pixel width  (contract pages[].image.w)
    int h = 0;              // pixel height (contract pages[].image.h)
};

// Lifecycle of a preview fetch. The non-Ready terminal states are the *typed placeholders* the
// pane shows instead of a blank frame (DoD: "unsupported formats show a typed placeholder").
enum class PreviewState {
    Empty,             // nothing selected
    Loading,           // render_document in flight (async) — pane shows a spinner, never stalls
    Ready,             // pages populated
    Unsupported,       // §6 unsupported_format — extension we cannot preview
    ConverterMissing,  // §6 converter_missing — office file present, LibreOffice absent
    NotFound,          // §6 artifact_not_found
    Error,             // any other failure (still shaped, never a traceback)
};

// The render_document result for one file, as the preview pane consumes it.
struct PreviewModel {
    std::string path;          // source path as requested (project-relative or absolute)
    std::string kind;          // extension/kind (docx, pdf, pptx, xlsx, csv, png, md, …)
    PreviewState state = PreviewState::Empty;
    std::vector<PreviewPage> pages;
    int activePage = 0;        // index into pages[] the pane is showing
    std::string resourceLink;  // file:// URI to the full original file (open-externally target)
    std::string message;       // human hint for placeholder states (e.g. "install libreoffice")
};

// One agent-produced deliverable, projected from the §3 artifact record for card display.
struct ArtifactCard {
    std::string artifactId;  // §3 artifact_id (stable, opaque)
    std::string kind;        // presentation | report | workbook | manual | <raw ext>
    std::string path;        // project-relative file path (§3 path)
    std::string title;       // display title (file stem when the record carries none)
    std::string createdTs;   // ISO-8601 UTC (§3 created_ts)
    std::string runId;       // grouping run id (§3 run_id)
};

// Project a contract §6 failure `reason` to a typed placeholder state. Unknown reasons (and the
// empty string) collapse to Error — the pane still shows a labelled placeholder, never a blank.
inline PreviewState previewStateFromReason(const std::string& reason) {
    if (reason == "unsupported_format") return PreviewState::Unsupported;
    if (reason == "converter_missing")  return PreviewState::ConverterMissing;
    if (reason == "artifact_not_found") return PreviewState::NotFound;
    return PreviewState::Error;
}

// Short human label for a placeholder state (backend renders this centered in the empty pane).
inline const char* previewStateLabel(PreviewState s) {
    switch (s) {
        case PreviewState::Empty:            return "No preview selected";
        case PreviewState::Loading:          return "Rendering preview…";
        case PreviewState::Ready:            return "";
        case PreviewState::Unsupported:      return "No preview for this format";
        case PreviewState::ConverterMissing: return "Preview needs LibreOffice";
        case PreviewState::NotFound:         return "File not found";
        case PreviewState::Error:            return "Preview failed";
    }
    return "Preview failed";
}

}  // namespace aicad::ui
