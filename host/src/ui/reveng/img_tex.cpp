// img_tex.cpp — image decode + GL texture util (RE-08)
// stb_image is compiled in host/src/stb_impl.cpp; only include the header here.
#include "img_tex.h"

#include "stb_image.h"
#include <glad/gl.h>

#include <cstdio>

namespace reveng {

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

static unsigned int uploadRGBA(const unsigned char* px, int w, int h) {
    if (!px || w <= 0 || h <= 0) return 0;
    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
    glBindTexture(GL_TEXTURE_2D, 0);
    return static_cast<unsigned int>(tex);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

bool decodeFile(const std::string& path, std::vector<unsigned char>& pixels,
                int& w, int& h) {
    int channels = 0;
    stbi_uc* px = stbi_load(path.c_str(), &w, &h, &channels, STBI_rgb_alpha);
    if (!px) {
        std::fprintf(stderr, "[img_tex] stbi_load failed for '%s': %s\n",
                     path.c_str(), stbi_failure_reason());
        return false;
    }
    pixels.assign(px, px + static_cast<size_t>(w) * h * 4);
    stbi_image_free(px);
    return true;
}

bool decodeMemory(const unsigned char* data, int dataLen,
                  std::vector<unsigned char>& pixels, int& w, int& h) {
    int channels = 0;
    stbi_uc* px = stbi_load_from_memory(data, dataLen, &w, &h, &channels, STBI_rgb_alpha);
    if (!px) {
        std::fprintf(stderr, "[img_tex] stbi_load_from_memory failed: %s\n",
                     stbi_failure_reason());
        return false;
    }
    pixels.assign(px, px + static_cast<size_t>(w) * h * 4);
    stbi_image_free(px);
    return true;
}

unsigned int loadTexFromFile(const std::string& path, int* outW, int* outH) {
    int w = 0, h = 0, channels = 0;
    stbi_uc* px = stbi_load(path.c_str(), &w, &h, &channels, STBI_rgb_alpha);
    if (!px) {
        std::fprintf(stderr, "[img_tex] stbi_load failed for '%s': %s\n",
                     path.c_str(), stbi_failure_reason());
        return 0;
    }
    unsigned int tex = uploadRGBA(px, w, h);
    stbi_image_free(px);
    if (outW) *outW = w;
    if (outH) *outH = h;
    return tex;
}

unsigned int loadTexFromMemory(const unsigned char* data, int dataLen,
                               int* outW, int* outH) {
    int w = 0, h = 0, channels = 0;
    stbi_uc* px = stbi_load_from_memory(data, dataLen, &w, &h, &channels, STBI_rgb_alpha);
    if (!px) {
        std::fprintf(stderr, "[img_tex] stbi_load_from_memory failed: %s\n",
                     stbi_failure_reason());
        return 0;
    }
    unsigned int tex = uploadRGBA(px, w, h);
    stbi_image_free(px);
    if (outW) *outW = w;
    if (outH) *outH = h;
    return tex;
}

void freeTexture(unsigned int texId) {
    if (texId) {
        GLuint t = static_cast<GLuint>(texId);
        glDeleteTextures(1, &t);
    }
}

} // namespace reveng
