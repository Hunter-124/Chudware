// test_agent_text.cpp — ctest for the copilot's agent-authored-text sanitizers
// (ui::substituteDocIds / ui::foldToAtlasGlyphs). Links the REAL implementation
// (src/ui/copilot/agent_text.cpp), not a copy of it — no GL context, no ImGui, no AppState.
//
// Regressions under test, both from one live session (2026-07-28):
//
//  1. The user opened a part off disk and asked the agent to pocket it. The copilot tool-call
//     card rendered the raw call arguments —
//         {"doc_id": "doc-cpp00000039", "ops": [...]}
//     — so the part's NAME was replaced, on screen, by an opaque engine id.
//  2. The same card's header read "DevicePocket ? MouthpieceSlot ?+2 more". Those '?' are
//     U+2192 RIGHTWARDS ARROW drawn as notdef: ui_theme.cpp uiGlyphRanges() does not request
//     the arrows block, and services/agent/agent_events.py joins tool-card targets with " -> ".
//
// Acceptance:
//   ✓ a single id in prose becomes `Name (id)`
//   ✓ an id inside a JSON string value is rewritten without breaking the quoting
//   ✓ two different ids in one string resolve independently
//   ✓ an unknown id (resolve -> "") is left byte-for-byte alone
//   ✓ a string with no match comes back unchanged
//   ✓ "doc-cpp00000039x" / "mydoc-cpp1" are NOT rewritten (whole-token match)
//   ✓ empty input
//   ✓ a second pass over already-rendered text does not nest the label
//   ✓ the live header string folds to something with no '?'
//   ✓ covered glyphs (· — … ⌀) survive untouched; emoji/CJK degrade to '?'
//   ✓ pure ASCII is byte-identical
//   ✓ malformed UTF-8 does not crash and yields valid UTF-8
//   ✓ the mirrored atlas range table has NOT drifted from ui_theme.cpp

#include "ui/copilot/agent_text.h"

#include <cstdio>
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <map>
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

