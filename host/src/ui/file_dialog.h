// Native Win32 file dialogs (Save As / Open) — tiny UTF-8 wrapper over the common dialogs
// (GetSaveFileNameW / GetOpenFileNameW). Used by the FeatureManager's Save/Open buttons.
//
// Contract:
//   - `filter` uses the classic common-dialog form, pairs of (description, pattern) separated
//     by embedded NULs and terminated by a double NUL when written as a string literal, e.g.
//         "AI-CAD part (*.step+)\0*.step+\0All files (*.*)\0*.*\0"
//     (a C literal supplies the final extra NUL itself).
//   - `defExt` is appended (with a dot) when the user types a name with no extension. We do
//     this ourselves instead of via lpstrDefExt because the OFN field only honors the first
//     three characters — useless for "step+".
//   - Returns the chosen absolute path as UTF-8, or "" on cancel/error.
//   - The last used directory is remembered (static) so the next dialog opens there; the
//     first dialog of a session opens in the user's Documents folder.
//   - Owner window: GetActiveWindow() (the GLFW HWND isn't reachable from here; the active
//     window at click time is ours).
#pragma once

#include <string>

namespace ui {

// "Save As" dialog. suggestedName prefills the file-name box (may be nullptr/empty).
std::string saveFileDialog(const char* filter, const char* defExt, const char* suggestedName);

// "Open" dialog. The chosen file must exist.
std::string openFileDialog(const char* filter);

// Modal Yes/No/Cancel prompt asking whether to import a mesh file as a solid body (vs. keep it as a
// graphics mesh). Only meaningful for mesh formats. Returns 1 (yes/solid), 0 (no/mesh), -1 (cancel).
int promptImportMeshAsSolid();

}  // namespace ui
