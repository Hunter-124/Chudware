// test_tool_phrase.cpp — ctest for the copilot's tool-call phrasing
// (ui::toolPhrase / ui::toolGroupTitle / ui::humanTarget). Links the REAL implementation
// (src/ui/copilot/tool_phrase.cpp), not a copy of it — no GL context, no ImGui, no AppState.
//
// Regressions under test, all from one live session (2026-07-28,
// ~/.local/share/Chudware/cache/runs/run-138660919763973):
//
//  1. The transcript printed MCP plumbing. A card read "Doing · add_features" with a dim
//     "Lug1 (box) -> Lug2 (box)" under it — a raw tool symbol and a raw target line, neither
//     written for a person.
//  2. The agent's repair loop stacked one byte-identical card per call, so a three-call fixup
//     read as three separate identical events and the ONE call that failed was invisible in the
//     run of them.
//  3. That same run created three documents where the user asked for one: new_part("mudbucket")
//     was called twice. A grouping rule that folded those into "Creating a part × 2" would hide
//     exactly the bug we are chasing, so document-creating calls must never fold.
//
// Acceptance:
//   ✓ table tools get their sentence and their mutating flag
//   ✓ an unknown tool falls back to "Foo bar baz", never a bare snake_case symbol
//   ✓ a FAILED call always gets an empty groupKey
//   ✓ ask_user / delegate / new_part / new_assembly / import_part never group
//   ✓ the render family shares "render" and the measure family shares "measure"
//   ✓ add_features groups only with add_features
//   ✓ toolGroupTitle renders the × sign, and a count of 1 adds nothing
//   ✓ humanTarget collapses whitespace, unwraps a quoted string and a one-key object,
//     leaves a two-key object alone, and yields "" for empty/whitespace input
//   ✓ humanTarget's ellipsis lands on a CODEPOINT boundary: the result of truncating a string
//     full of U+2192 arrows is still valid UTF-8

#include "ui/copilot/tool_phrase.h"

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

// ---- 1. the phrasing table --------------------------------------------------------------------

static void test_known_tools_get_sentences() {
    // The three symbols that were on screen raw during the live run.
    CHECK_EQ(ui::toolPhrase("add_features", "", true).title, "Building geometry");
    CHECK_EQ(ui::toolPhrase("render_part", "", true).title, "Rendering the part");
    CHECK_EQ(ui::toolPhrase("fs_list", "", true).title, "Reading the project folder");

    // A spread across the rest of the table.
    CHECK_EQ(ui::toolPhrase("new_part", "", true).title, "Creating a part");
    CHECK_EQ(ui::toolPhrase("new_assembly", "", true).title, "Creating an assembly");
    CHECK_EQ(ui::toolPhrase("import_part", "", true).title, "Importing a part");
    CHECK_EQ(ui::toolPhrase("save_part", "", true).title, "Saving the part");
    CHECK_EQ(ui::toolPhrase("export_part", "", true).title, "Exporting the part");
    CHECK_EQ(ui::toolPhrase("close_part", "", true).title, "Closing the part");
    CHECK_EQ(ui::toolPhrase("render_views", "", true).title, "Rendering views");
    CHECK_EQ(ui::toolPhrase("contact_sheet", "", true).title, "Building a contact sheet");
    CHECK_EQ(ui::toolPhrase("doc_sketch_contact_sheet", "", true).title, "Rendering sketches");
    CHECK_EQ(ui::toolPhrase("inspect_part", "", true).title, "Inspecting the part");
    CHECK_EQ(ui::toolPhrase("inspect_feature", "", true).title, "Inspecting a feature");
    CHECK_EQ(ui::toolPhrase("look_around", "", true).title, "Looking around the part");
    CHECK_EQ(ui::toolPhrase("describe_part", "", true).title, "Reading the part");
    CHECK_EQ(ui::toolPhrase("get_part", "", true).title, "Reading the part");
    CHECK_EQ(ui::toolPhrase("list_parts", "", true).title, "Listing open parts");
    CHECK_EQ(ui::toolPhrase("measure", "", true).title, "Measuring");
    CHECK_EQ(ui::toolPhrase("measure_between", "", true).title, "Measuring");
    CHECK_EQ(ui::toolPhrase("measure_part", "", true).title, "Measuring");
    CHECK_EQ(ui::toolPhrase("check_interference", "", true).title, "Checking interference");
    CHECK_EQ(ui::toolPhrase("verify_battery", "", true).title, "Running verification");
    CHECK_EQ(ui::toolPhrase("design_review", "", true).title, "Reviewing the design");
    CHECK_EQ(ui::toolPhrase("check_sketch", "", true).title, "Checking the sketch");
    CHECK_EQ(ui::toolPhrase("interface_check", "", true).title, "Checking interfaces");
    CHECK_EQ(ui::toolPhrase("fs_glob", "", true).title, "Searching the project folder");
    CHECK_EQ(ui::toolPhrase("fs_read", "", true).title, "Reading a file");
    CHECK_EQ(ui::toolPhrase("fs_write", "", true).title, "Writing a file");
    CHECK_EQ(ui::toolPhrase("fs_edit", "", true).title, "Editing a file");
    CHECK_EQ(ui::toolPhrase("cad_guide", "", true).title, "Consulting the modeling guide");
    CHECK_EQ(ui::toolPhrase("enable_tools", "", true).title, "Enabling tools");
    CHECK_EQ(ui::toolPhrase("ledger_note", "", true).title, "Recording a decision");
    CHECK_EQ(ui::toolPhrase("ask_user", "", true).title, "Asking you a question");
    CHECK_EQ(ui::toolPhrase("delegate", "", true).title, "Delegating to a specialist");
    CHECK_EQ(ui::toolPhrase("undo", "", true).title, "Undoing");
    CHECK_EQ(ui::toolPhrase("redo", "", true).title, "Redoing");
    CHECK_EQ(ui::toolPhrase("plm_rollback", "", true).title, "Rolling back");
    CHECK_EQ(ui::toolPhrase("make_drawing", "", true).title, "Creating a drawing");
    CHECK_EQ(ui::toolPhrase("export_drawing", "", true).title, "Exporting the drawing");
    CHECK_EQ(ui::toolPhrase("add_datum", "", true).title, "Adding a datum");
    CHECK_EQ(ui::toolPhrase("add_dimension", "", true).title, "Adding a dimension");
}

