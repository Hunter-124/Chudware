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

// Resolve the live message vector for a conversation id — see core/app_state.h for the
// contract. Global scope (not ui::) to match its app_state.h declaration, exactly like
// scopeClassColor above.
std::vector<ChatMsg>* conversationMsgs(AppState& st, const std::string& convId) {
    if (convId.empty()) return nullptr;
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size() &&
        st.conversations[st.activeConv].id == convId)
        return &st.chat;
    for (auto& c : st.conversations)
        if (c.id == convId) return &c.msgs;
    return nullptr;
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

#ifdef __EMSCRIPTEN__
// Shared workspace chat (GET/PUT /workspace/chat). IDBFS remains local cache/fallback.
// Fetch callbacks stash results; merge runs on the UI thread via syncChatToDoc.
static bool s_chatShareFetchStarted = false;
static bool s_chatShareHasPending = false;
static nlohmann::json s_chatSharePending = nlohmann::json::object();
static void wasmPutSharedChat(const nlohmann::json& root);
static void wasmKickSharedChatFetch();
static void wasmApplySharedChatPending(AppState& st);
#endif

// ============================ conversation tabs + persistence ============================
// Each conversation is its own tab. The ACTIVE conversation's messages live in st.chat; the
// rest park in st.conversations[i].msgs. Sessions are FIRST-CLASS + document-independent:
// persisted as one file per session under <Documents>/Chudware/Sessions (schema v2, see
// chatSessionsDir below); Conversation::docLink is an
// optional reference to the related document, never an ownership key.

std::string chatHistoryPath() { return appdataAppDir() + "/chat_history.json"; }
static std::string promptDraftsPath() { return appdataAppDir() + "/prompt_drafts.json"; }

// Session transcripts live as ONE FILE PER SESSION under <Documents>/Chudware/Sessions
// (override: CHUDWARE_SESSIONS_DIR) — human-browsable, next to Bug-Reports. index.json is
// the v2 envelope (active id + session order); <id>.json is one full session object.
// The legacy appdata chat_history.json (single-file store) is imported on first load.
std::string chatSessionsDir() {
    static const std::string cached = [] {
        const char* env = std::getenv("CHUDWARE_SESSIONS_DIR");
        if (env && *env) return std::string(env);
        return defaultWorkspaceDir() + "/Sessions";
    }();
    return cached;
}
static std::string chatSessionsIndexPath() { return chatSessionsDir() + "/index.json"; }
static std::string chatSessionFileStem(const std::string& id) {
    std::string safe = id.empty() ? std::string("c0") : id;
    for (char& ch : safe) {
        const unsigned char u = (unsigned char)ch;
        if (!std::isalnum(u) && ch != '-' && ch != '_') ch = '_';
    }
    return safe;
}
static std::string chatSessionFilePath(const std::string& id) {
    return chatSessionsDir() + "/" + chatSessionFileStem(id) + ".json";
}

// File STEMS of sessions this process has verifiably held LIVE (loaded successfully, or
// written by us this run). The prune step in persistChatHistory may ONLY remove transcripts
// for these stems — anything else on disk (a crash-orphaned transcript whose index write
// never landed, a file that failed to parse, another instance's session) is user data we
// cannot vouch for and MUST keep. Recovery beats pruning: when in doubt, keep the file.
static std::set<std::string> s_pruneableSessionStems;

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

// Guarantee the CURRENTLY VIEWED conversation exists, adopting whatever is already in st.chat.
// Additive: never clears other resident conversations (other doc keys, or idle same-doc
// siblings) — those stay resident regardless of what the active view needs right now.
void ensureConversations(AppState& st) {
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size()) return;
    Conversation c;
    c.id = newConvId(st);
    c.title = "Chat 1";
    c.docLink = convDocKey(st);  // link to whatever document is current — reference only
    st.conversations.push_back(std::move(c));
    st.activeConv = (int)st.conversations.size() - 1;  // st.chat remains the active buffer
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

// Defined near the lifecycle functions below.
static bool conversationIsFresh(const AppState& st, int idx);

