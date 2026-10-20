// Chudware Copilot pane (T-RF6 split of legacy_chat.cpp — this file is the pane host).
//
// A PINNED, full-height, rounded liquid-glass column overlaid on the viewport's right edge with
// ~12dp margins (geometry from ui::viewportBackdrop(); visible left-edge resize boundary + grip;
// the docked task-pane layout is gone — this is the only layout). Top-down: a compact flat
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
#include "gl_compat.h"           // glDeleteTextures — LRU eviction of render thumbnails
#include "ui/chudcode_session_actions.h"  // Chudcode session recovery (export/branch/repair/close)
#include <string_view>
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#include "core/jobs.h"
#endif

namespace ui {

// ---- header: collapse + [brand | Chat Tasks Artifacts ... gear] / [Sessions ... + New chat] ----
// Copilot vertical-collapse state (file scope so the header's collapse chevron and drawAIPanel's
// height animation share it). Mirrors the FeatureManager collapse (T-G21: rolls UP to this header
// strip instead of shrinking to a pill in both axes). Seeded from st.settings.copilotCollapsed on
// drawAIPanel's first call (see s_cpInited there) so a collapsed column stays collapsed across
// restarts.
static bool s_cpCollapsed = false;
// The header's tabs are a RADIO set, not independent toggles: exactly one of Chat / Tasks /
// Artifacts / Sessions / Settings is showing, and Chat is "no overlay". Sessions lives on the
// second header row but belongs to the same set. AppState carries it as four bools
// (integrator-owned), so funnel every read and write through these two so a new surface can
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

static void drawCopilotHeader(AppState& st, bool busy) {
    const auto& pal = ui::palette;
    const CpTab tabIn = cpActiveTab(st);
    CpTab tabOut = tabIn;
    // Compact logical metrics, independent of the global form-control padding.
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(ui::metrics::px(6.0f), ui::metrics::px(3.0f)));
    const float h = ImGui::GetFrameHeight();
    const float mark = ui::metrics::px(26.0f);
    const float rowH = std::max(mark, h);
    const float gap = ui::metrics::px(4.0f);
    const float left = ui::metrics::U(2.0f);  // clear the column resize strip
    const float right = ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x;
    const float avail = right - left;
    const float top = ImGui::GetCursorPosY() + ui::metrics::px(3.0f);
    const float iconW = h;
    auto textW = [&](const char* label) {
        return ImGui::CalcTextSize(label).x + ui::metrics::px(12.0f);
    };

    int done = 0, total = 0;
    copilotTaskProgress(st, done, total);
    char tasks[48];
    if (total > 0) std::snprintf(tasks, sizeof(tasks), "Tasks %d/%d", done, total);
    else std::snprintf(tasks, sizeof(tasks), "Tasks");
    const float sessW = textW("Sessions");
    const float newW = textW("New chat");
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
    x = oneRow ? x : left;
    const auto sessions = hit("##hdr_sess", sessW, actionY);
    if (!oneRow) x = right - newW;
    const auto fresh = hit("##hdr_new", newW, actionY);
    if (sessions.clicked) tabOut = cpTabAfterClick(tabIn, CpTab::Sessions);
    if (fresh.clicked) tabOut = CpTab::Chat;

    x = right - iconW * 2.0f - gap;
    const auto gear = hit("##hdr_gear", iconW, navY);
    if (gear.clicked) tabOut = cpTabAfterClick(tabIn, CpTab::Settings);
    ImGui::SetCursorPos(ImVec2(right - iconW, navY));
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(chatSurf().tint.x, chatSurf().tint.y, chatSurf().tint.z, 0.0f));
    if (ImGui::ArrowButton("##cp_collapse", s_cpCollapsed ? ImGuiDir_Down : ImGuiDir_Up)) {
        s_cpCollapsed = !s_cpCollapsed;
        st.settings.copilotCollapsed = s_cpCollapsed;
        saveSettings(st.settings);
    }
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("%s", s_cpCollapsed ? "Expand Copilot" : "Collapse Copilot");
    if (tabOut != tabIn) cpSetTab(st, tabOut);
    if (fresh.clicked) {
        newConversation(st);
        composerSetText(st, "");
        st.pendingAttachments.clear();
        st.pendingInlineImagePaths.clear();
        st.pendingContextRefs.clear();
        st.contextPickActive = false;
    }
    // Text-only tabs when there is room; icon-only tabs at small widths. The same measured
    // widths drive hits and paint, and all selection paints use this frame's resolved tab.
    headerTextButtonPaint(chat, "Chat", tabOut == CpTab::Chat, chatText ? nullptr : "ai_panel", chatText);
    headerTextButtonPaint(task, tasks, tabOut == CpTab::Tasks, tasksText ? nullptr : "plan_board", tasksText);
    headerTextButtonPaint(arts, "Artifacts", tabOut == CpTab::Artifacts, artsText ? nullptr : "artifacts", artsText);
    headerTextButtonPaint(sessions, "Sessions", tabOut == CpTab::Sessions);
    headerTextButtonPaint(fresh, "New chat", false);
    headerTextButtonPaint(gear, "Settings", tabOut == CpTab::Settings, "settings", false);
    if (chat.hovered) ImGui::SetTooltip("Chat \xc2\xb7 the current conversation");
    else if (task.hovered) {
        if (total > 0) ImGui::SetTooltip("Tasks \xc2\xb7 %d of %d complete", done, total);
        else ImGui::SetTooltip("Tasks \xc2\xb7 this session's plan and progress");
    } else if (arts.hovered) ImGui::SetTooltip("Artifacts \xc2\xb7 documents, renders and files");
    else if (sessions.hovered) ImGui::SetTooltip("Sessions \xc2\xb7 chats in the active project");
    else if (fresh.hovered) ImGui::SetTooltip("New chat \xc2\xb7 start a fresh session");
    else if (gear.hovered) ImGui::SetTooltip("AI settings");
    ImGui::SetCursorPos(ImVec2(left, actionY + h + ui::metrics::px(3.0f)));
    ImGui::Dummy(ImVec2(0, 0));
    ImGui::PopStyleVar();
}

// In-flight marker for a session row: the brand tesseract spinner at the row's left edge,
// animated by the same per-frame repaint that streams the run itself. Returns the extra
// x-indent title/subtitle text must use to clear the icon in every project-scoped Sessions row.
static float drawSessionRunningIcon(ImDrawList* dl, const ui::AdaptiveSurface& surf,
                                    const ImVec2& rowMin, float rowH, float padX, float sc) {
    const float sz = std::max(ui::kTesseractSpinnerMinPx, rowH - 14.0f * sc);
    const ImVec2 center(rowMin.x + padX + sz * 0.5f, rowMin.y + rowH * 0.5f);
    ImVec4 accInk = semanticOnGlass(ui::palette.accent, surf);
    accInk.w = 1.0f;
    ui::drawTesseractSpinner(dl, center, sz, ImGui::GetTime(),
                             ImGui::ColorConvertFloat4ToU32(accInk));
    return sz + 6.0f * sc;  // icon box + gap before the text
}