static void test_mutating_flags() {
    // Changes the model or the disk.
    CHECK(ui::toolPhrase("add_features", "", true).mutating);
    CHECK(ui::toolPhrase("new_part", "", true).mutating);
    CHECK(ui::toolPhrase("new_assembly", "", true).mutating);
    CHECK(ui::toolPhrase("import_part", "", true).mutating);
    CHECK(ui::toolPhrase("save_part", "", true).mutating);
    CHECK(ui::toolPhrase("export_part", "", true).mutating);
    CHECK(ui::toolPhrase("close_part", "", true).mutating);
    CHECK(ui::toolPhrase("fs_write", "", true).mutating);
    CHECK(ui::toolPhrase("fs_edit", "", true).mutating);
    CHECK(ui::toolPhrase("ledger_note", "", true).mutating);
    CHECK(ui::toolPhrase("delegate", "", true).mutating);
    CHECK(ui::toolPhrase("undo", "", true).mutating);
    CHECK(ui::toolPhrase("redo", "", true).mutating);
    CHECK(ui::toolPhrase("plm_rollback", "", true).mutating);
    CHECK(ui::toolPhrase("make_drawing", "", true).mutating);
    CHECK(ui::toolPhrase("export_drawing", "", true).mutating);
    CHECK(ui::toolPhrase("add_datum", "", true).mutating);
    CHECK(ui::toolPhrase("add_dimension", "", true).mutating);

    // Leaves nothing behind — expensive is not the same as mutating.
    CHECK(!ui::toolPhrase("render_part", "", true).mutating);
    CHECK(!ui::toolPhrase("render_views", "", true).mutating);
    CHECK(!ui::toolPhrase("contact_sheet", "", true).mutating);
    CHECK(!ui::toolPhrase("doc_sketch_contact_sheet", "", true).mutating);
    CHECK(!ui::toolPhrase("inspect_part", "", true).mutating);
    CHECK(!ui::toolPhrase("inspect_feature", "", true).mutating);
    CHECK(!ui::toolPhrase("look_around", "", true).mutating);
    CHECK(!ui::toolPhrase("describe_part", "", true).mutating);
    CHECK(!ui::toolPhrase("get_part", "", true).mutating);
    CHECK(!ui::toolPhrase("list_parts", "", true).mutating);
    CHECK(!ui::toolPhrase("measure", "", true).mutating);
    CHECK(!ui::toolPhrase("measure_between", "", true).mutating);
    CHECK(!ui::toolPhrase("measure_part", "", true).mutating);
    CHECK(!ui::toolPhrase("check_interference", "", true).mutating);
    CHECK(!ui::toolPhrase("verify_battery", "", true).mutating);
    CHECK(!ui::toolPhrase("design_review", "", true).mutating);
    CHECK(!ui::toolPhrase("check_sketch", "", true).mutating);
    CHECK(!ui::toolPhrase("interface_check", "", true).mutating);
    CHECK(!ui::toolPhrase("fs_list", "", true).mutating);
    CHECK(!ui::toolPhrase("fs_glob", "", true).mutating);
    CHECK(!ui::toolPhrase("fs_read", "", true).mutating);
    CHECK(!ui::toolPhrase("cad_guide", "", true).mutating);
    CHECK(!ui::toolPhrase("enable_tools", "", true).mutating);
    CHECK(!ui::toolPhrase("ask_user", "", true).mutating);
}

