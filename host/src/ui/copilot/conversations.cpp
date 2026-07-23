// Copilot conversations + persistence (T-RF6 split of legacy_chat.cpp): conversation tabs
// (ensure/new/switch/close, park/restore around doc switches), chat-history and prompt-draft
// persistence to %APPDATA%/Chudware/*.json (incl. the WASM shared-workspace chat sync), and the
// per-frame chat-triggered OpenFOAM CFD poll. The slash commands that mutate sessions live
// with the send path in composer.cpp.

#include "copilot_internal.h"

#include "ui/ai/ai_workspace.h"  // ui::agentDagToJson / agentDagFromJson — persist the live
                                 // agent-execution DAG with the active session's artifacts.

// Defined in feed.cpp — consumes the routing fields stashed by feedTurnActivity's
// task_route/agent.classification/complexity branch (feedTurnActivity only sees a ChatMsg;
// the GL draw path applies the assessment to the active Conversation here).
bool takePendingScopeAssessment(std::string& domain, std::string& complexity,
                                std::string& structure, std::string& execStyle,
                                std::string& snippet);

// Rail/tab color-code for Conversation::scopeClass (declared in core/app_state.h).
// -1 unassessed -> neutral dim, 0 trivial -> iterBuilt, 1 standard -> accent,
// 2 complex -> amber warn, 3 epic -> red err. RGBA 0..1, alpha 1.
void scopeClassColor(int scopeClass, float out[4]) {
    const float* src = ui::palette.textDim;
    switch (scopeClass) {
        case 0:  src = ui::palette.iterBuilt;  break;
        case 1:  src = ui::palette.accent;     break;
        case 2:  src = ui::palette.statusWarn; break;
        case 3:  src = ui::palette.statusErr;  break;
        default: break;  // -1 (unassessed) + unknown -> neutral
    }
    out[0] = src[0];
    out[1] = src[1];
    out[2] = src[2];
    out[3] = 1.0f;
}

#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
#include <fcntl.h>
#include <unistd.h>
#endif

namespace ui {

// ---- durable atomic file write (crash safety) ----------------------------------------------
// tmp + rename alone is NOT crash-safe: on ext4/xfs the rename metadata can hit the journal
// before the tmp file's DATA does, so a hard crash right after a save could leave a
// zero-length chat_history.json — every session gone. fsync the tmp file before the rename and
// fsync the containing directory after it, so the visible file always has complete contents.
// No-op on Windows/WASM (previous behavior preserved there).
static void fsyncPath(const std::string& p, bool isDir) {
#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
    const int fd = ::open(p.c_str(), isDir ? (O_RDONLY | O_DIRECTORY) : O_RDONLY);
    if (fd < 0) return;
    ::fsync(fd);
    ::close(fd);
#else
    (void)p;
    (void)isDir;
#endif
}

// Write `data` to `path` atomically and durably. Returns false if nothing was written (callers
// treat that as "still dirty" so the next tick retries instead of silently losing the state).
bool atomicWriteFileDurable(const std::string& path, const std::string& data) {
    namespace fs = std::filesystem;
    const std::string tmp = path + ".tmp";
    try {
        {
            std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
            if (!out) return false;
            out.write(data.data(), (std::streamsize)data.size());
            out.flush();
            if (!out) return false;
        }
        fsyncPath(tmp, /*isDir=*/false);
        std::error_code ec;
        fs::rename(tmp, path, ec);
        if (ec) {
            // Windows sometimes needs remove-then-rename; last-resort non-atomic.
            fs::remove(path, ec);
            ec.clear();
            fs::rename(tmp, path, ec);
            if (ec) return false;
        }
        const fs::path parent = fs::path(path).parent_path();
        if (!parent.empty()) fsyncPath(parent.string(), /*isDir=*/true);
        return true;
    } catch (...) {
        return false;
    }
}

static std::string s_chatKeyToErase;
std::string s_chatKeyHold;
#ifdef __EMSCRIPTEN__
// Shared workspace chat (GET/PUT /workspace/chat). IDBFS remains local cache/fallback.
// Fetch callbacks stash results; merge runs on the UI thread via syncChatToDoc.
static bool s_chatShareFetchStarted = false;
static bool s_chatShareHasPending = false;
static nlohmann::json s_chatSharePending = nlohmann::json::object();
static void wasmPutSharedChat(const std::string& key, const nlohmann::json& entry);
static void wasmKickSharedChatFetch();
static void wasmApplySharedChatPending(AppState& st);
#endif

// ============================ conversation tabs + persistence ============================
// Each conversation is its own tab. The ACTIVE conversation's messages live in st.chat; the
// rest park in st.conversations[i].msgs. Persisted per document to chat_history.json.

std::string chatHistoryPath() { return appdataAppDir() + "/chat_history.json"; }
static std::string promptDraftsPath() { return appdataAppDir() + "/prompt_drafts.json"; }

static std::string promptDraftKey(AppState& st) {
    std::string key = st.chatDocKey.empty() ? convDocKey(st) : st.chatDocKey;
    key += "/";
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size())
        key += st.conversations[st.activeConv].id;
    else
        key += "c0";
    return key;
}

static nlohmann::json serializePendingContextRefs(const AppState& st) {
    nlohmann::json refs = nlohmann::json::array();
    for (const auto& r : st.pendingContextRefs) {
        nlohmann::json o = {
            {"kind", r.kind},
            {"id", r.id},
            {"label", r.label},
        };
        // Prefer snake_case on disk; keep docId if set.
        if (!r.docId.empty()) o["doc_id"] = r.docId;
        refs.push_back(std::move(o));
    }
    return refs;
}

// Snapshot the composer (text + attachment paths + context refs) for prompt_drafts.json.
static nlohmann::json promptDraftObject(const AppState& st) {
    nlohmann::json o = nlohmann::json::object();
    const char* p = st.prompt.data() ? st.prompt.data() : "";
    o["text"] = p;
    o["attachments"] = st.pendingAttachments;
    o["context_refs"] = serializePendingContextRefs(st);
    return o;
}

