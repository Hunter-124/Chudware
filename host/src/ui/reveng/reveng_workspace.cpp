// Reverse Engineering workspace — full-viewport body for the "Reverse Eng" title-bar tab.
// Honest scan -> mesh -> AI -> parametric pipeline shell (contracts/reveng.md). Layout:
//   left  : session rail (list_reveng_sessions, ASYNC) + New Session affordance
//   center: stage-driven empty-states / sources panel for the selected session
//   right : session properties (honesty tier, artifacts, scale) — read-only for the shell
//   bottom: ADAPTIVE stage stepper (stages depend on source lane — RE-30)
//
// RE-30: Source-mode selector (Scan / Datasheet / Web+Part# / From Parts Library) added.
//   Doc lanes (Datasheet, Web) omit Scale + Reconstruct — stepper shows Source→Model→Review→Deliver.
//   Scan keeps the full Source→Scale→Reconstruct→Model→Review→Deliver sequence.
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
#include "dim_review.h"     // RE-22: drawDimReviewPanel / drawDeviationPanel
// RE-ANCHOR(RE-19): capture suite — webcam + gallery wired into Capture stage
#include "webcam_capture.h"
#include "image_gallery.h"
#include "sources_panel.h"  // RE-30: drawSourcesPanel

namespace ui {
namespace reveng {

RevEngState& S() {
    static RevEngState s;
    return s;
}

// RE-21: Apply a reveng event to the singleton (GL-thread only, called from app.cpp drain).
void feedRevEngEvent(const RevEngEvent& ev) {
    RevEngState& s = S();
    if (ev.kind == "reconstruct_progress") {
        s.recon.active = true;
        s.recon.done   = false;
        s.recon.err.clear();
        s.recon.pct    = ev.pct;
        s.recon.phase  = ev.msg;
    } else if (ev.kind == "reconstruct_done") {
        s.recon.active = false;
        s.recon.done   = true;
        s.recon.pct    = 100;
        s.recon.err.clear();
    } else if (ev.kind == "reconstruct_error") {
        s.recon.active = false;
        s.recon.err    = ev.msg.empty() ? "Unknown reconstruction error" : ev.msg;
    }
}

// RE-27: seed the canned splat fixture (for shot modes / headless verification).
void seedSplatFixture() {
    RevEngState& s = S();
    if (s.splatLoaded) return;
    const char* repo = std::getenv("AI_CAD_REPO");
    std::string base = repo ? std::string(repo) : std::string(".");
    std::string plyPath = base + "/host/tests/fixtures/splat_tiny.ply";
    s.splatLoaded = true;
    s.splatPathTried = plyPath;
    std::string err;
    if (!s.splat.load(plyPath, err))
        std::fprintf(stderr, "reveng seedSplatFixture: %s\n", err.c_str());
}

// ---- async loaders -------------------------------------------------------------

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
        if (st2.selected >= (int)st2.sessions.size()) st2.selected = -1;
    });
}

// Resolve a session-relative artifact path against $AICAD_REVENG_DIR/<sid>/.
static std::string resolveSessionPath(const std::string& session_id,
                                      const std::string& rel_or_abs) {
    if (rel_or_abs.empty()) return {};
    if (!rel_or_abs.empty() && rel_or_abs[0] == '/') return rel_or_abs;
#if defined(_WIN32)
    if (rel_or_abs.size() > 2 && rel_or_abs[1] == ':') return rel_or_abs;
#endif
    const char* override = std::getenv("AICAD_REVENG_DIR");
    std::string root;
    if (override && override[0]) {
        root = override;
    } else {
        const char* xdg = std::getenv("XDG_DATA_HOME");
        if (xdg && xdg[0]) root = std::string(xdg) + "/AI-CAD/reveng";
        else {
            const char* home = std::getenv("HOME");
            root = (home && home[0] ? std::string(home) : std::string(".")) +
                   "/.local/share/AI-CAD/reveng";
        }
    }
    if (!root.empty() && root.back() != '/') root += '/';
    if (!session_id.empty()) {
        root += session_id;
        root += '/';
    }
    root += rel_or_abs;
    return root;
}

