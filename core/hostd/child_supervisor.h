// Supervised agent children for chudware-hostd (Phase 5 of the shared-engine
// plan). The daemon becomes the parent of every agent worker process so:
//   * GUI/MCP client death does NOT kill a running mission (the child belongs
//     to the daemon, not to the spawning client);
//   * daemon death kills ALL children deterministically — each child binds its
//     lifetime to the daemon's via chudware::platform::guardParentDeath (plus
//     its own process group for the graceful path) so orphaned agent workers are
//     structurally impossible (the incident class this refactor exists to kill);
//   * the daemon refuses idle-exit and (un-forced) drain while children run.
//
// Daemon methods (dispatched via handleMethod):
//   agent_spawn{argv:[...], env:{...}, mission_id?, doc_id?}
//       -> {child_id, pid, pgid, log}
//     argv/env values and mission_id/doc_id must be NUL-free strings; invalid
//     metadata returns invalid_params before any child or child log is created.
//     fork(); child: setpgid(0,0) + guardParentDeath(daemon pid), which is
//     PR_SET_PDEATHSIG(SIGKILL) plus the getppid() exec-vs-parent-death race
//     guard on Linux and a kqueue/NOTE_EXIT babysitter on Darwin (see
//     util/parent_death.h) + stdio -> per-child log
//     file <logDir>/agent-<child_id>.log + execvpe. A failed exec is reported
//     synchronously as a clean error via a CLOEXEC pipe (no zombie, no table
//     entry).
//   agent_stop{child_id, force?} -> SIGTERM to -pgid, SIGKILL after a 5 s
//     grace (enforced by the reaper); force=true = immediate SIGKILL.
//     Idempotent: stopping an already-exited child_id is ok:true.
//   agent_list -> live children with status.
//
// A reaper thread waitpid()s each REGISTERED pid individually (never wait(-1):
// that would steal chudware_ipc::spawnDetached's self-reaped intermediate forks)
// and broadcasts {"event":"mission.exited","data":{child_id,pid,exit_code,
// signal,mission_id}} to clients on exit.
//
// SECURITY: local desktop agent_spawn executes arbitrary argv as the same OS
// account. Cell/tenant connections must refuse this method in Server policy.
#pragma once

#include <atomic>
#include <condition_variable>
#include <deque>
#include <exception>
#include <functional>
#include <future>
#include <map>
#include <mutex>
#include <string>
#include <thread>

#ifndef _WIN32
#include <sys/types.h>
#endif

#include <nlohmann/json.hpp>

namespace hostd {

class ChildSupervisor {
public:
    // Broadcasts one event line to every connected client (Server installs it;
    // Server also touch()es its activity clock there so a child exit restarts
    // the linger countdown).
    using Broadcast = std::function<void(const std::string& line)>;

    ~ChildSupervisor() { shutdown(); }

    void setBroadcast(Broadcast b) { broadcast_ = std::move(b); }
    // Where per-child logs land: <logDir>/agent-<child_id>.log.
    void setLogDir(const std::string& dir) { logDir_ = dir; }

    // Start the reaper thread (idempotent).
    void start();
    // Stop all children (TERM -> grace -> KILL), reap them, join the reaper.
    // Called on every daemon exit path (drain / idle / SIGTERM) BEFORE the
    // final autosave; PDEATHSIG covers the daemon-SIGKILL path.
    void shutdown();

    // agent_spawn / agent_stop / agent_list. Returns true if `method` was ours;
    // result/isError as in Server::dispatch.
    bool handleMethod(int clientId, const std::string& method,
                      const nlohmann::json& params, nlohmann::json& result,
                      bool& isError);

    int aliveCount();
    // For daemon_status: {"children": N, "missions": [ids...]}.
    void statusInto(nlohmann::json& status);

