// RE-30: Sources entry panel implementation.
// Three entry paths depending on the active source mode (set in reveng_internal.h):
//   Scan        — capture suite link + drag-drop for image folders
//   Datasheet   — file dropzone (PDF/image) → async reveng_ingest_datasheet
//   Web+Part#   — query/URL field → async reveng_research_source (RE-29)
//   FromLibrary — seeded externally; shows the routed mode read-only
//
// GL-thread only. NEVER sync worker calls in draw paths.
#include "sources_panel.h"

#include <cstring>
#include <string>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"
#include "reveng_internal.h"
#include "webcam_capture.h"   // RE-19: drawWebcamCapture
#include "image_gallery.h"    // RE-19: drawImageGallery / galleryLoadPendingThumbs
#include "../file_dialog.h"   // openFileDialog — visible "Add media" browse affordance

namespace ui {
namespace reveng {

// ---- async helpers -------------------------------------------------------------

// Kick an async reveng_research_source call; result lands in S().researchResult.
static void kickResearch(AppState& st) {
    RevEngState& s = S();
    if (s.researchPending) return;
    if (s.researchQuery.empty()) return;
    s.researchPending = true;
    s.researchErr.clear();
    s.researchResult.clear();

    // Build the session_id parameter if a session is active.
    nlohmann::json params = nlohmann::json::object();
    if (s.selected >= 0 && s.selected < (int)s.sessions.size())
        params["session_id"] = s.sessions[s.selected].session_id;
    params["query"] = s.researchQuery;

    st.rpc.call("reveng_research_source", params,
                [](const core::RpcResult& r) {
        RevEngState& st2 = S();
        st2.researchPending = false;
        if (!r.ok) {
            st2.researchErr = r.err.empty() ? "research call failed" : r.err;
            return;
        }
        if (r.result.is_object() && !r.result.value("ok", true)) {
            st2.researchErr = r.result.value("reason",
                                std::string("reveng_research_source failed"));
            return;
        }
        // Store a human-readable summary; MCP returns {ok, summary, sources:[...]}
        if (r.result.is_object() && r.result.contains("summary"))
            st2.researchResult = r.result["summary"].get<std::string>();
        else
            st2.researchResult = "(research complete — see session)";
    });
}

// Kick an async reveng_ingest_datasheet call for the dropped file path.
static void kickDatasheetIngest(AppState& st, const std::string& path) {
    RevEngState& s = S();
    if (s.datasheetPending) return;
    if (path.empty()) return;
    s.datasheetPending = true;
    s.datasheetErr.clear();
    s.datasheetPath = path;

    nlohmann::json params = nlohmann::json::object();
    if (s.selected >= 0 && s.selected < (int)s.sessions.size())
        params["session_id"] = s.sessions[s.selected].session_id;
    params["path"] = path;

    st.rpc.call("reveng_ingest_datasheet", params,
                [](const core::RpcResult& r) {
        RevEngState& st2 = S();
        st2.datasheetPending = false;
        if (!r.ok) {
            st2.datasheetErr = r.err.empty() ? "ingest call failed" : r.err;
            return;
        }
        if (r.result.is_object() && !r.result.value("ok", true)) {
            st2.datasheetErr = r.result.value("reason",
                                  std::string("reveng_ingest_datasheet failed"));
            return;
        }
        // Successful ingest; caller can poll session status for tables/crops.
        st2.datasheetErr.clear();
    });
}

// ---- sub-panels per mode -------------------------------------------------------

static void drawScanEntry(AppState& st) {
    RevEngState& s = S();
    ImGui::Spacing();
    ImGui::TextUnformatted("Scan / Webcam");
    ImGui::Separator();
    ImGui::Spacing();

    if (s.selected >= 0 && s.selected < (int)s.sessions.size()) {
        const std::string& sid = s.sessions[s.selected].session_id;

        // Visible "add media" affordance — not everyone discovers drag-drop. Opens a native
        // file picker and pushes the chosen image into the same reveng_add_images flow.
        if (ImGui::Button("Add photo\xe2\x80\xa6") && !sid.empty()) {
            std::string path = ui::openFileDialog(
                "Images (*.png *.jpg *.jpeg *.bmp)\0*.png;*.jpg;*.jpeg;*.bmp\0"
                "All files (*.*)\0*.*\0");
            if (!path.empty()) {
                nlohmann::json args;
                args["session_id"] = sid;
                args["paths"] = nlohmann::json::array({ path });
                st.rpc.call("reveng_add_images", args, [](const core::RpcResult&){});
            }
        }
        ImGui::SameLine();
        ImGui::TextDisabled("or drag images onto the gallery below");
        ImGui::Spacing();

        // RE-19: live webcam capture widget (stub camera works headless).
        drawWebcamCapture(st, sid, /*session_dir=*/".");
        ImGui::Spacing();
        // RE-19: image gallery / dropzone — also handles drag-drop.
        std::vector<std::string> dropped;
        galleryLoadPendingThumbs(s.galleryImages);
        drawImageGallery(st, sid, s.galleryImages, dropped);
        if (!dropped.empty() && !sid.empty()) {
            nlohmann::json args;
            args["session_id"] = sid;
            args["paths"] = nlohmann::json(dropped);
            st.rpc.call("reveng_add_images", args, [](const core::RpcResult&){});
        }
    } else {
        ImGui::TextWrapped(
            "Add photos or video via the ribbon Capture button, "
            "or drag an image folder here. The pipeline will guide you through Scale "
            "and Reconstruct before reaching Model.");
    }
}

static void drawDatasheetEntry(AppState& st) {
    const float sc = ui::uiScale;
    RevEngState& s = S();

    ImGui::Spacing();
    ImGui::TextUnformatted("Datasheet / Spec sheet");
    ImGui::Separator();
    ImGui::Spacing();
    ImGui::TextWrapped(
        "Drop a PDF or image spec sheet below. The datasheet reader (RE-15) extracts "
        "dimension tables and routes them directly to Model — Scale and Reconstruct are "
        "skipped for this lane.");
    ImGui::Spacing();

    // Drop-zone.
    ImVec2 sz(ImGui::GetContentRegionAvail().x, 72.0f * sc);
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(1, 1, 1, 0.04f));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 6.0f * sc);
    ImGui::BeginChild("##ds_drop", sz, ImGuiChildFlags_Border);
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();
    {
        ImVec2 avail2 = ImGui::GetContentRegionAvail();
        ImGui::Dummy(ImVec2(0, avail2.y * 0.25f));
        const char* hint = s.datasheetPath.empty()
                               ? "Drop PDF / image here"
                               : s.datasheetPath.c_str();
        const ImVec2 ts = ImGui::CalcTextSize(hint);
        float cx = (avail2.x - ts.x) * 0.5f;
        if (cx < 4.0f * sc) cx = 4.0f * sc;
        ImGui::SetCursorPosX(cx);
        if (s.datasheetPath.empty())
            ImGui::TextDisabled("%s", hint);
        else
            ImGui::TextUnformatted(hint);
    }
    ImGui::EndChild();

