// Chudware native host.
//
// Modes:
//   Chudware --render  <in.stl> <out.png> [view]   single offscreen view (iso/front/right/top)
//   Chudware --contact <in.stl> <out.png>          2x2 contact sheet
//   Chudware [--gui]                               interactive desktop app (ImGui)
//
// The offscreen modes need only a hidden GL context, so they verify the whole render
// path headlessly. GUI mode is in gui.cpp.
#include "gl_compat.h"
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#endif
//
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>
#include <stb_image_write.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <system_error>
#include <vector>

#include "bootstrap.h"
#include "core/app_state.h"  // appstateInitPersist (WASM IDBFS)
#include "mesh.h"
#include "renderer.h"
#include "worker_client.h"

#ifdef CHUDWARE_ENABLE_CEF
#include "ui/widgets/cef/cef_app.h"
#include "ui/widgets/cef/cef_osr_shot.h"
#include "ui/widgets/cef/cef_headless.h"
#include "ui/widgets/cef/cef_daemon.h"
#endif

#ifdef _WIN32
#include <windows.h>
#include <dbghelp.h>
#include <ctime>
#endif

int run_gui(const char* screenshotPath, const char* openPath = nullptr);  // gui_app.cpp

namespace {

#ifdef _WIN32
// ---- crash diagnostics ---------------------------------------------------------------------
// On any unhandled SEH fault (access violation, etc.) write a symbolized call stack + a minidump
// next to the exe (and append the stack to $CHUDWARE_LOG_FILE when set) so an "instant hard crash"
// reported in the field gives us the exact faulting frame instead of a silent window-close.
LONG WINAPI aicadCrashFilter(EXCEPTION_POINTERS* ep) {
    char dir[MAX_PATH] = {0};
    GetModuleFileNameA(nullptr, dir, MAX_PATH);
    std::string base(dir);
    size_t slash = base.find_last_of("\\/");
    base = (slash == std::string::npos) ? std::string(".") : base.substr(0, slash);

    char stamp[32];
    std::time_t now = std::time(nullptr);
    std::strftime(stamp, sizeof(stamp), "%Y%m%d_%H%M%S", std::localtime(&now));
    std::string stackPath = base + "\\Billet_crash_" + stamp + ".txt";
    std::string dmpPath   = base + "\\Billet_crash_" + stamp + ".dmp";

    std::ofstream out(stackPath, std::ios::binary);
    auto emit = [&](const std::string& line) {
        if (out) out << line << "\n";
        std::fprintf(stderr, "%s\n", line.c_str());
    };
    char hdr[128];
    std::snprintf(hdr, sizeof(hdr), "Chudware CRASH  code=0x%08lX  addr=%p",
                  ep->ExceptionRecord->ExceptionCode,
                  ep->ExceptionRecord->ExceptionAddress);
    emit(hdr);

    HANDLE proc = GetCurrentProcess();
    SymSetOptions(SYMOPT_LOAD_LINES | SYMOPT_DEFERRED_LOADS | SYMOPT_UNDNAME);
    SymInitialize(proc, nullptr, TRUE);

    CONTEXT ctx = *ep->ContextRecord;
    STACKFRAME64 frame = {};
    DWORD machine;
#if defined(_M_X64)
    machine = IMAGE_FILE_MACHINE_AMD64;
    frame.AddrPC.Offset = ctx.Rip;     frame.AddrPC.Mode = AddrModeFlat;
    frame.AddrFrame.Offset = ctx.Rbp;  frame.AddrFrame.Mode = AddrModeFlat;
    frame.AddrStack.Offset = ctx.Rsp;  frame.AddrStack.Mode = AddrModeFlat;
#else
    machine = IMAGE_FILE_MACHINE_I386;
    frame.AddrPC.Offset = ctx.Eip;     frame.AddrPC.Mode = AddrModeFlat;
    frame.AddrFrame.Offset = ctx.Ebp;  frame.AddrFrame.Mode = AddrModeFlat;
    frame.AddrStack.Offset = ctx.Esp;  frame.AddrStack.Mode = AddrModeFlat;
#endif
    for (int i = 0; i < 64; ++i) {
        if (!StackWalk64(machine, proc, GetCurrentThread(), &frame, &ctx, nullptr,
                         SymFunctionTableAccess64, SymGetModuleBase64, nullptr))
            break;
        if (frame.AddrPC.Offset == 0) break;

        DWORD64 modBase = SymGetModuleBase64(proc, frame.AddrPC.Offset);
        char modName[MAX_PATH] = "?";
        if (modBase) {
            char path[MAX_PATH];
            if (GetModuleFileNameA((HMODULE)modBase, path, MAX_PATH)) {
                const char* s = std::strrchr(path, '\\');
                std::snprintf(modName, sizeof(modName), "%s", s ? s + 1 : path);
            }
        }
        char symBuf[sizeof(SYMBOL_INFO) + 512] = {0};
        auto* sym = reinterpret_cast<SYMBOL_INFO*>(symBuf);
        sym->SizeOfStruct = sizeof(SYMBOL_INFO);
        sym->MaxNameLen = 511;
        DWORD64 disp = 0;
        char line[700];
        if (SymFromAddr(proc, frame.AddrPC.Offset, &disp, sym)) {
            IMAGEHLP_LINE64 il = {sizeof(IMAGEHLP_LINE64)};
            DWORD ld = 0;
            if (SymGetLineFromAddr64(proc, frame.AddrPC.Offset, &ld, &il))
                std::snprintf(line, sizeof(line), "  #%02d %s!%s+0x%llX  (%s:%lu)", i, modName,
                              sym->Name, (unsigned long long)disp, il.FileName, il.LineNumber);
            else
                std::snprintf(line, sizeof(line), "  #%02d %s!%s+0x%llX", i, modName, sym->Name,
                              (unsigned long long)disp);
        } else {
            std::snprintf(line, sizeof(line), "  #%02d %s!0x%llX", i, modName,
                          (unsigned long long)frame.AddrPC.Offset);
        }
        emit(line);
    }
    out.close();

    // Full minidump for offline analysis (cdb/WinDbg).
    HANDLE fh = CreateFileA(dmpPath.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,
                            FILE_ATTRIBUTE_NORMAL, nullptr);
    if (fh != INVALID_HANDLE_VALUE) {
        MINIDUMP_EXCEPTION_INFORMATION mei = {GetCurrentThreadId(), ep, FALSE};
        MiniDumpWriteDump(proc, GetCurrentProcessId(), fh,
                          (MINIDUMP_TYPE)(MiniDumpWithIndirectlyReferencedMemory |
                                          MiniDumpWithDataSegs),
                          &mei, nullptr, nullptr);
        CloseHandle(fh);
    }

    // Mirror the stack into the running log file if one is configured.
    if (const char* lf = std::getenv("CHUDWARE_LOG_FILE")) {
        std::ifstream in(stackPath, std::ios::binary);
        std::ofstream log(lf, std::ios::app | std::ios::binary);
        if (in && log) log << "\n" << in.rdbuf() << "(minidump: " << dmpPath << ")\n";
    }
    SymCleanup(proc);
    return EXCEPTION_EXECUTE_HANDLER;  // let the process die after we've recorded the fault
}

void installCrashHandler() { SetUnhandledExceptionFilter(aicadCrashFilter); }
#else
void installCrashHandler() {}
#endif

#ifndef _WIN32
// Declared extern "C" at namespace scope so it keeps external linkage even inside an
// unnamed namespace (a plain `extern` in the function got internal linkage -> link error).
extern "C" char** environ;
#endif

// ---- Chudware env-var shim -------------------------------------------------------------------
// CHUDWARE_* is the canonical public env-var prefix (deep-rename); CHUDWARE_* / CHUDWARE_* remain the
// internal transport read throughout the codebase. For every CHUDWARE_X present in the
// environment, set CHUDWARE_X and CHUDWARE_X when not already set, so a user exporting only the new
// names reaches every legacy read site. Never overwrites an explicitly-set legacy var.
void propagateBilletEnv() {
#ifdef _WIN32
    char** envp = _environ;
#else
    char** envp = environ;
#endif
    if (!envp) return;
    constexpr const char* kPrefix = "CHUDWARE_";
    constexpr size_t kPrefixLen = 7;
    for (char** e = envp; *e; ++e) {
        const char* kv = *e;
        if (std::strncmp(kv, kPrefix, kPrefixLen) != 0) continue;
        const char* eq = std::strchr(kv, '=');
        if (!eq) continue;
        const std::string suffix(kv + kPrefixLen, static_cast<size_t>(eq - (kv + kPrefixLen)));
        if (suffix.empty()) continue;
        const std::string value(eq + 1);
        for (const std::string& legacy : {"CHUDWARE_" + suffix, "CHUDWARE_" + suffix}) {
            if (!std::getenv(legacy.c_str())) {
#ifdef _WIN32
                _putenv_s(legacy.c_str(), value.c_str());
#else
                setenv(legacy.c_str(), value.c_str(), 0);
#endif
            }
        }
    }
}

GLFWwindow* makeContext(int w, int h, bool visible) {
    if (!glfwInit()) {
        std::fprintf(stderr, "glfwInit failed\n");
        return nullptr;
    }
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
    glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
#ifdef __APPLE__
    glfwWindowHint(GLFW_OPENGL_FORWARD_COMPAT, GL_TRUE);
#endif
    glfwWindowHint(GLFW_VISIBLE, visible ? GLFW_TRUE : GLFW_FALSE);
    GLFWwindow* win = glfwCreateWindow(w, h, "Chudware", nullptr, nullptr);
    if (!win) {
        std::fprintf(stderr, "window/context creation failed\n");
        glfwTerminate();
        return nullptr;
    }
    glfwMakeContextCurrent(win);
#ifndef __EMSCRIPTEN__
    if (!gladLoadGL((GLADloadfunc)glfwGetProcAddress)) {
        std::fprintf(stderr, "glad load failed\n");
        return nullptr;
    }
#endif
    return win;
}

// Paste a w x h RGBA image into a quadrant of a 2w x 2h canvas.
void paste(std::vector<unsigned char>& canvas, int cw, const std::vector<unsigned char>& img,
           int iw, int ih, int ox, int oy) {
    for (int y = 0; y < ih; ++y)
        for (int x = 0; x < iw; ++x) {
            const unsigned char* s = &img[(y * iw + x) * 4];
            unsigned char* d = &canvas[((oy + y) * cw + (ox + x)) * 4];
            for (int c = 0; c < 4; ++c) d[c] = s[c];
        }
}

int renderSingle(const std::string& stl, const std::string& out, const std::string& view) {
    GLFWwindow* win = makeContext(64, 64, false);
    if (!win) return 2;
    Mesh mesh;
    std::string err;
    if (!loadBinarySTL(stl, mesh, err)) {
        std::fprintf(stderr, "STL load failed: %s\n", err.c_str());
        return 3;
    }
    Renderer r;
    if (!r.init(1024, 768, err)) {
        std::fprintf(stderr, "renderer init failed: %s\n", err.c_str());
        return 4;
    }
    r.setMesh(mesh);
    r.renderView(view);
    auto px = r.readRGBA();
    if (!stbi_write_png(out.c_str(), r.width(), r.height(), 4, px.data(), r.width() * 4)) {
        std::fprintf(stderr, "png write failed: %s\n", out.c_str());
        return 5;
    }
    std::printf("rendered %s [%s] -> %s (%zu triangles)\n", stl.c_str(), view.c_str(),
                out.c_str(), mesh.triangleCount());
    glfwTerminate();
    return 0;
}

int renderContact(const std::string& stl, const std::string& out) {
    GLFWwindow* win = makeContext(64, 64, false);
    if (!win) return 2;
    Mesh mesh;
    std::string err;
    if (!loadBinarySTL(stl, mesh, err)) {
        std::fprintf(stderr, "STL load failed: %s\n", err.c_str());
        return 3;
    }
    const int qw = 512, qh = 384;  // quadrant size
    Renderer r;
    if (!r.init(qw, qh, err)) {
        std::fprintf(stderr, "renderer init failed: %s\n", err.c_str());
        return 4;
    }
    r.setMesh(mesh);

    const char* views[4] = {"iso", "front", "right", "top"};
    std::vector<unsigned char> canvas(qw * 2 * qh * 2 * 4, 235);
    for (int i = 0; i < 4; ++i) {
        r.renderView(views[i]);
        auto px = r.readRGBA();
        int ox = (i % 2) * qw, oy = (i / 2) * qh;
        paste(canvas, qw * 2, px, qw, qh, ox, oy);
    }
    if (!stbi_write_png(out.c_str(), qw * 2, qh * 2, 4, canvas.data(), qw * 2 * 4)) {
        std::fprintf(stderr, "png write failed: %s\n", out.c_str());
        return 5;
    }
    std::printf("contact sheet -> %s (%zu triangles)\n", out.c_str(), mesh.triangleCount());
    glfwTerminate();
    return 0;
}

}  // namespace

