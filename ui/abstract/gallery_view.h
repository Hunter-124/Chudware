#pragma once
// Framework-agnostic view-model for the T-U9 sample gallery panel. Mirrors
// ui/abstract/scaffold_demo_view.h / parts_view.h (T-U1/T-U2 exemplars): pure structs, no
// ImGui/GL/GLFW includes. Consumed by ui/imgui/src/ui/gallery_panel.cpp.
//
// Source of truth: samples/index.json (T-Q3) — one entry per sample product. This header
// mirrors that JSON shape; gallery_panel.cpp does the parsing (nlohmann::json isn't
// framework-specific, but keeping the parse in the .cpp keeps this header dependency-free).

#include <string>
#include <vector>

namespace aicad::ui {

struct GallerySample {
    std::string id;                       // "mech_part"
    std::string title;                    // "Mounting Bracket"
    std::string description;
    std::string discipline;               // "mech" | "cam" | "structural"
    std::string docFile;                  // absolute path, resolved from samples/<doc_file>
    std::string thumbnailPath;            // absolute path, resolved from samples/<thumbnail>
    std::vector<std::string> whatItTeaches;
    std::vector<std::string> tags;
    bool hasTour = false;                 // samples/tours/<id>_tour.py exists
    std::string tourScript;               // absolute path when hasTour
};

// Short display label for a discipline badge (pattern library-style domain badges,
// mirrors aicad::ui::partsDomainLabel in ui/abstract/parts_view.h).
inline const char* galleryDisciplineLabel(const std::string& discipline) {
    if (discipline == "mech")       return "MECH";
    if (discipline == "cam")        return "CAM";
    if (discipline == "structural") return "STRUCT";
    if (discipline == "ecad")       return "ECAD";
    return "PART";
}

enum class GalleryState {
    Loading,   // samples/index.json not read yet
    Ready,     // samples loaded (may be empty)
    Error,     // index.json missing/malformed
};

enum class GalleryOpenState {
    Idle,
    WaitingForEngine,  // doc_open queued, waiting for the worker/engine to come up
    Opening,
    Opened,
    Error,
};

struct GalleryModel {
    GalleryState state = GalleryState::Loading;
    std::string errorMsg;
    std::vector<GallerySample> samples;
    int selectedIdx = -1;

    GalleryOpenState openState = GalleryOpenState::Idle;
    std::string openErrorMsg;

    std::string tourStatusMsg;   // last tour-launch result, empty = nothing run yet
};

}  // namespace aicad::ui
