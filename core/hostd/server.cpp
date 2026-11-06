#include "server.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <exception>
#include <filesystem>
#include <fstream>
#include <functional>
#include <optional>
#include <stdexcept>
#include <thread>

#ifdef _WIN32
#include <process.h>  // _getpid
#else
#include <sys/socket.h>
#include <unistd.h>
#endif

#if defined(__linux__) && defined(__GLIBC__)
#include <malloc.h>
#endif

#include "../../services/worker/cpp_core.h"
#include "../../services/ipc/ipc_private.h"
#include "../util/sha256.h"
#include "file_watch.h"
#include "hostd_log.h"
#include "mfg_lane.h"
#include "worker_bridge.h"

namespace hostd {

using nlohmann::json;
static void sendConnectionEvent(ClientConn& c, const std::string& line);

namespace {
std::mutex cacheSinkMutex;
Server* cacheSinkServer = nullptr;

// A worker manifest must never disclose paths outside the workspace. Resolve
// symlinks as well as lexical traversal before returning browser-usable paths.
bool workspaceManifestPath(const std::string& raw, const std::string& work,
                           std::string& relative) {
    if (raw.empty() || raw.find('\0') != std::string::npos) return false;
    std::filesystem::path root(work), path(raw);
    if (path.is_absolute() && (path == "/work" || raw.rfind("/work/", 0) == 0))
        path = root / path.lexically_relative("/work");
    else if (!path.is_absolute())
        path = root / path;
    std::error_code ec;
    path = std::filesystem::weakly_canonical(path, ec);
    if (ec) return false;
    auto part = path.begin();
    for (auto base = root.begin(); base != root.end(); ++base, ++part)
        if (part == path.end() || *part != *base) return false;
    const auto rel = path.lexically_relative(root);
    relative = rel.empty() ? "." : rel.generic_string();
    return true;
}

void workspaceRelativePaths(json& value, const std::string& work) {
    if (value.is_array()) {
        for (auto& entry : value) workspaceRelativePaths(entry, work);
    } else if (value.is_object()) {
        static constexpr const char* keys[] = {
            "path", "saved_path", "file_path", "folder_path", "origin_path", "source_path",
            "input_path", "output_path", "workspace_dir", "project_dir", "cwd",
            "image_path", "save_path", "model_path", "artifact_path", "out_path",
            "step_path", "stl_path", "brep_path", "gcode_path", "progress_path"
        };
        for (auto it = value.begin(); it != value.end();) {
            // Runtime journals and directories are actor-private, not workspace
            // paths or browser resume handles. Never disclose them in replies or
            // streamed events, including nested payloads.
            const auto& key = it.key();
            if (key == "omp_session_file" || key == "omp_session_dir" ||
                key == "session_file" || key == "sessionFile" ||
                key == "session_dir" || key == "sessionDir") {
                it = value.erase(it);
                continue;
            }
            auto& entry = it.value();
            if (key == "exported_files" || key == "reused_files") {
                json safe = json::array();
                if (entry.is_array()) {
                    for (const auto& item : entry) {
                        std::string relative;
                        if (item.is_string() &&
                            workspaceManifestPath(item.get_ref<const std::string&>(), work, relative))
                            safe.push_back(std::move(relative));
                    }
                }
                entry = std::move(safe);
                ++it;
                continue;
            }
            if (entry.is_string() && std::find(std::begin(keys), std::end(keys), key) != std::end(keys)) {
                const std::string& path = entry.get_ref<const std::string&>();
                if (!path.empty()) {
                    std::string relative;
                    if (!workspaceManifestPath(path, work, relative)) {
                        it = value.erase(it);
                        continue;
                    }
                    entry = std::move(relative);
                }
            } else workspaceRelativePaths(entry, work);
            ++it;
        }
    }
}
}

bool Server::installCacheRefreshSink() {
    {
        std::lock_guard<std::mutex> lock(cacheSinkMutex);
        cacheSinkServer = this;
    }
    if (core::cpp_core().setCacheRefreshSink(&Server::cacheRefreshThunk, nullptr)) return true;
    std::lock_guard<std::mutex> lock(cacheSinkMutex);
    if (cacheSinkServer == this) cacheSinkServer = nullptr;
    return false;
}

void Server::clearCacheRefreshSink() {
    {
        std::lock_guard<std::mutex> lock(cacheSinkMutex);
        if (cacheSinkServer != this) return;
        cacheSinkServer = nullptr;
    }
    core::cpp_core().setCacheRefreshSink(nullptr, nullptr);
}

void Server::cacheRefreshThunk(const char* docId, const char* responseJson, void*) {
    if (!docId || !responseJson) return;
    try {
        const json response = json::parse(responseJson);
        // Clearing the pointer drains callbacks before registry/server destruction.
        std::lock_guard<std::mutex> lock(cacheSinkMutex);
        if (cacheSinkServer) cacheSinkServer->registry_.publishCacheRefresh(docId, response);
    } catch (const std::exception& e) {
        log().line("cache refresh publication failed: %s", e.what());
    }
}

static long selfPid() {
#ifdef _WIN32
    return (long)_getpid();
#else
    return (long)::getpid();
#endif
}

static std::string authenticatedPeerIdentity(const ClientConn& c) {
#if defined(__linux__)
    struct ucred credential {};
    socklen_t size = sizeof(credential);
    if (c.conn.fd() >= 0 &&
        ::getsockopt(c.conn.fd(), SOL_SOCKET, SO_PEERCRED,
                     &credential, &size) == 0 &&
        size == sizeof(credential))
        return "uid:" + std::to_string(credential.uid);
#elif !defined(_WIN32)
    return "uid:" + std::to_string(::geteuid());
#endif
    // Windows named pipes are created with a same-user SID DACL and reject remote
    // clients. The server-side security boundary is therefore the local OS account,
    // never a name supplied in the hello payload.
    return "local-os-account";
}

static long long monoMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

// LONG-tier engine-call ceiling for modeling/rebuilds, sims and I/O. Agent turns
// are excluded: their transport monitors silence and a turn can take hours.
// 0 disables this tier for deployments with longer engine solves.
// Env: CHUDWARE_HOSTD_ENGINE_WATCHDOG_LONG_S.
static long long engineWatchdogLongMs() {
    static const long long ms = [] {
        if (const char* w = std::getenv("CHUDWARE_HOSTD_ENGINE_WATCHDOG_LONG_S"); w && *w)
            return (long long)std::max(0, std::atoi(w)) * 1000;
        return 60LL * 60 * 1000;  // 60 min
    }();
    return ms;
}

Server::Server(chudware_ipc::IpcListener listener, std::string buildId, std::string daemonStartId)
    : listener_(std::move(listener)), buildId_(std::move(buildId)),
      daemonStartId_(std::move(daemonStartId)) {
    registry_.setEventSender(
        [this](int clientId, const std::string& line) { sendEventTo(clientId, line); });
    children_.setBroadcast([this](const std::string& line) {
        sendEventToAll(line);
        touch();  // a child exit restarts the linger countdown
    });
    std::string mfgInitError;
    if (auto* mfg = chudware::mfg::tryLane(&mfgInitError)) {
        mfg->setEventSink(
            [this](const std::string& line) { sendEventToAll(line); });
        // Printer adapters (Moonraker/OctoPrint/PrusaLink) are Python; the lane reaches them
        // through this injected seam because chudware_manufacturing is a static library and
        // WorkerBridge only exists in the daemon. The lane still owns claims, approval, seq.
        mfg->setWorkerCall(
            [](const std::string& method, const nlohmann::json& params) {
                nlohmann::json result;
                std::string err;
                if (!WorkerBridge::general().call(method, params, result, err))
                    throw std::runtime_error(err.empty() ? "worker call failed" : err);
                return result;
            });
    } else {
        hostd::log().line("manufacturing lane unavailable: %s", mfgInitError.c_str());
    }
    touch();
    // Fast-tier (interactive RPC) watchdog ceiling; 0 disables that tier. The long tier's far
    // more generous ceiling is engineWatchdogLongMs() above; see engine_watchdog.h for the split.
    if (const char* w = std::getenv("CHUDWARE_HOSTD_ENGINE_WATCHDOG_S"); w && *w)
        engineWatchdogMs_ = (long long)std::max(0, std::atoi(w)) * 1000;
}

bool Server::configureCell(std::string state, std::string work,
                           const std::string& policyPath, std::string& err) {
    try {
        std::ifstream input(policyPath);
        if (!input) throw std::runtime_error("cell method policy unavailable");
        input >> cellPolicy_;
        if (!cellPolicy_.is_object() || cellPolicy_.empty())
            throw std::runtime_error("invalid cell method policy");
        for (const auto& item : cellPolicy_.items()) {
            const auto& p = item.value();
            if (!p.is_object() || p.size() != 3 ||
                (p.at("cell") != "allow" && p.at("cell") != "refuse") ||
                (p.at("right") != "read" && p.at("right") != "write" &&
                 p.at("right") != "run" && p.at("right") != "manage") ||
                (p.at("scope") != "workspace" && p.at("scope") != "actor"))
                throw std::runtime_error("invalid cell method classification: " + item.key());
        }
        cellState_ = std::filesystem::canonical(state).string();
        cellWork_ = std::filesystem::canonical(work).string();
        if (const char* id = std::getenv("CHUDWARE_CELL_ID")) {
            cellId_ = id;
            if (cellId_.size() != 32 || !std::all_of(cellId_.begin(), cellId_.end(),
                [](unsigned char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); }))
                throw std::runtime_error("invalid trusted cell identity");
        }
        const auto run = std::filesystem::path(cellState_) / "run";
        if (!chudware_ipc::privateDir(run.string()))
            throw std::runtime_error("private operator directory unavailable");
        operatorListener_ = std::make_unique<chudware_ipc::IpcListener>();
        if (!operatorListener_->listen((run / "operator.sock").string(), err)) return false;
        cell_ = true;
        return true;
    } catch (const std::exception& e) {
        err = e.what();
        return false;
    }
}

json Server::cellContext(const ClientConn& c) {
    std::lock_guard<std::mutex> lock(cellMtx_);
    return json{{"cell", true}, {"workspace_key", workspaceKey_}, {"actor_key", c.actorKey},
                {"actor_hex", c.actorHex}, {"right", c.right}, {"epoch", c.epoch},
                {"client_id", c.id}};
}

