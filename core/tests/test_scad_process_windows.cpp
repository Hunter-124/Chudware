#define DOCTEST_CONFIG_IMPLEMENT
#include <doctest/doctest.h>
#include "util/scad_process.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <shellapi.h>
#include <sddl.h>
#include <userenv.h>
#include <objbase.h>

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <iostream>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using namespace chudware::util::scad_process;

namespace {
std::string utf8(const std::wstring& s) {
    if (s.empty()) return {};
    const int n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, s.data(),
        static_cast<int>(s.size()), nullptr, 0, nullptr, nullptr);
    if (!n) return {};
    std::string result(static_cast<std::size_t>(n), '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, s.data(),
        static_cast<int>(s.size()), result.data(), n, nullptr, nullptr)) return {};
    return result;
}
std::wstring module_path() {
    std::vector<wchar_t> buffer(32768);
    DWORD n = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    return n && n < buffer.size() ? std::wstring(buffer.data(), n) : std::wstring();
}
void report(const std::string& s) {
    std::ofstream f("output.stl", std::ios::binary);
    f << s;
}
int probe(int argc, wchar_t** argv) {
    if (argc == 2 && std::wstring(argv[1]) == L"--version") {
        std::cout << "sandbox probe 1\n";
        return 0;
    }
    if (argc == 3 && std::wstring(argv[1]) == L"--argv") {
        report(utf8(argv[2]) + (GetEnvironmentVariableW(L"CHUDWARE_TEST_SECRET", nullptr, 0) ? "\nleaked" : "\ncleared"));
        return 0;
    }
    if (argc == 4 && std::wstring(argv[1]) == L"--outside-network") {
        HANDLE f = CreateFileW(argv[2], GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        const bool read_allowed = f != INVALID_HANDLE_VALUE;
        if (read_allowed) CloseHandle(f);
        WSADATA data{};
        bool connected = false;
        if (WSAStartup(MAKEWORD(2, 2), &data) == 0) {
            SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
            if (s != INVALID_SOCKET) {
                u_long nonblocking = 1;
                ioctlsocket(s, FIONBIO, &nonblocking);
                sockaddr_in address{};
                address.sin_family = AF_INET;
                address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
                address.sin_port = htons(static_cast<u_short>(_wtoi(argv[3])));
                if (connect(s, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0) {
                    connected = true;
                } else if (WSAGetLastError() == WSAEWOULDBLOCK) {
                    fd_set writable;
                    FD_ZERO(&writable); FD_SET(s, &writable);
                    timeval timeout{1, 0};
                    if (select(0, nullptr, &writable, nullptr, &timeout) > 0) {
                        int error = 0, length = sizeof(error);
                        connected = getsockopt(s, SOL_SOCKET, SO_ERROR, reinterpret_cast<char*>(&error), &length) == 0 && !error;
                    }
                }
                closesocket(s);
            }
            WSACleanup();
        }
        report(std::string(read_allowed ? "file allowed" : "file denied") +
               (connected ? "\nnetwork allowed" : "\nnetwork denied"));
        return 0;
    }
    if (argc == 2 && std::wstring(argv[1]) == L"--project-access") {
        HANDLE input = CreateFileW(L"input.txt", GENERIC_READ, FILE_SHARE_READ, nullptr,
                                   OPEN_EXISTING, 0, nullptr);
        const bool can_read = input != INVALID_HANDLE_VALUE;
        if (can_read) CloseHandle(input);
        input = CreateFileW(L"input.txt", GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, 0, nullptr);
        const bool can_write = input != INVALID_HANDLE_VALUE;
        if (can_write) CloseHandle(input);
        HANDLE created = CreateFileW(L"extra.txt", GENERIC_WRITE, 0, nullptr, CREATE_NEW, 0, nullptr);
        const bool can_create = created != INVALID_HANDLE_VALUE;
        if (can_create) CloseHandle(created);
        std::string profile = "profile unknown";
        HANDLE token = nullptr;
        if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
            DWORD size = 0;
            GetTokenInformation(token, TokenAppContainerSid, nullptr, 0, &size);
            std::vector<unsigned char> info(size);
            if (size && GetTokenInformation(token, TokenAppContainerSid, info.data(), size, &size)) {
                auto app = reinterpret_cast<TOKEN_APPCONTAINER_INFORMATION*>(info.data());
                LPWSTR sid = nullptr;
                if (app->TokenAppContainer && ConvertSidToStringSidW(app->TokenAppContainer, &sid)) {
                    PWSTR folder = nullptr;
                    if (SUCCEEDED(GetAppContainerFolderPath(sid, &folder)) && folder) {
                        auto path = fs::path(folder) / "probe.txt";
                        HANDLE file = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, 0, nullptr);
                        profile = file == INVALID_HANDLE_VALUE ? "no profile write" : "profile write";
                        if (file != INVALID_HANDLE_VALUE) { CloseHandle(file); DeleteFileW(path.c_str()); }
                        CoTaskMemFree(folder);
                    }
                    LocalFree(sid);
                }
            }
            CloseHandle(token);
        }
        report(std::string(can_read ? "read" : "no read") +
               (can_write ? "\nwrite" : "\nno write") +
               (can_create ? "\ncreate" : "\nno create") + "\n" + profile);
        return 0;
    }
    if (argc == 2 && std::wstring(argv[1]) == L"--failure") {
        report("partial");
        std::cerr << std::string(16384, 'x');
        return 7;
    }
    if (argc == 2 && std::wstring(argv[1]) == L"--oversize") {
        report(std::string(32768, 'x'));
        return 0;
    }
    if (argc == 2 && std::wstring(argv[1]) == L"--sleep-child") {
        Sleep(INFINITE);
        return 0;
    }
    if (argc == 2 && std::wstring(argv[1]) == L"--descendant") {
        const auto exe = module_path();
        std::wstring command = L"\"" + exe + L"\" --sleep-child";
        STARTUPINFOW startup{}; startup.cb = sizeof(startup);
        PROCESS_INFORMATION child{};
        if (CreateProcessW(exe.c_str(), command.data(), nullptr, nullptr, FALSE,
                           CREATE_NO_WINDOW, nullptr, nullptr, &startup, &child)) {
            std::cerr << "child=" << child.dwProcessId << std::endl;
            CloseHandle(child.hThread); CloseHandle(child.hProcess);
        } else {
            std::cerr << "child=denied" << std::endl;
        }
        Sleep(INFINITE);
        return 0;
    }
    return -1;
}
struct Directory {
    fs::path path;
    explicit Directory(bool owner_only = false) {
        wchar_t temp[MAX_PATH + 1]{}, name[MAX_PATH + 1]{};
        if (!GetTempPathW(MAX_PATH, temp) || !GetTempFileNameW(temp, L"cws", 0, name)) return;
        DeleteFileW(name);
        PSECURITY_DESCRIPTOR descriptor = nullptr;
        SECURITY_ATTRIBUTES attributes{sizeof(attributes), nullptr, FALSE};
        if (owner_only) {
            if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    L"D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)",
                    SDDL_REVISION_1, &descriptor, nullptr)) return;
            attributes.lpSecurityDescriptor = descriptor;
        }
        if (CreateDirectoryW(name, owner_only ? &attributes : nullptr)) path = name;
        if (descriptor) LocalFree(descriptor);
    }
    ~Directory() { std::error_code ec; if (!path.empty()) fs::remove_all(path, ec); }
};
std::vector<unsigned char> dacl(const fs::path& path) {
    DWORD size = 0;
    GetFileSecurityW(path.c_str(), DACL_SECURITY_INFORMATION, nullptr, 0, &size);
    if (!size) return {};
    std::vector<unsigned char> descriptor(size);
    if (!GetFileSecurityW(path.c_str(), DACL_SECURITY_INFORMATION,
                          descriptor.data(), size, &size)) return {};
    return descriptor;
}
std::string contents(const fs::path& path) {
    std::ifstream f(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>()};
}
Request request(const fs::path& dir) {
    Request r;
    r.executable = utf8(module_path());
    r.work_dir = utf8(dir.wstring());
    r.output_file = "output.stl";
    return r;
}
}

