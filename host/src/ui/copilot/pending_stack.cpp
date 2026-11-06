// The pinned stack under the live turn: messages that are not part of the transcript yet.
//
//   * Pending steers  - sent to the running agent, which reads them at its next step. Their
//     bubbles trail st.chat (isPendingSteer) and the chat log stops its windowed rows before
//     them; placeSteerInTranscript moves one into the flow at the point the agent read it.
//     Read-only here: only Stop withdraws a steer (the whole run), so there is nothing to cancel.
//   * Queued messages - held by the composer until the run is free (st.queuedPrompts, with their
//     attachment drafts). Each can be edited in place, sent now, or cancelled back into the
//     composer.
//
// Drawn by drawChatLogWindowed inside the chat's scrolling child, after the last message, so the
// stick-to-bottom autoscroll keeps the stack in view and the per-message height cache never has
// to account for it. Everything is painted straight into the window draw list from measured
// geometry (no child windows), which is what lets a new bubble fade/slide in and a sent one fade
// and collapse out without the layout ever depending on the animation phase.
#include "copilot_internal.h"

namespace ui {
namespace {

constexpr int    kMaxBubbleLines = 6;      // longer messages are clipped; Edit shows all of it
constexpr size_t kMaxShownBytes  = 1200;   // measured/painted prefix of a very long message

enum class Act { None, Edit, SendNow, Cancel };
enum class Tone { Quiet, Primary };

// One bubble of the stack, snapshotted at the top of the frame so a mutation made by one
// bubble's action never invalidates what the rest of the frame draws.
struct StackItem {
    bool steer = false;       // pending steer (read-only) vs queued message (actions)
    size_t index = 0;         // st.chat index (steer) / st.queuedPrompts index (queued)
    ImGuiID id = 0;           // identity for the motion state
    std::string text;
    int attach = 0;           // attachments + inline images riding with it
    int context = 0;          // context chips riding with it
    int state = STEER_NONE;   // steer delivery state
    int mode = 0;             // copilot mode: tints the bubble like a sent one
};

// A bubble that just left the stack (sent, read, cancelled): it fades and collapses out where it
// stood while the transcript shows its message above.
struct Ghost {
    StackItem item;
    float height = 0.0f;
    double born = 0.0;
};

struct SurfaceState {
    std::string conversation;
    int lastFrame = -100;
    std::vector<StackItem> prev;
    std::unordered_map<ImGuiID, float> heights;   // slot stride per item, last frame
    std::vector<Ghost> ghosts;
};
// One entry per surface that draws the stack (the docked column and the AI workspace can both
// show the same conversation); keyed by the window-scoped id of the stack.
std::unordered_map<ImGuiID, SurfaceState> s_surfaces;

// Items that changed identity without leaving (a saved edit re-keys its bubble): no ghost.
struct NoGhost { ImGuiID id; int frame; };
std::vector<NoGhost> s_noGhost;

// The one queued message being edited in place.
struct QueueEdit {
    bool active = false;
    std::string conv, original;
    size_t index = 0;
    std::vector<char> buf;
    int generation = 0;        // a fresh widget per edit
    bool focus = false;        // focus the input on its first frame
    bool wasActive = false;    // the input held the caret last frame (Esc reverts only from there)
    int lastFrame = -100;      // last frame the editor was drawn
};
QueueEdit s_edit;

struct Look {
    float padX, padY, round, metaGap, itemGap, lineH, btnPadX, btnPadY, btnH;
};

Look look() {
    Look k;
    k.padX = ui::metrics::cpChatBubblePadX();
    k.padY = ui::metrics::cpChatBubblePadY();
    k.round = ui::metrics::rounding(ui::metrics::cpChatBubbleRadius());
    k.metaGap = ui::metrics::cpChatMetaGap();
    k.itemGap = ui::metrics::px(6.0f);
    k.lineH = ImGui::GetTextLineHeight();
    k.btnPadX = ui::metrics::px(7.0f);
    k.btnPadY = ui::metrics::px(2.0f);
    k.btnH = k.lineH + 2.0f * k.btnPadY;
    return k;
}

ImU32 inkU32(const ImVec4& c, float a) {
    return ImGui::ColorConvertFloat4ToU32(ImVec4(c.x, c.y, c.z, c.w * a));
}

ImU32 rgbU32(const float rgb[3], float a) {
    return ImGui::ColorConvertFloat4ToU32(ImVec4(rgb[0], rgb[1], rgb[2], a));
}

// ---- identity -------------------------------------------------------------------------------
// Queued messages carry no id of their own, so a bubble is named by its text plus how many
// identical messages queue ahead of it.
ImGuiID stackKey(ImGuiID seed, const char* kind, const std::string& text, int duplicate) {
    ImGuiID h = ImHashStr(kind, 0, seed);
    h = ImHashStr(text.c_str(), text.size(), h);
    return ImHashData(&duplicate, sizeof(duplicate), h);
}

ImGuiID queuedKey(const AppState& st, ImGuiID seed, size_t index, const std::string& text) {
    int duplicate = 0;
    for (size_t i = 0; i < index && i < st.queuedPrompts.size(); ++i)
        if (st.queuedPrompts[i] == text) ++duplicate;
    return stackKey(seed, "queued", text, duplicate);
}

ImGuiID enterTween(ImGuiID itemKey) { return ImHashStr("enter", 0, itemKey); }

int draftCount(const nlohmann::json& draft, const char* key) {
    return draft.is_object() && draft.contains(key) && draft[key].is_array()
               ? (int)draft[key].size() : 0;
}

std::vector<StackItem> collectItems(const AppState& st, ImGuiID seed) {
    std::vector<StackItem> items;
    for (size_t i = pendingSteerTail(st.chat); i < st.chat.size(); ++i) {
        const ChatMsg& m = st.chat[i];
        StackItem it;
        it.steer = true;
        it.index = i;
        it.id = stackKey(seed, "steer", m.steerId, 0);
        it.text = m.text;
        it.attach = (int)m.imagePaths.size();
        it.context = (int)m.contextRefs.size();
        it.state = m.steerState;
        it.mode = m.copilotMode;
        items.push_back(std::move(it));
    }
    for (size_t i = 0; i < st.queuedPrompts.size(); ++i) {
        StackItem it;
        it.index = i;
        it.id = queuedKey(st, seed, i, st.queuedPrompts[i]);
        it.text = st.queuedPrompts[i];
        if (i < st.queuedPromptDrafts.size()) {
            const nlohmann::json& draft = st.queuedPromptDrafts[i];
            it.attach = draftCount(draft, "attachments") + draftCount(draft, "inline_images");
            it.context = draftCount(draft, "context_refs");
        }
        it.mode = st.aiInteraction;
        items.push_back(std::move(it));
    }
    return items;
}

bool ghostSuppressed(ImGuiID id, int frame) {
    s_noGhost.erase(std::remove_if(s_noGhost.begin(), s_noGhost.end(),
                                   [&](const NoGhost& n) { return frame - n.frame > 3; }),
                    s_noGhost.end());
    return std::any_of(s_noGhost.begin(), s_noGhost.end(),
                       [&](const NoGhost& n) { return n.id == id; });
}

// ---- captions -------------------------------------------------------------------------------
struct Caption {
    const char* label;
    const char* tip;
    ImVec4 ink;
};

// The delivery states a steer can still be pinned in (the transcript draws the settled ones).
Caption steerCaption(int state, const ui::AdaptiveSurface& surf) {
    if (state == STEER_QUEUED)
        return {"Queued \xc2\xb7 after this step",
                "The agent reads it when its current tool call returns. A wait on a job ends at "
                "once; real work (a solve, a build) finishes first. Stop withdraws it unread and "
                "sends it with your next message.",
                semanticOnGlass(ui::palette.statusWarn, surf)};
    return {"Sending\xe2\x80\xa6", "Handing your message to the running agent.", surf.textTertiary};
}

// ---- quiet text action ----------------------------------------------------------------------
float actionWidth(const char* label, const Look& k) {
    return ImGui::CalcTextSize(label).x + 2.0f * k.btnPadX;
}

// A text action with a hover wash. The label is centred in a padded rect, so every action in a
// row shares one baseline with the caption beside it. `a` fades it with its row; at a quarter
// of full alpha or less it is inert, so an invisible action is never clicked by accident. `dy`
// slides the paint only; the hit area stays put.
bool actionButton(const char* id, const char* label, const ImVec2& min, const Look& k, float a,
                  float dy, Tone tone, const char* tip, bool enabled = true) {
    const ui::AdaptiveSurface& surf = chatSurf();
    const ImVec2 size(actionWidth(label, k), k.btnH);
    bool pressed = false, hovered = false;
    if (a > 0.25f && enabled) {
        ImGui::SetCursorScreenPos(min);
        pressed = ImGui::InvisibleButton(id, size);
        hovered = ImGui::IsItemHovered();
    }
    const float t = ui::anim::toward(ImGui::GetID(id), hovered ? 1.0f : 0.0f, ui::anim::kFast);
    ImVec4 rest = surf.textSecondary;
    ImVec4 hot = surf.textPrimary;
    ImVec4 wash = surf.textPrimary;
    if (tone == Tone::Primary) rest = hot = wash = semanticOnGlass(ui::palette.accent, surf);
    const ImVec4 ink(rest.x + (hot.x - rest.x) * t, rest.y + (hot.y - rest.y) * t,
                     rest.z + (hot.z - rest.z) * t, rest.w + (hot.w - rest.w) * t);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 lo(min.x, min.y + dy);
    const ImVec2 hi(min.x + size.x, min.y + size.y + dy);
    if (t > 0.01f)
        dl->AddRectFilled(lo, hi, inkU32(wash, 0.14f * t * a), ui::metrics::rounding(5.0f));
    dl->AddText(ImVec2(lo.x + k.btnPadX, lo.y + (size.y - k.lineH) * 0.5f),
                inkU32(ink, enabled ? a : a * 0.45f), label);
    if (hovered && tip) ImGui::SetTooltip("%s", tip);
    return pressed;
}

const char* sendNowTip(AppState& st, size_t index) {
    if (queuedSendNowSteers(st, index))
        return "Send it to the running agent now. It reads the message at its next step and the "
               "run keeps going.";
    if (aiTurnActive(st)) return "Stop the current run and send this message next.";
    return "Send this message next, as soon as the assistant is free.";
}

// ---- one pending bubble ---------------------------------------------------------------------
struct Drawn {
    Act act = Act::None;
    float height = 0.0f;   // the slot this bubble occupies, from the cursor it started at
};

// Paints one bubble and its caption row at the cursor and reports the action the user took on
// it. It does NOT move the cursor past the slot: the caller owns the layout (a live item and a
// collapsing ghost size their slot differently). `a` fades the whole slot, `dy` slides its paint.
Drawn drawBubble(AppState& st, const StackItem& it, float avail, float a, float dy,
                 bool interactive) {
    const ui::AdaptiveSurface& surf = chatSurf();
    const Look k = look();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const float right = origin.x + avail;
    const float maxW = std::max(avail * (avail < ui::metrics::px(420.0f) ? 0.90f : 0.80f),
                                ui::metrics::px(120.0f));
    const float wrapW = std::max(maxW - 2.0f * k.padX, ui::metrics::px(40.0f));

    // Text: a bounded prefix, wrapped exactly as it is painted, clipped to a few lines.
    std::string shown = it.text;
    bool clipped = false;
    if (shown.size() > kMaxShownBytes) {
        size_t cut = kMaxShownBytes;
        while (cut > 0 && (static_cast<unsigned char>(shown[cut]) & 0xC0) == 0x80) --cut;
        shown.resize(cut);
        clipped = true;
    }
    ImVec2 full(0.0f, 0.0f);
    if (!shown.empty())
        full = ImGui::CalcTextSize(shown.c_str(), shown.c_str() + shown.size(), false, wrapW);
    const float maxTextH = k.lineH * (float)kMaxBubbleLines;
    if (full.y > maxTextH + 0.5f) clipped = true;
    const float textH = std::min(full.y, maxTextH);
    const float tailH = clipped ? k.lineH : 0.0f;

    // What rides with it: attachments and context chips, as small pills.
    std::vector<std::string> chips;
    if (it.attach > 0)
        chips.push_back(std::to_string(it.attach) + (it.attach == 1 ? " attachment" : " attachments"));
    if (it.context > 0) chips.push_back(std::to_string(it.context) + " context");
    const float pillPadX = ui::metrics::px(6.0f);
    const float pillGap = ui::metrics::px(4.0f);
    const float pillH = k.lineH + ui::metrics::px(2.0f);
    float chipsW = 0.0f;
    for (size_t c = 0; c < chips.size(); ++c)
        chipsW += ImGui::CalcTextSize(chips[c].c_str()).x + 2.0f * pillPadX + (c ? pillGap : 0.0f);

    // Shrink-to-fit, right-aligned like a sent bubble.
    float bw = std::clamp(full.x + 2.0f * k.padX + ui::metrics::px(2.0f),
                          ui::metrics::px(48.0f), maxW);
    if (!chips.empty()) bw = std::max(bw, std::min(chipsW + 2.0f * k.padX, maxW));
    const float bodyH = textH + tailH;
    const float bh = 2.0f * k.padY + bodyH +
                     (chips.empty() ? 0.0f : (bodyH > 0.0f ? ui::metrics::px(4.0f) : 0.0f) + pillH);
    const ImVec2 bMin(right - bw, origin.y);
    const ImVec2 bMax(right, origin.y + bh);

    // Dimmed and outlined: a pending message reads as a draft of itself, not a sent bubble.
    float tint[3];
    modeAccentRgb(it.mode, tint);
    dl->AddRectFilled(ImVec2(bMin.x, bMin.y + dy), ImVec2(bMax.x, bMax.y + dy),
                      rgbU32(tint, 0.07f * a), k.round);
    dl->AddRect(ImVec2(bMin.x, bMin.y + dy), ImVec2(bMax.x, bMax.y + dy),
                rgbU32(tint, 0.45f * a), k.round, 0, ui::metrics::stroke(1.0f));
    const ImVec2 tPos(bMin.x + k.padX, bMin.y + k.padY + dy);
    if (!shown.empty()) {
        dl->PushClipRect(tPos, ImVec2(bMax.x - k.padX + 2.0f, tPos.y + textH), true);
        dl->AddText(ImGui::GetFont(), ImGui::GetFontSize(), tPos, inkU32(surf.textSecondary, a),
                    shown.c_str(), shown.c_str() + shown.size(), wrapW);
        dl->PopClipRect();
    }
    if (clipped)
        dl->AddText(ImVec2(tPos.x, tPos.y + textH), inkU32(surf.textTertiary, a), "\xe2\x80\xa6");
    if (!chips.empty()) {
        float x = tPos.x;
        const float y = tPos.y + bodyH + (bodyH > 0.0f ? ui::metrics::px(4.0f) : 0.0f);
        for (const std::string& chip : chips) {
            const float w = ImGui::CalcTextSize(chip.c_str()).x + 2.0f * pillPadX;
            dl->AddRectFilled(ImVec2(x, y), ImVec2(x + w, y + pillH),
                              inkU32(surf.textPrimary, 0.10f * a), ui::metrics::rounding(6.0f));
            dl->AddText(ImVec2(x + pillPadX, y + (pillH - k.lineH) * 0.5f),
                        inkU32(surf.textSecondary, a), chip.c_str());
            x += w + pillGap;
        }
    }

    // Caption row under the bubble, right-aligned to it: [actions]  label. The label is always
    // there; the actions (queued messages only) fade in when the message is hovered.
    const float rowY = bMax.y + k.metaGap;
    const char* label = "Queued";
    const char* labelTip = it.index == 0
        ? "Waits for the current turn to finish, then goes out on its own."
        : "Goes out after the messages queued before it.";
    ImVec4 labelInk = surf.textTertiary;
    if (it.steer) {
        const Caption c = steerCaption(it.state, surf);
        label = c.label;
        labelTip = c.tip;
        labelInk = c.ink;
    }
    const float labelW = ImGui::CalcTextSize(label).x;
    const float labelX = right - ui::metrics::px(4.0f) - labelW;
    dl->AddText(ImVec2(labelX, rowY + (k.btnH - k.lineH) * 0.5f + dy), inkU32(labelInk, a), label);
    if (interactive) {
        ImGui::SetCursorScreenPos(ImVec2(labelX, rowY));
        ImGui::InvisibleButton("##caption", ImVec2(labelW, k.btnH));
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", labelTip);
    }

    Act act = Act::None;
    if (!it.steer) {
        const char* const names[3] = {"Edit", "Send now", "Cancel"};
        float xs[3];
        float x = labelX - ui::metrics::px(6.0f);
        for (int j = 2; j >= 0; --j) {
            x -= actionWidth(names[j], k);
            xs[j] = x;
            x -= ui::metrics::px(2.0f);
        }
        const bool hot = interactive && ImGui::IsWindowHovered() &&
                         ImGui::IsMouseHoveringRect(ImVec2(std::min(bMin.x, xs[0]), origin.y),
                                                    ImVec2(right, rowY + k.btnH), false);
        const float hotT = ui::anim::toward(ImGui::GetID("##hot"), hot ? 1.0f : 0.0f,
                                            ui::anim::kFast);
        const float actionA = hotT * a;
        if (actionA > 0.01f) {
            if (actionButton("##edit", names[0], ImVec2(xs[0], rowY), k, actionA, dy, Tone::Quiet,
                             "Change this message before it is sent."))
                act = Act::Edit;
            if (actionButton("##now", names[1], ImVec2(xs[1], rowY), k, actionA, dy, Tone::Quiet,
                             hotT > 0.25f ? sendNowTip(st, it.index) : nullptr))
                act = Act::SendNow;
            if (actionButton("##cancel", names[2], ImVec2(xs[2], rowY), k, actionA, dy,
                             Tone::Quiet,
                             "Take it out of the queue and put it back in the message box to "
                             "change it. Anything already typed there stays below it."))
                act = Act::Cancel;
        }
    }
    Drawn out;
    out.act = act;
    out.height = rowY + k.btnH + k.itemGap - origin.y;
    return out;
}

// ---- in-place editor ------------------------------------------------------------------------
void beginEdit(AppState& st, const std::string& conv, size_t index) {
    // Moving from one editor to another keeps the first one's change.
    if (s_edit.active && s_edit.index < st.queuedPrompts.size() &&
        st.queuedPrompts[s_edit.index] == s_edit.original) {
        const std::string text = chatInputText(s_edit.buf);
        if (!text.empty()) setQueuedPromptText(st, s_edit.index, text);
    }
    if (index >= st.queuedPrompts.size()) return;
    s_edit.active = true;
    s_edit.conv = conv;
    s_edit.index = index;
    s_edit.original = st.queuedPrompts[index];
    chatInputLoad(s_edit.buf, s_edit.original);
    ++s_edit.generation;
    s_edit.focus = true;
    s_edit.wasActive = false;
    s_edit.lastFrame = ImGui::GetFrameCount();
}

// The queued message under edit, in place of its bubble: a multi-line input (Enter saves,
// Shift+Enter breaks the line, Esc reverts) and Revert / Save / Send now. Saving re-keys the
// bubble (its identity is its text); the old key's ghost is suppressed and the new key starts
// settled, so a saved edit reads as the same bubble, not one leaving and another arriving.
void drawEditor(AppState& st, ImGuiID seed, float avail, bool hasPayload) {
    const ui::AdaptiveSurface& surf = chatSurf();
    const Look k = look();
    s_edit.lastFrame = ImGui::GetFrameCount();
    float tint[3];
    modeAccentRgb(st.aiInteraction, tint);

    char inputId[32];
    std::snprintf(inputId, sizeof(inputId), "##qedit%d", s_edit.generation);
    const bool hadCaret = s_edit.wasActive;
    const float left = ImGui::GetCursorScreenPos().x;
    ImGui::PushStyleColor(ImGuiCol_FrameBg, inkU32(surf.textPrimary, 0.06f));
    ImGui::PushStyleColor(ImGuiCol_Border, rgbU32(tint, 0.60f));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, k.round);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, ui::metrics::stroke(1.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(k.padX, k.padY));
    if (s_edit.focus) {
        ImGui::SetKeyboardFocusHere();
        s_edit.focus = false;
    }
    const bool enter = drawChatInput(inputId, s_edit.buf, "Edit message", 8);
    s_edit.wasActive = ImGui::IsItemActive();
    ImGui::PopStyleVar(3);
    ImGui::PopStyleColor(3);

