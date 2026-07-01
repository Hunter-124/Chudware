// AI-CAD2 entry point. GLFW + glad + Dear ImGui (docking) bootstrap.
#define GLFW_INCLUDE_NONE
#include <glad/gl.h>
#include <GLFW/glfw3.h>

#include "imgui.h"
#include "backends/imgui_impl_glfw.h"
#include "backends/imgui_impl_opengl3.h"

#include "app/application.h"
#include "app/app_state.h"
#include "viewport/scene.h"
#include "theme/theme.h"
#include "theme/icons.h"

#include <platform/dpi.h>
#include <platform/paths.h>
#include <platform/resources.h>
#include <platform/window.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include "stb_image_write.h"

#ifdef _WIN32
#  include <windows.h>
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
    const char* shotPath = nullptr; int shotFrames = 12; bool demo = false;
    const char* doCmd = nullptr; bool openPalette = false; bool sketch = false;
    bool selftest = false, picktest = false, shelltest = false, edgetest = false, extrudetest = false, apptest = false; bool light = false;
    for (int i = 1; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--shot") && i+1 < argc) shotPath = argv[++i];
        else if (!std::strcmp(argv[i], "--frames") && i+1 < argc) shotFrames = atoi(argv[++i]);
        else if (!std::strcmp(argv[i], "--demo")) demo = true;
        else if (!std::strcmp(argv[i], "--do") && i+1 < argc) doCmd = argv[++i];
        else if (!std::strcmp(argv[i], "--palette")) openPalette = true;
        else if (!std::strcmp(argv[i], "--sketch")) sketch = true;
        else if (!std::strcmp(argv[i], "--selftest")) selftest = true;
        else if (!std::strcmp(argv[i], "--picktest")) picktest = true;
        else if (!std::strcmp(argv[i], "--shelltest")) shelltest = true;
        else if (!std::strcmp(argv[i], "--edgetest")) edgetest = true;
        else if (!std::strcmp(argv[i], "--extrudetest")) extrudetest = true;
        else if (!std::strcmp(argv[i], "--apptest")) apptest = true;
        else if (!std::strcmp(argv[i], "--light")) light = true;
    }

#if defined(AICAD_DEFAULT_LIBAICAD) && defined(_WIN32)
    if (!std::getenv("AI_CAD_LIBAICAD")) {
        std::string side = platform::joinPath(platform::exeDir(), "libaicad.dll");
        if (GetFileAttributesA(side.c_str()) == INVALID_FILE_ATTRIBUTES)
            _putenv_s("AI_CAD_LIBAICAD", AICAD_DEFAULT_LIBAICAD);
    }
#endif

    glfwSetErrorCallback(glfwErr);
    if (!platform::initGlfwPlatform()) { std::fprintf(stderr, "glfwInit failed\n"); return 1; }

    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
    glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
#ifdef __APPLE__
    glfwWindowHint(GLFW_OPENGL_FORWARD_COMPAT, GL_TRUE);
