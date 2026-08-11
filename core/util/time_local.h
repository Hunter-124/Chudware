// Re-entrant calendar-time conversion, spelled once.
//
// POSIX has localtime_r(&t, &tm) and gmtime_r(&t, &tm). MSVC has localtime_s
// and gmtime_s, which take the SAME two arguments in the OPPOSITE ORDER and
// return an errno_t rather than a pointer. That argument swap is the trap: the
// call compiles on both platforms if you get it wrong under an #ifdef, and then
// silently converts the wrong thing.
//
// Keep this ABI difference here rather than repeating platform guards at call sites.
//
// Both return false on failure and leave `out` value-initialised, so callers
// can branch instead of inspecting errno or a returned pointer.
#pragma once

#include <ctime>

namespace chudware::util {

inline bool localTime(std::time_t t, std::tm& out) {
    out = std::tm{};
#if defined(_WIN32)
    return ::localtime_s(&out, &t) == 0;
#else
    return ::localtime_r(&t, &out) != nullptr;
#endif
}

inline bool gmTime(std::time_t t, std::tm& out) {
    out = std::tm{};
#if defined(_WIN32)
    return ::gmtime_s(&out, &t) == 0;
#else
    return ::gmtime_r(&t, &out) != nullptr;
#endif
}

}  // namespace chudware::util
