// test_img_tex.cpp — ctest for img_tex decode helpers (no GL context needed).
// Tests decodeFile / decodeMemory only; GL upload functions need a live context.
//
// RE-08 acceptance: fixture PNG + JPEG decoded, dimensions verified.

#define STB_IMAGE_IMPLEMENTATION
#include "stb_image.h"
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "stb_image_write.h"

#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <string>
#include <filesystem>
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

[[noreturn]] static void fail(const char* message) {
    std::fprintf(stderr, "[FAIL] %s\n", message);
    std::exit(1);
}

static void require(bool condition, const char* message) {
    if (!condition) fail(message);
}

// Write a 4x3 red RGBA PNG to disk, return path.
static std::string makeFixturePNG() {
    const int W = 4, H = 3;
    unsigned char px[W * H * 4];
    for (int i = 0; i < W * H; i++) {
        px[i*4+0] = 255; px[i*4+1] = 0; px[i*4+2] = 0; px[i*4+3] = 255;
    }
    std::string path = gTmpDir + "/fixture.png";
    int ok = stbi_write_png(path.c_str(), W, H, 4, px, W * 4);
    require(ok != 0, "stbi_write_png failed");
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
    require(ok != 0, "stbi_write_jpg failed");
    return path;
}

// ---- Tests ---------------------------------------------------------------

static void test_decode_png_from_file() {
    std::string path = makeFixturePNG();
    std::vector<unsigned char> pixels;
    int w = 0, h = 0;
    bool ok = decodeFile(path, pixels, w, h);
    require(ok, "PNG decodeFile returned false");
    require(w == 4, "PNG width mismatch");
    require(h == 3, "PNG height mismatch");
    require(static_cast<int>(pixels.size()) == w * h * 4,
            "PNG pixel buffer size mismatch");
    require(pixels[0] == 255 && pixels[1] == 0 && pixels[2] == 0 &&
                pixels[3] == 255,
            "PNG first pixel mismatch");
    std::printf("[PASS] test_decode_png_from_file  (%dx%d)\n", w, h);
}

static void test_decode_jpeg_from_file() {
    std::string path = makeFixtureJPEG();
    std::vector<unsigned char> pixels;
    int w = 0, h = 0;
    bool ok = decodeFile(path, pixels, w, h);
    require(ok, "JPEG decodeFile returned false");
    require(w == 5, "JPEG width mismatch");
    require(h == 2, "JPEG height mismatch");
    require(static_cast<int>(pixels.size()) == w * h * 4,
            "JPEG pixel buffer size mismatch");
    std::printf("[PASS] test_decode_jpeg_from_file  (%dx%d)\n", w, h);
}

static void test_decode_png_from_memory() {
    std::string path = makeFixturePNG();
    // Read raw bytes
    FILE* f = std::fopen(path.c_str(), "rb");
    require(f != nullptr, "failed to open generated PNG");
    require(std::fseek(f, 0, SEEK_END) == 0, "failed to seek generated PNG");
    const long sz = std::ftell(f);
    require(sz > 0, "generated PNG is empty");
    std::rewind(f);
    std::vector<unsigned char> raw(static_cast<std::size_t>(sz));
    require(std::fread(raw.data(), 1, raw.size(), f) == raw.size(),
            "failed to read generated PNG");
    require(std::fclose(f) == 0, "failed to close generated PNG");

    std::vector<unsigned char> pixels;
    int w = 0, h = 0;
    bool ok = decodeMemory(raw.data(), (int)raw.size(), pixels, w, h);
    require(ok, "PNG decodeMemory returned false");
    require(w == 4 && h == 3, "memory PNG dimensions mismatch");
    std::printf("[PASS] test_decode_png_from_memory (%dx%d)\n", w, h);
}

static void test_bad_path_returns_false() {
    std::vector<unsigned char> pixels;
    int w = 0, h = 0;
    bool ok = decodeFile("/nonexistent/no_such_file.png", pixels, w, h);
    require(!ok, "expected failure for bad path");
    std::printf("[PASS] test_bad_path_returns_false\n");
}

int main(int argc, char* argv[]) {
    // Accept an optional tmp dir argument (CMake passes TEST_TMPDIR)
    gTmpDir = (argc > 1) ? argv[1] : "/tmp";
    std::error_code directory_error;
    std::filesystem::create_directories(gTmpDir, directory_error);
    require(!directory_error, "failed to create fixture directory");

    test_decode_png_from_file();
    test_decode_jpeg_from_file();
    test_decode_png_from_memory();
    test_bad_path_returns_false();

    std::printf("All img_tex tests passed.\n");
    return 0;
}
