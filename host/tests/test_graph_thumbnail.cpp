#include "ui/plm/graph_thumbnail.h"
#include "stb_image_write.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace fs = std::filesystem;
namespace intake = ui::graph_thumbnail;
static void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
static void appendPng(void* opaque, void* bytes, int count) {
    static_cast<std::string*>(opaque)->append(static_cast<const char*>(bytes), static_cast<size_t>(count));
}
static std::string tinyPng() {
    std::string png;
    const unsigned char pixels[] = {10, 20, 30, 255, 40, 50, 60, 255};
    require(stbi_write_png_to_func(appendPng, &png, 2, 1, 4, pixels, 8) != 0, "encode real PNG fixture");
    return png;
}
static std::string typicalPng() {
    std::vector<unsigned char> pixels(256 * 256 * 4);
    std::uint32_t state = 1;
    for (auto& pixel : pixels) {
        state = state * 1664525u + 1013904223u;
        pixel = static_cast<unsigned char>(state >> 24);
    }
    std::string png;
    require(stbi_write_png_to_func(appendPng, &png, 256, 256, 4, pixels.data(), 1024) != 0,
            "encode typical thumbnail fixture");
    return png;
}

static void appendChunk(std::string& png, const char* type, const std::string& data) {
    const auto size = static_cast<std::uint32_t>(data.size());
    for (int shift : {24, 16, 8, 0}) png.push_back(static_cast<char>(size >> shift));
    const size_t crcOffset = png.size();
    png.append(type, 4);
    png += data;
    const auto crc = static_cast<std::uint32_t>(mz_crc32(
        MZ_CRC32_INIT, reinterpret_cast<const unsigned char*>(png.data() + crcOffset),
        data.size() + 4));
    for (int shift : {24, 16, 8, 0}) png.push_back(static_cast<char>(crc >> shift));
}

static std::string tinyHeaderInflationBomb(const std::string& png) {
    const std::string expanded(1024 * 1024, '\0');
    std::string compressed(mz_compressBound(static_cast<mz_ulong>(expanded.size())), '\0');
    mz_ulong size = static_cast<mz_ulong>(compressed.size());
    require(mz_compress(reinterpret_cast<unsigned char*>(compressed.data()), &size,
                        reinterpret_cast<const unsigned char*>(expanded.data()),
                        static_cast<mz_ulong>(expanded.size())) == MZ_OK, "compress bounded IDAT fixture");
    compressed.resize(static_cast<size_t>(size));
    std::string result = png.substr(0, 33);  // signature and real tiny IHDR + CRC
    appendChunk(result, "IDAT", compressed);
    appendChunk(result, "IEND", "");
    return result;
}
static std::string package(const std::vector<std::pair<std::string, std::string>>& entries,
                           int compression = 0) {
    mz_zip_archive zip{};
    require(mz_zip_writer_init_heap(&zip, 0, 0) != 0, "fixture ZIP init");
    for (const auto& entry : entries)
        require(mz_zip_writer_add_mem(&zip, entry.first.c_str(), entry.second.data(), entry.second.size(), compression) != 0,
                "fixture ZIP entry");
    void* bytes = nullptr;
    size_t size = 0;
    require(mz_zip_writer_finalize_heap_archive(&zip, &bytes, &size) != 0, "fixture ZIP finalize");
    std::string result(static_cast<const char*>(bytes), size);
    mz_free(bytes);
    mz_zip_writer_end(&zip);
    return result;
}
static void put(const fs::path& path, const std::string& bytes) {
    std::ofstream out(path, std::ios::binary);
    out.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    require(out.good(), "fixture file written");
}
static std::uint32_t u32(const std::string& bytes, size_t pos) {
    return intake::le32(reinterpret_cast<const unsigned char*>(bytes.data() + pos));
}
static void put32(std::string& bytes, size_t pos, std::uint32_t value) {
    for (unsigned i = 0; i < 4; ++i) bytes.at(pos + i) = static_cast<char>(value >> (8 * i));
}
static size_t central(const std::string& bytes) {
    require(bytes.size() >= 22 && u32(bytes, bytes.size() - 22) == 0x06054b50, "fixture EOCD");
    return u32(bytes, bytes.size() - 6);
}

