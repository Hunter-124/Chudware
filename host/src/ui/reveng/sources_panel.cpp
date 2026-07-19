// RE-30: Sources entry panel implementation.
// Three entry paths depending on the active source mode (set in reveng_internal.h):
//   Scan        — capture suite link + drag-drop for image folders + phone capture
//   Datasheet   — file dropzone (PDF/image) → async reveng_ingest_datasheet
//   Web+Part#   — query/URL field → async reveng_research_source (RE-29)
//   FromLibrary — seeded externally; shows the routed mode read-only
//
// GL-thread only. NEVER sync worker calls in draw paths.
#include "sources_panel.h"

#include <cstring>
#include <string>
#include <vector>
#include <algorithm>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"
#include "reveng_internal.h"
#include "webcam_capture.h"   // RE-19: drawWebcamCapture
#include "image_gallery.h"    // RE-19: drawImageGallery / galleryLoadPendingThumbs
#include "img_tex.h"          // QR data-URL → GL texture (reuse existing path; no new dep)
#include "../file_dialog.h"   // openFileDialog — visible "Add media" browse affordance
#include "ui/parts/parts_widgets.h"  // b64decode for QR data URLs

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

// Decode a data:image/png;base64,… QR payload into phoneQrTex (or clear it).
static void updatePhoneQrTexture(RevEngState& s) {
    if (s.phoneQrTex) {
        ::reveng::freeTexture(s.phoneQrTex);
        s.phoneQrTex = 0;
        s.phoneQrW = s.phoneQrH = 0;
    }
    if (s.phoneQr.empty()) return;

    // Accept either a data URL or a bare URL (no texture for bare URLs).
    const std::string prefix = "data:image/png;base64,";
    if (s.phoneQr.rfind(prefix, 0) != 0) {
        // Not a PNG data URL — leave tex empty; UI shows the URL string.
        return;
    }
    const std::string b64 = s.phoneQr.substr(prefix.size());
    std::vector<unsigned char> bytes = ui::parts::b64decode(b64);
    if (bytes.empty()) return;
    int w = 0, h = 0;
    unsigned int tex = ::reveng::loadTexFromMemory(bytes.data(), (int)bytes.size(), &w, &h);
    if (tex) {
        s.phoneQrTex = tex;
        s.phoneQrW = w;
        s.phoneQrH = h;
    }
}

static void kickStartPhoneCapture(AppState& st) {
    RevEngState& s = S();
    if (s.phonePending || s.phoneActive) return;
    s.phonePending = true;
    s.phoneErr.clear();
    s.phoneStatus = "Starting capture server…";
    s.phoneUrl.clear();
    s.phoneQr.clear();
    s.phoneCount = 0;
    if (s.phoneQrTex) {
        ::reveng::freeTexture(s.phoneQrTex);
        s.phoneQrTex = 0;
    }

    nlohmann::json params = nlohmann::json::object();
    params["port"] = s.phonePort > 0 ? s.phonePort : 8742;

    st.rpc.call("start_capture_session", params,
                [](const core::RpcResult& r) {
        RevEngState& st2 = S();
        st2.phonePending = false;
        if (!r.ok) {
            st2.phoneActive = false;
            st2.phoneErr = r.err.empty()
                ? "start_capture_session failed (worker method missing?)"
                : r.err;
            st2.phoneStatus.clear();
            return;
        }
        if (!r.result.is_object()) {
            st2.phoneActive = false;
            st2.phoneErr = "start_capture_session returned non-object";
            st2.phoneStatus.clear();
            return;
        }
        // Already-running session still surfaces the URL so the user can continue.
        const bool ok = r.result.value("ok", false);
        if (!ok) {
            // Prefer showing the existing session rather than a hard failure.
            if (r.result.contains("upload_url")) {
                st2.phoneActive = true;
                st2.phoneUrl = r.result.value("upload_url", std::string());
                st2.phonePort = r.result.value("port", st2.phonePort);
                st2.phoneCount = r.result.value("photo_count", 0);
                st2.phoneStatus = r.result.value("reason",
                    std::string("Capture session already running"));
                st2.phoneErr.clear();
                if (r.result.contains("qr") && r.result["qr"].is_string()) {
                    st2.phoneQr = r.result["qr"].get<std::string>();
                    updatePhoneQrTexture(st2);
                }
                return;
            }
            st2.phoneActive = false;
            st2.phoneErr = r.result.value("reason",
                std::string("start_capture_session failed"));
            st2.phoneStatus.clear();
            return;
        }
        st2.phoneActive = true;
        st2.phonePort = r.result.value("port", 8742);
        st2.phoneUrl  = r.result.value("upload_url", std::string());
        st2.phoneCount = r.result.value("photo_count", 0);
        st2.phoneStatus = "Phone capture live — open the URL or scan the QR.";
        st2.phoneErr.clear();
        if (r.result.contains("qr") && r.result["qr"].is_string()) {
            st2.phoneQr = r.result["qr"].get<std::string>();
            updatePhoneQrTexture(st2);
        }
    });
}