json Server::cellHello(ClientConn& c, const json& params, bool& isError) {
    const auto refuse = [&](const char* code) {
        isError = true;
        return json{{"code", code}, {"message", code}};
    };
    const bool operatorHello = params.contains("operator") && params["operator"] == true &&
                               params.value("client", std::string()) == "control";
    if (operatorHello) {
        if (!c.gateway || !c.operatorListener || c.helloDone || params.contains("principal"))
            return refuse("cell_refused");
        std::lock_guard<std::mutex> admission(clientsMtx_);
        c.operatorControl = true;
        c.controlOnly = true;
        c.helloDone = true;
        return json::object();
    }
    const bool principalControl = c.gateway && params.value("client", std::string()) == "control";
    if (c.operatorControl || params.contains("operator") ||
        (c.operatorListener && !principalControl)) return refuse("cell_refused");
    std::unique_lock<std::mutex> transition(principalMtx_, std::defer_lock);
    if (c.gateway) transition.lock();
    std::unique_lock<std::mutex> lock(cellMtx_);
    if (c.gateway) {
        const auto p = params.find("principal");
        if (p == params.end() || !p->is_object()) return refuse("cell_refused");
        const std::string actor = p->value("actor_key", std::string());
        const std::string workspace = p->value("workspace_key", std::string());
        const std::string right = p->value("right", std::string());
        const std::string nonce = p->value("connection_nonce", std::string());
        if (actor.empty() || actor.size() > 128 || workspace.empty() || workspace.size() > 256 ||
            nonce.empty() || nonce.size() > 256 || !p->contains("epoch") ||
            !(*p)["epoch"].is_number_integer() ||
            (right != "owner" && right != "manager" && right != "editor" && right != "viewer"))
            return refuse("cell_refused");
        const long long epoch = p->at("epoch").get<long long>();
        if (epoch < 0 || (!workspaceKey_.empty() && workspace != workspaceKey_))
            return refuse("cell_refused");
        if (c.helloDone) {
            if (c.actorKey != actor || c.epoch != epoch || c.right != right)
                return refuse("cell_refused");
        } else if (connectionNonces_.count(nonce)) return refuse("cell_refused");
        auto& a = actors_[actor];
        if (epoch < a.epoch) return refuse("revoked");
        if (!principalControl) {
            if (a.revoked && epoch <= a.epoch) return refuse("revoked");
            if (epoch == a.epoch && !a.right.empty() && a.right != right)
                return refuse("revoked");
            if (epoch > a.epoch && a.epoch >= 0) {
                const json owner{{"cell", true}, {"workspace_key", workspace},
                    {"actor_key", actor}, {"actor_hex", a.hex}, {"right", a.right},
                    {"epoch", a.epoch}, {"client_id", c.id}};
                // Fence the old actor socket before publishing the new authority.
                // Fresh hello cannot outrun asynchronous gateway revocation.
                a.revoked = true;
                lock.unlock();
                retireActorGeneration(actor, epoch - 1, owner, -1);
                lock.lock();
            }
        }
        a.key = actor;
        a.hex = chudware::util::sha256_hex(actor).substr(0, 32);
        if (!a.listener && !principalControl) {
            const auto directory = std::filesystem::path(cellState_) / "actors" / a.hex / "run";
            if (!chudware_ipc::privateDir(directory.parent_path().string()) ||
                !chudware_ipc::privateDir(directory.string())) return refuse("cell_refused");
            a.listener = std::make_shared<chudware_ipc::IpcListener>();
            std::string err;
            if (!a.listener->listen((directory / "hostd.sock").string(), err)) {
                a.listener.reset();
                return refuse("cell_refused");
            }
        }
        // A control hello authenticates the command channel; only the command
        // itself may change authority or make a revoked actor usable again.
        if (!principalControl) {
            a.epoch = epoch;
            a.right = right;
            a.revoked = false;
        }
        workspaceKey_ = workspace;
        connectionNonces_[nonce] = true;
        c.actorKey = a.key;
        c.actorHex = a.hex;
        c.right = right;
        c.epoch = epoch;
    } else {
        // Internal authority derives exclusively from the accepting actor socket.
        const auto a = actors_.find(c.actorKey);
        if (a == actors_.end() || a->second.revoked || a->second.epoch != c.epoch)
            return refuse("revoked");
        c.right = a->second.right;
    }
    c.helloDone = true;
    c.userId = c.actorKey;
    c.userDisplay = c.actorKey;
    if (principalControl) c.controlOnly = true;
    else {
        c.writerGroup = workspaceKey_ + ":" + c.actorKey;
        registry_.setWriterGroup(c.id, c.writerGroup);
        registry_.setCellPrincipal(c.id, c.actorKey, c.right == "viewer");
    }
    return json::object();
}

json Server::changePrincipal(ClientConn& c, const std::string& method,
                             const json& params, bool& isError) {
    if (!c.gateway || !c.helloDone || c.operatorControl || c.actorKey.empty()) {
        isError = true;
        return json{{"code", "cell_refused"}, {"message", "gateway control required"}};
    }
    const std::string actor = params.value("actor_key", std::string());
    const long long epoch = params.at("epoch").get<long long>();
    const std::string right = method == "principal_update"
                                  ? params.value("right", std::string()) : std::string();
    if (actor.empty() || epoch < 0 || (method == "principal_update" &&
        right != "owner" && right != "manager" && right != "editor" && right != "viewer")) {
        isError = true;
        return json{{"code", "bad_request"}, {"message", "invalid principal update"}};
    }
    std::lock_guard<std::mutex> transition(principalMtx_);
    const bool revoke = method == "principal_revoke";
    const long long revokedThrough = revoke ? epoch : epoch - 1;
    json owner;
    {
        std::lock_guard<std::mutex> lock(cellMtx_);
        auto& a = actors_[actor];
        if (epoch < a.epoch) {
            isError = true;
            return json{{"code", "revoked"}, {"message", "stale principal update"}};
        }
        if (epoch == a.epoch && a.revoked == revoke && (revoke || a.right == right)) {
            if (c.actorKey == actor) c.retireAfterReply = true;
            return json{{"ok", true}, {"epoch", epoch}};
        }
        a.key = actor;
        a.hex = chudware::util::sha256_hex(actor).substr(0, 32);
        owner = json{{"cell", true}, {"workspace_key", workspaceKey_}, {"actor_key", actor},
                     {"actor_hex", a.hex}, {"right", a.right}, {"epoch", a.epoch},
                     {"client_id", c.id}};
        a.epoch = epoch;
        a.revoked = true;
    }
    retireActorGeneration(actor, revokedThrough, owner, c.id);
    {
        std::lock_guard<std::mutex> lock(cellMtx_);
        auto& a = actors_.at(actor);
        a.revoked = revoke;
        if (!right.empty()) a.right = right;
    }
    if (c.actorKey == actor) c.retireAfterReply = true;
    return json{{"ok", true}, {"epoch", epoch}};
}

void Server::retireActorGeneration(const std::string& actor, long long revokedThrough,
                                  const json& owner, int controlClient) {
    std::vector<std::string> runs;
    std::vector<std::shared_ptr<ClientConn>> close;
    {
        std::lock_guard<std::mutex> lock(clientsMtx_);
        std::lock_guard<std::mutex> identityLock(cellMtx_);
        for (const auto& [run, key] : runOwners_) if (key == actor) runs.push_back(run);
        for (const auto& [id, conn] : clients_)
            if (conn->actorKey == actor && conn->epoch <= revokedThrough) close.push_back(conn);
    }
    for (const auto& conn : close) {
        for (const auto& run : runs)
            sendConnectionEvent(*conn, json{{"event", "agent.event"},
                {"data", {{"kind", "run_stopped"}, {"run_id", run}, {"reason", "revoked"},
                          {"actor_key", actor}}}}.dump());
        if (conn->id != controlClient) {
            registry_.onDisconnect(conn->id, conn->kind);
            conn->conn.shutdown();
        }
    }
    WorkerBridge::revokeActor(actor, workspaceKey_, revokedThrough);
    if (status_.engineState == 1 && owner.value("epoch", -1LL) >= 0) {
        json result;
        std::string err;
        const json stop{{"_cell_ctx", owner}, {"_cell_revoke_through", revokedThrough}};
        json simStop = stop;
        simStop["job_id"] = "";
        core::cpp_core().call("sim_cancel", simStop, result, err);
    }
}

bool Server::cellAdmit(ClientConn& c, const std::string& method, json& params, json& error) {
    const auto fail = [&](const char* code) {
        error = json{{"code", code}, {"message", code}};
        return false;
    };
    {
        std::lock_guard<std::mutex> lock(cellMtx_);
        const auto actor = actors_.find(c.actorKey);
        if (!c.helloDone || actor == actors_.end()) return fail("cell_refused");
        if (actor->second.revoked || c.epoch != actor->second.epoch) return fail("revoked");
        c.right = actor->second.right;
    }
    const auto policy = cellPolicy_.find(method);
    if (policy == cellPolicy_.end() || policy->at("cell") != "allow")
        return fail("cell_refused");
    const std::string right = policy->at("right");
    if ((c.right == "viewer" && right != "read") ||
        (right == "manage" && c.right != "manager" && c.right != "owner"))
        return fail("forbidden_right");
    // Canonical path confinement supplements the namespace boundary, including symlinks.
    static const std::vector<std::string> pathKeys = {
        "path", "file_path", "folder_path", "output_path", "input_path", "source_path",
        "target_path", "workspace_dir", "project_dir", "session_file", "omp_session_file",
        "omp_session_dir",
        "aicm_path", "brep_path", "stl_path", "step_path", "image_path", "save_path",
        "model_path", "artifact_path", "working_dir", "cwd", "directory", "root",
        "out_path", "dest", "progress_path", "prompts_dir", "output_dir", "input_dir"
    };
    const auto under = [](const std::filesystem::path& p, const std::filesystem::path& root) {
        auto a = p.begin();
        for (auto b = root.begin(); b != root.end(); ++b, ++a)
            if (a == p.end() || *a != *b) return false;
        return true;
    };
    const auto own = std::filesystem::path(cellState_) / "actors" / c.actorHex;
    std::function<bool(std::filesystem::path&, unsigned)> resolveFilePath =
        [&](std::filesystem::path& p, unsigned links) {
            const auto normalized = p.lexically_normal();
            if (!under(normalized, cellWork_) && !under(normalized, own)) return false;
            std::filesystem::path prefix;
            for (auto it = p.begin(); it != p.end(); ++it) {
                prefix /= *it;
                std::error_code ec;
                const auto status = std::filesystem::symlink_status(prefix, ec);
                if (status.type() == std::filesystem::file_type::not_found) break;
                if (ec) return false;
                if (!std::filesystem::is_symlink(status)) continue;
                if (links == 40) return false;
                auto target = std::filesystem::read_symlink(prefix, ec);
                if (ec) return false;
                if (!target.is_absolute()) target = prefix.parent_path() / target;
                auto remainder = it;
                for (++remainder; remainder != p.end(); ++remainder) target /= *remainder;
                // Unlike weakly_canonical, inspect dangling links too: their
                // targets can be outside the cell and invisible in its namespace.
                p = std::move(target);
                return resolveFilePath(p, links + 1);
            }
            p = normalized;
            return true;
        };
    std::function<bool(json&, bool, unsigned)> paths = [&](json& value, bool path, unsigned depth) {
        if (value.is_object()) {
            for (auto& field : value.items()) {
                const bool isPath = std::find(pathKeys.begin(), pathKeys.end(), field.key()) != pathKeys.end();
                if (!paths(field.value(), isPath, depth + 1)) return false;
            }
        } else if (value.is_array()) {
            for (auto& item : value) if (!paths(item, path, depth + 1)) return false;
        } else if (path && value.is_string() && !value.get_ref<const std::string&>().empty()) {
            const auto& raw = value.get_ref<const std::string&>();
            if (raw.find('\0') != std::string::npos) return false;
            std::filesystem::path p(raw);
            if (depth > 1) {
                // Paths embedded in document content are persisted references,
                // not file-operation arguments. Preserve their relative spelling;
                // the engine resolves them and enforces symlink confinement.
                if (p.is_absolute()) return false;
                for (const auto& component : p)
                    if (component == "..") return false;
                return true;
            }
            if (!c.gateway && p.is_absolute() &&
                (p == "/actor" || p.string().rfind("/actor/", 0) == 0))
                p = own / p.lexically_relative("/actor");
            if (!p.is_absolute()) p = std::filesystem::path(cellWork_) / p;
            if (!resolveFilePath(p, 0)) return false;
            value = p.string();
        }
        return true;
    };
    if (!paths(params, false, 0)) return fail("cell_refused");
    json context = cellContext(c);
    // Reject a recorded foreign owner before acquiring a worker lane. Unknown
    // handles stay in the requester's own lane, whose persistent actor registry
    // resolves them; this also permits session resumption after hostd restarts.
    {
        std::lock_guard<std::mutex> lock(cellMtx_);
        std::string handleActor;
        for (const char* key : {"run_id", "session_id", "job_id", "dag_id"}) {
            const auto id = params.find(key);
            if (id == params.end() || !id->is_string() || id->get_ref<const std::string&>().empty()) continue;
            const auto owner = handleOwners_.find(std::string(key) + ":" + id->get<std::string>());
            if (owner == handleOwners_.end()) continue;
            if (!handleActor.empty() && handleActor != owner->second) return fail("not_found");
            handleActor = owner->second;
            if (owner->second == c.actorKey) continue;
            // Cross-actor authority is stop-only: no content reads, steering or session writes.
            const bool stop = method == "agent_cancel" || method == "sim_cancel" ||
                              method == "reveng_cancel_splat";
            if (!stop || (c.right != "manager" && c.right != "owner")) return fail("not_found");
            const auto actor = actors_.find(owner->second);
            if (actor == actors_.end() || actor->second.revoked) return fail("not_found");
            context["actor_key"] = actor->second.key;
            context["actor_hex"] = actor->second.hex;
            context["epoch"] = actor->second.epoch;
            context["right"] = actor->second.right;
            context["control_delegate"] = json{{"actor_key", c.actorKey},
                                               {"right", "manager"}, {"action", "cancel"}};
        }
    }
    params["_cell_ctx"] = std::move(context);
    return true;
}

