// Copilot composer (T-RF6 split of legacy_chat.cpp): the auto-growing wrapping chat input,
// file attachments + context refs, design/mission run launch, the session slash commands
// (/compact /shake /handoff /btw /prewalk), the submitTurn send path, and the composer chrome
// (runtime banner, Agent/Plan/Swarm/Edit/Ask prelude + context/provider-limit gauge rings,
// graduated segmented toolbar, in-box send/stop).

#include "copilot_internal.h"
#include "ui/provider_usage.h"   // THE subscription-usage poller behind the prelude's limit rings
#include "ui/provider_logos.h"   // brand mark for the model chip (same art as Settings->Providers)

// ---- auto-growing, wrapping chat input -------------------------------------------------------
// ImGui's InputTextMultiline does NOT word-wrap (it renders with wrap_width=0, so long lines
// extend past the box and get clipped — "run off the screen"). So we maintain the wrapping
// ourselves: rewrapInPlace toggles spaces<->newlines so every visual line fits within wrapW.
// It is SAME-LENGTH (only swaps ' ' <-> '\n'), so cursor indices in the active widget stay
// valid. The box then grows by counting the newlines. Enter submits; Shift+Enter inserts a
// soft break that the next rewrap reflows (a prompt rarely needs hard line breaks).
bool rewrapInPlace(char* buf, int len, float wrapW) {
    if (len <= 0 || wrapW <= 1.0f) return false;
    bool changed = false;
    // 1) flatten every existing newline back to a space — an idempotent source to re-wrap from
    //    (so widening the box reflows the text instead of leaving stale breaks).
    for (int i = 0; i < len; ++i)
        if (buf[i] == '\n') { buf[i] = ' '; changed = true; }
    // 2) greedy word-wrap: break at the last space that keeps the running line within wrapW.
    int lineStart = 0, lastSpace = -1;
    float lineW = 0.0f;
    for (int i = 0; i < len; ++i) {
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
namespace {
struct ChatInputCb { float wrapW = 0.0f; };
}  // namespace


namespace {
int chatInputEditCallback(ImGuiInputTextCallbackData* data) {
    if (data->EventFlag == ImGuiInputTextFlags_CallbackEdit) {
        auto* cb = static_cast<ChatInputCb*>(data->UserData);
        if (rewrapInPlace(data->Buf, data->BufTextLen, cb->wrapW))
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
    if (!s_activeLast[wid])
        rewrapInPlace(buf.data(), (int)std::strlen(buf.data()), wrapW);

    int rows = 1;
    for (const char* p = buf.data(); *p; ++p) if (*p == '\n') ++rows;
    if (rows > maxLines) rows = maxLines;
    const float h = lineH * (float)rows + style.FramePadding.y * 2.0f;

    ChatInputCb cb;
    cb.wrapW = wrapW;
    ImGui::InputTextMultiline(id, buf.data(), buf.size(),
        ImVec2(rightReserve > 0.0f ? -rightReserve : -1.0f, h),
        ImGuiInputTextFlags_CallbackEdit, chatInputEditCallback, &cb);
    s_activeLast[wid] = ImGui::IsItemActive();
    // Enter sends, Shift+Enter does not. Detect the KEY (not a trailing '\n') so a newline the
    // rewrap just inserted can never be mistaken for a submit.
    bool submit = ImGui::IsItemFocused() && !ImGui::GetIO().KeyShift &&
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
// PROV_LOCAL / PROV_CLOUDCOMPAT resolve as "openai" descriptors — the base_url (persisted to
// config.json by saveSettings immediately before every send) discriminates local vs cloud.
static const char* providerWireName(int provider) {
    switch (provider) {
        case ui::PROV_ANTHROPIC:   return "anthropic";
        case ui::PROV_LOCAL:       return "openai";  // base_url discriminates (loopback)
        case ui::PROV_OPENAI:
        case ui::PROV_CLOUDCOMPAT: return "openai";  // moonshot override happens in agentProviderOverride
        case ui::PROV_LLAMACPP:    return "llamacpp";
        case ui::PROV_GEMINI:      return "gemini";
        case ui::PROV_XAI:         return "xai";  // saveSettings rewrites to xai-sub when no key
        case ui::PROV_CURSOR:      return "cursor";
        case ui::PROV_KIMISUB:     return "kimi-sub";
        // ZenMux — OMP-bridge provider id, routed through the bun bridge (core/agent/providers.cpp
        // is_omp_bridge_provider) using its own auth db, not a config.json key. Default provider.
        case ui::PROV_ZENMUX:      return "zenmux";
    }
    return "";
}

// The explicit provider/model/effort override the GUI now attaches to EVERY agent-turn RPC
// (agent_run_v2 / agent_ask_v2 / agent_clarify_v1 / agent_mission_v1). Without it the core
// resolves from ambient config.json + process env, so an CHUDWARE_PROVIDER env var (or a config
// edited behind the GUI's back) silently beat the picker (tracker O1). saveSettings still runs
// before each send so descriptor details (base_url / api_key / model_path) match the picker;
// these fields pin name + model + effort explicitly on top of that.
nlohmann::json agentProviderOverride(const Settings& s) {
    nlohmann::json j = nlohmann::json::object();
    const char* name = providerWireName(s.provider);
    // Kimi presets ride PROV_CLOUDCOMPAT with a moonshot.ai base_url. Pin the real
    // provider name so resolve_provider lands on the moonshot descriptor (kimi-k3
    // alias normalize, 1M context window, reasoning_effort=max) instead of the
    // generic openai one. saveSettings already writes provider=moonshot +
    // api_key=moonshotKey to config.json in this case, so the key resolves.
    std::string wire = name ? name : "";
    if (s.provider == ui::PROV_CLOUDCOMPAT &&
        std::string(s.baseUrl).find("moonshot.ai") != std::string::npos)
        wire = "moonshot";
    if (!wire.empty()) j["provider"] = wire;
    // llamacpp's Settings::model holds the gguf FILE PATH (the wire model stays "local-gguf");
    // don't clobber the descriptor's model with a filesystem path.
    if (s.provider != ui::PROV_LLAMACPP && s.model[0]) j["model"] = s.model;
    if (s.reasoningEffort[0]) j["reasoning_effort"] = s.reasoningEffort;
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
// and (b) the structured "attachments" array carried in the RPC payload. HONESTY: the core's
// agent handlers currently pass only the request TEXT to the model, so the inline injection here
// is what actually delivers attachment content today; the structured field rides along (clearly
// named) for handler-side consumption later.
static nlohmann::json consumeAttachments(AppState& st, std::string& requestText) {
    nlohmann::json atts = nlohmann::json::array();
    if (st.pendingAttachments.empty()) return atts;
    for (const std::string& path : st.pendingAttachments) {
        nlohmann::json a = {{"path", path}, {"name", attachmentBaseName(path)}};
        std::string content;
        if (readAttachmentText(path, content)) {
            a["inline"] = true;
            requestText += "\n\n[Attached file: " + attachmentBaseName(path) + " (" + path +
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
    if (st.activityPanel)
        st.activityPanel->clearLive();
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
    if (st.activityPanel)
        st.activityPanel->clearLive();
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
    persistChatHistory(st);
    st.toast(ui::ToastLevel::Info, text.size() > 80 ? text.substr(0, 77) + "..." : text);
}

static void ovSetAgentBool(const char* key, bool v) {
    try {
        nlohmann::json j = nlohmann::json::object();
        {
            std::ifstream in(configPath());
            if (in) {
                nlohmann::json old;
                in >> old;
                if (old.is_object()) j = std::move(old);
            }
        }
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        j["agent"][key] = v;
        const std::string path = configPath();
        const std::string tmp = path + ".tmp";
        std::ofstream(tmp) << j.dump(2);
        std::filesystem::rename(tmp, path);
    } catch (...) {}
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
            if (r.result.contains("kept_messages") && r.result["kept_messages"].is_array())
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
                 "/handoff [focus]  /btw <q>  /prewalk");
        return true;
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
    // Slash commands: /compact /shake /handoff /btw /prewalk — pure host+RPC, no agent turn.
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
            u.text += "\n[attached:";
            for (const auto& a : attachments) u.text += " " + a.value("name", std::string());
            u.text += "]";
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
    }
    {
        ChatMsg a;
        a.running = true;
        a.copilotMode = st.aiInteraction;  // stamp; auto-switch may update to Agent
        if (st.settings.model[0]) a.model = st.settings.model;
        // Instant classification chrome — don't wait for the first drained event frame.
        a.liveStatus = "Routing request\xe2\x80\xa6";
        st.chat.push_back(a);
    }
    st.designStarted = true;   // conversation is now active (kept for shot scenarios)

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

    // MISSION intent: Agent mode only.
    auto looksLikeMission = [&]() -> bool {
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
        if (st.activityPanel)
            st.activityPanel->clearLive();
        nlohmann::json extra = provOverride;
        for (auto it = turnExtras.begin(); it != turnExtras.end(); ++it)
            extra[it.key()] = it.value();
        nlohmann::json opts = {{"mode", "edit"}, {"advisor", true},
                               {"corporate_mode", st.settings.corporateMode},
                               {"scope", buildEditScope()}};
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
                if (st.activityPanel) st.activityPanel->clearLive();
                nlohmann::json extra = provOverride;
                for (auto it = turnExtras.begin(); it != turnExtras.end(); ++it)
                    extra[it.key()] = it.value();
                nlohmann::json opts = {{"mode", "agent"}, {"advisor", true},
                                       {"corporate_mode", st.settings.corporateMode},
                                       {"scope", buildEditScope()}};
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
            {"budget_usd", st.settings.companyBudgetUsd},
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
        if (st.activityPanel)
            st.activityPanel->clearLive();
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
            if (st.activityPanel)
                st.activityPanel->clearLive();
            nlohmann::json extra = provOverride;
            for (auto it = turnExtras.begin(); it != turnExtras.end(); ++it)
                extra[it.key()] = it.value();
            nlohmann::json opts = {{"mode", "agent"}, {"advisor", true},
                                   {"corporate_mode", st.settings.corporateMode},
                                   {"scope", buildEditScope()}};
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
    // Keep in lockstep with ui::ProviderIndex (Anthropic..ZenMux).
    static const char* kProvNames[] = {
        "Anthropic", "OpenAI", "Local", "llama.cpp", "Gemini", "Cloud API", "xAI", "Cursor",
        "Kimi Code", "ZenMux"
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

// Context-window gauge for the composer prelude row: a fraction ring + "N/M" readout (compact
// "ctx N/M" with no ring when the pane is too narrow). Drawn at the current cursor; advances it.
// Moved here from the footer chips, where it was pinned to the pane bottom against the status bar.
static void drawContextMeter(AppState& st, float roomForMeter) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = chatSurf();
    const int   win  = effectiveContextWindow(st.settings);
    const int   used = st.lastContextTokens;
    const float frac = win > 0 ? std::min(1.0f, (float)used / (float)win) : 0.0f;
    const float gap = ui::metrics::px(5), ringAdv = gaugeRingAdvance();
    const std::string full = fmtTokens(used) + "/" + fmtTokens(win);
    const std::string lean = "ctx " + full;   // standalone (no ring) keeps a "ctx" tag so it reads
    const float fh = ImGui::GetFrameHeight();
    // Ring + readout only when there is room for both; uses the SAME roomForMeter the caller passed
    // to measureContextMeter() so the width it reserved and the layout drawn here agree exactly.
    const float fullTw = ImGui::CalcTextSize(full.c_str()).x;
    const bool  showRing = roomForMeter >= ringAdv + gap + fullTw;
    ImGui::BeginGroup();   // group so hovering the RING (not just the digits) raises the tooltip
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    if (showRing) {
        const ImVec2 p = ImGui::GetCursorScreenPos();
        // Same thresholds usageStatusColor() uses, so every gauge agrees on when amber turns red.
        const float* role = frac < 0.5f  ? pal.statusOk
                          : frac < 0.85f ? pal.statusWarn
                                         : pal.statusErr;
        ui::drawFractionRing(ImGui::GetWindowDrawList(),
                             ImVec2(p.x + ringAdv * 0.5f, p.y + fh * 0.5f),
                             gaugeRingRadius(), gaugeRingThickness(), frac,
                             gaugeTrackColor(surf),
                             ImGui::ColorConvertFloat4ToU32(semanticOnGlass(role, surf)));
        ImGui::Dummy(ImVec2(ringAdv, fh));
        ImGui::SameLine(0, gap);
    }
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(showRing ? full.c_str() : lean.c_str());
    ImGui::PopStyleColor();
    ImGui::EndGroup();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Context window usage — the last model call's input tokens vs the "
                          "active model's window (%d / %d).\nSet the window in AI settings if "
                          "your local model differs.", used, win);
}

// Width drawContextMeter() will occupy, so the caller can right-align it on its line above the
// composer. Mirrors the showRing decision in drawContextMeter() so reserved width == drawn width.
static float measureContextMeter(AppState& st, float roomForMeter) {
    const float gap = ui::metrics::px(5), ringAdv = gaugeRingAdvance();
    const std::string full = fmtTokens(st.lastContextTokens) + "/" +
                             fmtTokens(effectiveContextWindow(st.settings));
    const float fullTw = ImGui::CalcTextSize(full.c_str()).x;
    if (roomForMeter >= ringAdv + gap + fullTw) return ringAdv + gap + fullTw;
    return ImGui::CalcTextSize(("ctx " + full).c_str()).x;
}

// Provider rate-limit gauges for the prelude row: one ring per usage window the ACTIVE
// subscription account reports ("5H (o) 7D (o)"), sitting immediately left of the context ring.
// Every layer is opt-out by ABSENCE, which is what keeps inapplicable elements hidden: key-based
// and local providers have no subscription id, an account may report no windows at all, and a
// window without a fraction is status-only — each of those draws NOTHING rather than a
// misleading empty ring. `draw == false` measures only (emits no ImGui items) so the caller can
// right-align the usage cluster and the context gauge as one block.
//
// `budget` is the width the rings may consume. Indicators are dropped from the END until the
// set fits, rather than all-or-nothing: a 340dp column only clears ~90dp next to the context
// gauge, so an all-or-nothing rule would hide Claude's 5H+7D pair on the DEFAULT layout. Both
// passes are handed the same budget, so measure and draw always pick the same set.
static float usageRingCluster(AppState& st, float budget, bool draw) {
    const char* sub = ui::subscriptionProviderForCad(st.settings.provider);
    if (!sub) return 0.0f;
    const std::vector<ui::UsageWindow>* wins = ui::activeAccountUsage(sub);
    if (!wins || wins->empty()) return 0.0f;

    const ui::AdaptiveSurface& surf = chatSurf();
    const float capGap  = ui::metrics::px(3);   // caption -> its ring
    const float indGap  = ui::metrics::px(7);   // between indicators
    const float ringAdv = gaugeRingAdvance();
    const float fh = ImGui::GetFrameHeight();
    float total = 0.0f;
    int   shown = 0;
    for (const ui::UsageWindow& w : *wins) {
        if (shown >= 3) break;          // three rings is all even a wide prelude row can carry
        if (!w.hasFraction) continue;   // status-only window: never render it as 0%
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


// Prelude row above the prompt: Agent/Plan/Swarm/Edit/Ask dropdown (left) + busy forging
// glyph (inline, only while a run is active) + context meter (right).
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

    // Busy forging glyph — centered in the gap between the mode dropdown and the right-hand
    // gauge cluster. Hidden the instant Stop is pressed (aiTurnActive honors stopRequested)
    // so the animation never keeps playing after a stop.
    // Note: `busy` is job.running and can lag past Stop — always gate on the run's stopRequested.
    // Use `tmx` (mode chip screen rect captured above) — GetItemRectMax after the
    // popup is no longer the mode button.
    {
        auto* forgeRun = st.runIfExists(st.activeConvId());
        const bool forging = !(forgeRun && forgeRun->stopRequested.load()) &&
                             (busy || activeConversationRunning(st));
        const float meterRoomFull = ImGui::GetContentRegionAvail().x;
        const float meterW = measureContextMeter(st, meterRoomFull);
        const float rowRightScreen = ImGui::GetWindowPos().x + ImGui::GetContentRegionMax().x;
        // Provider-limit rings ride LEFT of the context gauge as one right-aligned cluster, and
        // give way first when the column narrows: the context gauge always wins, and the cluster
        // may never reach back over the mode chip (px(6) keeps a visible break between them).
        const float clusterGap = ui::metrics::px(10);
        const float usageBudget = (rowRightScreen - tmx.x) - clusterGap - meterW -
                                  ui::metrics::px(6);
        const float usageW = usageRingCluster(st, usageBudget, /*draw=*/false);
        const float clusterW = usageW > 0.0f ? usageW + clusterGap + meterW : meterW;
        const float meterLeftScreen = rowRightScreen - clusterW;
        if (forging) {
            const float midScreen = (tmx.x + meterLeftScreen) * 0.5f;
            float hammerScreenX = midScreen - segH * 0.5f;
            // Keep clear of the mode chip; fall back if the row is narrow.
            if (hammerScreenX < tmx.x + 4.0f * sc)
                hammerScreenX = tmx.x + 8.0f * sc;
            if (hammerScreenX + segH > meterLeftScreen - 4.0f * sc)
                hammerScreenX = ImMax(tmx.x + 4.0f * sc, meterLeftScreen - segH - 4.0f * sc);
            ImGui::SameLine(0, 0.0f);
            ImGui::SetCursorScreenPos(ImVec2(hammerScreenX, tmn.y));
            drawForgingAnimation(segH);  // match prelude chip height
            if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                ImGui::SetTooltip("AI is working\xe2\x80\xa6");
        }
        // Gauge cluster on the SAME row, right-aligned as one block.
        ImGui::SameLine(0, 0.0f);
        const float clusterCursorX = ImGui::GetContentRegionMax().x - clusterW;
        if (clusterCursorX > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(clusterCursorX);
        if (usageW > 0.0f) {
            usageRingCluster(st, usageBudget, /*draw=*/true);
            ImGui::SameLine(0, clusterGap);
        }
        drawContextMeter(st, meterRoomFull);
    }


    ImGui::PopStyleColor(8);
    ImGui::PopStyleVar(2);
    // Tight gap to the prompt — prelude sits closer to the input box.
}

// Toolbar row below the prompt: [model | thinking | paperclip | context-cube | New]
// Graduated widths with overlapping rounded corners (connected segmented look).
// Model picker is the largest share; paperclip/cube are icon-only minimums.
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
    int nEff = ui::reasoningEffortOptions(s.provider, s.model, effLabels, effValues, 8);
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

    // Graduated segment widths (left→right: model, thinking, paperclip, cube, New).
    // Icon chips stay near-square; New < thinking < model share the remaining row.
    const int nSeg = 5;
    const float segH = ImGui::GetFrameHeight();
    const float iconSz = segH;
    const float join = -overlap;  // positive join thickness per boundary
    const float totalW = availW + join * (float)(nSeg - 1);
    const float iconW = segH;     // minimal — just an icon
    const float restW = std::max(0.0f, totalW - iconW * 2.0f);
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

    // ---- 1/5 model: provider brand mark + the model's REAL display name (no provider text) ----
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

    // ---- 2/5 thinking effort (level only; bigger than New) ----
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

    // ---- 3/5 paperclip (icon-only, smallest) ----
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

    // ---- 4/5 context-cube (icon-only, smallest) ----
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

    // ---- 5/5 New session (slightly bigger than icon chips) ----
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
// immediately leave the streaming chrome (stop button + forging animation). run->running may
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
            // Finalize any live stream boxes so the forging animation / streaming chrome stops.
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

    // Esc: staged edit restore, else disarm context pick (Wave4).
    if (ImGui::IsKeyPressed(ImGuiKey_Escape, false)) {
        if (s_edit.active) {
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
    bool canStart = canRun && st.prompt[0] && !active;  // start now only when idle
    bool submit   = false;   // send / queue requested this frame
    bool stopReq  = false;   // stop requested this frame

    // ---- queued messages (shown above the composer, each removable) ----
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
        auto placeChip = [&](float chipW) {
            if (anyDrawn) {
                ImGui::SameLine(0, 4 * sc);
                if (ImGui::GetCursorPosX() + chipW > maxX) ImGui::NewLine();
            }
            anyDrawn = true;
        };

        for (size_t ai = 0; ai < st.pendingAttachments.size(); ++ai) {
            ImGui::PushID((int)ai + 12000);
            std::string name = attachmentBaseName(st.pendingAttachments[ai]);
            if (name.size() > 30) name = name.substr(0, 29) + "\xE2\x80\xA6";
            const std::string lbl = name + "  \xC3\x97";   // "name  ×"
            const float chipW = ImGui::CalcTextSize(lbl.c_str()).x + 16 * sc;
            placeChip(chipW);
            const bool removed = ImGui::SmallButton(lbl.c_str());
            drawChipBorder();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("%s\nAttached — rides your next message. Click to remove.",
                                  st.pendingAttachments[ai].c_str());
            if (removed) {
                st.pendingAttachments.erase(st.pendingAttachments.begin() + ai);
                savePromptDraft(st, /*force=*/true);
                ImGui::PopID();
                break;
            }
            ImGui::PopID();
        }

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
            placeChip(chipW);
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
    char inputId[32];
    std::snprintf(inputId, sizeof(inputId), "##aiprompt%d", s_composerGen);
    bool submitInput = drawChatInput(inputId, st.prompt, placeholder,
                                     ui::metrics::copilotInputMaxLines(),
                                     sendSz + sendInset * 2.0f);
    // Crash-safe draft: debounce-write whatever is in the box.
    savePromptDraft(st, /*force=*/false);
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(6);
    // Send/stop rides INSIDE the input box, anchored bottom-right of the composer glass.
    drawComposerSendStop(st, ImGui::GetItemRectMin(), ImGui::GetItemRectMax(),
                         composerMin.x + composerW, canRun, canStart, active, submit, stopReq);

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
    // The composer re-wraps text by toggling spaces<->newlines (rewrapInPlace). Flatten those
    // soft breaks back to spaces on the way OUT so submitted/queued/steered prompts — and the
    // user bubble that echoes them — never carry the composer's visual line breaks.
    auto flattenComposer = [](const char* s) {
        std::string out(s ? s : "");
        for (auto& c : out) if (c == '\n') c = ' ';
        return out;
    };
    auto clearComposerBox = [&]() {
        clearPromptDraft(st);
        st.prompt[0] = 0;
        ++s_composerGen;          // fresh InputText state (active widget ignores external clears)
        s_refocusComposer = true; // keep typing without re-clicking
    };
    if (submit || submitInput) {
        if (active) {
            // A turn is running — STEER it mid-run when the engine knows the run_id
            // (agent_steer: text is folded into the live turn at its next step). Fall
            // back to next-turn queueing only when no run_id is available (e.g. the
            // run hasn't announced itself yet). Never both — that would double-send.
            if (st.prompt[0]) {
                std::string runId;
                {
                    auto steerRun = st.activeRun();
                    std::lock_guard<std::mutex> lk(steerRun->mtx);
                    runId = steerRun->runId;
                }
                const std::string text = flattenComposer(st.prompt.data());
                if (!runId.empty() && st.workerStarted) {
                    agentControlDispatch(st, runId, text);
                    // Record the steering text in the visible conversation (it otherwise
                    // only reaches the engine + debug log and vanishes from the composer).
                    ChatMsg u; u.fromUser = true; u.text = text;
                    u.copilotMode = st.aiInteraction;
                    u.liveStatus = "steering";  // subtle caption under the bubble
                    st.chat.push_back(std::move(u));
                } else {
                    // Auto-sends when the assistant frees up (drain at the top of drawComposer).
                    st.queuedPrompts.emplace_back(text);
                }
                clearComposerBox();
            }
        } else if (canStart) {
            // Stage the rollback only on send (Esc cancelled without this).
            if (s_edit.active) commitEditStageOnSend(st);
            submitTurn(st, flattenComposer(st.prompt.data()));
            clearComposerBox();
        }
    }
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