// Async: pull full session manifest so Reconstruct can load real splat/mesh artifacts.
static void loadSessionDetail(AppState& st, bool force = false) {
    if (!st.workerStarted) return;
    RevEngState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) return;
    const std::string& sid = s.sessions[s.selected].session_id;
    if (sid.empty()) return;
    if (s.detailPending) return;
    const double now = ImGui::GetTime();
    if (!force && s.detailSessionId == sid && now - s.lastDetailT < 3.0) return;
    s.detailPending = true;
    s.lastDetailT = now;
    nlohmann::json params = nlohmann::json::object();
    params["session_id"] = sid;
    st.rpc.call("get_reveng_session", params, [sid](const core::RpcResult& r) {
        RevEngState& st2 = S();
        st2.detailPending = false;
        if (!r.ok) {
            st2.detailErr = r.err.empty() ? "get_reveng_session failed" : r.err;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            st2.detailErr = r.result.is_object()
                ? r.result.value("reason", std::string("get_reveng_session failed"))
                : std::string("get_reveng_session failed");
            return;
        }
        st2.detailErr.clear();
        st2.detailSessionId = sid;
        st2.artifactSplatPath.clear();
        st2.artifactMeshPath.clear();
        st2.artifactSparseDir.clear();
        if (!r.result.contains("session") || !r.result["session"].is_object()) return;
        const auto& sess = r.result["session"];
        if (sess.contains("artifacts") && sess["artifacts"].is_object()) {
            const auto& art = sess["artifacts"];
            auto take = [&](const char* key) -> std::string {
                if (!art.contains(key) || art[key].is_null()) return {};
                if (art[key].is_string()) return art[key].get<std::string>();
                return {};
            };
            const std::string splat = take("splat_path");
            const std::string mesh  = take("mesh_path");
            const std::string sparse = take("sparse_dir");
            if (!splat.empty())
                st2.artifactSplatPath = resolveSessionPath(sid, splat);
            if (!mesh.empty())
                st2.artifactMeshPath = resolveSessionPath(sid, mesh);
            if (!sparse.empty())
                st2.artifactSparseDir = resolveSessionPath(sid, sparse);
        }
        // If reconstruction completed server-side, mirror that into recon.done so the
        // stage UI + splat loader engage even without a live event stream.
        if (sess.contains("stages") && sess["stages"].is_object()) {
            const std::string reconBadge = sess["stages"].value("reconstruct", std::string());
            if (reconBadge == "done" || reconBadge == "complete") {
                st2.recon.done = true;
                st2.recon.active = false;
                st2.recon.pct = 100;
            }
        }
        const std::string status = sess.value("status", std::string());
        if (status == "done" || status == "review" || status == "modeling") {
            // Artifacts may exist even if reconstruct badge is still "idle" in older sessions.
            if (!st2.artifactSplatPath.empty() || !st2.artifactMeshPath.empty()) {
                st2.recon.done = true;
            }
        }
    });
}

// Prefer session splat; fall back to the canned fixture only when no session artifact
// exists and reconstruction is marked done (demo / shot path).
static void ensureSplatForSession(RevEngState& s) {
    // Pick the path we want to show.
    std::string want;
    bool demoFallback = false;
    if (!s.artifactSplatPath.empty()) {
        want = s.artifactSplatPath;
    } else if (s.recon.done) {
        // Explicit demo fallback — only when reconstruction completed with no splat_path.
        const char* repo = std::getenv("AI_CAD_REPO");
        std::string base = repo ? std::string(repo) : std::string(".");
        want = base + "/host/tests/fixtures/splat_tiny.ply";
        demoFallback = true;
    } else {
        return;  // nothing to load yet
    }

    if (want.empty()) return;
    if (s.splatLoaded && s.splatPathTried == want) return;

    s.splatLoaded = true;
    s.splatPathTried = want;
    std::string err;
    if (!s.splat.load(want, err)) {
        std::fprintf(stderr, "reveng splat load failed (%s): %s\n",
                     demoFallback ? "fixture" : "session", err.c_str());
        // Allow a later retry if a better path arrives.
        if (!demoFallback) {
            s.splatLoaded = false;
            s.splatPathTried.clear();
        }
    }
}


// ---- adaptive stepper helpers --------------------------------------------------

// Returns the ordered list of stages for the current source mode.
// Doc lanes (Datasheet, Web, FromLibrary) skip Scale + Reconstruct.
static std::vector<Stage> activeStagePipeline(SourceMode mode) {
    if (mode == SourceMode::Scan) {
        return { Stage::Source, Stage::Scale, Stage::Reconstruct,
                 Stage::Model, Stage::Review, Stage::Deliver };
    }
    // Datasheet, Web, FromLibrary → doc lane: omit Scale + Reconstruct.
    return { Stage::Source, Stage::Model, Stage::Review, Stage::Deliver };
}

