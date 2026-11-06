---
name: chudware-tool-dialect-hardening
description: Harden modeling tool dialects using exact failure replay and numerical geometry evidence.
---

# chudware-tool-dialect-hardening

Capture the full recorded tool call, response and surrounding context from supplied evidence; a screenshot may truncate arguments. Replay that exact call first in a private workspace and engine. Classify a complete, unambiguous request rejected or misbuilt by the harness separately from genuinely invalid model input and real engine geometry failures. Prioritize silent wrong geometry, and do not guess missing intent.

Trace dispatch in `services/agent/agent_turn.py` and normalization in `services/mcp/cad_doc_tools.py`; check engine backstops in `core/doc/session.cpp` when direct clients can bypass Python. Review active OMP instructions, tool schemas, role context and cheatsheets; reconcile native and FastMCP dispatch and avoid silently dropping unknown arguments. Errors should identify offending values and accepted forms.

## Family sweeps and sketch normalization

Sweep related primitives and their diameter/radius and parameter aliases (`_DIAMETER_ALIASES`, `_PARAM_RENAMES`). Canonical keys take precedence; only convert diameters where the actual primitive expects a radius. For sketches inspect entity-list aliases, coordinate forms, relative profile references and `_normalize_sketch_feature`. Scope relative references to intended profile slots. Inserting compound operations requires `_shift_batch_placeholders` to preserve `$N` references and map a compound to its intended result. Preserve identity for unchanged inputs where the normalizer contract requires it, and compare rewrite evidence by value.

## Reproduction and proof

Use offline recorded-reply scenarios first, bucket failures by cause and preserve the raw reply. A real provider is appropriate only when explicitly warranted. `tools/flywheel/replay_ledger_failures.py` supports `--tool` and `--detail`; use explicitly isolated engine/state and seed needed scratch geometry. Report state-dependent skipped cases separately, never as fixes or as mandatory 100% acceptance. Compare normalized failures on matched isolated source exports/builds rather than stashing the shared tree.

For each repair replay verbatim arguments into a fresh document and assert bbox, volume, entity or tree values against the requested geometry; `ok:true` is not proof. Cover canonical input, conflicts, ambiguity and sibling cases. Update project documentation when warranted.

Verify: `scripts/testbox.py --suite agent,geom,sketch`
