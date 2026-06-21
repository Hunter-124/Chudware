// AI-CAD native host.
//
// Modes:
//   AI-CAD --render  <in.stl> <out.png> [view]   single offscreen view (iso/front/right/top)
//   AI-CAD --contact <in.stl> <out.png>          2x2 contact sheet
//   AI-CAD [--gui]                               interactive desktop app (ImGui)
//
// The offscreen modes need only a hidden GL context, so they verify the whole render
// path headlessly. GUI mode is in gui.cpp.
#include <glad/gl.h>
//
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>
#include <stb_image_write.h>

#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "bootstrap.h"
#include "mesh.h"
#include "renderer.h"
#include "worker_client.h"

#ifdef _WIN32
#include <windows.h>
#include <dbghelp.h>
#include <ctime>
#endif

int run_gui(const char* screenshotPath);  // gui_app.cpp

namespace {

#ifdef _WIN32
// ---- crash diagnostics ---------------------------------------------------------------------
// On any unhandled SEH fault (access violation, etc.) write a symbolized call stack + a minidump
// next to the exe (and append the stack to $AI_CAD_LOG_FILE when set) so an "instant hard crash"
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
    std::string stackPath = base + "\\AI-CAD_crash_" + stamp + ".txt";
    std::string dmpPath   = base + "\\AI-CAD_crash_" + stamp + ".dmp";

    std::ofstream out(stackPath, std::ios::binary);
    auto emit = [&](const std::string& line) {
        if (out) out << line << "\n";
        std::fprintf(stderr, "%s\n", line.c_str());
    };
    char hdr[128];
    std::snprintf(hdr, sizeof(hdr), "AI-CAD CRASH  code=0x%08lX  addr=%p",
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
    if (const char* lf = std::getenv("AI_CAD_LOG_FILE")) {
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
    GLFWwindow* win = glfwCreateWindow(w, h, "AI-CAD", nullptr, nullptr);
    if (!win) {
        std::fprintf(stderr, "window/context creation failed\n");
        glfwTerminate();
        return nullptr;
    }
    glfwMakeContextCurrent(win);
    if (!gladLoadGL((GLADloadfunc)glfwGetProcAddress)) {
        std::fprintf(stderr, "glad load failed\n");
        return nullptr;
    }
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

// Drive the Python worker: build a solid (from a .py file / inline code) or run the full
// AI design loop, then render the resulting STL to a contact sheet — proving the whole
// native pipeline: host -> worker -> build123d -> STL -> GL render -> PNG.
int buildAndRender(const std::string& src, const std::string& out, bool isDesign) {
    std::string code = src;
    if (!isDesign) {
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
    bool ok;
    if (isDesign)
        ok = w.call("agent_design", {{"request", src}, {"name", "host_design"}}, result, err,
                    900000);
    else
        ok = w.call("run_model", {{"code", code}, {"name", "host_build"}}, result, err, 120000);
    if (!ok) {
        std::fprintf(stderr, "worker call failed: %s\n", err.c_str());
        return 7;
    }
    std::string stl;
    if (isDesign)
        stl = result.value("result", nlohmann::json::object()).value("stl_path", "");
    else
        stl = result.value("stl_path", "");
    if (stl.empty()) {
        std::fprintf(stderr, "no STL produced. worker result:\n%s\n", result.dump(2).c_str());
        return 8;
    }
    w.stop();
    return renderContact(stl, out);
}

int main(int argc, char** argv) {
    installCrashHandler();  // symbolized stack + minidump on any unhandled fault
    // Point the C++ core at its prompts on EVERY launch path (run_gui only calls
    // ensureRuntime when provisioning is needed, so it can't be relied on for this).
    ensurePromptsDir();
    if (argc >= 4 && std::strcmp(argv[1], "--render") == 0)
        return renderSingle(argv[2], argv[3], argc >= 5 ? argv[4] : "iso");
    if (argc >= 4 && std::strcmp(argv[1], "--contact") == 0)
        return renderContact(argv[2], argv[3]);
    if (argc >= 4 && std::strcmp(argv[1], "--build") == 0)
        return buildAndRender(argv[2], argv[3], false);
    if (argc >= 4 && std::strcmp(argv[1], "--design") == 0)
        return buildAndRender(argv[2], argv[3], true);
    if (argc >= 2 && std::strcmp(argv[1], "--ensure-runtime") == 0) {
        std::string log;
        bool ok = ensureRuntime(log);
        std::printf("%s\n", log.c_str());
        return ok ? 0 : 1;
    }
    if (argc >= 2 && std::strcmp(argv[1], "--ui-screenshot") == 0) {
        // Headless UI capture. An explicit output path is optional; when omitted (the common
        // smoke-test invocation `AI-CAD --ui-screenshot`), default to a PNG next to the exe so
        // `shot` is still enabled and the capture-then-exit path runs (otherwise we'd fall
        // through to interactive GUI mode and hang headlessly, never writing a PNG).
        static std::string defShot = exeDir() + "\\ui_screenshot.png";
        return run_gui(argc >= 3 ? argv[2] : defShot.c_str());
    }
    // GUI opens immediately and provisions the runtime in-app (with progress) if needed.
    return run_gui(nullptr);
}
