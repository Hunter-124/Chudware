// Copilot composer (T-RF6 split of legacy_chat.cpp): the auto-growing wrapping chat input,
// file attachments + context refs, the single agent-turn launch, the session slash commands
// (/compact /shake /handoff /btw /prewalk /rule /skills /help), slash-menu autocomplete
// (commands + skills), the submitTurn send path, and the composer chrome (runtime banner,
// Agent/Plan/Ask prelude + context/provider-limit gauge rings, graduated
// segmented toolbar, in-box send/stop).

#include "copilot_internal.h"
#include "ui/provider_usage.h"   // THE subscription-usage poller behind the prelude's limit rings
#include "ui/provider_logos.h"   // brand mark for the model chip (same art as Settings->Providers)
#include "ui/copilot/text_wrap.h"   // hard-break bookkeeping the rewrapper must not eat
#include "ui/copilot/inline_image_refs.h"
#include "ui/clipboard_image.h"          // image-kind probe / mime / clipboard->PNG capture
#include "ui/copilot/image_lightbox.h"   // attachment thumbnail tile + full-size viewer
#include "ui/copilot/context_estimate.h"  // transcript-size estimate behind the context meter
#include "ui/copilot/context_chips.h"      // context-picker chips: add / reconcile / draw / focus
#include "ui/chudcode_descriptors.h"      // ui::chudcodeCompactReplacement — compact-result guard
#include "ui/chudcode_session_actions.h"  // per-conversation Chudcode session link (resume key)
#include "ui/file_explorer.h"             // ui::ensureWorkspaceDir — the fs_* jail every turn rides
#include "ui/ai/plan_board.h"             // planBoardTakeEditsForTurn — idle plan edits ride the turn
#include <climits>                        // INT_MAX saturation in activityCharsOf()
#include "core/jobs.h"

// ---- auto-growing, wrapping chat input -------------------------------------------------------
// ImGui's InputTextMultiline does NOT word-wrap (it renders with wrap_width=0, so long lines
// extend past the box and get clipped — "run off the screen"). So we maintain the wrapping
// ourselves: rewrapCore toggles spaces<->newlines so every visual line fits within wrapW.
// It is SAME-LENGTH (only swaps ' ' <-> '\n'), so cursor indices in the active widget stay
// valid. The box then grows by counting the newlines. A bare Enter submits; Shift/Alt+Enter
// insert a HARD break.
//
// Hard breaks have to be tracked out-of-band. The rewrapper's first step flattens every '\n'
// back to a space so the text can reflow from an idempotent source — which also erased the
// line breaks the user typed the moment they pressed the next key (live 2026-07-28:
// "alt+enter doesn't add a new line"). A '\n' carries no marker saying who put it there, and
// the same-length invariant forbids adding one, so the offsets the USER broke at are kept per
// buffer in g_wrap and shifted by each edit. Everything else in the buffer is ours to reflow.
bool rewrapCore(char* buf, int len, float wrapW, const std::vector<int>* hard) {
    if (len <= 0 || wrapW <= 1.0f) return false;
    bool changed = false;
    const auto isHard = [&](int i) {
        return hard && std::binary_search(hard->begin(), hard->end(), i);
    };
    // 1) flatten our own newlines back to spaces; leave the user's breaks standing.
    for (int i = 0; i < len; ++i)
        if (buf[i] == '\n' && !isHard(i)) { buf[i] = ' '; changed = true; }
    // 2) greedy word-wrap: break at the last space that keeps the running line within wrapW.
    //    A hard break ends its visual line, so measuring restarts after it.
    int lineStart = 0, lastSpace = -1;
    float lineW = 0.0f;
    for (int i = 0; i < len; ++i) {
        if (buf[i] == '\n') { lineStart = i + 1; lastSpace = -1; lineW = 0.0f; continue; }
        float cw = ImGui::CalcTextSize(buf + i, buf + i + 1).x;
        if (lineW + cw > wrapW && lastSpace >= lineStart) {
            buf[lastSpace] = '\n';
            changed = true;
            lineStart = lastSpace + 1;
            lineW = ImGui::CalcTextSize(buf + lineStart, buf + i).x;
            lastSpace = -1;
        }
        lineW += cw;
        if (buf[i] == ' ') lastSpace = i;
    }
    return changed;
}
// Plain soft wrap, for callers with no editing caret (transcript rendering in ui_common).
bool rewrapInPlace(char* buf, int len, float wrapW) {
    return rewrapCore(buf, len, wrapW, nullptr);
}
namespace {
using ui::copilot::WrapState;
using ui::copilot::seedHardBreaks;
using ui::copilot::normalizeComposerSelection;
using ui::copilot::noteEdit;
struct ChatInputCb {
    float wrapW = 0.0f;
    bool  interceptHistory = false;  // slash menu open — eat Up/Down for menu nav
    bool  rejectNewline = false;     // Enter is the SEND chord this frame — refuse its '\n'
    int*  navDelta = nullptr;        // -1 Up / +1 Down when interceptHistory
    WrapState* wrap = nullptr;       // hard-break bookkeeping for this input's buffer
    bool composer = false;
};
// Keyed by the backing buffer, which every caller has (drawChatInput takes it, composerText
// reads st.prompt). A vector reallocation just yields a fresh entry, which re-seeds below.
std::unordered_map<const char*, WrapState> g_wrap;
// Set by drawComposer when the slash autocomplete is open so drawChatInput routes Up/Down
// to the menu instead of moving the caret. Clarify cards (cards.cpp) leave this false.
bool g_slashMenuActive = false;
int  g_slashNavDelta = 0;
// Set when the slash menu fills the composer; the next InputText callback parks
// the caret at the end of the inserted text (a fresh widget state starts at 0).
int  g_slashCaretToEnd = 0;
// Only the main prompt opts in; question/clarify inputs never receive image operations.
bool g_mainComposerInput = false;
std::string g_imageInsert;
std::vector<size_t> g_imageRemovals;
size_t g_imageRosterSize = 0;
int g_composerCursor = 0, g_composerSelectionStart = 0, g_composerSelectionEnd = 0;
bool g_imageUseRememberedSelection = false;
}  // namespace


namespace {
int chatInputEditCallback(ImGuiInputTextCallbackData* data) {
    auto* cb = static_cast<ChatInputCb*>(data->UserData);
    if (data->EventFlag == ImGuiInputTextFlags_CallbackCharFilter) {
        // InputTextMultiline turns the Enter KEY into a '\n' character BEFORE anyone outside
        // the widget can see the key, and the send path below only detects the key. So a bare
        // Enter used to ship the message AND leave a '\n' in it at the caret — at the end it
        // was trimmed away, mid-text it split a word ("for me4\n90", live 2026-09-03). The
        // character filter is the one hook that runs before insertion: refuse the newline when
        // the chord is a send. Pasted newlines arrive under Ctrl/Shift and pass through.
        return (cb->rejectNewline && data->EventChar == '\n') ? 1 : 0;
    }
    if (data->EventFlag == ImGuiInputTextFlags_CallbackAlways && cb->composer) {
        const bool imageChanged = !g_imageRemovals.empty() || !g_imageInsert.empty();
        if (!g_imageRemovals.empty()) {
            const std::string flat = normalizeComposerSelection(
                std::string_view(data->Buf, data->BufTextLen), cb->wrap->hard,
                0, data->BufTextLen);
            const auto refs = ui::copilot::inlineImageRefs(flat, g_imageRosterSize);
            for (auto it = refs.rbegin(); it != refs.rend(); ++it) {
                if (std::find(g_imageRemovals.begin(), g_imageRemovals.end(), it->image) ==
                    g_imageRemovals.end()) continue;
                const int begin = (int)it->begin, count = (int)(it->end - it->begin);
                data->DeleteChars(begin, count);
                noteEdit(*cb->wrap, data->Buf, data->BufTextLen, begin);
            }
            g_imageRemovals.clear();
        }
        if (!g_imageInsert.empty()) {
            if (g_imageUseRememberedSelection) {
                data->CursorPos = std::clamp(g_composerCursor, 0, data->BufTextLen);
                data->SelectionStart = std::clamp(g_composerSelectionStart, 0, data->BufTextLen);
                data->SelectionEnd = std::clamp(g_composerSelectionEnd, 0, data->BufTextLen);
            }
            const int begin = std::min(data->SelectionStart, data->SelectionEnd);
            const int end = std::max(data->SelectionStart, data->SelectionEnd);
            if (begin != end) {
                data->DeleteChars(begin, end - begin);
                noteEdit(*cb->wrap, data->Buf, data->BufTextLen, begin);
            }
            const int at = begin != end ? begin : data->CursorPos;
            data->InsertChars(at, g_imageInsert.c_str());
            data->CursorPos = at + (int)g_imageInsert.size();
            data->SelectionStart = data->SelectionEnd = data->CursorPos;
            noteEdit(*cb->wrap, data->Buf, data->BufTextLen, data->CursorPos);
            g_imageInsert.clear();
            g_imageUseRememberedSelection = false;
        }
        if (imageChanged &&
            rewrapCore(data->Buf, data->BufTextLen, cb->wrapW, &cb->wrap->hard))
            data->BufDirty = true;
        g_composerCursor = data->CursorPos;
        g_composerSelectionStart = data->SelectionStart;
        g_composerSelectionEnd = data->SelectionEnd;
    }
    if (data->EventFlag == ImGuiInputTextFlags_CallbackAlways && g_slashCaretToEnd) {
        g_slashCaretToEnd = 0;
        data->CursorPos = data->BufTextLen;
        return 0;
    }
    if (data->EventFlag == ImGuiInputTextFlags_CallbackHistory && cb->interceptHistory) {
        // Steal Up/Down from the multiline caret so the slash menu can navigate.
        if (data->EventKey == ImGuiKey_UpArrow) {
            if (cb->navDelta) *cb->navDelta -= 1;
            return 1;
        }
        if (data->EventKey == ImGuiKey_DownArrow) {
            if (cb->navDelta) *cb->navDelta += 1;
            return 1;
        }
    }
    if (data->EventFlag == ImGuiInputTextFlags_CallbackEdit) {
        // Order matters: classify the edit against the PRE-rewrap buffer, so a newline the
        // user just typed is claimed as theirs before the wrapper is allowed to touch it.
        if (cb->wrap) noteEdit(*cb->wrap, data->Buf, data->BufTextLen, data->CursorPos);
        if (rewrapCore(data->Buf, data->BufTextLen, cb->wrapW,
                       cb->wrap ? &cb->wrap->hard : nullptr))
            data->BufDirty = true;   // same length, so BufTextLen/CursorPos are unchanged
    }
    return 0;
}
}  // namespace
// Magic keywords in the composer: the gradient painted onto the input's own glyphs, shimmering
// while the box is focused. Matched on a FLATTENED copy (wrapper newlines back to spaces, the
// user's hard breaks kept) so a soft wrap can never fake a fence line; the copy is the same
// length, so its ranges index the displayed buffer directly. The ambient phase (and with it the
// frame pump) only runs while a keyword is on screen and the box is focused.
static void paintComposerKeywords(ImGuiID wid, const std::vector<char>& buf, const WrapState& ws) {
    const bool active = ImGui::IsItemActive();
    const ImGuiInputTextState* state = active ? ImGui::GetInputTextState(wid) : nullptr;
    const std::string_view shown = (state && state->TextA.Data)
        ? std::string_view(state->TextA.Data, (size_t)state->TextLen)
        : std::string_view(buf.data(), std::strlen(buf.data()));
    bool probe = false;
    for (const ui::copilot::MagicKeyword& k : ui::copilot::magicKeywords())
        if (shown.find(k.word) != std::string_view::npos) { probe = true; break; }
    if (!probe) return;
    static std::string s_flat;   // GL-thread scratch, reused across frames
    s_flat.assign(shown.data(), shown.size());
    for (size_t i = 0; i < s_flat.size(); ++i)
        if (s_flat[i] == '\n' && !ui::copilot::isHardBreak(ws.hard, (int)i)) s_flat[i] = ' ';
    const std::vector<ui::copilot::KeywordRange> ranges = ui::copilot::magicKeywordRanges(s_flat);
    if (ranges.empty()) return;
    const bool focused = active || ImGui::IsItemFocused();
    const float shimmer = focused ? ui::anim::phase(ui::anim::kBreathe) : -1.0f;
    chatPaintMagicKeywords(shown, ranges, shimmer, /*glow=*/true);
}
// rightReserve narrows the widget (and its wrap width) so composer overlays anchored at the
// input's right edge — the in-box send button — never sit over the text.
bool drawChatInput(const char* id, std::vector<char>& buf, const char* placeholder,
                          int maxLines, float rightReserve) {
    if (buf.empty()) return false;
    const ImGuiStyle& style = ImGui::GetStyle();
    const float lineH = ImGui::GetTextLineHeight();
    float wrapW = ImGui::GetContentRegionAvail().x - rightReserve -
                  style.FramePadding.x * 2.0f - style.ScrollbarSize;
    if (wrapW < 1.0f) wrapW = 1.0f;

    // When the widget is NOT active it is safe to rewrap the backing buffer directly — this
    // covers programmatically-filled text (example chips) and panel resize. While active, the
    // edit callback rewraps ImGui's internal copy instead (external edits would be ignored).
    // We can't read ImGui's active id (internal API), so track per-id active state across frames.
    static std::unordered_map<ImGuiID, bool> s_activeLast;
    ImGuiID wid = ImGui::GetID(id);
    // g_wrap is keyed by the backing buffer's address, and st.prompt.resize() (draft restore,
    // staged-edit restore, slash-autocomplete insert) can move it. Retire this widget's previous
    // key when that happens: the entry is unreachable afterwards, and leaving it behind leaked
    // one hard-break vector per relocation for the lifetime of the process.
    static std::unordered_map<ImGuiID, const char*> s_wrapKey;
    const char* wrapKey = buf.data();
    const char*& lastKey = s_wrapKey[wid];
    if (lastKey && lastKey != wrapKey) g_wrap.erase(lastKey);
    lastKey = wrapKey;
    WrapState& ws = g_wrap[wrapKey];
    const int curLen = (int)std::strlen(buf.data());
    if (!s_activeLast[wid]) {
        // Idle: a length change can only have come from outside (chip, slash insert, clear),
        // and any newline in that text was put there deliberately — re-seed from it.
        if (curLen != ws.lastLen) seedHardBreaks(ws, buf.data(), curLen);
        rewrapCore(buf.data(), curLen, wrapW, &ws.hard);
    }

    int rows = 1;
    for (const char* p = buf.data(); *p; ++p) if (*p == '\n') ++rows;
    if (rows > maxLines) rows = maxLines;
    const float h = lineH * (float)rows + style.FramePadding.y * 2.0f;

    // ONE spelling of the send chord, shared by the newline filter and the submit test below:
    // Enter (either key) with no Shift/Alt/Super, and no Ctrl unless the slash menu is open
    // (there CtrlEnterForNewLine makes Ctrl+Enter the break and bare Enter the pick).
    const ImGuiIO& kio = ImGui::GetIO();
    const bool sendChord = !kio.KeyShift && !kio.KeyAlt && !kio.KeySuper &&
                           !(kio.KeyCtrl && g_slashMenuActive);
    const bool enterHeld = ImGui::IsKeyPressed(ImGuiKey_Enter, true) ||
                           ImGui::IsKeyPressed(ImGuiKey_KeypadEnter, true);

    // Cut/copy leave the composer's VISUAL-wrap newlines on the clipboard. The selection has to
    // be normalized from the pre-edit state (a cut deletes it inside the widget), so snapshot
    // it before InputText runs and overwrite the native payload afterwards. Only when the
    // widget was active last frame — that is when ImGui itself honours the chord.
    std::string clipOverride;
    if (s_activeLast[wid]) {
        const bool copyChord = kio.KeyCtrl && (ImGui::IsKeyPressed(ImGuiKey_C, false) ||
                                               ImGui::IsKeyPressed(ImGuiKey_Insert, false));
        const bool cutChord = (kio.KeyCtrl && ImGui::IsKeyPressed(ImGuiKey_X, false)) ||
                              (kio.KeyShift && ImGui::IsKeyPressed(ImGuiKey_Delete, false));
        if (copyChord || cutChord) {
            ImGuiInputTextState* state = ImGui::GetInputTextState(wid);
            if (state && state->HasSelection() && state->TextA.Data)
                clipOverride = normalizeComposerSelection(
                    std::string_view(state->TextA.Data,
                                     static_cast<std::size_t>(state->TextLen)),
                    ws.hard, state->GetSelectionStart(), state->GetSelectionEnd());
        }
    }

    g_slashNavDelta = 0;
    ChatInputCb cb;
    cb.wrapW = wrapW;
    cb.interceptHistory = g_slashMenuActive;
    cb.rejectNewline = enterHeld && sendChord && !g_slashMenuActive;
    cb.navDelta = &g_slashNavDelta;
    cb.wrap = &ws;
    cb.composer = g_mainComposerInput;
    ImGuiInputTextFlags flags = ImGuiInputTextFlags_CallbackEdit |
                                ImGuiInputTextFlags_CallbackAlways |
                                ImGuiInputTextFlags_CallbackCharFilter;
    if (g_slashMenuActive) {
        // Enter accepts a slash pick (handled by the menu) — don't insert a newline into the token.
        flags |= ImGuiInputTextFlags_CallbackHistory | ImGuiInputTextFlags_CtrlEnterForNewLine;
    }
    ImGui::InputTextMultiline(id, buf.data(), buf.size(),
        ImVec2(rightReserve > 0.0f ? -rightReserve : -1.0f, h),
        flags, chatInputEditCallback, &cb);
    paintComposerKeywords(wid, buf, ws);
    if (!clipOverride.empty()) ImGui::SetClipboardText(clipOverride.c_str());
    s_activeLast[wid] = ImGui::IsItemActive();
    // A BARE Enter sends; so does Ctrl+Enter, which ImGui's multiline treats as "validate"
    // and never turns into a line break. Shift/Alt+Enter are newlines: the widget inserts one
    // itself, so counting them as a submit shipped the message AND left a stray break behind
    // (live 2026-07-28: Alt+Enter sent the prompt instead of breaking the line). Detect the
    // KEY, not a trailing '\n' — the send chord's own '\n' never reaches the buffer (filter
    // above), so the text ships exactly as typed.
    bool submit = ImGui::IsItemFocused() && sendChord &&
                  (ImGui::IsKeyPressed(ImGuiKey_Enter, false) ||
                   ImGui::IsKeyPressed(ImGuiKey_KeypadEnter, false));
    if (buf[0] == 0 && placeholder && placeholder[0]) {
        ImVec2 mn = ImGui::GetItemRectMin();
        // Honor a pushed TextDisabled (the glass composer pushes surf.textTertiary) so the hint
        // adapts to the backdrop instead of always using the theme's disabled grey.
        ImGui::GetWindowDrawList()->AddText(
            ImVec2(mn.x + style.FramePadding.x * 2.0f, mn.y + style.FramePadding.y),
            ImGui::GetColorU32(ImGuiCol_TextDisabled), placeholder);
    }
    return submit;
}

