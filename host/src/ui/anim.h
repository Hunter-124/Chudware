#pragma once
// Chudware motion language — the SINGLE place UI timing, easing and continuous-redraw
// requests are defined.
//
// Why this exists
// ---------------
// Two problems this header solves, and neither is cosmetic:
//
// 1. THE APP DOES NOT RENDER CONTINUOUSLY. app.cpp's frame loop idles on
//    glfwWaitEventsTimeout(0.05) — a 20 Hz floor — unless something declares itself busy.
//    Before this header the ONLY way to raise that floor was AppState::animActive, which
//    belongs to the 3D CAD viewport's camera tween (ui/camera_anim.*) and is cleared on
//    document-tab switches. Every other animation in the program (the PLM graph's blast
//    pulse, the tesseract spinner, hover washes) therefore ran at whatever cadence stray
//    mouse events happened to produce — smooth while the cursor moved, a 20 Hz stutter the
//    instant it stopped. requestFrames() is the general fix: any painter that needs the next
//    frame soon says so, every frame, for as long as it is moving.
//
// 2. EVERY SITE HAND-ROLLED ITS OWN CURVE. Durations and smoothsteps were scattered as bare
//    literals (0.35f here, 0.25f there, a raw t*t*(3-2t) inlined in four files), so nothing in
//    the product moved on the same curve. The k* tokens and ease* functions below are the
//    shared vocabulary; new motion MUST come from here rather than a fresh literal.
//
// Conventions
// -----------
// * All durations are SECONDS (wall clock), not frames — motion must look identical at 20 Hz
//   and 144 Hz.
// * All ease functions take and return normalized t in [0,1] and are clamped at both ends, so
//   passing an out-of-range t is safe and saturates rather than exploding.
// * toward()/pulse()/phase() call requestFrames() for you: if you use them, you get smooth
//   frames automatically, and the request stops the moment the value settles or the caller
//   stops drawing. Nothing has to be un-registered.
// * GUI thread only. No locking; the state tables are plain file-statics.

#include "imgui.h"

namespace ui {
namespace anim {

// ---- duration tokens (seconds) ------------------------------------------------------------
// Pick the smallest token that reads as motion. Anything slower than kSlow on an interactive
// control feels broken, not premium.
inline constexpr float kInstant = 0.08f;  // press/release feedback, checkbox fill
inline constexpr float kFast    = 0.14f;  // hover wash, chip tint, row highlight
inline constexpr float kBase    = 0.22f;  // panel expand/collapse, tab switch, selection move
inline constexpr float kSlow    = 0.38f;  // card entrance, drawer slide, camera dolly
inline constexpr float kStagger = 0.02f;  // per-item delay when a list enters as a wave

// Ambient (never-ending) motion periods.
inline constexpr float kBreathe = 2.40f;  // slow pulse for "this is alive / recently changed"
inline constexpr float kMarch   = 1.10f;  // dashed-edge / flow-line phase wrap

// ---- easing --------------------------------------------------------------------------------
// easeOut     — decelerating cubic. The default for anything the user triggered: it starts fast
//               so the UI feels instantly responsive, then settles.
// easeInOut   — symmetric smoothstep. For motion the user did NOT trigger (auto-scroll, camera
//               drift) where an abrupt start would look like a glitch.
// easeOutBack — decelerating with a small overshoot. Entrances only; never on a value the user
//               is reading, because it briefly displays a wrong number.
// easeSpring  — critically-damped settle, no overshoot. For chasing a moving target.
float easeOut(float t);
float easeInOut(float t);
float easeOutBack(float t);
float easeSpring(float t);

// ---- frame pump ------------------------------------------------------------------------------
// requestFrames() asks the frame loop to poll instead of idling on the NEXT iteration. It is a
// per-frame declaration, not a subscription: call it EVERY frame you are moving, and simply stop
// calling it when you are done. There is no "release" and no leak — a panel that stops drawing
// (because its tab closed) automatically stops requesting.
void requestFrames();

// Consumed once per frame by app.cpp's idle-throttle block: reads the flag and clears it. No
// other caller should invoke this; framesRequested() is for the loop, not for panels.
bool consumeFrameRequest();

// Frame delta in seconds, clamped to [0, 0.05]. Use this instead of raw ImGui DeltaTime so a
// stall (debugger breakpoint, blocking RPC, window drag) cannot teleport an animation to its end
// state — a 4-second hitch would otherwise finish every tween on the resume frame.
float dt();

// ---- stateless per-id tweens -----------------------------------------------------------------
// toward() eases a stored scalar toward `target` and returns the current value. State is keyed by
// `id`, so callers stay stateless — no member to declare, no reset to remember. Typical use is a
// hover/expand factor:
//
//     const float t = ui::anim::toward(ImGui::GetID("##row_hover"), hovered ? 1.f : 0.f,
//                                      ui::anim::kFast);
//     dl->AddRectFilled(mn, mx, washColor(t));
//
// The easing is a frame-rate-independent exponential settle, so it is correct at any cadence and
// re-targeting mid-flight is continuous (no snap). Frames are requested automatically while the
// value is in motion and NOT requested once it has settled, so a static UI still idles at 20 Hz.
float toward(ImGuiID id, float target, float seconds = kBase);

// Current value without advancing or requesting frames — for read-only queries (e.g. deciding
// whether to draw an expanded body at all). Returns `fallback` for an id never passed to toward().
float peek(ImGuiID id, float fallback = 0.0f);

// Drop a tween's state. Only needed when an id is genuinely retired (a closed document, a
// destroyed row) and you want the next appearance to animate in from scratch rather than resume.
void forget(ImGuiID id);

// ---- ambient phases ---------------------------------------------------------------------------
// pulse() returns a smooth 0..1..0 ping-pong over `periodSec` — the "breathing" curve for
// recently-changed markers and live indicators. phase() returns a monotonic 0..1 sawtooth that
// wraps every `periodSec` — the driver for marching-ants dashes and flow lines.
//
// BOTH request frames on every call, because ambient motion by definition never settles. Only
// call them while the animated thing is actually on screen; guarding on visibility is how the app
// gets to idle at 20 Hz when nothing is moving.
float pulse(float periodSec = kBreathe);
float phase(float periodSec = kMarch);

// ---- staggered entrances -----------------------------------------------------------------------
// Progress of item `index` in a list that entered at `startTime` (an ImGui::GetTime() stamp),
// where each item is delayed by kStagger and runs for `seconds`. Returns 1.0 once complete;
// requests frames only while at least one item is still moving.
//
//     const float a = ui::anim::stagger(S.enterT0, i, ui::anim::kSlow);
//     if (a < 1.f) { /* offset / fade the item by (1 - a) */ }
float stagger(double startTime, int index, float seconds = kSlow);

}  // namespace anim
}  // namespace ui
