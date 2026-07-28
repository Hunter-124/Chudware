// Copilot chat log (T-RF6 split of legacy_chat.cpp): message-list rendering — the user block
// (bubble + rewind/edit row), the provider log, the assistant turn (prose + activity timeline
// + cards), and the windowed log (height-cached spacer rows for off-screen turns) with
// stick-to-bottom autoscroll. Card/timeline bodies live in cards.cpp / activity_timeline.cpp.

#include "copilot_internal.h"

namespace ui {

// ---- message age helpers ------------------------------------------------------
// Relative age for the in-bubble meta row ("3m ago"), switching to a bare date
// once the message is over a day old. The hover tooltip carries the concise
// absolute time ("4:37 PM"), adding the date when it's over a day old.
static bool chatLocalTime(double ts, std::tm& out) {
    if (ts <= 0.0) return false;
    const std::time_t t = (std::time_t)ts;
#ifdef _WIN32
    return localtime_s(&out, &t) == 0;
#else
    return localtime_r(&t, &out) != nullptr;
#endif
}

static std::string chatAgeLabel(double ts) {
    if (ts <= 0.0) return std::string();
    static const char* kMonths[] = {"Jan","Feb","Mar","Apr","May","Jun",
                                    "Jul","Aug","Sep","Oct","Nov","Dec"};
    const double age = chatNowEpoch() - ts;
    char buf[40];
    if (age < 45.0) return "now";
    if (age < 3600.0) {
        int mins = (int)(age / 60.0); if (mins < 1) mins = 1;
        std::snprintf(buf, sizeof(buf), "%dm ago", mins);
        return buf;
    }
    if (age < 86400.0) {
        std::snprintf(buf, sizeof(buf), "%dh ago", (int)(age / 3600.0));
        return buf;
    }
    std::tm tmv; if (!chatLocalTime(ts, tmv)) return std::string();
    std::tm nowTm; chatLocalTime(chatNowEpoch(), nowTm);
    if (tmv.tm_year == nowTm.tm_year)
        std::snprintf(buf, sizeof(buf), "%s %d", kMonths[tmv.tm_mon], tmv.tm_mday);
    else
        std::snprintf(buf, sizeof(buf), "%s %d, %d", kMonths[tmv.tm_mon], tmv.tm_mday,
                      tmv.tm_year + 1900);
    return buf;
}

static std::string chatTimeTooltip(double ts) {
    std::tm tmv; if (!chatLocalTime(ts, tmv)) return std::string();
    static const char* kMonths[] = {"Jan","Feb","Mar","Apr","May","Jun",
                                    "Jul","Aug","Sep","Oct","Nov","Dec"};
    std::tm nowTm; chatLocalTime(chatNowEpoch(), nowTm);
    int h = tmv.tm_hour % 12; if (h == 0) h = 12;
    char clock[24];
    std::snprintf(clock, sizeof(clock), "%d:%02d %s", h, tmv.tm_min,
                  tmv.tm_hour < 12 ? "AM" : "PM");
    char buf[64];
    const bool sameDay = tmv.tm_year == nowTm.tm_year && tmv.tm_yday == nowTm.tm_yday;
    if (sameDay)
        std::snprintf(buf, sizeof(buf), "%s", clock);
    else if (tmv.tm_year == nowTm.tm_year)
        std::snprintf(buf, sizeof(buf), "%s %d, %s", kMonths[tmv.tm_mon], tmv.tm_mday, clock);
    else
        std::snprintf(buf, sizeof(buf), "%s %d, %d, %s", kMonths[tmv.tm_mon], tmv.tm_mday,
                      tmv.tm_year + 1900, clock);
    return buf;
}

// In-bubble meta row: author name (left) + relative age (right; hover = absolute
// time). Renders as a bubble's first (assistant) or last (user) row so the author
// lives INSIDE the message box. Name may be null (age-only row).
static void drawMsgMetaRow(const ui::Fonts& fonts, const char* name, const ImVec4& nameCol,
                           double ts) {
    const ui::AdaptiveSurface& surf = chatSurf();
    const std::string age = chatAgeLabel(ts);
    if ((!name || !name[0]) && age.empty()) return;   // legacy turn, no model — no blank row
    if (name && name[0]) {
        ImGui::PushFont(fonts.medium);
        ImGui::TextColored(nameCol, "%s", name);
        ImGui::PopFont();
    } else {
        ImGui::Dummy(ImVec2(1.0f, ImGui::GetTextLineHeight()));
    }
    if (!age.empty()) {
        const float ageW = ImGui::CalcTextSize(age.c_str()).x;
        // Right inset clears the hover "copy" glyph that owns the bubble's top-right corner
        // (14px glyph + 4px pad, chatHoverCopyGlyph) plus a small breathing gap.
        const float rightInset = (14.0f + 4.0f + 6.0f) * ui::uiScale;
        const float rx = ImGui::GetWindowContentRegionMax().x - ageW - rightInset;
        ImGui::SameLine();
        ImGui::SetCursorPosX(std::max(ImGui::GetCursorPosX(), rx));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(age.c_str());
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) {
            const std::string tip = chatTimeTooltip(ts);
            if (!tip.empty()) ImGui::SetTooltip("%s", tip.c_str());
        }
    }
}