// Clean re-implementation of stop that chains reveng_add_images.
static void kickStopPhoneCaptureAndAdd(AppState& st) {
    RevEngState& s = S();
    if (s.phonePending) return;
    s.phonePending = true;
    s.phoneStatus = "Stopping capture…";

    nlohmann::json params = nlohmann::json::object();
    params["port"] = s.phonePort > 0 ? s.phonePort : 8742;
    params["run_reconstruction"] = false;

    const std::string sid =
        (s.selected >= 0 && s.selected < (int)s.sessions.size())
            ? s.sessions[s.selected].session_id
            : std::string();

    // Capture raw pointer — AppState is process-lifetime for the host.
    AppState* stp = &st;
    st.rpc.call("stop_capture_session", params,
                [sid, stp](const core::RpcResult& r) {
        RevEngState& st2 = S();
        st2.phonePending = false;
        st2.phoneActive = false;

        if (!r.ok) {
            st2.phoneErr = r.err.empty() ? "stop_capture_session failed" : r.err;
            st2.phoneStatus.clear();
            return;
        }
        if (r.result.is_object() && !r.result.value("ok", true)) {
            st2.phoneErr = r.result.value("reason",
                std::string("stop_capture_session failed"));
            st2.phoneStatus.clear();
            return;
        }

        st2.phoneErr.clear();
        int count = 0;
        std::vector<std::string> photos;
        if (r.result.is_object()) {
            count = r.result.value("photo_count", 0);
            if (r.result.contains("photos") && r.result["photos"].is_array()) {
                for (const auto& p : r.result["photos"]) {
                    if (p.is_string()) photos.push_back(p.get<std::string>());
                }
            }
        }
        st2.phoneCount = count;
        st2.phoneUrl.clear();
        st2.phoneQr.clear();
        if (st2.phoneQrTex) {
            ::reveng::freeTexture(st2.phoneQrTex);
            st2.phoneQrTex = 0;
        }

        if (photos.empty()) {
            st2.phoneStatus = "Capture stopped (no photos received).";
            return;
        }

        for (const auto& p : photos) {
            GalleryImage g;
            g.path = p;
            st2.galleryImages.push_back(std::move(g));
        }

        if (sid.empty() || !stp) {
            st2.phoneStatus = "Captured " + std::to_string(count) +
                              " photo(s) — select a session to attach them.";
            return;
        }

        st2.phoneStatus = "Captured " + std::to_string(count) +
                          " photo(s) — adding to session…";
        nlohmann::json args;
        args["session_id"] = sid;
        args["paths"] = nlohmann::json(photos);
        stp->rpc.call("reveng_add_images", args, [](const core::RpcResult& r2) {
            RevEngState& st3 = S();
            if (!r2.ok) {
                st3.phoneErr = r2.err.empty() ? "reveng_add_images failed" : r2.err;
                st3.phoneStatus = "Photos saved locally; session add failed.";
                return;
            }
            if (r2.result.is_object() && !r2.result.value("ok", true)) {
                st3.phoneErr = r2.result.value("reason",
                    std::string("reveng_add_images failed"));
                st3.phoneStatus = "Photos saved locally; session add failed.";
                return;
            }
            st3.phoneStatus = "Phone photos added to session.";
            st3.phoneErr.clear();
        });
    });
}

// ---- sub-panels per mode -------------------------------------------------------

