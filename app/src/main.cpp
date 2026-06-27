// AI-CAD (new GUI) entry point. GLFW + glad + Dear ImGui (docking) bootstrap.
#define GLFW_INCLUDE_NONE
#include <glad/gl.h>
#include <GLFW/glfw3.h>

#include "imgui.h"
#include "backends/imgui_impl_glfw.h"
#include "backends/imgui_impl_opengl3.h"

#include "app/application.h"
#include "app/app_state.h"
#include "theme/theme.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include "stb_image_write.h"

#ifdef _WIN32
#  include <windows.h>
static std::string exeDir() {
    char buf[MAX_PATH]; DWORD n = GetModuleFileNameA(nullptr, buf, MAX_PATH);
    std::string p(buf, n);
    size_t slash = p.find_last_of("\\/");
    return slash == std::string::npos ? "." : p.substr(0, slash);
}
#else
static std::string exeDir() { return "."; }
#endif

static void glfwErr(int code, const char* desc) { std::fprintf(stderr, "[glfw] %d: %s\n", code, desc); }

static void capturePNG(GLFWwindow* win, const char* path) {
    int w, h; glfwGetFramebufferSize(win, &w, &h);
    std::vector<unsigned char> px((size_t)w * h * 4);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadBuffer(GL_BACK);
    glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
    stbi_flip_vertically_on_write(1);
    if (stbi_write_png(path, w, h, 4, px.data(), w*4)) std::fprintf(stderr, "[shot] wrote %s (%dx%d)\n", path, w, h);
    else std::fprintf(stderr, "[shot] FAILED to write %s\n", path);
}

int main(int argc, char** argv) {
    // headless screenshot mode: --shot <path> [--frames N]
    const char* shotPath = nullptr; int shotFrames = 12; bool demo = false;
    const char* doCmd = nullptr; bool openPalette = false; bool sketch = false;
    for (int i = 1; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--shot") && i+1 < argc) shotPath = argv[++i];
        else if (!std::strcmp(argv[i], "--frames") && i+1 < argc) shotFrames = atoi(argv[++i]);
        else if (!std::strcmp(argv[i], "--demo")) demo = true;
        else if (!std::strcmp(argv[i], "--do") && i+1 < argc) doCmd = argv[++i];
        else if (!std::strcmp(argv[i], "--palette")) openPalette = true;
        else if (!std::strcmp(argv[i], "--sketch")) sketch = true;
    }

    glfwSetErrorCallback(glfwErr);
    if (!glfwInit()) { std::fprintf(stderr, "glfwInit failed\n"); return 1; }

    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
    glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
#ifdef __APPLE__
    glfwWindowHint(GLFW_OPENGL_FORWARD_COMPAT, GL_TRUE);
#endif
    glfwWindowHint(GLFW_DECORATED, GLFW_FALSE);   // frameless: custom chrome (DESIGN.md §2)
    glfwWindowHint(GLFW_RESIZABLE, GLFW_TRUE);
    if (shotPath) { glfwWindowHint(GLFW_VISIBLE, GLFW_FALSE); glfwWindowHint(GLFW_SCALE_TO_MONITOR, GLFW_FALSE); }
    else            glfwWindowHint(GLFW_SCALE_TO_MONITOR, GLFW_TRUE);

    int winW = shotPath ? 1600 : 1480, winH = shotPath ? 1000 : 920;
    GLFWwindow* win = glfwCreateWindow(winW, winH, "AI-CAD", nullptr, nullptr);
    if (!win) { std::fprintf(stderr, "window create failed\n"); glfwTerminate(); return 1; }
    glfwMakeContextCurrent(win);
    glfwSwapInterval(1);

    if (!gladLoadGL((GLADloadfunc)glfwGetProcAddress)) {
        std::fprintf(stderr, "glad load failed\n"); return 1;
    }

    // DPI scale
    float xs = 1.f, ys = 1.f;
    glfwGetWindowContentScale(win, &xs, &ys);
    float scale = (xs > ys ? xs : ys);
    if (const char* e = std::getenv("AI_CAD_SCALE")) { float v = (float)atof(e); if (v > 0.4f && v < 4.f) scale = v; }
    if (scale < 0.5f) scale = 1.f;

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.ConfigFlags |= ImGuiConfigFlags_DockingEnable;
    io.ConfigWindowsMoveFromTitleBarOnly = true;
    io.IniFilename = nullptr;   // we own the layout (DockBuilder)

    const std::string res = exeDir() + "/resources";
    theme::init(res, scale, /*dark=*/true);

    ImGui_ImplGlfw_InitForOpenGL(win, true);
    ImGui_ImplOpenGL3_Init("#version 150");

    app::Application application;
    application.init(win, res, scale);
    if (demo) application.state().startDemoAgent("Design a 5V regulator board with a 3.3V output rail.");
    if (sketch) application.state().dispatch("sketch_new");
    if (doCmd)  application.state().dispatch(doCmd);
    if (openPalette) { application.state().paletteOpen = true; std::strcpy(application.state().paletteQuery, "pattern"); }

    int frame = 0;
    while (!glfwWindowShouldClose(win)) {
        glfwPollEvents();
        if (!shotPath && glfwGetWindowAttrib(win, GLFW_ICONIFIED)) { glfwWaitEventsTimeout(0.1); continue; }

        ImGui_ImplOpenGL3_NewFrame();
        ImGui_ImplGlfw_NewFrame();
        ImGui::GetIO().DeltaTime = 1.f/60.f;   // deterministic stepping (esp. for --shot)
        ImGui::NewFrame();

        application.frame();

        ImGui::Render();
        int dw, dh; glfwGetFramebufferSize(win, &dw, &dh);
        glViewport(0, 0, dw, dh);
        ImVec4 c = theme::vec(theme::pal().bg0);
        glClearColor(c.x, c.y, c.z, 1.f);
        glClear(GL_COLOR_BUFFER_BIT);
        ImGui_ImplOpenGL3_RenderDrawData(ImGui::GetDrawData());

        if (shotPath && frame >= shotFrames - 1) { capturePNG(win, shotPath); break; }
        glfwSwapBuffers(win);
        ++frame;
    }

    application.shutdown();
    ImGui_ImplOpenGL3_Shutdown();
    ImGui_ImplGlfw_Shutdown();
    ImGui::DestroyContext();
    glfwDestroyWindow(win);
    glfwTerminate();
    return 0;
}
