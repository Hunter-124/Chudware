// webcam_capture.cpp — live webcam capture console for the RE Sources stage (RE-40 rebuild).
//
// See webcam_capture.h for the honesty invariants this file enforces. The short version:
//   • A synthetic frame is refused at the shutter, in code — the UI disable is only the polite
//     half of the block. buildStubFrameRGBA() output can therefore never reach a session.
//   • Coverage is get_reveng_coverage(), not a local counter, and the ring says out loud whether
//     the backend measured it from poses or estimated it from a photo count.
//   • Blur is frameSharpness() vs kSharpnessBlurThreshold, surfaced live before the shutter.
//   • Every reveng_add_images round-trip reports its outcome and writes the resulting quality
//     back onto the gallery entry.
//
// GL-thread only. RPC is always async. grabFrame() is polled non-blocking: GrabResult::again is
// the steady state and never triggers a reopen.
#include "webcam_capture.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <memory>
#include <string>
#include <system_error>
#include <vector>

#include "imgui.h"
#include "gl_compat.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/metrics.h"
#include "platform/camera.h"

#include "reveng_internal.h"   // S() — gallery push + capability mirror
#include "reveng_widgets.h"    // ui::reveng::kit — the RE visual vocabulary

// stb_image_write — implementation compiled in host/src/stb_impl.cpp; include header only.
#include "stb_image_write.h"

namespace ui {
namespace reveng {

namespace {

namespace cam = chudware::platform;
namespace mx  = ui::metrics;

// ---------------------------------------------------------------------------
// Coverage plan — OpenScan-style 11-shot dome (2 rings + top). Purely a DRAWING
// plan: which of these are actually satisfied comes from get_reveng_coverage().
// ---------------------------------------------------------------------------
struct ShotPos { float az; float el; };
const ShotPos kShotPlan[11] = {
    {   0, 30}, { 36, 30}, { 72, 30}, {108, 30}, {144, 30},
    { 18, 60}, { 54, 60}, { 90, 60}, {126, 60}, {162, 60},
    {  0, 90},
};
constexpr int kPlanSize = 11;
constexpr int kStripMax = 6;      // capture strip depth
constexpr int kCtlCount = 6;      // CameraControl enumerators
// Fixed placeholder resolution. Deliberately small: the synthetic frame is a "there is no camera"
// sign, and painting it at sensor resolution would cost real frame time for zero information.
constexpr int kStubW = 640;
constexpr int kStubH = 360;

// ---------------------------------------------------------------------------
// Panel-local state. File-static, GL-thread only, no mutex (same contract as
// the reveng store itself).
// ---------------------------------------------------------------------------

// One entry of the inline capture strip. Owns its own small GL texture so a shot appears
// instantly, without waiting for the gallery's async decode of the JPEG we just wrote.
struct ShotThumb {
    GLuint      tex = 0;
    std::string path;
    std::string badge;        // "ok" | "blurred"
    double      sharpness = -1.0;
};

struct WebcamState {
    // ---- preview texture ----
    GLuint texId  = 0;
    int    allocW = 0;
    int    allocH = 0;

    // ---- latest frame (source of truth for both preview and shutter) ----
    std::vector<unsigned char> lastRgba;
    int    frameW = 0;
    int    frameH = 0;
    bool   haveFrame  = false;
    // Defaults to TRUE: until a sensor frame has actually landed, whatever is on screen is ours,
    // not the camera's. Fail-closed is the whole point of this flag.
    bool   frameSynthetic = true;
    double frameSharp = -1.0;

    // ---- backend ----
    std::unique_ptr<cam::CameraBackend> backend;
    std::vector<cam::CameraInfo> devices;
    std::vector<cam::CameraMode> modes;
    std::vector<std::string>     modeLabels;
    int    deviceIdx = -1;
    int    modeIdx   = -1;          // -1 = "Auto (highest resolution)"
    bool   enumDone  = false;
    bool   modesDone = false;
    bool   opened    = false;
    cam::CameraMode active{};
    std::string openErr;            // verbatim lastError() from the failing open()
    std::string grabNote;           // decode_failed / device_lost detail
    kit::Level  grabLevel = kit::Level::Neutral;

    // ---- controls ----
    cam::ControlState ctl[kCtlCount];
    float  ctlValue[kCtlCount] = {};
    double ctlRefreshT = -100.0;
    bool   locked = false;
    std::string lockMsg;
    kit::Level  lockLevel = kit::Level::Neutral;

    // ---- coverage (get_reveng_coverage) ----
    bool   covCaptured[kPlanSize] = {};
    bool   covBlurred[kPlanSize]  = {};
    int    covCapturedN = 0;
    int    covBlurredN  = 0;
    std::string covMethod;          // "pose_binned" | "count_proxy" | ""
    std::string covQuality;         // "good" | "partial" | "insufficient"
    bool   covLoaded  = false;
    bool   covPending = false;
    double covLastT   = -100.0;
    std::string covErr;
    std::string covSession;

    // ---- shutter ----
    bool   addPending = false;
    int    shotSeq    = 0;
    std::string lastShotPath;
    std::string status;
    kit::Level  statusLevel = kit::Level::Neutral;
    bool   blurArmed = false;       // a blurry press happened; "Capture anyway" is offered
    std::vector<ShotThumb> strip;

    // ---- throttles ----
    double lastGrabT = -100.0;
    double lastStubT = -100.0;
    double lastOpenT = -100.0;

    // ---- stub animation phase ----
    float  phase = 0.0f;
};

WebcamState g_wcam;

// ---------------------------------------------------------------------------
// Colour helpers — every draw-list colour is derived from the palette or the kit's
// semantic levels. No literal ImU32 anywhere in this file.
// ---------------------------------------------------------------------------

ImU32 lvlCol(kit::Level l, float alphaMul = 1.0f) {
    ImVec4 v = kit::levelColorVec(l);
    v.w *= alphaMul;
    return ImGui::GetColorU32(v);
}

ImU32 palCol(const float c[4], float alphaMul = 1.0f) {
    return ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], c[3] * alphaMul));
}

// ---------------------------------------------------------------------------
// Synthetic placeholder frame.
//
// Deliberately NOT photographic: a hazard-striped field in the theme's warn colour, so that a
// screenshot of the panel can never be mistaken for a capture. It exists so the panel (and the
// headless CHUDWARE_SHOT scenarios) still show something structured when no camera is present —
// and every consumer of it treats it as poisoned.
// ---------------------------------------------------------------------------
void buildStubFrameRGBA(std::vector<unsigned char>& px, int w, int h, float phase) {
    px.resize((size_t)w * h * 4);
    const Palette& pal = ui::palette;
    const auto ch = [](float v) -> unsigned char {
        const int i = (int)(v * 255.0f + 0.5f);
        return (unsigned char)(i < 0 ? 0 : (i > 255 ? 255 : i));
    };
    const unsigned char bg[3]  = { ch(pal.panelBg[0]),   ch(pal.panelBg[1]),   ch(pal.panelBg[2])   };
    const unsigned char st[3]  = { ch(pal.statusWarn[0]),ch(pal.statusWarn[1]),ch(pal.statusWarn[2])};
    const unsigned char xh[3]  = { ch(pal.textDim[0]),   ch(pal.textDim[1]),   ch(pal.textDim[2])   };

    const int band = std::max(8, w / 24);
    const int off  = (int)(phase * (float)(band * 2)) % (band * 2);
    for (int row = 0; row < h; ++row) {
        unsigned char* dst = px.data() + (size_t)row * w * 4;
        for (int col = 0; col < w; ++col) {
            const int d = ((col + row + off) % (band * 2));
            const bool stripe = d < band;
            // Faint stripes: a 12% wash of the warn colour over the panel background.
            const unsigned char* base = bg;
            unsigned char r = base[0], g = base[1], b = base[2];
            if (stripe) {
                r = (unsigned char)((r * 7 + st[0] * 1) / 8);
                g = (unsigned char)((g * 7 + st[1] * 1) / 8);
                b = (unsigned char)((b * 7 + st[2] * 1) / 8);
            }
            const bool cross = (std::abs(col - w / 2) < 2) || (std::abs(row - h / 2) < 2);
            if (cross) { r = xh[0]; g = xh[1]; b = xh[2]; }
            dst[col * 4 + 0] = r;
            dst[col * 4 + 1] = g;
            dst[col * 4 + 2] = b;
            dst[col * 4 + 3] = 255;
        }
    }
}