// ---- Chudcode session recovery card (Sessions rows -> Manage) --------------------------------
// Per-session export / diagnose / repair / branch / runtime-close against the exact worker
// methods chudcode_export_session / chudcode_repair_session / chudcode_branch_session /
// chudcode_close_session. ONE card is open at a time; every async completion is generation-
// guarded so a card switched mid-flight never adopts a stale response.
namespace {

struct CcManageUi {
    std::string convId;          // session the card is open for; empty = closed
    uint64_t gen = 0;            // bumped on open/close — stale RPC completions drop
    bool exportBusy = false, doctorBusy = false, closeBusy = false, branchBusy = false;
    bool repairMode = false;     // the in-flight doctor call is a repair, not a dry-run
    bool confirmRepair = false;  // inline confirm armed (explicit in-app confirmation)
    std::string err;             // last action failure (inlineStatus)
    std::string exportPath;      // last successful export destination
    bool haveDoctor = false;
    ui::ChudcodeDoctorSummary doctor;  // last diagnose/repair summary
    std::string backupPath;      // backup written by the last repair

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
    if (chudcodeSessionRpcAbsent(r.ok, r.err, r.result)) {
        chudcodeSessionServiceAvailable() = false;  // older service — whole surface off
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
    if (chudcodeSessionNotLive(r.result)) {
        // Clean refusal, not an error: the runtime is gone (restart / closed). Record the
        // truth so the buttons disable with the resume hint instead of retrying.
        chudcodeSessionLinkEnsure(convId).runtimeLive = false;
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
    st.rpc.call(kChudcodeExportSessionMethod,
                chudcodeExportSessionParams(convId, outputPath),
                [stp, gen, convId](const core::RpcResult& r) {
        if (s_ccm.gen != gen) return;
        s_ccm.exportBusy = false;
        if (ccConsumeFailure(convId, r, "Export")) return;
        ChudcodeExportResult res;
        if (!parseChudcodeExportResult(r.result, res)) {
            s_ccm.err = chudcodeActionError(r.result, "Export failed.");
            return;
        }
        s_ccm.exportPath = res.path;
        stp->toast(ui::ToastLevel::Success, "Transcript exported \xc2\xb7 " + res.path);
        chatArtifactOpenSmart(*stp, res.path);  // export SHOWS its destination and OPENS it
    }, 120000);
}

// One completion shape for Diagnose (repair=false, read-only) and Repair (repair=true,
// backup-first): both return the bounded doctor summary; only repair may carry backup_path.
void ccStartDoctor(AppState& st, const std::string& convId, bool repair) {
    s_ccm.doctorBusy = true;
    s_ccm.repairMode = repair;
    s_ccm.confirmRepair = false;
    s_ccm.err.clear();
    const uint64_t gen = s_ccm.gen;
    AppState* stp = &st;
    st.rpc.call(kChudcodeRepairSessionMethod,
                chudcodeRepairSessionParams(convId, repair),
                [stp, gen, convId, repair](const core::RpcResult& r) {
        if (s_ccm.gen != gen) return;
        s_ccm.doctorBusy = false;
        if (ccConsumeFailure(convId, r, repair ? "Repair" : "Diagnose")) return;
        ChudcodeDoctorSummary summary;
        if (!parseChudcodeDoctorSummary(r.result, summary)) {
            s_ccm.err = chudcodeActionError(
                r.result, repair ? "Repair returned no summary." : "Diagnose returned no summary.");
            return;
        }
        s_ccm.haveDoctor = true;
        s_ccm.doctor = summary;
        if (!summary.backupPath.empty()) s_ccm.backupPath = summary.backupPath;
        // Health (and any reported id/file) persists with the conversation.
        applyChudcodeDoctor(chudcodeSessionLinkEnsure(convId), summary);
#ifdef __EMSCRIPTEN__
        chudcodeSessionLinkSet(convId, chudcodeSessionLinkEnsure(convId));
#endif
        markChatHistoryDirty();
        if (repair)
            stp->toast(summary.changed ? ui::ToastLevel::Success : ui::ToastLevel::Info,
                       summary.changed
                           ? "Session file repaired \xe2\x80\x94 backup kept."
                           : "Nothing to repair \xe2\x80\x94 session file left untouched.");
    }, 120000);
}

void ccStartClose(AppState& st, const std::string& convId) {
    s_ccm.closeBusy = true;
    s_ccm.err.clear();
    const uint64_t gen = s_ccm.gen;
    AppState* stp = &st;
    st.rpc.call(kChudcodeCloseSessionMethod, chudcodeCloseSessionParams(convId),
                [stp, gen, convId](const core::RpcResult& r) {
        if (s_ccm.gen != gen) return;
        s_ccm.closeBusy = false;
        if (chudcodeSessionRpcAbsent(r.ok, r.err, r.result)) {
            chudcodeSessionServiceAvailable() = false;
            return;
        }
        if (!r.ok) {
            s_ccm.err = core::workerLaneBusy(r)
                            ? std::string(kAvailableAfterRun)
                            : "Close failed: " +
                                  userFacingRpcError(r, "the AI service did not respond.");
            return;
        }
        if (!parseChudcodeCloseResult(r.result)) {
            s_ccm.err = chudcodeActionError(r.result, "Close failed.");
            return;
        }
        // Idempotent by contract: the runtime is gone either way; the persisted file stays.
        chudcodeSessionLinkEnsure(convId).runtimeLive = false;
        stp->toast(ui::ToastLevel::Info,
                   "Runtime closed \xe2\x80\x94 transcript kept. Sending a message resumes it.");
    }, 30000);
}

void ccStartBranch(AppState& st, const std::string& convId, const std::string& entryId) {
    s_ccm.branchBusy = true;
    s_ccm.err.clear();
    const uint64_t gen = s_ccm.gen;
    AppState* stp = &st;
    st.rpc.call(kChudcodeBranchSessionMethod,
                chudcodeBranchSessionParams(convId, entryId),
                [stp, gen, convId](const core::RpcResult& r) {
        if (s_ccm.gen == gen) s_ccm.branchBusy = false;
        if (s_ccm.gen == gen && ccConsumeFailure(convId, r, "Branch")) return;
        ChudcodeBranchResult br;
        if (!parseChudcodeBranchResult(r.result, br)) {
            if (s_ccm.gen == gen)
                s_ccm.err = chudcodeActionError(r.result, "Branch failed.");
            return;
        }
        AppState& st2 = *stp;
        int idx = -1;
        for (int i = 0; i < (int)st2.conversations.size(); ++i)
            if (st2.conversations[i].id == convId) { idx = i; break; }
        if (idx < 0) {
            st2.toast(ui::ToastLevel::Warn,
                      "Branch succeeded but the source session is gone \xe2\x80\x94 not mapped.");
            return;
        }
        // Chudware lineage: the fork copies the transcript, revert branches and raw logs.
        const int forkIdx = forkConversation(st2, idx);
        if (forkIdx < 0) {
            st2.toast(ui::ToastLevel::Error, "Branch succeeded but forking the session failed.");
            return;
        }
        // The service branched the runtime in place, captured the child and disposed the
        // runtime (backend contract). The ORIGINAL resumes later from its own persisted
        // file; the FORK owns the returned child session id/file and resumes from those.
        chudcodeSessionLinkEnsure(convId).runtimeLive = false;
        ChudcodeSessionLink forkLink;
        forkLink.sessionId = br.sessionId;
        forkLink.sessionFile = br.sessionFile;
        chudcodeSessionLinkSet(st2.conversations[forkIdx].id, std::move(forkLink));
#ifdef __EMSCRIPTEN__
        bindCellAgentSessionHandle(st2.conversations[forkIdx].id, br.sessionId,
                                  cellAgentSessionProvider(convId));
#endif
        markChatHistoryDirty();
        st2.toast(ui::ToastLevel::Success,
                  "Branched into \"" + st2.conversations[forkIdx].title +
                      "\" \xe2\x80\x94 its next message resumes from the branch point.");
        s_ccm.close();  // forkConversation switched to the new chat; the card follows
    }, 60000);
}

// The expanded recovery card, drawn directly under its session row.
void drawChudcodeManageCard(AppState& st, const Conversation& c, bool running) {
    const std::string& convId = c.id;
    const ChudcodeSessionLink* link = chudcodeSessionLink(convId);
    const bool serviceOk = chudcodeSessionServiceAvailable();
#ifdef __EMSCRIPTEN__
    const bool live = link && link->runtimeLive && !cellAgentSessionHandle(convId).empty();
#else
    const bool live = link && link->runtimeLive;
#endif
    const bool busy = s_ccm.exportBusy || s_ccm.doctorBusy || s_ccm.closeBusy || s_ccm.branchBusy;
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
            ImGui::SameLine(0.0f, ui::metrics::U(1.0f));
            const std::string health = link ? link->health : std::string();
            const int hc = chudcodeHealthClass(health);
            ui::kit::chip(chudcodeHealthLabel(health),
                          hc == 0 ? ui::kit::Tone::Ok
                                  : hc == 1 ? ui::kit::Tone::Warn : ui::kit::Tone::Neutral,
                          /*dot=*/true, "Last session-file health verdict (persisted).");
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
            if (ui::kit::secondaryButton(
                    s_ccm.doctorBusy && !s_ccm.repairMode ? "Diagnosing\xe2\x80\xa6" : "Diagnose",
                    actionable, whyNot))
                ccStartDoctor(st, convId, /*repair=*/false);
            if (ImGui::IsItemHovered() && actionable)
                ImGui::SetTooltip("Read-only session-file check \xe2\x80\x94 changes nothing.");
            ImGui::SameLine(0.0f, ui::metrics::U(1.0f));
            if (ui::kit::secondaryButton(
                    s_ccm.doctorBusy && s_ccm.repairMode ? "Repairing\xe2\x80\xa6" : "Repair\xe2\x80\xa6",
                    actionable, whyNot))
                s_ccm.confirmRepair = !s_ccm.confirmRepair;
            ImGui::SameLine(0.0f, ui::metrics::U(1.0f));
            if (ui::kit::secondaryButton(s_ccm.closeBusy ? "Closing\xe2\x80\xa6" : "Close runtime",
                                         serviceOk && live && !runActive && !busy,
                                         !live ? "No live runtime to close \xe2\x80\x94 already inactive."
                                               : whyNot))
                ccStartClose(st, convId);
            if (ImGui::IsItemHovered() && live && !runActive)
                ImGui::SetTooltip("Stops the service-side process only. The transcript and the "
                                  "persisted session file are kept \xe2\x80\x94 unlike Delete.");

            // Explicit in-app confirmation before anything rewrites the session file.
            if (s_ccm.confirmRepair) {
                ui::kit::dimText("Repair rewrites the session file after saving a backup. "
                                 "Findings that would require inventing data are left untouched.");
                if (ui::kit::dangerButton("Repair now", actionable, whyNot))
                    ccStartDoctor(st, convId, /*repair=*/true);
                ImGui::SameLine(0.0f, ui::metrics::U(1.0f));
                if (ui::kit::secondaryButton("Cancel")) s_ccm.confirmRepair = false;
            }

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

            // ---- doctor summary -------------------------------------------------------------
            if (s_ccm.haveDoctor) {
                const ChudcodeDoctorSummary& d = s_ccm.doctor;
                const int hcd = chudcodeHealthClass(d.status);
                ui::kit::sectionHeader(d.mode == "repair" ? "Repair result" : "Diagnosis",
                                       nullptr, nullptr, -1, false);
                ui::kit::chip(chudcodeHealthLabel(d.status),
                              hcd == 0 ? ui::kit::Tone::Ok
                                       : hcd == 1 ? ui::kit::Tone::Warn : ui::kit::Tone::Neutral,
                              /*dot=*/true);
                if (d.findingCount > 0) {
                    char line[128];
                    std::snprintf(line, sizeof(line), "%d finding%s \xc2\xb7 %d repairable%s",
                                  d.findingCount, d.findingCount == 1 ? "" : "s",
                                  d.repairableFindingCount < 0 ? 0 : d.repairableFindingCount,
                                  d.remainingFindingCount > 0 ? " \xc2\xb7 issues remain" : "");
                    ui::kit::dimText(line);
                }
                if (!s_ccm.backupPath.empty()) {
                    ui::kit::keyValueRow("Backup", s_ccm.backupPath.c_str(),
                                         ui::kit::Tone::Neutral, /*copyable=*/true);
                    if (ui::haveOsFileManager() && ui::kit::secondaryButton("Show backup"))
                        ui::revealInFileManager(s_ccm.backupPath);
                }
                for (int fi = 0; fi < (int)d.findings.size(); ++fi) {
                    const ChudcodeDoctorFinding& f = d.findings[fi];
                    ImGui::PushID(fi);
                    ui::kit::chip(f.code.empty() ? "finding" : f.code.c_str(),
                                  f.repairable ? ui::kit::Tone::Warn : ui::kit::Tone::Err,
                                  /*dot=*/false,
                                  f.repairable ? "Repair can fix this finding."
                                               : "Not automatically repairable.");
                    if (!f.entryId.empty()) {
                        // A finding anchored on a Chudcode DAG entry is a branch point: fork
                        // this conversation and continue from just before the damage.
                        ImGui::SameLine(0.0f, ui::metrics::U(1.0f));
                        if (ui::kit::secondaryButton(
                                s_ccm.branchBusy ? "Branching\xe2\x80\xa6" : "Branch here",
                                actionable && !s_ccm.branchBusy, whyNot))
                            ccStartBranch(st, convId, f.entryId);
                        if (ImGui::IsItemHovered() && actionable)
                            ImGui::SetTooltip("Fork this session and continue from entry %s "
                                              "(the original keeps its full history).",
                                              f.entryId.c_str());
                    }
                    if (!f.message.empty()) ui::kit::dimText(f.message.c_str());
                    ImGui::PopID();
                }
                if (hcd == 1 && !d.findings.empty() &&
                    std::none_of(d.findings.begin(), d.findings.end(),
                                 [](const ChudcodeDoctorFinding& f) { return !f.entryId.empty(); }))
                    ui::kit::dimText("No finding is anchored on a session entry \xe2\x80\x94 "
                                     "branching is unavailable for this damage.");
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
        const std::string key = convProjectKey(c.projectDir);
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

// Header switcher: a popover listing every project that has sessions, the viewed one checked.
void drawSessionsProjectSwitcher(const std::vector<SessProjectEntry>& projects, int unassigned,
                                 const std::string& activeKey, float maxLabelW) {
    SessScopeState& S = s_sessScope;
    std::string label;
    if (S.scope == SessScope::All)             label = "All projects";
    else if (S.scope == SessScope::Unassigned) label = "Unassigned";
    else {
        label = "No project";
        for (const auto& e : projects)
            if (e.key == S.key) { label = e.name; break; }
    }
    label = ui::kit::elide(label.c_str(), maxLabelW);
    if (!ui::kit::popoverBegin("##sess_project", label.c_str())) {
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(S.scope == SessScope::All
                ? "Showing sessions from every project. Click to pick one project."
                : S.scope == SessScope::Unassigned
                    ? "Showing sessions with no project. Click to pick a project."
                    : "Showing this project's sessions only. Click to view another project's.");
        return;
    }
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
    ui::kit::popoverEnd();
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
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();

    ensureConversations(st);

    // One title treatment for every copilot menu (ui_common cpMenu*). The project switcher and
    // the "Show archived" filter sit in the header's accessory slot — above the rule, next to
    // the title cluster — instead of full body rows between the button and the list.
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
        drawSessionsProjectSwitcher(projects, unassigned, activeKey,
                                    ImGui::GetContentRegionAvail().x * 0.45f);
        ImGui::SameLine(0.0f, ui::metrics::U(1.0f));
        // The kit checkbox paints its own themed box (the raw ImGui box rendered as a bare black
        // block on dark glass). Its ink still comes from the glass surface like every other label
        // in this menu: the theme's default text tone vanished against the bright viewport
        // backdrop showing through the copilot column.
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ui::kit::checkbox("sess_show_archived", "Show archived", &s_showArchived,
                          s_showArchived
                              ? "Showing archived sessions only. Turn off to return to your "
                                "active list."
                              : "Archived sessions are hidden from this list \xe2\x80\x94 turn "
                                "on to review or restore them. Archiving keeps the full "
                                "transcript (unlike Delete).");
        ImGui::PopStyleColor();
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

    // No new-session action here: in the dock, the pinned header's "New chat" (row 2, next to
    // the Sessions toggle) is the one way to start a session from any overlay; the full AI
    // workspace carries New session in its own chat header. This panel only lists, switches
    // and manages the viewed project's sessions.

    const ImGuiStyle& style = ImGui::GetStyle();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    // Row shape/rhythm from the shared copilot-menu tokens, so session rows, artifact cards and
    // task rows carry ONE corner, ONE inner padding and ONE gap.
    const float padX     = ui::metrics::cpMenuRowPadX();
    const float padY     = ui::metrics::cpMenuRowPadY();
    const float rowGap   = ui::metrics::cpMenuRowGap();
    const float rowRnd   = ui::metrics::cpMenuRowRound();   // already DPI-scaled: direct draw calls
    const float rowRndDp = rowRnd / sc;                     // glassRowBackground scales internally
    const float fh = ImGui::GetFontSize();
    const float titleH = fh;
    const float subH  = fh * 0.92f;
    const float rowH  = padY * 2.0f + titleH + 2.0f * sc + subH;
    const float trashSz = ImGui::GetFrameHeight();
    // Project scope comes from the session's OWN stored projectDir (captured at creation,
    // restamped per turn), never from a live scan of what it produced. Only the viewed
    // project's sessions are listed; sessions with no stored project appear ONLY under
    // "Unassigned" (they used to leak into every project's list).
    const std::string viewKey = scope.scope == SessScope::Unassigned ? std::string() : scope.key;
    bool anyRowShown = false;
    // Newest sessions first: newConversation() appends, so the tail of st.conversations is the
    // most recent session — walk the vector back-to-front so it lands at the TOP of the list.
    for (int i = (int)st.conversations.size() - 1; i >= 0; --i) {
        Conversation& c = st.conversations[i];
        if (c.archived != s_showArchived) continue;
        // The active transcript lives in st.chat; parked conversations retain theirs in
        // Conversation::msgs.
        if (!allProjects && convProjectKey(c.projectDir) != viewKey) continue;
        const std::vector<ChatMsg>& msgs = (i == st.activeConv) ? st.chat : c.msgs;
        anyRowShown = true;
        // Fallback number is the conversation's own slot, not the display row — with the
        // newest-first order a row position changes every session, a slot number does not.
        std::string title = c.title.empty() ? ("Session " + std::to_string(i + 1)) : c.title;
        // Truthful per-session state: this conversation's own run, not a global flag. The
        // trailing-message scan catches one-shot RPC turns (agent_ask_v2 …) that never get
        // a JobState in st.runs.
        const bool running = st.convRunning(c.id) ||
            (!msgs.empty() && !msgs.back().fromUser && msgs.back().running);
        bool active = (i == st.activeConv);

        char sub[96];
        const int nMsg = (int)msgs.size();
        if (nMsg == 0)
            std::snprintf(sub, sizeof(sub), "Empty session");
        else if (nMsg == 1)
            std::snprintf(sub, sizeof(sub), "1 message");
        else
            std::snprintf(sub, sizeof(sub), "%d messages", nMsg);
        const char* archLabel = c.archived ? "Restore" : "Archive";
        const float archBtnW = ImGui::CalcTextSize(archLabel).x + 16.0f * sc;
        const float forkBtnW = ImGui::CalcTextSize("Fork").x + 16.0f * sc;
        const float manageBtnW = ImGui::CalcTextSize("Manage").x + 16.0f * sc;
        const float actionGap = 4.0f * sc;
        // Action-zone geometry must be computed ONCE and shared by the title clip and the
        // buttons below: the reserve used to assume the trash button was trashSz wide, but
        // the drawn ImageButton is img + FramePadding*2 (wider) — the Archive button slid
        // left over the title text on active/hovered rows.
        const float delImg  = trashSz - 8.0f * sc;
        const float delBtnW = delImg + style.FramePadding.x * 2.0f;
        const float delBtnH = delImg + style.FramePadding.y * 2.0f;
        const float rightMargin = 4.0f * sc;
        // The overflow button is PERMANENT on every row — the visible sign that a session can be
        // managed, and the only route to its actions without a hover-capable pointer. The hover
        // buttons stay as a shortcut to its left.
        const float moreW = delBtnH;
        const float moreReserve = moreW + rightMargin + actionGap;
        const bool renaming = s_rename.id == c.id;

        ImGui::PushID(i);
        const ImVec2 rowMin = ImGui::GetCursorScreenPos();
        const float  rowW   = ImGui::GetContentRegionAvail().x;
        const ImVec2 rowMax(rowMin.x + rowW, rowMin.y + rowH);

        // Every row reserves the permanent overflow button; the hover shortcut buttons
        // (Manage/Fork/Archive/Delete) only EXIST while the pointer is on the row, so only that
        // row reserves their width. The ACTIVE row is marked by its highlight, not by permanent
        // buttons — showing them there squeezed the open session's title to one letter. Hover is
        // tested against the row rect directly: the reserve is needed BEFORE this frame's
        // InvisibleButton reports its hover state.
        const bool rowHot = ImGui::IsMouseHoveringRect(rowMin, rowMax);
        // Shortcuts are added right-to-left (Delete, Archive, Fork, Manage) only while the title
        // keeps a readable share of the row; whatever does not fit stays in the overflow menu,
        // which carries every action. The title is never covered.
        const float titleKeep = std::max(ui::metrics::px(110.0f), rowW * 0.45f);
        const float shortcutRoom = rowW - padX * 2.0f - moreReserve - titleKeep;
        float actionsW = 0.0f;
        bool showDelBtn = false, showArchBtn = false, showForkBtn = false, showManageBtn = false;
        auto takeShortcut = [&](float w, bool& flag) {
            if (actionsW + w + actionGap > shortcutRoom) return false;
            actionsW += w + actionGap;
            flag = true;
            return true;
        };
        if (rowHot && !renaming && takeShortcut(delBtnW, showDelBtn) &&
            takeShortcut(archBtnW, showArchBtn) && takeShortcut(forkBtnW, showForkBtn))
            takeShortcut(manageBtnW, showManageBtn);
        const float actionsReserve = moreReserve + actionsW;

        // While the title is an edit box the row must not also be a click target: the button
        // would claim hover first and swallow the clicks meant for the text field.
        bool picked = false;
        const ImVec2 pickSize(std::max(1.0f, rowW - actionsReserve - padX), rowH);
        if (renaming) {
            ImGui::Dummy(pickSize);
        } else {
            picked = ImGui::InvisibleButton("##pickconv", pickSize);
            if (ImGui::IsItemClicked(ImGuiMouseButton_Right)) ImGui::OpenPopup("##sessrow_menu");
        }
        // NOTE: do NOT use IsItemHovered() on ##pickconv for row-hover logic — that
        // button is shrunk to exclude the action zone, so its hover drops the moment
        // the mouse reaches for the Archive/Delete buttons and they vanish under the
        // cursor. rowHot (the full-row rect test above) is the one true predicate.
        // Picking stays live during a run (multi-session): switching parks the viewed
        // conversation; its run keeps streaming into it in the background.
        if (picked) {
            // Always land in chat for the chosen session — including re-clicking
            // the already-active one (switchConversation early-returns on same idx).
            if (i != st.activeConv)
                switchConversation(st, i);
            cpSetTab(st, CpTab::Chat);
        }

        // Re-read the selection: `active` above was captured BEFORE the click that just
        // switched conversations, and painting from it left the row you clicked unhighlighted
        // for a frame (and the old row still lit).
        active = (i == st.activeConv);
        float wash = active ? 0.55f : (rowHot ? 0.44f : 0.34f);
        ui::glassRowBackground(dl, rowMin, rowMax, rowRndDp, false, active, wash);

        // Titles and snippets are user text of arbitrary length and are painted straight onto
        // the draw list, which does NOT clip to the row — a long snippet used to run out past
        // the card, under the scrollbar and off the column edge. Clip both to the room they
        // actually have (title also yields the action-button zone) with a real ellipsis.
        const float titleY = rowMin.y + padY;
        // In-flight sessions get the animated tesseract icon at the row's left edge; text
        // indents past it only for those rows.
        const float runIndent = running ? drawSessionRunningIcon(dl, surf, rowMin, rowH, padX, sc)
                                        : 0.0f;
        const float textMaxW = std::max(ui::metrics::px(24.0f),
                                        rowW - padX * 2.0f - actionsReserve - runIndent);
        if (renaming) {
            // Inline rename: Enter or clicking away saves, Esc cancels. An empty name hands the
            // session back to its automatic title.
            ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(4.0f * sc, 1.0f * sc));
            ImGui::SetCursorScreenPos(ImVec2(rowMin.x + padX + runIndent,
                                             std::max(rowMin.y, titleY - 1.0f * sc)));
            ImGui::SetNextItemWidth(textMaxW);
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
        } else {
            const std::string titleShown = ui::kit::elide(title.c_str(), textMaxW);
            ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
            dl->AddText(ImVec2(rowMin.x + padX + runIndent, titleY), ink, titleShown.c_str());
        }

        // Project-matching sessions can optionally carry a docLink reference. Show the linked
        // document's name on the sub line; clicking it jumps to that document's tab when open.
        // file: links always render (basename); doc: links only resolve while the tab is open,
        // otherwise the badge is hidden.
        // Resolved BEFORE the subtitle so the sub's clip yields room for the badge. The badge
        // is skipped on hover/active rows: its right-aligned slot is exactly where the
        // Archive/Delete action buttons appear, and painting both stacked the two (plus the
        // badge's invisible click target swallowed the row-action clicks).
        std::string badge;
        int badgeTab = -1;
        const bool showBadge = !rowHot;
        if (showBadge && c.docLink.rfind("file:", 0) == 0) {
            const std::string p = c.docLink.substr(5);
            const size_t slash = p.find_last_of("/\\");
            badge = slash == std::string::npos ? p : p.substr(slash + 1);
            for (int ti = 0; ti < (int)st.docTabs.size(); ++ti)
                if (st.docTabs[ti].path == p) { badgeTab = ti; break; }
        } else if (showBadge && c.docLink.rfind("doc:", 0) == 0) {
            const std::string id = c.docLink.substr(4);
            for (int ti = 0; ti < (int)st.docTabs.size(); ++ti)
                if (st.docTabs[ti].docId == id) {
                    // t.title can still be a placeholder ("Part1") or an opaque id;
                    // displayTitle() resolves the handoff/tree/path name instead.
                    badgeTab = ti; badge = st.docTabs[ti].displayTitle(); break;
                }
        }
        float badgeW = 0.0f;
        float badgeX0 = 0.0f, badgeX1 = 0.0f;
        if (!badge.empty()) {
            // Never let a long filename eat the whole sub line — cap at half the row.
            badge = ui::kit::elide(badge.c_str(),
                                   std::max(ui::metrics::px(24.0f), rowW * 0.5f - padX));
            badgeW = ImGui::CalcTextSize(badge.c_str()).x + 8.0f * sc;
            // Right-aligned just left of the permanent overflow button — hover/active rows never
            // reach here (showBadge above), so this never collides with the action buttons.
            badgeX1 = rowMax.x - moreReserve;
            badgeX0 = std::max(rowMin.x + padX + runIndent, badgeX1 - badgeW);
        }
        // Clip the subtitle to the room it ACTUALLY has: up to the badge's left edge when a
        // badge is drawn, else up to the row's action zone (overflow button, plus the hover
        // buttons on a hot row).
        const float subRight = badgeW > 0.0f ? badgeX0 - 6.0f * sc : rowMax.x - actionsReserve;
        const float subMaxW = std::max(ui::metrics::px(24.0f),
                                       subRight - (rowMin.x + padX + runIndent));
        dl->AddText(ImVec2(rowMin.x + padX + runIndent, titleY + titleH + 2.0f * sc),
                    ImGui::ColorConvertFloat4ToU32(surf.textSecondary),
                    ui::kit::elide(sub, subMaxW).c_str());
        // "All projects" reveals which project each session belongs to: a dimmed column on
        // the sub line, right of the message count. Unassigned sessions say so explicitly
        // rather than looking like they belong to the current project.
        if (allProjects) {
            const float subW = ImGui::CalcTextSize(ui::kit::elide(sub, subMaxW).c_str()).x;
            const float projX = rowMin.x + padX + runIndent + subW + 8.0f * sc;
            const float projMaxW = subRight - projX;
            if (projMaxW > ui::metrics::px(16.0f)) {
                const std::string projName =
                    c.projectDir.empty() ? std::string("Unassigned") : sessProjectName(c.projectDir);
                ImVec4 projInk = surf.textTertiary;
                projInk.w = 1.0f;
                dl->AddText(ImVec2(projX, titleY + titleH + 2.0f * sc),
                            ImGui::ColorConvertFloat4ToU32(projInk),
                            ui::kit::elide(projName.c_str(), projMaxW).c_str());
            }
        }

        if (!badge.empty()) {
            const ImVec2 bsz = ImGui::CalcTextSize(badge.c_str());
            const float bx0 = badgeX0, bx1 = badgeX1;
            const float by0 = rowMin.y + padY + titleH + 2.0f * sc
                              + (subH - bsz.y) * 0.5f - 1.0f * sc;
            const float by1 = by0 + bsz.y + 2.0f * sc;
            ImVec4 badgeBg(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.10f);
            if (badgeTab < 0) badgeBg.w = 0.05f;
            dl->AddRectFilled(ImVec2(bx0, by0), ImVec2(bx1, by1),
                              ImGui::ColorConvertFloat4ToU32(badgeBg),
                              ui::metrics::cpMenuChipRound());
            ImVec4 badgeInk = badgeTab >= 0 ? surf.textSecondary : surf.textTertiary;
            badgeInk.w = 1.0f;
            dl->AddText(ImVec2(bx0 + 4.0f * sc, by0 + 1.0f * sc),
                        ImGui::ColorConvertFloat4ToU32(badgeInk), badge.c_str());
            ImGui::SetCursorScreenPos(ImVec2(bx0, by0));
            ImGui::InvisibleButton("##docbadge", ImVec2(bx1 - bx0, by1 - by0));
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", badgeTab >= 0
                    ? "Jump to this document's tab" : "Document not open");
            if (ImGui::IsItemClicked() && badgeTab >= 0)
                st.switchToTab(badgeTab);
        }

        bool doDel = false;
        bool doArchiveToggle = false;
        bool doFork = false;
        bool doManage = false;
        bool doRename = false;
        const float btnY = rowMin.y + (rowH - delBtnH) * 0.5f;
        const float moreX = rowMax.x - rightMargin - moreW;
        // Shortcuts are laid out right-to-left from the overflow button, each only when
        // takeShortcut() above found room for it (the same set the title clip reserved).
        float nextX = moreX;
        if (showDelBtn) {
            const float delX = nextX - actionGap - delBtnW;
            nextX = delX;
            ImGui::SetCursorScreenPos(ImVec2(delX, btnY));
            IconUV trash = ui::getIcon("delete", 20);
            bool del = trash.valid
                ? ImGui::ImageButton("##delconv", trash.texId, ImVec2(delImg, delImg), trash.uv0, trash.uv1)
                : ImGui::SmallButton("x");
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(running ? "Delete \xe2\x80\x94 available when this session's run finishes."
                                          : "Delete this session");
            // Destructive row actions are blocked only for the row whose own run is live —
            // an unrelated background run never locks them.
            doDel = del && !running;
        }
        if (showArchBtn) {
            const float archX = nextX - archBtnW - actionGap;
            nextX = archX;
            ImGui::SetCursorScreenPos(ImVec2(archX, btnY));
            bool archClick = ImGui::Button(archLabel, ImVec2(archBtnW, delBtnH));
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(c.archived
                    ? "Restore to the active sessions list"
                    : "Archive: hides from the default list but keeps the full transcript "
                      "(unlike Delete). Turn on \"Show archived\" above to find it again.");
            doArchiveToggle = archClick && !running;
        }
        if (showForkBtn) {
            const float forkX = nextX - forkBtnW - actionGap;
            nextX = forkX;
            ImGui::SetCursorScreenPos(ImVec2(forkX, btnY));
            doFork = ImGui::Button("Fork", ImVec2(forkBtnW, delBtnH));
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Fork this session into a new idle copy");
        }
        if (showManageBtn) {
            const float mgX = nextX - manageBtnW - actionGap;
            ImGui::SetCursorScreenPos(ImVec2(mgX, btnY));
            doManage = ImGui::Button("Manage", ImVec2(manageBtnW, delBtnH));
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(s_ccm.convId == c.id
                    ? "Hide session recovery"
                    : "Session recovery: export, diagnose/repair, branch, close runtime");
        }

        // Permanent overflow: the same actions (plus Rename) on every row, hover or not.
        // Right-clicking the row opens the same menu.
        ImGui::SetCursorScreenPos(ImVec2(moreX, btnY));
        if (ImGui::Button("\xe2\x80\xa6##sessrow_more", ImVec2(moreW, delBtnH)))
            ImGui::OpenPopup("##sessrow_menu");
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Session actions");
        ui::pushContextMenuStyle();
        if (ImGui::BeginPopup("##sessrow_menu")) {
            constexpr const char* kAfterThisRun =
                "Available when this session's run finishes.";
            if (ImGui::MenuItem("Rename")) doRename = true;
            if (ImGui::MenuItem("Fork")) doFork = true;
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Fork this session into a new idle copy");
            if (ImGui::MenuItem(archLabel, nullptr, false, !running)) doArchiveToggle = true;
            if (running && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                ImGui::SetTooltip("%s", kAfterThisRun);
            if (ImGui::MenuItem(s_ccm.convId == c.id ? "Hide recovery" : "Manage")) doManage = true;
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Session recovery: export, diagnose/repair, branch, close runtime");
            ImGui::Separator();
            if (ImGui::MenuItem("Delete", nullptr, false, !running)) doDel = true;
            if (running && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                ImGui::SetTooltip("%s", kAfterThisRun);
            ImGui::EndPopup();
        }
        ui::popContextMenuStyle();
        if (doRename) {
            s_rename = SessRename{};
            s_rename.id = c.id;
            std::snprintf(s_rename.buf, sizeof(s_rename.buf), "%s", c.title.c_str());
        }

        ImGui::SetCursorScreenPos(ImVec2(rowMin.x, rowMax.y + rowGap));
        ImGui::PopID();
        // Toggle + draw the recovery card directly under its own row (persists while the
        // pointer leaves the row — only the toggle button is hover-gated, not the card).
        if (doManage) {
            if (s_ccm.convId == c.id) s_ccm.close();
            else s_ccm.open(c.id);
        }
        if (s_ccm.convId == c.id) drawChudcodeManageCard(st, c, running);
        if (doDel) { closeConversation(st, i); break; }
        if (doFork) { forkConversation(st, i); break; }
        if (doArchiveToggle) { archiveConversation(st, i, !c.archived); break; }
    }
    if (!anyRowShown) {
        // Same tone and indent as every other "nothing here yet" line in the copilot menus.
        std::string where;
        if (scope.scope == SessScope::Project)
            where = peeking ? " in " + peekName : std::string(" in this project");
        const std::string hint =
            allProjects                           ? std::string(s_showArchived ? "No archived sessions."
                                                                               : "No sessions yet.")
            : scope.scope == SessScope::Unassigned ? std::string(s_showArchived
                                                        ? "No archived unassigned sessions."
                                                        : "No unassigned sessions.")
            : (s_showArchived ? "No archived sessions" : "No sessions") + where + ".";
        ui::cpMenuHint(hint.c_str());
    }

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
// The Artifacts tab is an asset BROWSER: every row says what KIND of thing it is, how big it is
// and when it landed, and carries persistent actions. All chrome comes from ui::kit, so an
// artifact row and a PLM row are the same object.

// One listed artifact. `open` is the row's activation; `image` marks an ART-stream image.
struct ArtiDocRow {
    std::string label, tooltip, path;
    std::function<void()> open;
    bool image = false;
};

enum ArtiBucket { ArtiModels = 0, ArtiDrawings, ArtiData, ArtiOther };

// What an artifact IS — resolved once from its extension, then reused for the leading icon, the
// secondary line and the section bucket. The old panel put the PARENT FOLDER on the second line,
// which for a session whose output all lands in one directory is the same string on every row.
struct ArtiClass {
    const char* icon   = "file_generic";
    const char* what   = "File";
    bool        image  = false;
    ArtiBucket  bucket = ArtiOther;
};

static std::string artiLower(std::string s) {
    for (char& c : s) c = (char)std::tolower((unsigned char)c);
    return s;
}

static ArtiClass artiClassify(const std::string& path, const std::string& label) {
    ArtiClass c;
    if (path.empty()) {
        c.icon = "part";
        c.what = "Live document";
        return c;
    }
    const size_t slash = path.find_last_of("/\\");
    const size_t dot   = path.find_last_of('.');
    std::string ext;
    if (dot != std::string::npos && (slash == std::string::npos || dot > slash))
        ext = artiLower(path.substr(dot + 1));

    static const struct {
        const char* ext; const char* icon; const char* what; bool image; ArtiBucket bucket;
    } kMap[] = {
        {"step+",  "file_step",    "CAD document",       false, ArtiModels},
        {"aicm",   "file_step",    "CAD model",          false, ArtiModels},
        {"draw+",  "file_doc",     "Drawing document",   false, ArtiDrawings},
        {"ecad+",  "file_data",    "Electronics document", false, ArtiModels},
        {"step",   "file_step",    "STEP export",        false, ArtiModels},
        {"stp",    "file_step",    "STEP export",        false, ArtiModels},
        {"iges",   "file_step",    "IGES export",        false, ArtiModels},
        {"igs",    "file_step",    "IGES export",        false, ArtiModels},
        {"sldprt", "file_step",    "CAD part",           false, ArtiModels},
        {"x_t",    "file_step",    "Parasolid export",   false, ArtiModels},
        {"stl",    "file_mesh",    "STL mesh",           false, ArtiModels},
        {"obj",    "file_mesh",    "OBJ mesh",           false, ArtiModels},
        {"ply",    "file_mesh",    "PLY mesh",           false, ArtiModels},
        {"3mf",    "file_mesh",    "3MF print file",     false, ArtiModels},
        {"png",    "file_image",   "Render",             true,  ArtiOther},
        {"jpg",    "file_image",   "Render",             true,  ArtiOther},
        {"jpeg",   "file_image",   "Render",             true,  ArtiOther},
        {"webp",   "file_image",   "Render",             true,  ArtiOther},
        {"pdf",    "file_doc",     "PDF document",       false, ArtiDrawings},
        {"dxf",    "file_doc",     "DXF drawing",        false, ArtiDrawings},
        {"svg",    "file_doc",     "SVG drawing",        false, ArtiDrawings},
        {"md",     "file_doc",     "Notes",              false, ArtiData},
        {"txt",    "file_doc",     "Text file",          false, ArtiData},
        {"docx",   "file_doc",     "Word document",      false, ArtiData},
        {"json",   "file_data",    "JSON data",          false, ArtiData},
        {"csv",    "file_data",    "CSV table",          false, ArtiData},
        {"xml",    "file_data",    "XML data",           false, ArtiData},
        {"yaml",   "file_data",    "YAML data",          false, ArtiData},
        {"yml",    "file_data",    "YAML data",          false, ArtiData},
        {"py",     "file_code",    "Python script",      false, ArtiData},
        {"cpp",    "file_code",    "C++ source",         false, ArtiData},
        {"js",     "file_code",    "JavaScript source",  false, ArtiData},
        {"ts",     "file_code",    "TypeScript source",  false, ArtiData},
        {"zip",    "file_archive", "Archive",            false, ArtiOther},
        {"tar",    "file_archive", "Archive",            false, ArtiOther},
        {"7z",     "file_archive", "Archive",            false, ArtiOther},
        {"gz",     "file_archive", "Archive",            false, ArtiOther},
    };
    for (const auto& m : kMap) {
        if (ext != m.ext) continue;
        c.icon = m.icon; c.what = m.what; c.image = m.image; c.bucket = m.bucket;
        break;
    }
    // A CSV the agent produced is almost always the BOM; say so rather than "CSV table".
    if (ext == "csv" && artiLower(label).find("bom") != std::string::npos)
        c.what = "Bill of materials";
    return c;
}

// Size + age for the row's meta column, from ONE cached stat.
struct ArtiStat {
    unsigned long long size  = 0;
    double             mtime = 0.0;   // unix seconds; 0 = unknown
    bool               ok    = false;
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
    if (now - s.taken >= kStatTtl) {
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
        s.taken = now;
    }
    s.lastUse = now;
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

// Render-thumbnail cache for the Artifacts tab's "Renders" grid. The panel is redrawn every
// frame, so each PNG the agent produced is decoded and uploaded ONCE and then keyed by path —
// the same lazy-upload shape the inline chat tool rows use (activity_timeline.cpp s_artTex),
// but BOUNDED: a long session emits hundreds of render_part files and an unbounded map would
// hold a GL texture per file forever. 32 resident entries, least-recently-used evicted and its
// texture released. A FAILED decode is cached as id == 0 so a missing/corrupt file is not
// re-read every frame; callers fall back to a dim text row for it.
static Tex artiRenderThumb(const std::string& path) {
    struct Slot { Tex tex; double lastUse = 0.0; };
    static std::unordered_map<std::string, Slot> s_cache;
    static constexpr size_t kMaxResident = 32;
    const double now = ImGui::GetTime();
    auto it = s_cache.find(path);
    if (it == s_cache.end()) {
        if (s_cache.size() >= kMaxResident) {
            auto oldest = s_cache.begin();
            for (auto i = s_cache.begin(); i != s_cache.end(); ++i)
                if (i->second.lastUse < oldest->second.lastUse) oldest = i;
            if (oldest->second.tex.id) {
                GLuint dead = (GLuint)oldest->second.tex.id;
                glDeleteTextures(1, &dead);
            }
            s_cache.erase(oldest);
        }
        it = s_cache.emplace(path, Slot{loadTexture(path), now}).first;
    }
    it->second.lastUse = now;
    return it->second.tex;   // by value: the next call may evict/rehash this slot
}

// Filter / disclosure / view-mode state for the Artifacts tab. These lived in file statics, so a
// filter typed in one session was still applied — invisibly, because the field only appears past
// eight rows — after switching to another. Keyed to the viewed conversation and reset with it.
struct ArtiUiState {
    std::string convId;
    char        filter[192]     = "";
    bool        showOtherDocs   = false;
    bool        showAllRenders  = false;
    int         viewMode        = 0;    // 0 = List, 1 = Grid
    double      enterT0         = 0.0;  // stagger origin for the row entrance
    int         lastFrame       = -2;
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

// One artifact row: kit chrome, a kind icon (or the render thumbnail), "2.4 MB . 3 min ago" in the
// meta column, and a PERSISTENT action lane. Hover-gated actions are unreachable by keyboard and
// touch and invisible in a screenshot, which is why they are always drawn now.
static void artiRow(AppState& st, const ArtiDocRow& r, int index, double t0, bool thumbsOn) {
    const ArtiClass cls = artiClassify(r.path, r.label);

    std::string meta;
    if (!r.path.empty()) {
        const ArtiStat s = artiStatOf(r.path);
        if (s.ok) {
            meta = ui::kit::fmtBytes(s.size);
            const std::string ago = ui::kit::fmtAgo(s.mtime);
            if (!ago.empty()) meta += " \xc2\xb7 " + ago;
        }
    }
    std::string tip = r.tooltip.empty() ? (r.path.empty() ? r.label : r.path) : r.tooltip;
    if (!meta.empty()) tip += "\n" + meta;

    ui::kit::RowStyle rs;
    rs.icon      = cls.icon;
    rs.iconTone  = ui::kit::Tone::Accent;
    rs.secondary = cls.what;              // never empty — the row height below relies on it
    rs.meta      = meta.empty() ? nullptr : meta.c_str();
    rs.tooltip   = tip.c_str();
    if (cls.image && thumbsOn) {
        const Tex t = artiRenderThumb(r.path);
        if (t.id) rs.thumb = (ImTextureID)(intptr_t)t.id;
    }

    const float btn    = ImGui::GetFrameHeight();
    const float btnGap = ui::metrics::U(0.5f);
    const int   nAct   = r.path.empty() ? 1 : 3;
    rs.actionsW = btn * (float)nAct + btnGap * (float)(nAct - 1);

    const float  lineH = ImGui::GetTextLineHeight();
    const float  rowH  = lineH * 2.0f + ui::metrics::U(0.5f) + ui::kit::padY() * 2.0f;
    const float  rowW  = ImGui::GetContentRegionAvail().x;
    const float  a     = artiEnterAlpha(t0, index);
    const float  off   = (1.0f - a) * ui::metrics::px(8.0f);
    ImDrawList*  dl    = ImGui::GetWindowDrawList();
    const int    vtx0  = dl->VtxBuffer.Size;

    const ImVec2 base = ImGui::GetCursorScreenPos();
    if (off > 0.0f) ImGui::SetCursorScreenPos(ImVec2(base.x, base.y - off));
    const ImVec2 p0 = ImGui::GetCursorScreenPos();

    ImGui::PushID(index);
    ImGui::SetNextItemAllowOverlap();   // the action lane is submitted ON TOP of the row's hit box
    const bool   activated = ui::kit::row("arti", r.label.c_str(), rs);
    const ImVec2 after     = ImGui::GetCursorScreenPos();

    // The action lane lives inside the row's hit box and is submitted after it. AllowOverlap hands
    // the click to the buttons, but exclude their strip from the row activation explicitly anyway:
    // on the first hot frame the buttons were not hovered last frame, and "Folder" would then both
    // reveal the file AND open the artifact.
    const float bx0 = p0.x + rowW - ui::kit::padX() - rs.actionsW;
    const bool  overLane = ImGui::IsMouseHoveringRect(ImVec2(bx0, p0.y),
                                                      ImVec2(bx0 + rs.actionsW, p0.y + rowH),
                                                      /*clip=*/false);
    if (activated && !overLane && r.open) r.open();

    float bx = bx0;
    const float by = p0.y + (rowH - btn) * 0.5f;
    ImGui::SetCursorScreenPos(ImVec2(bx, by));
    if (ui::kit::iconButton("open", "open",
                            r.path.empty() ? "Reopen the live view of this part"
                                           : "Open this artifact",
                            true, ui::kit::Tone::Neutral, btn) && r.open)
        r.open();
    if (!r.path.empty()) {
        bx += btn + btnGap;
        ImGui::SetCursorScreenPos(ImVec2(bx, by));
        if (ui::kit::iconButton("copy", "copy", "Copy the full path to the clipboard", true,
                                ui::kit::Tone::Neutral, btn))
            ImGui::SetClipboardText(r.path.c_str());
        bx += btn + btnGap;
        ImGui::SetCursorScreenPos(ImVec2(bx, by));
        // body_folder is the atlas's plain folder mark; there is no separate "reveal" glyph.
        if (ui::kit::iconButton("reveal", "body_folder", "Show in the file manager", true,
                                ui::kit::Tone::Neutral, btn))
            chatArtifactReveal(st, r.path);
    }
    ImGui::PopID();

    if (a < 1.0f) artiFadeVerts(dl, vtx0, a);
    ImGui::SetCursorScreenPos(ImVec2(after.x, after.y + off));
}

// Square image tiles. The Copilot dock is narrow, so the grid reflows down to two columns and
// gives up entirely below the minimum content width rather than clipping tiles to slivers.
// `enterBase` continues the panel-wide entrance wave instead of restarting it at zero.
static constexpr float kArtiGridHoverLift = 0.03f;   // 1.00 -> 1.03 on hover
static bool artiDrawGrid(AppState& st, const std::vector<const ArtiDocRow*>& rows, double t0,
                         int enterBase, std::vector<const ArtiDocRow*>& fallbackOut) {
    const float availW = ImGui::GetContentRegionAvail().x;
    if (availW < ui::metrics::px(180.0f)) return false;   // caller falls back to the list

    const float gap  = ui::kit::rowGap();
    int         cols = (int)((availW + gap) / (ui::metrics::px(120.0f) + gap));
    cols = ImClamp(cols, 2, 4);
    const float cell = (availW - gap * (float)(cols - 1)) / (float)cols;
    const float rnd  = ui::metrics::cpMenuRowRound();
    const float rndDp = rnd / ui::metrics::scale();

    const ImVec2 origin = ImGui::GetCursorScreenPos();
    ImDrawList*  dl     = ImGui::GetWindowDrawList();
    int          shown  = 0;

    ImGui::PushID("arti_grid");
    for (size_t i = 0; i < rows.size(); ++i) {
        const ArtiDocRow& r = *rows[i];
        const Tex tex = artiRenderThumb(r.path);
        if (!tex.id || tex.w <= 0 || tex.h <= 0) {
            fallbackOut.push_back(&r);   // missing / corrupt / half-written stays visible as a row
            continue;
        }
        const float  a   = artiEnterAlpha(t0, enterBase + (int)i);
        const float  off = (1.0f - a) * ui::metrics::px(8.0f);
        const ImVec2 p0(origin.x + (float)(shown % cols) * (cell + gap),
                        origin.y + (float)(shown / cols) * (cell + gap) - off);
        const int vtx0 = dl->VtxBuffer.Size;

        ImGui::PushID((int)i);
        ImGui::SetCursorScreenPos(p0);
        ImGui::InvisibleButton("##tile", ImVec2(cell, cell));
        const bool hot = ImGui::IsItemHovered();
        if (ImGui::IsItemClicked()) chatArtifactOpenSmart(st, r.path);

        const float k = 1.0f + kArtiGridHoverLift *
                                   ui::anim::toward(ImGui::GetID("##lift"), hot ? 1.0f : 0.0f,
                                                    ui::anim::kFast);
        const ImVec2 c(p0.x + cell * 0.5f, p0.y + cell * 0.5f);
        const ImVec2 q0(c.x - cell * 0.5f * k, c.y - cell * 0.5f * k);
        const ImVec2 q1(c.x + cell * 0.5f * k, c.y + cell * 0.5f * k);
        if (k > 1.0f) ui::glassSelectionGlow(dl, q0, q1, rndDp, ui::palette.accent);
        dl->AddImageRounded((ImTextureID)(intptr_t)tex.id, q0, q1, ImVec2(0, 0), ImVec2(1, 1),
                            IM_COL32_WHITE, rnd);
        if (hot) {
            ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
            ImGui::SetTooltip("%s\n%s", r.label.c_str(), r.path.c_str());
        }
        ImGui::PopID();

        if (a < 1.0f) artiFadeVerts(dl, vtx0, a);
        ++shown;
    }
    ImGui::PopID();

    const int gridRows = (shown + cols - 1) / cols;
    ImGui::SetCursorScreenPos(origin);
    if (gridRows > 0)
        ImGui::Dummy(ImVec2(availW, (float)gridRows * cell + (float)(gridRows - 1) * gap));
    return true;
}

// Session Artifacts is a browser for session output: agent-touched documents, render images and
// produced files. Run telemetry stays in the Tasks panel.
static void drawArtifactsPanel(AppState& st, bool busy) {
    (void)busy;
    ArtiUiState& ui_ = artiUi(st);
    ui::cpMenuHeader(st.fonts, "Session Artifacts",
                     "Documents, renders and files produced or touched by this session.");

    // The question this panel answers is "what did the AGENT touch", not "what is open" — the old
    // single list showed every doc tab, so a session that built one part looked like it built
    // four. A tab counts as agent-touched when it was handed off by an agent OR carries an agent
    // edit stamp; everything else is the user's own document and goes behind a collapsed line.
    std::vector<ArtiDocRow> agentRows;   // documents this session's agent opened / built / edited
    std::vector<ArtiDocRow> otherRows;   // the user's own open documents (collapsed by default)
    std::vector<ArtiDocRow> fileRows;    // files the agent wrote this session (renders included)
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
            // displayTitle() is the one label that refuses to hand back an opaque
            // "doc-cpp…" id (doc_tabs.cpp:95); the old t.title fallback chain ended on
            // t.docId, so a part the agent opened listed itself as its engine id.
            r.label = t.displayTitle();
            // Unsaved in-memory docs have no file to reveal — fall back to their save
            // destination (the project/workspace folder) so the location action always exists.
            r.path  = !t.path.empty() ? t.path : st.plmProjectPath();
            r.tooltip = r.path.empty() ? r.label : (r.label + "\n" + r.path);
            r.open  = [&st, ti]() { st.switchToTab(ti); };
            const bool agentTouched =
                t.aiHandoff || t.lastAgentEditAt != std::chrono::steady_clock::time_point{};
            (agentTouched ? agentRows : otherRows).push_back(std::move(r));
        }
        // Docs the user closed from the viewport while an agent kept building them.
        // hostd degraded that close to a detach (the work is independent of the
        // user's tabs), so the part is still live — list it with a Reopen action
        // instead of letting it vanish from the session's artifacts entirely.
        // These are agent documents by construction, so they belong in the agent bucket.
        for (const auto& [docId, label] : st.dismissedMirrorDocs) {
            if (docId.empty() || !seenId.insert(docId).second) continue;
            ArtiDocRow r;
            r.label = (label.empty() ? docId : label) + "  (closed \xe2\x80\x94 agent may still be building)";
            r.path.clear();     // no file yet; the only action is to reopen the view
            r.tooltip = "Reopen the live view of this part";
            r.open = [&st, docId]() {
                st.dismissedMirrorDocs.erase(docId);
                core::mirrorRecoveredDocs(st, {docId});
            };
            agentRows.push_back(std::move(r));
        }
        // Cross-source dedup: seed `seen` with the doc tabs' real file paths so a file
        // already listed as an open document is not repeated as a produced-file row.
        std::set<std::string> seen;
        for (const auto& t : st.docTabs)
            if (!t.path.empty()) seen.insert(t.path);
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
                    if (line.empty()) continue;
                    size_t tab = line.find('\t');
                    std::string kindA = tab == std::string::npos ? "file" : line.substr(0, tab);
                    std::string path  = tab == std::string::npos ? line : line.substr(tab + 1);
                    if (path.empty() || !seen.insert(path).second) continue;
                    std::string base = path;
                    size_t slash = base.find_last_of("/\\");
                    if (slash != std::string::npos) base = base.substr(slash + 1);
                    ArtiDocRow r;
                    r.label   = base;
                    r.path    = path;
                    r.tooltip = path;
                    r.image   = (kindA == "image");
                    r.open    = [&st, path]() { chatArtifactOpenSmart(st, path); };
                    fileRows.push_back(std::move(r));
                }
            }
        }
    }

    // Panel-level actions stay above browsing/filter chrome. Project-folder reveal accepts a
    // directory directly; render visibility uses the existing cached chat display preference.
    const std::string projectPath = st.plmProjectPath();
    bool rendersOn = ui::chatShowRenders();
    if (ui::kit::secondaryButton("Open project folder", !projectPath.empty(),
                                 "Open or choose a project before revealing its folder.",
                                 ImVec2(-FLT_MIN, 0)))
        chatArtifactReveal(st, projectPath);

    if (ui::kit::toggleChip("Renders", &rendersOn, ui::kit::Tone::Accent,
                            "Show or hide image previews here and in the chat transcript."))
        ui::setChatShowRenders(rendersOn);
    {
        // List / Grid sits on the same control line, right-aligned.
        static const char* kViewLabels[]   = {"List", "Grid"};
        static const char* kViewTooltips[] = {"One row per artifact, with size and age",
                                              "Renders as square preview tiles"};
        const float segW = ui::metrics::px(104.0f);
        ImGui::SameLine();
        const float slack = ImGui::GetContentRegionAvail().x - segW;
        if (slack > 0.0f) ImGui::SetCursorPosX(ImGui::GetCursorPosX() + slack);
        ui::kit::segmented("arti_view", kViewLabels, 2, &ui_.viewMode, segW, kViewTooltips);
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));

    const size_t totalRows = agentRows.size() + otherRows.size() + fileRows.size();
    const bool showFilter = totalRows > 8;
    if (showFilter) {
        ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::cpMenuChipRound());
        ImGui::InputTextWithHint("##artifact_filter", "Filter artifacts\xe2\x80\xa6",
                                 ui_.filter, sizeof(ui_.filter));
        ImGui::PopStyleVar();
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
    }
    const bool filterActive = showFilter && ui_.filter[0] != '\0';
    const std::string filterNeedle = filterActive ? artiLower(ui_.filter) : std::string();
    auto matches = [&](const ArtiDocRow& r) {
        if (!filterActive) return true;
        return artiLower(r.label).find(filterNeedle) != std::string::npos ||
               artiLower(r.path).find(filterNeedle) != std::string::npos;
    };

