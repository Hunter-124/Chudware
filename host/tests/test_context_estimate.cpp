// test_context_estimate.cpp — ctest for the copilot context meter's transcript estimator
// (ui::estimateTokens / ui::estimateEntries). Links the REAL implementation
// (src/ui/copilot/context_estimate.cpp), not a copy of it — no GL context, no ImGui, no AppState.
// That is exactly why the ChatMsg flattening lives at the call site instead of in that TU.
//
// What this defends. The meter used to render "\xe2\x80\x94/1.0M" for any session it had not
// personally watched make a provider call, which answered none of the user's actual question
// ("how much does this session already hold, and what does continuing cost?"). The fallback is an
// ESTIMATE of the transcript, and a wrong estimate on screen is worse than no estimate — so the
// counting rules are pinned here rather than eyeballed in the GUI.
//
// Acceptance:
//   ✓ an empty transcript is all zeros, not a fabricated floor
//   ✓ ASCII scales at the documented ~4 bytes/token, rounding up
//   ✓ CJK counts per CODEPOINT, so it lands above what a bytes/4 rule would claim
//   ✓ every message pays the role-framing overhead even with empty text
//   ✓ the tool/activity payload riding along with a message is folded in
//   ✓ a single oversized message is clamped at the 32000-token cap and sets `truncated`
//   ✓ a transcript whose naive sum exceeds INT_MAX saturates at INT_MAX and never goes negative
//   ✓ `messages` is the entry count, independent of any clamping

#include "ui/copilot/context_estimate.h"

#include <climits>
#include <cstdio>
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

