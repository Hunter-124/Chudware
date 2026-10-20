// AI workspace: main conversation plus a project-scoped utility column.
// Current Run owns the execution trace, sub-agent roster, and single docked inspector;
// Sub-agents (view 6) is the project-wide sub-agent hub; Plan, Activity, Sessions, Insights and
// Options occupy the other nav compartments.
#include "ai_workspace.h"

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <string>
#include <unordered_map>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect (glassPanel/adaptivePanel rects)

#include "core/app_state.h"
#include "ui/copilot/copilot.h"
#include "ui/copilot/copilot_internal.h"
#include "ui_theme.h"
#include "ui/metrics.h"            // cpMenu* rhythm tokens (gutter / row pad / gap / corner)
#include "ui/ai/agent_dag.h"
#include "ui/ai/current_run_view.h"
#include "ui/widgets/kit.h"        // ui::kit — the app-wide section header / empty state
#include "ui/ai/active_work.h"     // activeWorkPump (per-frame thumb queue)
#include "ui/ai/plan_board.h"
#include "ui/ai/ai_prefs.h"
#include "ui/settings_modal.h"     // drawHarnessSessionKnobs (shared Options-tab run-behavior grid)
#include "ui/ai/activity_view.h"   // composed Activity view (Active Work + Documents)
#include "ui/ai/insights_view.h"   // composed Insights view (Leaderboard + Metrics)

namespace ui {

// ---- local state ----------------------------------------------------------------
namespace {

struct AiWsState {
    int view = -1;  // -1 = not yet restored from ai_prefs (agent.ui_ai_view) on first draw.
    int compactPane = 0;  // Ephemeral: 0 = Chat, 1 = Workspace; never stored in ai_prefs.
    // Follow live runs: the last run id seen per session. A session is seeded silently on first
    // sight, so switching sessions (whose saved trace carries an old run id) never moves the
    // pane — only a NEW run in the same session does.
    std::unordered_map<std::string, std::string> seenRunByConv;
};

static AiWsState s_ai;

static void setView(int v) {
    if (s_ai.view == v) return;
    s_ai.view = v;
    setAiWorkspaceView(v);
}


// coloredRowWash / glassRowBackground / glassPanel take a LOGICAL dp radius (they multiply by
// uiScale themselves), so hand them the shared cpMenu corner converted back to dp — one source
// of truth for the corner without double-scaling it.
inline float rowRoundDp() { return ui::metrics::cpMenuRowRound() / ui::uiScale; }

// ---- shared little helpers -> ui::kit --------------------------------------------
// Two of the app's duplicated painters lived here. They survive as file-local wrappers because
// this workspace's views spell them this way and they carry its two decisions: sections spaced by
// the caller, and an empty state that sits a third of the way down a full-height column.

// This workspace draws on the cpMenu* rhythm, and Density::Cozy IS that token family.
struct CozyDensity {
    CozyDensity()  { ui::kit::pushDensity(ui::kit::Density::Cozy); }
    ~CozyDensity() { ui::kit::popDensity(); }
    CozyDensity(const CozyDensity&)            = delete;
    CozyDensity& operator=(const CozyDensity&) = delete;
};

static void sectionHeader(const char* label) {
    const CozyDensity density;
    // `first`: the views space their own sections (an explicit cpMenuSectionGap between them), so
    // the shared painter must not add a second leading gap.
    ui::kit::sectionHeader(label, /*icon=*/nullptr, /*trailing=*/nullptr, /*count=*/-1,
                           /*first=*/true);
}

static void emptyState(const char* icon, const char* msg, const char* sub = nullptr) {
    const CozyDensity density;
    // These views are full-height columns; a state at the top of one reads as a stray label
    // rather than the pane's answer. The vertical push is this workspace's own decision.
    ImGui::Dummy(ImVec2(0, ImGui::GetContentRegionAvail().y * 0.35f));
    ui::kit::emptyState(icon, msg, sub);
}

// ---- right-panel vertical nav ----------------------------------------------------
static bool navRow(int id, const char* label, bool active, int badge = 0) {
    ImGui::PushID(id);
    kit::RowStyle style;
    style.selected = active;
    const std::string count = badge > 0 ? kit::fmtCount(badge) : std::string();
    style.meta = count.empty() ? nullptr : count.c_str();
    const bool hit = kit::row("##navrow", label, style);
    ImGui::PopID();
    return hit;
}

static void drawProjectChip(AppState& st) {
    const std::string projectDir = convProjectDir(st);
    const std::filesystem::path project(projectDir);
    std::string projectName = project.filename().string();
    if (projectName.empty() && !projectDir.empty())
        projectName = project.parent_path().filename().string();
    const std::string shortName = kit::elide(
        projectName.empty() ? "No project selected" : projectName.c_str(),
        ImGui::GetContentRegionAvail().x - kit::padX() * 2.0f);
    kit::chip(shortName.c_str(),
              projectName.empty() ? kit::Tone::Neutral : kit::Tone::Accent,
              false, projectDir.empty() ? nullptr : projectDir.c_str());
}

static void drawNavColumn(AppState& st, float navW) {
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuGutter(), ui::metrics::px(8.0f)));
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##ai_ws_nav", ImVec2(navW, 0),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();

