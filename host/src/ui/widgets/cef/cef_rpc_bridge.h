// cef/cef_rpc_bridge.h — CEF-header-free seam to the worker RPC.
//
// cef_web_view.cpp includes CEF's net headers, which define ERR_NONE and clash
// with core/app_state.h's enum. So anything that touches st.rpc (i.e. AppState)
// lives in cef_rpc_bridge.cpp, which includes app_state.h but NO CEF headers.
// This header is included from the CEF side and exposes only plain std types.
#pragma once

#include <functional>
#include <string>
#include <vector>

struct AppState;

namespace ui::cef {

// One exported cookie, in plain std types (no CEF, no JSON). Marshalled to the
// worker's `browser_session_import` RPC, which normalises it into Playwright's
// add_cookies shape and stages it for the site's persistent profile.
struct SessionCookie {
    std::string name, value, domain, path;
    double expires = -1.0;        // epoch seconds; <= 0 → session cookie
    bool secure = false, httpOnly = false;
    std::string sameSite;         // "strict" | "lax" | "none" | "" (unspecified)
};

// Stage an exported cookie jar for `site` on the worker (also stamps the login
// as verified — the signal the vendor dots / has_login trust). `cb(ok, err)`
// runs on the GL/UI thread.
void rpc_session_import(
    AppState& st, const std::string& site, std::vector<SessionCookie> cookies,
    std::function<void(bool ok, const std::string& err)> cb);

// Resolve a vendor key → its sign-in URL + per-site cookie cache dir over the
// `browser_start_url` worker RPC. `cb` runs on the GL/UI thread (RPC drain), so
// the caller may create the CEF browser directly from it. On failure `ok=false`
// and `err` carries a reason; `url`/`cacheDir` are empty.
void rpc_start_url(
    AppState& st, const std::string& site,
    std::function<void(bool ok, const std::string& url,
                       const std::string& cacheDir, const std::string& err)> cb);

}  // namespace ui::cef
