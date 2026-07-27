#pragma once
// -------------------------------------------------------------------------------------------------
// tesseract.h — the Chudware brand mark: a live 3D projection of a rotating 4-cube.
//
// This replaces the static isometric cube glyph that used to stand in as the copilot logo. A cube
// is a 3-cube; the mark is now a 4-cube (tesseract / 8-cell) projected 4D -> 3D -> 2D every frame,
// so the logo is genuinely the shape it claims to be rather than an illustration of one.
//
// PURE DRAW: no ImGui item, no layout, no state. The caller owns the cursor, the hit box and the
// tint; this only appends lines and vertex dots to `dl`. Cheap enough for a per-frame header mark
// (32 edges + 16 dots).
//
// Four animation styles are offered (user-selectable, Settings -> Appearance -> Brand mark).
// All use a perspective 4D -> 3D projection, so the mark always reads as the familiar nested
// "cube inside a cube"; the styles differ in HOW it turns:
//
//   EightCell       A SIMPLE 4D rotation in the zw plane: the fourth axis folds through the
//                   viewing direction, so the inner cell swells toward the camera, swallows the
//                   outer one and the figure turns itself inside out — the canonical tesseract
//                   motion, and the one the busy spinner always uses.
//
//   DoubleRotation  A DOUBLE rotation: two independent turns in two orthogonal 4D planes (xy and
//                   zw) at incommensurate rates, so the two planes never realign, the motion has
//                   no short loop, and — unlike a simple rotation — no direction of the figure
//                   stays fixed through it.
//
//   Isoclinic       A double rotation whose two planes turn in LOCKSTEP (one shared phase).
//                   Every vertex moves at the same speed along a circle — a Hopf fibre — so the
//                   figure rolls smoothly and never turns inside out.
//
//   Octave          A double rotation whose planes are locked at exactly 2:1 (one shared phase,
//                   the zw plane turning twice per xy turn): the figure folds inside out twice
//                   for every roll and returns to its start each loop — the only style with a
//                   short, closed period.
// -------------------------------------------------------------------------------------------------

#include "imgui.h"

namespace ui {

enum class TesseractAnim : int {
    EightCell      = 0,
    DoubleRotation = 1,
    Isoclinic      = 2,
    Octave         = 3,
    Count          = 4,
};

// Short UI label for a style (Settings picker). Never null; unknown values clamp to the first.
const char* tesseractAnimName(TesseractAnim anim);
// One-line description for the same picker (tooltip / caption under the preview).
const char* tesseractAnimBlurb(TesseractAnim anim);

// Clamp an arbitrary int (a persisted setting) onto a valid style. Out-of-range values (older
// config encodings) fall back to the default 8-cell.
inline TesseractAnim tesseractAnimFromInt(int v) {
    if (v < 0 || v >= (int)TesseractAnim::Count) return TesseractAnim::EightCell;
    return (TesseractAnim)v;
}

// Draw the mark centred on `center`, inscribed in a `size` x `size` box: the projection is
// pre-fitted per style so the figure never breathes or clips as it turns, and the stroke is inset
// so `size` bounds the INK, not just the vertices. `timeSec` drives the animation — pass
// ImGui::GetTime(); `col` is the ink (its alpha scales the whole mark, so a status tint works
// directly). `thickness` in pixels; 0 picks a size-appropriate stroke.
void drawTesseract(ImDrawList* dl, ImVec2 center, float size, double timeSec,
                   TesseractAnim anim, ImU32 col, float thickness = 0.0f);

// Smallest box in which the mark still reads as a turning solid rather than a smudge (measured on
// a rendered size ladder). Busy indicators smaller than this — the ~6 dp status dots inside chips
// and pills — MUST keep their existing arc or dot: a 16-vertex wireframe in 8 px is noise, and
// shipping one there would be worse than the thing it replaced.
inline constexpr float kTesseractSpinnerMinPx = 12.0f;

// Busy / loading flavour of the same mark: a fixed style (always the 8-cell's inside-out, the
// most legible motion at indicator sizes), a livelier spin, and a reduced edge set below ~18 px. Use this
// wherever the product needs to say "working" so every such indicator is the brand mark in
// motion rather than a generic arc. `size` is the ink box, as above; guard the call with
// `size >= kTesseractSpinnerMinPx`.
void drawTesseractSpinner(ImDrawList* dl, ImVec2 center, float size, double timeSec, ImU32 col,
                          float thickness = 0.0f);

}  // namespace ui
