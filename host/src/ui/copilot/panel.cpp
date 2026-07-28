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

namespace ui {

// ---- header: logo row + collapse + [Chat L | Sessions C | Settings R] + status ----
// Copilot vertical-collapse state (file scope so the header's collapse chevron and drawAIPanel's
// height animation share it). Mirrors the FeatureManager collapse (T-G21: rolls UP to this header
// strip instead of shrinking to a pill in both axes). Seeded from st.settings.copilotCollapsed on
// drawAIPanel's first call (see s_cpInited there) so a collapsed column stays collapsed across
// restarts.
static bool s_cpCollapsed = false;
// In-column AI-settings overlay (header Settings). Mutually exclusive with showSessionsPicker —
// same swap pattern: hide chat log, fill the body with the compact settings form
// (AppState::showAISettingsOverlay). Chat / Sessions / Settings are three stable labels
// (Sessions no longer renames to "< Chat") — Chat returns to the conversation.

// Compact AI-settings popover body (provider + model + effort + run toggles + "All settings...");
// defined below with the picker helpers, hosted by the header Settings button.

// Clip a label to a pixel width with a real ellipsis, UTF-8 safe (defined with the Artifacts
// presentation helpers below; declared here because the Sessions rows above need it too —
// draw-list text is not clipped by its card, so every user-supplied string gets measured).
static std::string artiEllipsize(const std::string& s, float maxW);

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

    const bool sessionsOpen = st.showSessionsPicker;
    const bool settingsOpen = st.showAISettingsOverlay;
    const bool chatOpen = !sessionsOpen && !settingsOpen && !st.showTaskList && !st.showArtifacts;

