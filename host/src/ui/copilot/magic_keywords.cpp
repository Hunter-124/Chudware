// Magic-keyword matcher — see magic_keywords.h. A byte-for-byte port of omp's standalone-prose
// rules (tui/src/prompt/magic-keywords.ts boundaries + markdown-prose.ts maskNonProse) to UTF-8.
#include "ui/copilot/magic_keywords.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <string>

namespace ui::copilot {

const std::vector<MagicKeyword>& magicKeywords() {
    static const std::vector<MagicKeyword> kTable = {
        {"ultrathink", 0.0f, 330.0f},
        {"orchestrate", 150.0f, 280.0f},
        {"workflowz", 30.0f, 150.0f},
        {"jevify", 300.0f, 420.0f},
        {"ultragoon", 330.0f, 270.0f},
        {"autobot", 190.0f, 260.0f},
        {"tokenmaxxing", 410.0f, 340.0f},
    };
    return kTable;
}

float hueAt(int keyword, float t) {
    const std::vector<MagicKeyword>& table = magicKeywords();
    if (keyword < 0 || keyword >= (int)table.size()) return 0.0f;
    t = std::clamp(t, 0.0f, 1.0f);
    const MagicKeyword& k = table[(size_t)keyword];
    const float h = std::fmod(k.hueFrom + t * (k.hueTo - k.hueFrom), 360.0f);
    return h < 0.0f ? h + 360.0f : h;
}

namespace {

// ---- UTF-8 codepoint classification ---------------------------------------------------------

// Decode the codepoint starting at `i` (no validation beyond length; malformed bytes decode as
// themselves, which classifies them as "letter" — conservative: an unknown neighbour never
// lets a keyword match).
std::uint32_t decodeAt(std::string_view s, std::size_t i) {
    const auto b0 = (unsigned char)s[i];
    if (b0 < 0x80) return b0;
    int extra = (b0 >= 0xF0) ? 3 : (b0 >= 0xE0) ? 2 : (b0 >= 0xC0) ? 1 : 0;
    std::uint32_t cp = (extra == 3) ? (b0 & 0x07u) : (extra == 2) ? (b0 & 0x0Fu)
                     : (extra == 1) ? (b0 & 0x1Fu) : b0;
    for (int k = 1; k <= extra; ++k) {
        if (i + (std::size_t)k >= s.size()) return b0;
        const auto bk = (unsigned char)s[i + (std::size_t)k];
        if ((bk & 0xC0) != 0x80) return b0;
        cp = (cp << 6) | (bk & 0x3Fu);
    }
    return cp;
}

// Start byte of the codepoint that ends just before `i` (i > 0).
std::size_t prevStart(std::string_view s, std::size_t i) {
    std::size_t j = i - 1;
    int guard = 0;
    while (j > 0 && ((unsigned char)s[j] & 0xC0) == 0x80 && guard < 3) { --j; ++guard; }
    return j;
}

// \p{L} / \p{N} approximation: ASCII letters/digits, and every non-ASCII codepoint that is not
// in a punctuation / symbol / space block. Smart quotes, dashes, ellipsis, NBSP, guillemets
// and CJK punctuation therefore act as prose punctuation, exactly like their ASCII cousins.
bool isLetterOrDigit(std::uint32_t cp) {
    if (cp < 0x80)
        return (cp >= '0' && cp <= '9') || (cp >= 'A' && cp <= 'Z') || (cp >= 'a' && cp <= 'z');
    if (cp <= 0xBF) return cp == 0xAA || cp == 0xB5 || cp == 0xBA;   // ª µ º are letters
    if (cp == 0xD7 || cp == 0xF7) return false;                         // × ÷
    if (cp >= 0x2000 && cp <= 0x206F) return false;                     // general punctuation
    if (cp >= 0x2190 && cp <= 0x2BFF) return false;                     // arrows .. misc symbols
    if (cp >= 0x3000 && cp <= 0x303F) return false;                     // CJK punctuation
    if (cp >= 0xFF00 && cp <= 0xFF0F) return false;                     // fullwidth punctuation
    return true;
}

// Left boundary: not preceded by a letter/digit/_/./slash/backslash/hyphen, nor by `::`.
bool leftBoundaryOk(std::string_view s, std::size_t at) {
    if (at == 0) return true;
    const std::size_t ps = prevStart(s, at);
    const std::uint32_t cp = decodeAt(s, ps);
    if (isLetterOrDigit(cp)) return false;
    if (cp == '_' || cp == '.' || cp == '/' || cp == '\\' || cp == '-') return false;
    if (at >= 2 && s[at - 1] == ':' && s[at - 2] == ':') return false;
    return true;
}

bool isExtensionChar(std::uint32_t cp) {
    return isLetterOrDigit(cp) || cp == '_' || cp == '-';
}

// Right boundary: not followed by a letter/digit/_/slash/backslash/hyphen, a file-extension
// dot (`.` + word char) or an immediate call paren.
bool rightBoundaryOk(std::string_view s, std::size_t at) {
    if (at >= s.size()) return true;
    const std::uint32_t cp = decodeAt(s, at);
    if (isLetterOrDigit(cp)) return false;
    if (cp == '_' || cp == '/' || cp == '\\' || cp == '-' || cp == '(') return false;
    if (cp == '.' && at + 1 < s.size() && isExtensionChar(decodeAt(s, at + 1))) return false;
    return true;
}

// ---- non-prose masking (markdown-prose.ts maskNonProse) ---------------------------------------

bool isTagStart(char c) { return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z'); }
bool isTagChar(char c) { return isTagStart(c) || (c >= '0' && c <= '9') || c == '-'; }

// End (exclusive) of a [A-Za-z][A-Za-z0-9-]* tag name at `j`, or npos when none starts there.
std::size_t tagNameEnd(std::string_view t, std::size_t j) {
    if (j >= t.size() || !isTagStart(t[j])) return std::string_view::npos;
    std::size_t k = j + 1;
    while (k < t.size() && isTagChar(t[k])) ++k;
    return k;
}

std::size_t backtickRunEnd(std::string_view t, std::size_t i) {
    std::size_t j = i;
    while (j < t.size() && t[j] == '`') ++j;
    return j;
}

std::size_t findBacktickClose(std::string_view t, std::size_t from, std::size_t runLen,
                              const std::vector<unsigned char>& masked) {
    std::size_t k = from;
    while (k < t.size()) {
        if (masked[k]) { ++k; continue; }
        if (t[k] == '`') {
            const std::size_t e = backtickRunEnd(t, k);
            if (e - k == runLen) return e;
            k = e;
            continue;
        }
        ++k;
    }
    return std::string_view::npos;
}

// Index of the '>' closing a tag whose attributes begin at `j` (quotes honored), npos when a
// new '<' comes first or there is none.
std::size_t findTagEnd(std::string_view t, std::size_t j) {
    char quote = 0;
    for (std::size_t k = j; k < t.size(); ++k) {
        const char ch = t[k];
        if (quote) {
            if (ch == quote) quote = 0;
            continue;
        }
        if (ch == '"' || ch == '\'') { quote = ch; continue; }
        if (ch == '>') return k;
        if (ch == '<') return std::string_view::npos;
    }
    return std::string_view::npos;
}

bool iequals(std::string_view a, std::string_view b) {
    if (a.size() != b.size()) return false;
    for (std::size_t i = 0; i < a.size(); ++i) {
        char x = a[i], y = b[i];
        if (x >= 'A' && x <= 'Z') x = (char)(x - 'A' + 'a');
        if (y >= 'A' && y <= 'Z') y = (char)(y - 'A' + 'a');
        if (x != y) return false;
    }
    return true;
}

std::size_t findMatchingClose(std::string_view t, std::size_t start, std::string_view name,
                              const std::vector<unsigned char>& masked) {
    int depth = 1;
    std::size_t k = start;
    while (k < t.size()) {
        if (masked[k] || t[k] != '<') { ++k; continue; }
        std::size_t m = k + 1;
        bool isClose = false;
        if (m < t.size() && t[m] == '/') { isClose = true; ++m; }
        const std::size_t ne = tagNameEnd(t, m);
        if (ne == std::string_view::npos) { ++k; continue; }
        const std::size_t gt = findTagEnd(t, ne);
        if (gt == std::string_view::npos) { ++k; continue; }
        if (iequals(t.substr(m, ne - m), name)) {
            if (isClose) {
                if (--depth == 0) return gt + 1;
            } else if (t[gt - 1] != '/') {
                ++depth;
            }
        }
        k = gt + 1;
    }
    return std::string_view::npos;
}

void maskRange(std::vector<unsigned char>& masked, std::size_t a, std::size_t b) {
    for (std::size_t p = a; p < b && p < masked.size(); ++p) masked[p] = 1;
}

// Mask the HTML/XML construct at '<' (index i); returns the index past it, or i when the '<'
// does not open a tag.
std::size_t maskTagAt(std::string_view t, std::size_t i, std::vector<unsigned char>& masked) {
    if (t.compare(i, 4, "<!--") == 0) {
        const std::size_t end = t.find("-->", i + 4);
        const std::size_t stop = end == std::string_view::npos ? t.size() : end + 3;
        maskRange(masked, i, stop);
        return stop;
    }
    std::size_t j = i + 1;
    bool closing = false;
    if (j < t.size() && t[j] == '/') { closing = true; ++j; }
    const std::size_t ne = tagNameEnd(t, j);
    if (ne == std::string_view::npos) return i;
    const std::size_t gt = findTagEnd(t, ne);
    if (gt == std::string_view::npos) return i;
    const std::size_t tagEnd = gt + 1;
    const bool selfClosing = t[gt - 1] == '/';
    maskRange(masked, i, tagEnd);
    if (closing || selfClosing) return tagEnd;
    const std::size_t close = findMatchingClose(t, tagEnd, t.substr(j, ne - j), masked);
    if (close == std::string_view::npos) return tagEnd;
    maskRange(masked, tagEnd, close);
    return close;
}

// Fence opener/closer: up to 3 spaces of indent, then 3+ of one of ` or ~. Returns the marker
// length (0 = not a fence) and its char / indent.
std::size_t fenceMarker(std::string_view line, char& ch, std::size_t& indent) {
    std::size_t i = 0;
    while (i < line.size() && i < 3 && line[i] == ' ') ++i;
    if (i >= line.size() || (line[i] != '`' && line[i] != '~')) return 0;
    std::size_t j = i;
    const char c = line[i];
    while (j < line.size() && line[j] == c) ++j;
    if (j - i < 3) return 0;
    ch = c;
    indent = i;
    return j - i;
}

bool blankRest(std::string_view s) {
    for (char c : s)
        if (c != ' ' && c != '\t' && c != '\r') return false;
    return true;
}

// Can `t` hold a non-prose region at all? (No backtick, '<' or tilde fence = all prose.)
bool mayHoldNonProse(std::string_view t) {
    return t.find('`') != std::string_view::npos || t.find('<') != std::string_view::npos ||
           t.find("~~~") != std::string_view::npos;
}

// Length-preserving copy with every byte inside a non-prose region turned into a space
// (newlines kept), so a boundary test run against it never lands inside code or markup.
std::string maskNonProse(std::string_view t) {
    std::string out(t);
    const std::size_t n = t.size();
    std::vector<unsigned char> masked(n, 0);

    // Phase 1: fenced code blocks, line by line.
    char fenceChar = 0;
    std::size_t fenceLen = 0;
    std::size_t lineStart = 0;
    while (lineStart <= n) {
        std::size_t nl = t.find('\n', lineStart);
        if (nl == std::string_view::npos) nl = n;
        const std::string_view line = t.substr(lineStart, nl - lineStart);
        char ch = 0;
        std::size_t indent = 0;
        const std::size_t len = fenceMarker(line, ch, indent);
        if (fenceChar) {
            maskRange(masked, lineStart, nl);
            if (len && ch == fenceChar && len >= fenceLen && blankRest(line.substr(indent + len))) {
                fenceChar = 0;
                fenceLen = 0;
            }
        } else if (len) {
            // A backtick fence's info string may not contain a backtick.
            if (!(ch == '`' && line.substr(indent + len).find('`') != std::string_view::npos)) {
                fenceChar = ch;
                fenceLen = len;
                maskRange(masked, lineStart, nl);
            }
        }
        if (nl == n) break;
        lineStart = nl + 1;
    }

    // Phase 2: inline code spans and HTML/XML over the not-yet-masked bytes.
    std::size_t i = 0;
    while (i < n) {
        if (masked[i]) { ++i; continue; }
        const char c = t[i];
        if (c == '`') {
            const std::size_t runEnd = backtickRunEnd(t, i);
            const std::size_t close = findBacktickClose(t, runEnd, runEnd - i, masked);
            if (close != std::string_view::npos) {
                maskRange(masked, i, close);
                i = close;
            } else {
                i = runEnd;
            }
            continue;
        }
        if (c == '<') {
            const std::size_t end = maskTagAt(t, i, masked);
            i = end > i ? end : i + 1;
            continue;
        }
        ++i;
    }

    for (std::size_t p = 0; p < n; ++p)
        if (masked[p] && out[p] != '\n') out[p] = ' ';
    return out;
}

}  // namespace

std::vector<KeywordRange> magicKeywordRanges(std::string_view utf8) {
    std::vector<KeywordRange> ranges;
    const std::vector<MagicKeyword>& table = magicKeywords();
    // Cheap probe first: the common "no keyword in this text" path never pays for masking.
    bool any = false;
    for (const MagicKeyword& k : table)
        if (utf8.find(k.word) != std::string_view::npos) { any = true; break; }
    if (!any) return ranges;

    std::string masked;
    std::string_view ms = utf8;
    if (mayHoldNonProse(utf8)) {
        masked = maskNonProse(utf8);
        ms = masked;
    }
    for (std::size_t ki = 0; ki < table.size(); ++ki) {
        const std::string_view w = table[ki].word;
        std::size_t at = ms.find(w);
        while (at != std::string_view::npos) {
            const std::size_t end = at + w.size();
            if (leftBoundaryOk(ms, at) && rightBoundaryOk(ms, end))
                ranges.push_back(KeywordRange{at, end, (int)ki});
            at = ms.find(w, at + 1);
        }
    }
    std::sort(ranges.begin(), ranges.end(),
              [](const KeywordRange& a, const KeywordRange& b) { return a.begin < b.begin; });
    return ranges;
}

}  // namespace ui::copilot
