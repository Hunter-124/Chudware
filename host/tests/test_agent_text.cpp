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
//     U+2192 RIGHTWARDS ARROW drawn as notdef: the atlas table (ui/glyph_ranges.h) does not request
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
//   ✓ every codepoint the atlas loads survives the fold; its neighbours outside the table do not

#include "ui/copilot/agent_text.h"

#include <cstdio>
#include <map>
#include <string>

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

// The Settings → AI → Rules tab shows context-pack bodies straight off disk
// (services/agent/advisor/context_packs/*.md). Those are MODEL-facing engineering prose and use
// Greek notation on purpose, so the fold — not the source — is what makes them legible.
static void test_greek_engineering_notation() {
    // eps U+03B5 (snap_fit.md strain limit) — the case that must not silently vanish.
    CHECK_EQ(ui::foldToAtlasGlyphs("\xCE\xB5 < 0.002"), "eps < 0.002");
    // delta U+03B4 deflection, theta U+03B8 bend angle, rho U+03C1 density, alpha U+03B1 CTE,
    // pi U+03C0, sigma U+03C3, mu U+03BC, lambda U+03BB, omega U+03C9.
    CHECK_EQ(ui::foldToAtlasGlyphs("\xCE\xB4 \xCE\xB8 \xCF\x81 \xCE\xB1 \xCF\x80 "
                                   "\xCF\x83 \xCE\xBC \xCE\xBB \xCF\x89"),
             "delta theta rho alpha pi sigma mu lambda omega");
    // Final sigma U+03C2 reads as sigma too.
    CHECK_EQ(ui::foldToAtlasGlyphs("\xCF\x82"), "sigma");
    // Capitals: Delta U+0394 (dfm_pre_op.md / press_fit.md), Sigma U+03A3, Omega U+03A9.
    CHECK_EQ(ui::foldToAtlasGlyphs("\xCE\x94T \xCE\xA3 \xCE\xA9"), "DeltaT Sigma Omega");
    // U+2126 OHM SIGN is the electrical unit, not a letter.
    CHECK_EQ(ui::foldToAtlasGlyphs("470 \xE2\x84\xA6"), "470 ohm");
    // A real pack line: every byte ASCII afterwards, and no character was dropped to nothing.
    const std::string line = "strain \xCE\xB5 \xE2\x89\xA4 0.02, \xCE\xB4 = 1.5 \xC3\x97 t";
    const std::string out  = ui::foldToAtlasGlyphs(line);
    CHECK_EQ(out, "strain eps <= 0.02, delta = 1.5 \xC3\x97 t");   // x U+00D7 IS covered
    CHECK(out.find('?') == std::string::npos);
}

static void test_circled_capitals_spell_their_letter() {
    // drawing_standards.md: (P) U+24C5, (F) U+24BB, (C) U+24B8.
    CHECK_EQ(ui::foldToAtlasGlyphs("\xE2\x93\x85 \xE2\x92\xBB \xE2\x92\xB8"), "(P) (F) (C)");
    CHECK_EQ(ui::foldToAtlasGlyphs("\xE2\x92\xB6"), "(A)");   // U+24B6, range start
    CHECK_EQ(ui::foldToAtlasGlyphs("\xE2\x93\x8F"), "(Z)");   // U+24CF, range end
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

// ---- 3. the atlas edges, observed through the fold ----------------------------------------------

// ui/glyph_ranges.h is the table ui_theme.cpp loads. What the user sees is the fold's output, so
// pin that: every edge of the table passes through byte-for-byte, one past each edge does not.
static void test_atlas_edges_survive_or_fold() {
    // CADSymbols merged over the body face: ∥ ≡ ⊕ ⊙ ⊥ ◆ ◇ (∅ and ⌀ are covered above).
    const std::string cad = "\xE2\x88\xA5 \xE2\x89\xA1 \xE2\x8A\x95 \xE2\x8A\x99 \xE2\x8A\xA5 "
                            "\xE2\x97\x86 \xE2\x97\x87";
    CHECK_EQ(ui::foldToAtlasGlyphs(cad), cad);
    // Body-face range edges: U+00FF, U+2010, U+2027, U+2070, U+209F.
    const std::string body = "\xC3\xBF \xE2\x80\x90 \xE2\x80\xA7 \xE2\x81\xB0 \xE2\x82\x9F";
    CHECK_EQ(ui::foldToAtlasGlyphs(body), body);
    // One past an edge with no spelled-out fold: U+0100, U+200F, U+2028, U+20A0, U+2204,
    // U+2206, U+2301 — each would paint as notdef, so each becomes the '?' stand-in.
    CHECK_EQ(ui::foldToAtlasGlyphs("\xC4\x80 \xE2\x80\x8F \xE2\x80\xA8 \xE2\x82\xA0 "
                                   "\xE2\x88\x84 \xE2\x88\x86 \xE2\x8C\x81"),
             "? ? ? ? ? ? ?");
    // The geometric shapes either side of the merged diamonds fold to the bullet stand-in.
    CHECK_EQ(ui::foldToAtlasGlyphs("\xE2\x97\x85\xE2\x97\x88"), "**");
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
    test_greek_engineering_notation();
    test_circled_capitals_spell_their_letter();
    test_pure_ascii_is_byte_identical();
    test_malformed_utf8_survives();
    test_pipeline_order();
    test_atlas_edges_survive_or_fold();
    if (g_failures) {
        std::printf("test_agent_text: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_agent_text: all checks passed\n");
    return 0;
}