    // TERM -> grace -> KILL every live child and reap. Used by shutdown() and
    // by forced drain (daemon_drain{force:true}).
    void stopAll(int graceMs = 5000);

private:
    struct Child {
        std::string childId;
#ifdef _WIN32
        // Windows (child_supervisor_win.cpp): the child lives in its OWN Job
        // Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (breakaway NOT granted).
        // Both handles are owned by this record; daemon death closes them, the
        // job's last handle drop fires KILL_ON_JOB_CLOSE = the PDEATHSIG analog.
        void* process = nullptr;  // HANDLE
        void* job = nullptr;      // HANDLE
        unsigned long pid = 0;
#else
        pid_t pid = 0;
        pid_t pgid = 0;
#endif
        std::string missionId;
        std::string docId;
        std::string logPath;
        int spawnerClient = 0;
        long long spawnedMs = 0;
        long long killDeadlineMs = 0;  // >0: TERM sent, KILL at this deadline
    };

    // A spawn request handed off to the reaper thread. PR_SET_PDEATHSIG fires
    // when the forking THREAD exits (prctl(2)), so children must be forked
    // from a thread that lives as long as the daemon — the reaper — never
    // from a per-client connection thread (whose exit on client disconnect
    // would SIGKILL every child it spawned).
    struct SpawnReq {
        int clientId = 0;
        nlohmann::json params;
        nlohmann::json result;
        bool isError = false;
        std::promise<void> done;
    };

    void reaperLoop();
    // Runs on the reaper thread. Fills req.result/req.isError.
    void doSpawn(SpawnReq& req);
    // Validate before ANY child/process/artifact creation on either backend.
    static bool validateSpawn(const nlohmann::json& params, nlohmann::json& error) {
        const auto fail = [&](const char* message) {
            error = {{"code", "invalid_params"}, {"message", message}};
            return false;
        };
        const auto string = [](const nlohmann::json& value) {
            return value.is_string() &&
                value.get_ref<const std::string&>().find('\0') == std::string::npos;
        };
        if (!params.is_object()) return fail("agent_spawn params must be an object");
        if (!params.contains("argv") || !params["argv"].is_array() || params["argv"].empty())
            return fail("agent_spawn needs a non-empty argv array");
        for (const auto& value : params["argv"])
            if (!string(value)) return fail("argv entries must be strings without NUL bytes");
        if (params["argv"][0].get_ref<const std::string&>().empty())
            return fail("argv executable must not be empty");
        for (const char* key : {"mission_id", "doc_id"})
            if (params.contains(key) && !string(params[key]))
                return fail("mission_id and doc_id must be strings without NUL bytes");
        if (params.contains("env")) {
            if (!params["env"].is_object()) return fail("env must be an object");
            for (const auto& entry : params["env"].items())
                if (entry.key().empty() || entry.key().find_first_of("=\0", 0, 2) !=
                        std::string::npos || !string(entry.value()))
                    return fail("env requires non-empty names and string values without NUL bytes");
        }
        return true;
    }

    // Never let a request exception escape the daemon-lifetime supervisor thread.
    // If constructing an error itself throws, complete the future exceptionally
    // so the client dispatch boundary, not std::terminate, handles it.
    void completeSpawn(SpawnReq& req, bool draining = false) noexcept {
        try {
            try {
                if (draining) {
                    req.isError = true;
                    req.result = {{"code", "draining"}, {"message", "daemon is shutting down"}};
                } else {
                    doSpawn(req);
                }
            } catch (const nlohmann::json::exception&) {
                req.isError = true;
                req.result = {{"code", "invalid_params"}, {"message", "invalid agent_spawn parameters"}};
            } catch (...) {
                req.isError = true;
                req.result = {{"code", "spawn_failed"}, {"message", "child spawn failed"}};
            }
            req.done.set_value();
        } catch (...) {
            try { req.done.set_exception(std::current_exception()); } catch (...) {}
        }
    }
    // Reap one pid (WNOHANG); on exit broadcast mission.exited + erase. Caller
    // holds mtx_. Returns true if the child was reaped.
    bool reapLocked(std::map<std::string, Child>::iterator it, int waitFlags);
    void emitExited(const Child& c, int status);

    Broadcast broadcast_;
    std::string logDir_;
    std::mutex mtx_;
    std::condition_variable cv_;
    std::map<std::string, Child> children_;  // child_id -> record
    std::deque<SpawnReq*> pendingSpawns_;    // owned by the waiting caller
    int nextId_ = 1;
    std::thread reaper_;
    std::atomic<bool> stopping_{false};
    bool started_ = false;
};

}  // namespace hostd
