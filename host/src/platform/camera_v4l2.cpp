#if defined(__linux__) && !defined(_WIN32)
// ---------------------------------------------------------------------------------------------
// camera_v4l2.cpp — Linux V4L2 implementation of CameraBackend v2 (RE-40).
//
// The v1 backend was unusable for photogrammetry: it captured with plain ::read(2) (which most
// UVC drivers do not implement at all), hardcoded 640x480 YUYV, and — worst of all — filled a
// flat grey buffer and reported SUCCESS whenever the negotiated format was not YUYV. An MJPEG-
// only camera therefore looked like it was working while producing nothing but grey.
//
// This rewrite is built around three rules:
//   1. Streaming I/O only — REQBUFS + mmap + QBUF/DQBUF + STREAMON. No ::read() capture path.
//   2. Never fabricate pixels. Every decode path either produces real sensor data or returns
//      GrabResult::decode_failed with a diagnosable lastError(). CameraFrame::synthetic is
//      always false here, because this backend cannot produce a synthetic frame.
//   3. Resolution first. open() negotiates the device's maximum resolution by default and only
//      falls back to a compressed format when that is the sole way to reach it — SfM wants
//      pixels far more than it wants framerate.
//
// Decode paths: YUYV/YUY2, UYVY, YVYU, NV12, NV21, YU12 (I420), YV12, GREY, RGB24, BGR24, and
// MJPG/JPEG via stb_image. Anything else is reported by name rather than silently faked.
//
// Contract: host/src/platform/camera.h, contracts/reveng.md §7.
// ---------------------------------------------------------------------------------------------

#include "camera.h"

// stb_image is compiled once in host/src/stb_impl.cpp; only the header is included here, the
// same way host/src/ui/reveng/img_tex.cpp does it.
#include "stb_image.h"

