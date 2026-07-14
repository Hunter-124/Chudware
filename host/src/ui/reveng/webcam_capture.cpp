// webcam_capture.cpp — live webcam preview + coverage overlay + shutter (RE-19)
// Draws a panel with:
//   1. Live preview via CameraBackend (V4L2 on Linux, MF stub on Windows, synthetic
//      stub as fallback when no device / open fails — keeps AI_CAD_SHOT=reveng_capture
//      working headless).
//   2. OpenScan-style 11-shot dome coverage ring (ImDrawList) — green=captured, amber=blurred.
//   3. Shutter button: encodes the latest frame as JPEG (stb_image_write), fires async
//      reveng_add_images.
//
// GL-thread only.  NEVER call st.rpc.call synchronously in a draw path (known freeze bug).
#include "webcam_capture.h"

#include <cstdio>
#include <cstring>
#include <cmath>
#include <string>
#include <vector>
#include <algorithm>
#include <memory>
#include <filesystem>
#include <system_error>
#include <cstdlib>

#include "imgui.h"
#include <glad/gl.h>
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"
#include "platform/camera.h"

// stb_image_write — implementation compiled in host/src/stb_impl.cpp; include header only.
#include "stb_image_write.h"

namespace ui {
namespace reveng {

// ---------------------------------------------------------------------------
// Internal state (file-static, GL-thread only)
// ---------------------------------------------------------------------------

namespace {

// Coverage ring: 11 shot positions (OpenScan semantics — 2 rings + top).
// azimuth (deg), elevation (deg)
struct ShotPos { float az; float el; };
static const ShotPos kShotPlan[11] = {
    {   0, 30}, { 36, 30}, { 72, 30}, {108, 30}, {144, 30},
    { 18, 60}, { 54, 60}, { 90, 60}, {126, 60}, {162, 60},
    {  0, 90},
};

struct WebcamState {
    // Preview texture
    GLuint texId  = 0;
    int    texW   = 320;
    int    texH   = 240;
    int    allocW = 0;   // currently allocated GL texture size
    int    allocH = 0;

    // Latest RGBA frame (source of truth for preview + shutter)
    std::vector<unsigned char> lastRgba;
    bool   haveFrame = false;

    // Camera backend
    std::unique_ptr<aicad::platform::CameraBackend> cam;
    std::vector<aicad::platform::CameraInfo> devices;
    int    deviceIdx = -1;
    bool   camOpened = false;
    bool   usingStub = true;       // true when falling back to synthetic frames
    bool   enumDone  = false;
    std::string camStatus;         // human-readable device/open status
    std::string camErr;            // sticky open/enum failure

    // Coverage tracking (which of the 11 shot positions have been captured)
    bool captured[11] = {};
    bool blurred[11]  = {};   // quality too low (stub: never)
    int  captureCount = 0;

    // Async shutter state
    bool   addPending   = false;
    std::string lastShotPath;
    std::string lastErr;
    std::string sessionId;

    // Stub frame: hue cycles to show liveness when no real camera
    float hue = 0.0f;

