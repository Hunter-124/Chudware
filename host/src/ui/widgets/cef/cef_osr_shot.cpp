// cef/cef_osr_shot.cpp — headless OSR render-to-PNG (docs/cef_webview.md phase 2).
//
// Proves the whole off-screen pipeline WITHOUT a display or GL: create a
// windowless CEF browser, navigate, capture OnPaint's BGRA buffer, write a PNG.
// Exposed as `AI-CAD --cef-shot <url> <out.png>` and used to verify CEF actually
// renders real pages before wiring it into the interactive panel.
//
// Compiled only with AI_CAD_ENABLE_CEF.
#include "cef_osr_shot.h"

#include <chrono>
#include <cstdlib>
#include <thread>
#include <vector>

#include "include/cef_app.h"     // CefDoMessageLoopWork
#include "include/cef_browser.h"
#include "include/cef_client.h"
#include "include/cef_render_handler.h"
#include "include/cef_life_span_handler.h"
#include "include/cef_load_handler.h"
#include "include/cef_display_handler.h"
#include "include/cef_request_handler.h"
#include "include/cef_resource_request_handler.h"
#include "include/cef_request.h"
#include "include/cef_response.h"
#include "include/cef_callback.h"
#include "include/cef_version.h"   // CHROME_VERSION_* for Sec-CH-UA alignment
#include <cstdio>

#include "stb_image_write.h"  // implementation lives in stb_impl.cpp (linked into AI-CAD)

#include "cef_app.h"
#include "cef_stealth.h"

