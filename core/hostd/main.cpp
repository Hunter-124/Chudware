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
//                               to stop them first). If the socket is dead but
//                               the pidfile process still lives, escalate:
//                               SIGTERM, wait 3s, then SIGKILL.
//   aicad-hostd --version       print build id + engine ABI
//
// Startup order matters: acquire instance lock -> bind socket -> THEN load the
// engine asynchronously. Racing clients connect immediately and see state:
// "loading" in the hello instead of a connect timeout.
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <thread>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <process.h>  // _getpid
#else
#include <limits.h>
#include <unistd.h>
#include <fcntl.h>
#include <cerrno>
#include <cstring>
#endif

#include "../../services/ipc/ipc_client.h"
#include "../../services/ipc/ipc_discovery.h"
#include "../../services/ipc/ipc_transport.h"
#include "../../services/worker/cpp_core.h"
#include "autosave.h"
#include "hostd_log.h"
#include "server.h"
#include "tray_icon.h"
#include "version_info.h"

static hostd::Server* g_server = nullptr;

static void trayQuitThunk(void*) {
    // When systemd supervises us (billet-hostd.service Restart=always), a bare
    // process exit is respawned in ~2s — the tray "Quit hostd" bounce users hit.
    // systemctl --user stop is a manual stop and stays down.
    //
    // Two traps this avoids:
    //  1. INVOCATION_ID leaks into ANY process spawned from a systemd-managed
    //     desktop session (GUI-spawned dev daemons inherit it), so env presence
    //     alone mis-stops the systemd unit while the dev daemon keeps running.
    //     Verify we really ARE the unit's process via /proc/self/cgroup.
    //  2. A SYNCHRONOUS `systemctl stop` deadlocks: it waits for the unit to die,
    //     but main() can't exit until trayStop() joins the GTK thread — which is
    //     blocked inside std::system. systemd SIGKILLs after TimeoutStopSec=30
    //     (observed: "failed (Result: signal) code=killed signal=KILL"). Run it
    //     async ("&") so the GTK thread returns immediately and SIGTERM does the rest.
#ifndef _WIN32
    bool under_unit = false;
    if (const char* inv = std::getenv("INVOCATION_ID"); inv && inv[0]) {
        if (int fd = ::open("/proc/self/cgroup", O_RDONLY | O_CLOEXEC); fd >= 0) {
            char buf[4096];
            const ssize_t n = ::read(fd, buf, sizeof buf - 1);
            ::close(fd);
            if (n > 0) {
                buf[n] = 0;
                under_unit = std::string(buf, (size_t)n).find("billet-hostd.service") !=
                             std::string::npos;
            }
        }
    }
    if (under_unit) {
        // systemctl --user needs the session bus; user services don't always get
        // DBUS_SESSION_BUS_ADDRESS. Fall back to the standard user-bus path.
        if (!std::getenv("DBUS_SESSION_BUS_ADDRESS")) {
            if (const char* xrt = std::getenv("XDG_RUNTIME_DIR"); xrt && *xrt) {
                const std::string bus = "unix:path=" + std::string(xrt) + "/bus";
                ::setenv("DBUS_SESSION_BUS_ADDRESS", bus.c_str(), /*overwrite=*/0);
            }
        }
        std::system("systemctl --user stop billet-hostd.service >/dev/null 2>&1 &");
        // SIGTERM lands in a moment (onSignal -> stop()). Do NOT also in-process
        // stop here: a clean self-exit before the stop job registers = Restart=
        // always bounce. If the stop call somehow fails, the next Quit click retries.
        return;
    }
#endif
    if (g_server) {
        g_server->status().draining = true;  // refuse new accepts while we wind down
        g_server->stop();
    }
}