void drawUserBlock(AppState& st, const ChatMsg& m, const ui::Fonts& fonts,
                          float panelW, int msgIdx) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ImVec4 accentCol = semanticOnGlass(pal.accent, chatSurf());
    // Right-align: indent ~22% of the panel so the bubble sits against the right edge.
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + panelW * 0.22f);
    const float bw = ImGui::GetContentRegionAvail().x;
    GlassBubble gb = beginGlassBubble(bw, ui::GlassLevel::Regular, 10.0f);
    // Every chat bubble in the log shares ONE inset (cpChatCardPadX/Y). Before this the user
    // bubble ran 10/5, the prose card 10/7 and every other card inherited the pane's tight
    // 3/2 — three different insets stacked down one column, which is what made the log read
    // as unrelated widgets rather than one surface.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##umsg", ImVec2(bw, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    // Zero the default 9px inter-item spacing for this bubble's body -> meta bar
    // stack: without it, ImGui inserts its own gap before AND after the Dummy
    // below on top of the explicit one, doubling the visual gap the redpen flagged.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(ImGui::GetStyle().ItemSpacing.x, 0.0f));
    // Body, then ONE meta bar INSIDE the bubble bottom: "You" (left) + AI-VC
    // rewind/edit buttons + age (right; hover for the absolute time).
    // Same selectable read-only InputText treatment as assistant prose (soft-wrap +
    // click-drag + Ctrl+C toast + right-click Copy).
    const std::string userBody = stripMdMarks(m.text);
    const ImGuiID textId = chatDrawSelectableText("##umsg_sel", userBody, chatSurf().textPrimary);
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));

    // ---- meta bar: You [rewind edit] ......... age ----
    const double now = ImGui::GetTime();
    if (s_aiVc.confirmMsgIdx >= 0 && now > s_aiVc.confirmUntil) {
        s_aiVc.confirmMsgIdx = -1;
        s_aiVc.confirmKind = 0;
    }
    const bool pending = (s_aiVc.confirmMsgIdx == msgIdx && s_aiVc.confirmKind == 3);
    const bool showBtns = s_aiVc.available && msgIdx >= 0 && !s_aiVc.busy;
    // Checkpoint coverage decides whether this rewind rolls geometry back too — the
    // tooltip (before) and the toast (after) must never imply a geometry restore when
    // no checkpoint exists for this message.
    const int cpState = showBtns ? aiVcMsgCheckpointState(st, msgIdx) : 2;
    const float btnSz = ImGui::GetFrameHeight() * 0.78f;
    const float gap = 4.0f * sc;
    const float lineH = ImGui::GetTextLineHeight();
    const float rowH = showBtns ? std::max(lineH, btnSz) : lineH;
    const float textOff = (rowH - lineH) * 0.5f;
    const float rowY = ImGui::GetCursorPosY();
    const ui::AdaptiveSurface& surf = chatSurf();
    ImDrawList* dl = ImGui::GetWindowDrawList();

    ImGui::SetCursorPosY(rowY + textOff);
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(accentCol, "You");
    ImGui::PopFont();

    if (showBtns) {
        float bx = (ImGui::GetItemRectMax().x - ImGui::GetWindowPos().x) + 8.0f * sc;
        // Rewind glyph button
        ImGui::SetCursorPos(ImVec2(bx, rowY));
        {
            if (pending) {
                ImGui::PushStyleColor(ImGuiCol_Button,
                    ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.55f));
            }
            const bool clicked = ImGui::InvisibleButton("##umsg_rw", ImVec2(btnSz, btnSz));
            const ImVec2 bmn = ImGui::GetItemRectMin(), bmx = ImGui::GetItemRectMax();
            if (pending || ImGui::IsItemHovered())
                dl->AddRectFilled(bmn, bmx,
                    ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textPrimary.x, surf.textPrimary.y,
                                                          surf.textPrimary.z, pending ? 0.18f : 0.10f)),
                    ui::metrics::rounding(4.0f));
            {
                // Inset to match the pencil icon's 0.18 pad -- the raw glyph
                // otherwise reads noticeably bigger/bolder than the pencil.
                const float rp = btnSz * 0.18f;
                drawDoubleRewindGlyph(dl, ImVec2(bmn.x + rp, bmn.y + rp),
                                     ImVec2(bmx.x - rp, bmx.y - rp),
                                     ImGui::ColorConvertFloat4ToU32(pending ? surf.textPrimary : surf.textSecondary));
            }
            if (pending) ImGui::PopStyleColor();
            if (clicked) {
                if (!pending) {
                    s_aiVc.confirmMsgIdx = msgIdx;
                    s_aiVc.confirmKind = 3;
                    s_aiVc.confirmUntil = now + 4.0;
                } else {
                    aiVcRequestRollback(st, "msg:" + std::to_string(msgIdx),
                                        aiVcRewindToast(cpState));
                }
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", pending ? "Click again to confirm rewind."
                                                : aiVcRewindTip(cpState));
        }

        // Pencil edit — atlas icon (msg_edit) with filled-glyph fallback.
        ImGui::SameLine(0, gap);
        {
            const bool clicked = ImGui::InvisibleButton("##umsg_edit", ImVec2(btnSz, btnSz));
            const ImVec2 bmn = ImGui::GetItemRectMin(), bmx = ImGui::GetItemRectMax();
            if (ImGui::IsItemHovered())
                dl->AddRectFilled(bmn, bmx,
                    ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textPrimary.x, surf.textPrimary.y,
                                                          surf.textPrimary.z, 0.10f)),
                    ui::metrics::rounding(4.0f));
            const ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textSecondary);
            IconUV editIcon = ui::getIcon("msg_edit", 20);
            if (editIcon.valid) {
                const float pad = btnSz * 0.18f;
                dl->AddImage(editIcon.texId,
                             ImVec2(bmn.x + pad, bmn.y + pad),
                             ImVec2(bmx.x - pad, bmx.y - pad),
                             editIcon.uv0, editIcon.uv1,
                             ink);
            } else {
                drawPencilGlyph(dl, bmn, bmx, ink);
            }
            if (clicked) beginEditStage(st, msgIdx);
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Edit this message — Esc cancels without changing the part.");
        }
    }

    // Age at the bar's right edge (hover = absolute time).
    const std::string age = chatAgeLabel(m.ts);
    if (!age.empty()) {
        const float ageW = ImGui::CalcTextSize(age.c_str()).x;
        const float rx = ImGui::GetWindowContentRegionMax().x - ageW;
        ImGui::SetCursorPos(ImVec2(std::max(ImGui::GetCursorPosX(), rx), rowY + textOff));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(age.c_str());
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) {
            const std::string tip = chatTimeTooltip(m.ts);
            if (!tip.empty()) ImGui::SetTooltip("%s", tip.c_str());
        }
    }
    // Guarantee the cursor advances past the bar even when the age was left of the buttons.
    ImGui::SetCursorPosY(rowY + rowH);
    ImGui::Dummy(ImVec2(1.0f, 0.0f));
    ImGui::PopStyleVar();   // ItemSpacing -- pushed after BeginChild, must pop before EndChild
    // Hover-copy glyph MUST be submitted while "##umsg" is still the current window: ImGui's
    // hovered-window resolution grants overlapping screen space to the (deeper) child window,
    // so an InvisibleButton added to the PARENT after EndChild silently loses every click here
    // to the InputTextMultiline underneath — the copy button drew fine but never fired.
    {
        const ImVec2 wp = ImGui::GetWindowPos();
        const ImVec2 ws = ImGui::GetWindowSize();
        chatHoverCopyGlyph(ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)), userBody);
    }
    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##umsg WindowPadding -- pushed before BeginChild, pops after
    ImRect bubble = endGlassBubble(gb);
    {
        ImU32 border = modeBorderColor(m.copilotMode);
        if (border == 0) {
            // Unstamped: keep legacy accent border.
            border = ImGui::ColorConvertFloat4ToU32(
                ImVec4(accentCol.x, accentCol.y, accentCol.z, 0.55f));
        }
        ImGui::GetWindowDrawList()->AddRect(bubble.Min, bubble.Max, border,
            ui::metrics::rounding(10.0f), 0, ui::metrics::stroke(1.0f));
    }
    const std::string sel = chatInputSelection(textId);
    chatCopyContext(bubble, "##umsg_copy", userBody, userBody, sel);

    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatTurnGap()));   // gap to the next TURN
}


