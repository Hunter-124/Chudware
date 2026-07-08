// webcam_capture.cpp — live webcam preview + coverage overlay + shutter (RE-19)
// Draws a panel with:
//   1. Live preview via a stub frame generator (no CameraBackend dependency — RE-07 owns
//      that header; we generate a solid-colour test pattern headlessly so the widget works
//      in AI_CAD_SHOT=reveng_capture without a real device).
//   2. OpenScan-style 11-shot dome coverage ring (ImDrawList) — green=captured, amber=blurred.
//   3. Shutter button: encodes frame as JPEG (stb_image_write), fires async reveng_add_images.
//
// GL-thread only.  NEVER call st.rpc.call synchronously in a draw path (known freeze bug).
#include "webcam_capture.h"

#include <cstdio>
#include <cstring>
#include <cmath>
#include <string>
#include <vector>
#include <algorithm>

#include "imgui.h"
#include <glad/gl.h>
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"

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
    // Stub frame texture
    GLuint texId  = 0;
    int    texW   = 320;
    int    texH   = 240;
    bool   texDirty = true;   // needs a fresh upload

    // Coverage tracking (which of the 11 shot positions have been captured)
    bool captured[11] = {};
    bool blurred[11]  = {};   // quality too low (stub: never)
    int  captureCount = 0;

    // Async shutter state
    bool   shuttingDown = false;
    bool   addPending   = false;
    std::string lastShotPath;
    std::string lastErr;
    std::string sessionId;

    // Stub frame: hue cycles to show liveness
    float hue = 0.0f;
};

static WebcamState g_wcam;

// ---------------------------------------------------------------------------
// Stub frame generator — produces a solid colour RGBA frame cycling through hue.
// When a real CameraBackend is available (RE-07) this would call grabFrame() instead.
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

// Upload/update the stub preview texture.
static void updateStubTex() {
    WebcamState& ws = g_wcam;
    std::vector<unsigned char> px;
    buildStubFrameRGBA(px, ws.texW, ws.texH, ws.hue);

    if (!ws.texId) {
        GLuint id = 0;
        glGenTextures(1, &id);
        ws.texId = id;
    }
    glBindTexture(GL_TEXTURE_2D, ws.texId);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, ws.texW, ws.texH, 0,
                 GL_RGBA, GL_UNSIGNED_BYTE, px.data());
    glBindTexture(GL_TEXTURE_2D, 0);
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
// JPEG encode + async RPC
// ---------------------------------------------------------------------------

static void shutterFire(AppState& st,
                        const std::string& session_id,
                        const std::string& session_dir) {
    WebcamState& ws = g_wcam;
    if (ws.addPending) return;  // already in-flight

    // Build a stub RGBA frame (same as the preview)
    std::vector<unsigned char> px;
    buildStubFrameRGBA(px, ws.texW, ws.texH, ws.hue);

    // Determine next shot index & path
    char fname[64];
    std::snprintf(fname, sizeof(fname), "webcam_%03d.jpg", ws.captureCount);
    std::string path = session_dir.empty() ? std::string(".") : session_dir;
    if (path.back() != '/') path += '/';
    path += fname;

    // Encode JPEG via stb_image_write (component order RGB — drop alpha)
    std::vector<unsigned char> rgb((size_t)ws.texW * ws.texH * 3);
    for (int i = 0; i < ws.texW * ws.texH; ++i) {
        rgb[(size_t)i*3+0] = px[(size_t)i*4+0];
        rgb[(size_t)i*3+1] = px[(size_t)i*4+1];
        rgb[(size_t)i*3+2] = px[(size_t)i*4+2];
    }
    const int ok = stbi_write_jpg(path.c_str(), ws.texW, ws.texH, 3, rgb.data(), 85);
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

    // Advance stub frame hue for liveness (~0.5 deg / frame)
    ws.hue += 0.0014f;
    if (ws.hue > 1.0f) ws.hue -= 1.0f;
    updateStubTex();

    // ---- layout: preview left, coverage ring right ----
    const float avW  = ImGui::GetContentRegionAvail().x;
    const float avH  = ImGui::GetContentRegionAvail().y;
    const float prevW = std::min(avW * 0.55f, 360.0f * sc);
    const float prevH = prevW * (float)ws.texH / (float)ws.texW;
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
        const bool canShoot = !session_id.empty() && !ws.addPending;
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
    if (g_wcam.texId) {
        glDeleteTextures(1, &g_wcam.texId);
        g_wcam.texId = 0;
    }
    g_wcam = WebcamState{};
}

}  // namespace reveng
}  // namespace ui
