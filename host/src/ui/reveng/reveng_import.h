// File-open classification shared by the dialog, explorer and OS-drop path.
#pragma once

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <istream>
#include <sstream>
#include <string>

namespace ui::reveng {

enum class SourceFile { Other, Video, Splat
#ifdef __EMSCRIPTEN__
    , Ply
#endif
};

#ifdef __EMSCRIPTEN__
// A classified non-Gaussian PLY returns to the normal mesh-open path once.
inline bool& bypassPlyClassification() {
    static bool bypass = false;
    return bypass;
}
#endif

// Read only a bounded PLY header. A face element belongs to the existing mesh
// importer even if unrelated properties happen to have Gaussian-like names.
inline bool gaussianPlyHeader(std::istream& in) {
    char line[4097];
    size_t bytes = 0;
    bool first = true, vertex = false, faces = false;
    bool opacity = false, scale = false, rotation = false, color = false;
    while (bytes < 65536 && in.getline(line, sizeof(line))) {
        const size_t length = std::char_traits<char>::length(line);
        bytes += length + 1;
        if (bytes > 65536) return false;
        std::istringstream tokens(line);
        std::string key, value, name;
        tokens >> key;
        if (first) {
            first = false;
            if (key != "ply") return false;
        } else if (key == "element") {
            tokens >> name;
            vertex = name == "vertex";
            if (name == "face") faces = true;
        } else if (key == "property" && vertex) {
            tokens >> value >> name;
            opacity |= name == "opacity";
            scale |= name == "scale_0";
            rotation |= name == "rot_0";
            color |= name == "f_dc_0";
        } else if (key == "end_header") {
            return !faces && opacity && scale && rotation && color;
        }
    }
    return false;
}

inline SourceFile classifySourceFile(const std::string& path) {
    std::string ext = std::filesystem::path(path).extension().string();
    std::transform(ext.begin(), ext.end(), ext.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    if (ext == ".mp4" || ext == ".mov" || ext == ".webm" || ext == ".mkv" || ext == ".avi")
        return SourceFile::Video;
    if (ext == ".splat" || ext == ".spz" || ext == ".ksplat") return SourceFile::Splat;
    if (ext == ".ply") {
#ifdef __EMSCRIPTEN__
        if (!bypassPlyClassification()) return SourceFile::Ply;
#else
        std::ifstream file(std::filesystem::u8path(path), std::ios::binary);
        if (file && gaussianPlyHeader(file)) return SourceFile::Splat;
#endif
    }
    return SourceFile::Other;
}

}  // namespace ui::reveng
