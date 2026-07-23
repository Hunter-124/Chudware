// EMSCRIPTEN-only boot-to-triangle path for the web-native client wave.
// Full app.cpp / renderer / IPC ports land in later waves; this TU proves the
// Emscripten toolchain + ImGui/WebGL2 pipeline boots far enough to paint UI.
//
// Native desktop never compiles this file (see host/CMakeLists.txt EMSCRIPTEN
// target_sources).
//
// Link mode: -pthread -sPROXY_TO_PTHREAD → main() runs on a worker. DOM access
// MUST go through MAIN_THREAD_EM_ASM / transferred OffscreenCanvas (see
// OFFSCREENCANVAS_SUPPORT + OFFSCREEN_FRAMEBUFFER link flags).
#ifdef __EMSCRIPTEN__

#include <emscripten.h>
#include <emscripten/html5.h>

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>
#include <GLFW/emscripten_glfw3.h>

#include <GLES3/gl3.h>

#include "imgui.h"
#include "imgui_impl_glfw.h"
#include "imgui_impl_opengl3.h"

#include "ui_theme.h"

#include <cstdio>
#include <cstring>
#include <string>

namespace {

GLFWwindow* gWin = nullptr;
ui::Fonts gFonts{};
bool gShowDemo = true;
char gStatus[160] = "WASM boot — engine disconnected (IPC next wave)";
int gFrameCount = 0;

// PROXY_TO_PTHREAD: main runs on a worker (no `document`). Hop to the browser
// main thread for any DOM write.
void setPageStatus(const char* msg) {
    MAIN_THREAD_EM_ASM({
        var el = document.getElementById('status');
        if (el) el.textContent = UTF8ToString($0);
    }, msg);
}

void glfwError(int code, const char* desc) {
    std::fprintf(stderr, "[wasm] glfw error %d: %s\n", code, desc ? desc : "?");
    char buf[192];
    std::snprintf(buf, sizeof(buf), "GLFW error %d: %s", code, desc ? desc : "?");
    setPageStatus(buf);
}

void frame() {
    if (!gWin) return;
    if (glfwWindowShouldClose(gWin)) {
        emscripten_cancel_main_loop();
        return;
    }

    glfwPollEvents();

    int fbw = 0, fbh = 0;
    glfwGetFramebufferSize(gWin, &fbw, &fbh);
    if (fbw < 1) fbw = 1;
    if (fbh < 1) fbh = 1;
    glViewport(0, 0, fbw, fbh);
    glClearColor(0.07f, 0.10f, 0.12f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);

    ImGui_ImplOpenGL3_NewFrame();
    ImGui_ImplGlfw_NewFrame();
    ImGui::NewFrame();

    ImGui::SetNextWindowPos(ImVec2(24.0f, 24.0f), ImGuiCond_FirstUseEver);
    ImGui::SetNextWindowSize(ImVec2(420.0f, 180.0f), ImGuiCond_FirstUseEver);
    if (ImGui::Begin("Chudware (WASM)")) {
        ImGui::TextUnformatted("Chudware web client — toolchain bring-up");
        ImGui::Separator();
        ImGui::TextWrapped("%s", gStatus);
        ImGui::Spacing();
        const char* ver = (const char*)glGetString(GL_VERSION);
        ImGui::Text("GL: %s", ver ? ver : "(null)");
        ImGui::Text("FB: %dx%d  FPS: %.1f", fbw, fbh, ImGui::GetIO().Framerate);
        ImGui::Checkbox("Show ImGui demo", &gShowDemo);
    }
    ImGui::End();

    if (gShowDemo) ImGui::ShowDemoWindow(&gShowDemo);

    ImGui::Render();
    ImGui_ImplOpenGL3_RenderDrawData(ImGui::GetDrawData());
    glfwSwapBuffers(gWin);

    if (++gFrameCount == 1) {
        char buf[128];
        std::snprintf(buf, sizeof(buf), "ImGui frame 1 — FB %dx%d", fbw, fbh);
        setPageStatus(buf);
        std::fprintf(stderr, "[wasm] %s\n", buf);
    }
}

bool initGlfw() {
    glfwSetErrorCallback(glfwError);
    if (!glfwInit()) {
        std::fprintf(stderr, "[wasm] glfwInit failed\n");
        setPageStatus("glfwInit failed");
        return false;
    }

    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 0);
    glfwWindowHint(GLFW_CLIENT_API, GLFW_OPENGL_ES_API);

    emscripten_glfw_set_next_window_canvas_selector("#canvas");

    gWin = glfwCreateWindow(1280, 800, "Chudware", nullptr, nullptr);
    if (!gWin) {
        std::fprintf(stderr, "[wasm] glfwCreateWindow failed\n");
        setPageStatus("glfwCreateWindow failed");
        glfwTerminate();
        return false;
    }

    // Best-effort: fill the browser window. Safe no-op if the canvas was
    // transferred as OffscreenCanvas (resize is driven from the main thread).
    emscripten_glfw_make_canvas_resizable(gWin, "window", nullptr);

    glfwMakeContextCurrent(gWin);
    glfwSwapInterval(1);

    const char* ver = (const char*)glGetString(GL_VERSION);
    const char* ren = (const char*)glGetString(GL_RENDERER);
    std::fprintf(stderr, "[wasm] GL_VERSION=%s\n", ver ? ver : "(null)");
    std::fprintf(stderr, "[wasm] GL_RENDERER=%s\n", ren ? ren : "(null)");
    char buf[160];
    std::snprintf(buf, sizeof(buf), "GL ready: %s", ver ? ver : "?");
    setPageStatus(buf);
    return true;
}

bool initImGui() {
    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
    io.IniFilename = nullptr;

    const std::string assets = "/assets";
    ui::applyTheme(ui::Theme::Dark, 1.0f);
    gFonts = ui::loadFonts(assets, 1.0f);

    ImGui_ImplGlfw_InitForOpenGL(gWin, true);
    // WebGL2 expects GLSL ES 300; desktop #version 330 core is next-wave shim work.
    if (!ImGui_ImplOpenGL3_Init("#version 300 es")) {
        std::fprintf(stderr, "[wasm] ImGui_ImplOpenGL3_Init failed\n");
        setPageStatus("ImGui_ImplOpenGL3_Init failed");
        return false;
    }
    return true;
}

}  // namespace

int main(int /*argc*/, char** /*argv*/) {
    std::fprintf(stderr, "[wasm] Chudware WASM boot starting (PROXY_TO_PTHREAD main)\n");
    setPageStatus("main() entered");
    if (!initGlfw()) return 1;
    if (!initImGui()) return 2;

    setPageStatus("entering main loop");
    emscripten_set_main_loop(frame, 0, /*simulate_infinite_loop=*/1);
    return 0;
}

#endif  // __EMSCRIPTEN__
