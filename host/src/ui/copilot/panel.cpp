// Chudware Copilot pane (T-RF6 split of legacy_chat.cpp — this file is the pane host).
//
// A PINNED, full-height, rounded liquid-glass column overlaid on the viewport's right edge with
// ~12dp margins (geometry from ui::viewportBackdrop(); invisible left-edge drag zone to resize;
// the docked task-pane layout is gone — this is the only layout). Top-down: a slim header (the
// live animated tesseract brand mark, tinted by run status — its animation style comes from
// Settings -> Appearance -> Brand mark — plus status + Sessions + settings gear), the
// conversation log with a per-turn transparent activity timeline (what the AI is doing /
// thinking, expandable), and a bottom composer (compact
// Agent/Plan/Swarm/Edit/Ask dropdown + context meter; transparent prompt with in-box send;
// graduated-width segmented model/thinking/paperclip/cube/New toolbar — model biggest, icons
// smallest). Header gear swaps the chat area for a compact AI-settings overlay (horizontal top
// tabs for Agents/Prompts/Skills/Context/Permissions/Providers). The full form lives in the
// Settings modal AI tab. Bubbles/cards and the composer are painted as translucent adaptive
// Liquid-Glass bubbles over the live viewport backdrop (host contract H5, GlassGroup::Copilot)
// so every ink tone stays readable over ANY model color.
//
// This file hosts the header, the Sessions/Tasks/Artifacts side panels, drawAIPanel itself,
// and the thin ai_workspace reuse hooks (chatWorkspaceSync/chatDrawLog/chatDrawComposer/...)
// that let the full-page AI workspace embed the SAME chat state + send path.

#include "copilot_internal.h"
#include "ui/tesseract.h"  // ui::drawTesseract — the animated 4-cube brand mark in the header
#include "core/engine_events.h"  // core::mirrorRecoveredDocs — Reopen a dismissed live agent doc
#include "ui/ai/ai_prefs.h"      // ui::chatShowRenders — Artifacts render-thumbnail display pref
#include "gl_compat.h"           // glDeleteTextures — LRU eviction of render thumbnails

namespace ui {

// ---- header: logo row + collapse + [Chat L | Sessions C | Settings R] + status ----
// Copilot vertical-collapse state (file scope so the header's collapse chevron and drawAIPanel's
// height animation share it). Mirrors the FeatureManager collapse (T-G21: rolls UP to this header
// strip instead of shrinking to a pill in both axes). Seeded from st.settings.copilotCollapsed on
// drawAIPanel's first call (see s_cpInited there) so a collapsed column stays collapsed across
// restarts.
static bool s_cpCollapsed = false;
// The header is a RADIO strip, not four independent toggles: exactly one of Chat / Tasks /
// Artifacts / Sessions / Settings is showing, and Chat is "no overlay". AppState carries it as
// four bools (integrator-owned), so funnel every read and write through these two so a new
// surface can never leave two of them true — or, worse, set one without clearing the rest.
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
    const float sc = ui::uiScale;
    const ImGuiStyle& style = ImGui::GetStyle();
    const float lh = ImGui::GetTextLineHeight();

    ImFont* wordF = st.fonts.medium ? st.fonts.medium : nullptr;
    float wordLh = lh;
    if (wordF) { ImGui::PushFont(wordF); wordLh = ImGui::GetTextLineHeight(); ImGui::PopFont(); }

    // Selection as it stands entering the frame, and the selection this frame's clicks
    // resolve to. Nothing paints until every hit zone has been submitted and `tabOut` is
    // final, so a tab never draws the previous selection on the frame you pick it.
    const CpTab tabIn = cpActiveTab(st);
    CpTab tabOut = tabIn;

    // Zero extra right inset — the window padding (7px) is the only gap between the
    // Artifacts/Settings buttons and the column edge, which itself hugs the program edge.
    const float rightInset = 0.0f;
    // Collapse chip sits on the left, vertically centered between row 1's
    // 0/0 tasks chip and row 2's Chat button. Row content shifts right past
    // it via contentLeftInset. Independent of rightInset (was a shared var —
    // zeroing it for the header-edge tightening put the chip's floating window
    // flush on the main window's origin, exactly overlapping the invisible
    // left-edge resize strip and stealing its clicks — T-broke-collapse).
    const float chevSz  = 26.0f * sc;
    const float leftInset = ui::metrics::U(0.5f);
    const float contentLeftInset = leftInset + chevSz + 6.0f * sc;
    const float colW = ImGui::GetContentRegionAvail().x;
    const float minGap = style.ItemSpacing.x;

    auto measureBtn = [&](const char* label, const char* iconName, bool withLabel) -> float {
        const float iconSz = lh * 0.92f;
        const float iconGap = 4.0f * sc;
        IconUV icon = (iconName && iconName[0]) ? ui::getIcon(iconName, 20) : IconUV{};
        const float padX = 6.0f * sc;
        const float tw = withLabel ? ImGui::CalcTextSize(label).x : 0.0f;
        float contentW = tw;
        if (icon.valid) contentW += iconSz + (withLabel ? iconGap : 0.0f);
        // Common minimum width so row 1's tasks chip and row 2's Chat/Sessions/Settings line up
        // as COLUMNS instead of five ragged pills sized only by their label length. An
        // icon-only pill drops that floor — the whole point of it is to be small.
        const float floorW = withLabel ? ui::metrics::px(58.0f) : (iconSz + padX * 2.0f);
        return std::max(contentW + padX * 2.0f, floorW);
    };
    // Responsive collapse. A pinned column narrows to dockRightMin(), which is not wide enough
    // for five labelled pills plus the wordmark — and the old fixed layout answered that by
    // clamping every pill to the same overlapping x, piling "0/0" on top of "Chudware" and
    // "Chat" on top of "Sessions". Shed chrome in order of value instead: labels first (the
    // icons and tooltips still identify each tab), then the wordmark, then the brand mark.
    const bool tabLabels =
        (measureBtn("Chat", "ai_panel", true) + measureBtn("Sessions", "history", true) +
         measureBtn("Settings", "settings", true) + 2.0f * minGap)
        <= (colW - rightInset - contentLeftInset);
    const float chatW = measureBtn("Chat", "ai_panel", tabLabels);
    const float sessW = measureBtn("Sessions", "history", tabLabels);
    const float gearW = measureBtn("Settings", "settings", tabLabels);

