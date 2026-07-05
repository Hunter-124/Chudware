#include "ui/panels.h"
#include "ui/command_search.h"
#include "ui/command_registry.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "imgui.h"
#include <cmath>
#include <cstdlib>
#include <string>

namespace ui {

using app::AppState;

namespace {

static int inferStatusKind(const AppState& s) {
    const std::string& t = s.status;
    if (t.find("error") != std::string::npos || t.find("Error") != std::string::npos ||
        t.find("fail")  != std::string::npos || t.find("Fail")  != std::string::npos)
        return 2;
    if (t.find("warn") != std::string::npos || t.find("issue") != std::string::npos ||
        t.find("Issue") != std::string::npos)
        return 1;
    return 0;
}

static const char* viewStyleLabel(int style) {
    static const char* kViewStyle[] = {
        "Shaded", "Shaded + Edges", "Hidden Lines", "Hidden Visible", "Wireframe"
    };
    if (style >= 0 && style < 5) return kViewStyle[style];
    return "Shaded + Edges";
}

static bool isIps(const AppState& s) {
    return s.units == "in" || s.units == "IPS";
}

static void drawGlassChip(const char* text, unsigned int textRole, bool pulse = false) {
    const float S = theme::S();
    ImVec2 tsz = ImGui::CalcTextSize(text);
    ImVec2 cp  = ImGui::GetCursorScreenPos();
    const float px = theme::U(1.f);
    const float py = theme::U(0.3f);
    theme::glassBackground(ImGui::GetWindowDrawList(),
                           ImVec2(cp.x - px * 0.5f, cp.y - py),
                           ImVec2(cp.x + tsz.x + px, cp.y + tsz.y + py),
                           (tsz.y + py * 2.f) * 0.5f / S);
    float mul = pulse ? (0.80f + 0.20f * std::sin((float)ImGui::GetTime() * 3.f)) : 1.f;
    ImGui::PushStyleColor(ImGuiCol_Text, theme::roleVec(textRole, mul));
    ImGui::TextUnformatted(text);
    ImGui::PopStyleColor();
}

} // namespace

void drawStatusBar(AppState& s, float height) {
    const float S = theme::S();
    const bool busy = s.job.running.load() || s.agent.running;
    const int kind  = inferStatusKind(s);

    ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg0));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(theme::U(1.75f), theme::U(0.5f)));
    ImGui::BeginChild("##status", ImVec2(0, height), false,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 wp = ImGui::GetWindowPos(), wsz = ImGui::GetWindowSize();
    dl->AddLine(wp, ImVec2(wp.x + wsz.x, wp.y), TCOL(line), 1.f);
    dl->AddLine(ImVec2(wp.x, wp.y + wsz.y - 0.5f), ImVec2(wp.x + wsz.x, wp.y + wsz.y - 0.5f),
                theme::roleU32A(theme::pal().line, 0.35f), 1.f);

    unsigned int dotRole = kind == 2 ? theme::pal().bad
                         : kind == 1 ? theme::pal().warn
                         : theme::pal().good;
    ImVec4 sc = theme::vec(dotRole);
    if (busy)
        sc.w = 0.45f + 0.55f * (0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 5.f));

    ImVec2 dp0 = ImGui::GetCursorScreenPos();
    float dr = ImGui::GetFontSize() * 0.24f;
    dl->AddCircleFilled(
        ImVec2(dp0.x + dr + 1.f, dp0.y + ImGui::GetTextLineHeight() * 0.55f), dr,
        ImGui::ColorConvertFloat4ToU32(sc));
    sc.w = 1.f;
    ImGui::Dummy(ImVec2(dr * 2.f + theme::U(1.f), 0.f));
    ImGui::SameLine();
    ImGui::TextColored(sc, "%s", s.status.c_str());

    if (s.inSketch) {
        ImGui::SameLine();
        const char* sl = s.rebuildPct >= 0.f ? "Under Defined" : "Fully Defined";
        unsigned int role = s.rebuildPct >= 0.f ? theme::pal().warn : theme::pal().good;
        ImGui::TextColored(theme::vec(role), "|  %s", sl);
    }

    if (busy && s.agent.running && !s.agent.composer.empty()) {
        ImGui::SameLine();
        std::string phase = std::string("|  ") + s.agent.composer;
        drawGlassChip(phase.c_str(), theme::pal().txt2);
    }

    // T-DP18: mission ETA clock chip.  Shown only when the agent is running
    // and the backend estimator has produced a label (empty = no data yet).
    if (busy && s.agent.running && !s.agent.etaLabel.empty()) {
        ImGui::SameLine();
        // warn colour for wide/unknown ranges, good for a tight ETA.
        unsigned int etaRole = (s.agent.etaSeconds < 0.f)
                               ? theme::pal().warn
                               : theme::pal().good;
        drawGlassChip(s.agent.etaLabel.c_str(), etaRole);
        if (ImGui::IsItemHovered()) {
            if (s.agent.etaSeconds >= 0.f && s.agent.etaLowS >= 0.f) {
                ImGui::SetTooltip(
                    "Mission time remaining: %.0f s (range %.0f \xe2\x80\x93 %.0f s)\n"
                    "Based on measured output-token costs + live tok/s.",
                    s.agent.etaSeconds, s.agent.etaLowS, s.agent.etaHighS);
            } else {
                ImGui::SetTooltip(
                    "ETA unknown \xe2\x80\x94 some stages have no measured token cost yet.");
            }
        }
    }

    const bool simForce = std::getenv("AI_CAD_SIMPROG") != nullptr;
    if (s.simClient.busy() || simForce) {
        static const char* kSpin[] = {"|", "/", "-", "\\"};
        int si = static_cast<int>(ImGui::GetTime() * 8.0) & 3;
        const bool live = s.simClient.busy();
        std::string stg = live ? s.simClient.stage() : std::string("solve");
        std::string lg  = live ? s.simClient.recentLog()
                               : std::string("assembling stiffness matrix (12,480 nodes)");
        float elapsed   = live ? s.simClient.elapsedSeconds() : 7.f;
        auto prettyStage = [](const std::string& stgIn) -> std::string {
            if (stgIn == "voxelize") return "Voxelizing";
            if (stgIn == "mesh")     return "Meshing";
            if (stgIn == "assemble") return "Assembling";
            if (stgIn == "solve")    return "Solving";
            if (stgIn == "stream")   return "Streaming results";
            if (stgIn == "post")     return "Post-processing";
            return stgIn.empty() ? "Solving" : stgIn;
        };
        std::string chip = std::string("Sim ") + kSpin[si] + "  " + prettyStage(stg);
        if (!lg.empty()) chip += "  \xe2\x80\x94  " + lg;
        chip += "  (" + std::to_string(static_cast<int>(elapsed)) + "s)";
        ImGui::SameLine();
        drawGlassChip(chip.c_str(), theme::pal().good, true);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Click to focus the Simulation pane");
        if (ImGui::IsItemClicked())
            s.workspace = cmd::Workspace::Simulate;
    }

    if (!s.inSketch && s.selCount > 0) {
        ImGui::SameLine();
        char pick[64];
        std::snprintf(pick, sizeof(pick), "|  %d selected", s.selCount);
        ImGui::TextColored(TVEC(txt2), "%s", pick);
    }

    bool isAsm = s.docType() == cmd::DocType::Assembly;
    static const char* kModels[] = {"Demo", "Claude", "GPT", "Local", "Gemini", "Cloud"};
    int mi = s.agent.model;
    if (mi < 0 || mi >= 6) mi = 0;
    std::string ptxt = std::string(kModels[mi]) + "  /  agent";
    const char* unitsTxt = isIps(s) ? "IPS" : "MMGS";
    const char* editTxt  = s.engineReady && s.engine.hasDoc()
                         ? (isAsm ? "Editing Assembly" : "Editing Part") : "";
    const char* vsTxt    = s.engineReady && s.engine.hasDoc()
                         ? viewStyleLabel(s.viewStyle) : "";

    const char* sep = "    |    ";
    float ptw   = ImGui::CalcTextSize(ptxt.c_str()).x;
    float uniw  = ImGui::CalcTextSize(unitsTxt).x;
    float editw = editTxt[0] ? ImGui::CalcTextSize(editTxt).x : 0.f;
    float vsw   = vsTxt[0] ? ImGui::CalcTextSize(vsTxt).x : 0.f;
    float sepw  = ImGui::CalcTextSize(sep).x;
    float total = (vsw > 0 ? vsw + sepw : 0.f) + uniw + sepw +
                  (editw > 0 ? editw + sepw : 0.f) + ptw;

    ImGui::SameLine(ImGui::GetWindowWidth() - total - theme::U(2.f));
    ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));

    if (vsTxt[0]) {
        ImGui::TextUnformatted(vsTxt);
        ImGui::SameLine(0, 0); ImGui::TextUnformatted(sep);
        ImGui::SameLine(0, 0);
    }

    ImGui::TextUnformatted(unitsTxt);
    if (ImGui::IsItemHovered()) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip("Document units: %s — click to switch to %s",
                          isIps(s) ? "IPS (inch)" : "MMGS (millimeter)",
                          isIps(s) ? "MMGS" : "IPS");
    }
    if (ImGui::IsItemClicked()) {
        s.units = isIps(s) ? "mm" : "in";
        s.status = std::string("[units] switched to ") + (isIps(s) ? "IPS" : "MMGS");
    }

    if (editTxt[0]) {
        ImGui::SameLine(0, 0); ImGui::TextUnformatted(sep);
        ImGui::SameLine(0, 0); ImGui::TextUnformatted(editTxt);
    }
    ImGui::SameLine(0, 0); ImGui::TextUnformatted(sep);
    ImGui::SameLine(0, 0);
    ImGui::TextUnformatted(ptxt.c_str());
    ImGui::PopStyleColor();

    if (ImGui::IsItemHovered()) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip("AI provider & agent settings");
    }
    if (ImGui::IsItemClicked())
        requestOpenSettings(-1);

    ImGui::EndChild();
    ImGui::PopStyleVar();
    ImGui::PopStyleColor();
}

} // namespace ui