#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <dirent.h>
#include <fcntl.h>
#include <poll.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace chudware::platform {
namespace {

// poll() budget per grabFrame(). Small enough that the GL thread can call this every frame
// without ever noticing; the camera.h contract says grabFrame must not stall.
constexpr int kPollTimeoutMs = 2;

// Number of mmap buffers requested from the driver. 4 is the usual sweet spot for UVC: enough
// to absorb a missed GL frame, few enough that latency stays under ~2 frames.
constexpr uint32_t kRequestedBuffers = 4;

uint8_t clamp_u8(int v) {
    return static_cast<uint8_t>(v < 0 ? 0 : (v > 255 ? 255 : v));
}

int32_t clamp_i32(int32_t v, int32_t lo, int32_t hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

// ioctl that survives signal interruption.
int xioctl(int fd, unsigned long request, void* arg) {
    int r = 0;
    do {
        r = ::ioctl(fd, request, arg);
    } while (r == -1 && errno == EINTR);
    return r;
}

std::string fourccLabel(uint32_t f) {
    char c[4] = {static_cast<char>(f & 0xFF), static_cast<char>((f >> 8) & 0xFF),
                 static_cast<char>((f >> 16) & 0xFF), static_cast<char>((f >> 24) & 0xFF)};
    for (char& ch : c) {
        if (ch < 32 || ch > 126) ch = '?';
    }
    std::string s(c, 4);
    while (!s.empty() && s.back() == ' ') s.pop_back();
    return s;
}

bool isCompressedFourcc(uint32_t f) {
    switch (f) {
        case V4L2_PIX_FMT_MJPEG:
        case V4L2_PIX_FMT_JPEG:
        case V4L2_PIX_FMT_MPEG:
        case V4L2_PIX_FMT_H264:
#ifdef V4L2_PIX_FMT_H264_NO_SC
        case V4L2_PIX_FMT_H264_NO_SC:
#endif
#ifdef V4L2_PIX_FMT_H264_MVC
        case V4L2_PIX_FMT_H264_MVC:
#endif
#ifdef V4L2_PIX_FMT_HEVC
        case V4L2_PIX_FMT_HEVC:
#endif
#ifdef V4L2_PIX_FMT_VP8
        case V4L2_PIX_FMT_VP8:
#endif
#ifdef V4L2_PIX_FMT_VP9
        case V4L2_PIX_FMT_VP9:
#endif
            return true;
        default:
            return false;
    }
}

// Formats this backend can actually turn into RGBA. open() will not negotiate anything else,
// because a mode we cannot decode is a mode that produces zero usable captures.
bool isDecodableFourcc(uint32_t f) {
    switch (f) {
        case V4L2_PIX_FMT_YUYV:
        case V4L2_PIX_FMT_UYVY:
        case V4L2_PIX_FMT_YVYU:
        case V4L2_PIX_FMT_NV12:
        case V4L2_PIX_FMT_NV21:
        case V4L2_PIX_FMT_YUV420:
        case V4L2_PIX_FMT_YVU420:
        case V4L2_PIX_FMT_GREY:
        case V4L2_PIX_FMT_RGB24:
        case V4L2_PIX_FMT_BGR24:
        case V4L2_PIX_FMT_MJPEG:
        case V4L2_PIX_FMT_JPEG:
            return true;
        default:
            return false;
    }
}

const char* controlName(CameraControl c) {
    switch (c) {
        case CameraControl::Focus:        return "focus";
        case CameraControl::Exposure:     return "exposure";
        case CameraControl::WhiteBalance: return "white balance";
        case CameraControl::Gain:         return "gain";
        case CameraControl::Zoom:         return "zoom";
        case CameraControl::Brightness:   return "brightness";
    }
    return "control";
}

// ---- control mapping ---------------------------------------------------------------------
// `auto_cid` is the paired automatic toggle. `auto_is_menu` marks V4L2_CID_EXPOSURE_AUTO, which
// is a menu (MANUAL / AUTO / SHUTTER_PRIORITY / APERTURE_PRIORITY) rather than a boolean.
struct ControlMap {
    uint32_t cid = 0;
    uint32_t auto_cid = 0;
    bool     auto_is_menu = false;
};

ControlMap mapControl(CameraControl c) {
    switch (c) {
        case CameraControl::Focus:
            return {V4L2_CID_FOCUS_ABSOLUTE, V4L2_CID_FOCUS_AUTO, false};
        case CameraControl::Exposure:
            return {V4L2_CID_EXPOSURE_ABSOLUTE, V4L2_CID_EXPOSURE_AUTO, true};
        case CameraControl::WhiteBalance:
            return {V4L2_CID_WHITE_BALANCE_TEMPERATURE, V4L2_CID_AUTO_WHITE_BALANCE, false};
        case CameraControl::Gain:
            // V4L2_CID_AUTOGAIN is the paired auto toggle; without driving it lockForCapture()
            // could not actually freeze gain on cameras that run AGC.
            return {V4L2_CID_GAIN, V4L2_CID_AUTOGAIN, false};
        case CameraControl::Zoom:
            return {V4L2_CID_ZOOM_ABSOLUTE, 0, false};
        case CameraControl::Brightness:
            return {V4L2_CID_BRIGHTNESS, 0, false};
    }
    return {};
}

struct CtrlRange {
    bool    supported = false;
    int32_t min = 0;
    int32_t max = 0;
    int32_t step = 1;
};

// QUERYCTRL. EINVAL (or the DISABLED flag) means the device simply does not have the control —
// that is "unsupported", not an error. INACTIVE is *not* treated as unsupported: exposure-
// absolute is routinely inactive while auto-exposure is engaged, yet still readable.
CtrlRange queryCtrl(int fd, uint32_t cid) {
    CtrlRange r;
    if (fd < 0 || cid == 0) return r;
    v4l2_queryctrl q{};
    q.id = cid;
    if (xioctl(fd, VIDIOC_QUERYCTRL, &q) != 0) return r;
    if (q.flags & V4L2_CTRL_FLAG_DISABLED) return r;
    r.supported = true;
    r.min = q.minimum;
    r.max = q.maximum;
    r.step = q.step > 0 ? q.step : 1;
    return r;
}

bool getCtrlRaw(int fd, uint32_t cid, int32_t& out) {
    v4l2_control c{};
    c.id = cid;
    if (xioctl(fd, VIDIOC_G_CTRL, &c) != 0) return false;
    out = c.value;
    return true;
}

bool setCtrlRaw(int fd, uint32_t cid, int32_t value) {
    v4l2_control c{};
    c.id = cid;
    c.value = value;
    return xioctl(fd, VIDIOC_S_CTRL, &c) == 0;
}

// V4L2_CID_EXPOSURE_AUTO is a menu and UVC devices implement wildly different subsets of it —
// plenty offer only MANUAL + APERTURE_PRIORITY. Probing with QUERYMENU lets setAutoRaw() pick an
// entry the device actually admits to instead of guessing and eating an EINVAL.
bool menuEntryValid(int fd, uint32_t cid, uint32_t index) {
    v4l2_querymenu m{};
    m.id = cid;
    m.index = index;
    return xioctl(fd, VIDIOC_QUERYMENU, &m) == 0;
}

double normalizeCtrl(int32_t raw, const CtrlRange& r) {
    if (r.max <= r.min) return 0.0;
    const double t = static_cast<double>(raw - r.min) / static_cast<double>(r.max - r.min);
    return t < 0.0 ? 0.0 : (t > 1.0 ? 1.0 : t);
}

int32_t denormalizeCtrl(double t, const CtrlRange& r) {
    if (t < 0.0) t = 0.0;
    if (t > 1.0) t = 1.0;
    if (r.max <= r.min) return r.min;
    const double span = static_cast<double>(r.max) - static_cast<double>(r.min);
    int32_t raw = r.min + static_cast<int32_t>(std::llround(t * span));
    raw = clamp_i32(raw, r.min, r.max);
    // Snap onto the driver's step grid, then re-clamp (rounding up can overshoot max).
    if (r.step > 1) raw = r.min + ((raw - r.min) / r.step) * r.step;
    return clamp_i32(raw, r.min, r.max);
}

// Drives the paired auto toggle. Returns false only when the toggle exists and the write failed.
bool setAutoRaw(int fd, const ControlMap& m, bool automatic) {
    if (m.auto_cid == 0) return true;
    if (!queryCtrl(fd, m.auto_cid).supported) return true;
    if (!m.auto_is_menu) return setCtrlRaw(fd, m.auto_cid, automatic ? 1 : 0);

    // Menu-valued V4L2_CID_EXPOSURE_AUTO. Ask the driver which entries exist before writing.
    const int32_t first = automatic ? V4L2_EXPOSURE_APERTURE_PRIORITY : V4L2_EXPOSURE_MANUAL;
    const int32_t second = automatic ? V4L2_EXPOSURE_AUTO : V4L2_EXPOSURE_SHUTTER_PRIORITY;
    if (menuEntryValid(fd, m.auto_cid, static_cast<uint32_t>(first)))
        return setCtrlRaw(fd, m.auto_cid, first);
    if (menuEntryValid(fd, m.auto_cid, static_cast<uint32_t>(second)))
        return setCtrlRaw(fd, m.auto_cid, second);
    // Driver refuses QUERYMENU entirely: fall back to the spec value and let S_CTRL judge.
    return setCtrlRaw(fd, m.auto_cid, first);
}

bool readAutoRaw(int fd, const ControlMap& m, bool& automatic) {
    int32_t v = 0;
    if (!getCtrlRaw(fd, m.auto_cid, v)) return false;
    if (!m.auto_is_menu) {
        automatic = (v != 0);
        return true;
    }
    // SHUTTER_PRIORITY still means the *exposure time* is under manual control.
    automatic = (v != V4L2_EXPOSURE_MANUAL && v != V4L2_EXPOSURE_SHUTTER_PRIORITY);
    return true;
}

// ---- colour conversion -------------------------------------------------------------------
// BT.601 limited range (Y 16..235, C 16..240) in 8-bit fixed point. The naive
// "R = Y + 1.402*(V-128)" form the old backend used assumes full-range Y and crushes blacks,
// which then shows up as a systematic bias in photometric feature matching.
inline void yuvToRgba(int y, int u, int v, uint8_t* p) {
    const int c = y - 16;
    const int d = u - 128;
    const int e = v - 128;
    p[0] = clamp_u8((298 * c + 409 * e + 128) >> 8);
    p[1] = clamp_u8((298 * c - 100 * d - 208 * e + 128) >> 8);
    p[2] = clamp_u8((298 * c + 516 * d + 128) >> 8);
    p[3] = 255;
}

// ---- MJPEG Huffman-table repair ------------------------------------------------------------
// The UVC spec permits MJPEG payloads that omit the Huffman tables (the host is expected to
// substitute the JPEG Annex-K defaults). stb_image has no built-in tables and, worse, does not
// reject such a frame — it decodes it into ~75% wrong pixels. That is exactly the silent-garbage
// failure this rewrite exists to eliminate, so splice the standard tables in ahead of the scan
// header. Cameras that do ship a DHT are passed through untouched.
constexpr uint8_t kStandardDht[] = {
    0xFF, 0xC4, 0x01, 0xA2,
    // DC luminance
    0x00,
    0x00, 0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B,
    // DC chrominance
    0x01,
    0x00, 0x03, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B,
    // AC luminance
    0x10,
    0x00, 0x02, 0x01, 0x03, 0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07,
    0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08, 0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0,
    0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
    0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
    0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7,
    0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5,
    0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
    0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
    0xF9, 0xFA,
    // AC chrominance
    0x11,
    0x00, 0x02, 0x01, 0x02, 0x04, 0x04, 0x03, 0x04, 0x07, 0x05, 0x04, 0x04, 0x00, 0x01, 0x02, 0x77,
    0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21, 0x31, 0x06, 0x12, 0x41, 0x51, 0x07, 0x61, 0x71,
    0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91, 0xA1, 0xB1, 0xC1, 0x09, 0x23, 0x33, 0x52, 0xF0,
    0x15, 0x62, 0x72, 0xD1, 0x0A, 0x16, 0x24, 0x34, 0xE1, 0x25, 0xF1, 0x17, 0x18, 0x19, 0x1A, 0x26,
    0x27, 0x28, 0x29, 0x2A, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48,
    0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68,
    0x69, 0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x88, 0x89, 0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5,
    0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3,
    0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA,
    0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
    0xF9, 0xFA,
};
static_assert(sizeof(kStandardDht) == 4 + 2 * (1 + 16 + 12) + 2 * (1 + 16 + 162),
              "standard DHT blob must hold four Annex-K tables plus the segment header");

// Finds the SOS marker offset and reports whether a DHT segment precedes it.
// Returns false when the payload is not a walkable JPEG (let stb_image produce the diagnosis).
bool scanJpegMarkers(const uint8_t* d, size_t n, size_t& sos_off, bool& has_dht) {
    if (n < 4 || d[0] != 0xFF || d[1] != 0xD8) return false;
    has_dht = false;
    size_t i = 2;
    while (i + 1 < n) {
        if (d[i] != 0xFF) return false;
        while (i + 1 < n && d[i + 1] == 0xFF) ++i;  // fill bytes
        if (i + 1 >= n) return false;
        const uint8_t marker = d[i + 1];
        if (marker == 0xD8 || marker == 0x01 || (marker >= 0xD0 && marker <= 0xD7)) {
            i += 2;  // standalone marker, no payload
            continue;
        }
        if (marker == 0xD9) return false;  // EOI before SOS — malformed
        if (i + 3 >= n) return false;
        const size_t seglen = (static_cast<size_t>(d[i + 2]) << 8) | d[i + 3];
        if (seglen < 2 || i + 2 + seglen > n) return false;
        if (marker == 0xC4) has_dht = true;
        if (marker == 0xDA) {
            sos_off = i;
            return true;
        }
        i += 2 + seglen;
    }
    return false;
}

// ---- mode helpers ----------------------------------------------------------------------------

void sortModes(std::vector<CameraMode>& modes) {
    std::sort(modes.begin(), modes.end(), [](const CameraMode& a, const CameraMode& b) {
        const long long aa = static_cast<long long>(a.width) * a.height;
        const long long ba = static_cast<long long>(b.width) * b.height;
        if (aa != ba) return aa > ba;
        if (a.fps != b.fps) return a.fps > b.fps;
        if (a.compressed != b.compressed) return !a.compressed;  // uncompressed first
        return a.fourcc < b.fourcc;
    });
    modes.erase(std::unique(modes.begin(), modes.end(),
                            [](const CameraMode& a, const CameraMode& b) {
                                return a.width == b.width && a.height == b.height &&
                                       a.fps == b.fps && a.fourcc == b.fourcc;
                            }),
                modes.end());
}

void appendMode(int fd, uint32_t fourcc, bool compressed, uint32_t w, uint32_t h,
                std::vector<CameraMode>& out) {
    if (w == 0 || h == 0) return;

    std::vector<double> fps_values;
    v4l2_frmivalenum fie{};
    fie.pixel_format = fourcc;
    fie.width = w;
    fie.height = h;
    for (fie.index = 0; xioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, &fie) == 0; ++fie.index) {
        if (fie.type == V4L2_FRMIVAL_TYPE_DISCRETE) {
            if (fie.discrete.numerator > 0) {
                fps_values.push_back(static_cast<double>(fie.discrete.denominator) /
                                     static_cast<double>(fie.discrete.numerator));
            }
        } else {
            // Stepwise/continuous: the shortest interval is the highest framerate.
            if (fie.stepwise.min.numerator > 0) {
                fps_values.push_back(static_cast<double>(fie.stepwise.min.denominator) /
                                     static_cast<double>(fie.stepwise.min.numerator));
            }
            break;
        }
        fie.pixel_format = fourcc;
        fie.width = w;
        fie.height = h;
    }
    // Driver advertises no intervals — emit the mode with fps unknown rather than dropping it.
    if (fps_values.empty()) fps_values.push_back(0.0);

    std::sort(fps_values.begin(), fps_values.end(), std::greater<double>());
    fps_values.erase(std::unique(fps_values.begin(), fps_values.end()), fps_values.end());

    for (double fps : fps_values) {
        CameraMode m;
        m.width = static_cast<int>(w);
        m.height = static_cast<int>(h);
        m.fps = fps;
        m.fourcc = fourcc;
        m.format_label = fourccLabel(fourcc);
        m.compressed = compressed;
        out.push_back(std::move(m));
    }
}

void appendStepwiseModes(int fd, uint32_t fourcc, bool compressed,
                         const v4l2_frmsize_stepwise& sw, std::vector<CameraMode>& out) {
    appendMode(fd, fourcc, compressed, sw.max_width, sw.max_height, out);

    const uint32_t step_w = sw.step_width > 0 ? sw.step_width : 1;
    const uint32_t step_h = sw.step_height > 0 ? sw.step_height : 1;
    static constexpr uint32_t kCommon[][2] = {{1920, 1080}, {1280, 720}, {640, 480}};
    for (const auto& c : kCommon) {
        const uint32_t w = c[0];
        const uint32_t h = c[1];
        if (w == sw.max_width && h == sw.max_height) continue;
        if (w < sw.min_width || w > sw.max_width) continue;
        if (h < sw.min_height || h > sw.max_height) continue;
        if ((w - sw.min_width) % step_w != 0) continue;
        if ((h - sw.min_height) % step_h != 0) continue;
        appendMode(fd, fourcc, compressed, w, h, out);
    }
}

// Last resort for capture hardware that refuses ENUM_FMT: whatever G_FMT reports is the mode.
void appendCurrentFormat(int fd, std::vector<CameraMode>& out) {
    v4l2_format cur{};
    cur.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (xioctl(fd, VIDIOC_G_FMT, &cur) != 0) return;
    if (cur.fmt.pix.width == 0 || cur.fmt.pix.height == 0) return;
    appendMode(fd, cur.fmt.pix.pixelformat, isCompressedFourcc(cur.fmt.pix.pixelformat),
               cur.fmt.pix.width, cur.fmt.pix.height, out);
}

void enumerateModes(int fd, std::vector<CameraMode>& out) {
    v4l2_fmtdesc fmtdesc{};
    fmtdesc.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    for (fmtdesc.index = 0; xioctl(fd, VIDIOC_ENUM_FMT, &fmtdesc) == 0; ++fmtdesc.index) {
        const uint32_t fourcc = fmtdesc.pixelformat;
        // Trust the driver's own COMPRESSED flag first; the fourcc table is the backstop for
        // drivers that leave flags at zero.
        const bool compressed =
            (fmtdesc.flags & V4L2_FMT_FLAG_COMPRESSED) != 0 || isCompressedFourcc(fourcc);

        v4l2_frmsizeenum fse{};
        fse.pixel_format = fourcc;
        bool any_size = false;
        for (fse.index = 0; xioctl(fd, VIDIOC_ENUM_FRAMESIZES, &fse) == 0; ++fse.index) {
            any_size = true;
            if (fse.type == V4L2_FRMSIZE_TYPE_DISCRETE) {
                appendMode(fd, fourcc, compressed, fse.discrete.width, fse.discrete.height, out);
            } else {
                appendStepwiseModes(fd, fourcc, compressed, fse.stepwise, out);
                break;  // stepwise/continuous enumerations carry a single entry
            }
            fse.pixel_format = fourcc;
        }

        if (!any_size) appendCurrentFormat(fd, out);
        fmtdesc.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    }

    if (out.empty()) appendCurrentFormat(fd, out);
    sortModes(out);
}

std::string errnoText(const std::string& path, int e) {
    switch (e) {
        case EACCES:
        case EPERM:
            return "permission denied on " + path +
                   " — add your user to the 'video' group (sudo usermod -aG video $USER), then "
                   "log out and back in";
        case EBUSY:
            return path + ": device is in use by another application";
        case ENODEV:
        case ENXIO:
        case ENOENT:
            return path + ": no such capture device (camera unplugged?)";
        default:
            return path + ": " + std::strerror(e);
    }
}

}  // namespace

