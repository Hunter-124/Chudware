// test_camera_backend.cpp — ctest for the CameraBackend v2 contract (camera.h, contracts/reveng.md §7).
// Pure CPU against the synthetic stub backend; no live device required.
//
// Compile:  see CMakeLists.txt (add_executable test_camera_backend).
// Run:      ctest -R test_camera_backend  (or ./test_camera_backend directly)
//
// Coverage:
//   ✓ listDevices / listModes / open(OpenRequest) / activeMode / isOpen / close lifecycle
//   ✓ grabFrame returns a GrabResult (not a bool) and not_open once closed
//   ✓ every stub frame is flagged synthetic — the honesty flag capture persistence relies on
//   ✓ every control reports supported=false on the stub; setters refuse
//   ✓ lockForCapture() succeeds trivially on the stub
//   ✓ frameSharpness(): flat frame ~0, high-contrast checkerboard >> kSharpnessBlurThreshold,
//     degenerate frames return the -1 "unscored" sentinel
//   ✓ optional live-hardware smoke behind CHUDWARE_TEST_CAMERA=1 (never fails CI)
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "../src/platform/camera.h"

using namespace chudware::platform;

static int gPass = 0, gFail = 0;

#define CHECK(cond) do { \
    if (cond) { ++gPass; } \
    else { ++gFail; std::fprintf(stderr, "FAIL  %s:%d  %s\n", __FILE__, __LINE__, #cond); } \
} while(0)

static void setEnvVar(const char* key, const char* value) {
#if defined(_WIN32)
    ::_putenv_s(key, value);
#else
    ::setenv(key, value, 1);
#endif
}

static void unsetEnvVar(const char* key) {
#if defined(_WIN32)
    ::_putenv_s(key, "");
#else
    ::unsetenv(key);
#endif
}

// CameraBackend::create() returns the real platform backend by default; the synthetic stub is
// selected with CHUDWARE_FORCE_STUB_CAMERA=1 so CI has no hardware dependency.
static std::unique_ptr<CameraBackend> makeStubBackend() {
    setEnvVar("CHUDWARE_FORCE_STUB_CAMERA", "1");
    unsetEnvVar("CHUDWARE_TEST_CAMERA");
    return CameraBackend::create();
}

static const char* controlName(CameraControl c) {
    switch (c) {
        case CameraControl::Focus:        return "Focus";
        case CameraControl::Exposure:     return "Exposure";
        case CameraControl::WhiteBalance: return "WhiteBalance";
        case CameraControl::Gain:         return "Gain";
        case CameraControl::Zoom:         return "Zoom";
        case CameraControl::Brightness:   return "Brightness";
    }
    return "?";
}

// ─── Lifecycle ───────────────────────────────────────────────────────────────

static void test_stub_lifecycle() {
    auto cam = makeStubBackend();
    CHECK(cam != nullptr);
    if (!cam) return;

    CHECK(!cam->isOpen());  // fresh backend starts closed

    auto devices = cam->listDevices();
    CHECK(!devices.empty());
    if (devices.empty()) return;
    CHECK(!devices[0].device_id.empty());
    CHECK(!devices[0].label.empty());

    auto modes = cam->listModes(devices[0].device_id);
    CHECK(!modes.empty());
    if (!modes.empty()) {
        CHECK(modes[0].width > 0 && modes[0].height > 0);
        CHECK(modes[0].fps > 0.0);
        CHECK(!modes[0].format_label.empty());
    }

    // Unknown device id must be refused rather than silently substituted.
    CHECK(cam->listModes("no-such-device").empty());

    OpenRequest req;
    req.prefer_max_resolution = true;
    CHECK(cam->open(devices[0].device_id, req));
    CHECK(cam->isOpen());

    const CameraMode active = cam->activeMode();
    CHECK(active.width > 0 && active.height > 0);
    CHECK(active.fps > 0.0);
    // activeMode() must be one of the advertised modes, not an invention.
    bool advertised = false;
    for (const CameraMode& m : modes)
        if (m.width == active.width && m.height == active.height) advertised = true;
    CHECK(advertised);

    CameraFrame frame;
    const GrabResult r = cam->grabFrame(frame);
    CHECK(r == GrabResult::ok);
    CHECK(frame.width == active.width);
    CHECK(frame.height == active.height);
    CHECK(frame.rgba.size() == (size_t)frame.width * (size_t)frame.height * 4u);
    CHECK(frame.rgba[3] == 255);            // opaque alpha
    CHECK(frame.sharpness < 0.0);           // backend leaves scoring to the caller

    // The whole point of the stub: it must never masquerade as real capture data.
    CHECK(frame.synthetic == true);

    // A second grab must advance the time base rather than repeat a frozen still.
    CameraFrame frame2;
    CHECK(cam->grabFrame(frame2) == GrabResult::ok);
    CHECK(frame2.synthetic == true);
    CHECK(frame2.timestamp_sec > frame.timestamp_sec);

    cam->close();
    CHECK(!cam->isOpen());

    // Grabbing from a closed backend is a defined state, not undefined behaviour.
    CameraFrame after;
    CHECK(cam->grabFrame(after) == GrabResult::not_open);

    std::fprintf(stdout, "stub frame: %dx%d @ %.1f fps [%s] rgba=%zu bytes synthetic=%d\n",
                 frame.width, frame.height, active.fps, active.format_label.c_str(),
                 frame.rgba.size(), (int)frame.synthetic);
}

static void test_stub_rejects_unknown_device() {
    auto cam = makeStubBackend();
    if (!cam) return;
    OpenRequest req;
    CHECK(!cam->open("no-such-device", req));
    CHECK(!cam->isOpen());
    CHECK(!cam->lastError().empty());  // failures must be diagnosable, not silent
}

// ─── Controls ────────────────────────────────────────────────────────────────

static void test_stub_controls() {
    auto cam = makeStubBackend();
    if (!cam) return;
    auto devices = cam->listDevices();
    if (devices.empty()) { CHECK(false); return; }
    OpenRequest req;
    CHECK(cam->open(devices[0].device_id, req));

    // A software pattern generator has no optics: every control must report unsupported so the
    // UI greys the slider out instead of pretending to drive a sensor.
    const CameraControl all[] = {CameraControl::Focus,        CameraControl::Exposure,
                                 CameraControl::WhiteBalance, CameraControl::Gain,
                                 CameraControl::Zoom,         CameraControl::Brightness};
    for (CameraControl c : all) {
        const ControlState st = cam->getControl(c);
        if (st.supported)
            std::fprintf(stderr, "  control %s unexpectedly reported supported\n", controlName(c));
        CHECK(st.supported == false);
        CHECK(st.automatic == false);
        CHECK(st.auto_capable == false);
        CHECK(cam->setControl(c, 0.5) == false);
        CHECK(cam->setControlAuto(c, true) == false);
    }

    // Nothing can drift, so the photogrammetry pre-flight is trivially satisfied.
    CHECK(cam->lockForCapture() == true);
    cam->close();
}

// ─── frameSharpness ──────────────────────────────────────────────────────────

static CameraFrame makeFrame(int w, int h) {
    CameraFrame f;
    f.width = w;
    f.height = h;
    f.rgba.assign((size_t)w * (size_t)h * 4u, 0);
    return f;
}

static void fillSolid(CameraFrame& f, uint8_t v) {
    for (size_t i = 0; i < f.rgba.size(); i += 4) {
        f.rgba[i + 0] = v;
        f.rgba[i + 1] = v;
        f.rgba[i + 2] = v;
        f.rgba[i + 3] = 255;
    }
}

static void fillCheckerboard(CameraFrame& f, int block, uint8_t lo, uint8_t hi) {
    for (int y = 0; y < f.height; ++y) {
        for (int x = 0; x < f.width; ++x) {
            const uint8_t v = (((x / block) ^ (y / block)) & 1) ? hi : lo;
            uint8_t* p = f.rgba.data() + ((size_t)y * (size_t)f.width + (size_t)x) * 4u;
            p[0] = p[1] = p[2] = v;
            p[3] = 255;
        }
    }
}

static void test_frame_sharpness() {
    // Flat grey has no Laplacian response at all: the score must collapse to ~0, far below the
    // blur threshold, so an out-of-focus wall is never mistaken for a usable capture.
    CameraFrame flat = makeFrame(640, 480);
    fillSolid(flat, 128);
    const double flat_score = frameSharpness(flat);
    CHECK(flat_score >= 0.0);
    CHECK(flat_score < 1.0);
    CHECK(flat_score < kSharpnessBlurThreshold);

    // A hard-edged checkerboard is the sharpest thing a frame can contain.
    CameraFrame sharp = makeFrame(640, 480);
    fillCheckerboard(sharp, 16, 20, 235);
    const double sharp_score = frameSharpness(sharp);
    CHECK(sharp_score > kSharpnessBlurThreshold);
    CHECK(sharp_score > flat_score);

    // Ordering must hold at high resolution too — the sampler must not rescale blur away.
    CameraFrame sharp4k = makeFrame(3840, 2160);
    fillCheckerboard(sharp4k, 16, 20, 235);
    CHECK(frameSharpness(sharp4k) > kSharpnessBlurThreshold);

    CameraFrame flat4k = makeFrame(3840, 2160);
    fillSolid(flat4k, 128);
    CHECK(frameSharpness(flat4k) < kSharpnessBlurThreshold);

    // Degenerate inputs return the documented "unscored" sentinel rather than 0 (which would
    // read as a legitimately black, perfectly flat frame).
    CameraFrame empty;
    CHECK(frameSharpness(empty) < 0.0);

    CameraFrame tiny = makeFrame(2, 2);
    CHECK(frameSharpness(tiny) < 0.0);

    // Truncated buffer must be detected, not read out of bounds.
    CameraFrame truncated = makeFrame(64, 64);
    truncated.rgba.resize(16);
    CHECK(frameSharpness(truncated) < 0.0);

    std::fprintf(stdout, "frameSharpness: flat=%.3f checkerboard=%.1f (threshold %.1f)\n",
                 flat_score, sharp_score, kSharpnessBlurThreshold);
}

// ─── Optional live hardware smoke ────────────────────────────────────────────
// Only runs when the caller sets CHUDWARE_TEST_CAMERA=1. Never fails CI: a missing, busy or
// permission-blocked camera is reported and tolerated.

static void test_live_optional(bool live_requested) {
    if (!live_requested) {
        std::fprintf(stdout, "live smoke skipped (CHUDWARE_TEST_CAMERA != 1)\n");
        return;
    }
    unsetEnvVar("CHUDWARE_FORCE_STUB_CAMERA");  // real V4L2 / MF path
    setEnvVar("CHUDWARE_TEST_CAMERA", "1");

    auto cam = CameraBackend::create();
    if (!cam) {
        std::fprintf(stdout, "live smoke: create() returned null — tolerated\n");
        return;
    }
    auto devices = cam->listDevices();
    if (devices.empty()) {
        std::fprintf(stdout, "live smoke: no capture devices (%s) — tolerated\n",
                     cam->lastError().c_str());
        return;
    }
    std::fprintf(stdout, "live smoke: %zu device(s); using '%s' [%s] bus=%s\n", devices.size(),
                 devices[0].label.c_str(), devices[0].device_id.c_str(), devices[0].bus.c_str());

    const auto modes = cam->listModes(devices[0].device_id);
    std::fprintf(stdout, "live smoke: %zu native mode(s)\n", modes.size());
    for (size_t i = 0; i < modes.size() && i < 5; ++i)
        std::fprintf(stdout, "    %dx%d @ %.1f [%s]%s\n", modes[i].width, modes[i].height,
                     modes[i].fps, modes[i].format_label.c_str(),
                     modes[i].compressed ? " compressed" : "");

    // Photogrammetry default: resolution over framerate.
    OpenRequest req;
    req.prefer_max_resolution = true;
    if (!cam->open(devices[0].device_id, req)) {
        std::fprintf(stdout, "live smoke: open() failed on %s (%s) — tolerated\n",
                     devices[0].device_id.c_str(), cam->lastError().c_str());
        return;
    }

    const CameraMode active = cam->activeMode();
    std::fprintf(stdout, "live smoke: negotiated %dx%d @ %.1f fps [%s]%s\n", active.width,
                 active.height, active.fps, active.format_label.c_str(),
                 active.compressed ? " compressed" : "");

    for (CameraControl c : {CameraControl::Focus, CameraControl::Exposure,
                            CameraControl::WhiteBalance, CameraControl::Gain}) {
        const ControlState st = cam->getControl(c);
        std::fprintf(stdout, "    %-12s supported=%d auto_capable=%d automatic=%d value=%.3f\n",
                     controlName(c), (int)st.supported, (int)st.auto_capable, (int)st.automatic,
                     st.value);
    }
    std::fprintf(stdout, "live smoke: lockForCapture()=%d (%s)\n", (int)cam->lockForCapture(),
                 cam->lastError().c_str());

    // `again` is the normal answer while the driver fills its first buffers.
    CameraFrame frame;
    GrabResult r = GrabResult::again;
    for (int attempt = 0; attempt < 60 && r == GrabResult::again; ++attempt)
        r = cam->grabFrame(frame);

    if (r == GrabResult::ok) {
        std::fprintf(stdout, "live frame: %dx%d synthetic=%d sharpness=%.1f\n", frame.width,
                     frame.height, (int)frame.synthetic, frameSharpness(frame));
        // A real backend must never claim a hardware frame is synthetic.
        CHECK(frame.synthetic == false);
    } else {
        std::fprintf(stdout, "live smoke: grabFrame() -> %d (%s) — tolerated\n", (int)r,
                     cam->lastError().c_str());
    }
    cam->close();
}

int main() {
    const char* ev = std::getenv("CHUDWARE_TEST_CAMERA");
    const bool live_requested = ev && std::strcmp(ev, "1") == 0;

    test_stub_lifecycle();
    test_stub_rejects_unknown_device();
    test_stub_controls();
    test_frame_sharpness();
    test_live_optional(live_requested);

    std::fprintf(stdout, "camera_backend: %d passed, %d failed\n", gPass, gFail);
    return (gFail == 0) ? 0 : 1;
}
