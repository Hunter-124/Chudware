// ui::CefWebView — in-process CEF off-screen browser backend (IWebViewBackend).
//
// SCAFFOLDING for phase 2 of docs/cef_webview.md. Compiled ONLY when the build
// is configured with AI_CAD_ENABLE_CEF (host/cmake/cef.cmake); the default build
// never sees this header. Not yet verified against a live CEF SDK — the OSR
// paint/input/cookie bodies in cef_web_view.cpp carry TODO(cef) markers where a
// real build/verify loop is required.
//
// Contract & threading identical to ScreencastWebView: GL-thread-only public
// API; CEF's OnPaint delivers BGRA buffers on a CEF thread, which we copy under
// a mutex and upload to the GL texture on the GL thread in draw().
#pragma once

#include "ui/widgets/web_view_backend.h"

namespace ui {

class CefWebView final : public IWebViewBackend {
public:
    // True when the CEF runtime initialised for this process (see cef_app).
    // makeWebView() checks this before choosing the CEF backend so any CEF
    // startup failure degrades to the screencast backend instead of a black box.
    static bool processReady();

    CefWebView();
    ~CefWebView() override;
    CefWebView(const CefWebView&)            = delete;
    CefWebView& operator=(const CefWebView&) = delete;

    void open(AppState& st, const std::string& siteOrUrl, bool isUrl) override;
    void attach(AppState& st, const std::string& embedId) override;  // no-op (no worker embed)
    void close(AppState& st) override;
    void update(AppState& st) override;
    bool draw(AppState& st, ImVec2 avail) override;

    const std::string& url() const override;
    const std::string& loginState() const override;
    bool active() const override;
    bool starting() const override;
    const std::string& lastError() const override;
    // CEF renders in-process — it has no worker-side embed to attach to, so the
    // panel opens the current wizard site's URL via open() instead of attach().
    bool usesWorkerEmbed() const override;
    // IWebViewBackend: false when cold CefInitialize failed (wizard falls back).
    bool available() const override;

    // Browser-chrome navigation (URL bar / refresh / back-forward), driven on the
    // GL/UI thread == TID_UI.
    void reload(AppState& st) override;
    void goBack(AppState& st) override;
    void goForward(AppState& st) override;
    bool canGoBack() const override;
    bool canGoForward() const override;
    bool hasNavChrome() const override { return true; }

    // Hand the live CEF session to the worker: visit the per-site request
    // context's cookie jar and stage it via browser_session_import so headless
    // Playwright (browser_fetch/download) reuses the sign-in. GL/UI thread.
    void exportSession(AppState& st, const std::string& site,
                       std::function<void(bool)> done) override;

private:
    void forwardInput(ImVec2 img0, float scale, ImVec2 imgSize);  // GL/UI thread

    struct Shared;                  // plain CEF-callback ↔ widget handoff (shared_ptr)
    class  Client;                  // CEF-refcounted CefClient (owned by CefRefPtr only)
    struct Impl;                    // widget-owned: Client ref + GL texture
    std::unique_ptr<Impl> impl_;
};

}  // namespace ui
