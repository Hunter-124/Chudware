// Chudware native host.
//
// Modes:
//   Chudware --render  <in.stl> <out.png> [view]   single offscreen view (iso/front/right/top)
//   Chudware --contact <in.stl> <out.png>          2x2 contact sheet
//   Chudware [--gui]                               interactive desktop app (ImGui)
//   Chudware --version                             print the product version and exit
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

#include <atomic>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <system_error>
#include <vector>
#ifndef _WIN32
#include <unistd.h>  // environ (glibc declares it at global scope)
#endif

#include "api/chudware_version.h"  // CHUDWARE_VERSION_LINE (generated, <build>/generated)
#include "bootstrap.h"
#include "core/app_state.h"  // appstateInitPersist (WASM IDBFS)
#include "core/startup_trace.h"
#include "ipc_discovery.h"   // chudware_ipc::SingleInstance/instanceKey/guiLockPath (services/ipc)
#include "mesh.h"
#include "renderer.h"
#include "worker_client.h"
#include "util/platform_paths.h"  // executablePath (one spelling per OS)
#include "update/updater.h"
#include "security/integrity.h"
#include "update/windows_update_helper.h"

#ifdef CHUDWARE_ENABLE_CEF
#include "ui/widgets/cef/cef_app.h"
#include "ui/widgets/cef/cef_osr_shot.h"
#include "ui/widgets/cef/cef_headless.h"
#include "ui/widgets/cef/cef_daemon.h"
#endif

#ifdef _WIN32
#include <windows.h>
#include "ipc_windows_local.h"
#include <dbghelp.h>
#include <ctime>
#endif

#ifndef _WIN32
#include <fcntl.h>   // single-instance guard: lock-file pid read
#include <unistd.h>
#endif

int run_gui(const char* screenshotPath, const char* openPath = nullptr);  // gui_app.cpp