// Collapsible "Provider log" — aggregates rowKind=9 provider_call events into a compact
// monospace-ish list (one line per HTTP attempt). Always expanded when the turn failed;
// collapsed behind a small toggle on success. Right-click copies the full log.
static void drawProviderLog(ChatMsg& m, const ui::Fonts& fonts) {
    std::vector<ActivityItem*> calls;
    calls.reserve(8);
    bool anyFail = false;
    for (auto& a : m.activity) {
        if (a.rowKind != 9) continue;
        calls.push_back(&a);
        if (a.status == "fail" || a.status == "error") anyFail = true;
    }
    if (calls.empty()) return;

    const bool turnFailed = (m.errKind != ERR_NONE) || anyFail;
    // Persist toggle on the first call row; force-open on failure so xai/claude debug is visible.
    bool& open = calls.front()->expanded;
    if (turnFailed) open = true;

    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const auto& pal = ui::palette;

    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    {
        char hdr[64];
        std::snprintf(hdr, sizeof(hdr), "Provider log (%d)", (int)calls.size());
        // Vector-drawn chevron — the ▼/▶ text glyphs are missing from the UI font and rendered
        // as a literal "?". Same hand-drawn-triangle recipe as the FM section dividers.
        const float box = ImGui::GetTextLineHeight();
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImGui::Dummy(ImVec2(box * 0.8f, box));
        {
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textTertiary);
            const float cx = p0.x + box * 0.4f, cy = p0.y + box * 0.5f;
            const float aa = box * 0.20f;
            if (open)
                dl->AddTriangleFilled(ImVec2(cx - aa, cy - aa * 0.6f),
                                      ImVec2(cx + aa, cy - aa * 0.6f),
                                      ImVec2(cx, cy + aa * 0.7f), ink);
            else
                dl->AddTriangleFilled(ImVec2(cx - aa * 0.6f, cy - aa),
                                      ImVec2(cx - aa * 0.6f, cy + aa),
                                      ImVec2(cx + aa * 0.7f, cy), ink);
        }
        ImGui::SameLine(0, 2.0f * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::PushFont(fonts.medium ? fonts.medium : fonts.body);
        ImGui::TextUnformatted(hdr);
        ImGui::PopFont();
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked() && !turnFailed) open = !open;
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(turnFailed
                                  ? "Provider HTTP attempts for this run (expanded on failure)."
                                  : "Toggle provider HTTP call log.");
    }
    if (!open) return;

    // Build full copy text once.
    std::string allCopy;
    allCopy.reserve(calls.size() * 96);
    for (const ActivityItem* a : calls) {
        if (!allCopy.empty()) allCopy += "\n";
        allCopy += !a->detail.empty() ? a->detail
                 : (!a->title.empty() ? a->title + "  " + a->reason : a->reason);
    }

    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##provlog", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    if (fonts.mono) ImGui::PushFont(fonts.mono);
    for (size_t i = 0; i < calls.size(); ++i) {
        const ActivityItem* a = calls[i];
        const bool fail = (a->status == "fail" || a->status == "error");
        // One compact line: "provider · model  HTTP n  Nms  endpoint…"
        std::string line = a->title;
        if (!a->reason.empty()) {
            if (!line.empty()) line += "  ";
            line += a->reason;
        }
        if (line.empty()) line = a->detail.empty() ? "(call)" : a->detail;
        // Collapse multi-line detail into the primary line for the list; error_excerpt stays
        // on a second muted line under fails.
        size_t nl = line.find('\n');
        std::string rest;
        if (nl != std::string::npos) {
            rest = line.substr(nl + 1);
            line.resize(nl);
        }
        ImVec4 col = fail ? semanticOnGlass(pal.statusErr, surf) : surf.textTertiary;
        // Soft red tint even when statusErr is strong — keep it muted for density.
        if (fail) col.w = std::min(col.w, 0.95f);
        ImGui::PushStyleColor(ImGuiCol_Text, col);
        ImGui::TextWrapped("%s", line.c_str());
        ImGui::PopStyleColor();
        if (!rest.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                fail ? semanticOnGlass(pal.statusErr, surf) : surf.textTertiary);
            ImGui::TextWrapped("  %s", rest.c_str());
            ImGui::PopStyleColor();
        }
        (void)i;
    }
    if (fonts.mono) ImGui::PopFont();
    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##provlog WindowPadding
    ImRect cardRect = endGlassBubble(gb);
    chatCopyContext(cardRect, "##provlog_copy", allCopy);
}

