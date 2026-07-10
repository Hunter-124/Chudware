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

namespace ui::cef {

// Apply the UA / Sec-CH-UA client-hints override (adds the "Google Chrome" brand
// that CEF's native hints omit) and the document-start JS hardening to `browser`.
// Call from OnAfterCreated. No-op on a null browser. Idempotent-safe to call once
// per browser. Mirrors services/browser/stealth.py so both browser surfaces agree.
void apply_stealth(CefRefPtr<CefBrowser> browser);

}  // namespace ui::cef
