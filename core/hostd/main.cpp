// aicad-hostd — the shared engine-host daemon.
//
// Owns the ONE libaicad instance and serves it over local IPC to every client:
// the desktop GUI, each MCP server process, and (Phase 5) supervised agent
// workers. Whichever client starts first spawns the daemon (detached, via
// aicad_ipc::spawnDetached); the daemon self-exits after AICAD_HOSTD_LINGER_SECS
// of no clients + no work. See ~/.claude plan "shared engine host daemon" and
// docs/HOSTD.md (Phase 7).
//
//   aicad-hostd                 run (assumes already detached by the spawner)
//   aicad-hostd --foreground    same, explicit (dev convenience)
//   aicad-hostd --status        print daemon_status of the running instance
//   aicad-hostd --stop          ask the running instance to drain and exit
//                               (refused while agent children run; add --force
//                               to stop them first)
//   aicad-hostd --version       print build id + engine ABI
//
// Startup order matters: acquire instance lock -> bind socket -> THEN load the
// engine asynchronously. Racing clients connect immediately and see state:
// "loading" in the hello instead of a connect timeout.
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>

#include <limits.h>
#include <unistd.h>

#include "../../services/ipc/ipc_client.h"
#include "../../services/ipc/ipc_discovery.h"
#include "../../services/ipc/ipc_transport.h"
#include "../../services/worker/cpp_core.h"
#include "autosave.h"
#include "hostd_log.h"
#include "server.h"
#include "version_info.h"

static hostd::Server* g_server = nullptr;

static void onSignal(int) {
    if (g_server) g_server->stop();
}

static std::string selfPath() {
    char buf[PATH_MAX];
    ssize_t n = ::readlink("/proc/self/exe", buf, sizeof buf - 1);
    if (n <= 0) return "";
    buf[n] = 0;
    return buf;
}

// --status / --stop [--force]: attach-only client against the same instance key.
static int controlCommand(const char* method,
                          const nlohmann::json& params = nlohmann::json::object()) {
    aicad_ipc::EngineClient cli;
    aicad_ipc::EngineClient::Options opts;
    opts.hostdBin = selfPath();
    opts.clientKind = "control";
    opts.allowSpawn = false;
    std::string err;
    if (!cli.connectOrSpawn(opts, err)) {
        std::fprintf(stderr, "aicad-hostd: no running instance (%s)\n", err.c_str());
        return 1;
    }
    nlohmann::json result;
    if (!cli.call(method, params, result, err)) {
        std::fprintf(stderr, "aicad-hostd: %s failed: %s\n", method, err.c_str());
        return 1;
    }
    std::printf("%s\n", result.dump(2).c_str());
    return 0;
}

int main(int argc, char** argv) {
    std::string mode = argc > 1 ? argv[1] : "";
    if (mode == "--version") {
        std::printf("aicad-hostd %s\n", AICAD_HOSTD_BUILD_ID);
        return 0;
    }
    if (mode == "--status") return controlCommand("daemon_status");
    if (mode == "--stop") {
        // Bare --stop is refused while agent children run (mission_active);
        // --stop --force stops the children (TERM -> grace -> KILL) first.
        const bool force = argc > 2 && std::string(argv[2]) == "--force";
        return controlCommand("daemon_drain", {{"force", force}});
    }
    if (!mode.empty() && mode != "--foreground") {
        std::fprintf(stderr,
                     "usage: aicad-hostd [--foreground|--status|--stop [--force]|--version]\n");
        return 2;
    }

    const std::string self = selfPath();
    const std::string key = aicad_ipc::instanceKey(self);
    const std::string lock = aicad_ipc::lockPath(key);
    const std::string sock = aicad_ipc::socketPath(key);
    if (lock.empty() || sock.empty()) {
        std::fprintf(stderr, "aicad-hostd: no usable runtime dir\n");
        return 1;
    }

    hostd::log().open(aicad_ipc::stateDir() + "/logs");

    // 1. Single instance. Losing the race is SUCCESS for a spawn-on-demand
    //    daemon: the winner serves everyone; exit 0 so the spawner's retry
    //    loop lands on it.
    aicad_ipc::SingleInstance instance;
    std::string err;
    if (!instance.acquire(lock, err)) {
        hostd::log().line("spawn race lost (%s) — exiting 0", err.c_str());
        return 0;
    }

    // 2. Bind before engine load.
    aicad_ipc::IpcListener listener;
    if (!listener.listen(sock, err)) {
        hostd::log().line("listen failed: %s", err.c_str());
        std::fprintf(stderr, "aicad-hostd: %s\n", err.c_str());
        return 1;
    }

    hostd::Server server(std::move(listener), AICAD_HOSTD_BUILD_ID);
    server.children().setLogDir(aicad_ipc::stateDir() + "/logs");
    g_server = &server;
    std::signal(SIGTERM, onSignal);
    std::signal(SIGINT, onSignal);
    std::signal(SIGPIPE, SIG_IGN);  // dead client mid-write must not kill the daemon

    hostd::log().line("hostd %s starting: key=%s sock=%s pid=%ld", AICAD_HOSTD_BUILD_ID,
                      key.c_str(), sock.c_str(), (long)::getpid());

    // 3. Load the engine asynchronously (many seconds for OCCT).
    std::thread loader([&server] {
        std::string lerr;
        if (core::cpp_core().ensureLoaded(lerr)) {
            server.status().engineState = 1;
            hostd::log().line("engine loaded, abi=%s",
                              core::cpp_core().abiVersion().c_str());
        } else {
            server.status().engineErr = lerr;
            server.status().engineState = 2;
            hostd::log().line("engine load FAILED: %s", lerr.c_str());
        }
    });

    // 4. Autosave scheduler + prior-session recovery (Phase 3). Keyed by
    //    instance so scratch/test daemons never touch the real one's sessions.
    hostd::Autosave autosave;
    autosave.start(aicad_ipc::stateDir() + "/hostd_autosave/" + key,
                   &server.status().engineState);
    server.setAutosave(&autosave);

    int linger = 120;
    if (const char* l = std::getenv("AICAD_HOSTD_LINGER_SECS"); l && *l)
        linger = std::max(1, std::atoi(l));
    server.run(linger);

    // Clean shutdown (idle-exit, --stop drain, SIGTERM): save dirty docs so the
    // next daemon recovers them; loss bound on a hard kill = autosave interval.
    autosave.stop(/*finalSave=*/true);
    loader.join();
    hostd::log().line("hostd exiting");
    return 0;
}
