// cef/cef_headless.cpp — see cef_headless.h. CEF-native replacement for the
// Playwright headless fetch/download. Compiled only with CHUDWARE_ENABLE_CEF.
#include "cef_headless.h"

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <map>
#include <memory>
#include <mutex>
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
#include "include/cef_request_handler.h"
#include "include/cef_resource_request_handler.h"
#include "include/cef_response.h"
#include "include/cef_string_visitor.h"
#include "include/cef_urlrequest.h"
#include "include/cef_request_context_handler.h"  // complete type for CreateContext

#include "cef_app.h"      // initialized()
#include "cef_stealth.h"  // apply_stealth()

namespace ui::cef {
namespace {

constexpr size_t kMaxHtmlBytes = 8 * 1024 * 1024;
constexpr size_t kMaxDownloadBytes = 200 * 1024 * 1024;

// Chromium's URLRequest path cannot bind a connection to our public-address
// snapshot. Tenant requests must use the broker-backed Python public HTTP client.
bool tenant_cell() {
    const char* value = std::getenv("CHUDWARE_CELL");
    return value && std::string(value) == "1";
}

// A per-site cookie jar for `profile_dir`, or the global context when empty. The
// cache_path must live under CefSettings.root_cache_path (CEF 149) — callers pass
// a dir under cef_platform::cache_root(), the same convention CefWebView uses.
//
// MEMOISED per dir: several browsers now run concurrently (see the pool below) and
// CEF is explicit that "when running in single-process mode there is only a single
// render process ... all browsers created in single-process mode will share the
// same request context ... the first request context passed into a CefBrowserHost
// static factory method and all other request context objects will be ignored"
// (include/cef_request_context.h). Handing out one context object per dir keeps
// that identity stable instead of minting ignored duplicates per CreateBrowser.
CefRefPtr<CefRequestContext> make_context(const std::string& profile_dir) {
    if (profile_dir.empty()) return CefRequestContext::GetGlobalContext();
    static std::map<std::string, CefRefPtr<CefRequestContext>> cache;
    CefRefPtr<CefRequestContext>& ctx = cache[profile_dir];
    if (ctx) return ctx;
    CefRequestContextSettings rc;
    CefString(&rc.cache_path) = profile_dir;
    ctx = CefRequestContext::CreateContext(rc, nullptr);
    if (!ctx) ctx = CefRequestContext::GetGlobalContext();
    return ctx;
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
    void Visit(const CefString& s) override {
        if (s.length() <= kMaxHtmlBytes / sizeof(char16_t)) {
            std::string html = s.ToString();
            if (html.size() <= kMaxHtmlBytes) html_ = std::move(html);
        }
        done_ = true;
    }
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
                    public CefLoadHandler,
                    public CefRequestHandler,
                    public CefResourceRequestHandler {
public:
    CefRefPtr<CefRenderHandler>   GetRenderHandler() override   { return this; }
    CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
    CefRefPtr<CefLoadHandler>     GetLoadHandler() override     { return this; }
    CefRefPtr<CefRequestHandler>  GetRequestHandler() override  { return this; }

    CefRefPtr<CefBrowser> browser() const { return browser_; }
    bool created() const { return created_; }
    bool loaded() const { return load_end_; }
    uint64_t document_generation() const { return document_generation_; }
    bool blank_loaded() const { return blank_loaded_; }
    bool committed2xx() {
        if (!load_started_) return false;
        std::lock_guard<std::mutex> lock(response_mutex_);
        return response_status_ >= 200 && response_status_ < 300;
    }
    int  status() const { return status_; }
    void copyResponse(FetchResult& out) {
        std::lock_guard<std::mutex> lock(response_mutex_);
        out.status = response_status_ ? response_status_ : status_;
        out.retry_after = retry_after_;
        out.final_url = final_url_;
    }

    // Point the reused browser at a new URL and arm a fresh load wait.
    void navigate(const std::string& url) {
        load_end_ = false;
        load_started_ = false;
        blank_loaded_ = false;
        status_ = 0;
        {
            std::lock_guard<std::mutex> lock(response_mutex_);
            recovering_ = (url == "about:blank");
            response_status_ = 0;
            retry_after_.clear();
            final_url_ = url;
        }
        if (browser_) if (CefRefPtr<CefFrame> f = browser_->GetMainFrame()) f->LoadURL(url);
    }

    // DIAGNOSTIC (login_probe): re-arm the load wait to catch a form-submit POST nav,
    // and run JS in the main frame via ExecuteJavaScript (no CDP/Runtime tell).
    void armLoadWait() { load_end_ = false; load_started_ = false; status_ = 0; }
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

    bool OnBeforeBrowse(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame,
                        CefRefPtr<CefRequest> request, bool, bool is_redirect) override {
        if (!frame->IsMain() || is_redirect) return false;
        load_started_ = false;
        load_end_ = false;
        std::lock_guard<std::mutex> lock(response_mutex_);
        if (!recovering_) {
            response_status_ = 0;
            retry_after_.clear();
            final_url_ = request->GetURL().ToString();
        }
        return false;
    }

    CefRefPtr<CefResourceRequestHandler> GetResourceRequestHandler(
        CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>, CefRefPtr<CefRequest> request,
        bool is_navigation, bool, const CefString&, bool&) override {
        return is_navigation && request->GetResourceType() == RT_MAIN_FRAME ? this : nullptr;
    }
    void OnResourceRedirect(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>,
                            CefRefPtr<CefRequest>, CefRefPtr<CefResponse> response,
                            CefString& new_url) override {
        std::lock_guard<std::mutex> lock(response_mutex_);
        if (recovering_) return;
        response_status_ = response->GetStatus();
        retry_after_ = response->GetHeaderByName("Retry-After").ToString();
        final_url_ = new_url.ToString();
    }


    bool OnResourceResponse(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>,
                            CefRefPtr<CefRequest> request,
                            CefRefPtr<CefResponse> response) override {
        std::lock_guard<std::mutex> lock(response_mutex_);
        if (recovering_) return false;
        response_status_ = response->GetStatus();
        retry_after_ = response->GetHeaderByName("Retry-After").ToString();
        final_url_ = response->GetURL().ToString();
        if (final_url_.empty()) final_url_ = request->GetURL().ToString();
        return false;
    }

    // OnLoadStart is the commit barrier: a late OnLoadEnd from the previous
    // document must never make a freshly requested URL eligible for capture.
    void OnLoadStart(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame,
                     TransitionType) override {
        if (!frame->IsMain()) return;
        ++document_generation_;
        load_end_ = false;
        const std::string url = frame->GetURL().ToString();
        std::lock_guard<std::mutex> lock(response_mutex_);
        started_blank_ = recovering_;
        load_started_ = started_blank_ ? url == "about:blank"
                                       : url.rfind("about:", 0) != 0;
    }

    void OnLoadEnd(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame, int http) override {
        if (!frame->IsMain()) return;
        const std::string url = frame->GetURL().ToString();
        if (!load_started_) return;
        if (started_blank_) {
            if (url != "about:blank") return;
            blank_loaded_ = true;
        } else {
            status_ = http;
            load_end_ = true;
        }
    }

private:
    bool created_ = false;
    bool load_end_ = false;
    bool load_started_ = false;
    bool blank_loaded_ = false;
    uint64_t document_generation_ = 0;
    bool started_blank_ = false;
    int  status_ = 0;
    std::mutex response_mutex_;
    bool recovering_ = true;
    int response_status_ = 0;
    std::string retry_after_;
    std::string final_url_ = "about:blank";
    CefRefPtr<CefBrowser> browser_;
    CefRefPtr<CefRegistration> stealth_reg_;  // keeps the DevTools agent attached
    IMPLEMENT_REFCOUNTING(FetchClient);
};

// CefURLRequestClient that buffers a response body for headless_download.
class DownloadClient : public CefURLRequestClient {
public:
    bool done() const { return done_; }
    const std::vector<unsigned char>& bytes() const { return bytes_; }
    bool too_large() const { return too_large_; }
    bool idle() const {
        return std::chrono::steady_clock::now() - last_data_ > std::chrono::seconds(30);
    }

    void OnRequestComplete(CefRefPtr<CefURLRequest>) override { done_ = true; }
    void OnDownloadData(CefRefPtr<CefURLRequest> request, const void* data, size_t n) override {
        if (too_large_) return;
        last_data_ = std::chrono::steady_clock::now();
        if (n > kMaxDownloadBytes - bytes_.size()) {
            too_large_ = true;
            bytes_.clear();
            request->Cancel();
            return;
        }
        const auto* p = static_cast<const unsigned char*>(data);
        bytes_.insert(bytes_.end(), p, p + n);
    }
    void OnUploadProgress(CefRefPtr<CefURLRequest>, int64_t, int64_t) override {}
    void OnDownloadProgress(CefRefPtr<CefURLRequest> request, int64_t, int64_t total) override {
        if (total > static_cast<int64_t>(kMaxDownloadBytes)) {
            too_large_ = true;
            bytes_.clear();
            request->Cancel();
        }
    }
    bool GetAuthCredentials(bool, const CefString&, int, const CefString&,
                            const CefString&, CefRefPtr<CefAuthCallback>) override {
        return false;  // never auto-answer HTTP auth
    }

private:
    bool done_ = false;
    bool too_large_ = false;
    std::chrono::steady_clock::time_point last_data_ = std::chrono::steady_clock::now();
    std::vector<unsigned char> bytes_;
    IMPLEMENT_REFCOUNTING(DownloadClient);
};

}  // namespace

// ---------------------------------------------------------------------------
// Browser pool + cooperative fetch jobs.
//
// THREAD MODEL — why concurrency is shaped this way and not as worker threads.
// CefInitialize runs with multi_threaded_message_loop=0 and
// external_message_pump=1 (cef_app.cpp), and CefDoMessageLoopWork() "should only
// be called on the main application thread ... and only if CefInitialize() is
// called with a multi_threaded_message_loop value of false" (include/cef_app.h).
// So exactly ONE thread may ever pump, and a fetch can NEVER be moved onto a
// worker thread. Overlap therefore comes from N browsers sharing that one pump,
// which CEF explicitly permits even in the --single-process mode cef_app.cpp
// selects (include/cef_request_context.h: all such browsers simply share the
// first request context, the rest being ignored — hence make_context's memo).
//
// Browsers are created once and NEVER closed until CefShutdown: closing the last
// one tears the single-process renderer thread down and the next CreateBrowser
// re-runs InitializeWtf in a fresh renderer thread → fatal CHECK (MEMORY
// cef-gpu-thread-sigtrap-nvidia). The pool only ever grows, to a hard cap.
//
// Everything below is main-thread-only; no locking, by construction.
// ---------------------------------------------------------------------------
namespace {

using steady = std::chrono::steady_clock;

long long ms_between(steady::time_point a, steady::time_point b) {
    return std::chrono::duration_cast<std::chrono::milliseconds>(b - a).count();
}

int env_int(const char* name, int def) {
    if (const char* v = std::getenv(name); v && v[0]) {
        char* end = nullptr;
        const long n = std::strtol(v, &end, 10);
        if (end && *end == '\0') return static_cast<int>(n);
    }
    return def;
}

bool env_on(const char* name, bool def) {
    const char* v = std::getenv(name);
    if (!v || !v[0]) return def;
    return !(v[0] == '0' && v[1] == '\0');
}

// Matches the old 500 × pump(20) navigation wait and 200 × pump(10) GetSource wait.
constexpr int kNavTimeoutMs     = 10000;
constexpr int kCaptureTimeoutMs = 2000;
// Adaptive-settle sample period, and how long an ABANDONED browser is parked
// before anyone may reuse it (see release_slot).
constexpr int kProbeSliceMs     = 100;
constexpr int kAbortCooldownMs  = 5000;

bool adaptive_settle() {
    static const bool on = env_on("CHUDWARE_CEF_ADAPTIVE_SETTLE", true);
    return on;
}
// Floor: never end a settle before this, however quiet the page looks.
int min_settle_ms() {
    static const int v = [] { const int n = env_int("CHUDWARE_CEF_MIN_SETTLE_MS", 400);
                              return n < 0 ? 0 : n; }();
    return v;
}
// How long the serialized DOM must be BYTE-IDENTICAL before the settle may end
// early. This is the whole safety margin of the adaptive path, so it is not a
// couple of sample slices: a page that appends its content in bursts (product
// cards arriving from staggered XHRs) sits momentarily still between bursts, and
// a short window walks away with a truncated document — measured, with a local
// page appending at t=200/1500/3000ms: a 2-slice window returned the t=200 state
// and dropped the rest. A window this size costs at most `quiet_ms` of the
// budget and provably cannot end the dwell while the DOM is still moving.
int quiet_ms() {
    static const int v = [] { const int n = env_int("CHUDWARE_CEF_QUIET_MS", 2000);
                              return n < 0 ? 0 : n; }();
    return v;
}

// Cheap content fingerprint (FNV-1a 64). Length alone is not a change detector —
// a same-length DOM mutation must count as movement.
uint64_t dom_hash(const std::string& s) {
    uint64_t h = 0xCBF29CE484222325ULL;
    for (const unsigned char c : s) { h ^= c; h *= 0x100000001B3ULL; }
    return h;
}

// An abandoned browser is parked until both its abort cooldown and the
// about:blank commit/load barrier have completed. A timer alone cannot prove
// that callbacks or the previous document have quiesced.
struct PoolEntry {
    CefRefPtr<FetchClient> client;
    bool busy = false;
    steady::time_point free_at{};
};

std::map<std::string, std::vector<PoolEntry>>& pool_map() {
    static std::map<std::string, std::vector<PoolEntry>> m;
    return m;
}

// Create + seed a browser at about:blank. Pumps inline, so main thread only.
CefRefPtr<FetchClient> spawn_client(const std::string& profile_dir) {
    CefRefPtr<FetchClient> client = new FetchClient();
    CefWindowInfo window_info;
    window_info.SetAsWindowless(0);
    CefBrowserSettings settings;
    settings.windowless_frame_rate = 30;
    // Seed at about:blank so the render-process stealth injection is live before
    // the first real navigation.
    if (!CefBrowserHost::CreateBrowser(window_info, client, "about:blank", settings,
                                       nullptr, make_context(profile_dir)))
        return nullptr;
    for (int i = 0; i < 500 && (!client->created() || !client->blank_loaded()); ++i) pump(20);
    return client->created() && client->blank_loaded() ? client : nullptr;
}

// Reserve an idle browser, growing the pool on demand. Returns the entry index,
// or -1 when every browser is busy/cooling, the cap is reached, or the create
// failed — the caller queues and retries. Main thread only (may pump).
int acquire_slot(const std::string& profile_dir) {
    std::vector<PoolEntry>& v = pool_map()[profile_dir];
    const steady::time_point now = steady::now();
    for (size_t i = 0; i < v.size(); ++i)
        if (!v[i].busy && now >= v[i].free_at &&
            (v[i].free_at == steady::time_point{} || v[i].client->blank_loaded())) {
            v[i].busy = true;
            v[i].free_at = {};
            return static_cast<int>(i);
        }
    // Headroom above the in-flight cap for entries still cooling after an abort.
    if (static_cast<int>(v.size()) >= headless_max_inflight() + 4) return -1;
    CefRefPtr<FetchClient> c = spawn_client(profile_dir);
    if (!c) return -1;
    v.push_back(PoolEntry{c, true, {}});
    return static_cast<int>(v.size() - 1);
}

// Every slot crosses a completed about:blank navigation before reuse. Dirty
// slots also wait out the abort cooldown.
void release_slot(const std::string& profile_dir, int idx, bool dirty) {
    auto it = pool_map().find(profile_dir);
    if (it == pool_map().end()) return;
    std::vector<PoolEntry>& v = it->second;
    if (idx < 0 || idx >= static_cast<int>(v.size())) return;
    if (v[idx].client) {
        v[idx].client->navigate("about:blank");
        v[idx].free_at = steady::now() +
                         std::chrono::milliseconds(dirty ? kAbortCooldownMs : 0);
    }
    v[idx].busy = false;
}

// RAII exclusive browser for the SYNCHRONOUS entry points (interact,
// login_probe), so they can never share a browser with an in-flight fetch job.
// Always released dirty: both run caller JS and leave arbitrary page state.
class SlotGuard {
public:
    explicit SlotGuard(std::string profile) : profile_(std::move(profile)) {
        idx_ = acquire_slot(profile_);
        if (idx_ >= 0) client_ = pool_map()[profile_][static_cast<size_t>(idx_)].client;
    }
    ~SlotGuard() { if (idx_ >= 0) release_slot(profile_, idx_, /*dirty=*/true); }
    SlotGuard(const SlotGuard&) = delete;
    SlotGuard& operator=(const SlotGuard&) = delete;
    bool ok() const { return idx_ >= 0 && client_; }
    const CefRefPtr<FetchClient>& client() const { return client_; }
private:
    std::string profile_;
    int idx_ = -1;
    CefRefPtr<FetchClient> client_;
};

}  // namespace

int headless_max_inflight() {
    static const int n = [] {
        int v = env_int("CHUDWARE_CEF_MAX_INFLIGHT", 6);
        if (v < 1) v = 1;
        if (v > 16) v = 16;   // pool cap + Chromium per-renderer view budget
        return v;
    }();
    return n;
}

void headless_pump(int ms) { pump(ms); }

// One in-flight fetch. step() is non-blocking and NEVER pumps: the driver pumps
// once for the whole set, then steps every job, so N requests share one dwell.
class FetchJob {
public:
    FetchJob(std::string profile, int slot, CefRefPtr<FetchClient> client,
             std::string url, int settle_ms)
        : profile_(std::move(profile)), slot_(slot), client_(std::move(client)),
          url_(std::move(url)), settle_ms_(settle_ms < 0 ? 0 : settle_ms),
          t_state_(steady::now()) {}
    ~FetchJob() {
        if (!released_) { visitor_ = nullptr; release_slot(profile_, slot_, true); }
    }
    FetchJob(const FetchJob&) = delete;
    FetchJob& operator=(const FetchJob&) = delete;

