// Browser file picker + workspace browser for the Emscripten build.
//
// Opens a hidden <input type=file>, yields via ASYNCIFY (emscripten_sleep) until
// the user picks or cancels, and returns the file bytes + name. Desktop stub
// always cancels so call sites can share the header.
//
// The workspace Open dialog's "Upload from my machine…" path fills the pick
// buffer from JS (gesture-safe) then signals browse done with "__UPLOAD__";
// callers consume via consumePickedFile() — never re-open a second picker.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace chudware {

struct PickedFile {
    std::string           name;
    std::vector<uint8_t>  data;
};

// accept: HTML accept attribute (e.g. ".step+,.step,.stl" or ""); empty = any.
// Returns true and fills out on success; false on cancel/error (err set on error).
// Prefer the workspace-browser upload path + consumePickedFile() for Open dialogs.
bool pickFile(const char* accept, PickedFile& out, std::string& err);

// Non-blocking: take bytes already delivered by JS into the pick buffer
// (chudware_file_pick_done). Used after browseWorkspace returns "__UPLOAD__".
// Returns true and fills out on success; false if nothing pending / cancel / error.
bool consumePickedFile(PickedFile& out, std::string& err);

// Minimal text prompt for a save filename (suggested prefilled). Returns "" on cancel.
std::string promptSaveFilename(const char* suggested);

// Workspace browser modes for browseWorkspace().
enum class WorkspaceBrowseMode {
    OpenFile = 0,    // pick an existing file under the workspace
    OpenFolder = 1,  // pick a workspace directory
    SaveAs = 2,      // pick a folder + type a filename
};

// Blocking workspace tree browser (web only). Returns a workspace-relative path
// ("." for root) or "" on cancel. `accept` is a comma
// list of extensions (".step+,.step") used to filter files in OpenFile mode;
// empty = show all files. `suggestedName` prefills the SaveAs name box.
// Desktop stub always returns "".
std::string browseWorkspace(WorkspaceBrowseMode mode,
                            const char* accept,
                            const char* suggestedName);

}  // namespace chudware
