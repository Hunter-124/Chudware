// cef/cef_stealth.h — one place that applies our fingerprint hardening to a CEF
// browser, so the headless fetch path, the --cef-shot self-test, and (eventually)
// the interactive panel all present ONE consistent wire fingerprint.
//
// Everything here uses CEF's IN-PROCESS DevTools invocation (ExecuteDevToolsMethod)
// for Network.setUserAgentOverride + Page.addScriptToEvaluateOnNewDocument only.
// Those two methods do NOT open a remote-debugging socket and do NOT enable the
// Runtime domain, so they carry none of the `Runtime.enable` automation-protocol
// tell that Playwright/Puppeteer leak (see docs + services/browser/stealth.py).
// HTML is extracted natively via CefFrame::GetSource — never Runtime.evaluate —
// precisely to keep the Runtime domain disabled.
//
// Compiled only with AI_CAD_ENABLE_CEF.
#pragma once

#include "include/cef_browser.h"
#include "include/cef_registration.h"

namespace ui::cef {

// Apply the UA / Sec-CH-UA client-hints override (adds the "Google Chrome" brand
// that CEF's native hints omit) and the document-start JS hardening to `browser`.
// Call from OnAfterCreated, BEFORE navigating to the real URL.
//
// Returns a DevTools-observer registration that the CALLER MUST KEEP ALIVE (store
// it as a member for the browser's lifetime). ExecuteDevToolsMethod is a silent
// no-op unless a DevTools message observer is attached to the browser — attaching
// one is what wires the in-process CDP pipe. Dropping the returned registration
// detaches the agent and the hardening stops taking effect on later navigations.
// No-op (returns null) on a null browser. Mirrors services/browser/stealth.py.
CefRefPtr<CefRegistration> apply_stealth(CefRefPtr<CefBrowser> browser);

// The document-start fingerprint-hardening JS (window.chrome, plugins, languages,
// webdriver, deviceMemory cap, WebGL vendor/renderer spoof). Injected from the
// render process at V8-context creation (CefRenderProcessHandler::OnContextCreated
// in cef_app.cpp) so it runs BEFORE any page script with zero CDP — the robust,
// no-Runtime.enable path. Mirrors services/browser/stealth.INIT_SCRIPT.
const char* stealth_init_script();

}  // namespace ui::cef