    // Liquid-glass slab behind the nav (same recipe as the chat column).
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 w0 = ImGui::GetWindowPos();
    const ImRect wRect(w0, ImVec2(w0.x + ImGui::GetWindowSize().x,
                                  w0.y + ImGui::GetWindowSize().y));
    const AdaptiveSurface& surf =
        adaptivePanel(panelId("ai_ws_nav_glass"), wRect, GlassGroup::Global);
    glassPanel(dl, wRect, GlassLevel::Thin, rowRoundDp(), surf);

    kit::sectionHeader("Active project", nullptr, nullptr, -1, true);
    drawProjectChip(st);
    kit::gapSection();

    const int live = currentRunLiveCount();

    if (navRow(0, "Current Run", s_ai.view == 0, live)) setView(0);
    if (navRow(6, "Sub-agents",  s_ai.view == 6, subagentHubLiveCount())) setView(6);
    if (navRow(1, "Plan",        s_ai.view == 1))       setView(1);
    if (navRow(2, "Activity",    s_ai.view == 2))       setView(2);
    if (navRow(3, "Sessions",    s_ai.view == 3))       setView(3);
    if (navRow(4, "Insights",    s_ai.view == 4))       setView(4);

    // Hairline + the utility section.
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));
    {
        const ImVec2 c = ImGui::GetCursorScreenPos();
        dl->AddLine(ImVec2(c.x + ui::metrics::cpMenuRowGap(), c.y),
                    ImVec2(c.x + navW - ui::metrics::cpMenuGutter() * 2.0f, c.y),
                    ui::Palette::toU32(palette.glassBorder), ui::metrics::stroke(1.0f));
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuHeadGap()));

    if (navRow(5, "Options", s_ai.view == 5)) setView(5);

    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();
}

// ---- center: expanded chat --------------------------------------------------------

// Session header: which session this chat belongs to + the Sessions panel's New session action
// (newConversation — safe mid-run, runs are per session and keep streaming when parked), then
// the shared runtime banner the docked Copilot shows under its own header.
static void drawSessionHeader(AppState& st) {
    const Conversation* conv =
        (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size())
            ? &st.conversations[(size_t)st.activeConv] : nullptr;
    const char* newLabel = "+ New session";
    const float x0     = ImGui::GetCursorPosX();
    const float availW = ImGui::GetContentRegionAvail().x;
    const float btnW   = ImGui::CalcTextSize(newLabel).x + ImGui::GetStyle().FramePadding.x * 2.0f;
    const std::string title = kit::elide(
        conv && !conv->title.empty() ? conv->title.c_str() : "New session",
        availW - btnW - ui::metrics::cpMenuLabelGap());
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(title.c_str());
    ImGui::SameLine(x0 + availW - btnW);
    if (kit::secondaryButton(newLabel)) {
        newConversation(st);
        st.prompt[0] = 0;
    }
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Start a fresh session.");
    drawRuntimeBanner(st);
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
}