static bool promptDraftIsEmpty(const nlohmann::json& o) {
    if (!o.is_object()) return true;
    const std::string text = o.value("text", std::string());
    if (!text.empty()) return false;
    if (o.contains("attachments") && o["attachments"].is_array() && !o["attachments"].empty())
        return false;
    if (o.contains("context_refs") && o["context_refs"].is_array() && !o["context_refs"].empty())
        return false;
    return true;
}

// Stable fingerprint for debounce (text + paths + ref ids).
static std::string promptDraftFingerprint(const AppState& st) {
    return promptDraftObject(st).dump();
}

static void applyPromptDraftObject(AppState& st, const nlohmann::json& d) {
    std::string text;
    if (d.is_string()) {
        text = d.get<std::string>();
    } else if (d.is_object()) {
        text = d.value("text", std::string());
    } else {
        return;
    }
    if (st.prompt.size() < text.size() + 1) st.prompt.resize(text.size() + 64, 0);
    std::memset(st.prompt.data(), 0, st.prompt.size());
    const size_t n = std::min(text.size(), st.prompt.size() - 1);
    if (n) std::memcpy(st.prompt.data(), text.data(), n);
    st.prompt[n] = 0;

    st.pendingAttachments.clear();
    st.pendingContextRefs.clear();
    if (!d.is_object()) return;

    if (d.contains("attachments") && d["attachments"].is_array()) {
        for (const auto& a : d["attachments"]) {
            if (!a.is_string()) continue;
            std::string path = a.get<std::string>();
            if (path.empty()) continue;
            if (std::find(st.pendingAttachments.begin(), st.pendingAttachments.end(), path) ==
                st.pendingAttachments.end())
                st.pendingAttachments.push_back(std::move(path));
        }
    }
    if (d.contains("context_refs") && d["context_refs"].is_array()) {
        for (const auto& r : d["context_refs"]) {
            if (!r.is_object()) continue;
            AppState::ContextRef ref;
            ref.kind = r.value("kind", std::string());
            ref.id = r.value("id", std::string());
            ref.label = r.value("label", std::string());
            if (r.contains("doc_id") && r["doc_id"].is_string())
                ref.docId = r["doc_id"].get<std::string>();
            else if (r.contains("docId") && r["docId"].is_string())
                ref.docId = r["docId"].get<std::string>();
            if (ref.id.empty() && ref.kind.empty() && ref.label.empty()) continue;
            st.pendingContextRefs.push_back(std::move(ref));
        }
    }
}

void savePromptDraft(AppState& st, bool force) {
    // Per-key debounce so switching conversations never confuses last-write state.
    static std::string s_lastKey;
    static std::string s_lastSaved;
    static double s_lastWrite = -1e9;
    const std::string key = promptDraftKey(st);
    const std::string cur = promptDraftFingerprint(st);
    const double now = ImGui::GetTime();
    if (!force) {
        if (key == s_lastKey && cur == s_lastSaved) return;
        if (key == s_lastKey && (now - s_lastWrite) < 0.40) return;
    }
    s_lastKey = key;
    s_lastSaved = cur;
    s_lastWrite = now;
    try {
        namespace fs = std::filesystem;
        const std::string path = promptDraftsPath();
        nlohmann::json root = nlohmann::json::object();
        {
            std::ifstream in(path);
            if (in) {
                try { in >> root; } catch (...) { root = nlohmann::json::object(); }
            }
        }
        if (!root.is_object()) root = nlohmann::json::object();
        const nlohmann::json draft = promptDraftObject(st);
        if (promptDraftIsEmpty(draft)) root.erase(key);
        else root[key] = draft;
        fs::create_directories(appdataAppDir());
        atomicWriteFileDurable(path, root.dump(1));
    } catch (...) {
    }
}

void clearPromptDraft(AppState& st) {
    try {
        namespace fs = std::filesystem;
        const std::string path = promptDraftsPath();
        nlohmann::json root = nlohmann::json::object();
        {
            std::ifstream in(path);
            if (in) {
                try { in >> root; } catch (...) { return; }
            }
        }
        if (!root.is_object()) return;
        if (root.erase(promptDraftKey(st)) == 0) return;
        atomicWriteFileDurable(path, root.dump(1));
        // Reset debounce fingerprint so a subsequent empty composer doesn't rewrite.
    } catch (...) {
    }
}

static void restorePromptDraft(AppState& st) {
    // Don't clobber a non-empty composer (user already typing) — session switch clears first.
    if (st.prompt.data() && st.prompt.data()[0] != '\0') return;
    // Also skip if chips already present (e.g. mid-frame attach) unless both empty.
    if (!st.pendingAttachments.empty() || !st.pendingContextRefs.empty()) return;
    try {
        std::ifstream in(promptDraftsPath());
        if (!in) return;
        nlohmann::json root;
        in >> root;
        if (!root.is_object()) return;
        auto it = root.find(promptDraftKey(st));
        if (it == root.end()) return;
        // Backward compatible: plain string = text-only draft.
        if (it->is_string()) {
            const std::string d = it->get<std::string>();
            if (d.empty()) return;
            applyPromptDraftObject(st, *it);
            return;
        }
        if (!it->is_object()) return;
        if (promptDraftIsEmpty(*it)) return;
        applyPromptDraftObject(st, *it);
    } catch (...) {
    }
}


// A stable key for the active document: the saved file path if any (survives restarts), else
// the live doc id, else "scratch" (an unsaved, never-touched session).
std::string convDocKey(AppState& st) {
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
        const auto& t = st.docTabs[st.activeTab];
        if (!t.path.empty()) return "file:" + t.path;
        if (!t.docId.empty()) return "doc:" + t.docId;
    }
    std::string d = st.docMirror.hasDoc() ? st.docMirror.docId() : std::string();
    return d.empty() ? std::string("scratch") : ("doc:" + d);
}

