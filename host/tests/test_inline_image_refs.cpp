// test_inline_image_refs.cpp — ctest for the inline-image marker helpers the Sessions list leans
// on. Header-only (ui/copilot/inline_image_refs.h): no GL context, no ImGui, no AppState.
//
// Regression under test: session titles derived from a first prompt kept the raw "[Image #1]"
// marker, so the Sessions list showed "[Image #1]modify this part so it…" and the marker ate
// the title's length budget. stripInlineImageMarkers() is what the title derivation and the
// list now share.
//
// Acceptance:
//   ✓ a marker glued to text is removed and leaves one clean title
//   ✓ any number of markers, in any position, are removed whatever their N
//   ✓ blanks the markers leave behind collapse and the ends are trimmed
//   ✓ text that only looks like a marker (no number, no closing bracket) is left alone
//   ✓ a title that was only markers comes back empty (callers fall back to the slot name)
//   ✓ numbered refs against a real roster are unchanged by the new helper

#include "ui/copilot/inline_image_refs.h"

#include <cstdio>
#include <string>

using ui::copilot::inlineImageMarker;
using ui::copilot::inlineImageRefs;
using ui::copilot::stripInlineImageMarkers;

static int g_failures = 0;

#define CHECK_EQ(got, want)                                                          \
    do {                                                                             \
        const std::string _g = (got), _w = (want);                                   \
        if (_g != _w) {                                                              \
            std::printf("FAIL %s:%d\n  got: \"%s\"\n  want:\"%s\"\n", __FILE__,      \
                        __LINE__, _g.c_str(), _w.c_str());                           \
            ++g_failures;                                                            \
        }                                                                            \
    } while (0)

int main() {
    CHECK_EQ(stripInlineImageMarkers("[Image #1]modify this part so it"),
             "modify this part so it");
    CHECK_EQ(stripInlineImageMarkers("modify [Image #2] this [Image #10] part"),
             "modify this part");
    CHECK_EQ(stripInlineImageMarkers("  [Image #3]   [Image #4]  fit\tthe\nbracket  "),
             "fit the bracket");
    CHECK_EQ(stripInlineImageMarkers("[Image #1]"), "");
    CHECK_EQ(stripInlineImageMarkers("[Image #]x [Image 1] [Image #1"), "[Image #]x [Image 1] [Image #1");
    CHECK_EQ(stripInlineImageMarkers("plain title"), "plain title");
    CHECK_EQ(stripInlineImageMarkers(""), "");

    // The roster-aware parser is untouched: it still resolves only numbers the roster holds.
    const std::string text = "A " + inlineImageMarker(0) + " B " + inlineImageMarker(4);
    const auto refs = inlineImageRefs(text, 2);
    if (refs.size() != 1 || refs[0].image != 0) {
        std::printf("FAIL: inlineImageRefs resolved %zu refs\n", refs.size());
        ++g_failures;
    }

    if (g_failures) {
        std::printf("%d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_inline_image_refs: all checks passed\n");
    return 0;
}
