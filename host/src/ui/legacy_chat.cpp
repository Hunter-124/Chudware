// AI-CAD Copilot pane.
//
// One clean vertical pane: a slim header (logo + status + chat menu), the conversation log with a
// per-turn transparent activity timeline (what the AI is doing / thinking, expandable), and a
// bottom composer (rounded input + inline model/settings/send toolbar + provider/mode/autopilot
// chips). Settings live in a popup off the composer; there is no separate Activity panel anymore.
// Bubbles/cards (user prompt, prose, thinking, action, plan, error) and the composer are painted
// as translucent Liquid-Glass bubbles over the live viewport backdrop (host contract H5; see
// chatSurf()/GlassBubble below) instead of the old near-opaque pushGlassChildStyle cards.
//
// Also home to the shared small-UI helpers (primaryButton/sectionHeader/dimText/toggleChip and
// the code/diff/prose/fit/plan/parts draw helpers) declared in core/app_state.h — they live here
// because the Copilot pane is their primary user, and are shared with the other panels via the
// header.
#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <set>
#include <string>
#include <unordered_map>

#include <nlohmann/json.hpp>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect — Liquid Glass v2 host port (contract H5: glass bubbles)

#include "legacy_chat.h"
#include "model_registry.h"  // ui::ModelInfo / capability flags / provider mapping
#include "metrics.h"          // ui::metrics::px/U/asmHeaderIconGap
#include "activity_panel.h"  // C22b: ActivityPanel still fed for the AI_CAD_SHOT=activity scenario
#include "console_panel.h"   // drawConsoleBody (shared console helper)
#include "icons.h"           // getIcon / IconUV — composer settings glyph

#include "../core/app_state.h"
#include "../core/cpp_core.h"  // detect the in-process C++ core (libaicad) backend
#include "../core/sim_client.h"  // chat-triggered OpenFOAM CFD on the SimClient channel
#include "../core/jobs.h"
#include "../ui_theme.h"

#include "../worker_client.h"

// ============================ shared small-UI helpers ============================
bool primaryButton(const char* label, const ImVec2& size) {
    const auto& p = ui::palette;
    // Primary button always uses the accent color regardless of theme.
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(p.accent[0], p.accent[1], p.accent[2], 1));
    // Hovered: lighten for dark theme, darken for light (just brighten accent channel).
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
        ImVec4(p.accent[0]*1.15f > 1.f ? 1.f : p.accent[0]*1.15f,
               p.accent[1]*1.08f > 1.f ? 1.f : p.accent[1]*1.08f,
               p.accent[2]*1.08f > 1.f ? 1.f : p.accent[2]*1.08f, 1));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,
        ImVec4(p.accentDim[0], p.accentDim[1], p.accentDim[2], 1));
    // Text on the accent button: dark for light-themed accent, dark for teal accent.
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.04f, 0.10f, 0.10f, 1));
    bool r = ImGui::Button(label, size);
    ImGui::PopStyleColor(4);
    return r;
}
void sectionHeader(ImFont* f, const char* text) {
    if (f) ImGui::PushFont(f);
    ImGui::TextUnformatted(text);
    if (f) ImGui::PopFont();
    ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
}
void dimText(const char* t) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                                                ui::palette.textDim[2], 1));
    ImGui::TextWrapped("%s", t);
    ImGui::PopStyleColor();
}

// A small pill button that highlights when its section is expanded.
bool toggleChip(const char* label, bool active) {
    if (active) ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(ui::palette.chipBg[0], ui::palette.chipBg[1],
               ui::palette.chipBg[2], ui::palette.chipBg[3]));
    bool clicked = ImGui::SmallButton(label);
    if (active) ImGui::PopStyleColor();
    return clicked;
}

// ---- auto-growing, wrapping chat input -------------------------------------------------------
// ImGui's InputTextMultiline does NOT word-wrap (it renders with wrap_width=0, so long lines
// extend past the box and get clipped — "run off the screen"). So we maintain the wrapping
// ourselves: rewrapInPlace toggles spaces<->newlines so every visual line fits within wrapW.
// It is SAME-LENGTH (only swaps ' ' <-> '\n'), so cursor indices in the active widget stay
// valid. The box then grows by counting the newlines. Enter submits; Shift+Enter inserts a
// soft break that the next rewrap reflows (a prompt rarely needs hard line breaks).
static bool rewrapInPlace(char* buf, int len, float wrapW) {
    if (len <= 0 || wrapW <= 1.0f) return false;
    bool changed = false;
    // 1) flatten every existing newline back to a space — an idempotent source to re-wrap from
    //    (so widening the box reflows the text instead of leaving stale breaks).
    for (int i = 0; i < len; ++i)
        if (buf[i] == '\n') { buf[i] = ' '; changed = true; }
    // 2) greedy word-wrap: break at the last space that keeps the running line within wrapW.
    int lineStart = 0, lastSpace = -1;
    float lineW = 0.0f;
    for (int i = 0; i < len; ++i) {
        float cw = ImGui::CalcTextSize(buf + i, buf + i + 1).x;
        if (lineW + cw > wrapW && lastSpace >= lineStart) {
            buf[lastSpace] = '\n';
            changed = true;
            lineStart = lastSpace + 1;
            lineW = ImGui::CalcTextSize(buf + lineStart, buf + i).x;
            lastSpace = -1;
        }
        lineW += cw;
        if (buf[i] == ' ') lastSpace = i;
    }
    return changed;
}
namespace {
struct ChatInputCb { float wrapW = 0.0f; };
// Panel-local transparency toggle: show/hide the AI's reasoning ("Thinking") blocks. Session-only
// (DESIGN §5 "Show thinking" switch, default on); not persisted and deliberately NOT in AppState.
bool gShowThinking = true;
int chatInputEditCallback(ImGuiInputTextCallbackData* data) {
    if (data->EventFlag == ImGuiInputTextFlags_CallbackEdit) {
        auto* cb = static_cast<ChatInputCb*>(data->UserData);
        if (rewrapInPlace(data->Buf, data->BufTextLen, cb->wrapW))
            data->BufDirty = true;   // same length, so BufTextLen/CursorPos are unchanged
    }
    return 0;
}
}  // namespace
static bool drawChatInput(const char* id, std::vector<char>& buf, const char* placeholder,
                          int maxLines) {
    if (buf.empty()) return false;
    const auto& pal = ui::palette;
    const ImGuiStyle& style = ImGui::GetStyle();
    const float lineH = ImGui::GetTextLineHeight();
    float wrapW = ImGui::GetContentRegionAvail().x - style.FramePadding.x * 2.0f -
                  style.ScrollbarSize;
    if (wrapW < 1.0f) wrapW = 1.0f;

    // When the widget is NOT active it is safe to rewrap the backing buffer directly — this
    // covers programmatically-filled text (example chips) and panel resize. While active, the
    // edit callback rewraps ImGui's internal copy instead (external edits would be ignored).
    // We can't read ImGui's active id (internal API), so track per-id active state across frames.
    static std::unordered_map<ImGuiID, bool> s_activeLast;
    ImGuiID wid = ImGui::GetID(id);
    if (!s_activeLast[wid])
        rewrapInPlace(buf.data(), (int)std::strlen(buf.data()), wrapW);

    int rows = 1;
    for (const char* p = buf.data(); *p; ++p) if (*p == '\n') ++rows;
    if (rows > maxLines) rows = maxLines;
    const float h = lineH * (float)rows + style.FramePadding.y * 2.0f;

    ChatInputCb cb;
    cb.wrapW = wrapW;
    ImGui::InputTextMultiline(id, buf.data(), buf.size(), ImVec2(-1, h),
        ImGuiInputTextFlags_CallbackEdit, chatInputEditCallback, &cb);
    s_activeLast[wid] = ImGui::IsItemActive();
    // Enter sends, Shift+Enter does not. Detect the KEY (not a trailing '\n') so a newline the
    // rewrap just inserted can never be mistaken for a submit.
    bool submit = ImGui::IsItemFocused() && !ImGui::GetIO().KeyShift &&
                  ImGui::IsKeyPressed(ImGuiKey_Enter, false);
    if (buf[0] == 0 && placeholder && placeholder[0]) {
        ImVec2 mn = ImGui::GetItemRectMin();
        ImGui::GetWindowDrawList()->AddText(
            ImVec2(mn.x + style.FramePadding.x * 2.0f, mn.y + style.FramePadding.y),
            ui::Palette::toU32(pal.textDisabled), placeholder);
    }
    return submit;
}

// Read-only, copyable monospace code view; height fits the content up to maxH.
void drawCodeBox(const char* id, const std::string& code, ImFont* mono, float maxH) {
    int lines = 1;
    for (char c : code) if (c == '\n') ++lines;
    if (mono) ImGui::PushFont(mono);
    float lh = ImGui::GetTextLineHeightWithSpacing();
    float h = lh * (float)lines + 10.0f;
    if (h > maxH) h = maxH;
    if (h < lh + 10.0f) h = lh + 10.0f;
    ImGui::InputTextMultiline(id, (char*)code.c_str(), code.size() + 1, ImVec2(-1, h),
                              ImGuiInputTextFlags_ReadOnly);
    if (mono) ImGui::PopFont();
}

// Colored unified-diff view (+ green / - red / @@ blue) in a scrolling box.
void drawDiffBox(const char* id, const std::string& diff, ImFont* mono, float maxH) {
    const auto& p = ui::palette;
    int lines = 1;
    for (char c : diff) if (c == '\n') ++lines;
    if (mono) ImGui::PushFont(mono);
    float lh = ImGui::GetTextLineHeightWithSpacing();
    float h = lh * (float)lines + 10.0f;
    if (h > maxH) h = maxH;
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(p.codeBg[0], p.codeBg[1], p.codeBg[2], p.codeBg[3]));
    ImGui::BeginChild(id, ImVec2(0, h), ImGuiChildFlags_Border,
                      ImGuiWindowFlags_HorizontalScrollbar);
    size_t start = 0;
    while (start <= diff.size()) {
        size_t nl = diff.find('\n', start);
        std::string ln = diff.substr(start, nl == std::string::npos ? std::string::npos : nl - start);
        ImVec4 col(p.diffDefault[0], p.diffDefault[1], p.diffDefault[2], 1);
        if (ln.rfind("+++", 0) == 0 || ln.rfind("---", 0) == 0)
            col = ImVec4(p.diffHdr[0], p.diffHdr[1], p.diffHdr[2], 1);
        else if (ln.rfind("@@", 0) == 0)
            col = ImVec4(p.diffHunk[0], p.diffHunk[1], p.diffHunk[2], 1);
        else if (!ln.empty() && ln[0] == '+')
            col = ImVec4(p.diffAdd[0], p.diffAdd[1], p.diffAdd[2], 1);
        else if (!ln.empty() && ln[0] == '-')
            col = ImVec4(p.diffDel[0], p.diffDel[1], p.diffDel[2], 1);
        ImGui::TextColored(col, "%s", ln.empty() ? " " : ln.c_str());
        if (nl == std::string::npos) break;
        start = nl + 1;
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
    if (mono) ImGui::PopFont();
}

// Dim, wrapped prose (reasoning / notes) in a scrolling box.
void drawProseBox(const char* id, const std::string& text, float maxH) {
    const auto& p = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(p.proseBg[0], p.proseBg[1], p.proseBg[2], p.proseBg[3]));
    ImGui::BeginChild(id, ImVec2(0, maxH), ImGuiChildFlags_Border);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(p.proseText[0], p.proseText[1], p.proseText[2], 1));
    ImGui::TextWrapped("%s", text.c_str());
    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

// Fit/interference readout: a green ("fits") or red ("clash") status line; when detailed,
// the per-pair overlap volumes (red) and clearance gaps (dim) below it.
void drawFitReadout(const FitReport& fit, bool detailed) {
    if (!fit.present) return;
    const auto& p = ui::palette;
    const ImVec4 good(p.fitOk[0], p.fitOk[1], p.fitOk[2], 1);
    const ImVec4 bad(p.fitBad[0], p.fitBad[1], p.fitBad[2], 1);
    const ImVec4 dim(p.fitDim[0], p.fitDim[1], p.fitDim[2], 1);
    ImGui::TextColored(fit.ok ? good : bad, "%s  %s", fit.ok ? "[fits]" : "[clash]",
                       fit.summary.empty() ? (fit.ok ? "No interference" : "Interference")
                                           : fit.summary.c_str());
    if (!detailed) return;
    for (const auto& f : fit.interferences) {
        ImGui::PushStyleColor(ImGuiCol_Text, bad);
        ImGui::BulletText("interference  %s <-> %s : %.1f mm\xc2\xb3", f.a.c_str(),
                          f.b.c_str(), f.value);
        ImGui::PopStyleColor();
    }
    for (const auto& f : fit.clearances) {
        ImGui::PushStyleColor(ImGuiCol_Text, dim);
        ImGui::BulletText("clearance  %s <-> %s : %.2f mm", f.a.c_str(), f.b.c_str(),
                          f.value);
        ImGui::PopStyleColor();
    }
}

// Draw a run-status glyph (spinning comet arc / check / cross / retry ring / pending hollow dot)
// centered in a `box`-wide cell at the cursor, then advance past it. Shared by the plan checklist
// and the action cards so every surface shows the same animated status art.
static void drawStatusGlyph(const std::string& status, float box) {
    const auto& pal = ui::palette;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 c(p0.x + box * 0.5f, p0.y + box * 0.5f);
    const float r = box * 0.30f;
    const bool running = status == "running";
    const bool pass    = status == "pass" || status == "done";
    const bool retry   = status == "retry";
    const bool fail    = status == "fail";
    ImU32 col = ui::Palette::toU32(running || pass ? pal.accent
                                   : retry ? pal.statusWarn
                                   : fail  ? pal.statusErr : pal.textDim);
    const float th = ui::metrics::stroke(1.6f);
    if (running) {                              // spinning comet arc
        float t = (float)ImGui::GetTime();
        const int seg = 14;
        for (int s = 0; s < seg; ++s) {
            float f = (float)s / seg, aa = t * 6.0f + f * 6.2831853f;
            float al = 0.12f + 0.88f * f;
            ImU32 cc = (col & 0x00FFFFFFu) | ((ImU32)(al * 255) << 24);
            dl->AddCircleFilled(ImVec2(c.x + std::cos(aa) * r, c.y + std::sin(aa) * r), th, cc);
        }
    } else if (pass) {                          // check mark
        dl->AddLine(ImVec2(c.x - r * 0.55f, c.y + r * 0.05f),
                    ImVec2(c.x - r * 0.10f, c.y + r * 0.50f), col, th);
        dl->AddLine(ImVec2(c.x - r * 0.10f, c.y + r * 0.50f),
                    ImVec2(c.x + r * 0.60f, c.y - r * 0.50f), col, th);
    } else if (fail) {                          // cross
        dl->AddLine(ImVec2(c.x - r * 0.5f, c.y - r * 0.5f),
                    ImVec2(c.x + r * 0.5f, c.y + r * 0.5f), col, th);
        dl->AddLine(ImVec2(c.x - r * 0.5f, c.y + r * 0.5f),
                    ImVec2(c.x + r * 0.5f, c.y - r * 0.5f), col, th);
    } else if (retry) {
        dl->AddCircle(c, r, col, 12, th);
    } else {
        dl->AddCircle(c, r * 0.8f, col, 12, th);  // pending hollow dot
    }
    ImGui::Dummy(ImVec2(box, box));
}

// ---- Liquid Glass v2 host port (contract H5): glass surface + bubble helpers ----------------
// ONE adaptivePanel id shared by the whole Copilot pane — header, composer, and every card below
// fetch this SAME cached surface via ui::panelId("host_agent_glass"); ui::adaptivePanel is
// idempotent per frame (it re-samples only on the first call each frame, from the full pane rect
// painted in drawAIPanel), so every later fetch below is free regardless of which window/child is
// current when it's called.
static const ui::AdaptiveSurface& chatSurf() {
    ImVec2 mn = ImGui::GetWindowPos();
    ImVec2 mx(mn.x + ImGui::GetWindowSize().x, mn.y + ImGui::GetWindowSize().y);
    return ui::adaptivePanel(ui::panelId("host_agent_glass"), ImRect(mn, mx));
}

// A "bubble" = an auto-height child painted over an explicit glassPanel fill, ported from
// ui/imgui/src/ui/agent_panel.cpp's GlassBubble. The child's true height is only known after
// EndChild(), so the PARENT draw list is channel-split: content goes to channel 1, and
// endGlassBubble() paints the correctly-sized fill into channel 0 (behind) once the real rect is
// known — measure-then-fill, no frame lag. Child-window content lives in the child's own draw
// list and is unaffected by the parent split.
struct GlassBubble {
    ImVec2         origin;
    float          width;
    ui::GlassLevel level;
    float          rounding;
};

static GlassBubble beginGlassBubble(float width, ui::GlassLevel level, float rounding) {
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
    ui::glassPanel(dl, ImRect(b.origin, p1), b.level, b.rounding, chatSurf());
    dl->ChannelsMerge();
    return ImRect(b.origin, p1);
}

