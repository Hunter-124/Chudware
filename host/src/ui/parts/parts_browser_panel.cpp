// Parts Library — embedded vendor-browser panel + sign-in wizard (GUI V2 phase C).
//
// drawPartsLibraryWorkspace swaps the results column for this panel while
// browserPanelActive() is true. The panel hosts a ui::WebView attached to the worker's
// login-wizard embed session (login_wizard_* RPCs) with a top bar: current site + step,
// clipped URL, login-state chip, and Skip / Next / Close actions.
//
// The worker AUTO-ADVANCES the wizard when a site reaches logged_in — the embed_id
// changes under us, so the 2 s status poll watches for it and re-attaches the WebView.
// All RPC is async via st.rpc (GL-freeze history); polls are single-flight + backed off.
#include <algorithm>
#include <cstdio>
#include <string>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/tesseract.h"  // ui::drawTesseractSpinner — the brand mark IS the busy indicator

#include "ui/widgets/web_view_backend.h"
#include "ui/widgets/cef/cef_trace_file.h"
#include "ui/web_capabilities.h"  // ui::webGapReason (T-RF12: vendor sign-in is desktop-only on web)

#include "parts_internal.h"
#include "parts_widgets.h"

// Wizard diagnostics share the explicit opt-in, bounded CEF event sink.
#define WIZ_TRACE(...)                                                       \
    do {                                                                    \
        if (::ui::cef::trace_enabled()) {                                    \
            char _wb[512];                                                  \
            std::snprintf(_wb, sizeof(_wb), "[wizard] " __VA_ARGS__);          \
            ::ui::cef::trace_file_line(_wb);                                 \
        }                                                                   \
    } while (0)