namespace ui {

static bool startupComposerMetadataReady(const AppState& st) {
    static double interactiveSince = -1.0;
    if (!st.workerStarted || st.workerBootPending || !st.startupPhase.empty()) {
        interactiveSince = -1.0;
        return false;
    }
    const double now = ImGui::GetTime();
    if (interactiveSince < 0.0) interactiveSince = now;
    return now - interactiveSince >= 1.5;
}

// ---- O1: model picker → RPC payload threading --------------------------------------------------
// Canonical provider name sent to the long-lived worker from the GUI enum. PROV_LOCAL uses the
// OpenAI-compatible wire family — base_url (persisted before every send) discriminates local from
// cloud. PROV_CLOUDCOMPAT is a shared slot serving many services; agentProviderOverride refines
// it to the selected service via ui::cloudServiceKey(base_url).
static const char* providerWireName(int provider) {
    switch (provider) {
        case ui::PROV_ANTHROPIC:   return "anthropic";
        case ui::PROV_LOCAL:       return "openai";  // base_url discriminates (loopback)
        case ui::PROV_OPENAI:
        case ui::PROV_CLOUDCOMPAT: return "openai";  // cloud slot refined in agentProviderOverride
        case ui::PROV_LLAMACPP:    return "llamacpp";
        case ui::PROV_GEMINI:      return "gemini";
        case ui::PROV_XAI:         return "xai";  // saveSettings rewrites to xai-sub when no key
        case ui::PROV_CURSOR:      return "cursor";
        // Kimi Code uses the worker-facing "kimi-code" provider id; the older "kimi-sub"
        // spelling belongs to the retired Python subscription-provider path.
        case ui::PROV_KIMISUB:     return "kimi-code";
        // ZenMux keeps its catalog provider id when sent to the long-lived worker.
        case ui::PROV_ZENMUX:      return "zenmux";
        // Free AI (FreeLLMAPI) keeps its own worker provider namespace.
        case ui::PROV_FREELLMAPI:  return "freellmapi";
    }
    return "";
}

// The GUI pins provider/model/effort on every agent-turn request. Online design and mission
// launches are centralized through jobs.cpp's `agent_admin_turn` route; explicit offline mode
// alone retains the native core method. saveSettings still runs before each send so descriptor
// details (base_url / api_key / model_path) match the picker, while these fields pin the selected
// provider, model, and effort into the worker request.
// Effort ladder for the picker's current model, Chudcode catalog first and the static family
// ladders in model_registry.h otherwise. Returns the rung count.
static int composerEffortLadder(const Settings& s, const char** outLabels,
                                const char** outValues, int maxN) {
    ui::ModelInfo probe;
    const ui::ModelInfo* probeP = nullptr;
    if (const ui::BridgeModelMeta* bm = ui::bridgeModelMetaForCad(s.provider, s.model, s.baseUrl);
        bm) {
        probe = ui::modelInfoFromProbe(s.provider, s.model, bm->name, bm->contextWindow,
                                       bm->maxTokens, bm->vision, bm->tools,
                                       bm->reasoningEfforts, "catalog");
        probeP = &probe;
    }
    return ui::reasoningEffortOptions(s.provider, s.model, outLabels, outValues, maxN, probeP);
}

nlohmann::json agentProviderOverride(const Settings& s) {
    nlohmann::json j = nlohmann::json::object();
    const char* name = providerWireName(s.provider);
    std::string wire = name ? name : "";
    // The cloud-compat slot is shared by every base_url-keyed service (OpenRouter / DashScope /
    // DeepSeek / Moonshot and other hosted provider ids). Pin the real provider name so
    // Chudcode resolves the intended service rather than generic OpenAI compatibility.
    if (s.provider == ui::PROV_CLOUDCOMPAT)
        wire = ui::cloudServiceWireName(ui::cloudServiceKey(s.baseUrl));
    if (!wire.empty()) j["provider"] = wire;
    // llamacpp's Settings::model holds the gguf FILE PATH (the wire model stays "local-gguf");
    // don't clobber the descriptor's model with a filesystem path.
    if (s.provider != ui::PROV_LLAMACPP && s.model[0]) j["model"] = s.model;
    if (s.reasoningEffort[0]) {
        // Pin only a rung the model's CURRENT ladder actually offers: a stale pick inherited
        // from another model (e.g. "medium" beside K3's low|high|max) would be rejected or
        // mis-mapped upstream — omitting it lets the engine apply the model's default effort.
        const char* lv[8] = {};
        const char* vv[8] = {};
        const int n = composerEffortLadder(s, lv, vv, 8);
        bool valid = false;  // known model with no ladder never exposes a dial
        for (int i = 0; i < n; ++i)
            if (std::strcmp(s.reasoningEffort, vv[i]) == 0) valid = true;
        if (valid) j["reasoning_effort"] = s.reasoningEffort;
    }
    return j;
}

// ---- M23: composer file attachments -------------------------------------------------------------
static std::string attachmentBaseName(const std::string& p) {
    size_t cut = p.find_last_of("\\/");
    return cut == std::string::npos ? p : p.substr(cut + 1);
}

// Inline cap shared by BOTH delivery routes below (verbatim text file / extracted document
// text). One cap, one place: if the two routes were bounded differently one of them would
// quietly become the exception nobody remembers.
static constexpr size_t kAttachInlineCap = 64 * 1024;

// True for a byte string that is well-formed UTF-8. Load-bearing, not pedantry: everything
// inlined below ends up inside a JSON string, and nlohmann::json::dump() THROWS
// (type_error.316) on an invalid byte. That throw happens on the RPC path, well away from
// here, and takes the whole send with it — so a file we cannot represent has to be refused at
// the door and reported, not smuggled through.
static bool isValidUtf8(const std::string& s) {
    const unsigned char* p = (const unsigned char*)s.data();
    const size_t n = s.size();
    for (size_t i = 0; i < n;) {
        const unsigned char c = p[i];
        size_t extra = 0;
        unsigned int cp = 0;
        if (c < 0x80) { ++i; continue; }
        else if ((c & 0xE0) == 0xC0) { extra = 1; cp = c & 0x1Fu; }
        else if ((c & 0xF0) == 0xE0) { extra = 2; cp = c & 0x0Fu; }
        else if ((c & 0xF8) == 0xF0) { extra = 3; cp = c & 0x07u; }
        else return false;                                  // continuation byte / 0xF8+
        if (i + extra >= n) return false;                   // sequence runs off the end
        for (size_t k = 1; k <= extra; ++k) {
            const unsigned char cc = p[i + k];
            if ((cc & 0xC0) != 0x80) return false;
            cp = (cp << 6) | (cc & 0x3Fu);
        }
        if (extra == 1 && cp < 0x80) return false;          // overlong
        if (extra == 2 && cp < 0x800) return false;
        if (extra == 3 && cp < 0x10000) return false;
        if (cp > 0x10FFFF) return false;
        if (cp >= 0xD800 && cp <= 0xDFFF) return false;     // surrogate half
        i += extra + 1;
    }
    return true;
}

// Read a reasonably-sized TEXT file for inline injection into the request. Returns false for
// large files, binary files, and text we cannot put in a JSON string. Documents that have a
// real extractor are refused outright — see isBinaryDocument below.
static bool readAttachmentText(const std::string& path, std::string& out) {
    std::error_code ec;
    const uintmax_t sz = std::filesystem::file_size(path, ec);
    if (ec || sz == 0 || sz > (uintmax_t)kAttachInlineCap) return false;
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    std::ostringstream ss;
    ss << f.rdbuf();
    std::string data = ss.str();
    size_t ctrl = 0;
    for (unsigned char c : data) {
        if (c == 0) return false;                    // NUL => binary
        if (c < 9 || (c > 13 && c < 32)) ++ctrl;     // odd control chars
    }
    if (ctrl * 10 > data.size()) return false;       // >10% control bytes => binary
    if (!isValidUtf8(data)) return false;            // cannot be carried in the JSON payload
    out = std::move(data);
    return true;
}

// ---- attached DOCUMENTS: actually read them ---------------------------------------------------
// readAttachmentText cannot read a document, so a PDF/DOCX/PPTX/XLSX used to ride as
// "path only; content not inlined": the user saw a chip that looked fully handled while the
// model was handed a filename and nothing else, and the document was read only if the agent
// independently decided to call a document tool on that exact path. CAD already ships the
// reader — the FROZEN `office_extract` worker RPC (contracts/office.md §2), the same one the
// AI-tab documents panel uses. This wires it to the attach path.
//
// Extraction runs at ATTACH time, never at send time. consumeComposerAttachments() below is
// call-and-clear: it empties st.pendingAttachments as a side effect of building the payload, so
// a worker round-trip in there could consume-and-clear before a send that then fails, losing
// the user's attachments with no re-add path. Attaching is also when the user is looking at the
// chip, which is when feedback is worth anything.
// Every non-image attachment gets one of these, not just documents: the "chip looks handled
// while the content was dropped" failure is the same failure for a Latin-1 .txt as for a PDF,
// and one classification per file is also what stops the sniffing read from re-running every
// frame the chip is on screen.
enum class AttachState {
    PlainText,    // small, textual, JSON-safe: consumeComposerAttachments inlines it verbatim
    PathOnly,     // no extractor for this kind and not inlinable as text
    Running,      // office_extract in flight
    Ready,        // text in hand (possibly empty — a scanned PDF has none)
    Failed,       // per-file failure (bad/encrypted file, missing engine)
    Unavailable,  // this worker has no office_extract at all
};
struct AttachInfo {
    AttachState state = AttachState::Running;
    std::string text;         // already capped at kAttachInlineCap, on a UTF-8 boundary
    size_t      rawBytes = 0; // extractor's FULL text size, before the cap
    bool        truncated = false;
    std::string err;          // reason[: hint] for Failed
};
// Keyed by ABSOLUTE PATH, never by chip index: the chip row erases st.pendingAttachments
// BY INDEX on detach, so an index key would silently re-point one file's extract at another
// file's chip. The path key is also why there is no parallel vector here to desync.
static std::unordered_map<std::string, AttachInfo> g_attachInfo;

static const AttachInfo* attachInfoFor(const std::string& path) {
    auto it = g_attachInfo.find(path);
    return it == g_attachInfo.end() ? nullptr : &it->second;
}

// Drop entries for files that are no longer attached (detach, a send that consumed the chips,
// a conversation switch that cleared them). Keeps the cache the size of the chip row instead of
// the size of the session. An in-flight callback whose slot is gone simply does nothing.
static void pruneAttachInfo(const AppState& st) {
    for (auto it = g_attachInfo.begin(); it != g_attachInfo.end();) {
        const bool attached = std::find(st.pendingAttachments.begin(),
                                        st.pendingAttachments.end(),
                                        it->first) != st.pendingAttachments.end();
        if (attached) ++it;
        else it = g_attachInfo.erase(it);
    }
}

// Lowercase extension, or "" when the path has none.
static std::string attachmentExt(const std::string& path) {
    const size_t dot = path.find_last_of('.');
    if (dot == std::string::npos) return {};
    const size_t slash = path.find_last_of("\\/");
    if (slash != std::string::npos && dot < slash) return {};   // dot in a directory name
    std::string ext = path.substr(dot + 1);
    for (char& c : ext) c = (char)std::tolower((unsigned char)c);
    return ext;
}

// Extensions office_extract handles (contracts/office.md §2 / services/mcp/office_tools.py).
static bool isExtractableDocument(const std::string& path) {
    static const char* const kExts[] = {"pdf",  "docx", "doc",  "odt", "pptx", "ppt",
                                        "odp",  "xlsx", "xlsm", "xls", "ods",  "csv"};
    const std::string ext = attachmentExt(path);
    if (ext.empty()) return false;
    for (const char* e : kExts)
        if (ext == e) return true;
    return false;
}

// A document whose bytes are NOT text and must never reach readAttachmentText. Its sniffer is
// heuristic, and a small PDF slips through it: no NUL byte, few control bytes, so a 1.6 KB
// reportlab PDF was inlined as "text" — dumping the raw PDF header into the prompt AND, since
// those bytes are not valid UTF-8, throwing type_error.316 out of json::dump() on the RPC
// thread mid-send. Extension beats sniffing here: every one of these has a real extractor, so
// there is no case where reading the raw bytes is the better answer. .csv is the lone member
// of the extractable set that genuinely IS text, and keeps the verbatim path.
static bool isBinaryDocument(const std::string& path) {
    return isExtractableDocument(path) && attachmentExt(path) != "csv";
}

static std::string extractSizeLabel(size_t n) {
    char buf[48];
    if (n >= 1024) std::snprintf(buf, sizeof(buf), "%.0f KB", (double)n / 1024.0);
    else           std::snprintf(buf, sizeof(buf), "%zu B", n);
    return buf;
}

// Store an extract under the inline cap, marking the cut EXPLICITLY. Never silently truncate:
// a model handed 64 KB of a 900 KB spec with no marker answers as if it read the whole thing.
static void capExtract(AttachInfo& d, std::string text) {
    d.rawBytes = text.size();
    if (text.size() > kAttachInlineCap) {
        text.resize(kAttachInlineCap);
        // Back off to a UTF-8 boundary. A cut mid-sequence leaves invalid UTF-8 and
        // nlohmann::json::dump() THROWS on that — it would take the whole send down.
        while (!text.empty() && ((unsigned char)text.back() & 0xC0) == 0x80) text.pop_back();
        if (!text.empty() && ((unsigned char)text.back() & 0x80)) text.pop_back();
        d.truncated = true;
    }
    d.text = std::move(text);
}

// Permanently-absent RPC (older worker, method not registered) vs a per-file failure — the
// same distinction the AI-tab documents panel draws for this exact method.
static bool officeExtractAbsent(const core::RpcResult& r) {
    if (!r.ok)
        return r.err.find("unknown") != std::string::npos ||
               r.err.find("method") != std::string::npos ||
               r.err.find("not found") != std::string::npos ||
               r.err.find("not_found") != std::string::npos;
    if (!r.result.is_object()) return false;
    const std::string code = r.result.value("code", std::string());
    const std::string reason =
        r.result.value("ok", true) ? std::string() : r.result.value("reason", std::string());
    for (const std::string& s : {code, reason}) {
        if (s.empty()) continue;
        if (s == "method_not_found" || s == "unknown_method" || s == "not_implemented" ||
            s.find("unknown") != std::string::npos)
            return true;
    }
    return false;
}

// Decide, once per attached file, how its CONTENT will reach the model, and start the document
// read if that is the answer. Idempotent and cheap enough to call every frame for every chip,
// which is the point: EVERY attach route funnels through here — the paperclip, a draft restored
// by conversations.cpp, anything added later — so exactly one place decides a file's story, and
// the sniffing read happens once instead of once per frame.
static void ensureAttachmentContent(AppState& st, const std::string& path) {
    if (ui::isImageAttachment(path)) return;   // images ride structurally, not as text
    if (g_attachInfo.count(path)) return;      // already classified, running, or settled
    if (!isBinaryDocument(path)) {
        std::string verbatim;
        if (readAttachmentText(path, verbatim)) {  // small, textual, JSON-safe: verbatim wins
            AttachInfo& d = g_attachInfo[path];
            d.state    = AttachState::PlainText;
            d.rawBytes = verbatim.size();
            d.text     = std::move(verbatim);      // kept so the send does not re-read the file
            return;
        }
        if (!isExtractableDocument(path)) {     // nothing can turn this into text
            g_attachInfo[path].state = AttachState::PathOnly;
            return;
        }
    }
    if (!st.workerStarted) return;              // no document service yet — retried next frame
    g_attachInfo[path].state = AttachState::Running;
    // Async by design: a 900-page PDF must not park the GL thread. AsyncRpc runs this callback
    // inside st.rpc.drain() on the GL thread, so touching g_attachInfo here is safe.
    st.rpc.call("office_extract", nlohmann::json{{"path", path}},
                [path](const core::RpcResult& r) {
        auto it = g_attachInfo.find(path);
        if (it == g_attachInfo.end()) return;   // detached (and pruned) mid-flight
        AttachInfo& d = it->second;
        if (officeExtractAbsent(r)) { d.state = AttachState::Unavailable; return; }
        if (!r.ok) {
            d.state = AttachState::Failed;
            d.err = userFacingRpcError(r, "the document service did not respond");
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", true)) {
            d.state = AttachState::Failed;
            d.err = r.result.is_object() ? r.result.value("reason", std::string())
                                         : std::string();
            if (d.err.empty()) d.err = "extraction failed";
            const std::string hint =
                r.result.is_object() ? r.result.value("hint", std::string()) : std::string();
            if (!hint.empty()) d.err += ": " + hint;
            return;
        }
        d.state = AttachState::Ready;
        capExtract(d, r.result.value("text", std::string()));
    });
}

// The bracket header the model sees above an inlined extract.
static std::string extractHeadline(const AttachInfo& d) {
    std::string h = "extracted text, " + extractSizeLabel(d.text.size());
    if (d.truncated)
        h += " of " + extractSizeLabel(d.rawBytes) +
             " \xE2\x80\x94 TRUNCATED at the 64 KB inline cap; the remainder is NOT below, read "
             "the path above with a document tool if you need it";
    return h;
}

// Why this file rides as a path. Stated in the request text itself, because "the content was
// dropped and nobody said so" is the bug being fixed — not something to relocate.
static std::string pathOnlyNote(const AttachInfo* d) {
    if (!d) return "path only; content not inlined";
    switch (d->state) {
        case AttachState::Running:
            return "path only \xE2\x80\x94 text extraction was still running when this message "
                   "was sent; read it with a document tool";
        case AttachState::Ready:
            return "path only \xE2\x80\x94 the extractor found no text in this document "
                   "(scanned or image-only?)";
        case AttachState::Failed:
            return "path only \xE2\x80\x94 text extraction failed: " + d->err;
        case AttachState::Unavailable:
            return "path only \xE2\x80\x94 no document text extractor in this runtime";
        case AttachState::PathOnly:
            return "path only \xE2\x80\x94 not text we can inline (binary, over the 64 KB cap, "
                   "or not UTF-8); open it from the path if you need it";
        case AttachState::PlainText:
            break;
    }
    return "path only; content not inlined";
}

// Short chip badge naming what ACTUALLY happened to this file's text. Empty = nothing worth
// saying. A chip that reads as "handled" while the content was dropped is exactly the defect,
// so this is the visible half of the fix, not decoration.
static std::string attachChipBadge(const AppState& st, const std::string& path,
                                   const AttachInfo* d) {
    if (!d)
        return (isExtractableDocument(path) && !st.workerStarted) ? "queued" : std::string();
    switch (d->state) {
        case AttachState::PlainText:   return {};
        case AttachState::PathOnly:    return {};   // no badge: this is the normal, expected case
        case AttachState::Running:     return "extracting\xE2\x80\xA6";
        case AttachState::Ready:
            if (d->text.empty()) return "no text found";
            return extractSizeLabel(d->text.size()) +
                   (d->truncated ? " text, clipped" : " text");
        case AttachState::Failed:      return "not extracted";
        case AttachState::Unavailable: return "no extractor";
    }
    return {};
}

static std::string attachChipTooltip(const AppState& st, const std::string& path,
                                     const AttachInfo* d) {
    std::string t = path + "\n";
    if (!d) {
        if (isExtractableDocument(path) && !st.workerStarted)
            return t + "Attached. Waiting for the document service to start before its text "
                       "can be extracted \xE2\x80\x94 sent now it rides as a path only, and "
                       "the message says so.";
        return t + "Attached \xE2\x80\x94 rides your next message.";
    }
    switch (d->state) {
        case AttachState::PlainText:
            return t + "Attached \xE2\x80\x94 the file's text is inlined into your message.";
        case AttachState::PathOnly:
            return t + "Attached by PATH only \xE2\x80\x94 its bytes are not text we can inline "
                       "(binary, over the 64 KB cap, or not UTF-8), so the model gets the path "
                       "and opens the file itself.";
        case AttachState::Running:
            return t + "Reading the document (office_extract)\xE2\x80\xA6 Send before it "
                       "finishes and it rides as a path only \xE2\x80\x94 the message says so.";
        case AttachState::Ready:
            if (d->text.empty())
                return t + "The extractor found no text in this document (scanned or "
                           "image-only?). It rides as a path only.";
            t += "Extracted " + extractSizeLabel(d->text.size()) + " of text";
            if (d->truncated)
                t += ", CLIPPED from " + extractSizeLabel(d->rawBytes) +
                     " at the 64 KB inline cap. Your message says so and names the path, so "
                     "the agent can read the rest.";
            else
                t += ", inlined into your message.";
            return t;
        case AttachState::Failed:
            return t + "Text extraction failed: " + (d->err.empty() ? "unknown reason" : d->err) +
                   "\nIt rides as a path only \xE2\x80\x94 your message says so.";
        case AttachState::Unavailable:
            return t + "No document text extractor in this runtime. It rides as a path only "
                       "\xE2\x80\x94 your message says so.";
    }
    return t;
}

// Consume st.pendingAttachments into (a) an augmented request string — the text the model
// actually receives: text files inlined verbatim, documents inlined as their extract,
// everything else as an explicit path WITH the reason its content is absent — and (b) the
// structured "attachments" array carried in the RPC payload, where every entry now declares
// its "kind" ("image"|"file") and, for images, its "mime".
//
// The kinds are delivered by DIFFERENT routes, and all of them are real:
//   TEXT files  — the inline injection below is what actually delivers their content, because
//                 the request text is all a text-only model ever sees.
//   DOCUMENTS   — likewise inlined, from the extract office_extract produced at attach time
//                 (ensureAttachmentContent above). Never extracted here: this function clears
//                 the attachments as it runs and must not depend on a round-trip.
//   IMAGES      — numbered references stay at their authored positions; the exact ordered
//                 paths travel structurally as provider image parts, never a filename list.
//
// Both inline routes read from the AttachInfo the classifier already filled, so the bytes sent
// are the bytes the chip described — and the file is read once per attach, not once per send.
nlohmann::json consumeComposerAttachments(AppState& st, std::string& requestText,
                                           std::string* bubbleText) {
    nlohmann::json atts = nlohmann::json::array();
    // Draft numbers are stable slots, not attachment-vector indices. Compact only at send,
    // ordering images by first reference and reusing the same image for repeated references.
    const auto refs = ui::copilot::inlineImageRefs(requestText, st.pendingInlineImagePaths.size());
    const size_t unreferenced = st.pendingInlineImagePaths.size();
    std::vector<size_t> numbered(unreferenced, unreferenced);
    std::string normalized;
    size_t cursor = 0, imageCount = 0;
    for (const auto& ref : refs) {
        normalized.append(requestText, cursor, ref.begin - cursor);
        const std::string& path = st.pendingInlineImagePaths[ref.image];
        if (!path.empty()) {
            if (numbered[ref.image] == unreferenced) {
                numbered[ref.image] = imageCount++;
                atts.push_back({{"path", path}, {"name", attachmentBaseName(path)},
                                {"kind", "image"}, {"mime", ui::imageMimeForPath(path)},
                                {"inline", false}});
            }
            normalized += ui::copilot::inlineImageMarker(numbered[ref.image]);
        }
        cursor = ref.end;
    }
    normalized.append(requestText, cursor, std::string::npos);
    requestText = std::move(normalized);
    st.pendingInlineImagePaths.clear();
    // Legacy drafts / externally attached images have no inline roster yet. Keep usable:
    // add numbered refs once here, rather than the retired attached-image filename list.
    for (const std::string& path : st.pendingAttachments) {
        if (!ui::isImageAttachment(path)) continue;
        if (!requestText.empty()) requestText += "\n";
        requestText += ui::copilot::inlineImageMarker(imageCount++);
    }
    if (bubbleText) *bubbleText = requestText;
    for (const std::string& path : st.pendingAttachments) {
        const std::string base = attachmentBaseName(path);
        nlohmann::json a = {{"path", path}, {"name", base}};
        if (ui::isImageAttachment(path)) {
            // Explicitly NOT run through readAttachmentText: an image is binary, that probe
            // would reject it on the first NUL anyway, and inlining is not how it travels.
            a["kind"]   = "image";
            a["mime"]   = ui::imageMimeForPath(path);
            a["inline"] = false;
            // The numbered marker was prepared above; do not append a duplicate listing.
            atts.push_back(std::move(a));
            continue;
        }
        a["kind"] = "file";
        const AttachInfo* info = attachInfoFor(path);
        std::string fallback;
        if (!info && !isBinaryDocument(path) && readAttachmentText(path, fallback)) {
            // No classification yet — a send path that never drew a chip. Read it here rather
            // than drop a text file's content over a missing reconcile tick.
            a["inline"] = true;
            requestText += "\n\n[Attached file: " + base + " (" + path +
                           ")]\n```\n" + fallback;
            if (fallback.empty() || fallback.back() != '\n') requestText += "\n";
            requestText += "```";
        } else if (info && info->state == AttachState::PlainText) {
            a["inline"] = true;
            requestText += "\n\n[Attached file: " + base + " (" + path +
                           ")]\n```\n" + info->text;
            if (info->text.empty() || info->text.back() != '\n') requestText += "\n";
            requestText += "```";
        } else if (info && info->state == AttachState::Ready && !info->text.empty()) {
            a["inline"]            = true;
            a["extracted"]         = true;
            a["extract_chars"]     = (uint64_t)info->text.size();
            a["extract_truncated"] = info->truncated;
            requestText += "\n\n[Attached document: " + base + " (" + path + ") \xE2\x80\x94 " +
                           extractHeadline(*info) + "]\n```\n" + info->text;
            if (info->text.back() != '\n') requestText += "\n";
            requestText += "```";
        } else {
            a["inline"] = false;
            requestText += "\n\n[Attached file (" + pathOnlyNote(info) + "): " + path + "]";
        }
        atts.push_back(std::move(a));
    }
    st.pendingAttachments.clear();
    return atts;
}

// Wave4: take the pending context chips for THIS turn and disarm the cube. A chip whose document
// is no longer open is dropped here (its ids could name nothing — or, after a restart, a
// different document), with a toast so the omission is visible. The online worker resolves the
// rest against the engine and renders them into the prompt (worker_rpc._editor_context_block);
// lanes it does not reach inline core::contextRefsPromptText instead (offline route, steer).
static std::vector<core::ContextRef> consumeContextRefs(AppState& st) {
    std::vector<core::ContextRef> refs;
    st.contextPickActive = false;
    if (st.pendingContextRefs.empty()) return refs;
    reconcileContextRefDocs(st);
    int dropped = 0;
    for (auto& r : st.pendingContextRefs) {
        if (contextRefTabIndex(st, r) < 0) { ++dropped; continue; }
        refs.push_back(std::move(r));
    }
    st.pendingContextRefs.clear();
    if (dropped > 0)
        st.toast(ToastLevel::Info,
                 std::to_string(dropped) + " context chip" + (dropped == 1 ? "" : "s") +
                 " left out \xE2\x80\x94 " + (dropped == 1 ? "its document is" : "their documents are") +
                 " no longer open.");
    return refs;
}

// Images are already referenced in the normalized text. Ordinary files keep their compact
// roster; context refs are structured chips shared by fresh turns and mid-run steers.
static std::string userBubbleText(const std::string& text, const nlohmann::json& attachments) {
    std::string out = text;
    std::string files;
    for (const auto& a : attachments) {
        const std::string nm = a.value("name", std::string());
        if (a.value("kind", std::string("file")) != "image")
            files += " " + nm;
    }
    if (!files.empty()) out += "\n[attached:" + files + "]";
    return out;
}

static std::vector<std::string> userImagePaths(const nlohmann::json& attachments) {
    std::vector<std::string> paths;
    for (const auto& a : attachments)
        if (a.value("kind", std::string("file")) == "image")
            paths.push_back(a.at("path").get<std::string>());
    return paths;
}

// Create a fresh part document if needed and launch the v2 orchestrator for `request`. The
// current doc is replaced only when it already has features (so an empty new part is reused).
// Centralized so both the direct Design path and the post-clarify "Build it" path share it.
// options.history: the ACTIVE conversation's prior turns for the model, OLDEST FIRST, as
// [{"role":"user"|"assistant","text":"..."}]. st.chat is the source of truth (already
// persisted per-session by conversations.cpp); we only SEND it — never copy it into a
// second store. The message being sent THIS turn is excluded: every send path appends the
// user's bubble (then a running assistant placeholder) to st.chat BEFORE launching, so
// after skipping running/empty/error entries the outgoing prompt is always the trailing
// "user" entry and is dropped below. Cards.cpp's plan-approve execute path re-sends the
// original plan-request turn — its bubble is likewise the trailing user entry. ALL caps
// live here (the only place — the Python side does NOT re-trim): at most 12 entries and
// 8000 total chars (drop from the FRONT; recent turns matter most), each entry clipped to
// 1500 chars with a trailing " ...". Empty result == caller attaches NO history key, i.e.
// exactly today's behaviour.
static nlohmann::json chatHistoryForAgent(AppState& st) {
    constexpr size_t kMaxEntries = 12;
    constexpr size_t kMaxTotalChars = 8000;
    constexpr size_t kMaxEntryChars = 1500;
    nlohmann::json hist = nlohmann::json::array();
    for (const ChatMsg& m : st.chat) {
        if (m.running) continue;             // live placeholder / mid-stream turn
        if (m.errKind != ERR_NONE) continue; // error cards are not conversation content
        std::string t = m.text;              // text only — no images, tool cards, activity
        if (m.fromUser && !m.contextRefs.empty())   // the chips it carried, as the model saw them
            t += (t.empty() ? "" : "\n\n") + core::contextRefsPromptText(m.contextRefs);
        if (t.find_first_not_of(" \t\r\n") == std::string::npos) continue;
        if (t.size() > kMaxEntryChars) t = t.substr(0, kMaxEntryChars) + " ...";
        hist.push_back({{"role", m.fromUser ? "user" : "assistant"}, {"text", std::move(t)}});
    }
    // Exclude the outgoing message (always the trailing user entry — see header comment).
    if (!hist.empty() && hist.back().value("role", std::string()) == "user")
        hist.erase(std::prev(hist.end()));
    // Caps, dropping from the FRONT.
    while (hist.size() > kMaxEntries) hist.erase(hist.begin());
    size_t total = 0;
    for (const auto& e : hist) total += e.value("text", std::string()).size();
    while (!hist.empty() && total > kMaxTotalChars) {
        total -= hist.front().value("text", std::string()).size();
        hist.erase(hist.begin());
    }
    return hist;
}

// Attach the active conversation context shared by every agent-turn launch. The worker uses
// session_id + sessions_dir to bind lossless sub-agent transcripts beside this chat session.
// workspace_dir is the fs_* jail (CHUDWARE_FS_ROOT) and the project-save root: without it every
// fs_list/fs_glob/fs_read in the main chat refuses with workspace_root_unavailable, so the agent
// can never find the user's project files. The core's agent_workspace_dir() only runs on the
// offline lane; the online route goes straight to the worker, so the GUI must send it.
static void attachAgentConversationOptions(AppState& st, nlohmann::json& opts) {
#ifndef __EMSCRIPTEN__
    opts["session_id"] = st.activeConvId();
    opts["sessions_dir"] = chatSessionsDir();
#endif
    // The chat renders ask_user as an interactive question card and answers it over the control
    // lane (`answer`), so the worker blocks the tool instead of auto-accepting the defaults.
    opts["interactive_questions"] = true;
    ui::ensureWorkspaceDir(st);
    if (!st.workspaceDir.empty()) {
        opts["workspace_dir"] = st.workspaceDir;
        bindActiveConversationProject(st, st.workspaceDir);
    }
    if (nlohmann::json hist = chatHistoryForAgent(st); !hist.empty())
        opts["history"] = std::move(hist);
    // Chudcode session continuity: a conversation whose persisted link carries a session
    // file resumes THAT service-side session (branch fork, closed runtime, app restart)
    // instead of minting a fresh one. Launching also marks the runtime live — this turn
    // creates or reattaches it — which is what arms the Sessions panel's recovery actions.
    ChudcodeSessionLink& ccLink = chudcodeSessionLinkEnsure(st.activeConvId());
#ifndef __EMSCRIPTEN__
    if (!ccLink.sessionFile.empty()) opts["chudcode_session_file"] = ccLink.sessionFile;
#endif
    ccLink.runtimeLive = true;
}

// Composer mode name for the worker payload ("agent"|"plan"|"ask"). Legacy persisted values
// (Swarm/Edit/EngFirm) fold onto Agent through normalizeCopilotMode.
static const char* copilotModeName(int mode) {
    switch (normalizeCopilotMode(mode)) {
        case (int)CopilotMode::Plan: return "plan";
        case (int)CopilotMode::Ask:  return "ask";
        default:                     return "agent";
    }
}

// ONE Chudcode general_agent turn for `request` on the active conversation. It never creates or
// replaces a document: whether to new_part or edit the open doc is the AGENT's decision, made
// with the full CAD tool loadout, so the host must not pre-empt it with a blank part.
// `extra` is merged on top of the provider override (attachments / context_refs / options…);
// options.mode defaults to the composer's current mode name.
void launchAgentTurn(AppState& st, const std::string& request, nlohmann::json extra) {
    // Pin the picker's provider/model/effort explicitly in the worker payload so ambient provider
    // configuration cannot beat the visible selection. Merge attachments/context refs on top.
    nlohmann::json payload = agentProviderOverride(st.settings);
    if (extra.is_object()) {
        for (auto it = extra.begin(); it != extra.end(); ++it)
            payload[it.key()] = it.value();
    }
    std::string modeName;
    {
        nlohmann::json opts = nlohmann::json::object();
        if (payload.contains("options") && payload["options"].is_object())
            opts = payload["options"];
        if (!opts.contains("mode") || !opts["mode"].is_string() ||
            opts["mode"].get<std::string>().empty())
            opts["mode"] = copilotModeName(st.aiInteraction);
        modeName = opts["mode"].get<std::string>();
        if (!opts.contains("advisor")) opts["advisor"] = true;
        // corporate_mode mirrors Settings → AI → Behavior (default off — no auto company).
        opts["corporate_mode"] = st.settings.corporateMode;
        attachAgentConversationOptions(st, opts);
        payload["options"] = std::move(opts);
    }
    // Single online agent entry: jobs.cpp flattens this payload into `agent_admin_turn`; only an
    // explicit offline request retains the native `agent_run_v2` modeling fallback.
    st.addLog("[..] agent turn (" + modeName + "): " + request);
    st.agentV2Running = true;
    st.agentV2 = true;
    launchAgentRunV2(st.worker, st.activeRun(), st.activeConvId(), st.chatDocKey, request,
                     st.docMirror.hasDoc() ? st.docMirror.docId() : std::string(),
                     st.ribbonTab == AppState::RT_Manufacture, payload);
}

// The inline clarify card: build-critical questions (interaction contract kinds).
// Supports text/dimension (editable field), choice single (radio) + multi (checkbox),
// approval (Y/N), and geometry_select (arm context-cube pick → chips). "Build it" folds
// answers into the request; "Skip" uses suggested defaults.

// ============================ session slash commands (/compact /shake /handoff /btw /prewalk) ===
// Host owns the ChatMsg store; session_* RPCs are pure transforms (PySessionCmds). We convert
// ChatMsg → OpenAI-style role+content messages[], call the RPC, then apply the result.

struct BtwPanelState {
    bool open = false;
    bool loading = false;
    std::string question;
    std::string answer;
    std::string error;
    bool        busy = false;   // refused only because a run holds the service: calm + retryable
};
static BtwPanelState s_btw;

static nlohmann::json chatMsgsToWireMessages(const std::vector<ChatMsg>& msgs) {
    nlohmann::json arr = nlohmann::json::array();
    for (const auto& m : msgs) {
        if (m.text.empty() && m.running) continue;
        if (m.text.empty()) continue;
        nlohmann::json j;
        j["role"] = m.fromUser ? "user" : "assistant";
        j["content"] = m.text;
        arr.push_back(std::move(j));
    }
    return arr;
}

static void applyWireMessagesToChat(AppState& st, const nlohmann::json& messages) {
    if (!messages.is_array()) return;
    st.chat.clear();
    for (const auto& jm : messages) {
        if (!jm.is_object()) continue;
        ChatMsg cm;
        std::string role = jm.value("role", std::string("assistant"));
        cm.fromUser = (role == "user");
        if (jm.contains("content")) {
            if (jm["content"].is_string()) cm.text = jm["content"].get<std::string>();
            else if (jm["content"].is_array()) {
                // Concatenate text parts if multi-part content.
                std::string acc;
                for (const auto& p : jm["content"]) {
                    if (!p.is_object()) continue;
                    if (p.contains("text") && p["text"].is_string())
                        acc += p["text"].get<std::string>();
                    else if (p.contains("type") && p["type"] == "text" &&
                             p.contains("text") && p["text"].is_string())
                        acc += p["text"].get<std::string>();
                }
                cm.text = acc;
            }
        }
        if (cm.text.empty()) continue;
        st.chat.push_back(std::move(cm));
        ui::appendChatRawMessage(st, st.chat.back());
    }
    for (auto& m : st.chat) m.uiCachedH = -1.0f;
    persistChatHistory(st);
}

static void pushSessionNotice(AppState& st, const std::string& text) {
    ChatMsg n;
    n.fromUser = false;
    n.text = text;
    n.model = "system";
    st.chat.push_back(std::move(n));
    ui::appendChatRawMessage(st, st.chat.back());
    persistChatHistory(st);
    st.toast(ui::ToastLevel::Info, text.size() > 80 ? text.substr(0, 77) + "..." : text);
}

static void ovSetAgentBool(const char* key, bool v) {
    updateConfigJson([&](nlohmann::json& j) {
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        j["agent"][key] = v;
    });
}

// ============================ slash autocomplete (commands + skills) ============================
// Editor-style drop-up menu when the composer holds a single slash token (`^/[^\s]*$`), and an
// options menu when it holds a command that takes enumerated arguments plus the argument being
// typed (`^/cmd[ \t]+[^\s]*$`). Free-form arguments get an in-box hint after `/cmd `.
// Session cmds are local; skills come from the skills_list RPC (same catalog as Settings).

enum class SlashKind { Session, Skill, Meta };

struct SlashArg {
    const char* value;    // typed after the command, exactly
    const char* detail;   // what choosing it does, in user terms
};

struct SlashItem {
    SlashKind   kind = SlashKind::Session;
    std::string insert;   // text written into the composer on accept
    std::string label;    // primary row text (usually same as insert, trimmed)
    std::string detail;   // muted description
    const char* icon = "ai_panel";
    // Its service call shares the agent lane a running turn holds: unavailable during a run.
    bool        needsIdle = false;
    // Free-form argument syntax: /help, usage refusals, and the hint painted after `/cmd `.
    const char* argHint = nullptr;
    // Enumerated arguments: `/cmd ` opens the options menu; any other value is refused.
    std::vector<SlashArg> args;
    // What a bare `/cmd` does (options menu header). Set = the argument is optional, so the
    // menu opens with nothing picked and Enter alone runs the command.
    const char* noArgDetail = nullptr;
};

struct ComposerSkillRow {
    std::string name;
    std::string description;
    std::string source;
};

struct ComposerSkillsCache {
    std::vector<ComposerSkillRow> skills;
    bool   loaded = false;
    bool   inFlight = false;
    bool   rpcMissing = false;
    double lastList = -1e9;
};
static ComposerSkillsCache s_composerSkills;

struct SlashMenuState {
    bool open = false;
    int  selected = 0;                  // -1: nothing picked (options of an optional argument)
    std::string query;                  // the slash text as typed: `/cmd` or `/cmd arg`
    std::vector<SlashItem> matches;
    // Esc latch: once dismissed, stay closed while the query text is unchanged
    // (drawSlashAutocomplete recomputes `open` from the buffer every frame, so a
    // bare `open = false` reopens on the very next frame).
    bool        dismissed = false;
    std::string dismissedQuery;
};
static SlashMenuState s_slash;

static std::string slashLower(std::string s) {
    for (char& c : s) c = (char)std::tolower((unsigned char)c);
    return s;
}

static bool slashSkillsAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        if (e.find("unknown") != std::string::npos || e.find("not found") != std::string::npos ||
            e.find("not_found") != std::string::npos || e.find("method") != std::string::npos)
            return true;
        return false;
    }
    if (r.result.is_object()) {
        const std::string c = r.result.value("code", std::string());
        if (c == "unknown_method" || c == "method_not_found" || c == "not_implemented")
            return true;
        if (!r.result.value("ok", true)) {
            const std::string reason = r.result.value("reason", std::string());
            if (reason == "unknown_method" || reason == "method_not_found")
                return true;
        }
    }
    return false;
}

static void pollComposerSkills(AppState& st, bool force) {
    if (s_composerSkills.rpcMissing) return;
    if (!force && !startupComposerMetadataReady(st)) return;
    const double now = ImGui::GetTime();
    if (!force && (now - s_composerSkills.lastList) < 8.0) return;
    if (s_composerSkills.inFlight) return;
    s_composerSkills.lastList = now;
    s_composerSkills.inFlight = true;
    st.rpc.call("skills_list", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_composerSkills.inFlight = false;
        s_composerSkills.lastList = ImGui::GetTime();
        if (slashSkillsAbsent(r)) {
            s_composerSkills.rpcMissing = true;
            s_composerSkills.loaded = true;
            s_composerSkills.skills.clear();
            return;
        }
        if (!r.ok || !r.result.is_object()) {
            s_composerSkills.loaded = true;
            return;
        }
        s_composerSkills.skills.clear();
        auto arr = r.result.value("skills", nlohmann::json::array());
        if (arr.is_array()) {
            for (const auto& e : arr) {
                if (!e.is_object()) continue;
                ComposerSkillRow row;
                row.name = e.value("name", std::string());
                row.description = e.value("description", std::string());
                row.source = e.value("source", std::string("builtin"));
                if (!row.name.empty()) s_composerSkills.skills.push_back(std::move(row));
            }
        }
        std::sort(s_composerSkills.skills.begin(), s_composerSkills.skills.end(),
                  [](const ComposerSkillRow& a, const ComposerSkillRow& b) {
                      return a.name < b.name;
                  });
        s_composerSkills.loaded = true;
    }, 15000);
}