static void drawChatCenter(AppState& st, bool busy, bool canRun) {
    // Both layouts use the same session, transcript and draft state.
    drawSessionHeader(st);

    // Reserve the composer height measured last frame so the log fills the rest
    // (same one-frame-lag trick as the docked Copilot).
    static float s_composerH = 0.0f;
    const float composerGap = ui::metrics::cpMenuRowGap();
    const float reserve = (s_composerH > 0.0f ? s_composerH
                                            : ImGui::GetFrameHeightWithSpacing() * 4.0f) +
                          composerGap + ImGui::GetStyle().ItemSpacing.y * 2.0f;
    const float logH = std::max(ui::metrics::stroke(1.0f),
        ImGui::GetContentRegionAvail().y - reserve - chatJumpToLatestFooterHeight());

    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuGutter(), ui::metrics::px(8.0f)));
    ImGui::BeginChild("##ai_ws_chatlog", ImVec2(0, logH),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleVar();
    // The chat column's slab is drawn by the host child; resolve the same surface id here so
    // the pill scrollbar picks up matching tones.
    const ImVec2 lw0 = ImGui::GetWindowPos();
    const AdaptiveSurface& logSurf = adaptivePanel(
        panelId("ai_ws_conv_glass"),
        ImRect(lw0, ImVec2(lw0.x + ImGui::GetWindowSize().x,
                           lw0.y + ImGui::GetWindowSize().y)),
        GlassGroup::Global);
    if (st.chat.empty()) {
        chatDrawEmptyState();
    } else {
        chatDrawLog(st);
        // Shared stick-to-bottom (releases on a user scroll-up). The region id scopes the flag
        // to this host so the docked Copilot column and the workspace never fight over one.
        chatLogStickToBottom("aiws.chatlog");
    }
    drawWindowPillScrollbarV(logSurf);
    ImGui::EndChild();
    ImGui::PopStyleColor();
    // A stable parent-layout gutter, outside the transcript and its pill-scrollbar lane.
    chatDrawJumpToLatest("aiws.chatlog");

    ImGui::Dummy(ImVec2(0, composerGap));
    ImGui::BeginGroup();
    chatDrawComposer(st, busy, canRun);   // the docked Copilot's real composer + send path
    ImGui::EndGroup();
    s_composerH = ImGui::GetItemRectSize().y;
}

// ---- Options view (v3): run-behavior + workspace-visibility knobs -------------------
// The model / API key / privacy controls are reachable ONLY on the main Settings page
// (Settings -> AI). This view keeps the run-behavior knobs (same config.json fields, no
// parallel store, shared verbatim with Settings -> AI -> Harness via
// drawHarnessSessionKnobs) + the shared generation controls + the command-center visibility
// prefs (agent.ui_*). 2-col stretch-same tables keep every label/control pair aligned
// regardless of label length (a fixed SameLine(labelW) crowded longer labels against their
// control) and a left indent keeps content off the panel's glass edge.
static void drawOptionsView(AppState& st, bool busy) {
    const float S = ui::uiScale;
    const ImGuiTableFlags gridFlags =
        ImGuiTableFlags_SizingStretchSame | ImGuiTableFlags_NoPadOuterX;

    sectionHeader("Workspace");
    if (ImGui::BeginTable("##ai_ws_workspace", 2, gridFlags)) {
        ImGui::TableNextColumn();
        {
            int v = aiUiVerbosity();
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted(aiPrefCopy::kDetailLabel);
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::Combo("##ai_ws_verb", &v, aiPrefCopy::kDetailLevels, 3))
                setAiUiVerbosity(v);
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", aiPrefCopy::kDetailHelp);
        }
        ImGui::TableNextColumn();
        {
            bool follow = aiFollowActiveSession();
            if (ImGui::Checkbox(aiPrefCopy::kFollowLabel, &follow))
                setAiFollowActiveSession(follow);
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", aiPrefCopy::kFollowHelp);
        }
        ImGui::TableNextColumn();
        {
            bool prev = aiActiveWorkPreviews();
            if (ImGui::Checkbox(aiPrefCopy::kPreviewsLabel, &prev)) setAiActiveWorkPreviews(prev);
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", aiPrefCopy::kPreviewsHelp);
        }
        ImGui::TableNextColumn();
        ImGui::Dummy(ImVec2(1, ImGui::GetFrameHeight()));
        ImGui::EndTable();
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));

    // Shared with Settings -> AI -> Harness: the exact same widgets/fields, one source of
    // truth. Covers plan/subagent/advisor preference, role adoption, proactive advisor,
    // company auto-route, vision, offline mode, show-thoughts, project memory, redaction,
    // refine-pass cap, max subagent nesting, memory budget, Eng. Firm budget.
    sectionHeader("Workflow");
    if (ui::drawHarnessSessionKnobs(st, S)) saveSettings(st.settings);
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));

    sectionHeader("Generation");
    if (ui::drawAiSettingsSection(st, busy, ui::AiSettingsSection::Generation))
        saveSettings(st.settings);
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));

    sectionHeader("More");
    if (ImGui::Button("Open full AI settings\xE2\x80\xA6")) {
        st.openSettings    = true;
        st.openSettingsTab = 2;   // Settings modal tab order: Appearance 0, General 1, AI 2
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1],
               palette.textDisabled[2], 1.0f));
    ImGui::TextWrapped("These settings are shared with the docked Copilot. Provider, model, "
                       "API key and privacy are in Settings > AI.");
    ImGui::PopStyleColor();
}

}  // anonymous namespace

// ---- public entry point ------------------------------------------------------------