static const char* stageName(Stage s) {
    switch (s) {
        case Stage::Source:      return "Source";
        case Stage::Scale:       return "Scale";
        case Stage::Reconstruct: return "Reconstruct";
        case Stage::Model:       return "Model";
        case Stage::Review:      return "Review";
        case Stage::Deliver:     return "Deliver";
    }
    return "?";
}

// Clamp the active stage to the pipeline permitted by the current source mode.
static void clampStageToPipeline(RevEngState& s) {
    auto pipeline = activeStagePipeline(s.sourceMode);
    for (const Stage& st : pipeline)
        if (st == s.stage) return;
    s.stage = pipeline.front();
}

// ---- small drawing helpers -----------------------------------------------------

static const char* stageBlurb(Stage stg) {
    switch (stg) {
        case Stage::Source:
            return "Choose a source lane: scan photos/video, ingest a datasheet,\n"
                   "look up a part number on the web, or seed from the parts library.";
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

// RE-21: Reconstruct stage center panel — progress stepper + point-cloud overlay note.
// The GL overlay (rePoints) is drawn by the caller (drawRevEngWorkspace) via beginOverlay /
// endOverlay AFTER ImGui rendering; this panel draws the ImGui progress UI only.
// RE-27: splat preview prefers session.artifacts.splat_path; fixture only as demo fallback.
static void drawReconstructStage(AppState& st) {
    const float sc = ui::uiScale;
    RevEngState& s = S();

    // Keep session artifact paths fresh while this stage is open.
    loadSessionDetail(st);

    // Lazy-load canned sparse fixture into the RePoints overlay on first entry
    // (until a real sparse cloud lands via session artifacts).
    if (!s.cannedFixtureLoaded) {
        s.cannedFixtureLoaded = true;
        std::vector<render::SparsePoint>   pts;
        std::vector<render::CameraFrustum> frusta;
        render::buildCannedSparseFixture(pts, frusta);
        s.rePoints.setCloud(pts);
        s.rePoints.setFrusta(frusta);
    }

    // Load session splat when present; fixture only as explicit demo fallback.
    // GL-safe: SplatRenderer::load() is CPU-only (GL resources lazily created on first draw()).
    ensureSplatForSession(s);


    ImVec2 avail = ImGui::GetContentRegionAvail();
    ImGui::Dummy(ImVec2(0, avail.y * 0.18f));

    // Title.
    {
        const char* title = "Reconstruct";
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
        const ImVec2 ts = ImGui::CalcTextSize(title);
        ImGui::SetCursorPosX((avail.x - ts.x) * 0.5f);
        ImGui::TextUnformatted(title);
        ImGui::PopStyleColor();
    }
    ImGui::Dummy(ImVec2(0, 10 * sc));

    // ---- Sub-stage stepper: Sparse → Dense → Mesh ----------------------------
    // Three steps mirroring contracts/reveng.md §3.2 (COLMAP sparse → OpenMVS dense → mesh).
    const char* kSubSteps[]  = { "Sparse", "Dense", "Mesh" };
    const int   kNSub        = 3;
    // Determine which sub-step we're on from pct (0-33 / 34-66 / 67-100).
    int activeStep = 0;
    if (s.recon.pct >= 67) activeStep = 2;
    else if (s.recon.pct >= 34) activeStep = 1;
    const bool reconRunning = s.recon.active;
    const bool reconDone    = s.recon.done;

    // Centre the stepper row.
    const float stepW = 80.0f * sc;
    const float arrowW = 16.0f * sc;
    const float totalStepW = kNSub * stepW + (kNSub - 1) * arrowW;
    ImGui::SetCursorPosX((avail.x - totalStepW) * 0.5f);
    for (int i = 0; i < kNSub; ++i) {
        if (i > 0) {
            ImGui::SameLine(0.0f, 0.0f);
            ImGui::TextDisabled(" > ");
            ImGui::SameLine(0.0f, 0.0f);
        }
        const bool stepDone   = reconDone || (reconRunning && i < activeStep);
        const bool stepActive = reconRunning && i == activeStep;
        if (stepDone) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(0.18f, 0.65f, 0.30f, 0.85f));  // green done
        } else if (stepActive) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
        } else {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImGui::GetStyleColorVec4(ImGuiCol_Button));
        }
        ImGui::PushID(i);
        ImGui::Button(kSubSteps[i], ImVec2(stepW, 0));
        ImGui::PopID();
        ImGui::PopStyleColor();
    }

    ImGui::Dummy(ImVec2(0, 14 * sc));

    // ---- Progress bar --------------------------------------------------------
    if (reconRunning || reconDone) {
        const float barW = std::min(avail.x * 0.6f, 360.0f * sc);
        ImGui::SetCursorPosX((avail.x - barW) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_PlotHistogram,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.9f));
        char overlay[32];
        std::snprintf(overlay, sizeof(overlay), "%d%%", s.recon.pct);
        ImGui::ProgressBar((float)s.recon.pct / 100.0f, ImVec2(barW, 0), overlay);
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 6 * sc));
        if (!s.recon.phase.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
            const ImVec2 ps = ImGui::CalcTextSize(s.recon.phase.c_str());
            ImGui::SetCursorPosX((avail.x - ps.x) * 0.5f);
            ImGui::TextUnformatted(s.recon.phase.c_str());
            ImGui::PopStyleColor();
        }
    }

    // ---- Error state ---------------------------------------------------------
    if (!s.recon.err.empty()) {
        ImGui::Dummy(ImVec2(0, 8 * sc));
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.25f, 0.25f, 1.0f));
        ImGui::TextWrapped("Error: %s", s.recon.err.c_str());
        ImGui::PopStyleColor();
    }

    // ---- Splat / mesh artifact indicator ------------------------------------
    if (s.splatLoaded && s.splat.hasSplats()) {
        ImGui::Dummy(ImVec2(0, 6 * sc));
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.4f, 0.85f, 0.55f, 1.0f));
        char splatInfo[160];
        const bool isFixture = s.splatPathTried.find("splat_tiny.ply") != std::string::npos;
        std::snprintf(splatInfo, sizeof(splatInfo),
                      "3DGS splat preview: %zu gaussians%s",
                      s.splat.count(),
                      isFixture ? " (demo fixture)" : " (session)");
        const ImVec2 si = ImGui::CalcTextSize(splatInfo);
        ImGui::SetCursorPosX((avail.x - si.x) * 0.5f);
        ImGui::TextUnformatted(splatInfo);
        ImGui::PopStyleColor();
        if (!isFixture && !s.splatPathTried.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
            const ImVec2 ps = ImGui::CalcTextSize(s.splatPathTried.c_str());
            ImGui::SetCursorPosX((avail.x - ps.x) * 0.5f);
            ImGui::TextUnformatted(s.splatPathTried.c_str());
            ImGui::PopStyleColor();
        }
    } else if (!s.artifactMeshPath.empty()) {
        ImGui::Dummy(ImVec2(0, 6 * sc));
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.4f, 0.85f, 0.55f, 1.0f));
        const char* meshLbl = "Session mesh ready";
        const ImVec2 mi = ImGui::CalcTextSize(meshLbl);
        ImGui::SetCursorPosX((avail.x - mi.x) * 0.5f);
        ImGui::TextUnformatted(meshLbl);
        ImGui::PopStyleColor();
        ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
        const ImVec2 ps = ImGui::CalcTextSize(s.artifactMeshPath.c_str());
        ImGui::SetCursorPosX((avail.x - ps.x) * 0.5f);
        ImGui::TextUnformatted(s.artifactMeshPath.c_str());
        ImGui::PopStyleColor();
    }


    // ---- Idle hint (no job running) ------------------------------------------
    if (!reconRunning && !reconDone && s.recon.err.empty()) {
        ImGui::Dummy(ImVec2(0, 8 * sc));
        ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
        const char* hint =
            "Point cloud and camera frusta render in the viewport.\n"
            "Run Reconstruct from the ribbon to start sparse \xe2\x86\x92 dense \xe2\x86\x92 mesh.\n"
            "3DGS splat preview available once reconstruction completes.";
        const char* p = hint;
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
}

