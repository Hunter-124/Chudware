#include "engine_backend.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <mutex>
#include <thread>

#include "cpp_core.h"

#ifndef _WIN32
#include <unistd.h>

#include "../ipc/ipc_client.h"
#include "../ipc/ipc_discovery.h"
#endif

namespace engine {

using nlohmann::json;

namespace {

#ifndef _WIN32
aicad_ipc::EngineClient* g_client = nullptr;  // non-null iff daemon mode won
#endif
bool g_daemon = false;
std::once_flag g_once;

#ifndef _WIN32
// hostd binary hint, mirroring services/mcp/daemon_bridge.py resolve_hostd_bin:
// $AICAD_HOSTD_BIN is honored inside resolveHostdBin(); the hint covers
// exe-adjacent (installed layout) then the repo dev-tree build.
std::string hostdBinHint() {
    namespace fs = std::filesystem;
    std::error_code ec;
    // exe-adjacent
    char buf[4096];
    ssize_t n = ::readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n > 0) {
        buf[n] = 0;
        fs::path cand = fs::path(buf).parent_path() / "aicad-hostd";
        if (fs::is_regular_file(cand, ec)) return cand.string();
    }
    // repo dev tree: <exe>/../../../../core/build-occt-linux/bin/aicad-hostd is
    // fragile — walk up from the exe looking for core/build-occt-linux instead.
    if (n > 0) {
        fs::path dir = fs::path(buf).parent_path();
        for (int i = 0; i < 6 && !dir.empty(); ++i, dir = dir.parent_path()) {
            fs::path cand = dir / "core" / "build-occt-linux" / "bin" / "aicad-hostd";
            if (fs::is_regular_file(cand, ec)) return cand.string();
            if (dir == dir.parent_path()) break;
        }
    }
    return std::string();
}

// One-shot startup latency probe: N pings over the socket, logged to stderr so
// the number lands in the app log/console (Phase 4 deliverable: <0.5 ms/call).
void logPingLatency(aicad_ipc::EngineClient& c) {
    json r;
    std::string e;
    const int N = 200;
    const auto t0 = std::chrono::steady_clock::now();
    for (int i = 0; i < N; ++i)
        if (!c.call("ping", json::object(), r, e)) return;
    const double us = std::chrono::duration<double, std::micro>(
                          std::chrono::steady_clock::now() - t0).count() / N;
    std::fprintf(stderr, "[engine] daemon ping round-trip: %.1f us/call (%d calls)\n", us, N);
}
#endif

void initOnce() {
#ifndef _WIN32
    const char* f = std::getenv("AI_CAD_ENGINE_DAEMON");
    if (!f || !f[0] || f[0] == '0') return;  // default OFF until Phase 8
    auto* c = new aicad_ipc::EngineClient();
    aicad_ipc::EngineClient::Options opts;
    opts.hostdBin = hostdBinHint();
    opts.clientKind = "gui";
    std::string err;
    if (!c->connectOrSpawn(opts, err)) {
        // core_bridge.py-style loud warning + legacy fallback.
        std::fprintf(stderr,
                     "[engine] WARNING: AI_CAD_ENGINE_DAEMON=1 but hostd connect failed (%s) "
                     "— falling back to in-process libaicad\n",
                     err.c_str());
        delete c;
        return;
    }
    g_client = c;
    g_daemon = true;
    std::fprintf(stderr, "[engine] daemon mode: connected to aicad-hostd (state=%s)\n",
                 c->serverInfo().value("state", std::string("?")).c_str());
    logPingLatency(*c);
#endif
}

#ifndef _WIN32
// Absorb {"code":"loading"}: the daemon binds before the (slow) OCCT load and
// answers engine calls with a retriable loading error until ready. Mirror the
// Python bridge: poll daemon_status, bounded.
bool waitEngineReady(aicad_ipc::EngineClient& c, std::string& err) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(300);
    while (std::chrono::steady_clock::now() < deadline) {
        json st;
        std::string e;
        if (!c.call("daemon_status", json::object(), st, e)) { err = e; return false; }
        const std::string state = st.value("state", std::string());
        if (state == "ready") return true;
        if (state == "failed") { err = "engine failed to load in hostd"; return false; }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    err = "engine load timed out in hostd";
    return false;
}
#endif

}  // namespace

bool daemonMode() {
    std::call_once(g_once, initOnce);
    return g_daemon;
}

aicad_ipc::EngineClient* client() {
#ifndef _WIN32
    return daemonMode() ? g_client : nullptr;
#else
    return nullptr;
#endif
}

bool call(const std::string& method, const json& params, json& result, std::string& err) {
    if (!daemonMode()) return core::cpp_core().call(method, params, result, err);
#ifndef _WIN32
    bool ok = g_client->call(method, params, result, err);
    if (!ok && result.is_object()) {
        const std::string code = result.value("code", std::string());
        if (code == "loading") {
            std::string werr;
            if (waitEngineReady(*g_client, werr))
                ok = g_client->call(method, params, result, err);
            else if (err.empty())
                err = werr;
        } else if (code == "read_only" && params.is_object() && params.contains("doc_id")) {
            // The user edited a doc this client only VIEWS (an agent's mirror
            // tab): human steering wins — upgrade the attach to rw and retry
            // once. Documented Phase 4 choice: auto-upgrade over new UI.
            json ar;
            std::string ae;
            if (g_client->call("attach_doc",
                               json{{"doc_id", params["doc_id"]}, {"mode", "rw"}}, ar, ae))
                ok = g_client->call(method, params, result, err);
        }
    }
    return ok;
#else
    return false;  // unreachable: daemonMode() is false on Windows (Phase 6)
#endif
}

bool available() {
    if (daemonMode()) return true;
    return core::cpp_core().available();
}

bool ensureLoaded(std::string& err) {
    if (daemonMode()) return true;
    return core::cpp_core().ensureLoaded(err);
}

}  // namespace engine