// ---------------------------------------------------------------------------
// GL texture helpers
// ---------------------------------------------------------------------------

void uploadPreviewTex(const unsigned char* px, int w, int h) {
    if (!px || w <= 0 || h <= 0) return;
    WebcamState& ws = g_wcam;
    if (!ws.texId) {
        GLuint id = 0;
        glGenTextures(1, &id);
        ws.texId  = id;
        ws.allocW = 0;
        ws.allocH = 0;
    }
    glBindTexture(GL_TEXTURE_2D, ws.texId);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    if (w != ws.allocW || h != ws.allocH) {
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
        ws.allocW = w;
        ws.allocH = h;
    } else {
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, px);
    }
    glBindTexture(GL_TEXTURE_2D, 0);
}

// Centre-crop to square, box-downsample to side x side, upload. Used for the capture strip so a
// shot is visible the instant it is written, independent of the gallery's decode queue.
GLuint makeThumbTexture(const std::vector<unsigned char>& rgba, int w, int h, int side) {
    if (w <= 0 || h <= 0 || side <= 0) return 0;
    if (rgba.size() < (size_t)w * (size_t)h * 4u) return 0;

    const int crop = std::min(w, h);
    const int ox   = (w - crop) / 2;
    const int oy   = (h - crop) / 2;
    std::vector<unsigned char> out((size_t)side * side * 4);
    for (int y = 0; y < side; ++y) {
        const int sy0 = oy + (int)((int64_t)y * crop / side);
        const int sy1 = std::max(sy0 + 1, oy + (int)((int64_t)(y + 1) * crop / side));
        for (int x = 0; x < side; ++x) {
            const int sx0 = ox + (int)((int64_t)x * crop / side);
            const int sx1 = std::max(sx0 + 1, ox + (int)((int64_t)(x + 1) * crop / side));
            uint32_t acc[3] = {0, 0, 0};
            uint32_t n = 0;
            for (int sy = sy0; sy < sy1; ++sy) {
                const unsigned char* row = rgba.data() + (size_t)sy * (size_t)w * 4u;
                for (int sx = sx0; sx < sx1; ++sx) {
                    acc[0] += row[(size_t)sx * 4 + 0];
                    acc[1] += row[(size_t)sx * 4 + 1];
                    acc[2] += row[(size_t)sx * 4 + 2];
                    ++n;
                }
            }
            if (n == 0) n = 1;
            unsigned char* d = out.data() + ((size_t)y * side + x) * 4;
            d[0] = (unsigned char)(acc[0] / n);
            d[1] = (unsigned char)(acc[1] / n);
            d[2] = (unsigned char)(acc[2] / n);
            d[3] = 255;
        }
    }
    GLuint id = 0;
    glGenTextures(1, &id);
    glBindTexture(GL_TEXTURE_2D, id);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, side, side, 0, GL_RGBA, GL_UNSIGNED_BYTE, out.data());
    glBindTexture(GL_TEXTURE_2D, 0);
    return id;
}

void clearStrip() {
    for (ShotThumb& t : g_wcam.strip)
        if (t.tex) glDeleteTextures(1, &t.tex);
    g_wcam.strip.clear();
}

// ---------------------------------------------------------------------------
// Device lifecycle
// ---------------------------------------------------------------------------

std::string deviceLabel(const cam::CameraInfo& d) {
    return d.label.empty() ? d.device_id : d.label;
}

void rebuildModeLabels() {
    WebcamState& ws = g_wcam;
    ws.modeLabels.clear();
    ws.modeLabels.reserve(ws.modes.size());
    for (const cam::CameraMode& m : ws.modes) {
        char buf[96];
        std::snprintf(buf, sizeof(buf), "%dx%d %s @ %.0f",
                      m.width, m.height,
                      m.format_label.empty() ? "?" : m.format_label.c_str(), m.fps);
        ws.modeLabels.emplace_back(buf);
    }
}

// Drop the open device so the next ensureCamera() renegotiates. Any pending lock is void:
// reopening resets focus/exposure to the driver defaults, and claiming otherwise would be a lie.
void dropDevice() {
    WebcamState& ws = g_wcam;
    if (ws.backend) ws.backend->close();
    ws.opened    = false;
    ws.locked    = false;
    ws.lockMsg.clear();
    ws.lockLevel = kit::Level::Neutral;
    ws.active    = cam::CameraMode{};
    ws.haveFrame = false;
    ws.frameSynthetic = true;
    ws.frameSharp = -1.0;
    ws.lastOpenT  = -100.0;
    ws.ctlRefreshT = -100.0;
}

void ensureCamera() {
    WebcamState& ws = g_wcam;
    if (!ws.backend) {
        ws.backend  = cam::CameraBackend::create();
        ws.enumDone = false;
        ws.opened   = false;
    }
    if (!ws.backend) return;

    if (!ws.enumDone) {
        ws.enumDone  = true;
        ws.devices   = ws.backend->listDevices();
        ws.modesDone = false;
        ws.modes.clear();
        ws.modeLabels.clear();
        ws.deviceIdx = ws.devices.empty() ? -1 : 0;
        if (ws.devices.empty()) {
            const std::string e = ws.backend->lastError();
            ws.openErr = e.empty()
                ? "No camera devices were reported by the platform backend."
                : e;
        } else {
            ws.openErr.clear();
        }
    }

    if (ws.deviceIdx >= 0 && ws.deviceIdx < (int)ws.devices.size() && !ws.modesDone) {
        ws.modesDone = true;
        ws.modes     = ws.backend->listModes(ws.devices[(size_t)ws.deviceIdx].device_id);
        rebuildModeLabels();
        ws.modeIdx = -1;   // default to the backend's max-resolution negotiation
    }

    if (!ws.opened && ws.deviceIdx >= 0 && ws.deviceIdx < (int)ws.devices.size()) {
        const double now = ImGui::GetTime();
        if (now - ws.lastOpenT < 1.0) return;   // throttle retries to ~1 Hz
        ws.lastOpenT = now;

        cam::OpenRequest req;
        if (ws.modeIdx >= 0 && ws.modeIdx < (int)ws.modes.size()) {
            const cam::CameraMode& m = ws.modes[(size_t)ws.modeIdx];
            req.width  = m.width;
            req.height = m.height;
            req.fps    = m.fps;
            req.prefer_max_resolution = false;
        } else {
            req.prefer_max_resolution = true;   // photogrammetry wants pixels, not framerate
        }

        if (ws.backend->open(ws.devices[(size_t)ws.deviceIdx].device_id, req)) {
            ws.opened  = true;
            ws.active  = ws.backend->activeMode();
            ws.openErr.clear();
            ws.grabNote.clear();
            ws.grabLevel   = kit::Level::Neutral;
            ws.ctlRefreshT = -100.0;
        } else {
            ws.opened = false;
            const std::string e = ws.backend->lastError();
            ws.openErr = e.empty()
                ? ("Could not open " + deviceLabel(ws.devices[(size_t)ws.deviceIdx]) +
                   " (no reason reported by the backend).")
                : e;
        }
    }
}

