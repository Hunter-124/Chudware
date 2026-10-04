// Magic keywords: standalone lowercase prose words ("orchestrate", "ultrathink", ...) that the
// agent runtime (Chudcode) gives special meaning server-side. The GUI only PRESENTS them — a
// per-character hue gradient in sent user bubbles and an animated shimmer in the composer —
// mirroring omp's TUI (packages/tui/src/prompt/magic-keywords.ts + markdown-prose.ts).
//
// This header is the single spelling of "is this a keyword occurrence" for the copilot: the
// composer and the chat log both ask magicKeywordRanges(), never an ad-hoc substring test.
// GL-free and ImGui-free on purpose (unit-tested: host/tests/test_magic_keywords.cpp).
#pragma once
#include <cstddef>
#include <string_view>
#include <vector>

namespace ui::copilot {

// One keyword: the exact lowercase trigger and its gradient as an HSL hue sweep in degrees.
// `hueTo` may exceed 360 (or sit below `hueFrom`) — the sweep wraps through red.
struct MagicKeyword {
    std::string_view word;
    float hueFrom;
    float hueTo;
};

// The keyword table (same rows, same order as omp's MAGIC_KEYWORDS).
const std::vector<MagicKeyword>& magicKeywords();

// One standalone-prose occurrence: byte range [begin, end) into the scanned text plus the
// index of the keyword in magicKeywords().
struct KeywordRange {
    std::size_t begin = 0;
    std::size_t end = 0;
    int keyword = -1;
};

// Every keyword occurrence that stands as prose in `utf8`, sorted by position. Case-sensitive,
// word-bounded at prose punctuation (a letter/digit/_/./slash/backslash/hyphen or `::` before
// it, or a letter/digit/_/slash/backslash/hyphen, a file-extension dot or a call paren after it,
// keeps it embedded in code), and never inside a fenced code block, an inline code span or an
// HTML/XML section.
std::vector<KeywordRange> magicKeywordRanges(std::string_view utf8);

// Gradient hue (degrees, [0, 360)) of keyword `keyword` at normalized position t in [0, 1]
// across the word. t is clamped; an out-of-range keyword index returns 0.
float hueAt(int keyword, float t);

}  // namespace ui::copilot
