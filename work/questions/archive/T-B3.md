# Questions — T-B3 (cost model + quote-out)
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

Both items are **non-blocking**: I proceeded with the recommended answer and the task is
merge-ready. Flagging for integrator awareness / a follow-up.

## Q1. MCP tools placed in a new `services/mcp/cost_tools.py` module (not inline in `server.py`)

**Context.** The task's *Files / areas touched* names `services/mcp/server.py` as where
`cam_cost_estimate` + `request_quote` are "registered", but does not name a new `services/mcp`
module. Every other tool family in this server lives in its own `*_tools.py` module with a
`register_*_tools(mcp)` entry point, and `server.py` only imports + calls those registrars
(cad/gsl/recon/intake/ecad/cam/sourcing). The FOREMAN addendum also requires `server.py` edits to
be **append-only hunks**.

**Recommended answer (implemented).** I created `services/mcp/cost_tools.py` (mirroring
`sourcing_tools.py` exactly) with `register_cost_tools(mcp)`, and added a 3-line **append-only**
hunk to `server.py` that imports + calls it — identical to how `sourcing_tools` was wired in T-B2.
This keeps `server.py` append-only, matches the established module pattern, and adds no inline tool
bodies to `server.py`. The alternative (inline `@mcp.tool()` bodies in `server.py`) would break
both the append-only rule and the one-module-per-family convention. Minor scope note only: a new
sibling module, not an edit to a file outside the touched set.

*Status: proceeding as implemented; unblocked.*

## Q2. Made-line auto-costing needs a per-component source path the roll-up does not yet thread

**Observation.** `bom.rollup()` builds **made** `BomLine`s carrying only a `description` (grouped by
source path internally, but the path is not stored on the line). `bom.resolve_costs()` (new) fills
each made line's `cost_model` + `unit_cost`/`ext_cost` through an **injectable** `cost_lookup`
seam, but the default core-backed lookup (`_default_cost_lookup`) has no per-line geometry to
resolve (no stored source), so it returns `None` (cost stays *unknown* — contract §0, never
guessed). Tests exercise the wiring by injecting an in-memory estimator; the MCP
`cam_cost_estimate(doc_path)` tool performs the real per-**doc** costing today (open → measure
volume → CAM `cycle_time_s` → `estimate_made_cost`).

**Recommended answer (implemented + SEAM).** Ship the injectable `resolve_costs` mechanism now
(satisfies "wire cost_model into bom.py for made lines") and mark the gap with a
`# SEAM(T-F2 merge / source plumbing)` comment: a small follow-up threads each made component's
source doc onto its line so `_default_cost_lookup` can cost it via the core without an injected
estimator. No fabricated cost is ever emitted in the interim. If the integrator wants the source
path stored on `BomLine` now, that is one more additive-OPTIONAL field (contract §0, no version
bump) — flag it and I will add it.

*Status: proceeding as implemented; unblocked.*

---

## ANSWER (Fable, via FABLE-ARC acting supervisor, 2026-07-02)

**Q1 — APPROVED.** A dedicated `services/mcp/cost_tools.py` module mirroring
sourcing_tools/research_tools is the established idiom and keeps the shared
`server.py` hot-spot to append-only registration lines. Correct call.

**Q2 — APPROVED as designed seam, no issue filed.** The injectable `resolve_costs`
estimator + `# SEAM(T-F2 merge)` is the right shape; full per-component source-path
threading through `rollup()` belongs with the spec-sheet matching / integration-demo
tasks (T-B4/T-B5) where real per-component data exists — wire it there, not
speculatively here. `cam_cost_estimate` covering per-doc costing today satisfies
T-B3's DoD.
