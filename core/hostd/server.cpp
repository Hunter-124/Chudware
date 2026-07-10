#include "server.h"

#include <chrono>

#include <unistd.h>

#include "../../services/worker/cpp_core.h"
#include "hostd_log.h"

namespace hostd {

using nlohmann::json;

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
        std::lock_guard<std::mutex> wlk(c->writeMtx);
        c->conn.writeLine(line);
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
    std::lock_guard<std::mutex> wlk(c->writeMtx);  // interleaves with response writes
    c->conn.writeLine(line);
}

Server::~Server() {
    listener_.close();
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
        registry_.tick();  // expire stale edit-holds + emit doc.lease_change
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
                    {"daemon_pid", (long)getpid()},
                    {"state", stateNames[status_.engineState.load()]},
                    // Autosave-recovery rebind map (empty until recovery ran;
                    // daemon_status.recovery tells clients when to re-hello).
                    {"id_remap", autosave_ ? autosave_->idRemap() : json::object()}};
    }
    if (method == "ping") return json{{"ok", true}};
    if (method == "daemon_status") {
        json st{{"state", stateNames[status_.engineState.load()]},
                {"clients", status_.clients.load()},
                {"inflight", status_.inflight.load()},
                {"draining", status_.draining.load()},
                {"git_rev", buildId_},
                {"recovery", !autosave_                  ? "off"
                             : autosave_->recoveryDone() ? "done"
                                                         : "pending"},
                {"pid", (long)getpid()}};
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
    if (!attachWarning.empty() && result.is_object() && !result.contains("warning"))
        result["warning"] = attachWarning;
    return result;
}

}  // namespace hostd
