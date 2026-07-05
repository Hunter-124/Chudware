// Copilot pane: sessions UX, glass chat bubbles, composer prelude/toolbar (C3).
#include "agent_panel.h"
#include "activity_panel.h"
#include "ask_dialog.h"
#include "console_panel.h"
#include "model_picker.h"
#include "model_registry.h"
#include "settings_modal.h"

#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"

#include "imgui.h"
#include "imgui_internal.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <map>
#include <vector>

namespace ui {

using namespace app;

namespace {

enum class AgentTab { Chat = 0, Console = 1, Activity = 2 };

struct Session {
    std::string title;
    std::vector<AgentBlock> blocks;
    std::vector<std::string> queuedPrompts;
    int  contextUsed = 0;
    bool running = false;
};

static std::vector<Session> gSessions;
static int  gActiveSession = 0;
static bool gShowSessions = false;
static int  gAgentMode = 0;       // 0 Design, 1 Edit, 2 Ask
static bool gShowThinking = true;
static int  gAgentTab = 0;
static std::string gChatDocKey;

static const char* kModes[] = {"Design", "Edit", "Ask"};

static std::string convDocKey(AppState& s) {
    if (s.activeTab >= 0 && s.activeTab < (int)s.docTabs.size()) {
        const auto& t = s.docTabs[s.activeTab];
        if (!t.path.empty()) return "file:" + t.path;
        if (!t.docId.empty()) return "doc:" + t.docId;
    }
    std::string d = s.docMirror.hasDoc() ? s.docMirror.docId() : std::string();
    return d.empty() ? std::string("scratch") : ("doc:" + d);
}

static void ensureSessions() {
    if (gSessions.empty()) {
        Session s; s.title = "Chat 1";
        gSessions.push_back(std::move(s));
        gActiveSession = 0;
    }
}

static void syncBlocksToSession(AppState& s) {
    ensureSessions();
    gSessions[gActiveSession].blocks = s.agent.blocks;
    gSessions[gActiveSession].running = s.agent.running;
}

static void loadSessionToAgent(AppState& s, int idx) {
    if (idx < 0 || idx >= (int)gSessions.size()) return;
    syncBlocksToSession(s);
    gActiveSession = idx;
    s.agent.blocks = gSessions[idx].blocks;
    s.agent.running = gSessions[idx].running;
    gShowSessions = false;
}

static void newSession(AppState& s) {
    ensureSessions();
    syncBlocksToSession(s);
    Session ns;
    ns.title = "Chat " + std::to_string((int)gSessions.size() + 1);
    gSessions.push_back(std::move(ns));
    gActiveSession = (int)gSessions.size() - 1;
    s.agent.blocks.clear();
    s.agent.running = false;
    gShowSessions = false;
}

static void closeSession(AppState& s, int idx) {
    if ((int)gSessions.size() <= 1) return;
    if (idx < 0 || idx >= (int)gSessions.size()) return;
    gSessions.erase(gSessions.begin() + idx);
    if (gActiveSession >= (int)gSessions.size()) gActiveSession = (int)gSessions.size() - 1;
    if (gActiveSession < 0) gActiveSession = 0;
    s.agent.blocks = gSessions[gActiveSession].blocks;
    s.agent.running = gSessions[gActiveSession].running;
}

static void syncChatToDoc(AppState& s) {
    std::string key = convDocKey(s);
    if (key == gChatDocKey) return;
    // First bind (startup): ADOPT whatever is already in the live transcript (the welcome
    // message, scenario-seeded chat) instead of wiping it. A real doc SWITCH parks the old
    // doc's sessions under its key and restores any sessions previously parked for the new key.
    static std::map<std::string, std::vector<Session>> gParkedByDoc;
    const bool firstBind = gChatDocKey.empty();
    syncBlocksToSession(s);
    if (!firstBind) gParkedByDoc[gChatDocKey] = gSessions;
    gChatDocKey = key;
    auto parked = gParkedByDoc.find(key);
    if (parked != gParkedByDoc.end() && !parked->second.empty()) {
        gSessions = parked->second;
        gActiveSession = 0;
        s.agent.blocks = gSessions[0].blocks;
        s.agent.running = gSessions[0].running;
        return;
    }
    gSessions.clear();
    gActiveSession = 0;
    Session c; c.title = "Chat 1";
    gSessions.push_back(std::move(c));
    if (firstBind) {
        gSessions[0].blocks = s.agent.blocks;
        gSessions[0].running = s.agent.running;
        return;
    }
    s.agent.blocks.clear();
    s.agent.running = false;
}

static bool aiTurnActive(const AppState& s) {
    if (s.job.running.load() || s.agent.running) return true;
    return false;
}

static int effectiveContextWindow() {
    AppSettings& cfg = appSettings();
    if (cfg.contextWindow > 0) return cfg.contextWindow;
    return defaultContextWindowForModel(cfg.provider, cfg.model);
}

static std::string fmtTokens(int n) {
    if (n >= 1000000) return std::to_string((n + 50000) / 100000) + "M";
    if (n >= 1000) return std::to_string((n + 500) / 1000) + "k";
    return std::to_string(n);
}

static void composerModelLabel(char* out, size_t n) {
    AppSettings& cfg = appSettings();
    const char* provNames[] = {"Anthropic", "OpenAI", "Local", "llama.cpp", "Gemini", "Cloud API"};
    const char* prov = provNames[(cfg.provider >= 0 && cfg.provider < PROV_COUNT)
                                     ? cfg.provider : PROV_GEMINI];
    std::snprintf(out, n, "%s \xc2\xb7 %s", prov, cfg.model[0] ? cfg.model : "default");
}

// ---- glass surface plumbing (contract §6/A1: ONE adaptivePanel id shared by the whole Copilot
// pane — header, composer, bubbles, and the Console/Activity tab bodies in the sibling TUs all
// fetch this SAME cached surface via theme::panelId("agent_glass"); adaptivePanel is idempotent
// per frame (A2), so repeated fetches after the first real sample are free.) ----
static const theme::AdaptiveSurface& agentSurf() {
    ImVec2 mn = ImGui::GetWindowPos();
    ImVec2 mx(mn.x + ImGui::GetWindowSize().x, mn.y + ImGui::GetWindowSize().y);
    return theme::adaptivePanel(theme::panelId("agent_glass"), ImRect(mn, mx));
}

// Contrast halo for a glyph over glass: opposite tone from the surface's chosen text color,
// derived from the (theme-independent) vibrancy tokens — never a raw literal.
static ImU32 haloForSurf(const theme::AdaptiveSurface& surf) {
    return theme::roleU32(surf.darkText ? theme::pal().vibrancyLightPrimary
                                        : theme::pal().vibrancyDarkPrimary, 0.31f);
}

// Drop-in glass replacement for icons::inlineIcon: same cursor-based placement, routed through
// icons::drawGlass with a surface-derived halo.
static void inlineIconGlass(const char* name, float size, ImU32 col) {
    const ImVec2 p = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(size, size));
    icons::drawGlass(ImGui::GetWindowDrawList(), name,
                     ImVec2(p.x + size * 0.5f, p.y + size * 0.5f), size, col, haloForSurf(agentSurf()));
}

// A "bubble" = an auto-height child painted over an explicit glassPanel fill. The child's true
// height is only known after EndChild(), so the PARENT draw list is channel-split: content goes
// to channel 1, and endGlassBubble() paints the correctly-sized fill into channel 0 (behind)
// once the real rect is known — measure-then-fill, no frame lag. Child-window content lives in
// the child's own draw list and is unaffected by the parent split.
struct GlassBubble {
    ImVec2               origin;
    float                width;
    theme::MaterialLevel level;
    float                rounding;
};

static GlassBubble beginGlassBubble(const char* strId, float width, theme::MaterialLevel level,
                                    float rounding) {
    (void)strId;  // kept for call-site stability / debugging
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->ChannelsSplit(2);
    dl->ChannelsSetCurrent(1);
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    return GlassBubble{p0, width, level, rounding};
}

static ImRect endGlassBubble(const GlassBubble& b) {
    ImGui::PopStyleColor();
    ImVec2 p1 = ImGui::GetItemRectMax();
    if (p1.x < b.origin.x + b.width) p1.x = b.origin.x + b.width;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->ChannelsSetCurrent(0);
    theme::glassPanel(dl, ImRect(b.origin, p1), b.level, b.rounding, agentSurf());
    dl->ChannelsMerge();
    return ImRect(b.origin, p1);
}

static float measureContextMeter(int used, int win, float roomForMeter) {
    const float S = theme::S();
    const float barW = 40.f * S, gap = 5.f * S;
    const std::string full = fmtTokens(used) + "/" + fmtTokens(win);
    const float fullTw = ImGui::CalcTextSize(full.c_str()).x;
    if (roomForMeter >= barW + gap + fullTw) return barW + gap + fullTw;
    return ImGui::CalcTextSize(("ctx " + full).c_str()).x;
}

static void drawContextMeter(int used, int win, float roomForMeter) {
    const float S = theme::S();
    const theme::AdaptiveSurface& surf = agentSurf();
    const float frac = win > 0 ? std::min(1.f, (float)used / (float)win) : 0.f;
    const float barW = 40.f * S, gap = 5.f * S;
    const std::string full = fmtTokens(used) + "/" + fmtTokens(win);
    const std::string lean = "ctx " + full;
    const float fh = ImGui::GetFrameHeight();
    const float fullTw = ImGui::CalcTextSize(full.c_str()).x;
    const bool showBar = roomForMeter >= barW + gap + fullTw;
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    if (showBar) {
        ImVec2 p = ImGui::GetCursorScreenPos();
        float by0 = p.y + fh * 0.5f - 3.f * S, by1 = p.y + fh * 0.5f + 3.f * S;
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImU32 track = TCOL(accentSoft);
        ImU32 fg = frac < 0.5f ? TCOL(good) : frac < 0.85f ? TCOL(warn) : TCOL(bad);
        dl->AddRectFilled(ImVec2(p.x, by0), ImVec2(p.x + barW, by1), track, 2.f * S);
        if (frac > 0)
            dl->AddRectFilled(ImVec2(p.x, by0), ImVec2(p.x + barW * frac, by1), fg, 2.f * S);
        dl->AddRect(ImVec2(p.x, by0), ImVec2(p.x + barW, by1),
                    ImGui::ColorConvertFloat4ToU32(surf.textSecondary), 2.f * S);
        ImGui::Dummy(ImVec2(barW, fh));
        ImGui::SameLine(0, gap);
    }
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(showBar ? full.c_str() : lean.c_str());
    ImGui::PopStyleColor();
}

static void drawUserBubble(const std::string& text, float panelW) {
    const float S = theme::S();
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + panelW * 0.22f);
    const float bw = panelW * 0.78f - 8.f * S;
    ImGui::PushID(&text);
    GlassBubble gb = beginGlassBubble("##umsgGlass", bw, theme::MaterialLevel::Regular, 8.f * S);
    ImGui::BeginChild("##umsg", ImVec2(bw, 0), ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushFont(theme::font(theme::Font::Label));
    ImGui::TextColored(TVEC(accent), "You");
    ImGui::PopFont();
    ImGui::PushTextWrapPos(0);
    ImGui::TextColored(agentSurf().textPrimary, "%s", text.c_str());
    ImGui::PopTextWrapPos();
    ImGui::EndChild();
    ImRect bubble = endGlassBubble(gb);
    ImGui::GetWindowDrawList()->AddRect(bubble.Min, bubble.Max,
                                        theme::roleU32(theme::pal().accent, 0.55f), 8.f * S, 0, 1.f * S);
    ImGui::PopID();
    ImGui::Dummy(ImVec2(0, 6.f * S));
}

static void drawActionCard(AgentBlock& b) {
    const float S = theme::S();
    ImGui::Dummy(ImVec2(0, 2.f * S));
    const float w = ImGui::GetContentRegionAvail().x;
    GlassBubble gb = beginGlassBubble("##actGlass", w, theme::MaterialLevel::Thin, 8.f * S);
    ImGui::BeginChild(ImGui::GetID(&b), ImVec2(w, 0), ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImU32 statusCol = TCOL(accent);
    if (b.status == ActionStatus::Failed) statusCol = TCOL(bad);
    else if (b.status == ActionStatus::Ok) statusCol = TCOL(good);
    inlineIconGlass("agent", 16.f * S, statusCol);
    ImGui::SameLine(0, 8.f * S);
    ImGui::PushFont(theme::font(theme::Font::Label));
    ImGui::TextColored(ImGui::ColorConvertU32ToFloat4(statusCol), "%s", b.title.c_str());
    ImGui::PopFont();
    if (!b.args.empty() && ImGui::SmallButton(b.expanded ? "Hide" : "Details"))
        b.expanded = !b.expanded;
    if (b.expanded && !b.args.empty()) {
        ImGui::PushFont(theme::font(theme::Font::Mono));
        theme::pushGlassChildStyle(4.f * S);
        ImGui::BeginChild("##args", ImVec2(0, 120.f * S), ImGuiChildFlags_Borders);
        ImGui::TextColored(agentSurf().textPrimary, "%s", b.args.c_str());
        ImGui::EndChild();
        theme::popGlassChildStyle();
        ImGui::PopFont();
    }
    if (!b.result.empty())
        ImGui::TextColored(agentSurf().textSecondary, "%s", b.result.c_str());
    if (b.status == ActionStatus::Running) {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImVec2 c = ImGui::GetCursorScreenPos();
        c.x += 10.f * S; c.y += 10.f * S;
        dl->PathClear();
        dl->PathArcTo(c, 6.f * S, b.spin, b.spin + 4.2f, 12);
        dl->PathStroke(TCOL(accent), 0, 2.f * S);
        ImGui::Dummy(ImVec2(0, 14.f * S));
    }
    ImGui::EndChild();
    endGlassBubble(gb);
}

static void drawAssistantBubble(AgentBlock& b) {
    const float S = theme::S();
    const float w = ImGui::GetContentRegionAvail().x;
    GlassBubble gb = beginGlassBubble("##asstGlass", w, theme::MaterialLevel::Thin, 8.f * S);
    ImGui::BeginChild("##asst", ImVec2(w, 0), ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    inlineIconGlass("agent", 16.f * S, ImGui::ColorConvertFloat4ToU32(agentSurf().textPrimary));
    ImGui::SameLine(0, 8.f * S);
    ImGui::PushFont(theme::font(theme::Font::Label));
    ImGui::TextColored(agentSurf().textSecondary, "Assistant");
    ImGui::PopFont();
    ImGui::PushTextWrapPos(0);
    ImGui::TextColored(agentSurf().textPrimary, "%s", b.body.c_str());
    ImGui::PopTextWrapPos();
    ImGui::EndChild();
    endGlassBubble(gb);
}

static void drawErrorBubble(AgentBlock& b) {
    const float S = theme::S();
    const float w = ImGui::GetContentRegionAvail().x;
    GlassBubble gb = beginGlassBubble("##errGlass", w, theme::MaterialLevel::Thin, 8.f * S);
    ImGui::BeginChild("##err", ImVec2(w, 0), ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::TextColored(TVEC(bad), "%s", b.body.c_str());
    ImGui::EndChild();
    endGlassBubble(gb);
}

static void drawBlock(AppState& s, AgentBlock& b, int idx, float panelW) {
    const float S = theme::S();
    ImGui::PushID(idx);
    ImGui::PushStyleVar(ImGuiStyleVar_Alpha, ImGui::GetStyle().Alpha * (0.15f + 0.85f * b.anim));
    float dy = (1.f - b.anim) * 8.f * S;
    ImGui::SetCursorPosY(ImGui::GetCursorPosY() + dy);

    switch (b.kind) {
    case BlockKind::UserMsg:
        drawUserBubble(b.body, panelW);
        break;
    case BlockKind::Thinking:
        if (gShowThinking) {
            const theme::AdaptiveSurface& surf = agentSurf();
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            inlineIconGlass("thinking", 14.f * S, ImGui::ColorConvertFloat4ToU32(surf.textSecondary));
            ImGui::SameLine(0, 6.f * S);
            ImGui::TextColored(surf.textSecondary, "%s", b.body.c_str());
            ImGui::PopStyleColor();
        }
        break;
    case BlockKind::Action:
        drawActionCard(b);
        break;
    case BlockKind::AssistantMsg:
        drawAssistantBubble(b);
        break;
    case BlockKind::Usage:
        ImGui::TextColored(agentSurf().textSecondary, "%s", b.body.c_str());
        break;
    case BlockKind::Error:
        drawErrorBubble(b);
        break;
    default:
        ImGui::TextColored(agentSurf().textSecondary, "%s", b.body.c_str());
        break;
    }
    ImGui::PopStyleVar();
    ImGui::Dummy(ImVec2(0, 7.f * S));
    ImGui::PopID();
}

static void drawSessionsList(AppState& s, bool busy) {
    const theme::AdaptiveSurface& surf = agentSurf();
    ensureSessions();
    if (ImGui::Button("+ New session") && !busy) newSession(s);
    ImGui::SameLine();
    if (ImGui::SmallButton("Back to chat")) gShowSessions = false;
    ImGui::Separator();
    ImGui::TextColored(surf.textSecondary,
        "Switch sessions — running ones keep working in the background.");
    ImGui::Dummy(ImVec2(0, 4.f * theme::S()));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    for (int i = 0; i < (int)gSessions.size(); ++i) {
        std::string label = gSessions[i].title;
        if (gSessions[i].running || (i == gActiveSession && aiTurnActive(s)))
            label += "  \xe2\x80\xa2 running";
        ImGui::PushID(i);
        float trashW = ImGui::GetFrameHeight();
        float rowW = ImGui::GetContentRegionAvail().x;
        if (ImGui::Selectable(label.c_str(), i == gActiveSession, 0,
                              ImVec2(rowW - trashW - 4.f * theme::S(), 0)) && !busy)
            loadSessionToAgent(s, i);
        ImGui::SameLine(ImGui::GetContentRegionMax().x - trashW);
        if (ImGui::SmallButton("x") && !busy && (int)gSessions.size() > 1) {
            closeSession(s, i);
            ImGui::PopID();
            break;
        }
        ImGui::PopID();
    }
    ImGui::PopStyleColor();
}

static void drawCopilotHeader(AppState& s, bool busy) {
    const float S = theme::S();
    const theme::AdaptiveSurface& surf = agentSurf();
    ImGui::PushFont(theme::font(theme::Font::H2));
    inlineIconGlass("agent", 18.f * S, ImGui::ColorConvertFloat4ToU32(surf.textPrimary));
    ImGui::SameLine(0, 8.f * S);
    ImGui::TextColored(surf.textPrimary, "ShopCraft Copilot");
    ImGui::PopFont();

    const ImGuiStyle& style = ImGui::GetStyle();
    const float lh = ImGui::GetTextLineHeight();
    float sessW = ImGui::CalcTextSize("Sessions").x + style.FramePadding.x * 2.f;
    float gearW = lh;
    float cluster = sessW + style.ItemSpacing.x + gearW + style.ItemSpacing.x + lh;
    ImGui::SameLine();
    float xRight = ImGui::GetContentRegionMax().x - cluster;
    if (xRight > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(xRight);

    if (busy) ImGui::BeginDisabled();
    if (ImGui::SmallButton("Sessions")) gShowSessions = !gShowSessions;
    if (busy) ImGui::EndDisabled();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Show or hide the session list.");

    ImGui::SameLine();
    {
        ui::IconUV gear = ui::getIcon("settings", 20);
        float gw = lh;
        bool openSettings = false;
        if (gear.valid)
            openSettings = ImGui::ImageButton("##aiheadergear", gear.texId,
                                              ImVec2(gw - 4.f * S, gw - 4.f * S),
                                              gear.uv0, gear.uv1);
        else
            openSettings = ImGui::SmallButton("set");
        if (openSettings) {
            settingsUi().openSettings = true;
            settingsUi().openSettingsTab = 1;  // T-G22: tab 1 = "AI Models & Routing" (was tab 2 before tab rename)
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("AI settings");
    }

    ImGui::SameLine();
    ImU32 dot = TCOL(accentMid);
    const char* tip = "Idle";
    if (busy) { dot = TCOL(accent); tip = "Running"; }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 cp = ImGui::GetCursorScreenPos();
    dl->AddCircleFilled(ImVec2(cp.x + lh * 0.5f, cp.y + lh * 0.5f), lh * 0.30f, dot);
    ImGui::Dummy(ImVec2(lh, lh));
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", tip);
}

// drawModelDropdown — delegates to the shared model_picker component (T-G22).
// Preset lists and search chrome live in ui/model_picker.{h,cpp}; single source of
// truth is model_registry.h.  This function is called inside a BeginPopup/EndPopup.
static void drawModelDropdown(bool busy) {
    (void)busy;
    AppSettings& cfg = appSettings();
    ModelPickerSelection sel;
    sel.provider = cfg.provider;
    sel.modelId  = cfg.model;
    drawModelPicker(cfg.offlineMode, sel,
                    [&](int prov, const std::string& id,
                        const std::string& base, bool vision) {
                        cfg.provider = prov;
                        std::strncpy(cfg.model, id.c_str(), sizeof(cfg.model) - 1);
                        if (!base.empty())
                            std::strncpy(cfg.baseUrl, base.c_str(), sizeof(cfg.baseUrl) - 1);
                        cfg.vision = vision;
                        saveAppSettings(cfg);
                    },
                    /*closeOnPick=*/true);
}

static void drawComposerPrelude(AppState& s) {
    const float S = theme::S();
    const theme::AdaptiveSurface& surf = agentSurf();
    const bool hasDoc = s.docMirror.hasDoc();
    ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float rowH = ImGui::GetFrameHeight() + 4.f * S;
    ImVec2 rowMax(rowMin.x + ImGui::GetContentRegionAvail().x, rowMin.y + rowH);
    theme::glassRowBackground(ImGui::GetWindowDrawList(), rowMin, rowMax, 4.f * S, false, false);

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8.f * S, 3.f * S));
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, theme::roleVec(theme::pal().hover, 0.55f));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, theme::roleVec(theme::pal().active, 0.60f));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    bool openMode = ImGui::SmallButton(kModes[gAgentMode]);
    ImGui::PopStyleColor(4);
    if (openMode) ImGui::OpenPopup("##cmpmode");
    if (ImGui::BeginPopup("##cmpmode")) {
        if (ImGui::Selectable("Design  — model a new part from a description", gAgentMode == 0))
            gAgentMode = 0;
        if (!hasDoc) ImGui::BeginDisabled();
        if (ImGui::Selectable("Edit  — change the open part", gAgentMode == 1)) gAgentMode = 1;
        if (!hasDoc) ImGui::EndDisabled();
        if (ImGui::Selectable("Ask  — questions about the part, no changes", gAgentMode == 2))
            gAgentMode = 2;
        ImGui::EndPopup();
    }
    ImGui::SameLine();
    AppSettings& cfg = appSettings();
    const char* permLabel = cfg.offlineMode ? "Permissions: Offline" : "Permissions";
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, theme::roleVec(theme::pal().hover, 0.55f));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, theme::roleVec(theme::pal().active, 0.60f));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    bool openPerm = ImGui::SmallButton(permLabel);
    ImGui::PopStyleColor(4);
    // T-G22: shot trigger — open permissions popup for screenshot capture
    if (settingsUi().openPermissionsPopup) {
        settingsUi().openPermissionsPopup = false;
        ImGui::OpenPopup("##cmpperm");
    }
    if (openPerm) ImGui::OpenPopup("##cmpperm");
    if (ImGui::BeginPopup("##cmpperm")) {
        bool off = cfg.offlineMode;
        if (ImGui::Checkbox("Offline mode (local models only — ITAR-safe)", &off)) {
            cfg.offlineMode = off;
            saveAppSettings(cfg);
        }
        bool sup = cfg.planMode;
        if (ImGui::Checkbox("Supervised (plan + clarify before modeling)", &sup)) {
            cfg.planMode = sup;
            saveAppSettings(cfg);
        }
        ImGui::Checkbox("Show assistant thoughts", &gShowThinking);
        ImGui::EndPopup();
    }
    int used = gSessions.empty() ? 0 : gSessions[gActiveSession].contextUsed;
    const float room = ImGui::GetContentRegionAvail().x;
    const float w = measureContextMeter(used, effectiveContextWindow(), room);
    const float x = ImGui::GetContentRegionMax().x - w;
    if (x > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(x);
    drawContextMeter(used, effectiveContextWindow(), room);
    ImGui::PopStyleVar();
    ImGui::Dummy(ImVec2(0, 4.f * S));
}

static void drawComposerToolbar(AppState& s, bool busy, bool canStart, bool active,
                                bool& submit, bool& stopReq) {
    const float S = theme::S();
    const theme::AdaptiveSurface& surf = agentSurf();
    ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float rowH = ImGui::GetFrameHeight() + 4.f * S;
    ImVec2 rowMax(rowMin.x + ImGui::GetContentRegionAvail().x, rowMin.y + rowH);
    theme::glassRowBackground(ImGui::GetWindowDrawList(), rowMin, rowMax, 4.f * S, false, false);

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8.f * S, 4.f * S));
    // Translucent chips over the glass composer (opaque theme fills fought the adaptive text).
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, theme::roleVec(theme::pal().hover, 0.55f));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, theme::roleVec(theme::pal().active, 0.60f));

    char modelLabel[160];
    composerModelLabel(modelLabel, sizeof(modelLabel));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    bool openModelDd = ImGui::Button(modelLabel);
    ImGui::PopStyleColor();
    // T-G22: shot trigger — open model dropdown for screenshot capture
    if (settingsUi().openComposerModelDropdown) {
        settingsUi().openComposerModelDropdown = false;
        ImGui::OpenPopup("##modeldropdown");
    }
    if (openModelDd) ImGui::OpenPopup("##modeldropdown");
    ImGui::SameLine();

    AppSettings& cfg = appSettings();
    const char* effLabels[8] = {};
    const char* effValues[8] = {};
    int nEff = reasoningEffortOptions(cfg.provider, cfg.model, effLabels, effValues, 8);
    if (nEff > 0) {
        int effIdx = 0;
        for (int i = 0; i < nEff; ++i)
            if (std::strcmp(cfg.reasoningEffort, effValues[i]) == 0) effIdx = i;
        char effBtn[48];
        std::snprintf(effBtn, sizeof(effBtn), "Thinking: %s", effLabels[effIdx]);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        bool openEff = ImGui::Button(effBtn);
        ImGui::PopStyleColor();
        if (openEff) ImGui::OpenPopup("##cmpeffort");
        if (ImGui::BeginPopup("##cmpeffort")) {
            for (int i = 0; i < nEff; ++i) {
                if (ImGui::Selectable(effLabels[i], effIdx == i)) {
                    std::strncpy(cfg.reasoningEffort, effValues[i], sizeof(cfg.reasoningEffort) - 1);
                    saveAppSettings(cfg);
                }
            }
            ImGui::EndPopup();
        }
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::BeginDisabled();
        ImGui::Button("Thinking: n/a");
        ImGui::EndDisabled();
        ImGui::PopStyleColor();
    }

    const float sendSz = ImGui::GetFrameHeight();
    ImGui::SameLine();
    float rightX = ImGui::GetContentRegionMax().x - sendSz;
    if (rightX > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(rightX);

    if (active) {
        ImGui::PushStyleColor(ImGuiCol_Button, TVEC(bad));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, TVEC(bad));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, TVEC(bad));
        if (ImGui::Button("##stop", ImVec2(sendSz, sendSz))) stopReq = true;
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
        ImVec2 c((mn.x + mx.x) * 0.5f, (mn.y + mx.y) * 0.5f);
        float h = sendSz * 0.18f;
        dl->AddRectFilled(ImVec2(c.x - h, c.y - h), ImVec2(c.x + h, c.y + h),
                          theme::roleU32(theme::pal().txt0, 0.92f), 1.5f * S);
    } else {
        ImGui::PushStyleColor(ImGuiCol_Button, canStart ? TVEC(accent) : TVEC(bg2));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, canStart ? TVEC(accentDim) : TVEC(bg3));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, TVEC(accentDim));
        if (ImGui::Button("##send", ImVec2(sendSz, sendSz)) && canStart) submit = true;
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
        ImVec2 c((mn.x + mx.x) * 0.5f, (mn.y + mx.y) * 0.5f);
        float t = sendSz * 0.22f;
        ImU32 ac = canStart ? theme::roleU32(theme::pal().bg0, 0.95f) : TCOL(txt2);
        dl->AddTriangleFilled(ImVec2(c.x, c.y - t), ImVec2(c.x - t, c.y + t * 0.55f),
                              ImVec2(c.x + t, c.y + t * 0.55f), ac);
    }
    ImGui::PopStyleColor(3);
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar();

    ImGui::SetNextWindowSizeConstraints(ImVec2(300.f * S, 0), ImVec2(300.f * S, FLT_MAX));
    if (ImGui::BeginPopup("##modeldropdown")) { drawModelDropdown(busy); ImGui::EndPopup(); }
}

