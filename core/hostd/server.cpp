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
    touch();
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

bool Server::idleExpired(int lingerSecs) const {
    if (status_.clients > 0 || status_.inflight > 0) return false;
    // Phase 5 adds: agent children alive / mission active / dirty docs -> autosave.
    return monoMs() - status_.lastActivityMs > (long long)lingerSecs * 1000;
}

void Server::run(int lingerSecs) {
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
        if (status_.draining && status_.clients == 0 && status_.inflight == 0) break;
        if (idleExpired(lingerSecs)) {
            log().line("idle for %ds with no clients — exiting", lingerSecs);
            break;
        }
    }
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
    // Phase 3: release this client's doc attachments/holds + emit lease_change.
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
                    {"id_remap", json::object()}};  // Phase 3: autosave recovery map
    }
    if (method == "ping") return json{{"ok", true}};
    if (method == "daemon_status") {
        return json{{"state", stateNames[status_.engineState.load()]},
                    {"clients", status_.clients.load()},
                    {"inflight", status_.inflight.load()},
                    {"draining", status_.draining.load()},
                    {"git_rev", buildId_},
                    {"pid", (long)getpid()}};
    }
    if (method == "daemon_drain") {
        // Phase 5 adds refusal while a mission is active (unless {"force":true}).
        status_.draining = true;
        log().line("drain requested by client #%d", c.id);
        return json{{"ok", true}};
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
    json result;
    std::string err;
    if (!core::cpp_core().call(method, params, result, err)) {
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
    return result;
}

}  // namespace hostd
