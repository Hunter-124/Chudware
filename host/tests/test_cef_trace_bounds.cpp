// Exercises the real GL/CEF-free persistent trace sink, not source text.
#include "ui/widgets/cef/cef_trace_file.h"

#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <string>
#include <thread>
#include <vector>
#ifndef _WIN32
#include <sys/wait.h>
#endif

namespace {
namespace fs = std::filesystem;
int failures = 0;
#define CHECK(condition) do { if (!(condition)) { \
    std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); \
    ++failures; } } while (0)

void environment(const char* name, const char* value) {
#ifdef _WIN32
    CHECK(_putenv_s(name, value ? value : "") == 0);
#else
    CHECK(value ? ::setenv(name, value, 1) == 0 : ::unsetenv(name) == 0);
#endif
}
struct TempTree {
    fs::path root;
    TempTree() {
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        root = fs::temp_directory_path() / ("chudware-cef-trace-test-" + std::to_string(stamp));
        std::error_code error;
        CHECK(fs::create_directory(root, error) && !error);
    }
    ~TempTree() { std::error_code ignored; fs::remove_all(root, ignored); }
};
void write(const fs::path& path, const std::string& bytes) {
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    CHECK(bool(out)); out.write(bytes.data(), std::streamsize(bytes.size())); CHECK(bool(out));
}
std::string read(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
}
void test_no_payloads(const fs::path& log) {
    ui::cef::trace_file_line("[RC] page-controlled credential=TRACE_SENTINEL\nFORGED");
    CHECK(!fs::exists(log));
    ui::cef::trace_file_line("[cef-trace] OnLoadEnd https://user:TRACE_SENTINEL@example.test/?token=TRACE_SENTINEL\nFORGED");
    ui::cef::trace_file_line("[wizard] login session='TRACE_SENTINEL'");
    const auto text = read(log);
    CHECK(text == "[cef-trace] OnLoadEnd\n[wizard] login\n");
    CHECK(text.find("TRACE_SENTINEL") == std::string::npos && text.find("FORGED") == std::string::npos);
    ui::cef::trace_file_line("[cef-trace] " + std::string(4096, 'x'));
    const auto bounded = read(log).substr(text.size());
    CHECK(bounded.size() <= ui::cef::kTraceMaxLineBytes && !bounded.empty() && bounded.back() == '\n');
#ifndef _WIN32
    struct stat info{}; CHECK(::stat(log.c_str(), &info) == 0);
    CHECK((info.st_mode & 0777) == 0600 && info.st_nlink == 1);
#endif
}
void burst() {
    const std::string event = "[cef-trace] " + std::string(ui::cef::kTraceMaxLineBytes, 'x');
    for (int i = 0; i < 2048; ++i) ui::cef::trace_file_line(event);
}
void test_threads_and_processes(const fs::path& log) {
    fs::remove(log);
#ifndef _WIN32
    // Fork before creating threads so no inherited locked mutex is possible.
    std::vector<pid_t> children;
    for (int i = 0; i < 3; ++i) {
        const pid_t pid = ::fork(); CHECK(pid >= 0);
        if (pid == 0) { burst(); std::_Exit(0); }
        if (pid > 0) children.push_back(pid);
    }
#endif
    std::vector<std::thread> threads;
    for (int i = 0; i < 3; ++i) threads.emplace_back(burst);
    for (auto& thread : threads) thread.join();
#ifndef _WIN32
    for (const pid_t pid : children) {
        int status = 0; CHECK(::waitpid(pid, &status, 0) == pid);
        CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    }
#endif
    CHECK(fs::exists(log));
    if (!fs::exists(log)) return;
    CHECK(fs::file_size(log) <= ui::cef::kTraceMaxBytes);
    const auto text = read(log);
    size_t start = 0;
    while (start < text.size()) {
        const size_t end = text.find('\n', start);
        CHECK(end != std::string::npos);
        if (end == std::string::npos) break;
        CHECK(end + 1 - start <= ui::cef::kTraceMaxLineBytes);
        CHECK(text.compare(start, std::string_view("[cef-trace] ").size(), "[cef-trace] ") == 0);
        start = end + 1;
    }
    size_t retained = 0;
    for (const auto& entry : fs::directory_iterator(log.parent_path())) {
        CHECK(entry.path() == log);
        if (entry.is_regular_file()) retained += entry.file_size();
    }
    CHECK(retained <= ui::cef::kTraceMaxBytes);
}
void test_exact_cap_and_legacy(const fs::path& log) {
    const std::string event = "[cef-trace] Boundary\n";
    write(log, std::string(ui::cef::kTraceMaxBytes - event.size(), 'x'));
    ui::cef::trace_file_line("[cef-trace] Boundary");
    CHECK(fs::file_size(log) == ui::cef::kTraceMaxBytes);
    const auto full = read(log);
    ui::cef::trace_file_line("[cef-trace] NoSpace");
    CHECK(read(log) == full);
    write(log, std::string(ui::cef::kTraceMaxBytes + 1, 'x'));
    ui::cef::trace_file_line("[cef-trace] Legacy");
    CHECK(fs::file_size(log) == ui::cef::kTraceMaxBytes);
}
void test_writable_aliases(const fs::path& root, const fs::path& log) {
    fs::remove(log);
    const auto victim = root / "victim";
    write(victim, "unchanged");
    std::error_code error;
    fs::create_hard_link(victim, log, error);
    CHECK(!error);
    if (!error) {
        ui::cef::trace_file_line("[cef-trace] Hardlink");
        CHECK(read(victim) == "unchanged" && read(log) == "unchanged");
        fs::remove(log);
    }
    error.clear(); fs::create_symlink(victim, log, error);
#ifndef _WIN32
    CHECK(!error);
#endif
    if (!error) {
        ui::cef::trace_file_line("[cef-trace] Symlink");
        CHECK(read(victim) == "unchanged"); fs::remove(log);
    }
    const auto outside = root / "outside"; fs::create_directory(outside);
    const auto saved = root / "saved"; fs::rename(log.parent_path(), saved);
    error.clear(); fs::create_directory_symlink(outside, log.parent_path(), error);
#ifndef _WIN32
    CHECK(!error);
#endif
    if (!error) {
        ui::cef::trace_file_line("[cef-trace] DirectoryAlias");
        CHECK(!fs::exists(outside / log.filename()));
        fs::remove(log.parent_path());
    }
    fs::rename(saved, log.parent_path());
    CHECK(read(victim) == "unchanged");
}
} // namespace