    // Throttle grab attempts a bit so a failing device doesn't spam open()
    double lastGrabT = -100.0;
    double lastOpenAttemptT = -100.0;
};

static WebcamState g_wcam;

// ---------------------------------------------------------------------------
// Stub frame generator — used when no CameraBackend device is available.
// ---------------------------------------------------------------------------

static void buildStubFrameRGBA(std::vector<unsigned char>& px, int w, int h, float hue) {
    px.resize((size_t)w * h * 4);
    // HSV → RGB (simple, no dependency)
    const float s = 0.55f, v = 0.35f;
    const float hh = hue * 6.0f;
    const int   i  = (int)hh;
    const float f  = hh - (float)i;
    const float p  = v * (1.0f - s);
    const float q  = v * (1.0f - s * f);
    const float t  = v * (1.0f - s * (1.0f - f));
    float r, g, b;
    switch (i % 6) {
        case 0: r=v; g=t; b=p; break;
        case 1: r=q; g=v; b=p; break;
        case 2: r=p; g=v; b=t; break;
        case 3: r=p; g=q; b=v; break;
        case 4: r=t; g=p; b=v; break;
        default:r=v; g=p; b=q; break;
    }
    const auto ur = (unsigned char)(r * 255.0f);
    const auto ug = (unsigned char)(g * 255.0f);
    const auto ub = (unsigned char)(b * 255.0f);
    for (int row = 0; row < h; ++row) {
        for (int col = 0; col < w; ++col) {
            const size_t idx = ((size_t)row * w + col) * 4;
            // Cross-hair overlay so we can verify the frame visually
            const bool xh = (std::abs(col - w/2) < 2) || (std::abs(row - h/2) < 2);
            px[idx+0] = xh ? 255 : ur;
            px[idx+1] = xh ? 255 : ug;
            px[idx+2] = xh ? 255 : ub;
            px[idx+3] = 255;
        }
    }
}

// Upload/update the preview texture from `px` (RGBA8, w×h).
static void uploadPreviewTex(const unsigned char* px, int w, int h) {
    if (!px || w <= 0 || h <= 0) return;
    WebcamState& ws = g_wcam;
    if (!ws.texId) {
        GLuint id = 0;
        glGenTextures(1, &id);
        ws.texId = id;
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
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0,
                     GL_RGBA, GL_UNSIGNED_BYTE, px);
        ws.allocW = w;
        ws.allocH = h;
    } else {
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h,
                        GL_RGBA, GL_UNSIGNED_BYTE, px);
    }
    ws.texW = w;
    ws.texH = h;
    glBindTexture(GL_TEXTURE_2D, 0);
}

// Ensure the CameraBackend exists and (when possible) a device is open.
// Falls back to the synthetic stub when enumeration/open fails — never silent.
static void ensureCamera() {
    WebcamState& ws = g_wcam;
    if (!ws.cam) {
        ws.cam = aicad::platform::CameraBackend::create();
        ws.enumDone = false;
        ws.camOpened = false;
        ws.usingStub = true;
    }
    if (!ws.enumDone) {
        ws.enumDone = true;
        ws.devices = ws.cam->listDevices();
        if (ws.devices.empty()) {
            ws.camErr = "No camera devices found — showing test pattern.";
            ws.camStatus = "stub (no devices)";
            ws.usingStub = true;
            // Try opening a well-known stub id so grabFrame works on pure stub backends.
            if (ws.cam->open("stub:0")) {
                ws.camOpened = true;
                ws.deviceIdx = -1;
            }
            return;
        }
        // Prefer first real device.
        ws.deviceIdx = 0;
        ws.camErr.clear();
    }
    if (!ws.camOpened && ws.deviceIdx >= 0 &&
        ws.deviceIdx < (int)ws.devices.size()) {
        const double now = ImGui::GetTime();
        // Throttle re-open attempts (~1 Hz) after a failure.
        if (now - ws.lastOpenAttemptT < 1.0) return;
        ws.lastOpenAttemptT = now;
        const auto& dev = ws.devices[(size_t)ws.deviceIdx];
        if (ws.cam->open(dev.device_id)) {
            ws.camOpened = true;
            ws.usingStub = false;
            ws.camErr.clear();
            ws.camStatus = dev.label.empty() ? dev.device_id
                                             : (dev.label + " (" + dev.device_id + ")");
        } else {
            ws.camOpened = false;
            ws.usingStub = true;
            ws.camErr = "Failed to open " +
                        (dev.label.empty() ? dev.device_id : dev.label) +
                        " (" + dev.device_id + ") — showing test pattern.";
            ws.camStatus = "stub (open failed)";
            // Fall back to stub open so grabFrame still yields a frame when the
            // backend is the pure stub; V4L2 open failure leaves fd closed.
            if (ws.cam->open("stub:0")) {
                ws.camOpened = true;
            }
        }
    }
}

// Pull a fresh frame into lastRgba (real camera or stub) and upload the texture.
static void updatePreviewFrame() {
    WebcamState& ws = g_wcam;
    ensureCamera();

    const double now = ImGui::GetTime();
    // Cap grab rate ~30 Hz so a slow V4L2 read doesn't stall the UI.
    if (now - ws.lastGrabT < (1.0 / 30.0) && ws.haveFrame) {
        return;
    }
    ws.lastGrabT = now;

    aicad::platform::CameraFrame frame;
    bool got = false;
    if (ws.camOpened && ws.cam) {
        got = ws.cam->grabFrame(frame);
    }

    if (got && frame.width > 0 && frame.height > 0 &&
        (int)frame.rgba.size() >= frame.width * frame.height * 4) {
        const int fw = frame.width;
        const int fh = frame.height;
        ws.lastRgba = std::move(frame.rgba);
        // If grab succeeded after a real open, clear the open-error banner.
        if (!ws.usingStub) ws.camErr.clear();
        uploadPreviewTex(ws.lastRgba.data(), fw, fh);
        ws.haveFrame = true;
        return;
    }

    // Fallback: synthetic animated test pattern (also used when grabFrame fails
    // mid-stream, e.g. device unplugged).
    ws.usingStub = true;
    if (ws.camOpened && !ws.devices.empty() && ws.camErr.empty()) {
        ws.camErr = "Camera grab failed — showing test pattern.";
    }
    ws.hue += 0.0014f;
    if (ws.hue > 1.0f) ws.hue -= 1.0f;
    // Keep a stable stub resolution.
    const int sw = ws.texW > 0 ? ws.texW : 320;
    const int sh = ws.texH > 0 ? ws.texH : 240;
    buildStubFrameRGBA(ws.lastRgba, sw, sh, ws.hue);
    uploadPreviewTex(ws.lastRgba.data(), sw, sh);
    ws.haveFrame = true;
}

// ---------------------------------------------------------------------------
// Coverage ring drawing  (ImDrawList; no GL state changes)
// ---------------------------------------------------------------------------

// Project a dome position to a 2D point inside a unit circle (polar projection from top).
static ImVec2 domeProject(float az_deg, float el_deg, ImVec2 centre, float radius) {
    const float az = az_deg * 3.14159265f / 180.0f;
    const float el = el_deg * 3.14159265f / 180.0f;
    // el=90 → centre; el=0 → rim
    const float r = radius * (1.0f - el / (3.14159265f * 0.5f));
    return ImVec2(centre.x + r * std::cos(az - 3.14159265f * 0.5f),
                  centre.y + r * std::sin(az - 3.14159265f * 0.5f));
}

static void drawCoverageRing(ImDrawList* dl, ImVec2 centre, float radius,
                             const bool captured[11], const bool blurred[11]) {
    // Outer circle (dome rim)
    dl->AddCircle(centre, radius, IM_COL32(100, 100, 120, 120), 64, 1.5f);
    // Inner ring tick for 60-deg elevation
    dl->AddCircle(centre, radius * 0.5f, IM_COL32(80, 80, 100, 80), 64, 1.0f);
    // Shot positions
    for (int i = 0; i < 11; ++i) {
        const ImVec2 p = domeProject(kShotPlan[i].az, kShotPlan[i].el, centre, radius);
        ImU32 col;
        float dotR;
        if (captured[i] && !blurred[i]) {
            col  = IM_COL32(60, 200, 80, 230);   // green: captured OK
            dotR = 6.0f;
        } else if (blurred[i]) {
            col  = IM_COL32(230, 160, 30, 220);  // amber: blurred/uncertain
            dotR = 5.0f;
        } else {
            col  = IM_COL32(180, 180, 200, 120); // white-ish: pending
            dotR = 4.5f;
        }
        dl->AddCircleFilled(p, dotR, col);
        // ring outline
        dl->AddCircle(p, dotR, IM_COL32(255, 255, 255, 60), 12, 1.0f);
    }
    // Legend text
    const float lx = centre.x - radius;
    const float ly = centre.y + radius + 6.0f;
    dl->AddText(ImVec2(lx, ly),        IM_COL32(60, 200, 80, 200),  "● captured");
    dl->AddText(ImVec2(lx + 75.0f, ly), IM_COL32(230,160,30, 200), "● blurred");
    dl->AddText(ImVec2(lx + 145.0f,ly), IM_COL32(180,180,200,160), "● pending");
}

// ---------------------------------------------------------------------------
// Session directory helper
// ---------------------------------------------------------------------------

static std::string defaultSessionDir(const std::string& session_id) {
    // Mirror services/mcp/reveng_tools._reveng_dir default so JPEGs land next to
    // the session the worker knows about.
    const char* override = std::getenv("AICAD_REVENG_DIR");
    std::string root;
    if (override && override[0]) {
        root = override;
    } else {
        const char* xdg = std::getenv("XDG_DATA_HOME");
        if (xdg && xdg[0]) {
            root = std::string(xdg) + "/AI-CAD/reveng";
        } else {
            const char* home = std::getenv("HOME");
            root = (home && home[0] ? std::string(home) : std::string(".")) +
                   "/.local/share/AI-CAD/reveng";
        }
    }
    if (!session_id.empty()) {
        if (!root.empty() && root.back() != '/') root += '/';
        root += session_id;
        root += "/images";
    }
    return root;
}

// Best-effort mkdir -p for the images directory.
static void ensureDir(const std::string& dir) {
    if (dir.empty()) return;
    std::error_code ec;
    std::filesystem::create_directories(std::filesystem::u8path(dir), ec);
}

// ---------------------------------------------------------------------------
// JPEG encode + async RPC
// ---------------------------------------------------------------------------

static void shutterFire(AppState& st,
                        const std::string& session_id,
                        const std::string& session_dir) {
    WebcamState& ws = g_wcam;
    if (ws.addPending) return;  // already in-flight

    // Prefer the live frame; rebuild stub if somehow empty.
    if (!ws.haveFrame || ws.lastRgba.empty()) {
        buildStubFrameRGBA(ws.lastRgba, ws.texW > 0 ? ws.texW : 320,
                           ws.texH > 0 ? ws.texH : 240, ws.hue);
        ws.haveFrame = true;
    }
    const int w = ws.texW > 0 ? ws.texW : 320;
    const int h = ws.texH > 0 ? ws.texH : 240;
    if ((int)ws.lastRgba.size() < w * h * 4) {
        ws.lastErr = "No frame available to capture";
        return;
    }

    // Determine next shot index & path
    char fname[64];
    std::snprintf(fname, sizeof(fname), "webcam_%03d.jpg", ws.captureCount);
    std::string dir = session_dir.empty() ? defaultSessionDir(session_id) : session_dir;
    ensureDir(dir);
    std::string path = dir;
    if (!path.empty() && path.back() != '/') path += '/';
    path += fname;

    // Encode JPEG via stb_image_write (component order RGB — drop alpha)
    std::vector<unsigned char> rgb((size_t)w * h * 3);
    for (int i = 0; i < w * h; ++i) {
        rgb[(size_t)i*3+0] = ws.lastRgba[(size_t)i*4+0];
        rgb[(size_t)i*3+1] = ws.lastRgba[(size_t)i*4+1];
        rgb[(size_t)i*3+2] = ws.lastRgba[(size_t)i*4+2];
    }
    const int ok = stbi_write_jpg(path.c_str(), w, h, 3, rgb.data(), 85);
    if (!ok) {
        ws.lastErr = "JPEG write failed: " + path;
        return;
    }
    ws.lastShotPath = path;

    // Mark next coverage position as captured (round-robin through 11)
    const int idx = ws.captureCount % 11;
    ws.captured[idx] = true;
    ws.captureCount++;

    // Async RPC — NEVER sync in draw path
    ws.addPending   = true;
    ws.sessionId    = session_id;
    ws.lastErr.clear();
    nlohmann::json args;
    args["session_id"] = session_id;
    args["paths"]      = nlohmann::json::array({path});
    st.rpc.call("reveng_add_images", args, [](const core::RpcResult& r) {
        WebcamState& ws2 = g_wcam;
        ws2.addPending = false;
        if (!r.ok) {
            ws2.lastErr = r.err.empty() ? "reveng_add_images failed" : r.err;
        } else if (r.result.is_object() && !r.result.value("ok", true)) {
            ws2.lastErr = r.result.value("reason", std::string("reveng_add_images failed"));
        }
    });
}

}  // anonymous namespace

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void drawWebcamCapture(AppState& st,
                       const std::string& session_id,
                       const std::string& session_dir) {
    WebcamState& ws = g_wcam;
    const float sc = ui::uiScale;

    updatePreviewFrame();

    // Device / status line
    if (!ws.camStatus.empty()) {
        ImGui::TextDisabled("Camera: %s", ws.camStatus.c_str());
    }
    if (!ws.camErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.55f, 0.2f, 1.0f));
        ImGui::TextWrapped("%s", ws.camErr.c_str());
        ImGui::PopStyleColor();
    }

    // Device picker when multiple devices are available
    if (ws.devices.size() > 1) {
        ImGui::SetNextItemWidth(220.0f * sc);
        const char* preview =
            (ws.deviceIdx >= 0 && ws.deviceIdx < (int)ws.devices.size())
                ? (ws.devices[(size_t)ws.deviceIdx].label.empty()
                       ? ws.devices[(size_t)ws.deviceIdx].device_id.c_str()
                       : ws.devices[(size_t)ws.deviceIdx].label.c_str())
                : "(select camera)";
        if (ImGui::BeginCombo("##cam_dev", preview)) {
            for (int i = 0; i < (int)ws.devices.size(); ++i) {
                const bool sel = (i == ws.deviceIdx);
                const std::string& lab = ws.devices[(size_t)i].label.empty()
                    ? ws.devices[(size_t)i].device_id
                    : ws.devices[(size_t)i].label;
                if (ImGui::Selectable(lab.c_str(), sel)) {
                    if (i != ws.deviceIdx) {
                        if (ws.cam) ws.cam->close();
                        ws.camOpened = false;
                        ws.deviceIdx = i;
                        ws.camErr.clear();
                        ws.lastOpenAttemptT = -100.0;  // allow immediate reopen
                    }
                }
                if (sel) ImGui::SetItemDefaultFocus();
            }
            ImGui::EndCombo();
        }
        ImGui::SameLine();
        if (ImGui::Button("Rescan##cam")) {
            if (ws.cam) ws.cam->close();
            ws.camOpened = false;
            ws.enumDone = false;
            ws.devices.clear();
            ws.camErr.clear();
            ws.lastOpenAttemptT = -100.0;
        }
    }

    // ---- layout: preview left, coverage ring right ----
    const float avW  = ImGui::GetContentRegionAvail().x;
    const float avH  = ImGui::GetContentRegionAvail().y;
    const float prevW = std::min(avW * 0.55f, 360.0f * sc);
    const float prevH = prevW * (float)ws.texH / (float)std::max(ws.texW, 1);
    const float ringDiam = std::min(avW - prevW - 24.0f * sc, avH - 80.0f * sc);
    const float ringR    = (ringDiam * 0.5f) * 0.85f;

    // Preview image
    if (ws.texId) {
        ImGui::Image((ImTextureID)(uintptr_t)ws.texId, ImVec2(prevW, prevH));
    } else {
        ImGui::Dummy(ImVec2(prevW, prevH));
    }

    // Shutter button below preview
    ImGui::Spacing();
    {
        const bool canShoot = !session_id.empty() && !ws.addPending && ws.haveFrame;
        if (!canShoot) ImGui::BeginDisabled();
        if (ImGui::Button("  Capture  ", ImVec2(prevW, 28.0f * sc))) {
            shutterFire(st, session_id, session_dir);
        }
        if (!canShoot) ImGui::EndDisabled();
        if (ws.addPending) {
            ImGui::SameLine();
            ImGui::TextDisabled("uploading…");
        }
    }

    // Shot counter + last path
    ImGui::Spacing();
    ImGui::TextDisabled("Shots: %d / 11", std::min(ws.captureCount, 11));
    if (!ws.lastShotPath.empty()) {
        ImGui::TextDisabled("%s", ws.lastShotPath.c_str());
    }
    if (!ws.lastErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.9f, 0.3f, 0.3f, 1.0f));
        ImGui::TextWrapped("%s", ws.lastErr.c_str());
        ImGui::PopStyleColor();
    }
    if (session_id.empty()) {
        ImGui::TextDisabled("(no active session — shutter disabled)");
    }

    // ---- coverage ring (right of preview) ----
    if (ringR > 20.0f) {
        ImGui::SameLine(prevW + 16.0f * sc);
        // Push an invisible child to anchor the draw-list correctly
        ImGui::BeginChild("##coverage_ring",
                          ImVec2(ringDiam + 20.0f * sc, avH),
                          ImGuiChildFlags_None);
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ImVec2 wp = ImGui::GetWindowPos();
        const ImVec2 ws2sz = ImGui::GetWindowSize();
        const ImVec2 centre(wp.x + ws2sz.x * 0.5f, wp.y + ringR + 10.0f * sc);
        drawCoverageRing(dl, centre, ringR, ws.captured, ws.blurred);

        ImGui::Dummy(ImVec2(0, ringDiam + 40.0f * sc));  // reserve vertical space
        ImGui::EndChild();
    }
}

void webcamCaptureShutdown() {
    if (g_wcam.cam) {
        g_wcam.cam->close();
        g_wcam.cam.reset();
    }
    if (g_wcam.texId) {
        glDeleteTextures(1, &g_wcam.texId);
        g_wcam.texId = 0;
    }
    g_wcam = WebcamState{};
}

}  // namespace reveng
}  // namespace ui
