// Copilot activity timeline (T-RF6 split of legacy_chat.cpp): the per-turn typed-block
// timeline — agent steps, thinking blocks, action cards, the inline transparency rows
// (context chips, thought boxes, tool-call rows), artifact file actions, and
// drawAgentTimeline which sequences a turn's ActivityItems. Cards it delegates to live in
// cards.cpp; the items themselves are built in feed.cpp.

#include "copilot_internal.h"
#include "agent_text.h"
#include "stream_rows.h"              // ui::streamTextVisible — a blank prose box draws nothing
#include "ui/copilot/tool_phrase.h"   // human phrasing + grouping for raw MCP tool symbols
#include "ui/ai/ai_prefs.h"           // cached chat-display prefs (Settings -> AI -> Harness)
#include "gl_compat.h"                // glDeleteTextures — bounded artifact texture cache
#include "util/time_local.h"

namespace ui {

// ---- agent-authored text sanitizers (agent_text.{h,cpp}) -------------------------------------
// Two live defects, both on strings the copilot did NOT author in C++: a tool-call card printed
// the opaque engine id ("doc-cpp00000039") where the part's NAME belongs, and the U+2192 arrows
// services/agent/agent_events.py joins tool targets with rendered as notdef '?' (uiGlyphRanges()
// never requests the arrows block). Fixed HERE rather than at ingest because feed.cpp has no
// AppState — the doc-id -> name lookup needs the tab list — and because a transcript restored
// from disk never passes through feed.cpp at all.
//
// docLabel() is deliberately NOT used: it returns the literal "a document" for an unknown id,
// which must never be spliced into a JSON blob. An id with no open tab stays a raw id.
//
// `Name (doc-cpp0017)` reads well in prose, but nested inside a JSON string value on the args
// drawer it produced exactly the {"doc_id": "AI: mudbucket2 (doc-cpp00000023)"} the user called
// out. substituteDocIds composes that parenthetical itself (agent_text.cpp — owned elsewhere),
// so the JSON path drops it here: the NAME is what was missing, and diagnostic mode
// (chatHumanizeDocIds off) still shows the untouched id.
// Idempotent: with the parenthetical gone there is no id left for a later pass to match.
static std::string stripResolvedDocIdParens(const std::string& s) {
    if (s.find(" (doc") == std::string::npos) return s;
    std::string out;
    out.reserve(s.size());
    for (size_t i = 0; i < s.size();) {
        if (s[i] == ' ' && i + 9 < s.size() && s[i + 1] == '(' &&
            s.compare(i + 2, 3, "doc") == 0 && (s[i + 5] == '-' || s[i + 5] == '_') &&
            s.compare(i + 6, 3, "cpp") == 0) {
            size_t j = i + 9;
            while (j < s.size() && std::isxdigit((unsigned char)s[j])) ++j;
            if (j > i + 9 && j < s.size() && s[j] == ')') { i = j + 1; continue; }
        }
        out.push_back(s[i++]);
    }
    return out;
}

// `jsonBody` marks the tool-row args blob, where the id parenthetical is stripped (above).
// The whole substitution is user-gated: chatHumanizeDocIds() off (diagnostic mode) leaves engine
// ids exactly as the harness wrote them, which is the entire point of that mode. The glyph fold
// always runs — an unrenderable codepoint is a defect at every detail level.
std::string sanitizeAgentText(AppState& st, const std::string& s, bool jsonBody) {
    if (s.empty()) return s;
    if (!ui::chatHumanizeDocIds()) return foldToAtlasGlyphs(s);
    std::string out = substituteDocIds(s, [&st](const std::string& id) -> std::string {
        for (const auto& t : st.docTabs)
            if (t.docId == id) return t.displayTitle();
        return std::string();
    });
    if (jsonBody) out = stripResolvedDocIdParens(out);
    return foldToAtlasGlyphs(out);
}

// Applied IN PLACE, so the persisted transcript and the right-click "Copy text" path
// (ui_common chatActivityCopyText) see the same clean strings. Restricted to the prose row kinds
// (0 thinking/action, 1 context chip, 2 thought box, 3 tool call) — rowKind >= 4 are
// corporate/swarm cards whose detail is a JSON payload cards.cpp re-parses, and those carry no
// doc ids. Only a tool row's detail is an args JSON body.
//
// Both halves are idempotent, but "idempotent" is not "free": their unchanged path still returns
// the string BY VALUE. This runs over every visible row every frame, and a mission turn keeps
// hundreds of multi-KB tool-arg blobs on screen, so the pre-scan (agentTextMayChange, allocation
// -free) is what keeps a long transcript from paying two heap round-trips per string per frame.
void sanitizeAgentTextInPlace(AppState& st, std::string& s, bool jsonBody) {
    if (s.empty() || !ui::agentTextMayChange(s)) return;
    s = sanitizeAgentText(st, s, jsonBody);
}

static void sanitizeActivityItem(AppState& st, ActivityItem& a) {
    if (a.rowKind > 3) return;
    sanitizeAgentTextInPlace(st, a.reason);
    sanitizeAgentTextInPlace(st, a.detail, /*jsonBody=*/a.rowKind == 3);
    sanitizeAgentTextInPlace(st, a.title);
}

// ---- collapse animation (ui::chatCollapseAnim) ----------------------------------------------
// Thinking boxes, thought boxes and tool drawers used to SNAP shut the frame a later row landed.
// Each animated box keeps an eased 0..1 factor plus the natural height measured on its last
// fully-open frame; while the factor is strictly between the ends the EXPANDED body is drawn
// inside a fixed-height clipping child, so the content slides shut instead of vanishing.
// Keyed by the caller's ImGui id, which drawAgentTimeline's per-row PushID makes unique.
struct CollapseAnim {
    float t = -1.0f;   // < 0 = never seen: seeded to the target so history never animates in
    float h = 0.0f;    // natural expanded height, logical px, from the last fully-open frame
};
static std::unordered_map<ImGuiID, CollapseAnim> s_collapseT;
static constexpr float kCollapseSecs = 0.18f;

// Advance *key*'s factor toward *open* and return the smoothstep-eased value in [0,1].
static float collapseEase(ImGuiID key, bool open) {
    // A long session scrolls through thousands of row ids; dropping the table wholesale is
    // cheaper than per-entry liveness tracking and only costs a reseed (no visible animation).
    if (s_collapseT.size() > 4096) s_collapseT.clear();
    CollapseAnim& c = s_collapseT[key];
    const float target = open ? 1.0f : 0.0f;
    if (c.t < 0.0f) {
        c.t = target;
    } else if (c.t != target) {
        const float step = ImGui::GetIO().DeltaTime / kCollapseSecs;
        c.t = (c.t < target) ? ImMin(target, c.t + step) : ImMax(target, c.t - step);
    }
    return c.t * c.t * (3.0f - 2.0f * c.t);
}

struct CollapseBody {
    ImGuiID key = 0;
    bool    child = false;     // body is clipped inside a fixed-height child
    bool    measure = false;   // body is inline this frame: record its natural height
    float   y0 = 0.0f;
};

// Opens the animated body for *key*; the caller only reaches this when e > 0. Fully open — or
// not yet measured, i.e. the box's very first open frame — draws inline and measures. Anything
// in between clips into a child of the measured height scaled by e.
static CollapseBody beginCollapseBody(ImGuiID key, float e, const char* id) {
    CollapseBody b;
    b.key = key;
    const float h = s_collapseT[key].h;
    if (e >= 1.0f || h <= 0.0f) {
        b.measure = true;
        b.y0 = ImGui::GetCursorPosY();
        return b;
    }
    b.child = true;
    const float ah = h * e;
    ImGui::BeginChild(id, ImVec2(0.0f, ah < 1.0f ? 1.0f : ah), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    return b;
}

static void endCollapseBody(CollapseBody& b) {
    if (b.child) { ImGui::EndChild(); return; }
    if (b.measure) {
        const float h = ImGui::GetCursorPosY() - b.y0;
        if (h > 0.0f) s_collapseT[b.key].h = h;
    }
}

// ---- inline artifact thumbnails: bounded texture cache --------------------------------------
// Renders are now written once per render_part call, so the old unbounded path-keyed map leaked
// one GL texture per call for the life of the process. LRU-bounded; eviction deletes on the GL
// thread, the only thread that ever draws these rows. There is no shared Tex release helper in
// the host (app_state.cpp:1176 deletes inline exactly the same way), so the delete lives here.
struct ArtTexEntry {
    Tex                tex;
    unsigned long long use = 0;
};
static std::unordered_map<std::string, ArtTexEntry> s_artTex;
static unsigned long long s_artTexClock = 0;
static constexpr size_t kArtTexCap = 48;

static const Tex& artTexFor(const std::string& path) {
    auto it = s_artTex.find(path);
    if (it != s_artTex.end()) {
        it->second.use = ++s_artTexClock;
        return it->second.tex;
    }
    while (s_artTex.size() >= kArtTexCap) {
        auto oldest = s_artTex.begin();
        for (auto i = s_artTex.begin(); i != s_artTex.end(); ++i)
            if (i->second.use < oldest->second.use) oldest = i;
        if (oldest->second.tex.id) {
            unsigned int id = oldest->second.tex.id;
            glDeleteTextures(1, &id);
        }
        s_artTex.erase(oldest);
    }
    ArtTexEntry e;
    e.tex = loadTexture(path);
    e.use = ++s_artTexClock;
    return s_artTex.emplace(path, e).first->second.tex;
}

void drawAgentStep(AgentStep& s, const ui::Fonts& fonts, float logW) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& stepSurf = chatSurf();
    const ImVec4 accentCol = semanticOnGlass(pal.accent, stepSurf);
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    // Shared card inset (cpChatCardPadX/Y — same tokens every other activity card in this file
    // and cards.cpp uses): without it the card inherits the pane's tight 3/2 window padding and
    // its text sits flush against the glass edge, unlike the prose/user bubbles above it.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, ui::metrics::cpChatRowGap()));
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
    ImGui::PopStyleVar(2);   // ##stepcard compact ItemSpacing + WindowPadding
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
// Declared in copilot_internal.h: chat_log.cpp bounds its model-name labels with it too.
std::string chatEllipsize(const std::string& s, float maxW) {
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
    // Collapse animation: the body eases shut over kCollapseSecs instead of vanishing.
    const ImGuiID animKey = ImGui::GetID("##thinkblk_anim");
    const bool    wantOpen = open && hasDetail;
    const float   openE = ui::chatCollapseAnim() ? collapseEase(animKey, wantOpen)
                                                 : (wantOpen ? 1.0f : 0.0f);
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, ui::metrics::cpChatRowGap()));
    ImGui::BeginChild("##thinkblk", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);

