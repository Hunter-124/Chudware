// Re-root an assembly component reference whose stored path no longer exists.
//
// An assembly saved on one machine can carry an absolute reference from that machine
// ("/home/me/proj/parts/a.step+", "C:\\work\\proj\\parts\\a.step+"). When the project
// folder is copied elsewhere that path is dead, but its tail still names the file
// relative to some folder of the assembly's own ancestry. Try the assembly's directory
// and then each parent (nearest first) against the stored path's tails (longest first)
// and return the first existing regular file. A bare file name is only tried in the
// assembly's own directory, so an unrelated same-named file higher up never wins.
//
// Header-only: the kernel (open/load) and the GUI explorer thumbnails share it.
#pragma once

#include <filesystem>
#include <string>
#include <system_error>
#include <vector>

namespace chudware::util {

inline std::filesystem::path relocateComponentRef(const std::string& stored,
                                                  const std::filesystem::path& baseDir) {
    namespace fs = std::filesystem;
    if (stored.empty() || baseDir.empty()) return {};
    // Split on both separators so either OS's spelling decomposes the same way.
    std::vector<std::string> parts;
    std::string cur;
    for (char c : stored) {
        if (c == '/' || c == '\\') {
            if (!cur.empty()) parts.push_back(std::move(cur));
            cur.clear();
        } else {
            cur.push_back(c);
        }
    }
    if (!cur.empty()) parts.push_back(std::move(cur));
    if (!parts.empty() && parts.front().size() == 2 && parts.front()[1] == ':')
        parts.erase(parts.begin());   // drive letter
    for (const auto& p : parts)
        if (p == "..") return {};     // a relative reference is resolved by its own rules
    if (parts.empty()) return {};

    constexpr int kMaxAncestors = 8;
    fs::path dir = baseDir.lexically_normal();
    for (int level = 0; level < kMaxAncestors && !dir.empty(); ++level) {
        const size_t shortest = level == 0 ? 1 : 2;
        for (size_t n = parts.size(); n >= shortest; --n) {
            fs::path candidate = dir;
            for (size_t i = parts.size() - n; i < parts.size(); ++i)
                candidate /= fs::u8path(parts[i]);
            std::error_code ec;
            if (fs::is_regular_file(candidate, ec)) return candidate;
        }
        const fs::path parent = dir.parent_path();
        if (parent == dir) break;
        dir = parent;
    }
    return {};
}

}  // namespace chudware::util