namespace ui {
namespace parts {

// Null-tolerant string read. Client-mode wizard payloads carry embed_id /
// current_site as JSON null (Python None); nlohmann::json::value(key, "")
// THROWS type_error.302 on a present-but-null key (null → std::string is not
// convertible), which — uncaught in an st.rpc drain callback — aborts the whole
// GUI. Treat null / missing / non-string uniformly as the default: EVERY string
// read of a worker response in this TU and in parts_vendors.cpp goes through here.
// Deliberately outside the anonymous namespace below so parts_vendors.cpp can
// forward-declare it and share the one definition.
std::string jstr(const nlohmann::json& j, const char* key,
                 const std::string& dflt = "") {
    if (!j.is_object()) return dflt;
    auto it = j.find(key);
    if (it == j.end() || !it->is_string()) return dflt;
    return it->get<std::string>();
}

// Magento MSP invisible reCAPTCHA (and similar) reject in-panel OSR CEF under
// software GL / automation. Mirror services/browser NATIVE_CHROME_LOGIN sites:
// open real system Chrome, export cookies → Playwright profile + CEF warm so
// headless browser_fetch / CEF daemon can reuse the session.
// External linkage for the same reason as jstr: parts_vendors.cpp's vendor rows
// forward-declare it so this bot-check rule has exactly one definition.
bool sitePrefersDesktopLogin(const std::string& site) {
    return site == "anaheimautomation";
}

namespace {

struct WizardState {
    // Backend-agnostic (screencast today, CEF later) — see web_view_backend.h.
    std::unique_ptr<ui::IWebViewBackend> web = ui::makeWebView();
    bool active     = false;   // login_wizard session live on the worker
    bool starting   = false;   // login_wizard_start in flight
    bool singleSite = false;   // launched from one vendor row's "Sign in"
    std::string currentSite;
    std::string embedId;
    std::vector<std::string> done, skipped, remaining;
    bool   statusPending = false, actionPending = false;
    double lastStatusAt = 0.0;
    // Vendor rows this wizard marked loginInProgress (the embedded flow mirroring
    // what headed vendor_login does). loadVendors() rewrites the flag from the
    // worker — which only tracks headed login threads — so drawBrowserPanel
    // re-asserts this set every frame and every exit path releases it.
    std::vector<std::string> busySites;
    // Load watchdog: when the current site's page was (re)opened, and whether we
    // already toasted a timeout for it. Lets the panel surface a stall + Retry
    // instead of leaving the user on an eternal "Loading".
    double siteOpenedAt = 0.0;
    bool   loadTimedOut = false;
    std::string setupError;   // embed backend failed — rendered in-panel
    std::string setupHint;
    // Editable URL bar (CEF nav chrome). Mirrors web->url() while the field is
    // idle; while the user is typing we leave their text alone until Enter.
    char urlBuf[1024] = {0};
    bool urlEditing   = false;
};

// How long a vendor page may sit without its first paint before we call it stalled.
constexpr double kLoadTimeoutSec = 25.0;

WizardState& W() {
    static WizardState w;
    return w;
}

std::string vendorLabel(const std::string& site) {
    for (const auto& v : S().vendors)
        if (v.id == site) return v.label;
    return site;
}

// ---- vendor-row busy bookkeeping -----------------------------------------------------
// The embedded wizard must mark the rows it owns loginInProgress, exactly like the
// headed vendor_login flow: drawVendorStrip's re-poll is edge-triggered on that flag,
// and the popup's per-row "Sign in" button uses it to render disabled instead of
// dead-clicking into startLoginWizard's already-active guard.

void applyWizardBusy() {
    auto& b = W().busySites;
    if (b.empty()) return;
    for (auto& v : S().vendors)
        if (std::find(b.begin(), b.end(), v.id) != b.end()) v.loginInProgress = true;
}

// Release one row (handover to the headed flow, whose own bookkeeping then owns the
// flag) or — with an empty site — every row this wizard still holds.
void clearWizardBusy(const std::string& site = std::string()) {
    auto& b = W().busySites;
    for (auto& v : S().vendors)
        if ((site.empty() || v.id == site) &&
            std::find(b.begin(), b.end(), v.id) != b.end())
            v.loginInProgress = false;
    if (site.empty()) b.clear();
    else              b.erase(std::remove(b.begin(), b.end(), site), b.end());
}

// Take ownership of `sites`, releasing whatever the previous set was.
void setWizardBusy(std::vector<std::string> sites) {
    clearWizardBusy();
    W().busySites = std::move(sites);
    applyWizardBusy();
}

// Point the web view at the wizard's current step. The screencast backend adopts
// the worker-side embed session (embed_id); CEF has no worker embed, so it opens
// the current site's sign-in URL in-process instead (usesWorkerEmbed()==false).
// Hard-captcha sites skip CEF and open native desktop Chrome (session still
// lands in the warm jar for headless reuse when the user closes the window).
void openWizardSite(AppState& st, const std::string& embedId) {
    auto& w = W();
    w.siteOpenedAt  = ImGui::GetTime();   // arm the load watchdog
    w.loadTimedOut  = false;
    WIZ_TRACE("openWizardSite site='%s' embed='%s' workerEmbed=%d",
              w.currentSite.c_str(), embedId.c_str(), (int)w.web->usesWorkerEmbed());
    if (w.web->usesWorkerEmbed()) {
        w.web->attach(st, embedId);
        return;
    }
    if (w.currentSite.empty()) {
        WIZ_TRACE("openWizardSite: NO currentSite — nothing opened");
        return;
    }
    if (sitePrefersDesktopLogin(w.currentSite)) {
        // Hand this row to the headed flow's bookkeeping first — our own wizard
        // mark would otherwise trip the !loginInProgress guard below and nothing
        // would open at all.
        clearWizardBusy(w.currentSite);
        for (auto& v : S().vendors) {
            if (v.id == w.currentSite && !v.loginInProgress) {
                WIZ_TRACE("openWizardSite: native desktop for '%s' (reCAPTCHA)",
                          w.currentSite.c_str());
                st.toast(ui::ToastLevel::Info,
                         "Opening system Chrome for " + vendorLabel(w.currentSite) +
                             " (bot-check site). Sign in, then close the window — "
                             "session carries to headless fetch.");
                startVendorLogin(st, v);
                return;
            }
        }
    }
    w.web->open(st, w.currentSite, /*isUrl=*/false);
}

// Re-open the current site's page in-place (Retry after a stall / load error).
void retryCurrentSite(AppState& st) {
    auto& w = W();
    if (w.currentSite.empty()) return;
    w.siteOpenedAt = ImGui::GetTime();
    w.loadTimedOut = false;
    w.web->open(st, w.currentSite, /*isUrl=*/false);
    st.toast(ui::ToastLevel::Info, "Reloading " + vendorLabel(w.currentSite) + "…");
}

void closeWizard(AppState& st, bool sendStop) {
    auto& w = W();
    if (sendStop)
        st.rpc.call("login_wizard_stop", {}, [](const core::RpcResult&) {});
    w.web->close(st);
    w.active = w.starting = false;
    w.currentSite.clear();
    w.embedId.clear();
    w.done.clear();
    w.skipped.clear();
    w.remaining.clear();
    w.setupError.clear();
    w.setupHint.clear();
    clearWizardBusy();               // release every vendor row the wizard held
    w.siteOpenedAt  = 0.0;
    w.loadTimedOut  = false;
    S().browserWizardRequested = false;
    loadVendors(st, /*force=*/true);
}

void finishWizard(AppState& st) {
    auto& w    = W();
    const int n = (int)w.done.size();
    if (n > 0) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "Signed into %d vendor%s", n, n == 1 ? "" : "s");
        st.toast(ui::ToastLevel::Success, buf);
    } else {
        st.toast(ui::ToastLevel::Info, "Vendor sign-in finished");
    }
    closeWizard(st, /*sendStop=*/true);
}

// Shared parser for the status-shaped responses of login_wizard_status / _next.
void applyWizardStatus(AppState& st, const nlohmann::json& j) {
    auto& w = W();
    if (!w.active) return;
    const std::string prevSite = w.currentSite;

    if (j.contains("done") && j["done"].is_array()) {
        std::vector<std::string> nd;
        for (const auto& s : j["done"])
            if (s.is_string()) nd.push_back(s.get<std::string>());
        // Toast every newly signed-in site (the worker auto-advances on logged_in).
        for (const auto& s : nd)
            if (std::find(w.done.begin(), w.done.end(), s) == w.done.end())
                st.toast(ui::ToastLevel::Success, vendorLabel(s) + " signed in");
        w.done = std::move(nd);
    }
    if (j.contains("skipped") && j["skipped"].is_array()) {
        w.skipped.clear();
        for (const auto& s : j["skipped"])
            if (s.is_string()) w.skipped.push_back(s.get<std::string>());
    }
    if (j.contains("remaining") && j["remaining"].is_array()) {
        w.remaining.clear();
        for (const auto& s : j["remaining"])
            if (s.is_string()) w.remaining.push_back(s.get<std::string>());
    }
    w.currentSite = jstr(j, "current_site", w.currentSite);

    // Rows the wizard is finished with stop reading as "login in progress" — the set
    // is re-asserted every frame, so releasing it here is what makes it stick.
    for (const auto& s : w.done)    clearWizardBusy(s);
    for (const auto& s : w.skipped) clearWizardBusy(s);

    if (j.value("all_done", false) || !j.value("active", true)) {
        finishWizard(st);
        return;
    }
    const std::string embed = jstr(j, "embed_id");
    if (!embed.empty() && embed != w.embedId) {   // wizard advanced → new session
        w.embedId = embed;
        openWizardSite(st, embed);
    } else if (!w.web->usesWorkerEmbed() && !w.currentSite.empty() &&
               w.currentSite != prevSite) {
        // Client mode has no embed_id — the wizard advanced when current_site
        // changed, and the GUI opens the next site's sign-in page itself.
        openWizardSite(st, "");
    }
}

void pollWizardStatus(AppState& st) {
    auto& w = W();
    if (w.statusPending) return;
    w.statusPending = true;
    w.lastStatusAt  = ImGui::GetTime();
    st.rpc.call("login_wizard_status", {}, [&st](const core::RpcResult& r) {
        W().statusPending = false;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) return;
        applyWizardStatus(st, r.result);
    });
}