static void drawAssistantTurn(AppState& st, ChatMsg& m, const ui::Fonts& fonts, float logW,
                              int msgIdx = -1, int turnNum = 0, bool isLastTurn = false) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& turnSurf = chatSurf();
    const float sc = ui::uiScale;

    // Author label. Each card that CAN host it (prose / partial / error) draws the
    // model+age INSIDE itself as the top row (first card wins via metaDrawn).
    // Activity-only turns and VC-restore turns keep the tight label row above the
    // turn content with the age appended inline.
    // Prefer the model stamped on the turn at run start; only fall back to the
    // live picker for an in-flight turn that somehow has no stamp. No model →
    // omit label (never bare "Assistant").
    const bool proseCard = !m.text.empty() && m.errKind == ERR_NONE;
    // Geometry-mutating turns keep the checkpoint-backed Before/After restore; EVERY
    // other finished turn still offers a chat-only rewind (msg:<idx>) so a revert is
    // reachable at any point in the chat. turnNum > 0 already implies chatMsgMutatedDoc
    // (turn numbers are only assigned to mutating turns in drawChatLogWindowed).
    const bool vcMutated = msgIdx >= 0 && turnNum > 0 && chatMsgMutatedDoc(m);
    const bool vcRestore = msgIdx >= 0 && !m.running && s_aiVc.available && !s_aiVc.busy &&
                           !isLastTurn;   // the last turn IS the current state — nothing to restore to
    // Drawn ONCE, INSIDE the turn's card as a bottom bar (same slot the user bubble's
    // rewind/edit pair occupies) — never floating below the card, which read as a stray
    // glyph detached from the box it restores. Turns with no card fall back to a bottom row.
    bool restoreDrawn = false;
    auto restoreBar = [&] {
        if (!vcRestore || restoreDrawn) return;
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
        drawAiVcTurnRestore(st, msgIdx, turnNum);
        restoreDrawn = true;
    };
    const bool metaInCard = !vcMutated &&
        (proseCard || m.errKind != ERR_NONE || m.resultPartial);
    bool metaDrawn = false;
    const char* mid = nullptr;
    if (!m.model.empty()) mid = m.model.c_str();
    else if (m.running && st.settings.model[0]) mid = st.settings.model;
    const bool haveModel = mid && mid[0];
    const std::string modelLabel = haveModel ? shortModelName(mid) : std::string();
    {
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                            ImVec2(ImGui::GetStyle().ItemSpacing.x, 0.0f));
        if (haveModel && !metaInCard) {
            ImGui::PushFont(fonts.medium);
            ImGui::TextColored(turnSurf.textSecondary, "%s", modelLabel.c_str());
            ImGui::PopFont();
            // Inline age next to the above-card label (in-card rows right-align it).
            const std::string age = chatAgeLabel(m.ts);
            if (!age.empty()) {
                ImGui::SameLine(0, 6 * sc);
                ImGui::PushStyleColor(ImGuiCol_Text, turnSurf.textTertiary);
                ImGui::TextUnformatted(age.c_str());
                ImGui::PopStyleColor();
                if (ImGui::IsItemHovered()) {
                    const std::string tip = chatTimeTooltip(m.ts);
                    if (!tip.empty()) ImGui::SetTooltip("%s", tip.c_str());
                }
            }
        }
        // The restore affordance used to live HERE, inline with the model name, where it
        // read as part of the author label. It now draws as a bottom action row at the end
        // of the turn (see the end of this function) so agent and user messages both offer
        // revert in the same place.
        if (haveModel && !metaInCard)
            ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
        ImGui::PopStyleVar();
    }

    // (1) Plan / Objectives — multi-shape section (tiles + todos + subagents).
    // Prefer planState; back-compat: legacy PlanInfo (Python kind:plan via app.cpp) normalizes
    // on the fly when planState was never filled. When a rowKind-18 marker exists in the
    // activity timeline the section renders THERE (arrival position) instead — drawing it
    // here too would double the card and re-introduce the out-of-order top-pin bug.
    bool planInTimeline = false;
    for (const auto& a : m.activity)
        if (a.rowKind == 18) { planInTimeline = true; break; }
    if (!planInTimeline && m.planState.present) {
        drawPlanSection(m.planState, m.activity, fonts);
    } else if (!planInTimeline && m.plan.present) {
        // Lazy normalize so the old AgentEvent plan path still lights the section.
        m.planState = planStateFromPlanInfo(m.plan);
        drawPlanSection(m.planState, m.activity, fonts);
    } else if (planInTimeline && m.planState.present && m.running) {
        // The full plan card lives inline at its arrival position (below). While the turn
        // runs, pin a one-line live progress summary at the TOP of the turn so step/part
        // progress stays visible even when the inline card has scrolled out of view.
        const std::string prog = planProgressLine(m.planState, m.activity);
        if (!prog.empty()) {
            const ui::AdaptiveSurface& surf = chatSurf();
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::TextUnformatted((std::string("Plan \xc2\xb7 ") + prog).c_str());
            ImGui::PopStyleColor();
            ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
        }
    }

    // (2) Agent timeline — typed blocks: collapsible thinking + glass action cards.
    drawAgentTimeline(st, m, fonts, logW, gShowThinking);

    // (3) Streaming assistant text — a Thin glass "prose" card (Liquid Glass v2 §6, host
    // contract H5) keeps body copy legible over the live viewport blur. Selectable for
    // click-drag + Ctrl+C; right-click copies selection or full text.
    if (proseCard) {
        const std::string prose = stripMdMarks(m.text);
        GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                          10.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
        ImGui::BeginChild("##prose", ImVec2(0, 0),
                          ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
        // Author meta row INSIDE the card top (model left, age right) when it
        // wasn't drawn above the turn (VC-restore turns keep the top row).
        if (metaInCard && !metaDrawn) {
            drawMsgMetaRow(fonts, modelLabel.empty() ? nullptr : modelLabel.c_str(),
                           turnSurf.textSecondary, m.ts);
            metaDrawn = true;
            ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
        }
        // Raw m.text (not `prose`): the renderer needs the ``` fences intact to route code
        // spans into the mono box. `prose` stays the copy payload, as before.
        const ImGuiID textId = drawMdProse("##prose_sel", m.text, chatSurf().textPrimary,
                                           fonts.mono);
        // Submitted before EndChild — see the comment on the user-bubble copy glyph in
        // drawUserBlock() for why an after-EndChild button silently eats no clicks here.
        {
            const ImVec2 wp = ImGui::GetWindowPos();
            const ImVec2 ws = ImGui::GetWindowSize();
            chatHoverCopyGlyph(ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)), prose);
        }
        restoreBar();
        ImGui::EndChild();
        ImGui::PopStyleVar();  // ##prose WindowPadding
        ImRect proseRect = endGlassBubble(gb);
        const std::string sel = chatInputSelection(textId);
        chatCopyContext(proseRect, "##prose_copy", prose, prose, sel);
        ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    }


    // (4) Pre-flight clarify gate: the model's build-critical questions, awaiting the user.
    if (m.clarify.active) drawClarifyCard(st, m, fonts);
    // (4b) Plan-mode / mission plan_ready approve gate (Execute as Agent/Swarm/Eng.Firm).
    if (m.planApprovePending && !m.running) drawPlanApproveCard(st, m, fonts);

    // (5) Legacy v1 AgentStep cards — kept for backward compat (glass-restyled in drawAgentStep).
    for (size_t si = 0; si < m.steps.size(); ++si) {
        ImGui::PushID((int)si);
        drawAgentStep(m.steps[si], fonts, logW);
        ImGui::PopID();
    }

    // (6) While running the live activity stream (timeline thoughts/tools/output) is the
    // primary content — no fake "Saying · working..." bar. Only fall back to a minimal
    // liveStatus line when the timeline is still empty (zero content yet).
    if (m.running) {
        bool hasLiveContent = false;
        for (const auto& a : m.activity) {
            if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
            hasLiveContent = true;
            break;
        }
        if (!hasLiveContent && !m.text.empty()) hasLiveContent = true;
        if (!hasLiveContent) {
            ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
            const ui::AdaptiveSurface& surf = chatSurf();
            std::string say = m.liveStatus;
            if (say == "Working" || say == "working...")
                say.clear();               // never "Saying · Working" — the dots carry it
            // The silent window is the one place with NO streamed content, so a static glyph
            // makes a slow provider and a hung app look identical. Cycle the dot count off
            // GetTime() (same technique as drawBusyMark) and, once the wait is long
            // enough to worry about, add an elapsed readout. Dots are padded to a fixed width
            // so only the dots move, not the readout after them.
            const int dots = 1 + (int)((long long)(ImGui::GetTime() * 2.5) % 3);
            const double elapsed = (m.ts > 0.0) ? (chatNowEpoch() - m.ts) : 0.0;
            const bool showElapsed = elapsed >= 3.0;
            say.append((size_t)dots, '.');
            if (showElapsed) {
                say.append((size_t)(3 - dots), ' ');
                say += " \xc2\xb7 " + std::to_string((long long)elapsed) + "s";
            }
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextWrapped("%s", say.c_str());
            ImGui::PopStyleColor();
        }
    } else {
        // (7) Usage footer — tokens + wall-time for the finished turn, dim (DESIGN §5).
        int tin = 0, tout = 0, tms = 0;
        for (const auto& a : m.activity) {
            if (a.rowKind != 0) continue;   // tool / ctx / provider rows aren't stage wall-time
            tin += a.tokensIn; tout += a.tokensOut; tms += a.durationMs;
        }
        if (tin > 0) {
            ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textTertiary);
            ImGui::Text("%d tokens in / %d out \xc2\xb7 %.1f s", tin, tout, tms / 1000.0);
            ImGui::PopStyleColor();
        }
    }

    // (8) Result / error block.
    // ERR_PARTIAL: warning-accent "Partially complete" card (mission mixed outcomes) —
    // geometry is kept; NOT a hard error. ERR_CAD/ERR_SYSTEM: red-bordered error card.
    if (m.errKind == ERR_PARTIAL || m.resultPartial) {
        std::string body = stripMdMarks(m.text);
        if (body.empty()) body = "Partially complete";
        ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
        GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                          10.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
        ImGui::BeginChild("##partialcard", ImVec2(0, 0),
                          ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
        const ImVec4 warnCol = semanticOnGlass(pal.statusWarn, chatSurf());
        if (metaInCard && !metaDrawn) {
            drawMsgMetaRow(fonts, modelLabel.empty() ? nullptr : modelLabel.c_str(),
                           turnSurf.textSecondary, m.ts);
            metaDrawn = true;
            ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
        }
        ImGui::PushFont(fonts.medium);
        ImGui::TextColored(warnCol, "Partially complete");
        ImGui::PopFont();
        // Body is the selectable detail (counts + first failure / verdict).
        const ImGuiID textId = chatDrawSelectableText("##partial_sel", body, chatSurf().textPrimary);
        // Submitted before EndChild — see the comment on the user-bubble copy glyph in
        // drawUserBlock() for why an after-EndChild button silently eats no clicks here.
        {
            const ImVec2 wp = ImGui::GetWindowPos();
            const ImVec2 ws = ImGui::GetWindowSize();
            chatHoverCopyGlyph(ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)), body);
        }
        restoreBar();
        ImGui::EndChild();
        ImGui::PopStyleVar();   // ##partialcard WindowPadding
        ImRect cardRect = endGlassBubble(gb);
        ImGui::GetWindowDrawList()->AddRect(
            cardRect.Min, cardRect.Max, ui::Palette::toU32(pal.statusWarn),
            ui::metrics::rounding(10.0f), 0, ui::metrics::stroke(1.5f));
        const std::string sel = chatInputSelection(textId);
        chatCopyContext(cardRect, "##partial_copy", body, body, sel);
    } else if (m.errKind != ERR_NONE) {
        std::string stopReason, errorDetail;
        for (const auto& a : m.activity) {
            if (a.stage != "__stop_meta__") continue;
            stopReason = a.title;
            errorDetail = !a.detail.empty() ? a.detail : a.reason;
            break;
        }
        // Nothing to show if the turn has no prose, no stop fields and no humanized line.
        if (!(m.text.empty() && stopReason.empty() && errorDetail.empty() && m.errorHuman.empty())) {
        // Also surface any failed activity reason strings (phase errors) that aren't already
        // folded into the meta row — keeps "budget exhausted (1)" from being the only line.
        std::string phaseErr;
        for (const auto& a : m.activity) {
            if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
            if (a.status != "fail" && a.status != "error") continue;
            std::string bit;
            if (!a.reason.empty()) bit = a.reason;
            else if (!a.detail.empty()) bit = a.detail;
            else if (!a.title.empty()) bit = a.title;
            if (bit.empty()) continue;
            if (errorDetail.find(bit) != std::string::npos) continue;
            if (m.text.find(bit) != std::string::npos) continue;
            if (!phaseErr.empty()) phaseErr += "\n";
            phaseErr += bit;
        }

        // Prefer full detail as the primary body when present; otherwise keep m.text.
        // Headline is always "stopped: <reason>" when stop_reason is known; full error_detail
        // is the selectable body. Generic "I stopped before fully finishing (unknown reason)"
        // only survives when both stop fields are truly absent.
        std::string body;
        if (!stopReason.empty() || !errorDetail.empty()) {
            body = "stopped: ";
            body += stopReason.empty() ? "unknown" : stopReason;
            // Skip the detail when it IS the headline verbatim — core emits
            // error_human == error_detail for failures that are already human-readable.
            if (!errorDetail.empty() && errorDetail != m.errorHuman) {
                body += "\n";
                body += errorDetail;
            }
            // Keep original prose when it adds guidance beyond the terse stop fields.
            const std::string plain = stripMdMarks(m.text);
            if (!plain.empty() && plain != errorDetail &&
                (errorDetail.empty() || plain.find(errorDetail) == std::string::npos) &&
                body.find(plain) == std::string::npos) {
                // Drop the old parenthetical-only early-stop sentence if detail supersedes it.
                const char* kFallback = "I stopped before fully finishing";
                if (plain.find(kFallback) == std::string::npos) {
                    body += "\n\n";
                    body += plain;
                } else {
                    const size_t dot = plain.find(". ");
                    if (dot != std::string::npos && dot + 2 < plain.size()) {
                        body += "\n\n";
                        body += plain.substr(dot + 2);
                    }
                }
            }
        } else {
            body = stripMdMarks(m.text);
        }
        if (!phaseErr.empty()) {
            body += "\n\n";
            body += phaseErr;
        }

        // The whole raw failure text lives on the message (the drain stopped throwing the
        // tail away). Keep the card body short and put every byte behind "Error details".
        constexpr size_t kErrBodyCap = 600;
        size_t cut = body.size() > kErrBodyCap ? kErrBodyCap : body.size();
        while (cut > 0 && cut < body.size() && ((unsigned char)body[cut] & 0xC0) == 0x80)
            --cut;  // never split a UTF-8 sequence
        std::string shown = body.substr(0, cut);
        const bool clipped = cut < body.size();
        if (clipped) shown += "\xe2\x80\xa6";
        const std::string& fullErr = !m.errorFull.empty() ? m.errorFull : body;
        const bool haveFull = !fullErr.empty() && (clipped || fullErr != shown);

        if (!shown.empty() || !m.errorHuman.empty()) {
            ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
            GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                              10.0f);
            ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                                ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
            ImGui::BeginChild("##errcard", ImVec2(0, 0),
                              ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
            // Error tone retuned for the frost so the reason stays readable over any model.
            const float* ec = (m.errKind == ERR_SYSTEM) ? pal.errSystem : pal.errCad;
            const ImVec4 errCol = semanticOnGlass(ec, chatSurf());
            if (metaInCard && !metaDrawn) {
                drawMsgMetaRow(fonts, modelLabel.empty() ? nullptr : modelLabel.c_str(),
                               turnSurf.textSecondary, m.ts);
                metaDrawn = true;
                ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
            }
            // Headline: the short actionable sentence core/agent derived from this same
            // failure ("Provider not signed in. Open Settings > AI > Accounts."). The stop
            // fields below are supporting detail, not the lede.
            if (!m.errorHuman.empty()) {
                ImGui::PushFont(fonts.medium);
                ImGui::PushStyleColor(ImGuiCol_Text, errCol);
                ImGui::TextWrapped("%s", m.errorHuman.c_str());
                ImGui::PopStyleColor();
                ImGui::PopFont();
                ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
            }
            // Headline-only card (humanized line, no stop fields) skips the body widget.
            ImGuiID textId = 0;
            if (!shown.empty()) textId = chatDrawSelectableText("##err_sel", shown, errCol);
            if (haveFull) {
                ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
                if (toggleChip("Error details", m.errFullOpen)) {
                    m.errFullOpen = !m.errFullOpen;
                    m.uiCachedH = -1.0f;  // the card just changed height
                }
                if (m.errFullOpen)
                    drawCodeBox("##err_full", fullErr, fonts.mono, ui::metrics::copilotDiffMaxH());
            }
            // Submitted before EndChild — see the comment on the user-bubble copy glyph in
            // drawUserBlock() for why an after-EndChild button silently eats no clicks here.
            {
                const ImVec2 wp = ImGui::GetWindowPos();
                const ImVec2 ws = ImGui::GetWindowSize();
                chatHoverCopyGlyph(ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)), fullErr);
            }
            restoreBar();
            ImGui::EndChild();
            ImGui::PopStyleVar();   // ##errcard WindowPadding
            ImRect cardRect = endGlassBubble(gb);
            ImGui::GetWindowDrawList()->AddRect(cardRect.Min, cardRect.Max, ui::Palette::toU32(pal.statusErr),
                                                ui::metrics::rounding(10.0f), 0, ui::metrics::stroke(1.5f));
            const std::string sel = chatInputSelection(textId);
            chatCopyContext(cardRect, "##err_copy", fullErr, fullErr, sel);
        }
        } // has something to show
    }

    // (8b) Fallback: an error/partial turn whose card ended up with no body renders
    // no card at all — don't lose the model label; draw it here (above-style, inline age).
    if (metaInCard && !metaDrawn && haveModel) {
        ImGui::PushFont(fonts.medium);
        ImGui::TextColored(turnSurf.textSecondary, "%s", modelLabel.c_str());
        ImGui::PopFont();
        const std::string age = chatAgeLabel(m.ts);
        if (!age.empty()) {
            ImGui::SameLine(0, 6 * sc);
            ImGui::PushStyleColor(ImGuiCol_Text, turnSurf.textTertiary);
            ImGui::TextUnformatted(age.c_str());
            ImGui::PopStyleColor();
            if (ImGui::IsItemHovered()) {
                const std::string tip = chatTimeTooltip(m.ts);
                if (!tip.empty()) ImGui::SetTooltip("%s", tip.c_str());
            }
        }
    }

    // (9) Provider HTTP call log — always expanded on fail; collapsed toggle on success.
    if (!m.running)
        drawProviderLog(m, fonts);

    // (10) Restore/rewind fallback — only for turns whose content drew NO card (bare
    // activity/error streams with an empty body). Card-bearing turns already host the
    // restore bar inside the card bottom via restoreBar() above, mirroring the user
    // bubble's meta bar.
    if (vcRestore && !restoreDrawn) {
        ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
        drawAiVcTurnRestore(st, msgIdx, turnNum);
    }

}