void Server::rememberCellHandles(const ClientConn& c, const json& params, const json& result) {
    if (!cell_ || !result.is_object()) return;
    // A transport-successful worker reply can still be a semantic refusal.
    // Never let a guessed handle in such a request poison the ownership ledger.
    if (const auto ok = result.find("ok"); ok != result.end() && *ok == false) return;
    std::lock_guard<std::mutex> lock(cellMtx_);
    for (const char* key : {"run_id", "session_id", "job_id", "dag_id"}) {
        for (const json* object : {&params, &result}) {
            const auto value = object->find(key);
            if (value != object->end() && value->is_string() && !value->get_ref<const std::string&>().empty())
                handleOwners_.try_emplace(std::string(key) + ":" + value->get<std::string>(),
                    params.at("_cell_ctx").value("actor_key", c.actorKey));
        }
    }
}

static void sendConnectionEvent(ClientConn& c, const std::string& line) {
    // Dispatch/event callbacks never run while owning writeMtx: the connection
    // loop locks it only AFTER dispatch returns. Waiting here cannot deadlock on
    // a response needing the engine/registry lock. Bound both contention and the
    // transport write (IpcConn's send timeout) so a wedged spectator cannot stall
    // another writer indefinitely, without silently losing a live doc update.
    std::unique_lock<std::timed_mutex> wlk(c.writeMtx, std::defer_lock);
    const bool locked = wlk.try_lock_for(std::chrono::seconds(2));
    if (locked && c.conn.writeLine(line)) return;
    if (!c.eventWriteFailureLogged.exchange(true))
        log().line("client #%d: event delivery failed (%s); disconnecting for re-sync",
                   c.id, locked ? "socket write" : "write mutex timeout");
    // A timed-out/partial event must not leave a client silently watching stale
    // geometry (nor a partial JSON line). Reconnect re-subscribes and re-lists.
    c.conn.shutdown();
}

void Server::sendEventToAll(const std::string& line) {
    if (cell_) return;  // Broadcast classes are desktop/operator-only, never tenant data.
    std::vector<std::shared_ptr<ClientConn>> live;
    {
        std::lock_guard<std::mutex> lk(clientsMtx_);
        for (auto& [id, c] : clients_) live.push_back(c);
    }
    for (auto& c : live) sendConnectionEvent(*c, line);
}

void Server::sendEventTo(int clientId, const std::string& line) {
    std::shared_ptr<ClientConn> c;
    {
        std::lock_guard<std::mutex> lk(clientsMtx_);
        auto it = clients_.find(clientId);
        if (it == clients_.end()) return;
        c = it->second;
    }
    if (cell_) {
        std::lock_guard<std::mutex> lock(cellMtx_);
        const auto actor = actors_.find(c->actorKey);
        if (!c->helloDone || actor == actors_.end() || actor->second.revoked ||
            actor->second.epoch != c->epoch) return;
    }
    if (cell_ && c->gateway) {
        auto event = json::parse(line, nullptr, false);
        if (!event.is_object()) return;
        workspaceRelativePaths(event, cellWork_);
        sendConnectionEvent(*c, event.dump());
    } else sendConnectionEvent(*c, line);
}

// Live callback context. It lives on the dispatch stack for the duration of the synchronous
// engine call; the C sink is thread-local and cleared before this object leaves scope.
namespace {
// renewWatchdog: each streamed progress event restarts the call's long-tier watchdog clock,
// turning that wall-clock ceiling into a silence bound for a solve that reports progress.
struct StreamCtx { hostd::Server* srv; int clientId; bool renewWatchdog = false; };
}

void Server::sendAgentEvent(int clientId, const char* eventJson) {
    if (!eventJson) return;
    std::string line = "{\"event\":\"agent.event\",\"data\":";
    line += eventJson;
    line += "}";
    if (!cell_) {
        sendEventTo(clientId, line);
        return;
    }
    std::vector<std::shared_ptr<ClientConn>> live;
    {
        std::lock_guard<std::mutex> lock(clientsMtx_);
        for (const auto& [id, conn] : clients_) live.push_back(conn);
    }
    std::string actor;
    {
        std::lock_guard<std::mutex> lock(cellMtx_);
        for (const auto& conn : live) if (conn->id == clientId) actor = conn->actorKey;
        const auto event = json::parse(eventJson, nullptr, false);
        if (event.is_object() && event.contains("run_id") && event["run_id"].is_string()) {
            const std::string run = event["run_id"];
            handleOwners_.try_emplace("run_id:" + run, actor);
            const std::string kind = event.value("kind", std::string());
            if (kind == "run_started") runOwners_[run] = actor;
            if (kind == "run_finished" || kind == "run_stopped") runOwners_.erase(run);
        }
    }
    if (actor.empty()) return;
    for (const auto& conn : live) {
        bool owns;
        {
            std::lock_guard<std::mutex> lock(cellMtx_);
            owns = conn->helloDone && conn->actorKey == actor;
        }
        if (owns) sendEventTo(conn->id, line);
    }
}

void Server::streamThunk(const char* eventJson, void* ud) {
    auto* ctx = static_cast<StreamCtx*>(ud);
    if (!ctx || !ctx->srv) return;
    if (ctx->renewWatchdog) {
        // A bounded optimization legitimately runs past the long tier's wall-clock ceiling;
        // what proves it is not hung is that it keeps emitting progress. A solve that falls
        // silent for the whole ceiling is still reclaimed exactly as before.
        std::lock_guard<std::mutex> lk(ctx->srv->inflightMtx_);
        auto it = ctx->srv->inflight_.find(ctx->clientId);
        if (it != ctx->srv->inflight_.end()) it->second.startMs = monoMs();
    }
    ctx->srv->sendAgentEvent(ctx->clientId, eventJson);
}

std::string Server::detachSocketForFlockRelease() {
    // Close the listen fd so new connects fail, but leave the sock file for
    // SingleInstance to unlink when it drops the flock.
    std::string path = listener_.releasePath();
    listener_.close(/*unlink_path=*/false);
    return path;
}

Server::~Server() {
    clearCacheRefreshSink();
    // Client loops must not re-enter readLine once we start tearing down.
    stopping_ = true;
    // The watchdog thread polls stopping_ every 500ms; join it before tearing down.
    if (watchdog_.joinable()) watchdog_.join();
    // If detachSocketForFlockRelease already ran, path is empty and close is a no-op
    // on the fd; otherwise fall back to the historical unlink-on-close behavior.
    listener_.close(/*unlink_path=*/true);
    std::vector<std::shared_ptr<ClientConn>> live;
    std::vector<std::thread> threads;
    {
        // Take the client threads out under the same lock clientLoop's disconnect tail
        // detaches under. Checking joinable() and then joining raced that detach: a loop
        // woken by the shutdown below detached itself between the two, join() threw
        // std::system_error and every idle exit with a client still connected aborted
        // with a core dump (live: a headless agent turn's MCP socket).
        std::lock_guard<std::mutex> lk(clientsMtx_);
        for (auto& [id, c] : clients_) {
            live.push_back(c);
            if (c->thread.joinable()) threads.push_back(std::move(c->thread));
        }
    }
    // Interrupt any thread parked in readLine()'s poll() BEFORE joining. A bare close()
    // does NOT wake a thread already blocked in poll() on Linux, so the join below hung
    // until systemd's TimeoutStopSec fired and SIGABRT'd the daemon (the "crashed on the
    // other end" restart hang). shutdown() delivers POLLHUP so readLine returns at once;
    // close() after the join reclaims the fd.
    for (auto& c : live) c->conn.shutdown();
    for (auto& t : threads) t.join();
    for (auto& c : live) c->conn.close();
}

void Server::touch() { status_.lastActivityMs = monoMs(); }

// A replaced hostd or engine starts a soft drain. Existing sessions finish on
// the old engine; only their already-owned agent workers may open the fresh MCP
// sockets needed by later turns. Independent clients wait for the next daemon.
void Server::staleBinaryTick(bool initial) {
    static const long long intervalMs = [] {
        if (const char* s = std::getenv("CHUDWARE_HOSTD_STALECHECK_S"); s && *s)
            return (long long)std::max(1, std::atoi(s)) * 1000;
        return 60000LL;
    }();
    const long long now = monoMs();
    if (!initial && now - lastStaleCheckMs_ < intervalMs) return;
    if (!initial) lastStaleCheckMs_ = now;
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
            beginDrain(/*deadlineSecs=*/0, "stale_binary", p);  // soft: live sessions finish
        }
    }
}

void Server::beginDrain(int deadlineSecs, std::string reason, std::string changedPath) {
    std::lock_guard<std::mutex> lk(drainMtx_);
    // A late stale check must never downgrade an explicit stop or refresh the
    // eligibility snapshot after the drain has already begun.
    if (deadlineSecs <= 0 && status_.draining.load()) return;
    if (deadlineSecs <= 0 && reason == "stale_binary" && !cell_)
        drainAgentPeers_ = WorkerBridge::snapshotAgentPeers();
    else
        drainAgentPeers_.clear();
    drainReason_ = std::move(reason);
    if (!changedPath.empty()) drainChangedPath_ = std::move(changedPath);
    drainDeadlineMs_ = deadlineSecs > 0 ? monoMs() + (long long)deadlineSecs * 1000 : 0;
    status_.draining = true;
}

std::string Server::drainReason() const {
    std::lock_guard<std::mutex> lk(drainMtx_);
    return status_.draining.load() ? drainReason_ : std::string();
}

json Server::drainRefusal() {
    std::string reason, changedPath;
    {
        std::lock_guard<std::mutex> lk(drainMtx_);
        reason = drainReason_;
        changedPath = drainChangedPath_;
    }
    const int active = pinningClients(guiExitGraceSecs_);
    json r{{"code", "daemon_draining"},
           {"reason", reason},
           {"daemon_pid", selfPid()},
           {"clients", status_.clients.load()},
           {"active_clients", active},
           {"children", children_.aliveCount()},
           {"hard_stop", drainDeadlineMs_.load() > 0}};
    if (reason == "stale_binary") {
        const std::string file =
            changedPath.empty() ? std::string("an engine binary")
                                : std::filesystem::path(changedPath).filename().string();
        r["changed_path"] = changedPath;
        r["message"] = "The engine changed on disk (" + file +
                       "). This daemon runs the old engine and is draining. Open sessions "
                       "continue on the old engine. New independent clients cannot connect until " +
                       std::to_string(active) + " active session(s) end.";
        r["hint"] = "Restart Chudware to load the new engine. Or wait until the open sessions "
                    "end, then connect again. Do not start a separate in-process engine: it "
                    "does not share documents.";
    } else {
        r["message"] = std::string("This daemon is stopping (") +
                       (reason.empty() ? "stop_requested" : reason) +
                       "). It accepts no new clients.";
        r["hint"] = "Wait until the daemon exits, then connect again. The next connection "
                    "starts a new daemon.";
    }
    return r;
}

bool Server::admitWhileDraining(chudware_ipc::IpcConn& conn, std::string& firstLine,
                                bool allowControl) {
    // Every client sends hello as soon as it connects, so a short bound is enough. It
    // also bounds how long one silent connect can hold the accept loop.
    std::string line;
    if (!conn.readLine(line, 1000)) {
        log().line("draining: a new connection sent no request in 1 s — closed it");
        conn.close();
        return false;
    }
    const json msg = json::parse(line, nullptr, false);
    const bool isObj = msg.is_object();
    const int id = isObj && msg.contains("id") && msg["id"].is_number_integer()
                       ? msg["id"].get<int>()
                       : -1;
    const std::string method = isObj && msg.contains("method") && msg["method"].is_string()
                                   ? msg["method"].get<std::string>()
                                   : std::string();
    std::string kind;
    if (isObj && msg.contains("params") && msg["params"].is_object()) {
        const json& p = msg["params"];
        if (p.contains("client") && p["client"].is_string()) kind = p["client"].get<std::string>();
    }
    // An operator control client (`chudware-hostd --status` / `--stop`) is admitted: it
    // reports the drain, or turns a soft drain into a hard stop. It holds no document, and
    // it disconnects at once, so it never extends the drain.
    if (allowControl && method == "hello" && kind == "control") {
        firstLine = std::move(line);
        return true;
    }
    const bool guiLane = kind == "gui" || kind == "gui-background";
    if (!cell_ && method == "hello" && (kind == "mcp" || guiLane)) {
        const long peerPid = conn.peerPid();
        bool admit = false;
        {
            std::lock_guard<std::mutex> lk(drainMtx_);
            admit = drainReason_ == "stale_binary" && drainDeadlineMs_.load() == 0 &&
                    (guiLane || WorkerBridge::ownsAgentPeer(drainAgentPeers_, peerPid));
        }
        // A GUI opens side connections (agent answer/steer/cancel, background queries)
        // beside its primary one. They belong to an open session, not an independent
        // client: the app's own `gui` connection already pins this drain.
        if (admit && guiLane) {
            admit = false;
            if (peerPid > 0) {
                std::lock_guard<std::mutex> lk(clientsMtx_);
                for (const auto& [id, client] : clients_)
                    if (client->guiPeerPid.load() == peerPid) { admit = true; break; }
            }
        }
        if (admit) {
            firstLine = std::move(line);
            return true;
        }
    }
    json refusal = drainRefusal();
    log().line("draining (%s): refused new %s client (%s)",
               refusal.value("reason", std::string()).c_str(),
               kind.empty() ? "unknown" : kind.c_str(), method.c_str());
    // The request was read, so closing after this reply is a clean EOF for the client,
    // not a reset. The client reads the reply first. The daemon.draining event comes
    // first: a client that cannot match the reply id still sees why it was refused.
    conn.writeLine(json{{"event", "daemon.draining"},
                        {"reason", refusal.value("reason", std::string())}}
                       .dump());
    conn.writeLine(json{{"id", id}, {"error", std::move(refusal)}}.dump());
    conn.close();
    return false;
}

