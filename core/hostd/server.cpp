#include "server.h"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <filesystem>

#ifdef _WIN32
#include <process.h>  // _getpid
#else
#include <unistd.h>
#endif

#include "../../services/worker/cpp_core.h"
#include "hostd_log.h"

namespace hostd {

using nlohmann::json;

static long selfPid() {
#ifdef _WIN32
    return (long)_getpid();
#else
    return (long)::getpid();
#endif
}

static long long monoMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

Server::Server(aicad_ipc::IpcListener listener, std::string buildId)
    : listener_(std::move(listener)), buildId_(std::move(buildId)) {
    registry_.setEventSender(
        [this](int clientId, const std::string& line) { sendEventTo(clientId, line); });
    children_.setBroadcast([this](const std::string& line) {
        sendEventToAll(line);
        touch();  // a child exit restarts the linger countdown
    });
    touch();
}

void Server::sendEventToAll(const std::string& line) {
    std::vector<std::shared_ptr<ClientConn>> live;
    {
        std::lock_guard<std::mutex> lk(clientsMtx_);
        for (auto& [id, c] : clients_) live.push_back(c);
    }
    for (auto& c : live) {
        // try_lock, never block: a client mid-write (its own response, or another
        // fanout) must not stall event delivery to everyone else. A busy client
        // simply misses this push — it re-syncs on its next reconnect (which now
        // re-subscribes + re-lists). Same rationale as sendEventTo.
        std::unique_lock<std::mutex> wlk(c->writeMtx, std::try_to_lock);
        if (wlk.owns_lock()) c->conn.writeLine(line);
    }
}

void Server::sendEventTo(int clientId, const std::string& line) {
    std::shared_ptr<ClientConn> c;
    {
        std::lock_guard<std::mutex> lk(clientsMtx_);
        auto it = clients_.find(clientId);
        if (it == clients_.end()) return;
        c = it->second;
    }
    // try_lock (not lock_guard): if the client is mid-write we skip this push
    // rather than block the caller — event fanout runs on OTHER clients' dispatch
    // threads and on the accept loop, so blocking here on one wedged consumer
    // would stall unrelated work. The bounded SO_SNDTIMEO write is the second
    // backstop once we DO hold the lock.
    std::unique_lock<std::mutex> wlk(c->writeMtx, std::try_to_lock);
    if (wlk.owns_lock()) c->conn.writeLine(line);
}

std::string Server::detachSocketForFlockRelease() {
    // Close the listen fd so new connects fail, but leave the sock file for
    // SingleInstance to unlink when it drops the flock (T-PERF10).
    std::string path = listener_.releasePath();
    listener_.close(/*unlink_path=*/false);
    return path;
}

Server::~Server() {
    // If detachSocketForFlockRelease already ran, path is empty and close is a no-op
    // on the fd; otherwise fall back to the historical unlink-on-close behavior.
    listener_.close(/*unlink_path=*/true);
    std::vector<std::shared_ptr<ClientConn>> live;
    {
        std::lock_guard<std::mutex> lk(clientsMtx_);
        for (auto& [id, c] : clients_) live.push_back(c);
    }
    for (auto& c : live) c->conn.close();
    for (auto& c : live)
        if (c->thread.joinable()) c->thread.join();
}

void Server::touch() { status_.lastActivityMs = monoMs(); }

// Stale-binary self-drain backstop (Phase 8; plan Design A "Version &
// staleness"). Deploys are temp+RENAME (never cp over a live .so — SIGBUS), so
// an mtime change on the hostd binary or the loaded libaicad means a NEW file
// sits at that path: drain so existing clients finish, new connects are refused
// with daemon.draining, and the next client respawns the fresh build. Installers
// calling --stop are the primary path; this covers dev rebuilds.
void Server::staleBinaryTick() {
    static const long long intervalMs = [] {
        if (const char* s = std::getenv("AICAD_HOSTD_STALECHECK_S"); s && *s)
            return (long long)std::max(1, std::atoi(s)) * 1000;
        return 60000LL;
    }();
    const long long now = monoMs();
    if (now - lastStaleCheckMs_ < intervalMs) return;
    lastStaleCheckMs_ = now;
    namespace fs = std::filesystem;
    const std::string paths[] = {selfBinary_, core::cpp_core().loadedPath()};
    for (const std::string& p : paths) {
        if (p.empty()) continue;
        std::error_code ec;
        const auto t = fs::last_write_time(p, ec);
        if (ec) continue;  // transient (mid-rename) — recheck next tick
        const long long mt =
            std::chrono::duration_cast<std::chrono::milliseconds>(t.time_since_epoch())
                .count();
        auto [it, inserted] = watchBaselineMs_.emplace(p, mt);
        if (!inserted && it->second != mt && !status_.draining) {
            log().line("binary changed on disk (%s) — draining so clients respawn the "
                       "new build",
                       p.c_str());
            status_.draining = true;
        }
    }
}

bool Server::idleExpired(int lingerSecs) {
    if (status_.clients > 0 || status_.inflight > 0) return false;
    // Never idle-exit under a running mission: children are the daemon's work
    // even with zero clients (GUI-close continuity). Dirty docs do NOT pin the
    // daemon — the final autosave on exit covers them.
    if (children_.aliveCount() > 0) return false;
    return monoMs() - status_.lastActivityMs > (long long)lingerSecs * 1000;
}

void Server::run(int lingerSecs) {
    children_.start();
    while (!stopping_) {
        aicad_ipc::IpcConn conn = listener_.accept(500);
        if (conn.valid()) {
            if (status_.draining) {
                conn.writeLine(json{{"event", "daemon.draining"}}.dump());
                continue;  // conn closes on scope exit; client respawns the new binary
            }
            auto c = std::make_shared<ClientConn>();
            {
                std::lock_guard<std::mutex> lk(clientsMtx_);
                c->id = nextClientId_++;
                c->conn = std::move(conn);
                clients_[c->id] = c;
            }
            ++status_.clients;
            c->thread = std::thread([this, c] { clientLoop(c); });
            log().line("client #%d connected (%d total)", c->id, status_.clients.load());
        }
        // Expire stale edit-holds + emit doc.lease_change; docs whose GC grace
        // expired with no attached client are retired + closed here so dead
        // sessions can never pile zombie docs into the shared engine.
        for (const std::string& id : registry_.tick()) gcCloseDoc(id);
        staleBinaryTick();  // mtime watch on hostd binary + libaicad (self-drain)
        if (status_.draining && status_.clients == 0 && status_.inflight == 0) break;
        if (idleExpired(lingerSecs)) {
            log().line("idle for %ds with no clients — exiting", lingerSecs);
            break;
        }
    }
    // Every exit path (drain / idle / SIGTERM via stop()): stop the children
    // (TERM -> 5 s grace -> KILL) BEFORE main runs the final autosave, so the
    // saved state postdates the last agent edit. Daemon SIGKILL is covered by
    // each child's PDEATHSIG.
    children_.shutdown();
}

bool Server::gcCloseDoc(const std::string& docId, const char* reason) {
    if (status_.engineState.load() != 1) return false;
    // TOCTOU guard: tick() decided this doc was abandoned under the registry
    // lock, then released it before we got here. A client may have re-attached
    // in that window (GUI re-list -> attach_doc, or an implicit rw touch); closing
    // it now would yank a live doc out from under that client. Re-check under the
    // lock and bail if it's no longer abandoned.
    if (!registry_.isReapable(docId)) return false;
    // DISCARD, never retire: an abandoned doc (doc-GC grace expired) or an
    // explicitly-closed one is not a crash, so it is not preserved for recovery.
    // doc_close + onDocClosed purge the doc's autosave artifacts so it can never
    // resurrect. Recovery is reserved for crashes (see Autosave::recoverPriorSession).
    json result;
    std::string err;
    if (!core::cpp_core().call("doc_close", json{{"doc_id", docId}}, result, err)) {
        // Already gone from the engine (raced a real doc_close): still drop
        // registry state + artifacts so nothing lingers.
        log().line("doc-gc: doc_close(%s) failed: %s", docId.c_str(), err.c_str());
    }
    if (autosave_) autosave_->onDocClosed(docId);
    registry_.onDaemonClosedDoc(docId, reason);
    log().line("doc-gc: discarded doc %s (reason %s)", docId.c_str(), reason);
    return true;
}

void Server::clientLoop(std::shared_ptr<ClientConn> c) {
    std::string line;
    while (!stopping_ && c->conn.readLine(line, -1)) {
        json msg = json::parse(line, nullptr, false);
        if (msg.is_discarded()) continue;
        const int id = msg.value("id", -1);
        const std::string method = msg.value("method", "");
        ++status_.inflight;
        bool isError = false;
        json payload = dispatch(*c, method, msg.value("params", json::object()), isError);
        --status_.inflight;
        touch();
        json resp = {{"id", id}};
        resp[isError ? "error" : "result"] = std::move(payload);
        std::lock_guard<std::mutex> wlk(c->writeMtx);
        if (!c->conn.writeLine(resp.dump())) break;
    }
    {
        std::lock_guard<std::mutex> lk(clientsMtx_);
        c->thread.detach();  // we're on this thread; the map drop releases the struct
        clients_.erase(c->id);
    }
    --status_.clients;
    touch();
    log().line("client #%d (%s) disconnected (%d left)", c->id, c->kind.c_str(),
               status_.clients.load());
    registry_.onDisconnect(c->id, c->kind);
}

json Server::dispatch(ClientConn& c, const std::string& method, const json& params,
                      bool& isError) {
    const char* stateNames[] = {"loading", "ready", "failed"};
    if (method == "hello") {
        c.kind = params.value("client", "unknown");
        c.pid = params.value("pid", 0L);
        return json{{"proto", 1},
                    {"abi", core::cpp_core().abiVersion()},
                    {"git_rev", buildId_},
                    // "" while the engine loads or when the .so predates the
                    // aicad_build_id export — clients tolerate absence.
                    {"engine_build_id", core::cpp_core().buildId()},
                    {"daemon_pid", selfPid()},
                    {"state", stateNames[status_.engineState.load()]},
                    // Autosave-recovery rebind map (empty until recovery ran;
                    // daemon_status.recovery tells clients when to re-hello).
                    {"id_remap", autosave_ ? autosave_->idRemap() : json::object()},
                    // Retired docs awaiting an explicit recovery_open/discard —
                    // the GUI prompts the user when this is non-zero.
                    {"recovery_pending", autosave_ ? autosave_->orphanCount() : 0}};
    }
    if (method == "ping") return json{{"ok", true}};
    if (method == "daemon_status") {
        json st{{"state", stateNames[status_.engineState.load()]},
                {"clients", status_.clients.load()},
                {"inflight", status_.inflight.load()},
                {"draining", status_.draining.load()},
                {"git_rev", buildId_},
                {"engine_build_id", core::cpp_core().buildId()},
                {"recovery", !autosave_                  ? "off"
                             : autosave_->recoveryDone() ? "done"
                                                         : "pending"},
                {"recovery_pending", autosave_ ? autosave_->orphanCount() : 0},
                {"pid", selfPid()}};
        children_.statusInto(st);  // children count + mission ids
        return st;
    }
    if (method == "daemon_drain") {
        if (children_.aliveCount() > 0) {
            if (!params.value("force", false)) {
                isError = true;
                return json{{"code", "mission_active"},
                            {"message", "agent children are running; stop them or "
                                        "pass {\"force\":true} (aicad-hostd --stop --force)"}};
            }
            children_.stopAll();  // TERM -> grace -> KILL, reaped before we drain
        }
        status_.draining = true;
        log().line("drain requested by client #%d", c.id);
        return json{{"ok", true}};
    }

    // Orphan-tier recovery (docs retired by clean shutdown / doc-GC — never
    // auto-opened; the user or agent decides).
    if (method == "recovery_list") {
        if (!autosave_) return json{{"entries", json::array()}};
        return autosave_->recoveryList();
    }
    if (method == "recovery_discard") {
        if (!autosave_) return json{{"discarded", 0}};
        const bool all = params.value("all", false);
        const std::string id = params.value("id", std::string());
        if (!all && id.empty()) {
            isError = true;
            return json{{"code", "invalid_params"},
                        {"message", "pass {\"id\": ...} or {\"all\": true}"}};
        }
        return json{{"discarded", autosave_->discardOrphans(all ? "" : id)}};
    }
    if (method == "recovery_open") {
        if (status_.engineState.load() != 1) {
            isError = true;
            return json{{"code", "loading"}, {"message", "engine is still loading; retry"}};
        }
        if (!autosave_) return json{{"opened", json::array()}};
        std::vector<std::string> ids;
        if (params.value("all", false)) {
            const json list = autosave_->recoveryList();
            for (const auto& e : list["entries"]) ids.push_back(e.value("id", std::string()));
        } else if (params.contains("id") && params["id"].is_string()) {
            ids.push_back(params["id"].get<std::string>());
        }
        if (ids.empty()) {
            isError = true;
            return json{{"code", "invalid_params"},
                        {"message", "pass {\"id\": ...} or {\"all\": true}"}};
        }
        json opened = json::array();
        for (const std::string& id : ids) {
            const std::string path = autosave_->orphanPath(id);
            if (path.empty()) continue;
            json result;
            std::string err;
            // detach_path: the orphan artifact is deleted right after this open, so
            // the doc must forget it (and re-unique its name vs already-open docs).
            json p2 = json{{"path", path}, {"detach_path", true}};
            if (!core::cpp_core().call("doc_open", p2, result, err)) {
                log().line("recovery_open: doc_open(%s) failed: %s", path.c_str(),
                           err.c_str());
                continue;
            }
            // Route through the registry so the caller attaches rw and every
            // mirror gets doc.opened — same envelope as a client doc_open.
            registry_.postCall(c.id, c.kind, "doc_open", p2, result);
            const std::string newId = result.value("doc_id", std::string());
            // The original id (pre-crash) lives in the entry meta: record the
            // rebind so a reconnecting client's hello resolves its old handle, and
            // hand it back so callers (recover_parts) can alias without a re-hello.
            const std::string origId = autosave_->orphanMeta(id).value("doc_id", std::string());
            autosave_->noteRecoveredRemap(origId, newId);
            autosave_->discardOrphans(id);  // consumed — it lives in the engine now
            opened.push_back(json{{"id", id},
                                  {"doc_id", newId},
                                  {"original_doc_id", origId}});
        }
        return json{{"opened", opened}};
    }

    // Explicit user discard: the caller (GUI closing a tab) drops its attach and,
    // if no other client still holds the doc, we close it WITHOUT retiring it to
    // the orphan tier — a deliberately-closed part must not resurface on the next
    // launch. If an agent is still building it (another attach remains), this is a
    // plain detach: the doc lives on for that writer. Contrast doc-GC, which
    // retires abandoned docs for recovery.
    if (method == "doc_discard") {
        const std::string docId = params.value("doc_id", std::string());
        if (docId.empty()) {
            isError = true;
            return json{{"code", "invalid_params"}, {"message", "doc_id required"}};
        }
        json det;
        bool de = false;
        registry_.handleMethod(c.id, c.kind, "detach_doc", params, det, de);
        // isReapable == no client still attached: the writer (agent) is gone, so
        // this close is safe and the doc was truly abandoned.
        const bool discarded =
            registry_.isReapable(docId) && gcCloseDoc(docId, "discarded");
        return json{{"ok", true}, {"discarded", discarded}};
    }

    // Agent child supervision (agent_spawn/agent_stop/agent_list) — daemon
    // state only, no engine touch.
    {
        json result;
        if (children_.handleMethod(c.id, method, params, result, isError))
            return result;
    }

    // Doc-registry daemon methods (attach_doc/detach_doc/edit_hold/edit_release/
    // subscribe) — pure daemon state, no engine touch.
    {
        json result;
        if (registry_.handleMethod(c.id, c.kind, method, params, result, isError))
            return result;
    }

    // Engine passthrough.
    const int st = status_.engineState.load();
    if (st == 0) {
        isError = true;
        return json{{"code", "loading"}, {"message", "engine is still loading; retry"}};
    }
    if (st == 2) {
        isError = true;
        return json{{"code", "engine_failed"}, {"message", status_.engineErr}};
    }

    // Registry policy gate: ro-attach rejection, edit-hold busy_preview, overlap
    // CAS (base_revision injection when >1 writer). May mutate the params copy.
    json p = params;
    std::string attachWarning;
    {
        json policyErr;
        if (!registry_.preCall(c.id, c.kind, method, p, policyErr, attachWarning)) {
            isError = true;
            return policyErr;
        }
    }

    json result;
    std::string err;
    if (!core::cpp_core().call(method, p, result, err)) {
        isError = true;
        // Normalize: clients (CoreError mapping) rely on a "message" field. Keep
        // whatever in-band payload the engine produced ({"error":...}, OpError
        // {"code","message","detail"}), but guarantee message + code exist.
        json e = result.is_object() ? result : json::object();
        if (!e.contains("message"))
            e["message"] = !err.empty()           ? err
                           : e.contains("error")  ? e["error"].dump()
                                                  : std::string("engine error");
        if (e["message"].is_string() == false) e["message"] = e["message"].dump();
        if (!e.contains("code")) e["code"] = "engine_error";
        return e;
    }
    // Revision sniff, minted-doc implicit attach, event fanout, _foreign_edits.
    registry_.postCall(c.id, c.kind, method, p, result);
    // Closed means closed: an explicit doc_close deletes the doc's autosave
    // artifacts so it can never resurrect on the next daemon start.
    if (method == "doc_close" && autosave_)
        autosave_->onDocClosed(p.value("doc_id", std::string()));
    if (!attachWarning.empty() && result.is_object() && !result.contains("warning"))
        result["warning"] = attachWarning;
    return result;
}

}  // namespace hostd
