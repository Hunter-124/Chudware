// context_estimate — how much of the model's context window the transcript in front of the user
// already occupies, and what continuing this session would cost to resend.
//
// Why this exists: the copilot's context meter could only report the token counts returned by the
// LAST provider call. A restored session, or a fresh app start on an existing transcript, has no
// such measurement, so the meter rendered "\xe2\x80\x94/1.0M" and answered nothing. A transcript
// the user can see is a transcript we can count, so the meter falls back to this estimate instead
// of a dash.
//
// This is a HEURISTIC LOWER BOUND, never a measurement. See the .cpp for exactly what it cannot
// know (system prompt, tool schemas, the provider's real tokenizer). Anything drawn from it MUST
// be marked on screen as an estimate.
//
// Deliberately GL/ImGui/AppState-free so host/tests/test_context_estimate.cpp can link the REAL
// implementation rather than a copy of it (same shape as tool_phrase.{h,cpp} next door).
//
// NOTE ON THE MISSING ChatMsg ADAPTER: there is intentionally no ChatMsg overload here. `ChatMsg`
// lives in core/app_state.h, which drags in ImGui and GL and cannot be linked by a standalone
// unit test. So the ChatMsg -> TranscriptEntry flattening deliberately lives at the CALL SITE
// (host/src/ui/copilot/composer.cpp), which is already compiled against app_state.h. That keeps
// this translation unit GL-free and linkable by test_context_estimate on its own, and keeps the
// counting rules — the part that is easy to get subtly wrong — under test.
#pragma once

#include <string>
#include <vector>

namespace ui {

// The result of estimating a transcript. `tokens` is what the transcript would cost to RESEND as
// conversation history on the next call, not what any past call actually charged.
struct ContextEstimate {
    int  tokens    = 0;      // estimated tokens the transcript would occupy on the next call
    int  messages  = 0;      // messages counted
    bool truncated = false;  // a per-message cap clipped something (estimate is a lower bound)
};

// One transcript message, flattened to the only three things the estimate depends on. The GUI
// builds these from ChatMsg; the unit test builds them literally.
//
// `activityChars` is the size of the tool/activity payload that rides along with the message
// (tool arguments, results, streamed activity text) — bytes that are resent with the history but
// do not live in `text`. Pass 0 when there is none.
struct TranscriptEntry {
    bool        fromUser;
    std::string text;
    int         activityChars;
};

// Estimate what `entries` cost to resend as conversation history. Saturating: a transcript large
// enough to overflow an int yields INT_MAX, never a negative or wrapped number.
ContextEstimate estimateEntries(const std::vector<TranscriptEntry>& entries);

// Rough token count for one UTF-8 string. Public so the meter can price the draft prompt sitting
// in the composer before it is sent. Empty input yields 0; the result is never negative.
int estimateTokens(const std::string& text);

}  // namespace ui
