// Chudware Copilot pane (T-RF6 split of legacy_chat.cpp — this file is the pane host).
//
// A PINNED, full-height, rounded liquid-glass column overlaid on the viewport's right edge with
// ~12dp margins (geometry from ui::viewportBackdrop(); invisible left-edge drag zone to resize;
// the docked task-pane layout is gone — this is the only layout). Top-down: a slim header (outline
// cube + status + Sessions + settings gear), the conversation log with a per-turn transparent
// activity timeline (what the AI is doing / thinking, expandable), and a bottom composer (compact
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

// Aggregate task/plan progress (done/total) from the active conversation's most recent
// assistant turn. DETERMINISTIC: statuses come from engine stage/mission events, not the
// model claiming completion (no cheating). Excludes subagent/objective rows.
static void copilotTaskProgress(AppState& st, int& done, int& total) {
    done = 0;
    total = 0;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it) {
        if (it->fromUser || !it->planState.present || it->planState.items.empty()) continue;
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
        return contentW + padX * 2.0f;
    };
    const float chatW = measureBtn("Chat", "ai_panel");
    const float sessW = measureBtn("Sessions", "history");
    const float gearW = measureBtn("Settings", "settings");
    const float minGap = style.ItemSpacing.x;
    const float rightInset = ui::metrics::U(0.5f);
    // Collapse chip sits on the left, vertically centered between row 1's
    // 0/0 tasks chip and row 2's Chat button. Row content shifts right past
    // it via contentLeftInset; right side keeps the plain rightInset so the
    // block hugs the screen edge.
    const float chevSz  = 26.0f * sc;
    const float leftInset = rightInset;
    const float contentLeftInset = leftInset + chevSz + 6.0f * sc;
    const float colW = ImGui::GetContentRegionAvail().x;

    // ---- Collapse chip (floating window, left edge, between the two rows) ----
    {
        const ImVec2 winP0 = ImGui::GetWindowPos();
        // The header is pulled to window top (drawAIPanel SetCursorPosY(0)), so the
        // true stack is: 6sc top pad + row-1 (wordLh) + row-2 (frame height). Center
        // the chip across exactly that — no content-region/padding offset.
        const float row1H = 6.0f * sc + wordLh;
        const float row2H = std::max(lh, ImGui::GetFrameHeight());
        const float chipY = winP0.y + (row1H + row2H - chevSz) * 0.5f;
        const ImVec2 c0(winP0.x + leftInset, chipY);
        ImGui::SetNextWindowPos(c0, ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2(chevSz, chevSz), ImGuiCond_Always);
        ImGui::SetNextWindowBgAlpha(0.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
        ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 7.0f);
        const ImGuiWindowFlags chipFlags =
            ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoNav |
            ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoResize |
            ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoDocking |
            ImGuiWindowFlags_NoFocusOnAppearing | ImGuiWindowFlags_NoBringToFrontOnFocus |
            ImGuiWindowFlags_NoBackground;
        if (ImGui::Begin("##cp_collapse_chip", nullptr, chipFlags)) {
            const ImVec2 b0 = ImGui::GetCursorScreenPos();
            const bool cClicked = ImGui::InvisibleButton("##cp_collapse", ImVec2(chevSz, chevSz));
            const bool cHover = ImGui::IsItemHovered();
            ImDrawList* wdl = ImGui::GetWindowDrawList();
            const ImVec2 b1(b0.x + chevSz, b0.y + chevSz);
            // Shared collapse-chip recipe (must match FM ##lp_collapse):
            // rest wash 0.32 / hover 0.50, textPrimary glyph, 1.85px stroke.
            ui::glassRowBackground(wdl, b0, b1, 7.0f, false, cHover,
                                   cHover ? 0.50f : 0.32f);
            const ImU32 cCol = ImGui::ColorConvertFloat4ToU32(chatSurf().textPrimary);
            const ImVec2 cc(b0.x + chevSz * 0.5f, b0.y + chevSz * 0.5f);
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
        }
        ImGui::End();
        ImGui::PopStyleVar(3);
    }

    // ---- Row 1: [Tasks] .. centered Chudware .. [Artifacts]  (clears leftInset) ----
    {
        const float logoGap = 3.0f * sc;
        const float cubeSz = std::min(lh, wordLh) * 0.95f;
        if (wordF) ImGui::PushFont(wordF);
        const float wmW = ImGui::CalcTextSize("Chudware").x;
        if (wordF) ImGui::PopFont();
        const float logoW = cubeSz + logoGap + wmW;
        ImGui::Dummy(ImVec2(0, 6.0f * sc));
        const float rowTop1 = ImGui::GetCursorPosY();
        const float bh = lh + 4.0f * sc;
        auto cy1 = [&](float h) { ImGui::SetCursorPosY(rowTop1 + (wordLh - h) * 0.5f); };

        int tlDone = 0, tlTotal = 0;
        copilotTaskProgress(st, tlDone, tlTotal);
        char tlLabel[24];
        std::snprintf(tlLabel, sizeof(tlLabel), "%d/%d", tlDone, tlTotal);
        const float tlW = measureBtn(tlLabel, "plan_board");
        const float artW = measureBtn("Artifacts", "team");

        cy1(bh);
        ImGui::SetCursorPosX(contentLeftInset);
        if (busy) ImGui::BeginDisabled();
        if (drawHeaderTextButton("##hdr_tasks", tlLabel, st.showTaskList, tlW, "plan_board")) {
            const bool open = !st.showTaskList;
            st.showTaskList = open;
            if (open) { st.showArtifacts = st.showSessionsPicker = st.showAISettingsOverlay = false; }
        }
        if (busy) ImGui::EndDisabled();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Tasks \xc2\xb7 %d of %d done \xc2\xb7 click for the task list", tlDone, tlTotal);

        const float* statusCol = pal.accentMid;  const char* statusTip = "Idle";
        if (st.statusKind == 2) { statusCol = pal.statusErr; statusTip = "Error"; }
        else if (busy || st.agentV2Running || activeConversationRunning(st)) {
            statusCol = pal.accent; statusTip = "Running";
        }
        const ImVec4 statusTint(statusCol[0], statusCol[1], statusCol[2], 1.0f);

        float logoLeft = (colW - logoW) * 0.5f;
        logoLeft = std::max(contentLeftInset + tlW + 6.0f * sc, logoLeft);
        logoLeft = std::min(logoLeft, colW - rightInset - artW - logoW - 6.0f * sc);
        ImGui::SameLine(logoLeft);
        {
            IconUV cube = ui::getIcon("context_cube", 20);
            cy1(cubeSz);
            if (cube.valid) {
                ImGui::Image(cube.texId, ImVec2(cubeSz, cubeSz), cube.uv0, cube.uv1, statusTint);
            } else {
                ImGui::Dummy(ImVec2(cubeSz, cubeSz));
            }
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
        if (busy) ImGui::BeginDisabled();
        if (drawHeaderTextButton("##hdr_arts", "Artifacts", st.showArtifacts, artW, "team")) {
            const bool open = !st.showArtifacts;
            st.showArtifacts = open;
            if (open) { st.showTaskList = st.showSessionsPicker = st.showAISettingsOverlay = false; }
        }
        if (busy) ImGui::EndDisabled();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Session Artifacts \xc2\xb7 subagents, plan, documents");
        ImGui::SetCursorPosY(rowTop1 + wordLh);
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
    if (busy) ImGui::BeginDisabled();
    if (drawHeaderTextButton("##hdr_chat", "Chat", chatOpen, chatW, "ai_panel")) {
        st.showSessionsPicker = false;
        st.showAISettingsOverlay = false;
        st.showTaskList = false;
        st.showArtifacts = false;
    }
    if (busy) ImGui::EndDisabled();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Chat — conversation with the agent.");

    centerY(lh);
    ImGui::SetCursorPosX(sessX);
    if (busy) ImGui::BeginDisabled();
    if (drawHeaderTextButton("##hdr_sess", "Sessions", sessionsOpen, sessW, "history")) {
        const bool open = !st.showSessionsPicker;
        st.showSessionsPicker = open;
        if (open) { st.showAISettingsOverlay = st.showTaskList = st.showArtifacts = false; }
    }
    if (busy) ImGui::EndDisabled();
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
}

// Sessions list — replaces the chat log while open so users can jump between sessions easily.
void drawSessionsPanel(AppState& st, bool busy) {
    const float sc = ui::uiScale;
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();

    ensureConversations(st);

    // New-session button spans the full tab width. Return to chat via the header Chat button
    // (Sessions stays labeled "Sessions" — no "< Chat" rename that reflows the header).
    if (busy) ImGui::BeginDisabled();
    if (ImGui::Button("+ New session", ImVec2(-1, 0))) {
        newConversation(st);
        st.prompt[0] = 0;
        st.showAISettingsOverlay = false;
        st.showSessionsPicker = false;  // land in the new chat, not stuck on the list
    }
    if (busy) ImGui::EndDisabled();
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Start a fresh conversation.");

    ImGui::Dummy(ImVec2(0, ui::metrics::U(1)));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::TextWrapped("Running sessions keep working in the background.");
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, ui::metrics::U(1)));

    const ImGuiStyle& style = ImGui::GetStyle();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float padX = 10.0f * sc;
    const float padY = 8.0f * sc;
    const float rowGap = 4.0f * sc;
    const float fh = ImGui::GetFontSize();
    const float titleH = fh;
    const float subH  = fh * 0.92f;
    const float rowH  = padY * 2.0f + titleH + 2.0f * sc + subH;
    const float trashSz = ImGui::GetFrameHeight();

    for (int i = 0; i < (int)st.conversations.size(); ++i) {
        Conversation& c = st.conversations[i];
        std::string title = c.title.empty() ? ("Chat " + std::to_string(i + 1)) : c.title;
        const std::vector<ChatMsg>& msgs = (i == st.activeConv) ? st.chat : c.msgs;
        bool running = false;
        for (auto it = msgs.rbegin(); it != msgs.rend(); ++it)
            if (!it->fromUser && it->running) { running = true; break; }
        if (i == st.activeConv && aiTurnActive(st)) running = true;
        bool active = (i == st.activeConv);

        char sub[96];
        const int nMsg = (int)msgs.size();
        if (nMsg == 0)
            std::snprintf(sub, sizeof(sub), "Empty session");
        else if (nMsg == 1)
            std::snprintf(sub, sizeof(sub), "1 message");
        else
            std::snprintf(sub, sizeof(sub), "%d messages", nMsg);

        ImGui::PushID(i);
        const ImVec2 rowMin = ImGui::GetCursorScreenPos();
        const float  rowW   = ImGui::GetContentRegionAvail().x;
        const ImVec2 rowMax(rowMin.x + rowW, rowMin.y + rowH);

        bool picked = ImGui::InvisibleButton("##pickconv",
                                            ImVec2(std::max(1.0f, rowW - trashSz - padX), rowH));
        bool rowHov = ImGui::IsItemHovered();
        if (picked && !busy) {
            // Always land in chat for the chosen session — including re-clicking
            // the already-active one (switchConversation early-returns on same idx).
            if (i != st.activeConv)
                switchConversation(st, i);
            st.showSessionsPicker = false;
            st.showAISettingsOverlay = false;
        }

        float wash = active ? 0.55f : (rowHov ? 0.44f : 0.34f);
        ui::glassRowBackground(dl, rowMin, rowMax, 8.0f * sc, (i % 2) == 1, active, wash);

        // Scope color-code: once the model has assessed the session (first routing event),
        // tint the row with a 3px left-edge bar + subtle wash + a tiny class chip on the
        // title line. semanticOnGlass keeps the chip ink legible over dark + light glass.
        if (c.scopeClass >= 0) {
            float scopeRgba[4];
            scopeClassColor(c.scopeClass, scopeRgba);
            const ImVec4 scopeCol = semanticOnGlass(scopeRgba, surf);
            const float barW = 3.0f * sc;
            ImVec4 barCol = scopeCol;
            barCol.w = 0.95f;
            dl->AddRectFilled(rowMin, ImVec2(rowMin.x + barW, rowMax.y),
                              ImGui::ColorConvertFloat4ToU32(barCol), 8.0f * sc,
                              ImDrawFlags_RoundCornersLeft);
            ImVec4 washCol = scopeCol;
            washCol.w = active ? 0.10f : 0.07f;
            dl->AddRectFilled(rowMin, rowMax, ImGui::ColorConvertFloat4ToU32(washCol),
                              8.0f * sc);

            const char* scopeLbl = c.scopeClass == 0 ? "trivial" : c.scopeClass == 1 ? "standard"
                                 : c.scopeClass == 2 ? "complex" : "epic";
            const ImVec2 lsz = ImGui::CalcTextSize(scopeLbl);
            // Right-aligned, kept clear of the hover/active trash button zone.
            const float cx1 = rowMax.x - trashSz - padX;
            const float cx0 = cx1 - lsz.x - 8.0f * sc;
            const float cy0 = rowMin.y + padY + (titleH - lsz.y) * 0.5f - 1.0f * sc;
            const float cy1 = cy0 + lsz.y + 2.0f * sc;
            ImVec4 chipBg = scopeCol;
            chipBg.w = 0.16f;
            dl->AddRectFilled(ImVec2(cx0, cy0), ImVec2(cx1, cy1),
                              ImGui::ColorConvertFloat4ToU32(chipBg), 4.0f * sc);
            ImVec4 chipInk = scopeCol;
            chipInk.w = 1.0f;
            dl->AddText(ImVec2(cx0 + 4.0f * sc, cy0 + 1.0f * sc),
                        ImGui::ColorConvertFloat4ToU32(chipInk), scopeLbl);
        }

        const float titleY = rowMin.y + padY;
        ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
        dl->AddText(ImVec2(rowMin.x + padX, titleY), ink, title.c_str());
        if (running) {
            const float tw = ImGui::CalcTextSize(title.c_str()).x;
            const char* badge = "running";
            const ImVec2 bsz = ImGui::CalcTextSize(badge);
            const float bx0 = rowMin.x + padX + tw + 8.0f * sc;
            const float by0 = titleY + (titleH - bsz.y) * 0.5f - 1.0f * sc;
            const float bx1 = bx0 + bsz.x + 8.0f * sc;
            const float by1 = by0 + bsz.y + 2.0f * sc;
            ImVec4 acc = semanticOnGlass(ui::palette.accent, surf);
            acc.w = 0.22f;
            dl->AddRectFilled(ImVec2(bx0, by0), ImVec2(bx1, by1),
                              ImGui::ColorConvertFloat4ToU32(acc), 4.0f * sc);
            ImVec4 accInk = semanticOnGlass(ui::palette.accent, surf);
            accInk.w = 1.0f;
            dl->AddText(ImVec2(bx0 + 4.0f * sc, by0 + 1.0f * sc),
                        ImGui::ColorConvertFloat4ToU32(accInk), badge);
        }

        dl->AddText(ImVec2(rowMin.x + padX, titleY + titleH + 2.0f * sc),
                    ImGui::ColorConvertFloat4ToU32(surf.textSecondary), sub);

        bool doDel = false;
        if (rowHov || active) {
            const float img  = trashSz - 8.0f * sc;
            const float btnW = img + style.FramePadding.x * 2.0f;
            const float btnH = img + style.FramePadding.y * 2.0f;
            ImGui::SetCursorScreenPos(
                ImVec2(rowMax.x - btnW - 4.0f * sc, rowMin.y + (rowH - btnH) * 0.5f));
            IconUV trash = ui::getIcon("delete", 20);
            bool del = trash.valid
                ? ImGui::ImageButton("##delconv", trash.texId, ImVec2(img, img), trash.uv0, trash.uv1)
                : ImGui::SmallButton("x");
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Delete this session");
            doDel = del && !busy;
        }

        ImGui::SetCursorScreenPos(ImVec2(rowMin.x, rowMax.y + rowGap));
        ImGui::PopID();
        if (doDel) { closeConversation(st, i); break; }
    }

    // ---- Other sessions: orphaned keys in chat_history.json (not the active doc) ----
    // Keeps doc:* / other-file histories visible after an autosave rekey or tab switch.
    struct OtherSess {
        std::string key;
        std::string label;
        std::string snippet;
        int nConvs = 0;
    };
    std::vector<OtherSess> others;
    {
        nlohmann::json root = nlohmann::json::object();
        try {
            std::ifstream in(chatHistoryPath());
            if (in) in >> root;
        } catch (...) {
            root = nlohmann::json::object();
        }
        if (root.is_object()) {
            for (auto it = root.begin(); it != root.end(); ++it) {
                if (!it.value().is_object()) continue;
                const std::string& k = it.key();
                if (k == st.chatDocKey) continue;
                OtherSess o;
                o.key = k;
                // Label = key tail (basename for file:, id for doc:, else raw).
                if (k.rfind("file:", 0) == 0) {
                    std::string p = k.substr(5);
                    size_t slash = p.find_last_of("/\\");
                    o.label = slash == std::string::npos ? p : p.substr(slash + 1);
                } else if (k.rfind("doc:", 0) == 0) {
                    o.label = "unsaved " + k.substr(4);
                    if (o.label.size() > 28) o.label = o.label.substr(0, 25) + "...";
                } else {
                    o.label = k;
                }
                const auto& convs = it.value().value("conversations", nlohmann::json::array());
                o.nConvs = (int)convs.size();
                // First user-message snippet across conversations.
                for (const auto& jc : convs) {
                    for (const auto& m : jc.value("msgs", nlohmann::json::array())) {
                        if (m.value("u", false)) {
                            o.snippet = m.value("t", std::string());
                            break;
                        }
                    }
                    if (!o.snippet.empty()) break;
                }
                if (o.snippet.size() > 60) o.snippet = o.snippet.substr(0, 57) + "...";
                // Skip totally empty orphan buckets.
                if (o.nConvs == 0 && o.snippet.empty()) continue;
                others.push_back(std::move(o));
            }
        }
    }
    if (!others.empty()) {
        ImGui::Dummy(ImVec2(0, ui::metrics::U(2)));
        // Header: "Other sessions" + inline Clear all (click-again confirm).
        {
            static double s_clearOtherUntil = 0.0;
            const double now = ImGui::GetTime();
            if (s_clearOtherUntil > 0.0 && now > s_clearOtherUntil) s_clearOtherUntil = 0.0;
            const bool clearPend = s_clearOtherUntil > 0.0;

            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Other sessions");
            ImGui::PopStyleColor();
            ImGui::SameLine();
            {
                const char* clearLbl = clearPend ? "Confirm?" : "Clear all";
                const float clearW = ImGui::CalcTextSize(clearLbl).x
                                     + style.FramePadding.x * 2.0f;
                const float clearX = ImGui::GetContentRegionMax().x - clearW;
                if (clearX > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(clearX);
                ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6.0f * sc, 1.0f * sc));
                if (clearPend) {
                    ImGui::PushStyleColor(ImGuiCol_Button,
                        ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.55f));
                    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
                        ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.70f));
                    ImGui::PushStyleColor(ImGuiCol_ButtonActive,
                        ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.85f));
                    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
                } else {
                    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0, 0, 0, 0));
                    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
                        ImVec4(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.10f));
                    ImGui::PushStyleColor(ImGuiCol_ButtonActive,
                        ImVec4(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.16f));
                    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                }
                if (ImGui::SmallButton(clearLbl)) {
                    if (!clearPend) {
                        s_clearOtherUntil = now + 4.0;
                    } else {
                        std::vector<std::string> drop;
                        drop.reserve(others.size());
                        for (const auto& o : others) drop.push_back(o.key);
                        // If we're browsing one of these keys, bounce back to the live doc first.
                        bool heldDying = false;
                        for (const auto& k : drop)
                            if (!s_chatKeyHold.empty() && k == s_chatKeyHold) { heldDying = true; break; }
                        if (heldDying) {
                            s_chatKeyHold.clear();
                            const std::string live = convDocKey(st);
                            loadChatHistoryForKey(st, live);
                            st.chatDocKey = live;
                        }
                        eraseChatHistoryKeys(drop);
                        s_clearOtherUntil = 0.0;
                        others.clear();
                    }
                }
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("%s",
                        clearPend
                            ? "Click again to permanently remove all other sessions from local history."
                            : "Remove all other-document sessions from local chat history.");
                ImGui::PopStyleColor(4);
                ImGui::PopStyleVar();
            }
        }
        ImGui::Dummy(ImVec2(0, ui::metrics::U(0.5f)));
        for (int oi = 0; oi < (int)others.size(); ++oi) {
            const OtherSess& o = others[oi];
            ImGui::PushID(10000 + oi);
            const ImVec2 rowMin = ImGui::GetCursorScreenPos();
            const float  rowW   = ImGui::GetContentRegionAvail().x;
            const ImVec2 rowMax(rowMin.x + rowW, rowMin.y + rowH);
            bool picked = ImGui::InvisibleButton("##pickother",
                                                ImVec2(std::max(1.0f, rowW - trashSz - padX), rowH));
            bool rowHov = ImGui::IsItemHovered();
            if (picked && !busy) {
                // Read-only browse: load that key's sessions into view and pin so
                // syncChatToDoc doesn't immediately bounce back to the live doc key.
                if (!st.chatDocKey.empty()) persistChatHistory(st);
                loadChatHistoryForKey(st, o.key);
                st.chatDocKey = o.key;
                s_chatKeyHold = o.key;
                st.showSessionsPicker = false;
                st.showAISettingsOverlay = false;
            }
            float wash = rowHov ? 0.42f : 0.22f;
            ui::glassRowBackground(dl, rowMin, rowMax, 8.0f * sc, (oi % 2) == 1, false, wash);
            const float titleY = rowMin.y + padY;
            ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
            dl->AddText(ImVec2(rowMin.x + padX, titleY), ink, o.label.c_str());
            char sub[160];
            if (!o.snippet.empty())
                std::snprintf(sub, sizeof(sub), "%s", o.snippet.c_str());
            else if (o.nConvs == 1)
                std::snprintf(sub, sizeof(sub), "1 session");
            else
                std::snprintf(sub, sizeof(sub), "%d sessions", o.nConvs);
            dl->AddText(ImVec2(rowMin.x + padX, titleY + titleH + 2.0f * sc),
                        ImGui::ColorConvertFloat4ToU32(surf.textSecondary), sub);

            // Per-row delete — same trash affordance as active-doc conversations.
            bool doDel = false;
            if (rowHov) {
                const float img  = trashSz - 8.0f * sc;
                const float btnW = img + style.FramePadding.x * 2.0f;
                const float btnH = img + style.FramePadding.y * 2.0f;
                ImGui::SetCursorScreenPos(
                    ImVec2(rowMax.x - btnW - 4.0f * sc, rowMin.y + (rowH - btnH) * 0.5f));
                IconUV trash = ui::getIcon("delete", 20);
                bool del = trash.valid
                    ? ImGui::ImageButton("##delother", trash.texId, ImVec2(img, img),
                                         trash.uv0, trash.uv1)
                    : ImGui::SmallButton("x");
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("Delete this session from local history");
                doDel = del;
            }

            ImGui::SetCursorScreenPos(ImVec2(rowMin.x, rowMax.y + rowGap));
            ImGui::PopID();
            if (doDel) {
                // If browsing this orphan, return to the live doc before erasing the key.
                if (!s_chatKeyHold.empty() && s_chatKeyHold == o.key) {
                    s_chatKeyHold.clear();
                    const std::string live = convDocKey(st);
                    loadChatHistoryForKey(st, live);
                    st.chatDocKey = live;
                }
                eraseChatHistoryKeys({o.key});
                others.erase(others.begin() + oi);
                break;
            }
        }
    }

    // If we're browsing a held foreign key, offer a one-click return to the live doc.
    if (!s_chatKeyHold.empty()) {
        ImGui::Dummy(ImVec2(0, ui::metrics::U(1)));
        if (ImGui::Button("Back to current document", ImVec2(-1, 0))) {
            if (!st.chatDocKey.empty()) persistChatHistory(st);
            s_chatKeyHold.clear();
            const std::string live = convDocKey(st);
            loadChatHistoryForKey(st, live);
            st.chatDocKey = live;
            st.showSessionsPicker = false;
        }
    }
}

