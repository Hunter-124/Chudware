// cef/cef_app.cpp — CEF process lifecycle + browser-process CefApp.
// SCAFFOLDING (docs/cef_webview.md phase 2); compiled only with AI_CAD_ENABLE_CEF.
// TODO(cef) markers flag the spots that need verification against a live SDK.
#include "cef_app.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>

#include "include/cef_app.h"
#include "include/cef_command_line.h"
#include "include/cef_frame.h"
#include "include/cef_v8.h"
#include "include/wrapper/cef_helpers.h"

#include "cef_platform.h"
#include "cef_stealth.h"

namespace ui::cef {
namespace {

bool g_initialized = false;
int    g_argc = 0;       // cached by execute_subprocess for a later argless initialize()
char** g_argv = nullptr;

// External message-pump deadline (ms since steady epoch; -1 = nothing pending).
// CEF calls OnScheduleMessagePumpWork from any thread; the GL/UI loop drains it
// via pump_if_due(). Atomic so the two threads don't need a lock.
std::atomic<long long> g_pump_due{-1};

// Live OSR browser count. While > 0 the frame loop pumps CEF every frame (the
// deadline model starves OSR during load); at 0 it pumps only on the deadline.
std::atomic<int> g_live_browsers{0};

// Browsers whose CreateBrowser has been issued but whose OnAfterCreated has not
// yet fired. CreateBrowser is async and needs several message-loop pumps to reach
// OnAfterCreated; the deadline model can under-pump that window and wedge creation
// (no OnAfterCreated → g_live_browsers never rises → permanent "Loading"). While
// > 0 we pump every frame, exactly like a live browser.
std::atomic<int> g_creating{0};

long long now_ms() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
}

void schedule_pump(long long delay_ms) {
    const long long due = now_ms() + (delay_ms < 0 ? 0 : delay_ms);
    long long cur = g_pump_due.load(std::memory_order_relaxed);
    while (cur < 0 || due < cur) {            // keep the earliest deadline
        if (g_pump_due.compare_exchange_weak(cur, due, std::memory_order_relaxed)) break;
    }
}

// Browser-process CefApp: registers windowless rendering and forces the cookie
// crypto that makes CEF's saved sessions readable by the Playwright scraper.
class AiCadApp : public CefApp,
                public CefBrowserProcessHandler,
                public CefRenderProcessHandler {
public:
    CefRefPtr<CefBrowserProcessHandler> GetBrowserProcessHandler() override { return this; }
    CefRefPtr<CefRenderProcessHandler>  GetRenderProcessHandler() override  { return this; }

    // ANTI-DETECTION: inject the document-start fingerprint hardening (window.chrome,
    // plugins, languages, webdriver, deviceMemory cap, WebGL vendor/renderer spoof)
    // at V8-context creation — BEFORE any page script runs, in EVERY frame, with no
    // CDP (so no Runtime.enable automation tell). This is the render-process path;
    // it fires in-process under --single-process too. Mirrors stealth.INIT_SCRIPT.
    void OnContextCreated(CefRefPtr<CefBrowser>, CefRefPtr<CefFrame> frame,
                          CefRefPtr<CefV8Context>) override {
        if (!frame) return;
        frame->ExecuteJavaScript(stealth_init_script(), frame->GetURL(), 0);
        // reCAPTCHA login fix (NOT anti-detection) — MAIN FRAME ONLY, so it never runs
        // in the reCAPTCHA challenge iframe. Attaches a fresh token to a reCAPTCHA-
        // protected login form whose token field is empty at submit; no-op otherwise.
        if (frame->IsMain()) {
            frame->ExecuteJavaScript(recaptcha_login_helper_script(), frame->GetURL(), 0);
            // Opt-in captcha-flow trace (AI_CAD_CEF_NETLOG=1): logs the real reCAPTCHA
            // verdict of a live panel login to the trace file. Off by default.
            if (const char* nl = std::getenv("AI_CAD_CEF_NETLOG"); nl && nl[0] && nl[0] != '0')
                frame->ExecuteJavaScript(recaptcha_trace_script(), frame->GetURL(), 0);
        }
    }

    void OnBeforeCommandLineProcessing(const CefString& /*process_type*/,
                                       CefRefPtr<CefCommandLine> cmd) override {
        // COOKIE INTEROP (docs/cef_webview.md): the scraper (Playwright) and this
        // browser must agree on cookie encryption or sessions won't cross. Pin
        // the Chromium OSCrypt backend to the fixed-key store on both.
        //   → services/browser/stealth.launch_args must add the same flag.
        // CEF 149 uses the Chrome runtime, whose main delegate REQUIRES --lang on
        // subprocesses (Check failed: HasSwitch(kLang)). Set it for every process.
        if (!cmd->HasSwitch("lang"))
            cmd->AppendSwitchWithValue("lang", "en-US");
        cmd->AppendSwitchWithValue("password-store", "basic");
        cmd->AppendSwitch("use-mock-keychain");
        // OSR renders off-screen, so keep the GPU off the native-window/Vulkan
        // path (silences the Wayland+Vulkan incompatibility warning and avoids a
        // GPU-process surface CEF can't present in windowless mode).
        cmd->AppendSwitch("disable-gpu-compositing");
        cmd->AppendSwitch("disable-vulkan");
        // CRASH FIX (coredump-confirmed, SIGTRAP in gl::init::CreateGLContext):
        // --disable-gpu-compositing does NOT stop the GPU service from trying a
        // REAL context for shared-image/canvas work (SharedImageStub::Initialize
        // still calls GpuChannelManager::GetSharedContextState on demand). On
        // this NVIDIA+Wayland box that real EGL context creation fails (ANGLE
        // "eglMakeCurrent failed with 0x00003000") and the failure isn't
        // recovered — the GPU thread hits a fatal CHECK and the whole process
        // dies with SIGTRAP the moment a real page (not a trivial test page)
        // triggers EstablishGpuChannel. Force the software (SwiftShader) GL
        // backend up front so CreateGLContext never probes the real NVIDIA
        // driver at all.
        //
        // BUT SwiftShader is the #1 residual bot-scan tell: a reCAPTCHA-grade scorer
        // fingerprints the canvas/WebGL *pixels*, and a software-GL signature reads as
        // a datacenter/headless browser — so invisible-reCAPTCHA login pages can reject
        // the submission even though the widget renders. The crash that forces
        // SwiftShader is specific to NVIDIA+Wayland EGL; an Intel/Mesa (or other
        // non-crashing) box can drive its REAL GL context for canvas/WebGL and present
        // an honest, consistent GPU fingerprint. Opt in with AI_CAD_CEF_GPU=1 to use the
        // real GL backend (disable-gpu-compositing above keeps OSR presentation software,
        // so paint is unaffected). Default stays SwiftShader — safe on the NVIDIA box this
        // workaround targets. Validate in the HEADED GUI (a display-less daemon has no EGL
        // surface and hangs at CefInitialize under real GL).
        const char* real_gpu = std::getenv("AI_CAD_CEF_GPU");
        if (!(real_gpu && real_gpu[0] && real_gpu[0] != '0')) {
            cmd->AppendSwitchWithValue("use-gl", "angle");
            cmd->AppendSwitchWithValue("use-angle", "swiftshader");
        }
        // Linux: the zygote's fd-passing breaks ICU/resource loading for the
        // separate-helper layout ("Invalid file descriptor to ICU data
        // received"). Disable it so each subprocess loads resources from disk.
        cmd->AppendSwitch("no-zygote");
        // SINGLE-PROCESS by default. On this platform (Fedora 44 + this CEF
        // build) the multi-process helper handshake fails Chromium's subprocess
        // ICU file-descriptor hand-off ("Invalid file descriptor to ICU data
        // received"), which crash-loops the GPU + network services so OSR never
        // paints — the "stuck on Loading" bug. Proven empirically: `AI-CAD
        // --cef-shot` renders a correct PNG ONLY in single-process mode; the
        // multi-process path crash-loops to "GPU process isn't usable. Goodbye."
        // Single-process ≠ single-thread — Chromium still runs the renderer on
        // its own threads, so the GL/UI thread is not blocked by page JS; the
        // only cost is software (SwiftShader) rasterisation, fine for OSR login
        // pages. Override with AI_CAD_CEF_SINGLE_PROCESS=0 to test multi-process
        // once the subprocess ICU hand-off is fixed for this distro.
        if (const char* sp = std::getenv("AI_CAD_CEF_SINGLE_PROCESS"); !(sp && sp[0] == '0'))
            cmd->AppendSwitch("single-process");
        // ANTI-DETECTION (mirror services/browser/stealth.launch_args): vendor
        // login pages run bot-walls (Cloudflare/Turnstile) that reject anything
        // that looks automated. These flags remove the cheap automation tells:
        //   AutomationControlled → flips navigator.webdriver to false (the #1 tell)
        //   no-first-run / no-default-browser-check / disable-infobars → no
        //     "Chrome is being controlled by automated software" banner or
        //     first-run state that fingerprinters key on.
        // The document-start JS patches (window.chrome, plugins, languages,
        // webdriver, deviceMemory, WebGL spoof) are injected in the RENDER process at
        // OnContextCreated (this file, below) from the ONE source of truth
        // stealth_init_script()/kInitScript (cef_stealth.cpp) — it fires for every
        // frame incl. cross-origin reCAPTCHA iframes, which a browser-process CDP
        // addScript cannot reach. Do NOT re-add a per-browser CDP inject in
        // cef_web_view.cpp; only the Sec-CH-UA override there genuinely needs CDP.
        // NOTE: the software (SwiftShader) WebGL vendor is a residual tell removable
        // only via AI_CAD_CEF_GPU=1 (see use-angle above) on non-NVIDIA boxes; strict
        // walls may still trip on the software canvas pixels.
        // CLIPBOARD: DO NOT pin --ozone-platform=x11. It was tried as an OS
        // clipboard bridge (the default headless Ozone clipboard is an in-memory
        // stub) and CRASHED: pasting corrupted the heap ("malloc(): corrupted
        // top size", coredump PID 56925) — the X11 Ozone clipboard path is not
        // safe in this windowless single-process setup. External paste is
        // bridged at the app layer instead: cef_web_view.cpp intercepts Ctrl+V,
        // reads the host (GLFW/Wayland) clipboard, and injects the text via CDP
        // Input.insertText, bypassing Chromium's clipboard entirely.
        cmd->AppendSwitch("disable-blink-features=AutomationControlled");
        cmd->AppendSwitch("no-first-run");
        cmd->AppendSwitch("no-default-browser-check");
        cmd->AppendSwitch("disable-infobars");
        // NB: NO explicit q-values — Chromium adds the ;q weight to the HTTP
        // Accept-Language header itself, and derives navigator.languages from this
        // list. Passing "en-US,en;q=0.9" leaks the malformed entry "en;q=0.9" into
        // navigator.languages (a fingerprint tell). "en-US,en" → languages
        // ["en-US","en"] + header "en-US,en;q=0.9". (Mirrors stealth._CDP_ACCEPT_LANGUAGE.)
        if (!cmd->HasSwitch("accept-lang"))
            cmd->AppendSwitchWithValue("accept-lang", "en-US,en");
        // EXPERIMENT HOOK: AI_CAD_CEF_EXTRA_SWITCHES="a=1;b;c=2" appends extra
        // Chromium switches (';'-separated, 'key' or 'key=value') so flag
        // hypotheses can be tested against the --cef-shot repro without a rebuild.
        if (const char* extra = std::getenv("AI_CAD_CEF_EXTRA_SWITCHES")) {
            std::string s(extra), tok;
            std::size_t i = 0;
            while (i <= s.size()) {
                if (i == s.size() || s[i] == ';') {
                    if (!tok.empty()) {
                        const auto eq = tok.find('=');
                        if (eq == std::string::npos) cmd->AppendSwitch(tok);
                        else cmd->AppendSwitchWithValue(tok.substr(0, eq), tok.substr(eq + 1));
                    }
                    tok.clear();
                } else tok += s[i];
                ++i;
            }
        }
    }

    void OnScheduleMessagePumpWork(int64_t delay_ms) override {
        // External-pump model: CEF asks to be pumped in delay_ms. Record the
        // deadline; the GLFW frame loop calls cef::do_message_loop_work() when due
        // (and glfwPostEmptyEvent wakes an idle loop). See cef::pump_due_in_ms().
        schedule_pump(delay_ms);
    }

    // CRASH FIX (coredump-confirmed, SIGTRAP in RenderProcessHostImpl::
    // GetProcessHostForSiteInstance via ProcessSingletonNotificationCallbackImpl →
    // StartupBrowserCreator → WebContentsImpl::CreateWithOpener, PID 18704): CEF 149
    // runs on the Chrome runtime, which holds a process-singleton lock keyed on
    // root_cache_path. When something (an OAuth/external-protocol navigation during
    // a vendor login, or the user re-launching AI-CAD) rendezvouses a second launch
    // into this instance, CEF's DEFAULT relaunch behavior "creates a new default
    // styled Chrome window" — a brand-new top-level WebContents, i.e. a second
    // renderer process, which is fatal under --single-process. Returning true here
    // suppresses that default entirely: we never want a second Chrome window in an
    // embedded OSR app. The forwarded command line is dropped (there is nowhere in
    // the embedded UI to honor a new-window request); the live login view keeps its
    // page. This is on the browser-process UI thread.
    bool OnAlreadyRunningAppRelaunch(CefRefPtr<CefCommandLine> /*command_line*/,
                                     const CefString& /*current_directory*/) override {
        return true;   // handled — do NOT open a new Chrome window (would crash)
    }

    IMPLEMENT_REFCOUNTING(AiCadApp);
};

CefRefPtr<AiCadApp> g_app;

}  // namespace

