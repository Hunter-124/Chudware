#pragma once
// Smooth camera tweens for HUD presets, gesture wheel, view selector, zoom-to-fit.
namespace app { struct AppState; }

namespace ui {

void requestCameraAnimation(app::AppState& st, float yawDeg, float pitchDeg,
                            float zoom = -1.0f, const float pan[3] = nullptr);
void tickCameraAnimation(app::AppState& st);
bool cameraAnimating();
void cancelCameraAnimation();

}  // namespace ui