static void test_unknown_tool_falls_back() {
    CHECK_EQ(ui::toolPhrase("foo_bar_baz", "", true).title, "Foo bar baz");
    CHECK_EQ(ui::toolPhrase("frobnicate", "", true).title, "Frobnicate");
    CHECK_EQ(ui::toolPhrase("Already_Capital", "", true).title, "Already Capital");
    // Never a bare symbol: no underscore survives.
    CHECK(ui::toolPhrase("some_new_mcp_tool", "", true).title.find('_') == std::string::npos);
    // We cannot know, so the lighter card: an unknown tool claims nothing.
    CHECK(!ui::toolPhrase("foo_bar_baz", "", true).mutating);
    // Degenerate input must not crash or index past the end.
    CHECK_EQ(ui::toolPhrase("", "", true).title, "");
    CHECK_EQ(ui::toolPhrase("_", "", true).title, " ");
}

// ---- 2. grouping --------------------------------------------------------------------------

static void test_failed_calls_never_group() {
    // Regression 2: a failure folded into a run of successes is a failure nobody sees.
    CHECK_EQ(ui::toolPhrase("add_features", "Lug1", false).groupKey, "");
    CHECK_EQ(ui::toolPhrase("render_part", "", false).groupKey, "");
    CHECK_EQ(ui::toolPhrase("fs_read", "", false).groupKey, "");
    CHECK_EQ(ui::toolPhrase("foo_bar_baz", "", false).groupKey, "");
    // …while the same call, succeeding, does group.
    CHECK_EQ(ui::toolPhrase("add_features", "Lug1", true).groupKey, "add_features");
}

static void test_document_and_user_calls_never_group() {
    // Regression 3: two new_part("mudbucket") calls must stay two visible rows.
    CHECK_EQ(ui::toolPhrase("new_part", "mudbucket", true).groupKey, "");
    CHECK_EQ(ui::toolPhrase("new_assembly", "rig", true).groupKey, "");
    CHECK_EQ(ui::toolPhrase("import_part", "mudbucket.step", true).groupKey, "");
    CHECK_EQ(ui::toolPhrase("ask_user", "which bore?", true).groupKey, "");
    CHECK_EQ(ui::toolPhrase("delegate", "sketcher", true).groupKey, "");
}

static void test_render_and_measure_families_share_a_key() {
    CHECK_EQ(ui::toolPhrase("render_part", "", true).groupKey, "render");
    CHECK_EQ(ui::toolPhrase("render_views", "", true).groupKey, "render");
    CHECK_EQ(ui::toolPhrase("contact_sheet", "", true).groupKey, "render");
    CHECK_EQ(ui::toolPhrase("doc_sketch_contact_sheet", "", true).groupKey, "render");

    CHECK_EQ(ui::toolPhrase("measure", "", true).groupKey, "measure");
    CHECK_EQ(ui::toolPhrase("measure_between", "", true).groupKey, "measure");
    CHECK_EQ(ui::toolPhrase("measure_part", "", true).groupKey, "measure");

    // Three back-to-back renders of the same part are one row.
    CHECK(ui::toolPhrase("render_part", "mudbucket", true).groupKey ==
          ui::toolPhrase("render_part", "mudbucket", true).groupKey);
}

