// cef/cef_web_view.cpp — CefWebView (IWebViewBackend) off-screen render.
//
// In-process Chromium Embedded Framework off-screen browser
// (docs/design/cef_webview.md). Compiled only with CHUDWARE_ENABLE_CEF.
// GL-thread-only public API; CEF's OnPaint delivers BGRA on a CEF thread,
// copied under a mutex and uploaded to the GL texture in draw().
//
// Refcounting (the one thing you MUST NOT get wrong):
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
#include <cstring>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include <atomic>


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
#include "include/cef_request_handler.h"           // resource-load failure diagnostics
#include "include/cef_resource_request_handler.h"
#include "include/cef_request.h"
#include "include/cef_response.h"
#include "include/cef_devtools_message_observer.h" // copy-out: read getSelection() result
#include "include/cef_parser.h"                     // CefParseJSON
#include "include/cef_registration.h"
#include "include/cef_version.h"                     // CHROME_VERSION_* for Sec-CH-UA
#include "include/wrapper/cef_helpers.h"   // CEF_REQUIRE_UI_THREAD
#include "include/cef_task.h"               // CefPostTask / CefTask — hop onto TID_UI

// NOTE: do NOT include core/app_state.h here — CEF's net-error headers define
// ERR_NONE, which collides with app_state.h's enum. We only need st.rpc, reached
// through the CEF-header-free helper in cef_rpc_bridge.cpp.
#include "cef_app.h"
#include "cef_platform.h"
#include "cef_rpc_bridge.h"
#include "cef_trace_file.h"
#include "ui_theme.h"

