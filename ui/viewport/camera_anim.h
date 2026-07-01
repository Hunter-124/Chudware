#pragma once
// AI-CAD camera animation -- framework-agnostic port of host/src/ui/camera_anim.
//
// Smoothly tweens an orbit camera (yaw / pitch / distance / target) between two states so preset
// views (Front/Top/Right/Isometric), Normal-To, and zoom-to-fit ease into place instead of
// snapping. This is plain-data + free functions: the caller owns a CameraAnim instance and drives
// it explicitly with tickCameraAnimation(dt) each frame. There is no global back-pointer, no
// ImGui dependency, and no dependency on the host's AppState -- unlike the host/ original, which
// pulled its per-frame delta time from ImGui::GetIO() and stored the tween on a single global
// AppState reached via a static pointer.
//
// Conventions match aicad::ui::CameraState (see ui/abstract/viewport.h): yaw/pitch in degrees
// (yaw takes the shortest signed arc, wrapping +-180); distance/target follow whatever units the
// concrete IViewport uses (the legacy renderer's zoom convention was 0.1..10, 1 = fit -- callers
// that still want that range can pass it via minDistance/maxDistance).
#include "../abstract/viewport.h"

namespace aicad::ui {

// Holds one in-flight (or idle) camera tween. Caller owns the instance (e.g. as a member of its
// viewport controller) and passes it by reference to every call below.
struct CameraAnim {
    bool active = false;
    float t = 0.0f;          // elapsed seconds since requestCameraAnimation
    float duration = 0.25f;  // tween length in seconds

    // Snapshot of the camera when the tween began.
    float yaw0 = 0.0f, pitch0 = 0.0f, distance0 = 5.0f;
    float target0[3] = {0.0f, 0.0f, 0.0f};

    // Resolved end state (yaw0 + shortest signed arc; pitch clamped to the gimbal guard).
    float yaw1 = 0.0f, pitch1 = 0.0f, distance1 = 5.0f;
    float target1[3] = {0.0f, 0.0f, 0.0f};
};

// Begin a tween from `from` to the given target. distance < 0 keeps from.distance; a null target
// keeps from.target. Replaces any in-flight animation. minDistance/maxDistance clamp the resolved
// end distance (defaults match the legacy renderer's 0.1..10 zoom range); duration <= 0 uses the
// CameraAnim's current duration (default 0.25s).
void requestCameraAnimation(CameraAnim& anim, const CameraState& from,
                             float yawDeg, float pitchDeg, float distance = -1.0f,
                             const float target[3] = nullptr,
                             float minDistance = 0.1f, float maxDistance = 10.0f,
                             float duration = -1.0f);

// Advance the tween by dt seconds (caller-supplied; typically the frame's delta time) and write
// the resulting camera into `out`. No-op when idle -- `out` is left unchanged. Call once per frame
// before rendering.
void tickCameraAnimation(CameraAnim& anim, CameraState& out, float dt);

// True while a tween is in flight (the camera is being driven, not the user).
bool cameraAnimating(const CameraAnim& anim);

// Stop the tween, leaving the camera wherever it currently is (no snap to the target).
void cancelCameraAnimation(CameraAnim& anim);

}  // namespace aicad::ui
