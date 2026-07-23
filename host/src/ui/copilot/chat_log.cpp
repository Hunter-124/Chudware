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
        const float rx = ImGui::GetWindowContentRegionMax().x - ageW;
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
    GlassBubble gb = beginGlassBubble(bw, ui::GlassLevel::Regular, 8.0f);
    // Tighter in-bubble padding than the theme default — slimmer message box.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10.0f * sc, 5.0f * sc));
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
    ImGui::Dummy(ImVec2(0, 3.0f * sc));

    // ---- meta bar: You [rewind edit] ......... age ----
    const double now = ImGui::GetTime();
    if (s_aiVc.confirmMsgIdx >= 0 && now > s_aiVc.confirmUntil) {
        s_aiVc.confirmMsgIdx = -1;
        s_aiVc.confirmKind = 0;
    }
    const bool pending = (s_aiVc.confirmMsgIdx == msgIdx && s_aiVc.confirmKind == 3);
    const bool showBtns = s_aiVc.available && msgIdx >= 0 && !s_aiVc.busy;
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
                    aiVcRollback(st, "msg:" + std::to_string(msgIdx),
                                 "Rewound to before this message");
                }
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(pending ? "Click again to confirm rewind."
                                          : "Rewind — restore the part to before this message.");
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
            8.0f * ui::uiScale, 0, ui::metrics::stroke(1.0f));
    }
    const std::string sel = chatInputSelection(textId);
    chatCopyContext(bubble, "##umsg_copy", userBody, userBody, sel);

    ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
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

    ImGui::Dummy(ImVec2(0, 3 * sc));
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

    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 6.0f);
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
    ImRect cardRect = endGlassBubble(gb);
    chatCopyContext(cardRect, "##provlog_copy", allCopy);
}

