// Standalone sanitizer regression for the production flow STL loader; no solver or OCCT.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <vector>

namespace chudware::sim {
#include "../sim/stl_read.inc"
}

namespace {
void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}

struct Fixture {
    std::filesystem::path directory;
    std::filesystem::path path;
    Fixture() {
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        directory = std::filesystem::temp_directory_path() /
                    ("chudware-stl-bounds-" + std::to_string(stamp));
        require(std::filesystem::create_directory(directory), "cannot create fixture directory");
        path = directory / "fixture.stl";
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

void regressions() {
    Fixture fixture;
    std::vector<float> pos;
    std::vector<std::uint32_t> idx;
    std::string error;
    auto read = [&] { return chudware::sim::load_stl_soup(fixture.path.string(), pos, idx, error); };

    // NumericalServices #7: no newline or allocation-slack assumptions. Each fixture
    // ends precisely after the keyword (30/60/120 bytes) at the terminating NUL.
    for (std::size_t size : {30u, 60u, 120u}) {
        fixture.write("solid facet " + std::string(size - 18, ' ') + "vertex");
        require(!read(), "unterminated vertex accepted");
        require(error == "bad ASCII STL vertex", "unterminated vertex not rejected at parsing");
    }
    const std::string valid = "solid fixture\nfacet normal 0 0 1\nouter loop\n"
        "vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid fixture";
    for (const std::string& suffix : {"vertex", "vertex ", "vertex 0", "vertex 0 1",
                                     "vertex nope 0 0", "vertex 0 nope 0", "vertex 0 0 nope"}) {
        fixture.write(valid + "\n" + suffix);
        require(!read(), "malformed trailing vertex accepted");
        require(error == "bad ASCII STL vertex", "malformed trailing vertex not rejected");
    }
    fixture.write("solid facet vertex nope\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0");
    require(!read(), "malformed first vertex skipped instead of rejected");
    require(error == "bad ASCII STL vertex", "malformed first vertex not rejected");

    fixture.write(valid);
    require(read(), "valid ASCII STL rejected");
    require(pos == std::vector<float>({0, 0, 0, 1, 0, 0, 0, 1, 0}), "ASCII geometry changed");
    require(idx == std::vector<std::uint32_t>({0, 1, 2}), "ASCII indices changed");

    std::string binary(134, '\0');
    binary[80] = 1;
    const float vertices[] = {0, 0, 0, 1, 0, 0, 0, 1, 0};
    std::memcpy(binary.data() + 96, vertices, sizeof(vertices));
    fixture.write(binary);
    require(read(), "valid binary STL rejected");
    require(pos == std::vector<float>(std::begin(vertices), std::end(vertices)), "binary geometry changed");
    require(idx == std::vector<std::uint32_t>({0, 1, 2}), "binary indices changed");
    binary.pop_back();
    fixture.write(binary);
    require(!read(), "truncated binary STL accepted");

    fixture.write("solid facet vertex nan 0 0\nvertex 1 0 0\nvertex 0 1 0");
    require(!read(), "nonfinite ASCII coordinate accepted");
    require(error == "STL contains non-finite vertices", "nonfinite coordinate not diagnosed");
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