void wizardNext(AppState& st, bool skip, bool assertLoggedIn = false) {
    auto& w = W();
    if (w.actionPending) return;
    w.actionPending = true;
    // In client mode the worker never sees the in-process browser, so the GUI is
    // authoritative for "did the user sign in?". The "I'm signed in" button passes
    // assertLoggedIn — the user's own confirmation, which the URL heuristic can't
    // reliably reproduce — so wizard_next records this site as done (not skipped)
    // and stamps the server-side login.
    const bool loggedIn = !skip && assertLoggedIn;
    auto send = [&st, skip, loggedIn]() {
        st.rpc.call("login_wizard_next",
                    nlohmann::json{{"skip", skip}, {"logged_in", loggedIn}},
                    [&st](const core::RpcResult& r) {
            W().actionPending = false;
            if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
                std::string why = r.ok ? jstr(r.result, "reason", "wizard step failed")
                                       : r.err;
                st.toast(ui::ToastLevel::Error, "Wizard: " + why);
                return;
            }
            applyWizardStatus(st, r.result);
        });
    };
    // Signed in on an in-process backend → hand the session to the worker FIRST
    // (cookie export + browser_session_import staging) so headless fetches can
    // reuse it, then advance. Export failure still advances — the login stamp is
    // also written by the import, so a failed export degrades to "signed in for
    // this GUI session only" rather than blocking the wizard.
    //
    // Native-desktop sites (Anaheim etc.) already push cookies via
    // browser_login → import_session_cookies + CEF warm; skip CEF export
    // (the panel browser never held that jar).
    if (loggedIn && !w.web->usesWorkerEmbed() && !w.currentSite.empty()
        && !sitePrefersDesktopLogin(w.currentSite)) {
        w.web->exportSession(st, w.currentSite, [send](bool ok) {
            if (!ok)
                std::fprintf(stderr, "[parts] vendor session export failed — "
                                     "headless reuse unavailable for this site\n");
            send();
        });
    } else {
        send();
    }
}

