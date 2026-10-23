// ui::webBuild / ui::webGapReason — web-build capability gaps.
//
// The web app is this same host/src/ui tree compiled to WASM, talking to chudware-hostd
// with NO Python worker and NO CEF. Surfaces that depend on either must degrade to an
// honest "desktop only" affordance instead of a dead button/panel. This header is the
// single source of truth: ribbon buttons (widgets/ribbon.cpp), panels and settings rows
// all query webGapReason(surfaceId) and render disabled + explained when it answers.
//
// Gap ids are the affected surface's own identifier — ribbon cmd_ids ("ecad_ai_design")
// or panel surface ids ("web_browser", "vendor_signin", "account_signin"). Python-only
// method families mirror contracts/web_parity.md's web_unsupported list.
#pragma once
#include <string>

namespace ui {

// True only in the WASM (browser) build. constexpr so every native call site folds the
// gap check away — desktop behavior is unchanged by construction.
constexpr bool webBuild() {
#ifdef __EMSCRIPTEN__
    return true;
#else
    return false;
#endif
}

// Why `surfaceId` is unavailable in the web build, or nullptr when it is available.
// Always nullptr in native builds (see webBuild).
const char* webGapReason(const char* surfaceId);

// Open an absolute external HTTP(S) URL without passing it through a command shell.
// Browser builds use window.open; native builds invoke the platform URL opener with
// the URL as one argv/API argument. Returns false when the URL is not allowlisted or
// the platform opener could not be launched.
bool openExternalHttpUrl(const std::string& url);

}  // namespace ui
