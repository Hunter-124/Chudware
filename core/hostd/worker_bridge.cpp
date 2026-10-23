// hostd → independent, lazily-spawned services worker stdio lanes.
#include "worker_bridge.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <stdexcept>
#include <cwctype>
#include <map>
#include <thread>
#include <unordered_map>
#include <vector>

#include "hostd_log.h"
#include "security/integrity.h"
#include "util/platform_paths.h"

namespace hostd {

std::atomic<bool> WorkerBridge::agentCreated_{false};
std::atomic<bool> WorkerBridge::generalCreated_{false};

struct WorkerBridge::AgentPool {
    struct Slot {
        bool reserved = false;
        bool reaping = false;
        std::string sessionId;
        std::chrono::steady_clock::time_point lastEnded = std::chrono::steady_clock::now();
    };
    std::mutex mutex;
    std::condition_variable changed;
    std::vector<WorkerBridge*> extras;
    std::unordered_map<WorkerBridge*, Slot> slots;
    std::unordered_map<std::string, WorkerBridge*> owners;
    bool stopping = false;
    const int cap = [] {
        const char* value = std::getenv("CHUDWARE_AGENT_WORKERS");
        if (!value || !*value) return 4;
        char* end = nullptr;
        const long parsed = std::strtol(value, &end, 10);
        if (end == value || *end) return 4;
        return static_cast<int>(std::clamp(parsed, 1L, 8L));
    }();
    ~AgentPool() {
        for (auto* bridge : extras) delete bridge;
    }
};

WorkerBridge::AgentPool& WorkerBridge::pool() {
    static AgentPool state;
    return state;
}

// Registry lifetime is independent of request threads. A request holds its generation
// alive after revocation removes it from the registry, so reservations never dangle.
struct WorkerBridge::ActorLanes {
    AgentPool state;
    WorkerBridge* primary;
    WorkerBridge* general;
    const nlohmann::json identity;
    explicit ActorLanes(const nlohmann::json& ctx)
        : primary(new WorkerBridge(true, false, ctx)),
          general(new WorkerBridge(false, false, ctx)), identity(ctx) {}
    ~ActorLanes() { delete primary; delete general; }
};

struct WorkerBridge::ActorRegistry {
    std::mutex mutex;
    std::unordered_map<std::string, std::shared_ptr<ActorLanes>> lanes;
    std::unordered_map<std::string, int64_t> revokedEpochs;
    bool stopping = false;
};

WorkerBridge::ActorRegistry& WorkerBridge::actorRegistry() {
    static ActorRegistry registry;
    return registry;
}

std::shared_ptr<WorkerBridge::ActorLanes> WorkerBridge::actorLanes(
    const nlohmann::json& ctx, bool create, std::string& err) {
#ifdef _WIN32
    // Actor cells require the POSIX sandbox; never launch them as desktop workers.
    err = "cell workers are unsupported on Windows";
    return {};
#endif
    if (!ctx.is_object() || ctx.value("cell", false) != true ||
        !ctx.contains("workspace_key") || !ctx["workspace_key"].is_string() ||
        !ctx.contains("actor_key") || !ctx["actor_key"].is_string() ||
        !ctx.contains("actor_hex") || !ctx["actor_hex"].is_string() ||
        !ctx.contains("epoch") || !ctx["epoch"].is_number_integer()) {
        err = "invalid cell worker context";
        return {};
    }
    const auto workspace = ctx["workspace_key"].get<std::string>();
    const auto actor = ctx["actor_key"].get<std::string>();
    const auto hex = ctx["actor_hex"].get<std::string>();
    if (workspace.empty() || actor.empty() || workspace.find('\0') != std::string::npos ||
        actor.find('\0') != std::string::npos || hex.size() != 32 ||
        !std::all_of(hex.begin(), hex.end(), [](unsigned char c) {
            return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
        })) {
        err = "invalid cell worker identity";
        return {};
    }
    const std::string key = workspace + '\0' + actor;
    const int64_t epoch = ctx["epoch"].get<int64_t>();
    auto& registry = actorRegistry();
    std::lock_guard<std::mutex> lock(registry.mutex);
    if (registry.stopping) { err = "worker bridge stopping"; return {}; }
    const auto revoked = registry.revokedEpochs.find(key);
    if (revoked != registry.revokedEpochs.end() && epoch <= revoked->second) {
        err = "revoked";
        return {};
    }
    const auto found = registry.lanes.find(key);
    if (found != registry.lanes.end()) {
        if (found->second->identity["epoch"] != ctx["epoch"] ||
            found->second->identity["actor_hex"] != ctx["actor_hex"]) {
            err = "revoked";
            return {};
        }
        return found->second;
    }
    if (!create) return {};
    auto lanes = std::make_shared<ActorLanes>(ctx);
    registry.lanes.emplace(key, lanes);
    return lanes;
}

void WorkerBridge::stopActorLanes(const std::shared_ptr<ActorLanes>& lanes) {
    std::vector<WorkerBridge*> bridges{lanes->primary, lanes->general};
    {
        std::lock_guard<std::mutex> lock(lanes->state.mutex);
        lanes->state.stopping = true;
        bridges.insert(bridges.end(), lanes->state.extras.begin(), lanes->state.extras.end());
        // Set every stop flag before waiting for any child. No lost call may respawn.
        for (auto* bridge : bridges) bridge->stopping_.store(true, std::memory_order_release);
    }
    lanes->state.changed.notify_all();
    std::vector<std::thread> stops;
    for (auto* bridge : bridges) stops.emplace_back([bridge] { bridge->stop(); });
    for (auto& thread : stops) thread.join();
}

void WorkerBridge::revokeActor(const std::string& actorKey,
                                const std::string& workspaceKey, std::int64_t revokedThrough) {
    std::vector<std::shared_ptr<ActorLanes>> retired;
    auto& registry = actorRegistry();
    {
        std::lock_guard<std::mutex> lock(registry.mutex);
        // A request admitted before revocation may not have reached dispatch yet.
        // Tombstone its epoch even if no worker generation was ever constructed.
        if (!workspaceKey.empty() && revokedThrough >= 0) {
            auto& tombstone = registry.revokedEpochs[workspaceKey + '\0' + actorKey];
            tombstone = std::max(tombstone, revokedThrough);
        }
        for (auto it = registry.lanes.begin(); it != registry.lanes.end(); ++it) {
            if (it->second->identity["actor_key"] != actorKey ||
                it->second->identity["workspace_key"] != workspaceKey ||
                it->second->identity["epoch"].get<int64_t>() > revokedThrough) continue;
            auto& tombstone = registry.revokedEpochs[it->first];
            tombstone = std::max(tombstone, it->second->identity["epoch"].get<int64_t>());
            auto lanes = it->second;
            {
                std::lock_guard<std::mutex> poolLock(lanes->state.mutex);
                lanes->state.stopping = true;
                lanes->primary->stopping_.store(true, std::memory_order_release);
                lanes->general->stopping_.store(true, std::memory_order_release);
                for (auto* extra : lanes->state.extras)
                    extra->stopping_.store(true, std::memory_order_release);
            }
            lanes->state.changed.notify_all();
            retired.push_back(std::move(lanes));
        }
    }
    for (const auto& lanes : retired) stopActorLanes(lanes);
    // Do not admit a fresh epoch until the old processes finish flushing their
    // actor-private session state.
    std::lock_guard<std::mutex> lock(registry.mutex);
    for (auto it = registry.lanes.begin(); it != registry.lanes.end();) {
        if (std::find(retired.begin(), retired.end(), it->second) != retired.end())
            it = registry.lanes.erase(it);
        else ++it;
    }
}

WorkerBridge& WorkerBridge::agent() {
    static WorkerBridge bridge(true);
    agentCreated_.store(true, std::memory_order_release);
    return bridge;
}

WorkerBridge& WorkerBridge::general() {
    static WorkerBridge bridge(false);
    generalCreated_.store(true, std::memory_order_release);
    return bridge;
}

bool WorkerBridge::isAgentMethod(const std::string& method) {
    // Reviewed against worker_main._METHODS registrations. These families share
    // Chudcode sessions, the agent prompt cache, subscription login progress,
    // benchmark subprocesses, or agent/subagent/DAG control state. The only
    // non-family exceptions are the control endpoint and trigger rule creation.
    // office_extract and all other registered worker families (PLM, printer,
    // parts, reveng, sim, deliverables, run_model, prompts) use general.
    static constexpr const char* agentPrefixes[] = {
        "agent_", "chudcode_", "session_", "subscription_", "skills_",
        "triggers_", "packs_", "ai_roles_", "model_rankings_", "bench_",
        "memory_", "subagent_", "dag_", "ai_prompt_", "ai_tool_",
    };
    if (method == "worker_control_info" || method == "rule_create")
        return true;
    // The sub-agent registry methods are served by the general lane so the GUI's Current Run
    // roster, transcript and Steer/Pause/Resume/Terminate keep working while a turn holds
    // its agent worker for hours (registry sub-agents only exist during such a turn). Invariant
    // that makes this safe: services/agent/subagent_rpc.py serves all five purely from the
    // on-disk registry (services/agent/subagent_registry.py) with no in-memory agent-lane state —
    // metadata is replaced atomically and every read-modify-write of it holds the per-session
    // cross-process lock (_update_meta), because the runners writing the same records live in
    // the agent-lane worker; transcript/steer logs are O_APPEND with torn lines skipped. Keep
    // this list explicit: a new subagent_* method goes here only if it keeps that invariant.
    static constexpr const char* generalSubagentMethods[] = {
        "subagent_list", "subagent_transcript", "subagent_steer", "subagent_control",
        "subagent_terminate",
    };
    for (const char* name : generalSubagentMethods)
        if (method == name) return false;
    for (const char* prefix : agentPrefixes)
        if (method.compare(0, std::strlen(prefix), prefix) == 0)
            return true;
    return false;
}

WorkerBridge& WorkerBridge::forMethod(const std::string& method) {
    return isAgentMethod(method) ? agent() : general();
}

void WorkerBridge::shutdownIfCreated() {
    std::vector<std::shared_ptr<ActorLanes>> cells;
    auto& registry = actorRegistry();
    {
        std::lock_guard<std::mutex> lock(registry.mutex);
        registry.stopping = true;
        for (const auto& entry : registry.lanes) cells.push_back(entry.second);
        registry.lanes.clear();
    }
    for (const auto& lanes : cells) stopActorLanes(lanes);
    auto& state = pool();
    std::vector<WorkerBridge*> extras;
    {
        std::lock_guard<std::mutex> lock(state.mutex);
        state.stopping = true;
        extras = state.extras;
    }
    state.changed.notify_all();
    std::vector<std::thread> stops;
    stops.reserve(extras.size());
    for (auto* bridge : extras) stops.emplace_back([bridge] { bridge->stop(); });
    if (agentCreated_.load(std::memory_order_acquire)) agent().stop();
    if (generalCreated_.load(std::memory_order_acquire)) general().stop();
    for (auto& thread : stops) thread.join();
}

WorkerBridge::Activity WorkerBridge::activity() {
    Activity result;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(2000);
    auto visit = [&](WorkerBridge* bridge, AgentPool* state) {
        std::unique_lock<std::mutex> poolLock;
        if (state) {
            poolLock = std::unique_lock<std::mutex>(state->mutex);
            const auto slot = state->slots.find(bridge);
            if (slot != state->slots.end() && (slot->second.reserved || slot->second.reaping)) {
                ++result.busy;
                return;
            }
        }
        std::unique_lock<std::timed_mutex> lane(bridge->mtx_, std::try_to_lock);
        if (poolLock.owns_lock()) poolLock.unlock();
        if (!lane.owns_lock()) { ++result.busy; return; }
        if (!bridge->running_) return;
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now()).count();
        if (remaining <= 0) { ++result.unknown; return; }
        std::uint64_t active = 0;
        bool complete = false;
        try { complete = bridge->activityLocked(active, static_cast<int>(remaining)); } catch (...) {}
        if (active > static_cast<std::uint64_t>(INT64_MAX) - result.background) {
            result.background = static_cast<std::uint64_t>(INT64_MAX);
            complete = false;
        } else result.background += active;
        if (!complete) ++result.unknown;
    };
    // Retain generation ownership without holding the registry during stdio I/O.
    std::vector<std::shared_ptr<ActorLanes>> actors;
    {
        auto& registry = actorRegistry();
        std::lock_guard<std::mutex> lock(registry.mutex);
        for (const auto& [key, lanes] : registry.lanes) actors.push_back(lanes);
    }
    for (const auto& lanes : actors) {
        visit(lanes->primary, &lanes->state);
        visit(lanes->general, nullptr);
        std::vector<WorkerBridge*> extras;
        {
            std::lock_guard<std::mutex> lock(lanes->state.mutex);
            extras = lanes->state.extras;
        }
        for (auto* bridge : extras) visit(bridge, &lanes->state);
    }
    if (agentCreated_.load(std::memory_order_acquire)) visit(&agent(), &pool());
    if (generalCreated_.load(std::memory_order_acquire)) visit(&general(), nullptr);
    std::vector<WorkerBridge*> extras;
    {
        auto& state = pool();
        std::lock_guard<std::mutex> lock(state.mutex);
        extras = state.extras;
    }
    for (auto* bridge : extras) visit(bridge, &pool());
    return result;
}

