#include "frame_stats.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <filesystem>
#include <fstream>
#include <map>
#include <system_error>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

namespace core::framestats {
namespace {

using json = nlohmann::json;

struct FrameRecord {
    std::uint64_t frame_idx = 0;
    double frame_ms = 0.0;
    double work_ms = 0.0;
    double wait_ms = 0.0;
    double swap_ms = 0.0;
    std::array<double, kMaxSections> sections{};
    std::uint64_t section_mask = 0;
    const char* phase = "interactive";
    Clock::time_point presented_at{};
    bool presented = false;
    bool idle = false;
    bool spike = false;
};

struct PresentationBucket {
    std::int64_t millisecond = 0;
    std::uint64_t count = 0;
};

struct State {
    State() noexcept {
        const char* path = std::getenv("CHUDWARE_FRAMESTATS");
        collect_full_run = path && path[0];
        section_names[0] = "other";
        work_sections[0] = true;
    }

    std::array<FrameRecord, kFrameCapacity> frames{};
    std::size_t next_frame = 0;
    std::size_t retained_frames = 0;
    std::array<double, kFrameCapacity> sorted_work{};
    std::array<SpikeRecord, kSpikeCapacity> spikes{};
    std::size_t next_spike = 0;
    std::size_t retained_spikes = 0;
    // Millisecond buckets count every swap, even above 1024 FPS or when a
    // capture performs several presentations inside a single frame record.
    std::array<PresentationBucket, 1024> presentations{};
    std::array<const char*, kMaxSections> section_names{};
    std::array<bool, kMaxSections> work_sections{};
    std::size_t section_count = 1;
    std::uint64_t frame_count = 0;
    std::uint64_t idle_frames = 0;
    std::uint64_t spike_count = 0;
    const char* phase = "interactive";
    FrameRecord current;
    Clock::time_point started{};
    Scope* active_scope = nullptr;
    bool active = false;
    bool collect_full_run = false;
    bool collection_failed = false;
    bool exported = false;
    std::string output_path;
    std::vector<FrameRecord> all_frames;
    Snapshot view;
    Clock::time_point view_updated{};
    std::uint64_t view_frame_count = 0;
    bool have_view = false;
};

void exportAtExit() noexcept { flushReport(); }

State& state() noexcept {
    static State instance;
    // Registered after State's destructor, so export runs before storage dies.
    static const bool registered = [] {
        if (std::atexit(exportAtExit) == 0) return true;
        std::fprintf(stderr, "[framestats] Could not register atexit export\n");
        return false;
    }();
    (void)registered;
    return instance;
}

double milliseconds(Clock::duration duration) noexcept {
    return std::chrono::duration<double, std::milli>(duration).count();
}

double nonnegative(double value) noexcept {
    return std::isfinite(value) && value > 0.0 ? value : 0.0;
}

std::size_t sectionIndex(State& s, const char* name) noexcept {
    if (!name || !name[0]) return kMaxSections;
    for (std::size_t i = 0; i < s.section_count; ++i) {
        if (s.section_names[i] == name || std::strcmp(s.section_names[i], name) == 0)
            return i;
    }
    if (s.section_count == kMaxSections) return kMaxSections;
    const std::size_t index = s.section_count++;
    s.section_names[index] = name;
    s.work_sections[index] = std::strcmp(name, "event_wait") != 0 &&
                             std::strcmp(name, "swap") != 0;
    return index;
}

const FrameRecord& retainedFrame(const State& s, std::size_t index) noexcept {
    const std::size_t oldest =
        (s.next_frame + kFrameCapacity - s.retained_frames) % kFrameCapacity;
    return s.frames[(oldest + index) % kFrameCapacity];
}

double rollingMedian(const State& s) noexcept {
    const std::size_t count = s.retained_frames;
    if (count == 0) return 0.0;
    if (count % 2) return s.sorted_work[count / 2];
    return (s.sorted_work[count / 2 - 1] + s.sorted_work[count / 2]) * 0.5;
}

void updateSortedWork(State& s, double value) noexcept {
    auto first = s.sorted_work.begin();
    auto last = first + s.retained_frames;
    auto insertion = std::lower_bound(first, last, value);
    if (s.retained_frames < kFrameCapacity) {
        std::move_backward(insertion, last, last + 1);
        *insertion = value;
        return;
    }
    const double removed = s.frames[s.next_frame].work_ms;
    auto removal = std::lower_bound(first, last, removed);
    // Replace the evicted value by shifting only the interval between it and
    // the new value. The rolling median never sorts the frame ring.
    if (insertion > removal) {
        std::move(removal + 1, insertion, removal);
        *(insertion - 1) = value;
    } else {
        std::move_backward(insertion, removal, removal + 1);
        *insertion = value;
    }
}

SpikeRecord spikeRecord(const State& s, const FrameRecord& frame) noexcept {
    SpikeRecord result;
    result.frame_idx = frame.frame_idx;
    result.work_ms = frame.work_ms;
    result.phase = frame.phase;
    for (std::size_t section = 0; section < s.section_count; ++section) {
        if (!s.work_sections[section]) continue;
        const double ms = frame.sections[section];
        if (ms <= 0.0) continue;
        for (std::size_t rank = 0; rank < result.top_sections.size(); ++rank) {
            if (ms <= result.top_sections[rank].ms) continue;
            for (std::size_t move = result.top_sections.size() - 1; move > rank; --move)
                result.top_sections[move] = result.top_sections[move - 1];
            result.top_sections[rank] = {s.section_names[section], ms};
            result.top_count = std::min(result.top_count + 1, result.top_sections.size());
            break;
        }
    }
    return result;
}

double quantile(const double* values, std::size_t count, double fraction) noexcept {
    if (count == 0) return 0.0;
    const double position = fraction * static_cast<double>(count - 1);
    const std::size_t lower = static_cast<std::size_t>(position);
    const std::size_t upper = std::min(lower + 1, count - 1);
    return values[lower] + (values[upper] - values[lower]) * (position - lower);
}

Distribution distribution(double* values, std::size_t count) noexcept {
    Distribution result;
    if (count == 0) return result;
    double total = 0.0;
    for (std::size_t i = 0; i < count; ++i) total += values[i];
    std::sort(values, values + count);
    result.p50 = quantile(values, count, 0.50);
    result.p95 = quantile(values, count, 0.95);
    result.p99 = quantile(values, count, 0.99);
    result.max = values[count - 1];
    result.mean = total / static_cast<double>(count);
    return result;
}

void refreshView(State& s, Clock::time_point now) noexcept {
    Snapshot& view = s.view;
    view.window_frame_count = s.retained_frames;
    view.work_count = s.retained_frames;
    std::array<double, kFrameCapacity> scratch{};
    const auto metric = [&](double FrameRecord::*member) {
        for (std::size_t i = 0; i < s.retained_frames; ++i)
            scratch[i] = retainedFrame(s, i).*member;
        return distribution(scratch.data(), s.retained_frames);
    };
    view.frame_ms = metric(&FrameRecord::frame_ms);
    view.work_ms = metric(&FrameRecord::work_ms);
    view.wait_ms = metric(&FrameRecord::wait_ms);
    view.swap_ms = metric(&FrameRecord::swap_ms);
    for (std::size_t i = 0; i < s.retained_frames; ++i)
        view.work_history_ms[i] = static_cast<float>(retainedFrame(s, i).work_ms);
    view.section_count = 0;
    for (std::size_t section = 0; section < s.section_count; ++section) {
        std::size_t count = 0;
        for (std::size_t i = 0; i < s.retained_frames; ++i) {
            const FrameRecord& frame = retainedFrame(s, i);
            if (frame.section_mask & (std::uint64_t{1} << section))
                scratch[count++] = frame.sections[section];
        }
        if (count == 0) continue;
        SectionSummary& summary = view.sections[view.section_count++];
        summary.name = s.section_names[section];
        summary.ms = distribution(scratch.data(), count);
        summary.frame_count = count;
    }
    view.retained_spike_count = s.retained_spikes;
    const std::size_t oldest_spike =
        (s.next_spike + kSpikeCapacity - s.retained_spikes) % kSpikeCapacity;
    for (std::size_t i = 0; i < s.retained_spikes; ++i)
        view.spikes[i] = s.spikes[(oldest_spike + i) % kSpikeCapacity];
    s.view_frame_count = s.frame_count;
    s.view_updated = now;
    s.have_view = true;
}

json distributionJson(const Distribution& value) {
    return {{"p50", value.p50}, {"p95", value.p95}, {"p99", value.p99},
            {"max", value.max}, {"mean", value.mean}};
}

json spikeJson(const SpikeRecord& spike) {
    json top = json::array();
    for (std::size_t i = 0; i < spike.top_count; ++i)
        top.push_back({{"name", spike.top_sections[i].name}, {"ms", spike.top_sections[i].ms}});
    return {{"frame_idx", spike.frame_idx}, {"work_ms", spike.work_ms},
            {"phase", spike.phase}, {"top_sections", std::move(top)}};
}

json summarize(const State& s, const std::vector<const FrameRecord*>& frames) {
    std::uint64_t idle = 0;
    std::uint64_t spikes = 0;
    for (const FrameRecord* frame : frames) {
        if (frame->idle) ++idle;
        if (frame->spike) ++spikes;
    }
    json result = {{"frame_count", frames.size()}, {"busy_frames", frames.size() - idle},
                   {"idle_frames", idle}, {"spike_count", spikes}};
    std::vector<double> values;
    values.reserve(frames.size());
    const auto metric = [&](const char* name, double FrameRecord::*member) {
        values.clear();
        for (const FrameRecord* frame : frames) values.push_back(frame->*member);
        result[name] = distributionJson(distribution(values.data(), values.size()));
    };
    metric("frame_ms", &FrameRecord::frame_ms);
    metric("work_ms", &FrameRecord::work_ms);
    metric("wait_ms", &FrameRecord::wait_ms);
    metric("swap_ms", &FrameRecord::swap_ms);
    json sections = json::object();
    for (std::size_t section = 0; section < s.section_count; ++section) {
        values.clear();
        for (const FrameRecord* frame : frames) {
            if (frame->section_mask & (std::uint64_t{1} << section))
                values.push_back(frame->sections[section]);
        }
        if (values.empty()) continue;
        json summary = distributionJson(distribution(values.data(), values.size()));
        summary["frame_count"] = values.size();
        sections[s.section_names[section]] = std::move(summary);
    }
    result["sections"] = std::move(sections);
    std::array<const FrameRecord*, kSpikeCapacity> recent{};
    std::size_t count = 0;
    for (auto it = frames.rbegin(); it != frames.rend() && count < recent.size(); ++it) {
        if ((*it)->spike) recent[count++] = *it;
    }
    json recent_spikes = json::array();
    while (count > 0) recent_spikes.push_back(spikeJson(spikeRecord(s, *recent[--count])));
    result["spikes"] = std::move(recent_spikes);
    return result;
}

json reportJson(const State& s) {
    std::vector<const FrameRecord*> all;
    all.reserve(s.all_frames.size());
    std::map<std::string, std::vector<const FrameRecord*>> phases;
    for (const FrameRecord& frame : s.all_frames) {
        all.push_back(&frame);
        phases[frame.phase].push_back(&frame);
    }
    json report = summarize(s, all);
    json phase_reports = json::object();
    for (const auto& phase : phases) phase_reports[phase.first] = summarize(s, phase.second);
    report["phases"] = std::move(phase_reports);
    return report;
}

}  // namespace

void beginFrame() noexcept {
    State& s = state();
    if (s.exported) return;
    if (s.active) endFrame();
    s.current = FrameRecord{};
    s.current.frame_idx = s.frame_count;
    s.current.phase = s.phase;
    s.started = Clock::now();
    s.active = true;
}

void endFrame() noexcept {
    State& s = state();
    if (!s.active) return;
    const auto now = Clock::now();
    while (s.active_scope) s.active_scope->finish(now);
    FrameRecord& frame = s.current;
    frame.frame_ms = milliseconds(now - s.started);
    frame.wait_ms = std::min(frame.wait_ms, frame.frame_ms);
    frame.swap_ms = std::min(frame.swap_ms, frame.frame_ms - frame.wait_ms);
    frame.work_ms = frame.frame_ms - frame.wait_ms - frame.swap_ms;
    double accounted_work = 0.0;
    for (std::size_t i = 0; i < s.section_count; ++i) {
        if (s.work_sections[i]) accounted_work += frame.sections[i];
    }
    frame.sections[0] += std::max(0.0, frame.work_ms - accounted_work);
    frame.section_mask |= std::uint64_t{1};
    frame.spike = frame.work_ms > std::max(8.0, rollingMedian(s) * 2.0);
    if (frame.spike) {
        s.spikes[s.next_spike] = spikeRecord(s, frame);
        s.next_spike = (s.next_spike + 1) % kSpikeCapacity;
        s.retained_spikes = std::min(s.retained_spikes + 1, kSpikeCapacity);
        ++s.spike_count;
    }
    updateSortedWork(s, frame.work_ms);
    s.frames[s.next_frame] = frame;
    s.next_frame = (s.next_frame + 1) % kFrameCapacity;
    s.retained_frames = std::min(s.retained_frames + 1, kFrameCapacity);
    ++s.frame_count;
    if (frame.idle) ++s.idle_frames;
    s.active = false;
    if (s.collect_full_run && !s.collection_failed) {
        try {
            s.all_frames.push_back(frame);
        } catch (...) {
            s.collection_failed = true;
            std::fprintf(stderr, "[framestats] Could not retain exact full-run samples\n");
        }
    }
}

void markWait(double ms) noexcept {
    State& s = state();
    if (s.active) s.current.wait_ms += nonnegative(ms);
}

void markSwap(double ms) noexcept {
    State& s = state();
    if (!s.active) return;
    s.current.swap_ms += nonnegative(ms);
    s.current.presented_at = Clock::now();
    s.current.presented = true;
    const auto tick = std::chrono::duration_cast<std::chrono::milliseconds>(
                          s.current.presented_at.time_since_epoch()).count();
    PresentationBucket& bucket =
        s.presentations[static_cast<std::uint64_t>(tick) % s.presentations.size()];
    if (bucket.millisecond != tick) {
        bucket.millisecond = tick;
        bucket.count = 0;
    }
    ++bucket.count;
}

void setIdle(bool idle) noexcept {
    State& s = state();
    if (s.active) s.current.idle = s.current.idle || idle;
}

void setPhase(const char* name) noexcept {
    if (!name || !name[0]) return;
    State& s = state();
    s.phase = name;
    if (s.active) s.current.phase = name;
}

Scope::Scope(const char* name) noexcept {
    State& s = state();
    if (!s.active) return;
    section_ = sectionIndex(s, name);
    parent_ = s.active_scope;
    started_ = Clock::now();
    active_ = true;
    s.active_scope = this;
}

Scope::~Scope() { stop(); }

void Scope::finish(Clock::time_point now) noexcept {
    State& s = state();
    elapsed_ms_ = milliseconds(now - started_);
    if (section_ < kMaxSections) {
        s.current.sections[section_] += std::max(0.0, elapsed_ms_ - children_ms_);
        s.current.section_mask |= std::uint64_t{1} << section_;
    }
    if (parent_) parent_->children_ms_ += elapsed_ms_;
    s.active_scope = parent_;
    active_ = false;
}

void Scope::stop() noexcept {
    if (!active_) return;
    State& s = state();
    const auto now = Clock::now();
    while (s.active_scope != this) s.active_scope->finish(now);
    finish(now);
}

double Scope::elapsedMs() const noexcept {
    return active_ ? milliseconds(Clock::now() - started_) : elapsed_ms_;
}

const Snapshot& snapshot() noexcept {
    State& s = state();
    const auto now = Clock::now();
    if (!s.have_view || (s.view_frame_count != s.frame_count &&
                        now - s.view_updated >= std::chrono::milliseconds(250)))
        refreshView(s, now);
    Snapshot& view = s.view;
    view.frame_count = s.frame_count;
    view.idle_frames = s.idle_frames;
    view.busy_frames = s.frame_count - s.idle_frames;
    view.spike_count = s.spike_count;
    view.idle = s.retained_frames && retainedFrame(s, s.retained_frames - 1).idle;
    const auto tick = std::chrono::duration_cast<std::chrono::milliseconds>(
                          now.time_since_epoch()).count();
    std::uint64_t presented = 0;
    for (const PresentationBucket& bucket : s.presentations) {
        if (bucket.millisecond > tick - 1000 && bucket.millisecond <= tick)
            presented += bucket.count;
    }
    view.presented_fps = static_cast<double>(presented);
    return view;
}

void setOutputPath(std::string path) {
    State& s = state();
    s.output_path = std::move(path);
    const char* environment = std::getenv("CHUDWARE_FRAMESTATS");
    s.collect_full_run = !s.output_path.empty() || (environment && environment[0]);
}

bool flushReport() noexcept {
    State& s = state();
    if (s.exported) return true;
    endFrame();
    try {
        const char* environment = std::getenv("CHUDWARE_FRAMESTATS");
        const std::string path = environment && environment[0] ? environment : s.output_path;
        if (path.empty()) return false;
        if (s.collection_failed || s.all_frames.size() != s.frame_count) {
            std::fprintf(stderr,
                         "[framestats] Cannot export exact full-run statistics; enable the "
                         "output path before the first frame and retain all samples\n");
            return false;
        }
        const json report = reportJson(s);
        std::string text = report.dump(2);
        text.push_back('\n');
        const std::filesystem::path parent = std::filesystem::path(path).parent_path();
        if (!parent.empty()) {
            std::error_code error;
            std::filesystem::create_directories(parent, error);
            if (error) {
                std::fprintf(stderr, "[framestats] Cannot create report directory for %s: %s\n",
                             path.c_str(), error.message().c_str());
                return false;
            }
        }
        std::ofstream output(path, std::ios::binary | std::ios::trunc);
        if (!output) {
            std::fprintf(stderr, "[framestats] Cannot open report: %s\n", path.c_str());
            return false;
        }
        output.write(text.data(), static_cast<std::streamsize>(text.size()));
        output.close();
        if (!output) {
            std::fprintf(stderr, "[framestats] Cannot write report: %s\n", path.c_str());
            return false;
        }
        const json& frame = report.at("frame_ms");
        const json& work = report.at("work_ms");
        const json& wait = report.at("wait_ms");
        const json& swap = report.at("swap_ms");
        std::fprintf(
            stderr,
            "[framestats] frames=%llu busy=%llu idle=%llu spikes=%llu "
            "ms(p50/p99/max) frame=%.3f/%.3f/%.3f work=%.3f/%.3f/%.3f "
            "wait=%.3f/%.3f/%.3f swap=%.3f/%.3f/%.3f output=%s\n",
            static_cast<unsigned long long>(s.frame_count),
            static_cast<unsigned long long>(s.frame_count - s.idle_frames),
            static_cast<unsigned long long>(s.idle_frames),
            static_cast<unsigned long long>(s.spike_count),
            frame.at("p50").get<double>(), frame.at("p99").get<double>(),
            frame.at("max").get<double>(), work.at("p50").get<double>(),
            work.at("p99").get<double>(), work.at("max").get<double>(),
            wait.at("p50").get<double>(), wait.at("p99").get<double>(),
            wait.at("max").get<double>(), swap.at("p50").get<double>(),
            swap.at("p99").get<double>(), swap.at("max").get<double>(), path.c_str());
        s.exported = true;
        return true;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "[framestats] Export failed: %s\n", error.what());
    } catch (...) {
        std::fprintf(stderr, "[framestats] Export failed\n");
    }
    return false;
}

ReportOnExit::ReportOnExit() noexcept { (void)state(); }
ReportOnExit::~ReportOnExit() { flushReport(); }

}  // namespace core::framestats