// The build-plan card: a glass panel listing each planned part with its purpose, committed
// dimensions (mono) and feature bullets — plus a live status tick per part (spins while the
// matching orchestrator stage runs, checks when it passes), so it doubles as the todo checklist.
void drawPlanCard(const PlanInfo& plan, const std::vector<ActivityItem>& activity,
                  const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const ImVec4 accent(pal.accent[0], pal.accent[1], pal.accent[2], 1);
    auto lower = [](std::string s) {
        for (char& c : s) c = (char)std::tolower((unsigned char)c);
        return s;
    };
    // Match a part to an activity stage by name (substring of title or stage key) for its tick.
    auto partStatus = [&](const std::string& name) -> std::string {
        std::string ln = lower(name);
        if (ln.empty()) return "";
        for (const auto& a : activity)
            if (lower(a.title).find(ln) != std::string::npos ||
                lower(a.stage).find(ln) != std::string::npos)
                return a.status;
        return "";
    };
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                      8.0f * ui::uiScale);
    ImGui::BeginChild("##plancard", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 w0 = ImGui::GetWindowPos();
    // Left accent bar: the accent color at the planAccentBar alpha.
    dl->AddRectFilled(w0, ImVec2(w0.x + 3.0f, w0.y + ImGui::GetWindowHeight()),
                      ui::Palette::toU32(pal.accent) & 0x00FFFFFFu |
                      ((uint32_t)(unsigned char)pal.planAccentBar << 24));
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(accent, "Build plan");
    if (!plan.name.empty()) {
        ImGui::SameLine();
        ImGui::TextUnformatted(plan.name.c_str());
    }
    ImGui::PopFont();
    if (!plan.summary.empty()) dimText(plan.summary.c_str());
    for (size_t i = 0; i < plan.parts.size(); ++i) {
        const PlanPart& p = plan.parts[i];
        ImGui::PushID((int)i);
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        drawStatusGlyph(partStatus(p.name), ImGui::GetTextLineHeight());
        ImGui::SameLine(0, 7 * ui::uiScale);
        ImGui::PushFont(fonts.medium);
        ImGui::TextUnformatted(p.name.c_str());
        ImGui::PopFont();
        if (!p.purpose.empty()) dimText(p.purpose.c_str());
        if (!p.dims.empty()) {
            ImGui::PushFont(fonts.mono);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(pal.planDims[0], pal.planDims[1], pal.planDims[2], 1));
            ImGui::TextWrapped("%s  (mm)", p.dims.c_str());
            ImGui::PopStyleColor();
            ImGui::PopFont();
        }
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.planFeature[0], pal.planFeature[1], pal.planFeature[2], 1));
        for (const auto& f : p.features) {
            ImGui::Bullet();
            ImGui::SameLine();
            ImGui::TextWrapped("%s", f.c_str());
        }
        ImGui::PopStyleColor();
        ImGui::PopID();
    }
    if (!plan.fit.empty()) {
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        dimText(("Fit: " + plan.fit).c_str());
    }
    ImGui::PopStyleColor();
    ImGui::EndChild();
    endGlassBubble(gb);
}

// One line "Parts: a, b, c" with each name tinted its assembly color.
void drawPartsLine(const std::vector<PartInfo>& parts) {
    if (parts.empty()) return;
    const auto& p = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(p.partsDim[0], p.partsDim[1], p.partsDim[2], 1));
    ImGui::TextUnformatted("Parts:");
    ImGui::PopStyleColor();
    for (size_t i = 0; i < parts.size(); ++i) {
        ImGui::SameLine();
        std::string lbl = parts[i].name + (i + 1 < parts.size() ? "," : "");
        ImGui::TextColored(partColor((int)i), "%s", lbl.c_str());
    }
}

// ============================ transparent activity timeline ============================
// Map a raw orchestrator stage key to a plain-language heading the user can follow.
static std::string friendlyStageTitle(const std::string& stage) {
    if (stage == "intake")  return "Understanding the request";
    if (stage == "plan")    return "Planning the build";
    if (stage == "model")   return "Building the model";
    if (stage == "edit")    return "Editing the model";
    if (stage == "review")  return "Reviewing the result";
    if (stage == "fix")     return "Fixing issues";
    if (stage == "memory")  return "Saving notes";
    std::string t = stage;            // Title-case fallback for any unknown stage
    if (!t.empty()) t[0] = (char)std::toupper((unsigned char)t[0]);
    return t.empty() ? std::string("Working") : t;
}

void feedTurnActivity(ChatMsg& m, const std::string& kind, const nlohmann::json& data) {
    // Inside a mission, sub-run stage keys (intake/model/...) repeat per task — namespace them by
    // the current task index so task 2's "model" row doesn't reopen task 1's. Outside a mission
    // (curMissionTask < 0) the key is the bare stage, preserving the single-run behavior exactly.
    auto nsKey = [&](const std::string& s) {
        return m.curMissionTask >= 0 ? (std::to_string(m.curMissionTask) + ":" + s) : s;
    };
    auto findStage = [&](const std::string& s) -> ActivityItem* {
        for (auto it = m.activity.rbegin(); it != m.activity.rend(); ++it)
            if (it->stage == s) return &*it;
        return nullptr;
    };
    if (kind == "mission_started") {
        m.curMissionTask = -1;  // task rows arrive via mission_task_started
    } else if (kind == "mission_task_started") {
        int idx = jint(data, "index");
        m.curMissionTask = idx;
        std::string key = "task" + std::to_string(idx);
        ActivityItem* a = findStage(key);
        if (!a) {
            ActivityItem ni;
            ni.stage = key;
            ni.title = jstr(data, "title");
            if (ni.title.empty()) ni.title = jstr(data, "capability");
            ni.missionHeader = true;
            m.activity.push_back(std::move(ni));
            a = &m.activity.back();
        }
        a->status = "running";
    } else if (kind == "mission_task_result") {
        ActivityItem* a = findStage("task" + std::to_string(jint(data, "index")));
        if (a) {
            std::string s = jstr(data, "status");
            a->status = (s == "done") ? "done" : (s.empty() ? "done" : s);
            std::string summary = jstr(data, "summary");
            if (!summary.empty()) a->reason = summary;
        }
    } else if (kind == "mission_finished") {
        m.curMissionTask = -1;
        for (auto& a : m.activity)
            if (a.status == "running" || a.status.empty()) a.status = "done";
    } else if (kind == "stage_started") {
        std::string s = nsKey(jstr(data, "stage"));
        ActivityItem* a = findStage(s);
        if (!a) {
            ActivityItem ni;
            ni.stage = s;
            ni.title = friendlyStageTitle(jstr(data, "stage"));
            m.activity.push_back(std::move(ni));
            a = &m.activity.back();
        }
        a->status = "running";
    } else if (kind == "agent_output") {
        ActivityItem* a = findStage(nsKey(jstr(data, "stage")));
        if (a) {
            std::string raw = jstr(data, "raw");
            if (!raw.empty()) a->detail = raw;     // keep the latest attempt's thoughts/output
            a->durationMs = jint(data, "duration_ms");
            a->opsCount   = jint(data, "ops_count");
            if (data.contains("tokens") && data["tokens"].is_object()) {
                a->tokensIn  = jint(data["tokens"], "in");
                a->tokensOut = jint(data["tokens"], "out");
            }
        }
    } else if (kind == "stage_result") {
        ActivityItem* a = findStage(nsKey(jstr(data, "stage")));
        if (a) {
            std::string v = jstr(data, "verdict");
            a->status = v.empty() ? "done" : v;
            a->reason = jstr(data, "reason");
        }
    } else if (kind == "run_finished") {
        // A sub-run finished (one mission task) OR a single-run completed. Close only the rows
        // for the CURRENT task; mission_finished does the final sweep. Outside a mission this
        // closes everything, as before.
        for (auto& a : m.activity)
            if ((a.status == "running" || a.status.empty()) && !a.missionHeader) a.status = "done";
    }
}

namespace ui {

// Forward decls for the conversation helpers (defined further down) so the header/composer can
// drive them.
static void ensureConversations(AppState& st);
static void newConversation(AppState& st);
static void switchConversation(AppState& st, int idx);
static void closeConversation(AppState& st, int idx);
static void submitTurn(AppState& st, const std::string& text);
static bool aiTurnActive(AppState& st);
static bool activeConversationRunning(AppState& st);

// ============================ W3-H: unified AI Copilot pane ============================
// One pane, top-down: header -> input -> mode pills -> conversation (with inline live run
// status + embedded Activity tree) -> collapsing Code / Console footers. Replaces the old
// Design/Edit/Chat tab triad. All backend launch calls are preserved verbatim.

// One pass of the agent loop, rendered as a compact expandable card. Reuses the shared
// code/diff/prose/fit/parts renderers (drawCodeBox/drawDiffBox/...). Refactored out of the
// old conversation loop unchanged in behavior.
static void drawAgentStep(AgentStep& s, const ui::Fonts& fonts, float logW) {
    const auto& pal = ui::palette;
    const ImVec4 accentCol(pal.accent[0], pal.accent[1], pal.accent[2], 1);
    ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                      8.0f * ui::uiScale);
    ImGui::BeginChild("##stepcard", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
    // Build issues are amber, not red: a failed pass is normal mid-loop — the error goes
    // straight back to the model, which usually fixes it.
    ImVec4 badge = s.ok ? (s.done ? accentCol
                               : ImVec4(pal.iterBuilt[0], pal.iterBuilt[1], pal.iterBuilt[2], 1))
                        : ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1);
    const char* word = s.ok ? (s.done ? "done" : "built") : "build issue";
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(badge, "Iteration %d - %s", s.index + 1, word);
    ImGui::PopFont();
    if (!s.dims.empty()) {
        ImGui::SameLine();
        ImGui::PushFont(fonts.mono);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1));
        ImGui::Text("%s", s.dims.c_str());
        ImGui::PopStyleColor();
        ImGui::PopFont();
    }

    // assembly: colored parts line + interference/clearance readout
    if (s.isAssembly) {
        drawPartsLine(s.parts);
        drawFitReadout(s.fit, /*detailed=*/true);
    }

    // render thumbnail (lazy GL upload — we're on the GL thread here)
    if (!s.contactSheet.empty()) {
        if (!s.triedThumb) { s.thumb = loadTexture(s.contactSheet); s.triedThumb = true; }
        if (s.thumb.id) {
            float maxTw = ui::metrics::copilotThumbW();
            float tw = logW < maxTw ? logW : maxTw;
            float th = tw * (float)s.thumb.h / (float)s.thumb.w;
            ImGui::Image((ImTextureID)(intptr_t)s.thumb.id, ImVec2(tw, th));
        }
    }

    if (!s.notes.empty()) dimText(s.notes.c_str());
    if (!s.ok && !s.error.empty()) {
        // Calm one-line cause; the raw traceback stays behind a toggle so a self-correcting
        // pass never reads like a crash.
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1));
        ImGui::TextWrapped("%s", errSummary(s.error).c_str());
        ImGui::PopStyleColor();
        dimText("The CAD code failed this pass; the error was fed back to the AI to correct.");
    }

    // expanders: the model's reasoning, the full code, and what changed
    bool first = true;
    if (!s.thinking.empty()) {
        if (toggleChip("Thoughts", s.showThinking)) s.showThinking = !s.showThinking;
        first = false;
    }
    if (!s.code.empty()) {
        if (!first) ImGui::SameLine();
        if (toggleChip("Code", s.showCode)) s.showCode = !s.showCode;
        first = false;
    }
    if (!s.diff.empty()) {
        if (!first) ImGui::SameLine();
        if (toggleChip("Diff", s.showDiff)) s.showDiff = !s.showDiff;
        first = false;
    }
    if (!s.ok && !s.error.empty()) {
        if (!first) ImGui::SameLine();
        if (toggleChip("Error details", s.showError)) s.showError = !s.showError;
        first = false;
    }
    if (s.showThinking && !s.thinking.empty())
        drawProseBox("##think", s.thinking, ui::metrics::copilotProseMaxH());
    if (s.showCode && !s.code.empty())
        drawCodeBox("##code_v", s.code, fonts.mono, ui::metrics::px(260.0f));
    if (s.showDiff && !s.diff.empty())
        drawDiffBox("##diff_v", s.diff, fonts.mono, ui::metrics::copilotDiffMaxH());
    if (s.showError && !s.error.empty())
        drawCodeBox("##err_v", s.error, fonts.mono, ui::metrics::px(200.0f));
    ImGui::PopStyleColor();
    ImGui::EndChild();
    endGlassBubble(gb);
}

// One assistant turn: name + plan card + summary + iteration cards. While running, an inline
// live status line (spinner + phase) with a thin progress accent bar, plus a collapsible
// "Activity" section embedding the orchestrator run tree for this run.
// C45: local models reply in markdown but ImGui renders plain text — the raw ** and ###
// marks read as noise. Strip the common inline marks (bold/italic asterisks, heading hashes,
// backticks) for DISPLAY only; the stored message keeps the original text.
static std::string stripMdMarks(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (size_t i = 0; i < in.size(); ++i) {
        char c = in[i];
        if (c == '*' || c == '`') continue;                       // **bold* / *it* / `code`
        if (c == '#' && (i == 0 || in[i - 1] == '\n')) {          // heading hashes at line start
            while (i < in.size() && in[i] == '#') ++i;
            if (i < in.size() && in[i] == ' ') ++i;
            --i;
            continue;
        }
        out.push_back(c);
    }
    return out;
}

// Create a fresh part document if needed and launch the v2 orchestrator for `request`. The
// current doc is replaced only when it already has features (so an empty new part is reused).
// Centralized so both the direct Design path and the post-clarify "Build it" path share it.
static void launchDesignRun(AppState& st, const std::string& request) {
    std::string err;
    bool docOk = st.docMirror.hasDoc();
    if (docOk) {
        const nlohmann::json& tree = st.docMirror.tree();
        bool hasFeatures = tree.is_object() && tree.contains("features") &&
                           tree["features"].is_array() && !tree["features"].empty();
        if (hasFeatures) docOk = st.docMirror.newPart(st.worker, err);
    } else {
        docOk = st.docMirror.newPart(st.worker, err);
    }
    if (!docOk) {
        st.addLog("[err] design: could not create a part document: " + err);
        st.status = "Worker not ready";  st.statusKind = 2;
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
            if (!it->fromUser && (it->running || it->clarify.active)) {
                it->running = false;
                it->clarify = ClarifyState{};
                it->errKind = ERR_SYSTEM;
                it->text = "I couldn't open a new part document (worker not ready). "
                           "Check the Console and try again.";
                break;
            }
        return;
    }
    st.clearViewportForNewDoc();
    st.addLog("[..] designing (v2): " + request);
    st.status = "Designing...";  st.statusKind = 0;
    st.agentV2Running = true;
    st.agentV2 = true;
    if (st.activityPanel)
        st.activityPanel->clearLive();
    launchAgentRunV2(st.worker, st.job, request, st.docMirror.docId(),
                     st.ribbonTab == AppState::RT_Manufacture);
}

// Launch the MISSION orchestrator for a multi-capability request (e.g. "model X, then run CFD and
// prove it's good"). Mirrors launchDesignRun's doc-prep + mutex + activity reset, but routes to
// agent_mission_v1 so the run decomposes into a task chain (design/assembly -> sim -> verdict).
// Shares the same fresh-part policy: a new build starts on a clean part doc; an existing model is
// kept (a mission that only analyzes the open part should not wipe it).
static void launchMissionRunChat(AppState& st, const std::string& request, bool needFreshDoc) {
    std::string err;
    bool docOk = st.docMirror.hasDoc();
    if (needFreshDoc) {
        if (docOk) {
            const nlohmann::json& tree = st.docMirror.tree();
            bool hasFeatures = tree.is_object() && tree.contains("features") &&
                               tree["features"].is_array() && !tree["features"].empty();
            if (hasFeatures) docOk = st.docMirror.newPart(st.worker, err);
        } else {
            docOk = st.docMirror.newPart(st.worker, err);
        }
    }
    if (!docOk) {
        st.addLog("[err] mission: could not create a part document: " + err);
        st.status = "Worker not ready";  st.statusKind = 2;
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
            if (!it->fromUser && (it->running || it->clarify.active)) {
                it->running = false;
                it->clarify = ClarifyState{};
                it->errKind = ERR_SYSTEM;
                it->text = "I couldn't open a part document (worker not ready). "
                           "Check the Console and try again.";
                break;
            }
        return;
    }
    if (needFreshDoc) st.clearViewportForNewDoc();
    st.addLog("[..] mission: " + request);
    st.status = "Working on it...";  st.statusKind = 0;
    st.agentV2Running = true;
    st.agentV2 = true;
    if (st.activityPanel)
        st.activityPanel->clearLive();
    launchMissionRun(st.worker, st.job, request, st.docMirror.docId());
}

// The inline clarify card: the build-critical questions the model asked, each with an editable
// value field seeded with its suggested default. "Build it" folds the answers into the request
// and launches the run; "Skip" uses the suggested defaults. Shown inside an assistant turn.
static void drawClarifyCard(AppState& st, ChatMsg& m, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const ImVec4 accentCol(pal.accent[0], pal.accent[1], pal.accent[2], 1);
    ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                      8.0f * ui::uiScale);
    ImGui::BeginChild("##clarify", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(accentCol, "A few details to get this right");
    ImGui::PopFont();
    dimText("Accept the suggested values or change them, then build. Skip uses the defaults.");
    for (size_t i = 0; i < m.clarify.questions.size(); ++i) {
        ClarifyQ& q = m.clarify.questions[i];
        ImGui::PushID((int)i);
        std::string lbl = q.ask;
        if (!q.unit.empty()) lbl += "  (" + q.unit + ")";
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        ImGui::TextWrapped("%s", lbl.c_str());
        drawChatInput("##cq", q.answer, q.suggested.empty() ? "value" : q.suggested.c_str(), 2);
        ImGui::PopID();
    }
    if (!m.clarify.assumptions.empty()) {
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
        ImGui::TextUnformatted("Assuming:");
        ImGui::PopStyleColor();
        for (const auto& a : m.clarify.assumptions) {
            ImGui::Bullet();
            ImGui::SameLine();
            dimText(a.c_str());
        }
    }
    ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
    bool build = primaryButton("Build it", ImVec2(-1, ui::metrics::copilotBtnH()));
    bool skip = ImGui::Button("Skip - use defaults", ImVec2(-1, 0));
    ImGui::PopStyleColor();
    ImGui::EndChild();
    endGlassBubble(gb);
    if (build || skip) {
        // Fold each answer (or its suggested default on Skip) into the request so the
        // orchestrator builds with concrete numbers.
        std::string aug = m.clarify.request + "\n\nClarified dimensions:";
        for (auto& q : m.clarify.questions) {
            std::string val = skip ? q.suggested : std::string(q.answer.data());
            if (val.empty()) val = q.suggested;
            std::string nm = q.key.empty() ? q.ask : q.key;
            aug += "\n- " + nm + ": " + val + (q.unit.empty() ? "" : " " + q.unit);
        }
        for (const auto& a : m.clarify.assumptions) aug += "\n- (assumed) " + a;
        m.clarify.active = false;
        m.clarify.questions.clear();
        m.clarify.assumptions.clear();
        m.running = true;
        m.liveStatus = "Building...";
        launchDesignRun(st, aug);
    }
}

