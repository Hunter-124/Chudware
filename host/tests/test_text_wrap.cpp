// test_text_wrap.cpp — ctest for the composer's hard-break bookkeeping and copied-selection
// normalization. Links the REAL implementation (src/ui/copilot/text_wrap.cpp), not a copy —
// no GL context, no ImGui, no AppState.
//
// Regression under test, live 2026-07-28: "alt+enter in the prompt input box doesn't add a
// new line". Two causes, one of them silent:
//   1. Only Shift was excluded from the submit chord, so Alt+Enter SENT the message (and the
//      widget had already inserted a '\n' behind it). Fixed in composer.cpp's submit guard.
//   2. Even with the submit suppressed the break did not survive: the composer word-wraps by
//      swapping ' ' <-> '\n' in place, and step one of that wrap flattens EVERY '\n' back to
//      a space. So the user's break vanished on their next keystroke. A '\n' carries no
//      marker saying who put it there, so the user's offsets are tracked here instead.
//
// Acceptance:
//   ✓ a typed newline is claimed as hard
//   ✓ typing before a hard break slides it right; typing after leaves it alone
//   ✓ deleting before it slides it left; deleting the break itself drops it
//   ✓ a deletion spanning the break drops it, and only it
//   ✓ a multi-line paste claims every break it brought
//   ✓ a same-length change (the wrapper's own swap) never invents or moves a break
//   ✓ an external rewrite (example chip, slash insert) re-seeds from the text
//   ✓ offsets stay sorted and unique
//   ✓ a first sighting seeds instead of shifting garbage
//   ✓ copied composer selections flatten synthetic wraps while preserving hard breaks and UTF-8

#include "ui/copilot/text_wrap.h"

#include <cstdio>
#include <string>
#include <vector>

using ui::copilot::WrapState;
using ui::copilot::isHardBreak;
using ui::copilot::normalizeComposerSelection;
using ui::copilot::noteEdit;
using ui::copilot::seedHardBreaks;

static int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

static std::string show(const std::vector<int>& v) {
    std::string s = "[";
    for (size_t i = 0; i < v.size(); ++i) s += (i ? "," : "") + std::to_string(v[i]);
    return s + "]";
}