namespace ui {

namespace {

// Lifecycle events share the opt-in, bounded, metadata-only persistent sink.
bool cefTrace() {
    return cef::trace_enabled();
}
#define CEF_TRACE(...)                                                       \
    do {                                                                    \
        if (::ui::cef::trace_enabled()) {                                    \
            char _tb[512];                                                  \
            std::snprintf(_tb, sizeof(_tb), "[cef-trace] " __VA_ARGS__);       \
            ::ui::cef::trace_file_line(_tb);                                 \
        }                                                                   \
    } while (0)

// Run a closure on the CEF browser-process UI thread. CefBrowserHost::CreateBrowser
// is documented TID_UI-only ("can only be called on the browser process UI thread")
// and CefRequestContext::CreateContext returns NULL off it — so the create path must
// hop back to TID_UI when a caller lands off-thread (see openOrNavigate). TID_UI is
// drained by our external message pump, so a posted task runs on the next pump tick.
class UiClosureTask : public CefTask {
public:
    explicit UiClosureTask(std::function<void()> fn) : fn_(std::move(fn)) {}
    void Execute() override { if (fn_) fn_(); }
private:
    std::function<void()> fn_;
    IMPLEMENT_REFCOUNTING(UiClosureTask);
};
inline void post_to_ui(std::function<void()> fn) {
    CefPostTask(TID_UI, new UiClosureTask(std::move(fn)));
}

// DIAGNOSTIC: capture the real reCAPTCHA / login network verdict. OFF by default
// (opt-in CHUDWARE_CEF_NETLOG=1) because it enables the CDP Network domain, which we
// otherwise keep quiet — it never touches Runtime.enable, so it does not reintroduce
// the automation tell, but it is strictly a debugging aid. When on, OnDevToolsEvent
// logs Network.responseReceived for the reCAPTCHA token exchange and login POST so we
// can see (server-side status/mime) whether a token was issued and scored-low vs.
// blocked outright — instead of inferring the failure from an absent console error.
bool cefNetLog() {
    static const bool on = [] {
        const char* v = std::getenv("CHUDWARE_CEF_NETLOG");
        return v && std::strcmp(v, "1") == 0;
    }();
    return on && cefTrace();
}

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
// clipboard).
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

// PANEL PERSISTENT PROFILE: one stable on-disk profile for the interactive panel,
// shared by EVERY vendor and every app run. Under --single-process only the FIRST
// CefRequestContext is ever created (vendor switches reuse it via LoadURL — see
// openOrNavigate), so the old per-vendor cache dirs were already collapsed to one
// de-facto jar, and a raw-URL open passed "" == an in-memory context wiped on every
// restart. A single persistent dir means Google / Cloudflare / reCAPTCHA cookies +
// history accumulate across sessions, which lifts the challenge trust score on
// repeat visits (a pristine empty profile scores near zero — a big reason first-time
// reCAPTCHA/Turnstile fail in-app). Cookies stay domain-scoped so vendors never
// collide.
//
// This is DELIBERATELY a DIFFERENT dir from the headless daemon's _warm_profile:
// the panel (app process) and the cef_daemon (sibling process) must never write the
// same Chromium profile concurrently — two owners corrupt the cookie/leveldb store.
// The daemon is the sole owner of _warm_profile; a login here carries into headless
// fetch via exportSession → browser_session_import → the daemon's "import_session"
// (cookie re-injection, not dir-sharing — OSCrypt is per-process). See MEMORY
// vendor-login-session-handoff and cef_daemon.cpp.
// CHUDWARE_CEF_PANEL_PROFILE_DIR overrides the location (tests / throwaway runs).
std::string panel_profile_dir() {
    if (const char* ov = std::getenv("CHUDWARE_CEF_PANEL_PROFILE_DIR"); ov && ov[0])
        return ov;
    return cef_platform::cache_root() + "/_panel_profile";
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

    // OSR POPUP layer (HTML <select> dropdowns, autofill lists). In windowless
    // mode CEF paints these as a SEPARATE PET_POPUP element with its own buffer +
    // an OnPopupSize rect (in view px); the main view underneath does NOT contain
    // them. Dropping PET_POPUP (the old behaviour) made every native dropdown
    // invisible/dead. We keep the popup buffer + rect here and composite it over
    // the main texture in draw(). Guarded by mtx.
    std::vector<unsigned char> popupBgra;
    int  popupW = 0, popupH = 0;       // size of the last PET_POPUP paint
    int  popupX = 0, popupY = 0;       // OnPopupSize origin, in view pixels
    bool popupShown = false;           // OnPopupShow(true/false)
    bool popupDirty = false;
    // Requested view size (what draw() wants; GetViewRect returns this). Kept
    // separate from paintW/H so a resize can't feed back into itself.
    int  reqW = 1280, reqH = 800;

    // Status (written on CEF thread, read on GL thread; small races benign).
    std::string url, loginState = "unknown", lastError;
    bool active = false, starting = false, detached = false;
    bool startedAtAuth = false;        // opened on a sign-in URL → detect redirect-away
    // URL requested via open() during the async window between createBrowser() and
    // OnAfterCreated (browser_ still null). OnAfterCreated LoadURLs it so we never
    // fire a SECOND createBrowser — which under --single-process re-inits Blink in a
    // new renderer thread and SIGTRAPs (InitializeWtf). Guarded by mtx.
    std::string pendingNav;

    // COPY-OUT bridge (mirror of the Ctrl+V paste bridge). Chromium's own
    // clipboard here is the headless-Ozone in-memory stub (OSR has no native
    // window), so a page Ctrl+C never reaches the OS clipboard — nothing to
    // paste into another app. On Ctrl+C we ask the page for its selection via
    // CDP Runtime.evaluate; the async result lands in OnDevToolsMethodResult,
    // which stashes the text here for draw() to push to the HOST clipboard on
    // the GL thread. copyMsgId matches the reply to our query. Guarded by mtx.
    std::string pendingClipboardOut;
    int copyMsgId = -1;

    // One-shot, fired from Client::OnBeforeClose once the outgoing browser's
    // teardown actually completes. open() defers creating the next browser (a new
    // per-site CefRequestContext == a new StoragePartition == a new renderer
    // process) until this fires — see closeBrowser()'s comment for why firing
    // CreateBrowser right after the async CloseBrowser races the old process's
    // teardown under --single-process.
    std::function<void()> onFullyClosed;

    // One-shot fired from Client::OnLoadEnd / OnLoadError for main-frame loads.
    // clearSession uses this to inject origin-scoped storage clear AFTER the
    // auth host has actually finished loading (not on a stale origin).
    // Signature: (url, ok). Guarded by mtx; swapped out before invoke.
    std::function<void(const std::string&, bool)> onMainLoadEnd;
};

// ---- Client: the CEF-refcounted handler (owned by CefRefPtr only) ------------
class CefWebView::Client : public CefClient,
                           public CefRenderHandler,
                           public CefLifeSpanHandler,
                           public CefLoadHandler,
                           public CefDisplayHandler,
                           public CefRequestHandler,
                           public CefResourceRequestHandler,
                           public CefDevToolsMessageObserver {
public:
    explicit Client(std::shared_ptr<Shared> s) : s_(std::move(s)) {}

    CefRefPtr<CefBrowser> browser() const { return browser_; }

    // CefClient
    CefRefPtr<CefRenderHandler>   GetRenderHandler() override   { return this; }
    CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
    CefRefPtr<CefLoadHandler>     GetLoadHandler() override     { return this; }
    CefRefPtr<CefDisplayHandler>  GetDisplayHandler() override  { return this; }
    CefRefPtr<CefRequestHandler>  GetRequestHandler() override  { return this; }

    // CRITICAL: returning a non-null CefResourceRequestHandler here makes CEF wrap
    // EVERY request in a mojo URLLoader proxy — and that proxy mishandles the
    // 302-redirect + cross-origin CORS hop that anti-bot challenge scripts all do
    // (Cloudflare Turnstile's api.js -> /v0/b/<hash>/api.js, Google reCAPTCHA,
    // Cloudflare's /cdn-cgi/challenge-platform/ jsd), failing them with
    // net::ERR_INVALID_ARGUMENT (-4). Proven by A/B --cef-shot: with a handler the
    // Turnstile/challenge scripts die at -4 and Cloudflare login pages loop; with
    // nullptr they load and execute. The handler only ever existed to log failures
    // (OnResourceLoadComplete) — a diagnostic that was silently breaking every
    // challenge-gated vendor login. So default to nullptr (challenges work) and
    // only opt back into the proxy under an EXPLICIT, dedicated env var. Do NOT
    // gate this on cefTrace(): tracing must not change request routing.
    // CHUDWARE_CEF_RESOURCE_HANDLER=1
    // re-enables the sub-resource failure logging when we're deliberately
    // diagnosing (and knowingly accept that challenge scripts break in that run).
    CefRefPtr<CefResourceRequestHandler> GetResourceRequestHandler(
        CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>, CefRefPtr<CefRequest>,
        bool, bool, const CefString&, bool&) override {
        const char* v = std::getenv("CHUDWARE_CEF_RESOURCE_HANDLER");
        return (v && v[0] == '1') ? this : nullptr;
    }

    // CefRenderHandler
    void GetViewRect(CefRefPtr<CefBrowser>, CefRect& rect) override {
        std::lock_guard<std::mutex> lk(s_->mtx);
        rect = CefRect(0, 0, s_->reqW > 0 ? s_->reqW : 1280,
                              s_->reqH > 0 ? s_->reqH : 800);
    }
    void OnPaint(CefRefPtr<CefBrowser>, PaintElementType type, const RectList&,
                 const void* buffer, int width, int height) override {
        const size_t n = static_cast<size_t>(width) * height * 4;
        if (type == PET_POPUP) {
            // <select> dropdown / autofill layer — keep it, composite in draw().
            std::lock_guard<std::mutex> lk(s_->mtx);
            s_->popupBgra.assign(static_cast<const unsigned char*>(buffer),
                                 static_cast<const unsigned char*>(buffer) + n);
            s_->popupW = width; s_->popupH = height; s_->popupDirty = true;
            return;
        }
        if (type != PET_VIEW) return;
        if (!tracedFirstPaint_) { tracedFirstPaint_ = true; CEF_TRACE("OnPaint FIRST %dx%d", width, height); }
        std::lock_guard<std::mutex> lk(s_->mtx);
        s_->bgra.assign(static_cast<const unsigned char*>(buffer),
                        static_cast<const unsigned char*>(buffer) + n);
        s_->paintW = width; s_->paintH = height; s_->dirty = true;
        if (!s_->active) { s_->active = true; s_->starting = false; }
    }
    // OSR popup geometry/visibility (the <select> dropdown layer).
    void OnPopupShow(CefRefPtr<CefBrowser>, bool show) override {
        std::lock_guard<std::mutex> lk(s_->mtx);
        s_->popupShown = show;
        if (!show) { s_->popupW = s_->popupH = 0; s_->popupDirty = true; }
    }
    void OnPopupSize(CefRefPtr<CefBrowser>, const CefRect& rect) override {
        std::lock_guard<std::mutex> lk(s_->mtx);
        s_->popupX = rect.x; s_->popupY = rect.y;
        s_->popupW = rect.width; s_->popupH = rect.height;
    }

    // CefLifeSpanHandler
    void OnAfterCreated(CefRefPtr<CefBrowser> browser) override {
        browser_ = browser;
        cef::note_browser_opened();   // frame loop pumps every frame while alive
        CEF_TRACE("OnAfterCreated (browser id=%d)", browser ? browser->GetIdentifier() : -1);
        // Copy-out: observe CDP replies so a Ctrl+C selection query can come back;
        // also carries the opt-in Network.responseReceived netlog (OnDevToolsEvent).
        if (browser) devtoolsReg_ = browser->GetHost()->AddDevToolsMessageObserver(this);
        // Align UA client-hints BEFORE draining pendingNav, or the first vendor page
        // loads with the raw "Chromium"-only Sec-CH-UA (the obvious-CEF tell). The
        // document-start fingerprint patches (window.chrome/plugins/languages/
        // webdriver/deviceMemory + WebGL spoof) are NOT injected here anymore: they
        // live in the ONE source of truth kInitScript (cef_stealth.cpp), injected in
        // the render process at OnContextCreated (cef_app.cpp) for EVERY frame — incl.
        // cross-origin reCAPTCHA iframes — which a browser-process CDP addScript can't
        // reach. The old per-view injectStealthScript() duplicated a SUBSET of that
        // blob (no WebGL spoof) and only the main frame, so it was redundant + a
        // divergence risk; removed. Sec-CH-UA still needs CDP, so applyUaClientHints stays.
        applyUaClientHints(browser);
        if (cefNetLog() && browser)
            browser->GetHost()->ExecuteDevToolsMethod(/*message_id=*/0, "Network.enable", nullptr);
        // Drain any navigation requested while the create was in flight.
        std::string pending;
        { std::lock_guard<std::mutex> lk(s_->mtx); pending.swap(s_->pendingNav); }
        if (!pending.empty() && browser) {
            CEF_TRACE("OnAfterCreated: draining pendingNav → LoadURL %s", pending.c_str());
            browser->GetMainFrame()->LoadURL(pending);
        }
    }

    // ANTI-DETECTION (Sec-CH-UA): CEF's native User-Agent header is a genuine
    // "Chrome/149.0.0.0" string, but its Sec-CH-UA client hints advertise only
    // "Chromium" + "Not)A;Brand" — MISSING the "Google Chrome" brand that a real
    // Chrome sends. That contradiction (UA says Chrome, hints say only Chromium)
    // is the single most obvious "this is CEF, not a browser" tell. Fix it the
    // same way the Playwright path does (stealth.cdp_ua_override_params): send
    // Network.setUserAgentOverride with the full brand list. We keep CEF's own
    // REDUCED UA (Chrome/<major>.0.0.0 — do NOT regress to a full version string)
    // and derive every version from CHROME_VERSION_* so it never drifts from the
    // bundled binary. NB: this is consistency, not mimicry — we assert the brands
    // real Chrome built from this same Chromium would send.
    void applyUaClientHints(CefRefPtr<CefBrowser> browser) {
        if (!browser) return;
        const std::string major = std::to_string(CHROME_VERSION_MAJOR);
        const std::string full  = major + "." + std::to_string(CHROME_VERSION_MINOR) +
                                  "." + std::to_string(CHROME_VERSION_BUILD) +
                                  "." + std::to_string(CHROME_VERSION_PATCH);
        const std::string reducedUa =
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/" + major + ".0.0.0 Safari/537.36";

        auto brand = [](const char* name, const std::string& ver) {
            CefRefPtr<CefDictionaryValue> d = CefDictionaryValue::Create();
            d->SetString("brand", name);
            d->SetString("version", ver);
            return d;
        };
        CefRefPtr<CefListValue> brands = CefListValue::Create();
        brands->SetDictionary(0, brand("Chromium", major));
        brands->SetDictionary(1, brand("Google Chrome", major));
        brands->SetDictionary(2, brand("Not)A;Brand", "24"));
        CefRefPtr<CefListValue> fullList = CefListValue::Create();
        fullList->SetDictionary(0, brand("Chromium", full));
        fullList->SetDictionary(1, brand("Google Chrome", full));
        fullList->SetDictionary(2, brand("Not)A;Brand", "24.0.0.0"));

        CefRefPtr<CefDictionaryValue> meta = CefDictionaryValue::Create();
        meta->SetList("brands", brands);
        meta->SetList("fullVersionList", fullList);
        meta->SetString("fullVersion", full);
        meta->SetString("platform", "Linux");
        meta->SetString("platformVersion", "");
        meta->SetString("architecture", "x86");
        meta->SetString("model", "");
        meta->SetBool("mobile", false);
        meta->SetString("bitness", "64");
        meta->SetBool("wow64", false);

        CefRefPtr<CefDictionaryValue> params = CefDictionaryValue::Create();
        params->SetString("userAgent", reducedUa);
        params->SetString("acceptLanguage", "en-US,en");   // unweighted; CDP adds q
        params->SetString("platform", "Linux x86_64");
        params->SetDictionary("userAgentMetadata", meta);
        browser->GetHost()->ExecuteDevToolsMethod(
            /*message_id=*/0, "Network.setUserAgentOverride", params);
        CEF_TRACE("applyUaClientHints: Sec-CH-UA brands aligned (Chrome %s)", major.c_str());
    }
    void OnBeforeClose(CefRefPtr<CefBrowser>) override {
        browser_ = nullptr;
        cef::note_browser_closed();
        std::function<void()> cb;
        { std::lock_guard<std::mutex> lk(s_->mtx); cb.swap(s_->onFullyClosed); }
        if (cb) cb();   // safe to CreateBrowser here: OnBeforeClose runs on TID_UI
    }
    // Keep popups (target=_blank etc.) in-frame: block the OS window, navigate the
    // main frame instead — the embedded panel has nowhere to put a second window.
    bool OnBeforePopup(CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame>, int /*popup_id*/,
                       const CefString& target_url, const CefString& /*target_frame_name*/,
                       CefLifeSpanHandler::WindowOpenDisposition, bool,
                       const CefPopupFeatures&, CefWindowInfo&,
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
            const std::string url = frame->GetURL().ToString();
            CEF_TRACE("OnLoadEnd http=%d %s", httpStatus, url.c_str());
            refreshLoginState(url);
            std::function<void(const std::string&, bool)> cb;
            { std::lock_guard<std::mutex> lk(s_->mtx); cb.swap(s_->onMainLoadEnd); }
            if (cb) cb(url, httpStatus >= 200 && httpStatus < 400);
        }
    }
    // A failed main-frame load (DNS, TLS, blocked, net error) previously left the
    // panel on "Loading…" forever with no signal. Surface it: record the error so
    // draw() shows it instead of a perpetual spinner, and clear `starting`.
    void OnLoadError(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame,
                     ErrorCode errorCode, const CefString& errorText,
                     const CefString& failedUrl) override {
        if (!frame->IsMain()) return;                 // sub-resource error — ignore
        // ERR_ABORTED = navigation superseded. Still release any armed one-shot
        // load-end waiter so multi-hop chains cannot hang forever.
        if (errorCode == ERR_ABORTED) {
            std::function<void(const std::string&, bool)> cb;
            { std::lock_guard<std::mutex> lk(s_->mtx); cb.swap(s_->onMainLoadEnd); }
            if (cb) cb(failedUrl.ToString(), false);
            return;
        }
        const std::string msg = errorText.ToString();
        CEF_TRACE("OnLoadError code=%d '%s' url=%s", (int)errorCode, msg.c_str(),
                  failedUrl.ToString().c_str());
        {
            std::lock_guard<std::mutex> lk(s_->mtx);
            s_->starting = false;
            s_->lastError = msg.empty() ? "page failed to load" : ("load error: " + msg);
        }
        // Still fire the one-shot so clearSession never hangs on a dead origin.
        std::function<void(const std::string&, bool)> cb;
        { std::lock_guard<std::mutex> lk(s_->mtx); cb.swap(s_->onMainLoadEnd); }
        if (cb) cb(failedUrl.ToString(), false);
    }
    void OnAddressChange(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame,
                         const CefString& url) override {
        if (frame->IsMain()) refreshLoginState(url);
    }