std::string newConvId(AppState& st) { return "c" + std::to_string(++st.convCounter); }

// Guarantee at least one (active) conversation exists, adopting whatever is already in st.chat.
void ensureConversations(AppState& st) {
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size()) return;
    st.conversations.clear();
    Conversation c;
    c.id = newConvId(st);
    c.title = "Chat 1";
    st.conversations.push_back(std::move(c));
    st.activeConv = 0;  // st.chat remains the active buffer
}

// Serialize PlanState for chat_history.json — shape matches parsePlanState().
static nlohmann::json planStateToJson(const PlanState& ps) {
    nlohmann::json j = nlohmann::json::object();
    j["shape"] = ps.shape;
    j["title"] = ps.title;
    j["summary"] = ps.summary;
    j["revision"] = ps.revision;
    j["from_event"] = ps.fromEvent;
    nlohmann::json items = nlohmann::json::array();
    for (const auto& it : ps.items) {
        items.push_back({{"id", it.id},
                         {"title", it.title},
                         {"desc", it.desc},
                         {"status", it.status},
                         {"kind", it.kind},
                         {"phase", it.phase},
                         {"parent_id", it.parent_id}});
    }
    j["items"] = std::move(items);
    if (!ps.edges.empty()) {
        nlohmann::json edges = nlohmann::json::array();
        for (const auto& e : ps.edges)
            edges.push_back({{"from", e.from}, {"to", e.to}});
        j["edges"] = std::move(edges);
    }
    return j;
}

void persistChatHistory(AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    if (st.chatDocKey.empty()) return;
    namespace fs = std::filesystem;
    const std::string path = chatHistoryPath();
    nlohmann::json root = nlohmann::json::object();
    bool hadNontrivialFile = false;
    bool parseOk = false;
    {
        std::ifstream in(path, std::ios::binary);
        if (in) {
            // Probe size first so a corrupt-but-non-trivial file can be preserved.
            in.seekg(0, std::ios::end);
            const auto sz = in.tellg();
            in.seekg(0, std::ios::beg);
            hadNontrivialFile = (sz > 2);
            try {
                in >> root;
                parseOk = root.is_object();
            } catch (...) {
                root = nlohmann::json::object();
                parseOk = false;
            }
            if (!parseOk) root = nlohmann::json::object();
        }
    }
    // Corrupt existing store: rename aside for recovery before we write a fresh object.
    // Never silently clobber multi-key history with an empty root.
    if (hadNontrivialFile && !parseOk) {
        try {
            const auto ts = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
            const std::string bak = path + ".corrupt-" + std::to_string(static_cast<long long>(ts));
            std::error_code ec;
            fs::rename(path, bak, ec);
            if (ec) {
                // Fallback copy+remove if rename across devices fails.
                fs::copy_file(path, bak, fs::copy_options::overwrite_existing, ec);
            }
        } catch (...) {
        }
    }
    nlohmann::json convs = nlohmann::json::array();
    for (int i = 0; i < (int)st.conversations.size(); ++i) {
        const std::vector<ChatMsg>& msgs = (i == st.activeConv) ? st.chat : st.conversations[i].msgs;
        nlohmann::json jm = nlohmann::json::array();
        for (const auto& m : msgs) {
            std::string body = m.text;
            if (body.empty() && !m.fromUser && !m.activity.empty()) {
                // Design/build turns put their result in the activity timeline, not
                // m.text — persist a concise summary so the turn survives reload
                // instead of vanishing (the whole session used to disappear).
                int steps = 0;
                std::string lastReason;
                for (const auto& a : m.activity) {
                    if (a.rowKind == 0 && !a.missionHeader) ++steps;
                    if (!a.reason.empty()) lastReason = a.reason;
                }
                body = !lastReason.empty()
                           ? lastReason
                           : ("(design run \xc2\xb7 " + std::to_string(steps) + " steps)");
            }
            // Keep turns that only carry a plan (no text body) so the task list survives reload.
            // Also ALWAYS keep a still-running turn even when it has nothing yet: dropping it
            // left a user prompt with no reply bubble after a mid-run crash, and the restore
            // path below needs the row to stamp the "interrupted" marker on.
            if (body.empty() && !m.planState.present && !m.running) continue;
            nlohmann::json entry = {{"u", m.fromUser}, {"t", body}, {"e", m.errKind}};
            // In-flight marker: this turn was still streaming when the snapshot was taken. On
            // reload it means the app died (or was killed) mid-run — see applyChatEntry.
            if (!m.fromUser && m.running) entry["run"] = true;
            if (m.ts > 0.0) entry["ts"] = m.ts;   // send/receive time for the in-bubble age label
            if (!m.fromUser && !m.model.empty()) entry["m"] = m.model;
            if (m.copilotMode >= 0) entry["cm"] = m.copilotMode;
            if (m.planState.present) entry["plan_state"] = planStateToJson(m.planState);
            // Persist activity timeline (capped) so thoughts/tools survive reload.
            if (!m.fromUser && !m.activity.empty()) {
                nlohmann::json act = nlohmann::json::array();
                const int nAct = (int)m.activity.size();
                // Keep all thought/tool/routing rows (rk 2/3/10) + last N stage rows so
                // mid-turn kill/restart still has planning + tools visible.
                std::vector<int> keepIdx;
                keepIdx.reserve((size_t)nAct);
                int stagesKept = 0;
                const int kMaxStages = 80;
                for (int ai = nAct - 1; ai >= 0; --ai) {
                    const auto& a = m.activity[(size_t)ai];
                    const bool priority = (a.rowKind == 2 || a.rowKind == 3 || a.rowKind == 10 ||
                                           a.rowKind == 1 || a.rowKind == 18 ||
                                           a.stage == "__stop_meta__");
                    if (priority) {
                        keepIdx.push_back(ai);
                    } else if (stagesKept < kMaxStages) {
                        keepIdx.push_back(ai);
                        ++stagesKept;
                    }
                }
                std::sort(keepIdx.begin(), keepIdx.end());
                for (int ai : keepIdx) {
                    const auto& a = m.activity[(size_t)ai];
                    nlohmann::json row = {
                        {"rk", a.rowKind},
                        {"stage", a.stage},
                        {"title", a.title},
                        {"status", a.status},
                        {"reason", a.reason},
                    };
                    std::string det = a.detail;
                    const bool isOps = (a.stage == "stream:out" && a.opsCount > 0) ||
                                       (a.title.find("Building ops") != std::string::npos);
                    const size_t cap = isOps ? 2000u : 8000u;
                    if (det.size() > cap) det = det.substr(0, cap - 1) + "\xe2\x80\xa6";
                    if (!det.empty()) row["detail"] = det;
                    act.push_back(std::move(row));
                }
                if (!act.empty()) entry["activity"] = std::move(act);
            }
            // Persist tool-call artifact paths so Artifacts panel + reopen keep file links.
            if (!m.fromUser && !m.activity.empty()) {
                nlohmann::json arts = nlohmann::json::array();
                std::set<std::string> seen;
                for (const auto& a : m.activity) {
                    if (a.detail.find("\x1e" "ART\x1e") == std::string::npos) continue;
                    const size_t sep = a.detail.find("\x1e" "ART\x1e");
                    std::string blob = a.detail.substr(sep);
                    size_t nl = blob.find('\n');
                    if (nl != std::string::npos) blob = blob.substr(nl + 1);
                    std::istringstream iss(blob);
                    std::string line;
                    while (std::getline(iss, line)) {
                        if (line.empty()) continue;
                        size_t tab = line.find('\t');
                        std::string kindA = tab == std::string::npos ? "file" : line.substr(0, tab);
                        std::string path  = tab == std::string::npos ? line : line.substr(tab + 1);
                        if (path.empty() || !seen.insert(path).second) continue;
                        arts.push_back({{"kind", kindA}, {"path", path},
                                        {"tool", a.title.empty() ? a.stage : a.title}});
                        if (arts.size() >= 32) break;
                    }
                    if (arts.size() >= 32) break;
                }
                if (!arts.empty()) entry["artifacts"] = std::move(arts);
            }
            jm.push_back(std::move(entry));
        }
        nlohmann::json convObj = {{"id", st.conversations[i].id},
                                 {"title", st.conversations[i].title},
                                 {"msgs", jm}};
        convObj["scope"] = st.conversations[i].scopeClass;  // rail color-code (-1 = unassessed)
        if (st.conversations[i].archived) convObj["archived"] = true;
        if (i == st.activeConv) {
            // The live agent-execution DAG belongs to the active session's run — save it as a
            // session artifact so the workflow trace survives a reopen.
            nlohmann::json dag = ui::agentDagToJson();
            if (dag.is_object() && dag.contains("nodes") && !dag["nodes"].empty())
                convObj["agent_dag"] = std::move(dag);
        }
        const int lct = (i == st.activeConv) ? st.lastContextTokens
                                             : st.conversations[i].lastContextTokens;
        if (lct > 0) convObj["last_context_tokens"] = lct;
        const auto& qp = (i == st.activeConv) ? st.queuedPrompts
                                              : st.conversations[i].queuedPrompts;
        if (!qp.empty()) convObj["queued_prompts"] = qp;
        convs.push_back(std::move(convObj));
    }
    // Load-modify-write: only replace this doc key; every other key stays intact.
    root[st.chatDocKey] = {{"active", st.activeConv}, {"conversations", convs}};
    // Rekey migration cleanup: drop the pre-autosave doc:<id> key once the entry lives at file:.
    if (!s_chatKeyToErase.empty() && s_chatKeyToErase != st.chatDocKey) {
        root.erase(s_chatKeyToErase);
        s_chatKeyToErase.clear();
    }
    try {
        fs::create_directories(appdataAppDir());
        // Durable atomic write (tmp + fsync + rename + dir fsync) — a hard crash mid-run must
        // never be able to leave a truncated/empty history behind.
        if (!atomicWriteFileDurable(path, root.dump(1))) return;
        appstatePersistSync();  // WASM: flush IDBFS; no-op on desktop
#ifdef __EMSCRIPTEN__
        // Also push this key to the shared workspace store (async; never block UI thread).
        if (root.contains(st.chatDocKey) && root[st.chatDocKey].is_object())
            wasmPutSharedChat(st.chatDocKey, root[st.chatDocKey]);
#endif
    } catch (...) {
    }
}