static void requestStopRun(AppState& s) {
    s.agent.running = false;
    s.agent.demoStep = -1;
    for (auto it = s.agent.blocks.rbegin(); it != s.agent.blocks.rend(); ++it) {
        if (it->kind == BlockKind::Action && it->status == ActionStatus::Running) {
            it->status = ActionStatus::Failed;
            it->result = "Stopped by user.";
            break;
        }
    }
    appendConsoleLog("[..] stop requested by user");
    s.status = "Stopped.";
}

static void submitTurn(AppState& s, const std::string& text) {
    if (text.empty()) return;
    appendConsoleLog("[..] copilot: " + (text.size() > 64 ? text.substr(0, 63) + "…" : text));
    activityPanel().clearLive();
    activityPanel().onEvent("run_started", nlohmann::json{
        {"run_id", "ui"}, {"request", text},
        {"doc_id", s.docMirror.docId()}, {"stages", nlohmann::json::array({"intake", "model"})}});
    s.startDemoAgent(text);
    gSessions[gActiveSession].contextUsed = 1200 + (int)s.agent.blocks.size() * 400;
    gSessions[gActiveSession].running = true;
}

static void drawChatBody(AppState& s, bool busy, bool canRun) {
    const float S = theme::S();
    AgentThread& a = s.agent;
    ensureSessions();
    syncChatToDoc(s);

    static float sComposerH = 0.f;
    const float bottomClearance = theme::U(1.25f);
    float reserve = (sComposerH > 0 ? sComposerH : 110.f * S) + 8.f * S + bottomClearance;
    float chatH = ImGui::GetContentRegionAvail().y - reserve;
    if (chatH < 120.f * S) chatH = 120.f * S;

    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##transcript", ImVec2(0, chatH), false);
    const float logW = ImGui::GetContentRegionAvail().x;
    if (gShowSessions) drawSessionsList(s, busy);
    else for (int i = 0; i < (int)a.blocks.size(); ++i) drawBlock(s, a.blocks[i], i, logW);
    if (!gShowSessions && ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 40.f * S)
        ImGui::SetScrollHereY(1.f);
    ImGui::EndChild();
    ImGui::PopStyleColor();

    if (!gSessions[gActiveSession].queuedPrompts.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, agentSurf().textSecondary);
        for (size_t qi = 0; qi < gSessions[gActiveSession].queuedPrompts.size(); ++qi) {
            ImGui::PushID((int)qi + 9000);
            const std::string& q = gSessions[gActiveSession].queuedPrompts[qi];
            std::string label = "Queued: " + (q.size() > 48 ? q.substr(0, 47) + "\xe2\x80\xa6" : q);
            ImGui::TextUnformatted(label.c_str());
            ImGui::SameLine(ImGui::GetContentRegionMax().x - ImGui::GetFrameHeight());
            if (ImGui::SmallButton("x"))
                gSessions[gActiveSession].queuedPrompts.erase(
                    gSessions[gActiveSession].queuedPrompts.begin() + (int)qi);
            ImGui::PopID();
        }
        ImGui::PopStyleColor();
    }

    ImGui::BeginGroup();
    static char buf[4096] = {};
    drawComposerPrelude(s);

    const char* placeholder = gAgentMode == 2 ? "Ask about this part\xe2\x80\xa6"
                            : gAgentMode == 1 ? "Describe a change to this part\xe2\x80\xa6"
                                              : "Describe what to build\xe2\x80\xa6";
    ImGui::PushStyleColor(ImGuiCol_FrameBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_Border, TVEC(line));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 1.f);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 8.f * S);
    ImGui::InputTextMultiline("##in", buf, sizeof(buf), ImVec2(-FLT_MIN, 56.f * S),
                              ImGuiInputTextFlags_EnterReturnsTrue);
    bool enterSend = ImGui::IsItemDeactivatedAfterEdit() && buf[0] && !ImGui::GetIO().KeyShift;
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(2);
    if (buf[0] == 0 && ImGui::IsItemHovered())
        ImGui::SetTooltip("%s", placeholder);
    ImGui::Dummy(ImVec2(0, 4.f * S));

    const bool active = aiTurnActive(s);
    bool submit = false;
    bool stopReq = false;
    bool canStart = canRun && buf[0] != 0 && !active;
    drawComposerToolbar(s, busy, canStart, active, submit, stopReq);

    if (stopReq) requestStopRun(s);
    if ((submit || enterSend) && buf[0]) {
        if (active) {
            gSessions[gActiveSession].queuedPrompts.emplace_back(buf);
            buf[0] = 0;
        } else if (canStart) {
            submitTurn(s, buf);
            buf[0] = 0;
        }
    }
    if (!active && !gSessions[gActiveSession].queuedPrompts.empty() && canRun) {
        std::string next = gSessions[gActiveSession].queuedPrompts.front();
        gSessions[gActiveSession].queuedPrompts.erase(
            gSessions[gActiveSession].queuedPrompts.begin());
        submitTurn(s, next);
    }

    ImGui::EndGroup();
    sComposerH = ImGui::GetItemRectSize().y;
    ImGui::Dummy(ImVec2(0, bottomClearance));
    syncBlocksToSession(s);
    if (!a.running) gSessions[gActiveSession].running = false;
}

}  // namespace