    auto measureBtn = [&](const char* label, const char* iconName) -> float {
        const float iconSz = lh * 0.92f;
        const float iconGap = 4.0f * sc;
        IconUV icon = (iconName && iconName[0]) ? ui::getIcon(iconName, 20) : IconUV{};
        const float padX = 6.0f * sc;
        const float tw = ImGui::CalcTextSize(label).x;
        const float contentW = (icon.valid ? iconSz + iconGap : 0.0f) + tw;
        // Common minimum width so row 1's tasks chip and row 2's Chat/Sessions/Settings line up
        // as COLUMNS instead of five ragged pills sized only by their label length.
        return std::max(contentW + padX * 2.0f, ui::metrics::px(58.0f));
    };
    const float chatW = measureBtn("Chat", "ai_panel");
    const float sessW = measureBtn("Sessions", "history");
    const float gearW = measureBtn("Settings", "settings");
    const float minGap = style.ItemSpacing.x;
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
    {
        const float logoGap = 3.0f * sc;
        // The mark is a 32-edge figure, not a 12-edge cube glyph: measured against rendered
        // filmstrips, its nested-cell structure only resolves from ~28 px up, so it is sized off
        // the wordmark's line height rather than clamped under it. Row 1 grows to match (rowH1).
        const float markSz = wordLh * 1.5f;
        if (wordF) ImGui::PushFont(wordF);
        const float wmW = ImGui::CalcTextSize("Chudware").x;
        if (wordF) ImGui::PopFont();
        const float logoW = markSz + logoGap + wmW;
        ImGui::Dummy(ImVec2(0, 6.0f * sc));
        const float rowTop1 = ImGui::GetCursorPosY();
        const float bh = lh + 4.0f * sc;
        // Row 1 is as tall as its tallest occupant — the brand mark — and everything in it
        // (tasks chip, mark, wordmark, artifacts chip) centres on that, not on wordLh alone.
        const float rowH1 = std::max(wordLh, markSz);
        auto cy1 = [&](float h) { ImGui::SetCursorPosY(rowTop1 + (rowH1 - h) * 0.5f); };

        int tlDone = 0, tlTotal = 0;
        copilotTaskProgress(st, tlDone, tlTotal);
        char tlLabel[24];
        std::snprintf(tlLabel, sizeof(tlLabel), "%d/%d", tlDone, tlTotal);
        const float tlW = measureBtn(tlLabel, "plan_board");
        const float artW = measureBtn("Artifacts", "team");

        cy1(bh);
        ImGui::SetCursorPosX(contentLeftInset);
        // NOTE: deliberately NOT gated on `busy` — these are read-only viewer panels
        // (task list / artifacts / chat log / session switcher), not actions that could
        // race a running turn. Users need to check progress while a run is in motion.
        if (drawHeaderTextButton("##hdr_tasks", tlLabel, st.showTaskList, tlW, "plan_board")) {
            const bool open = !st.showTaskList;
            st.showTaskList = open;
            if (open) { st.showArtifacts = st.showSessionsPicker = st.showAISettingsOverlay = false; }
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Tasks \xc2\xb7 %d of %d done \xc2\xb7 click for the task list", tlDone, tlTotal);

        const float* statusCol = pal.accentMid;  const char* statusTip = "Idle";
        if (st.statusKind == 2) { statusCol = pal.statusErr; statusTip = "Error"; }
        // Per-conversation: `busy` is convRunning(activeConv) and activeConversationRunning
        // covers one-shot RPC turns. The global agentV2Running is deliberately NOT OR'd in —
        // it would light this dot for an idle session whenever ANY other session is running.
        else if (busy || activeConversationRunning(st)) {
            statusCol = pal.accent; statusTip = "Running";
        }
        const ImVec4 statusTint(statusCol[0], statusCol[1], statusCol[2], 1.0f);

        float logoLeft = (colW - logoW) * 0.5f;
        logoLeft = std::max(contentLeftInset + tlW + 6.0f * sc, logoLeft);
        logoLeft = std::min(logoLeft, colW - rightInset - artW - logoW - 6.0f * sc);
        ImGui::SameLine(logoLeft);
        {
            // Brand mark: a live 4D tesseract painted straight onto the window draw list. The
            // Dummy reserves EXACTLY the box the old atlas cube occupied, so logoW and the
            // centring clamps above stay valid, and it is the item that carries the tooltip.
            cy1(markSz);
            ImGui::Dummy(ImVec2(markSz, markSz));
            const ImVec2 mkMin = ImGui::GetItemRectMin(), mkMax = ImGui::GetItemRectMax();
            ui::drawTesseract(ImGui::GetWindowDrawList(),
                              ImVec2((mkMin.x + mkMax.x) * 0.5f, (mkMin.y + mkMax.y) * 0.5f),
                              markSz, ImGui::GetTime(),
                              ui::tesseractAnimFromInt(st.settings.brandAnim),
                              ImGui::ColorConvertFloat4ToU32(statusTint));
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", statusTip);
            ImGui::SameLine(0, logoGap);
        }
        if (wordF) ImGui::PushFont(wordF);
        cy1(wordLh);
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
        ImGui::TextUnformatted("Chudware");
        ImGui::PopStyleColor();
        if (wordF) ImGui::PopFont();

        ImGui::SameLine(colW - rightInset - artW);
        cy1(bh);
        if (drawHeaderTextButton("##hdr_arts", "Artifacts", st.showArtifacts, artW, "team")) {
            const bool open = !st.showArtifacts;
            st.showArtifacts = open;
            if (open) { st.showTaskList = st.showSessionsPicker = st.showAISettingsOverlay = false; }
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Session Artifacts \xc2\xb7 subagents, plan, documents");
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
    float chatX = usableL;
    float sessX = usableL + chatW + gapEq;
    float setX  = usableR - gearW;
    sessX = std::max(sessX, chatX + chatW + minGap);
    sessX = std::min(sessX, setX - sessW - minGap);

    centerY(lh);
    ImGui::SetCursorPosX(chatX);
    if (drawHeaderTextButton("##hdr_chat", "Chat", chatOpen, chatW, "ai_panel")) {
        st.showSessionsPicker = false;
        st.showAISettingsOverlay = false;
        st.showTaskList = false;
        st.showArtifacts = false;
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Chat — conversation with the agent.");

    centerY(lh);
    ImGui::SetCursorPosX(sessX);
    if (drawHeaderTextButton("##hdr_sess", "Sessions", sessionsOpen, sessW, "history")) {
        const bool open = !st.showSessionsPicker;
        st.showSessionsPicker = open;
        if (open) { st.showAISettingsOverlay = st.showTaskList = st.showArtifacts = false; }
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip(sessionsOpen ? "Sessions list is open."
                                       : "Sessions — switch between chats.");

    centerY(lh);
    ImGui::SetCursorPosX(setX);
    if (drawHeaderTextButton("##hdr_gear", "Settings", settingsOpen, gearW, "settings")) {
        const bool open = !st.showAISettingsOverlay;
        st.showAISettingsOverlay = open;
        if (open) { st.showSessionsPicker = st.showTaskList = st.showArtifacts = false; }
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip(settingsOpen ? "Settings is open."
                                       : "AI settings — provider, model, privacy.");

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
        st.showAISettingsOverlay = false;
        st.showSessionsPicker = false;  // land in the new chat, not stuck on the list
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
        const float actionGap = 4.0f * sc;
        // Action-zone geometry must be computed ONCE and shared by the title clip and the
        // buttons below: the reserve used to assume the trash button was trashSz wide, but
        // the drawn ImageButton is img + FramePadding*2 (wider) — the Archive button slid
        // left over the title text on active/hovered rows.
        const float delImg  = trashSz - 8.0f * sc;
        const float delBtnW = delImg + style.FramePadding.x * 2.0f;
        const float delBtnH = delImg + style.FramePadding.y * 2.0f;
        const float rightMargin = 4.0f * sc;
        const float actionsW = delBtnW + archBtnW + actionGap + rightMargin;

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
            st.showSessionsPicker = false;
            st.showAISettingsOverlay = false;
        }

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
        const std::string titleShown = artiEllipsize(title, textMaxW);
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
            badge = artiEllipsize(badge, std::max(ui::metrics::px(24.0f), rowW * 0.5f - padX));
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
                    artiEllipsize(sub, subMaxW).c_str());

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
        }

        ImGui::SetCursorScreenPos(ImVec2(rowMin.x, rowMax.y + rowGap));
        ImGui::PopID();
        if (doDel) { closeConversation(st, i); break; }
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
        return;
    }
    // Fixed glyph column: TextWrapped after a SameLine wraps back to the LINE start, which pushed
    // continuation lines under the [x] marker. Indent the title past a MEASURED glyph column
    // instead, so a multi-line task title stays one aligned text block.
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
        ImGui::SetCursorPosY(rowY);            // back onto the glyph's own line
        ImGui::Indent(glyphW);
        ImGui::PushStyleColor(ImGuiCol_Text, ok ? surf.textSecondary : surf.textPrimary);
        ImGui::TextWrapped("%s", pi.title.empty() ? "(task)" : pi.title.c_str());
        ImGui::PopStyleColor();
        ImGui::Unindent(glyphW);
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
    }
}

// ============================ Session Artifacts presentation ============================
// Presentation-only helpers for the Artifacts tab (no artifact data/model changes). Every
// size goes through ui::metrics::px so the tab scales with DPI like the rest of the app, and
// every tone comes from the adaptive glass surface (chatSurf) — no hardcoded colors.

// Group header: delegates to the SHARED copilot-menu section chrome so an Artifacts group and the
// Sessions "Other sessions" group are the same widget. This tab's call sites pass 0 to mean "no
// count" (the Plan group), which maps onto cpMenuSection's -1 sentinel.
static void artiSectionHeader(const ui::AdaptiveSurface& surf, const char* label, int count) {
    (void)surf;   // tone now comes from cpMenuSection's own chatSurf() lookup
    ui::cpMenuSection(label, count > 0 ? count : -1);
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

// One artifact row: a single grouped CARD (frosted background) per document — the name on
// its own line up top, and its actions ("Open file" / "Open file location") stacked directly
// beneath it inside the SAME card background, so each row visibly reads as one unit instead of
// a name-left/buttons-right strip (the old layout put "Folder" so far from the name it read as
// a separate, unrelated control).
static void artiRow(AppState& st, const std::string& label, const std::string& tooltip,
                    const std::string& path, const std::function<void()>& onOpen) {
    const ui::AdaptiveSurface& surf = chatSurf();
    const float S = ui::uiScale;
    const bool hasFolder = !path.empty();

    const float padX  = ui::metrics::cpMenuRowPadX();
    const float padY  = ui::metrics::cpMenuRowPadY();
    const float rnd   = ui::metrics::cpMenuRowRound();
    const float rowW  = ImGui::GetContentRegionAvail().x;
    const float nameH = ImGui::GetTextLineHeight();
    const float btnH  = ImGui::GetFrameHeight();
    const float innerGap = ui::metrics::px(4.0f);
    const float cardH = padY * 2.0f + nameH + innerGap + btnH;

    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::glassRowBackground(dl, p0, ImVec2(p0.x + rowW, p0.y + cardH), rnd / S,
                           /*stripe*/false, /*selected*/false, /*washAlpha*/0.30f);

    // -- name line: a small dot marker (drawn directly, NOT ImGui::Bullet() — its trailing
    // spacing is easy to accidentally cancel and clip into the label) + ellipsized name --
    const float dotR = ui::metrics::px(2.2f);
    const float nameX = p0.x + padX + dotR * 2.0f + ui::metrics::px(6.0f);
    const ImVec2 dotC(p0.x + padX + dotR, p0.y + padY + nameH * 0.5f);
    dl->AddCircleFilled(dotC, dotR, ImGui::ColorConvertFloat4ToU32(surf.textTertiary));

    ImGui::SetCursorScreenPos(ImVec2(nameX, p0.y + padY));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    const float nameMaxW = rowW - (nameX - p0.x) - padX;
    ImGui::TextUnformatted(artiEllipsize(label, nameMaxW).c_str());
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (!tooltip.empty()) ImGui::SetTooltip("%s", tooltip.c_str());
    }
    if (ImGui::IsItemClicked() && onOpen) onOpen();

    // -- action row: grouped directly under the name, inside the same card --
    ImGui::SetCursorScreenPos(ImVec2(p0.x + padX, p0.y + padY + nameH + innerGap));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    if (ImGui::SmallButton("Open file")) { if (onOpen) onOpen(); }
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Open in the viewport");
    if (hasFolder) {
        ImGui::SameLine(0, ui::metrics::px(6.0f));
        if (ImGui::SmallButton("Open file location")) chatArtifactReveal(st, path);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Show in file manager\n%s", path.c_str());
    }
    ImGui::PopStyleColor();

    // Advance past the card + a small gap before the next one.
    ImGui::SetCursorScreenPos(ImVec2(p0.x, p0.y + cardH));
    ImGui::Dummy(ImVec2(rowW, 0));
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuRowGap()));
}

