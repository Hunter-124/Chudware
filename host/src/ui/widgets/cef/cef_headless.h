// cef/cef_headless.h — CEF-native headless page fetch + file download.
//
// This is the replacement for the Playwright headless path (services/browser):
// drive our OWN CEF Chromium directly instead of Playwright, which removes the
// automation-protocol (CDP handshake / Runtime.enable) fingerprint that bot-gates
// key on — the single biggest win per the 2026 anti-detect benchmark. HTML is read
// with CefFrame::GetSource and bytes with CefURLRequest; neither enables the
// Runtime domain, so there is no DevTools-driven tell.
// Both calls require initialize() to have succeeded and pump the CEF loop inline,
// so they must run on the browser-process main thread (the same context that
// --cef-shot uses). Compiled only with CHUDWARE_ENABLE_CEF.
//
// CONCURRENCY (headless_fetch_start/poll/take, below): CefDoMessageLoopWork() is
// documented "should only be called on the main application thread" (see
// extern/cef/.../include/cef_app.h), so there is exactly ONE pumping thread here
// and a fetch can NEVER be moved onto a worker thread. Overlap is instead
// cooperative: N browsers, one shared pump, a non-blocking state machine per
// request. See the pool comment in cef_headless.cpp for the --single-process
// constraints that shape it.
#pragma once

#include <memory>
#include <string>
#include <vector>

