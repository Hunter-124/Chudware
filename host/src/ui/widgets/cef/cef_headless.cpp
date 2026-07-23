// cef/cef_headless.cpp — see cef_headless.h. CEF-native replacement for the
// Playwright headless fetch/download. Compiled only with CHUDWARE_ENABLE_CEF.
#include "cef_headless.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <map>
#include <memory>
#include <thread>
#include <vector>

#include "include/cef_app.h"          // CefDoMessageLoopWork
#include "include/cef_browser.h"
#include "include/cef_client.h"
#include "include/cef_cookie.h"       // cookie import (panel→daemon session hand-off)
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
// Windowless client for HTML extraction. Created once at about:blank and REUSED
// across fetches via navigate() — creating/closing a fresh browser per fetch
// SIGTRAPs on the 2nd create in single-process mode (Chromium can't spawn a 2nd
// renderer; MEMORY cef-gpu-thread-sigtrap-nvidia). Seeding at about:blank also
// guarantees the render-process stealth injection is live before the first real
// document loads. We implement CefRenderHandler (OSR needs one) but ignore paints.
class FetchClient : public CefClient,
                    public CefRenderHandler,
                    public CefLifeSpanHandler,
                    public CefLoadHandler {
public:
    CefRefPtr<CefRenderHandler>   GetRenderHandler() override   { return this; }
    CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
    CefRefPtr<CefLoadHandler>     GetLoadHandler() override     { return this; }

    CefRefPtr<CefBrowser> browser() const { return browser_; }
    bool created() const { return created_; }
    bool loaded() const { return load_end_; }
    int  status() const { return status_; }

    // Point the reused browser at a new URL and arm a fresh load wait.
    void navigate(const std::string& url) {
        load_end_ = false;
        status_ = 0;
        if (browser_) if (CefRefPtr<CefFrame> f = browser_->GetMainFrame()) f->LoadURL(url);
    }

    // DIAGNOSTIC (login_probe): re-arm the load wait to catch a form-submit POST nav,
    // and run JS in the main frame via ExecuteJavaScript (no CDP/Runtime tell).
    void armLoadWait() { load_end_ = false; status_ = 0; }
    void runJs(const std::string& js) {
        if (browser_) if (CefRefPtr<CefFrame> f = browser_->GetMainFrame())
            f->ExecuteJavaScript(js, f->GetURL(), 0);
    }

    // CefRenderHandler — give Blink a viewport; ignore the paint.
    void GetViewRect(CefRefPtr<CefBrowser>, CefRect& rect) override { rect.Set(0, 0, 1280, 800); }
    void OnPaint(CefRefPtr<CefBrowser>, PaintElementType, const RectList&,
                 const void*, int, int) override {}

    // CefLifeSpanHandler
    void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
        browser_ = browser;
        stealth_reg_ = apply_stealth(browser);  // UA/hints, no CDP tell (JS via OnContextCreated)
        created_ = true;
    }

    // CefLoadHandler — only the real navigation counts, not the about:blank seed.
    void OnLoadEnd(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame, int http) override {
        if (frame->IsMain() && frame->GetURL().ToString().rfind("about:", 0) != 0) {
            status_ = http;
            load_end_ = true;
        }
    }

private:
    bool created_ = false;
    bool load_end_ = false;
    int  status_ = 0;
    CefRefPtr<CefBrowser> browser_;
    CefRefPtr<CefRegistration> stealth_reg_;  // keeps the DevTools agent attached
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

// Persistent OSR browsers, one per profile dir, REUSED across fetches (see
// FetchClient). Never create/close per request — the 2nd create SIGTRAPs in
// single-process mode. Single-threaded access (the daemon serves one request at a
// time), so no locking. Closed by headless_close_all() before CefShutdown.
std::map<std::string, CefRefPtr<FetchClient>>& browser_pool() {
    static std::map<std::string, CefRefPtr<FetchClient>> pool;
    return pool;
}

FetchResult headless_fetch(const std::string& url, int settle_ms,
                           const std::string& profile_dir) {
    FetchResult out;
    if (!initialized()) return out;

    CefRefPtr<FetchClient>& client = browser_pool()[profile_dir];
    if (!client) {
        client = new FetchClient();
        CefWindowInfo window_info;
        window_info.SetAsWindowless(0);
        CefBrowserSettings settings;
        settings.windowless_frame_rate = 30;
        // Seed at about:blank so the render-process stealth injection is live
        // before the first real navigation.
        if (!CefBrowserHost::CreateBrowser(window_info, client, "about:blank", settings,
                                           nullptr, make_context(profile_dir))) {
            browser_pool().erase(profile_dir);
            return out;
        }
        for (int i = 0; i < 500 && !client->created(); ++i) pump(20);
        if (!client->created()) { browser_pool().erase(profile_dir); return out; }
    }

    client->navigate(url);
    for (int i = 0; i < 500 && !client->loaded(); ++i) pump(20);
    // If the navigation never completed, DO NOT return the reused browser's stale
    // previous-page DOM as success — fail so the caller degrades/retries.
    if (!client->loaded()) return out;
    if (settle_ms > 0) pump(settle_ms);  // let JS / challenge scripts settle

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
    }
    return out;
}

