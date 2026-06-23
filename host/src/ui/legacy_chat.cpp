// AI-CAD Copilot pane.
//
// One clean vertical pane: a slim header (logo + status + chat menu), the conversation log with a
// per-turn transparent activity timeline (what the AI is doing / thinking, expandable), and a
// bottom composer (rounded input + inline model/settings/send toolbar + provider/mode/autopilot
// chips). Settings live in a popup off the composer; there is no separate Activity panel anymore.
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

#include "legacy_chat.h"
#include "model_registry.h"  // ui::ModelInfo / capability flags / provider mapping
#include "metrics.h"          // ui::metrics::px/U/asmHeaderIconGap
#include "activity_panel.h"  // C22b: ActivityPanel still fed for the AI_CAD_SHOT=activity scenario
#include "console_panel.h"   // drawConsoleBody (shared console helper)
#include "icons.h"           // getIcon / IconUV — composer settings glyph

#include "../core/app_state.h"
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

// The build-plan card: an accent-edged panel listing each planned part with its purpose,
// committed dimensions (mono) and feature bullets — the agent's blueprint, shown before
// any code runs so the user can see (and steer) what is about to be built.
void drawPlanCard(const PlanInfo& plan, const ui::Fonts& fonts) {
    const auto& pal = ui::palette;
    const ImVec4 accent(pal.accent[0], pal.accent[1], pal.accent[2], 1);
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(pal.planCardBg[0], pal.planCardBg[1], pal.planCardBg[2], pal.planCardBg[3]));
    ImGui::BeginChild("##plancard", ImVec2(0, 0),
                      ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 w0 = ImGui::GetWindowPos();
    // Accent bar: use the same accent color with alpha from planAccentBar role.
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
        ImGui::ColorButton("##pdot", partColor((int)i),
                           ImGuiColorEditFlags_NoTooltip | ImGuiColorEditFlags_NoDragDrop,
                           ImVec2(10 * ui::uiScale, 10 * ui::uiScale));
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
    ImGui::EndChild();
    ImGui::PopStyleColor();
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
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(pal.stepCardBg[0], pal.stepCardBg[1], pal.stepCardBg[2], pal.stepCardBg[3]));
    ImGui::BeginChild("##stepcard", ImVec2(0, 0),
                      ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
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
    ImGui::EndChild();
    ImGui::PopStyleColor();
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
    launchAgentRunV2(st.worker, st.job, request, st.docMirror.docId());
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
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(pal.stepCardBg[0], pal.stepCardBg[1], pal.stepCardBg[2], pal.stepCardBg[3]));
    ImGui::BeginChild("##clarify", ImVec2(0, 0),
                      ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
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
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
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
    ImGui::EndChild();
    ImGui::PopStyleColor();
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

// The transparent activity timeline: one expandable row per thing the AI did/thought, with a
// status glyph (drawn with the draw list — check/arrow glyphs are not in the body font), the
// plain-language heading, optional verdict reason, and a click-to-expand body that reveals the
// raw reasoning/output behind the step. This is the primary "what the AI is doing" surface and
// replaces the old separate Activity panel + cramped embedded run tree.
static void drawActivityTimeline(ChatMsg& m, const ui::Fonts& fonts) {
    if (m.activity.empty()) return;
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    ImGui::Dummy(ImVec2(0, 2 * sc));
    for (size_t i = 0; i < m.activity.size(); ++i) {
        ActivityItem& a = m.activity[i];
        ImGui::PushID((int)i);
        const float lh = ImGui::GetTextLineHeight();
        const float gw = lh;                       // status-glyph box width
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        const ImVec2 c(p0.x + gw * 0.5f, p0.y + lh * 0.5f);
        const float r = lh * 0.30f;
        const bool running = a.status == "running";
        const bool pass    = a.status == "pass" || a.status == "done";
        const bool retry   = a.status == "retry";
        const bool fail    = a.status == "fail";
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
        ImGui::Dummy(ImVec2(gw, lh));
        ImGui::SameLine(0, 6 * sc);

        const bool hasDetail = !a.detail.empty();
        ImVec4 titleCol = running ? ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1)
                        : fail    ? ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1)
                        : pass    ? ImVec4(pal.text[0], pal.text[1], pal.text[2], 1)
                                  : ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1);
        ImGui::PushFont(fonts.medium);
        ImGui::TextColored(titleCol, "%s", a.title.c_str());
        ImGui::PopFont();
        if (hasDetail) {
            if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
            if (ImGui::IsItemClicked()) a.expanded = !a.expanded;
            ImGui::SameLine(0, 6 * sc);
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
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
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
            ImGui::TextUnformatted(meta);
            ImGui::PopStyleColor();
        }
        if (!a.reason.empty()) {
            ImGui::Indent(gw + 6 * sc);
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
            ImGui::TextWrapped("%s", a.reason.c_str());
            ImGui::PopStyleColor();
            ImGui::Unindent(gw + 6 * sc);
        }
        if (a.expanded && hasDetail) {
            ImGui::Indent(gw + 6 * sc);
            drawProseBox("##actdetail", stripMdMarks(a.detail), ui::metrics::copilotProseMaxH());
            ImGui::Unindent(gw + 6 * sc);
        }
        ImGui::Dummy(ImVec2(0, 3 * sc));
        ImGui::PopID();
    }
}

