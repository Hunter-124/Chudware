#pragma once

#include <chrono>
#include <cstdio>
#include <cstdlib>

namespace startup_trace {

// main() supplies the first mark, so every translation unit shares the same
// process-entry reference. Disabled tracing never reads the clock or formats output.
inline void mark(const char* phase) {
    static const bool enabled = [] {
        const char* value = std::getenv("CHUDWARE_STARTUP_TRACE");
        return value && value[0] == '1' && value[1] == '\0';
    }();
    if (!enabled) return;
    using Clock = std::chrono::steady_clock;
    static const auto started = Clock::now();
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        Clock::now() - started).count();
    std::fprintf(stderr, "[startup] +%lld ms %s\n", static_cast<long long>(ms), phase);
}

}  // namespace startup_trace
