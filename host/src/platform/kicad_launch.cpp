#include "kicad_launch.h"

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <vector>
#elif !defined(__EMSCRIPTEN__)
#include <cerrno>
#include <spawn.h>
#include <sys/wait.h>
#include <thread>
extern char** environ;
#endif

namespace platform {

#if defined(_WIN32)
namespace {
std::wstring utf16(const std::string& text) {
    const int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(),
                                      static_cast<int>(text.size()), nullptr, 0);
    if (n <= 0) return {};
    std::wstring out(static_cast<size_t>(n), L'\0');
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(),
                            static_cast<int>(text.size()), out.data(), n) != n) return {};
    return out;
}

// Quote one argv element using the Windows C runtime command-line rules.
std::wstring quote(const std::wstring& text) {
    std::wstring out = L"\"";
    size_t slashes = 0;
    for (wchar_t c : text) {
        if (c == L'\\') { ++slashes; continue; }
        if (c == L'\"') {
            out.append(slashes * 2 + 1, L'\\');
            out += c;
        } else {
            out.append(slashes, L'\\');
            out += c;
        }
        slashes = 0;
    }
    out.append(slashes * 2, L'\\');
    out += L'\"';
    return out;
}
}  // namespace
#endif

bool launchKicad(const std::string& executable, const std::string& document) {
    if (executable.empty() || document.empty()) return false;
#if defined(__EMSCRIPTEN__)
    return false;
#elif defined(_WIN32)
    const std::wstring exe = utf16(executable), file = utf16(document);
    if (exe.empty() || file.empty()) return false;
    std::wstring command = quote(exe) + L" " + quote(file);
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(exe.c_str(), command.data(), nullptr, nullptr, FALSE,
                        DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, nullptr, nullptr,
                        &startup, &process)) return false;
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return true;
#else
    char* const argv[] = {const_cast<char*>(executable.c_str()),
                          const_cast<char*>(document.c_str()), nullptr};
    posix_spawnattr_t attrs;
    if (posix_spawnattr_init(&attrs) != 0) return false;
#ifdef POSIX_SPAWN_SETSID
    const short flags = POSIX_SPAWN_SETSID;
#else
    const short flags = POSIX_SPAWN_SETPGROUP;
    if (posix_spawnattr_setpgroup(&attrs, 0) != 0) {
        posix_spawnattr_destroy(&attrs);
        return false;
    }
#endif
    if (posix_spawnattr_setflags(&attrs, flags) != 0) {
        posix_spawnattr_destroy(&attrs);
        return false;
    }
    pid_t child = 0;
    const int result = posix_spawnp(&child, executable.c_str(), nullptr, &attrs, argv, environ);
    posix_spawnattr_destroy(&attrs);
    if (result != 0) return false;
    std::thread([child]() {
        int status = 0;
        while (waitpid(child, &status, 0) < 0 && errno == EINTR) {}
    }).detach();
    return true;
#endif
}

}  // namespace platform
