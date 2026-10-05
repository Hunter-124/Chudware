// cef/cef_daemon.h — the lazily-spawned, idle-exiting CEF host daemon.
//
// This daemon owns CEF for the headless path: it initialises CEF once, listens on
// a local chudware_ipc socket, and serves headless fetch/download requests from the
// Python scraper (services/browser/cef_client.py). It idle-exits after `idle_secs`
// with no request so Chromium is not resident when parts search is not running.
// The interactive panel stays in the GUI process (CefWebView) with its own
// profile; its logins reach the daemon through "import_session".
//
// Compiled only with CHUDWARE_ENABLE_CEF. Entry: `Chudware --cef-daemon <sock> [idle]`.
#pragma once

#include <string>

namespace ui::cef {

// Initialise CEF, listen on `sock_path`, and serve fetch/download until an idle
// timeout (`idle_secs`, 0 = never idle-exit) or an explicit "shutdown" request.
// Returns a process exit code. Runs the CEF message loop inline on this thread —
// CefDoMessageLoopWork() may only be called here (include/cef_app.h), so there is
// no worker-thread pool and never will be.
//
// Fetches nonetheless OVERLAP: the accept loop no longer runs a request to
// completion inline, it hands each one a browser of its own and lets ONE shared
// pump advance them all, answering each connection when ITS page has settled.
// Up to CHUDWARE_CEF_MAX_INFLIGHT (default 6) at a time, the rest queued. Without
// this a six-vendor parts fan-out paid six full render-settle dwells back to back
// (measured 51.4s; 9.2s with the overlap, 3.3s with the adaptive settle too).
int run_cef_daemon(const std::string& sock_path, int idle_secs);

}  // namespace ui::cef
