# Questions / notes — T-B5 (spec-sheet matching v2: AI path + integration demo)

All items non-blocking: proceeded with the recommended answer; task is merge-ready. Flagging
for supervisor awareness.

## N1. `search_reference_data` / `script` feature / "GSL render" are plan-doc terms, not real schemas

**Context.** The task Context + Steps reference the INTAKE plan's `search_reference_data` schema,
a `script` feature, and a "GSL render". Verified: none exist as importable code — they are
aspirational terms in `docs/SPRINT_5DAY_PLAN.md` / `docs/INTAKE_MEASUREMENT_DFM_PLAN.md`. The real
intake package (`engine_oracle/intake/`) exposes MeasuredFeature/CriticalDimension schemas, not a
`search_reference_data` entry point.

**Decision (implemented).** Modeled the AI path on the real primitives:
- **Parametric template** = a small `TEMPLATES` registry in `spec_sheet_matcher.py`
  (`capacitor_radial`/`capacitor_axial`/`box`/`cylinder`), each mapping validated dims → real
  `doc_apply_ops` box/cylinder feature ops (the "`script` feature" == the doc-ops build path the
  MCP `add_features` tool already uses). New templates are additive.
- **GSL render** = `gsl_preview()`, a deterministic offline text render of the filled template for
  user confirmation (matches the engine's text-only `describe_*`/GSL posture; no vision, no core).
- Provenance is never fabricated: the LLM may only fill template-defined keys; a missing REQUIRED
  dim is surfaced (`missing_required`) and `register_from_spec_sheet` refuses to build.

## N2. LATENT BUG in T-H7 `LoggingProvider` on Linux — `engine.paths.DATA` does not exist

**What I found.** `services/agent/providers/logging_provider.py::_default_sidecar_dir()` returns
`engine.paths.DATA / "telemetry" / "sidecars"`. On this repo `engine.paths` (==
`engine_oracle/paths.py`) has **no `DATA` attribute** (only ROOT/CACHE/CONFIG/VENDOR_PARTS/…). The
constructor calls `_default_sidecar_dir()` **eagerly** whenever `sidecar_dir` is None — so any
call site that does `logging_provider.wrap(provider)` **without** an explicit `sidecar_dir`
crashes with `AttributeError: module 'engine.paths' has no attribute 'DATA'` at construction
(before any consent check). T-H7's own tests pass only because they always inject a `tmp_path`
sidecar_dir.

**Impact.** Latent: it bites the first production Linux call site that wraps without a sidecar_dir.
Not triggered on master today (loop.py's path may differ), but it's a real cross-platform gap.

**Workaround (in my code).** `spec_sheet_matcher._default_provider()` passes an explicit
`sidecar_dir=paths.CACHE / "telemetry" / "sidecars"`, so T-B5 is unaffected.

**Recommended fix (for the T-H7 owner / a follow-up):** make `_default_sidecar_dir()` use
`engine.paths.CACHE` (exists on all platforms) or add a `DATA` alias to `engine_oracle/paths.py`.
Filing as awareness; happy to take a one-line follow-up if you want it in this branch.

## N3. Flywheel logging wiring (IP tag + emit)

Per the assignment, every LLM call goes through the T-F9 provider wrapped in T-H7's
`LoggingProvider`. Chose **insertion_point = "IP-16"** ("Standards/parts lookup & substitution …
datasheet quote substring-match" — the exact survey row for this path) and **suppress_emit=False**
with an `on_event` sink (there is no downstream loop.py emitter at this standalone call site, so the
default `suppress_emit=True` would silently no-op the requirement). Default sink appends the
`ai_artifact` event to a durable telemetry journal (`paths.CACHE/telemetry/events.jsonl`); a
caller may inject its own. Test `test_default_provider_wraps_with_logging_ip16` +
`test_ai_extract_default_provider_emits_to_journal` pin this.

## N4. `resolve_prices` overwrites `vendor_part` → spec-sheet provenance modeling in the demo

`bom.resolve_prices` replaces a bought line's `vendor_part` wholesale with the priced record. In the
integration demo the spec-sheet-matched cap is priced through the (mock) chain; to keep its
`source="spec_sheet"` provenance after pricing, the demo chain tags the priced record for the
spec-sheet MPN back to `source="spec_sheet"` (a distributor price + link attached to a spec-sheet
part, not a provenance rewrite). Contract untouched — this is demo-local modeling.

## N5. server.py — no edit needed

`register_spec_tools(mcp)` is already wired in `services/mcp/server.py` (from T-B4). The two new
tools (`spec_sheet_match`, `list_spec_templates`) ride that existing registration — **no server.py
change**, append-only rule trivially satisfied.