FetchResult headless_login_probe(const std::string& url, const std::string& fill_submit_js,
                                 int settle_ms, int post_wait_ms,
                                 const std::string& profile_dir) {
    FetchResult out;
    if (!initialized()) return out;

    CefRefPtr<FetchClient>& client = browser_pool()[profile_dir];
    if (!client) {
        client = new FetchClient();
        CefWindowInfo window_info;
        window_info.SetAsWindowless(0);
        CefBrowserSettings settings;
        settings.windowless_frame_rate = 30;
        if (!CefBrowserHost::CreateBrowser(window_info, client, "about:blank", settings,
                                           nullptr, make_context(profile_dir))) {
            browser_pool().erase(profile_dir);
            return out;
        }
        for (int i = 0; i < 500 && !client->created(); ++i) pump(20);
        if (!client->created()) { browser_pool().erase(profile_dir); return out; }
    }

    client->navigate(url);
    for (int i = 0; i < 500 && !client->loaded(); ++i) pump(20);
    if (!client->loaded()) return out;
    if (settle_ms > 0) pump(settle_ms);      // let form_key + reCAPTCHA arm

    // Inject TRUSTED human interaction before submitting: reCAPTCHA weights mouse
    // movement/timing heavily, and JS-dispatched events are isTrusted=false (an
    // instant bot tell). SendMouseMoveEvent goes through the real input pipeline
    // (isTrusted=true), so a wandering path + dwell gives a faithful interaction
    // signal — without this the probe fails on zero-interaction regardless of browser.
    if (CefRefPtr<CefBrowser> b = client->browser()) {
        CefRefPtr<CefBrowserHost> host = b->GetHost();
        // Mark the windowless browser VISIBLE + FOCUSED. An OSR browser otherwise
        // reports document.visibilityState='hidden' / hasFocus()=false, which
        // reCAPTCHA penalises as a backgrounded/bot page.
        host->WasHidden(false);
        host->SetFocus(true);
        pump(200);
        for (int i = 0; i < 24; ++i) {
            CefMouseEvent me;
            me.x = 120 + (i * 47) % 1000;
            me.y = 90 + (i * 71) % 600;
            me.modifiers = 0;
            host->SendMouseMoveEvent(me, /*mouseLeave=*/false);
            pump(40);
        }
    }
    client->armLoadWait();                   // watch for the submit's POST-response nav
    client->runJs(fill_submit_js);           // fill + submit → grecaptcha.execute() fires
    // A clean pass navigates to the response page; a hard challenge shows an iframe
    // with NO nav — so wait for either the nav OR post_wait_ms, then capture anyway.
    const int steps = post_wait_ms / 20;
    for (int i = 0; i < steps && !client->loaded(); ++i) pump(20);
    pump(1500);                              // let the result page's messages render

    if (client->browser()) {
        CefRefPtr<CefFrame> frame = client->browser()->GetMainFrame();
        if (frame) {
            CefRefPtr<SourceVisitor> visitor = new SourceVisitor();
            frame->GetSource(visitor);
            for (int i = 0; i < 200 && !visitor->done(); ++i) pump(10);
            if (visitor->done()) {
                out.ok = true;
                out.html = visitor->html();
                out.status = client->status();
                out.final_url = frame->GetURL().ToString();
            }
        }
    }
    return out;
}

namespace {
// Synchronously (pump-until-done) serialize the main frame's post-JS DOM.
std::string get_source_sync(const CefRefPtr<FetchClient>& client) {
    if (!client || !client->browser()) return {};
    CefRefPtr<CefFrame> frame = client->browser()->GetMainFrame();
    if (!frame) return {};
    CefRefPtr<SourceVisitor> visitor = new SourceVisitor();
    frame->GetSource(visitor);
    for (int i = 0; i < 200 && !visitor->done(); ++i) pump(10);
    return visitor->done() ? visitor->html() : std::string{};
}
}  // namespace