// Map routing labels (complexity / structure / execution_style) to a scope class:
// simple/solo -> 0, standard -> 1, complex -> 2, epic/huge -> 3.
static int scopeClassFromRouting(const std::string& complexity, const std::string& structure,
                                 const std::string& execStyle) {
    auto norm = [](std::string s) {
        for (char& ch : s) ch = (char)std::tolower((unsigned char)ch);
        return s;
    };
    const std::string vals[3] = {norm(complexity), norm(structure), norm(execStyle)};
    auto anyIs = [&](std::initializer_list<const char*> keys) {
        for (const auto& v : vals)
            for (const char* k : keys)
                if (v == k) return true;
        return false;
    };
    if (anyIs({"epic", "huge", "company", "eng_firm"})) return 3;
    if (anyIs({"complex", "dag", "swarm"})) return 2;
    if (anyIs({"simple", "solo", "trivial"})) return 0;
    // "standard", "subagent", "edit", or any assessed-but-unrecognized label.
    return 1;
}

// Consume the routing assessment stashed by feed.cpp and stamp the ACTIVE conversation on
// its FIRST routing event (scopeClass -1 -> class): sets the rail color-code class and, when
// the title is still the default "Chat N", a concise model-derived name from the routing
// domain (+ short snippet when the classifier provided one).
static void applyPendingScopeAssessment(AppState& st) {
    std::string domain, complexity, structure, execStyle, snippet;
    if (!takePendingScopeAssessment(domain, complexity, structure, execStyle, snippet)) return;
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    Conversation& c = st.conversations[st.activeConv];
    if (c.scopeClass >= 0) return;  // first assessment wins; later routing events enrich only
    c.scopeClass = scopeClassFromRouting(complexity, structure, execStyle);
    const bool defaultTitle = c.title.empty() || c.title == "Chat" ||
        (c.title.rfind("Chat ", 0) == 0 &&
         c.title.find_first_not_of("0123456789", 5) == std::string::npos);
    if (defaultTitle) {
        std::string name = domain;
        if (!name.empty()) name[0] = (char)std::toupper((unsigned char)name[0]);
        if (!snippet.empty()) {
            std::string s = snippet;
            const size_t nl = s.find('\n');
            if (nl != std::string::npos) s = s.substr(0, nl);
            if (s.size() > 48) s = s.substr(0, 45) + "...";
            if (!s.empty()) name += (name.empty() ? std::string() : std::string(" - ")) + s;
        }
        if (!name.empty()) c.title = name;
    }
    markChatHistoryDirty();
}

