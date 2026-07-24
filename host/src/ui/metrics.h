#pragma once
// -------------------------------------------------------------------------------------------------
// Chudware GUI metrics — the SINGLE place GUI dimensions are defined.
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

// ---- pinned Copilot column default/min width (PIXELS; consumed by copilot/panel.cpp drawAIPanel) -----
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

// ---- AI Copilot pane (ui/copilot/, split from legacy_chat.cpp) -------------------------------
inline float copilotBtnH()        { return px(32.0f); }   // primary send / build button height
inline int   copilotInputMaxLines(){ return 6; }          // prompt box grows to N lines, then scrolls
inline float copilotThumbW()      { return px(220.0f); }  // iteration contact-sheet thumbnail width
inline float copilotProseMaxH()   { return px(150.0f); }  // thoughts / prose scroll cap
inline float copilotDiffMaxH()    { return px(220.0f); }  // diff / code view scroll cap

// ---- Copilot MENU views (Sessions / Tasks / Artifacts / Quick settings) ----------------------
// The four header views share one scroll region inside the pinned column, so they must also
// share their rhythm: same gutter, same row corner, same gap between rows, same gap between
// sections. Before these tokens each view invented its own (10/8 vs 8/6 padding, 8 vs 6 dp
// corners, four different Dummy() spacers), which is exactly what made the menus read as four
// unrelated screens. Every copilot menu MUST size itself from here — no fresh literals.
inline float cpMenuGutter()   { return px(10.0f); }  // left/right inset of menu content
inline float cpMenuRowPadX()  { return px(10.0f); }  // inside a menu row/card, horizontal
inline float cpMenuRowPadY()  { return px(8.0f); }   // inside a menu row/card, vertical
inline float cpMenuRowGap()   { return px(5.0f); }   // between sibling rows/cards
inline float cpMenuRowRound() { return rounding(8.0f); }   // menu row/card corner
inline float cpMenuChipRound(){ return rounding(6.0f); }   // chips/pills inside menus
inline float cpMenuSectionGap(){ return px(14.0f); }  // above a section header
inline float cpMenuHeadGap()  { return px(6.0f); }    // below a section header rule
inline float cpMenuLabelGap() { return px(3.0f); }    // label -> its control
inline float cpMenuCtlGap()   { return px(7.0f); }    // control -> next label (vertical rhythm)

// ---- Reverse Engineering workspace (ui/reveng/, RE-40 overhaul) -------------------------------
// The RE tab is a wizard: a session rail, a stage stepper, one stage body, a properties column.
// Before these tokens each of the four stage TUs invented its own paddings and card corners, so
// the six stages read as six unrelated screens. Every reveng panel MUST size itself from here.
// The rhythm deliberately mirrors the copilot menu tokens above (4-px grid, 8-dp card corner)
// so the RE tab feels like the same product as the rest of the shell.
inline float reRailW()        { return px(288.0f); }  // session rail column width
inline float rePropW()        { return px(312.0f); }  // right properties/capability column
inline float reGutter()       { return px(10.0f); }   // between the three top-level columns
inline float reStepperH()     { return px(64.0f); }   // top stage stepper band height
inline float reCardPadX()     { return px(12.0f); }   // inside a card, horizontal
inline float reCardPadY()     { return px(10.0f); }   // inside a card, vertical
inline float reCardGap()      { return px(8.0f); }    // between sibling cards
inline float reCardRound()    { return rounding(8.0f); }
inline float reChipRound()    { return rounding(6.0f); }
inline float reSectionGap()   { return px(16.0f); }   // above a section header
inline float reHeadGap()      { return px(6.0f); }    // below a section header rule
inline float reLabelGap()     { return px(3.0f); }    // label -> its control
inline float reCtlGap()       { return px(8.0f); }    // control -> next label
inline float reFieldW()       { return px(150.0f); }  // numeric / combo field width
inline float reBtnH()         { return px(30.0f); }   // primary action button height
inline float reBtnW()         { return px(112.0f); }  // standard action button width
// Thumbnails: gallery contact sheet, session-rail card preview, fit/result preview tiles.
inline float reThumbSm()      { return px(48.0f); }   // rail card / inline row preview
inline float reThumbMd()      { return px(104.0f); }  // gallery contact-sheet tile
inline float reThumbLg()      { return px(196.0f); }  // stage hero preview (scale mat, result)
inline float reThumbGap()     { return px(6.0f); }    // between contact-sheet tiles
// Live webcam preview: 16:9 hero with a minimum so the coverage dome stays legible.
inline float rePreviewMinW()  { return px(360.0f); }
inline float rePreviewMaxW()  { return px(720.0f); }
inline float reDomeR()        { return px(56.0f); }   // coverage dome ring radius
inline float reProgressH()    { return px(8.0f); }    // stage progress bar thickness
}  // namespace metrics
}  // namespace ui
