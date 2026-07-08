// Reverse Engineering workspace — full-viewport body for the "Reverse Eng" title-bar tab.
// Honest scan -> mesh -> AI -> parametric pipeline shell (contracts/reveng.md). Layout:
//   left  : session rail (list_reveng_sessions, ASYNC) + New Session affordance
//   center: stage-driven empty-states for the selected session (or a first-run welcome)
//   right : session properties (honesty tier, artifacts, scale) — read-only for the shell
//   bottom: the six-stage stepper (Capture · Scale · Reconstruct · Model · Review · Deliver)
//
// GL-thread only. The session rail loads via ASYNC st.rpc.call — NEVER a sync worker call in
// a draw path (known freeze bug). If the reveng MCP tools are not registered yet the list
// call fails gracefully and the rail shows an honest empty/unavailable state.
#include "reveng_workspace.h"

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"

#include "reveng_internal.h"
#include "dim_review.h"  // RE-22: drawDimReviewPanel / drawDeviationPanel
// RE-ANCHOR(RE-19): capture suite — webcam + gallery wired into Capture stage
#include "webcam_capture.h"
#include "image_gallery.h"

namespace ui {
namespace reveng {

RevEngState& S() {
    static RevEngState s;
    return s;
}

// ---- async loaders -------------------------------------------------------------

// Refresh the session rail. Throttled (>=4s between polls) and guarded against overlapping
// in-flight calls. Tolerates the reveng MCP family not being registered yet — a transport
// error or {ok:false} just sets a sticky, human-readable listErr and leaves sessions empty.
static void loadSessions(AppState& st, bool force = false) {
    if (!st.workerStarted) return;
    RevEngState& s = S();
    if (s.listPending) return;
    const double now = ImGui::GetTime();
    if (!force && s.listedOnce && now - s.lastListT < 4.0) return;
    s.listPending = true;
    s.lastListT = now;
    st.rpc.call("list_reveng_sessions", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        RevEngState& st2 = S();
        st2.listPending = false;
        st2.listedOnce = true;
        if (!r.ok) {
            st2.listErr = r.err.empty() ? "worker unreachable" : r.err;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            // reveng tools not registered yet, or an honest failure envelope.
            st2.listErr = r.result.is_object()
                              ? r.result.value("reason", std::string("Reverse Eng tools unavailable"))
                              : std::string("Reverse Eng tools unavailable");
            st2.sessions.clear();
            return;
        }
        st2.listErr.clear();
        st2.sessions.clear();
        if (r.result.contains("sessions") && r.result["sessions"].is_array()) {
            for (const auto& j : r.result["sessions"]) {
                if (!j.is_object()) continue;
                SessionSummary ss;
                ss.session_id = j.value("session_id", std::string());
                ss.name = j.value("name", std::string("(unnamed)"));
                ss.status = j.value("status", std::string());
                ss.images = j.value("images", 0);
                if (j.contains("honesty") && j["honesty"].is_object())
                    ss.approximate = j["honesty"].value("approximate", false);
                ss.approximate = ss.approximate || j.value("approximate", false);
                st2.sessions.push_back(std::move(ss));
            }
        }
        // Clamp selection to the refreshed list.
        if (st2.selected >= (int)st2.sessions.size()) st2.selected = -1;
    });
}

// ---- small drawing helpers -----------------------------------------------------

static const char* kStageLabels[kStageCount] = {
    "Capture", "Scale", "Reconstruct", "Model", "Review", "Deliver",
};

// Center empty-state copy per stage — describes what the stage will do, honestly noting
// the shell is not yet wired to the pipeline.
static const char* stageBlurb(Stage stg) {
    switch (stg) {
        case Stage::Capture:
            return "Add photos, a video, a datasheet, or a live webcam capture to start a\n"
                   "reverse-engineering session. The pipeline never claims direct\n"
                   "scan->parametric — single-photo tiers are tagged approximate.";
        case Stage::Scale:
            return "Set real-world scale from an ArUco mat, two picked points, or a known\n"
                   "dimension. Scale must be confirmed before reconstruction runs.";
        case Stage::Reconstruct:
            return "Sparse -> dense -> mesh (or 3D Gaussian splat) reconstruction of the\n"
                   "captured views into a scaled mesh body.";
        case Stage::Model:
            return "Fit primitives and build an editable parametric model grounded in the\n"
                   "fitted dimensions, with a mesh-body fallback.";
        case Stage::Review:
            return "Deviation heat-map (mesh vs. B-rep) and compare-parts loop to prove the\n"
                   "model matches the scan within tolerance.";
        case Stage::Deliver:
            return "Attach the result to the parts hub, export a bundle, or print a scale mat.";
    }
    return "";
}

static void drawCenteredEmptyState(const char* title, const char* body) {
    const float sc = ui::uiScale;
    ImVec2 avail = ImGui::GetContentRegionAvail();
    // Rough vertical centering.
    ImGui::Dummy(ImVec2(0, avail.y * 0.32f));
    {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.accent[0], palette.accent[1],
                                                    palette.accent[2], 1.0f));
        const ImVec2 ts = ImGui::CalcTextSize(title);
        ImGui::SetCursorPosX((avail.x - ts.x) * 0.5f);
        ImGui::TextUnformatted(title);
        ImGui::PopStyleColor();
    }
    ImGui::Dummy(ImVec2(0, 6 * sc));
    // Body: center each line.
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
    const char* p = body;
    while (p && *p) {
        const char* nl = std::strchr(p, '\n');
        const char* end = nl ? nl : p + std::strlen(p);
        const ImVec2 ls = ImGui::CalcTextSize(p, end);
        ImGui::SetCursorPosX((avail.x - ls.x) * 0.5f);
        ImGui::TextUnformatted(p, end);
        if (!nl) break;
        p = nl + 1;
    }
    ImGui::PopStyleColor();
}

