// Window icon loading — stb_image + stb_image_resize (G1 wires stb include path).
#include "resources.h"

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include <cstdlib>

#include <stb_image.h>
#include <stb_image_resize2.h>

namespace platform {

void setWindowIcon(GLFWwindow* win, const std::string& path) {
    int w, h, n;
    unsigned char* src = stbi_load(path.c_str(), &w, &h, &n, 4);
    if (!src) return;

    static const int kSizes[] = {16, 32, 48, 128, 256};
    static const int kCount = (int)(sizeof(kSizes) / sizeof(kSizes[0]));

    GLFWimage imgs[kCount];
    unsigned char* bufs[kCount] = {};
    int nImgs = 0;

    for (int i = 0; i < kCount; ++i) {
        int s = kSizes[i];
        if (s > w || s > h) continue;
        unsigned char* buf = (unsigned char*)malloc((size_t)s * s * 4);
        if (!buf) continue;
        stbir_resize_uint8_srgb(src, w, h, 0, buf, s, s, 0, STBIR_RGBA);
        imgs[nImgs] = {s, s, buf};
        bufs[nImgs] = buf;
        ++nImgs;
    }

    if (nImgs == 0) {
        imgs[0] = {w, h, src};
        glfwSetWindowIcon(win, 1, imgs);
    } else {
        glfwSetWindowIcon(win, nImgs, imgs);
        for (int i = 0; i < nImgs; ++i) free(bufs[i]);
    }

    stbi_image_free(src);
}

}  // namespace platform
