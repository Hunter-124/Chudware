// tool_phrase — turns raw MCP tool calls into sentences a person reads, and decides which
// consecutive calls may be folded into one row. Deliberately GL/ImGui/AppState-free so
// host/tests/test_tool_phrase.cpp can link the REAL implementation rather than a copy of it
// (same shape as agent_text.{h,cpp} next door).
//
// The live defect (session 2026-07-28, run-138660919763973): the chat transcript printed the
// harness's own plumbing. One card read
//     Doing · add_features
// with a dim second line
//     Lug1 (box) -> Lug2 (box)
// and the agent's repair loop stacked one byte-identical card per call, so a three-call fixup
// became three identical cards. Nothing on screen was written for a human: `add_features` is an
// MCP symbol, "Doing ·" is filler, and the repetition hid the one call that actually failed.
//
// So: a phrasing table maps the tool symbol to a present-participle title ("Building geometry"),
// records whether the call MUTATES the model/filesystem (the timeline draws mutating cards
// heavier), and hands back a groupKey that says whether this row may fold into the previous one.
//
// Two invariants the grouping must never break:
//   1. A FAILED call always gets an empty groupKey. A failure that folds into a run of successes
//      is a failure the user cannot see.
//   2. Calls that create or ingest a document (new_part / new_assembly / import_part) and calls
//      that address the user (ask_user / delegate) never fold either — each one is a distinct
//      event with a distinct consequence, and collapsing "Creating a part × 2" is precisely the
//      duplicate-document bug we are trying to make visible.
#pragma once

#include <cstddef>
#include <string>
#include <string_view>

namespace ui {

// A tool call rendered for humans.
struct ToolPhrase {
    std::string title;    // "Building geometry", "Rendering the part", "Reading the project folder"
    std::string detail;   // the human target, already cleaned; may be empty
    std::string groupKey; // consecutive rows with an equal, non-empty groupKey may be folded
    bool        mutating; // true when the call changes the model/filesystem (affects card weight)
};

// tool: the raw MCP tool name ("add_features"). target: the event's human target line
// (may be empty, may be raw JSON-ish). ok: whether the call succeeded.
//
// An unknown tool never leaves a bare snake_case symbol on screen: `foo_bar_baz` becomes
// "Foo bar baz" and is treated as non-mutating (we cannot know, and the lighter card is the
// safer lie — it claims nothing).
ToolPhrase toolPhrase(std::string_view tool, std::string_view target, bool ok);

// Human label for a group of `count` consecutive calls sharing a groupKey, e.g.
// toolGroupTitle("add_features", 3) -> "Building geometry \xc3\x97 3". A count of 1 or less is
// just the title — "Rendering the part × 1" is noise.
std::string toolGroupTitle(std::string_view tool, int count);

// Turn a raw tool target into something a person reads:
//   · trim, and collapse every whitespace run (newlines and tabs included) to one space;
//   · unwrap a lone JSON string      `"foo"`                  -> foo
//   · unwrap a one-key JSON object   `{"doc_id": "mudbucket"}` -> mudbucket
//     (two or more keys are left byte-for-byte alone — the raw args drawer handles those);
//   · ellipsize at *maxChars* CODEPOINTS, appending "\xe2\x80\xa6".
//
// The "\xe2\x86\x92" arrows the harness joins targets with are kept as they are — the timeline
// folds them to atlas glyphs later (agent_text.h), and rewriting them to prose here would fight
// that pass. Truncation is UTF-8 safe: a multi-byte sequence is never split.
//
// Empty or whitespace-only input yields an empty string.
std::string humanTarget(std::string_view target, std::size_t maxChars = 90);

}  // namespace ui