void stash_args(int argc, char** argv) {
    g_argc = argc;
    g_argv = argv;
}

int execute_subprocess(int argc, char** argv) {
    stash_args(argc, argv);
    CefMainArgs args = cef_platform::main_args(argc, argv);
    if (!g_app) g_app = new AiCadApp();
    // >=0 in a helper process (caller returns it); -1 otherwise.
    return CefExecuteProcess(args, g_app.get(), nullptr);
}

bool initialize() {
    if (g_initialized) return true;

    CefSettings settings;
    settings.windowless_rendering_enabled = 1;
    settings.external_message_pump = 1;               // pair with OnScheduleMessagePump
    settings.multi_threaded_message_loop = 0;
    const std::string dir = cef_platform::executable_dir();
    CefString(&settings.browser_subprocess_path) = cef_platform::helper_path();
    // Point CEF at the runtime staged next to the binary (icudtl.dat, *.pak,
    // v8 snapshot, locales/) — otherwise Chromium fails with "Invalid file
    // descriptor to ICU data received".
    CefString(&settings.resources_dir_path) = dir;
    CefString(&settings.locales_dir_path)   = dir + "/locales";
    // root_cache_path is the parent every per-site CefRequestContextSettings.
    // cache_path must live under (CEF 149 requirement). CefWebView opens each
    // vendor in its own per-site context = cache_root()/<slug> so cookie jars stay
    // separate. This tree is CEF's own (NOT the Playwright profiles) until the
    // cookie-interop pass — see cef_platform::cache_root().
    CefString(&settings.root_cache_path) = cef_platform::cache_root();
    // Keep CEF's DEFAULT user agent (a genuine "Chrome/149… Safari/537.36" string
    // with no Headless/CEF token) — a hand-rolled UA that doesn't match the native
    // Sec-CH-UA client hints is a WORSE tell than the default. Only pin the
    // language list so Accept-Language + navigator.languages stay consistent.
    CefString(&settings.accept_language_list) = "en-US,en";  // no q-values (see accept-lang note)
    cef_platform::configure_settings(settings);

    if (!g_app) g_app = new AiCadApp();
    // Pass CEF a CLEAN command line (program name only). The app's own flags
    // (--ui-screenshot, a doc path, …) are not Chromium switches and can make
    // ContentMain misclassify the process (e.g. take the subprocess ICU-fd path,
    // aborting with "Invalid file descriptor to ICU data received").
    static char  lang_sw[]     = "--lang=en-US";
    static char* clean_argv[2] = {g_argv ? g_argv[0] : const_cast<char*>("AI-CAD"),
                                  lang_sw};
    CefMainArgs args(2, clean_argv);
    std::fprintf(stderr, "[cef] calling CefInitialize (subproc=%s)\n",
                 cef_platform::helper_path().c_str());
    g_initialized = CefInitialize(args, settings, g_app.get(), nullptr);
    std::fprintf(stderr, "[cef] CefInitialize %s\n", g_initialized ? "OK" : "FAILED");
    return g_initialized;
}

