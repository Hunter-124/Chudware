// First-run onboarding (T-Q1): a one-time consent + sample-project offer, plus the
// "Repair Runtime" trigger the worker's missing-runtime error message already promises
// (services/worker/worker_client.cpp) but nothing wired up until now. Reads/writes the SAME
// %APPDATA%/AI-CAD/config.json the Python side resolves telemetry_consent from
// (services/agent/config.py:consent_tier) — one config file, one source of truth.
#pragma once

namespace app { struct AppState; }

namespace ui {

// Call once per frame (application.cpp). Opens the dialog the first time config.json has no
// recorded telemetry_consent choice; a no-op every frame after (or forever, once dismissed).
void maybeOpenFirstRun(app::AppState& s);

// Body: BeginPopupModal/EndPopupModal internally (unlike other draw*Pane functions here, this
// one owns its own popup lifecycle since it must block normal interaction until dismissed).
void drawFirstRunDialog(app::AppState& s);

// Kick off runtime provisioning (force=false, "AI-CAD ▸ Repair runtime" style manual
// re-provision) on a detached background thread; never blocks the caller. Posts a start toast
// immediately and a success/failure toast on completion (ui::postToast, thread-safe). Calling
// again while one is already in flight posts a Warn toast and does nothing else.
void triggerRuntimeRepair(bool force);

// True while a triggerRuntimeRepair() is in flight — settings UI uses this to disable the
// button instead of letting the user queue a second concurrent provision.
bool runtimeRepairInProgress();

// Telemetry consent tier as an index into {"off","meta","full"} (matches
// services/agent/config.py's consent_tier() enum) — read fresh from config.json each call
// (settings panel), and the mutator that persists a choice made outside the first-run dialog.
int  currentConsentIndex();
void setConsentIndex(int idx);
const char* consentLabel(int idx);

}  // namespace ui