static bool tryHandleSkillsListCommand(AppState& st) {
    pollComposerSkills(st, /*force=*/true);
    if (s_composerSkills.skills.empty()) {
        pushSessionNotice(st,
            s_composerSkills.inFlight
                ? "Loading skills\xe2\x80\xa6 try /skills again in a moment."
                : (s_composerSkills.rpcMissing
                       ? "Skills aren't available in this version."
                       : "No skills discovered. Type / for the command menu."));
        return true;
    }
    std::string body = "Skills (" + std::to_string(s_composerSkills.skills.size()) +
                       ") \xe2\x80\x94 activate with /skill:<name> or pick from / autocomplete:\n";
    for (const auto& sk : s_composerSkills.skills) {
        body += "  /skill:";
        body += sk.name;
        if (!sk.description.empty()) {
            body += "  \xe2\x80\x94  ";
            body += sk.description.size() > 90 ? sk.description.substr(0, 87) + "\xe2\x80\xa6"
                                               : sk.description;
        }
        body += "\n";
    }
    pushSessionNotice(st, body);
    return true;
}

// Descriptions are user copy: what the command does, in plain words. Argument syntax lives in
// /help and the in-box hint, never in the menu row (raw "[elide|images]" read as developer
// notation); enumerated arguments get their own options menu with plain-word rows.
static const SlashItem kSessionSlashItems[] = {
    {SlashKind::Session, "/compact ", "/compact",
     "Summarize older messages to free up context", "ai_panel", true, nullptr,
     {{"summary", "The model writes a summary that replaces older messages"},
      {"snapcompact", "Archive older history as images (image-capable models only)"},
      {"shake", "Drop recoverable heavy tool output, no model call"}},
     "Enter alone uses your Compaction setting"},
    {SlashKind::Session, "/shake ", "/shake",
     "Drop bulky tool output and old images to free up context", "ai_panel", true, nullptr,
     {{"elide", "Replace bulky tool output with short placeholders"},
      {"images", "Remove old images from the transcript"}},
     "Enter alone drops bulky tool output"},
    {SlashKind::Session, "/handoff ", "/handoff",
     "Start a new session from a handoff summary of this one", "ai_panel", true, "[focus]"},
    {SlashKind::Session, "/btw ", "/btw",
     "Ask a side question that stays out of the transcript", "ask", false, "<question>"},
    {SlashKind::Session, "/prewalk", "/prewalk",
     "Walk through the project before the next run", "plan_board"},
    {SlashKind::Session, "/rule ", "/rule",
     "Add a rule that injects context when trigger words appear", "plan_board", true,
     "<trigger words> -> <context>"},
    {SlashKind::Meta, "/skills", "/skills",
     "List available skills", "team"},
    {SlashKind::Meta, "/help", "/help",
     "Show every command and its options", "ask"},
};

// The built-in command `text` invokes (first token, case-insensitive), or nullptr.
static const SlashItem* slashCommandFor(const std::string& text) {
    const std::string cmd = slashLower(text.substr(0, text.find_first_of(" \t\n")));
    for (const auto& it : kSessionSlashItems)
        if (it.label == cmd) return &it;
    return nullptr;
}

// The enumerated argument `argLower` names on `cmd`, or nullptr.
static const SlashArg* slashArgFor(const SlashItem& cmd, const std::string& argLower) {
    for (const SlashArg& a : cmd.args)
        if (argLower == a.value) return &a;
    return nullptr;
}

// One usage line per command, built from the table so /help and refusals never drift from it.
static std::string slashUsage(const SlashItem& cmd) {
    std::string u = cmd.label;
    if (!cmd.args.empty()) {
        u += cmd.noArgDetail ? " [" : " <";
        for (size_t i = 0; i < cmd.args.size(); ++i) {
            if (i) u += "|";
            u += cmd.args[i].value;
        }
        u += cmd.noArgDetail ? "]" : ">";
    } else if (cmd.argHint) {
        u += " ";
        u += cmd.argHint;
    }
    return u;
}

// True when the composer is still typing a single slash token (menu should stay open).
static bool parseSlashQuery(const char* buf, std::string& outFilter) {
    outFilter.clear();
    if (!buf || buf[0] != '/') return false;
    for (const char* p = buf + 1; *p; ++p) {
        if (*p == ' ' || *p == '\t' || *p == '\n') return false;
    }
    outFilter.assign(buf + 1);
    return true;
}

// `/cmd <partial>` on one line where /cmd takes enumerated arguments: the command, with the
// argument typed so far in `outArg`. A complete argument returns nullptr — there is nothing
// left to pick, so Enter sends.
static const SlashItem* parseSlashArgQuery(const char* buf, std::string& outArg) {
    outArg.clear();
    if (!buf || buf[0] != '/') return nullptr;
    const std::string text(buf);
    if (text.find('\n') != std::string::npos) return nullptr;
    const size_t sp = text.find_first_of(" \t");
    if (sp == std::string::npos) return nullptr;
    const SlashItem* cmd = slashCommandFor(text);
    if (!cmd || cmd->args.empty()) return nullptr;
    const size_t argAt = text.find_first_not_of(" \t", sp);
    if (argAt != std::string::npos) {
        if (text.find_first_of(" \t", argAt) != std::string::npos) return nullptr;
        outArg = text.substr(argAt);
    }
    if (slashArgFor(*cmd, slashLower(outArg))) return nullptr;
    return cmd;
}

// Free-form argument hint for the box: `/cmd ` (whitespace, nothing typed yet) on a command
// whose argument has no options menu.
static const char* slashFreeArgHint(const char* buf) {
    if (!buf || buf[0] != '/') return nullptr;
    const std::string text(buf);
    const size_t sp = text.find_first_of(" \t");
    if (sp == std::string::npos || text.find_first_not_of(" \t", sp) != std::string::npos)
        return nullptr;
    const SlashItem* cmd = slashCommandFor(text);
    return cmd && cmd->args.empty() ? cmd->argHint : nullptr;
}

static bool slashMatches(const SlashItem& it, const std::string& filterLower) {
    if (filterLower.empty()) return true;
    auto has = [&](const std::string& hay) {
        return slashLower(hay).find(filterLower) != std::string::npos;
    };
    if (has(it.label)) return true;
    if (has(it.insert)) return true;
    if (has(it.detail)) return true;
    // `/skill:edit-part` also matches bare skill name / `skill:edit`.
    if (it.kind == SlashKind::Skill) {
        const size_t colon = it.label.rfind(':');
        if (colon != std::string::npos && has(it.label.substr(colon + 1))) return true;
    }
    return false;
}

static void rebuildSlashMatches(const std::string& filter) {
    const std::string fl = slashLower(filter);
    s_slash.matches.clear();
    for (const auto& it : kSessionSlashItems) {
        if (slashMatches(it, fl)) s_slash.matches.push_back(it);
    }
    for (const auto& sk : s_composerSkills.skills) {
        SlashItem it;
        it.kind = SlashKind::Skill;
        it.insert = "/skill:" + sk.name + " ";
        it.label = "/skill:" + sk.name;
        it.detail = sk.description.empty()
                        ? (sk.source.empty() ? "skill" : sk.source)
                        : sk.description;
        it.icon = "plan_board";
        if (slashMatches(it, fl) || slashLower(sk.name).find(fl) != std::string::npos)
            s_slash.matches.push_back(std::move(it));
    }
    if (s_slash.selected < 0) s_slash.selected = 0;
    if (s_slash.selected >= (int)s_slash.matches.size())
        s_slash.selected = s_slash.matches.empty() ? 0 : (int)s_slash.matches.size() - 1;
}

// Options menu rows for `cmd`'s enumerated arguments matching `arg` (value or description).
// Accepting a row writes the complete `/cmd value`, which closes the menu (parseSlashArgQuery).
static void rebuildSlashArgMatches(const SlashItem& cmd, const std::string& arg) {
    const std::string fl = slashLower(arg);
    s_slash.matches.clear();
    for (const SlashArg& a : cmd.args) {
        if (!fl.empty() && slashLower(a.value).find(fl) == std::string::npos &&
            slashLower(a.detail).find(fl) == std::string::npos)
            continue;
        SlashItem it;
        it.kind = cmd.kind;
        it.insert = cmd.label + " " + a.value;
        it.label = a.value;
        it.detail = a.detail;
        it.icon = cmd.icon;
        it.needsIdle = cmd.needsIdle;
        s_slash.matches.push_back(std::move(it));
    }
    if (s_slash.selected >= (int)s_slash.matches.size())
        s_slash.selected = s_slash.matches.empty() ? -1 : (int)s_slash.matches.size() - 1;
}

static void applySlashInsert(std::vector<char>& buf, const std::string& text) {
    if (buf.size() < text.size() + 1)
        buf.resize(text.size() + 1, 0);
    std::memcpy(buf.data(), text.c_str(), text.size() + 1);
}

// Each row is the command line plus its description WRAPPED to the row's text width (up to
// kSlashDetailLines lines, the last one ellipsized only past that), plus padding. Heights are
// measured per row from the same text width the painter uses, so the list height computed
// before the window opens matches what is drawn. A fixed one-line description elided every
// longer description ("Compact conversation with the…").
static constexpr int kSlashDetailLines = 2;
static float slashRowPad()     { return 8.0f * ui::uiScale; }
static float slashIconLane()   { return ImGui::GetFontSize() * 1.02f + 7.0f * ui::uiScale; }
// Text width of a row whose Selectable spans `rowW` (icon lane + both side pads excluded).
static float slashTextWidth(float rowW) {
    return std::max(1.0f, rowW - slashRowPad() * 2.0f - slashIconLane());
}
// The description as drawn: wrapped at `textW`, cut with an ellipsis after kSlashDetailLines.
static std::string slashFitDetail(const char* text, float textW) {
    std::string s = text ? text : "";
    const float maxH = ImGui::GetTextLineHeight() * (float)kSlashDetailLines + 0.5f;
    auto wrappedH = [&](const std::string& t) {
        return ImGui::GetFont()->CalcTextSizeA(ImGui::GetFontSize(), FLT_MAX, textW, t.c_str())
            .y;
    };
    if (s.empty() || wrappedH(s) <= maxH) return s;
    const std::string ell = "\xe2\x80\xa6";
    while (!s.empty() && wrappedH(s + ell) > maxH) {
        do s.pop_back();
        while (!s.empty() && (static_cast<unsigned char>(s.back()) & 0xC0) == 0x80);
    }
    while (!s.empty() && s.back() == ' ') s.pop_back();
    return s + ell;
}
static float slashRowHeight(const char* detail, float textW) {
    const float lineH = ImGui::GetTextLineHeight();
    float h = lineH;
    if (detail && detail[0]) {
        const std::string d = slashFitDetail(detail, textW);
        h += ImGui::GetFont()->CalcTextSizeA(ImGui::GetFontSize(), FLT_MAX, textW, d.c_str()).y;
    }
    return h + slashRowPad();
}

// Trim `s` (UTF-8) with a trailing ellipsis so it fits `maxW` pixels. Shrinks by whole
// codepoints; the ellipsis is appended once (an earlier loop re-appended it every pass and
// never converged, hanging the UI thread).
static std::string slashFitText(const char* text, float maxW) {
    std::string s = text ? text : "";
    if (maxW <= 1.0f || ImGui::CalcTextSize(s.c_str()).x <= maxW) return s;
    const std::string ell = "\xe2\x80\xa6";
    while (!s.empty() && ImGui::CalcTextSize((s + ell).c_str()).x > maxW) {
        do s.pop_back();
        while (!s.empty() && (static_cast<unsigned char>(s.back()) & 0xC0) == 0x80);
    }
    while (!s.empty() && s.back() == ' ') s.pop_back();
    return s + ell;
}

// `disabled` rows (a command that cannot run during a run) paint dimmed, never accept, and show
// the reason as their detail line.
static bool slashMenuRow(const char* iconName, const char* label, const char* detail,
                         bool selected, int uid, bool disabled = false) {
    const auto& pal = ui::palette;
    const float fh = ImGui::GetFontSize();
    const float lineH = ImGui::GetTextLineHeight();
    const float rowW = ImGui::GetContentRegionAvail().x;
    const float textW = slashTextWidth(rowW);
    const float rowH = slashRowHeight(detail, textW);
    ImGui::PushID(uid);
    const bool clicked = ImGui::Selectable("##slashrow", selected,
                                           ImGuiSelectableFlags_AllowItemOverlap |
                                               ImGuiSelectableFlags_DontClosePopups,
                                           ImVec2(rowW, rowH));
    const bool hov = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
    const float rnd = 4.0f;
    const float washA = selected ? 0.42f : (hov ? 0.36f : 0.28f);
    ui::glassRowBackground(dl, mn, mx, rnd, /*stripe=*/false, /*selected=*/false,
                           /*washAlpha=*/washA);
    if (!disabled && (selected || hov)) {
        ui::coloredRowWash(dl, mn, mx, rnd, pal.accent,
                           selected ? 0.34f : 0.18f, /*selected=*/selected);
    }
    const float pad = slashRowPad();
    const float labelY = mn.y + pad * 0.5f;
    const float isz = fh * 1.02f;
    const ImU32 ink = ui::Palette::toU32(selected && !disabled ? pal.text : pal.textDim);
    // The icon lane is reserved on every row (icon or not) so the wrap width used to measure the
    // row height above is exactly the one the description is drawn with.
    const float textX = mn.x + pad + slashIconLane();
    IconUV ic = ui::getIcon(iconName, 20);
    if (ic.valid) {
        const ImVec2 i0(mn.x + pad, labelY + (lineH - isz) * 0.5f);
        dl->AddImage(ic.texId, i0, ImVec2(i0.x + isz, i0.y + isz), ic.uv0, ic.uv1, ink);
    }
    dl->AddText(ImVec2(textX, labelY), ui::Palette::toU32(disabled ? pal.textDim : pal.text),
                slashFitText(label, textW).c_str());
    if (detail && detail[0]) {
        const std::string d = slashFitDetail(detail, textW);
        dl->AddText(ImGui::GetFont(), fh, ImVec2(textX, labelY + lineH),
                    ui::Palette::toU32(pal.textDim), d.c_str(), nullptr, textW);
    }
    ImGui::PopID();
    return clicked && !disabled;
}

// Draw the drop-up slash menu above the composer (its bottom sits `anchorTopY`, the top of the
// prelude row, minus a gap, so it never covers the mode chip or the gauges). Returns true when
// an item was accepted (caller must bump the InputText generation so the insert is visible).
static bool drawSlashAutocomplete(AppState& st, const ImVec2& inputMin, const ImVec2& inputMax,
                                  float anchorTopY, bool& ateEnter) {
    ateEnter = false;
    std::string filter;
    const SlashItem* argCmd = nullptr;
    bool want = parseSlashQuery(st.prompt.data(), filter);
    if (!want) {
        argCmd = parseSlashArgQuery(st.prompt.data(), filter);
        want = argCmd != nullptr;
    }
    if (!want) {
        s_slash.open = false;
        s_slash.matches.clear();
        s_slash.dismissed = false;
        s_slash.dismissedQuery.clear();
        g_slashMenuActive = false;
        return false;
    }
    const std::string query(st.prompt.data());

    if (s_slash.dismissed) {
        if (query == s_slash.dismissedQuery) {
            // Esc dismissed this exact query — stay closed until the text changes.
            s_slash.open = false;
            s_slash.matches.clear();
            g_slashMenuActive = false;
            return false;
        }
        s_slash.dismissed = false;  // query changed — re-arm the menu
    }

    if (argCmd) {
        if (!s_slash.open || query != s_slash.query) {
            s_slash.query = query;
            // An optional argument opens with nothing picked so Enter alone runs the bare
            // command; once the user types, the best match is picked.
            s_slash.selected = (argCmd->noArgDetail && filter.empty()) ? -1 : 0;
        }
        rebuildSlashArgMatches(*argCmd, filter);
        s_slash.open = !s_slash.matches.empty();
        if (!s_slash.open) {
            g_slashMenuActive = false;
            return false;
        }
    } else {
        pollComposerSkills(st, /*force=*/!s_composerSkills.loaded);
        if (!s_slash.open || query != s_slash.query) {
            s_slash.query = query;
            s_slash.selected = 0;
        }
        rebuildSlashMatches(filter);  // every frame: skills may arrive mid-typing
        s_slash.open = !s_slash.matches.empty() || filter.empty();
        if (s_slash.matches.empty()) {
            // Keep the menu shell open on bare `/` while skills load; otherwise hide.
            if (!filter.empty() || (!s_composerSkills.inFlight && s_composerSkills.loaded)) {
                s_slash.open = false;
                g_slashMenuActive = false;
                return false;
            }
        }
    }
    g_slashMenuActive = true;

    // Apply Up/Down captured by the InputText history callback.
    bool scrolledByKey = false;
    if (g_slashNavDelta != 0 && !s_slash.matches.empty()) {
        const int n = (int)s_slash.matches.size();
        if (s_slash.selected < 0) {
            s_slash.selected = g_slashNavDelta > 0 ? 0 : n - 1;
        } else {
            s_slash.selected = (s_slash.selected + g_slashNavDelta) % n;
            if (s_slash.selected < 0) s_slash.selected += n;
        }
        g_slashNavDelta = 0;
        scrolledByKey = true;
    }

    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const auto& pal = ui::palette;
    const float popW = ImMax(inputMax.x - inputMin.x, 280.0f * sc);
    const float winPad = 6.0f * sc;
    const float rowGap = 2.0f * sc;  // ItemSpacing.y pushed below
    // An optional argument's options carry a second, wrapped header line saying what Enter does.
    const char* headerNote = argCmd ? argCmd->noArgDetail : nullptr;
    const float headerTextW = popW - winPad * 2.0f;
    const float headerH =
        ImGui::GetTextLineHeight() + 10.0f * sc +
        (headerNote ? ImGui::CalcTextSize(headerNote, nullptr, false, headerTextW).y + rowGap
                    : 0.0f);
    const float gap = 4.0f * sc;
    // Any run (this session's or another's) holds the agent lane these commands need.
    const bool runBusy = aiTurnActive(st) || st.anyRunActive();
    auto rowDisabled = [runBusy](const SlashItem& it) { return runBusy && it.needsIdle; };
    // Up to 8 rows, but never taller than the space above the composer: the menu drops UP
    // and must not run off the top of the window. The list scrolls for the rest. Rows are
    // measured with the list's text width (a scrollbar's width is held back once the list
    // overflows) so the computed height matches the drawn rows.
    const float roomAbove = (anchorTopY - gap) - ImGui::GetMainViewport()->WorkPos.y -
                            headerH - 20.0f * sc;
    const size_t shownMax = std::min<size_t>(8, s_slash.matches.size());
    const bool   overflows = s_slash.matches.size() > shownMax;
    const float  listW = popW - winPad * 2.0f - (overflows ? ImGui::GetStyle().ScrollbarSize : 0.0f);
    const float  textW = slashTextWidth(listW);
    float listContentH = 0.0f;
    for (size_t i = 0; i < shownMax; ++i) {
        const SlashItem& it = s_slash.matches[i];
        const float h = slashRowHeight(rowDisabled(it) ? kAvailableAfterRun : it.detail.c_str(),
                                       textW);
        const float next = listContentH + (i ? rowGap : 0.0f) + h;
        if (i > 0 && next > roomAbove) break;   // always at least one row
        listContentH = next;
    }
    if (listContentH <= 0.0f) listContentH = ImGui::GetTextLineHeight() + slashRowPad();
    const float listH = listContentH + 6.0f * sc;
    const float popH = headerH + listH + 8.0f * sc;

    ImGui::SetNextWindowPos(ImVec2(inputMin.x, anchorTopY - gap), ImGuiCond_Always,
                            ImVec2(0.0f, 1.0f));
    ImGui::SetNextWindowSize(ImVec2(popW, popH), ImGuiCond_Always);
    ImGui::SetNextWindowBgAlpha(0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(winPad, winPad));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0, 2.0f * sc));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, ui::metrics::rounding(8.0f));
    ImGui::PushStyleColor(ImGuiCol_Border, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_Header, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive, ImVec4(0, 0, 0, 0));

    bool accepted = false;
    std::string acceptText;
    ImGuiWindowFlags wflags =
        ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoMove |
        ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoFocusOnAppearing |
        ImGuiWindowFlags_NoNavFocus | ImGuiWindowFlags_NoScrollbar;
    if (ImGui::Begin("##slashautocomplete", nullptr, wflags)) {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        dl->ChannelsSplit(2);
        dl->ChannelsSetCurrent(1);

        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        if (argCmd) {
            const std::string header = argCmd->label + " options";
            ImGui::TextUnformatted(slashFitText(header.c_str(), headerTextW).c_str());
        } else {
            ImGui::TextUnformatted(s_slash.matches.empty() ? "Loading commands\xe2\x80\xa6"
                                                           : "Commands & skills");
        }
        ImGui::PopStyleColor();
        if (headerNote) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + headerTextW);
            ImGui::TextUnformatted(headerNote);
            ImGui::PopTextWrapPos();
            ImGui::PopStyleColor();
        }

        ImGui::BeginChild("##slashlist", ImVec2(-1, listH), ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoBackground);
        for (int i = 0; i < (int)s_slash.matches.size(); ++i) {
            const auto& it = s_slash.matches[(size_t)i];
            const bool sel = (i == s_slash.selected);
            const bool off = rowDisabled(it);
            if (slashMenuRow(it.icon, it.label.c_str(),
                             off ? kAvailableAfterRun : it.detail.c_str(), sel, i, off)) {
                accepted = true;
                acceptText = it.insert;
                s_slash.selected = i;
            }
            if (sel && scrolledByKey) ImGui::SetScrollHereY(0.35f);
            if (ImGui::IsItemHovered()) s_slash.selected = i;
        }
        if (s_slash.matches.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextUnformatted("No matches");
            ImGui::PopStyleColor();
        }
        ImGui::EndChild();

        // Glass backdrop behind content (same family as drawing-panel popups).
        {
            ImVec2 p0 = ImGui::GetWindowPos();
            ImVec2 p1(p0.x + ImGui::GetWindowSize().x, p0.y + ImGui::GetWindowSize().y);
            dl->ChannelsSetCurrent(0);
            // Thick glass: the menu floats over the transcript, so what is behind it must not
            // read through the command rows.
            ui::glassPanel(dl, ImRect(p0, p1), ui::GlassLevel::Thick,
                           ImGui::GetStyle().WindowRounding / sc, surf);
            // Subtle border so the menu reads against busy chat backdrops.
            dl->AddRect(p0, p1,
                        ImGui::ColorConvertFloat4ToU32(
                            ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.35f)),
                        ImGui::GetStyle().WindowRounding, 0, ui::metrics::stroke(1.0f));
            dl->ChannelsMerge();
        }
    }
    ImGui::End();
    ImGui::PopStyleColor(4);
    ImGui::PopStyleVar(3);

    // A bare Enter (or Tab) accepts the highlighted row; the turn is not submitted. A modified
    // Enter is the user asking for a line break, so it must fall through to the input instead
    // of silently completing a command they were done typing. With nothing picked (an optional
    // argument's options) Enter falls through and runs the bare command; Tab takes the first row.
    const ImGuiIO& sio = ImGui::GetIO();
    const bool enterPick = ImGui::IsKeyPressed(ImGuiKey_Enter, false) && !sio.KeyShift &&
                           !sio.KeyAlt && s_slash.selected >= 0;
    if (!accepted && !s_slash.matches.empty() &&
        (enterPick || ImGui::IsKeyPressed(ImGuiKey_Tab, false))) {
        const SlashItem& pick = s_slash.matches[(size_t)std::max(0, s_slash.selected)];
        if (rowDisabled(pick)) {
            st.toast(ui::ToastLevel::Info, std::string(pick.label) + ": " + kAvailableAfterRun);
        } else {
            accepted = true;
            acceptText = pick.insert;
        }
        ateEnter = true;
    }

    if (accepted && !acceptText.empty()) {
        applySlashInsert(st.prompt, acceptText);
        g_slashCaretToEnd = 1;  // park the caret after the inserted command
        s_slash.open = false;
        s_slash.matches.clear();
        g_slashMenuActive = false;
        return true;
    }
    return false;
}

// Shared failure path for the session slash commands: a run holding the service is a calm Info
// note, anything else an error in user terms. Returns true when `r` failed (already reported).
static bool slashRpcFailed(AppState& st, const core::RpcResult& r, const char* fallback) {
    if (core::workerLaneBusy(r)) {
        st.toast(ui::ToastLevel::Info, kAvailableAfterRun);
        return true;
    }
    if (r.ok) return false;
    st.toast(ui::ToastLevel::Error, userFacingRpcError(r, fallback));
    return true;
}

// /btw: a side question answered from the transcript so far. It never enters the transcript and
// never steers a running turn. A run holding the service is a calm, retryable panel state.
static void askBtw(AppState& st, const std::string& q) {
    s_btw.open = true;
    s_btw.loading = true;
    s_btw.busy = false;
    s_btw.question = q;
    s_btw.answer.clear();
    s_btw.error.clear();
    nlohmann::json params = nlohmann::json::object();
    params["messages"] = chatMsgsToWireMessages(st.chat);
    params["question"] = q;
    nlohmann::json pov = agentProviderOverride(st.settings);
    if (pov.contains("provider")) params["provider"] = pov["provider"];
    if (pov.contains("model")) params["model"] = pov["model"];
    else if (st.settings.model[0]) params["model"] = st.settings.model;
    st.rpc.call("session_btw", params, [](const core::RpcResult& r) {
        s_btw.loading = false;
        if (core::workerLaneBusy(r)) {
            s_btw.busy = true;
            s_btw.error = kAvailableAfterRun;
            return;
        }
        if (!r.ok) {
            s_btw.error = userFacingRpcError(r, "Couldn't answer that right now.");
            return;
        }
        if (r.result.is_object()) {
            if (r.result.contains("answer") && r.result["answer"].is_string())
                s_btw.answer = r.result["answer"].get<std::string>();
            else if (r.result.contains("text") && r.result["text"].is_string())
                s_btw.answer = r.result["text"].get<std::string>();
            else
                s_btw.error = userFacingRpcError(r, "No answer came back.");
        } else if (r.result.is_string()) {
            s_btw.answer = r.result.get<std::string>();
        } else {
            s_btw.error = "No answer came back.";
        }
    }, 180000);
}

