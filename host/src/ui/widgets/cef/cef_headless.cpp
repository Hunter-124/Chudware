// cef/cef_headless.cpp — see cef_headless.h. CEF-native replacement for the
// Playwright headless fetch/download. Compiled only with AI_CAD_ENABLE_CEF.
#include "cef_headless.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <thread>
#include <vector>

#include "include/cef_app.h"          // CefDoMessageLoopWork
#include "include/cef_browser.h"
#include "include/cef_client.h"
#include "include/cef_life_span_handler.h"
#include "include/cef_load_handler.h"
#include "include/cef_render_handler.h"
#include "include/cef_request.h"
#include "include/cef_request_context.h"
#include "include/cef_response.h"
#include "include/cef_string_visitor.h"
#include "include/cef_urlrequest.h"
#include "include/cef_request_context_handler.h"  // complete type for CreateContext

#include "cef_app.h"      // initialized()
#include "cef_stealth.h"  // apply_stealth()

namespace ui::cef {
namespace {

// A per-site cookie jar for `profile_dir`, or the global context when empty. The
// cache_path must live under CefSettings.root_cache_path (CEF 149) — callers pass
// a dir under cef_platform::cache_root(), the same convention CefWebView uses.
CefRefPtr<CefRequestContext> make_context(const std::string& profile_dir) {
    if (profile_dir.empty()) return CefRequestContext::GetGlobalContext();
    CefRequestContextSettings rc;
    CefString(&rc.cache_path) = profile_dir;
    CefRefPtr<CefRequestContext> ctx = CefRequestContext::CreateContext(rc, nullptr);
    return ctx ? ctx : CefRequestContext::GetGlobalContext();
}

// Pump the CEF message loop for ~`ms`, matching cef_osr_shot's default (raw
// CefDoMessageLoopWork — the headless CLI is the browser-process main thread).
void pump(int ms) {
    const int steps = ms / 10;
    for (int i = 0; i < steps; ++i) {
        CefDoMessageLoopWork();
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
}

// Collects CefFrame::GetSource's async result.
class SourceVisitor : public CefStringVisitor {
public:
    void Visit(const CefString& s) override { html_ = s.ToString(); done_ = true; }
    bool done() const { return done_; }
    const std::string& html() const { return html_; }
private:
    bool done_ = false;
    std::string html_;
    IMPLEMENT_REFCOUNTING(SourceVisitor);
};

// Windowless client that loads a page for HTML extraction. We still implement
// CefRenderHandler (OSR requires one) but never touch the pixels — GetViewRect
// just gives Blink a viewport so responsive/JS-gated content renders.
class FetchClient : public CefClient,
                    public CefRenderHandler,
                    public CefLifeSpanHandler,
                    public CefLoadHandler {
public:
    CefRefPtr<CefRenderHandler>   GetRenderHandler() override   { return this; }
    CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
    CefRefPtr<CefLoadHandler>     GetLoadHandler() override     { return this; }

    CefRefPtr<CefBrowser> browser() const { return browser_; }
    bool loaded() const { return load_end_; }
    int  status() const { return status_; }

    // CefRenderHandler — give Blink a viewport; ignore the paint.
    void GetViewRect(CefRefPtr<CefBrowser>, CefRect& rect) override { rect.Set(0, 0, 1280, 800); }
    void OnPaint(CefRefPtr<CefBrowser>, PaintElementType, const RectList&,
                 const void*, int, int) override {}

    // CefLifeSpanHandler
    void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
        browser_ = browser;
        apply_stealth(browser);  // UA/hints + document-start hardening, no CDP tell
    }

    // CefLoadHandler
    void OnLoadEnd(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame, int http) override {
        if (frame->IsMain()) { status_ = http; load_end_ = true; }
    }

private:
    bool load_end_ = false;
    int  status_ = 0;
    CefRefPtr<CefBrowser> browser_;
    IMPLEMENT_REFCOUNTING(FetchClient);
};

// CefURLRequestClient that buffers a response body for headless_download.
class DownloadClient : public CefURLRequestClient {
public:
    bool done() const { return done_; }
    const std::vector<unsigned char>& bytes() const { return bytes_; }

