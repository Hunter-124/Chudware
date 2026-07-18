// Browser file picker + workspace browser for the Emscripten build.
//
// Opens a hidden <input type=file>, blocks (PROXY_TO_PTHREAD worker) until the
// user picks or cancels, and returns the file bytes + name. Desktop stub always
// cancels so call sites can share the header.
//
// Also provides a blocking workspace tree browser (list via /workspace/list) so
// Open / Save As / Open Folder can pick server-side paths under the workspace.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace billet {

struct PickedFile {
    std::string           name;
    std::vector<uint8_t>  data;
};

// accept: HTML accept attribute (e.g. ".step+,.step,.stl" or ""); empty = any.
// Returns true and fills out on success; false on cancel/error (err set on error).
bool pickFile(const char* accept, PickedFile& out, std::string& err);

// Minimal text prompt for a save filename (suggested prefilled). Returns "" on cancel.
std::string promptSaveFilename(const char* suggested);

// Workspace browser modes for browseWorkspace().
enum class WorkspaceBrowseMode {
    OpenFile = 0,    // pick an existing file under the workspace
    OpenFolder = 1,  // pick a workspace directory
    SaveAs = 2,      // pick a folder + type a filename
};

// Blocking workspace tree browser (web only). Returns a server-absolute path
// (gateway list's root + relative entry) or "" on cancel. `accept` is a comma
// list of extensions (".step+,.step") used to filter files in OpenFile mode;
// empty = show all files. `suggestedName` prefills the SaveAs name box.
// Desktop stub always returns "".
std::string browseWorkspace(WorkspaceBrowseMode mode,
                            const char* accept,
                            const char* suggestedName);

}  // namespace billet