// Returns true if `text` was a slash command (handled; do not run a normal turn).
static bool tryHandleSlashCommand(AppState& st, const std::string& text) {
    if (text.empty() || text[0] != '/') return false;
    // Split command + rest.
    std::string cmd, rest;
    {
        size_t sp = text.find_first_of(" \t\n");
        if (sp == std::string::npos) {
            cmd = text;
        } else {
            cmd = text.substr(0, sp);
            rest = text.substr(sp);
            while (!rest.empty() && (rest[0] == ' ' || rest[0] == '\t')) rest.erase(rest.begin());
        }
        for (char& c : cmd) c = (char)std::tolower((unsigned char)c);
    }

    auto needIdle = [&]() -> bool {
        if (aiTurnActive(st) || st.anyRunActive()) {
            st.toast(ui::ToastLevel::Info, cmd + ": " + kAvailableAfterRun);
            return false;
        }
        return true;
    };

    // An argument outside a command's options is refused by name, never replaced by the
    // default: a typo must not run a different compaction method than the one asked for.
    const SlashItem* spec = slashCommandFor(cmd);
    if (spec && !spec->args.empty() && !rest.empty() && !slashArgFor(*spec, slashLower(rest))) {
        st.toast(ui::ToastLevel::Warn, "Usage: " + slashUsage(*spec));
        return true;
    }

    if (cmd == "/prewalk") {
        ovSetAgentBool("prewalk", true);
        st.toast(ui::ToastLevel::Success, "Prewalk armed for the next run.");
        st.addLog("[..] /prewalk: agent.prewalk enabled");
        return true;
    }

    // /rule <trigger words> -> <context to inject>
    // Creates a live Rule (aicad.trigger/1) through the worker's single writer and jumps to
    // Settings -> AI -> Rules so the user lands on the row they just made. Both halves are
    // required server-side: a rule with no match could never fire.
    if (cmd == "/rule") {
        if (rest.find("->") == std::string::npos) {
            st.toast(ui::ToastLevel::Warn, "Usage: " + slashUsage(*spec));
            return true;
        }
        if (!needIdle()) return true;
        nlohmann::json params = nlohmann::json::object();
        params["text"] = "/rule " + rest;
        st.rpc.call("rule_create", params, [&st](const core::RpcResult& r) {
            if (core::workerLaneBusy(r)) {
                st.toast(ui::ToastLevel::Info, std::string("/rule: ") + kAvailableAfterRun);
                return;
            }
            if (!r.ok || !r.result.is_object()) {
                st.toast(ui::ToastLevel::Error,
                         userFacingRpcError(r, "Couldn't create the rule."));
                return;
            }
            if (!r.result.value("ok", false)) {
                std::string why = userFacingRpcError(r, "Couldn't create the rule.");
                const std::string hint = r.result.value("hint", std::string());
                if (!hint.empty()) why += "  \xc2\xb7  " + hint;
                st.toast(ui::ToastLevel::Warn, why);
                return;
            }
            const std::string id = r.result.value("id", std::string());
            const std::string kw = r.result.value("keyword", std::string());
            pushSessionNotice(st, "Rule \"" + id + "\" created \xe2\x80\x94 fires when \"" + kw +
                                      "\" appears. Edit it in Settings \xc2\xb7 AI \xc2\xb7 Rules.");
            st.addLog("[..] /rule: created rule '" + id + "'");
            st.openSettings = true;
            st.openSettingsTab = 6;  // AI tab, Rules sub-tab
        }, 20000);
        st.toast(ui::ToastLevel::Info, "Creating rule\xe2\x80\xa6");
        return true;
    }

    if (cmd == "/btw") {
        if (rest.empty()) {
            st.toast(ui::ToastLevel::Warn, "Usage: " + slashUsage(*spec));
            return true;
        }
        askBtw(st, rest);
        return true;
    }

    if (cmd == "/compact") {
        if (!needIdle()) return true;
        // No argument = the Compaction setting (Settings > AI). An explicit argument (one of
        // the options, checked above) is sent as-is; the worker refuses one the session
        // cannot honour rather than compacting with a different method.
        const std::string strategy = slashLower(rest);
        nlohmann::json params = nlohmann::json::object();
        params["messages"] = chatMsgsToWireMessages(st.chat);
        if (!strategy.empty()) params["strategy"] = strategy;
        // Honor Agents-tab agent.compaction.* (defaults match shared contract).
        int keepRecent = 20000;
        bool supersedeReads = true;
        bool dropUseless = true;
        try {
            std::ifstream in(configPath());
            if (in) {
                nlohmann::json j;
                in >> j;
                if (j.is_object()) {
                    auto agent = j.value("agent", nlohmann::json::object());
                    if (agent.is_object()) {
                        auto comp = agent.value("compaction", nlohmann::json::object());
                        if (comp.is_object()) {
                            if (comp.contains("keep_recent_tokens") &&
                                comp["keep_recent_tokens"].is_number_integer())
                                keepRecent = comp["keep_recent_tokens"].get<int>();
                            if (comp.contains("supersede_reads") &&
                                comp["supersede_reads"].is_boolean())
                                supersedeReads = comp["supersede_reads"].get<bool>();
                            if (comp.contains("drop_useless") &&
                                comp["drop_useless"].is_boolean())
                                dropUseless = comp["drop_useless"].get<bool>();
                        }
                    }
                }
            }
        } catch (...) {}
        params["keep_recent_tokens"] = keepRecent;
        params["supersede_reads"] = supersedeReads;
        params["drop_useless"] = dropUseless;
        // Anchor the compaction to this conversation's session: the Chudcode host owns the
        // service-side transcript, so session_id (= conversation id, same value
        // agent_admin_turn rides) is what lets an online compact rewrite the session it
        // actually belongs to. Provider/model overrides pin summary rendering.
        {
            nlohmann::json pov = agentProviderOverride(st.settings);
            for (auto& el : pov.items()) params[el.key()] = el.value();
            if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size() &&
                !st.conversations[st.activeConv].id.empty()) {
#ifdef __EMSCRIPTEN__
                params["session_id"] = cellAgentSessionHandle(st.conversations[st.activeConv].id);
#else
                params["session_id"] = st.conversations[st.activeConv].id;
#endif
            }
        }
        st.rpc.call("session_compact", params, [&st](const core::RpcResult& r) {
            if (slashRpcFailed(st, r, "Couldn't compact this session.")) return;
            if (!r.result.is_object()) {
                st.toast(ui::ToastLevel::Error, "Couldn't compact this session.");
                return;
            }
            // Replace the visible chat ONLY when the result carries an explicit nonempty
            // replacement payload (ui::chudcodeCompactReplacement: rebuilt "messages"
            // first, legacy bare "kept_messages" tail second). Chudcode online compaction
            // rewrites the service-side session and returns NEITHER — the visible chat
            // must stay intact then, and a present-but-empty array must never wipe it.
            if (const nlohmann::json* repl = ui::chudcodeCompactReplacement(r.result))
                applyWireMessagesToChat(st, *repl);
            std::string notice;
            if (r.result.contains("short_summary") && r.result["short_summary"].is_string())
                notice = r.result["short_summary"].get<std::string>();
            else if (r.result.contains("summary") && r.result["summary"].is_string())
                notice = r.result["summary"].get<std::string>();
            // Name only the method the agent service reports it used (the runtime's
            // compaction entry), never the requested or configured one.
            const auto methodIt = r.result.find("method");
            const std::string method = (methodIt != r.result.end() && methodIt->is_string())
                                           ? methodIt->get<std::string>()
                                           : std::string();
            std::string methodLabel = method;
            if (method == "soft") methodLabel = "summary";
            else if (method == "remote") methodLabel = "provider-native compaction";
            if (notice.empty())
                notice = methodLabel.empty() ? "Compacted." : "Compacted (" + methodLabel + ").";
            pushSessionNotice(st, notice);
            st.addLog("[..] /compact ok" + (methodLabel.empty() ? std::string()
                                                                : " (" + methodLabel + ")"));
        }, 180000);
        st.toast(ui::ToastLevel::Info, "Compacting...");
        return true;
    }

    if (cmd == "/shake") {
        if (!needIdle()) return true;
        const std::string mode = rest.empty() ? std::string("elide") : slashLower(rest);
        bool aggressive = true;
        nlohmann::json params = nlohmann::json::object();
        params["messages"] = chatMsgsToWireMessages(st.chat);
        params["mode"] = mode;
        params["aggressive"] = aggressive;
        st.rpc.call("session_shake", params, [&st, mode](const core::RpcResult& r) {
            if (slashRpcFailed(st, r, "Couldn't shake this session.")) return;
            if (!r.result.is_object()) {
                st.toast(ui::ToastLevel::Error, "Couldn't shake this session.");
                return;
            }
            // Same replacement guard as /compact: only a nonempty explicit payload may
            // replace the visible transcript.
            if (const nlohmann::json* repl = ui::chudcodeCompactReplacement(r.result))
                applyWireMessagesToChat(st, *repl);
            std::string notice;
            if (r.result.contains("notice") && r.result["notice"].is_string())
                notice = r.result["notice"].get<std::string>();
            int saved = 0;
            if (r.result.contains("saved_tokens") && r.result["saved_tokens"].is_number_integer())
                saved = r.result["saved_tokens"].get<int>();
            if (notice.empty()) {
                notice = "Shook (" + mode + ")";
                if (saved > 0) notice += " ~" + std::to_string(saved) + " tokens";
                notice += ".";
            }
            pushSessionNotice(st, notice);
            st.addLog("[..] /shake " + mode + " ok");
        }, 120000);
        st.toast(ui::ToastLevel::Info, "Shaking...");
        return true;
    }

    if (cmd == "/handoff") {
        if (!needIdle()) return true;
        if (st.chat.size() < 2) {
            st.toast(ui::ToastLevel::Warn, "Need at least two messages to hand off.");
            return true;
        }
        nlohmann::json params = nlohmann::json::object();
        params["messages"] = chatMsgsToWireMessages(st.chat);
        if (!rest.empty()) params["focus"] = rest;
        nlohmann::json pov = agentProviderOverride(st.settings);
        if (pov.contains("provider")) params["provider"] = pov["provider"];
        if (pov.contains("model")) params["model"] = pov["model"];
        else if (st.settings.model[0]) params["model"] = st.settings.model;
        st.rpc.call("session_handoff", params, [&st](const core::RpcResult& r) {
            if (slashRpcFailed(st, r, "Couldn't generate the handoff.")) return;
            std::string doc;
            if (r.result.is_object()) {
                if (r.result.contains("document") && r.result["document"].is_string())
                    doc = r.result["document"].get<std::string>();
                else if (r.result.contains("text") && r.result["text"].is_string())
                    doc = r.result["text"].get<std::string>();
            } else if (r.result.is_string()) {
                doc = r.result.get<std::string>();
            }
            if (doc.empty()) {
                st.toast(ui::ToastLevel::Error, "Handoff returned empty document.");
                return;
            }
            // New session/tab; old tab transcript left intact.
            ensureConversations(st);
            parkActiveConversation(st);
            Conversation c;
            c.id = newConvId(st);
            c.title = "Handoff";
            c.projectDir = convProjectDir(st);  // project scope, captured once at creation
            st.conversations.push_back(std::move(c));
            st.activeConv = (int)st.conversations.size() - 1;
            st.chat.clear();
            st.lastContextTokens = 0;
            st.queuedPrompts.clear();
            st.queuedPromptDrafts.clear();
            // Visible handoff seed wrapping the document.
            ChatMsg seed;
            seed.fromUser = false;
            seed.model = "handoff";
            seed.text =
                "<handoff-context>\n" + doc +
                "\n</handoff-context>\n\n"
                "The above is a handoff document from a previous session. "
                "Use this context to continue the work seamlessly.";
            st.chat.push_back(std::move(seed));
            appendChatRawMessage(st, st.chat.back());
            persistChatHistory(st);
            st.toast(ui::ToastLevel::Success, "New session started with handoff context");
            st.addLog("[..] /handoff: new session seeded");
        }, 300000);
        st.toast(ui::ToastLevel::Info, "Generating handoff...");
        return true;
    }

    // Unknown /command — fall through so the user can still send "/something" as plain text
    // only when it is not one of our known commands. Known-prefix typos get a usage hint.
    if (cmd == "/help") {
        std::string help;
        for (const auto& it : kSessionSlashItems) {
            if (!help.empty()) help += "  ";
            help += slashUsage(it);
        }
        st.toast(ui::ToastLevel::Info, help + "  (type / for autocomplete)");
        return true;
    }
    if (cmd == "/skills") {
        return tryHandleSkillsListCommand(st);
    }
    return false;  // unknown slash — send as normal text
}

void drawBtwPanel(AppState& st) {
    if (!s_btw.open) return;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::SetNextWindowSize(ImVec2(360.0f * sc, 280.0f * sc), ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowBgAlpha(0.94f);
    bool open = s_btw.open;
    if (ImGui::Begin("By the way", &open,
                     ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoDocking)) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("Q: %s", s_btw.question.c_str());
        ImGui::PopStyleColor();
        ImGui::Separator();
        if (s_btw.loading) {
            ImGui::TextUnformatted("Thinking...");
        } else if (s_btw.busy) {
            // Refused only because a run holds the service: calm copy, and the question is
            // kept so one click asks it again once the run finishes.
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::TextWrapped("%s", s_btw.error.c_str());
            ImGui::PopStyleColor();
        } else if (!s_btw.error.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
            ImGui::TextWrapped("%s", s_btw.error.c_str());
            ImGui::PopStyleColor();
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
            ImGui::TextWrapped("%s",
                s_btw.answer.empty() ? "(no answer)" : s_btw.answer.c_str());
            ImGui::PopStyleColor();
        }
        if (!s_btw.loading && !s_btw.error.empty()) {
            const bool runBusy = aiTurnActive(st) || st.anyRunActive();
            if (ui::kit::secondaryButton("Ask again", !(s_btw.busy && runBusy),
                                         kAvailableAfterRun, ImVec2(-1, 0))) {
                const std::string q = s_btw.question;
                askBtw(st, q);
            }
        }
        if (ImGui::Button("Close", ImVec2(-1, 0))) open = false;
    }
    ImGui::End();
    s_btw.open = open;
}

// Launch the active turn for `text`: ONE Chudcode general_agent turn in the mode the USER picked
// in the composer (Agent | Plan | Ask). The host never inspects the request text — no intent
// sniffing, no auto-routing, no mode promotion, no CFD hijack: the agent decides for itself
// whether to answer inline, spawn subagents or run a mission, with the tool policy its mode
// implies. Pushes the user + live-assistant ChatMsgs, then hands off to launchAgentTurn.
void submitTurn(AppState& st, const std::string& text) {
    if (text.empty() && st.pendingAttachments.empty() && st.pendingContextRefs.empty()) return;
    // A fresh turn always clears a lingering stop latch (e.g. worker never drained after cancel).
    st.activeRun()->stopRequested = false;
    // Slash commands: /compact /shake /handoff /btw /prewalk /skills /help — pure host+RPC,
    // no agent turn. Skills themselves (/skill:<name>) ride a normal turn and activate in-agent.
    if (tryHandleSlashCommand(st, text)) return;
#ifndef __EMSCRIPTEN__
    if (!st.verifyEntitlementForAi()) return;
#endif
    // ONE live run per conversation. The composer normally queues a send while a turn is in
    // flight (drain at the top of drawComposer), so this only catches an out-of-band submit.
    if (st.convRunning(st.activeConvId())) {
        st.toast(ui::ToastLevel::Warn,
                 "A run is still in progress - ask me again when it finishes.");
        return;
    }
    // The composer mode is a user choice; normalize a legacy persisted value once, here, so the
    // bubble stamps, the persisted setting and the worker payload all agree.
    const int mode = normalizeCopilotMode(st.aiInteraction);
    st.aiInteraction = mode;
    st.settings.copilotMode = mode;
    // Persist the selected descriptor before launching, then pin provider/model/effort explicitly
    // in the request below. The long-lived worker therefore receives exactly what the picker
    // shows instead of re-resolving a stale endpoint or an ambient CHUDWARE_PROVIDER override.
    saveSettings(st.settings);
    // M23: fold pending attachments into THIS turn. `sendText` (what the model receives) gets
    // inlined text-file content / explicit paths appended; the chat shows a compact marker.
    // Wave4: context_refs ride as structured payload only: the online worker resolves each
    // against the engine and renders it into the prompt; the explicit offline core route gets
    // them inlined by buildAgentJobRpcRoute. The bubble shows them as chips (ChatMsg).
    // Nothing else is prepended: the persistent Chudcode session already holds the conversation
    // (options.history only seeds a fresh one), so the request is the user's own words.
    std::string sendText = text;
    std::string bubbleText;
    const nlohmann::json attachments = consumeComposerAttachments(st, sendText, &bubbleText);
    std::vector<core::ContextRef> contextRefs = consumeContextRefs(st);
    // Plan edits made while idle ride with the next turn (agent-facing only, never the bubble).
    if (std::string planNote = planBoardTakeEditsForTurn(st, st.activeConvId());
        !planNote.empty())
        sendText += "\n\n" + planNote;
    nlohmann::json extra = nlohmann::json::object();
    if (!attachments.empty()) extra["attachments"] = attachments;
    if (!contextRefs.empty()) extra["context_refs"] = core::contextRefsToJson(contextRefs);
    {
        ChatMsg u; u.fromUser = true;
        u.text = userBubbleText(bubbleText, attachments);
        u.imagePaths = userImagePaths(attachments);
        u.contextRefs = std::move(contextRefs);
        u.copilotMode = mode;  // stamp composer mode: tints the sent bubble
        st.chat.push_back(std::move(u));
        appendChatRawMessage(st, st.chat.back());
    }
    {
        ChatMsg a;
        a.running = true;
        a.copilotMode = mode;
        if (st.settings.model[0]) a.model = st.settings.model;
        // Instant chrome — don't wait for the first drained event frame. The agent is thinking;
        // there is no host-side routing or classification step to narrate.
        a.liveStatus = "Thinking\xe2\x80\xa6";
        st.chat.push_back(a);
        appendChatRawMessage(st, st.chat.back());
    }
    st.designStarted = true;   // conversation is now active (kept for shot scenarios)
    // Sending a message is intent to follow the reply: re-engage stick-to-bottom on every
    // chat surface even if the user had scrolled up into the history.
    chatLogResetStick();

    // Persist so the turn survives a restart; persistChatHistory also titles a session from its
    // first prompt (autoTitleConversation) unless the user renamed it.
    ui::persistChatHistory(st);

    // The turn's "current editor context", sent in EVERY mode whenever a document is open (the
    // CAD analogue of the active file in a code editor): the open doc, the feature the
    // PropertyManager/tree has focused, and the viewport selection. Python renders it into the
    // prompt; it is context, never an instruction to edit those entities.
    auto buildTurnScope = [&]() -> nlohmann::json {
        nlohmann::json scope = nlohmann::json::object();
        scope["doc_id"] = st.docMirror.docId();
        nlohmann::json fids = nlohmann::json::array();
        if (st.propertyManager && st.propertyManager->active() &&
            !st.propertyManager->targetId().empty()) {
            fids.push_back(st.propertyManager->targetId());
        } else if (!st.selectedFeatureId.empty()) {
            fids.push_back(st.selectedFeatureId);
        }
        if (!fids.empty()) scope["feature_ids"] = fids;
        nlohmann::json sels = nlohmann::json::array();
        for (const auto& it : st.selection.items()) {
            if (!it.selector.empty()) {
                try {
                    sels.push_back(nlohmann::json::parse(it.selector));
                } catch (...) {
                    sels.push_back(it.selector);
                }
            }
        }
        if (!sels.empty()) scope["selection"] = sels;
        return scope;
    };

    nlohmann::json opts = {{"mode", copilotModeName(mode)},
                           {"advisor", true},
                           {"corporate_mode", st.settings.corporateMode}};
    if (st.docMirror.hasDoc()) opts["scope"] = buildTurnScope();
    attachAgentConversationOptions(st, opts);
    extra["options"] = std::move(opts);

    st.status = mode == (int)CopilotMode::Plan ? "Planning..."
              : mode == (int)CopilotMode::Ask  ? "Answering..."
                                               : "Working...";
    st.statusKind = 0;
    launchAgentTurn(st, sendText, std::move(extra));
}

// ---- runtime-not-ready banner (kept from the old Design body) ----
void drawRuntimeBanner(AppState& st) {
    if (st.workerStarted) return;
    // The in-process core remains sufficient for documents, simulation, and the explicit offline
    // modeling fallback, so a loaded core does not need the Python-runtime setup banner.
    if (core::cpp_core().available()) return;
    const auto& pal = ui::palette;
    ImGui::PushStyleColor(ImGuiCol_ChildBg,
        ImVec4(pal.setupWarnBg[0], pal.setupWarnBg[1], pal.setupWarnBg[2], pal.setupWarnBg[3]));
    ImGui::BeginChild("setup", ImVec2(0, 0), ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
    if (st.setupRunning.load()) {
        ImGui::TextColored(ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1),
                           "Setting up runtime...");
        dimText("First run installs the local CAD engine runtime (~once).");
    } else {
        ImGui::TextColored(ImVec4(pal.statusWarn[0], pal.statusWarn[1], pal.statusWarn[2], 1),
                           "Runtime not ready");
#if defined(_WIN32)
        dimText("Run the packaged build (dist) or install.ps1 so the engine can self-install.");
#elif defined(__APPLE__)
        dimText("Place libchudware.dylib next to the Chudware executable, or set CHUDWARE_LIBCHUDWARE.");
#else
        dimText("Place libchudware.so next to the Chudware executable, or set CHUDWARE_LIBCHUDWARE.");
#endif
        if (ImGui::Button("Try setup now")) st.startSetup(false);
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 4 * ui::uiScale));
}

// ---- composer model chip ---------------------------------------------------------------------
// The chip identifies the MODEL; the provider is carried by its brand mark. The old
// "Anthropic \xc2\xb7 claude-sonnet-5" text spent the widest segment on a prefix the mark already
// states and then ellipsized away the only part that mattered (the model).

// Provider display name — TOOLTIP ONLY; the chip itself never shows it.
static const char* composerProviderName(int provider) {
    // Keep in lockstep with ui::ProviderIndex (Anthropic..FreeAI).
    static const char* kProvNames[] = {
        "Anthropic", "OpenAI", "Local", "llama.cpp", "Gemini", "Cloud API", "xAI", "Cursor",
        "Kimi Code", "ZenMux", "Free AI"
    };
    static_assert(sizeof(kProvNames) / sizeof(kProvNames[0]) == ui::PROV_COUNT,
                  "composerProviderName kProvNames must match PROV_COUNT");
    return kProvNames[(provider >= 0 && provider < ui::PROV_COUNT) ? provider : ui::PROV_GEMINI];
}

// Provider id for the brand mark. This is the id space the Settings -> Providers rows hand to
// drawProviderLogo, so the chip shows the same art. It differs from local runtime wire names.
static std::string composerProviderLogoId(const Settings& s) {
    switch (s.provider) {
        case ui::PROV_ANTHROPIC:   return "anthropic";
        case ui::PROV_OPENAI:      return "openai";
        case ui::PROV_GEMINI:      return "google";
        case ui::PROV_XAI:         return "xai";
        case ui::PROV_CURSOR:      return "cursor";
        case ui::PROV_KIMISUB:     return "kimi-code";
        case ui::PROV_ZENMUX:      return "zenmux";
        case ui::PROV_CLOUDCOMPAT: return ui::cloudServiceKey(s.baseUrl);
        default:                   return ui::providerCatKey(s.provider);  // local: generic badge
    }
}

// The model's real display name ("Claude Sonnet 5"). Resolution order:
//   1. cached Chudcode catalog metadata,
//   2. static builtinModels() fallback,
//   3. shortModelName, then the raw id.
// Never carries the provider — the brand mark says that.
static std::string composerModelName(const Settings& s) {
    if (!s.model[0]) return "default";
    if (const ui::BridgeModelMeta* m = ui::bridgeModelMetaForCad(s.provider, s.model, s.baseUrl))
        if (!m->name.empty()) return m->name;
    for (const ui::ModelInfo& mi : ui::builtinModels())
        if (mi.provider == s.provider && mi.id == s.model && !mi.label.empty())
            return mi.label;
    const std::string shortId = ui::shortModelName(s.model);
    return shortId.empty() ? std::string(s.model) : shortId;
}

// ---- composer gauges ---------------------------------------------------------------------
// ONE ring geometry for every gauge in the prelude row (context window + provider limits) so
// they read as a set, and so each measure() can never drift from its draw().
static float gaugeRingRadius()    { return ImGui::GetFrameHeight() * 0.30f; }
static float gaugeRingThickness() { return ui::metrics::stroke(2.0f); }
static float gaugeRingAdvance()   { return gaugeRingRadius() * 2.0f + gaugeRingThickness(); }

// Shared track tone: the surface's own faint ink, so an unfilled ring reads on dark AND light
// glass without a hardcoded gray.
static ImU32 gaugeTrackColor(const ui::AdaptiveSurface& surf) {
    ImVec4 c = surf.textTertiary;
    c.w = 0.35f;
    return ImGui::ColorConvertFloat4ToU32(c);
}

// Provenance of st.lastContextWindow — the `window_source` key the harness now stamps on every
// agent.context event ("user" | "catalog" | "probe" | "heuristic" | ""). Deliberately a
// file-scoped global instead of a new AppState field: st.lastContextWindow is itself ONE global
// value feeding ONE global gauge, so an annotation of it is exactly as scoped as the thing it
// annotates and app_state.h stays untouched. Written by the event drain in host/src/app.cpp
// through ui::setContextWindowSource() (forward-declared at that call site). This whole file is
// already inside `namespace ui` (opened above), so the definition needs no wrapper.
static std::string g_ctxWindowSource;
void setContextWindowSource(const std::string& src) { g_ctxWindowSource = src; }
// "" and "heuristic" both mean "a rough per-provider default the harness fell back to" — the
// least trusted answer there is, and NOT something that may outrank curated catalog data.
static bool ctxWindowSourceTrusted() {
    return !g_ctxWindowSource.empty() && g_ctxWindowSource != "heuristic";
}

// The window the context meter reports, in strict precedence:
//   1. settings.contextWindow — the user typed a number into AI settings. An OVERRIDE means
//      exactly that: it beats even the server, because the reason to set it is that the
//      catalog/provider is reporting a window you do not believe (a local model behind an
//      OpenAI-compatible shim habitually lies about its own capacity).
//   2. st.lastContextWindow, but ONLY when agent.context said where it came from and that source
//      is a real answer ("user"/"catalog"/"probe"). A sourced server value describes the model
//      that ACTUALLY ran, which Settings cannot know: the picker may have been changed
//      mid-session, and an auto-route may have run a different model entirely.
//   3. The model catalog row for the selected model (the live fetch, or the copy persisted from
//      the last session — see settings.cpp, so a restart knows the real window from frame one).
//   4. effectiveContextWindow()'s id-pattern estimate, then st.lastContextWindow even when its
//      source is heuristic/unstated. Both are GUESSES: `known` is false, and the meter then
//      shows the token count alone — never a denominator, fraction ring or over-limit red built
//      on a family default (that is what painted "145k/128k" red before the catalog landed).
//
// A heuristic provider default must lose to model-specific catalog metadata. Otherwise the
// first provider call can downgrade a correct model window to a generic family estimate.
// tokens 0 = genuinely unknown, never a real capacity.
struct CtxWindow {
    int  tokens = 0;
    bool known  = false;   // false = a guess; the meter must not show a fraction of it
};
static CtxWindow contextMeterWindow(const AppState& st) {
    if (st.settings.contextWindow > 0) return {st.settings.contextWindow, true};
    if (st.lastContextWindow > 0 && ctxWindowSourceTrusted()) return {st.lastContextWindow, true};
    if (const ui::BridgeModelMeta* bm = ui::bridgeModelMetaForCad(
            st.settings.provider, st.settings.model, st.settings.baseUrl);
        bm && bm->contextWindow > 0)
        return {bm->contextWindow, true};
    if (const int eff = effectiveContextWindow(st.settings); eff > 0) return {eff, false};
    return {st.lastContextWindow > 0 ? st.lastContextWindow : 0, false};
}

// ---- precomposed system-prompt baseline ------------------------------------------------------
// A fresh session has no provider measurement AND an empty transcript, so the meter used to show
// "\xe2\x80\x94/1.0M" and answer nothing — while the first request in fact already carries a
// multi-thousand-token system prompt (role card, harness protocol, role catalog, project files,
// standing guidance, tool schemas, skills catalog). services/agent/prompt_cache precomposes those
// bytes and reports their token cost per BLOCK; `ai_prompt_baseline` returns it.
//
// Fetched through the async rpc queue, never synchronously: this is reached from a per-frame draw
// path, and a synchronous worker.call there re-hits a failing worker every frame from the GL
// thread (that pattern hard-froze the app once — see cam_panel.cpp refreshDialects). Guards: an
// in-flight flag, a failure cooldown, and a staleness timer so a settings change that recomposes
// the prompt is picked up without polling.
struct PromptBaselineSeg {
    std::string title;
    int         tokens = 0;
};
struct PromptBaselineState {
    bool                          loaded   = false;
    bool                          inflight = false;
    double                        nextTry  = 0.0;
    double                        staleAt  = 0.0;
    int                           tokens   = 0;
    int                           toolCount = 0;
    std::vector<PromptBaselineSeg> segments;
};
static PromptBaselineState g_promptBaseline;

static constexpr double kPromptBaselineTtlS   = 60.0;
static constexpr double kPromptBaselineRetryS = 15.0;

static void refreshPromptBaseline(AppState& st) {
    if (!startupComposerMetadataReady(st)) return;
    const double now = ImGui::GetTime();
    if (g_promptBaseline.inflight) return;
    if (g_promptBaseline.loaded && now < g_promptBaseline.staleAt) return;
    if (now < g_promptBaseline.nextTry) return;
    g_promptBaseline.inflight = true;
    st.rpc.call("ai_prompt_baseline", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        g_promptBaseline.inflight = false;
        const double t = ImGui::GetTime();
        if (!r.ok || !r.result.value("ok", false)) {
            g_promptBaseline.nextTry = t + kPromptBaselineRetryS;
            return;
        }
        g_promptBaseline.tokens    = r.result.value("tokens", 0);
        g_promptBaseline.toolCount = r.result.value("allowed_tool_count", 0);
        g_promptBaseline.segments.clear();
        if (r.result.contains("segments") && r.result["segments"].is_array()) {
            for (const auto& s : r.result["segments"]) {
                PromptBaselineSeg seg;
                seg.title  = s.value("title", std::string());
                seg.tokens = s.value("tokens", 0);
                if (seg.tokens > 0 && !seg.title.empty())
                    g_promptBaseline.segments.push_back(std::move(seg));
            }
        }
        g_promptBaseline.loaded  = g_promptBaseline.tokens > 0;
        g_promptBaseline.staleAt = t + kPromptBaselineTtlS;
        if (!g_promptBaseline.loaded) g_promptBaseline.nextTry = t + kPromptBaselineRetryS;
    });
}

// Model-visible payload carried by one message's activity timeline, in characters.
//
// INCLUDED: `detail` (the model's own raw output/reasoning for the step, and the tool
// result/thought body — by far the bulk of it), `title` (rowKind 3 stores the tool NAME here) and
// `reason` (rowKind 3 stores the tool's target, rowKind 1 the injected-context source). Those
// three are the strings that correspond to something the provider actually saw.
// EXCLUDED: `stage` (a raw internal key like "intake"/"review" used only for row matching) and
// `status` ("running"/"pass"/"fail" — a GUI verdict token the model never receives). Both are
// bounded by a handful of characters anyway, so including them would add noise, not accuracy.
static int activityCharsOf(const ChatMsg& m) {
    size_t n = 0;
    for (const ActivityItem& a : m.activity)
        n += a.detail.size() + a.title.size() + a.reason.size();
    return n > (size_t)INT_MAX ? INT_MAX : (int)n;
}

// The estimated size of the transcript the user is LOOKING AT, i.e. what continuing this session
// would cost to resend. st.chat is the active conversation's live message vector (every other
// resident conversation parks its msgs; see conversations.cpp) — the same source
// chatHistoryForAgent() sends — so this needs no second accessor.
//
// Error cards are skipped for the same reason chatHistoryForAgent() skips them: they are GUI
// failure notices, not conversation content, and are not resent.
//
// Memoised on (message count, length of the last message's text, active conversation id) so a
// streaming turn and a switched session both recompute, but an idle frame over a long transcript
// does NOT rescan it. Called from the per-frame draw path, so a full scan every frame is the
// thing being avoided here.
static const ui::ContextEstimate& viewedTranscriptEstimate(const AppState& st) {
    struct Memo {
        bool                valid = false;
        size_t              count = 0, lastLen = 0;
        std::string         convId;
        ui::ContextEstimate est;
    };
    static Memo memo;
    const std::string convId =
        (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size())
            ? st.conversations[st.activeConv].id
            : std::string();
    const size_t count   = st.chat.size();
    const size_t lastLen = count ? st.chat.back().text.size() : 0;
    if (memo.valid && memo.count == count && memo.lastLen == lastLen && memo.convId == convId)
        return memo.est;
    std::vector<ui::TranscriptEntry> entries;
    entries.reserve(count);
    for (const ChatMsg& m : st.chat) {
        if (m.errKind != ERR_NONE) continue;
        const int actChars = activityCharsOf(m);
        if (m.text.empty() && actChars == 0) continue;
        entries.push_back(ui::TranscriptEntry{m.fromUser, m.text, actChars});
    }
    memo.valid   = true;
    memo.count   = count;
    memo.lastLen = lastLen;
    memo.convId  = convId;
    memo.est     = ui::estimateEntries(entries);
    return memo.est;
}

// The numerator the meter shows, and what kind of number it is. A MEASURED call always wins; the
// transcript estimate exists precisely for the case there has been no call this process (a
// session restored from disk), which is the state that used to render a bare em dash.
struct CtxMeterUsage {
    int  tokens   = 0;      // 0 = nothing to show; TOTAL (baseline + conversation)
    bool estimate = false;  // true = derived from the transcript, MUST be marked on screen
    int  messages = 0;      // estimate only: how many messages it counted
    bool clipped  = false;  // estimate only: a per-message cap bit, so `tokens` is a lower bound
    int  baseline = 0;      // the system-prompt portion of `tokens`, 0 when not known yet
};
static CtxMeterUsage contextMeterUsage(const AppState& st) {
    // A MEASURED prompt_tokens figure ALREADY CONTAINS the system prompt, so the baseline must be
    // shown as a portion OF it, never added to it — adding would double-count the largest single
    // block and overstate the window every turn.
    const int base = g_promptBaseline.loaded ? g_promptBaseline.tokens : 0;
    if (st.lastContextTokens > 0)
        return {st.lastContextTokens, false, 0, false,
                std::min(base, st.lastContextTokens)};
    // Nothing measured: the system prompt is still genuinely spent on the next call, so it is
    // added to the transcript estimate. This is what replaces the em dash on a fresh session.
    const ui::ContextEstimate& e = viewedTranscriptEstimate(st);
    const int total = e.tokens + base;
    return {total, total > 0, e.messages, e.truncated, base};
}