    void begin() { client_->navigate(url_); t_state_ = steady::now(); }

    // Returns true once finished (either outcome).
    bool step() {
        if (state_ == State::Finished) return true;
        const steady::time_point now = steady::now();
        // A page may navigate itself while settling or while GetSource is in
        // flight. Discard that snapshot and wait for the new committed document
        // instead of either returning stale HTML or failing a valid redirect.
        if ((state_ == State::Settling || state_ == State::Capturing) &&
            (!client_->loaded() || client_->document_generation() != settle_generation_)) {
            visitor_ = nullptr;
            last_hash_ = 0;
            state_ = State::Navigating;
            t_state_ = now;
        }
        switch (state_) {
        case State::Navigating:
            if (client_->loaded()) {
                t_settle_ = now;
                t_probe_  = now;
                settle_generation_ = client_->document_generation();
                t_quiet_  = now;
                state_    = State::Settling;
                if (settle_ms_ == 0) { state_ = State::Capturing; t_state_ = now; beginProbe(); }
            } else if (ms_between(t_state_, now) >= kNavTimeoutMs) {
                if (!client_->committed2xx()) { finishFail(); break; }
                state_ = State::Partial;
                t_state_ = now;
                probe_generation_ = client_->document_generation();
                partial_token_ = std::to_string(reinterpret_cast<uintptr_t>(this)) + "-" +
                                 std::to_string(probe_generation_);
                beginPartialProbe();
            }
            break;

        case State::Settling: {
            const long long elapsed = ms_between(t_settle_, now);
            if (visitor_) {
                if (visitor_->done()) {
                    const std::string html = visitor_->html();
                    visitor_ = nullptr;
                    const uint64_t h = dom_hash(html);
                    if (h != last_hash_) { last_hash_ = h; t_quiet_ = now; }
                    // Early-out: the serialized DOM has been byte-identical for the
                    // whole quiet window, CEF has reported load-end, and the floor
                    // has elapsed. This probe IS the capture — the same GetSource
                    // path the ceiling uses, so the document returned here is one
                    // the flat dwell would also have returned.
                    if (ms_between(t_quiet_, now) >= quiet_ms() &&
                        elapsed >= min_settle_ms() && client_->loaded()) {
                        finishOk(html);
                        break;
                    }
                } else if (ms_between(t_probe_, now) >= kCaptureTimeoutMs) {
                    visitor_ = nullptr;   // hung probe: drop it and keep dwelling
                    t_quiet_ = now;
                }
            }
            if (elapsed >= settle_ms_) { state_ = State::Capturing; t_state_ = now; beginProbe(); }
            else if (adaptive_ && !visitor_ && ms_between(t_probe_, now) >= kProbeSliceMs)
                beginProbe();
            break;
        }

        case State::Capturing:
            if (!visitor_) finishFail();
            else if (visitor_->done()) finishOk(visitor_->html());
            else if (ms_between(t_state_, now) >= kCaptureTimeoutMs) finishFail();
            break;

        case State::Partial:
            if (client_->document_generation() != probe_generation_ || !client_->committed2xx()) {
                finishFail();
            } else if (visitor_ && visitor_->done()) {
                std::string html = visitor_->html();
                visitor_ = nullptr;
                const std::string marker = " data-chudware-partial-ready=\"" + partial_token_ + "\"";
                const size_t pos = html.find(marker);
                if (pos != std::string::npos) {
                    html.erase(pos, marker.size());
                    finishOk(html, /*partial=*/true);
                }
            }
            if (state_ == State::Partial) {
                if (ms_between(t_state_, now) >= kCaptureTimeoutMs) finishFail();
                else if (!visitor_ && ms_between(t_probe_, now) >= kProbeSliceMs)
                    beginPartialProbe();
            }
            break;

        case State::Finished:
            break;
        }
        return state_ == State::Finished;
    }

