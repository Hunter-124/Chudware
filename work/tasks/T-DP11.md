---
id: T-DP11
title: GUI transparency panel — helpers / advisories / councils / delegation / tool calls
workstream: DP
owner: sonnet-vetch2
status: review
branch: task/T-DP11-transparency-panel
depends_on: [T-DP6]
lane: app-gui
kind: new
priority: 2
---

## Context

Part of the guided design-thinking pipeline — see
[docs/AGENT_DESIGN_PIPELINE.md](../../docs/AGENT_DESIGN_PIPELINE.md) (architecture of record,
Hunter 2026-07-05). MANDATORY: graphify to orient before editing; REUSE existing W-stack
substrate (phases.py, roles/, councils.py, advisor.md, aicad.buildgraph/1, delegation/routing,
aicad.effort/1, aicad.ledger/1, mission workspace) — extend, do not duplicate. Numbers never
invented — every value cites a tool/table/calc. Flexibility rule: never impose a fixed modeling
path; helpers are offered, stages are skippable checkpoints, scaffolding scales by effort/tier.

## Scope

Doc §3.7. A ui/imgui panel showing every helper fire, advisory, council vote, tool call,
auto-applied fix, and delegation — what fired, why, what it injected, what it changed — so
issues can be troubleshot. Glass-column design language, dark-mode contrast, ui_style_check
clean. Shot-verify each state.

## Definition of done

- [ ] Panel renders the live action stream from ledger `advisory`/`audit`/tool events.
- [ ] Each entry inspectable (inputs/outputs/why); auto-applied fixes flagged distinctly.
- [ ] Shot scenarios for populated + empty states, EYEBALLED.
- [ ] ui_style_check clean; gate.sh PASS; graphify update.

## Progress log
- 2026-07-06 02:27 UTC [sonnet-vetch2] Section 5 "Design-Pipeline Action Stream" shipped. Added to existing T-U8 transparency panel (not a rewrite). Deliverables: (1) ledger_events RPC in services/mcp/ledger_tools.py with kinds filter + after_seq cursor; (2) registered in tool_registry.py _READ_EXACT; (3) DPStreamModel / DPStreamEntry / DPStreamKind structs in ui/abstract/transparency_view.h; (4) full Section 5 implementation in transparency_panel.cpp — async polling via AsyncRpc, newest-first 500-entry ring, per-kind structured display (advisory items, audit verdicts, council decision, delegation budget, tool call names), [AUTO-FIX] badge for deterministic candidate_fixes, drawEntryInspector popover with raw payload TreeNode, theme tokens only (no literal colors); (5) shot scenarios dp_stream (6-event populated fixture) + dp_stream_empty eyeballed PASS; (6) application.cpp 2-line edit for taller window on dp_stream shots. Gate: PASS (all FAIL-RECORDED pre-existing / T-V2). ui_style_check 0 findings. Branch: task/T-DP11-transparency-panel commit 95ac68b2.
