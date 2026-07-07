// cef/cef_rpc_bridge.h — CEF-header-free seam to the worker RPC.
//
// cef_web_view.cpp includes CEF's net headers, which define ERR_NONE and clash
// with core/app_state.h's enum. So anything that touches st.rpc (i.e. AppState)
// lives in cef_rpc_bridge.cpp, which includes app_state.h but NO CEF headers.
// This header is included from the CEF side and exposes only plain std types.
#pragma once

#include <functional>
#include <string>

struct AppState;

namespace ui::cef {

// Resolve a vendor key → its sign-in URL + per-site cookie cache dir over the
// `browser_start_url` worker RPC. `cb` runs on the GL/UI thread (RPC drain), so
// the caller may create the CEF browser directly from it. On failure `ok=false`
// and `err` carries a reason; `url`/`cacheDir` are empty.
void rpc_start_url(
    AppState& st, const std::string& site,
    std::function<void(bool ok, const std::string& url,
                       const std::string& cacheDir, const std::string& err)> cb);

}  // namespace ui::cef
