// Copilot composer (T-RF6 split of legacy_chat.cpp): the auto-growing wrapping chat input,
// file attachments + context refs, design/mission run launch, the session slash commands
// (/compact /shake /handoff /btw /prewalk /rule /skills /help), slash-menu autocomplete
// (commands + skills), the submitTurn send path, and the composer chrome (runtime banner,
// Agent/Plan/Swarm/Edit/Ask prelude + context/provider-limit gauge rings, graduated
// segmented toolbar, in-box send/stop).

#include "copilot_internal.h"
#include "ui/provider_usage.h"   // THE subscription-usage poller behind the prelude's limit rings
#include "ui/provider_logos.h"   // brand mark for the model chip (same art as Settings->Providers)
#include "ui/copilot/text_wrap.h"   // hard-break bookkeeping the rewrapper must not eat
#include "ui/clipboard_image.h"          // image-kind probe / mime / clipboard->PNG capture
#include "ui/copilot/image_lightbox.h"   // attachment thumbnail tile + full-size viewer
#include "ui/copilot/context_estimate.h"  // transcript-size estimate behind the context meter
#include <climits>                        // INT_MAX saturation in activityCharsOf()

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
using ui::copilot::noteEdit;
struct ChatInputCb {
    float wrapW = 0.0f;
    bool  interceptHistory = false;  // slash menu open — eat Up/Down for menu nav
    int*  navDelta = nullptr;        // -1 Up / +1 Down when interceptHistory
    WrapState* wrap = nullptr;       // hard-break bookkeeping for this input's buffer
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
}  // namespace


