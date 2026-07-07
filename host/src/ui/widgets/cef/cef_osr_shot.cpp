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
#include <thread>
#include <vector>

#include "include/cef_app.h"     // CefDoMessageLoopWork
#include "include/cef_browser.h"
#include "include/cef_client.h"
#include "include/cef_render_handler.h"
#include "include/cef_life_span_handler.h"
#include "include/cef_load_handler.h"

#include "stb_image_write.h"  // implementation lives in stb_impl.cpp (linked into AI-CAD)

#include "cef_app.h"

namespace ui::cef {
namespace {

class ShotClient : public CefClient,
                   public CefRenderHandler,
                   public CefLifeSpanHandler,
                   public CefLoadHandler {
public:
    ShotClient(int w, int h) : w_(w), h_(h) {}

    int  width() const { return w_; }
    int  height() const { return h_; }
    bool loaded() const { return load_end_; }
    bool painted() const { return !bgra_.empty(); }
    CefRefPtr<CefBrowser> browser() const { return browser_; }
    const std::vector<unsigned char>& bgra() const { return bgra_; }

    // CefClient
    CefRefPtr<CefRenderHandler>   GetRenderHandler() override   { return this; }
    CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
    CefRefPtr<CefLoadHandler>     GetLoadHandler() override     { return this; }

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
    void OnAfterCreated(CefRefPtr<CefBrowser> browser) override { browser_ = browser; }

    // CefLoadHandler
    void OnLoadEnd(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame, int) override {
        if (frame->IsMain()) load_end_ = true;
    }

private:
    int w_, h_;
    bool load_end_ = false;
    std::vector<unsigned char> bgra_;
    CefRefPtr<CefBrowser> browser_;
    IMPLEMENT_REFCOUNTING(ShotClient);
};

void pump(int ms) {
    const int steps = ms / 10;
    for (int i = 0; i < steps; ++i) {
        CefDoMessageLoopWork();
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

    if (!CefBrowserHost::CreateBrowser(window_info, client, url, settings, nullptr, nullptr))
        return false;

    // Wait for the browser to exist + the main frame to finish loading.
    for (int i = 0; i < 500 && !(client->browser() && client->loaded()); ++i) pump(20);
    // Force at least one repaint after load, then let paints settle.
    if (client->browser())
        client->browser()->GetHost()->Invalidate(PET_VIEW);
    for (int i = 0; i < 60 && !client->painted(); ++i) pump(20);
    pump(300);  // let late paints (fonts/images) land

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
    return ok;
}

}  // namespace ui::cef