// ---------------------------------------------------------------------------
// Frame pump. Non-blocking: GrabResult::again is the normal steady state and must never
// trigger a reopen — that was the old bug that reopened the device several times a second
// on any driver that buffers.
// ---------------------------------------------------------------------------
void updatePreviewFrame() {
    WebcamState& ws = g_wcam;
    ensureCamera();

    const double now = ImGui::GetTime();
    // Poll a little above display rate. `again` is cheap, so this is only about not burning a
    // whole core on drivers that memcpy on every DQBUF.
    if (ws.haveFrame && now - ws.lastGrabT < (1.0 / 60.0)) return;
    ws.lastGrabT = now;

    cam::GrabResult gr = cam::GrabResult::not_open;
    cam::CameraFrame frame;
    if (ws.opened && ws.backend) gr = ws.backend->grabFrame(frame);

    switch (gr) {
        case cam::GrabResult::ok: {
            const bool valid = frame.width > 0 && frame.height > 0 &&
                               frame.rgba.size() >= (size_t)frame.width * frame.height * 4u;
            if (valid) {
                ws.frameSynthetic = frame.synthetic;
                ws.frameSharp = frame.sharpness >= 0.0 ? frame.sharpness
                                                       : cam::frameSharpness(frame);
                ws.frameW  = frame.width;
                ws.frameH  = frame.height;
                ws.lastRgba = std::move(frame.rgba);
                uploadPreviewTex(ws.lastRgba.data(), ws.frameW, ws.frameH);
                ws.haveFrame = true;
                ws.grabNote.clear();
                ws.grabLevel = kit::Level::Neutral;
                if (!frame.synthetic) ws.openErr.clear();
                return;
            }
            ws.grabNote  = "Backend returned an ok frame with no pixels.";
            ws.grabLevel = kit::Level::Error;
            break;
        }
        case cam::GrabResult::again:
            // No buffer queued yet. Keep showing the previous frame; do NOT reopen.
            if (ws.haveFrame) return;
            break;
        case cam::GrabResult::decode_failed: {
            const std::string e = ws.backend ? ws.backend->lastError() : std::string();
            ws.grabNote = e.empty()
                ? "Frame decode failed — the negotiated pixel format has no decode path. "
                  "Pick a different resolution/format above."
                : e;
            ws.grabLevel = kit::Level::Error;
            // Keep the last good frame on screen rather than flashing a grey hole.
            if (ws.haveFrame) return;
            break;
        }
        case cam::GrabResult::device_lost: {
            const std::string e = ws.backend ? ws.backend->lastError() : std::string();
            ws.grabNote = e.empty() ? "Camera disconnected — retrying." : e;
            ws.grabLevel = kit::Level::Error;
            dropDevice();
            break;
        }
        case cam::GrabResult::not_open:
        default:
            break;
    }

    // ---- synthetic fallback: structured placeholder, permanently barred from the shutter ----
    // Regenerated at a lazy ~8 Hz and at a fixed small size: it is a placeholder, not a video
    // feed, and there is no reason to burn frame time painting and re-uploading it at 60 Hz.
    ws.frameSynthetic = true;
    ws.frameSharp = -1.0;
    if (ws.haveFrame && now - ws.lastStubT < 0.125) return;
    ws.lastStubT = now;
    ws.phase += 0.03f;
    if (ws.phase > 1.0f) ws.phase -= 1.0f;
    const int sw = kStubW;
    const int sh = kStubH;
    ws.frameW = sw;
    ws.frameH = sh;
    buildStubFrameRGBA(ws.lastRgba, sw, sh, ws.phase);
    uploadPreviewTex(ws.lastRgba.data(), sw, sh);
    ws.haveFrame = true;
}

// ---------------------------------------------------------------------------
// Coverage — the real thing, from the worker.
// ---------------------------------------------------------------------------

void refreshCoverage(AppState& st, const std::string& sid, bool force) {
    WebcamState& ws = g_wcam;
    if (sid.empty() || ws.covPending) return;
    const double now = ImGui::GetTime();
    if (!force && ws.covSession == sid && now - ws.covLastT < 2.0) return;
    if (ws.covSession != sid) {
        ws.covLoaded = false;
        ws.covErr.clear();
    }
    ws.covSession = sid;
    ws.covLastT   = now;
    ws.covPending = true;

    nlohmann::json params;
    params["session_id"] = sid;
    st.rpc.call("get_reveng_coverage", params, [](const core::RpcResult& r) {
        WebcamState& w = g_wcam;
        w.covPending = false;
        if (!r.ok) {
            w.covErr = r.err.empty() ? "get_reveng_coverage failed" : r.err;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            w.covErr = r.result.is_object()
                ? r.result.value("reason", std::string("get_reveng_coverage failed"))
                : std::string("get_reveng_coverage returned a non-object");
            return;
        }
        const nlohmann::json& cov = r.result.value("coverage", nlohmann::json::object());

        for (int i = 0; i < kPlanSize; ++i) { w.covCaptured[i] = false; w.covBlurred[i] = false; }
        std::vector<int> capturedIdx;
        if (cov.contains("captured") && cov["captured"].is_array()) {
            for (const auto& v : cov["captured"]) {
                if (!v.is_number_integer()) continue;
                const int idx = v.get<int>();
                if (idx >= 0 && idx < kPlanSize) { w.covCaptured[idx] = true; capturedIdx.push_back(idx); }
            }
        }
        // blur_flags is parallel to `captured` in the contract; tolerate a plan-length array too.
        if (cov.contains("blur_flags") && cov["blur_flags"].is_array()) {
            const auto& bf = cov["blur_flags"];
            if (bf.size() == capturedIdx.size()) {
                for (size_t i = 0; i < capturedIdx.size(); ++i)
                    if (bf[i].is_boolean() && bf[i].get<bool>()) w.covBlurred[capturedIdx[i]] = true;
            } else {
                for (size_t i = 0; i < bf.size() && i < (size_t)kPlanSize; ++i)
                    if (bf[i].is_boolean() && bf[i].get<bool>()) w.covBlurred[i] = true;
            }
        }
        w.covCapturedN = 0;
        w.covBlurredN  = 0;
        for (int i = 0; i < kPlanSize; ++i) {
            if (w.covCaptured[i]) ++w.covCapturedN;
            if (w.covBlurred[i])  ++w.covBlurredN;
        }
        w.covMethod  = cov.value("method", std::string());
        w.covQuality = cov.value("quality", std::string());
        w.covLoaded  = true;
        w.covErr.clear();
    });
}

// ---------------------------------------------------------------------------
// Session directory
// ---------------------------------------------------------------------------

std::string defaultSessionDir(const std::string& session_id) {
    // Mirror services/mcp/reveng_tools._reveng_dir so JPEGs land next to the session the worker
    // already knows about.
    const char* override_dir = std::getenv("CHUDWARE_REVENG_DIR");
    std::string root;
    if (override_dir && override_dir[0]) {
        root = override_dir;
    } else {
        const char* xdg = std::getenv("XDG_DATA_HOME");
        if (xdg && xdg[0]) {
            root = std::string(xdg) + "/Chudware/reveng";
        } else {
            const char* home = std::getenv("HOME");
            root = (home && home[0] ? std::string(home) : std::string(".")) +
                   "/.local/share/Chudware/reveng";
        }
    }
    if (!session_id.empty()) {
        if (!root.empty() && root.back() != '/') root += '/';
        root += session_id;
        root += "/images";
    }
    return root;
}