int main(int argc, char** argv) {
    int count = 0;
    LPWSTR* wide = CommandLineToArgvW(GetCommandLineW(), &count);
    if (wide) {
        int result = probe(count, wide);
        LocalFree(wide);
        if (result >= 0) return result;
    }
    doctest::Context context;
    context.applyCommandLine(argc, argv);
    return context.run();
}

TEST_CASE("Windows SCAD literal argv and cleared environment") {
    Directory dir;
    REQUIRE_FALSE(dir.path.empty());
    REQUIRE(SetEnvironmentVariableW(L"CHUDWARE_TEST_SECRET", L"private"));
    auto r = request(dir.path);
    const std::string literal = "quoted \"value\" spaces & $(not-a-shell) \\\" trailing\\";
    r.arguments = {"--argv", literal};
    auto result = run(r);
    SetEnvironmentVariableW(L"CHUDWARE_TEST_SECRET", nullptr);
    INFO(result.diagnostics);
    REQUIRE(result.status == Status::success);
    CHECK(contents(dir.path / "output.stl") == literal + "\ncleared");
}

TEST_CASE("Windows SCAD denies access outside staged project and network") {
    Directory dir, outside;
    REQUIRE_FALSE(dir.path.empty()); REQUIRE_FALSE(outside.path.empty());
    // A protected owner-only DACL ensures this is a private user file even if the
    // system TEMP directory is configured with package-readable inheritance.
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    REQUIRE(ConvertStringSecurityDescriptorToSecurityDescriptorW(
        L"D:P(A;;FA;;;OW)(A;;FA;;;SY)", SDDL_REVISION_1, &descriptor, nullptr));
    SECURITY_ATTRIBUTES security{sizeof(security), descriptor, FALSE};
    const auto secret = outside.path / "secret.txt";
    HANDLE f = CreateFileW(secret.c_str(), GENERIC_WRITE, 0, &security, CREATE_NEW, 0, nullptr);
    LocalFree(descriptor);
    REQUIRE(f != INVALID_HANDLE_VALUE);
    DWORD written = 0;
    REQUIRE(WriteFile(f, "secret", 6, &written, nullptr));
    CloseHandle(f);

    WSADATA data{};
    REQUIRE(WSAStartup(MAKEWORD(2, 2), &data) == 0);
    SOCKET listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    REQUIRE(listener != INVALID_SOCKET);
    sockaddr_in address{};
    address.sin_family = AF_INET; address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    REQUIRE(bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0);
    REQUIRE(listen(listener, 1) == 0);
    int size = sizeof(address);
    REQUIRE(getsockname(listener, reinterpret_cast<sockaddr*>(&address), &size) == 0);
    auto r = request(dir.path);
    r.arguments = {"--outside-network", utf8(secret.wstring()), std::to_string(ntohs(address.sin_port))};
    auto result = run(r);
    closesocket(listener); WSACleanup();
    INFO(result.diagnostics);
    REQUIRE(result.status == Status::success);
    CHECK(contents(dir.path / "output.stl") == "file denied\nnetwork denied");
}