FetchResult headless_interact(const std::string& url, const std::string& script,
                              const std::string& sentinel_id, int settle_ms,
                              int timeout_ms, const std::string& profile_dir) {
    FetchResult out;
    if (!initialized()) return out;

    CefRefPtr<FetchClient>& client = browser_pool()[profile_dir];
    if (!client) {
        client = new FetchClient();
        CefWindowInfo window_info;
        window_info.SetAsWindowless(0);
        CefBrowserSettings settings;
        settings.windowless_frame_rate = 30;
        if (!CefBrowserHost::CreateBrowser(window_info, client, "about:blank", settings,
                                           nullptr, make_context(profile_dir))) {
            browser_pool().erase(profile_dir);
            return out;
        }
        for (int i = 0; i < 500 && !client->created(); ++i) pump(20);
        if (!client->created()) { browser_pool().erase(profile_dir); return out; }
    }

    client->navigate(url);
    for (int i = 0; i < 500 && !client->loaded(); ++i) pump(20);
    if (!client->loaded()) return out;

    // Mark the windowless browser visible/focused so visibility-gated SPA code runs
    // (document.visibilityState='visible'), then let the page settle before the
    // caller script fires. No synthetic mouse — this is not a bot-score probe.
    if (CefRefPtr<CefBrowser> b = client->browser()) {
        b->GetHost()->WasHidden(false);
        b->GetHost()->SetFocus(true);
    }
    if (settle_ms > 0) pump(settle_ms);

    client->runJs(script);

    // Poll the serialized DOM until the caller's sentinel element appears (its
    // presence is the "script finished" signal — the script must create/populate it
    // only when its result is ready), or the timeout elapses. Async work in the
    // script (fetch to a config endpoint) completes within this window.
    const std::string needle = "id=\"" + sentinel_id + "\"";
    std::string html;
    const int budget = timeout_ms > 0 ? timeout_ms : 12000;
    const int slices = budget / 300;
    for (int i = 0; i < slices; ++i) {
        pump(300);
        html = get_source_sync(client);
        if (!sentinel_id.empty() && html.find(needle) != std::string::npos) break;
    }
    if (html.empty()) html = get_source_sync(client);

    if (client->browser()) {
        out.ok = true;
        out.html = html;
        out.status = client->status();
        if (CefRefPtr<CefFrame> f = client->browser()->GetMainFrame())
            out.final_url = f->GetURL().ToString();
    }
    return out;
}

void headless_close_all() {
    for (auto& [profile, client] : browser_pool())
        if (client && client->browser()) client->browser()->GetHost()->CloseBrowser(true);
    for (int i = 0; i < 50; ++i) pump(10);  // let OnBeforeClose settle
    browser_pool().clear();
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

namespace {
// Counts SetCookie completions so headless_import_cookies can pump until the async
// writes land (single-threaded pump, so plain shared ints are safe).
class SetCookieCounter : public CefSetCookieCallback {
public:
    SetCookieCounter(std::shared_ptr<int> ok, std::shared_ptr<int> done)
        : ok_(std::move(ok)), done_(std::move(done)) {}
    void OnComplete(bool success) override { if (success) ++*ok_; ++*done_; }
private:
    std::shared_ptr<int> ok_, done_;
    IMPLEMENT_REFCOUNTING(SetCookieCounter);
};
// Fires when FlushStore has persisted the jar to disk.
class FlushWaiter : public CefCompletionCallback {
public:
    explicit FlushWaiter(std::shared_ptr<bool> done) : done_(std::move(done)) {}
    void OnComplete() override { *done_ = true; }
private:
    std::shared_ptr<bool> done_;
    IMPLEMENT_REFCOUNTING(FlushWaiter);
};
}  // namespace

int headless_import_cookies(const std::string& profile_dir,
                            const std::vector<ImportCookie>& cookies) {
    if (!initialized() || cookies.empty()) return 0;
    CefRefPtr<CefRequestContext> ctx = make_context(profile_dir);
    CefRefPtr<CefCookieManager> mgr = ctx ? ctx->GetCookieManager(nullptr) : nullptr;
    if (!mgr) return 0;

    auto ok = std::make_shared<int>(0);
    auto done = std::make_shared<int>(0);
    int posted = 0;
    for (const auto& c : cookies) {
        if (c.name.empty() || c.domain.empty()) continue;
        CefCookie ck;
        CefString(&ck.name).FromString(c.name);
        CefString(&ck.value).FromString(c.value);
        CefString(&ck.domain).FromString(c.domain);
        CefString(&ck.path).FromString(c.path.empty() ? std::string("/") : c.path);
        ck.secure = c.secure;
        ck.httponly = c.http_only;
        if (c.expires > 0) {
            cef_time_t t{};
            cef_basetime_t bt{};
            if (cef_time_from_doublet(c.expires, &t) && cef_time_to_basetime(&t, &bt)) {
                ck.has_expires = true;
                ck.expires = bt;
            }
        }
        if (c.same_site == "none")        ck.same_site = CEF_COOKIE_SAME_SITE_NO_RESTRICTION;
        else if (c.same_site == "lax")    ck.same_site = CEF_COOKIE_SAME_SITE_LAX_MODE;
        else if (c.same_site == "strict") ck.same_site = CEF_COOKIE_SAME_SITE_STRICT_MODE;
        else                              ck.same_site = CEF_COOKIE_SAME_SITE_UNSPECIFIED;

        // URL whose host+scheme let CEF accept this (domain-scoped) cookie.
        const std::string host = (!c.domain.empty() && c.domain[0] == '.')
                                     ? c.domain.substr(1) : c.domain;
        const std::string url = (c.secure ? "https://" : "http://") + host + "/";
        if (mgr->SetCookie(url, ck, new SetCookieCounter(ok, done))) ++posted;
    }
    // Pump until every posted write has completed (~5s cap), then flush to disk.
    for (int i = 0; i < 500 && *done < posted; ++i) pump(10);
    auto flushed = std::make_shared<bool>(false);
    mgr->FlushStore(new FlushWaiter(flushed));
    for (int i = 0; i < 500 && !*flushed; ++i) pump(10);
    return *ok;
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
