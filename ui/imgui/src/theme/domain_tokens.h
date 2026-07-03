// Domain color tokens — semantic, theme-independent colors for domain canvases.
// Drawing sheets are ink-on-paper (paper is always white regardless of app theme);
// CAM backplots follow industry move-type conventions; feature-tree category strips
// key on feature family. This header lives in ui/imgui/src/theme/ — the single
// authorized home for color literals (scripts/ui_style_check.py rule=color).
// Values are byte-identical to the host-ported literals they replaced (T-G9 P1
// de-literalization of the T-G8 wave-4 debt) so rendering is unchanged.
#pragma once
#include "imgui.h"

namespace theme {

// ---- 2D drawing sheet (drawing_panel.cpp) ------------------------------------------------------
namespace draw2d {
// canvas + paper
inline ImU32 canvasBackdrop() { return IM_COL32(30, 32, 38, 255); }    // darker than panel so paper reads as paper
inline ImU32 paper()          { return IM_COL32(252, 252, 250, 255); } // paper is always white
inline ImU32 paperShadow()    { return IM_COL32(0, 0, 0, 60); }
inline ImU32 paperBorder()    { return IM_COL32(60, 64, 74, 255); }    // sheet frame, titleblock frame, BOM border
inline ImU32 trimLine()       { return IM_COL32(150, 154, 164, 200); } // inner margin line, BOM header underline
// inks (fixed dark inks, independent of theme, for legibility on white paper)
inline ImU32 inkVisible()     { return IM_COL32(28, 32, 40, 255); }
inline ImU32 inkHidden()      { return IM_COL32(120, 126, 138, 230); }
inline ImU32 inkTitle()       { return IM_COL32(40, 44, 52, 255); }    // titleblock values, BOM text
inline ImU32 mutedLabel()     { return IM_COL32(120, 126, 138, 255); } // titleblock keys, BOM empty note
inline ImU32 emptyMsg()       { return IM_COL32(150, 154, 164, 255); } // "No sheet to display"
// dimensions + annotations
inline ImU32 dimCritical()    { return IM_COL32(200, 40, 40, 255); }   // critical dims pop red
inline ImU32 dimSelected()    { return IM_COL32(220, 120, 30, 255); }  // selected dim, snap markers, armed tools
inline ImU32 dimInk()         { return IM_COL32(40, 90, 180, 255); }   // default drawing-blue dimension ink
inline ImU32 toolPreview()    { return IM_COL32(220, 120, 30, 160); }  // rubber-band previews
inline ImU32 annDragLine()    { return IM_COL32(220, 120, 30, 200); }
inline ImU32 annDragDot()     { return IM_COL32(220, 120, 30, 220); }
// white pads behind text/glyphs for legibility over geometry
inline ImU32 labelPad()       { return IM_COL32(255, 255, 255, 220); }
inline ImU32 fillWhite()      { return IM_COL32(255, 255, 255, 235); } // balloon fill, titleblock fill
inline ImU32 textPad()        { return IM_COL32(255, 255, 255, 200); }
inline ImU32 weldLabelPad()   { return IM_COL32(255, 255, 255, 210); }
inline ImU32 weldLabelInk()   { return IM_COL32(70, 76, 88, 255); }
// view-error note
inline ImU32 errFill()        { return IM_COL32(250, 235, 235, 235); }
inline ImU32 errBorder()      { return IM_COL32(200, 120, 110, 255); }
inline ImU32 errInk()         { return IM_COL32(150, 60, 50, 255); }
// section / detail / GD&T tools
inline ImU32 hatch()          { return IM_COL32(70, 90, 130, 200); }   // 45-degree section hatching
inline ImU32 sectionMark()    { return IM_COL32(190, 60, 50, 235); }   // parent-view cutting lines / detail circles
inline ImU32 sectionPreview() { return IM_COL32(190, 60, 50, 220); }   // in-progress section/detail preview
inline ImU32 gdtSnap()        { return IM_COL32(30, 140, 80, 255); }
inline ImU32 gdtPreview()     { return IM_COL32(30, 140, 80, 180); }
inline ImU32 viewDragGhost()  { return IM_COL32(40, 120, 220, 230); }
inline ImU32 bomFill()        { return IM_COL32(252, 252, 250, 240); }
inline ImU32 hudText()        { return IM_COL32(235, 235, 240, 255); }
// status strip text
inline ImVec4 statusIdle()    { return ImVec4(0.55f, 0.58f, 0.63f, 1.0f); }
inline ImVec4 statusArmed()   { return ImVec4(0.86f, 0.55f, 0.20f, 1.0f); }
} // namespace draw2d

// ---- CAM backplot + sim verdicts (cam/cam_panel.cpp) -------------------------------------------
namespace camview {
inline ImU32 backdrop() { return IM_COL32(24, 26, 30, 255); }
inline ImU32 emptyMsg() { return IM_COL32(160, 160, 160, 255); }
inline ImU32 rapid()    { return IM_COL32(200, 60, 60, 120); }   // rapid = faint red
inline ImU32 lead()     { return IM_COL32(80, 200, 120, 200); }  // lead = green
inline ImU32 plunge()   { return IM_COL32(80, 180, 220, 220); }  // plunge = cyan
inline ImU32 cut()      { return IM_COL32(90, 150, 240, 255); }  // cut = blue
inline ImU32 arc()      { return IM_COL32(150, 130, 240, 255); } // arc = violet
inline ImU32 dimText()  { return IM_COL32(150, 160, 170, 255); } // extent readout
inline ImU32 simWarn()  { return IM_COL32(230, 120, 60, 255); }  // gouges / rapid-through-stock
inline ImU32 simOk()    { return IM_COL32(90, 200, 120, 255); }
} // namespace camview

// ---- Feature-tree category strip accents (feature_tree.cpp) ------------------------------------
namespace featcat {
inline ImU32 sketch()     { return IM_COL32(86, 152, 245, 180); }  // soft blue
inline ImU32 solidCut()   { return IM_COL32(245, 120, 72, 190); }  // orange-red
inline ImU32 solidAdd()   { return IM_COL32(45, 212, 191, 190); }  // teal accent
inline ImU32 dressUp()    { return IM_COL32(155, 120, 220, 190); } // purple
inline ImU32 pattern()    { return IM_COL32(74, 173, 204, 190); }  // cyan
inline ImU32 datum()      { return IM_COL32(100, 140, 210, 190); } // slate-blue
inline ImU32 sheetMetal() { return IM_COL32(242, 182, 64, 190); }  // amber
inline ImU32 weldment()   { return IM_COL32(220, 90, 80, 190); }   // coral-red
inline ImU32 surface()    { return IM_COL32(90, 200, 110, 190); }  // green
inline ImU32 booleanOp()  { return IM_COL32(175, 120, 205, 190); } // soft purple
inline ImU32 unknown()    { return IM_COL32(120, 130, 148, 140); } // grey
} // namespace featcat

// ---- Evaluate dialogs (evaluate_dialogs.cpp) ----------------------------------------------------
namespace evalview {
inline ImVec4 pending() { return ImVec4(0.6f, 0.6f, 0.6f, 1); }
inline ImVec4 errText() { return ImVec4(0.90f, 0.35f, 0.30f, 1); }
inline ImVec4 okText()  { return ImVec4(0.30f, 0.80f, 0.45f, 1); }
} // namespace evalview

// ---- PM header OK button (property_manager.cpp) -------------------------------------------------
namespace pmview {
inline ImVec4 okBtn()        { return ImVec4(0.08f, 0.38f, 0.24f, 0.9f); }
inline ImVec4 okBtnHovered() { return ImVec4(0.14f, 0.58f, 0.36f, 1.0f); }
} // namespace pmview

// ---- Misc panel one-offs -------------------------------------------------------------------------
namespace panelview {
inline ImVec4 accentButtonInk() { return ImVec4(0.04f, 0.10f, 0.10f, 1.f); } // settings accent-btn text
inline ImU32  sparkline()       { return IM_COL32(90, 170, 250, 255); }      // sim_manager demo sparkline
} // namespace panelview

} // namespace theme
