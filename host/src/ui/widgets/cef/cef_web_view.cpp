// cef/cef_web_view.cpp — CefWebView (IWebViewBackend) off-screen render.
//
// In-process Chromium Embedded Framework off-screen browser (docs/cef_webview.md
// phase 2). Compiled only with AI_CAD_ENABLE_CEF. Contract & threading mirror
// ScreencastWebView: GL-thread-only public API; CEF's OnPaint delivers BGRA on a
// CEF thread, copied under a mutex and uploaded to the GL texture in draw().
//
// Refcounting (the one thing you MUST NOT mirror mechanically from the scaffold):
// the CefClient handed to CreateBrowser is CEF-refcounted (IMPLEMENT_REFCOUNTING,
// owned by CefRefPtr) — exactly like cef_osr_shot.cpp's ShotClient. It is a
// DIFFERENT object from the widget-owned GL/status state. They share a plain
// std::shared_ptr<Shared> handoff struct (no CefRefPtr, no intrusive count), so
// there is never a double-ownership double-free.
#include "cef_web_view.h"

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <string>
#include <vector>

#include <glad/gl.h>

#include "include/cef_browser.h"
#include "include/cef_client.h"
#include "include/cef_cookie.h"
#include "include/cef_render_handler.h"
#include "include/cef_life_span_handler.h"
#include "include/cef_load_handler.h"
#include "include/cef_display_handler.h"
#include "include/cef_request_context.h"
#include "include/cef_request_context_handler.h"  // complete type for CreateContext(nullptr)
#include "include/wrapper/cef_helpers.h"   // CEF_REQUIRE_UI_THREAD

// NOTE: do NOT include core/app_state.h here — CEF's net-error headers define
// ERR_NONE, which collides with app_state.h's enum. We only need st.rpc, reached
// through the CEF-header-free helper in cef_rpc_bridge.cpp.
#include "cef_app.h"
#include "cef_platform.h"
#include "cef_rpc_bridge.h"
#include "ui_theme.h"