static void drawCenteredEmptyState(const char* title, const char* body) {
    const float sc = ui::uiScale;
    ImVec2 avail = ImGui::GetContentRegionAvail();
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

    // New-session creator: name field + "New Session" button. Calls reveng_start_session
    // async, then auto-selects the returned id (via st.pendingRevengSessionId) and lands on
    // the Source stage so the upload / link entry widgets are immediately visible.
    {
        const bool canCreate = st.workerStarted && !s.newSessionPending;
        ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x);
        ImGui::InputTextWithHint("##reveng_new_name", "Name (optional)",
                                 s.newNameBuf, sizeof(s.newNameBuf));
        if (!canCreate) ImGui::BeginDisabled();
        if (ImGui::Button(s.newSessionPending ? "Creating\xe2\x80\xa6" : "+ New Session",
                          ImVec2(ImGui::GetContentRegionAvail().x, 0))) {
            s.newSessionPending = true;
            s.newSessionErr.clear();
            nlohmann::json params = nlohmann::json::object();
            if (s.newNameBuf[0] != '\0') params["name"] = std::string(s.newNameBuf);
            st.rpc.call("reveng_start_session", params, [&st](const core::RpcResult& r) {
                RevEngState& s2 = S();
                s2.newSessionPending = false;
                if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
                    s2.newSessionErr = r.ok && r.result.is_object()
                                           ? r.result.value("reason", std::string("create failed"))
                                           : (r.err.empty() ? std::string("worker unreachable") : r.err);
                    return;
                }
                s2.newSessionErr.clear();
                s2.newNameBuf[0] = '\0';
                s2.stage = Stage::Source;            // land on Source so entry widgets show
                s2.lastListT = -100.0;               // expire throttle → refresh now
                st.pendingRevengSessionId = r.result.value("session_id", std::string());
            });
        }
        if (!canCreate) ImGui::EndDisabled();
        if (!st.workerStarted && ImGui::IsItemHovered())
            ImGui::SetTooltip("Worker not running");
        if (!s.newSessionErr.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.9f, 0.35f, 0.3f, 1.0f));
            ImGui::TextWrapped("%s", s.newSessionErr.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::Separator();
    }

    if (!st.workerStarted) {
        ImGui::TextDisabled("Worker not running.");
    } else if (s.sessions.empty()) {
        if (!s.listErr.empty())
            ImGui::TextWrapped("No sessions. %s", s.listErr.c_str());
        else
            ImGui::TextDisabled("No sessions yet.\nUse + New Session above.");
    } else {
        for (int i = 0; i < (int)s.sessions.size(); ++i) {
            const SessionSummary& ss = s.sessions[i];
            ImGui::PushID(i);
            const bool sel = (s.selected == i);
            std::string label = ss.name;
            if (ss.approximate) label += "  [approx]";
            if (ImGui::Selectable(label.c_str(), sel)) {
                s.selected = i;
                s.stage = Stage::Source;  // land on Source so entry widgets are visible
            }
            if (!ss.status.empty()) {
                ImGui::SameLine();
                ImGui::TextDisabled("%s", ss.status.c_str());
            }
            ImGui::PopID();
        }
    }
    ImGui::EndChild();
}

