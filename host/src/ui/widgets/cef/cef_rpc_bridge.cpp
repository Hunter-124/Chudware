// cef/cef_rpc_bridge.cpp — AppState/RPC side of the CEF backend (NO CEF headers).
//
// Kept separate from cef_web_view.cpp because CEF's net headers define ERR_NONE,
// which collides with core/app_state.h's enum. This TU includes app_state.h and
// speaks only plain std types back to the CEF side (cef_rpc_bridge.h).
#include "cef_rpc_bridge.h"

#include <utility>

#include <nlohmann/json.hpp>

#include "core/app_state.h"

namespace ui::cef {

void rpc_start_url(
    AppState& st, const std::string& site,
    std::function<void(bool, const std::string&, const std::string&, const std::string&)> cb) {
    nlohmann::json p;
    p["site"] = site;
    st.rpc.call("browser_start_url", p, [cb = std::move(cb)](const core::RpcResult& r) {
        if (!r.ok) { cb(false, "", "", r.err); return; }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            const std::string reason =
                r.result.is_object() ? r.result.value("reason", std::string()) : std::string();
            cb(false, "", "", reason);
            return;
        }
        cb(true, r.result.value("url", std::string()),
                 r.result.value("cache_dir", std::string()), "");
    });
}

}  // namespace ui::cef