    // Console messages and source URLs are page-controlled, including "[RC]".
    // Record only that a console event occurred, never its payload/provenance.
    bool OnConsoleMessage(CefRefPtr<CefBrowser>, cef_log_severity_t,
                          const CefString&, const CefString&, int) override {
        CEF_TRACE("ConsoleMessage");
        return true; // Prevent CEF's default handler from retaining the raw text.
    }
    // CefResourceRequestHandler
    void OnResourceLoadComplete(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>,
                                CefRefPtr<CefRequest>,
                                CefRefPtr<CefResponse> response,
                                URLRequestStatus status,
                                int64_t /*received_content_length*/) override {
        if (!cefTrace()) return;
        const int http = response ? response->GetStatus() : -1;
        if (status == UR_SUCCESS && http < 400) return;   // only log failures
        CEF_TRACE("ResourceLoadFailed status=%d http=%d err=%d", (int)status,
                  http, response ? (int)response->GetError() : 0);
    }
    // CefDevToolsMessageObserver — copy-out reply: pull window.getSelection()
    // out of the Runtime.evaluate result and hand it to the GL thread.
    void OnDevToolsMethodResult(CefRefPtr<CefBrowser>, int message_id, bool success,
                                const void* result, size_t result_size) override {
        int want;
        { std::lock_guard<std::mutex> lk(s_->mtx); want = s_->copyMsgId; }
        if (!success || message_id != want || !result || result_size == 0) return;
        std::string json(static_cast<const char*>(result), result_size);
        CefRefPtr<CefValue> v = CefParseJSON(json, JSON_PARSER_RFC);
        if (!v || v->GetType() != VTYPE_DICTIONARY) return;
        CefRefPtr<CefDictionaryValue> d = v->GetDictionary();
        if (!d || !d->HasKey("result")) return;
        CefRefPtr<CefDictionaryValue> r = d->GetDictionary("result");
        if (!r || !r->HasKey("value")) return;
        const std::string text = r->GetString("value").ToString();
        std::lock_guard<std::mutex> lk(s_->mtx);
        s_->copyMsgId = -1;
        if (!text.empty()) s_->pendingClipboardOut = text;
    }