    std::vector<const ArtiDocRow*> documents;
    std::vector<const ArtiDocRow*> renders;
    std::vector<const ArtiDocRow*> otherDocs;
    for (const auto& r : agentRows) if (matches(r)) documents.push_back(&r);
    for (const auto& r : otherRows) if (matches(r)) otherDocs.push_back(&r);
    // Chat activity is chronological. Collect render and file rows back-to-front once so every
    // rendering loop can index from zero while still presenting newest output first.
    for (auto it = fileRows.rbegin(); it != fileRows.rend(); ++it)
        if (it->image && matches(*it)) renders.push_back(&*it);

    // One classifier drives the leading icon, the "what it IS" line AND this bucketing, so a row's
    // section can never disagree with the kind its icon claims.
    std::vector<const ArtiDocRow*> buckets[4];
    for (auto it = fileRows.rbegin(); it != fileRows.rend(); ++it) {
        if (it->image || !matches(*it)) continue;
        buckets[artiClassify(it->path, it->label).bucket].push_back(&*it);
    }

    const size_t fileCount = buckets[ArtiModels].size() + buckets[ArtiDrawings].size() +
                             buckets[ArtiData].size() + buckets[ArtiOther].size();
    const size_t matchCount = documents.size() + renders.size() + fileCount + otherDocs.size();
    if (filterActive && matchCount == 0)
        ui::kit::emptyState("artifacts", "No matches",
                            "Try a document name, a file name or a folder.");

