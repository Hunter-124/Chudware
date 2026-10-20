// Private CPU intake for the graph's optional packaged thumbnail. No GL or UI state.
#pragma once

#include "miniz.h"
#include "stb_image.h"

#include <array>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <exception>
#include <filesystem>
#include <fstream>
#include <set>
#include <string>

namespace ui::graph_thumbnail {

// Match the native container's 4096-entry / 256-MiB archive and 200:1 limits;
// a display-only PNG has a much smaller encoded and expanded pixel allowance.
inline constexpr std::uint64_t kMaxPackageBytes = 256ull * 1024 * 1024;
inline constexpr std::uint32_t kMaxDirectoryBytes = 4u * 1024 * 1024;
inline constexpr mz_uint kMaxEntries = 4096;
inline constexpr std::size_t kMaxPngBytes = 8u * 1024 * 1024;
inline constexpr std::uint64_t kMaxCompressionRatio = 200;
inline constexpr int kMaxImageEdge = 2048;
inline constexpr std::uint64_t kMaxImagePixels = 4ull * 1024 * 1024;

struct PackageReader {
    std::ifstream file;
    std::uint64_t size = 0;
    mz_zip_archive zip{};
    ~PackageReader() { if (zip.m_pState) mz_zip_reader_end(&zip); }

    static size_t read(void* opaque, mz_uint64 offset, void* buffer, size_t bytes) {
        auto& self = *static_cast<PackageReader*>(opaque);
        if (offset > self.size || bytes > self.size - offset) return 0;
        self.file.clear();
        self.file.seekg(static_cast<std::streamoff>(offset));
        self.file.read(static_cast<char*>(buffer), static_cast<std::streamsize>(bytes));
        return static_cast<size_t>(self.file.gcount());
    }