static void drawAssistantTurn(AppState& st, ChatMsg& m, const ui::Fonts& fonts, float logW) {
    const auto& pal = ui::palette;
    const ImVec4 accentCol(pal.accent[0], pal.accent[1], pal.accent[2], 1);

    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(ImVec4(pal.assistantName[0], pal.assistantName[1],
                             pal.assistantName[2], 1), "Assistant");
    ImGui::PopFont();
    if (m.plan.present) drawPlanCard(m.plan, fonts);

    // Transparent activity timeline — what the AI is doing / thinking, inline + expandable.
    drawActivityTimeline(m, fonts);

    if (!m.text.empty()) {
        if (m.errKind == ERR_SYSTEM)
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(pal.errSystem[0], pal.errSystem[1], pal.errSystem[2], 1));
        else if (m.errKind == ERR_CAD)
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(pal.errCad[0], pal.errCad[1], pal.errCad[2], 1));
        ImGui::TextWrapped("%s", stripMdMarks(m.text).c_str());
        if (m.errKind != ERR_NONE) ImGui::PopStyleColor();
    }

    // pre-flight clarify gate: the model's build-critical questions, awaiting the user.
    if (m.clarify.active) drawClarifyCard(st, m, fonts);

    // iteration trace — one expandable card per pass of the agent loop
    for (size_t si = 0; si < m.steps.size(); ++si) {
        ImGui::PushID((int)si);
        drawAgentStep(m.steps[si], fonts, logW);
        ImGui::PopID();
    }

    if (m.running) {
        // Inline live status: a simple braille spinner + the current phase, then a thin
        // accent progress bar so the pane reads as "working" at a glance. The transparent
        // activity timeline above already shows the per-stage detail.
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        static const char* kSpin[] = {"|", "/", "-", "\\"};
        int sp = (int)(ImGui::GetTime() * 8.0) & 3;
        ImGui::TextColored(accentCol, "%s", kSpin[sp]);
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.consoleDim[0], pal.consoleDim[1], pal.consoleDim[2], 1));
        ImGui::TextWrapped("%s", m.liveStatus.empty() ? "working..." : m.liveStatus.c_str());
        ImGui::PopStyleColor();
        // thin animated accent progress bar
        ImGui::PushStyleColor(ImGuiCol_PlotHistogram, ui::Palette::toU32(pal.accent));
        ImGui::PushStyleColor(ImGuiCol_FrameBg, ui::Palette::toU32(pal.accentSoft));
        float frac = 0.15f + 0.35f * (float)(0.5 + 0.5 * std::sin(ImGui::GetTime() * 2.2));
        ImGui::ProgressBar(frac, ImVec2(-1, 3.0f * ui::uiScale), "");
        ImGui::PopStyleColor(2);
    }
}