void WorkerBridge::reapIdleExtras(AgentPool& state) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(2000);
    for (;;) {
        if (std::chrono::steady_clock::now() >= deadline) return;
        WorkerBridge* idle = nullptr;
        std::unique_lock<std::timed_mutex> laneLock;
        {
            std::lock_guard<std::mutex> lock(state.mutex);
            if (state.stopping) return;
            const auto now = std::chrono::steady_clock::now();
            for (auto* bridge : state.extras) {
                auto& slot = state.slots.at(bridge);
                if (slot.reserved || slot.reaping ||
                    now - slot.lastEnded <= std::chrono::minutes(10))
                    continue;
                laneLock = std::unique_lock<std::timed_mutex>(bridge->mtx_, std::try_to_lock);
                if (!laneLock.owns_lock()) continue;
                slot.lastEnded = now;
                slot.reaping = true;
                idle = bridge;
                break;
            }
        }
        if (!idle) return;
        // Its conversations stay owned until the child has exited: the stopping worker's
        // Chudcode processes write their session files on the way out, and another worker
        // resuming one of them before that write would fork its history.
        std::uint64_t active = 0;
        bool safe = !idle->running_;
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now()).count();
        if (!safe && remaining > 0) try {
            safe = idle->activityLocked(active, static_cast<int>(remaining)) && active == 0;
        } catch (...) {}
        if (safe) idle->stopChildLocked();
        laneLock.unlock();
        {
            std::lock_guard<std::mutex> lock(state.mutex);
            state.slots.at(idle).reaping = false;
            if (safe) for (auto it = state.owners.begin(); it != state.owners.end();) {
                if (it->second == idle) it = state.owners.erase(it);
                else ++it;
            }
        }
        state.changed.notify_all();
    }
}

void WorkerBridge::releaseTurn(AgentPool& state, WorkerBridge* bridge) {
    {
        std::lock_guard<std::mutex> lock(state.mutex);
        auto& slot = state.slots.at(bridge);
        slot.reserved = false;
        slot.sessionId.clear();
        slot.lastEnded = std::chrono::steady_clock::now();
    }
    // No reaping here: this runs before the finished turn's reply is sent, and stopping
    // an idle worker can take seconds. The next turn's selection reaps instead.
    state.changed.notify_all();
}

bool WorkerBridge::dispatch(const std::string& method, const nlohmann::json& params,
                            nlohmann::json& result, std::string& err, int timeoutMs,
                            const EventFn& onEvent, const nlohmann::json& ctx) {
    std::shared_ptr<ActorLanes> lanes;
    if (ctx.is_object() && ctx.value("cell", false)) {
        lanes = actorLanes(ctx, true, err);
        if (!lanes) {
            result = nlohmann::json{{"code", err == "revoked" ? "revoked" : "cell_refused"},
                                    {"message", err}};
            return false;
        }
    }
    WorkerBridge& primary = lanes ? (isAgentMethod(method) ? *lanes->primary : *lanes->general)
                                  : forMethod(method);
    if (!primary.agentLane_)
        return primary.call(method, params, result, err, timeoutMs, onEvent, ctx);
    std::string sid;
    if (params.is_object()) {
        const auto it = params.find("session_id");
        if (it != params.end() && it->is_string()) sid = it->get<std::string>();
    }
    auto& state = lanes ? lanes->state : pool();
    if (method != "agent_admin_turn") {
        WorkerBridge* bridge = &primary;
        if (!sid.empty() && method != "worker_control_info" && method != "rule_create") {
            std::lock_guard<std::mutex> lock(state.mutex);
            const auto owner = state.owners.find(sid);
            if (owner != state.owners.end()) bridge = owner->second;
        }
        return bridge->call(method, params, result, err, timeoutMs, onEvent, ctx);
    }

    if (timeoutMs == 0) timeoutMs = lanes ? -1 : 120000;
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(timeoutMs < 0 ? 0 : timeoutMs);
    reapIdleExtras(state);
    WorkerBridge* chosen = nullptr;
    // A conversation's live Chudcode runtime must exist in ONE worker. When its owner is busy
    // with another conversation, the turn moves to a free worker only after the owner has
    // released (disposed) its idle runtime; otherwise both would append to one session file
    // from different leaves and fork the history. Owner unreachable -> wait for the owner.
    bool ownerOnly = false;
    for (;;) {
        WorkerBridge* previousOwner = nullptr;
        {
            std::unique_lock<std::mutex> lock(state.mutex);
            state.slots.try_emplace(&primary);
            for (;;) {
                if (state.stopping) {
                    err = "worker bridge stopping";
                    return false;
                }
                chosen = nullptr;
                previousOwner = nullptr;
                bool waitForOwner = false;
                const auto owner = sid.empty() ? state.owners.end() : state.owners.find(sid);
                if (owner != state.owners.end()) {
                    const auto& slot = state.slots.at(owner->second);
                    if (!slot.reserved && !slot.reaping) chosen = owner->second;
                    else waitForOwner = ownerOnly || slot.reaping || slot.sessionId == sid;
                    if (!chosen && !waitForOwner) previousOwner = owner->second;
                }
                if (!chosen && !waitForOwner) {
                    auto available = [&](WorkerBridge* bridge) {
                        const auto& slot = state.slots.at(bridge);
                        return !slot.reserved && !slot.reaping;
                    };
                    if (available(&primary)) chosen = &primary;
                    else {
                        for (auto* bridge : state.extras)
                            if (available(bridge)) {
                                chosen = bridge;
                                break;
                            }
                    }
                    if (!chosen && state.extras.size() + 1 < static_cast<size_t>(state.cap)) {
                        chosen = new WorkerBridge(true, true, lanes ? lanes->identity
                                                                  : nlohmann::json::object());
                        state.extras.push_back(chosen);
                        state.slots.try_emplace(chosen);
                    }
                }
                if (chosen) {
                    auto& slot = state.slots.at(chosen);
                    slot.reserved = true;
                    slot.sessionId = sid;
                    if (!sid.empty()) state.owners[sid] = chosen;
                    break;
                }
                if (timeoutMs < 0) state.changed.wait(lock);
                else if (state.changed.wait_until(lock, deadline) == std::cv_status::timeout) {
                    err = "worker lane busy (agent turn in progress)";
                    result = nlohmann::json{{"code", "worker_lane_busy"}, {"message", err}};
                    return false;
                }
            }
        }
        if (!previousOwner) break;
        std::string releaseErr;
        if (previousOwner->releaseSession(sid, releaseErr, ctx)) break;
        log().line("worker_bridge: session %s not released by its worker (%s); waiting for it",
                   sid.c_str(), releaseErr.c_str());
        {
            std::lock_guard<std::mutex> lock(state.mutex);
            auto& slot = state.slots.at(chosen);
            slot.reserved = false;
            slot.sessionId.clear();
            state.owners[sid] = previousOwner;
        }
        state.changed.notify_all();
        ownerOnly = true;
    }
    struct TurnReservation {
        AgentPool& state;
        WorkerBridge* bridge;
        ~TurnReservation() { WorkerBridge::releaseTurn(state, bridge); }
    } reservation{state, chosen};
    return chosen->call(method, params, result, err, timeoutMs, onEvent, ctx);
}

}  // namespace hostd

