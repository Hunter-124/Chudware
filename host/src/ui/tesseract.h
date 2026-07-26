#pragma once
// -------------------------------------------------------------------------------------------------
// tesseract.h — the Chudware brand mark: a live 3D projection of a rotating 4-cube.
//
// This replaces the static isometric cube glyph that used to stand in as the copilot logo. A cube
// is a 3-cube; the mark is now a 4-cube (tesseract / 8-cell) projected 4D -> 3D -> 2D every frame,
// so the logo is genuinely the shape it claims to be rather than an illustration of one.
//
// PURE DRAW: no ImGui item, no layout, no state. The caller owns the cursor, the hit box and the
// tint; this only appends lines to `dl`. Cheap enough for a per-frame header mark (32 edges).
//
// Three animation styles are offered (user-selectable, Settings -> Appearance -> Brand mark):
//
//   SimpleRotation  A tesseract in a SIMPLE 4D rotation: one invariant plane stays fixed and its
//                   orthogonal complement turns. The fixed plane is the one bisecting the figure
//                   from front-left to back-right and from top to bottom, i.e. span{ (1,0,1,0),
//                   (0,1,0,0) }; the rotating plane is therefore span{ (1,0,-1,0), (0,0,0,1) }.
//                   Perspective 4D -> 3D projection, so the mark reads as the familiar
//                   "inner cube turning itself inside out".
//
//   DoubleRotation  A DOUBLE rotation: two independent turns in two orthogonal 4D planes (xy and
//                   zw) at incommensurate rates, so the two planes never realign, the motion has
//                   no short loop, and — unlike a simple rotation — no direction of the figure
//                   stays fixed through it. Perspective 4D -> 3D projection.
//
//   EightCell       The same simple rotation as the first style, but with an ORTHOGRAPHIC
//                   4D -> 3D projection. Dropping the perspective divide collapses the two
//                   bounding cubes onto each other instead of nesting them, which is the classic
//                   "8-cell" reading: eight cubic cells sliding through one another.
// -------------------------------------------------------------------------------------------------

#include "imgui.h"

namespace ui {

enum class TesseractAnim : int {
    SimpleRotation = 0,
    DoubleRotation = 1,
    EightCell      = 2,
    Count          = 3,
};

// Short UI label for a style (Settings picker). Never null; unknown values clamp to the first.
const char* tesseractAnimName(TesseractAnim anim);
// One-line description for the same picker (tooltip / caption under the preview).
const char* tesseractAnimBlurb(TesseractAnim anim);

// Clamp an arbitrary int (a persisted setting) onto a valid style.
inline TesseractAnim tesseractAnimFromInt(int v) {
    if (v < 0 || v >= (int)TesseractAnim::Count) return TesseractAnim::SimpleRotation;
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

// Busy / loading flavour of the same mark: a fixed style (always the simple rotation, for
// legibility at indicator sizes), a livelier spin, and a reduced edge set below ~18 px. Use this
// wherever the product needs to say "working" so every such indicator is the brand mark in
// motion rather than a generic arc. `size` is the ink box, as above; guard the call with
// `size >= kTesseractSpinnerMinPx`.
void drawTesseractSpinner(ImDrawList* dl, ImVec2 center, float size, double timeSec, ImU32 col,
                          float thickness = 0.0f);

}  // namespace ui
