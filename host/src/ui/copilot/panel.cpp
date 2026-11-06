// Chudware Copilot pane — the pane host.
//
// A PINNED, full-height, rounded liquid-glass column overlaid on the viewport's right edge with
// ~12dp margins (geometry from ui::viewportBackdrop(); visible left-edge resize boundary + grip;
// the docked task-pane layout is gone — this is the only layout). Top-down: a compact rounded
// header with a live, run-tinted tesseract, Chat / Tasks / Artifacts navigation, settings and
// collapse actions. Sessions / New chat share that row when their measured widths fit, and
// move together onto a second row otherwise; small panes retain named tooltips for icon tabs.
// Then the conversation log with a per-turn transparent activity timeline (what the AI is
// doing / thinking, expandable), a reserved latest-reply footer, and a bottom composer (compact
// Agent/Plan/Swarm/Edit/Ask dropdown + context meter; transparent prompt with in-box send;
// graduated-width segmented model/effort/attachment/mic/context toolbar). Header gear swaps the
// chat area for a compact AI-settings overlay (horizontal top
// tabs for Agents/Prompts/Skills/Context/Permissions/Providers). The full form lives in the
// Settings modal AI tab. Bubbles/cards and the composer are painted as translucent adaptive
// Liquid-Glass bubbles over the live viewport backdrop (host contract H5, GlassGroup::Copilot)
// so every ink tone stays readable over ANY model color.
//
// This file hosts the header, the Sessions/Tasks/Artifacts side panels, drawAIPanel itself,
// and the thin ai_workspace reuse hooks (chatWorkspaceSync/chatDrawLog/chatDrawComposer/...)
// that let the full-page AI workspace embed the SAME chat state + send path.

#include "copilot_internal.h"
#include "agent_overview.h"         // ui::drawAgentOverview — live sub-agents + persistent todos
#include "ui/tesseract.h"  // ui::drawTesseract — the animated 4-cube brand mark in the header
#include "core/engine_events.h"  // core::mirrorRecoveredDocs — Reopen a dismissed live agent doc
#include "ui/ai/ai_prefs.h"      // ui::chatShowRenders — Artifacts render-thumbnail display pref
#include "ui/ai/agent_dag.h"      // one terminal-state source for every sub-agent surface
#include "image_lightbox.h"
#include "ui/omp_session_actions.h"  // OMP session recovery
#include "ui/workspace_home.h"      // empty-workspace home: wider chat column
#include "ui/copilot/inline_image_refs.h"  // stripInlineImageMarkers — Sessions titles
#include "util/time_local.h"        // chudware::util::localTime — Sessions recency groups
#include <string_view>
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#include "core/jobs.h"
#endif

namespace ui {

// ---- header: collapse + [brand | Chat Tasks Artifacts ... gear] / [Sessions ... + New chat] ----
// Copilot vertical-collapse state (file scope so the header's collapse chevron and drawAIPanel's
// height animation share it). Mirrors the FeatureManager collapse (rolls UP to this header
// strip instead of shrinking to a pill in both axes). Seeded from st.settings.copilotCollapsed on
// drawAIPanel's first call (see s_cpInited there) so a collapsed column stays collapsed across
// restarts.
static bool s_cpCollapsed = false;
// The header's tabs are a RADIO set, not independent toggles: exactly one of Chat / Tasks /
// Artifacts / Sessions / Settings is showing, and Chat is "no overlay". Sessions lives on the
// second header row but belongs to the same set. AppState carries it as four bools
// (a shared core type), so funnel every read and write through these two so a new surface can
// never leave two of them true — or, worse, set one without clearing the rest.
enum class CpTab { Chat, Tasks, Artifacts, Sessions, Settings };

static CpTab cpActiveTab(const AppState& st) {
    if (st.showTaskList)          return CpTab::Tasks;
    if (st.showArtifacts)         return CpTab::Artifacts;
    if (st.showSessionsPicker)    return CpTab::Sessions;
    if (st.showAISettingsOverlay) return CpTab::Settings;
    return CpTab::Chat;
}

static void cpSetTab(AppState& st, CpTab t) {
    st.showTaskList          = (t == CpTab::Tasks);
    st.showArtifacts         = (t == CpTab::Artifacts);
    st.showSessionsPicker    = (t == CpTab::Sessions);
    st.showAISettingsOverlay = (t == CpTab::Settings);
}

// Clicking the tab you are already on returns to Chat; clicking Chat while on Chat is a no-op.
static CpTab cpTabAfterClick(CpTab current, CpTab clicked) {
    return (current == clicked) ? CpTab::Chat : clicked;
}

// A fresh, empty chat in the active project: the same reset the header's "New chat" performs.
static void cpStartNewChat(AppState& st) {
    newConversation(st);
    composerSetText(st, "");
    st.pendingAttachments.clear();
    st.pendingInlineImagePaths.clear();
    st.pendingContextRefs.clear();
    st.contextPickActive = false;
}

// Compact AI-settings popover body (provider + model + effort + run toggles + "All settings...");
// defined below with the picker helpers, hosted by the header Settings button.


// True when the plan has real checklist content — at least one non-subagent item
// (step/task/part/objective/node) or an explicit fromEvent plan with a non-empty summary.
// Spawn-only shells (present + title "Plan" + subagent rows) are not real; same gate as
// cards.cpp drawPlanSection (W1-B). Local copy — do not share across TUs in this wave.
static bool planIsReal(const PlanState& ps) {
    if (!ps.present) return false;
    for (const auto& it : ps.items) {
        if (it.kind == "step" || it.kind == "task" || it.kind == "part" ||
            it.kind == "objective" || it.kind == "node")
            return true;
    }
    return ps.fromEvent && !ps.summary.empty();
}

// Aggregate task/plan progress (done/total) from the active conversation's most recent
// assistant turn. DETERMINISTIC: statuses come from engine stage/mission events, not the
// model claiming completion (no cheating). Excludes subagent/objective rows.
static void copilotTaskProgress(AppState& st, int& done, int& total) {
    done = 0;
    total = 0;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it) {
        if (it->fromUser || !planIsReal(it->planState)) continue;
        for (const auto& pi : it->planState.items) {
            if (pi.kind == "subagent" || pi.kind == "objective") continue;
            ++total;
            if (pi.status == "done" || pi.status == "pass") ++done;
        }
        if (total > 0) break;  // newest turn that has a plan wins
    }
}

// Scale the alpha of every vertex in dl->VtxBuffer[from, end) by `a`. Used for the view-switch
// fade: widgets, custom draw-list painters (ColorConvertFloat4ToU32) and images all end up as
// vertices, so scaling them is the only way the fade covers fills, borders and text alike.
static void cpFadeDrawList(ImDrawList* dl, int from, float a) {
    for (int i = from; i < dl->VtxBuffer.Size; ++i) {
        ImU32& c = dl->VtxBuffer[i].col;
        const ImU32 alpha = static_cast<ImU32>(static_cast<float>(c >> IM_COL32_A_SHIFT) * a + 0.5f);
        c = (c & ~IM_COL32_A_MASK) | (alpha << IM_COL32_A_SHIFT);
    }
}

// A child window's draw list holds only what this frame drew into it (it is reset on the frame's
// first Begin), so the whole list and every nested child window's list belong to the view.
static void cpFadeWindowTree(ImGuiWindow* w, float a) {
    cpFadeDrawList(w->DrawList, 0, a);
    for (ImGuiWindow* child : w->DC.ChildWindows) cpFadeWindowTree(child, a);
}

// Sliding selection pill behind the header tabs. `tab` identifies the selection (a change starts
// the glide); `target` is the selected tab's rect this frame. Once settled it simply tracks the
// target, so resizing the column never drags the pill behind its tab. `snap` (screenshot
// captures) lands the pill instantly so a capture never depends on frame timing.
static void drawCopilotTabIndicator(int tab, const ImRect& target, bool hovered, bool held,
                                    bool snap) {
    struct Glide {
        bool  seeded = false;
        int   tab = 0;
        float p = 1.0f;       // 0..1 progress of the current glide
        float from[4] = {};   // rect when the glide began: minX, minY, maxX, maxY
        float cur[4] = {};    // rect drawn last frame
    };
    static Glide g;
    const float to[4] = {target.Min.x, target.Min.y, target.Max.x, target.Max.y};
    if (!g.seeded) {
        std::copy(to, to + 4, g.cur);
        g.seeded = true;
        g.tab = tab;
    } else if (tab != g.tab) {
        std::copy(g.cur, g.cur + 4, g.from);
        g.tab = tab;
        g.p = 0.0f;
    }
    if (snap) g.p = 1.0f;
    if (g.p < 1.0f) {
        g.p = std::min(1.0f, g.p + ui::anim::dt() / ui::anim::kBase);
        ui::anim::requestFrames();
    }
    if (g.p >= 1.0f) {
        std::copy(to, to + 4, g.cur);
    } else {
        const bool right = (to[0] + to[2]) > (g.from[0] + g.from[2]);
        const float lead = ui::anim::easeOut(g.p);
        const float trail = ui::anim::easeOut(std::clamp((g.p - 0.15f) / 0.85f, 0.0f, 1.0f));
        const float eMin = right ? trail : lead;   // the edge the pill moves AWAY from lags
        const float eMax = right ? lead : trail;
        g.cur[0] = g.from[0] + (to[0] - g.from[0]) * eMin;
        g.cur[2] = g.from[2] + (to[2] - g.from[2]) * eMax;
        g.cur[1] = g.from[1] + (to[1] - g.from[1]) * lead;
        g.cur[3] = g.from[3] + (to[3] - g.from[3]) * lead;
    }
    const float hv = ui::anim::toward(ImGui::GetID("##hdr_ind_hover"),
                                      hovered ? 1.0f : 0.0f, ui::anim::kFast);
    const ui::AdaptiveSurface& surf = chatSurf();
    ImVec4 fill = semanticOnGlass(ui::palette.accent, surf);
    ImVec4 edge = fill;
    fill.w = 0.12f + 0.05f * hv + (held ? 0.06f : 0.0f);
    edge.w = 0.42f;
    ui::controlFace(ImGui::GetWindowDrawList(), ImVec2(g.cur[0], g.cur[1]), ImVec2(g.cur[2], g.cur[3]),
                    ImGui::GetColorU32(fill), ImGui::GetColorU32(edge), ui::kit::chipRound(),
                    held ? 0.0f : 1.0f);
}

static void drawCopilotHeader(AppState& st, bool busy) {
    const auto& pal = ui::palette;
    const CpTab tabIn = cpActiveTab(st);
    CpTab tabOut = tabIn;
    // Keep header controls independent of the global form-control padding.
    ui::kit::pushDensity(ui::kit::Density::Compact);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(ui::metrics::cpMenuHeadGap(), ui::kit::padY()));
    const float h = ImGui::GetFrameHeight();
    const float mark = std::max(ui::metrics::iconHeader(), h);
    const float rowH = std::max(mark, h);
    const float gap = ui::metrics::U(1.0f);
    const float left = ui::kit::gutter();  // clear the column resize strip
    const float right = ImGui::GetWindowWidth() - left;
    const float avail = right - left;
    const float top = ui::metrics::U(1.0f);
    const float iconW = h;
    auto textW = [&](const char* label) {
        return ImGui::CalcTextSize(label).x + ImGui::GetStyle().FramePadding.x * 2.0f;
    };

    int done = 0, total = 0;
    copilotTaskProgress(st, done, total);
    char tasks[48];
    if (total > 0) std::snprintf(tasks, sizeof(tasks), "Tasks %d/%d", done, total);
    else std::snprintf(tasks, sizeof(tasks), "Tasks");
    const float sessW = textW("Sessions");
    const float newW = textW("New chat") + std::min(ui::metrics::iconHeader(), h - gap) + gap;
    const float fullTabs = textW("Chat") + textW(tasks) + textW("Artifacts");
    const bool oneRow = mark + fullTabs + sessW + newW + iconW * 2.0f + gap * 7.0f <= avail;
    const float navRoom = avail - mark - iconW * 2.0f - gap * 4.0f -
                         (oneRow ? sessW + newW + gap * 2.0f : 0.0f);
    // Drop the count before dropping a name. Never show a meaningless bare "0/0".
    if (!oneRow && fullTabs > navRoom && total > 0)
        std::snprintf(tasks, sizeof(tasks), "Tasks");
    bool chatText = true, tasksText = true, artsText = true;
    auto tabsW = [&]() {
        return (chatText ? textW("Chat") : iconW) +
               (tasksText ? textW(tasks) : iconW) +
               (artsText ? textW("Artifacts") : iconW);
    };
    if (tabsW() > navRoom) artsText = false;
    if (tabsW() > navRoom) tasksText = false;
    if (tabsW() > navRoom) chatText = false;

    const float* statusCol = pal.accentMid;
    const char* statusTip = "Idle";
    if (st.statusKind == 2) { statusCol = pal.statusErr; statusTip = "Error"; }
    else if (busy || activeConversationRunning(st)) {
        statusCol = pal.accent; statusTip = "Running";
    }
    ImGui::SetCursorPos(ImVec2(left, top + (rowH - mark) * 0.5f));
    ImGui::Dummy(ImVec2(mark, mark));
    const ImVec2 mkMin = ImGui::GetItemRectMin();
    ui::drawTesseract(ImGui::GetWindowDrawList(),
                      ImVec2(mkMin.x + mark * 0.5f, mkMin.y + mark * 0.5f),
                      mark, ImGui::GetTime(), ui::tesseractAnimFromInt(st.settings.brandAnim),
                      ImGui::ColorConvertFloat4ToU32(
                          ImVec4(statusCol[0], statusCol[1], statusCol[2], 1.0f)));
    ui::anim::requestFramesAt(30);
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", statusTip);

    float x = left + mark + gap;
    auto hit = [&](const char* id, float width, float y) {
        ImGui::SetCursorPos(ImVec2(x, y));
        const auto result = headerTextButtonHit(id, width);
        x += width + gap;
        return result;
    };
    const float navY = top + (rowH - h) * 0.5f;
    const auto chat = hit("##hdr_chat", chatText ? textW("Chat") : iconW, navY);
    const auto task = hit("##hdr_tasks", tasksText ? textW(tasks) : iconW, navY);
    const auto arts = hit("##hdr_arts", artsText ? textW("Artifacts") : iconW, navY);
    if (chat.clicked) tabOut = CpTab::Chat;
    if (task.clicked) tabOut = cpTabAfterClick(tabIn, CpTab::Tasks);
    if (arts.clicked) tabOut = cpTabAfterClick(tabIn, CpTab::Artifacts);

    const float actionY = oneRow ? navY : top + rowH + gap;
    x = oneRow ? right - iconW * 2.0f - gap * 3.0f - sessW - newW : left;
    const auto sessions = hit("##hdr_sess", sessW, actionY);
    const bool newText = oneRow || sessW + newW + gap <= avail;
    const float freshW = newText ? newW : iconW;
    if (!oneRow) x = right - freshW;
    const auto fresh = hit("##hdr_new", freshW, actionY);
    if (sessions.clicked) tabOut = cpTabAfterClick(tabIn, CpTab::Sessions);
    if (fresh.clicked) tabOut = CpTab::Chat;

    x = right - iconW * 2.0f - gap;
    const auto gear = hit("##hdr_gear", iconW, navY);
    if (gear.clicked) tabOut = cpTabAfterClick(tabIn, CpTab::Settings);
    const auto collapse = hit("##cp_collapse", iconW, navY);
    if (collapse.clicked) {
        s_cpCollapsed = !s_cpCollapsed;
        st.settings.copilotCollapsed = s_cpCollapsed;
        saveSettings(st.settings);
    }
    if (tabOut != tabIn) cpSetTab(st, tabOut);
    if (fresh.clicked) cpStartNewChat(st);
    // The selected tab's face is ONE pill that glides between tabs (leading edge first, trailing
    // edge catching up) instead of each tab snapping its own fill on and off. It is emitted before
    // the buttons so their ink and icons draw on top of it.
    {
        const HeaderBtnHit* on = &chat;
        switch (tabOut) {
            case CpTab::Tasks:     on = &task;     break;
            case CpTab::Artifacts: on = &arts;     break;
            case CpTab::Sessions:  on = &sessions; break;
            case CpTab::Settings:  on = &gear;     break;
            default:               break;
        }
        drawCopilotTabIndicator(static_cast<int>(tabOut), on->rect, on->hovered, on->held, st.shot);
    }
    // Text-only tabs when there is room; icon-only tabs at small widths. The same measured
    // widths drive hits and paint, and all selection paints use this frame's resolved tab.
    headerTextButtonPaint(chat, "Chat", tabOut == CpTab::Chat, chatText ? nullptr : "ai_panel", chatText, false, false);
    headerTextButtonPaint(task, tasks, tabOut == CpTab::Tasks, tasksText ? nullptr : "plan_board", tasksText, false, false);
    headerTextButtonPaint(arts, "Artifacts", tabOut == CpTab::Artifacts, artsText ? nullptr : "artifacts", artsText, false, false);
    headerTextButtonPaint(sessions, "Sessions", tabOut == CpTab::Sessions, nullptr, true, true, false);
    headerTextButtonPaint(fresh, "New chat", false, "new_doc", newText, true);
    headerTextButtonPaint(gear, "Settings", tabOut == CpTab::Settings, "settings", false, false, false);
    headerTextButtonPaint(collapse, "", false, nullptr, false, true);
    const ImVec2 center = collapse.rect.GetCenter();
    const float r = ImGui::GetTextLineHeight() * 0.22f;
    const float direction = s_cpCollapsed ? 1.0f : -1.0f;
    const ImVec2 chevron[] = {
        ImVec2(center.x - r, center.y - direction * r * 0.5f),
        ImVec2(center.x, center.y + direction * r * 0.5f),
        ImVec2(center.x + r, center.y - direction * r * 0.5f)};
    ImGui::GetWindowDrawList()->AddPolyline(chevron, 3,
        ImGui::GetColorU32(chatSurf().textSecondary), ImDrawFlags_None,
        ui::metrics::stroke(1.5f));
    if (collapse.hovered)
        ImGui::SetTooltip("%s", s_cpCollapsed ? "Expand Copilot" : "Collapse Copilot");
    if (chat.hovered) ImGui::SetTooltip("Chat \xc2\xb7 the current conversation");
    else if (task.hovered) {
        if (total > 0) ImGui::SetTooltip("Tasks \xc2\xb7 %d of %d complete", done, total);
        else ImGui::SetTooltip("Tasks \xc2\xb7 this session's plan and progress");
    } else if (arts.hovered) ImGui::SetTooltip("Artifacts \xc2\xb7 documents, renders and files");
    else if (sessions.hovered) ImGui::SetTooltip("Sessions \xc2\xb7 chats in the active project");
    else if (fresh.hovered) ImGui::SetTooltip("New chat \xc2\xb7 start a fresh session");
    else if (gear.hovered) ImGui::SetTooltip("AI settings");
    ImGui::SetCursorPos(ImVec2(left, top + rowH + (oneRow ? 0.0f : gap + h) + gap));
    ImGui::Dummy(ImVec2(0, 0));
    ImGui::PopStyleVar();
    ui::kit::popDensity();
}

// In-flight marker for a session row: the brand tesseract spinner in the row's leading slot,
// animated by the same per-frame repaint that streams the run itself. Rows reserve
// sessRunningIconSize() plus a gap before their text, so title and meta clear the icon.
static float sessRunningIconSize() {
    return std::max(ui::kTesseractSpinnerMinPx, ui::metrics::px(18.0f));
}
static void drawSessionRunningIcon(ImDrawList* dl, const ui::AdaptiveSurface& surf,
                                   const ImVec2& center) {
    ImVec4 accInk = semanticOnGlass(ui::palette.accent, surf);
    accInk.w = 1.0f;
    ui::drawTesseractSpinner(dl, center, sessRunningIconSize(), ImGui::GetTime(),
                             ImGui::ColorConvertFloat4ToU32(accInk));
}


// Session recovery actions supported by the runtime. Async completions are generation-guarded.
namespace {

struct CcManageUi {
    std::string convId;          // session the card is open for; empty = closed
    uint64_t gen = 0;            // bumped on open/close — stale RPC completions drop
    bool exportBusy = false, closeBusy = false, branchBusy = false;
    char branchEntry[257] = {};
    std::string err;
    std::string exportPath;

