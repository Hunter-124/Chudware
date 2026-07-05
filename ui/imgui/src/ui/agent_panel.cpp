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
    // Propagate real token count whenever the engine has reported one.
    if (s.agent.lastInputTokens > 0)
        gSessions[gActiveSession].contextUsed = s.agent.lastInputTokens;
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
    // Use registry-first lookup: prefers ModelInfo.contextLength, falls back to heuristic.
    return modelContextWindow(cfg.provider, cfg.model);
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
    ImGui::Dummy(ImVec2(0, 4.f * S));  // T-BL39: tighter user bubble bottom gap (was 6pt)
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
    const theme::AdaptiveSurface& surf = agentSurf();
    const float w = ImGui::GetContentRegionAvail().x;
    GlassBubble gb = beginGlassBubble("##asstGlass", w, theme::MaterialLevel::Thin, 8.f * S);
    ImGui::BeginChild("##asst", ImVec2(w, 0), ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    // T-BL39: real model attribution. modelName is stamped from the ai_artifact telemetry
    // event emitted by the engine loop (T-H1/T-BL22 envelope: ev["model"].provider + model).
    // Demo path labels itself "demo". Falls back to "AI" only when metadata is absent —
    // never "Assistant". surf.textSecondary is theme-adaptive (no black-on-black, T-BL33).
    // Format "Provider · slug" mirrors composerModelLabel() in the input row (T-BL38 parity).
    const std::string& attrName = b.modelName.empty() ? std::string("AI") : b.modelName;

    inlineIconGlass("agent", 14.f * S, ImGui::ColorConvertFloat4ToU32(surf.textPrimary));
    ImGui::SameLine(0, 6.f * S);
    ImGui::PushFont(theme::font(theme::Font::Label));
    ImGui::TextColored(surf.textSecondary, "%s", attrName.c_str());
    ImGui::PopFont();

    // Compact: body immediately after attribution line, no extra spacer between them.
    ImGui::PushTextWrapPos(0);
    ImGui::TextColored(surf.textPrimary, "%s", b.body.c_str());
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

// Unit tables for dimension questions (mirrors ask_dialog.cpp).
static constexpr const char* kLengthUnits[] = {"mm", "cm", "m", "in", "ft"};
static constexpr const char* kAngleUnits[]  = {"deg"};
static constexpr const char* kCountUnits[]  = {"count"};
static void unitsForFamily(const std::string& family,
                            const char* const*& units, int& count) {
    if (family == "angle")      { units = kAngleUnits; count = 1; }
    else if (family == "count") { units = kCountUnits; count = 1; }
    else                        { units = kLengthUnits; count = 5; }
}

// Seed InlineFieldState from the field's typed `suggested` JSON (contract §2).
static void initInlineField(InlineFieldState& fs,
                             const aicad::ui::AskField& f) {
    using aicad::ui::AskFieldKind;
    nlohmann::json sug = nlohmann::json::parse(f.suggestedJson, nullptr, false);
    fs = InlineFieldState{};
    switch (f.kind) {
    case AskFieldKind::Dimension: {
        if (!sug.is_discarded() && sug.is_object()) {
            auto vit = sug.find("value");
            if (vit != sug.end() && vit->is_number()) fs.dimValue = vit->get<double>();
            std::string unit = sug.value("unit", std::string());
            const char* const* units; int n;
            unitsForFamily(f.family, units, n);
            for (int i = 0; i < n; ++i) if (unit == units[i]) { fs.dimUnitIdx = i; break; }
        }
        break;
    }
    case AskFieldKind::Choice: {
        fs.choiceChecked.assign(f.options.size(), 0);
        if (!sug.is_discarded()) {
            if (!f.multi) {
                std::string sv = sug.is_string() ? sug.get<std::string>() : std::string();
                for (size_t i = 0; i < f.options.size(); ++i)
                    if (f.options[i] == sv) { fs.choiceIdx = (int)i; break; }
            } else {
                // multi-select: suggested is an array or a single value
                auto mark = [&](const std::string& v) {
                    for (size_t i = 0; i < f.options.size(); ++i)
                        if (f.options[i] == v) fs.choiceChecked[i] = 1;
                };
                if (sug.is_array()) {
                    for (const auto& sv : sug) if (sv.is_string()) mark(sv.get<std::string>());
                } else if (sug.is_string()) {
                    mark(sug.get<std::string>());
                }
            }
        }
        break;
    }
    case AskFieldKind::Text:
    case AskFieldKind::FileRequest: {
        std::string sv = (!sug.is_discarded() && sug.is_string())
                          ? sug.get<std::string>() : std::string();
        fs.textBuf = sv;
        break;
    }
    case AskFieldKind::Approval:
        fs.approved = (!sug.is_discarded() && sug.is_boolean()) && sug.get<bool>();
        break;
    }
}

// Collect raw answer strings from InlineFieldState → map for buildAnswersJson.
static std::map<std::string, std::string>
collectInlineAnswers(const aicad::ui::AskForm& form,
                     const std::vector<InlineFieldState>& states) {
    using aicad::ui::AskFieldKind;
    std::map<std::string, std::string> raw;
    for (size_t i = 0; i < form.fields.size() && i < states.size(); ++i) {
        const auto& f  = form.fields[i];
        const auto& fs = states[i];
        switch (f.kind) {
        case AskFieldKind::Dimension: {
            const char* const* units; int n;
            unitsForFamily(f.family, units, n);
            char buf[96];
            std::snprintf(buf, sizeof(buf), "%g %s", fs.dimValue,
                          (fs.dimUnitIdx >= 0 && fs.dimUnitIdx < n) ? units[fs.dimUnitIdx] : "");
            raw[f.id] = buf;
            break;
        }
        case AskFieldKind::Choice:
            if (f.multi) {
                // Build JSON array string for buildAnswersJson.
                nlohmann::json arr = nlohmann::json::array();
                for (size_t j = 0; j < f.options.size() && j < fs.choiceChecked.size(); ++j)
                    if (fs.choiceChecked[j]) arr.push_back(f.options[j]);
                raw[f.id] = arr.dump();
            } else {
                raw[f.id] = (fs.choiceIdx >= 0 && fs.choiceIdx < (int)f.options.size())
                                ? f.options[fs.choiceIdx] : std::string();
            }
            break;
        case AskFieldKind::Approval:
            raw[f.id] = fs.approved ? "true" : "false";
            break;
        case AskFieldKind::Text:
        case AskFieldKind::FileRequest:
        default:
            raw[f.id] = fs.textBuf;
            break;
        }
    }
    return raw;
}

// Inline question card — renders an ask_user batch as an interactive card inside the
// Copilot transcript. The card stays editable until the user presses Submit; after that
// it shows a read-only answered summary. Returns true on the frame Submit is pressed.
// Writes the §4 answer envelope into b.askAnswersJson and sets b.askAnswered.
// Also mirrors into s.askAnswersJson so the agent-event pump can pick it up.
static bool drawQuestionCard(AppState& s, AgentBlock& b) {
    using aicad::ui::AskFieldKind;
    const float S = theme::S();
    const float w = ImGui::GetContentRegionAvail().x;

    GlassBubble gb = beginGlassBubble("##qGlass", w, theme::MaterialLevel::Thin, 8.f * S);
    ImGui::BeginChild(ImGui::GetID(&b), ImVec2(w, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    // Header row: question icon + "Agent question"
    inlineIconGlass("agent", 16.f * S, TCOL(accent));
    ImGui::SameLine(0, 8.f * S);
    ImGui::PushFont(theme::font(theme::Font::Label));
    ImGui::TextColored(ImGui::ColorConvertU32ToFloat4(TCOL(accent)), "Agent question");
    ImGui::PopFont();

    bool submitted = false;

    // Seed widget state from suggested defaults on first draw.
    if (b.askFields.size() != b.askForm.fields.size())
        b.askFields.resize(b.askForm.fields.size());
    for (size_t i = 0; i < b.askForm.fields.size(); ++i) {
        if (!b.askFields[i].seeded) {
            initInlineField(b.askFields[i], b.askForm.fields[i]);
            b.askFields[i].seeded = true;
        }
    }

    if (b.askAnswered) {
        // Read-only summary after submission.
        ImGui::SameLine(0, 12.f * S);
        ImGui::TextColored(agentSurf().textSecondary, "(answered)");
    } else {
        // Editable fields.
        ImGui::Spacing();
        for (size_t i = 0; i < b.askForm.fields.size() && i < b.askFields.size(); ++i) {
            const auto& f  = b.askForm.fields[i];
            InlineFieldState& fs = b.askFields[i];

            ImGui::PushID((int)i);
            ImGui::PushTextWrapPos(0);
            ImGui::TextColored(agentSurf().textPrimary, "%s", f.prompt.c_str());
            ImGui::PopTextWrapPos();
            if (!f.why.empty()) {
                ImGui::SameLine();
                ImGui::TextDisabled("(?)");
                if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", f.why.c_str());
            }

            switch (f.kind) {
            case AskFieldKind::Dimension: {
                const char* const* units; int n;
                unitsForFamily(f.family, units, n);
                ImGui::SetNextItemWidth(160.f * S);
                ImGui::InputDouble("##val", &fs.dimValue, 0.0, 0.0, "%.4g");
                ImGui::SameLine();
                ImGui::SetNextItemWidth(90.f * S);
                ImGui::Combo("##unit", &fs.dimUnitIdx, units, n);
                break;
            }
            case AskFieldKind::Choice: {
                if (!f.multi) {
                    // Single-select: radio buttons for <=6 opts, combo otherwise.
                    if (f.options.size() <= 6) {
                        for (int oi = 0; oi < (int)f.options.size(); ++oi) {
                            ImGui::PushID(oi);
                            ImGui::RadioButton(f.options[oi].c_str(), &fs.choiceIdx, oi);
                            ImGui::PopID();
                        }
                    } else {
                        std::vector<const char*> opts;
                        opts.reserve(f.options.size());
                        for (const auto& o : f.options) opts.push_back(o.c_str());
                        ImGui::SetNextItemWidth(-1.f);
                        ImGui::Combo("##choice", &fs.choiceIdx, opts.data(), (int)opts.size());
                    }
                } else {
                    // Multi-select: checkboxes.
                    if (fs.choiceChecked.size() != f.options.size())
                        fs.choiceChecked.assign(f.options.size(), 0);
                    for (size_t oi = 0; oi < f.options.size(); ++oi) {
                        ImGui::PushID((int)oi);
                        bool checked = fs.choiceChecked[oi] != 0;
                        if (ImGui::Checkbox(f.options[oi].c_str(), &checked))
                            fs.choiceChecked[oi] = checked ? 1 : 0;
                        ImGui::PopID();
                    }
                }
                break;
            }
            case AskFieldKind::Text: {
                // Reserve a mutable buffer. TextBuf is std::string — expose via InputText.
                fs.textBuf.resize(std::max(fs.textBuf.size(), (size_t)1));
                ImGui::SetNextItemWidth(-1.f);
                if (f.multiline) {
                    // Open-ended: multiline field.
                    fs.textBuf.resize(std::max(fs.textBuf.size(), (size_t)512));
                    ImGui::InputTextMultiline("##text", fs.textBuf.data(), fs.textBuf.capacity(),
                                             ImVec2(-1.f, 80.f * S));
                    // Sync capacity used → actual size (InputText writes a null terminator).
                    fs.textBuf.resize(std::strlen(fs.textBuf.data()));
                } else {
                    // Short answer: single-line field.
                    fs.textBuf.resize(std::max(fs.textBuf.size(), (size_t)256));
                    ImGui::InputText("##text", fs.textBuf.data(), fs.textBuf.capacity());
                    fs.textBuf.resize(std::strlen(fs.textBuf.data()));
                }
                break;
            }
            case AskFieldKind::FileRequest:
                fs.textBuf.resize(std::max(fs.textBuf.size(), (size_t)512));
                ImGui::SetNextItemWidth(-1.f);
                ImGui::InputTextWithHint("##path", "file path or paste", fs.textBuf.data(),
                                         fs.textBuf.capacity());
                fs.textBuf.resize(std::strlen(fs.textBuf.data()));
                break;
            case AskFieldKind::Approval:
                ImGui::Checkbox("Approve", &fs.approved);
                break;
            }
            ImGui::Spacing();
            ImGui::PopID();
        }

        ImGui::Separator();
        if (ImGui::Button("Submit")) {
            auto raw = collectInlineAnswers(b.askForm, b.askFields);
            b.askAnswersJson = aicad::ui::buildAnswersJson(b.askForm, raw);
            b.askAnswered = true;
            // Mirror to AppState so the event pump can pick it up.
            s.askAnswersJson = b.askAnswersJson;
            submitted = true;
        }
        ImGui::SameLine();
        if (ImGui::Button("Accept defaults")) {
            // Pre-fill with suggested defaults (already seeded at init); just submit.
            auto raw = collectInlineAnswers(b.askForm, b.askFields);
            b.askAnswersJson = aicad::ui::buildAnswersJson(b.askForm, raw);
            b.askAnswered = true;
            s.askAnswersJson = b.askAnswersJson;
            submitted = true;
        }
    }

    ImGui::EndChild();
    endGlassBubble(gb);
    return submitted;
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
    case BlockKind::Question:
        drawQuestionCard(s, b);
        break;
    default:
        ImGui::TextColored(agentSurf().textSecondary, "%s", b.body.c_str());
        break;
    }
    ImGui::PopStyleVar();
    ImGui::Dummy(ImVec2(0, 4.f * S));  // T-BL39: tighter inter-block rhythm (was 7pt)
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
    // Context meter lives in the toolbar row (between model picker and send). Nothing more here.
    ImGui::PopStyleVar();
    ImGui::Dummy(ImVec2(0, 4.f * S));
}

// Draw the context bar as a button: shows "12k / 200k" usage, hover/press animations match the
// rest of the toolbar row. Clicking opens a breakdown popup limited to categories the session
// genuinely tracks (total input tokens from the engine; fabricated categories are NOT shown).
static void drawContextBarButton(AppState& s) {
    const float S = theme::S();
    const theme::AdaptiveSurface& surf = agentSurf();
    int used = gSessions.empty() ? 0 : gSessions[gActiveSession].contextUsed;
    int win  = effectiveContextWindow();

    // Build label: "12k / 200k" preferred, "ctx 12k/200k" when space is tight.
    std::string usedStr = fmtTokens(used);
    std::string winStr  = fmtTokens(win);
    char label[64];
    std::snprintf(label, sizeof(label), "%s / %s", usedStr.c_str(), winStr.c_str());

    // Colour the text by fill fraction (good / warn / bad) for quick visual scan.
    const float frac = win > 0 ? std::min(1.f, (float)used / (float)win) : 0.f;
    ImU32 barFg = frac < 0.5f ? TCOL(good) : frac < 0.85f ? TCOL(warn) : TCOL(bad);
    (void)barFg;  // used below in the mini progress bar

    // Translucent button matching toolbar style.
    ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,  theme::roleVec(theme::pal().hover, 0.55f));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,   theme::roleVec(theme::pal().active, 0.60f));
    ImGui::PushStyleColor(ImGuiCol_Text,           surf.textSecondary);

    bool clicked = ImGui::Button(label);

    // Draw a slim progress bar underline inside the button rect, after the button is placed.
    {
        ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
        const float barH = 2.f * S;
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImU32 track = TCOL(accentSoft);
        dl->AddRectFilled(ImVec2(mn.x, mx.y - barH), mx, track, 0.f);
        if (frac > 0.f)
            dl->AddRectFilled(ImVec2(mn.x, mx.y - barH),
                              ImVec2(mn.x + (mx.x - mn.x) * frac, mx.y), barFg, 0.f);
    }

    ImGui::PopStyleColor(4);

    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Context usage — click for breakdown");

    if (clicked) ImGui::OpenPopup("##ctxbreakdown");

    // Breakdown popup — shows only what the session genuinely tracks.
    ImGui::SetNextWindowSizeConstraints(ImVec2(260.f * S, 0), ImVec2(380.f * S, FLT_MAX));
    if (ImGui::BeginPopup("##ctxbreakdown")) {
        ImGui::PushFont(theme::font(theme::Font::H2));
        ImGui::TextColored(surf.textPrimary, "Context breakdown");
        ImGui::PopFont();
        ImGui::Separator();

        // Only show what the session actually tracks.
        if (used > 0) {
            // The engine reports a total input-token count per turn (input_tokens from the
            // agent result JSON). That is the only per-category data we receive — show it
            // as "Conversation (last turn)" rather than guessing sub-categories.
            char buf[80];
            std::snprintf(buf, sizeof(buf), "%s tok", fmtTokens(used).c_str());
            ImGui::TextColored(surf.textSecondary, "Conversation (last turn input)");
            ImGui::SameLine();
            ImGui::TextColored(surf.textPrimary, "%s", buf);
        } else {
            ImGui::TextColored(surf.textSecondary,
                "(No turn completed yet — send a message to see usage)");
        }
        ImGui::Separator();
        ImGui::TextColored(surf.textSecondary, "Model context window");
        ImGui::SameLine();
        ImGui::TextColored(surf.textPrimary, "%s tok", fmtTokens(win).c_str());

        // Honest note: per-category breakdown (system prompt, tool defs, etc.) requires the
        // engine to expose per-block token counts, which it does not currently report.
        ImGui::Dummy(ImVec2(0, 4.f * S));
        ImGui::PushFont(theme::font(theme::Font::Label));
        ImGui::TextColored(surf.textSecondary,
            "Per-category breakdown (system prompt, tool defs,\n"
            "history, current input) requires engine support\n"
            "that is not yet wired.");
        ImGui::PopFont();
        ImGui::EndPopup();
    }
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

    // ---- Model picker — constrained to max 160p so context bar is never overlapped ----
    char modelLabel[160];
    composerModelLabel(modelLabel, sizeof(modelLabel));
    const float kPickerMaxW = 160.f * S;
    // Measure the label; if wider than max, it is ellipsized inside a fixed-width button.
    const float labelW = ImGui::CalcTextSize(modelLabel).x + 16.f * S;  // 16 = 2× frame padding
    const float pickerW = std::min(labelW, kPickerMaxW);
    // Ellipsize label if it won't fit.
    char pickerLabel[160];
    if (labelW > kPickerMaxW) {
        // Truncate to fit; append "…" (UTF-8 ellipsis).
        // Binary search would be ideal; a simple byte-chop is safe for ASCII + known UTF-8 here.
        std::strncpy(pickerLabel, modelLabel, sizeof(pickerLabel) - 1);
        pickerLabel[sizeof(pickerLabel) - 1] = '\0';
        float tw = ImGui::CalcTextSize(pickerLabel).x + 16.f * S;
        const char* ellipsis = "\xe2\x80\xa6";  // U+2026 …
        const float ellW = ImGui::CalcTextSize(ellipsis).x;
        int len = (int)std::strlen(pickerLabel);
        while (len > 0 && tw + ellW > kPickerMaxW) {
            pickerLabel[--len] = '\0';
            tw = ImGui::CalcTextSize(pickerLabel).x + 16.f * S;
        }
        std::strncat(pickerLabel, ellipsis, sizeof(pickerLabel) - len - 1);
    } else {
        std::strncpy(pickerLabel, modelLabel, sizeof(pickerLabel) - 1);
        pickerLabel[sizeof(pickerLabel) - 1] = '\0';
    }
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    bool openModelDd = ImGui::Button(pickerLabel, ImVec2(pickerW, 0));
    ImGui::PopStyleColor();
    // T-G22: shot trigger — open model dropdown for screenshot capture
    if (settingsUi().openComposerModelDropdown) {
        settingsUi().openComposerModelDropdown = false;
        ImGui::OpenPopup("##modeldropdown");
    }
    if (ImGui::IsItemHovered() && labelW > kPickerMaxW)
        ImGui::SetTooltip("%s", modelLabel);  // full name on hover when ellipsized
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

    // ---- Context bar — sits between model picker row and send button ----
    const float sendSz = ImGui::GetFrameHeight();
    // Reserve space for [context bar] [send] at the right edge.
    int used = gSessions.empty() ? 0 : gSessions[gActiveSession].contextUsed;
    int win  = effectiveContextWindow();
    std::string usedStr = fmtTokens(used);
    std::string winStr  = fmtTokens(win);
    char ctxLabel[64];
    std::snprintf(ctxLabel, sizeof(ctxLabel), "%s / %s", usedStr.c_str(), winStr.c_str());
    const float ctxLabelW = ImGui::CalcTextSize(ctxLabel).x + 16.f * S;
    const float ctxGap    = 6.f * S;
    const float ctxRight  = ImGui::GetContentRegionMax().x - sendSz - ctxGap - ctxLabelW - ctxGap;
    ImGui::SameLine();
    if (ctxRight > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(ctxRight);
    drawContextBarButton(s);
    ImGui::SameLine(0, ctxGap);

    // ---- Send / Stop button ----
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
    // contextUsed is updated from real input_tokens once the turn completes (see tickDemoAgent).
    // While the turn is in-flight the previous value is kept — do not fabricate a number.
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
    if (gShowSessions) {
        drawSessionsList(s, busy);
    } else if (a.blocks.empty()) {
        // T-BL39: empty state — no greeting card. Minimal centered hint so the panel
        // doesn't look broken; disappears as soon as the first message arrives.
        const theme::AdaptiveSurface& surf = agentSurf();
        ImGui::Dummy(ImVec2(0, chatH * 0.35f));
        ImGui::PushFont(theme::font(theme::Font::Label));
        const char* hint = "Type below to start a conversation.";
        float hw = ImGui::CalcTextSize(hint).x;
        float avail = ImGui::GetContentRegionAvail().x;
        if (avail > hw) ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (avail - hw) * 0.5f);
        ImGui::TextColored(surf.textSecondary, "%s", hint);
        ImGui::PopFont();
    } else {
        for (int i = 0; i < (int)a.blocks.size(); ++i) drawBlock(s, a.blocks[i], i, logW);
    }
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
