#include "anim.h"

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <mutex>
#include <unordered_map>

namespace ui {
namespace anim {
namespace {

// One frame's redraw request. Set by requestFrames(), read+cleared once per frame by
// app.cpp's idle-throttle block. A plain bool is enough: the flag is written and read on the
// GUI thread only, and "somebody wants the next frame" is not a count.
bool g_wantFrames = false;

using Clock = std::chrono::steady_clock;
Clock::time_point g_frameStart = Clock::now();
Clock::time_point g_ambientDeadline = Clock::time_point::max();
double g_ambientInterval = std::numeric_limits<double>::infinity();

std::mutex g_wakeMutex;
bool g_wakeEnabled = false;

// IDs from closed documents/messages must not accumulate indefinitely.
struct Tween {
    float value = 0.0f;
    bool  seeded = false;
    int lastTouched = 0;
};
std::unordered_map<ImGuiID, Tween>& tweens() {
    static std::unordered_map<ImGuiID, Tween> t;
    return t;
}

inline float clamp01(float t) { return t < 0.0f ? 0.0f : (t > 1.0f ? 1.0f : t); }
void retireTweens() {
    static int lastSweep = 0;
    const int frame = ImGui::GetFrameCount();
    auto& t = tweens();
    if (frame < lastSweep) {
        t.clear();
    } else if (frame - lastSweep < 120) {
        return;
    }
    lastSweep = frame;
    for (auto it = t.begin(); it != t.end();) {
        if (frame - it->second.lastTouched > 600)
            it = t.erase(it);
        else
            ++it;
    }
}


// Settled threshold. Below this the tween snaps to target and stops requesting frames — without
// it an exponential settle never mathematically arrives and the app would never idle again.
constexpr float kSettleEps = 0.0015f;

}  // namespace

float easeOut(float t) {
    t = clamp01(t);
    const float u = 1.0f - t;
    return 1.0f - u * u * u;
}

float easeInOut(float t) {
    t = clamp01(t);
    return t * t * (3.0f - 2.0f * t);
}

float easeOutBack(float t) {
    t = clamp01(t);
    // Overshoot tuned to ~6% — enough to read as a settle, small enough that a numeric label
    // riding the curve never displays a visibly wrong value.
    constexpr float c1 = 1.20f;
    constexpr float c3 = c1 + 1.0f;
    const float u = t - 1.0f;
    return 1.0f + c3 * u * u * u + c1 * u * u;
}

float easeSpring(float t) {
    t = clamp01(t);
    // Critically-damped step response, normalized so easeSpring(1) == 1 exactly. omega picked so
    // the visible settle completes within the nominal duration rather than trailing past it.
    constexpr float omega = 6.0f;
    const float raw  = 1.0f - (1.0f + omega * t) * std::exp(-omega * t);
    constexpr float norm = 1.0f - (1.0f + omega) * 0.00247875218f;  // exp(-6)
    return raw / norm;
}

void requestFrames() { g_wantFrames = true; }

void requestFramesAt(float hz) {
    if (!(hz > 0.0f) || !std::isfinite(hz)) return;
    g_ambientInterval = std::min(g_ambientInterval, 1.0 / static_cast<double>(hz));
}

void setLoopWakeEnabled(bool enabled) {
    std::lock_guard<std::mutex> lk(g_wakeMutex);
    g_wakeEnabled = enabled;
}

void wakeLoop() {
    std::lock_guard<std::mutex> lk(g_wakeMutex);
    if (g_wakeEnabled) glfwPostEmptyEvent();
}

bool consumeFrameRequest() {
    retireTweens();
    const bool w = g_wantFrames;
    g_wantFrames = false;
    return w;
}

double consumeAmbientWait(double maxWaitSeconds) {
    if (std::isfinite(g_ambientInterval)) {
        const auto next = g_frameStart + std::chrono::duration_cast<Clock::duration>(
            std::chrono::duration<double>(g_ambientInterval));
        g_ambientDeadline = std::min(g_ambientDeadline, next);
    } else {
        g_ambientDeadline = Clock::time_point::max();
    }
    g_ambientInterval = std::numeric_limits<double>::infinity();
    if (g_ambientDeadline == Clock::time_point::max()) return maxWaitSeconds;
    const double remaining = std::chrono::duration<double>(g_ambientDeadline - Clock::now()).count();
    return std::clamp(remaining, 0.0, maxWaitSeconds);
}

void beginFrame() {
    g_frameStart = Clock::now();
    if (g_ambientDeadline <= g_frameStart)
        g_ambientDeadline = Clock::time_point::max();
}

float dt() {
    const float d = ImGui::GetIO().DeltaTime;
    if (!(d > 0.0f)) return 0.0f;   // also catches NaN
    return d > 0.05f ? 0.05f : d;
}

float toward(ImGuiID id, float target, float seconds) {
    Tween& tw = tweens()[id];
    tw.lastTouched = ImGui::GetFrameCount();
    if (!tw.seeded) {
        // First sight of this id: adopt the target instantly. Animating from 0 would make every
        // freshly-scrolled-in row visibly slide, which reads as jank, not polish.
        tw.value = target;
        tw.seeded = true;
        return tw.value;
    }
    const float diff = target - tw.value;
    if (std::fabs(diff) <= kSettleEps || !(seconds > 0.0f)) {
        tw.value = target;
        return tw.value;
    }
    // Frame-rate-independent exponential settle: the fraction of the remaining distance covered
    // depends on elapsed TIME, not on how many frames happened to elapse. `seconds` is read as
    // the time to close ~95% of the gap (3 time constants), which matches how the k* duration
    // tokens are described.
    const float tau = seconds / 3.0f;
    const float k   = 1.0f - std::exp(-dt() / tau);
    tw.value += diff * k;
    requestFrames();
    return tw.value;
}

float peek(ImGuiID id, float fallback) {
    auto& t = tweens();
    auto it = t.find(id);
    if (it != t.end()) it->second.lastTouched = ImGui::GetFrameCount();
    return it == t.end() ? fallback : it->second.value;
}

void forget(ImGuiID id) { tweens().erase(id); }

float pulse(float periodSec) {
    requestFramesAt(30.0f);
    if (!(periodSec > 0.0f)) return 0.0f;
    const double t = ImGui::GetTime();
    const float  u = static_cast<float>(std::fmod(t, static_cast<double>(periodSec))) / periodSec;
    // Cosine ping-pong: 0 -> 1 -> 0 with no corner at either end, so a breathing halo has no
    // visible "tick" at the wrap.
    return 0.5f - 0.5f * std::cos(u * 6.28318530718f);
}

float phase(float periodSec) {
    requestFramesAt(30.0f);
    if (!(periodSec > 0.0f)) return 0.0f;
    const double t = ImGui::GetTime();
    return static_cast<float>(std::fmod(t, static_cast<double>(periodSec))) / periodSec;
}

float stagger(double startTime, int index, float seconds) {
    if (!(seconds > 0.0f)) return 1.0f;
    const double now   = ImGui::GetTime();
    const double delay = static_cast<double>(index < 0 ? 0 : index) * kStagger;
    const double local = now - startTime - delay;
    if (local <= 0.0) { requestFrames(); return 0.0f; }
    const float t = static_cast<float>(local) / seconds;
    if (t >= 1.0f) return 1.0f;
    requestFrames();
    return easeOutBack(t);
}

}  // namespace anim
}  // namespace ui
