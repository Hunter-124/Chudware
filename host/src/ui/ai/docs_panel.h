// AI workspace — Documents panel.
//
// Per-session document attach / preview / edit surface for the AI tab (wired into the
// utility panel at nav view 3). LEFT: the active conversation's agent-created artifacts (parsed from
// the same "\x1eART\x1e" activity blobs conversations.cpp persists as {kind,path,tool}) plus
// paths the user attached this session (native open dialog, or the manual path entry for
// headless runs). RIGHT: an extension-keyed preview — text/source via drawCodeBox, CSV as a
// table, PNG/JPG as a GL texture, office/PDF through the worker's office_extract RPC (the
// pane degrades to an honest note when the method is absent). Plain-text/CSV/MD files are
// editable in place (multiline buffer + Save, atomic tmp+rename write-back).
#pragma once

struct AppState;

namespace ui {

// The Documents tab body (drawn inside the DAG pane child; scrolls automatically).
void drawDocsPanel(AppState& st);

}  // namespace ui