static void drawPhoneCaptureBlock(AppState& st) {
    const float sc = ui::uiScale;
    RevEngState& s = S();

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    ImGui::TextUnformatted("Capture from phone");
    ImGui::TextDisabled(
        "Starts a local upload server. Open the URL on your phone (same LAN) "
        "or scan the QR when available.");
    ImGui::Spacing();

    if (!s.phoneActive && !s.phonePending) {
        if (ImGui::Button("Capture from phone")) {
            kickStartPhoneCapture(st);
        }
    } else if (s.phonePending) {
        ImGui::TextDisabled("%s",
            s.phoneStatus.empty() ? "Working…" : s.phoneStatus.c_str());
    } else {
        // Active session: show URL + optional QR texture + stop.
        if (!s.phoneStatus.empty()) {
            ImGui::TextWrapped("%s", s.phoneStatus.c_str());
        }
        if (!s.phoneUrl.empty()) {
            ImGui::Spacing();
            ImGui::TextUnformatted("Open on phone:");
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
            ImGui::TextWrapped("%s", s.phoneUrl.c_str());
            ImGui::PopStyleColor();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Copy this URL into your phone browser on the same Wi-Fi.");
        }

        if (s.phoneQrTex) {
            ImGui::Spacing();
            const float qrSide = 160.0f * sc;
            ImGui::Image((ImTextureID)(uintptr_t)s.phoneQrTex, ImVec2(qrSide, qrSide));
            ImGui::TextDisabled("Scan with phone camera");
        } else if (!s.phoneQr.empty() && s.phoneQr.rfind("data:", 0) != 0) {
            // qr field fell back to the bare URL (qrcode package absent on worker).
            ImGui::TextDisabled("(QR unavailable — use the URL above)");
        }

        ImGui::Spacing();
        ImGui::TextDisabled("Photos received (last report): %d", s.phoneCount);
        ImGui::Spacing();
        if (ImGui::Button("Done — pull photos")) {
            kickStopPhoneCaptureAndAdd(st);
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel")) {
            // Stop without requiring add (still runs stop_capture_session).
            kickStopPhoneCaptureAndAdd(st);
        }
    }

    if (!s.phoneErr.empty()) {
        ImGui::Spacing();
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.9f, 0.35f, 0.3f, 1.0f));
        ImGui::TextWrapped("Error: %s", s.phoneErr.c_str());
        ImGui::PopStyleColor();
    }
}

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
                GalleryImage g;
                g.path = path;
                s.galleryImages.push_back(std::move(g));
            }
        }
        ImGui::SameLine();
        // Video ingest — reveng_add_video extracts + blur-culls frames server-side and
        // returns a run_id (long job; frames land in the session when it completes).
        if (ImGui::Button("Add video\xe2\x80\xa6") && !sid.empty()) {
            std::string path = ui::openFileDialog(
                "Video (*.mp4 *.mov *.avi *.mkv *.webm)\0*.mp4;*.mov;*.avi;*.mkv;*.webm\0"
                "All files (*.*)\0*.*\0");
            if (!path.empty()) {
                nlohmann::json args;
                args["session_id"] = sid;
                args["path"] = path;
                st.rpc.call("reveng_add_video", args, [](const core::RpcResult&){});
            }
        }
        // (drag-drop hint lives on the gallery itself — a SameLine caption here clipped at
        // the panel edge on narrow layouts)
        ImGui::Spacing();

        // RE-19: live webcam capture widget (CameraBackend; stub fallback).
        // session_dir empty → widget resolves $AICAD_REVENG_DIR/<sid>/images.
        drawWebcamCapture(st, sid, /*session_dir=*/"");
        ImGui::Spacing();

        // Phone capture (start_capture_session / stop_capture_session).
        drawPhoneCaptureBlock(st);
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
            "Select or create a session, then add photos via webcam, phone, "
            "file picker, or drag-drop. The pipeline will guide you through Scale "
            "and Reconstruct before reaching Model.");
        // Still allow phone capture without a session — photos land in gallery after stop
        // and can be attached once a session is selected.
        drawPhoneCaptureBlock(st);
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

    // Payload accept (ImGui internal DnD) — must immediately follow the drop-zone child
    // so the target region IS the drop-zone (previously sat after the Browse button, so
    // drops only registered on the button).
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

    // Visible browse affordance alongside drag-drop.
    if (ImGui::Button("Browse\xe2\x80\xa6##ds")) {
        std::string path = ui::openFileDialog(
            "Spec sheet (*.pdf *.png *.jpg *.jpeg)\0*.pdf;*.png;*.jpg;*.jpeg\0"
            "All files (*.*)\0*.*\0");
        if (!path.empty()) kickDatasheetIngest(st, path);
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