    // ---- Collapse chip: drawn INSIDE this window at an absolute screen position
    // (SetCursorScreenPos), exactly like the FeatureManager's ##lp_collapse — NOT a
    // separate nested Begin()'d floating window. A standalone utility window here
    // silently loses hover/click forever the first time the main Copilot window is
    // refocused: NoBringToFrontOnFocus keeps it pinned behind the (constantly
    // refocused) main window in z-order for HIT-TESTING even though it still RENDERS
    // fine (nothing else paints that pixel region, so the stale draw looks correct).
    // Confirmed via a live click test: mouse sat inside the button's own reported
    // rect every frame, yet IsItemHovered() stayed false. Drawing it as a same-window
    // widget removes the cross-window z-order ambiguity entirely; submitting it AFTER
    // the left-edge resize hit-zone (drawAIPanel, above drawCopilotHeader's call) lets
    // it win the same-window overlap the normal "later item wins" way.
    {
        const ImVec2 winP0 = ImGui::GetWindowPos();
        // The header is pulled to window top (drawAIPanel SetCursorPosY(0)), so the
        // true stack is: 6sc top pad + row-1 (wordLh) + row-2 (frame height). Center
        // the chip across exactly that — no content-region/padding offset.
        const float row1H = 6.0f * sc + wordLh;
        const float row2H = std::max(lh, ImGui::GetFrameHeight());
        const float chipY = winP0.y + (row1H + row2H - chevSz) * 0.5f;
        const ImVec2 c0(winP0.x + leftInset, chipY);
        const ImVec2 savedCur = ImGui::GetCursorScreenPos();
        ImGui::SetCursorScreenPos(c0);
        const bool cClicked = ImGui::InvisibleButton("##cp_collapse", ImVec2(chevSz, chevSz));
        const bool cHover = ImGui::IsItemHovered();
        ImDrawList* wdl = ImGui::GetWindowDrawList();
        const ImVec2 b1(c0.x + chevSz, c0.y + chevSz);
        // Shared collapse-chip recipe (must match FM ##lp_collapse):
        // rest wash 0.32 / hover 0.50, textPrimary glyph, 1.85px stroke.
        // glassRowBackground takes LOGICAL dp (it scales internally) — un-scale the shared chip
        // token so the chevron chip carries the same corner as every other copilot-menu pill.
        ui::glassRowBackground(wdl, c0, b1, ui::metrics::cpMenuChipRound() / sc, false, cHover,
                               cHover ? 0.50f : 0.32f);
        const ImU32 cCol = ImGui::ColorConvertFloat4ToU32(chatSurf().textPrimary);
        const ImVec2 cc(c0.x + chevSz * 0.5f, c0.y + chevSz * 0.5f);
        const float ca = 3.5f * sc;
        const float stroke = 1.85f * sc;
        // "«" collapse to the left, "»" expand back right.
        const float dir = s_cpCollapsed ? 1.0f : -1.0f;
        for (int i = 0; i < 2; ++i) {
            const float ox = (i == 0 ? -2.5f : 2.5f) * sc;
            wdl->AddLine(ImVec2(cc.x + dir * (ox - ca * 0.5f), cc.y - ca),
                         ImVec2(cc.x + dir * (ox + ca * 0.5f), cc.y), cCol, stroke);
            wdl->AddLine(ImVec2(cc.x + dir * (ox + ca * 0.5f), cc.y),
                         ImVec2(cc.x + dir * (ox - ca * 0.5f), cc.y + ca), cCol, stroke);
        }
        if (cHover) ImGui::SetTooltip(s_cpCollapsed ? "Expand" : "Collapse");
        if (cClicked) {
            s_cpCollapsed = !s_cpCollapsed;
            st.settings.copilotCollapsed = s_cpCollapsed;
            saveSettings(st.settings);
        }
        ImGui::SetCursorScreenPos(savedCur);
    }

    // ---- Row 1: [Tasks] .. centered Chudware .. [Artifacts]  (clears leftInset) ----
    // Rows 1 and 2 submit HIT ZONES only. Every tab pill is painted together at the end,
    // after this frame's click has already moved `tabOut`.
    int tlDone = 0, tlTotal = 0;
    copilotTaskProgress(st, tlDone, tlTotal);
    char tlLabel[24];
    std::snprintf(tlLabel, sizeof(tlLabel), "%d/%d", tlDone, tlTotal);
    HeaderBtnHit hTasks, hArts;
    {
        const float logoGap = 3.0f * sc;
        // The mark is a 32-edge figure, not a 12-edge cube glyph: measured against rendered
        // filmstrips, its nested-cell structure only resolves from ~28 px up, so it is sized off
        // the wordmark's line height rather than clamped under it. Row 1 grows to match (rowH1).
        const float markSz = wordLh * 1.5f;
        if (wordF) ImGui::PushFont(wordF);
        const float wmW = ImGui::CalcTextSize("Chudware").x;
        if (wordF) ImGui::PopFont();
        ImGui::Dummy(ImVec2(0, 6.0f * sc));
        const float rowTop1 = ImGui::GetCursorPosY();
        const float bh = lh + 4.0f * sc;
        // Row 1 is as tall as its tallest occupant — the brand mark — and everything in it
        // (tasks chip, mark, wordmark, artifacts chip) centres on that, not on wordLh alone.
        const float rowH1 = std::max(wordLh, markSz);
        auto cy1 = [&](float h) { ImGui::SetCursorPosY(rowTop1 + (rowH1 - h) * 0.5f); };

        const float tlW = measureBtn(tlLabel, "plan_board", /*withLabel=*/true);
        const float artW = measureBtn("Artifacts", "artifacts", tabLabels);

        // Shed the wordmark, then the mark, rather than letting the brand collide with the
        // chips. `pillGap` is the clearance each side of the logo cluster must keep.
        const float pillGap = 6.0f * sc;
        const float brandRoom = colW - rightInset - contentLeftInset - tlW - artW - pillGap * 2.0f;
        const bool showWordmark = brandRoom >= markSz + logoGap + wmW;
        const bool showMark     = showWordmark || brandRoom >= markSz;
        const float logoW = showWordmark ? (markSz + logoGap + wmW) : (showMark ? markSz : 0.0f);

        cy1(bh);
        ImGui::SetCursorPosX(contentLeftInset);
        // NOTE: deliberately NOT gated on `busy` — these are read-only viewer panels
        // (task list / artifacts / chat log / session switcher), not actions that could
        // race a running turn. Users need to check progress while a run is in motion.
        hTasks = headerTextButtonHit("##hdr_tasks", tlW);
        if (hTasks.clicked) tabOut = cpTabAfterClick(tabIn, CpTab::Tasks);

        const float* statusCol = pal.accentMid;  const char* statusTip = "Idle";
        if (st.statusKind == 2) { statusCol = pal.statusErr; statusTip = "Error"; }
        // Per-conversation: `busy` is convRunning(activeConv) and activeConversationRunning
        // covers one-shot RPC turns. The global agentV2Running is deliberately NOT OR'd in —
        // it would light this dot for an idle session whenever ANY other session is running.
        else if (busy || activeConversationRunning(st)) {
            statusCol = pal.accent; statusTip = "Running";
        }
        const ImVec4 statusTint(statusCol[0], statusCol[1], statusCol[2], 1.0f);

        if (showMark) {
            // Centre the logo cluster, then clamp it into the gap between the two chips. The
            // clamps can no longer invert: logoW was shrunk above until the gap could hold it.
            const float leftLimit  = contentLeftInset + tlW + pillGap;
            const float rightLimit = colW - rightInset - artW - logoW - pillGap;
            const float logoLeft = std::max(leftLimit, std::min((colW - logoW) * 0.5f, rightLimit));
            ImGui::SameLine(logoLeft);
            {
                // Brand mark: a live 4D tesseract painted straight onto the window draw list.
                // The Dummy reserves EXACTLY the box the old atlas cube occupied, so logoW and
                // the centring clamps stay valid, and it carries the status tooltip.
                cy1(markSz);
                ImGui::Dummy(ImVec2(markSz, markSz));
                const ImVec2 mkMin = ImGui::GetItemRectMin(), mkMax = ImGui::GetItemRectMax();
                ui::drawTesseract(ImGui::GetWindowDrawList(),
                                  ImVec2((mkMin.x + mkMax.x) * 0.5f, (mkMin.y + mkMax.y) * 0.5f),
                                  markSz, ImGui::GetTime(),
                                  ui::tesseractAnimFromInt(st.settings.brandAnim),
                                  ImGui::ColorConvertFloat4ToU32(statusTint));
                if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", statusTip);
            }
            if (showWordmark) {
                ImGui::SameLine(0, logoGap);
                if (wordF) ImGui::PushFont(wordF);
                cy1(wordLh);
                ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
                ImGui::TextUnformatted("Chudware");
                ImGui::PopStyleColor();
                if (wordF) ImGui::PopFont();
            }
        }

        ImGui::SameLine(colW - rightInset - artW);
        cy1(bh);
        hArts = headerTextButtonHit("##hdr_arts", artW);
        if (hArts.clicked) tabOut = cpTabAfterClick(tabIn, CpTab::Artifacts);
        ImGui::SetCursorPosY(rowTop1 + rowH1);
    }