#define CHECK_EQ(a, b)                                                           \
    do {                                                                         \
        std::string _a = (a);                                                    \
        std::string _b = (b);                                                    \
        if (_a != _b) {                                                          \
            std::printf("FAIL %s:%d: %s == %s\n  got: [%s]\n  want:[%s]\n",      \
                        __FILE__, __LINE__, #a, #b, _a.c_str(), _b.c_str());     \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

// ---- 1. doc-id substitution ----------------------------------------------------------------

// The tab table the GUI resolver reads: an id with no tab yields "".
static std::map<std::string, std::string> g_tabs;
static std::string resolve(const std::string& id) {
    auto it = g_tabs.find(id);
    return it == g_tabs.end() ? std::string() : it->second;
}

static void test_single_id_in_prose() {
    g_tabs = {{"doc-cpp00000039", "Mouthpiece"}};
    CHECK_EQ(ui::substituteDocIds("Adding a pocket to doc-cpp00000039 now.", resolve),
             "Adding a pocket to Mouthpiece (doc-cpp00000039) now.");
    CHECK_EQ(ui::substituteDocIds("target: doc-cpp00000039", resolve),   // trailing position
             "target: Mouthpiece (doc-cpp00000039)");
    CHECK_EQ(ui::substituteDocIds("doc-cpp00000039 rebuilt", resolve),   // leading position
             "Mouthpiece (doc-cpp00000039) rebuilt");
}

static void test_json_string_value_stays_quoted() {
    g_tabs = {{"doc-cpp00000039", "Mouthpiece"}};
    const std::string in  = "{\"doc_id\": \"doc-cpp00000039\", \"ops\": [{\"op\": \"pocket\"}]}";
    const std::string out = ui::substituteDocIds(in, resolve);
    CHECK_EQ(out, "{\"doc_id\": \"Mouthpiece (doc-cpp00000039)\", \"ops\": [{\"op\": \"pocket\"}]}");
    // Quoting intact: same quote count, no escape introduced, no stray brace.
    size_t qIn = 0, qOut = 0;
    for (char c : in) qIn += (c == '"');
    for (char c : out) qOut += (c == '"');
    CHECK(qIn == qOut);
    CHECK(out.find('\\') == std::string::npos);
    // The "doc_id" KEY (underscore spelling, no hex suffix) must survive untouched.
    CHECK(out.find("\"doc_id\"") != std::string::npos);
}

static void test_two_ids_resolve_independently() {
    g_tabs = {{"doc-cpp00000039", "Mouthpiece"}, {"doc_cpp0000003a", "Housing"}};
    CHECK_EQ(ui::substituteDocIds("copy doc-cpp00000039 into doc_cpp0000003a", resolve),
             "copy Mouthpiece (doc-cpp00000039) into Housing (doc_cpp0000003a)");
}

static void test_unknown_id_untouched() {
    g_tabs = {{"doc-cpp00000039", "Mouthpiece"}};
    // No tab for 0000003a: it stays a raw id, never filler like "a document" inside a JSON blob.
    const std::string in = "{\"doc_id\": \"doc-cpp0000003a\"}";
    CHECK_EQ(ui::substituteDocIds(in, resolve), in);
    CHECK_EQ(ui::substituteDocIds("doc-cpp00000039 vs doc-cpp0000003a", resolve),
             "Mouthpiece (doc-cpp00000039) vs doc-cpp0000003a");
    g_tabs.clear();
    CHECK_EQ(ui::substituteDocIds(in, resolve), in);
}

static void test_no_match_returned_unchanged() {
    g_tabs = {{"doc-cpp00000039", "Mouthpiece"}};
    const std::string in = "{\"name\": \"DevicePocket\", \"depth\": 72}";
    CHECK_EQ(ui::substituteDocIds(in, resolve), in);
    CHECK_EQ(ui::substituteDocIds("doc-cpp and doc_cpp", resolve), "doc-cpp and doc_cpp");
}

static void test_whole_token_only() {
    g_tabs = {{"doc-cpp00000039", "Mouthpiece"}, {"doc-cpp1", "Widget"}};
    CHECK_EQ(ui::substituteDocIds("doc-cpp00000039x", resolve), "doc-cpp00000039x");
    CHECK_EQ(ui::substituteDocIds("mydoc-cpp1", resolve), "mydoc-cpp1");
    CHECK_EQ(ui::substituteDocIds("part-doc-cpp1", resolve), "part-doc-cpp1");
    CHECK_EQ(ui::substituteDocIds("doc-cpp1_old", resolve), "doc-cpp1_old");
    CHECK_EQ(ui::substituteDocIds("[doc-cpp1]", resolve), "[Widget (doc-cpp1)]");
}

static void test_empty_input() {
    g_tabs = {{"doc-cpp00000039", "Mouthpiece"}};
    CHECK_EQ(ui::substituteDocIds("", resolve), "");
    CHECK_EQ(ui::substituteDocIds("doc-cpp", resolve), "doc-cpp");
    CHECK_EQ(ui::substituteDocIds("doc-cpp00000039",
                                  std::function<std::string(const std::string&)>()),
             "doc-cpp00000039");
    CHECK_EQ(ui::foldToAtlasGlyphs(""), "");
}

static void test_second_pass_does_not_nest() {
    g_tabs = {{"doc-cpp00000039", "Mouthpiece"}};
    const std::string once = ui::substituteDocIds("act on doc-cpp00000039", resolve);
    CHECK_EQ(ui::substituteDocIds(once, resolve), once);
    const std::string jsonOnce = ui::substituteDocIds("{\"doc_id\": \"doc-cpp00000039\"}", resolve);
    CHECK_EQ(ui::substituteDocIds(jsonOnce, resolve), jsonOnce);
}

// ---- 2. atlas glyph folding ------------------------------------------------------------------

static void test_live_arrow_header_has_no_notdef() {
    // The exact string from the user's screenshot (agent_events.py joins targets with " → "
    // and appends " → +N more"). It rendered as "DevicePocket ? MouthpieceSlot ?+2 more".
    const std::string live = "DevicePocket \xE2\x86\x92 MouthpieceSlot \xE2\x86\x92 +2 more";
    const std::string out  = ui::foldToAtlasGlyphs(live);
    CHECK_EQ(out, "DevicePocket -> MouthpieceSlot -> +2 more");
    CHECK(out.find('?') == std::string::npos);
    // Every byte is now ASCII, so every byte has a glyph.
    for (unsigned char c : out) CHECK(ui::atlasCovers(c));
}

static void test_covered_glyphs_survive() {
    // · U+00B7, — U+2014, … U+2026, ⌀ U+2300 (CADSymbols), ° U+00B0, Ø U+00D8, ∅ U+2205.
    const std::string in = "a \xC2\xB7 b \xE2\x80\x94 c \xE2\x80\xA6 \xE2\x8C\x80 90\xC2\xB0 "
                           "\xC3\x98 \xE2\x88\x85";
    CHECK_EQ(ui::foldToAtlasGlyphs(in), in);
}

static void test_uncovered_degrades_to_question_mark() {
    // Emoji (U+1F600, 4-byte) and CJK (U+4E2D, 3-byte) have no glyph and no sensible mapping.
    CHECK_EQ(ui::foldToAtlasGlyphs("ok \xF0\x9F\x98\x80 done"), "ok ? done");
    CHECK_EQ(ui::foldToAtlasGlyphs("\xE4\xB8\xAD\xE6\x96\x87"), "??");
    // …but the string around them is intact and the mapped symbols still map.
    CHECK_EQ(ui::foldToAtlasGlyphs("w \xE2\x89\xA5 5 \xE2\x89\xA4 9 \xE2\x89\xA0 0 "
                                   "\xE2\x89\x88 4 \xE2\x9C\x93 \xE2\x9C\x97 "
                                   "x \xE2\x88\x88 S \xE2\x87\x92 y \xE2\x86\x94 z "
                                   "\xE2\x88\x92 1 \xE2\x94\x80 \xE2\x94\x82"),
             "w >= 5 <= 9 != 0 ~ 4 ok x x in S => y <-> z - 1 - |");
}

static void test_pure_ascii_is_byte_identical() {
    const std::string in = "{\"doc_id\": \"doc-cpp00000039\"}\n\tplain ASCII ~!@#$%^&*()";
    CHECK_EQ(ui::foldToAtlasGlyphs(in), in);
}

static void test_malformed_utf8_survives() {
    // Lone continuation byte, truncated 3-byte sequence, stray 0xFF — none may crash, and the
    // output must be valid UTF-8 (here: pure ASCII).
    const std::string bad = std::string("a\x80"
                                        "b\xE2\x86"
                                        "c\xFF"
                                        "d");
    const std::string out = ui::foldToAtlasGlyphs(bad);
    for (unsigned char c : out) CHECK(c < 0x80);
    CHECK(out.find('a') != std::string::npos && out.find('d') != std::string::npos);
    // A valid sequence immediately after a bad byte still decodes.
    CHECK_EQ(ui::foldToAtlasGlyphs("\x80\xE2\x86\x92"), "?->");
}

static void test_pipeline_order() {
    // Documented order: substituteDocIds first (ids are ASCII), then foldToAtlasGlyphs, so a
    // resolved document NAME containing an uncovered codepoint is folded too.
    g_tabs = {{"doc-cpp00000039", "Bracket \xE2\x86\x92 v2"}};
    const std::string subbed = ui::substituteDocIds("edit doc-cpp00000039", resolve);
    CHECK_EQ(ui::foldToAtlasGlyphs(subbed), "edit Bracket -> v2 (doc-cpp00000039)");
}

// ---- 3. drift guard: the mirrored range table vs ui_theme.cpp ---------------------------------

// Parse the hex range pairs out of one `static const ImWchar* <fn>() { ... }` body.
static std::vector<unsigned> parseRanges(const std::string& src, const std::string& fn) {
    std::vector<unsigned> out;
    const size_t at = src.find("* " + fn + "()");
    if (at == std::string::npos) return out;
    const size_t open = src.find('{', at);
    const size_t close = src.find("};", open);
    if (open == std::string::npos || close == std::string::npos) return out;
    const std::string body = src.substr(open, close - open);
    for (size_t i = 0; i + 2 < body.size(); ++i) {
        if (body[i] != '0' || (body[i + 1] != 'x' && body[i + 1] != 'X')) continue;
        out.push_back((unsigned)std::strtoul(body.c_str() + i, nullptr, 16));
        i += 2;
        while (i < body.size() && std::isxdigit((unsigned char)body[i])) ++i;
    }
    return out;
}

static void test_atlas_ranges_have_not_drifted() {
#ifndef UI_THEME_CPP
    std::printf("FAIL: UI_THEME_CPP not defined\n");
    ++g_failures;
#else
    std::ifstream f(UI_THEME_CPP);
    if (!f) {
        std::printf("FAIL: cannot open %s\n", UI_THEME_CPP);
        ++g_failures;
        return;
    }
    std::ostringstream ss;
    ss << f.rdbuf();
    const std::string src = ss.str();

    std::vector<unsigned> want = parseRanges(src, "uiGlyphRanges");
    const std::vector<unsigned> cad = parseRanges(src, "cadSymbolRanges");
    want.insert(want.end(), cad.begin(), cad.end());
    CHECK(!want.empty());

    std::size_t pairs = 0;
    const unsigned* have = ui::atlasCoveredRanges(pairs);
    // ui_theme's tables are 0-terminated; ours holds only the pairs.
    while (!want.empty() && want.back() == 0) want.pop_back();

    if (want.size() != pairs * 2) {
        std::printf("FAIL: atlas range table drifted from ui_theme.cpp "
                    "(ui_theme has %zu values, agent_text.cpp mirrors %zu)\n",
                    want.size(), pairs * 2);
        ++g_failures;
        return;
    }
    for (std::size_t i = 0; i < want.size(); ++i) {
        if (want[i] != have[i]) {
            std::printf("FAIL: atlas range drift at index %zu: ui_theme.cpp 0x%04X vs "
                        "agent_text.cpp 0x%04X\n", i, want[i], have[i]);
            ++g_failures;
        }
    }
    // And the consequence the fold depends on: U+2192 is NOT covered, U+2300 IS.
    CHECK(!ui::atlasCovers(0x2192));
    CHECK(ui::atlasCovers(0x2300));
    CHECK(ui::atlasCovers(0x00B7));
#endif
}

int main() {
    test_single_id_in_prose();
    test_json_string_value_stays_quoted();
    test_two_ids_resolve_independently();
    test_unknown_id_untouched();
    test_no_match_returned_unchanged();
    test_whole_token_only();
    test_empty_input();
    test_second_pass_does_not_nest();
    test_live_arrow_header_has_no_notdef();
    test_covered_glyphs_survive();
    test_uncovered_degrades_to_question_mark();
    test_pure_ascii_is_byte_identical();
    test_malformed_utf8_survives();
    test_pipeline_order();
    test_atlas_ranges_have_not_drifted();
    if (g_failures) {
        std::printf("test_agent_text: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_agent_text: all checks passed\n");
    return 0;
}
