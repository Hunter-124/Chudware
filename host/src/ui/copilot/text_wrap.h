// Hard-break bookkeeping for the composer's hand-rolled word wrapper.
//
// ImGui's InputTextMultiline does not word-wrap, so the composer wraps the buffer itself by
// swapping ' ' <-> '\n' in place (same length, so the active widget's caret stays valid).
// That wrapper reflows from an idempotent source: it flattens every '\n' back to a space
// first. Which means a line break the USER typed was erased on their very next keystroke
// (live 2026-07-28: "alt+enter doesn't add a new line").
//
// A '\n' carries no marker saying who put it there, and the same-length invariant forbids
// adding one, so the offsets the user broke at are tracked out-of-band here and shifted by
// each edit. GL-free and ImGui-free on purpose: this is the subtle part, so it is unit-tested
// (host/tests/test_text_wrap.cpp).
#pragma once
#include <string>
#include <string_view>
#include <vector>

namespace ui::copilot {

// Where the user broke lines in one composer buffer.
struct WrapState {
    std::vector<int> hard;   // sorted, unique offsets of user-typed '\n'
    int lastLen = -1;        // buffer length when `hard` was last valid; -1 = never seen
};

// Treat every '\n' currently in the buffer as intentional. Correct for a buffer the wrapper
// has not touched yet: a fresh sighting, or text written programmatically (example chip,
// slash-command insert), where any newline was placed deliberately.
void seedHardBreaks(WrapState& ws, const char* buf, int len);

// Slide the recorded offsets across one single-caret edit whose result ends at `cursor`, and
// claim any newline the edit itself introduced (Alt/Shift+Enter, or a multi-line paste).
// Call this BEFORE rewrapping, while the buffer still shows what the user actually typed.
void noteEdit(WrapState& ws, const char* buf, int len, int cursor);

// Is the '\n' at `off` one of the user's?
bool isHardBreak(const std::vector<int>& hard, int off);

// Return the selected byte range from the live composer buffer, restoring only wrapper-created
// '\n' bytes to spaces. Offsets listed in `hardBreaks` are user-authored and remain newlines.
// Bounds are clamped and may be reversed.
std::string normalizeComposerSelection(std::string_view wrapped,
                                       const std::vector<int>& hardBreaks,
                                       int selectionStart, int selectionEnd);

}  // namespace ui::copilot