json Server::cellCensus(bool commitExit, bool idleExit) {
    WorkerBridge::Activity workers;
    std::unique_lock<std::mutex> probe(cellCensusMtx_, std::try_to_lock);
    if (probe.owns_lock()) workers = WorkerBridge::activity();
    else workers.unknown = 1;
    std::uint64_t native[3]{};
    // Worker parents precede native children; task units include final publication.
    const bool nativeComplete = status_.engineState.load() == 1 &&
                                core::cpp_core().activity(native);
    std::lock_guard<std::mutex> admission(clientsMtx_);
    std::uint64_t sessions = 0;
    for (const auto& [id, client] : clients_)
        if (!client->controlOnly) ++sessions;
    const auto rpc = static_cast<std::uint64_t>(std::max(0, status_.inflight.load()));
    const bool complete = nativeComplete && workers.unknown == 0;
    const std::uint64_t active = sessions + rpc + native[0] + native[1] + native[2] +
                                 workers.busy + workers.background + workers.unknown;
    const bool quiescent = complete && active == 0;
    if (commitExit && quiescent && controlRepliesPending_.load() == 0 &&
        (idleExit || status_.draining || (cellAdmissionClosed_ && cellExitWhenQuiescent_))) {
        if (!cellAdmissionClosed_) { cellAdmissionClosed_ = true; ++cellGeneration_; }
        cellExiting_ = true;
    }
    return json{{"schema", 1}, {"admission", cellAdmissionClosed_ ? "closed" : "open"},
                {"generation", cellGeneration_}, {"sessions", sessions}, {"rpc_inflight", rpc},
                {"native", {{"cache_verification", native[0]}, {"profiler", native[1]},
                            {"dirty_documents", native[2]}}},
                {"workers", {{"busy", workers.busy}, {"background", workers.background},
                             {"unknown", workers.unknown}}},
                {"active", active}, {"complete", complete}, {"quiescent", quiescent},
                {"exiting", cellExiting_}};
}

json Server::cellOperator(ClientConn& c, const std::string& method,
                          const json& params, bool& isError) {
    const auto refuse = [&](const char* code) {
        isError = true;
        return json{{"code", code}, {"message", code}};
    };
    if (!c.gateway || !c.operatorControl || !c.helloDone) return refuse("cell_refused");
    if (method == "ping") return json{{"ok", true}};
    if (method == "cell_census") return cellCensus();
    if (method != "cell_drain") return refuse("cell_refused");
    const std::string admission = params.value("admission", std::string());
    const std::string exit = params.value("exit", std::string("none"));
    if ((admission != "closed" && admission != "open") ||
        (exit != "none" && exit != "when_quiescent") ||
        (admission == "open" && exit != "none")) return refuse("bad_request");
    if (params.contains("generation") &&
        (!params["generation"].is_number_unsigned() &&
         (!params["generation"].is_number_integer() || params["generation"].get<long long>() < 0)))
        return refuse("bad_request");
    {
        std::lock_guard<std::mutex> lock(clientsMtx_);
        if (cellExiting_) return refuse("cell_exiting");
        if ((admission == "open" && !params.contains("generation")) ||
            (params.contains("generation") &&
             params["generation"].get<std::uint64_t>() != cellGeneration_))
            return refuse("stale_generation");
        const bool closed = admission == "closed";
        if (closed != cellAdmissionClosed_) { cellAdmissionClosed_ = closed; ++cellGeneration_; }
        cellExitWhenQuiescent_ = closed && exit == "when_quiescent";
    }
    return cellCensus();
}

int Server::pinningClients(int idleWindowSecs) {
    std::vector<std::shared_ptr<ClientConn>> live;
    {
        std::lock_guard<std::mutex> lk(clientsMtx_);
        for (auto& [id, c] : clients_) live.push_back(c);
    }
    const long long now = monoMs();
    const long long windowMs = (long long)std::max(1, idleWindowSecs) * 1000;
    int n = 0;
    for (const auto& c : live) {
        if (c->controlOnly) continue;
        // The app being open is itself the reason this process exists.
        if (c->kind == "gui") { ++n; continue; }
        if (now - c->lastRpcMs.load() <= windowMs) { ++n; continue; }
        {
            std::lock_guard<std::mutex> lk(inflightMtx_);
            if (inflight_.count(c->id)) { ++n; continue; }
        }
        if (registry_.hasAttachments(c->id)) ++n;
    }
    return n;
}

void Server::disconnectAllClients() {
    std::vector<std::shared_ptr<ClientConn>> live;
    {
        std::lock_guard<std::mutex> lk(clientsMtx_);
        for (auto& [id, c] : clients_) live.push_back(c);
    }
    // shutdown() (not close()) so a thread parked in readLine()'s poll() gets POLLHUP and
    // returns at once; clientLoop's disconnect tail then reclaims the fd and the slot.
    for (const auto& c : live) c->conn.shutdown();
}

// The GC grace does not pin an idle daemon: shutdown snapshots and migrates
// every outstanding dirty document before the process exits.

bool Server::idleExpired(int lingerSecs) {
    // After the last GUI detached, only an ACTIVE non-GUI client or in-flight work justifies
    // staying up, so use the short grace rather than the full linger.
    const int idleLimitSecs = guiDetached_ ? guiExitGraceSecs_ : lingerSecs;
    if (pinningClients(idleLimitSecs) > 0 || status_.inflight > 0) return false;
    // Never idle-exit under a running mission: children are the daemon's work
    // even with zero clients (GUI-close continuity).
    if (children_.aliveCount() > 0) return false;
    if (monoMs() - status_.lastActivityMs <= (long long)idleLimitSecs * 1000) return false;
    if (!cell_) return true;
    if (monoMs() - lastCellProbeMs_ < 1000) return false;
    lastCellProbeMs_ = monoMs();
    const json census = cellCensus(true, true);
    if (!census["exiting"].get<bool>()) { touch(); return false; }
    return true;
}

void Server::acceptClient(chudware_ipc::IpcConn conn, bool gateway, const std::string& actor,
                          bool operatorListener) {
    if (!conn.valid()) return;
    std::string firstLine;
    // Cell actor sockets are not operator channels. In particular, claiming
    // "control" on one must not bypass a hard or stale-binary drain.
    if (status_.draining &&
        !admitWhileDraining(conn, firstLine, !cell_ || gateway)) return;
    auto c = std::make_shared<ClientConn>();
    c->gateway = gateway;
    c->operatorListener = operatorListener;
    if (cell_ && !gateway) {
        std::lock_guard<std::mutex> lock(cellMtx_);
        const auto a = actors_.find(actor);
        if (a == actors_.end() || a->second.revoked) return;
        c->actorKey = a->second.key;
        c->actorHex = a->second.hex;
        c->right = a->second.right;
        c->epoch = a->second.epoch;
    }
    std::unique_lock<std::mutex> lock(clientsMtx_);
    if (cell_ && cellExiting_) return;
    if (cell_ && gateway && cellAdmissionClosed_) {
        lock.unlock();
        if (firstLine.empty() && !conn.readLine(firstLine, 1000)) return;
        const json request = json::parse(firstLine, nullptr, false);
        const bool control = request.is_object() && request.contains("method") &&
            request["method"] == "hello" && request.contains("params") &&
            request["params"].is_object() && request["params"].contains("client") &&
            request["params"]["client"] == "control";
        if (!control) {
            const json id = request.is_object() && request.contains("id") ? request["id"] : json(-1);
            conn.writeLine(json{{"id", id}, {"error", {{"code", "daemon_draining"},
                {"message", "cell maintenance admission is closed"}, {"reason", "cell_drain"}}}}.dump());
            return;
        }
        lock.lock();
        if (cellExiting_) return;
    }
    c->id = nextClientId_++;
    c->conn = std::move(conn);
    c->lastRpcMs = monoMs();
    clients_[c->id] = c;
    ++status_.clients;
    c->thread = std::thread([this, c, first = std::move(firstLine)]() mutable {
        clientLoop(c, std::move(first));
    });
}

void Server::run(int lingerSecs) {
    children_.start();
    auto fileWatch = std::make_unique<FileWatch>(
        [this]() -> json {
            if (status_.engineState.load() != 1) return nullptr;
            json rows;
            std::string error;
            if (!core::cpp_core().call("doc_list", json::object(), rows, error)) return nullptr;
            registry_.annotateDocList(rows, -1);
            return rows;
        },
        [this](const std::string& id, const std::string& path, const json& stamp) {
            ClientConn system;
            system.id = -1;
            system.kind = "system";
            bool error = false;
            const json result = dispatch(system, "doc_reload",
                json{{"doc_id", id}, {"origin", "system"}, {"_watch_path", path},
                     {"_watch_stamp", stamp}}, error);
            if (error) return FileWatch::Outcome::Retry;
            if (result.value("_watch_conflict", false)) return FileWatch::Outcome::Conflict;
            if (result.value("_watch_reloaded", false)) return FileWatch::Outcome::Reloaded;
            return FileWatch::Outcome::Retry;
        });
    // Either tier being enabled is reason enough to run the thread: the ceilings are tuned
    // independently, so disabling the interactive one must not silently disable the long one.
    if (engineWatchdogMs_ > 0 || engineWatchdogLongMs() > 0)
        watchdog_ = std::thread([this] { watchdogLoop(); });
    while (!stopping_) {
        acceptClient(listener_.accept(cell_ ? 50 : 500), cell_);
        if (cell_) {
            acceptClient(operatorListener_->accept(0), true, {}, true);
            std::vector<std::pair<std::string, std::shared_ptr<chudware_ipc::IpcListener>>> listeners;
            {
                std::lock_guard<std::mutex> lock(cellMtx_);
                for (const auto& [key, actor] : actors_)
                    if (!actor.revoked && actor.listener) listeners.emplace_back(key, actor.listener);
            }
            for (const auto& [actor, listener] : listeners)
                acceptClient(listener->accept(0), false, actor);
        }
        // Expire stale edit-holds; retire documents only after their attachment
        // and live-run holds have ended and the orphan grace has elapsed.
        for (const std::string& id : registry_.tick()) gcCloseDoc(id);
        staleBinaryTick();  // mtime watch on hostd binary + libchudware (self-drain)
        if (cell_) {
            bool wantsExit = false;
            { std::lock_guard<std::mutex> lock(clientsMtx_); wantsExit = cellExitWhenQuiescent_; }
            if (wantsExit && monoMs() - lastCellProbeMs_ >= 1000) {
                lastCellProbeMs_ = monoMs();
                if (cellCensus(true)["exiting"].get<bool>()) break;
            }
        }
        if (status_.draining) {
            // A drain ends when the work ends — not when the last SOCKET goes away. Parked
            // connections do not count (pinningClients), and a HARD drain (--stop, tray Quit,
            // SIGTERM) additionally has a deadline after which stragglers are dropped: the
            // daemon this replaces sat "draining (1 client)" in the tray for 5.5 h because one
            // silent socket was allowed to veto its own shutdown forever.
            const long long deadline = drainDeadlineMs_.load();
            const bool expired = deadline > 0 && monoMs() >= deadline;
            const bool noWork = pinningClients(guiExitGraceSecs_) == 0 &&
                                status_.inflight == 0 && children_.aliveCount() == 0;
            bool safeCell = !cell_;
            if (cell_ && noWork && monoMs() - lastCellProbeMs_ >= 1000) {
                lastCellProbeMs_ = monoMs();
                safeCell = cellCensus(true)["exiting"].get<bool>();
            }
            if ((noWork && safeCell) || expired) {
                if (expired && status_.clients > 0) {
                    log().line("drain deadline reached with %d client(s) still connected — "
                               "dropping them and exiting", status_.clients.load());
                    disconnectAllClients();
                } else if (!expired) {
                    // Nothing pins (see pinningClients): any socket left is parked, so the
                    // same clean-exit notice as an idle exit applies.
                    sendEventToAll(json{{"event", "daemon.exiting"}, {"reason", "drained"}}.dump());
                }
                break;
            }
        } else if (idleExpired(lingerSecs)) {
            log().line("idle for %ds with nothing to serve — exiting",
                       guiDetached_ ? guiExitGraceSecs_ : lingerSecs);
            // A parked client (no attachments, nothing in flight) may still hold its socket.
            // Tell it this was a clean idle exit so its next call reconnects instead of
            // reporting a lost session; a crash sends nothing, keeping that path loud.
            sendEventToAll(json{{"event", "daemon.exiting"}, {"reason", "idle"}}.dump());
            break;
        }
    }
    // Stop accepting requests and cancel worker turns before waiting for client
    // threads: a turn can otherwise hold a thread indefinitely. Keep sockets
    // open until their in-flight replies have been delivered.
    stopping_ = true;
    WorkerBridge::shutdownIfCreated();
    while (status_.inflight.load() > 0 || controlRepliesPending_.load() > 0)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    disconnectAllClients();
    fileWatch.reset();
    // Every exit path (drain / idle / SIGTERM via stop()): stop the children
    // (TERM -> 5 s grace -> KILL) BEFORE main runs the final autosave, so the
    // saved state postdates the last agent edit. Daemon SIGKILL is covered by
    // each child's PDEATHSIG.
    children_.shutdown();
    std::vector<std::shared_ptr<ClientConn>> live;
    std::vector<std::thread> threads;
    {
        std::lock_guard<std::mutex> lk(clientsMtx_);
        for (auto& [id, client] : clients_) {
            live.push_back(client);
            if (client->thread.joinable()) threads.push_back(std::move(client->thread));
        }
    }
    for (auto& thread : threads) thread.join();
    for (auto& client : live) client->conn.close();
}

