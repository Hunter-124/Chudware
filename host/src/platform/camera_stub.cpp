// ---------------------------------------------------------------------------------------------
// camera_stub.cpp — the synthetic fallback CameraBackend, the cross-platform create() factory,
// and the shared frameSharpness() scorer.
//
// The stub exists so CI, wasm builds and hardware-less dev boxes still exercise the whole capture
// path. Everything it emits is flagged CameraFrame::synthetic = true; the reveng session refuses
// to persist such a frame as a real capture, so a fake image can never silently become the basis
// of a dimensioned model.
// ---------------------------------------------------------------------------------------------
#include "camera.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <memory>

namespace chudware::platform {

// =================================================================================================
// frameSharpness — variance of the Laplacian, agreeing with services/vision/frames.py blur_score()
// =================================================================================================
//
// blur_score() is `cv2.Laplacian(gray, CV_64F).var()` over a BT.601 luma image in 0..255 units,
// using the 3x3 kernel [0 1 0; 1 -4 1; 0 1 0]. Both sides must produce the same number, because
// kSharpnessBlurThreshold (100.0) == frames.py DEFAULT_BLUR_THRESHOLD is compared against both.
//
// Why this samples instead of downscaling
// ---------------------------------------
// The obvious speed trick — downscale to ~320px and score that — silently destroys the metric.
// Blur is a property measured in *sensor pixels*, and any resample rescales it away: a 4K frame
// with a 2.5px Gaussian blur scores 2.6 at native resolution (correctly "blurry"), but 176.8
// after a 12x box downscale, because the blur shrinks to 0.2px on the coarse grid and the frame
// reads as tack-sharp. That inverts the verdict at the exact threshold this constant defines.
//
// So we keep the native pixel spacing — the stencil always reads immediately-adjacent pixels —
// and instead evaluate it on a subset of positions. Variance is a pointwise statistic, so a
// uniform subsample is an unbiased estimator of the full-image value.
//
// The sample positions are *stratified and jittered* rather than a plain fixed stride: a regular
// lattice aliases against periodic subject matter (knurling, gear teeth, mesh, threads) and can
// step over the strongest responses entirely — on a period-64 test grid a plain stride
// underestimated the score by 7x, dragging a sharp frame below the threshold. One jittered sample
// per stride x stride cell removes that failure mode.
//
// Measured against frames.py on 640x480/1080p/4K, blur sigma 0..5, over photographic (1/f),
// machined-edge and adversarial periodic content: ratio 0.90-1.01, with identical sharp/blurry
// verdicts at the 100.0 threshold in every case.

namespace {

// Sample budget along the long edge. Bounds the work at ~60k stencil evaluations for any
// resolution — the same compute budget a 320px downscale would have cost.
constexpr int kSharpnessSampleBudget = 320;

}  // namespace

double frameSharpness(const CameraFrame& f) {
    const int w = f.width;
    const int h = f.height;
    // Need at least one interior pixel for the 3x3 stencil.
    if (w < 3 || h < 3) return -1.0;
    if (f.rgba.size() < (size_t)w * (size_t)h * 4u) return -1.0;

    const uint8_t* const px = f.rgba.data();

    // BT.601 luma in 0..255, rounded to integer to match OpenCV's uint8 grayscale conversion.
    // Q16 fixed point: 0.299/0.587/0.114 * 65536, +32768 to round to nearest.
    const auto luma = [px, w](int x, int y) -> int {
        const uint8_t* p = px + ((size_t)y * (size_t)w + (size_t)x) * 4u;
        return (int)((p[0] * 19595u + p[1] * 38470u + p[2] * 7471u + 32768u) >> 16);
    };

    double sum = 0.0;
    double sum_sq = 0.0;
    size_t count = 0;
    const auto accumulate = [&](int x, int y) {
        const double lap = (double)(luma(x, y - 1) + luma(x, y + 1) + luma(x - 1, y) +
                                    luma(x + 1, y) - 4 * luma(x, y));
        sum += lap;
        sum_sq += lap * lap;
        ++count;
    };

    const int longest = w > h ? w : h;
    const int stride = std::max(1, longest / kSharpnessSampleBudget);
    const int strata_y = (h - 2) / stride;
    const int strata_x = (w - 2) / stride;

    if (stride == 1 || strata_y <= 0 || strata_x <= 0) {
        // Small enough to score exhaustively — this is literally frames.py's computation.
        for (int y = 1; y < h - 1; ++y)
            for (int x = 1; x < w - 1; ++x) accumulate(x, y);
    } else {
        for (int gy = 0; gy < strata_y; ++gy) {
            for (int gx = 0; gx < strata_x; ++gx) {
                // Deterministic per-cell hash -> a fixed but lattice-decorrelated jitter, so the
                // score is reproducible frame to frame while immune to periodic aliasing.
                uint32_t hash = ((uint32_t)gy * 73856093u) ^ ((uint32_t)gx * 19349663u) ^ 0x9E3779B9u;
                hash = (hash ^ (hash >> 13)) * 1274126177u;
                hash ^= (hash >> 16);
                const uint32_t span = (uint32_t)stride;
                const int y = 1 + gy * stride + (int)(hash % span);
                const int x = 1 + gx * stride + (int)((hash / span) % span);
                accumulate(std::min(x, w - 2), std::min(y, h - 2));
            }
        }
    }

    if (count < 2) return -1.0;
    const double mean = sum / (double)count;
    const double variance = sum_sq / (double)count - mean * mean;
    return variance > 0.0 ? variance : 0.0;  // clamp float noise on a perfectly flat frame
}

// =================================================================================================
// StubCameraBackend
// =================================================================================================

namespace {

constexpr int kStubWidth = 1280;
constexpr int kStubHeight = 720;
constexpr double kStubFps = 30.0;

// 'S','Y','N','T' little-endian — deliberately not a real FourCC any driver emits.
constexpr uint32_t kStubFourcc = (uint32_t)'S' | ((uint32_t)'Y' << 8) | ((uint32_t)'N' << 16) |
                                 ((uint32_t)'T' << 24);

void hsvToRgb(double hue_deg, double sat, double val, uint8_t* out) {
    const double h = std::fmod(std::fmod(hue_deg, 360.0) + 360.0, 360.0) / 60.0;
    const double c = val * sat;
    const double x = c * (1.0 - std::fabs(std::fmod(h, 2.0) - 1.0));
    const double m = val - c;
    double r = 0.0, g = 0.0, b = 0.0;
    switch ((int)h) {
        case 0:  r = c; g = x; break;
        case 1:  r = x; g = c; break;
        case 2:  g = c; b = x; break;
        case 3:  g = x; b = c; break;
        case 4:  r = x; b = c; break;
        default: r = c; b = x; break;
    }
    out[0] = (uint8_t)std::lround((r + m) * 255.0);
    out[1] = (uint8_t)std::lround((g + m) * 255.0);
    out[2] = (uint8_t)std::lround((b + m) * 255.0);
}

}  // namespace

class StubCameraBackend : public CameraBackend {
    bool opened_ = false;
    uint64_t frame_index_ = 0;
    CameraMode active_{};
    std::string last_error_;