namespace {
int chatInputEditCallback(ImGuiInputTextCallbackData* data) {
    auto* cb = static_cast<ChatInputCb*>(data->UserData);
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
    WrapState& ws = g_wrap[buf.data()];
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

    g_slashNavDelta = 0;
    ChatInputCb cb;
    cb.wrapW = wrapW;
    cb.interceptHistory = g_slashMenuActive;
    cb.navDelta = &g_slashNavDelta;
    cb.wrap = &ws;
    ImGuiInputTextFlags flags = ImGuiInputTextFlags_CallbackEdit |
                                ImGuiInputTextFlags_CallbackAlways;
    if (g_slashMenuActive) {
        // Enter accepts a slash pick (handled by the menu) — don't insert a newline into the token.
        flags |= ImGuiInputTextFlags_CallbackHistory | ImGuiInputTextFlags_CtrlEnterForNewLine;
    }
    ImGui::InputTextMultiline(id, buf.data(), buf.size(),
        ImVec2(rightReserve > 0.0f ? -rightReserve : -1.0f, h),
        flags, chatInputEditCallback, &cb);
    s_activeLast[wid] = ImGui::IsItemActive();
    // A BARE Enter sends; so does Ctrl+Enter, which ImGui's multiline treats as "validate"
    // and never turns into a line break. Shift/Alt+Enter are newlines: the widget inserts one
    // itself, so counting them as a submit shipped the message AND left a stray break behind
    // (live 2026-07-28: Alt+Enter sent the prompt instead of breaking the line). Detect the
    // KEY, not a trailing '\n', so a newline the rewrap just inserted is never read as submit.
    const ImGuiIO& kio = ImGui::GetIO();
    // In slash mode CtrlEnterForNewLine is set, so there Ctrl+Enter is a break, not a validate.
    const bool sendChord = !kio.KeyShift && !kio.KeyAlt && !kio.KeySuper &&
                           !(kio.KeyCtrl && g_slashMenuActive);
    bool submit = ImGui::IsItemFocused() && sendChord &&
                  ImGui::IsKeyPressed(ImGuiKey_Enter, false);
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

// Forward decls — drawCodeBox/drawDiffBox sit above the chat-copy helper block but need
// the shared "Copied" toast + context menu (every copy path must light the same indicator).

namespace ui {

// ---- O1: model picker → RPC payload threading --------------------------------------------------
// Canonical provider NAME for the core's resolve_provider(name, cfg, env) from the GUI enum.
// PROV_LOCAL resolves as an "openai" descriptor — the base_url (persisted to config.json by
// saveSettings immediately before every send) discriminates local vs cloud. PROV_CLOUDCOMPAT is
// a SHARED slot serving many services; this returns only its generic fallback, and
// agentProviderOverride refines it to the real service via ui::cloudServiceKey(base_url).
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
        // Kimi Code — OMP-bridge provider id. The old "kimi-sub" spelling is a PYTHON
        // subscription-provider id (services/agent/providers/kimi_sub_provider.py): the C++
        // resolve_provider has no branch for it and it is absent from kOmpBridgeProviders, so
        // every send from this slot died on "Unknown provider: 'kimi-sub'".
        case ui::PROV_KIMISUB:     return "kimi-code";
        // ZenMux — OMP-bridge provider id, routed through the bun bridge (core/agent/providers.cpp
        // is_omp_bridge_provider) using its own auth db, not a config.json key. Default provider.
        case ui::PROV_ZENMUX:      return "zenmux";
        // Free AI (FreeLLMAPI) — native wire "freellmapi" (core/agent/providers.cpp +
        // config.cpp), own config namespace, always cloud-classified.
        case ui::PROV_FREELLMAPI:  return "freellmapi";
    }
    return "";
}

// The explicit provider/model/effort override the GUI now attaches to EVERY agent-turn RPC
// (agent_run_v2 / agent_ask_v2 / agent_clarify_v1 / agent_mission_v1). Without it the core
// resolves from ambient config.json + process env, so an CHUDWARE_PROVIDER env var (or a config
// edited behind the GUI's back) silently beat the picker (tracker O1). saveSettings still runs
// before each send so descriptor details (base_url / api_key / model_path) match the picker;
// these fields pin name + model + effort explicitly on top of that.
// Effort ladder for the picker's current model, OMP bridge catalog first (dynamic — K3 is
// low|high|max, current Claude low|medium|high|xhigh|max, per the vendored OMP catalog),
// the static family ladders in model_registry.h otherwise. Returns the rung count.
static int composerEffortLadder(const Settings& s, const char** outLabels,
                                const char** outValues, int maxN) {
    ui::ModelInfo probe;
    const ui::ModelInfo* probeP = nullptr;
    if (const ui::BridgeModelMeta* bm = ui::bridgeModelMetaForCad(s.provider, s.model, s.baseUrl);
        bm && !bm->reasoningEfforts.empty()) {
        probe = ui::modelInfoFromProbe(s.provider, s.model, bm->name, bm->contextWindow,
                                       bm->maxTokens, bm->vision, bm->tools,
                                       bm->reasoningEfforts, "omp");
        probeP = &probe;
    }
    return ui::reasoningEffortOptions(s.provider, s.model, outLabels, outValues, maxN, probeP);
}

nlohmann::json agentProviderOverride(const Settings& s) {
    nlohmann::json j = nlohmann::json::object();
    const char* name = providerWireName(s.provider);
    std::string wire = name ? name : "";
    // The cloud-compat slot is shared by every base_url-keyed service (OpenRouter / DashScope /
    // DeepSeek / Moonshot, plus the bridge-catalog ids that have no native transport at all).
    // Pin the REAL provider name so resolve_provider lands on that service's descriptor — its
    // OMP bridge id, or the first-class native "moonshot" branch for Kimi presets (kimi-k3 alias
    // normalize, 1M context window, reasoning_effort=max) — instead of the generic openai one.
    // saveSettings writes the same classified name + the matching credential to config.json.
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
        bool valid = n <= 0;  // ladder unknown → don't second-guess the saved value
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

// Read a reasonably-sized TEXT file for inline injection into the request. Returns false for
// binary or large files — those ride as path-only attachments (no fabricated previews).
static bool readAttachmentText(const std::string& path, std::string& out) {
    constexpr uintmax_t kMaxInline = 64 * 1024;   // inline cap: keep the prompt sane
    std::error_code ec;
    const uintmax_t sz = std::filesystem::file_size(path, ec);
    if (ec || sz == 0 || sz > kMaxInline) return false;
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
    out = std::move(data);
    return true;
}

// Consume st.pendingAttachments into (a) an augmented request string — the text the model
// actually receives: small text files inlined verbatim, everything else as an explicit path —
// and (b) the structured "attachments" array carried in the RPC payload, where every entry now
// declares its "kind" ("image"|"file") and, for images, its "mime".
//
// The two kinds are delivered by DIFFERENT routes, and both are real:
//   TEXT files  — the inline injection below is what actually delivers their content, because
//                 the request text is all a text-only model ever sees.
//   IMAGES      — ride STRUCTURALLY: the kind/mime entry is forwarded through the core to the
//                 Python worker, which turns it into a provider image part for the vision model.
//                 The request text only gets a short marker naming the file, so a text-only
//                 model still knows an image was attached rather than silently missing it.
static nlohmann::json consumeAttachments(AppState& st, std::string& requestText) {
    nlohmann::json atts = nlohmann::json::array();
    if (st.pendingAttachments.empty()) return atts;
    for (const std::string& path : st.pendingAttachments) {
        const std::string base = attachmentBaseName(path);
        nlohmann::json a = {{"path", path}, {"name", base}};
        if (ui::isImageAttachment(path)) {
            // Explicitly NOT run through readAttachmentText: an image is binary, that probe
            // would reject it on the first NUL anyway, and inlining is not how it travels.
            a["kind"]   = "image";
            a["mime"]   = ui::imageMimeForPath(path);
            a["inline"] = false;
            requestText += "\n\n[Attached image: " + base + " (" + path + ")]\n";
            atts.push_back(std::move(a));
            continue;
        }
        a["kind"] = "file";
        std::string content;
        if (readAttachmentText(path, content)) {
            a["inline"] = true;
            requestText += "\n\n[Attached file: " + base + " (" + path +
                           ")]\n```\n" + content;
            if (content.empty() || content.back() != '\n') requestText += "\n";
            requestText += "```";
        } else {
            a["inline"] = false;
            requestText += "\n\n[Attached file (path only; content not inlined): " + path + "]";
        }
        atts.push_back(std::move(a));
    }
    st.pendingAttachments.clear();
    return atts;
}

// Wave4: serialize st.pendingContextRefs into the turn payload's "context_refs" array AND
// inline a "Context references:" block into the request text (C++ agent_run only sees request
// text — same honesty pattern as consumeAttachments). Then clear chips + disarm the cube.
static nlohmann::json consumeContextRefs(AppState& st, std::string& requestText) {
    nlohmann::json refs = nlohmann::json::array();
    if (st.pendingContextRefs.empty()) {
        st.contextPickActive = false;
        return refs;
    }
    requestText += "\n\nContext references:";
    for (const auto& r : st.pendingContextRefs) {
        nlohmann::json o = {
            {"kind", r.kind},
            {"id", r.id},
            {"doc_id", r.docId},
            {"label", r.label},
        };
        refs.push_back(std::move(o));
        // One line the model can resolve/measure/target: kind + label + ids.
        requestText += "\n- " + (r.kind.empty() ? std::string("ref") : r.kind) + ": ";
        requestText += r.label.empty() ? (r.id.empty() ? "(unnamed)" : r.id) : r.label;
        requestText += " (id: " + r.id;
        if (!r.docId.empty()) requestText += ", doc: " + r.docId;
        requestText += ")";
    }
    st.pendingContextRefs.clear();
    st.contextPickActive = false;
    return refs;
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
        if (m.text.find_first_not_of(" \t\r\n") == std::string::npos) continue;
        std::string t = m.text;              // text only — no images, tool cards, activity
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
static void attachAgentConversationOptions(AppState& st, nlohmann::json& opts) {
    opts["session_id"] = st.activeConvId();
    opts["sessions_dir"] = chatSessionsDir();
    if (nlohmann::json hist = chatHistoryForAgent(st); !hist.empty())
        opts["history"] = std::move(hist);
}

// `extra` is merged on top of the provider override (attachments / context_refs / …).
void launchDesignRun(AppState& st, const std::string& request,
                            nlohmann::json extra) {
    std::string err;
    bool docOk = st.docMirror.hasDoc();
    if (docOk) {
        const nlohmann::json& tree = st.docMirror.tree();
        bool hasFeatures = tree.is_object() && tree.contains("features") &&
                           tree["features"].is_array() && !tree["features"].empty();
        if (hasFeatures) docOk = st.docMirror.newPart(st.worker, err);
    } else {
        docOk = st.docMirror.newPart(st.worker, err);
    }
    if (!docOk) {
        st.addLog("[err] design: could not create a part document: " + err);
        st.status = "Worker not ready";  st.statusKind = 2;
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
            if (!it->fromUser && (it->running || it->clarify.active)) {
                it->running = false;
                it->clarify = ClarifyState{};
                it->errKind = ERR_SYSTEM;
                it->text = "I couldn't open a new part document (worker not ready). "
                           "Check the Console and try again.";
                break;
            }
        return;
    }
    st.clearViewportForNewDoc();
    // O1: pin the picker's provider/model/effort explicitly in the RPC payload (beats an
    // ambient CHUDWARE_PROVIDER env override in the core's resolve_provider). Merge call-site
    // extras (attachments / context_refs) on top.
    nlohmann::json payload = agentProviderOverride(st.settings);
    if (extra.is_object()) {
        for (auto it = extra.begin(); it != extra.end(); ++it)
            payload[it.key()] = it.value();
    }
    // Mode overhaul: default options.mode=agent when caller didn't stamp one (Build-it path).
    // corporate_mode mirrors Settings → AI → Behavior (default off — no auto company).
    {
        nlohmann::json opts = nlohmann::json::object();
        if (payload.contains("options") && payload["options"].is_object())
            opts = payload["options"];
        if (!opts.contains("mode")) opts["mode"] = "agent";
        if (!opts.contains("advisor")) opts["advisor"] = true;
        opts["corporate_mode"] = st.settings.corporateMode;
        attachAgentConversationOptions(st, opts);
        payload["options"] = std::move(opts);
    }
    // T-RF5: single agent entry — every Design send goes to agent_run_v2. (The legacy
    // Python agent_design loop is gone. A cursor pick now reaches the core's
    // resolve_provider, which returns a classified pre-run refusal: Cursor has no C++
    // chat completions transport — the chat paints that as a normal failure card.)
    st.addLog("[..] designing (v2): " + request);
    st.status = "Designing...";  st.statusKind = 0;
    st.agentV2Running = true;
    st.agentV2 = true;
    launchAgentRunV2(st.worker, st.activeRun(), st.activeConvId(), st.chatDocKey, request,
                     st.docMirror.docId(), st.ribbonTab == AppState::RT_Manufacture, payload);
}

// Launch the MISSION orchestrator for a multi-capability request (e.g. "model X, then run CFD and
// prove it's good"). Mirrors launchDesignRun's doc-prep + mutex + activity reset, but routes to
// agent_mission_v1 so the run decomposes into a task chain (design/assembly -> sim -> verdict).
// Shares the same fresh-part policy: a new build starts on a clean part doc; an existing model is
// kept (a mission that only analyzes the open part should not wipe it).
// `extra` is merged on top of the provider override (attachments / context_refs / …).
void launchMissionRunChat(AppState& st, const std::string& request, bool needFreshDoc,
                                 nlohmann::json extra) {
    std::string err;
    bool docOk = st.docMirror.hasDoc();
    if (needFreshDoc) {
        if (docOk) {
            const nlohmann::json& tree = st.docMirror.tree();
            bool hasFeatures = tree.is_object() && tree.contains("features") &&
                               tree["features"].is_array() && !tree["features"].empty();
            if (hasFeatures) docOk = st.docMirror.newPart(st.worker, err);
        } else {
            docOk = st.docMirror.newPart(st.worker, err);
        }
    }
    if (!docOk) {
        st.addLog("[err] mission: could not create a part document: " + err);
        st.status = "Worker not ready";  st.statusKind = 2;
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
            if (!it->fromUser && (it->running || it->clarify.active)) {
                it->running = false;
                it->clarify = ClarifyState{};
                it->errKind = ERR_SYSTEM;
                it->text = "I couldn't open a part document (worker not ready). "
                           "Check the Console and try again.";
                break;
            }
        return;
    }
    if (needFreshDoc) st.clearViewportForNewDoc();
    st.addLog("[..] mission: " + request);
    st.status = "Working on it...";  st.statusKind = 0;
    st.agentV2Running = true;
    st.agentV2 = true;
    // O1: pin the picker's provider/model/effort explicitly in the RPC payload.
    nlohmann::json payload = agentProviderOverride(st.settings);
    if (extra.is_object()) {
        for (auto it = extra.begin(); it != extra.end(); ++it)
            payload[it.key()] = it.value();
    }
    // Mode overhaul: default mode=swarm when corporate_force, else agent, if unstamped.
    // corporate_mode mirrors Settings → AI → Behavior (default off — no auto company).
    {
        nlohmann::json opts = nlohmann::json::object();
        if (payload.contains("options") && payload["options"].is_object())
            opts = payload["options"];
        if (!opts.contains("mode")) {
            if (opts.value("corporate_force", false)) opts["mode"] = "swarm";
            else opts["mode"] = "agent";
        }
        if (!opts.contains("advisor")) opts["advisor"] = true;
        opts["corporate_mode"] = st.settings.corporateMode;
        attachAgentConversationOptions(st, opts);
        payload["options"] = std::move(opts);
    }
    launchMissionRun(st.worker, st.activeRun(), st.activeConvId(), st.chatDocKey, request,
                     st.docMirror.docId(), payload);
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
    freeChatTextures(st.chat);
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
// Editor-style drop-up menu when the composer holds a single slash token (`^/[^\s]*$`).
// Session cmds are local; skills come from the skills_list RPC (same catalog as Settings).

enum class SlashKind { Session, Skill, Meta };

struct SlashItem {
    SlashKind   kind = SlashKind::Session;
    std::string insert;   // text written into the composer on accept
    std::string label;    // primary row text (usually same as insert, trimmed)
    std::string detail;   // muted description
    const char* icon = "ai_panel";
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
    double lastList = 0.0;
};
static ComposerSkillsCache s_composerSkills;

struct SlashMenuState {
    bool open = false;
    int  selected = 0;
    std::string filter;                 // typed text AFTER the leading '/'
    std::vector<SlashItem> matches;
    // Esc latch: once dismissed, stay closed while the query text is unchanged
    // (drawSlashAutocomplete recomputes `open` from the buffer every frame, so a
    // bare `open = false` reopens on the very next frame).
    bool        dismissed = false;
    std::string dismissedFilter;
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
    if (!st.workerStarted && !force) return;
    const double now = ImGui::GetTime();
    if (!force && s_composerSkills.loaded && (now - s_composerSkills.lastList) < 8.0) return;
    if (s_composerSkills.inFlight) return;
    s_composerSkills.lastList = now;
    s_composerSkills.inFlight = true;
    st.rpc.call("skills_list", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_composerSkills.inFlight = false;
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
                       ? "Skills RPC not available (worker missing skills_list)."
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

static const SlashItem kSessionSlashItems[] = {
    {SlashKind::Session, "/compact ", "/compact",
     "Compact conversation [snapcompact|shake|summary]", "ai_panel"},
    {SlashKind::Session, "/shake ", "/shake",
     "Shake loose tokens [elide|images]", "ai_panel"},
    {SlashKind::Session, "/handoff ", "/handoff",
     "New session seeded with a handoff document", "ai_panel"},
    {SlashKind::Session, "/btw ", "/btw",
     "Side question (does not enter the transcript)", "ask"},
    {SlashKind::Session, "/prewalk", "/prewalk",
     "Arm agent prewalk for the next turn", "plan_board"},
    {SlashKind::Session, "/rule ", "/rule",
     "New rule: /rule <trigger words> -> <context to inject>", "plan_board"},
    {SlashKind::Meta, "/skills", "/skills",
     "List available skills", "team"},
    {SlashKind::Meta, "/help", "/help",
     "Show slash-command help", "ask"},
};

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

static void applySlashInsert(std::vector<char>& buf, const std::string& text) {
    if (buf.size() < text.size() + 1)
        buf.resize(text.size() + 1, 0);
    std::memcpy(buf.data(), text.c_str(), text.size() + 1);
}

static bool slashMenuRow(const char* iconName, const char* label, const char* detail,
                         bool selected, int uid) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const float rowH = ImGui::GetFrameHeight() + 6.0f * sc;
    const float fh = ImGui::GetFontSize();
    ImGui::PushID(uid);
    const bool clicked = ImGui::Selectable("##slashrow", selected,
                                           ImGuiSelectableFlags_AllowItemOverlap |
                                               ImGuiSelectableFlags_DontClosePopups,
                                           ImVec2(-1, rowH));
    const bool hov = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
    const float rnd = 4.0f;
    const float washA = selected ? 0.42f : (hov ? 0.36f : 0.28f);
    ui::glassRowBackground(dl, mn, mx, rnd, /*stripe=*/false, /*selected=*/false,
                           /*washAlpha=*/washA);
    if (selected || hov) {
        ui::coloredRowWash(dl, mn, mx, rnd, pal.accent,
                           selected ? 0.34f : 0.18f, /*selected=*/selected);
    }
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
    dl->AddText(ImVec2(textX, midY - fh * 0.55f), ui::Palette::toU32(pal.text), label);
    if (detail && detail[0]) {
        std::string d = detail;
        const float maxDetailW = (mx.x - textX) - 10.0f * sc;
        // Trim-to-fit. The previous loop appended the ellipsis EVERY pass
        // (`substr(size-2) + "…"` → net +1 byte/iteration), so a detail wider
        // than the row never converged — an infinite loop that froze the UI
        // thread the first frame the menu drew (the "type / and the app
        // crashes" hang). Shrink the raw text first, append the ellipsis once.
        if (maxDetailW > 1.0f && ImGui::CalcTextSize(d.c_str()).x > maxDetailW) {
            const std::string ell = "\xe2\x80\xa6";
            while (d.size() > 4 &&
                   ImGui::CalcTextSize((d + ell).c_str()).x > maxDetailW)
                d.resize(d.size() - 2);
            d += ell;
        }
        dl->AddText(ImVec2(textX, midY + fh * 0.15f),
                    ui::Palette::toU32(pal.textDim), d.c_str());
    }
    ImGui::PopID();
    return clicked;
}

// Draw the drop-up slash menu above the composer input. Returns true when an item was
// accepted (caller must bump the InputText generation so the insert is visible).
static bool drawSlashAutocomplete(AppState& st, const ImVec2& inputMin, const ImVec2& inputMax,
                                  bool& ateEnter) {
    ateEnter = false;
    std::string filter;
    const bool want = parseSlashQuery(st.prompt.data(), filter);
    if (!want) {
        s_slash.open = false;
        s_slash.matches.clear();
        s_slash.dismissed = false;
        s_slash.dismissedFilter.clear();
        g_slashMenuActive = false;
        return false;
    }

    if (s_slash.dismissed) {
        if (filter == s_slash.dismissedFilter) {
            // Esc dismissed this exact query — stay closed until the text changes.
            s_slash.open = false;
            s_slash.matches.clear();
            g_slashMenuActive = false;
            return false;
        }
        s_slash.dismissed = false;  // query changed — re-arm the menu
    }

    pollComposerSkills(st, /*force=*/false);
    if (!s_slash.open || filter != s_slash.filter) {
        s_slash.filter = filter;
        s_slash.selected = 0;
        rebuildSlashMatches(filter);
    } else {
        rebuildSlashMatches(filter);  // skills may arrive mid-typing
    }
    s_slash.open = !s_slash.matches.empty() || filter.empty();
    if (s_slash.matches.empty()) {
        // Keep the menu shell open on bare `/` while skills load; otherwise hide.
        if (!filter.empty() || (!s_composerSkills.inFlight && s_composerSkills.loaded)) {
            s_slash.open = false;
            g_slashMenuActive = false;
            return false;
        }
    }
    g_slashMenuActive = true;

    // Apply Up/Down captured by the InputText history callback.
    bool scrolledByKey = false;
    if (g_slashNavDelta != 0 && !s_slash.matches.empty()) {
        const int n = (int)s_slash.matches.size();
        s_slash.selected = (s_slash.selected + g_slashNavDelta) % n;
        if (s_slash.selected < 0) s_slash.selected += n;
        g_slashNavDelta = 0;
        scrolledByKey = true;
    }

    const float sc = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    const auto& pal = ui::palette;
    const float popW = ImMax(inputMax.x - inputMin.x, 280.0f * sc);
    const float rowH = ImGui::GetFrameHeight() + 6.0f * sc;
    const float headerH = ImGui::GetTextLineHeight() + 10.0f * sc;
    const int visible = (int)std::min<size_t>(8, std::max<size_t>(1, s_slash.matches.size()));
    const float listH = rowH * (float)visible + 6.0f * sc;
    const float popH = headerH + listH + 8.0f * sc;
    const float gap = 4.0f * sc;

    ImGui::SetNextWindowPos(ImVec2(inputMin.x, inputMin.y - gap), ImGuiCond_Always,
                            ImVec2(0.0f, 1.0f));
    ImGui::SetNextWindowSize(ImVec2(popW, popH), ImGuiCond_Always);
    ImGui::SetNextWindowBgAlpha(0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(6.0f * sc, 6.0f * sc));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0, 2.0f * sc));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, ui::metrics::rounding(8.0f));
    ImGui::PushStyleColor(ImGuiCol_Border, IM_COL32(0, 0, 0, 0));
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
        ImGui::TextUnformatted(s_slash.matches.empty() ? "Loading commands\xe2\x80\xa6"
                                                       : "Commands & skills");
        ImGui::PopStyleColor();

        ImGui::BeginChild("##slashlist", ImVec2(-1, listH), ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoBackground);
        for (int i = 0; i < (int)s_slash.matches.size(); ++i) {
            const auto& it = s_slash.matches[(size_t)i];
            const bool sel = (i == s_slash.selected);
            if (slashMenuRow(it.icon, it.label.c_str(), it.detail.c_str(), sel, i)) {
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
            ui::glassPanel(dl, ImRect(p0, p1), ui::GlassLevel::Regular,
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
    // of silently completing a command they were done typing.
    const ImGuiIO& sio = ImGui::GetIO();
    if (!accepted && !s_slash.matches.empty() &&
        ((ImGui::IsKeyPressed(ImGuiKey_Enter, false) && !sio.KeyShift && !sio.KeyAlt) ||
         ImGui::IsKeyPressed(ImGuiKey_Tab, false))) {
        accepted = true;
        acceptText = s_slash.matches[(size_t)s_slash.selected].insert;
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
            st.toast(ui::ToastLevel::Warn, "Wait for the current turn to finish.");
            return false;
        }
        return true;
    };

    if (cmd == "/prewalk") {
        ovSetAgentBool("prewalk", true);
        st.toast(ui::ToastLevel::Success, "Prewalk armed (agent.prewalk = true)");
        st.addLog("[..] /prewalk: agent.prewalk enabled");
        return true;
    }

    // /rule <trigger words> -> <context to inject>
    // Creates a live Rule (aicad.trigger/1) through the worker's single writer and jumps to
    // Settings -> AI -> Rules so the user lands on the row they just made. Both halves are
    // required server-side: a rule with no match could never fire.
    if (cmd == "/rule") {
        static const char* kRuleUsage =
            "Usage: /rule <trigger words> -> <context to inject>";
        if (rest.find("->") == std::string::npos) {
            st.toast(ui::ToastLevel::Warn, kRuleUsage);
            return true;
        }
        nlohmann::json params = nlohmann::json::object();
        params["text"] = "/rule " + rest;
        st.rpc.call("rule_create", params, [&st](const core::RpcResult& r) {
            if (!r.ok || !r.result.is_object()) {
                st.toast(ui::ToastLevel::Error,
                         r.err.empty() ? "rule_create failed (worker missing rule_create?)"
                                       : r.err);
                return;
            }
            if (!r.result.value("ok", false)) {
                std::string why = r.result.value("reason", std::string("rule_create failed"));
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
            st.toast(ui::ToastLevel::Warn, "Usage: /btw <question>");
            return true;
        }
        s_btw.open = true;
        s_btw.loading = true;
        s_btw.question = rest;
        s_btw.answer.clear();
        s_btw.error.clear();
        nlohmann::json params = nlohmann::json::object();
        params["messages"] = chatMsgsToWireMessages(st.chat);
        params["question"] = rest;
        nlohmann::json pov = agentProviderOverride(st.settings);
        if (pov.contains("provider")) params["provider"] = pov["provider"];
        if (pov.contains("model")) params["model"] = pov["model"];
        else if (st.settings.model[0]) params["model"] = st.settings.model;
        st.rpc.call("session_btw", params, [](const core::RpcResult& r) {
            s_btw.loading = false;
            if (!r.ok) {
                s_btw.error = r.err.empty() ? "session_btw failed" : r.err;
                return;
            }
            if (r.result.is_object()) {
                if (r.result.contains("answer") && r.result["answer"].is_string())
                    s_btw.answer = r.result["answer"].get<std::string>();
                else if (r.result.contains("text") && r.result["text"].is_string())
                    s_btw.answer = r.result["text"].get<std::string>();
                else
                    s_btw.answer = r.result.dump(2);
            } else if (r.result.is_string()) {
                s_btw.answer = r.result.get<std::string>();
            } else {
                s_btw.error = "empty answer";
            }
        }, 180000);
        st.toast(ui::ToastLevel::Info, "Asking (side channel)...");
        return true;
    }

    if (cmd == "/compact") {
        if (!needIdle()) return true;
        std::string strategy = "snapcompact";
        if (!rest.empty()) {
            std::string arg = rest;
            for (char& c : arg) c = (char)std::tolower((unsigned char)c);
            if (arg == "shake" || arg == "snapcompact" || arg == "summary" || arg == "off")
                strategy = arg;
        }
        if (strategy == "off") {
            st.toast(ui::ToastLevel::Info, "Compaction strategy is Off — nothing to do.");
            return true;
        }
        nlohmann::json params = nlohmann::json::object();
        params["messages"] = chatMsgsToWireMessages(st.chat);
        params["strategy"] = strategy;
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
        // Snap rendering is provider-shaped: tell the worker which provider/model
        // this session runs on (agentProviderOverride pins both), and anchor the
        // re-snap chain to the conversation so later rounds append and a provider
        // switch re-renders from the stored OG source instead of losing history.
        {
            nlohmann::json pov = agentProviderOverride(st.settings);
            for (auto& el : pov.items()) params[el.key()] = el.value();
            if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size() &&
                !st.conversations[st.activeConv].id.empty())
                params["session_id"] = st.conversations[st.activeConv].id;
        }
        st.rpc.call("session_compact", params, [&st, strategy](const core::RpcResult& r) {
            if (!r.ok) {
                st.toast(ui::ToastLevel::Error,
                         r.err.empty() ? "session_compact failed" : r.err);
                return;
            }
            if (!r.result.is_object()) {
                st.toast(ui::ToastLevel::Error, "session_compact: bad result");
                return;
            }
            // Prefer the rebuilt conversation (archive message + kept tail) so the
            // compacted history is reinjected instead of silently dropped; fall
            // back to the bare kept tail for older workers.
            if (r.result.contains("messages") && r.result["messages"].is_array())
                applyWireMessagesToChat(st, r.result["messages"]);
            else if (r.result.contains("kept_messages") && r.result["kept_messages"].is_array())
                applyWireMessagesToChat(st, r.result["kept_messages"]);
            std::string notice;
            if (r.result.contains("short_summary") && r.result["short_summary"].is_string())
                notice = r.result["short_summary"].get<std::string>();
            else if (r.result.contains("summary") && r.result["summary"].is_string())
                notice = r.result["summary"].get<std::string>();
            if (notice.empty()) notice = "Compacted (" + strategy + ").";
            pushSessionNotice(st, notice);
            st.addLog("[..] /compact " + strategy + " ok");
        }, 180000);
        st.toast(ui::ToastLevel::Info, "Compacting...");
        return true;
    }

    if (cmd == "/shake") {
        if (!needIdle()) return true;
        std::string mode = "elide";
        bool aggressive = true;
        if (!rest.empty()) {
            std::string arg = rest;
            for (char& c : arg) c = (char)std::tolower((unsigned char)c);
            if (arg == "images") mode = "images";
            else if (arg == "elide") mode = "elide";
        }
        nlohmann::json params = nlohmann::json::object();
        params["messages"] = chatMsgsToWireMessages(st.chat);
        params["mode"] = mode;
        params["aggressive"] = aggressive;
        st.rpc.call("session_shake", params, [&st, mode](const core::RpcResult& r) {
            if (!r.ok) {
                st.toast(ui::ToastLevel::Error,
                         r.err.empty() ? "session_shake failed" : r.err);
                return;
            }
            if (!r.result.is_object()) {
                st.toast(ui::ToastLevel::Error, "session_shake: bad result");
                return;
            }
            if (r.result.contains("messages") && r.result["messages"].is_array())
                applyWireMessagesToChat(st, r.result["messages"]);
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
            if (!r.ok) {
                st.toast(ui::ToastLevel::Error,
                         r.err.empty() ? "session_handoff failed" : r.err);
                return;
            }
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
            st.conversations.push_back(std::move(c));
            st.activeConv = (int)st.conversations.size() - 1;
            st.chat.clear();
            st.lastContextTokens = 0;
            st.queuedPrompts.clear();
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
        st.toast(ui::ToastLevel::Info,
                 "/compact [snapcompact|shake|summary]  /shake [elide|images]  "
                 "/handoff [focus]  /btw <q>  /prewalk  /skills  "
                 "/rule <words> -> <context>  (type / for autocomplete)");
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
        if (ImGui::Button("Close", ImVec2(-1, 0))) open = false;
        (void)st;
    }
    ImGui::End();
    s_btw.open = open;
}

// Launch the active turn for `text` according to the current CopilotMode
// (Agent/Plan/Swarm/Edit/Ask). Pushes the user + live-assistant ChatMsgs.
// Centralizes launch paths so the top input and per-turn follow-up share one code path.
// Compact transcript of recent conversation turns for the agent run REQUEST (never shown in
// the chat). Every launch path below is otherwise context-free: a follow-up like "continue"
// reached intake with no idea what it was continuing and was rejected as unintelligible.
// Keep it small — newest few finished messages, each truncated; the live bubble is skipped.
static std::string buildTurnChatContext(const AppState& st, size_t maxMsgs = 8,
                                        size_t maxCharsPerMsg = 280) {
    std::vector<std::string> lines;
    for (auto it = st.chat.rbegin(); it != st.chat.rend() && lines.size() < maxMsgs; ++it) {
        if (it->running || it->text.empty()) continue;
        std::string t = it->text;
        if (t.size() > maxCharsPerMsg) t = t.substr(0, maxCharsPerMsg - 1) + "\xe2\x80\xa6";
        lines.push_back(std::string(it->fromUser ? "User: " : "Assistant: ") + t);
    }
    std::string out;
    for (auto it = lines.rbegin(); it != lines.rend(); ++it) { out += *it; out += '\n'; }
    return out;
}

void submitTurn(AppState& st, const std::string& text) {
    if (text.empty()) return;
    // A fresh turn always clears a lingering stop latch (e.g. worker never drained after cancel).
    st.activeRun()->stopRequested = false;
    // Slash commands: /compact /shake /handoff /btw /prewalk /skills /help — pure host+RPC,
    // no agent turn. Skills themselves (/skill:<name>) ride a normal turn and activate in-agent.
    if (tryHandleSlashCommand(st, text)) return;
    // Persist the active provider/model to config.json BEFORE launching any run. The C++ core
    // resolves the provider from config.json (not in-memory st.settings), so this guarantees the
    // run uses exactly what the picker shows — closing the window where a freshly-picked local
    // model still ran against a stale, previously-saved cloud endpoint (the 403 "model_failed").
    // (O1 additionally pins provider/model/effort EXPLICITLY in each RPC payload below, so an
    // CHUDWARE_PROVIDER env var can no longer silently override the picker.)
    saveSettings(st.settings);
    // M23: fold pending attachments into THIS turn. `sendText` (what the model receives) gets
    // inlined text-file content / explicit paths appended; the chat shows a compact marker.
    // Wave4: context_refs ride as structured payload AND are inlined into sendText (C++ agent_run
    // only reads request text — same honesty pattern as attachments).
    std::string sendText = text;
    const nlohmann::json attachments = consumeAttachments(st, sendText);
    const nlohmann::json contextRefs = consumeContextRefs(st, sendText);
    const nlohmann::json provOverride = agentProviderOverride(st.settings);
    // Shared extras (attachments + context_refs) for every agent RPC path below.
    nlohmann::json turnExtras = nlohmann::json::object();
    if (!attachments.empty()) turnExtras["attachments"] = attachments;
    if (!contextRefs.empty()) turnExtras["context_refs"] = contextRefs;
    // Fold in recent conversation context so follow-ups ("continue", "make it bigger") make
    // sense to intake/planning. Only the model payload changes — the visible bubble keeps the
    // raw typed text.
    {
        const std::string ctx = buildTurnChatContext(st);
        if (!ctx.empty())
            sendText = "[conversation so far]\n" + ctx +
                       "[/conversation]\nLatest user message: " + sendText;
    }
    {
        ChatMsg u; u.fromUser = true; u.text = text;
        u.copilotMode = st.aiInteraction;  // stamp composer mode for colored border
        if (!attachments.empty()) {
            // Images get one line each so chat_log.cpp can lift them out and render the real
            // picture; the plain [attached:] roster lists ONLY the non-image files, and is
            // omitted entirely when every attachment was an image. A name never appears twice.
            std::string files;
            for (const auto& a : attachments) {
                const std::string nm = a.value("name", std::string());
                if (a.value("kind", std::string("file")) == "image")
                    u.text += "\n[attached image: " + nm + "]";
                else
                    files += " " + nm;
            }
            if (!files.empty()) u.text += "\n[attached:" + files + "]";
        }
        if (!contextRefs.empty()) {
            u.text += "\n[context:";
            for (const auto& r : contextRefs) {
                std::string lbl = r.value("label", std::string());
                if (lbl.empty()) lbl = r.value("kind", std::string("ref"));
                u.text += " " + lbl;
            }
            u.text += "]";
        }
        st.chat.push_back(u);
        appendChatRawMessage(st, st.chat.back());
    }
    {
        ChatMsg a;
        a.running = true;
        a.copilotMode = st.aiInteraction;  // stamp; auto-switch may update to Agent
        if (st.settings.model[0]) a.model = st.settings.model;
        // Instant classification chrome — don't wait for the first drained event frame.
        a.liveStatus = "Routing request\xe2\x80\xa6";
        st.chat.push_back(a);
        appendChatRawMessage(st, st.chat.back());
    }
    st.designStarted = true;   // conversation is now active (kept for shot scenarios)
    // Sending a message is intent to follow the reply: re-engage stick-to-bottom on every
    // chat surface even if the user had scrolled up into the history.
    chatLogResetStick();

    // Name the conversation tab after its first prompt; persist so the turn survives a restart.
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size()) {
        std::string& title = st.conversations[st.activeConv].title;
        bool isDefault = title.empty() || (title.rfind("Chat ", 0) == 0);
        if (isDefault) title = text.size() > 28 ? (text.substr(0, 27) + "...") : text;
    }
    ui::persistChatHistory(st);

    // Sync persisted settings.copilotMode with live dropdown.
    st.settings.copilotMode = st.aiInteraction;
    const CopilotMode mode = (st.aiInteraction >= 0 && st.aiInteraction <= 5)
                                 ? (CopilotMode)st.aiInteraction
                                 : CopilotMode::Agent;
    const bool editMode  = (mode == CopilotMode::Edit);
    const bool askMode   = (mode == CopilotMode::Ask);
    const bool planMode  = (mode == CopilotMode::Plan);
    const bool swarmMode = (mode == CopilotMode::Swarm);
    const bool agentMode = (mode == CopilotMode::Agent);
    const bool engFirmMode = (mode == CopilotMode::EngFirm);
    (void)agentMode;

    auto withOptions = [&](nlohmann::json base, nlohmann::json opts) -> nlohmann::json {
        if (!opts.is_object()) opts = nlohmann::json::object();
        if (!opts.contains("advisor")) opts["advisor"] = true;
        // Settings gate for company structure auto-route (core recommend_structure).
        opts["corporate_mode"] = st.settings.corporateMode;
        base["options"] = std::move(opts);
        return base;
    };
    auto buildEditScope = [&]() -> nlohmann::json {
        nlohmann::json scope = nlohmann::json::object();
        if (st.docMirror.hasDoc()) scope["doc_id"] = st.docMirror.docId();
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

    std::string lo = text;
    std::transform(lo.begin(), lo.end(), lo.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    auto has = [&](const char* k) { return lo.find(k) != std::string::npos; };

    // Shared intent classifiers (Agent auto-router + Ask promote-to-build).
    // Eng. Firm is NEVER auto-invoked — only the explicit engFirmMode branch may set eng_firm.
    const bool buildIntent = has("make ") || has("model ") || has("build ") || has("design ") ||
                             has("create ") || has("generate ") || has("give me") || has("add ") ||
                             has("cut ") || has("draw ");
    const bool changeIntent = has("edit ") || has("change ") || has("modify ") || has("update ") ||
                              has("fix ") || has("on this") || has("this part") || has("the part") ||
                              // Bare continuation follow-ups — continue the work on the open part.
                              has("continu") || has("go on") || has("keep going") ||
                              has("proceed") || has("resume") || has("carry on") ||
                              has("finish it") || has("keep building");
    const bool questionIntent = !buildIntent && !changeIntent &&
        (lo.find('?') != std::string::npos ||
         lo.rfind("what ", 0) == 0 || lo.find(" what ") != std::string::npos ||
         lo.rfind("how ", 0) == 0 || lo.find(" how ") != std::string::npos ||
         lo.rfind("why ", 0) == 0 || lo.find(" why ") != std::string::npos ||
         lo.rfind("which ", 0) == 0 || lo.find(" which ") != std::string::npos ||
         has("is it") || has("should i") || has("can you tell") || has("explain "));

    // MISSION intent: Agent mode only, and only when Settings → AI → Harness
    // "Auto-route missions" is on. Default off — main chat stays on the direct
    // general_agent tool loop (no intake/breakdown).
    auto looksLikeMission = [&]() -> bool {
        if (!st.settings.autoMissionRouting) return false;
        if (!agentMode || st.convRunning(st.activeConvId())) return false;
        bool buildVerb = has("make ") || has("model ") || has("build ") || has("design ") ||
                         has("create ") || has("generate ") || has("give me");
        bool analyzeVerb = has("cfd") || has("wind tunnel") || has("windtunnel") ||
                           has("aerodynam") || has("simulat") || has("flow sim") || has("fea") ||
                           has("stress") || has("downforce") ||
                           ((has("drag") || has("lift")) && (has("air") || has("speed") ||
                            has("wind") || has("m/s") || has("mph"))) ||
                           has("prove") || has("validate") || has("verify it") ||
                           has("is it a good") || has("good design") || has("analyze");
        return buildVerb && analyzeVerb;
    };
    if (looksLikeMission()) {
        nlohmann::json extras = withOptions(turnExtras, {{"mode", "agent"}, {"advisor", true}});
        launchMissionRunChat(st, sendText, /*needFreshDoc=*/true, extras);
        return;
    }

    // CFD/sim: Agent/Ask only — Plan/Swarm/Edit must not be hijacked.
    auto looksLikeSim = [&]() -> bool {
        if (!(agentMode || askMode)) return false;
        if (!st.docMirror.hasDoc() || st.convRunning(st.activeConvId())) return false;
        if (has("cfd") || has("wind tunnel") || has("windtunnel") || has("aerodynam") ||
            has("drag") || has("downforce"))
            return true;
        return (has("simulate") || has("simulation") || has("flow") || has("lift")) &&
               (has("air") || has("m/s") || has("mph") || has("km/h") || has("speed") ||
                has("velocity") || has("wind"));
    };
    if (looksLikeSim()) {
        st.addLog("[..] cfd: " + text);
        st.status = "Running CFD...";  st.statusKind = 0;
        nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"request", text}};
        const bool wantAccurate = has("accurate") || has("openfoam") || has("open foam") ||
                                  has("precise") || has("high-fidelity") || has("high fidelity") ||
                                  has("rans") || has("validated");
        bool ofAvail = false;
        if (wantAccurate && st.simClient) {
            nlohmann::json caps = st.simClient->caps();
            if (caps.is_object()) ofAvail = caps.value("openfoam", false);
        }
        if (wantAccurate && ofAvail) params["solver"] = "openfoam";
        if (wantAccurate && !ofAvail)
            st.addLog("[cfd] accurate requested but OpenFOAM not provisioned — using LBM estimate");

        st.rpc.call("agent_simulate_v1", params, [&st](const core::RpcResult& r) {
            ChatMsg* msg = nullptr;
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser && it->running) { msg = &*it; break; }
            std::string err;
            if (!r.ok) err = r.err.empty() ? "worker call failed" : r.err;
            else if (r.result.is_object() && r.result.contains("code"))
                err = r.result.value("message", std::string("worker error"));

            if (err.empty() && r.result.value("needs_openfoam", false) && st.simClient) {
                const bool water = (r.result.value("fluid", std::string("air")) == "water");
                nlohmann::json fparams = {
                    {"stl_path", r.result.value("stl_path", std::string())},
                    {"direction", {1.0, 0.0, 0.0}},
                    {"speed_mps", r.result.value("velocity_ms", 10.0)},
                    {"aoa_deg", r.result.value("aoa_deg", 0.0)},
                    {"sideslip_deg", 0.0}, {"preset", "coarse"}, {"ref_area_mm2", 0.0},
                    {"fluid", {{"name", water ? "water" : "air"},
                               {"density_kgm3", water ? 1000.0 : 1.225},
                               {"kinematic_visc_m2s", water ? 1.0e-6 : 1.5e-5}}},
                    {"solver", "openfoam"},
                };
                std::string note;
                if (st.simClient->launch("sim_run_flow", fparams, note, "__chat_cfd__")) {
                    st.chatCfdVel = r.result.value("velocity_ms", 10.0);
                    st.chatCfdAoa = r.result.value("aoa_deg", 0.0);
                    st.chatCfdPending = true;
                    if (msg) msg->liveStatus = "OpenFOAM RANS (a few minutes)...";
                    st.status = "Running OpenFOAM...";  st.statusKind = 0;
                    return;
                }
                err = note.empty() ? "could not launch OpenFOAM" : note;
            }

            if (msg) {
                msg->running = false;
                msg->liveStatus.clear();
                if (!err.empty()) { msg->errKind = ERR_SYSTEM; msg->text = "The CFD sim failed: " + err; }
                else {
                    msg->text = r.result.value("answer", std::string());
                    if (msg->text.empty()) msg->text = "(no result)";
                }
            }
            st.status = err.empty() ? "Ready." : "CFD failed.";
            st.statusKind = err.empty() ? 0 : 2;
        }, 600000);
        return;
    }

    // ---- mode branch table (Agent | Plan | Swarm | Edit | Ask) ----
    if (editMode) {
        if (!st.docMirror.hasDoc()) {
            if (!st.chat.empty() && st.chat.back().running && !st.chat.back().fromUser) {
                st.chat.back().running = false;
                st.chat.back().text = "Edit mode needs an open part — open a document first.";
            }
            return;
        }
        st.addLog("[..] editing: " + text);
        st.status = "Editing...";  st.statusKind = 0;
        st.agentV2Running = true;
        st.agentV2 = true;
        nlohmann::json extra = provOverride;
        for (auto it = turnExtras.begin(); it != turnExtras.end(); ++it)
            extra[it.key()] = it.value();
        nlohmann::json opts = {{"mode", "edit"}, {"advisor", true},
                               {"corporate_mode", st.settings.corporateMode},
                               {"scope", buildEditScope()}};
        attachAgentConversationOptions(st, opts);
        extra["options"] = std::move(opts);
        launchAgentRunV2(st.worker, st.activeRun(), st.activeConvId(), st.chatDocKey, sendText,
                         st.docMirror.docId(), st.ribbonTab == AppState::RT_Manufacture, extra);
        return;
    }

    if (askMode) {
        // Build/change intent in Ask → auto-promote to Agent (never dead-end with
        // "switch modes"). Eng. Firm is NEVER auto-invoked from this path.
        if (buildIntent || changeIntent) {
            if (st.convRunning(st.activeConvId())) {
                if (!st.chat.empty() && st.chat.back().running && !st.chat.back().fromUser) {
                    st.chat.back().running = false;
                    st.chat.back().text = "A design/edit run is still in progress - "
                                          "ask me again when it finishes.";
                }
                return;
            }
            st.addLog("[..] ask→agent (build/change intent): " + text);
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it) {
                if (!it->fromUser && it->running) {
                    it->copilotMode = (int)CopilotMode::Agent;  // border reflects effective mode
                    it->liveStatus = "Auto-switched to Agent to build this";
                    break;
                }
            }
            // Route exactly like Agent default: edit-in-place if features+change, else design.
            bool hasFeatures = false;
            if (st.docMirror.hasDoc()) {
                const nlohmann::json& tree = st.docMirror.tree();
                hasFeatures = tree.is_object() && tree.contains("features") &&
                              tree["features"].is_array() && !tree["features"].empty();
            }
            if (hasFeatures && changeIntent) {
                st.addLog("[..] agent edit-in-place: " + text);
                st.status = "Editing...";  st.statusKind = 0;
                st.agentV2Running = true;
                st.agentV2 = true;
                nlohmann::json extra = provOverride;
                for (auto it = turnExtras.begin(); it != turnExtras.end(); ++it)
                    extra[it.key()] = it.value();
                nlohmann::json opts = {{"mode", "agent"}, {"advisor", true},
                                       {"corporate_mode", st.settings.corporateMode},
                                       {"scope", buildEditScope()}};
                attachAgentConversationOptions(st, opts);
                extra["options"] = std::move(opts);
                launchAgentRunV2(st.worker, st.activeRun(), st.activeConvId(), st.chatDocKey,
                                 sendText, st.docMirror.docId(),
                                 st.ribbonTab == AppState::RT_Manufacture, extra);
                return;
            }
            nlohmann::json agentExtras =
                withOptions(turnExtras, {{"mode", "agent"}, {"advisor", true}});
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser && it->running) { it->liveStatus = "Building..."; break; }
            st.status = "Building...";  st.statusKind = 0;
            launchDesignRun(st, sendText, agentExtras);
            return;
        }
        // Genuine questions stay on agent_ask_v2.
        if (st.convRunning(st.activeConvId())) {
            if (!st.chat.empty() && st.chat.back().running && !st.chat.back().fromUser) {
                st.chat.back().running = false;
                st.chat.back().text = "A design/edit run is still in progress - "
                                      "ask me again when it finishes.";
            }
            return;
        }
        st.addLog("[..] ask: " + text);
        st.status = "Answering...";  st.statusKind = 0;
        nlohmann::json params = {
            {"doc_id", st.docMirror.hasDoc() ? st.docMirror.docId() : std::string()},
            {"question", sendText},
            {"options", {{"mode", "ask"}, {"advisor", true}}}};
        for (auto it = provOverride.begin(); it != provOverride.end(); ++it)
            params[it.key()] = it.value();
        for (auto it = turnExtras.begin(); it != turnExtras.end(); ++it)
            if (it.key() != "options") params[it.key()] = it.value();
        st.rpc.call("agent_ask_v2", params, [&st](const core::RpcResult& r) {
            ChatMsg* msg = nullptr;
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                if (!it->fromUser && it->running) { msg = &*it; break; }
            std::string err;
            if (!r.ok)
                err = r.err.empty() ? "worker call failed" : r.err;
            else if (r.result.is_object() && r.result.contains("code"))
                err = r.result.value("message", std::string("worker error"));
            if (msg) {
                msg->running = false;
                msg->liveStatus.clear();
                if (!err.empty()) {
                    msg->errKind = ERR_SYSTEM;
                    msg->text = "I couldn't answer that: " + err;
                } else {
                    msg->text = r.result.value("answer", std::string());
                    if (msg->text.empty()) msg->text = "(no answer)";
                }
            }
            if (!err.empty()) {
                st.addLog("[err] ask: " + err);
                st.status = "Ask failed.";  st.statusKind = 2;
            } else {
                st.status = "Ready.";  st.statusKind = 0;
            }
        }, 300000);
        return;
    }

    if (swarmMode) {
        st.addLog("[..] swarm: " + text);
        // Agent-decided plan is advisor-approved in-core — no user approve card.
        // liveStatus shows drafting then execution continues automatically.
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
            if (!it->fromUser && it->running) {
                it->liveStatus = "Drafting plan for swarm execution\xe2\x80\xa6";
                break;
            }
        st.status = "Drafting plan for swarm execution...";  st.statusKind = 0;
        nlohmann::json extras = withOptions(turnExtras, {
            {"mode", "swarm"},
            {"corporate_force", true},
            {"advisor", true}});
        bool needFresh = true;
        if (st.docMirror.hasDoc()) {
            const nlohmann::json& tree = st.docMirror.tree();
            bool hasFeatures = tree.is_object() && tree.contains("features") &&
                               tree["features"].is_array() && !tree["features"].empty();
            if (hasFeatures && (has("edit ") || has("change ") || has("modify ") ||
                                has("update ") || has("fix ") || has("on this")))
                needFresh = false;
        }
        launchMissionRunChat(st, sendText, needFresh, extras);
        return;
    }

    if (engFirmMode) {
        // USER-ONLY: Eng. Firm is never auto-invoked by Agent/Ask auto-routing.
        // Agent-decided plan is advisor-approved in-core — no user approve card.
        st.addLog("[..] eng.firm: " + text);
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
            if (!it->fromUser && it->running) {
                it->liveStatus = "Drafting plan for eng. firm execution\xe2\x80\xa6";
                break;
            }
        st.status = "Drafting plan for eng. firm execution...";  st.statusKind = 0;
        nlohmann::json extras = withOptions(turnExtras, {
            {"mode", "eng_firm"},
            {"org", "eng_firm"},
            {"corporate_force", true},
            {"execute_as", "eng_firm"},
            {"advisor", true}});
        bool needFresh = true;
        if (st.docMirror.hasDoc()) {
            const nlohmann::json& tree = st.docMirror.tree();
            bool hasFeatures = tree.is_object() && tree.contains("features") &&
                               tree["features"].is_array() && !tree["features"].empty();
            if (hasFeatures && changeIntent)
                needFresh = false;
        }
        launchMissionRunChat(st, sendText, needFresh, extras);
        return;
    }

    if (planMode) {
        st.addLog("[..] plan: " + text);
        st.status = "Planning...";  st.statusKind = 0;
        st.agentV2Running = true;
        st.agentV2 = true;
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
            if (!it->fromUser && it->running) {
                it->planApprovePending = true;
                it->planApproveRequest = sendText;
                it->planApproveExtras = turnExtras;
                it->liveStatus = "Drafting a plan...";
                break;
            }
        nlohmann::json extra = provOverride;
        for (auto it = turnExtras.begin(); it != turnExtras.end(); ++it)
            extra[it.key()] = it.value();
        extra["options"] = {{"mode", "plan"}, {"plan_only", true}, {"advisor", true},
                            {"corporate_mode", st.settings.corporateMode}};
        attachAgentConversationOptions(st, extra["options"]);
        std::string err;
        if (!st.docMirror.hasDoc()) {
            if (!st.docMirror.newPart(st.worker, err)) {
                st.addLog("[err] plan: could not create a part document: " + err);
                st.status = "Worker not ready";  st.statusKind = 2;
                for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                    if (!it->fromUser && it->running) {
                        it->running = false;
                        it->planApprovePending = false;
                        it->errKind = ERR_SYSTEM;
                        it->text = "I couldn't open a part document (worker not ready).";
                        break;
                    }
                return;
            }
        }
        launchAgentRunV2(st.worker, st.activeRun(), st.activeConvId(), st.chatDocKey, sendText,
                         st.docMirror.docId(), st.ribbonTab == AppState::RT_Manufacture, extra);
        return;
    }

    // Agent (default) — smart auto-router: pure questions → ask; edit-in-place when
    // features+change; else design/clarify. Never sets mode/org eng_firm.
    st.designStarted = true;
    {
        bool hasFeatures = false;
        if (st.docMirror.hasDoc()) {
            const nlohmann::json& tree = st.docMirror.tree();
            hasFeatures = tree.is_object() && tree.contains("features") &&
                          tree["features"].is_array() && !tree["features"].empty();
        }
        // Pure question (no build/change) → agent_ask_v2 so Agent auto-invokes Ask.
        if (questionIntent && !(hasFeatures && changeIntent)) {
            if (st.convRunning(st.activeConvId())) {
                if (!st.chat.empty() && st.chat.back().running && !st.chat.back().fromUser) {
                    st.chat.back().running = false;
                    st.chat.back().text = "A design/edit run is still in progress - "
                                          "ask me again when it finishes.";
                }
                return;
            }
            st.addLog("[..] agent→ask (question intent): " + text);
            for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it) {
                if (!it->fromUser && it->running) {
                    // Keep Agent border (user picked Agent); liveStatus notes the ask path.
                    it->liveStatus = "Answering...";
                    break;
                }
            }
            st.status = "Answering...";  st.statusKind = 0;
            nlohmann::json params = {
                {"doc_id", st.docMirror.hasDoc() ? st.docMirror.docId() : std::string()},
                {"question", sendText},
                {"options", {{"mode", "ask"}, {"advisor", true}}}};
            for (auto it = provOverride.begin(); it != provOverride.end(); ++it)
                params[it.key()] = it.value();
            for (auto it = turnExtras.begin(); it != turnExtras.end(); ++it)
                if (it.key() != "options") params[it.key()] = it.value();
            st.rpc.call("agent_ask_v2", params, [&st](const core::RpcResult& r) {
                ChatMsg* msg = nullptr;
                for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
                    if (!it->fromUser && it->running) { msg = &*it; break; }
                std::string err;
                if (!r.ok)
                    err = r.err.empty() ? "worker call failed" : r.err;
                else if (r.result.is_object() && r.result.contains("code"))
                    err = r.result.value("message", std::string("worker error"));
                if (msg) {
                    msg->running = false;
                    msg->liveStatus.clear();
                    if (!err.empty()) {
                        msg->errKind = ERR_SYSTEM;
                        msg->text = "I couldn't answer that: " + err;
                    } else {
                        msg->text = r.result.value("answer", std::string());
                        if (msg->text.empty()) msg->text = "(no answer)";
                    }
                }
                if (!err.empty()) {
                    st.addLog("[err] ask: " + err);
                    st.status = "Ask failed.";  st.statusKind = 2;
                } else {
                    st.status = "Ready.";  st.statusKind = 0;
                }
            }, 300000);
            return;
        }
        // Shared changeIntent (above) — edit-in-place when doc has features.
        if (hasFeatures && changeIntent) {
            st.addLog("[..] agent edit-in-place: " + text);
            st.status = "Editing...";  st.statusKind = 0;
            st.agentV2Running = true;
            st.agentV2 = true;
            nlohmann::json extra = provOverride;
            for (auto it = turnExtras.begin(); it != turnExtras.end(); ++it)
                extra[it.key()] = it.value();
            nlohmann::json opts = {{"mode", "agent"}, {"advisor", true},
                                   {"corporate_mode", st.settings.corporateMode},
                                   {"scope", buildEditScope()}};
            attachAgentConversationOptions(st, opts);
            extra["options"] = std::move(opts);
            launchAgentRunV2(st.worker, st.activeRun(), st.activeConvId(), st.chatDocKey, sendText,
                             st.docMirror.docId(), st.ribbonTab == AppState::RT_Manufacture,
                             extra);
            return;
        }
    }
    nlohmann::json agentExtras = withOptions(turnExtras, {{"mode", "agent"}, {"advisor", true}});
    // Agent build → straight to the streaming design run. The old agent_clarify_v1
    // pre-pass was a BLOCKING, non-streaming one-shot ("Working out what I need to build
    // this...") that showed no progress and could stall up to a minute on a slow provider
    // call. The design run's intake stage does the same spec parsing AND streams live
    // (assistant_delta waterfall) + emits agent.classification (the Routing card) +
    // plan_state, so the classifier is fully transparent. Supervised plan-approval
    // remains available via the composer Plan mode.
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (!it->fromUser && it->running) {
            // Progressive chrome: Routing → Classifying as soon as the design path starts.
            it->liveStatus = "Classifying\xe2\x80\xa6";
            break;
        }
    launchDesignRun(st, sendText, agentExtras);
}

// ---- runtime-not-ready banner (kept from the old Design body) ----
void drawRuntimeBanner(AppState& st) {
    if (st.workerStarted) return;
    // The in-process C++ core (libchudware) is a complete backend on its own (doc/agent/sim route
    // through it), so don't nag about the Python runtime when it's loaded.
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
#ifdef _WIN32
        dimText("Run the packaged build (dist) or install.ps1 so the engine can self-install.");
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

// OMP provider id for the brand mark. This is the id space the Settings -> Providers rows hand
// to drawProviderLogo, so the chip shows the IDENTICAL art. The resolve_provider wire name is a
// DIFFERENT space ("kimi-sub" has no brand row; local endpoints resolve as "openai") and would
// give a hashed badge or, worse, brand a loopback runtime as OpenAI.
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

// The model's REAL display name ("Claude Sonnet 5"). Resolution order, best label first:
//   1. the OMP bridge catalog (live, covers models the static table has never heard of),
//   2. the static builtinModels() table (works offline / before the bridge catalog lands —
//      without it the chip fell straight through to the bare id, which is exactly the
//      "claude-sonnet-5" wire string the chip exists to replace),
//   3. shortModelName, 4. the raw id.
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
//   3. effectiveContextWindow() — the curated OMP catalog row for the SELECTED model, then an id
//      estimate (ui_common.cpp).
//   4. st.lastContextWindow even when its source is heuristic/unstated — better than nothing,
//      but only once 3 has produced nothing at all.
//
// Why a heuristic server value must LOSE to the catalog, measured on this box 2026-07-28 and not
// to be "simplified" back: services/agent/model_catalog.resolve_capabilities("anthropic",
// "claude-opus-5") returns context=200000 with auto_source="heuristic" — a hardcoded
// per-PROVIDER default, not a fact about that model — while the live OMP catalog row for the
// same id reports contextWindow=1000000. Ranking the server unconditionally above the catalog
// (as this function did before) made the first provider call DOWNGRADE a correct 1.0M readout to
// a wrong 200k, i.e. the meter got worse the moment it had data.
// 0 = genuinely unknown, never a real capacity.
static int contextMeterWindow(const AppState& st) {
    if (st.settings.contextWindow > 0) return st.settings.contextWindow;
    if (st.lastContextWindow > 0 && ctxWindowSourceTrusted()) return st.lastContextWindow;
    if (const int eff = effectiveContextWindow(st.settings); eff > 0) return eff;
    return st.lastContextWindow > 0 ? st.lastContextWindow : 0;
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
struct CtxMeterText {
    std::string full;      // ring-adjacent readout (or the bare unknown mark)
    bool        ringOk;    // false = window unknown; draw the compact tag only, never a ring
};
static CtxMeterText contextMeterText(const AppState& st) {
    const int win = contextMeterWindow(st);
    if (win <= 0) return {std::string("\xe2\x80\x94"), false};
    const CtxMeterUsage u = contextMeterUsage(st);
    std::string num = u.tokens <= 0  ? std::string("\xe2\x80\x94")
                      : u.estimate   ? "~" + fmtTokens(u.tokens)
                                     : fmtTokens(u.tokens);
    return {num + "/" + fmtTokens(win), true};
}

// Context-window gauge for the composer prelude row: a fraction ring + "N/M" readout (compact
// "ctx N/M" with no ring when the pane is too narrow, or when the window itself is unknown).
// Drawn at the current cursor; advances it.
// Moved here from the footer chips, where it was pinned to the pane bottom against the status bar.
static void drawContextMeter(AppState& st, float roomForMeter) {
    refreshPromptBaseline(st);   // async, guarded; never a synchronous call on a draw path
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();
    const int   win  = contextMeterWindow(st);
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
        const float* role = frac < 0.5f  ? pal.statusOk
                          : frac < 0.85f ? pal.statusWarn
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
        } else if (effectiveContextWindow(st.settings) > 0) {
            const ui::BridgeModelMeta* bm = ui::bridgeModelMetaForCad(
                st.settings.provider, st.settings.model, st.settings.baseUrl);
            srcTxt = (bm && bm->contextWindow > 0)
                         ? "from the model catalog entry for the selected model"
                         : "a fallback estimate from the model id \xe2\x80\x94 no catalog entry "
                           "for it yet";
        } else if (st.lastContextWindow > 0) {
            // Only reachable once the catalog answered nothing at all: a heuristic is the last
            // thing left, and it says so instead of posing as a probe.
            srcTxt = "reported by the harness (rough per-provider default)";
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
            const float* role = frac < 0.5f  ? pal.statusOk
                              : frac < 0.85f ? pal.statusWarn
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
// Two upstream stores can answer, and the omp bridge is asked FIRST: a Claude Max / ZenMux /
// Kimi Code session signs in through the sidecar's own credential store, so the bridge is the
// only source that knows about it — and it is the one Settings → Providers already agrees with.
// The Python subscription poller serves everything else (xAI, Cursor) and remains the fallback
// for a bridge provider that has not answered yet.
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
// stronger fill + mode-colored glow outline; hover brightens. Returns true when clicked
// and enabled. Header colors are pushed transparent by the caller so the wash is the
// only row background.
static bool modeMenuRow(const char* iconName, const char* label, bool selected,
                        bool disabled, int uid) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const float rowH = ImGui::GetFrameHeight() + 2.0f * sc;
    const float fh = ImGui::GetFontSize();
    ImGui::PushID(uid);
    if (disabled) ImGui::BeginDisabled();
    const bool clicked = ImGui::Selectable("##moderow", selected,
                                           ImGuiSelectableFlags_AllowItemOverlap, ImVec2(0, rowH));
    const bool hov = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
    // Logical dp rounding (helpers multiply by uiScale) — matches Layers/rail-tab pills.
    const float rnd = 4.0f;
    const float washA = disabled ? 0.18f : (selected ? 0.42f : (hov ? 0.36f : 0.28f));
    ui::glassRowBackground(dl, mn, mx, rnd,
                           /*stripe=*/false, /*selected=*/false, /*washAlpha=*/washA);
    float tint[3];
    modeAccentRgb(uid, tint);
    // Resting rows still show a clear mode tint; hover brightens; selected strongest + glow.
    const float strength = disabled ? 0.10f
                         : (selected ? 0.38f
                         : (hov ? 0.28f : 0.18f));
    ui::coloredRowWash(dl, mn, mx, rnd, tint, strength, /*selected=*/selected && !disabled);
    const float midY = (mn.y + mx.y) * 0.5f;
    const float pad = 8.0f * sc;
    const float isz = fh * 1.02f;
    const ImU32 ink = ui::Palette::toU32(disabled ? pal.textDisabled
                                                  : (selected ? pal.text : pal.textDim));
    float textX = mn.x + pad;
    IconUV ic = ui::getIcon(iconName, 20);
    if (ic.valid) {
        const ImVec2 i0(mn.x + pad, midY - isz * 0.5f);
        dl->AddImage(ic.texId, i0, ImVec2(i0.x + isz, i0.y + isz), ic.uv0, ic.uv1, ink);
        textX = i0.x + isz + 7.0f * sc;
    }
    dl->AddText(ImVec2(textX, midY - fh * 0.5f),
                ui::Palette::toU32(disabled ? pal.textDisabled : pal.text), label);
    if (disabled) ImGui::EndDisabled();
    ImGui::PopID();
    return clicked && !disabled;
}


// Prelude row above the prompt: Agent/Plan/Swarm/Edit/Ask dropdown (left) + busy mark
// (inline, only while a run is active) + context meter (right).
// Permissions live only in the Settings overlay (not the composer).
static void drawComposerPrelude(AppState& st, bool busy) {

    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const bool hasDoc = st.docMirror.hasDoc();
    const ui::AdaptiveSurface& surf = chatSurf();
    // Subscription usage feeds the prelude's provider-limit rings. Both polls are TTL-gated and
    // cheap; the 60s accounts TTL is deliberately lazier than the Settings UI's 1.5s — nothing is
    // being MANAGED here, the rings just need the active account's windows to stay roughly fresh.
    ui::providerUsagePollAccounts(st, 60.0, false);
    ui::providerUsagePollUsage(st, false);
    // Bridge-owned providers (Claude Max / ZenMux / Kimi Code) keep their windows in the omp
    // sidecar's credential store, which the two subscription RPCs above cannot see. Only the
    // ACTIVE provider is polled, on the same 5-min TTL — one RPC per provider per five minutes.
    if (const char* bridgeUsageId = ui::bridgeProviderForCad(st.settings.provider))
        ui::providerUsagePollBridge(st, bridgeUsageId, false);
    // The model chip's human name ("Claude Opus 5") comes from the OMP catalog. Until now the
    // catalog was only fetched by the Settings/Providers UI and the model dropdown, so a user who
    // never opened either saw the raw id in the chip — the exact thing the chip stopped showing.
    // Same 5-min TTL + in-flight guard, so this is one RPC per session in practice.
    ui::refreshBridgeModelCatalog(st, false);

    // Compact frame height matching the toolbar chips (model / thinking / New).
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

    // Agent / Plan / Swarm / Edit / Ask — drop-UP menu that propagates upward like the model
    // picker, with an atlas icon + glass row styling per entry.
    static const char* modes[]     = {"Agent", "Plan", "Swarm", "Edit", "Ask", "Eng. Firm"};
    static const char* modeIcons[] = {"ai_panel", "plan_board", "team", "msg_edit", "ask",
                                      "eng_firm"};
    static const char* modeTips[]  = {
        "Agent — full tools; auto-routes (may plan, swarm, or edit).",
        "Plan — draft a plan only; approve before building.",
        "Swarm — force multi-agent / corporate mission execution.",
        "Edit — change the open part (scoped to selection when set).",
        "Ask — questions about the part; nothing changes.",
        "Eng. Firm — engineering firm: specialist roles + contractor firms bid on tasks "
        "within a $ budget for the heaviest jobs."};
    int mode = (st.aiInteraction >= 0 && st.aiInteraction <= 5) ? st.aiInteraction : 0;
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
        for (int i = 0; i < 6; ++i) {
            const bool needDoc = (i == (int)CopilotMode::Edit && !hasDoc);
            if (modeMenuRow(modeIcons[i], modes[i], mode == i, needDoc, i)) {
                st.aiInteraction = i;
                st.settings.copilotMode = i;
                saveSettings(st.settings);  // survive restart without waiting for next send
                ImGui::CloseCurrentPopup();
            }
            if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                ImGui::SetTooltip("%s", needDoc ? "Edit — open a part first." : modeTips[i]);
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

// ---- voice input (OMP ASR sidecar via agent_stt_*) --------------------------------------------
enum class VoiceUiState { Idle, Listening, Processing, Error, Unavailable };

struct VoiceInputUi {
    VoiceUiState state = VoiceUiState::Unavailable;
    bool probed = false;
    bool rpcBusy = false;
    bool pollQueued = false;
    std::string reason;       // unavailable / error tip
    std::string modelLabel;
    std::string sizeHint;
    bool modelCached = false;
    std::string partial;
    std::string committed;
    float level = 0.0f;
    float animT = 0.0f;
    std::string basePrompt;   // composer text at start of this listen session
    double lastPollSec = 0.0;
};

static VoiceInputUi& voiceUi() {
    static VoiceInputUi v;
    return v;
}

static void voiceApplyPrompt(AppState& st, const std::string& base, const std::string& committed,
                             const std::string& partial) {
    std::string out = base;
    auto append = [&](const std::string& piece) {
        if (piece.empty()) return;
        if (!out.empty() && out.back() != ' ' && out.back() != '\n') out.push_back(' ');
        out += piece;
    };
    append(committed);
    append(partial);
    if (st.prompt.size() < out.size() + 1) st.prompt.resize(out.size() + 256, 0);
    std::memcpy(st.prompt.data(), out.c_str(), out.size() + 1);
}

static void voiceApplyStatus(AppState& st, const nlohmann::json& r) {
    VoiceInputUi& v = voiceUi();
    const bool available = r.value("available", false);
    const std::string bridgeState = r.value("state", std::string("idle"));
    v.modelLabel = r.value("modelLabel", std::string());
    v.sizeHint = r.value("modelSizeHint", std::string());
    v.modelCached = r.value("modelCached", false);
    v.partial = r.value("partial", std::string());
    v.committed = r.value("committed", std::string());
    v.level = r.value("level", 0.0f);
    if (r.contains("error") && r["error"].is_string())
        v.reason = r["error"].get<std::string>();
    else if (r.contains("reason") && r["reason"].is_string())
        v.reason = r["reason"].get<std::string>();

    if (!available) {
        v.state = VoiceUiState::Unavailable;
        return;
    }
    if (bridgeState == "listening") {
        v.state = VoiceUiState::Listening;
        voiceApplyPrompt(st, v.basePrompt, v.committed, v.partial);
    } else if (bridgeState == "processing") {
        v.state = VoiceUiState::Processing;
    } else if (bridgeState == "error") {
        v.state = VoiceUiState::Error;
    } else {
        v.state = VoiceUiState::Idle;
    }
}

static void voiceProbe(AppState& st) {
    VoiceInputUi& v = voiceUi();
    if (v.probed || v.rpcBusy) return;
#ifdef __EMSCRIPTEN__
    v.probed = true;
    v.state = VoiceUiState::Unavailable;
    v.reason = "Voice input is not available in the web build.";
    return;
#else
    v.rpcBusy = true;
    st.rpc.call("agent_stt_status", nlohmann::json::object(),
                [](const core::RpcResult& r) {
                    VoiceInputUi& vv = voiceUi();
                    vv.rpcBusy = false;
                    vv.probed = true;
                    if (!r.ok || !r.result.is_object()) {
                        vv.state = VoiceUiState::Unavailable;
                        vv.reason = r.err.empty()
                                        ? "Speech-to-text unavailable (OMP bridge)."
                                        : r.err;
                        return;
                    }
                    // AppState not captured — status-only; prompt rewrite needs st in poll.
                    const auto& res = r.result;
                    vv.modelLabel = res.value("modelLabel", std::string());
                    vv.sizeHint = res.value("modelSizeHint", std::string());
                    vv.modelCached = res.value("modelCached", false);
                    if (!res.value("available", false)) {
                        vv.state = VoiceUiState::Unavailable;
                        vv.reason = res.value("reason",
                                              std::string("Speech-to-text unavailable."));
                    } else {
                        vv.state = VoiceUiState::Idle;
                        vv.reason.clear();
                    }
                },
                15000);
#endif
}

static void voicePoll(AppState& st) {
    VoiceInputUi& v = voiceUi();
    if (v.state != VoiceUiState::Listening && v.state != VoiceUiState::Processing) return;
    if (v.rpcBusy || v.pollQueued) return;
    const double now = ImGui::GetTime();
    if (now - v.lastPollSec < 0.20) return;
    v.lastPollSec = now;
    v.pollQueued = true;
    st.rpc.call("agent_stt_status", nlohmann::json::object(),
                [&st](const core::RpcResult& r) {
                    VoiceInputUi& vv = voiceUi();
                    vv.pollQueued = false;
                    if (!r.ok || !r.result.is_object()) return;
                    voiceApplyStatus(st, r.result);
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
    v.rpcBusy = true;
    v.state = VoiceUiState::Processing;  // warming / opening mic
    st.rpc.call("agent_stt_start", nlohmann::json::object(),
                [&st](const core::RpcResult& r) {
                    VoiceInputUi& vv = voiceUi();
                    vv.rpcBusy = false;
                    if (!r.ok || !r.result.is_object()) {
                        vv.state = VoiceUiState::Error;
                        vv.reason = r.err.empty() ? "Failed to start voice input." : r.err;
                        st.toast(ui::ToastLevel::Warn, vv.reason.c_str());
                        return;
                    }
                    voiceApplyStatus(st, r.result);
                    if (vv.state == VoiceUiState::Error ||
                        vv.state == VoiceUiState::Unavailable) {
                        st.toast(ui::ToastLevel::Warn,
                                 vv.reason.empty() ? "Voice input unavailable." : vv.reason.c_str());
                    }
                },
                300000);  // first-use model download can take a while
}

static void voiceStop(AppState& st) {
    VoiceInputUi& v = voiceUi();
    if (v.rpcBusy) return;
    v.rpcBusy = true;
    v.state = VoiceUiState::Processing;
    st.rpc.call("agent_stt_stop", nlohmann::json::object(),
                [&st](const core::RpcResult& r) {
                    VoiceInputUi& vv = voiceUi();
                    vv.rpcBusy = false;
                    if (!r.ok || !r.result.is_object()) {
                        vv.state = VoiceUiState::Error;
                        vv.reason = r.err.empty() ? "Transcription failed." : r.err;
                        st.toast(ui::ToastLevel::Warn, vv.reason.c_str());
                        return;
                    }
                    const std::string text = r.result.value("text", std::string());
                    voiceApplyPrompt(st, vv.basePrompt, text, "");
                    vv.partial.clear();
                    vv.committed = text;
                    vv.state = VoiceUiState::Idle;
                    if (text.empty())
                        st.toast(ui::ToastLevel::Info, "No speech detected.");
                },
                120000);
}

static void voiceCancel(AppState& st) {
    VoiceInputUi& v = voiceUi();
    if (v.rpcBusy && v.state != VoiceUiState::Listening) return;
    v.rpcBusy = true;
    st.rpc.call("agent_stt_cancel", nlohmann::json::object(),
                [&st](const core::RpcResult& r) {
                    VoiceInputUi& vv = voiceUi();
                    vv.rpcBusy = false;
                    (void)r;
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

// Toolbar row below the prompt: [model | thinking | paperclip | mic | context-cube | New]
// Graduated widths with overlapping rounded corners (connected segmented look).
// Model picker is the largest share; paperclip/mic/cube are icon-only minimums.
// The send/stop control lives inside the input box (drawComposerSendStop).
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
    // Negative spacing so adjacent rounded chips overlap into a segmented strip.
    const float overlap = -2.0f * sc;
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(overlap, ImGui::GetStyle().ItemSpacing.y));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::cpMenuChipRound());
    // Shared translucent chip fill (opaque fills fought the adaptive text).
    const ImVec4 chipBg(0, 0, 0, 0);
    const ImVec4 chipHov(
        pal.buttonHovered[0], pal.buttonHovered[1], pal.buttonHovered[2], 0.55f);
    const ImVec4 chipAct(
        pal.buttonActive[0], pal.buttonActive[1], pal.buttonActive[2], 0.60f);
    ImGui::PushStyleColor(ImGuiCol_Button, chipBg);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, chipHov);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, chipAct);

    // Thinking effort — ALWAYS present. Show just the level (no "Thinking: " prefix)
    // so the model label has room to extend left.
    Settings& s = st.settings;
    const char* effLabels[8] = {};
    const char* effValues[8] = {};
    int nEff = composerEffortLadder(s, effLabels, effValues, 8);
    if (nEff <= 0) {
        static const char* kFbLabels[] = {"Low", "Medium", "High"};
        static const char* kFbValues[] = {"low", "medium", "high"};
        for (int i = 0; i < 3; ++i) { effLabels[i] = kFbLabels[i]; effValues[i] = kFbValues[i]; }
        nEff = 3;
    }
    int effIdx = -1;
    for (int i = 0; i < nEff; ++i)
        if (std::strcmp(s.reasoningEffort, effValues[i]) == 0) effIdx = i;
    const char* effBtn = effIdx >= 0 ? effLabels[effIdx] : "Default";

    // Graduated segment widths (left→right: model, thinking, paperclip, mic, cube, New).
    // Icon chips stay near-square; New < thinking < model share the remaining row.
    const int nSeg = 6;
    const float segH = ImGui::GetFrameHeight();
    const float iconSz = segH;
    const float join = -overlap;  // positive join thickness per boundary
    const float totalW = availW + join * (float)(nSeg - 1);
    const float iconW = segH;     // minimal — just an icon
    const float restW = std::max(0.0f, totalW - iconW * 3.0f);
    // Relative shares of the non-icon remainder, solved against the DEFAULT 340dp column
    // (metrics::dockRightTarget): availW 326 -> restW 287dp at 17.5px body text. "New" needs
    // 48dp and the widest effort label ("Medium") 76dp before either clips inside its button, so
    // the model chip may take the remaining ~159dp — which is what a brand mark (18dp) + gap +
    // a real name like "Claude Opus 5" (117dp) actually occupies. Dropping the model share
    // further (the chip lost its "Provider \xc2\xb7 " prefix) would just ellipsize the ONE string
    // this chip exists to show, so the prefix's slack goes to thinking/New instead.
    const float kNew = 1.30f, kThink = 2.05f, kModel = 4.15f;
    const float kSum = kNew + kThink + kModel;
    const float newW   = restW * (kNew   / kSum);
    const float thinkW = restW * (kThink / kSum);
    const float modelW = restW * (kModel / kSum);

    // ---- 1/6 model: provider brand mark + the model's REAL display name (no provider text) ----
    const std::string modelLogo = composerProviderLogoId(s);
    const float logoSide    = ImGui::GetFontSize() * 1.05f;
    const float logoGap     = ui::metrics::px(6);
    const float logoAdvance = logoSide + logoGap;
    std::string modelText = composerModelName(s);
    {
        // Ellipsize the NAME only — the mark is fixed-width and never clipped. No caret/arrow.
        const float maxTextW = modelW - logoAdvance - ImGui::GetStyle().FramePadding.x * 2.0f;
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

    // ---- 2/6 thinking effort (level only; bigger than New) ----
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    if (ImGui::Button(effBtn, ImVec2(thinkW, segH))) ImGui::OpenPopup("##cmpeffort");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Thinking effort for the selected model.");
    if (ImGui::BeginPopup("##cmpeffort")) {
        for (int i = 0; i < nEff; ++i) {
            if (ImGui::Selectable(effLabels[i], effIdx == i)) {
                std::strncpy(s.reasoningEffort, effValues[i], sizeof(s.reasoningEffort) - 1);
                saveSettings(st.settings);   // the core reads config.json — persist immediately
            }
        }
        ImGui::EndPopup();
    }

    // ---- 3/6 paperclip (icon-only, smallest) ----
    ImGui::SameLine();
    {
        if (ImGui::Button("##attach", ImVec2(iconW, segH))) {
            if (std::getenv("CHUDWARE_NO_FILE_DIALOGS")) {
                st.toast(ui::ToastLevel::Info,
                         "File dialogs are disabled (CHUDWARE_NO_FILE_DIALOGS).");
            } else {
                std::string p = openFileDialog("All files (*.*)\0*.*\0");
                if (!p.empty() &&
                    std::find(st.pendingAttachments.begin(), st.pendingAttachments.end(),
                              p) == st.pendingAttachments.end()) {
                    st.pendingAttachments.push_back(p);
                    savePromptDraft(st, /*force=*/true);
                }
            }
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Attach a file — it rides your next message (small text "
                              "files are inlined; other files are passed by path).");
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

    // ---- 4/6 mic (OMP ASR voice input) ----
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
                    ? "Voice input unavailable — ASR runtime or microphone capture missing."
                    : v.reason.c_str());
            } else if (listening) {
                ImGui::SetTooltip("Listening — click to stop & insert text (Esc/right-click cancels).");
            } else if (processing) {
                ImGui::SetTooltip("Processing speech… click to cancel.");
            } else if (errored) {
                ImGui::SetTooltip("%s\nClick to retry.",
                                  v.reason.empty() ? "Voice input error." : v.reason.c_str());
            } else {
                const char* cacheHint = v.modelCached ? "model ready" : "first use downloads model";
                ImGui::SetTooltip("Dictate into the composer (%s%s%s — %s).\nClick to start; click again to stop.",
                                  v.modelLabel.empty() ? "Whisper" : v.modelLabel.c_str(),
                                  v.sizeHint.empty() ? "" : ", ",
                                  v.sizeHint.c_str(),
                                  cacheHint);
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
            IconUV mic = ui::getIcon("mic", 20);
            const float pulse = listening
                ? 0.5f + 0.5f * std::sin(v.animT * 4.0f)
                : (processing ? 0.5f + 0.5f * std::sin(v.animT * 6.0f) : 0.0f);
            if (mic.valid && !listening) {
                mdl->AddImage(mic.texId,
                              ImVec2(mc.x - isz * 0.5f, mc.y - isz * 0.5f),
                              ImVec2(mc.x + isz * 0.5f, mc.y + isz * 0.5f),
                              mic.uv0, mic.uv1, ink);
            } else {
                drawMicGlyph(mdl, mc, isz, ink, pulse, v.level, listening || processing);
            }
            if (listening) {
                mdl->AddRect(mmn, mmx,
                             ImGui::ColorConvertFloat4ToU32(
                                 ImVec4(pal.accent[0], pal.accent[1], pal.accent[2],
                                        0.55f + 0.30f * pulse)),
                             ui::metrics::rounding(4.0f), 0, ui::metrics::stroke(1.4f));
            }
        }
    }

    // ---- 5/6 context-cube (icon-only, smallest) ----
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

    // ---- 6/6 New session (slightly bigger than icon chips) ----
    ImGui::SameLine();
    {
        if (busy) ImGui::BeginDisabled();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        if (ImGui::Button("New", ImVec2(newW, segH))) {
            newConversation(st);
            st.prompt[0] = 0;
            st.pendingAttachments.clear();
            st.pendingContextRefs.clear();
            st.contextPickActive = false;
            st.showAISettingsOverlay = false;
        }
        ImGui::PopStyleColor();
        if (busy) ImGui::EndDisabled();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Start a fresh conversation.");
    }

    ImGui::PopStyleColor(3);   // button base / hovered / active
    ImGui::PopStyleVar(3);     // FramePadding + ItemSpacing + FrameRounding

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
        (void)modelBtnMax;
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
    struct Job { std::string runId; std::string steer; };  // empty steer = cancel
    std::mutex mtx;
    std::condition_variable cv;
    std::deque<Job> q;
    std::deque<std::string> pendingLogs;   // GL-thread-only drain via agentControlDrainLogs()
    std::atomic<AppState*> st{nullptr};

    void pushLog(const std::string& msg) {
        std::lock_guard<std::mutex> lk(mtx);
        pendingLogs.push_back(msg);
    }

    AgentControlChannel() {
        std::thread([this] {
            for (;;) {
                Job job;
                {
                    std::unique_lock<std::mutex> lk(mtx);
                    cv.wait(lk, [&] { return !q.empty(); });
                    job = std::move(q.front());
                    q.pop_front();
                }
                AppState* s = st.load(std::memory_order_acquire);
                if (!s) continue;
                nlohmann::json r;
                std::string e;
                if (job.steer.empty()) {
                    try {
                        s->worker.callAgentCancel(job.runId, r, e);
                        if (!e.empty())
                            pushLog(std::string("[..] agent_cancel: ") + e);
                        else
                            pushLog("[..] agent_cancel ok cancelled=" +
                                    std::string(r.value("cancelled", false) ? "true" : "false"));
                    } catch (const std::exception& ex) {
                        pushLog(std::string("[..] agent_cancel exception: ") + ex.what());
                    } catch (...) {
                        pushLog("[..] agent_cancel exception");
                    }
                } else {
                    s->worker.callAgentSteer(job.runId, job.steer, r, e);
                    if (!e.empty()) {
                        pushLog(std::string("[..] agent_steer: ") + e);
                    } else if (!r.value("steered", false)) {
                        // Run finished between submit and delivery — tell the user their
                        // text was NOT consumed rather than silently dropping it.
                        pushLog("[..] agent_steer: run already finished "
                                "(steering not delivered — send again to continue)");
                    } else {
                        pushLog("[..] agent_steer ok (delivered at next step)");
                    }
                }
            }
        }).detach();
    }
};
static AgentControlChannel g_agentCtl;

// GL-thread drain of the control channel's queued log lines — the only safe place to touch
// AppState::log from this subsystem. Cheap no-op when nothing queued; call once per composer
// frame (covers both the docked Copilot and the AI-tab workspace host, since both draw the
// composer every visible frame).
static void agentControlDrainLogs(AppState& st) {
    for (;;) {
        std::string msg;
        {
            std::lock_guard<std::mutex> lk(g_agentCtl.mtx);
            if (g_agentCtl.pendingLogs.empty()) return;
            msg = std::move(g_agentCtl.pendingLogs.front());
            g_agentCtl.pendingLogs.pop_front();
        }
        st.addLog(msg);
    }
}

static void agentControlDispatch(AppState& st, const std::string& runId, const std::string& steer) {
    g_agentCtl.st.store(&st, std::memory_order_release);
    {
        std::lock_guard<std::mutex> lk(g_agentCtl.mtx);
        g_agentCtl.q.push_back({runId, steer});
    }
    g_agentCtl.cv.notify_one();
}

// Stop the in-flight run: fire agent_cancel (hard cooperative cancel — aborts in-flight HTTP
// via curl xfer + stage boundaries), then soft-finalize the UI so the composer reverts to send
// immediately. The job drain discards the eventual cancel_result.
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

static void requestStopRun(AppState& st) {
    // Stop only ever acts on the VIEWED conversation's run — a background run in another
    // conversation keeps streaming (that is the point of per-conversation runs).
    auto* run = st.runIfExists(st.activeConvId());
    if (!run) return;
    // Capture run_id before we clear flags.
    std::string runId;
    {
        std::lock_guard<std::mutex> lk(run->mtx);
        runId = run->runId;
    }
    // Fire agent_cancel on the persistent control channel so we never block the GL thread
    // behind the worker mutex / daemon socket (callAgentCancel bypasses callMtx_ / uses a
    // side socket) — and never create a thread per click (WASM pthread-pool exhaustion
    // blocked thread creation on the main thread = frozen page).
    // Empty run_id still dispatches when THIS run is live: core agent_cancel with an empty id
    // cancels ALL live runs (covers Stop before run_started lands, which previously left hostd
    // 1 in-flight). That is exactly why the gate must be this run's own state — a global
    // "something is running" flag would fire an empty cancel that kills other conversations.
    if (st.workerStarted && (run->running.load() || !runId.empty())) {
        try {
            agentControlDispatch(st, runId, /*steer=*/"");
        } catch (...) {
            st.addLog("[..] agent_cancel dispatch failed");
        }
    }
    // Only a job-based run produces a result the drain must discard. Async ask/sim runs land via
    // their own callbacks, which look for a *running* message — clearing it below makes them no-op.
    if (run->running.load() || run->ready.load()) run->stopRequested = true;
    st.livePhase.clear();
    st.agentV2Status.clear();
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
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
    for (auto& m : st.chat)
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

// The composer buffer, unwrapped and trimmed — the ONE definition of "is there a message".
// The rewrapper toggles spaces<->newlines as the user types, so a buffer holding nothing but
// whitespace still satisfied `prompt[0] != 0` and Enter shipped an empty user bubble to the
// model (live 2026-07-28: three blank "You" turns in a row). Every send / queue / steer path
// gates on this instead, and it doubles as the soft-break flattener on the way out — SOFT
// only: a break the user typed with Alt/Shift+Enter is part of their message and survives
// into the prompt, while emptiness is still judged on the text around those breaks.
static std::string composerText(const AppState& st) {
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
    // toolbar's New chip below the box. Short (one-row) inputs still center vertically.
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
    const float round = ui::metrics::rounding(7.0f);
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
        const ImVec4 accC(pal.accent[0], pal.accent[1], pal.accent[2], 1);
        const ImVec4 fill = canStart
            ? (hov ? ImVec4(pal.accentDim[0], pal.accentDim[1], pal.accentDim[2], 1) : accC)
            : ImVec4(pal.button[0], pal.button[1], pal.button[2], 1);
        dl->AddRectFilled(bMin, bMax, ImGui::ColorConvertFloat4ToU32(fill), round);
        // Enter/return-key glyph: stem down from the top-right into a horizontal run ending in a
        // left-pointing arrowhead. Ink pairs with the button's own fill (same rule as before).
        const ImU32 ink = canStart
            ? ImGui::ColorConvertFloat4ToU32(ui::maxContrastTextOn(accC))
            : ui::Palette::toU32(pal.textDim);
        const float g  = sz * 0.24f;
        const float by = c.y + g * 0.55f;              // the horizontal run's y
        const float th = ui::metrics::stroke(1.6f);
        dl->PathClear();
        dl->PathLineTo(ImVec2(c.x + g, c.y - g));      // stem top (right side)
        dl->PathLineTo(ImVec2(c.x + g, by));           // down
        dl->PathLineTo(ImVec2(c.x - g, by));           // across to the left
        dl->PathStroke(ink, 0, th);
        const float a = g * 0.62f;                     // arrowhead, pointing LEFT
        dl->AddLine(ImVec2(c.x - g, by), ImVec2(c.x - g + a, by - a), ink, th);
        dl->AddLine(ImVec2(c.x - g, by), ImVec2(c.x - g + a, by + a), ink, th);
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


    const bool hasDoc = st.docMirror.hasDoc();

    // Keep mode valid + sync settings.copilotMode (persisted).
    // Edit requires a doc — fall back to Agent when none is open.
    if (st.aiInteraction == (int)CopilotMode::Edit && !hasDoc) {
        st.aiInteraction = (int)CopilotMode::Agent;
        st.settings.copilotMode = (int)CopilotMode::Agent;
    }
    if (st.aiInteraction < 0 || st.aiInteraction > 5) st.aiInteraction = 0;
    // agentV2 flag: any mutating orchestrator mode (Agent/Plan/Swarm/Edit). Ask is one-shot.
    st.agentV2 = (st.aiInteraction != (int)CopilotMode::Ask);
    // One-shot: seed live mode from persisted settings after loadSettings.
    static bool s_modeSeeded = false;
    if (!s_modeSeeded) {
        s_modeSeeded = true;
        int cm = st.settings.copilotMode;
        if (cm >= 0 && cm <= 4) st.aiInteraction = cm;
    }

    // Esc: close slash autocomplete first, then staged edit restore, else disarm context pick.
    if (ImGui::IsKeyPressed(ImGuiKey_Escape, false)) {
        if (s_slash.open) {
            // Latch the dismissal on the current query — drawSlashAutocomplete
            // recomputes `open` from the buffer each frame and would reopen
            // instantly (editing the query re-arms it there).
            s_slash.dismissed = true;
            s_slash.dismissedFilter = s_slash.filter;
            s_slash.open = false;
            s_slash.matches.clear();
            g_slashMenuActive = false;
        } else if (s_edit.active) {
            cancelEditStage(st);
        } else if (st.contextPickActive) {
            st.contextPickActive = false;
        }
    }

    // Drain the message queue: once the assistant is free, auto-send the next queued message
    // (Claude-Code-style). Done before computing `active` so the new turn shows as running now.
    // Also require the viewed conversation's run to be idle: after Stop, aiTurnActive is false
    // (stopRequested terminal) but the cancelled run may still be unwinding — don't launch a
    // queued turn on top of it. Another conversation's run is irrelevant here.
    if (!aiTurnActive(st) && !st.convRunning(st.activeConvId()) && !st.queuedPrompts.empty() &&
        canRun) {
        std::string next = st.queuedPrompts.front();
        st.queuedPrompts.erase(st.queuedPrompts.begin());
        // Queued sends never commit a staged edit (stage should already be cleared).
        submitTurn(st, next);
    }

    const bool active = aiTurnActive(st);
    bool canStart = canRun && !composerText(st).empty() && !active;  // start now only when idle
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

    // ---- chips above the input: file attachments (M23) + context refs (Wave4) ----
    if (!st.pendingAttachments.empty() || !st.pendingContextRefs.empty()) {
        const ui::AdaptiveSurface& asurf = chatSurf();
        const float sc = ui::uiScale;
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::rounding(6.0f));
        ImGui::PushStyleColor(ImGuiCol_Text, asurf.textPrimary);
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(asurf.textPrimary.x, asurf.textPrimary.y, asurf.textPrimary.z, 0.08f));
        const float maxX = ImGui::GetContentRegionMax().x;
        bool anyDrawn = false;

        auto drawChipBorder = [&]() {
            ImGui::GetWindowDrawList()->AddRect(
                ImGui::GetItemRectMin(), ImGui::GetItemRectMax(),
                ImGui::ColorConvertFloat4ToU32(ImVec4(asurf.textPrimary.x, asurf.textPrimary.y,
                                                      asurf.textPrimary.z, 0.35f)),
                ui::metrics::rounding(6.0f), 0, ui::metrics::stroke(1.0f));
        };
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
        const float chipPadX = 6 * sc, chipPadY = 3 * sc;

        for (size_t ai = 0; ai < st.pendingAttachments.size(); ++ai) {
            ImGui::PushID((int)ai + 12000);
            const std::string apath = st.pendingAttachments[ai];  // copy: the vector can erase
            const bool  isImg   = ui::isImageAttachment(apath);
            std::string name    = attachmentBaseName(apath);
            if (name.size() > 30) name = name.substr(0, 29) + "\xE2\x80\xA6";
            const float lineH   = ImGui::GetTextLineHeight();
            const float thumbSz = lineH * 2.0f;            // about two text lines tall
            const float rowH    = isImg ? thumbSz : lineH;
            const float nameW   = ImGui::CalcTextSize(name.c_str()).x;
            const float xBtnW   = ImGui::CalcTextSize("\xC3\x97").x +
                                  ImGui::GetStyle().FramePadding.x * 2.0f;
            const float chipW   = chipPadX * 2.0f + (isImg ? thumbSz + 4 * sc : 0.0f) +
                                  nameW + 6 * sc + xBtnW;
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
            ImGui::InvisibleButton("##attname", ImVec2(nameW, rowH));
            const bool nameClicked = ImGui::IsItemClicked();
            const bool nameHovered = ImGui::IsItemHovered();
            const ImVec2 nameMin = ImGui::GetItemRectMin();
            chipDl->AddText(ImVec2(nameMin.x, nameMin.y + (rowH - lineH) * 0.5f),
                            chipInkCol, name.c_str());
            if (isImg) {
                if (nameClicked) view = true;
                if (nameHovered) {
                    ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
                    ImGui::SetTooltip("%s\nClick to view.", apath.c_str());
                }
            } else if (nameHovered) {
                // Deliberately inert: detaching is the × alone.
                ImGui::SetTooltip("%s\nAttached \xE2\x80\x94 rides your next message.",
                                  apath.c_str());
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
                st.pendingAttachments.erase(st.pendingAttachments.begin() + ai);
                savePromptDraft(st, /*force=*/true);
                ImGui::PopID();
                break;
            }
            ImGui::PopID();
        }
        chipSplit.Merge(chipDl);

        // Context-ref chips: kind letter + label + ×
        for (size_t ci = 0; ci < st.pendingContextRefs.size(); ++ci) {
            ImGui::PushID((int)ci + 14000);
            const auto& ref = st.pendingContextRefs[ci];
            char kindLetter = '?';
            if (!ref.kind.empty()) {
                if (ref.kind == "sketch") kindLetter = 'S';
                else if (ref.kind == "feature") kindLetter = 'F';
                else if (ref.kind == "component") kindLetter = 'C';
                else if (ref.kind == "body") kindLetter = 'B';
                else if (ref.kind == "face") kindLetter = 'A';
                else if (ref.kind == "edge") kindLetter = 'E';
                else kindLetter = (char)std::toupper((unsigned char)ref.kind[0]);
            }
            std::string name = ref.label.empty() ? (ref.kind.empty() ? "ref" : ref.kind) : ref.label;
            if (name.size() > 28) name = name.substr(0, 27) + "\xE2\x80\xA6";
            const std::string lbl = std::string(1, kindLetter) + "  " + name + "  \xC3\x97";
            const float chipW = ImGui::CalcTextSize(lbl.c_str()).x + 16 * sc;
            placeChip(chipW, 4 * sc);
            const bool removed = ImGui::SmallButton(lbl.c_str());
            drawChipBorder();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("%s · %s\nContext reference — rides your next message. Click to remove.",
                                  ref.kind.c_str(),
                                  ref.label.empty() ? ref.id.c_str() : ref.label.c_str());
            if (removed) {
                st.pendingContextRefs.erase(st.pendingContextRefs.begin() + ci);
                savePromptDraft(st, /*force=*/true);
                ImGui::PopID();
                break;
            }
            ImGui::PopID();
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
    drawComposerPrelude(st, busy);

    const char* placeholder =
        st.aiInteraction == (int)CopilotMode::Ask   ? "Ask about this part\xE2\x80\xA6"
      : st.aiInteraction == (int)CopilotMode::Edit  ? "Describe a change to this part\xE2\x80\xA6"
      : st.aiInteraction == (int)CopilotMode::Plan  ? "Describe what to plan\xE2\x80\xA6"
      : st.aiInteraction == (int)CopilotMode::Swarm ? "Describe a multi-agent mission\xE2\x80\xA6"
                                                    : "Describe what to build\xE2\x80\xA6";

    ImDrawList* composerDl = ImGui::GetWindowDrawList();
    ImDrawListSplitter composerSplit;
    const ImVec2 composerMin = ImGui::GetCursorScreenPos();
    const float composerW = ImGui::GetContentRegionAvail().x;
    composerSplit.Split(composerDl, 2);
    composerSplit.SetCurrentChannel(composerDl, 1);

    const ui::AdaptiveSurface& surf = chatSurf();
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 0.0f));
    ImGui::PushStyleColor(ImGuiCol_FrameBg, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::PushStyleColor(ImGuiCol_TextDisabled, surf.textTertiary);
    ImGui::PushStyleColor(ImGuiCol_Border, IM_COL32(0, 0, 0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 0.0f);
    // Minimal top pad so the Design dropdown + context meter sit close to the input.
    ImGui::Dummy(ImVec2(0, ui::metrics::U(0.25f)));
    // Reserve the in-box send button's footprint on the input's right so text never runs under it.
    const float sendSz    = ImGui::GetFrameHeight();
    const float sendInset = 6.0f * ui::uiScale;
    // After a send the backing buffer is cleared while the InputText is still ACTIVE — ImGui
    // keeps its own edit state for the active widget and ignores the external clear, so the
    // prompt stayed in the box. Bump a generation into the widget id on send so the widget
    // restarts from the (empty) buffer; refocus it so the user can keep typing.
    static int  s_composerGen = 0;
    static bool s_refocusComposer = false;
    if (s_refocusComposer) {
        ImGui::SetKeyboardFocusHere();
        s_refocusComposer = false;
    }
    // Prime slash-menu skill catalog once the worker is up (cheap TTL'd RPC).
    if (st.workerStarted) pollComposerSkills(st, /*force=*/false);

    // Slash query opens before the InputText so Up/Down history callbacks fire this frame.
    {
        std::string q;
        g_slashMenuActive = parseSlashQuery(st.prompt.data(), q);
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
    static bool s_composerInputActive = false;
    {
        const ImGuiIO& pio = ImGui::GetIO();
        if (s_composerInputActive && (pio.KeyCtrl || pio.KeySuper) &&
            ImGui::IsKeyPressed(ImGuiKey_V, false) && ui::clipboardHasImage()) {
            std::string imgPath, imgErr;
            if (ui::clipboardImageToPng(imgPath, imgErr)) {
                if (std::find(st.pendingAttachments.begin(), st.pendingAttachments.end(),
                              imgPath) == st.pendingAttachments.end()) {
                    st.pendingAttachments.push_back(imgPath);
                    savePromptDraft(st, /*force=*/true);
                }
                ImGui::SetKeyOwner(ImGuiKey_V, ImGui::GetID("##aipromptimagepaste"));
            } else {
                // Not swallowed: the ordinary text paste still runs, so nothing is lost.
                st.toast(ui::ToastLevel::Warn, imgErr);
            }
        }
    }

    char inputId[32];
    std::snprintf(inputId, sizeof(inputId), "##aiprompt%d", s_composerGen);
    bool submitInput = drawChatInput(inputId, st.prompt, placeholder,
                                     ui::metrics::copilotInputMaxLines(),
                                     sendSz + sendInset * 2.0f);
    s_composerInputActive = ImGui::IsItemActive();
    const ImVec2 inputMin = ImGui::GetItemRectMin();
    const ImVec2 inputMax = ImGui::GetItemRectMax();
    // Crash-safe draft: debounce-write whatever is in the box.
    savePromptDraft(st, /*force=*/false);
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(6);
    // Send/stop rides INSIDE the input box, anchored bottom-right of the composer glass.
    drawComposerSendStop(st, inputMin, inputMax,
                         composerMin.x + composerW, canRun, canStart, active, submit, stopReq);

    // Slash autocomplete drop-up (commands + skills). Accept steals Enter from submit.
    bool slashAteEnter = false;
    if (drawSlashAutocomplete(st, inputMin, inputMax, slashAteEnter)) {
        ++s_composerGen;          // active InputText ignores external buffer writes
        s_refocusComposer = true;
        submitInput = false;
        submit = false;
    } else if (slashAteEnter || (s_slash.open && submitInput)) {
        // Menu open with matches: Enter must not send the half-typed `/foo` token.
        submitInput = false;
        submit = false;
    }

    // No usage/provider id strip above the toolbar — model lives in the toolbar chip.
    drawComposerToolbar(st, busy, true);
    ImGui::PopStyleVar();

    const ImVec2 composerMax(composerMin.x + composerW, ImGui::GetItemRectMax().y);
    composerSplit.SetCurrentChannel(composerDl, 0);
    ui::glassPanel(composerDl, ImRect(composerMin, composerMax), ui::GlassLevel::Thin,
                  8.0f /*logical dp — glassPanel scales internally*/, surf);
    composerSplit.Merge(composerDl);

    // ---- stop / submit / queue ----
    if (stopReq) requestStopRun(st);
    auto clearComposerBox = [&]() {
        clearPromptDraft(st);
        st.prompt[0] = 0;
        ++s_composerGen;          // fresh InputText state (active widget ignores external clears)
        s_refocusComposer = true; // keep typing without re-clicking
    };
    if (submit || submitInput) {
        const std::string text = composerText(st);   // flattened + trimmed; empty = no message
        if (active) {
            // A turn is running — STEER it mid-run when the engine knows the run_id
            // (agent_steer: text is folded into the live turn at its next step). Fall
            // back to next-turn queueing only when no run_id is available (e.g. the
            // run hasn't announced itself yet). Never both — that would double-send.
            if (!text.empty()) {
                std::string runId;
                {
                    auto steerRun = st.activeRun();
                    std::lock_guard<std::mutex> lk(steerRun->mtx);
                    runId = steerRun->runId;
                }
                if (!runId.empty() && st.workerStarted) {
                    agentControlDispatch(st, runId, text);
                    // Record the steering text in the visible conversation (it otherwise
                    // only reaches the engine + debug log and vanishes from the composer).
                    ChatMsg u; u.fromUser = true; u.text = text;
                    u.copilotMode = st.aiInteraction;
                    u.liveStatus = "steering";  // subtle caption under the bubble
                    st.chat.push_back(std::move(u));
                    appendChatRawMessage(st, st.chat.back());
                } else {
                    // Auto-sends when the assistant frees up (drain at the top of drawComposer).
                    st.queuedPrompts.emplace_back(text);
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
            // Stage the rollback only on send (Esc cancelled without this).
            if (s_edit.active) commitEditStageOnSend(st);
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