// Debounced flush while a turn is running (min 1.0s). Force bypasses the timer.
void maybePersistChatHistoryDebounced(AppState& st, bool force) {
    applyPendingScopeAssessment(st);
    if (!force && !s_chatHistoryDirty.load(std::memory_order_relaxed)) return;
    const double now = ImGui::GetTime();
    if (!force && (now - s_chatHistoryLastPersist) < 1.0) return;
    // persistChatHistory no-ops when the session binding isn't ready yet. Clearing the dirty
    // flag in that case dropped the pending state on the floor until the NEXT event marked it
    // dirty again — a run whose last event arrived before binding never got saved. Keep the
    // flag set and retry on the following frame instead.
    if (st.chatDocKey.empty() || st.activeConv < 0 ||
        st.activeConv >= (int)st.conversations.size())
        return;
    s_chatHistoryDirty.store(false, std::memory_order_relaxed);
    s_chatHistoryLastPersist = now;
    persistChatHistory(st);
}


// Drop one or more orphaned doc keys from chat_history.json (Other sessions cleanup).
// Load-modify-write with the same atomic tmp+rename path as persistChatHistory.
// Never touches the active st.chatDocKey. Local store only — does not DELETE on the
// shared workspace chat endpoint (web-shared sessions may reappear after merge).
void eraseChatHistoryKeys(const std::vector<std::string>& keys) {
    if (keys.empty()) return;
    namespace fs = std::filesystem;
    const std::string path = chatHistoryPath();
    nlohmann::json root = nlohmann::json::object();
    {
        std::ifstream in(path, std::ios::binary);
        if (in) {
            try {
                in >> root;
            } catch (...) {
                return;  // corrupt — don't clobber
            }
        }
    }
    if (!root.is_object()) return;
    bool changed = false;
    for (const std::string& k : keys) {
        if (k.empty()) continue;
        if (root.erase(k) > 0) changed = true;
    }
    if (!changed) return;
    try {
        fs::create_directories(appdataAppDir());
        if (!atomicWriteFileDurable(path, root.dump(1))) return;
        appstatePersistSync();
    } catch (...) {
    }
}


void pollChatOpenFoam(AppState& st) {
    if (!st.chatCfdPending || !st.simClient) return;
    core::SimJobResult& job = st.simClient->job();
    if (!job.ready.load()) return;                 // OpenFOAM still meshing/solving
    if (job.ownerDocId != "__chat_cfd__") return;  // not the chat's job (a study's result)
    st.chatCfdPending = false;
    job.ready = false;                             // consume (SimManager ignored it — wrong owner)

    ChatMsg* msg = nullptr;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
        if (!it->fromUser && it->running) { msg = &*it; break; }

    std::string answer, err;
    if (!job.ok) {
        err = job.error.empty() ? "OpenFOAM run failed" : job.error;
    } else {
        const double cd = job.result.value("cd", 0.0), cl = job.result.value("cl", 0.0);
        const double drag = job.result.value("drag_n", 0.0), lift = job.result.value("lift_n", 0.0);
        const double re = job.result.value("reynolds", 0.0);
        char b[512];
        std::snprintf(b, sizeof(b),
            "Accurate OpenFOAM RANS (k-omega SST) at %.1f m/s, %.1f deg AoA (Re %.0f): drag %.3f N, "
            "lift %.3f N, Cd %.3f, Cl %.3f. Validated solver — trust these as absolute coefficients.",
            st.chatCfdVel, st.chatCfdAoa, re, drag, lift, cd, cl);
        answer = b;
    }
    if (msg) {
        msg->running = false; msg->liveStatus.clear();
        if (!err.empty()) { msg->errKind = ERR_SYSTEM; msg->text = "The CFD sim failed: " + err; }
        else msg->text = answer;
    }
    st.status = err.empty() ? "Ready." : "CFD failed.";
    st.statusKind = err.empty() ? 0 : 2;
}

