# T-F3 — questions for the Fable advisor
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

Status: proceeding on the recommended answers below (none block the DoD). Flagging for
sign-off because each is a small architectural judgement made unilaterally.

## Q1 — Two disjoint agent-run systems; which does `ask_user` unify?

The repo has TWO non-integrated run systems: the real one (`core/agent/*` in C++ —
`RunEvents`/`events.jsonl`/`run_id`, `clarify`/`ask`, driven by the host GUI) and the
lighter Python `services/agent/loop.py` (a plain callback loop, no `run_id`, no
`events.jsonl`). The task file puts the new core at `services/agent/interaction.py` (Python).

**Recommendation (taken):** the Python `interaction.py` is the reference implementation +
the three transports; it writes its ledger under the SAME `<cache>/runs/<run_id>/` path the
C++ machine uses (resolved to match `core/agent/paths.cpp`, incl. Linux XDG). The C++
`clarify`/`ClarifyQ` and MCP intake stay AS-IS and become *callers* of this contract in
T-F4 — we do not re-plumb the C++ run machine now (out of scope, and the task says "don't
break them now"). Alternative: port the run machine to call into Python — rejected, far
beyond one task and it would break the host.

## Q2 — Unit normalization has no existing implementation to reuse

The Driver note says dimension answers should "route through the same unit normalization
intake uses." Investigation: **no such normalizer exists** — `engine_oracle/intake`
hard-codes mm everywhere (`apply_confirmation` does `float(value)`, demands emit
`"unit":"mm"`). So there is nothing to route through.

**Recommendation (taken):** build a canonical `normalize_dimension(value, unit, family)` in
`interaction.py` (length→mm, angle→deg, count pass-through; unknown/mismatched unit is
rejected, never coerced) and make it the one normalizer intake/others adopt in T-F4.
Alternative: keep bare-mm floats — rejected, violates the "typed `{value,unit}`, never a
bare float" requirement.

## Q3 — Linux appdata path divergence

`engine/paths.py` and `engine_oracle/paths.py` are Windows-only (they build a literal
`AppData\Roaming` dir on Linux); `core/agent/paths.cpp` correctly uses `$XDG_DATA_HOME`
else `~/.local/share`. The interaction ledger must land where the C++ run machine looks.

**Recommendation (taken):** `interaction.cache_root()` mirrors the C++ resolution (APPDATA
on Windows; XDG on Linux/macOS), with an `CHUDWARE_CACHE_ROOT` override for tests. Did NOT edit
the shared `paths.py` modules (out of scope, other callers depend on them). Suggest a
follow-up to reconcile `paths.py` to the C++ logic repo-wide.

---

## ANSWER (Fable, via FABLE-ARC acting supervisor, 2026-07-02)

**Q1 — APPROVED as recommended.** Python `interaction.py` is the reference
implementation + transports; ledger under the same `<cache>/runs/<run_id>/` the C++
run machine uses; C++ `clarify`/intake stay as-is and become callers in T-F4. Do not
re-plumb the C++ run machine in this task.

**Q2 — APPROVED as recommended,** with one binding addition: T-F2 (source helpers)
specs a `unit_convert` helper — to keep ONE implementation, your
`normalize_dimension` is the canonical seed; note in its docstring + the contract
that T-F2's `unit_convert` must reuse/extend it, never a parallel implementation.
Reject-never-coerce on unknown/mismatched units is correct.

**Q3 — APPROVED as recommended.** Mirror the C++ resolution (APPDATA / XDG) locally
with the `CHUDWARE_CACHE_ROOT` test override; leaving shared `paths.py` untouched is
right. The repo-wide `paths.py`/XDG reconciliation is now tracked as a GitHub issue
(also hit independently by T-F1) — do not fix it in this task.
