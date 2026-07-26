// Chudware status bar — state + live agent phase (left), provider/model summary (right).
#include <cmath>
#include <string>

#include "imgui.h"

#include "metrics.h"
#include "model_registry.h"
#include "status_bar.h"

#include "../core/app_state.h"
#include "sketch_mode.h"   // C18
#include "sketch3d_mode.h" // 3D sketch status readout
#include "plm/plm_quickbar.h"  // embedded per-doc PLM control
#include "../core/sim_client.h"

namespace ui {

// [workspace-tabs seam] Parts download-queue hook (see status_bar.h). Default-implemented as
// unwired (null) — the Parts Library token shows "Downloads: 0" until the parts agent assigns it.
int (*partsDownloadQueueCount)() = nullptr;

// Human-readable stage labels that match the sim worker's stage token vocabulary.
static std::string prettySimStage(const std::string& s) {
    if (s == "voxelize") return "Voxelizing";
    if (s == "mesh")     return "Meshing";
    if (s == "assemble") return "Assembling";
    if (s == "solve")    return "Solving";
    if (s == "stream")   return "Streaming results";
    if (s == "post")     return "Post-processing";
    return s.empty() ? "Solving" : s;
}

// Title-bar workspace names, indexed by AppState::Workspace.
static const char* kWsName[] = {"Model", "Drawing", "Simulate", "Electronics",
                                "Manufacture", "PLM", "Parts Library", "AI",
                                "Reverse Eng", "Firmware"};

void drawStatusBar(AppState& st, ImGuiViewport* vp, float statusH, bool busy,
                   ImGuiWindowFlags hostFlags) {
    // ---------- status bar (state + live agent phase left, provider/model right) ----------
    ImGui::SetNextWindowPos(ImVec2(vp->Pos.x, vp->Pos.y + vp->Size.y - statusH));
    ImGui::SetNextWindowSize(ImVec2(vp->Size.x, statusH));
    ImGui::SetNextWindowViewport(vp->ID);
    float dpiS = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(14.0f * dpiS, 4.0f * dpiS));
    const auto& pal = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_WindowBg,
        ImVec4(pal.statusBg[0], pal.statusBg[1], pal.statusBg[2], pal.statusBg[3]));
    ImGui::Begin("##StatusBar", nullptr, hostFlags);
    ImVec4 sc = st.statusKind == 2
              ? ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1)
              : st.statusKind == 1
              ? ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1)
              : ImVec4(pal.statusOk[0], pal.statusOk[1], pal.statusOk[2], 1);
    ImVec2 dp0 = ImGui::GetCursorScreenPos();
    float dr = ImGui::GetFontSize() * 0.24f;
    if (busy)  // pulse while a job runs
        sc.w = 0.45f + 0.55f * (0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 5.0f));
    ImGui::GetWindowDrawList()->AddCircleFilled(
        ImVec2(dp0.x + dr + 1, dp0.y + ImGui::GetTextLineHeight() * 0.55f), dr,
        ImGui::ColorConvertFloat4ToU32(sc));
    sc.w = 1.0f;
    ImGui::Dummy(ImVec2(dr * 2 + ui::metrics::px(8), 0));
    ImGui::SameLine();
    ImGui::TextColored(sc, "%s", st.status.c_str());
    // [workspace-tabs seam] Active workspace context right after the app state — the title-bar
    // tabs write st.activeWorkspace; this keeps the bottom-left readout in sync with them.
    {
        int wsIdx = (int)st.activeWorkspace;
        if (wsIdx >= 0 && wsIdx < (int)(sizeof(kWsName) / sizeof(kWsName[0]))) {
            ImGui::SameLine();
            ImGui::TextColored(ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1),
                               "|  %s", kWsName[wsIdx]);
        }
    }
    // ---- LEFT loading / rebuild chip (ITEM 15) ----------------------------------------------
    // Mirrors the simulation-progress glass pill (spinner + stage + detail). Sits LEFT of the
    // PLM quick chip so it is never trapped under the left-pane PLM / Explorer / Feature Manager
    // rail (the old viewport "Rebuilding..." HUD was). Covers startup/warmup and rebuild/solve.
    {
        std::string chip;
        bool isStartup = false;
        if (st.setupRunning.load() || st.workerBootPending || !st.startupPhase.empty()) {
            isStartup = true;
            if (st.setupRunning.load())
                chip = "Setup  Provisioning runtime\xe2\x80\xa6";
            else if (!st.startupPhase.empty())
                chip = "Startup  " + st.startupPhase;
            else
                chip = "Startup  Starting engine\xe2\x80\xa6";
        } else if (st.rebuildsInFlight > 0) {
            // Prefer an explicit livePhase that names rebuild work; else derive progress from
            // feature_status / tree feature count / ops-in-flight. Gated on rebuildsInFlight
            // (same signal as the old viewport HUD) so agent/sim RPCs do not false-trigger it.
            chip = "Rebuild  ";
            bool named = false;
            if (!st.livePhase.empty()) {
                const std::string& lp = st.livePhase;
                if (lp.rfind("Refresh", 0) == 0 || lp.rfind("Rebuilding", 0) == 0 ||
                    lp.rfind("Solving", 0) == 0 || lp.find("rebuild") != std::string::npos ||
                    lp.find("model") != std::string::npos) {
                    chip += lp;
                    named = true;
                }
            }
            if (!named) {
                int done = 0, total = 0;
                const auto& fs = st.docMirror.featureStatus();
                if (fs.is_array()) {
                    total = (int)fs.size();
                    for (const auto& e : fs) {
                        if (!e.is_object()) continue;
                        if (e.value("ok", true) &&
                            (!e.contains("error") || e["error"].is_null() ||
                             (e["error"].is_string() && e["error"].get<std::string>().empty())))
                            ++done;
                    }
                }
                if (total <= 0) {
                    const auto& tr = st.docMirror.tree();
                    if (tr.is_object() && tr.contains("features") && tr["features"].is_array())
                        total = (int)tr["features"].size();
                }
                if (total > 0) {
                    // During a live rebuild, feature_status may still reflect the prior compile.
                    // Prefer ops-in-flight as the primary signal; use feature count as context.
                    if (done >= total) {
                        chip += "Rebuilding feature " + std::to_string(total) + "/" +
                                std::to_string(total);
                        if (st.rebuildsInFlight > 1)
                            chip += "  (" + std::to_string(st.rebuildsInFlight) + " ops)";
                        else
                            chip += "\xe2\x80\xa6";
                    } else {
                        const int cur = (done + 1 < total) ? (done + 1) : total;
                        chip += "Rebuilding feature " + std::to_string(cur) + "/" +
                                std::to_string(total);
                        if (st.rebuildsInFlight > 1)
                            chip += "  (" + std::to_string(st.rebuildsInFlight) + " ops)";
                    }
                } else if (st.rebuildsInFlight > 1) {
                    chip += "Rebuilding (" + std::to_string(st.rebuildsInFlight) +
                            " ops)\xe2\x80\xa6";
                } else {
                    chip += "Rebuilding\xe2\x80\xa6";
                }
            }
        }
        if (!chip.empty()) {
            ImGui::SameLine();
            // The mark occupies its own square ahead of the text, so the pill has to pay for
            // that width itself: the measured string no longer carries a spinner glyph.
            const float box = ImGui::GetTextLineHeight();
            const float gap = ImGui::GetStyle().ItemInnerSpacing.x;
            ImVec2 tsz = ImGui::CalcTextSize(chip.c_str());
            ImVec2 cp  = ImGui::GetCursorScreenPos();
            const float px = 10.0f * dpiS, py = 2.5f * dpiS;
            ui::glassBackground(ImGui::GetWindowDrawList(),
                                ImVec2(cp.x - px * 0.5f, cp.y - py),
                                ImVec2(cp.x + box + gap + tsz.x + px, cp.y + tsz.y + py),
                                (tsz.y + py * 2.0f) * 0.5f / dpiS);
            float pulse = 0.80f + 0.20f * std::sin(static_cast<float>(ImGui::GetTime()) * 3.0f);
            const float* role = isStartup ? pal.statusWarn : pal.statusOk;
            // One tint for mark and text so both breathe on the same beat; grouped so the hover
            // target still spans the whole pill instead of only the text half of it.
            ImGui::BeginGroup();
            ui::busyLabel(chip.c_str(),
                          ImGui::ColorConvertFloat4ToU32(
                              ImVec4(role[0], role[1], role[2], pulse)));
            ImGui::EndGroup();
            if (ImGui::IsItemHovered()) {
                if (isStartup)
                    ImGui::SetTooltip(
                        "Engine startup / warmup progress.\n"
                        "The window is interactive; modeling tools unlock when Ready.");
                else
                    ImGui::SetTooltip(
                        "Model rebuild / solve in flight.\n"
                        "Viewport mesh may lag until this completes.");
            }
        }
    }
    // Embedded PLM control: current item's revision/lifecycle + a checkpoint/release popover,
    // reachable from every workspace (no-op in the PLM workspace and without a project).
    ui::drawPlmQuickChip(st);
    // C18: while editing a sketch, show "Under/Fully/Over Defined + DOF" tinted by status.
    if (auto* sk = st.sketchMode) {
        if (sk->active()) {
            std::string sl = sk->statusLine();
            if (!sl.empty()) {
                ImGui::SameLine();
                const float* role = sl.rfind("Over", 0) == 0 ? pal.statusErr
                                  : sl.rfind("Fully", 0) == 0 ? pal.statusOk : pal.sketchUnder;
                ImGui::TextColored(ImVec4(role[0], role[1], role[2], 1), "|  %s", sl.c_str());
            }
        }
    }
    // 3D sketch: same Under/Fully/Over Defined + DOF readout while the 3D sketcher is active.
    if (auto* sk3 = st.sketch3dMode) {
        if (sk3->active()) {
            std::string sl = sk3->statusLine();
            if (!sl.empty()) {
                ImGui::SameLine();
                const float* role = sl.rfind("Over", 0) == 0 ? pal.statusErr
                                  : sl.rfind("Fully", 0) == 0 ? pal.statusOk : pal.sketchUnder;
                ImGui::TextColored(ImVec4(role[0], role[1], role[2], 1), "|  %s", sl.c_str());
            }
        }
    }
    // H2: sticky daemon-unreachable chip — one persistent notice while hostd is
    // down (engine_events sets st.daemonUnreachable). Not a toast storm: the chip
    // stays until reconnect clears the flag.
    if (st.daemonUnreachable) {
        ImGui::SameLine();
        const char* chip = "Engine daemon unreachable \xe2\x80\x94 retrying";
        const float box = ImGui::GetTextLineHeight();
        const float gap = ImGui::GetStyle().ItemInnerSpacing.x;
        ImVec2 tsz = ImGui::CalcTextSize(chip);
        ImVec2 cp  = ImGui::GetCursorScreenPos();
        const float px = 10.0f * dpiS, py = 2.5f * dpiS;
        ui::glassBackground(ImGui::GetWindowDrawList(),
                            ImVec2(cp.x - px * 0.5f, cp.y - py),
                            ImVec2(cp.x + box + gap + tsz.x + px, cp.y + tsz.y + py),
                            (tsz.y + py * 2.0f) * 0.5f / dpiS);
        float pulse = 0.75f + 0.25f * std::sin(static_cast<float>(ImGui::GetTime()) * 4.0f);
        ImGui::BeginGroup();
        ui::busyLabel(chip, ImGui::ColorConvertFloat4ToU32(
                                ImVec4(pal.statusWarn[0], pal.statusWarn[1],
                                       pal.statusWarn[2], pulse)));
        ImGui::EndGroup();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(
                "The engine host daemon (chudware-hostd) is not reachable.\n"
                "Chudware will keep retrying (1s, 2s, 5s, then every 10s).\n"
                "Agent/shared docs reappear automatically on reconnect.");
    }
    if (busy && !st.livePhase.empty()) {
        ImGui::SameLine();
        // Live-phase progress chip on a frosted-glass pill so the running-job readout matches the
        // app-wide glass family (the dim text stays legible on glassBg in both themes).
        std::string phase = std::string("|  ") + st.livePhase;
        ImVec2 tsz = ImGui::CalcTextSize(phase.c_str());
        ImVec2 cp  = ImGui::GetCursorScreenPos();
        const float px = 8.0f * dpiS, py = 2.5f * dpiS;
        ui::glassBackground(ImGui::GetWindowDrawList(),
                            ImVec2(cp.x - px * 0.5f, cp.y - py),
                            ImVec2(cp.x + tsz.x + px, cp.y + tsz.y + py),
                            (tsz.y + py * 2.0f) * 0.5f / dpiS);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1));
        ImGui::TextUnformatted(phase.c_str());
        ImGui::PopStyleColor();
    }
    // ---- Simulation progress chip (BOTTOM-LEFT, visible while a FEA/CFD solve runs) --------
    // Shows: animated spinner | pretty stage label | last log line | elapsed seconds.
    // Uses the same glass-background pill as the agent livePhase chip above so the two styles
    // are consistent.  The text pulses slightly so it is obviously alive even when stage is
    // stuck on "Solving" for a long static analysis.
    const bool simForce = std::getenv("CHUDWARE_SIMPROG") != nullptr;  // headless smoke: force the chip
    if ((st.simClient && st.simClient->busy()) || simForce) {
        const bool live = st.simClient && st.simClient->busy();
        std::string stg = live ? st.simClient->stage() : std::string("solve");
        std::string lg  = live ? st.simClient->recentLog()
                               : std::string("assembling stiffness matrix (12,480 nodes)");
        float elapsed   = live ? st.simClient->elapsedSeconds() : 7.0f;
        std::string chip = "Sim  " + prettySimStage(stg);
        if (!lg.empty()) chip += "  \xe2\x80\x94  " + lg;  // em-dash separator before detail
        chip += "  (" + std::to_string(static_cast<int>(elapsed)) + "s)";
        ImGui::SameLine();
        const float box = ImGui::GetTextLineHeight();
        const float gap = ImGui::GetStyle().ItemInnerSpacing.x;
        ImVec2 tsz = ImGui::CalcTextSize(chip.c_str());
        ImVec2 cp  = ImGui::GetCursorScreenPos();
        const float px = 10.0f * dpiS, py = 2.5f * dpiS;
        ui::glassBackground(ImGui::GetWindowDrawList(),
                            ImVec2(cp.x - px * 0.5f, cp.y - py),
                            ImVec2(cp.x + box + gap + tsz.x + px, cp.y + tsz.y + py),
                            (tsz.y + py * 2.0f) * 0.5f / dpiS);
        // Pulse mark and text on the same beat so the chip is obviously alive even during a
        // long quiet stage.
        float pulse = 0.80f + 0.20f * std::sin(static_cast<float>(ImGui::GetTime()) * 3.0f);
        // Grouped so the click target still spans the whole pill, mark included.
        ImGui::BeginGroup();
        ui::busyLabel(chip.c_str(), ImGui::ColorConvertFloat4ToU32(
                                        ImVec4(pal.statusOk[0], pal.statusOk[1],
                                               pal.statusOk[2], pulse)));
        ImGui::EndGroup();
        // Clicking the progress chip navigates to the Simulation tab so the user can
        // watch progress or cancel without hunting for the panel.
        if (ImGui::IsItemClicked()) {
            st.leftPaneTab = AppState::LP_Simulation;
            st.ribbonTab   = AppState::RT_Simulation;
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Click to focus the Simulation pane");
    }
    // W5C: pick / selection label (e.g. "Face 12 of Body1") — shown when not editing a sketch
    // (a sketch shows its DOF status instead). The label is maintained on hover/selection change.
    if (!(st.sketchMode && st.sketchMode->active()) && !st.pickStatus.empty()) {
        ImGui::SameLine();
        ImGui::TextColored(ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1),
                           "|  %s", st.pickStatus.c_str());
    }
    // ---- right cluster (SolidWorks order): view-style | units | editing-mode | provider/model ----
    // C24: SW shows the unit system ("MMGS"/"IPS") and "Editing Part"/"Editing Assembly" near the
    // right edge of the status bar; we mirror that, with the provider/model summary furthest right
    // (click anywhere on it to open Settings).
    bool isAsm = false;
    {
        const auto& tr = st.docMirror.tree();
        if (tr.is_object()) {
            auto it = tr.find("schema");
            isAsm = (it != tr.end() && it->is_string() &&
                     it->get<std::string>() == "aicad.asm/1");
        }
    }
    // Provider name from the shared registry (covers every ProviderIndex incl. bridge
    // providers like ZenMux); the old hardcoded 0-6 chain mislabeled newer slots "Local".
    const char* provName = ui::providerLabel(st.settings.provider);
    std::string ptxt = std::string(provName) + "  /  " +
                       (st.settings.model[0] ? st.settings.model : "default") +
                       (st.settings.planMode ? "   [plan mode]" : "");
    // C24: the unit-system cluster mirrors SolidWorks — it shows MMGS / IPS and clicking it
    // toggles the active document unit system, reformatting every dimension display app-wide.
    const char* unitsTxt = st.ips() ? "IPS" : "MMGS";
    // Unsaved-changes marker: mirror the active tab's dirty asterisk next to the editing mode so
    // the save state is readable without looking up at the tab strip.
    const bool docDirty = st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
                          st.docTabs[st.activeTab].dirty;
    std::string editStr = st.docMirror.hasDoc()
                        ? std::string(isAsm ? "Editing Assembly" : "Editing Part") +
                              (docDirty ? " *" : "")
                        : std::string();
    const char* editTxt = editStr.c_str();
    // [workspace-tabs seam] Per-workspace live token, leftmost in the right cluster. Kept small
    // and quiet; degrades gracefully when idle (Simulate shows "Solver idle", Parts Library shows
    // the download-queue count via the partsDownloadQueueCount hook). Workspaces with no cheap
    // live source (Manufacture CAM post state lives in the panel, not AppState) show nothing.
    std::string wsTok;
    bool wsTokLive = false;   // true => tint statusOk (something is actively running)
    switch (st.activeWorkspace) {
        case AppState::Workspace::Simulate:
            if (st.simClient && st.simClient->busy()) {
                wsTok = "Solver: " + prettySimStage(st.simClient->stage()) + " (" +
                        std::to_string((int)st.simClient->elapsedSeconds()) + "s)";
                wsTokLive = true;
            } else {
                wsTok = "Solver idle";
            }
            break;
        case AppState::Workspace::PartsLibrary: {
            int n = partsDownloadQueueCount ? partsDownloadQueueCount() : 0;
            wsTok = "Downloads: " + std::to_string(n);
            wsTokLive = n > 0;
            break;
        }
        default: break;
    }
    // W5C: current display style (mirrors viewport_panel's DisplayStyle order: 0=Shaded
    // 1=ShadedEdges 2=HLR 3=HLV 4=Wireframe). Shown only when a document is open.
    static const char* kViewStyle[] = {"Shaded", "Shaded + Edges", "Hidden Lines",
                                        "Hidden Visible", "Wireframe"};
    const char* vsTxt = st.docMirror.hasDoc()
        ? kViewStyle[(st.displayStyle >= 0 && st.displayStyle < 5) ? st.displayStyle : 1] : "";
    std::string sep = "    |    ";
    float ptw   = ImGui::CalcTextSize(ptxt.c_str()).x;
    float uniw  = ImGui::CalcTextSize(unitsTxt).x;
    float editw = editTxt[0] ? ImGui::CalcTextSize(editTxt).x : 0.0f;
    float vsw   = vsTxt[0] ? ImGui::CalcTextSize(vsTxt).x : 0.0f;
    float sepw  = ImGui::CalcTextSize(sep.c_str()).x;
    float wstw  = wsTok.empty() ? 0.0f : ImGui::CalcTextSize(wsTok.c_str()).x;
    float total = (wstw > 0 ? wstw + sepw : 0.0f) + (vsw > 0 ? vsw + sepw : 0.0f) + uniw + sepw +
                  (editw > 0 ? editw + sepw : 0.0f) + ptw;
    ImGui::SameLine(ImGui::GetWindowWidth() - total - 16.0f * dpiS);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1));
    // --- per-workspace live token (leftmost; statusOk tint while something runs).
    if (!wsTok.empty()) {
        if (wsTokLive)
            ImGui::TextColored(ImVec4(pal.statusOk[0], pal.statusOk[1], pal.statusOk[2], 1),
                               "%s", wsTok.c_str());
        else
            ImGui::TextUnformatted(wsTok.c_str());
        ImGui::SameLine(0, 0); ImGui::TextUnformatted(sep.c_str());
        ImGui::SameLine(0, 0);
    }
    // --- view-style token (read-only label; the viewport HUD / context menu changes it).
    if (vsTxt[0]) {
        ImGui::TextUnformatted(vsTxt);
        ImGui::SameLine(0, 0); ImGui::TextUnformatted(sep.c_str());
        ImGui::SameLine(0, 0);
    }
    // --- units token: its own clickable item so it can toggle independently of the provider link.
    ImGui::TextUnformatted(unitsTxt);
    bool unitsHovered = ImGui::IsItemHovered();
    if (unitsHovered) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip("Document units: %s — click to switch to %s\n"
                          "(lengths reformat everywhere; angles stay in degrees)",
                          st.ips() ? "IPS (inch)" : "MMGS (millimeter)",
                          st.ips() ? "MMGS" : "IPS");
    }
    if (ImGui::IsItemClicked()) {
        st.toggleUnits();
        saveSettings(st.settings);   // persist the choice immediately (survives restart)
        st.addLog(std::string("[units] switched to ") + (st.ips() ? "IPS" : "MMGS"));
    }
    if (editTxt[0]) {
        ImGui::SameLine(0, 0); ImGui::TextUnformatted(sep.c_str());
        ImGui::SameLine(0, 0); ImGui::TextUnformatted(editTxt);
    }
    ImGui::SameLine(0, 0); ImGui::TextUnformatted(sep.c_str());
    // --- provider/model token: clicking opens Settings (unchanged behavior).
    ImGui::SameLine(0, 0);
    ImGui::TextUnformatted(ptxt.c_str());
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip("AI provider & agent settings");
    }
    if (ImGui::IsItemClicked()) st.openSettings = true;
    ImGui::End();
    ImGui::PopStyleColor();
    ImGui::PopStyleVar(3);
}

}  // namespace ui