#endif
    glfwWindowHint(GLFW_RESIZABLE, GLFW_TRUE);
    platform::applyBorderlessWindowHints(shotPath != nullptr);

    int winW = shotPath ? 1600 : 1480, winH = shotPath ? 1000 : 920;
    GLFWwindow* win = glfwCreateWindow(winW, winH, "AI-CAD", nullptr, nullptr);
    if (!win) { std::fprintf(stderr, "window create failed\n"); glfwTerminate(); return 1; }
    glfwMakeContextCurrent(win);
    glfwSwapInterval(1);

    if (!gladLoadGL((GLADloadfunc)glfwGetProcAddress)) {
        std::fprintf(stderr, "glad load failed\n"); return 1;
    }

    const float scale = platform::contentScale(win, shotPath != nullptr);
    const std::string res = platform::resourcesDir();
    platform::setWindowIcon(win, platform::joinPath(res, "icons/logo_icon.png"));

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.ConfigFlags |= ImGuiConfigFlags_DockingEnable;
    io.ConfigWindowsMoveFromTitleBarOnly = true;
    static std::string iniPath = platform::imguiIniPath();
    io.IniFilename = shotPath ? nullptr : iniPath.c_str();

    theme::init(res, scale, /*dark=*/!light);
    icons::initAtlas(res, /*dark=*/!light);

    ImGui_ImplGlfw_InitForOpenGL(win, true);
    ImGui_ImplOpenGL3_Init("#version 150");

    static platform::WindowChromeLayout chromeLayout;
    chromeLayout.titleBarHeight = (int)(theme::ctx().m.titleBarH * theme::S());
    chromeLayout.resizeBorder = (int)(6.f * theme::S());
    platform::installWindowChrome(win, &chromeLayout);

    app::Application application;
    application.init(win, res, scale);
    if (selftest) {
        auto& st = application.state();
        int rc = 2;
        if (!st.engineReady) { std::fprintf(stderr, "[selftest] FAIL: engine not ready (%s)\n", st.engine.lastError().c_str()); }
        else {
            std::string path = platform::joinPath(platform::tempDir(), "aicad2_selftest.step+");
            bool sv = st.engine.save(path);
            bool op = sv && st.engine.open(path);
            int nf = 0; const auto& t = st.engine.tree();
            if (t.contains("features") && t["features"].is_array()) nf = (int)t["features"].size();
            bool ok = sv && op && nf > 0;
            std::fprintf(stderr, "[selftest] save=%d open=%d features=%d rev=%d -> %s\n",
                         sv, op, nf, st.engine.revision(), ok ? "OK" : "FAIL");
            rc = ok ? 0 : 2;
        }
        application.shutdown();
        glfwDestroyWindow(win); glfwTerminate();
        return rc;
    }

    if (picktest) {
        auto& st = application.state();
        int rc = 2;
        if (!st.engineReady || !st.scene) std::fprintf(stderr, "[picktest] FAIL: no engine/scene\n");
        else {
            st.scene->resize(800, 600);
            bool loaded = st.scene->loadAicm(st.engine.aicmPath());
            st.scene->render();
            viewport::Scene::PickRef pr;
            bool hit = st.scene->pickAt(400, 300, pr);
            bool ok = loaded && hit && pr.topo_id > 0;
            std::fprintf(stderr, "[picktest] loaded=%d hit=%d body=%u face=%u -> %s\n",
                         loaded, hit, pr.body_id, pr.topo_id, ok ? "OK" : "FAIL");
            rc = ok ? 0 : 2;
        }
        application.shutdown();
        glfwDestroyWindow(win); glfwTerminate();
        return rc;
    }

    if (shelltest) {
        auto& st = application.state();
        int rc = 2;
        if (!st.engineReady || !st.scene) std::fprintf(stderr, "[shelltest] FAIL: no engine/scene\n");
        else {
            st.engine.newPart("T"); st.engine.addBox(40, 30, 20, "Base"); st.engine.rebuild();
            st.scene->resize(800, 600);
            st.scene->loadAicm(st.engine.aicmPath());
            st.scene->render();
            viewport::Scene::PickRef pr;
            bool hit = st.scene->pickAt(400, 300, pr);
            nlohmann::json refs = nlohmann::json::array();
            if (hit) { nlohmann::json sel = st.engine.resolvePick(pr.body_id, "face", pr.topo_id); if (!sel.is_null()) refs.push_back(sel); }
            eng::OpsResult r; if (!refs.empty()) r = st.engine.addShellFaces(2.0, refs);
            int nf = 0; const auto& t = st.engine.tree(); if (t.contains("features") && t["features"].is_array()) nf = (int)t["features"].size();
            bool ok = hit && !refs.empty() && r.ok && nf >= 2;
            std::fprintf(stderr, "[shelltest] hit=%d resolved=%d shellOk=%d features=%d -> %s\n",
                         hit, (int)!refs.empty(), (int)r.ok, nf, ok ? "OK" : "FAIL");
            rc = ok ? 0 : 2;
        }
        application.shutdown();
        glfwDestroyWindow(win); glfwTerminate();
        return rc;
    }

    if (edgetest) {
        auto& st = application.state();
        int rc = 2;
        if (!st.engineReady || !st.scene) std::fprintf(stderr, "[edgetest] FAIL: no engine/scene\n");
        else {
            st.engine.newPart("T"); st.engine.addBox(40, 30, 20, "Base"); st.engine.rebuild();
            st.scene->resize(800, 600);
            st.scene->loadAicm(st.engine.aicmPath());
            st.scene->render();
            st.scene->setPickKind(1);
            viewport::Scene::PickRef pr; bool hit = false;
            for (int yy = 120; yy <= 480 && !hit; yy += 30)
                for (int xx = 120; xx <= 680 && !hit; xx += 30)
                    if (st.scene->pickAt(xx, yy, pr)) hit = true;
            nlohmann::json refs = nlohmann::json::array();
            if (hit) { nlohmann::json sel = st.engine.resolvePick(pr.body_id, "edge", pr.topo_id); if (!sel.is_null()) refs.push_back(sel); }
            eng::OpsResult r; if (!refs.empty()) r = st.engine.addFilletEdges(3.0, refs);
            int nf = 0; const auto& t = st.engine.tree(); if (t.contains("features") && t["features"].is_array()) nf = (int)t["features"].size();
            bool ok = hit && !refs.empty() && r.ok && nf >= 2;
            std::fprintf(stderr, "[edgetest] hit=%d edge=%u resolved=%d filletOk=%d features=%d -> %s\n",
                         hit, pr.topo_id, (int)!refs.empty(), (int)r.ok, nf, ok ? "OK" : "FAIL");
            rc = ok ? 0 : 2;
        }
        application.shutdown();
        glfwDestroyWindow(win); glfwTerminate();
        return rc;
    }

    if (extrudetest) {
        auto& st = application.state();
        int rc = 2;
        if (!st.engineReady) std::fprintf(stderr, "[extrudetest] FAIL: no engine\n");
        else {
            st.engine.newPart("T");
            eng::OpsResult r = st.engine.sketchRectExtrude("plane.top", 40, 30, 20, "boss");
            int nf = 0; const auto& t = st.engine.tree(); if (t.contains("features") && t["features"].is_array()) nf = (int)t["features"].size();
            bool ok = r.ok && nf >= 2;
            std::fprintf(stderr, "[extrudetest] ok=%d features=%d rev=%d -> %s\n", (int)r.ok, nf, st.engine.revision(), ok ? "OK" : "FAIL");
            rc = ok ? 0 : 2;
        }
        application.shutdown();
        glfwDestroyWindow(win); glfwTerminate();
        return rc;
    }

    if (apptest) {
        auto& st = application.state();
        int rc = 2;
        if (!st.engineReady || !st.scene) std::fprintf(stderr, "[apptest] FAIL: no engine/scene\n");
        else {
            st.engine.newPart("T"); st.engine.addBox(40, 30, 20, "Base"); st.engine.rebuild();
            st.scene->resize(800, 600);
            st.scene->loadAicm(st.engine.aicmPath());
            uint32_t bid = st.scene->bodyIdAt(0);
            nlohmann::json ops = nlohmann::json::array();
            ops.push_back({ {"op","set_appearance"}, {"id", std::string("b") + std::to_string(bid)},
                            {"color", {1.00, 0.78, 0.34}}, {"metallic", 1.0}, {"roughness", 0.25} });
            eng::OpsResult r = st.engine.apply(ops);
            bool reloaded = r.ok && !st.engine.aicmPath().empty() && st.scene->loadAicm(st.engine.aicmPath());
            float c[3] = {0,0,0}; float m = -1, rough = -1;
            bool got = reloaded && st.scene->getBodyAppearance(st.scene->bodyIdAt(0), c, m, rough);
            bool colorOk = got && c[0] > 0.9f && c[1] > 0.65f && c[2] > 0.25f && c[2] < 0.45f
                               && m > 0.9f && rough < 0.35f;
            bool ok = r.ok && reloaded && colorOk;
            std::fprintf(stderr, "[apptest] applyOk=%d reload=%d color=(%.2f,%.2f,%.2f) metal=%.2f rough=%.2f -> %s\n",
                         (int)r.ok, (int)reloaded, c[0], c[1], c[2], m, rough, ok ? "OK" : "FAIL");
            rc = ok ? 0 : 2;
        }
        application.shutdown();
        glfwDestroyWindow(win); glfwTerminate();
        return rc;
    }

    if (demo) application.state().startDemoAgent("Design a 5V regulator board with a 3.3V output rail.");
    if (sketch) application.state().dispatch("sketch_new");
    if (doCmd)  application.state().dispatch(doCmd);
    if (openPalette) { application.state().paletteOpen = true; std::strcpy(application.state().paletteQuery, "pattern"); }

    int frame = 0;
    while (!glfwWindowShouldClose(win)) {
        glfwPollEvents();
        platform::tickWindowChrome(win, &chromeLayout);
        if (!shotPath && glfwGetWindowAttrib(win, GLFW_ICONIFIED)) { glfwWaitEventsTimeout(0.1); continue; }

        ImGui_ImplOpenGL3_NewFrame();
        ImGui_ImplGlfw_NewFrame();
        ImGui::GetIO().DeltaTime = 1.f/60.f;
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
