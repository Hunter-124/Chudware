// Copilot chat log: message-list rendering — the user block
// (bubble + rewind/edit row), the provider log, the assistant turn (prose + activity timeline
// + cards), and the windowed log (height-cached spacer rows for off-screen turns) with
// stick-to-bottom autoscroll. Card/timeline bodies live in cards.cpp / activity_timeline.cpp.

#include "copilot_internal.h"
#include "agent_text.h"
#include "image_lightbox.h"       // attachmentImagePreview / openImageLightbox
#include "inline_image_refs.h"
#include "util/time_local.h"
#include "ui/copilot/context_chips.h"  // context-picker chips on the sent bubble
#include "ui/ai/ai_prefs.h"

#include <unordered_map>

namespace ui {

// ---- message age helpers ------------------------------------------------------
// Relative age for the in-bubble meta row ("3m ago"), switching to a bare date
// once the message is over a day old. The hover tooltip carries the concise
// absolute time ("4:37 PM"), adding the date when it's over a day old.
static bool chatLocalTime(double ts, std::tm& out) {
    if (ts <= 0.0) return false;
    const std::time_t t = (std::time_t)ts;
    return chudware::util::localTime(t, out);
}

static const char* const kChatMonths[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};

// "2:26 PM" (or "2:26:07 PM").
static std::string chatClock(const std::tm& t, bool seconds) {
    int h = t.tm_hour % 12; if (h == 0) h = 12;
    char buf[24];
    if (seconds)
        std::snprintf(buf, sizeof(buf), "%d:%02d:%02d %s", h, t.tm_min, t.tm_sec,
                      t.tm_hour < 12 ? "AM" : "PM");
    else
        std::snprintf(buf, sizeof(buf), "%d:%02d %s", h, t.tm_min, t.tm_hour < 12 ? "AM" : "PM");
    return buf;
}

// The visible message timestamp: clock time today, "Sep 26, 2:26 PM" earlier this year,
// "Sep 26, 2025, 2:26 PM" before that. Static text — it never re-measures as time passes.
static std::string chatStampLabel(double ts) {
    std::tm tmv; if (!chatLocalTime(ts, tmv)) return std::string();
    std::tm nowTm; chatLocalTime(chatNowEpoch(), nowTm);
    const std::string clock = chatClock(tmv, false);
    char buf[64];
    if (tmv.tm_year == nowTm.tm_year && tmv.tm_yday == nowTm.tm_yday)
        return clock;
    if (tmv.tm_year == nowTm.tm_year)
        std::snprintf(buf, sizeof(buf), "%s %d, %s", kChatMonths[tmv.tm_mon], tmv.tm_mday,
                      clock.c_str());
    else
        std::snprintf(buf, sizeof(buf), "%s %d, %d, %s", kChatMonths[tmv.tm_mon], tmv.tm_mday,
                      tmv.tm_year + 1900, clock.c_str());
    return buf;
}

// Hover twin: how long ago, then the full date with seconds ("3m ago · Sep 27, 2026, 2:26:07 PM").
static std::string chatTimeTooltip(double ts) {
    std::tm tmv; if (!chatLocalTime(ts, tmv)) return std::string();
    const double age = chatNowEpoch() - ts;
    char rel[32];
    if (age < 45.0) std::snprintf(rel, sizeof(rel), "just now");
    else if (age < 3600.0) std::snprintf(rel, sizeof(rel), "%dm ago", std::max(1, (int)(age / 60.0)));
    else if (age < 86400.0) std::snprintf(rel, sizeof(rel), "%dh ago", (int)(age / 3600.0));
    else std::snprintf(rel, sizeof(rel), "%dd ago", (int)(age / 86400.0));
    char buf[96];
    std::snprintf(buf, sizeof(buf), "%s \xc2\xb7 %s %d, %d, %s", rel, kChatMonths[tmv.tm_mon],
                  tmv.tm_mday, tmv.tm_year + 1900, chatClock(tmv, true).c_str());
    return buf;
}

// Timestamp with the turn's prompt-cache figure appended ("2:26 PM · 21.4k cached").
// Shared by the in-card meta row and the above-card model label so a mutating
// turn's cache reads are visible in the same form as a prose turn's.
// `cacheTok` is the PEAK single-completion read (ChatMsg::cacheReadTok), never the
// turn sum — with prompt caching the same prefix is re-read every call, so the sum
// outgrows the context window and reads as nonsense next to the meter.
static std::string chatStampCacheLabel(double ts, int cacheTok) {
    std::string age = chatStampLabel(ts);
    if (cacheTok > 0 && !age.empty()) {
        age += " \xc2\xb7 ";
        age += chatTokCount(cacheTok);
        age += " cached";
    }
    return age;
}

// Tooltip twin of chatStampCacheLabel: relative + full time and the cache sentence. The tooltip is
// where the CUMULATIVE figure belongs — it is a cost quantity, and spelling out the
// call count is what stops the two numbers looking contradictory.
static std::string chatAgeCacheTip(double ts, int cacheTok, int totTok = 0, int calls = 0) {
    std::string tip = chatTimeTooltip(ts);
    if (cacheTok > 0) {
        tip += " \xc2\xb7 ";
        tip += chatTokCount(cacheTok);
        tip += " tokens served from the prompt cache on this turn's largest model call";
        if (calls > 1 && totTok > cacheTok) {
            tip += "\n";
            tip += chatTokCount(totTok);
            tip += " cached-token reads billed across the turn's " + std::to_string(calls)
                 + " calls (the same prefix is re-read each call).";
        }
    }
    return tip;
}

// In-bubble meta row: author name (left) + relative age (right; hover = absolute
// time). Renders as a bubble's first (assistant) or last (user) row so the author
// lives INSIDE the message box. Name may be null (age-only row). cacheTok > 0 appends
// the turn's peak prompt-cache read to the age ("3m ago · 21.4k cached"); totTok/calls
// carry the cumulative figure, which only ever appears in the hover tooltip.
static void drawMsgMetaRow(const ui::Fonts& fonts, const char* name, const ImVec4& nameCol,
                           double ts, int cacheTok = 0, int totTok = 0, int calls = 0,
                           const char* fullName = nullptr) {
    const ui::AdaptiveSurface& surf = chatSurf();
    const std::string age = chatStampCacheLabel(ts, cacheTok);
    if ((!name || !name[0]) && age.empty()) return;   // legacy turn, no model — no blank row
    // Right inset clears the hover "copy" glyph that owns the bubble's top-right corner
    // (14px glyph + 4px pad, chatHoverCopyGlyph) plus a small breathing gap.
    const float rightInset = (14.0f + 4.0f + 6.0f) * ui::uiScale;
    const float ageW = age.empty() ? 0.0f : ImGui::CalcTextSize(age.c_str()).x;
    if (name && name[0]) {
        ImGui::PushFont(fonts.medium);
        // The name shares this row with the right-anchored age, so it gets only what is
        // left over: an unbounded draw let a long id ("Kimi K3-256k (custom)") run past
        // the card's right edge. Truncated names keep the full id on hover.
        float nameMax = ImGui::GetWindowContentRegionMax().x - ImGui::GetCursorPosX()
                      - rightInset - ageW;
        if (!age.empty()) nameMax -= ImGui::GetStyle().ItemSpacing.x;
        const std::string fit = chatEllipsize(name, std::max(nameMax, 24.0f * ui::uiScale));
        ImGui::TextColored(nameCol, "%s", fit.c_str());
        ImGui::PopFont();
        if (fit != name && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", (fullName && fullName[0]) ? fullName : name);
    } else {
        ImGui::Dummy(ImVec2(1.0f, ImGui::GetTextLineHeight()));
    }
    if (!age.empty()) {
        const float rx = ImGui::GetWindowContentRegionMax().x - ageW - rightInset;
        ImGui::SameLine();
        ImGui::SetCursorPosX(std::max(ImGui::GetCursorPosX(), rx));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted(age.c_str());
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) {
            const std::string tip = chatAgeCacheTip(ts, cacheTok, totTok, calls);
            if (!tip.empty()) ImGui::SetTooltip("%s", tip.c_str());
        }
    }
}

// ---- ordered text / images in a SENT user message -----------------------------
// imagePaths is the exact send-time roster. Numbered refs index it; legacy markers consume
// that roster in order, never resolve their names across paste-cache day buckets. Without a
// recorded path the marker stays prose. Only unreferenced historical attachments trail the body.
namespace {
struct SentBodySegment {
    std::string text;
    std::vector<ui::copilot::KeywordRange> keywords;
    size_t image = std::string::npos;
};

struct SentBody {
    std::string source;
    std::vector<std::string> paths;
    std::vector<SentBodySegment> segments;
    int lastFrame = 0;
};

// Sent messages are immutable in the usual draw path. Cache the split and keyword remapping
// rather than copying/stripping every segment every frame; compare inputs for edits/session reuse.
static const SentBody& sentBody(const ChatMsg& m) {
    static std::unordered_map<ImGuiID, SentBody> cache;
    const ImGuiID id = ImGui::GetID("##sent_body");
    auto it = cache.find(id);
    if (it == cache.end()) {
        constexpr size_t kBodyCacheCap = 128;
        if (cache.size() >= kBodyCacheCap) {
            auto oldest = cache.begin();
            for (auto entry = cache.begin(); entry != cache.end(); ++entry)
                if (entry->second.lastFrame < oldest->second.lastFrame) oldest = entry;
            cache.erase(oldest);
        }
        it = cache.emplace(id, SentBody{}).first;
    }
    SentBody& body = it->second;
    body.lastFrame = ImGui::GetFrameCount();
    if (body.source == m.text && body.paths == m.imagePaths) return body;
    body.source = m.text;
    body.paths = m.imagePaths;
    body.segments.clear();

    auto refs = ui::copilot::inlineImageRefs(m.text, m.imagePaths.size());
    constexpr std::string_view legacyPrefix = "[attached image: ";
    size_t legacyImage = 0;
    for (size_t pos = 0; (pos = m.text.find(legacyPrefix, pos)) != std::string::npos;) {
        const size_t end = m.text.find(']', pos + legacyPrefix.size());
        if (end == std::string::npos) break;
        if (legacyImage < m.imagePaths.size())
            refs.push_back({pos, end + 1, legacyImage});
        ++legacyImage;
        pos = end + 1;
    }
    std::sort(refs.begin(), refs.end(), [](const auto& a, const auto& b) {
        return a.begin < b.begin;
    });

    // Match keywords before stripping markdown so keywords inside code remain ordinary prose.
    std::vector<size_t> srcOf;
    std::string plain = stripMdMarks(m.text, &srcOf);
    auto keywords = ui::copilot::magicKeywordRanges(m.text);
    const auto plainOffset = [&](size_t raw) {
        return static_cast<size_t>(std::lower_bound(srcOf.begin(), srcOf.end(), raw) - srcOf.begin());
    };
    for (auto& r : keywords) {
        r.begin = plainOffset(r.begin);
        r.end = plainOffset(r.end);
    }
    const auto addProse = [&](size_t begin, size_t end) {
        if (end <= begin) return;
        SentBodySegment segment;
        segment.text = plain.substr(begin, end - begin);
        for (const auto& r : keywords) {
            if (r.begin >= end) break;
            if (r.begin >= begin && r.end <= end) {
                auto local = r;
                local.begin -= begin;
                local.end -= begin;
                segment.keywords.push_back(local);
            }
        }
        body.segments.push_back(std::move(segment));
    };
    std::vector<bool> referenced(m.imagePaths.size(), false);
    size_t cursor = 0;
    for (const auto& ref : refs) {
        if (m.imagePaths[ref.image].empty()) continue;
        const size_t begin = plainOffset(ref.begin);
        const size_t end = plainOffset(ref.end);
        if (begin < cursor) continue;
        addProse(cursor, begin);
        SentBodySegment image;
        image.image = ref.image;
        body.segments.push_back(std::move(image));
        referenced[ref.image] = true;
        cursor = end;
    }
    addProse(cursor, plain.size());
    for (size_t image = 0; image < referenced.size(); ++image) {
        if (referenced[image] || m.imagePaths[image].empty()) continue;
        SentBodySegment segment;
        segment.image = image;
        body.segments.push_back(std::move(segment));
    }
    return body;
}

// Keep the owning text widget's selection, not just the final prose segment's selection.
// ImGui has one InputTextState; querying each segment immediately preserves its plain-text map.
static std::string drawSentBody(const SentBody& body) {
    std::string selection;
    const float previewW = std::min(ImGui::GetContentRegionAvail().x, ui::metrics::px(480.0f));
    const float previewH = ui::metrics::px(320.0f);
    ImGui::PushID("##sent_segments");
    for (size_t i = 0; i < body.segments.size(); ++i) {
        const SentBodySegment& segment = body.segments[i];
        ImGui::PushID(static_cast<int>(i));
        if (segment.image == std::string::npos) {
            const ImGuiID textId = chatDrawSelectableText("##prose", segment.text,
                                                         chatSurf().textPrimary, &segment.keywords);
            std::string selected = chatInputSelection(textId);
            if (!selected.empty()) selection = std::move(selected);
        } else {
            if (i > 0) ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
            ImGui::TextColored(chatSurf().textTertiary, "#%zu", segment.image + 1);
            if (ui::attachmentImagePreview(body.paths[segment.image], previewW, previewH))
                ui::openImageLightbox(body.paths[segment.image]);
            if (i + 1 < body.segments.size())
                ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
        }
        ImGui::PopID();
    }
    ImGui::PopID();
    return selection;
}
}  // namespace

// The model entities this message carried (composer context picker): one chip per item, same
// painter as the pending chips above the composer. Clicking one shows it in the viewport.
static void drawSentContextChips(AppState& st, const ChatMsg& m) {
    if (m.contextRefs.empty()) return;
    const float gap = ui::metrics::px(4.0f);
    const float maxX = ImGui::GetContentRegionMax().x;
    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    ImGui::PushID("##umsg_ctx");
    for (size_t i = 0; i < m.contextRefs.size(); ++i) {
        const core::ContextRef& ref = m.contextRefs[i];
        const float w = contextRefChipWidth(st, ref, /*removable=*/false);
        if (i > 0) {
            ImGui::SameLine(0.0f, gap);
            if (ImGui::GetCursorPosX() + w > maxX) ImGui::NewLine();
        }
        ImGui::PushID((int)i);
        if (drawContextRefChip(st, ref, /*removable=*/false) == ContextChipAction::Focus)
            focusContextRef(st, ref);
        ImGui::PopID();
    }
    ImGui::PopID();
}

// The user's message: a right-aligned, shrink-to-fit soft bubble tinted by the turn's mode
// (Agent = accent, Plan = amber, Ask = neutral — the role the old outline carried), with no
// author label and no hard outline: alignment and tint already say "you". The age and the
// rewind/edit actions sit in a quiet meta row UNDER the bubble, right-aligned, so the bubble
// holds only what was said.
void drawUserBlock(AppState& st, const ChatMsg& m, const ui::Fonts& fonts,
                          float panelW, int msgIdx) {
    (void)fonts;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const float avail = std::min(panelW, ImGui::GetContentRegionAvail().x);
    const float rowX = ImGui::GetCursorPosX();

    const SentBody& body = sentBody(m);

    // Shrink-to-fit: a one-liner gets a small bubble, long text wraps at ~80% of the column
    // (a little more on the narrow docked column, where 80% would starve the line length).
    const float padX = ui::metrics::cpChatBubblePadX();
    const float padY = ui::metrics::cpChatBubblePadY();
    const float maxW = std::max(avail * (avail < ui::metrics::px(420.0f) ? 0.90f : 0.80f),
                                ui::metrics::px(120.0f));
    const bool hasExtras = !m.imagePaths.empty() || !m.contextRefs.empty();
    // Text-only messages shrink to fit; images/context use the full bubble width.
    float bw = maxW;
    if (!hasExtras) {
        const float textW = body.segments.empty() ? 0.0f
            : chatSoftWrapNaturalWidth(body.segments.front().text) +
              ImGui::GetStyle().FramePadding.x * 2.0f + ui::metrics::px(2.0f);
        bw = std::clamp(textW + padX * 2.0f, ui::metrics::px(48.0f), maxW);
    }
    ImGui::SetCursorPosX(rowX + avail - bw);

    GlassBubble gb = beginGlassBubble(bw, ui::GlassLevel::Regular,
                                      ui::metrics::cpChatBubbleRadius());
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(padX, padY));
    ImGui::BeginChild("##umsg", ImVec2(bw, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize |
                          ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(ImGui::GetStyle().ItemSpacing.x, 0.0f));
    // Selectable prose and clickable image previews follow the reference order in the body.
    const std::string selection = drawSentBody(body);
    // Model context the message carried: chips (click = show it in the model).
    drawSentContextChips(st, m);
    ImGui::PopStyleVar();   // ItemSpacing -- pushed after BeginChild, must pop before EndChild
    // Hover-copy glyph MUST be submitted while "##umsg" is still the current window: ImGui's
    // hovered-window resolution grants overlapping screen space to the (deeper) child window,
    // so an InvisibleButton added to the PARENT after EndChild silently loses every click here
    // to the InputTextMultiline underneath — the copy button drew fine but never fired.
    {
        const ImVec2 wp = ImGui::GetWindowPos();
        const ImVec2 ws = ImGui::GetWindowSize();
        chatHoverCopyGlyph(ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)), m.text);
    }
    ImGui::EndChild();
    ImGui::PopStyleVar();   // ##umsg WindowPadding -- pushed before BeginChild, pops after
    float tint[3];
    modeAccentRgb(m.copilotMode, tint);
    // A soft pill: the tint carries "you", so the same-hue edge stays a whisper, not an outline.
    const ImRect bubble = endTintBubble(gb, tint, surf.darkText ? 0.16f : 0.24f,
                                        surf.darkText ? 0.16f : 0.18f);
    chatCopyContext(bubble, "##umsg_copy", m.text, m.text, selection);

    // ---- meta row under the bubble, right-aligned: [rewind] [edit]  age ----
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
    const float btnSz = ImGui::GetFrameHeight() * 0.72f;
    const float gap = 4.0f * sc;
    const float lineH = ImGui::GetTextLineHeight();
    const float rowH = showBtns ? std::max(lineH, btnSz) : lineH;
    const std::string age = chatStampLabel(m.ts);
    const float ageW = age.empty() ? 0.0f : ImGui::CalcTextSize(age.c_str()).x;
    // A message sent mid-run says whether the agent has read it: always visible (not hover-
    // gated), since "queued behind a long step" otherwise looks exactly like a frozen app.
    const char* steerLabel = nullptr;
    const char* steerTip = nullptr;
    ImVec4 steerInk = surf.textTertiary;
    switch (m.steerState) {
    case STEER_SENDING:
        steerLabel = "Sending\xe2\x80\xa6";
        steerTip = "Handing your message to the running agent.";
        break;
    case STEER_QUEUED:
        steerLabel = "Queued \xc2\xb7 after this step";
        steerTip = "The agent reads it when its current tool call returns. A wait on a job "
                   "ends at once; real work (a solve, a build) finishes first. Stop withdraws "
                   "it unread and sends it with your next message.";
        steerInk = semanticOnGlass(ui::palette.statusWarn, surf);
        break;
    case STEER_DELIVERED:
        steerLabel = "Read by agent";
        steerTip = "The agent has this message in its context.";
        steerInk = semanticOnGlass(ui::palette.statusOk, surf);
        break;
    case STEER_CARRIED:
        steerLabel = "Unread \xc2\xb7 sends next time";
        steerTip = "You stopped the run before the agent read this message. It is sent ahead "
                   "of the next message you send in this chat.";
        steerInk = semanticOnGlass(ui::palette.statusWarn, surf);
        break;
    case STEER_FAILED:
        steerLabel = "Not delivered \xc2\xb7 resend";
        steerTip = "The run ended before the agent read this message.";
        steerInk = semanticOnGlass(ui::palette.statusErr, surf);
        break;
    default:
        break;
    }
    const float steerW = steerLabel ? ImGui::CalcTextSize(steerLabel).x : 0.0f;
    const float rowW = (steerLabel ? steerW + gap * 2.0f : 0.0f) +
                       (showBtns ? btnSz * 2.0f + gap : 0.0f) +
                       (showBtns && !age.empty() ? gap * 2.0f : 0.0f) + ageW;
    if (rowW <= 0.0f) {
        ImGui::Dummy(ImVec2(0, ui::metrics::cpChatMsgGap()));
        return;
    }
    // Anchored to the bubble's bottom edge (not the layout cursor, which already carries the
    // item spacing) so the row reads as the bubble's caption; never left of the column, where
    // a delivery caption on the narrow docked chat would otherwise be clipped.
    const ImVec2 rowMin(std::max(bubble.Max.x - ui::metrics::px(4.0f) - rowW,
                                 ImGui::GetWindowPos().x + rowX),
                        bubble.Max.y + ui::metrics::cpChatMetaGap());
    ImDrawList* dl = ImGui::GetWindowDrawList();
    // The row is quiet until the message is hovered (or a rewind is waiting on its confirm).
    const bool rowHot = pending || ImGui::IsMouseHoveringRect(
        bubble.Min, ImVec2(bubble.Max.x, rowMin.y + rowH), /*clip=*/false);
    const float rowA = ui::anim::toward(ImGui::GetID("##umsg_meta_hot"), rowHot ? 1.0f : 0.0f,
                                        ui::anim::kFast);
    const ImVec4& inkIdle = surf.textTertiary;
    const ImVec4& inkHot = surf.textSecondary;
    const ImVec4 ink(inkIdle.x + (inkHot.x - inkIdle.x) * rowA,
                     inkIdle.y + (inkHot.y - inkIdle.y) * rowA,
                     inkIdle.z + (inkHot.z - inkIdle.z) * rowA,
                     inkIdle.w * (0.70f + 0.30f * rowA));
    float x = rowMin.x;
    if (steerLabel) {
        ImGui::SetCursorScreenPos(ImVec2(x, rowMin.y + (rowH - lineH) * 0.5f));
        ImGui::PushStyleColor(ImGuiCol_Text, steerInk);
        ImGui::TextUnformatted(steerLabel);
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", steerTip);
        x += steerW + gap * 2.0f;
    }
    if (showBtns) {
        // Rewind glyph button
        ImGui::SetCursorScreenPos(ImVec2(x, rowMin.y + (rowH - btnSz) * 0.5f));
        {
            const bool clicked = ImGui::InvisibleButton("##umsg_rw", ImVec2(btnSz, btnSz));
            const ImVec2 bmn = ImGui::GetItemRectMin(), bmx = ImGui::GetItemRectMax();
            if (pending || ImGui::IsItemHovered())
                dl->AddRectFilled(bmn, bmx,
                    ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textPrimary.x, surf.textPrimary.y,
                                                          surf.textPrimary.z, pending ? 0.18f : 0.10f)),
                    btnSz * 0.5f);   // round hover pad, like every other icon affordance
            // Inset to match the pencil icon's 0.18 pad -- the raw glyph otherwise reads
            // noticeably bigger/bolder than the pencil.
            const float rp = btnSz * 0.18f;
            drawDoubleRewindGlyph(dl, ImVec2(bmn.x + rp, bmn.y + rp),
                                  ImVec2(bmx.x - rp, bmx.y - rp),
                                  ImGui::ColorConvertFloat4ToU32(pending ? surf.textPrimary : ink));
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
        x += btnSz + gap;
        // Pencil edit — atlas icon (msg_edit) with filled-glyph fallback.
        ImGui::SetCursorScreenPos(ImVec2(x, rowMin.y + (rowH - btnSz) * 0.5f));
        {
            const bool clicked = ImGui::InvisibleButton("##umsg_edit", ImVec2(btnSz, btnSz));
            const ImVec2 bmn = ImGui::GetItemRectMin(), bmx = ImGui::GetItemRectMax();
            if (ImGui::IsItemHovered())
                dl->AddRectFilled(bmn, bmx,
                    ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textPrimary.x, surf.textPrimary.y,
                                                          surf.textPrimary.z, 0.10f)),
                    btnSz * 0.5f);
            const ImU32 inkU = ImGui::ColorConvertFloat4ToU32(ink);
            IconUV editIcon = ui::getIcon("msg_edit", 20);
            if (editIcon.valid) {
                const float pad = btnSz * 0.18f;
                dl->AddImage(editIcon.texId,
                             ImVec2(bmn.x + pad, bmn.y + pad),
                             ImVec2(bmx.x - pad, bmx.y - pad),
                             editIcon.uv0, editIcon.uv1,
                             inkU);
            } else {
                drawPencilGlyph(dl, bmn, bmx, inkU);
            }
            if (clicked) beginEditStage(st, msgIdx);
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("%s",
                    editStageInFlight()
                        ? "An edit is being applied \xe2\x80\x94 wait for it to finish."
                    : activeConversationRunning(st)
                        ? "Stop the run and edit this message \xe2\x80\x94 Esc cancels the "
                          "edit without changing the part."
                        : "Edit this message \xe2\x80\x94 Esc cancels without changing the part.");
        }
        x += btnSz + gap * 2.0f;
    }
    // Timestamp (hover = relative age + full date).
    if (!age.empty()) {
        ImGui::SetCursorScreenPos(ImVec2(x, rowMin.y + (rowH - lineH) * 0.5f));
        ImGui::PushStyleColor(ImGuiCol_Text, ink);
        ImGui::TextUnformatted(age.c_str());
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) {
            const std::string tip = chatTimeTooltip(m.ts);
            if (!tip.empty()) ImGui::SetTooltip("%s", tip.c_str());
        }
    }
    // Leave the cursor at the row's left edge, one row down, whatever the row held.
    ImGui::SetCursorPos(ImVec2(rowX, rowMin.y + rowH - ImGui::GetWindowPos().y +
                                         ImGui::GetScrollY()));
    ImGui::Dummy(ImVec2(1.0f, ui::metrics::cpChatMsgGap()));   // air before the next message
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

    const ui::AdaptiveSurface& surf = chatSurf();
    const auto& pal = ui::palette;

    ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    {
        char hdr[64];
        std::snprintf(hdr, sizeof(hdr), "Provider log (%d)", (int)calls.size());
        // Vector chevron — the ▼/▶ text glyphs are missing from the UI font and render as a
        // literal "?". The SAME disclosure the timeline rows use, rotating as it opens.
        const float box = ImGui::GetTextLineHeight();
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImGui::Dummy(ImVec2(box * 0.8f, box));
        const float openT = ui::anim::toward(ImGui::GetID("##provlog_chev"), open ? 1.0f : 0.0f,
                                             ui::anim::kFast);
        chatPaintDisclosure(ImGui::GetWindowDrawList(), ImVec2(p0.x + box * 0.4f, p0.y + box * 0.5f),
                            box * 0.38f, openT, ImGui::ColorConvertFloat4ToU32(surf.textTertiary));
        ImGui::SameLine(0, ui::metrics::U(0.5f));
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
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize |
                          ImGuiChildFlags_AlwaysUseWindowPadding);
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