// --- model-routing + context-meter helpers (transparency UI) -----------------------------------
// "openai/gpt-oss-120b:free" -> "gpt-oss-120b" for compact display.
static std::string shortModelName(const std::string& id) {
    std::string s = id;
    size_t slash = s.rfind('/'); if (slash != std::string::npos) s = s.substr(slash + 1);
    size_t colon = s.find(':');  if (colon != std::string::npos) s = s.substr(0, colon);
    return s;
}
static std::string fmtTokens(int n) {
    char b[32];
    if (n >= 1000000) std::snprintf(b, sizeof(b), "%.1fM", n / 1000000.0);
    else if (n >= 1000) std::snprintf(b, sizeof(b), "%dk", (n + 500) / 1000);
    else std::snprintf(b, sizeof(b), "%d", n);
    return b;
}
// The active model's total context window (mirrors core default_context_window for the meter).
static int effectiveContextWindow(const Settings& s) {
    if (s.contextWindow > 0) return s.contextWindow;
    return ui::defaultContextWindowForModel(s.provider, s.model[0] ? s.model : "");
}
// ============================ typed-block agent timeline ============================
// The orchestrator event stream (feedTurnActivity -> ChatMsg::activity) is rendered as the
// DESIGN.md §5 typed blocks: dimmed, collapsible "thinking" blocks for reasoning-only stages and
// glass "action cards" (icon + human label + animated status + folded result + expandable detail)
// for tool / build stages. Engine RPCs + the event plumbing are unchanged — this is layout only.

// Atlas icon for a stage key (string-keyed: a missing name just falls back to no glyph).
static const char* stageIconName(const std::string& stage, bool missionHeader) {
    if (missionHeader) return "list";
    std::string s = stage;
    size_t colon = s.find(':');           // strip the "<taskIdx>:" mission namespace prefix
    if (colon != std::string::npos) s = s.substr(colon + 1);
    if (s == "intake") return "search";
    if (s == "plan")   return "list";
    if (s == "model")  return "cpu";
    if (s == "edit")   return "edit";
    if (s == "review") return "eye";
    if (s == "fix")    return "wrench";
    if (s == "memory") return "bookmark";
    return "cpu";
}

// Classify a row: reasoning-only stages (no CAD ops, with captured detail) render as a thinking
// block; build/tool stages (model/edit/review/fix/memory) and mission headers are action cards.
static bool isThinkingItem(const ActivityItem& a) {
    if (a.missionHeader) return false;
    std::string s = a.stage;
    size_t colon = s.find(':');
    if (colon != std::string::npos) s = s.substr(colon + 1);
    if (s == "model" || s == "edit" || s == "review" || s == "fix" || s == "memory") return false;
    return a.opsCount == 0 && !a.detail.empty();
}

// A collapsible, dimmed reasoning block. While it is the frontier of work it stays open so the
// thoughts stream; once a later stage reaches a verdict (`laterDone`) it auto-collapses unless the
// user expanded it (a.expanded, set by clicking). `showThinking` off -> a single dim one-liner.
static void drawThinkingBlock(ActivityItem& a, const ui::Fonts& fonts, bool showThinking,
                              bool laterDone) {
    const float sc = ui::uiScale;
    ImGui::Dummy(ImVec2(0, 2 * sc));

    if (!showThinking) {
        ImGui::PushFont(fonts.body);
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
        ImGui::TextUnformatted(a.title.empty() ? "Thinking" : a.title.c_str());
        ImGui::PopStyleColor();
        ImGui::PopFont();
        return;
    }

    const bool open = a.expanded || !laterDone;   // forced open while it is the active frontier
    const bool hasDetail = !a.detail.empty();
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f * sc);
    ImGui::BeginChild("##thinkblk", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushFont(fonts.body);
    ImGui::TextColored(surf.textSecondary, "%s", a.title.empty() ? "Thinking" : a.title.c_str());
    ImGui::PopFont();
    if (hasDetail) {
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) a.expanded = !open;
        ImGui::SameLine(0, 6 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(open ? "(hide)" : "(show thinking)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) a.expanded = !open;
    }
    if (open && hasDetail)
        drawProseBox("##thinkdetail", stripMdMarks(a.detail), ui::metrics::copilotProseMaxH());

    ImGui::EndChild();
    endGlassBubble(gb);
}

// A glass action card for a tool call / orchestrator build stage: status glyph + tool icon + human
// label + duration/ops meta, the verdict folded in as a result line, an expandable drawer (a
// legacy AgentStep's thinking/code/diff/thumbnail when attached, else the raw detail), and a red
// border when the stage failed. `step` is non-null only for v1 runs that carry an AgentStep.
static void drawActionCard(ActivityItem& a, AgentStep* step, const ui::Fonts& fonts, float logW) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const bool running = a.status == "running";
    const bool fail    = a.status == "fail";

    ImGui::Dummy(ImVec2(0, 2 * sc));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f * sc);
    ImGui::BeginChild("##actcard", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    const ui::AdaptiveSurface& surf = chatSurf();

    // ---- header row: status glyph + tool icon + label + meta ----
    drawStatusGlyph(a.status, ImGui::GetTextLineHeight());
    ImGui::SameLine(0, 6 * sc);
    ui::IconUV iv = ui::getIcon(stageIconName(a.stage, a.missionHeader), 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight();
        ImVec4 tint(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z,
                   running ? 1.0f : 0.85f);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 6 * sc);
    }
    ImVec4 titleCol = running ? ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1)
                    : fail    ? ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1)
                              : surf.textPrimary;
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(titleCol, "%s", a.title.c_str());
    ImGui::PopFont();

    const bool hasDrawer = (step && (!step->thinking.empty() || !step->code.empty() ||
                                     !step->diff.empty() || !step->contactSheet.empty())) ||
                           !a.detail.empty();
    if (hasDrawer) {
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) a.expanded = !a.expanded;
        ImGui::SameLine(0, 6 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(a.expanded ? "(hide)" : "(details)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) a.expanded = !a.expanded;
    }
    if (a.durationMs > 0 || a.opsCount > 0) {
        ImGui::SameLine(0, 8 * sc);
        char meta[96];
        if (a.opsCount > 0 && a.durationMs > 0)
            std::snprintf(meta, sizeof(meta), "%d ops \xc2\xb7 %.1fs", a.opsCount, a.durationMs / 1000.0);
        else if (a.durationMs > 0)
            std::snprintf(meta, sizeof(meta), "%.1fs", a.durationMs / 1000.0);
        else
            std::snprintf(meta, sizeof(meta), "%d ops", a.opsCount);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(meta);
        ImGui::PopStyleColor();
    }

    // ---- result / notes line (folded into the card) ----
    if (!a.reason.empty()) dimText(a.reason.c_str());
    else if (step && !step->notes.empty()) dimText(step->notes.c_str());

    // ---- expandable drawer ----
    if (a.expanded && hasDrawer) {
        if (step) {
            if (!step->thinking.empty())
                drawProseBox("##actthink", step->thinking, ui::metrics::copilotProseMaxH());
            if (!step->code.empty())
                drawCodeBox("##actcode", step->code, fonts.mono, ui::metrics::px(260.0f));
            if (!step->diff.empty())
                drawDiffBox("##actdiff", step->diff, fonts.mono, ui::metrics::copilotDiffMaxH());
            if (!step->contactSheet.empty()) {
                if (!step->triedThumb) { step->thumb = loadTexture(step->contactSheet); step->triedThumb = true; }
                if (step->thumb.id) {
                    float maxTw = ui::metrics::copilotThumbW();
                    float tw = logW < maxTw ? logW : maxTw;
                    float th = tw * (float)step->thumb.h / (float)step->thumb.w;
                    ImGui::Image((ImTextureID)(intptr_t)step->thumb.id, ImVec2(tw, th));
                }
            }
        } else if (!a.detail.empty()) {
            drawProseBox("##actdetail", stripMdMarks(a.detail), ui::metrics::copilotProseMaxH());
        }
    }

    ImGui::EndChild();
    ImRect cardRect = endGlassBubble(gb);

    // ---- error frame: a red border around the whole card ----
    if (fail) {
        ImGui::GetWindowDrawList()->AddRect(cardRect.Min, cardRect.Max, ui::Palette::toU32(pal.statusErr),
                                            8.0f * sc, 0, ui::metrics::stroke(1.5f));
    }
}

// The agent timeline: dispatch each activity row to a thinking block or an action card, computing
// the thinking auto-collapse (a thinking row collapses once a later stage reaches a verdict).
static void drawAgentTimeline(ChatMsg& m, const ui::Fonts& fonts, float logW, bool showThinking) {
    if (m.activity.empty()) return;
    ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
    for (size_t i = 0; i < m.activity.size(); ++i) {
        ActivityItem& a = m.activity[i];
        ImGui::PushID((int)i);
        if (isThinkingItem(a)) {
            bool laterDone = false;
            for (size_t j = i + 1; j < m.activity.size(); ++j) {
                const std::string& s = m.activity[j].status;
                if (s == "pass" || s == "done" || s == "fail" || s == "retry") { laterDone = true; break; }
            }
            drawThinkingBlock(a, fonts, showThinking, laterDone);
        } else {
            drawActionCard(a, nullptr, fonts, logW);
        }
        ImGui::PopID();
    }
}

// One user turn: a right-aligned, markdown-stripped bubble (DESIGN.md §5 "User message" block).
static void drawUserBlock(const ChatMsg& m, const ui::Fonts& fonts, float panelW) {
    const auto& pal = ui::palette;
    const ImVec4 accentCol(pal.accent[0], pal.accent[1], pal.accent[2], 1);
    // Right-align: indent ~22% of the panel so the bubble sits against the right edge.
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + panelW * 0.22f);
    const float bw = ImGui::GetContentRegionAvail().x;
    GlassBubble gb = beginGlassBubble(bw, ui::GlassLevel::Regular, 8.0f * ui::uiScale);
    ImGui::BeginChild("##umsg", ImVec2(bw, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(accentCol, "You");
    ImGui::PopFont();
    ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
    ImGui::TextWrapped("%s", stripMdMarks(m.text).c_str());
    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImRect bubble = endGlassBubble(gb);
    ImGui::GetWindowDrawList()->AddRect(bubble.Min, bubble.Max,
        ImGui::ColorConvertFloat4ToU32(ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.55f)),
        8.0f * ui::uiScale, 0, ui::metrics::stroke(1.0f));
    ImGui::Dummy(ImVec2(0, 6 * ui::uiScale));
}

static void drawAssistantTurn(AppState& st, ChatMsg& m, const ui::Fonts& fonts, float logW) {
    const auto& pal = ui::palette;
    const ImVec4 accentCol(pal.accent[0], pal.accent[1], pal.accent[2], 1);

    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(ImVec4(pal.assistantName[0], pal.assistantName[1],
                             pal.assistantName[2], 1), "Assistant");
    ImGui::PopFont();

    // (1) Plan / todo block — a live checklist that ticks off as stages complete (DESIGN §5).
    if (m.plan.present) drawPlanCard(m.plan, m.activity, fonts);

    // (2) Agent timeline — typed blocks: collapsible thinking + glass action cards.
    drawAgentTimeline(m, fonts, logW, gShowThinking);

    // (3) Streaming assistant text — a Thin glass "prose" card (Liquid Glass v2 §6, host
    // contract H5) keeps body copy legible over the live viewport blur.
    if (!m.text.empty() && m.errKind == ERR_NONE) {
        GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                          4.0f * ui::uiScale);
        ImGui::BeginChild("##prose", ImVec2(0, 0),
                          ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
        ImGui::TextWrapped("%s", stripMdMarks(m.text).c_str());
        ImGui::PopStyleColor();
        ImGui::EndChild();
        endGlassBubble(gb);
        ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
    }

    // (4) Pre-flight clarify gate: the model's build-critical questions, awaiting the user.
    if (m.clarify.active) drawClarifyCard(st, m, fonts);

    // (5) Legacy v1 AgentStep cards — kept for backward compat (glass-restyled in drawAgentStep).
    for (size_t si = 0; si < m.steps.size(); ++si) {
        ImGui::PushID((int)si);
        drawAgentStep(m.steps[si], fonts, logW);
        ImGui::PopID();
    }

    // (6) Live spinner + model-transparency line + thin progress bar (while running).
    if (m.running) {
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        static const char* kSpin[] = {"|", "/", "-", "\\"};
        int sp = (int)(ImGui::GetTime() * 8.0) & 3;
        ImGui::TextColored(accentCol, "%s", kSpin[sp]);
        ImGui::SameLine();
        const ui::AdaptiveSurface& surf = chatSurf();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s", m.liveStatus.empty() ? "working..." : m.liveStatus.c_str());
        ImGui::PopStyleColor();
        // Which model(s) are driving this turn — transparency about what's actually running.
        {
            std::string line = "Command model: " + shortModelName(st.settings.model);
            if (st.settings.expVisionReview && st.settings.expVisionModel[0])
                line += "   \xc2\xb7   Vision: " + shortModelName(st.settings.expVisionModel);
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextWrapped("%s", line.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::PushStyleColor(ImGuiCol_PlotHistogram, ui::Palette::toU32(pal.accent));
        ImGui::PushStyleColor(ImGuiCol_FrameBg, ui::Palette::toU32(pal.accentSoft));
        float frac = 0.15f + 0.35f * (float)(0.5 + 0.5 * std::sin(ImGui::GetTime() * 2.2));
        ImGui::ProgressBar(frac, ImVec2(-1, 3.0f * ui::uiScale), "");
        ImGui::PopStyleColor(2);
    } else {
        // (7) Usage footer — tokens + wall-time for the finished turn, dim (DESIGN §5).
        int tin = 0, tout = 0, tms = 0;
        for (const auto& a : m.activity) { tin += a.tokensIn; tout += a.tokensOut; tms += a.durationMs; }
        if (tin > 0) {
            ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textTertiary);
            ImGui::Text("%d tokens in / %d out \xc2\xb7 %.1f s", tin, tout, tms / 1000.0);
            ImGui::PopStyleColor();
        }
    }

    // (8) Error block — a distinct red-bordered glass card carrying the real reason (DESIGN §5).
    if (m.errKind != ERR_NONE && !m.text.empty()) {
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                          8.0f * ui::uiScale);
        ImGui::BeginChild("##errcard", ImVec2(0, 0),
                          ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
        const float* ec = (m.errKind == ERR_SYSTEM) ? pal.errSystem : pal.errCad;
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ec[0], ec[1], ec[2], 1));
        ImGui::TextWrapped("%s", stripMdMarks(m.text).c_str());
        ImGui::PopStyleColor();
        ImGui::EndChild();
        ImRect cardRect = endGlassBubble(gb);
        ImGui::GetWindowDrawList()->AddRect(cardRect.Min, cardRect.Max, ui::Palette::toU32(pal.statusErr),
                                            8.0f * ui::uiScale, 0, ui::metrics::stroke(1.5f));
    }
}

// ---- header: logo + "AI-CAD Copilot" + Sessions + status dot ----
static void drawCopilotHeader(AppState& st, bool busy) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;

    if (st.logoIcon.id) {
        float h = ImGui::GetTextLineHeight();
        ImGui::Image((ImTextureID)(intptr_t)st.logoIcon.id, ImVec2(h, h));
        ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
    }
    ImGui::PushFont(st.fonts.h2);
    ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
    ImGui::TextUnformatted("AI-CAD Copilot");
    ImGui::PopStyleColor();
    ImGui::PopFont();

    const ImGuiStyle& style = ImGui::GetStyle();
    const float lh = ImGui::GetTextLineHeight();
    float sessW = ImGui::CalcTextSize("Sessions").x + style.FramePadding.x * 2.0f;
    float gearW = lh;
    float cluster = sessW + style.ItemSpacing.x + gearW + style.ItemSpacing.x + lh;
    ImGui::SameLine();
    float xRight = ImGui::GetContentRegionMax().x - cluster;
    if (xRight > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(xRight);

    if (busy) ImGui::BeginDisabled();
    if (ImGui::SmallButton("Sessions")) st.showSessionsPicker = !st.showSessionsPicker;
    if (busy) ImGui::EndDisabled();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Show or hide the session list — switch chats out of order.");

    ImGui::SameLine();
    IconUV gear = ui::getIcon("settings", 20);
    bool openSettings = false;
    if (gear.valid)
        openSettings = ImGui::ImageButton("##aiheadergear", gear.texId,
                                          ImVec2(gearW - 4 * sc, gearW - 4 * sc),
                                          gear.uv0, gear.uv1);
    else
        openSettings = ImGui::SmallButton("set");
    if (openSettings) st.focusSettingsTab = true;
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("AI settings");

    ImGui::SameLine();
    const float* dot = pal.accentMid;  const char* tip = "Idle";
    if (st.statusKind == 2) { dot = pal.statusErr; tip = "Error"; }
    else if (busy || st.agentV2Running || activeConversationRunning(st)) {
        dot = pal.accent; tip = "Running";
    }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 cp = ImGui::GetCursorScreenPos();
    dl->AddCircleFilled(ImVec2(cp.x + lh * 0.5f, cp.y + lh * 0.5f), lh * 0.30f,
                        ImGui::ColorConvertFloat4ToU32(ImVec4(dot[0], dot[1], dot[2], dot[3])));
    ImGui::Dummy(ImVec2(lh, lh));
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", tip);
}

// Sessions list — replaces the chat log while open so users can jump between sessions easily.
static void drawSessionsPanel(AppState& st, bool busy) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    ensureConversations(st);

    if (ImGui::Button("+ New session") && !busy) { newConversation(st); st.prompt[0] = 0; }
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Start a fresh conversation.");
    ImGui::SameLine();
    if (ImGui::SmallButton("Back to chat")) st.showSessionsPicker = false;

    ImGui::Separator();
    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
    ImGui::TextUnformatted("Switch sessions — running ones keep working in the background.");
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 4 * sc));

    for (int i = 0; i < (int)st.conversations.size(); ++i) {
        Conversation& c = st.conversations[i];
        std::string title = c.title.empty() ? ("Chat " + std::to_string(i + 1)) : c.title;
        const std::vector<ChatMsg>& msgs = (i == st.activeConv) ? st.chat : c.msgs;
        bool running = false;
        for (auto it = msgs.rbegin(); it != msgs.rend(); ++it)
            if (!it->fromUser && it->running) { running = true; break; }
        if (i == st.activeConv && aiTurnActive(st)) running = true;
        bool active = (i == st.activeConv);
        ImGui::PushID(i);
        float trashW = ImGui::GetFrameHeight();
        float rowW = ImGui::GetContentRegionAvail().x;
        std::string label = title;
        if (running) label += "  \xe2\x80\xa2 running";
        if (ImGui::Selectable(label.c_str(), active, 0,
                              ImVec2(rowW - trashW - 4 * sc, 0)) && !busy)
            switchConversation(st, i);
        ImGui::SameLine(ImGui::GetContentRegionMax().x - trashW);
        IconUV trash = ui::getIcon("delete", 20);
        bool del = trash.valid
            ? ImGui::ImageButton("##delconv", trash.texId,
                                 ImVec2(trashW - 8 * sc, trashW - 8 * sc), trash.uv0, trash.uv1)
            : ImGui::SmallButton("x");
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Delete this session");
        if (del && !busy) { closeConversation(st, i); ImGui::PopID(); break; }
        ImGui::PopID();
    }
}


