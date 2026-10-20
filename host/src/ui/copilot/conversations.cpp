// Copilot conversations + persistence (T-RF6 split of legacy_chat.cpp): conversation tabs
// (ensure/new/switch/close, park/restore around doc switches), chat-history and prompt-draft
// persistence to %APPDATA%/Chudware/*.json (incl. the WASM shared-workspace chat sync), and the
// per-frame chat-triggered OpenFOAM CFD poll. The slash commands that mutate sessions live
// with the send path in composer.cpp.

#include "copilot_internal.h"
#include "ui/clipboard_image.h"
#include "ui/copilot/inline_image_refs.h"

#include "ui/ai/agent_dag.h"  // ui::agentDagToJson / agentDagFromJson — persist the live
                              // agent-execution DAG with the active session's artifacts.

#include "conv_ids.h"  // session-id allocation (GL-free TU, unit-tested in host/tests)
#include "ui/chudcode_session_actions.h"  // per-conversation Chudcode session link (GL-free TU)
#include "core/jobs.h"
#include "core/fs_backend.h"
#include <algorithm>
#include <cctype>
#include <filesystem>
#include <cerrno>
#include <map>
#include <memory>
#include "chat_message_snapshot.h"

// Resolve the live message vector for a conversation id — see core/app_state.h for the
// contract. Global scope (not ui::) to match its app_state.h declaration.
std::vector<ChatMsg>* conversationMsgs(AppState& st, const std::string& convId) {
    if (convId.empty()) return nullptr;
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size() &&
        st.conversations[st.activeConv].id == convId)
        return &st.chat;
    for (auto& c : st.conversations)
        if (c.id == convId) return &c.msgs;
    return nullptr;
}

#if defined(_WIN32)
#include <fcntl.h>
#include <io.h>
#include <sys/stat.h>
#elif !defined(__EMSCRIPTEN__)
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
static bool fsyncPath(const std::string& p, bool isDir) {
#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
    const int fd = ::open(p.c_str(), isDir ? (O_RDONLY | O_DIRECTORY) : O_RDONLY);
    if (fd < 0) return false;
    int rc;
    do { rc = ::fsync(fd); } while (rc < 0 && errno == EINTR);
    ::close(fd);
    return rc == 0;
#else
    (void)p;
    (void)isDir;
    return true;
#endif
}

// A rename may succeed even when its directory flush fails. Keep that distinction private:
// the writer must still own the visible file so a later deletion can prune it on retry.
static bool atomicWriteFileDurableImpl(const std::string& path, const std::string& data,
                                       bool* replaced) {
    if (replaced) *replaced = false;
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
        if (!fsyncPath(tmp, /*isDir=*/false)) return false;
        std::error_code ec;
        fs::rename(tmp, path, ec);
        if (ec) {
#if defined(_WIN32)
            // Windows sometimes needs remove-then-rename; last-resort non-atomic.
            fs::remove(path, ec);
            ec.clear();
            fs::rename(tmp, path, ec);
#endif
            if (ec) return false;
        }
        if (replaced) *replaced = true;
        const fs::path parent = fs::path(path).parent_path();
        if (!parent.empty() && !fsyncPath(parent.string(), /*isDir=*/true)) return false;
        return true;
    } catch (...) {
        return false;
    }
}