// Apply one chat_history.json entry object into st.conversations/chat.
static void applyChatEntry(AppState& st, const nlohmann::json& e) {
    freeChatTextures(st.chat);
    st.chat.clear();
    st.conversations.clear();
    for (const auto& jc : e.value("conversations", nlohmann::json::array())) {
        Conversation c;
        c.id = jc.value("id", newConvId(st));
        c.title = jc.value("title", std::string("Chat"));
        c.scopeClass = jc.value("scope", -1);  // legacy sessions omit the key
        c.archived = jc.value("archived", false);
        for (const auto& m : jc.value("msgs", nlohmann::json::array())) {
            ChatMsg cm;
            cm.fromUser = m.value("u", false);
            cm.text = m.value("t", std::string());
            cm.errKind = m.value("e", 0);
            if (!cm.fromUser) cm.model = m.value("m", std::string());
            cm.copilotMode = m.value("cm", -1);  // -1 when absent (legacy sessions)
            cm.ts = m.value("ts", 0.0);          // 0 = legacy (constructed with now) — no age label
            // Restore plan/task-list state when present (legacy sessions omit the key).
            if (m.contains("plan_state") && m["plan_state"].is_object()) {
                cm.planState = parsePlanState(m["plan_state"]);
                cm.planState.fromEvent =
                    m["plan_state"].value("from_event", true);
            }
            // Restore activity timeline (thoughts/tools) when persisted.
            if (m.contains("activity") && m["activity"].is_array()) {
                for (const auto& row : m["activity"]) {
                    if (!row.is_object()) continue;
                    ActivityItem a;
                    a.rowKind = row.value("rk", 0);
                    a.stage = row.value("stage", std::string());
                    a.title = row.value("title", std::string());
                    a.status = row.value("status", std::string());
                    a.reason = row.value("reason", std::string());
                    a.detail = row.value("detail", std::string());
                    cm.activity.push_back(std::move(a));
                }
            }
            // Restore agent-created file links into a synthetic activity row so the
            // Artifacts panel can list them after a session reopen.
            if (m.contains("artifacts") && m["artifacts"].is_array() && !m["artifacts"].empty()) {
                ActivityItem artRow;
                artRow.rowKind = 3;
                artRow.stage = "artifacts";
                artRow.title = "Session files";
                artRow.status = "done";
                std::string blob;
                for (const auto& art : m["artifacts"]) {
                    if (!art.is_object()) continue;
                    std::string path = art.value("path", std::string());
                    std::string kindA = art.value("kind", std::string("file"));
                    if (path.empty()) continue;
                    if (!blob.empty()) blob += "\n";
                    blob += kindA;
                    blob += '\t';
                    blob += path;
                }
                if (!blob.empty()) {
                    artRow.detail = std::string("\x1e" "ART\x1e\n") + blob;
                    cm.activity.push_back(std::move(artRow));
                }
            }
            // Crash recovery: a turn persisted as still-running never got a result — the app
            // died mid-run. Never resume it as "running" (the worker is gone), but don't let it
            // masquerade as a completed answer either: flag it partial and say so.
            if (!cm.fromUser && m.value("run", false)) {
                cm.resultPartial = true;
                const char* note = "(Interrupted \xe2\x80\x94 the app closed while this run was "
                                   "in progress. Nothing after this point was saved.)";
                if (cm.text.empty()) cm.text = note;
                else if (cm.text.find("Interrupted \xe2\x80\x94") == std::string::npos)
                    cm.text += std::string("\n\n") + note;
            }
            cm.running = false;
            c.msgs.push_back(std::move(cm));
        }
        c.lastContextTokens = jc.value("last_context_tokens", 0);
        if (jc.contains("queued_prompts") && jc["queued_prompts"].is_array()) {
            for (const auto& q : jc["queued_prompts"])
                if (q.is_string()) c.queuedPrompts.push_back(q.get<std::string>());
        }
        st.conversations.push_back(std::move(c));
    }
    if (st.conversations.empty()) { st.activeConv = -1; ensureConversations(st); return; }
    int act = e.value("active", 0);
    if (act < 0 || act >= (int)st.conversations.size()) act = 0;
    st.activeConv = act;
    {
        // Restore the active session's saved agent-execution DAG into the live store.
        const auto& convsJson = e.value("conversations", nlohmann::json::array());
        if (act < (int)convsJson.size() && convsJson[act].is_object() &&
            convsJson[act].contains("agent_dag"))
            ui::agentDagFromJson(convsJson[act]["agent_dag"]);
    }
    st.chat = std::move(st.conversations[act].msgs);
    st.lastContextTokens = st.conversations[act].lastContextTokens;
    st.queuedPrompts = std::move(st.conversations[act].queuedPrompts);
    st.conversations[act].msgs.clear();
    st.conversations[act].queuedPrompts.clear();
}


// Replace conversations/chat with the saved set for `key` (or a fresh empty chat if none).
// migrateFrom: on miss for `key`, adopt migrateFrom's entry (file or still-in-memory) so an
// autosave rekey (doc:<id> -> file:<path>) never clears the visible conversation list.
void loadChatHistoryForKey(AppState& st, const std::string& key,
                                  const std::string& migrateFrom) {
    nlohmann::json root = nlohmann::json::object();
    {
        std::ifstream in(chatHistoryPath());
        if (in) { try { in >> root; } catch (...) {} }
    }
    auto hasEntry = [&](const std::string& k) {
        return root.is_object() && root.contains(k) && root[k].is_object();
    };
    if (hasEntry(key)) {
        applyChatEntry(st, root[key]);
        restorePromptDraft(st);
        return;
    }
    // Rekey migration: new key empty, but old key has history (disk or live memory).
    if (!migrateFrom.empty() && migrateFrom != key) {
        if (hasEntry(migrateFrom)) {
            applyChatEntry(st, root[migrateFrom]);
            s_chatKeyToErase = migrateFrom;  // drop old key on next persist under new key
            restorePromptDraft(st);
            return;
        }
        // In-memory still holds the outgoing doc's conversations (persist just wrote them
        // under migrateFrom). Keep them bound to the new key instead of clearing.
        if (!st.conversations.empty() && st.activeConv >= 0) {
            s_chatKeyToErase = migrateFrom;
            restorePromptDraft(st);
            return;
        }
    }
    if (st.conversations.empty() && st.activeConv < 0) {
        ensureConversations(st);  // first init: adopt current st.chat (welcome) as conversation 1
    } else {
        freeChatTextures(st.chat);  // switching to a different doc with no history: fresh chat
        st.chat.clear();
        st.lastContextTokens = 0;
        st.queuedPrompts.clear();
        st.conversations.clear();
        st.activeConv = -1;
        ensureConversations(st);
    }
    restorePromptDraft(st);
}

