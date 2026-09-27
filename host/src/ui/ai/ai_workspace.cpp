// AI workspace: main conversation plus a project-scoped utility column.
// Current Run owns the execution trace, sub-agent roster, and single docked inspector;
// Plan, Activity, Sessions, Insights and Options occupy the other nav compartments.
#include "ai_workspace.h"

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <string>

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
    int view = 0;  // Current Run by default; main conversation is always visible.
    std::string lastAutoRun;
};

static AiWsState s_ai;


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

    const std::string projectDir = convProjectDir(st);
    const std::filesystem::path project(projectDir);
    std::string projectName = project.filename().string();
    if (projectName.empty() && !projectDir.empty())
        projectName = project.parent_path().filename().string();
    const std::string shortName = kit::elide(
        projectName.empty() ? "No project selected" : projectName.c_str(),
        navW - ui::metrics::cpMenuGutter() * 3.0f);
    kit::sectionHeader("Active project", nullptr, nullptr, -1, true);
    kit::chip(shortName.c_str(),
              projectName.empty() ? kit::Tone::Neutral : kit::Tone::Accent,
              false, projectDir.empty() ? nullptr : projectDir.c_str());
    kit::gapSection();

    const int live = currentRunLiveCount();

    if (navRow(0, "Current Run", s_ai.view == 0, live)) s_ai.view = 0;
    if (navRow(1, "Plan",        s_ai.view == 1))       s_ai.view = 1;
    if (navRow(2, "Activity",    s_ai.view == 2))       s_ai.view = 2;
    if (navRow(3, "Sessions", s_ai.view == 3)) s_ai.view = 3;
    if (navRow(4, "Insights", s_ai.view == 4)) s_ai.view = 4;

    // Hairline + the utility section.
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuSectionGap()));
    {
        const ImVec2 c = ImGui::GetCursorScreenPos();
        dl->AddLine(ImVec2(c.x + ui::metrics::cpMenuRowGap(), c.y),
                    ImVec2(c.x + navW - ui::metrics::cpMenuGutter() * 2.0f, c.y),
                    ui::Palette::toU32(palette.glassBorder), ui::metrics::stroke(1.0f));
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuHeadGap()));

    if (navRow(5, "Options", s_ai.view == 5)) s_ai.view = 5;

    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();
}

// ---- center: expanded chat --------------------------------------------------------

