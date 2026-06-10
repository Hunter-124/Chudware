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

int run_gui(const char* screenshotPath);  // gui_app.cpp

namespace {

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
    if (argc >= 3 && std::strcmp(argv[1], "--ui-screenshot") == 0)
        return run_gui(argv[2]);  // headless UI capture
    // GUI opens immediately and provisions the runtime in-app (with progress) if needed.
    return run_gui(nullptr);
}