// ================================================================================================

class V4L2CameraBackend : public CameraBackend {
public:
    ~V4L2CameraBackend() override { close(); }

    // ---- lifecycle -----------------------------------------------------------------------

    std::vector<CameraInfo> listDevices() override {
        std::vector<CameraInfo> out;

        DIR* dir = ::opendir("/dev");
        if (!dir) {
            err_ = std::string("cannot enumerate /dev: ") + std::strerror(errno);
            return out;
        }
        std::vector<std::pair<int, std::string>> nodes;
        while (const dirent* ent = ::readdir(dir)) {
            const std::string name = ent->d_name;
            if (name.compare(0, 5, "video") != 0 || name.size() <= 5) continue;
            int index = 0;
            bool numeric = true;
            for (size_t i = 5; i < name.size(); ++i) {
                if (name[i] < '0' || name[i] > '9') { numeric = false; break; }
                index = index * 10 + (name[i] - '0');
            }
            if (!numeric) continue;
            nodes.emplace_back(index, "/dev/" + name);
        }
        ::closedir(dir);
        std::sort(nodes.begin(), nodes.end());

        int denied = 0;
        for (const auto& node : nodes) {
            const std::string& path = node.second;
            const int fd = ::open(path.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC);
            if (fd < 0) {
                if (errno == EACCES || errno == EPERM) ++denied;
                continue;
            }
            v4l2_capability cap{};
            if (xioctl(fd, VIDIOC_QUERYCAP, &cap) == 0) {
                // Modern UVC exposes several nodes per physical camera (metadata, output).
                // Only the node advertising both CAPTURE and STREAMING can actually stream.
                const uint32_t caps = (cap.capabilities & V4L2_CAP_DEVICE_CAPS)
                                          ? cap.device_caps
                                          : cap.capabilities;
                if ((caps & V4L2_CAP_VIDEO_CAPTURE) && (caps & V4L2_CAP_STREAMING)) {
                    CameraInfo info;
                    info.device_id = path;
                    info.label = cstr(cap.card, sizeof(cap.card));
                    if (info.label.empty()) info.label = path;
                    info.bus = cstr(cap.bus_info, sizeof(cap.bus_info));
                    out.push_back(std::move(info));
                }
            }
            ::close(fd);
        }

        if (out.empty() && denied > 0) {
            err_ = "found " + std::to_string(denied) +
                   " /dev/video* node(s) but none could be opened — add your user to the "
                   "'video' group (sudo usermod -aG video $USER), then log out and back in";
        }
        return out;
    }

