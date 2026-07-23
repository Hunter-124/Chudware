// Optional Linux system-tray status for chudware-hostd (libappindicator3).
// No-ops on Windows, when CHUDWARE_HOSTD_HAS_TRAY is off, CHUDWARE_HOSTD_TRAY=0,
// or DISPLAY is missing.
#pragma once

#include <string>

namespace hostd {

enum class TrayStatus {
    Idle,   // ready / waiting
    Busy,   // clients or inflight work
    Error,  // engine failed / draining with error
};

// Start the tray on a dedicated GTK thread. Safe to call once; subsequent
// calls are ignored. Returns false when the tray is unavailable (env off,
// no display, missing deps) — callers treat that as success-with-no-UI.
bool trayStart(const std::string& selfBinaryPath);

// Update icon tint + hover tooltip. Thread-safe; marshalled onto the GTK loop.
void traySetStatus(TrayStatus status, const std::string& tooltip);

// Register a Quit-menu callback (e.g. server.stop). Called from the GTK thread.
void traySetQuitHandler(void (*fn)(void*), void* user);

// Tear down the indicator and join the GTK thread. Idempotent.
void trayStop();

}  // namespace hostd