TEST_CASE("Windows SCAD stages a read-only project with only its output writable") {
    Directory dir(true);
    REQUIRE_FALSE(dir.path.empty());
    const auto original_dacl = dacl(dir.path);
    REQUIRE_FALSE(original_dacl.empty());
    std::ofstream(dir.path / "input.txt", std::ios::binary) << "input";
    auto r = request(dir.path);
    r.arguments = {"--project-access"};
    auto result = run(r);
    INFO(result.diagnostics);
    REQUIRE(result.status == Status::success);
    CHECK(contents(dir.path / "output.stl") == "read\nno write\nno create\nno profile write");
    CHECK(contents(dir.path / "input.txt") == "input");
    CHECK_FALSE(fs::exists(dir.path / "extra.txt"));
    CHECK(dacl(dir.path) == original_dacl);
}

TEST_CASE("Windows SCAD rejects reparse-point dependencies and arbitrary resource roots") {
    Directory dir, outside;
    REQUIRE_FALSE(dir.path.empty()); REQUIRE_FALSE(outside.path.empty());
    std::ofstream(outside.path / "secret.txt") << "secret";
    auto r = request(dir.path);
    r.renderer_resource_roots = {utf8(outside.path.wstring())};
    CHECK(run(r).status == Status::invalid_request);
    r.renderer_resource_roots.clear();
    // Developer mode or the symlink privilege is needed on some Windows versions.
    constexpr DWORD allow_unprivileged_create = 0x2;
    if (CreateSymbolicLinkW((dir.path / "link.txt").c_str(),
                            (outside.path / "secret.txt").c_str(), allow_unprivileged_create)) {
        CHECK(run(r).status == Status::invalid_request);
        CHECK_FALSE(fs::exists(dir.path / "output.stl"));
    } else {
        MESSAGE("Reparse-point probe requires Windows Developer Mode or symlink privilege");
    }
}

