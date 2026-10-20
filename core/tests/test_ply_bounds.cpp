// Standalone production PLY parser tests: OCCT headers only, no sewing or OCCT libraries.
#include <array>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include "doc/formats.h"
#include "doc/session.h"

namespace chudware::doc {
#include "../doc/ply_read.inc"
}

namespace {
using chudware::doc::OpError;
void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
template<class F> void rejected(F&& call) {
    try {
        call();
    } catch (const OpError& error) {
        require(error.code == "import_failed", "incorrect parser error code");
        return;
    }
    throw std::runtime_error("malformed PLY accepted");
}

struct Fixture {
    std::filesystem::path directory;
    std::filesystem::path path;
    Fixture() {
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        directory = std::filesystem::temp_directory_path() /
                    ("chudware-ply-bounds-" + std::to_string(stamp));
        require(std::filesystem::create_directory(directory), "cannot create fixture directory");
        path = directory / "fixture.ply";
    }
    ~Fixture() {
        std::error_code ignored;
        std::filesystem::remove_all(directory, ignored);
    }
    void write(const std::string& data) {
        std::ofstream out(path, std::ios::binary);
        out.write(data.data(), static_cast<std::streamsize>(data.size()));
        require(out.good(), "cannot write fixture");
    }
};

std::string header(const std::string& encoding, unsigned vertices = 3, unsigned faces = 1,
                   const std::string& index_type = "int") {
    return "ply\nformat " + encoding + " 1.0\nelement vertex " + std::to_string(vertices) +
        "\nproperty float x\nproperty float y\nproperty float z\nelement face " +
        std::to_string(faces) + "\nproperty list uchar " + index_type + " vertex_indices\nend_header\n";
}
void u32(std::string& out, std::uint32_t value) {
    for (unsigned i = 0; i < 4; ++i) out.push_back(static_cast<char>(value >> (8 * i)));
}
std::string binary_vertices() {
    std::string out;
    for (float value : {0.f, 0.f, 0.f, 1.f, 0.f, 0.f, 0.f, 1.f, 0.f}) {
        std::uint32_t bits = 0;
        std::memcpy(&bits, &value, sizeof(bits));
        u32(out, bits);
    }
    return out;
}

void regressions() {
    using namespace chudware::doc;
    Fixture fixture;
    auto read = [&] { return read_ply(fixture.path.string()); };
    auto refuse = [&](const std::string& data) {
        fixture.write(data);
        rejected(read);
    };
    // NativeInputs #3: exact one-byte upload and all incomplete scalar widths.
    refuse(header("binary_little_endian", 1, 0) + "x");
    for (const std::string& type : {"char", "uchar", "short", "ushort", "int", "uint",
                                   "int64", "uint64", "float", "double"}) {
        const auto width = ply::scalar_type(type).width;
        for (std::size_t size = 0; size < width; ++size) {
            // No slack byte: ASan sees the real scalar boundary, not a string's NUL.
            const std::vector<unsigned char> bytes(size ? size : 1, 0);
            const auto* cursor = bytes.data();
            const auto* end = cursor + size;
            rejected([&] { (void)ply::read_le_scalar(cursor, end, type); });
            require(cursor == bytes.data(), "truncated scalar advanced the cursor");
        }
        const std::vector<unsigned char> bytes(width, 0);
        const auto* cursor = bytes.data();
        require(ply::read_le_scalar(cursor, cursor + width, type) == 0, "complete scalar changed");
        require(cursor == bytes.data() + width, "complete scalar failed to advance");
    }
    rejected([] { (void)ply::scalar_type("half"); });

    const std::string binary_header = header("binary_little_endian");
    const std::string vertices = binary_vertices();
    std::string face(1, '\3');
    u32(face, 0); u32(face, 1); u32(face, 2);
    for (std::size_t size = 0; size < vertices.size(); ++size)
        refuse(binary_header + vertices.substr(0, size));
    for (std::size_t size = 0; size < face.size(); ++size)
        refuse(binary_header + vertices + face.substr(0, size));
    fixture.write(binary_header + vertices + face);
    const auto binary = read();
    require(binary.vertex_count() == 3 && binary.triangle_count() == 1, "valid binary PLY changed");
    require(binary.tris == std::vector<std::uint32_t>({0, 1, 2}), "binary triangle changed");
    for (std::uint32_t index : {3u, UINT32_MAX}) {
        std::string invalid = face.substr(0, 9);
        u32(invalid, index);
        refuse(binary_header + vertices + invalid);
    }

    // NativeInputs #4: complete review witness, negative index, failed extraction,
    // fractional integer, nonfinite coordinate and invalid declared property types.
    const std::string ascii_header = header("ascii");
    const std::string ascii_vertices = "0 0 0\n1 0 0\n0 1 0\n";
    for (const std::string& row : {"3 0 1 3", "3 0 1 -1", "3 0 1", "3 0 nope 2",
                                  "3 0 1 1.5", "255 0 1 2"})
        refuse(ascii_header + ascii_vertices + row);
    refuse(ascii_header + "0 0 0\n1 0 0\n0 nan 0\n3 0 1 2\n");
    refuse(header("ascii", 3, 1, "float") + ascii_vertices + "3 0 1 2\n");
    refuse(header("ascii", 3, 1, "half") + ascii_vertices + "3 0 1 2\n");
    fixture.write(ascii_header + ascii_vertices + "3 0 1 2\n");
    const auto ascii = read();
    require(ascii.verts == binary.verts && ascii.tris == binary.tris, "ASCII/binary PLY diverged");
}
}  // namespace

int main() {
    try {
        regressions();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