int main(int argc, char** argv) {
    if (argc != 2) { std::fputs("expected --disabled, --not-opted-in or --enabled\n", stderr); return 2; }
    TempTree tree;
    const auto root = tree.root.u8string();
    environment("XDG_DATA_HOME", root.c_str()); environment("HOME", root.c_str());
    const std::string mode = argv[1];
    if (mode == "--disabled") environment("CHUDWARE_CEF_TRACE", nullptr);
    else if (mode == "--not-opted-in") environment("CHUDWARE_CEF_TRACE", "yes");
    else if (mode == "--enabled") environment("CHUDWARE_CEF_TRACE", "1");
    else return 2;
    const auto log = fs::u8path(ui::cef::trace_file_path());
    if (mode != "--enabled") {
        CHECK(!ui::cef::trace_enabled());
        for (int i = 0; i < 256; ++i) ui::cef::trace_file_line("[cef-trace] Disabled");
        CHECK(!fs::exists(log) && !fs::exists(log.parent_path()));
        fs::create_directory(log.parent_path());
        write(log, "existing trace stays unchanged");
        ui::cef::trace_file_line("[cef-trace] StillDisabled");
        CHECK(read(log) == "existing trace stays unchanged");
    } else {
        CHECK(ui::cef::trace_enabled());
        test_no_payloads(log);
        test_threads_and_processes(log);
        test_exact_cap_and_legacy(log);
        test_writable_aliases(tree.root, log);
    }
    if (failures) { std::fprintf(stderr, "test_cef_trace_bounds: %d failure(s)\n", failures); return 1; }
    std::puts("test_cef_trace_bounds: all checks passed");
    return 0;
}