namespace ui {

namespace {

// Lifecycle tracing → stderr. Diagnoses "stuck on Loading" / freeze-on-open by
// showing exactly which CEF lifecycle step fired (or didn't). Currently ON BY
// DEFAULT while we chase the OSR open crash; set AI_CAD_CEF_TRACE=0 to silence.
// CEF's own [FATAL:…] DCHECK/CHECK aborts also print to this same stderr, so a
// run captured with `2>&1 | tee` shows both our trace and any CEF fatal line.
bool cefTrace() {
    static const bool on = [] {
        const char* v = std::getenv("AI_CAD_CEF_TRACE");
        return !(v && v[0] == '0');   // default on; only "0" disables
    }();
    return on;
}
#define CEF_TRACE(...)                                                    \
    do {                                                                 \
        if (::ui::cefTrace()) {                                           \
            std::fprintf(stderr, "[cef-trace] " __VA_ARGS__);            \
            std::fprintf(stderr, "\n");                                  \
            std::fflush(stderr);                                         \
        }                                                                \
    } while (0)

// --- ImGui → CEF modifier / key mapping ---------------------------------------
uint32_t cefModifiers() {
    const ImGuiIO& io = ImGui::GetIO();
    uint32_t m = 0;
    if (io.KeyShift) m |= EVENTFLAG_SHIFT_DOWN;
    if (io.KeyCtrl)  m |= EVENTFLAG_CONTROL_DOWN;
    if (io.KeyAlt)   m |= EVENTFLAG_ALT_DOWN;
    if (io.KeySuper) m |= EVENTFLAG_COMMAND_DOWN;
    if (ImGui::IsMouseDown(ImGuiMouseButton_Left))   m |= EVENTFLAG_LEFT_MOUSE_BUTTON;
    if (ImGui::IsMouseDown(ImGuiMouseButton_Right))  m |= EVENTFLAG_RIGHT_MOUSE_BUTTON;
    if (ImGui::IsMouseDown(ImGuiMouseButton_Middle)) m |= EVENTFLAG_MIDDLE_MOUSE_BUTTON;
    return m;
}

// Windows virtual-key codes (CEF's cross-platform windows_key_code) for the
// non-character keys the page needs. Character text goes through CHAR events.
int vkFor(ImGuiKey k) {
    switch (k) {
        case ImGuiKey_Enter: case ImGuiKey_KeypadEnter: return 0x0D;  // VK_RETURN
        case ImGuiKey_Backspace: return 0x08;   // VK_BACK
        case ImGuiKey_Tab:       return 0x09;   // VK_TAB
        case ImGuiKey_Escape:    return 0x1B;   // VK_ESCAPE
        case ImGuiKey_Delete:    return 0x2E;   // VK_DELETE
        case ImGuiKey_LeftArrow: return 0x25;   // VK_LEFT
        case ImGuiKey_UpArrow:   return 0x26;   // VK_UP
        case ImGuiKey_RightArrow:return 0x27;   // VK_RIGHT
        case ImGuiKey_DownArrow: return 0x28;   // VK_DOWN
        case ImGuiKey_Home:      return 0x24;   // VK_HOME
        case ImGuiKey_End:       return 0x23;   // VK_END
        case ImGuiKey_PageUp:    return 0x21;   // VK_PRIOR
        case ImGuiKey_PageDown:  return 0x22;   // VK_NEXT
        case ImGuiKey_Insert:    return 0x2D;   // VK_INSERT
        default: return 0;
    }
}

// Clipboard + select-all shortcuts: page-focused Ctrl/Cmd + letter → the VK the
// Chrome runtime turns into a real copy/paste/cut/select-all (native OS
// clipboard — the whole reason for CEF over the screencast).
int vkForCtrlLetter(ImGuiKey k) {
    switch (k) {
        case ImGuiKey_A: return 0x41;
        case ImGuiKey_C: return 0x43;
        case ImGuiKey_V: return 0x56;
        case ImGuiKey_X: return 0x58;
        case ImGuiKey_Z: return 0x5A;
        case ImGuiKey_Y: return 0x59;
        default: return 0;
    }
}

// Normalise a vendor key to a filesystem-safe slug for its cache subdir. Mirrors
// services.browser.profiles.slug (lowercase; non-[a-z0-9_.-] → '-'), so the two
// engines line up if/when cookie interop shares a store.
std::string slug(const std::string& site) {
    std::string s;
    for (char c : site) {
        unsigned char u = (unsigned char)c;
        s += (std::isalnum(u) || c == '_' || c == '.' || c == '-')
                 ? (char)std::tolower(u) : '-';
    }
    size_t a = s.find_first_not_of('-'), b = s.find_last_not_of('-');
    return a == std::string::npos ? std::string() : s.substr(a, b - a + 1);
}

// URL-path fragments that mean "still in an auth flow" — mirrors
// embed._AUTH_PATH_FRAGMENTS so the login chip reads the same on both backends.
bool isAuthUrl(const std::string& url) {
    static const char* kFrags[] = {
        "/sign-in", "/signin", "/sign-up", "/signup", "/register",
        "/login", "/account/login", "/user/login", "/auth/", "/ap/signin"};
    std::string u = url;
    std::transform(u.begin(), u.end(), u.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    for (const char* f : kFrags)
        if (u.find(f) != std::string::npos) return true;
    return false;
}

// Collects every cookie of a request context into plain SessionCookie structs.
// CEF calls Visit() on the UI thread (== the GL thread under our external
// message pump); the jar is delivered from the destructor, which runs when CEF
// releases the last reference AFTER visitation completes — also on the UI
// thread, so the completion callback may talk to st.rpc directly.
class CookieCollector : public CefCookieVisitor {
public:
    using Done = std::function<void(std::vector<cef::SessionCookie>)>;
    explicit CookieCollector(Done done) : done_(std::move(done)) {}
    ~CookieCollector() override {
        CEF_TRACE("cookie export: visitation complete, %d cookies", (int)cookies_.size());
        if (done_) done_(std::move(cookies_));
    }

    bool Visit(const CefCookie& c, int /*count*/, int /*total*/, bool& /*deleteCookie*/) override {
        cef::SessionCookie sc;
        sc.name     = CefString(&c.name).ToString();
        sc.value    = CefString(&c.value).ToString();
        sc.domain   = CefString(&c.domain).ToString();
        sc.path     = CefString(&c.path).ToString();
        sc.secure   = c.secure != 0;
        sc.httpOnly = c.httponly != 0;
        if (c.has_expires) {
            cef_time_t t{};
            double d = 0.0;
            if (cef_time_from_basetime(c.expires, &t) && cef_time_to_doublet(&t, &d))
                sc.expires = d;
        }
        switch (c.same_site) {
            case CEF_COOKIE_SAME_SITE_NO_RESTRICTION: sc.sameSite = "none";   break;
            case CEF_COOKIE_SAME_SITE_LAX_MODE:       sc.sameSite = "lax";    break;
            case CEF_COOKIE_SAME_SITE_STRICT_MODE:    sc.sameSite = "strict"; break;
            default:                                  break;  // unspecified
        }
        cookies_.push_back(std::move(sc));
        return true;   // keep visiting
    }

private:
    Done done_;
    std::vector<cef::SessionCookie> cookies_;
    IMPLEMENT_REFCOUNTING(CookieCollector);
};

}  // namespace

// ---- Shared: the CEF-callback ↔ widget handoff (plain, shared_ptr-owned) ------
// Written by the CefClient on a CEF thread, read by the widget on the GL thread.
// No CefRefPtr / IMPLEMENT_REFCOUNTING here — see the file header.
struct CefWebView::Shared {
    std::mutex mtx;

