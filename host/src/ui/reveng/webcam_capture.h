// webcam_capture.h — live webcam preview + shutter widget (RE-19)
// Draws an ImGui child panel with:
//   • Live preview: grabs frames via a stub CameraBackend (works headless — stub returns a
//     solid-colour test frame when no device is available, matching RE-07 contract).
//   • Coverage overlay: OpenScan-style 11-shot dome ring drawn with ImDrawList.
//     Captured positions = green, pending = white, blurred/uncertain = amber.
//   • Shutter button: writes JPEG to the session dir and fires async reveng_add_images.
// GL-thread only.  All RPC calls are ASYNC (st.rpc.call) — never sync in a draw path.
#pragma once

#include <string>

struct AppState;

namespace ui {
namespace reveng {

// Draw the webcam capture widget inside the current ImGui layout region.
// session_id: active reveng session (may be empty → shutter is disabled).
// session_dir: filesystem path where captured frames are written.
void drawWebcamCapture(AppState& st,
                       const std::string& session_id,
                       const std::string& session_dir);

// Release the GL texture + stub camera resources held by the widget.
// Call once on workspace teardown.
void webcamCaptureShutdown();

}  // namespace reveng
}  // namespace ui