// Login-state chip: borderRow-style pill, colored by state.
// Chip label + role for a login state — shared by the chip renderer and the
// top-bar layout (which needs the width to reserve space).
const char* chipLabel(const std::string& state, const float*& role) {
    role = palette.textDisabled;
    if (state == "logged_in")   { role = palette.statusOk;   return "Signed in"; }
    if (state == "login_page")  { role = palette.statusWarn; return "Login page"; }
    if (state == "challenge")   { role = palette.statusErr;
                                  return "Challenge — solve it here"; }
    // "browsing" (user is signed-in-or-not, just looking around) shows NO chip —
    // it was a wide hint box that overlapped/hid the URL bar. Empty → not drawn.
    return "";
}

void loginStateChip(const std::string& state) {
    const float sc = ui::uiScale;
    const float* role = nullptr;
    const char*  txt  = chipLabel(state, role);
    if (!txt || !txt[0]) return;   // no chip for neutral states
    ImDrawList* dl  = ImGui::GetWindowDrawList();
    const ImVec2 ts = ImGui::CalcTextSize(txt);
    const ImVec2 p  = ImGui::GetCursorScreenPos();
    const float  h  = ts.y + 4.0f * sc;
    const float  wd = ts.x + 14.0f * sc;
    dl->AddRectFilled(p, ImVec2(p.x + wd, p.y + h),
                      ImGui::ColorConvertFloat4ToU32(ImVec4(role[0], role[1], role[2], 0.14f)),
                      h * 0.5f);
    dl->AddRect(p, ImVec2(p.x + wd, p.y + h),
                ImGui::ColorConvertFloat4ToU32(ImVec4(role[0], role[1], role[2], 0.55f)),
                h * 0.5f, 0, 1.0f * sc);
    dl->AddText(ImVec2(p.x + 7.0f * sc, p.y + 2.0f * sc),
                ImGui::ColorConvertFloat4ToU32(ImVec4(role[0], role[1], role[2], 1.0f)), txt);
    ImGui::Dummy(ImVec2(wd, h));
}

}  // namespace

// ---- public: engagement flag ----------------------------------------------------------

bool browserPanelActive() {
    return S().browserWizardRequested;
}

// ---- public: start the wizard (multi-vendor when site empty, else that one site) --------