#ifdef __EMSCRIPTEN__
// Count messages in one chat_history entry (all conversations) — merge tie-break.
static int wasmChatEntryMsgCount(const nlohmann::json& e) {
    int n = 0;
    if (!e.is_object()) return 0;
    const auto& convs = e.value("conversations", nlohmann::json::array());
    if (!convs.is_array()) return 0;
    for (const auto& c : convs) {
        const auto& msgs = c.value("msgs", nlohmann::json::array());
        if (msgs.is_array()) n += (int)msgs.size();
    }
    return n;
}

// Fire-and-forget async PUT of one (or few) keys — copy of fs_backend saveRecents pattern.
// Heap-owns the payload until the fetch callback frees it. Never EMSCRIPTEN_FETCH_SYNCHRONOUS.
static void wasmPutSharedChat(const std::string& key, const nlohmann::json& entry) {
    if (key.empty() || !entry.is_object()) return;
    nlohmann::json body = nlohmann::json::object();
    body[key] = entry;
    const std::string payload = body.dump();
    char* buf = static_cast<char*>(std::malloc(payload.size()));
    if (!buf) return;
    std::memcpy(buf, payload.data(), payload.size());
    emscripten_fetch_attr_t attr;
    emscripten_fetch_attr_init(&attr);
    std::strcpy(attr.requestMethod, "PUT");
    attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;  // async
    static const char* headers[] = {"Content-Type", "application/json", nullptr};
    attr.requestHeaders = headers;
    attr.requestData = buf;
    attr.requestDataSize = payload.size();
    attr.userData = buf;
    attr.onsuccess = [](emscripten_fetch_t* f) {
        std::free(f->userData);
        emscripten_fetch_close(f);
    };
    attr.onerror = [](emscripten_fetch_t* f) {
        std::free(f->userData);
        emscripten_fetch_close(f);
    };
    emscripten_fetch(&attr, "/workspace/chat");
}

// One-shot async GET of the full shared store. Result stashed for syncChatToDoc merge.
static void wasmKickSharedChatFetch() {
    if (s_chatShareFetchStarted) return;
    s_chatShareFetchStarted = true;
    emscripten_fetch_attr_t attr;
    emscripten_fetch_attr_init(&attr);
    std::strcpy(attr.requestMethod, "GET");
    attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;  // async
    attr.onsuccess = [](emscripten_fetch_t* f) {
        try {
            if (f->status >= 200 && f->status < 300 && f->data && f->numBytes > 0) {
                auto j = nlohmann::json::parse(f->data, f->data + f->numBytes);
                if (j.is_object()) {
                    s_chatSharePending = std::move(j);
                    s_chatShareHasPending = true;
                }
            } else if (f->status >= 200 && f->status < 300) {
                // Empty body / {} — still a successful empty store.
                s_chatSharePending = nlohmann::json::object();
                s_chatShareHasPending = true;
            }
        } catch (...) {
        }
        emscripten_fetch_close(f);
    };
    attr.onerror = [](emscripten_fetch_t* f) {
        emscripten_fetch_close(f);
    };
    emscripten_fetch(&attr, "/workspace/chat");
}

// Merge server store into local IDBFS chat_history.json.
// Rule: server wins for keys local lacks; when both have a key, prefer more messages.
// If the active key's entry changes, reload the visible conversation list.
static void wasmApplySharedChatPending(AppState& st) {
    if (!s_chatShareHasPending) return;
    s_chatShareHasPending = false;
    nlohmann::json server = std::move(s_chatSharePending);
    s_chatSharePending = nlohmann::json::object();
    if (!server.is_object()) return;

    namespace fs = std::filesystem;
    const std::string path = chatHistoryPath();
    nlohmann::json local = nlohmann::json::object();
    {
        std::ifstream in(path);
        if (in) {
            try {
                in >> local;
            } catch (...) {
                local = nlohmann::json::object();
            }
        }
    }
    if (!local.is_object()) local = nlohmann::json::object();

    const std::string curKey = st.chatDocKey;
    const bool hadCur = !curKey.empty() && local.contains(curKey) && local[curKey].is_object();
    const std::string prevDump = hadCur ? local[curKey].dump() : std::string();

    bool dirty = false;
    for (auto it = server.begin(); it != server.end(); ++it) {
        if (!it.value().is_object()) continue;
        const std::string& k = it.key();
        if (!local.contains(k) || !local[k].is_object()) {
            local[k] = it.value();
            dirty = true;
            continue;
        }
        const int sn = wasmChatEntryMsgCount(it.value());
        const int ln = wasmChatEntryMsgCount(local[k]);
        if (sn > ln) {
            local[k] = it.value();
            dirty = true;
        }
    }
    if (!dirty) {
        // Still reload current if server filled a key we only had in-memory empty.
        if (!curKey.empty() && local.contains(curKey) && local[curKey].is_object()) {
            const std::string nowDump = local[curKey].dump();
            if (nowDump != prevDump) applyChatEntry(st, local[curKey]);
        }
        return;
    }

    try {
        fs::create_directories(appdataAppDir());
        if (!atomicWriteFileDurable(path, local.dump(1))) return;
        appstatePersistSync();
    } catch (...) {
        return;
    }

    if (!curKey.empty() && local.contains(curKey) && local[curKey].is_object()) {
        const std::string nowDump = local[curKey].dump();
        if (nowDump != prevDump) applyChatEntry(st, local[curKey]);
    }
}
#endif

// Detect a document change and swap the chat history accordingly (called each frame).
void syncChatToDoc(AppState& st) {
#ifdef __EMSCRIPTEN__
    wasmKickSharedChatFetch();       // once: async GET full shared store
    wasmApplySharedChatPending(st);  // merge when the GET lands
#endif
    // Sessions "Other" browse pins a foreign key until the user returns.
    if (!s_chatKeyHold.empty()) { ensureConversations(st); return; }
    std::string key = convDocKey(st);
    if (key == st.chatDocKey) { ensureConversations(st); return; }
    const std::string prev = st.chatDocKey;
    if (!prev.empty()) persistChatHistory(st);  // save the outgoing doc's conversations
    loadChatHistoryForKey(st, key, prev);       // migrate prev -> key when autosave rekeys
    st.chatDocKey = key;
    // Migration wrote history under migrateFrom only; mirror it to the new key immediately
    // and drop the old key (s_chatKeyToErase) so a crash doesn't orphan the conversation.
    if (!s_chatKeyToErase.empty())
        persistChatHistory(st);
}

