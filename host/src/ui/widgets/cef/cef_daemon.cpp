// cef/cef_daemon.cpp — see cef_daemon.h. The lazily-spawned CEF host that serves
// headless fetch/download to the Python scraper over an aicad_ipc socket, then
// idle-exits. Compiled only with AI_CAD_ENABLE_CEF.
#include "cef_daemon.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "ipc_transport.h"  // aicad_ipc::IpcListener / IpcConn

#include "cef_app.h"        // initialize()/shutdown()/initialized()
#include "cef_headless.h"   // headless_fetch()/headless_download()
#include "cef_platform.h"   // cache_root()

using nlohmann::json;

namespace ui::cef {
namespace {

// The warm persistent profile. The daemon is now its SOLE on-disk owner (Phase 2b):
// the interactive panel writes its OWN separate profile (cef_web_view.cpp's
// panel_profile_dir) so two Chromium processes never write this store concurrently.
// A login in the panel carries here via the "import_session" method below, which
// re-writes the panel's exported cookies into this jar under the daemon's own
// OSCrypt (raw dir-sharing can't hand a session across processes — see MEMORY
// vendor-login-session-handoff).
std::string warm_profile_dir() {
    if (const char* ov = std::getenv("AI_CAD_CEF_PROFILE_DIR"); ov && ov[0]) return ov;
    return cef_platform::cache_root() + "/_warm_profile";
}

// Handle one JSON-RPC request line; returns the response object. Sets `stop` on a
// "shutdown" request. Protocol mirrors core/hostd (newline-delimited JSON-RPC):
//   {"id":N,"method":"fetch|download|ping|shutdown","params":{...}}
//   -> {"id":N,"result":{...}} | {"id":N,"error":"msg"}
// fetch params:    {url, out?(file for html), settle_ms?, profile?}
// download params: {url, dest, profile?}
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
    if (method == "fetch") {
        const std::string url = p.value("url", "");
        if (url.empty()) return err("missing url");
        const std::string out = p.value("out", "");
        const int settle = p.value("settle_ms", 1200);
        const std::string profile = p.value("profile", warm_profile_dir());
        FetchResult r = headless_fetch(url, settle, profile);
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
    // DIAGNOSTIC (opt-in AI_CAD_CEF_PROBE): drive a real form login headlessly to
    // observe a bot-wall verdict. Executes caller JS in the page, so it is gated off
    // by default and never part of the normal fetch/download surface.
    if (method == "login_probe") {
        if (const char* g = std::getenv("AI_CAD_CEF_PROBE"); !(g && g[0] && g[0] != '0'))
            return err("login_probe disabled (set AI_CAD_CEF_PROBE=1)");
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

}  // namespace

int run_cef_daemon(const std::string& sock_path, int idle_secs) {
    // GUI and daemon must not share Chromium ProcessSingleton (same root_cache_path).
    // Mark this process before initialize() so cache_root() → cef_cache_daemon.
    cef_platform::set_daemon_process(true);
    if (!initialize()) { std::fprintf(stderr, "[cef-daemon] CEF init failed\n"); return 1; }

    aicad_ipc::IpcListener listener;
    std::string err;
    if (!listener.listen(sock_path, err)) {
        std::fprintf(stderr, "[cef-daemon] listen(%s) failed: %s\n", sock_path.c_str(), err.c_str());
        shutdown();
        return 1;
    }
    std::fprintf(stderr, "[cef-daemon] listening on %s (idle=%ds)\n", sock_path.c_str(), idle_secs);

    auto last = std::chrono::steady_clock::now();
    bool stop = false;
    while (!stop) {
        aicad_ipc::IpcConn c = listener.accept(/*timeoutMs=*/250);
        if (c.valid()) {
            last = std::chrono::steady_clock::now();
            std::string line;
            if (c.readLine(line, /*timeoutMs=*/120000)) {
                json resp = handle(line, stop);
                c.writeLine(resp.dump());
            }
            // one request per connection; the client opens a fresh conn per call.
        } else if (idle_secs > 0) {
            const auto idle = std::chrono::steady_clock::now() - last;
            if (std::chrono::duration_cast<std::chrono::seconds>(idle).count() >= idle_secs) {
                std::fprintf(stderr, "[cef-daemon] idle %ds — exiting\n", idle_secs);
                stop = true;
            }
        }
    }
    headless_close_all();  // close pooled browsers before CefShutdown
    shutdown();
    return 0;
}

}  // namespace ui::cef
