// Optional system-tray status for chudware-hostd: the Windows notification area, or
// Linux libappindicator3. No-ops when CHUDWARE_HOSTD_TRAY=0, on Linux when
// CHUDWARE_HOSTD_HAS_TRAY is off or DISPLAY is missing, and on other platforms.
#pragma once

#include <string>

namespace hostd {

enum class TrayStatus {
    Idle,   // ready / waiting
    Busy,   // clients or inflight work
    Error,  // engine failed / draining with error
};

// Start the tray on a dedicated thread (Win32 message loop or GTK). Safe to call
// once; subsequent calls are ignored. Returns false when the tray is unavailable
// (env off, no display, missing deps) — callers treat that as success-with-no-UI.
bool trayStart(const std::string& selfBinaryPath);

// Update status + hover tooltip (Linux also tints the icon). Thread-safe; marshalled
// onto the tray thread.
void traySetStatus(TrayStatus status, const std::string& tooltip);

// Register a Quit-menu callback (e.g. server.stop). Called from the tray thread.
void traySetQuitHandler(void (*fn)(void*), void* user);

// Tear down the icon and join the tray thread. Idempotent.
void trayStop();

}  // namespace hostd
