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

// reCAPTCHA login fix. Some vendor logins (e.g. anaheimautomation's Magento MSP
// invisible reCAPTCHA) bind reCAPTCHA to the form submit, but that execute-then-
// submit handoff does not attach a token in our OSR browser, so the form POSTs an
// empty token and the server answers "reCAPTCHA verification failed" — even though
// grecaptcha.execute() itself works here (proven: it returns a full token). This
// shim closes the gap WITHOUT any bot-evasion: on a real submit of a reCAPTCHA-
// protected form whose token field is still empty, it generates a fresh token from
// the page's own sitekey via grecaptcha and attaches it before the POST, then lets
// the form submit normally (preserving form_key / session). It is a pure no-op on
// every other form and every page without an empty reCAPTCHA field. Main frame only
// (never the reCAPTCHA challenge iframe). Idempotent under the double-inject.
const char* kRecaptchaLoginHelper = R"JS(
(() => {
  if (window.__aicadRcHook) return; window.__aicadRcHook = true;
  const RESP = 'textarea[name="g-recaptcha-response"],input[name="g-recaptcha-response"]';
  const findSitekey = (form) => {
    const el = (form && form.querySelector('[data-sitekey]')) || document.querySelector('[data-sitekey]');
    if (el && el.getAttribute('data-sitekey')) return el.getAttribute('data-sitekey');
    // Magento MSP and other explicit-render setups keep the key in page config; a
    // Google sitekey is a 40-char 6L… token. Match the first one in the document.
    try { const m = document.documentElement.innerHTML.match(/6L[0-9A-Za-z_-]{38}/); if (m) return m[0]; } catch (e) {}
    return null;
  };
  const ensureToken = (form) => {
    // Already carries a token → nothing to do.
    const existing = form.querySelector(RESP);
    if (existing && existing.value && existing.value.length > 20) return false;
    if (typeof grecaptcha === 'undefined' || !grecaptcha.render || !grecaptcha.execute) return false;
    const sitekey = findSitekey(form);
    if (!sitekey) return false;
    let slot = document.getElementById('__aicad_rc_slot');
    if (!slot) { slot = document.createElement('div'); slot.id = '__aicad_rc_slot';
      slot.style.position = 'fixed'; slot.style.left = '-9999px'; slot.style.bottom = '0'; document.body.appendChild(slot); }
    let wid;
    try { wid = grecaptcha.render(slot, { sitekey, size: 'invisible', callback: (tok) => {
      try {
        let fields = form.querySelectorAll(RESP);
        if (!fields.length) { const h = document.createElement('input'); h.type = 'hidden';
          h.name = 'g-recaptcha-response'; form.appendChild(h); fields = [h]; }
        for (const f of fields) f.value = tok;
        // Magento rotates its CSRF form_key via a cookie; after our async token
        // wait the form's hidden form_key can be stale → a silent "Invalid Form Key"
        // redirect. Refresh it from the live cookie so the POST is accepted.
        try { const m = document.cookie.match(/(?:^|;\s*)form_key=([^;]+)/);
          if (m) { const fk = form.querySelector('[name="form_key"]');
            if (fk) fk.value = decodeURIComponent(m[1]); } } catch (e) {}
        form.__aicadRcSubmitting = true;   // let this pass straight through the hook
        if (form.requestSubmit) form.requestSubmit(); else form.submit();
      } catch (e) {}
    } }); } catch (e) { return false; }   // widget already rendered / not ready → don't block
    try { grecaptcha.execute(wid); } catch (e) { return false; }
    return true;   // we took over: prevent the original (broken) submit
  };
  document.addEventListener('submit', (ev) => {
    const form = ev.target;
    if (!form || form.tagName !== 'FORM' || form.__aicadRcSubmitting) return;
    try { if (ensureToken(form)) { ev.preventDefault(); ev.stopImmediatePropagation(); } } catch (e) {}
  }, true);   // capture phase: run before the vendor's own (broken) submit handler
})();
)JS";

}  // namespace

const char* stealth_init_script() { return kInitScript; }

const char* recaptcha_login_helper_script() { return kRecaptchaLoginHelper; }

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