#ifdef _WIN32
#include "../../services/ipc/ipc_windows_local.h"
#include "../../services/worker/worker_client.h"
#else
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#include "util/parent_death.h"
#endif

namespace hostd {

using nlohmann::json;

namespace {

std::string nativeUtf8(const std::filesystem::path::string_type& value) {
#ifdef _WIN32
    if (value.empty()) return {};
    const int length = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    if (!length) throw std::runtime_error("worker UTF-16 conversion failed");
    std::string result(static_cast<std::size_t>(length), '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
            static_cast<int>(value.size()), result.data(), length, nullptr, nullptr))
        throw std::runtime_error("worker UTF-16 conversion failed");
    return result;
#else
    return value;
#endif
}

std::string envOr(const char* key, const std::string& def) {
#ifdef _WIN32
    const std::wstring name(key, key + std::strlen(key));
    const DWORD size = GetEnvironmentVariableW(name.c_str(), nullptr, 0);
    if (size) {
        std::wstring value(size, L'\0');
        const DWORD count = GetEnvironmentVariableW(name.c_str(), value.data(), size);
        if (count && count < size) {
            value.resize(count);
            return nativeUtf8(value);
        }
    }
#else
    if (const char* v = std::getenv(key); v && *v) return std::string(v);
#endif
    return def;
}

#ifndef _WIN32
std::string homeDir() {
    if (const char* h = std::getenv("HOME"); h && *h) return std::string(h);
    return std::string();
}
#endif

#ifdef CHUDWARE_DEVELOPER_BUILD
// Developer worker cwd: CHUDWARE_REPO, else the source checkout this binary runs
// from, else ".".
std::string developerRepoRoot() {
    if (std::string env = envOr("CHUDWARE_REPO", ""); !env.empty()) return env;
    const auto root = chudware::platform::developerSourceRoot();
    return root.empty() ? std::string(".") : nativeUtf8(root.native());
}
#endif

bool fileExists(const std::string& p) {
    std::error_code ec;
    return std::filesystem::is_regular_file(std::filesystem::u8path(p), ec);
}


std::string adjacentWorkerBinary(std::string& err) {
    const std::string executable = chudware::platform::executablePath();
    if (executable.empty()) {
        err = "could not resolve the current executable path";
        return {};
    }
#ifdef _WIN32
    constexpr const char* name = "chudware-worker.exe";
#else
    constexpr const char* name = "chudware-worker";
#endif
    std::error_code ec;
    const auto candidate = std::filesystem::weakly_canonical(
        std::filesystem::u8path(executable).parent_path() / name, ec);
    if (ec) {
        err = "could not canonicalize adjacent worker path";
        return {};
    }
    return nativeUtf8(candidate.native());
}

bool sameCanonicalPath(const std::string& left, const std::string& right) {
    std::error_code leftEc, rightEc;
    const auto canonicalLeft = std::filesystem::weakly_canonical(std::filesystem::u8path(left), leftEc);
    const auto canonicalRight = std::filesystem::weakly_canonical(std::filesystem::u8path(right), rightEc);
    return !leftEc && !rightEc && canonicalLeft == canonicalRight;
}

constexpr size_t kControlRequestLimit = 64 * 1024;
constexpr size_t kControlResponseLimit = 256 * 1024;
constexpr auto kControlTimeout = std::chrono::milliseconds(2500);
// release stops a live Chudcode process, which flushes its session file before exiting.
constexpr auto kReleaseTimeout = std::chrono::milliseconds(15000);

void wipeString(std::string& value) {
    volatile char* p = value.empty() ? nullptr : value.data();
    for (size_t i = 0; i < value.size(); ++i) p[i] = 0;
    value.clear();
}

bool validControlToken(const std::string& token) {
    return token.size() == 64 &&
           std::all_of(token.begin(), token.end(), [](unsigned char c) {
               return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
           });
}

struct SensitiveLine {
    std::string value;
    ~SensitiveLine() { wipeString(value); }
};

bool validControlInfo(const std::string& transport, const std::string& endpoint,
                      const std::string& token) {
    if (!validControlToken(token)) return false;
#ifdef _WIN32
    // Reuse the GUI worker's native endpoint/token validation.
    json value{{"transport", transport}, {"endpoint", endpoint}, {"token", token}};
    chudware::worker::detail::ControlInfo parsed;
    std::string err;
    const bool valid = chudware::worker::detail::parseControlInfo(value, true, parsed, err);
    wipeString(value["token"].get_ref<std::string&>());
    wipeString(parsed.token);
    return valid;
#else
    if (!validControlToken(token) || transport != "unix" || endpoint.empty() ||
        endpoint.front() != '/' || endpoint.size() > 103)
        return false;
    return std::none_of(endpoint.begin(), endpoint.end(), [](unsigned char c) {
        return c < 0x20 || c == 0x7f;
    });
#endif
}

#ifndef _WIN32
// The caller owns the cell lane and is the sole waitpid owner.
void terminateCellProcess(pid_t pid) {
    ::kill(-pid, SIGTERM);
    ::kill(pid, SIGTERM);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    while (std::chrono::steady_clock::now() < deadline) {
        const pid_t reaped = ::waitpid(pid, nullptr, WNOHANG);
        if (reaped == pid || (reaped < 0 && errno == ECHILD)) return;
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    ::kill(-pid, SIGKILL);
    ::kill(pid, SIGKILL);
    while (::waitpid(pid, nullptr, 0) < 0 && errno == EINTR) {}
}

int remainingMs(const std::chrono::steady_clock::time_point deadline) {
    const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
                          deadline - std::chrono::steady_clock::now())
                          .count();
    if (left <= 0) return 0;
    return static_cast<int>(std::min<long long>(left, 2500));
}

bool waitSocket(int fd, short events,
                const std::chrono::steady_clock::time_point deadline) {
    for (;;) {
        const int timeout = remainingMs(deadline);
        if (timeout <= 0) return false;
        pollfd pfd{fd, events, 0};
        const int rc = ::poll(&pfd, 1, timeout);
        if (rc > 0) return (pfd.revents & events) != 0;
        if (rc == 0) return false;
        if (errno != EINTR) return false;
    }
}
#endif

bool sendControlRequest(const std::string& transport, const std::string& endpoint,
                        const std::string& token, const std::string& action,
                        const json& params, json& result, std::string& err,
                        const json& ctx,
                        std::chrono::milliseconds timeout = kControlTimeout) {
    result = json::object();
    err.clear();
    if (!validControlInfo(transport, endpoint, token) ||
        (action != "cancel" && action != "steer" && action != "answer" &&
         action != "status" && action != "release") ||
        !params.is_object()) {
        err = "worker control information is invalid";
        return false;
    }
    SensitiveLine request;
    std::string paramsText;
    try {
        paramsText = params.dump();
        request.value.reserve(token.size() + action.size() + paramsText.size() + 40);
        request.value = "{\"token\":\"";
        request.value += token;
        request.value += "\",\"action\":\"";
        request.value += action;
        request.value += "\",\"params\":";
        request.value += paramsText;
        if (!ctx.empty()) {
            request.value += ",\"ctx\":";
            request.value += ctx.dump();
        }
        request.value += "}\n";
    } catch (...) {
        err = "worker control params are not serializable";
        return false;
    }
    if (request.value.size() > kControlRequestLimit) {
        err = "worker control request exceeded limit";
        return false;
    }

    std::string response;
#ifdef _WIN32
    if (!chudware_ipc::exchangeLocalPipe(endpoint, request.value, response,
            kControlResponseLimit, static_cast<int>(timeout.count()), err)) return false;
#else
    const int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        err = "worker control socket failed";
        return false;
    }
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    const int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) != 0) {
        ::close(fd);
        err = "worker control socket setup failed";
        return false;
    }

    const auto deadline = std::chrono::steady_clock::now() + timeout;
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    std::memcpy(address.sun_path, endpoint.data(), endpoint.size());
    address.sun_path[endpoint.size()] = '\0';
    const int connected =
        ::connect(fd, reinterpret_cast<const sockaddr*>(&address), sizeof(address));
    if (connected != 0) {
        if (errno != EINPROGRESS || !waitSocket(fd, POLLOUT, deadline)) {
            ::close(fd);
            err = "worker control connect failed";
            return false;
        }
        int socketError = 0;
        socklen_t errorSize = sizeof(socketError);
        if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &socketError, &errorSize) != 0 ||
            socketError != 0) {
            ::close(fd);
            err = "worker control connect failed";
            return false;
        }
    }

    size_t sent = 0;
    while (sent < request.value.size()) {
        if (!waitSocket(fd, POLLOUT, deadline)) {
            ::close(fd);
            err = "worker control send timed out";
            return false;
        }
#ifdef MSG_NOSIGNAL
        const ssize_t count =
            ::send(fd, request.value.data() + sent, request.value.size() - sent,
                   MSG_NOSIGNAL);
#else
        const ssize_t count =
            ::send(fd, request.value.data() + sent, request.value.size() - sent, 0);
#endif
        if (count > 0) {
            sent += static_cast<size_t>(count);
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
            continue;
        ::close(fd);
        err = "worker control send failed";
        return false;
    }

    for (;;) {
        const size_t newline = response.find('\n');
        if (newline != std::string::npos) {
            if (newline > kControlResponseLimit) {
                ::close(fd);
                err = "worker control response exceeded limit";
                return false;
            }
            response.resize(newline);
            break;
        }
        if (response.size() > kControlResponseLimit) {
            ::close(fd);
            err = "worker control response exceeded limit";
            return false;
        }
        if (!waitSocket(fd, POLLIN, deadline)) {
            ::close(fd);
            err = "worker control response timed out";
            return false;
        }
        char chunk[4096];
        const ssize_t count = ::recv(fd, chunk, sizeof(chunk), 0);
        if (count > 0) {
            response.append(chunk, static_cast<size_t>(count));
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
            continue;
        ::close(fd);
        err = "worker control response closed";
        return false;
    }
    ::close(fd);
#endif

    if (response.find(token) != std::string::npos) {
        err = "worker control response exposed authentication material";
        return false;
    }
    json decoded;
    try {
        decoded = json::parse(response);
    } catch (...) {
        err = "worker control response was invalid";
        return false;
    }
    if (!decoded.is_object()) {
        err = "worker control response was invalid";
        return false;
    }
    const auto ok = decoded.find("ok");
    if (ok == decoded.end() || !ok->is_boolean()) {
        err = "worker control response was invalid";
        return false;
    }
    if (!ok->get<bool>()) {
        err = "worker control request failed";
        return false;
    }
    const auto it = decoded.find("result");
    if (it == decoded.end() || !it->is_object()) {
        err = "worker control response was invalid";
        return false;
    }
    result = *it;
    return true;
}
}  // namespace

