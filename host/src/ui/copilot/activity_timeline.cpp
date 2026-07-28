// Copilot activity timeline (T-RF6 split of legacy_chat.cpp): the per-turn typed-block
// timeline — agent steps, thinking blocks, action cards, the inline transparency rows
// (context chips, thought boxes, tool-call rows), artifact file actions, and
// drawAgentTimeline which sequences a turn's ActivityItems. Cards it delegates to live in
// cards.cpp; the items themselves are built in feed.cpp.

#include "copilot_internal.h"

namespace ui {

void drawAgentStep(AgentStep& s, const ui::Fonts& fonts, float logW) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& stepSurf = chatSurf();
    const ImVec4 accentCol = semanticOnGlass(pal.accent, stepSurf);
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    // Shared card inset: without it the card inherits the pane's tight 3/2 window padding and
    // its text sits flush against the glass edge, unlike the prose/user bubbles above it.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuRowPadX(), ui::metrics::cpMenuRowPadY()));
    ImGui::BeginChild("##stepcard", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textPrimary);
    // Build issues are amber, not red: a failed pass is normal mid-loop — the error goes
    // straight back to the model, which usually fixes it.
    ImVec4 badge = s.ok ? (s.done ? accentCol : semanticOnGlass(pal.iterBuilt, stepSurf))
                        : semanticOnGlass(pal.statusWarn, stepSurf);
    const char* word = s.ok ? (s.done ? "done" : "built") : "build issue";
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(badge, "Iteration %d - %s", s.index + 1, word);
    ImGui::PopFont();
    if (!s.dims.empty()) {
        ImGui::SameLine();
        ImGui::PushFont(fonts.mono);
        ImGui::PushStyleColor(ImGuiCol_Text, stepSurf.textSecondary);
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
        ImGui::PushStyleColor(ImGuiCol_Text, semanticOnGlass(pal.statusWarn, stepSurf));
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
    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##stepcard WindowPadding
    endGlassBubble(gb);
}

// One assistant turn: name + plan card + summary + iteration cards. While running, an inline
// live status line (spinner + phase) with a thin progress accent bar, plus a collapsible
// "Activity" section embedding the orchestrator run tree for this run.
// C45: local models reply in markdown but ImGui renders plain text — the raw ** and ###
// marks read as noise. Strip the common inline marks (bold/italic asterisks, heading hashes,
// backticks) for DISPLAY only; the stored message keeps the original text.
std::string stripMdMarks(const std::string& in) {
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

// C45b: stripMdMarks eats backticks one at a time, so a ``` fenced block collapsed into
// proportional body copy with its language tag stranded as a stray word. Split the raw text on
// fence LINES and render the code spans through drawCodeBox, prose spans as before. Fence-only:
// this is deliberately not a markdown parser, and inline `code` still goes through stripMdMarks.
// An unterminated fence (the common case mid-stream) renders as code to the end of the text.
// Returns the last prose segment's InputText id so the caller keeps its selection-aware copy
// menu; 0 when the text is entirely code.
ImGuiID drawMdProse(const char* id, const std::string& in, const ImVec4& col, ImFont* mono) {
    if (in.find("```") == std::string::npos)
        return chatDrawSelectableText(id, stripMdMarks(in), col);

    ImGuiID     lastTextId = 0;
    int         seg = 0;
    bool        inFence = false;
    std::string buf;
    // Emits the segment that just ENDED — hence flush() before the inFence flip below.
    auto flush = [&]() {
        size_t b = 0;
        while (b < buf.size() && buf[b] == '\n') ++b;   // no stray blank line where a fence sat
        if (b) buf.erase(0, b);
        while (!buf.empty() && buf.back() == '\n') buf.pop_back();
        if (!buf.empty()) {
            const std::string sid = std::string(id) + "_" + std::to_string(seg++);
            if (inFence)
                drawCodeBox(sid.c_str(), buf, mono, ui::metrics::copilotProseMaxH());
            else
                lastTextId = chatDrawSelectableText(sid.c_str(), stripMdMarks(buf), col);
        }
        buf.clear();
    };
    for (size_t i = 0; i <= in.size();) {
        size_t e = in.find('\n', i);
        const bool last = (e == std::string::npos);
        if (last) e = in.size();
        const std::string line = in.substr(i, e - i);
        std::string probe = line;
        while (!probe.empty() && (probe.back() == ' ' || probe.back() == '\t' || probe.back() == '\r'))
            probe.pop_back();
        if (probe.rfind("```", 0) == 0) {
            flush();
            inFence = !inFence;   // the fence line itself (incl. its ```lang tag) is never content
        } else {
            buf += line;
            buf += '\n';
        }
        if (last) break;
        i = e + 1;
    }
    flush();
    return lastTextId;
}


// ============================ typed-block agent timeline ============================
// The orchestrator event stream (feedTurnActivity -> ChatMsg::activity) is rendered as the
// DESIGN.md §5 typed blocks: dimmed, collapsible "thinking" blocks for reasoning-only stages and
// glass "action cards" (icon + human label + animated status + folded result + expandable detail)
// for tool / build stages. Engine RPCs + the event plumbing are unchanged — this is layout only.

// Atlas icon for a stage key (string-keyed: a missing name just falls back to no glyph).
static const char* stageIconName(const std::string& stage, bool missionHeader) {
    if (missionHeader) return "plan_board";
    std::string s = stage;
    size_t colon = s.find(':');           // strip the "<taskIdx>:" mission namespace prefix
    if (colon != std::string::npos) s = s.substr(colon + 1);
    if (s == "intake") return "ask";
    if (s == "plan")   return "plan_board";
    if (s == "model")  return "part";
    if (s == "edit")   return "prompt_edit";
    if (s == "review") return "health_check";
    if (s == "fix")    return "fix";
    if (s == "memory") return "history";
    return "activity";
}

// Classify a row: reasoning-only stages (no CAD ops, with captured detail) render as a thinking
// block; build/tool stages (model/edit/review/fix/memory) and mission headers are action cards.
static bool isThinkingItem(const ActivityItem& a) {
    if (a.missionHeader) return false;
    if (a.stage == "__stop_meta__") return false;  // hidden stop_reason/error_detail carrier
    std::string s = a.stage;
    size_t colon = s.find(':');
    if (colon != std::string::npos) s = s.substr(colon + 1);
    if (s == "model" || s == "edit" || s == "review" || s == "fix" || s == "memory") return false;
    return a.opsCount == 0 && !a.detail.empty();
}

// Truncate `s` to fit `maxW` in the current font, appending "…" when truncated (UTF-8-safe).
static std::string chatEllipsize(const std::string& s, float maxW) {
    if (maxW <= 0.0f) return s;
    if (ImGui::CalcTextSize(s.c_str()).x <= maxW) return s;
    static const char* kEll = "\xe2\x80\xa6";
    const float ellW = ImGui::CalcTextSize(kEll).x;
    std::string out = s;
    while (!out.empty() && ImGui::CalcTextSize(out.c_str()).x + ellW > maxW) out.pop_back();
    while (!out.empty() && (out.back() & 0xC0) == 0x80) out.pop_back();  // UTF-8 boundary
    while (!out.empty() && out.back() == ' ') out.pop_back();
    return out.empty() ? s : out + kEll;
}

// A collapsible, dimmed reasoning block. While it is the live frontier of the turn (or still
// streaming) it stays open so the thoughts stream; as soon as a later row lands or the turn
// finishes it auto-collapses to icon + title + one-line preview — unless the user explicitly
// toggled it (userToggled always wins, both directions). `showThinking` off -> one-liner.
static void drawThinkingBlock(ActivityItem& a, const ui::Fonts& fonts, bool showThinking,
                              bool liveFrontier) {
    const float sc = ui::uiScale;
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));

