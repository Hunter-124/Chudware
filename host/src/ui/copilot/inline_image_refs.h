#pragma once

#include <cstddef>
#include <limits>
#include <string>
#include <string_view>
#include <vector>

namespace ui::copilot {

// Numbered references index the exact image-path roster, never a filename lookup.
struct InlineImageRef {
    std::size_t begin;
    std::size_t end;
    std::size_t image;
};

inline std::string inlineImageMarker(std::size_t image) {
    return "[Image #" + std::to_string(image + 1) + "]";
}

inline std::vector<InlineImageRef> inlineImageRefs(std::string_view text,
                                                  std::size_t imageCount) {
    std::vector<InlineImageRef> refs;
    constexpr std::string_view prefix = "[Image #";
    for (std::size_t pos = 0; (pos = text.find(prefix, pos)) != std::string_view::npos;) {
        std::size_t end = pos + prefix.size();
        std::size_t number = 0;
        const std::size_t digits = end;
        while (end < text.size() && text[end] >= '0' && text[end] <= '9') {
            const std::size_t digit = static_cast<std::size_t>(text[end] - '0');
            if (number > imageCount / 10 ||
                (number == imageCount / 10 && digit > imageCount % 10)) break;
            number = number * 10 + digit;
            ++end;
        }
        if (end > digits && end < text.size() && text[end] == ']' && number > 0 &&
            number <= imageCount)
            refs.push_back({pos, end + 1, number - 1});
        pos = end > pos ? end : pos + prefix.size();
    }
    return refs;
}

// One-line surfaces (session titles) have no image roster to resolve numbers against: drop every
// "[Image #N]" marker whatever N is, then collapse the blanks the markers leave behind.
inline std::string stripInlineImageMarkers(std::string_view text) {
    std::string stripped;
    stripped.reserve(text.size());
    std::size_t cursor = 0;
    for (const InlineImageRef& ref : inlineImageRefs(text, std::numeric_limits<std::size_t>::max())) {
        stripped.append(text.substr(cursor, ref.begin - cursor));
        stripped += ' ';
        cursor = ref.end;
    }
    stripped.append(text.substr(cursor));
    std::string out;
    out.reserve(stripped.size());
    for (const char ch : stripped) {
        const bool blank = ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r';
        if (!blank) out += ch;
        else if (!out.empty() && out.back() != ' ') out += ' ';
    }
    if (!out.empty() && out.back() == ' ') out.pop_back();
    return out;
}

}  // namespace ui::copilot
