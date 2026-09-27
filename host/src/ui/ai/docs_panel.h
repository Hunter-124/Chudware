// AI workspace — Documents panel.
//
// The left list collects artifact files from every conversation in the active project,
// agent-touched saved tabs beneath the project directory, never-saved agent tabs with
// matching originating project, and attachments of the selected conversation in that
// project. The right pane previews only the selected visible entry. Text/source, CSV,
// images, and office/PDF use their existing preview/edit paths.
#pragma once

struct AppState;

namespace ui {

// The Documents tab body (drawn inside the DAG pane child; scrolls automatically).
void drawDocsPanel(AppState& st);

}  // namespace ui