    // Rendering handoff (guarded by mtx).
    std::vector<unsigned char> bgra;   // latest PET_VIEW frame, paintW*paintH*4 BGRA
    int  paintW = 0, paintH = 0;       // size CEF actually painted (OnPaint)
    bool dirty  = false;
    // Requested view size (what draw() wants; GetViewRect returns this). Kept
    // separate from paintW/H so a resize can't feed back into itself.
    int  reqW = 1280, reqH = 800;

    // Status (written on CEF thread, read on GL thread; small races benign).
    std::string url, loginState = "unknown", lastError;
    bool active = false, starting = false, detached = false;
    bool startedAtAuth = false;        // opened on a sign-in URL → detect redirect-away
};

// ---- Client: the CEF-refcounted handler (owned by CefRefPtr only) ------------
class CefWebView::Client : public CefClient,
                           public CefRenderHandler,
                           public CefLifeSpanHandler,
                           public CefLoadHandler,
                           public CefDisplayHandler {
public:
    explicit Client(std::shared_ptr<Shared> s) : s_(std::move(s)) {}

    CefRefPtr<CefBrowser> browser() const { return browser_; }

    // CefClient
    CefRefPtr<CefRenderHandler>   GetRenderHandler() override   { return this; }
    CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
    CefRefPtr<CefLoadHandler>     GetLoadHandler() override     { return this; }
    CefRefPtr<CefDisplayHandler>  GetDisplayHandler() override  { return this; }

    // CefRenderHandler
    void GetViewRect(CefRefPtr<CefBrowser>, CefRect& rect) override {
        std::lock_guard<std::mutex> lk(s_->mtx);
        rect = CefRect(0, 0, s_->reqW > 0 ? s_->reqW : 1280,
                              s_->reqH > 0 ? s_->reqH : 800);
    }
    void OnPaint(CefRefPtr<CefBrowser>, PaintElementType type, const RectList&,
                 const void* buffer, int width, int height) override {
        if (type != PET_VIEW) return;   // PET_POPUP (<select>) is a follow-up commit
        if (!tracedFirstPaint_) { tracedFirstPaint_ = true; CEF_TRACE("OnPaint FIRST %dx%d", width, height); }
        std::lock_guard<std::mutex> lk(s_->mtx);
        const size_t n = static_cast<size_t>(width) * height * 4;
        s_->bgra.assign(static_cast<const unsigned char*>(buffer),
                        static_cast<const unsigned char*>(buffer) + n);
        s_->paintW = width; s_->paintH = height; s_->dirty = true;
        if (!s_->active) { s_->active = true; s_->starting = false; }
    }

    // CefLifeSpanHandler
    void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
        browser_ = browser;
        CEF_TRACE("OnAfterCreated (browser id=%d)", browser ? browser->GetIdentifier() : -1);
    }
    void OnBeforeClose(CefRefPtr<CefBrowser>) override          { browser_ = nullptr; }
    // Keep popups (target=_blank etc.) in-frame: block the OS window, navigate the
    // main frame instead — the embedded panel has nowhere to put a second window.
    bool OnBeforePopup(CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame>, int /*popup_id*/,
                       const CefString& target_url, const CefString& /*target_frame_name*/,
                       WindowOpenDisposition, bool, const CefPopupFeatures&, CefWindowInfo&,
                       CefRefPtr<CefClient>&, CefBrowserSettings&, CefRefPtr<CefDictionaryValue>&,
                       bool*) override {
        if (browser && !target_url.empty()) browser->GetMainFrame()->LoadURL(target_url);
        return true;   // cancel the native popup
    }

