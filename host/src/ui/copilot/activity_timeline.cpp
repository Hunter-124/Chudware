// Copilot activity timeline: the per-turn typed-block
// timeline — thinking blocks, action cards, the inline transparency rows
// (context chips, thought boxes, tool-call rows), artifact file actions, and
// drawAgentTimeline which sequences a turn's ActivityItems. Cards it delegates to live in
// cards.cpp; the items themselves are built in feed.cpp.

#include "copilot_internal.h"
#include "agent_text.h"
#include "stream_rows.h"              // ui::streamTextVisible — a blank prose box draws nothing
#include "tool_outcome.h"
#include "ui/copilot/tool_phrase.h"   // human phrasing + grouping for raw MCP tool symbols
#include "ui/ai/ai_prefs.h"           // cached chat-display prefs (Settings -> AI -> Harness)
#include "gl_compat.h"                // glDeleteTextures — bounded artifact texture cache
#include "util/time_local.h"
#include "ui/file_dialog.h"

namespace ui {

// ---- agent-authored text sanitizers (agent_text.{h,cpp}) -------------------------------------
// Two live defects, both on strings the copilot did NOT author in C++: a tool-call card printed
// the opaque engine id ("doc-cpp00000039") where the part's NAME belongs, and the U+2192 arrows
// services/agent/agent_events.py joins tool targets with rendered as notdef '?' (the atlas body
// table, ui/glyph_ranges.h kBody, never requests the arrows block). Fixed HERE rather than at
// ingest because feed.cpp has no AppState — the doc-id -> name lookup needs the tab list — and
// because a transcript restored from disk never passes through feed.cpp at all.
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

// A FAILED tool row is frozen to exactly what the harness wrote. The in-place pass above runs
// every frame against the CURRENT tab map, so an id the call could not resolve got rewritten the
// moment any later document took that number: after an app restart the agent passed its stale
// "doc-cpp00000003", the render failed "no such document 'doc-cpp00000003'", a re-import then
// minted a new doc-cpp00000003, and the card turned into "no such document 'AI: upper_arm_revD'
// … Open parts: sixaxis=\"sixaxis\"" — a tab label the agent never sent, reported as missing
// while it sat open (2026-09-28, c221). A failure is a diagnostic; its ids stay verbatim.
static void sanitizeActivityItem(AppState& st, ActivityItem& a) {
    if (a.rowKind > 3) return;
    if (a.rowKind == 3 && a.status == "fail") {
        for (std::string* s : {&a.reason, &a.detail, &a.title})
            if (!s->empty() && ui::agentTextMayChange(*s)) *s = ui::foldToAtlasGlyphs(*s);
        return;
    }
    // A tool row's reason is its TARGET ("AI: surface"), a compact label on the row's title
    // line: the "(doc-cpp00000002)" parenthetical is engine noise there, exactly as in the args
    // JSON. Prose rows keep it (it reads well inside a sentence).
    sanitizeAgentTextInPlace(st, a.reason, /*jsonBody=*/a.rowKind == 3);
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
        // ui::anim::dt() rather than raw DeltaTime: a debugger stall or a blocking RPC would
        // otherwise teleport the drawer to its end state on the resume frame. requestFrames() is
        // what keeps the ease smooth instead of stepping at the 20 Hz idle floor — and it is
        // asked for ONLY while moving, so a settled transcript still idles.
        const float step = ui::anim::dt() / ui::anim::kBase;
        c.t = (c.t < target) ? ImMin(target, c.t + step) : ImMax(target, c.t - step);
        ui::anim::requestFrames();
    }
    return ui::anim::easeInOut(c.t);
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

// ---- row silhouettes -------------------------------------------------------------------------
// Weight tracks importance, and the shell is what encodes it — rounded, soft, never a hard bar:
//
//   the reply                open prose, full width (drawProseBubble) — no box at all
//   orchestrator stage card  a faint tone wash fading toward its foot + a soft same-tone hairline
//   thinking / thought box   one dim italic line; a hover pill once it has something to disclose
//   tool call                one dense line; a hover pill, a faint fill while its drawer is open
//   raw ops payload          one dim mono line with the same disclosure
//   anything that failed     a soft rounded Tone::Err tint + hairline (a refusal: Tone::Info)
//
// Status lives in the run spine's gutter, so no row carries a coloured rail any more: a rail per
// step was the "hard red bar" that made a failed call read like a broken layout.
//
// Measure-then-fill exactly like beginGlassBubble: the body draws on a front channel and the shell
// is painted behind it once the child's true height is known, so an auto-height row needs no
// second frame. beginRowShell owns the child AND the style stack it pushed; endRowShell must run
// on every path out.
struct RowShell {
    ImDrawList*   dl = nullptr;
    ImVec2        origin{};
    float         width = 0.0f;
    ui::kit::Tone tone  = ui::kit::Tone::Neutral;
    float         wash  = 0.0f;   // resting fill alpha; 0 = no fill
    float         edge  = 0.0f;   // resting hairline alpha; 0 = no hairline
    float         openE = 0.0f;   // drawer open factor: an open row keeps a faint fill
    bool          interactive = false;   // hover pill (the header toggles a drawer)
    ImGuiID       hotId = 0;
};

// Shell alphas, named once so the hierarchy is legible in one place rather than as magic numbers
// scattered through the painters.
static constexpr float kStageWash  = 0.06f;   // stage-card fill — present, but well under glass
static constexpr float kStageEdge  = 0.14f;   // stage-card hairline: an edge, not a border
static constexpr float kFailWash   = 0.09f;   // exceptional tool row: a soft tint, never a bar
static constexpr float kFailEdge   = 0.24f;
static constexpr float kHoverWash  = 0.09f;   // pointer over a disclosable row
static constexpr float kHoverEdge  = 0.10f;
static constexpr float kOpenWash   = 0.05f;   // a row whose drawer is open stays grouped
static constexpr float kTintFall   = 0.40f;   // fill fades this much from a shell's top to foot
static constexpr float kItalicShear = 0.20f;  // faux-italic slant (x shift per px of height)

// A log row's vertical inset: three quarters of the density's card pad. Consecutive tool rows are
// one log, and the full card pad doubled the pitch of a 90-call turn for no information.
static float rowPadY() { return ui::kit::padY() * 0.75f; }

// Rounded fill whose alpha falls off toward the foot — the "softly lit from above" read of a
// Fluent/macOS surface — plus an optional hairline. The falloff rescales vertex alpha in place
// (AA fringe vertices are already transparent, so they stay so).
static void paintSoftShell(ImDrawList* dl, const ImRect& r, float rnd, ImU32 fill, ImU32 edge) {
    if (fill & IM_COL32_A_MASK) {
        const int   v0 = dl->VtxBuffer.Size;
        dl->AddRectFilled(r.Min, r.Max, fill, rnd);
        const float h  = ImMax(r.GetHeight(), 1.0f);
        for (int i = v0; i < dl->VtxBuffer.Size; ++i) {
            ImDrawVert& v = dl->VtxBuffer[i];
            const float t = ImClamp((v.pos.y - r.Min.y) / h, 0.0f, 1.0f);
            const float a = (float)((v.col & IM_COL32_A_MASK) >> IM_COL32_A_SHIFT);
            v.col = (v.col & ~IM_COL32_A_MASK) |
                    ((ImU32)(a * (1.0f - kTintFall * t)) << IM_COL32_A_SHIFT);
        }
    }
    if (edge & IM_COL32_A_MASK) dl->AddRect(r.Min, r.Max, edge, rnd, 0, ui::metrics::stroke(1.0f));
}

static RowShell beginRowShell(const char* id, ui::kit::Tone tone, float wash, float edge,
                              bool interactive = false, float openE = 0.0f,
                              bool measureOffscreen = false) {
    RowShell sh;
    sh.dl     = ImGui::GetWindowDrawList();
    sh.origin = ImGui::GetCursorScreenPos();
    sh.width  = ImGui::GetContentRegionAvail().x;
    sh.tone   = tone;
    sh.wash   = wash;
    sh.edge   = edge;
    sh.openE  = openE;
    sh.interactive = interactive;
    sh.hotId  = ImHashStr("##row_hot", 0, ImGui::GetID(id));
    sh.dl->ChannelsSplit(2);
    sh.dl->ChannelsSetCurrent(1);
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(ui::kit::padX(), rowPadY()));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, ui::kit::rowGap()));
    ImGui::BeginChild(id, ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding |
                      (measureOffscreen ? ImGuiChildFlags_AlwaysAutoResize : ImGuiChildFlags_None));
    return sh;
}

static ImRect endRowShell(const RowShell& sh) {
    ImGui::EndChild();
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor();
    ImVec2 p1 = ImGui::GetItemRectMax();
    if (p1.x < sh.origin.x + sh.width) p1.x = sh.origin.x + sh.width;
    const ImRect r(sh.origin, p1);
    float hot = 0.0f;
    if (sh.interactive) {
        const bool hov = ImGui::IsMouseHoveringRect(r.Min, r.Max) &&
                         ImGui::IsWindowHovered(ImGuiHoveredFlags_ChildWindows);
        hot = ui::anim::toward(sh.hotId, hov ? 1.0f : 0.0f, ui::anim::kFast);
    }
    sh.dl->ChannelsSetCurrent(0);
    const float fillA = sh.wash + kHoverWash * hot + kOpenWash * sh.openE;
    const float edgeA = sh.edge + kHoverEdge * hot;
    paintSoftShell(sh.dl, r, ui::kit::rowRound(),
                   fillA > 0.0f ? ui::kit::toneU32(sh.tone, fillA) : 0u,
                   edgeA > 0.0f ? ui::kit::toneU32(sh.tone, edgeA) : 0u);
    sh.dl->ChannelsMerge();
    return r;
}

// The tone for a row, from the same status vocabulary the gutter spine mark uses — so a red mark
// in the gutter can never sit beside an amber shell. A plain COMPLETED step gets Neutral rather
// than Tone::Ok on purpose: the gutter already carries its check, and eight green shells down a
// finished turn is a wall of chroma that says nothing.
static ui::kit::Tone rowToneOf(const std::string& status, bool guardRefusal) {
    if (status == "fail" || status == "error")
        return guardRefusal ? ui::kit::Tone::Info : ui::kit::Tone::Err;
    if (status == "running")  return ui::kit::Tone::Accent;
    if (status == "retry")    return ui::kit::Tone::Warn;
    if (status == "skipped")  return ui::kit::Tone::Info;
    return ui::kit::Tone::Neutral;
}

// ---- row header vocabulary -----------------------------------------------------------------------
// Every disclosable row shares ONE header grammar: [icon] title  dim target ...... meta [chevron].
// The chevron lane is reserved on every row (drawer or not) so the right-aligned numbers form one
// column down the whole turn.

// Where the header line's centre landed, for the run spine: each row painter records the FIRST
// header it draws, so the gutter mark sits on the title's centre line rather than on a guessed
// offset from the row's top. drawAgentTimeline resets it before every row.
static float g_rowHeadY = -1.0f;
static void noteRowHead() {
    if (g_rowHeadY >= 0.0f) return;
    g_rowHeadY = (ImGui::GetItemRectMin().y + ImGui::GetItemRectMax().y) * 0.5f;
}

// Right-hand lanes of a header line, in window-local x. The chevron cell sits flush right, inset by
// half a pad so the pane's overlaid pill scrollbar can never cover it; meta right-aligns against it.
struct RowLanes {
    float chevCx    = 0.0f;
    float metaRight = 0.0f;
};
static RowLanes rowLanes(bool chevLane) {
    const float right = ImGui::GetContentRegionMax().x - ui::kit::padX() * 0.5f;
    const float lane  = chevLane ? ImGui::GetTextLineHeight() : 0.0f;
    RowLanes l;
    l.chevCx    = right - lane * 0.5f;
    l.metaRight = right - lane - (chevLane ? ui::metrics::U(0.5f) : 0.0f);
    return l;
}

// Disclosure chevron: a stroked right-pointing chevron that rotates to point down as the drawer
// opens, so ONE shape carries the state continuously. Shared with chat_log.cpp's provider log.
void chatPaintDisclosure(ImDrawList* dl, ImVec2 c, float r, float openT, ImU32 col) {
    const float ang = ImClamp(openT, 0.0f, 1.0f) * IM_PI * 0.5f;
    const float ca = ImCos(ang), sa = ImSin(ang);
    auto rot = [&](float x, float y) {
        return ImVec2(c.x + x * ca - y * sa, c.y + x * sa + y * ca);
    };
    const ImVec2 pts[3] = {rot(-r * 0.30f, -r * 0.62f), rot(r * 0.32f, 0.0f),
                           rot(-r * 0.30f, r * 0.62f)};
    dl->AddPolyline(pts, 3, col, ImDrawFlags_None, ui::metrics::stroke(1.5f));
}

// The whole header line is the disclosure's hit target (no "(details)" link to aim at). A real
// control on the line — the per-call rewind glyph — keeps its own click. Returns {hovered, clicked}.
struct HeaderHit {
    bool hovered = false;
    bool clicked = false;
};
static HeaderHit headerHit(float topY, float botY) {
    HeaderHit h;
    const ImVec2 wp = ImGui::GetWindowPos();
    const ImVec2 mn(wp.x, topY - rowPadY());
    const ImVec2 mx(wp.x + ImGui::GetWindowSize().x, botY);
    if (!ImGui::IsWindowHovered(ImGuiHoveredFlags_ChildWindows) ||
        !ImGui::IsMouseHoveringRect(mn, mx) || ImGui::IsAnyItemHovered())
        return h;
    h.hovered = true;
    ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    h.clicked = ImGui::IsMouseClicked(ImGuiMouseButton_Left);
    return h;
}

// Paint the chevron in the lane at the header's centre line. Ink lifts while the header is hot.
static void headerChevron(const RowLanes& lanes, float centerY, float openE, bool hot) {
    const ui::AdaptiveSurface& surf = chatSurf();
    const float r = ImGui::GetTextLineHeight() * 0.42f;
    chatPaintDisclosure(ImGui::GetWindowDrawList(),
                        ImVec2(ImGui::GetWindowPos().x + lanes.chevCx, centerY), r, openE,
                        ImGui::ColorConvertFloat4ToU32(hot ? surf.textSecondary
                                                           : surf.textTertiary));
}

// A leading atlas icon, vertically centred on the text line. Does NOT SameLine itself.
static void rowIcon(const char* name, const ImVec4& tint) {
    const ui::IconUV iv = ui::getIcon(name, 20);
    if (!iv.valid) return;
    const float  lh = ImGui::GetTextLineHeight();
    const float  gz = lh * 0.86f;
    const ImVec2 p  = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(gz, lh));
    const float  y  = p.y + (lh - gz) * 0.5f;
    ImGui::GetWindowDrawList()->AddImage(iv.texId, ImVec2(p.x, y), ImVec2(p.x + gz, y + gz),
                                         iv.uv0, iv.uv1, ImGui::ColorConvertFloat4ToU32(tint));
}

