#pragma once
// -------------------------------------------------------------------------------------------------
// AI-CAD GUI metrics — the SINGLE place GUI dimensions are defined.
//
// Rationale (docs/GUI_STYLE_GUIDE.md): sizing was scattered as raw pixel literals across ~25 files,
// which drifted and mis-scaled at HiDPI. This header centralizes the dimensions so they can be
// changed / recalculated in ONE spot, and so the gui_lint.py "scale" rule recognizes px()/U() as
// proper scaling. All accessors return DPI-scaled pixels (logical × ui::uiScale).
//
// Usage:
//   ImGui::SetNextItemWidth(ui::metrics::pmLabelCol());
//   ImGui::SameLine(0, ui::metrics::U(1));            // a 4px grid gap
//   dl->AddLine(a, b, col, ui::metrics::stroke(2));   // hairline-safe, never < 1px
//
// Fonts are pre-scaled and ImGuiStyle goes through ScaleAllSizes once, so values derived from
// CalcTextSize / GetFrameHeight / GetStyle() are ALREADY scaled — do NOT wrap those in px().
// -------------------------------------------------------------------------------------------------
#include "../ui_theme.h"   // ui::uiScale

namespace ui {
namespace metrics {

// ---- primitives -------------------------------------------------------------------------------
inline float scale()              { return ui::uiScale; }
inline float px(float logical)    { return logical * ui::uiScale; }       // scale a logical-px value
inline float U(float n = 1.0f)    { return n * 4.0f * ui::uiScale; }      // 4-px base grid unit
inline float stroke(float logicalThickness) {                            // hairline-safe (>= 1px)
    const float t = logicalThickness * ui::uiScale;
    return t < 1.0f ? 1.0f : t;
}
inline float rounding(float logicalRadius) {                             // corner radius (>= 1px)
    const float r = logicalRadius * ui::uiScale;
    return r < 1.0f ? 1.0f : r;
}

// ---- icon display tiers (logical px) ----------------------------------------------------------
inline float iconHeader()  { return px(20.0f); }   // PM / panel header icon

// ---- PropertyManager forms --------------------------------------------------------------------
inline float pmLabelCol()  { return px(116.0f); }  // fixed label column width (two-column rows)
inline float pmOkBtn()     { return px(28.0f); }   // OK / Cancel button size

// ---- bars -------------------------------------------------------------------------------------
inline float titleFramePadX() { return px(14.0f); }
inline float titleFramePadY() { return px(12.0f); }

// ---- pinned Copilot column default/min width (PIXELS; consumed by legacy_chat drawAIPanel) -----
inline float dockRightTarget() { return px(340.0f); }
inline float dockRightMin()    { return px(200.0f); }

// ---- overlay pills / chips (viewport HUD, sketch overlays, legend, view_selector tooltips) ------
// Recurring pattern: a rounded translucent rect with a text label. See docs/GUI_SIZING_FIXPLAN.md.
inline float chipRound()   { return rounding(6.0f); }   // standard overlay pill corner

// ---- assembly panel forms (asm_panels.cpp) ----------------------------------------------------
inline float asmBtnW()         { return px(90.0f); }    // Add/Close/Insert/Collapse buttons
inline float asmInputW()       { return px(120.0f); }   // mate / pattern numeric fields
inline float asmHeaderIconGap(){ return px(6.0f); }     // SameLine gap after a panel header icon

// ---- ribbon chrome (widgets/ribbon.cpp) -------------------------------------------------------
inline float ribbonBtnRound()    { return rounding(3.0f); }  // button + tab fill corner
inline float ribbonSplitInset()  { return px(4.0f); }        // big-button split divider inset
inline float ribbonSmSplitInset(){ return px(2.0f); }        // small-button + tab underline inset

// ---- AI Copilot pane (legacy_chat.cpp) --------------------------------------------------------
inline float copilotBtnH()        { return px(32.0f); }   // primary send / build button height
inline int   copilotInputMaxLines(){ return 6; }          // prompt box grows to N lines, then scrolls
inline float copilotThumbW()      { return px(220.0f); }  // iteration contact-sheet thumbnail width
inline float copilotProseMaxH()   { return px(150.0f); }  // thoughts / prose scroll cap
inline float copilotDiffMaxH()    { return px(220.0f); }  // diff / code view scroll cap

}  // namespace metrics
}  // namespace ui
