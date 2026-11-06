// Composer spell check + word completion — see spell_check.h.
#include "ui/copilot/spell_check.h"

#include "ui/copilot/magic_keywords.h"   // maskNonProse: code never gets checked

#include <algorithm>
#include <cstdlib>
#include <unordered_set>

namespace ui::copilot {

namespace {

constexpr std::string_view kRsquo = "\xE2\x80\x99";   // ’ typographic apostrophe

bool isUpper(char c) { return c >= 'A' && c <= 'Z'; }
bool isLower(char c) { return c >= 'a' && c <= 'z'; }
bool isAlpha(char c) { return isUpper(c) || isLower(c); }
char toLower(char c) { return isUpper(c) ? (char)(c - 'A' + 'a') : c; }
char toUpper(char c) { return isLower(c) ? (char)(c - 'a' + 'A') : c; }
bool isSpace(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r'; }

// Lowercase copy with typographic apostrophes folded to '.
std::string normalizeWord(std::string_view w) {
    std::string out;
    out.reserve(w.size());
    for (std::size_t i = 0; i < w.size();) {
        if (w.compare(i, kRsquo.size(), kRsquo) == 0) {
            out += '\'';
            i += kRsquo.size();
        } else {
            out += toLower(w[i++]);
        }
    }
    return out;
}

// Bytes of the punctuation mark that may wrap a prose word at s[i] (0 = none): quotes,
// brackets and emphasis before it; those plus sentence punctuation and an ellipsis after it.
std::size_t wrapPunctAt(std::string_view s, std::size_t i, bool leading) {
    static constexpr std::string_view kLead = "([{\"'*";
    static constexpr std::string_view kTrail = ".,;:!?)]}\"'*";
    if ((leading ? kLead : kTrail).find(s[i]) != std::string_view::npos) return 1;
    // “ ” ‘ ’ …  (E2 80 9C / 9D / 98 / 99 / A6)
    if (i + 3 <= s.size() && (unsigned char)s[i] == 0xE2 && (unsigned char)s[i + 1] == 0x80) {
        const auto b = (unsigned char)s[i + 2];
        if (b == 0x9C || b == 0x98) return leading ? 3 : 0;
        if (b == 0x9D || b == 0x99 || b == 0xA6) return leading ? 0 : 3;
    }
    return 0;
}

// Same as wrapPunctAt, for the mark that ENDS just before `end`.
std::size_t trailPunctBefore(std::string_view s, std::size_t begin, std::size_t end) {
    if (end - begin >= 3 && wrapPunctAt(s, end - 3, /*leading=*/false) == 3) return 3;
    return wrapPunctAt(s, end - 1, /*leading=*/false) == 1 ? 1 : 0;
}

bool isApostropheAt(std::string_view s, std::size_t i) {
    return s[i] == '\'' || s.compare(i, kRsquo.size(), kRsquo) == 0;
}
std::size_t apostropheLen(std::string_view s, std::size_t i) { return s[i] == '\'' ? 1 : 3; }

// One hyphen-separated part of a token: trim edge apostrophes, then keep it only when it is
// a 2+ letter word in plain or Capitalized case.
void emitPart(std::string_view s, std::size_t b, std::size_t e, std::vector<SpellRange>& out) {
    while (b < e && isApostropheAt(s, b)) b += apostropheLen(s, b);
    while (e > b && s[e - 1] == '\'') --e;
    while (e - b >= 3 && s.compare(e - 3, 3, kRsquo) == 0) e -= 3;
    if (e <= b || !isAlpha(s[b])) return;
    int letters = 0;
    for (std::size_t i = b; i < e; ++i) {
        if (isAlpha(s[i])) {
            ++letters;
            if (i > b && isUpper(s[i])) return;   // ALLCAPS, camelCase, iPhone
        }
    }
    if (letters >= 2) out.push_back(SpellRange{b, e});
}

// One whitespace-delimited token [b, e).
void scanToken(std::string_view s, std::size_t b, std::size_t e, std::vector<SpellRange>& out) {
    for (std::size_t n; b < e && (n = wrapPunctAt(s, b, /*leading=*/true)) != 0;) b += n;
    for (std::size_t n; e > b && (n = trailPunctBefore(s, b, e)) != 0;) e -= n;
    if (b >= e) return;
    // Anything but letters, apostrophes and hyphens: a path, URL, number, identifier, ...
    for (std::size_t i = b; i < e;) {
        if (isAlpha(s[i]) || s[i] == '\'' || s[i] == '-') { ++i; continue; }
        if (s.compare(i, kRsquo.size(), kRsquo) == 0) { i += kRsquo.size(); continue; }
        return;
    }
    std::size_t part = b;
    for (std::size_t i = b; i <= e; ++i) {
        if (i == e || s[i] == '-') {
            emitPart(s, part, i, out);
            part = i + 1;
        }
    }
}

}  // namespace

std::vector<SpellRange> spellCheckWords(std::string_view utf8) {
    std::string masked;
    std::string_view s = utf8;
    if (mayHoldNonProse(utf8)) {
        masked = maskNonProse(utf8);
        s = masked;
    }
    std::vector<SpellRange> out;
    std::size_t i = 0;
    while (i < s.size()) {
        while (i < s.size() && isSpace(s[i])) ++i;
        const std::size_t b = i;
        while (i < s.size() && !isSpace(s[i])) ++i;
        if (i > b) scanToken(s, b, i, out);
    }
    return out;
}

// ---- dictionary --------------------------------------------------------------------------------

void SpellDictionary::addWordList(std::string_view text) {
    std::uint16_t tier = kUnranked;
    std::size_t pos = 0;
    while (pos < text.size()) {
        std::size_t nl = text.find('\n', pos);
        if (nl == std::string_view::npos) nl = text.size();
        std::string_view line = text.substr(pos, nl - pos);
        pos = nl + 1;
        while (!line.empty() && isSpace(line.back())) line.remove_suffix(1);
        while (!line.empty() && isSpace(line.front())) line.remove_prefix(1);
        if (line.empty()) continue;
        if (line[0] == '#') {
            constexpr std::string_view kTier = "#tier ";
            if (line.compare(0, kTier.size(), kTier) == 0)
                tier = (std::uint16_t)std::clamp(
                    std::atoi(std::string(line.substr(kTier.size())).c_str()), 0, 65535);
            continue;
        }
        entries_.push_back(Entry{normalizeWord(line), tier});
    }
    // Sort by word then tier, and keep the first (most common) entry of every word.
    std::sort(entries_.begin(), entries_.end(), [](const Entry& a, const Entry& b) {
        return a.word != b.word ? a.word < b.word : a.tier < b.tier;
    });
    entries_.erase(std::unique(entries_.begin(), entries_.end(),
                               [](const Entry& a, const Entry& b) { return a.word == b.word; }),
                   entries_.end());
}

void SpellDictionary::addWord(std::string_view word, std::uint16_t tier) {
    std::string w = normalizeWord(word);
    if (w.empty()) return;
    auto it = std::lower_bound(entries_.begin(), entries_.end(), w,
                               [](const Entry& e, const std::string& v) { return e.word < v; });
    if (it != entries_.end() && it->word == w) it->tier = std::min(it->tier, tier);
    else entries_.insert(it, Entry{std::move(w), tier});
}

const SpellDictionary::Entry* SpellDictionary::find(std::string_view lower) const {
    auto it = std::lower_bound(entries_.begin(), entries_.end(), lower,
                               [](const Entry& e, std::string_view v) { return e.word < v; });
    return (it != entries_.end() && it->word == lower) ? &*it : nullptr;
}

bool SpellDictionary::knows(std::string_view word) const {
    const std::string w = normalizeWord(word);
    const std::string_view v = w;
    if (find(v)) return true;
    if (v.size() > 2 && v.substr(v.size() - 2) == "'s") return find(v.substr(0, v.size() - 2));
    if (v.size() > 1 && v.back() == '\'') return find(v.substr(0, v.size() - 1));
    return false;
}

std::vector<std::string> SpellDictionary::suggestions(std::string_view word,
                                                      std::size_t max) const {
    const std::string w = normalizeWord(word);
    if (w.empty() || max == 0) return {};
    static constexpr std::string_view kAlphabet = "abcdefghijklmnopqrstuvwxyz'";
    // Every string one edit from `s`, handed to `emit` with whether the edit is a typing slip
    // (swapped neighbours, a doubled letter dropped or added) rather than a wrong letter.
    const auto edits = [](const std::string& s, const auto& emit) {
        std::string t;
        for (std::size_t i = 0; i < s.size(); ++i) {
            t = s;
            t.erase(i, 1);
            emit(t, (i > 0 && s[i - 1] == s[i]) || (i + 1 < s.size() && s[i + 1] == s[i]));
        }
        for (std::size_t i = 0; i + 1 < s.size(); ++i) {
            t = s;
            std::swap(t[i], t[i + 1]);
            emit(t, true);
        }
        for (std::size_t i = 0; i < s.size(); ++i) {
            for (char c : kAlphabet) {
                if (c == s[i]) continue;
                t = s;
                t[i] = c;
                emit(t, false);
            }
        }
        for (std::size_t i = 0; i <= s.size(); ++i) {
            for (char c : kAlphabet) {
                t = s;
                t.insert(t.begin() + (std::ptrdiff_t)i, c);
                emit(t, (i > 0 && s[i - 1] == c) || (i < s.size() && s[i] == c));
            }
        }
    };
    struct Cand {
        const Entry* e;
        int cost;   // 0 = one slip, 1 = one edit, 2 = two edits
    };
    std::vector<Cand> found;
    std::unordered_map<const Entry*, std::size_t> at;   // entry -> index in found
    const auto consider = [&](const std::string& t, int cost) {
        const Entry* e = find(t);
        if (!e || t == w) return;
        auto [it, fresh] = at.emplace(e, found.size());
        if (fresh) found.push_back({e, cost});
        else found[it->second].cost = std::min(found[it->second].cost, cost);
    };
    std::vector<std::string> ring1;
    edits(w, [&](const std::string& t, bool slip) {
        consider(t, slip ? 0 : 1);
        ring1.push_back(t);
    });
    // Two edits only when one did not fill the list; long words would make it slow and noisy.
    if (found.size() < max && w.size() <= 14) {
        for (const std::string& r : ring1)
            edits(r, [&](const std::string& t, bool) { consider(t, 2); });
    }
    const auto lenGap = [&](const Entry* e) {
        return std::abs((int)e->word.size() - (int)w.size());
    };
    std::sort(found.begin(), found.end(), [&](const Cand& a, const Cand& b) {
        if (a.cost != b.cost) return a.cost < b.cost;
        if (a.e->tier != b.e->tier) return a.e->tier < b.e->tier;
        if (lenGap(a.e) != lenGap(b.e)) return lenGap(a.e) < lenGap(b.e);
        return a.e->word < b.e->word;
    });
    const bool capital = isUpper(word[0]);
    std::vector<std::string> out;
    for (std::size_t i = 0; i < found.size() && out.size() < max; ++i) {
        std::string s = found[i].e->word;
        if (capital) s[0] = toUpper(s[0]);
        out.push_back(std::move(s));
    }
    return out;
}

std::string SpellDictionary::complete(std::string_view lowerPrefix) const {
    auto it = std::lower_bound(entries_.begin(), entries_.end(), lowerPrefix,
                               [](const Entry& e, std::string_view v) { return e.word < v; });
    const Entry* best = nullptr;
    for (; it != entries_.end() && it->word.compare(0, lowerPrefix.size(), lowerPrefix) == 0;
         ++it) {
        if (it->word.size() <= lowerPrefix.size()) continue;
        if (!best || it->tier < best->tier ||
            (it->tier == best->tier && it->word.size() < best->word.size()))
            best = &*it;
    }
    return best ? best->word : std::string();
}

// ---- conversation vocabulary + completion ------------------------------------------------------

void WordCounts::addText(std::string_view utf8) {
    std::size_t i = 0;
    while (i < utf8.size()) {
        if (!isAlpha(utf8[i])) { ++i; continue; }
        const std::size_t b = i;
        while (i < utf8.size() && isAlpha(utf8[i])) ++i;
        if (i - b < 4) continue;
        const std::string_view run = utf8.substr(b, i - b);
        Seen& s = counts_[normalizeWord(run)];
        if (s.count++ == 0) s.spelling = std::string(run);
    }
}

std::string completionSuffix(std::string_view prefix, const SpellDictionary& dict,
                             const WordCounts& seen) {
    if (prefix.size() < 3) return {};
    bool allCaps = true;
    for (char c : prefix) {
        if (!isAlpha(c)) return {};
        allCaps = allCaps && isUpper(c);
    }
    const std::string lp = normalizeWord(prefix);

    const std::string* bestKey = nullptr;
    const WordCounts::Seen* best = nullptr;
    for (const auto& [key, s] : seen.counts()) {
        if (key.size() <= lp.size() || key.compare(0, lp.size(), lp) != 0) continue;
        if (s.count < 2 && !dict.knows(key)) continue;   // a one-off may be a typo
        if (!best || s.count > best->count ||
            (s.count == best->count &&
             (key.size() < bestKey->size() || (key.size() == bestKey->size() && key < *bestKey)))) {
            best = &s;
            bestKey = &key;
        }
    }
    std::string suffix;
    if (best) {
        suffix = best->spelling.compare(0, prefix.size(), prefix) == 0
                     ? best->spelling.substr(prefix.size())
                     : bestKey->substr(lp.size());
    } else {
        // A finished word ("the", "fill") would otherwise always grow a ghost tail.
        if (dict.knows(lp)) return {};
        const std::string w = dict.complete(lp);
        if (w.empty()) return {};
        suffix = w.substr(lp.size());
    }
    if (allCaps)
        for (char& c : suffix) c = toUpper(c);
    return suffix;
}

}  // namespace ui::copilot