// Persist EVERY resident session as ONE flat list (schema v2) — sessions are
// document-independent, so the store is a simple {"version":2,"active","sessions":[…]}
// object fully described by the resident state (no load-modify-merge per document key;
// every session is resident by design). Fresh sessions (no messages, no queue, no run) are
// never written — same rule as empty parts never autosaving.
void persistChatHistory(AppState& st) {
    if (st.conversations.empty()) return;
    namespace fs = std::filesystem;
    std::error_code dirEc;
    fs::create_directories(chatSessionsDir(), dirEc);
    nlohmann::json sessions = nlohmann::json::array();  // WASM shared push only
    nlohmann::json order = nlohmann::json::array();
    std::set<std::string> writtenIds;  // defensive: never write an id twice in one snapshot
    std::string activeId;
    // A PARKED session whose only content is an unsent composer draft (saved under its
    // docKey/id key in prompt_drafts.json) is not "fresh" — the session is the draft's only
    // anchor; dropping it strands the user's typed text in an unreachable drafts entry.
    nlohmann::json drafts = nlohmann::json::object();
    {
        std::ifstream in(promptDraftsPath());
        if (in) { try { in >> drafts; } catch (...) { drafts = nlohmann::json::object(); } }
    }
    auto sessionHasDraft = [&](const std::string& id) {
        if (!drafts.is_object() || id.empty()) return false;
        const std::string suffix = "/" + id;  // "/" anchor: c1 must not match c12
        for (auto it = drafts.begin(); it != drafts.end(); ++it) {
            if (it.key().size() < suffix.size()) continue;
            if (it.key().compare(it.key().size() - suffix.size(), suffix.size(), suffix) != 0)
                continue;
            if (!promptDraftIsEmpty(it.value())) return true;
        }
        return false;
    };
    for (int i = 0; i < (int)st.conversations.size(); ++i) {
        // Fresh sessions (no messages, no queue, no run, no draft) are never written — same
        // rule as empty parts never autosaving.
        if (conversationIsFresh(st, i) && !sessionHasDraft(st.conversations[i].id)) continue;
        if (!writtenIds.insert(st.conversations[i].id).second) continue;
        if (i == st.activeConv) activeId = st.conversations[i].id;
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
            // reload it means the app died (or was killed) mid-run — see parseChatSessionJson.
            if (!m.fromUser && m.running) entry["run"] = true;
            if (m.ts > 0.0) entry["ts"] = m.ts;   // send/receive time for the in-bubble age label
            if (!m.fromUser && !m.model.empty()) entry["m"] = m.model;
            if (m.copilotMode >= 0) entry["cm"] = m.copilotMode;
            // Error card fields — the humanized headline and the raw failure text (capped like
            // an activity detail). Without these a reloaded error turn loses its guidance.
            if (!m.errorHuman.empty()) entry["eh"] = m.errorHuman;
            if (!m.errorFull.empty())
                entry["ef"] = m.errorFull.size() > 8000u
                                  ? m.errorFull.substr(0, 7999) + "\xe2\x80\xa6"
                                  : m.errorFull;
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
        // Optional document LINK (reference only — never an ownership key).
        if (!st.conversations[i].docLink.empty())
            convObj["doc"] = st.conversations[i].docLink;
        convObj["scope"] = st.conversations[i].scopeClass;  // rail color-code (-1 = unassessed)
        if (st.conversations[i].archived) convObj["archived"] = true;
        // Every session owns its agent-execution DAG (the live store is conversation-keyed) —
        // save each one as a session artifact so the workflow trace survives a reopen.
        nlohmann::json dag = ui::agentDagToJson(st.conversations[i].id);
        if (dag.is_object() && dag.contains("nodes") && !dag["nodes"].empty())
            convObj["agent_dag"] = std::move(dag);
        const int lct = (i == st.activeConv) ? st.lastContextTokens
                                             : st.conversations[i].lastContextTokens;
        if (lct > 0) convObj["last_context_tokens"] = lct;
        const auto& qp = (i == st.activeConv) ? st.queuedPrompts
                                              : st.conversations[i].queuedPrompts;
        if (!qp.empty()) convObj["queued_prompts"] = qp;
        sessions.push_back(convObj);
        order.push_back(st.conversations[i].id);
        // One transcript file per session — durable atomic write per file.
        if (atomicWriteFileDurable(chatSessionFilePath(st.conversations[i].id), convObj.dump(1)))
            s_pruneableSessionStems.insert(chatSessionFileStem(st.conversations[i].id));
    }
    nlohmann::json index = {{"version", 2}, {"active", activeId}, {"order", order}};
    try {
        // Durable atomic write (tmp + fsync + rename + dir fsync) — a hard crash mid-run must
        // never be able to leave a truncated/empty transcript behind.
        if (!atomicWriteFileDurable(chatSessionsIndexPath(), index.dump(1))) return;
        // Prune transcripts of sessions that no longer exist — but ONLY stems this process
        // has verifiably held live (loaded successfully or written by us: see
        // s_pruneableSessionStems). A file we cannot vouch for — crash-orphaned, corrupt at
        // load time, or written by a second app instance with a different resident set — is
        // user data: KEEP it. The dir is the store, but never at the cost of silent
        // destruction of unverifiable history.
        std::set<std::string> keep;
        for (const auto& id : order) keep.insert(chatSessionFileStem(id.get<std::string>()));
        std::error_code ec;
        for (const auto& de : fs::directory_iterator(chatSessionsDir(), ec)) {
            if (!de.is_regular_file(ec)) continue;
            const std::string name = de.path().filename().string();
            if (name == "index.json" || name.size() < 6 ||
                name.compare(name.size() - 5, 5, ".json") != 0)
                continue;
            std::string stem = name.substr(0, name.size() - 5);
            if (keep.count(stem) || !s_pruneableSessionStems.count(stem)) continue;
            fs::remove(de.path(), ec);
            if (!ec) s_pruneableSessionStems.erase(stem);
        }
        appstatePersistSync();  // WASM: flush IDBFS; no-op on desktop
#ifdef __EMSCRIPTEN__
        // Push the whole flat store to the shared workspace (async; never blocks UI).
        nlohmann::json root = {{"version", 2}, {"active", activeId}, {"sessions", sessions}};
        wasmPutSharedChat(root);
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

// Parse ONE persisted session object into the resident set (APPEND). docLink is the
// session's optional document reference: the v2 loader passes its "doc" field; the v1
// migration stamps the bucket key the session was folded out of. Returns false when the
// session carried no content (fresh sessions are never resurrected from disk).
static bool parseChatSessionJson(AppState& st, const nlohmann::json& jc,
                                 const std::string& docLink) {
    {
        Conversation c;
        c.id = jc.value("id", newConvId(st));
        c.title = jc.value("title", std::string("Chat"));
        c.docLink = docLink;
        c.scopeClass = jc.value("scope", -1);  // legacy sessions omit the key
        c.archived = jc.value("archived", false);
        // This session's own saved execution trace, straight into its conversation-keyed store
        // (no active/parked special case — switching sessions just reads the other tree).
        if (jc.contains("agent_dag")) ui::agentDagFromJson(c.id, jc["agent_dag"]);
        for (const auto& m : jc.value("msgs", nlohmann::json::array())) {
            ChatMsg cm;
            cm.fromUser = m.value("u", false);
            cm.text = m.value("t", std::string());
            cm.errKind = m.value("e", 0);
            if (!cm.fromUser) cm.model = m.value("m", std::string());
            cm.copilotMode = m.value("cm", -1);  // -1 when absent (legacy sessions)
            cm.errorHuman = m.value("eh", std::string());
            cm.errorFull = m.value("ef", std::string());
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
            // Crash recovery: a turn persisted as still-running never got a result. This session
            // was NOT already resident (guaranteed by the caller — see loadChatHistory),
            // so a "run":true marker here can ONLY be a leftover from a run that died with a
            // PREVIOUS process — never a live navigation, which now keeps a running conversation
            // resident (parked, still streaming) instead of reloading it from disk over itself.
            if (!cm.fromUser && m.value("run", false)) {
                cm.resultPartial = true;
                const char* note = "(Interrupted \xe2\x80\x94 the app was closed while this run "
                                   "was still in progress. Nothing after this point was saved.)";
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
        // Fresh sessions are never kept (same rule as persist) — drop legacy empty entries
        // left on disk by older builds instead of resurrecting them.
        if (c.msgs.empty() && c.queuedPrompts.empty()) return false;
        st.conversations.push_back(std::move(c));
    }
    return true;
}

// Defined below (file-local): swaps the newly-selected conversation's parked msgs/draft back
// into the live st.chat view. Forward-declared here for loadChatHistory's restore path.
static void restoreActiveConversation(AppState& st);


// Load the WHOLE session store once (startup / first copilot frame). Transcripts live as
// one file per session under <Documents>/Chudware/Sessions (index.json = envelope). On first
// run the legacy single-file appdata store is imported — v2 flat ({sessions:[…]}) or v1
// per-document buckets (each folded session keeps its bucket key as its doc LINK) — and
// written through to the new store on the next persist. Sessions are document-independent,
// so there is no per-document load path anymore; the WASM shared-store merge is the only
// re-entry.
void loadChatHistory(AppState& st) {
    // Park + drop everything resident — this runs before any session work has happened
    // (or from the WASM merge, which accepts a rebuild).
    parkActiveConversation(st);
    freeChatTextures(st.chat);
    st.chat.clear();
    st.lastContextTokens = 0;
    st.queuedPrompts.clear();
    for (auto& c : st.conversations) freeChatTextures(c.msgs);
    st.conversations.clear();
    st.activeConv = -1;

    bool migratedLegacy = false;
    bool recoveredOrphan = false;
    std::string activeId;
    std::set<std::string> seen;  // ids loaded this run — dup guard + orphan-sweep skip set
    auto noteLoaded = [&] {  // record a successfully parsed session as prunable + seen
        const std::string& id = st.conversations.back().id;
        seen.insert(id);
        s_pruneableSessionStems.insert(chatSessionFileStem(id));
    };
    // Per-session transcript store: index.json (order + active id) + <id>.json transcripts.
    {
        nlohmann::json index = nlohmann::json::object();
        {
            std::ifstream in(chatSessionsIndexPath());
            if (in) { try { in >> index; } catch (...) {} }
        }
        if (index.is_object() && index.contains("order") && index["order"].is_array()) {
            activeId = index.value("active", std::string());
            bool droppedDup = false;
            for (const auto& jid : index["order"]) {
                if (!jid.is_string()) continue;
                if (!seen.insert(jid.get<std::string>()).second) { droppedDup = true; continue; }
                nlohmann::json js;
                {
                    std::ifstream sf(chatSessionFilePath(jid.get<std::string>()));
                    if (sf) { try { sf >> js; } catch (...) { continue; } }
                }
                // A corrupt transcript is skipped here (this one file only — later sessions
                // still load) and, crucially, NOT recorded in s_pruneableSessionStems, so
                // the prune step can never destroy it: it stays on disk for recovery.
                if (js.is_object() &&
                    parseChatSessionJson(st, js, js.value("doc", std::string())))
                    s_pruneableSessionStems.insert(
                        chatSessionFileStem(st.conversations.back().id));
            }
            if (droppedDup) markChatHistoryDirty();  // rewrite a clean order on next persist
        }
    }
    // Recovery sweep: a transcript file on disk that the index does NOT list is still user
    // data — a crash between the per-session write and the index write (or a failed index
    // write, which early-returns persistChatHistory) used to orphan it forever, and the old
    // prune step would then have DELETED it. Adopt any parseable orphan into the resident
    // set and mark dirty so the next persist rewrites an index that lists it. Unparseable
    // files are left untouched on disk (never prunable — see above).
    {
        namespace fs = std::filesystem;
        std::error_code ec;
        for (const auto& de : fs::directory_iterator(chatSessionsDir(), ec)) {
            if (!de.is_regular_file(ec)) continue;
            const std::string name = de.path().filename().string();
            if (name == "index.json" || name.size() < 6 ||
                name.compare(name.size() - 5, 5, ".json") != 0)
                continue;
            nlohmann::json js;
            {
                std::ifstream sf(de.path());
                if (sf) { try { sf >> js; } catch (...) { continue; } }
            }
            if (!js.is_object()) continue;
            const std::string id = js.value("id", std::string());
            if (id.empty() || seen.count(id)) continue;
            if (parseChatSessionJson(st, js, js.value("doc", std::string()))) {
                noteLoaded();
                recoveredOrphan = true;
            }
        }
    }
    if (st.conversations.empty()) {
        // First run on the new store: import the legacy single-file history.
        nlohmann::json root = nlohmann::json::object();
        {
            std::ifstream in(chatHistoryPath());
            if (in) { try { in >> root; } catch (...) {} }
        }
        if (root.is_object() && root.contains("sessions") && root["sessions"].is_array()) {
            if (activeId.empty()) activeId = root.value("active", std::string());
            for (const auto& jc : root["sessions"]) {
                if (!jc.is_object()) continue;
                if (parseChatSessionJson(st, jc, jc.value("doc", std::string()))) noteLoaded();
                migratedLegacy = true;
            }
        } else if (root.is_object()) {
            // v1 migration: every top-level key was a document key owning {active, conversations}.
            for (auto it = root.begin(); it != root.end(); ++it) {
                if (!it.value().is_object()) continue;
                for (const auto& jc : it.value().value("conversations", nlohmann::json::array())) {
                    if (!jc.is_object()) continue;
                    if (parseChatSessionJson(st, jc, it.key())) noteLoaded();
                    migratedLegacy = true;
                }
            }
        }
    }
    if (st.conversations.empty()) {
        ensureConversations(st);  // seeds "Chat 1" linked to the current document
    } else {
        st.activeConv = 0;
        if (!activeId.empty())
            for (int i = 0; i < (int)st.conversations.size(); ++i)
                if (st.conversations[i].id == activeId) { st.activeConv = i; break; }
        restoreActiveConversation(st);
    }
    restorePromptDraft(st);
    // An imported legacy store is written through to the per-session transcripts on the
    // next debounced persist. A recovered orphan likewise needs the index rewritten so it
    // is listed (and therefore reachable on every future load) again.
    if (migratedLegacy || recoveredOrphan) markChatHistoryDirty();
}

#ifdef __EMSCRIPTEN__
// Count messages in one persisted session object — merge tie-break.
static int wasmChatSessionMsgCount(const nlohmann::json& jc) {
    if (!jc.is_object()) return 0;
    const auto& msgs = jc.value("msgs", nlohmann::json::array());
    return msgs.is_array() ? (int)msgs.size() : 0;
}

// Fire-and-forget async PUT of the whole flat v2 store — copy of fs_backend saveRecents
// pattern. Heap-owns the payload until the fetch callback frees it. Never
// EMSCRIPTEN_FETCH_SYNCHRONOUS.
static void wasmPutSharedChat(const nlohmann::json& root) {
    if (!root.is_object()) return;
    const std::string payload = root.dump();
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

// Merge the server store into the local IDBFS chat_history.json, v2 flat shape.
// Rule: per-session id — server wins for sessions local lacks; when both have a session,
// prefer more messages. The resident set is rebuilt via loadChatHistory only when the merge
// actually changed the disk store (accepted: WASM merge is a startup-time path).
static void wasmApplySharedChatPending(AppState& st) {
    if (!s_chatShareHasPending) return;
    s_chatShareHasPending = false;
    nlohmann::json server = std::move(s_chatSharePending);
    s_chatSharePending = nlohmann::json::object();
    if (!server.is_object() || !server.contains("sessions") || !server["sessions"].is_array())
        return;

    namespace fs = std::filesystem;
    // Local store: index.json order + per-session transcript files.
    nlohmann::json localSessions = nlohmann::json::array();
    std::string localActive;
    {
        nlohmann::json index = nlohmann::json::object();
        {
            std::ifstream in(chatSessionsIndexPath());
            if (in) {
                try {
                    in >> index;
                } catch (...) {
                    index = nlohmann::json::object();
                }
            }
        }
        if (index.is_object() && index.contains("order") && index["order"].is_array()) {
            localActive = index.value("active", std::string());
            for (const auto& jid : index["order"]) {
                if (!jid.is_string()) continue;
                nlohmann::json js;
                {
                    std::ifstream sf(chatSessionFilePath(jid.get<std::string>()));
                    if (sf) { try { sf >> js; } catch (...) { continue; } }
                }
                if (js.is_object()) localSessions.push_back(std::move(js));
            }
        }
    }

    bool dirty = false;
    for (const auto& ss : server["sessions"]) {
        if (!ss.is_object()) continue;
        const std::string sid = ss.value("id", std::string());
        if (sid.empty()) continue;
        bool found = false;
        for (auto& ls : localSessions) {
            if (!ls.is_object() || ls.value("id", std::string()) != sid) continue;
            found = true;
            if (wasmChatSessionMsgCount(ss) > wasmChatSessionMsgCount(ls)) {
                ls = ss;
                dirty = true;
            }
            break;
        }
        if (!found) {
            localSessions.push_back(ss);
            dirty = true;
        }
    }
    if (!dirty) return;

    try {
        fs::create_directories(chatSessionsDir());
        nlohmann::json order = nlohmann::json::array();
        for (const auto& ls : localSessions) {
            const std::string id = ls.value("id", std::string());
            if (id.empty()) continue;
            order.push_back(id);
            if (!atomicWriteFileDurable(chatSessionFilePath(id), ls.dump(1))) return;
        }
        nlohmann::json index = {{"version", 2}, {"active", localActive}, {"order", order}};
        if (!atomicWriteFileDurable(chatSessionsIndexPath(), index.dump(1))) return;
        appstatePersistSync();
    } catch (...) {
        return;
    }
    loadChatHistory(st);
}
#endif

// Relink sweep: when a document gains a saved file path, any session LINKED to it via
// doc:<id> has its link rewritten to file:<path> — resident sessions and the v2 store's
// "doc" fields. Links are metadata: nothing moves, nothing merges, and a session whose
// document is never saved simply keeps its doc:<id> link. Gated on a per-frame
// docId/path signature so the disk scan only runs on change.
static void relinkSavedDocSessions(AppState& st) {
    std::unordered_map<std::string, std::string> savedDocs;
    std::string sig;
    for (const auto& t : st.docTabs) {
        if (t.docId.empty() || t.path.empty()) continue;
        savedDocs[t.docId] = t.path;
        sig += t.docId;
        sig += '=';
        sig += t.path;
        sig += ';';
    }
    static std::string s_relinkSig;
    if (sig == s_relinkSig) return;
    s_relinkSig = sig;
    if (savedDocs.empty()) return;

    // 1) Resident sessions: rewrite the link only.
    bool relinked = false;
    for (auto& c : st.conversations) {
        if (c.docLink.rfind("doc:", 0) != 0) continue;
        auto it = savedDocs.find(c.docLink.substr(4));
        if (it == savedDocs.end()) continue;
        c.docLink = "file:" + it->second;
        relinked = true;
    }

    // 2) On-disk transcripts: rewrite "doc" fields (a legacy single-file store is left
    //    alone — loadChatHistory imports it and the next persist writes through).
    namespace fs = std::filesystem;
    std::error_code ec;
    for (const auto& de : fs::directory_iterator(chatSessionsDir(), ec)) {
        if (!de.is_regular_file(ec)) continue;
        const std::string name = de.path().filename().string();
        if (name == "index.json" || name.size() < 6 ||
            name.compare(name.size() - 5, 5, ".json") != 0)
            continue;
        nlohmann::json js;
        {
            std::ifstream in(de.path());
            if (in) { try { in >> js; } catch (...) { continue; } }
        }
        if (!js.is_object()) continue;
        const std::string doc = js.value("doc", std::string());
        if (doc.rfind("doc:", 0) != 0) continue;
        auto it = savedDocs.find(doc.substr(4));
        if (it == savedDocs.end()) continue;
        js["doc"] = "file:" + it->second;
        atomicWriteFileDurable(de.path().string(), js.dump(1));
    }
    if (relinked) markChatHistoryDirty();
}

// Per-frame chat/doc sync (called each frame). Sessions are document-independent now: a
// document switch NEVER loads/parks/replaces the session set — it only refreshes
// st.chatDocKey (the current document's key, for run launches + prompt drafts), runs the
// relink sweep, and kicks the one-time startup load.
void syncChatToDoc(AppState& st) {
    relinkSavedDocSessions(st);
#ifdef __EMSCRIPTEN__
    wasmKickSharedChatFetch();       // once: async GET full shared store
    wasmApplySharedChatPending(st);  // merge when the GET lands
#endif
    static bool s_historyLoaded = false;
    if (!s_historyLoaded) {
        s_historyLoaded = true;
        loadChatHistory(st);
    }
    st.chatDocKey = convDocKey(st);
    ensureConversations(st);
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

// A session with nothing in it: no messages, no queued prompts, no live run. Mirrors the
// "never autosave an empty part" rule (featureTreeSaveDocToPath) — fresh sessions are
// disposable and must never accumulate in the list or on disk.
static bool conversationIsFresh(const AppState& st, int idx) {
    if (idx < 0 || idx >= (int)st.conversations.size()) return false;
    const Conversation& c = st.conversations[idx];
    const bool active = (idx == st.activeConv);
    const std::vector<ChatMsg>& msgs = active ? st.chat : c.msgs;
    const auto& queued = active ? st.queuedPrompts : c.queuedPrompts;
    // The composer's unsent draft is content too — a session the user is typing into is
    // not fresh.
    if (active && st.prompt[0] != 0) return false;
    return msgs.empty() && queued.empty() && !st.convRunning(c.id);
}

// Drop the ACTIVE conversation when it is still fresh (see above), fixing up activeConv.
// Returns true when it erased — callers then skip parking (there was nothing to park).
static bool discardActiveConversationIfFresh(AppState& st) {
    if (!conversationIsFresh(st, st.activeConv)) return false;
    freeChatTextures(st.chat);
    st.chat.clear();
    st.lastContextTokens = 0;
    st.queuedPrompts.clear();
    st.conversations.erase(st.conversations.begin() + st.activeConv);
    st.activeConv = -1;  // callers set the new active index right after
    return true;
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

// True if the CURRENTLY VIEWED conversation's own turn is active — resolved from its JobState
// (st.runs) for job-based runs, plus a fallback scan of st.chat's own trailing message for
// one-shot RPC turns (agent_ask_v2 / agent_simulate_v1) that never touch a JobState at all.
// Never an ambient scan of an unrelated conversation, so a background run never makes the
// viewed (idle) conversation look busy.
bool activeConversationRunning(AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return false;
    if (st.convRunning(st.conversations[st.activeConv].id)) return true;
    if (!st.chat.empty()) {
        const ChatMsg& b = st.chat.back();
        if (!b.fromUser && b.running) return true;
    }
    return false;
}

void newConversation(AppState& st) {
    ensureConversations(st);
    // A fresh outgoing session is replaced, not parked — "+ New session" from an untouched
    // chat must not accumulate "Chat 1, Chat 2, …" empties in the list or on disk.
    if (!discardActiveConversationIfFresh(st))
        parkActiveConversation(st);  // force-saves old draft
    Conversation c;
    c.id = newConvId(st);
    c.title = "Chat " + std::to_string((int)st.conversations.size() + 1);
    c.docLink = convDocKey(st);  // link to the current document — reference only
    st.conversations.push_back(std::move(c));
    st.activeConv = (int)st.conversations.size() - 1;
    st.showSessionsPicker = false;
    clearComposerState(st);
    clearPromptDraft(st);  // new empty session has no draft
    // No persist: a fresh session carries no state worth writing. It reaches disk with the
    // first real message via the dirty-flag path (maybePersistChatHistoryDebounced).
}

void switchConversation(AppState& st, int idx) {
    if (idx == st.activeConv || idx < 0 || idx >= (int)st.conversations.size()) return;
    // Same fresh-session rule as newConversation: never park an empty session.
    const int oldActive = st.activeConv;
    if (!discardActiveConversationIfFresh(st))
        parkActiveConversation(st);  // force-saves draft + parks chat (incl. running msgs)
    else if (idx > oldActive)
        --idx;  // erase shifted the target down
    st.activeConv = idx;
    restoreActiveConversation(st);  // restores chat + draft for target
    // The incoming log is a different document: without this the outgoing conversation's
    // scrolled-up state (and its stale scroll offset) carries over and new streaming replies
    // silently never scroll into view.
    chatLogResetStick();
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
    if (!closingActive) {
        if (idx < st.activeConv) --st.activeConv;
        persistChatHistory(st);
        return;
    }
    // Hop to the nearest remaining session (prefer later indices); if none are left,
    // start a fresh blank session so the composer never lands on a void.
    int target = -1;
    for (int j = 0; j < (int)st.conversations.size(); ++j)
        if (!st.conversations[j].archived) { target = j; break; }
    if (target < 0) {
        st.activeConv = -1;
        ensureConversations(st);   // fresh blank session, linked to the current document
    } else {
        st.activeConv = target;
    }
    restoreActiveConversation(st);
    persistChatHistory(st);
}

// Archive (or unarchive) a session in place: unlike closeConversation, the transcript and
// all history stay — the session just drops out of the default Sessions list until "Show
// archived" is toggled on. Archiving the ACTIVE session hops to the nearest non-archived
// session (preferring later indices, matching closeConversation's "next" bias); if none are
// left, starts a fresh session so the composer never lands on a hidden conversation.
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