    // Visible browse affordance alongside drag-drop.
    if (ImGui::Button("Browse\xe2\x80\xa6##ds")) {
        std::string path = ui::openFileDialog(
            "Spec sheet (*.pdf *.png *.jpg *.jpeg)\0*.pdf;*.png;*.jpg;*.jpeg\0"
            "All files (*.*)\0*.*\0");
        if (!path.empty()) kickDatasheetIngest(st, path);
    }

    // Payload accept (ImGui internal DnD).
    if (ImGui::BeginDragDropTarget()) {
        if (const ImGuiPayload* pl =
                ImGui::AcceptDragDropPayload("FILES", ImGuiDragDropFlags_None)) {
            // Payload is a NUL-separated list of paths; take the first one.
            const char* data = static_cast<const char*>(pl->Data);
            if (data && pl->DataSize > 0) {
                std::string path(data, std::find(data, data + pl->DataSize, '\0'));
                kickDatasheetIngest(st, path);
            }
        }
        ImGui::EndDragDropTarget();
    }

    ImGui::Spacing();
    if (s.datasheetPending) {
        ImGui::TextDisabled("Ingesting…");
    } else if (!s.datasheetErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.9f, 0.35f, 0.3f, 1.0f));
        ImGui::TextWrapped("Error: %s", s.datasheetErr.c_str());
        ImGui::PopStyleColor();
    } else if (!s.datasheetPath.empty()) {
        ImGui::TextDisabled("Ingest complete.");
    }
}

static void drawWebEntry(AppState& st) {
    const float sc = ui::uiScale;
    RevEngState& s = S();

    ImGui::Spacing();
    ImGui::TextUnformatted("Web / Part Number");
    ImGui::Separator();
    ImGui::Spacing();
    ImGui::TextWrapped(
        "Enter a part number, vendor URL, or search query. "
        "reveng_research_source (RE-29) will pull specs and datasheets, "
        "then route to Model — Scale and Reconstruct are skipped.");
    ImGui::Spacing();

    // Query input.
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - 72.0f * sc);
    bool enter = ImGui::InputText("##reveng_query",
                                  s.researchQueryBuf, sizeof(s.researchQueryBuf),
                                  ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::SameLine();
    const bool canSearch = !s.researchPending
                           && s.researchQueryBuf[0] != '\0';
    if (!canSearch) ImGui::BeginDisabled();
    bool clicked = ImGui::Button("Search", ImVec2(64.0f * sc, 0));
    if (!canSearch) ImGui::EndDisabled();

    if ((enter || clicked) && canSearch) {
        s.researchQuery = s.researchQueryBuf;
        kickResearch(st);
    }

    ImGui::Spacing();
    if (s.researchPending) {
        ImGui::TextDisabled("Searching…");
    } else if (!s.researchErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.9f, 0.35f, 0.3f, 1.0f));
        ImGui::TextWrapped("Error: %s", s.researchErr.c_str());
        ImGui::PopStyleColor();
    } else if (!s.researchResult.empty()) {
        ImGui::Spacing();
        ImGui::TextUnformatted("Result:");
        ImGui::TextWrapped("%s", s.researchResult.c_str());
    }
    (void)sc; // suppress warning if sc unused after last use
}

static void drawFromLibraryEntry(AppState& /*st*/) {
    ImGui::Spacing();
    ImGui::TextUnformatted("From Parts Library");
    ImGui::Separator();
    ImGui::Spacing();
    ImGui::TextWrapped(
        "This session was seeded from the Parts Library (RE-32/RE-33). "
        "The pipeline is routed automatically: Scan if the part record "
        "includes photos, or Datasheet/Web if only docs/specs are available.");
    ImGui::Spacing();
    ImGui::TextDisabled("Waiting for external seed…");
}

// ---- public entry point --------------------------------------------------------

void drawSourcesPanel(AppState& st) {
    RevEngState& s = S();
    switch (s.sourceMode) {
        case SourceMode::Scan:        drawScanEntry(st); break;
        case SourceMode::Datasheet:   drawDatasheetEntry(st); break;
        case SourceMode::Web:         drawWebEntry(st); break;
        case SourceMode::FromLibrary: drawFromLibraryEntry(st); break;
    }
}

}  // namespace reveng
}  // namespace ui