// Drive the Python worker: build a solid (from a .py file / inline code), then render the
// resulting STL to a contact sheet — proving the whole native pipeline: host -> worker ->
// build123d -> STL -> GL render -> PNG. (The --design mode was removed in T-RF5 with the
// legacy agent_design loop; the in-app copilot is the design path.)
int buildAndRender(const std::string& src, const std::string& out) {
    std::string code = src;
    {
        std::ifstream f(src, std::ios::binary);
        if (f) {
            std::ostringstream ss;
            ss << f.rdbuf();
            code = ss.str();  // src was a path to a .py script
        }
    }
    std::string blog;
    if (!ensureRuntime(blog)) {
        std::fprintf(stderr, "runtime not ready: %s\n", blog.c_str());
        return 9;
    }
    WorkerClient w;
    std::string err;
    if (!w.start(err)) {
        std::fprintf(stderr, "worker start failed: %s\n", err.c_str());
        return 6;
    }
    nlohmann::json result;
    const bool ok = w.call("run_model", {{"code", code}, {"name", "host_build"}}, result, err,
                           120000);
    if (!ok) {
        std::fprintf(stderr, "worker call failed: %s\n", err.c_str());
        return 7;
    }
    std::string stl = result.value("stl_path", "");
    if (stl.empty()) {
        std::fprintf(stderr, "no STL produced. worker result:\n%s\n", result.dump(2).c_str());
        return 8;
    }
    w.stop();
    return renderContact(stl, out);
}