    // Opt-in network diagnostics use the same fixed retention cap. Neither
    // response bodies nor URL/query/userinfo/mime strings are retained.
    void OnDevToolsEvent(CefRefPtr<CefBrowser>, const CefString& method,
                         const void* params, size_t params_size) override {
        if (!cefNetLog() || method != "Network.responseReceived" || !params || !params_size)
            return;
        std::string json(static_cast<const char*>(params), params_size);
        CefRefPtr<CefValue> v = CefParseJSON(json, JSON_PARSER_RFC);
        if (!v || v->GetType() != VTYPE_DICTIONARY) return;
        CefRefPtr<CefDictionaryValue> d = v->GetDictionary();
        if (!d || !d->HasKey("response")) return;
        CefRefPtr<CefDictionaryValue> resp = d->GetDictionary("response");
        if (!resp) return;
        const std::string url = resp->GetString("url").ToString();
        const int status = resp->HasKey("status") ? resp->GetInt("status") : 0;
        // Use the URL only to classify this event; never publish it.
        auto has = [&](const char* s) { return url.find(s) != std::string::npos; };
        const bool interesting = has("recaptcha") || has("/login") ||
                                 has("customer/account") || has("/userverify") ||
                                 has("gstatic.com/recaptcha") || status >= 400;
        if (!interesting) return;
        CEF_TRACE("NetworkResponse status=%d", status);
    }

private:
    void refreshLoginState(const CefString& cefUrl) {
        const std::string url = cefUrl.ToString();
        std::lock_guard<std::mutex> lk(s_->mtx);
        s_->url = url;
        if (isAuthUrl(url)) {
            s_->loginState = "login_page";
        } else {
            // We deliberately do NOT infer "logged_in" from the URL. "Left the
            // sign-in page" is indistinguishable from an SSO/consent redirect or a
            // homepage bounce (false-positive skip), and modal logins never touch a
            // sign-in URL at all (false-negative miss). The user confirms sign-in
            // explicitly via the "I'm signed in" button; only that stamps the
            // server-side login. Chip stays neutral while they browse.
            s_->loginState = "browsing";
        }
    }

