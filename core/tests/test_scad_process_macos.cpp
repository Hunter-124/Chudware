#define DOCTEST_CONFIG_IMPLEMENT
#include <doctest/doctest.h>
#include "util/scad_process.h"

#ifdef __APPLE__
#include <arpa/inet.h>
#include <netinet/in.h>
#include <mach-o/dyld.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using namespace chudware::util::scad_process;

int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "--version") { std::cout << "probe 1\n"; return 0; }
    if (argc == 5 && std::string(argv[1]) == "--probe") {
        std::ifstream secret(argv[3]);
        std::ofstream project("input.scad", std::ios::app);
        std::ofstream extra("unapproved");
        int sock = ::socket(AF_INET, SOCK_STREAM, 0);
        sockaddr_in address{}; address.sin_family = AF_INET;
        address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        address.sin_port = htons(static_cast<uint16_t>(std::atoi(argv[4])));
        bool connected = sock >= 0 && ::connect(sock, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0;
        pid_t child = ::fork();
        if (!child) ::_exit(0);
        if (child > 0) ::waitpid(child, nullptr, 0);
        std::ofstream out("output.stl", std::ios::binary);
        out << argv[2] << '\n' << (std::getenv("CHUDWARE_TEST_SECRET") ? "leaked" : "cleared")
            << '\n' << (secret ? "read" : "denied") << '\n' << (project ? "wrote" : "denied")
            << '\n' << (extra ? "created" : "denied") << '\n' << (connected ? "connected" : "denied")
            << '\n' << (child >= 0 ? "forked" : "denied");
        if (sock >= 0) ::close(sock);
        return out ? 0 : 2;
    }
    if (argc == 2 && std::string(argv[1]) == "--fail") {
        std::ofstream("output.stl") << "partial";
        std::cerr << std::string(16384, 'x');
        return 7;
    }
    if (argc == 2 && std::string(argv[1]) == "--large") {
        std::ofstream out("output.stl"); out << std::string(32768, 'x');
        return out ? 0 : 3;
    }
    if (argc == 2 && std::string(argv[1]) == "--timeout") for (;;) ::pause();
    doctest::Context context; context.applyCommandLine(argc, argv); return context.run();
}

namespace {
struct Directory {
    fs::path path;
    Directory() { char name[] = "/private/tmp/chudware-scad-mac-test-XXXXXX"; if (char* p = ::mkdtemp(name)) path = p; }
    ~Directory() { std::error_code ec; fs::remove_all(path, ec); }
};
std::string read_file(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
}
fs::path self() {
    uint32_t size = 0; _NSGetExecutablePath(nullptr, &size);
    std::vector<char> name(size);
    if (_NSGetExecutablePath(name.data(), &size)) return {};
    std::error_code ec; return fs::canonical(name.data(), ec);
}
Request request(const fs::path& exe, const fs::path& work) {
    Request r; r.executable = exe.string(); r.work_dir = work.string(); r.output_file = "output.stl"; return r;
}
}

TEST_CASE("macOS SCAD sandbox isolates literal arguments, files, network and children") {
    Directory dir; REQUIRE_FALSE(dir.path.empty());
    auto work = dir.path / "project"; REQUIRE(fs::create_directory(work));
    std::ofstream(work / "input.scad") << "source";
    // The secret is adjacent to the executable, not in the project. Quotes in
    // the binary path also exercise profile string escaping.
    auto exe = dir.path / "probe \"quoted\"";
    REQUIRE(fs::copy_file(self(), exe));
    auto secret = dir.path / "secret"; std::ofstream(secret) << "secret";
    int listener = ::socket(AF_INET, SOCK_STREAM, 0); REQUIRE(listener >= 0);
    sockaddr_in address{}; address.sin_family = AF_INET; address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    REQUIRE(::bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0);
    REQUIRE(::listen(listener, 1) == 0);
    socklen_t length = sizeof(address);
    REQUIRE(::getsockname(listener, reinterpret_cast<sockaddr*>(&address), &length) == 0);
    REQUIRE(::setenv("CHUDWARE_TEST_SECRET", "private", 1) == 0);
    auto r = request(exe, work);
    r.arguments = {"--probe", "$(touch INJECTION);' literal", secret.string(), std::to_string(ntohs(address.sin_port))};
    auto result = run(r); ::unsetenv("CHUDWARE_TEST_SECRET"); ::close(listener);
    if (result.status == Status::sandbox_unavailable) { MESSAGE(result.diagnostics); return; }
    INFO(result.diagnostics);
    REQUIRE(result.status == Status::success);
    CHECK(read_file(work / "output.stl") == "$(touch INJECTION);' literal\ncleared\ndenied\ndenied\ndenied\ndenied\ndenied");
    CHECK(read_file(work / "input.scad") == "source");
    CHECK_FALSE(fs::exists(work / "INJECTION"));
    CHECK_FALSE(fs::exists(work / "unapproved"));
    CHECK(capability(exe.string()).status == Status::success);
}

TEST_CASE("macOS SCAD sandbox preserves prior output on failure, limits and deadline") {
    Directory dir; REQUIRE_FALSE(dir.path.empty());
    std::ofstream(dir.path / "output.stl") << "previous";
    auto r = request(self(), dir.path); r.max_diagnostic_bytes = 256; r.arguments = {"--fail"};
    auto result = run(r);
    if (result.status == Status::sandbox_unavailable) { MESSAGE(result.diagnostics); return; }
    CHECK(result.status == Status::model_failure); CHECK(result.exit_code == 7);
    CHECK(result.diagnostics.size() <= 256); CHECK(read_file(dir.path / "output.stl") == "previous");
    r.arguments = {"--large"}; r.max_output_bytes = 4096;
    result = run(r); CHECK(result.status != Status::success); CHECK(read_file(dir.path / "output.stl") == "previous");
    r.arguments = {"--timeout"}; r.deadline = std::chrono::milliseconds(300);
    result = run(r); CHECK(result.status == Status::timed_out); CHECK(read_file(dir.path / "output.stl") == "previous");
}

TEST_CASE("macOS SCAD rejects symlink inputs and arbitrary resource roots") {
    Directory dir; REQUIRE_FALSE(dir.path.empty());
    auto r = request(self(), dir.path);
    std::error_code ec; fs::create_symlink("/etc/passwd", dir.path / "link", ec); REQUIRE_FALSE(ec);
    CHECK(run(r).status == Status::invalid_request);
    fs::remove(dir.path / "link", ec); REQUIRE_FALSE(ec);
    r.renderer_resource_roots = {dir.path.string()};
    const auto result = run(r);
    CHECK((result.status == Status::invalid_request || result.status == Status::sandbox_unavailable));
    CHECK_FALSE(fs::exists(dir.path / "output.stl"));
}
#else
int main() { return 0; }
#endif