    if (!showThinking) {
        ImGui::PushFont(fonts.body);
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
        ImGui::TextUnformatted(a.title.empty() ? "Thinking" : a.title.c_str());
        ImGui::PopStyleColor();
        ImGui::PopFont();
        return;
    }

    // Auto-open while streaming or while this block is the live frontier — UNLESS the user
    // explicitly toggled it: a user collapse must stick even mid-run.
    const bool open = a.userToggled ? a.expanded : (a.expanded || a.streaming || liveFrontier);
    // Split detail into reply body + optional packed sent-prompt trailer.
    std::string replyBody = a.detail;
    std::string sentBody;
    {
        const char* kSentMark = "\n\x1e" "SENT\x1e\n";
        const char* kSentMarkBare = "\x1e" "SENT\x1e\n";
        size_t mark = replyBody.find(kSentMark);
        if (mark != std::string::npos) {
            sentBody = replyBody.substr(mark + std::strlen(kSentMark));
            replyBody = replyBody.substr(0, mark);
        } else if (replyBody.rfind(kSentMarkBare, 0) == 0) {
            sentBody = replyBody.substr(std::strlen(kSentMarkBare));
            replyBody.clear();
        }
    }
    const bool hasDetail = !replyBody.empty() || !sentBody.empty();
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##thinkblk", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    const ui::AdaptiveSurface& surf = chatSurf();
    if (!open && hasDetail) {
        // Collapsed presentation: dim stage icon before the title, same Image+SameLine pattern
        // as drawActionCard's header.
        ui::IconUV iv = ui::getIcon(stageIconName(a.stage, a.missionHeader), 20);
        if (iv.valid) {
            float gz = ImGui::GetTextLineHeight();
            ImVec4 tint(surf.textTertiary.x, surf.textTertiary.y, surf.textTertiary.z, 0.9f);
            ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
            ImGui::SameLine(0, 6 * sc);
        }
    }
    ImGui::PushFont(fonts.body);
    ImGui::TextColored(surf.textSecondary, "%s", a.title.empty() ? "Thinking" : a.title.c_str());
    ImGui::PopFont();
    if (hasDetail) {
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) { a.expanded = !open; a.userToggled = true; }
        ImGui::SameLine(0, 6 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(open ? "(hide)" : "(show thinking)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) { a.expanded = !open; a.userToggled = true; }
    }
    if (open && hasDetail) {
        if (!sentBody.empty()) {
            if (ImGui::TreeNodeEx("Sent to model##thinksent", ImGuiTreeNodeFlags_SpanAvailWidth)) {
                drawProseBox("##thinksentbox", sentBody, ui::metrics::copilotProseMaxH());
                ImGui::TreePop();
            }
        }
        if (!replyBody.empty()) {
            if (ImGui::TreeNodeEx("Model reply##thinkreply", ImGuiTreeNodeFlags_SpanAvailWidth)) {
                drawProseBox("##thinkreplybox", stripMdMarks(replyBody),
                             ui::metrics::copilotProseMaxH());
                ImGui::TreePop();
            }
        }
    } else if (hasDetail && !replyBody.empty()) {
        // Collapsed: dim one-line preview of the first reply line, ellipsized to the card's
        // content width (same pattern as drawThoughtBox).
        std::string prev = stripMdMarks(replyBody.substr(0, replyBody.find('\n')));
        prev = chatEllipsize(prev, ImGui::GetContentRegionAvail().x);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(prev.c_str());
        ImGui::PopStyleColor();
    }

    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##thinkblk WindowPadding
    ImRect thinkRect = endGlassBubble(gb);
    const std::string thinkCopy = chatActivityCopyText(a);
    chatCopyContext(thinkRect, "##think_copy", thinkCopy);

}

// A glass action card for a tool call / orchestrator build stage: status glyph + tool icon + human
// label + duration/ops meta, the verdict folded in as a result line, an expandable drawer (a
// legacy AgentStep's thinking/code/diff/thumbnail when attached, else the raw detail), and a red
// border when the stage failed. `step` is non-null only for v1 runs that carry an AgentStep.
void drawActionCard(ActivityItem& a, AgentStep* step, const ui::Fonts& fonts, float logW,
                           bool liveFrontier) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const bool running = a.status == "running";
    const bool fail    = a.status == "fail";
    auto hasWord = [](const std::string& s, const char* w) {
        std::string low; low.reserve(s.size());
        for (unsigned char c : s) low.push_back((char)std::tolower(c));
        return low.find(w) != std::string::npos;
    };
    const bool intakeLike = hasWord(a.stage, "intake") || hasWord(a.title, "intake") ||
                            hasWord(a.title, "understanding") || hasWord(a.stage, "understand");
    const bool planLike = hasWord(a.stage, "plan") || hasWord(a.title, "planning") ||
                          hasWord(a.title, "planner");

    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##actcard", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);   // base ink (dimText derives from it)

