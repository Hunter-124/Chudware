// Composer spell check + word completion: the word list, which words in a prompt are worth
// checking, correction suggestions, and the completion pick behind the composer's ghost text.
//
// GL-free and ImGui-free on purpose (unit-tested: host/tests/test_spell_check.cpp); the
// composer (composer_spell.cpp) owns loading, painting and the context menu.
#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace ui::copilot {

// One word to check: byte range [begin, end) into the scanned text.
struct SpellRange {
    std::size_t begin = 0;
    std::size_t end = 0;
};

// The prose words of `utf8` worth checking, sorted by position. Code (fences, inline spans,
// HTML/XML) never counts. A whitespace-delimited token is skipped whole when it looks like a
// path, URL, address, command, number or identifier (any of / \ @ # _ = digits, a dot inside
// it, non-ASCII other than a typographic apostrophe); hyphenated tokens check per part.
// Single letters, ALLCAPS and mixed-case words (camelCase, iPhone) are not checked.
std::vector<SpellRange> spellCheckWords(std::string_view utf8);

class SpellDictionary {
public:
    // Commonness of a word: lower = more common. SCOWL sizes 10..60 are the shipped tiers.
    static constexpr std::uint16_t kUnranked = 100;   // words before any `#tier` line
    static constexpr std::uint16_t kUserTier = 30;    // "Add to dictionary" words

    // Merge a word list: one word per line, `#tier N` sets the tier of the words after it,
    // any other `#` line is a comment. A word already present keeps its more common tier.
    void addWordList(std::string_view text);
    void addWord(std::string_view word, std::uint16_t tier);
    bool empty() const { return entries_.empty(); }

    // Is `word` spelled correctly? Case-insensitive; a typographic apostrophe reads as '
    // and a trailing possessive ('s or ') is accepted on a known stem.
    bool knows(std::string_view word) const;

    // Up to `max` corrections for a misspelled word: known words one edit away (delete,
    // transpose, replace, insert), then two, each group most common first. The first letter
    // keeps the word's capitalization.
    std::vector<std::string> suggestions(std::string_view word, std::size_t max) const;

    // The most common known word longer than `lowerPrefix` that starts with it (lowercase in,
    // lowercase out), or "" when there is none.
    std::string complete(std::string_view lowerPrefix) const;

private:
    struct Entry {
        std::string word;          // lowercase
        std::uint16_t tier;
    };
    const Entry* find(std::string_view lower) const;
    std::vector<Entry> entries_;   // sorted by word, unique
};

// Words seen in the conversation, counted, so completion prefers the user's own vocabulary
// (part names, feature names, domain terms) over a generic dictionary pick.
class WordCounts {
public:
    void clear() { counts_.clear(); }
    // Count every ASCII letter run of 4+ letters in `utf8`.
    void addText(std::string_view utf8);
    struct Seen {
        std::string spelling;      // as first seen (keeps OpenCASCADE's casing)
        int count = 0;
    };
    const std::unordered_map<std::string, Seen>& counts() const { return counts_; }

private:
    std::unordered_map<std::string, Seen> counts_;   // keyed lowercase
};

// What to append to the word `prefix` the caret sits after, or "" for no completion. Needs a
// 3+ letter ASCII prefix. A conversation word wins when it is a known word or was seen twice
// (most frequent first, shorter on ties); otherwise the dictionary's most common word. The
// suffix is uppercased after an ALLCAPS prefix, keeps a conversation word's own casing when
// the prefix matches it exactly, and is lowercase otherwise.
std::string completionSuffix(std::string_view prefix, const SpellDictionary& dict,
                             const WordCounts& seen);

}  // namespace ui::copilot
