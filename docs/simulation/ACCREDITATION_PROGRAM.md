# Simulation Credibility and External Accreditation Program

**Status:** Proposed program; internal implementation is incomplete and no external approval has been obtained  
**Program owner:** Simulation Assurance Lead  
**Quality owner:** Quality Management Lead (role not yet staffed)  
**Effective date:** 2026-08-24

## 1. Boundary of the program

This program separates two bodies of work that must never be conflated:

1. **Internal computational credibility** is revision-bound evidence that a simulation family
   passed stated verification, validation, cross-solver, or field-correlation gates. It is
   represented by the `S0_unvalidated` through `S3_field_correlated` contract.
2. **External accreditation or certification** requires an independently assessed management
   system, agreed scope, competent people, controlled records, and an engagement with the
   relevant external body. No amount of self-authored code or internal V&V evidence completes
   that work.

The repository contains no licensed standards text. Test targets are independently derived from
public-domain physics, openly licensed benchmark material, or hand-entered facts with citations.
The official [ASME V&V 20 page](https://www.asme.org/codes-standards/find-codes-standards/standard-for-verification-and-validation-in-computational-fluid-dynamics-and-heat-transfer)
describes a framework for CFD and heat-transfer V&V; using its public description as program
orientation does not imply conformity. The team must acquire any standards it elects to use
through a lawful external process and may not copy their text into this repository.

## 2. Internal credibility contract

| Tier | Minimum registered evidence | What may be said |
|---|---|---|
| `S0_unvalidated` | No current, matching evidence | `unvalidated` |
| `S1_benchmarked` | A passing benchmark record for the exact solver and optional model revision | `internally benchmarked` |
| `S2_cross_solver` | S1 plus a current comparison against an independently sourced solver, linked by evidence id | `internally cross-solver compared` |
| `S3_field_correlated` | S2 plus a current, revisioned field or laboratory dataset linked by evidence id | `internally field-correlated` |

`services/sim/credibility.py` derives the tier; producers cannot assign one. Every record includes
an evidence id, simulation family, solver source, solver revision hash, optional model revision
hash, creation and expiry timestamps, outcome, and dependency evidence ids. S2 additionally
identifies an independent reference solver and revision. S3 additionally identifies a revisioned
measurement dataset. Expired, failed, invalidated, malformed, wrong-family, wrong-revision, or
incompletely chained evidence is ignored. An unreadable registry yields S0 and cannot be
silently overwritten.

Internal evidence expires at the earlier of its registered expiry or any of these events:
solver/model revision change, benchmark/reference change, material-model change, unit or mesh
contract change, discovered benchmark defect, unexplained regression, or field-instrument
traceability break. The family owner records a reason before re-running the evidence chain.

### Claim-language guard

Machine-generated credibility labels use a four-value allow-list. Labels containing a form of
“certify” or “accredit” are rejected, including at S3. Product, sales, UI, report, and API text
must not turn internal evidence into an external-approval claim. A third-party solver's status,
an individual's credential, or a laboratory's scope does not transfer to Chudware or to a
customer result.

## 3. Code-complete work versus accepted evidence

A merged implementation proves only that code exists. It does not prove a numerical result.
The following states are tracked independently:

| Deliverable | Code state | Evidence state at program issue | Completion rule |
|---|---|---|---|
| Registry and claim guard | Implemented in source | No production evidence registry is supplied; therefore S0 by default | Focused contract tests pass and a release owner approves the registry location/retention policy |
| General V&V harness | Existing | Existing reports include visible failures/skips; they are not promoted by this document | Current full run records exact sources/revisions and has zero unexplained FAIL or unexpected SKIP |
| PolyMesh/Polyhedral-FEA family | Harness cases implemented | Not executed or registered by this document | Both clean-room cases execute on the shipped binary, identify the binary hash and the pinned Polyhedral-FEA fork revision, and meet their declared tolerances |
| External QMS/accreditation | No qualifying repository implementation | Not started | Written external scope and decision from the relevant independent body |

The known FEA uniform-pressure failure remains a failure/xfail until its root cause is fixed and
the unchanged reference passes. It must not be removed, converted to a skip, or hidden in an
aggregate percentage.

## 4. Simulation-family evidence matrix

Every released solver family appears in this matrix. A family not yet backed by registered,
current evidence remains S0 regardless of nearby or similarly named evidence.

| Family | Required S1 benchmark set | Required S2 comparator | Candidate S3 observation | Accountable owner |
|---|---|---|---|---|
| Structural static FEA | Patch tests; axial bar; cantilever bending; pressure-load resultant; mesh/order convergence | CalculiX or another independently implemented structural solver | Instrumented strain/deflection coupon with uncertainty budget | Structural V&V Owner |
| Modal, buckling, fatigue, contact | Beam eigenfrequencies; Euler buckling; S-N reference; contact force balance and convergence | Independent eigensolver/CalculiX for each claimed mode | Modal hammer, buckling coupon, fatigue/contact rig as applicable | Structural V&V Owner |
| PolyMesh/Polyhedral-FEA integration | PM1 bending and PM2 axial clean-room references plus mesh-quality invariants | Same geometry solved on an independent discretizer; compare convergence, not one favorable mesh | Instrumented coupon across at least three mesh bands | Meshing V&V Owner |
| Thermal, including transient | 1-D steady conduction and transient slab series solution; conservation | Elmer or CalculiX thermal on the same boundary conditions | Calibrated thermal coupon with sensor uncertainty | Thermal V&V Owner |
| Native LBM CFD | Conservation and convergence cases; drag is trend-only until calibrated | OpenFOAM or another independently implemented CFD solver | Wind/water-tunnel dataset with geometry and fluid-property traceability | CFD V&V Owner |
| OpenFOAM and forked CFD backends | Laminar channel/pipe, manufactured solution where available, mesh/time-step convergence | NASA TMR/public benchmark comparator or a distinct solver | Facility dataset with inlet, turbulence, and instrumentation uncertainty | CFD V&V Owner |
| Hydraulics and piping networks | Pump sizing; Darcy-Weisbach; topology/mass conservation; property-range boundaries | EPANET/WNTR or pandapipes as applicable | Logged loop/plant data with calibrated pressure and flow sensors | Fluids V&V Owner |
| ECAD impedance and circuit/SPICE | Trace formulas; DC/AC/transient canonical circuits; convergence failures | ngspice or independently derived MNA result | Bench measurements with fixture/probe de-embedding notes | ECAD V&V Owner |
| Electromagnetics | Waveguide/cavity/antenna canonical cases; energy balance; mesh convergence | openEMS or Elmer as applicable | VNA/chamber dataset with calibration records | EM V&V Owner |
| Digital logic/FPGA simulation | Deterministic protocol and timing fixtures | Verilator plus GHDL/Icarus where language permits | Hardware-in-loop traces from identified bitstream/board revisions | Electronics V&V Owner |
| CAM/material-removal and cycle-time simulation | Stock conservation, swept-volume, kinematic limits, deterministic cycle-time fixtures | Independent post/verification result where available | Machine log and measured part/stock outcomes | Manufacturing V&V Owner |
| Assembly motion/interference | Analytic joint motion, exact overlap fixtures, tolerance-boundary cases | Independent geometric/kinematic calculation | Metrology or motion-capture fixture where the use case warrants it | Assembly V&V Owner |

PolyMesh is a mesher path into Chudware's supported tet4/tet10 solver contract. Its name and
fork lineage do not mean that Chudware executed a VEM or general polyhedral-element
solve. The benchmark report must state both the solver source and the exact linked-binary hash.

## 5. Measurable internal gates and milestones

Dates are targets, not completion statements. A gate closes only when its evidence ids are in
the registry and the evidence-review record names the approver.

| Gate | Measure | Owner | Target milestone | Current state |
|---|---|---|---|---|
| I0 — Registry integrity | 100% of tier derivations revision-bound; corrupt/expired/invalidated registries yield S0; forbidden claim-label fixtures rejected | Simulation Platform Owner | 2026-09-04 | Implementation present; acceptance run pending |
| I1 — Inventory | 100% of released simulation entry points mapped to exactly one family owner and S0-S3 state | Simulation Assurance Lead | 2026-09-11 | Not started |
| I2 — Reproducible S1 | Full harness records solver source/hash, reference id, units, tolerance, seed/environment; zero unexplained FAIL and zero unexpected SKIP on two clean machines | V&V Harness Owner | 2026-09-25 | Not achieved; known FEA pressure failure remains open |
| I3 — Cross-solver S2 | At least two geometrically/materially distinct cases per family; independent revision captured; differences within predeclared bounds; dependency chain registered | Each Family Owner | 2026-10-23 | Not started |
| I4 — Sensitivity/uncertainty | Mesh/time-step/order study has at least three levels; observed convergence and numerical uncertainty recorded for every scalar claim | Numerical Methods Owner | 2026-11-13 | Not started |
| I5 — Field S3 pilot | At least one predeclared dataset per candidate family; sensor calibration, uncertainty, exclusions, and residual metrics reviewed independently | Test Engineering Owner | 2026-12-18 | Not started |
| I6 — Release control | 100% of solver/model changes invalidate affected evidence before release; quarterly expiry review has no overdue active record | Release Quality Owner | First review 2027-01-08, then quarterly | Not started |

Recommended quantitative pass criteria are defined before observing results. At minimum: no
NaN/inf, conservation residual within the family limit, all declared tolerance checks pass,
convergence is monotonic or the exception is explained, and repeated runs reproduce within the
registered deterministic/nondeterministic band. Pass rates never erase an individual failure.

## 6. External QMS workstream

This workstream is organizational and externally assessed; repository work cannot close it.
The [ISO 9000 family overview](https://www.iso.org/standards/popular/iso-9000-family) is the
primary orientation source. Selection of ISO 9001 and any sector-specific extension must follow
market/scope analysis and licensed review, not assumptions in code.

| Milestone | Objective and measurable exit | Owner | Target | State |
|---|---|---|---|---|
| Q0 — Scope decision | Approved product/site/legal-entity scope; target markets and excluded uses listed; applicable QMS scheme chosen | Executive Sponsor + Quality Lead | 2026-09-18 | Not started |
| Q1 — Independent gap assessment | Contracted qualified assessor; 100% of applicable clauses mapped to owner/evidence/gap; remediation plan approved | Quality Lead | 2026-10-16 | Not started |
| Q2 — Controlled QMS | Document control, design control, risk, supplier control, training, nonconformance, CAPA, internal audit, and management review procedures operating with named owners | Process Owners | 2027-01-29 | Not started |
| Q3 — Record period | At least one complete internal-audit and management-review cycle; CAPAs have effectiveness checks; training records current | Quality Lead | 2027-04-30 | Not started |
| Q4 — External audit readiness | Independent readiness review finds no unresolved major gap; audit scope and body contract approved | Executive Sponsor + Quality Lead | 2027-05-28 | Not started |

Sector schemes such as aerospace or medical-device QMS are separate scope decisions. They are
not inherited from a generic quality program and are not implied here.

## 7. Accredited-body and laboratory engagement

The [ISO/IEC 17025 overview](https://www.iso.org/ISO-IEC-17025-testing-and-calibration-laboratories.html)
explains that the standard concerns testing and calibration laboratory competence. A laboratory's
accredited result may support a field-correlation evidence record only when the measurement is
inside that laboratory's current scope; it does not approve the application or solver.

| Milestone | Objective and measurable exit | Owner | Target | State |
|---|---|---|---|---|
| E0 — Claims/scope counsel | Written advice identifies intended claims, jurisdictions, schemes, prohibited interim language, and evidence-retention duties | Legal/Compliance Owner | 2026-09-25 | Not started |
| E1 — Body shortlist | At least three candidate bodies/laboratories assessed for recognized scope, competence, conflicts, geography, schedule, and cost | Quality Lead + Procurement | 2026-10-30 | Not started |
| E2 — Pre-application meeting | Written minutes confirm proposed product/QMS/lab scope, required records, exclusions, and whether the desired claim is available | Executive Sponsor | 2026-11-20 | Not started |
| E3 — Witness plan | Signed test plan fixes samples, revisions, uncertainty, acceptance criteria, chain of custody, retest rules, and report ownership | Test Engineering + External Body | 2027-02-26 | Not started |
| E4 — Assessment | All findings logged; zero open major nonconformities before any external-status claim | Quality Lead | Date set by contracted body | Not scheduled |
| E5 — Ongoing surveillance | Scope/status checked before every public claim; surveillance dates and corrective actions tracked; expired scope removes the claim immediately | Quality Lead | Only after a written grant | Not applicable yet |

NAFEMS publishes [code-verification and vendor-benchmark information](https://www.nafems.org/publications/code-verification/)
that may inform benchmark selection without bundling proprietary publications. NAFEMS PSE is an
individual professional competency scheme; its [code of conduct](https://www.nafems.org/professional-development/certification/pse_code_of_conduct/)
does not make a software product externally approved. Staff credentials must never be rendered
as solver status.

## 8. Evidence review record

Each gate review records: family and claim scope; solver/model/source revisions; evidence ids;
reference provenance and license; environment; tolerances fixed before execution; raw and
reduced outputs; failures/skips; reviewer; decision; expiry; invalidation triggers; and links to
corrective actions. Raw records are append-only. Corrections supersede rather than erase the
original result.

Until the relevant external workstream is completed and a current written scope is held, public
language is limited to factual solver identity, benchmark method, revision, measured result, and
the guarded internal S0-S3 label.