    // Harvest + release. Aborts an unfinished job (deadline path).
    FetchResult take() {
        if (released_) return result_;
        if (state_ != State::Finished) finishFail();
        visitor_  = nullptr;
        released_ = true;
        release_slot(profile_, slot_, dirty_);
        return result_;
    }

private:
    enum class State { Navigating, Settling, Capturing, Partial, Finished };

    void beginProbe() {
        if (visitor_) return;
        CefRefPtr<CefBrowser> b = client_->browser();
        CefRefPtr<CefFrame> f = b ? b->GetMainFrame() : nullptr;
        if (!f) { finishFail(); return; }
        if (!client_->loaded()) { finishFail(); return; }
        probe_generation_ = client_->document_generation();
        visitor_ = new SourceVisitor();
        f->GetSource(visitor_);           // async — completes under the shared pump
        t_probe_ = steady::now();
    }
    void beginPartialProbe() {
        CefRefPtr<CefBrowser> b = client_->browser();
        CefRefPtr<CefFrame> f = b ? b->GetMainFrame() : nullptr;
        if (!f) { finishFail(); return; }
        client_->runJs("if (document.readyState !== 'loading' && document.documentElement) "
                       "document.documentElement.setAttribute('data-chudware-partial-ready','" +
                       partial_token_ + "');");
        visitor_ = new SourceVisitor();
        f->GetSource(visitor_);
        t_probe_ = steady::now();
    }