// Map server status → tray tint + tooltip. Called from a lightweight ticker
// thread so the accept loop never blocks on GTK.
static void trayStatusTick(hostd::Server* server) {
    using hostd::TrayStatus;
    const auto& st = server->status();
    const int eng = st.engineState.load();
    const int clients = st.clients.load();
    // Engine calls only — Status::inflight includes every hello/status poll and
    // permanently reads "1 in-flight" while a GUI client is connected.
    const int engInflight = server->engineInflightCount();
    const bool draining = st.draining.load();
    TrayStatus ts = TrayStatus::Idle;
    char tip[256];
    if (eng == 2) {
        ts = TrayStatus::Error;
        std::snprintf(tip, sizeof tip, "Billet hostd — engine failed");
    } else if (draining) {
        ts = TrayStatus::Busy;
        std::snprintf(tip, sizeof tip, "Billet hostd — draining (%d client%s)",
                      clients, clients == 1 ? "" : "s");
    } else if (eng == 0) {
        ts = TrayStatus::Busy;
        std::snprintf(tip, sizeof tip, "Billet hostd — loading engine…");
    } else if (engInflight > 0) {
        ts = TrayStatus::Busy;
        std::snprintf(tip, sizeof tip, "Billet hostd — %d client%s, %d in-flight",
                      clients, clients == 1 ? "" : "s", engInflight);
    } else if (clients > 0) {
        ts = TrayStatus::Idle;
        std::snprintf(tip, sizeof tip, "Billet hostd — %d client%s (idle)",
                      clients, clients == 1 ? "" : "s");
    } else {
        ts = TrayStatus::Idle;
        std::snprintf(tip, sizeof tip, "Billet hostd — idle (ready)");
    }
    hostd::traySetStatus(ts, tip);
}

#ifdef _WIN32

// Ctrl-C / console close / logoff / system shutdown all funnel into the same
// graceful drain the POSIX SIGTERM handler runs. Returning TRUE claims the
// event; for CTRL_CLOSE/SHUTDOWN Windows still terminates us after a grace
// period, which the final autosave fits inside.
static BOOL WINAPI onConsoleCtrl(DWORD /*ctrlType*/) {
    if (g_server) g_server->stop();
    return TRUE;
}