int main() {
    fs::path root;
    try {
        root = fs::temp_directory_path() /
            ("chudware-thumbnail-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
        require(fs::create_directory(root), "unique scratch directory");
        struct Cleanup { fs::path root; ~Cleanup() { std::error_code ec; fs::remove_all(root, ec); } } cleanup{root};
        const auto path = root / fs::u8path("quoted'-\xe9\x9b\xaa.step+");
        const std::string png = tinyPng();
        std::string out;
        auto read = [&](const std::string& bytes) {
            put(path, bytes); return intake::readPackageThumbnail(path.u8string(), out);
        };
        require(read(package({{"meta.json", "{}"}, {"thumbnail.png", png}})) && out == png,
                "exact-case stored PNG round trip");
        int w = 0, h = 0;
        unsigned char* pixels = intake::decodeThumbnailPng(out, w, h);
        require(pixels && w == 2 && h == 1 && pixels[0] == 10 && pixels[4] == 40,
                "real PNG decoded exact pixels");
        stbi_image_free(pixels);
        const auto typical = typicalPng();
        require(read(package({{"thumbnail.png", typical}})), "typical packaged thumbnail admitted");
        pixels = intake::decodeThumbnailPng(out, w, h);
        require(pixels && w == 256 && h == 256, "bounded PNG ring supports multiple output windows");
        stbi_image_free(pixels);
        require(read(package({{"thumbnail.png", png}}, 6)) && out == png, "deflated benign PNG admitted");
        require(!read(package({{"Thumbnail.png", png}})) && out.empty(), "uppercase thumbnail refused");
        require(!read(package({{"thumbnail.png", png}, {"Thumbnail.png", png}})), "case alias ambiguity refused");
        require(!read(package({{"thumbnail.png", png}, {"thumbnail.png", png}})), "duplicate thumbnail refused");
        require(!read(package({{"thumbnail.png", png}, {"notes", "a"}, {"notes", "b"}})), "duplicate archive names refused");
        require(!read(package({{"nested/thumbnail.png", png}})), "nested thumbnail does not alias canonical root");
        auto bytes = package({{"thumbnail.png", png}});
        const size_t directory = central(bytes);
        bytes.at(directory + 46 + 5) = '\0';
        require(!read(bytes), "embedded-NUL member name refused");

        // A tiny, deliberately invalid payload advertises a huge expanded length.
        // Refusal must leave no expanded-output allocation, before any inflate/CRC work.
        bytes = package({{"thumbnail.png", "not deflate"}}, 0);
        put32(bytes, central(bytes) + 24, static_cast<std::uint32_t>(intake::kMaxPngBytes + 1));
        put32(bytes, 22, static_cast<std::uint32_t>(intake::kMaxPngBytes + 1));
        out = "old";
        const size_t oldCapacity = out.capacity();
        require(!read(bytes) && out.empty() && out.capacity() == oldCapacity,
                "oversized declared thumbnail refuses before expanded allocation");
        bytes = package({{"thumbnail.png", std::string(1024 * 1024, '\0')}}, 6);
        require(!read(bytes) && out.empty(), "high-ratio thumbnail refuses before inflation");
        bytes = package({{"thumbnail.png", png}});
        put32(bytes, central(bytes) + 20, static_cast<std::uint32_t>(intake::kMaxPngBytes + 1));
        require(!read(bytes), "oversized compressed thumbnail refused");
        bytes = package({{"thumbnail.png", png}});
        bytes.at(30 + std::string("thumbnail.png").size() + 10) ^= 1;
        require(!read(bytes) && out.empty(), "stored payload CRC mismatch refused");
        bytes = package({{"thumbnail.png", png}}, 6);
        put32(bytes, central(bytes) + 24, 1);
        put32(bytes, 22, 1);
        require(!read(bytes) && out.empty(), "dishonest small output cannot overrun fixed buffer");

        bytes = package({{"thumbnail.png", png}});
        put32(bytes, bytes.size() - 10, intake::kMaxDirectoryBytes + 1);
        require(!read(bytes), "central directory cap refuses before miniz allocation");
        bytes = package({{"thumbnail.png", png}});
        const size_t end = bytes.size() - 22;
        bytes.at(end + 8) = bytes.at(end + 10) = 1;
        bytes.at(end + 9) = bytes.at(end + 11) = 32;
        require(!read(bytes), "archive entry cap refuses before miniz allocation");
        bytes = package({{"thumbnail.png", png}});
        put32(bytes, bytes.size() - 6, UINT32_MAX);
        require(!read(bytes), "invalid/ZIP64 directory offset refused");
        put(path, package({{"thumbnail.png", png}}));
        fs::resize_file(path, intake::kMaxPackageBytes + 1);
        require(!intake::readPackageThumbnail(path.u8string(), out), "oversized package data refused");

        std::string huge = png;
        // PNG IHDR uses big endian; info must reject the header before pixel allocation.
        huge.at(16) = 0; huge.at(17) = 0; huge.at(18) = 0x20; huge.at(19) = 1;
        require(!intake::decodeThumbnailPng(huge, w, h) && w == 0 && h == 0,
                "oversized image dimensions refuse before decode");
        huge = png; huge.at(20) = huge.at(21) = huge.at(22) = huge.at(23) = 0;
        require(!intake::decodeThumbnailPng(huge, w, h), "zero image dimension refused");
        require(!intake::decodeThumbnailPng(tinyHeaderInflationBomb(png), w, h),
                "small IHDR cannot bypass expanded IDAT scratch limit");
        require(!intake::decodeThumbnailPng("not a PNG", w, h), "non-PNG thumbnail refused");
        require(!intake::decodeThumbnailPng(std::string(intake::kMaxPngBytes + 1, 'x'), w, h),
                "encoded PNG cap refuses before header/decode");
        require(!intake::readPackageThumbnail(std::string("x\0y", 3), out), "NUL package path refused");
        require(!intake::readPackageThumbnail((root / "missing").string(), out), "missing package refuses");
        std::cout << "package thumbnail archive, dimension and CRC regressions passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAILED: " << error.what() << '\n';
        return 1;
    }
}
