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
// Read-only entitlement notice. This is RESERVED CHROME, not an overlay: app.cpp measures it
// with entitlementBannerHeight(), folds that height into dockTopY before ui::setOverlayBand(),
// and paints the strip itself between the ribbon and the dockspace. It was previously a floating
// decoration-less window anchored inside the overlay band, which meant it covered whatever
// happened to be at the top-left of the active workspace — on the PLM hub that was the project
// breadcrumb, the Refresh button and the search field. Reserving the height is the only fix that
// holds for EVERY workspace, including ones with no pinned columns to hide behind.
//
// entitlementBannerHeight() returns 0 when the account is in good standing, and must be callable
// before any panel has drawn. drawEntitlementBanner() paints the strip at the given rect.
float entitlementBannerHeight(const AppState& state);
void  drawEntitlementBanner(AppState& state, float x, float y, float w, float h);
// The "subscription check unavailable" grace chip stays a bottom-right overlay: it is transient,
// small, and anchored to a corner nothing else claims. Call once per frame after the panels.
void  drawEntitlementGraceChip(AppState& state);
// System-browser loopback login. Non-blocking after the listener is armed.
bool beginDesktopAccountLogin(AppState& state);
// Forgets this device's stored subscription session (sealed token, subject, signed security
// policy) and drops the verdict of any refresh/login still in flight. The device authorization
// key survives — it is identity, not session state. Read-only mode resumes immediately.
void signOutDesktopAccount(AppState& state);

}  // namespace ui