WorkerBridge::~WorkerBridge() {
    stop();
#ifdef _WIN32
    if (stdoutEvent_) CloseHandle(stdoutEvent_);
#endif
}

void WorkerBridge::clearActiveRun() {
    std::lock_guard<std::mutex> lk(controlMtx_);
    wipeString(activeWorkerRunId_);
    activeCtx_ = json::object();
}

void WorkerBridge::clearControlState() {
    std::lock_guard<std::mutex> lk(controlMtx_);
    wipeString(control_.token);
    wipeString(control_.endpoint);
    wipeString(control_.transport);
    wipeString(activeWorkerRunId_);
    activeCtx_ = json::object();
}

bool WorkerBridge::controlActiveRun(const std::string& action, const json& params,
                                    bool& matched, json& result, std::string& err,
                                    const json& ctx) {
    matched = false;
    err.clear();
    std::vector<WorkerBridge*> bridges;
    std::shared_ptr<ActorLanes> lanes;
    if (ctx.is_object() && ctx.value("cell", false)) {
        lanes = actorLanes(ctx, false, err);
        if (!lanes) return false;
    }
    auto& state = lanes ? lanes->state : pool();
    {
        std::lock_guard<std::mutex> lock(state.mutex);
        if (lanes) bridges.push_back(lanes->primary);
        else if (agentCreated_.load(std::memory_order_acquire)) bridges.push_back(&agent());
        bridges.insert(bridges.end(), state.extras.begin(), state.extras.end());
    }
    for (auto* bridge : bridges) {
        if (bridge->controlActiveRunImpl(action, params, matched, result, err, ctx)) return true;
        if (matched) return false;
    }
    return false;
}

bool WorkerBridge::releaseSession(const std::string& sessionId, std::string& err,
                                  const json& ctx) {
    err.clear();
    // No child: its Chudcode processes died with it, nothing is live to release.
    if (livePid_.load(std::memory_order_acquire) <= 0) return true;
    // Copied out: releasing can take seconds, and Stop for this worker's own running turn
    // must not wait behind it on controlMtx_.
    ControlInfo info;
    {
        std::lock_guard<std::mutex> lk(controlMtx_);
        info = control_;
    }
    struct Wipe {
        ControlInfo& info;
        ~Wipe() { wipeString(info.token); }
    } wipe{info};
    if (info.token.empty()) {
        err = "worker control endpoint unknown";
        return false;
    }
    json result;
    if (!sendControlRequest(info.transport, info.endpoint, info.token, "release",
                            json{{"session_id", sessionId}}, result, err, ctx, kReleaseTimeout))
        return false;
    if (result.value("ok", false)) return true;
    err = result.value("reason", std::string("release refused"));
    return false;
}

bool WorkerBridge::controlActiveRunImpl(const std::string& action, const json& params,
                                        bool& matched, json& result, std::string& err,
                                        const json& ctx) {
    matched = false;
    result = json::object();
    err.clear();
    if (!params.is_object()) return false;
    const auto runIt = params.find("run_id");
    if (runIt == params.end() || !runIt->is_string()) return false;
    const std::string runId = runIt->get<std::string>();
    if (runId.empty()) return false;

    // The endpoint belongs to the worker, not to one private connection. Copy it
    // under the state lock so a slow control request cannot delay another run's
    // control routing, a terminal response, or child teardown.
    ControlInfo info;
    {
        std::lock_guard<std::mutex> lk(controlMtx_);
        if (activeWorkerRunId_ != runId) return false;
        if (!identity_.empty() &&
            (!ctx.is_object() || ctx.value("workspace_key", std::string()) !=
                                     identity_.value("workspace_key", std::string()) ||
             ctx.value("actor_key", std::string()) != identity_.value("actor_key", std::string()) ||
             ctx.value("actor_hex", std::string()) != identity_.value("actor_hex", std::string()) ||
             ctx.value("epoch", int64_t{-1}) != identity_.value("epoch", int64_t{-1})))
            return false;
        matched = true;
        if (control_.token.empty()) {
            err = "worker control endpoint unavailable for active run";
            return false;
        }
        info = control_;
    }
    struct Wipe {
        ControlInfo& info;
        ~Wipe() { wipeString(info.token); }
    } wipe{info};
    // A refusal, timeout, or bad/closed response does not prove worker replacement.
    // Retain its credentials until reap()/stopChildLocked() clears them; stdio
    // cannot reacquire them while this run occupies the lane.
    return sendControlRequest(info.transport, info.endpoint, info.token,
                              action, params, result, err, ctx);
}

void WorkerBridge::stop() {
    stopping_.store(true, std::memory_order_release);
    std::unique_lock<std::timed_mutex> lk(mtx_, std::try_to_lock);
    if (!lk.owns_lock()) {
        // A call holds the lane — an agent turn holds it for hours. Waiting here wedged the
        // daemon on tray Quit / SIGTERM mid-turn (2026-09-24): the accept loop was gone, the
        // worker's engine socket hung, and the turn's next CAD call blocked forever. Ask the
        // active run to cancel, give it a bounded moment to return, then end the child so
        // the blocked readLine sees EOF and releases the lane.
        std::string runId;
        json ctx;
        {
            std::lock_guard<std::mutex> controlLock(controlMtx_);
            runId = activeWorkerRunId_;
            ctx = activeCtx_;
        }
        if (!runId.empty()) {
            bool matched = false;
            json ignored;
            std::string cancelErr;
            controlActiveRunImpl("cancel", json{{"run_id", runId}}, matched, ignored, cancelErr, ctx);
        }
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
        while (!lk.try_lock() && std::chrono::steady_clock::now() < deadline)
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        if (!lk.owns_lock()) {
            terminateInFlightChild();
#ifndef _WIN32
            if (!identity_.empty()) {
                // A sandbox/provider ignoring TERM must not hold revocation forever.
                const auto killDeadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
                while (!lk.try_lock() && std::chrono::steady_clock::now() < killDeadline)
                    std::this_thread::sleep_for(std::chrono::milliseconds(20));
                if (!lk.owns_lock()) {
                    const pid_t pid = livePid_.load(std::memory_order_acquire);
                    if (pid > 0) { ::kill(-pid, SIGKILL); ::kill(pid, SIGKILL); }
                    lk.lock();
                }
            } else lk.lock();
#else
            lk.lock();  // Job termination unblocks both stdio directions.
#endif
        }
    }
    stopChildLocked();
}