static void test_add_features_groups_only_with_itself() {
    const std::string add = ui::toolPhrase("add_features", "", true).groupKey;
    CHECK_EQ(add, "add_features");
    CHECK(add != ui::toolPhrase("render_part", "", true).groupKey);
    CHECK(add != ui::toolPhrase("save_part", "", true).groupKey);
    CHECK(add != ui::toolPhrase("describe_part", "", true).groupKey);
    // describe_part / get_part say the same sentence but are still different calls.
    CHECK(ui::toolPhrase("describe_part", "", true).groupKey !=
          ui::toolPhrase("get_part", "", true).groupKey);
    // An unknown tool groups under its own symbol.
    CHECK_EQ(ui::toolPhrase("foo_bar_baz", "", true).groupKey, "foo_bar_baz");
}

// ---- 3. toolGroupTitle ------------------------------------------------------------------------

static void test_group_title_formatting() {
    CHECK_EQ(ui::toolGroupTitle("add_features", 3), "Building geometry \xc3\x97 3");
    CHECK_EQ(ui::toolGroupTitle("render_part", 12), "Rendering the part \xc3\x97 12");
    CHECK_EQ(ui::toolGroupTitle("foo_bar_baz", 2), "Foo bar baz \xc3\x97 2");
    // "× 1" is noise; so is "× 0".
    CHECK_EQ(ui::toolGroupTitle("add_features", 1), "Building geometry");
    CHECK_EQ(ui::toolGroupTitle("add_features", 0), "Building geometry");
    // The multiply sign is U+00D7, not an ASCII 'x'.
    CHECK(ui::toolGroupTitle("add_features", 3).find('x') == std::string::npos);
}

// ---- 4. humanTarget ---------------------------------------------------------------------------

static void test_target_whitespace_collapse() {
    CHECK_EQ(ui::humanTarget("  Lug1   (box)\n\t-> Lug2  "), "Lug1 (box) -> Lug2");
    CHECK_EQ(ui::humanTarget("\n\n mudbucket \n"), "mudbucket");
    CHECK_EQ(ui::humanTarget("already clean"), "already clean");
}

static void test_target_empty() {
    CHECK_EQ(ui::humanTarget(""), "");
    CHECK_EQ(ui::humanTarget("   "), "");
    CHECK_EQ(ui::humanTarget("\n\t \r"), "");
}

static void test_target_quoted_string_unwrap() {
    CHECK_EQ(ui::humanTarget("\"mudbucket\""), "mudbucket");
    CHECK_EQ(ui::humanTarget("  \"mudbucket\"  "), "mudbucket");
    CHECK_EQ(ui::humanTarget("\"say \\\"hi\\\"\""), "say \"hi\"");
    // Not a lone string — two of them side by side stay as they are.
    CHECK_EQ(ui::humanTarget("\"a\" \"b\""), "\"a\" \"b\"");
    // Unterminated quoting is left alone rather than eaten.
    CHECK_EQ(ui::humanTarget("\"mudbucket"), "\"mudbucket");
}

static void test_target_single_key_object_unwrap() {
    CHECK_EQ(ui::humanTarget("{\"doc_id\": \"mudbucket\"}"), "mudbucket");
    CHECK_EQ(ui::humanTarget("  {\"doc_id\":\"mudbucket\"}  "), "mudbucket");
    CHECK_EQ(ui::humanTarget("{\"count\": 3}"), "3");
    CHECK_EQ(ui::humanTarget("{\"path\": \"Projects/Testing/mudbucket.step\"}"),
             "Projects/Testing/mudbucket.step");
    // An empty object has no value to show.
    CHECK_EQ(ui::humanTarget("{}"), "{}");
}