// Task-list panel — replaces the chat log; the deterministic plan/checklist for the
// active conversation's most recent turn (statuses from engine events, not the model).
static void drawTaskListPanel(AppState& st, bool busy) {
    (void)busy;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const PlanState* ps = nullptr;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (!it->fromUser && it->planState.present && !it->planState.items.empty()) {
            ps = &it->planState; break;
        }
    int done = 0, total = 0;
    copilotTaskProgress(st, done, total);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::TextUnformatted(ps && !ps->title.empty() ? ps->title.c_str() : "Task list");
    ImGui::PopStyleColor();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::Text("%d of %d complete", done, total);
    if (ps && !ps->summary.empty()) ImGui::TextWrapped("%s", ps->summary.c_str());
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 4 * sc));
    if (!ps) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("No task list yet — start a build and the agent's plan appears here.");
        ImGui::PopStyleColor();
        return;
    }
    for (const auto& pi : ps->items) {
        if (pi.kind == "subagent") continue;
        const bool ok = (pi.status == "done" || pi.status == "pass");
        const bool run = (pi.status == "running");
        const bool fail = (pi.status == "fail");
        const char* glyph = ok ? "[x]" : run ? "[~]" : fail ? "[!]" : "[ ]";
        ImVec4 gc = surf.textSecondary;
        if (ok) { gc = semanticOnGlass(ui::palette.accent, surf); gc.w = 1.0f; }
        else if (fail) { gc = semanticOnGlass(ui::palette.statusErr, surf); gc.w = 1.0f; }
        ImGui::PushStyleColor(ImGuiCol_Text, gc);
        ImGui::TextUnformatted(glyph);
        ImGui::PopStyleColor();
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text, ok ? surf.textSecondary : surf.textPrimary);
        ImGui::TextWrapped("%s", pi.title.empty() ? "(task)" : pi.title.c_str());
        ImGui::PopStyleColor();
    }
}