    void clearPartialMarker() {
        client_->runJs("if (document.documentElement) "
                       "document.documentElement.removeAttribute('data-chudware-partial-ready');");
    }

    void finishOk(const std::string& html, bool partial = false) {
        if ((!partial && !client_->loaded()) || client_->document_generation() != probe_generation_ ||
            (partial && !client_->committed2xx())) {
            finishFail();
            return;
        }
        if (partial) {
            clearPartialMarker();
            dirty_ = true;  // unfinished subresources must be canceled before reuse
            result_.partial_load = true;
        }
        result_.ok     = true;
        result_.html   = html;
        client_->copyResponse(result_);
        state_ = State::Finished;
    }

    void finishFail() {
        if (state_ == State::Partial) clearPartialMarker();
        client_->copyResponse(result_);
        dirty_  = true;      // a load may still be running — cancel + cool the browser
        state_  = State::Finished;
    }

    std::string profile_;
    std::string partial_token_;
    uint64_t    settle_generation_ = 0;
    uint64_t    probe_generation_ = 0;
    int         slot_ = -1;
    CefRefPtr<FetchClient> client_;
    std::string url_;
    int         settle_ms_ = 0;
    State       state_ = State::Navigating;
    bool        adaptive_ = adaptive_settle();
    bool        dirty_ = false;
    bool        released_ = false;
    steady::time_point t_state_;   // entered current state (nav / capture timeout)
    steady::time_point t_settle_;  // settle window opened
    steady::time_point t_probe_;   // last GetSource issued
    steady::time_point t_quiet_;   // last time the serialized DOM actually changed
    CefRefPtr<SourceVisitor> visitor_;
    uint64_t    last_hash_ = 0;    // 0 = no probe yet; first probe always counts as change
    FetchResult result_;
};

FetchJobPtr headless_fetch_start(const std::string& url, int settle_ms,
                                 const std::string& profile_dir) {
    if (tenant_cell() || !initialized()) return nullptr;
    const int slot = acquire_slot(profile_dir);
    if (slot < 0) return nullptr;
    FetchJobPtr job = std::make_shared<FetchJob>(
        profile_dir, slot, pool_map()[profile_dir][static_cast<size_t>(slot)].client,
        url, settle_ms);
    job->begin();
    return job;
}

bool headless_fetch_poll(const FetchJobPtr& job) { return !job || job->step(); }

FetchResult headless_fetch_take(const FetchJobPtr& job) {
    return job ? job->take() : FetchResult{};
}

FetchResult headless_fetch(const std::string& url, int settle_ms,
                           const std::string& profile_dir) {
    FetchJobPtr job = headless_fetch_start(url, settle_ms, profile_dir);
    if (!job) return FetchResult{};
    while (!headless_fetch_poll(job)) pump(10);
    return headless_fetch_take(job);
}

FetchResult headless_login_probe(const std::string& url, const std::string& fill_submit_js,
                                 int settle_ms, int post_wait_ms,
                                 const std::string& profile_dir) {
    FetchResult out;
    if (tenant_cell() || !initialized()) return out;

    // Exclusive browser for the whole probe — it runs caller JS and synthetic
    // input, so it must never share a browser with an in-flight fetch job.
    SlotGuard slot(profile_dir);
    if (!slot.ok()) return out;
    const CefRefPtr<FetchClient>& client = slot.client();

    client->navigate(url);
    for (int i = 0; i < 500 && !client->loaded(); ++i) pump(20);
    if (!client->loaded()) { client->copyResponse(out); return out; }
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
            }
        }
    }
    client->copyResponse(out);
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
    if (tenant_cell() || !initialized()) return out;

    // Exclusive browser for the whole interaction — it runs caller JS, so it must
    // never share a browser with an in-flight fetch job.
    SlotGuard slot(profile_dir);
    if (!slot.ok()) return out;
    const CefRefPtr<FetchClient>& client = slot.client();

    client->navigate(url);
    for (int i = 0; i < 500 && !client->loaded(); ++i) pump(20);
    if (!client->loaded()) { client->copyResponse(out); return out; }

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
    }
    client->copyResponse(out);
    return out;
}