    const ui::AdaptiveSurface& surf = chatSurf();
    if (openE <= 0.0f && hasDetail) {
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
    if (openE > 0.0f) {
        CollapseBody cb = beginCollapseBody(animKey, openE, "##thinkanim");
        if (!sentBody.empty()) {
            if (ImGui::TreeNodeEx("Sent to model##thinksent", ImGuiTreeNodeFlags_SpanAvailWidth)) {
                drawProseBox("##thinksentbox", sentBody, ui::metrics::copilotProseMaxH());
                ImGui::TreePop();
            }
        }
        if (!replyBody.empty()) {
            if (ImGui::TreeNodeEx("Model reply##thinkreply", ImGuiTreeNodeFlags_SpanAvailWidth)) {
                // Same grow-to-fit treatment as drawThoughtBox: a still-live reasoning block
                // fills out instead of scrolling inside the small static cap.
                const bool grow = a.streaming || liveFrontier;
                drawProseBox("##thinkreplybox", stripMdMarks(replyBody),
                             grow ? ui::metrics::copilotProseStreamCapH()
                                  : ui::metrics::copilotProseMaxH(), grow);
                ImGui::TreePop();
            }
        }
        endCollapseBody(cb);
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
    ImGui::PopStyleVar(2);   // ##thinkblk compact ItemSpacing + WindowPadding
    ImRect thinkRect = endGlassBubble(gb);
    const std::string thinkCopy = chatActivityCopyText(a);
    chatCopyContext(thinkRect, "##think_copy", thinkCopy);

}

// A glass action card for a tool call / orchestrator build stage: status glyph + tool icon + human
// label + duration/ops meta, the verdict folded in as a result line, an expandable drawer (a
// legacy AgentStep's thinking/code/diff/thumbnail when attached, else the raw detail), and a red
// border when the stage failed. `step` is non-null only for v1 runs that carry an AgentStep.
//
// `onSpine` = the run spine is drawing this row's status mark out in the left gutter, so the card
// must NOT draw its own leading glyph — two marks for one step reads as two steps. The card keeps
// drawing its own when it is rendered outside a timeline (drawToolCallRow's single-row entry).
void drawActionCard(ActivityItem& a, AgentStep* step, const ui::Fonts& fonts, float logW,
                           bool liveFrontier, bool onSpine) {
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
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, ui::metrics::cpChatRowGap()));
    ImGui::BeginChild("##actcard", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);   // base ink (dimText derives from it)

    // ---- header row: status glyph + tool icon + label + meta ----
    // The spine already carries this step's mark in the gutter; drawing a second one here would
    // read as two steps. Still reserve the cell so the label column lines up either way.
    if (onSpine) ImGui::Dummy(ImVec2(0.0f, ImGui::GetTextLineHeight()));
    else         drawStatusGlyph(a.status, ImGui::GetTextLineHeight());
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
    ImGui::PopStyleVar(2);   // ##actcard compact ItemSpacing + WindowPadding
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
    // ONE line, ellipsized: these chips are inline transparency notes, and a
    // wrapped second line ran ragged past the icon gutter (and could spill past
    // the timeline's right edge). The full text stays on the hover tooltip.
    const std::string clipped = chatEllipsize(line, ImGui::GetContentRegionAvail().x);
    ImGui::TextUnformatted(clipped.c_str());
    if (clipped != line && ImGui::IsItemHovered()) ImGui::SetTooltip("%s", line.c_str());
    ImGui::PopStyleColor();
    ImGui::PopFont();
    {
        const ImRect chipRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax());
        chatCopyContext(chipRect, "##ctx_copy", line);

    }
}

// A plain-prose "Output" box IS the model's reply, drawn where it arrived. It used to be a
// hidden raw-log carrier because the reply was rendered once, after the whole timeline
// (chat_log.cpp's prose card) — which is exactly why a reasoning burst that followed a
// sentence rendered ABOVE it: the sentence was structurally last no matter when it was
// spoken. Ops-JSON streams are not prose; they keep their own summary card.
bool isProseOutputRow(const ActivityItem& a) {
    if (a.rowKind != 2) return false;
    if (a.stage != "stream:out" && a.title != "Output") return false;
    return a.opsCount <= 0 && a.title.find("Building ops") == std::string::npos;
}