void startLoginWizard(AppState& st, const std::string& site) {
    auto& w = W();
    WIZ_TRACE("startLoginWizard(site='%s') entry (starting=%d active=%d)",
              site.c_str(), (int)w.starting, (int)w.active);
    // T-RF12: web (WASM) build — no CEF, no Playwright worker. Every sign-in entry
    // point ("Connect vendor accounts", per-vendor "Sign in") funnels here, so one
    // guard turns a dead wizard into an explained toast. Folds away on desktop.
    if (const char* why = ui::webGapReason("vendor_signin")) {
        st.toast(ui::ToastLevel::Info, why);
        return;
    }
    if (w.starting || w.active) return;
    w.starting                 = true;
    w.singleSite               = !site.empty();
    S().browserWizardRequested = true;   // engage the panel immediately (shows "starting")
    S().vendorsPopupOpen       = false;  // the wizard owns the results column now
    // Mark the rows this wizard is about to visit busy right away; the worker's
    // authoritative site list replaces this guess in the callback below.
    {
        std::vector<std::string> busy;
        if (!site.empty()) {
            busy.push_back(site);
        } else {
            for (const auto& v : S().vendors)
                if (!v.loggedIn) busy.push_back(v.id);
        }
        setWizardBusy(std::move(busy));
    }

    nlohmann::json p = nlohmann::json::object();
    if (!site.empty()) p["sites"] = nlohmann::json::array({site});
    // In-process backends (CEF) render + classify the login themselves; the
    // worker must NOT launch its own invisible Playwright session per step (it
    // would sit unattended on the login URL, hold the site's profile lock, and
    // never see the user's sign-in). client mode = bookkeeping-only wizard.
    if (!w.web->usesWorkerEmbed()) p["mode"] = "client";

    WIZ_TRACE("login_wizard_start sent (site='%s' clientMode=%d)",
              site.c_str(), (int)!w.web->usesWorkerEmbed());
    st.rpc.call("login_wizard_start", p, [&st, site](const core::RpcResult& r) {
        auto& w    = W();
        w.starting = false;
        const bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        WIZ_TRACE("login_wizard_start cb: transport_ok=%d ok=%d reason='%s' "
                  "current_site='%s' embed='%s'",
                  (int)r.ok, (int)ok,
                  r.ok ? jstr(r.result, "reason", "").c_str() : r.err.c_str(),
                  jstr(r.result, "current_site").c_str(), jstr(r.result, "embed_id").c_str());
        if (!ok) {
            std::string reason = r.ok ? jstr(r.result, "reason", "") : r.err;
            std::string hint   = r.ok ? jstr(r.result, "hint", "") : std::string();
            // Playwright/embed setup failures affect headed login too — don't fall back
            // to vendor_login (it would toast "browser opened" while nothing appears).
            const bool embedSetup =
                reason == "playwright_not_installed" || reason == "embed_start_failed" ||
                reason.find("playwright") != std::string::npos;
            if (embedSetup || reason == "wizard_active") {
                clearWizardBusy();   // nothing is signing in — free the rows
                if (reason == "wizard_active")
                    st.rpc.call("login_wizard_stop", {}, [](const core::RpcResult&) {});
                w.setupError = embedSetup
                                   ? "Embedded browser unavailable — Playwright/Chromium not installed"
                                   : "Sign-in wizard is busy — close it and try again";
                w.setupHint  = hint.empty()
                                   ? "Run Settings > Repair runtime, or: pip install playwright && "
                                     "playwright install chromium"
                                   : hint;
                st.toast(ui::ToastLevel::Error, w.setupError);
                return;
            }
            // Embed backend missing / failed → old headed vendor_login fallback.
            S().browserWizardRequested = false;
            clearWizardBusy();   // must precede the !loginInProgress guards below
            if (!site.empty()) {
                for (auto& v : S().vendors)
                    if (v.id == site && !v.loginInProgress) {
                        startVendorLogin(st, v);
                        return;
                    }
                st.toast(ui::ToastLevel::Error, "Sign-in unavailable for " + site);
                return;
            }
            for (auto& v : S().vendors) {
                if (v.loggedIn || v.loginInProgress) continue;
                startVendorLogin(st, v);
                return;
            }
            st.toast(ui::ToastLevel::Info, "All vendor sites are already signed in.");
            return;
        }
        if (r.result.value("all_done", false)) {
            S().browserWizardRequested = false;
            clearWizardBusy();
            st.toast(ui::ToastLevel::Info, "All vendor sites are already signed in.");
            return;
        }
        w.currentSite = jstr(r.result, "current_site");
        // In-process CEF: if cold init failed (e.g. ProcessSingleton held by a
        // stale daemon, or GPU init), do NOT open the panel — a second
        // CefInitialize used to SIGTRAP. Fall back to headed/native desktop login.
        if (!w.web->usesWorkerEmbed() && !w.web->available()) {
            S().browserWizardRequested = false;
            w.active = false;
            clearWizardBusy();   // must precede the !loginInProgress guards below
            std::string fallback = !w.currentSite.empty() ? w.currentSite : site;
            if (fallback.empty() && r.result.contains("sites") && r.result["sites"].is_array()
                && !r.result["sites"].empty() && r.result["sites"][0].is_string())
                fallback = r.result["sites"][0].get<std::string>();
            if (!fallback.empty()) {
                for (auto& v : S().vendors)
                    if (v.id == fallback && !v.loginInProgress) {
                        st.toast(ui::ToastLevel::Warn,
                                 "In-app browser unavailable — opening desktop Chrome for "
                                     + fallback);
                        startVendorLogin(st, v);
                        return;
                    }
            }
            st.toast(ui::ToastLevel::Error,
                     "In-app browser unavailable (CEF init failed). Restart Chudware, or use "
                     "Desktop browser on a vendor row.");
            return;
        }
        w.active = true;
        w.done.clear();
        w.skipped.clear();
        w.remaining.clear();
        if (r.result.contains("sites") && r.result["sites"].is_array())
            for (const auto& s : r.result["sites"])
                if (s.is_string()) w.remaining.push_back(s.get<std::string>());
        if (!w.remaining.empty())
            setWizardBusy(w.remaining);   // authoritative site list from the worker
        w.embedId.clear();
        const std::string embed = jstr(r.result, "embed_id");
        if (!embed.empty()) {
            w.embedId = embed;
            openWizardSite(st, embed);
        } else if (!w.web->usesWorkerEmbed() && !w.currentSite.empty()) {
            openWizardSite(st, "");   // client mode: open the first site in-process
        }
        w.lastStatusAt = ImGui::GetTime();
    });
}