    // Reset every field EXCEPT the generation, which must only ever advance — a reset that
    // zeroed it would let a pre-reset RPC completion match a post-reset card again.
    void reset() {
        const uint64_t g = gen + 1;
        *this = CcManageUi{};
        gen = g;
    }
    void open(std::string id) {
        reset();
        convId = std::move(id);
    }
    void close() { reset(); }
};
CcManageUi s_ccm;

constexpr const char* kCcNotLiveHint =
    "Session runtime is not active \xe2\x80\x94 send a message in this session to resume it, "
    "then retry.";

// Shared refusal triage for every recovery completion. Returns true when the response was
// consumed as a failure (latched-absent / transport / clean not-live refusal).
bool ccConsumeFailure(const std::string& convId, const core::RpcResult& r, const char* what) {
    if (ompSessionRpcAbsent(r.ok, r.err, r.result)) {
        ompSessionServiceAvailable() = false;  // older service — whole surface off
        return true;
    }
    if (!r.ok) {
        // A run holding the AI service answers "busy" — say when the action comes back, never
        // the internal refusal text.
        s_ccm.err = core::workerLaneBusy(r)
                        ? std::string(kAvailableAfterRun)
                        : std::string(what) + " failed: " +
                              userFacingRpcError(r, "the AI service did not respond.");
        return true;
    }
    if (ompSessionNotLive(r.result)) {
        // Clean refusal, not an error: the runtime is gone (restart / closed). Record the
        // truth so the buttons disable with the resume hint instead of retrying.
        ompSessionLinkEnsure(convId).runtimeLive = false;
        s_ccm.err = kCcNotLiveHint;
        return true;
    }
    return false;
}

void ccStartExport(AppState& st, const std::string& convId, const std::string& outputPath) {
    s_ccm.exportBusy = true;
    s_ccm.err.clear();
    const uint64_t gen = s_ccm.gen;
    AppState* stp = &st;
    st.rpc.call(kOmpExportSessionMethod,
                ompExportSessionParams(convId, outputPath),
                [stp, gen, convId](const core::RpcResult& r) {
        if (s_ccm.gen != gen) return;
        s_ccm.exportBusy = false;
        if (ccConsumeFailure(convId, r, "Export")) return;
        OmpExportResult res;
        if (!parseOmpExportResult(r.result, res)) {
            s_ccm.err = ompActionError(r.result, "Export failed.");
            return;
        }
        s_ccm.exportPath = res.path;
        stp->toast(ui::ToastLevel::Success, "Transcript exported \xc2\xb7 " + res.path);
        chatArtifactOpenSmart(*stp, res.path);  // export SHOWS its destination and OPENS it
    }, 120000);
}

void ccStartClose(AppState& st, const std::string& convId) {
    s_ccm.closeBusy = true;
    s_ccm.err.clear();
    const uint64_t gen = s_ccm.gen;
    AppState* stp = &st;
    st.rpc.call(kOmpCloseSessionMethod, ompCloseSessionParams(convId),
                [stp, gen, convId](const core::RpcResult& r) {
        if (s_ccm.gen != gen) return;
        s_ccm.closeBusy = false;
        if (ompSessionRpcAbsent(r.ok, r.err, r.result)) {
            ompSessionServiceAvailable() = false;
            return;
        }
        if (!r.ok) {
            s_ccm.err = core::workerLaneBusy(r)
                            ? std::string(kAvailableAfterRun)
                            : "Close failed: " +
                                  userFacingRpcError(r, "the AI service did not respond.");
            return;
        }
        if (!parseOmpCloseResult(r.result)) {
            s_ccm.err = ompActionError(r.result, "Close failed.");
            return;
        }
        // Idempotent by contract: the runtime is gone either way; the persisted file stays.
        ompSessionLinkEnsure(convId).runtimeLive = false;
        stp->toast(ui::ToastLevel::Info,
                   "Runtime closed \xe2\x80\x94 transcript kept. Sending a message resumes it.");
    }, 30000);
}
void ccStartBranch(AppState& st, const std::string& convId, const std::string& entryId) {
    s_ccm.branchBusy = true;
    s_ccm.err.clear();
    const uint64_t gen = s_ccm.gen;
    AppState* stp = &st;
    st.rpc.call(kOmpBranchSessionMethod, ompBranchSessionParams(convId, entryId),
                [stp, gen, convId](const core::RpcResult& r) {
        if (s_ccm.gen != gen) return;
        s_ccm.branchBusy = false;
        if (ccConsumeFailure(convId, r, "Branch")) return;
        OmpBranchResult result;
        if (!parseOmpBranchResult(r.result, result)) {
            s_ccm.err = ompActionError(r.result, "Branch failed.");
            return;
        }
        int idx = -1;
        for (int i = 0; i < (int)stp->conversations.size(); ++i)
            if (stp->conversations[i].id == convId) { idx = i; break; }
        if (idx < 0) {
            s_ccm.err = "Branch succeeded, but the source conversation is no longer open.";
            return;
        }
        const int forkIdx = forkConversation(*stp, idx);
        if (forkIdx < 0) {
            s_ccm.err = "Branch succeeded, but the conversation could not be created.";
            return;
        }
        ompSessionLinkEnsure(convId).runtimeLive = false;
        OmpSessionLink forkLink;
        forkLink.sessionId = result.sessionId;
        forkLink.sessionFile = result.sessionFile;
        ompSessionLinkSet(stp->conversations[forkIdx].id, std::move(forkLink));
#ifdef __EMSCRIPTEN__
        bindCellAgentSessionHandle(stp->conversations[forkIdx].id, result.sessionId,
                                  cellAgentSessionProvider(convId));
#endif
        markChatHistoryDirty();
        stp->toast(ui::ToastLevel::Success, "Branched conversation is ready to resume.");
        s_ccm.close();
    }, 60000);
}



// The expanded recovery card, drawn directly under its session row.
void drawOmpManageCard(AppState& st, const Conversation& c, bool running) {
    const std::string& convId = c.id;
    const OmpSessionLink* link = ompSessionLink(convId);
    const bool serviceOk = ompSessionServiceAvailable();
#ifdef __EMSCRIPTEN__
    const bool live = link && link->runtimeLive && !cellAgentSessionHandle(convId).empty();
#else
    const bool live = link && link->runtimeLive;
#endif
    const bool busy = s_ccm.exportBusy || s_ccm.closeBusy || s_ccm.branchBusy;
    // Every recovery action goes to the AI service, which cannot take them while ANY run (on
    // this session or another) is in progress — so they wait for the run instead of failing.
    const bool runActive = running || st.anyRunActive();
    // One disabled-reason string per state — kit buttons show it as the tooltip, so the
    // action is never hidden, only explained.
    const char* whyNot = !serviceOk
                             ? "The AI service does not offer session recovery (older service)."
                         : runActive ? kAvailableAfterRun
                         : !live ? kCcNotLiveHint
                         : busy  ? "Another recovery action is still running."
                                 : nullptr;
    const bool actionable = whyNot == nullptr;

    ImGui::PushID(convId.c_str());
    if (ui::kit::cardBegin("ccm_card", "Session recovery", "history")) {
        if (!serviceOk) {
            ui::kit::blockedState("Session recovery unavailable",
                                  "The AI service does not provide session recovery methods.",
                                  "Update the service, then reopen this panel.");
        } else {
            // ---- state row: runtime + persisted health --------------------------------------
            ui::kit::chip(live ? "runtime active" : "runtime inactive",
                          live ? ui::kit::Tone::Ok : ui::kit::Tone::Neutral, /*dot=*/true,
                          live ? "This app process holds the live service-side session."
                               : "No live runtime \xe2\x80\x94 sending a message resumes the "
                                 "persisted session.");
            if (link && !link->sessionId.empty())
                ui::kit::keyValueRow("Agent session", link->sessionId.c_str(),
                                     ui::kit::Tone::Neutral, /*copyable=*/true);
            if (link && !link->sessionFile.empty())
                ui::kit::keyValueRow("Session file", link->sessionFile.c_str(),
                                     ui::kit::Tone::Neutral, /*copyable=*/true);
            if (!link || (link->sessionId.empty() && link->sessionFile.empty()))
                ui::kit::dimText("No agent session recorded yet \xe2\x80\x94 send a message "
                                 "in this session first.");

            // ---- actions --------------------------------------------------------------------
            // Export goes to the project chat-exports folder unless a destination is chosen.
            if (ui::kit::secondaryButton(s_ccm.exportBusy ? "Exporting\xe2\x80\xa6" : "Export HTML",
                                         actionable, whyNot))
                ccStartExport(st, convId, std::string());
            ImGui::SameLine(0.0f, ui::metrics::U(1.0f));
            if (ui::kit::secondaryButton("Export to\xe2\x80\xa6", actionable, whyNot)) {
                const std::string chosen = ui::saveFileDialog(
                    "HTML (*.html)\0*.html\0All files (*.*)\0*.*\0", "html", c.title.c_str());
                if (!chosen.empty()) ccStartExport(st, convId, chosen);
            }
            if (ImGui::IsItemHovered() && actionable)
                ImGui::SetTooltip("Default destination: the project's chat-exports folder.");
            ImGui::SameLine(0.0f, ui::metrics::U(1.0f));
            if (ui::kit::secondaryButton(s_ccm.closeBusy ? "Closing\xe2\x80\xa6" : "Close runtime",
                                         serviceOk && live && !runActive && !busy,
                                         !live ? "No live runtime to close \xe2\x80\x94 already inactive."
                                               : whyNot))
                ccStartClose(st, convId);
            if (ImGui::IsItemHovered() && live && !runActive)
                ImGui::SetTooltip("Stops the service-side process only. The transcript and the "
                                  "persisted session file are kept \xe2\x80\x94 unlike Delete.");
#ifdef __EMSCRIPTEN__
            ui::kit::dimText("Branching runtime sessions is unavailable in this web session.");
#else
            ImGui::SetNextItemWidth(-1.0f);
            ImGui::InputTextWithHint("##branch_entry", "Runtime entry ID",
                                     s_ccm.branchEntry, sizeof(s_ccm.branchEntry));
            if (ui::kit::secondaryButton(s_ccm.branchBusy ? "Branching..." : "Branch at entry",
                                         actionable && s_ccm.branchEntry[0],
                                         actionable ? "Enter a runtime entry ID." : whyNot))
                ccStartBranch(st, convId, s_ccm.branchEntry);
#endif

            // A "wait for the run" refusal is not a failure: calm tone, and it clears itself
            // once the run is over and the actions are live again.
            const bool waitNote = s_ccm.err == kAvailableAfterRun;
            if (waitNote && !runActive) s_ccm.err.clear();
            if (!s_ccm.err.empty())
                ui::kit::inlineStatus(s_ccm.err.c_str(),
                                      waitNote ? ui::kit::Tone::Neutral : ui::kit::Tone::Err);

            // ---- last export ----------------------------------------------------------------
            if (!s_ccm.exportPath.empty()) {
                ui::kit::keyValueRow("Exported to", s_ccm.exportPath.c_str(),
                                     ui::kit::Tone::Ok, /*copyable=*/true);
                if (ui::haveOsFileManager() && ui::kit::secondaryButton("Show in folder"))
                    ui::revealInFileManager(s_ccm.exportPath);
            }

        }
    }
    ui::kit::cardEnd();
    ImGui::PopID();
    ui::kit::gapRow();
}

}  // namespace

// ---- Sessions project scope -------------------------------------------------------------------
// The list shows ONE project's sessions at a time, chosen by a compact switcher in the header.
// Membership is the session's STORED projectDir key, compared at view time — never re-derived
// from what a session produced. Sessions with no stored key live under "Unassigned" only.
// The scope follows the active project (convProjectDir) and snaps back to it whenever that
// changes; peeking at another project is a view choice until the user explicitly switches.
namespace {
enum class SessScope { Project, Unassigned, All };
struct SessScopeState {
    SessScope   scope = SessScope::Project;
    std::string key;           // viewed project key (SessScope::Project)
    std::string dir;           // a stored projectDir carrying `key` (the switch target)
    std::string followedKey;   // active-project key the scope last synced to
    bool        seeded = false;
};
SessScopeState s_sessScope;

// Inline rename of ONE session row (the row's title becomes an edit box). Keyed by id, not row
// index, so a list that reorders mid-edit cannot move the box onto another session.
struct SessRename {
    std::string id;        // session being renamed; empty = none
    char buf[128] = {};
    int  frames = 0;       // frames the box has been up (focus is requested on the first)
    bool everActive = false;
};
SessRename s_rename;

struct SessProjectEntry {
    std::string key, dir, name;
    int         count = 0;     // sessions listed under the current archived filter
};

std::string sessProjectName(const std::string& dir) {
    if (dir.empty()) return "No project";
    const std::filesystem::path p = lexicalPath(dir);
    if (!p.filename().empty()) return p.filename().generic_string();
    if (!p.parent_path().filename().empty()) return p.parent_path().filename().generic_string();
    return p.generic_string();
}

// Distinct stored project keys (active project first, then by name) + the unassigned count.
std::vector<SessProjectEntry> sessProjects(const AppState& st, bool archived,
                                           const std::string& activeKey,
                                           const std::string& activeDir, int& unassigned) {
    std::vector<SessProjectEntry> out;
    unassigned = 0;
    if (!activeKey.empty()) out.push_back({activeKey, activeDir, sessProjectName(activeDir), 0});
    for (const Conversation& c : st.conversations) {
        const std::string& key = convProjectKey(c);
        const int n = (c.archived == archived) ? 1 : 0;
        if (key.empty()) { unassigned += n; continue; }
        auto it = std::find_if(out.begin(), out.end(),
                               [&](const SessProjectEntry& e) { return e.key == key; });
        if (it == out.end())
            out.push_back({key, c.projectDir, sessProjectName(c.projectDir), n});
        else
            it->count += n;
    }
    const auto first = out.begin() + (activeKey.empty() ? 0 : 1);
    std::sort(first, out.end(), [](const SessProjectEntry& a, const SessProjectEntry& b) {
        return a.name != b.name ? a.name < b.name : a.key < b.key;
    });
    return out;
}

// ---- Sessions list chrome -----------------------------------------------------------------------
// Header controls and row furniture are painted onto the window draw list in the pane's
// chat-surface ink (chatSurf tones), so they read over any viewport backdrop in both themes, and
// every position derives from the row / pill rect — nothing leans on ImGui cursor drift.

// Session whose inline delete confirmation is up / whose overflow menu is open. Both keep their
// row submitted while it is scrolled out of view, so the prompt and the popup survive scrolling.
// Keyed by id: the list re-sorts by activity as sessions are used.
std::string s_delConfirm;
std::string s_menuConv;
char        s_filter[96] = {};

ImU32 sessInk(ImVec4 c, float alpha) {
    c.w *= alpha;
    return ImGui::ColorConvertFloat4ToU32(c);
}

// The title as listed: inline-image markers ("[Image #1]") stripped; an emptied or missing title
// falls back to the session's own slot number (stable, unlike a display position, which moves
// as the list re-sorts).
std::string sessDisplayTitle(const Conversation& c, int slot) {
    std::string title = ui::copilot::stripInlineImageMarkers(c.title);
    if (title.empty()) title = "Session " + std::to_string(slot + 1);
    return title;
}

bool sessMatchesFilter(const std::string& title, const std::string& needleLower) {
    if (needleLower.empty()) return true;
    std::string hay;
    hay.reserve(title.size());
    for (const char ch : title) hay += (char)std::tolower((unsigned char)ch);
    return hay.find(needleLower) != std::string::npos;
}

// ---- recency groups ---------------------------------------------------------------------------
// Today / Yesterday / Earlier by a session's LAST message time (ChatMsg::ts; 0 = unknown, which
// files under Earlier). Local calendar days, not 24 h windows.
struct SessDays { double today = 0.0, yesterday = 0.0; };   // local midnights, epoch seconds

SessDays sessDayStarts() {
    SessDays d;
    std::tm t{};
    if (!chudware::util::localTime((std::time_t)chatNowEpoch(), t)) return d;
    t.tm_hour = t.tm_min = t.tm_sec = 0;
    t.tm_isdst = -1;
    d.today = (double)std::mktime(&t);
    t.tm_mday -= 1;   // mktime normalises across month and year edges
    t.tm_isdst = -1;
    d.yesterday = (double)std::mktime(&t);
    return d;
}

int sessBucket(double ts, const SessDays& d) {
    if (!(ts > 0.0)) return 2;
    if (ts >= d.today) return 0;
    return ts >= d.yesterday ? 1 : 2;
}

const char* const kSessBucketLabel[3] = {"Today", "Yesterday", "Earlier"};

// Compact age for the meta line: "just now", "5m ago", "2h ago", "3d ago", then a bare date.
std::string sessAgeLabel(double ts) {
    if (!(ts > 0.0)) return std::string();
    static const char* const kMonths[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
    const double age = chatNowEpoch() - ts;
    char buf[32];
    if (age < 45.0) return "just now";
    if (age < 3600.0)
        std::snprintf(buf, sizeof(buf), "%dm ago", std::max(1, (int)(age / 60.0)));
    else if (age < 86400.0)
        std::snprintf(buf, sizeof(buf), "%dh ago", (int)(age / 3600.0));
    else if (age < 7.0 * 86400.0)
        std::snprintf(buf, sizeof(buf), "%dd ago", (int)(age / 86400.0));
    else {
        std::tm then{}, now{};
        if (!chudware::util::localTime((std::time_t)ts, then)) return std::string();
        chudware::util::localTime((std::time_t)chatNowEpoch(), now);
        if (then.tm_year == now.tm_year)
            std::snprintf(buf, sizeof(buf), "%s %d", kMonths[then.tm_mon], then.tm_mday);
        else
            std::snprintf(buf, sizeof(buf), "%s %d, %d", kMonths[then.tm_mon], then.tm_mday,
                          then.tm_year + 1900);
    }
    return buf;
}

// ---- vector glyphs (the app font has no dots/chevron/folder codepoints worth trusting) ----------
// Three stacked dots: the overflow ("more") mark.
void sessDrawKebab(ImDrawList* dl, const ImVec2& c, float size, ImU32 col) {
    const float r = ui::metrics::px(1.6f);
    const float step = std::max(r * 2.8f, size * 0.24f);
    for (int k = -1; k <= 1; ++k)
        dl->AddCircleFilled(ImVec2(c.x, c.y + (float)k * step), r, col);
}

void sessDrawChevronDown(ImDrawList* dl, const ImVec2& c, float r, ImU32 col) {
    const ImVec2 pts[3] = {ImVec2(c.x - r, c.y - r * 0.5f), ImVec2(c.x, c.y + r * 0.5f),
                           ImVec2(c.x + r, c.y - r * 0.5f)};
    dl->AddPolyline(pts, 3, col, ImDrawFlags_None, ui::metrics::stroke(1.5f));
}

// Closed folder: a tabbed outline.
void sessDrawFolder(ImDrawList* dl, const ImVec2& c, float size, ImU32 col) {
    const float t = ui::metrics::stroke(1.3f);
    const float w = size, h = size * 0.74f;
    const float x0 = c.x - w * 0.5f, y0 = c.y - h * 0.5f;
    const float tab = h * 0.28f, r = size * 0.10f, tabW = w * 0.40f;
    dl->AddRect(ImVec2(x0, y0 + tab), ImVec2(x0 + w, y0 + h), col, r, 0, t);
    const ImVec2 flap[5] = {ImVec2(x0, y0 + tab), ImVec2(x0, y0 + r), ImVec2(x0 + r, y0),
                            ImVec2(x0 + tabW, y0), ImVec2(x0 + tabW + tab * 0.8f, y0 + tab)};
    dl->AddPolyline(flap, 5, col, ImDrawFlags_None, t);
}

// Pill backing for the header controls and in-row buttons: a hairline outline and a wash that
// eases in with `hv`; `on` (0..1) swaps both to the accent so an engaged filter reads engaged.
void sessPillFace(ImDrawList* dl, const ui::AdaptiveSurface& surf, const ImVec2& p0,
                  const ImVec2& p1, float hv, float on) {
    const ImVec4 ink = surf.textPrimary;
    const ImVec4 acc = semanticOnGlass(ui::palette.accent, surf);
    const ImVec4 fillIdle(ink.x, ink.y, ink.z, 0.05f + 0.07f * hv);
    const ImVec4 fillOn(acc.x, acc.y, acc.z, 0.20f + 0.08f * hv);
    const ImVec4 lineIdle(ink.x, ink.y, ink.z, 0.16f + 0.10f * hv);
    const ImVec4 lineOn(acc.x, acc.y, acc.z, 0.60f);
    const float rnd = ui::metrics::cpMenuChipRound();
    dl->AddRectFilled(p0, p1, ImGui::ColorConvertFloat4ToU32(ImLerp(fillIdle, fillOn, on)), rnd);
    dl->AddRect(p0, p1, ImGui::ColorConvertFloat4ToU32(ImLerp(lineIdle, lineOn, on)), rnd, 0,
                ui::metrics::stroke(1.0f));
}

// Small text button for in-row prompts (the delete confirmation), placed by explicit rect so the
// row keeps its computed geometry. `danger` fills it with the error tone.
bool sessTextButton(const char* id, const char* label, const ImVec2& p0, const ImVec2& p1,
                    bool danger, const ui::AdaptiveSurface& surf) {
    ImGui::PushID(id);
    ImGui::SetCursorScreenPos(p0);
    const bool clicked = ImGui::InvisibleButton("##btn", ImVec2(p1.x - p0.x, p1.y - p0.y));
    const bool hovered = ImGui::IsItemHovered();
    const float hv = ui::anim::toward(ImGui::GetID("##hv"), hovered ? 1.0f : 0.0f,
                                      ui::anim::kFast);
    ImGui::PopID();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec4 ink = surf.textPrimary;
    if (danger) {
        const float* e = ui::palette.statusErr;
        const ImVec4 fill(e[0], e[1], e[2], 0.80f + 0.20f * hv);
        dl->AddRectFilled(p0, p1, ImGui::ColorConvertFloat4ToU32(fill),
                          ui::metrics::cpMenuChipRound());
        ink = ui::maxContrastTextOn(fill);
    } else {
        sessPillFace(dl, surf, p0, p1, hv, 0.0f);
    }
    const ImVec2 ts = ImGui::CalcTextSize(label);
    dl->AddText(ImVec2(p0.x + (p1.x - p0.x - ts.x) * 0.5f, p0.y + (p1.y - p0.y - ts.y) * 0.5f),
                ImGui::ColorConvertFloat4ToU32(ink), label);
    if (hovered) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    return clicked;
}

// ---- header toolbar ---------------------------------------------------------------------------
// Under the title: the project dropdown chip (folder, name, chevron) on the left and the archived
// toggle on the right, both one frame tall so they share a baseline. The chip's popup lists every
// project that has sessions, the viewed one checked.
void drawSessionsToolbar(const ui::AdaptiveSurface& surf,
                         const std::vector<SessProjectEntry>& projects, int unassigned,
                         const std::string& activeKey, bool* showArchived) {
    SessScopeState& S = s_sessScope;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 o = ImGui::GetCursorScreenPos();
    const float availW = ImGui::GetContentRegionAvail().x;
    const float h = ImGui::GetFrameHeight();
    const float lineH = ImGui::GetTextLineHeight();
    const float padX = ui::metrics::px(9.0f);
    const float gap = ui::metrics::px(6.0f);
    const float glyph = ui::metrics::px(14.0f);
    const float chevR = ui::metrics::px(3.5f);

    std::string label;
    if (S.scope == SessScope::All)             label = "All projects";
    else if (S.scope == SessScope::Unassigned) label = "Unassigned";
    else {
        label = "No project";
        for (const auto& e : projects)
            if (e.key == S.key) { label = e.name; break; }
    }
    const char* const toggleLabel = "Archived";
    const float toggleW = ImGui::CalcTextSize(toggleLabel).x + padX * 2.0f;
    const float chipChrome = padX * 2.0f + glyph + gap * 2.0f + chevR * 2.0f;
    // The project name yields first when the column is narrow; the toggle never shrinks.
    label = ui::kit::elide(label.c_str(),
                           std::max(ui::metrics::px(24.0f), availW - toggleW - gap - chipChrome));
    const float chipW = chipChrome + ImGui::CalcTextSize(label.c_str()).x;
    const ImVec2 chipMax(o.x + chipW, o.y + h);

    ImGui::PushID("sess_toolbar");

    // ---- project chip
    ImGui::SetCursorScreenPos(o);
    if (ImGui::InvisibleButton("##project", ImVec2(chipW, h))) ImGui::OpenPopup("##project_menu");
    const bool chipHot = ImGui::IsItemHovered();
    const bool chipOpen = ImGui::IsPopupOpen("##project_menu");
    if (chipHot && !chipOpen) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip(S.scope == SessScope::All
            ? "Showing sessions from every project. Click to pick one project."
            : S.scope == SessScope::Unassigned
                ? "Showing sessions with no project. Click to pick a project."
                : "Showing this project's sessions only. Click to view another project's.");
    }
    const float chv = ui::anim::toward(ImGui::GetID("##chv"),
                                       (chipHot || chipOpen) ? 1.0f : 0.0f, ui::anim::kFast);
    sessPillFace(dl, surf, o, chipMax, chv, 0.0f);
    {
        const float cy = o.y + h * 0.5f;
        float x = o.x + padX;
        sessDrawFolder(dl, ImVec2(x + glyph * 0.5f, cy), glyph, sessInk(surf.textSecondary, 1.0f));
        x += glyph + gap;
        dl->AddText(ImVec2(x, o.y + (h - lineH) * 0.5f), sessInk(surf.textPrimary, 1.0f),
                    label.c_str());
        x += ImGui::CalcTextSize(label.c_str()).x + gap;
        sessDrawChevronDown(dl, ImVec2(x + chevR, cy), chevR, sessInk(surf.textSecondary, 1.0f));
    }

    ui::pushContextMenuStyle();
    ImGui::SetNextWindowPos(ImVec2(o.x, chipMax.y + ui::metrics::px(3.0f)), ImGuiCond_Appearing);
    ImGui::SetNextWindowSizeConstraints(ImVec2(chipW, 0.0f), ImVec2(FLT_MAX, FLT_MAX));
    if (ImGui::BeginPopup("##project_menu")) {
        for (const auto& e : projects) {
            ImGui::PushID(e.key.c_str());
            char item[160];
            std::snprintf(item, sizeof(item), "%s  (%d)", e.name.c_str(), e.count);
            const bool checked = S.scope == SessScope::Project && S.key == e.key;
            if (ImGui::MenuItem(item, e.key == activeKey ? "active" : nullptr, checked)) {
                S.scope = SessScope::Project;
                S.key   = e.key;
                S.dir   = e.dir;
            }
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", e.dir.c_str());
            ImGui::PopID();
        }
        if (projects.empty()) ui::cpMenuHint("No project has sessions yet.");
        ImGui::Separator();
        char un[64];
        std::snprintf(un, sizeof(un), "Unassigned  (%d)", unassigned);
        if (ImGui::MenuItem(un, nullptr, S.scope == SessScope::Unassigned))
            S.scope = SessScope::Unassigned;
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Sessions that never recorded a project folder.");
        if (ImGui::MenuItem("All projects", nullptr, S.scope == SessScope::All))
            S.scope = SessScope::All;
        ImGui::EndPopup();
    }
    ui::popContextMenuStyle();

    // ---- archived toggle (right-aligned)
    const ImVec2 tMin(o.x + availW - toggleW, o.y);
    const ImVec2 tMax(tMin.x + toggleW, tMin.y + h);
    ImGui::SetCursorScreenPos(tMin);
    if (ImGui::InvisibleButton("##archived", ImVec2(toggleW, h))) *showArchived = !*showArchived;
    const bool togHot = ImGui::IsItemHovered();
    if (togHot) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip("%s", *showArchived
            ? "Showing archived sessions only. Turn off to return to your active list."
            : "Archived sessions are hidden from this list \xe2\x80\x94 turn on to review or "
              "restore them. Archiving keeps the full transcript (unlike Delete).");
    }
    const float tv = ui::anim::toward(ImGui::GetID("##tv"), togHot ? 1.0f : 0.0f,
                                      ui::anim::kFast);
    const float on = ui::anim::toward(ImGui::GetID("##on"), *showArchived ? 1.0f : 0.0f,
                                      ui::anim::kFast);
    sessPillFace(dl, surf, tMin, tMax, tv, on);
    dl->AddText(ImVec2(tMin.x + padX, o.y + (h - lineH) * 0.5f),
                sessInk(ImLerp(surf.textSecondary, surf.textPrimary, std::max(tv, on)), 1.0f),
                toggleLabel);
    ImGui::PopID();