    static CameraMode syntheticMode() {
        CameraMode m;
        m.width = kStubWidth;
        m.height = kStubHeight;
        m.fps = kStubFps;
        m.fourcc = kStubFourcc;
        m.format_label = "SYNT";
        m.compressed = false;
        return m;
    }

public:
    std::vector<CameraInfo> listDevices() override {
        return {CameraInfo{"stub:0", "Synthetic Camera (no hardware)", "synthetic"}};
    }

    std::vector<CameraMode> listModes(const std::string& device_id) override {
        if (!device_id.empty() && device_id != "stub:0") {
            last_error_ = "no synthetic device with id '" + device_id + "'";
            return {};
        }
        return {syntheticMode()};
    }

    // The stub has exactly one mode, so negotiation always settles there regardless of the
    // request — activeMode() reports what was really selected, not what was asked for.
    bool open(const std::string& device_id, const OpenRequest&) override {
        if (!device_id.empty() && device_id != "stub:0") {
            last_error_ = "no synthetic device with id '" + device_id + "'";
            return false;
        }
        opened_ = true;
        frame_index_ = 0;
        active_ = syntheticMode();
        last_error_.clear();
        return true;
    }

    void close() override {
        opened_ = false;
        frame_index_ = 0;
        active_ = CameraMode{};
    }

    bool isOpen() const override { return opened_; }
    CameraMode activeMode() const override { return active_; }
    std::string lastError() const override { return last_error_; }