// Right properties column.
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

// RE-30: Source-mode selector — drawn at the top of the center panel.
// Returns true if the mode changed (caller should clamp stage).
static bool drawSourceModeSelector(RevEngState& s) {
    const float sc = ui::uiScale;
    ImGui::Spacing();
    ImGui::TextUnformatted("Source:");
    ImGui::SameLine();

    struct ModeEntry { SourceMode mode; const char* label; const char* tip; };
    static const ModeEntry kModes[4] = {
        { SourceMode::Scan,        "Scan",        "Photos / video / webcam \xe2\x86\x92 Scale \xe2\x86\x92 Reconstruct \xe2\x86\x92 Model" },
        { SourceMode::Datasheet,   "Datasheet",   "Local PDF/spec \xe2\x86\x92 Model  (no Scale / Reconstruct)" },
        { SourceMode::Web,         "Web / Part#", "Part number or URL \xe2\x86\x92 Research \xe2\x86\x92 Model" },
        { SourceMode::FromLibrary, "From Library","Seeded from the Parts Library \xe2\x80\x94 auto-routed" },
    };

    bool changed = false;
    for (int i = 0; i < 4; ++i) {
        if (i > 0) ImGui::SameLine(0.0f, 4.0f * sc);
        const bool active = (s.sourceMode == kModes[i].mode);
        if (active) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
        }
        if (ImGui::Button(kModes[i].label)) {
            if (!active) { s.sourceMode = kModes[i].mode; changed = true; }
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", kModes[i].tip);
        if (active) ImGui::PopStyleColor();
    }
    ImGui::Spacing();
    return changed;
}

