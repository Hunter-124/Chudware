// See web_capabilities.h. The kWebGaps table is the ONE place web-degradation copy lives;
// adding a row gates every surface that asks about that id.
#include "web_capabilities.h"

#include <cstring>

namespace ui {
namespace {

struct WebGap {
    const char* id;      // ribbon cmd_id or panel surface id
    const char* reason;  // shown in the disabled tooltip / toast / panel body
};

// Keep in sync with contracts/web_parity.md (web_unsupported — do not edit; the
// integrator owns it) and with the CEF-less WASM build (CHUDWARE_ENABLE_CEF off).
constexpr WebGap kWebGaps[] = {
    // CEF-dependent: no embedded Chromium in the browser build.
    {"web_browser",
     "The in-app browser is desktop-only — the web app has no embedded Chromium. "
     "Open external links in a normal browser tab instead."},
    {"vendor_signin",
     "Vendor sign-in is desktop-only: it drives an embedded browser session the web "
     "app cannot host. Sign in from the desktop app."},
    {"account_signin",
     "Account sign-in is desktop-only: the subscription token store runs in the "
     "desktop app's engine worker, which the web build does not have."},
    // Python-worker-only methods (contracts/web_parity.md web_unsupported).
    {"ecad_ai_design",
     "AI circuit design is desktop-only until the ECAD design orchestrator is ported "
     "into the C++ core (T-RF20)."},
};

}  // namespace

const char* webGapReason(const char* surfaceId) {
    if (!webBuild() || !surfaceId) return nullptr;
    for (const WebGap& g : kWebGaps)
        if (std::strcmp(g.id, surfaceId) == 0) return g.reason;
    return nullptr;
}

}  // namespace ui
