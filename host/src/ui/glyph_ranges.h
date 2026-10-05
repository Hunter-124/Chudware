// glyph_ranges — the ONE table of codepoints the UI font atlas carries.
//
// Consumers: ui_theme.cpp loadFonts() requests exactly these ranges from the TTFs;
// copilot/agent_text.cpp atlasCovers() folds everything outside them before it is drawn;
// scripts/gui_lint.py --rule glyph parses kBody/kCadSymbols out of this file as its allowlist.
// Deliberately free of ImGui/GL so the GL-free agent_text unit (and its ctest) can include it.
#pragma once

#include <cstddef>

namespace ui::glyphs {

struct Range {
    unsigned lo, hi;   // inclusive
};

// Glyphs covered by Rubik / JetBrains Mono (the body faces).
// Latin-1 (incl. ° U+00B0, Ø U+00D8), general punctuation, superscripts/subscripts, bullet.
// NOTE: arrows (0x2190-0x21FF) and geometric-shapes (0x25A0-0x25FF) are intentionally NOT
// requested — Rubik does not ship them, so leaving them out keeps the atlas small and avoids
// allocating notdef boxes. CAD-specific math/shape glyphs come from kCadSymbols below.
inline constexpr Range kBody[] = {
    {0x0020, 0x00FF},
    {0x2010, 0x2027},
    {0x2070, 0x209F},
};

// CAD-specific symbols sourced from CADSymbols.ttf (merged over each body face).
// Codepoints: ∅ U+2205, ∥ U+2225, ≡ U+2261, ⊕ U+2295, ⊙ U+2299, ⊥ U+22A5,
// ⌀ U+2300, ◆ U+25C6, ◇ U+25C7. Keep this in sync with the font's cmap.
inline constexpr Range kCadSymbols[] = {
    {0x2205, 0x2205},
    {0x2225, 0x2225},
    {0x2261, 0x2261},
    {0x2295, 0x2295},
    {0x2299, 0x2299},
    {0x22A5, 0x22A5},
    {0x2300, 0x2300},
    {0x25C6, 0x25C7},
};

// Dear ImGui's flat, zero-terminated [lo, hi, lo, hi, ..., 0] form of one table, built at
// compile time. *Char* is ImWchar at the call site; this header never sees ImGui.
template <typename Char, std::size_t N>
struct FontRanges {
    Char v[N * 2 + 1];
};

template <typename Char, std::size_t N>
constexpr FontRanges<Char, N> fontRanges(const Range (&table)[N]) {
    FontRanges<Char, N> out{};
    for (std::size_t i = 0; i < N; ++i) {
        out.v[i * 2] = static_cast<Char>(table[i].lo);
        out.v[i * 2 + 1] = static_cast<Char>(table[i].hi);
    }
    out.v[N * 2] = 0;
    return out;
}

// Highest codepoint either table requests; the font loader asserts it fits ImWchar.
constexpr unsigned maxCodepoint() {
    unsigned m = 0;
    for (const Range& r : kBody) m = r.hi > m ? r.hi : m;
    for (const Range& r : kCadSymbols) m = r.hi > m ? r.hi : m;
    return m;
}

// True when the atlas holds a glyph for *cp* (either table).
constexpr bool inAtlas(unsigned cp) {
    for (const Range& r : kBody)
        if (cp >= r.lo && cp <= r.hi) return true;
    for (const Range& r : kCadSymbols)
        if (cp >= r.lo && cp <= r.hi) return true;
    return false;
}

}  // namespace ui::glyphs