    // CefLoadHandler / CefDisplayHandler — status for the login chip.
    void OnLoadStart(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame,
                     TransitionType) override {
        if (frame->IsMain()) CEF_TRACE("OnLoadStart %s", frame->GetURL().ToString().c_str());
    }
    void OnLoadEnd(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame, int httpStatus) override {
        if (frame->IsMain()) {
            CEF_TRACE("OnLoadEnd http=%d %s", httpStatus, frame->GetURL().ToString().c_str());
            refreshLoginState(frame->GetURL());
        }
    }
    // A failed main-frame load (DNS, TLS, blocked, net error) previously left the
    // panel on "Loading…" forever with no signal. Surface it: record the error so
    // draw() shows it instead of a perpetual spinner, and clear `starting`.
    void OnLoadError(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame,
                     ErrorCode errorCode, const CefString& errorText,
                     const CefString& failedUrl) override {
        if (!frame->IsMain()) return;                 // sub-resource error — ignore
        if (errorCode == ERR_ABORTED) return;         // navigation superseded — not fatal
        const std::string msg = errorText.ToString();
        CEF_TRACE("OnLoadError code=%d '%s' url=%s", (int)errorCode, msg.c_str(),
                  failedUrl.ToString().c_str());
        std::lock_guard<std::mutex> lk(s_->mtx);
        s_->starting = false;
        s_->lastError = msg.empty() ? "page failed to load" : ("load error: " + msg);
    }
    void OnAddressChange(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame,
                         const CefString& url) override {
        if (frame->IsMain()) refreshLoginState(url);
    }

private:
    void refreshLoginState(const CefString& cefUrl) {
        const std::string url = cefUrl.ToString();
        std::lock_guard<std::mutex> lk(s_->mtx);
        s_->url = url;
        if (isAuthUrl(url)) {
            s_->loginState = "login_page";
        } else if (s_->startedAtAuth) {
            // Left the sign-in URL for a normal page → probably signed in. Heuristic
            // hint for the chip only; the authoritative signed-in check stays
            // server-side (profiles.has_login) until cookie interop lands.
            s_->loginState = "logged_in";
        } else {
            s_->loginState = "unknown";
        }
    }

    std::shared_ptr<Shared> s_;
    CefRefPtr<CefBrowser> browser_;   // set in OnAfterCreated (CEF/UI thread)
    bool tracedFirstPaint_ = false;   // AI_CAD_CEF_TRACE: log only the first OnPaint
    IMPLEMENT_REFCOUNTING(Client);
};

// ---- widget-owned state (GL thread) ------------------------------------------
struct CefWebView::Impl {
    std::shared_ptr<Shared> s = std::make_shared<Shared>();
    CefRefPtr<Client>       client;         // null until open()
    std::string             pendingUrlReq;  // guards stale browser_start_url callbacks

    // GL-thread-owned texture.
    unsigned tex = 0;
    int texW = 0, texH = 0;
    bool focused = false;
    bool invalidatedOnce_ = false;   // AI_CAD_CEF_TRACE: log the first paint kick only

    void createBrowser(const std::string& url, const std::string& cacheDir, bool startedAtAuth) {
        CEF_TRACE("createBrowser url=%s cache=%s onUiThread=%d",
                  url.c_str(), cacheDir.c_str(), (int)CefCurrentlyOn(TID_UI));
        CEF_REQUIRE_UI_THREAD();   // CreateBrowser is TID_UI-affine (loud if a future change breaks it)
        { std::lock_guard<std::mutex> lk(s->mtx); s->startedAtAuth = startedAtAuth; }
        client = new Client(s);

        CefWindowInfo window_info;
        window_info.SetAsWindowless(0);          // OSR
        CefBrowserSettings settings;
        settings.windowless_frame_rate = 30;

        // Per-site request context → its own on-disk cookie jar (cacheDir aligns
        // with the Playwright profile dir; see docs/cef_webview.md cookie interop).
        CefRefPtr<CefRequestContext> ctx;
        if (!cacheDir.empty()) {
            CefRequestContextSettings rc;
            CefString(&rc.cache_path) = cacheDir;
            ctx = CefRequestContext::CreateContext(rc, nullptr);
            CEF_TRACE("per-site request context %s", ctx ? "created" : "NULL (CreateContext failed)");
        }
        const bool ok =
            CefBrowserHost::CreateBrowser(window_info, client, url, settings, nullptr, ctx);
        CEF_TRACE("CreateBrowser returned %d", (int)ok);
    }

