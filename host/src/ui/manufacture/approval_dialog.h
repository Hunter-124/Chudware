#pragma once

struct AppState;

namespace ui {

// Human-only approval-authoring surface. It proposes a binding and asks the
// gateway to create approval; it never validates or approves locally.
void drawManufactureApprovalDialog(AppState& st);

}  // namespace ui