// The prose the timeline renders inline, in arrival order. feed.cpp appends the SAME bytes to
// both the box and m.text from one delta, and drawAgentTimeline runs the boxes through the
// same sanitizer chat_log runs over m.text, so this is a byte-exact prefix of m.text.
std::string timelineInlineProse(const ChatMsg& m) {
    std::string out;
    for (const auto& a : m.activity)
        if (isProseOutputRow(a)) out += a.detail;
    return out;
}

bool timelineHasInlineProse(const ChatMsg& m) {
    for (const auto& a : m.activity)
        if (isProseOutputRow(a) && ui::streamTextVisible(a.detail)) return true;
    return false;
}

// What m.text carries BEYOND the inline prose: a reply promoted whole (nothing streamed), or a
// note appended after the fact such as "(Stopped by you.)". Callers render this instead of
// m.text so nothing prints twice. If the two ever diverge (a doc-id substitution spanning a
// segment boundary is the only known way), the timeline is authoritative and the tail is
// dropped — a missing rename is a far smaller defect than the whole reply printed twice.
std::string timelineProseTail(const ChatMsg& m) {
    const std::string shown = timelineInlineProse(m);
    if (shown.empty()) return m.text;
    if (m.text.size() <= shown.size()) return {};
    if (m.text.compare(0, shown.size(), shown) != 0) return {};
    return m.text.substr(shown.size());
}

// The model's prose, as a Thin glass bubble (Liquid Glass v2 §6, host contract H5) drawn at the
// position in the column where the segment actually arrived. Selectable for click-drag +
// Ctrl+C; right-click copies the selection or the whole segment — the same affordances the
// single trailing reply card used to carry, which chat_log.cpp now only draws for a reply the
// timeline never saw.
static void drawProseBubble(ActivityItem& a, const ui::Fonts& fonts) {
    if (!ui::streamTextVisible(a.detail)) return;   // an empty segment is not a card
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    const std::string prose = stripMdMarks(a.detail);
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                      10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::BeginChild("##prose", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    // Raw detail (not `prose`): the renderer needs the ``` fences intact to route code spans
    // into the mono box. `prose` stays the copy payload.
    const ImGuiID textId = drawMdProse("##prose_sel", a.detail, chatSurf().textPrimary,
                                       fonts.mono);
    // Submitted before EndChild — an after-EndChild button silently eats no clicks here (see
    // the user-bubble copy glyph in chat_log.cpp drawUserBlock).
    {
        const ImVec2 wp = ImGui::GetWindowPos();
        const ImVec2 ws = ImGui::GetWindowSize();
        chatHoverCopyGlyph(ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)), prose);
    }
    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##prose WindowPadding
    const ImRect proseRect = endGlassBubble(gb);
    const std::string sel = chatInputSelection(textId);
    chatCopyContext(proseRect, "##prose_copy", prose, prose, sel);
}