    std::shared_ptr<Shared> s_;
    CefRefPtr<CefBrowser> browser_;   // set in OnAfterCreated (CEF/UI thread)
    CefRefPtr<CefRegistration> devtoolsReg_;  // keeps the DevTools observer alive
    bool tracedFirstPaint_ = false;   // CHUDWARE_CEF_TRACE: log only the first OnPaint
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
    unsigned popupTex = 0;           // OSR <select> dropdown layer (composited over tex)
    int popupTexW = 0, popupTexH = 0;
    bool focused = false;
    bool invalidatedOnce_ = false;   // CHUDWARE_CEF_TRACE: log the first paint kick only

    void createBrowser(const std::string& url, const std::string& cacheDir, bool startedAtAuth) {
        CEF_TRACE("createBrowser url=%s cache=%s onUiThread=%d",
                  url.c_str(), cacheDir.c_str(), (int)CefCurrentlyOn(TID_UI));
        CEF_REQUIRE_UI_THREAD();   // CreateBrowser is TID_UI-affine (loud if a future change breaks it)
        // NB: the UA client-hints override (setUserAgentOverride) applies at the
        // browser level and covers the first page regardless of timing. The
        // addScriptToEvaluateOnNewDocument patches (deviceMemory cap) register in
        // OnAfterCreated, so they catch the SECOND+ document (vendor login always
        // navigates onward) but may miss the very first landing page. That was NOT
        // worth rerouting the load-bearing create path through about:blank —
        // a regression there breaks ALL vendor loading (a far bigger deal than the
        // minor deviceMemory tell). Keep the proven create-with-url path.
        { std::lock_guard<std::mutex> lk(s->mtx); s->startedAtAuth = startedAtAuth; }
        client = new Client(s);

        CefWindowInfo window_info;
        window_info.SetAsWindowless(0);          // OSR
        CefBrowserSettings settings;
        settings.windowless_frame_rate = 30;

        // Per-site request context → its own on-disk cookie jar (cacheDir aligns
        // with the Playwright profile dir; see docs/design/cef_webview.md).
        CefRefPtr<CefRequestContext> ctx;
        if (!cacheDir.empty()) {
            CefRequestContextSettings rc;
            CefString(&rc.cache_path) = cacheDir;
            ctx = CefRequestContext::CreateContext(rc, nullptr);
            CEF_TRACE("per-site request context %s", ctx ? "created" : "NULL (CreateContext failed)");
        }
        // Pump every frame until OnAfterCreated fires — CreateBrowser is async and
        // the deadline pump can starve the create window, wedging OnAfterCreated and
        // hanging the panel on "Loading". Paired with note_browser_opened (success)
        // / note_browser_create_failed (below) which settle this counter.
        cef::note_browser_creating();
        const bool ok =
            CefBrowserHost::CreateBrowser(window_info, client, url, settings, nullptr, ctx);
        CEF_TRACE("CreateBrowser returned %d", (int)ok);
        if (!ok) cef::note_browser_create_failed();
    }

    // Open `url`, REUSING the existing browser if one is already live.
    //
    // CRASH FIX (coredump-confirmed, SIGTRAP in RenderProcessHostImpl::
    // GetProcessHostForSiteInstance, PID 9723 + 16517, both during the SECOND
    // browser creation on a vendor switch): each vendor site was given its own
    // CefRequestContext with a distinct cache_path == a distinct StoragePartition,
    // which Chromium always assigns its OWN renderer process. Under --single-process
    // (this box's multi-process-ICU-handoff fallback, see cef_app.cpp) only ONE
    // renderer process may ever exist, so the second distinct context hits a fatal
    // CHECK — regardless of teardown sequencing (the earlier close/create deferral
    // did NOT help: the identical crash recurred after it shipped). The fix is to
    // never create a second browser: reuse the live one and LoadURL the new site.
    // Cross-domain navigation stays in the one process because single-process
    // disables site isolation, so this structurally cannot hit that CHECK. The
    // first context's cookie jar is reused for every vendor — cookies are
    // domain-scoped so vendors don't collide, and exportSession stages the live
    // per-domain jar into Playwright, which is where real per-vendor persistence
    // lives (CEF's jar is transient staging; see cache_root()).
    void openOrNavigate(const std::string& url, const std::string& cacheDir,
                        bool startedAtAuth) {
        // Marshal onto TID_UI when called off-thread. The original design assumed the
        // rpc_start_url callback that drives open()→doOpen always ran on the GL/UI
        // thread (== TID_UI under the external pump). Since the daemon-timeout IPC
        // rework the worker RPC callback can instead complete on an IO/worker thread,
        // and then createBrowser's CreateContext/CreateBrowser run off TID_UI —
        // CreateContext returns NULL → CreateBrowser 0 → OnAfterCreated never fires →
        // the panel hangs on "Loading" forever (root cause, confirmed in cef-trace:
        // every onUiThread=1 create succeeds, every onUiThread=0 fails). Capture the
        // shared state by value so the detached guard is safe even if the widget is
        // torn down before the hop runs (same-thread with ~Impl, so no data race).
        if (!CefCurrentlyOn(TID_UI)) {
            auto st = s;
            Impl* self = this;
            post_to_ui([self, st, url, cacheDir, startedAtAuth]() {
                if (st->detached) return;
                self->openOrNavigate(url, cacheDir, startedAtAuth);
            });
            return;
        }
        CEF_REQUIRE_UI_THREAD();
        if (client && client->browser()) {
            { std::lock_guard<std::mutex> lk(s->mtx); s->startedAtAuth = startedAtAuth; }
            CEF_TRACE("openOrNavigate: reuse live browser → LoadURL %s", url.c_str());
            client->browser()->GetMainFrame()->LoadURL(url);
            return;
        }
        if (client) {
            // createBrowser is in flight (browser_ not set until OnAfterCreated).
            // Stash the URL — OnAfterCreated navigates to it — instead of creating a
            // SECOND browser (fatal under --single-process; see pendingNav).
            std::lock_guard<std::mutex> lk(s->mtx);
            s->startedAtAuth = startedAtAuth;
            s->pendingNav = url;
            CEF_TRACE("openOrNavigate: create in flight → queue pendingNav %s", url.c_str());
            return;
        }
        createBrowser(url, cacheDir, startedAtAuth);
    }

