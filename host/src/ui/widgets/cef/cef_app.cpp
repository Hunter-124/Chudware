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
#include "include/wrapper/cef_helpers.h"

#include "cef_platform.h"

namespace ui::cef {
namespace {

bool g_initialized = false;
int    g_argc = 0;       // cached by execute_subprocess for a later argless initialize()
char** g_argv = nullptr;

// External message-pump deadline (ms since steady epoch; -1 = nothing pending).
// CEF calls OnScheduleMessagePumpWork from any thread; the GL/UI loop drains it
// via pump_if_due(). Atomic so the two threads don't need a lock.
std::atomic<long long> g_pump_due{-1};

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
class AiCadApp : public CefApp, public CefBrowserProcessHandler {
public:
    CefRefPtr<CefBrowserProcessHandler> GetBrowserProcessHandler() override { return this; }

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
        // TODO(cef): match the stealth profile — UA/lang are set per-browser via
        // CefBrowserSettings/RequestContext, not here.
    }

    void OnScheduleMessagePumpWork(int64_t delay_ms) override {
        // External-pump model: CEF asks to be pumped in delay_ms. Record the
        // deadline; the GLFW frame loop calls cef::do_message_loop_work() when due
        // (and glfwPostEmptyEvent wakes an idle loop). See cef::pump_due_in_ms().
        schedule_pump(delay_ms);
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

bool pump_if_due() {
    if (!g_initialized) return false;
    const long long due = g_pump_due.load(std::memory_order_relaxed);
    if (due < 0 || now_ms() < due) return false;
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