// M13 — the model's thoughts in a collapsible textbox. While deltas stream (a.streaming) or the
// box is the live frontier of the turn it stays open so tokens appear live; once a later row
// lands or the turn ends it auto-collapses to a one-line preview unless the user pinned it
// (userToggled always wins). `showThinking` off -> a single dim one-liner.
// The only stream:out box that reaches here is ops JSON ("Building ops…"): it always shows a
// one-line summary by default, with the raw JSON inside the expander (never expanded-by-default
// mid-stream). Plain prose Output rows are skipped upstream by isProseOutputRow.
static void drawThoughtBox(ActivityItem& a, const ui::Fonts& fonts, bool showThinking,
                           bool liveFrontier) {
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    // The ops-JSON Output box never hides behind the "show thinking" switch.
    const bool isOpsJson = (a.stage == "stream:out" && a.opsCount > 0) ||
                           (a.title.find("Building ops") != std::string::npos);
    if (!showThinking && !isOpsJson) {
        ImGui::PushFont(fonts.body);
        ImGui::TextColored(surf.textSecondary, "Thinking");
        ImGui::PopFont();
        return;
    }
    // Ops JSON: only open when the user expanded it (never auto-opens, even mid-stream).
    // Thinking: open while streaming OR the live frontier OR user-expanded. A user's explicit
    // toggle always wins.
    const bool open = isOpsJson ? a.expanded
                                : (a.userToggled ? a.expanded
                                                 : (a.expanded || a.streaming || liveFrontier));
    // Collapse animation: the body eases shut over kCollapseSecs instead of vanishing.
    const ImGuiID animKey = ImGui::GetID("##thoughtbox_anim");
    const float   openE = ui::chatCollapseAnim() ? collapseEase(animKey, open)
                                                 : (open ? 1.0f : 0.0f);
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, ui::metrics::cpChatRowGap()));
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
    if (isOpsJson && openE <= 0.0f) {
        std::string sum = a.reason;
        if (sum.empty()) {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "%zu bytes", a.detail.size());
            sum = buf;
        }
        ImGui::SameLine(0, 8 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        // Ellipsize to what the row ACTUALLY has left — an unbounded summary
        // ("1747 bytes · mission_st…") ran straight off the card's right edge.
        const std::string sumFit = chatEllipsize(sum, ImGui::GetContentRegionAvail().x);
        ImGui::TextUnformatted(sumFit.c_str());
        ImGui::PopStyleColor();
    }

    if (openE > 0.0f) {
        if (!a.detail.empty()) {
            CollapseBody cb = beginCollapseBody(animKey, openE, "##thoughtanim");
            if (isOpsJson) {
                // Raw JSON only inside the expander.
                drawProseBox("##opsraw", a.detail, ui::metrics::copilotProseMaxH());
            } else {
                // Grow to fit while the text is still arriving (or this box is the live
                // frontier just after streaming stopped) instead of scrolling inside a small
                // fixed box — the box fills out as the model reasons, only falling back to the
                // scrolling presentation past the generous cap. Finished/historical boxes keep
                // the tight static scroll cap.
                const bool grow = a.streaming || liveFrontier;
                drawProseBox("##thoughttxt", stripMdMarks(a.detail),
                             grow ? ui::metrics::copilotProseStreamCapH()
                                  : ui::metrics::copilotProseMaxH(), grow);
            }
            endCollapseBody(cb);
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
    ImGui::PopStyleVar(2);   // ##thoughtbox compact ItemSpacing + WindowPadding
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
// text after "\n\x1eERR\x1e\n", artifact paths after "\n\x1eART\x1e\n" and the ts/cache-read
// META trailer last (ActivityItem gains no fields — app_state.h is integrator-owned). Split
// back into its parts. ToolRowDetail / declaration shared via copilot_internal.h —
// chatActivityCopyText (ui_common) uses the same split so a clipboard copy never carries the
// raw markers.
ToolRowDetail splitToolRowDetail(const std::string& detail) {
    ToolRowDetail d;
    d.args = detail;
    // META is the LAST trailer ("<epoch-ts>\t<cache-read-tokens>") — lift it off
    // the tail first so the ART/ERR splits below never see it. A persisted row
    // whose detail was tail-capped can carry a truncated META; parse defensively.
    const char* kMetaMark = "\n\x1e" "META\x1e\n";
    const char* kMetaMarkBare = "\x1e" "META\x1e\n";
    size_t mark = d.args.find(kMetaMark);
    std::string meta;
    if (mark != std::string::npos) {
        meta = d.args.substr(mark + std::strlen(kMetaMark));
        d.args = d.args.substr(0, mark);
    } else if (d.args.rfind(kMetaMarkBare, 0) == 0) {
        meta = d.args.substr(std::strlen(kMetaMarkBare));
        d.args.clear();
    }
    if (!meta.empty()) {
        const size_t tab = meta.find('\t');
        try {
            d.ts = std::stod(tab == std::string::npos ? meta : meta.substr(0, tab));
            if (tab != std::string::npos) d.cacheRead = std::stoi(meta.substr(tab + 1));
        } catch (...) { d.ts = 0.0; d.cacheRead = 0; }
        if (d.cacheRead < 0) d.cacheRead = 0;
    }
    const char* kArtMark = "\n\x1e" "ART\x1e\n";
    const char* kArtMarkBare = "\x1e" "ART\x1e\n";
    mark = d.args.find(kArtMark);
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

// Compact wall-clock label for a tool row's META timestamp ("4:37 PM"), ""
// when the stamp is missing/legacy. Date tooltip stays the hover's job (same
// convention as the bubble meta rows in chat_log.cpp).
static std::string chatRowClock(double ts) {
    if (ts <= 0.0) return std::string();
    const std::time_t t = (std::time_t)ts;
    std::tm tmv;
    if (!chudware::util::localTime(t, tmv)) return std::string();
    int h = tmv.tm_hour % 12; if (h == 0) h = 12;
    char buf[24];
    std::snprintf(buf, sizeof(buf), "%d:%02d %s", h, tmv.tm_min,
                  tmv.tm_hour < 12 ? "AM" : "PM");
    return buf;
}

// "12.3k" / "482" token counts for the cache-read chip.
std::string chatTokCount(int n) {
    char buf[32];
    if (n >= 10000) std::snprintf(buf, sizeof(buf), "%.1fk", n / 1000.0);
    else if (n >= 1000) std::snprintf(buf, sizeof(buf), "%.1fk", n / 1000.0);
    else std::snprintf(buf, sizeof(buf), "%d", n);
    return buf;
}

// Tool payloads are prose-shaped even when they contain JSON or an exception: at narrow chat
// widths they must soft-wrap, not become a horizontally scrolling InputText. Size the nested
// child from the WRAPPED text so the parent AutoResizeY child and its glass/failure outline
// include the entire visible detail region. Long payloads stop at maxH and remain clipped/
// scrollable inside this child; they can never paint below the owning tool card.
static void drawToolDetailBox(const char* id, const std::string& text, ImFont* font,
                              float maxH, bool failure) {
    if (text.empty()) return;
    const auto& pal = ui::palette;
    const float padX = ui::metrics::cpChatCardPadX();
    const float padY = ui::metrics::cpChatCardPadY();
    if (font) ImGui::PushFont(font);
    const float availW = ImGui::GetContentRegionAvail().x;
    const float wrapW = ImMax(ImGui::GetFontSize(), availW - padX * 2.0f);
    const float textH = ImGui::CalcTextSize(text.c_str(), nullptr, false, wrapW).y;
    const float minH = ImGui::GetTextLineHeight() + padY * 2.0f;
    const float boxH = ImMin(maxH, ImMax(minH, textH + padY * 2.0f));
    const ImVec4 bg = failure
        ? ImVec4(pal.codeBg[0], pal.codeBg[1], pal.codeBg[2], pal.codeBg[3])
        : ImVec4(pal.proseBg[0], pal.proseBg[1], pal.proseBg[2], pal.proseBg[3]);
    const ImVec4 ink = failure
        ? ui::maxContrastTextOn(bg)
        : ImVec4(pal.proseText[0], pal.proseText[1], pal.proseText[2], 1.0f);
    const ImVec4 border = failure
        ? semanticOnGlass(pal.statusWarn, chatSurf())
        : ImVec4(pal.glassBorder[0], pal.glassBorder[1],
                 pal.glassBorder[2], pal.glassBorder[3]);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(padX, padY));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, ui::metrics::rounding(4.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildBorderSize, ui::metrics::stroke(1.0f));
    ImGui::PushStyleColor(ImGuiCol_ChildBg, bg);
    ImGui::PushStyleColor(ImGuiCol_Text, ink);
    ImGui::PushStyleColor(ImGuiCol_Border, border);
    ImGui::BeginChild(id, ImVec2(0.0f, boxH),
                      ImGuiChildFlags_Border | ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::TextWrapped("%s", text.c_str());
    ImGui::EndChild();
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar(3);
    if (font) ImGui::PopFont();
}

// M14 — one inline row per tool call: ok/fail glyph + tool icon + a HUMAN title (tool_phrase.h
// turns `add_features` into "Building geometry"; the raw MCP symbol survives as the title's
// tooltip, and as a dim suffix at chatToolDetail() >= 2) + latency, with the cleaned target as a
// dim wrapped line. Args preview + failure text + image artifacts expand on click, and the
// drawer eases shut (chatCollapseAnim) instead of snapping when the next call starts.
// Mid-loop CAD tool failures use statusWarn (amber), matching drawAgentStep build issues — the
// error is fed back for repair, not a catastrophic turn failure. A loop-guard refusal is not a
// failure at all (the tool never ran), so it drops the amber chrome entirely for the neutral
// "skipped" glyph + iterBuilt blue chip. `failCount` > 1 collapses consecutive identical
// tool+target fails into one row; a non-null `group` of >1 is a fold of consecutive SIMILAR
// successful calls into one card (drawAgentTimeline computes both runs).
static void drawToolCallRowImpl(AppState& st, ActivityItem& a, const ui::Fonts& fonts,
                                int failCount, bool liveFrontier, int callI = -1,
                                const std::vector<ActivityItem*>* group = nullptr,
                                bool onSpine = false) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    // Chat-display prefs (cached in ai_prefs — safe to read per row per frame).
    const int  detailLvl = ui::chatToolDetail();
    const bool simple    = detailLvl <= 0;   // human sentence only: no drawer, no meta, no ids
    const bool diag      = detailLvl >= 2;   // + the raw MCP symbol dim after the human title
    const bool showRenders = ui::chatShowRenders();
    const int  groupN    = (group && group->size() > 1) ? (int)group->size() : 1;
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

    // Drawer state is settled BEFORE anything is drawn: the header's click targets and the
    // animated body both need it, and a grouped card's drawer is its members' drawers stacked.
    const bool open = a.userToggled ? a.expanded : (a.expanded || liveFrontier);
    bool hasDrawer = !argsBody.empty() || !errBody.empty() || !artsBlob.empty();
    if (groupN > 1) {
        hasDrawer = false;
        for (const ActivityItem* mi : *group) {
            const ToolRowDetail mp = splitToolRowDetail(mi->detail);
            if (!mp.args.empty() || !mp.err.empty() || !mp.arts.empty()) { hasDrawer = true; break; }
        }
    }
    // Simplified mode has no (details) affordance, no args drawer and no engine ids at all —
    // artifacts still reach the user, drawn inline below instead of behind an expander.
    if (simple) hasDrawer = false;
    const ImGuiID animKey = ImGui::GetID("##toolrow_anim");
    const bool    wantOpen = open && hasDrawer;
    const float   openE = ui::chatCollapseAnim() ? collapseEase(animKey, wantOpen)
                                                 : (wantOpen ? 1.0f : 0.0f);

    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin, 8.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, ui::metrics::cpChatRowGap()));
    ImGui::BeginChild("##toolrow", ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding);
    const ui::AdaptiveSurface& surf = chatSurf();

    // One artifact blob -> inline images (or, with renders off, dim path lines) + file actions.
    // Shared by the single-row drawer, every member of a grouped drawer, and simplified mode.
    auto drawArtifacts = [&](const std::string& blob) {
        if (blob.empty()) return;
        const float logW = ImGui::GetContentRegionAvail().x;
        std::istringstream iss(blob);
        std::string line;
        int imgI = 0;
        while (std::getline(iss, line)) {
            if (line.empty()) continue;
            size_t tab = line.find('\t');
            std::string kindA = (tab == std::string::npos) ? "file" : line.substr(0, tab);
            std::string path  = (tab == std::string::npos) ? line : line.substr(tab + 1);
            if (path.empty()) continue;
            ++imgI;
            if (kindA == "image" && showRenders) {
                const Tex& tx = artTexFor(path);
                if (tx.id) {
                    float maxTw = ui::metrics::copilotThumbW();
                    float tw = logW < maxTw ? logW : maxTw;
                    float th = tw * (float)tx.h / (float)std::max(1, tx.w);
                    ImGui::PushID(imgI);
                    ImGui::Image((ImTextureID)(intptr_t)tx.id, ImVec2(tw, th));
                    ImGui::PopID();
                } else {
                    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
                    ImGui::TextWrapped("image: %s", path.c_str());
                    ImGui::PopStyleColor();
                }
            } else {
                // mesh / file — and, with renders off, images too: a dim path line, no upload.
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
                ImGui::TextWrapped("%s: %s", kindA.c_str(), path.c_str());
                ImGui::PopStyleColor();
            }
            ImGui::PushID(imgI + 1000);
            chatArtifactActions(st, path);
            ImGui::PopID();
        }
    };

    // Same rule as drawActionCard: when the run spine owns this row's mark out in the gutter, the
    // row must not paint a second one. The spine's own status resolution (spineStatusOf) applies
    // the identical retry/skipped remap, so the gutter mark says exactly what this glyph would.
    if (onSpine) ImGui::Dummy(ImVec2(0.0f, ImGui::GetTextLineHeight()));
    else         drawStatusGlyph(hardFail ? "retry" : guard ? "skipped" : a.status,
                                 ImGui::GetTextLineHeight());
    ImGui::SameLine(0, 6 * sc);
    ui::IconUV iv = ui::getIcon("wrench", 20);
    if (iv.valid) {
        float gz = ImGui::GetTextLineHeight();
        ImVec4 tint(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.85f);
        ImGui::Image(iv.texId, ImVec2(gz, gz), iv.uv0, iv.uv1, tint);
        ImGui::SameLine(0, 6 * sc);
    }
    // ---- the human title. `add_features` is an MCP symbol, not a sentence: the card says what
    // the call DOES, and the raw symbol stays reachable as the title's tooltip (always) and as a
    // dim suffix in diagnostic mode.
    const std::string humanTitle = groupN > 1
        ? ui::toolGroupTitle(a.title, groupN)
        : ui::toolPhrase(a.title, a.reason, !fail).title;
    ImGui::PushFont(fonts.medium);
    ImGui::TextColored(hardFail ? failTone : surf.textPrimary, "%s", humanTitle.c_str());
    ImGui::PopFont();
    // Click state is sampled BEFORE SetTooltip: that opens a window of its own and the
    // IsItem* queries afterwards would no longer refer to the title.
    const bool titleHovered = ImGui::IsItemHovered();
    bool toggleHit = hasDrawer && ImGui::IsItemClicked();
    if (titleHovered) {
        if (hasDrawer) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        ImGui::SetTooltip("%s", a.title.c_str());
    }
    if (diag) {
        ImGui::SameLine(0, 6 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::Text("\xc2\xb7 %s", a.title.c_str());
        ImGui::PopStyleColor();
    }
    if (hasDrawer) {
        ImGui::SameLine(0, 6 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(open ? "(hide)" : "(details)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        if (ImGui::IsItemClicked()) toggleHit = true;
    }
    if (toggleHit) { a.expanded = !open; a.userToggled = true; }

    // ---- meta chip: latency / failure count / refusal note. Simplified mode shows none of it.
    double groupMs = a.durationMs;
    if (groupN > 1) {
        groupMs = 0;
        for (const ActivityItem* mi : *group) groupMs += mi->durationMs;
    }
    std::string metaS;
    if (!simple) {
        char meta[96];
        // A refusal has no latency worth showing (nothing was dispatched) and is not a failure:
        // it is the harness skipping a call the model already made.
        if (groupN > 1) {
            if (groupMs > 0) {
                std::snprintf(meta, sizeof(meta), "%.1fs", groupMs / 1000.0);
                metaS = meta;
            }
        } else if (guard && failCount > 1) {
            std::snprintf(meta, sizeof(meta), "skipped \xc2\xb7 repeated call \xc3\x97%d", failCount);
            metaS = meta;
        } else if (guard) {
            metaS = "skipped \xc2\xb7 repeated call";
        } else if (hardFail && failCount > 1 && a.durationMs > 0) {
            std::snprintf(meta, sizeof(meta), "failed \xc3\x97%d \xc2\xb7 %.1fs",
                          failCount, a.durationMs / 1000.0);
            metaS = meta;
        } else if (hardFail && failCount > 1) {
            std::snprintf(meta, sizeof(meta), "failed \xc3\x97%d", failCount);
            metaS = meta;
        } else if (hardFail && a.durationMs > 0) {
            std::snprintf(meta, sizeof(meta), "failed \xc2\xb7 %.1fs", a.durationMs / 1000.0);
            metaS = meta;
        } else if (hardFail) {
            metaS = "failed";
        } else if (a.durationMs > 0) {
            std::snprintf(meta, sizeof(meta), "%.1fs", a.durationMs / 1000.0);
            metaS = meta;
        }
    }
    if (!metaS.empty()) {
        ImGui::SameLine(0, 8 * sc);
        ImGui::PushStyleColor(ImGuiCol_Text, guard ? guardTone
                                           : hardFail ? failTone : surf.textSecondary);
        ImGui::TextUnformatted(metaS.c_str());
        ImGui::PopStyleColor();
    }
    // Per-call rewind: restore the part to the state BEFORE this individual tool call.
    // Self-gating — it draws nothing unless exactly one checkpoint matches this call
    // ordinal AND tool name, so read-only calls and pre-checkpoint history get no glyph.
    // A grouped card covers several calls, so it never offers one (callI arrives as -1).
    if (groupN == 1) drawAiVcCallRewind(st, callI, a.title);

    // ---- the human target(s) — "what it acts on".
    if (groupN > 1) {
        // Each folded member's own target, so a run of three never hides which three.
        std::vector<std::string> tgts;
        tgts.reserve(group->size());
        for (const ActivityItem* mi : *group) {
            std::string t = ui::humanTarget(mi->reason);
            if (!t.empty()) tgts.push_back(std::move(t));
        }
        constexpr size_t kMaxTargetLines = 6;
        for (size_t k = 0; k < tgts.size() && k < kMaxTargetLines; ++k) dimText(tgts[k].c_str());
        if (tgts.size() > kMaxTargetLines)
            dimText(("+" + std::to_string(tgts.size() - kMaxTargetLines) + " more").c_str());
    } else {
        const std::string tgt = ui::humanTarget(a.reason);
        if (!tgt.empty()) dimText(tgt.c_str());
    }

    // ---- bottom meta line: call timestamp + prompt-cache read (META trailer),
    // right-aligned like the user bubble's own meta bar. Kept OFF the header row:
    // the title/(details)/meta/rewind chain already fills it, and a right-aligned
    // jump there either collided or clipped under the log's scrollbar. A grouped
    // row shows its LAST member's stamp (the most recent call in the fold).
    double metaTs = parts.ts;
    int metaCache = parts.cacheRead;
    if (groupN > 1) {
        const ToolRowDetail tailParts = splitToolRowDetail(group->back()->detail);
        metaTs = tailParts.ts;
        metaCache = tailParts.cacheRead;
    }
    if (!simple && (metaTs > 0.0 || metaCache > 0)) {
        std::string tail;
        if (metaTs > 0.0) tail = chatRowClock(metaTs);
        if (metaCache > 0) {
            if (!tail.empty()) tail += " \xc2\xb7 ";
            tail += "cache " + chatTokCount(metaCache);
        }
        if (!tail.empty()) {
            const float tailW = ImGui::CalcTextSize(tail.c_str()).x;
            const float rx = ImGui::GetWindowContentRegionMax().x - tailW;
            if (rx > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(rx);
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextUnformatted(tail.c_str());
            ImGui::PopStyleColor();
            if (ImGui::IsItemHovered() && metaCache > 0)
                ImGui::SetTooltip("%d tokens read from the prompt cache by the completion\n"
                                  "that made this call (billed at the cached-input rate).",
                                  metaCache);
        }
    }

    if (guard) {
        // What happened, in the user's terms — collapsed AND expanded, because it is the row's
        // label, not its payload. The model-facing guidance text appears only in the drawer, so
        // the same sentence is never printed twice on one card.
        ImGui::PushStyleColor(ImGuiCol_Text, guardTone);
        ImGui::TextWrapped("Identical call repeated — the harness skipped it and asked the model "
                           "to try another approach. The tool never ran.");
        ImGui::PopStyleColor();
    }
    if (simple) {
        // No drawer, but a render the agent looked at must still reach the user.
        if (groupN > 1)
            for (const ActivityItem* mi : *group) drawArtifacts(splitToolRowDetail(mi->detail).arts);
        else
            drawArtifacts(artsBlob);
    }
    if (openE > 0.0f && hasDrawer) {
        CollapseBody cb = beginCollapseBody(animKey, openE, "##toolanim");
        if (groupN > 1) {
            // Grouped: every member's drawer stacked, each under its own dim target line.
            // Only successes ever group (toolPhrase hands failures an empty groupKey), so
            // there is no per-member failure chrome to reproduce here.
            int memberI = 0;
            for (const ActivityItem* mi : *group) {
                ImGui::PushID(memberI++);
                const ToolRowDetail mp = splitToolRowDetail(mi->detail);
                const std::string t = ui::humanTarget(mi->reason);
                if (!t.empty()) dimText(t.c_str());
                if (!mp.args.empty())
                    drawToolDetailBox("##tooldetail", stripMdMarks(mp.args), nullptr,
                                      ui::metrics::copilotProseMaxH(), /*failure=*/false);
                drawArtifacts(mp.arts);
                ImGui::PopID();
            }
        } else {
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
                drawToolDetailBox("##toolerr", errBody, fonts.mono,
                                  ui::metrics::px(200.0f), /*failure=*/true);
            }
            // The guard's own text quotes the repeated arguments verbatim ("…with the same
            // arguments ({})"), so a second box holding that same "{}" is noise — drop it in that
            // case only. Longer args, which the guard summarizes and truncates, still get their
            // box; either way right-click Copy on the row yields the whole raw detail.
            if (!argsBody.empty() &&
                !(guard && errBody.find(argsBody) != std::string::npos))
                drawToolDetailBox("##tooldetail", stripMdMarks(argsBody), nullptr,
                                  ui::metrics::copilotProseMaxH(), /*failure=*/false);
            drawArtifacts(artsBlob);
        }
        endCollapseBody(cb);
    }
    ImGui::EndChild();
    ImGui::PopStyleVar(2);   // ##toolrow compact ItemSpacing + WindowPadding
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
    // Standalone caller (outside the turn timeline): never the live frontier -> collapsed,
    // and with no turn/call ordinal there is no per-call rewind to offer.
    sanitizeActivityItem(st, a);
    drawToolCallRowImpl(st, a, fonts, /*failCount=*/1, /*liveFrontier=*/false);
}

// ---- transcript scroll anchors -----------------------------------------------------------------
// The ONE anchor mechanism the run receipt's failure list and the cost-attribution strip share.
// The scroll is DEFERRED rather than applied at click time because of the render-windowing cache
// (ChatMsg::uiCachedH): when the footer is clicked, the target row may not be submitted this frame
// at all — the message above it can still be collapsed to a spacer. So a request survives until
// the row actually draws and consumes it, which also makes it correct across a scroll-height
// change. Exactly one request is pending at a time; a second click simply retargets.
namespace {
ImGuiID g_pendingAnchor = 0;

// Consumed by the timeline as it submits each row. Scrolls only the row that matches, and only
// once — clearing the request here is what keeps the view from fighting the user afterwards.
void consumeAnchorScroll(ImGuiID rowAnchor) {
    if (!g_pendingAnchor || g_pendingAnchor != rowAnchor) return;
    g_pendingAnchor = 0;
    // Bias toward the top of the viewport rather than dead centre: the interesting content of a
    // card is its header, and a tall card centred by its middle would put that header offscreen.
    ImGui::SetScrollFromPosY(ImGui::GetCursorPosY(), 0.15f);
}
}  // namespace

ImGuiID activityAnchorId(int activityIdx) {
    // Keyed the way the timeline keys its rows: chat_log has already pushed the message index, and
    // the timeline pushes the activity index per row, so deriving the id from the SAME scope makes
    // a request raised in the turn footer resolve to the row the timeline will draw.
    return ImGui::GetID((void*)(intptr_t)(activityIdx + 1));
}

void timelineScrollTo(ImGuiID anchor) { g_pendingAnchor = anchor; }

bool activityIsGuardRefusal(const ActivityItem& a) { return toolRowIsGuardRefusal(a); }

// ---- the run spine -----------------------------------------------------------------------------
namespace {
// One status mark on the rail. Collected during the row loop and painted afterwards, because the
// rail's extent is only known once every row has landed.
struct SpineMark {
    float           y;
    ui::kit::Status st;
};

// RAII guard around an ImDrawListSplitter. Merging is the whole point: an unmerged splitter does
// not fail loudly, it silently corrupts every subsequent primitive in the window, which is a
// spectacularly confusing failure mode to debug. The destructor merges if the caller has not, so
// no early return — or an exception out of a card painter — can leak one.
class ChannelSplit {
public:
    ChannelSplit(ImDrawList* dl, int channels) : dl_(dl) {
        if (!dl_) return;
        split_.Split(dl_, channels);
        active_ = true;
    }
    ChannelSplit(const ChannelSplit&) = delete;
    ChannelSplit& operator=(const ChannelSplit&) = delete;
    ~ChannelSplit() { merge(); }
    void setChannel(int c) {
        if (active_) split_.SetCurrentChannel(dl_, c);
    }
    void merge() {
        if (!active_) return;
        active_ = false;
        split_.Merge(dl_);
    }

private:
    ImDrawList*        dl_ = nullptr;
    ImDrawListSplitter split_;
    bool               active_ = false;
};

// What this row's mark says. Deliberately the SAME remap the row's own glyph applies (a hard
// repeated failure reads as retry, a loop-guard refusal as skipped rather than failed) so the
// gutter can never disagree with the card beside it. `frontier` is the live row of a running turn:
// its mark spins even when the event stream has not stamped a status yet, because something IS
// happening — a hollow pending dot there would be a lie.
ui::kit::Status spineStatusOf(const ActivityItem& a, bool turnRunning, bool frontier) {
    if (a.status == "fail")
        return toolRowIsGuardRefusal(a) ? ui::kit::Status::Skipped : ui::kit::Status::Failed;
    if (a.status == "running" || (turnRunning && frontier)) return ui::kit::Status::Running;
    if (a.status == "done" || a.status == "ok" || a.status == "pass") return ui::kit::Status::Done;
    if (a.status == "skipped") return ui::kit::Status::Skipped;
    if (a.status == "retry") return ui::kit::Status::Retry;
    return ui::kit::Status::Pending;
}

// Paint the rail and its marks. Called with the splitter on the BACK channel so the hairline sits
// under the cards no matter what order they were submitted in.
//
// The rail spans mark-to-mark, never the whole turn height: that is what makes it survive the
// grouped-tool fold, the collapse animations and the synthesized "Session files" presentation row.
// A row that drew nothing contributed no mark, so the rail simply does not reach it — it can never
// draw a segment to a step that is not on screen.
void paintRunSpine(ImDrawList* dl, float railX, float cell,
                   const std::vector<SpineMark>& marks, bool running) {
    if (!dl || marks.size() < 1) return;
    const ui::AdaptiveSurface& surf = chatSurf();
    const float r      = cell * 0.5f;
    const float thick  = ui::metrics::stroke(1.0f);
    const float top    = marks.front().y;
    const float bottom = marks.back().y;

    if (bottom > top) {
        // The track: a hairline in the tertiary ink, so it reads as structure rather than content.
        ImVec4 track = surf.textTertiary;
        // 0.75, not 0.55: pixel-measured against the light glass surface, 0.55 put only ~30/255
        // between the rail and the panel — below the point where a hairline reads as a deliberate
        // connector rather than a compression artifact.
        track.w *= 0.75f;
        dl->AddLine(ImVec2(railX, top), ImVec2(railX, bottom),
                    ImGui::ColorConvertFloat4ToU32(track), thick);

        // The LIVE segment: from the last completed mark down to the running one, marching
        // downward. This is the whole point of the spine — "how far into the turn is the agent"
        // answerable at a glance. Only drawn while a turn is actually running, so a finished
        // transcript is completely static and requests no frames.
        if (running) {
            size_t runIdx = marks.size();
            for (size_t i = 0; i < marks.size(); ++i)
                if (marks[i].st == ui::kit::Status::Running) { runIdx = i; break; }
            if (runIdx < marks.size()) {
                const float segTop = runIdx == 0 ? top : marks[runIdx - 1].y;
                const float segBot = marks[runIdx].y;
                if (segBot > segTop) {
                    // A short bright head sliding down the segment, rather than pulsing the whole
                    // length: motion with a direction reads as progress, motion without one reads
                    // as an error state.
                    const float span = segBot - segTop;
                    const float head = ImMin(span, cell * 1.6f);
                    const float p    = ui::anim::phase(ui::anim::kMarch);
                    const float hy   = segTop + p * span;
                    const ImU32 hot  = ui::kit::toneU32(ui::kit::Tone::Accent, 0.85f);
                    const ImU32 cold = ui::kit::toneU32(ui::kit::Tone::Accent, 0.0f);
                    const int   v0   = dl->VtxBuffer.Size;
                    dl->AddLine(ImVec2(railX, ImMax(segTop, hy - head)), ImVec2(railX, hy), hot,
                                ui::metrics::stroke(2.0f));
                    ImGui::ShadeVertsLinearColorGradientKeepAlpha(
                        dl, v0, dl->VtxBuffer.Size, ImVec2(railX, ImMax(segTop, hy - head)),
                        ImVec2(railX, hy), cold, hot);
                }
            }
        }
    }
    for (const SpineMark& mk : marks)
        ui::kit::statusGlyph(dl, ImVec2(railX, mk.y), r, mk.st, surf);
}
}  // namespace


void drawAgentTimeline(AppState& st, ChatMsg& m, const ui::Fonts& fonts, float logW,
                              bool showThinking, bool isLastTurn) {
    if (m.activity.empty()) return;
    // ONE rhythm for the whole run (metrics.h:93): a streaming conversation is denser than a
    // docked menu, and every kit widget any row draws — here, or in the corp/firm/swarm family
    // cards.cpp draws for us — must resolve its padding from the SAME density or the stack
    // alternates rhythms row to row, which is the uneven spacing a live session called trash.
    ui::kit::pushDensity(ui::kit::Density::Compact);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, ui::metrics::cpChatRowGap()));
    const bool turnRunning = m.running;
    if (!turnRunning) {
        // Stale live rows on a finished turn (restored history, terminal events dropped after
        // a stop) would animate forever — clamp them once here, idempotently. Thinking/thought
        // rows also collapse once the turn is done: nothing is still "filling out", and an
        // open reasoning box per turn floods scrollback (the user re-opens with one click).
        for (auto& a : m.activity) {
            if (a.streaming) a.streaming = false;
            if (a.status == "running") a.status = "done";
            if (!a.userToggled && (a.rowKind == 2 || isThinkingItem(a))) a.expanded = false;
        }
    }
    // Resolve opaque doc ids to part names and fold uncovered glyphs before ANY row is measured
    // or drawn — one place, every row kind, and the widths computed below are the final ones.
    for (auto& a : m.activity) sanitizeActivityItem(st, a);
    // No blanket leading Dummy here: EVERY dispatched row (both the local draw* functions below
    // and cards.cpp's corp/firm/swarm family) already opens with its own cpChatRowGap() Dummy —
    // one used to sit here too, doubling the gap above the first card in every turn.
    // The live frontier: the most recent row this loop will actually draw (skip conditions
    // below). Only that row auto-expands mid-turn; earlier rows collapse to summary+icon.
    size_t lastVisible = (size_t)-1;
    for (size_t i = 0; i < m.activity.size(); ++i) {
        const ActivityItem& a = m.activity[i];
        if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
        if (a.rowKind == 10 && a.stage == "class:pending") continue;
        if (isProseOutputRow(a) && !ui::streamTextVisible(a.detail)) continue;
        lastVisible = i;
    }

    // ---- the run spine -------------------------------------------------------------------
    // A continuous hairline down the left gutter of the turn with each step's status mark ON it,
    // so "how far into this run is the agent" is answerable across the room. The rail is derived
    // ENTIRELY from where rows actually landed this frame, which is what makes it survive the
    // grouped-tool fold, the collapse animations and the synthesized "Session files" row: a row
    // that did not draw contributes no mark, and the rail spans mark-to-mark, so it can never
    // run to a step that is not on screen.
    //
    // It paints into a BACK channel of the window draw list so it sits under every card
    // regardless of submission order. ChannelSplit merges in its destructor, so every exit from
    // this function — the normal path, and a throw out of any card painter — leaves the draw
    // list intact; an unmerged splitter silently corrupts the whole window.
    const float spineCell = ImGui::GetTextLineHeight();          // the status-mark glyph cell
    const float spineW    = spineCell + ui::kit::padX();         // gutter the rows indent past
    const float railX     = ImGui::GetCursorScreenPos().x + spineCell * 0.5f;
    std::vector<SpineMark> spineMarks;
    spineMarks.reserve(m.activity.size());
    ImDrawList* const spineDl = ImGui::GetWindowDrawList();
    ChannelSplit spine(spineDl, 2);
    spine.setChannel(1);
    ImGui::Indent(spineW);
    // Tool-call ordinal within the turn — the `i` of an AI-VC "call:<turn>:<i>" target.
    // Counted over rowKind-3 rows only (the timeline also carries thinking, context and
    // card rows), and NOT over the synthesized "Session files" artifact row, which is a
    // presentation row rather than a call the engine checkpointed.
    int toolOrdinal = -1;
    for (size_t i = 0; i < m.activity.size(); ++i) {
        ActivityItem& a = m.activity[i];
        // Hidden meta row carrying stop_reason / error_detail — never drawn as a card.
        // Provider-call rows (rowKind 9) aggregate into the collapsible Provider log.
        // Pending routing rows (rowKind 10, "class:pending") stay hidden until a real
        // role change (agent.role) reveals them — first-turn default routing shows no card.
        if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
        if (a.rowKind == 10 && a.stage == "class:pending") continue;
        const bool liveFrontier = turnRunning && (i == lastVisible);
        // Deferred scroll-to-anchor: the receipt / cost strip requested this row LAST frame, when
        // the render-windowing cache may still have had the message collapsed to a spacer.
        // Resolved BEFORE PushID((int)i) so the id matches activityAnchorId's — that helper is
        // documented to be called from the turn footer, i.e. the message scope, not the row scope.
        // The cursor is at the row's top edge here, which is the position we want to bring into
        // view; a tall card centred on its own middle would push its header offscreen.
        consumeAnchorScroll(activityAnchorId((int)i));
        ImGui::PushID((int)i);
        // Where this row's status mark sits on the rail, and what it says. Captured around the
        // dispatch so a row that draws nothing (an empty prose segment) leaves no orphan mark.
        const float rowTopY = ImGui::GetCursorScreenPos().y;
        const ui::kit::Status rowStatus = spineStatusOf(a, turnRunning, i == lastVisible);
        const bool onSpine = !isProseOutputRow(a) && a.rowKind != 1;
        if (isProseOutputRow(a)) {
            // The model's own words, in the column at the point it said them.
            drawProseBubble(a, fonts);
        } else if (a.rowKind == 1) {
            drawContextChip(a, fonts);
        } else if (a.rowKind == 2) {
            drawThoughtBox(a, fonts, showThinking, liveFrontier);
        } else if (a.rowKind == 3) {
            // TWO independent folds, and they can never collide: tool_phrase.h invariant 1 gives
            // a FAILED call an empty groupKey, so the similarity fold below only ever sees
            // successes while the identical-failure fold below only ever sees failures.
            //
            // Collapse consecutive identical tool+target failures into one row with a count
            // (stops five identical amber cards stacking during a repair loop). Show the
            // last item in the run so the visible error_preview is the most recent.
            int failCount = 1;
            size_t drawIdx = i;
            size_t frontIdx = i;     // the run's LAST row decides live-frontier auto-expansion
            std::vector<ActivityItem*> group;
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
                frontIdx = j - 1;
                i = j - 1;  // loop ++i advances past the coalesced run
            } else if (ui::chatGroupToolCalls() && a.status != "running" &&
                       a.stage != "artifacts") {
                // Fold a run of consecutive successful calls that phrase to the same groupKey
                // into ONE card ("Rendering the part x 3") instead of N near-identical cards.
                const std::string key = ui::toolPhrase(a.title, a.reason, true).groupKey;
                if (!key.empty()) {
                    size_t j = i + 1;
                    while (j < m.activity.size()) {
                        const ActivityItem& n = m.activity[j];
                        if (n.rowKind != 3 || n.status == "fail" || n.status == "running") break;
                        if (n.stage == "artifacts") break;
                        if (ui::toolPhrase(n.title, n.reason, true).groupKey != key) break;
                        ++j;
                    }
                    if (j > i + 1) {
                        group.reserve(j - i);
                        for (size_t k = i; k < j; ++k) group.push_back(&m.activity[k]);
                        frontIdx = j - 1;
                        i = j - 1;   // loop ++i advances past the folded run
                    }
                }
            }
            // This row's call ordinal. The synthesized "Session files" row is presentation,
            // not a checkpointed call, so it never consumes an index. A coalesced run of
            // identical failures — or a folded group of similar successes — draws ONE row but
            // consumed that many calls, so the ordinal advances by all of them.
            int callIdx = -1;
            if (a.stage != "artifacts") {
                callIdx = ++toolOrdinal;
                if (failCount > 1) toolOrdinal += failCount - 1;
                if (group.size() > 1) toolOrdinal += (int)group.size() - 1;
            }
            // A grouped card must never offer a rewind glyph pointing at an arbitrary member;
            // the identical-failure fold still rewinds to before the FIRST attempt. The most
            // recent row of the whole chat (the live frontier of the LAST turn) offers none
            // either: it IS the current state — there is nothing to rewind to.
            const bool chatFrontier = isLastTurn && (frontIdx == lastVisible);
            const int rewindIdx = (group.size() > 1 || chatFrontier) ? -1 : callIdx;
            drawToolCallRowImpl(st, m.activity[drawIdx], fonts, failCount,
                                turnRunning && (frontIdx == lastVisible), rewindIdx,
                                group.size() > 1 ? &group : nullptr, onSpine);
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
            drawActionCard(a, nullptr, fonts, logW, liveFrontier, onSpine);
        }
        // The mark's y: the row's own header line, not its centre — a card with a long open
        // drawer would otherwise float its mark halfway down the rail, away from the title it
        // belongs to. Clamped inside the row so a one-line row can never push it outside.
        if (onSpine) {
            const float rowBotY = ImGui::GetCursorScreenPos().y;
            if (rowBotY > rowTopY) {
                const float half = spineCell * 0.5f;
                float my = rowTopY + ui::metrics::cpChatRowGap() + ui::kit::padY() + half;
                const float lo = rowTopY + half;
                const float hi = ImMax(lo, rowBotY - half);
                spineMarks.push_back(SpineMark{ImClamp(my, lo, hi), rowStatus});
            }
        }
        ImGui::PopID();
    }
    ImGui::Unindent(spineW);
    spine.setChannel(0);
    paintRunSpine(spineDl, railX, spineCell, spineMarks, turnRunning);
    spine.merge();   // explicit, ahead of the guard, so the rail is in the list before PopStyleVar
    ImGui::PopStyleVar();   // timeline-wide compact ItemSpacing
    ui::kit::popDensity();
}

// Forward: user bubble + rewind/edit actions (defined after AI-VC helpers).

}  // namespace ui