    std::vector<CameraMode> listModes(const std::string& device_id) override {
        std::vector<CameraMode> out;
        const std::string path = resolvePath(device_id);
        if (path.empty()) {
            err_ = "no V4L2 capture device available";
            return out;
        }

        // Reuse the live fd when the caller is asking about the device we already hold open;
        // some drivers refuse a second O_RDWR open while streaming.
        if (fd_ >= 0 && path == path_) {
            enumerateModes(fd_, out);
            return out;
        }

        const int fd = ::open(path.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC);
        if (fd < 0) {
            err_ = errnoText(path, errno);
            return out;
        }
        enumerateModes(fd, out);
        ::close(fd);
        if (out.empty()) err_ = path + ": device advertises no capture formats";
        return out;
    }

    bool open(const std::string& device_id, const OpenRequest& req) override {
        close();
        err_.clear();

        path_ = resolvePath(device_id);
        if (path_.empty()) {
            err_ = "no V4L2 capture device available";
            return false;
        }

        fd_ = ::open(path_.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC);
        if (fd_ < 0) {
            err_ = errnoText(path_, errno);
            fd_ = -1;
            return false;
        }

        v4l2_capability cap{};
        if (xioctl(fd_, VIDIOC_QUERYCAP, &cap) != 0) {
            setIoctlError("VIDIOC_QUERYCAP");
            close();
            return false;
        }
        const uint32_t caps =
            (cap.capabilities & V4L2_CAP_DEVICE_CAPS) ? cap.device_caps : cap.capabilities;
        if (!(caps & V4L2_CAP_VIDEO_CAPTURE)) {
            err_ = path_ + " is not a video-capture node (metadata or output-only device)";
            close();
            return false;
        }
        if (!(caps & V4L2_CAP_STREAMING)) {
            err_ = path_ + " does not support streaming I/O (mmap); this backend requires it";
            close();
            return false;
        }

        std::vector<CameraMode> modes;
        enumerateModes(fd_, modes);
        if (modes.empty()) {
            err_ = path_ + ": device advertises no capture formats";
            close();
            return false;
        }

        CameraMode chosen;
        if (!pickMode(modes, req, chosen)) {
            err_ = path_ + ": no decodable capture format (device offers " +
                   describeFormats(modes) +
                   "; this backend decodes YUYV, UYVY, YVYU, NV12, NV21, YU12, YV12, GREY, "
                   "RGB3, BGR3, MJPG, JPEG)";
            close();
            return false;
        }

        v4l2_format fmt{};
        fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        fmt.fmt.pix.width = static_cast<uint32_t>(chosen.width);
        fmt.fmt.pix.height = static_cast<uint32_t>(chosen.height);
        fmt.fmt.pix.pixelformat = chosen.fourcc;
        fmt.fmt.pix.field = V4L2_FIELD_ANY;
        if (xioctl(fd_, VIDIOC_S_FMT, &fmt) != 0) {
            setIoctlError("VIDIOC_S_FMT");
            close();
            return false;
        }
        // The driver is allowed to substitute geometry *and* format; honour what came back, but
        // refuse to stream a format we cannot decode rather than emitting garbage.
        if (!isDecodableFourcc(fmt.fmt.pix.pixelformat)) {
            err_ = path_ + ": driver substituted undecodable format " +
                   fourccLabel(fmt.fmt.pix.pixelformat) + " for requested " +
                   fourccLabel(chosen.fourcc);
            close();
            return false;
        }
        if (fmt.fmt.pix.width == 0 || fmt.fmt.pix.height == 0) {
            err_ = path_ + ": driver granted a zero-sized capture format";
            close();
            return false;
        }

        active_.width = static_cast<int>(fmt.fmt.pix.width);
        active_.height = static_cast<int>(fmt.fmt.pix.height);
        active_.fourcc = fmt.fmt.pix.pixelformat;
        active_.format_label = fourccLabel(active_.fourcc);
        active_.compressed = isCompressedFourcc(active_.fourcc) || chosen.compressed;
        active_.fps = chosen.fps;
        stride_ = fmt.fmt.pix.bytesperline;

        applyFrameInterval(req.fps > 0.0 ? req.fps : chosen.fps);

        if (!startStreaming()) {
            close();
            return false;
        }
        return true;
    }

