#define DOCTEST_CONFIG_IMPLEMENT
#include <doctest/doctest.h>

#include "util/scad_process.h"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

#ifdef __linux__
#include <unistd.h>
#endif

namespace fs = std::filesystem;
using namespace chudware::util::scad_process;

int main(int argc, char** argv) {
    if (argc == 3 && std::string(argv[1]) == "--scad-argv-probe") {
        std::ofstream out("output.stl", std::ios::binary);
        out << argv[2] << '\n' << (std::getenv("CHUDWARE_TEST_SECRET") ? "leaked" : "cleared");
        return out ? 0 : 2;
    }
#ifdef __linux__
    if (argc == 2 && std::string(argv[1]) == "--scad-fail-probe") {
        std::ofstream("output.stl", std::ios::binary) << "partial";
        std::cerr << "model error";
        return 7;
    }
    if (argc == 2 && std::string(argv[1]) == "--scad-diagnostic-overflow-probe") {
        std::ofstream("output.stl", std::ios::binary) << "incomplete geometry";
        std::cerr << std::string(16384, 'x');
        return 0;
    }
    if (argc == 2 && std::string(argv[1]) == "--scad-limit-probe") {
        std::ofstream out("output.stl", std::ios::binary);
        out << std::string(32768, 'x');
        return out ? 0 : 3;
    }
    if (argc == 2 && std::string(argv[1]) == "--scad-timeout-probe") {
        for (;;) ::pause();
    }
#endif
    doctest::Context context;
    context.applyCommandLine(argc, argv);
    return context.run();
}

#ifdef __linux__
namespace {
struct Directory {
    fs::path path;
    Directory() {
        char name[] = "/tmp/chudware-scad-test-XXXXXX";
        if (char* p = ::mkdtemp(name)) path = p;
    }
    ~Directory() { std::error_code ec; fs::remove_all(path, ec); }
};
Request valid_request(const fs::path& dir) {
    Request r;
    r.executable = "/bin/true";
    r.work_dir = dir.string();
    return r;
}
}

TEST_CASE("SCAD process rejects invalid limits and output paths before execution") {
    Directory dir;
    REQUIRE_FALSE(dir.path.empty());
    auto r = valid_request(dir.path);
    r.deadline = std::chrono::milliseconds(0);
    CHECK(run(r).status == Status::invalid_request);
    r = valid_request(dir.path);
    r.deadline = std::chrono::hours(2);
    CHECK(run(r).status == Status::invalid_request);
    r = valid_request(dir.path);
    r.max_output_bytes = 0;
    CHECK(run(r).status == Status::invalid_request);
    r = valid_request(dir.path);
    r.max_diagnostic_bytes = 0;
    CHECK(run(r).status == Status::invalid_request);
    r = valid_request(dir.path);
    r.output_file = "../outside.stl";
    CHECK(run(r).status == Status::invalid_request);
    r.output_file = "nested/output.stl";
    CHECK(run(r).status == Status::invalid_request);
    r.output_file = "..";
    CHECK(run(r).status == Status::invalid_request);
    r.output_file = "out.stl";
    r.arguments = {std::string("literal\0hidden", 14)};
    CHECK(run(r).status == Status::invalid_request);
}

TEST_CASE("Missing renderer is distinguished from a failed model") {
    Directory dir;
    REQUIRE_FALSE(dir.path.empty());
    auto r = valid_request(dir.path);
    r.executable = "/definitely/nonexistent/chudware-openscad";
    const auto result = run(r);
    CHECK(result.status == Status::dependency_unavailable);
    CHECK_FALSE(result.diagnostics.empty());
    CHECK_FALSE(fs::exists(dir.path / "output.stl"));
}