bool atomicWriteFileDurable(const std::string& path, const std::string& data) {
    return atomicWriteFileDurableImpl(path, data, nullptr);
}

}  // namespace ui
#ifndef __EMSCRIPTEN__
#include "chat_persistence.h"
#endif
namespace ui {
#ifndef __EMSCRIPTEN__
static chat_persistence::Writer& chatWriter() {
    static chat_persistence::Writer writer;
    return writer;
}
static void reportChatSaveFailure(AppState& st) {
    const std::string error = chatWriter().takeError();
    if (!error.empty()) {
        st.addLog(error);
        markChatHistoryDirty();
    }
}
#endif

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
// Scripted screenshot scenarios (CHUDWARE_SHOT) seed, replay and persist conversations. They
// must never write into the user's real Sessions folder or prompt drafts, so unless the run
// names an explicit CHUDWARE_SESSIONS_DIR, a shot gets its own store under scratch/, one per
// scenario name, emptied when the process first resolves it so every run starts identically.
static std::string shotChatStoreDir() {
    static const std::string cached = [] {
        const char* shot = std::getenv("CHUDWARE_SHOT");
        if (!shot || !*shot) return std::string();
        const char* repo = std::getenv("CHUDWARE_REPO");
        const std::string dir =
            std::string(repo && *repo ? repo : ".") + "/scratch/shot_sessions/" + shot;
        std::error_code ec;
        std::filesystem::remove_all(dir, ec);
        std::filesystem::create_directories(dir, ec);
        return dir;
    }();
    return cached;
}
static std::string promptDraftsPath() {
    const std::string shotDir = shotChatStoreDir();
    if (!shotDir.empty() && !std::getenv("CHUDWARE_SESSIONS_DIR")) return shotDir + "/prompt_drafts.json";
    return appdataAppDir() + "/prompt_drafts.json";
}

// Session transcripts live as ONE FILE PER SESSION under <Documents>/Chudware/Sessions
// (override: CHUDWARE_SESSIONS_DIR) — human-browsable, next to Bug-Reports. index.json is
// the v2 envelope (active id + session order); <id>.json is one full session object.
// The legacy appdata chat_history.json (single-file store) is imported on first load.
std::string chatSessionsDir() {
    static const std::string cached = [] {
        const char* env = std::getenv("CHUDWARE_SESSIONS_DIR");
        if (env && *env) return std::string(env);
        const std::string shotDir = shotChatStoreDir();
        if (!shotDir.empty()) return shotDir;
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
static std::string chatSubagentsDirPath(const std::string& id) {
    return chatSessionsDir() + "/" + chatSessionFileStem(id) + ".subagents";
}
static std::string chatRawLogPath(const std::string& id) {
    return chatSessionsDir() + "/" + chatSessionFileStem(id) + ".raw.jsonl";
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

// Every field, so a restored chip still carries its engine selector, owner and pick ids.
static nlohmann::json serializePendingContextRefs(const AppState& st) {
    return core::contextRefsToJson(st.pendingContextRefs);
}

// Snapshot the composer (text + attachment paths + context refs) for prompt_drafts.json.
static nlohmann::json promptDraftObject(const AppState& st) {
    nlohmann::json o = nlohmann::json::object();
    o["text"] = composerText(st);
    o["attachments"] = st.pendingAttachments;
    o["inline_images"] = st.pendingInlineImagePaths;
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

#ifdef __EMSCRIPTEN__
// Stable fingerprint for debounce (text + paths + ref ids).
static std::string promptDraftFingerprint(const AppState& st) {
    return promptDraftObject(st).dump();
}
#endif

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
    composerResetInputState(st);

    st.pendingAttachments.clear();
    st.pendingInlineImagePaths.clear();
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
    if (d.contains("inline_images") && d["inline_images"].is_array()) {
        // Keep order, duplicates and empty detached slots: #N indexes this exact roster.
        for (const auto& image : d["inline_images"])
            st.pendingInlineImagePaths.push_back(image.is_string()
                ? image.get<std::string>() : std::string());
    }
    if (!d.contains("inline_images")) {
        // Pre-numbered drafts stored pasted images among ordinary attachments. Promote once;
        // a modern draft with zero surviving references must NOT resurrect its deleted images.
        for (auto it = st.pendingAttachments.begin(); it != st.pendingAttachments.end();) {
            if (!ui::isImageAttachment(*it)) { ++it; continue; }
            if (!text.empty()) text += "\n";
            text += ui::copilot::inlineImageMarker(st.pendingInlineImagePaths.size());
            st.pendingInlineImagePaths.push_back(*it);
            it = st.pendingAttachments.erase(it);
        }
        if (st.prompt.size() < text.size() + 1) st.prompt.resize(text.size() + 64, 0);
        std::memset(st.prompt.data(), 0, st.prompt.size());
        std::memcpy(st.prompt.data(), text.data(), text.size());
        composerResetInputState(st);
    }
    if (d.contains("context_refs"))
        st.pendingContextRefs = core::contextRefsFromJson(d["context_refs"]);
}

#ifndef __EMSCRIPTEN__
static nlohmann::json& residentPromptDrafts() {
    static nlohmann::json root = [] {
        nlohmann::json loaded = nlohmann::json::object();
        std::ifstream in(promptDraftsPath());
        if (in) { try { in >> loaded; } catch (...) {} }
        return loaded.is_object() ? loaded : nlohmann::json::object();
    }();
    return root;
}
static void persistPromptDrafts(AppState& st, bool immediate) {
    chat_persistence::Snapshot snapshot;
    snapshot.files[promptDraftsPath()] =
        std::make_shared<const std::string>(residentPromptDrafts().dump(1));
    chatWriter().submit(std::move(snapshot));
    if (immediate && !chatWriter().flush()) markChatHistoryDirty();
    reportChatSaveFailure(st);
}
#endif

void savePromptDraft(AppState& st, bool force) {
#ifndef __EMSCRIPTEN__
    static std::string lastKey;
    static double lastWrite = -1e9;
    const std::string key = promptDraftKey(st);
    const nlohmann::json draft = promptDraftObject(st);
    const double now = ImGui::GetTime();
    if (!force && key == lastKey && now - lastWrite < 0.40) return;
    auto& root = residentPromptDrafts();
    bool changed = false;
    if (promptDraftIsEmpty(draft)) changed = root.erase(key) != 0;
    else {
        auto old = root.find(key);
        changed = old == root.end() || *old != draft;
        if (changed) root[key] = draft;
    }
    lastKey = key;
    lastWrite = now;
    if (changed) persistPromptDrafts(st, force);
    else if (force && !chatWriter().flush()) markChatHistoryDirty();
    reportChatSaveFailure(st);
#else
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
        if (!atomicWriteFileDurable(path, root.dump(1))) {
            s_lastKey.clear();
            s_lastSaved.clear();
            st.addLog("[chat] draft save failed; pending composer will be retried");
        }
    } catch (...) {
        s_lastKey.clear();
        s_lastSaved.clear();
        st.addLog("[chat] draft save failed; pending composer will be retried");
    }
#endif
}

void clearPromptDraft(AppState& st) {
#ifndef __EMSCRIPTEN__
    if (residentPromptDrafts().erase(promptDraftKey(st))) persistPromptDrafts(st, true);
#else
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
#endif
}

static void restorePromptDraft(AppState& st) {
    // Don't clobber a non-empty composer (user already typing) — session switch clears first.
    if (st.prompt.data() && st.prompt.data()[0] != '\0') return;
    // Also skip if chips already present (e.g. mid-frame attach) unless both empty.
    if (!st.pendingAttachments.empty() || !st.pendingInlineImagePaths.empty() ||
        !st.pendingContextRefs.empty()) return;
    try {
#ifndef __EMSCRIPTEN__
        const auto& root = residentPromptDrafts();
#else
        std::ifstream in(promptDraftsPath());
        if (!in) return;
        nlohmann::json root;
        in >> root;
#endif
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

// ---- project scoping -------------------------------------------------------------------------
// ONE path normalization for the whole copilot (project keys here, artifact paths in the
// Sessions panel): separators folded, lexically normalized, trailing slash dropped, and
// case-folded on Windows.
std::filesystem::path lexicalPath(std::string_view raw) {
    std::string path(raw);
    std::replace(path.begin(), path.end(), '\\', '/');
    return std::filesystem::path(path).lexically_normal();
}

std::string comparablePath(const std::filesystem::path& path) {
    std::string result = path.generic_string();
    const std::string root = path.root_path().generic_string();
    while (result.size() > root.size() && !result.empty() && result.back() == '/')
        result.pop_back();
#ifdef _WIN32
    std::transform(result.begin(), result.end(), result.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
#endif
    return result;
}

std::string convProjectKey(std::string_view rawDir) {
    if (rawDir.empty()) return std::string();
#ifdef __EMSCRIPTEN__
    std::string relative, err;
    if (!chudware::workspaceRelativePath(std::string(rawDir), relative, err)) return {};
    return relative.empty() ? "." : relative;
#else
    return comparablePath(lexicalPath(rawDir));
#endif
}

// The project folder chat belongs to: the selected working directory (Explorer root), and
// nothing else. An open document — even one saved outside that folder — never changes it.
// A turn launch restamps a session to the workspace it actually ran in.
std::string convProjectDir(AppState& st) {
    return st.workspaceDir;
}

// Legacy fallback #1: the Chudcode session JSONL records the run's cwd on its session line
// (line 2). Bounded read — a fixed head of the file, and only COMPLETE lines inside it are
// parsed, so a multi-megabyte entry line can never be pulled into memory. A missing or
// damaged file is simply no answer.
static std::string chudcodeSessionCwd(const std::string& sessionFile) {
    if (sessionFile.empty()) return std::string();
    std::ifstream in(sessionFile, std::ios::binary);
    if (!in) return std::string();
    std::string head(64u * 1024u, '\0');
    in.read(head.data(), (std::streamsize)head.size());
    head.resize((size_t)in.gcount());
    size_t pos = 0;
    for (int i = 0; i < 4; ++i) {
        const size_t nl = head.find('\n', pos);
        if (nl == std::string::npos) break;   // truncated tail line: never parsed
        const std::string line = head.substr(pos, nl - pos);
        pos = nl + 1;
        if (line.empty()) continue;
        try {
            const nlohmann::json j = nlohmann::json::parse(line);
            if (!j.is_object() || !j.contains("cwd") || !j["cwd"].is_string()) continue;
            std::string cwd = j["cwd"].get<std::string>();
            if (!cwd.empty()) return cwd;
        } catch (...) {
            // Not JSON — keep scanning the remaining bounded lines.
        }
    }
    return std::string();
}

// Companion to chudcodeSessionCwd: the same session line carries the Chudcode session id
// ({"type":"session","version":3,"id":…,"cwd":…}). Same bounded-head discipline — only
// COMPLETE lines inside a fixed head are parsed, so a huge entry line is never read in.
// Empty means "not a Chudcode session file" (our own .raw.jsonl logs answer empty too).
static std::string chudcodeSessionIdOf(const std::string& sessionFile) {
    if (sessionFile.empty()) return std::string();
    std::ifstream in(sessionFile, std::ios::binary);
    if (!in) return std::string();
    std::string head(64u * 1024u, '\0');
    in.read(head.data(), (std::streamsize)head.size());
    head.resize((size_t)in.gcount());
    size_t pos = 0;
    for (int i = 0; i < 4; ++i) {
        const size_t nl = head.find('\n', pos);
        if (nl == std::string::npos) break;   // truncated tail line: never parsed
        const std::string line = head.substr(pos, nl - pos);
        pos = nl + 1;
        if (line.empty()) continue;
        try {
            const nlohmann::json j = nlohmann::json::parse(line);
            if (!j.is_object() || j.value("type", std::string()) != "session") continue;
            std::string id = j.value("id", std::string());
            if (!id.empty()) return id;
        } catch (...) {
            // Not JSON — keep scanning the remaining bounded lines.
        }
    }
    return std::string();
}

static bool artifactPathIsInProject(std::string_view rawPath,
                                    const std::filesystem::path& projectRoot,
                                    const std::string& projectKey) {
    if (rawPath.empty() || projectKey.empty()) return false;
    std::filesystem::path artifact = lexicalPath(rawPath);
    if (!artifact.is_absolute()) artifact = (projectRoot / artifact).lexically_normal();
    const std::string artifactKey = comparablePath(artifact);
    if (artifactKey == projectKey) return true;
    if (artifactKey.size() <= projectKey.size() ||
        artifactKey.compare(0, projectKey.size(), projectKey) != 0)
        return false;
    return projectKey.back() == '/' || artifactKey[projectKey.size()] == '/';
}

// Legacy fallback #2: a session that never recorded a project may still have produced a
// file/image/mesh under the CURRENT project root. Used ONCE, at load time, to fill
// projectDir — never per frame (that heuristic used to hide sessions from the list).
static bool hasProjectArtifact(const std::vector<ChatMsg>& msgs,
                               const std::filesystem::path& projectRoot,
                               const std::string& projectKey) {
    static constexpr std::string_view kMarker("\x1e" "ART\x1e");
    for (const ChatMsg& msg : msgs) {
        if (msg.fromUser) continue;
        for (const auto& activity : msg.activity) {
            const std::string_view detail(activity.detail);
            const size_t marker = detail.find(kMarker);
            if (marker == std::string_view::npos) continue;
            std::string_view packed = detail.substr(marker + kMarker.size());
            while (!packed.empty() && (packed.front() == '\r' || packed.front() == '\n'))
                packed.remove_prefix(1);
            while (!packed.empty()) {
                const size_t newline = packed.find('\n');
                std::string_view line = packed.substr(0, newline);
                if (!line.empty() && line.back() == '\r') line.remove_suffix(1);
                const size_t tab = line.find('\t');
                if (tab != std::string_view::npos) {
                    const std::string_view kind = line.substr(0, tab);
                    const std::string_view path = line.substr(tab + 1);
                    if ((kind == "file" || kind == "image" || kind == "mesh") &&
                        artifactPathIsInProject(path, projectRoot, projectKey))
                        return true;
                }
                if (newline == std::string_view::npos) break;
                packed.remove_prefix(newline + 1);
            }
        }
    }
    return false;
}

// Remembered active session PER PROJECT (index.json v3 "active_by_project"): opening the app
// under project X must land on the chat that was last active THERE, not on whatever was last
// active anywhere. Keyed by convProjectKey. Invariant: an entry only ever names a session whose
// OWN stored project has that key — the session on screen may be another project's (the user
// opened it explicitly), and it must never claim the current root's slot.
static std::map<std::string, std::string> s_activeByProject;

// The root key the active session was last reconciled against. loadChatHistory clears
// s_followReconciled so a (re)load — startup or the WASM shared-store merge — is reconciled
// like a root switch: the restored selection is whatever was on screen last time, possibly
// under another root.
static bool        s_followReconciled = false;
static std::string s_followKey;

// True when session `id` is resident and its OWN stored project has `key` — the only kind of
// session s_activeByProject may remember for that key.
static bool sessionOwnedByProject(const AppState& st, const std::string& id,
                                  const std::string& key) {
    for (const Conversation& c : st.conversations)
        if (c.id == id) return convProjectKey(c.projectDir) == key;
    return false;
}

// A run binds the session to the workspace actually sent to the worker. A session
// resumed under another project must also stop being the old project's remembered tab.
void bindActiveConversationProject(AppState& st, const std::string& workspace) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    Conversation& active = st.conversations[st.activeConv];
    const std::string previous = convProjectKey(active.projectDir);
    if (previous == convProjectKey(workspace)) return;
    const auto old = s_activeByProject.find(previous);
    if (old != s_activeByProject.end() && old->second == active.id)
        s_activeByProject.erase(old);
    active.projectDir = workspace;
    markChatHistoryDirty();
}

// Keep the id source ahead of an id we just took in from disk. st.convCounter starts at 0
// EVERY launch, so without this a second run handed out c1, c2, c3… straight on top of the
// c3/c7/c8 transcripts already on disk — and a colliding session was then silently dropped
// by persistChatHistory's writtenIds guard (the user's session "vanished" after a restart)
// or written over the older one's file (an archived "Knurled hex standoff" that actually
// held a mudbucket transcript). Ids must be unique for the life of the store, not the run.
static void noteConvIdSeen(AppState& st, const std::string& id) {
    st.convCounter = std::max(st.convCounter, ui::convIdOrdinal(id));
}

// A session id no resident conversation and no transcript on disk is using. The on-disk
// probe matters because a session can be archived out of the index yet still own its file.
std::string newConvId(AppState& st) {
    std::vector<std::string> taken;
    taken.reserve(st.conversations.size());
    for (const auto& c : st.conversations) taken.push_back(c.id);
    return ui::nextFreeConvId(
        st.convCounter, taken,
        [](const std::string& id, void*) {
            std::error_code ec;
            if (std::filesystem::exists(chatSessionFilePath(id), ec)) return true;
            ec.clear();
            return std::filesystem::exists(chatRawLogPath(id), ec);
        },
        nullptr);
}

// ---- session titles -------------------------------------------------------------------------
// A session starts as "Session N" and takes its name from its first prompt; a name the user
// typed (Rename) is never overwritten. N comes from a monotonic counter persisted in index.json
// ("session_seq", additive — older builds ignore it), so deleting a session can never make two
// live sessions share a number the way conversations.size()+1 did. The user-named flag rides
// in each session file as "title_user".
static int s_sessionSeq = 0;                  // highest "Session N" handed out
static std::set<std::string> s_userTitled;    // conversation ids the user named

// "Session 12" -> 12, anything else -> 0.
static int sessionTitleOrdinal(const std::string& t) {
    static const std::string kPrefix = "Session ";
    if (t.size() <= kPrefix.size() || t.compare(0, kPrefix.size(), kPrefix) != 0) return 0;
    int n = 0;
    for (size_t i = kPrefix.size(); i < t.size(); ++i) {
        if (!std::isdigit((unsigned char)t[i]) || n > 100000000) return 0;
        n = n * 10 + (t[i] - '0');
    }
    return n;
}

// A title nobody chose: empty, the placeholders, or the numbered defaults (current
// "Session N" and the legacy "Chat N").
static bool isDefaultSessionTitle(const std::string& t) {
    if (t.empty() || t == "Chat" || t == "Session") return true;
    if (sessionTitleOrdinal(t) > 0) return true;
    static const std::string kLegacy = "Chat ";
    if (t.size() <= kLegacy.size() || t.compare(0, kLegacy.size(), kLegacy) != 0) return false;
    return std::all_of(t.begin() + (long)kLegacy.size(), t.end(),
                       [](char ch) { return std::isdigit((unsigned char)ch) != 0; });
}

static std::string nextSessionTitle() { return "Session " + std::to_string(++s_sessionSeq); }

// Collapse whitespace runs (incl. newlines) to single spaces and trim both ends.
static std::string collapseWhitespace(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    bool pendingSpace = false;
    for (char ch : in) {
        if (std::isspace((unsigned char)ch)) { pendingSpace = !out.empty(); continue; }
        if (pendingSpace) { out.push_back(' '); pendingSpace = false; }
        out.push_back(ch);
    }
    return out;
}

// Cut to at most `maxChars` code points, preferring the last word boundary past the halfway
// mark, and mark the cut with an ellipsis. Never splits a UTF-8 sequence.
static std::string clipTitle(const std::string& s, size_t maxChars) {
    size_t chars = 0, cut = 0, lastSpace = std::string::npos;
    for (size_t i = 0; i < s.size(); ++i) {
        if (((unsigned char)s[i] & 0xC0) == 0x80) continue;  // continuation byte
        if (chars == maxChars) { cut = i; break; }
        if (s[i] == ' ' && chars >= maxChars / 2) lastSpace = i;
        ++chars;
    }
    if (cut == 0) return s;  // fits
    std::string out = s.substr(0, lastSpace != std::string::npos ? lastSpace : cut);
    while (!out.empty() && (out.back() == ' ' || out.back() == ',' || out.back() == '.' ||
                            out.back() == ':' || out.back() == ';'))
        out.pop_back();
    return out + "\xe2\x80\xa6";
}

// The title a prompt earns: the typed text only — the "[attached image: …]", "[attached: …]"
// and "[context: …]" marker lines composer.cpp appends to the user bubble are dropped.
static std::string sessionTitleFromPrompt(const std::string& prompt) {
    std::string typed;
    size_t pos = 0;
    while (pos <= prompt.size()) {
        size_t nl = prompt.find('\n', pos);
        if (nl == std::string::npos) nl = prompt.size();
        const std::string line = prompt.substr(pos, nl - pos);
        const size_t lead = line.find_first_not_of(" \t\r");
        const bool marker = lead != std::string::npos &&
                            (line.compare(lead, 16, "[attached image:") == 0 ||
                             line.compare(lead, 10, "[attached:") == 0 ||
                             line.compare(lead, 9, "[context:") == 0);
        if (!marker) { typed += line; typed += ' '; }
        pos = nl + 1;
    }
    return clipTitle(collapseWhitespace(typed), 40);
}

// Returns true when the title changed. Does not mark history dirty: persistChatHistory calls
// it on the snapshot it is about to write.
static bool applyAutoTitle(AppState& st, int idx) {
    if (idx < 0 || idx >= (int)st.conversations.size()) return false;
    Conversation& c = st.conversations[idx];
    if (s_userTitled.count(c.id) || !isDefaultSessionTitle(c.title)) return false;
    const std::vector<ChatMsg>& msgs = (idx == st.activeConv) ? st.chat : c.msgs;
    for (const ChatMsg& m : msgs) {
        if (!m.fromUser) continue;
        std::string t = sessionTitleFromPrompt(m.text);
        if (t.empty()) continue;  // attachment-only prompt: the next typed one names it
        c.title = std::move(t);
        return true;
    }
    return false;
}

void autoTitleConversation(AppState& st, int idx) {
    if (applyAutoTitle(st, idx)) markChatHistoryDirty();
}

bool renameConversation(AppState& st, int idx, const std::string& title) {
    if (idx < 0 || idx >= (int)st.conversations.size()) return false;
    Conversation& c = st.conversations[idx];
    const std::string clean = clipTitle(collapseWhitespace(title), 80);
    if (clean.empty()) {
        // Clearing the name hands it back to the automatic title.
        if (!s_userTitled.erase(c.id)) return false;
        c.title = nextSessionTitle();
        applyAutoTitle(st, idx);
    } else {
        if (clean == c.title && s_userTitled.count(c.id)) return false;
        c.title = clean;
        s_userTitled.insert(c.id);
    }
    markChatHistoryDirty();
    return true;
}

// Guarantee the CURRENTLY VIEWED conversation exists, adopting whatever is already in st.chat.
// Additive: never clears other resident conversations (other doc keys, or idle same-doc
// siblings) — those stay resident regardless of what the active view needs right now.
void ensureConversations(AppState& st) {
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size()) return;
    Conversation c;
    c.id = newConvId(st);
    c.title = nextSessionTitle();
    c.docLink = convDocKey(st);  // link to whatever document is current — reference only
    c.projectDir = convProjectDir(st);  // initial scope; a turn may run in another workspace
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
// ---- append-only, uncapped chat archive --------------------------------------------------
// <id>.json above remains the bounded fast-reload representation. This JSONL sibling is the
// recovery/archive representation: compact records, no caps, no wholesale rewrites.
static nlohmann::json rawActivityToJson(const ActivityItem& a) {
    return {
        {"stage", a.stage},
        {"title", a.title},
        {"detail", a.detail},
        {"reason", a.reason},
        {"status", a.status},
        {"durationMs", a.durationMs},
        {"tokensIn", a.tokensIn},
        {"tokensOut", a.tokensOut},
        {"opsCount", a.opsCount},
        {"expanded", a.expanded},
        {"missionHeader", a.missionHeader},
        {"rowKind", a.rowKind},
        {"ctxTokens", a.ctxTokens},
        {"streaming", a.streaming},
        {"userToggled", a.userToggled},
    };
}

static nlohmann::json rawPlanToJson(const PlanInfo& plan) {
    nlohmann::json parts = nlohmann::json::array();
    for (const auto& part : plan.parts)
        parts.push_back({{"name", part.name}, {"purpose", part.purpose},
                         {"dims", part.dims}, {"features", part.features}});
    return {
        {"present", plan.present},
        {"name", plan.name},
        {"summary", plan.summary},
        {"fit", plan.fit},
        {"parts", std::move(parts)},
    };
}

static nlohmann::json rawClarifyToJson(const ClarifyState& clarify) {
    nlohmann::json questions = nlohmann::json::array();
    for (const auto& q : clarify.questions) {
        size_t answerLen = 0;
        while (answerLen < q.answer.size() && q.answer[answerLen] != '\0') ++answerLen;
        nlohmann::json options = nlohmann::json::array();
        for (const auto& option : q.options)
            options.push_back({{"value", option.first}, {"label", option.second}});
        questions.push_back({
            {"key", q.key},
            {"ask", q.ask},
            {"unit", q.unit},
            {"suggested", q.suggested},
            {"answer", std::string(q.answer.data(), answerLen)},
            {"kind", q.kind},
            {"multi", q.multi},
            {"options", std::move(options)},
            {"targets", q.targets},
            {"selectedValues", q.selectedValues},
            {"selectedKind", q.selectedKind},
            {"pickArmed", q.pickArmed},
        });
    }
    return {
        {"awaiting", clarify.awaiting},
        {"active", clarify.active},
        {"request", clarify.request},
        {"questions", std::move(questions)},
        {"assumptions", clarify.assumptions},
        {"geometryPickQ", clarify.geometryPickQ},
    };
}

static nlohmann::json rawMessageToJson(const ChatMsg& m) {
    return {
        {"fromUser", m.fromUser},
        {"ts", m.ts},
        {"text", m.text},
        {"imagePaths", m.imagePaths},
        {"running", m.running},
        {"model", m.model},
        {"errKind", m.errKind},
        {"errorHuman", m.errorHuman},
        {"errorFull", m.errorFull},
        {"errFullOpen", m.errFullOpen},
        {"liveStatus", m.liveStatus},
        {"plan", rawPlanToJson(m.plan)},
        {"planState", planStateToJson(m.planState)},
        {"planStatePresent", m.planState.present},
        {"curMissionTask", m.curMissionTask},
        {"clarify", rawClarifyToJson(m.clarify)},
        {"asks", [&] {
             nlohmann::json asks = nlohmann::json::array();
             for (const auto& b : m.asks) asks.push_back(askBatchToJson(b));
             return asks;
         }()},
        {"planApprovePending", m.planApprovePending},
        {"planApproveRequest", m.planApproveRequest},
        {"planApproveExtras", m.planApproveExtras},
        {"planApproveReason", m.planApproveReason},
        {"resultPartial", m.resultPartial},
        {"copilotMode", m.copilotMode},
        {"cacheReadTok", m.cacheReadTok},
        {"cacheReadTotTok", m.cacheReadTotTok},
        {"cacheReadCalls", m.cacheReadCalls},
        {"contextRefs", core::contextRefsToJson(m.contextRefs)},
    };
}

static std::mutex s_chatRawFileMutex;
static std::unordered_map<std::string, long long> s_chatRawNextSeq;
static std::mutex s_chatRawOwnerMutex;
static std::unordered_map<const ChatMsg*, std::string> s_chatRawOwners;
static std::unordered_map<std::string, std::vector<std::string>> s_chatRawMessageSnapshots;

// Recover from the last valid compact line rather than a process-local counter. A torn final
// write is skipped and the preceding valid seq wins, so relaunch never restarts from 1.
static long long chatRawNextSeqFromDisk(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return 1;
    in.seekg(0, std::ios::end);
    std::streamoff pos = in.tellg();
    if (pos <= 0) return 1;
    std::string reversed;
    constexpr std::streamoff kChunk = 64 * 1024;
    while (pos > 0) {
        const std::streamoff take = std::min(pos, kChunk);
        pos -= take;
        in.seekg(pos, std::ios::beg);
        std::string chunk((size_t)take, '\0');
        in.read(chunk.data(), take);
        const std::streamsize got = in.gcount();
        for (std::streamsize i = got; i > 0; --i) {
            const char ch = chunk[(size_t)i - 1];
            if (ch != '\n') {
                if (ch != '\r' || !reversed.empty()) reversed.push_back(ch);
                continue;
            }
            if (reversed.empty()) continue;
            std::reverse(reversed.begin(), reversed.end());
            try {
                const nlohmann::json line = nlohmann::json::parse(reversed);
                if (line.is_object() && line.contains("seq") && line["seq"].is_number_integer())
                    return std::max(1LL, line["seq"].get<long long>() + 1);
                if (line.is_object() && line.value("kind", std::string()) == "header") return 1;
            } catch (...) {
            }
            reversed.clear();
        }
    }
    if (!reversed.empty()) {
        std::reverse(reversed.begin(), reversed.end());
        try {
            const nlohmann::json line = nlohmann::json::parse(reversed);
            if (line.is_object() && line.contains("seq") && line["seq"].is_number_integer())
                return std::max(1LL, line["seq"].get<long long>() + 1);
        } catch (...) {
        }
    }
    return 1;
}

static bool chatRawNeedsSeparator(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return false;
    in.seekg(0, std::ios::end);
    const std::streamoff size = in.tellg();
    if (size <= 0) return false;
    in.seekg(-1, std::ios::end);
    char last = '\0';
    in.get(last);
    return last != '\n';
}

static bool appendChatRawBytes(const std::string& path, const std::string& bytes) {
#if defined(_WIN32)
    const int fd = ::_open(path.c_str(), _O_WRONLY | _O_CREAT | _O_APPEND | _O_BINARY,
                           _S_IREAD | _S_IWRITE);
    if (fd < 0) return false;
    size_t offset = 0;
    while (offset < bytes.size()) {
        const unsigned int want =
            (unsigned int)std::min<size_t>(bytes.size() - offset, 0x7fffffffU);
        const int wrote = ::_write(fd, bytes.data() + offset, want);
        if (wrote <= 0) {
            ::_close(fd);
            return false;
        }
        offset += (size_t)wrote;
    }
    ::_close(fd);
    return true;
#elif defined(__EMSCRIPTEN__)
    std::ofstream out(path, std::ios::binary | std::ios::app);
    if (!out) return false;
    out.write(bytes.data(), (std::streamsize)bytes.size());
    return (bool)out;
#else
    const int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return false;
    size_t offset = 0;
    while (offset < bytes.size()) {
        const ssize_t wrote = ::write(fd, bytes.data() + offset, bytes.size() - offset);
        if (wrote <= 0) {
            ::close(fd);
            return false;
        }
        offset += (size_t)wrote;
    }
    ::close(fd);
    return true;
#endif
}

static void appendChatRawRecord(const std::string& conversationId, const char* kind,
                                const char* payloadKey, const nlohmann::json& payload) noexcept {
    if (conversationId.empty()) return;
    try {
        std::lock_guard<std::mutex> lock(s_chatRawFileMutex);
        namespace fs = std::filesystem;
        std::error_code ec;
        fs::create_directories(chatSessionsDir(), ec);
        if (ec) return;
        const std::string path = chatRawLogPath(conversationId);
        const bool empty = !fs::exists(path, ec) || (!ec && fs::file_size(path, ec) == 0);
        if (ec) return;
        auto [it, inserted] = s_chatRawNextSeq.emplace(path, 1);
        if (inserted) it->second = chatRawNextSeqFromDisk(path);
        const long long seq = it->second;
        std::string encoded;
        if (!empty && chatRawNeedsSeparator(path)) encoded.push_back('\n');
        if (empty) {
            const nlohmann::json header = {
                {"kind", "header"},
                {"schema", "chudware.chat.raw/1"},
                {"conversation_id", conversationId},
                {"created_ts", chatNowEpoch()},
            };
            encoded += header.dump();
            encoded.push_back('\n');
        }
        nlohmann::json record = {
            {"ts", chatNowEpoch()},
            {"seq", seq},
            {"kind", kind},
            {payloadKey, payload},
        };
        encoded += record.dump();
        encoded.push_back('\n');
        if (appendChatRawBytes(path, encoded)) ++it->second;
    } catch (...) {
        // The raw archive is strictly best-effort; chat operation success never depends on it.
    }
}

static std::string activeConversationId(const AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return {};
    return st.conversations[(size_t)st.activeConv].id;
}

static void bindChatRawOwners(const std::string& conversationId,
                              const std::vector<ChatMsg>& messages) noexcept {
    if (conversationId.empty()) return;
    try {
        std::lock_guard<std::mutex> lock(s_chatRawOwnerMutex);
        for (const auto& message : messages) s_chatRawOwners[&message] = conversationId;
    } catch (...) {
    }
}

void appendChatRawMessage(AppState& st, const ChatMsg& message) noexcept {
    try {
        const std::string conversationId = activeConversationId(st);
        if (conversationId.empty()) return;
        bindChatRawOwners(conversationId, st.chat);
        const nlohmann::json payload = rawMessageToJson(message);
        const std::string snapshot = payload.dump();
        appendChatRawRecord(conversationId, "message", "message", payload);
        size_t index = st.chat.size();
        for (size_t i = 0; i < st.chat.size(); ++i) {
            if (&st.chat[i] == &message) {
                index = i;
                break;
            }
        }
        if (index == st.chat.size()) return;
        std::lock_guard<std::mutex> lock(s_chatRawOwnerMutex);
        auto& snapshots = s_chatRawMessageSnapshots[conversationId];
        if (snapshots.size() <= index) snapshots.resize(index + 1);
        snapshots[index] = snapshot;
    } catch (...) {
        // Message insertion must remain successful even when raw serialization fails.
    }
}

static void archiveChangedChatMessages(const std::string& conversationId,
                                       const std::vector<ChatMsg>& messages,
                                       const std::vector<chat_persistence::MessageSnapshot>& previous) noexcept {
    try {
        bindChatRawOwners(conversationId, messages);
        for (size_t i = 0; i < messages.size(); ++i) {
            if (i < previous.size() &&
                chat_persistence::sameMessage(previous[i].source, messages[i], false)) continue;
            const nlohmann::json payload = rawMessageToJson(messages[i]);
            const std::string snapshot = payload.dump();
            bool changed = false;
            {
                std::lock_guard<std::mutex> lock(s_chatRawOwnerMutex);
                auto& snapshots = s_chatRawMessageSnapshots[conversationId];
                if (snapshots.size() <= i) snapshots.resize(i + 1);
                changed = snapshots[i] != snapshot;
                if (changed) snapshots[i] = snapshot;
            }
            if (changed) appendChatRawRecord(conversationId, "message", "message", payload);
        }
    } catch (...) {
        // The bounded session snapshot must still proceed when the archive cannot.
    }
}

static void seedChatRawSnapshots(const std::string& conversationId,
                                 const std::vector<ChatMsg>& messages) noexcept {
    try {
        bindChatRawOwners(conversationId, messages);
        std::lock_guard<std::mutex> lock(s_chatRawOwnerMutex);
        auto& snapshots = s_chatRawMessageSnapshots[conversationId];
        snapshots.clear();
        snapshots.reserve(messages.size());
        for (const auto& message : messages)
            snapshots.push_back(rawMessageToJson(message).dump());
    } catch (...) {
    }
}

ChatRawActivityCapture::ChatRawActivityCapture(ChatMsg& message) noexcept : message_(message) {
    try {
        {
            std::lock_guard<std::mutex> lock(s_chatRawOwnerMutex);
            const auto it = s_chatRawOwners.find(&message_);
            if (it == s_chatRawOwners.end()) return;
            conversationId_ = it->second;
        }
        before_.reserve(message_.activity.size());
        for (const auto& row : message_.activity)
            before_.push_back(rawActivityToJson(row).dump());
    } catch (...) {
        conversationId_.clear();
        before_.clear();
    }
}

ChatRawActivityCapture::~ChatRawActivityCapture() {
    if (conversationId_.empty()) return;
    try {
        std::unordered_map<std::string, int> unchanged;
        unchanged.reserve(before_.size());
        for (const auto& snapshot : before_) ++unchanged[snapshot];
        for (const auto& row : message_.activity) {
            const nlohmann::json payload = rawActivityToJson(row);
            const std::string snapshot = payload.dump();
            auto it = unchanged.find(snapshot);
            if (it != unchanged.end() && it->second > 0) {
                --it->second;
                continue;
            }
            appendChatRawRecord(conversationId_, "activity", "row", payload);
        }
    } catch (...) {
        // Activity folding must remain successful even when serialization or append fails.
    }
}


// Defined near the lifecycle functions below.
static bool conversationIsFresh(const AppState& st, int idx);

// Reuse exact immutable session snapshots; only changed messages are serialized. Desktop
// debounce submits to the ordered durable writer, while explicit callers retain a barrier.
static std::map<std::string, chat_persistence::SessionSnapshot> s_sessionSnapshots;
static void snapshotChatHistory(AppState& st, bool immediate) {
#ifndef __EMSCRIPTEN__
    chat_persistence::Snapshot snapshot;
    snapshot.history = true;
    snapshot.directory = chatSessionsDir();
    snapshot.indexPath = chatSessionsIndexPath();
    snapshot.owned = s_pruneableSessionStems;
#else
    (void)immediate;
    namespace fs = std::filesystem;
    std::error_code dirEc;
    fs::create_directories(chatSessionsDir(), dirEc);
    nlohmann::json sessions = nlohmann::json::array();
#endif
    nlohmann::json order = nlohmann::json::array();
    std::set<std::string> writtenIds;  // defensive: never write an id twice in one snapshot
    std::string activeId;
    // A PARKED session whose only content is an unsent composer draft (saved under its
    // docKey/id key in prompt_drafts.json) is not "fresh" — the session is the draft's only
    // anchor; dropping it strands the user's typed text in an unreachable drafts entry.
#ifndef __EMSCRIPTEN__
    const auto& drafts = residentPromptDrafts();
#else
    nlohmann::json drafts = nlohmann::json::object();
    {
        std::ifstream in(promptDraftsPath());
        if (in) { try { in >> drafts; } catch (...) { drafts = nlohmann::json::object(); } }
    }
#endif
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
    // Every write names still-untitled sessions from their first prompt — the one place titles
    // are derived, whichever path appended that prompt (send, queued drain, adoption, load).
    for (int i = 0; i < (int)st.conversations.size(); ++i) applyAutoTitle(st, i);
    for (int i = 0; i < (int)st.conversations.size(); ++i) {
        // Fresh sessions (no messages, no queue, no run, no draft) are never written — same
        // rule as empty parts never autosaving.
        if (conversationIsFresh(st, i) && !sessionHasDraft(st.conversations[i].id)) continue;
        // A duplicate id used to `continue` here, silently throwing the whole transcript
        // away — the exact data loss this guard was meant to prevent. Re-key the session
        // instead: it keeps its content, gets its own file, and the collision is logged.
        if (!writtenIds.insert(st.conversations[i].id).second) {
            const std::string clashed = st.conversations[i].id;
            st.conversations[i].id = newConvId(st);
            writtenIds.insert(st.conversations[i].id);
            st.addLog("[chat] duplicate session id '" + clashed + "' re-keyed to '" +
                      st.conversations[i].id + "' (transcript kept)");
            chudcodeSessionLinkRekey(clashed, st.conversations[i].id);
        }
        if (i == st.activeConv) activeId = st.conversations[i].id;
        auto& cached = s_sessionSnapshots[st.conversations[i].id];
        const auto& liveMsgs = i == st.activeConv ? st.chat : st.conversations[i].msgs;
        nlohmann::json convObj = {{"id", st.conversations[i].id},
                                 {"title", st.conversations[i].title}};
        if (s_userTitled.count(st.conversations[i].id)) convObj["title_user"] = true;
        // Optional document LINK (reference only — never an ownership key).
        if (!st.conversations[i].docLink.empty())
            convObj["doc"] = st.conversations[i].docLink;
        // Project scope follows the workspace of the last launched turn.
        if (!st.conversations[i].projectDir.empty())
            convObj["project"] = st.conversations[i].projectDir;
        if (st.conversations[i].archived) convObj["archived"] = true;
        // Chudcode runtime link (id/file/health) — metadata only, never a second transcript.
        chudcodeLinkPersist(st.conversations[i].id, convObj);
#ifdef __EMSCRIPTEN__
        const auto cellSession = cellAgentSessionHandle(st.conversations[i].id);
        if (!cellSession.empty()) convObj["cell_session_id"] = cellSession;
        const auto cellProvider = cellAgentSessionProvider(st.conversations[i].id);
        if (!cellProvider.empty()) convObj["cell_session_provider"] = cellProvider;
#endif
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
        const auto& qd = (i == st.activeConv) ? st.queuedPromptDrafts
                                              : st.conversations[i].queuedPromptDrafts;
        if (!qd.empty()) convObj["queued_prompt_drafts"] = qd;
        if (!st.conversations[i].branches.empty()) {
            convObj["branches"] = nlohmann::json::array();
            for (const auto& b : st.conversations[i].branches)
                convObj["branches"].push_back({{"id", b.id}, {"label", b.label},
                                              {"when", b.when}, {"from", b.fromMsgIdx}});
        }
        auto unchanged = [&](const std::vector<ChatMsg>& msgs, const std::string& key) {
            const auto old = cached.messages.find(key);
            if (old == cached.messages.end() || old->second.size() != msgs.size()) return false;
            for (size_t mi = 0; mi < msgs.size(); ++mi)
                if (!chat_persistence::sameMessage(old->second[mi].source, msgs[mi])) return false;
            return true;
        };
        bool same = cached.bytes && cached.metadata == convObj && unchanged(liveMsgs, "");
        for (const auto& b : st.conversations[i].branches)
            if (same && !unchanged(b.msgs, b.id)) same = false;
        if (same) {
#ifdef __EMSCRIPTEN__
            sessions.push_back(cached.wire);
            if (!cached.durable) {
                cached.durable = atomicWriteFileDurable(chatSessionFilePath(st.conversations[i].id),
                                                        *cached.bytes);
                if (!cached.durable) markChatHistoryDirty();
                else s_pruneableSessionStems.insert(chatSessionFileStem(st.conversations[i].id));
            }
#else
            snapshot.files[chatSessionFilePath(st.conversations[i].id)] = cached.bytes;
            snapshot.keep.insert(chatSessionFileStem(st.conversations[i].id));
#endif
            order.push_back(st.conversations[i].id);
            continue;
        }
        cached.metadata = convObj;
        // One serializer, used for the live transcript AND for any revert branches parked
        // on this session — a branch is real conversation history and must round-trip
        // through exactly the same shape, not a lossy summary of it.
        auto serializeMsgs = [&](const std::vector<ChatMsg>& msgs, const std::string& key) {
        auto& previous = cached.messages[key];
        nlohmann::json jm = nlohmann::json::array();
        for (size_t mi = 0; mi < msgs.size(); ++mi) {
            const auto& m = msgs[mi];
            if (mi < previous.size() && chat_persistence::sameMessage(previous[mi].source, m)) {
                jm.push_back(previous[mi].wire);
                continue;
            }
            std::string body = m.text;
            if (body.empty() && !m.fromUser && !m.activity.empty() && !m.steerSplit) {
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
            // NEVER skip a message here: AI-VC checkpoints anchor on the chat message ORDINAL
            // (msg_idx), so dropping any row shifts every later checkpoint's anchor one
            // position up after a reload — a revert would silently rewind to the wrong
            // message. Persist an explicit placeholder instead (marked "empty" so the load
            // path can render a subdued note), keeping save/load a position-preserving
            // bijection across restarts.
            const bool emptyTurn = body.empty() && !m.planState.present && !m.running &&
                                   !m.steerSplit && m.contextRefs.empty() && m.imagePaths.empty() &&
                                   m.asks.empty();
            nlohmann::json entry = {{"u", m.fromUser}, {"t", body}, {"e", m.errKind}};
            if (emptyTurn) entry["empty"] = true;
            if (m.steerSplit) entry["split"] = true;
            if (m.fromUser && m.steerState != STEER_NONE) {
                entry["steer"] = m.steerState;
                if (!m.steerId.empty()) entry["sid"] = m.steerId;
            }
            // In-flight marker: this turn was still streaming when the snapshot was taken. On
            // reload it means the app died (or was killed) mid-run — see parseChatSessionJson.
            if (!m.fromUser && m.running) entry["run"] = true;
            if (m.ts > 0.0) entry["ts"] = m.ts;   // send/receive time for the in-bubble age label
            if (!m.fromUser && m.cacheReadTok > 0) {
                entry["cache_tok"] = m.cacheReadTok;          // peak single completion (shown)
                entry["cache_tot"] = m.cacheReadTotTok;       // turn sum (tooltip)
                entry["cache_n"]   = m.cacheReadCalls;
            }
            if (!m.fromUser && !m.model.empty()) entry["m"] = m.model;
            if (m.copilotMode >= 0) entry["cm"] = m.copilotMode;
            // The context chips a user message carried, so they still draw after a reload.
            if (m.fromUser && !m.contextRefs.empty())
                entry["ctx"] = core::contextRefsToJson(m.contextRefs);
            // Exact sent-image paths: the bubble renders these, never a basename guess.
            if (m.fromUser && !m.imagePaths.empty()) entry["images"] = m.imagePaths;
            // Error card fields — the humanized headline and the raw failure text (capped like
            // an activity detail). Without these a reloaded error turn loses its guidance.
            if (!m.errorHuman.empty()) entry["eh"] = m.errorHuman;
            if (!m.errorFull.empty())
                entry["ef"] = m.errorFull.size() > 8000u
                                  ? m.errorFull.substr(0, 7999) + "\xe2\x80\xa6"
                                  : m.errorFull;
            if (m.planState.present) entry["plan_state"] = planStateToJson(m.planState);
            // ask_user question batches: what was asked and how it was resolved.
            if (!m.asks.empty()) {
                nlohmann::json asks = nlohmann::json::array();
                for (const auto& b : m.asks) asks.push_back(askBatchToJson(b));
                entry["asks"] = std::move(asks);
            }
            // Persist activity timeline (capped) so thoughts/tools survive reload.
            if (!m.fromUser && !m.activity.empty()) {
                nlohmann::json act = nlohmann::json::array();
                const int nAct = (int)m.activity.size();
                // Keep all thought/tool/plan/question rows (rk 1/2/3/18/20) + last N stage rows so
                // mid-turn kill/restart still has planning + tools visible.
                std::vector<int> keepIdx;
                keepIdx.reserve((size_t)nAct);
                int stagesKept = 0;
                const int kMaxStages = 80;
                for (int ai = nAct - 1; ai >= 0; --ai) {
                    const auto& a = m.activity[(size_t)ai];
                    const bool priority = (a.rowKind == 2 || a.rowKind == 3 ||
                                           a.rowKind == 1 || a.rowKind == 18 || a.rowKind == 20 ||
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
                    // Tool fields are bounded at ingest; keep their caption/artifact trailers
                    // intact so restored evidence stays on its originating timeline row.
                    if (a.rowKind != 3 && det.size() > cap)
                        det = det.substr(0, cap - 1) + "\xe2\x80\xa6";
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
            chat_persistence::MessageSnapshot saved{m, entry};
            if (mi < previous.size()) previous[mi] = std::move(saved);
            else previous.push_back(std::move(saved));
            jm.push_back(std::move(entry));
        }
        previous.resize(msgs.size());
        return jm;
        };
        // Archive the uncapped in-memory state before serializeMsgs applies the bounded
        // <id>.json representation. This also records terminal message mutations (full error,
        // final prose, plan/step state) after the creation record.
        archiveChangedChatMessages(st.conversations[i].id, liveMsgs, cached.messages[""]);
        convObj["msgs"] = serializeMsgs(liveMsgs, "");
        // Revert branches: tails a rollback moved aside. Persisted so a revert stays
        // undoable after a restart.
        if (!st.conversations[i].branches.empty()) {
            nlohmann::json jb = nlohmann::json::array();
            for (const auto& b : st.conversations[i].branches)
                jb.push_back({{"id", b.id}, {"label", b.label}, {"when", b.when},
                              {"from", b.fromMsgIdx}, {"msgs", serializeMsgs(b.msgs, b.id)}});
            convObj["branches"] = std::move(jb);
        }
        for (auto it = cached.messages.begin(); it != cached.messages.end();) {
            const bool keep = it->first.empty() ||
                std::any_of(st.conversations[i].branches.begin(), st.conversations[i].branches.end(),
                            [&](const ChatBranch& b) { return b.id == it->first; });
            if (keep) ++it;
            else it = cached.messages.erase(it);
        }
        cached.wire = std::move(convObj);
        cached.bytes = std::make_shared<const std::string>(cached.wire.dump(1));
#ifdef __EMSCRIPTEN__
        sessions.push_back(cached.wire);
        cached.durable = atomicWriteFileDurable(chatSessionFilePath(st.conversations[i].id),
                                              *cached.bytes);
        if (!cached.durable) markChatHistoryDirty();
        else s_pruneableSessionStems.insert(chatSessionFileStem(st.conversations[i].id));
#else
        snapshot.files[chatSessionFilePath(st.conversations[i].id)] = cached.bytes;
        snapshot.keep.insert(chatSessionFileStem(st.conversations[i].id));
#endif
        order.push_back(st.conversations[i].id);
    }
    // Index v3: `order` + the global `active` (fallback when no root is resolvable) +
    // `active_by_project`, so relaunching under a root lands on THAT root's last chat. The
    // active session is remembered under its OWN stored project, never the current root's:
    // it may be another project's session the user opened explicitly.
    if (!activeId.empty()) {
        const std::string ownKey = convProjectKey(st.conversations[st.activeConv].projectDir);
        if (!ownKey.empty()) s_activeByProject[ownKey] = activeId;
    }
    nlohmann::json activeByProject = nlohmann::json::object();
    for (const auto& [key, id] : s_activeByProject)
        if (!key.empty() && writtenIds.count(id) && sessionOwnedByProject(st, id, key))
            activeByProject[key] = id;
    nlohmann::json index = {{"version", 3},
                            {"active", activeId},
                            {"active_by_project", std::move(activeByProject)},
                            {"order", order},
                            {"session_seq", s_sessionSeq}};
#ifndef __EMSCRIPTEN__
    snapshot.files[snapshot.indexPath] = std::make_shared<const std::string>(index.dump(1));
    for (auto it = s_pruneableSessionStems.begin(); it != s_pruneableSessionStems.end();) {
        if (!snapshot.keep.count(*it)) it = s_pruneableSessionStems.erase(it);
        else ++it;
    }
    chatWriter().submit(std::move(snapshot));
    if (immediate && !chatWriter().flush()) markChatHistoryDirty();
    reportChatSaveFailure(st);
#else
    try {
        // Durable atomic write (tmp + fsync + rename + dir fsync) — a hard crash mid-run must
        // never be able to leave a truncated/empty transcript behind.
        if (!atomicWriteFileDurable(chatSessionsIndexPath(), index.dump(1))) {
            markChatHistoryDirty();
            st.addLog("[chat] session index save failed; pending history will be retried");
            return;
        }
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
            if (!ec) {
                // Both append-only transcript families are owned by the same vouched-for
                // session. Best-effort removal stays behind the exact JSON prune safety gate.
                std::error_code rawEc;
                fs::remove_all(chatSubagentsDirPath(stem), rawEc);
                rawEc.clear();
                fs::remove(chatRawLogPath(stem), rawEc);
                {
                    std::lock_guard<std::mutex> lock(s_chatRawFileMutex);
                    s_chatRawNextSeq.erase(chatRawLogPath(stem));
                }
                {
                    std::lock_guard<std::mutex> lock(s_chatRawOwnerMutex);
                    s_chatRawMessageSnapshots.erase(stem);
                }
                s_pruneableSessionStems.erase(stem);
            }
        }
        appstatePersistSync();  // WASM: flush IDBFS; no-op on desktop
#ifdef __EMSCRIPTEN__
        // Push the whole flat store to the shared workspace (async; never blocks UI).
        nlohmann::json root = {{"version", 2}, {"active", activeId}, {"sessions", sessions}};
        wasmPutSharedChat(root);
#endif
    } catch (...) {
        markChatHistoryDirty();
        st.addLog("[chat] session save failed; pending history will be retried");
    }
#endif
    for (auto it = s_sessionSnapshots.begin(); it != s_sessionSnapshots.end();) {
        if (!writtenIds.count(it->first)) it = s_sessionSnapshots.erase(it);
        else ++it;
    }
}

void persistChatHistory(AppState& st) {
    snapshotChatHistory(st, true);
}

void flushChatPersistence(AppState& st) {
    savePromptDraft(st, true);
    snapshotChatHistory(st, true);
#ifndef __EMSCRIPTEN__
    if (!chatWriter().flush()) markChatHistoryDirty();
    reportChatSaveFailure(st);
#endif
}

// Debounced flush while a turn is running (min 1.0s). Force bypasses the timer.
void maybePersistChatHistoryDebounced(AppState& st, bool force) {
#ifndef __EMSCRIPTEN__
    reportChatSaveFailure(st);
#endif
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
    snapshotChatHistory(st, force);
}

// A transcript restored from disk or forked into a new session owns no live stream. Keep
// terminal outcomes, but close unfinished work so every consumer sees the same truthful state.
static void closeDetachedTurn(ChatMsg& m) {
    m.running = false;
    m.liveStatus.clear();
    for (auto& item : m.planState.items) {
        const bool terminal = item.status == "done" || item.status == "pass" ||
            item.status == "fail" || item.status == "failed" || item.status == "error" ||
            item.status == "abandoned" || item.status == "skipped" || item.status == "cancelled";
        if (item.status == "running" || (item.kind == "subagent" && !terminal))
            item.status = "stopped";
    }
    for (auto& a : m.activity) {
        if (a.status == "running") a.status = "stopped";
        a.streaming = false;
    }
    m.uiCachedH = -1.0f;
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
        // Every id that comes back from disk pushes the allocator past it, so the next
        // "+ New session" cannot land on a transcript that already exists.
        noteConvIdSeen(st, c.id);
        c.title = jc.value("title", std::string("Session"));
        const bool titleUser = jc.value("title_user", false);
        c.docLink = docLink;
        c.archived = jc.value("archived", false);
        // Project scope. Legacy sessions (written before the field existed) recover one
        // below, once, after the transcript and the Chudcode link are known.
        c.projectDir = jc.value("project", std::string());
#ifdef __EMSCRIPTEN__
        std::string relativeProject, projectError;
        c.projectDir = chudware::workspaceRelativePath(c.projectDir, relativeProject, projectError)
                           ? (relativeProject.empty() ? "." : relativeProject)
                           : ".";
#endif
        // This session's own saved execution trace, straight into its conversation-keyed store
        // (no active/parked special case — switching sessions just reads the other tree).
        if (jc.contains("agent_dag")) ui::agentDagFromJson(c.id, jc["agent_dag"]);
        // One parser, used for the live transcript AND for revert branches — a branch is
        // real history and must come back in exactly the same shape it went out in.
        auto parseMsgs = [&](const nlohmann::json& arr) {
        std::vector<ChatMsg> out;
        for (const auto& m : arr) {
            ChatMsg cm;
            cm.fromUser = m.value("u", false);
            cm.text = m.value("t", std::string());
            cm.errKind = m.value("e", 0);
            if (!cm.fromUser) cm.model = m.value("m", std::string());
            // Legacy per-message mode ints (Swarm/Edit/EngFirm) fold onto the Agent|Plan|Ask
            // enum; -1 stays -1 (absent = no badge).
            cm.copilotMode = m.contains("cm") ? normalizeCopilotMode(m.value("cm", -1)) : -1;
            cm.errorHuman = m.value("eh", std::string());
            cm.errorFull = m.value("ef", std::string());
            cm.ts = m.value("ts", 0.0);          // 0 = legacy (constructed with now) — no age label
            cm.cacheReadTok = m.value("cache_tok", 0);
            // Legacy sessions wrote only cache_tok (a SUM under the old semantics). Falling
            // back to it here would reprint the inflated figure in the tooltip, so default the
            // total to the peak: a one-call turn, which is the honest reading of "unknown".
            cm.cacheReadTotTok = m.value("cache_tot", cm.cacheReadTok);
            cm.cacheReadCalls  = m.value("cache_n", cm.cacheReadTok > 0 ? 1 : 0);
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
            cm.steerSplit = !cm.fromUser && m.value("split", false);
            if (cm.fromUser) {
                cm.steerId = m.value("sid", std::string());
                cm.steerState = m.value("steer", (int)STEER_NONE);
                // Like an open question batch: the run that held it is gone after a reload.
                if (cm.steerState == STEER_SENDING || cm.steerState == STEER_QUEUED)
                    cm.steerState = STEER_FAILED;
                if (cm.steerState < STEER_NONE || cm.steerState > STEER_FAILED)
                    cm.steerState = STEER_NONE;
            }
            if (cm.fromUser && m.contains("ctx"))
                cm.contextRefs = core::contextRefsFromJson(m["ctx"]);
            if (cm.fromUser && m.contains("images") && m["images"].is_array()) {
                for (const auto& p : m["images"])
                    if (p.is_string() && !p.get_ref<const std::string&>().empty())
                        cm.imagePaths.push_back(p.get<std::string>());
            }
            // A batch saved while still open comes back Expired: a restart drops every live run
            // (sessions are only parsed from disk when not already resident — see above).
            if (m.contains("asks") && m["asks"].is_array())
                for (const auto& jb : m["asks"])
                    if (jb.is_object()) cm.asks.push_back(askBatchFromJson(jb));
            if (!cm.asks.empty()) anchorLegacyAsks(cm);
            closeDetachedTurn(cm);
            // Placeholder for a turn that produced no visible output (persisted as
            // "empty" so message ORDINALS survive the round trip — AI-VC msg_idx
            // anchors depend on positions never shifting across a restart).
            if (cm.text.empty() && m.value("empty", false))
                cm.text = "(Turn produced no visible output \xe2\x80\x94 kept to preserve chat positions.)";
            out.push_back(std::move(cm));
        }
        ui::reconcileResolvedAskReplies(out);
        return out;
        };
        c.msgs = parseMsgs(jc.value("msgs", nlohmann::json::array()));
        // Revert branches: tails a rollback moved aside, so a revert stays undoable after
        // a restart. Absent on sessions written before branches existed.
        if (jc.contains("branches") && jc["branches"].is_array()) {
            for (const auto& jb : jc["branches"]) {
                if (!jb.is_object()) continue;
                ChatBranch b;
                b.id = jb.value("id", std::string());
                b.label = jb.value("label", std::string());
                b.when = jb.value("when", 0.0);
                b.fromMsgIdx = jb.value("from", -1);
                b.msgs = parseMsgs(jb.value("msgs", nlohmann::json::array()));
                if (!b.msgs.empty()) c.branches.push_back(std::move(b));
            }
        }
        c.lastContextTokens = jc.value("last_context_tokens", 0);
        if (jc.contains("queued_prompts") && jc["queued_prompts"].is_array()) {
            for (const auto& q : jc["queued_prompts"])
                if (q.is_string()) c.queuedPrompts.push_back(q.get<std::string>());
        }
        if (jc.contains("queued_prompt_drafts") && jc["queued_prompt_drafts"].is_array()) {
            for (const auto& d : jc["queued_prompt_drafts"])
                c.queuedPromptDrafts.push_back(d.is_object() ? d : nlohmann::json::object());
        }
        c.queuedPromptDrafts.resize(c.queuedPrompts.size(), nlohmann::json::object());
        // Fresh sessions are never kept (same rule as persist) — drop legacy empty entries
        // left on disk by older builds instead of resurrecting them.
        // A session reverted all the way back still holds its branches — that is user
        // history, and dropping it here would make the revert unrecoverable.
        // A session the user named is kept even when empty: the name is content they chose.
        if (c.msgs.empty() && c.queuedPrompts.empty() && c.branches.empty() && !titleUser)
            return false;
        if (titleUser) s_userTitled.insert(c.id);
        s_sessionSeq = std::max(s_sessionSeq, sessionTitleOrdinal(c.title));
        // Chudcode runtime link (id/file/health): restore into the conversation-keyed
        // registry only for sessions that are actually kept.
        chudcodeLinkRestore(c.id, jc);
#ifndef __EMSCRIPTEN__
        // Legacy project-scope recovery — ONCE, at load time, in priority order:
        //   1) the Chudcode session JSONL's own `cwd` (exactly where that run worked), then
        //   2) the old artifact heuristic against the CURRENT project root (a session that
        //      produced a file under this project belongs to it).
        // Neither answers -> projectDir stays empty: listed only under Sessions' "Unassigned".
        // The next persist writes whatever was recovered, so this runs at most once per
        // session for the life of the store.
        if (c.projectDir.empty()) {
            if (const ChudcodeSessionLink* link = chudcodeSessionLink(c.id))
                c.projectDir = chudcodeSessionCwd(link->sessionFile);
            if (c.projectDir.empty()) {
                const std::string current = convProjectDir(st);
                const std::filesystem::path root = lexicalPath(current);
                if (hasProjectArtifact(c.msgs, root, comparablePath(root)))
                    c.projectDir = current;
            }
            if (!c.projectDir.empty()) markChatHistoryDirty();
        }
#endif
        st.conversations.push_back(std::move(c));
    }
    return true;
}

// ---- headless Chudcode session adoption --------------------------------------------------
// A mission launched WITHOUT the GUI (services/worker_main.py agent_admin_turn with
// sessions_dir=<Documents>/Chudware/Sessions) writes only Chudcode's own
// "<timestamp>_<uuid>.jsonl" into that folder — no <id>.json transcript at all, so the
// orphan sweep above never saw it and the run was unreachable from the Sessions list.
// Replay its VISIBLE transcript into a real conversation instead, linked to the session
// file so the GUI can resume it (chudcode_session_file).
//
// Chudcode session JSONL (version 3) entry types, one JSON object per line:
//   {"type":"session","version":3,"id":…,"cwd":…}   session identity + the run's project cwd
//   {"type":"message","message":{"role":"user"|"assistant"|"toolResult","content":[…]}}
//        content parts are {"type":"text","text":…} | {"type":"thinking",…} |
//        {"type":"toolCall",…}; assistant messages also carry model/provider/usage
//   {"type":"title"|"title_change"|"model_change"|"thinking_level_change"|
//    "credential_pin"|"custom"|"custom_message"}   lifecycle / tool-execution / system records
// Only user+assistant "text" parts are replayed. thinking, toolCall, toolResult and every
// lifecycle record stay in the file — which is exactly what the leading note tells the user.
static bool adoptChudcodeSessionJsonl(AppState& st, const std::filesystem::path& file,
                                      const std::string& sessionId) {
    static constexpr size_t kMaxReplayBytes = 2u * 1024u * 1024u;
    static constexpr int    kMaxEntries = 400;
    std::ifstream in(file, std::ios::binary);
    if (!in) return false;

    std::string cwd, firstUserText;
    std::vector<ChatMsg> msgs;
    size_t consumed = 0;
    int entries = 0;
    std::string line;
    while (std::getline(in, line)) {
        consumed += line.size() + 1;
        if (consumed > kMaxReplayBytes || entries >= kMaxEntries) break;
        if (line.empty()) continue;
        nlohmann::json j;
        try { j = nlohmann::json::parse(line); } catch (...) { continue; }
        if (!j.is_object()) continue;
        const std::string type = j.value("type", std::string());
        if (type == "session") {
            if (cwd.empty()) cwd = j.value("cwd", std::string());
            continue;
        }
        if (type != "message") continue;   // lifecycle / tool / system record
        const auto m = j.find("message");
        if (m == j.end() || !m->is_object()) continue;
        const std::string role = m->value("role", std::string());
        if (role != "user" && role != "assistant") continue;   // toolResult stays in the file
        const auto content = m->find("content");
        if (content == m->end() || !content->is_array()) continue;
        ++entries;
        std::string text;
        for (const auto& part : *content) {
            if (!part.is_object() || part.value("type", std::string()) != "text") continue;
            const std::string piece = part.value("text", std::string());
            if (piece.empty()) continue;
            if (!text.empty()) text += "\n\n";
            text += piece;
        }
        if (text.empty()) continue;   // a pure thinking/tool-call turn has nothing to show
        ChatMsg cm;
        cm.fromUser = (role == "user");
        if (!cm.fromUser) cm.model = m->value("model", std::string());
        cm.text = std::move(text);
        if (cm.fromUser && firstUserText.empty()) firstUserText = cm.text;
        msgs.push_back(std::move(cm));
    }

    Conversation c;
    c.id = newConvId(st);
    noteConvIdSeen(st, c.id);
    // The run's own cwd IS its project (same field the composer would have sent as
    // workspace_dir), so the adopted session lists under that project and nowhere else.
    c.projectDir = cwd;
    // Title: the first prompt, one line, bounded — else the session file's own stem.
    {
        std::string t = firstUserText.substr(0, firstUserText.find('\n'));
        size_t b = t.find_first_not_of(" \t\r");
        size_t e = t.find_last_not_of(" \t\r");
        t = (b == std::string::npos) ? std::string() : t.substr(b, e - b + 1);
        if (t.size() > 60) t.resize(60);
        c.title = t.empty() ? file.stem().string() : t;
    }
    ChatMsg note;
    note.fromUser = false;
    note.text = "Adopted from an external agent session; tool activity lives in the "
                "session file";
    c.msgs.reserve(msgs.size() + 1);
    c.msgs.push_back(std::move(note));
    for (ChatMsg& m : msgs) c.msgs.push_back(std::move(m));

    ChudcodeSessionLink link;
    link.sessionId = sessionId;
    link.sessionFile = file.string();
    link.health = "healthy";
    chudcodeSessionLinkSet(c.id, std::move(link));
    st.conversations.push_back(std::move(c));
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
#ifndef __EMSCRIPTEN__
    // Reload is a read-after-write barrier. Never replace newer resident history with stale
    // disk state when storage failed; the writer retains that state for its next attempt.
    if (!chatWriter().flush()) { reportChatSaveFailure(st); return; }
    chatWriter().forgetDurable();
#endif
    s_sessionSnapshots.clear();
    // Park + drop everything resident — this runs before any session work has happened
    // (or from the WASM merge, which accepts a rebuild).
    parkActiveConversation(st);
    st.chat.clear();
    st.lastContextTokens = 0;
    st.queuedPrompts.clear();
    st.queuedPromptDrafts.clear();
    st.conversations.clear();
    st.activeConv = -1;
    // The Chudcode link registry mirrors the resident set — rebuild it with the store.
    chudcodeSessionLinksClear();
#ifdef __EMSCRIPTEN__
    clearCellAgentSessionHandles();
#endif

    bool migratedLegacy = false;
    bool recoveredOrphan = false;
    std::string activeId;
    std::set<std::string> seen;  // ids loaded this run — dup guard + orphan-sweep skip set
    auto noteLoaded = [&] {  // record a successfully parsed session as prunable + seen
        const std::string& id = st.conversations.back().id;
        seen.insert(id);
        s_pruneableSessionStems.insert(chatSessionFileStem(id));
    };
    // Per-session transcript store: index.json (order + active ids) + <id>.json transcripts.
    // The remembered per-project active map is rebuilt with the store.
    s_activeByProject.clear();
    // Titles are rebuilt with the store too: the user-named set from each session file, the
    // number counter from the index (and from any "Session N" already on disk).
    s_userTitled.clear();
    s_sessionSeq = 0;
    const std::string projectKey = convProjectKey(convProjectDir(st));
    {
        nlohmann::json index = nlohmann::json::object();
        {
            std::ifstream in(chatSessionsIndexPath());
            if (in) { try { in >> index; } catch (...) {} }
        }
        if (index.is_object() && index.contains("order") && index["order"].is_array()) {
            // v3: per-project active pointer wins for the project we are opening under (resolved
            // once every session is loaded, below); the top-level `active` remains the fallback
            // (and is all a v2 index carries).
            activeId = index.value("active", std::string());
            if (index.contains("session_seq") && index["session_seq"].is_number_integer())
                s_sessionSeq = std::max(s_sessionSeq, index["session_seq"].get<int>());
            if (index.contains("active_by_project") && index["active_by_project"].is_object()) {
                for (auto it = index["active_by_project"].begin();
                     it != index["active_by_project"].end(); ++it) {
                    if (!it.value().is_string() || it.key().empty()) continue;
                    s_activeByProject[it.key()] = it.value().get<std::string>();
                }
            }
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
    if (st.conversations.empty() && shotChatStoreDir().empty()) {
        // First run on the new store: import the legacy single-file history (never into a
        // scripted shot's private store, which must start empty).
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
#ifndef __EMSCRIPTEN__
    // Headless-mission sweep: Chudcode session JSONLs in the Sessions dir that NO resident
    // conversation links to (see adoptChudcodeSessionJsonl). Runs after the legacy import so
    // adopting one can never suppress that migration. Bounded and never throwing: oversized
    // files are skipped, a parse failure just means "no answer".
    {
        namespace fs = std::filesystem;
        static constexpr std::uintmax_t kMaxJsonlBytes = 64ull * 1024ull * 1024ull;
        std::set<std::string> linkedIds, linkedFiles;
        for (const auto& c : st.conversations) {
            const ChudcodeSessionLink* link = chudcodeSessionLink(c.id);
            if (!link) continue;
            if (!link->sessionId.empty()) linkedIds.insert(link->sessionId);
            if (!link->sessionFile.empty())
                linkedFiles.insert(comparablePath(lexicalPath(link->sessionFile)));
        }
        std::error_code ec;
        for (const auto& de : fs::directory_iterator(chatSessionsDir(), ec)) {
            if (!de.is_regular_file(ec)) continue;
            const std::string name = de.path().filename().string();
            if (name.size() < 7 || name.compare(name.size() - 6, 6, ".jsonl") != 0) continue;
            // Our own per-conversation raw event log lives in this folder too.
            if (name.size() >= 10 && name.compare(name.size() - 10, 10, ".raw.jsonl") == 0)
                continue;
            const std::uintmax_t bytes = de.file_size(ec);
            if (ec || bytes == 0 || bytes > kMaxJsonlBytes) { ec.clear(); continue; }
            if (linkedFiles.count(comparablePath(lexicalPath(de.path().string())))) continue;
            const std::string sessionId = chudcodeSessionIdOf(de.path().string());
            if (sessionId.empty() || linkedIds.count(sessionId)) continue;
            if (adoptChudcodeSessionJsonl(st, de.path(), sessionId)) {
                noteLoaded();
                linkedIds.insert(sessionId);
                recoveredOrphan = true;   // needs its own <id>.json + an index entry
            }
        }
    }
#endif
    // A remembered pointer is valid only for a session stored under that same project. Older
    // builds keyed whatever session was on screen under the current root, so another project's
    // chat could claim it; drop such entries (the next persist rewrites a clean index).
    for (auto it = s_activeByProject.begin(); it != s_activeByProject.end();) {
        if (sessionOwnedByProject(st, it->second, it->first)) { ++it; continue; }
        it = s_activeByProject.erase(it);
        markChatHistoryDirty();
    }
    if (const auto mine = s_activeByProject.find(projectKey);
        !projectKey.empty() && mine != s_activeByProject.end())
        activeId = mine->second;
    if (st.conversations.empty()) {
        ensureConversations(st);  // seeds a numbered session linked to the current document
    } else {
        st.activeConv = 0;
        if (!activeId.empty())
            for (int i = 0; i < (int)st.conversations.size(); ++i)
                if (st.conversations[i].id == activeId) { st.activeConv = i; break; }
        restoreActiveConversation(st);
    }
    // The restored selection was on screen under whatever root was current LAST time; the next
    // followProjectActiveConversation reconciles it against the root selected now (and again
    // whenever an asynchronously restored root arrives).
    s_followReconciled = false;
    restorePromptDraft(st);
    // Sessions saved under a placeholder ("Chat 3") show their first prompt straight away.
    for (int i = 0; i < (int)st.conversations.size(); ++i) autoTitleConversation(st, i);
    // Loading is not a new chat event. Seed change detection from the restored state so the
    // first persistence tick does not duplicate the entire prior transcript into the archive.
    for (int i = 0; i < (int)st.conversations.size(); ++i) {
        const auto& messages = (i == st.activeConv) ? st.chat : st.conversations[i].msgs;
        seedChatRawSnapshots(st.conversations[i].id, messages);
    }
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
    int localSeq = 0;
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
            if (index.contains("session_seq") && index["session_seq"].is_number_integer())
                localSeq = index["session_seq"].get<int>();
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
        if (localSeq > 0) index["session_seq"] = localSeq;
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

    // All parseable transcripts are resident after startup. Serialize their relinked state
    // through the same ordered writer; a second disk-rewrite path could race a pending save.
    if (relinked) markChatHistoryDirty();
}

// ---- Chudcode session recovery: service latch + post-turn metadata/health capture ----------
// One latch for the whole surface: flipped false on the first unknown-method response so an
// older worker (slice not deployed) disables every recovery control instead of retrying
// forever. Shared with the Sessions panel via copilot_internal.h.
bool& chudcodeSessionServiceAvailable() {
    static bool s_available = true;
    return s_available;
}

// When the ACTIVE conversation's turn finishes, capture the Chudcode runtime metadata
// (chudcode_session_id / chudcode_session_file) and a health verdict with ONE read-only
// doctor dry-run, then stamp them into the conversation's persisted link. This is what makes
// resume-after-restart work without the user ever opening the Sessions panel: the resume key
// reaches disk as soon as a turn has run. Never fires for background (parked) runs — their
// link updates the next time they are viewed and finish a turn.
static void pollChudcodeSessionHealth(AppState& st) {
    if (!chudcodeSessionServiceAvailable()) return;
    static bool s_inFlight = false;
    static std::string s_runningConv;   // active conv observed running (transition source)
    const std::string convId = activeConversationId(st);
    if (activeConversationRunning(st)) {
        s_runningConv = convId;
        return;
    }
    // Only the run we watched START on this same conversation counts as "finished" here —
    // switching from a running session to an idle one must not diagnose the idle one.
    if (s_inFlight || s_runningConv.empty() || s_runningConv != convId) return;
    s_runningConv.clear();
    const ChudcodeSessionLink* link = chudcodeSessionLink(convId);
    if (!link || !link->runtimeLive) return;   // no service-side runtime to describe
    s_inFlight = true;
    st.rpc.call(kChudcodeRepairSessionMethod,
                chudcodeRepairSessionParams(convId, /*repair=*/false),
                [&st, convId](const core::RpcResult& r) {
        s_inFlight = false;
        if (chudcodeSessionRpcAbsent(r.ok, r.err, r.result)) {
            chudcodeSessionServiceAvailable() = false;
            return;
        }
        if (!r.ok) return;   // transport hiccup — next finished turn retries
        if (chudcodeSessionNotLive(r.result)) {
            chudcodeSessionLinkEnsure(convId).runtimeLive = false;
            return;
        }
        ChudcodeDoctorSummary summary;
        if (!parseChudcodeDoctorSummary(r.result, summary)) return;
        // An edit transaction of this conversation owns its link (it adopts the rewind branch
        // at commit); a dry-run of the session being rewound must not stamp the old file back.
        if (editStageHoldsConversation(convId)) return;
        applyChudcodeDoctor(chudcodeSessionLinkEnsure(convId), summary);
#ifdef __EMSCRIPTEN__
        chudcodeSessionLinkSet(convId, chudcodeSessionLinkEnsure(convId));
#endif
        markChatHistoryDirty();   // resume key + health reach disk on the debounced flush
    }, 30000);
}


// Newest non-archived session stored under `key`, never index `skip`; -1 when there is none.
// st.conversations is creation order (newConversation appends), so the tail is the newest.
static int latestProjectConversation(const AppState& st, const std::string& key, int skip) {
    for (int i = (int)st.conversations.size() - 1; i >= 0; --i) {
        const Conversation& c = st.conversations[i];
        if (i != skip && !c.archived && convProjectKey(c.projectDir) == key) return i;
    }
    return -1;
}

// The selected root is the sole chat authority. Whenever it changes — and once after every
// (re)load, because the restored selection was on screen under LAST run's root — the active
// session is reconciled to it: kept when it is stored under that root; otherwise the root's
// remembered session, else its newest one, opens (switchConversation parks the outgoing
// session; a run keeps streaming into it in the background). The session being left stays
// its OWN project's remembered chat. An empty draft has run nowhere: with nothing to restore
// it moves to the entered root; otherwise a new session opens there. Peeking at another
// project in Sessions, or opening one of its sessions explicitly, is not a root change.
static void followProjectActiveConversation(AppState& st) {
    const std::string dir = convProjectDir(st);
    const std::string key = convProjectKey(dir);
    if (s_followReconciled && key == s_followKey) return;
    const bool switched = s_followReconciled;   // false: first observation after a (re)load
    s_followReconciled = true;
    s_followKey = key;
    if (key.empty()) return;   // no root resolved yet: nothing to reconcile against
    const bool haveActive = st.activeConv >= 0 && st.activeConv < (int)st.conversations.size();
    bool draft = false;
    if (haveActive) {
        const Conversation& active = st.conversations[st.activeConv];
        const std::string activeKey = convProjectKey(active.projectDir);
        if (activeKey == key) return;   // already this root's session
        draft = st.chat.empty() && active.msgs.empty() && st.queuedPrompts.empty() &&
                !st.convRunning(active.id);
        if (switched && !draft && !activeKey.empty()) s_activeByProject[activeKey] = active.id;
    }
    int target = -1;
    if (const auto it = s_activeByProject.find(key); it != s_activeByProject.end()) {
        for (int i = 0; i < (int)st.conversations.size(); ++i) {
            const Conversation& c = st.conversations[i];
            if (c.id != it->second) continue;
            if (!c.archived && convProjectKey(c.projectDir) == key) target = i;
            break;
        }
        if (target < 0) s_activeByProject.erase(it);   // stale or foreign: never restored
    }
    if (target < 0) target = latestProjectConversation(st, key, st.activeConv);
    if (target >= 0) {
        switchConversation(st, target);
        return;
    }
    if (draft) {
        bindActiveConversationProject(st, dir);   // the draft keeps its session id and text
        return;
    }
    newConversation(st);   // parks the outgoing session; scope = the entered root
}

// Per-frame chat/doc sync (called each frame). Sessions are document-independent now: a
// document switch NEVER loads/parks/replaces the session set — it only refreshes
// st.chatDocKey (the current document's key, for run launches + prompt drafts), runs the
// relink sweep, and kicks the one-time startup load.
void syncChatToDoc(AppState& st) {
#ifdef __EMSCRIPTEN__
    wasmKickSharedChatFetch();       // once: async GET full shared store
    wasmApplySharedChatPending(st);  // merge when the GET lands
#endif
    static bool s_historyLoaded = false;
    if (!s_historyLoaded) {
        s_historyLoaded = true;
        loadChatHistory(st);
    }
    relinkSavedDocSessions(st);
    st.chatDocKey = convDocKey(st);
    ensureConversations(st);
    followProjectActiveConversation(st);
#ifdef __EMSCRIPTEN__
    // Mint replies can arrive on a job thread; copy only opaque metadata on the
    // UI thread, for both active and parked chats, so restart resumes the cell.
    for (const auto& conversation : st.conversations) {
        const auto handle = cellAgentSessionHandle(conversation.id);
        auto& link = chudcodeSessionLinkEnsure(conversation.id);
        if (!handle.empty() && handle != link.sessionId) {
            link.sessionId = handle;
            link.sessionFile.clear();
            markChatHistoryDirty();
        }
    }
#endif
    pollChudcodeSessionHealth(st);
}

static void clearComposerState(AppState& st) {
    if (st.prompt.data() && !st.prompt.empty()) {
        std::memset(st.prompt.data(), 0, st.prompt.size());
        st.prompt[0] = 0;
    }
    st.pendingAttachments.clear();
    st.pendingInlineImagePaths.clear();
    st.pendingContextRefs.clear();
    st.contextPickActive = false;
    composerResetInputState(st);
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
    // A session the user named is not disposable either.
    return msgs.empty() && queued.empty() && !st.convRunning(c.id) && !s_userTitled.count(c.id);
}

// Drop the ACTIVE conversation when it is still fresh (see above), fixing up activeConv.
// Returns true when it erased — callers then skip parking (there was nothing to park).
static bool discardActiveConversationIfFresh(AppState& st) {
    if (!conversationIsFresh(st, st.activeConv)) return false;
    st.chat.clear();
    st.lastContextTokens = 0;
    st.queuedPrompts.clear();
    st.queuedPromptDrafts.clear();
    st.conversations.erase(st.conversations.begin() + st.activeConv);
    st.activeConv = -1;  // callers set the new active index right after
    return true;
}

void parkActiveConversation(AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    // A merely staged edit dies with the view (its composer text restored while this is still
    // the viewed conversation, so the draft saved below is the pre-edit one); an in-flight or
    // committed edit transaction continues for ITS conversation by id.
    editStageBeforeConversationPark(st);
    // Keep in-progress prompt + chips for this session on disk before parking.
    savePromptDraft(st, /*force=*/true);
    Conversation& c = st.conversations[st.activeConv];
    c.msgs = std::move(st.chat);
    c.lastContextTokens = st.lastContextTokens;
    c.queuedPrompts = std::move(st.queuedPrompts);
    c.queuedPromptDrafts = std::move(st.queuedPromptDrafts);
    st.chat.clear();
    st.lastContextTokens = 0;
    st.queuedPrompts.clear();
    st.queuedPromptDrafts.clear();
}

static void restoreActiveConversation(AppState& st) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size()) return;
    Conversation& c = st.conversations[st.activeConv];
    st.chat = std::move(c.msgs);
    st.lastContextTokens = c.lastContextTokens;
    st.queuedPrompts = std::move(c.queuedPrompts);
    st.queuedPromptDrafts = std::move(c.queuedPromptDrafts);
    c.msgs.clear();
    c.queuedPrompts.clear();
    c.queuedPromptDrafts.clear();
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
    // chat must not accumulate "Session 1, Session 2, …" empties in the list or on disk. The
    // replaced session hands its number back so repeated clicks do not burn through the count.
    const std::string outgoingTitle =
        (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size())
            ? st.conversations[st.activeConv].title : std::string();
    if (discardActiveConversationIfFresh(st)) {
        if (s_sessionSeq > 0 && sessionTitleOrdinal(outgoingTitle) == s_sessionSeq)
            --s_sessionSeq;
    } else {
        parkActiveConversation(st);  // force-saves old draft
    }
    Conversation c;
    c.id = newConvId(st);
    c.title = nextSessionTitle();
    c.docLink = convDocKey(st);  // link to the current document — reference only
    c.projectDir = convProjectDir(st);  // initial scope; a turn may run in another workspace
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
        st.chat.clear();
        st.lastContextTokens = 0;
        st.queuedPrompts.clear();
        st.queuedPromptDrafts.clear();
        clearComposerState(st);
    }
    // Drop draft key for the closed conversation.
    {
        const std::string closedId = st.conversations[idx].id;
        s_userTitled.erase(closedId);
#ifndef __EMSCRIPTEN__
        auto& root = residentPromptDrafts();
        const std::string suffix = "/" + closedId;
        bool removed = false;
        for (auto it = root.begin(); it != root.end();) {
            const auto& key = it.key();
            if (key.size() >= suffix.size() &&
                key.compare(key.size() - suffix.size(), suffix.size(), suffix) == 0) {
                it = root.erase(it);
                removed = true;
            } else ++it;
        }
        if (removed) persistPromptDrafts(st, true);
#else
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
#endif
    }
    // Hard delete disposes the Chudcode runtime too (idempotent, best-effort) and drops the
    // link — unlike archive and unlike "Close runtime", which both keep it. The persisted
    // Chudcode session file is the service's own store; deleting a Chudware conversation
    // never reaches into it.
    {
        const std::string closedId = st.conversations[idx].id;
        const ChudcodeSessionLink* link = chudcodeSessionLink(closedId);
        if (link && link->runtimeLive && chudcodeSessionServiceAvailable())
            st.rpc.call(kChudcodeCloseSessionMethod, chudcodeCloseSessionParams(closedId),
                        [](const core::RpcResult&) {}, 15000);
        chudcodeSessionLinkErase(closedId);
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

int forkConversation(AppState& st, int idx) {
    if (idx < 0 || idx >= (int)st.conversations.size()) return -1;

    const Conversation& source = st.conversations[idx];
    std::string base = source.title;
    int ordinal = 1;
    const std::string plainSuffix = " (fork)";
    if (base.size() >= plainSuffix.size() &&
        base.compare(base.size() - plainSuffix.size(), plainSuffix.size(), plainSuffix) == 0) {
        base.resize(base.size() - plainSuffix.size());
        ordinal = 2;
    } else if (!base.empty() && base.back() == ')') {
        const size_t marker = base.rfind(" (fork ");
        if (marker != std::string::npos && marker + 7 < base.size() - 1) {
            int parsed = 0;
            bool digits = true;
            for (size_t p = marker + 7; p + 1 < base.size(); ++p) {
                if (!std::isdigit(static_cast<unsigned char>(base[p]))) {
                    digits = false;
                    break;
                }
                parsed = parsed * 10 + (base[p] - '0');
            }
            if (digits && parsed >= 2) {
                base.resize(marker);
                ordinal = parsed + 1;
            }
        }
    }

    auto forkTitle = [&](int n) {
        return base + (n == 1 ? " (fork)" : " (fork " + std::to_string(n) + ")");
    };
    std::string title = forkTitle(ordinal);
    while (std::any_of(st.conversations.begin(), st.conversations.end(),
                       [&](const Conversation& c) { return c.title == title; }))
        title = forkTitle(++ordinal);

    Conversation fork;
    fork.id = newConvId(st);
    {
        // A fork owns full snapshots of both raw-log families. Copies are best effort:
        // conversation forking must still succeed if the filesystem refuses either one.
        namespace fs = std::filesystem;
        std::error_code ec;
        const fs::path sourceRaw = chatSubagentsDirPath(source.id);
        if (fs::exists(sourceRaw, ec) && !ec) {
            fs::copy(sourceRaw, chatSubagentsDirPath(fork.id), fs::copy_options::recursive, ec);
        }
        ec.clear();
        const fs::path sourceChatRaw = chatRawLogPath(source.id);
        if (fs::exists(sourceChatRaw, ec) && !ec) {
            ec.clear();
            fs::copy_file(sourceChatRaw, chatRawLogPath(fork.id), ec);
        }
    }
    fork.title = std::move(title);
    fork.docLink = source.docLink;
    fork.projectDir = source.projectDir;  // a fork belongs to the same project as its source
    fork.archived = false;
    fork.msgs = idx == st.activeConv ? st.chat : source.msgs;
    for (auto& msg : fork.msgs) {
        closeDetachedTurn(msg);
    }
    seedChatRawSnapshots(fork.id, fork.msgs);

    const int inserted = idx + 1;
    st.conversations.insert(st.conversations.begin() + inserted, std::move(fork));
    if (st.activeConv >= inserted) ++st.activeConv;
    switchConversation(st, inserted);
    return st.activeConv;
}

// (Conversations are switched from the Sessions picker in the Copilot header.)

// Probe the local OpenAI-compatible endpoint for its loaded models + reachability, so the
// picker can hide local models that are down. Throttled to ~5s; force bypasses the throttle.

}  // namespace ui