void headless_close_all() {
    for (auto& [profile, entries] : pool_map())
        for (PoolEntry& e : entries)
            if (e.client && e.client->browser())
                e.client->browser()->GetHost()->CloseBrowser(true);
    for (int i = 0; i < 50; ++i) pump(10);  // let OnBeforeClose settle
    pool_map().clear();
}

DownloadResult headless_download(const std::string& url, const std::string& dest_path,
                                 const std::string& profile_dir) {
    DownloadResult out;
    out.final_url = url;
    if (tenant_cell() || !initialized()) return out;

    CefRefPtr<CefRequest> request = CefRequest::Create();
    request->SetURL(url);
    request->SetMethod("GET");
    request->SetFlags(UR_FLAG_SKIP_CACHE | UR_FLAG_ALLOW_STORED_CREDENTIALS);

    CefRefPtr<DownloadClient> client = new DownloadClient();
    // Per-site (or global) context = the profile's cookie jar, so authenticated
    // downloads carry the login session.
    CefRefPtr<CefURLRequest> req =
        CefURLRequest::Create(request, client, make_context(profile_dir));

    if (!req) return out;
    while (!client->done() && !client->idle()) pump(10);
    if (CefRefPtr<CefResponse> resp = req->GetResponse()) {
        out.status = resp->GetStatus();
        out.retry_after = resp->GetHeaderByName("Retry-After").ToString();
        const std::string resolved = resp->GetURL().ToString();
        if (!resolved.empty()) out.final_url = resolved;
    }
    if (!client->done()) { req->Cancel(); return out; }
    if (client->too_large() || req->GetRequestStatus() != UR_SUCCESS) return out;
    if (out.status < 200 || out.status >= 300 || client->bytes().empty()) return out;

    std::ofstream f(dest_path, std::ios::binary);
    if (!f) return out;
    f.write(reinterpret_cast<const char*>(client->bytes().data()),
            static_cast<std::streamsize>(client->bytes().size()));
    out.ok = static_cast<bool>(f);
    return out;
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
    if (tenant_cell() || !initialized() || cookies.empty()) return 0;
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