void WorkerBridge::stopChildLocked() {
    clearControlState();
#ifdef _WIN32
    if (childStdinWr_) {
        // EOF runs worker_main's shutdown/finally without risking a blocking
        // shutdown write behind a poisoned or unresponsive stdio handler.
        CloseHandle(childStdinWr_);
        childStdinWr_ = nullptr;
    }
    if (process_) WaitForSingleObject(process_, 2000);
    reap();
#else
    if (!running_ && process_ < 0 && childStdinWr_ < 0 && childStdoutRd_ < 0) return;
    // Soft shutdown request, then reap (mirrors worker_client.cpp stop()).
    if (childStdinWr_ >= 0) {
        const std::string bye = identity_.empty()
            ? "{\"id\":0,\"method\":\"shutdown\"}\n"
            : json{{"id", 0}, {"method", "shutdown"}, {"params", json::object()},
                   {"ctx", identity_}}.dump() + "\n";
        ssize_t w = ::write(childStdinWr_, bye.data(), bye.size());
        (void)w;
        ::close(childStdinWr_);
        childStdinWr_ = -1;
    }
    if (process_ >= 0) {
        bool reaped = false;
        for (int i = 0; i < 200; ++i) {
            int status = 0;
            pid_t r = waitpid(process_, &status, WNOHANG);
            if (r == process_ || (r < 0 && errno == ECHILD)) {
                reaped = true;
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        if (!reaped) {
            if (!identity_.empty()) terminateCellProcess(process_);
            else {
                ::kill(process_, SIGTERM);
                waitpid(process_, nullptr, 0);
            }
        }
        process_ = -1;
        livePid_.store(-1, std::memory_order_release);
    }
    if (childStdoutRd_ >= 0) {
        ::close(childStdoutRd_);
        childStdoutRd_ = -1;
    }
    readBuf_.clear();
    running_ = false;
#endif
}

void WorkerBridge::reap() {
    // Caller holds mtx_. Tear down without the graceful shutdown write (child
    // already dead or pipe broken).
#ifdef _WIN32
    // Closing the noninherited job also kills straggling grandchildren after an
    // ordinary worker exit; no RPC process may survive its owning generation.
    {
        std::lock_guard<std::mutex> lock(nativeMtx_);
        if (childJob_) {
            TerminateJobObject(childJob_, 1);
            CloseHandle(childJob_);
            childJob_ = nullptr;
        }
        if (process_) {
            WaitForSingleObject(process_, 2000);
            CloseHandle(process_);
            process_ = nullptr;
        }
        livePid_.store(-1, std::memory_order_release);
    }
    if (childStdinWr_) { CloseHandle(childStdinWr_); childStdinWr_ = nullptr; }
    if (childStdoutRd_) { CloseHandle(childStdoutRd_); childStdoutRd_ = nullptr; }
#else
    if (childStdinWr_ >= 0) {
        ::close(childStdinWr_);
        childStdinWr_ = -1;
    }
    if (childStdoutRd_ >= 0) {
        ::close(childStdoutRd_);
        childStdoutRd_ = -1;
    }
    if (process_ >= 0) {
        int status = 0;
        pid_t r = waitpid(process_, &status, WNOHANG);
        if (r == 0) {
            if (!identity_.empty()) terminateCellProcess(process_);
            else {
                ::kill(process_, SIGTERM);
                waitpid(process_, nullptr, 0);
            }
        } else if (r < 0 && errno != ECHILD) {
            // best-effort
            ::kill(process_, SIGKILL);
            waitpid(process_, nullptr, 0);
        }
        process_ = -1;
        livePid_.store(-1, std::memory_order_release);
    }
#endif
    readBuf_.clear();
    running_ = false;
    clearControlState();
}

bool WorkerBridge::transportReady() const {
#ifdef _WIN32
    return process_ && childStdinWr_ && childStdoutRd_ && stdoutEvent_;
#else
    return process_ >= 0 && childStdinWr_ >= 0 && childStdoutRd_ >= 0;
#endif
}

bool WorkerBridge::childAlive() {
#ifdef _WIN32
    return process_ && WaitForSingleObject(process_, 0) == WAIT_TIMEOUT;
#else
    if (process_ < 0) return false;
    const pid_t reaped = ::waitpid(process_, nullptr, WNOHANG);
    if (reaped == process_ || (reaped < 0 && errno == ECHILD)) {
        process_ = -1;
        livePid_.store(-1, std::memory_order_release);
        return false;
    }
    return true;
#endif
}

void WorkerBridge::terminateInFlightChild() {
#ifdef _WIN32
    std::lock_guard<std::mutex> lock(nativeMtx_);
    if (childJob_) TerminateJobObject(childJob_, 1);
#else
    const pid_t pid = static_cast<pid_t>(livePid_.load(std::memory_order_acquire));
    if (pid > 0) ::kill(pid, SIGTERM);
#endif
}

namespace {
bool allowWorkerSpawn(std::string& err) {
    const auto executable = chudware::platform::executablePath();
    const auto integrity = chudware::security::verifyInstalledPayload(
        executable, chudware::security::VerifyMode::ProtectedOperation);
    if (!integrity.ok) {
        err = "worker spawn blocked: protected_file_hash_mismatch";
        return false;
    }
    return true;
}

#ifdef _WIN32
// Native process plumbing follows WorkerClient and ChildSupervisor: UTF-16
// environment, explicit inherited handles, suspended launch, then job ownership.
struct WinHandle {
    HANDLE value = nullptr;
    ~WinHandle() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); }
    HANDLE release() { HANDLE result = value; value = nullptr; return result; }
};

std::string nativeError(const char* operation) {
    return std::string(operation) + " failed (" + std::to_string(GetLastError()) + ")";
}

bool overlappedWorkerOutput(WinHandle& parent, WinHandle& child, HANDLE event,
                            std::string& err) {
    auto& security = chudware_ipc::privateSecurity();
    if (!security.valid()) { err = "worker stdio private security unavailable"; return false; }
    // Private anonymous-name pipe: only the parent's end is overlapped. The
    // worker still receives an ordinary synchronous stdout handle.
    static std::atomic<std::uint64_t> sequence{0};
    const std::wstring name = L"\\\\.\\pipe\\chudware-worker-stdout-" +
        std::to_wstring(GetCurrentProcessId()) + L"-" +
        std::to_wstring(sequence.fetch_add(1, std::memory_order_relaxed));
    parent.value = CreateNamedPipeW(name.c_str(),
        PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED | FILE_FLAG_FIRST_PIPE_INSTANCE,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
        1, 0, 65536, 0, &security.attributes);
    if (parent.value == INVALID_HANDLE_VALUE) {
        err = nativeError("worker stdout pipe creation"); return false;
    }
    SECURITY_ATTRIBUTES inherited = security.attributes;
    inherited.bInheritHandle = TRUE;
    child.value = CreateFileW(name.c_str(), GENERIC_WRITE, 0, &inherited,
        OPEN_EXISTING, SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, nullptr);
    if (child.value == INVALID_HANDLE_VALUE) {
        err = nativeError("worker stdout pipe connection"); return false;
    }
    OVERLAPPED connection{};
    connection.hEvent = event;
    if (!ConnectNamedPipe(parent.value, &connection)) {
        const DWORD error = GetLastError();
        if (error != ERROR_PIPE_CONNECTED) {
            if (error == ERROR_IO_PENDING) {
                // Never retire stack OVERLAPPED storage before cancellation completes.
                DWORD ignored = 0;
                CancelIoEx(parent.value, &connection);
                GetOverlappedResult(parent.value, &connection, &ignored, TRUE);
            }
            err = "worker stdout connection failed (" + std::to_string(error) + ")";
            return false;
        }
    }
    return true;
}

std::wstring workerEnvironment(const wchar_t* lane) {
    auto upper = [](std::wstring value) {
        for (auto& c : value) c = static_cast<wchar_t>(std::towupper(c));
        return value;
    };
    std::map<std::wstring, std::wstring> entries;
    wchar_t* parent = GetEnvironmentStringsW();
    if (!parent) return {};
    for (const wchar_t* p = parent; *p;) {
        const std::wstring entry(p);
        p += entry.size() + 1;
        const auto eq = entry.find(L'=', 1);  // preserve "=C:=..." drive entries
        if (eq != std::wstring::npos) entries[upper(entry.substr(0, eq))] = entry;
    }
    FreeEnvironmentStringsW(parent);
    entries[L"CHUDWARE_HOSTD_WORKER_LANE"] = std::wstring(L"CHUDWARE_HOSTD_WORKER_LANE=") + lane;
    // hostd workers always reach hostd, never an independent in-process engine.
    entries[L"CHUDWARE_ENGINE_DAEMON"] = L"CHUDWARE_ENGINE_DAEMON=1";
    std::wstring block;
    for (const auto& entry : entries) { block += entry.second; block.push_back(L'\0'); }
    block.push_back(L'\0');
    return block;
}

std::string windowsPython() {
    // Same managed and portable interpreter layouts as bootstrap::runtimePython.
    const auto executableDir = std::filesystem::u8path(
        chudware::platform::executablePath()).parent_path();
    const auto appdata = envOr("APPDATA", "");
    for (const auto& root : {std::filesystem::u8path(appdata) / "Chudware", executableDir}) {
        if (root.empty() || (appdata.empty() && root == std::filesystem::path("Chudware"))) continue;
        const auto candidate = root / "runtime/venv/Scripts/python.exe";
        std::error_code ec;
        if (std::filesystem::is_regular_file(candidate, ec)) return nativeUtf8(candidate.native());
    }
    // Developer-only PATH discovery; packaged workers never use an interpreter.
    return "python.exe";
}
#endif

#ifndef _WIN32
// Linux PR_SET_PDEATHSIG (guardParentDeath) fires when the THREAD that forked the child
// exits, not the process. hostd serves every connection on its own thread, so a worker
// forked while serving a short-lived connection was SIGKILLed the moment that client
// disconnected (2026-10-12: every agent worker died right after its turn's client
// closed, and the next turn paid a respawn and lost its live Chudcode sessions). All
// worker forks therefore run on one thread that lives as long as hostd (the same handoff
// ChildSupervisor::reaperLoop uses). The child body runs in the forked copy of this
// process, where the caller's stack is still mapped.
pid_t forkOnStableThread(const std::function<void()>& childBody, int& forkErrno) {
    struct Request {
        const std::function<void()>* body;
        pid_t pid = -1;
        int err = 0;
        bool done = false;
    };
    struct Spawner {
        std::mutex mutex;
        std::condition_variable changed;
        std::vector<Request*> queue;
    };
    // Leaked on purpose: the thread is never joined and must outlive static destruction.
    static Spawner* spawner = [] {
        auto* s = new Spawner();
        std::thread([s] {
            std::unique_lock<std::mutex> lock(s->mutex);
            for (;;) {
                s->changed.wait(lock, [s] { return !s->queue.empty(); });
                Request* request = s->queue.front();
                s->queue.erase(s->queue.begin());
                const pid_t pid = ::fork();
                if (pid == 0) {
                    (*request->body)();
                    _exit(127);
                }
                request->pid = pid;
                request->err = pid < 0 ? errno : 0;
                request->done = true;
                s->changed.notify_all();
            }
        }).detach();
        return s;
    }();
    Request request{&childBody};
    std::unique_lock<std::mutex> lock(spawner->mutex);
    spawner->queue.push_back(&request);
    spawner->changed.notify_all();
    spawner->changed.wait(lock, [&] { return request.done; });
    forkErrno = request.err;
    return request.pid;
}
#endif
}  // namespace
bool WorkerBridge::spawn(std::string& err) {
    // Caller holds mtx_. running_ must be false / fds closed.
    if (stopping_.load(std::memory_order_acquire)) {
        err = "worker bridge stopping";
        return false;
    }
    if (!allowWorkerSpawn(err)) return false;
    if (!pathsResolved_) {
        if (!identity_.empty()) {
            // Cell workers never honor developer interpreter/binary overrides.
            workerBin_ = adjacentWorkerBinary(err);
            if (workerBin_.empty()) return false;
            compiledWorker_ = true;
            repo_ = nativeUtf8(std::filesystem::u8path(workerBin_).parent_path().native());
        } else {
#ifdef CHUDWARE_DEVELOPER_BUILD
#ifndef _WIN32
        const std::string home = homeDir();
#endif
        if (const std::string env = envOr("CHUDWARE_WORKER_BIN", ""); !env.empty()) {
            std::error_code ec;
            workerBin_ = nativeUtf8(std::filesystem::weakly_canonical(std::filesystem::u8path(env), ec).native());
            if (ec) workerBin_ = env;
            compiledWorker_ = true;
        } else {
            workerBin_ = envOr("CHUDWARE_PYTHON", std::string());
            if (workerBin_.empty()) {
                #ifdef _WIN32
                workerBin_ = windowsPython();
                #else
                const std::string venv =
                    home.empty() ? std::string()
                                 : home + "/.local/share/Chudware/runtime/venv/bin/python";
                if (!venv.empty() && fileExists(venv)) {
                    workerBin_ = venv;
                } else {
                    for (const char* cand :
                         {"/usr/bin/python3", "/usr/local/bin/python3", "/usr/bin/python"}) {
                        if (fileExists(cand)) { workerBin_ = cand; break; }
                    }
                    if (workerBin_.empty()) workerBin_ = "python3";
                }
                #endif
            }
        }
        repo_ = compiledWorker_
                    ? nativeUtf8(std::filesystem::u8path(workerBin_).parent_path().native())
                    : developerRepoRoot();
#else
        workerBin_ = adjacentWorkerBinary(err);
        if (workerBin_.empty()) return false;
        if (const std::string env = envOr("CHUDWARE_WORKER_BIN", ""); !env.empty() &&
            !sameCanonicalPath(env, workerBin_)) {
            err = "CHUDWARE_WORKER_BIN must name the adjacent packaged worker";
            log().line("worker_bridge: spawn failed: %s", err.c_str());
            return false;
        }
        compiledWorker_ = true;
        repo_ = nativeUtf8(std::filesystem::u8path(workerBin_).parent_path().native());
#endif
        }
        pathsResolved_ = true;
    }

    if (workerBin_.empty()) {
        err = "CHUDWARE_WORKER_BIN is required in packaged builds";
        log().line("worker_bridge: spawn failed: %s", err.c_str());
        return false;
    }

    // Packaged binaries must resolve to a real manifest-owned file. Developer-mode bare
    // interpreter names may still defer to execlp's PATH search.
    if ((compiledWorker_ || workerBin_.find_first_of("/\\") != std::string::npos) &&
        !fileExists(workerBin_)) {
        err = (compiledWorker_ ? "Compiled worker not found at " : "Python runtime not found at ") +
              workerBin_;
        log().line("worker_bridge: spawn failed: %s", err.c_str());
        return false;
    }

    #ifdef _WIN32
    if (!identity_.empty()) { err = "cell workers are unsupported on Windows"; return false; }
    SECURITY_ATTRIBUTES security{sizeof(security), nullptr, TRUE};
    WinHandle outRead, outWrite, inRead, inWrite, diagnostic, job, process, thread, readEvent;
    if (!stdoutEvent_) {
        readEvent.value = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (!readEvent.value) { err = nativeError("worker stdout event creation"); return false; }
    }
    HANDLE event = stdoutEvent_ ? static_cast<HANDLE>(stdoutEvent_) : readEvent.value;
    if (!overlappedWorkerOutput(outRead, outWrite, event, err)) return false;
    if (!CreatePipe(&inRead.value, &inWrite.value, &security, 0) ||
        !SetHandleInformation(inWrite.value, HANDLE_FLAG_INHERIT, 0)) {
        err = nativeError("worker stdin setup"); return false;
    }
    // Do not change the inheritable flag of hostd's own console/log handle.
    const HANDLE stderrHandle = GetStdHandle(STD_ERROR_HANDLE);
    if (!stderrHandle || stderrHandle == INVALID_HANDLE_VALUE ||
        !DuplicateHandle(GetCurrentProcess(), stderrHandle, GetCurrentProcess(),
                         &diagnostic.value, 0, TRUE, DUPLICATE_SAME_ACCESS)) {
        diagnostic.value = CreateFileW(L"NUL", GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                      &security, OPEN_EXISTING, 0, nullptr);
    }
    if (!diagnostic.value || diagnostic.value == INVALID_HANDLE_VALUE) {
        err = nativeError("worker stderr setup"); return false;
    }
    job.value = CreateJobObjectW(nullptr, nullptr);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!job.value || !SetInformationJobObject(job.value, JobObjectExtendedLimitInformation,
                                              &limits, sizeof(limits))) {
        err = nativeError("worker job setup"); return false;
    }
    STARTUPINFOEXW startup{};
    startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = inRead.value;
    startup.StartupInfo.hStdOutput = outWrite.value;
    startup.StartupInfo.hStdError = diagnostic.value;
    SIZE_T attributeSize = 0;
    InitializeProcThreadAttributeList(nullptr, 1, 0, &attributeSize);
    std::vector<unsigned char> attributes(attributeSize);
    startup.lpAttributeList = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(attributes.data());
    if (!InitializeProcThreadAttributeList(startup.lpAttributeList, 1, 0, &attributeSize)) {
        err = nativeError("worker handle allowlist setup"); return false;
    }
    HANDLE inherited[] = {inRead.value, outWrite.value, diagnostic.value};
    const bool handlesReady = UpdateProcThreadAttribute(startup.lpAttributeList, 0,
        PROC_THREAD_ATTRIBUTE_HANDLE_LIST, inherited, sizeof(inherited), nullptr, nullptr) != FALSE;
    const auto launcher = std::filesystem::u8path(workerBin_).wstring();
    // A Windows filename cannot contain a quote; launcher is the only variable argv.
    std::wstring command = L"\"" + launcher + L"\"" +
        (compiledWorker_ ? L" --module worker" : L" -m services.worker_main");
    const auto directory = std::filesystem::u8path(repo_).wstring();
    auto environment = workerEnvironment(turnWorker_ ? L"agent-turn" : agentLane_ ? L"agent" : L"general");
    PROCESS_INFORMATION created{};
    // CREATE_NO_WINDOW still allocates a conhost child. Explicit private stdio
    // needs no console; detaching avoids that idle resource and unknown census entry.
    const DWORD flags = DETACHED_PROCESS | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT |
                        EXTENDED_STARTUPINFO_PRESENT;
    const bool launched = handlesReady && !environment.empty() &&
        CreateProcessW(nullptr, command.data(), nullptr, nullptr, TRUE, flags, environment.data(),
                       directory.empty() ? nullptr : directory.c_str(), &startup.StartupInfo, &created);
    const DWORD launchError = GetLastError();
    DeleteProcThreadAttributeList(startup.lpAttributeList);
    if (!launched) {
        err = "worker launch failed (" + std::to_string(launchError) + ")"; return false;
    }
    process.value = created.hProcess;
    thread.value = created.hThread;
    if (!AssignProcessToJobObject(job.value, process.value) ||
        ResumeThread(thread.value) == static_cast<DWORD>(-1)) {
        err = nativeError("worker containment/activation");
        TerminateProcess(process.value, 1);  // Never run an uncontained child.
        WaitForSingleObject(process.value, 2000);
        return false;
    }
    {
        std::lock_guard<std::mutex> lock(nativeMtx_);
        process_ = process.release();
        childJob_ = job.release();
        livePid_.store(static_cast<long>(created.dwProcessId), std::memory_order_release);
    }
    childStdoutRd_ = outRead.release();
    childStdinWr_ = inWrite.release();
    if (readEvent.value) stdoutEvent_ = readEvent.release();
    const long pid = static_cast<long>(created.dwProcessId);
    #else
    int outPipe[2] = {-1, -1}, inPipe[2] = {-1, -1};
    if (pipe(outPipe) != 0 || pipe(inPipe) != 0) {
        err = std::string("pipe() failed: ") + std::strerror(errno);
        for (int fd : outPipe) if (fd >= 0) ::close(fd);
        for (int fd : inPipe) if (fd >= 0) ::close(fd);
        return false;
    }

    constexpr const char* actorLauncher = "/opt/chudware/bin/chudware-actor-sandbox";
    std::vector<std::string> cellEnv;
    std::vector<char*> cellEnvp;
    std::vector<char*> cellArgv;
    std::string actorHex;
    if (!identity_.empty()) {
        if (!fileExists(actorLauncher)) {
            for (int fd : outPipe) ::close(fd);
            for (int fd : inPipe) ::close(fd);
            err = "actor sandbox launcher unavailable";
            return false;
        }
        actorHex = identity_["actor_hex"].get<std::string>();
        cellEnv = {
            "PATH=/opt/chudware/bin:/usr/bin:/bin", "LANG=C.UTF-8", "LC_ALL=C.UTF-8",
            "HOME=/actor/home", "XDG_DATA_HOME=/actor/data", "XDG_CONFIG_HOME=/actor/config",
            "XDG_RUNTIME_DIR=/actor/run", "CHUDWARE_CHUDCODE_BIN=/opt/chudware/bin/chud",
            "XDG_CACHE_HOME=/actor/cache", "TMPDIR=/tmp", "CHUDWARE_CELL=1",
            "CHUDWARE_HOSTD_SOCKET=/actor/run/hostd.sock",
            "CHUDWARE_NATIVE_ROOT=/work", "CHUDWARE_FS_ROOT=/work",
            "CHUDWARE_PROJECT_DIR=/work", "CHUDWARE_PROJECT_SAVE_DIR=/work",
            "CHUDWARE_WORKSPACE_KEY=" + identity_["workspace_key"].get<std::string>(),
            "CHUDWARE_ACTOR_KEY=" + identity_["actor_key"].get<std::string>(),
            "CHUDWARE_ACTOR_HEX=" + actorHex,
            std::string("CHUDWARE_HOSTD_WORKER_LANE=") +
                (turnWorker_ ? "agent-turn" : agentLane_ ? "agent" : "general")};
        for (auto& value : cellEnv) cellEnvp.push_back(value.data());
        cellEnvp.push_back(nullptr);
        cellArgv = {const_cast<char*>(actorLauncher), actorHex.data(),
                    const_cast<char*>("rw"), const_cast<char*>("--"), workerBin_.data(),
                    const_cast<char*>("--module"), const_cast<char*>("worker"), nullptr};
    }
    const pid_t parentPid = ::getpid();
    const std::function<void()> child = [&] {
        // Child: async-signal-safe orphan-proofing first (mirrors child_supervisor.cpp:310-315
        // in spirit).
        ::setpgid(0, 0);
        // Parent-death linkage: PDEATHSIG on Linux, a kqueue babysitter on
        // Darwin. Never returns if hostd already died.
        chudware::platform::guardParentDeath(parentPid);
        // stdin <- inPipe[0], stdout -> outPipe[1]; stderr stays (hostd log/console).
        dup2(inPipe[0], STDIN_FILENO);
        dup2(outPipe[1], STDOUT_FILENO);
        ::close(inPipe[0]);
        ::close(inPipe[1]);
        ::close(outPipe[0]);
        ::close(outPipe[1]);
        // Close every other inherited fd (hostd's listen/client sockets etc.) so they never
        // leak into the worker child.
        chudware::platform::closeInheritedFds(3);
        if (!identity_.empty()) {
            // Everything was allocated before fork. No daemon credentials or loader
            // overrides survive. Only the trusted launcher may add egress settings.
#ifdef __linux__
            ::clearenv();
#endif
            ::execve(actorLauncher, cellArgv.data(), cellEnvp.data());
            _exit(127);  // Missing/failed sandbox is never an unsandboxed fallback.
        }
        if (!repo_.empty() && chdir(repo_.c_str()) != 0) {
            // Fall through; imports or resource lookup will fail clearly.
        }
        // This role belongs to the spawned child, not the hostd environment:
        // desktop/standalone workers continue to start with their full lifecycle.
        if (::setenv("CHUDWARE_HOSTD_WORKER_LANE",
                     turnWorker_ ? "agent-turn" : agentLane_ ? "agent" : "general", 1) != 0)
            _exit(127);
        if (compiledWorker_)
            execlp(workerBin_.c_str(), workerBin_.c_str(), "--module", "worker", (char*)nullptr);
        else
            execlp(workerBin_.c_str(), workerBin_.c_str(), "-m", "services.worker_main",
                   (char*)nullptr);
        _exit(127);
    };
    int forkErrno = 0;
    const pid_t pid = forkOnStableThread(child, forkErrno);
    if (pid < 0) {
        err = std::string("fork failed: ") + std::strerror(forkErrno);
        ::close(outPipe[0]);
        ::close(outPipe[1]);
        ::close(inPipe[0]);
        ::close(inPipe[1]);
        return false;
    }

    // Parent mirrors the child's setpgid (whoever runs first wins; both same), then keeps our
    // ends and closes the child's.
    ::setpgid(pid, pid);
    ::close(inPipe[0]);
    ::close(outPipe[1]);
    fcntl(outPipe[0], F_SETFL, O_NONBLOCK);
    process_ = pid;
    livePid_.store(pid, std::memory_order_release);
    childStdoutRd_ = outPipe[0];
    childStdinWr_ = inPipe[1];
    #endif
    readBuf_.clear();
    pendingActivityId_ = -1;
    discardingLine_ = false;
    running_ = true;
    log().line("worker_bridge: spawned %s services.worker_main pid=%d launcher=%s repo=%s",
               agentLane_ ? "agent" : "general", static_cast<int>(pid),
               workerBin_.c_str(), repo_.c_str());
    return true;
}

