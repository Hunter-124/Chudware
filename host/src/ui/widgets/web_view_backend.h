// ui::IWebViewBackend — the embedded-browser seam (Parts Library vendor sign-in).
//
// The vendor sign-in panel (parts_browser_panel.cpp) talks to the browser ONLY
// through this interface. CefWebView (cef/cef_web_view.h) is the sole shipping
// browser backend; ScreencastWebView was deleted when CEF landed on 2026-07-07.
// CHUDWARE_ENABLE_CEF defaults ON for Linux and Windows. macOS and Web/WASM
// force it OFF and use NullWebView, which honestly reports that no embedded
// browser is available.
//
// CEF is load-bearing beyond the in-app browser: a separate headless CEF daemon
// provides the fetch primitive used by the 100+ Parts Library source adapters.
// See docs/design/cef_webview.md for the architecture and session interop.
//
// Threading contract: GL-thread-only; all RPC is async via st.rpc; texture
// create/update happens only on the GL thread (see MEMORY gl-thread-rpc-freeze).
// CEF's OnPaint hands buffers off a CEF thread; the backend copies them and
// uploads them on the GL thread.
#pragma once

#include <functional>
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

    // Hand the live CEF session over to the headless fetch daemon so downloads
    // reuse it: export the backend's cookie jar for `site` and stage it via the
    // browser_session_import RPC (which also stamps the login as verified).
    // `done(ok)` fires on the GL thread. NullWebView has nothing to export, so
    // the default reports success without doing anything.
    virtual void exportSession(AppState& st, const std::string& site,
                               std::function<void(bool)> done) {
        (void)st; (void)site;
        if (done) done(true);
    }

    // Browser-chrome navigation (URL bar / refresh / back / forward). The panel
    // shows these controls only when the backend supports them; NullWebView uses
    // the inert defaults. navigate() takes a full URL.
    virtual void navigate(AppState& st, const std::string& url) { open(st, url, /*isUrl=*/true); }
    virtual void reload(AppState& st) { (void)st; }
    virtual void goBack(AppState& st) { (void)st; }
    virtual void goForward(AppState& st) { (void)st; }
    virtual bool canGoBack() const { return false; }
    virtual bool canGoForward() const { return false; }
    // True when this backend exposes the URL-bar / back-forward chrome above.
    virtual bool hasNavChrome() const { return false; }

    // Legacy capability query retained by the wizard interface. CefWebView
    // overrides this to false; unavailable NullWebView never starts a session.
    virtual bool usesWorkerEmbed() const { return true; }

    // False when the backend cannot host a browser (e.g. CEF cold-init failed).
    // Wizard falls back to headed/native desktop login instead of crashing.
    virtual bool available() const { return true; }

    // Wipe cookies / session storage so the next OAuth login is not auto-signed
    // into the previous account (multi-account add). CEF deletes the request-
    // context cookie jar; default is a no-op. `done` fires on the GL/UI thread
    // when the wipe finishes (or immediately for backends with nothing to clear).
    virtual void clearSession(AppState& st, std::function<void()> done = nullptr) {
        (void)st;
        if (done) done();
    }
};

// Backend selection, single source of truth. Returns CefWebView when
// CHUDWARE_ENABLE_CEF is enabled (the Linux/Windows default), otherwise the
// inert NullWebView (macOS, Web/WASM, or an explicit SDK-less native build).
// There is no runtime toggle. Call sites stay backend-agnostic:
// `auto web = ui::makeWebView();  web->open(...);`.
std::unique_ptr<IWebViewBackend> makeWebView();

}  // namespace ui