    // One item spanning the row advances the cursor past it (and validates the window extent).
    ImGui::SetCursorScreenPos(o);
    ImGui::Dummy(ImVec2(availW, h));
}

// ---- session rows ------------------------------------------------------------------------------
struct SessRowStyle {
    float padX = 0.0f;
    float rowH = 0.0f, rowRndDp = 0.0f;               // rowRndDp: glassRowBackground scales itself
    float titleH = 0.0f, metaH = 0.0f, lineGap = 0.0f;
    float kebabSz = 0.0f, kebabEdge = 0.0f;
    float actionW = 0.0f;                              // right zone title/meta never enter
    ImFont* titleFont = nullptr;
    bool allProjects = false;
};

struct SessRowResult {
    bool running = false;
    bool del = false, fork = false, archive = false, manage = false;
};

// One session row at `rowMin`, `g.rowH` tall: leading slot (running spinner), title, one meta line
// (age · message count [· project] + doc chip) and a right-hand overflow button. Everything is
// laid out from the row rect, so content is centred and the text clip ends before the action
// zone on every row, hovered or not. List-mutating actions are RETURNED, never run here — the
// caller owns the iteration the mutation would invalidate.
SessRowResult drawSessionRow(AppState& st, int i, const SessRowStyle& g, const ImVec2& rowMin,
                             float rowW, double ts, const ui::AdaptiveSurface& surf) {
    const auto& pal = ui::palette;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const Conversation& c = st.conversations[i];
    const std::vector<ChatMsg>& msgs = (i == st.activeConv) ? st.chat : c.msgs;
    const std::string title = sessDisplayTitle(c, i);
    // Everything below that reads `msgs` is evaluated BEFORE a click can switch conversations
    // (which moves the message buffers between st.chat and the parked conversation).
    const int nMsg = (int)msgs.size();
    SessRowResult out;
    // Truthful per-session state: this conversation's own run, not a global flag. The
    // trailing-message scan catches a running message that has no JobState in
    // st.runs.
    out.running = st.convRunning(c.id) ||
                  (!msgs.empty() && !msgs.back().fromUser && msgs.back().running);
    const bool running = out.running;
    if (running && s_delConfirm == c.id) s_delConfirm.clear();   // a live run cannot be deleted
    const bool renaming = s_rename.id == c.id;
    const bool confirming = s_delConfirm == c.id;
    const char* const archLabel = c.archived ? "Restore" : "Archive";
    const ImVec2 rowMax(rowMin.x + rowW, rowMin.y + g.rowH);
    const ImVec2 kbMin(rowMax.x - g.kebabEdge - g.kebabSz,
                       rowMin.y + (g.rowH - g.kebabSz) * 0.5f);

    ImGui::PushID(c.id.c_str());

    // ---- hit items. Submitted first (they draw nothing) so hover is known before painting. The
    // row button stops short of the action zone and the overflow button sits inside it: they never
    // overlap, so hover and clicks go where the pointer is.
    bool picked = false, pickHover = false, kebabHover = false;
    ImGui::SetCursorScreenPos(rowMin);
    if (renaming || confirming) {
        // The title is an edit box / the row is a prompt: it must not also be a click target.
        ImGui::Dummy(ImVec2(rowW, g.rowH));
    } else {
        picked = ImGui::InvisibleButton("##pick", ImVec2(std::max(1.0f, rowW - g.actionW), g.rowH));
        pickHover = ImGui::IsItemHovered();
        if (ImGui::IsItemClicked(ImGuiMouseButton_Right)) {
            s_menuConv = c.id;
            ImGui::OpenPopup("##sessrow_menu");
        }
        ImGui::SetCursorScreenPos(kbMin);
        if (ImGui::InvisibleButton("##more", ImVec2(g.kebabSz, g.kebabSz))) {
            s_menuConv = c.id;
            ImGui::OpenPopup("##sessrow_menu");
        }
        kebabHover = ImGui::IsItemHovered();
    }
    const bool menuOpen = ImGui::IsPopupOpen("##sessrow_menu");
    if (!menuOpen && s_menuConv == c.id) s_menuConv.clear();

    // ---- text geometry
    const float runSz = sessRunningIconSize();
    const float leadW = running ? runSz + ui::metrics::px(8.0f) : 0.0f;
    const float textX = rowMin.x + g.padX + leadW;
    const float textMaxW = std::max(ui::metrics::px(24.0f), rowMax.x - g.actionW - textX);
    const float blockTop = rowMin.y + (g.rowH - (g.titleH + g.lineGap + g.metaH)) * 0.5f;
    const float metaY = blockTop + g.titleH + g.lineGap;

    // Meta line: age · message count [· project]. "All projects" reveals which project each
    // session belongs to; unassigned sessions say so rather than posing as the current project's.
    std::string meta;
    auto addMeta = [&meta](const std::string& part) {
        if (part.empty()) return;
        if (!meta.empty()) meta += " \xc2\xb7 ";
        meta += part;
    };
    if (nMsg == 0) {
        addMeta("Empty session");
    } else {
        addMeta(sessAgeLabel(ts));
        addMeta(nMsg == 1 ? std::string("1 message") : std::to_string(nMsg) + " messages");
    }
    if (g.allProjects)
        addMeta(c.projectDir.empty() ? std::string("Unassigned") : sessProjectName(c.projectDir));
    const std::string shownMeta = ui::kit::elide(meta.c_str(), textMaxW);
    const float metaW = ImGui::CalcTextSize(shownMeta.c_str()).x;

    // A session can carry a docLink reference: file: links always show (basename); doc: links
    // resolve only while the tab is open. The chip sits INLINE after the meta text, and is dropped
    // (never squeezed over the text) when the row has no room for a readable one.
    std::string badge;
    int badgeTab = -1;
    if (c.docLink.rfind("file:", 0) == 0) {
        const std::string p = c.docLink.substr(5);
        const size_t slash = p.find_last_of("/\\");
        badge = slash == std::string::npos ? p : p.substr(slash + 1);
        for (int ti = 0; ti < (int)st.docTabs.size(); ++ti)
            if (st.docTabs[ti].path == p) { badgeTab = ti; break; }
    } else if (c.docLink.rfind("doc:", 0) == 0) {
        const std::string id = c.docLink.substr(4);
        for (int ti = 0; ti < (int)st.docTabs.size(); ++ti)
            if (st.docTabs[ti].docId == id) {
                // t.title can still be a placeholder ("Part1") or an opaque id;
                // displayTitle() resolves the handoff/tree/path name instead.
                badgeTab = ti; badge = st.docTabs[ti].displayTitle(); break;
            }
    }
    const float chipPadX = ui::metrics::px(5.0f);
    std::string chipText;
    ImVec2 chipMin(0.0f, 0.0f), chipMax(0.0f, 0.0f);
    if (!badge.empty() && !renaming && !confirming) {
        const float gapMeta = ui::metrics::px(6.0f);
        // Never let a long filename eat the whole meta line — cap at half the row.
        const float inner = std::min(textMaxW - metaW - gapMeta, rowW * 0.5f) - chipPadX * 2.0f;
        if (inner >= ui::metrics::px(28.0f)) {
            chipText = ui::kit::elide(badge.c_str(), inner);
            if (!chipText.empty()) {
                chipMin = ImVec2(textX + metaW + gapMeta, metaY - ui::metrics::px(1.0f));
                chipMax = ImVec2(chipMin.x + ImGui::CalcTextSize(chipText.c_str()).x + chipPadX * 2.0f,
                                 metaY + g.metaH + ui::metrics::px(1.0f));
            }
        }
    }
    // The chip is a click target INSIDE the row button: routed by rect here instead of by a second
    // overlapping item, which ImGui would resolve unpredictably.
    const bool chipHit = !chipText.empty() && pickHover && ImGui::IsMouseHoveringRect(chipMin, chipMax);

    // Picking stays live during a run (multi-session): switching parks the viewed conversation;
    // its run keeps streaming into it in the background.
    if (picked) {
        if (chipHit && badgeTab >= 0) {
            st.switchToTab(badgeTab);
        } else {
            // Always land in chat for the chosen session — including re-clicking
            // the already-active one (switchConversation early-returns on same idx).
            if (i != st.activeConv) switchConversation(st, i);
            cpSetTab(st, CpTab::Chat);
        }
    }

    // ---- paint
    // Re-read the selection: it may have just changed under the click above, and painting from a
    // stale value left the row you clicked unhighlighted for a frame (and the old row still lit).
    const bool active = (i == st.activeConv);
    // The wash tracks the whole row rect, not only the two hit items, so crossing the few pixels
    // between the row button and the overflow button never flickers it off.
    const bool overRow = ImGui::IsWindowHovered(ImGuiHoveredFlags_AllowWhenBlockedByActiveItem) &&
                         ImGui::IsMouseHoveringRect(rowMin, rowMax);
    const bool hot = !renaming && !confirming &&
                     (overRow || pickHover || kebabHover || menuOpen);
    const float hv  = ui::anim::toward(ImGui::GetID("##hv"),  hot ? 1.0f : 0.0f, ui::anim::kFast);
    const float act = ui::anim::toward(ImGui::GetID("##act"), active ? 1.0f : 0.0f,
                                       ui::anim::kBase);
    ui::glassRowBackground(dl, rowMin, rowMax, g.rowRndDp, false, false,
                           0.34f + 0.10f * hv + 0.12f * act);
    if (act > 0.01f) {
        // The open session: a soft accent wash and a slim accent bar down the leading edge.
        ui::coloredRowWash(dl, rowMin, rowMax, g.rowRndDp, pal.accent, 0.20f * act, false);
        const float barW = ui::metrics::px(3.0f);
        const float barX = rowMin.x + ui::metrics::px(2.0f);
        const float barInset = g.rowH * 0.22f;
        dl->AddRectFilled(ImVec2(barX, rowMin.y + barInset),
                          ImVec2(barX + barW, rowMax.y - barInset),
                          sessInk(semanticOnGlass(pal.accent, surf), act), barW * 0.5f);
    }
    if (confirming) ui::coloredRowWash(dl, rowMin, rowMax, g.rowRndDp, pal.statusErr, 0.30f, false);
    if (running)
        drawSessionRunningIcon(dl, surf,
                               ImVec2(rowMin.x + g.padX + runSz * 0.5f, rowMin.y + g.rowH * 0.5f));

    bool doDel = false, doArchive = false, doFork = false, doManage = false, doRename = false;
    if (renaming) {
        // Inline rename: Enter or clicking away saves, Esc cancels. An empty name hands the
        // session back to its automatic title.
        const float padRename = ui::metrics::px(1.5f);
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(ui::metrics::px(5.0f), padRename));
        ImGui::SetCursorScreenPos(ImVec2(textX, blockTop - padRename));
        ImGui::SetNextItemWidth(std::max(ui::metrics::px(24.0f), rowMax.x - g.padX - textX));
        if (s_rename.frames++ == 0) ImGui::SetKeyboardFocusHere();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        const bool enter = ImGui::InputText("##rename", s_rename.buf, sizeof(s_rename.buf),
                                            ImGuiInputTextFlags_EnterReturnsTrue |
                                                ImGuiInputTextFlags_AutoSelectAll);
        ImGui::PopStyleColor();
        ImGui::PopStyleVar();
        if (ImGui::IsItemActive()) s_rename.everActive = true;
        const bool deactivated = ImGui::IsItemDeactivated();
        if (enter || (deactivated && !ImGui::IsKeyPressed(ImGuiKey_Escape))) {
            renameConversation(st, i, s_rename.buf);
            s_rename = SessRename{};
        } else if (deactivated || (!s_rename.everActive && s_rename.frames > 3) ||
                   (s_rename.everActive && !ImGui::IsItemActive())) {
            // Esc, focus never landed, or the box lost focus while the row was off-screen:
            // leave the name as it was.
            s_rename = SessRename{};
        }
        dl->AddText(ImVec2(textX, metaY), sessInk(surf.textTertiary, 1.0f),
                    ui::kit::elide("Enter to save \xc2\xb7 Esc to cancel", textMaxW).c_str());
    } else if (confirming) {
        // Inline delete confirmation: the row becomes the prompt, buttons on its right edge.
        const float btnH = g.kebabSz;
        const float btnY = rowMin.y + (g.rowH - btnH) * 0.5f;
        const float btnPadX = ui::metrics::px(10.0f);
        const float delW = ImGui::CalcTextSize("Delete").x + btnPadX * 2.0f;
        const float cancelW = ImGui::CalcTextSize("Cancel").x + btnPadX * 2.0f;
        const ImVec2 delMin(rowMax.x - g.kebabEdge - delW, btnY);
        const ImVec2 cancelMin(delMin.x - ui::metrics::px(6.0f) - cancelW, btnY);
        const float promptW = std::max(ui::metrics::px(24.0f),
                                       cancelMin.x - ui::metrics::px(8.0f) - textX);
        if (g.titleFont) ImGui::PushFont(g.titleFont);
        dl->AddText(ImVec2(textX, blockTop), sessInk(surf.textPrimary, 1.0f),
                    ui::kit::elide("Delete session?", promptW).c_str());
        if (g.titleFont) ImGui::PopFont();
        dl->AddText(ImVec2(textX, metaY), sessInk(surf.textSecondary, 0.9f),
                    ui::kit::elide(title.c_str(), promptW).c_str());
        const bool cancel = sessTextButton("##delcancel", "Cancel", cancelMin,
                                           ImVec2(cancelMin.x + cancelW, btnY + btnH), false, surf);
        const bool ok = sessTextButton("##delok", "Delete", delMin,
                                       ImVec2(delMin.x + delW, btnY + btnH), true, surf);
        if (ok) doDel = true;
        if (cancel || (ImGui::IsKeyPressed(ImGuiKey_Escape, false) &&
                       ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows)))
            s_delConfirm.clear();
    } else {
        if (g.titleFont) ImGui::PushFont(g.titleFont);
        const std::string titleShown = ui::kit::elide(title.c_str(), textMaxW);
        dl->AddText(ImVec2(textX, blockTop), sessInk(surf.textPrimary, 1.0f), titleShown.c_str());
        if (g.titleFont) ImGui::PopFont();
        dl->AddText(ImVec2(textX, metaY), sessInk(surf.textSecondary, 0.9f), shownMeta.c_str());
        if (!chipText.empty()) {
            const float chipH = chipMax.y - chipMin.y;
            dl->AddRectFilled(chipMin, chipMax,
                              sessInk(surf.textPrimary, (badgeTab >= 0 ? 0.12f : 0.06f) +
                                                            (chipHit ? 0.07f : 0.0f)),
                              ui::metrics::cpMenuChipRound());
            ImVec4 chipInk = badgeTab >= 0 ? surf.textSecondary : surf.textTertiary;
            chipInk.w = 1.0f;
            dl->AddText(ImVec2(chipMin.x + chipPadX, chipMin.y + (chipH - g.metaH) * 0.5f),
                        ImGui::ColorConvertFloat4ToU32(chipInk), chipText.c_str());
        }

        // Overflow button: faint at rest (the visible sign a session can be managed, and the only
        // route to its actions without a hover-capable pointer), full on hover or selection.
        const bool kebabHot = kebabHover || menuOpen;
        const float kv = ui::anim::toward(ImGui::GetID("##kv"), kebabHot ? 1.0f : 0.0f,
                                          ui::anim::kFast);
        if (kv > 0.01f)
            dl->AddRectFilled(kbMin, ImVec2(kbMin.x + g.kebabSz, kbMin.y + g.kebabSz),
                              sessInk(surf.textPrimary, 0.10f * kv),
                              ui::metrics::cpMenuChipRound());
        const float restA = 0.42f + 0.58f * std::max(hv, act * 0.6f);
        sessDrawKebab(dl, ImVec2(kbMin.x + g.kebabSz * 0.5f, kbMin.y + g.kebabSz * 0.5f),
                      g.kebabSz,
                      sessInk(ImLerp(surf.textSecondary, surf.textPrimary, kv), std::max(restA, kv)));

        if (kebabHover) {
            ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
            ImGui::SetTooltip("Session actions");
        } else if (chipHit) {
            ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
            ImGui::SetTooltip("%s", badgeTab >= 0 ? "Jump to this document's tab"
                                                  : "Document not open");
        } else if (pickHover) {
            ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
            if (titleShown != title) ImGui::SetTooltip("%s", title.c_str());
        }
    }

    // Overflow menu: every session action, on every row. Right-clicking the row opens it too.
    ui::pushContextMenuStyle();
    if (ImGui::BeginPopup("##sessrow_menu")) {
        constexpr const char* kAfterThisRun = "Available when this session's run finishes.";
        if (ImGui::MenuItem("Rename")) doRename = true;
        if (ImGui::MenuItem("Fork")) doFork = true;
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Fork this session into a new idle copy");
        if (ImGui::MenuItem(archLabel, nullptr, false, !running)) doArchive = true;
        if (running && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
            ImGui::SetTooltip("%s", kAfterThisRun);
        else if (!running && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", c.archived
                ? "Restore to the active sessions list"
                : "Archive: hides from the default list but keeps the full transcript (unlike "
                  "Delete). Turn on \"Archived\" above to find it again.");
        if (ImGui::MenuItem(s_ccm.convId == c.id ? "Hide recovery" : "Manage")) doManage = true;
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Session recovery: export, branch, and close runtime");
        ImGui::Separator();
        // Destructive: opens the row's own confirmation; nothing is deleted from the menu itself.
        if (ImGui::MenuItem("Delete", nullptr, false, !running)) s_delConfirm = c.id;
        if (running && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
            ImGui::SetTooltip("%s", kAfterThisRun);
        ImGui::EndPopup();
    }
    ui::popContextMenuStyle();
    if (doRename) {
        s_rename = SessRename{};
        s_rename.id = c.id;
        std::snprintf(s_rename.buf, sizeof(s_rename.buf), "%s",
                      ui::copilot::stripInlineImageMarkers(c.title).c_str());
    }

    ImGui::PopID();
    // Destructive row actions are blocked only for the row whose own run is live — an unrelated
    // background run never locks them.
    out.del = doDel && !running;
    out.archive = doArchive && !running;
    out.fork = doFork;
    out.manage = doManage;
    return out;
}
}  // namespace