    // Async CloseBrowser (force). Actual teardown completes later, when
    // Client::OnBeforeClose fires and clears browser_. Used only for full wizard
    // teardown / widget destruction — vendor SWITCHES no longer close+recreate
    // (see openOrNavigate). `then` (a one-shot fired from OnBeforeClose) is retained
    // for callers that must sequence work after teardown; the switch path no longer
    // uses it.
    void closeBrowser(std::function<void()> then = nullptr) {
        if (client && client->browser()) {
            { std::lock_guard<std::mutex> lk(s->mtx); s->onFullyClosed = std::move(then); }
            client->browser()->GetHost()->CloseBrowser(/*force=*/true);
        } else if (then) {
            then();   // nothing was open — safe to proceed immediately
        }
        client = nullptr;
    }

    void freeTexture() {
        if (tex) { glDeleteTextures(1, &tex); tex = 0; }
        if (popupTex) { glDeleteTextures(1, &popupTex); popupTex = 0; }
        popupTexW = popupTexH = 0;
        texW = texH = 0;
    }
};

// ---- public ------------------------------------------------------------------

bool CefWebView::processReady() { return cef::initialized(); }
// available() == "can still host a browser". Only false after a hard cold-init
// failure — while not yet initialized (lazy path) open() may still succeed.
// Wizard must NOT treat "not yet init" as dead, or multi-site Connect falls
// back to desktop before CEF has a chance.
bool CefWebView::available() const { return !cef::init_failed(); }

CefWebView::CefWebView() : impl_(std::make_unique<Impl>()) {}

CefWebView::~CefWebView() {
    { std::lock_guard<std::mutex> lk(impl_->s->mtx); impl_->s->detached = true; }
    impl_->closeBrowser();   // CloseBrowser(force); the Client (CefRefPtr) drains its own callbacks
    impl_->freeTexture();    // GL thread (widget-owned)
}

void CefWebView::open(AppState& st, const std::string& siteOrUrl, bool isUrl) {
    // Prefer cold-start initialize() in main (before GLFW). This is a safety
    // net for CHUDWARE_CEF_LAZY=1 / re-open after shutdown — late init after
    // GLFW is unsupported for vendor sign-in on Linux.
    if (!cef::initialize()) {
        std::lock_guard<std::mutex> lk(impl_->s->mtx);
        impl_->s->starting = false;
        impl_->s->active = false;
        impl_->s->lastError = "CEF failed to initialize";
        return;
    }
    impl_->pendingUrlReq.clear();
    impl_->invalidatedOnce_ = false;
    Impl* impl = impl_.get();
    auto& s = *impl_->s;
    {
        std::lock_guard<std::mutex> lk(s.mtx);
        s.active = false; s.starting = true;
        s.url.clear(); s.loginState = "unknown"; s.startedAtAuth = false;
        s.lastError.clear();
        s.bgra.clear(); s.paintW = s.paintH = 0; s.dirty = false;
    }

    AppState* stp = &st;   // AppState outlives every RPC/callback (app-lifetime object)
    auto doOpen = [impl, stp, siteOrUrl, isUrl]() {
        if (isUrl) {
            // Persistent panel profile even for raw URLs (was "" == in-memory/no persist).
            impl->openOrNavigate(siteOrUrl, panel_profile_dir(), isAuthUrl(siteOrUrl));
            std::lock_guard<std::mutex> lk(impl->s->mtx);
            impl->s->url = siteOrUrl;
            return;
        }

        // Vendor key → resolve its sign-in URL + per-site cache dir over the worker
        // RPC (reuses embed.start_url_for_site + profiles.profile_dir). The callback
        // runs on the GL/UI thread (the worker-RPC callback drain), so
        // CreateBrowser / LoadURL there is TID_UI-safe.
        const std::string req = siteOrUrl;
        impl->pendingUrlReq = req;
        CEF_TRACE("open(site=%s) → rpc browser_start_url…", siteOrUrl.c_str());
        cef::rpc_start_url(*stp, siteOrUrl, [impl, req](bool ok, const std::string& url,
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
            // One persistent panel profile for every vendor (see panel_profile_dir)
            // — accumulates challenge cookies across sessions.
            impl->openOrNavigate(url, panel_profile_dir(), isAuthUrl(url));
            std::lock_guard<std::mutex> lk(impl->s->mtx);
            impl->s->url = url;
        });
    };

    // Reuse the live browser (LoadURL) on a vendor switch instead of close+recreate:
    // a second per-site CefRequestContext would demand a second renderer process and
    // crash under --single-process. open() runs on the GL/UI thread (== TID_UI under
    // the external pump), so doOpen's CreateBrowser/LoadURL calls are thread-safe.
    doOpen();
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
    // CRASH FIX (coredump PID 47211: SIGTRAP in InProcessRendererThread →
    // InitializeBlink/InitializeWtf while the main thread is in ~CefBrowser). Under
    // --single-process, Blink/WTF initialises exactly ONCE per process; tearing the
    // browser down and creating another re-runs InitializeWtf in a fresh renderer
    // thread → fatal CHECK. Vendor SWITCHES already reuse the one browser (LoadURL);
    // the only remaining close+recreate was close()→next open(). So DON'T close:
    // keep the single browser alive for the whole process, just park it on
    // about:blank and mark inactive (stops frame uploads). Real teardown happens
    // only in ~CefWebView — the WizardState (and thus this widget) is a
    // function-static, so that runs at process exit where re-init can't happen.
    if (impl_->client && impl_->client->browser())
        impl_->client->browser()->GetMainFrame()->LoadURL("about:blank");
    auto& s = *impl_->s;
    std::lock_guard<std::mutex> lk(s.mtx);
    s.active = s.starting = false;
    s.url.clear();
    s.loginState = "unknown";
    s.startedAtAuth = false;
    s.pendingNav.clear();
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

// Completion for DeleteCookies — fires `done` once CEF reports the delete
// finished (UI thread). Also used as a one-shot after storage-clear JS.
class CookieDeleteDone : public CefDeleteCookiesCallback {
public:
    explicit CookieDeleteDone(std::function<void()> done) : done_(std::move(done)) {}
    void OnComplete(int /*num_deleted*/) override {
        if (done_) {
            auto d = std::move(done_);
            done_ = nullptr;
            d();
        }
    }
private:
    std::function<void()> done_;
    IMPLEMENT_REFCOUNTING(CookieDeleteDone);
};

void CefWebView::clearSession(AppState& /*st*/, std::function<void()> done) {
    // Multi-account OAuth isolation (Settings → Accounts "Add another"):
    // Delete x.ai cookies on the live request context, park on about:blank, and
    // invoke done. Prefer a reliable cookie wipe over multi-hop SPA navigations
    // (logout + auth.x.ai) that could miss OnLoadEnd / hang forever.
    //
    // HARD DEADLINE (~4s): if DeleteCookies OnComplete never arrives, still call
    // done() so OauthCapture cannot wedge on "Resetting browser session…".
    Client* c = impl_->client.get();
    if (!c || !c->browser()) {
        CEF_TRACE("clearSession: no live browser — nothing to wipe");
        if (done) done();
        return;
    }
    CEF_REQUIRE_UI_THREAD();
    CefRefPtr<CefBrowser> browser = c->browser();
    auto shared = impl_->s;
    // Drop any prior load-end waiter (stale hop from a previous wipe attempt).
    {
        std::lock_guard<std::mutex> lk(shared->mtx);
        shared->onMainLoadEnd = nullptr;
    }

    CefRefPtr<CefRequestContext> rctx = browser->GetHost()->GetRequestContext();
    CefRefPtr<CefCookieManager> mgr = rctx ? rctx->GetCookieManager(nullptr) : nullptr;

    // Once-only finish: timeout and cookie-complete race; first wins.
    auto finished = std::make_shared<std::atomic<bool>>(false);
    auto finish = [finished, done, browser](const char* why) {
        bool expected = false;
        if (!finished->compare_exchange_strong(expected, true)) return;
        CEF_TRACE("clearSession: done (%s)", why ? why : "?");
        if (browser && browser->GetMainFrame())
            browser->GetMainFrame()->LoadURL("about:blank");
        if (done) done();
    };

    // Delayed timeout task on TID_UI (same thread as DeleteCookies callbacks).
    class ClearSessionTimeout : public CefTask {
    public:
        explicit ClearSessionTimeout(std::function<void()> fn) : fn_(std::move(fn)) {}
        void Execute() override {
            if (fn_) {
                auto f = std::move(fn_);
                fn_ = nullptr;
                f();
            }
        }
    private:
        std::function<void()> fn_;
        IMPLEMENT_REFCOUNTING(ClearSessionTimeout);
    };
    constexpr int kClearSessionTimeoutMs = 4000;
    CefPostDelayedTask(TID_UI,
                       new ClearSessionTimeout([finish]() {
                           finish("timeout 4s — proceeding without full wipe");
                       }),
                       kClearSessionTimeoutMs);

    if (!mgr) {
        CEF_TRACE("clearSession: no cookie manager — finish immediately");
        finish("no cookie manager");
        return;
    }

    CEF_TRACE("clearSession: DeleteCookies for x.ai hosts only (preserve vendor jar; "
              "timeout %dms)",
              kClearSessionTimeoutMs);
    // Do NOT wipe the whole panel profile — it is shared with the Web tab and
    // Parts Library (Google/Cloudflare/reCAPTCHA trust). Scope to xAI auth hosts.
    static const char* kXaiCookieUrls[] = {
        "https://accounts.x.ai/",
        "https://auth.x.ai/",
        "https://x.ai/",
        "https://grok.x.ai/",
        "https://api.x.ai/",
        // Device approve may bounce through X's IdP; wipe those jars too so
        // "Add another" does not auto-bind the previous X session.
        "https://x.com/",
        "https://twitter.com/",
        "https://grok.com/",
    };
    const int nUrls = (int)(sizeof(kXaiCookieUrls) / sizeof(kXaiCookieUrls[0]));
    auto remaining = std::make_shared<int>(nUrls);
    for (int i = 0; i < nUrls; ++i) {
        CefRefPtr<CefDeleteCookiesCallback> cb = new CookieDeleteDone(
            [remaining, finish]() {
                if (--(*remaining) == 0)
                    finish("cookies deleted");
            });
        if (!mgr->DeleteCookies(CefString(kXaiCookieUrls[i]), CefString(), cb)) {
            // Refused — count it as done so we never hang on this host alone.
            if (--(*remaining) == 0)
                finish("cookies deleted (partial refuse)");
        }
    }
    mgr->FlushStore(nullptr);
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
                // CEF OSR routes keyboard to the renderer's focused frame ONLY after
                // SetFocus(true) — without it the page displays but form fields never
                // accept typing / caret (the "can't interact" bug). Assert focus on
                // the click that grabs it, drop it when the user clicks away.
                host->SetFocus(true);
                host->SendMouseClickEvent(me, kBtn[b], /*mouseUp=*/false, 1);
            } else {
                impl_->focused = false;
                host->SetFocus(false);
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

    // Ctrl/Cmd shortcuts (copy/cut/select-all/undo/redo) — send the VK so the
    // Chrome runtime performs the native action; suppress text below.
    //
    // PASTE (Ctrl+V) is special-cased: Chromium's own clipboard here is the
    // headless Ozone in-memory stub (OSR has no native window), so the VK path
    // can only paste what was copied inside a page — never from another app.
    // Pinning --ozone-platform=x11 to bridge it corrupted the heap on paste
    // (coredump PID 56925, "malloc(): corrupted top size"). Instead read the
    // HOST clipboard (ImGui → GLFW → native Wayland/X11, safe) and inject it
    // via CDP Input.insertText, which types into whatever has focus exactly
    // like a real paste — Chromium's clipboard is never touched.
    const bool cmd = io.KeyCtrl || io.KeySuper;
    if (cmd) {
        for (ImGuiKey k : {ImGuiKey_A, ImGuiKey_C, ImGuiKey_X,
                           ImGuiKey_Z, ImGuiKey_Y})
            if (ImGui::IsKeyPressed(k, false))
                if (int vk = vkForCtrlLetter(k)) sendVK(vk);
        // Copy-OUT bridge (mirror of paste): the VK above copies into Chromium's
        // in-memory stub clipboard, which never reaches the OS. Also ask the page
        // for its current selection; OnDevToolsMethodResult writes it to the HOST
        // clipboard so it can be pasted into other apps. Cut (X) copies too.
        if (ImGui::IsKeyPressed(ImGuiKey_C, false) ||
            ImGui::IsKeyPressed(ImGuiKey_X, false)) {
            CefRefPtr<CefDictionaryValue> ev = CefDictionaryValue::Create();
            ev->SetString("expression",
                          "(window.getSelection?window.getSelection().toString():'')");
            ev->SetBool("returnByValue", true);
            int id = host->ExecuteDevToolsMethod(0, "Runtime.evaluate", ev);
            if (id > 0) { std::lock_guard<std::mutex> lk(impl_->s->mtx); impl_->s->copyMsgId = id; }
        }
        if (ImGui::IsKeyPressed(ImGuiKey_V, false)) {
            const char* clip = ImGui::GetClipboardText();
            if (clip && clip[0]) {
                CefRefPtr<CefDictionaryValue> p = CefDictionaryValue::Create();
                p->SetString("text", clip);
                host->ExecuteDevToolsMethod(0, "Input.insertText", p);
                CEF_TRACE("paste: Input.insertText %d bytes", (int)std::strlen(clip));
            } else {
                // Host clipboard empty — fall back to the in-page stub paste so
                // page-internal copy → paste still works.
                if (int vk = vkForCtrlLetter(ImGuiKey_V)) sendVK(vk);
            }
        }
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
    bool popupShown = false;
    int  popupX = 0, popupY = 0, popupW = 0, popupH = 0;
    std::string lastError, clipOut;
    {
        std::lock_guard<std::mutex> lk(s.mtx);
        starting = s.starting; active = s.active; lastError = s.lastError;
        clipOut.swap(s.pendingClipboardOut);   // copy-out: page selection → host clipboard
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
        // Upload the <select> popup layer too (same BGRA→GL path).
        if (s.popupDirty && s.popupW > 0 && s.popupH > 0 && !s.popupBgra.empty()) {
            if (!impl_->popupTex) glGenTextures(1, &impl_->popupTex);
            glBindTexture(GL_TEXTURE_2D, impl_->popupTex);
            if (s.popupW != impl_->popupTexW || s.popupH != impl_->popupTexH) {
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
                glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, s.popupW, s.popupH, 0,
                             GL_BGRA, GL_UNSIGNED_BYTE, s.popupBgra.data());
                impl_->popupTexW = s.popupW; impl_->popupTexH = s.popupH;
            } else {
                glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, s.popupW, s.popupH,
                                GL_BGRA, GL_UNSIGNED_BYTE, s.popupBgra.data());
            }
            s.popupDirty = false;
        }
        popupShown = s.popupShown && s.popupW > 0 && s.popupH > 0;
        popupX = s.popupX; popupY = s.popupY; popupW = s.popupW; popupH = s.popupH;
        haveTex = impl_->tex && impl_->texW > 0 && impl_->texH > 0;
    }

    // Copy-out: push the page selection to the host (GLFW) clipboard on the GL
    // thread. ImGui's clipboard is GL-thread-only, so it can't run in the CEF
    // callback — draw() is the safe place.
    if (!clipOut.empty()) ImGui::SetClipboardText(clipOut.c_str());

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

    // Composite the <select> dropdown / autofill popup ON TOP of the page, mapped
    // from view pixels through the same letterbox scale so it lands over its
    // control. Input still flows through forwardInput → CEF, which drives the
    // popup selection; we only need to draw its pixels (the main view doesn't
    // contain them in OSR mode).
    if (popupShown && impl_->popupTex && impl_->popupTexW > 0) {
        const ImVec2 pp0(img0.x + popupX * scale, img0.y + popupY * scale);
        const ImVec2 pp1(pp0.x + popupW * scale, pp0.y + popupH * scale);
        dl->AddImage((ImTextureID)(intptr_t)impl_->popupTex, pp0, pp1);
        dl->AddRect(pp0, pp1, Palette::toU32(palette.textDisabled), 0.0f, 0, 1.0f);
    }

    ImGui::SetCursorScreenPos(p0);
    ImGui::Dummy(avail);
    return true;
}

// ---- browser-chrome navigation (GL/UI thread == TID_UI) ----------------------
// The panel calls these from draw(), which runs on the GL thread; under the
// external message pump that IS TID_UI, so the CefBrowser calls are thread-safe
// (same contract as openOrNavigate's LoadURL).
void CefWebView::reload(AppState&) {
    if (impl_->client && impl_->client->browser()) impl_->client->browser()->Reload();
}
void CefWebView::goBack(AppState&) {
    if (impl_->client && impl_->client->browser()) impl_->client->browser()->GoBack();
}
void CefWebView::goForward(AppState&) {
    if (impl_->client && impl_->client->browser()) impl_->client->browser()->GoForward();
}
bool CefWebView::canGoBack() const {
    return impl_->client && impl_->client->browser() && impl_->client->browser()->CanGoBack();
}
bool CefWebView::canGoForward() const {
    return impl_->client && impl_->client->browser() && impl_->client->browser()->CanGoForward();
}

// ---- accessors (small benign races on std::string across threads) ------------
const std::string& CefWebView::url() const        { return impl_->s->url; }
const std::string& CefWebView::loginState() const { return impl_->s->loginState; }
bool CefWebView::active() const                   { return impl_->s->active; }
bool CefWebView::starting() const                 { return impl_->s->starting; }
const std::string& CefWebView::lastError() const  { return impl_->s->lastError; }

}  // namespace ui