namespace ui::cef {
namespace {

class ShotClient : public CefClient,
                   public CefRenderHandler,
                   public CefLifeSpanHandler,
                   public CefLoadHandler,
                   public CefDisplayHandler,
                   public CefRequestHandler,
                   public CefResourceRequestHandler {
public:
    ShotClient(int w, int h) : w_(w), h_(h) {}

    int  width() const { return w_; }
    int  height() const { return h_; }
    bool loaded() const { return load_end_; }
    void reset_loaded() { load_end_ = false; }
    bool painted() const { return !bgra_.empty(); }
    CefRefPtr<CefBrowser> browser() const { return browser_; }
    const std::vector<unsigned char>& bgra() const { return bgra_; }

    // CefClient
    CefRefPtr<CefRenderHandler>   GetRenderHandler() override   { return this; }
    CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
    CefRefPtr<CefLoadHandler>     GetLoadHandler() override     { return this; }
    CefRefPtr<CefDisplayHandler>  GetDisplayHandler() override  { return this; }
    CefRefPtr<CefRequestHandler>  GetRequestHandler() override  { return this; }
    CefRefPtr<CefResourceRequestHandler> GetResourceRequestHandler(
        CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>, CefRefPtr<CefRequest>,
        bool, bool, const CefString&, bool&) override {
        // DIAGNOSTIC A/B: AI_CAD_CEF_NO_RRH=1 returns nullptr so the request
        // takes the default network-service path with NO app resource handler.
        // Isolates whether returning a passive handler is what breaks challenge
        // (Turnstile/reCAPTCHA) sub-resources with ERR_INVALID_ARGUMENT.
        if (const char* v = std::getenv("AI_CAD_CEF_NO_RRH"); v && v[0] == '1')
            return nullptr;
        return this;
    }
    // Dump the outgoing header map + fetch mode for challenge requests, so we can
    // compare against what a working headless Chromium sends (Referer / Sec-Fetch).
    ReturnValue OnBeforeResourceLoad(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>,
                                     CefRefPtr<CefRequest> request,
                                     CefRefPtr<CefCallback>) override {
        if (const char* v = std::getenv("AI_CAD_CEF_SHOT_CONSOLE"); v && v[0] == '1' && request) {
            const std::string u = request->GetURL().ToString();
            if (u.find("turnstile") != std::string::npos ||
                u.find("challenge-platform") != std::string::npos) {
                CefRequest::HeaderMap hm; request->GetHeaderMap(hm);
                std::fprintf(stderr, "[cef-req] flags=%d %s\n", request->GetFlags(), u.c_str());
                std::fprintf(stderr, "[cef-req]   Referrer=%s policy=%d\n",
                             request->GetReferrerURL().ToString().c_str(),
                             (int)request->GetReferrerPolicy());
                for (auto& kv : hm)
                    std::fprintf(stderr, "[cef-req]   %s: %s\n",
                                 kv.first.ToString().c_str(), kv.second.ToString().c_str());
            }
        }
        return RV_CONTINUE;
    }
    void OnResourceRedirect(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>,
                            CefRefPtr<CefRequest> request, CefRefPtr<CefResponse> response,
                            CefString& new_url) override {
        if (const char* v = std::getenv("AI_CAD_CEF_SHOT_CONSOLE"); v && v[0] == '1' && request) {
            const std::string u = request->GetURL().ToString();
            if (u.find("turnstile") != std::string::npos)
                std::fprintf(stderr, "[cef-redir] %d %s -> %s\n",
                             response ? response->GetStatus() : -1, u.c_str(),
                             new_url.ToString().c_str());
        }
    }
    // Log every non-analytics request + all failures so a --cef-shot reveals
    // whether the challenge (Turnstile) script is even fetched in the CEF runtime.
    void OnResourceLoadComplete(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>,
                                CefRefPtr<CefRequest> request,
                                CefRefPtr<CefResponse> response,
                                URLRequestStatus status, int64_t) override {
        if (const char* v = std::getenv("AI_CAD_CEF_SHOT_CONSOLE"); !(v && v[0] == '1')) return;
        const std::string u = request ? request->GetURL().ToString() : "?";
        const int http = response ? response->GetStatus() : -1;
        const bool interesting = u.find("cloudflare") != std::string::npos ||
                                 u.find("turnstile")  != std::string::npos ||
                                 u.find("challenge")  != std::string::npos ||
                                 u.find("recaptcha")  != std::string::npos ||
                                 status != UR_SUCCESS || http >= 400;
        const int err = response ? (int)response->GetError() : 0;
        if (interesting)
            std::fprintf(stderr, "[cef-net] status=%d http=%d err=%d %s\n",
                         (int)status, http, err, u.c_str());
    }

    // Console capture — AI_CAD_CEF_SHOT_CONSOLE=1 dumps page console to stderr so
    // a --cef-shot of a challenge page reveals why a widget (e.g. Cloudflare
    // Turnstile) fails to initialise in the CEF single-process/OSR runtime.
    bool OnConsoleMessage(CefRefPtr<CefBrowser>, cef_log_severity_t level,
                          const CefString& message, const CefString& source,
                          int line) override {
        if (const char* v = std::getenv("AI_CAD_CEF_SHOT_CONSOLE"); v && v[0] == '1')
            std::fprintf(stderr, "[cef-console] sev=%d %s (%s:%d)\n", (int)level,
                         message.ToString().c_str(), source.ToString().c_str(), line);
        return false;
    }

    // CefRenderHandler
    void GetViewRect(CefRefPtr<CefBrowser>, CefRect& rect) override {
        rect.Set(0, 0, w_, h_);
    }
    void OnPaint(CefRefPtr<CefBrowser>, PaintElementType type, const RectList&,
                 const void* buffer, int width, int height) override {
        if (type != PET_VIEW) return;
        const size_t n = static_cast<size_t>(width) * height * 4;
        bgra_.assign(static_cast<const unsigned char*>(buffer),
                     static_cast<const unsigned char*>(buffer) + n);
        w_ = width; h_ = height;
    }

    // CefLifeSpanHandler
    void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
        browser_ = browser;
        // Same fingerprint hardening the headless-fetch path applies, so --cef-shot
        // reflects the real wire fingerprint (see cef_stealth.h). Keep the returned
        // registration alive — it holds the DevTools agent that makes it take effect.
        stealth_reg_ = apply_stealth(browser);
    }