// Left session rail.
static void drawSessionRail(AppState& st, float w) {
    const float sc = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 8 * sc));
    ImGui::BeginChild("##reveng_rail", ImVec2(w, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();

    ImGui::TextUnformatted("Sessions");
    ImGui::SameLine(ImGui::GetContentRegionAvail().x - 24 * sc);
    if (ImGui::SmallButton("R")) loadSessions(st, /*force=*/true);
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Refresh session list");
    ImGui::Separator();

    RevEngState& s = S();
    if (!st.workerStarted) {
        ImGui::TextDisabled("Worker not running.");
    } else if (s.sessions.empty()) {
        if (!s.listErr.empty())
            ImGui::TextWrapped("No sessions. %s", s.listErr.c_str());
        else
            ImGui::TextDisabled("No sessions yet.\nStart one from the ribbon.");
    } else {
        for (int i = 0; i < (int)s.sessions.size(); ++i) {
            const SessionSummary& ss = s.sessions[i];
            ImGui::PushID(i);
            const bool sel = (s.selected == i);
            std::string label = ss.name;
            if (ss.approximate) label += "  [approx]";
            if (ImGui::Selectable(label.c_str(), sel)) s.selected = i;
            if (!ss.status.empty()) {
                ImGui::SameLine();
                ImGui::TextDisabled("%s", ss.status.c_str());
            }
            ImGui::PopID();
        }
    }
    ImGui::EndChild();
}

// Right properties column — read-only summary for the selected session.
static void drawProperties(AppState& st, float w) {
    (void)st;
    const float sc = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 8 * sc));
    ImGui::BeginChild("##reveng_props", ImVec2(w, 0), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();

    ImGui::TextUnformatted("Properties");
    ImGui::Separator();

    RevEngState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
        ImGui::TextDisabled("Select a session\nto see its details.");
    } else {
        const SessionSummary& ss = s.sessions[s.selected];
        ImGui::Text("Name");
        ImGui::TextDisabled("%s", ss.name.c_str());
        ImGui::Spacing();
        ImGui::Text("Session ID");
        ImGui::TextDisabled("%s", ss.session_id.c_str());
        ImGui::Spacing();
        ImGui::Text("Status");
        ImGui::TextDisabled("%s", ss.status.empty() ? "(unknown)" : ss.status.c_str());
        ImGui::Spacing();
        ImGui::Text("Images");
        ImGui::TextDisabled("%d", ss.images);
        ImGui::Spacing();
        ImGui::Text("Pipeline");
        if (ss.approximate) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.6f, 0.2f, 1.0f));
            ImGui::TextWrapped("approximate — reference/preview only");
            ImGui::PopStyleColor();
        } else {
            ImGui::TextDisabled("scan -> mesh -> AI -> parametric");
        }
    }
    ImGui::EndChild();
}

// Bottom stage stepper.
static void drawStepper(AppState& st) {
    (void)st;
    const float sc = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 6 * sc));
    ImGui::BeginChild("##reveng_stepper", ImVec2(0, 40 * sc), ImGuiChildFlags_Border);
    ImGui::PopStyleVar();

    RevEngState& s = S();
    for (int i = 0; i < kStageCount; ++i) {
        if (i > 0) { ImGui::SameLine(); ImGui::TextDisabled(">"); ImGui::SameLine(); }
        const bool active = ((int)s.stage == i);
        if (active) {
            ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(palette.accent[0], palette.accent[1],
                                                          palette.accent[2], 0.85f));
        }
        if (ImGui::Button(kStageLabels[i])) s.stage = (Stage)i;
        if (active) ImGui::PopStyleColor();
    }
    ImGui::EndChild();
}

// ---- entry point ---------------------------------------------------------------