    // ---- header row: status glyph + tool icon + label + meta ----
    drawStatusGlyph(a.status, ImGui::GetTextLineHeight());
    ImGui::SameLine(0, 6 * sc);
    ui::IconUV iv = ui::getIcon(stageIconName(a.stage, a.missionHeader), 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight();
        ImVec4 tint(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z,
                   running ? 1.0f : 0.85f);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 6 * sc);
    }
    ImVec4 titleCol = running ? semanticOnGlass(pal.accent, surf)
                    : fail    ? semanticOnGlass(pal.statusErr, surf)
                              : surf.textPrimary;
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(titleCol, "%s", a.title.c_str());
    ImGui::PopFont();

    // Split detail into reply body + optional packed sent-prompt trailer.
    std::string replyBody = a.detail;
    std::string sentBody;
    {
        const char* kSentMark = "\n\x1e" "SENT\x1e\n";
        const char* kSentMarkBare = "\x1e" "SENT\x1e\n";
        size_t mark = replyBody.find(kSentMark);
        if (mark != std::string::npos) {
            sentBody = replyBody.substr(mark + std::strlen(kSentMark));
            replyBody = replyBody.substr(0, mark);
        } else if (replyBody.rfind(kSentMarkBare, 0) == 0) {
            sentBody = replyBody.substr(std::strlen(kSentMarkBare));
            replyBody.clear();
        }
    }
    // Optional alternatives packed as JSON in detail (intake/understanding cards).
    std::vector<std::string> altLines;
    if (!replyBody.empty() && (replyBody[0] == '{' || replyBody[0] == '[')) {
        try {
            auto j = nlohmann::json::parse(replyBody, nullptr, false);
            if (!j.is_discarded() && j.is_object()) {
                auto pull = [&](const char* key) {
                    if (!j.contains(key) || !j[key].is_array()) return;
                    for (const auto& it : j[key]) {
                        if (it.is_string()) altLines.push_back(it.get<std::string>());
                        else if (it.is_object()) {
                            std::string tc = jstr(it, "task_class");
                            if (tc.empty()) tc = jstr(it, "class");
                            if (tc.empty()) tc = jstr(it, "name");
                            if (!tc.empty()) altLines.push_back(std::move(tc));
                        }
                    }
                };
                pull("alternatives"); pull("alt_classes"); pull("candidates");
            }
        } catch (...) {}
    }
    // Every action card lands EXPANDED while it is the live frontier (the most recent visible
    // row mid-turn) or still running, and auto-collapses once the next row lands or the turn
    // ends — UNLESS the user explicitly toggled it (userToggled always wins, both directions).
    // No mutation of a.expanded here: pinning state belongs to the click handlers only.
    const bool open = a.userToggled ? a.expanded : (a.expanded || running || liveFrontier);
    const bool hasDrawer = (step && (!step->thinking.empty() || !step->code.empty() ||
                                     !step->diff.empty() || !step->contactSheet.empty())) ||
                           !replyBody.empty() || !sentBody.empty() || !altLines.empty();
    if (hasDrawer) {
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) { a.expanded = !open; a.userToggled = true; }
        ImGui::SameLine(0, 6 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(open ? "(hide)" : (planLike || intakeLike ? "(show thinking)" : "(details)"));
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) { a.expanded = !open; a.userToggled = true; }
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
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(meta);
        ImGui::PopStyleColor();
    }

    // ---- result / notes line (folded into the card) ----
    if (!a.reason.empty()) dimText(a.reason.c_str());
    else if (step && !step->notes.empty()) dimText(step->notes.c_str());

    // ---- expandable drawer ----
    if (open && hasDrawer) {
        if (!altLines.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::TextUnformatted("Alternatives");
            ImGui::PopStyleColor();
            for (const auto& al : altLines) {
                ImGui::Bullet();
                ImGui::SameLine(0, 4 * sc);
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
                ImGui::TextUnformatted(al.c_str());
                ImGui::PopStyleColor();
            }
        }
        if (step) {
            if (!step->thinking.empty())
                drawProseBox("##actthink", step->thinking, ui::metrics::copilotProseMaxH());
            if (!step->code.empty())
                drawCodeBox("##actcode", step->code, fonts.mono, ui::metrics::px(260.0f));
            if (!step->diff.empty())
                drawDiffBox("##actdiff", step->diff, fonts.mono, ui::metrics::copilotDiffMaxH());
            if (!step->contactSheet.empty()) {
                if (!step->triedThumb) { step->thumb = loadTexture(step->contactSheet); step->triedThumb = true; }
                if (step->thumb.id) {
                    float maxTw = ui::metrics::copilotThumbW();
                    float tw = logW < maxTw ? logW : maxTw;
                    float th = tw * (float)step->thumb.h / (float)step->thumb.w;
                    ImGui::Image((ImTextureID)(intptr_t)step->thumb.id, ImVec2(tw, th));
                }
            }
        }
        // Model I/O: Sent to model + Model reply (drawProseBox scrollable bodies).
        if (!sentBody.empty()) {
            if (ImGui::TreeNodeEx("Sent to model##actsent", ImGuiTreeNodeFlags_SpanAvailWidth)) {
                drawProseBox("##actsentbox", sentBody, ui::metrics::copilotProseMaxH());
                ImGui::TreePop();
            }
        }
        if (!replyBody.empty()) {
            if (ImGui::TreeNodeEx("Model reply##actreply", ImGuiTreeNodeFlags_SpanAvailWidth)) {
                drawProseBox("##actreplybox", stripMdMarks(replyBody), ui::metrics::copilotProseMaxH());
                ImGui::TreePop();
            }
        } else if (step == nullptr && !a.detail.empty() && sentBody.empty()) {
            // Fallback: no SENT packing, show detail as before.
            drawProseBox("##actdetail", stripMdMarks(a.detail), ui::metrics::copilotProseMaxH());
        }
    }

    ImGui::PopStyleColor();   // base ink
    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##actcard WindowPadding
    ImRect cardRect = endGlassBubble(gb);

    // ---- error frame: a red border around the whole card ----
    if (fail) {
        ImGui::GetWindowDrawList()->AddRect(cardRect.Min, cardRect.Max, ui::Palette::toU32(pal.statusErr),
                                            ui::metrics::rounding(8.0f), 0, ui::metrics::stroke(1.5f));
    }
    {
        const std::string copyTxt = chatActivityCopyText(a);
        chatCopyContext(cardRect, "##act_copy", copyTxt);

    }
}