#define CHECK_EQ_INT(a, b)                                                       \
    do {                                                                         \
        const long long _a = static_cast<long long>(a);                          \
        const long long _b = static_cast<long long>(b);                          \
        if (_a != _b) {                                                          \
            std::printf("FAIL %s:%d: %s == %s\n  got: %lld\n  want:%lld\n",      \
                        __FILE__, __LINE__, #a, #b, _a, _b);                     \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

static ui::TranscriptEntry msg(bool fromUser, std::string text, int activityChars = 0) {
    ui::TranscriptEntry e;
    e.fromUser = fromUser;
    e.text = std::move(text);
    e.activityChars = activityChars;
    return e;
}

static std::string repeat(const std::string& unit, int times) {
    std::string out;
    out.reserve(unit.size() * static_cast<std::size_t>(times));
    for (int i = 0; i < times; ++i) out += unit;
    return out;
}

// The per-message role framing, mirrored from context_estimate.cpp. If that constant changes on
// purpose, this changes with it; if it changes by accident, these tests say so.
static const int kOverhead = 4;
static const int kCap = 32000;

// ---- 1. estimateTokens ------------------------------------------------------------------------

static void test_empty_text_is_zero() {
    CHECK_EQ_INT(ui::estimateTokens(""), 0);
    CHECK(ui::estimateTokens("") >= 0);
}

static void test_ascii_scaling() {
    // ~4 bytes per token, rounded UP: a 1-byte string still costs a token.
    CHECK_EQ_INT(ui::estimateTokens("a"), 1);
    CHECK_EQ_INT(ui::estimateTokens("abcd"), 1);
    CHECK_EQ_INT(ui::estimateTokens("abcde"), 2);
    CHECK_EQ_INT(ui::estimateTokens(repeat("a", 100)), 25);
    CHECK_EQ_INT(ui::estimateTokens(repeat("a", 4001)), 1001);
}

static void test_cjk_counts_per_codepoint_not_per_byte() {
    // 30 x U+65E5 (3 bytes each) = 90 bytes. A bytes/4 rule would claim 23 tokens; real
    // tokenizers charge roughly one token per han character, so we must land at 30 — the byte
    // rule underestimates CJK by ~3x and this meter must not read that low.
    const std::string cjk = repeat("\xe6\x97\xa5", 30);
    CHECK_EQ_INT(cjk.size(), 90);
    CHECK_EQ_INT(ui::estimateTokens(cjk), 30);
    CHECK(ui::estimateTokens(cjk) > static_cast<int>(cjk.size()) / 4);
    // Same BYTE length in ASCII costs strictly less, which is the whole point of the split.
    CHECK(ui::estimateTokens(cjk) > ui::estimateTokens(repeat("a", 90)));
}

static void test_emoji_and_mixed_text() {
    // A 4-byte astral codepoint is ONE codepoint, counted once — not four bytes of ASCII.
    const std::string emoji = repeat("\xf0\x9f\x98\x80", 10);
    CHECK_EQ_INT(emoji.size(), 40);
    CHECK_EQ_INT(ui::estimateTokens(emoji), 10);
    // Mixed: 8 ASCII bytes -> (8+3)/4 = 2, plus 2 emoji -> 4 total.
    CHECK_EQ_INT(ui::estimateTokens("abcdefgh\xf0\x9f\x98\x80\xf0\x9f\x98\x80"), 4);
}

// ---- 2. estimateEntries -----------------------------------------------------------------------

static void test_empty_transcript_is_all_zeros() {
    const ui::ContextEstimate e = ui::estimateEntries({});
    CHECK_EQ_INT(e.tokens, 0);
    CHECK_EQ_INT(e.messages, 0);
    CHECK(!e.truncated);
}

static void test_per_message_overhead_is_charged() {
    // Five empty messages are not free: each still carries role framing on the wire.
    std::vector<ui::TranscriptEntry> t;
    for (int i = 0; i < 5; ++i) t.push_back(msg(i % 2 == 0, ""));
    const ui::ContextEstimate e = ui::estimateEntries(t);
    CHECK_EQ_INT(e.messages, 5);
    CHECK_EQ_INT(e.tokens, 5 * kOverhead);
    CHECK(!e.truncated);
}

static void test_text_plus_overhead_sums() {
    const std::vector<ui::TranscriptEntry> t = {
        msg(true, repeat("a", 100)),   // 25
        msg(false, repeat("b", 40)),   // 10
    };
    const ui::ContextEstimate e = ui::estimateEntries(t);
    CHECK_EQ_INT(e.messages, 2);
    CHECK_EQ_INT(e.tokens, 25 + 10 + 2 * kOverhead);
    CHECK(!e.truncated);
}

static void test_activity_chars_are_folded_in() {
    // Tool arguments/results ride along with the message and are resent with the history, so they
    // must be priced. 400 chars -> 100 tokens.
    const ui::ContextEstimate e = ui::estimateEntries({msg(false, "", 400)});
    CHECK_EQ_INT(e.messages, 1);
    CHECK_EQ_INT(e.tokens, 100 + kOverhead);

    // Text and activity add, they do not replace one another.
    const ui::ContextEstimate both = ui::estimateEntries({msg(false, repeat("a", 40), 400)});
    CHECK_EQ_INT(both.tokens, 10 + 100 + kOverhead);

    // A nonsensical negative payload size contributes nothing rather than subtracting.
    const ui::ContextEstimate neg = ui::estimateEntries({msg(false, "", -1000)});
    CHECK_EQ_INT(neg.tokens, kOverhead);
    CHECK(neg.tokens >= 0);
}

static void test_single_message_cap_clamps_and_flags() {
    // 200000 ASCII bytes would naively be 50000 tokens; the harness clips oversized history
    // entries, so we clamp at the cap AND admit the total is now a lower bound.
    const ui::ContextEstimate e = ui::estimateEntries({msg(true, repeat("a", 200000))});
    CHECK(e.truncated);
    CHECK_EQ_INT(e.tokens, kCap + kOverhead);
    CHECK_EQ_INT(e.messages, 1);

    // Just under the cap must NOT claim truncation — a false "estimate is low" badge is still a
    // wrong thing on screen.
    const ui::ContextEstimate under = ui::estimateEntries({msg(true, repeat("a", 4 * (kCap - 1)))});
    CHECK(!under.truncated);
    CHECK_EQ_INT(under.tokens, (kCap - 1) + kOverhead);

    // The cap is PER MESSAGE, not per transcript: three capped messages sum to three caps.
    const std::vector<ui::TranscriptEntry> three = {
        msg(true, repeat("a", 200000)),
        msg(false, repeat("a", 200000)),
        msg(true, repeat("a", 200000)),
    };
    const ui::ContextEstimate e3 = ui::estimateEntries(three);
    CHECK(e3.truncated);
    CHECK_EQ_INT(e3.messages, 3);
    CHECK_EQ_INT(e3.tokens, 3 * (kCap + kOverhead));
}

static void test_saturates_instead_of_overflowing() {
    // Eight messages each carrying an INT_MAX-sized activity payload: the naive sum is roughly
    // 4.3e9 tokens, well past INT_MAX. The answer must be INT_MAX, not a wrapped negative.
    std::vector<ui::TranscriptEntry> t;
    for (int i = 0; i < 8; ++i) t.push_back(msg(false, "", INT_MAX));
    const ui::ContextEstimate e = ui::estimateEntries(t);
    CHECK_EQ_INT(e.tokens, INT_MAX);
    CHECK(e.tokens > 0);
    CHECK_EQ_INT(e.messages, 8);

    // One INT_MAX payload alone is ~536M tokens: large, but representable, so it must NOT be
    // clamped to INT_MAX — saturation is a last resort, not a habit.
    const ui::ContextEstimate one = ui::estimateEntries({msg(false, "", INT_MAX)});
    CHECK(one.tokens > 0);
    CHECK(one.tokens < INT_MAX);
    CHECK_EQ_INT(one.tokens, (INT_MAX / 4 + 1) + kOverhead);
}

static void test_message_count_is_the_entry_count() {
    std::vector<ui::TranscriptEntry> t;
    for (int i = 0; i < 137; ++i) t.push_back(msg(i % 2 == 0, "hello there", 12));
    const ui::ContextEstimate e = ui::estimateEntries(t);
    CHECK_EQ_INT(e.messages, 137);
    // 11 ASCII bytes -> 3 tokens; 12 activity chars -> 3 tokens; plus framing.
    CHECK_EQ_INT(e.tokens, 137 * (3 + 3 + kOverhead));
}

int main() {
    test_empty_text_is_zero();
    test_ascii_scaling();
    test_cjk_counts_per_codepoint_not_per_byte();
    test_emoji_and_mixed_text();
    test_empty_transcript_is_all_zeros();
    test_per_message_overhead_is_charged();
    test_text_plus_overhead_sums();
    test_activity_chars_are_folded_in();
    test_single_message_cap_clamps_and_flags();
    test_saturates_instead_of_overflowing();
    test_message_count_is_the_entry_count();
    if (g_failures) {
        std::printf("test_context_estimate: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_context_estimate: all checks passed\n");
    return 0;
}
