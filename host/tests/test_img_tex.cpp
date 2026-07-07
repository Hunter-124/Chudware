// test_img_tex.cpp — ctest for img_tex decode helpers (no GL context needed).
// Tests decodeFile / decodeMemory only; GL upload functions need a live context.
//
// RE-08 acceptance: fixture PNG + JPEG decoded, dimensions verified.

#define STB_IMAGE_IMPLEMENTATION
#include "stb_image.h"
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "stb_image_write.h"

#include <cassert>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

// ---- Inline the pure-decode helpers (no GL) from img_tex logic -----------

static bool decodeFile(const std::string& path, std::vector<unsigned char>& pixels,
                       int& w, int& h) {
    int ch = 0;
    stbi_uc* px = stbi_load(path.c_str(), &w, &h, &ch, STBI_rgb_alpha);
    if (!px) return false;
    pixels.assign(px, px + (size_t)w * h * 4);
    stbi_image_free(px);
    return true;
}

static bool decodeMemory(const unsigned char* data, int len,
                         std::vector<unsigned char>& pixels, int& w, int& h) {
    int ch = 0;
    stbi_uc* px = stbi_load_from_memory(data, len, &w, &h, &ch, STBI_rgb_alpha);
    if (!px) return false;
    pixels.assign(px, px + (size_t)w * h * 4);
    stbi_image_free(px);
    return true;
}

// ---- Fixture generation ---------------------------------------------------

static std::string gTmpDir;

// Write a 4x3 red RGBA PNG to disk, return path.
static std::string makeFixturePNG() {
    const int W = 4, H = 3;
    unsigned char px[W * H * 4];
    for (int i = 0; i < W * H; i++) {
        px[i*4+0] = 255; px[i*4+1] = 0; px[i*4+2] = 0; px[i*4+3] = 255;
    }
    std::string path = gTmpDir + "/fixture.png";
    int ok = stbi_write_png(path.c_str(), W, H, 4, px, W * 4);
    assert(ok && "stbi_write_png failed");
    return path;
}

// Write a 5x2 green RGB JPEG to disk, return path.
static std::string makeFixtureJPEG() {
    const int W = 5, H = 2;
    unsigned char px[W * H * 3];
    for (int i = 0; i < W * H; i++) {
        px[i*3+0] = 0; px[i*3+1] = 200; px[i*3+2] = 0;
    }
    std::string path = gTmpDir + "/fixture.jpg";
    int ok = stbi_write_jpg(path.c_str(), W, H, 3, px, 90);
    assert(ok && "stbi_write_jpg failed");
    return path;
}

// ---- Tests ---------------------------------------------------------------

static void test_decode_png_from_file() {
    std::string path = makeFixturePNG();
    std::vector<unsigned char> pixels;
    int w = 0, h = 0;
    bool ok = decodeFile(path, pixels, w, h);
    assert(ok && "PNG decodeFile returned false");
    assert(w == 4 && "PNG width mismatch");
    assert(h == 3 && "PNG height mismatch");
    assert((int)pixels.size() == w * h * 4 && "PNG pixel buffer size mismatch");
    // First pixel should be red
    assert(pixels[0] == 255 && pixels[1] == 0 && pixels[2] == 0 && pixels[3] == 255);
    std::printf("[PASS] test_decode_png_from_file  (%dx%d)\n", w, h);
}

static void test_decode_jpeg_from_file() {
    std::string path = makeFixtureJPEG();
    std::vector<unsigned char> pixels;
    int w = 0, h = 0;
    bool ok = decodeFile(path, pixels, w, h);
    assert(ok && "JPEG decodeFile returned false");
    assert(w == 5 && "JPEG width mismatch");
    assert(h == 2 && "JPEG height mismatch");
    assert((int)pixels.size() == w * h * 4);
    std::printf("[PASS] test_decode_jpeg_from_file  (%dx%d)\n", w, h);
}

static void test_decode_png_from_memory() {
    std::string path = makeFixturePNG();
    // Read raw bytes
    FILE* f = std::fopen(path.c_str(), "rb");
    assert(f);
    std::fseek(f, 0, SEEK_END);
    long sz = std::ftell(f);
    std::rewind(f);
    std::vector<unsigned char> raw(sz);
    std::fread(raw.data(), 1, sz, f);
    std::fclose(f);

    std::vector<unsigned char> pixels;
    int w = 0, h = 0;
    bool ok = decodeMemory(raw.data(), (int)raw.size(), pixels, w, h);
    assert(ok && "PNG decodeMemory returned false");
    assert(w == 4 && h == 3);
    std::printf("[PASS] test_decode_png_from_memory (%dx%d)\n", w, h);
}

static void test_bad_path_returns_false() {
    std::vector<unsigned char> pixels;
    int w = 0, h = 0;
    bool ok = decodeFile("/nonexistent/no_such_file.png", pixels, w, h);
    assert(!ok && "expected failure for bad path");
    std::printf("[PASS] test_bad_path_returns_false\n");
}

int main(int argc, char* argv[]) {
    // Accept an optional tmp dir argument (CMake passes TEST_TMPDIR)
    gTmpDir = (argc > 1) ? argv[1] : "/tmp";

    test_decode_png_from_file();
    test_decode_jpeg_from_file();
    test_decode_png_from_memory();
    test_bad_path_returns_false();

    std::printf("All img_tex tests passed.\n");
    return 0;
}