// ============================ M12-M14: inline transparency rows ============================
// Typed rows fed from the aicad.agent_events/2 stream (feedTurnActivity), rendered inline in the
// same timeline as the orchestrator stage rows — subdued and informational, visually distinct
// from the user/assistant bubbles. Nothing is fabricated: no events => none of these render.

// M12 — a compact, dim one-line chip for a context injection: source + label + ~token count.
static void drawContextChip(const ActivityItem& a, const ui::Fonts& fonts) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    ui::IconUV iv = ui::getIcon("bookmark", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight() * 0.85f;
        ImVec4 tint(surf.textTertiary.x, surf.textTertiary.y, surf.textTertiary.z, 0.9f);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 5 * sc);
    }
    std::string line = "Context";
    if (!a.reason.empty()) line += " \xc2\xb7 " + a.reason;          // source (ledger/memory/...)
    line += ": " + a.title;                                          // human label
    if (a.ctxTokens > 0) line += "  (~" + std::to_string(a.ctxTokens) + " tok)";
    ImGui::PushFont(fonts.body);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    ImGui::TextWrapped("%s", line.c_str());
    ImGui::PopStyleColor();
    ImGui::PopFont();
    {
        const ImRect chipRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax());
        chatCopyContext(chipRect, "##ctx_copy", line);

    }
}

// M13 — the model's thoughts in a collapsible textbox. While deltas stream (a.streaming) or the
// box is the live frontier of the turn it stays open so tokens appear live; once a later row
// lands or the turn ends it auto-collapses to a one-line preview unless the user pinned it
// (userToggled always wins). `showThinking` off -> a single dim one-liner.
// stream:out classified as ops JSON ("Building ops…") always shows a one-line summary by default;
// raw JSON lives only inside the expander (never expanded-by-default mid-stream).
static void drawThoughtBox(ActivityItem& a, const ui::Fonts& fonts, bool showThinking,
                           bool liveFrontier) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    // Ops-JSON Output box OR plain prose Output — never hide behind the "show thinking" switch.
    const bool isOpsJson = (a.stage == "stream:out" && a.opsCount > 0) ||
                           (a.title.find("Building ops") != std::string::npos);
    const bool isOutput = isOpsJson || a.stage == "stream:out" || a.title == "Output";
    if (!showThinking && !isOutput) {
        ImGui::PushFont(fonts.body);
        ImGui::TextColored(surf.textSecondary, "Thinking");
        ImGui::PopFont();
        return;
    }
    // Ops JSON: only open when the user expanded it (never auto-opens, even mid-stream).
    // Thinking: open while streaming OR the live frontier OR user-expanded; a plain stream:out
    // "Output" box still collapses when the stream stops. A user's explicit toggle always wins.
    const bool open = isOpsJson ? a.expanded
                                : (a.userToggled ? a.expanded
                                                 : (a.expanded || a.streaming ||
                                                    (!isOutput && liveFrontier)));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##thoughtbox", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushFont(fonts.body);
    const char* boxLabel = a.title.empty() ? "Thinking" : a.title.c_str();
    ImGui::TextColored(surf.textSecondary, "%s%s", boxLabel,
                       (!isOpsJson && a.streaming) ? "\xe2\x80\xa6" : "");
    ImGui::PopFont();
    if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (ImGui::IsItemClicked()) {
        a.expanded = !open;
        a.userToggled = true;
    }
    ImGui::SameLine(0, 6 * sc);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    ImGui::TextUnformatted(open ? "(hide)" : (isOpsJson ? "(raw)" : "(show)"));
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (ImGui::IsItemClicked()) {
        a.expanded = !open;
        a.userToggled = true;
    }

    // Ops JSON collapsed (default): one-line summary from reason (op count / byte count).
    if (isOpsJson && !open) {
        std::string sum = a.reason;
        if (sum.empty()) {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "%zu bytes", a.detail.size());
            sum = buf;
        }
        ImGui::SameLine(0, 8 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(sum.c_str());
        ImGui::PopStyleColor();
    }

    if (open) {
        if (!a.detail.empty()) {
            if (isOpsJson) {
                // Raw JSON only inside the expander.
                drawProseBox("##opsraw", a.detail, ui::metrics::copilotProseMaxH());
            } else if (isOutput && a.streaming) {
                // Live prose stream: render inline so the box grows with the text.
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
                ImGui::TextWrapped("%s", stripMdMarks(a.detail).c_str());
                ImGui::PopStyleColor();
            } else {
                drawProseBox("##thoughttxt", stripMdMarks(a.detail), ui::metrics::copilotProseMaxH());
            }
        }
    } else if (!isOpsJson && !a.detail.empty()) {
        // Collapsed thinking/prose: dim one-line preview (first line), ellipsized to the card's
        // content width — a raw first line + TextUnformatted ran past the card's right edge.
        std::string prev = stripMdMarks(a.detail.substr(0, a.detail.find('\n')));
        prev = chatEllipsize(prev, ImGui::GetContentRegionAvail().x);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(prev.c_str());
        ImGui::PopStyleColor();
    }
    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##thoughtbox WindowPadding
    ImRect thoughtRect = endGlassBubble(gb);
    const std::string thoughtCopy = chatActivityCopyText(a);
    chatCopyContext(thoughtRect, "##thought_copy", thoughtCopy);
}