SessionsScopeProbe sessionsScopeProbe() {
    SessionsScopeProbe p;
    p.drawn       = s_sessScope.seeded;
    p.followedKey = s_sessScope.followedKey;
    if (s_sessScope.scope == SessScope::Project) p.viewedKey = s_sessScope.key;
    return p;
}


// Sessions list — replaces the chat log while open so users can jump between sessions easily.
void drawSessionsPanel(AppState& st, bool busy) {
    (void)busy;  // runs are per-conversation; this panel never locks on the viewed run
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();

    ensureConversations(st);

    // One title treatment for every copilot menu (ui_common cpMenu*). The project dropdown and
    // the archived toggle share ONE toolbar row in the header's accessory slot — above the rule,
    // under the title — instead of full body rows between the title and the list.
    static bool s_showArchived = false;
    SessScopeState& scope = s_sessScope;
    const std::string activeDir = convProjectDir(st);
    const std::string activeKey = convProjectKey(activeDir);
    if (!scope.seeded || activeKey != scope.followedKey) {
        // The active project changed (or first draw): the list follows it.
        scope.seeded      = true;
        scope.followedKey = activeKey;
        scope.scope       = activeKey.empty() ? SessScope::Unassigned : SessScope::Project;
        scope.key         = activeKey;
        scope.dir         = activeDir;
    }
    int unassigned = 0;
    const std::vector<SessProjectEntry> projects =
        sessProjects(st, s_showArchived, activeKey, activeDir, unassigned);
    ui::cpMenuHeader(st.fonts, "Sessions", nullptr, [&]{
        drawSessionsToolbar(surf, projects, unassigned, activeKey, &s_showArchived);
    });
    const bool allProjects = scope.scope == SessScope::All;
    // Peeking at another project's sessions: offer the real project switch (the same path as
    // the title bar / Explorer "Set as working folder"), but keep the user on this surface.
    const bool peeking = scope.scope == SessScope::Project && scope.key != activeKey;
    std::string peekName;
    if (peeking) {
        peekName = sessProjectName(scope.dir);
        const std::string hint = "Viewing " + peekName + " \xe2\x80\x94 not the active project.";
        ui::cpMenuHint(hint.c_str());
        const std::string btn = "Switch active project to " + peekName;
        if (ui::kit::secondaryButton(ui::kit::elide(btn.c_str(),
                                         ImGui::GetContentRegionAvail().x).c_str())) {
            const AppState::Workspace keepWs = st.activeWorkspace;
            setActiveProjectDir(st, scope.dir);
            st.activeWorkspace = keepWs;   // switching projects must not yank the user to PLM
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", scope.dir.c_str());
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
    }

    // Starting a session from a populated list is the pinned header's "New chat" (row 2, next to
    // the Sessions toggle); this panel only offers it from the empty state. The full AI workspace
    // carries New session in its own chat header.

    // Row shape/rhythm from the shared copilot-menu tokens, so session rows, artifact cards and
    // task rows carry ONE corner, ONE inner padding and ONE gap. Every row is the same height:
    // padding + a medium-weight title line + a meta line.
    SessRowStyle row;
    row.padX      = ui::metrics::cpMenuRowPadX();
    row.rowRndDp  = ui::metrics::cpMenuRowRound() / sc;   // glassRowBackground scales internally
    row.titleFont = st.fonts.medium;
    if (row.titleFont) ImGui::PushFont(row.titleFont);
    row.titleH    = ImGui::GetTextLineHeight();
    if (row.titleFont) ImGui::PopFont();
    row.metaH     = ImGui::GetTextLineHeight();
    row.lineGap   = ui::metrics::px(2.0f);
    row.rowH      = ui::metrics::cpMenuRowPadY() * 2.0f + row.titleH + row.lineGap + row.metaH;
    row.kebabSz   = std::min(row.rowH - ui::metrics::px(12.0f),
                             std::max(ImGui::GetFrameHeight(), ui::metrics::px(26.0f)));
    row.kebabEdge = ui::metrics::px(6.0f);
    row.actionW   = row.kebabSz + row.kebabEdge + ui::metrics::px(4.0f);
    row.allProjects = allProjects;
    const float rowGap = ui::metrics::cpMenuRowGap();

    // Project scope comes from the session's OWN stored projectDir (captured at creation,
    // restamped per turn), never from a live scan of what it produced. Only the viewed
    // project's sessions are listed; sessions with no stored project appear ONLY under
    // "Unassigned" (they used to leak into every project's list).
    const std::string viewKey = scope.scope == SessScope::Unassigned ? std::string() : scope.key;
    std::vector<int> scoped;   // slots in the viewed project / archived state, newest-created first
    for (int i = (int)st.conversations.size() - 1; i >= 0; --i) {
        const Conversation& c = st.conversations[i];
        if (c.archived != s_showArchived || (!allProjects && convProjectKey(c) != viewKey))
            continue;
        scoped.push_back(i);
    }

    // The filter field only appears once the list is long enough to need it — and never hides
    // while it is still filtering.
    if (scoped.size() > 8 || s_filter[0] != '\0') {
        ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::cpMenuChipRound());
        ImGui::InputTextWithHint("##sess_filter", "Filter sessions\xe2\x80\xa6", s_filter,
                                 sizeof(s_filter), ImGuiInputTextFlags_EscapeClearsAll);
        ImGui::PopStyleVar();
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
    }
    std::string needle;
    for (const char* p = s_filter; *p; ++p) needle += (char)std::tolower((unsigned char)*p);

    std::vector<int> visibleSessions;
    for (const int i : scoped)
        if (sessMatchesFilter(sessDisplayTitle(st.conversations[i], i), needle))
            visibleSessions.push_back(i);

    // Most recently active first, grouped by calendar day. Activity is the LAST message's time;
    // the open session with nothing said yet counts as now, so a fresh chat leads the list. The
    // sort is stable: sessions with equal (or unknown) times keep newest-created-first order.
    const double now = chatNowEpoch();
    auto lastTs = [&](int i) -> double {
        const std::vector<ChatMsg>& m = (i == st.activeConv) ? st.chat : st.conversations[i].msgs;
        if (m.empty()) return i == st.activeConv ? now : 0.0;
        return std::max(0.0, m.back().ts);
    };
    std::stable_sort(visibleSessions.begin(), visibleSessions.end(),
                     [&](int a, int b) { return lastTs(a) > lastTs(b); });
    const SessDays days = sessDayStarts();
    std::vector<double> tsOf(visibleSessions.size());
    std::vector<int> bucketOf(visibleSessions.size());
    int bucketsSeen = 0;
    for (size_t k = 0; k < visibleSessions.size(); ++k) {
        tsOf[k] = lastTs(visibleSessions[k]);
        bucketOf[k] = sessBucket(tsOf[k], days);
        bucketsSeen |= 1 << bucketOf[k];
    }
    // Group headers only earn their space when the list actually spans more than one group.
    const bool grouped = (bucketsSeen & (bucketsSeen - 1)) != 0;

    if (visibleSessions.empty()) {
        if (!scoped.empty()) {
            ui::kit::emptyState("history", "No matching sessions",
                                "Try part of a session title.");
            return;
        }
        std::string title, body;
        if (s_showArchived) {
            title = "No archived sessions";
            body  = "Archive a session from its menu to keep its transcript without cluttering "
                    "this list.";
        } else if (scope.scope == SessScope::Unassigned) {
            title = "No unassigned sessions";
            body  = "Sessions that never recorded a project folder show up here.";
        } else if (allProjects) {
            title = "No sessions yet";
            body  = "Start a chat and it is saved here.";
        } else if (peeking) {
            title = "No sessions in " + peekName;
            body  = "Switch to this project to start one.";
        } else {
            title = "No sessions in this project";
            body  = "Start a chat \xe2\x80\x94 it is saved here so you can come back to it.";
        }
        // New chat lands in the ACTIVE project, so it is only offered where the result would
        // show up in the list the user is looking at.
        const bool canStart = !s_showArchived && !peeking && scope.scope != SessScope::Unassigned;
        if (ui::kit::emptyState("history", title.c_str(), body.c_str(),
                                canStart ? "New chat" : nullptr)) {
            cpStartNewChat(st);
            cpSetTab(st, CpTab::Chat);
        }
        return;
    }

    // Rows are placed from a running `y`, not ImGui's cursor, and only the rows on screen are
    // submitted (skipped ones advance `y` by their exact height, so the scroll extent is stable).
    // Rows that must stay alive while scrolled away — the open recovery card, an inline rename or
    // delete prompt, an open overflow menu — are always submitted.
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float x0 = ImGui::GetCursorScreenPos().x;
    const float rowW = ImGui::GetContentRegionAvail().x;
    float y = ImGui::GetCursorScreenPos().y;
    bool listChanged = false;   // a row action mutated st.conversations: indices below are stale
    int lastBucket = -1;
    for (size_t k = 0; k < visibleSessions.size(); ++k) {
        const int i = visibleSessions[k];
        const int bucket = bucketOf[k];
        if (grouped && bucket != lastBucket) {
            // Group label with a faint rule running out to the row edge.
            const float headTop = lastBucket < 0 ? 0.0f : ui::metrics::px(10.0f);
            const float headH = headTop + row.metaH + ui::metrics::px(4.0f);
            if (!listChanged &&
                ImGui::IsRectVisible(ImVec2(x0, y), ImVec2(x0 + rowW, y + headH))) {
                const char* label = kSessBucketLabel[bucket];
                const float lx = x0 + row.padX;
                const float ly = y + headTop;
                ImVec4 ink = surf.textTertiary;
                ink.w = 1.0f;
                dl->AddText(ImVec2(lx, ly), ImGui::ColorConvertFloat4ToU32(ink), label);
                const float ruleX = lx + ImGui::CalcTextSize(label).x + ui::metrics::px(8.0f);
                const float ruleEnd = x0 + rowW - row.padX;
                if (ruleEnd > ruleX)
                    dl->AddLine(ImVec2(ruleX, ly + row.metaH * 0.5f),
                                ImVec2(ruleEnd, ly + row.metaH * 0.5f),
                                sessInk(surf.textTertiary, 0.35f), ui::metrics::stroke(1.0f));
            }
            y += headH;
        }
        lastBucket = bucket;
        // After a list action the remaining rows are only measured, never submitted, so the
        // window keeps its full extent for the frame and the scroll position is not clamped.
        if (listChanged) {
            y += row.rowH + rowGap;
            continue;
        }

        const Conversation& c = st.conversations[i];
        const bool pinned = s_ccm.convId == c.id || s_rename.id == c.id ||
                            s_delConfirm == c.id || s_menuConv == c.id;
        if (!pinned && !ImGui::IsRectVisible(ImVec2(x0, y), ImVec2(x0 + rowW, y + row.rowH))) {
            y += row.rowH + rowGap;
            continue;
        }
        const SessRowResult res = drawSessionRow(st, i, row, ImVec2(x0, y), rowW, tsOf[k], surf);
        y += row.rowH + rowGap;
        ImGui::SetCursorScreenPos(ImVec2(x0, y));
        // Toggle + draw the recovery card directly under its own row (it persists while the
        // pointer leaves the row — only the toggle is an action, not the card).
        if (res.manage) {
            if (s_ccm.convId == c.id) s_ccm.close();
            else s_ccm.open(c.id);
        }
        if (s_ccm.convId == c.id) {
            drawOmpManageCard(st, c, res.running);
            y = ImGui::GetCursorScreenPos().y;
        }
        if (res.del) {
            s_delConfirm.clear();
            closeConversation(st, i);
            listChanged = true;
        } else if (res.fork) {
            forkConversation(st, i);
            listChanged = true;
        } else if (res.archive) {
            archiveConversation(st, i, !c.archived);
            listChanged = true;
        }
    }
    // An item at the end of the list validates the window extent the skipped rows never claimed.
    ImGui::SetCursorScreenPos(ImVec2(x0, y));
    ImGui::Dummy(ImVec2(rowW, 0.0f));
}

// PlanItem::status -> the app's one status vocabulary.
static ui::kit::Status planItemStatus(const std::string& s) {
    if (s == "done" || s == "pass") return ui::kit::Status::Done;
    if (s == "running")             return ui::kit::Status::Running;
    if (s == "fail" || s == "failed" || s == "error") return ui::kit::Status::Failed;
    if (s == "abandoned" || s == "stopped" || s == "interrupted" || s == "cancelled")
        return ui::kit::Status::Skipped;
    return ui::kit::Status::Pending;
}

// Nesting depth along the parent_id chain. Bounded: a malformed plan event could describe a cycle.
static int planDepth(const PlanState& ps, const PlanItem& item) {
    int depth = 0;
    std::string parent = item.parent_id;
    for (int guard = 0; guard < 8 && !parent.empty(); ++guard) {
        const PlanItem* p = nullptr;
        for (const auto& c : ps.items)
            if (c.id == parent) { p = &c; break; }
        if (!p) break;
        ++depth;
        parent = p->parent_id;
    }
    return depth;
}