// ---- header: logo + "AI-CAD Copilot" + right cluster ([New chat] [Chats ▾] + status dot) ----
static void drawCopilotHeader(AppState& st, bool busy) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    // NOTE: do NOT ensureConversations() here — drawAIPanel calls syncChatToDoc() first, which
    // adopts the seeded/persisted chat. Forcing a conversation earlier flips that load into its
    // "fresh chat" branch and wipes the active turn.

    if (st.logoIcon.id) {
        float h = ImGui::GetTextLineHeight();
        ImGui::Image((ImTextureID)(intptr_t)st.logoIcon.id, ImVec2(h, h));
        ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
    }
    ImGui::PushFont(st.fonts.h2);
    ImGui::TextUnformatted("AI-CAD Copilot");
    ImGui::PopFont();

    // ---- right cluster ----
    const ImGuiStyle& style = ImGui::GetStyle();
    const float lh = ImGui::GetTextLineHeight();
    float newW   = ImGui::CalcTextSize("New chat").x + style.FramePadding.x * 2.0f;
    float chatsW = ImGui::CalcTextSize("Chats").x + style.FramePadding.x * 2.0f;
    float cluster = newW + style.ItemSpacing.x + chatsW + style.ItemSpacing.x + lh;
    ImGui::SameLine();
    float xRight = ImGui::GetContentRegionMax().x - cluster;
    if (xRight > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(xRight);

    if (busy) ImGui::BeginDisabled();
    if (ImGui::SmallButton("New chat")) { newConversation(st); st.prompt[0] = 0; }
    if (busy) ImGui::EndDisabled();
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Start a new conversation (past chats are kept).");

    ImGui::SameLine();
    bool openChats = ImGui::SmallButton("Chats");
    // The "Chats" button sits near the panel's right edge, so a popup anchored there grows off the
    // right of the window (hiding the trashcan column). Capture the button's bottom-right and pin
    // the popup's top-RIGHT corner to it so it opens leftward, fully on-screen.
    ImVec2 chatsAnchor = ImGui::GetItemRectMax();
    if (openChats) ImGui::OpenPopup("##chatspopup");

    // Render one chat row: a width-bounded Selectable (so long titles clip instead of overflowing)
    // plus a right-aligned trashcan delete button. Returns true if the chat was deleted, in which
    // case the caller must stop iterating (st.conversations changed).
    auto chatRow = [&](int i) -> bool {
        std::string title = st.conversations[i].title.empty()
                                ? ("Chat " + std::to_string(i + 1)) : st.conversations[i].title;
        ImGui::PushID(i);
        float trashW = ImGui::GetFrameHeight();
        float rowW = ImGui::GetContentRegionAvail().x;
        if (ImGui::Selectable(title.c_str(), i == st.activeConv,
                              ImGuiSelectableFlags_DontClosePopups,
                              ImVec2(rowW - trashW - 4 * sc, 0)) &&
            !busy)
            switchConversation(st, i);
        ImGui::SameLine(ImGui::GetContentRegionMax().x - trashW);
        IconUV trash = ui::getIcon("delete", 20);
        bool del;
        if (trash.valid) {
            ImGui::PushStyleColor(ImGuiCol_Button, IM_COL32(0, 0, 0, 0));  // frameless icon
            del = ImGui::ImageButton("##delconv", trash.texId,
                                     ImVec2(trashW - 8 * sc, trashW - 8 * sc),
                                     trash.uv0, trash.uv1);
            ImGui::PopStyleColor();
        } else {
            del = ImGui::SmallButton("x");
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Delete this chat");
        bool deleted = del && !busy;
        if (deleted) closeConversation(st, i);
        ImGui::PopID();
        return deleted;
    };

    // Pin the popup width. BeginPopup forces ImGuiWindowFlags_AlwaysAutoResize, which IGNORES
    // SetNextWindowSize — so the popup would auto-size to its "fill available" content and balloon
    // to viewport width. SizeConstraints ARE honored under auto-resize: clamp the width (height
    // auto). Anchoring the top-RIGHT to the button then keeps it on-screen, growing leftward.
    ImGui::SetNextWindowPos(chatsAnchor, ImGuiCond_Appearing, ImVec2(1.0f, 0.0f));  // grow left
    ImGui::SetNextWindowSizeConstraints(ImVec2(248 * sc, 0), ImVec2(248 * sc, FLT_MAX));
    if (ImGui::BeginPopup("##chatspopup")) {
        for (int i = 0; i < (int)st.conversations.size(); ++i) if (chatRow(i)) break;
        ImGui::Separator();
        if (ImGui::Selectable("+ New chat") && !busy) newConversation(st);
        ImGui::EndPopup();
    }

    // status dot (drawn — the ● glyph is not in the body font), accent=running / red=error / dim=idle
    ImGui::SameLine();
    const float* dot = pal.accentMid;  const char* tip = "Idle";
    if (st.statusKind == 2) { dot = pal.statusErr; tip = "Error"; }
    else if (busy || st.agentV2Running) { dot = pal.accent; tip = "Running"; }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 cp = ImGui::GetCursorScreenPos();
    dl->AddCircleFilled(ImVec2(cp.x + lh * 0.5f, cp.y + lh * 0.5f), lh * 0.30f,
                        ImGui::ColorConvertFloat4ToU32(ImVec4(dot[0], dot[1], dot[2], dot[3])));
    ImGui::Dummy(ImVec2(lh, lh));
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", tip);
}

// ============================ conversation tabs + persistence ============================
// Each conversation is its own tab. The ACTIVE conversation's messages live in st.chat; the
// rest park in st.conversations[i].msgs. Persisted per document to chat_history.json.

static std::string chatHistoryPath() { return appdataDir() + "\\AI-CAD\\chat_history.json"; }

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
        std::filesystem::create_directories(appdataDir() + "\\AI-CAD");
        std::ofstream out(chatHistoryPath());
        if (out) out << root.dump(1);
    } catch (...) {
    }
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
        st.conversations[act].msgs.clear();
    } else if (st.conversations.empty() && st.activeConv < 0) {
        ensureConversations(st);  // first init: adopt current st.chat (welcome) as conversation 1
    } else {
        freeChatTextures(st.chat);  // switching to a different doc with no history: fresh chat
        st.chat.clear();
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

static void newConversation(AppState& st) {
    ensureConversations(st);
    st.conversations[st.activeConv].msgs = std::move(st.chat);
    st.chat.clear();
    Conversation c;
    c.id = newConvId(st);
    c.title = "Chat " + std::to_string((int)st.conversations.size() + 1);
    st.conversations.push_back(std::move(c));
    st.activeConv = (int)st.conversations.size() - 1;
    persistChatHistory(st);
}

static void switchConversation(AppState& st, int idx) {
    if (idx == st.activeConv || idx < 0 || idx >= (int)st.conversations.size()) return;
    st.conversations[st.activeConv].msgs = std::move(st.chat);
    st.chat = std::move(st.conversations[idx].msgs);
    st.conversations[idx].msgs.clear();
    st.activeConv = idx;
}

static void closeConversation(AppState& st, int idx) {
    if (idx < 0 || idx >= (int)st.conversations.size()) return;
    bool closingActive = (idx == st.activeConv);
    if (closingActive) { freeChatTextures(st.chat); st.chat.clear(); }
    else { freeChatTextures(st.conversations[idx].msgs); }
    st.conversations.erase(st.conversations.begin() + idx);
    if (st.conversations.empty()) { st.activeConv = -1; ensureConversations(st); persistChatHistory(st); return; }
    if (closingActive) {
        int na = std::min(idx, (int)st.conversations.size() - 1);
        st.chat = std::move(st.conversations[na].msgs);
        st.conversations[na].msgs.clear();
        st.activeConv = na;
    } else if (idx < st.activeConv) {
        st.activeConv--;
    }
    persistChatHistory(st);
}

// (The horizontal conversation-tab strip was removed in the copilot overhaul — conversations are
//  now switched from the header's "Chats" dropdown; New chat lives in the header + composer.)

// Probe the local OpenAI-compatible endpoint for its loaded models + reachability, so the
// picker can hide local models that are down. Throttled to ~5s; force bypasses the throttle.
void refreshModelAvailability(AppState& st, bool force) {
    static double lastProbe = -1e9;
    double now = ImGui::GetTime();
    if (!force && (st.modelProbeInFlight.load() || (now - lastProbe) < 5.0)) return;
    lastProbe = now;
    st.modelProbeInFlight.store(true);
    nlohmann::json p = {{"provider", "openai"},
                        {"base_url", st.settings.baseUrl},
                        {"api_key", st.settings.apiKey}};
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
    if (ui::modelSupportsReasoning(s.provider, s.model)) {
        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::TextUnformatted("Reasoning effort");
        int nEff = 0; auto effVals = ui::reasoningEffortValues(&nEff);
        auto effLabels = ui::reasoningEffortLabels(nullptr);
        int eff = ui::reasoningEffortToIndex(s.reasoningEffort);
        ImGui::SetNextItemWidth(-1);
        if (ImGui::Combo("##effort", &eff, effLabels, nEff)) {
            std::strncpy(s.reasoningEffort, effVals[eff], sizeof(s.reasoningEffort) - 1);
            changed = true;
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("How much hidden reasoning the model spends before answering.\n"
                              "Higher = better planning on hard parts, slower + more tokens.");
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
        ImGui::PushStyleColor(ImGuiCol_Text, haveKey ? IM_COL32(120, 200, 120, 255)
                                                     : IM_COL32(220, 170, 90, 255));
        ImGui::TextUnformatted(haveKey ? "key set — cloud API available (blocked in Offline mode)"
                                       : "no API key — add your DashScope / DeepSeek key");
        ImGui::PopStyleColor();
    } else if (cloud) {
        ImGui::TextUnformatted("API key");
        char* key = providerKeyBuf(s, s.provider);
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##provkey", key, 256, ImGuiInputTextFlags_Password)) changed = true;
        bool haveKey = key[0] != '\0';
        ImGui::PushStyleColor(ImGuiCol_Text, haveKey ? IM_COL32(120, 200, 120, 255)
                                                     : IM_COL32(220, 170, 90, 255));
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
        else if (pm.ok) { msg = "endpoint reachable"; col = IM_COL32(120, 200, 120, 255); }
        else { msg = "endpoint unreachable — start your local model server"; col = IM_COL32(220, 120, 110, 255); }
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
        const ImU32 okCol = IM_COL32(120, 200, 120, 255);
        const ImU32 dimCol = ui::Palette::toU32(pal2.textDim);
        const ImU32 warnCol = IM_COL32(220, 170, 90, 255);

        // Built-in cloud models, grouped by provider.
        for (int pv : {ui::PROV_GEMINI, ui::PROV_ANTHROPIC, ui::PROV_OPENAI}) {
            bool header = false;
            for (const auto& m : ui::builtinModels()) {
                if (m.provider != pv) continue;
                if (!header) { ImGui::SeparatorText(ui::providerLabel(pv)); header = true; }
                bool keyed = providerUsable(st, pv);
                visRow(m.id, m.label, keyed ? "available" : "needs key", keyed ? okCol : warnCol);
            }
        }
        // Discovered local models (deduped across the local provider pair).
        const ProviderModels& lpm = st.providerModels[ui::PROV_LOCAL];
        if (!lpm.models.empty()) {
            ImGui::SeparatorText("Local endpoint");
            std::set<std::string> seen;
            for (const auto& id : lpm.models) {
                if (!seen.insert(id).second) continue;
                bool loaded = localModelLoaded(lpm, id);
                visRow(id, id, lpm.loadedKnown ? (loaded ? "loaded" : "not loaded") : "available",
                       loaded ? okCol : dimCol);
            }
        } else {
            ImGui::SeparatorText("Local endpoint");
            dimText("No local models discovered — start your local model server.");
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

// The Settings dock tab — a sibling of the "AI" window (docked next to it). Hosts the same AI
// settings body. SetNextWindowDockID pins it beside "AI" even on a saved layout; focusSettingsTab
// (set by the composer gear / model-dropdown gear) brings it forward.
void drawAISettingsTab(AppState& st, bool busy) {
    if (!st.showAIPanel) return;
    if (st.aiDockId) ImGui::SetNextWindowDockID((ImGuiID)st.aiDockId, ImGuiCond_FirstUseEver);
    if (st.focusSettingsTab) { ImGui::SetNextWindowFocus(); st.focusSettingsTab = false; }
    if (!ImGui::Begin("Settings###AISettings")) { ImGui::End(); return; }
    const auto& pal = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(pal.panelBg[0], pal.panelBg[1], pal.panelBg[2], 1));
    ImGui::BeginChild("##aisetbody", ImVec2(0, 0), ImGuiChildFlags_None);
    drawAISettingsBody(st, busy);
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

    // Usable built-in cloud models (provider has a key), minus any the user hid.
    for (const auto& m : ui::builtinModels()) {
        if (!providerUsable(st, m.provider) || !matchq(m.label, providerShort(m.provider))) continue;
        if (modelHidden(st, m.id)) continue;
        ++shown;
        if (modelRow(st, m.provider, m.id, m.label, true, uid++)) {
            st.settings.provider = m.provider;
            std::strncpy(st.settings.model, m.id.c_str(), sizeof(st.settings.model) - 1);
            picked = true;
        }
    }
    // Live-discovered local models (when a local endpoint is up). The probe assigns the same
    // model list to PROV_LOCAL and PROV_LLAMACPP (one endpoint family), so dedup by id to avoid
    // listing each model twice. Skip models that are downloaded but not loaded, and any the user
    // hid.
    std::set<std::string> seenLocal;
    for (int lp : {ui::PROV_LOCAL, ui::PROV_LLAMACPP}) {
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
    if (picked) ImGui::CloseCurrentPopup();
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
        st.rpc.call("agent_simulate_v1", params, [&st](const core::RpcResult& r) {
            ChatMsg* msg = nullptr;
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser && it->running) { msg = &*it; break; }
            std::string err;
            if (!r.ok) err = r.err.empty() ? "worker call failed" : r.err;
            else if (r.result.is_object() && r.result.contains("code"))
                err = r.result.value("message", std::string("worker error"));
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
        launchAgentRunV2(st.worker, st.job, text, st.docMirror.docId());
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
        dimText("Run the packaged build (dist) or install.ps1 so the engine can self-install.");
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

// ---- footer chips under the composer: provider · mode · autopilot (SolidWorks-clean pills) ----
static void drawComposerChips(AppState& st, bool busy) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const bool hasDoc = st.docMirror.hasDoc();
    ImGui::Dummy(ImVec2(0, 3 * sc));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8 * sc, 3 * sc));
    ImGui::PushStyleColor(ImGuiCol_Button, ui::Palette::toU32(pal.button));
    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));

    // provider / connection chip — opens the model picker
    const char* provNames[] = {"Anthropic", "OpenAI", "Local", "llama.cpp", "Gemini", "Cloud API"};
    int pv = (st.settings.provider >= 0 && st.settings.provider < ui::PROV_COUNT)
                 ? st.settings.provider : ui::PROV_GEMINI;
    bool isLocal = (pv == ui::PROV_LOCAL || pv == ui::PROV_LLAMACPP);
    if (ImGui::SmallButton(isLocal ? "Local" : provNames[pv])) ImGui::OpenPopup("##chipmodel");
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip(isLocal ? "Running on a local model — click to search/switch models."
                                  : "Cloud provider — click to search/switch models.");
    ImGui::SetNextWindowSizeConstraints(ImVec2(300 * sc, 0), ImVec2(300 * sc, FLT_MAX));
    if (ImGui::BeginPopup("##chipmodel")) { drawModelDropdown(st, busy); ImGui::EndPopup(); }

    // mode chip — Design / Edit / Ask
    ImGui::SameLine();
    const char* modes[] = {"Design", "Edit", "Ask"};
    int mode = (st.aiInteraction >= 0 && st.aiInteraction <= 2) ? st.aiInteraction : 0;
    if (ImGui::SmallButton(modes[mode])) ImGui::OpenPopup("##chipmode");
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("What the AI does with your message.");
    if (ImGui::BeginPopup("##chipmode")) {
        if (ImGui::Selectable("Design  — model a new part from a description", mode == 0))
            st.aiInteraction = 0;
        if (!hasDoc) ImGui::BeginDisabled();
        if (ImGui::Selectable("Edit  — change the open part", mode == 1)) st.aiInteraction = 1;
        if (!hasDoc) ImGui::EndDisabled();
        if (!hasDoc) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDisabled));
            ImGui::TextUnformatted("   (open a part to edit)");
            ImGui::PopStyleColor();
        }
        if (ImGui::Selectable("Ask  — questions about the part, no changes", mode == 2))
            st.aiInteraction = 2;
        ImGui::EndPopup();
    }

    // run-mode chip — Supervised / Autopilot (our real plan/clarify behavior)
    ImGui::SameLine();
    if (ImGui::SmallButton(st.settings.planMode ? "Supervised" : "Autopilot"))
        ImGui::OpenPopup("##runmode");
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Whether the AI plans and asks before modeling.");
    if (ImGui::BeginPopup("##runmode")) { drawRunModeMenu(st); ImGui::EndPopup(); }

    ImGui::PopStyleColor(2);
    ImGui::PopStyleVar();
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
    const auto& pal = ui::palette;
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
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
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

    // ---- rounded composer box ----
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ui::Palette::toU32(pal.frameBg));
    ImGui::PushStyleColor(ImGuiCol_Border, ui::Palette::toU32(pal.border));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, ui::metrics::rounding(10.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildBorderSize, ui::metrics::stroke(1.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 8 * sc));
    ImGui::BeginChild("##composer", ImVec2(0, 0),
                      ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);

    // input (transparent frame so it blends into the rounded box)
    const char* placeholder = st.aiInteraction == 2 ? "Ask about this part\xE2\x80\xA6"
                            : st.aiInteraction == 1 ? "Describe a change to this part\xE2\x80\xA6"
                                                    : "Describe what to build\xE2\x80\xA6";
    ImGui::PushStyleColor(ImGuiCol_FrameBg, ImVec4(0, 0, 0, 0));
    bool submitInput = drawChatInput("##aiprompt", st.prompt, placeholder,
                                     ui::metrics::copilotInputMaxLines());
    ImGui::PopStyleColor();

    // ---- inline toolbar row ----
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * sc, 3 * sc));
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0, 0, 0, 0));   // ghost buttons

    // + new chat
    if (ImGui::SmallButton("+")) { newConversation(st); st.prompt[0] = 0; }
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("New chat");

    // model chip
    ImGui::SameLine();
    char modelLabel[160];
    composerModelLabel(st.settings, modelLabel, sizeof(modelLabel));
    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
    if (ImGui::SmallButton(modelLabel)) ImGui::OpenPopup("##modeldropdown");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Model — click to search and switch.");

    // right cluster: [settings] [send]
    const float sendSz = ImGui::GetFrameHeight();
    const float gearSz = ImGui::GetFrameHeight();
    ImGui::SameLine();
    float rightX = ImGui::GetContentRegionMax().x - sendSz - 6 * sc - gearSz;
    if (rightX > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(rightX);

    // settings (gear glyph from the icon atlas; text fallback)
    IconUV gear = ui::getIcon("settings", 20);
    bool openSettings = false;
    if (gear.valid)
        openSettings = ImGui::ImageButton("##aigear", gear.texId,
                                          ImVec2(gearSz - 8 * sc, gearSz - 8 * sc),
                                          gear.uv0, gear.uv1);
    else
        openSettings = ImGui::SmallButton("set");
    if (openSettings) st.focusSettingsTab = true;   // bring the Settings dock tab forward
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("AI settings");

    // right action: STOP while a turn is active (red square), else SEND (accent up-arrow).
    // Arrow/square glyphs aren't in the body font, so both are drawn on the draw list.
    ImGui::SameLine(0, 6 * sc);
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
                              : !canRun ? "Runtime/worker not ready yet." : "Type a message first.");
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
    ImGui::PopStyleColor();   // ghost button base
    ImGui::PopStyleVar();     // toolbar frame padding

    // model dropdown popup (search + checkmarked list + gear)
    ImGui::SetNextWindowSizeConstraints(ImVec2(300 * sc, 0), ImVec2(300 * sc, FLT_MAX));
    if (ImGui::BeginPopup("##modeldropdown")) { drawModelDropdown(st, busy); ImGui::EndPopup(); }

    ImGui::EndChild();
    ImGui::PopStyleVar(3);   // child rounding / border / window padding
    ImGui::PopStyleColor(2); // child bg / border

    // ---- footer chips ----
    drawComposerChips(st, busy);

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
    if (!ImGui::Begin("AI")) { ImGui::End(); return; }
    ui::Fonts& fonts = st.fonts;
    const auto& pal = ui::palette;
    const ImVec4 accentCol(pal.accent[0], pal.accent[1], pal.accent[2], 1);

    // Remember which dock node we live in so the sibling "Settings" tab can dock next to us.
    st.aiDockId = (unsigned int)ImGui::GetWindowDockID();

    // Keep the conversation set bound to the active document (swaps history on doc change). Must
    // run BEFORE the header/composer touch st.conversations, so the seeded/persisted chat is
    // adopted rather than cleared.
    syncChatToDoc(st);

    drawCopilotHeader(st, busy);
    ImGui::Separator();
    drawRuntimeBanner(st);

    // Reserve the composer's height (measured last frame) so the conversation log fills the rest.
    static float s_composerH = 0.0f;
    float reserve = (s_composerH > 0 ? s_composerH
                                     : ImGui::GetFrameHeightWithSpacing() * 4.0f) + ui::metrics::U(2);
    float chatH = ImGui::GetContentRegionAvail().y - reserve;
    if (chatH < ui::metrics::px(120.0f)) chatH = ui::metrics::px(120.0f);

    // ---- conversation log (transparent — the cards carry their own backgrounds) ----
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("chatlog", ImVec2(0, chatH), ImGuiChildFlags_None);
    const float logW = ImGui::GetContentRegionAvail().x;

    for (size_t mi = 0; mi < st.chat.size(); ++mi) {
        ChatMsg& m = st.chat[mi];
        ImGui::PushID((int)mi);
        if (m.fromUser) {
            // user turn: a soft accent-tinted bubble
            ImGui::PushStyleColor(ImGuiCol_ChildBg,
                ImVec4(pal.userBubbleBg[0], pal.userBubbleBg[1],
                       pal.userBubbleBg[2], pal.userBubbleBg[3]));
            ImGui::PushStyleColor(ImGuiCol_Border,
                ImVec4(pal.userBubbleBorder[0], pal.userBubbleBorder[1],
                       pal.userBubbleBorder[2], pal.userBubbleBorder[3]));
            ImGui::BeginChild("##umsg", ImVec2(0, 0),
                              ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
            ImGui::PushFont(fonts.medium);
            ImGui::TextColored(accentCol, "You");
            ImGui::PopFont();
            ImGui::TextWrapped("%s", m.text.c_str());
            ImGui::EndChild();
            ImGui::PopStyleColor(2);
            ImGui::Dummy(ImVec2(0, 6 * ui::uiScale));
            ImGui::PopID();
            continue;
        }
        drawAssistantTurn(st, m, fonts, logW);
        ImGui::Dummy(ImVec2(0, 8 * ui::uiScale));
        ImGui::PopID();
    }
    if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 6) ImGui::SetScrollHereY(1.0f);
    ImGui::EndChild();
    ImGui::PopStyleColor();

    // ---- composer (measure its height for next frame's reserve) ----
    ImGui::Dummy(ImVec2(0, ui::metrics::U(1)));
    ImGui::BeginGroup();
    drawComposer(st, busy, canRun);
    ImGui::EndGroup();
    s_composerH = ImGui::GetItemRectSize().y + ui::metrics::U(1);

    ImGui::End();
}

}  // namespace ui