// ---- public: the panel -------------------------------------------------------------------

void drawBrowserPanel(AppState& st) {
    auto& w        = W();
    const float sc = ui::uiScale;

    // loadVendors() rewrites loginInProgress from the worker, which only knows about
    // headed vendor_login threads — re-assert the rows this wizard owns. Runs before
    // drawFiltersBlock/drawVendorStrip in the frame, so the strip reads the truth.
    applyWizardBusy();

    // Background work: WebView frame/input polling + wizard status watch (2 s, single-flight;
    // the worker auto-advances on logged_in — embed_id changes are picked up here).
    w.web->update(st);
    if (w.active && !w.statusPending && ImGui::GetTime() - w.lastStatusAt > 2.0)
        pollWizardStatus(st);

    // NO client-side auto-advance. The in-process browser cannot reliably tell
    // "signed in" from a redirect: inferring logged_in from leaving a sign-in URL
    // skipped providers that merely bounced through an SSO/consent page, and never
    // fired for vendors (e.g. McMaster) whose login is a modal on a normal URL.
    // The user drives it with the explicit "I'm signed in" button, which asserts
    // the sign-in authoritatively (see wizardNext).

    // Load watchdog (in-process backend): if the page hasn't painted its first
    // frame within kLoadTimeoutSec and there's no explicit load error, treat it
    // as stalled — toast once and let the body show a Retry. Covers the silent
    // OSR/network stalls that used to leave the user on an eternal "Loading".
    if (!w.web->usesWorkerEmbed() && w.siteOpenedAt > 0.0 && !w.web->active() &&
        w.web->lastError().empty() && !w.loadTimedOut &&
        ImGui::GetTime() - w.siteOpenedAt > kLoadTimeoutSec) {
        w.loadTimedOut = true;
        st.toast(ui::ToastLevel::Error,
                 vendorLabel(w.currentSite) +
                     " is taking too long to load — check your connection, then Retry or Skip.");
    }

    // ---- top bar (30dp): title/step, back-fwd-reload, editable URL, chip, actions -------
    // Every item is vertically centered in the bar by setting its Y from its own
    // height (text, small buttons and the URL input have different heights), so the
    // row reads as one clean, centered strip instead of a ragged baseline.
    const float barH = 30.0f * sc;
    ImGui::BeginChild("##wiz_bar", ImVec2(0, barH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    {
        const float availW = ImGui::GetContentRegionAvail().x;
        const float fh = ImGui::GetFrameHeight();       // URL input height
        const float th = ImGui::GetTextLineHeight();     // text / small-button height
        auto vcenter   = [&](float h) { ImGui::SetCursorPosY((barH - h) * 0.5f); };
        auto bsz       = [&](const char* t) {
            return ImGui::CalcTextSize(t).x + ImGui::GetStyle().FramePadding.x * 2.0f;
        };

        // Reserve the right cluster (login chip + action buttons) up front so the
        // URL field can flex into whatever's left.
        // "I'm signed in" is the user's explicit sign-in confirmation — the only
        // authoritative signal in client mode, shown for every active step
        // (including single-site, which has no auto-advance). "Skip" only makes
        // sense with more than one site left to move past.
        const bool showConfirm = w.active;
        const bool showSkip     = w.active && !w.singleSite;
        const bool showRetry    = !w.web->usesWorkerEmbed() &&
                                  (w.loadTimedOut || !w.web->lastError().empty());
        const float* chipRole = nullptr;
        const char*  chipTxt  = chipLabel(w.web->loginState(), chipRole);
        const bool   showChip = chipTxt && chipTxt[0];
        const float  chipW    = showChip ? ImGui::CalcTextSize(chipTxt).x + 14.0f * sc : 0.0f;
        const bool showDesktop = !w.currentSite.empty() && !w.actionPending;
        float rightW = showChip ? chipW + 10.0f * sc : 0.0f;
        if (showSkip)    rightW += bsz("Skip") + 4.0f * sc;
        if (showConfirm) rightW += bsz("I'm signed in") + 4.0f * sc;
        if (showRetry)   rightW += bsz("Retry") + 4.0f * sc;
        if (showDesktop) rightW += bsz("Desktop browser") + 4.0f * sc;
        rightW += bsz("Close") + 4.0f * sc;

        // Title.
        if (S().fonts && S().fonts->medium) ImGui::PushFont(S().fonts->medium);
        vcenter(ImGui::GetTextLineHeight());
        ImGui::TextUnformatted("Vendor sign-in");
        if (S().fonts && S().fonts->medium) ImGui::PopFont();

        // Step K/N.
        if (w.active) {
            const int doneN = (int)(w.done.size() + w.skipped.size());
            const int total = doneN + (int)w.remaining.size();
            if (total > 0) {
                ImGui::SameLine(0.0f, 8.0f * sc);
                vcenter(th);
                ImGui::TextDisabled("step %d/%d", std::min(doneN + 1, total), total);
            }
        }

        // Back / Forward / Reload + editable URL (in-process CEF only).
        if (w.web->hasNavChrome()) {
            ImGui::SameLine(0.0f, 12.0f * sc);
            vcenter(th);
            ImGui::BeginDisabled(!w.web->canGoBack());
            if (ImGui::SmallButton("<")) w.web->goBack(st);
            ImGui::EndDisabled();
            ImGui::SameLine(0.0f, 2.0f * sc); vcenter(th);
            ImGui::BeginDisabled(!w.web->canGoForward());
            if (ImGui::SmallButton(">")) w.web->goForward(st);
            ImGui::EndDisabled();
            ImGui::SameLine(0.0f, 2.0f * sc); vcenter(th);
            if (ImGui::SmallButton("Reload")) w.web->reload(st);

            // URL field flexes into the gap before the right cluster.
            ImGui::SameLine(0.0f, 8.0f * sc);
            const float urlW = availW - ImGui::GetCursorPosX() - rightW - 8.0f * sc;
            if (urlW > 60.0f * sc) {
                if (!w.urlEditing)   // mirror the live URL while the user isn't typing
                    std::snprintf(w.urlBuf, sizeof(w.urlBuf), "%s", w.web->url().c_str());
                vcenter(fh);
                ImGui::SetNextItemWidth(urlW);
                const bool submit = ImGui::InputText(
                    "##wiz_url", w.urlBuf, sizeof(w.urlBuf),
                    ImGuiInputTextFlags_EnterReturnsTrue);
                w.urlEditing = ImGui::IsItemActive();
                if (submit && w.urlBuf[0]) {
                    w.web->navigate(st, w.urlBuf);
                    w.urlEditing = false;
                }
            }
        } else {
            // Screencast backend: no nav chrome — show the clipped dim URL as before.
            const std::string& u = w.web->url();
            if (!u.empty()) {
                ImGui::SameLine(0.0f, 10.0f * sc);
                const float urlW = availW - ImGui::GetCursorPosX() - rightW - 8.0f * sc;
                if (urlW > 30.0f * sc) {
                    vcenter(th);
                    const ImVec2 p = ImGui::GetCursorScreenPos();
                    ImGui::GetWindowDrawList()->PushClipRect(
                        p, ImVec2(p.x + urlW, p.y + barH), true);
                    ImGui::TextDisabled("%s", u.c_str());
                    ImGui::GetWindowDrawList()->PopClipRect();
                }
            }
        }

        // Right cluster: login chip + action buttons, right-aligned.
        ImGui::SameLine();
        ImGui::SetCursorPosX(availW - rightW);
        if (showChip) {
            vcenter(th + 4.0f * sc);
            loginStateChip(w.web->loginState());
            ImGui::SameLine(0.0f, 10.0f * sc);
        }
        if (showSkip) {
            vcenter(th);
            if (ImGui::SmallButton(w.actionPending ? "..." : "Skip") && !w.actionPending)
                wizardNext(st, /*skip=*/true);
            ImGui::SameLine(0.0f, 4.0f * sc);
        }
        if (showConfirm) {
            vcenter(th);
            if (ImGui::SmallButton("I'm signed in") && !w.actionPending)
                wizardNext(st, /*skip=*/false, /*assertLoggedIn=*/true);
            ImGui::SameLine(0.0f, 4.0f * sc);
        }
        if (showRetry) {
            vcenter(th);
            if (ImGui::SmallButton("Retry"))
                retryCurrentSite(st);
            ImGui::SameLine(0.0f, 4.0f * sc);
        }
        // Desktop Chromium (real GPU, headed Playwright) — needed when the
        // in-panel OSR CEF fails vendor bot checks (Anaheim Magento reCAPTCHA
        // under SwiftShader on NVIDIA+Wayland). Opens a real window; cookies
        // are exported into the CEF warm profile when the user closes it.
        if (showDesktop) {
            vcenter(th);
            if (ImGui::SmallButton("Desktop browser")) {
                // Hand the row over before the !loginInProgress guard below.
                clearWizardBusy(w.currentSite);
                for (auto& v : S().vendors) {
                    if (v.id == w.currentSite && !v.loginInProgress) {
                        startVendorLogin(st, v);
                        st.toast(ui::ToastLevel::Info,
                                 "System Chrome opened for " + vendorLabel(w.currentSite) +
                                     " (no automation). Allow cookies, sign in, then close the window.");
                        break;
                    }
                }
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(
                    "Open a full desktop Chromium window (real GPU). Use this when the "
                    "in-panel browser fails a vendor bot check / reCAPTCHA.");
            ImGui::SameLine(0.0f, 4.0f * sc);
        }
        vcenter(th);
        if (ImGui::SmallButton("Close")) {
            closeWizard(st, /*sendStop=*/true);
            ImGui::EndChild();
            return;
        }
    }
    ImGui::EndChild();

    // ---- body: the embedded page ----------------------------------------------------------
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    if (w.starting && !w.web->active()) {
        // login_wizard_start still in flight.
        ImDrawList* dl  = ImGui::GetWindowDrawList();
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        dl->AddRectFilled(p0, ImVec2(p0.x + avail.x, p0.y + avail.y),
                          Palette::toU32(palette.panelBg), 6.0f * sc);
        const char* msg = "Starting sign-in wizard…";
        const ImVec2 ts = ImGui::CalcTextSize(msg);
        // Mark + gap + label centred as ONE block: a start that can take seconds needs motion,
        // and a mark pinned independently of the line would drift apart from it as avail moves.
        const float mark = 28.0f * sc;
        const float gap  = 8.0f * sc;
        const float topY = p0.y + (avail.y - (mark + gap + ts.y)) * 0.5f;
        const float midX = p0.x + avail.x * 0.5f;
        if (mark >= ui::kTesseractSpinnerMinPx)
            ui::drawTesseractSpinner(dl, ImVec2(midX, topY + mark * 0.5f), mark, ImGui::GetTime(),
                                     Palette::toU32(palette.textDisabled));
        dl->AddText(ImVec2(midX - ts.x * 0.5f, topY + mark + gap),
                    Palette::toU32(palette.textDisabled), msg);
        ImGui::Dummy(avail);
        return;
    }
    if (!w.setupError.empty()) {
        ImDrawList* dl  = ImGui::GetWindowDrawList();
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        dl->AddRectFilled(p0, ImVec2(p0.x + avail.x, p0.y + avail.y),
                          Palette::toU32(palette.panelBg), 6.0f * sc);
        const ImVec2 errTs = ImGui::CalcTextSize(w.setupError.c_str());
        dl->AddText(ImVec2(p0.x + (avail.x - errTs.x) * 0.5f, p0.y + avail.y * 0.45f),
                    Palette::toU32(palette.statusErr), w.setupError.c_str());
        if (!w.setupHint.empty()) {
            ImGui::PushTextWrapPos(p0.x + avail.x - 20.0f * sc);
            ImGui::SetCursorScreenPos(ImVec2(p0.x + 20.0f * sc, p0.y + avail.y * 0.55f));
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            ImGui::TextWrapped("%s", w.setupHint.c_str());
            ImGui::PopStyleColor();
            ImGui::PopTextWrapPos();
        }
        ImGui::Dummy(avail);
        return;
    }
    // Stalled load (watchdog fired, still no paint): replace the eternal
    // "Loading" with an actionable message. Use Retry (top bar) or Skip.
    if (w.loadTimedOut && !w.web->active()) {
        ImDrawList* dl  = ImGui::GetWindowDrawList();
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        dl->AddRectFilled(p0, ImVec2(p0.x + avail.x, p0.y + avail.y),
                          Palette::toU32(palette.panelBg), 6.0f * sc);
        const std::string msg = vendorLabel(w.currentSite) + " didn't load";
        const ImVec2 ts = ImGui::CalcTextSize(msg.c_str());
        dl->AddText(ImVec2(p0.x + (avail.x - ts.x) * 0.5f, p0.y + avail.y * 0.45f),
                    Palette::toU32(palette.statusErr), msg.c_str());
        const char* hint = "Check your connection, then Retry — or Skip this vendor.";
        const ImVec2 hts = ImGui::CalcTextSize(hint);
        dl->AddText(ImVec2(p0.x + (avail.x - hts.x) * 0.5f, p0.y + avail.y * 0.55f),
                    Palette::toU32(palette.textDisabled), hint);
        ImGui::Dummy(avail);
        return;
    }
    w.web->draw(st, avail);
}

}  // namespace parts
}  // namespace ui
