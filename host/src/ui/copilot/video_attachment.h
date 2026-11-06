#pragma once

#include <algorithm>
#include <cctype>
#include <cstring>
#include <fstream>
#include <string>

namespace ui::copilot {

// Container signatures are intentionally limited to video container brands. In particular,
// do not mistake HEIF/AVIF still images, which also have an ftyp box, for videos.
inline bool isVideoAttachment(const std::string& path) {
    const auto dot = path.find_last_of('.');
    const auto slash = path.find_last_of("/\\");
    if (dot != std::string::npos && (slash == std::string::npos || dot > slash)) {
        std::string ext = path.substr(dot + 1);
        std::transform(ext.begin(), ext.end(), ext.begin(),
                       [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        if (ext == "mp4" || ext == "mov" || ext == "webm" || ext == "mkv" ||
            ext == "avi" || ext == "m4v") return true;
    }
    char bytes[16] = {};
    std::ifstream file(path, std::ios::binary);
    if (!file.read(bytes, sizeof(bytes))) return false;
    if (std::memcmp(bytes, "\x1a\x45\xdf\xa3", 4) == 0) return true; // Matroska/WebM
    if (std::memcmp(bytes, "RIFF", 4) == 0 && std::memcmp(bytes + 8, "AVI ", 4) == 0)
        return true;
    if (std::memcmp(bytes + 4, "ftyp", 4) != 0) return false;
    static const char* const brands[] = {"isom", "iso2", "iso3", "iso4", "iso5", "iso6",
                                         "mp41", "mp42", "avc1", "M4V ", "qt  ", "MSNV"};
    for (const char* brand : brands)
        if (std::memcmp(bytes + 8, brand, 4) == 0) return true;
    return false;
}

}  // namespace ui::copilot