// The meter's readout string + whether a ring is meaningful, shared by drawContextMeter() and
// measureContextMeter() so the reserved width can never disagree with the drawn one. Four honest
// states, none of which ever prints a zero denominator or a fabricated figure:
//   measured, win > 0  -> "12k/200k" with a filled fraction ring
//   estimated, win > 0 -> "~8k/200k" — the tilde is the on-screen mark that this came from
//                         counting the transcript, not from the provider. ASCII deliberately:
//                         U+2248 is NOT in the UI font's glyph ranges and rendered as a literal
//                         "?", which turned the estimate into a broken-looking "?978/262k".
//   empty transcript   -> "\xe2\x80\x94/200k": nothing measured AND nothing to count, so the
//                         numerator is an em dash and the ring sits empty — a pure capacity hint
//   win == 0           -> "ctx \xe2\x80\x94" with NO ring: the window is unknown, so there is no
//                         fraction to show at all
//   win is a guess     -> "ctx 145k" with NO ring and no denominator: an id-pattern estimate
//                         is not a capacity, so the meter stays neutral (never over-limit red)
//                         until the catalog or the provider reports the real window
struct CtxMeterText {
    std::string full;      // ring-adjacent readout (or the bare unknown mark)
    bool        ringOk;    // false = window unknown; draw the compact tag only, never a ring
};
static CtxMeterText contextMeterText(const AppState& st) {
    const CtxWindow win = contextMeterWindow(st);
    if (win.tokens <= 0) return {std::string("\xe2\x80\x94"), false};
    const CtxMeterUsage u = contextMeterUsage(st);
    std::string num = u.tokens <= 0  ? std::string("\xe2\x80\x94")
                      : u.estimate   ? "~" + fmtTokens(u.tokens)
                                     : fmtTokens(u.tokens);
    if (!win.known) return {num, false};
    return {num + "/" + fmtTokens(win.tokens), true};
}

// Context-window gauge for the composer prelude row: a fraction ring + "N/M" readout (compact
// "ctx N/M" with no ring when the pane is too narrow, or when the window itself is unknown).
// Drawn at the current cursor; advances it.
// Moved here from the footer chips, where it was pinned to the pane bottom against the status bar.
static void drawContextMeter(AppState& st, float roomForMeter) {
    refreshPromptBaseline(st);   // async, guarded; never a synchronous call on a draw path
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();
    const CtxWindow winInfo = contextMeterWindow(st);
    // A guessed window never drives a fraction (ring, tooltip bar or percentage): only a real
    // capacity may tell the user they are near or over the limit.
    const int   win  = winInfo.known ? winInfo.tokens : 0;
    const CtxMeterUsage usage = contextMeterUsage(st);
    const int   used = usage.tokens;   // measured OR estimated; the ring follows what is SHOWN
    const float frac = (win > 0 && used > 0) ? std::min(1.0f, (float)used / (float)win) : 0.0f;
    // The system-prompt baseline is a DIFFERENT kind of number from the conversation on top of it:
    // it is fixed, it is spent before the user types anything, and it is not something continuing
    // the chat grows. Drawing it as its own dimmed arc keeps "what I cannot avoid" visually apart
    // from "what this conversation has cost", instead of summing both into one indistinct sweep.
    const float baseFrac = (win > 0 && usage.baseline > 0)
                               ? std::min(frac, (float)usage.baseline / (float)win)
                               : 0.0f;
    const float gap = ui::metrics::px(5), ringAdv = gaugeRingAdvance();
    const CtxMeterText txt = contextMeterText(st);
    const std::string& full = txt.full;
    // The ringless form only earns its "ctx" tag when the WINDOW is unknown ("ctx —" would
    // otherwise be a bare em dash). Dropping the ring for lack of ROOM must never swap in a
    // WIDER string than the one that did not fit — that is what pushed "23k/262k" past the
    // column's right edge and clipped the trailing glyph.
    const std::string lean = txt.ringOk ? full : ("ctx " + full);
    const float fh = ImGui::GetFrameHeight();
    // Ring + readout only when the window is known AND there is room for both; uses the SAME
    // roomForMeter the caller passed to measureContextMeter() so the width it reserved and the
    // layout drawn here agree exactly.
    const float fullTw = ImGui::CalcTextSize(full.c_str()).x;
    const bool  showRing = txt.ringOk && roomForMeter >= ringAdv + gap + fullTw;
    ImGui::BeginGroup();   // group so hovering the RING (not just the digits) raises the tooltip
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    if (showRing) {
        const ImVec2 p = ImGui::GetCursorScreenPos();
        // Same thresholds usageStatusColor() uses, so every gauge agrees on when amber turns red.
        const float* role = frac < ui::kUsageWarnFrac ? pal.statusOk
                          : frac < ui::kUsageCritFrac ? pal.statusWarn
                                                      : pal.statusErr;
        const ImU32 convCol = ImGui::ColorConvertFloat4ToU32(semanticOnGlass(role, surf));
        ImVec4 baseC = ImGui::ColorConvertU32ToFloat4(convCol);
        baseC.w *= 0.42f;    // same hue, clearly recessive: one gauge, two readable layers
        const ui::RingSpan spans[2] = {
            {baseFrac, ImGui::ColorConvertFloat4ToU32(baseC)},
            {std::max(0.0f, frac - baseFrac), convCol},
        };
        ui::drawStackedFractionRing(ImGui::GetWindowDrawList(),
                                    ImVec2(p.x + ringAdv * 0.5f, p.y + fh * 0.5f),
                                    gaugeRingRadius(), gaugeRingThickness(),
                                    spans, 2, gaugeTrackColor(surf));
        ImGui::Dummy(ImVec2(ringAdv, fh));
        ImGui::SameLine(0, gap);
    }
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(showRing ? full.c_str() : lean.c_str());
    ImGui::PopStyleColor();
    ImGui::EndGroup();
    if (ImGui::IsItemHovered()) {
        // Answer the question the meter exists for: how much context this session already holds,
        // and what continuing it costs. Every line is OMITTED when its input is unknown rather
        // than printed as a zero. The catalog lookup and the cost math run ONLY here, on hover —
        // the per-frame path never pays for either.
        //
        // Provenance is resolved in the SAME precedence contextMeterWindow() uses, so the tooltip
        // can never claim a provenance the number does not have.
        const char* srcTxt = nullptr;
        if (st.settings.contextWindow > 0) {
            srcTxt = "your explicit override in AI settings";
        } else if (st.lastContextWindow > 0 && ctxWindowSourceTrusted()) {
            srcTxt = g_ctxWindowSource == "user"
                         ? "the override the harness was configured with"
                         : g_ctxWindowSource == "catalog"
                               ? "the harness's curated catalog entry for the model that "
                                 "actually ran"
                               : "probed from the provider for the model that actually ran";
        } else if (winInfo.known) {
            srcTxt = "from the model catalog entry for the selected model";
        }
        std::string tip = "Context window usage \xe2\x80\x94 ";
        if (used > 0 && !usage.estimate)
            tip += "the last model call's input tokens (" + std::to_string(used) + ").\n";
        else if (used > 0)
            tip += "no call measured yet this session, so this is an ESTIMATE of the "
                   + std::to_string(usage.messages) + "-message transcript on screen ("
                   + std::to_string(used) + " tokens"
                   + (usage.clipped ? ", clipped by a per-message cap \xe2\x80\x94 the real "
                                      "figure is larger" : "") + ").\n";
        else if (win > 0)
            tip += "nothing measured and nothing in this transcript yet, so only the capacity is "
                   "shown (" + std::to_string(win) + " tokens).\n";
        else
            tip += "neither the usage nor the window is known yet this session.\n";
        if (win > 0 && srcTxt) {
            tip += "Window " + std::to_string(win) + " tokens, ";
            tip += srcTxt;
            tip += ".\n";
        } else if (winInfo.tokens > 0) {
            // A guess (id pattern or the harness's per-provider default) is said to be one and
            // never turned into a fraction of the window.
            tip += "The model's window is not reported yet (roughly " + fmtTokens(winInfo.tokens)
                   + " by its family), so no share of it is shown until the model catalog "
                     "loads.\n";
        }
        if (win > 0 && used > 0) {
            char pct[48];
            std::snprintf(pct, sizeof(pct), "%.1f%% of the window used.\n",
                          100.0 * (double)used / (double)win);
            tip += pct;
            // Cost to continue: resending this context on the next call. Emitted ONLY when the
            // catalog row actually carries pricing — the live Anthropic rows report costIn as
            // null (hasCost == false), and a "$0.00" there would be a fabricated number.
            const ui::BridgeModelMeta* pm = ui::bridgeModelMetaForCad(
                st.settings.provider, st.settings.model, st.settings.baseUrl);
            if (pm && pm->hasCost && pm->costIn > 0.0) {
                char cost[80];
                std::snprintf(cost, sizeof(cost),
                              "Continuing costs about $%.4f in input tokens per call.\n",
                              (double)used / 1.0e6 * pm->costIn);
                tip += cost;
            }
        }
        // The old wording had to admit the estimate "excludes the system prompt and tool
        // schemas, so the real prompt will be larger". It no longer does: the precomposed
        // baseline is IN the number, and the breakdown below itemises it.
        if (usage.estimate && usage.baseline <= 0)
            tip += "The estimate counts only this transcript \xe2\x80\x94 the system prompt is "
                   "not loaded yet, so the real prompt will be larger.\n";
        else if (usage.estimate)
            tip += "No call measured yet, so this is the precomposed system prompt plus an "
                   "estimate of the transcript on screen.\n";
        tip += "Set the window in AI settings if your local model differs.";

        // Graphical breakdown: a stacked bar of the window plus one row per block, so the
        // question "what is filling my context?" is answered by SHAPE, not by reading a
        // paragraph. Drawn with an explicit Begin/EndTooltip because SetTooltip is text-only.
        ImGui::BeginTooltip();
        ImGui::PushTextWrapPos(ui::metrics::px(420));
        ImGui::TextUnformatted(tip.c_str());
        ImGui::PopTextWrapPos();
        if (win > 0 && used > 0) {
            const float barW = ui::metrics::px(400), barH = ui::metrics::px(9);
            const float* role = frac < ui::kUsageWarnFrac ? pal.statusOk
                              : frac < ui::kUsageCritFrac ? pal.statusWarn
                                                          : pal.statusErr;
            const ImU32 convCol = ImGui::ColorConvertFloat4ToU32(semanticOnGlass(role, surf));
            ImVec4 dim = ImGui::ColorConvertU32ToFloat4(convCol);
            dim.w *= 0.42f;
            const ImU32 baseCol = ImGui::ColorConvertFloat4ToU32(dim);
            ImGui::Spacing();
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const ImVec2 o = ImGui::GetCursorScreenPos();
            const float r = barH * 0.5f;
            dl->AddRectFilled(o, ImVec2(o.x + barW, o.y + barH),
                              gaugeTrackColor(surf), r);
            const float bw = barW * baseFrac;
            const float cw = barW * std::max(0.0f, frac - baseFrac);
            if (bw > 0.5f)
                dl->AddRectFilled(o, ImVec2(o.x + bw, o.y + barH), baseCol, r);
            if (cw > 0.5f)
                dl->AddRectFilled(ImVec2(o.x + bw, o.y), ImVec2(o.x + bw + cw, o.y + barH),
                                  convCol, r);
            ImGui::Dummy(ImVec2(barW, barH));
            ImGui::Spacing();

            // Legend: the two layers the ring shows, named and quantified.
            auto swatch = [&](ImU32 col) {
                const ImVec2 s = ImGui::GetCursorScreenPos();
                const float k = ImGui::GetTextLineHeight() * 0.62f;
                ImGui::GetWindowDrawList()->AddRectFilled(
                    ImVec2(s.x, s.y + (ImGui::GetTextLineHeight() - k) * 0.5f),
                    ImVec2(s.x + k, s.y + (ImGui::GetTextLineHeight() + k) * 0.5f),
                    col, k * 0.3f);
                ImGui::Dummy(ImVec2(k + ui::metrics::px(6), ImGui::GetTextLineHeight()));
                ImGui::SameLine(0, 0);
            };
            if (usage.baseline > 0) {
                swatch(baseCol);
                ImGui::Text("System prompt  %s", fmtTokens(usage.baseline).c_str());
                swatch(convCol);
                ImGui::Text("Conversation   %s",
                            fmtTokens(std::max(0, used - usage.baseline)).c_str());
                ImGui::Spacing();
                // Per-block itemisation of the system prompt, largest first (the worker already
                // sorts it). Tool schemas normally dominate, which is the actionable fact:
                // trimming tool families is the lever that shrinks a fresh chat.
                if (!g_promptBaseline.segments.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                    ImGui::TextUnformatted("System prompt blocks:");
                    ImGui::PopStyleColor();
                    const int top = (int)g_promptBaseline.segments.size();
                    for (int i = 0; i < top; ++i) {
                        const PromptBaselineSeg& sg = g_promptBaseline.segments[i];
                        const float sf = usage.baseline > 0
                                             ? (float)sg.tokens / (float)usage.baseline : 0.0f;
                        const ImVec2 c = ImGui::GetCursorScreenPos();
                        const float mw = ui::metrics::px(120), mh = ui::metrics::px(6);
                        const float my = c.y + (ImGui::GetTextLineHeight() - mh) * 0.5f;
                        ImGui::GetWindowDrawList()->AddRectFilled(
                            ImVec2(c.x, my), ImVec2(c.x + mw, my + mh),
                            gaugeTrackColor(surf), mh * 0.5f);
                        if (sf > 0.0f)
                            ImGui::GetWindowDrawList()->AddRectFilled(
                                ImVec2(c.x, my),
                                ImVec2(c.x + mw * std::min(1.0f, sf), my + mh),
                                baseCol, mh * 0.5f);
                        ImGui::Dummy(ImVec2(mw + ui::metrics::px(8),
                                            ImGui::GetTextLineHeight()));
                        ImGui::SameLine(0, 0);
                        ImGui::Text("%-18s %6s", sg.title.c_str(),
                                    fmtTokens(sg.tokens).c_str());
                    }
                    if (g_promptBaseline.toolCount > 0) {
                        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                        ImGui::Text("%d tools advertised \xe2\x80\x94 trim families in AI "
                                    "settings to shrink this.", g_promptBaseline.toolCount);
                        ImGui::PopStyleColor();
                    }
                }
            }
        }
        ImGui::EndTooltip();
    }
}

// Width drawContextMeter() will occupy, so the caller can right-align it on its line above the
// composer. Shares contextMeterText() with drawContextMeter() and mirrors its showRing decision,
// so reserved width == drawn width in EVERY state (measured fraction, the wider
// "~"-prefixed estimate, em-dash numerator, and the ringless unknown-window tag).
static float measureContextMeter(AppState& st, float roomForMeter) {
    const float gap = ui::metrics::px(5), ringAdv = gaugeRingAdvance();
    const CtxMeterText txt = contextMeterText(st);
    const float fullTw = ImGui::CalcTextSize(txt.full.c_str()).x;
    if (txt.ringOk && roomForMeter >= ringAdv + gap + fullTw) return ringAdv + gap + fullTw;
    return txt.ringOk ? fullTw : ImGui::CalcTextSize(("ctx " + txt.full).c_str()).x;
}

// Provider rate-limit gauges for the prelude row: one ring per usage window the ACTIVE
// account reports ("5H (o) 7D (o)"), sitting immediately left of the context ring.
// Every layer is opt-out by ABSENCE, which is what keeps inapplicable elements hidden: key-based
// and local providers have no usage source at all, an account may report no windows, and a
// window without a fraction is status-only — each of those draws NOTHING rather than a
// misleading empty ring. `draw == false` measures only (emits no ImGui items) so the caller can
// right-align the usage cluster and the context gauge as one block.
//
// Chudcode is authoritative for online account usage (Claude Max, ZenMux, Kimi Code, xAI,
// Cursor). Providers without usage windows simply contribute no rings.
//
// `budget` is the width the rings may consume. Indicators are dropped from the END until the
// set fits, rather than all-or-nothing: a 340dp column only clears ~90dp next to the context
// gauge, so an all-or-nothing rule would hide Claude's 5H+7D pair on the DEFAULT layout. Both
// passes are handed the same budget, so measure and draw always pick the same set. RPC results
// only land in AsyncRpc::drain() at the top of a frame, so the source cannot change between the
// two passes and the composer row never jumps.
static float usageRingCluster(AppState& st, float budget, bool draw) {
    const std::vector<ui::UsageWindow>* wins = nullptr;
    if (const char* bridgeId = ui::bridgeProviderForCad(st.settings.provider))
        wins = ui::bridgeProviderUsage(bridgeId);
    if (!wins) {
        if (const char* sub = ui::subscriptionProviderForCad(st.settings.provider))
            wins = ui::activeAccountUsage(sub);
    }
    if (!wins || wins->empty()) return 0.0f;

    // Which windows earn a ring is a rule with its own test (ui::pickUsageRings in
    // provider_usage.h, host test test_usage_rings): rolling windows win, a lifetime
    // total only shows when there is no rolling one, and two rings is the ceiling for
    // this column — the third only ever fit by starving the context gauge beside it.
    const std::vector<const ui::UsageWindow*> pick = ui::pickUsageRings(*wins, 2);
    if (pick.empty()) return 0.0f;

    const ui::AdaptiveSurface& surf = chatSurf();
    const float capGap  = ui::metrics::px(3);   // caption -> its ring
    const float indGap  = ui::metrics::px(7);   // between indicators
    const float ringAdv = gaugeRingAdvance();
    const float fh = ImGui::GetFrameHeight();
    float total = 0.0f;
    int   shown = 0;
    for (const ui::UsageWindow* wp : pick) {
        const ui::UsageWindow& w = *wp;
        const std::string cap = ui::usageWindowShortLabel(w);
        const float capW = ImGui::CalcTextSize(cap.c_str()).x;
        const float indW = capW + capGap + ringAdv;
        const float step = (shown > 0 ? indGap : 0.0f) + indW;
        if (total + step > budget) break;   // out of room — the remaining windows drop
        if (shown > 0 && draw) ImGui::SameLine(0, indGap);
        total += step;
        ++shown;
        if (!draw) continue;
        const ImVec2 p = ImGui::GetCursorScreenPos();
        ImGui::Dummy(ImVec2(indW, fh));   // reserve first: the ring is painted into this rect
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const float midY = p.y + fh * 0.5f;
        dl->AddText(ImVec2(p.x, midY - ImGui::GetFontSize() * 0.5f),
                    ImGui::ColorConvertFloat4ToU32(surf.textSecondary), cap.c_str());
        const float frac = ImClamp(w.usedFraction, 0.0f, 1.0f);
        ui::drawFractionRing(dl, ImVec2(p.x + capW + capGap + ringAdv * 0.5f, midY),
                             gaugeRingRadius(), gaugeRingThickness(), frac,
                             gaugeTrackColor(surf),
                             ImGui::ColorConvertFloat4ToU32(
                                 semanticOnGlass(ui::usageStatusColor(w.status, frac), surf)));
        if (ImGui::IsItemHovered()) {
            const std::string label  = w.label.empty() ? w.window : w.label;
            const std::string resets = ui::usageResetCountdown(w.resetsAt);
            if (resets.empty())
                ImGui::SetTooltip("%s \xe2\x80\x94 %.0f%%", label.c_str(), frac * 100.0f);
            else
                ImGui::SetTooltip("%s \xe2\x80\x94 %.0f%%\n%s", label.c_str(), frac * 100.0f,
                                  resets.c_str());
        }
    }
    return total;
}

// One row in the drop-up mode menu: frosted glass base + per-mode coloredRowWash gradient
// (same hues as modeBorderColor / modeAccentRgb). Every row is tinted; selected gets a
// stronger fill + mode-colored glow outline; hover brightens. Returns true when clicked.
// No mode is ever gated — all three run the same agent turn. Header colors are pushed
// transparent by the caller so the wash is the only row background.
static bool modeMenuRow(const char* iconName, const char* label, bool selected, int uid) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const float rowH = ImGui::GetFrameHeight() + 2.0f * sc;
    const float fh = ImGui::GetFontSize();
    ImGui::PushID(uid);
    const bool clicked = ImGui::Selectable("##moderow", selected,
                                           ImGuiSelectableFlags_AllowItemOverlap, ImVec2(0, rowH));
    const bool hov = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
    // Logical dp rounding (helpers multiply by uiScale) — matches Layers/rail-tab pills.
    const float rnd = 4.0f;
    const float washA = selected ? 0.42f : (hov ? 0.36f : 0.28f);
    ui::glassRowBackground(dl, mn, mx, rnd,
                           /*stripe=*/false, /*selected=*/false, /*washAlpha=*/washA);
    float tint[3];
    modeAccentRgb(uid, tint);
    // Resting rows still show a clear mode tint; hover brightens; selected strongest + glow.
    const float strength = selected ? 0.38f : (hov ? 0.28f : 0.18f);
    ui::coloredRowWash(dl, mn, mx, rnd, tint, strength, /*selected=*/selected);
    const float midY = (mn.y + mx.y) * 0.5f;
    const float pad = 8.0f * sc;
    const float isz = fh * 1.02f;
    const ImU32 ink = ui::Palette::toU32(selected ? pal.text : pal.textDim);
    float textX = mn.x + pad;
    IconUV ic = ui::getIcon(iconName, 20);
    if (ic.valid) {
        const ImVec2 i0(mn.x + pad, midY - isz * 0.5f);
        dl->AddImage(ic.texId, i0, ImVec2(i0.x + isz, i0.y + isz), ic.uv0, ic.uv1, ink);
        textX = i0.x + isz + 7.0f * sc;
    }
    dl->AddText(ImVec2(textX, midY - fh * 0.5f), ui::Palette::toU32(pal.text), label);
    ImGui::PopID();
    return clicked;
}