    // One entrance wave across the whole panel: the running index makes section 2 continue
    // section 1's stagger instead of restarting it.
    int enterIdx = 0;

    // ---- Documents ----
    ui::kit::sectionHeader("Documents", "part", nullptr, (int)documents.size(), /*first=*/true);
    if (documents.empty()) {
        ui::kit::emptyState(nullptr, filterActive ? "No matching documents" : "No documents yet",
                            "Parts the agent opens, imports or edits appear here.");
    } else {
        ImGui::PushID("arti_docs");
        for (const ArtiDocRow* r : documents)
            artiRow(st, *r, enterIdx++, ui_.enterT0, rendersOn);
        ImGui::PopID();
    }

    // ---- Renders ----
    ui::kit::sectionHeader("Renders", "file_image", nullptr, (int)renders.size());
    if (renders.empty()) {
        ui::kit::emptyState(nullptr, filterActive ? "No matching renders" : "No renders yet",
                            "Images produced for agent vision appear here as preview tiles.");
    } else if (!rendersOn) {
        ui::kit::emptyState(nullptr, "Render previews are hidden",
                            "Use the Renders control above to show them.");
    } else {
        // The overflow used to be a dead-end "+N more" line. It is a real control now.
        const size_t kCap = 24;
        const size_t drawCount = ui_.showAllRenders ? renders.size()
                                                    : std::min(renders.size(), kCap);
        std::vector<const ArtiDocRow*> shown(renders.begin(), renders.begin() + (long)drawCount);
        std::vector<const ArtiDocRow*> fallbackRows;

        ImGui::PushID("arti_renders");
        const bool gridded =
            ui_.viewMode == 1 && artiDrawGrid(st, shown, ui_.enterT0, enterIdx, fallbackRows);
        if (gridded) enterIdx += (int)shown.size();
        if (!gridded) {
            for (const ArtiDocRow* r : shown)
                artiRow(st, *r, enterIdx++, ui_.enterT0, rendersOn);
        }
        // Missing, corrupt or half-written images stay visible as ordinary rows.
        for (const ArtiDocRow* r : fallbackRows)
            artiRow(st, *r, enterIdx++, ui_.enterT0, /*thumbsOn=*/false);
        ImGui::PopID();

        const size_t more = renders.size() - drawCount;
        if (more > 0) {
            char mline[64];
            std::snprintf(mline, sizeof(mline), "Show all %d renders##arti_showall",
                          (int)renders.size());
            if (ui::kit::secondaryButton(mline)) ui_.showAllRenders = true;
        }
    }