static void drawAssistantTurn(AppState& st, ChatMsg& m, const ui::Fonts& fonts, float logW,
                              int msgIdx = -1, int turnNum = 0) {
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
    const bool vcRestore = msgIdx >= 0 && turnNum > 0 && chatMsgMutatedDoc(m);
    const bool metaInCard = !vcRestore &&
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
        // Subtle restore affordance on mutating turns (hover / inline confirm — no modal).
        if (vcRestore) {
            if (haveModel && !metaInCard) ImGui::SameLine(0, 8 * sc);
            drawAiVcTurnRestore(st, msgIdx, turnNum);
        }
        if ((haveModel && !metaInCard) || vcRestore)
            ImGui::Dummy(ImVec2(0, 2.5f * sc));
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
            ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
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
                                          4.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10.0f * sc, 7.0f * sc));
        ImGui::BeginChild("##prose", ImVec2(0, 0),
                          ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
        // Author meta row INSIDE the card top (model left, age right) when it
        // wasn't drawn above the turn (VC-restore turns keep the top row).
        if (metaInCard && !metaDrawn) {
            drawMsgMetaRow(fonts, modelLabel.empty() ? nullptr : modelLabel.c_str(),
                           turnSurf.textSecondary, m.ts);
            metaDrawn = true;
            ImGui::Dummy(ImVec2(0, 2.0f * sc));
        }
        const ImGuiID textId = chatDrawSelectableText("##prose_sel", prose, chatSurf().textPrimary);
        // Submitted before EndChild — see the comment on the user-bubble copy glyph in
        // drawUserBlock() for why an after-EndChild button silently eats no clicks here.
        {
            const ImVec2 wp = ImGui::GetWindowPos();
            const ImVec2 ws = ImGui::GetWindowSize();
            chatHoverCopyGlyph(ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)), prose);
        }
        ImGui::EndChild();
        ImGui::PopStyleVar();  // ##prose WindowPadding
        ImRect proseRect = endGlassBubble(gb);
        const std::string sel = chatInputSelection(textId);
        chatCopyContext(proseRect, "##prose_copy", prose, prose, sel);
        ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
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
            ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
            const ui::AdaptiveSurface& surf = chatSurf();
            std::string say = m.liveStatus;
            if (say.empty() || say == "Working" || say == "working...")
                say = "\xe2\x80\xa6";  // minimal ellipsis — never "Saying · Working"
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
        ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
        GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                          8.0f);
        ImGui::BeginChild("##partialcard", ImVec2(0, 0),
                          ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
        const ImVec4 warnCol = semanticOnGlass(pal.statusWarn, chatSurf());
        if (metaInCard && !metaDrawn) {
            drawMsgMetaRow(fonts, modelLabel.empty() ? nullptr : modelLabel.c_str(),
                           turnSurf.textSecondary, m.ts);
            metaDrawn = true;
            ImGui::Dummy(ImVec2(0, 2.0f * sc));
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
        ImGui::EndChild();
        ImRect cardRect = endGlassBubble(gb);
        ImGui::GetWindowDrawList()->AddRect(
            cardRect.Min, cardRect.Max, ui::Palette::toU32(pal.statusWarn),
            8.0f * ui::uiScale, 0, ui::metrics::stroke(1.5f));
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
        // Nothing to show if the turn has no prose AND no stop fields.
        if (!(m.text.empty() && stopReason.empty() && errorDetail.empty())) {
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
            if (!errorDetail.empty()) {
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

        if (!body.empty()) {
            ImGui::Dummy(ImVec2(0, 2 * ui::uiScale));
            GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                              8.0f);
            ImGui::BeginChild("##errcard", ImVec2(0, 0),
                              ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
            // Error tone retuned for the frost so the reason stays readable over any model.
            const float* ec = (m.errKind == ERR_SYSTEM) ? pal.errSystem : pal.errCad;
            const ImVec4 errCol = semanticOnGlass(ec, chatSurf());
            if (metaInCard && !metaDrawn) {
                drawMsgMetaRow(fonts, modelLabel.empty() ? nullptr : modelLabel.c_str(),
                               turnSurf.textSecondary, m.ts);
                metaDrawn = true;
                ImGui::Dummy(ImVec2(0, 2.0f * sc));
            }
            const ImGuiID textId = chatDrawSelectableText("##err_sel", body, errCol);
            // Submitted before EndChild — see the comment on the user-bubble copy glyph in
            // drawUserBlock() for why an after-EndChild button silently eats no clicks here.
            {
                const ImVec2 wp = ImGui::GetWindowPos();
                const ImVec2 ws = ImGui::GetWindowSize();
                chatHoverCopyGlyph(ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)), body);
            }
            ImGui::EndChild();
            ImRect cardRect = endGlassBubble(gb);
            ImGui::GetWindowDrawList()->AddRect(cardRect.Min, cardRect.Max, ui::Palette::toU32(pal.statusErr),
                                                8.0f * ui::uiScale, 0, ui::metrics::stroke(1.5f));
            const std::string sel = chatInputSelection(textId);
            chatCopyContext(cardRect, "##err_copy", body, body, sel);
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

}


void drawChatLogWindowed(AppState& st) {
    ui::Fonts& fonts = st.fonts;
    const float logW    = ImGui::GetContentRegionAvail().x;
    const float viewTop = ImGui::GetScrollY();
    const float viewBot = viewTop + ImGui::GetWindowSize().y;
    const float slack   = ImGui::GetWindowSize().y;   // one extra viewport of buffer each side
    // 1-based index of finished assistant turns that mutated the doc — matches AI-VC turn labels.
    int mutTurn = 0;
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
            drawAssistantTurn(st, m, fonts, logW, (int)mi, thisTurn);
            ImGui::Dummy(ImVec2(0, 8 * ui::uiScale));
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
void chatLogStickToBottom() {
    static bool  s_stick = true;
    static float s_prevScrollY = -1.0f;
    const float sy   = ImGui::GetScrollY();
    const float smax = ImGui::GetScrollMaxY();
    const float thresh = 24.0f * ui::uiScale;
    if (s_prevScrollY >= 0.0f && sy < s_prevScrollY - 0.5f)
        s_stick = false;                       // user scrolled up this frame
    if (sy >= smax - thresh)
        s_stick = true;                        // at/near the bottom — re-engage
    if (s_stick && smax > sy)
        ImGui::SetScrollY(smax);
    s_prevScrollY = ImGui::GetScrollY();
}


// ---- unified AI Copilot pane: header -> conversation -> bottom composer ----
// The pane is a PINNED, full-height, rounded glass column overlaid on the viewport's right edge
// (~12dp margins inside the area between ribbon and status bar) — the ONLY layout; the docked
// task-pane path is gone. Geometry comes from ui::overlayBand() (published per-frame by app.cpp;
// same source as the FeatureManager column), so the column tracks ribbon/status-bar/window
// resizes with no extra wiring. Width is user-resizable via an INVISIBLE hit-zone on the
// column's left edge (no visible splitter). Until the first backdrop is published (early frames)
// the window falls back to its default placement so nothing breaks headless.

}  // namespace ui