// Session Artifacts panel — subagents, the plan, and session documents for this session.
static void drawArtifactsPanel(AppState& st, bool busy) {
    (void)busy;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::TextUnformatted("Session Artifacts");
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 4 * sc));
    bool any = false;
    // Subagents (from plan_state subagent rows + agent.spawn activity).
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::TextUnformatted("Subagents");
    ImGui::PopStyleColor();
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it) {
        if (it->fromUser) continue;
        if (it->planState.present)
            for (const auto& pi : it->planState.items)
                if (pi.kind == "subagent") {
                    any = true;
                    ImGui::BulletText("%s%s", pi.title.c_str(),
                                      pi.status.empty() ? "" : (" \xc2\xb7 " + pi.status).c_str());
                }
        for (const auto& a : it->activity)
            if (a.stage.rfind("agent:", 0) == 0 || a.title.rfind("Subagent", 0) == 0) {
                any = true;
                ImGui::BulletText("%s", a.title.empty() ? a.stage.c_str() : a.title.c_str());
            }
    }
    if (!any) { ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                ImGui::TextWrapped("  (none)"); ImGui::PopStyleColor(); }
    ImGui::Dummy(ImVec2(0, 6 * sc));
    // Plan.
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::TextUnformatted("Plan");
    ImGui::PopStyleColor();
    const PlanState* ps = nullptr;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (!it->fromUser && it->planState.present) { ps = &it->planState; break; }
    if (ps) {
        int d = 0, t = 0; copilotTaskProgress(st, d, t);
        ImGui::BulletText("%s (%d/%d)", ps->title.empty() ? "build plan" : ps->title.c_str(), d, t);
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("  (none)"); ImGui::PopStyleColor();
    }
    ImGui::Dummy(ImVec2(0, 6 * sc));
    // Documents (open doc tabs) + agent-created files from tool_call artifacts.
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::TextUnformatted("Documents");
    ImGui::PopStyleColor();
    bool anyDoc = false;
    if (!st.docTabs.empty()) {
        anyDoc = true;
        for (int ti = 0; ti < (int)st.docTabs.size(); ++ti) {
            const auto& t = st.docTabs[ti];
            ImGui::PushID(t.docId.empty() ? ti : (int)(std::hash<std::string>{}(t.docId) & 0x7fffffff));
            ImGui::Bullet();
            ImGui::SameLine();
            ImGui::TextWrapped("%s", t.title.empty()
                                         ? (t.path.empty() ? t.docId.c_str() : t.path.c_str())
                                         : t.title.c_str());
            if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
            if (ImGui::IsItemClicked()) st.switchToTab(ti);
            // Row actions: (open) fronts the doc in the viewport (switchToTab — the same call
            // the doc-tab strip uses); (folder) reveals the saved file in the OS file manager —
            // only when the doc has an on-disk path (never-saved docs don't). Real SmallButtons
            // so the click can't be swallowed by the wrapped title's item rect.
            ImGui::SameLine(0, 6.0f * sc);
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            if (ImGui::SmallButton("(open)")) st.switchToTab(ti);
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Open in the viewport");
            if (!t.path.empty()) {
                ImGui::SameLine(0, 4.0f * sc);
                if (ImGui::SmallButton("(folder)")) chatArtifactReveal(st, t.path);
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("Show in file manager\n%s", t.path.c_str());
            }
            ImGui::PopStyleColor();
            ImGui::PopID();
        }
    }
    // Agent-created files packed into activity detail after "\x1eART\x1e" (live + restored).
    {
        std::set<std::string> seen;
        for (const auto& m : st.chat) {
            if (m.fromUser) continue;
            for (const auto& a : m.activity) {
                if (a.detail.find("\x1e" "ART\x1e") == std::string::npos) continue;
                const size_t sep = a.detail.find("\x1e" "ART\x1e");
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
                    anyDoc = true;
                    std::string base = path;
                    size_t slash = base.find_last_of("/\\");
                    if (slash != std::string::npos) base = base.substr(slash + 1);
                    ImGui::PushID(path.c_str());
                    ImGui::Bullet();
                    ImGui::SameLine();
                    ImGui::TextWrapped("%s  (%s)", base.c_str(), kindA.c_str());
                    if (ImGui::IsItemHovered()) {
                        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
                        ImGui::SetTooltip("%s", path.c_str());
                    }
                    if (ImGui::IsItemClicked()) chatArtifactOpenSmart(st, path);
                    ImGui::SameLine(0, 6.0f * sc);
                    chatArtifactActions(st, path);
                    ImGui::PopID();
                }
            }
        }
    }
    if (!anyDoc) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("  (none)"); ImGui::PopStyleColor();
    }
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
        const float margin = ui::metrics::px(12.0f);
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
        ImGui::SetNextWindowPos(ImVec2(band.maxX - margin - s_paneW, colY), ImGuiCond_Always);
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

    // Keep the conversation set bound to the active document (swaps history on doc change). Must
    // run BEFORE the header/composer touch st.conversations, so the seeded/persisted chat is
    // adopted rather than cleared.
    syncChatToDoc(st);
    maybePersistChatHistoryDebounced(st, /*force=*/false);

    // T-G17 screenshot hook: CHUDWARE_COPILOT_VIEW forces a Copilot sub-view (and tops up a couple of
    // demo sessions) so the sessions/settings layouts can be captured headless in both themes. Runs
    // AFTER syncChatToDoc (which rebinds st.conversations to the doc) and re-tops-up each frame so
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
            st.showAISettingsOverlay = false;
        } else if (std::strcmp(cpView, "settings") == 0) {
            st.showAISettingsOverlay = true;
            st.showSessionsPicker = false;
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

        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
        // Slim chat-log gutters so bubbles/cards use the column width.
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(3.0f * ui::uiScale, 2.0f * ui::uiScale));
        ImGui::BeginChild("chatlog", ImVec2(0, chatH), ImGuiChildFlags_None);
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
            chatLogStickToBottom();
        }
        const bool  logScrolled = !overlayOpen && ImGui::GetScrollY() > 0.5f;
        const ImVec2 logP0 = ImGui::GetWindowPos();
        const float  logVisW = ImGui::GetWindowSize().x;
        ImGui::EndChild();

        // Top fade mask over the conversation scroll viewport.
        if (logScrolled) {
            const ImVec2 keep = ImGui::GetCursorPos();
            const float fadeH = ImGui::GetTextLineHeight() + ui::metrics::px(6.0f);
            ImGui::SetCursorScreenPos(logP0);
            ImGui::BeginChild("##chatlog_fade", ImVec2(logVisW, fadeH), ImGuiChildFlags_None,
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
        ImGui::PopStyleVar();   // chatlog WindowPadding
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
