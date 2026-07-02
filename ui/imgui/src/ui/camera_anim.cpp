#include "camera_anim.h"

#include <cmath>

#include "imgui.h"
#include "app/app_state.h"
#include "ui/viewport.h"

namespace ui {

static app::AppState* s_anim = nullptr;

static float shortestArcDeg(float a, float b) {
    float d = std::fmod(b - a, 360.0f);
    if (d < -180.0f) d += 360.0f;
    if (d >  180.0f) d -= 360.0f;
    return d;
}

void requestCameraAnimation(app::AppState& st, float yawDeg, float pitchDeg,
                            float zoom, const float pan[3]) {
    (void)st;
    ViewportState& vs = viewportState();
    vs.animYaw0   = vs.yaw;
    vs.animPitch0 = vs.pitch;
    vs.animZoom0  = vs.zoom;
    vs.animPan0[0] = vs.pan[0]; vs.animPan0[1] = vs.pan[1]; vs.animPan0[2] = vs.pan[2];

    vs.animYaw1   = vs.animYaw0 + shortestArcDeg(vs.animYaw0, yawDeg);
    float p = pitchDeg;
    if (p >  89.5f) p =  89.5f;
    if (p < -89.5f) p = -89.5f;
    vs.animPitch1 = p;
    vs.animZoom1  = (zoom < 0.0f) ? vs.animZoom0 : zoom;
    if (vs.animZoom1 < 0.1f) vs.animZoom1 = 0.1f;
    if (vs.animZoom1 > 10.0f) vs.animZoom1 = 10.0f;
    if (pan) { vs.animPan1[0] = pan[0]; vs.animPan1[1] = pan[1]; vs.animPan1[2] = pan[2]; }
    else     { vs.animPan1[0] = vs.animPan0[0]; vs.animPan1[1] = vs.animPan0[1];
               vs.animPan1[2] = vs.animPan0[2]; }

    vs.animT = 0.0f;
    vs.animActive = true;
    s_anim = &st;
}

void tickCameraAnimation(app::AppState& st) {
    (void)st;
    ViewportState& vs = viewportState();
    if (!vs.animActive) return;
    s_anim = &st;

    float dt = ImGui::GetIO().DeltaTime;
    if (dt <= 0.0f || dt > 0.25f) dt = 1.0f / 60.0f;
    vs.animT += dt;

    float u = (vs.animDur > 1e-4f) ? (vs.animT / vs.animDur) : 1.0f;
    if (u >= 1.0f) {
        vs.yaw   = vs.animYaw1;
        vs.pitch = vs.animPitch1;
        vs.zoom  = vs.animZoom1;
        vs.pan[0] = vs.animPan1[0]; vs.pan[1] = vs.animPan1[1]; vs.pan[2] = vs.animPan1[2];
        vs.animActive = false;
        return;
    }
    float e = u * u * (3.0f - 2.0f * u);
    vs.yaw   = vs.animYaw0   + (vs.animYaw1   - vs.animYaw0)   * e;
    vs.pitch = vs.animPitch0 + (vs.animPitch1 - vs.animPitch0) * e;
    vs.zoom  = vs.animZoom0  + (vs.animZoom1  - vs.animZoom0)  * e;
    for (int i = 0; i < 3; ++i)
        vs.pan[i] = vs.animPan0[i] + (vs.animPan1[i] - vs.animPan0[i]) * e;
}

bool cameraAnimating() {
    return s_anim && viewportState().animActive;
}

void cancelCameraAnimation() {
    if (s_anim) viewportState().animActive = false;
}

}  // namespace ui