// The nav-selected compartment beside the persistent main conversation.
static void drawViewPane(AppState& st, bool busy) {
    // (no local uiScale: every dimension below comes from ui::metrics)
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuGutter(), ui::metrics::px(8.0f)));
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##ai_ws_view", ImVec2(0, 0),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();

    // Liquid-glass slab behind the whole pane (shared adaptive surface — the views fetch the
    // same panelId for their text tones, and the pill scrollbar its track/thumb tones).
    const ImVec2 vw0 = ImGui::GetWindowPos();
    const ImRect vRect(vw0, ImVec2(vw0.x + ImGui::GetWindowSize().x,
                                   vw0.y + ImGui::GetWindowSize().y));
    const AdaptiveSurface& viewSurf =
        adaptivePanel(panelId("ai_ws_view_glass"), vRect, GlassGroup::Global);
    glassPanel(ImGui::GetWindowDrawList(), vRect, GlassLevel::Thin, rowRoundDp(), viewSurf);

    // The child's WindowPadding gutter (cpMenuGutter) IS the left margin now — one indent for
    // every nav view, shared with the docked Copilot menus. The old ad-hoc Indent(6dp) stacked
    // on top of it and is gone.

    switch (s_ai.view) {
        case 0:
            drawCurrentRunView(st);
            break;
        case 1:  // Plan: the active session's editable checklist + document checkpoints.
            drawPlanBoardView(st);
            break;
        case 2:  // Activity: live render grid of agent-edited docs + the documents panel.
            drawActivityView(st);
            break;
        case 3:  // Sessions: the docked Copilot's exact session list, in a wider pane.
            chatDrawSessionsList(st, busy);
            break;
        case 4:  // Insights: leaderboard board + token spend / usage aggregation.
            drawInsightsView(st);
            break;
        case 5:  // Options: run-behavior + workspace-visibility knobs.
            drawOptionsView(st, busy);
            break;
        case 6:  // Sub-agents: project-wide roster with docked details (current_run_view.cpp).
            drawSubagentHubView(st);
            break;
        default:
            setView(0);
            break;
    }
    drawWindowPillScrollbarV(viewSurf);
    ImGui::EndChild();
}

