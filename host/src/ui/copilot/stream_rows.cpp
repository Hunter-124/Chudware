// See stream_rows.h. Pure ordering logic — no ImGui, no AppState.

#include "stream_rows.h"

namespace ui {

bool streamTextVisible(const std::string& text) {
    for (const char ch : text) {
        switch (ch) {
            case ' ': case '\t': case '\n': case '\r': case '\f': case '\v': case '\0':
                continue;
            default:
                return true;   // any other byte, including UTF-8 continuation bytes, is content
        }
    }
    return false;
}

int streamDeltaTarget(const std::vector<StreamRowState>& rows, bool thinking) {
    for (int i = (int)rows.size() - 1; i >= 0; --i) {
        const StreamRowState& r = rows[i];
        if (r.thinking != thinking) continue;
        if (r.streaming) return i;
        // A just-finalized ops box still at the tail keeps collecting: consecutive stage
        // bursts then share ONE "Building ops…" box instead of stacking near-identical cards.
        // It is still the last row, so growing it cannot reorder anything.
        if (!thinking && r.ops && r.tail) return i;
        // The newest box on this channel is closed and buried under later rows. Anything
        // appended to it would render above them — open a new box at the tail instead.
        return -1;
    }
    return -1;
}

int streamChannelBoundary(const std::vector<StreamRowState>& rows, bool thinking) {
    for (int i = (int)rows.size() - 1; i >= 0; --i) {
        const StreamRowState& r = rows[i];
        if (r.thinking == thinking) continue;
        if (!r.streaming) return -1;   // already closed; nothing to do
        if (thinking && r.ops) return -1;
        return i;
    }
    return -1;
}

}  // namespace ui