bool Server::gcCloseDoc(const std::string& docId, const char* reason, int clientId,
                        const std::string& clientKind, const char* method) {
    if (status_.engineState.load() != 1) return false;
    for (int attempt = 0; attempt < 3; ++attempt) {
        std::uint64_t token = 0;
        if (!registry_.reapToken(docId, token)) return false;
        json docs;
        std::string err;
        if (!core::cpp_core().call("doc_list", json::object(), docs, err) ||
            !docs.is_object() || !docs.contains("docs") || !docs["docs"].is_array())
            return false;
        json row;
        for (const auto& d : docs["docs"])
            if (d.is_object() && d.value("doc_id", std::string()) == docId) { row = d; break; }
        if (!row.is_object()) {
            if (!registry_.closeAbandoned(docId, token, reason, [] { return true; })) return false;
            log().line("doc removal: doc=%s client=%d kind=%s method=%s reason=%s dirty=unknown preservation=absent",
                       docId.c_str(), clientId, clientKind.c_str(), method, reason);
            return true;
        }
        const bool internal = registry_.isInternal(docId);
        const bool dirty = row.value("dirty", false) || registry_.humanDirty(docId);
        if (cell_ && !internal && (dirty || row.value("busy", false))) return false;
        const std::string path = row.contains("path") && row["path"].is_string()
                                   ? row["path"].get<std::string>() : std::string();
        long revision = row.value("revision", -1L);
        long metadataRevision = row.value("metadata_revision", -1L);
        // A clean new blank needs no artifact; any authored revision without a
        // durable destination (including imported/metadata-only work) does.
        const bool undelivered = path.empty() && (revision > 0 || metadataRevision > 0);
        const bool needRetire = !internal && (dirty || undelivered ||
                                (autosave_ && autosave_->isAutosavePath(path)));
        if (needRetire && (!autosave_ || !autosave_->retireDoc(
                              docId, row.value("name", docId), revision, metadataRevision)))
            return false;
        bool changed = false;
        const bool closed = registry_.closeAbandoned(docId, token, reason, [&] {
            json current;
            if (!core::cpp_core().call("doc_list", json::object(), current, err) ||
                !current.is_object() || !current.contains("docs") || !current["docs"].is_array())
                return false;
            bool matches = false;
            for (const auto& d : current["docs"]) {
                if (!d.is_object() || d.value("doc_id", std::string()) != docId) continue;
                matches = d.value("revision", -1L) == revision &&
                          d.value("metadata_revision", -1L) == metadataRevision;
                break;
            }
            if (!matches) { changed = true; return false; }
            json result;
            return core::cpp_core().call("doc_close", json{{"doc_id", docId}}, result, err);
        });
        if (closed) {
            if (autosave_) autosave_->onDocClosed(docId);
            log().line("doc removal: doc=%s client=%d kind=%s method=%s reason=%s dirty=%s preservation=%s",
                       docId.c_str(), clientId, clientKind.c_str(), method, reason,
                       dirty ? "true" : "false",
                       needRetire ? "recovery" : internal ? "internal" : "clean");
            return true;
        }
        if (!changed) return false;  // a new attachment or a failed close
    }
    return false;
}

// RAII tally for Status::inflight. A bare ++/-- pair around dispatch() leaks the counter when
// dispatch throws, and that leak is PERMANENT: idleExpired() gates on inflight == 0, so a single
// leaked increment locks the daemon out of its own idle-exit for the rest of the process.
namespace {
struct InflightTally {
    std::atomic<int>& n;
    explicit InflightTally(std::atomic<int>& c) : n(c) { ++n; }
    ~InflightTally() { --n; }
};

// Large CAD operations leave substantial freed OCCT/JSON allocations in glibc
// arenas. Return free pages after the reply's temporary objects are destroyed;
// allocated geometry, history and caches are untouched. Limit this to calls
// that can build or release document geometry.
struct DocumentArenaTrim {
    bool active;
    explicit DocumentArenaTrim(const std::string& method)
        : active(method == "doc_apply_ops" || method == "doc_open" ||
                 method == "doc_rebuild" || method == "doc_undo" ||
                 method == "doc_redo" || method == "doc_revert" ||
                 method == "doc_save" || method == "doc_close") {}
    ~DocumentArenaTrim() {
#if defined(__linux__) && defined(__GLIBC__)
        if (active) malloc_trim(0);
#endif
    }
};

namespace {
struct RequestCancellation {
    ClientConn& client;
    bool abandoned = false;
    static int check(void* opaque) {
        auto& self = *static_cast<RequestCancellation*>(opaque);
        return self.abandoned = self.abandoned || self.client.conn.peerDisconnected();
    }
};
}
}  // namespace

void Server::clientLoop(std::shared_ptr<ClientConn> c, std::string firstLine) {
    // An exception escaping a std::thread entry function is an instant std::terminate() of the
    // WHOLE daemon — every other client's session gone, and none of the clean-shutdown tail
    // (final autosave, child shutdown, sidecar reap) runs. Nothing in this loop may propagate
    // out, and the disconnect tail below must run whatever happens in it.
    try {
        std::string line = std::move(firstLine);
        bool pending = !line.empty();  // a request admitWhileDraining already read
        while (!stopping_ && (pending || c->conn.readLine(line, -1))) {
            pending = false;
            if (c->conn.peerDisconnected()) {
                log().line("client #%d: dropped abandoned queued request", c->id);
                break;
            }
            json msg = json::parse(line, nullptr, false);
            if (msg.is_discarded()) continue;
            // The envelope is client-supplied too: msg.value("id", -1) throws on a string id
            // exactly like a bad param does, and we need the id to answer with an error at all.
            // Type-check both instead of relying on value()'s conversion.
            const int id =
                msg.contains("id") && msg["id"].is_number_integer() ? msg["id"].get<int>() : -1;
            const std::string method = msg.contains("method") && msg["method"].is_string()
                                           ? msg["method"].get<std::string>()
                                           : std::string();
            DocumentArenaTrim arenaTrim(method);
            const bool operatorRequest = c->controlOnly ||
                (cell_ && c->gateway && method == "hello" && msg.contains("params") &&
                 msg["params"].is_object() && msg["params"].contains("client") &&
                 msg["params"]["client"] == "control");
            // Keep this barrier through reply delivery, not just dispatch.
            InflightTally tally(operatorRequest ? controlRepliesPending_ : status_.inflight);
            bool isError = false;
            json payload;
            try {
                // `value("params", object())` substitutes its default only when the KEY is
                // ABSENT — an explicit `"params": null` on the wire comes back as null and
                // makes the first `params.value(...)` in a handler throw type_error.306.
                // Coerce anything that is not an object so a zero-argument request can never
                // be rejected for it (client-side normalization also exists in AsyncRpc).
                const json rawParams = msg.value("params", json::object());
                payload = dispatch(*c, method,
                                   rawParams.is_object() ? rawParams : json::object(), isError);
            } catch (const std::exception& e) {
                // Canonical case: a field of the wrong JSON type ({"force":1} instead of
                // {"force":true}) throws nlohmann::json::type_error out of one of dispatch()'s
                // dozens of typed accessors. One bad request must cost exactly one request.
                isError = true;
                payload = json{{"code", "bad_request"}, {"message", e.what()}};
                log().line("client #%d: '%s' threw (%s) — rejected that request; the daemon and "
                           "every other client are unaffected", c->id, method.c_str(), e.what());
            } catch (...) {
                isError = true;
                payload = json{{"code", "bad_request"},
                               {"message", "dispatch threw a non-standard exception"}};
                log().line("client #%d: '%s' threw a non-standard exception — rejected that "
                           "request", c->id, method.c_str());
            }
            if (c->conn.peerDisconnected()) {
                log().line("client #%d: dropped abandoned '%s' request or reply", c->id,
                           method.c_str());
                break;
            }
            if (!c->controlOnly) touch();
            c->lastRpcMs = monoMs();  // this connection is doing work — see pinningClients
            if (cell_ && c->gateway) workspaceRelativePaths(payload, cellWork_);
            json resp = {{"id", id}};
            resp[isError ? "error" : "result"] = std::move(payload);
            // dump() is this loop's other throw site: it rejects invalid UTF-8 (type_error.316),
            // which an engine message quoting raw bytes can carry. Answer with something
            // serialisable rather than silently dropping a client that is blocked on this reply.
            std::string out;
            try {
                out = resp.dump();
            } catch (const std::exception& e) {
                log().line("client #%d: response to '%s' is not serialisable (%s)", c->id,
                           method.c_str(), e.what());
                out = json{{"id", id},
                           {"error", {{"code", "encoding_error"}, {"message", e.what()}}}}
                          .dump();
            }
            std::lock_guard<std::timed_mutex> wlk(c->writeMtx);
            if (!c->conn.writeLine(out)) break;
            if (c->retireAfterReply) break;
        }
    } catch (const std::exception& e) {
        log().line("client #%d: connection loop aborted (%s) — dropping this client only", c->id,
                   e.what());
    } catch (...) {
        log().line("client #%d: connection loop aborted (non-standard exception) — dropping "
                   "this client only", c->id);
    }
    // Disconnect tail: runs on EVERY path out of the loop, including the guarded ones. Skipping
    // it would leak this client's slot and the clients_ count — and a stuck count is itself one
    // of the things that stops the daemon ever idle-exiting. It must not throw out of the thread
    // either, hence the guard around it.
    try {
        {
            std::lock_guard<std::mutex> lk(clientsMtx_);
            // We're on this thread; the map drop releases the struct. During teardown
            // ~Server already owns (and joins) the thread object, so it is not joinable.
            if (c->thread.joinable()) c->thread.detach();
            clients_.erase(c->id);
            // Last GUI gone: switch idle-exit to the short grace (see guiDetached_).
            if (c->kind == "gui") {
                bool anyGuiLeft = false;
                for (const auto& [id, other] : clients_)
                    if (other->kind == "gui") { anyGuiLeft = true; break; }
                if (!anyGuiLeft) guiDetached_ = true;
            }
        }
        --status_.clients;
        if (!c->controlOnly) touch();
        log().line("client #%d (%s) disconnected (%d left)", c->id, c->kind.c_str(),
                   status_.clients.load());
        registry_.onDisconnect(c->id, c->kind);
    } catch (...) {
        log().line("client #%d: disconnect cleanup threw — daemon continues", c->id);
    }
}