static void clearComposerState(AppState& st) {
    if (st.prompt.data() && !st.prompt.empty()) {
        std::memset(st.prompt.data(), 0, st.prompt.size());
        st.prompt[0] = 0;
    }
    st.pendingAttachments.clear();
    st.pendingContextRefs.clear();
    st.contextPickActive = false;
}

void parkActiveConversation(AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    // Keep in-progress prompt + chips for this session on disk before parking.
    savePromptDraft(st, /*force=*/true);
    Conversation& c = st.conversations[st.activeConv];
    c.msgs = std::move(st.chat);
    c.lastContextTokens = st.lastContextTokens;
    c.queuedPrompts = std::move(st.queuedPrompts);
    st.chat.clear();
    st.lastContextTokens = 0;
    st.queuedPrompts.clear();
}

static void restoreActiveConversation(AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    Conversation& c = st.conversations[st.activeConv];
    st.chat = std::move(c.msgs);
    st.lastContextTokens = c.lastContextTokens;
    st.queuedPrompts = std::move(c.queuedPrompts);
    c.msgs.clear();
    c.queuedPrompts.clear();
    // Composer is session-scoped: wipe then restore this session's draft.
    clearComposerState(st);
    restorePromptDraft(st);
}

static bool conversationRunning(const Conversation& c) {
    for (auto it = c.msgs.rbegin(); it != c.msgs.rend(); ++it)
        if (!it->fromUser && it->running) return true;
    return false;
}

bool activeConversationRunning(AppState& st) {
    if (aiTurnActive(st)) return true;
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size())
        return conversationRunning(st.conversations[st.activeConv]);
    return false;
}

void newConversation(AppState& st) {
    ensureConversations(st);
    parkActiveConversation(st);  // force-saves old draft
    Conversation c;
    c.id = newConvId(st);
    c.title = "Chat " + std::to_string((int)st.conversations.size() + 1);
    st.conversations.push_back(std::move(c));
    st.activeConv = (int)st.conversations.size() - 1;
    st.showSessionsPicker = false;
    clearComposerState(st);
    clearPromptDraft(st);  // new empty session has no draft
    persistChatHistory(st);
}

void switchConversation(AppState& st, int idx) {
    if (idx == st.activeConv || idx < 0 || idx >= (int)st.conversations.size()) return;
    parkActiveConversation(st);  // force-saves draft + parks chat (incl. running msgs)
    st.activeConv = idx;
    restoreActiveConversation(st);  // restores chat + draft for target
    st.showSessionsPicker = false;
    // Force disk so both sessions' msgs + drafts survive kill mid-switch.
    persistChatHistory(st);
}

void closeConversation(AppState& st, int idx) {
    if (idx < 0 || idx >= (int)st.conversations.size()) return;
    bool closingActive = (idx == st.activeConv);
    if (closingActive) {
        // Persist leaving session's draft before wiping composer.
        savePromptDraft(st, /*force=*/true);
        freeChatTextures(st.chat);
        st.chat.clear();
        st.lastContextTokens = 0;
        st.queuedPrompts.clear();
        clearComposerState(st);
    } else {
        freeChatTextures(st.conversations[idx].msgs);
    }
    // Drop draft key for the closed conversation.
    {
        const std::string closedId = st.conversations[idx].id;
        try {
            namespace fs = std::filesystem;
            const std::string path = promptDraftsPath();
            nlohmann::json root = nlohmann::json::object();
            {
                std::ifstream in(path);
                if (in) {
                    try { in >> root; } catch (...) { root = nlohmann::json::object(); }
                }
            }
            if (root.is_object()) {
                std::string key = st.chatDocKey.empty() ? convDocKey(st) : st.chatDocKey;
                key += "/";
                key += closedId;
                if (root.erase(key) > 0) atomicWriteFileDurable(path, root.dump(1));
            }
        } catch (...) {
        }
    }
    st.conversations.erase(st.conversations.begin() + idx);
    if (st.conversations.empty()) {
        st.activeConv = -1;
        ensureConversations(st);
        restoreActiveConversation(st);
        persistChatHistory(st);
        return;
    }
    if (closingActive) {
        int na = std::min(idx, (int)st.conversations.size() - 1);
        st.activeConv = na;
        restoreActiveConversation(st);
    } else if (idx < st.activeConv) {
        st.activeConv--;
    }
    persistChatHistory(st);
}

// Archive (or unarchive) a session in place: unlike closeConversation, the transcript and
// all history stay — the session just drops out of the default Sessions list until "Show
// archived" is toggled on. Archiving the ACTIVE session hops to the nearest non-archived one
// (preferring later indices, matching closeConversation's "next" bias); if none are left,
// starts a fresh session so the composer never lands on a hidden conversation.
void archiveConversation(AppState& st, int idx, bool archived) {
    if (idx < 0 || idx >= (int)st.conversations.size()) return;
    if (st.conversations[idx].archived == archived) return;
    st.conversations[idx].archived = archived;
    if (archived && idx == st.activeConv) {
        int target = -1;
        for (int j = idx + 1; j < (int)st.conversations.size(); ++j)
            if (!st.conversations[j].archived) { target = j; break; }
        if (target < 0)
            for (int j = idx - 1; j >= 0; --j)
                if (!st.conversations[j].archived) { target = j; break; }
        if (target < 0)
            newConversation(st);   // every session archived — land somewhere visible
        else
            switchConversation(st, target);
    }
    persistChatHistory(st);
}

// (Conversations are switched from the Sessions picker in the Copilot header.)

// Probe the local OpenAI-compatible endpoint for its loaded models + reachability, so the
// picker can hide local models that are down. Throttled to ~5s; force bypasses the throttle.

}  // namespace ui