// ---- artifact file actions ---------------------------------------------------------------
// Shared by the inline chat artifact rows (tool-call files/images) and the Session Artifacts
// panel: open with the OS default handler, or reveal in the OS file manager. Warn-toasts when
// the file is gone (moved/deleted after the run).
static std::string chatShellQuote(std::string s) {
    std::string out;
    out.reserve(s.size() + 2);
    for (char c : s) {
        if (c == '"' || c == '\\' || c == '$' || c == '`') out.push_back('\\');
        out.push_back(c);
    }
    return out;
}

static bool chatArtifactExists(const std::string& path) {
    namespace fs = std::filesystem;
    std::error_code ec;
    return fs::exists(path, ec);
}

static void chatArtifactOpen(AppState& st, const std::string& path) {
    if (!chatArtifactExists(path)) {
        namespace fs = std::filesystem;
        st.toast(ui::ToastLevel::Warn, "File not found: " + fs::path(path).filename().string());
        return;
    }
    const std::string q = chatShellQuote(path);
#ifdef _WIN32
    std::string cmd = "start \"\" \"" + q + "\"";
#elif defined(__APPLE__)
    std::string cmd = "open \"" + q + "\"";
#else
    std::string cmd = "xdg-open \"" + q + "\" >/dev/null 2>&1 &";
#endif
    (void)std::system(cmd.c_str());
}

void chatArtifactReveal(AppState& st, const std::string& path) {
    namespace fs = std::filesystem;
    if (!chatArtifactExists(path)) {
        st.toast(ui::ToastLevel::Warn, "File not found: " + fs::path(path).filename().string());
        return;
    }
    // A directory target (e.g. an unsaved document's workspace folder) is opened directly;
    // a file target is revealed/selected inside its containing folder.
    std::error_code ec;
    const bool isDir = fs::is_directory(path, ec);
    const std::string q = chatShellQuote(path);
#ifdef _WIN32
    std::string cmd = isDir ? "explorer \"" + q + "\"" : "explorer /select,\"" + q + "\"";
#elif defined(__APPLE__)
    std::string cmd = isDir ? "open \"" + q + "\"" : "open -R \"" + q + "\"";
#else
    // No portable select-file on Linux — open the containing folder instead.
    const std::string dir = isDir ? path : fs::path(path).parent_path().string();
    const std::string qd = chatShellQuote(dir.empty() ? std::string(".") : dir);
    std::string cmd = "xdg-open \"" + qd + "\" >/dev/null 2>&1 &";
#endif
    (void)std::system(cmd.c_str());
}

// Lower-cased extension (with the leading dot) of a path.
static std::string chatLowerExt(const std::string& p) {
    const size_t dot = p.find_last_of('.');
    const size_t slash = p.find_last_of("/\\");
    if (dot == std::string::npos || (slash != std::string::npos && dot < slash)) return {};
    std::string e = p.substr(dot);
    for (char& c : e) c = (char)std::tolower((unsigned char)c);
    return e;
}

// Extensions the app itself opens in the viewport (mirrors file_explorer.cpp isKnownExt: native
// .step+ container, CAD import formats, ECAD/KiCad).
static bool chatAppOpenableExt(const std::string& e) {
    return e == ".step+" || e == ".step" || e == ".stp" || e == ".iges" || e == ".igs" ||
           e == ".stl"   || e == ".3mf"  || e == ".ecad+" || e == ".ecad" ||
           e == ".kicad_sch" || e == ".kicad_pcb";
}

// Smart "open": CAD/ECAD files open IN THE APP's viewport via st.requestOpenPath (the same
// dispatch a File Explorer double-click uses — app.cpp's pump routes CAD vs ECAD). xdg-open on a
// .step usually has no OS association and silently does nothing, which read as "the button
// doesn't work". Everything else still goes to the OS default handler.
void chatArtifactOpenSmart(AppState& st, const std::string& path) {
    if (!chatArtifactExists(path)) {
        namespace fs = std::filesystem;
        st.toast(ui::ToastLevel::Warn, "File not found: " + fs::path(path).filename().string());
        return;
    }
    if (chatAppOpenableExt(chatLowerExt(path))) {
        st.requestOpenPath = path;
        return;
    }
    chatArtifactOpen(st, path);
}

// Two small actions — "(open)" / "(folder)" — for one artifact path. Real SmallButtons (not
// bare text + IsItemClicked): a TextWrapped label on the same row swallows the hover/click for
// the trailing bare-text items, so the old text "buttons" never fired. Caller wraps in PushID.
void chatArtifactActions(AppState& st, const std::string& path) {
    ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textTertiary);
    if (ImGui::SmallButton("(open)")) chatArtifactOpenSmart(st, path);
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("%s\n%s",
                          chatAppOpenableExt(chatLowerExt(path))
                              ? "Open in the viewport"
                              : "Open with the default app",
                          path.c_str());
    ImGui::SameLine(0, 4.0f * ui::uiScale);
    if (ImGui::SmallButton("(folder)")) chatArtifactReveal(st, path);
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Show in file manager\n%s", path.c_str());
    ImGui::PopStyleColor();
}

// The packed tool-row detail: feedTurnActivity writes args_preview, then the capped failure
// text after "\n\x1eERR\x1e\n" and artifact paths after "\n\x1eART\x1e\n" (ActivityItem gains
// no fields — app_state.h is integrator-owned). Split back into its three parts.
struct ToolRowDetail {
    std::string args;
    std::string err;
    std::string arts;
};