void ensureDir(const std::string& dir) {
    if (dir.empty()) return;
    std::error_code ec;
    std::filesystem::create_directories(std::filesystem::u8path(dir), ec);
}

// ---------------------------------------------------------------------------
// Shutter
//
// THE synthetic gate lives here, not in the button's enabled flag. A caller that reaches this
// function with a software-generated frame gets a refusal and an error status, never a file.
// ---------------------------------------------------------------------------
void shutterFire(AppState& st,
                 const std::string& session_id,
                 const std::string& session_dir,
                 bool ignoreBlur) {
    WebcamState& ws = g_wcam;
    if (ws.addPending) return;

    if (session_id.empty()) {
        ws.status = "No active session — create or select one before capturing.";
        ws.statusLevel = kit::Level::Error;
        return;
    }
    if (!ws.haveFrame || ws.lastRgba.empty() || ws.frameW <= 0 || ws.frameH <= 0) {
        ws.status = "No frame available to capture.";
        ws.statusLevel = kit::Level::Error;
        return;
    }

    // ---- HARD BLOCK: a synthetic frame never becomes a capture ----
    if (ws.frameSynthetic) {
        ws.status = "Refused: the preview is a software test pattern, not a camera frame. "
                    "A synthetic frame can never be written into a session — connect a webcam "
                    "and press Rescan.";
        ws.statusLevel = kit::Level::Error;
        ws.blurArmed = false;
        return;
    }

    const bool blurry = ws.frameSharp >= 0.0 && ws.frameSharp < cam::kSharpnessBlurThreshold;
    if (blurry && !ignoreBlur) {
        char buf[224];
        std::snprintf(buf, sizeof(buf),
                      "This frame is blurry (sharpness %.0f, need %.0f). Steady the camera, or "
                      "press \"Capture anyway\" if you know it is good enough.",
                      ws.frameSharp, cam::kSharpnessBlurThreshold);
        ws.status = buf;
        ws.statusLevel = kit::Level::Warn;
        ws.blurArmed = true;
        return;
    }
    ws.blurArmed = false;

    const int w = ws.frameW;
    const int h = ws.frameH;
    if (ws.lastRgba.size() < (size_t)w * (size_t)h * 4u) {
        ws.status = "Frame buffer is smaller than its declared size — capture aborted.";
        ws.statusLevel = kit::Level::Error;
        return;
    }

    std::string dir = session_dir.empty() ? defaultSessionDir(session_id) : session_dir;
    ensureDir(dir);
    if (!dir.empty() && dir.back() != '/') dir += '/';

    // Never clobber a previous shot: bump until the name is free.
    std::string path;
    for (int guard = 0; guard < 10000; ++guard) {
        char fname[64];
        std::snprintf(fname, sizeof(fname), "webcam_%03d.jpg", ws.shotSeq);
        ++ws.shotSeq;
        path = dir + fname;
        std::error_code ec;
        if (!std::filesystem::exists(std::filesystem::u8path(path), ec)) break;
    }

    std::vector<unsigned char> rgb((size_t)w * h * 3);
    for (size_t i = 0, n = (size_t)w * h; i < n; ++i) {
        rgb[i * 3 + 0] = ws.lastRgba[i * 4 + 0];
        rgb[i * 3 + 1] = ws.lastRgba[i * 4 + 1];
        rgb[i * 3 + 2] = ws.lastRgba[i * 4 + 2];
    }
    if (!stbi_write_jpg(path.c_str(), w, h, 3, rgb.data(), 92)) {
        ws.status = "JPEG write failed: " + path;
        ws.statusLevel = kit::Level::Error;
        return;
    }
    ws.lastShotPath = path;

    // Capture strip: own texture so the shot is visible this frame.
    {
        ShotThumb t;
        t.path = path;
        t.badge = blurry ? "blurred" : "ok";
        t.sharpness = ws.frameSharp;
        t.tex = makeThumbTexture(ws.lastRgba, w, h, (int)mx::reThumbMd());
        ws.strip.push_back(std::move(t));
        while ((int)ws.strip.size() > kStripMax) {
            if (ws.strip.front().tex) glDeleteTextures(1, &ws.strip.front().tex);
            ws.strip.erase(ws.strip.begin());
        }
    }

    // Gallery: webcam shots were previously invisible in the contact sheet. They are not any more.
    {
        GalleryImage g;
        g.path      = path;
        g.quality   = blurry ? "blurred" : "ok";
        g.sharpness = ws.frameSharp;
        g.synthetic = false;   // unreachable otherwise — the hard block above guarantees it
        S().galleryImages.push_back(std::move(g));
    }

    ws.addPending  = true;
    ws.status      = "Adding to session\xe2\x80\xa6";
    ws.statusLevel = kit::Level::Busy;

    nlohmann::json args;
    args["session_id"] = session_id;
    args["paths"]      = nlohmann::json::array({ path });
    st.rpc.call("reveng_add_images", args, [path, blurry](const core::RpcResult& r) {
        WebcamState& w = g_wcam;
        w.addPending = false;

        std::string quality = blurry ? "blurred" : "ok";
        if (!r.ok) {
            w.status = r.err.empty() ? "reveng_add_images failed (transport)." : r.err;
            w.statusLevel = kit::Level::Error;
            quality.clear();
        } else if (!r.result.is_object()) {
            w.status = "reveng_add_images returned a non-object.";
            w.statusLevel = kit::Level::Error;
            quality.clear();
        } else if (!r.result.value("ok", true)) {
            w.status = r.result.value("reason", std::string("reveng_add_images failed."));
            w.statusLevel = kit::Level::Error;
            quality.clear();
        } else {
            const int added = r.result.value("added", 0);
            if (added > 0) {
                char buf[128];
                std::snprintf(buf, sizeof(buf), "Saved and added to the session (%d image%s).",
                              added, added == 1 ? "" : "s");
                w.status = buf;
                w.statusLevel = blurry ? kit::Level::Warn : kit::Level::Ok;
            } else {
                w.status = "The session accepted the call but reported 0 images added — "
                           "check the session's images directory.";
                w.statusLevel = kit::Level::Warn;
                quality.clear();
            }
        }

        // Write the outcome back onto the gallery entry we optimistically pushed.
        for (GalleryImage& g : S().galleryImages) {
            if (g.path == path) { g.quality = quality; break; }
        }
        // Coverage is now stale — let the next frame refetch it.
        w.covLastT = -100.0;
    });
}

// ---------------------------------------------------------------------------
// Coverage dome overlay (ImDrawList; no GL state changes)
// ---------------------------------------------------------------------------

ImVec2 domeProject(float az_deg, float el_deg, ImVec2 centre, float radius) {
    const float kPi = 3.14159265f;
    const float az  = az_deg * kPi / 180.0f;
    const float el  = el_deg * kPi / 180.0f;
    const float r   = radius * (1.0f - el / (kPi * 0.5f));   // el=90 -> centre, el=0 -> rim
    return ImVec2(centre.x + r * std::cos(az - kPi * 0.5f),
                  centre.y + r * std::sin(az - kPi * 0.5f));
}