// One plan item. `parent_id` (indent) and `desc` (second line) were both parsed and persisted and
// neither reached this panel before, so a one-word subtask sat at the same level as its parent
// with no elaboration anywhere in the surface.
static void planTaskRow(const PlanState& ps, const PlanItem& pi, int index, bool running) {
    const ui::kit::Status s = pi.status == "running" && !running
                                  ? ui::kit::Status::Skipped : planItemStatus(pi.status);
    const float ind = (float)planDepth(ps, pi) * ui::metrics::U(3.0f);
    if (ind > 0.0f) ImGui::Indent(ind);

    const bool  hasDesc = !pi.desc.empty();
    const float lineH   = ImGui::GetTextLineHeight();
    // The second line is a separate ImGui item, so the gap the group WILL emit is ItemSpacing.y —
    // reserve exactly that or the desc spills past the row background.
    const float lineGap = ImGui::GetStyle().ItemSpacing.y;
    const float h       = lineH + (hasDesc ? lineGap + lineH : 0.0f) + ui::kit::padY() * 2.0f;

    ImGui::PushID(index);
    ui::kit::rowBegin("task", h);
    const bool hovered = ImGui::IsItemHovered();

    // Running IS the brand mark in motion, so the spinner REPLACES the static glyph on the live
    // item rather than sitting next to a second copy of it.
    if (s == ui::kit::Status::Running) ui::kit::spinner("");
    else                               ui::kit::statusGlyphInline(s);
    ImGui::SameLine(0.0f, ui::metrics::U(1.0f));

    // BeginGroup pins the line start to the text column, so the desc lines up under the title
    // instead of wrapping back under the status mark.
    ImGui::BeginGroup();
    const float textW = ImMax(ImGui::GetContentRegionAvail().x - ui::kit::padX(),
                              ui::metrics::stroke(1.0f));
    const std::string title = pi.title.empty() ? std::string("(task)") : pi.title;
    ImGui::TextUnformatted(ui::kit::elide(title.c_str(), textW).c_str());
    if (hasDesc) ui::kit::dimText(ui::kit::elide(pi.desc.c_str(), textW).c_str());
    ImGui::EndGroup();
    ui::kit::rowEnd();
    ImGui::PopID();

    if (hovered)
        ImGui::SetTooltip("%s%s%s", title.c_str(), hasDesc ? "\n" : "", pi.desc.c_str());
    if (ind > 0.0f) ImGui::Unindent(ind);
}

// Task-list panel — replaces the chat log; the deterministic plan/checklist for the
// active conversation's most recent turn (statuses from engine events, not the model).
static void drawTaskListPanel(AppState& st, bool busy) {
    (void)busy;
    const PlanState* ps = nullptr;
    bool planRunning = false;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (!it->fromUser && planIsReal(it->planState)) {
            ps = &it->planState;
            planRunning = it->running;
            break;
        }

    // The trace knows lifecycle outcomes; spawn-only plan rows never receive them. Use the
    // SAME source as the overview and AI roster, so "done/failed/stopped" cannot disagree.
    static std::vector<AgentTraceAgent> agents;
    agentTraceSubagents(st, st.activeConvId(), agents);
    std::vector<std::string> subs;
    subs.reserve(agents.size());
    for (const auto& a : agents) subs.push_back(a.name + " \xc2\xb7 " + a.status);
    if (agents.empty()) {
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it) {
            if (it->fromUser) continue;
            for (const auto& pi : it->planState.items) {
                if (pi.kind != "subagent") continue;
                const bool terminal = pi.status == "done" || pi.status == "pass" ||
                    pi.status == "fail" || pi.status == "failed" || pi.status == "error" ||
                    pi.status == "cancelled" || pi.status == "abandoned";
                const std::string status = !it->running && !terminal ? "stopped" : pi.status;
                subs.push_back(pi.title + (status.empty() ? "" : (" \xc2\xb7 " + status)));
            }
            if (!subs.empty()) break;
        }
    }

    int done = 0, total = 0;
    copilotTaskProgress(st, done, total);

    const int revision = ps ? ps->revision : 0;
    ui::cpMenuHeader(st.fonts,
                     ps && !ps->title.empty() ? ps->title.c_str() : "Task list", nullptr,
                     revision > 0 ? std::function<void()>([revision]() {
                         char rev[32];
                         std::snprintf(rev, sizeof(rev), "revised x %d", revision);
                         ui::kit::chip(rev, ui::kit::Tone::Info);
                     })
                                  : std::function<void()>());
    if (ps && !ps->summary.empty()) {
        ui::cpMenuHint(ps->summary.c_str());
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
    }
    // A bare "3 of 7 complete" shows no trend; the bar does, and it sweeps when a step lands.
    if (ps && total > 0) {
        char caption[48];
        std::snprintf(caption, sizeof(caption), "%d of %d complete", done, total);
        ui::kit::progressBar((float)done / (float)total, caption,
                             done >= total ? ui::kit::Tone::Ok : ui::kit::Tone::Accent);
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
    }

    if (!ps) {
        ui::kit::emptyState("plan_board", "No task list yet",
                            "Start a build and the agent's plan appears here.");
    } else {
        // A shape=phased_todo plan carries a phase per item and used to lose it entirely.
        std::vector<std::string> phases;
        bool anyPhase = false;
        for (const auto& pi : ps->items) {
            if (pi.kind == "subagent") continue;
            if (!pi.phase.empty()) anyPhase = true;
            if (std::find(phases.begin(), phases.end(), pi.phase) == phases.end())
                phases.push_back(pi.phase);
        }
        if (!anyPhase) phases.assign(1, std::string());

        int  index       = 0;
        bool firstHeader = true;
        for (const std::string& ph : phases) {
            int count = 0;
            for (const auto& pi : ps->items)
                if (pi.kind != "subagent" && (!anyPhase || pi.phase == ph)) ++count;
            if (count == 0) continue;
            if (anyPhase) {
                ui::kit::sectionHeader(ph.empty() ? "Unphased" : ph.c_str(), nullptr, nullptr,
                                       count, firstHeader);
                firstHeader = false;
            }
            for (const auto& pi : ps->items) {
                if (pi.kind == "subagent") continue;
                if (anyPhase && pi.phase != ph) continue;
                planTaskRow(*ps, pi, index++, planRunning);
            }
        }
    }

    ui::kit::sectionHeader("Subagents", "ai_panel", nullptr, (int)subs.size());
    if (subs.empty()) {
        ui::kit::emptyState(nullptr, "No subagents recorded",
                            "Spawned helpers and their run outcomes appear here.");
        return;
    }

    // Subagent rows go through ui::kit::row like every other list row in the app: the hand-rolled
    // version here painted its own glass background, its own hover wash and a generic 2.2px dot
    // that carried no information — the same placeholder the artifact rows used, which is exactly
    // the duplication the kit exists to end. The leading slot now says what the row IS.
    ImGui::PushID("task_subagents");
    for (size_t i = 0; i < subs.size(); ++i) {
        ui::kit::RowStyle rs;
        rs.icon     = "ai_panel";
        rs.iconTone = ui::kit::Tone::Accent;
        rs.tooltip  = subs[i].c_str();
        ui::kit::row(std::to_string(i).c_str(), subs[i].c_str(), rs);
    }
    ImGui::PopID();
}

// ============================ Session Artifacts presentation ============================
// The Artifacts tab is an asset BROWSER for what the agent produced this session, grouped by KIND:
// Models / Drawings / Images / Data are collapsible sections and a chip row narrows the view to
// one kind. Images are a responsive thumbnail grid; everything else is a list of uniform rows
// (kind plate, name, a quiet "what . size" line, age) whose actions fade in over the age on hover
// and repeat in the right-click menu. Rows and tiles are painted straight onto the pane's glass in
// the copilot's adaptive ink, so they stay legible over any viewport backdrop in both themes.

// What an artifact IS. Doubles as the section it lands in and (+1) as its filter chip index.
enum ArtiBucket { ArtiModels = 0, ArtiDrawings, ArtiImages, ArtiData };
static constexpr int kArtiKinds    = 4;
// The user's own open documents are a fifth, collapsed-by-default section after the four kinds.
static constexpr int kArtiOpenSec  = kArtiKinds;
static constexpr int kArtiSections = kArtiKinds + 1;

// What an artifact IS — resolved once, then reused for the leading icon, the secondary line and the
// section it lands in, so a row's section can never disagree with the kind its icon claims.
struct ArtiClass {
    const char* icon   = "file_generic";
    const char* what   = "File";
    ArtiBucket  bucket = ArtiData;
};

// One listed artifact. `open` is the row's activation (an image row views the image full size).
struct ArtiDocRow {
    std::string key;                  // stable ImGui / animation id
    std::string label;
    std::string path;                 // the file on disk; empty for a document with no file yet
    std::string revealPath;           // "show in file manager" target (an unsaved doc: the project folder)
    std::function<void()> open;
    ArtiClass   cls;
    bool        doc    = false;       // a document tab rather than a produced file
    bool        live   = false;       // an agent is editing this document right now
    bool        closed = false;       // view closed while the agent kept building: the only action is Reopen
    bool        own    = false;       // opened by the user and never touched by the agent
};

static std::string artiLower(std::string s) {
    for (char& c : s) c = (char)std::tolower((unsigned char)c);
    return s;
}

static ArtiClass artiClassify(const std::string& path, const std::string& label) {
    ArtiClass c;
    const size_t slash = path.find_last_of("/\\");
    const size_t dot   = path.find_last_of('.');
    std::string ext;
    if (dot != std::string::npos && (slash == std::string::npos || dot > slash))
        ext = artiLower(path.substr(dot + 1));

    static const struct {
        const char* ext; const char* icon; const char* what; ArtiBucket bucket;
    } kMap[] = {
        {"step+",  "file_step",    "CAD document",       ArtiModels},
        {"aicm",   "file_step",    "CAD model",          ArtiModels},
        {"draw+",  "file_doc",     "Drawing document",   ArtiDrawings},
        {"kicad_pro", "file_data", "KiCad project",       ArtiModels},
        {"kicad_sch", "file_data", "KiCad schematic",     ArtiModels},
        {"kicad_pcb", "file_data", "KiCad board",         ArtiModels},
        {"step",   "file_step",    "STEP export",        ArtiModels},
        {"stp",    "file_step",    "STEP export",        ArtiModels},
        {"iges",   "file_step",    "IGES export",        ArtiModels},
        {"igs",    "file_step",    "IGES export",        ArtiModels},
        {"sldprt", "file_step",    "CAD part",           ArtiModels},
        {"x_t",    "file_step",    "Parasolid export",   ArtiModels},
        {"stl",    "file_mesh",    "STL mesh",           ArtiModels},
        {"obj",    "file_mesh",    "OBJ mesh",           ArtiModels},
        {"ply",    "file_mesh",    "PLY mesh",           ArtiModels},
        {"3mf",    "file_mesh",    "3MF print file",     ArtiModels},
        {"png",    "file_image",   "Render",             ArtiImages},
        {"jpg",    "file_image",   "Render",             ArtiImages},
        {"jpeg",   "file_image",   "Render",             ArtiImages},
        {"webp",   "file_image",   "Render",             ArtiImages},
        {"pdf",    "file_doc",     "PDF document",       ArtiDrawings},
        {"dxf",    "file_doc",     "DXF drawing",        ArtiDrawings},
        {"dwg",    "file_doc",     "DWG drawing",        ArtiDrawings},
        {"svg",    "file_doc",     "SVG drawing",        ArtiDrawings},
        {"md",     "file_doc",     "Notes",              ArtiData},
        {"txt",    "file_doc",     "Text file",          ArtiData},
        {"docx",   "file_doc",     "Word document",      ArtiData},
        {"json",   "file_data",    "JSON data",          ArtiData},
        {"csv",    "file_data",    "CSV table",          ArtiData},
        {"xml",    "file_data",    "XML data",           ArtiData},
        {"yaml",   "file_data",    "YAML data",          ArtiData},
        {"yml",    "file_data",    "YAML data",          ArtiData},
        {"py",     "file_code",    "Python script",      ArtiData},
        {"cpp",    "file_code",    "C++ source",         ArtiData},
        {"js",     "file_code",    "JavaScript source",  ArtiData},
        {"ts",     "file_code",    "TypeScript source",  ArtiData},
        {"zip",    "file_archive", "Archive",            ArtiData},
        {"tar",    "file_archive", "Archive",            ArtiData},
        {"7z",     "file_archive", "Archive",            ArtiData},
        {"gz",     "file_archive", "Archive",            ArtiData},
    };
    for (const auto& m : kMap) {
        if (ext != m.ext) continue;
        c.icon = m.icon; c.what = m.what; c.bucket = m.bucket;
        break;
    }
    // A CSV the agent produced is almost always the BOM; say so rather than "CSV table".
    if (ext == "csv" && artiLower(label).find("bom") != std::string::npos)
        c.what = "Bill of materials";
    return c;
}

// A document tab's kind: the leading icon and the "what it is" line of its row.
static ArtiClass artiDocClass(const AppState::DocTab& t) {
    using K = AppState::DocTab::TabKind;
    ArtiClass c;
    c.bucket = ArtiModels;
    switch (t.kind) {
        case K::Assembly:
            c.icon = "assembly";
            c.what = "Assembly";
            break;
        case K::Drawing:
            c.icon   = "ws_drawing";
            c.what   = "Drawing";
            c.bucket = ArtiDrawings;
            break;
        case K::Schematic:
            c.icon = "schematic";
            c.what = "Schematic";
            break;
        case K::PCB:
            c.icon = "ecad_pcb";
            c.what = "PCB layout";
            break;
        case K::Part:
        default:
            c.icon = "part";
            c.what = "Part";
            break;
    }
    return c;
}

// Size + age for the row's meta column, from ONE cached stat.
struct ArtiStat {
    unsigned long long size  = 0;
    double             mtime = 0.0;   // unix seconds; 0 = unknown
    bool               ok    = false;   // the file exists and was readable at the last sample
    bool               known = false;   // at least one sample has landed (the first pass is paced)
};

// BOUNDED stat cache: the panel repaints every frame and stat()ing 50 artifact paths per frame is
// real syscall cost for numbers that move at most once a second. 128 resident paths,
// least-recently-used evicted; each entry re-stat'ed after kStatTtl so a file the agent is still
// writing settles to its final size instead of freezing at its first sample.
static ArtiStat artiStatOf(const std::string& path) {
    struct Slot { ArtiStat st; double lastUse = 0.0; double taken = -1.0e9; };
    static std::unordered_map<std::string, Slot> s_cache;
    static constexpr size_t kMaxResident = 128;
    static constexpr double kStatTtl     = 2.0;
    static int refreshedFrame = -1;
    const double now = ImGui::GetTime();

    auto it = s_cache.find(path);
    if (it == s_cache.end()) {
        if (s_cache.size() >= kMaxResident) {
            auto oldest = s_cache.begin();
            for (auto i = s_cache.begin(); i != s_cache.end(); ++i)
                if (i->second.lastUse < oldest->second.lastUse) oldest = i;
            s_cache.erase(oldest);
        }
        it = s_cache.emplace(path, Slot{}).first;
    }
    Slot& s = it->second;
    if (now - s.taken >= kStatTtl && refreshedFrame != ImGui::GetFrameCount()) {
        refreshedFrame = ImGui::GetFrameCount();
        s.st.known = true;
        namespace fs = std::filesystem;
        std::error_code ec;
        const auto sz = fs::file_size(path, ec);
        s.st.ok = !ec;
        if (!ec) {
            s.st.size = (unsigned long long)sz;
            std::error_code tec;
            const auto ft = fs::last_write_time(path, tec);
            if (!tec) {
                // file_clock has no portable epoch; rebase it through both clocks' "now".
                const auto sysT = std::chrono::time_point_cast<std::chrono::system_clock::duration>(
                    ft - fs::file_time_type::clock::now() + std::chrono::system_clock::now());
                s.st.mtime = std::chrono::duration<double>(sysT.time_since_epoch()).count();
            }
        }
        s.taken = now + (std::hash<std::string>{}(path) % 1000) * 0.001;
    }
    s.lastUse = now;
    // First samples are paced one per frame; keep frames coming until every row has its own.
    if (!s.st.known) ui::anim::requestFrames();
    return s.st;   // by value: the next call may evict/rehash this slot
}

// Fade everything a widget just painted. The kit resolves its colors from ui::palette directly,
// so ImGuiStyleVar_Alpha cannot reach it — scale the alpha of the vertices it emitted instead.
static void artiFadeVerts(ImDrawList* dl, int firstVtx, float a) {
    const float f = ImClamp(a, 0.0f, 1.0f);
    for (int i = firstVtx; i < dl->VtxBuffer.Size; ++i) {
        ImU32& c        = dl->VtxBuffer.Data[i].col;
        const ImU32 al  = (c >> IM_COL32_A_SHIFT) & 0xFFu;
        c = (c & ~IM_COL32_A_MASK) | ((ImU32)((float)al * f) << IM_COL32_A_SHIFT);
    }
}

// Entrance stagger: rows land as a wave. The index is CAPPED so a 200-row list does not spend
// four seconds arriving — past the cap every remaining row shares the last slot's timing.
static constexpr int kArtiStaggerCap = 12;
static float artiEnterAlpha(double t0, int index) {
    return ui::anim::stagger(t0, index < kArtiStaggerCap ? index : kArtiStaggerCap,
                             ui::anim::kSlow);
}

// Grid tiles decode at half the transcript's edge: a tile is at most ~300 physical px wide and a
// full-size view goes through the lightbox. No decode on the GL thread.
static Tex artiRenderThumb(const std::string& path) {
    Tex tex;
    tex.id = (unsigned int)(intptr_t)ui::copilot::asyncImageTexture(path, 512, &tex.w, &tex.h);
    return tex;
}

// Filter / disclosure state for the Artifacts tab. These lived in file statics, so a filter typed
// in one session was still applied — invisibly, because the field only appears past eight rows —
// after switching to another. Keyed to the viewed conversation and reset with it.
struct ArtiUiState {
    std::string convId;
    char        filter[192]           = "";
    int         kind                  = 0;   // 0 = every kind, else ArtiBucket + 1
    bool        open[kArtiSections]   = {true, true, true, true, false};   // section disclosure
    double      openT0[kArtiSections] = {};  // when each section last expanded (row entrance origin)
    double      enterT0               = 0.0; // stagger origin for the whole panel's entrance
    int         lastFrame             = -2;
};
static ArtiUiState& artiUi(const AppState& st) {
    static ArtiUiState s;
    const std::string id    = st.activeConvId();
    const int         frame = ImGui::GetFrameCount();
    // A gap in the frame numbers means the tab was closed and reopened — replay the entrance.
    if (id != s.convId || frame != s.lastFrame + 1) {
        if (id != s.convId) {
            s = ArtiUiState{};
            s.convId = id;
        }
        s.enterT0 = ImGui::GetTime();
    }
    s.lastFrame = frame;
    return s;
}

// ImU32 of a palette role / an adaptive ink at `a` alpha (the kit's own helper is private to kit.cpp).
static ImU32 artiRole(const float c[4], float a = 1.0f) {
    return ImGui::ColorConvertFloat4ToU32(ImVec4(c[0], c[1], c[2], c[3] * a));
}
static ImU32 artiInk(const ImVec4& c, float a = 1.0f) {
    return ImGui::ColorConvertFloat4ToU32(ImVec4(c.x, c.y, c.z, c.w * a));
}

// A named atlas glyph centred at `c`. Line-art glyphs take the theme's text role; the raster
// file-kind tiles keep their baked colors (white tint), as the doc-tab strip does.
static void artiPaintIcon(ImDrawList* dl, const char* name, ImVec2 c, float sz, float alpha = 1.0f) {
    if (!name || !name[0] || sz <= 0.0f) return;
    if (ui::hasIconSdf(name)) {
        ui::drawIconGlass(dl, name, c, sz, artiRole(ui::palette.text, alpha), 0);
        return;
    }
    const ui::IconUV ic = ui::getIcon(name, sz >= ui::metrics::iconHeader() ? ui::kIconCellLarge
                                                                            : ui::kIconCellSmall);
    if (!ic.valid) return;
    const float r = sz * 0.5f;
    dl->AddImage(ic.texId, ImVec2(c.x - r, c.y - r), ImVec2(c.x + r, c.y + r), ic.uv0, ic.uv1,
                 IM_COL32(255, 255, 255, (int)(255.0f * ImClamp(alpha, 0.0f, 1.0f))));
}

// Row geometry shared by artiRow and the list clipper in drawArtifactsPanel: two text lines inside
// an inset, so every row is the same height whatever it holds.
static float artiRowInset() { return ui::metrics::U(1.25f); }
static float artiRowHeight() {
    return ImGui::GetTextLineHeight() * 2.0f + ui::metrics::U(0.5f) + artiRowInset() * 2.0f;
}

// What the row's primary action does, for its tooltip and its menu entry.
static const char* artiOpenVerb(const ArtiDocRow& r) {
    if (r.closed) return "Reopen live view";
    if (r.cls.bucket == ArtiImages) return "View full size";
    return r.doc ? "Switch to document" : "Open";
}

static void artiCopyPath(AppState& st, const std::string& path) {
    ImGui::SetClipboardText(path.c_str());
    st.toast(ui::ToastLevel::Info, "Path copied to the clipboard");
}