    // A deliberately garish, obviously-generated pattern: hue-cycling colour bars over a grey
    // staircase, with a sweeping marker and a hard checkerboard border. Nobody can mistake this
    // for a photograph of a part, which is the point.
    GrabResult grabFrame(CameraFrame& out) override {
        if (!opened_) {
            last_error_ = "grabFrame called with no open device";
            return GrabResult::not_open;
        }

        out.width = kStubWidth;
        out.height = kStubHeight;
        out.rgba.resize((size_t)kStubWidth * (size_t)kStubHeight * 4u);

        const double phase = (double)(frame_index_ % 120u) * 3.0;  // 4s hue rotation at 30fps
        const int sweep = (int)((frame_index_ * 7u) % (uint64_t)kStubWidth);
        const int bars_bottom = (kStubHeight * 2) / 3;

        // Precompute the eight bar colours once per frame rather than per pixel.
        uint8_t bar_rgb[8][3];
        for (int b = 0; b < 8; ++b) hsvToRgb(phase + b * 45.0, 0.85, 0.95, bar_rgb[b]);

        for (int y = 0; y < kStubHeight; ++y) {
            uint8_t* row = out.rgba.data() + (size_t)y * (size_t)kStubWidth * 4u;
            for (int x = 0; x < kStubWidth; ++x) {
                uint8_t* p = row + (size_t)x * 4u;
                if (x < 32 || x >= kStubWidth - 32 || y < 32 || y >= kStubHeight - 32) {
                    // Hard-edged checkerboard border — also gives frameSharpness real gradients.
                    const uint8_t v = (((x >> 4) ^ (y >> 4)) & 1) ? 235 : 20;
                    p[0] = p[1] = p[2] = v;
                } else if (y < bars_bottom) {
                    const int bar = (x * 8) / kStubWidth;
                    p[0] = bar_rgb[bar][0];
                    p[1] = bar_rgb[bar][1];
                    p[2] = bar_rgb[bar][2];
                } else {
                    const int steps = 16;
                    const int step = (x * steps) / kStubWidth;
                    const uint8_t v = (uint8_t)(step * 255 / (steps - 1));
                    p[0] = p[1] = p[2] = v;
                }
                if (x >= sweep && x < sweep + 6) { p[0] = 255; p[1] = 0; p[2] = 255; }
                p[3] = 255;
            }
        }

        out.timestamp_sec = (double)frame_index_ / kStubFps;
        out.sharpness = -1.0;
        // The honesty flag. No caller may persist this as a capture.
        out.synthetic = true;
        ++frame_index_;
        last_error_.clear();
        return GrabResult::ok;
    }

    // A software pattern generator has no optics and no sensor: reporting anything as
    // "supported" here would be a lie the UI would render as a live slider.
    ControlState getControl(CameraControl) override { return ControlState{}; }
    bool setControl(CameraControl, double) override { return false; }
    bool setControlAuto(CameraControl, bool) override { return false; }

    // Nothing can drift, so the photogrammetry pre-flight is trivially satisfied.
    bool lockForCapture() override { return true; }
};

#if defined(__linux__) && !defined(_WIN32)
// Forward declaration — implemented in camera_v4l2.cpp
std::unique_ptr<CameraBackend> createV4L2Camera();
#endif

#ifdef _WIN32
// Forward declaration — implemented in camera_mf.cpp
std::unique_ptr<CameraBackend> createMFCamera();
#endif

std::unique_ptr<CameraBackend> CameraBackend::create() {
#if defined(__linux__) && !defined(_WIN32)
    // Production path: always prefer V4L2. Unit tests force the synthetic stub with
    // CHUDWARE_FORCE_STUB_CAMERA=1 so CI stays hardware-free.
    const char* force_stub = std::getenv("CHUDWARE_FORCE_STUB_CAMERA");
    if (force_stub && std::strcmp(force_stub, "1") == 0)
        return std::make_unique<StubCameraBackend>();
    return createV4L2Camera();
#elif defined(_WIN32)
    const char* force_stub = std::getenv("CHUDWARE_FORCE_STUB_CAMERA");
    if (force_stub && std::strcmp(force_stub, "1") == 0)
        return std::make_unique<StubCameraBackend>();
    return createMFCamera();
#else
    return std::make_unique<StubCameraBackend>();
#endif
}

}  // namespace chudware::platform
