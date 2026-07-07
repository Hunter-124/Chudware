// ui::IWebViewBackend — the embedded-browser seam (Parts Library vendor sign-in).
//
// The vendor sign-in panel (parts_browser_panel.cpp) talks to the browser ONLY
// through this interface, so the rendering engine underneath can be swapped
// without touching any UI code. Two implementations:
//
//   * ScreencastWebView (web_view.h) — the shipping default. A worker-side
//     headless Chromium (Playwright, services/browser) streamed as JPEG frames
//     over the browser_embed_* RPCs into a GL texture, with synthetic input
//     forwarded back. Zero extra build deps; works today. Limitation: native
//     OS UI (native <select> popups, the desktop clipboard, password-manager
//     autofill) never renders into the frame stream.
//
//   * CefWebView (cef/cef_web_view.h, built only with AI_CAD_ENABLE_CEF) — an
//     in-process Chromium Embedded Framework off-screen render straight to a GL
//     texture. CEF composites its own <select>/popup layer and bridges the OS
//     clipboard, so those interactions work. See docs/cef_webview.md for the
//     architecture, the cookie-store interop contract with the Playwright
//     scraper, and the C++/Python boundary.
//
// Threading contract is identical for every backend: GL-thread-only; all RPC is
// async via st.rpc; texture create/update happens only on the GL thread (see
// MEMORY gl-thread-rpc-freeze). CEF's OnPaint hands buffers off a CEF thread —
// its backend copies them and uploads on the GL thread like the screencast one.
#pragma once

#include <memory>
#include <string>

#include "imgui.h"

struct AppState;

namespace ui {

class IWebViewBackend {
public:
    virtual ~IWebViewBackend() = default;

    // Begin a session: a vendor key (opens its login page) or a full URL.
    virtual void open(AppState& st, const std::string& siteOrUrl, bool isUrl) = 0;
    // Adopt an existing worker-side session (the login wizard's embed_id). Not
    // owned: close() will not stop it on the worker. CEF has no separate
    // worker session, so its implementation opens the site's login URL instead.
    virtual void attach(AppState& st, const std::string& embedId) = 0;
    // Stop the session (only when owned), free the texture, reset state.
    virtual void close(AppState& st) = 0;
    // Frame + input polling — call once per frame while the widget is visible.
    virtual void update(AppState& st) = 0;
    // Draw the letterboxed page + forward input inside the given content size.
    // Returns true while a session is alive (starting counts).
    virtual bool draw(AppState& st, ImVec2 avail) = 0;

    virtual const std::string& url() const = 0;
    virtual const std::string& loginState() const = 0;  // unknown|login_page|logged_in|challenge
    virtual bool active() const = 0;
    virtual bool starting() const = 0;
    virtual const std::string& lastError() const = 0;

    // True when the backend adopts the login wizard's worker-side embed via
    // attach(embed_id) (the ScreencastWebView default). CEF renders in-process and
    // has no worker embed, so it returns false and the panel drives it with
    // open(site) instead. Keeps the wizard flow backend-agnostic.
    virtual bool usesWorkerEmbed() const { return true; }
};

// Backend selection, single source of truth. Returns the in-process CefWebView —
// the one and only embedded-browser backend — whenever built with
// AI_CAD_ENABLE_CEF (the default on every OS; no runtime toggle). Only an
// SDK-less build (AI_CAD_ENABLE_CEF=OFF, e.g. CI without the CEF binary dist)
// falls back to the inert NullWebView. Call sites stay backend-agnostic:
// `auto web = ui::makeWebView();  web->open(...);`.
std::unique_ptr<IWebViewBackend> makeWebView();

}  // namespace ui
