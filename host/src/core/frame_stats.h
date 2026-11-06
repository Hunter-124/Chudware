#pragma once

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <string>

namespace core::framestats {

using Clock = std::chrono::steady_clock;
inline constexpr std::size_t kFrameCapacity = 1024;
inline constexpr std::size_t kMaxSections = 48;
inline constexpr std::size_t kSpikeCapacity = 64;

// GUI-thread wall-clock timings, NOT GPU elapsed time or CPU execution counters.
// Section and phase names must have static lifetime. The built-in "other" uses
// one of kMaxSections slots and accounts for uninstrumented work; "event_wait"
// and "swap" are non-work sections. Sections beyond the fixed name limit still
// exclude their time from parents, and their work appears in "other".
void beginFrame() noexcept;
void endFrame() noexcept;
void markWait(double milliseconds) noexcept;
// Call immediately after presentation, even for a zero-duration swap. This also
// records the presented steady-clock timestamp used by the one-second FPS count.
void markSwap(double milliseconds) noexcept;
void setIdle(bool idle) noexcept;  // True only when an actual idle wait occurred.
void setPhase(const char* staticName) noexcept;

class Frame {
public:
    Frame() noexcept { beginFrame(); }
    ~Frame() { endFrame(); }
    Frame(const Frame&) = delete;
    Frame& operator=(const Frame&) = delete;
    Frame(Frame&&) = delete;
    Frame& operator=(Frame&&) = delete;
};

class Scope {
public:
    explicit Scope(const char* staticName) noexcept;
    ~Scope();
    Scope(const Scope&) = delete;
    Scope& operator=(const Scope&) = delete;
    // Idempotent; also closes any still-active child scopes at this boundary.
    // The destructor becomes a no-op, so surrounding C++ locals stay in scope.
    void stop() noexcept;

    Scope(Scope&&) = delete;
    Scope& operator=(Scope&&) = delete;

    // Inclusive wall time, suitable for markWait/markSwap. Recorded section time
    // is exclusive: all nested scopes are subtracted, including repeated names.
    double elapsedMs() const noexcept;

private:
    friend void endFrame() noexcept;
    void finish(Clock::time_point now) noexcept;
    Scope* parent_ = nullptr;
    Clock::time_point started_{};
    double children_ms_ = 0.0;
    double elapsed_ms_ = 0.0;
    std::size_t section_ = kMaxSections;
    bool active_ = false;
};

struct Distribution {
    double p50 = 0.0;
    double p95 = 0.0;
    double p99 = 0.0;
    double max = 0.0;
    double mean = 0.0;
};

struct SectionSummary {
    const char* name = nullptr;
    Distribution ms;
    std::size_t frame_count = 0;
};

struct TopSection {
    const char* name = nullptr;
    double ms = 0.0;
};

struct SpikeRecord {
    std::uint64_t frame_idx = 0;
    double work_ms = 0.0;
    const char* phase = "interactive";
    std::array<TopSection, 3> top_sections{};
    std::size_t top_count = 0;
};

struct Snapshot {
    // Counts cover the whole run; distributions/history cover the retained ring.
    std::uint64_t frame_count = 0;
    std::uint64_t busy_frames = 0;
    std::uint64_t idle_frames = 0;
    std::uint64_t spike_count = 0;
    std::size_t window_frame_count = 0;
    double presented_fps = 0.0;  // Preceding second, at one-millisecond resolution.
    bool idle = false;          // Most recently completed frame actually waited.
    Distribution frame_ms;
    Distribution work_ms;
    Distribution wait_ms;
    Distribution swap_ms;
    std::array<SectionSummary, kMaxSections> sections{};
    std::size_t section_count = 0;
    std::array<float, kFrameCapacity> work_history_ms{};  // Oldest first.
    std::size_t work_count = 0;
    std::array<SpikeRecord, kSpikeCapacity> spikes{};  // Oldest first.
    std::size_t retained_spike_count = 0;
};

// No allocations. FPS/idle update on every call; window quantiles and plot data
// refresh at most four times per second, and are computed only when requested.
// Section distributions summarize each frame that entered that section, using
// its total exclusive time across all invocations in that frame.
const Snapshot& snapshot() noexcept;

// Optional scenario default; call once BEFORE the first frame. A nonempty
// CHUDWARE_FRAMESTATS is always a literal output path and overrides this default.
// Exact full-run samples allocate only when either output path is enabled.
void setOutputPath(std::string path);

// Finalizes any active frame/scopes and exports full-run JSON. Safe before
// std::_Exit; normal stack unwinding and std::exit are handled by ReportOnExit
// and atexit as well. Successful exports are idempotent. Returns true if already
// exported or the write succeeds, false if disabled or export fails. Does not
// throw; errors are reported to stderr. Creates missing output parent directories
// at export time and prints one success-summary line. No frames afterward.
bool flushReport() noexcept;

class ReportOnExit {
public:
    ReportOnExit() noexcept;
    ~ReportOnExit();
    ReportOnExit(const ReportOnExit&) = delete;
    ReportOnExit& operator=(const ReportOnExit&) = delete;
};

}  // namespace core::framestats
