// Browser file picker for the Emscripten build.
//
// Opens a hidden <input type=file>, blocks (PROXY_TO_PTHREAD worker) until the
// user picks or cancels, and returns the file bytes + name. Desktop stub always
// cancels so call sites can share the header.
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

}  // namespace billet