    // ---- Row 2: Chat | Sessions | Settings (equal gaps; left edge = leftInset) ----
    const float rowH   = std::max(lh, ImGui::GetFrameHeight());
    const float rowTop = ImGui::GetCursorPosY();
    auto centerY = [&](float h) { ImGui::SetCursorPosY(rowTop + (rowH - h) * 0.5f); };

    const float usableL = contentLeftInset;
    const float usableR = colW - rightInset;
    const float free = usableR - usableL - chatW - sessW - gearW;
    float gapEq = free * 0.5f;
    if (gapEq < minGap) gapEq = minGap;
    const float chatX = usableL;
    const float setX  = usableR - gearW;
    // Clamp Sessions into the gap between the two, LOW bound last: the old max-then-min order
    // inverted on a narrow column and stacked Sessions on top of Chat. tabLabels already
    // guarantees the three fit, so the fallback only matters for an absurdly narrow column.
    const float sessLo = chatX + chatW + minGap;
    const float sessHi = setX - sessW - minGap;
    const float sessX = (sessHi >= sessLo)
                            ? std::min(std::max(usableL + chatW + gapEq, sessLo), sessHi)
                            : sessLo;

    centerY(lh);
    ImGui::SetCursorPosX(chatX);
    HeaderBtnHit hChat = headerTextButtonHit("##hdr_chat", chatW);
    if (hChat.clicked) tabOut = CpTab::Chat;

    centerY(lh);
    ImGui::SetCursorPosX(sessX);
    HeaderBtnHit hSess = headerTextButtonHit("##hdr_sess", sessW);
    if (hSess.clicked) tabOut = cpTabAfterClick(tabIn, CpTab::Sessions);

    centerY(lh);
    ImGui::SetCursorPosX(setX);
    HeaderBtnHit hGear = headerTextButtonHit("##hdr_gear", gearW);
    if (hGear.clicked) tabOut = cpTabAfterClick(tabIn, CpTab::Settings);

    // ---- Resolve, THEN paint. Every pill draws from the same, final selection, so the tab
    // you just picked lights up on the very frame you release the mouse. ----
    if (tabOut != tabIn) cpSetTab(st, tabOut);
    headerTextButtonPaint(hTasks, tlLabel,     tabOut == CpTab::Tasks,     "plan_board", true);
    headerTextButtonPaint(hArts,  "Artifacts", tabOut == CpTab::Artifacts, "artifacts", tabLabels);
    headerTextButtonPaint(hChat,  "Chat",      tabOut == CpTab::Chat,      "ai_panel",  tabLabels);
    headerTextButtonPaint(hSess,  "Sessions",  tabOut == CpTab::Sessions,  "history",   tabLabels);
    headerTextButtonPaint(hGear,  "Settings",  tabOut == CpTab::Settings,  "settings",  tabLabels);

    // One tooltip at most; each says what the click will DO, including the click that takes
    // you back to the conversation.
    const char* kBack = "Back to the conversation";
    if (hTasks.hovered) {
        if (tabOut == CpTab::Tasks) ImGui::SetTooltip("%s", kBack);
        else ImGui::SetTooltip("Tasks \xc2\xb7 %d of %d done \xc2\xb7 click for the task list",
                               tlDone, tlTotal);
    } else if (hArts.hovered) {
        ImGui::SetTooltip(tabOut == CpTab::Artifacts
                              ? kBack
                              : "Artifacts \xc2\xb7 documents, renders and files this session");
    } else if (hChat.hovered) {
        ImGui::SetTooltip(tabOut == CpTab::Chat ? "Chat \xc2\xb7 conversation with the agent"
                                                : kBack);
    } else if (hSess.hovered) {
        ImGui::SetTooltip(tabOut == CpTab::Sessions ? kBack
                                                    : "Sessions \xc2\xb7 switch between chats");
    } else if (hGear.hovered) {
        ImGui::SetTooltip(tabOut == CpTab::Settings
                              ? kBack
                              : "AI settings \xc2\xb7 provider, model, privacy");
    }

    ImGui::SetCursorPosY(std::max(ImGui::GetCursorPosY(), rowTop + rowH));
    // Breathing room between row 2 and the Separator drawAIPanel emits right after — the two were
    // flush, which read as the buttons sitting ON the rule. ItemSpacing.y is 0 here, so this is exact.
    ImGui::Dummy(ImVec2(0, ui::metrics::px(4.0f)));
}

// In-flight marker for a session row: the brand tesseract spinner at the row's left edge,
// animated by the same per-frame repaint that streams the run itself. Returns the extra
// x-indent title/subtitle text must use to clear the icon. Shared by the current-doc
// Sessions rows and the "Other sessions" rows so both read identically.
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