// RE-30: Adaptive bottom stepper — only shows stages relevant to the active source mode.
static void drawStepper(AppState& st) {
    (void)st;
    const float sc = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 6 * sc));
    ImGui::BeginChild("##reveng_stepper", ImVec2(0, 40 * sc), ImGuiChildFlags_Border,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImGui::PopStyleVar();

    RevEngState& s = S();
    auto pipeline = activeStagePipeline(s.sourceMode);

    for (int i = 0; i < (int)pipeline.size(); ++i) {
        if (i > 0) { ImGui::SameLine(); ImGui::TextDisabled(">"); ImGui::SameLine(); }
        const Stage stg = pipeline[i];
        const bool active = (s.stage == stg);
        if (active) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
        }
        if (ImGui::Button(stageName(stg))) s.stage = stg;
        if (active) ImGui::PopStyleColor();
    }
    ImGui::EndChild();
}

// ---- entry point ---------------------------------------------------------------

void drawRevEngWorkspace(AppState& st) {
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
            // Drop previous session's splat/mesh mirror so Reconstruct reloads.
            s.splatLoaded = false;
            s.splatPathTried.clear();
            s.artifactSplatPath.clear();
            s.artifactMeshPath.clear();
            s.artifactSparseDir.clear();
            s.detailSessionId.clear();
            s.lastDetailT = -100.0;
            s.recon.done = false;
            s.recon.active = false;
            s.recon.pct = 0;
            s.recon.phase.clear();
            s.recon.err.clear();
        }
    }

    RevEngState& s = S();

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
            // RE-30: source-mode selector always visible at top of center panel.
            if (drawSourceModeSelector(s)) {
                clampStageToPipeline(s);
            }
            ImGui::Separator();

            if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
                drawCenteredEmptyState(
                    "Reverse Engineering",
                    "Turn photos, scans, or a datasheet into an editable parametric model.\n"
                    "Start a session from the ribbon, or select one from the rail.\n"
                    "Pipeline: scan -> mesh -> AI -> parametric (mesh-body fallback).");
            } else if (s.stage == Stage::Source) {
                // RE-ANCHOR(reveng): RE-30 — sources panel (mode-specific entry widgets).
                drawSourcesPanel(st);
            } else if (s.stage == Stage::Reconstruct) {
                // RE-ANCHOR(reveng): RE-21 — reconstruct stage: sub-stage stepper + progress bar.
                // The GL point-cloud + frusta overlay is drawn in drawRevEngWorkspace() below via
                // beginOverlay/endOverlay AFTER ImGui rendering so it composites over the viewport.
                drawReconstructStage(st);
            } else if (s.stage == Stage::Review) {
                // RE-22: dim-review + deviation panels.
                const float sc2 = ui::uiScale;
                const float halfH = (ImGui::GetContentRegionAvail().y - 12.0f * sc2) * 0.5f;
                ImGui::BeginChild("##reveng_dimreview", ImVec2(0, halfH), ImGuiChildFlags_None);
                reveng::drawDimReviewPanel(st);
                ImGui::EndChild();
                ImGui::Dummy(ImVec2(0, 6.0f * sc2));
                ImGui::BeginChild("##reveng_deviation", ImVec2(0, 0), ImGuiChildFlags_None);
                reveng::drawDeviationPanel(st);
                ImGui::EndChild();
            } else {
                char title[128];
                std::snprintf(title, sizeof(title), "%s — %s",
                              stageName(s.stage),
                              s.sessions[s.selected].name.c_str());
                drawCenteredEmptyState(title, stageBlurb(s.stage));
            }
        }
        ImGui::EndChild();

        ImGui::SameLine(0.0f, gutter);
        drawProperties(st, propW);
    }
    ImGui::EndChild();

    // ---- bottom: adaptive stage stepper ----
    drawStepper(st);
}

}  // namespace reveng

// Thin forwarder in the ui:: namespace so app.cpp calls ui::drawRevEngWorkspace(st).
void drawRevEngWorkspace(AppState& st) { reveng::drawRevEngWorkspace(st); }

}  // namespace ui