bool WorkerBridge::writeLine(const std::string& line) {
    #ifdef _WIN32
    if (!childStdinWr_) return false;
    std::size_t sent = 0;
    while (sent < line.size()) {
        DWORD count = 0;
        const DWORD size = static_cast<DWORD>(std::min<std::size_t>(line.size() - sent, 65536));
        if (!WriteFile(childStdinWr_, line.data() + sent, size, &count, nullptr) || !count) return false;
        sent += count;
    }
    return true;
    #else
    if (childStdinWr_ < 0) return false;
    const char* p = line.data();
    size_t left = line.size();
    while (left > 0) {
        ssize_t n = ::write(childStdinWr_, p, left);
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (n == 0) return false;
        p += n;
        left -= (size_t)n;
    }
    #endif
    return true;
}

bool WorkerBridge::readLine(std::string& line, int timeoutMs, bool* malformed, std::size_t maxBytes) {
    if (malformed) *malformed = false;
    auto start = std::chrono::steady_clock::now();
    size_t scanFrom = 0;
    #ifdef _WIN32
    bool workerExited = false;
    #endif
    for (;;) {
        size_t nl = readBuf_.find('\n', scanFrom);
        if (discardingLine_) {
            if (nl == std::string::npos) readBuf_.clear();
            else {
                readBuf_.erase(0, nl + 1);
                discardingLine_ = false;
                line.clear();
                if (malformed) *malformed = true;
                return true;  // malformed frame consumed, next reply stays framed
            }
        } else if ((nl != std::string::npos && nl > maxBytes) ||
                   (nl == std::string::npos && readBuf_.size() > maxBytes)) {
            if (nl != std::string::npos) {
                readBuf_.erase(0, nl + 1);
                line.clear();
                if (malformed) *malformed = true;
                return true;
            }
            readBuf_.clear();
            discardingLine_ = true;
        }
        scanFrom = readBuf_.size();
        if (nl != std::string::npos) {
            line = readBuf_.substr(0, nl);
            readBuf_.erase(0, nl + 1);
            return true;
        }
        auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                           std::chrono::steady_clock::now() - start)
                           .count();
        if (timeoutMs >= 0 && (int)elapsed > timeoutMs) return false;

        #ifdef _WIN32
        if (!childStdoutRd_ || !stdoutEvent_ || !process_) return false;
        if (!ResetEvent(stdoutEvent_)) return false;
        char chunk[4096];
        DWORD received = 0;
        OVERLAPPED operation{};
        operation.hEvent = stdoutEvent_;
        BOOL completed = ReadFile(childStdoutRd_, chunk, sizeof(chunk), &received, &operation);
        if (!completed && GetLastError() == ERROR_IO_PENDING) {
            const auto left = std::chrono::milliseconds(timeoutMs < 0 ? 0 : timeoutMs) -
                (std::chrono::steady_clock::now() - start);
            const auto milliseconds = std::chrono::ceil<std::chrono::milliseconds>(left).count();
            const DWORD waitMs = timeoutMs < 0 ? INFINITE :
                static_cast<DWORD>(std::clamp<std::int64_t>(milliseconds, 0, MAXDWORD - 1));
            HANDLE waits[] = {stdoutEvent_, process_};
            const DWORD wait = workerExited ? WaitForSingleObject(stdoutEvent_, waitMs)
                                           : WaitForMultipleObjects(2, waits, FALSE, waitMs);
            if (wait == WAIT_OBJECT_0) {
                completed = GetOverlappedResult(childStdoutRd_, &operation, &received, FALSE);
            } else {
                // Cancellation is only a request. Drain completion before the
                // stack buffer/OVERLAPPED or the lane's reusable event can retire.
                CancelIoEx(childStdoutRd_, &operation);
                completed = GetOverlappedResult(childStdoutRd_, &operation, &received, TRUE);
                if (completed && received) readBuf_.append(chunk, received);
                if (wait != WAIT_OBJECT_0 + 1) return false;
                // Root exit owns the whole tree's end. Closing every inherited
                // writer guarantees pending/future reads reach EOF after the
                // final buffered bytes; do not repeatedly wait on a signaled pid.
                terminateInFlightChild();
                workerExited = true;
                continue;
            }
        }
        if (!completed || !received) {
            CloseHandle(childStdoutRd_); childStdoutRd_ = nullptr; return false;
        }
        readBuf_.append(chunk, received);
        #else
        struct pollfd pfd;
        pfd.fd = childStdoutRd_;
        pfd.events = POLLIN;
        pfd.revents = 0;
        int pr = ::poll(&pfd, 1, 2);  // 2 ms tick — matches worker_client.cpp
        if (pr < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (pr == 0) {
            int status = 0;
            pid_t r = (process_ >= 0) ? waitpid(process_, &status, WNOHANG) : -1;
            if (r == process_ || (r < 0 && errno == ECHILD)) {
                process_ = -1;
                livePid_.store(-1, std::memory_order_release);
                char chunk[4096];
                ssize_t got = ::read(childStdoutRd_, chunk, sizeof(chunk));
                if (got > 0) {
                    readBuf_.append(chunk, (size_t)got);
                    continue;
                }
                return false;  // EOF / dead child, buffer drained
            }
            continue;
        }
        char chunk[4096];
        ssize_t got = ::read(childStdoutRd_, chunk, sizeof(chunk));
        if (got > 0) {
            readBuf_.append(chunk, (size_t)got);
        } else if (got == 0) {
            ::close(childStdoutRd_);
            childStdoutRd_ = -1;
            return false;  // EOF
        } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
            ::close(childStdoutRd_);
            childStdoutRd_ = -1;
            return false;
        }
        #endif
    }
}

