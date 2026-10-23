# Simulation Optimizer Program

**Program baseline:** 2026-08-24

This program defines how learned simulation optimizers may be admitted into Chudware. The
per-family track registry, its source-hash validator, and the training corpora were
research tooling and are not part of the public tree; the rules, gates and current
decisions below are the record. Any future registry must check every registered baseline
digest against the current source bytes and revalidate after a refresh.

## Authority boundary

An optimizer may advise mesh sizing, order, adaptation, iteration controls, time or frequency sampling, and resource allocation only inside the action space registered for its simulation family. It does not supply physics and it does not certify a result.

The deterministic mesher, solver, error estimator, convergence checks, conservation checks, resource governor, and family-specific safety vetoes remain authoritative. A veto always wins. An unavailable, out-of-distribution, over-budget, unsupported, or failed optimizer falls back to the registered deterministic baseline and says why.

In particular, Chudware's current structural interface accepts tet4/tet10 connectivity. The PolyMesh advisor (from the pinned Polyhedral-FEA fork, see [`contracts/licenses.md`](../../contracts/licenses.md)) may rank actions such as hex or hybrid meshes that its own runtime supports; Chudware must refuse those actions rather than translate them into a different solve or claim that VEM, hybrid, hex, or polyhedral elements ran. Static and adaptive FEA share one optimizer track because they advise the same starting mesh/action space, but adaptive convergence remains a solver-side decision.

All evidence in this program is internal verification and validation (V&V). It is not third-party accreditation, regulatory approval, or external certification.

## Registry and promotion rules

Every shipped family has exactly one track. A track records the deterministic baseline and its source hash, corpus state and hashes, a group-held-out split, objective, vetoes, model and model-card hashes, decision metric, and promotion state.

Promotion is evidence-driven and monotonic:

1. **Gated** — no trained artifact is claimed. Missing corpora and artifacts are explicit `null` values with a blocker, never placeholder hashes or fabricated measurements.
2. **Candidate** — training completed and artifact, model card, corpus, split, and held-out evidence are hash-addressed. The candidate is not active by default.
3. **Validated** — the preregistered family-held-out decision gate passes without weakening a veto or tuning on the evaluation folds.
4. **Promoted** — runtime integration applies only supported advice, preserves deterministic fallback, and passes the same frozen evidence gate on the exact shipped artifact.

A promotion review is an explicit no-go if any of the following is true:

- the corpus, manifest, baseline, model, model card, or evaluation evidence lacks a SHA-256 digest;
- rows, actions, candidate settings, or repeated runs from one geometry/template/asset occur on both sides of a split;
- any registered veto is absent or bypassable;
- the candidate does not beat the deterministic baseline on its preregistered decision metric;
- the candidate improves an average while regressing a safety or tail-error bound;
- the evaluated action space differs from the runtime action space;
- the result depends on a solver, mesher, hardware, or case version absent from provenance.

The model may be retrained after a corpus correction, but the old artifact, dataset, and evidence hashes remain immutable evidence. A corrected corpus starts a new evaluation; it does not overwrite a failed gate.

## Current state

The PolyMesh learned mesh advisor is the only trained candidate. Its registered portable-cost artifact has a 36,010-row measured corpus and family-held-out evaluation. It is limited to linear-elastostatic mesh advice, retains published coverage gaps, has about 0.62 decades of primary-head family-held-out error, and misses 17.4% of measured failures at its 0.5 veto threshold. Chudware now carries the upstream runtime integration but keeps it explicitly opt-in and default-off: after selecting PolyMesh, normal GUI and MCP static, adaptive, modal, and transient-thermal FEA routes can send `learned_advisor: true` with an optional non-negative `advisor_max_dof` budget. Any returned choice is disclosed only as advisor output; the tet4/tet10 boundary, resource limits, mesher checks, solver health checks, and convergence gates remain authoritative. Transporting the static-trained advisor through modal or thermal mesh routes does not accredit those families or turn it into their optimizer. Its current evidence still does not show a decision win over the deterministic default, so it remains **candidate / no-go for promotion**.

Every other family is **gated**. No model, corpus, or performance claim is inferred from the presence of a shipped numerical solver.

## Dated family gates

Dates below are evidence-freeze and decision-review dates, not promises to ship a model. Missing evidence on the review date is a measured no-go. Cost improvements are measured against the source-hashed deterministic baseline on identical hardware and cases; accuracy and safety are evaluated on complete held-out groups.

