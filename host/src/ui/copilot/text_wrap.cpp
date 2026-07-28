#include "ui/copilot/text_wrap.h"

#include <algorithm>

namespace ui::copilot {

void seedHardBreaks(WrapState& ws, const char* buf, int len) {
    ws.hard.clear();
    for (int i = 0; i < len; ++i)
        if (buf[i] == '\n') ws.hard.push_back(i);
    ws.lastLen = len;
}

void noteEdit(WrapState& ws, const char* buf, int len, int cursor) {
    if (ws.lastLen < 0 || cursor < 0 || cursor > len) {
        seedHardBreaks(ws, buf, len);
        return;
    }
    const int delta = len - ws.lastLen;
    ws.lastLen = len;
    // A same-length change is either the wrapper's own space<->newline swap or a one-for-one
    // replacement; either way no offset moved, and the wrapper never creates a hard break.
    if (delta == 0) return;

    // An insertion of `delta` chars ends at the caret, so it began `delta` back. A deletion
    // leaves the caret at the start of the hole it made.
    const int editStart = delta > 0 ? cursor - delta : cursor;
    std::vector<int> next;
    next.reserve(ws.hard.size() + 1);
    for (int h : ws.hard) {
        if (h < editStart) { next.push_back(h); continue; }
        if (delta < 0 && h < editStart - delta) continue;   // sat inside the deleted region
        next.push_back(h + delta);
    }
    for (int i = editStart; delta > 0 && i < cursor && i < len; ++i)
        if (buf[i] == '\n') next.push_back(i);

    std::sort(next.begin(), next.end());
    next.erase(std::unique(next.begin(), next.end()), next.end());
    ws.hard.swap(next);
}

bool isHardBreak(const std::vector<int>& hard, int off) {
    return std::binary_search(hard.begin(), hard.end(), off);
}

}  // namespace ui::copilot