// ============================ conversation tabs + persistence ============================
// Each conversation is its own tab. The ACTIVE conversation's messages live in st.chat; the
// rest park in st.conversations[i].msgs. Persisted per document to chat_history.json.

static std::string chatHistoryPath() { return appdataDir() + "/AI-CAD/chat_history.json"; }

// A stable key for the active document: the saved file path if any (survives restarts), else
// the live doc id, else "scratch" (an unsaved, never-touched session).
static std::string convDocKey(AppState& st) {
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
        const auto& t = st.docTabs[st.activeTab];
        if (!t.path.empty()) return "file:" + t.path;
        if (!t.docId.empty()) return "doc:" + t.docId;
    }
    std::string d = st.docMirror.hasDoc() ? st.docMirror.docId() : std::string();
    return d.empty() ? std::string("scratch") : ("doc:" + d);
}

static std::string newConvId(AppState& st) { return "c" + std::to_string(++st.convCounter); }

// Guarantee at least one (active) conversation exists, adopting whatever is already in st.chat.
static void ensureConversations(AppState& st) {
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size()) return;
    st.conversations.clear();
    Conversation c;
    c.id = newConvId(st);
    c.title = "Chat 1";
    st.conversations.push_back(std::move(c));
    st.activeConv = 0;  // st.chat remains the active buffer
}

void persistChatHistory(AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    if (st.chatDocKey.empty()) return;
    nlohmann::json root = nlohmann::json::object();
    {
        std::ifstream in(chatHistoryPath());
        if (in) { try { in >> root; } catch (...) { root = nlohmann::json::object(); } }
        if (!root.is_object()) root = nlohmann::json::object();
    }
    nlohmann::json convs = nlohmann::json::array();
    for (int i = 0; i < (int)st.conversations.size(); ++i) {
        const std::vector<ChatMsg>& msgs = (i == st.activeConv) ? st.chat : st.conversations[i].msgs;
        nlohmann::json jm = nlohmann::json::array();
        for (const auto& m : msgs) {
            if (m.text.empty()) continue;  // skip running/placeholder turns with no content yet
            jm.push_back({{"u", m.fromUser}, {"t", m.text}, {"e", m.errKind}});
        }
        convs.push_back({{"id", st.conversations[i].id},
                         {"title", st.conversations[i].title},
                         {"msgs", jm}});
    }
    root[st.chatDocKey] = {{"active", st.activeConv}, {"conversations", convs}};
    try {
        std::filesystem::create_directories(appdataDir() + "/AI-CAD");
        std::ofstream out(chatHistoryPath());
        if (out) out << root.dump(1);
    } catch (...) {
    }
}

void pollChatOpenFoam(AppState& st) {
    if (!st.chatCfdPending || !st.simClient) return;
    core::SimJobResult& job = st.simClient->job();
    if (!job.ready.load()) return;                 // OpenFOAM still meshing/solving
    if (job.ownerDocId != "__chat_cfd__") return;  // not the chat's job (a study's result)
    st.chatCfdPending = false;
    job.ready = false;                             // consume (SimManager ignored it — wrong owner)

    ChatMsg* msg = nullptr;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (!it->fromUser && it->running) { msg = &*it; break; }

    std::string answer, err;
    if (!job.ok) {
        err = job.error.empty() ? "OpenFOAM run failed" : job.error;
    } else {
        const double cd = job.result.value("cd", 0.0), cl = job.result.value("cl", 0.0);
        const double drag = job.result.value("drag_n", 0.0), lift = job.result.value("lift_n", 0.0);
        const double re = job.result.value("reynolds", 0.0);
        char b[512];
        std::snprintf(b, sizeof(b),
            "Accurate OpenFOAM RANS (k-omega SST) at %.1f m/s, %.1f deg AoA (Re %.0f): drag %.3f N, "
            "lift %.3f N, Cd %.3f, Cl %.3f. Validated solver — trust these as absolute coefficients.",
            st.chatCfdVel, st.chatCfdAoa, re, drag, lift, cd, cl);
        answer = b;
    }
    if (msg) {
        msg->running = false; msg->liveStatus.clear();
        if (!err.empty()) { msg->errKind = ERR_SYSTEM; msg->text = "The CFD sim failed: " + err; }
        else msg->text = answer;
    }
    st.status = err.empty() ? "Ready." : "CFD failed.";
    st.statusKind = err.empty() ? 0 : 2;
}

// Replace conversations/chat with the saved set for `key` (or a fresh empty chat if none).
static void loadChatHistoryForKey(AppState& st, const std::string& key) {
    nlohmann::json root = nlohmann::json::object();
    {
        std::ifstream in(chatHistoryPath());
        if (in) { try { in >> root; } catch (...) {} }
    }
    bool found = root.is_object() && root.contains(key) && root[key].is_object();
    if (found) {
        freeChatTextures(st.chat);
        st.chat.clear();
        st.conversations.clear();
        const nlohmann::json& e = root[key];
        for (const auto& jc : e.value("conversations", nlohmann::json::array())) {
            Conversation c;
            c.id = jc.value("id", newConvId(st));
            c.title = jc.value("title", std::string("Chat"));
            for (const auto& m : jc.value("msgs", nlohmann::json::array())) {
                ChatMsg cm;
                cm.fromUser = m.value("u", false);
                cm.text = m.value("t", std::string());
                cm.errKind = m.value("e", 0);
                cm.running = false;
                c.msgs.push_back(std::move(cm));
            }
            st.conversations.push_back(std::move(c));
        }
        if (st.conversations.empty()) { st.activeConv = -1; ensureConversations(st); return; }
        int act = e.value("active", 0);
        if (act < 0 || act >= (int)st.conversations.size()) act = 0;
        st.activeConv = act;
        st.chat = std::move(st.conversations[act].msgs);
        st.lastContextTokens = st.conversations[act].lastContextTokens;
        st.queuedPrompts = std::move(st.conversations[act].queuedPrompts);
        st.conversations[act].msgs.clear();
        st.conversations[act].queuedPrompts.clear();
    } else if (st.conversations.empty() && st.activeConv < 0) {
        ensureConversations(st);  // first init: adopt current st.chat (welcome) as conversation 1
    } else {
        freeChatTextures(st.chat);  // switching to a different doc with no history: fresh chat
        st.chat.clear();
        st.lastContextTokens = 0;
        st.queuedPrompts.clear();
        st.conversations.clear();
        st.activeConv = -1;
        ensureConversations(st);
    }
}

// Detect a document change and swap the chat history accordingly (called each frame).
static void syncChatToDoc(AppState& st) {
    std::string key = convDocKey(st);
    if (key == st.chatDocKey) { ensureConversations(st); return; }
    if (!st.chatDocKey.empty()) persistChatHistory(st);  // save the outgoing doc's conversations
    loadChatHistoryForKey(st, key);
    st.chatDocKey = key;
}

static void parkActiveConversation(AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    Conversation& c = st.conversations[st.activeConv];
    c.msgs = std::move(st.chat);
    c.lastContextTokens = st.lastContextTokens;
    c.queuedPrompts = std::move(st.queuedPrompts);
    st.chat.clear();
    st.lastContextTokens = 0;
    st.queuedPrompts.clear();
}

static void restoreActiveConversation(AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    Conversation& c = st.conversations[st.activeConv];
    st.chat = std::move(c.msgs);
    st.lastContextTokens = c.lastContextTokens;
    st.queuedPrompts = std::move(c.queuedPrompts);
    c.msgs.clear();
    c.queuedPrompts.clear();
}

static bool conversationRunning(const Conversation& c) {
    for (auto it = c.msgs.rbegin(); it != c.msgs.rend(); ++it)
        if (!it->fromUser && it->running) return true;
    return false;
}

static bool activeConversationRunning(AppState& st) {
    if (aiTurnActive(st)) return true;
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size())
        return conversationRunning(st.conversations[st.activeConv]);
    return false;
}

static void newConversation(AppState& st) {
    ensureConversations(st);
    parkActiveConversation(st);
    Conversation c;
    c.id = newConvId(st);
    c.title = "Chat " + std::to_string((int)st.conversations.size() + 1);
    st.conversations.push_back(std::move(c));
    st.activeConv = (int)st.conversations.size() - 1;
    st.showSessionsPicker = false;
    persistChatHistory(st);
}

static void switchConversation(AppState& st, int idx) {
    if (idx == st.activeConv || idx < 0 || idx >= (int)st.conversations.size()) return;
    parkActiveConversation(st);
    st.activeConv = idx;
    restoreActiveConversation(st);
    st.showSessionsPicker = false;
    persistChatHistory(st);
}

static void closeConversation(AppState& st, int idx) {
    if (idx < 0 || idx >= (int)st.conversations.size()) return;
    bool closingActive = (idx == st.activeConv);
    if (closingActive) {
        freeChatTextures(st.chat);
        st.chat.clear();
        st.lastContextTokens = 0;
        st.queuedPrompts.clear();
    } else {
        freeChatTextures(st.conversations[idx].msgs);
    }
    st.conversations.erase(st.conversations.begin() + idx);
    if (st.conversations.empty()) {
        st.activeConv = -1;
        ensureConversations(st);
        restoreActiveConversation(st);
        persistChatHistory(st);
        return;
    }
    if (closingActive) {
        int na = std::min(idx, (int)st.conversations.size() - 1);
        st.activeConv = na;
        restoreActiveConversation(st);
    } else if (idx < st.activeConv) {
        st.activeConv--;
    }
    persistChatHistory(st);
}

// (Conversations are switched from the Sessions picker in the Copilot header.)

// Probe the local OpenAI-compatible endpoint for its loaded models + reachability, so the
// picker can hide local models that are down. Throttled to ~5s; force bypasses the throttle.
void refreshModelAvailability(AppState& st, bool force) {
    // Discover models on the LOCAL endpoint regardless of which provider is currently active, so
    // local models always show up in the picker WITHOUT the user first switching the Provider
    // dropdown to "Local". Previously this early-returned whenever a cloud provider (OpenRouter /
    // DashScope / ...) was active — st.settings.baseUrl is then a remote URL — which is exactly why
    // local models were invisible until the user "poked around in settings" and selected Local.
    auto isLoopback = [](const char* u) {
        std::string s = u ? u : "";
        return s.find("localhost") != std::string::npos ||
               s.find("127.0.0.1") != std::string::npos ||
               s.find("0.0.0.0") != std::string::npos || s.find("[::1]") != std::string::npos;
    };
    // When the active base_url IS loopback, probe it (honors a custom local port). Otherwise a cloud
    // provider is active, so its base_url isn't the local one — fall back to the default LM Studio
    // endpoint so local discovery still happens. (A non-default local port is only auto-found while
    // the Local provider is the active one; the default covers the common case.)
    bool activeLoopback = isLoopback(st.settings.baseUrl);
    std::string localUrl = activeLoopback ? std::string(st.settings.baseUrl)
                                          : std::string("http://localhost:1234/v1");
    std::string localKey = activeLoopback ? std::string(st.settings.apiKey) : std::string();
    static double lastProbe = -1e9;
    double now = ImGui::GetTime();
    if (!force && (st.modelProbeInFlight.load() || (now - lastProbe) < 5.0)) return;
    lastProbe = now;
    st.modelProbeInFlight.store(true);
    nlohmann::json p = {{"provider", "openai"},
                        {"base_url", localUrl},
                        {"api_key", localKey}};
    st.rpc.call("agent_list_models", p, [&st](const core::RpcResult& r) {
        ProviderModels pm;
        pm.checked = true;
        if (r.ok && r.result.is_object()) {
            pm.ok = r.result.value("ok", false);
            pm.error = r.result.value("error", std::string());
            if (r.result.contains("models") && r.result["models"].is_array())
                for (const auto& m : r.result["models"])
                    if (m.is_string()) pm.models.push_back(m.get<std::string>());
            // LM Studio reports which models are actually loaded (vs merely downloaded); when
            // present, the picker shows only those. Absent => every listed model is available.
            pm.loadedKnown = r.result.value("loaded_known", false);
            if (r.result.contains("loaded") && r.result["loaded"].is_array())
                for (const auto& m : r.result["loaded"])
                    if (m.is_string()) pm.loaded.push_back(m.get<std::string>());
        } else {
            pm.error = r.err.empty() ? "core unavailable" : r.err;
        }
        st.providerModels[ui::PROV_LOCAL] = pm;
        st.providerModels[ui::PROV_LLAMACPP] = pm;  // same local endpoint family
        st.modelProbeInFlight.store(false);
    }, 12000);
}

// A row in the OpenRouter model list (curated preset or live-discovered free model).
struct OpenRouterModel {
    std::string id;
    std::string label;
    bool vision = false;
    bool free = false;
};
std::vector<OpenRouterModel> openRouterModels(AppState& st);  // fwd (defined just below)

// Probe OpenRouter's public /models catalog and cache the list of currently-FREE model ids (the
// endpoint exposes per-model pricing). Stored in providerModels[PROV_CLOUDCOMPAT] so the OpenRouter
// category can list every free model live instead of from a stale hardcoded set. Listing needs no
// key (the catalog is public); running a free model still needs the OpenRouter key. Throttled — the
// catalog changes slowly.
void refreshOpenRouterModels(AppState& st, bool force) {
    static double last = -1e9;
    double now = ImGui::GetTime();
    if (!force && (now - last) < 60.0) return;
    last = now;
    nlohmann::json p = {{"provider", "openai"},
                        {"base_url", "https://openrouter.ai/api/v1"},
                        {"api_key", st.settings.cloudKey}};
    st.rpc.call("agent_list_models", p, [&st](const core::RpcResult& r) {
        ProviderModels pm;
        pm.checked = true;
        if (r.ok && r.result.is_object()) {
            pm.ok = r.result.value("ok", false);
            if (r.result.contains("models") && r.result["models"].is_array())
                for (const auto& m : r.result["models"])
                    if (m.is_string()) pm.models.push_back(m.get<std::string>());
            if (r.result.contains("free") && r.result["free"].is_array())
                for (const auto& m : r.result["free"])
                    if (m.is_string()) pm.free.push_back(m.get<std::string>());
        } else {
            pm.error = r.err.empty() ? "core unavailable" : r.err;
        }
        st.providerModels[ui::PROV_CLOUDCOMPAT] = pm;
    }, 12000);
}

// The OpenRouter model list shown in the picker/manage UI: the curated presets PLUS every live
// free model the catalog probe found (deduped). `free`/`vision` flags drive the row status.
std::vector<OpenRouterModel> openRouterModels(AppState& st) {
    refreshOpenRouterModels(st, false);
    std::vector<OpenRouterModel> out;
    std::set<std::string> seen;
    for (const auto& p : ui::cloudPresets()) {
        if (std::strcmp(ui::cloudServiceKey(p.baseUrl), "openrouter") != 0) continue;
        out.push_back({p.id, p.label, p.vision,
                       p.id.find(":free") != std::string::npos});
        seen.insert(p.id);
    }
    for (const auto& id : st.providerModels[ui::PROV_CLOUDCOMPAT].free)
        if (seen.insert(id).second) out.push_back({id, id, false, true});
    return out;
}

// The active provider's API-key buffer (cloud providers keep separate keys; local reuses the
// optional apiKey buffer).
static char* providerKeyBuf(Settings& s, int provider) {
    switch (provider) {
        case ui::PROV_GEMINI: return s.geminiKey;
        case ui::PROV_ANTHROPIC: return s.anthropicKey;
        case ui::PROV_OPENAI: return s.openaiKey;
        case ui::PROV_CLOUDCOMPAT: return s.cloudKey;  // DashScope / DeepSeek / OpenAI-compatible
        default: return s.apiKey;  // local OpenAI-compatible optional key
    }
}

// Availability helpers (defined with the model-dropdown section below; forward-declared so the
// "Manage models" panel inside drawProviderModelPicker can call them).
static bool providerUsable(AppState& st, int provider);
static bool localModelLoaded(const ProviderModels& pm, const std::string& id);

