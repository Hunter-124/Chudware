#pragma once
// AI-CAD camera animation (W2-D).
//
// Smoothly tweens the orbit camera (yaw / pitch / zoom / pan) between view states so HUD presets,
// the gesture wheel, Normal-To and zoom-to-fit ease into place instead of snapping. The tween's
// from/to snapshots + clock live on AppState (see the camera-animation fields in app_state.h); the
// app has a single AppState, so cameraAnimating()/cancelCameraAnimation() reach it via a file-local
// back-pointer set on the last requestCameraAnimation.
//
// Conventions: yaw/pitch in degrees (yaw takes the shortest signed arc, wrapping ±180); zoom in the
// renderer's 0.1..10 range (1 = fit). pan is a world-space orbit-target offset (mm).
struct AppState;

namespace ui {

// Begin a tween from the current camera to the given target. zoom < 0 keeps the current zoom; a
// null pan keeps the current pan. Replaces any in-flight animation. Safe to call every frame from a
// held button — it re-snapshots from wherever the camera currently is.
void requestCameraAnimation(AppState& st, float yawDeg, float pitchDeg,
                            float zoom = -1.0f, const float pan[3] = nullptr);

// Advance the active tween by one frame (uses ImGui delta time). No-op when idle. Call once per
// frame from the viewport before renderOrbit.
void tickCameraAnimation(AppState& st);

// True while a tween is in flight (the camera is being driven, not the user).
bool cameraAnimating();

// Stop any in-flight tween, leaving the camera wherever it currently is (no snap to the target).
void cancelCameraAnimation();

}  // namespace ui