static ToolRowDetail splitToolRowDetail(const std::string& detail) {
    ToolRowDetail d;
    d.args = detail;
    const char* kArtMark = "\n\x1e" "ART\x1e\n";
    const char* kArtMarkBare = "\x1e" "ART\x1e\n";
    size_t mark = d.args.find(kArtMark);
    if (mark != std::string::npos) {
        d.arts = d.args.substr(mark + std::strlen(kArtMark));
        d.args = d.args.substr(0, mark);
    } else if (d.args.rfind(kArtMarkBare, 0) == 0) {
        d.arts = d.args.substr(std::strlen(kArtMarkBare));
        d.args.clear();
    }
    const char* kErrMark = "\n\x1e" "ERR\x1e\n";
    const char* kErrMarkBare = "\x1e" "ERR\x1e\n";
    mark = d.args.find(kErrMark);
    if (mark != std::string::npos) {
        d.err = d.args.substr(mark + std::strlen(kErrMark));
        d.args = d.args.substr(0, mark);
    } else if (d.args.rfind(kErrMarkBare, 0) == 0) {
        d.err = d.args.substr(std::strlen(kErrMarkBare));
        d.args.clear();
    }
    return d;
}

// True when a tool-call failure payload is the harness REFUSING a repeated (tool, args) pair
// (services/agent/tool_loop_guard.py — ToolCallLoopGuard.message()) rather than a tool that ran
// and broke. agent_events.tool_call_event ships no structured flag for it (the payload carries
// only ok/error_preview), so the message marker IS the wire contract. Matched case-insensitively
// anywhere in the HEAD of the text rather than as an exact prefix: the same message reaches the
// GUI bare (dispatch raises RuntimeError(msg)), behind an exception-class prefix, or quoted
// inside a {"ok": false, "error": …} dump depending on which dispatch path refused. Bounding the
// search to the head keeps a genuine tool failure that merely quotes a guard message deeper in
// its output from being recoloured as an intervention.
static bool isLoopGuardRefusal(const std::string& err) {
    static const char kMark[] = "loop guard:";
    const size_t marklen = sizeof(kMark) - 1;
    const size_t head = err.size() < 160 ? err.size() : 160;
    if (head < marklen) return false;
    for (size_t i = 0; i + marklen <= head; ++i) {
        size_t k = 0;
        while (k < marklen &&
               (char)std::tolower((unsigned char)err[i + k]) == kMark[k]) ++k;
        if (k == marklen) return true;
    }
    return false;
}

static bool toolRowIsGuardRefusal(const ActivityItem& a) {
    return a.status == "fail" && isLoopGuardRefusal(splitToolRowDetail(a.detail).err);
}