// `estimated` renders every dot HOLLOW: an estimated ring must never look like a measurement.
void drawCoverageDome(ImDrawList* dl, ImVec2 centre, float radius, bool estimated) {
    const WebcamState& ws = g_wcam;
    const float thin = mx::stroke(1.0f);
    const float thick = mx::stroke(1.5f);

    // Backing disc so the dome stays legible over a bright frame.
    dl->AddCircleFilled(centre, radius + mx::U(1.5f), palCol(ui::palette.windowBg, 0.62f), 0);
    dl->AddCircle(centre, radius, palCol(ui::palette.border), 0, thick);
    dl->AddCircle(centre, radius * 0.5f, palCol(ui::palette.border, 0.6f), 0, thin);

    for (int i = 0; i < kPlanSize; ++i) {
        const ImVec2 p = domeProject(kShotPlan[i].az, kShotPlan[i].el, centre, radius);
        kit::Level lvl = kit::Level::Neutral;
        float dotR = mx::U(1.125f);
        if (ws.covBlurred[i]) {
            lvl = kit::Level::Warn;
            dotR = mx::U(1.25f);
        } else if (ws.covCaptured[i]) {
            lvl = kit::Level::Ok;
            dotR = mx::U(1.5f);
        }
        if (estimated && lvl != kit::Level::Neutral) {
            dl->AddCircle(p, dotR, lvl == kit::Level::Warn ? lvlCol(kit::Level::Warn)
                                                           : lvlCol(kit::Level::Ok),
                          0, thick);
        } else {
            dl->AddCircleFilled(p, dotR, lvlCol(lvl, lvl == kit::Level::Neutral ? 0.45f : 0.92f), 0);
            dl->AddCircle(p, dotR, palCol(ui::palette.text, 0.25f), 0, thin);
        }
    }
}

// "SYNTHETIC PREVIEW" band across the middle of the hero. Impossible to miss, impossible to
// mistake for a photograph.
void drawSyntheticBadge(ImDrawList* dl, ImVec2 p0, ImVec2 p1) {
    const char* line1 = "SYNTHETIC PREVIEW \xe2\x80\x94 NOT A REAL CAMERA";
    const char* line2 = "capture is disabled; this image can never enter a session";
    const ImVec2 t1 = ImGui::CalcTextSize(line1);
    const ImVec2 t2 = ImGui::CalcTextSize(line2);
    const float padY = mx::reCardPadY();
    const float bandH = t1.y + t2.y + padY * 3.0f;
    const float midY  = (p0.y + p1.y) * 0.5f;
    const ImVec2 b0(p0.x, midY - bandH * 0.5f);
    const ImVec2 b1(p1.x, midY + bandH * 0.5f);
    dl->AddRectFilled(b0, b1, lvlCol(kit::Level::Error, 0.86f));
    dl->AddRect(b0, b1, lvlCol(kit::Level::Error), 0.0f, 0, mx::stroke(1.0f));
    const ImU32 ink = palCol(ui::palette.windowBg);
    dl->AddText(ImVec2((b0.x + b1.x - t1.x) * 0.5f, b0.y + padY), ink, line1);
    dl->AddText(ImVec2((b0.x + b1.x - t2.x) * 0.5f, b0.y + padY * 2.0f + t1.y), ink, line2);
}

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------

struct CtlDef {
    cam::CameraControl id;
    const char*        label;
    const char*        help;
};

const CtlDef kCtlDefs[kCtlCount] = {
    { cam::CameraControl::Focus,        "Focus",
      "Autofocus hunting between shots changes the effective focal length, so every photo has "
      "slightly different intrinsics and the bundle adjustment fights itself. Set focus once on "
      "the part, then lock." },
    { cam::CameraControl::Exposure,     "Exposure",
      "Auto-exposure re-meters as the part rotates, which changes apparent brightness of the same "
      "surface between views and confuses feature matching." },
    { cam::CameraControl::WhiteBalance, "White balance",
      "Auto white balance shifts colour between views; texture-baking then produces a part that "
      "changes colour as you orbit it." },
    { cam::CameraControl::Gain,         "Gain",
      "Higher gain buys shutter speed at the cost of sensor noise, which reads as false texture "
      "detail and produces spurious features." },
    { cam::CameraControl::Zoom,         "Zoom",
      "Digital zoom crops the sensor and changes intrinsics. Prefer moving the camera." },
    { cam::CameraControl::Brightness,   "Brightness",
      "Post-gain brightness trim. Safe to adjust, but keep it fixed for the whole set." },
};

void refreshControls() {
    WebcamState& ws = g_wcam;
    if (!ws.backend || !ws.opened) return;
    const double now = ImGui::GetTime();
    if (now - ws.ctlRefreshT < 0.5) return;
    ws.ctlRefreshT = now;
    for (int i = 0; i < kCtlCount; ++i) {
        ws.ctl[i] = ws.backend->getControl(kCtlDefs[i].id);
        ws.ctlValue[i] = (float)ws.ctl[i].value;
    }
}

// Width hint for CardHeaderRight — chips are text plus a dot plus card padding.
float chipWidth(const char* label) {
    return ImGui::CalcTextSize(label).x + mx::reCardPadX() * 2.5f;
}

// Path of the workspace-wide contact-sheet selection, or "" when nothing is selected.
// Shared with the gallery and the properties column via RevEngState::selectedImage.
std::string selectedGalleryPath() {
    const RevEngState& s = S();
    if (s.selectedImage < 0 || s.selectedImage >= (int)s.galleryImages.size()) return {};
    return s.galleryImages[(size_t)s.selectedImage].path;
}