void drawRevEngWorkspace(AppState& st) {
    // Kick / refresh the async session list (throttled, non-blocking).
    loadSessions(st);

    // RE-ANCHOR(reveng/RE-32): consume a session_id seeded from outside (e.g. the
    // Parts Library "Generate 3D model" action, which calls reveng_make_part_from_library
    // and stores the returned session_id here so the rail can auto-select it).
    // We try to match on the freshly-loaded session list; if the list hasn't arrived yet
    // we leave the id in place and it will be consumed on the next frame once the list
    // has populated. Clear the id once consumed (or if no match — avoids a sticky stale
    // selection across unrelated workspace visits).
    if (!st.pendingRevengSessionId.empty()) {
        RevEngState& s = S();
        if (s.listedOnce) {
            const std::string want = st.pendingRevengSessionId;
            st.pendingRevengSessionId.clear();
            for (int i = 0; i < (int)s.sessions.size(); ++i) {
                if (s.sessions[i].session_id == want) {
                    s.selected = i;
                    break;
                }
            }
            // If the session hasn't appeared yet (backend still building), force a fresh
            // list load on the next poll interval so it surfaces promptly.
            if (s.selected < 0 || s.selected >= (int)s.sessions.size() ||
                s.sessions[s.selected].session_id != want) {
                s.lastListT = -100.0;   // expire throttle → next loadSessions fires immediately
            }
        }
    }

    const float sc = ui::uiScale;
    const float railW = 240.0f * sc;
    const float propW = 260.0f * sc;
    const float gutter = 8.0f * sc;
    const float stepperH = 48.0f * sc;

    ImVec2 avail = ImGui::GetContentRegionAvail();
    const float bodyH = avail.y - stepperH;

    // RE-19: reset gallery when selected session changes
    {
        RevEngState& s = S();
        if (s.selected != s.gallerySession) {
            galleryFreeTextures(s.galleryImages);
            s.galleryImages.clear();
            s.gallerySession = s.selected;
        }
    }

    // ---- upper body: rail | center | properties ----
    ImGui::BeginChild("##reveng_body", ImVec2(0, bodyH), ImGuiChildFlags_None);
    {
        float totalW = ImGui::GetContentRegionAvail().x;
        float midW = totalW - railW - propW - 2.0f * gutter;
        if (midW < 200.0f * sc) midW = 200.0f * sc;

        drawSessionRail(st, railW);
        ImGui::SameLine(0.0f, gutter);

        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * sc, 8 * sc));
        ImGui::BeginChild("##reveng_center", ImVec2(midW, 0), ImGuiChildFlags_Border);
        ImGui::PopStyleVar();
        {
            RevEngState& s = S();
            if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
                drawCenteredEmptyState(
                    "Reverse Engineering",
                    "Turn photos, scans, or a datasheet into an editable parametric model.\n"
                    "Start a session from the ribbon, or select one from the rail.\n"
                    "Pipeline: scan -> mesh -> AI -> parametric (mesh-body fallback).");
            } else if (s.stage == Stage::Review) {
                // RE-ANCHOR(reveng): RE-22 — dim-review + deviation panels replace the
                // generic empty-state for the Review stage.
                const float sc2 = ui::uiScale;
                const float halfH = (ImGui::GetContentRegionAvail().y - 12.0f * sc2) * 0.5f;
                // Top half: dimension-confirm review table.
                ImGui::BeginChild("##reveng_dimreview", ImVec2(0, halfH), ImGuiChildFlags_None);
                reveng::drawDimReviewPanel(st);
                ImGui::EndChild();
                ImGui::Dummy(ImVec2(0, 6.0f * sc2));
                // Bottom half: deviation heat summary.
                ImGui::BeginChild("##reveng_deviation", ImVec2(0, 0), ImGuiChildFlags_None);
                reveng::drawDeviationPanel(st);
                ImGui::EndChild();
            } else {
                char title[128];
                std::snprintf(title, sizeof(title), "%s — %s",
                              kStageLabels[(int)s.stage],
                              s.sessions[s.selected].name.c_str());
                // RE-ANCHOR(RE-19): Capture stage shows the live webcam + gallery;
                // other stages keep the blurb empty-state.
                if (s.stage == Stage::Capture) {
                    ImGui::TextUnformatted(title);
                    ImGui::Separator();
                    // Webcam capture widget (stub camera works headless)
                    const std::string& sid = s.sessions[s.selected].session_id;
                    drawWebcamCapture(st, sid, /*session_dir=*/".");
                    ImGui::Spacing();
                    // Image gallery / dropzone
                    std::vector<std::string> dropped;
                    galleryLoadPendingThumbs(s.galleryImages);
                    drawImageGallery(st, sid, s.galleryImages, dropped);
                    // Fire async reveng_add_images for drag-dropped files
                    if (!dropped.empty() && !sid.empty()) {
                        nlohmann::json args;
                        args["session_id"] = sid;
                        args["paths"] = nlohmann::json(dropped);
                        st.rpc.call("reveng_add_images", args,
                                    [](const core::RpcResult&){});
                    }
                } else {
                    drawCenteredEmptyState(title, stageBlurb(s.stage));
                }
            }
        }
        ImGui::EndChild();

        ImGui::SameLine(0.0f, gutter);
        drawProperties(st, propW);
    }
    ImGui::EndChild();

    // ---- bottom: stage stepper ----
    drawStepper(st);
}

}  // namespace reveng

// Thin forwarder in the ui:: namespace so app.cpp calls ui::drawRevEngWorkspace(st).
void drawRevEngWorkspace(AppState& st) { reveng::drawRevEngWorkspace(st); }

}  // namespace ui