// Sessions list — replaces the chat log while open so users can jump between sessions easily.
void drawSessionsPanel(AppState& st, bool busy) {
    (void)busy;  // runs are per-conversation; this panel never locks on the viewed run
    const float sc = ui::uiScale;
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();

    ensureConversations(st);

    // One title treatment for every copilot menu (ui_common cpMenu*). The "Show archived"
    // filter sits in the header's accessory slot — above the rule, next to the title cluster
    // — instead of a full body row between the button and the list.
    static bool s_showArchived = false;
    ui::cpMenuHeader(st.fonts, "Sessions", nullptr, [&]{
        // Checkbox ink must come from the glass surface like every other label in this menu:
        // the theme's default text tone is tuned for opaque panels and vanished against the
        // bright viewport backdrop showing through the copilot column.
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::Checkbox("Show archived", &s_showArchived);
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(s_showArchived
                ? "Showing archived sessions only. Uncheck to return to your active list."
                : "Archived sessions are hidden from this list \xe2\x80\x94 check to review or "
                  "restore them. Archiving keeps the full transcript (unlike Delete).");
    });

    // New-session is the one PRIMARY action in the copilot menus, so it uses the accent
    // primaryButton rather than the theme's default slab (which rendered as a flat dark block
    // that read as a disabled panel over the light glass). Return to chat via the header Chat
    // button (Sessions stays labeled "Sessions" — no "< Chat" rename that reflows the header).
    // New-session stays enabled while a run is active: runs are per-conversation (st.runs)
    // and park/restore keeps them streaming in the background, so starting or switching
    // sessions mid-run is safe — this is the multi-session workflow.
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::cpMenuChipRound());
    if (primaryButton("+ New session", ImVec2(-1, 0))) {
        newConversation(st);
        st.prompt[0] = 0;
        cpSetTab(st, CpTab::Chat);   // land in the new chat, not stuck on the list
    }
    ImGui::PopStyleVar();
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Start a fresh conversation.");

    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));

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
    bool anyRowShown = false;
    // Newest sessions first: newConversation() appends, so the tail of st.conversations is the
    // most recent session — walk the vector back-to-front so it lands at the TOP of the list.
    for (int i = (int)st.conversations.size() - 1; i >= 0; --i) {
        Conversation& c = st.conversations[i];
        // Sessions are document-independent: list ALL of them regardless of the document
        // on screen. Each row can carry a doc badge linking back to its document.
        if (c.archived != s_showArchived) continue;
        anyRowShown = true;
        // Fallback number is the conversation's own slot, not the display row — with the
        // newest-first order a row position changes every session, a slot number does not.
        std::string title = c.title.empty() ? ("Chat " + std::to_string(i + 1)) : c.title;
        const std::vector<ChatMsg>& msgs = (i == st.activeConv) ? st.chat : c.msgs;
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
        const float actionGap = 4.0f * sc;
        // Action-zone geometry must be computed ONCE and shared by the title clip and the
        // buttons below: the reserve used to assume the trash button was trashSz wide, but
        // the drawn ImageButton is img + FramePadding*2 (wider) — the Archive button slid
        // left over the title text on active/hovered rows.
        const float delImg  = trashSz - 8.0f * sc;
        const float delBtnW = delImg + style.FramePadding.x * 2.0f;
        const float delBtnH = delImg + style.FramePadding.y * 2.0f;
        const float rightMargin = 4.0f * sc;
        const float actionsW = delBtnW + archBtnW + forkBtnW +
                               actionGap * 2.0f + rightMargin;

        ImGui::PushID(i);
        const ImVec2 rowMin = ImGui::GetCursorScreenPos();
        const float  rowW   = ImGui::GetContentRegionAvail().x;
        const ImVec2 rowMax(rowMin.x + rowW, rowMin.y + rowH);

        // The action-button zone (Archive/Delete) only EXISTS on hover/active rows, so only
        // those rows reserve its width — reserving it on idle rows squeezed every title to a
        // stub for buttons that were not even drawn. Hovering re-clips the title, which reads
        // as the buttons taking their place. Hover is tested against the row rect directly:
        // the reserve is needed BEFORE this frame's InvisibleButton reports its hover state.
        const bool rowHot = active || ImGui::IsMouseHoveringRect(rowMin, rowMax);
        const float actionsReserve = rowHot ? actionsW : 0.0f;

        bool picked = ImGui::InvisibleButton("##pickconv",
                                            ImVec2(std::max(1.0f, rowW - actionsReserve - padX), rowH));
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
        ui::glassRowBackground(dl, rowMin, rowMax, rowRndDp, (i % 2) == 1, active, wash);

        // Scope color-code: once the model has assessed the session (first routing event),
        // tint the row with a subtle wash + a tiny class chip on the title line.
        // semanticOnGlass keeps the chip ink legible over dark + light glass. (The old 3px
        // left-edge bar read as a stray vertical line above the rows — removed.)
        float chipW = 0.0f;   // chip + its gap to the title — the title clip reserves this
        if (c.scopeClass >= 0) {
            float scopeRgba[4];
            scopeClassColor(c.scopeClass, scopeRgba);
            const ImVec4 scopeCol = semanticOnGlass(scopeRgba, surf);
            ImVec4 washCol = scopeCol;
            washCol.w = active ? 0.10f : 0.07f;
            dl->AddRectFilled(rowMin, rowMax, ImGui::ColorConvertFloat4ToU32(washCol),
                              rowRnd);

            const char* scopeLbl = c.scopeClass == 0 ? "trivial" : c.scopeClass == 1 ? "standard"
                                 : c.scopeClass == 2 ? "complex" : "epic";
            const ImVec2 lsz = ImGui::CalcTextSize(scopeLbl);
            // Right-aligned against whatever sits to its right this frame: the action
            // buttons on hover/active rows, the row edge on idle ones.
            const float cx1 = rowMax.x - actionsReserve - padX;
            const float cx0 = cx1 - lsz.x - 8.0f * sc;
            chipW = (cx1 - cx0) + 6.0f * sc;
            const float cy0 = rowMin.y + padY + (titleH - lsz.y) * 0.5f - 1.0f * sc;
            const float cy1 = cy0 + lsz.y + 2.0f * sc;
            ImVec4 chipBg = scopeCol;
            chipBg.w = 0.16f;
            dl->AddRectFilled(ImVec2(cx0, cy0), ImVec2(cx1, cy1),
                              ImGui::ColorConvertFloat4ToU32(chipBg),
                              ui::metrics::cpMenuChipRound());
            ImVec4 chipInk = scopeCol;
            chipInk.w = 1.0f;
            dl->AddText(ImVec2(cx0 + 4.0f * sc, cy0 + 1.0f * sc),
                        ImGui::ColorConvertFloat4ToU32(chipInk), scopeLbl);
        }

        // Titles and snippets are user text of arbitrary length and are painted straight onto
        // the draw list, which does NOT clip to the row — a long snippet used to run out past
        // the card, under the scrollbar and off the column edge. Clip both to the room they
        // actually have (title also yields the action-button zone and the scope chip — an
        // unreserved chip used to paint right over the title's tail) with a real ellipsis.
        const float titleY = rowMin.y + padY;
        // In-flight sessions get the animated tesseract icon at the row's left edge; text
        // indents past it only for those rows.
        const float runIndent = running ? drawSessionRunningIcon(dl, surf, rowMin, rowH, padX, sc)
                                        : 0.0f;
        const float textMaxW = std::max(ui::metrics::px(24.0f),
                                        rowW - padX * 2.0f - actionsReserve - chipW - runIndent);
        const std::string titleShown = ui::kit::elide(title.c_str(), textMaxW);
        ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
        dl->AddText(ImVec2(rowMin.x + padX + runIndent, titleY), ink, titleShown.c_str());

        // Document badge: sessions are document-independent but optionally carry a docLink
        // reference. Show the linked document's name on the sub line; clicking it jumps to
        // that document's tab when the tab is open. file: links always render (basename);
        // doc: links only resolve while the tab is open, otherwise the badge is hidden.
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
            // Right-aligned at the row's true right edge — hover/active rows never reach
            // here (showBadge above), so this never collides with the action buttons.
            badgeX1 = rowMax.x - padX;
            badgeX0 = std::max(rowMin.x + padX + runIndent, badgeX1 - badgeW);
        }
        // Clip the subtitle to the room it ACTUALLY has: up to the badge's left edge when a
        // badge is drawn (not the row edge — the badge sits left of the action zone).
        const float subRight = badgeW > 0.0f ? badgeX0 - 6.0f * sc : rowMax.x - padX;
        const float subMaxW = std::max(ui::metrics::px(24.0f),
                                       subRight - (rowMin.x + padX + runIndent));
        dl->AddText(ImVec2(rowMin.x + padX + runIndent, titleY + titleH + 2.0f * sc),
                    ImGui::ColorConvertFloat4ToU32(surf.textSecondary),
                    ui::kit::elide(sub, subMaxW).c_str());

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
        if (rowHot) {
            const float delX = rowMax.x - delBtnW - rightMargin;
            ImGui::SetCursorScreenPos(ImVec2(delX, rowMin.y + (rowH - delBtnH) * 0.5f));
            IconUV trash = ui::getIcon("delete", 20);
            bool del = trash.valid
                ? ImGui::ImageButton("##delconv", trash.texId, ImVec2(delImg, delImg), trash.uv0, trash.uv1)
                : ImGui::SmallButton("x");
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Delete this session");
            // Destructive row actions are blocked only for the row whose own run is live —
            // an unrelated background run never locks them.
            doDel = del && !running;

            const float archX = delX - archBtnW - actionGap;
            ImGui::SetCursorScreenPos(ImVec2(archX, rowMin.y + (rowH - delBtnH) * 0.5f));
            bool archClick = ImGui::Button(archLabel, ImVec2(archBtnW, delBtnH));
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(c.archived
                    ? "Restore to the active sessions list"
                    : "Archive: hides from the default list but keeps the full transcript "
                      "(unlike Delete). Toggle \"Show archived\" above to find it again.");
            doArchiveToggle = archClick && !running;

            const float forkX = archX - forkBtnW - actionGap;
            ImGui::SetCursorScreenPos(ImVec2(forkX, rowMin.y + (rowH - delBtnH) * 0.5f));
            doFork = ImGui::Button("Fork", ImVec2(forkBtnW, delBtnH));
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Fork this session into a new idle copy");
        }

        ImGui::SetCursorScreenPos(ImVec2(rowMin.x, rowMax.y + rowGap));
        ImGui::PopID();
        if (doDel) { closeConversation(st, i); break; }
        if (doFork) { forkConversation(st, i); break; }
        if (doArchiveToggle) { archiveConversation(st, i, !c.archived); break; }
    }
    if (!anyRowShown) {
        // Same tone and indent as every other "nothing here yet" line in the copilot menus.
        ui::cpMenuHint(s_showArchived ? "No archived sessions."
                                      : "No active sessions yet. Start one above.");
    }

}