void drawDeviceCard(AppState& st) {
    WebcamState& ws = g_wcam;
    (void)st;

    // No camera/photo glyph exists in contracts/icons.md, so these cards are text-only: the kit
    // renders nullptr as "no icon" rather than inventing a name the atlas does not have.
    if (!kit::CardBegin("re_wcam_device", "Camera device")) { kit::CardEnd(); return; }

    const bool live = ws.opened && ws.haveFrame && !ws.frameSynthetic;
    const char* headLabel = live ? "live" : (ws.opened ? "opening" : "no camera");
    kit::CardHeaderRight(chipWidth(headLabel));
    kit::StatusChip(headLabel,
                    live ? kit::Level::Ok : (ws.opened ? kit::Level::Busy : kit::Level::Error),
                    live ? "A real sensor frame arrived this second."
                         : (ws.opened ? "Device open; waiting for the first buffer."
                                      : "No device is open — every capture control is blocked."));

    if (ws.devices.empty()) {
        if (kit::BlockedState("No camera detected",
                              ws.openErr.empty() ? "The platform backend reported no video devices."
                                                 : ws.openErr.c_str(),
                              "Plug in a USB webcam and press Rescan. On Linux the app also needs "
                              "read access to /dev/video* (the 'video' group). You can still add "
                              "photos with the file picker, phone capture, or drag-and-drop.",
                              "Rescan devices")) {
            dropDevice();
            ws.enumDone = false;
        }
        kit::CardEnd();
        return;
    }

    // ---- device combo ----
    kit::FieldLabel("Device", "Pick the camera to capture from. The tooltip shows the hardware "
                              "bus path, which is how you tell two identical webcams apart.");
    ImGui::SetNextItemWidth(mx::reFieldW() * 2.0f);
    const std::string preview =
        (ws.deviceIdx >= 0 && ws.deviceIdx < (int)ws.devices.size())
            ? deviceLabel(ws.devices[(size_t)ws.deviceIdx])
            : std::string("(select camera)");
    if (ImGui::BeginCombo("##re_wcam_dev", preview.c_str())) {
        for (int i = 0; i < (int)ws.devices.size(); ++i) {
            const bool sel = (i == ws.deviceIdx);
            const std::string lab = deviceLabel(ws.devices[(size_t)i]);
            if (ImGui::Selectable(lab.c_str(), sel) && i != ws.deviceIdx) {
                dropDevice();
                ws.deviceIdx = i;
                ws.modesDone = false;
                ws.openErr.clear();
                ws.grabNote.clear();
            }
            if (ImGui::IsItemHovered()) {
                const std::string& bus = ws.devices[(size_t)i].bus;
                ImGui::SetTooltip("%s\n%s", ws.devices[(size_t)i].device_id.c_str(),
                                  bus.empty() ? "(bus path unavailable)" : bus.c_str());
            }
            if (sel) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    ImGui::SameLine(0.0f, mx::reCtlGap());
    if (kit::SecondaryButton("Rescan")) {
        dropDevice();
        ws.enumDone = false;
    }

    kit::GapControl();

    // ---- mode combo ----
    kit::FieldLabel("Resolution",
                    "Photogrammetry wants pixels, not framerate: more sensor resolution means more "
                    "matchable features and finer dimensional detail. 'Auto' asks the backend for "
                    "its highest-resolution mode.");
    ImGui::SetNextItemWidth(mx::reFieldW() * 2.0f);
    const std::string modePreview =
        (ws.modeIdx >= 0 && ws.modeIdx < (int)ws.modeLabels.size())
            ? ws.modeLabels[(size_t)ws.modeIdx]
            : std::string("Auto (highest resolution)");
    if (ImGui::BeginCombo("##re_wcam_mode", modePreview.c_str())) {
        if (ImGui::Selectable("Auto (highest resolution)", ws.modeIdx < 0) && ws.modeIdx >= 0) {
            ws.modeIdx = -1;
            dropDevice();
        }
        for (int i = 0; i < (int)ws.modeLabels.size(); ++i) {
            const bool sel = (i == ws.modeIdx);
            if (ImGui::Selectable(ws.modeLabels[(size_t)i].c_str(), sel) && i != ws.modeIdx) {
                ws.modeIdx = i;
                dropDevice();
            }
            if (ImGui::IsItemHovered() && i < (int)ws.modes.size()) {
                ImGui::SetTooltip("%s\n%s",
                                  ws.modes[(size_t)i].compressed
                                      ? "Compressed stream (MJPEG/H264) — higher resolutions "
                                        "available, needs a decode step."
                                      : "Uncompressed stream — lower resolutions, no decode cost.",
                                  ws.modes[(size_t)i].format_label.empty()
                                      ? "" : ws.modes[(size_t)i].format_label.c_str());
            }
            if (sel) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    if (ws.modes.empty()) {
        ImGui::SameLine(0.0f, mx::reCtlGap());
        kit::StatusChip("modes unavailable", kit::Level::Warn,
                        "listModes() returned nothing for this device — the backend will fall back "
                        "to whatever the driver offers on open().");
    }

    kit::GapControl();

    // ---- negotiated mode ----
    if (ws.opened && ws.active.width > 0) {
        char buf[96];
        std::snprintf(buf, sizeof(buf), "%dx%d %s @ %.0f",
                      ws.active.width, ws.active.height,
                      ws.active.format_label.empty() ? "?" : ws.active.format_label.c_str(),
                      ws.active.fps);
        kit::ValueChip("negotiated", buf, kit::Level::Ok,
                       "What the driver actually settled on after open(). If this is lower than "
                       "you asked for, the mode was not available.");
        if (ws.modeIdx >= 0 && ws.modeIdx < (int)ws.modes.size()) {
            const cam::CameraMode& want = ws.modes[(size_t)ws.modeIdx];
            if (want.width != ws.active.width || want.height != ws.active.height) {
                ImGui::SameLine(0.0f, mx::reCtlGap());
                kit::StatusChip("mode downgraded", kit::Level::Warn,
                                "The driver refused the requested mode and picked another.");
            }
        }
    } else {
        kit::ValueChip("negotiated", "\xe2\x80\x94", kit::Level::Neutral,
                       "No device is open yet.");
    }

    kit::InlineStatus(ws.openErr.c_str(), kit::Level::Error);
    kit::InlineStatus(ws.grabNote.c_str(), ws.grabLevel);

    // listModes() reports every format the device advertises, including ones the backend cannot
    // decode — so a mode the user picked from the combo is not automatically openable. Give them
    // the way back instead of leaving them stuck on a dead selection.
    if (!ws.opened && ws.modeIdx >= 0 && !ws.openErr.empty()) {
        if (kit::SecondaryButton("Fall back to Auto (highest resolution)")) {
            ws.modeIdx = -1;
            dropDevice();
            ws.openErr.clear();
        }
    }

    // Capability mirror: the worker's own view of webcam support. Advisory only — the shutter is
    // gated on the LOCAL device, because the JPEG is written client-side.
    const RevEngState& s = S();
    if (s.caps.loaded && !s.caps.webcam) {
        kit::CapabilityRow("worker webcam support", false,
                           "reveng_status() reports no webcam capability on the worker. Local "
                           "capture still works — the photo is written by the app and handed to "
                           "the session by path.");
    }

    kit::CardEnd();
}

void drawControlsCard() {
    WebcamState& ws = g_wcam;

    if (!kit::CardBegin("re_wcam_controls", "Camera controls", "settings")) { kit::CardEnd(); return; }

    const char* lockLabel = ws.locked ? "LOCKED for capture" : "UNLOCKED";
    kit::CardHeaderRight(chipWidth(lockLabel));
    kit::StatusChip(lockLabel, ws.locked ? kit::Level::Ok : kit::Level::Warn,
                    ws.locked
                        ? "Focus, exposure, white balance and gain are frozen. Intrinsics will "
                          "stay constant for the rest of this capture set."
                        : "Autofocus and auto-exposure are still free to drift between shots, "
                          "which changes the effective intrinsics mid-set and degrades the "
                          "reconstruction. Lock before you start shooting.");

    const bool canDrive = ws.opened && ws.backend != nullptr;
    const char* noDev = "No camera is open.";

    // ---- the single most important control on the panel ----
    if (kit::PrimaryButton("Lock focus + exposure for capture", canDrive, canDrive ? nullptr : noDev,
                           ImVec2(0, mx::reBtnH()))) {
        const bool ok = ws.backend->lockForCapture();
        ws.locked = ok;
        if (ok) {
            ws.lockMsg   = "Focus, exposure, white balance and gain frozen at their current values.";
            ws.lockLevel = kit::Level::Ok;
        } else {
            const std::string e = ws.backend->lastError();
            ws.lockMsg = e.empty()
                ? "The camera refused to lock at least one supported control — check the "
                  "per-control 'auto' boxes below."
                : e;
            ws.lockLevel = kit::Level::Error;
        }
        ws.ctlRefreshT = -100.0;
    }
    ImGui::SameLine(0.0f, mx::reCtlGap());
    kit::HelpMarker(
        "Photogrammetry pre-flight. A webcam left on autofocus re-focuses every time the part "
        "moves, and auto-exposure re-meters every time the lighting balance shifts. Both change "
        "the effective camera intrinsics between photos, and structure-from-motion assumes they "
        "are constant. The result is a reconstruction that will not converge, or one that "
        "converges to the wrong scale. Frame the part, let the camera settle, then lock.");
    kit::InlineStatus(ws.lockMsg.c_str(), ws.lockLevel);

    kit::GapSection();
    kit::SectionHeader("Manual overrides");

    refreshControls();

    for (int i = 0; i < kCtlCount; ++i) {
        const CtlDef& def = kCtlDefs[i];
        const cam::ControlState& cs = ws.ctl[i];
        const bool usable = canDrive && cs.supported;

        ImGui::PushID(i);
        kit::FieldLabel(def.label, def.help);

        const bool disabled = !usable || cs.automatic;
        if (disabled) ImGui::BeginDisabled();
        ImGui::SetNextItemWidth(mx::reFieldW());
        if (ImGui::SliderFloat("##val", &ws.ctlValue[i], 0.0f, 1.0f, "%.2f") && usable) {
            if (!ws.backend->setControl(def.id, (double)ws.ctlValue[i])) {
                ws.lockMsg   = ws.backend->lastError();
                ws.lockLevel = kit::Level::Error;
            }
            ws.locked = false;   // a manual nudge invalidates the "everything is frozen" claim
            ws.ctlRefreshT = -100.0;
        }
        if (disabled) ImGui::EndDisabled();
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
            if (!canDrive)          ImGui::SetTooltip("%s", noDev);
            else if (!cs.supported) ImGui::SetTooltip("Not supported by this camera.");
            else if (cs.automatic)  ImGui::SetTooltip("Currently on automatic — clear 'auto' to "
                                                      "set this manually.");
        }

        ImGui::SameLine(0.0f, mx::reCtlGap());
        const bool autoBoxUsable = usable && cs.auto_capable;
        bool autoOn = cs.automatic;
        if (!autoBoxUsable) ImGui::BeginDisabled();
        if (ImGui::Checkbox("auto", &autoOn) && autoBoxUsable) {
            if (!ws.backend->setControlAuto(def.id, autoOn)) {
                ws.lockMsg   = ws.backend->lastError();
                ws.lockLevel = kit::Level::Error;
            }
            if (autoOn) ws.locked = false;
            ws.ctlRefreshT = -100.0;
        }
        if (!autoBoxUsable) ImGui::EndDisabled();
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled) && !autoBoxUsable) {
            ImGui::SetTooltip(!cs.supported ? "Not supported by this camera."
                                            : "This camera exposes no automatic mode for it.");
        }

        if (!cs.supported) {
            ImGui::SameLine(0.0f, mx::reCtlGap());
            kit::StatusChip("unsupported", kit::Level::Neutral,
                            "The device does not expose this control. It is shown greyed rather "
                            "than hidden so you know it exists and simply is not available here.");
        } else if (cs.automatic) {
            ImGui::SameLine(0.0f, mx::reCtlGap());
            kit::StatusChip("auto", kit::Level::Warn,
                            "Value drifts on its own — not safe for a capture set.");
        }
        ImGui::PopID();
    }

    kit::CardEnd();
}

void drawPreviewCard(AppState& st, const std::string& session_id, const std::string& session_dir) {
    WebcamState& ws = g_wcam;

    if (!kit::CardBegin("re_wcam_preview", "Live preview & capture")) {
        kit::CardEnd();
        return;
    }

    const bool blurry = ws.frameSharp >= 0.0 && ws.frameSharp < cam::kSharpnessBlurThreshold;
    // The blur refusal is about THIS frame. Once the preview steadies, retract the offer rather
    // than leaving a "capture anyway" escape hatch armed over a perfectly sharp image.
    if (ws.blurArmed && !blurry && !ws.frameSynthetic && ws.frameSharp >= 0.0) {
        ws.blurArmed = false;
        if (ws.statusLevel == kit::Level::Warn) {
            ws.status.clear();
            ws.statusLevel = kit::Level::Neutral;
        }
    }
    char sharpBuf[48];
    if (ws.frameSharp >= 0.0) std::snprintf(sharpBuf, sizeof(sharpBuf), "%.0f", ws.frameSharp);
    else                      std::snprintf(sharpBuf, sizeof(sharpBuf), "%s", "\xe2\x80\x94");

    kit::CardHeaderRight(chipWidth("sharpness 0000"));
    kit::ValueChip("sharpness", sharpBuf,
                   ws.frameSynthetic ? kit::Level::Neutral
                                     : (ws.frameSharp < 0.0 ? kit::Level::Neutral
                                                            : (blurry ? kit::Level::Warn
                                                                      : kit::Level::Ok)),
                   "Variance of the Laplacian, the same score the frame culler in "
                   "services/vision uses. Below 100 counts as blurry. Watch it settle before you "
                   "press the shutter — this is the number that decides whether a photo is worth "
                   "keeping.");

    // ---- hero ----
    const float availW = ImGui::GetContentRegionAvail().x;
    float prevW = availW;
    if (prevW > mx::rePreviewMaxW()) prevW = mx::rePreviewMaxW();
    if (prevW < mx::rePreviewMinW()) prevW = mx::rePreviewMinW();
    if (prevW > availW) prevW = availW;
    const float prevH = prevW * 9.0f / 16.0f;

    std::string caption;
    if (ws.frameSynthetic) {
        caption = "software test pattern";
    } else {
        char cbuf[128];
        std::snprintf(cbuf, sizeof(cbuf), "%dx%d %s \xc2\xb7 sharpness %s",
                      ws.frameW, ws.frameH,
                      ws.active.format_label.empty() ? "" : ws.active.format_label.c_str(),
                      sharpBuf);
        caption = cbuf;
    }

    const ImVec2 heroP0 = ImGui::GetCursorScreenPos();
    kit::HeroPreview("re_wcam_hero", ws.texId, ws.frameW, ws.frameH, ImVec2(prevW, prevH),
                     caption.c_str(), "No camera frame yet");

    // Overlays must land on the letterboxed IMAGE, not on the box or the card padding, so mirror
    // HeroPreview's own fit: box -> frame inset -> aspect-preserving centre fit.
    const float frameInset = mx::U(1.0f);
    const float boxW = prevW - frameInset * 2.0f;
    const float boxH = prevH - frameInset * 2.0f;
    const float aspect = (ws.frameW > 0 && ws.frameH > 0)
                             ? (float)ws.frameW / (float)ws.frameH
                             : 1.0f;
    float drawW = boxW;
    float drawH = boxW / aspect;
    if (drawH > boxH) { drawH = boxH; drawW = boxH * aspect; }
    const ImVec2 imgP0(heroP0.x + frameInset + (boxW - drawW) * 0.5f,
                       heroP0.y + frameInset + (boxH - drawH) * 0.5f);
    const ImVec2 imgP1(imgP0.x + drawW, imgP0.y + drawH);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float domeR = mx::reDomeR();
    const float inset = domeR + mx::reCardPadX();
    if (drawW > inset * 2.0f && drawH > inset * 2.0f) {
        const ImVec2 domeC(imgP1.x - inset, imgP0.y + inset);
        drawCoverageDome(dl, domeC, domeR, ws.covMethod == "count_proxy" || !ws.covLoaded);
    }
    // Anchored to the image's TOP so it never collides with HeroPreview's bottom caption scrim.
    if (ws.frameSynthetic) drawSyntheticBadge(dl, imgP0, imgP1);

    kit::GapControl();

    // ---- coverage readout ----
    {
        char covBuf[32];
        std::snprintf(covBuf, sizeof(covBuf), "%d / %d", ws.covCapturedN, kPlanSize);
        const kit::Level covLvl = ws.covCapturedN >= kPlanSize ? kit::Level::Ok
                                : (ws.covCapturedN >= 5 ? kit::Level::Warn : kit::Level::Error);
        kit::ValueChip("coverage", covBuf, ws.covLoaded ? covLvl : kit::Level::Neutral,
                       "Orbit positions the session already has photos for, from "
                       "get_reveng_coverage(). This is the worker's opinion, not a local counter.");

        ImGui::SameLine(0.0f, mx::reCtlGap());
        if (!ws.covLoaded) {
            kit::StatusChip(ws.covPending ? "reading coverage" : "coverage unknown",
                            ws.covPending ? kit::Level::Busy : kit::Level::Neutral,
                            "No coverage report yet for this session.");
        } else if (ws.covMethod == "pose_binned") {
            kit::StatusChip("measured from camera poses", kit::Level::Ok,
                            "Each photo was matched to an orbit position using its recovered "
                            "camera pose. The ring reflects where you actually stood.");
        } else if (ws.covMethod == "count_proxy") {
            kit::StatusChip("ESTIMATED from photo count", kit::Level::Warn,
                            "No poses are available yet, so the backend simply assumed one photo "
                            "covers one position. The ring is a guess: filled dots do NOT mean "
                            "those angles were actually shot. Run Reconstruct to get a measured "
                            "ring.");
            ImGui::SameLine(0.0f, mx::reCtlGap());
            kit::HonestyTag("approximate", "coverage estimated from photo count, not poses");
        } else {
            kit::StatusChip("coverage method unreported", kit::Level::Warn,
                            "The backend did not say how it derived coverage; treat the ring as "
                            "an estimate.");
        }

        if (ws.covBlurredN > 0) {
            ImGui::SameLine(0.0f, mx::reCtlGap());
            char bb[32];
            std::snprintf(bb, sizeof(bb), "%d", ws.covBlurredN);
            kit::ValueChip("blurry shots", bb, kit::Level::Warn,
                           "Positions whose photo failed the blur check server-side. Re-shoot "
                           "them before reconstructing.");
        }
        if (!ws.covQuality.empty()) {
            ImGui::SameLine(0.0f, mx::reCtlGap());
            const kit::Level ql = ws.covQuality == "good" ? kit::Level::Ok
                                : (ws.covQuality == "partial" ? kit::Level::Warn
                                                              : kit::Level::Error);
            kit::StatusChip(ws.covQuality.c_str(), ql,
                            "The backend's overall verdict on this capture set.");
        }
        kit::InlineStatus(ws.covErr.c_str(), kit::Level::Error);
    }

    kit::GapControl();
    // Dome legend — chips rather than draw-list text, because the atlas has no dot glyph.
    kit::StatusChip("captured", kit::Level::Ok);
    ImGui::SameLine(0.0f, mx::reCtlGap());
    kit::StatusChip("blurry", kit::Level::Warn);
    ImGui::SameLine(0.0f, mx::reCtlGap());
    kit::StatusChip("pending", kit::Level::Neutral);
    ImGui::SameLine(0.0f, mx::reCtlGap());
    kit::HelpMarker(
        "The dome is a top-down map of an 11-position orbit: the rim is a low camera angle, the "
        "centre is straight down at the part. Hollow dots mean the ring is an estimate rather "
        "than a measurement.");

    kit::GapSection();

    // ---- shutter ----
    std::string reason;
    if (session_id.empty()) {
        reason = "No active session — create or select one in the session rail first.";
    } else if (ws.devices.empty()) {
        reason = "No camera is connected. Plug in a USB webcam and press Rescan in the device "
                 "card above.";
    } else if (!ws.opened) {
        reason = ws.openErr.empty() ? "The camera is not open yet." : ws.openErr;
    } else if (ws.frameSynthetic) {
        reason = "The preview is a software test pattern, not a camera frame. A synthetic frame "
                 "can never be written into a session.";
    } else if (ws.addPending) {
        reason = "The previous shot is still being added to the session.";
    }
    const bool canShoot = reason.empty();

    if (kit::PrimaryButton("Capture photo", canShoot, canShoot ? nullptr : reason.c_str(),
                           ImVec2(prevW, mx::reBtnH()))) {
        shutterFire(st, session_id, session_dir, /*ignoreBlur=*/false);
    }
    if (ws.blurArmed) {
        kit::GapControl();
        if (kit::SecondaryButton("Capture anyway (frame is blurry)", canShoot,
                                 canShoot ? nullptr : reason.c_str())) {
            shutterFire(st, session_id, session_dir, /*ignoreBlur=*/true);
        }
        ImGui::SameLine(0.0f, mx::reCtlGap());
        if (kit::SecondaryButton("Dismiss")) ws.blurArmed = false;
    }
    if (ws.addPending) {
        kit::ProgressBar(-1.0f, "adding to session", kit::Level::Busy);
    }
    kit::InlineStatus(ws.status.c_str(), ws.statusLevel);

    kit::GapSection();
    kit::SectionHeader("This session's last shots");

    if (ws.strip.empty()) {
        kit::EmptyState(nullptr, "No shots captured here yet",
                        "Frame the part, lock focus and exposure, then press Capture. Each shot "
                        "appears here immediately and in the gallery below.");
    } else {
        const float side = mx::reThumbMd();
        const float gap  = mx::reThumbGap();
        const float rowW = ImGui::GetContentRegionAvail().x;
        float used = 0.0f;
        for (int i = (int)ws.strip.size() - 1; i >= 0; --i) {
            const ShotThumb& t = ws.strip[(size_t)i];
            if (used > 0.0f) {
                if (used + gap + side > rowW) used = 0.0f;         // wrap to a fresh row
                else                          ImGui::SameLine(0.0f, gap);
            }
            ImGui::PushID(i);
            char label[24];
            std::snprintf(label, sizeof(label), "#%d", i + 1);
            const bool selected = selectedGalleryPath() == t.path;
            if (kit::ThumbTile("shot", t.tex, side, selected, t.badge.c_str(), label)) {
                // Select the matching gallery entry so the properties column's hero preview and
                // the Scale stage's picker follow what the user just clicked.
                const std::vector<GalleryImage>& gal = S().galleryImages;
                for (int gi = 0; gi < (int)gal.size(); ++gi) {
                    if (gal[(size_t)gi].path == t.path) { S().selectedImage = gi; break; }
                }
            }
            if (ImGui::IsItemHovered()) {
                std::vector<std::string> meta;
                meta.push_back(t.path);
                if (t.sharpness >= 0.0) {
                    char sb[48];
                    std::snprintf(sb, sizeof(sb), "sharpness %.0f (threshold %.0f)",
                                  t.sharpness, cam::kSharpnessBlurThreshold);
                    meta.emplace_back(sb);
                }
                meta.push_back(t.badge == "blurred" ? "flagged blurry at capture time"
                                                    : "passed the blur check");
                kit::ThumbHoverPreview(t.tex, (int)side, (int)side, meta);
            }
            ImGui::PopID();
            used += (used > 0.0f ? gap : 0.0f) + side;
        }
    }

    kit::CardEnd();
}

}  // namespace

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void drawWebcamCapture(AppState& st,
                       const std::string& session_id,
                       const std::string& session_dir) {
    updatePreviewFrame();
    refreshCoverage(st, session_id, /*force=*/false);

    drawDeviceCard(st);
    kit::GapCards();
    drawControlsCard();
    kit::GapCards();
    drawPreviewCard(st, session_id, session_dir);
}

void webcamCaptureResetSession() {
    WebcamState& ws = g_wcam;
    clearStrip();
    for (int i = 0; i < kPlanSize; ++i) { ws.covCaptured[i] = false; ws.covBlurred[i] = false; }
    ws.covCapturedN = 0;
    ws.covBlurredN  = 0;
    ws.covMethod.clear();
    ws.covQuality.clear();
    ws.covLoaded  = false;
    ws.covLastT   = -100.0;
    ws.covSession.clear();
    ws.covErr.clear();
    ws.shotSeq = 0;
    ws.lastShotPath.clear();
    ws.status.clear();
    ws.statusLevel = kit::Level::Neutral;
    ws.blurArmed = false;
    // Device, negotiated mode and the capture lock deliberately survive a session switch.
}

void webcamCaptureShutdown() {
    WebcamState& ws = g_wcam;
    clearStrip();
    if (ws.backend) {
        ws.backend->close();
        ws.backend.reset();
    }
    if (ws.texId) {
        glDeleteTextures(1, &ws.texId);
        ws.texId = 0;
    }
    ws = WebcamState{};
}

}  // namespace reveng
}  // namespace ui