// M14 — one inline row per tool call ("Doing"): ok/fail glyph + tool icon + tool name + latency,
// with the human-readable target as a dim wrapped line. Args preview + failure text + image
// artifacts expand on click.
// Mid-loop CAD tool failures use statusWarn (amber), matching drawAgentStep build issues — the
// error is fed back for repair, not a catastrophic turn failure. A loop-guard refusal is not a
// failure at all (the tool never ran), so it drops the amber chrome entirely for the neutral
// "skipped" glyph + iterBuilt blue chip. `failCount` > 1 collapses consecutive identical
// tool+target fails into one row (drawAgentTimeline computes the run).
static void drawToolCallRowImpl(AppState& st, ActivityItem& a, const ui::Fonts& fonts,
                                int failCount, bool liveFrontier) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    // Split the packed detail FIRST: whether this row is a genuine failure or the harness's own
    // loop-guard refusal decides the glyph, chip, tone and border drawn below.
    const ToolRowDetail parts = splitToolRowDetail(a.detail);
    const std::string& argsBody = parts.args;
    const std::string& errBody  = parts.err;
    const std::string& artsBlob = parts.arts;
    const bool fail     = a.status == "fail";
    const bool guard    = fail && isLoopGuardRefusal(errBody);
    const bool hardFail = fail && !guard;   // a tool actually ran and broke
    // Amber chrome for repairable mid-loop fails (glyph via "retry" status → statusWarn).
    const ImVec4 failTone = semanticOnGlass(pal.statusWarn, chatSurf());
    // Refusals get the palette's informational role instead (iterBuilt — the "in progress,
    // nothing broke" blue of the "built" step badge and the AI-VC tool chip), never amber.
    const ImVec4 guardTone = semanticOnGlass(pal.iterBuilt, chatSurf());
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##toolrow", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    const ui::AdaptiveSurface& surf = chatSurf();
    drawStatusGlyph(hardFail ? "retry" : guard ? "skipped" : a.status,
                    ImGui::GetTextLineHeight());
    ImGui::SameLine(0, 6 * sc);
    ui::IconUV iv = ui::getIcon("wrench", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight();
        ImVec4 tint(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.85f);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 6 * sc);
    }
    ImGui::PushFont(fonts.medium);
    {
        std::string doingTitle = "Doing \xc2\xb7 " + a.title;
        ImGui::TextColored(hardFail ? failTone : surf.textPrimary, "%s", doingTitle.c_str());
    }
    ImGui::PopFont();

    // Auto-open while this row is the live frontier of the turn; collapses as soon as a later
    // row lands or the turn ends. A user's explicit toggle always wins, both directions.
    const bool open = a.userToggled ? a.expanded : (a.expanded || liveFrontier);
    const bool hasDrawer = !argsBody.empty() || !errBody.empty() || !artsBlob.empty();
    if (hasDrawer) {
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) { a.expanded = !open; a.userToggled = true; }
        ImGui::SameLine(0, 6 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(open ? "(hide)" : "(details)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) { a.expanded = !open; a.userToggled = true; }
    }
    if (fail || a.durationMs > 0 || failCount > 1) {
        ImGui::SameLine(0, 8 * sc);
        char meta[80];
        // A refusal has no latency worth showing (nothing was dispatched) and is not a failure:
        // it is the harness skipping a call the model already made.
        if (guard && failCount > 1)
            std::snprintf(meta, sizeof(meta), "skipped \xc2\xb7 repeated call \xc3\x97%d", failCount);
        else if (guard)
            std::snprintf(meta, sizeof(meta), "skipped \xc2\xb7 repeated call");
        else if (hardFail && failCount > 1 && a.durationMs > 0)
            std::snprintf(meta, sizeof(meta), "failed \xc3\x97%d \xc2\xb7 %.1fs",
                          failCount, a.durationMs / 1000.0);
        else if (hardFail && failCount > 1)
            std::snprintf(meta, sizeof(meta), "failed \xc3\x97%d", failCount);
        else if (hardFail && a.durationMs > 0)
            std::snprintf(meta, sizeof(meta), "failed \xc2\xb7 %.1fs", a.durationMs / 1000.0);
        else if (hardFail)
            std::snprintf(meta, sizeof(meta), "failed");
        else
            std::snprintf(meta, sizeof(meta), "%.1fs", a.durationMs / 1000.0);
        ImGui::PushStyleColor(ImGuiCol_Text, guard ? guardTone
                                           : hardFail ? failTone : surf.textSecondary);
        ImGui::TextUnformatted(meta);
        ImGui::PopStyleColor();
    }
    if (!a.reason.empty()) dimText(a.reason.c_str());   // the human target — "what it acts on"
    if (guard) {
        // What happened, in the user's terms — collapsed AND expanded, because it is the row's
        // label, not its payload. The model-facing guidance text appears only in the drawer, so
        // the same sentence is never printed twice on one card.
        ImGui::PushStyleColor(ImGuiCol_Text, guardTone);
        ImGui::TextWrapped("Identical call repeated — the harness skipped it and asked the model "
                           "to try another approach. The tool never ran.");
        ImGui::PopStyleColor();
    } else if (hardFail && !errBody.empty() && !open) {
        // Keep a one-line cause visible even when collapsed (same calm tone as drawAgentStep).
        ImGui::PushStyleColor(ImGuiCol_Text, failTone);
        ImGui::TextWrapped("%s", errSummary(errBody).c_str());
        ImGui::PopStyleColor();
    }
    if (open && hasDrawer) {
        if (guard) {
            // The guidance sent back to the model, in full, wrapped in place. NOT drawCodeBox:
            // that is an InputTextMultiline, which does not wrap — it clipped this single long
            // line mid-word — and its content would only repeat the summary above it.
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::TextWrapped("%s", errBody.c_str());
            ImGui::PopStyleColor();
        } else if (hardFail && !errBody.empty()) {
            // Why it failed, first: amber summary + full text in a warn-outlined code box.
            ImGui::PushStyleColor(ImGuiCol_Text, failTone);
            ImGui::TextWrapped("%s", errSummary(errBody).c_str());
            ImGui::PopStyleColor();
            const ImVec2 errMin = ImGui::GetCursorScreenPos();
            const float errW = ImGui::GetContentRegionAvail().x;
            drawCodeBox("##toolerr", errBody, fonts.mono, ui::metrics::px(200.0f));
            const ImVec2 errMax(errMin.x + errW,
                                ImGui::GetCursorScreenPos().y - ImGui::GetStyle().ItemSpacing.y);
            ImGui::GetWindowDrawList()->AddRect(errMin, errMax,
                                                ui::Palette::toU32(pal.statusWarn),
                                                4.0f * sc, 0, ui::metrics::stroke(1.0f));
        }
        // The guard's own text quotes the repeated arguments verbatim ("…with the same arguments
        // ({})"), so a second box holding that same "{}" is noise — drop it in that case only.
        // Longer args, which the guard summarizes and truncates, still get their box; either way
        // right-click Copy on the row yields the whole raw detail.
        if (!argsBody.empty() &&
            !(guard && errBody.find(argsBody) != std::string::npos))
            drawProseBox("##tooldetail", stripMdMarks(argsBody), ui::metrics::copilotProseMaxH());
        // Inline image artifacts (lazy GL upload; same path as drawAgentStep contact sheets).
        if (!artsBlob.empty()) {
            const float logW = ImGui::GetContentRegionAvail().x;
            // Session-local texture cache keyed by path (tool rows are redrawn every frame).
            static std::unordered_map<std::string, Tex> s_artTex;
            std::istringstream iss(artsBlob);
            std::string line;
            int imgI = 0;
            while (std::getline(iss, line)) {
                if (line.empty()) continue;
                size_t tab = line.find('\t');
                std::string kindA = (tab == std::string::npos) ? "file" : line.substr(0, tab);
                std::string path  = (tab == std::string::npos) ? line : line.substr(tab + 1);
                if (path.empty()) continue;
                if (kindA == "image") {
                    auto it = s_artTex.find(path);
                    if (it == s_artTex.end()) {
                        Tex t = loadTexture(path);
                        it = s_artTex.emplace(path, t).first;
                    }
                    if (it->second.id) {
                        float maxTw = ui::metrics::copilotThumbW();
                        float tw = logW < maxTw ? logW : maxTw;
                        float th = tw * (float)it->second.h / (float)std::max(1, it->second.w);
                        ImGui::PushID(imgI++);
                        ImGui::Image((ImTextureID)(intptr_t)it->second.id, ImVec2(tw, th));
                        ImGui::PopID();
                    } else {
                        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
                        ImGui::TextWrapped("image: %s", path.c_str());
                        ImGui::PopStyleColor();
                    }
                    ImGui::PushID(imgI + 1000);
                    chatArtifactActions(st, path);
                    ImGui::PopID();
                } else {
                    // mesh / file — show path as a dim line (no inline preview) + file actions.
                    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
                    ImGui::TextWrapped("%s: %s", kindA.c_str(), path.c_str());
                    ImGui::PopStyleColor();
                    ImGui::PushID(imgI + 2000);
                    chatArtifactActions(st, path);
                    ImGui::PopID();
                    ++imgI;
                }
            }
        }
    }
    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##toolrow WindowPadding
    ImRect rowRect = endGlassBubble(gb);
    if (hardFail)
        ImGui::GetWindowDrawList()->AddRect(rowRect.Min, rowRect.Max,
                                            ui::Palette::toU32(pal.statusWarn),
                                            ui::metrics::rounding(8.0f), 0, ui::metrics::stroke(1.5f));
    else if (guard)
        // Hairline informational outline: enough to set the row apart from a plain completed
        // call, deliberately lighter than the amber failure frame.
        ImGui::GetWindowDrawList()->AddRect(rowRect.Min, rowRect.Max,
                                            ui::Palette::toU32(pal.iterBuilt),
                                            ui::metrics::rounding(8.0f), 0, ui::metrics::stroke(1.0f));
    {
        const std::string toolCopy = chatActivityCopyText(a);
        chatCopyContext(rowRect, "##tool_copy", toolCopy);

    }
}

