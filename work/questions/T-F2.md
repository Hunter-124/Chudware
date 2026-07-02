# T-F2 questions for the advisor (Fable)

## Q1 — CAM cutting DB unification is C++-side / out of file scope

**Context.** The task DoD says "Unify engine/sim/materials.json + CAM cutting DB + DFM
profiles into engine/materials/ ... all three subsystems read the registry." The **CAM
cutting-data DB (feeds & speeds / machinability) is not a Python table** — it lives in the
C++ core and is reached only via the bridge (`cb.call("cam_materials")`, see
`services/mcp/cam_tools.py:301`). My "Files / areas touched" is Python-only
(engine/research, engine/materials, services/mcp); the C++ CAM DB and its bridge are
outside my scope, and I must not edit C++ blind.

**What I did (recommended answer).** Unified the two *Python* material tables that were
actually drifting — the FEA `_MATERIALS` literal in `cad_tools.py` and the DfM default
densities — onto the new `engine/materials` registry (single provenance-stamped source);
both now read it. `engine/sim/materials.json` (read by the desktop FEA GUI at a fixed path)
is regenerated from the registry, preserving its C++ sim keys. The **CAM cutting DB
unification is deferred** to a follow-up that can touch the C++/bridge seam.

**Recommendation:** accept the Python-side unification as T-F2's scope; open a follow-up
task to fold the C++ CAM cutting DB onto the registry (bridge work). Alternatives: (a) I
add machining/machinability fields to the registry now as a *seed* for that future task
(cheap, no C++ change) — happy to do this if you want it in T-F2; (b) expand my scope to
the C++ bridge (not recommended for one task, unverifiable without a host build here).

## Q2 — engine/sim/materials.json now carries reconciled values (C++ GUI reads it)

`engine/sim/materials.json` is loaded by `host/src/ui/sim_manager.cpp` (host/ is the
deprecated GUI). I made it a **generated mirror** of the registry (same keys, so the
appearance mapping is untouched), which reconciled a few values the two old Python tables
disagreed on. Most notable: **brass C360 yield 310 -> 124 MPa** (the FEA `_MATERIALS`
value; 124 = annealed, 310 = harder temper) and alu 6061 E 68900 -> 69000. I chose the
FEA-path values to keep the *tested* Python behaviour unchanged. Flagging because this
changes the desktop GUI's brass/alu FEA defaults on the next host build (unverifiable in
this Linux worktree). **Recommendation:** keep as-is (registry authoritative); if you
prefer the harder-temper brass (310 MPa) as the nominal, say so and I'll set it in the
registry (both sim + Python FEA pick it up).

---

## ANSWER (Fable, via FABLE-ARC acting supervisor, 2026-07-02)

**Q1 — APPROVED: Python-side unification is T-F2's scope.** The C++ CAM cutting DB
fold-in is now a tracked GitHub issue (bridge-seam follow-up; do not touch C++ here).
One cheap amendment on your branch, per your alternative (a): add the *schema-level*
machining seed to the registry — optional fields (e.g. `machinability_pct`,
`condition`/`temper`) with NO fabricated values (provenance rule §3.3 applies to
registry rows too; the only permissible seed value is the AISI 1212 = 100% reference
datum with its citation, if you include any at all).

**Q2 — APPROVED: registry stays authoritative with the conservative annealed value
(C360 yield 124 MPa).** Rationale: (a) it keeps tested Python FEA behavior unchanged;
(b) when temper is unstated, the conservative value is the defensible engineering
default for margin calculations — a user who wants half-hard 310 MPa is asserting a
temper and should say so. Required with this: the C360 registry row must carry
`condition: "annealed"` (and alu 6061 row its temper) so the 310-vs-124 discrepancy
is self-explanatory. The desktop-GUI default change is flagged to Erik in the
FOREMAN merge-queue line for T-F2 — the human merge is the sign-off gate for it.
