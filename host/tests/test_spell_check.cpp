// test_spell_check.cpp — ctest for the composer's spell check and word completion
// (src/ui/copilot/spell_check.cpp). No GL, ImGui or AppState.
//
// Acceptance:
//   - prose words are checked; code spans/fences, paths, URLs, identifiers, numbers,
//     ALLCAPS, camelCase and single letters are not; hyphenated words check per part
//   - wrapping punctuation and quotes never become part of the checked range
//   - knows(): case-insensitive, typographic apostrophe, possessive on a known stem
//   - a later word list keeps a word's more common tier
//   - suggestions(): one edit before two, common before rare, capitalization kept
//   - completion: 3+ letters, conversation vocabulary before the dictionary, a finished
//     dictionary word grows no tail, casing follows the prefix
//   - the shipped en_US + engineering lists load and correct real typos (when present)

#include "ui/copilot/spell_check.h"

#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

static int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

using ui::copilot::SpellDictionary;
using ui::copilot::WordCounts;

// The checked words of `s`, as strings.
static std::vector<std::string> words(const std::string& s) {
    std::vector<std::string> out;
    for (const auto& r : ui::copilot::spellCheckWords(s)) out.push_back(s.substr(r.begin, r.end - r.begin));
    return out;
}

static void testTokenizer() {
    CHECK((words("Make the boss taller.") == std::vector<std::string>{"Make", "the", "boss", "taller"}));
    CHECK((words("(\"quoted\"), **bold** and 'single'!") ==
           std::vector<std::string>{"quoted", "bold", "and", "single"}));
    CHECK((words("\xE2\x80\x9Csmart\xE2\x80\x9D quotes\xE2\x80\xA6") ==
           std::vector<std::string>{"smart", "quotes"}));
    // Code and markup are never checked.
    CHECK((words("use `fooo_bar` here") == std::vector<std::string>{"use", "here"}));
    CHECK((words("see\n```\nnot checkd\n```\ndone") == std::vector<std::string>{"see", "done"}));
    // Paths, URLs, identifiers, numbers, commands, mentions, file names.
    CHECK(words("/compact src/main.cpp https://x.io a_b @bob #12 v2 10mm e.g. file.step").empty());
    // ALLCAPS, camelCase, mixed case and single letters.
    CHECK(words("PCB OpenCASCADE iPhone a I").empty());
    // Hyphenated words check per part; contractions stay whole.
    CHECK((words("well-known don't dogs'") == std::vector<std::string>{"well", "known", "don't", "dogs"}));
    CHECK((words("it\xE2\x80\x99s") == std::vector<std::string>{"it\xE2\x80\x99s"}));
    // Ranges index the original text.
    const std::string s = "  (hello)";
    const auto r = ui::copilot::spellCheckWords(s);
    CHECK(r.size() == 1 && r[0].begin == 3 && r[0].end == 8);
}

static SpellDictionary smallDict() {
    SpellDictionary d;
    d.addWordList("#tier 10\nthe\nthen\nthere\nhello\nit\nworld\nword\nwords\n"
                  "#tier 50\nworm\nwold\nsimulate\nsimulation\nsimple\n");
    return d;
}

static void testKnows() {
    const SpellDictionary d = smallDict();
    CHECK(d.knows("hello"));
    CHECK(d.knows("Hello"));
    CHECK(d.knows("HELLO"));
    CHECK(!d.knows("helo"));
    CHECK(d.knows("world's"));
    CHECK(d.knows("it\xE2\x80\x99s"));
    CHECK(d.knows("words'"));
    CHECK(!d.knows("wor's"));

    SpellDictionary t;
    t.addWordList("#tier 60\nfillet\n");
    t.addWordList("#tier 20\nfillet\nchamfer\n");
    // fillet moved up to tier 20, so it beats the shorter tier-30 words
    t.addWordList("#tier 30\nfill\nfilled\n");
    CHECK(t.complete("fil") == "fillet");
    t.addWord("Gusset", SpellDictionary::kUserTier);
    CHECK(t.knows("gusset"));
}

static void testSuggestions() {
    const SpellDictionary d = smallDict();
    const auto s = d.suggestions("wrold", 5);
    CHECK(!s.empty() && s[0] == "world");   // transpose, tier 10 beats wold (tier 50)
    const auto c = d.suggestions("Wrold", 3);
    CHECK(!c.empty() && c[0] == "World");
    // Distance 1 ranks before distance 2 regardless of tier.
    const auto t = d.suggestions("worl", 5);
    CHECK(t.size() >= 2 && (t[0] == "world" || t[0] == "word" || t[0] == "worm" || t[0] == "wold"));
    const auto far = d.suggestions("smiulaton", 3);
    CHECK(!far.empty() && far[0] == "simulation");
    CHECK(d.suggestions("zzzzzzzz", 3).empty());
}

static void testCompletion() {
    const SpellDictionary d = smallDict();
    WordCounts none;
    CHECK(ui::copilot::completionSuffix("sim", d, none) == "ple");      // shortest, same tier
    CHECK(ui::copilot::completionSuffix("si", d, none).empty());        // too short
    CHECK(ui::copilot::completionSuffix("the", d, none).empty());       // finished word
    CHECK(ui::copilot::completionSuffix("SIM", d, none) == "PLE");
    CHECK(ui::copilot::completionSuffix("s1m", d, none).empty());

    WordCounts seen;
    seen.addText("Run the simulation. The simulation diverged; check OpenCASCADE and OpenCASCADE.");
    CHECK(ui::copilot::completionSuffix("sim", d, seen) == "ulation");   // known + seen
    CHECK(ui::copilot::completionSuffix("Sim", d, seen) == "ulation");
    CHECK(ui::copilot::completionSuffix("OpenC", d, seen) == "ASCADE");  // own casing
    CHECK(ui::copilot::completionSuffix("openc", d, seen) == "ascade");
    WordCounts once;
    once.addText("simmulation");                                          // unknown, seen once
    CHECK(ui::copilot::completionSuffix("sim", d, once) == "ple");
}

static std::string slurp(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return {};
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

static void testShippedLists(const char* assetsDir) {
    const std::string dir = assetsDir;
    const std::string en = slurp(dir + "/dict/en_US.txt");
    const std::string eng = slurp(dir + "/dict/engineering.txt");
    if (en.empty() || eng.empty()) {
        std::printf("FAIL shipped word lists missing under %s/dict\n", assetsDir);
        ++g_failures;
        return;
    }
    SpellDictionary d;
    d.addWordList(en);
    d.addWordList(eng);
    for (const char* w : {"the", "extrude", "chamfer", "fillet", "toolpath", "don't", "it's",
                          "Bracket", "subassemblies", "coplanar"})
        CHECK(d.knows(w));
    for (const char* w : {"teh", "recieve", "chamfr", "extrdue"}) CHECK(!d.knows(w));
    const auto r = d.suggestions("recieve", 5);
    CHECK(!r.empty() && r[0] == "receive");
    const auto e = d.suggestions("extrdue", 5);
    CHECK(!e.empty() && e[0] == "extrude");
    WordCounts none;
    CHECK(ui::copilot::completionSuffix("toolp", d, none) == "ath");
}

int main(int argc, char** argv) {
    testTokenizer();
    testKnows();
    testSuggestions();
    testCompletion();
    if (argc > 1) testShippedLists(argv[1]);
    if (g_failures) {
        std::printf("%d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_spell_check: all passed\n");
    return 0;
}