TEST_CASE("Windows SCAD failures and output limits preserve the old artifact") {
    Directory dir;
    REQUIRE_FALSE(dir.path.empty());
    std::ofstream(dir.path / "output.stl", std::ios::binary) << "previous";
    auto r = request(dir.path);
    r.max_diagnostic_bytes = 256;
    r.arguments = {"--failure"};
    auto result = run(r);
    INFO(result.diagnostics);
    CHECK(result.status == Status::model_failure);
    CHECK(result.exit_code == 7);
    CHECK(result.diagnostics.size() <= r.max_diagnostic_bytes);
    CHECK(contents(dir.path / "output.stl") == "previous");
    r.arguments = {"--oversize"}; r.max_output_bytes = 4096;
    result = run(r);
    CHECK(result.status != Status::success);
    CHECK(result.diagnostics.size() <= r.max_diagnostic_bytes);
    CHECK(contents(dir.path / "output.stl") == "previous");
}

TEST_CASE("Windows SCAD timeout terminates or refuses descendants") {
    Directory dir;
    REQUIRE_FALSE(dir.path.empty());
    std::ofstream(dir.path / "output.stl", std::ios::binary) << "previous";
    auto r = request(dir.path);
    r.arguments = {"--descendant"}; r.deadline = std::chrono::seconds(3);
    auto result = run(r);
    INFO(result.diagnostics);
    REQUIRE(result.status == Status::timed_out);
    CHECK(contents(dir.path / "output.stl") == "previous");
    const auto pos = result.diagnostics.find("child=");
    REQUIRE(pos != std::string::npos);
    const auto value = result.diagnostics.substr(pos + 6);
    if (value.find("denied") != 0) {
        const DWORD pid = std::stoul(value);
        HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, pid);
        if (process) {
            CHECK(WaitForSingleObject(process, 5000) == WAIT_OBJECT_0);
            CloseHandle(process);
        } else CHECK(GetLastError() == ERROR_INVALID_PARAMETER);
    }
}

TEST_CASE("Windows SCAD rejects ambiguous output paths") {
    Directory dir;
    REQUIRE_FALSE(dir.path.empty());
    auto r = request(dir.path);
    for (const auto* name : {"..", "../escape", "nested\\file", "C:stream", "output.stl:stream",
                             "CON", "NUL.stl", "COM1.txt", "output.stl.", "output.stl "}) {
        r.output_file = name;
        INFO(name);
        CHECK(run(r).status == Status::invalid_request);
    }
}

TEST_CASE("Windows SCAD capability runs a confined version probe") {
    const auto result = capability(utf8(module_path()));
    INFO(result.diagnostics);
    CHECK(result.status == Status::success);
    CHECK(result.diagnostics.find("sandbox probe 1") != std::string::npos);
}
