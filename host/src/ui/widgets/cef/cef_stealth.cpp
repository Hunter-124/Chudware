// cef/cef_stealth.cpp — see cef_stealth.h. Kept in sync with the client-side
// patches in services/browser/stealth.py (INIT_SCRIPT) so the headless-fetch and
// interactive surfaces share ONE fingerprint. Compiled only with AI_CAD_ENABLE_CEF.
#include "cef_stealth.h"

#include <string>

#include "include/cef_devtools_message_observer.h"
#include "include/cef_values.h"
#include "include/cef_version.h"  // CHROME_VERSION_* for Sec-CH-UA alignment

namespace ui::cef {
namespace {

// Minimal observer whose only job is to attach the DevTools agent so
// ExecuteDevToolsMethod takes effect (see cef_stealth.h). We consume no messages.
class StealthDevToolsObserver : public CefDevToolsMessageObserver {
public:
    bool OnDevToolsMessage(CefRefPtr<CefBrowser>, const void*, size_t) override { return false; }
    void OnDevToolsMethodResult(CefRefPtr<CefBrowser>, int, bool, const void*, size_t) override {}
    void OnDevToolsEvent(CefRefPtr<CefBrowser>, const CefString&, const void*, size_t) override {}
    void OnDevToolsAgentAttached(CefRefPtr<CefBrowser>) override {}
    void OnDevToolsAgentDetached(CefRefPtr<CefBrowser>) override {}
private:
    IMPLEMENT_REFCOUNTING(StealthDevToolsObserver);
};

// Document-start hardening. Mirrors stealth.INIT_SCRIPT: removes the automation
// tells a normal Chrome never emits (window.chrome absent, single-entry languages,
// empty plugin list, webdriver true) plus the deviceMemory cap CEF leaks from host
// RAM. Each patch is isolated in try/catch so a hardened property on a future
// Chromium can never break page init. WebGL vendor/renderer spoofing is added in
// Phase 3 (cef_web_view getParameter hook) — kept here in lockstep with stealth.py.
const char* kInitScript = R"JS(
(() => {
  const patch = (fn) => { try { fn(); } catch (e) { /* keep page init alive */ } };
  patch(() => { if (!window.chrome) window.chrome = { runtime: {} }; });
  patch(() => {
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  });
  patch(() => {
    if (navigator.plugins && navigator.plugins.length) return;
    const mk = (name) => ({ name, description: 'Portable Document Format',
                            filename: 'internal-pdf-viewer', length: 1 });
    const plugins = [mk('PDF Viewer'), mk('Chrome PDF Viewer'), mk('Chromium PDF Viewer')];
    Object.defineProperty(navigator, 'plugins', { get: () => plugins });
    Object.defineProperty(navigator, 'mimeTypes',
                          { get: () => [{ type: 'application/pdf', suffixes: 'pdf' }] });
  });
  patch(() => {
    if (navigator.webdriver) Object.defineProperty(navigator, 'webdriver', { get: () => false });
  });
  patch(() => {
    if (navigator.deviceMemory > 8)
      Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
  });
  // WebGL vendor/renderer spoof — masks the software-GL (SwiftShader/ANGLE) tell
  // that our --use-angle=swiftshader crash-workaround otherwise leaks (a top
  // bot-scan flag). Report a plausible desktop-Linux Intel/Mesa GPU consistent
  // with the Linux UA. The override's toString reads native so it isn't itself a
  // trivial hook tell. Technique ported from the OSS clark-browser patch set.
  patch(() => {
    const VENDOR = 37445, RENDERER = 37446;  // UNMASKED_VENDOR/RENDERER_WEBGL
    const spoof = {
      [VENDOR]: 'Google Inc. (Intel)',
      [RENDERER]: 'ANGLE (Intel, Mesa Intel(R) UHD Graphics (CML GT2), '
                + 'OpenGL 4.6 (Core Profile) Mesa 23.2.1)',
    };
    const hook = (proto) => {
      if (!proto || !proto.getParameter) return;
      const orig = proto.getParameter;
      const patched = function (p) { return (p in spoof) ? spoof[p] : orig.call(this, p); };
      try { patched.toString = orig.toString.bind(orig); } catch (e) { /* keep going */ }
      proto.getParameter = patched;
    };
    hook(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    hook(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  });
})();
)JS";

CefRefPtr<CefDictionaryValue> brand(const char* name, const std::string& version) {
    CefRefPtr<CefDictionaryValue> d = CefDictionaryValue::Create();
    d->SetString("brand", name);
    d->SetString("version", version);
    return d;
}

}  // namespace

const char* stealth_init_script() { return kInitScript; }

CefRefPtr<CefRegistration> apply_stealth(CefRefPtr<CefBrowser> browser) {
    if (!browser) return nullptr;

    // Attach a DevTools observer FIRST — this wires the in-process CDP pipe;
    // without it the ExecuteDevToolsMethod calls below are silent no-ops.
    CefRefPtr<CefRegistration> reg =
        browser->GetHost()->AddDevToolsMessageObserver(new StealthDevToolsObserver());

    const std::string major = std::to_string(CHROME_VERSION_MAJOR);
    const std::string full  = major + "." + std::to_string(CHROME_VERSION_MINOR) +
                              "." + std::to_string(CHROME_VERSION_BUILD) +
                              "." + std::to_string(CHROME_VERSION_PATCH);
    // Reduced UA (Chrome/<major>.0.0.0) — matches what modern Chrome actually sends
    // and can't drift from the real build.
    const std::string reducedUa =
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/" + major + ".0.0.0 Safari/537.36";

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
    params->SetString("acceptLanguage", "en-US,en");  // CDP re-weights; no explicit q
    params->SetString("platform", "Linux x86_64");
    params->SetDictionary("userAgentMetadata", meta);
    // In-process only — no remote-debugging port, no Runtime.enable (see header).
    browser->GetHost()->ExecuteDevToolsMethod(0, "Network.setUserAgentOverride", params);

    CefRefPtr<CefDictionaryValue> sp = CefDictionaryValue::Create();
    sp->SetString("source", kInitScript);
    browser->GetHost()->ExecuteDevToolsMethod(0, "Page.addScriptToEvaluateOnNewDocument", sp);
    return reg;
}

}  // namespace ui::cef