// Session Artifacts panel — subagents, the plan, and session documents for this session.
static void drawArtifactsPanel(AppState& st, bool busy) {
    (void)busy;
    const ui::AdaptiveSurface& surf = chatSurf();
    ui::cpMenuHeader(st.fonts, "Session Artifacts",
                     "Subagents, plan and documents for this session.");

    // ---- Subagents (from plan_state subagent rows + agent.spawn activity) ----
    // Collected first so the section header can carry a count; the data selection is unchanged.
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
    artiSectionHeader(surf, "Subagents", (int)subs.size());
    if (subs.empty()) {
        artiEmptyState(surf, "No subagents yet \xe2\x80\x94 spawned helpers appear here while a run is working.");
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        for (const auto& s : subs) {
            // Bullet() already appends its own trailing spacing; forcing SameLine(0,0)
            // after it collapses that gap and the dot visually clips into the label
            // (T-artifacts-bullet-clip). Use the default gap instead.
            ImGui::Bullet();
            ImGui::SameLine();
            const std::string shown = artiEllipsize(s, ImGui::GetContentRegionAvail().x);
            ImGui::TextUnformatted(shown.c_str());
            if (shown.size() != s.size() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", s.c_str());
        }
        ImGui::PopStyleColor();
    }

    // ---- Plan ----
    artiSectionHeader(surf, "Plan", 0);
    const PlanState* ps = nullptr;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (!it->fromUser && planIsReal(it->planState)) { ps = &it->planState; break; }
    if (ps) {
        int d = 0, t = 0; copilotTaskProgress(st, d, t);
        char line[320];
        snprintf(line, sizeof(line), "%s (%d/%d)",
                 ps->title.empty() ? "build plan" : ps->title.c_str(), d, t);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::Bullet();
        ImGui::SameLine();
        const std::string shown = artiEllipsize(line, ImGui::GetContentRegionAvail().x);
        ImGui::TextUnformatted(shown.c_str());
        ImGui::PopStyleColor();
        if (shown.size() != strlen(line) && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", line);
    } else {
        artiEmptyState(surf, "No plan yet \xe2\x80\x94 the agent's task list shows up here once a build starts.");
    }

    // ---- Open documents (deduplicated doc tabs) + files produced this session ----
    // Rows are gathered before drawing so each header can show a count; same sources as before.
    struct ArtiDocRow {
        std::string label, tooltip, path;
        std::function<void()> open;
    };
    std::vector<ArtiDocRow> docRows;   // open documents (one row per underlying doc)
    std::vector<ArtiDocRow> fileRows;  // files the agent wrote this session
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
            docRows.push_back(std::move(r));
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
                    r.label   = base + "  (" + kindA + ")";
                    r.path    = path;
                    r.tooltip = path;
                    r.open    = [&st, path]() { chatArtifactOpenSmart(st, path); };
                    fileRows.push_back(std::move(r));
                }
            }
        }
    }
    // Section-scoped ImGui ids: both loops index from 0, so without a per-section scope
    // the first produced-file row would collide with the first open-document row and one
    // of the two rows' buttons would go dead.
    artiSectionHeader(surf, "Open Documents", (int)docRows.size());
    if (docRows.empty()) {
        artiEmptyState(surf, "No open documents \xe2\x80\x94 parts you open are listed here.");
    } else {
        ImGui::PushID("arti_docs");
        for (size_t i = 0; i < docRows.size(); ++i) {
            ImGui::PushID((int)i);
            artiRow(st, docRows[i].label, docRows[i].tooltip, docRows[i].path, docRows[i].open);
            ImGui::PopID();
        }
        ImGui::PopID();
    }
    artiSectionHeader(surf, "Produced This Session", (int)fileRows.size());
    if (fileRows.empty()) {
        artiEmptyState(surf, "No files yet \xe2\x80\x94 files the agent writes show up here.");
    } else {
        ImGui::PushID("arti_files");
        for (size_t i = 0; i < fileRows.size(); ++i) {
            ImGui::PushID((int)i);
            artiRow(st, fileRows[i].label, fileRows[i].tooltip, fileRows[i].path, fileRows[i].open);
            ImGui::PopID();
        }
        ImGui::PopID();
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::px(6.0f)));
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
        if (std::strcmp(cpView, "sessions") == 0) {
            st.showSessionsPicker = true;
            st.showAISettingsOverlay = st.showTaskList = st.showArtifacts = false;
        } else if (std::strcmp(cpView, "settings") == 0) {
            st.showAISettingsOverlay = true;
            st.showSessionsPicker = st.showTaskList = st.showArtifacts = false;
        } else if (std::strcmp(cpView, "tasks") == 0) {
            st.showTaskList = true;
            st.showSessionsPicker = st.showAISettingsOverlay = st.showArtifacts = false;
        } else if (std::strcmp(cpView, "artifacts") == 0) {
            st.showArtifacts = true;
            st.showSessionsPicker = st.showAISettingsOverlay = st.showTaskList = false;
        }
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
        // others) and a click outside the Copilot column dismisses them.
        const bool overlayOpen = st.showSessionsPicker || st.showAISettingsOverlay ||
                                 st.showTaskList || st.showArtifacts;
        if (overlayOpen && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
            !ImGui::IsWindowHovered(ImGuiHoveredFlags_ChildWindows)) {
            st.showSessionsPicker = st.showAISettingsOverlay =
                st.showTaskList = st.showArtifacts = false;
        }
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
        // These are string literals — the SAME object every frame — so a pointer compare IS the
        // "view changed this frame" test, with no per-frame strcmp.
        static const char* s_lastViewId = nullptr;
        const bool viewChanged = (s_lastViewId != viewId);
        s_lastViewId = viewId;

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
        // Never open a menu mid-scroll. SetNextWindowScroll (not SetScrollY) so the reset lands on
        // THIS frame instead of flashing one frame at the previous view's offset; -1 leaves X alone.
        if (viewChanged) ImGui::SetNextWindowScroll(ImVec2(-1.0f, 0.0f));
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