void drawAiWorkspace(AppState& st) {
    const float S      = ui::uiScale;
    const bool  busy   = st.convRunning(st.activeConvId());
    const bool  canRun = st.workerStarted && !busy;

    // Keep model discovery + the doc-bound conversation set fresh even when the docked
    // Copilot column is not drawn (this hub replaces it in the AI workspace).
    refreshModelAvailability(st, false);
    chatWorkspaceSync(st);
    activeWorkPump(st);  // drive at most one Active Work preview render per frame (thumb queue)
    // Poll on every compartment so project switches invalidate stale roster data promptly.
    pollCurrentRun(st);
    // Resume-safe autosave: the docked Copilot column (which normally drives this) is not
    // drawn while the AI tab is active, so flush streaming chat here too — a crash mid-turn
    // keeps the assistant text on disk (drafts ride the shared composer's savePromptDraft).
    maybePersistChatHistoryDebounced(st, /*force=*/false);
    if (s_ai.view < 0) s_ai.view = aiWorkspaceView();   // restore the last nav selection

    // Follow runs even while the compact layout is showing only Chat. Seeding a session is
    // silent, as before; only a new live run in that session changes the selected compartment.
    {
        const std::string convId = st.activeConvId();
        const std::string runId  = agentDagSummary(convId).runId;
        auto [seen, firstSight] = s_ai.seenRunByConv.try_emplace(convId, runId);
        if (!firstSight && seen->second != runId) {
            seen->second = runId;
            if (aiFollowActiveSession() && !runId.empty() && st.convRunning(convId))
                setView(0);
        }
    }

    // Keep the three-column arrangement only when chat, nav and a useful compartment all
    // fit. The breakpoint and bounds scale together; compact mode gives either pane the
    // full width without changing the persisted compartment selection. The Sub-agents hub
    // takes a larger share of the utility side (side-by-side roster and details) while the
    // chat keeps its minimum width.
    const float totalW  = ImGui::GetContentRegionAvail().x;
    const float gap     = 10.0f * S;
    const float navW    = 168.0f * S;
    const float chatMin = 360.0f * S;
    const float viewMin = 300.0f * S;
    const bool hubView  = s_ai.view == 6;
    const bool compact = totalW < chatMin + navW + gap * 2.0f + viewMin;
    const float chatShare = hubView ? 0.34f : 0.44f;
    const float chatCap   = (hubView ? 640.0f : 780.0f) * S;
    const float chatW = compact ? totalW :
        std::min(std::max(chatMin, std::min(totalW * chatShare, chatCap)),
                 totalW - navW - gap * 2.0f - viewMin);

    if (compact) {
        const CozyDensity density;
        static const char* const panes[] = {"Chat", "Workspace"};
        kit::segmented("##ai_ws_compact_pane", panes, 2, &s_ai.compactPane);
        // Keep context alongside the switch when useful space remains; otherwise wrap it.
        const float remaining = totalW - ImGui::GetItemRectSize().x - gap;
        if (remaining >= ui::metrics::px(120.0f)) ImGui::SameLine(0.0f, gap);
        drawProjectChip(st);
        kit::gapRow();
    }

    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const float  bodyH  = ImGui::GetContentRegionAvail().y;
    const ImU32  hairline = ui::Palette::toU32(palette.glassBorder);

    // ---- left: main chat ----------------------------------------------------------
    if (!compact || s_ai.compactPane == 0) {
        // Horizontal gutter is ASYMMETRIC: the pill scrollbar is clipped to this child, so the right
        // side must reserve a full kChatScrollbarLane or the chat runs under the bar. ImGui's
        // WindowPadding is symmetric, so push the WIDE (right) value and pull the LEFT edge back to
        // the shared cpMenuGutter with a negative Indent below.
        const float padL = ui::metrics::cpMenuGutter();
        const float padR = std::max(padL, ui::metrics::px(ui::kChatScrollbarLane));
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(padR, ui::metrics::px(8.0f)));
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        ImGui::BeginChild("##ai_ws_conv", ImVec2(chatW, 0),
                          ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
        if (padR > padL) ImGui::Indent(padL - padR);   // negative: pull the LEFT edge back to padL
        ImGui::PopStyleColor();
        ImGui::PopStyleVar();
        // Liquid-glass slab behind the conversation (same recipe as the view pane); the same
        // surface backs the pill scrollbar if the column ever overflows.
        const ImVec2 cw0 = ImGui::GetWindowPos();
        const ImRect cRect(cw0, ImVec2(cw0.x + ImGui::GetWindowSize().x,
                                     cw0.y + ImGui::GetWindowSize().y));
        const AdaptiveSurface& convSurf =
            adaptivePanel(panelId("ai_ws_conv_glass"), cRect, GlassGroup::Global);
        glassPanel(ImGui::GetWindowDrawList(), cRect, GlassLevel::Thin, rowRoundDp(), convSurf);
        drawChatCenter(st, busy, canRun);  // same state/send path as the dock
        if (padR > padL) ImGui::Unindent(padL - padR);  // undo before the scrollbar geometry read
        drawWindowPillScrollbarV(convSurf, ui::kChatScrollbar);  // same 9px right-edge bar as the dock
        ImGui::EndChild();
    }

    if (!compact) ImGui::SameLine(0.0f, gap);

    // ---- right: navigation + compartment -----------------------------------------
    if (!compact || s_ai.compactPane == 1) {
        // Pure layout wrapper: nav + view on wide surfaces, selector + view on compact ones.
        // The view carries its own gutter + pill scrollbar; the wrapper must never scroll
        // or grow a bar of its own — hence 0 padding and NoScrollbar|NoScrollWithMouse.
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
        ImGui::BeginChild("##ai_ws_right", ImVec2(0, 0), ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
        ImGui::PopStyleVar();

        if (compact) {
            // Display order keeps Sub-agents beside Current Run; values stay the persisted ids.
            static const char* const compartments[] = {
                "Current Run", "Sub-agents", "Plan", "Activity", "Sessions", "Insights", "Options"
            };
            static const int compartmentView[] = {0, 6, 1, 2, 3, 4, 5};
            int pick = 0;
            for (int i = 0; i < 7; ++i)
                if (compartmentView[i] == s_ai.view) pick = i;
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::Combo("##ai_ws_compartment", &pick, compartments, 7))
                setView(compartmentView[pick]);
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Choose a workspace compartment.");
            kit::gapRow();
        } else {
            drawNavColumn(st, navW);
            ImGui::SameLine(0.0f, gap);
        }
        drawViewPane(st, busy);

        ImGui::EndChild();  // ##ai_ws_right
    }

    // Vertical hairline along the chat/utility boundary (full height).
    if (!compact) {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const float x = origin.x + chatW + gap * 0.5f;
        dl->AddLine(ImVec2(x, origin.y), ImVec2(x, origin.y + bodyH), hairline,
                    ui::metrics::stroke(1.0f));
    }

}

}  // namespace ui