// Map legacy AI_CAD_/AICAD_/BILLET_/SHOPCRAFT_ environment variables onto their CHUDWARE_
// equivalents when the new name is unset, so pre-rename scripts and environments keep
// working after the Chudware rename. Runs before anything reads config/env.
static void applyLegacyEnvCompat() {
#if defined(_WIN32)
    char** envp = _environ;
#else
    char** envp = environ;  // extern "C" declared at file scope above (line 167)
#endif
    if (!envp) return;
    static const char* kPrefixes[] = {"AI_CAD_", "AICAD_", "BILLET_", "SHOPCRAFT_"};
    std::vector<std::string> entries;
    for (char** e = envp; *e; ++e) entries.emplace_back(*e);  // snapshot: setenv may realloc environ
    for (const std::string& kv : entries) {
        const std::size_t eq = kv.find('=');
        if (eq == std::string::npos) continue;
        const std::string key = kv.substr(0, eq);
        for (const char* pfx : kPrefixes) {
            const std::size_t plen = std::strlen(pfx);
            if (key.size() > plen && key.compare(0, plen, pfx) == 0) {
                const std::string neu = "CHUDWARE_" + key.substr(plen);
                if (!std::getenv(neu.c_str())) {
#if defined(_WIN32)
                    _putenv_s(neu.c_str(), kv.substr(eq + 1).c_str());
#else
                    setenv(neu.c_str(), kv.substr(eq + 1).c_str(), 0);
#endif
                }
                break;
            }
        }
    }
}