    // ---- Files ----
    ui::kit::sectionHeader("Files", "file_generic", nullptr, (int)fileCount);
    if (fileCount == 0) {
        ui::kit::emptyState(nullptr, filterActive ? "No matching files" : "No files yet",
                            "Exported models, drawings, data and other files appear here.");
    } else {
        auto drawFileGroup = [&](const char* label, const char* id, ArtiBucket b) {
            if (buckets[b].empty()) return;
            ui::cpMenuLabel(label);
            ImGui::PushID(id);
            for (const ArtiDocRow* r : buckets[b])
                artiRow(st, *r, enterIdx++, ui_.enterT0, rendersOn);
            ImGui::PopID();
        };
        ImGui::PushID("arti_files");
        drawFileGroup("Models", "models", ArtiModels);
        drawFileGroup("Drawings / exports", "drawings", ArtiDrawings);
        drawFileGroup("Data", "data", ArtiData);
        drawFileGroup("Other", "other", ArtiOther);
        ImGui::PopID();
    }

    // ---- Other open documents ----
    // The user's own tabs must not dominate this panel, but dropping them outright would lose the
    // only way to reach a part the agent worked on BEFORE the handoff / edit-stamp flags were set
    // — so they stay, collapsed behind a click-to-expand line.
    ui::kit::sectionHeader("Other open documents", "open", nullptr, (int)otherDocs.size());
    if (otherDocs.empty()) {
        ui::kit::emptyState(nullptr,
                            filterActive ? "No matching open documents" : "Nothing else open",
                            "User-opened tabs appear here, collapsed away from session output.");
    } else {
        // The ## suffix keeps ONE ImGui id across the Show/Hide label flip.
        char line[96];
        std::snprintf(line, sizeof(line), "%s %d other open document%s##arti_otherdocs",
                      ui_.showOtherDocs ? "Hide" : "Show", (int)otherDocs.size(),
                      otherDocs.size() == 1 ? "" : "s");
        if (ui::kit::secondaryButton(line)) ui_.showOtherDocs = !ui_.showOtherDocs;
        if (ui_.showOtherDocs) {
            ImGui::PushID("arti_other_docs");
            for (const ArtiDocRow* r : otherDocs)
                artiRow(st, *r, enterIdx++, ui_.enterT0, rendersOn);
            ImGui::PopID();
        }
    }
    ImGui::Dummy(ImVec2(0, ui::kit::rowGap()));
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

