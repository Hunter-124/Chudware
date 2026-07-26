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
// Two animation styles are offered (user-selectable, Settings -> Appearance -> Brand mark). Both
// use a perspective 4D -> 3D projection, so the mark always reads as the familiar nested
// "cube inside a cube" turning itself inside out:
//
//   EightCell       An 8-cell in a SIMPLE 4D rotation: one invariant plane stays fixed and its
//                   orthogonal complement turns. The fixed plane bisects the figure from
//                   front-left to back-right and from top to bottom, i.e. span{ (1,0,1,0),
//                   (0,1,0,0) }; the rotating plane is therefore span{ (1,0,-1,0), (0,0,0,1) }.
//                   Modelled on the reference animation at
//                   https://commons.wikimedia.org/wiki/File:8-cell-simple.gif — the camera and
//                   the 4D eye distance below were fitted against its frames, not guessed.
//
//   DoubleRotation  A DOUBLE rotation: two independent turns in two orthogonal 4D planes (xy and
//                   zw) at incommensurate rates, so the two planes never realign, the motion has
//                   no short loop, and — unlike a simple rotation — no direction of the figure
//                   stays fixed through it.
// -------------------------------------------------------------------------------------------------

#include "imgui.h"

namespace ui {

enum class TesseractAnim : int {
    EightCell      = 0,
    DoubleRotation = 1,
    Count          = 2,
};

// Short UI label for a style (Settings picker). Never null; unknown values clamp to the first.
const char* tesseractAnimName(TesseractAnim anim);
// One-line description for the same picker (tooltip / caption under the preview).
const char* tesseractAnimBlurb(TesseractAnim anim);

// Clamp an arbitrary int (a persisted setting) onto a valid style. Callers loading a config
// written before the styles were reduced to two must migrate the value FIRST — see
// loadSettings(): a bare clamp would silently turn the old 8-cell into the double rotation.
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

// Busy / loading flavour of the same mark: a fixed style (always the simple rotation, for
// legibility at indicator sizes), a livelier spin, and a reduced edge set below ~18 px. Use this
// wherever the product needs to say "working" so every such indicator is the brand mark in
// motion rather than a generic arc. `size` is the ink box, as above; guard the call with
// `size >= kTesseractSpinnerMinPx`.
void drawTesseractSpinner(ImDrawList* dl, ImVec2 center, float size, double timeSec, ImU32 col,
                          float thickness = 0.0f);

}  // namespace ui