    void close() override {
        // Deliberately does NOT clear err_: open() calls close() on its failure paths and the
        // diagnostic must survive for lastError().
        if (fd_ >= 0 && streaming_) {
            int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            xioctl(fd_, VIDIOC_STREAMOFF, &type);
        }
        streaming_ = false;

        for (auto& b : buffers_) {
            if (b.start && b.start != MAP_FAILED) ::munmap(b.start, b.length);
        }
        buffers_.clear();

        if (fd_ >= 0) {
            v4l2_requestbuffers rb{};
            rb.count = 0;
            rb.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            rb.memory = V4L2_MEMORY_MMAP;
            xioctl(fd_, VIDIOC_REQBUFS, &rb);
            ::close(fd_);
            fd_ = -1;
        }
        active_ = CameraMode{};
        stride_ = 0;
    }

    bool isOpen() const override { return fd_ >= 0 && streaming_; }

    CameraMode activeMode() const override { return active_; }

    std::string lastError() const override { return err_; }

    // ---- streaming -----------------------------------------------------------------------

    GrabResult grabFrame(CameraFrame& out) override {
        if (fd_ < 0 || !streaming_) return GrabResult::not_open;

        pollfd pfd{};
        pfd.fd = fd_;
        pfd.events = POLLIN;
        const int pr = ::poll(&pfd, 1, kPollTimeoutMs);
        if (pr == 0) return GrabResult::again;
        if (pr < 0) {
            if (errno == EINTR || errno == EAGAIN) return GrabResult::again;
            err_ = path_ + ": poll() failed: " + std::strerror(errno);
            return GrabResult::device_lost;
        }
        if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
            err_ = path_ + ": device disconnected while streaming";
            return GrabResult::device_lost;
        }
        if (!(pfd.revents & POLLIN)) return GrabResult::again;

        v4l2_buffer buf{};
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        if (xioctl(fd_, VIDIOC_DQBUF, &buf) != 0) {
            const int e = errno;
            if (e == EAGAIN) return GrabResult::again;
            if (e == EIO || e == ENODEV || e == ENXIO) {
                err_ = path_ + ": device lost during DQBUF (" + std::strerror(e) + ")";
                return GrabResult::device_lost;
            }
            err_ = path_ + ": VIDIOC_DQBUF failed: " + std::strerror(e);
            return GrabResult::device_lost;
        }
        if (buf.index >= buffers_.size()) {
            err_ = path_ + ": driver returned out-of-range buffer index";
            return GrabResult::device_lost;
        }

        GrabResult result;
        if (buf.flags & V4L2_BUF_FLAG_ERROR) {
            // Corrupt payload: the stream is alive, this one frame is not. Never surface it.
            result = GrabResult::again;
        } else {
            const auto* src = static_cast<const uint8_t*>(buffers_[buf.index].start);
            const size_t bytes =
                buf.bytesused > 0 ? buf.bytesused : buffers_[buf.index].length;
            result = decodeInto(src, bytes, out);
            if (result == GrabResult::ok) {
                out.synthetic = false;   // this backend only ever emits real sensor data
                out.sharpness = -1.0;    // callers score with frameSharpness()
                out.timestamp_sec = static_cast<double>(buf.timestamp.tv_sec) +
                                    static_cast<double>(buf.timestamp.tv_usec) * 1e-6;
                err_.clear();            // frames are flowing: lastError() is "" when healthy
            }
        }

        // Requeue immediately, whatever the decode outcome — a leaked buffer starves the stream.
        v4l2_buffer requeue{};
        requeue.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        requeue.memory = V4L2_MEMORY_MMAP;
        requeue.index = buf.index;
        if (xioctl(fd_, VIDIOC_QBUF, &requeue) != 0 && result == GrabResult::ok) {
            err_ = path_ + ": VIDIOC_QBUF failed: " + std::strerror(errno);
            return GrabResult::device_lost;
        }
        return result;
    }

    // ---- controls ------------------------------------------------------------------------

    ControlState getControl(CameraControl c) override {
        ControlState st;
        if (fd_ < 0) return st;

        const ControlMap m = mapControl(c);
        const CtrlRange r = queryCtrl(fd_, m.cid);
        if (!r.supported) return st;  // EINVAL / DISABLED — device does not have it
        st.supported = true;

        const CtrlRange ar = queryCtrl(fd_, m.auto_cid);
        st.auto_capable = ar.supported;
        if (ar.supported) readAutoRaw(fd_, m, st.automatic);

        int32_t raw = 0;
        if (getCtrlRaw(fd_, m.cid, raw)) st.value = normalizeCtrl(raw, r);
        return st;
    }

    bool setControl(CameraControl c, double normalized_value) override {
        if (fd_ < 0) {
            err_ = "camera is not open";
            return false;
        }
        const ControlMap m = mapControl(c);
        const CtrlRange r = queryCtrl(fd_, m.cid);
        if (!r.supported) {
            err_ = std::string("this camera does not expose a ") + controlName(c) + " control";
            return false;
        }
        // A manual value is meaningless while the driver is still auto-converging, and many
        // drivers reject S_CTRL on a control that auto owns — drop out of auto first.
        setAutoRaw(fd_, m, false);

        const int32_t raw = denormalizeCtrl(normalized_value, r);
        if (!setCtrlRaw(fd_, m.cid, raw)) {
            err_ = std::string("failed to set ") + controlName(c) + " to " +
                   std::to_string(raw) + ": " + std::strerror(errno);
            return false;
        }
        err_.clear();
        return true;
    }

    bool setControlAuto(CameraControl c, bool automatic) override {
        if (fd_ < 0) {
            err_ = "camera is not open";
            return false;
        }
        const ControlMap m = mapControl(c);
        if (m.auto_cid == 0 || !queryCtrl(fd_, m.auto_cid).supported) {
            err_ = std::string("this camera has no automatic ") + controlName(c) + " mode";
            return false;
        }
        if (!setAutoRaw(fd_, m, automatic)) {
            err_ = std::string("failed to switch ") + controlName(c) +
                   (automatic ? " to auto: " : " to manual: ") + std::strerror(errno);
            return false;
        }
        err_.clear();
        return true;
    }

    bool lockForCapture() override {
        if (fd_ < 0) {
            err_ = "camera is not open";
            return false;
        }
        static constexpr CameraControl kLockOrder[] = {
            CameraControl::Focus, CameraControl::Exposure, CameraControl::WhiteBalance,
            CameraControl::Gain};

        std::string failures;
        for (CameraControl c : kLockOrder) {
            const ControlMap m = mapControl(c);
            const CtrlRange r = queryCtrl(fd_, m.cid);
            if (!r.supported) continue;  // absent control is not a failure

            // Read the auto-converged value *before* leaving auto, then pin it. That is the
            // whole point: intrinsics and colour response must not drift mid-capture.
            int32_t converged = 0;
            const bool have_value = getCtrlRaw(fd_, m.cid, converged);

            if (!setAutoRaw(fd_, m, false)) {
                appendFailure(failures, controlName(c), "could not leave auto mode");
                continue;
            }
            if (!have_value) {
                appendFailure(failures, controlName(c), "current value is unreadable");
                continue;
            }
            if (!setCtrlRaw(fd_, m.cid, clamp_i32(converged, r.min, r.max))) {
                appendFailure(failures, controlName(c), std::strerror(errno));
            }
        }

        if (!failures.empty()) {
            err_ = "could not lock " + failures;
            return false;
        }
        err_.clear();
        return true;
    }