    static float s_paneW = 0.0f;                    // column width (px), session-persistent
    // s_cpCollapsed is file-scoped (shared with drawCopilotHeader's collapse chevron). Seed it
    // once from the persisted setting (T-G21: a collapsed Copilot column stays collapsed across
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
            s_cpCollapsed = false;
            st.settings.copilotCollapsed = false;
            saveSettings(st.settings);
        }
        if (s_paneW <= 0.0f) s_paneW = ui::metrics::dockRightTarget();
        if (std::getenv("CHUDWARE_COPILOT_NARROW"))       // T-G17 shot: capture the narrowest column
            s_paneW = ui::metrics::dockRightMin();
        if (st.shot)
            if (const char* width = std::getenv("CHUDWARE_COPILOT_WIDTH"))
                s_paneW = ui::metrics::px(std::atof(width));
        // T-G21 shot: mirrors CHUDWARE_FM_COLLAPSED — --ui-screenshot always resets the overlay
        // columns for determinism (see the reset branch above), which clobbers the persisted
        // collapse setting before it can render; force it back for the collapsed capture.
        if (std::getenv("CHUDWARE_COPILOT_COLLAPSED")) {
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

        // T-G21: vertical collapse — the column rolls UP to its header row (drawCopilotHeader)
        // instead of shrinking to a pill in both axes (mirrors the FeatureManager column; see
        // ui::PanelCollapseAnim). Width is unaffected by collapse.
        const float animatedH = s_cpAnim.tick(s_cpCollapsed, colH);
        const float winH = mobile ? std::min(animatedH, colH) : animatedH;
        ImGui::SetNextWindowPos(ImVec2(band.maxX - marginX - paneW, colY), ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2(paneW, winH), ImGuiCond_Always);
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

    // Collapse chip floats just left of the column (see drawCopilotHeader) so header
    // content can start flush without a chevron reserve.

    // Left-edge resize boundary — the same visible hairline + grip + hover glow as the
    // FeatureManager column (ui::columnResizeEdge, layout.h). Dragging left widens the column;
    // s_paneW is clamped at the top of the next frame; double-click restores the default width.
    // Submitted before the header so the collapse chip (later, overlapping) keeps its clicks.
    if (overlay) {
        const ui::ColumnResizeResult rz = ui::columnResizeEdge(
            "##copilot_resize", /*rightEdge=*/false, ImGui::GetWindowWidth(), !mobile);
        if (rz.resetRequested) s_paneW = ui::metrics::dockRightTarget();
        else if (rz.active)    s_paneW = rz.width;
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

    // T-G17 screenshot hook: CHUDWARE_COPILOT_VIEW forces a Copilot sub-view (and tops up a couple of
    // demo sessions) so the sessions/settings layouts can be captured headless in both themes. Runs
    // AFTER syncChatToDoc (which loads/ensures the resident sessions) and re-tops-up each frame so
    // the demo rows survive to the capture frame. Screenshot-only — gated on the env var, no effect
    // in a normal run.
    if (const char* cpView = std::getenv("CHUDWARE_COPILOT_VIEW")) {
        ensureConversations(st);
        st.conversations[0].title = "Knurled hex standoff";
        while (st.conversations.size() < 3) {
            Conversation cx; cx.id = newConvId(st);
            cx.title = (st.conversations.size() == 1) ? "Bracket + FEA study"
                                                      : "Gearbox housing revision";
            cx.projectDir = convProjectDir(st);  // demo rows must pass the project filter
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

    // T-G21: the title row + separator above IS the header strip a vertical collapse rolls up to
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
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        // Horizontal gutter is ASYMMETRIC: the pill scrollbar is clipped to this child, so the
        // right side must reserve a full kChatScrollbarLane for it or bubbles run under the bar.
        // ImGui's WindowPadding is symmetric, so push the WIDE (right) value and pull the LEFT
        // edge back with a negative Indent below. Chat keeps its tight left gutter (bubbles/cards
        // need the column width); the MENU views keep the shared cpMenuGutter so
        // Sessions/Tasks/Artifacts/Settings all start on one left edge.
        const float padL = overlayOpen ? ui::metrics::cpMenuGutter() : 3.0f * ui::uiScale;
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
        ImGui::BeginChild(viewId, ImVec2(0, chatH), ImGuiChildFlags_AlwaysUseWindowPadding,
                          ImGuiWindowFlags_NoScrollbar);
        if (padR > padL) ImGui::Indent(padL - padR);   // negative: pull the LEFT edge back to padL
        if (mobile && overlayOpen) drawRuntimeBanner(st);
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
