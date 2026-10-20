#pragma once

#include <cstddef>
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

}  // namespace ui::copilot