    void OnRequestComplete(CefRefPtr<CefURLRequest>) override { done_ = true; }
    void OnDownloadData(CefRefPtr<CefURLRequest>, const void* data, size_t n) override {
        const auto* p = static_cast<const unsigned char*>(data);
        bytes_.insert(bytes_.end(), p, p + n);
    }
    void OnUploadProgress(CefRefPtr<CefURLRequest>, int64_t, int64_t) override {}
    void OnDownloadProgress(CefRefPtr<CefURLRequest>, int64_t, int64_t) override {}
    bool GetAuthCredentials(bool, const CefString&, int, const CefString&,
                            const CefString&, CefRefPtr<CefAuthCallback>) override {
        return false;  // never auto-answer HTTP auth
    }

private:
    bool done_ = false;
    std::vector<unsigned char> bytes_;
    IMPLEMENT_REFCOUNTING(DownloadClient);
};

}  // namespace

FetchResult headless_fetch(const std::string& url, int settle_ms,
                           const std::string& profile_dir) {
    FetchResult out;
    if (!initialized()) return out;

    CefRefPtr<FetchClient> client = new FetchClient();
    CefWindowInfo window_info;
    window_info.SetAsWindowless(0);
    CefBrowserSettings settings;
    settings.windowless_frame_rate = 30;

    if (!CefBrowserHost::CreateBrowser(window_info, client, url, settings, nullptr,
                                       make_context(profile_dir)))
        return out;

    // Wait for the browser to exist and the main frame to finish loading.
    for (int i = 0; i < 500 && !(client->browser() && client->loaded()); ++i) pump(20);
    // Let JS / challenge scripts settle so GetSource sees the post-JS DOM.
    if (settle_ms > 0) pump(settle_ms);

    if (client->browser()) {
        CefRefPtr<CefFrame> frame = client->browser()->GetMainFrame();
        if (frame) {
            CefRefPtr<SourceVisitor> visitor = new SourceVisitor();
            frame->GetSource(visitor);
            for (int i = 0; i < 200 && !visitor->done(); ++i) pump(10);  // GetSource is async
            if (visitor->done()) {
                out.ok = true;
                out.html = visitor->html();
                out.status = client->status();
                out.final_url = frame->GetURL().ToString();
            }
        }
        client->browser()->GetHost()->CloseBrowser(true);
        pump(200);
    }
    return out;
}

bool headless_download(const std::string& url, const std::string& dest_path,
                       const std::string& profile_dir) {
    if (!initialized()) return false;

    CefRefPtr<CefRequest> request = CefRequest::Create();
    request->SetURL(url);
    request->SetMethod("GET");
    request->SetFlags(UR_FLAG_SKIP_CACHE | UR_FLAG_ALLOW_STORED_CREDENTIALS);

    CefRefPtr<DownloadClient> client = new DownloadClient();
    // Per-site (or global) context = the profile's cookie jar, so authenticated
    // downloads carry the login session.
    CefRefPtr<CefURLRequest> req =
        CefURLRequest::Create(request, client, make_context(profile_dir));

    for (int i = 0; i < 3000 && !client->done(); ++i) pump(10);  // up to ~30s
    if (!client->done()) return false;

    int status = 0;
    if (CefRefPtr<CefResponse> resp = req->GetResponse()) status = resp->GetStatus();
    if (status < 200 || status >= 300 || client->bytes().empty()) return false;

    std::ofstream f(dest_path, std::ios::binary);
    if (!f) return false;
    f.write(reinterpret_cast<const char*>(client->bytes().data()),
            static_cast<std::streamsize>(client->bytes().size()));
    return static_cast<bool>(f);
}

void shutdown_and_exit(int exit_code) {
    std::fflush(nullptr);
    // Watchdog: if CefShutdown wedges (single-process teardown can), hard-exit so
    // the CLI never hangs. Detached so it survives the shutdown() call below.
    std::thread([exit_code] {
        std::this_thread::sleep_for(std::chrono::seconds(5));
        std::fflush(nullptr);
        std::_Exit(exit_code);
    }).detach();
    shutdown();          // flush cookies to the profile on the clean path
    std::_Exit(exit_code);  // don't run static destructors after CEF teardown
}

}  // namespace ui::cef