bool WorkerBridge::fetchControlInfo(std::string& err, const json& ctx) {
    {
        std::lock_guard<std::mutex> lk(controlMtx_);
        if (!control_.token.empty()) return true;
    }

    json info;
    std::string infoErr;
    if (!callOnce("worker_control_info", json::object(), info, infoErr, 5000, {}, ctx)) {
        err = infoErr.empty() ? "worker control information unavailable" : infoErr;
        return false;
    }

    const auto wipeInfoToken = [&info] {
        if (!info.is_object()) return;
        const auto token = info.find("token");
        if (token != info.end() && token->is_string())
            wipeString(token->get_ref<std::string&>());
    };
    ControlInfo next;
    try {
        if (!info.is_object()) throw std::runtime_error("invalid");
        next.transport = info.at("transport").get<std::string>();
        next.endpoint = info.at("endpoint").get<std::string>();
        next.token = info.at("token").get<std::string>();
    } catch (...) {
        wipeInfoToken();
        wipeString(next.token);
        err = "worker control information is invalid";
        return false;
    }
    wipeInfoToken();
    if (!identity_.empty()) {
        // The worker sees only /actor; hostd sees that exact actor's cell-private state.
        // Refuse traversal and symlink escapes rather than accepting arbitrary endpoints.
        const std::filesystem::path endpoint(next.endpoint);
        bool safe = next.endpoint.compare(0, 7, "/actor/") == 0;
        for (const auto& part : endpoint)
            if (part == "." || part == "..") safe = false;
        const std::filesystem::path root =
            "/state/actors/" + identity_["actor_hex"].get<std::string>();
        if (safe) {
            const auto mapped = root / next.endpoint.substr(7);
            std::error_code ec;
            const auto resolved = std::filesystem::canonical(mapped, ec);
            const auto relative = resolved.lexically_relative(root);
            if (ec || relative.empty() || relative.is_absolute() ||
                *relative.begin() == "..") safe = false;
            else next.endpoint = resolved.string();
        }
        if (!safe) {
            wipeString(next.token);
            err = "worker control endpoint outside owning actor";
            return false;
        }
    }
    if (!validControlInfo(next.transport, next.endpoint, next.token)) {
        wipeString(next.token);
        wipeString(next.endpoint);
        wipeString(next.transport);
        err = "worker control information is invalid";
        return false;
    }
    {
        std::lock_guard<std::mutex> lk(controlMtx_);
        wipeString(control_.token);
        control_ = std::move(next);
    }
    return true;
}