// Shared provider + model picker. Returns true if a setting changed this frame.
bool drawProviderModelPicker(AppState& st, bool busy) {
    (void)busy;
    Settings& s = st.settings;
    const float S = ui::uiScale;
    bool changed = false;
    refreshModelAvailability(st, false);

    // ---- provider ----
    ImGui::TextUnformatted("Provider");
    ImGui::SetNextItemWidth(-1);
    const char* provs[ui::PROV_COUNT];
    for (int i = 0; i < ui::PROV_COUNT; ++i) provs[i] = ui::providerLabel(i);
    int prevProvider = s.provider;
    if (ImGui::Combo("##provider", &s.provider, provs, ui::PROV_COUNT) &&
        s.provider != prevProvider) {
        changed = true;
        // Set sensible per-provider defaults on switch.
        if (s.provider == ui::PROV_GEMINI && (!s.model[0] || std::strstr(s.model, "gpt") ||
                                              std::strstr(s.model, "claude")))
            std::strncpy(s.model, "gemini-2.5-pro", sizeof(s.model) - 1);
        else if (s.provider == ui::PROV_ANTHROPIC)
            std::strncpy(s.model, "claude-opus-4-8", sizeof(s.model) - 1);
        else if (s.provider == ui::PROV_OPENAI)
            std::strncpy(s.model, "gpt-4o", sizeof(s.model) - 1);
        else if (s.provider == ui::PROV_LOCAL) {
            std::strncpy(s.baseUrl, "http://localhost:1234/v1", sizeof(s.baseUrl) - 1);
            if (std::strstr(s.model, ".gguf") || std::strstr(s.model, "gemini") ||
                std::strstr(s.model, "claude"))
                std::strncpy(s.model, "gemma-4-e4b", sizeof(s.model) - 1);
            s.vision = true;
        } else if (s.provider == ui::PROV_LLAMACPP) {
            std::strncpy(s.baseUrl, "http://127.0.0.1:1234/v1", sizeof(s.baseUrl) - 1);
            if (!std::strstr(s.model, ".gguf"))
                std::strncpy(s.model, "%APPDATA%\\AI-CAD\\models\\model.gguf",
                             sizeof(s.model) - 1);
            s.vision = false;
        } else if (s.provider == ui::PROV_CLOUDCOMPAT) {
            // Default to Alibaba DashScope Qwen-Max; the preset dropdown can switch service+model.
            const auto& cp = ui::cloudPresets();
            if (!cp.empty()) {
                std::strncpy(s.model, cp[0].id.c_str(), sizeof(s.model) - 1);
                std::strncpy(s.baseUrl, cp[0].baseUrl.c_str(), sizeof(s.baseUrl) - 1);
                s.vision = cp[0].vision;
            }
        }
    }

    ImGui::Dummy(ImVec2(0, 4 * S));
    const bool cloud = ui::providerIsCloud(s.provider);

    // ---- model ----
    if (s.provider == ui::PROV_CLOUDCOMPAT) {
        // Cloud OpenAI-compatible API: a preset bundles the service base URL + model id, plus a
        // free-text model field. Credentials (base URL + key) are edited in the section below.
        ImGui::TextUnformatted("Service / model preset");
        dimText("Alibaba Cloud (DashScope) Qwen and the official DeepSeek API. These are CLOUD "
                "providers — Offline (ITAR) mode refuses them; use the local presets for ITAR.");
        const auto& cps = ui::cloudPresets();
        std::vector<const char*> plabels;
        plabels.push_back("(choose a preset…)");
        for (const auto& p : cps) plabels.push_back(p.label.c_str());
        int psel = 0;
        ImGui::SetNextItemWidth(-1);
        if (ImGui::Combo("##cloudpreset", &psel, plabels.data(), (int)plabels.size()) &&
            psel > 0 && psel - 1 < (int)cps.size()) {
            const auto& p = cps[psel - 1];
            std::strncpy(s.model, p.id.c_str(), sizeof(s.model) - 1);
            std::strncpy(s.baseUrl, p.baseUrl.c_str(), sizeof(s.baseUrl) - 1);
            s.vision = p.vision;
            changed = true;
        }
        if (psel > 0 && psel - 1 < (int)cps.size() && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", cps[psel - 1].note.c_str());
        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::TextUnformatted("Model id");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##cloudmodel", s.model, sizeof(s.model))) changed = true;
    } else if (cloud) {
        // Known-good model list for this provider, plus the current value if it's custom.
        std::vector<const char*> ids, labels;
        for (const auto& m : ui::builtinModels())
            if (m.provider == s.provider) { ids.push_back(m.id.c_str()); labels.push_back(m.label.c_str()); }
        int cur = -1;
        for (size_t i = 0; i < ids.size(); ++i)
            if (s.model[0] && std::strcmp(ids[i], s.model) == 0) cur = (int)i;
        std::string customLabel;
        if (cur < 0) {  // present whatever is configured as a trailing custom entry
            customLabel = std::string(s.model[0] ? s.model : "(choose a model)") + "  (custom)";
            labels.push_back(customLabel.c_str());
            cur = (int)labels.size() - 1;
        }
        ImGui::TextUnformatted("Model");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::Combo("##modelpick", &cur, labels.data(), (int)labels.size())) {
            if (cur >= 0 && cur < (int)ids.size()) {
                std::strncpy(s.model, ids[cur], sizeof(s.model) - 1);
                changed = true;
            }
        }
    } else {
        // Local: list discovered models when the endpoint is up; else a free-text field.
        const ProviderModels& pm = st.providerModels[s.provider == ui::PROV_LLAMACPP
                                                          ? ui::PROV_LLAMACPP : ui::PROV_LOCAL];
        bool useGguf = (s.provider == ui::PROV_LLAMACPP);
        // Experimental: one-click presets for the research-backed open-weight local models
        // (Qwen2.5-Coder / Qwen2.5-VL / DeepSeek-R1). Fills the model id + vision flag + a default
        // base URL; the weights must already be loaded in the local server.
        if (!useGguf) {
            ImGui::TextUnformatted("Quick preset");
            dimText("Open-weight, self-hostable models recommended for the copilot (ITAR-safe). "
                    "Fills the model id, vision flag, and a default base URL. The weights must "
                    "already be loaded in your local server.");
            const auto& presets = ui::localPresets();
            std::vector<const char*> plabels;
            plabels.push_back("(choose a preset…)");
            for (const auto& p : presets) plabels.push_back(p.label.c_str());
            int psel = 0;
            ImGui::SetNextItemWidth(-1);
            if (ImGui::Combo("##localpreset", &psel, plabels.data(), (int)plabels.size()) &&
                psel > 0 && psel - 1 < (int)presets.size()) {
                const auto& p = presets[psel - 1];
                std::strncpy(s.model, p.id.c_str(), sizeof(s.model) - 1);
                if (!p.baseUrl.empty())
                    std::strncpy(s.baseUrl, p.baseUrl.c_str(), sizeof(s.baseUrl) - 1);
                s.vision = p.vision;
                changed = true;
                refreshModelAvailability(st, true);  // re-probe for the (possibly new) endpoint
            }
            if (psel > 0 && psel - 1 < (int)presets.size() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", presets[psel - 1].note.c_str());
            ImGui::Dummy(ImVec2(0, 4 * S));
        }
        ImGui::TextUnformatted(useGguf ? "GGUF model path" : "Model");
        if (!useGguf && pm.ok && !pm.models.empty()) {
            std::vector<const char*> ids;
            for (const auto& id : pm.models) ids.push_back(id.c_str());
            int cur = -1;
            for (size_t i = 0; i < ids.size(); ++i)
                if (s.model[0] && std::strcmp(ids[i], s.model) == 0) cur = (int)i;
            std::string customLabel;
            if (cur < 0) { customLabel = std::string(s.model[0] ? s.model : "(select)") + "  (custom)";
                           ids.push_back(customLabel.c_str()); cur = (int)ids.size() - 1; }
            ImGui::SetNextItemWidth(-1);
            if (ImGui::Combo("##localmodel", &cur, ids.data(), (int)ids.size())) {
                if (cur >= 0 && (size_t)cur < pm.models.size()) {
                    std::strncpy(s.model, pm.models[cur].c_str(), sizeof(s.model) - 1);
                    changed = true;
                }
            }
        } else {
            ImGui::SetNextItemWidth(-1);
            if (ImGui::InputText("##localmodeltext", s.model, sizeof(s.model))) changed = true;
        }
    }

    // ---- reasoning effort (only for reasoning-capable models) ----
    {
        const char* effLabels[8] = {};
        const char* effValues[8] = {};
        int nEff = ui::reasoningEffortOptions(s.provider, s.model, effLabels, effValues, 8);
        if (nEff > 0) {
            ImGui::Dummy(ImVec2(0, 4 * S));
            ImGui::TextUnformatted("Reasoning effort");
            int effIdx = 0;
            for (int i = 0; i < nEff; ++i)
                if (std::strcmp(s.reasoningEffort, effValues[i]) == 0) effIdx = i;
            ImGui::SetNextItemWidth(-1);
            if (ImGui::Combo("##effort", &effIdx, effLabels, nEff)) {
                std::strncpy(s.reasoningEffort, effValues[effIdx], sizeof(s.reasoningEffort) - 1);
                changed = true;
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("How much hidden reasoning the model spends before answering.\n"
                                  "Higher = better planning on hard parts, slower + more tokens.");
        }
    }

    // ---- max response tokens (per model call) ----
    // The lever for "budget exhausted" / "no JSON ops in reply": a reasoning model can spend its
    // whole output budget thinking and emit no/truncated JSON. 0 = automatic per-provider default
    // (4096 local, 8192 cloud, +4096 at high effort). Raise it if builds keep failing to parse.
    ImGui::Dummy(ImVec2(0, 4 * S));
    ImGui::TextUnformatted("Max response tokens");
    ImGui::SetNextItemWidth(-1);
    int mt = s.maxTokens;  // 0 = auto
    if (ImGui::InputInt("##maxtokens", &mt, 1024, 4096)) {
        if (mt < 0) mt = 0;
        if (mt > 0 && mt < 512) mt = 512;
        if (mt > 200000) mt = 200000;
        s.maxTokens = mt;
        changed = true;
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Output token cap per model call. 0 = automatic.\n"
                          "Raise this if the agent reports \"budget exhausted\" or \"no JSON ops\"\n"
                          "— a reasoning model spent its budget thinking before emitting the build.");

    // ---- model context window ----
    // The model's TOTAL token window. The agent fits input (prompt) + output (above) inside it so
    // a small LOCAL model never silently runs out of context mid-chain. 0 = automatic per-provider
    // default (cloud large; local 8192). Set this to your local server's real window (e.g. 4096,
    // 8192, 32768) so the prompt is trimmed to fit instead of being truncated blindly by the server.
    ImGui::Dummy(ImVec2(0, 4 * S));
    ImGui::TextUnformatted("Model context window");
    ImGui::SetNextItemWidth(-1);
    int cw = s.contextWindow;  // 0 = auto
    if (ImGui::InputInt("##ctxwindow", &cw, 1024, 8192)) {
        if (cw < 0) cw = 0;
        if (cw > 0 && cw < 2048) cw = 2048;
        if (cw > 2000000) cw = 2000000;
        s.contextWindow = cw;
        changed = true;
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("The model's total token context window. 0 = automatic\n"
                          "(cloud large; local 8192). Set your LOCAL server's real window so the\n"
                          "agent trims the prompt to fit instead of overrunning it and breaking the run.");

    // ---- credentials / connection ----
    ImGui::Dummy(ImVec2(0, 6 * S));
    if (s.provider == ui::PROV_CLOUDCOMPAT) {
        // Cloud OpenAI-compatible: needs BOTH an endpoint (set by the preset, editable) and a key.
        ImGui::TextUnformatted("Endpoint (base URL)");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##cloudbase", s.baseUrl, sizeof(s.baseUrl))) changed = true;
        ImGui::TextUnformatted("API key");
        char* key = providerKeyBuf(s, s.provider);
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##provkey", key, 256, ImGuiInputTextFlags_Password)) changed = true;
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
        bool haveKey = key[0] != '\0';
        ImGui::PushStyleColor(ImGuiCol_Text, haveKey ? ui::Palette::toU32(ui::palette.statusOk)
                                                     : ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextUnformatted(haveKey ? "key set — cloud API available (blocked in Offline mode)"
                                       : "no API key — add your DashScope / DeepSeek key");
        ImGui::PopStyleColor();
    } else if (cloud) {
        ImGui::TextUnformatted("API key");
        char* key = providerKeyBuf(s, s.provider);
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##provkey", key, 256, ImGuiInputTextFlags_Password)) changed = true;
        bool haveKey = key[0] != '\0';
        ImGui::PushStyleColor(ImGuiCol_Text, haveKey ? ui::Palette::toU32(ui::palette.statusOk)
                                                     : ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextUnformatted(haveKey ? "key set — model available"
                                       : "no API key — add one to use this provider");
        ImGui::PopStyleColor();
    } else {
        ImGui::TextUnformatted("Base URL");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##baseurl", s.baseUrl, sizeof(s.baseUrl))) {
            changed = true;
            refreshModelAvailability(st, true);  // re-probe the new endpoint
        }
        if (s.provider == ui::PROV_LOCAL) {
            ImGui::TextUnformatted("API key (optional)");
            ImGui::SetNextItemWidth(-1);
            if (ImGui::InputText("##localkey", s.apiKey, sizeof(s.apiKey),
                                 ImGuiInputTextFlags_Password)) changed = true;
        }
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
        // Local availability status.
        const ProviderModels& pm = st.providerModels[s.provider == ui::PROV_LLAMACPP
                                                          ? ui::PROV_LLAMACPP : ui::PROV_LOCAL];
        const char* msg; ImU32 col;
        if (!pm.checked) { msg = "checking endpoint…"; col = IM_COL32(180, 180, 180, 255); }
        else if (pm.ok) { msg = "endpoint reachable"; col = ui::Palette::toU32(ui::palette.statusOk); }
        else { msg = "endpoint unreachable — start your local model server"; col = ui::Palette::toU32(ui::palette.statusErr); }
        ImGui::PushStyleColor(ImGuiCol_Text, col);
        ImGui::TextUnformatted(msg);
        ImGui::PopStyleColor();
    }

    // ---- manage which models appear in the chat picker (VS-Code-Copilot "Manage models") ----
    ImGui::Dummy(ImVec2(0, 6 * S));
    if (ImGui::CollapsingHeader("Manage models (chat picker)")) {
        dimText("Uncheck a model to hide it from the composer's model dropdown. Only available "
                "models (keyed cloud / loaded local) appear there; this just trims that list.");
        ImGui::Dummy(ImVec2(0, 2 * S));
        const auto& pal2 = ui::palette;
        int rowuid = 0;
        // One toggle row: [x] Label .................... status
        auto visRow = [&](const std::string& id, const std::string& label,
                          const char* status, ImU32 statusCol) {
            ImGui::PushID(rowuid++);
            bool show = (s.hiddenModels.count(id) == 0);
            if (ImGui::Checkbox("##vis", &show)) {
                if (show) s.hiddenModels.erase(id);
                else      s.hiddenModels.insert(id);
                changed = true;
            }
            ImGui::SameLine();
            ImGui::TextUnformatted(label.c_str());
            if (status && status[0]) {
                float tw = ImGui::CalcTextSize(status).x;
                ImGui::SameLine(ImGui::GetContentRegionMax().x - tw);
                ImGui::PushStyleColor(ImGuiCol_Text, statusCol);
                ImGui::TextUnformatted(status);
                ImGui::PopStyleColor();
            }
            ImGui::PopID();
        };
        const ImU32 okCol = ui::Palette::toU32(pal2.statusOk);
        const ImU32 dimCol = ui::Palette::toU32(pal2.textDim);
        const ImU32 warnCol = ui::Palette::toU32(pal2.statusWarn);
        dimText("Each provider is a category: the checkbox bulk-toggles the whole provider on/off; "
                "expand it to hide individual models. Off = hidden from the composer's model picker.");
        ImGui::Dummy(ImVec2(0, 2 * S));

        // One collapsible category header: [x] bulk-on/off checkbox + an expander showing the model
        // count. Returns whether the body (the model rows) should be drawn.
        auto catHeader = [&](const std::string& key, int n) -> bool {
            bool enabled = st.settings.disabledProviders.count(key) == 0;
            ImGui::PushID(("cat_" + key).c_str());
            if (ImGui::Checkbox("##en", &enabled)) {
                if (enabled) st.settings.disabledProviders.erase(key);
                else         st.settings.disabledProviders.insert(key);
                changed = true;
            }
            ImGui::PopID();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(enabled ? "Turn this whole provider OFF (hide all its models)"
                                          : "Turn this provider back ON");
            ImGui::SameLine();
            char hdr[128];
            std::snprintf(hdr, sizeof(hdr), "%s  (%d)%s###hdr_%s", ui::categoryLabel(key), n,
                          enabled ? "" : "   - off", key.c_str());
            return ImGui::CollapsingHeader(hdr);
        };
        auto catEnabled = [&](const std::string& key) {
            return st.settings.disabledProviders.count(key) == 0;
        };

        // Built-in cloud providers (Gemini / Anthropic / OpenAI).
        for (int pv : {ui::PROV_GEMINI, ui::PROV_ANTHROPIC, ui::PROV_OPENAI}) {
            std::string key = ui::providerCatKey(pv);
            int n = 0;
            for (const auto& m : ui::builtinModels()) if (m.provider == pv) ++n;
            if (catHeader(key, n)) {
                bool en = catEnabled(key);
                if (!en) ImGui::BeginDisabled();
                ImGui::Indent(14 * S);
                bool keyed = providerUsable(st, pv);
                for (const auto& m : ui::builtinModels()) {
                    if (m.provider != pv) continue;
                    visRow(m.id, m.label, keyed ? "available" : "needs key", keyed ? okCol : warnCol);
                }
                ImGui::Unindent(14 * S);
                if (!en) ImGui::EndDisabled();
            }
        }
        // Cloud services as their own categories. OpenRouter is discovered LIVE so its category
        // lists every currently-free model (plus curated paid presets); Alibaba/DeepSeek use presets.
        for (const char* svc : {"openrouter", "alibaba", "deepseek"}) {
            std::vector<OpenRouterModel> rows;
            if (std::strcmp(svc, "openrouter") == 0) {
                rows = openRouterModels(st);
            } else {
                for (const auto& p : ui::cloudPresets())
                    if (std::strcmp(ui::cloudServiceKey(p.baseUrl), svc) == 0)
                        rows.push_back({p.id, p.label, p.vision, false});
            }
            if (rows.empty()) continue;
            if (catHeader(svc, (int)rows.size())) {
                bool en = catEnabled(svc);
                if (!en) ImGui::BeginDisabled();
                ImGui::Indent(14 * S);
                if (std::strcmp(svc, "openrouter") == 0)
                    dimText("Free models discovered live from OpenRouter (cost $0; still need a key "
                            "to run). Paid presets are listed too.");
                for (const auto& r : rows)
                    visRow(r.id, r.label, r.free ? "free" : (r.vision ? "vision" : "paid"),
                           r.free ? okCol : dimCol);
                ImGui::Unindent(14 * S);
                if (!en) ImGui::EndDisabled();
            }
        }
        // Local discovered models (deduped across the local provider pair).
        {
            const ProviderModels& lpm = st.providerModels[ui::PROV_LOCAL];
            std::vector<std::string> ids;
            std::set<std::string> seen;
            for (const auto& id : lpm.models) if (seen.insert(id).second) ids.push_back(id);
            if (catHeader("local", (int)ids.size())) {
                bool en = catEnabled("local");
                if (!en) ImGui::BeginDisabled();
                ImGui::Indent(14 * S);
                if (ids.empty()) {
                    dimText("No local models discovered - start your local model server.");
                } else {
                    for (const auto& id : ids) {
                        bool loaded = localModelLoaded(lpm, id);
                        visRow(id, id,
                               lpm.loadedKnown ? (loaded ? "loaded" : "not loaded") : "available",
                               loaded ? okCol : dimCol);
                    }
                }
                ImGui::Unindent(14 * S);
                if (!en) ImGui::EndDisabled();
            }
        }
    }

    // ---- privacy / offline ----
    ImGui::Dummy(ImVec2(0, 6 * S));
    ImGui::SeparatorText("Privacy");
    if (ImGui::Checkbox("Offline mode (local models only — no network egress)", &s.offlineMode))
        changed = true;
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Refuses any cloud provider (Gemini, Anthropic, OpenAI). Only a local "
                          "loopback endpoint is allowed. Enforced before every run.");
    return changed;
}

// The AI settings popover body (opened from the composer's settings glyph): the shared
// provider/model/reasoning picker plus the common run-behavior toggles. No outer child — the
// popup window provides the frame.
static void drawAISettingsBody(AppState& st, bool busy) {
    Settings& settings = st.settings;

    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted("AI Settings");
    ImGui::PopFont();
    dimText("Provider, model, privacy, and run behavior.");
    ImGui::Separator();

    // Shared provider + availability-filtered model picker + reasoning effort (Settings modal
    // uses the same widget, so the two never diverge).
    ui::drawProviderModelPicker(st, busy);

    ImGui::SeparatorText("Behavior");
    {
        const char* rmItems[] = {"Supervised (plan + clarify before modeling)",
                                 "Autopilot (model straight from the prompt)"};
        int rm = settings.planMode ? 0 : 1;
        ImGui::TextUnformatted("Run mode");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::Combo("##runmodecombo", &rm, rmItems, 2)) settings.planMode = (rm == 0);
    }
    int units = st.ips() ? 1 : 0;
    if (ImGui::RadioButton("MMGS", &units, 0)) settings.units = Units::MMGS;
    ImGui::SameLine();
    if (ImGui::RadioButton("IPS", &units, 1)) settings.units = Units::IPS;

    bool light = (ui::activeTheme == ui::Theme::Light);
    if (ImGui::Checkbox("Light theme", &light)) {
        ui::Theme next = light ? ui::Theme::Light : ui::Theme::Dark;
        ui::applyTheme(next, st.scale);
        settings.theme = light ? "light" : "dark";
        if (st.renderer)
            st.renderer->setBackground(
                ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                ui::palette.viewportTop[2], ui::palette.viewportBottom[0],
                ui::palette.viewportBottom[1], ui::palette.viewportBottom[2]);
    }

    if (busy) ImGui::BeginDisabled();
    if (primaryButton("Save settings", ImVec2(-1, ui::metrics::copilotBtnH()))) {
        settings.systemPrompt = st.spBuf.data();
        saveSettings(settings);
        st.settings.recentFiles = st.recentFiles;
        st.addLog("[ok] saved settings");
        st.toast(ui::ToastLevel::Info, "Settings saved");
    }
    if (busy) ImGui::EndDisabled();
    if (ImGui::Button("Advanced settings...", ImVec2(-1, 0))) st.openSettings = true;
    if (ImGui::Button("Repair / install runtime", ImVec2(-1, 0)) &&
        !st.setupRunning.load() && !st.job.running.load()) {
        st.startSetup(true);
    }
}

// The Settings dock tab — a sibling of the "Copilot" window (docked next to it). Hosts the same AI
// settings body. SetNextWindowDockID pins it beside "Copilot" even on a saved layout; focusSettingsTab
// (set by the composer gear / model-dropdown gear) brings it forward.
void drawAISettingsTab(AppState& st, bool busy) {
    if (!st.showAIPanel) return;
    if (st.aiDockId) ImGui::SetNextWindowDockID((ImGuiID)st.aiDockId, ImGuiCond_FirstUseEver);
    if (st.focusSettingsTab) { ImGui::SetNextWindowFocus(); st.focusSettingsTab = false; }
    if (!ImGui::Begin("Settings###AISettings", nullptr, ImGuiWindowFlags_NoBackground)) {
        ImGui::End(); return;
    }
    const auto& pal = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##aisetbody", ImVec2(0, 0), ImGuiChildFlags_None);
    drawAISettingsBody(st, busy);
    // Transparency toggle (moved here from the composer per request): show/hide the AI's
    // reasoning ("Thoughts") blocks inline. File-static gShowThinking, default on (DESIGN §5).
    ImGui::Separator();
    ImGui::Checkbox("Show assistant thoughts", &gShowThinking);
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Show the AI's reasoning steps inline (like Claude Code verbose mode).");
    ImGui::EndChild();
    ImGui::PopStyleColor();
    ImGui::End();
}

// ============================ searchable model dropdown ============================
// A short provider tag shown as the dim sublabel next to each model (like the reference's
// "Copilot").
static const char* providerShort(int provider) {
    switch (provider) {
        case ui::PROV_ANTHROPIC: return "Claude";
        case ui::PROV_OPENAI:    return "OpenAI";
        case ui::PROV_LOCAL:     return "Local";
        case ui::PROV_LLAMACPP:  return "llama.cpp";
        case ui::PROV_GEMINI:    return "Gemini";
    }
    return "";
}

// A discovered local model is selectable only when the endpoint can actually serve it: when LM
// Studio reports loaded state we require the model to be loaded; otherwise (servers without that
// endpoint, e.g. vLLM/llama.cpp, which only list what they serve) every listed id is available.
static bool localModelLoaded(const ProviderModels& pm, const std::string& id) {
    if (!pm.loadedKnown) return true;
    return std::find(pm.loaded.begin(), pm.loaded.end(), id) != pm.loaded.end();
}

// Has the user hidden this model from the chat picker (Settings → AI → Manage models)?
static bool modelHidden(AppState& st, const std::string& id) {
    return st.settings.hiddenModels.count(id) != 0;
}

// Does this provider have credentials / a reachable endpoint, so its models can be selected?
static bool providerUsable(AppState& st, int provider) {
    switch (provider) {
        case ui::PROV_GEMINI:      return st.settings.geminiKey[0] != 0;
        case ui::PROV_ANTHROPIC:   return st.settings.anthropicKey[0] != 0;
        case ui::PROV_OPENAI:      return st.settings.openaiKey[0] != 0;
        case ui::PROV_CLOUDCOMPAT: return st.settings.cloudKey[0] != 0;
        case ui::PROV_LOCAL:       return st.providerModels[ui::PROV_LOCAL].ok;
        case ui::PROV_LLAMACPP:    return st.providerModels[ui::PROV_LLAMACPP].ok;
    }
    return false;
}

// One model row: checkmark gutter (drawn — no check glyph in the font) + label + dim provider
// sublabel; an accent "Add key" when the provider isn't usable. Returns true when picked.
// `uid` makes the ImGui id unique per row — model ids can repeat across providers/endpoints
// (the same local endpoint backs both PROV_LOCAL and PROV_LLAMACPP), and two rows sharing an
// id string would trip ImGui's "conflicting ID" assert.
static bool modelRow(AppState& st, int provider, const std::string& id, const std::string& label,
                     bool usable, int uid) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const float rowH = ImGui::GetFrameHeight();
    const float fh = ImGui::GetFontSize();
    const float gut = fh + 8 * sc;
    bool current = (st.settings.provider == provider &&
                    std::strcmp(st.settings.model, id.c_str()) == 0);
    ImGui::PushID(uid);
    bool clicked = ImGui::Selectable("##mrow", current,
                                     usable ? 0 : ImGuiSelectableFlags_Disabled, ImVec2(0, rowH));
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
    float midY = (mn.y + mx.y) * 0.5f;
    if (current) {
        float r = fh * 0.30f; ImVec2 c(mn.x + 5 * sc + r, midY);
        ImU32 col = ui::Palette::toU32(pal.accent); float th = ui::metrics::stroke(1.6f);
        dl->AddLine(ImVec2(c.x - r * 0.55f, c.y + r * 0.05f),
                    ImVec2(c.x - r * 0.10f, c.y + r * 0.50f), col, th);
        dl->AddLine(ImVec2(c.x - r * 0.10f, c.y + r * 0.50f),
                    ImVec2(c.x + r * 0.60f, c.y - r * 0.50f), col, th);
    }
    dl->AddText(ImVec2(mn.x + gut, midY - fh * 0.5f),
                usable ? ui::Palette::toU32(pal.text) : ui::Palette::toU32(pal.textDisabled),
                label.c_str());
    float lw = ImGui::CalcTextSize(label.c_str()).x;
    dl->AddText(ImVec2(mn.x + gut + lw + 8 * sc, midY - fh * 0.5f),
                ui::Palette::toU32(pal.textDim), providerShort(provider));
    if (!usable) {
        const char* hint = "Add key";
        float hw = ImGui::CalcTextSize(hint).x;
        dl->AddText(ImVec2(mx.x - hw - 8 * sc, midY - fh * 0.5f),
                    ui::Palette::toU32(pal.accent), hint);
    }
    ImGui::PopID();
    return clicked && usable;
}

// The model dropdown body (hosted in a popup): a search box, the usable models with a checkmark
// on the current one, an "Other Models" group for unkeyed providers, and a gear that opens the
// Settings tab. Picking a model sets provider+model and closes the popup.
static void drawModelDropdown(AppState& st, bool busy) {
    (void)busy;
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    static char s_modelSearch[64] = "";
    refreshModelAvailability(st, false);

    if (ImGui::IsWindowAppearing()) { s_modelSearch[0] = 0; ImGui::SetKeyboardFocusHere(); }
    ImGui::SetNextItemWidth(-1);
    ImGui::InputTextWithHint("##modelsearch", "Search models", s_modelSearch, sizeof(s_modelSearch));
    ImGui::Separator();

    auto lc = [](std::string s) {
        std::transform(s.begin(), s.end(), s.begin(),
                       [](unsigned char c) { return (char)std::tolower(c); });
        return s;
    };
    std::string q = lc(s_modelSearch);
    auto matchq = [&](const std::string& a, const std::string& b) {
        return q.empty() || lc(a).find(q) != std::string::npos || lc(b).find(q) != std::string::npos;
    };

    bool picked = false;
    int uid = 0;       // monotonic per-row id seed — keeps ImGui ids unique across sections
    int shown = 0;     // visible rows after availability + search filtering
    ImGui::BeginChild("##modellist", ImVec2(0, 240 * sc), ImGuiChildFlags_None);

    auto catOff = [&](const std::string& key) {
        return st.settings.disabledProviders.count(key) != 0;
    };

    // Usable built-in cloud models (provider has a key), minus any the user hid or whose provider
    // category is bulk-disabled.
    for (const auto& m : ui::builtinModels()) {
        if (catOff(ui::providerCatKey(m.provider))) continue;
        if (!providerUsable(st, m.provider) || !matchq(m.label, providerShort(m.provider))) continue;
        if (modelHidden(st, m.id)) continue;
        ++shown;
        if (modelRow(st, m.provider, m.id, m.label, true, uid++)) {
            st.settings.provider = m.provider;
            std::strncpy(st.settings.model, m.id.c_str(), sizeof(st.settings.model) - 1);
            picked = true;
        }
    }
    // Cloud OpenAI-compatible service presets (OpenRouter / Alibaba / DeepSeek). Shown when a cloud
    // key is configured and the service category isn't bulk-disabled. Picking one points the active
    // Cloud API provider at that service's endpoint + model.
    if (st.settings.cloudKey[0]) {
        // OpenRouter: the live list (all free models + curated paid), minus any the user hid.
        if (!catOff("openrouter")) {
            for (const auto& r : openRouterModels(st)) {
                if (modelHidden(st, r.id) || !matchq(r.label, "openrouter free")) continue;
                ++shown;
                if (modelRow(st, ui::PROV_CLOUDCOMPAT, r.id, r.label, true, uid++)) {
                    st.settings.provider = ui::PROV_CLOUDCOMPAT;
                    std::strncpy(st.settings.model, r.id.c_str(), sizeof(st.settings.model) - 1);
                    std::strncpy(st.settings.baseUrl, "https://openrouter.ai/api/v1",
                                 sizeof(st.settings.baseUrl) - 1);
                    st.settings.vision = r.vision;
                    picked = true;
                }
            }
        }
        // Alibaba / DeepSeek curated presets.
        for (const auto& p : ui::cloudPresets()) {
            std::string key = ui::cloudServiceKey(p.baseUrl);
            if (key == "openrouter") continue;  // handled above (live)
            if (catOff(key)) continue;
            if (modelHidden(st, p.id) || !matchq(p.label, key)) continue;
            ++shown;
            if (modelRow(st, ui::PROV_CLOUDCOMPAT, p.id, p.label, true, uid++)) {
                st.settings.provider = ui::PROV_CLOUDCOMPAT;
                std::strncpy(st.settings.model, p.id.c_str(), sizeof(st.settings.model) - 1);
                std::strncpy(st.settings.baseUrl, p.baseUrl.c_str(), sizeof(st.settings.baseUrl) - 1);
                st.settings.vision = p.vision;
                picked = true;
            }
        }
    }
    // Live-discovered local models (when a local endpoint is up). The probe assigns the same
    // model list to PROV_LOCAL and PROV_LLAMACPP (one endpoint family), so dedup by id to avoid
    // listing each model twice. Skip models that are downloaded but not loaded, and any the user
    // hid.
    std::set<std::string> seenLocal;
    for (int lp : {ui::PROV_LOCAL, ui::PROV_LLAMACPP}) {
        if (catOff("local")) break;
        const ProviderModels& pm = st.providerModels[lp];
        if (!pm.ok) continue;
        for (const auto& id : pm.models) {
            if (!seenLocal.insert(id).second) continue;     // already shown for the sibling provider
            if (!localModelLoaded(pm, id)) continue;        // downloaded but not loaded into memory
            if (modelHidden(st, id)) continue;
            if (!matchq(id, providerShort(lp))) continue;
            ++shown;
            if (modelRow(st, lp, id, id, true, uid++)) {
                st.settings.provider = lp;
                std::strncpy(st.settings.model, id.c_str(), sizeof(st.settings.model) - 1);
                // This pick may be switching AWAY from a cloud provider whose base_url is remote.
                // Point base_url back at the local endpoint so the run hits LM Studio, not the cloud
                // (a local model id sent to the cloud 403s). Preserve an already-loopback base_url so
                // a custom local port survives.
                std::string bu = st.settings.baseUrl ? st.settings.baseUrl : "";
                bool loop = bu.find("localhost") != std::string::npos ||
                            bu.find("127.0.0.1") != std::string::npos ||
                            bu.find("0.0.0.0") != std::string::npos ||
                            bu.find("[::1]") != std::string::npos;
                if (!loop)
                    std::strncpy(st.settings.baseUrl,
                                 lp == ui::PROV_LLAMACPP ? "http://127.0.0.1:1234/v1"
                                                         : "http://localhost:1234/v1",
                                 sizeof(st.settings.baseUrl) - 1);
                picked = true;
            }
        }
    }
    if (shown == 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDisabled));
        ImGui::TextWrapped(q.empty() ? "No models available — add a provider key or start a local "
                                       "model server (gear below)."
                                     : "No models match your search.");
        ImGui::PopStyleColor();
    }
    ImGui::EndChild();

    // Unavailable models (no key / endpoint down) are hidden from the list above; surface a
    // single muted hint pointing at Settings so they're still discoverable.
    bool anyUnavailable = false;
    for (const auto& m : ui::builtinModels())
        if (!providerUsable(st, m.provider)) { anyUnavailable = true; break; }
    if (anyUnavailable) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
        ImGui::TextUnformatted("Other providers need an API key \xE2\x80\x94 add one in Settings.");
        ImGui::PopStyleColor();
    }

    ImGui::Separator();
    IconUV gear = ui::getIcon("settings", 20);
    float gsz = ImGui::GetFrameHeight();
    ImGui::SetCursorPosX(ImGui::GetContentRegionMax().x - gsz);
    bool openSet = gear.valid
        ? ImGui::ImageButton("##mdlgear", gear.texId, ImVec2(gsz - 8 * sc, gsz - 8 * sc),
                             gear.uv0, gear.uv1)
        : ImGui::SmallButton("settings");
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("AI settings");
    if (openSet) { st.focusSettingsTab = true; ImGui::CloseCurrentPopup(); }
    if (picked) {
        // CRITICAL: persist the pick to config.json NOW. The in-app run (agent_run_v2) resolves its
        // provider from config.json in the C++ core — NOT from in-memory st.settings — so a pick that
        // only updates st.settings would leave the run using the STALE persisted provider. That's the
        // bug where selecting a local model (gemma) still routed to the previously-saved cloud
        // endpoint and 403'd. Saving here keeps config.json == what the picker shows before any run.
        saveSettings(st.settings);
        ImGui::CloseCurrentPopup();
    }
}