    const std::string text = chatInputText(s_edit.buf);
    const bool canSave = !text.empty() || hasPayload;
    bool save = enter && canSave;
    bool revert = hadCaret && ImGui::IsKeyPressed(ImGuiKey_Escape, false);
    bool sendNow = false;

    const float rowY = ImGui::GetCursorScreenPos().y + ui::metrics::px(2.0f);
    float x = left + avail;
    x -= actionWidth("Send now", k);
    const float sendX = x;
    x -= ui::metrics::px(2.0f) + actionWidth("Save", k);
    const float saveX = x;
    x -= ui::metrics::px(2.0f) + actionWidth("Revert", k);
    const float revertX = x;
    if (actionButton("##q_now", "Send now", ImVec2(sendX, rowY), k, 1.0f, 0.0f, Tone::Quiet,
                     sendNowTip(st, s_edit.index), canSave))
        sendNow = true;
    if (actionButton("##q_save", "Save", ImVec2(saveX, rowY), k, 1.0f, 0.0f, Tone::Primary,
                     "Keep the change (Enter).", canSave))
        save = true;
    if (actionButton("##q_revert", "Revert", ImVec2(revertX, rowY), k, 1.0f, 0.0f, Tone::Quiet,
                     "Discard the change and keep the message as it was (Esc)."))
        revert = true;
    // The hint takes whatever room the actions leave, longest wording that fits.
    const float room = revertX - left - ui::metrics::px(8.0f);
    for (const char* hint : {"Enter saves \xc2\xb7 Shift+Enter adds a line \xc2\xb7 Esc reverts",
                             "Enter saves \xc2\xb7 Esc reverts"}) {
        if (ImGui::CalcTextSize(hint).x > room) continue;
        ImGui::GetWindowDrawList()->AddText(ImVec2(left, rowY + (k.btnH - k.lineH) * 0.5f),
                                            inkU32(surf.textTertiary, 1.0f), hint);
        break;
    }
    ImGui::SetCursorScreenPos(ImVec2(left, rowY));
    ImGui::Dummy(ImVec2(avail, k.btnH + k.itemGap));

