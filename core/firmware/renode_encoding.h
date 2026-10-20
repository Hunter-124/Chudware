#pragma once

#include <filesystem>
#include <stdexcept>
#include <string>

namespace chudware::firmware {

// Renode monitor quoted strings are not JSON: unsupported delimiters must be
// refused, not escaped using an unrelated language's rules.
inline std::string renode_string(const std::string& value) {
    for (unsigned char c : value)
        if (c < 0x20 || c == 0x7f || c == '"' || c == '\\')
            throw std::invalid_argument("unsupported character in Renode monitor string");
    return "\"" + value + "\"";
}

inline std::string renode_path(const std::filesystem::path& value) {
    // Renode 1.16 monitor file tokens use @/absolute/path, not @"...".
    // Inputs are copied under controlled names; refuse unsafe storage roots
    // instead of treating shell/JSON quoting as monitor-language escaping.
    const auto path = value.generic_string();
    if (!value.is_absolute())
        throw std::invalid_argument("Renode monitor path must be absolute");
    for (std::size_t i = 0; i < path.size(); ++i) {
        const unsigned char c = path[i];
        const bool letter = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z');
        const bool drive_colon = c == ':' && i == 1 &&
            ((path[0] >= 'A' && path[0] <= 'Z') || (path[0] >= 'a' && path[0] <= 'z'));
        if (!letter && !(c >= '0' && c <= '9') && c != '/' &&
            c != '_' && c != '-' && c != '.' && !drive_colon)
            throw std::invalid_argument("unsupported character in Renode monitor path");
    }
    return "@" + path;
}

inline bool renode_identifier(const std::string& value) {
    if (value.empty() || value.size() > 64) return false;
    const auto alpha = [](unsigned char c) {
        return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '_';
    };
    if (!alpha(value.front())) return false;
    for (unsigned char c : value)
        if (!alpha(c) && !(c >= '0' && c <= '9')) return false;
    return true;
}

}  // namespace chudware::firmware