// ============================ run-mode dropdown ============================
// Our real run modes (driven by Settings::planMode): Supervised drafts a plan + asks the
// build-critical clarify questions before modeling; Autopilot models straight from the prompt.
static void drawRunModeMenu(AppState& st) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    struct Row { bool plan; const char* title; const char* desc; };
    static const Row rows[] = {
        {true,  "Supervised", "Drafts a plan and asks for key dimensions before modeling"},
        {false, "Autopilot",  "Models straight from your prompt \xE2\x80\x94 no plan or questions"},
    };
    const float lh = ImGui::GetTextLineHeight();
    for (const auto& r : rows) {
        bool cur = st.settings.planMode == r.plan;
        float h = lh * 2 + 8 * sc;
        ImGui::PushID(r.title);
        bool clicked = ImGui::Selectable("##rmrow", cur, 0, ImVec2(300 * sc, h));
        ImVec2 mn = ImGui::GetItemRectMin();
        float tx = mn.x + 8 * sc;
        if (st.fonts.medium)
            dl->AddText(st.fonts.medium, st.fonts.medium->FontSize, ImVec2(tx, mn.y + 4 * sc),
                        ui::Palette::toU32(pal.text), r.title);
        else
            dl->AddText(ImVec2(tx, mn.y + 4 * sc), ui::Palette::toU32(pal.text), r.title);
        dl->AddText(ImVec2(tx, mn.y + 4 * sc + lh), ui::Palette::toU32(pal.textDim), r.desc);
        ImGui::PopID();
        if (clicked) { st.settings.planMode = r.plan; saveSettings(st.settings); }
    }
}

