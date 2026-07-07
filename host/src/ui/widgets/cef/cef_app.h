// cef/cef_app.h — CEF process lifecycle + the browser-process CefApp.
//
// SCAFFOLDING (docs/cef_webview.md phase 2); compiled only with AI_CAD_ENABLE_CEF.
//
// Two entry points, both of which MUST run CefExecuteProcess at the very top of
// their process's main():
//   * cef::execute_subprocess() — called first in BOTH AI-CAD's main() and the
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
// the argc/argv captured by execute_subprocess (call that first). Returns false
// (and available()==false) if CEF failed to start, so callers fall back to the
// screencast backend. Safe to call once.
bool initialize();

// True after a successful initialize(). CefWebView::available() forwards this.
bool initialized();

// External-pump model: call once per GLFW frame. Runs CefDoMessageLoopWork()
// only when CEF's scheduled deadline (OnScheduleMessagePumpWork) is due, so OSR
// isn't starved or busy-spun. No-op until initialize(). Returns true if it
// pumped this call.
bool pump_if_due();

// Browser process only, at app exit, after all CefWebViews are destroyed.
void shutdown();

}  // namespace ui::cef
