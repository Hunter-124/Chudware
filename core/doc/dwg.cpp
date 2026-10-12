// AutoCAD DWG: native R2000 writing (dwg_write.h) and LibreDWG's `dwg2dxf` for reading the
// 2D entities of a DWG — see dwg.h.

#include "doc/dwg.h"

#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <random>
#include <stdexcept>
#include <thread>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

#include "doc/dwg_write.h"  // build_dwg: the native R2000 encoder
#include "doc/formats.h"    // dxf_entity_count: re-read check of the written DWG

namespace chudware::doc {

namespace fs = std::filesystem;

const char* const kLibredwgInstallHint =
    "Install LibreDWG (GPL-3.0, run as a separate program) for DWG 2D-entity import: put its "
    "`dwg2dxf` on PATH, or run `python3 scripts/provision_libredwg.py` to build the pinned "
    "release into the Chudware tools folder. DWG export does not need it.";

namespace {

#ifdef _WIN32
constexpr const char* kExeSuffix = ".exe";
constexpr char kPathSep = ';';

std::wstring widen(const std::string& s) {
    if (s.empty()) return std::wstring();
    const int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    std::wstring w(static_cast<std::size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), w.data(), n);
    return w;
}

// One argument quoted for CommandLineToArgvW / the MSVC CRT parser.
void append_quoted(std::wstring& cmd, const std::wstring& arg) {
    cmd.push_back(L'"');
    std::size_t slashes = 0;
    for (wchar_t c : arg) {
        if (c == L'\\') { ++slashes; continue; }
        if (c == L'"') cmd.append(slashes * 2 + 1, L'\\');
        else cmd.append(slashes, L'\\');
        slashes = 0;
        cmd.push_back(c);
    }
    cmd.append(slashes * 2, L'\\');
    cmd.push_back(L'"');
}
#else
constexpr const char* kExeSuffix = "";
constexpr char kPathSep = ':';
#endif

// <appdata>/Chudware, matching agent/paths.cpp::appdata_root and services/paths.py.
fs::path chudware_data_root() {
#ifdef _WIN32
    if (const wchar_t* a = _wgetenv(L"APPDATA"); a && *a) return fs::path(a) / "Chudware";
    if (const wchar_t* u = _wgetenv(L"USERPROFILE"); u && *u)
        return fs::path(u) / "AppData" / "Roaming" / "Chudware";
    return fs::path();
#else
    if (const char* x = std::getenv("XDG_DATA_HOME"); x && *x) return fs::path(x) / "Chudware";
    if (const char* h = std::getenv("HOME"); h && *h)
        return fs::path(h) / ".local" / "share" / "Chudware";
    return fs::path();
#endif
}

bool is_executable(const fs::path& p) {
    std::error_code ec;
    if (!fs::is_regular_file(p, ec)) return false;
#ifdef _WIN32
    return true;
#else
    return ::access(p.c_str(), X_OK) == 0;
#endif
}

fs::path unique_sibling(const fs::path& target, const std::string& tag) {
    std::random_device random;
    const auto nonce = (static_cast<std::uint64_t>(random()) << 32) ^ random();
    return target.parent_path() /
           ("." + target.filename().string() + "." + tag + "-" + std::to_string(nonce));
}

}  // namespace

std::string libredwg_tool(const std::string& name) {
    const std::string exe = name + kExeSuffix;
    if (const char* path = std::getenv("PATH"); path && *path) {
        std::string dirs(path);
        std::size_t start = 0;
        while (start <= dirs.size()) {
            std::size_t end = dirs.find(kPathSep, start);
            if (end == std::string::npos) end = dirs.size();
            if (end > start) {
                const fs::path candidate = fs::path(dirs.substr(start, end - start)) / exe;
                if (is_executable(candidate)) return candidate.string();
            }
            start = end + 1;
        }
    }
    const fs::path root = chudware_data_root();
    if (!root.empty()) {
        const fs::path managed = root / "tools" / "libredwg" / "bin" / exe;
        if (is_executable(managed)) return managed.string();
    }
    return std::string();
}

