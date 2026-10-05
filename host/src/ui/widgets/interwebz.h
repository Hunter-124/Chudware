// Interwebz-style chrome widgets for Chudware — clean-room Liquid Glass port.
// Visual structure + interaction patterns only (no upstream palette or code copy).
// All geometry is uiScale-aware; colors come exclusively from ui::palette / glass helpers.
// Draw-list based; does not mutate global ImGui style (scoped PushStyle* only).
#pragma once

#include "imgui.h"

namespace ui {

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

// Label | value row. Value is dim; label is primary.
void InterwebzKeyValueRow(const char* key, const char* value);

}  // namespace ui
