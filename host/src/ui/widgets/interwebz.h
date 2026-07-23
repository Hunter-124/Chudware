// Interwebz-style chrome widgets for Chudware — clean-room Liquid Glass port.
// Visual structure + interaction patterns only (no upstream palette or code copy).
// All geometry is uiScale-aware; colors come exclusively from ui::palette / glass helpers.
// Draw-list based; does not mutate global ImGui style (scoped PushStyle* only).
#pragma once

#include "imgui.h"

namespace ui {

// ---- Horizontal pill tab ----------------------------------------------------
// One tab chip. `selected` is the current visual state; returns true on click.
// size.x <= 0 → label-fit (+ horizontal pad). size.y <= 0 → default 28dp height.
bool InterwebzTabPill(const char* label, bool selected, ImVec2 size = ImVec2(0, 0));

// ---- Vertical category rail -------------------------------------------------
// Single rail row: optional atlas icon + label, left accent indicator when selected.
// height <= 0 → default 32dp. Returns true on click. Hit target is the full row.
bool InterwebzVTabRailItem(const char* label, const char* icon, bool selected,
                           float height = 0.0f);

// Full vertical rail. `labels[count]` required; `icons` may be null or per-entry null.
// `*selected` is the active index (clamped). Returns true when the selection changes.
// width <= 0 → default 132dp.
bool InterwebzVTabRail(const char* str_id, const char* const* labels,
                       const char* const* icons, int count, int* selected,
                       float width = 0.0f);

// ---- Group / child panel (MenuChild analogue) -------------------------------
// Bordered glass child with a titled header + accent underline. size.x/y <= 0 use
// remaining content region on that axis. Pair with InterwebzGroupEnd().
bool InterwebzGroupBegin(const char* id, const char* title, ImVec2 size = ImVec2(0, 0));
void InterwebzGroupEnd();

// Two equal columns with a gap. Begin → left column content → Next → right → End.
// When parent width < 520dp the columns stack vertically (right under left).
void InterwebzTwoColumnBegin(float gap_dp = 12.0f);
void InterwebzTwoColumnNext();
void InterwebzTwoColumnEnd();

// ---- Form controls ----------------------------------------------------------
// Checkbox with rounded square + accent fill when on. Hit target includes label.
bool InterwebzCheckbox(const char* label, bool* v);

// Slider with accent→accentDim fill left of grab, frameBg residual, value on grab.
bool InterwebzSliderFloat(const char* label, float* v, float v_min, float v_max,
                          const char* format = "%.3f");
bool InterwebzSliderInt(const char* label, int* v, int v_min, int v_max,
                        const char* format = "%d");

// Closed-field combo with triangle caret. Opens a themed popup of Selectables.
// Returns true when `*current` changes. items[items_count] must outlive the call.
bool InterwebzCombo(const char* label, int* current, const char* const items[],
                    int items_count);

// Begin/End form for custom item lists (Selectable inside). preview is the closed text.
bool InterwebzBeginCombo(const char* label, const char* preview_value);
void InterwebzEndCombo();

// Label | value row. Value is dim; label is primary. Optional right-side custom width.
void InterwebzKeyValueRow(const char* key, const char* value);

// ---- Demo -------------------------------------------------------------------
// Self-contained preview (static open flag). Integrator wires:
//   ui::InterwebzDemoToggle() on menu click; ui::InterwebzDemoDraw() each frame.
// InterwebzDemoWindow(bool*) also works if the caller owns the open flag.
void InterwebzDemoToggle();
void InterwebzDemoDraw();
void InterwebzDemoWindow(bool* open);

}  // namespace ui
