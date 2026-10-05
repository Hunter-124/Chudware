// agent_text — sanitizers for text the copilot did NOT author in C++: tool-call argument
// previews, target lines and summaries that arrive from the Python harness or straight from the
// model. Deliberately GL/ImGui/AppState-free so host/tests/test_agent_text.cpp can link the REAL
// implementation rather than a copy of it (same shape as conv_ids.{h,cpp} next door).
//
// Two live defects from one session, same boundary:
//
//  1. Opaque doc ids. The user opened a part off disk and asked the agent to pocket it; the
//     tool-call card rendered the raw arguments —
//         {"doc_id": "doc-cpp00000039", "ops": [...]}
//     — so the part's NAME was replaced, on screen, by an engine id. The doc-tab strip already
//     solved this (AppState::DocTab::displayTitle()); chat never did.  -> substituteDocIds
//
//  2. Missing glyphs. The card header read "DevicePocket ? MouthpieceSlot ?+2 more": those are
//     U+2192 RIGHTWARDS ARROW, which the UI font atlas does not cover, drawn as notdef.
//     services/agent/agent_events.py joins tool-card targets with " -> " (a real arrow) and the
//     model writes arrows into prose constantly. C++-authored strings were purged of these long
//     ago (host/src/ui/copilot/cards.cpp:188, :418); nothing sanitized text from outside.
//     -> foldToAtlasGlyphs
//
// ORDER: substituteDocIds FIRST, then foldToAtlasGlyphs. Doc ids are pure ASCII so the fold can
// never create or destroy one, while a resolved document name (a filename) may well contain
// uncovered codepoints — running the fold last catches those too. Both are idempotent, so the
// ingest side (feed.cpp) and the render side (activity_timeline.cpp) may each apply them.
#pragma once

#include <functional>
#include <string>

namespace ui {

// ---- 1. opaque engine doc ids -------------------------------------------------------------

// Rewrite every opaque engine doc id in *text* using *resolve*, which returns the human label for
// an id or an empty string when it has none.
//
// An id is "doc-cpp" or "doc_cpp" followed by one or more hex digits, matched as a WHOLE token:
// "mydoc-cpp1" and "doc-cpp00000039x" are left alone (a longer identifier that merely contains
// the shape is not a doc id).
//
// Rendered form: `Mouthpiece (doc-cpp00000039)` — the name first, the id kept in parentheses.
// The SAME form is used in prose and inside a JSON string value. Deliberate: the args blob on a
// tool-call card is display-only (nothing re-parses it), the user's complaint is that the name is
// missing rather than that the id is present, and support/log correlation still needs the id.
// Parentheses need no JSON escaping, so the surrounding quoting stays intact either way.
//
// Unknown ids (resolve -> "") are left exactly as they are: a name we do not have must never
// become filler like "a document" inside a JSON blob.
//
// Idempotent: an id already rendered as `Name (id)` is not rewritten again.
//
// No-op fast path: when *text* contains no match it is returned unchanged, with no scratch buffer.
std::string substituteDocIds(const std::string& text,
                             const std::function<std::string(const std::string&)>& resolve);

// ---- 2. codepoints the font atlas has no glyph for ----------------------------------------

// Replace codepoints the UI atlas has no glyph for with covered equivalents ("→" -> "->",
// "✓" -> "ok", "ε" -> "eps", …). Anything uncovered with no sensible mapping becomes a plain '?'
// — the same thing the user sees today, so never a regression and never a mangled byte.
//
// Second consumer besides the chat: Settings → AI → Rules (settings_modal.cpp) renders rule
// metadata and context-pack BODIES that come from services/agent/advisor. Those packs are
// model-facing engineering prose and legitimately contain ε δ θ ρ α π — the source must not be
// degraded, so the fold happens at the display boundary only, before any CalcTextSize().
//
// Valid UTF-8 in, valid UTF-8 out; multi-byte sequences are never split. Malformed input does not
// crash — each bad byte becomes '?'.
//
// No-op fast path: pure-ASCII text is returned unchanged with no scratch buffer. This runs on
// every rendered row.
std::string foldToAtlasGlyphs(const std::string& text);

// True when the font atlas has a glyph for *cp*: the ui/glyph_ranges.h table ui_theme.cpp loads,
// plus the layout controls ('\n', '\t', '\r', 0x1E) ImGui handles without a glyph.
bool atlasCovers(unsigned cp);

// ---- 3. skip-work predicate ---------------------------------------------------------------

// True when either sanitizer above COULD change *text*: it holds a byte >= 0x80 (the only bytes
// foldToAtlasGlyphs touches) or a "doc-cpp"/"doc_cpp" prefix. Allocation-free, one pass.
//
// The "no-op fast path" both functions document is about the SCRATCH BUFFER — they still return
// `text` BY VALUE, which copy-constructs a whole new string for anything past SSO. The transcript
// re-sanitizes every visible row every frame, and nearly every one of those strings is plain
// ASCII with no doc id, so gating on this turns two heap round-trips per string per row per
// frame into one linear scan.
bool agentTextMayChange(const std::string& text);

}  // namespace ui
