// Self-contained Interwebz widget preview — showcases every ui::Interwebz* control
// in a vertical-rail + 2-column group layout. Open flag is static; Main wires the
// menu via InterwebzDemoToggle() / InterwebzDemoDraw() (no AppState this wave).
#include "interwebz.h"

#include <cstdio>

#include "imgui.h"
#include "imgui_internal.h"

#include "../../ui_theme.h"

namespace ui {
namespace {

bool s_demoOpen = false;

void drawDemoWindow() {
    if (!s_demoOpen) return;

    const float S = ui::uiScale;
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(
        ImVec2(vp->WorkPos.x + 80.0f * S, vp->WorkPos.y + 70.0f * S), ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowSize(ImVec2(720.0f * S, 560.0f * S), ImGuiCond_FirstUseEver);

    ui::pushGlassWindowStyle();
    const bool begun = ImGui::Begin("Interwebz Widgets##iz_demo", &s_demoOpen,
                                    ImGuiWindowFlags_NoCollapse);
    ui::popGlassWindowStyle();
    if (!begun) {
        ImGui::End();
        return;
    }

    // Frosted adaptive backdrop when the live viewport is available.
    {
        const ImVec2 p0 = ImGui::GetWindowPos();
        const ImVec2 p1(p0.x + ImGui::GetWindowSize().x, p0.y + ImGui::GetWindowSize().y);
        const ImRect winRect(p0, p1);
        const AdaptiveSurface& surf =
            adaptivePanel(panelId("host_interwebz_demo"), winRect, GlassGroup::Copilot);
        glassPanel(ImGui::GetWindowDrawList(), winRect, GlassLevel::Regular, 12.0f, surf);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    }

    // Demo state (static — preview only).
    static int  s_rail     = 0;
    static int  s_hTab     = 0;
    static bool s_offline  = true;
    static bool s_vision   = false;
    static bool s_plan     = true;
    static bool s_stream   = true;
    static float s_temp    = 0.4f;
    static float s_effort  = 0.7f;
    static int   s_tokens  = 4096;
    static int   s_refine  = 2;
    static int   s_provider = 0;
    static int   s_model    = 1;

    static const char* kRailLabels[] = {
        "Provider", "Model", "Accounts", "Roles", "Agent", "Advanced",
    };
    // Icons optional; nullptrs so missing atlas names never trip icon lint.
    static const char* kRailIcons[] = {
        nullptr, nullptr, nullptr, nullptr, nullptr, nullptr,
    };
    static const char* kHTabs[] = {"General", "Limits", "Privacy"};
    static const char* kProviders[] = {"OpenAI", "Anthropic", "Local", "Custom"};
    static const char* kModels[] = {"fast", "default", "slow", "vision"};

    ImGui::TextUnformatted("Interwebz control kit");
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1));
    ImGui::TextUnformatted("  — Liquid Glass tokens, draw-list widgets");
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 4.0f * S));

    // Horizontal pill tabs
    {
        ImGui::BeginGroup();
        for (int i = 0; i < 3; ++i) {
            if (i) ImGui::SameLine(0.0f, 6.0f * S);
            ImGui::PushID(i);
            if (InterwebzTabPill(kHTabs[i], s_hTab == i, ImVec2(96.0f * S, 26.0f * S)))
                s_hTab = i;
            ImGui::PopID();
        }
        ImGui::EndGroup();
    }
    ImGui::Dummy(ImVec2(0, 6.0f * S));

    // Body: vertical rail | content
    const float railW = 132.0f * S;
    const float gap   = 12.0f * S;

    InterwebzVTabRail("##iz_demo_rail", kRailLabels, kRailIcons, 6, &s_rail, railW);
    ImGui::SameLine(0.0f, gap);

    ImGui::BeginChild("##iz_demo_body", ImVec2(0, 0), ImGuiChildFlags_None);

    {
        char cap[64];
        std::snprintf(cap, sizeof(cap), "Category: %s", kRailLabels[s_rail]);
        ImGui::PushStyleColor(ImGuiCol_Text,
                              ImVec4(palette.textDim[0], palette.textDim[1],
                                     palette.textDim[2], 1));
        ImGui::TextUnformatted(cap);
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 4.0f * S));
    }

    InterwebzTwoColumnBegin(12.0f);

    {
        const float colH = ImMax(220.0f * S, ImGui::GetContentRegionAvail().y - 8.0f * S);
        if (InterwebzGroupBegin("##iz_left", "Connection", ImVec2(0, colH))) {
            InterwebzCheckbox("Offline mode", &s_offline);
            InterwebzCheckbox("Enable vision", &s_vision);
            InterwebzCheckbox("Plan mode", &s_plan);
            InterwebzCheckbox("Stream tokens", &s_stream);
            ImGui::Dummy(ImVec2(0, 4.0f * S));
            InterwebzCombo("Provider", &s_provider, kProviders, 4);
            InterwebzCombo("Model", &s_model, kModels, 4);
            ImGui::Dummy(ImVec2(0, 4.0f * S));
            InterwebzKeyValueRow("Status", s_offline ? "offline" : "online");
            InterwebzKeyValueRow("Theme",
                                 activeTheme == Theme::Light ? "light" : "dark");
            InterwebzKeyValueRow("uiScale",
                                 (S >= 1.45f) ? "1.5" : (S >= 1.2f ? "1.25" : "1.0"));
        }
        InterwebzGroupEnd();
    }

    InterwebzTwoColumnNext();

    {
        const float colH = ImMax(220.0f * S, ImGui::GetContentRegionAvail().y - 8.0f * S);
        if (InterwebzGroupBegin("##iz_right", "Limits", ImVec2(0, colH))) {
            InterwebzSliderFloat("Temperature", &s_temp, 0.0f, 1.0f, "%.2f");
            InterwebzSliderFloat("Reasoning effort", &s_effort, 0.0f, 1.0f, "%.2f");
            InterwebzSliderInt("Max tokens", &s_tokens, 256, 32768, "%d");
            InterwebzSliderInt("Refine passes", &s_refine, 0, 8, "%d");
            ImGui::Dummy(ImVec2(0, 6.0f * S));
            InterwebzKeyValueRow("Rail index", kRailLabels[s_rail]);
            InterwebzKeyValueRow("H-tab", kHTabs[s_hTab]);
            char tokBuf[32];
            std::snprintf(tokBuf, sizeof(tokBuf), "%d", s_tokens);
            InterwebzKeyValueRow("Tokens", tokBuf);
        }
        InterwebzGroupEnd();
    }

    InterwebzTwoColumnEnd();

    ImGui::EndChild();

    ImGui::PopStyleColor();  // adaptive text
    ImGui::End();
}

}  // namespace

void InterwebzDemoToggle() { s_demoOpen = !s_demoOpen; }

void InterwebzDemoDraw() { drawDemoWindow(); }

// Back-compat: external bool* form still available if a caller prefers ownership.
void InterwebzDemoWindow(bool* open) {
    if (!open) return;
    s_demoOpen = *open;
    drawDemoWindow();
    *open = s_demoOpen;
}

}  // namespace ui
