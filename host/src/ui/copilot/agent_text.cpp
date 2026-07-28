// agent_text — see agent_text.h for why this is its own GL-free translation unit.
#include "agent_text.h"

#include <cctype>

namespace ui {

// ===========================================================================================
// 1. opaque engine doc ids
// ===========================================================================================

namespace {

inline bool isHexDigit(char c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

// What may NOT sit against a doc id if it is to count as a whole token. Letters, digits and the
// two separators that appear inside identifiers here — so "mydoc-cpp1", "doc-cpp39x" and
// "part-doc-cpp39" are all left alone, while '"', '(', ' ', ',', '{' and ':' are boundaries.
inline bool isTokenChar(char c) {
    return std::isalnum((unsigned char)c) != 0 || c == '_' || c == '-';
}

// The 7-byte prefix at *i*, in either spelling the engine emits.
inline bool hasDocPrefix(const std::string& s, size_t i) {
    if (i + 7 > s.size() || s[i] != 'd') return false;
    return s.compare(i, 7, "doc-cpp") == 0 || s.compare(i, 7, "doc_cpp") == 0;
}

}  // namespace

std::string substituteDocIds(const std::string& text,
                             const std::function<std::string(const std::string&)>& resolve) {
    // Shortest possible id is "doc-cpp0" (8 bytes) — and with no resolver there is nothing to do.
    if (text.size() < 8 || !resolve) return text;

    std::string out;          // stays empty (unallocated) until the first real rewrite
    size_t copied = 0;        // how much of `text` has been flushed into `out`

    for (size_t i = 0; i + 8 <= text.size();) {
        if (!hasDocPrefix(text, i)) { ++i; continue; }
        size_t end = i + 7;
        while (end < text.size() && isHexDigit(text[end])) ++end;
        if (end == i + 7) { i += 7; continue; }              // prefix with no hex suffix

        // Whole-token boundaries on both sides, else this is a longer identifier.
        if ((i > 0 && isTokenChar(text[i - 1])) ||
            (end < text.size() && isTokenChar(text[end]))) {
            i = end;
            continue;
        }
        // Idempotency: skip an id already rendered by us as `Name (doc-cppNN)`. Both the ingest
        // side and the render side run this over the same strings; without this a second pass
        // would nest the label inside itself.
        if (i >= 2 && text[i - 1] == '(' && text[i - 2] == ' ' &&
            end < text.size() && text[end] == ')') {
            i = end;
            continue;
        }

        const std::string id = text.substr(i, end - i);
        const std::string label = resolve(id);
        if (label.empty()) { i = end; continue; }            // no name for it — leave it verbatim

        if (out.empty()) out.reserve(text.size() + 32);
        out.append(text, copied, i - copied);
        out += label;
        out += " (";
        out += id;
        out += ')';
        copied = end;
        i = end;
    }

    if (copied == 0) return text;                            // no match: unchanged, no scratch buf
    out.append(text, copied, std::string::npos);
    return out;
}

// ===========================================================================================
// 2. codepoints the font atlas has no glyph for
// ===========================================================================================

namespace {

// MIRROR of host/src/ui_theme.cpp uiGlyphRanges() (:662) + cadSymbolRanges() (:675). Those are
// file-static in a TU that pulls in ImGui and GL, so a GL-free unit-testable TU cannot call them.
// test_agent_text.cpp parses ui_theme.cpp and FAILS if this table drifts from it — do not edit
// one without the other.
const unsigned kCovered[] = {
    // uiGlyphRanges(): Latin-1, general punctuation, superscripts/subscripts.
    0x0020, 0x00FF,
    0x2010, 0x2027,
    0x2070, 0x209F,
    // cadSymbolRanges(): CADSymbols.ttf merged over the body face.
    0x2205, 0x2205,
    0x2225, 0x2225,
    0x2261, 0x2261,
    0x2295, 0x2295,
    0x2299, 0x2299,
    0x22A5, 0x22A5,
    0x2300, 0x2300,
    0x25C6, 0x25C7,
};
constexpr std::size_t kCoveredPairs = sizeof(kCovered) / sizeof(kCovered[0]) / 2;

// Uncovered codepoints worth spelling out rather than dropping to '?'. Kept ASCII on purpose:
// an ASCII replacement is covered by definition and cannot itself need folding.
struct GlyphFold { unsigned cp; const char* repl; };
const GlyphFold kFolds[] = {
    {0x2190, "<-"},  {0x2191, "^"},   {0x2192, "->"},  {0x2193, "v"},   {0x2194, "<->"},
    {0x21A6, "|->"}, {0x21D0, "<="},  {0x21D2, "=>"},  {0x21D4, "<=>"},
    {0x2208, "in"},  {0x2209, "!in"}, {0x2211, "sum"}, {0x2212, "-"},   {0x2213, "-+"},
    {0x221A, "sqrt"},{0x221E, "inf"}, {0x2248, "~"},   {0x2260, "!="},  {0x2264, "<="},
    {0x2265, ">="},
    // Box drawing: the light forms the harness uses for rules and trees.
    {0x2500, "-"},   {0x2501, "-"},   {0x2502, "|"},   {0x2503, "|"},
    {0x2504, "-"},   {0x2505, "-"},   {0x2506, "|"},   {0x2507, "|"},
    {0x2508, "-"},   {0x2509, "-"},   {0x250A, "|"},   {0x250B, "|"},
    {0x254C, "-"},   {0x254D, "-"},   {0x254E, "|"},   {0x254F, "|"},
    // Marks the harness uses for pass/fail.
    {0x2713, "ok"},  {0x2714, "ok"},  {0x2717, "x"},   {0x2718, "x"},
    {0x2705, "ok"},  {0x274C, "x"},   {0x26A0, "!"},
};

// Ranges folded wholesale after the table above misses. Order matters only in that these are
// checked last.
const char* rangeFold(unsigned cp) {
    if (cp >= 0x2500 && cp <= 0x257F) return "+";   // remaining box-drawing joints/corners
    if (cp >= 0x2580 && cp <= 0x259F) return "#";   // block elements (progress bars)
    if (cp >= 0x25A0 && cp <= 0x25FF) return "*";   // geometric shapes (bullets, arrowheads)
    if (cp >= 0x2190 && cp <= 0x21FF) return "->";  // remaining arrows: direction is the meaning
    return nullptr;
}

// Decode one UTF-8 scalar at *i*. On success advances *i* and returns true. On any malformed or
// truncated sequence, advances *i* by exactly one byte and returns false (the caller emits '?').
bool decodeUtf8(const std::string& s, size_t& i, unsigned& cp) {
    const unsigned char b0 = (unsigned char)s[i];
    size_t need;
    unsigned acc;
    if (b0 < 0x80)                       { cp = b0; ++i; return true; }
    else if ((b0 & 0xE0) == 0xC0)        { need = 1; acc = b0 & 0x1Fu; }
    else if ((b0 & 0xF0) == 0xE0)        { need = 2; acc = b0 & 0x0Fu; }
    else if ((b0 & 0xF8) == 0xF0)        { need = 3; acc = b0 & 0x07u; }
    else                                 { ++i; return false; }   // stray continuation / 0xF8+
    if (i + need >= s.size()) { ++i; return false; }               // truncated
    for (size_t k = 1; k <= need; ++k) {
        const unsigned char bk = (unsigned char)s[i + k];
        if ((bk & 0xC0) != 0x80) { ++i; return false; }
        acc = (acc << 6) | (bk & 0x3Fu);
    }
    // Overlong, surrogate and out-of-range encodings are malformed too.
    static const unsigned kMin[4] = {0, 0x80, 0x800, 0x10000};
    if (acc < kMin[need] || acc > 0x10FFFFu || (acc >= 0xD800u && acc <= 0xDFFFu)) {
        ++i;
        return false;
    }
    cp = acc;
    i += need + 1;
    return true;
}

}  // namespace

bool atlasCovers(unsigned cp) {
    for (std::size_t p = 0; p < kCoveredPairs; ++p)
        if (cp >= kCovered[p * 2] && cp <= kCovered[p * 2 + 1]) return true;
    // Tab / newline are not in any range but are layout, not glyphs — ImGui handles them.
    return cp == '\n' || cp == '\t' || cp == '\r' || cp == 0x1E;
}

const unsigned* atlasCoveredRanges(std::size_t& pairs) {
    pairs = kCoveredPairs;
    return kCovered;
}

std::string foldToAtlasGlyphs(const std::string& text) {
    // Fast path: pure ASCII (below 0x80) is entirely inside 0x0020-0x00FF plus the control
    // characters atlasCovers() waves through, so there is nothing to rewrite.
    bool hasHighByte = false;
    for (unsigned char c : text)
        if (c >= 0x80) { hasHighByte = true; break; }
    if (!hasHighByte) return text;

    std::string out;
    out.reserve(text.size() + 16);
    size_t i = 0;
    while (i < text.size()) {
        const size_t start = i;
        unsigned cp = 0;
        if (!decodeUtf8(text, i, cp)) { out += '?'; continue; }   // malformed byte
        if (cp < 0x80 || atlasCovers(cp)) {
            out.append(text, start, i - start);                   // keep the original bytes
            continue;
        }
        const char* repl = nullptr;
        for (const GlyphFold& f : kFolds)
            if (f.cp == cp) { repl = f.repl; break; }
        if (!repl) repl = rangeFold(cp);
        out += repl ? repl : "?";                                 // no mapping -> notdef stand-in
    }
    return out;
}

}  // namespace ui
