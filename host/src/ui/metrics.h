#pragma once
// -------------------------------------------------------------------------------------------------
// Chudware GUI metrics — the SINGLE place GUI dimensions are defined.
//
// Centralized DPI-scaled dimensions; gui_lint.py's scale rule recognizes px()/U().
// Accessors return logical sizes multiplied by ui::uiScale. Layout conventions:
// docs/dev/gui-theme-layout.md.
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
inline float copilotProseStreamCapH() { return px(420.0f); }  // generous grow cap for a LIVE
// streaming thinking/thought box: while text is arriving it grows to
// fit instead of scrolling inside copilotProseMaxH(), up to this height, THEN falls back to the
// normal fixed-height scroll. Finished/historical drawers keep using copilotProseMaxH().
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

// ---- Copilot CHAT timeline density (ui/copilot/activity_timeline.cpp + cards.cpp + chat_log.cpp)
// The per-turn activity timeline (thinking blocks, thought boxes, tool rows, action cards, AND
// every corp/firm/swarm/classification/delegation card cards.cpp draws for drawAgentTimeline) is
// a conversation, not a menu: cards land in dense runs while a turn streams, so it sizes TIGHTER
// than the cpMenu* rhythm or a single run overflows the viewport. The four menu views above keep
// cpMenu*; EVERY row kind drawAgentTimeline dispatches — regardless of file, card kind, or
// collapsed/expanded state — MUST size from cpChatRowGap/cpChatCardPadX/cpChatCardPadY, full
// stop. Before this rule cards.cpp's corp/firm/swarm family still used the heavier cpMenuRowGap/
// cpMenuRowPadX/Y, so the sibling stack alternated between a 2px/9px-pad rhythm and a 5px/10px-pad
// rhythm row to row — the "trash", uneven spacing a live session flagged.
inline float cpChatRowGap()   { return px(2.0f); }  // between sibling activity rows/cards
inline float cpChatCardPadX() { return px(9.0f); }  // inside an activity card, horizontal
inline float cpChatCardPadY() { return px(4.0f); }  // inside an activity card, vertical
inline float cpChatTurnGap()  { return px(6.0f); }  // between chat TURNS (user/assistant)
// Conversation-level rhythm (copilot chat restyle): the user's bubble and the air between
// messages. Activity rows/cards inside a turn keep the compact cpChatRow/Card rhythm above.
inline float cpChatMsgGap()       { return px(14.0f); }  // between chat messages (turns)
inline float cpChatBubblePadX()   { return px(12.0f); }  // inside the user's message bubble
inline float cpChatBubblePadY()   { return px(7.0f); }
inline float cpChatBubbleRadius() { return 14.0f; }      // LOGICAL dp; rounding() scales it
inline float cpChatMetaGap()      { return px(3.0f); }   // bubble -> its meta row (age, actions)

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

// ---- Parts Library workspace (ui/parts/) ------------------------------------------------------
// ONE horizontal inset for the whole Parts workspace, used in two STACKED roles:
//   * pane gutter   — the child WindowPadding.x of every parts pane, so the categories / results
//                     / filters columns all start their SURFACES (rows, cards, frames) on one line;
//   * content inset — how far a row, a grid card or a results-table cell insets its own text from
//                     that surface, so every LABEL lands on one text line at gutter + inset.
// Deliberately equal to the theme FramePadding.x (ui_theme.cpp), which is where ImGui puts the
// text inside an InputText / Combo / Button — native framed widgets then align for free.
// Before this token the three panes used 12 / 10 / 12+15 px (a nested child silently re-applied
// the default WindowPadding) and a grid card mixed an 8-px pad, a 6-px text margin and a centred
// title, so nothing in the workspace shared a left edge.
inline float partsPadX()      { return px(12.0f); }

// ---- CAM / Manufacture workspace (ui/cam/) ----------------------------------------------------
// The Manufacture tab was the last workspace still sizing itself from raw pixel literals, so its
// integrated Job Manager / NC Output surfaces mis-scaled at HiDPI and shared no visual rhythm with
// the rest of the shell. The tokens below deliberately mirror the copilot-menu and reveng tokens
// (4-px grid, 8-dp card corner) while geometric previews stay in the native viewport. Every CAM
// panel MUST size itself from here — no fresh literals.
inline float camGutter()      { return px(10.0f); }    // pane content inset (matches partsPadX rhythm)
inline float camRowPadX()     { return px(10.0f); }    // inside a row/card, horizontal
inline float camRowPadY()     { return px(6.0f); }     // inside a row/card, vertical
inline float camRowGap()      { return px(4.0f); }     // between sibling rows
inline float camRowRound()    { return rounding(6.0f); }
inline float camChipRound()   { return rounding(5.0f); }
inline float camSectionGap()  { return px(12.0f); }    // above a section header
inline float camHeadGap()     { return px(5.0f); }     // below a section header rule
inline float camFieldW()      { return px(120.0f); }   // numeric / combo field width
inline float camBtnH()        { return px(28.0f); }    // toolbar / action button height
inline float camBtnGap()      { return px(6.0f); }     // between toolbar buttons
inline float camTreeIndent()  { return px(12.0f); }    // per-level tree indent
inline float camCaptionGap()  { return px(3.0f); }     // row -> its sub-caption ("extracted stock ...")

// ---- Simulate workspace left column (layout.cpp drawLeftPane) -----------------------------------
// Default WINDOW width of the Simulation Manager column in the Simulate workspace. Sim forms
// (label column + value + unit suffix, face wells, result cards) wrapped/clipped at the shared
// 320dp FeatureManager default (~280 px of content after the 8dp window + 12dp body insets); 380dp
// yields ~340 px of content, inside the standard's <= ~360 px side-pane budget. layout.cpp clamps
// it to a fraction of the overlay band so the viewport stays usable on narrow windows; user
// resizes persist per workspace and "Reset layout" restores this value.
inline float simManagerPaneW() { return px(380.0f); }
}  // namespace metrics
}  // namespace ui
