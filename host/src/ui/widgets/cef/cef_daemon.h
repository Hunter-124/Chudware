// cef/cef_daemon.h — the lazily-spawned, idle-exiting CEF host daemon.
//
// Per the migration plan, ONE process owns all CEF. This daemon is that owner for
// the headless path (Phase 2a): it initialises CEF once, listens on a local
// chudware_ipc socket, and serves headless fetch/download requests from the Python
// scraper (services/browser/cef_client.py). It idle-exits after `idle_secs` with
// no request so Chromium is not resident when parts search is not running. The
// interactive panel is migrated onto the same daemon in Phase 2b.
//
// Compiled only with CHUDWARE_ENABLE_CEF. Entry: `Chudware --cef-daemon <sock> [idle]`.
#pragma once

#include <string>

namespace ui::cef {

// Initialise CEF, listen on `sock_path`, and serve fetch/download until an idle
// timeout (`idle_secs`, 0 = never idle-exit) or an explicit "shutdown" request.
// Returns a process exit code. Runs the CEF message loop inline on this thread.
int run_cef_daemon(const std::string& sock_path, int idle_secs);

}  // namespace ui::cef
