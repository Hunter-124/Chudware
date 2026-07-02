---
id: T-D4
title: Code compliance - agentic RAG and deterministic rule engine
workstream: D
owner: unassigned
status: todo
branch: task/T-D4-code-compliance
day: 4
depends_on: [T-D3]
---

## Context

Building-code compliance uses two layers: (1) a deterministic IDS-style rule engine (ifctester pattern, LGPL) for checks we encode ourselves (span limits, deflection L/360, fastener schedules, post base requirements), and (2) agentic RAG against public code viewers at runtime for jurisdiction-specific requirements. Jurisdiction-adoption metadata is bundled (not the code text). The `code_check` MCP tool returns citations, a rule-pass list, and a disclaimer. Never bundle IBC/IRC/NDS text.

## Files / areas touched

- `engine_oracle/structural/code_check.py` — new: deterministic rules + agentic RAG orchestration
- `engine_oracle/structural/jurisdiction_meta.py` — new: bundled jurisdiction-adoption metadata
- `engine_oracle/data/jurisdiction_meta.json` — e.g., `{"CA": {"ibc_edition": "2022", "asce7_edition": "22"}}`
- `services/mcp/server.py` — register code_check tool

## Steps

1. Run `graphify query "ifctester rule engine IDS"` to check if any rule engine patterns exist.
2. Create `engine_oracle/data/jurisdiction_meta.json`: map US state codes → `{ibc_edition, asce7_edition, nds_edition, residential_code}`; seed with common states (CA, TX, NY, FL, WA, OR).
3. Implement `jurisdiction_meta.py`: `get_jurisdiction(state_code) -> dict`.
4. Implement deterministic rules in `code_check.py`:
   - `check_deflection(span_mm, deflection_mm) -> RuleResult`: pass if deflection < span/360; cite "AISC 360 / IBC 1604.3 deflection limits (L/360 live load)."
   - `check_post_span(post: Post, load_kN) -> RuleResult`: simplified span table check for lumber posts.
   - `check_fastener_schedule(connection_type: str) -> RuleResult`: check against our encoded fastener schedule.
5. Implement `agentic_rag(query: str, jurisdiction: str) -> list[Citation]`: call the LLM with a RAG prompt pointing to public code viewers (upcodes.com public pages, ICC public viewer); return a list of `{text, source_url, section}` citations. Mark these as agent-retrieved, not guaranteed accurate.
6. Register `code_check(doc_path: str, jurisdiction: str, load_combo: str) -> dict` MCP tool: runs deterministic rules + agentic RAG; returns `{rule_results, citations, disclaimer}`.
7. Test: deflection check for a 3m span with 8mm deflection; assert passed=True (8 < 3000/360 = 8.33mm). Then with 10mm; assert passed=False.

## Definition of done

- [ ] `check_deflection()` correctly applies L/360 limit with citation
- [ ] `jurisdiction_meta.json` covers at least 6 US states
- [ ] `agentic_rag()` returns citation list (mock the LLM call in tests)
- [ ] `code_check` MCP tool returns `{rule_results, citations, disclaimer}`; disclaimer always present
- [ ] `python -m pytest engine_oracle/structural/test_code_check.py -q` passes

## AI hints

- `graphify query "LLM agent RAG citation retrieval"` — find existing RAG patterns in the codebase
- `graphify query "deterministic rule engine check pass fail"` — look for existing rule-engine patterns
- Relevant files: `engine_oracle/structural/checks.py`, `engine_oracle/structural/analysis.py`, `services/agent/`, `services/mcp/server.py`
- UPCodes public viewer: https://codes.iccsafe.org/public — some sections are publicly viewable; agent retrieves at runtime, never caches text; always attribute the source URL in citations
- IDS/ifctester pattern: each rule is a `Rule` object with `check(element) -> RuleResult`; register rules in a `RuleSet`; run all rules and collect results