int main(int argc, char** argv) {
    applyLegacyEnvCompat();  // legacy env-var back-compat (pre-Chudware rename)
#ifdef __EMSCRIPTEN__
    // Web client: skip desktop CLI modes entirely. Report progress via Module.setStatus
    // (shell.html) which is reliable from the browser main thread.
    EM_ASM({
        try {
            if (Module.setStatus) Module.setStatus('main enter');
            if (!window.__billetLogs) window.__billetLogs = [];
            window.__billetLogs.push('boot: main enter');
            console.log('boot: main enter');
        } catch (e) { console.error(e); }
    });
    std::fprintf(stderr, "[wasm-boot] main enter\n");
    std::fflush(stderr);
    // Mount IDBFS at /persist and populate from IndexedDB BEFORE any appdata
    // read (loadSettings / chat_history). Blocks via ASYNCIFY until ready.
    appstateInitPersist();
    propagateBilletEnv();
    ensurePromptsDir();
    EM_ASM({
        try {
            if (Module.setStatus) Module.setStatus('calling run_gui');
            if (!window.__billetLogs) window.__billetLogs = [];
            window.__billetLogs.push('boot: calling run_gui');
            console.log('boot: calling run_gui');
        } catch (e) { console.error(e); }
    });
    const int rc = run_gui(nullptr);
    EM_ASM({
        try {
            if (Module.setStatus) Module.setStatus('run_gui returned ' + $0);
            if (!window.__billetLogs) window.__billetLogs = [];
            window.__billetLogs.push('boot: run_gui returned ' + $0);
        } catch (e) {}
    }, rc);
    return rc;
#else
    // FIRST: mirror CHUDWARE_* env vars onto the legacy CHUDWARE_*/CHUDWARE_* names every
    // subsystem (and every child process we spawn) actually reads.
    propagateBilletEnv();
#ifdef CHUDWARE_ENABLE_CEF
    // A separate subprocess executable (ai_cad_cef_helper) is configured via
    // CefSettings.browser_subprocess_path, so this main binary is ALWAYS the
    // browser process and must NOT call CefExecuteProcess. Initialise CEF HERE,
    // before GLFW/X11 grab the display and signal handlers — CefInitialize wants
    // to bootstrap Chromium first. Only when the CEF backend is selected.
    ui::cef::stash_args(argc, argv);
    // Headless OSR self-test: `Chudware --cef-shot <url> <out.png>` renders a page
    // off-screen via CEF and writes a PNG — proves the browser pipeline with no
    // display. Returns before any GUI/GLFW setup.
    if (argc >= 4 && std::strcmp(argv[1], "--cef-shot") == 0) {
        const bool ok = ui::cef::initialize() &&
                        ui::cef::render_url_to_png(argv[2], 1280, 800, argv[3]);
        std::fprintf(stderr, "[cef-shot] %s -> %s\n", ok ? "OK" : "FAILED", argv[3]);
        ui::cef::shutdown();
        return ok ? 0 : 1;
    }
    // Headless page fetch: `Chudware --cef-fetch <url> [out.html]` drives CEF (not
    // Playwright) to load a page and print/write its post-JS HTML. This is the
    // production headless path the Python scraper calls via RPC; the CLI form is
    // the standalone/one-off entry. Prints HTML to stdout when no file is given.
    if (argc >= 3 && std::strcmp(argv[1], "--cef-fetch") == 0) {
        if (!ui::cef::initialize()) { ui::cef::shutdown(); return 1; }
        ui::cef::FetchResult r = ui::cef::headless_fetch(argv[2]);
        if (r.ok) {
            if (argc >= 4) {
                std::ofstream f(argv[3], std::ios::binary);
                f.write(r.html.data(), static_cast<std::streamsize>(r.html.size()));
            } else {
                std::fwrite(r.html.data(), 1, r.html.size(), stdout);
            }
        }
        std::fprintf(stderr, "[cef-fetch] %s status=%d %s\n",
                     r.ok ? "OK" : "FAILED", r.status, r.final_url.c_str());
        ui::cef::shutdown_and_exit(r.ok ? 0 : 1);  // watchdog'd; does not return
    }
    // Headless file download: `Chudware --cef-download <url> <dest>` pulls raw bytes
    // (datasheet/CAD) through CEF's network stack with the profile's cookies.
    if (argc >= 4 && std::strcmp(argv[1], "--cef-download") == 0) {
        const bool ok = ui::cef::initialize() &&
                        ui::cef::headless_download(argv[2], argv[3]);
        std::fprintf(stderr, "[cef-download] %s -> %s\n", ok ? "OK" : "FAILED", argv[3]);
        ui::cef::shutdown_and_exit(ok ? 0 : 1);  // watchdog'd; does not return
    }
    // CEF host daemon: `Chudware --cef-daemon <sock> [idle_secs]` serves headless
    // fetch/download to the Python scraper over the socket and idle-exits. Lazily
    // spawned by services/browser/cef_client.py; the sole CEF owner (Phase 2).
    if (argc >= 3 && std::strcmp(argv[1], "--cef-daemon") == 0) {
        const int idle = argc >= 4 ? std::atoi(argv[3]) : 120;
        return ui::cef::run_cef_daemon(argv[2], idle);
    }
#endif
    installCrashHandler();  // symbolized stack + minidump on any unhandled fault
    // Point the C++ core at its prompts on EVERY launch path (run_gui only calls
    // ensureRuntime when provisioning is needed, so it can't be relied on for this).
    ensurePromptsDir();
    if (argc >= 4 && std::strcmp(argv[1], "--render") == 0)
        return renderSingle(argv[2], argv[3], argc >= 5 ? argv[4] : "iso");
    if (argc >= 4 && std::strcmp(argv[1], "--contact") == 0)
        return renderContact(argv[2], argv[3]);
    if (argc >= 4 && std::strcmp(argv[1], "--build") == 0)
        return buildAndRender(argv[2], argv[3]);
    if (argc >= 2 && std::strcmp(argv[1], "--ensure-runtime") == 0) {
        std::string log;
        bool ok = ensureRuntime(log);
        std::printf("%s\n", log.c_str());
        return ok ? 0 : 1;
    }
#ifdef CHUDWARE_ENABLE_CEF
    // GUI path always boots CEF here — before run_gui / GLFW. Chromium must own
    // display + signal setup first; initializing after GLFW (true "lazy" first-
    // web-surface) aborts on Linux vendor open (SIGABRT / joinable-thread
    // terminate on the way out). Headless CEF modes above already returned.
    // Experimental only: CHUDWARE_CEF_LAZY=1 skips cold init (CefWebView::open
    // still calls initialize()); do not use for vendor sign-in.
    const bool cefLazy = [] {
        const char* v = std::getenv("CHUDWARE_CEF_LAZY");
        return v && v[0] && v[0] != '0';
    }();
    // CHUDWARE_CEF_EARLY=1 remains accepted as an alias for "eager" (no-op when
    // lazy is unset). Prefer leaving both unset for the production path.
    if (!cefLazy) ui::cef::initialize();
#endif
    if (argc >= 2 && std::strcmp(argv[1], "--ui-screenshot") == 0) {
        // Headless UI capture. An explicit output path is optional; when omitted (the common
        // smoke-test invocation `Chudware --ui-screenshot`), default to a PNG next to the exe so
        // `shot` is still enabled and the capture-then-exit path runs (otherwise we'd fall
        // through to interactive GUI mode and hang headlessly, never writing a PNG).
        static std::string defShot = exeDir() + "/ui_screenshot.png";
        const int rc = run_gui(argc >= 3 ? argv[2] : defShot.c_str());
        // Chromium threads outlive CefShutdown() on this build and hang static/AtExit
        // destruction AFTER run_gui's teardown already completed (chat persisted, workers
        // stopped, shot written) — the gate's shot smoke then never exits. Exit directly,
        // skipping static dtors; the interactive exit path keeps the normal return (the
        // underlying CEF exit hang is tracked as T-RF23).
        std::fflush(stdout);
        std::fflush(stderr);
        std::_Exit(rc);
    }
    // "Open with" / file association (task 05): a bare path argument — the verb the OS invokes when
    // a user double-clicks a .step+ (or any registered CAD/ECAD file) — launches the GUI and opens
    // that document through the normal deferred open pump (NOT a headless mode). Anything starting
    // with '-' is a flag handled above; a non-flag arg that names an existing file is the doc to open.
    // T-RF23: ALL native exits go through the same hard-exit tail as --ui-screenshot — Chromium
    // threads outlive CefShutdown() on this build and hang static/AtExit destruction on the
    // interactive close path too. run_gui's teardown (chat persisted, workers stopped, docs
    // autosaved) has fully completed by the time it returns, so skipping static dtors is safe.
    auto hard_exit = [](int rc) -> int {
        std::fflush(stdout);
        std::fflush(stderr);
        std::_Exit(rc);
        return rc;  // unreachable; satisfies the compiler
    };
    if (argc >= 2 && argv[1] && argv[1][0] != '-') {
        std::error_code ec;
        const char* openArg = std::filesystem::exists(argv[1], ec) ? argv[1] : nullptr;
        return hard_exit(run_gui(nullptr, openArg));
    }
    // GUI opens immediately and provisions the runtime in-app (with progress) if needed.
#ifdef __EMSCRIPTEN__
    std::fprintf(stderr, "[wasm-boot] calling run_gui\n");
    std::fflush(stderr);
    EM_ASM({
        var el = document.getElementById('status');
        if (el) el.textContent = 'calling run_gui';
        if (!window.__billetLogs) window.__billetLogs = [];
        window.__billetLogs.push('boot: calling run_gui');
        console.log('boot: calling run_gui');
    });
#endif
#ifdef __EMSCRIPTEN__
    return run_gui(nullptr);  // wasm: never hard-exit — the VM owns the lifecycle
#else
    return hard_exit(run_gui(nullptr));
#endif
#endif  // !__EMSCRIPTEN__

}
