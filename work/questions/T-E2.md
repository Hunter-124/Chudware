# Questions — T-E2 (1D pipe network solver)

## Q1 — Solver backend: native fluids+scipy vs. WNTR/EPANET  (RESOLVED-BY-DRIVER, advisor sign-off requested)

**Context.** The task-file DoD literally says "piping doc -> WNTR/EPANET model -> node
pressures + link flows". WNTR (EPANET 2.2 wrapper) is **not** installed in the tree, and
adding it is a NEW pip dependency (needs a `contracts/licenses.md` row; MIT/BSD, so
license-clean). My DRIVER addenda from the foreman explicitly direct: *"build the 1D
network solver on [`fluids`] (plus numpy/scipy already in the ledger)"* and to follow the
`engine/sim/circuit.py` MNA precedent (numpy/scipy, GPL-clean, contract-shaped errors).

**Decision taken (proceeding unblocked).** Native solver in `engine_oracle/piping/solve.py`:
a nodal (H-equation) Newton solve — the direct hydraulic analogue of the MNA circuit
solver — with Darcy-Weisbach headloss, Colebrook friction and fitting K-factors from
`fluids`. **No WNTR/EPANET dependency added.**

**Rationale.**
1. Foreman addenda are explicit direct orders overriding the task-file wording on *how*.
2. WNTR is not present; adding it + the EPANET solver would make the gate/pytest depend on
   an uninstalled dep and enlarge the license surface for no functional gain here.
3. `circuit.py` is the house pattern for network solves (nodal Newton, scipy sparse,
   soft-fail). A pipe network maps 1:1 (heads <-> node voltages, pipe conductance <->
   `dQ/dH`, fixed-head reservoir <-> ground/voltage source).
4. The DoD *substance* — node pressures, link flows, per-segment pressure-drop report with
   K-factor breakdown, pump-curve CSV + affinity scaling — is fully delivered natively.

**Recommended answer:** APPROVE native fluids+scipy solver; treat "WNTR/EPANET" in the DoD
as descriptive-of-intent, not a binding backend choice. If water-distribution parity with
EPANET is later required, WNTR can be added as an optional backend behind the same
`piping_solve` seam.

**Alternative:** add WNTR as an optional (feature-detected, soft-fail-when-absent) backend
in the same change with a licenses.md row — deferred unless the advisor wants it now.

---

## ANSWER (Fable, via FABLE-ARC acting supervisor, 2026-07-02)

**Q1 — APPROVED: native solver stands as primary.** The repo's own precedent decides
this: the ECAD circuit simulator is native MNA (numpy/scipy) with ngspice as an
optional backend, and rule §3.6's OSS-first mandate is about commodity plumbing —
which you honored where it matters (friction factors, fitting K's from `fluids`).
A 750-line validated Newton solve is the engineering layer we do own. Read the DoD's
"WNTR/EPANET" as descriptive-of-intent. If a cross-validation backend is ever wanted,
WNTR (BSD) would be an optional add via question file — not required for T-E2.
Physics validation record (hand-calc parity) noted and appreciated; keep those tests.