static void test_target_two_key_object_left_intact() {
    // The raw args drawer handles these; picking one value would silently drop the rest.
    CHECK_EQ(ui::humanTarget("{\"doc_id\": \"mudbucket\", \"ops\": 2}"),
             "{\"doc_id\": \"mudbucket\", \"ops\": 2}");
    CHECK_EQ(ui::humanTarget("{\"a\":1,\"b\":2}"), "{\"a\":1,\"b\":2}");
    // A comma nested INSIDE the single value is not a second key.
    CHECK_EQ(ui::humanTarget("{\"ops\": [1,2,3]}"), "[1,2,3]");
    CHECK_EQ(ui::humanTarget("{\"name\": \"a,b\"}"), "a,b");
}

// Walk *s* as UTF-8; false on a truncated or malformed sequence. Byte-based truncation of a
// string full of U+2192 would leave a lead byte with no continuation and fail here.
static bool isValidUtf8(const std::string& s) {
    std::size_t i = 0;
    while (i < s.size()) {
        unsigned char c = (unsigned char)s[i];
        std::size_t extra = 0;
        if (c < 0x80)             extra = 0;
        else if ((c & 0xE0) == 0xC0) extra = 1;
        else if ((c & 0xF0) == 0xE0) extra = 2;
        else if ((c & 0xF8) == 0xF0) extra = 3;
        else return false;  // continuation byte or invalid lead in lead position
        for (std::size_t k = 1; k <= extra; ++k) {
            if (i + k >= s.size()) return false;
            if (((unsigned char)s[i + k] & 0xC0) != 0x80) return false;
        }
        i += extra + 1;
    }
    return true;
}

static void test_target_ellipsis_on_codepoint_boundary() {
    // The harness joins tool targets with a real U+2192 arrow, so a byte-based cut lands inside
    // a 3-byte sequence and emits invalid UTF-8.
    const std::string arrows = "Lug1 \xe2\x86\x92 Lug2 \xe2\x86\x92 Lug3";
    CHECK(isValidUtf8(arrows));  // the fixture itself is sane

    // 18 codepoints, 24 bytes: a limit above the codepoint count must not truncate.
    CHECK_EQ(ui::humanTarget(arrows, 18), arrows);
    CHECK_EQ(ui::humanTarget(arrows, 90), arrows);

    // Cutting at 6 codepoints lands exactly after the first arrow. Byte index 6 would sit in the
    // arrow's middle.
    const std::string cut6 = ui::humanTarget(arrows, 6);
    CHECK_EQ(cut6, "Lug1 \xe2\x86\x92\xe2\x80\xa6");
    CHECK(isValidUtf8(cut6));

    // Every limit across the string keeps the result valid UTF-8 and never splits an arrow.
    for (std::size_t n = 0; n <= 20; ++n) {
        const std::string got = ui::humanTarget(arrows, n);
        if (!isValidUtf8(got)) {
            std::printf("FAIL %s:%d: humanTarget(arrows, %zu) is not valid UTF-8: [%s]\n",
                        __FILE__, __LINE__, n, got.c_str());
            ++g_failures;
        }
    }

    // ASCII truncation still appends the ellipsis rather than cutting silently.
    CHECK_EQ(ui::humanTarget("abcdefghij", 4), "abcd\xe2\x80\xa6");
    CHECK_EQ(ui::humanTarget("abcd", 4), "abcd");
}

static void test_phrase_carries_cleaned_detail() {
    // The whole point: the card's second line is the cleaned target, not the raw envelope.
    const ui::ToolPhrase p = ui::toolPhrase("describe_part", "{\"doc_id\": \"mudbucket\"}", true);
    CHECK_EQ(p.title, "Reading the part");
    CHECK_EQ(p.detail, "mudbucket");
    CHECK_EQ(ui::toolPhrase("save_part", "", true).detail, "");
}

int main() {
    test_known_tools_get_sentences();
    test_mutating_flags();
    test_unknown_tool_falls_back();
    test_failed_calls_never_group();
    test_document_and_user_calls_never_group();
    test_render_and_measure_families_share_a_key();
    test_add_features_groups_only_with_itself();
    test_group_title_formatting();
    test_target_whitespace_collapse();
    test_target_empty();
    test_target_quoted_string_unwrap();
    test_target_single_key_object_unwrap();
    test_target_two_key_object_left_intact();
    test_target_ellipsis_on_codepoint_boundary();
    test_phrase_carries_cleaned_detail();
    if (g_failures) {
        std::printf("test_tool_phrase: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_tool_phrase: all checks passed\n");
    return 0;
}