void drawAgentPanel(AppState& s) {
    const float S = theme::S();
    s.tryStartWorker();
    const bool busy = s.job.running.load() || s.agent.running;
    const bool canRun = s.engineReady;

    // ONE UltraThin glass surface for the whole Copilot pane, painted once here at panel top
    // (contract §6/A1/A10: id "agent_glass", window-independent via theme::panelId — the
    // Console/Activity tab bodies and every bubble below fetch this SAME cached surface).
    ImVec2 wmin = ImGui::GetWindowPos();
    ImVec2 wmax(wmin.x + ImGui::GetWindowSize().x, wmin.y + ImGui::GetWindowSize().y);
    theme::glassPanel(ImGui::GetWindowDrawList(), ImRect(wmin, wmax),
                      theme::MaterialLevel::UltraThin, -1.f, agentSurf());

    drawCopilotHeader(s, busy);
    ImGui::Separator();

    if (ImGui::BeginTabBar("##agenttabs")) {
        if (ImGui::BeginTabItem("Chat")) { gAgentTab = (int)AgentTab::Chat; ImGui::EndTabItem(); }
        if (ImGui::BeginTabItem("Console")) { gAgentTab = (int)AgentTab::Console; ImGui::EndTabItem(); }
        if (ImGui::BeginTabItem("Activity")) { gAgentTab = (int)AgentTab::Activity; ImGui::EndTabItem(); }
        ImGui::EndTabBar();
    }

    switch ((AgentTab)gAgentTab) {
    case AgentTab::Console:
        drawConsoleBody(s);
        break;
    case AgentTab::Activity:
        activityPanel().drawBody(s, &agentSurf());
        break;
    case AgentTab::Chat:
    default:
        drawChatBody(s, busy, canRun);
        break;
    }

    // Interaction contract §8 in-app-dialog transport: renders the pending ask_user
    // batch (if any) as a modal, independent of which Copilot tab is active.
    ui::drawAskDialog(s);

    (void)S;
}

}  // namespace ui
