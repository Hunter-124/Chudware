// Native Win32 file dialogs (Save As / Open) — tiny UTF-8 wrapper over the common dialogs
// (GetSaveFileNameW / GetOpenFileNameW). Used by the FeatureManager's Save/Open buttons.
//
// Contract:
//   - `filter` uses the classic common-dialog form, pairs of (description, pattern) separated
//     by embedded NULs and terminated by a double NUL when written as a string literal, e.g.
//         "Chudware part (*.step+)\0*.step+\0All files (*.*)\0*.*\0"
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

// "Choose folder" dialog. Returns the chosen directory as UTF-8, or "" on cancel/error.
std::string openFolderDialog();
// Modal Yes/No/Cancel prompt asking whether to import a mesh file as a solid body (vs. keep it as a
// graphics mesh). Only meaningful for mesh formats. Returns 1 (yes/solid), 0 (no/mesh), -1 (cancel).
int promptImportMeshAsSolid();

// After the engine has written `path` (export/save), desktop no-ops; under
// __EMSCRIPTEN__ triggers a browser download via GET /workspace/download.
void afterFileWritten(const std::string& path);

// Reveal a file in the OS file manager, selecting the item where the platform supports
// it (Explorer selection, Finder `open -R`, freedesktop FileManager1 ShowItems); falls
// back to opening the containing folder. Opens directories themselves. No-op for
// empty/invalid paths and in the browser build (workspace paths are server-side).
void revealInFileManager(const std::string& path);

// Open a local file/directory with the OS default handler, never through a shell.
// Relative paths resolve against cwd, not as opener options or URL schemes. Returns
// false on invalid input or launch failure; unavailable in the browser build.
bool openExternalFile(const std::string& path);

// False in the browser build (no OS file manager); callers skip reveal menu items.
inline bool haveOsFileManager() {
#ifdef __EMSCRIPTEN__
    return false;
#else
    return true;
#endif
}

}  // namespace ui