// Empty-state hero for a brand-new/blank conversation. Without it the composer's grey
// placeholder is the only affordance — the log reads as a broken blank panel. One centered
// title + one hint line; the hint left-aligns and wraps when the panel is too narrow to
// center it on a single line.
static void drawEmptyChatHero(const ui::Fonts& fonts, float logW) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const char* title = "Start a new session";
    const char* hint  = "Ask a question about this model, or describe what you want to build.";

    ImGui::Dummy(ImVec2(0, 28.0f * sc));
    ImGui::PushFont(fonts.h2 ? fonts.h2 : fonts.medium);
    const float tw = ImGui::CalcTextSize(title).x;
    if (tw < logW) ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (logW - tw) * 0.5f);
    ImGui::TextColored(surf.textPrimary, "%s", title);
    ImGui::PopFont();

    ImGui::Dummy(ImVec2(0, 4.0f * sc));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    const float hw = ImGui::CalcTextSize(hint).x;
    if (hw < logW) {
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (logW - hw) * 0.5f);
        ImGui::TextUnformatted(hint);
    } else {
        ImGui::TextWrapped("%s", hint);
    }
    ImGui::PopStyleColor();
}

void drawChatLogWindowed(AppState& st) {
    ui::Fonts& fonts = st.fonts;
    drawAiVcRollbackConfirm(st);   // pre-rollback confirmation modal (warning ladder)
    const float logW    = ImGui::GetContentRegionAvail().x;
    if (st.chat.empty()) { drawEmptyChatHero(fonts, logW); return; }
    const float viewTop = ImGui::GetScrollY();
    const float viewBot = viewTop + ImGui::GetWindowSize().y;
    const float slack   = ImGui::GetWindowSize().y;   // one extra viewport of buffer each side
    // 1-based index of finished assistant turns that mutated the doc — matches AI-VC turn labels.
    int mutTurn = 0;
    // The LATEST assistant turn is the state the user is already at — offering a restore
    // there is a no-op button. Find it once so drawAssistantTurn can suppress its bar.
    int lastAssistantIdx = -1;
    for (size_t k = 0; k < st.chat.size(); ++k)
        if (!st.chat[k].fromUser) lastAssistantIdx = (int)k;
    for (size_t mi = 0; mi < st.chat.size(); ++mi) {
        ChatMsg& m = st.chat[mi];
        ImGui::PushID((int)mi);
        const float y = ImGui::GetCursorPosY();
        const bool cacheValid = m.uiCachedH > 0.0f && std::fabs(m.uiCachedW - logW) < 0.5f;
        const bool interactive = m.running || m.clarify.active || m.clarify.awaiting ||
                                 (s_aiVc.confirmMsgIdx == (int)mi) ||
                                 (s_edit.active && s_edit.msgIdx == (int)mi);
        // Pre-count mutating assistant turns even when the row is off-screen (stable turn nums).
        int thisTurn = 0;
        if (!m.fromUser && chatMsgMutatedDoc(m)) {
            ++mutTurn;
            thisTurn = mutTurn;
        }
        if (cacheValid && !interactive &&
            (y + m.uiCachedH < viewTop - slack || y > viewBot + slack)) {
            ImGui::Dummy(ImVec2(logW, m.uiCachedH));   // same-height spacer, zero draw cost
            ImGui::PopID();
            continue;
        }
        ImGui::BeginGroup();
        if (m.fromUser) {
            drawUserBlock(st, m, fonts, logW, (int)mi);
        } else {
            drawAssistantTurn(st, m, fonts, logW, (int)mi, thisTurn,
                              (int)mi == lastAssistantIdx);
            ImGui::Dummy(ImVec2(0, ui::metrics::cpChatTurnGap()));   // gap to the next TURN
        }
        ImGui::EndGroup();
        // (Mode-colored left accent bar removed — it read as a stray vertical line next
        // to the model label. Mode is still stamped on the message for other chrome.)
        m.uiCachedH = ImGui::GetItemRectSize().y;
        m.uiCachedW = logW;
        ImGui::PopID();
    }
    chatDrawCopyFlash();
}