    static void* allocate(void*, size_t items, size_t bytes) {
        if (bytes != 0 && items > kMaxDirectoryBytes / bytes) return nullptr;
        return std::malloc(items * bytes);
    }
    static void* reallocate(void*, void* pointer, size_t items, size_t bytes) {
        if (bytes != 0 && items > kMaxDirectoryBytes / bytes) return nullptr;
        return std::realloc(pointer, items * bytes);
    }
    static void release(void*, void* pointer) { std::free(pointer); }
};

inline std::uint16_t le16(const unsigned char* p) {
    return static_cast<std::uint16_t>(p[0] | (static_cast<unsigned>(p[1]) << 8));
}
inline std::uint32_t le32(const unsigned char* p) {
    return static_cast<std::uint32_t>(le16(p)) |
           (static_cast<std::uint32_t>(le16(p + 2)) << 16);
}

// Bound the central-directory allocation BEFORE miniz initializes. Native small
// thumbnails do not require multidisk/ZIP64 packages; those optional previews miss.
inline bool boundedDirectory(PackageReader& reader) {
    std::array<unsigned char, 65535 + 22> footer{};
    const size_t bytes = static_cast<size_t>(
        reader.size < footer.size() ? reader.size : footer.size());
    if (bytes < 22 || PackageReader::read(&reader, reader.size - bytes,
                                         footer.data(), bytes) != bytes) return false;
    for (size_t pos = bytes - 22;; --pos) {
        const auto* end = footer.data() + pos;
        if (le32(end) == 0x06054b50) {
            if (pos + 22 + le16(end + 20) != bytes) return false;
            const std::uint16_t count = le16(end + 10);
            const std::uint32_t directoryBytes = le32(end + 12);
            const std::uint32_t directoryOffset = le32(end + 16);
            const std::uint64_t endOffset = reader.size - bytes + pos;
            std::array<unsigned char, 20> locator{};
            if (endOffset >= locator.size() &&
                PackageReader::read(&reader, endOffset - locator.size(), locator.data(),
                                    locator.size()) == locator.size() &&
                le32(locator.data()) == 0x07064b50) return false;
            return le16(end + 4) == 0 && le16(end + 6) == 0 &&
                   le16(end + 8) == count && count <= kMaxEntries &&
                   directoryBytes <= kMaxDirectoryBytes &&
                   directoryOffset <= endOffset &&
                   directoryBytes <= endOffset - directoryOffset;
        }
        if (pos == 0) return false;
    }
}

inline bool readPackageThumbnail(const std::string& path, std::string& out) {
    out.clear();
    if (path.empty() || path.find('\0') != std::string::npos) return false;
    PackageReader reader;
    try {
        const auto filePath = std::filesystem::u8path(path);
        std::error_code ec;
        if (!std::filesystem::is_regular_file(filePath, ec)) return false;
        reader.file.open(filePath, std::ios::binary | std::ios::ate);
    } catch (const std::exception&) {
        return false;
    }
    if (!reader.file) return false;
    const auto size = reader.file.tellg();
    if (size < 0 || static_cast<std::uint64_t>(size) > kMaxPackageBytes) return false;
    reader.size = static_cast<std::uint64_t>(size);
    if (!boundedDirectory(reader)) return false;
    reader.zip.m_pRead = &PackageReader::read;
    reader.zip.m_pIO_opaque = &reader;
    reader.zip.m_pAlloc = &PackageReader::allocate;
    reader.zip.m_pRealloc = &PackageReader::reallocate;
    reader.zip.m_pFree = &PackageReader::release;
    if (!mz_zip_reader_init(&reader.zip, reader.size,
                            MZ_ZIP_FLAG_DO_NOT_SORT_CENTRAL_DIRECTORY)) return false;
    const mz_uint count = mz_zip_reader_get_num_files(&reader.zip);
    if (count > kMaxEntries) return false;
    std::set<std::string> names;
    mz_uint thumbnail = count;
    mz_zip_archive_file_stat thumbStat{};
    for (mz_uint i = 0; i < count; ++i) {
        mz_zip_archive_file_stat stat{};
        if (!mz_zip_reader_file_stat(&reader.zip, i, &stat)) return false;
        const mz_uint nameBytes = mz_zip_reader_get_filename(&reader.zip, i, nullptr, 0);
        // Truncated or embedded-NUL names cannot be compared unambiguously.
        if (nameBytes == 0 || nameBytes > sizeof(stat.m_filename) ||
            std::strlen(stat.m_filename) + 1 != nameBytes) return false;
        const std::string name(stat.m_filename);
        if (!names.insert(name).second) return false;
        bool isThumbnail = name.size() == sizeof("thumbnail.png") - 1;
        for (size_t ch = 0; isThumbnail && ch < name.size(); ++ch) {
            char c = name[ch];
            if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
            isThumbnail = c == "thumbnail.png"[ch];
        }
        if (!isThumbnail) continue;
        if (name != "thumbnail.png" || thumbnail != count || stat.m_is_directory ||
            stat.m_is_encrypted || !stat.m_is_supported) return false;
        thumbnail = i;
        thumbStat = stat;
    }
    if (thumbnail == count || thumbStat.m_uncomp_size == 0 ||
        thumbStat.m_uncomp_size > kMaxPngBytes || thumbStat.m_comp_size == 0 ||
        thumbStat.m_comp_size > kMaxPngBytes ||
        thumbStat.m_uncomp_size > thumbStat.m_comp_size * kMaxCompressionRatio)
        return false;
    // A single fixed-capacity destination prevents both dishonest output growth
    // and extract-to-heap's second full copy. miniz still verifies CRC/errors.
    out.resize(static_cast<size_t>(thumbStat.m_uncomp_size));
    if (!mz_zip_reader_extract_to_mem(&reader.zip, thumbnail, out.data(), out.size(), 0)) {
        out.clear();
        return false;
    }
    return true;
}

inline std::uint32_t be32(const unsigned char* p) {
    return (static_cast<std::uint32_t>(p[0]) << 24) |
           (static_cast<std::uint32_t>(p[1]) << 16) |
           (static_cast<std::uint32_t>(p[2]) << 8) | p[3];
}

// stb's PNG zlib scratch buffer can otherwise grow beyond even a tiny IHDR.
// Validate the IDAT stream with a fixed 32-KiB ring before its allocating decode.
// This security pass also bounds malformed IDAT inflation; it is not a pixel copy.
inline bool boundedPngInflation(const std::string& png, int width, int height) {
    const auto* data = reinterpret_cast<const unsigned char*>(png.data());
    const std::uint64_t budget = static_cast<std::uint64_t>(width) * height * 8 +
                                 static_cast<std::uint64_t>(height) * 16 + 128;
    std::array<unsigned char, TINFL_LZ_DICT_SIZE> ring{};
    tinfl_decompressor decoder;
    tinfl_init(&decoder);
    size_t ringOffset = 0;
    std::uint64_t expanded = 0;
    bool seenIdat = false, closedIdat = false, done = false;
    for (size_t offset = 8; offset < png.size();) {
        if (png.size() - offset < 12) return false;
        const size_t bytes = be32(data + offset);
        if (bytes > png.size() - offset - 12) return false;
        const auto* type = data + offset + 4;
        if (std::memcmp(type, "IDAT", 4) == 0) {
            if (closedIdat || (done && bytes != 0)) return false;
            seenIdat = true;
            size_t consumed = 0;
            bool flushOutput = false;
            while (consumed < bytes || flushOutput) {
                size_t input = bytes - consumed;
                size_t output = ring.size() - ringOffset;
                const auto status = tinfl_decompress(
                    &decoder, data + offset + 8 + consumed, &input,
                    ring.data(), ring.data() + ringOffset, &output,
                    TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_HAS_MORE_INPUT);
                if (output > budget - expanded) return false;
                expanded += output;
                consumed += input;
                ringOffset = (ringOffset + output) % ring.size();
                if (status == TINFL_STATUS_DONE) {
                    done = true;
                    if (consumed != bytes) return false;
                    break;
                }
                if (status < 0 || (input == 0 && output == 0)) return false;
                flushOutput = status == TINFL_STATUS_HAS_MORE_OUTPUT;
            }
        } else {
            if (seenIdat) closedIdat = true;
            if (std::memcmp(type, "IEND", 4) == 0)
                return bytes == 0 && done && seenIdat && offset + 12 == png.size();
        }
        offset += bytes + 12;
    }
    return false;
}

inline unsigned char* decodeThumbnailPng(const std::string& png, int& width, int& height) {
    width = height = 0;
    static constexpr unsigned char signature[] = {137, 80, 78, 71, 13, 10, 26, 10};
    if (png.size() < sizeof(signature) || png.size() > kMaxPngBytes ||
        std::memcmp(png.data(), signature, sizeof(signature)) != 0) return nullptr;
    const auto* data = reinterpret_cast<const unsigned char*>(png.data());
    int w = 0, h = 0, channels = 0;
    if (!stbi_info_from_memory(data, static_cast<int>(png.size()), &w, &h, &channels) ||
        w <= 0 || h <= 0 || w > kMaxImageEdge || h > kMaxImageEdge ||
        static_cast<std::uint64_t>(w) * static_cast<std::uint64_t>(h) > kMaxImagePixels)
        return nullptr;
    if (!boundedPngInflation(png, w, h)) return nullptr;
    int decodedW = 0, decodedH = 0;
    unsigned char* pixels = stbi_load_from_memory(data, static_cast<int>(png.size()),
                                                 &decodedW, &decodedH, &channels, 4);
    if (!pixels || decodedW != w || decodedH != h) {
        if (pixels) stbi_image_free(pixels);
        return nullptr;
    }
    width = w;
    height = h;
    return pixels;
}

}  // namespace ui::graph_thumbnail
