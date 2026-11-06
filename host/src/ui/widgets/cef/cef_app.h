// cef/cef_app.h — CEF process lifecycle + the browser-process CefApp.
//
// Compiled only with CHUDWARE_ENABLE_CEF (design: docs/design/cef_webview.md).
//
// Two entry points, both of which MUST run CefExecuteProcess at the very top of
// their process's main():
//   * cef::execute_subprocess() — called first in BOTH Chudware's main() and the
//     helper's main(). In a helper (render/GPU/utility) process it blocks and
//     returns the process exit code; in the browser process it returns -1 and
//     execution continues.
//   * cef::initialize()/shutdown() — browser process only, wraps CefInitialize
//     with the shared cookie/OSCrypt + windowless settings.
#pragma once

namespace ui::cef {

// Cache argc/argv for a later CefMainArgs (the browser process uses a separate
// helper exe, so it must NOT call execute_subprocess — just stash the args).
void stash_args(int argc, char** argv);

// Run at the top of the HELPER's main() only. Returns the subprocess exit code
// (the helper returns it immediately). Not called by the browser process.
int execute_subprocess(int argc, char** argv);

// Browser process only. Initialises CEF with windowless rendering + the cookie
// crypto settings that let the Playwright scraper reuse CEF's sessions. Reuses
// the argc/argv captured by stash_args (call that first). Returns false (and
// init_failed()==true) if CEF failed to start; CefWebView::available() then
// reports false so the sign-in wizard falls back to desktop login. Safe to call
// once; also the lazy-init entry — first CefWebView::open() calls this
// if main() did not (default). Force early init with CHUDWARE_CEF_EARLY=1.
// persistent_global is for the daemon's private cache root, not the GUI.
bool initialize(bool persistent_global = false);

// True after a successful initialize(). CefWebView::processReady() forwards this.
// Note: after lazy-init, processReady() is false until the first web surface opens.
bool initialized();

// True when initialize() was attempted and returned false. Never call
// CefInitialize again in this process (CEF 149 SIGTRAP). Wizard uses this to
// fall back to desktop/native login instead of crashing on re-init.
bool init_failed();

// Call once per GLFW frame. Pumps only when CEF's deadline has arrived, except
// while CreateBrowser is in flight (continuous pumping avoids stuck "Loading").
// No-op until initialize(). Returns true if it pumped this call.
bool pump_if_due();

// Bound the GUI loop's idle wait: -1 means no work pending, 0 means pump now,
// otherwise the number of milliseconds until the next scheduled pump.
int msUntilDue();

// GUI installs anim::wakeLoop; standalone CEF helper leaves this unset.
void setWakeCallback(void (*wake)());

// Live off-screen browser bookkeeping, bumped by OnAfterCreated / OnBeforeClose.
void note_browser_opened();
void note_browser_closed();

// Browser-creation-in-flight refcount. CefBrowserHost::CreateBrowser is async:
// it returns true immediately but OnAfterCreated fires only after several message-
// loop iterations. In that window g_live_browsers is still 0, and the external-
// pump deadline model can starve the loop mid-creation → OnAfterCreated never
// fires → the panel hangs on "Loading" forever (root cause of the vendor-browser
// bug). Bracket the CreateBrowser call with these so pump_if_due pumps EVERY frame
// until the browser is created (note_browser_opened) or the create fails.
void note_browser_creating();        // just before CreateBrowser
void note_browser_create_failed();   // CreateBrowser returned false (no OnAfterCreated will come)

// Browser process only, at app exit, after all CefWebViews are destroyed.
void shutdown();

}  // namespace ui::cef