// Stick-to-bottom autoscroll for the chat scroll region. Call right after chatDrawLog /
// drawChatLogWindowed, INSIDE the scrolling child. While new content streams in (thinking,
// tool rows) the view stays pinned to the tail UNLESS the user scrolled up; scrolling back to
// within a few px of the bottom re-engages. The old "if scrollY >= maxY - 12 snap" check
// evaluated AFTER the frame's growth, so one >12px growth frame dropped stickiness forever —
// this instead detects a user scroll (position moved UP vs. last frame) and otherwise snaps.
// The state is PER-CALLER: the docked column, the full-page AI-tab chat and the firmware
// workspace can render the same conversation in the same frame, so one shared function-local
// static let scrolling up in one surface silently kill autoscroll in the others.
namespace {
struct StickState {
    bool  stick = true;
    bool  atBottom = true;   // mirrors stick after the snap check; drives the jump button
    float prevScrollY = -1.0f;
};
std::unordered_map<std::string, StickState> s_stickByRegion;
}  // namespace

void chatLogStickToBottom(const char* regionId) {
    StickState& s = s_stickByRegion[regionId ? regionId : ""];
    const float sy   = ImGui::GetScrollY();
    const float smax = ImGui::GetScrollMaxY();
    const float thresh = 24.0f * ui::uiScale;
    if (s.prevScrollY >= 0.0f && sy < s.prevScrollY - 0.5f)
        s.stick = false;                       // user scrolled up this frame
    if (sy >= smax - thresh)
        s.stick = true;                        // at/near the bottom — re-engage
    if (s.stick && smax > sy)
        ImGui::SetScrollY(smax);
    s.atBottom = s.stick;                      // the near-bottom check above folded into stick
    s.prevScrollY = ImGui::GetScrollY();
}

