// AI-CAD status bar — state + live agent phase (left), provider/model summary (right).
#include <cmath>
#include <string>

#include "imgui.h"

#include "metrics.h"
#include "status_bar.h"

#include "../core/app_state.h"
#include "sketch_mode.h"   // C18
#include "../core/sim_client.h"

namespace ui {

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
    const bool simForce = std::getenv("AI_CAD_SIMPROG") != nullptr;  // headless smoke: force the chip
    if ((st.simClient && st.simClient->busy()) || simForce) {
        static const char* kSpin[] = {"|", "/", "-", "\\"};
        int si = static_cast<int>(ImGui::GetTime() * 8.0) & 3;
        const bool live = st.simClient && st.simClient->busy();
        std::string stg = live ? st.simClient->stage() : std::string("solve");
        std::string lg  = live ? st.simClient->recentLog()
                               : std::string("assembling stiffness matrix (12,480 nodes)");
        float elapsed   = live ? st.simClient->elapsedSeconds() : 7.0f;
        // Human-readable stage labels that match the worker's stage token vocabulary.
        auto prettyStage = [](const std::string& s) -> std::string {
            if (s == "voxelize") return "Voxelizing";
            if (s == "mesh")     return "Meshing";
            if (s == "assemble") return "Assembling";
            if (s == "solve")    return "Solving";
            if (s == "stream")   return "Streaming results";
            if (s == "post")     return "Post-processing";
            return s.empty() ? "Solving" : s;
        };
        std::string chip = std::string("Sim ") + kSpin[si] + "  " + prettyStage(stg);
        if (!lg.empty()) chip += "  \xe2\x80\x94  " + lg;  // em-dash separator before detail
        chip += "  (" + std::to_string(static_cast<int>(elapsed)) + "s)";
        ImGui::SameLine();
        ImVec2 tsz = ImGui::CalcTextSize(chip.c_str());
        ImVec2 cp  = ImGui::GetCursorScreenPos();
        const float px = 10.0f * dpiS, py = 2.5f * dpiS;
        ui::glassBackground(ImGui::GetWindowDrawList(),
                            ImVec2(cp.x - px * 0.5f, cp.y - py),
                            ImVec2(cp.x + tsz.x + px, cp.y + tsz.y + py),
                            (tsz.y + py * 2.0f) * 0.5f / dpiS);
        // Pulse the text alpha slightly so it is visually "alive" even during a long quiet stage.
        float pulse = 0.80f + 0.20f * std::sin(static_cast<float>(ImGui::GetTime()) * 3.0f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.statusOk[0] * pulse, pal.statusOk[1] * pulse,
                   pal.statusOk[2] * pulse, 1.0f));
        ImGui::TextUnformatted(chip.c_str());
        // Clicking the progress chip navigates to the Simulation tab so the user can
        // watch progress or cancel without hunting for the panel.
        if (ImGui::IsItemClicked()) {
            st.leftPaneTab = AppState::LP_Simulation;
            st.ribbonTab   = AppState::RT_Simulation;
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Click to focus the Simulation pane");
        ImGui::PopStyleColor();
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
    const char* provName = st.settings.provider == 0 ? "Anthropic"
                         : st.settings.provider == 1 ? "OpenAI"
                         : st.settings.provider == 3 ? "llama.cpp"
                         : st.settings.provider == 4 ? "Gemini"
                         : st.settings.provider == 5 ? "Cloud API" : "Local";
    std::string ptxt = std::string(provName) + "  /  " +
                       (st.settings.model[0] ? st.settings.model : "default") +
                       (st.settings.planMode ? "   [plan mode]" : "");
    // C24: the unit-system cluster mirrors SolidWorks — it shows MMGS / IPS and clicking it
    // toggles the active document unit system, reformatting every dimension display app-wide.
    const char* unitsTxt = st.ips() ? "IPS" : "MMGS";
    const char* editTxt  = st.docMirror.hasDoc()
                         ? (isAsm ? "Editing Assembly" : "Editing Part") : "";
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
    float total = (vsw > 0 ? vsw + sepw : 0.0f) + uniw + sepw +
                  (editw > 0 ? editw + sepw : 0.0f) + ptw;
    ImGui::SameLine(ImGui::GetWindowWidth() - total - 16.0f * dpiS);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1));
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