bool WorkerBridge::activityLocked(std::uint64_t& active, int timeoutMs) {
    active = 0;
    if (!running_) return true;
    if (stopping_.load(std::memory_order_acquire) || !transportReady()) return false;
    const bool pending = pendingActivityId_ >= 0;
    if (!pending) {
        pendingActivityId_ = nextId_++;
        json request = {{"id", pendingActivityId_}, {"method", "worker_activity"},
                        {"params", json::object()}};
        if (!identity_.empty()) request["ctx"] = identity_;
        if (!writeLine(request.dump() + "\n")) return false;
    }
    const auto deadline = std::chrono::steady_clock::now() +
        std::chrono::milliseconds(pending ? 0 : std::max(0, timeoutMs));
    for (;;) {
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now()).count();
        std::string line;
        bool malformed = false;
        if (!readLine(line, static_cast<int>(std::max<std::int64_t>(0, remaining)),
                      &malformed, 64u * 1024u) || malformed) return false;
        const auto response = json::parse(line, nullptr, false);
        if (!response.is_object() || !response.contains("id") ||
            !response["id"].is_number_integer()) return false;
        if (response["id"] != pendingActivityId_) return false;
        if (response.contains("event") && !response.contains("ok")) {
            if (std::chrono::steady_clock::now() >= deadline) return false;
            continue;
        }
        pendingActivityId_ = -1;
        if (!response.contains("ok") || response["ok"] != true ||
            !response.contains("result") || !response["result"].is_object()) return false;
        const auto& value = response["result"];
        const auto count = [](const json& v, std::uint64_t& out) {
            if (!v.is_number_integer() || (!v.is_number_unsigned() && v.get<std::int64_t>() < 0))
                return false;
            const auto parsed = v.get<std::uint64_t>();
            if (parsed > static_cast<std::uint64_t>(INT64_MAX)) return false;
            out = parsed;
            return true;
        };
        if (!value.contains("active") || !count(value["active"], active) ||
            !value.contains("by_kind") || !value["by_kind"].is_object()) return false;
        std::uint64_t kinds = 0;
        for (const auto& [kind, number] : value["by_kind"].items()) {
            std::uint64_t ignored = 0;
            if (kind.empty() || kind.size() > 64 ||
                !std::all_of(kind.begin(), kind.end(), [](unsigned char c) {
                    return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_';
                }) || !count(number, ignored)) return false;
            if (ignored > static_cast<std::uint64_t>(INT64_MAX) - kinds) return false;
            kinds += ignored;
        }
        if (kinds != active) { active = std::max(active, kinds); return false; }
        return value.contains("ok") && value["ok"] == true &&
               value.contains("complete") && value["complete"] == true;
    }
}

bool WorkerBridge::callOnce(const std::string& method, const json& params, json& result,
                            std::string& err, int timeoutMs, const EventFn& onEvent,
                            const json& ctx) {
    if (stopping_.load(std::memory_order_acquire)) {
        err = "worker bridge stopping";
        return false;
    }
    if (!running_) {
        if (!spawn(err)) return false;
    }

    const int id = nextId_++;
    json req = {{"id", id}, {"method", method}, {"params", params}};
    if (!identity_.empty()) req["ctx"] = ctx;
    const std::string line = req.dump() + "\n";
    if (!writeLine(line)) {
        err = "write failed";
        return false;
    }

    for (;;) {
        std::string resp;
        bool malformed = false;
        if (!readLine(resp, timeoutMs, &malformed)) {
            // Distinguish child death (EOF / reaped) from a live slow call so
            // the outer retry only respawns on a dead child.
            err = (!transportReady() || !childAlive()) ? "closed waiting for response"
                                                      : "timeout waiting for response";
            return false;
        }
        if (malformed) {
            if (pendingActivityId_ >= 0) { pendingActivityId_ = -1; continue; }
            err = "worker response frame exceeds 64 MiB";
            return false;
        }
        if (resp.empty()) continue;
        json j;
        try {
            j = json::parse(resp);
        } catch (...) {
            continue;  // ignore non-JSON noise
        }
        if (!j.contains("id") || j["id"].is_null()) continue;
        // id may be number; tolerate int-or-int64
        int rid = -1;
        try {
            rid = j["id"].get<int>();
        } catch (...) {
            continue;
        }
        if (rid != id) {
            if (rid == pendingActivityId_ && j.contains("ok")) pendingActivityId_ = -1;
            continue;
        }

        // Matching streamed progress is delivered once, before the terminal response.
        if (j.contains("event") && j.contains("data") && !j.contains("ok")) {
            if (onEvent) {
                try {
                    onEvent(j["data"]);
                } catch (...) {
                    // Event delivery is transparency only; it must not poison the
                    // worker transport or turn an eventual success into failure.
                }
            }
            continue;
        }

        // A later terminal response proves the earlier serialized census ended,
        // even if that census's response was malformed. Never consume this reply
        // for the timed-out probe or replace a live lane because of its deadline.
        pendingActivityId_ = -1;
        if (j.value("ok", false)) {
            result = j.value("result", json::object());
            return true;
        }
        // ok:false — structured envelope preferred (protocol-2).
        {
            const auto it = j.find("error");
            if (it != j.end() && it->is_object()) {
                result = *it;
                err = it->value("message", std::string("rpc error"));
                return false;
            }
            err = (it != j.end() && it->is_string()) ? it->get<std::string>()
                                                     : std::string("unknown rpc error");
            result = json::object();
            return false;
        }
    }
}

bool WorkerBridge::call(const std::string& method, const json& params, json& result,
                        std::string& err, int timeoutMs, const EventFn& onEvent,
                        const json& ctx) {
#ifdef _WIN32
    if (!identity_.empty() || (ctx.is_object() && ctx.value("cell", false))) {
        err = "cell workers are unsupported on Windows";
        result = json{{"code", "cell_refused"}, {"message", err}};
        return false;
    }
#endif
    // Only this lane's stdio mutex is held; an agent turn cannot serialize
    // clients using the independent general-worker channel. WITHIN a lane the wait for
    // the stdio channel is bounded: an agent_admin_turn holds its worker for hours,
    // and a plain lock parked every other agent-lane call (subscription_accounts from the
    // Copilot usage rings) behind it past its 120 s bound until the long-tier watchdog
    // _Exit'ed hostd with the live session's docs (2026-09-27). A non-turn call behind a
    // running turn fails at once — the GUI's single async RPC lane must not sit out its
    // whole timeout per poll — and any other wait ends at the call's own timeout.
    // timeoutMs < 0 (an agent_admin_turn without a caller budget) has no deadline: the
    // turn ends on its reply, Stop over the control lane, child death or stop().
    const bool longAgentTurn = method == "agent_admin_turn";
    if (timeoutMs == 0)
        timeoutMs = !identity_.empty() && longAgentTurn ? -1 : 120000;
    const bool unbounded = timeoutMs < 0;
    const auto laneDeadline =
        std::chrono::steady_clock::now() + std::chrono::milliseconds(unbounded ? 0 : timeoutMs);
    std::unique_lock<std::timed_mutex> lk(mtx_, std::defer_lock);
    while (!lk.try_lock_for(std::chrono::milliseconds(50))) {
        bool turnActive = false;
        {
            std::lock_guard<std::mutex> controlLock(controlMtx_);
            turnActive = !activeWorkerRunId_.empty();
        }
        if (stopping_.load(std::memory_order_acquire)) {
            err = "worker bridge stopping";
            return false;
        }
        if ((turnActive && !longAgentTurn) ||
            (!unbounded && std::chrono::steady_clock::now() >= laneDeadline)) {
            err = turnActive ? "worker lane busy (agent turn in progress)"
                             : "worker lane busy";
            result = json{{"code", "worker_lane_busy"}, {"message", err}};
            return false;
        }
    }
    result = json::object();
    err.clear();
    if (stopping_.load(std::memory_order_acquire)) {
        err = "worker bridge stopping";
        return false;
    }

    std::string runId;
    if (longAgentTurn && params.is_object()) {
        const auto runIt = params.find("run_id");
        if (runIt != params.end() && runIt->is_string()) runId = runIt->get<std::string>();
    }
    if (longAgentTurn && runId.empty()) {
        err = "agent_admin_turn requires a non-empty run_id";
        return false;
    }

    // Child died (write fail/EOF) → reap + respawn once, then fail.
    // Timeouts and method-level errors do NOT respawn, and neither does an agent turn
    // the worker had already received: a turn is not idempotent. Replaying it on the
    // fresh worker started the same prompt as a SECOND run in a new Chudcode session
    // with no GUI attached (2026-09-27, c221: the app went away mid-turn, the replay
    // ran 23 min unwatched, edited the user's firmware repo and pushed it; the GUI
    // later listed it as an "external" conversation, c223). The conversation resumes
    // from its persisted session when the user sends "continue".
    for (int attempt = 0; attempt < 2; ++attempt) {
        std::string attemptErr;
        result = json::object();

        // The authenticated side channel must be known before an online turn
        // starts. Otherwise stdio would be occupied by an uncancellable call.
        if (longAgentTurn && !fetchControlInfo(attemptErr, ctx)) {
            const bool childDead =
                attemptErr == "write failed" || attemptErr == "closed waiting for response";
            if (!childDead) {
                err = std::move(attemptErr);
                return false;
            }
            log().line("worker_bridge: child lost before %s; %s", method.c_str(),
                       attempt == 0 ? "respawning once" : "giving up");
            reap();
            err = std::move(attemptErr);
            if (attempt == 1 || stopping_.load(std::memory_order_acquire)) return false;
            continue;
        }

        if (longAgentTurn) {
            std::lock_guard<std::mutex> controlLock(controlMtx_);
            activeWorkerRunId_ = runId;
            activeCtx_ = ctx;
        }
        const bool ok =
            callOnce(method, params, result, attemptErr, timeoutMs, onEvent, ctx);
        clearActiveRun();
        const bool childDead =
            attemptErr == "write failed" || attemptErr == "closed waiting for response";
        const bool methodError = result.is_object() && result.contains("code");
        if (ok) return true;

        if (methodError || !childDead) {
            err = std::move(attemptErr);
            return false;
        }
        // A partial write may already have delivered the prompt. Never replay a turn.
        const bool replayable = !longAgentTurn;
        log().line("worker_bridge: child lost during %s (%s); %s", method.c_str(),
                   attemptErr.c_str(),
                   !replayable ? "turn not replayed"
                               : attempt == 0 ? "respawning once" : "giving up");
        reap();
        if (!replayable) {
            err = "worker lost during agent turn";
            result = json{{"code", "worker_lost_mid_turn"},
                          {"message",
                           "The agent worker stopped mid-turn; the turn was not restarted. "
                           "Send \"continue\" to resume the conversation."}};
            return false;
        }
        err = std::move(attemptErr);
        if (attempt == 1 || stopping_.load(std::memory_order_acquire)) return false;
        // loop: respawn inside fetchControlInfo/callOnce
    }
    return false;
}

}  // namespace hostd