static std::string selfPath() {
    wchar_t buf[MAX_PATH];
    DWORD n = GetModuleFileNameW(nullptr, buf, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return "";
    int len = WideCharToMultiByte(CP_UTF8, 0, buf, (int)n, nullptr, 0, nullptr, nullptr);
    std::string out(len, '\0');
    WideCharToMultiByte(CP_UTF8, 0, buf, (int)n, out.data(), len, nullptr, nullptr);
    return out;
}

static long selfPid() { return (long)_getpid(); }

#else

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

static long selfPid() { return (long)::getpid(); }

#endif

#ifndef _WIN32
// Read the PID recorded in the instance lock file (diagnostics written by
// SingleInstance::acquire). Returns 0 if unreadable / not a live process.
static long readLockPid(const std::string& lockFile) {
    if (lockFile.empty()) return 0;
    int fd = ::open(lockFile.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    char buf[64] = {0};
    const ssize_t n = ::read(fd, buf, sizeof buf - 1);
    ::close(fd);
    if (n <= 0) return 0;
    char* end = nullptr;
    const long pid = std::strtol(buf, &end, 10);
    if (pid <= 1 || end == buf) return 0;
    // Alive?
    if (::kill((pid_t)pid, 0) != 0) return 0;
    return pid;
}

// True while `pid` exists and is not a zombie.
static bool pidAlive(long pid) {
    if (pid <= 1) return false;
    if (::kill((pid_t)pid, 0) != 0) return false;
    char path[64];
    std::snprintf(path, sizeof path, "/proc/%ld/stat", pid);
    FILE* f = std::fopen(path, "r");
    if (!f) return false;
    char buf[512];
    const bool ok = std::fgets(buf, sizeof buf, f) != nullptr;
    std::fclose(f);
    if (!ok) return true;
    // Format: pid (comm) state ... — state is first token after the last ')'
    const char* rp = std::strrchr(buf, ')');
    if (!rp || !rp[1]) return true;
    return rp[2] != 'Z';
}

// Socket-dead fallback for --stop: the accept loop is wedged (or the sock was
// unlinked) while the process still holds the lock. Escalate via the pidfile.
// Returns 0 if the process was killed (or already gone); 1 if nothing to kill.
static int stopViaPidfile(const std::string& reason) {
    const std::string self = selfPath();
    const std::string key = aicad_ipc::instanceKey(self);
    const std::string lock = aicad_ipc::lockPath(key);
    const long pid = readLockPid(lock);
    if (pid <= 1) {
        std::fprintf(stderr,
                     "aicad-hostd: no running instance (%s); no live pid in %s\n",
                     reason.c_str(), lock.empty() ? "(no lock path)" : lock.c_str());
        return 1;
    }
    std::fprintf(stderr,
                 "aicad-hostd: socket dead (%s); escalating --stop to pid %ld via pidfile\n",
                 reason.c_str(), pid);
    if (::kill((pid_t)pid, SIGTERM) != 0) {
        if (errno == ESRCH) {
            std::printf("{\"stopped\":true,\"via\":\"pidfile\",\"pid\":%ld,"
                        "\"signal\":\"none\",\"note\":\"already gone\"}\n",
                        pid);
            return 0;
        }
        std::fprintf(stderr, "aicad-hostd: SIGTERM pid %ld failed: %s\n", pid,
                     std::strerror(errno));
        return 1;
    }
    // Wait up to 3 s for a clean exit after TERM.
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::seconds(3);
    while (std::chrono::steady_clock::now() < deadline) {
        if (!pidAlive(pid)) {
            std::printf("{\"stopped\":true,\"via\":\"pidfile\",\"pid\":%ld,"
                        "\"signal\":\"SIGTERM\"}\n",
                        pid);
            return 0;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    std::fprintf(stderr,
                 "aicad-hostd: pid %ld still alive after SIGTERM; sending SIGKILL\n",
                 pid);
    if (::kill((pid_t)pid, SIGKILL) != 0 && errno != ESRCH) {
        std::fprintf(stderr, "aicad-hostd: SIGKILL pid %ld failed: %s\n", pid,
                     std::strerror(errno));
        return 1;
    }
    // Brief wait so callers see the process gone.
    for (int i = 0; i < 40; ++i) {
        if (!pidAlive(pid)) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    std::printf("{\"stopped\":true,\"via\":\"pidfile\",\"pid\":%ld,"
                "\"signal\":\"SIGKILL\"}\n",
                pid);
    return 0;
}
#else
// Windows lock is a named mutex (no pidfile). Socket-dead escalation is a
// no-op beyond reporting the original connect failure.
static int stopViaPidfile(const std::string& reason) {
    std::fprintf(stderr, "aicad-hostd: no running instance (%s)\n", reason.c_str());
    return 1;
}
#endif

// --status / --stop [--force]: attach-only client against the same instance key.
// --stop falls back to pidfile SIGTERM/SIGKILL when the socket is dead but the
// process still holds the lock (wedged accept loop / half-dead daemon).
static int controlCommand(const char* method,
                          const nlohmann::json& params = nlohmann::json::object(),
                          bool stopEscalation = false) {
    aicad_ipc::EngineClient cli;
    aicad_ipc::EngineClient::Options opts;
    opts.hostdBin = selfPath();
    opts.clientKind = "control";
    opts.allowSpawn = false;
    std::string err;
    if (!cli.connectOrSpawn(opts, err)) {
        if (stopEscalation) return stopViaPidfile(err);
        std::fprintf(stderr, "aicad-hostd: no running instance (%s)\n", err.c_str());
        return 1;
    }
    nlohmann::json result;
    // Bound --stop so a wedged accept loop (socket up, process frozen) cannot
    // hang the control client forever; fall through to pidfile escalation.
    const int callTimeoutMs = stopEscalation ? 2000 : -1;
    if (!cli.call(method, params, result, err, callTimeoutMs)) {
        // Connected then failed mid-call (reset / timeout) — same wedged class.
        if (stopEscalation) {
            cli.disconnect();
            return stopViaPidfile(err);
        }
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
        // Socket-dead + live pidfile: escalate to SIGTERM / SIGKILL.
        const bool force = argc > 2 && std::string(argv[2]) == "--force";
        return controlCommand("daemon_drain", {{"force", force}},
                              /*stopEscalation=*/true);
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
        // T-PERF10: spawn-loser flock-wait. The incumbent may be mid-drain with
        // the sock still up (or briefly gone). Poll for either a live sock
        // (client will reconnect) or a freed lock we can claim — up to ~5 s —
        // instead of immediately exiting and leaving the spawner to burn budget.
        hostd::log().line("spawn race lost (%s) — waiting for incumbent", err.c_str());
        const auto deadline =
            std::chrono::steady_clock::now() + std::chrono::seconds(5);
        while (std::chrono::steady_clock::now() < deadline) {
            std::string cerr;
            aicad_ipc::IpcConn probe = aicad_ipc::connectTo(sock, cerr);
            if (probe.valid()) {
                hostd::log().line("incumbent reachable — exiting 0 for client reconnect");
                return 0;
            }
            if (instance.acquire(lock, err)) {
                hostd::log().line("lock freed — becoming the daemon");
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        if (!instance.held()) {
            hostd::log().line("spawn race still lost after wait — exiting 0");
            return 0;
        }
    }

#ifndef _WIN32
    // 1b. Housekeeping: sweep dead sibling endpoints. SIGKILLed daemons (mostly
    // scratch pytest instances) leave hostd-*.sock/.lock pairs behind forever —
    // the flock died with the process, so a non-blocking acquire tells us the
    // owner is gone and the pair is garbage. Own key is skipped (we hold it).
    {
        namespace fs = std::filesystem;
        const std::string dir = fs::path(sock).parent_path().string();
        std::error_code ec;
        int swept = 0;
        for (const auto& e : fs::directory_iterator(dir, ec)) {
            const std::string name = e.path().filename().string();
            if (name.rfind("hostd-", 0) != 0 || e.path().extension() != ".lock") continue;
            if (e.path().string() == lock) continue;
            aicad_ipc::SingleInstance probe;
            std::string perr;
            if (!probe.acquire(e.path().string(), perr)) continue;  // owner alive
            fs::remove(e.path(), ec);
            fs::path s = e.path();
            s.replace_extension(".sock");
            if (fs::remove(s, ec)) ++swept;
        }
        if (swept) hostd::log().line("swept %d dead sibling endpoint(s) in %s", swept, dir.c_str());
    }
#endif

    // 2. Bind before engine load.
    aicad_ipc::IpcListener listener;
    if (!listener.listen(sock, err)) {
        hostd::log().line("listen failed: %s", err.c_str());
        std::fprintf(stderr, "aicad-hostd: %s\n", err.c_str());
        return 1;
    }

    hostd::Server server(std::move(listener), AICAD_HOSTD_BUILD_ID);
    server.children().setLogDir(aicad_ipc::stateDir() + "/logs");
    server.setSelfBinary(self);  // stale-binary self-drain watches this + libaicad
    g_server = &server;
#ifdef _WIN32
    SetConsoleCtrlHandler(onConsoleCtrl, TRUE);
    // No SIGPIPE on Windows: a dead client mid-write surfaces as a WriteFile
    // error (ERROR_BROKEN_PIPE), which IpcConn::writeLine already handles.
#else
    std::signal(SIGTERM, onSignal);
    std::signal(SIGINT, onSignal);
    std::signal(SIGPIPE, SIG_IGN);  // dead client mid-write must not kill the daemon
#endif

    hostd::log().line("hostd %s starting: key=%s sock=%s pid=%ld", AICAD_HOSTD_BUILD_ID,
                      key.c_str(), sock.c_str(), selfPid());

    // Optional system tray (Linux + libappindicator). No-op when disabled,
    // headless, or the feature was not compiled in.
    hostd::trayStart(self);
    hostd::traySetQuitHandler(&trayQuitThunk, nullptr);
    hostd::traySetStatus(hostd::TrayStatus::Busy, "Billet hostd — starting…");
    std::atomic<bool> trayTickerStop{false};
    std::thread trayTicker([&server, &trayTickerStop] {
        while (!trayTickerStop.load()) {
            trayStatusTick(&server);
            for (int i = 0; i < 10 && !trayTickerStop.load(); ++i)
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    });

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

    trayTickerStop.store(true);
    if (trayTicker.joinable()) trayTicker.join();
    hostd::trayStop();

    // Clean shutdown (idle-exit, --stop drain, SIGTERM): save dirty docs so the
    // next daemon recovers them; loss bound on a hard kill = autosave interval.
    autosave.stop(/*finalSave=*/true);
    loader.join();
    // T-PERF10 drain-window: hand the sock path to SingleInstance BEFORE the
    // Server dtor closes the listen fd, so unlink happens only at flock release
    // (instance is destroyed after server, so order is: close fd → release flock
    // → unlink sock). Clients never observe lock-held-with-no-sock.
    instance.setSockPathToUnlink(server.detachSocketForFlockRelease());
    hostd::log().line("hostd exiting");
    return 0;
}