// Prelude row above the prompt: Agent/Plan/Ask dropdown (left) + busy mark
// (inline, only while a run is active) + context meter (right).
// Permissions live only in the Settings overlay (not the composer).
static void drawComposerPrelude(AppState& st, bool busy) {

    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    // Subscription usage feeds the prelude's provider-limit rings. Both polls are TTL-gated and
    // cheap; the 60s accounts TTL is deliberately lazier than the Settings UI's 1.5s — nothing is
    // being MANAGED here, the rings just need the active account's windows to stay roughly fresh.
    ui::providerUsagePollAccounts(st, 60.0, false);
    ui::providerUsagePollUsage(st, false);
    // Chudcode owns online account usage. Poll only the active provider on the existing
    // five-minute TTL.
    if (const char* bridgeUsageId = ui::bridgeProviderForCad(st.settings.provider))
        ui::providerUsagePollBridge(st, bridgeUsageId, false);
    // Refresh cached model names on the same TTL so the chip does not fall back to raw ids.
    ui::refreshBridgeModelCatalog(st, false);

    // Compact frame height matching the toolbar chips (model / thinking).
    // Arrow button uses ImGuiCol_Button — keep it identical to FrameBg so the caret
    // well is not shaded differently from the combo body.
    const ImVec4 frameBg(
        surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.08f);
    const ImVec4 frameHov(
        surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.12f);
    const ImVec4 frameAct(
        surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.16f);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8.0f * sc, 3.0f * sc));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::rounding(6.0f));
    ImGui::PushStyleColor(ImGuiCol_Button, frameBg);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, frameHov);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, frameAct);
    ImGui::PushStyleColor(ImGuiCol_FrameBg, frameBg);
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, frameHov);
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive, frameAct);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::PushStyleColor(ImGuiCol_PopupBg,
        ImVec4(pal.popupBg[0], pal.popupBg[1], pal.popupBg[2], pal.popupBg[3]));

    // Agent / Plan / Ask — drop-UP menu that propagates upward like the model picker, with an
    // atlas icon + glass row styling per entry. The mode is a pure USER choice of tool policy:
    // every entry runs the same single Chudcode general_agent turn.
    static const char* modes[]     = {"Agent", "Plan", "Ask"};
    static const char* modeIcons[] = {"ai_panel", "plan_board", "ask"};
    static const char* modeTips[]  = {
        "Agent — full CAD tool loadout; the agent decides how to do the work "
        "(inline, subagents, or a mission).",
        "Plan — read/inspect and draft a plan; no geometry or file mutation.",
        "Ask — read-only questions about the part, project or CAD; nothing changes."};
    const int mode = normalizeCopilotMode(st.aiInteraction);
    // Trigger chip: icon + longest label + up-caret. Custom-drawn (InvisibleButton + glass wash)
    // so the leading atlas icon sits inline, matching the composer's other chips.
    const float fh = ImGui::GetFontSize();
    float labelW = 0.0f;
    for (const char* m : modes) labelW = ImMax(labelW, ImGui::CalcTextSize(m).x);
    const float iconSz = fh * 1.02f;
    const float caretW = fh * 0.7f;
    const float triggerW = 8.0f * sc + iconSz + 7.0f * sc + labelW + 8.0f * sc + caretW + 6.0f * sc;
    const float segH = ImGui::GetFrameHeight();
    const bool trigClick = ImGui::InvisibleButton("##aimode", ImVec2(triggerW, segH));
    const bool trigHov = ImGui::IsItemHovered();
    ImDrawList* pdl = ImGui::GetWindowDrawList();
    const ImVec2 tmn = ImGui::GetItemRectMin(), tmx = ImGui::GetItemRectMax();
    // Frosted base + active-mode colored gradient (matches the selected popup row).
    const float trigRnd = 6.0f;
    ui::glassRowBackground(pdl, tmn, tmx, trigRnd,
                           /*stripe=*/false, /*selected=*/false,
                           /*washAlpha=*/trigHov ? 0.36f : 0.30f);
    {
        float tint[3];
        modeAccentRgb(mode, tint);
        const float strength = trigHov ? 0.32f : 0.22f;
        ui::coloredRowWash(pdl, tmn, tmx, trigRnd, tint, strength, /*selected=*/true);
    }
    const float tMidY = (tmn.y + tmx.y) * 0.5f;
    const ImU32 tink = ImGui::GetColorU32(surf.textPrimary);
    {
        float tx = tmn.x + 8.0f * sc;
        IconUV ic = ui::getIcon(modeIcons[mode], 20);
        if (ic.valid) {
            const ImVec2 i0(tmn.x + 8.0f * sc, tMidY - iconSz * 0.5f);
            pdl->AddImage(ic.texId, i0, ImVec2(i0.x + iconSz, i0.y + iconSz), ic.uv0, ic.uv1, tink);
            tx = i0.x + iconSz + 7.0f * sc;
        }
        pdl->AddText(ImVec2(tx, tMidY - fh * 0.5f), tink, modes[mode]);
    }
    {   // up-caret — signals the menu opens upward
        const float cx = tmx.x - caretW - 4.0f * sc, cy = tMidY;
        const float r = fh * 0.24f;
        pdl->AddTriangleFilled(ImVec2(cx + r * 0.8f, cy - r * 0.7f),
                               ImVec2(cx, cy + r * 0.6f),
                               ImVec2(cx + r * 1.6f, cy + r * 0.6f), tink);
    }
    if (trigClick) ImGui::OpenPopup("##aimodemenu");
    // Shot-only hook (CHUDWARE_CHAT_MODEMENU): hold the mode drop-up open for headless
    // screenshot verification of the per-mode gradient rows. No-op in normal use.
    if (std::getenv("CHUDWARE_CHAT_MODEMENU")) ImGui::OpenPopup("##aimodemenu");
    if (trigHov && !ImGui::IsPopupOpen("##aimodemenu"))
        ImGui::SetTooltip("%s", modeTips[mode]);

    // Drop-UP: anchor the popup's bottom-left just above the trigger (pivot 0,1) so entries
    // propagate upward — same fixed-anchor trick as the model picker.
    {
        const float popW = ImMax(triggerW, 150.0f * sc);
        const float gap = 4.0f * sc;
        ImGui::SetNextWindowSizeConstraints(ImVec2(popW, 0), ImVec2(popW, FLT_MAX));
        ImGui::SetNextWindowPos(ImVec2(tmn.x, tmn.y - gap), ImGuiCond_Always, ImVec2(0.0f, 1.0f));
    }
    ImGui::PushStyleColor(ImGuiCol_Header, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive, ImVec4(0, 0, 0, 0));
    if (ImGui::BeginPopup("##aimodemenu",
                          ImGuiWindowFlags_NoMove | ImGuiWindowFlags_AlwaysAutoResize)) {
        for (int i = 0; i < kCopilotModeCount; ++i) {
            if (modeMenuRow(modeIcons[i], modes[i], mode == i, i)) {
                st.aiInteraction = i;
                st.settings.copilotMode = i;
                saveSettings(st.settings);  // survive restart without waiting for next send
                ImGui::CloseCurrentPopup();
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", modeTips[i]);
        }
        ImGui::EndPopup();
    }
    ImGui::PopStyleColor(3);

    // Busy mark — centered in the gap between the mode dropdown and the right-hand
    // gauge cluster. Hidden the instant Stop is pressed (aiTurnActive honors stopRequested)
    // so the animation never keeps playing after a stop.
    // Note: `busy` is job.running and can lag past Stop — always gate on the run's stopRequested.
    // Use `tmx` (mode chip screen rect captured above) — GetItemRectMax after the
    // popup is no longer the mode button.
    {
        auto* markRun = st.runIfExists(st.activeConvId());
        const bool showBusyMark = !(markRun && markRun->stopRequested.load()) &&
                                  (busy || activeConversationRunning(st));
        // The gauge cluster is RIGHT-aligned, so its last glyph sits on the column's clip
        // edge. Two things then clip the readout, and both are fixed here rather than in the
        // gauge: (a) the meter was measured against the FULL remaining width, so it kept its
        // ring even when the cluster as a whole could not fit and overflowed the row; (b) the
        // text landed exactly at contentRegionMax, where the trailing glyph's ink is shaved by
        // the clip rect. Budget the meter to the space actually left of the mode chip, and
        // hold a hair of inset off the edge.
        const float edgeInset = ui::metrics::px(2);
        const float rowRightScreen =
            ImGui::GetWindowPos().x + ImGui::GetContentRegionMax().x - edgeInset;
        const float meterRoomFull = std::max(0.0f,
            std::min(ImGui::GetContentRegionAvail().x - edgeInset,
                     rowRightScreen - (tmx.x + ui::metrics::px(6))));
        const float meterW = measureContextMeter(st, meterRoomFull);
        // Provider-limit rings ride LEFT of the context gauge as one right-aligned cluster, and
        // give way first when the column narrows: the context gauge always wins, and the cluster
        // may never reach back over the mode chip (px(6) keeps a visible break between them).
        const float clusterGap = ui::metrics::px(10);
        const float usageBudget = (rowRightScreen - tmx.x) - clusterGap - meterW -
                                  ui::metrics::px(6);
        const float usageW = usageRingCluster(st, usageBudget, /*draw=*/false);
        const float clusterW = usageW > 0.0f ? usageW + clusterGap + meterW : meterW;
        const float meterLeftScreen = rowRightScreen - clusterW;
        if (showBusyMark) {
            const float midScreen = (tmx.x + meterLeftScreen) * 0.5f;
            float markScreenX = midScreen - segH * 0.5f;
            // Keep clear of the mode chip; fall back if the row is narrow.
            if (markScreenX < tmx.x + 4.0f * sc)
                markScreenX = tmx.x + 8.0f * sc;
            if (markScreenX + segH > meterLeftScreen - 4.0f * sc)
                markScreenX = ImMax(tmx.x + 4.0f * sc, meterLeftScreen - segH - 4.0f * sc);
            ImGui::SameLine(0, 0.0f);
            ImGui::SetCursorScreenPos(ImVec2(markScreenX, tmn.y));
            drawBusyMark(segH);  // match prelude chip height
            if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                ImGui::SetTooltip("AI is working\xe2\x80\xa6");
        }
        // Gauge cluster on the SAME row, right-aligned as one block.
        ImGui::SameLine(0, 0.0f);
        const float clusterCursorX = ImGui::GetContentRegionMax().x - edgeInset - clusterW;
        if (clusterCursorX > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(clusterCursorX);
        if (usageW > 0.0f) {
            usageRingCluster(st, usageBudget, /*draw=*/true);
            // Subscription quotas and the context window answer DIFFERENT questions
            // ("when can I work again" vs "how full is this conversation"), and butting
            // them together at identical weight is what made the row read as one soup.
            // A hairline rule centred in the existing gap separates the two groups
            // without costing layout width — clusterGap is already reserved, so this
            // paints inside it and measure/draw stay in agreement.
            const ImVec2 gp = ImGui::GetCursorScreenPos();
            const float ruleH = ImGui::GetFrameHeight() * 0.52f;
            const float ruleY = gp.y + (ImGui::GetFrameHeight() - ruleH) * 0.5f;
            const float ruleX = std::round(gp.x + clusterGap * 0.5f);
            ImVec4 ink = chatSurf().textSecondary;
            ink.w *= 0.34f;   // present, never a competing element
            ImGui::GetWindowDrawList()->AddLine(
                ImVec2(ruleX, ruleY), ImVec2(ruleX, ruleY + ruleH),
                ImGui::ColorConvertFloat4ToU32(ink), std::max(1.0f, ui::uiScale));
            ImGui::SameLine(0, clusterGap);
        }
        drawContextMeter(st, meterRoomFull);
    }


    ImGui::PopStyleColor(8);
    ImGui::PopStyleVar(2);
    // Tight gap to the prompt — prelude sits closer to the input box.
}

// ---- voice input (live Chudcode session STT) -------------------------------------------------
enum class VoiceUiState { Idle, Listening, Processing, Error, Unavailable };

struct VoiceInputUi {
    VoiceUiState state = VoiceUiState::Unavailable;
    bool probed = false;
    bool rpcBusy = false;
    bool pollQueued = false;
    std::string reason;
    std::string modelLabel;
    bool modelCached = false;
    int progress = -1;
    std::string partial;
    std::string committed;
    float level = 0.0f;
    float animT = 0.0f;
    std::string basePrompt;
    std::string sessionId;
    unsigned long long requestGeneration = 0;
    double lastPollSec = 0.0;
    double lastProbeSec = -1e9;
};

static VoiceInputUi& voiceUi() {
    static VoiceInputUi v;
    return v;
}

static nlohmann::json voiceSessionParams(const VoiceInputUi& v) {
    return nlohmann::json{{"session_id", v.sessionId}};
}

static void voiceApplyPrompt(AppState& st, const std::string& base, const std::string& committed,
                             const std::string& partial) {
    const std::string out = ui::composeChudcodeTranscript(base, committed, partial);
    if (st.prompt.size() < out.size() + 1) st.prompt.resize(out.size() + 256, 0);
    std::memcpy(st.prompt.data(), out.c_str(), out.size() + 1);
}

static bool voiceApplyStatus(AppState& st, const nlohmann::json& r) {
    VoiceInputUi& v = voiceUi();
    ui::ChudcodeSttResult status;
    if (!ui::parseChudcodeSttResult(r, status)) return false;
    v.modelLabel = status.model;
    v.progress = status.progress;
    if (status.progress >= 100 || status.state == "recording") v.modelCached = true;
    if (!status.error.empty())
        v.reason = status.error;
    else if (status.available)
        v.reason.clear();

    if (!status.available) {
        v.state = VoiceUiState::Unavailable;
    } else if (status.state == "recording") {
        v.state = VoiceUiState::Listening;
        voiceApplyPrompt(st, v.basePrompt, v.committed, v.partial);
    } else if (status.state == "transcribing" ||
               (status.state == "idle" && v.rpcBusy)) {
        v.state = VoiceUiState::Processing;
    } else {
        v.state = VoiceUiState::Idle;
    }
    return true;
}

static void voiceBindSession(AppState& st) {
    VoiceInputUi& v = voiceUi();
#ifdef __EMSCRIPTEN__
    const std::string sessionId = cellAgentSessionHandle(st.activeConvId());
#else
    const std::string sessionId = st.activeConvId();
#endif
    if (v.sessionId == sessionId) return;
    const std::string priorSession = v.sessionId;
    const bool cancelPrior = !priorSession.empty() &&
                             (v.state == VoiceUiState::Listening ||
                              v.state == VoiceUiState::Processing || v.rpcBusy);
    const auto generation = v.requestGeneration + 1;
    v = VoiceInputUi{};
    v.sessionId = sessionId;
    v.requestGeneration = generation;
    if (cancelPrior) {
        st.rpc.call(ui::kChudcodeSttCancelMethod,
                    nlohmann::json{{"session_id", priorSession}},
                    [](const core::RpcResult&) {}, 15000);
    }
}

static void voiceProbe(AppState& st) {
    voiceBindSession(st);
    VoiceInputUi& v = voiceUi();
    if (v.rpcBusy || v.pollQueued || v.state == VoiceUiState::Listening ||
        v.state == VoiceUiState::Processing) return;
#ifdef __EMSCRIPTEN__
    if (v.probed) return;
    v.probed = true;
    v.state = VoiceUiState::Unavailable;
    v.reason = "Voice input is not available in the web build.";
    return;
#else
    if (!startupComposerMetadataReady(st)) return;
    if (v.probed && ImGui::GetTime() - v.lastProbeSec < 5.0) return;
    v.lastProbeSec = ImGui::GetTime();
    if (v.sessionId.empty()) {
        v.probed = true;
        v.state = VoiceUiState::Unavailable;
        v.reason = "Voice input needs an active copilot session.";
        return;
    }
    v.rpcBusy = true;
    const std::string sessionId = v.sessionId;
    const auto generation = v.requestGeneration;
    st.rpc.call(ui::kChudcodeSttStatusMethod, voiceSessionParams(v),
                [&st, sessionId, generation](const core::RpcResult& r) {
                    VoiceInputUi& vv = voiceUi();
                    if (vv.sessionId != sessionId || vv.requestGeneration != generation) return;
                    vv.rpcBusy = false;
                    vv.lastProbeSec = ImGui::GetTime();
                    if (core::workerLaneBusy(r)) {
                        // A run holds the speech service: keep the last-known mic state and
                        // re-probe on the normal cadence. Before any answer, say when it opens.
                        if (!vv.probed) vv.reason = kAvailableAfterRun;
                        vv.probed = true;   // keeps the 5 s cadence instead of per-frame retries
                        return;
                    }
                    vv.probed = true;
                    if (!r.ok || !voiceApplyStatus(st, r.result)) {
                        vv.state = VoiceUiState::Unavailable;
                        vv.reason =
                            userFacingRpcError(r, "Speech-to-text isn't available right now.");
                    }
                },
                15000);
#endif
}

static void voicePoll(AppState& st) {
    VoiceInputUi& v = voiceUi();
    if (v.state != VoiceUiState::Listening && v.state != VoiceUiState::Processing) return;
    if (v.pollQueued || v.rpcBusy) return;
    const double now = ImGui::GetTime();
    if (now - v.lastPollSec < 0.20) return;
    v.lastPollSec = now;
    v.pollQueued = true;
    const std::string sessionId = v.sessionId;
    const auto generation = v.requestGeneration;
    st.rpc.call(ui::kChudcodeSttStatusMethod, voiceSessionParams(v),
                [&st, sessionId, generation](const core::RpcResult& r) {
                    VoiceInputUi& vv = voiceUi();
                    if (vv.sessionId != sessionId) return;
                    vv.pollQueued = false;
                    vv.lastPollSec = ImGui::GetTime();
                    if (vv.requestGeneration != generation) return;
                    if (r.ok) voiceApplyStatus(st, r.result);
                },
                10000);
}

static void voiceStart(AppState& st) {
    VoiceInputUi& v = voiceUi();
    if (v.rpcBusy || v.state == VoiceUiState::Unavailable) return;
    v.basePrompt = st.prompt.data();
    v.partial.clear();
    v.committed.clear();
    v.reason.clear();
    v.progress = 0;
    v.rpcBusy = true;
    v.state = VoiceUiState::Processing;
    const std::string sessionId = v.sessionId;
    const auto generation = ++v.requestGeneration;
    st.rpc.call(ui::kChudcodeSttStartMethod, voiceSessionParams(v),
                [&st, sessionId, generation](const core::RpcResult& r) {
                    VoiceInputUi& vv = voiceUi();
                    if (vv.sessionId != sessionId || vv.requestGeneration != generation) return;
                    vv.rpcBusy = false;
                    if (core::workerLaneBusy(r)) {
                        // Refused only because a run holds the speech service.
                        vv.state = VoiceUiState::Idle;
                        vv.reason.clear();
                        st.toast(ui::ToastLevel::Info,
                                 std::string("Voice input: ") + kAvailableAfterRun);
                        return;
                    }
                    if (!r.ok || !voiceApplyStatus(st, r.result)) {
                        vv.state = VoiceUiState::Error;
                        vv.reason = userFacingRpcError(r, "Couldn't start voice input.");
                        st.toast(ui::ToastLevel::Warn, vv.reason.c_str());
                    }
                },
                300000);
}

static void voiceStop(AppState& st) {
    VoiceInputUi& v = voiceUi();
    if (v.rpcBusy && v.state != VoiceUiState::Listening) return;
    v.rpcBusy = true;
    v.state = VoiceUiState::Processing;
    const std::string sessionId = v.sessionId;
    const auto generation = ++v.requestGeneration;
    st.rpc.call(ui::kChudcodeSttStopMethod, voiceSessionParams(v),
                [&st, sessionId, generation](const core::RpcResult& r) {
                    VoiceInputUi& vv = voiceUi();
                    if (vv.sessionId != sessionId || vv.requestGeneration != generation) return;
                    vv.rpcBusy = false;
                    if (core::workerLaneBusy(r)) {
                        // The recording is still live on the service; keep listening and say
                        // when stopping works again.
                        vv.state = VoiceUiState::Listening;
                        st.toast(ui::ToastLevel::Info,
                                 std::string("Stop dictation: ") + kAvailableAfterRun);
                        return;
                    }
                    ui::ChudcodeSttResult result;
                    if (!r.ok || !ui::parseChudcodeSttResult(r.result, result)) {
                        vv.state = VoiceUiState::Error;
                        vv.reason = userFacingRpcError(r, "Transcription failed.");
                        st.toast(ui::ToastLevel::Warn, vv.reason.c_str());
                        return;
                    }
                    voiceApplyPrompt(st, vv.basePrompt, result.text, "");
                    vv.partial.clear();
                    vv.committed = result.text;
                    vv.state = VoiceUiState::Idle;
                    if (result.text.empty())
                        st.toast(ui::ToastLevel::Info, "No speech detected.");
                },
                120000);
}

static void voiceCancel(AppState& st) {
    VoiceInputUi& v = voiceUi();
    v.rpcBusy = true;
    const std::string sessionId = v.sessionId;
    const auto generation = ++v.requestGeneration;
    st.rpc.call(ui::kChudcodeSttCancelMethod, voiceSessionParams(v),
                [&st, sessionId, generation](const core::RpcResult&) {
                    VoiceInputUi& vv = voiceUi();
                    if (vv.sessionId != sessionId || vv.requestGeneration != generation) return;
                    vv.rpcBusy = false;
                    voiceApplyPrompt(st, vv.basePrompt, "", "");
                    vv.partial.clear();
                    vv.committed.clear();
                    vv.state = VoiceUiState::Idle;
                },
                15000);
}

static void drawMicGlyph(ImDrawList* dl, ImVec2 c, float isz, ImU32 ink, float pulse,
                         float level, bool listening) {
    const float th = ui::metrics::stroke(1.4f);
    constexpr float kPi = 3.14159265f;
    // Capsule mic body
    const float bw = isz * 0.22f;
    const float bh = isz * 0.38f;
    dl->AddRect(ImVec2(c.x - bw, c.y - bh), ImVec2(c.x + bw, c.y + bh * 0.15f), ink,
                bw, 0, th);
    // Stem + base
    dl->AddLine(ImVec2(c.x, c.y + bh * 0.15f), ImVec2(c.x, c.y + bh * 0.55f), ink, th);
    dl->AddLine(ImVec2(c.x - bw * 0.9f, c.y + bh * 0.55f),
                ImVec2(c.x + bw * 0.9f, c.y + bh * 0.55f), ink, th);
    // Arc yoke
    dl->PathClear();
    dl->PathArcTo(ImVec2(c.x, c.y + bh * 0.05f), bw * 1.55f, 0.15f * kPi, 0.85f * kPi);
    dl->PathStroke(ink, 0, th);
    if (listening) {
        // Soft pulse ring + 3-bar waveform driven by level (not noisy).
        const float ring = isz * (0.62f + 0.10f * pulse);
        dl->AddCircle(c, ring, IM_COL32((ink >> IM_COL32_R_SHIFT) & 0xFF,
                                       (ink >> IM_COL32_G_SHIFT) & 0xFF,
                                       (ink >> IM_COL32_B_SHIFT) & 0xFF,
                                       (int)(55 + 50 * pulse)),
                      0, ui::metrics::stroke(1.2f));
        const float barBase = isz * 0.12f;
        for (int i = 0; i < 3; ++i) {
            const float x = c.x + (i - 1) * isz * 0.18f;
            const float h = barBase * (0.55f + level * (0.9f + 0.35f * (float)(i == 1)));
            dl->AddLine(ImVec2(x, c.y - h), ImVec2(x, c.y + h), ink, th);
        }
    }
}

// Toolbar row below the prompt: [model | thinking | paperclip | mic | context-cube]
// Five abutting, non-overlapping segments separated by painted vertical dividers. Model picker
// takes the remaining width; thinking reserves its widest label; icon chips are square.
// The send/stop control lives inside the input box (drawComposerSendStop); New chat lives in
// the panel header.
static void drawComposerToolbar(AppState& st, bool busy, bool embedded = false) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();

    ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float availW = ImGui::GetContentRegionAvail().x;
    ImDrawList* bgDl = ImGui::GetWindowDrawList();
    if (embedded) {
        // Hairline divider from the surface's own ink so it reads on any backdrop, inset by the
        // shared menu gutter so it lines up with every other copilot menu's content edge.
        bgDl->AddLine(ImVec2(rowMin.x + ui::metrics::cpMenuGutter(), rowMin.y),
                      ImVec2(rowMin.x + availW - ui::metrics::cpMenuGutter(), rowMin.y),
                      ImGui::ColorConvertFloat4ToU32(ImVec4(surf.textTertiary.x,
                          surf.textTertiary.y, surf.textTertiary.z, 0.40f)),
                      ui::metrics::stroke(1.0f));
    } else {
        const float rowH = ImGui::GetFrameHeight() + 4 * sc;
        ui::glassPanel(bgDl, ImRect(rowMin, ImVec2(rowMin.x + availW, rowMin.y + rowH)),
                       ui::GlassLevel::Thin, 4.0f, surf);
    }

    // Compact chip height matches the Design combo above.
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * sc, 3 * sc));
    // Segments abut exactly (zero spacing): hitboxes never overlap and the dividers painted at
    // each shared edge do the separating.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(0.0f, ImGui::GetStyle().ItemSpacing.y));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::cpMenuChipRound());
    // No all-around chip outlines: a ring of outlined cells under a rounded field reads as a
    // spreadsheet. Vertical dividers (painted last, below) separate the segments; hover/press
    // washes carry affordance.
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 0.0f);
    // Shared translucent chip fill (opaque fills fought the adaptive text).
    const ImVec4 chipBg(0, 0, 0, 0);
    const ImVec4 chipHov(
        pal.buttonHovered[0], pal.buttonHovered[1], pal.buttonHovered[2], 0.55f);
    const ImVec4 chipAct(
        pal.buttonActive[0], pal.buttonActive[1], pal.buttonActive[2], 0.60f);
    ImGui::PushStyleColor(ImGuiCol_Button, chipBg);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, chipHov);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, chipAct);

    // The catalog can explicitly report no dial; do not invent one.
    Settings& s = st.settings;
    const char* effLabels[8] = {};
    const char* effValues[8] = {};
    int nEff = composerEffortLadder(s, effLabels, effValues, 8);
    int effIdx = -1;
    for (int i = 0; i < nEff; ++i)
        if (std::strcmp(s.reasoningEffort, effValues[i]) == 0) effIdx = i;
    const char* effBtn = effIdx >= 0 ? effLabels[effIdx] : "Default";

    // Segment widths (left→right: model, thinking, paperclip, mic, cube). Icon chips are square.
    // Thinking reserves its WIDEST possible label plus padding, so "Medium"/"Default" never clip
    // and the strip does not jump when the level changes. Model takes everything left — the
    // brand mark + real display name is the one string this strip exists to show.
    const float segH = ImGui::GetFrameHeight();
    const float iconSz = segH;
    const float iconW = segH;
    const float iconsW = iconW * 3.0f;
    const float padX = ImGui::GetStyle().FramePadding.x;

    const std::string modelLogo = composerProviderLogoId(s);
    const float logoSide    = ImGui::GetFontSize() * 1.05f;
    const float logoGap     = ui::metrics::px(6);
    const float logoAdvance = logoSide + logoGap;

    float effTextW = std::max(ImGui::CalcTextSize("Default").x, ImGui::CalcTextSize("No dial").x);
    for (int i = 0; i < nEff; ++i)
        effTextW = std::max(effTextW, ImGui::CalcTextSize(effLabels[i]).x);
    const float thinkWant = std::ceil(effTextW + padX * 2.0f + ui::metrics::px(8));
    // Below the dock minimum the model chip keeps at least its brand mark; thinking yields first.
    // Widths stay >= 1px: ImGui treats a 0 width as "auto-size to label", which would overflow.
    const float minModelW = logoAdvance + padX * 2.0f;
    const float thinkW = std::max(1.0f, std::min(thinkWant, availW - iconsW - minModelW));
    const float modelW = std::max(1.0f, std::floor(availW - iconsW - thinkW));

    // Right edges of the first four segments — where the dividers go.
    float divX[4] = {};
    int nDiv = 0;

    // ---- 1/5 model: provider brand mark + the model's REAL display name (no provider text) ----
    std::string modelText = composerModelName(s);
    {
        // Ellipsize the NAME only — the mark is fixed-width and never clipped. No caret/arrow.
        const float maxTextW = modelW - logoAdvance - padX * 2.0f;
        if (maxTextW > 0.0f && ImGui::CalcTextSize(modelText.c_str()).x > maxTextW) {
            const float dotsW = ImGui::CalcTextSize("...").x;
            const char* b = modelText.c_str();
            size_t len = modelText.size();
            while (len > 1 && ImGui::CalcTextSize(b, b + len).x + dotsW > maxTextW) {
                --len;
                while (len > 1 && ((unsigned char)modelText[len] & 0xC0) == 0x80) --len;  // UTF-8
            }
            modelText.erase(len);
            modelText += "...";
        }
    }
    // Empty button: mark + name are painted into its rect so the icon sits inline with the text.
    if (ImGui::Button("##model", ImVec2(modelW, segH))) ImGui::OpenPopup("##modeldropdown");
    // Capture the model chip rect so the drop-up popup can anchor fixed above it.
    const ImVec2 modelBtnMin = ImGui::GetItemRectMin();
    const ImVec2 modelBtnMax = ImGui::GetItemRectMax();
    divX[nDiv++] = modelBtnMax.x;
    {
        ImDrawList* mdl = ImGui::GetWindowDrawList();
        const float midY = (modelBtnMin.y + modelBtnMax.y) * 0.5f;
        const float x0 = modelBtnMin.x + ImGui::GetStyle().FramePadding.x;
        ui::drawProviderLogo(mdl, ImVec2(x0, midY - logoSide * 0.5f), logoSide, modelLogo);
        mdl->AddText(ImVec2(x0 + logoAdvance, midY - ImGui::GetFontSize() * 0.5f),
                     ImGui::ColorConvertFloat4ToU32(surf.textPrimary), modelText.c_str());
    }
    // The chip shows the model; the tooltip keeps the full provider + exact id.
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("%s \xc2\xb7 %s\nModel — click to search and switch.",
                          composerProviderName(s.provider), s.model[0] ? s.model : "default");

    // ---- 2/5 thinking effort (level only) ----
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    if (nEff <= 0) ImGui::BeginDisabled();
    if (ImGui::Button(nEff > 0 ? effBtn : "No dial", ImVec2(thinkW, segH)))
        ImGui::OpenPopup("##cmpeffort");
    if (nEff <= 0) ImGui::EndDisabled();
    divX[nDiv++] = ImGui::GetItemRectMax().x;
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip(nEff > 0 ? "Thinking effort for the selected model."
                                  : "This model has no thinking-effort dial.");
    if (ImGui::BeginPopup("##cmpeffort")) {
        for (int i = 0; i < nEff; ++i) {
            if (ImGui::Selectable(effLabels[i], effIdx == i)) {
                std::strncpy(s.reasoningEffort, effValues[i], sizeof(s.reasoningEffort) - 1);
                saveSettings(st.settings);   // the core reads config.json — persist immediately
            }
        }
        ImGui::EndPopup();
    }

    // ---- 3/5 paperclip (icon-only, square) ----
    ImGui::SameLine();
    {
        const bool attachClick = ImGui::Button("##attach", ImVec2(iconW, segH));
        divX[nDiv++] = ImGui::GetItemRectMax().x;
        if (attachClick) {
            if (std::getenv("CHUDWARE_NO_FILE_DIALOGS")) {
                st.toast(ui::ToastLevel::Info,
                         "File dialogs are disabled (CHUDWARE_NO_FILE_DIALOGS).");
            } else {
                std::string p = openFileDialog("All files (*.*)\0*.*\0");
                if (!p.empty() && ui::isImageAttachment(p)) {
                    g_imageInsert = ui::copilot::inlineImageMarker(st.pendingInlineImagePaths.size());
                    st.pendingInlineImagePaths.push_back(p);
                    g_imageUseRememberedSelection = true;
                } else if (!p.empty() &&
                           std::find(st.pendingAttachments.begin(), st.pendingAttachments.end(),
                                     p) == st.pendingAttachments.end()) {
                    st.pendingAttachments.push_back(p);
                    savePromptDraft(st, /*force=*/true);
                    ensureAttachmentContent(st, p);
                }
            }
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Attach a file — it rides your next message (text files are "
                              "inlined; PDF/Word/Excel/PowerPoint are read to text and "
                              "inlined; anything else is passed by path).");
        {
            ImDrawList* adl = ImGui::GetWindowDrawList();
            const ImVec2 amn = ImGui::GetItemRectMin(), amx = ImGui::GetItemRectMax();
            const ImVec2 ac((amn.x + amx.x) * 0.5f, (amn.y + amx.y) * 0.5f);
            const float isz = iconSz * 0.55f;
            IconUV clip = ui::getIcon("paperclip", 20);
            if (clip.valid) {
                const ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
                adl->AddImage(clip.texId,
                              ImVec2(ac.x - isz * 0.5f, ac.y - isz * 0.5f),
                              ImVec2(ac.x + isz * 0.5f, ac.y + isz * 0.5f),
                              clip.uv0, clip.uv1, ink);
            } else {
                // Fallback vector paperclip if the atlas cell isn't registered yet.
                const float ph = iconSz * 0.24f;
                const float pr = iconSz * 0.13f;
                const ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
                const float th = ui::metrics::stroke(1.4f);
                constexpr float kPi = 3.14159265f;
                adl->PathClear();
                adl->PathLineTo(ImVec2(ac.x + pr, ac.y - ph));
                adl->PathLineTo(ImVec2(ac.x + pr, ac.y + ph * 0.55f));
                adl->PathArcTo(ImVec2(ac.x, ac.y + ph * 0.55f), pr, 0.0f, kPi);
                adl->PathLineTo(ImVec2(ac.x - pr, ac.y - ph * 0.55f));
                adl->PathArcTo(ImVec2(ac.x, ac.y - ph * 0.55f), pr, kPi, 2.0f * kPi);
                adl->PathStroke(ink, 0, th);
                adl->AddLine(ImVec2(ac.x, ac.y - ph * 0.45f), ImVec2(ac.x, ac.y + ph * 0.45f),
                             ink, th);
            }
        }
    }

    // ---- 4/5 mic (live Chudcode speech-to-text) ----
    ImGui::SameLine();
    {
        voiceProbe(st);
        voicePoll(st);
        VoiceInputUi& v = voiceUi();
        v.animT += ImGui::GetIO().DeltaTime;
        const bool listening = v.state == VoiceUiState::Listening;
        const bool processing = v.state == VoiceUiState::Processing;
        const bool unavailable = v.state == VoiceUiState::Unavailable;
        const bool errored = v.state == VoiceUiState::Error;
        const bool disabled = unavailable || busy;
        if (listening) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.28f));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.40f));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.50f));
        } else if (errored) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 0.22f));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
                ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 0.34f));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive,
                ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 0.44f));
        }
        if (disabled) ImGui::BeginDisabled();
        const bool micClick = ImGui::Button("##voice_mic", ImVec2(iconW, segH));
        if (disabled) ImGui::EndDisabled();
        divX[nDiv++] = ImGui::GetItemRectMax().x;
        if (listening || errored) ImGui::PopStyleColor(3);
        if (micClick) {
            if (listening) voiceStop(st);
            else if (processing || v.rpcBusy) voiceCancel(st);
            else if (!unavailable) voiceStart(st);
        }
        // Right-click / Esc-while-hover cancels an active listen.
        if (listening && ImGui::IsItemHovered() &&
            (ImGui::IsMouseClicked(ImGuiMouseButton_Right) ||
             ImGui::IsKeyPressed(ImGuiKey_Escape))) {
            voiceCancel(st);
        }
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
            if (unavailable) {
                ImGui::SetTooltip("%s", v.reason.empty()
                    ? "Voice input unavailable — no microphone or speech model found."
                    : v.reason.c_str());
            } else if (listening) {
                ImGui::SetTooltip("Listening — click to stop & insert text (Esc/right-click cancels).");
            } else if (processing) {
                if (v.progress >= 0 && v.progress < 100)
                    ImGui::SetTooltip("Preparing speech model — %d%%. Click to cancel.",
                                      v.progress);
                else
                    ImGui::SetTooltip("Processing speech… click to cancel.");
            } else if (errored) {
                ImGui::SetTooltip("%s\nClick to retry.",
                                  v.reason.empty() ? "Voice input error." : v.reason.c_str());
            } else {
                const char* cacheHint = v.modelCached ? "model ready" : "downloads on first use";
                ImGui::SetTooltip(
                    "Dictate into the composer (%s — %s).\nClick to start; click again to stop.",
                    v.modelLabel.empty() ? "speech model" : v.modelLabel.c_str(), cacheHint);
            }
        }
        {
            ImDrawList* mdl = ImGui::GetWindowDrawList();
            const ImVec2 mmn = ImGui::GetItemRectMin(), mmx = ImGui::GetItemRectMax();
            const ImVec2 mc((mmn.x + mmx.x) * 0.5f, (mmn.y + mmx.y) * 0.5f);
            const float isz = iconSz * 0.55f;
            ImVec4 inkV = surf.textPrimary;
            if (unavailable) inkV = surf.textTertiary;
            else if (errored) inkV = ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1.0f);
            else if (listening) inkV = ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1.0f);
            const ImU32 ink = ImGui::ColorConvertFloat4ToU32(inkV);
            const float pulse = listening
                ? 0.5f + 0.5f * std::sin(v.animT * 4.0f)
                : (processing ? 0.5f + 0.5f * std::sin(v.animT * 6.0f) : 0.0f);
            drawMicGlyph(mdl, mc, isz, ink, pulse, v.level, listening || processing);
            if (listening) {
                mdl->AddRect(mmn, mmx,
                             ImGui::ColorConvertFloat4ToU32(
                                 ImVec4(pal.accent[0], pal.accent[1], pal.accent[2],
                                        0.55f + 0.30f * pulse)),
                             ui::metrics::rounding(4.0f), 0, ui::metrics::stroke(1.4f));
            }
        }
    }

    // ---- 5/5 context-cube (icon-only, square) ----
    ImGui::SameLine();
    {
        const bool armed = st.contextPickActive;
        // Armed state: soft accent fill (selection-well language).
        if (armed) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.28f));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.40f));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.50f));
        }
        if (ImGui::Button("##ctxcube", ImVec2(iconW, segH))) {
            st.contextPickActive = !st.contextPickActive;
        }
        if (armed) ImGui::PopStyleColor(3);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(armed
                ? "Finish picking (Esc) — chips ride your next message."
                : "Add model context — pick parts, faces, edges, sketches or features.");
        // Draw cube glyph (atlas) or a simple wireframe cube fallback.
        {
            ImDrawList* cdl = ImGui::GetWindowDrawList();
            const ImVec2 cmn = ImGui::GetItemRectMin(), cmx = ImGui::GetItemRectMax();
            const ImVec2 cc((cmn.x + cmx.x) * 0.5f, (cmn.y + cmx.y) * 0.5f);
            const float isz = iconSz * 0.55f;
            const ImU32 ink = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
            IconUV cube = ui::getIcon("context_cube", 20);
            if (cube.valid) {
                cdl->AddImage(cube.texId,
                              ImVec2(cc.x - isz * 0.5f, cc.y - isz * 0.5f),
                              ImVec2(cc.x + isz * 0.5f, cc.y + isz * 0.5f),
                              cube.uv0, cube.uv1, ink);
            } else {
                // Wireframe cube fallback (isometric-ish).
                const float s = isz * 0.42f;
                const float d = s * 0.45f;
                const ImVec2 f0(cc.x - s, cc.y - s * 0.25f);
                const ImVec2 f1(cc.x + s * 0.35f, cc.y - s * 0.25f);
                const ImVec2 f2(cc.x + s * 0.35f, cc.y + s * 0.75f);
                const ImVec2 f3(cc.x - s, cc.y + s * 0.75f);
                const ImVec2 b0(f0.x + d, f0.y - d);
                const ImVec2 b1(f1.x + d, f1.y - d);
                const ImVec2 b2(f2.x + d, f2.y - d);
                const float th = ui::metrics::stroke(1.3f);
                cdl->AddLine(f0, f1, ink, th); cdl->AddLine(f1, f2, ink, th);
                cdl->AddLine(f2, f3, ink, th); cdl->AddLine(f3, f0, ink, th);
                cdl->AddLine(b0, b1, ink, th); cdl->AddLine(b1, b2, ink, th);
                cdl->AddLine(f0, b0, ink, th); cdl->AddLine(f1, b1, ink, th);
                cdl->AddLine(f2, b2, ink, th);
            }
            if (armed) {
                cdl->AddRect(cmn, cmx,
                             ImGui::ColorConvertFloat4ToU32(
                                 ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.85f)),
                             ui::metrics::rounding(4.0f), 0, ui::metrics::stroke(1.5f));
            }
        }
    }

    // Vertical dividers at every shared segment edge, painted AFTER all fills, glyphs and state
    // rings so hover/active/armed/listening washes and the disabled mic never occlude them.
    // Surface-derived ink reads on both themes and any backdrop; the vertical inset keeps them
    // reading as separators rather than cell borders.
    {
        ImDrawList* ddl = ImGui::GetWindowDrawList();
        const ImU32 divInk = ImGui::ColorConvertFloat4ToU32(ImVec4(
            surf.textTertiary.x, surf.textTertiary.y, surf.textTertiary.z, 0.55f));
        const float inset = std::floor(segH * 0.22f);
        const float th = ui::metrics::stroke(1.0f);
        for (int i = 0; i < nDiv; ++i) {
            const float x = std::floor(divX[i]);
            ddl->AddLine(ImVec2(x, modelBtnMin.y + inset),
                         ImVec2(x, modelBtnMax.y - inset), divInk, th);
        }
    }

    ImGui::PopStyleColor(3);   // button base / hovered / active
    ImGui::PopStyleVar(4);     // FramePadding + ItemSpacing + FrameRounding + FrameBorderSize

    // Fixed drop-UP model picker: anchor the popup's BOTTOM-LEFT just above the model chip so
    // options propagate upward (composer sits at the bottom). ImGuiCond_Always + NoMove keep it
    // fixed to the chip rather than jumping with default below-item placement.
    {
        const float popW = std::max(180.0f * sc,
                                    std::min(300.0f * sc, ImGui::GetWindowWidth() - 16.0f * sc));
        const float gap = 4.0f * sc;
        ImGui::SetNextWindowSizeConstraints(ImVec2(popW, 0), ImVec2(popW, FLT_MAX));
        ImGui::SetNextWindowPos(ImVec2(modelBtnMin.x, modelBtnMin.y - gap),
                               ImGuiCond_Always, ImVec2(0.0f, 1.0f));
    }
    if (ImGui::BeginPopup("##modeldropdown",
                          ImGuiWindowFlags_NoMove | ImGuiWindowFlags_AlwaysAutoResize)) {
        drawModelDropdown(st, busy);
        ImGui::EndPopup();
    }
}

