// Rules authoring popup — the little guided-chat element in Settings -> AI.
//
// One compact nested modal that walks the user through creating ONE of:
//   * a Skill          -> `skills_save`   -> ~/.local/share/Chudware/skills/<name>/SKILL.md
//   * a Context pack   -> `packs_save`    -> <user context dir>/packs/<name>.md
//   * a Rule           -> `triggers_save` -> <user context dir>/triggers.json
//                         (+ its pack body in the same call, via `pack_body`)
//
// It is a GUIDED FORM wearing a chat presentation: the panel asks one question at a time in
// assistant bubbles, the user answers in a single composer row, and the answers are echoed as
// user bubbles. There is no model call anywhere in here — nothing is ever invented on the
// user's behalf, and the exact bytes that will be written are shown in a preview bubble before
// the Create button is enabled. The only persistence path is the existing worker RPCs above.
//
// A Rule with no trigger can never fire, so the trigger step is mandatory: the user either
// supplies one, or takes the explicit "no trigger - save disabled" exit, which sets
// `enabled: false` and says so in the preview.
#pragma once

struct AppState;

namespace ui {

// Draw the authoring popup. Call this UNCONDITIONALLY every frame from inside the Settings
// window's draw scope (it must be a child of that window so ImGui's modal stack keeps it
// interactive while Settings is up). Set `*open = true` to raise it; it clears `*open` itself
// when dismissed or after a successful create.
void drawRulesAuthoringPopup(AppState& st, bool* open);

}  // namespace ui