#define CHECK_HARD(ws, ...)                                                      \
    do {                                                                         \
        const std::vector<int> _want{__VA_ARGS__};                               \
        if ((ws).hard != _want) {                                                \
            std::printf("FAIL %s:%d: hard breaks\n  got: %s\n  want:%s\n",       \
                        __FILE__, __LINE__, show((ws).hard).c_str(),             \
                        show(_want).c_str());                                    \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

// Apply one single-caret edit to `buf` the way ImGui does, then tell the tracker about it
// exactly as the CallbackEdit path in composer.cpp does: post-edit buffer, post-edit caret.
static void edit(WrapState& ws, std::string& buf, int at, int removeLen,
                 const std::string& insert) {
    buf.erase((size_t)at, (size_t)removeLen);
    buf.insert((size_t)at, insert);
    const int cursor = at + (int)insert.size();
    noteEdit(ws, buf.data(), (int)buf.size(), cursor);
}

int main() {
    // ---- a fresh buffer seeds instead of shifting an uninitialised baseline ----
    {
        WrapState ws;
        std::string buf = "hello world";
        CHECK(ws.lastLen == -1);
        noteEdit(ws, buf.data(), (int)buf.size(), 5);
        CHECK_HARD(ws);
        CHECK(ws.lastLen == 11);
    }

    // ---- Alt+Enter: the typed newline is claimed ----
    {
        WrapState ws;
        std::string buf = "abcdef";
        seedHardBreaks(ws, buf.data(), (int)buf.size());
        CHECK_HARD(ws);
        edit(ws, buf, 3, 0, "\n");           // caret was after "abc"
        CHECK(buf == "abc\ndef");
        CHECK_HARD(ws, 3);
        CHECK(isHardBreak(ws.hard, 3));
        CHECK(!isHardBreak(ws.hard, 2));
    }

    // ---- typing before it slides it right; typing after leaves it put ----
    {
        WrapState ws;
        std::string buf = "abc\ndef";
        seedHardBreaks(ws, buf.data(), (int)buf.size());
        CHECK_HARD(ws, 3);
        edit(ws, buf, 0, 0, "XY");           // "XYabc\ndef"
        CHECK_HARD(ws, 5);
        edit(ws, buf, 9, 0, "!");            // after the break: "XYabc\ndef!"
        CHECK_HARD(ws, 5);
        CHECK(buf[5] == '\n');
    }

    // ---- deleting before it slides it left ----
    {
        WrapState ws;
        std::string buf = "abc\ndef";
        seedHardBreaks(ws, buf.data(), (int)buf.size());
        edit(ws, buf, 0, 2, "");             // "c\ndef"
        CHECK_HARD(ws, 1);
        CHECK(buf[1] == '\n');
    }

    // ---- backspacing the break itself drops it ----
    {
        WrapState ws;
        std::string buf = "abc\ndef";
        seedHardBreaks(ws, buf.data(), (int)buf.size());
        edit(ws, buf, 3, 1, "");             // "abcdef"
        CHECK_HARD(ws);
    }

    // ---- a selection delete spanning one break of two drops ONLY that one ----
    {
        WrapState ws;
        std::string buf = "ab\ncd\nef";      // breaks at 2 and 5
        seedHardBreaks(ws, buf.data(), (int)buf.size());
        CHECK_HARD(ws, 2, 5);
        edit(ws, buf, 1, 3, "");             // remove "b\nc" -> "ad\nef", break 5 -> 2
        CHECK(buf == "ad\nef");
        CHECK_HARD(ws, 2);
        CHECK(buf[2] == '\n');
    }

    // ---- a multi-line paste claims every break it brought ----
    {
        WrapState ws;
        std::string buf = "start end";
        seedHardBreaks(ws, buf.data(), (int)buf.size());
        edit(ws, buf, 6, 0, "one\ntwo\n");   // "start one\ntwo\nend"
        CHECK(buf == "start one\ntwo\nend");
        CHECK_HARD(ws, 9, 13);
        CHECK(buf[9] == '\n' && buf[13] == '\n');
    }

    // ---- the wrapper's own same-length swap must not invent or move a break ----
    {
        WrapState ws;
        std::string buf = "aa bb\ncc dd";    // hard break at 5
        seedHardBreaks(ws, buf.data(), (int)buf.size());
        CHECK_HARD(ws, 5);
        buf[2] = '\n';                       // a SOFT break the wrapper just inserted
        noteEdit(ws, buf.data(), (int)buf.size(), 11);
        CHECK_HARD(ws, 5);
        CHECK(!isHardBreak(ws.hard, 2));     // stays the wrapper's to reflow
    }

    // ---- an external rewrite re-seeds: every newline in that text was deliberate ----
    {
        WrapState ws;
        std::string buf = "typed";
        seedHardBreaks(ws, buf.data(), (int)buf.size());
        CHECK_HARD(ws);
        buf = "line one\nline two\nline three";   // example chip / slash insert
        seedHardBreaks(ws, buf.data(), (int)buf.size());
        CHECK_HARD(ws, 8, 17);
        CHECK(ws.lastLen == (int)buf.size());
    }

    // ---- offsets stay sorted and unique across an interleaved session ----
    {
        WrapState ws;
        std::string buf;
        seedHardBreaks(ws, buf.data(), 0);
        edit(ws, buf, 0, 0, "hello");
        edit(ws, buf, 5, 0, "\n");
        edit(ws, buf, 0, 0, "\n");           // a break BEFORE an existing one
        CHECK(buf == "\nhello\n");
        CHECK_HARD(ws, 0, 6);
        for (size_t i = 1; i < ws.hard.size(); ++i) CHECK(ws.hard[i - 1] < ws.hard[i]);
    }

    // ---- whole selection removes every synthetic composer wrap ----
    {
        const std::string wrapped = "one\nline\nprompt";
        CHECK(normalizeComposerSelection(wrapped, {}, 0, (int)wrapped.size()) ==
              "one line prompt");
    }

    // ---- user-authored hard newlines survive beside synthetic wraps ----
    {
        const std::string wrapped = "first\nline\nsecond\npart";
        CHECK(normalizeComposerSelection(wrapped, {10}, 0, (int)wrapped.size()) ==
              "first line\nsecond part");
    }

    // ---- a partial byte range is normalized without expanding to logical lines ----
    {
        const std::string wrapped = "alpha\nbeta\ngamma";
        CHECK(normalizeComposerSelection(wrapped, {}, 3, 12) == "ha beta g");
    }

    // ---- reversed and out-of-range bounds are ordered and clamped ----
    {
        const std::string wrapped = "alpha\nbeta";
        CHECK(normalizeComposerSelection(wrapped, {}, 999, -999) == "alpha beta");
        CHECK(normalizeComposerSelection(wrapped, {}, -20, 5) == "alpha");
    }

    // ---- UTF-8 bytes remain stable around replaced ASCII wrap bytes ----
    {
        const std::string wrapped = "café\ncrème\nbrûlée";
        const std::string expected = "café crème brûlée";
        const int end = (int)expected.find(" brûlée");
        CHECK(normalizeComposerSelection(wrapped, {}, 0, end) ==
              expected.substr(0, (size_t)end));
        CHECK(normalizeComposerSelection(wrapped, {}, 0, (int)wrapped.size()) == expected);
    }

    if (g_failures == 0) std::printf("test_text_wrap: all checks passed\n");
    else std::printf("test_text_wrap: %d FAILURE(S)\n", g_failures);
    return g_failures == 0 ? 0 : 1;
}
