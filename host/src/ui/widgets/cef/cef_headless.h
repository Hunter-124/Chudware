// cef/cef_headless.h — CEF-native headless page fetch + file download.
//
// This is the replacement for the Playwright headless path (services/browser):
// drive our OWN CEF Chromium directly instead of Playwright, which removes the
// automation-protocol (CDP handshake / Runtime.enable) fingerprint that bot-gates
// key on — the single biggest win per the 2026 anti-detect benchmark. HTML is read
// with CefFrame::GetSource and bytes with CefURLRequest; neither enables the
// Runtime domain, so there is no DevTools-driven tell.
//
// Both calls require initialize() to have succeeded and pump the CEF loop inline,
// so they must run on the browser-process main thread (the same context that
// --cef-shot uses). Compiled only with AI_CAD_ENABLE_CEF.
#pragma once

#include <string>

namespace ui::cef {

// Result of a headless page fetch.
struct FetchResult {
    bool        ok = false;      // navigation completed and HTML was captured
    int         status = 0;      // HTTP status of the main document (0 if unknown)
    std::string final_url;       // URL after redirects
    std::string html;            // serialized post-JS DOM of the main frame
};

// Navigate a windowless CEF browser to `url`, let it settle (JS/challenge scripts
// run), and return the rendered main-frame HTML. `settle_ms` is extra dwell after
// load for late JS (default 1200). `profile_dir`, when non-empty, selects a
// per-site cookie jar (CefRequestContext cache_path) — the same per-vendor dir the
// interactive login window uses, so an authenticated session carries over; empty
// uses the global context. Caller serializes same-dir access via profile_lock.
FetchResult headless_fetch(const std::string& url, int settle_ms = 1200,
                           const std::string& profile_dir = "");

// Fetch `url`'s raw bytes (datasheet / CAD file) through CEF's network stack with
// the profile's cookies, writing them to `dest_path`. Returns true on a 2xx with a
// non-empty body written. No page render — a direct CefURLRequest. `profile_dir`
// as in headless_fetch.
bool headless_download(const std::string& url, const std::string& dest_path,
                       const std::string& profile_dir = "");

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