// Launch the active turn for `text` according to the current mode (Design/Edit/Ask). Pushes
// the user + live-assistant ChatMsgs. Centralizes the three old launch paths so the top input
// and the per-turn follow-up input share one code path.
static void submitTurn(AppState& st, const std::string& text) {
    if (text.empty()) return;
    // Persist the active provider/model to config.json BEFORE launching any run. The C++ core
    // resolves the provider from config.json (not in-memory st.settings), so this guarantees the
    // run uses exactly what the picker shows — closing the window where a freshly-picked local
    // model still ran against a stale, previously-saved cloud endpoint (the 403 "model_failed").
    saveSettings(st.settings);
    { ChatMsg u; u.fromUser = true; u.text = text; st.chat.push_back(u); }
    { ChatMsg a; a.running = true; st.chat.push_back(a); }
    st.designStarted = true;   // conversation is now active (kept for shot scenarios)

    // Name the conversation tab after its first prompt; persist so the turn survives a restart.
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size()) {
        std::string& title = st.conversations[st.activeConv].title;
        bool isDefault = title.empty() || (title.rfind("Chat ", 0) == 0);
        if (isDefault) title = text.size() > 28 ? (text.substr(0, 27) + "...") : text;
    }
    ui::persistChatHistory(st);

    bool editMode = (st.aiInteraction == 1);
    bool askMode  = (st.aiInteraction == 2);

    std::string lo = text;
    std::transform(lo.begin(), lo.end(), lo.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    auto has = [&](const char* k) { return lo.find(k) != std::string::npos; };

    // MISSION intent: a request that asks to BUILD something AND then analyze/validate it
    // ("model an airplane, then run CFD and prove it's good"; "design a bracket and FEA it").
    // The old routing was mutually exclusive — a "...run a CFD..." phrase greedily hijacked the
    // whole message into the sim tool, which then refused on the empty doc. Route the combined
    // case to the mission orchestrator, which decomposes it into design -> analyze -> verdict.
    // Conservative on purpose: a miss just falls through to plain design (no regression).
    auto looksLikeMission = [&]() -> bool {
        if (editMode || askMode || st.agentV2Running) return false;
        bool buildVerb = has("make ") || has("model ") || has("build ") || has("design ") ||
                         has("create ") || has("generate ") || has("give me");
        bool analyzeVerb = has("cfd") || has("wind tunnel") || has("windtunnel") ||
                           has("aerodynam") || has("simulat") || has("flow sim") || has("fea") ||
                           has("stress") || has("downforce") ||
                           ((has("drag") || has("lift")) && (has("air") || has("speed") ||
                            has("wind") || has("m/s") || has("mph"))) ||
                           has("prove") || has("validate") || has("verify it") ||
                           has("is it a good") || has("good design") || has("analyze");
        return buildVerb && analyzeVerb;
    };
    if (looksLikeMission()) {
        launchMissionRunChat(st, text, /*needFreshDoc=*/true);
        return;
    }

    // CFD/sim intent: a flow-sim request on an open part routes to the simulation tool in ANY mode
    // (the AI runs a wind-tunnel CFD and reports drag/lift). Mirrors Ask — async, fills the chat,
    // never mutates geometry. Works fully offline (the core parses the flow condition itself).
    // This is the SIM-ONLY quick path (no build verb) — a combined build+analyze request was
    // already handled by looksLikeMission above.
    auto looksLikeSim = [&]() -> bool {
        if (!st.docMirror.hasDoc() || st.agentV2Running) return false;
        if (has("cfd") || has("wind tunnel") || has("windtunnel") || has("aerodynam") ||
            has("drag") || has("downforce"))
            return true;
        return (has("simulate") || has("simulation") || has("flow") || has("lift")) &&
               (has("air") || has("m/s") || has("mph") || has("km/h") || has("speed") ||
                has("velocity") || has("wind"));
    };
    if (looksLikeSim()) {
        st.addLog("[..] cfd: " + text);
        st.status = "Running CFD...";  st.statusKind = 0;
        nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"request", text}};
        // Accurate (OpenFOAM RANS) opt-in: when the user asks for an accurate/validated run AND the
        // worker reports OpenFOAM available, request solver=openfoam. The core exports an STL and
        // hands back {needs_openfoam, stl_path, velocity_ms, aoa_deg, fluid}; the app loop fulfils
        // it on the dedicated (non-blocking) SimClient channel — see pollChatOpenFoam in app.cpp.
        // (sim_run_flow must NOT go through st.rpc: WorkerClient::call serialises on callMtx_, so a
        // minutes-long solve there would freeze every other RPC.)
        const bool wantAccurate = has("accurate") || has("openfoam") || has("open foam") ||
                                  has("precise") || has("high-fidelity") || has("high fidelity") ||
                                  has("rans") || has("validated");
        bool ofAvail = false;
        if (wantAccurate && st.simClient) {
            nlohmann::json caps = st.simClient->caps();
            if (caps.is_object()) ofAvail = caps.value("openfoam", false);
        }
        if (wantAccurate && ofAvail) params["solver"] = "openfoam";
        if (wantAccurate && !ofAvail)
            st.addLog("[cfd] accurate requested but OpenFOAM not provisioned — using LBM estimate");

        st.rpc.call("agent_simulate_v1", params, [&st](const core::RpcResult& r) {
            ChatMsg* msg = nullptr;
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser && it->running) { msg = &*it; break; }
            std::string err;
            if (!r.ok) err = r.err.empty() ? "worker call failed" : r.err;
            else if (r.result.is_object() && r.result.contains("code"))
                err = r.result.value("message", std::string("worker error"));

            // Accurate hand-off: the core exported an STL; launch OpenFOAM on the SimClient channel
            // (non-blocking) and let the app loop post the result when the study finishes.
            if (err.empty() && r.result.value("needs_openfoam", false) && st.simClient) {
                const bool water = (r.result.value("fluid", std::string("air")) == "water");
                nlohmann::json fparams = {
                    {"stl_path", r.result.value("stl_path", std::string())},
                    {"direction", {1.0, 0.0, 0.0}},
                    {"speed_mps", r.result.value("velocity_ms", 10.0)},
                    {"aoa_deg", r.result.value("aoa_deg", 0.0)},
                    {"sideslip_deg", 0.0}, {"preset", "coarse"}, {"ref_area_mm2", 0.0},
                    {"fluid", {{"name", water ? "water" : "air"},
                               {"density_kgm3", water ? 1000.0 : 1.225},
                               {"kinematic_visc_m2s", water ? 1.0e-6 : 1.5e-5}}},
                    {"solver", "openfoam"},
                };
                std::string note;
                // Sentinel owner so SimManager::tick (which matches the real doc id) ignores this
                // job — the chat's own poll consumes it instead. No study/overlay side effects.
                if (st.simClient->launch("sim_run_flow", fparams, note, "__chat_cfd__")) {
                    st.chatCfdVel = r.result.value("velocity_ms", 10.0);
                    st.chatCfdAoa = r.result.value("aoa_deg", 0.0);
                    st.chatCfdPending = true;   // app loop polls SimClient and posts the result
                    if (msg) msg->liveStatus = "OpenFOAM RANS (a few minutes)...";
                    st.status = "Running OpenFOAM...";  st.statusKind = 0;
                    return;
                }
                err = note.empty() ? "could not launch OpenFOAM" : note;  // e.g. a study is running
            }

            if (msg) {
                msg->running = false;
                msg->liveStatus.clear();
                if (!err.empty()) { msg->errKind = ERR_SYSTEM; msg->text = "The CFD sim failed: " + err; }
                else {
                    msg->text = r.result.value("answer", std::string());
                    if (msg->text.empty()) msg->text = "(no result)";
                }
            }
            st.status = err.empty() ? "Ready." : "CFD failed.";
            st.statusKind = err.empty() ? 0 : 2;
        }, 600000);
        return;
    }

    if (editMode && st.docMirror.hasDoc()) {
        // Edit part -> orchestrator v2 (validated document ops).
        st.addLog("[..] editing: " + text);
        st.status = "Editing...";  st.statusKind = 0;
        st.agentV2Running = true;
        st.agentV2 = true;
        if (st.activityPanel)
            st.activityPanel->clearLive();
        launchAgentRunV2(st.worker, st.job, text, st.docMirror.docId(),
                         st.ribbonTab == AppState::RT_Manufacture);
    } else if (askMode) {
        // Ask -> Wave D: question answering about the open part via agent_ask_v2. The worker
        // answers from the doc tree without mutating anything (an empty doc_id is handled
        // there — the assistant falls back to general CAD usage). NOT a job: the call runs on
        // the AsyncRpc dispatch thread and the callback fills the chat on the GL thread.
        if (st.agentV2Running) {
            // A design/edit run is in flight — refuse politely instead of queueing behind it.
            if (!st.chat.empty() && st.chat.back().running && !st.chat.back().fromUser) {
                st.chat.back().running = false;
                st.chat.back().text = "A design/edit run is still in progress - "
                                      "ask me again when it finishes.";
            }
            return;
        }
        st.addLog("[..] ask: " + text);
        st.status = "Answering...";  st.statusKind = 0;
        nlohmann::json params = {
            {"doc_id", st.docMirror.hasDoc() ? st.docMirror.docId() : std::string()},
            {"question", text}};
        st.rpc.call("agent_ask_v2", params, [&st](const core::RpcResult& r) {
            // Find the last running assistant message (the one this turn pushed).
            ChatMsg* msg = nullptr;
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser && it->running) { msg = &*it; break; }
            std::string err;
            if (!r.ok)
                err = r.err.empty() ? "worker call failed" : r.err;
            else if (r.result.is_object() && r.result.contains("code"))
                err = r.result.value("message", std::string("worker error"));
            if (msg) {
                msg->running = false;
                msg->liveStatus.clear();
                if (!err.empty()) {
                    msg->errKind = ERR_SYSTEM;
                    msg->text = "I couldn't answer that: " + err;
                } else {
                    msg->text = r.result.value("answer", std::string());
                    if (msg->text.empty()) msg->text = "(no answer)";
                }
            }
            if (!err.empty()) {
                st.addLog("[err] ask: " + err);
                st.status = "Ask failed.";  st.statusKind = 2;
            } else {
                st.status = "Ready.";  st.statusKind = 0;
            }
        }, 300000);
    } else {
        // Design new -> pre-flight CLARIFY gate, THEN the v2 orchestrator on a FRESH part
        // document. The clarify call (agent_clarify_v1) runs async via AsyncRpc; its callback
        // (GL thread) either launches the run directly (request already specific) or shows the
        // inline clarify card with the build-critical questions. Any failure falls straight
        // through to the build — clarify never blocks. See launchDesignRun / drawClarifyCard.
        st.designStarted = true;
        // Autopilot (planMode off) skips the clarify gate and builds straight away.
        if (!st.settings.planMode) {
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser && it->running) { it->liveStatus = "Building..."; break; }
            launchDesignRun(st, text);
            return;
        }
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
            if (!it->fromUser && it->running) {
                it->clarify.awaiting = true;
                it->clarify.request = text;
                it->liveStatus = "Working out what I need to build this...";
                break;
            }
        st.addLog("[..] clarify: " + text);
        st.status = "Thinking...";  st.statusKind = 0;
        const std::string clarifyDocId = st.docMirror.hasDoc() ? st.docMirror.docId() : std::string();
        nlohmann::json params = {{"request", text}, {"doc_id", clarifyDocId}};
        st.rpc.call("agent_clarify_v1", params, [&st, text, clarifyDocId](const core::RpcResult& r) {
            ChatMsg* msg = nullptr;
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser && it->clarify.awaiting) { msg = &*it; break; }
            if (!msg) return;  // turn was cleared (New part) — nothing to do
            // Multi-document: the user switched to a different document while clarify was in flight.
            // launchDesignRun below would build against the NOW-active doc — the wrong one. Stop the
            // turn's spinner and let the user re-issue from the doc they meant, rather than corrupt it.
            if (st.docMirror.docId() != clarifyDocId) {
                msg->clarify.awaiting = false;
                msg->running = false;
                msg->liveStatus.clear();
                st.addLog("[..] clarify result for " + clarifyDocId + " dropped — active doc changed");
                return;
            }
            msg->clarify.awaiting = false;
            // Parse questions; on ANY error/empty -> build straight away (never block).
            bool ready = true;
            std::vector<ClarifyQ> qs;
            std::vector<std::string> assumptions;
            if (r.ok && r.result.is_object() && !r.result.contains("code")) {
                ready = r.result.value("ready", true);
                if (r.result.contains("questions") && r.result["questions"].is_array()) {
                    for (const auto& q : r.result["questions"]) {
                        if (!q.is_object()) continue;
                        ClarifyQ cq;
                        cq.key = q.value("key", std::string());
                        cq.ask = q.value("ask", std::string());
                        cq.unit = q.value("unit", std::string());
                        cq.suggested = q.value("suggested", std::string());
                        if (cq.ask.empty()) continue;
                        std::strncpy(cq.answer.data(), cq.suggested.c_str(),
                                     cq.answer.size() - 1);
                        qs.push_back(std::move(cq));
                    }
                }
                if (r.result.contains("assumptions") && r.result["assumptions"].is_array())
                    for (const auto& a : r.result["assumptions"])
                        if (a.is_string()) assumptions.push_back(a.get<std::string>());
            }
            if (ready || qs.empty()) {
                msg->liveStatus.clear();
                launchDesignRun(st, text);   // keeps msg->running -> the run streams into it
                return;
            }
            // Hold for the user: show the clarify card (msg stops "running" until Build it).
            msg->running = false;
            msg->liveStatus.clear();
            msg->clarify.active = true;
            msg->clarify.request = text;
            msg->clarify.questions = std::move(qs);
            msg->clarify.assumptions = std::move(assumptions);
            st.status = "Waiting on a few details...";  st.statusKind = 0;
            st.addLog("[..] clarify: " + std::to_string(msg->clarify.questions.size()) +
                      " question(s)");
        }, 120000);
    }
}

// ---- runtime-not-ready banner (kept from the old Design body) ----
static void drawRuntimeBanner(AppState& st) {
    if (st.workerStarted) return;
    // The in-process C++ core (libaicad) is a complete backend on its own (doc/agent/sim route
    // through it), so don't nag about the Python runtime when it's loaded.
    if (core::cpp_core().available()) return;
    const auto& pal = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(pal.setupWarnBg[0], pal.setupWarnBg[1], pal.setupWarnBg[2], pal.setupWarnBg[3]));
    ImGui::BeginChild("setup", ImVec2(0, 0), ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
    if (st.setupRunning.load()) {
        ImGui::TextColored(ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1),
                           "Setting up runtime...");
        dimText("First run installs the local CAD engine runtime (~once).");
    } else {
        ImGui::TextColored(ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1),
                           "Runtime not ready");
#ifdef _WIN32
        dimText("Run the packaged build (dist) or install.ps1 so the engine can self-install.");
#else
        dimText("Place libaicad.so next to the AI-CAD executable, or set AI_CAD_LIBAICAD.");
#endif
        if (ImGui::Button("Try setup now")) st.startSetup(false);
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
}

// Build a compact "Provider · model" label for the composer's model chip.
static void composerModelLabel(const Settings& s, char* out, size_t n) {
    const char* provNames[] = {"Anthropic", "OpenAI", "Local", "llama.cpp", "Gemini", "Cloud API"};
    const char* prov = provNames[(s.provider >= 0 && s.provider < ui::PROV_COUNT)
                                     ? s.provider : ui::PROV_GEMINI];
    std::snprintf(out, n, "%s \xc2\xb7 %s", prov, s.model[0] ? s.model : "default");
}

// Context-window meter for the composer prelude row: a small fill-bar + "N/M" readout (compact
// "ctx N/M" with no bar when the pane is too narrow). Drawn at the current cursor; advances it.
// Moved here from the footer chips, where it was pinned to the pane bottom against the status bar.
static void drawContextMeter(AppState& st, float roomForMeter) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const int   win  = effectiveContextWindow(st.settings);
    const int   used = st.lastContextTokens;
    const float frac = win > 0 ? std::min(1.0f, (float)used / (float)win) : 0.0f;
    const float barW = 40 * sc, gap = 5 * sc;
    const std::string full = fmtTokens(used) + "/" + fmtTokens(win);
    const std::string lean = "ctx " + full;       // standalone (no bar) keeps a "ctx" tag so it reads
    const float fh = ImGui::GetFrameHeight();
    // Bar + readout only when there is room for both; uses the SAME roomForMeter the caller passed to
    // measureContextMeter() so the width it reserved and the layout drawn here agree exactly.
    const float fullTw = ImGui::CalcTextSize(full.c_str()).x;
    const bool  showBar = roomForMeter >= barW + gap + fullTw;
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    if (showBar) {
        ImVec2 p = ImGui::GetCursorScreenPos();
        float by0 = p.y + fh * 0.5f - 3 * sc, by1 = p.y + fh * 0.5f + 3 * sc;
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImU32 fg = frac < 0.5f ? ui::Palette::toU32(pal.statusOk)
                 : frac < 0.85f ? ui::Palette::toU32(pal.statusWarn)
                                : ui::Palette::toU32(pal.statusErr);
        dl->AddRectFilled(ImVec2(p.x, by0), ImVec2(p.x + barW, by1),
                          ui::Palette::toU32(pal.accentSoft), 2 * sc);
        if (frac > 0)
            dl->AddRectFilled(ImVec2(p.x, by0), ImVec2(p.x + barW * frac, by1), fg, 2 * sc);
        dl->AddRect(ImVec2(p.x, by0), ImVec2(p.x + barW, by1),
                    ImGui::ColorConvertFloat4ToU32(surf.textSecondary), 2 * sc);
        ImGui::Dummy(ImVec2(barW, fh));
        ImGui::SameLine(0, gap);
    }
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(showBar ? full.c_str() : lean.c_str());
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Context window usage — the last model call's input tokens vs the "
                          "active model's window (%d / %d).\nSet the window in AI settings if "
                          "your local model differs.", used, win);
}

// Width drawContextMeter() will occupy, so the caller can right-align it on its line above the
// composer. Mirrors the showBar decision in drawContextMeter() so reserved width == drawn width.
static float measureContextMeter(AppState& st, float roomForMeter) {
    const float sc = ui::uiScale;
    const float barW = 40 * sc, gap = 5 * sc;
    const std::string full = fmtTokens(st.lastContextTokens) + "/" +
                             fmtTokens(effectiveContextWindow(st.settings));
    const float fullTw = ImGui::CalcTextSize(full.c_str()).x;
    if (roomForMeter >= barW + gap + fullTw) return barW + gap + fullTw;
    return ImGui::CalcTextSize(("ctx " + full).c_str()).x;
}