TEST_CASE("SCAD arguments remain literal and ambient secrets are removed") {
    Directory dir;
    REQUIRE_FALSE(dir.path.empty());
    REQUIRE(::setenv("CHUDWARE_TEST_SECRET", "private", 1) == 0);
    auto r = valid_request(dir.path);
    r.executable = "/proc/self/exe";
    r.arguments = {"--scad-argv-probe", "$(touch INJECTION_MARKER);' quoted value"};
    r.output_file = "output.stl";
    const auto result = run(r);
    ::unsetenv("CHUDWARE_TEST_SECRET");
    if (result.status == Status::sandbox_unavailable) {
        MESSAGE("Bubblewrap unavailable: ", result.diagnostics);
        return;
    }
    REQUIRE(result.status == Status::success);
    std::ifstream in(dir.path / "output.stl", std::ios::binary);
    const std::string content((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    CHECK(content == "$(touch INJECTION_MARKER);' quoted value\ncleared");
    CHECK_FALSE(fs::exists(dir.path / "INJECTION_MARKER"));
}

TEST_CASE("SCAD failure, limits and timeout never publish partial output") {
    Directory dir;
    REQUIRE_FALSE(dir.path.empty());
    std::ofstream(dir.path / "output.stl") << "previous";
    auto r = valid_request(dir.path);
    r.executable = "/proc/self/exe";
    r.output_file = "output.stl";
    r.max_diagnostic_bytes = 256;
    r.arguments = {"--scad-fail-probe"};
    auto result = run(r);
    if (result.status == Status::sandbox_unavailable) {
        MESSAGE("Bubblewrap unavailable: ", result.diagnostics);
        return;
    }
    CHECK(result.status == Status::model_failure);
    CHECK(result.exit_code == 7);
    CHECK(result.diagnostics.size() <= r.max_diagnostic_bytes);
    std::ifstream previous(dir.path / "output.stl");
    std::string content;
    previous >> content;
    CHECK(content == "previous");

    r.arguments = {"--scad-diagnostic-overflow-probe"};
    result = run(r);
    CHECK(result.status == Status::resource_limit);
    CHECK(result.diagnostics.size() <= r.max_diagnostic_bytes);
    std::ifstream after_diagnostics(dir.path / "output.stl");
    after_diagnostics >> content;
    CHECK(content == "previous");

    r.arguments = {"--scad-limit-probe"};
    r.max_output_bytes = 4096;
    result = run(r);
    CHECK(result.status != Status::success);
    std::ifstream after_limit(dir.path / "output.stl");
    after_limit >> content;
    CHECK(content == "previous");

    r.arguments = {"--scad-timeout-probe"};
    r.deadline = std::chrono::milliseconds(300);
    result = run(r);
    CHECK(result.status == Status::timed_out);
    std::ifstream after_timeout(dir.path / "output.stl");
    after_timeout >> content;
    CHECK(content == "previous");
}

TEST_CASE("SCAD renderer resource roots cannot expose arbitrary directories") {
    Directory dir;
    REQUIRE_FALSE(dir.path.empty());
    auto r = valid_request(dir.path);
    r.renderer_resource_roots = {dir.path.string()};
    const auto result = run(r);
    CHECK((result.status == Status::invalid_request || result.status == Status::sandbox_unavailable));
    CHECK_FALSE(result.diagnostics.empty());
}

TEST_CASE("SCAD project refuses symlink dependencies instead of following them") {
    Directory dir;
    REQUIRE_FALSE(dir.path.empty());
    std::error_code ec;
    fs::create_symlink("/etc/passwd", dir.path / "secret", ec);
    REQUIRE_FALSE(ec);
    auto r = valid_request(dir.path);
    r.output_file = "output.stl";
    const auto result = run(r);
    // A missing bubblewrap is also an explicit fail-closed result.
    CHECK((result.status == Status::invalid_request || result.status == Status::sandbox_unavailable));
    CHECK_FALSE(fs::exists(dir.path / "output.stl"));
}
#elif !defined(_WIN32) && !defined(__APPLE__)
TEST_CASE("Unsupported platforms fail closed") {
    Request r;
    const auto result = run(r);
    CHECK(result.status == Status::sandbox_unavailable);
    CHECK_FALSE(result.diagnostics.empty());
}
#endif
