#pragma once
// ---------------------------------------------------------------------------------------------
// CameraBackend — platform webcam abstraction for the Reverse Engineering workspace.
//
// v2 (RE-40). The v1 interface was stills-only, single hardcoded 640x480 YUYV mode, no format
// negotiation and no controls. That is unusable for photogrammetry: SfM needs the sensor's full
// resolution, and it needs autofocus/auto-exposure/auto-white-balance LOCKED so intrinsics stay
// constant across the whole capture set. v2 therefore adds:
//
//   • Mode enumeration + negotiation (listModes / OpenRequest) so the GUI can pick max-res.
//   • Explicit control surface (focus / exposure / white balance / gain / zoom) with an
//     "auto" flag per control, plus lockForCapture() which is the one call photogrammetry
//     actually wants: freeze everything at its current auto-converged value.
//   • Honesty: CameraFrame::synthetic marks frames that did NOT come off a sensor. A synthetic
//     frame MUST never be committed into a reveng session as a real capture.
//   • grabFrame() is non-blocking-ish: backends buffer via streaming I/O and return false with
//     Result::again when no new frame is ready, so the GL thread never stalls on a read().
//
// Contract: contracts/reveng.md §7. Implementations: camera_v4l2.cpp (Linux),
// camera_mf.cpp (Windows / Media Foundation), camera_stub.cpp (fallback + create() factory).
// ---------------------------------------------------------------------------------------------
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace chudware::platform {

// ---- device / mode description ---------------------------------------------------------------

struct CameraInfo {
    std::string device_id;
    std::string label;
    std::string bus;       // stable-ish hardware path (USB port / symlink target); may be empty
};

// A negotiable capture mode. `fourcc` is the raw platform pixel format tag, kept opaque to the
// GUI — compare modes by width/height/fps and let the backend pick the decode path.
struct CameraMode {
    int      width  = 0;
    int      height = 0;
    double   fps    = 0.0;
    uint32_t fourcc = 0;
    std::string format_label;   // "MJPG" / "YUYV" / "NV12" … for the UI mode combo
    bool     compressed = false;  // MJPEG/H264 — higher res available, needs decode
};

struct OpenRequest {
    int    width  = 0;        // 0 = let the backend pick its highest-resolution still mode
    int    height = 0;
    double fps    = 0.0;      // 0 = backend default
    bool   prefer_max_resolution = true;  // photogrammetry default: resolution over framerate
};

// ---- frames -----------------------------------------------------------------------------------

struct CameraFrame {
    int width  = 0;
    int height = 0;
    std::vector<uint8_t> rgba;   // row-major RGBA8, tightly packed (stride == width*4)
    double timestamp_sec = 0.0;

    // TRUE when this frame was generated in software (no camera present, open failed, or a
    // decode path was unavailable). Callers MUST refuse to persist synthetic frames as captures.
    bool synthetic = false;

    // Variance-of-Laplacian sharpness computed by the backend-independent helper below.
    // < 0 means "not scored yet".
    double sharpness = -1.0;
};

// Outcome of grabFrame(). `again` is the normal steady-state answer when the driver has no new
// buffer queued yet — it is NOT an error and MUST NOT trigger a device reopen.
enum class GrabResult { ok, again, device_lost, decode_failed, not_open };

// ---- controls -----------------------------------------------------------------------------------

enum class CameraControl { Focus, Exposure, WhiteBalance, Gain, Zoom, Brightness };

// Normalized, device-independent view of one control. `value` is always 0..1 within
// [min_raw, max_raw]; backends do the raw conversion. `supported` false means the device
// does not expose the control at all (the UI greys the slider out rather than lying).
struct ControlState {
    bool   supported = false;
    bool   auto_capable = false;
    bool   automatic = false;   // currently in auto mode
    double value = 0.0;         // normalized 0..1, meaningful only when !automatic
};

class CameraBackend {
public:
    virtual ~CameraBackend() = default;
    static std::unique_ptr<CameraBackend> create();  // V4L2 / MF / stub

    // ---- lifecycle ----
    virtual std::vector<CameraInfo> listDevices() = 0;
    virtual std::vector<CameraMode> listModes(const std::string& device_id) = 0;
    virtual bool open(const std::string& device_id, const OpenRequest& req) = 0;
    virtual void close() = 0;
    virtual bool isOpen() const = 0;

    // The mode the device actually settled on after open() negotiation.
    virtual CameraMode activeMode() const = 0;

    // Human-readable reason for the last failure ("" when healthy). Surfaced verbatim in the
    // capture panel so a permissions / busy-device problem is diagnosable without a log dive.
    virtual std::string lastError() const = 0;

    // ---- streaming ----
    // Pulls the next queued buffer. Never blocks longer than the driver's own dequeue timeout.
    virtual GrabResult grabFrame(CameraFrame& out) = 0;

    // ---- controls ----
    virtual ControlState getControl(CameraControl c) = 0;
    virtual bool setControl(CameraControl c, double normalized_value) = 0;  // implies manual mode
    virtual bool setControlAuto(CameraControl c, bool automatic) = 0;

    // Freeze focus + exposure + white balance + gain at their present values. This is the
    // photogrammetry pre-flight: intrinsics and colour response must not drift mid-capture.
    // Returns false if any *supported* control could not be locked.
    virtual bool lockForCapture() = 0;
};

// Backend-independent sharpness score (variance of the Laplacian on a luma downsample).
// Mirrors services/vision/frames.py blur_score() so GUI blur badges and the Python frame
// culler agree on what "blurry" means. Higher is sharper; the shared threshold is
// kSharpnessBlurThreshold.
double frameSharpness(const CameraFrame& f);
inline constexpr double kSharpnessBlurThreshold = 100.0;

}  // namespace chudware::platform