// Prelude row above the prompt: Design + Permissions on the left, context meter on the right.
static void drawComposerPrelude(AppState& st, bool /*busy*/) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const bool hasDoc = st.docMirror.hasDoc();
    const ui::AdaptiveSurface& surf = chatSurf();

    ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float rowH = ImGui::GetFrameHeight() + 4 * sc;
    const float availW = ImGui::GetContentRegionAvail().x;
    ImVec2 rowMax(rowMin.x + availW, rowMin.y + rowH);
    ui::glassRowBackground(ImGui::GetWindowDrawList(), rowMin, rowMax, 4.0f * sc, false, false);

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8 * sc, 3 * sc));
    // Translucent chips over the glass composer (opaque fills fought the adaptive text).
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
        ImVec4(pal.buttonHovered[0], pal.buttonHovered[1], pal.buttonHovered[2], 0.55f));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,
        ImVec4(pal.buttonActive[0], pal.buttonActive[1], pal.buttonActive[2], 0.60f));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);

    const char* modes[] = {"Design", "Edit", "Ask"};
    int mode = (st.aiInteraction >= 0 && st.aiInteraction <= 2) ? st.aiInteraction : 0;
    if (ImGui::SmallButton(modes[mode])) ImGui::OpenPopup("##cmpmode");
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Agent mode — what the AI does with your message.");
    if (ImGui::BeginPopup("##cmpmode")) {
        if (ImGui::Selectable("Design  — model a new part from a description", mode == 0))
            st.aiInteraction = 0;
        if (!hasDoc) ImGui::BeginDisabled();
        if (ImGui::Selectable("Edit  — change the open part", mode == 1)) st.aiInteraction = 1;
        if (!hasDoc) ImGui::EndDisabled();
        if (ImGui::Selectable("Ask  — questions about the part, no changes", mode == 2))
            st.aiInteraction = 2;
        ImGui::EndPopup();
    }

    ImGui::SameLine();
    const char* permLabel = st.settings.offlineMode ? "Permissions: Offline" : "Permissions";
    if (ImGui::SmallButton(permLabel)) ImGui::OpenPopup("##cmpperm");
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Privacy and run permissions for the copilot.");
    if (ImGui::BeginPopup("##cmpperm")) {
        bool off = st.settings.offlineMode;
        if (ImGui::Checkbox("Offline mode (local models only — ITAR-safe)", &off))
            st.settings.offlineMode = off;
        bool sup = st.settings.planMode;
        if (ImGui::Checkbox("Supervised (plan + clarify before modeling)", &sup))
            st.settings.planMode = sup;
        ImGui::Checkbox("Show assistant thoughts", &gShowThinking);
        ImGui::EndPopup();
    }

    const float room = ImGui::GetContentRegionAvail().x;
    const float w    = measureContextMeter(st, room);
    const float x    = ImGui::GetContentRegionMax().x - w;
    if (x > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(x);
    drawContextMeter(st, room);

    ImGui::PopStyleColor(4);
    ImGui::PopStyleVar();
    ImGui::Dummy(ImVec2(0, 4 * sc));
}

// Toolbar row below the prompt: model picker + thinking effort ... send/stop.
static void drawComposerToolbar(AppState& st, bool busy, bool canStart, bool active,
                                bool& submit, bool& stopReq, bool embedded = false) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();

    ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float rowH = ImGui::GetFrameHeight() + 4 * sc;
    const float availW = ImGui::GetContentRegionAvail().x;
    ImVec2 rowMax(rowMin.x + availW, rowMin.y + rowH);
    ImDrawList* bgDl = ImGui::GetWindowDrawList();
    if (embedded) {
        bgDl->AddLine(ImVec2(rowMin.x + 8.0f * sc, rowMin.y),
                      ImVec2(rowMax.x - 8.0f * sc, rowMin.y),
                      IM_COL32(71, 82, 102, 170), ui::metrics::stroke(1.0f));
    } else {
        ui::glassPanel(bgDl, ImRect(rowMin, rowMax), ui::GlassLevel::Thin, 4.0f * sc, surf);
    }

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8 * sc, 4 * sc));
    // Translucent chips over the glass composer (opaque fills fought the adaptive text).
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
        ImVec4(pal.buttonHovered[0], pal.buttonHovered[1], pal.buttonHovered[2], 0.55f));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,
        ImVec4(pal.buttonActive[0], pal.buttonActive[1], pal.buttonActive[2], 0.60f));

    char modelLabel[160];
    composerModelLabel(st.settings, modelLabel, sizeof(modelLabel));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    if (ImGui::Button(modelLabel)) ImGui::OpenPopup("##modeldropdown");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Model — click to search and switch.");

    ImGui::SameLine();
    Settings& s = st.settings;
    const char* effLabels[8] = {};
    const char* effValues[8] = {};
    int nEff = ui::reasoningEffortOptions(s.provider, s.model, effLabels, effValues, 8);
    if (nEff > 0) {
        int effIdx = 0;
        for (int i = 0; i < nEff; ++i)
            if (std::strcmp(s.reasoningEffort, effValues[i]) == 0) effIdx = i;
        char effBtn[48];
        std::snprintf(effBtn, sizeof(effBtn), "Thinking: %s", effLabels[effIdx]);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        if (ImGui::Button(effBtn)) ImGui::OpenPopup("##cmpeffort");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Thinking effort for the selected model.");
        if (ImGui::BeginPopup("##cmpeffort")) {
            for (int i = 0; i < nEff; ++i) {
                if (ImGui::Selectable(effLabels[i], effIdx == i))
                    std::strncpy(s.reasoningEffort, effValues[i], sizeof(s.reasoningEffort) - 1);
            }
            ImGui::EndPopup();
        }
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::BeginDisabled();
        ImGui::Button("Thinking: n/a");
        ImGui::EndDisabled();
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("The selected model does not expose a thinking-effort control.");
    }

    const float sendSz = ImGui::GetFrameHeight();
    ImGui::SameLine();
    float rightX = ImGui::GetContentRegionMax().x - sendSz;
    if (rightX > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(rightX);

    if (active) {
        ImGui::PushStyleColor(ImGuiCol_Button, ui::Palette::toU32(pal.statusErr));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, IM_COL32(220, 100, 92, 255));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, IM_COL32(190, 80, 74, 255));
        if (ImGui::Button("##stop", ImVec2(sendSz, sendSz))) stopReq = true;
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(st.stopRequested ? "Stopping\xE2\x80\xA6 (finishing current step)"
                                               : "Stop the current run");
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
        ImVec2 c((mn.x + mx.x) * 0.5f, (mn.y + mx.y) * 0.5f);
        float h = sendSz * 0.18f;
        dl->AddRectFilled(ImVec2(c.x - h, c.y - h), ImVec2(c.x + h, c.y + h),
                          IM_COL32(255, 255, 255, 235), 1.5f * sc);
    } else {
        ImGui::PushStyleColor(ImGuiCol_Button,
            canStart ? ui::Palette::toU32(pal.accent) : ui::Palette::toU32(pal.button));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
            canStart ? ui::Palette::toU32(pal.accentDim) : ui::Palette::toU32(pal.buttonHovered));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, ui::Palette::toU32(pal.accentDim));
        if (ImGui::Button("##send", ImVec2(sendSz, sendSz)) && canStart) submit = true;
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(canStart ? "Send  (Enter)"
                              : !canStart ? "Runtime/worker not ready yet." : "Type a message first.");
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
        ImVec2 c((mn.x + mx.x) * 0.5f, (mn.y + mx.y) * 0.5f);
        float t = sendSz * 0.22f;
        ImU32 ac = canStart ? IM_COL32(8, 26, 26, 255) : ui::Palette::toU32(pal.textDim);
        dl->AddTriangleFilled(ImVec2(c.x, c.y - t), ImVec2(c.x - t, c.y + t * 0.55f),
                              ImVec2(c.x + t, c.y + t * 0.55f), ac);
        dl->AddRectFilled(ImVec2(c.x - t * 0.30f, c.y - t * 0.1f),
                          ImVec2(c.x + t * 0.30f, c.y + t * 0.9f), ac);
    }
    ImGui::PopStyleColor(3);

    ImGui::PopStyleColor(3);   // button base / hovered / active
    ImGui::PopStyleVar();

    ImGui::SetNextWindowSizeConstraints(ImVec2(300 * sc, 0), ImVec2(300 * sc, FLT_MAX));
    if (ImGui::BeginPopup("##modeldropdown")) { drawModelDropdown(st, busy); ImGui::EndPopup(); }
}

// Is the assistant currently working a turn? Covers every run path: job-based design/edit, the
// async ask/sim calls (which set the back message running without a job), and any streaming
// message. Drives the composer's send<->stop swap and the message queue.
static bool aiTurnActive(AppState& st) {
    if (st.job.running.load() || st.agentV2Running) return true;
    if (!st.chat.empty()) {
        const ChatMsg& b = st.chat.back();
        if (!b.fromUser && b.running) return true;
    }
    return false;
}

// Stop the in-flight run. The worker RPC can't be interrupted mid-flight (one blocking call per
// WorkerClient, serialized), so this is a SOFT stop: finalize the active message as stopped and,
// for a job-based run, flag the drain to discard the result when the background thread lands. The
// model may finish its current step in the background; we just stop listening.
static void requestStopRun(AppState& st) {
    // Only a job-based run produces a result the drain must discard. Async ask/sim runs land via
    // their own callbacks, which look for a *running* message — clearing it below makes them no-op.
    if (st.job.running.load() || st.job.ready.load()) st.stopRequested = true;
    st.agentV2Running = false;
    st.livePhase.clear();
    st.agentV2Status.clear();
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (!it->fromUser && it->running) {
            it->running = false;
            it->liveStatus.clear();
            it->errKind = ERR_CAD;  // a user stop is not an app error
            if (it->text.empty())
                it->text = "Stopped. The model may finish its current step in the background.";
            else
                it->text += "\n\n(Stopped by you.)";
            break;
        }
    st.status = st.job.running.load() ? "Stopping..." : "Stopped.";
    st.statusKind = 1;
    st.addLog("[..] stop requested by user");
}

// ---- the composer: a rounded input box + inline toolbar (+, model chip, settings, send) ----
// Submits on Enter or the send button. Returns nothing; runs the turn directly.
static void drawComposer(AppState& st, bool busy, bool canRun) {
    const float sc = ui::uiScale;
    const bool hasDoc = st.docMirror.hasDoc();

    // Keep the orchestrator flag synced to the mode (Edit == validated document ops on the doc).
    if (st.aiInteraction == 1 && !hasDoc) st.aiInteraction = 0;
    st.agentV2 = (st.aiInteraction == 1);

    // Drain the message queue: once the assistant is free, auto-send the next queued message
    // (Claude-Code-style). Done before computing `active` so the new turn shows as running now.
    if (!aiTurnActive(st) && !st.queuedPrompts.empty() && canRun) {
        std::string next = st.queuedPrompts.front();
        st.queuedPrompts.erase(st.queuedPrompts.begin());
        submitTurn(st, next);
    }

    const bool active = aiTurnActive(st);
    bool canStart = canRun && st.prompt[0] && !active;  // start now only when idle
    bool submit   = false;   // send / queue requested this frame
    bool stopReq  = false;   // stop requested this frame

    // ---- queued messages (shown above the composer, each removable) ----
    if (!st.queuedPrompts.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
        for (size_t qi = 0; qi < st.queuedPrompts.size(); ++qi) {
            ImGui::PushID((int)qi + 9000);
            float xBtn = ImGui::GetFrameHeight();
            std::string& q = st.queuedPrompts[qi];
            std::string label = "Queued: " + (q.size() > 48 ? q.substr(0, 47) + "\xE2\x80\xA6" : q);
            ImGui::TextUnformatted(label.c_str());
            ImGui::SameLine(ImGui::GetContentRegionMax().x - xBtn);
            if (ImGui::SmallButton("x")) {
                st.queuedPrompts.erase(st.queuedPrompts.begin() + qi);
                ImGui::PopID();
                break;
            }
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Remove from queue");
            ImGui::PopID();
        }
        ImGui::PopStyleColor();
    }

    // ---- composer: prelude row -> transparent input -> toolbar row ----
    drawComposerPrelude(st, busy);

    const char* placeholder = st.aiInteraction == 2 ? "Ask about this part\xE2\x80\xA6"
                            : st.aiInteraction == 1 ? "Describe a change to this part\xE2\x80\xA6"
                                                    : "Describe what to build\xE2\x80\xA6";

    ImDrawList* composerDl = ImGui::GetWindowDrawList();
    ImDrawListSplitter composerSplit;
    const ImVec2 composerMin = ImGui::GetCursorScreenPos();
    const float composerW = ImGui::GetContentRegionAvail().x;
    composerSplit.Split(composerDl, 2);
    composerSplit.SetCurrentChannel(composerDl, 1);

    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 0.0f));
    ImGui::PushStyleColor(ImGuiCol_FrameBg, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::PushStyleColor(ImGuiCol_TextDisabled, surf.textTertiary);
    ImGui::PushStyleColor(ImGuiCol_Border, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 0.0f);
    bool submitInput = drawChatInput("##aiprompt", st.prompt, placeholder,
                                     ui::metrics::copilotInputMaxLines());
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(6);

    drawComposerToolbar(st, busy, canStart, active, submit, stopReq, true);
    ImGui::PopStyleVar();

    const ImVec2 composerMax(composerMin.x + composerW, ImGui::GetItemRectMax().y);
    composerSplit.SetCurrentChannel(composerDl, 0);
    ui::glassPanel(composerDl, ImRect(composerMin, composerMax), ui::GlassLevel::Thin,
                  ui::metrics::rounding(8.0f), surf);
    composerSplit.Merge(composerDl);

    // ---- stop / submit / queue ----
    if (stopReq) requestStopRun(st);
    if (submit || submitInput) {
        if (active) {
            // A turn is running — queue the message instead of starting concurrently. It auto-
            // sends when the assistant frees up (drain at the top of drawComposer).
            if (st.prompt[0]) {
                st.queuedPrompts.emplace_back(st.prompt.data());
                st.prompt[0] = 0;
            }
        } else if (canStart) {
            submitTurn(st, std::string(st.prompt.data()));
            st.prompt[0] = 0;
        }
    }
}

// ---- unified AI Copilot pane: header -> conversation -> bottom composer ----
void drawAIPanel(AppState& st, bool busy, bool canRun) {
    if (!st.showAIPanel) return;

    // Keep the local-endpoint model list fresh whenever the copilot panel exists — BEFORE the
    // visibility gate below, so discovery runs even when the AI tab is docked in the background
    // (ImGui::Begin returns false for an inactive tab). Without this, a model loaded in LM Studio
    // isn't discovered until the user opens the picker ("pokes around in settings"). The probe is
    // throttled to ~5s and no-ops for a non-loopback (cloud) base_url, so this is cheap.
    refreshModelAvailability(st, false);

    if (!ImGui::Begin("Copilot###AI", nullptr, ImGuiWindowFlags_NoBackground)) { ImGui::End(); return; }
    ui::Fonts& fonts = st.fonts;

    // Liquid-Glass v2 host port (contract H5): ONE UltraThin glass surface for the whole Copilot
    // pane, painted once here at panel top (id "host_agent_glass" via ui::panelId — window-
    // independent, so chatSurf() below returns the SAME cached surface for the header, composer,
    // and every card this frame; ui::adaptivePanel is idempotent per frame). Falls back to the
    // palette's glassBackground when no viewport backdrop has been published yet.
    {
        ImVec2 wmin = ImGui::GetWindowPos();
        ImVec2 wmax(wmin.x + ImGui::GetWindowSize().x, wmin.y + ImGui::GetWindowSize().y);
        ui::glassPanel(ImGui::GetWindowDrawList(), ImRect(wmin, wmax), ui::GlassLevel::UltraThin,
                      -1.0f, chatSurf());
    }

    // Remember which dock node we live in so the sibling "Settings" tab can dock next to us.
    st.aiDockId = (unsigned int)ImGui::GetWindowDockID();

    // Keep the conversation set bound to the active document (swaps history on doc change). Must
    // run BEFORE the header/composer touch st.conversations, so the seeded/persisted chat is
    // adopted rather than cleared.
    syncChatToDoc(st);

    ImGui::Dummy(ImVec2(0, ui::metrics::U(1.5f)));
    drawCopilotHeader(st, busy);
    ImGui::Separator();
    drawRuntimeBanner(st);

    // Reserve the composer's height (measured last frame) so the conversation log fills the rest.
    static float s_composerH = 0.0f;
    const float bottomClearance = ui::metrics::U(5);
    float reserve = (s_composerH > 0 ? s_composerH
                                     : ImGui::GetFrameHeightWithSpacing() * 4.0f) +
                    ui::metrics::U(2) + bottomClearance;
    float chatH = ImGui::GetContentRegionAvail().y - reserve;
    if (chatH < ui::metrics::px(120.0f)) chatH = ui::metrics::px(120.0f);

    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("chatlog", ImVec2(0, chatH), ImGuiChildFlags_None);
    if (st.showSessionsPicker) {
        drawSessionsPanel(st, busy);
    } else {
        const float logW = ImGui::GetContentRegionAvail().x;
        for (size_t mi = 0; mi < st.chat.size(); ++mi) {
            ChatMsg& m = st.chat[mi];
            ImGui::PushID((int)mi);
            if (m.fromUser) {
                drawUserBlock(m, fonts, logW);
                ImGui::PopID();
                continue;
            }
            drawAssistantTurn(st, m, fonts, logW);
            ImGui::Dummy(ImVec2(0, 8 * ui::uiScale));
            ImGui::PopID();
        }
        if (!st.showSessionsPicker && ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 12)
            ImGui::SetScrollHereY(1.0f);
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();

    // ---- composer (measure its height for next frame's reserve) ----
    ImGui::Dummy(ImVec2(0, ui::metrics::U(1)));
    ImGui::BeginGroup();
    drawComposer(st, busy, canRun);
    ImGui::EndGroup();
    s_composerH = ImGui::GetItemRectSize().y + ui::metrics::U(1);
    ImGui::Dummy(ImVec2(0, bottomClearance));

    ImGui::End();
}

}  // namespace ui