// Is the assistant currently working a turn? Covers every run path: job-based design/edit, the
// async ask/sim calls (which set the back message running without a job), and any streaming
// message. Drives the composer's send<->stop swap and the message queue.
// stopRequested is UI-terminal: once the user hits Stop we soft-finalize the bubble and must
// immediately leave the streaming chrome (stop button + busy mark). run->running may
// stay true until the worker unwinds — the drain discards that stale result and clears the flag.
// Everything here is scoped to the VIEWED conversation: a background run in another conversation
// must never make the visible composer look busy.
bool aiTurnActive(AppState& st) {
    auto* run = st.runIfExists(st.activeConvId());
    if (run && run->stopRequested.load()) return false;
    if (run && run->running.load()) return true;
    if (!st.chat.empty()) {
        const ChatMsg& b = st.chat.back();
        if (!b.fromUser && b.running) return true;
    }
    return false;
}

// ---- Agent control channel (cancel / steer) --------------------------------------
// ONE long-lived worker thread created at LOAD TIME (file-scope static), never a fresh
// std::thread per click: on Emscripten, thread creation BLOCKS the caller when the
// pthread pool (PTHREAD_POOL_SIZE=8) is exhausted — during an agent run the pool is
// busy, so a Stop click that spawned a thread wedged the whole browser page.
// AppState::log is a plain std::vector with NO internal locking (AppState::addLog does a bare
// push_back/erase) — it is read every frame by the console/log panel on the GL thread. The
// control-channel worker thread below used to call st->addLog(...) directly, racing that
// GL-thread push_back/read on every Stop/steer click and corrupting the vector (observed as a
// hard crash immediately after Stop). Fix: the worker thread only ever queues message strings
// (under the SAME mtx that already guards the job deque); agentControlDrainLogs() drains that
// queue into AppState::addLog() from the GL thread once per composer frame.
struct AgentControlChannel {
    // empty steer = cancel; attachments ride only with a steer. `answer` jobs carry an ask_user
    // batch resolution (answers, or declined) instead.
    struct Job {
        std::string runId;
        std::string provider;
        std::string steer;
        std::string steerId;        // the steer's user bubble (empty = no bubble to update)
        nlohmann::json attachments;
        bool answer = false;
        std::string convId, batchId;
        nlohmann::json answers;
        bool declined = false;
    };
    // A steer's or a Stop's delivery outcome, applied to the conversation's user bubbles on
    // the GL thread. A steer moves its own bubble; a Stop names the steers it withdrew unread.
    struct SteerResult {
        std::string convId, steerId;
        int state = STEER_NONE;
        bool cancel = false;
        std::vector<std::string> carried;
    };
    // An `answer` call's outcome, applied to its batch on the GL thread by agentControlDrainLogs.
    struct AnswerResult {
        std::string convId, batchId;
        bool declined = false;
        bool delivered = false;     // the worker accepted the answer
        std::string transportErr;   // the call itself failed (retryable)
        std::string reason;         // the worker refused it (no longer pending)
    };
    std::mutex mtx;
    std::condition_variable cv;
    std::deque<Job> q;
    std::deque<std::string> pendingLogs;   // GL-thread-only drain via agentControlDrainLogs()
    std::deque<AnswerResult> pendingAnswers;   // likewise
    std::deque<SteerResult> pendingSteers;     // likewise
    std::atomic<AppState*> st{nullptr};
    bool stopping = false; // guarded by mtx; shutdown runs on the UI thread
    std::thread thread;

    void pushLog(const std::string& msg) {
        std::lock_guard<std::mutex> lk(mtx);
        pendingLogs.push_back(msg);
    }

    void runAnswer(AppState& s, const Job& job) {
        AnswerResult res;
        res.convId = job.convId;
        res.batchId = job.batchId;
        res.declined = job.declined;
        nlohmann::json r;
        std::string e;
        try {
            s.worker.callAgentAnswer(job.runId, job.batchId, job.answers, job.declined, r, e,
                                     job.provider);
        } catch (const std::exception& ex) {
            e = ex.what();
        } catch (...) {
            e = "agent_answer exception";
        }
        if (!e.empty()) {
            res.transportErr = e;
        } else if (jbool(r, "ok") || jbool(r, "answered")) {
            res.delivered = true;
        } else {
            res.reason = jstr(r, "reason");
            if (res.reason.empty()) res.reason = jstr(r, "error");
            if (res.reason.empty()) res.reason = "question is no longer pending";
        }
        std::lock_guard<std::mutex> lk(mtx);
        pendingLogs.push_back("[..] agent_answer " + job.batchId + ": " +
                              (res.delivered ? std::string("ok")
                               : !res.transportErr.empty() ? res.transportErr : res.reason));
        pendingAnswers.push_back(std::move(res));
    }

    AgentControlChannel() : thread([this] {
            for (;;) {
                Job job;
                {
                    std::unique_lock<std::mutex> lk(mtx);
                    cv.wait(lk, [&] { return stopping || !q.empty(); });
                    if (q.empty()) return; // drain accepted jobs before releasing AppState
                    job = std::move(q.front());
                    q.pop_front();
                }
                AppState* s = st.load(std::memory_order_acquire);
                if (!s) continue;
                if (job.answer) {
                    runAnswer(*s, job);
                    continue;
                }
                nlohmann::json r;
                std::string e;
                if (job.steer.empty()) {
                    SteerResult res;
                    res.convId = job.convId;
                    res.cancel = true;
                    try {
                        s->worker.callAgentCancel(job.runId, r, e, job.provider);
                        if (!e.empty())
                            pushLog(std::string("[..] agent_cancel: ") + e);
                        else
                            pushLog("[..] agent_cancel ok cancelled=" +
                                    std::string(r.value("cancelled", false) ? "true" : "false"));
                    } catch (const std::exception& ex) {
                        e = ex.what();
                        pushLog(std::string("[..] agent_cancel exception: ") + ex.what());
                    } catch (...) {
                        e = "agent_cancel exception";
                        pushLog("[..] agent_cancel exception");
                    }
                    if (e.empty()) {
                        const auto ids = r.find("carried_steer_ids");
                        if (ids != r.end() && ids->is_array())
                            for (const auto& id : *ids)
                                if (id.is_string()) res.carried.push_back(id.get<std::string>());
                        std::lock_guard<std::mutex> lk(mtx);
                        pendingSteers.push_back(std::move(res));
                    }
                } else {
                    SteerResult res;
                    res.convId = job.convId;
                    res.steerId = job.steerId;
                    try {
                        s->worker.callAgentSteer(job.runId, job.steer, job.attachments,
                                                 job.steerId, r, e, job.provider);
                    } catch (const std::exception& ex) {
                        e = ex.what();
                    } catch (...) {
                        e = "agent_steer exception";
                    }
                    if (!e.empty()) {
                        pushLog(std::string("[..] agent_steer: ") + e);
                        res.state = STEER_FAILED;
                    } else if (!r.value("steered", false)) {
                        // Run finished between submit and delivery — tell the user their
                        // text was NOT consumed rather than silently dropping it.
                        pushLog("[..] agent_steer: run already finished "
                                "(steering not delivered — send again to continue)");
                        res.state = STEER_FAILED;
                    } else if (r.contains("answered_question")) {
                        // The message answered the agent's open question form directly.
                        pushLog("[..] agent_steer ok (answered the open question)");
                        res.state = STEER_DELIVERED;
                    } else if (r.value("duplicate", false)) {
                        // Identical to a message the run already holds; that one is tracked.
                        pushLog("[..] agent_steer: duplicate of a message already sent");
                        res.state = STEER_NONE;
                    } else {
                        pushLog("[..] agent_steer ok (delivered at next step)");
                        res.state = STEER_QUEUED;
                    }
                    if (!res.steerId.empty()) {
                        std::lock_guard<std::mutex> lk(mtx);
                        pendingSteers.push_back(std::move(res));
                    }
                }
            }
        }) {}
    ~AgentControlChannel() { shutdown(); }
    void shutdown() {
        {
            std::lock_guard<std::mutex> lk(mtx);
            stopping = true;
        }
        cv.notify_one();
        if (thread.joinable()) thread.join();
        st.store(nullptr, std::memory_order_release);
    }
};
static AgentControlChannel g_agentCtl;

// One `answer` outcome onto its batch. A delivered answer resolves the card; a failed call puts
// it back to pending with the reason (retry, or reply in the chat); a refusal means the worker
// no longer holds the question, so the card says it expired.
static void applyAnswerResult(AppState& st, const AgentControlChannel::AnswerResult& res) {
    std::vector<ChatMsg>* msgs = conversationMsgs(st, res.convId);
    if (!msgs) return;
    ChatMsg* owner = nullptr;
    AskBatch* b = findAskBatch(*msgs, res.batchId, &owner);
    // agent.answer may already have resolved it (it is the authoritative record).
    if (!b || b->status != AskStatus::Submitting) return;
    if (res.delivered) {
        b->status = res.declined ? AskStatus::Declined : AskStatus::Answered;
        b->error.clear();
    } else if (!res.transportErr.empty()) {
        b->status = AskStatus::Pending;
        b->error = "Couldn't deliver your answers (" + res.transportErr +
                   "). Try again, or reply in the chat.";
    } else {
        b->status = AskStatus::Expired;
        b->error = res.reason;
    }
    owner->uiCachedH = -1.0f;
    markChatHistoryDirty();
}

// One steer/Stop outcome onto the conversation's user bubbles. A Stop names the steers it
// withdrew unread (they go with the next message). Silence about another steer is not
// consumption evidence; only its streamed delivery acknowledgement can mark it read.
static void applySteerResult(AppState& st, const AgentControlChannel::SteerResult& res) {
    std::vector<ChatMsg>* msgs = conversationMsgs(st, res.convId);
    if (!msgs) return;
    if (!res.cancel) {
        if (res.state == STEER_NONE) {
            for (auto& m : *msgs)
                if (m.fromUser && m.steerId == res.steerId && m.steerState == STEER_SENDING) {
                    m.steerState = STEER_NONE;
                    m.uiCachedH = -1.0f;
                    markChatHistoryDirty();
                }
            return;
        }
        setSteerState(*msgs, res.steerId, res.state);
        return;
    }
    for (const auto& id : res.carried) setSteerState(*msgs, id, STEER_CARRIED);
}

// GL-thread drain of the control channel's queued log lines and answer/steer outcomes — the
// only safe place to touch AppState::log / ChatMsg from this subsystem. Cheap no-op when nothing
// queued; call once per composer frame (covers both the docked Copilot and the AI-tab
// workspace host, since both draw the composer every visible frame).
static void agentControlDrainLogs(AppState& st) {
    for (;;) {
        std::string msg;
        {
            std::lock_guard<std::mutex> lk(g_agentCtl.mtx);
            if (g_agentCtl.pendingLogs.empty()) break;
            msg = std::move(g_agentCtl.pendingLogs.front());
            g_agentCtl.pendingLogs.pop_front();
        }
        st.addLog(msg);
    }
    for (;;) {
        AgentControlChannel::AnswerResult res;
        {
            std::lock_guard<std::mutex> lk(g_agentCtl.mtx);
            if (g_agentCtl.pendingAnswers.empty()) break;
            res = std::move(g_agentCtl.pendingAnswers.front());
            g_agentCtl.pendingAnswers.pop_front();
        }
        applyAnswerResult(st, res);
    }
    for (;;) {
        AgentControlChannel::SteerResult res;
        {
            std::lock_guard<std::mutex> lk(g_agentCtl.mtx);
            if (g_agentCtl.pendingSteers.empty()) return;
            res = std::move(g_agentCtl.pendingSteers.front());
            g_agentCtl.pendingSteers.pop_front();
        }
        applySteerResult(st, res);
    }
}

void shutdownAgentControl(AppState& st) {
    g_agentCtl.shutdown();
    agentControlDrainLogs(st);
}

#ifdef __EMSCRIPTEN__
static std::string launchedRunProvider(AppState& st, const std::string& handle) {
    for (const auto& entry : st.runs) {
        if (!entry.second) continue;
        std::lock_guard<std::mutex> lock(entry.second->mtx);
        if (entry.second->runId == handle) return entry.second->provider;
    }
    return {};
}
#endif

void agentControlDispatch(AppState& st, const std::string& runId, const std::string& steer,
                          nlohmann::json attachments, const std::string& steerId,
                          const std::string& convId) {
#ifdef __EMSCRIPTEN__
    if (runId.empty()) return;
#endif
    g_agentCtl.st.store(&st, std::memory_order_release);
    {
        std::lock_guard<std::mutex> lk(g_agentCtl.mtx);
        AgentControlChannel::Job job;
        job.runId = runId;
#ifdef __EMSCRIPTEN__
        job.provider = launchedRunProvider(st, runId);
#endif
        job.steer = steer;
        job.steerId = steerId;
        job.convId = convId.empty() ? st.activeConvId() : convId;
        job.attachments = std::move(attachments);
        g_agentCtl.q.push_back(std::move(job));
    }
    g_agentCtl.cv.notify_one();
}

void agentAnswerDispatch(AppState& st, const std::string& runId, const std::string& batchId,
                         nlohmann::json answers, bool declined) {
    g_agentCtl.st.store(&st, std::memory_order_release);
    {
        std::lock_guard<std::mutex> lk(g_agentCtl.mtx);
        AgentControlChannel::Job job;
        job.runId = runId;
#ifdef __EMSCRIPTEN__
        if (runId.empty()) return;
        job.provider = launchedRunProvider(st, runId);
#endif
        job.answer = true;
        job.convId = st.activeConvId();
        job.batchId = batchId;
        job.answers = std::move(answers);
        job.declined = declined;
        g_agentCtl.q.push_back(std::move(job));
    }
    g_agentCtl.cv.notify_one();
}

// Stop-finalization is cosmetic only: the JobState remains running until its real terminal,
// so document locks cannot be released while cancellation is still unwinding.
// Stop-finalize one turn's activity rows: streaming thought boxes collapse (unless pinned),
// stage/tool rows leave "running" so no glyph animates a turn that is no longer live.
static void finalizeStoppedTurnActivity(ChatMsg& m) {
    for (auto& a : m.activity) {
        if (a.rowKind == 2 && (a.streaming || a.status == "running" || a.status.empty()))
            finalizeThoughtBox(a);
        else if (a.streaming || a.status == "running" || a.status.empty()) {
            a.status = "done";
            a.streaming = false;
        }
    }
}

// A steer lands mid-run, so the transcript must read in the order things happened: the turn
// so far, the user's message, then everything the run does after it. The event drain and the
// result drain (app.cpp) both feed the LAST running assistant message; appending the steer
// alone left the rest of the run, final reply included, streaming into the bubble ABOVE it
// with nothing live below. Close the current bubble and open a fresh running one under the
// steer instead. Append-only: AI-VC msg_idx anchors must never shift.
static void appendSteerToTranscript(AppState& st, const std::string& text,
                                    const nlohmann::json& attachments,
                                    std::vector<core::ContextRef> contextRefs,
                                    const std::string& steerId) {
    ChatMsg* live = nullptr;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (!it->fromUser && it->running) { live = &*it; break; }
    const int mode = live ? live->copilotMode : st.aiInteraction;
    const std::string model = live ? live->model : std::string();
    if (live) {
        finalizeStoppedTurnActivity(*live);
        live->running = false;
        live->liveStatus.clear();
        live->steerSplit = true;
        live->uiCachedH = -1.0f;
    }
    ChatMsg u; u.fromUser = true; u.text = text;
    u.imagePaths = userImagePaths(attachments);
    u.contextRefs = std::move(contextRefs);
    u.copilotMode = mode;
    u.steerId = steerId;
    u.steerState = STEER_SENDING;
    st.chat.push_back(std::move(u));
    appendChatRawMessage(st, st.chat.back());
    if (live) {
        ChatMsg a;
        a.running = true;
        a.copilotMode = mode;
        a.model = model;
        a.liveStatus = "Thinking\xe2\x80\xa6";
        st.chat.push_back(std::move(a));
        appendChatRawMessage(st, st.chat.back());
    }
    chatLogResetStick();
    markChatHistoryDirty();
    maybePersistChatHistoryDebounced(st, /*force=*/true);
}

void requestStopRunFor(AppState& st, const std::string& convId) {
    auto* run = st.runIfExists(convId);
    if (!run || run->stopRequested.load()) return;
    auto* msgs = conversationMsgs(st, convId);
    // Capture run_id before we clear flags.
    std::string runId;
#ifdef __EMSCRIPTEN__
    runId = requestCellAgentStop(*run);
#else
    {
        std::lock_guard<std::mutex> lk(run->mtx);
        runId = run->runId;
    }
#endif
    // Fire agent_cancel on the persistent control channel so we never block the GL thread
    // behind the worker mutex / daemon socket (callAgentCancel bypasses callMtx_ / uses a
    // side socket) — and never create a thread per click (WASM pthread-pool exhaustion
    // blocked thread creation on the main thread = frozen page).
    // Web Stop before mint stays on this job; publishing its handle sends the
    // exact cancel. Desktop retains its established early all-run cancellation.
#ifdef __EMSCRIPTEN__
    const bool canCancel = !runId.empty();
#else
    const bool canCancel = run->running.load() || !runId.empty();
#endif
    if (st.workerStarted && canCancel) {
        try {
            agentControlDispatch(st, runId, /*steer=*/"", nlohmann::json::array(),
                                 std::string(), convId);
        } catch (...) {
            st.addLog("[..] agent_cancel dispatch failed");
        }
    }
    // Only a job-based run produces a result the drain must discard. Async ask/sim runs land via
    // their own callbacks, which look for a *running* message — clearing it below makes them no-op.
    if (run->running.load() || run->ready.load()) run->stopRequested = true;
    // Stopping drops the run's remaining events, so its open questions never hear back: close
    // them here (the worker's own agent.answer {cancelled} is folded idempotently if it lands).
    if (msgs) closeOpenAsks(*msgs);
    if (convId == st.activeConvId()) {
        st.livePhase.clear();
        st.agentV2Status.clear();
    }
    if (msgs) for (auto it = msgs->rbegin(); it != msgs->rend(); ++it)
        if (!it->fromUser && it->running) {
            it->running = false;
            it->liveStatus = "Stopped";
            it->errKind = ERR_NONE;  // user stop is not an error card
            // Finalize any live stream boxes so the busy mark / streaming chrome stops.
            finalizeStoppedTurnActivity(*it);
            if (it->text.empty())
                it->text = "Stopped.";
            else if (it->text.find("(Stopped by you.)") == std::string::npos)
                it->text += "\n\n(Stopped by you.)";
            break;
        }
    // Never leave a running bubble after stop — and never leave its rows mid-animation:
    // every OTHER still-running message (subagent/parked turns the sweep above didn't reach)
    // gets the same finalize, or its stage cards keep spinning forever with no live turn.
    if (msgs) for (auto& m : *msgs)
        if (!m.fromUser && m.running) {
            m.running = false;
            finalizeStoppedTurnActivity(m);
        }
    // Cosmetic global flag: other conversations may still be running, so recompute it instead
    // of forcing it false.
    st.agentV2Running = st.anyRunActive();
    st.status = "Stopped.";
    st.statusKind = 1;
    st.addLog("[..] stop requested by user");
    markChatHistoryDirty();
    s_chatHistoryLastPersist = -1e9;
    maybePersistChatHistoryDebounced(st, /*force=*/true);
}

void requestStopRun(AppState& st) {
    // Composer Stop targets only the viewed conversation; document-lock chrome can name others.
    requestStopRunFor(st, st.activeConvId());
}

// After a send the backing buffer is cleared while the InputText is still ACTIVE — ImGui
// keeps its own edit state for the active widget and ignores the external clear, so the
// prompt stayed in the box. Bump a generation into the widget id on send so the widget
// restarts from the (empty) buffer; refocus it so the user can keep typing.
static int  s_composerGen = 0;
static bool s_refocusComposer = false;

void composerSetText(AppState& st, const std::string& text) {
    if (st.prompt.size() < text.size() + 1) st.prompt.resize(text.size() + 256, 0);
    std::memset(st.prompt.data(), 0, st.prompt.size());
    std::memcpy(st.prompt.data(), text.data(), text.size());
    composerResetInputState(st);
    if (text.empty()) clearPromptDraft(st);
}

void composerResetInputState(AppState& st) {
    if (!st.prompt.empty()) {
        g_wrap.erase(st.prompt.data());
        seedHardBreaks(g_wrap[st.prompt.data()], st.prompt.data(),
                       (int)std::strlen(st.prompt.data()));
    }
    g_imageInsert.clear();
    g_imageRemovals.clear();
    g_composerCursor = g_composerSelectionStart = g_composerSelectionEnd = 0;
    g_imageUseRememberedSelection = false;
    ++s_composerGen;
    s_refocusComposer = true;
}

// The composer buffer, unwrapped and trimmed — the ONE definition of "is there a message".
// The rewrapper toggles spaces<->newlines as the user types, so a buffer holding nothing but
// whitespace still satisfied `prompt[0] != 0` and Enter shipped an empty user bubble to the
// model (live 2026-07-28: three blank "You" turns in a row). Every send / queue / steer path
// gates on this instead, and it doubles as the soft-break flattener on the way out — SOFT
// only: a break the user typed with Alt/Shift+Enter is part of their message and survives
// into the prompt, while emptiness is still judged on the text around those breaks.
std::string composerText(const AppState& st) {
    if (st.prompt.empty()) return {};
    std::string out(st.prompt.data());
    const auto it = g_wrap.find(st.prompt.data());
    const std::vector<int>* hard = it == g_wrap.end() ? nullptr : &it->second.hard;
    for (size_t i = 0; i < out.size(); ++i) {
        char& c = out[i];
        if (c == '\n' && hard && std::binary_search(hard->begin(), hard->end(), (int)i))
            continue;
        if (c == '\n' || c == '\r' || c == '\t') c = ' ';
    }
    const size_t b = out.find_first_not_of(" \n");
    if (b == std::string::npos) return {};
    return out.substr(b, out.find_last_not_of(" \n") - b + 1);
}

// The send/stop control, drawn INSIDE the message input box at its bottom-right (flagship
// composer pattern — the input reserves right padding so text never runs under it). Idle: a
// rounded accent button with an enter/return-key glyph (Enter submits). During a turn the same
// button becomes the stop control (square glyph, statusErr fill).
static void drawComposerSendStop(AppState& st, const ImVec2& inMin, const ImVec2& inMax,
                                 float rightEdgeX, bool canRun, bool canStart, bool active,
                                 bool& submit, bool& stopReq) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const float sz = ImGui::GetFrameHeight();
    const float inset = 6.0f * sc;   // clear of the composer's rounded border
    // Sit fully inside the input: keep a bottom inset so the button never intersects the
    // toolbar strip below the box. Short (one-row) inputs still center vertically.
    const float bottomClear = 6.0f * sc;
    float y0 = inMax.y - sz - bottomClear;
    if (y0 < inMin.y + inset) y0 = (inMin.y + inMax.y - sz) * 0.5f;
    const ImVec2 bMin(rightEdgeX - inset - sz, y0);
    const ImVec2 bMax(bMin.x + sz, bMin.y + sz);

    const ImVec2 keep = ImGui::GetCursorPos();
    ImGui::SetCursorScreenPos(bMin);
    const bool clicked = ImGui::InvisibleButton("##cp_sendstop", ImVec2(sz, sz));
    const bool hov = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float round = sz * 0.5f;   // a circle: the send/stop control reads as ONE action
    const ImVec2 c((bMin.x + bMax.x) * 0.5f, (bMin.y + bMax.y) * 0.5f);

    if (active) {
        // Stop: statusErr fill with derived hover weight (no color literals).
        ImVec4 fill(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1);
        if (hov) fill = ImVec4(std::min(fill.x * 1.12f, 1.0f), std::min(fill.y * 1.12f, 1.0f),
                               std::min(fill.z * 1.12f, 1.0f), 1);
        dl->AddRectFilled(bMin, bMax, ImGui::ColorConvertFloat4ToU32(fill), round);
        ImVec4 stopInk = ui::maxContrastTextOn(fill);
        stopInk.w = 0.92f;
        const float h = sz * 0.18f;
        dl->AddRectFilled(ImVec2(c.x - h, c.y - h), ImVec2(c.x + h, c.y + h),
                          ImGui::ColorConvertFloat4ToU32(stopInk), 1.5f * sc);
        if (hov) {
            auto* stopping = st.runIfExists(st.activeConvId());
            ImGui::SetTooltip(stopping && stopping->stopRequested.load()
                                  ? "Stopping\xE2\x80\xA6 (finishing current step)"
                                  : "Stop the current run");
        }
        if (clicked) stopReq = true;
    } else {
        // Send: an up-arrow in a circle. Sendable = solid accent (eased in as the message
        // becomes sendable); not yet = a faint ink disc so the empty box shows no loud button.
        const ImVec4 accC(pal.accent[0], pal.accent[1], pal.accent[2], 1);
        const float live = ui::anim::toward(ImGui::GetID("##cp_send_live"),
                                            canStart ? 1.0f : 0.0f, ui::anim::kFast);
        const ui::AdaptiveSurface& ssurf = chatSurf();
        const ImVec4 idle(ssurf.textPrimary.x, ssurf.textPrimary.y, ssurf.textPrimary.z, 0.10f);
        const ImVec4 on = hov && canStart
            ? ImVec4(pal.accentDim[0], pal.accentDim[1], pal.accentDim[2], 1) : accC;
        const ImVec4 fill(idle.x + (on.x - idle.x) * live, idle.y + (on.y - idle.y) * live,
                          idle.z + (on.z - idle.z) * live, idle.w + (on.w - idle.w) * live);
        dl->AddCircleFilled(c, round, ImGui::ColorConvertFloat4ToU32(fill));
        // Ink pairs with the button's own fill (same rule as before).
        const ImVec4 onInk = ui::maxContrastTextOn(accC);
        const ImVec4 offInk = ssurf.textTertiary;
        const ImU32 ink = ImGui::ColorConvertFloat4ToU32(
            ImVec4(offInk.x + (onInk.x - offInk.x) * live, offInk.y + (onInk.y - offInk.y) * live,
                   offInk.z + (onInk.z - offInk.z) * live, offInk.w + (onInk.w - offInk.w) * live));
        const float g  = sz * 0.22f;
        const float th = ui::metrics::stroke(1.7f);
        dl->AddLine(ImVec2(c.x, c.y + g), ImVec2(c.x, c.y - g), ink, th);   // stem
        const float a = g * 0.75f;                                         // arrowhead, UP
        dl->AddLine(ImVec2(c.x, c.y - g), ImVec2(c.x - a, c.y - g + a), ink, th);
        dl->AddLine(ImVec2(c.x, c.y - g), ImVec2(c.x + a, c.y - g + a), ink, th);
        if (hov)
            ImGui::SetTooltip(canStart ? "Send  (Enter)"
                              : canRun ? "Type a message first."
                                       : "Runtime/worker not ready yet.");
        if (clicked && canStart) submit = true;
    }
    ImGui::SetCursorPos(keep);
}

