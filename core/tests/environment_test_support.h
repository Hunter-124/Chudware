#pragma once

#include <cstdio>
#include <cstdlib>

namespace chudware::test {

inline int setEnvironment(const char* name, const char* value, bool overwrite = true) {
#if defined(_WIN32)
    if (!overwrite && std::getenv(name) != nullptr) return 0;
    return ::_putenv_s(name, value);
#else
    return ::setenv(name, value, overwrite ? 1 : 0);
#endif
}

inline int unsetEnvironment(const char* name) {
#if defined(_WIN32)
    return ::_putenv_s(name, "");
#else
    return ::unsetenv(name);
#endif
}

inline int setOrUnsetEnvironment(const char* name, const char* value) {
    return value != nullptr ? setEnvironment(name, value) : unsetEnvironment(name);
}

inline constexpr const char* appDataEnvironmentVariable() {
#if defined(_WIN32)
    return "APPDATA";
#else
    return "XDG_DATA_HOME";
#endif
}

inline FILE* openPipe(const char* command, const char* mode) {
#if defined(_WIN32)
    return ::_popen(command, mode);
#else
    return ::popen(command, mode);
#endif
}

inline int closePipe(FILE* stream) {
#if defined(_WIN32)
    return ::_pclose(stream);
#else
    return ::pclose(stream);
#endif
}

}  // namespace chudware::test