| Family / registry track | Evidence freeze | Decision review | Measurable go gate (all clauses required) | Current decision |
|---|---:|---:|---|---|
| Static/adaptive FEA — `polymesh_mesh_advisor` | 2026-09-23 | 2026-09-30 | At least 55% wins among non-tied paired held-out-family cases; lower macro regret than default with paired one-sided $p \le 0.05$; failure pick rate no more than 2 percentage points above default; at least 90% of measured failures caught; 100% refusal of non-tet Chudware actions and over-budget actions; no loss of estimator/convergence vetoes. | **No-go:** trained candidate only; current default comparison is a non-result and the measured missed-failure rate is 17.4%. |
| Modal — `modal_optimizer` | 2026-10-23 | 2026-10-30 | P95 relative error across the first ten requested physical modes $\le 2\%$; 100% requested-mode retention; 100% rigid-body/non-positive-mode veto coverage; median solve cost at least 20% below baseline with no tail-error regression. | **Gated:** no registered corpus or artifact. |
| Thermal — `thermal_optimizer` | 2026-11-06 | 2026-11-13 | P95 temperature-functional error $\le 3\%$ on steady and transient held-out families; energy imbalance $\le 1\%$ on every accepted case; 100% non-finite/non-converged veto coverage; median cell-step cost at least 20% below baseline. | **Gated:** no registered corpus or artifact. |
| Fatigue — `fatigue_optimizer` | 2026-11-20 | 2026-11-27 | P95 life error $\le 0.15$ log10 cycles; 100% rejection of unconverged source stress, unproven S-N data, static overload, and raw singular peak-stress labels; median upstream solve cost at least 20% below baseline without worse conservative life error. | **Gated:** no licensed, provenance-complete life corpus. |
| Buckling — `buckling_optimizer` | 2026-12-04 | 2026-12-11 | P95 critical-load-factor error $\le 5\%$; 100% rejection of absent prestress, non-positive modes, and unsupported nonlinear/contact regimes; median solve cost at least 20% below baseline; no missed lowest physical mode. | **Gated:** no independently converged buckling corpus. |
| Contact — `contact_optimizer` | 2027-01-08 | 2027-01-15 | At least 95% of held-out topology cases converge with force-balance error $\le 2\%$ and registered penetration tolerance satisfied; 100% ambiguous-pair, negative-stiffness, and non-convergence veto coverage; median nonlinear iterations at least 20% below baseline. | **Gated:** no nonlinear contact corpus or tolerance evidence. |
| Frequency response — `frequency_response_optimizer` | 2027-01-22 | 2027-01-29 | P95 normalized response-curve area error $\le 3\%$; every reference resonance represented with peak-frequency error $\le 2\%$; at least 25% fewer sampled frequencies than baseline; 100% incomplete-basis and unresolved-resonance veto coverage. | **Gated:** no response-curve corpus or artifact. |
| LBM CFD — `lbm_cfd_optimizer` | 2027-02-05 | 2027-02-12 | P95 drag or pressure-drop error $\le 5\%$ across held-out geometries and registered Reynolds regimes; mass imbalance $\le 1\%$ on every accepted run; zero false passes for Mach/relaxation instability; median cell-steps at least 20% below baseline; CPU/CUDA decisions pass parity bounds. | **Gated:** no grid-converged conservation corpus. |
| OpenFOAM CFD — `openfoam_cfd_optimizer` | 2027-02-19 | 2027-02-26 | P95 registered flow-functional error $\le 5\%$; mass imbalance $\le 1\%$ and residual gate met on every accepted run; 100% version/case/boundary provenance; median wall cost at least 20% below the version-pinned baseline without selecting an unevidenced turbulence model. | **Gated:** no version-pinned optimizer corpus. |
| Motion — `motion_optimizer` | 2027-03-05 | 2027-03-12 | P95 normalized pose and joint-reaction error $\le 3\%$ on held-out physical assemblies; mate residual $\le 10^{-6}$ and force/moment balance pass on every accepted frame; 100% interference, missing-inertia, and trajectory-discontinuity veto coverage; at least 20% fewer evaluated frames at equal path accuracy. | **Gated:** no assembly-scale optimizer corpus. |

## Evidence package required at each review

A review packet is complete only when it contains:

- a corpus manifest listing every case, source/license, geometry and boundary-condition hashes, solver/mesher versions, deterministic seeds, machine provenance where cost is used, and omissions;
- the dataset SHA-256 and a split report proving complete group isolation;
- deterministic-baseline configuration and source hash plus replay results on the same evaluation cases;
- every attempted training run's configuration, seed, status, and selection rule, including failed runs;
- the immutable model artifact and model-card SHA-256 digests;
- paired per-case candidate-versus-baseline results, confidence interval or preregistered paired test, tail metrics, and resource measurements;
- a veto matrix with one positive and one negative fixture per veto and zero bypasses;
- runtime proof that unsupported or unavailable advice falls back without changing solver inputs or result labels.

No family borrows evidence from another. FEA mesh-advice evidence cannot accredit modal, fatigue, buckling, contact, thermal, motion, or CFD behavior, and OpenFOAM evidence cannot accredit the LBM implementation (or vice versa).
