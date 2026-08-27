// cef/cef_daemon.cpp — see cef_daemon.h. The lazily-spawned CEF host that serves
// headless fetch/download to the Python scraper over an chudware_ipc socket, then
// idle-exits. Compiled only with CHUDWARE_ENABLE_CEF.
#include "cef_daemon.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "ipc_transport.h"  // chudware_ipc::IpcListener / IpcConn

#include "cef_app.h"        // initialize()/shutdown()/initialized()
#include "cef_headless.h"   // headless_fetch()/headless_download()
#include "cef_platform.h"   // cache_root()

using nlohmann::json;

namespace ui::cef {
namespace {

using steady = std::chrono::steady_clock;

// The warm persistent profile. The daemon is now its SOLE on-disk owner (Phase 2b):
// the interactive panel writes its OWN separate profile (cef_web_view.cpp's
// panel_profile_dir) so two Chromium processes never write this store concurrently.
// A login in the panel carries here via the "import_session" method below, which
// re-writes the panel's exported cookies into this jar under the daemon's own
// OSCrypt (raw dir-sharing can't hand a session across processes — see MEMORY
// vendor-login-session-handoff).
std::string warm_profile_dir() {
    if (const char* ov = std::getenv("CHUDWARE_CEF_PROFILE_DIR"); ov && ov[0]) return ov;
    return cef_platform::cache_root() + "/_warm_profile";
}

// Handle one JSON-RPC request line; returns the response object. Sets `stop` on a
// "shutdown" request. Protocol mirrors core/hostd (newline-delimited JSON-RPC):
//   {"id":N,"method":"fetch|download|ping|shutdown","params":{...}}
//   -> {"id":N,"result":{...}} | {"id":N,"error":"msg"}
// fetch params:    {url, out?(file for html), settle_ms?, profile?}
// download params: {url, dest, profile?}
//
// "fetch" is NOT served here: run_cef_daemon intercepts it and drives it as an
// in-flight job so several fetches overlap on the one CEF pump (see below). Every
// other method still runs to completion inline — they are rare, and they pump the
// same shared loop, so live fetch jobs keep advancing while one runs.
json handle(const std::string& line, bool& stop) {
    json req = json::parse(line, nullptr, /*allow_exceptions=*/false);
    if (req.is_discarded()) return json{{"id", 0}, {"error", "bad json"}};

    const int id = req.value("id", 0);
    const std::string method = req.value("method", "");
    const json p = req.value("params", json::object());
    auto err = [&](const char* m) { return json{{"id", id}, {"error", m}}; };

    if (method == "ping") return json{{"id", id}, {"result", {{"ok", true}}}};
    if (method == "shutdown") {
        stop = true;
        return json{{"id", id}, {"result", {{"ok", true}}}};
    }
    if (method == "download") {
        const std::string url = p.value("url", "");
        const std::string dest = p.value("dest", "");
        if (url.empty() || dest.empty()) return err("missing url/dest");
        const std::string profile = p.value("profile", warm_profile_dir());
        const bool ok = headless_download(url, dest, profile);
        return json{{"id", id}, {"result", {{"ok", ok}, {"dest", dest}}}};
    }
    // Inject a cookie jar exported from the interactive panel into the warm profile,
    // so a login there carries into headless fetch/download here (the panel→daemon
    // session hand-off; the panel no longer shares this on-disk profile).
    // params: {cookies:[{name,value,domain,path?,secure?,httponly?,expires?,same_site?}], profile?}
    if (method == "import_session") {
        const json jar = p.value("cookies", json::array());
        if (!jar.is_array() || jar.empty()) return err("missing cookies");
        std::vector<ImportCookie> cookies;
        cookies.reserve(jar.size());
        for (const auto& j : jar) {
            if (!j.is_object()) continue;
            ImportCookie c;
            c.name      = j.value("name", "");
            c.value     = j.value("value", "");
            c.domain    = j.value("domain", "");
            c.path      = j.value("path", "");
            c.secure    = j.value("secure", false);
            c.http_only = j.value("httponly", false);
            c.expires   = j.value("expires", 0.0);
            c.same_site = j.value("same_site", "");
            cookies.push_back(std::move(c));
        }
        const std::string profile = p.value("profile", warm_profile_dir());
        const int n = headless_import_cookies(profile, cookies);
        return json{{"id", id}, {"result", {{"ok", n > 0}, {"count", n}}}};
    }
    // Run caller JS in the page and wait for a sentinel DOM element to publish a
    // result — the reusable primitive for SPA configurators that generate a
    // download URL client-side (McMaster CAD FilePath, Misumi series→SKU).
    // params: {url, script, sentinel?, settle_ms?, timeout_ms?, profile?}
    if (method == "interact") {
        const std::string url = p.value("url", "");
        const std::string js = p.value("script", "");
        if (url.empty() || js.empty()) return err("missing url/script");
        const std::string sentinel = p.value("sentinel", "__aicad_capture");
        const int settle = p.value("settle_ms", 4000);
        const int timeout = p.value("timeout_ms", 12000);
        const std::string profile = p.value("profile", warm_profile_dir());
        FetchResult r = headless_interact(url, js, sentinel, settle, timeout, profile);
        json result = {{"ok", r.ok}, {"status", r.status}, {"final_url", r.final_url},
                       {"html_len", r.html.size()}};
        if (r.ok) result["html"] = r.html;
        return json{{"id", id}, {"result", result}};
    }
    // DIAGNOSTIC (opt-in CHUDWARE_CEF_PROBE): drive a real form login headlessly to
    // observe a bot-wall verdict. Executes caller JS in the page, so it is gated off
    // by default and never part of the normal fetch/download surface.
    if (method == "login_probe") {
        if (const char* g = std::getenv("CHUDWARE_CEF_PROBE"); !(g && g[0] && g[0] != '0'))
            return err("login_probe disabled (set CHUDWARE_CEF_PROBE=1)");
        const std::string url = p.value("url", "");
        const std::string js = p.value("fill_js", "");
        if (url.empty() || js.empty()) return err("missing url/fill_js");
        const int settle = p.value("settle_ms", 3000);
        const int postw = p.value("post_wait_ms", 12000);
        const std::string profile = p.value("profile", warm_profile_dir());
        FetchResult r = headless_login_probe(url, js, settle, postw, profile);
        json result = {{"ok", r.ok}, {"status", r.status}, {"final_url", r.final_url},
                       {"html_len", r.html.size()}};
        if (r.ok) result["html"] = r.html;
        return json{{"id", id}, {"result", result}};
    }
    return err("unknown method");
}

// ---------------------------------------------------------------------------
// Overlapped fetch service.
//
// The accept loop used to run each request to completion inline, so the parts
// fan-out's six per-site threads all queued behind one another and each paid its
// own full render-settle dwell (measured: 6 sites, 8s settle → 53s wall, a clean
// ~8.9s staircase). CefDoMessageLoopWork() may only be called on this one thread
// (include/cef_app.h), so the fix is not worker threads: it is N browsers driven
// by ONE pump, with the accept loop never blocking on a single request.
//
// Every connection is answered exactly once — on success, on failure, on the
// per-request deadline, and on drain at shutdown.
// ---------------------------------------------------------------------------

// Accepted, request line not yet fully read.
struct Waiting {
    chudware_ipc::IpcConn conn;
    steady::time_point since;
};

// A parsed fetch waiting for a free browser.
struct Queued {
    chudware_ipc::IpcConn conn;
    int id = 0;
    std::string url, out, profile;
    int settle_ms = 1200;
    steady::time_point since;
};

// A fetch whose browser is loading/settling right now.
struct InFlight {
    chudware_ipc::IpcConn conn;
    int id = 0;
    std::string out;
    FetchJobPtr job;
    steady::time_point deadline;
};

// How long a connection may sit without sending its request line (matches the old
// inline readLine budget), and how long a fetch may sit queued for a browser.
constexpr int kRequestReadTimeoutMs = 120000;
constexpr int kQueueTimeoutMs       = 180000;
// Backstop above the job's own nav (10s) + capture (2s) timeouts. The job always
// self-terminates; this only fires if CEF itself stops advancing.
constexpr int kFetchSlackMs         = 45000;
// One pump slice per loop turn while work is in flight.
constexpr int kPumpSliceMs          = 20;

json fetch_response(int id, const FetchResult& r, const std::string& out) {
    if (r.ok && !out.empty()) {
        std::ofstream f(out, std::ios::binary);
        f.write(r.html.data(), static_cast<std::streamsize>(r.html.size()));
    }
    json result = {{"ok", r.ok}, {"status", r.status}, {"final_url", r.final_url},
                   {"html_len", r.html.size()}};
    if (r.ok && out.empty()) result["html"] = r.html;  // inline when no sidecar file
    else result["out"] = out;
    return json{{"id", id}, {"result", result}};
}

// The failure shape a fetch that never ran must still produce (same as a fetch
// whose navigation failed), so no caller is ever left hanging.
json fetch_failure(int id, const std::string& out) {
    return fetch_response(id, FetchResult{}, out);
}

// Monotonic ms of the last thing this daemon actually did. Written by the serve loop,
// read by the watchdog thread.
std::atomic<long long> g_last_activity_ms{0};

long long steady_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               steady::now().time_since_epoch())
        .count();
}