// The right-click menu shared by rows and tiles. Call inside the item's PushID scope; the caller
// opens "##arti_ctx" on a right click of its hit item.
static void artiContextMenu(AppState& st, const ArtiDocRow& r) {
    ui::pushContextMenuStyle();
    if (ImGui::BeginPopup("##arti_ctx")) {
        if (r.open && ImGui::MenuItem(artiOpenVerb(r))) r.open();
        if (r.cls.bucket == ArtiImages && ImGui::MenuItem("Open with default app"))
            chatArtifactOpenSmart(st, r.path);
        if (!r.revealPath.empty() && ImGui::MenuItem("Show in file manager"))
            chatArtifactReveal(st, r.revealPath);
        if (!r.path.empty() && ImGui::MenuItem("Copy path")) artiCopyPath(st, r.path);
        ImGui::EndPopup();
    }
    ui::popContextMenuStyle();
}

// One artifact row, painted onto the glass in the copilot's adaptive ink:
//
//   [ plate ]  Name (medium)                                   3 min ago    <- at rest
//   [  icon ]  STEP export . 2.4 MB                         [open][dir][copy] <- on hover
//
// The right lane is ONE slot that crossfades between the age and the action buttons, so hovering
// never reflows the text (the text column is sized for the wider of the two). Everything is laid
// out from the same computed geometry, so plate, both text lines and the buttons share a centre.
static void artiRow(AppState& st, const ArtiDocRow& r, int index, double t0) {
    namespace M = ui::metrics;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImDrawList* dl = ImGui::GetWindowDrawList();

    const ArtiStat stat    = r.path.empty() ? ArtiStat{} : artiStatOf(r.path);
    const bool     missing = !r.doc && stat.known && !stat.ok;

    // Quiet second line: what it is, then how big (or why there is no size to show).
    std::string meta = r.cls.what;
    if (r.closed)                     meta += " \xc2\xb7 closed, the agent may still be building";
    else if (missing)                 meta += " \xc2\xb7 file not found";
    else if (stat.ok)                 meta += " \xc2\xb7 " + ui::kit::fmtBytes(stat.size);
    else if (r.doc && r.path.empty()) meta += " \xc2\xb7 not saved yet";
    // The lane's resting text: a live document says so, a file says how long ago it landed.
    const std::string rest = r.live ? std::string("Editing")
                                    : (stat.ok ? ui::kit::fmtAgo(stat.mtime) : std::string());

    const float lineH     = ImGui::GetTextLineHeight();
    const float inset     = artiRowInset();
    const float rowH      = artiRowHeight();
    const float tile      = rowH - inset * 2.0f;
    const float btn       = lineH + M::U(1.5f);
    const float btnGap    = M::U(0.5f);
    const bool  canReveal = !r.revealPath.empty();
    const bool  canCopy   = !r.path.empty();
    const int   nAct      = 1 + (canReveal ? 1 : 0) + (canCopy ? 1 : 0);
    const float actionsW  = btn * (float)nAct + btnGap * (float)(nAct - 1);
    const float dotR      = M::U(0.5f);
    const float dotGap    = M::U(1.0f);
    const float restW     = rest.empty() ? 0.0f
                          : ImGui::CalcTextSize(rest.c_str()).x +
                                (r.live ? dotR * 2.0f + dotGap : 0.0f);
    const float laneW     = ImMax(actionsW, restW);
    const float padR      = inset + M::U(0.5f);
    const float rowW      = ImMax(ImGui::GetContentRegionAvail().x, M::stroke(1.0f));

    // Entrance: rows land as a wave, fading in while easing down from a few px above.
    const float  a    = artiEnterAlpha(t0, index);
    const float  off  = (1.0f - a) * M::px(8.0f);
    const int    vtx0 = dl->VtxBuffer.Size;
    const ImVec2 base = ImGui::GetCursorScreenPos();
    const ImVec2 p0(base.x, base.y - off);
    const ImVec2 p1(p0.x + rowW, p0.y + rowH);

    ImGui::PushID(r.key.c_str());
    ImGui::SetCursorScreenPos(p0);
    ImGui::SetNextItemAllowOverlap();   // the action lane is submitted ON TOP of the row's hit box
    const bool   activated = ImGui::InvisibleButton("##hit", ImVec2(rowW, rowH));
    const bool   tipHover  = ImGui::IsItemHovered(ImGuiHoveredFlags_DelayShort);
    const ImVec2 after     = ImGui::GetCursorScreenPos();
    if (ImGui::IsItemClicked(ImGuiMouseButton_Right)) ImGui::OpenPopup("##arti_ctx");

    // A row with its menu open stays "hot" so its actions do not vanish under the menu.
    const bool  ctxOpen = ImGui::IsPopupOpen("##arti_ctx");
    const bool  hot     = ctxOpen || (ImGui::IsWindowHovered() && ImGui::IsMouseHoveringRect(p0, p1));
    const float hv      = ui::anim::toward(ImGui::GetID("##hv"), hot ? 1.0f : 0.0f, ui::anim::kFast);

    const float rndDp = M::cpMenuRowRound() / M::scale();   // glass helpers take LOGICAL dp
    ui::glassRowBackground(dl, p0, p1, rndDp, false, false, 0.30f + 0.16f * hv);
    if (hv > 0.01f) ui::coloredRowWash(dl, p0, p1, rndDp, ui::palette.accent, 0.12f * hv, false);

    // Kind plate: a themed matte square so the file-kind glyph reads over any backdrop.
    const float  dim = missing ? 0.55f : 1.0f;
    const ImVec2 t0p(p0.x + inset, p0.y + inset);
    const ImVec2 t1p(t0p.x + tile, t0p.y + tile);
    const float  tileRnd = M::cpMenuChipRound();
    dl->AddRectFilled(t0p, t1p, artiRole(ui::palette.codeBg, 0.80f * dim), tileRnd);
    dl->AddRect(t0p, t1p, artiRole(ui::palette.border, 0.55f * dim), tileRnd, 0, M::stroke(1.0f));
    artiPaintIcon(dl, r.cls.icon, ImVec2((t0p.x + t1p.x) * 0.5f, (t0p.y + t1p.y) * 0.5f),
                  tile * 0.62f, dim);

    // Text column: name in the medium weight over the quiet kind / size line.
    const float textX  = t1p.x + M::U(2.0f);
    const float laneR  = p1.x - padR;
    const float textW  = ImMax(laneR - laneW - M::U(1.5f) - textX, M::px(24.0f));
    const float titleY = p0.y + inset;
    if (st.fonts.medium) ImGui::PushFont(st.fonts.medium);
    const std::string title = ui::kit::elide(r.label.c_str(), textW);
    dl->AddText(ImVec2(textX, titleY), artiInk(surf.textPrimary, dim), title.c_str());
    if (st.fonts.medium) ImGui::PopFont();
    ImVec4 metaInk = surf.textSecondary;
    metaInk.w *= 0.9f;
    dl->AddText(ImVec2(textX, titleY + lineH + M::U(0.5f)), artiInk(metaInk, dim),
                ui::kit::elide(meta.c_str(), textW).c_str());

    // Right lane, resting state: the age (or the live marker) fades out as the buttons fade in.
    const float restA = 1.0f - hv;
    if (!rest.empty() && restA > 0.01f) {
        const float rx = laneR - restW;
        const float ry = p0.y + (rowH - lineH) * 0.5f;
        if (r.live) {
            const ImVec4 acc  = semanticOnGlass(ui::palette.accent, surf);
            const float  beat = 0.55f + 0.45f * ui::anim::pulse();
            dl->AddCircleFilled(ImVec2(rx + dotR, ry + lineH * 0.5f), dotR,
                                artiInk(acc, beat * restA));
            dl->AddText(ImVec2(rx + dotR * 2.0f + dotGap, ry), artiInk(acc, restA), rest.c_str());
        } else {
            ImVec4 quiet = surf.textTertiary;
            quiet.w      = 1.0f;
            dl->AddText(ImVec2(rx, ry), artiInk(quiet, restA), rest.c_str());
        }
    }

    // Right lane, hover state. Submitted while fading out too, so the crossfade is symmetric.
    bool overLane = false;
    if (hot || hv > 0.02f) {
        const float bx0  = laneR - actionsW;
        const float by   = p0.y + (rowH - btn) * 0.5f;
        overLane = hot && ImGui::IsMouseHoveringRect(ImVec2(bx0, p0.y), ImVec2(laneR, p1.y), false);
        const int bvtx = dl->VtxBuffer.Size;
        float bx = bx0;
        ImGui::SetCursorScreenPos(ImVec2(bx, by));
        if (ui::kit::iconButton("open", "open", artiOpenVerb(r), true, ui::kit::Tone::Neutral,
                                btn) && r.open)
            r.open();
        if (canReveal) {
            bx += btn + btnGap;
            ImGui::SetCursorScreenPos(ImVec2(bx, by));
            // body_folder is the atlas's plain folder mark; there is no separate "reveal" glyph.
            if (ui::kit::iconButton("reveal", "body_folder", "Show in file manager", true,
                                    ui::kit::Tone::Neutral, btn))
                chatArtifactReveal(st, r.revealPath);
        }
        if (canCopy) {
            bx += btn + btnGap;
            ImGui::SetCursorScreenPos(ImVec2(bx, by));
            if (ui::kit::iconButton("copy", "copy", "Copy the full path", true,
                                    ui::kit::Tone::Neutral, btn))
                artiCopyPath(st, r.path);
        }
        artiFadeVerts(dl, bvtx, hv);
    }

    // AllowOverlap hands the click to the buttons, but exclude their strip from the row
    // activation explicitly anyway: on the first hot frame the buttons were not hovered last
    // frame, and "Show in file manager" would otherwise both reveal the file AND open it.
    if (activated && !overLane && r.open) r.open();
    if (hot && !overLane && !ctxOpen) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (tipHover && !overLane) {
        std::string tip = r.label;
        if (!r.path.empty()) tip += "\n" + r.path;
        tip += "\n" + meta;
        if (!rest.empty() && !r.live) tip += " \xc2\xb7 " + rest;
        ui::kit::tooltip(tip.c_str());
    }
    artiContextMenu(st, r);
    ImGui::PopID();

    if (a < 1.0f) artiFadeVerts(dl, vtx0, a);
    ImGui::SetCursorScreenPos(ImVec2(after.x, after.y + off));
}

// Image tiles: a responsive grid of uniform rounded plates (4:3, the image aspect-fit inside) with
// the file name under each. Columns come from the available width — one on the narrowest dock,
// up to four on a wide one. Hover lifts the plate and lights its edge; a click views full size.
// Only the rows inside the viewport are submitted, so a 200-render session decodes a screenful.
// `enterBase` continues the panel-wide entrance wave instead of restarting it at zero.
static void artiDrawGrid(AppState& st, const std::vector<const ArtiDocRow*>& rows, double t0,
                         int enterBase) {
    namespace M = ui::metrics;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImDrawList* dl = ImGui::GetWindowDrawList();

    const float availW = ImMax(ImGui::GetContentRegionAvail().x, M::px(40.0f));
    const float gap    = M::U(1.5f);
    const int   cols   = ImClamp((int)((availW + gap) / (M::px(112.0f) + gap)), 1, 4);
    const float cell   = (availW - gap * (float)(cols - 1)) / (float)cols;
    const float plateH = std::floor(cell * 0.75f);
    const float lineH  = ImGui::GetTextLineHeight();
    const float capGap = M::U(0.75f);
    const float tileH  = plateH + capGap + lineH;
    const float pitch  = tileH + gap;
    const float rnd    = M::cpMenuRowRound();
    const float rndDp  = rnd / M::scale();
    const float pad    = M::px(3.0f);
    const float lift   = M::px(2.0f);

    const ImVec2 origin   = ImGui::GetCursorScreenPos();
    const int    gridRows = ((int)rows.size() + cols - 1) / cols;
    const ImRect clip     = ImGui::GetCurrentWindow()->ClipRect;
    const int    firstRow = ImMax(0, (int)std::floor((clip.Min.y - origin.y) / pitch));
    const int    lastRow  = ImMin(gridRows, (int)std::ceil((clip.Max.y - origin.y) / pitch));
    const size_t begin    = (size_t)firstRow * (size_t)cols;
    const size_t end      = ImMin(rows.size(), (size_t)ImMax(firstRow, lastRow) * (size_t)cols);

    for (size_t i = begin; i < end; ++i) {
        const ArtiDocRow& r = *rows[i];
        const Tex   tex = artiRenderThumb(r.path);
        const float a   = artiEnterAlpha(t0, enterBase + (int)i);
        const float off = (1.0f - a) * M::px(8.0f);
        const ImVec2 p0(origin.x + (float)(i % (size_t)cols) * (cell + gap),
                        origin.y + (float)(i / (size_t)cols) * pitch - off);
        const int vtx0 = dl->VtxBuffer.Size;

        ImGui::PushID(r.key.c_str());
        ImGui::SetCursorScreenPos(p0);
        const bool clicked = ImGui::InvisibleButton("##tile", ImVec2(cell, tileH));
        const bool hot     = ImGui::IsItemHovered();
        const bool tipHot  = ImGui::IsItemHovered(ImGuiHoveredFlags_DelayShort);
        if (ImGui::IsItemClicked(ImGuiMouseButton_Right)) ImGui::OpenPopup("##arti_ctx");
        if (clicked && r.open) r.open();

        const float  hv = ui::anim::toward(ImGui::GetID("##hv"), hot ? 1.0f : 0.0f, ui::anim::kFast);
        const float  grow = lift * hv;
        const ImVec2 q0(p0.x - grow, p0.y - grow);
        const ImVec2 q1(p0.x + cell + grow, p0.y + plateH + grow);

        // Plate, then the image aspect-fit inside it (or the kind glyph until the decode lands).
        dl->AddRectFilled(q0, q1, artiRole(ui::palette.codeBg, 0.85f), rnd);
        const ImVec2 in0(q0.x + pad, q0.y + pad), in1(q1.x - pad, q1.y - pad);
        const ImVec2 mid((in0.x + in1.x) * 0.5f, (in0.y + in1.y) * 0.5f);
        if (tex.id && tex.w > 0 && tex.h > 0) {
            const float fit = ImMin((in1.x - in0.x) / (float)tex.w, (in1.y - in0.y) / (float)tex.h);
            const float hw = (float)tex.w * fit * 0.5f, hh = (float)tex.h * fit * 0.5f;
            dl->AddImageRounded((ImTextureID)(intptr_t)tex.id, ImVec2(mid.x - hw, mid.y - hh),
                                ImVec2(mid.x + hw, mid.y + hh), ImVec2(0, 0), ImVec2(1, 1),
                                IM_COL32_WHITE, ImMax(rnd - pad, M::stroke(1.0f)));
        } else {
            artiPaintIcon(dl, r.cls.icon, mid, M::iconHeader() * 1.4f, 0.45f);
        }
        // Edge: the quiet border, easing to the accent with the lift.
        const ImVec4 edge = ImLerp(ImVec4(ui::palette.border[0], ui::palette.border[1],
                                          ui::palette.border[2], ui::palette.border[3] * 0.6f),
                                   ImVec4(ui::palette.accent[0], ui::palette.accent[1],
                                          ui::palette.accent[2], ui::palette.accent[3]), hv);
        dl->AddRect(q0, q1, artiInk(edge), rnd, 0, M::stroke(1.0f));
        if (hv > 0.02f) {
            float acc[4];
            ui::Palette::withAlpha(ui::palette.accent, acc, hv);
            // The glow self-insets by 2 dp, so hand it the plate grown by the same amount.
            ui::glassSelectionGlow(dl, ImVec2(q0.x - M::px(2.0f), q0.y - M::px(2.0f)),
                                   ImVec2(q1.x + M::px(2.0f), q1.y + M::px(2.0f)), rndDp, acc);
        }

        // Caption: the file name, brightening with the hover.
        const ImVec4 capInk = ImLerp(surf.textSecondary, surf.textPrimary, hv);
        dl->AddText(ImVec2(p0.x + M::px(2.0f), p0.y + plateH + capGap), artiInk(capInk),
                    ui::kit::elide(r.label.c_str(), cell - M::px(4.0f)).c_str());

        if (hot) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (tipHot) ui::kit::tooltip((r.label + "\n" + r.path).c_str());
        artiContextMenu(st, r);
        ImGui::PopID();

        if (a < 1.0f) artiFadeVerts(dl, vtx0, a);
    }

    ImGui::SetCursorScreenPos(origin);
    if (gridRows > 0) ImGui::Dummy(ImVec2(availW, (float)gridRows * pitch - gap));
}

// A collapsible section header: animated disclosure chevron, the label in small caps, a quiet
// count. `reserveRight` px at the right edge stay out of the toggle's hit area so the caller can
// place a trailing control on the returned rect (then restore the cursor to `after`).
struct ArtiHeader {
    ImVec2 min;                 // top-left of the header row
    float  w = 0.0f, h = 0.0f;
    ImVec2 after;               // cursor below the header
};

static ArtiHeader artiSectionHeader(const ui::Fonts& fonts, const char* id, const char* label,
                                    int count, bool* open, bool first, float reserveRight,
                                    const char* tip) {
    namespace M = ui::metrics;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (!first) ImGui::Dummy(ImVec2(0.0f, M::cpMenuSectionGap()));

    ArtiHeader hd;
    const float lineH = ImGui::GetTextLineHeight();
    hd.min = ImGui::GetCursorScreenPos();
    hd.w   = ImMax(ImGui::GetContentRegionAvail().x, M::stroke(1.0f));
    hd.h   = lineH + M::U(1.5f);

    ImGui::PushID(id);
    if (ImGui::InvisibleButton("##head", ImVec2(ImMax(hd.w - reserveRight, M::stroke(1.0f)), hd.h)))
        *open = !*open;
    const bool  hovered = ImGui::IsItemHovered();
    const float hv      = ui::anim::toward(ImGui::GetID("##hv"), hovered ? 1.0f : 0.0f,
                                           ui::anim::kFast);
    const float openT   = ui::anim::toward(ImGui::GetID("##open"), *open ? 1.0f : 0.0f,
                                           ui::anim::kBase);
    ImGui::PopID();
    if (hovered) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (hovered && tip && tip[0]) ui::kit::tooltip(tip);

    const float cy = hd.min.y + hd.h * 0.5f;
    chatPaintDisclosure(dl, ImVec2(hd.min.x + lineH * 0.4f, cy), lineH * 0.38f, openT,
                        artiInk(ImLerp(surf.textTertiary, surf.textSecondary, hv)));

    // Small caps: the medium weight, upper-cased, a notch under the body size.
    if (fonts.medium) ImGui::PushFont(fonts.medium);
    ImFont*     capsFont = ImGui::GetFont();
    const float fs       = ImGui::GetFontSize() * 0.82f;
    std::string caps(label);
    for (char& ch : caps) ch = (char)std::toupper((unsigned char)ch);
    const ImVec2 ls = capsFont->CalcTextSizeA(fs, FLT_MAX, 0.0f, caps.c_str());
    const float  lx = hd.min.x + lineH * 0.95f;
    dl->AddText(capsFont, fs, ImVec2(lx, cy - ls.y * 0.5f),
                artiInk(ImLerp(surf.textSecondary, surf.textPrimary, hv)), caps.c_str());
    if (fonts.medium) ImGui::PopFont();

    const std::string cnt     = ui::kit::fmtCount((long long)count);
    ImFont*           bodyFont = ImGui::GetFont();
    const ImVec2      cs      = bodyFont->CalcTextSizeA(fs, FLT_MAX, 0.0f, cnt.c_str());
    ImVec4            cntInk  = surf.textTertiary;
    cntInk.w                  = 1.0f;
    dl->AddText(bodyFont, fs, ImVec2(lx + ls.x + M::U(1.5f), cy - cs.y * 0.5f), artiInk(cntInk),
                cnt.c_str());

    ImGui::Dummy(ImVec2(0.0f, M::cpMenuHeadGap()));
    hd.after = ImGui::GetCursorScreenPos();
    return hd;
}