    // CefLoadHandler
    void OnLoadEnd(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame, int) override {
        if (frame->IsMain()) load_end_ = true;
    }

private:
    int w_, h_;
    bool load_end_ = false;
    std::vector<unsigned char> bgra_;
    CefRefPtr<CefBrowser> browser_;
    CefRefPtr<CefRegistration> stealth_reg_;  // keeps the DevTools agent attached
    IMPLEMENT_REFCOUNTING(ShotClient);
};

// AI_CAD_CEF_SHOT_EXTPUMP=1 drives the loop through the INTERACTIVE pump path
// (cef::pump_if_due, fed by the live-browser counter) instead of a raw
// CefDoMessageLoopWork, so this tool can verify the same pump the vendor panel
// uses actually renders a page headlessly.
bool ext_pump() {
    static const bool on = [] {
        const char* v = std::getenv("AI_CAD_CEF_SHOT_EXTPUMP");
        return v && v[0] == '1';
    }();
    return on;
}

void pump(int ms) {
    const int steps = ms / 10;
    for (int i = 0; i < steps; ++i) {
        if (ext_pump()) pump_if_due(); else CefDoMessageLoopWork();
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
}

}  // namespace

bool render_url_to_png(const std::string& url, int w, int h, const std::string& out_path) {
    if (!initialized()) return false;

    CefRefPtr<ShotClient> client = new ShotClient(w, h);
    CefWindowInfo window_info;
    window_info.SetAsWindowless(0);          // OSR
    CefBrowserSettings settings;
    settings.windowless_frame_rate = 30;

    // In ext-pump mode, mark a browser alive so pump_if_due pumps every frame
    // (mirrors CefWebView's OnAfterCreated/OnBeforeClose bookkeeping).
    if (ext_pump()) note_browser_opened();

    if (!CefBrowserHost::CreateBrowser(window_info, client, url, settings, nullptr, nullptr)) {
        if (ext_pump()) note_browser_closed();
        return false;
    }

    // Wait for the browser to exist + the main frame to finish loading.
    for (int i = 0; i < 500 && !(client->browser() && client->loaded()); ++i) pump(20);
    // Force at least one repaint after load, then let paints settle.
    if (client->browser())
        client->browser()->GetHost()->Invalidate(PET_VIEW);
    for (int i = 0; i < 60 && !client->painted(); ++i) pump(20);
    pump(300);  // let late paints (fonts/images) land
    // Challenge pages (Cloudflare Turnstile etc.) run JS/worker challenges for
    // several seconds after load; give them time so the console + final PNG show
    // the settled widget state. AI_CAD_CEF_SHOT_SETTLE_MS overrides.
    if (const char* s = std::getenv("AI_CAD_CEF_SHOT_SETTLE_MS")) {
        int ms = std::atoi(s);
        while (ms > 0) { const int chunk = ms > 500 ? 500 : ms; pump(chunk); ms -= chunk; }
        if (client->browser()) client->browser()->GetHost()->Invalidate(PET_VIEW);
        pump(300);
    }

    bool ok = false;
    if (client->painted()) {
        // BGRA → RGBA for stb_image_write.
        std::vector<unsigned char> rgba = client->bgra();
        for (size_t i = 0; i + 3 < rgba.size(); i += 4) std::swap(rgba[i], rgba[i + 2]);
        ok = stbi_write_png(out_path.c_str(), client->width(), client->height(), 4,
                            rgba.data(), client->width() * 4) != 0;
    }

    if (client->browser())
        client->browser()->GetHost()->CloseBrowser(true);
    pump(200);
    if (ext_pump()) note_browser_closed();
    return ok;
}

}  // namespace ui::cef