// Task-list panel — replaces the chat log; the deterministic plan/checklist for the
// active conversation's most recent turn (statuses from engine events, not the model).
static void drawTaskListPanel(AppState& st, bool busy) {
    (void)busy;
    const ui::AdaptiveSurface& surf = chatSurf();
    const PlanState* ps = nullptr;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (!it->fromUser && planIsReal(it->planState)) {
            ps = &it->planState; break;
        }

    // Subagents are run telemetry, not session artifacts. Keep the exact two sources the
    // Artifacts panel historically used: plan-state subagent rows and agent activity rows.
    std::vector<std::string> subs;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it) {
        if (it->fromUser) continue;
        if (it->planState.present)
            for (const auto& pi : it->planState.items)
                if (pi.kind == "subagent")
                    subs.push_back(pi.title +
                                   (pi.status.empty() ? "" : (" \xc2\xb7 " + pi.status)));
        for (const auto& a : it->activity)
            if (a.stage.rfind("agent:", 0) == 0 || a.title.rfind("Subagent", 0) == 0)
                subs.push_back(a.title.empty() ? a.stage : a.title);
    }

    int done = 0, total = 0;
    copilotTaskProgress(st, done, total);
    char progress[48];
    std::snprintf(progress, sizeof(progress), "%d of %d complete", done, total);
    ui::cpMenuHeader(st.fonts,
                     ps && !ps->title.empty() ? ps->title.c_str() : "Task list",
                     ps ? progress : "");
    if (ps && !ps->summary.empty()) {
        ui::cpMenuHint(ps->summary.c_str());
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
    }
    if (!ps) {
        ui::cpMenuHint("No task list yet \xe2\x80\x94 start a build and the agent's plan appears here.");
    } else {
        // Fixed glyph column: TextWrapped after a SameLine wraps back to the LINE start, which
        // pushed continuation lines under the [x] marker. Indent the title past a MEASURED glyph
        // column instead, so a multi-line task title stays one aligned text block.
        const float glyphW = ImGui::CalcTextSize("[~]").x + ui::metrics::px(6.0f);
        for (const auto& pi : ps->items) {
            if (pi.kind == "subagent") continue;
            const bool ok = (pi.status == "done" || pi.status == "pass");
            const bool run = (pi.status == "running");
            const bool fail = (pi.status == "fail");
            const char* glyph = ok ? "[x]" : run ? "[~]" : fail ? "[!]" : "[ ]";
            ImVec4 gc = surf.textSecondary;
            if (ok) { gc = semanticOnGlass(ui::palette.accent, surf); gc.w = 1.0f; }
            else if (fail) { gc = semanticOnGlass(ui::palette.statusErr, surf); gc.w = 1.0f; }
            const float rowY = ImGui::GetCursorPosY();
            ImGui::PushStyleColor(ImGuiCol_Text, gc);
            ImGui::TextUnformatted(glyph);
            ImGui::PopStyleColor();
            ImGui::SetCursorPosY(rowY);
            ImGui::Indent(glyphW);
            ImGui::PushStyleColor(ImGuiCol_Text, ok ? surf.textSecondary : surf.textPrimary);
            ImGui::TextWrapped("%s", pi.title.empty() ? "(task)" : pi.title.c_str());
            ImGui::PopStyleColor();
            ImGui::Unindent(glyphW);
            ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
        }
    }

    ui::cpMenuSection("Subagents", (int)subs.size());
    if (subs.empty()) {
        ui::cpMenuHint("Spawned helpers and their current status appear here while a run is working.");
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
// Presentation-only helpers for the Artifacts tab (no artifact data/model changes). Every
// size goes through ui::metrics::px so the tab scales with DPI like the rest of the app, and
// every tone comes from the adaptive glass surface (chatSurf) — no hardcoded colors.

// Group header: delegates to the shared copilot-menu section chrome so every artifact section
// carries the same label/count treatment, including an explicit zero count.
static void artiSectionHeader(const ui::AdaptiveSurface& surf, const char* label, int count) {
    (void)surf;
    ui::cpMenuSection(label, count);
}

// Styled empty state: the SHARED menu hint tone (cpMenuHint), indented to the section body, and
// INFORMATIVE (says what will appear here) instead of a bare "(none)".
static void artiEmptyState(const ui::AdaptiveSurface& surf, const char* msg) {
    (void)surf;   // tone now comes from cpMenuHint
    const float ind = ui::metrics::cpMenuRowPadX();
    ImGui::Indent(ind);
    ui::cpMenuHint(msg);
    ImGui::Unindent(ind);
}

// Clip a label to maxW with a real ellipsis, respecting UTF-8 boundaries.
static std::string artiEllipsize(const std::string& s, float maxW) {
    static const char* kEll = "\xe2\x80\xa6";
    if (s.empty() || maxW <= 0.0f) return s;
    if (ImGui::CalcTextSize(s.c_str()).x <= maxW) return s;
    const float ew = ImGui::CalcTextSize(kEll).x;
    std::string out;
    for (size_t i = 0; i < s.size();) {
        size_t n = 1;
        const unsigned char c = (unsigned char)s[i];
        if (c >= 0xF0) n = 4; else if (c >= 0xE0) n = 3; else if (c >= 0xC0) n = 2;
        if (i + n > s.size()) n = s.size() - i;
        std::string cand = out + s.substr(i, n);
        if (ImGui::CalcTextSize(cand.c_str()).x + ew > maxW) break;
        out.swap(cand);
        i += n;
    }
    return out.empty() ? std::string(kEll) : out + kEll;
}

// One tight artifact card: the full row opens the item, while compact actions appear at the right
// only on hover. Both user-supplied lines are draw-list text, so both are ellipsized to the card's
// measured width instead of relying on an ImGui clip rect that does not exist here.
static void artiRow(AppState& st, const std::string& label, const std::string& tooltip,
                    const std::string& path, const std::function<void()>& onOpen) {
    const ui::AdaptiveSurface& surf = chatSurf();
    const float S = ui::uiScale;
    const bool canReveal = !path.empty();
    const float padX = ui::metrics::cpMenuRowPadX();
    const float padY = ui::metrics::cpMenuRowPadY();
    const float lineGap = ui::metrics::cpMenuLabelGap();
    const float rowGap = ui::metrics::cpMenuRowGap();
    const float rowW = ImGui::GetContentRegionAvail().x;
    const float lineH = ImGui::GetTextLineHeight();
    const float cardH = padY * 2.0f + lineH * 2.0f + lineGap;
    const float dotR = ui::metrics::px(2.2f);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + rowW, p0.y + cardH);
    const bool rowHot = ImGui::IsMouseHoveringRect(p0, p1);

    std::string secondary;
    if (path.empty()) {
        secondary = "Live document";
    } else {
        const size_t slash = path.find_last_of("/\\");
        const std::string parent = slash == std::string::npos ? std::string() : path.substr(0, slash);
        const size_t parentSlash = parent.find_last_of("/\\");
        secondary = parent.empty() ? std::string()
                                   : parent.substr(parentSlash == std::string::npos
                                                       ? 0 : parentSlash + 1);
        if (secondary.empty()) {
            const size_t dot = path.find_last_of('.');
            if (dot != std::string::npos && (slash == std::string::npos || dot > slash))
                secondary = path.substr(dot);
        }
        if (secondary.empty()) secondary = "File";
    }

    const ImGuiStyle& style = ImGui::GetStyle();
    const float actionGap = ui::metrics::cpMenuLabelGap();
    const float openW = ImGui::CalcTextSize("Open").x + style.FramePadding.x * 2.0f;
    const float revealW = canReveal
        ? ImGui::CalcTextSize("Folder").x + style.FramePadding.x * 2.0f : 0.0f;
    const float actionsW = rowHot ? openW + (canReveal ? actionGap + revealW : 0.0f) : 0.0f;

    ImGui::Selectable("##artifact_row", false, ImGuiSelectableFlags_AllowItemOverlap,
                      ImVec2(rowW, cardH));
    // The hover actions sit INSIDE the selectable and are submitted after it, so overlap
    // normally hands them the click. Exclude their strip explicitly anyway: the first frame a
    // row becomes hot the buttons were not hovered last frame, and "Folder" would then both
    // reveal the file AND open the artifact.
    const bool overActions =
        actionsW > 0.0f &&
        ImGui::IsMouseHoveringRect(ImVec2(p1.x - padX - actionsW, p0.y),
                                   ImVec2(p1.x - padX, p1.y), /*clip=*/false);
    if (ImGui::IsItemClicked() && !overActions && onOpen) onOpen();

    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::glassRowBackground(dl, p0, p1, ui::metrics::cpMenuRowRound() / S,
                           false, false, rowHot ? 0.42f : 0.30f);
    const ImVec2 dotC(p0.x + padX + dotR, p0.y + cardH * 0.5f);
    dl->AddCircleFilled(dotC, dotR, ImGui::ColorConvertFloat4ToU32(surf.textTertiary));
    const float textX = dotC.x + dotR + ui::metrics::cpMenuLabelGap();
    const float textW = std::max(ui::metrics::px(24.0f),
                                 p1.x - padX - actionsW - textX -
                                     (rowHot ? actionGap : 0.0f));
    dl->AddText(ImVec2(textX, p0.y + padY),
                ImGui::ColorConvertFloat4ToU32(surf.textPrimary),
                artiEllipsize(label, textW).c_str());
    dl->AddText(ImVec2(textX, p0.y + padY + lineH + lineGap),
                ImGui::ColorConvertFloat4ToU32(surf.textTertiary),
                artiEllipsize(secondary, textW).c_str());

    if (rowHot) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip("%s", tooltip.empty() ? (path.empty() ? label.c_str() : path.c_str())
                                               : tooltip.c_str());
        const float buttonY = p0.y + (cardH - ImGui::GetFrameHeight()) * 0.5f;
        float buttonX = p1.x - padX - actionsW;
        ImGui::SetCursorScreenPos(ImVec2(buttonX, buttonY));
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::cpMenuChipRound());
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        if (ImGui::SmallButton("Open") && onOpen) onOpen();
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Open this artifact");
        if (canReveal) {
            buttonX += openW + actionGap;
            ImGui::SetCursorScreenPos(ImVec2(buttonX, buttonY));
            if (ImGui::SmallButton("Folder")) chatArtifactReveal(st, path);
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Show in file manager\n%s", path.c_str());
        }
        ImGui::PopStyleColor();
        ImGui::PopStyleVar();
    }

    ImGui::SetCursorScreenPos(ImVec2(p0.x, p1.y + rowGap));
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

