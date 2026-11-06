---
name: imgui-drawlist-widget-traps
description: Diagnose clipping, input reversion, disabled painting, and overlap in custom ImGui widgets.
---

# ImDrawList widget traps

For hand-painted controls in `host/src/ui/`, inspect both layout and input handling:

- Centered labels can clip when a left-aligned inset is used as the right clip boundary. Size the widget from its text where possible, or choose the clip boundary for its alignment. Share width calculations between layout and painting; see `ui::parts::chipToggle` and its width helper.
- Decorated prefix formats in `InputFloat`/`InputScalar` can render but fail to parse on edit. Draw a prefix as separate chrome and keep the numeric input format parseable; verify against the bundled ImGui version.
- `BeginDisabled` affects ImGui widget rendering but custom draw-list colors need explicit disabled styling. Include an accessible explanation for unavailable actions where appropriate.
- Reserve space for hover action bands instead of painting over card content. Keep a band alive while its popup is open, and avoid clipping the popup to the card.
- An overlay submitted after an active text input can lose the mouse-down to the input. Inspect item submission order and active-ID overlap behavior when a clear button needs two clicks.

If requested, use a separately launched headless GUI with private HOME, XDG directories, workspace and isolated engine. Compare captures with the pointer parked away and over the target; do not touch the user's open GUI.

Verify: `scripts/testbox.py --suite render,gui`
