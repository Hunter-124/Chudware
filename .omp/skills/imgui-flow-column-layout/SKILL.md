---
name: imgui-flow-column-layout
description: Design ImGui flow-column layouts that handle cursor wrapping, width allocation, and narrow windows.
---

# ImGui flow-column layout

For multi-column layouts in `host/src/ui/`, inspect the current helpers in `settings_modal.cpp` rather than relying on historical line numbers.

- After an item, `GetCursorPosX()` can be the next line's start, not the item's right edge. Capture the row origin and measure widths, or use item rectangles and `SameLine` appropriately.
- `SetCursorPos` places the next item only. A section helper that starts with a spacer can consume that placement; use a zero-padding wrapper child to position the entire second section, then advance past the taller column.
- Exact floating-point fit tests can fail at the equality boundary after pixel rounding. For fixed-size cells, compute a column count and use index-based flow; make each cell occupy its intended width before placing the next.
- Allocate width to the primary label before secondary metadata and ellipsize to fit. Account for UI scale, narrow windows, and the actual section widths rather than assuming a fixed screen size.

If capture is requested, use a separately launched headless GUI with private HOME, XDG directories, workspace, isolated test credentials, and engine. Do not poll or alter the user's active sign-in state. Check both wide and narrow layouts and the appropriate settings-tab mapping in the current source.

Verify: `scripts/testbox.py --suite gui`