    void closeBrowser() {
        if (client && client->browser())
            client->browser()->GetHost()->CloseBrowser(/*force=*/true);
        client = nullptr;
    }

    void freeTexture() {
        if (tex) { glDeleteTextures(1, &tex); tex = 0; }
        texW = texH = 0;
    }
};

// ---- public ------------------------------------------------------------------

bool CefWebView::available() { return cef::initialized(); }

CefWebView::CefWebView() : impl_(std::make_unique<Impl>()) {}

CefWebView::~CefWebView() {
    { std::lock_guard<std::mutex> lk(impl_->s->mtx); impl_->s->detached = true; }
    impl_->closeBrowser();   // CloseBrowser(force); the Client (CefRefPtr) drains its own callbacks
    impl_->freeTexture();    // GL thread (widget-owned)
}

void CefWebView::open(AppState& st, const std::string& siteOrUrl, bool isUrl) {
    close(st);
    auto& s = *impl_->s;
    { std::lock_guard<std::mutex> lk(s.mtx); s.lastError.clear(); s.starting = true; }

    if (isUrl) {
        impl_->createBrowser(siteOrUrl, /*cacheDir=*/"", isAuthUrl(siteOrUrl));
        std::lock_guard<std::mutex> lk(s.mtx);
        s.url = siteOrUrl;
        return;
    }

    // Vendor key → resolve its sign-in URL + per-site cache dir over the worker
    // RPC (reuses embed.start_url_for_site + profiles.profile_dir). The callback
    // runs on the GL/UI thread (same drain as the screencast frame callbacks), so
    // CreateBrowser there is TID_UI-safe.
    const std::string req = siteOrUrl;
    impl_->pendingUrlReq = req;
    Impl* impl = impl_.get();
    CEF_TRACE("open(site=%s) → rpc browser_start_url…", siteOrUrl.c_str());
    cef::rpc_start_url(st, siteOrUrl, [impl, req](bool ok, const std::string& url,
                                                  const std::string& /*plwCacheDir*/,
                                                  const std::string& err) {
        CEF_TRACE("rpc_start_url cb ok=%d url=%s err=%s onUiThread=%d",
                  (int)ok, url.c_str(), err.c_str(), (int)CefCurrentlyOn(TID_UI));
        if (impl->s->detached || impl->pendingUrlReq != req) { CEF_TRACE("… stale/closed, drop"); return; }
        if (!ok || url.empty()) {
            std::lock_guard<std::mutex> lk(impl->s->mtx);
            impl->s->starting = false;
            impl->s->lastError = err.empty() ? "no sign-in URL for this vendor" : err;
            return;
        }
        // Per-site cache under CEF's own root (NOT the Playwright dir the RPC
        // returns — cookie interop is deferred; see cache_root()).
        const std::string cacheDir = cef_platform::cache_root() + "/" + slug(req);
        impl->createBrowser(url, cacheDir, isAuthUrl(url));
        std::lock_guard<std::mutex> lk(impl->s->mtx);
        impl->s->url = url;
    });
}

void CefWebView::attach(AppState& st, const std::string& embedId) {
    // CEF has no worker embed; the wizard's per-site step maps to opening that
    // site's login URL in-process. `embedId` is unused (the panel passes the site
    // via open() for CEF — see usesWorkerEmbed()).
    (void)st; (void)embedId;
}

void CefWebView::close(AppState& /*st*/) {
    impl_->pendingUrlReq.clear();
    impl_->invalidatedOnce_ = false;   // next open() kicks its own first paint
    impl_->closeBrowser();
    auto& s = *impl_->s;
    std::lock_guard<std::mutex> lk(s.mtx);
    s.active = s.starting = false;
    s.url.clear();
    s.loginState = "unknown";
    s.startedAtAuth = false;
    s.bgra.clear();
    s.paintW = s.paintH = 0;
    s.dirty = false;
}

void CefWebView::update(AppState& /*st*/) {
    // The external message pump is driven globally (cef::pump_if_due from the GLFW
    // loop), so there's no per-widget polling. Status/URL flow in via CEF handlers.
}

bool CefWebView::usesWorkerEmbed() const { return false; }

void CefWebView::exportSession(AppState& st, const std::string& site,
                               std::function<void(bool)> done) {
    Client* c = impl_->client.get();
    if (site.empty() || !c || !c->browser()) {
        CEF_TRACE("exportSession(%s): no live browser", site.c_str());
        if (done) done(false);
        return;
    }
    CefRefPtr<CefRequestContext> rctx = c->browser()->GetHost()->GetRequestContext();
    CefRefPtr<CefCookieManager>  mgr  = rctx ? rctx->GetCookieManager(nullptr) : nullptr;
    if (!mgr) {
        CEF_TRACE("exportSession(%s): no cookie manager", site.c_str());
        if (done) done(false);
        return;
    }
    mgr->FlushStore(nullptr);   // best-effort: persist CEF's own on-disk jar too
    CEF_TRACE("exportSession(%s): visiting cookies…", site.c_str());
    AppState* stp = &st;        // AppState outlives every RPC (app-lifetime object)
    CefRefPtr<CefCookieVisitor> collector = new CookieCollector(
        [stp, site, done](std::vector<cef::SessionCookie> cookies) {
            if (cookies.empty()) {
                if (done) done(false);
                return;
            }
            cef::rpc_session_import(*stp, site, std::move(cookies),
                                    [site, done](bool ok, const std::string& err) {
                CEF_TRACE("exportSession(%s): import %s %s", site.c_str(),
                          ok ? "ok" : "FAILED", err.c_str());
                if (done) done(ok);
            });
        });
    if (!mgr->VisitAllCookies(collector))
        CEF_TRACE("exportSession(%s): VisitAllCookies refused", site.c_str());
    // A refused visit still destroys the collector → done(false) via empty jar.
}

// ---- input (GL/UI thread) ----------------------------------------------------

void CefWebView::forwardInput(ImVec2 img0, float scale, ImVec2 imgSize) {
    Client* c = impl_->client.get();
    if (!c || !c->browser()) return;
    CEF_REQUIRE_UI_THREAD();   // Send*Event are TID_UI-affine; assert don't hope (advisor)
    CefRefPtr<CefBrowserHost> host = c->browser()->GetHost();

    ImGuiIO& io = ImGui::GetIO();
    const bool hovered = ImGui::IsItemHovered();

    auto pagePos = [&](ImVec2 m) {
        float x = (m.x - img0.x) / scale;
        float y = (m.y - img0.y) / scale;
        x = std::max(0.0f, std::min(x, imgSize.x / scale));
        y = std::max(0.0f, std::min(y, imgSize.y / scale));
        CefMouseEvent e;
        e.x = (int)x; e.y = (int)y; e.modifiers = cefModifiers();
        return e;
    };
    const CefMouseEvent me = pagePos(io.MousePos);

    // Mouse buttons: click grabs page focus, click elsewhere releases it.
    static const cef_mouse_button_type_t kBtn[3] = {MBT_LEFT, MBT_RIGHT, MBT_MIDDLE};
    for (int b = 0; b < 3; ++b) {
        if (ImGui::IsMouseClicked((ImGuiMouseButton)b)) {
            if (hovered) {
                impl_->focused = true;
                host->SendMouseClickEvent(me, kBtn[b], /*mouseUp=*/false, 1);
            } else {
                impl_->focused = false;
            }
        }
        if (hovered && ImGui::IsMouseReleased((ImGuiMouseButton)b))
            host->SendMouseClickEvent(me, kBtn[b], /*mouseUp=*/true, 1);
    }
    if (hovered && (io.MouseDelta.x != 0.0f || io.MouseDelta.y != 0.0f))
        host->SendMouseMoveEvent(me, /*mouseLeave=*/false);
    if (hovered && (io.MouseWheel != 0.0f || io.MouseWheelH != 0.0f))
        host->SendMouseWheelEvent(me, (int)(io.MouseWheelH * 120.0f),
                                      (int)(io.MouseWheel * 120.0f));

    // Keyboard only while the page holds focus.
    if (!impl_->focused) return;
    ImGui::SetNextFrameWantCaptureKeyboard(true);
    const uint32_t mods = cefModifiers();

    auto sendVK = [&](int vk) {
        CefKeyEvent e;
        e.modifiers = mods;
        e.windows_key_code = vk;
        e.native_key_code  = vk;
        e.type = KEYEVENT_RAWKEYDOWN; host->SendKeyEvent(e);
        e.type = KEYEVENT_KEYUP;      host->SendKeyEvent(e);
    };

    // Ctrl/Cmd shortcuts (copy/paste/cut/select-all/undo/redo) — send the VK so the
    // Chrome runtime performs the native clipboard action; suppress text below.
    const bool cmd = io.KeyCtrl || io.KeySuper;
    if (cmd) {
        for (ImGuiKey k : {ImGuiKey_A, ImGuiKey_C, ImGuiKey_V, ImGuiKey_X,
                           ImGuiKey_Z, ImGuiKey_Y})
            if (ImGui::IsKeyPressed(k, false))
                if (int vk = vkForCtrlLetter(k)) sendVK(vk);
    } else {
        // Printable text as CHAR events (correct Unicode + IME composition).
        for (int i = 0; i < io.InputQueueCharacters.Size; ++i) {
            unsigned int ch = (unsigned int)io.InputQueueCharacters[i];
            if (ch == 0 || ch < 32) continue;
            CefKeyEvent e;
            e.modifiers = mods;
            e.type = KEYEVENT_CHAR;
            e.character = (char16_t)ch;
            e.unmodified_character = (char16_t)ch;
            e.windows_key_code = (int)ch;
            host->SendKeyEvent(e);
        }
    }

    // Non-character keys (Enter, arrows, Backspace, …) as raw key events.
    static const ImGuiKey kSpecial[] = {
        ImGuiKey_Enter, ImGuiKey_KeypadEnter, ImGuiKey_Backspace, ImGuiKey_Tab,
        ImGuiKey_Escape, ImGuiKey_Delete, ImGuiKey_Insert,
        ImGuiKey_LeftArrow, ImGuiKey_RightArrow, ImGuiKey_UpArrow, ImGuiKey_DownArrow,
        ImGuiKey_Home, ImGuiKey_End, ImGuiKey_PageUp, ImGuiKey_PageDown};
    for (ImGuiKey k : kSpecial)
        if (ImGui::IsKeyPressed(k, false))
            if (int vk = vkFor(k)) sendVK(vk);
}

// ---- draw (GL/UI thread) -----------------------------------------------------

bool CefWebView::draw(AppState& /*st*/, ImVec2 avail) {
    auto& s        = *impl_->s;
    const float uc = ui::uiScale;
    if (avail.x < 16.0f || avail.y < 16.0f) {
        std::lock_guard<std::mutex> lk(s.mtx);
        return s.active || s.starting;
    }

    ImDrawList* dl  = ImGui::GetWindowDrawList();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1 = ImVec2(p0.x + avail.x, p0.y + avail.y);
    dl->AddRectFilled(p0, p1, Palette::toU32(palette.panelBg), 6.0f * uc);

    auto centeredText = [&](const char* txt, float yOff, const float* col) {
        const ImVec2 ts = ImGui::CalcTextSize(txt);
        dl->AddText(ImVec2(p0.x + (avail.x - ts.x) * 0.5f, p0.y + avail.y * 0.5f + yOff),
                    Palette::toU32(col), txt);
    };

    // Record the requested size + upload the newest frame under the lock, but do
    // NOT touch CEF while holding it. WasResized() synchronously re-enters
    // GetViewRect() on THIS thread (TID_UI == the GL thread under the external
    // message pump), and GetViewRect() locks s.mtx — a non-recursive relock =
    // self-deadlock (the "freeze/abort on opening the browser" bug: main thread
    // stuck in __lll_lock_wait, every CEF callback that wants s.mtx wedged behind
    // it). So we only flip a flag here and call WasResized() after the lock drops.
    bool haveTex = false, starting = false, active = false, needResize = false;
    std::string lastError;
    {
        std::lock_guard<std::mutex> lk(s.mtx);
        starting = s.starting; active = s.active; lastError = s.lastError;
        const int nw = (int)avail.x, nh = (int)avail.y;
        if (nw != s.reqW || nh != s.reqH) {
            s.reqW = nw; s.reqH = nh;
            needResize = true;
        }
        if (s.dirty && !s.bgra.empty() && s.paintW > 0 && s.paintH > 0) {
            if (!impl_->tex) glGenTextures(1, &impl_->tex);
            glBindTexture(GL_TEXTURE_2D, impl_->tex);
            if (s.paintW != impl_->texW || s.paintH != impl_->texH) {
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
                glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, s.paintW, s.paintH, 0,
                             GL_BGRA, GL_UNSIGNED_BYTE, s.bgra.data());  // CEF is BGRA
                impl_->texW = s.paintW; impl_->texH = s.paintH;
            } else {
                glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, s.paintW, s.paintH,
                                GL_BGRA, GL_UNSIGNED_BYTE, s.bgra.data());
            }
            s.dirty = false;
        }
        haveTex = impl_->tex && impl_->texW > 0 && impl_->texH > 0;
    }

    // CEF host call OUTSIDE the lock (see above): safe now that s.mtx is dropped.
    const bool haveBrowser = impl_->client && impl_->client->browser();
    if (needResize) {
        CEF_TRACE("draw WasResized %dx%d haveBrowser=%d", (int)avail.x, (int)avail.y,
                  (int)haveBrowser);
        if (haveBrowser)
            impl_->client->browser()->GetHost()->WasResized();
    }

    // OSR FIRST-PAINT KICK. cef_osr_shot.cpp (the known-good headless OSR path in
    // this exact CEF build) has to call Invalidate(PET_VIEW) to force the first
    // frame — with --disable-gpu-compositing the windowless compositor does NOT
    // reliably start painting from page-load auto-invalidation alone. Without
    // this the interactive panel sits on "Loading…" forever (no OnPaint → active
    // never flips). Kick every frame until the first OnPaint sets `active`;
    // afterwards normal content invalidations drive paints and we stop nagging.
    if (haveBrowser && !active) {
        if (!impl_->invalidatedOnce_) { impl_->invalidatedOnce_ = true; CEF_TRACE("draw Invalidate(PET_VIEW) — kicking first paint"); }
        impl_->client->browser()->GetHost()->Invalidate(PET_VIEW);
    }

    // No frame yet → status text.
    if (!haveTex) {
        if (!lastError.empty()) centeredText(lastError.c_str(), -8.0f * uc, palette.statusErr);
        else if (starting || active) {
            char buf[64];
            int dots = 1 + (int)(ImGui::GetTime() * 2.0) % 3;
            std::snprintf(buf, sizeof(buf), "Loading%.*s", dots, "...");
            centeredText(buf, -8.0f * uc, palette.textDisabled);
        } else {
            centeredText("No page open", -8.0f * uc, palette.textDisabled);
        }
        ImGui::SetCursorScreenPos(p0);
        ImGui::Dummy(avail);
        return active || starting;
    }

    // Letterbox the texture into `avail`; input maps to PAGE pixels (never × uiScale).
    const float scale = std::min(avail.x / (float)impl_->texW, avail.y / (float)impl_->texH);
    const ImVec2 imgSize((float)impl_->texW * scale, (float)impl_->texH * scale);
    const ImVec2 img0(p0.x + (avail.x - imgSize.x) * 0.5f,
                      p0.y + (avail.y - imgSize.y) * 0.5f);
    dl->AddImage((ImTextureID)(intptr_t)impl_->tex, img0,
                 ImVec2(img0.x + imgSize.x, img0.y + imgSize.y));

    ImGui::SetCursorScreenPos(img0);
    ImGui::InvisibleButton("##cefview_input", imgSize,
                           ImGuiButtonFlags_MouseButtonLeft |
                               ImGuiButtonFlags_MouseButtonRight |
                               ImGuiButtonFlags_MouseButtonMiddle);
    forwardInput(img0, scale, imgSize);

    if (impl_->focused)
        dl->AddRect(img0, ImVec2(img0.x + imgSize.x, img0.y + imgSize.y),
                    Palette::toU32(palette.accentMid), 2.0f * uc, 0, 1.5f * uc);

    ImGui::SetCursorScreenPos(p0);
    ImGui::Dummy(avail);
    return true;
}

// ---- accessors (small benign races on std::string across threads) ------------
const std::string& CefWebView::url() const        { return impl_->s->url; }
const std::string& CefWebView::loginState() const { return impl_->s->loginState; }
bool CefWebView::active() const                   { return impl_->s->active; }
bool CefWebView::starting() const                 { return impl_->s->starting; }
const std::string& CefWebView::lastError() const  { return impl_->s->lastError; }

}  // namespace ui