bool run_libredwg(const std::string& name, const std::vector<std::string>& args, int timeout_s) {
    const std::string exe = libredwg_tool(name);
    if (exe.empty()) return false;
#ifdef _WIN32
    std::wstring cmd;
    append_quoted(cmd, widen(exe));
    for (const std::string& a : args) {
        cmd.push_back(L' ');
        append_quoted(cmd, widen(a));
    }
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(widen(exe).c_str(), cmd.data(), nullptr, nullptr, FALSE,
                        CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi))
        return false;
    const DWORD waited = WaitForSingleObject(pi.hProcess, static_cast<DWORD>(timeout_s) * 1000u);
    DWORD code = 1;
    if (waited == WAIT_OBJECT_0) {
        GetExitCodeProcess(pi.hProcess, &code);
    } else {
        TerminateProcess(pi.hProcess, 1);
        WaitForSingleObject(pi.hProcess, INFINITE);
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return waited == WAIT_OBJECT_0 && code == 0;
#else
    std::vector<const char*> argv;
    argv.reserve(args.size() + 2);
    argv.push_back(exe.c_str());
    for (const std::string& a : args) argv.push_back(a.c_str());
    argv.push_back(nullptr);
    const pid_t pid = ::fork();
    if (pid < 0) return false;
    if (pid == 0) {
        const int null_fd = ::open("/dev/null", O_RDWR);
        if (null_fd >= 0) {
            (void)::dup2(null_fd, STDIN_FILENO);
            (void)::dup2(null_fd, STDOUT_FILENO);
            (void)::dup2(null_fd, STDERR_FILENO);
            ::close(null_fd);
        }
        ::execv(argv[0], const_cast<char* const*>(argv.data()));
        ::_exit(127);
    }
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout_s);
    int status = 0;
    for (;;) {
        const pid_t r = ::waitpid(pid, &status, WNOHANG);
        if (r == pid) break;
        if (r < 0 && errno != EINTR) return false;
        if (std::chrono::steady_clock::now() >= deadline) {
            ::kill(pid, SIGKILL);
            while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
            return false;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
#endif
}

std::string write_dwg(const std::string& path, const DxfDocument& doc) {
    const DwgImage image = build_dwg(doc);
    if (image.entities == 0)
        throw std::runtime_error("write_dwg: no geometry to export (all layers empty)");

    const fs::path target = fs::absolute(fs::path(path));
    std::error_code ec;
    if (target.has_parent_path()) fs::create_directories(target.parent_path(), ec);
    const fs::path staged = unique_sibling(target, "dwg");
    fs::path check = unique_sibling(target, "check");
    check += ".dxf";
    auto cleanup = [&] {
        std::error_code e;
        fs::remove(staged, e);
        fs::remove(check, e);
    };
    {
        std::ofstream out(staged, std::ios::binary | std::ios::trunc);
        out.write(image.bytes.data(), static_cast<std::streamsize>(image.bytes.size()));
        out.close();
        if (!out) {
            cleanup();
            throw std::runtime_error("write_dwg: cannot write " + staged.string());
        }
    }
    // Independent re-read when LibreDWG's reader is installed: every entity must come back.
    // Writing itself never depends on it.
    if (!libredwg_tool("dwg2dxf").empty()) {
        std::size_t read_back = 0;
        if (run_libredwg("dwg2dxf", {"-y", "-o", check.string(), staged.string()})) {
            try {
                read_back = dxf_entity_count(check.string());
            } catch (const std::exception&) {
                read_back = 0;
            }
        }
        if (read_back != image.entities) {
            cleanup();
            throw std::runtime_error("write_dwg: the written DWG re-reads (LibreDWG dwg2dxf) with " +
                                     std::to_string(read_back) + " of " +
                                     std::to_string(image.entities) + " entities; refusing " +
                                     target.string());
        }
    }
#ifdef _WIN32
    fs::remove(target, ec);
#endif
    fs::rename(staged, target, ec);
    if (ec) {
        cleanup();
        throw std::runtime_error("write_dwg: cannot move the written DWG into place: " +
                                 ec.message());
    }
    std::error_code e;
    fs::remove(check, e);
    return target.string();
}

}  // namespace chudware::doc