// ---- the composer: a rounded input box (in-box send) + inline toolbar (model, thinking, +) ----
// Submits on Enter or the in-box send button. Returns nothing; runs the turn directly.
void drawComposer(AppState& st, bool busy, bool canRun) {
    // GL-thread-only drain of Stop/steer log lines queued by the control-channel worker
    // thread (see AgentControlChannel above) — must run every visible frame, not just on
    // Stop, so nothing queued right before a frame boundary is lost.
    agentControlDrainLogs(st);
    // Forging busy affordance lives in drawComposerPrelude (inline between mode
    // dropdown and context meter) — no reserved row above the input.


    // One-shot: seed live mode from persisted settings after loadSettings (legacy values fold
    // onto Agent/Plan/Ask), then keep the live value normalized and mirrored into settings.
    static bool s_modeSeeded = false;
    if (!s_modeSeeded) {
        s_modeSeeded = true;
        st.aiInteraction = normalizeCopilotMode(st.settings.copilotMode);
    }
    st.aiInteraction = normalizeCopilotMode(st.aiInteraction);
    st.settings.copilotMode = st.aiInteraction;
    // Every mode is one streaming Chudcode agent turn — no one-shot RPC lane left.
    st.agentV2 = true;
    // Screenshot hook, same spirit as CHUDWARE_CHAT_MODEMENU: seed the composer once with
    // CHUDWARE_CHAT_PROMPT (focused, so the magic-keyword shimmer is live) for headless
    // verification of the keyword presentation. No-op in normal use.
    static bool s_shotPromptSeeded = false;
    if (!s_shotPromptSeeded) {
        s_shotPromptSeeded = true;
        if (const char* shotPrompt = std::getenv("CHUDWARE_CHAT_PROMPT"))
            composerSetText(st, shotPrompt);
    }

    // Esc: close slash autocomplete first, then staged edit restore, else disarm context pick.
    if (ImGui::IsKeyPressed(ImGuiKey_Escape, false)) {
        if (s_slash.open) {
            // Latch the dismissal on the current query — drawSlashAutocomplete
            // recomputes `open` from the buffer each frame and would reopen
            // instantly (editing the query re-arms it there).
            s_slash.dismissed = true;
            s_slash.dismissedQuery = s_slash.query;
            s_slash.open = false;
            s_slash.matches.clear();
            g_slashMenuActive = false;
        } else if (editStageHoldsConversation(st.activeConvId())) {
            cancelEditStage(st);
        } else if (st.contextPickActive) {
            st.contextPickActive = false;
        }
    }

    // Staged edit transaction: start a requested edit once the run has truly settled, and
    // launch a committed edit's single turn when its conversation is the viewed one.
    if (canRun) editStageTick(st);

    // Drain the message queue: once the assistant is free, auto-send the next queued message
    // (Claude-Code-style). Done before computing `active` so the new turn shows as running now.
    // Also require the viewed conversation's run to be idle: after Stop, aiTurnActive is false
    // (stopRequested terminal) but the cancelled run may still be unwinding — don't launch a
    // queued turn on top of it. Another conversation's run is irrelevant here. A staged edit
    // of this conversation holds the queue: nothing may append under the edit point, and an
    // in-flight checkpoint restore truncates the chat when it lands, so no turn starts under it.
    if (!aiTurnActive(st) && !st.convRunning(st.activeConvId()) && !st.queuedPrompts.empty() &&
        canRun && !s_aiVc.busy && !editStageHoldsConversation(st.activeConvId())) {
        const std::string next = st.queuedPrompts.front();
        // Queue snapshots never borrow attachments from a newer unsent draft.
        auto files = std::move(st.pendingAttachments);
        auto images = std::move(st.pendingInlineImagePaths);
        auto contexts = std::move(st.pendingContextRefs);
        st.pendingAttachments.clear();
        st.pendingInlineImagePaths.clear();
        st.pendingContextRefs.clear();
        if (!st.queuedPromptDrafts.empty()) {
            const auto& draft = st.queuedPromptDrafts.front();
            st.pendingAttachments = draft.value("attachments", std::vector<std::string>{});
            st.pendingInlineImagePaths = draft.value("inline_images", std::vector<std::string>{});
            st.pendingContextRefs = core::contextRefsFromJson(
                draft.value("context_refs", nlohmann::json::array()));
            st.queuedPromptDrafts.erase(st.queuedPromptDrafts.begin());
        }
        st.queuedPrompts.erase(st.queuedPrompts.begin());
        submitTurn(st, next);
        st.pendingAttachments = std::move(files);
        st.pendingInlineImagePaths = std::move(images);
        st.pendingContextRefs = std::move(contexts);
    }

    const bool active = aiTurnActive(st);
    // A staged edit whose send is pending or being applied owns the composer: its text IS the
    // edit, so the box is read-only and nothing else can be sent from it meanwhile.
    const bool editFrozen = editStageHoldsConversation(st.activeConvId()) &&
                            (editStageInFlight() || s_edit.sendRequested ||
                             s_edit.phase == EditStage::Phase::AwaitSubmit);
    bool canStart = canRun && (!composerText(st).empty() || !st.pendingAttachments.empty() ||
                              !st.pendingContextRefs.empty()) && !active &&
                    !editFrozen && !s_aiVc.busy;
    bool submit   = false;   // send / queue requested this frame
    bool stopReq  = false;   // stop requested this frame

    // ---- queued messages (shown above the composer: send-now-and-interrupt, or remove) ----
    if (!st.queuedPrompts.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textSecondary);
        for (size_t qi = 0; qi < st.queuedPrompts.size(); ++qi) {
            ImGui::PushID((int)qi + 9000);
            float xBtn = ImGui::GetFrameHeight();
            std::string& q = st.queuedPrompts[qi];
            std::string label = "Queued: " + (q.size() > 48 ? q.substr(0, 47) + "\xE2\x80\xA6" : q);
            ImGui::TextUnformatted(label.c_str());
            ImGui::SameLine(ImGui::GetContentRegionMax().x - xBtn);
            if (ImGui::SmallButton("x")) {
                st.queuedPrompts.erase(st.queuedPrompts.begin() + qi);
                if (qi < st.queuedPromptDrafts.size())
                    st.queuedPromptDrafts.erase(st.queuedPromptDrafts.begin() + qi);
                markChatHistoryDirty();
                ImGui::PopID();
                break;
            }
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Remove from queue");
            // Send now: interrupt the live run so the drain at the top of the next frame
            // launches THIS message instead of waiting for the turn to finish on its own.
            ImGui::SameLine(ImGui::GetContentRegionMax().x - xBtn * 2.0f);
            if (ImGui::SmallButton("\xE2\x96\xB6")) {   // ▶
                if (qi != 0) std::rotate(st.queuedPrompts.begin(),
                                         st.queuedPrompts.begin() + (long)qi,
                                         st.queuedPrompts.begin() + (long)qi + 1);
                if (qi != 0 && qi < st.queuedPromptDrafts.size())
                    std::rotate(st.queuedPromptDrafts.begin(),
                                st.queuedPromptDrafts.begin() + qi,
                                st.queuedPromptDrafts.begin() + qi + 1);
                markChatHistoryDirty();
                if (active) requestStopRun(st);
                ImGui::PopID();
                break;
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Send now \xE2\x80\x94 interrupts the current run");
            ImGui::PopID();
        }
        ImGui::PopStyleColor();
    }

    // One prune per frame, ahead of the chip row: drops document extracts whose file is no
    // longer attached, whichever path removed it (the detach ×, a send that consumed the
    // chips, a conversation switch that cleared them). Cheap — the chip row is a handful.
    pruneAttachInfo(st);
    // Context chips follow their documents (rebind by path after a restart).
    reconcileContextRefDocs(st);

    // ---- chips above the input: file attachments (M23) + context refs (Wave4) ----
    if (!st.pendingAttachments.empty() || !st.pendingInlineImagePaths.empty() ||
        !st.pendingContextRefs.empty()) {
        const ui::AdaptiveSurface& asurf = chatSurf();
        const float sc = ui::uiScale;
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::rounding(6.0f));
        ImGui::PushStyleColor(ImGuiCol_Text, asurf.textPrimary);
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(asurf.textPrimary.x, asurf.textPrimary.y, asurf.textPrimary.z, 0.08f));
        const float maxX = ImGui::GetContentRegionMax().x;
        bool anyDrawn = false;

        auto placeChip = [&](float chipW, float gap) {
            if (anyDrawn) {
                ImGui::SameLine(0, gap);
                if (ImGui::GetCursorPosX() + chipW > maxX) ImGui::NewLine();
            }
            anyDrawn = true;
        };

        // Attachment chips are THREE hit targets, not one button: for an image the thumbnail
        // and the name both OPEN the viewer, and ONLY the × detaches. (As one button the chip
        // destroyed the attachment on any click, so there was no way to look at what you had
        // attached.) Being several widgets, the chip can no longer borrow ImGuiCol_Button for
        // its fill — paint it on a lower draw channel so it lands behind the content.
        ImDrawList* chipDl = ImGui::GetWindowDrawList();
        ImDrawListSplitter chipSplit;
        chipSplit.Split(chipDl, 2);
        chipSplit.SetCurrentChannel(chipDl, 1);
        const ImU32 chipFillCol = ImGui::ColorConvertFloat4ToU32(
            ImVec4(asurf.textPrimary.x, asurf.textPrimary.y, asurf.textPrimary.z, 0.08f));
        const ImU32 chipEdgeCol = ImGui::ColorConvertFloat4ToU32(
            ImVec4(asurf.textPrimary.x, asurf.textPrimary.y, asurf.textPrimary.z, 0.35f));
        const ImU32 chipInkCol  = ImGui::ColorConvertFloat4ToU32(asurf.textPrimary);
        const ImU32 chipDimCol  = ImGui::ColorConvertFloat4ToU32(
            ImVec4(asurf.textPrimary.x, asurf.textPrimary.y, asurf.textPrimary.z, 0.55f));
        const float chipPadX = 6 * sc, chipPadY = 3 * sc;

        const size_t inlineCount = st.pendingInlineImagePaths.size();
        const auto visibleRefs = inlineCount
            ? ui::copilot::inlineImageRefs(composerText(st), inlineCount)
            : std::vector<ui::copilot::InlineImageRef>{};
        for (size_t ai = 0; ai < inlineCount + st.pendingAttachments.size(); ++ai) {
            const bool inlineImage = ai < inlineCount;
            const std::string apath = inlineImage ? st.pendingInlineImagePaths[ai]
                                                  : st.pendingAttachments[ai - inlineCount];
            if (apath.empty()) continue;
            if (inlineImage && std::none_of(visibleRefs.begin(), visibleRefs.end(),
                [ai](const auto& ref) { return ref.image == ai; })) continue;
            ImGui::PushID((int)ai + 12000);
            const bool isImg = inlineImage || ui::isImageAttachment(apath);
            std::string name = inlineImage ? "#" + std::to_string(ai + 1)
                                           : attachmentBaseName(apath);
            if (name.size() > 30) name = name.substr(0, 29) + "\xE2\x80\xA6";
            const float lineH   = ImGui::GetTextLineHeight();
            const float thumbSz = lineH * 2.0f;            // about two text lines tall
            const float rowH    = isImg ? thumbSz : lineH;
            // Reconcile every frame, not only on attach: this is what picks up a draft
            // restored by conversations.cpp (which has no hook into the composer) and a
            // document attached before the worker was up. Idempotent by design.
            if (!isImg) ensureAttachmentContent(st, apath);
            const AttachInfo* ainfo = isImg ? nullptr : attachInfoFor(apath);
            // The badge is load-bearing, not decoration: without it a document whose text was
            // dropped looks exactly like one that was read.
            const std::string badge = attachChipBadge(st, apath, ainfo);
            const std::string badgeText =
                badge.empty() ? std::string() : "  \xC2\xB7  " + badge;
            const float nameW   = ImGui::CalcTextSize(name.c_str()).x;
            const float badgeW  = badgeText.empty()
                                      ? 0.0f : ImGui::CalcTextSize(badgeText.c_str()).x;
            const float labelW  = nameW + badgeW;
            const float xBtnW   = ImGui::CalcTextSize("\xC3\x97").x +
                                  ImGui::GetStyle().FramePadding.x * 2.0f;
            const float chipW   = chipPadX * 2.0f + (isImg ? thumbSz + 4 * sc : 0.0f) +
                                  labelW + 6 * sc + xBtnW;
            placeChip(chipW, 14 * sc);   // wider gap than the ref chips: these fills are inflated

            bool view = false, detach = false;
            ImGui::BeginGroup();
            const float rowTop = ImGui::GetCursorPosY();
            if (isImg) {
                const bool thumbClicked = ui::attachmentThumbnail(apath, thumbSz);
                const bool thumbHovered = ImGui::IsItemHovered();
                if (thumbClicked) view = true;
                if (thumbHovered) {
                    ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
                    ImGui::SetTooltip("%s\nClick to view.", apath.c_str());
                }
                ImGui::SameLine(0, 4 * sc);
                ImGui::SetCursorPosY(rowTop);
            }
            // The name is an InvisibleButton + hand-drawn text rather than a Text item so it
            // hit-tests reliably while the composer input holds ImGui's active id.
            ImGui::InvisibleButton("##attname", ImVec2(labelW, rowH));
            const bool nameClicked = ImGui::IsItemClicked();
            const bool nameHovered = ImGui::IsItemHovered();
            const ImVec2 nameMin = ImGui::GetItemRectMin();
            chipDl->AddText(ImVec2(nameMin.x, nameMin.y + (rowH - lineH) * 0.5f),
                            chipInkCol, name.c_str());
            if (!badgeText.empty())
                chipDl->AddText(ImVec2(nameMin.x + nameW, nameMin.y + (rowH - lineH) * 0.5f),
                                chipDimCol, badgeText.c_str());
            if (isImg) {
                if (nameClicked) view = true;
                if (nameHovered) {
                    ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
                    ImGui::SetTooltip("%s\nClick to view.", apath.c_str());
                }
            } else if (nameHovered) {
                // Deliberately inert: detaching is the × alone. The tooltip is where the
                // full truth about this file's content lives.
                ImGui::SetTooltip("%s", attachChipTooltip(st, apath, ainfo).c_str());
            }
            ImGui::SameLine(0, 6 * sc);
            ImGui::SetCursorPosY(rowTop + (rowH - lineH) * 0.5f);
            if (ImGui::SmallButton("\xC3\x97")) detach = true;
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Detach");
            ImGui::EndGroup();
            {
                const ImVec2 gmn = ImGui::GetItemRectMin(), gmx = ImGui::GetItemRectMax();
                const ImVec2 bmn(gmn.x - chipPadX, gmn.y - chipPadY);
                const ImVec2 bmx(gmx.x + chipPadX, gmx.y + chipPadY);
                chipSplit.SetCurrentChannel(chipDl, 0);
                chipDl->AddRectFilled(bmn, bmx, chipFillCol, ui::metrics::rounding(6.0f));
                chipDl->AddRect(bmn, bmx, chipEdgeCol, ui::metrics::rounding(6.0f), 0,
                                ui::metrics::stroke(1.0f));
                chipSplit.SetCurrentChannel(chipDl, 1);
            }
            if (view) ui::openImageLightbox(apath);
            if (detach) {
                if (inlineImage) {
                    st.pendingInlineImagePaths[ai].clear(); // stable slots: never remap #N
                    g_imageRemovals.push_back(ai);
                } else {
                    st.pendingAttachments.erase(st.pendingAttachments.begin() + ai - inlineCount);
                }
                if (!inlineImage) savePromptDraft(st, /*force=*/true);
                ImGui::PopID();
                break;
            }
            ImGui::PopID();
        }
        chipSplit.Merge(chipDl);

        // Context-ref chips: kind icon + label (+ the doc when it is not the active one) + ×.
        // Clicking a chip shows its entity in the model; × detaches it.
        for (size_t ci = 0; ci < st.pendingContextRefs.size(); ++ci) {
            ImGui::PushID((int)ci + 14000);
            const core::ContextRef& ref = st.pendingContextRefs[ci];
            placeChip(contextRefChipWidth(st, ref, /*removable=*/true), 4 * sc);
            const ContextChipAction act = drawContextRefChip(st, ref, /*removable=*/true);
            ImGui::PopID();
            if (act == ContextChipAction::Focus) {
                focusContextRef(st, ref);
            } else if (act == ContextChipAction::Remove) {
                st.pendingContextRefs.erase(st.pendingContextRefs.begin() + (std::ptrdiff_t)ci);
                savePromptDraft(st, /*force=*/true);
                break;
            }
        }

        ImGui::PopStyleColor(2);
        ImGui::PopStyleVar();
    }

    // Armed pick hint (selection-well language) above the prelude.
    if (st.contextPickActive) {
        const ui::AdaptiveSurface& asurf = chatSurf();
        const float sc = ui::uiScale;
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(asurf.textSecondary.x, asurf.textSecondary.y, asurf.textSecondary.z, 0.95f));
        ImGui::TextWrapped(
            "Pick parts, faces, edges, sketches or features to add to context \xE2\x80\x94 Esc to finish");
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 2.0f * sc));
    }

    // ---- composer: prelude row -> transparent input -> toolbar row ----
    // The slash drop-up anchors ABOVE the prelude (mode chip + gauges): anchored to the input it
    // covered that row, and the chip and meter showed through the menu's glass.
    const float composerTopY = ImGui::GetCursorScreenPos().y;
    drawComposerPrelude(st, busy);

    const char* placeholder =
        hasOpenAsk(st.chat)                        ? "Answer in the form above, or type a reply\xE2\x80\xA6"
      : st.aiInteraction == (int)CopilotMode::Ask  ? "Ask about this part\xE2\x80\xA6"
      : st.aiInteraction == (int)CopilotMode::Plan ? "Describe what to plan\xE2\x80\xA6"
                                                   : "Describe what to build or change\xE2\x80\xA6";

    ImDrawList* composerDl = ImGui::GetWindowDrawList();
    ImDrawListSplitter composerSplit;
    const ImVec2 composerMin = ImGui::GetCursorScreenPos();
    const float composerW = ImGui::GetContentRegionAvail().x;
    composerSplit.Split(composerDl, 2);
    composerSplit.SetCurrentChannel(composerDl, 1);

    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 0.0f));
    ImGui::PushStyleColor(ImGuiCol_FrameBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::PushStyleColor(ImGuiCol_TextDisabled, surf.textTertiary);
    ImGui::PushStyleColor(ImGuiCol_Border, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 0.0f);
    // Minimal top pad so the Design dropdown + context meter sit close to the input.
    ImGui::Dummy(ImVec2(0, ui::metrics::U(0.25f)));
    // Staged edit of THIS conversation: say what Enter / Esc do, or what the transaction is
    // waiting on. The box is read-only while the edit is being applied (editFrozen above).
    if (const char* editStatus = editStageStatus(st)) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextWrapped("%s", editStatus);
        ImGui::PopStyleColor();
    }
    // Reserve the in-box send button's footprint on the input's right so text never runs under it.
    const float sendSz    = ImGui::GetFrameHeight();
    const float sendInset = 6.0f * ui::uiScale;
    if (s_refocusComposer) {
        ImGui::SetKeyboardFocusHere();
        s_refocusComposer = false;
    }
    // Prime the slash-menu catalog after the initial interactive frame has had time to settle.
    if (st.workerStarted) pollComposerSkills(st, /*force=*/false);

    // Slash query opens before the InputText so Up/Down history callbacks fire this frame.
    {
        std::string q;
        g_slashMenuActive = parseSlashQuery(st.prompt.data(), q) ||
                            parseSlashArgQuery(st.prompt.data(), q) != nullptr;
    }

    // ---- Ctrl+V of an IMAGE ------------------------------------------------------------------
    // InputTextMultiline claims Ctrl+V itself (imgui_widgets.cpp: Shortcut(Ctrl+V, id) -> is_paste)
    // and pastes the clipboard's TEXT flavour, which for a copied image is empty or a stale
    // leftover string. So the image case has to be decided BEFORE the widget runs. On success we
    // take ownership of the V key for this frame: the widget's Shortcut() then fails its owner
    // test, so the chord is swallowed and no stale text lands in the box alongside the chip.
    // (This is the composer's own idiom for a swallowed chord — the slash menu likewise decides
    // Enter before/around the widget rather than clearing global input state.)
    // Scoped to "the input was active last frame" so clipboardHasImage() — a real windowing-system
    // round trip, throttled but not free — is never polled while the user is elsewhere in the app.
    char inputId[32];
    std::snprintf(inputId, sizeof(inputId), "##aiprompt%d", s_composerGen);
    static bool s_composerInputActive = false;
    {
        const ImGuiIO& pio = ImGui::GetIO();
        if (!editFrozen && s_composerInputActive &&
            ImGui::GetActiveID() == ImGui::GetID(inputId) && (pio.KeyCtrl || pio.KeySuper) &&
            ImGui::IsKeyPressed(ImGuiKey_V, false) && ui::clipboardHasImage()) {
            // Image clipboard always owns V, even if capture fails: never paste stale text.
            ImGui::SetKeyOwner(ImGuiKey_V, ImGui::GetID("##aipromptimagepaste"));
            std::string imgPath, imgErr;
            if (ui::clipboardImageToPng(imgPath, imgErr)) {
                g_imageInsert = ui::copilot::inlineImageMarker(st.pendingInlineImagePaths.size());
                st.pendingInlineImagePaths.push_back(imgPath);
                g_imageUseRememberedSelection = false; // use the live caret/selection
            } else {
                st.toast(ui::ToastLevel::Warn, imgErr);
            }
        }
    }

    const bool imageDraftChanged = !g_imageInsert.empty() || !g_imageRemovals.empty();
    if (imageDraftChanged) {
        ImGui::SetKeyboardFocusHere();
        const size_t needed = std::strlen(st.prompt.data()) + g_imageInsert.size() + 1;
        if (needed > st.prompt.size()) {
            const auto oldWrap = g_wrap.find(st.prompt.data());
            WrapState savedWrap;
            if (oldWrap != g_wrap.end()) savedWrap = oldWrap->second;
            st.prompt.resize(needed + 256, 0);
            g_wrap[st.prompt.data()] = std::move(savedWrap);
        }
    }
    g_imageRosterSize = st.pendingInlineImagePaths.size();
    if (editFrozen) ImGui::BeginDisabled();
    g_mainComposerInput = true;
    bool submitInput = drawChatInput(inputId, st.prompt, placeholder,
                                     ui::metrics::copilotInputMaxLines(),
                                     sendSz + sendInset * 2.0f);
    g_mainComposerInput = false;
    if (editFrozen) {
        ImGui::EndDisabled();
        submitInput = false;
    }
    s_composerInputActive = ImGui::IsItemActive();
    const ImVec2 inputMin = ImGui::GetItemRectMin();
    const ImVec2 inputMax = ImGui::GetItemRectMax();
    // Free-form argument hint: `/btw ` reads `/btw <question>` until the user types the argument.
    if (const char* argHint = slashFreeArgHint(st.prompt.data())) {
        const ImVec2 pad = ImGui::GetStyle().FramePadding;
        const float x = inputMin.x + pad.x + ImGui::CalcTextSize(st.prompt.data()).x;
        const float room = inputMax.x - x - pad.x;
        if (room > 1.0f)
            ImGui::GetWindowDrawList()->AddText(ImVec2(x, inputMin.y + pad.y),
                                                ImGui::GetColorU32(ImGuiCol_TextDisabled),
                                                slashFitText(argHint, room).c_str());
    }
    // Crash-safe draft: debounce-write whatever is in the box.
    savePromptDraft(st, /*force=*/imageDraftChanged);
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(6);
    // Send/stop rides INSIDE the input box, anchored bottom-right of the composer glass.
    drawComposerSendStop(st, inputMin, inputMax,
                         composerMin.x + composerW, canRun, canStart, active, submit, stopReq);

    // Slash autocomplete drop-up (commands + skills). Accept steals Enter from submit.
    bool slashAteEnter = false;
    if (drawSlashAutocomplete(st, inputMin, inputMax, std::min(composerTopY, inputMin.y),
                              slashAteEnter)) {
        ++s_composerGen;          // active InputText ignores external buffer writes
        s_refocusComposer = true;
        submitInput = false;
        submit = false;
    } else if (slashAteEnter || (s_slash.open && s_slash.selected >= 0 && submitInput)) {
        // Menu open with a row picked: Enter must not send the half-typed `/foo` token. With
        // nothing picked (an optional argument's options) Enter sends the bare command.
        submitInput = false;
        submit = false;
    }

    // No usage/provider id strip above the toolbar — model lives in the toolbar chip.
    if (editFrozen) ImGui::BeginDisabled();
    drawComposerToolbar(st, busy, true);
    if (editFrozen) ImGui::EndDisabled();
    ImGui::PopStyleVar();

    const ImVec2 composerMax(composerMin.x + composerW, ImGui::GetItemRectMax().y);
    composerSplit.SetCurrentChannel(composerDl, 0);
    const float composerRound = 12.0f;   // logical dp — glassPanel scales internally
    ui::glassPanel(composerDl, ImRect(composerMin, composerMax), ui::GlassLevel::Thin,
                  composerRound, surf);
    composerSplit.Merge(composerDl);
    // Focus ring: while the box holds the caret, a crisp accent edge over a soft wide halo,
    // eased in/out so focusing reads as the field lighting up rather than a border snapping on.
    {
        const float focus = ui::anim::toward(ImGui::GetID("##composer_focus"),
                                             s_composerInputActive ? 1.0f : 0.0f,
                                             ui::anim::kFast);
        if (focus > 0.01f) {
            const auto& pal = ui::palette;
            const float r = ui::metrics::rounding(composerRound);
            const float halo = ui::metrics::px(2.0f);
            composerDl->AddRect(ImVec2(composerMin.x - halo, composerMin.y - halo),
                                ImVec2(composerMax.x + halo, composerMax.y + halo),
                                ImGui::ColorConvertFloat4ToU32(ImVec4(
                                    pal.accent[0], pal.accent[1], pal.accent[2], 0.16f * focus)),
                                r + halo, 0, ui::metrics::stroke(3.0f));
            composerDl->AddRect(composerMin, composerMax,
                                ImGui::ColorConvertFloat4ToU32(ImVec4(
                                    pal.accent[0], pal.accent[1], pal.accent[2], 0.70f * focus)),
                                r, 0, ui::metrics::stroke(1.25f));
        }
    }

    // ---- stop / submit / queue ----
    if (stopReq) requestStopRun(st);
    // Fresh InputText state (an active widget ignores external clears) + refocus to keep typing.
    auto clearComposerBox = [&]() { composerSetText(st, std::string()); };
    if (submit || submitInput) {
        const std::string text = composerText(st);   // flattened + trimmed; empty = no message
        const bool slash = !text.empty() && text[0] == '/';
        const SlashItem* slashCmd = slash ? slashCommandFor(text) : nullptr;
        if (editStageHoldsConversation(st.activeConvId())) {
            // A staged edit owns Enter: never a steer or a plain turn. The transaction (stop →
            // runtime rewind → geometry rollback → commit → ONE turn) keeps the text in the box
            // until it succeeds; a failure leaves it there with the original tail intact.
            if (editFrozen || !canRun || text.empty()) {
                // Pending/applying, or nothing to send: the status line says what is happening.
            } else if (slash) {
                st.toast(ui::ToastLevel::Info,
                         "A command can't replace a message \xe2\x80\x94 press Esc to stop "
                         "editing first.");
            } else {
                editStageSubmit(st, text);
            }
        } else if (active && slashCmd && slashCmd->needsIdle) {
            // Commands that need the service a run holds are refused, never steered into the
            // run; the text stays in the box so it can be sent once the run finishes.
            st.toast(ui::ToastLevel::Info, slashCmd->label + ": " + kAvailableAfterRun);
        } else if (active && slash && tryHandleSlashCommand(st, text)) {
            // Slash commands run as commands (/btw side question, /prewalk, /skills, /help)
            // rather than being steered into the live run as literal text.
            clearComposerBox();
        } else if (active) {
            // A turn is running — STEER it mid-run when the engine knows the run_id
            // (agent_steer: text is folded into the live turn at its next step). Fall
            // back to next-turn queueing only when no run_id is available (e.g. the
            // run hasn't announced itself yet). Never both — that would double-send.
            // Attachments ride with the steer, so a picture alone is a valid follow-up.
            const bool hasChips = !st.pendingAttachments.empty() || !st.pendingContextRefs.empty();
            if (!text.empty() || hasChips) {
                std::string runId;
                {
                    auto steerRun = st.activeRun();
                    std::lock_guard<std::mutex> lk(steerRun->mtx);
                    runId = steerRun->runId;
                }
                if (!runId.empty() && st.workerStarted) {
                    std::string sendText = text;
                    std::string bubbleText;
                    nlohmann::json attachments =
                        consumeComposerAttachments(st, sendText, &bubbleText);
                    std::vector<core::ContextRef> contextRefs = consumeContextRefs(st);
                    // The control lane carries text + images only: the chips ride inline.
                    if (const std::string ctx = core::contextRefsPromptText(contextRefs);
                        !ctx.empty())
                        sendText += "\n\n" + ctx;
                    // Names this bubble on the control lane and in agent.steer events, so
                    // its "queued" / "read" state follows the message.
                    static unsigned s_steerSeq = 0;
                    const std::string steerId =
                        "steer-" + std::to_string((long long)(chatNowEpoch() * 1000.0)) + "-" +
                        std::to_string(++s_steerSeq);
                    appendSteerToTranscript(st, userBubbleText(bubbleText, attachments),
                                            attachments, std::move(contextRefs), steerId);
                    agentControlDispatch(st, runId, sendText, std::move(attachments), steerId);
                } else {
                    // Freeze this prompt's exact roster; a later draft is independent.
                    st.queuedPromptDrafts.resize(st.queuedPrompts.size(), nlohmann::json::object());
                    st.queuedPrompts.emplace_back(text);
                    st.queuedPromptDrafts.push_back({
                        {"attachments", std::move(st.pendingAttachments)},
                        {"inline_images", std::move(st.pendingInlineImagePaths)},
                        {"context_refs", core::contextRefsToJson(st.pendingContextRefs)}});
                    markChatHistoryDirty();
                    st.pendingAttachments.clear();
                    st.pendingInlineImagePaths.clear();
                    st.pendingContextRefs.clear();
                }
                clearComposerBox();
            } else if (!st.queuedPrompts.empty()) {
                // Enter on an EMPTY composer with something queued = "send it now": the message
                // didn't interrupt the run (it queued instead of steering), so a second Enter
                // stops the run and the drain at the top of the next frame launches the queued
                // message immediately. Same action as the ▶ button on the queued row.
                requestStopRun(st);
            }
        } else if (canStart) {
            submitTurn(st, text);
            clearComposerBox();
        }
    }

    // Full-size attachment viewer, drawn LAST in the composer so it sits above the chips and
    // the input. drawComposer is the composer's single per-frame entry point, so this is the
    // one and only call — the viewer is a no-op on frames where nothing was opened.
    ui::drawImageLightbox(st);
}

// ---- M19: endless scroll-back via render-windowing --------------------------------------------
// The full history is already in memory (persistChatHistory reload), so "lazy loading" here means
// lazily RENDERING: a message far outside the scroll viewport re-emits as a fixed-height spacer
// (the height measured the last time it was really drawn at this width) instead of running its
// full draw path. Per-frame cost is O(visible) real draws + O(total) trivial spacer loop — no
// crash and no unbounded frame cost on large histories. Interactive/animated messages (the
// running turn, an open clarify card) always render so nothing live is ever frozen out. Cached
// heights invalidate on width change (resize) and refresh every frame the message is visible
// (expand/collapse toggles re-measure naturally). Shared by the docked Copilot column and the AI
// workspace chat column.

}  // namespace ui
