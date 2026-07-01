// AI-CAD camera animation. See camera_anim.h.
#include "camera_anim.h"

#include <cmath>

namespace aicad::ui {

// Shortest signed angular delta from a to b, in degrees, wrapped to (-180, 180]. Keeps yaw tweens
// from spinning the long way around (e.g. 170 -> -170 goes +20, not -340).
static float shortestArcDeg(float a, float b) {
    float d = std::fmod(b - a, 360.0f);
    if (d < -180.0f) d += 360.0f;
    if (d > 180.0f) d -= 360.0f;
    return d;
}

void requestCameraAnimation(CameraAnim& anim, const CameraState& from,
                             float yawDeg, float pitchDeg, float distance,
                             const float target[3],
                             float minDistance, float maxDistance,
                             float duration) {
    // Snapshot the current camera as the start; the targets default to "keep current".
    anim.yaw0 = from.yaw;
    anim.pitch0 = from.pitch;
    anim.distance0 = from.distance;
    anim.target0[0] = from.target[0];
    anim.target0[1] = from.target[1];
    anim.target0[2] = from.target[2];

    // Resolve the end state. Yaw end is start + shortest signed arc so the tween wraps correctly;
    // pitch is clamped to the renderer's gimbal guard (+-89.5 deg).
    anim.yaw1 = anim.yaw0 + shortestArcDeg(anim.yaw0, yawDeg);
    float p = pitchDeg;
    if (p > 89.5f) p = 89.5f;
    if (p < -89.5f) p = -89.5f;
    anim.pitch1 = p;

    anim.distance1 = (distance < 0.0f) ? anim.distance0 : distance;
    if (anim.distance1 < minDistance) anim.distance1 = minDistance;
    if (anim.distance1 > maxDistance) anim.distance1 = maxDistance;

    if (target) {
        anim.target1[0] = target[0];
        anim.target1[1] = target[1];
        anim.target1[2] = target[2];
    } else {
        anim.target1[0] = anim.target0[0];
        anim.target1[1] = anim.target0[1];
        anim.target1[2] = anim.target0[2];
    }

    if (duration > 0.0f) anim.duration = duration;

    anim.t = 0.0f;
    anim.active = true;
}

void tickCameraAnimation(CameraAnim& anim, CameraState& out, float dt) {
    if (!anim.active) return;

    if (dt <= 0.0f || dt > 0.25f) dt = 1.0f / 60.0f;  // guard against pauses / first frame
    anim.t += dt;

    float u = (anim.duration > 1e-4f) ? (anim.t / anim.duration) : 1.0f;
    if (u >= 1.0f) {
        // Land exactly on the target and stop.
        out.yaw = anim.yaw1;
        out.pitch = anim.pitch1;
        out.distance = anim.distance1;
        out.target[0] = anim.target1[0];
        out.target[1] = anim.target1[1];
        out.target[2] = anim.target1[2];
        anim.active = false;
        return;
    }
    // Smoothstep ease (3u^2 - 2u^3).
    float e = u * u * (3.0f - 2.0f * u);
    out.yaw = anim.yaw0 + (anim.yaw1 - anim.yaw0) * e;
    out.pitch = anim.pitch0 + (anim.pitch1 - anim.pitch0) * e;
    out.distance = anim.distance0 + (anim.distance1 - anim.distance0) * e;
    for (int i = 0; i < 3; ++i)
        out.target[i] = anim.target0[i] + (anim.target1[i] - anim.target0[i]) * e;
}

bool cameraAnimating(const CameraAnim& anim) {
    return anim.active;
}

void cancelCameraAnimation(CameraAnim& anim) {
    anim.active = false;
}

}  // namespace aicad::ui
