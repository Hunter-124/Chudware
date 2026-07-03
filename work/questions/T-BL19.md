# T-BL19 — questions & bug reports for Erik / Fable advisor

## BUG (needs a GitHub issue filed by a human — `gh issue create` blocked by the write classifier for this agent)

**Title:** Engine: boolean fuse of two DISJOINT bodies succeeds (returns ok + 2 bodies) instead of failing per contract

**Found by:** T-BL19 modeling battery, case `nc_disjoint_fuse` (marked XFAIL against this bug).

**Contract** (`services/agent/prompts/shared/ops_cheatsheet.md`, BOOLEAN section):
> A fuse must produce ONE connected solid; a cut/common must also yield ONE solid piece (disjoint results fail …).

**Actual:** fusing two 10 mm boxes 100 mm apart —
`box A@origin; box B@[100,0,0] (merge:false); boolean{op:"fuse",a:"f1",b:"f2"}` —
returns `feature_status.ok == true`, `eval_mass.body_count == 2`, `eval_check.issues == []`.
The fuse silently no-ops; the two bodies stay disjoint and no error is raised.

**Expected:** a fuse whose operands do not touch should fail the rebuild with a clear
"disjoint fuse" error (or explicitly flag a non-manifold compound). Silent success with
2 bodies breaks the "fuse => 1 connected solid" invariant downstream code relies on.

**Repro:** `AI_CAD_LIBAICAD=$PWD/core/build-occt-linux/bin/libaicad.so PYTHONPATH=$PWD python3 scripts/modeling_battery.py` — see case `nc_disjoint_fuse`.

**Severity:** low-medium (valid solids produced, but a promised invariant is unenforced).

---

## Coverage decisions taken autonomously (flag if you disagree)

- **`.so` pinned to the OCCT build.** Only `core/build-occt-linux/bin/libaicad.so` has the
  real geometry kernel (netgen + openNURBS + lib3mf). `core/build-linux/bin/libaicad.so`
  has no geometry — running the battery against it would ALL-SKIP, which the battery
  treats as a hard failure. The gate stage prefers the OCCT `.so`.

- **"pattern-of-boolean" reinterpreted.** The engine only patterns/mirrors SKETCH-based
  subtractive features (extrude-cut / hole), not an explicit `boolean` feature ("no
  patternable geometry" error). This is a documented engine constraint, not a bug — I
  cover the intent with `pattern_of_cut` / `mirror_of_cut` and record the constraint as a
  passing negative control `nc_pattern_of_explicit_boolean`.

- **Manifold predicate.** `eval_check` on this OCCT build emits `{"issues":[...]}` (not the
  `free_edges` scalar the `services/verify/battery.py::_gap_is_manifold` check reads —
  see note below). Curved solids (sphere/revolve) carry `tiny_edges` *warnings* at their
  poles/seams; those are cosmetic. The battery flags NON-manifold only on a `free_edges`
  issue or an `error`-severity issue.

## Observation (not filed — scope rule; verify battery is outside my write-set)

`services/verify/battery.py::_gap_is_manifold` reads `r.get("free_edges", 0)` from
`eval_check`, but the OCCT build returns `{"issues":[...]}` with no top-level `free_edges`
key — so that check currently returns `ok=True` for ANY body (a blind check). The T-BL19
battery rolls its own manifold predicate instead of patching that file. Recommend a
follow-up task to fix `_gap_is_manifold` to parse the `issues` list.

## ANSWER (Fable foreman, delegated by Hunter 2026-07-03)

- **Bug filed:** GitHub issue #8 (foreman filed on Hunter's delegation).
- **Severity ruling:** low-medium CONFIRMED — not drop-everything, but the invariant is contract-promised and downstream code relies on it. Fix engine-side. Backlog task **T-BL30** cut (modeling-core, fix, P3): enforce the fuse/cut/common single-connected-solid invariant; battery XFAIL `nc_disjoint_fuse` flips to expected-pass on fix.
- **Coverage decisions** (OCCT .so pin, pattern-of-boolean reinterpretation, manifold predicate): all three APPROVED.
- **`_gap_is_manifold` blind check:** folded into in-flight T-BL23 by foreman note (same battery.py surface — parse the `issues[]` list instead of the missing `free_edges` key).