// Small rounded count badge sized to the text line ("Read  [2]"), so a folded group never grows
// the row's height the way a full chip would.
static void countBadge(int n) {
    const ui::AdaptiveSurface& surf = chatSurf();
    char buf[16];
    std::snprintf(buf, sizeof(buf), "%d", n);
    const ImVec2 ts = ImGui::CalcTextSize(buf);
    const float  lh = ImGui::GetTextLineHeight();
    const float  w  = ImMax(lh, ts.x + ui::metrics::U(1.5f) * 2.0f);
    const ImVec2 p  = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(w, lh));
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->AddRectFilled(p, ImVec2(p.x + w, p.y + lh),
                      ui::kit::toneU32(ui::kit::Tone::Neutral, 0.16f), lh * 0.5f);
    dl->AddText(ImVec2(p.x + (w - ts.x) * 0.5f, p.y + (lh - ts.y) * 0.5f),
                ImGui::ColorConvertFloat4ToU32(surf.textSecondary), buf);
}

// Faux italic: the app ships no italic face, so the glyph quads a text item just submitted are
// sheared about the item's centre line. Wrap any text submission (plain or shimmer) in begin/end.
struct ItalicSpan {
    ImDrawList* dl = nullptr;
    int         v0 = 0;
};
static ItalicSpan italicBegin() {
    ItalicSpan s;
    s.dl = ImGui::GetWindowDrawList();
    s.v0 = s.dl->VtxBuffer.Size;
    return s;
}
static void italicEnd(const ItalicSpan& s) {
    const float midY = (ImGui::GetItemRectMin().y + ImGui::GetItemRectMax().y) * 0.5f;
    for (int i = s.v0; i < s.dl->VtxBuffer.Size; ++i) {
        ImDrawVert& v = s.dl->VtxBuffer[i];
        v.pos.x += (midY - v.pos.y) * kItalicShear;
    }
}
static void italicText(const char* text, const ImVec4& col) {
    const ItalicSpan s = italicBegin();
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::TextUnformatted(text);
    ImGui::PopStyleColor();
    italicEnd(s);
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

// C45: local models reply in markdown but ImGui renders plain text — the raw ** and ###
// marks read as noise. Strip the common inline marks (bold/italic asterisks, heading hashes,
// backticks) for DISPLAY only; the stored message keeps the original text.
// `srcOf` (optional) receives, per output byte, the index of the input byte it came from — the
// strip only DROPS bytes, so positions found in the raw text (magic keywords, which must be
// matched before the backticks that delimit code spans are gone) map straight across.
std::string stripMdMarks(const std::string& in, std::vector<size_t>* srcOf) {
    std::string out;
    out.reserve(in.size());
    if (srcOf) {
        srcOf->clear();
        srcOf->reserve(in.size());
    }
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
        if (srcOf) srcOf->push_back(i);
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

// The reasoning one-liner shared by drawThinkingBlock and drawThoughtBox: a dim italic label (the
// working sweep while it is live) and — while collapsed — the thought's first line, ellipsized, on
// the SAME line instead of a second one. The tooltip carries the untruncated preview line: an
// ellipsis with no way to read what it ate is a dead end. Returns the header's centre y.
static float thoughtHeader(const ui::Fonts& fonts, const char* label, bool live,
                           const std::string& preview, const RowLanes& lanes) {
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushFont(fonts.body);
    {
        const ItalicSpan s = italicBegin();
        if (live) {
            const std::string sweep = std::string(label) + "\xe2\x80\xa6";
            chatShimmerText(sweep.c_str(), surf.textTertiary, surf.textPrimary,
                            ui::anim::kBreathe);
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::TextUnformatted(label);
            ImGui::PopStyleColor();
        }
        italicEnd(s);
    }
    noteRowHead();
    const float centerY = (ImGui::GetItemRectMin().y + ImGui::GetItemRectMax().y) * 0.5f;
    if (!preview.empty()) {
        ImGui::SameLine(0, ui::metrics::U(1.5f));
        const float room = lanes.metaRight - ImGui::GetCursorPosX();
        if (room > ui::metrics::U(8.0f)) {
            const std::string fit = chatEllipsize(preview, room);
            italicText(fit.c_str(), surf.textTertiary);
            if (fit != preview && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", preview.c_str());
        }
    }
    ImGui::PopFont();
    return centerY;
}

// A collapsible, dimmed reasoning block. While it is the live frontier of the turn (or still
// streaming) it stays open so the thoughts stream; as soon as a later row lands or the turn
// finishes it auto-collapses to its one italic line — unless the user explicitly toggled it
// (userToggled always wins, both directions). `showThinking` off -> the label alone.
static void drawThinkingBlock(ActivityItem& a, const ui::Fonts& fonts, bool showThinking,
                              bool liveFrontier) {
    const char* label = a.title.empty() ? "Thinking" : a.title.c_str();
    if (!showThinking) {
        ImGui::Indent(ui::kit::padX());   // the rows' text column: one left edge for the timeline
        ImGui::PushFont(fonts.body);
        italicText(label, chatSurf().textSecondary);
        noteRowHead();
        ImGui::PopFont();
        ImGui::Unindent(ui::kit::padX());
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
    // Collapse animation: the body eases shut over anim::kBase instead of vanishing.
    const ImGuiID animKey = ImGui::GetID("##thinkblk_anim");
    const bool    wantOpen = open && hasDetail;
    const float   openE = ui::chatCollapseAnim() ? collapseEase(animKey, wantOpen)
                                                 : (wantOpen ? 1.0f : 0.0f);
    // The agent muttering: no fill, no border — one italic line. It must not compete with the
    // answer, and eight of these in a row must not read as eight decisions.
    RowShell sh = beginRowShell("##thinkblk", ui::kit::Tone::Neutral, 0.0f, 0.0f, hasDetail,
                                openE);
    const RowLanes lanes = rowLanes(true);
    const float    topY  = ImGui::GetCursorScreenPos().y;
    const std::string preview = (openE <= 0.0f && !replyBody.empty())
        ? stripMdMarks(replyBody.substr(0, replyBody.find('\n')))
        : std::string();
    const float centerY = thoughtHeader(fonts, label, a.streaming, preview, lanes);
    if (hasDetail) {
        const HeaderHit hh = headerHit(topY, ImGui::GetItemRectMax().y + rowPadY());
        if (hh.clicked) { a.expanded = !open; a.userToggled = true; }
        headerChevron(lanes, centerY, openE, hh.hovered);
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
    }

    ImRect thinkRect = endRowShell(sh);
    const std::string thinkCopy = chatActivityCopyText(a);
    chatCopyContext(thinkRect, "##think_copy", thinkCopy);

}

// An orchestrator build stage: status glyph + stage icon + human label + duration/ops meta, the
// verdict folded in as a result line, and an expandable drawer with the raw detail.
//
// Silhouette: a faint rounded tone wash fading toward its foot plus a soft same-tone hairline. The
// card is one beat in the run's story, not a document, so it is lit, never boxed or railed.
//
// `onSpine` = the run spine is drawing this row's status mark out in the left gutter, so the card
// must NOT draw its own leading glyph — two marks for one step reads as two steps. The card keeps
// drawing its own when it is rendered outside a timeline (drawToolCallRow's single-row entry).
void drawActionCard(ActivityItem& a, const ui::Fonts& fonts, bool liveFrontier, bool onSpine) {
    const auto& pal = ui::palette;
    const bool running = a.status == "running";
    const bool fail    = a.status == "fail";

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
    const bool hasDrawer = !replyBody.empty() || !sentBody.empty() || !altLines.empty();
    const ImGuiID animKey = ImGui::GetID("##actcard_anim");
    const bool    wantOpen = open && hasDrawer;
    const float   openE = ui::chatCollapseAnim() ? collapseEase(animKey, wantOpen)
                                                 : (wantOpen ? 1.0f : 0.0f);

    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    const ui::kit::Tone tone = rowToneOf(a.status, /*guardRefusal=*/false);
    RowShell sh = beginRowShell("##actcard", tone, kStageWash, kStageEdge, hasDrawer, openE);
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);   // base ink (dimText derives from it)

    // ---- header row: [status glyph] stage icon + label ...... meta [chevron] ----
    // The spine already carries this step's mark in the gutter; drawing a second one here would
    // read as two steps, and reserving its empty cell only wasted width.
    const RowLanes lanes = rowLanes(true);
    const float    topY  = ImGui::GetCursorScreenPos().y;
    const float    gap   = ui::metrics::U(1.5f);
    if (!onSpine) {
        drawStatusGlyph(a.status, ImGui::GetTextLineHeight());
        ImGui::SameLine(0, gap);
    }
    rowIcon(stageIconName(a.stage, a.missionHeader),
            running ? surf.textPrimary : surf.textSecondary);
    ImGui::SameLine(0, gap);
    char meta[96] = "";
    if (a.opsCount > 0 && a.durationMs > 0)
        std::snprintf(meta, sizeof(meta), "%d ops \xc2\xb7 %.1fs", a.opsCount, a.durationMs / 1000.0);
    else if (a.durationMs > 0)
        std::snprintf(meta, sizeof(meta), "%.1fs", a.durationMs / 1000.0);
    else if (a.opsCount > 0)
        std::snprintf(meta, sizeof(meta), "%d ops", a.opsCount);
    const float metaW = meta[0] ? ImGui::CalcTextSize(meta).x + gap : 0.0f;
    const ImVec4 titleCol = running ? semanticOnGlass(pal.accent, surf)
                          : fail    ? semanticOnGlass(pal.statusErr, surf)
                                    : surf.textPrimary;
    ImGui::PushFont(fonts.medium);
    const std::string titleFit = chatEllipsize(
        a.title, ImMax(lanes.metaRight - ImGui::GetCursorPosX() - metaW, ui::metrics::U(6.0f)));
    ImGui::TextColored(titleCol, "%s", titleFit.c_str());
    ImGui::PopFont();
    noteRowHead();
    const float centerY = (ImGui::GetItemRectMin().y + ImGui::GetItemRectMax().y) * 0.5f;
    if (titleFit != a.title && ImGui::IsItemHovered()) ImGui::SetTooltip("%s", a.title.c_str());
    if (meta[0]) {
        ImGui::SameLine(0, 0);
        ImGui::SetCursorPosX(ImMax(ImGui::GetCursorPosX() + gap,
                                   lanes.metaRight - ImGui::CalcTextSize(meta).x));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(meta);
        ImGui::PopStyleColor();
    }
    if (hasDrawer) {
        const HeaderHit hh = headerHit(topY, ImGui::GetItemRectMax().y + rowPadY());
        if (hh.clicked) { a.expanded = !open; a.userToggled = true; }
        headerChevron(lanes, centerY, openE, hh.hovered);
    }

    // ---- result / notes line (folded into the card) ----
    if (!a.reason.empty()) dimText(a.reason.c_str());

    // ---- expandable drawer ----
    if (openE > 0.0f && hasDrawer) {
        CollapseBody cb = beginCollapseBody(animKey, openE, "##actanim");
        if (!altLines.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::TextUnformatted("Alternatives");
            ImGui::PopStyleColor();
            for (const auto& al : altLines) {
                ImGui::Bullet();
                ImGui::SameLine(0, ui::metrics::U(1.0f));
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
                ImGui::TextUnformatted(al.c_str());
                ImGui::PopStyleColor();
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
        } else if (!a.detail.empty() && sentBody.empty()) {
            // Fallback: no SENT packing, show detail as before.
            drawProseBox("##actdetail", stripMdMarks(a.detail), ui::metrics::copilotProseMaxH());
        }
        endCollapseBody(cb);
    }

    ImGui::PopStyleColor();   // base ink
    ImRect cardRect = endRowShell(sh);
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
// Same shell (and so the same vertical pitch) as a tool row, without fill or hover: it is a note.
static void drawContextChip(const ActivityItem& a, const ui::Fonts& fonts) {
    const ui::AdaptiveSurface& surf = chatSurf();
    RowShell sh = beginRowShell("##ctxchip", ui::kit::Tone::Neutral, 0.0f, 0.0f);
    rowIcon("paperclip", surf.textTertiary);
    ImGui::SameLine(0, ui::metrics::U(1.5f));
    std::string line = "Context";
    if (!a.reason.empty()) line += " \xc2\xb7 " + a.reason;          // source (ledger/memory/...)
    line += ": " + a.title;                                          // human label
    if (a.ctxTokens > 0) line += "  (~" + std::to_string(a.ctxTokens) + " tok)";
    ImGui::PushFont(fonts.body);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    // ONE line, ellipsized against the shared right lane: these chips are inline transparency
    // notes, and a wrapped second line ran ragged past the icon gutter (and could spill past the
    // timeline's right edge). The full text stays on the hover tooltip.
    const std::string clipped =
        chatEllipsize(line, rowLanes(true).metaRight - ImGui::GetCursorPosX());
    ImGui::TextUnformatted(clipped.c_str());
    noteRowHead();
    if (clipped != line && ImGui::IsItemHovered()) ImGui::SetTooltip("%s", line.c_str());
    ImGui::PopStyleColor();
    ImGui::PopFont();
    const ImRect chipRect = endRowShell(sh);
    chatCopyContext(chipRect, "##ctx_copy", line);
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
    // An appended note ("\n\n(Stopped by you.)", a failure message) starts with its separator;
    // the tail renders as its own card, where those blank lines are just a gap at the top.
    const size_t start = m.text.find_first_not_of("\n\r ", shown.size());
    return start == std::string::npos ? std::string() : m.text.substr(start);
}

// Horizontal inset of the assistant's prose: the timeline rows' text column (the row pad), so an
// answer and the tool/thought rows above it share one left edge.
float chatProseInsetX() { return ui::kit::padX(); }

// The model's prose — the answer — as the calm centre of the column: no card, no rim, the text
// flowing full-width on the pane's glass like a reply in a modern chat (the timeline steps out of
// the spine gutter for it), while every stage/thought/tool row is a quiet log line. Selectable for
// click-drag + Ctrl+C; right-click copies the selection or the whole segment — the same affordances
// the single trailing reply card used to carry, which chat_log.cpp now only draws for a reply the
// timeline never saw.
static void drawProseBubble(ActivityItem& a, const ui::Fonts& fonts) {
    if (!ui::streamTextVisible(a.detail)) return;   // an empty segment is not a card
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    const std::string prose = stripMdMarks(a.detail);
    GlassBubble gb = beginOpenBubble(ImGui::GetContentRegionAvail().x);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(chatProseInsetX(), ui::metrics::cpChatCardPadY()));
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
    const ImRect proseRect = endOpenBubble(gb);
    const std::string sel = chatInputSelection(textId);
    chatCopyContext(proseRect, "##prose_copy", prose, prose, sel);
}

// M13 — the model's thoughts as ONE dim italic line with a disclosure chevron. While deltas stream
// (a.streaming) or the box is the live frontier of the turn it stays open so tokens appear live;
// once a later row lands or the turn ends it auto-collapses to the line (label + first-line
// preview) unless the user pinned it (userToggled always wins). `showThinking` off -> the label.
// The only stream:out box that reaches here is ops JSON ("Building ops…"): it always shows a
// one-line summary by default, with the raw JSON inside the drawer (never expanded-by-default
// mid-stream). Plain prose Output rows are skipped upstream by isProseOutputRow.
static void drawThoughtBox(ActivityItem& a, const ui::Fonts& fonts, bool showThinking,
                           bool liveFrontier) {
    const ui::AdaptiveSurface& surf = chatSurf();
    // The ops-JSON Output box never hides behind the "show thinking" switch.
    const bool isOpsJson = (a.stage == "stream:out" && a.opsCount > 0) ||
                           (a.title.find("Building ops") != std::string::npos);
    if (!showThinking && !isOpsJson) {
        ImGui::Indent(ui::kit::padX());   // the rows' text column: one left edge for the timeline
        ImGui::PushFont(fonts.body);
        // While the model is still thinking the label carries the same light sweep as the
        // turn's working line; a finished thought is a still caption.
        const ItalicSpan s = italicBegin();
        if (a.streaming || liveFrontier)
            chatShimmerText("Thinking\xe2\x80\xa6", surf.textTertiary, surf.textPrimary,
                            ui::anim::kBreathe);
        else
            ImGui::TextColored(surf.textSecondary, "Thinking");
        italicEnd(s);
        noteRowHead();
        ImGui::PopFont();
        ImGui::Unindent(ui::kit::padX());
        return;
    }
    // Ops JSON: only open when the user expanded it (never auto-opens, even mid-stream).
    // Thinking: open while streaming OR the live frontier OR user-expanded. A user's explicit
    // toggle always wins.
    const bool open = isOpsJson ? a.expanded
                                : (a.userToggled ? a.expanded
                                                 : (a.expanded || a.streaming || liveFrontier));
    const bool hasDetail = !a.detail.empty();
    // Collapse animation: the body eases shut over anim::kBase instead of vanishing.
    const ImGuiID animKey = ImGui::GetID("##thoughtbox_anim");
    const float   openE = ui::chatCollapseAnim() ? collapseEase(animKey, open && hasDetail)
                                                 : ((open && hasDetail) ? 1.0f : 0.0f);

    // ---- a raw wire payload is NOT a card. "Building ops… (raw) 1747 bytes" used to claim the
    // same silhouette, fill and border as a decision the agent made. It gets one dim MONO line
    // and the shared disclosure: mono says "this is a dump", the weight says "audit me if you like".
    if (isOpsJson) {
        std::string sum = a.reason;
        if (sum.empty()) {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "%zu bytes", a.detail.size());
            sum = buf;
        }
        const std::string line =
            (a.title.empty() ? std::string("Building ops") : a.title) + "  \xc2\xb7  " + sum;
        RowShell sh = beginRowShell("##opsline", ui::kit::Tone::Neutral, 0.0f, 0.0f, hasDetail,
                                    openE);
        const RowLanes lanes = rowLanes(true);
        const float    topY  = ImGui::GetCursorScreenPos().y;
        if (fonts.mono) ImGui::PushFont(fonts.mono);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        const std::string fit = chatEllipsize(
            line, ImMax(lanes.metaRight - ImGui::GetCursorPosX(), ui::metrics::U(6.0f)));
        ImGui::TextUnformatted(fit.c_str());
        ImGui::PopStyleColor();
        if (fonts.mono) ImGui::PopFont();
        noteRowHead();
        const float centerY = (ImGui::GetItemRectMin().y + ImGui::GetItemRectMax().y) * 0.5f;
        if (fit != line && ImGui::IsItemHovered()) ImGui::SetTooltip("%s", line.c_str());
        if (hasDetail) {
            const HeaderHit hh = headerHit(topY, ImGui::GetItemRectMax().y + rowPadY());
            if (hh.clicked) { a.expanded = !open; a.userToggled = true; }
            headerChevron(lanes, centerY, openE, hh.hovered);
        }
        if (openE > 0.0f && hasDetail) {
            CollapseBody cb = beginCollapseBody(animKey, openE, "##thoughtanim");
            drawProseBox("##opsraw", a.detail, ui::metrics::copilotProseMaxH());
            endCollapseBody(cb);
        }
        const ImRect opsRect = endRowShell(sh);
        chatCopyContext(opsRect, "##thought_copy", chatActivityCopyText(a));
        return;
    }

    // ---- the agent muttering: one dim italic line, no fill, no border.
    RowShell sh = beginRowShell("##thoughtbox", ui::kit::Tone::Neutral, 0.0f, 0.0f, hasDetail,
                                openE);
    const RowLanes lanes = rowLanes(true);
    const float    topY  = ImGui::GetCursorScreenPos().y;
    const std::string preview = (openE <= 0.0f && hasDetail)
        ? stripMdMarks(a.detail.substr(0, a.detail.find('\n')))
        : std::string();
    const float centerY = thoughtHeader(fonts, a.title.empty() ? "Thinking" : a.title.c_str(),
                                        a.streaming, preview, lanes);
    if (hasDetail) {
        const HeaderHit hh = headerHit(topY, ImGui::GetItemRectMax().y + rowPadY());
        if (hh.clicked) { a.expanded = !open; a.userToggled = true; }
        headerChevron(lanes, centerY, openE, hh.hovered);
    }
    if (openE > 0.0f && hasDetail) {
        CollapseBody cb = beginCollapseBody(animKey, openE, "##thoughtanim");
        // Grow to fit while the text is still arriving (or this box is the live frontier just
        // after streaming stopped) instead of scrolling inside a small fixed box — the box fills
        // out as the model reasons, only falling back to the scrolling presentation past the
        // generous cap. Finished/historical boxes keep the tight static scroll cap.
        const bool grow = a.streaming || liveFrontier;
        drawProseBox("##thoughttxt", stripMdMarks(a.detail),
                     grow ? ui::metrics::copilotProseStreamCapH()
                          : ui::metrics::copilotProseMaxH(), grow);
        endCollapseBody(cb);
    }
    ImRect thoughtRect = endRowShell(sh);
    const std::string thoughtCopy = chatActivityCopyText(a);
    chatCopyContext(thoughtRect, "##thought_copy", thoughtCopy);
}

// ---- artifact file actions ---------------------------------------------------------------
// Shared by the inline chat artifact rows (tool-call files/images) and the Session Artifacts
// panel: open with the OS default handler, or reveal in the OS file manager. Warn-toasts when
// the file is gone (moved/deleted after the run).

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
    if (!ui::openExternalFile(path))
        st.toast(ui::ToastLevel::Warn, "Could not open file: " + path);
}

void chatArtifactReveal(AppState& st, const std::string& path) {
    namespace fs = std::filesystem;
    if (!chatArtifactExists(path)) {
        st.toast(ui::ToastLevel::Warn, "File not found: " + fs::path(path).filename().string());
        return;
    }
    ui::revealInFileManager(path);
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

// Extensions the app itself opens (mirrors file_explorer.cpp isKnownExt: native
// .draw+/.step+ containers, CAD import formats incl. AutoCAD DXF/DWG and ACIS, ECAD/KiCad).
static bool chatAppOpenableExt(const std::string& e) {
    return e == ".draw+" || e == ".step+" || e == ".step" || e == ".stp" ||
           e == ".iges" || e == ".igs" || e == ".stl" || e == ".3mf" ||
           e == ".dxf" || e == ".dwg" || e == ".sat" || e == ".sab" ||
           e == ".ecad+" || e == ".ecad" || e == ".kicad_sch" || e == ".kicad_pcb";
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
// text after "\n\x1eERR\x1e\n", image captions after "\n\x1eCAP\x1e\n", artifact paths after
// "\n\x1eART\x1e\n" and the ts/cache-read META trailer last (ActivityItem gains no fields). Split
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
    // OUT is a private ActivityItem trailer, not user-visible detail. Strip it before ERR/ART
    // parsing so copy paths and drawers never expose marker bytes or taxonomy plumbing.
    ui::copilot::stripToolOutcomeTrailer(d.args);
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
    // CAP sits between ERR and ART (feed.cpp packs it just ahead of ART), so it is lifted off the
    // remaining tail before the ERR split.
    const char* kCapMark = "\n\x1e" "CAP\x1e\n";
    const char* kCapMarkBare = "\x1e" "CAP\x1e\n";
    mark = d.args.find(kCapMark);
    if (mark != std::string::npos) {
        d.cap = d.args.substr(mark + std::strlen(kCapMark));
        d.args = d.args.substr(0, mark);
    } else if (d.args.rfind(kCapMarkBare, 0) == 0) {
        d.cap = d.args.substr(std::strlen(kCapMarkBare));
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

// ONE parse of a line inside the packed "\x1eART\x1e" blob. Both producers — feed.cpp's tool-call
// packer and conversations.cpp's session-restore synthesizer — write "<kind>\t<path>", one artifact
// per line, so a line that does not have that shape is not an artifact.
//
// The path sanity check is not pedantry. Real persisted transcripts on this machine carry ART blobs
// whose "path" was a multi-LINE tool payload (a cad_guide dump): the embedded newlines shatter the
// one-artifact-per-line contract, and 450 of the 634 artifact lines in the local store are guide
// prose wearing a "file\t" prefix. Unfiltered, that renders 32 fake "file: WORKFLOW" rows — each
// with a live (open)/(folder) button — on a single tool row, and would have the run receipt claim
// the turn wrote 93 files.
//
// A real artifact path therefore has to look like one: no leading/trailing space, a directory
// separator, and a basename ending in a 1-8 character extension of alphanumerics plus '+' (the
// native ".step+"/".ecad+" containers). Deliberately NOT "no spaces in the basename" — a part named
// "rev 2 plate" exports to "rev 2 plate.step" and that file is real. Measured against all 634
// artifact lines in the local session stores: 0 prose lines survive, every well-formed path does.
static bool artifactLine(const std::string& line, std::string* kindOut, std::string* pathOut) {
    const size_t tab = line.find('\t');
    if (tab == std::string::npos) return false;
    const std::string kind = line.substr(0, tab);
    if (kind != "image" && kind != "mesh" && kind != "file") return false;
    const std::string path = line.substr(tab + 1);
    if (path.empty() || std::isspace((unsigned char)path.front()) ||
        std::isspace((unsigned char)path.back()))
        return false;
    const size_t slash = path.find_last_of("/\\");
    if (slash == std::string::npos) return false;
    const size_t dot = path.find_last_of('.');
    if (dot == std::string::npos || dot <= slash + 1) return false;   // no basename, or dotfile
    const std::string ext = path.substr(dot + 1);
    if (ext.empty() || ext.size() > 8) return false;
    for (unsigned char c : ext)
        if (!std::isalnum(c) && c != '+') return false;
    if (kindOut) *kindOut = kind;
    if (pathOut) *pathOut = path;
    return true;
}
// Images are transcript evidence, not drawer content. Validate with the same parser the painter
// and receipt use so a corrupt legacy path cannot advertise an empty disclosure.
static bool drawerHasArtifacts(const std::string& blob, bool imagesInline) {
    std::istringstream iss(blob);
    std::string line;
    while (std::getline(iss, line)) {
        std::string kind;
        if (artifactLine(line, &kind, nullptr) && (kind != "image" || !imagesInline)) return true;
    }
    return false;
}

static bool hasImageArtifacts(const std::string& blob) {
    std::istringstream iss(blob);
    std::string line;
    while (std::getline(iss, line)) {
        std::string kind;
        if (artifactLine(line, &kind, nullptr) && kind == "image") return true;
    }
    return false;
}

// A restored Session files row is an aggregate index, not another return from the agent. Suppress
// only images retained at their original tool rows; old transcripts with index-only artifacts
// still show their sole evidence. Match the existing ART wire format without reparsing every row.
static bool timelineHasEvidencePath(const std::vector<ActivityItem>& rows, const std::string& path) {
    for (const ActivityItem& row : rows) {
        if (row.rowKind != 3 || row.stage == "artifacts") continue;
        const size_t art = row.detail.find("\x1e" "ART\x1e\n");
        if (art == std::string::npos) continue;
        size_t pos = row.detail.find(path, art + 5);
        while (pos != std::string::npos) {
            const size_t end = pos + path.size();
            if (pos >= 6 && row.detail.compare(pos - 6, 6, "image\t") == 0 &&
                (end == row.detail.size() || row.detail[end] == '\n')) return true;
            pos = row.detail.find(path, pos + 1);
        }
    }
    return false;
}

static std::string evidenceCaptionLead(const std::string& caption) {
    size_t end = std::min(caption.find('\n'), size_t(360));
    end = std::min(end, caption.size());
    if (end == caption.size()) return caption;
    // Do not split a UTF-8 codepoint when a long first paragraph is shortened.
    while (end > 0 && (static_cast<unsigned char>(caption[end]) & 0xc0) == 0x80) --end;
    return caption.substr(0, end) + "\xe2\x80\xa6";
}


// Rendering policy for the structured tool outcome. Legacy means the OUT trailer is absent,
// malformed, or from a future daemon; every caller must then preserve the old status-based path.
// `executed` is part of the wording, not decorative metadata: it prevents a timeout from claiming
// nothing ran and keeps pre-execution refusals honest.
struct ToolOutcomePresentation {
    ui::copilot::ToolOutcomeInfo info;
    bool structured = false;
    bool succeeded  = false;
    bool declined   = false;
    bool timeout    = false;
    bool failure    = false;
    ui::kit::Tone tone = ui::kit::Tone::Neutral;
    const char* chip = "";
    std::string message;
};

static ToolOutcomePresentation toolOutcomePresentation(const ActivityItem& a) {
    ToolOutcomePresentation p;
    p.info = ui::copilot::toolOutcomeFromDetail(a.detail);
    p.structured = p.info.kind != ui::copilot::ToolOutcomeKind::Legacy;
    switch (p.info.kind) {
        case ui::copilot::ToolOutcomeKind::Ok:
            p.succeeded = true;
            break;
        case ui::copilot::ToolOutcomeKind::PolicyRefusal:
            p.declined = true;
            p.tone = ui::kit::Tone::Info;
            p.chip = "declined";
            p.message = p.info.executed
                ? "Harness policy declined this call, although the event reports that execution "
                  "started."
                : "Harness policy declined this call. The tool did not run.";
            break;
        case ui::copilot::ToolOutcomeKind::GuardBlock:
            p.declined = true;
            p.tone = ui::kit::Tone::Info;
            p.chip = "blocked";
            p.message = p.info.executed
                ? "The harness declined this call because a guard blocked it, although the event "
                  "reports that execution started."
                : "The harness declined this call because a guard blocked it before execution. "
                  "The tool did not run.";
            break;
        case ui::copilot::ToolOutcomeKind::ValidationError:
            p.failure = true;
            p.tone = ui::kit::Tone::Err;
            p.chip = "invalid call";
            p.message = p.info.executed
                ? "Call validation failed after execution was reported."
                : "Call validation failed \xe2\x80\x94 the model's arguments did not bind. The tool "
                  "did not run.";
            break;
        case ui::copilot::ToolOutcomeKind::TimeoutUnknown:
            p.timeout = true;
            p.tone = ui::kit::Tone::Warn;
            p.chip = "UNKNOWN";
            p.message = p.info.executed
                ? "Outcome UNKNOWN \xe2\x80\x94 the tool started, timed out, and may still be running."
                : "Outcome UNKNOWN \xe2\x80\x94 execution could not be confirmed and the call may "
                  "still be running.";
            break;
        case ui::copilot::ToolOutcomeKind::InternalError:
            p.failure = true;
            p.tone = ui::kit::Tone::Err;
            p.chip = "harness error";
            p.message = p.info.executed
                ? "Harness internal error after the tool started."
                : "Harness internal error before tool execution could be confirmed.";
            break;
        case ui::copilot::ToolOutcomeKind::ToolError:
            p.failure = true;
            p.tone = ui::kit::Tone::Err;
            p.chip = "tool failed";
            p.message = p.info.executed
                ? "The tool ran and failed."
                : "The tool reported an error before its body ran.";
            break;
        case ui::copilot::ToolOutcomeKind::Legacy:
        default:
            p.failure = a.status == "fail" || a.status == "error";
            p.succeeded = !p.failure && a.status != "running";
            p.tone = rowToneOf(a.status, /*guardRefusal=*/false);
            p.chip = p.failure ? "failed" : "";
            break;
    }
    return p;
}

// A live turn is deliberately not windowed by chat_log.cpp. Cache its tool rows here instead:
// the old loop reparsed every packed detail (including folded members) and submitted every
// auto-height child even when thousands of pixels outside the log's clip rectangle.
// Snapshots are byte-exact, not pointer/size/hash keys: completion and steering may replace a
// string in place without changing its length. Only changed tools pay for parsing/sanitizing.
struct ToolTimelineRow {
    ActivityItem source;
    ToolRowDetail parts;
    ToolOutcomePresentation outcome;
    std::string groupKey;
    bool valid = false, changed = true, images = false, files = false, settled = false;
    float height = -1.0f, headOffset = -1.0f;
    float drawerHeight = -1.0f, evidenceHeight = -1.0f, artifactHeight = -1.0f;
    size_t end = 0;
    int rewind = -1;
    bool frontier = false;
};

static bool sameToolRow(const ActivityItem& a, const ActivityItem& b) {
    return a.rowKind == b.rowKind && a.stage == b.stage && a.title == b.title &&
           a.reason == b.reason && a.status == b.status && a.detail == b.detail &&
           a.durationMs == b.durationMs && a.expanded == b.expanded &&
           a.userToggled == b.userToggled && a.streaming == b.streaming;
}

struct ToolTimelineCache {
    std::vector<ToolTimelineRow> rows;
    std::vector<std::pair<std::string, std::string>> docs;
    std::string conversation;
    ImGuiStyle style{};
    ImFont* font = nullptr;
    ImFont* mono = nullptr;
    ImFont* medium = nullptr;
    float width = -1.0f, fontSize = -1.0f, monoSize = -1.0f, mediumSize = -1.0f;
    int detail = -1, frame = -1;
    double stamp = 0.0;
    bool renders = false, grouping = false, humanize = false, collapse = false;
    bool rewindAvailable = false, rewindBusy = false;
    double checkpointFetch = -1.0;
    size_t checkpoints = 0;
};

static std::unordered_map<ImGuiID, ToolTimelineCache> s_toolTimelines;

static ToolTimelineCache& toolTimelineCache(AppState& st, ChatMsg& m, const ui::Fonts& fonts) {
    // Bound both lifetime and retained transcripts. No pointers into chat/activity survive a
    // frame (switching conversations and growing the vectors can move all of their storage).
    const ImGuiID id = ImGui::GetID("##tool_timeline_cache");
    const int frame = ImGui::GetFrameCount();
    for (auto it = s_toolTimelines.begin(); it != s_toolTimelines.end();) {
        if (it->first != id && frame - it->second.frame > 120) it = s_toolTimelines.erase(it);
        else ++it;
    }
    if (s_toolTimelines.find(id) == s_toolTimelines.end() && s_toolTimelines.size() >= 8) {
        auto oldest = s_toolTimelines.begin();
        for (auto it = s_toolTimelines.begin(); it != s_toolTimelines.end(); ++it)
            if (it->second.frame < oldest->second.frame) oldest = it;
        s_toolTimelines.erase(oldest);
    }
    ToolTimelineCache& cache = s_toolTimelines[id];
    const std::string conversation = st.activeConvId();
    if (cache.conversation != conversation || cache.stamp != m.ts) {
        cache = ToolTimelineCache{};
        cache.conversation = conversation;
        cache.stamp = m.ts;
    }
    cache.frame = frame;
    bool docsChanged = cache.docs.size() != st.docTabs.size();
    if (!docsChanged)
        for (size_t i = 0; i < st.docTabs.size(); ++i)
            if (cache.docs[i].first != st.docTabs[i].docId ||
                cache.docs[i].second != st.docTabs[i].displayTitle()) { docsChanged = true; break; }
    if (docsChanged) {
        cache.docs.clear();
        for (const auto& tab : st.docTabs) cache.docs.emplace_back(tab.docId, tab.displayTitle());
    }
    const float width = ImGui::GetContentRegionAvail().x;
    const bool humanize = ui::chatHumanizeDocIds();
    const bool layoutChanged = cache.width != width || cache.font != ImGui::GetFont() ||
        cache.mono != fonts.mono || cache.medium != fonts.medium ||
        cache.fontSize != ImGui::GetFontSize() ||
        cache.monoSize != (fonts.mono ? fonts.mono->FontSize * fonts.mono->Scale : 0.0f) ||
        cache.mediumSize != (fonts.medium ? fonts.medium->FontSize * fonts.medium->Scale : 0.0f) ||
        std::memcmp(&cache.style, &ImGui::GetStyle(), sizeof(ImGuiStyle)) != 0 ||
        cache.detail != ui::chatToolDetail() || cache.renders != ui::chatShowRenders() ||
        cache.grouping != ui::chatGroupToolCalls() || cache.collapse != ui::chatCollapseAnim() ||
        cache.rewindAvailable != s_aiVc.available || cache.rewindBusy != s_aiVc.busy ||
        cache.checkpointFetch != s_aiVc.lastFetch || cache.checkpoints != s_aiVc.checkpoints.size() ||
        cache.humanize != humanize;
    cache.width = width;
    cache.font = ImGui::GetFont();
    cache.mono = fonts.mono;
    cache.medium = fonts.medium;
    cache.fontSize = ImGui::GetFontSize();
    cache.monoSize = fonts.mono ? fonts.mono->FontSize * fonts.mono->Scale : 0.0f;
    cache.mediumSize = fonts.medium ? fonts.medium->FontSize * fonts.medium->Scale : 0.0f;
    cache.style = ImGui::GetStyle();
    cache.detail = ui::chatToolDetail();
    cache.renders = ui::chatShowRenders();
    cache.grouping = ui::chatGroupToolCalls();
    cache.collapse = ui::chatCollapseAnim();
    const bool sanitizeChanged = docsChanged || cache.humanize != humanize;
    cache.rewindAvailable = s_aiVc.available;
    cache.rewindBusy = s_aiVc.busy;
    cache.checkpointFetch = s_aiVc.lastFetch;
    cache.checkpoints = s_aiVc.checkpoints.size();
    cache.humanize = humanize;
    // At most 8192 rows / 4 MiB of source text per timeline (parsed trailers retain at most
    // another copy). Larger transcripts still render correctly; excess rows use the old path.
    cache.rows.resize(std::min(m.activity.size(), size_t(8192)));
    size_t bytes = 0;
    for (size_t i = 0; i < m.activity.size(); ++i) {
        ActivityItem& a = m.activity[i];
        const size_t rowBytes = a.detail.size() + a.title.size() + a.reason.size() + a.stage.size();
        if (a.rowKind != 3 || i >= cache.rows.size() || rowBytes > 4 * 1024 * 1024 - bytes) {
            if (i < cache.rows.size()) cache.rows[i] = ToolTimelineRow{};
            sanitizeActivityItem(st, a);
            continue;
        }
        bytes += rowBytes;
        ToolTimelineRow& row = cache.rows[i];
        row.changed = !row.valid || !sameToolRow(row.source, a) || sanitizeChanged;
        if (row.changed) {
            sanitizeActivityItem(st, a);
            row.source = ActivityItem(a);  // replace old capacities as well as old content
            row.parts = splitToolRowDetail(a.detail);
            row.outcome = toolOutcomePresentation(a);
            row.images = hasImageArtifacts(row.parts.arts);
            row.files = drawerHasArtifacts(row.parts.arts, true);
            row.groupKey = row.outcome.succeeded
                ? ui::toolPhrase(a.title, a.reason, true).groupKey : std::string();
            row.valid = true;
        }
        if (row.changed || layoutChanged) {
            row.height = row.drawerHeight = row.evidenceHeight = row.artifactHeight = -1.0f;
        }
    }
    return cache;
}

static bool toolRowIsDeclined(const ActivityItem& a) {
    return toolOutcomePresentation(a).declined;
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
    // A failure payload is edged in a soft error hairline, not a loud amber border: the row above
    // already says it failed.
    const ImVec4 border = failure
        ? ui::kit::toneVec(ui::kit::Tone::Err, 0.35f)
        : ImVec4(pal.glassBorder[0], pal.glassBorder[1],
                 pal.glassBorder[2], pal.glassBorder[3]);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(padX, padY));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, ui::kit::rowRound());
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

// Tool arguments as the user reads them. NEVER through stripMdMarks: `**kwargs`, `x**2` and
// backticks are code, not markdown. Producers ship args_preview as JSON capped at ~2 KB with a
// trailing "…", so a long eval script is usually NOT parseable JSON — its code string is then
// salvaged from the prefix. The right-click Copy keeps the raw preview.
struct ToolArgsView {
    std::string code;     // code/script/source/command string value, unescaped (verbatim)
    std::string fields;   // every other key as "key: value" lines
    std::string raw;      // fallback only: pretty JSON or the preview text, verbatim
};

static constexpr const char* kToolCodeKeys[] = {"code", "script", "source", "command", "cmd"};

static void appendUtf8(std::string& out, uint32_t cp) {
    if (cp < 0x80) {
        out.push_back((char)cp);
    } else if (cp < 0x800) {
        out.push_back((char)(0xC0 | (cp >> 6)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    } else if (cp < 0x10000) {
        out.push_back((char)(0xE0 | (cp >> 12)));
        out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    } else {
        out.push_back((char)(0xF0 | (cp >> 18)));
        out.push_back((char)(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    }
}

// Decode a JSON string literal whose body starts at s[i] (just past the opening quote), up to
// its closing quote or the end of a capped preview — whatever the cap left is still real code.
static std::string decodeJsonStringPrefix(const std::string& s, size_t i) {
    auto hex4 = [&s](size_t at, uint32_t& v) {
        if (at + 4 > s.size()) return false;
        v = 0;
        for (size_t k = at; k < at + 4; ++k) {
            const char c = s[k];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= (uint32_t)(c - '0');
            else if (c >= 'a' && c <= 'f') v |= (uint32_t)(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= (uint32_t)(c - 'A' + 10);
            else return false;
        }
        return true;
    };
    std::string out;
    while (i < s.size()) {
        const char c = s[i];
        if (c == '"') break;
        if (c != '\\' || i + 1 >= s.size()) { out.push_back(c); ++i; continue; }
        const char e = s[i + 1];
        i += 2;
        switch (e) {
            case 'n': out.push_back('\n'); break;
            case 't': out.push_back('\t'); break;
            case 'r': out.push_back('\r'); break;
            case 'b': out.push_back('\b'); break;
            case 'f': out.push_back('\f'); break;
            case 'u': {
                uint32_t cp = 0;
                if (!hex4(i, cp)) { out += "\\u"; break; }
                i += 4;
                uint32_t lo = 0;
                if (cp >= 0xD800 && cp <= 0xDBFF && i + 6 <= s.size() && s[i] == '\\' &&
                    s[i + 1] == 'u' && hex4(i + 2, lo) && lo >= 0xDC00 && lo <= 0xDFFF) {
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                    i += 6;
                }
                appendUtf8(out, cp);
                break;
            }
            default: out.push_back(e); break;   // \" \\ \/ and anything unexpected, literally
        }
    }
    return out;
}

static ToolArgsView toolArgsView(const std::string& args) {
    ToolArgsView v;
    const nlohmann::json j = nlohmann::json::parse(args, nullptr, /*allow_exceptions=*/false);
    if (j.is_object()) {
        const char* codeKey = nullptr;
        for (const char* key : kToolCodeKeys) {
            const auto it = j.find(key);
            if (it != j.end() && it->is_string() && !it->get_ref<const std::string&>().empty()) {
                v.code = it->get<std::string>();
                codeKey = key;
                break;
            }
        }
        for (const auto& el : j.items()) {
            if (el.key().rfind("_truncated", 0) == 0) continue;   // producer bookkeeping
            if (codeKey && el.key() == codeKey) continue;
            const nlohmann::json& val = el.value();
            if (!v.fields.empty()) v.fields += "\n";
            v.fields += el.key();
            v.fields += ": ";
            v.fields += val.is_string() ? val.get<std::string>() : val.dump();
        }
        if (v.code.empty() && v.fields.empty()) v.raw = args;
        return v;
    }
    if (!j.is_discarded()) {
        // Arrays / scalars: nothing better than readable JSON.
        v.raw = j.is_string() ? j.get<std::string>() : j.dump(2);
        return v;
    }
    // Not JSON — usually a capped object preview. Salvage the code string when one leads it.
    if (!args.empty() && args[0] == '{') {
        for (const char* key : kToolCodeKeys) {
            const std::string quoted = std::string("\"") + key + "\"";
            size_t at = args.find(quoted);
            if (at == std::string::npos) continue;
            at += quoted.size();
            while (at < args.size() && std::isspace((unsigned char)args[at])) ++at;
            if (at >= args.size() || args[at] != ':') continue;
            ++at;
            while (at < args.size() && std::isspace((unsigned char)args[at])) ++at;
            if (at >= args.size() || args[at] != '"') continue;
            v.code = decodeJsonStringPrefix(args, at + 1);
            if (!v.code.empty()) return v;
        }
    }
    v.raw = args;
    return v;
}

// The Doing drawer's argument section: the code in the mono code box, the remaining arguments as
// a key: value list, and raw text (verbatim, mono) only when neither exists.
static void drawToolArgs(const std::string& args, const ui::Fonts& fonts) {
    if (args.empty()) return;
    const ToolArgsView v = toolArgsView(args);
    if (!v.code.empty())
        drawCodeBox("##toolcode", v.code, fonts.mono, ui::metrics::px(260.0f));
    if (!v.fields.empty())
        drawToolDetailBox("##toolfields", v.fields, nullptr, ui::metrics::copilotProseMaxH(),
                          /*failure=*/false);
    if (!v.raw.empty())
        drawToolDetailBox("##tooldetail", v.raw, fonts.mono, ui::metrics::copilotProseMaxH(),
                          /*failure=*/false);
}

// M14 — one tool call as ONE dense rounded LOG LINE, not a card: [status mark in the gutter] tool
// icon, the HUMAN title (tool_phrase.h turns `add_features` into "Building geometry"), the target it
// acts on, a right-aligned latency/cache figure and the disclosure chevron. Nothing was dropped to
// get there: the raw MCP symbol, the wall clock and the full target live on the title's tooltip,
// and args/failure/artifacts expand from the whole header line (chatCollapseAnim eases the drawer
// shut instead of snapping when the next call starts).
//
// A call that FAILED is a soft rounded Tone::Err tint with the FIRST line of its error inline under
// the title (full text on hover) and the rest in the drawer. Harness refusals are informational
// Tone::Info rows, and a timeout is amber/UNKNOWN because execution may still be live. `failCount`
// > 1 collapses consecutive identical tool+target failures of the SAME taxonomy class into one row;
// a non-null `group` of >1 is a fold of consecutive SIMILAR successful calls (drawAgentTimeline
// computes both runs), shown as ONE line with a count badge and its members' targets joined.
static void drawToolCallRowImpl(AppState& st, ActivityItem& a, const ui::Fonts& fonts,
                                int failCount, bool liveFrontier, int callI = -1,
                                const std::vector<ActivityItem*>* group = nullptr,
                                bool onSpine = false,
                                const std::vector<ActivityItem>* timeline = nullptr,
                                std::vector<ToolTimelineRow>* cachedRows = nullptr) {
    const float gap = ui::metrics::U(1.5f);
    // Chat-display prefs (cached in ai_prefs — safe to read per row per frame).
    const int  detailLvl = ui::chatToolDetail();
    const bool simple    = detailLvl <= 0;   // human sentence only: no drawer, no meta, no ids
    const bool diag      = detailLvl >= 2;   // + the raw MCP symbol dim after the human title
    const bool showRenders = ui::chatShowRenders();
    const int  groupN    = (group && group->size() > 1) ? (int)group->size() : 1;
    // Split the packed detail once and resolve the structured presentation once. Unknown/missing
    // taxonomy stays on the legacy status path; no historical row receives an invented state.
    auto cached = [&](const ActivityItem& row) -> ToolTimelineRow* {
        if (!cachedRows || !timeline) return nullptr;
        const size_t idx = size_t(&row - timeline->data());
        return idx < cachedRows->size() && (*cachedRows)[idx].valid ? &(*cachedRows)[idx] : nullptr;
    };
    const ToolTimelineRow* rowCache = cached(a);
    const ToolRowDetail uncachedParts = rowCache ? ToolRowDetail{} : splitToolRowDetail(a.detail);
    const ToolRowDetail& parts = rowCache ? rowCache->parts : uncachedParts;
    ToolRowDetail memberParts;
    auto detailOf = [&](const ActivityItem& row) -> const ToolRowDetail& {
        if (const ToolTimelineRow* c = cached(row)) return c->parts;
        memberParts = splitToolRowDetail(row.detail);
        return memberParts;
    };
    // A folded row can itself span many viewports. Window its individual drawer/evidence
    // members too, retaining exact measured heights in the same content/layout cache.
    auto windowMember = [&](const ActivityItem& member, float ToolTimelineRow::*height,
                            bool allowSkip, auto&& draw) {
        ToolTimelineRow* c = cached(member);
        const float y = ImGui::GetCursorPosY();
        const float sy = ImGui::GetCursorScreenPos().y;
        const ImRect clip = ImGui::GetCurrentWindow()->ClipRect;
        const float slack = ImGui::GetTextLineHeightWithSpacing() * 2.0f;
        if (c && allowSkip && (c->*height) >= 0.0f &&
            (sy + (c->*height) < clip.Min.y - slack || sy > clip.Max.y + slack)) {
            if ((c->*height) > 0.0f) {
                ImGui::Dummy(ImVec2(0.0f, ImMax(0.0f, (c->*height) - ImGui::GetStyle().ItemSpacing.y)));
                ImGui::SetCursorPosY(y + (c->*height));
            }
            return;
        }
        draw();
        if (c) (c->*height) = allowSkip ? ImGui::GetCursorPosY() - y : -1.0f;
    };
    const std::string& argsBody = parts.args;
    const std::string& errBody  = parts.err;
    const std::string& artsBlob = parts.arts;
    const ToolOutcomePresentation uncachedOutcome = rowCache ? ToolOutcomePresentation{}
                                                             : toolOutcomePresentation(a);
    const ToolOutcomePresentation& outcome = rowCache ? rowCache->outcome : uncachedOutcome;
    const bool hardFail = outcome.failure;
    const bool declined = outcome.declined;
    const bool timeout  = outcome.timeout;
    const bool exceptional = hardFail || declined || timeout;
    const ImVec4 outcomeTone = ui::kit::toneOn(outcome.tone, chatSurf());
    // The raw cause, summarized. Its FIRST line rides inline under the title; whatever the inline
    // line cannot carry (the structured explanation, further lines, a traceback) is the drawer's.
    const std::string errShort =
        ((hardFail || timeout) && !errBody.empty()) ? errSummary(errBody) : std::string();
    const std::string errLead = !errShort.empty() ? errShort : outcome.message;
    const std::string errLine = errLead.substr(0, errLead.find('\n'));
    // Everything the inline error line stands for, for its hover tooltip.
    std::string errTip = outcome.message;
    if (!errShort.empty() && errShort != outcome.message)
        errTip += (errTip.empty() ? "" : "\n") + errShort;

    // Drawer state is settled BEFORE anything is drawn: the header's click target and the
    // animated body both need it, and a grouped card's drawer is its members' drawers stacked.
    const bool open = a.userToggled ? a.expanded : (a.expanded || liveFrontier);
    // A summarized exception whose raw text IS its inline line has nothing extra to reveal, so it
    // must not advertise a chevron onto an empty drawer.
    // Visual evidence — the images a call returned to the agent (a render, a section, a clash
    // highlight) — is drawn UNDER the row whether or not the drawer is open, grouped or not, at
    // every detail level: it is what the agent looked at, so it belongs in the transcript at the
    // point it arrived. Only the display preference turns that off; then images fall back to dim
    // path lines in the drawer. A restored "Session files" index shows only evidence whose
    // originating tool row is missing (legacy history); retained tool images never repeat.
    const bool inlineEvidence = showRenders;
    bool hasDrawer = !argsBody.empty() || drawerHasArtifacts(artsBlob, inlineEvidence) ||
                     (declined && !errBody.empty()) ||
                     (exceptional && !outcome.message.empty() && outcome.message != errLine) ||
                     ((hardFail || timeout) && !errBody.empty() && errBody != errLine);
    if (groupN > 1) {
        hasDrawer = false;
        for (const ActivityItem* mi : *group) {
            const ToolRowDetail& mp = detailOf(*mi);
            const ToolTimelineRow* member = cached(*mi);
            const bool artifacts = member ? member->files || (!inlineEvidence && member->images)
                                          : drawerHasArtifacts(mp.arts, inlineEvidence);
            if (!mp.args.empty() || !mp.err.empty() || artifacts) { hasDrawer = true; break; }
        }
    }
    // Simplified mode has no disclosure, no args drawer and no engine ids at all — artifacts still
    // reach the user, drawn inline below instead of behind an expander.
    if (simple) hasDrawer = false;
    const ImGuiID animKey = ImGui::GetID("##toolrow_anim");
    const bool    wantOpen = open && hasDrawer;
    const float   openE = ui::chatCollapseAnim() ? collapseEase(animKey, wantOpen)
                                                 : (wantOpen ? 1.0f : 0.0f);

    // No leading spacer: consecutive calls are one log, and the timeline's ItemSpacing is the gap.
    // A completed call has no resting fill — it is a log line that lights up under the pointer;
    // an exceptional one is a soft rounded tint, never a bar.
    RowShell sh = beginRowShell("##toolrow", outcome.tone, exceptional ? kFailWash : 0.0f,
                                exceptional ? kFailEdge : 0.0f, hasDrawer, openE, true);
    const ui::AdaptiveSurface& surf = chatSurf();

    // Drawer / simplified-mode artifact list: dim "kind: path" lines + file actions for meshes and
    // files — and, with renders explicitly disabled, images with the caption the tool returned.
    auto drawArtifacts = [&](const ToolRowDetail& d) {
        if (d.arts.empty()) return;
        std::istringstream iss(d.arts);
        std::string line;
        int artI = 0;
        bool captioned = inlineEvidence || d.cap.empty();
        while (std::getline(iss, line)) {
            std::string kindA, path;
            // artifactLine drops continuation lines from a corrupted multi-line "path": offering an
            // (open) button on "WORKFLOW" is worse than showing nothing.
            if (!artifactLine(line, &kindA, &path)) continue;
            if (kindA == "image" && inlineEvidence) continue;
            ++artI;
            if (kindA == "image" && !captioned) {
                captioned = true;
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                ImGui::TextWrapped("%s", d.cap.c_str());
                ImGui::PopStyleColor();
            }
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextWrapped("%s: %s", kindA.c_str(), path.c_str());
            ImGui::PopStyleColor();
            ImGui::PushID(artI + 1000);
            chatArtifactActions(st, path);
            ImGui::PopID();
        }
    };
    // Inline evidence for ONE call: each returned image (bounded thumbnail, larger on hover; a
    // missing file keeps the dim "image: path" line) with its file actions, then the caption the
    // tool returned with them — its first paragraph inline, the whole text on hover.
    auto drawEvidence = [&](const ToolRowDetail& d) {
        if (!inlineEvidence || d.arts.empty()) return;
        const float logW = ImGui::GetContentRegionAvail().x;
        std::istringstream iss(d.arts);
        std::string line;
        int imgI = 0;
        while (std::getline(iss, line)) {
            std::string kindA, path;
            if (!artifactLine(line, &kindA, &path) || kindA != "image") continue;
            if (a.stage == "artifacts" && timeline && timelineHasEvidencePath(*timeline, path))
                continue;
            ++imgI;
            ImGui::PushID(imgI);
            const Tex& tx = artTexFor(path);
            if (tx.id) {
                const float maxTw = ui::metrics::copilotThumbW();
                const float tw = logW < maxTw ? logW : maxTw;
                const float th = tw * (float)tx.h / (float)std::max(1, tx.w);
                ImGui::Image((ImTextureID)(intptr_t)tx.id, ImVec2(tw, th));
                if (ImGui::IsItemHovered()) {
                    const float bigW = ImMin(ui::metrics::px(560.0f), (float)tx.w);
                    ImGui::BeginTooltip();
                    ImGui::Image((ImTextureID)(intptr_t)tx.id,
                                 ImVec2(bigW, bigW * (float)tx.h / (float)std::max(1, tx.w)));
                    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
                    ImGui::TextUnformatted(path.c_str());
                    ImGui::PopStyleColor();
                    ImGui::EndTooltip();
                }
            } else {
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
                ImGui::TextWrapped("image: %s", path.c_str());
                ImGui::PopStyleColor();
            }
            ImGui::PushID(1000);
            chatArtifactActions(st, path);
            ImGui::PopID();
            ImGui::PopID();
        }
        if (imgI == 0 || d.cap.empty()) return;
        const std::string lead = evidenceCaptionLead(d.cap);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s", lead.c_str());
        ImGui::PopStyleColor();
        if (lead != d.cap && ImGui::IsItemHovered()) {
            ImGui::BeginTooltip();
            ImGui::PushTextWrapPos(ImGui::GetFontSize() * 36.0f);
            ImGui::TextUnformatted(d.cap.c_str());
            ImGui::PopTextWrapPos();
            ImGui::EndTooltip();
        }
    };

    // ---- the right-hand chip is composed FIRST, because the title/target elision budget has to
    // reserve it. A second meta LINE was the old way around that, at the cost of a whole extra
    // line on every call in the transcript.
    double groupMs = a.durationMs;
    if (groupN > 1) {
        groupMs = 0;
        for (const ActivityItem* mi : *group) groupMs += mi->durationMs;
    }
    std::string metaS;
    if (!simple) {
        char meta[128];
        if (groupN > 1) {
            if (groupMs > 0) {
                std::snprintf(meta, sizeof(meta), "%.1fs", groupMs / 1000.0);
                metaS = meta;
            }
        } else if (exceptional) {
            metaS = outcome.chip;
            if (failCount > 1) {
                std::snprintf(meta, sizeof(meta), " \xc3\x97%d", failCount);
                metaS += meta;
            }
            // A pre-execution decline has no meaningful latency. Failures and UNKNOWN timeouts do:
            // elapsed time helps distinguish an immediate bind error from work that ran.
            if (!declined && a.durationMs > 0) {
                std::snprintf(meta, sizeof(meta), " \xc2\xb7 %.1fs", a.durationMs / 1000.0);
                metaS += meta;
            }
        } else if (a.durationMs > 0) {
            std::snprintf(meta, sizeof(meta), "%.1fs", a.durationMs / 1000.0);
            metaS = meta;
        }
    }
    // META trailer: when the call ran + what its completion read from the prompt cache. A grouped
    // row shows its LAST member's stamp (the most recent call in the fold).
    double metaTs = parts.ts;
    int    metaCache = parts.cacheRead;
    if (groupN > 1) {
        const ToolRowDetail& tailParts = detailOf(*group->back());
        metaTs = tailParts.ts;
        metaCache = tailParts.cacheRead;
    }
    if (!simple && metaCache > 0) {
        if (!metaS.empty()) metaS += " \xc2\xb7 ";
        metaS += "cache " + chatTokCount(metaCache);
    }
    const std::string clockS = (!simple && metaTs > 0.0) ? chatRowClock(metaTs) : std::string();
    // Right-hand lanes: the chevron cell (reserved whether or not this row has a drawer, so the
    // figures form one column down the turn) and the meta figure right-aligned against it.
    const RowLanes lanes = rowLanes(!simple);
    const float metaW = metaS.empty() ? 0.0f : ImGui::CalcTextSize(metaS.c_str()).x + gap;
    // The per-call rewind glyph SameLines itself in and is self-gating, so its slot has to be
    // reserved up front or a long title pushes it under the meta figure. Resolved ONCE here and
    // used to skip the painter entirely when there is no checkpoint, so the row does not pay for
    // the same checkpoint scan twice.
    const bool hasRewind = groupN == 1 && ui::s_aiVc.available && !ui::s_aiVc.busy &&
                           !ui::aiVcCallCheckpointId(callI, a.title).empty();
    const float rewindW = hasRewind ? ImGui::GetTextLineHeight() * 1.15f + ui::metrics::U(2.0f)
                                    : 0.0f;

    // ---- header: [status glyph] icon  Title [n]  dim target ........ meta  [chevron]
    // Same rule as drawActionCard: when the run spine owns this row's mark out in the gutter, the
    // row must not paint a second one (nor reserve its empty cell). UNKNOWN uses the Retry ring, a
    // declined call the Skipped dash, and every true failure the Failed cross.
    const float topY = ImGui::GetCursorScreenPos().y;
    if (!onSpine) {
        drawStatusGlyph(hardFail ? "fail" : declined ? "skipped" : timeout ? "retry" : a.status,
                        ImGui::GetTextLineHeight());
        ImGui::SameLine(0, gap);
    }
    rowIcon("cat_tools", exceptional ? outcomeTone : surf.textSecondary);
    ImGui::SameLine(0, gap);
    const float titleX = ImGui::GetCursorPosX();   // the second (error) line hangs from here
    // What is left on THIS line once the right-hand cluster is reserved. Measure only AFTER a
    // SameLine: following a text item the cursor has already wrapped to the line start.
    auto lineRoom = [&]() {
        return lanes.metaRight - ImGui::GetCursorPosX() - metaW - rewindW;
    };
    const bool phraseOk = outcome.structured ? outcome.succeeded : !hardFail;
    // A fold shows its base phrasing plus a count BADGE, not "Reading × 3" spelled into the title.
    const std::string humanTitle = groupN > 1
        ? ui::toolGroupTitle(a.title, 1)
        : ui::toolPhrase(a.title, a.reason, phraseOk).title;
    float badgeW = 0.0f;
    if (groupN > 1) {
        const std::string n = std::to_string(groupN);
        badgeW = ImMax(ImGui::GetTextLineHeight(),
                       ImGui::CalcTextSize(n.c_str()).x + ui::metrics::U(3.0f)) + gap;
    }
    ImGui::PushFont(fonts.medium);
    const std::string titleFit =
        chatEllipsize(humanTitle, ImMax(lineRoom() - badgeW, ui::metrics::U(6.0f)));
    ImGui::TextColored(exceptional ? outcomeTone : surf.textPrimary, "%s", titleFit.c_str());
    ImGui::PopFont();
    noteRowHead();
    const float centerY = (ImGui::GetItemRectMin().y + ImGui::GetItemRectMax().y) * 0.5f;
    if (ImGui::IsItemHovered()) {
        // Everything the single line could not afford, in one place: the untruncated phrasing,
        // when the call ran, and — only at the Full tool-detail level — the raw tool symbol.
        std::string tip;
        auto addLine = [&tip](const std::string& line) {
            if (line.empty()) return;
            if (!tip.empty()) tip += "\n";
            tip += line;
        };
        if (titleFit != humanTitle) addLine(humanTitle);
        if (groupN > 1) addLine(std::to_string(groupN) + " similar calls");
        if (diag) addLine(a.title);
        addLine(clockS);
        if (metaCache > 0)
            addLine(chatTokCount(metaCache) + " tokens read from the prompt cache by the "
                    "completion that made this call.");
        if (!tip.empty()) ImGui::SetTooltip("%s", tip.c_str());
    }
    if (groupN > 1) {
        ImGui::SameLine(0, gap);
        countBadge(groupN);
    }
    if (diag) {
        // Same ordering rule as the target below: SameLine, then measure.
        const ImVec2 wrapped = ImGui::GetCursorPos();
        ImGui::SameLine(0, gap);
        if (lineRoom() > ui::metrics::U(10.0f)) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextUnformatted(chatEllipsize("\xc2\xb7 " + a.title, lineRoom()).c_str());
            ImGui::PopStyleColor();
        } else {
            ImGui::SetCursorPos(wrapped);
        }
    }

    // ---- the target — "what it acts on" — on the SAME line, dim, elided with the full text on
    // hover. A fold's members' targets are joined here too: one line, not one line per member,
    // and each distinct target once — two reads of the same place used to print "docs, docs".
    std::string tgt, tgtTip;
    if (groupN == 1) {
        tgt = tgtTip = ui::humanTarget(a.reason);
    } else {
        std::vector<std::string> seen;
        for (const ActivityItem* mi : *group) {
            const std::string t = ui::humanTarget(mi->reason);
            if (t.empty() || std::find(seen.begin(), seen.end(), t) != seen.end()) continue;
            seen.push_back(t);
            if (!tgt.empty()) { tgt += ", "; tgtTip += "\n"; }
            tgt += t;
            tgtTip += t;
        }
    }
    if (!tgt.empty()) {
        const ImVec2 wrapped = ImGui::GetCursorPos();
        ImGui::SameLine(0, ui::metrics::U(2.0f));
        const float room = lineRoom();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        std::string fit;
        if (room > ui::metrics::U(12.0f)) {
            fit = chatEllipsize(tgt, room);
        } else {
            // Genuinely no room on the line: keep it, elided, on its own line under the title.
            ImGui::SetCursorPos(wrapped);
            ImGui::SetCursorPosX(titleX);
            fit = chatEllipsize(tgt, ImMax(lanes.metaRight - titleX, ui::metrics::U(6.0f)));
        }
        ImGui::TextUnformatted(fit.c_str());
        ImGui::PopStyleColor();
        if ((fit != tgt || groupN > 1) && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", tgtTip.c_str());
    }
    if (hasRewind) drawAiVcCallRewind(st, callI, a.title);

    // ---- the right-aligned latency/outcome figure, in the slot reserved above. SameLine FIRST,
    // then decide: if the slot did not survive the title/target elision, the figure takes its own
    // right-aligned line instead of being pasted onto whatever ran long ("standoffcache").
    if (!metaS.empty()) {
        const float rx = lanes.metaRight - ImGui::CalcTextSize(metaS.c_str()).x;
        ImGui::SameLine(0, 0);
        if (rx >= ImGui::GetCursorPosX() + gap) {
            ImGui::SetCursorPosX(rx);
        } else {
            ImGui::NewLine();
            ImGui::SetCursorPosX(ImMax(titleX, rx));
        }
        ImGui::PushStyleColor(ImGuiCol_Text, exceptional ? outcomeTone : surf.textTertiary);
        ImGui::TextUnformatted(metaS.c_str());
        ImGui::PopStyleColor();
    }

    // ---- exceptional: the FIRST line of the cause, inline under the title. Simplified mode has no
    // drawer to hold the rest, so there it prints the whole explanation, wrapped.
    if (exceptional && !errLine.empty()) {
        const float ind = titleX - ImGui::GetCursorStartPos().x;
        ImGui::Indent(ind);
        ImGui::PushStyleColor(ImGuiCol_Text, outcomeTone);
        if (simple) {
            if (!outcome.message.empty()) ImGui::TextWrapped("%s", outcome.message.c_str());
            if (!errShort.empty() && errShort != outcome.message)
                ImGui::TextWrapped("%s", errShort.c_str());
        } else {
            const std::string fit = chatEllipsize(
                errLine, ImMax(lanes.metaRight - ImGui::GetCursorPosX(), ui::metrics::U(6.0f)));
            ImGui::TextUnformatted(fit.c_str());
            if ((fit != errLine || errTip != errLine) && !errTip.empty() &&
                ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", errTip.c_str());
        }
        ImGui::PopStyleColor();
        ImGui::Unindent(ind);
    }

    // The whole header (title line + inline error) toggles the drawer; the chevron says so.
    if (hasDrawer) {
        const HeaderHit hh = headerHit(topY, ImGui::GetItemRectMax().y + rowPadY());
        if (hh.clicked) { a.expanded = !open; a.userToggled = true; }
        headerChevron(lanes, centerY, openE, hh.hovered);
    }

    if (simple) {
        // No drawer: non-image files (and, with renders off, image paths) list inline here.
        if (groupN > 1)
            for (const ActivityItem* mi : *group)
                windowMember(*mi, &ToolTimelineRow::artifactHeight, true,
                             [&] { drawArtifacts(detailOf(*mi)); });
        else
            drawArtifacts(parts);
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
                windowMember(*mi, &ToolTimelineRow::drawerHeight, openE >= 1.0f, [&] {
                    const ToolRowDetail& mp = detailOf(*mi);
                    const std::string t = ui::humanTarget(mi->reason);
                    if (!t.empty()) dimText(t.c_str());
                    drawToolArgs(mp.args, fonts);
                    drawArtifacts(mp);
                });
                ImGui::PopID();
            }
        } else {
            // The structured explanation in the user's terms, when the inline line was the raw
            // cause rather than this sentence.
            if (exceptional && !outcome.message.empty() && outcome.message != errLine) {
                ImGui::PushStyleColor(ImGuiCol_Text, outcomeTone);
                ImGui::TextWrapped("%s", outcome.message.c_str());
                ImGui::PopStyleColor();
            }
            if (declined) {
                // Full policy/guard guidance sent back to the model. The stable user explanation
                // is above; this is the actionable, model-facing detail.
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                ImGui::TextWrapped("%s", errBody.c_str());
                ImGui::PopStyleColor();
            } else if ((hardFail || timeout) && !errBody.empty() && errBody != errLine) {
                // The first line is already on the row; the full payload (a traceback, a multi-line
                // summary) lives here.
                drawToolDetailBox("##toolerr", errBody, fonts.mono,
                                  ui::metrics::px(200.0f), /*failure=*/true);
            }
            // Decline guidance can quote arguments verbatim, so do not print an identical args box.
            // Longer arguments still get their own box; right-click Copy retains the whole detail.
            if (!argsBody.empty() &&
                !(declined && errBody.find(argsBody) != std::string::npos))
                drawToolArgs(argsBody, fonts);
            drawArtifacts(parts);
        }
        endCollapseBody(cb);
    }
    // The evidence itself, after the drawer: header (what was called) -> drawer (how) -> what came
    // back. Hung from the title column like the inline error line. A fold shows every member's
    // images in call order, so grouping never hides one.
    if (inlineEvidence) {
        const float ind = titleX - ImGui::GetCursorStartPos().x;
        ImGui::Indent(ind);
        if (groupN > 1) {
            int memberI = 0;
            for (const ActivityItem* mi : *group) {
                ImGui::PushID(2000 + memberI++);
                windowMember(*mi, &ToolTimelineRow::evidenceHeight, true,
                             [&] { drawEvidence(detailOf(*mi)); });
                ImGui::PopID();
            }
        } else {
            drawEvidence(parts);
        }
        ImGui::Unindent(ind);
    }
    ImRect rowRect = endRowShell(sh);
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

bool activityIsGuardRefusal(const ActivityItem& a) { return toolRowIsDeclined(a); }

// ---- the run receipt + cost attribution --------------------------------------------------------
// A finished turn tells its story chronologically but cannot answer the three questions people
// actually ask afterwards: what changed on disk, what went wrong along the way, and where the time
// went. Every input is already on ChatMsg::activity — per-row status, the packed \x1e ART \x1e
// artifact blobs, per-stage tokens and durations — so the receipt adds no state and no second
// bookkeeping path. It REPLACES the old "N tokens in / M out" footer line, whose numbers are now
// the last clause of its headline.
namespace {

// One stage's slice of the turn's cost. `anchor` is resolved in the MESSAGE id scope (see
// activityAnchorId), which is what makes a click here land on the row the timeline will draw.
struct StageCost {
    ImGuiID     anchor = 0;
    std::string label;
    int         tokens = 0;         // in + out — the segment's weight
    int         in = 0, out = 0, ms = 0;
};

// Any exceptional call worth naming. `meta` keeps declined/blocked/UNKNOWN/invalid/harness/tool
// outcomes distinct in the expanded receipt instead of flattening every row to \"retried\".
struct ReceiptFail {
    ImGuiID     anchor = 0;
    std::string title;
    std::string why;
    std::string meta;
};

struct TurnReceipt {
    int toolCalls = 0;
    int retried   = 0;   // validation/internal/tool failures
    int skipped   = 0;   // policy and guard declines — nothing was dispatched
    int unknown   = 0;   // timeout outcome is unresolved and may still be live
    int ms        = 0;
    int tokensIn = 0, tokensOut = 0;
    std::vector<StageCost>   stages;
    std::vector<ReceiptFail> fails;
    std::vector<std::string> files;   // artifact paths, de-duplicated, in arrival order
};

// MUST run in the MESSAGE id scope, before any PushID: activityAnchorId is only meaningful there.
TurnReceipt buildReceipt(const ChatMsg& m) {
    TurnReceipt r;
    for (size_t i = 0; i < m.activity.size(); ++i) {
        const ActivityItem& a = m.activity[i];
        if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
        if (a.rowKind == 0) {
            // Orchestrator stages ARE the turn's model I/O and wall clock; tool and context rows
            // are neither (the usage footer has always summed exactly this set).
            r.tokensIn  += a.tokensIn;
            r.tokensOut += a.tokensOut;
            r.ms        += a.durationMs;
            const int tok = a.tokensIn + a.tokensOut;
            if (tok > 0) {
                StageCost s;
                s.anchor = activityAnchorId((int)i);
                s.label  = a.title.empty() ? a.stage : a.title;
                s.tokens = tok;
                s.in = a.tokensIn;
                s.out = a.tokensOut;
                s.ms = a.durationMs;
                r.stages.push_back(std::move(s));
            }
        } else if (a.rowKind == 3) {
            // The synthesized "Session files" row is presentation, not a call the engine made.
            if (a.stage != "artifacts") ++r.toolCalls;
            const ToolRowDetail parts = splitToolRowDetail(a.detail);
            const ToolOutcomePresentation outcome = toolOutcomePresentation(a);
            if (outcome.failure || outcome.declined || outcome.timeout) {
                if (outcome.declined) ++r.skipped;
                else if (outcome.timeout) ++r.unknown;
                else ++r.retried;
                ReceiptFail f;
                f.anchor = activityAnchorId((int)i);
                f.title  = ui::toolPhrase(a.title, a.reason, /*ok=*/false).title;
                f.meta   = outcome.structured ? outcome.chip
                                              : (outcome.failure ? "retried" : "");
                f.why    = outcome.message;
                const std::string rawWhy = errSummary(parts.err);
                if (!rawWhy.empty() && rawWhy != f.why) {
                    if (!f.why.empty()) f.why += "\n";
                    f.why += rawWhy;
                }
                if (f.why.empty()) f.why = a.reason.empty() ? a.title : a.reason;
                r.fails.push_back(std::move(f));
            }
            if (!parts.arts.empty()) {
                std::istringstream iss(parts.arts);
                std::string line;
                while (std::getline(iss, line)) {
                    std::string path;
                    // Same parse the transcript rows use: "3 files written" must never count a
                    // continuation line of a corrupted multi-line artifact path as a file.
                    if (!artifactLine(line, nullptr, &path)) continue;
                    if (std::find(r.files.begin(), r.files.end(), path) == r.files.end())
                        r.files.push_back(std::move(path));
                }
            }
        }
    }
    // A pure tool-loop turn stamps no stage durations; fall back to the calls' own latency rather
    // than claim "0 s" for a turn that plainly took time.
    if (r.ms == 0)
        for (const auto& a : m.activity)
            if (a.rowKind == 3) r.ms += a.durationMs;
    return r;
}

// The cost-attribution strip: one segment per stage, weighted by that stage's tokens, so an intake
// that quietly re-read a 40k context stops being indistinguishable from one that cost nothing.
// Hover names the stage and its in/out/duration; a click jumps to its card.
//
// HONEST LIMIT: ActivityItem carries NO per-stage cache-read figure. This attributes tokens and
// time by stage and claims nothing about prompt-cache reads — the cache total is a turn-level fact
// and stays on the assistant bubble's meta row, where it is actually known.
void drawCostStrip(const std::vector<StageCost>& stages, int totalTokens) {
    if (stages.empty() || totalTokens <= 0) return;
    const float  h  = ui::metrics::px(9.0f);
    const float  w  = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::px(24.0f));
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const bool clicked = ImGui::InvisibleButton("##cost_strip", ImVec2(w, h));
    const bool hovered = ImGui::IsItemHovered();
    const float mouseX = ImGui::GetIO().MousePos.x;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float r   = h * 0.5f;
    const float gap = ui::metrics::stroke(1.0f);
    dl->AddRectFilled(p0, ImVec2(p0.x + w, p0.y + h),
                      ui::kit::toneU32(ui::kit::Tone::Neutral, 0.20f), r);
    // ONE tone at alternating alpha, never a rainbow: the kit owns meaning->color and "stage 3" is
    // not a meaning. Proportion is the message; the name arrives on hover.
    float x      = p0.x;
    int   hoverI = -1;
    for (size_t i = 0; i < stages.size(); ++i) {
        float segW = w * (float)stages[i].tokens / (float)totalTokens;
        if (i + 1 == stages.size()) segW = p0.x + w - x;   // absorb rounding into the last segment
        if (segW <= 0.0f) continue;
        const bool hit = hovered && mouseX >= x && mouseX < x + segW;
        if (hit) hoverI = (int)i;
        ImDrawFlags fl = 0;
        if (i == 0)                  fl |= ImDrawFlags_RoundCornersLeft;
        if (i + 1 == stages.size())  fl |= ImDrawFlags_RoundCornersRight;
        if (!fl)                     fl  = ImDrawFlags_RoundCornersNone;
        dl->AddRectFilled(ImVec2(x, p0.y), ImVec2(ImMax(x, x + segW - gap), p0.y + h),
                          ui::kit::toneU32(ui::kit::Tone::Accent,
                                           hit ? 1.0f : ((i % 2) ? 0.55f : 0.85f)),
                          r, fl);
        x += segW;
    }
    if (hoverI >= 0) {
        const StageCost& s = stages[(size_t)hoverI];
        ImGui::SetTooltip("%s\n%s in / %s out \xc2\xb7 %s\nClick to jump to this step.",
                          s.label.c_str(), ui::fmtTokens(s.in).c_str(),
                          ui::fmtTokens(s.out).c_str(),
                          ui::kit::fmtDuration(s.ms / 1000.0).c_str());
        if (clicked) {
            timelineScrollTo(s.anchor);
            chatLogBreakStick();   // stick-to-bottom would snap the deferred jump straight back
        }
    }
}

// Receipt disclosure state. NOT a ChatMsg field — app_state.h stays unchanged — so it keys off
// the message id scope, which is stable per turn and per conversation.
std::unordered_map<ImGuiID, bool> g_receiptOpen;

const char* receiptFileIcon(const std::string& path) {
    const std::string e = chatLowerExt(path);
    if (e == ".png" || e == ".jpg" || e == ".jpeg" || e == ".bmp" || e == ".webp")
        return "file_image";
    if (e == ".stl" || e == ".3mf" || e == ".obj" || e == ".ply")   return "file_mesh";
    if (e == ".step" || e == ".step+" || e == ".stp" || e == ".iges" || e == ".igs")
        return "file_step";
    if (e == ".json" || e == ".csv" || e == ".yaml" || e == ".yml") return "file_data";
    if (e == ".py" || e == ".cpp" || e == ".h" || e == ".js" || e == ".ts") return "file_code";
    if (e == ".zip" || e == ".tar" || e == ".gz" || e == ".7z")     return "file_archive";
    if (e == ".md" || e == ".txt" || e == ".pdf")                   return "file_doc";
    return "file_generic";
}

}  // namespace

// The finished turn's footer: ONE rounded, softly lit row of compact chips — "98 tool calls",
// "4 retried", "3 files written", "1m 12s", "41k in / 3k out" — whose whole surface discloses the
// detail (calls needing attention, files written, where the tokens went) under a chevron.
void drawTurnReceipt(AppState& st, ChatMsg& m) {
    if (m.activity.empty()) return;
    // Built HERE, in the message id scope, before the shell pushes an id of its own: every anchor
    // in the receipt has to resolve to the same id the timeline's row loop will compute.
    const TurnReceipt r = buildReceipt(m);
    const int totalTok = r.tokensIn + r.tokensOut;
    if (r.toolCalls == 0 && totalTok == 0 && r.files.empty() && r.fails.empty()) return;
    const bool hasBody = !r.fails.empty() || !r.files.empty() || (!r.stages.empty() && totalTok > 0);

    ui::kit::pushDensity(ui::kit::Density::Compact);
    if (g_receiptOpen.size() > 4096) g_receiptOpen.clear();
    bool&         openState = g_receiptOpen[ImGui::GetID("##turn_receipt")];
    const bool    wasOpen   = openState;
    const ImGuiID animKey   = ImGui::GetID("##turn_receipt_anim");
    const bool    wantOpen  = openState && hasBody;
    const float   openE = ui::chatCollapseAnim() ? collapseEase(animKey, wantOpen)
                                                 : (wantOpen ? 1.0f : 0.0f);
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    RowShell sh = beginRowShell("##turn_receipt", ui::kit::Tone::Neutral, kStageWash, kStageEdge,
                                hasBody, openE);
    const RowLanes lanes = rowLanes(hasBody);
    const float    topY  = ImGui::GetCursorScreenPos().y;
    float          centerY = topY + ImGui::GetTextLineHeight() * 0.5f;
    // The chips flow inside a child that stops short of the chevron lane, so a wrapped second
    // chip line can never run under the disclosure.
    ImGui::BeginChild("##receipt_chips",
                      ImVec2(ImMax(lanes.metaRight - ImGui::GetCursorPosX(), ui::metrics::U(12.0f)),
                             0.0f),
                      ImGuiChildFlags_AutoResizeY,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ui::kit::chipFlowBegin("##receipt_flow");
    bool firstChip = true;
    auto chipOf = [&](const std::string& label, ui::kit::Tone t, bool dot) {
        ui::kit::flowChip(label.c_str(), t, dot);
        if (firstChip) {
            centerY = (ImGui::GetItemRectMin().y + ImGui::GetItemRectMax().y) * 0.5f;
            firstChip = false;
        }
    };
    if (r.toolCalls > 0)
        chipOf(std::to_string(r.toolCalls) + (r.toolCalls == 1 ? " tool call" : " tool calls"),
               ui::kit::Tone::Neutral, false);
    if (r.retried > 0) chipOf(std::to_string(r.retried) + " retried", ui::kit::Tone::Warn, true);
    if (r.skipped > 0) chipOf(std::to_string(r.skipped) + " skipped", ui::kit::Tone::Info, true);
    if (r.unknown > 0) chipOf(std::to_string(r.unknown) + " UNKNOWN", ui::kit::Tone::Warn, true);
    if (!r.files.empty())
        chipOf(std::to_string(r.files.size()) +
                   (r.files.size() == 1 ? " file written" : " files written"),
               ui::kit::Tone::Neutral, false);
    if (r.ms > 0) chipOf(ui::kit::fmtDuration(r.ms / 1000.0), ui::kit::Tone::Neutral, false);
    if (totalTok > 0)
        chipOf(ui::fmtTokens(r.tokensIn) + " in / " + ui::fmtTokens(r.tokensOut) + " out",
               ui::kit::Tone::Neutral, false);
    ui::kit::chipFlowEnd();
    ImGui::EndChild();
    if (hasBody) {
        const HeaderHit hh = headerHit(topY, ImGui::GetItemRectMax().y + rowPadY());
        if (hh.clicked) openState = !openState;
        headerChevron(lanes, centerY, openE, hh.hovered);
    }
    if (openE > 0.0f && hasBody) {
        CollapseBody cb = beginCollapseBody(animKey, openE, "##receipt_body");
        if (!r.fails.empty()) {
            ui::kit::sectionHeader("Calls needing attention", nullptr, nullptr,
                                   (int)r.fails.size(), /*first=*/true);
            for (size_t i = 0; i < r.fails.size(); ++i) {
                const ReceiptFail& f = r.fails[i];
                ui::kit::RowStyle rs;
                rs.secondary = f.why.c_str();
                rs.meta      = f.meta.c_str();
                rs.tooltip   = "Jump to this step in the transcript.";
                ImGui::PushID((int)i);
                const bool hit = ui::kit::row("##fail", f.title.c_str(), rs);
                ImGui::PopID();
                if (hit) { timelineScrollTo(f.anchor); chatLogBreakStick(); }
            }
        }
        if (!r.files.empty()) {
            ui::kit::sectionHeader("Written to disk", "artifacts", nullptr, (int)r.files.size(),
                                   /*first=*/r.fails.empty());
            for (size_t i = 0; i < r.files.size(); ++i) {
                const std::string& p = r.files[i];
                const size_t slash = p.find_last_of("/\\");
                const std::string base = (slash == std::string::npos) ? p : p.substr(slash + 1);
                ui::kit::RowStyle rs;
                rs.icon    = receiptFileIcon(p);
                rs.tooltip = p.c_str();
                ImGui::PushID((int)i);
                const bool hit = ui::kit::row("##file", base.c_str(), rs);
                ImGui::PopID();
                if (hit) chatArtifactOpenSmart(st, p);
            }
        }
        if (!r.stages.empty() && totalTok > 0) {
            ui::kit::sectionHeader("Where the tokens went", nullptr, nullptr, -1,
                                   /*first=*/r.fails.empty() && r.files.empty());
            drawCostStrip(r.stages, totalTok);
        }
        endCollapseBody(cb);
    }
    endRowShell(sh);
    ui::kit::popDensity();
    // The turn's rendered height just changed; the render-windowing cache has to re-measure it.
    if (openState != wasOpen) m.uiCachedH = -1.0f;
}

// ---- the run spine -----------------------------------------------------------------------------
namespace {
// One status mark on the rail. Collected during the row loop and painted afterwards, because the
// rail's extent is only known once every row has landed. `joined` = a rail segment connects this
// mark to the previous one; the assistant's own prose breaks the rail, so each burst of tool work
// between two sentences reads as its own run.
struct SpineMark {
    float           y;
    ui::kit::Status st;
    bool            joined = true;
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

// What this row's mark says. It uses the same structured taxonomy as the row's own glyph so the
// gutter cannot disagree with the label beside it. Missing/future values deliberately fall through
// to the legacy status mapping.
ui::kit::Status spineStatusOf(const ActivityItem& a, bool turnRunning, bool frontier,
                             const ToolOutcomePresentation* outcome = nullptr) {
    switch (outcome ? outcome->info.kind : ui::copilot::toolOutcomeFromDetail(a.detail).kind) {
        case ui::copilot::ToolOutcomeKind::Ok:
            return ui::kit::Status::Done;
        case ui::copilot::ToolOutcomeKind::PolicyRefusal:
            return ui::kit::Status::Skipped;
        case ui::copilot::ToolOutcomeKind::GuardBlock:
            return ui::kit::Status::Skipped;
        case ui::copilot::ToolOutcomeKind::ValidationError:
            return ui::kit::Status::Failed;
        case ui::copilot::ToolOutcomeKind::TimeoutUnknown:
            return ui::kit::Status::Retry;
        case ui::copilot::ToolOutcomeKind::InternalError:
            return ui::kit::Status::Failed;
        case ui::copilot::ToolOutcomeKind::ToolError:
            return ui::kit::Status::Failed;
        case ui::copilot::ToolOutcomeKind::Legacy:
        default:
            break;
    }
    if (a.status == "fail" || a.status == "error") return ui::kit::Status::Failed;
    if (a.status == "running" || (turnRunning && frontier)) return ui::kit::Status::Running;
    if (a.status == "done" || a.status == "ok" || a.status == "pass") return ui::kit::Status::Done;
    if (a.status == "skipped") return ui::kit::Status::Skipped;
    if (a.status == "retry") return ui::kit::Status::Retry;
    return ui::kit::Status::Pending;
}

// Paint the rail and its marks. Called with the splitter on the BACK channel so the hairline sits
// under the rows no matter what order they were submitted in.
//
// The rail spans mark-to-mark, never the whole turn height: that is what makes it survive the
// grouped-tool fold, the collapse animations and the synthesized "Session files" presentation row.
// A row that drew nothing contributed no mark, so the rail simply does not reach it — it can never
// draw a segment to a step that is not on screen. Each segment stops short of the marks at both
// ends: a hairline driven THROUGH a check mark is the sharp joint that made the old spine read as
// wiring; with a gap the marks are beads on a thread.
void paintRunSpine(ImDrawList* dl, float railX, float cell,
                   const std::vector<SpineMark>& marks, bool running) {
    if (!dl || marks.empty()) return;
    const ui::AdaptiveSurface& surf = chatSurf();
    const float r     = cell * 0.36f;                       // lighter marks than the text cell
    const float clear = r + ui::metrics::U(0.75f);          // rail stops this far from a mark
    const float thick = ui::metrics::stroke(1.0f);
    // The track: a hairline in the tertiary ink, so it reads as structure rather than content.
    // 0.75, not 0.55: pixel-measured against the light glass surface, 0.55 put only ~30/255
    // between the rail and the panel — below the point where a hairline reads as a deliberate
    // connector rather than a compression artifact.
    ImVec4 track = surf.textTertiary;
    track.w *= 0.75f;
    const ImU32 trackU = ImGui::ColorConvertFloat4ToU32(track);
    for (size_t i = 1; i < marks.size(); ++i) {
        if (!marks[i].joined) continue;
        const float y0 = marks[i - 1].y + clear;
        const float y1 = marks[i].y - clear;
        if (y1 > y0) dl->AddLine(ImVec2(railX, y0), ImVec2(railX, y1), trackU, thick);
    }

    // The LIVE segment: from the last completed mark down to the running one, marching downward.
    // This is the whole point of the spine — "how far into the turn is the agent" answerable at a
    // glance. Only drawn while a turn is actually running, so a finished transcript is completely
    // static and requests no frames.
    if (running) {
        size_t runIdx = marks.size();
        for (size_t i = 0; i < marks.size(); ++i)
            if (marks[i].st == ui::kit::Status::Running) { runIdx = i; break; }
        if (runIdx > 0 && runIdx < marks.size() && marks[runIdx].joined) {
            const float segTop = marks[runIdx - 1].y + clear;
            const float segBot = marks[runIdx].y - clear;
            if (segBot > segTop) {
                // A short bright head sliding down the segment, rather than pulsing the whole
                // length: motion with a direction reads as progress, motion without one reads as
                // an error state.
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
    for (const SpineMark& mk : marks)
        ui::kit::statusGlyph(dl, ImVec2(railX, mk.y), r, mk.st, surf);
}
}  // namespace


void drawAgentTimeline(AppState& st, ChatMsg& m, const ui::Fonts& fonts, bool showThinking,
                       bool isLastTurn) {
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
    ToolTimelineCache& toolCache = toolTimelineCache(st, m, fonts);
    auto cachedTool = [&](size_t idx) -> ToolTimelineRow* {
        return idx < toolCache.rows.size() && toolCache.rows[idx].valid ? &toolCache.rows[idx] : nullptr;
    };
    // No blanket leading Dummy here: EVERY dispatched row (both the local draw* functions below
    // and cards.cpp's corp/firm/swarm family) already opens with its own cpChatRowGap() Dummy —
    // one used to sit here too, doubling the gap above the first card in every turn.
    // The live frontier: the most recent row this loop will actually draw (skip conditions
    // below). Only that row auto-expands mid-turn; earlier rows collapse to summary+icon.
    size_t lastVisible = (size_t)-1;
    for (size_t i = 0; i < m.activity.size(); ++i) {
        const ActivityItem& a = m.activity[i];
        if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
        if (isProseOutputRow(a) && !ui::streamTextVisible(a.detail)) continue;
        lastVisible = i;
    }

    // ---- the run spine -------------------------------------------------------------------
    // A hairline down the left gutter of the turn with each step's status mark ON it, so "how far
    // into this run is the agent" is answerable across the room. The rail is derived ENTIRELY
    // from where rows actually landed this frame, which is what makes it survive the grouped-tool
    // fold, the collapse animations and the synthesized "Session files" row: a row that did not
    // draw contributes no mark, and the rail spans mark-to-mark, so it can never run to a step
    // that is not on screen. The assistant's prose steps OUT of the gutter (full width) and breaks
    // the rail there.
    //
    // It paints into a BACK channel of the window draw list so it sits under every card
    // regardless of submission order. ChannelSplit merges in its destructor, so every exit from
    // this function — the normal path, and a throw out of any card painter — leaves the draw
    // list intact; an unmerged splitter silently corrupts the whole window.
    const float spineCell = ImGui::GetTextLineHeight();             // the status-mark glyph cell
    const float spineW    = spineCell + ui::kit::padX() * 0.5f;     // gutter the rows indent past
    const float railX     = ImGui::GetCursorScreenPos().x + spineCell * 0.5f;
    // Keep the nearest offscreen mark at each boundary, so clipped rail segments are identical
    // without generating glyphs/vertices for every tool in a long active turn.
    static std::vector<SpineMark> spineMarks;
    spineMarks.clear();
    const ImRect timelineClip = ImGui::GetCurrentWindow()->ClipRect;
    bool spineBreak = false;   // prose landed since the last mark: do not join across it
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
        if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
        const bool liveFrontier = turnRunning && (i == lastVisible);
        // Deferred scroll-to-anchor: the receipt / cost strip requested this row LAST frame, when
        // the render-windowing cache may still have had the message collapsed to a spacer.
        // Resolved BEFORE PushID((int)i) so the id matches activityAnchorId's — that helper is
        // documented to be called from the turn footer, i.e. the message scope, not the row scope.
        // The cursor is at the row's top edge here, which is the position we want to bring into
        // view; a tall card centred on its own middle would push its header offscreen.
        const bool anchorRequested = g_pendingAnchor == activityAnchorId((int)i);
        consumeAnchorScroll(activityAnchorId((int)i));
        ImGui::PushID((int)i);
        // Where this row's status mark sits on the rail, and what it says. Captured around the
        // dispatch so a row that draws nothing (an empty prose segment) leaves no orphan mark.
        const float rowTopY = ImGui::GetCursorScreenPos().y;
        ToolTimelineRow* const firstTool = a.rowKind == 3 ? cachedTool(i) : nullptr;
        const ui::kit::Status rowStatus = spineStatusOf(a, turnRunning, i == lastVisible,
                                                       firstTool ? &firstTool->outcome : nullptr);
        const bool onSpine = !isProseOutputRow(a) && a.rowKind != 1 && a.rowKind != 20;
        g_rowHeadY = -1.0f;   // each painter records its own header centre (noteRowHead)
        if (isProseOutputRow(a)) {
            // The model's own words, in the column at the point it said them — full width, out of
            // the spine gutter, and the rail breaks around them.
            ImGui::Unindent(spineW);
            drawProseBubble(a, fonts);
            ImGui::Indent(spineW);
            if (ImGui::GetCursorScreenPos().y > rowTopY) spineBreak = true;
        } else if (a.rowKind == 20) {
            // ask_user marker — the question card (form while pending, summary once answered)
            // at the point the agent asked, full width like prose, breaking the rail.
            ImGui::Unindent(spineW);
            drawAskMarker(st, m, a, fonts);
            ImGui::Indent(spineW);
            if (ImGui::GetCursorScreenPos().y > rowTopY) spineBreak = true;
        } else if (a.rowKind == 1) {
            drawContextChip(a, fonts);
        } else if (a.rowKind == 2) {
            drawThoughtBox(a, fonts, showThinking, liveFrontier);
        } else if (a.rowKind == 3) {
            // TWO independent folds: true failures have an empty groupKey and taxonomy classes are
            // never mixed, while only successful calls enter the similarity fold. Declined and
            // UNKNOWN rows remain individually visible.
            //
            // Collapse consecutive identical tool+target failures of the SAME class into one row
            // with a count. Show the last item so the visible error_preview is the most recent.
            int failCount = 1;
            size_t drawIdx = i;
            size_t frontIdx = i;     // the run's LAST row decides live-frontier auto-expansion
            size_t groupCount = 1;
            const size_t firstIdx = i;
            const ToolOutcomePresentation uncachedOutcome = firstTool ? ToolOutcomePresentation{}
                                                                      : toolOutcomePresentation(a);
            const ToolOutcomePresentation& aOutcome = firstTool ? firstTool->outcome : uncachedOutcome;
            if (aOutcome.failure && !(firstTool ? firstTool->images
                                                : hasImageArtifacts(splitToolRowDetail(a.detail).arts))) {
                const ui::copilot::ToolOutcomeKind aKind = aOutcome.info.kind;
                size_t j = i + 1;
                while (j < m.activity.size()) {
                    const ActivityItem& n = m.activity[j];
                    const ToolTimelineRow* next = cachedTool(j);
                    const ToolOutcomePresentation fallback = next ? ToolOutcomePresentation{} : toolOutcomePresentation(n);
                    const ToolOutcomePresentation& nOutcome = next ? next->outcome : fallback;
                    if (n.rowKind != 3 || !nOutcome.failure) break;
                    // A failed geometry check may return a diagnostic image. Never fold away
                    // an attempt's evidence in favour of the last attempt's error summary.
                    if (next ? next->images : hasImageArtifacts(splitToolRowDetail(n.detail).arts)) break;
                    if (n.title != a.title || n.reason != a.reason) break;
                    if (nOutcome.info.kind != aKind) break;
                    ++failCount;
                    ++j;
                }
                drawIdx = j - 1;
                frontIdx = j - 1;
                i = j - 1;  // loop ++i advances past the coalesced run
            } else if (ui::chatGroupToolCalls() && aOutcome.succeeded &&
                       a.status != "running" && a.stage != "artifacts") {
                // Fold a run of consecutive successful calls that phrase to the same groupKey
                // into ONE card ("Rendering the part x 3") instead of N near-identical cards.
                const std::string uncachedKey = firstTool ? std::string()
                    : ui::toolPhrase(a.title, a.reason, true).groupKey;
                const std::string& key = firstTool ? firstTool->groupKey : uncachedKey;
                if (!key.empty()) {
                    size_t j = i + 1;
                    while (j < m.activity.size()) {
                        const ActivityItem& n = m.activity[j];
                        const ToolTimelineRow* next = cachedTool(j);
                        const ToolOutcomePresentation fallback = next ? ToolOutcomePresentation{} : toolOutcomePresentation(n);
                        const ToolOutcomePresentation& nOutcome = next ? next->outcome : fallback;
                        if (n.rowKind != 3 || !nOutcome.succeeded || n.status == "running") break;
                        if (n.stage == "artifacts") break;
                        if (next ? next->groupKey != key
                                 : ui::toolPhrase(n.title, n.reason, true).groupKey != key) break;
                        ++j;
                    }
                    if (j > i + 1) {
                        groupCount = j - i;
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
                if (groupCount > 1) toolOrdinal += (int)groupCount - 1;
            }
            // A grouped card must never offer a rewind glyph pointing at an arbitrary member;
            // the identical-failure fold still rewinds to before the FIRST attempt. The most
            // recent row of the whole chat (the live frontier of the LAST turn) offers none
            // either: it IS the current state — there is nothing to rewind to.
            const bool chatFrontier = isLastTurn && (frontIdx == lastVisible);
            const int rewindIdx = (groupCount > 1 || chatFrontier) ? -1 : callIdx;
            bool changed = !firstTool;
            bool anchor = anchorRequested;
            ImGui::PopID();
            for (size_t k = firstIdx; k <= frontIdx; ++k) {
                const ToolTimelineRow* member = cachedTool(k);
                changed = changed || !member || member->changed;
                const ImGuiID messageAnchor = activityAnchorId((int)k);
                anchor = anchor || g_pendingAnchor == messageAnchor;
                consumeAnchorScroll(messageAnchor);
            }
            ImGui::PushID((int)firstIdx);
            const bool frontier = turnRunning && (frontIdx == lastVisible);
            const ImRect clip = ImGui::GetCurrentWindow()->ClipRect;
            const float slack = ImGui::GetTextLineHeightWithSpacing() * 2.0f;
            const bool reusable = firstTool && !changed && firstTool->height > 0.0f &&
                firstTool->settled && firstTool->end == frontIdx && firstTool->rewind == rewindIdx &&
                firstTool->frontier == frontier && a.stage != "artifacts";
            if (reusable && !anchor &&
                (rowTopY + firstTool->height < clip.Min.y - slack || rowTopY > clip.Max.y + slack)) {
                const float y = ImGui::GetCursorPosY();
                ImGui::Dummy(ImVec2(0.0f, ImMax(0.0f, firstTool->height - ImGui::GetStyle().ItemSpacing.y)));
                ImGui::SetCursorPosY(y + firstTool->height);
                g_rowHeadY = rowTopY + firstTool->headOffset;
            } else {
                std::vector<ActivityItem*> group;
                if (groupCount > 1) {
                    group.reserve(groupCount);
                    for (size_t k = firstIdx; k <= frontIdx; ++k) group.push_back(&m.activity[k]);
                }
                drawToolCallRowImpl(st, m.activity[drawIdx], fonts, failCount, frontier, rewindIdx,
                                    group.size() > 1 ? &group : nullptr, onSpine, &m.activity, &toolCache.rows);
                if (firstTool) {
                    const float height = ImGui::GetCursorScreenPos().y - rowTopY;
                    // AutoResizeY consumes the previous frame's content extent. Never freeze
                    // the first padding-only measurement into an offscreen spacer.
                    const bool stableHeight = firstTool->height > 0.0f &&
                                              std::fabs(firstTool->height - height) < 0.5f;
                    firstTool->height = height;
                    firstTool->headOffset = g_rowHeadY - rowTopY;
                    firstTool->end = frontIdx;
                    firstTool->rewind = rewindIdx;
                    firstTool->frontier = frontier;
                    const auto anim = s_collapseT.find(ImGui::GetID("##toolrow_anim"));
                    firstTool->settled = stableHeight && (!ui::chatCollapseAnim() ||
                        anim == s_collapseT.end() || anim->second.t == 0.0f || anim->second.t == 1.0f);
                }
            }
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
            drawSubRunCard(a, fonts, turnRunning && a.status == "running");
        } else if (a.rowKind == 18) {
            // Plan position marker — the plan/objectives section renders HERE, where the plan
            // first arrived in the event stream, instead of pinned to the top of the turn.
            if (m.planState.present) drawPlanSection(m.planState, m.activity, fonts);
        } else if (isThinkingItem(a)) {
            drawThinkingBlock(a, fonts, showThinking, liveFrontier);
        } else {
            drawActionCard(a, fonts, liveFrontier, onSpine);
        }
        // The mark's y: the centre of the row's own header line, as the painter recorded it — so
        // the glyph sits level with the title, and a card with a long open drawer never floats its
        // mark halfway down the rail. Painters that record nothing (cards.cpp's family) fall back
        // to the first-line estimate. Clamped inside the row either way.
        if (onSpine) {
            const float rowBotY = ImGui::GetCursorScreenPos().y;
            if (rowBotY > rowTopY) {
                const float half = spineCell * 0.5f;
                const float my = g_rowHeadY >= rowTopY
                    ? g_rowHeadY
                    : rowTopY + ui::metrics::cpChatRowGap() + ui::kit::padY() + half;
                const float lo = rowTopY + half;
                const float hi = ImMax(lo, rowBotY - half);
                const SpineMark mark{ImClamp(my, lo, hi), rowStatus, !spineBreak};
                if (mark.y < timelineClip.Min.y - spineCell) {
                    if (spineMarks.empty()) spineMarks.push_back(mark);
                    else spineMarks.back() = mark;
                } else if (spineMarks.empty() || spineMarks.back().y <= timelineClip.Max.y + spineCell) {
                    spineMarks.push_back(mark);
                }
                spineBreak = false;
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
