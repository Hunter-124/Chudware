// webcam_capture.h — live webcam capture console for the Reverse Engineering Sources stage.
//
// RE-40 rebuild. The old widget was a 640x480 preview, a blind round-robin coverage ring and a
// shutter that would happily JPEG-encode a software test pattern into a real session. The console
// this header now declares is built on the camera.h v2 interface and the ui::reveng::kit vocabulary:
//
//   • Device card    — device combo (listDevices), Rescan, resolution/mode combo (listModes),
//                      negotiated activeMode() chip, lastError() surfaced verbatim.
//   • Controls card  — Focus / Exposure / White balance / Gain / Zoom / Brightness sliders bound to
//                      getControl / setControl / setControlAuto, plus lockForCapture() — the
//                      photogrammetry pre-flight. Unsupported controls render greyed, never hidden.
//   • Preview card   — 16:9 hero at metrics::rePreviewMinW()..rePreviewMaxW() with the OpenScan
//                      dome overlaid, a live variance-of-Laplacian sharpness readout, the shot
//                      counter, an inline capture strip of the last shots, and the shutter.
//
// HONESTY INVARIANTS enforced here (contracts/reveng.md §7):
//   • A frame with CameraFrame::synthetic set can NEVER be written into a session. The shutter is
//     hard-blocked in shutterFire(), not merely disabled in the UI, and the preview carries an
//     unmistakable "SYNTHETIC PREVIEW" band.
//   • Coverage comes from get_reveng_coverage(), never from a local capture counter, and the ring
//     states whether the backend measured it from poses or merely estimated it from a photo count.
//   • Blur is scored with chudware::platform::frameSharpness() against kSharpnessBlurThreshold,
//     shown BEFORE the shutter is pressed, and a blurry shutter press is refused-with-warning
//     (with an explicit "Capture anyway") rather than silently accepted.
//
// GL-thread only. All RPC is ASYNC via st.rpc.call — a sync worker call in a draw path is a
// known freeze bug. grabFrame() is polled non-blocking (GrabResult::again is the steady state);
// there is no capture thread, because the reveng store is a mutex-free GL-thread singleton.
#pragma once

#include <cstdint>
#include <string>

struct AppState;

namespace ui {
namespace reveng {

// Draw the webcam capture console inside the current ImGui layout region.
// session_id: active reveng session (empty → shutter disabled with a reason).
// session_dir: filesystem path where captured frames are written (empty → derived from
//              $CHUDWARE_REVENG_DIR / XDG_DATA_HOME, mirroring the worker's session layout).
void drawWebcamCapture(AppState& st,
                       const std::string& session_id,
                       const std::string& session_dir);

// Stop/finalize live capture and clear per-session state on selection changes.
// Releases the device so no images can leak into a newly selected session; an outstanding
// ingest retains its originating session and is drained before that run is finalized.
void webcamCaptureResetSession();

// Monotonic session-selection epoch for source RPC callbacks (including A -> B -> A).
uint64_t webcamCaptureSessionGeneration();

// Gaussian panel Finish shares the camera's ingest-drain boundary. False means no native run.
bool webcamCaptureFinishGaussian(AppState& st, const std::string& session_id);

// Release the GL textures + CameraBackend held by the console. Workspace teardown / app shutdown
// only. Leaving the RE tab without this leaks the camera fd and the preview texture.
void webcamCaptureShutdown();

}  // namespace reveng
}  // namespace ui
