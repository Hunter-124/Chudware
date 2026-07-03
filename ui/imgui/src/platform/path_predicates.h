// Platform path predicates — OS branching lives here (ui_style_check rule=ifdef),
// not in panel code. Header-only.
#pragma once
#include <string>

namespace platform {

// True when `p` is an absolute filesystem path on the current OS. Deliberately
// permissive vs std::filesystem::path::is_absolute(): a leading '/' counts as
// absolute on Windows too (worker-returned POSIX-style paths must not get a
// project dir prepended).
inline bool isAbsolutePath(const std::string& p) {
    if (p.empty()) return false;
    if (p[0] == '/') return true;
#if defined(_WIN32)
    if (p.size() > 1 && p[1] == ':') return true;
#endif
    return false;
}

} // namespace platform