// Force-exit this process after `idle_secs` with nothing to do, from a thread that
// does NOTHING else.
//
// WHY a separate thread: the serve loop below is single-threaded and also drives the
// native CEF pump (CefDoMessageLoopWork, which may only run on that one thread), and
// initialize() itself can block indefinitely on Chromium's ProcessSingleton when
// another daemon already holds the shared profile lock. In both cases the loop's own
// idle check never runs again, and the daemon's ONLY self-termination mechanism is
// gone. Measured consequence: 193 of these processes alive 34 h after every spawner
// had died, their socket files long since unlinked — each one spawned because the
// previous one never came up. This thread is the guarantee the loop cannot give.
//
// _Exit, not exit/return: a wedged process must not run CEF teardown or atexit
// handlers, both of which can block on the same thing that wedged it.
void start_idle_watchdog(int idle_secs) {
    if (idle_secs <= 0) return;  // 0 = run until killed (dev/debug)
    g_last_activity_ms = steady_ms();
    std::thread([idle_secs] {
        const long long limitMs = (long long)idle_secs * 1000;
        for (;;) {
            std::this_thread::sleep_for(std::chrono::seconds(1));
            if (steady_ms() - g_last_activity_ms.load() < limitMs) continue;
            std::fprintf(stderr, "[cef-daemon] idle %ds — exiting (watchdog)\n", idle_secs);
            std::fflush(stderr);
            std::_Exit(0);
        }
    }).detach();
}

}  // namespace