json Server::dispatch(ClientConn& c, const std::string& method, const json& rawParams,
                      bool& isError) {
    json params = rawParams;
    const bool internalWatcher = c.id == -1 && method == "doc_reload";
    if (!internalWatcher) {
        params.erase("_watch_path");
        params.erase("_watch_stamp");
    }
    params.erase("_cell_ctx");
    params.erase("ctx");
    params.erase("_cell_revoke_through");
    params.erase("control_delegate");
    // Only hostd may assign a turn's document-lifetime scope to its worker.
    params.erase("_hostd_doc_scope");
    const char* stateNames[] = {"loading", "ready", "failed"};
    if (!cell_ && c.controlOnly && method != "ping" &&
        method != "daemon_status" && method != "daemon_drain") {
        isError = true;
        return json{{"code", "control_only"}, {"message", "daemon control connection only"}};
    }
    if (method == "hello") {
        if (cell_) {
            json result = cellHello(c, params, isError);
            if (isError) return result;
            c.kind = c.controlOnly ? "control" : c.gateway ? "gui" : "agent";
            if (c.kind == "agent")
                registry_.joinAgentRun(c.id, params.value("agent_run_scope", std::string()),
                                       c.actorKey);
            if (c.kind == "gui") guiDetached_ = false;
            json hello{{"proto", 1}, {"abi", core::cpp_core().abiVersion()},
                       {"git_rev", buildId_}, {"engine_build_id", core::cpp_core().buildId()},
                       {"daemon_pid", selfPid()}, {"daemon_start_id", daemonStartId_},
                       {"state", stateNames[status_.engineState.load()]},
                       {"cell", true}, {"id_remap", json::object()}, {"recovery_pending", 0}};
            if (c.operatorControl) { hello["operator"] = true; hello["cell_id"] = cellId_; }
            return hello;
        }
        c.kind = params.value("client", "unknown");
        if (c.kind == "control") c.controlOnly = true;
        c.pid = params.value("pid", 0L);
        c.writerGroup = params.value("writer_group", std::string());
        registry_.setWriterGroup(c.id, c.writerGroup);
        if (c.kind == "mcp" || c.kind == "agent")
            registry_.joinAgentRun(c.id, params.value("agent_run_scope", std::string()), {});
        if (c.kind == "gui") {
            guiDetached_ = false;  // GUI (re)attached: full linger again
            c.guiPeerPid = c.conn.peerPid();
        }
        // Two distinct identities. AUTHORITY (manufacturing leases, undo policy)
        // always re-derives the authenticated local peer credential at the call
        // site — `hello.user` is never accepted as an identity assertion. But doc
        // ATTRIBUTION (doc.changed/doc.opened user stamps, _foreign_edits) uses
        // hello-declared display metadata: the web gateway multiplexes many real
        // tenants through one uid, so the credential cannot distinguish writers
        // there (01117f2d). Absent/blank hello.user falls back to the credential.
        c.userId = authenticatedPeerIdentity(c);
        c.userDisplay = c.userId;
        if (const auto user = params.value("user", json::object()); user.is_object()) {
            if (const std::string id = user.value("id", std::string()); !id.empty())
                c.userId = id;
            if (const std::string display = user.value("display", std::string());
                !display.empty())
                c.userDisplay = display;
        }
        return json{{"proto", 1},
                    {"abi", core::cpp_core().abiVersion()},
                    {"git_rev", buildId_},
                    // "" while the engine loads or when the .so predates the
                    // chudware_build_id export — clients tolerate absence.
                    {"engine_build_id", core::cpp_core().buildId()},
                    {"daemon_pid", selfPid()},
                    {"daemon_start_id", daemonStartId_},
                    {"state", stateNames[status_.engineState.load()]},
                    // Autosave-recovery rebind map (empty until recovery ran;
                    // daemon_status.recovery tells clients when to re-hello).
                    {"id_remap", autosave_ ? autosave_->idRemap() : json::object()},
                    // The final count is available after migration; do not block hello.
                    {"recovery_pending", autosave_ && autosave_->recoveryDone()
                                             ? autosave_->orphanCount() : 0}};
    }
    if (cell_ && !internalWatcher) {
        if (c.operatorControl) return cellOperator(c, method, params, isError);
        if (method == "principal_revoke" || method == "principal_update")
            return changePrincipal(c, method, params, isError);
        if (c.controlOnly && method != "ping") {
            isError = true;
            return json{{"code", "cell_refused"}, {"message", "principal control only"}};
        }
        json error;
        if (!cellAdmit(c, method, params, error)) {
            isError = true;
            return error;
        }
    }
    if (method == "ping") return json{{"ok", true}};
    const std::string authenticatedSeat = authenticatedPeerIdentity(c);
    // hello.client is display/routing metadata only. It cannot attest a human at
    // the keyboard, so manufacturing sees every raw socket as non-human.
    const chudware::mfg::LeaseOwner mfgCaller{
        "raw", authenticatedSeat, "hostd-client:" + std::to_string(c.id)};
    if (auto* mfg = chudware::mfg::tryLane()) {
        if (auto result = mfg->handle(method, params, mfgCaller)) {
            isError = result->is_error;
            return std::move(result->body);
        }
    } else if (method.rfind("mfg_", 0) == 0) {
        std::string why;
        chudware::mfg::tryLane(&why);
        isError = true;
        return json{{"code", "mfg_lane_unavailable"},
                    {"message", "manufacturing lane failed to initialize: " + why}};
    }
    // TEST-ONLY (env-gated; neither name is ever a real client RPC): register an
    // in-flight engine call and sleep, to exercise the watchdog end-to-end.
    // "agent_test_stall" is also classified as an agent RPC by the watchdog.
    if (const char* sm = std::getenv("CHUDWARE_HOSTD_TEST_STALL_MS");
        sm && *sm && (method == "__hostd_test_stall" || method == "agent_test_stall")) {
        beginInflight(c.id, method);
        std::this_thread::sleep_for(std::chrono::milliseconds(std::max(0, std::atoi(sm))));
        endInflight(c.id);
        return json{{"ok", true}, {"slept_ms", std::atoi(sm)}};
    }
    if (method == "daemon_status") {
        json st{{"state", stateNames[status_.engineState.load()]},
                {"clients", status_.clients.load()},
                {"inflight", status_.inflight.load()},
                // Engine-only (agent turns / heavy doc ops) — tray / Stop UX use this.
                {"engine_inflight", engineInflightCount()},
                {"draining", status_.draining.load()},
                {"git_rev", buildId_},
                {"engine_build_id", core::cpp_core().buildId()},
                {"recovery", !autosave_                  ? "off"
                             : autosave_->recoveryDone() ? "done"
                                                         : "pending"},
                {"recovery_pending", autosave_ && autosave_->recoveryDone()
                                         ? autosave_->orphanCount() : 0},
                {"daemon_pid", selfPid()}, {"daemon_start_id", daemonStartId_},
                {"pid", selfPid()}};
        children_.statusInto(st);  // children count + mission ids
        // A draining daemon says why, and what a new client gets (the same text as the
        // daemon_draining refusal), so `--status` and the tray agree with the refusal.
        const std::string reason = drainReason();
        st["drain_reason"] = reason.empty() ? json(nullptr) : json(reason);
        if (status_.draining.load()) {
            json drain = drainRefusal();
            drain.erase("code");
            st["drain"] = std::move(drain);
        }
        return st;
    }
    if (method == "daemon_drain") {
        if (children_.aliveCount() > 0) {
            if (!params.value("force", false)) {
                isError = true;
                return json{{"code", "mission_active"},
                            {"message", "agent children are running; stop them or "
                                        "pass {\"force\":true} (chudware-hostd --stop --force)"}};
            }
            children_.stopAll();  // TERM -> grace -> KILL, reaped before we drain
        }
        // An explicit drain is a STOP request: it gets a deadline, so a client that never
        // disconnects cannot veto it. Env CHUDWARE_HOSTD_DRAIN_GRACE_S (default 20 s, floor 1).
        int graceSecs = 20;
        if (const char* g = std::getenv("CHUDWARE_HOSTD_DRAIN_GRACE_S"); g && *g)
            graceSecs = std::max(1, std::atoi(g));
        beginDrain(graceSecs, "stop_requested");
        log().line("drain requested by client #%d (deadline %ds)", c.id, graceSecs);
        return json{{"ok", true}, {"deadline_secs", graceSecs}};
    }

    // Lock-free doc read for spectators. Returns the last revision payload the registry
    // fanned out for this doc (tree + aicm_path + owner_map + datums + feature_status),
    // straight out of DocState::lastPush — this NEVER reaches the engine, so it cannot
    // queue behind the per-doc mutex a writing client holds.
    // WHY: a GUI mirror tab used doc_activate to populate itself. doc_activate is
    // read-only in intent but still takes the doc's DocLock, so clicking the tab for the
    // part an agent was actively building blocked for 142.9 s on a live run (measured:
    // "[perf] tab_switch_rebuild ... total=142902ms") while every further click coalesced
    // behind it. Spectating must never wait on the writer.
    if (method == "doc_snapshot") {
        const std::string id = params.value("doc_id", std::string());
        if (id.empty()) {
            isError = true;
            return json{{"code", "invalid_params"}, {"message", "pass {\"doc_id\": ...}"}};
        }
        json push = registry_.lastPushFor(id);
        if (!push.is_object())
            return json{{"doc_id", id}, {"has_snapshot", false}};
        push["doc_id"] = id;
        push["has_snapshot"] = true;
        return push;
    }

    // List only after migration and automatic AI restoration have completed.
    if (method == "recovery_list") {
        if (!autosave_) return json{{"entries", json::array()}};
        autosave_->waitForRecovery();
        json list = autosave_->recoveryList(params.value("scope", std::string()));
        if (list.contains("error")) {
            isError = true;
            return json{{"code", "recovery_unavailable"}, {"message", "recovery storage unavailable"}};
        }
        return list;
    }
    if (method == "recovery_discard") {
        if (!params.contains("ids") || !params["ids"].is_array() || params.contains("all")) {
            isError = true;
            return json{{"code", "invalid_params"}, {"message", "pass {\"ids\": [...]}"}};
        }
        for (const auto& id : params["ids"]) {
            if (!id.is_string() || id.get_ref<const std::string&>().empty()) {
                isError = true;
                return json{{"code", "invalid_params"}, {"message", "ids must be nonempty strings"}};
            }
        }
        if (!autosave_) return json{{"discarded", 0}};
        autosave_->waitForRecovery();
        if (!autosave_->recoveryAvailable()) {
            isError = true;
            return json{{"code", "recovery_unavailable"}, {"message", "recovery storage unavailable"}};
        }
        int count = 0;
        for (const auto& id : params["ids"]) count += autosave_->discardOrphans(id.get<std::string>());
        return json{{"discarded", count}};
    }
    if (method == "recovery_open") {
        if (status_.engineState.load() != 1) {
            isError = true;
            return json{{"code", "loading"}, {"message", "engine is still loading; retry"}};
        }
        if (!autosave_) return json{{"opened", json::array()}};
        autosave_->waitForRecovery();
        if (!autosave_->recoveryAvailable()) {
            isError = true;
            return json{{"code", "recovery_unavailable"}, {"message", "recovery storage unavailable"}};
        }
        std::vector<std::string> ids;
        if (params.value("all", false)) {
            const json list = autosave_->recoveryList();
            if (!list.contains("entries")) {
                isError = true;
                return json{{"code", "recovery_unavailable"}, {"message", "recovery storage unavailable"}};
            }
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
            autosave_->recoverOrphan(id, [&](const std::string& path, const json& meta) {
                json result;
                std::string err;
                json p2{{"path", path}, {"detach_path", true}, {"recovered", true}};
                bool diskConflict = false;
                json observedStamp = nullptr;
                if (meta.contains("path") && meta["path"].is_string() &&
                    !meta["path"].get_ref<const std::string&>().empty()) {
                    p2["recovery_original_path"] = meta["path"];
                    p2["recovery_base_stamp"] = meta.value("file_stamp", json());
                    const json base = meta.value("file_stamp", json());
                    observedStamp = FileWatch::stamp(meta["path"].get<std::string>());
                    diskConflict = !base.is_object() || observedStamp != base;
                }
                if (!core::cpp_core().call("doc_open", p2, result, err) ||
                    (result.contains("ok") && result["ok"].is_boolean() && !result["ok"].get<bool>())) {
                    log().line("recovery_open: doc_open(%s) failed: %s", path.c_str(), err.c_str());
                    return false;
                }
                if (p2.contains("recovery_original_path")) {
                    observedStamp = FileWatch::stamp(p2["recovery_original_path"].get<std::string>());
                    diskConflict = diskConflict || observedStamp != p2["recovery_base_stamp"];
                }
                const std::string newId = result.value("doc_id", std::string());
                if (newId.empty()) return false;
                auto recoveryGuard = registry_.lockMutation(newId);
                autosave_->rememberRecoveredSource(newId, meta, id,
                                                  result.value("revision", -1L),
                                                  result.value("metadata_revision", -1L));
                registry_.postCall(c.id, c.kind, c.userId, c.userDisplay, "doc_open", p2, result);
                const std::string origId = meta.contains("doc_id") && meta["doc_id"].is_string()
                                             ? meta["doc_id"].get<std::string>() : std::string();
                const std::string origin = meta.contains("origin_kind") && meta["origin_kind"].is_string()
                                             ? meta["origin_kind"].get<std::string>() : "unknown";
                const bool humanDirty = !meta.contains("human_dirty") || !meta["human_dirty"].is_boolean()
                                         || meta["human_dirty"].get<bool>();
                registry_.restoreRecoveryState(newId, origin, humanDirty,
                                               result.value("revision", 0L), -1);
                autosave_->noteRecoveredRemap(origId, newId);
                opened.push_back(json{{"id", id}, {"doc_id", newId},
                                      {"original_doc_id", origId}, {"origin_kind", origin},
                                      {"human_dirty", humanDirty}, {"disk_conflict", diskConflict},
                                      {"observed_stamp", observedStamp}});
                if (autosave_->snapshotNow(newId)) return true;
                log().line("recovery_open: kept orphan %s (session snapshot of %s failed)",
                           id.c_str(), newId.c_str());
                return false;
            });
        }
        return json{{"opened", opened}};
    }

    // Closing a view never destroys another client's attachment or lease.
    // Both close forms preserve dirty authored work before the last owner leaves.
    if (method == "doc_close" || method == "doc_discard") {
        const std::string docId = params.value("doc_id", std::string());
        if (docId.empty()) {
            isError = true;
            return json{{"code", "invalid_params"}, {"message", "doc_id required"}};
        }
        const bool blocked = registry_.closeBlockedBy(docId, c.id);
        json det;
        bool de = false;
        registry_.handleMethod(c.id, c.kind, "detach_doc", params, det, de);
        const bool closed = !blocked && gcCloseDoc(docId,
                            method == "doc_discard" ? "discarded" : "closed",
                            c.id, c.kind, method.c_str());
        json out{{"ok", true}, {"detached", !closed}};
        if (method == "doc_discard") out["discarded"] = closed;
        if (!closed) out["note"] = "document remains open for another client, an active agent run, or recovery";
        return out;
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

    // An online worker turn occupies its stdio channel for the full run. Route
    // matching cancel/steer/answer through the worker's authenticated side
    // channel first. The engine has no run registry: a question or run only
    // lives inside an active worker run, so an unmatched control call ends here
    // with its "no active agent run" reply and never queues behind a busy agent
    // lane.
    if (method == "agent_cancel" || method == "agent_steer" || method == "agent_answer") {
        json controlResult;
        std::string controlErr;
        bool workerRunMatched = false;
        const char* action = method == "agent_cancel"  ? "cancel"
                             : method == "agent_steer" ? "steer"
                                                       : "answer";
        if (cell_ && method == "agent_cancel" && params.value("run_id", std::string()).empty()) {
            std::vector<std::string> own;
            {
                std::lock_guard<std::mutex> lock(cellMtx_);
                for (const auto& [run, actor] : runOwners_) if (actor == c.actorKey) own.push_back(run);
            }
            for (const auto& run : own) {
                bool matched = false;
                json ownControl = params;
                ownControl["run_id"] = run;
                ownControl.erase("_cell_ctx");
                WorkerBridge::controlActiveRun("cancel", ownControl, matched,
                    controlResult, controlErr, cellContext(c));
            }
        }
        json workerControl = params;
        workerControl.erase("_cell_ctx");
        if (WorkerBridge::controlActiveRun(action, workerControl, workerRunMatched,
                                           controlResult, controlErr,
                                           cell_ ? params.at("_cell_ctx") : json::object()))
            return controlResult;
        if (workerRunMatched) {
            isError = true;
            return json{{"code", "worker_control_error"},
                        {"message", controlErr.empty()
                                        ? std::string("worker control request failed")
                                        : controlErr}};
        }
        if (method == "agent_answer") {
            if (cell_) {
                isError = true;
                return json{{"code", "not_found"}, {"message", "not_found"}};
            }
            const auto field = [&](const char* key) {
                const auto it = params.is_object() ? params.find(key) : params.end();
                return params.is_object() && it != params.end() && it->is_string()
                           ? it->get<std::string>()
                           : std::string();
            };
            return json{{"ok", false},
                        {"run_id", field("run_id")},
                        {"batch_id", field("batch_id")},
                        {"answered", false},
                        {"reason", "no active agent run for this question"}};
        }
        if (method == "agent_steer")
            return json{{"ok", true}, {"steered", false}, {"reason", "no active agent run"}};
        return json{{"ok", true}, {"cancelled", false}, {"reason", "no active agent run"}};
    }

    if (method == "omp_apply_compaction_policy") {
        const auto invalid = [] {
            return json{{"ok", false}, {"reason", "invalid compaction policy"}};
        };
        if (!params.is_object()) return invalid();
        json arguments = params;
        arguments.erase("_cell_ctx");
        for (const auto& item : arguments.items())
            if (item.key() != "strategy" && item.key() != "compaction") return invalid();
        const auto strategy = arguments.find("strategy");
        if (strategy == arguments.end()) {
            if (cell_ || !arguments.empty()) return invalid();
        } else {
            if (!strategy->is_string()) return invalid();
            const auto& value = strategy->get_ref<const std::string&>();
            if (value != "auto" && value != "off" && value != "snapcompact" &&
                value != "shake" && value != "summary") return invalid();
        }
        const auto options = arguments.find("compaction");
        if (options != arguments.end()) {
            if (!options->is_object()) return invalid();
            for (const auto& item : options->items()) {
                if (item.key() == "supersede_reads" || item.key() == "drop_useless") {
                    if (!item.value().is_boolean()) return invalid();
                } else if (item.key() == "keep_recent_tokens") {
                    const auto& number = item.value();
                    if (!number.is_number_integer()) return invalid();
                    if (number.is_number_unsigned()) {
                        if (number.get<std::uint64_t>() > 9007199254740991ULL) return invalid();
                    } else {
                        const auto n = number.get<std::int64_t>();
                        if (n < 0 || n > 9007199254740991LL) return invalid();
                    }
                } else return invalid();
            }
        }
        return WorkerBridge::applyCompactionPolicy(
            arguments, cell_ ? params.at("_cell_ctx") : json::object());
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
    if (autosave_) autosave_->waitForRecovery();

    const std::string guardedDoc = params.value("doc_id", std::string());
    std::optional<DocRegistry::MutationGuard> mutationGuard;
    if (!guardedDoc.empty() && DocRegistry::requiresMutationLock(method))
        mutationGuard.emplace(registry_.lockMutation(guardedDoc));
    if (method == "doc_save" && params.value("if_no_human_dirty", false) &&
        registry_.humanDirty(guardedDoc)) {
        isError = true;
        return json{{"code", "human_dirty"}, {"message", "document has unsaved human edits"}};
    }
    if (internalWatcher) {
        const std::string path = params.value("_watch_path", std::string());
        const json expected = params.value("_watch_stamp", json(nullptr));
        json rows;
        std::string error;
        if (path.empty() || !expected.is_object() || FileWatch::stamp(path) != expected ||
            !core::cpp_core().call("doc_list", json::object(), rows, error) ||
            !rows.is_object() || !rows.contains("docs") || !rows["docs"].is_array())
            return json::object();
        json matched;
        for (const auto& row : rows["docs"]) {
            if (!row.is_object() || row.value("doc_id", std::string()) != guardedDoc) continue;
            if (row.value("busy", false) || !row.contains("path") || !row["path"].is_string() ||
                row["path"] != path || !row.contains("file_stamp") || !row["file_stamp"].is_object())
                return json::object();
            if (row["file_stamp"] != expected) matched = row;
            break;
        }
        if (!matched.is_object() || FileWatch::stamp(path) != expected) return json::object();
        if (registry_.humanDirty(guardedDoc)) {
            registry_.notifyDiskConflict(guardedDoc, path, expected);
            return json{{"_watch_conflict", true}};
        }
        const long revision = matched.value("revision", -1L);
        const long metadataRevision = matched.value("metadata_revision", -1L);
        if (revision < 0 || metadataRevision < 0) return json::object();
        if (matched.value("dirty", false)) {
            long captured = -1, capturedMetadata = -1;
            if (!autosave_ || !autosave_->retireDoc(guardedDoc,
                    matched.value("name", guardedDoc), captured, capturedMetadata) ||
                captured != revision || capturedMetadata != metadataRevision) {
                return json::object();
            }
        }
        if (FileWatch::stamp(path) != expected) return json::object();
        // Engine checks these under its document lock immediately before replacement.
        params["expected_revision"] = revision;
        params["expected_metadata_revision"] = metadataRevision;
    }

    // Registry policy gate: read-only attaches, edit holds and agent undo.
    json p = params;
    p.erase("_watch_path");
    p.erase("_watch_stamp");
    std::string attachWarning;
    if (!internalWatcher) {
        json policyErr;
        if (!registry_.preCall(c.id, c.kind, c.userId, c.userDisplay, method, p, policyErr,
                              attachWarning, cell_ && cellPolicy_.at(method).at("right") == "read")) {
            isError = true;
            return policyErr;
        }
    }
    if (!internalWatcher && c.conn.peerDisconnected()) {
        isError = true;
        return json{{"code", "abandoned"}, {"message", "client disconnected"}};
    }

    json result;
    std::string err;
    const bool agentRun = method == "agent_admin_turn";
    const std::string docScope = agentRun
        ? daemonStartId_ + ":" + std::to_string(++nextAgentRunScope_) : std::string();
    if (agentRun)
        registry_.startAgentRun(docScope, c.id, c.actorKey,
                                p.value("doc_id", std::string()));
    beginInflight(c.id, method);
    struct InflightGuard {
        Server& server;
        int clientId;
        std::string runId;
        std::string docScope;
        ~InflightGuard() {
            server.endInflight(clientId);
            if (!docScope.empty()) server.registry_.endAgentRun(docScope);
            if (server.cell_ && !runId.empty()) {
                std::lock_guard<std::mutex> lock(server.cellMtx_);
                server.runOwners_.erase(runId);
            }
        }
    } inflightGuard{*this, c.id, cell_ && method == "agent_admin_turn"
                                   ? p.value("run_id", std::string()) : std::string(), docScope};
    // Live streaming (transparency): an online turn's worker events go back to the requesting
    // client (see workerEvent below); nested document commits enter the shared registry through
    // the worker's own doc calls and fan out to every subscribed GUI.
    const bool agentStream = method.rfind("agent_", 0) == 0 ||
                             method.rfind("omp_", 0) == 0 ||
                             method == "session_btw" || method == "session_handoff";
    // The native topology solve streams its sim.progress phases and per-iteration metrics to
    // the requester only, over the same push frame. It runs on the caller's own connection
    // thread, so sim_cancel and every other control call reach the engine on other
    // connections while it streams. Only this one simulation method is armed: the other sim_*
    // routes keep their non-streaming replies. The engine emits no other events; the thread-local
    // sink fires synchronously on this dispatch thread and is cleared by RAII.
    const bool topologyStream = method == "sim_run_topology";
    const bool streamThis = agentStream || topologyStream;
    // The context is declared before the guard so it outlives its destructor on every exit.
    StreamCtx sctx{this, c.id, topologyStream};
    struct SinkGuard {
        bool eventsOn;
        ~SinkGuard() {
            if (eventsOn) core::cpp_core().setEventSink(nullptr, nullptr);
        }
    } sinkGuard{topologyStream};
    if (topologyStream) core::cpp_core().setEventSink(&Server::streamThunk, &sctx);
    RequestCancellation cancellation{c};
    struct CancelGuard {
        ~CancelGuard() { core::cpp_core().setRequestCancel(nullptr, nullptr); }
    } cancelGuard;
    if (!internalWatcher) core::cpp_core().setRequestCancel(&RequestCancellation::check, &cancellation);
    // The connection kind, not a client-supplied document origin, identifies who
    // is making this mutation. A document can be shared by GUI and agent clients.
    if (method == "doc_apply_ops")
        p["_mutation_origin"] = c.kind;
    const bool engineOk = core::cpp_core().call(method, p, result, err);
    if (!engineOk) {
        // Engine-unknown only: forward to the lazily-spawned Python worker
        // (plm_*, git_*, ai_roles_*, provider accounts, …). Known-method
        // failures stay on the engine error path byte-identical — never mask
        // a real engine error behind a worker round-trip. Each worker lane
        // holds only its own stdio mutex; a slow agent turn cannot block
        // unrelated worker calls from other clients.
        const bool unknown =
            err.rfind("unknown method:", 0) == 0 ||
            (result.is_object() && result.contains("error") && result["error"].is_string() &&
             result["error"].get<std::string>().rfind("unknown method:", 0) == 0);
        // Native-only topology RPCs have no services-worker implementation; an engine build
        // that does not dispatch them is reported as such rather than proxied to a worker
        // method that merely shares the name.
        const bool nativeOnly =
            method == "sim_run_topology" || method == "topology_capabilities";
        if (unknown && nativeOnly) {
            isError = true;
            json e = result.is_object() ? result : json::object();
            e["code"] = "engine_unsupported";
            e["message"] = "This engine build has no native topology optimizer, so '" + method +
                           "' is unavailable. Update the engine to a build that dispatches it.";
            return e;
        }
        if (unknown) {
            json wresult;
            std::string werr;
            WorkerBridge::EventFn workerEvent;
            if (streamThis) {
                const int requester = c.id;
                const std::string owner = cell_ ? p.at("_cell_ctx").value("actor_key", c.actorKey) : std::string();
                workerEvent = [this, requester, owner](const json& data) {
                    const std::string encoded = data.dump();
                    if (!cell_ || owner.empty()) {
                        sendAgentEvent(requester, encoded.c_str());
                        return;
                    }
                    std::vector<int> recipients;
                    {
                        std::lock_guard<std::mutex> lock(clientsMtx_);
                        std::lock_guard<std::mutex> identityLock(cellMtx_);
                        for (const auto& [id, conn] : clients_)
                            if (conn->helloDone && conn->actorKey == owner) recipients.push_back(id);
                    }
                    const std::string line = json{{"event", "agent.event"}, {"data", data}}.dump();
                    for (int id : recipients) sendEventTo(id, line);
                };
            }
            // An online turn's budget is the caller's optional `timeout` (seconds). The
            // bridge wait is an IDLE bound (each streamed line restarts
            // WorkerBridge::readLine), sized past that budget so the worker's structured
            // "turn_budget" reply always arrives first. A turn without a budget has no
            // wall-clock stop (-1): it ends on its reply, Stop over the control lane,
            // worker death or bridge stop. Everything else keeps the ordinary 120 s bound
            // (omp_login: see below).
            int workerTimeoutMs = 120000;
            if (method == "agent_admin_turn") {
                workerTimeoutMs = -1;
                const auto t = p.find("timeout");
                if (t != p.end() && t->is_number() && std::isfinite(t->get<double>()) &&
                    t->get<double>() > 0) {
                    const double ms = (t->get<double>() + 60.0) * 1000.0;
                    workerTimeoutMs = ms >= 2147483647.0 ? 2147483647 : (int)ms;
                }
            } else if (method == "omp_login") {
                // One call carries the whole browser OAuth flow and streams nothing while the
                // user is away; the GUI's own bound (300 s) must end it, not this idle bound.
                workerTimeoutMs = 330000;
            }
            json workerParams = p;
            workerParams.erase("_cell_ctx");
            if (method == "agent_admin_turn") workerParams["_hostd_doc_scope"] = docScope;
            if (cell_ && method == "agent_admin_turn") {
                std::lock_guard<std::mutex> lock(cellMtx_);
                runOwners_[p.value("run_id", std::string())] = p.at("_cell_ctx").at("actor_key").get<std::string>();
            }
            // A desktop turn streams only to its requester. Once that connection is gone (the
            // GUI crashed or quit mid-run) the run keeps its conversation's worker slot while
            // nobody can see its events or answer its ask_user, and the reopened chat's next
            // turn queued behind it with no end (2026-11-04: "thinking" forever after a crash).
            // Its journal already holds the finished work, so the new turn stops it and
            // resumes from there.
            const bool trackTurn = !cell_ && method == "agent_admin_turn";
            const auto stringParam = [&p](const char* key) {
                const auto it = p.find(key);
                return it != p.end() && it->is_string() ? it->get<std::string>() : std::string();
            };
            const std::string turnRun = trackTurn ? stringParam("run_id") : std::string();
            const std::string turnSession = trackTurn ? stringParam("session_id") : std::string();
            if (!turnSession.empty()) supersedeOrphanedTurns(turnSession);
            struct LiveTurnGuard {
                Server& server;
                std::string run;
                ~LiveTurnGuard() {
                    if (run.empty()) return;
                    std::lock_guard<std::mutex> lock(server.liveTurnsMtx_);
                    server.liveTurns_.erase(run);
                }
            } liveTurnGuard{*this, std::string()};
            if (!turnRun.empty() && !turnSession.empty()) {
                json credentials = json::object();
                for (const char* key : {"byok_key", "byok_provider", "byok_tenant"})
                    if (const auto it = p.find(key); it != p.end()) credentials[key] = *it;
                std::lock_guard<std::mutex> lock(liveTurnsMtx_);
                if (liveTurns_.try_emplace(turnRun, LiveTurn{turnSession, &c, std::move(credentials)})
                        .second)
                    liveTurnGuard.run = turnRun;
            }
            const bool workerOk =
                WorkerBridge::dispatch(method, workerParams, wresult, werr,
                                       workerTimeoutMs, workerEvent,
                                       cell_ ? p.at("_cell_ctx") : json::object());
            if (workerOk) {
                rememberCellHandles(c, p, wresult);
                // Worker success: return result verbatim except any legacy
                // batched event copy; matching progress already streamed live.
                if (streamThis && wresult.is_object()) wresult.erase("events");
                return wresult;
            }
            isError = true;
            // Prefer the worker's structured envelope when present; else a
            // transport-level failure. Verbatim enough for clients that key on
            // code/message (including the worker's own unknown-method error).
            if (wresult.is_object() && (wresult.contains("code") || wresult.contains("message"))) {
                if (!wresult.contains("message"))
                    wresult["message"] = !werr.empty() ? werr : std::string("worker error");
                if (!wresult.contains("code")) wresult["code"] = "worker_error";
                return wresult;
            }
            return json{{"code", "worker_error"},
                        {"message", !werr.empty() ? werr : std::string("worker bridge failed")}};
        }
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
    if ((method == "doc_save" || method == "doc_reload") && result.is_object() && result.contains("ok") &&
        result["ok"].is_boolean() && !result["ok"].get<bool>()) return result;
    if (!internalWatcher) rememberCellHandles(c, p, result);
    // Revision sniff, minted-doc implicit attach, event fanout, _foreign_edits.
    registry_.postCall(c.id, c.kind, c.userId, c.userDisplay, method, p, result);
    if (internalWatcher && result.is_object()) result["_watch_reloaded"] = true;
    // Stamp origin_kind onto each doc_list entry so a GUI that connected after a
    // doc was minted can recover user-vs-AI ownership on bootstrap.
    if (method == "doc_list") registry_.annotateDocList(result, c.id);
    // Reconcile only snapshots covered by the exact successfully saved revision.
    if (method == "doc_save" && autosave_ && !p.value("preserve_path", false)) {
        const std::string savedPath = result.value("path", std::string());
        if (!savedPath.empty() && !autosave_->isAutosavePath(savedPath))
            autosave_->onDocSaved(p.value("doc_id", std::string()),
                                  result.value("saved_revision", -1L),
                                  result.value("metadata_revision", -1L));
    }
    // Human (gui) save clears that doc's undo/redo history (session stacks +
    // disk journal) and fans out doc.history_cleared so other GUIs grey undo.
    // Agent/mcp saves deliberately keep history so AI rollback paths still work.
    if (method == "doc_save" && c.kind == "gui" && !p.value("preserve_path", false)) {
        const std::string docId = p.value("doc_id", std::string());
        if (!docId.empty()) {
            json clr;
            std::string e2;
            if (core::cpp_core().call("doc_clear_history", json{{"doc_id", docId}}, clr, e2))
                registry_.notifyHistoryCleared(docId, c.id);
        }
    }
    if (!attachWarning.empty() && result.is_object() && !result.contains("warning"))
        result["warning"] = attachWarning;
    return result;
}

void Server::beginInflight(int clientId, const std::string& method) {
    std::lock_guard<std::mutex> lk(inflightMtx_);
    inflight_[clientId] = InflightCall{method, monoMs()};
}

void Server::endInflight(int clientId) {
    std::lock_guard<std::mutex> lk(inflightMtx_);
    inflight_.erase(clientId);
}

void Server::supersedeOrphanedTurns(const std::string& sessionId) {
    std::vector<std::pair<std::string, json>> orphans;
    {
        std::lock_guard<std::mutex> lock(liveTurnsMtx_);
        for (const auto& [run, turn] : liveTurns_)
            if (turn.sessionId == sessionId && turn.requester->conn.peerDisconnected())
                orphans.emplace_back(run, turn.credentials);
    }
    for (auto& [run, control] : orphans) {
        control["run_id"] = run;
        bool matched = false;
        json result;
        std::string err;
        const bool ok = WorkerBridge::controlActiveRun("cancel", control, matched, result, err);
        log().line("agent: stopping run %s of session %s, its requester disconnected (%s)",
                   run.c_str(), sessionId.c_str(),
                   ok ? "cancel sent" : err.empty() ? "no active worker run" : err.c_str());
    }
}

int Server::engineInflightCount() const {
    std::lock_guard<std::mutex> lk(inflightMtx_);
    return (int)inflight_.size();
}


// Background watchdog bounds engine calls that cannot be interrupted in-process.
// Agent turns end on their caller's optional budget, Stop, or worker death, not this tier.
void Server::watchdogLoop() {
    const long long longCeilingMs = engineWatchdogLongMs();
    while (!stopping_.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
        if (stopping_.load()) break;
        const long long now = monoMs();
        std::string method;
        const char* tier = nullptr;
        long long elapsed = 0;
        long long ceiling = 0;
        int clientId = 0;
        {
            std::lock_guard<std::mutex> lk(inflightMtx_);
            for (const auto& [cid, call] : inflight_) {
                const long long ms = now - call.startMs;
                if (engineCallOverdue(call.method, ms, engineWatchdogMs_)) {
                    tier = "interactive";
                    ceiling = engineWatchdogMs_;
                } else if (engineCallOverdueLong(call.method, ms, longCeilingMs)) {
                    tier = "long-running";
                    ceiling = longCeilingMs;
                } else {
                    continue;
                }
                method = call.method;
                elapsed = ms;
                clientId = cid;
                break;
            }
        }
        if (tier) {
            log().line("FATAL: %s engine call '%s' (client #%d) stuck %lldms > %lldms ceiling — "
                       "self-healing via restart; docs recover from autosave on next start",
                       tier, method.c_str(), clientId, elapsed, ceiling);
            std::fflush(stdout);
            std::fflush(stderr);
            std::_Exit(70);
        }
    }
}

}  // namespace hostd