static void drawChatCenter(AppState& st, bool busy, bool canRun) {
    const float S = ui::uiScale;

    // Main conversation stays visible while Current Run inspects one selected agent.

    // Reserve the composer height measured last frame so the log fills the rest
    // (same one-frame-lag trick as the docked Copilot).
    static float s_composerH = 0.0f;
    float reserve = (s_composerH > 0.0f ? s_composerH
                                        : ImGui::GetFrameHeightWithSpacing() * 4.0f) +
                    14.0f * S;
    float logH = ImGui::GetContentRegionAvail().y - reserve;
    if (logH < 120.0f * S) logH = 120.0f * S;

    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuGutter(), ui::metrics::px(8.0f)));
    ImGui::BeginChild("##ai_ws_chatlog", ImVec2(0, logH),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleVar();
    // The chat column's slab is drawn by the host child; resolve the same surface id here so
    // the pill scrollbar picks up matching tones.
    const ImVec2 lw0 = ImGui::GetWindowPos();
    const ImVec2 lwSize = ImGui::GetWindowSize();
    const AdaptiveSurface& logSurf = adaptivePanel(
        panelId("ai_ws_conv_glass"),
        ImRect(lw0, ImVec2(lw0.x + ImGui::GetWindowSize().x,
                           lw0.y + ImGui::GetWindowSize().y)),
        GlassGroup::Global);
    if (st.chat.empty()) {
        emptyState("ai_panel", "Start a conversation",
                   "Describe what to build — the copilot plans, models and verifies it.");
    } else {
        chatDrawLog(st);
        // Shared stick-to-bottom (releases on a user scroll-up). The region id scopes the flag
        // to this host so the docked Copilot column and the workspace never fight over one.
        chatLogStickToBottom("aiws.chatlog");
    }
    drawWindowPillScrollbarV(logSurf);
    ImGui::EndChild();
    ImGui::PopStyleColor();
    // Floating jump-to-latest arrow over the chat viewport's bottom-right corner; hidden while
    // pinned to the tail. Region id matches the stick call above.
    chatDrawJumpToLatest("aiws.chatlog", lw0,
                         ImVec2(lw0.x + lwSize.x, lw0.y + lwSize.y));

    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    ImGui::BeginGroup();
    chatDrawComposer(st, busy, canRun);   // the docked Copilot's real composer + send path
    ImGui::EndGroup();
    s_composerH = ImGui::GetItemRectSize().y + 6.0f * S;
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
            static const char* kVerb[] = {"Quiet", "Normal", "Full"};
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Detail level");
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::Combo("##ai_ws_verb", &v, kVerb, 3)) setAiUiVerbosity(v);
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("How much detail the Current Run and Active Work views show.");
        }
        ImGui::TableNextColumn();
        {
            bool follow = aiFollowActiveSession();
            if (ImGui::Checkbox("Follow live runs", &follow)) setAiFollowActiveSession(follow);
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Switch to Current Run when the viewed conversation starts a new run.");
        }
        ImGui::TableNextColumn();
        {
            bool prev = aiActiveWorkPreviews();
            if (ImGui::Checkbox("Active Work previews", &prev)) setAiActiveWorkPreviews(prev);
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Render iso-preview thumbnails for the docs agents are "
                                  "editing (worker render jobs; cached per saved revision).");
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
    ImGui::TextWrapped("Workflow knobs persist to config.json (shared with the docked Copilot). "
                       "Provider, model, API key and privacy live on the main Settings page "
                       "(Settings -> AI).");
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

    // Follow new runs in the viewed conversation only.
    const auto dag = agentDagSummary(st.activeConvId());
    const std::string runKey = st.activeConvId() + ":" + dag.runId;
    if (aiFollowActiveSession() && !dag.runId.empty() && runKey != s_ai.lastAutoRun) {
        s_ai.lastAutoRun = runKey;
        s_ai.view = 0;
    }

    // The child's WindowPadding gutter (cpMenuGutter) IS the left margin now — one indent for
    // every nav view, shared with the docked Copilot menus. The old ad-hoc Indent(6dp) stacked
    // on top of it and is gone.

    switch (s_ai.view) {
        case 0:
            drawCurrentRunView(st);
            break;
        case 1:  // Session Plan: the plan board over persisted buildgraph runs.
            if (!st.workerStarted)
                emptyState("health_check", "Worker not running",
                           "The Chudware worker service is required to load and steer plans.");
            else
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
        default:
            s_ai.view = 0;
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

    // v3 layout (Hunter's directive): MAIN CHAT on the left, everything else in the right
    // utility panel — a vertical nav menu + the selected view. No sessions rail, no bottom
    // options strip. A hairline marks the chat/utility boundary (layout.cpp grammar).
    const float totalW = ImGui::GetContentRegionAvail().x;
    const float gap    = 10.0f * S;
    const float navW   = 168.0f * S;

    // Chat column: a generous main-chat slice — 44% with clean bounds so it never starves
    // (min) or sprawls on ultrawide monitors (max). Full height: log + composer inside.
    const float chatW = std::max(360.0f * S, std::min(totalW * 0.44f, 780.0f * S));

    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const float  bodyH  = ImGui::GetContentRegionAvail().y;
    const ImU32  hairline = ui::Palette::toU32(palette.glassBorder);

    // ---- left: main chat ----------------------------------------------------------
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
    drawChatCenter(st, busy, canRun);   // log + composer (same state/send path as the dock)
    if (padR > padL) ImGui::Unindent(padL - padR);  // undo before the scrollbar geometry read
    drawWindowPillScrollbarV(convSurf, ui::kChatScrollbar);  // same 9px right-edge bar as the dock
    ImGui::EndChild();

    ImGui::SameLine(0.0f, gap);

    // ---- right: navigation + compartment -----------------------------------------

    // Pure layout wrapper: it only hosts the nav column + the view pane side by side, both of
    // which fill it exactly and carry their own gutter + pill scrollbar. It must never scroll
    // or grow a bar of its own — hence 0 padding and NoScrollbar|NoScrollWithMouse.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##ai_ws_right", ImVec2(0, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImGui::PopStyleVar();

    drawNavColumn(st, navW);
    ImGui::SameLine(0.0f, gap);
    drawViewPane(st, busy);

    ImGui::EndChild();  // ##ai_ws_right

    // Vertical hairline along the chat/utility boundary (full height).
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const float x = origin.x + chatW + gap * 0.5f;
        dl->AddLine(ImVec2(x, origin.y), ImVec2(x, origin.y + bodyH), hairline,
                    ui::metrics::stroke(1.0f));
    }

}

}  // namespace ui