    if (revert) {
        s_edit.active = false;
    } else if (save || sendNow) {
        const size_t index = s_edit.index;
        const ImGuiID oldKey = queuedKey(st, seed, index, s_edit.original);
        s_edit.active = false;
        setQueuedPromptText(st, index, text);
        s_noGhost.push_back({oldKey, ImGui::GetFrameCount()});
        ui::anim::toward(enterTween(queuedKey(st, seed, index, text)), 1.0f);   // seeds settled
        if (sendNow) queuedPromptSendNow(st, index);
    }
}

}  // namespace

bool queuedPromptEditHoldsDrain() {
    return s_edit.active && ImGui::GetFrameCount() - s_edit.lastFrame <= 2;
}

void drawPendingMessageStack(AppState& st, float width) {
    const bool any = pendingSteerTail(st.chat) != st.chat.size() || !st.queuedPrompts.empty();
    const ImGuiID surfaceId = ImGui::GetID("##pending_stack");
    const auto known = s_surfaces.find(surfaceId);
    const bool tracked = known != s_surfaces.end() &&
                         (!known->second.prev.empty() || !known->second.ghosts.empty());
    if (!any) s_edit.active = false;
    if (!any && !tracked) return;

    SurfaceState& S = s_surfaces[surfaceId];
    const std::string conv = st.activeConvId();
    const int frame = ImGui::GetFrameCount();
    const double now = ImGui::GetTime();
    // A different conversation, or a surface that was not drawn for a while (the chat was
    // hidden), starts settled: nothing arrives or leaves visibly.
    const bool fresh = S.conversation != conv || frame - S.lastFrame > 2;
    S.conversation = conv;
    S.lastFrame = frame;
    if (fresh) {
        S.ghosts.clear();
        S.heights.clear();
    }

    // The editor follows its message: re-find it when an earlier one left, drop it when it went
    // (sent, cancelled) or the conversation changed.
    if (s_edit.active) {
        bool ok = s_edit.conv == conv;
        if (ok && !(s_edit.index < st.queuedPrompts.size() &&
                    st.queuedPrompts[s_edit.index] == s_edit.original)) {
            const auto at = std::find(st.queuedPrompts.begin(), st.queuedPrompts.end(),
                                      s_edit.original);
            if (at == st.queuedPrompts.end()) ok = false;
            else s_edit.index = (size_t)(at - st.queuedPrompts.begin());
        }
        if (!ok) s_edit.active = false;
    }

    std::vector<StackItem> items = collectItems(st, surfaceId);

    // What left since last frame fades out where it stood.
    if (!fresh) {
        for (const StackItem& old : S.prev) {
            if (std::any_of(items.begin(), items.end(),
                            [&](const StackItem& cur) { return cur.id == old.id; }))
                continue;
            if (ghostSuppressed(old.id, frame)) continue;
            Ghost g;
            g.item = old;
            const auto h = S.heights.find(old.id);
            g.height = h != S.heights.end() ? h->second : ImGui::GetTextLineHeight() * 3.0f;
            g.born = now;
            S.ghosts.push_back(std::move(g));
        }
    }
    S.ghosts.erase(std::remove_if(S.ghosts.begin(), S.ghosts.end(),
                                  [&](const Ghost& g) {
                                      return (float)(now - g.born) >= ui::anim::kBase;
                                  }),
                   S.ghosts.end());

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float spacing = ImGui::GetStyle().ItemSpacing.y;
    const float avail = std::min(width, ImGui::GetContentRegionAvail().x);
    ImGui::PushID("##pending_stack");

    for (const Ghost& g : S.ghosts) {
        const float e = ui::anim::easeOut(std::min(1.0f, (float)(now - g.born) / ui::anim::kBase));
        const float extent = g.height * (1.0f - e);
        const ImVec2 p = ImGui::GetCursorScreenPos();
        if (extent > 0.5f) {
            dl->PushClipRect(p, ImVec2(p.x + avail, p.y + extent), true);
            drawBubble(st, g.item, avail, 1.0f - e, -e * ui::metrics::px(6.0f), false);
            dl->PopClipRect();
        }
        if (extent > spacing) ImGui::Dummy(ImVec2(avail, extent - spacing));
        ui::anim::requestFrames();
    }

    Act act = Act::None;
    size_t actIndex = 0;
    std::unordered_map<ImGuiID, float> heights;
    for (const StackItem& it : items) {
        // A bubble that arrives eases in (fade + a short rise); one seen before stays put.
        const ImGuiID tween = enterTween(it.id);
        if (!fresh && ui::anim::peek(tween, -1.0f) < 0.0f) ui::anim::toward(tween, 0.0f);
        const float enter = ui::anim::toward(tween, 1.0f, ui::anim::kBase);
        const float startY = ImGui::GetCursorScreenPos().y;
        ImGui::PushID((int)it.id);
        if (!it.steer && s_edit.active && s_edit.index == it.index) {
            drawEditor(st, surfaceId, avail, it.attach + it.context > 0);
        } else {
            const ImVec2 origin = ImGui::GetCursorScreenPos();
            const Drawn d = drawBubble(st, it, avail, enter, (1.0f - enter) * ui::metrics::px(10.0f),
                                       enter > 0.9f);
            if (d.act != Act::None) {
                act = d.act;
                actIndex = it.index;
            }
            ImGui::SetCursorScreenPos(origin);
            ImGui::Dummy(ImVec2(avail, d.height));
        }
        ImGui::PopID();
        heights[it.id] = ImGui::GetCursorScreenPos().y - startY;
    }
    ImGui::PopID();

    S.prev = std::move(items);
    S.heights = std::move(heights);

    // Applied after the whole stack drew, so the frame never mixes pre- and post-action rows.
    switch (act) {
    case Act::Edit:    beginEdit(st, conv, actIndex); break;
    case Act::SendNow: queuedPromptSendNow(st, actIndex); break;
    case Act::Cancel:  cancelQueuedPromptToComposer(st, actIndex); break;
    case Act::None:    break;
    }
}

}  // namespace ui