bool chatLogAtBottom(const char* regionId) {
    const auto it = s_stickByRegion.find(regionId ? regionId : "");
    return it == s_stickByRegion.end() || it->second.atBottom;
}

void chatLogJumpToBottom(const char* regionId) {
    StickState& s = s_stickByRegion[regionId ? regionId : ""];
    s.stick = true;
    s.prevScrollY = -1.0f;   // the deferred snap must not read as a user scroll-up
}

void chatDrawJumpToLatest(const char* regionId, const ImVec2& viewMin, const ImVec2& viewMax) {
    (void)viewMin;
    if (chatLogAtBottom(regionId)) return;
    const float sc = ui::uiScale;
    const float r  = 13.0f * sc;
    // 18dp x-inset keeps the disc clear of the pill-scrollbar lane on the child's right edge.
    const ImVec2 c(viewMax.x - 18.0f * sc - r, viewMax.y - 10.0f * sc - r);

    // The hit widget must live in the PARENT window (we are after EndChild) without disturbing
    // its cursor layout — save/restore around the InvisibleButton.
    const ImVec2 keep = ImGui::GetCursorPos();
    ImGui::SetCursorScreenPos(ImVec2(c.x - r, c.y - r));
    ImGui::PushID(regionId ? regionId : "chat");
    ImGui::InvisibleButton("##jump_latest", ImVec2(r * 2.0f, r * 2.0f));
    ImGui::PopID();
    const bool hov     = ImGui::IsItemHovered();
    const bool clicked = ImGui::IsItemClicked();
    if (hov) {
        ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip("Jump to the latest reply");
    }
    ImGui::SetCursorPos(keep);

    const ui::AdaptiveSurface& surf = chatSurf();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec4 fill = surf.tint;
    fill.w = hov ? 0.98f : 0.85f;
    dl->AddCircleFilled(c, r, ImGui::ColorConvertFloat4ToU32(fill));
    dl->AddCircle(c, r,
                  ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textPrimary.x, surf.textPrimary.y,
                                                        surf.textPrimary.z, hov ? 0.55f : 0.28f)),
                  0, ui::metrics::stroke(1.0f));
    // Down chevron.
    const ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
    const float hw = r * 0.42f, hh = r * 0.24f, th = ui::metrics::stroke(2.0f);
    const ImVec2 mid(c.x, c.y + hh * 0.6f);
    dl->AddLine(ImVec2(c.x - hw, c.y - hh), mid, ink, th);
    dl->AddLine(ImVec2(c.x + hw, c.y - hh), mid, ink, th);

    if (clicked) chatLogJumpToBottom(regionId);
}

// Re-engage stick-to-bottom on every chat surface (conversation switch). Dropping the entries
// also clears prevScrollY: the incoming log is usually a different height, and ImGui clamping
// the carried-over offset down would otherwise look exactly like a user scroll-up and unstick
// the very first frame of the new conversation.
void chatLogResetStick() { s_stickByRegion.clear(); }


// ---- unified AI Copilot pane: header -> conversation -> bottom composer ----
// The pane is a PINNED, full-height, rounded glass column overlaid on the viewport's right edge
// (~12dp margins inside the area between ribbon and status bar) — the ONLY layout; the docked
// task-pane path is gone. Geometry comes from ui::overlayBand() (published per-frame by app.cpp;
// same source as the FeatureManager column), so the column tracks ribbon/status-bar/window
// resizes with no extra wiring. Width is user-resizable via an INVISIBLE hit-zone on the
// column's left edge (no visible splitter). Until the first backdrop is published (early frames)
// the window falls back to its default placement so nothing breaks headless.

}  // namespace ui