namespace ui::cef {

// Result of a headless page fetch.
struct FetchResult {
    bool        ok = false;      // navigation completed and HTML was captured
    int         status = 0;      // HTTP status of the main document (0 if unknown)
    std::string final_url;       // URL after redirects
    std::string html;            // serialized post-JS DOM of the main frame
};

// Navigate a windowless CEF browser to `url`, let it settle (JS/challenge scripts
// run), and return the rendered main-frame HTML. `profile_dir`, when non-empty,
// selects a per-site cookie jar (CefRequestContext cache_path) — the same
// per-vendor dir the interactive login window uses, so an authenticated session
// carries over; empty uses the global context.
//
// `settle_ms` is the dwell CEILING after load, not a fixed sleep. The main frame
// is re-serialized every 100ms (the same CefFrame::GetSource path the final
// capture uses) and the dwell ends early once ALL of:
//   * the serialized DOM has been byte-identical for CHUDWARE_CEF_QUIET_MS
//     (default 2000) — long enough that a page appending content in staggered
//     bursts cannot be mistaken for a finished one,
//   * CEF has reported load-end for the main frame, and
//   * at least CHUDWARE_CEF_MIN_SETTLE_MS (default 400) has elapsed.
// The ceiling is never exceeded and the early capture is the same document the
// flat dwell would have returned, so an adapter that genuinely needs the whole
// window still gets it — this can only finish sooner, never later.
// CHUDWARE_CEF_ADAPTIVE_SETTLE=0 restores the flat dwell verbatim.
FetchResult headless_fetch(const std::string& url, int settle_ms = 1200,
                           const std::string& profile_dir = "");

// ---------------------------------------------------------------------------
// Cooperative multi-fetch: the same fetch, split so ONE pumping thread can hold
// several in flight at once (the daemon's accept loop used to run each request to
// completion inline, so a 6-site fan-out paid 6x the render-settle dwell).
//
// Contract — ALL of these must be called on the CEF main thread:
//   auto job = headless_fetch_start(url, settle, profile);   // null = no slot/CEF down
//   while (!headless_fetch_poll(job)) headless_pump(10);     // interleave other jobs here
//   FetchResult r = headless_fetch_take(job);                // releases the browser
// headless_fetch_poll() never pumps and never blocks; headless_pump() advances
// EVERY live browser at once, which is what makes the overlap free. Taking an
// unfinished job aborts it (the caller's deadline path) and yields ok=false.
// ---------------------------------------------------------------------------
class FetchJob;
using FetchJobPtr = std::shared_ptr<FetchJob>;

// Reserve a browser and begin navigating. Returns null when CEF is down, the
// browser could not be created, or every pooled browser is busy (the caller
// queues and retries). `settle_ms` is the dwell CEILING — see the adaptive-settle
// note on headless_fetch above.
FetchJobPtr headless_fetch_start(const std::string& url, int settle_ms,
                                 const std::string& profile_dir);

// Advance `job` without pumping or blocking. True once it has finished (either
// outcome). Safe to call on a finished job.
bool headless_fetch_poll(const FetchJobPtr& job);

// Harvest the result and return the browser to the pool. Aborts the job if it has
// not finished. The job is dead afterwards.
FetchResult headless_fetch_take(const FetchJobPtr& job);

// Pump the shared CEF message loop for ~`ms` (CefDoMessageLoopWork in 10ms
// slices). Advances every live browser, in-flight job or not.
void headless_pump(int ms);

// Max fetches the pool will hold in flight at once (CHUDWARE_CEF_MAX_INFLIGHT,
// default 6). Resolved once.
int headless_max_inflight();

// Fetch `url`'s raw bytes (datasheet / CAD file) through CEF's network stack with
// the profile's cookies, writing them to `dest_path`. Returns true on a 2xx with a
// non-empty body written. No page render — a direct CefURLRequest. `profile_dir`
// as in headless_fetch.
bool headless_download(const std::string& url, const std::string& dest_path,
                       const std::string& profile_dir = "");

// DIAGNOSTIC: drive a real form login headlessly to OBSERVE a bot-wall verdict
// (e.g. whether an invisible-reCAPTCHA login rejects our browser). Navigates to
// `url` reusing the profile's session/cookies, lets it settle so the CSRF token +
// reCAPTCHA are ready, then runs `fill_submit_js` in the main frame via
// CefFrame::ExecuteJavaScript (render-frame JS — NOT CDP Runtime.evaluate, so the
// no-automation-tell guarantee holds) to fill + submit the login form, waits up to
// `post_wait_ms` for the response, and returns the resulting main-frame HTML — search
// it for the vendor's "reCAPTCHA verification failed" vs "invalid login" message.
// Opt-in only (daemon gates on CHUDWARE_CEF_PROBE); local diagnostics, not a product path.
FetchResult headless_login_probe(const std::string& url, const std::string& fill_submit_js,
                                 int settle_ms, int post_wait_ms,
                                 const std::string& profile_dir = "");

// Navigate to `url` under the profile's session, run caller `script` in the main
// frame (CefFrame::ExecuteJavaScript — render-frame JS, NOT CDP Runtime.evaluate,
// so the no-automation-tell guarantee holds), and wait for the script to publish a
// result into a sentinel DOM element with id `sentinel_id`. The script is expected
// to do its work (drive an SPA control, or fetch a same-origin JSON/config endpoint
// with the page's own cookies+headers) and, when finished, append/populate an
// element `<... id="sentinel_id">RESULT</...>`; this call polls the serialized DOM
// until that element appears (or `timeout_ms` elapses) and returns the final HTML —
// the caller extracts the sentinel's contents. `settle_ms` is dwell after load
// before the script runs (default 4000). Unlike login_probe this is a normal
// capability (no CHUDWARE_CEF_PROBE gate, no synthetic mouse) — it is the reusable
// primitive behind client-generated download URLs on SPA configurators (McMaster
// CAD FilePath, Misumi/cadenas series→SKU). `profile_dir` as in headless_fetch.
FetchResult headless_interact(const std::string& url, const std::string& script,
                              const std::string& sentinel_id, int settle_ms,
                              int timeout_ms, const std::string& profile_dir = "");

// One cookie to inject into a profile's jar (plain fields — no CEF/JSON types, so
// the daemon TU can parse the wire jar and this TU can build the CefCookie).
struct ImportCookie {
    std::string name, value, domain, path, same_site;  // same_site: none|lax|strict|""
    bool   secure = false, http_only = false;
    double expires = 0;                                 // epoch seconds; <=0 = session cookie
};

// Inject `cookies` into the persistent cookie jar of `profile_dir` (the daemon's
// warm profile) and flush it to disk, so a session captured in the interactive
// panel carries into subsequent headless_fetch/download. This is the daemon-side
// half of the panel→daemon login hand-off: raw profile-dir sharing across the two
// CEF processes can't share sessions (OSCrypt re-encrypts per process), so the
// panel exports its cookies and the daemon re-writes them here under its own
// crypto. Returns the number of cookies accepted. Pumps the CEF loop inline, so it
// must run on the browser-process main thread (like the other headless_* calls).
int headless_import_cookies(const std::string& profile_dir,
                            const std::vector<ImportCookie>& cookies);

// Close all pooled headless browsers (reused across fetches). Call before
// shutdown() so CefShutdown doesn't race live browsers. No-op if none are open.
void headless_close_all();

// Clean exit for the one-shot CEF CLI modes. Calls shutdown() to flush cookies,
// but arms a watchdog that hard-_exit()s the process after a few seconds if CEF's
// single-process teardown hangs (a known CEF issue) — so `--cef-fetch` and friends
// can never wedge. Does not return. The long-lived daemon path shuts down once at
// app exit and does not use this.
[[noreturn]] void shutdown_and_exit(int exit_code);

}  // namespace ui::cef