private:
    struct MappedBuffer {
        void*  start = nullptr;
        size_t length = 0;
    };

    int         fd_ = -1;
    bool        streaming_ = false;
    std::string path_;
    std::string err_;
    CameraMode  active_;
    uint32_t    stride_ = 0;
    std::vector<MappedBuffer> buffers_;

    // ---- small helpers ---------------------------------------------------------------------

    static std::string cstr(const uint8_t* raw, size_t cap) {
        const char* p = reinterpret_cast<const char*>(raw);
        size_t n = 0;
        while (n < cap && p[n] != '\0') ++n;
        std::string s(p, n);
        while (!s.empty() && s.back() == ' ') s.pop_back();
        return s;
    }

    static void appendFailure(std::string& acc, const char* name, const char* why) {
        if (!acc.empty()) acc += "; ";
        acc += name;
        acc += " (";
        acc += why;
        acc += ")";
    }

    static std::string describeFormats(const std::vector<CameraMode>& modes) {
        std::vector<std::string> labels;
        for (const auto& m : modes) {
            if (std::find(labels.begin(), labels.end(), m.format_label) == labels.end())
                labels.push_back(m.format_label);
        }
        std::string s;
        for (const auto& l : labels) {
            if (!s.empty()) s += ", ";
            s += l;
        }
        return s;
    }

    void setIoctlError(const char* what) {
        err_ = std::string(what) + " — " + errnoText(path_, errno);
    }

    // Accepts "/dev/video2", "video2", "2", or "" (first enumerated capture node).
    std::string resolvePath(const std::string& device_id) {
        if (!device_id.empty()) {
            if (device_id[0] == '/') return device_id;
            if (device_id.compare(0, 5, "video") == 0) return "/dev/" + device_id;
            bool numeric = true;
            for (char ch : device_id) {
                if (ch < '0' || ch > '9') { numeric = false; break; }
            }
            if (numeric) return "/dev/video" + device_id;
            return device_id;  // unrecognised shape: let ::open() produce the error
        }
        auto devices = listDevices();
        return devices.empty() ? std::string() : devices.front().device_id;
    }

    // ---- mode negotiation --------------------------------------------------------------------

    static bool pickMode(const std::vector<CameraMode>& modes, const OpenRequest& req,
                         CameraMode& chosen) {
        std::vector<const CameraMode*> usable;
        usable.reserve(modes.size());
        for (const auto& m : modes) {
            if (isDecodableFourcc(m.fourcc)) usable.push_back(&m);
        }
        if (usable.empty()) return false;

        const CameraMode* best = nullptr;

        if (req.width > 0 && req.height > 0) {
            // Closest available geometry; ties broken by fps proximity then uncompressed.
            long long best_dist = 0;
            double best_fps_err = 0.0;
            for (const auto* m : usable) {
                const long long dw = m->width - req.width;
                const long long dh = m->height - req.height;
                const long long dist = dw * dw + dh * dh;
                const double fps_err = req.fps > 0.0 ? std::fabs(m->fps - req.fps) : -m->fps;
                if (!best || dist < best_dist ||
                    (dist == best_dist &&
                     (fps_err < best_fps_err ||
                      (fps_err == best_fps_err && best->compressed && !m->compressed)))) {
                    best = m;
                    best_dist = dist;
                    best_fps_err = fps_err;
                }
            }
        } else if (req.prefer_max_resolution) {
            // Highest resolution wins. At that resolution prefer an uncompressed format, but
            // fall back to MJPG when compression is the only way to reach it — which is exactly
            // the case on every USB2 webcam above ~1280x720.
            long long best_area = 0;
            for (const auto* m : usable) {
                const long long area = static_cast<long long>(m->width) * m->height;
                if (area > best_area) best_area = area;
            }
            for (const auto* m : usable) {
                if (static_cast<long long>(m->width) * m->height != best_area) continue;
                if (!best || (best->compressed && !m->compressed) ||
                    (best->compressed == m->compressed && m->fps > best->fps)) {
                    best = m;
                }
            }
        } else {
            // Framerate-first: highest fps, then largest frame at that fps.
            for (const auto* m : usable) {
                if (!best || m->fps > best->fps ||
                    (m->fps == best->fps &&
                     static_cast<long long>(m->width) * m->height >
                         static_cast<long long>(best->width) * best->height)) {
                    best = m;
                }
            }
        }

        if (!best) return false;
        chosen = *best;
        return true;
    }

    // S_PARM is optional in V4L2; a driver that does not advertise TIMEPERFRAME simply runs at
    // its own rate, which is fine. Always read back so activeMode() reports the truth.
    void applyFrameInterval(double fps) {
        v4l2_streamparm parm{};
        parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        if (xioctl(fd_, VIDIOC_G_PARM, &parm) != 0) return;

        if (fps > 0.0 && (parm.parm.capture.capability & V4L2_CAP_TIMEPERFRAME)) {
            v4l2_streamparm want{};
            want.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            const double rounded = std::floor(fps + 0.5);
            if (std::fabs(fps - rounded) < 1e-6) {
                want.parm.capture.timeperframe.numerator = 1;
                want.parm.capture.timeperframe.denominator =
                    static_cast<uint32_t>(std::llround(rounded));
            } else {
                // Rational form for the 7.5 / 14.98 style rates some cameras advertise.
                want.parm.capture.timeperframe.numerator = 1000;
                want.parm.capture.timeperframe.denominator =
                    static_cast<uint32_t>(std::llround(fps * 1000.0));
            }
            if (xioctl(fd_, VIDIOC_S_PARM, &want) == 0) parm = want;
        }

        const auto& tpf = parm.parm.capture.timeperframe;
        if (tpf.numerator > 0 && tpf.denominator > 0) {
            active_.fps = static_cast<double>(tpf.denominator) / static_cast<double>(tpf.numerator);
        }
    }

    // ---- mmap streaming ----------------------------------------------------------------------

    bool startStreaming() {
        v4l2_requestbuffers rb{};
        rb.count = kRequestedBuffers;
        rb.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        rb.memory = V4L2_MEMORY_MMAP;
        if (xioctl(fd_, VIDIOC_REQBUFS, &rb) != 0) {
            if (errno == EINVAL) {
                err_ = path_ + " does not support mmap streaming I/O";
            } else {
                setIoctlError("VIDIOC_REQBUFS");
            }
            return false;
        }
        if (rb.count == 0) {
            err_ = path_ + ": driver granted no capture buffers";
            return false;
        }

        buffers_.resize(rb.count);
        for (uint32_t i = 0; i < rb.count; ++i) {
            v4l2_buffer buf{};
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            buf.index = i;
            if (xioctl(fd_, VIDIOC_QUERYBUF, &buf) != 0) {
                setIoctlError("VIDIOC_QUERYBUF");
                return false;
            }
            void* p = ::mmap(nullptr, buf.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd_,
                             buf.m.offset);
            if (p == MAP_FAILED) {
                err_ = path_ + ": mmap of capture buffer failed: " + std::strerror(errno);
                buffers_[i].start = nullptr;
                return false;
            }
            buffers_[i].start = p;
            buffers_[i].length = buf.length;
        }

        for (uint32_t i = 0; i < rb.count; ++i) {
            v4l2_buffer buf{};
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            buf.index = i;
            if (xioctl(fd_, VIDIOC_QBUF, &buf) != 0) {
                setIoctlError("VIDIOC_QBUF");
                return false;
            }
        }

        int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        if (xioctl(fd_, VIDIOC_STREAMON, &type) != 0) {
            setIoctlError("VIDIOC_STREAMON");
            return false;
        }
        streaming_ = true;
        return true;
    }

    // ---- decode ------------------------------------------------------------------------------

    // Returns ok only when `out` holds genuine decoded sensor pixels. There is deliberately no
    // "fill grey and claim success" path anywhere below.
    GrabResult decodeInto(const uint8_t* src, size_t bytes, CameraFrame& out) {
        if (!src || bytes == 0) {
            err_ = path_ + ": driver returned an empty buffer";
            return GrabResult::decode_failed;
        }
        const int w = active_.width;
        const int h = active_.height;

        switch (active_.fourcc) {
            case V4L2_PIX_FMT_MJPEG:
            case V4L2_PIX_FMT_JPEG:
                return decodeJpeg(src, bytes, out);
            // Packed 4:2:2, distinguished only by byte order within each two-pixel group.
            case V4L2_PIX_FMT_YUYV:
                return decodePacked422(src, bytes, w, h, 0, 1, 2, 3, out);
            case V4L2_PIX_FMT_UYVY:
                return decodePacked422(src, bytes, w, h, 1, 0, 3, 2, out);
            case V4L2_PIX_FMT_YVYU:
                return decodePacked422(src, bytes, w, h, 0, 3, 2, 1, out);
            // Semi-planar 4:2:0 — NV21 is NV12 with the chroma pair swapped.
            case V4L2_PIX_FMT_NV12:
                return decodeSemiPlanar420(src, bytes, w, h, 0, 1, out);
            case V4L2_PIX_FMT_NV21:
                return decodeSemiPlanar420(src, bytes, w, h, 1, 0, out);
            // Planar 4:2:0 — YV12 is I420 with the U and V planes swapped.
            case V4L2_PIX_FMT_YUV420:
                return decodePlanar420(src, bytes, w, h, /*v_first=*/false, out);
            case V4L2_PIX_FMT_YVU420:
                return decodePlanar420(src, bytes, w, h, /*v_first=*/true, out);
            // A genuine monochrome sensor. Unlike the v1 backend's fabricated grey buffer, this
            // is real luma off the device — machine-vision cameras are often mono on purpose.
            case V4L2_PIX_FMT_GREY:
                return decodeGrey(src, bytes, w, h, out);
            case V4L2_PIX_FMT_RGB24:
                return decodePacked24(src, bytes, w, h, /*bgr=*/false, out);
            case V4L2_PIX_FMT_BGR24:
                return decodePacked24(src, bytes, w, h, /*bgr=*/true, out);
            default:
                err_ = path_ + ": unsupported pixel format '" + fourccLabel(active_.fourcc) +
                       "' — no decode path in this build";
                return GrabResult::decode_failed;
        }
    }

    uint32_t rowStride(uint32_t minimum) const {
        return stride_ >= minimum ? stride_ : minimum;
    }

    static void prepare(CameraFrame& out, int w, int h) {
        out.width = w;
        out.height = h;
        out.rgba.resize(static_cast<size_t>(w) * static_cast<size_t>(h) * 4);
    }

    GrabResult shortBuffer(size_t have, size_t need) {
        err_ = path_ + ": truncated frame (" + std::to_string(have) + " of " +
               std::to_string(need) + " bytes for " + fourccLabel(active_.fourcc) + ")";
        return GrabResult::decode_failed;
    }

    // YUYV / UYVY / YVYU: two pixels per four bytes, sharing one chroma pair.
    GrabResult decodePacked422(const uint8_t* src, size_t bytes, int w, int h, int o_y0, int o_u,
                               int o_y1, int o_v, CameraFrame& out) {
        const uint32_t stride = rowStride(static_cast<uint32_t>(w) * 2);
        const size_t need = static_cast<size_t>(stride) * h;
        if (bytes < need) return shortBuffer(bytes, need);

        prepare(out, w, h);
        uint8_t* dst = out.rgba.data();
        for (int y = 0; y < h; ++y) {
            const uint8_t* row = src + static_cast<size_t>(stride) * y;
            uint8_t* drow = dst + static_cast<size_t>(w) * 4 * y;
            for (int x = 0; x + 1 < w; x += 2) {
                const int u = row[o_u];
                const int v = row[o_v];
                yuvToRgba(row[o_y0], u, v, drow);
                yuvToRgba(row[o_y1], u, v, drow + 4);
                row += 4;
                drow += 8;
            }
            if (w & 1) {  // odd width: the last pixel reuses the final chroma pair
                yuvToRgba(row[o_y0], row[o_u], row[o_v], drow);
            }
        }
        return GrabResult::ok;
    }

    // NV12 / NV21: full-res luma plane followed by an interleaved half-res chroma plane.
    GrabResult decodeSemiPlanar420(const uint8_t* src, size_t bytes, int w, int h, int u_off,
                                   int v_off, CameraFrame& out) {
        const uint32_t stride = rowStride(static_cast<uint32_t>(w));
        const int ch = (h + 1) / 2;
        const size_t need = static_cast<size_t>(stride) * h + static_cast<size_t>(stride) * ch;
        if (bytes < need) return shortBuffer(bytes, need);

        prepare(out, w, h);
        const uint8_t* yp = src;
        const uint8_t* uvp = src + static_cast<size_t>(stride) * h;
        uint8_t* dst = out.rgba.data();
        for (int y = 0; y < h; ++y) {
            const uint8_t* yrow = yp + static_cast<size_t>(stride) * y;
            const uint8_t* uvrow = uvp + static_cast<size_t>(stride) * (y / 2);
            uint8_t* drow = dst + static_cast<size_t>(w) * 4 * y;
            for (int x = 0; x < w; ++x) {
                const int c = (x / 2) * 2;
                yuvToRgba(yrow[x], uvrow[c + u_off], uvrow[c + v_off],
                          drow + static_cast<size_t>(x) * 4);
            }
        }
        return GrabResult::ok;
    }

    // YU12 (I420) / YV12: three separate planes, chroma at half resolution in both axes.
    GrabResult decodePlanar420(const uint8_t* src, size_t bytes, int w, int h, bool v_first,
                               CameraFrame& out) {
        const uint32_t stride = rowStride(static_cast<uint32_t>(w));
        const uint32_t cstride = (stride + 1) / 2;
        const int cw = (w + 1) / 2;
        const int ch = (h + 1) / 2;
        const size_t ysize = static_cast<size_t>(stride) * h;
        const size_t csize = static_cast<size_t>(cstride) * ch;
        const size_t need = ysize + 2 * csize;
        if (bytes < need) return shortBuffer(bytes, need);

        prepare(out, w, h);
        const uint8_t* yp = src;
        const uint8_t* first = src + ysize;
        const uint8_t* second = first + csize;
        const uint8_t* up = v_first ? second : first;
        const uint8_t* vp = v_first ? first : second;
        uint8_t* dst = out.rgba.data();
        for (int y = 0; y < h; ++y) {
            const uint8_t* yrow = yp + static_cast<size_t>(stride) * y;
            const uint8_t* urow = up + static_cast<size_t>(cstride) * (y / 2);
            const uint8_t* vrow = vp + static_cast<size_t>(cstride) * (y / 2);
            uint8_t* drow = dst + static_cast<size_t>(w) * 4 * y;
            for (int x = 0; x < w; ++x) {
                const int c = x / 2 < cw ? x / 2 : cw - 1;
                yuvToRgba(yrow[x], urow[c], vrow[c], drow + static_cast<size_t>(x) * 4);
            }
        }
        return GrabResult::ok;
    }

    GrabResult decodeGrey(const uint8_t* src, size_t bytes, int w, int h, CameraFrame& out) {
        const uint32_t stride = rowStride(static_cast<uint32_t>(w));
        const size_t need = static_cast<size_t>(stride) * h;
        if (bytes < need) return shortBuffer(bytes, need);

        prepare(out, w, h);
        uint8_t* dst = out.rgba.data();
        for (int y = 0; y < h; ++y) {
            const uint8_t* row = src + static_cast<size_t>(stride) * y;
            uint8_t* drow = dst + static_cast<size_t>(w) * 4 * y;
            for (int x = 0; x < w; ++x) {
                // Mono sensors are full-range: replicate luma across RGB without a Y-16 bias.
                const uint8_t l = row[x];
                drow[0] = l;
                drow[1] = l;
                drow[2] = l;
                drow[3] = 255;
                drow += 4;
            }
        }
        return GrabResult::ok;
    }

    GrabResult decodePacked24(const uint8_t* src, size_t bytes, int w, int h, bool bgr,
                              CameraFrame& out) {
        const uint32_t stride = rowStride(static_cast<uint32_t>(w) * 3);
        const size_t need = static_cast<size_t>(stride) * h;
        if (bytes < need) return shortBuffer(bytes, need);

        prepare(out, w, h);
        uint8_t* dst = out.rgba.data();
        const int r_off = bgr ? 2 : 0;
        const int b_off = bgr ? 0 : 2;
        for (int y = 0; y < h; ++y) {
            const uint8_t* row = src + static_cast<size_t>(stride) * y;
            uint8_t* drow = dst + static_cast<size_t>(w) * 4 * y;
            for (int x = 0; x < w; ++x) {
                drow[0] = row[r_off];
                drow[1] = row[1];
                drow[2] = row[b_off];
                drow[3] = 255;
                row += 3;
                drow += 4;
            }
        }
        return GrabResult::ok;
    }

    GrabResult decodeJpeg(const uint8_t* src, size_t bytes, CameraFrame& out) {
        const uint8_t* payload = src;
        size_t payload_len = bytes;

        std::vector<uint8_t> repaired;
        size_t sos_off = 0;
        bool has_dht = false;
        if (scanJpegMarkers(src, bytes, sos_off, has_dht) && !has_dht) {
            repaired.reserve(bytes + sizeof(kStandardDht));
            repaired.insert(repaired.end(), src, src + sos_off);
            repaired.insert(repaired.end(), kStandardDht,
                            kStandardDht + sizeof(kStandardDht));
            repaired.insert(repaired.end(), src + sos_off, src + bytes);
            payload = repaired.data();
            payload_len = repaired.size();
        }

        int w = 0, h = 0, channels = 0;
        stbi_uc* px = stbi_load_from_memory(payload, static_cast<int>(payload_len), &w, &h,
                                            &channels, STBI_rgb_alpha);
        if (!px) {
            const char* why = stbi_failure_reason();
            err_ = path_ + ": MJPEG decode failed (" + (why ? why : "unknown") + ")";
            return GrabResult::decode_failed;
        }
        if (w <= 0 || h <= 0) {
            stbi_image_free(px);
            err_ = path_ + ": MJPEG decode produced an empty image";
            return GrabResult::decode_failed;
        }

        prepare(out, w, h);
        std::memcpy(out.rgba.data(), px, static_cast<size_t>(w) * h * 4);
        stbi_image_free(px);
        return GrabResult::ok;
    }
};

std::unique_ptr<CameraBackend> createV4L2Camera() {
    return std::make_unique<V4L2CameraBackend>();
}

}  // namespace chudware::platform

#endif  // __linux__