namespace {

#ifdef _WIN32
// ---- crash diagnostics ---------------------------------------------------------------------
// On any unhandled SEH fault (access violation, etc.) write a symbolized call stack + a minidump
// next to the exe (and append the stack to $CHUDWARE_LOG_FILE when set) so an "instant hard crash"
// reported in the field gives us the exact faulting frame instead of a silent window-close.
LONG WINAPI chudwareCrashFilter(EXCEPTION_POINTERS* ep) {
    char dir[MAX_PATH] = {0};
    GetModuleFileNameA(nullptr, dir, MAX_PATH);
    std::string base(dir);
    size_t slash = base.find_last_of("\\/");
    base = (slash == std::string::npos) ? std::string(".") : base.substr(0, slash);

    char stamp[32];
    std::time_t now = std::time(nullptr);
    std::strftime(stamp, sizeof(stamp), "%Y%m%d_%H%M%S", std::localtime(&now));
    std::string stackPath = base + "\\Chudware_crash_" + stamp + ".txt";
    std::string dmpPath   = base + "\\Chudware_crash_" + stamp + ".dmp";

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

void installCrashHandler() { SetUnhandledExceptionFilter(chudwareCrashFilter); }
#else
void installCrashHandler() {}
#endif




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
// build123d -> STL -> GL render -> PNG. (The --design mode was removed along with the
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

// ---- single-instance guard (desktop GUI) -----------------------------------------------------
// One install, one window. This reuses the daemon's own primitive — chudware_ipc::SingleInstance
// (flock on POSIX, owner-private file handle on Windows) — keyed on THIS binary's path, exactly the way
// chudware-hostd guards itself (core/hostd/main.cpp). Keying on the binary keeps a dev tree and
// an installed build independent singletons, matching how they already get separate daemons.
//
// Losing the race is not an error: the user double-clicked expecting a window, so poke the
// incumbent to surface itself and exit 0. The poke is one bit — SIGUSR1 on POSIX (addressed to the
// pid SingleInstance::acquire already records in the lock file), an auto-reset named event on
// Windows — consumed by the frame loop in app.cpp. The handler only sets an atomic; every GLFW
// call stays on the render thread.
//
// The guard FAILS OPEN by design. A lock we could not even establish (unwritable runtime dir,
// denied kernel namespace) must never be the reason the app refuses to start, so we stand down
// only after confirming a live incumbent really holds it.
//
// AUTOMATION EXEMPTIONS — load-bearing. scripts/gate.sh stage 5, scripts/icon_pipeline.py and the
// CHUDWARE_SHOT=* scenarios launch this binary headlessly, sometimes back-to-back or concurrently;
// a blanket guard would fail the build gate. The guard is skipped when:
//   * "--ui-screenshot" appears anywhere in argv (the headless capture-then-exit mode), or
//   * $CHUDWARE_SHOT is set (a scripted scenario), or
//   * $CHUDWARE_ALLOW_MULTI is set to anything but 0 (explicit power-user escape hatch).
#ifndef __EMSCRIPTEN__
namespace {

#ifdef _WIN32
HANDLE g_activateEvent = nullptr;
#else
std::atomic<bool> g_activateRequested{false};

void onActivateSignal(int) { g_activateRequested.store(true, std::memory_order_relaxed); }

// The pid SingleInstance::acquire writes into the lock file — same convention, and the same
// liveness re-check, as the daemon's readLockPid. 0 = unreadable or not a live process.
long readGuiLockPid(const std::string& lockFile) {
    const int fd = ::open(lockFile.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    char buf[64] = {0};
    const ssize_t n = ::read(fd, buf, sizeof buf - 1);
    ::close(fd);
    if (n <= 0) return 0;
    char* end = nullptr;
    const long pid = std::strtol(buf, &end, 10);
    if (pid <= 1 || end == buf) return 0;
    return ::kill(static_cast<pid_t>(pid), 0) == 0 ? pid : 0;
}
#endif

std::string selfExePath() { return chudware::platform::executablePath(); }

bool automationLaunch(int argc, char** argv) {
    for (int i = 1; i < argc; ++i)
        if (argv[i] && std::strcmp(argv[i], "--ui-screenshot") == 0) return true;
    if (const char* s = std::getenv("CHUDWARE_SHOT"); s && *s) return true;
    const char* multi = std::getenv("CHUDWARE_ALLOW_MULTI");
    return multi && multi[0] && multi[0] != '0';
}

// The document a bare (non-flag) argv[1] names, when it exists on disk — the "Open with" verb the
// OS invokes on a double-clicked .step+/.ecad+. nullptr = plain launch, or an argument naming
// nothing. Shared by the guard's hand-off below and the file-association path in main().
const char* documentArg(int argc, char** argv) {
    if (argc < 2 || !argv[1] || argv[1][0] == '-') return nullptr;
    std::error_code ec;
    return std::filesystem::exists(argv[1], ec) ? argv[1] : nullptr;
}

// Sibling of the lock file, in the same runtime dir. "" when there is no usable runtime dir.
std::string g_activationDoc;

// False = another GUI already owns this install; it has been asked to raise itself and this
// process must exit 0 without touching CEF, GLFW or the daemon.
bool claimSingleInstance(int argc, char** argv) {
    if (automationLaunch(argc, argv)) return true;

    const std::string key = chudware_ipc::instanceKey(selfExePath());
    const std::string lock = chudware_ipc::guiLockPath(key);
    if (lock.empty()) return true;  // no usable runtime dir — fail open
    // "Open with" hand-off note: the app opens documents from argv, so a second launch carrying
    // one must not have it silently swallowed by the guard. Not a queue and not a protocol —
    // one pending doc, last writer wins, unlinked the moment it is read.
    if (const std::string dir = chudware_ipc::runtimeDir(); !dir.empty())
        g_activationDoc = dir + "/gui-" + key + ".open";


    // Process-lifetime claim. Every GUI exit path ends in std::_Exit, so this is never released
    // explicitly — which is the point: the live lock handle dies with the process, so a crashed
    // instance leaves nothing to clean up and the next launch simply wins the lock.
    static chudware_ipc::SingleInstance instance;
    std::string err;
    if (instance.acquire(lock, err)) {
#ifdef _WIN32
        // Wake is availability-only. A squatted/foreign event cannot defeat the
        // private file lock, and must not receive document handoff or activation.
        const std::string name = "Local\\chudware-gui-activate-" + chudware_ipc::fnv1aHex(lock);
        g_activateEvent = chudware_ipc::privateActivationEvent(
            std::wstring(name.begin(), name.end()), true);
        g_activationDoc = lock + ".open";
#endif
        // A fresh incumbent owns no hand-off: discard anything a previous, dead instance left.
        if (!g_activationDoc.empty()) {
            std::error_code ec;
            std::filesystem::remove(g_activationDoc, ec);
        }
#ifndef _WIN32
        // sigaction, not signal(): SA_RESTART. SIGUSR1 lands on an arbitrary thread, and the
        // worker/IPC threads sit in blocking reads that must not come back EINTR.
        struct sigaction sa {};
        sa.sa_handler = onActivateSignal;
        sigemptyset(&sa.sa_mask);
        sa.sa_flags = SA_RESTART;
        ::sigaction(SIGUSR1, &sa, nullptr);
#endif
        return true;
    }

    // Confirm a LIVE incumbent before standing down — see "fails open" above.
#ifdef _WIN32
    const bool incumbent = chudware_ipc::privateLockHeld(lock);
#else
    const long pid = readGuiLockPid(lock);
    const bool incumbent = pid > 1;
#endif
    if (!incumbent) {
        std::fprintf(stderr,
                     "Chudware: single-instance guard unavailable (%s) — starting anyway\n",
                     err.c_str());
        return true;
    }
#ifdef _WIN32
    const std::string eventName = "Local\\chudware-gui-activate-" + chudware_ipc::fnv1aHex(lock);
    // The file claim precedes event creation. Retry the availability channel
    // briefly without ever turning event existence into authority.
    for (int attempt = 0; attempt < 20 && !g_activateEvent; ++attempt) {
        g_activateEvent = chudware_ipc::privateActivationEvent(
            std::wstring(eventName.begin(), eventName.end()), false);
        if (!g_activateEvent) Sleep(10);
    }
    g_activationDoc = g_activateEvent ? lock + ".open" : std::string();
#endif

    // Hand the document over BEFORE the poke, so the incumbent's activation branch always sees
    // both in the same frame. Absolute: the incumbent's cwd is not ours.
    if (const char* doc = documentArg(argc, argv); doc && !g_activationDoc.empty()) {
        std::error_code ec;
#ifdef _WIN32
        chudware_ipc::privateWrite(g_activationDoc, std::filesystem::absolute(doc, ec).u8string() + "\n");
#else
        std::filesystem::create_directories(
            std::filesystem::path(g_activationDoc).parent_path(), ec);
        std::ofstream f(g_activationDoc, std::ios::binary | std::ios::trunc);
        if (f) f << std::filesystem::absolute(doc, ec).string() << "\n";
#endif
    }

#ifdef _WIN32
    if (g_activateEvent) {
        // We are the foreground process (the user just launched us). Hand that right over, or the
        // incumbent's SetForegroundWindow is vetoed by the foreground lock and only flashes.
        AllowSetForegroundWindow(ASFW_ANY);
        SetEvent(g_activateEvent);
    }
#else
    ::kill(static_cast<pid_t>(pid), SIGUSR1);
#endif
    std::fprintf(stderr, "Chudware: already running — raising the existing window\n");
    return false;
}

}  // namespace

// app.cpp frame loop: true exactly once per activation poke from a second launch.
bool guiConsumeActivationRequest() {
#ifdef _WIN32
    return g_activateEvent && WaitForSingleObject(g_activateEvent, 0) == WAIT_OBJECT_0;
#else
    return g_activateRequested.exchange(false, std::memory_order_relaxed);
#endif
}

// app.cpp frame loop: the document a second launch handed over with its poke ("Open with" on an
// already-running app), or "" when there is none. Read-and-forget.
std::string guiTakeActivationDoc() {
    if (g_activationDoc.empty()) return "";
    std::string path;
    {
        std::ifstream f(g_activationDoc, std::ios::binary);
        std::getline(f, path);
    }
    std::error_code ec;
    std::filesystem::remove(g_activationDoc, ec);
    return path;
}
#endif  // !__EMSCRIPTEN__

int main(int argc, char** argv) {
    startup_trace::mark("main entry");
#ifdef __EMSCRIPTEN__
    // Web client: skip desktop CLI modes entirely. Report progress via Module.setStatus
    // (shell.html) which is reliable from the browser main thread.
    EM_ASM({
        try {
            if (Module.setStatus) Module.setStatus('main enter');
            if (!window.__chudwareLogs) window.__chudwareLogs = [];
            window.__chudwareLogs.push('boot: main enter');
            console.log('boot: main enter');
        } catch (e) { console.error(e); }
    });
    std::fprintf(stderr, "[wasm-boot] main enter\n");
    std::fflush(stderr);
    // Mount IDBFS at /persist and populate from IndexedDB BEFORE any appdata
    // read (loadSettings / chat_history). Blocks via ASYNCIFY until ready.
    appstateInitPersist();
    ensurePromptsDir();
    EM_ASM({
        try {
            if (Module.setStatus) Module.setStatus('calling run_gui');
            if (!window.__chudwareLogs) window.__chudwareLogs = [];
            window.__chudwareLogs.push('boot: calling run_gui');
            console.log('boot: calling run_gui');
        } catch (e) { console.error(e); }
    });
    const int rc = run_gui(nullptr);
    EM_ASM({
        try {
            if (Module.setStatus) Module.setStatus('run_gui returned ' + $0);
            if (!window.__chudwareLogs) window.__chudwareLogs = [];
            window.__chudwareLogs.push('boot: run_gui returned ' + $0);
        } catch (e) {}
    }, rc);
    return rc;
#else
    const std::filesystem::path executable = chudware::platform::executablePath();
    if (argc == 4 && std::strcmp(argv[1], "--verify-payload-manifest") == 0) {
        const auto root = chudware::security::installedPayloadRoot(executable);
        const auto verified = chudware::security::verifyPayloadManifest(
            root, argv[2], argv[3], chudware::security::VerifyMode::Full, executable);
        // Every early exit below takes the hard-exit tail `--version` documents: a
        // namespace-scope static spawns a DETACHED worker parked on a condition
        // variable, so `return` deadlocks in global destruction. Measured: a
        // production binary whose manifest is missing printed this refusal and then
        // hung forever instead of exiting.
        if (!verified.ok) {
            std::fprintf(stderr, "chudware: payload integrity failed (%s): %s\n",
                         chudware::security::integrityReasonName(verified.reason),
                         verified.detail.c_str());
            std::fflush(nullptr);
            std::_Exit(chudware::security::kIntegrityFailureExitCode);
        }
        std::fflush(nullptr);
        std::_Exit(0);
    }
    // `chudware --version` — first thing checked, before CEF stashes argv and long
    // before GLFW/X11 or the single-instance lock. Support triage and the installer
    // must be able to read the version out of a binary that cannot start a GUI.
    //
    // It also precedes the payload-integrity gate below, deliberately: this prints a
    // compile-time constant and loads nothing from the signed payload, so hashing
    // ~800 MB of it first only made a version query take ~2.7 s. The gate still runs
    // unconditionally for every path that actually starts the app.
    //
    // OUTPUT FORMAT IS A CONTRACT: exactly ONE line on stdout,
    // "chudware <semver> (<sha>, <date>)", nothing before or after it. Packaging
    // and release tooling reads the semver out of a built binary, and a parser
    // written against `awk '{print $2}'` or a last-token split is broken by any
    // extra line. chudware-hostd learned this the hard way — its `--version` has
    // TWO independent parsers (services/ipc/ipc_client.cpp diskBuildId() reads the
    // last token of the FIRST line, services/mcp/daemon_bridge.py _disk_build_id()
    // the last token of the WHOLE output) that no two-line form can satisfy. Put
    // anything new on its own flag, never on this one.
    for (int i = 1; i < argc; ++i) {
        if (argv[i] && std::strcmp(argv[i], "--version") == 0) {
            std::printf("chudware %s\n", CHUDWARE_VERSION_LINE);
            // Same hard-exit tail every other native exit uses (hard_exit()
            // below), and for the same class of reason: a namespace-scope static in
            // ui/copilot/composer.cpp (g_agentCtl) spawns a DETACHED worker parked
            // forever on a condition_variable, so global destruction deadlocks in
            // pthread_cond_destroy. `return 0` here printed nothing and hung —
            // eu-stack: main thread in condition_variable::~condition_variable
            // under __run_exit_handlers, worker in cv.wait. This path has opened no
            // file, taken no lock and touched no engine: nothing to unwind.
            std::fflush(nullptr);
            std::_Exit(0);
        }
    }
#ifdef _WIN32
    {
        std::string recoveryError;
        if (!chudware::update::recoverPendingWindowsUpdate(executable, &recoveryError)) {
            std::fprintf(stderr, "chudware: update recovery failed: %s\n", recoveryError.c_str());
            std::fflush(nullptr);
            std::_Exit(79);
        }
    }
#endif
    {
        const auto verified = chudware::security::verifyInstalledPayload(
            executable, chudware::security::VerifyMode::Startup);
        if (!verified.ok) {
            std::fprintf(stderr, "chudware: installed payload integrity failed (%s): %s\n",
                         chudware::security::integrityReasonName(verified.reason),
                         verified.detail.c_str());
            std::fflush(nullptr);
            std::_Exit(chudware::security::kIntegrityFailureExitCode);
        }
    }
#ifdef CHUDWARE_ENABLE_CEF
    // A separate subprocess executable (chudware_cef_helper) is configured via
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
#else
    // NO CEF in this build. These argv forms must still be CLAIMED and fail fast: without
    // this they fell through to the normal GUI launch, so `chudware --cef-daemon <sock> 120`
    // started a whole desktop app that never listened on the socket and never exited. The
    // Python client (services/browser/cef_client.py) only checks that the binary exists, so
    // every later request spawned another one — 193 were found alive at once, 34 h old.
    if (argc >= 2 && std::strncmp(argv[1], "--cef-", 6) == 0) {
        std::fprintf(stderr,
                     "%s: this build has no CEF backend (CHUDWARE_ENABLE_CEF=OFF); "
                     "rebuild with -DCHUDWARE_ENABLE_CEF=ON to use the headless browser\n",
                     argv[1]);
        // Same hard-exit tail as --version above, and for the same reason: a
        // namespace-scope static spawns a detached worker parked on a condition_variable,
        // so `return` deadlocks in global destruction. Verified: `return 2` printed the
        // message and then hung forever — which is the very failure mode this branch exists
        // to prevent.
        std::fflush(nullptr);
        std::_Exit(2);
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
    // One window per install. Deliberately before CEF init below, so a duplicate launch exits
    // without spinning Chromium up (and then having to tear it down); every headless CLI mode
    // above has already returned, and --ui-screenshot is exempted inside the guard itself.
    if (!claimSingleInstance(argc, argv)) return 0;
    const bool updateShot =
        argc >= 2 && std::strcmp(argv[1], "--ui-screenshot") == 0;
    // stop() on a never-started updater is a no-op (nothing to join), so the exit path below
    // needs no matching condition.
    auto& desktopUpdater = chudware::update::globalUpdater();
    // A build that did not come from a release point reports version 0.0.0 with
    // CHUDWARE_BUILD_DIRTY=1 by construction (core/cmake/ChudwareVersion.cmake), and
    // "nothing downstream may treat it as one" is that file's stated invariant. The updater
    // broke it in both directions: 0.0.0 is below every published min_supported, so the
    // retirement gate pinned a full-screen "mandatory update" modal over every source build
    // with no way out, and the same check would then download a release AppImage and stage it
    // over the developer tree. Release artifacts are stamped by the pipeline with dirty=0, so
    // the gate still retires them exactly as before.
    constexpr bool kReleaseIdentity = CHUDWARE_BUILD_DIRTY == 0;
    if (!updateShot && kReleaseIdentity) {
        desktopUpdater.configure(CHUDWARE_VERSION, appdataAppDir(),
                                 chudware::platform::executablePath());
        const auto apply = desktopUpdater.applyStagedBeforeWindow();
        if (apply == chudware::update::ApplyResult::Relaunching) {
            // Windows/macOS handoff has started. Linux succeeds by execv and
            // never reaches here. No GUI subsystem has started yet.
            std::fflush(nullptr);
            std::_Exit(0);
        }
        desktopUpdater.start();
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
        // underlying CEF exit hang is a known open issue).
        std::fflush(stdout);
        std::fflush(stderr);
        std::_Exit(rc);
    }
    // "Open with" / file association (task 05): a bare path argument — the verb the OS invokes when
    // a user double-clicks a .step+ (or any registered CAD/ECAD file) — launches the GUI and opens
    // that document through the normal deferred open pump (NOT a headless mode). Anything starting
    // with '-' is a flag handled above; a non-flag arg that names an existing file is the doc to open.
    // ALL native exits go through the same hard-exit tail as --ui-screenshot — Chromium
    // threads outlive CefShutdown() on this build and hang static/AtExit destruction on the
    // interactive close path too. run_gui's teardown (chat persisted, workers stopped, docs
    // autosaved) has fully completed by the time it returns, so skipping static dtors is safe.
    auto hard_exit = [](int rc) -> int {
        std::fflush(stdout);
        std::fflush(stderr);
        std::_Exit(rc);
        return rc;  // unreachable; satisfies the compiler
    };
    auto runGuiWithUpdater = [&](const char* openPath) {
        const int rc = run_gui(nullptr, openPath);
        desktopUpdater.stop();  // joins the poll/download worker before hard_exit
        return rc;
    };
    if (argc >= 2 && argv[1] && argv[1][0] != '-')
        return hard_exit(runGuiWithUpdater(documentArg(argc, argv)));
    // GUI opens immediately and provisions the runtime in-app (with progress) if needed.
#ifdef __EMSCRIPTEN__
    std::fprintf(stderr, "[wasm-boot] calling run_gui\n");
    std::fflush(stderr);
    EM_ASM({
        var el = document.getElementById('status');
        if (el) el.textContent = 'calling run_gui';
        if (!window.__chudwareLogs) window.__chudwareLogs = [];
        window.__chudwareLogs.push('boot: calling run_gui');
        console.log('boot: calling run_gui');
    });
#endif
#ifdef __EMSCRIPTEN__
    return run_gui(nullptr);  // wasm: never hard-exit — the VM owns the lifecycle
#else
    return hard_exit(runGuiWithUpdater(nullptr));
#endif
#endif  // !__EMSCRIPTEN__

}
