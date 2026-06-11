// AI-CAD camera animation (W2-D). See camera_anim.h.
#include "camera_anim.h"

#include <cmath>

#include "imgui.h"

#include "../core/app_state.h"

namespace ui {

// The single AppState currently being animated (the app is single-document/single-window). Used so
// the no-arg cameraAnimating()/cancelCameraAnimation() can reach the tween that lives on AppState.
static AppState* s_anim = nullptr;

// Shortest signed angular delta from a to b, in degrees, wrapped to (-180, 180]. Keeps yaw tweens
// from spinning the long way around (e.g. 170° -> -170° goes +20°, not -340°).
static float shortestArcDeg(float a, float b) {
    float d = std::fmod(b - a, 360.0f);
    if (d < -180.0f) d += 360.0f;
    if (d >  180.0f) d -= 360.0f;
    return d;
}

void requestCameraAnimation(AppState& st, float yawDeg, float pitchDeg,
                            float zoom, const float pan[3]) {
    // Snapshot the current camera as the start; the targets default to "keep current".
    st.animYaw0   = st.yaw;
    st.animPitch0 = st.pitch;
    st.animZoom0  = st.zoom;
    st.animPan0[0] = st.pan[0]; st.animPan0[1] = st.pan[1]; st.animPan0[2] = st.pan[2];

    // Resolve the end state. Yaw end is start + shortest signed arc so the tween wraps correctly;
    // pitch is clamped to the renderer's gimbal guard (±89.5°).
    st.animYaw1   = st.animYaw0 + shortestArcDeg(st.animYaw0, yawDeg);
    float p = pitchDeg;
    if (p >  89.5f) p =  89.5f;
    if (p < -89.5f) p = -89.5f;
    st.animPitch1 = p;
    st.animZoom1  = (zoom < 0.0f) ? st.animZoom0 : zoom;
    if (st.animZoom1 < 0.1f) st.animZoom1 = 0.1f;
    if (st.animZoom1 > 10.0f) st.animZoom1 = 10.0f;
    if (pan) { st.animPan1[0] = pan[0]; st.animPan1[1] = pan[1]; st.animPan1[2] = pan[2]; }
    else     { st.animPan1[0] = st.animPan0[0]; st.animPan1[1] = st.animPan0[1];
               st.animPan1[2] = st.animPan0[2]; }

    st.animT = 0.0f;
    st.animActive = true;
    s_anim = &st;
}

void tickCameraAnimation(AppState& st) {
    if (!st.animActive) return;
    s_anim = &st;

    float dt = ImGui::GetIO().DeltaTime;
    if (dt <= 0.0f || dt > 0.25f) dt = 1.0f / 60.0f;  // guard against pauses / first frame
    st.animT += dt;

    float u = (st.animDur > 1e-4f) ? (st.animT / st.animDur) : 1.0f;
    if (u >= 1.0f) {
        // Land exactly on the target and stop.
        st.yaw   = st.animYaw1;
        st.pitch = st.animPitch1;
        st.zoom  = st.animZoom1;
        st.pan[0] = st.animPan1[0]; st.pan[1] = st.animPan1[1]; st.pan[2] = st.animPan1[2];
        st.animActive = false;
        return;
    }
    // Smoothstep ease (3u^2 - 2u^3).
    float e = u * u * (3.0f - 2.0f * u);
    st.yaw   = st.animYaw0   + (st.animYaw1   - st.animYaw0)   * e;
    st.pitch = st.animPitch0 + (st.animPitch1 - st.animPitch0) * e;
    st.zoom  = st.animZoom0  + (st.animZoom1  - st.animZoom0)  * e;
    for (int i = 0; i < 3; ++i)
        st.pan[i] = st.animPan0[i] + (st.animPan1[i] - st.animPan0[i]) * e;
}

bool cameraAnimating() {
    return s_anim && s_anim->animActive;
}

void cancelCameraAnimation() {
    if (s_anim) s_anim->animActive = false;
}

}  // namespace ui