int run_cef_daemon(const std::string& sock_path, int idle_secs) {
    // Armed BEFORE initialize(): a ProcessSingleton stall inside CEF init is one of the two
    // ways this process wedges, and it happens before the serve loop ever runs.
    start_idle_watchdog(idle_secs);
    // GUI and daemon must not share Chromium ProcessSingleton (same root_cache_path).
    // Mark this process before initialize() so cache_root() → cef_cache_daemon.
    cef_platform::set_daemon_process(true);
    if (!initialize()) { std::fprintf(stderr, "[cef-daemon] CEF init failed\n"); return 1; }

    chudware_ipc::IpcListener listener;
    std::string err;
    if (!listener.listen(sock_path, err)) {
        std::fprintf(stderr, "[cef-daemon] listen(%s) failed: %s\n", sock_path.c_str(), err.c_str());
        shutdown();
        return 1;
    }
    const int max_inflight = headless_max_inflight();
    std::fprintf(stderr, "[cef-daemon] listening on %s (idle=%ds, max_inflight=%d)\n",
                 sock_path.c_str(), idle_secs, max_inflight);

    std::vector<Waiting>  waiting;
    std::vector<Queued>   queued;
    std::vector<InFlight> inflight;

    // One clock for both idle checks: the loop's cooperative one (below) and the watchdog's
    // hard one. mark_activity() must be called wherever `last` is updated.
    auto last = steady::now();
    const auto mark_activity = [&last](steady::time_point t) {
        last = t;
        g_last_activity_ms = steady_ms();
    };
    bool stop = false;
    // Keep servicing until a shutdown/idle-exit AND every in-flight or queued
    // request has been answered — a connection is never dropped unanswered.
    while (!stop || !inflight.empty() || !queued.empty()) {
        const steady::time_point now = steady::now();
        const bool busy = !waiting.empty() || !queued.empty() || !inflight.empty();

        // 1. Accept. Never block while there is work to advance; a 250ms park when
        //    fully idle keeps the idle-exit check responsive at ~no CPU.
        if (!stop) {
            const int accept_ms = !inflight.empty() ? 0 : (busy ? 25 : 250);
            chudware_ipc::IpcConn c = listener.accept(accept_ms);
            if (c.valid()) {
                mark_activity(now);
                waiting.push_back(Waiting{std::move(c), now});
            }
        }

        // 2. Drain request lines without blocking. The client writes its request
        //    immediately after connecting, so this is normally a single poll.
        for (size_t i = 0; i < waiting.size();) {
            std::string line;
            bool drop = false;
            if (waiting[i].conn.readLine(line, /*timeoutMs=*/0)) {
                mark_activity(now);
                json req = json::parse(line, nullptr, /*allow_exceptions=*/false);
                const std::string method = req.is_discarded() ? std::string{}
                                                              : req.value("method", "");
                if (method == "fetch") {
                    const json p = req.value("params", json::object());
                    const std::string url = p.value("url", "");
                    const int id = req.value("id", 0);
                    if (url.empty()) {
                        waiting[i].conn.writeLine(
                            json{{"id", id}, {"error", "missing url"}}.dump());
                    } else {
                        queued.push_back(Queued{std::move(waiting[i].conn), id, url,
                                                p.value("out", std::string{}),
                                                p.value("profile", warm_profile_dir()),
                                                p.value("settle_ms", 1200), now});
                    }
                } else {
                    json resp = handle(line, stop);
                    waiting[i].conn.writeLine(resp.dump());
                }
                drop = true;  // one request per connection; the client reconnects
            } else if (waiting[i].conn.eof() ||
                       std::chrono::duration_cast<std::chrono::milliseconds>(
                           now - waiting[i].since).count() >= kRequestReadTimeoutMs) {
                drop = true;
            }
            if (drop) waiting.erase(waiting.begin() + static_cast<long>(i));
            else ++i;
        }

        // 3. Start queued fetches on free browsers, oldest first.
        while (!queued.empty() && static_cast<int>(inflight.size()) < max_inflight) {
            Queued& q = queued.front();
            FetchJobPtr job = headless_fetch_start(q.url, q.settle_ms, q.profile);
            if (!job) {
                // No browser available right now. If nothing is running to free one
                // up, this is a hard failure (CEF down / create refused) — answer
                // with the fetch failure shape rather than spinning forever.
                const bool stalled = inflight.empty();
                const bool expired =
                    std::chrono::duration_cast<std::chrono::milliseconds>(now - q.since)
                        .count() >= kQueueTimeoutMs;
                if (stalled || expired) {
                    std::fprintf(stderr, "[cef-daemon] no browser for %s (%s)\n",
                                 q.url.c_str(), stalled ? "create failed" : "queue timeout");
                    q.conn.writeLine(fetch_failure(q.id, q.out).dump());
                    queued.erase(queued.begin());
                    continue;
                }
                break;  // a live job will free a browser shortly
            }
            inflight.push_back(InFlight{std::move(q.conn), q.id, q.out, std::move(job),
                                        now + std::chrono::milliseconds(q.settle_ms +
                                                                        kFetchSlackMs)});
            queued.erase(queued.begin());
            mark_activity(now);
        }

        // 4. One shared pump advances EVERY live browser, then each job is stepped
        //    and completions answer their OWN connection.
        if (!inflight.empty()) {
            // A live fetch IS activity: stamp before the pump so a long navigation can never
            // look idle to the watchdog thread.
            mark_activity(now);
            headless_pump(kPumpSliceMs);
            const steady::time_point tick = steady::now();
            for (size_t i = 0; i < inflight.size();) {
                const bool done = headless_fetch_poll(inflight[i].job);
                const bool late = tick >= inflight[i].deadline;
                if (done || late) {
                    // take() aborts an unfinished job: it cancels the navigation and
                    // parks that browser to cool, so a wedged page cannot wedge the
                    // daemon or bleed its DOM into the next request.
                    FetchResult r = headless_fetch_take(inflight[i].job);
                    if (late && !done)
                        std::fprintf(stderr, "[cef-daemon] fetch id=%d hit its deadline\n",
                                     inflight[i].id);
                    inflight[i].conn.writeLine(
                        fetch_response(inflight[i].id, r, inflight[i].out).dump());
                    inflight.erase(inflight.begin() + static_cast<long>(i));
                    mark_activity(steady::now());
                } else {
                    ++i;
                }
            }
        } else if (idle_secs > 0 && !stop && waiting.empty() && queued.empty()) {
            const auto idle = steady::now() - last;
            if (std::chrono::duration_cast<std::chrono::seconds>(idle).count() >= idle_secs) {
                std::fprintf(stderr, "[cef-daemon] idle %ds — exiting\n", idle_secs);
                stop = true;
            }
        }
    }

    // Nothing is in flight here (the loop condition guarantees it), so no browser
    // is mid-navigation when CEF tears down.
    headless_close_all();  // close pooled browsers before CefShutdown
    shutdown();
    return 0;
}

}  // namespace ui::cef