// Kind filter chips, wrapped to as many lines as the width needs. A kind with nothing in it is
// hidden (the active one stays so it can always be left); "All" counts what the text filter kept.
static void artiKindChips(ArtiUiState& ui_, const int (&count)[kArtiKinds], int total) {
    namespace M = ui::metrics;
    static const char* const kLabels[kArtiKinds + 1] = {"All", "Models", "Drawings", "Images", "Data"};
    static const char* const kTips[kArtiKinds + 1] = {
        "Everything the agent produced this session",
        "Parts, assemblies, and CAD or mesh exports",
        "Drawings, PDFs and vector exports",
        "Renders and screenshots",
        "Spreadsheets, notes, scripts and other files",
    };

    const float  availW = ImMax(ImGui::GetContentRegionAvail().x, M::px(40.0f));
    const float  gap    = M::U(1.0f);
    const float  padX   = M::U(1.5f);   // the kit chip's own inner padding
    const float  chipH  = ImGui::GetTextLineHeight() + M::U(1.0f);
    const ImVec2 origin = ImGui::GetCursorScreenPos();

    float x = 0.0f, y = 0.0f;
    for (int i = 0; i <= kArtiKinds; ++i) {
        const int n = i == 0 ? total : count[i - 1];
        if (i > 0 && n == 0 && ui_.kind != i) continue;
        char label[48];
        std::snprintf(label, sizeof(label), "%s %d##kind%d", kLabels[i], n, i);
        const float w = ImGui::CalcTextSize(label, nullptr, true).x + padX * 2.0f;
        if (x > 0.0f && x + w > availW) {
            x = 0.0f;
            y += chipH + gap;
        }
        ImGui::SetCursorScreenPos(ImVec2(origin.x + x, origin.y + y));
        bool on = ui_.kind == i;
        if (ui::kit::toggleChip(label, &on, ui::kit::Tone::Accent, kTips[i]) && ui_.kind != i) {
            ui_.kind    = i;
            ui_.enterT0 = ImGui::GetTime();   // the new set of rows lands as a wave
        }
        x += w + gap;
    }
    ImGui::SetCursorScreenPos(origin);
    ImGui::Dummy(ImVec2(availW, y + chipH));
}

// Session Artifacts is a browser for session output: agent-touched documents, render images and
// produced files, grouped by kind. Run telemetry stays in the Tasks panel.
static void drawArtifactsPanel(AppState& st, bool busy) {
    (void)busy;
    namespace M = ui::metrics;
    ArtiUiState& ui_ = artiUi(st);

    // The question this panel answers is "what did the AGENT touch", not "what is open" — the old
    // single list showed every doc tab, so a session that built one part looked like it built
    // four. A tab counts as agent-touched when it was handed off by an agent OR carries an agent
    // edit stamp; everything else is the user's own document (`own`) and goes behind a collapsed
    // section. Documents come first, then produced files newest-first.
    std::vector<ArtiDocRow> rows;
    {
        // Dedup doc-tab rows: an AI read-only mirror tab and the user's own tab can be
        // two docTabs entries for the SAME underlying document. Two things independently
        // identify one document, so BOTH are checked — the mirror shares the agent's
        // docId, while a part reopened from disk under a fresh docId shares only its
        // path. Claiming either key is enough to make a later tab a duplicate.
        std::set<std::string> seenId, seenPath;
        for (int ti = 0; ti < (int)st.docTabs.size(); ++ti) {
            const auto& t = st.docTabs[ti];
            const bool dupId   = !t.docId.empty() && !seenId.insert(t.docId).second;
            const bool dupPath = !t.path.empty()  && !seenPath.insert(t.path).second;
            if (dupId || dupPath) continue;
            if (t.docId.empty() && t.path.empty() && !t.title.empty() &&
                !seenId.insert("title:" + t.title).second)
                continue;  // last resort for a tab with neither identifier
            ArtiDocRow r;
            r.doc = true;
            r.cls = artiDocClass(t);
            // displayTitle() is the one label that refuses to hand back an opaque
            // "doc-cpp…" id (doc_tabs.cpp:95); the old t.title fallback chain ended on
            // t.docId, so a part the agent opened listed itself as its engine id.
            r.label = t.displayTitle();
            if (r.label.empty()) r.label = r.cls.what;
            r.path = t.path;
            // Unsaved in-memory docs have no file to reveal — fall back to their save
            // destination (the project/workspace folder) so the location action always exists.
            r.revealPath = !t.path.empty() ? t.path : st.plmProjectPath();
            r.key  = "doc:" + (!t.docId.empty() ? t.docId
                               : !t.path.empty() ? t.path : "title:" + t.title);
            r.live = t.aiPulse;
            r.open = [&st, ti]() { st.switchToTab(ti); };
            r.own  = !(t.aiHandoff ||
                       t.lastAgentEditAt != std::chrono::steady_clock::time_point{});
            rows.push_back(std::move(r));
        }
        // Docs the user closed from the viewport while an agent kept building them.
        // hostd degraded that close to a detach (the work is independent of the
        // user's tabs), so the part is still live — list it with a Reopen action
        // instead of letting it vanish from the session's artifacts entirely.
        // These are agent documents by construction, so they are never `own`.
        for (const auto& [docId, label] : st.dismissedMirrorDocs) {
            if (docId.empty() || !seenId.insert(docId).second) continue;
            ArtiDocRow r;
            r.doc    = true;
            r.closed = true;   // no file yet; the only action is to reopen the view
            r.cls    = ArtiClass{"part", "Part", ArtiModels};
            r.label  = label.empty() ? std::string("Closed part") : label;
            const std::string id = docId;   // copied: a structured binding cannot be captured
            r.key    = "closed:" + id;
            r.open   = [&st, id]() {
                st.dismissedMirrorDocs.erase(id);
                core::mirrorRecoveredDocs(st, {id});
            };
            rows.push_back(std::move(r));
        }
        // Cross-source dedup: seed `seen` with the doc tabs' real file paths so a file
        // already listed as an open document is not repeated as a produced-file row.
        std::set<std::string> seen;
        for (const auto& t : st.docTabs)
            if (!t.path.empty()) seen.insert(t.path);
        std::vector<ArtiDocRow> files;   // chronological, as the chat produced them
        for (const auto& m : st.chat) {
            if (m.fromUser) continue;
            for (const auto& a : m.activity) {
                const size_t sep = a.detail.find("\x1e" "ART\x1e");
                if (sep == std::string::npos) continue;
                std::string blob = a.detail.substr(sep);
                size_t nl = blob.find('\n');
                if (nl != std::string::npos) blob = blob.substr(nl + 1);
                std::istringstream iss(blob);
                std::string line;
                while (std::getline(iss, line)) {
                    std::string kindA, path;
                    if (!artifactLine(line, &kindA, &path) || !seen.insert(path).second) continue;
                    std::string base = path;
                    size_t slash = base.find_last_of("/\\");
                    if (slash != std::string::npos) base = base.substr(slash + 1);
                    ArtiDocRow r;
                    r.label      = base;
                    r.path       = path;
                    r.revealPath = path;
                    r.key        = "file:" + path;
                    r.cls        = artiClassify(path, base);
                    // One classifier drives the icon, the "what it IS" line AND the section; the
                    // ART stream can still know an image the extension does not say so.
                    if (kindA == "image" && r.cls.bucket != ArtiImages) {
                        r.cls.icon   = "file_image";
                        r.cls.what   = "Render";
                        r.cls.bucket = ArtiImages;
                    }
                    if (r.cls.bucket == ArtiImages)
                        r.open = [path]() { openImageLightbox(path); };
                    else
                        r.open = [&st, path]() { chatArtifactOpenSmart(st, path); };
                    files.push_back(std::move(r));
                }
            }
        }
        rows.insert(rows.end(), std::make_move_iterator(files.rbegin()),
                    std::make_move_iterator(files.rend()));
    }
    int agentCount = 0;
    for (const ArtiDocRow& r : rows)
        if (!r.own) ++agentCount;

    // ---- header: title, a one-line count, and the project-folder action on the title row ----
    char subtitle[64];
    if (agentCount == 0)
        std::snprintf(subtitle, sizeof(subtitle), "Nothing produced yet");
    else
        std::snprintf(subtitle, sizeof(subtitle), "%d %s from this session", agentCount,
                      agentCount == 1 ? "item" : "items");
    const float  titleH = st.fonts.medium ? st.fonts.medium->FontSize : ImGui::GetTextLineHeight();
    const ImVec2 hdrTop = ImGui::GetCursorScreenPos();
    const float  hdrW   = ImGui::GetContentRegionAvail().x;
    ui::cpMenuHeader(st.fonts, "Artifacts", subtitle);
    {
        // Project-folder reveal accepts a directory directly. The button sits at the right edge
        // of the title line, centred on it (the header owns its own cursor, so place it, then put
        // the cursor back).
        const std::string projectPath = st.plmProjectPath();
        const ImVec2      keep        = ImGui::GetCursorScreenPos();
        const float       btn         = ImGui::GetTextLineHeight() + M::U(1.5f);
        ImGui::SetCursorScreenPos(ImVec2(hdrTop.x + hdrW - btn, hdrTop.y + (titleH - btn) * 0.5f));
        if (ui::kit::iconButton("arti_project", "body_folder",
                                projectPath.empty()
                                    ? "Open or choose a project to show its folder"
                                    : "Show the project folder in the file manager",
                                !projectPath.empty(), ui::kit::Tone::Neutral, btn))
            chatArtifactReveal(st, projectPath);
        ImGui::SetCursorScreenPos(keep);
    }

    // Everything below spaces itself with explicit tokens, so row pitch and the list clipper agree.
    const ImVec2 spc = ImGui::GetStyle().ItemSpacing;
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(spc.x, 0.0f));

    const bool showFilter = rows.size() > 8;
    if (showFilter) {
        ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, M::cpMenuChipRound());
        ImGui::InputTextWithHint("##artifact_filter", "Filter artifacts\xe2\x80\xa6",
                                 ui_.filter, sizeof(ui_.filter));
        ImGui::PopStyleVar();
        ImGui::Dummy(ImVec2(0.0f, M::cpMenuCtlGap()));
    }
    const bool        filterActive = showFilter && ui_.filter[0] != '\0';
    const std::string needle       = filterActive ? artiLower(ui_.filter) : std::string();
    auto matches = [&](const ArtiDocRow& r) {
        if (!filterActive) return true;
        return artiLower(r.label).find(needle) != std::string::npos ||
               artiLower(r.path).find(needle) != std::string::npos;
    };

    // ---- bucket rows by kind (the user's own tabs all go to the collapsed fifth section) ----
    std::vector<const ArtiDocRow*> sec[kArtiSections];
    int kindCount[kArtiKinds] = {0, 0, 0, 0};
    int matched = 0;
    for (const ArtiDocRow& r : rows) {
        if (!matches(r)) continue;
        const int k = (int)r.cls.bucket;
        if (!r.own) {
            ++kindCount[k];
            ++matched;
        }
        if (ui_.kind != 0 && ui_.kind - 1 != k) continue;
        sec[r.own ? kArtiOpenSec : k].push_back(&r);
    }

    int kindsPresent = 0;
    for (int n : kindCount)
        if (n > 0) ++kindsPresent;
    if (agentCount > 0 && (kindsPresent >= 2 || ui_.kind != 0)) {
        artiKindChips(ui_, kindCount, matched);
        ImGui::Dummy(ImVec2(0.0f, M::cpMenuCtlGap()));
    }

    bool anyShown = false;
    for (const auto& list : sec)
        if (!list.empty()) anyShown = true;
    if (agentCount == 0) {
        // The kit's empty-state spacing assumes the default item spacing.
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, spc);
        ui::kit::emptyState("artifacts", "No artifacts yet",
                            "Models, drawings, renders and files the agent produces appear here "
                            "as it works.");
        ImGui::PopStyleVar();
    } else if (!anyShown) {
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, spc);
        const bool clear = ui::kit::emptyState("artifacts", "Nothing matches",
                                               "No artifact fits the current filter.",
                                               "Clear filters");
        ImGui::PopStyleVar();
        if (clear) {
            ui_.kind      = 0;
            ui_.filter[0] = '\0';
        }
    }

    // ---- sections ----
    static const char* const kSecId[kArtiSections]    = {"models", "drawings", "images", "data", "own"};
    static const char* const kSecLabel[kArtiSections] = {"Models", "Drawings", "Images", "Data",
                                                         "Open documents"};
    // The user's own tabs must not dominate this panel, but dropping them outright would lose the
    // only way to reach a part the agent worked on BEFORE the handoff / edit-stamp flags were set
    // — so they stay, collapsed until the header is clicked.
    static const char* const kSecTip[kArtiSections]   = {
        nullptr, nullptr, nullptr, nullptr,
        "Documents you opened yourself. Parts the agent has worked on are listed under their kind.",
    };
    const float rowGap   = M::cpMenuRowGap();
    const bool  previews = ui::chatShowRenders();
    int         enterIdx = 0;   // one entrance wave across the panel: section 2 continues section 1
    bool        first    = true;
    for (int s = 0; s < kArtiSections; ++s) {
        const std::vector<const ArtiDocRow*>& list = sec[s];
        if (list.empty()) continue;
        const bool images = s == (int)ArtiImages;
        // Previews is a pane-wide display pref (shared with the transcript); it lives on the
        // header of the one section it governs.
        const float chipW = images ? ImGui::CalcTextSize("Previews").x + M::U(1.5f) * 2.0f : 0.0f;

        // A text filter searches the user's tabs too, so their section opens while one is typed.
        const bool forced  = s == kArtiOpenSec && filterActive;
        bool       open    = ui_.open[s] || forced;
        const bool wasOpen = open;
        const ArtiHeader hd = artiSectionHeader(st.fonts, kSecId[s], kSecLabel[s], (int)list.size(),
                                                &open, first, images ? chipW + M::U(1.0f) : 0.0f,
                                                kSecTip[s]);
        first = false;
        if (!forced) {
            if (open && !wasOpen) ui_.openT0[s] = ImGui::GetTime();   // rows re-enter on expand
            ui_.open[s] = open;
        }
        if (images) {
            const float chipH = ImGui::GetTextLineHeight() + M::U(1.0f);
            bool        on    = previews;
            ImGui::SetCursorScreenPos(ImVec2(hd.min.x + hd.w - chipW,
                                             hd.min.y + (hd.h - chipH) * 0.5f));
            if (ui::kit::toggleChip("Previews", &on, ui::kit::Tone::Accent,
                                    "Show or hide image previews here and in the chat transcript."))
                ui::setChatShowRenders(on);
            ImGui::SetCursorScreenPos(hd.after);
        }
        if (!open) continue;

        const double secT0 = ImMax(ui_.enterT0, ui_.openT0[s]);
        ImGui::PushID(kSecId[s]);
        if (images && previews) {
            artiDrawGrid(st, list, secT0, enterIdx);
        } else {
            ImGuiListClipper clipper;
            clipper.Begin((int)list.size(), artiRowHeight() + rowGap);
            while (clipper.Step()) {
                for (int i = clipper.DisplayStart; i < clipper.DisplayEnd; ++i) {
                    artiRow(st, *list[(size_t)i], enterIdx + i, secT0);
                    ImGui::Dummy(ImVec2(0.0f, rowGap));
                }
            }
        }
        enterIdx += (int)list.size();
        ImGui::PopID();
    }
    ImGui::Dummy(ImVec2(0.0f, M::cpMenuSectionGap()));
    ImGui::PopStyleVar();   // ItemSpacing

    // The full-size viewer normally rides on the composer, which the menu views replace.
    drawImageLightbox(st);
}



