// Supervised agent children for aicad-hostd (Phase 5 of the shared-engine
// plan). The daemon becomes the parent of every agent worker process so:
//   * GUI/MCP client death does NOT kill a running mission (the child belongs
//     to the daemon, not to the spawning client);
//   * daemon death kills ALL children deterministically — each child sets
//     PR_SET_PDEATHSIG(SIGKILL) (plus its own process group for the graceful
//     path) so orphaned agent workers are structurally impossible (the
//     incident class this refactor exists to kill);
//   * the daemon refuses idle-exit and (un-forced) drain while children run.
//
// Daemon methods (dispatched via handleMethod):
//   agent_spawn{argv:[...], env:{...}, mission_id?, doc_id?}
//       -> {child_id, pid, pgid, log}
//     fork(); child: setpgid(0,0) + prctl(PDEATHSIG, SIGKILL) + re-check
//     getppid() (exec-vs-parent-death race guard) + stdio -> per-child log
//     file <logDir>/agent-<child_id>.log + execvpe. A failed exec is reported
//     synchronously as a clean error via a CLOEXEC pipe (no zombie, no table
//     entry).
//   agent_stop{child_id, force?} -> SIGTERM to -pgid, SIGKILL after a 5 s
//     grace (enforced by the reaper); force=true = immediate SIGKILL.
//     Idempotent: stopping an already-exited child_id is ok:true.
//   agent_list -> live children with status.
//
// A reaper thread waitpid()s each REGISTERED pid individually (never wait(-1):
// that would steal aicad_ipc::spawnDetached's self-reaped intermediate forks)
// and broadcasts {"event":"mission.exited","data":{child_id,pid,exit_code,
// signal,mission_id}} to clients on exit.
//
// SECURITY: agent_spawn executes arbitrary argv as the daemon's user. This is
// the SAME trust domain as every existing client of the daemon — the unix
// socket lives in a 0700 per-user runtime dir, so anyone who can reach
// agent_spawn can already run arbitrary engine ops (and arbitrary code) as
// that user. No additional authentication is warranted at this layer.
#pragma once

#include <atomic>
#include <condition_variable>
#include <deque>
#include <functional>
#include <future>
#include <map>
#include <mutex>
#include <string>
#include <thread>

#include <sys/types.h>

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
        pid_t pid = 0;
        pid_t pgid = 0;
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