bool initialized() { return g_initialized; }

void note_browser_creating() { g_creating.fetch_add(1, std::memory_order_relaxed); }

void note_browser_create_failed() {
    int prev = g_creating.fetch_sub(1, std::memory_order_relaxed);
    if (prev <= 0) g_creating.store(0, std::memory_order_relaxed);
}

void note_browser_opened() {
    g_live_browsers.fetch_add(1, std::memory_order_relaxed);
    note_browser_create_failed();   // creation settled: hand off from g_creating to g_live_browsers
}
void note_browser_closed() {
    int prev = g_live_browsers.fetch_sub(1, std::memory_order_relaxed);
    if (prev <= 0) g_live_browsers.store(0, std::memory_order_relaxed);  // never negative
}

bool pump_if_due() {
    if (!g_initialized) return false;
    // Pump EVERY frame while any browser is alive OR is being created (live count
    // is bumped in OnAfterCreated, which itself needs a pump — so also pump while
    // a create is pending, tracked by a nonzero deadline). The external-pump
    // deadline model proved unreliable for interactive OSR here: the panel hung on
    // "Loading" while the --cef-shot path, which pumps continuously, worked. This
    // matches that: the GLFW loop already spins at 60 fps (glfwPollEvents), so an
    // unconditional pump-per-frame costs nothing extra and removes all dependence
    // on OnScheduleMessagePumpWork firing. When fully idle (no browser, no pending
    // deadline) we skip the call so a closed-panel app isn't needlessly poked.
    const bool anyLive    = g_live_browsers.load(std::memory_order_relaxed) > 0;
    const bool creating   = g_creating.load(std::memory_order_relaxed) > 0;
    const bool createPend = g_pump_due.load(std::memory_order_relaxed) >= 0;
    // Pump every frame while a browser is live OR a CreateBrowser is in flight
    // (OnAfterCreated pending) — the latter closes the starvation window that left
    // the vendor panel on "Loading" forever. Otherwise pump only on CEF's deadline.
    if (!anyLive && !creating && !createPend) return false;
    g_pump_due.store(-1, std::memory_order_relaxed);  // CEF reschedules the next
    CefDoMessageLoopWork();
    return true;
}

void shutdown() {
    if (!g_initialized) return;
    CefShutdown();
    g_initialized = false;
}

}  // namespace ui::cef
