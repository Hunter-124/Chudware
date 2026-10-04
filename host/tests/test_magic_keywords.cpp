// test_magic_keywords.cpp — ctest for the copilot's magic-keyword matcher
// (src/ui/copilot/magic_keywords.cpp). No GL, ImGui or AppState.
//
// Acceptance (mirrors omp's tui magic-keyword rules):
//   - a standalone keyword matches; sentence punctuation and quotes may touch it
//   - embedded occurrences miss: suffix/prefix letters, file extension, call paren, `::`,
//     path segment, hyphenated word
//   - inline code spans, fenced code blocks and HTML/XML sections never match
//   - matching is case-sensitive (uppercase misses)
//   - several keywords in one string come back sorted with the right table index
//   - hueAt sweeps from -> to and wraps past 360

#include "ui/copilot/magic_keywords.h"

#include <cmath>
#include <cstdio>
#include <string>

static int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

using ui::copilot::KeywordRange;
using ui::copilot::magicKeywordRanges;

static int indexOf(const char* word) {
    const auto& t = ui::copilot::magicKeywords();
    for (size_t i = 0; i < t.size(); ++i)
        if (t[i].word == word) return (int)i;
    return -1;
}

// Exactly one hit for `word` at byte `at`.
static bool hitOnce(const std::string& s, const char* word, size_t at) {
    const auto r = magicKeywordRanges(s);
    return r.size() == 1 && r[0].begin == at && r[0].end == at + std::string(word).size() &&
           r[0].keyword == indexOf(word);
}

static bool miss(const std::string& s) { return magicKeywordRanges(s).empty(); }

int main() {
    // Table matches omp.
    CHECK(ui::copilot::magicKeywords().size() == 7);
    CHECK(indexOf("orchestrate") >= 0 && indexOf("tokenmaxxing") >= 0);

    // Standalone + punctuation-adjacent hits.
    CHECK(hitOnce("orchestrate", "orchestrate", 0));
    CHECK(hitOnce("please orchestrate this", "orchestrate", 7));
    CHECK(hitOnce("orchestrate, then build", "orchestrate", 0));
    CHECK(hitOnce("say \"orchestrate\" now", "orchestrate", 5));
    CHECK(hitOnce("(orchestrate)", "orchestrate", 1));
    CHECK(hitOnce("done. orchestrate.", "orchestrate", 6));
    CHECK(hitOnce("ultrathink?", "ultrathink", 0));
    CHECK(hitOnce("line one\norchestrate\nline three", "orchestrate", 9));
    // Smart quotes are punctuation, not letters.
    CHECK(hitOnce("\xe2\x80\x9corchestrate\xe2\x80\x9d", "orchestrate", 3));

    // Embedded misses.
    CHECK(miss("orchestrated"));
    CHECK(miss("reorchestrate"));
    CHECK(miss("orchestrate.md"));
    CHECK(miss("orchestrate()"));
    CHECK(miss("foo::orchestrate"));
    CHECK(miss("path/orchestrate"));
    CHECK(miss("orchestrate/path"));
    CHECK(miss("my-orchestrate"));
    CHECK(miss("orchestrate-me"));
    CHECK(miss("x_orchestrate"));
    CHECK(miss("v2.orchestrate"));
    CHECK(miss("\\orchestrate"));
    CHECK(miss("caf\xc3\xa9orchestrate"));   // non-ASCII letter glues it into a word

    // Code and markup never match.
    CHECK(miss("run `orchestrate` here"));
    CHECK(miss("run ``a orchestrate b`` here"));
    CHECK(miss("```\norchestrate\n```"));
    CHECK(miss("text\n```bash\nultrathink now\n```\n"));
    CHECK(miss("~~~\norchestrate\n~~~"));
    CHECK(miss("<note>orchestrate</note>"));
    CHECK(miss("<!-- orchestrate -->"));
    // ...but prose next to them still does.
    CHECK(hitOnce("`code` orchestrate", "orchestrate", 7));
    CHECK(hitOnce("```\nx\n```\norchestrate", "orchestrate", 10));
    // An unmatched backtick is literal text, not a span.
    CHECK(hitOnce("a ` orchestrate", "orchestrate", 4));

    // Case-sensitive.
    CHECK(miss("Orchestrate"));
    CHECK(miss("ORCHESTRATE"));
    CHECK(miss("UltraThink"));

    // Several keywords, sorted by position.
    {
        const std::string s = "ultrathink and orchestrate, then autobot; jevify.";
        const auto r = magicKeywordRanges(s);
        CHECK(r.size() == 4);
        if (r.size() == 4) {
            CHECK(r[0].keyword == indexOf("ultrathink") && r[0].begin == 0);
            CHECK(r[1].keyword == indexOf("orchestrate") && r[1].begin == 15);
            CHECK(r[2].keyword == indexOf("autobot") && r[2].begin == 33);
            CHECK(r[3].keyword == indexOf("jevify") && r[3].begin == 42);
        }
        // Same word twice.
        const auto r2 = magicKeywordRanges("orchestrate orchestrate");
        CHECK(r2.size() == 2 && r2[0].begin == 0 && r2[1].begin == 12);
    }

    // hueAt: endpoints and wrap.
    {
        const int orch = indexOf("orchestrate");
        CHECK(std::fabs(ui::copilot::hueAt(orch, 0.0f) - 150.0f) < 1e-3f);
        CHECK(std::fabs(ui::copilot::hueAt(orch, 1.0f) - 280.0f) < 1e-3f);
        const int jev = indexOf("jevify");   // 300 -> 420 wraps to 60
        CHECK(std::fabs(ui::copilot::hueAt(jev, 1.0f) - 60.0f) < 1e-3f);
        const int tok = indexOf("tokenmaxxing");   // 410 -> 340
        CHECK(std::fabs(ui::copilot::hueAt(tok, 0.0f) - 50.0f) < 1e-3f);
        const int goon = indexOf("ultragoon");     // 330 -> 270 runs backwards
        CHECK(std::fabs(ui::copilot::hueAt(goon, 0.5f) - 300.0f) < 1e-3f);
        CHECK(ui::copilot::hueAt(-1, 0.5f) == 0.0f);
        CHECK(std::fabs(ui::copilot::hueAt(orch, 2.0f) - 280.0f) < 1e-3f);   // clamped
    }

    if (g_failures) {
        std::printf("test_magic_keywords: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_magic_keywords: OK\n");
    return 0;
}