void drawAIPanel(AppState& st, bool busy, bool canRun) {
    if (!st.showAIPanel) return;
#ifdef __EMSCRIPTEN__
    const bool mobile = ImGui::GetIO().DisplaySize.x <= 900.0f ||
                        EM_ASM_INT({ return window.matchMedia('(pointer: coarse)').matches; });
#else
    const bool mobile = false;
#endif

    // Keep the local-endpoint model list fresh whenever the copilot panel exists — BEFORE the
    // visibility gate below, so discovery runs even when the pane is occluded. Without this, a
    // model loaded in LM Studio isn't discovered until the user opens the picker ("pokes around
    // in settings"). The probe is throttled to ~5s and no-ops for a non-loopback (cloud)
    // base_url, so this is cheap.
    refreshModelAvailability(st, false);

    static float s_paneW = 0.0f;                    // column width (px); persisted as Settings::copilotWidth
    static bool s_savePaneW = false;                // a column drag just ended: persist the clamped width
    static const bool shotNarrow = std::getenv("CHUDWARE_COPILOT_NARROW") != nullptr;
    static const bool shotCollapsed = std::getenv("CHUDWARE_COPILOT_COLLAPSED") != nullptr;
    static const float shotWidth = [] {
        const char* value = std::getenv("CHUDWARE_COPILOT_WIDTH");
        return value ? (float)std::atof(value) : 0.0f;
    }();
    static const std::string shotView = [] {
        const char* value = std::getenv("CHUDWARE_COPILOT_VIEW");
        return value ? std::string(value) : std::string();
    }();
    // s_cpCollapsed is file-scoped (shared with drawCopilotHeader's collapse chevron). Seed it
    // once from the persisted setting (a collapsed Copilot column stays collapsed across
    // restarts) — can't do this at s_cpCollapsed's own file-scope initializer since that runs
    // before loadSettings, so seed lazily on this function's first call instead.
    static bool s_cpInited = false;
    if (!s_cpInited) { s_cpCollapsed = st.settings.copilotCollapsed; s_cpInited = true; }
    static ui::PanelCollapseAnim s_cpAnim;
    // Anchor to the overlay band app.cpp publishes each frame (ribbon bottom -> status-bar top),
    // inset by the doc-tab strip on top — the SAME geometry source as the FeatureManager column in
    // layout.cpp drawLeftPane, so the two pinned columns always share top/bottom edges. Falls back
    // to the main viewport's work rect for the pre-publish first frame.
    ui::OverlayBand band = ui::overlayBand();
    if (!band.valid) {
        const ImGuiViewport* mvp = ImGui::GetMainViewport();
        band.minX = mvp->WorkPos.x;
        band.minY = mvp->WorkPos.y;
        band.maxX = mvp->WorkPos.x + mvp->WorkSize.x;
        band.maxY = mvp->WorkPos.y + mvp->WorkSize.y;
        band.valid = band.maxX > band.minX && band.maxY > band.minY;
    }
    const bool overlay = band.valid;
    float homeBlend = 0.0f;   // workspace home widening (workspace_home.h), overlay columns only
    // ONE bubble radius shared by the window clip and the drawn glass (same rule as drawLeftPane):
    // the resolved Copilot rounding knob wins when set, else the 12dp bubble default.
    const ui::GlassStyleResolved& rsCp = ui::glassStyleResolved(ui::GlassGroup::Copilot);
    const float bubbleDp = (rsCp.rounding >= 0.f) ? rsCp.rounding : 12.0f;
    ImGuiWindowFlags flags = ImGuiWindowFlags_NoBackground;
    if (overlay) {
        // Slimmer right-edge margin than the 12dp bottom margin: the pinned column
        // hugs the program edge so header buttons/chat bubbles use the full width.
        const float marginX = ui::metrics::px(6.0f);
        const float margin  = ui::metrics::px(12.0f);
        // The doc-tab strip now floats over the viewport (fix 2), not above these columns, so the
        // Copilot column starts flush at the top of the viewport band — same rule as drawLeftPane,
        // keeping the two pinned columns sharing their top edge.
        const float colY = band.minY + 4.0f * ui::uiScale;   // match left-pane top (doc_tabs / FM)
        const float availableH = std::max(1.0f, band.maxY - margin - colY);
        const float colH = mobile ? availableH : std::max(120.0f * ui::uiScale, availableH);
        // "Reset layout" (slot 1 = Copilot column) restores the default width + expands it.
        if (ui::takeOverlayColumnReset(1)) {
            s_paneW = 0.0f;
            st.settings.copilotWidth = 0.0f;       // default width again (saved just below)
            s_cpCollapsed = false;
            st.settings.copilotCollapsed = false;
            saveSettings(st.settings);
        }
        // Seed from the persisted width (logical px) on the first frame after launch / a reset.
        if (s_paneW <= 0.0f)
            s_paneW = st.settings.copilotWidth > 0.0f
                          ? ui::metrics::px(st.settings.copilotWidth)
                          : ui::metrics::dockRightTarget();
        if (shotNarrow)       // shot: capture the narrowest column
            s_paneW = ui::metrics::dockRightMin();
        if (st.shot && shotWidth > 0) s_paneW = ui::metrics::px(shotWidth);
        // Shot: mirrors CHUDWARE_FM_COLLAPSED — --ui-screenshot always resets the overlay
        // columns for determinism (see the reset branch above), which clobbers the persisted
        // collapse setting before it can render; force it back for the collapsed capture.
        if (shotCollapsed) {
            s_cpCollapsed = true;
            s_cpAnim.snap(true, colH);  // snap immediately — headless captures must not depend on frame count
        }
        const float availableW = std::max(1.0f, band.maxX - band.minX - marginX * 2.0f);
        const float maxW = mobile ? availableW
                                  : std::max((band.maxX - band.minX) * 0.5f,
                                             ui::metrics::dockRightMin());
        const float paneW = mobile ? std::min(availableW, ui::metrics::dockRightTarget())
                                  : std::min(std::max(s_paneW, ui::metrics::dockRightMin()), maxW);
        if (!mobile) s_paneW = paneW;
        if (s_savePaneW && !mobile) {
            s_savePaneW = false;
            st.settings.copilotWidth = paneW / ui::uiScale;
            saveSettings(st.settings);
        }
        // Workspace home (no document open): the chat gets more room too, easing back after.
        homeBlend = mobile ? 0.0f : ui::workspaceHomeBlend(st);
        float shownW = paneW;
        if (homeBlend > 0.0f) {
            const float homeW = std::clamp((band.maxX - band.minX) * 0.30f, paneW,
                                           std::max(paneW, std::min(maxW, 600.0f * ui::uiScale)));
            shownW = paneW + (homeW - paneW) * homeBlend;
        }

        // Vertical collapse — the column rolls UP to its header row (drawCopilotHeader)
        // instead of shrinking to a pill in both axes (mirrors the FeatureManager column; see
        // ui::PanelCollapseAnim). Width is unaffected by collapse.
        const float animatedH = s_cpAnim.tick(s_cpCollapsed, colH);
        const float winH = mobile ? std::min(animatedH, colH) : animatedH;
        ImGui::SetNextWindowPos(ImVec2(band.maxX - marginX - shownW, colY), ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2(shownW, winH), ImGuiCond_Always);
        if (mobile)
            ImGui::SetNextWindowSizeConstraints(ImVec2(1.0f, 1.0f), ImVec2(paneW, colH));
        flags |= ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                 ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                 ImGuiWindowFlags_NoDocking | ImGuiWindowFlags_NoScrollbar |
                 ImGuiWindowFlags_NoScrollWithMouse;
    }
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, bubbleDp * ui::uiScale);
    // Tighter column padding than the theme default (15,13): the Copilot is a slim
    // pinned column — wide gutters just shrink the chat bubbles and composer.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(7.0f * ui::uiScale, 4.0f * ui::uiScale));
    const bool cpOpen = ImGui::Begin("Copilot###AI", nullptr, flags);
    ImGui::PopStyleVar(2);
    if (!cpOpen) { ImGui::End(); return; }
    // Theme glass slab under the whole column (no-op in light theme): in dark theme the adaptive
    // ink follows the theme, so the column must never let light ink sit on a light viewport.
    {
        const ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
        ui::pinnedColumnSurface(ImGui::GetWindowDrawList(),
                                ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)),
                                ui::GlassGroup::Copilot);
    }
    ui::Fonts& fonts = st.fonts;

    // Liquid-Glass v2 host port (contract H5): ONE fully transparent glass surface for the whole
    // Copilot column, painted once here at panel top (id "host_agent_glass" via ui::panelId —
    // window-independent, so chatSurf() below returns the SAME cached surface for the header,
    // composer, and every card this frame; ui::adaptivePanel is idempotent per frame). Tagged
    // GlassGroup::Copilot in chatSurf(), so the user's Appearance knobs (tint/level/rounding/
    // opacity) ride in automatically. Falls back to the palette's glassBackground when no
    // viewport backdrop has been published yet.
    {
        ImVec2 wmin = ImGui::GetWindowPos();
        // The WHOLE-column glass slab is intentionally NOT painted anymore: the Copilot bounding box
        // is hidden (fully transparent) so the 3D viewport shows straight through with no misaligned
        // frosted refraction. Only the individual chat cards / header / composer / text carry their
        // own glass pills. chatSurf() is still resolved (idempotent per frame) so those elements
        // fetch the SAME contrast-matched text color as before.
        // Let the viewport HUD (result legend) dodge the pinned column (layout.h, slot 1).
        if (overlay) ui::publishOverlayColumnEdge(1, wmin.x);
    }

    // Left-edge resize boundary — the same visible hairline + grip + hover glow as the
    // FeatureManager column (ui::columnResizeEdge, layout.h). Dragging left widens the column;
    // s_paneW is clamped at the top of the next frame; double-click restores the default width.
    // Submitted before the header; its controls remain inset clear of the resize hit zone.
    if (overlay) {
        const ui::ColumnResizeResult rz = ui::columnResizeEdge(
            "##copilot_resize", /*rightEdge=*/false, ImGui::GetWindowWidth(),
            !mobile && homeBlend <= 0.0f);
        static bool s_wasResizing = false;
        if (rz.resetRequested) {
            s_paneW = ui::metrics::dockRightTarget();
            st.settings.copilotWidth = 0.0f;
            saveSettings(st.settings);
        } else if (rz.active) {
            s_paneW = rz.width;
        }
        if (s_wasResizing && !rz.active && !rz.resetRequested) s_savePaneW = true;
        s_wasResizing = rz.active;
    }

    // focusSettingsTab opens the Settings modal AI tab (full form + leaderboard).
    // The in-column gear uses showAISettingsOverlay instead.
    if (st.focusSettingsTab) {
        st.openSettings     = true;
        st.openSettingsTab  = 2;
        st.focusSettingsTab = false;
    }

    // Conversation history is resident; this loads it once, keeps st.chatDocKey (run-launch key)
    // current, and guarantees at least one conversation. The Sessions view applies project scope.
    // Must run BEFORE the header/composer touch st.conversations, so the seeded/persisted chat is
    // adopted rather than cleared.
    syncChatToDoc(st);
    maybePersistChatHistoryDebounced(st, /*force=*/false);

    // Screenshot hook: CHUDWARE_COPILOT_VIEW forces a Copilot sub-view (and tops up a couple of
    // demo sessions) so the sessions/settings layouts can be captured headless in both themes. Runs
    // AFTER syncChatToDoc (which loads/ensures the resident sessions) and re-tops-up each frame so
    // the demo rows survive to the capture frame. Screenshot-only — gated on the env var, no effect
    // in a normal run.
    if (!shotView.empty()) {
        const char* cpView = shotView.c_str();
        ensureConversations(st);
        st.conversations[0].title = "Knurled hex standoff";
        while (st.conversations.size() < 3) {
            Conversation cx; cx.id = newConvId(st);
            const bool first = st.conversations.size() == 1;
            // The first demo row carries a raw inline-image marker like a real prompt-derived title.
            cx.title = first ? "[Image #1]modify this part so it fits the Bracket + FEA study"
                             : "Gearbox housing revision";
            cx.projectDir = convProjectDir(st);  // demo rows must pass the project filter
            // Real message times, so the Sessions recency groups and ages are exercised.
            const double ago = first ? 5.0 * 3600.0 : 2.0 * 86400.0;
            for (int m = 0; m < (first ? 6 : 14); ++m) {
                ChatMsg msg;
                msg.fromUser = (m % 2) == 0;
                msg.text = "Demo message";
                msg.ts = chatNowEpoch() - ago + (double)m;
                cx.msgs.push_back(std::move(msg));
            }
            st.conversations.push_back(std::move(cx));
        }
        if (std::strcmp(cpView, "sessions") == 0)       cpSetTab(st, CpTab::Sessions);
        else if (std::strcmp(cpView, "settings") == 0)  cpSetTab(st, CpTab::Settings);
        else if (std::strcmp(cpView, "tasks") == 0)     cpSetTab(st, CpTab::Tasks);
        else if (std::strcmp(cpView, "artifacts") == 0) cpSetTab(st, CpTab::Artifacts);
    }

    // Pull the header flush to the window content top (column already +4 under ribbon).
    {
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                            ImVec2(ImGui::GetStyle().ItemSpacing.x, 0.0f));
        ImGui::SetCursorPosY(0.0f);
        drawCopilotHeader(st, busy);
        ImGui::PopStyleVar();
    }
    ImGui::Separator();

    // The title row + separator above IS the header strip a vertical collapse rolls up to
    // — measure it (cursor position right after, symmetric-padded) so PanelCollapseAnim knows the
    // exact collapsed target, same one-frame-lag trick as s_composerH below. Everything from the
    // runtime banner down is gated on bodyVisible() so a collapsed column costs nothing per frame.
    s_cpAnim.measureHeader();
    if (s_cpAnim.bodyVisible()) {
        if (!mobile) drawRuntimeBanner(st);

        // Settings / Sessions / Tasks / Artifacts overlays replace the chat log (and hide the
        // composer) while open. They are mutually exclusive (the header toggles clear the
        // others) and the ONLY way out is the header strip: the tab you are on, or Chat.
        //
        // There used to be a "a click outside the column dismisses them" rule here, keyed on
        // `IsMouseClicked(Left) && !IsWindowHovered(ChildWindows)`. It fired on the MOUSE-DOWN
        // frame of a click on the tab pill itself: IsWindowHovered() returns false whenever any
        // item is active (imgui.cpp, the `g.ActiveId != 0 && !g.ActiveIdAllowOverlap` bail) or a
        // popup is blocking, and the header's own hit zone had just taken ActiveId two hundred
        // lines above. So pressing "Settings" while on Settings dismissed to Chat, and the pill's
        // release then toggled Chat -> Settings straight back: the view flicked to the
        // conversation under the finger and snapped back on release (reproduced live, 2026-08-06).
        // No hover flag expresses "outside": the pointer is literally inside the column in every
        // one of those cases. This is a radio TAB strip, not a popover — tabs stay put until you
        // pick another one.
        const bool overlayOpen = cpActiveTab(st) != CpTab::Chat;
        static float s_composerH = 0.0f;
        const float bottomClearance = ui::metrics::U(2);
        const float spacingY = ImGui::GetStyle().ItemSpacing.y;
        const float composerGap = ui::metrics::U(1);
        float reserve = 0.0f;
        if (!overlayOpen) {
            reserve = (s_composerH > 0 ? s_composerH
                                       : ImGui::GetFrameHeightWithSpacing() * 4.0f) +
                      composerGap + spacingY * 3.0f + bottomClearance;
        } else {
            reserve = spacingY + bottomClearance;
        }
        const float latestFooterH = overlayOpen ? 0.0f : chatJumpToLatestFooterHeight();
        float chatH = ImGui::GetContentRegionAvail().y - reserve - latestFooterH;
        // The composer has priority in short windows; a fixed transcript minimum pushed it
        // below the column's bottom even when the composer reservation was already correct.
        chatH = std::max(chatH, ui::metrics::stroke(1.0f));

        // ONE SCROLL REGION PER VIEW. All five views used to share the id "chatlog", so ImGui kept
        // ONE scroll state for them: opening Sessions after scrolling the chat log landed mid-list,
        // and the settings view (which nested a second scroll child inside this one) never engaged
        // the pane's pill scrollbar or top fade at all. A distinct id per view gives each its own
        // ImGui window, its own scroll state, and the SAME custom scrollbar.
        const char* viewId = st.showAISettingsOverlay ? "cp_settings"
                           : st.showTaskList          ? "cp_tasks"
                           : st.showArtifacts         ? "cp_artifacts"
                           : st.showSessionsPicker    ? "cp_sessions"
                                                      : "cp_chat";
        // View switch: the incoming view fades in (menus also settle up from a few px below) over
        // ~kFast. It runs on wall-clock dt and never gates input; screenshot captures land at once.
        static ImGuiID s_viewShown = 0;
        static float   s_viewP = 1.0f;
        const ImGuiID  viewKey = ImHashStr(viewId);
        if (s_viewShown == 0) s_viewShown = viewKey;
        else if (viewKey != s_viewShown) { s_viewShown = viewKey; s_viewP = 0.0f; }
        if (st.shot) s_viewP = 1.0f;
        if (s_viewP < 1.0f) {
            s_viewP = std::min(1.0f, s_viewP + ui::anim::dt() / ui::anim::kFast);
            ui::anim::requestFrames();
        }
        const float viewE = ui::anim::easeOut(s_viewP);
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        // Horizontal gutter is ASYMMETRIC: the pill scrollbar is clipped to this child, so the
        // right side must reserve a full kChatScrollbarLane for it or bubbles run under the bar.
        // ImGui's WindowPadding is symmetric, so push the WIDE (right) value and pull the LEFT
        // edge back with a negative Indent below. Chat keeps its tight left gutter (bubbles/cards
        // need the column width); the MENU views keep the shared cpMenuGutter so
        // Sessions/Tasks/Artifacts/Settings all start on one left edge.
        // Menu content starts on the header's left edge: the window's own padding already
        // supplies part of the gutter, so only the remainder is added here (no double inset).
        const float outerPadX = ImGui::GetCursorPosX();
        const float padL = overlayOpen ? std::max(0.0f, ui::metrics::cpMenuGutter() - outerPadX)
                                       : 3.0f * ui::uiScale;
        const float padR = std::max(padL, ui::metrics::px(ui::kChatScrollbarLane));
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            overlayOpen
                                ? ImVec2(padR, ui::metrics::px(4.0f))
                                : ImVec2(padR, 2.0f * ui::uiScale));
        // NOTE: this used to force SetNextWindowScroll(-1,0) on every view change ("never open a
        // menu mid-scroll") from back when all five views shared ONE scroll id — closing over a
        // stale offset left over from a completely different view's content. Now that each view
        // gets its own BeginChild id (see above), ImGui already keeps a separate window->Scroll
        // per id and restores it on its own; forcing it to 0 here instead threw away the user's
        // own position in that view every time they left and came back (reported as "switching
        // tabs is jarring") and, worse, snapped Chat back to the top whenever the user had
        // scrolled up and stick-to-bottom was disengaged. Let each view remember itself.
        // Everything the view paints lands in its child window tree (and, for code that paints into
        // the parent list directly, in the outer window's list past this mark); both are faded by
        // vertex alpha after EndChild.
        ImDrawList* outerDl = ImGui::GetWindowDrawList();
        const int outerVtx0 = outerDl->VtxBuffer.Size;
        ImGui::BeginChild(viewId, ImVec2(0, chatH), ImGuiChildFlags_AlwaysUseWindowPadding,
                          ImGuiWindowFlags_NoScrollbar);
        ImGuiWindow* viewWin = ImGui::GetCurrentWindow();
        if (padR > padL) ImGui::Indent(padL - padR);   // negative: pull the LEFT edge back to padL
        if (mobile && overlayOpen) drawRuntimeBanner(st);
        if (overlayOpen && viewE < 1.0f)
            ImGui::SetCursorPosY(ImGui::GetCursorPosY() + (1.0f - viewE) * ui::metrics::px(6.0f));
        if (st.showAISettingsOverlay) {
            drawAISettingsBody(st, busy);
        } else if (st.showTaskList || st.showArtifacts || st.showSessionsPicker) {
            // The menu views run at the docked-pane rhythm whatever the host pushed around us.
            ui::kit::pushDensity(ui::kit::Density::Cozy);
            if (st.showTaskList)            drawTaskListPanel(st, busy);
            else if (st.showArtifacts)      drawArtifactsPanel(st, busy);
            else                            drawSessionsPanel(st, busy);
            ui::kit::popDensity();
        } else {
            drawChatLogWindowed(st);   // M19: render-windowed endless scroll-back
            chatLogStickToBottom("copilot.panel");
        }
        if (padR > padL) ImGui::Unindent(padL - padR);  // undo before the scrollbar/window reads
        // The top fade is a SCROLL AFFORDANCE, not chat decoration: every scrolled view gets it, so
        // a scrolled menu never looks like it simply starts mid-content.
        const bool  logScrolled = ImGui::GetScrollY() > 0.5f;
        const ImVec2 logP0 = ImGui::GetWindowPos();
        const float  logVisW = ImGui::GetWindowSize().x;
        // Chat scrollbar: 9px, sitting ON the right edge of the lane reserved by padR above.
        ui::drawWindowPillScrollbarV(chatSurf(), ui::kChatScrollbar);
        ImGui::EndChild();
        if (viewE < 1.0f) {
            const float fade = 0.2f + 0.8f * viewE;
            cpFadeWindowTree(viewWin, fade);
            cpFadeDrawList(outerDl, outerVtx0, fade);
        }

        // Top fade mask over the conversation scroll viewport.
        if (logScrolled) {
            const ImVec2 keep = ImGui::GetCursorPos();
            const float fadeH = ImGui::GetTextLineHeight() + ui::metrics::px(6.0f);
            ImGui::SetCursorScreenPos(logP0);
            ImGui::BeginChild("##cp_view_fade", ImVec2(logVisW, fadeH), ImGuiChildFlags_None,
                              ImGuiWindowFlags_NoInputs | ImGuiWindowFlags_NoScrollbar |
                              ImGuiWindowFlags_NoScrollWithMouse);
            const ui::AdaptiveSurface& fsurf = chatSurf();
            const ImU32 top = ImGui::ColorConvertFloat4ToU32(
                ImVec4(fsurf.tint.x, fsurf.tint.y, fsurf.tint.z, 0.92f));
            const ImU32 clear = ImGui::ColorConvertFloat4ToU32(
                ImVec4(fsurf.tint.x, fsurf.tint.y, fsurf.tint.z, 0.0f));
            ImGui::GetWindowDrawList()->AddRectFilledMultiColor(
                logP0, ImVec2(logP0.x + logVisW, logP0.y + fadeH), top, top, clear, clear);
            ImGui::EndChild();
            ImGui::SetCursorPos(keep);
        }
        // Reserved navigation gutter between the log and composer, never over question inputs.
        if (!overlayOpen)
            chatDrawJumpToLatest("copilot.panel");
        ImGui::PopStyleVar();   // per-view WindowPadding
        ImGui::PopStyleColor();

        // Composer is hidden under the header overlays (sessions / settings / tasks / artifacts).
        if (!overlayOpen) {
            ImGui::Dummy(ImVec2(0, composerGap));
            if (mobile)
                ImGui::BeginChild("##mobile_composer",
                    ImVec2(0, std::max(ui::metrics::stroke(1.0f),
                        ImGui::GetContentRegionAvail().y - bottomClearance - spacingY)),
                    ImGuiChildFlags_None, ImGuiWindowFlags_NoScrollbar);
            ImGui::BeginGroup();
            // Keep runtime actions scrollable with the composer on a keyboard-shortened screen.
            if (mobile) drawRuntimeBanner(st);
            ui::drawAgentOverview(st, ImGui::GetContentRegionAvail().x);
            drawComposer(st, busy, canRun);
            ImGui::EndGroup();
            s_composerH = ImGui::GetItemRectSize().y;
            if (mobile) {
                ui::drawWindowPillScrollbarV(chatSurf());
                ImGui::EndChild();
            }
            ImGui::Dummy(ImVec2(0, bottomClearance));
        } else {
            ImGui::Dummy(ImVec2(0, bottomClearance));
        }
    }  // s_cpAnim.bodyVisible()

    // Side-channel /btw answer panel (not part of chat history).
    drawBtwPanel(st);

    ImGui::End();
}

// ============================ AI workspace hub reuse hooks ============================
// Thin exported wrappers over the statics above (see copilot.h). They let the full-page
// AI workspace (host/src/ui/ai/ai_workspace.cpp) render the SAME conversation state, session
// list and composer/send path as the docked Copilot column — single implementation, two hosts.

void chatWorkspaceSync(AppState& st) {
    syncChatToDoc(st);
    maybePersistChatHistoryDebounced(st, /*force=*/false);
    ensureConversations(st);
}

bool chatTurnActive(AppState& st) { return aiTurnActive(st); }

void chatDrawLog(AppState& st) {
    drawChatLogWindowed(st);   // M19: render-windowed endless scroll-back (shared with drawAIPanel)
}

void chatDrawComposer(AppState& st, bool busy, bool canRun) { drawComposer(st, busy, canRun); }

void chatDrawSessionsList(AppState& st, bool busy) { drawSessionsPanel(st, busy); }


}  // namespace ui
