// context_estimate — see context_estimate.h for why this is its own GL-free translation unit.
//
// ===================================================================================
// HONESTY NOTE — read this before trusting a number that came out of here
// ===================================================================================
//
// This is a HEURISTIC LOWER BOUND on the next call's prompt size. It will read LOW against the
// provider's real `prompt_tokens`, usually by a wide margin, because it does not know:
//
//   · the SYSTEM PROMPT — the harness prepends a large role/skill/instruction preamble that the
//     transcript on screen never shows;
//   · the TOOL SCHEMAS — every advertised tool's JSON schema is resent on every single call, and
//     for the modeling toolset that alone is thousands of tokens;
//   · the PROVIDER'S TOKENIZER — each vendor's BPE splits differently; we approximate with
//     bytes-and-codepoints, which is close for prose and poor for dense JSON, base64 and code;
//   · attachments — images and files are billed by rules that have nothing to do with text length.
//
// So: never print a number from here without marking it an estimate. The context meter does
// exactly that, and it prefers a real measured prompt_tokens from the last provider call whenever
// one exists. This is the fallback for a restored session that has no measurement yet — an
// honest approximate answer beats the "\xe2\x80\x94" that told the user nothing.

#include "ui/copilot/context_estimate.h"

#include <climits>
#include <cstdint>

namespace ui {

namespace {

// Role framing per message. Every message resent as history carries a role marker and message
// delimiters on top of its own content; ~4 tokens is the usual cost across the chat formats we
// talk to. Small, but with a thousand-message transcript it is not noise.
constexpr std::int64_t kPerMessageOverheadTokens = 4;

// Ceiling on what ONE message's TEXT may contribute. The harness itself clips oversized history
// entries before sending them, so summing an unbounded pasted-log message would OVERSTATE the
// next call — and this estimate must never read high. When the cap bites we say so
// (ContextEstimate::truncated) rather than quietly reporting a clipped total as exact.
constexpr std::int64_t kPerMessageTokenCap = 32000;

// Saturating narrow to int. 64-bit accumulation upstream means the only way to get here out of
// range is a genuinely enormous transcript, and INT_MAX is the honest answer for "more than we
// can represent" — a wrapped negative token count on screen would be a lie.
int saturate(std::int64_t v) {
    if (v <= 0) return 0;
    if (v >= static_cast<std::int64_t>(INT_MAX)) return INT_MAX;
    return static_cast<int>(v);
}

// ~4 bytes per token for ASCII-ish text, rounded up.
std::int64_t tokensForChars(std::int64_t chars) {
    if (chars <= 0) return 0;
    return (chars + 3) / 4;
}

// 64-bit form of estimateTokens, so callers can accumulate without an intermediate clamp.
//
// Two populations, counted separately on purpose. ASCII bytes go through the bytes/4 rule.
// Non-ASCII CODEPOINTS are counted one token each instead, because a byte/4 rule underestimates
// CJK by roughly 3x: a 3-byte han character is about one token, not three quarters of one. We
// detect a codepoint as "a byte that is not a UTF-8 continuation byte" (0b10xxxxxx), which also
// degrades sanely on malformed input — a stray continuation byte is simply ignored rather than
// throwing the count off.
std::int64_t tokensForText(const std::string& text) {
    std::int64_t asciiBytes = 0;
    std::int64_t wideCodepoints = 0;
    for (const char c : text) {
        const unsigned char b = static_cast<unsigned char>(c);
        if (b < 0x80) {
            ++asciiBytes;
        } else if ((b & 0xC0) != 0x80) {
            ++wideCodepoints;  // lead byte of a multi-byte sequence
        }
        // else: continuation byte, already accounted for by its lead byte.
    }
    return tokensForChars(asciiBytes) + wideCodepoints;
}

}  // namespace

int estimateTokens(const std::string& text) {
    return saturate(tokensForText(text));
}

ContextEstimate estimateEntries(const std::vector<TranscriptEntry>& entries) {
    ContextEstimate out;
    out.messages = saturate(static_cast<std::int64_t>(entries.size()));

    std::int64_t total = 0;
    for (const TranscriptEntry& e : entries) {
        std::int64_t textTokens = tokensForText(e.text);
        if (textTokens > kPerMessageTokenCap) {
            textTokens = kPerMessageTokenCap;
            out.truncated = true;
        }
        // The activity payload is NOT capped: unlike a pasted wall of text it is machine-emitted
        // tool traffic that the harness resends whole, and clipping it here would understate a
        // tool-heavy session — the exact sessions where the meter matters most.
        total += textTokens;
        total += tokensForChars(static_cast<std::int64_t>(e.activityChars));
        total += kPerMessageOverheadTokens;

        // Saturate as we go. Without this, ~2^61 tokens of accumulated garbage could still wrap
        // the 64-bit accumulator itself; stopping at INT_MAX makes that unreachable.
        if (total >= static_cast<std::int64_t>(INT_MAX)) {
            total = INT_MAX;
            break;
        }
    }

    out.tokens = saturate(total);
    return out;
}

}  // namespace ui