void drawToolCallRow(AppState& st, ActivityItem& a, const ui::Fonts& fonts) {
    // Standalone caller (outside the turn timeline): never the live frontier -> collapsed.
    drawToolCallRowImpl(st, a, fonts, /*failCount=*/1, /*liveFrontier=*/false);
}


void drawAgentTimeline(AppState& st, ChatMsg& m, const ui::Fonts& fonts, float logW,
                              bool showThinking) {
    if (m.activity.empty()) return;
    const bool turnRunning = m.running;
    if (!turnRunning) {
        // Stale live rows on a finished turn (restored history, terminal events dropped after
        // a stop) would animate forever — clamp them once here, idempotently.
        for (auto& a : m.activity) {
            if (a.streaming) a.streaming = false;
            if (a.status == "running") a.status = "done";
        }
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    // The live frontier: the most recent row this loop will actually draw (skip conditions
    // below). Only that row auto-expands mid-turn; earlier rows collapse to summary+icon.
    size_t lastVisible = (size_t)-1;
    for (size_t i = 0; i < m.activity.size(); ++i) {
        const ActivityItem& a = m.activity[i];
        if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
        if (a.rowKind == 10 && a.stage == "class:pending") continue;
        lastVisible = i;
    }
    for (size_t i = 0; i < m.activity.size(); ++i) {
        ActivityItem& a = m.activity[i];
        // Hidden meta row carrying stop_reason / error_detail — never drawn as a card.
        // Provider-call rows (rowKind 9) aggregate into the collapsible Provider log.
        // Pending routing rows (rowKind 10, "class:pending") stay hidden until a real
        // role change (agent.role) reveals them — first-turn default routing shows no card.
        if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
        if (a.rowKind == 10 && a.stage == "class:pending") continue;
        const bool liveFrontier = turnRunning && (i == lastVisible);
        ImGui::PushID((int)i);
        if (a.rowKind == 1) {
            drawContextChip(a, fonts);
        } else if (a.rowKind == 2) {
            drawThoughtBox(a, fonts, showThinking, liveFrontier);
        } else if (a.rowKind == 3) {
            // Collapse consecutive identical tool+target failures into one row with a count
            // (stops five identical amber cards stacking during a repair loop). Show the
            // last item in the run so the visible error_preview is the most recent.
            int failCount = 1;
            size_t drawIdx = i;
            if (a.status == "fail") {
                const bool aGuard = toolRowIsGuardRefusal(a);
                size_t j = i + 1;
                while (j < m.activity.size()) {
                    const ActivityItem& n = m.activity[j];
                    if (n.rowKind != 3 || n.status != "fail") break;
                    if (n.title != a.title || n.reason != a.reason) break;
                    // Never fold a harness refusal in with a genuine failure of the same tool:
                    // the merged row carries ONE label, which would then misdescribe the other.
                    if (toolRowIsGuardRefusal(n) != aGuard) break;
                    ++failCount;
                    ++j;
                }
                drawIdx = j - 1;
                i = j - 1;  // loop ++i advances past the coalesced run
            }
            // Frontier for the coalesced row is judged by the row actually drawn (drawIdx).
            drawToolCallRowImpl(st, m.activity[drawIdx], fonts, failCount,
                                turnRunning && (drawIdx == lastVisible));
        } else if (a.rowKind == 10) {
            drawClassificationCard(a, fonts, turnRunning);
        } else if (a.rowKind == 19) {
            drawDelegationCard(a, fonts);
        } else if (a.rowKind == 4) {
            drawCorpOrgCard(a, fonts);
        } else if (a.rowKind == 5) {
            drawCorpMeetingCard(a, fonts);
        } else if (a.rowKind == 6) {
            drawCorpSteeringCard(a, fonts);
        } else if (a.rowKind == 7 || a.rowKind == 8) {
            drawCorpMutedLine(a, fonts);
        } else if (a.rowKind == 11) {
            drawSwarmWaveCard(a, fonts);
        } else if (a.rowKind == 12) {
            drawFirmBidsCard(a, fonts);
        } else if (a.rowKind == 13) {
            drawFirmAwardCard(a, fonts);
        } else if (a.rowKind == 14) {
            drawFirmTeamCard(a, fonts);
        } else if (a.rowKind == 15) {
            drawFirmCommCard(a, fonts);
        } else if (a.rowKind == 16) {
            drawAdvisorCard(a, fonts);
        } else if (a.rowKind == 17) {
            drawSubRunCard(a, fonts);
        } else if (a.rowKind == 18) {
            // Plan position marker — the plan/objectives section renders HERE, where the plan
            // first arrived in the event stream, instead of pinned to the top of the turn.
            if (m.planState.present) drawPlanSection(m.planState, m.activity, fonts);
        } else if (isThinkingItem(a)) {
            drawThinkingBlock(a, fonts, showThinking, liveFrontier);
        } else {
            drawActionCard(a, nullptr, fonts, logW, liveFrontier);
        }
        ImGui::PopID();
    }
}

// Forward: user bubble + rewind/edit actions (defined after AI-VC helpers).

}  // namespace ui
