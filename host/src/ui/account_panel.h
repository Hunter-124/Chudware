#pragma once

struct AppState;

namespace ui {

// Loads and independently verifies the stored token, installs the onboarding
// seam, and starts the launch refresh. Call once after app data paths exist.
void initializeAccountPanel(AppState& state);
// Applies background login/refresh results and schedules the 12-hour check-in.
void tickAccountPanel(AppState& state);
// Settings > General account controls.
void drawAccountSettings(AppState& state);
// Persistent read-only banner and last-three-days unreachable chip.
void drawEntitlementBanner(AppState& state);
// System-browser loopback login. Non-blocking after the listener is armed.
bool beginDesktopAccountLogin(AppState& state);

}  // namespace ui