static void drawAssistantTurn(AppState& st, ChatMsg& m, const ui::Fonts& fonts,
                              int msgIdx = -1, int turnNum = 0, bool isLastTurn = false) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& turnSurf = chatSurf();
    const float sc = ui::uiScale;

    // The assistant reply is the largest patch of text the copilot did NOT author in C++:
    // raw model prose, where an arrow renders as "?" (the atlas stops at U+2027) and a doc
    // id the model quotes back reads as "doc-cpp00000039" instead of the part's name. Runs
    // through the SAME gated sanitizer drawAgentTimeline applies to the prose rows, so the
    // two stay byte-identical and timelineProseTail can tell them apart. Idempotent with a
    // no-op fast path, and m.text is mutated in place, so the cost is paid once per message
    // rather than per frame — the persisted transcript and Copy carry the cleaned text too.
    ui::sanitizeAgentTextInPlace(st, m.text);

    // Author label. Each card that CAN host it (prose / partial / error) draws the
    // model+age INSIDE itself as the top row (first card wins via metaDrawn).
    // Activity-only turns and VC-restore turns keep the tight label row above the
    // turn content with the age appended inline.
    // Prefer the model stamped on the turn at run start; only fall back to the
    // live picker for an in-flight turn that somehow has no stamp. No model →
    // omit label (never bare "Assistant").
    //
    // A turn whose prose streamed has it drawn INSIDE the timeline, at the point the model
    // said it — so there is no trailing reply card to host the label, and it goes above the
    // turn like every other activity-led turn. The trailing card survives only for a reply
    // the timeline never saw (a non-streaming provider, a restored transcript, an RPC answer).
    const bool proseInline = ui::timelineHasInlineProse(m);
    // A partial outcome (mission mixed results, or a run interrupted mid-reply) shows its reply
    // INSIDE the amber "Partially complete" card below; drawing the open prose too printed the
    // same text twice.
    const bool partialCard = m.errKind == ERR_PARTIAL || m.resultPartial;
    const bool proseCard = !proseInline && !m.text.empty() && m.errKind == ERR_NONE &&
                           !partialCard;
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
        (proseCard || m.errKind != ERR_NONE || partialCard);
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
            // Inline age (+ prompt-cache total) trails the label on this same line
            // (in-card rows right-align it via drawMsgMetaRow), so the name only gets the
            // rest of the row: unbounded, a long model id ran off the column's right edge.
            const std::string age = chatStampCacheLabel(m.ts, m.cacheReadTok);
            const float ageW = age.empty() ? 0.0f
                                           : ImGui::CalcTextSize(age.c_str()).x + 6 * sc;
            ImGui::PushFont(fonts.medium);
            const std::string fit = chatEllipsize(
                modelLabel, std::max(ImGui::GetContentRegionAvail().x - ageW, 24.0f * sc));
            ImGui::TextColored(turnSurf.textSecondary, "%s", fit.c_str());
            ImGui::PopFont();
            if (fit != modelLabel && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", mid);
            if (!age.empty()) {
                ImGui::SameLine(0, 6 * sc);
                ImGui::PushStyleColor(ImGuiCol_Text, turnSurf.textTertiary);
                ImGui::TextUnformatted(age.c_str());
                ImGui::PopStyleColor();
                if (ImGui::IsItemHovered()) {
                    const std::string tip = chatAgeCacheTip(m.ts, m.cacheReadTok,
                                                            m.cacheReadTotTok, m.cacheReadCalls);
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
    // Prefer planState; a PlanInfo-only turn (feed.cpp kind:plan) normalizes on the fly when
    // planState was never filled. When a rowKind-18 marker exists in the
    // activity timeline the section renders THERE (arrival position) instead — drawing it
    // here too would double the card and re-introduce the out-of-order top-pin bug.
    bool planInTimeline = false;
    for (const auto& a : m.activity)
        if (a.rowKind == 18) { planInTimeline = true; break; }
    if (!planInTimeline && m.planState.present) {
        drawPlanSection(m.planState, m.activity, fonts);
    } else if (!planInTimeline && m.plan.present) {
        // Lazy normalize so a PlanInfo-only turn still lights the section.
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

    // (2) Agent timeline — typed blocks in arrival order: thinking, tool cards, and the
    // model's prose exactly where it was spoken.
    drawAgentTimeline(st, m, fonts, gShowThinking, isLastTurn);

    // What the timeline did NOT already show. For a streamed turn this is normally empty (or
    // a note appended after the fact, e.g. "(Stopped by you.)"); for a non-streaming provider
    // or a restored transcript it is the whole reply. Every card below renders THIS, never
    // m.text, or a streamed reply prints twice.
    const std::string bodyText = proseInline ? ui::timelineProseTail(m) : m.text;

    // (3) The reply as open prose — no card, no rim, flowing on the pane's glass at the
    // timeline's text column, exactly like the timeline's inline prose. Every other row kind
    // (stage cards, thoughts, tool lines) is a rail or a bare line, so the answer still reads as
    // the deliverable without being boxed. Selectable for click-drag + Ctrl+C; right-click
    // copies selection or full text.
    if (proseCard ||
        (proseInline && !bodyText.empty() && m.errKind == ERR_NONE && !partialCard)) {
        const std::string prose = stripMdMarks(bodyText);
        GlassBubble gb = beginOpenBubble(ImGui::GetContentRegionAvail().x);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(chatProseInsetX(), ui::metrics::cpChatCardPadY()));
        ImGui::BeginChild("##prose", ImVec2(0, 0),
                          ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize |
                              ImGuiChildFlags_AlwaysUseWindowPadding);
        // Author meta row INSIDE the card top (model left, age right) when it
        // wasn't drawn above the turn (VC-restore turns keep the top row).
        if (metaInCard && !metaDrawn) {
            drawMsgMetaRow(fonts, modelLabel.empty() ? nullptr : modelLabel.c_str(),
                           turnSurf.textSecondary, m.ts, m.cacheReadTok,
                           m.cacheReadTotTok, m.cacheReadCalls, mid);
            metaDrawn = true;
            ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
        }
        // Raw bodyText (not `prose`): the renderer needs the ``` fences intact to route code
        // spans into the mono box. `prose` stays the copy payload, as before.
        const ImGuiID textId = drawMdProse("##prose_sel", bodyText, chatSurf().textPrimary,
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
        ImRect proseRect = endOpenBubble(gb);
        const std::string sel = chatInputSelection(textId);
        chatCopyContext(proseRect, "##prose_copy", prose, prose, sel);
        ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
    }


    // (4) Pre-flight clarify gate: the model's build-critical questions, awaiting the user.
    if (m.clarify.active) drawClarifyCard(st, m, fonts);
    // (4a) The online agent's ask_user questions — interactive while pending, a summary after.
    if (!m.asks.empty()) drawAskCards(st, m, fonts);
    // (4b) Plan-mode / mission plan_ready approve gate (Execute as Agent/Swarm/Eng.Firm).
    if (m.planApprovePending && !m.running) drawPlanApproveCard(st, m, fonts);

    // (6) While running the live activity stream (timeline thoughts/tools/output) is the
    // primary content — no fake "Saying · working..." bar. The liveStatus line appears only when
    // the timeline is still empty, when the run is parked on the user's answers to an ask_user
    // card, or when the run has gone QUIET: nothing streaming or running and no event for a few
    // seconds. A provider stalled for 25 min (c245: two restarted GPT responses at 248k
    // context) otherwise left a finished row on screen that read as a frozen app.
    if (m.running) {
        constexpr double kQuietWaitS = 5.0;
        bool hasLiveContent = false;
        bool rowInProgress = false;
        for (const auto& a : m.activity) {
            if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
            hasLiveContent = true;
            if (a.streaming || a.status == "running") rowInProgress = true;
        }
        if (!hasLiveContent && !m.text.empty()) hasLiveContent = true;
        const bool waitingOnUser =
            std::any_of(m.asks.begin(), m.asks.end(),
                        [](const AskBatch& b) { return b.status == AskStatus::Pending; });
        const double now = chatNowEpoch();
        const bool quiet = hasLiveContent && !rowInProgress && m.lastEventTs > 0.0 &&
                           now - m.lastEventTs >= kQuietWaitS;
        if (!hasLiveContent || waitingOnUser || quiet) {
            ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
            const ui::AdaptiveSurface& surf = chatSurf();
            std::string say = waitingOnUser ? std::string("Waiting for your answers")
                                            : m.liveStatus;
            while (!say.empty() && (say.back() == '.' || say.back() == ' ')) say.pop_back();
            if (say.size() >= 3 && say.compare(say.size() - 3, 3, "\xe2\x80\xa6") == 0)
                say.resize(say.size() - 3);
            if (say.empty() || say == "Working" || say == "working") say = "Working";
            // The silent window is the one place with NO streamed content, so a static label
            // makes a slow provider and a hung app look identical: the label carries a light
            // sweep while the agent works and, once the wait is long enough to worry about, an
            // elapsed readout — since the turn began, or since the last event once the timeline
            // has content. Waiting on the USER is not work — that line stays still.
            const double since = quiet ? m.lastEventTs : m.ts;
            const double elapsed = (since > 0.0) ? (now - since) : 0.0;
            if (!waitingOnUser) say += "\xe2\x80\xa6";
            if (elapsed >= 3.0) say += "  \xc2\xb7  " + ui::kit::fmtDuration(elapsed);
            ImGui::Indent(chatProseInsetX());
            say = chatEllipsize(say, ImGui::GetContentRegionAvail().x);
            if (waitingOnUser) {
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                ImGui::TextUnformatted(say.c_str());
                ImGui::PopStyleColor();
            } else {
                chatShimmerText(say.c_str(), surf.textTertiary, surf.textPrimary,
                                ui::anim::kBreathe);
            }
            ImGui::Unindent(chatProseInsetX());
        }
    } else {
        // (7) The run receipt — the finished turn's footer. It carries the usage numbers this slot
        // used to print as bare text, and expands to what went wrong, what was written to disk and
        // where the tokens went. Anchors inside it resolve against the message id scope the
        // per-message PushID has already established.
        drawTurnReceipt(st, m);
    }

    // (8) Result / error block.
    // ERR_PARTIAL: warning-accent "Partially complete" card (mission mixed outcomes) —
    // geometry is kept; NOT a hard error. ERR_CAD/ERR_SYSTEM: red-bordered error card.
    if (partialCard) {
        // An empty tail (the timeline already drew the reply inline) leaves just the headline:
        // a "Partially complete" body under a "Partially complete" title read as a stutter.
        const std::string body = stripMdMarks(bodyText);
        const std::string copyText = body.empty() ? std::string("Partially complete") : body;
        ImGui::Dummy(ImVec2(0, ui::metrics::cpChatRowGap()));
        GlassBubble gb = beginGlassBubble(ImGui::GetContentRegionAvail().x, ui::GlassLevel::Thin,
                                          ui::metrics::cpChatBubbleRadius() * 0.7f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
        ImGui::BeginChild("##partialcard", ImVec2(0, 0),
                          ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize |
                              ImGuiChildFlags_AlwaysUseWindowPadding);
        const ImVec4 warnCol = semanticOnGlass(pal.statusWarn, chatSurf());
        if (metaInCard && !metaDrawn) {
            drawMsgMetaRow(fonts, modelLabel.empty() ? nullptr : modelLabel.c_str(),
                           turnSurf.textSecondary, m.ts, m.cacheReadTok,
                           m.cacheReadTotTok, m.cacheReadCalls, mid);
            metaDrawn = true;
            ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
        }
        ImGui::PushFont(fonts.medium);
        ImGui::TextColored(warnCol, "Partially complete");
        ImGui::PopFont();
        // Body is the selectable detail (counts + first failure / verdict).
        ImGuiID textId = 0;
        if (!body.empty())
            textId = chatDrawSelectableText("##partial_sel", body, chatSurf().textPrimary);
        // Submitted before EndChild — see the comment on the user-bubble copy glyph in
        // drawUserBlock() for why an after-EndChild button silently eats no clicks here.
        {
            const ImVec2 wp = ImGui::GetWindowPos();
            const ImVec2 ws = ImGui::GetWindowSize();
            chatHoverCopyGlyph(ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)), copyText);
        }
        restoreBar();
        ImGui::EndChild();
        ImGui::PopStyleVar();   // ##partialcard WindowPadding
        // A partial outcome is a soft amber card (wash + same-hue hairline), not glass with a
        // heavy border: it informs, it does not alarm.
        ImRect cardRect = endTintBubble(gb, pal.statusWarn, 0.08f, 0.40f);
        const std::string sel = chatInputSelection(textId);
        chatCopyContext(cardRect, "##partial_copy", copyText, copyText, sel);
    } else if (m.errKind != ERR_NONE) {
        std::string stopReason, errorDetail;
        for (const auto& a : m.activity) {
            if (a.stage != "__stop_meta__") continue;
            stopReason = a.title;
            errorDetail = !a.detail.empty() ? a.detail : a.reason;
            break;
        }
        // Nothing to show if the turn has no un-shown prose, no stop fields and no
        // humanized line. (Prose the timeline already drew inline is not repeated here.)
        if (!(bodyText.empty() && stopReason.empty() && errorDetail.empty() && m.errorHuman.empty())) {
        // Also surface failed activity reasons (phase errors) that aren't already folded into
        // the meta row — keeps "budget exhausted (1)" from being the only line. Only failures
        // the turn never got past: a row that fails and is followed by later successful work
        // was recovered from and is not why the turn stopped (a `read local://PLAN.md`
        // miss 14 minutes before a timeout was painted as the cause, 2026-09-24). Tool rows
        // carry a packed detail (args + \x1eERR\x1e + \x1eMETA\x1e trailers): show only its
        // error text, never the raw marker bytes.
        std::string phaseErr;
        std::vector<std::string> unrecovered;
        for (const auto& a : m.activity) {
            if (a.stage == "__stop_meta__" || a.rowKind == 9) continue;
            if (a.status == "done") {
                unrecovered.clear();
                continue;
            }
            if (a.status != "fail" && a.status != "error") continue;
            std::string bit;
            if (!a.reason.empty()) bit = a.reason;
            else if (a.stage.rfind("tool:", 0) == 0) {
                const std::string err = splitToolRowDetail(a.detail).err;
                if (!err.empty()) bit = a.title.empty() ? err : (a.title + ": " + err);
            } else if (!a.detail.empty()) bit = a.detail;
            if (bit.empty()) bit = a.title;
            if (bit.empty()) continue;
            if (errorDetail.find(bit) != std::string::npos) continue;
            if (m.text.find(bit) != std::string::npos) continue;
            unrecovered.push_back(std::move(bit));
        }
        for (const auto& bit : unrecovered) {
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
            const std::string plain = stripMdMarks(bodyText);
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
            body = stripMdMarks(bodyText);
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
                                              ui::metrics::cpChatBubbleRadius() * 0.7f);
            ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                                ImVec2(ui::metrics::cpChatCardPadX(), ui::metrics::cpChatCardPadY()));
            ImGui::BeginChild("##errcard", ImVec2(0, 0),
                              ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize |
                                  ImGuiChildFlags_AlwaysUseWindowPadding);
            // Error tone retuned for the frost so the reason stays readable over any model.
            const float* ec = (m.errKind == ERR_SYSTEM) ? pal.errSystem : pal.errCad;
            const ImVec4 errCol = semanticOnGlass(ec, chatSurf());
            if (metaInCard && !metaDrawn) {
                drawMsgMetaRow(fonts, modelLabel.empty() ? nullptr : modelLabel.c_str(),
                               turnSurf.textSecondary, m.ts, 0, 0, 0, mid);
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
            // Same soft-card treatment as the partial outcome, in the error role.
            ImRect cardRect = endTintBubble(gb, pal.statusErr, 0.08f, 0.45f);
            const std::string sel = chatInputSelection(textId);
            chatCopyContext(cardRect, "##err_copy", fullErr, fullErr, sel);
        }
        } // has something to show
    }

    // (8b) Fallback: an error/partial turn whose card ended up with no body renders
    // no card at all — don't lose the model label; draw it here (above-style, inline age).
    if (metaInCard && !metaDrawn && haveModel) {
        // Same bound as the above-card label: the inline age owns the row's tail, the
        // name gets the rest, and the full id stays on hover when it is clipped.
        const std::string age = chatStampLabel(m.ts);
        const float ageW = age.empty() ? 0.0f : ImGui::CalcTextSize(age.c_str()).x + 6 * sc;
        ImGui::PushFont(fonts.medium);
        const std::string fit = chatEllipsize(
            modelLabel, std::max(ImGui::GetContentRegionAvail().x - ageW, 24.0f * sc));
        ImGui::TextColored(turnSurf.textSecondary, "%s", fit.c_str());
        ImGui::PopFont();
        if (fit != modelLabel && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", mid);
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


// Empty state for a brand-new/blank session — the ONE empty state for st.chat, shared by the
// docked Copilot and the AI workspace (copilot.h), so a blank session reads the same in both.
// Without it the composer's grey placeholder is the only affordance and the log reads as a
// broken blank panel. Forwards to the app-wide kit painter.
void chatDrawEmptyState() {
    ui::kit::emptyState("ai_panel", "Start a new session",
                        "Ask about this model, or describe what to build \xe2\x80\x94 the agent "
                        "plans, models and verifies it.");
}

// The outer message spacers must not hide a timeline's layout invalidation. Width alone is
// insufficient when font scaling or the tool detail/grouping/render preferences change while
// an older turn is offscreen. Keep this render-only state bounded and separate for each surface.
struct ChatLogLayout {
    ImGuiStyle style{};
    ImFont* font = nullptr;
    ImFont* mono = nullptr;
    ImFont* medium = nullptr;
    float size = -1.0f, monoSize = -1.0f, mediumSize = -1.0f;
    int detail = -1;
    bool thinking = false, renders = false, grouping = false, humanize = false;
    std::string conversation;
};

static void invalidateChatLogLayout(AppState& st) {
    static std::unordered_map<ImGuiID, ChatLogLayout> layouts;
    const ImGuiID id = ImGui::GetID("##chat_log_layout");
    if (layouts.find(id) == layouts.end() && layouts.size() >= 8) layouts.clear();
    ChatLogLayout& old = layouts[id];
    ChatLogLayout now;
    now.style = ImGui::GetStyle();
    now.font = ImGui::GetFont();
    now.mono = st.fonts.mono;
    now.medium = st.fonts.medium;
    now.size = ImGui::GetFontSize();
    now.monoSize = now.mono ? now.mono->FontSize * now.mono->Scale : 0.0f;
    now.mediumSize = now.medium ? now.medium->FontSize * now.medium->Scale : 0.0f;
    now.detail = ui::chatToolDetail();
    now.thinking = gShowThinking;
    now.renders = ui::chatShowRenders();
    now.grouping = ui::chatGroupToolCalls();
    now.humanize = ui::chatHumanizeDocIds();
    now.conversation = st.activeConvId();
    if (old.font != now.font || old.mono != now.mono || old.medium != now.medium ||
        old.size != now.size || old.monoSize != now.monoSize || old.mediumSize != now.mediumSize ||
        old.detail != now.detail || old.thinking != now.thinking || old.renders != now.renders ||
        old.grouping != now.grouping || old.humanize != now.humanize ||
        old.conversation != now.conversation ||
        std::memcmp(&old.style, &now.style, sizeof(ImGuiStyle)) != 0) {
        for (auto& message : st.chat) message.uiCachedH = -1.0f;
        old = std::move(now);
    }
}

void drawChatLogWindowed(AppState& st) {
    ui::Fonts& fonts = st.fonts;
    invalidateChatLogLayout(st);
    drawAiVcRollbackConfirm(st);   // pre-rollback confirmation modal (warning ladder)
    const float logW    = ImGui::GetContentRegionAvail().x;
    // A staged edit HIDES the tail it would replace — the messages stay in st.chat (and on
    // disk) untouched until the edit commits; the hide is never applied to a running tail.
    const int hideFrom = editStageHideFrom(st);
    const size_t shown = hideFrom >= 0 && hideFrom < (int)st.chat.size() ? (size_t)hideFrom
                                                                         : st.chat.size();
    if (shown == 0) { chatDrawEmptyState(); return; }
    const float viewTop = ImGui::GetScrollY();
    const float viewBot = viewTop + ImGui::GetWindowSize().y;
    const float slack   = ImGui::GetWindowSize().y;   // one extra viewport of buffer each side
    // 1-based index of finished assistant turns that mutated the doc — matches AI-VC turn labels.
    int mutTurn = 0;
    // The LATEST assistant turn is the state the user is already at — offering a restore
    // there is a no-op button. Find it once so drawAssistantTurn can suppress its bar.
    int lastAssistantIdx = -1;
    for (size_t k = 0; k < shown; ++k)
        if (!st.chat[k].fromUser) lastAssistantIdx = (int)k;
    for (size_t mi = 0; mi < shown; ++mi) {
        ChatMsg& m = st.chat[mi];
        ImGui::PushID((int)mi);
        const float y = ImGui::GetCursorPosY();
        const bool cacheValid = m.uiCachedH > 0.0f && std::fabs(m.uiCachedW - logW) < 0.5f;
        // Auto-height bubbles must continue measuring even when their previous height clips the
        // selectable text. Otherwise a newly hosted/restored row caches only padding and remains
        // an empty bar. The bubble children use AlwaysAutoResize; the outer log still windows
        // genuinely off-screen, already-measured turns here.
        const bool interactive = m.running || m.clarify.active || m.clarify.awaiting ||
                                 std::any_of(m.asks.begin(), m.asks.end(),
                                             [](const AskBatch& b) { return askStatusOpen(b.status); }) ||
                                 (s_aiVc.confirmMsgIdx == (int)mi);
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
        // Entrance: a message that JUST arrived (its send/receive stamp is younger than kSlow)
        // fades in, so a new bubble eases into the log instead of popping. Alpha only — no
        // offset, so the layout never jumps and the render-windowing heights stay exact.
        // Restored history carries old stamps and simply appears.
        const double age = m.ts > 0.0 ? chatNowEpoch() - m.ts : 1e9;
        float enter = 1.0f;
        if (age >= 0.0 && age < (double)ui::anim::kSlow) {
            enter = ui::anim::easeOut((float)(age / (double)ui::anim::kSlow));
            ui::anim::requestFrames();
        }
        ImDrawList* const msgDl = ImGui::GetWindowDrawList();
        const int vtx0 = msgDl->VtxBuffer.Size;
        if (enter < 1.0f)
            ImGui::PushStyleVar(ImGuiStyleVar_Alpha, ImGui::GetStyle().Alpha * enter);
        ImGui::BeginGroup();
        if (m.fromUser) {
            drawUserBlock(st, m, fonts, logW, (int)mi);
        } else {
            drawAssistantTurn(st, m, fonts, (int)mi, thisTurn, (int)mi == lastAssistantIdx);
            ImGui::Dummy(ImVec2(0, ui::metrics::cpChatMsgGap()));   // air before the next message
        }
        ImGui::EndGroup();
        if (enter < 1.0f) {
            ImGui::PopStyleVar();
            // Fills painted straight into this window's list (bubble washes, rails) bypass the
            // style alpha; fade the vertices this message added so the whole bubble enters
            // together. Child windows' content already follows the pushed alpha.
            for (int vi = vtx0; vi < msgDl->VtxBuffer.Size; ++vi) {
                ImU32& c = msgDl->VtxBuffer.Data[vi].col;
                const ImU32 a = (ImU32)((float)((c >> IM_COL32_A_SHIFT) & 0xFF) * enter);
                c = (c & ~IM_COL32_A_MASK) | (a << IM_COL32_A_SHIFT);
            }
        }
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
    // ROOT CAUSE of the "autoscroll stops keeping up during a live turn" report: ImGui only
    // finalizes window->ContentSize (and thus ScrollMax) at End()/next Begin(), so GetScrollMaxY()
    // here still reports LAST frame's height — one full frame behind whatever the streaming
    // message just grew to. SetScrollY(smax) therefore chased a permanently-stale target and the
    // view trailed the true bottom by one frame's growth on every single frame of a stream.
    // SetScrollHereY(1.0f) instead reads window->DC.CursorPosPrevLine, i.e. the position the log
    // loop's last item ACTUALLY finished at this frame (we run right after drawChatLogWindowed /
    // chatDrawLog, still inside the scrolling child) — so the target always matches the current
    // frame's real content height, card expand/collapse included, with no lag to chase.
    if (s.stick)
        ImGui::SetScrollHereY(1.0f);
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

float chatJumpToLatestFooterHeight() {
    // Keep the gutter even while pinned: showing the action must not resize the viewport and
    // turn a layout-induced scroll adjustment into a user scroll-up.
    return ImGui::GetFrameHeightWithSpacing();
}

void chatDrawJumpToLatest(const char* regionId) {
    const float height = ImGui::GetFrameHeight();
    if (chatLogAtBottom(regionId)) {
        ImGui::Dummy(ImVec2(0, height));
        return;
    }
    // This is a normal parent item AFTER EndChild, entirely outside the log's hit-test rectangle.
    // A full-width action also stays reachable in the minimum-width Copilot column.
    ImGui::PushID(regionId ? regionId : "chat");
    if (ui::kit::secondaryButton("Jump to latest###chat_jump_latest", true, nullptr,
                                ImVec2(std::max(ImGui::GetContentRegionAvail().x,
                                               ui::metrics::stroke(1.0f)), height)))
        chatLogJumpToBottom(regionId);
    ImGui::PopID();
}

// Re-engage stick-to-bottom on every chat surface (conversation switch). Dropping the entries
// also clears prevScrollY: the incoming log is usually a different height, and ImGui clamping
// the carried-over offset down would otherwise look exactly like a user scroll-up and unstick
// the very first frame of the new conversation.
void chatLogResetStick() { s_stickByRegion.clear(); }

// The inverse, for a deferred scroll-to-anchor (run receipt / cost strip). The jump lands one
// frame AFTER the click, and chatLogStickToBottom runs after the log every frame — so without
// un-sticking first, the snap-to-tail would immediately undo the jump and the click would look
// like it did nothing. Every region is broken, not just the active one: the docked column and
// the AI tab render the same conversation, and the jump has to survive on whichever is visible.
// atBottom is cleared too so the "jump to latest" affordance appears, which is now accurate.
void chatLogBreakStick() {
    for (auto& kv : s_stickByRegion) {
        kv.second.stick = false;
        kv.second.atBottom = false;
    }
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