// Session Artifacts is a browser for session output: agent-touched documents, render images and
// produced files. Run telemetry stays in the Tasks panel.
static void drawArtifactsPanel(AppState& st, bool busy) {
    (void)busy;
    const ui::AdaptiveSurface& surf = chatSurf();
    ui::cpMenuHeader(st.fonts, "Session Artifacts",
                     "Documents, renders and files produced or touched by this session.");

    struct ArtiDocRow {
        std::string label, tooltip, path;
        std::function<void()> open;
        bool image = false;
    };
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
    const float actionGap = ui::metrics::cpMenuCtlGap();
    const float actionAvail = ImGui::GetContentRegionAvail().x;
    const float actionW = std::max(ui::metrics::px(24.0f),
                                   (actionAvail - actionGap) * 0.5f);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::cpMenuChipRound());
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::BeginDisabled(projectPath.empty());
    if (ImGui::Button("Open project folder", ImVec2(actionW, 0)))
        chatArtifactReveal(st, projectPath);
    ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("%s", projectPath.empty()
            ? "Open or choose a project before revealing its folder."
            : projectPath.c_str());
    ImGui::SameLine(0, actionGap);
    const char* renderToggle = rendersOn ? "Renders: on" : "Renders: off";
    if (ImGui::Button(renderToggle, ImVec2(actionW, 0))) {
        rendersOn = !rendersOn;
        ui::setChatShowRenders(rendersOn);
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Show or hide image previews here and in the chat transcript.");
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));

    auto lowerCopy = [](std::string s) {
        for (char& c : s) c = (char)std::tolower((unsigned char)c);
        return s;
    };
    static char s_artifactFilter[192] = "";
    const size_t totalRows = agentRows.size() + otherRows.size() + fileRows.size();
    const bool showFilter = totalRows > 8;
    if (showFilter) {
        const ImVec4 fieldBg(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.08f);
        const ImVec4 fieldHov(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.14f);
        const ImVec4 fieldAct(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.18f);
        ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::PushStyleColor(ImGuiCol_FrameBg, fieldBg);
        ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, fieldHov);
        ImGui::PushStyleColor(ImGuiCol_FrameBgActive, fieldAct);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::cpMenuChipRound());
        ImGui::InputTextWithHint("##artifact_filter", "Filter artifacts\xe2\x80\xa6",
                                 s_artifactFilter, sizeof(s_artifactFilter));
        ImGui::PopStyleVar();
        ImGui::PopStyleColor(4);
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap()));
    }
    const bool filterActive = showFilter && s_artifactFilter[0] != '\0';
    const std::string filterNeedle = filterActive ? lowerCopy(s_artifactFilter) : std::string();
    auto matches = [&](const ArtiDocRow& r) {
        if (!filterActive) return true;
        return lowerCopy(r.label).find(filterNeedle) != std::string::npos ||
               lowerCopy(r.path).find(filterNeedle) != std::string::npos;
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

    std::vector<const ArtiDocRow*> models;
    std::vector<const ArtiDocRow*> drawings;
    std::vector<const ArtiDocRow*> data;
    std::vector<const ArtiDocRow*> otherFiles;
    auto lowerExt = [&](const std::string& path) {
        const size_t dot = path.find_last_of('.');
        const size_t slash = path.find_last_of("/\\");
        if (dot == std::string::npos || (slash != std::string::npos && dot < slash))
            return std::string();
        return lowerCopy(path.substr(dot));
    };
    for (auto it = fileRows.rbegin(); it != fileRows.rend(); ++it) {
        if (it->image || !matches(*it)) continue;
        const std::string ext = lowerExt(it->path);
        if (ext == ".step" || ext == ".stp" || ext == ".stl" || ext == ".3mf" ||
            ext == ".obj" || ext == ".step+")
            models.push_back(&*it);
        else if (ext == ".pdf" || ext == ".dxf" || ext == ".svg")
            drawings.push_back(&*it);
        else if (ext == ".csv" || ext == ".json" || ext == ".txt" || ext == ".md")
            data.push_back(&*it);
        else
            otherFiles.push_back(&*it);
    }

    const size_t fileCount = models.size() + drawings.size() + data.size() + otherFiles.size();
    const size_t matchCount = documents.size() + renders.size() + fileCount + otherDocs.size();
    if (filterActive && matchCount == 0)
        artiEmptyState(surf, "No matches \xe2\x80\x94 try a document name, file name or folder.");

    // Section-scoped ImGui ids: every loop below indexes from 0, so without a per-section scope
    // the first file row would collide with the first document row and one row would go dead.
    // ---- Documents ----
    artiSectionHeader(surf, "Documents", (int)documents.size());
    if (documents.empty()) {
        artiEmptyState(surf, filterActive
            ? "No matching documents \xe2\x80\x94 parts the agent opens, imports or edits appear here."
            : "Parts the agent opens, imports or edits appear here.");
    } else {
        ImGui::PushID("arti_docs");
        for (size_t i = 0; i < documents.size(); ++i) {
            ImGui::PushID((int)i);
            artiRow(st, documents[i]->label, documents[i]->tooltip, documents[i]->path,
                    documents[i]->open);
            ImGui::PopID();
        }
        ImGui::PopID();
    }

    // ---- Renders ----
    artiSectionHeader(surf, "Renders", (int)renders.size());
    if (renders.empty()) {
        artiEmptyState(surf, filterActive
            ? "No matching renders \xe2\x80\x94 images produced for agent vision appear here."
            : "Images produced for agent vision appear here as preview tiles.");
    } else if (!rendersOn) {
        artiEmptyState(surf, "Render previews are hidden \xe2\x80\x94 use the Renders control above to show them.");
    } else {
        const float S = ui::uiScale;
        const float availW = ImGui::GetContentRegionAvail().x;
        const float targetW = ui::metrics::copilotThumbW();
        const float cellGap = ui::metrics::cpMenuRowGap();
        const int columns = std::max(1, (int)((availW + cellGap) / (targetW + cellGap)));
        const float cellW = (availW - cellGap * (float)(columns - 1)) / (float)columns;
        const float padX = ui::metrics::cpMenuRowPadX();
        const float padY = ui::metrics::cpMenuRowPadY();
        const float imageW = std::max(ui::metrics::px(24.0f), cellW - padX * 2.0f);
        const float imageH = imageW * 0.72f;
        const float captionH = ImGui::GetTextLineHeight();
        const float cellH = padY * 2.0f + imageH + ui::metrics::cpMenuLabelGap() + captionH;
        const ImVec2 gridStart = ImGui::GetCursorScreenPos();
        const int kCap = 24;
        const size_t drawCount = std::min(renders.size(), (size_t)kCap);
        std::vector<const ArtiDocRow*> fallbackRows;
        size_t gridCount = 0;
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImGui::PushID("arti_renders");
        for (size_t i = 0; i < drawCount; ++i) {
            const ArtiDocRow& r = *renders[i];
            ImGui::PushID((int)i);
            const Tex tex = artiRenderThumb(r.path);
            if (!tex.id || tex.w <= 0 || tex.h <= 0) {
                // Missing, corrupt or half-written images remain visible as dim fallback rows.
                fallbackRows.push_back(&r);
                ImGui::PopID();
                continue;
            }
            const size_t row = gridCount / (size_t)columns;
            const size_t col = gridCount % (size_t)columns;
            const ImVec2 p0(gridStart.x + (float)col * (cellW + cellGap),
                            gridStart.y + (float)row * (cellH + cellGap));
            const ImVec2 p1(p0.x + cellW, p0.y + cellH);
            ImGui::SetCursorScreenPos(p0);
            ImGui::InvisibleButton("##render_cell", ImVec2(cellW, cellH));
            const bool hot = ImGui::IsItemHovered();
            if (ImGui::IsItemClicked()) chatArtifactOpenSmart(st, r.path);
            ui::glassRowBackground(dl, p0, p1, ui::metrics::cpMenuRowRound() / S,
                                   (gridCount % 2) == 1, false, hot ? 0.48f : 0.28f);
            const float scale = std::min(imageW / (float)tex.w, imageH / (float)tex.h);
            const ImVec2 drawSize((float)tex.w * scale, (float)tex.h * scale);
            const ImVec2 imageMin(p0.x + padX + (imageW - drawSize.x) * 0.5f,
                                  p0.y + padY + (imageH - drawSize.y) * 0.5f);
            dl->AddImage((ImTextureID)(intptr_t)tex.id, imageMin,
                         ImVec2(imageMin.x + drawSize.x, imageMin.y + drawSize.y));
            dl->AddText(ImVec2(p0.x + padX,
                               p0.y + padY + imageH + ui::metrics::cpMenuLabelGap()),
                        ImGui::ColorConvertFloat4ToU32(surf.textSecondary),
                        artiEllipsize(r.label, imageW).c_str());
            if (hot) {
                ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
                ImGui::SetTooltip("%s", r.path.c_str());
            }
            ++gridCount;
            ImGui::PopID();
        }
        if (gridCount > 0) {
            const size_t gridRows = (gridCount + (size_t)columns - 1) / (size_t)columns;
            ImGui::SetCursorScreenPos(
                ImVec2(gridStart.x, gridStart.y + (float)gridRows * cellH +
                                        (float)(gridRows - 1) * cellGap + cellGap));
        } else {
            ImGui::SetCursorScreenPos(gridStart);
        }
        ImGui::PushID("fallback");
        for (size_t i = 0; i < fallbackRows.size(); ++i) {
            ImGui::PushID((int)i);
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextWrapped("Preview unavailable \xc2\xb7 %s", fallbackRows[i]->label.c_str());
            ImGui::PopStyleColor();
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", fallbackRows[i]->path.c_str());
            chatArtifactActions(st, fallbackRows[i]->path);
            ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
            ImGui::PopID();
        }
        ImGui::PopID();
        ImGui::PopID();
        const size_t more = renders.size() - drawCount;
        if (more > 0) {
            char mline[64];
            std::snprintf(mline, sizeof(mline), "+%d more render%s",
                          (int)more, more == 1 ? "" : "s");
            artiEmptyState(surf, mline);
        }
    }

    // ---- Files ----
    artiSectionHeader(surf, "Files", (int)fileCount);
    if (fileCount == 0) {
        artiEmptyState(surf, filterActive
            ? "No matching files \xe2\x80\x94 exported models, drawings and data appear here."
            : "Exported models, drawings, data and other files appear here.");
    } else {
        auto drawFileGroup = [&](const char* label, const char* id,
                                 const std::vector<const ArtiDocRow*>& rows) {
            if (rows.empty()) return;
            ui::cpMenuLabel(label);
            ImGui::PushID(id);
            for (size_t i = 0; i < rows.size(); ++i) {
                ImGui::PushID((int)i);
                artiRow(st, rows[i]->label, rows[i]->tooltip, rows[i]->path, rows[i]->open);
                ImGui::PopID();
            }
            ImGui::PopID();
        };
        ImGui::PushID("arti_files");
        drawFileGroup("Models", "models", models);
        drawFileGroup("Drawings / exports", "drawings", drawings);
        drawFileGroup("Data", "data", data);
        drawFileGroup("Other", "other", otherFiles);
        ImGui::PopID();
    }

    // ---- Other open documents ----
    // The user's own tabs must not dominate this panel, but dropping them outright would lose the
    // only way to reach a part the agent worked on BEFORE the handoff / edit-stamp flags were set
    // — so they stay, collapsed behind a dim click-to-expand line.
    artiSectionHeader(surf, "Other open documents", (int)otherDocs.size());
    {
        static bool s_showOtherDocs = false;
        const float ind = ui::metrics::cpMenuRowPadX();
        ImGui::Indent(ind);
        if (otherDocs.empty()) {
            ui::cpMenuHint(filterActive
                ? "No matching open documents \xe2\x80\x94 user-opened tabs are kept here, collapsed."
                : "User-opened tabs appear here, collapsed away from session output.");
        } else {
            char line[96];
            std::snprintf(line, sizeof(line), "%d other open document%s \xc2\xb7 %s",
                          (int)otherDocs.size(), otherDocs.size() == 1 ? "" : "s",
                          s_showOtherDocs ? "hide" : "show");
            ui::cpMenuHint(line);
            if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
            if (ImGui::IsItemClicked()) s_showOtherDocs = !s_showOtherDocs;
        }
        ImGui::Unindent(ind);
        if (s_showOtherDocs && !otherDocs.empty()) {
            ImGui::PushID("arti_other_docs");
            for (size_t i = 0; i < otherDocs.size(); ++i) {
                ImGui::PushID((int)i);
                artiRow(st, otherDocs[i]->label, otherDocs[i]->tooltip, otherDocs[i]->path,
                        otherDocs[i]->open);
                ImGui::PopID();
            }
            ImGui::PopID();
        }
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
}



void drawAIPanel(AppState& st, bool busy, bool canRun) {
    if (!st.showAIPanel) return;

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
        const float colH = std::max(120.0f * ui::uiScale, band.maxY - margin - colY);
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
        // T-G21 shot: mirrors CHUDWARE_FM_COLLAPSED — --ui-screenshot always resets the overlay
        // columns for determinism (see the reset branch above), which clobbers the persisted
        // collapse setting before it can render; force it back for the collapsed capture.
        if (std::getenv("CHUDWARE_COPILOT_COLLAPSED")) {
            s_cpCollapsed = true;
            s_cpAnim.snap(true, colH);  // snap immediately — headless captures must not depend on frame count
        }
        const float maxW = std::max((band.maxX - band.minX) * 0.5f,
                                    ui::metrics::dockRightMin());
        s_paneW = std::min(std::max(s_paneW, ui::metrics::dockRightMin()), maxW);

        // T-G21: vertical collapse — the column rolls UP to its header row (drawCopilotHeader)
        // instead of shrinking to a pill in both axes (mirrors the FeatureManager column; see
        // ui::PanelCollapseAnim). Width is unaffected by collapse.
        const float winH = s_cpAnim.tick(s_cpCollapsed, colH);
        ImGui::SetNextWindowPos(ImVec2(band.maxX - marginX - s_paneW, colY), ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2(s_paneW, winH), ImGuiCond_Always);
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

    // Invisible left-edge resize hit-zone (design rule: no visible splitter bars). Dragging the
    // strip left widens the column; clamped at the top of the next frame.
    if (overlay) {
        const ImVec2 keep = ImGui::GetCursorPos();
        ImGui::SetCursorScreenPos(ImGui::GetWindowPos());
        ImGui::InvisibleButton("##copilot_resize",
                               ImVec2(ui::metrics::px(6.0f), ImGui::GetWindowSize().y));
        if (ImGui::IsItemHovered() || ImGui::IsItemActive())
            ImGui::SetMouseCursor(ImGuiMouseCursor_ResizeEW);
        if (ImGui::IsItemActive()) s_paneW -= ImGui::GetIO().MouseDelta.x;
        ImGui::SetCursorPos(keep);
    }

    // focusSettingsTab opens the Settings modal AI tab (full form + leaderboard).
    // The in-column gear uses showAISettingsOverlay instead.
    if (st.focusSettingsTab) {
        st.openSettings     = true;
        st.openSettingsTab  = 2;
        st.focusSettingsTab = false;
    }

    // Sessions are document-independent and all resident; this only loads history once, keeps
    // st.chatDocKey (run-launch key) current, and guarantees at least one conversation. Must
    // run BEFORE the header/composer touch st.conversations, so the seeded/persisted chat is
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
        drawRuntimeBanner(st);

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
        float reserve = 0.0f;
        if (!overlayOpen) {
            reserve = (s_composerH > 0 ? s_composerH
                                       : ImGui::GetFrameHeightWithSpacing() * 4.0f) +
                      ui::metrics::U(2) + bottomClearance;
        } else {
            reserve = bottomClearance;
        }
        float chatH = ImGui::GetContentRegionAvail().y - reserve;
        if (chatH < ui::metrics::px(120.0f)) chatH = ui::metrics::px(120.0f);

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
        if (st.showAISettingsOverlay) {
            drawAISettingsBody(st, busy);
        } else if (st.showTaskList) {
            drawTaskListPanel(st, busy);
        } else if (st.showArtifacts) {
            drawArtifactsPanel(st, busy);
        } else if (st.showSessionsPicker) {
            drawSessionsPanel(st, busy);
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
        const float  logVisH = ImGui::GetWindowSize().y;
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
        // Floating jump-to-latest arrow over the chat viewport's bottom-right corner (chat
        // view only); hidden while pinned to the tail.
        if (!overlayOpen)
            chatDrawJumpToLatest("copilot.panel", logP0,
                                 ImVec2(logP0.x + logVisW, logP0.y + logVisH));
        ImGui::PopStyleVar();   // per-view WindowPadding
        ImGui::PopStyleColor();

        // Composer is hidden under the header overlays (sessions / settings / tasks / artifacts).
        if (!overlayOpen) {
            ImGui::Dummy(ImVec2(0, ui::metrics::U(1)));
            ImGui::BeginGroup();
            drawComposer(st, busy, canRun);
            ImGui::EndGroup();
            s_composerH = ImGui::GetItemRectSize().y + ui::metrics::U(1);
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
