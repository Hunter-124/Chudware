# Intake, Photo-Measurement & Manufacturing-Clarification Subsystem — Build-Ready Plan

*Status: design writeup, 2026-06-26. No code changed — planning only, per "writeups first" while the GUI rework is in flight.*

**The ask (paraphrased):** when the user has a real-world part to incorporate and we can't find a standard reference model/datasheet, the agent should ask for photos and extract measurements, demanding user-supplied critical dimensions where fit really matters, so everything fits on the first try. Primary manufacturing is FDM 3D printing, but other processes must be considered and the user asked to clarify when ambiguous. All of it integrated into BOTH the MCP and the built-in agent.

This plan was produced by a 4-architect design panel (photo-metrology, critical-dim/fit, DfM/process, MCP+agent integration) → a 3-judge review (the FDM maker, the systems engineer, the skeptic) → an opus synthesis, all grounded in the real codebase. It is a companion to [GEOMETRY_LLM_REPRESENTATION_REVIEW.md](GEOMETRY_LLM_REPRESENTATION_REVIEW.md) — that doc fixes how geometry reaches the model; this one fixes how real-world requirements and fit constraints reach it.

> Note on `clarify`: today's `core/agent/clarify.{h,cpp}` is a one-shot, never-block, text-only pre-flight. This plan **replaces it as a standalone gate** (folding its prompt + never-block fallback into a multi-turn intake module), because fit/photo/process elicitation is fundamentally multi-turn. You green-lit reinventing it.

---

# AI-CAD Intake & First-Try-Fit Subsystem — Build-Ready Plan

**North star:** the parts that *matter* fit on the first try. We spend metrology, photo asks, and "go measure with calipers" demands **only** on dimensions that gate a real mating interface. Everything else gets a sensible default and a flag.

**One-line architecture (the decision that frames everything else):** This is **one subsystem**, not four features. There is **one canonical schema**, **one shared decision module** both surfaces call, **one persistence seam** (cloned from `studies`), and **one pause/resume mechanism**. The MCP tools and the in-app agent stage are *thin adapters* over that core. Parity means **identical schema + gate + scorer + process-math** — explicitly **not** identical measured numbers from two different vision models (that is unachievable and we do not promise it).

---

## 0. What the code says today (grounded)

Verified against the tree so the plan rests on reality, not assumption:

- **`clarify` is a one-shot, never-block, text-only pre-flight.** `clarify.h` returns `{ready, questions[<=4]{key,ask,unit,suggested}, assumptions[]}`; on any parse failure `ready_now()` returns `ready:true` (clarify.cpp:96, 117). No photos, no fit/tolerance/process reasoning.
- **The agent machine is fire-and-finish.** `StageOutcome.verdict` is only `"pass"|"retry"|"fail"|"skip"` (machine.h:38); the only external interrupt is `cancelled()` (machine.h:75). Stages are linear: `intake, plan, model, review, fix, memory` (run.cpp:361). **There is no suspend/resume.**
- **`studies` is the persistence template to clone.** FEA/CFD setups live as an opaque top-level `studies` array in the doc JSON; the core only appends and never rebuilds geometry from it (session.cpp:3223+). This is exactly how our four records should persist — and is why an MCP-built part reopens in-app with references intact, for free.
- **MCP `Image` is OUTPUT-only.** `render_part` *returns* `Image` (cad_tools.py:883); **no tool ingests pixels**. So in MCP, the external LLM measures in *its own* vision context and reports structured **claims**; tools never receive raw photos.
- **In-app vision exists but is wired only to renders.** `image_part(path)` (providers.cpp:23) is called solely with `state.last_render` (run.cpp:243-244). Feeding *user photos* into the run is **net-new plumbing** (host file-intake → `%APPDATA%` path → `DesignState` photo field → provider image part), not "reuse."
- **Standard-parts-first chain is real.** `list_standard_parts`/`make_standard_part`, `engine/parts_library.py`, `engine/vendor_parts.py`, `engine/parts_providers.py` already do 3-tier lookup. We wrap it, we do not rebuild it.
- **Process assumptions are hard-coded and wrong for FDM.** `planner.md` bakes generic anchors ("slip fit 0.1-0.3, press fit ~0.05"); `wall_thickness`/`draft_analysis` carry literal minima. This is a *real current bug* a press-fit at 0.05 mm interference is unprintable.

---

## 1. End-to-end user journey (one worked example)

**User has a real gearmotor-bracket part we have no model for. Manufacturing = FDM.**

> **User (in-app Copilot or via Claude/MCP):** "Make a 3D-printed wall bracket that holds this little gearmotor I have. I don't have a model for it. It bolts on with two screws and the output shaft pokes through."

**1. INTAKE — normalize.** Existing `intake.md` produces `{object:"gearmotor bracket", key_dims:{}, features:["hold gearmotor","2 mounting screws","shaft passthrough"], constraints:["3D printed"]}`.

**2. REFERENCE-FIRST — look before asking.** `search_reference_data("gearmotor")` runs the 3-tier chain (`parts_catalog_list` → `vendor_parts.index` → `parts_providers.fetch`). The two screws → resolved as standard M2 self-tap (no ask). The gearmotor → **miss**. Only the miss flips it to *real-world-part-no-model*. We never photograph an M5 SHCS.

**3. PROCESS RESOLUTION (early, deterministic).** `resolve_process(spec)`: prior = FDM; explicit cue "3D printed" → FDM, high confidence, runner-up far below margin → **ambiguity gate does NOT fire**. ProcessProfile = FDM/PLA recorded as an *assumption* (`assumed:true`). This binds the tolerance band that all later clearance math uses.

**4. CRITICAL-DIM LEDGER — which numbers gate fit.** The mating-feature **taxonomy** classifies: `body_pocket` (prism, fit=clearance), `output_shaft` (shaft-in-bore, fit=slip), `mount_holes` (hole_pattern, fit=positional). Each archetype's `critical_keys` are seeded `unknown`. Overall bracket size → **cosmetic** → default 80×60, flagged, never asked.

**5. PHOTO REQUEST with scale.** `request_part_photos` emits a prioritized, templated ask + a **static printable scale-card PDF** (ruler/coin acceptable too):
> "I couldn't find a standard model, so I'll measure from photos. The three things that actually have to fit: the motor body pocket, the output-shaft bore, and the 2 screw holes + their spacing. Send: (1) a TOP photo of the motor flat with the scale card in frame, (2) FRONT + SIDE for height, (3) close-ups of the shaft end and the mounting-hole face with the card in the **same plane**. For the shaft and the hole spacing I'll need caliper numbers — have calipers ready."

**6. PAUSE.** Run suspends with `needs_user_input` (status `awaiting_user`); IntakeState persists in the doc. *In-app:* a drag-drop photo card + caliper-annotation overlay appears. *MCP:* the external LLM relayed the ask; the user pastes photos into the chat on their next turn.

**7. MEASUREMENT EXTRACTION + CONFIDENCE.** Vision reads dims; the **deterministic scorer** assigns confidence from concrete signals (scale source, in-plane vs out-of-plane, cross-view agreement, edge crispness): body envelope 24.1×31.0×19 mm → **high** (ArUco/card + 2 views agree, ±0.3); shaft Ø ~3.0 → **low** (single soft-edge close-up, ±0.4); hole spacing ~17 → **medium**.

**8. CRITICAL-DIM CONFIRMATION (the demand rule).** Shaft is a slip interface → required tier `measured`; achieved `low` → **DEMAND**. Hole spacing for a 2-screw locate → required `high`; achieved `medium` → DEMAND. The two are surfaced as **typed-value** confirms (photo is corroboration only — we never trust OCR on the one dim that decides fit):
> "Two I won't guess because they decide whether it bolts on: the output-shaft diameter and the exact hole spacing. Put calipers on each and type the numbers."
>
> **User:** "Shaft 3.00 mm. Holes 18.0 mm center-to-center."

Ledger now all `≥ required_tier`. **No process ambiguity → no process question asked.**

**9. FIT-BUDGETED GEOMETRY (build the receptacle, not a clone).** The planner receives a **PROCESS BLOCK** (appended like the existing STANDARD HARDWARE CATALOG) with FDM numbers, and commits **stacked** dims:
- shaft passthrough = 3.00 + FDM clearance 0.4 = **Ø3.4**
- body pocket = 24.1×31.0 + print tol = **24.5×31.4**
- screw holes = M2 self-tap at **18.0 mm** spacing (positional, no clearance added)

**10. MODEL → REVIEW → FitReport (+ optional coupon).** REVIEW runs `dfm_check(process=FDM)`: walls vs 1.0 min → pass; overhang flag on the pocket lip → "add 45° chamfer or it needs support." A **FitReport** lists every mating pair with source/provenance/margin/verdict. The single tightest interface (the shaft bore + hole pattern) is offered as a **fit-test coupon** — a ~20-min, ~5 g printable mini-part of just that interface — so the user verifies real fit before printing the full bracket.

**11. FIRST-TRY FIT.** Coupon seats → user commits → full bracket fits the real motor on the first print. Every committed dim shows provenance (`measured` vs `photo-high`) so REVIEW could have warned if anything critical were still soft. The part reopens in-app later with the full ledger + FitReport intact (records live in the `.step+` doc).

**Contrast (gate FIRES):** "Make a 2 mm steel plate, 100×60, four M4 threaded holes, must be flat." `resolve_process`: constant 2 mm → sheet-metal cue; threaded-in-steel + flatness → CNC cue; two strong non-FDM cues, no quantity → **ambiguous** → one process question: *"Sheet-metal (laser+bend) or CNC from plate? [suggested: CNC]."* User answers CNC → CNC profile drives tapped M4 (no insert), 0 draft, tight fit table.

---

## 2. Shared data structures (ONE canonical schema)

**Persistence rule (the linchpin):** all four records are **opaque top-level keys in the document JSON**, cloned byte-for-byte from the `studies` machinery (session.cpp:3223+). The C++ core assigns only `id`s and never rebuilds geometry from them. They ride inside `.step+` automatically → an MCP-built part reopens in-app with references intact, and **resume is stateless across process restarts** because state is doc-resident. **Schema lives in ONE place** (`engine/intake.py` + a shared C++ header); Areas/tools import it, never redefine it.

### `measured_parts[]` — MeasuredPartProfile
```jsonc
{
  "id": "mp_1", "label": "gearmotor", "source": "photo",   // photo|vendor|catalog|user
  "images": [{"handle":"p1","view":"top","scale_ref":{"method":"card|ruler|coin|caliper_typed","px_per_mm":12.4,"in_plane":true}}],
  "envelope": {"x_mm":24.5,"y_mm":31.4,"z_mm":19.0,"confidence":"high","uncert_mm":0.3},
  "features": [    // named mating features (taxonomy archetypes); these are the things that must fit
    {"key":"output_shaft","archetype":"shaft_in_bore","dia_mm":3.00,"fit":"slip",
     "confidence":"measured","uncert_mm":0.05,"provenance":{"method":"caliper_typed","photos":["p3"]}},
    {"key":"mount_holes","archetype":"bolt_pattern","count":2,"spacing_mm":18.0,"dia_mm":2.2,
     "confidence":"measured","provenance":{"method":"caliper_typed"}},
    {"key":"body_pocket","archetype":"prism","x_mm":24.1,"y_mm":31.0,"fit":"clearance",
     "confidence":"high","uncert_mm":0.3,"provenance":{"method":"card_homography","photos":["p1","p2"]}}
  ],
  "datum": {"origin_face":"mount_face","axis":"+Z"}   // so the modeler seats it without a measure round-trip (mirrors make_standard_part)
}
```

### `process` — ProcessProfile (one object; default FDM)
```jsonc
{
  "process": "fdm",            // fdm|cnc_mill|sheet_metal|injection|lathe|laser
  "confidence": 0.86, "runner_up": "cnc_mill", "assumed": true, "chosen_by": "model",
  "material": {"default":"PLA","density_g_cm3":1.24},
  "params": {
    "min_wall_mm":1.0, "min_feature_mm":0.8,
    "hole_comp_mm":0.20,                              // added to printed hole Ø; 0 for CNC/laser
    "tolerance_band_mm":0.40,                         // general ±; feeds every clearance
    "fit_classes_mm": {"slip":0.40,"location":0.20,"press":0.10,"free":0.50},  // FDM diametral; see §5
    "draft": {"required":false,"min_deg":0.0},
    "overhang": {"max_deg":45,"max_bridge_mm":5},
    "thread": {"strategy":"heat_set_insert","printed_ok_min":"M5"}
  },
  "why": "explicit '3D printed' cue; no metal/tolerance/quantity signals"
}
```

### `crit_dims[]` — CriticalDimensionLedger (the gate's source of truth)
```jsonc
{
  "id":"cd_1","key":"output_shaft.dia","feature":"output_shaft","archetype":"shaft_in_bore",
  "is_critical": true,                                  // = participates in a mate (deterministic, from taxonomy)
  "required_tier":"measured",                            // scales with fit + process (§5)
  "status":"unknown|given|measured|standard|assumed|stacked",
  "resolved_by":"user_confirmed|vision_high|reference|assumed",
  "nominal_mm":3.00, "fit":"slip",
  "stackup": {"clearance_mm":0.40,"modeled_mm":3.40,"rationale":"FDM slip"},
  "blocks_features":["shaft_passthrough"],              // geometry forbidden while unresolved+critical
  "why_it_matters":"the shaft won't pass if this is wrong", "suggested": 3.0
}
```
Computed views (not stored): `unresolved_critical()` = rows where `is_critical && status==unknown`; `assumed_flags()` → FitReport assumptions.

### `fit_report` — FitReport (the review artifact)
```jsonc
{
  "checks": [
    {"feature":"shaft_passthrough","modeled_mm":3.40,"source_mm":3.00,"fit":"slip","clearance_mm":0.40,
     "source":"measured","confidence":"measured","verdict":"ok"},     // ok|tight|risky|unmeasured
    {"feature":"body_pocket","modeled_mm":[24.5,31.4],"source":"photo-high","verdict":"ok"}
  ],
  "assumptions": ["bracket overall size 80×60 (cosmetic)","process FDM/PLA assumed"],
  "process":"fdm", "overall":"pass",                    // pass|at_risk
  "coupon": {"offered":true,"interface":"shaft+holes","stl_path":"...","est_print_min":20}
}
```

### `intake` — IntakeState (the resume token)
```jsonc
{"status":"open|awaiting_user|resolved","needs":[{"kind":"photo|dimension_confirm|process_clarify","key","ask","unit","suggested"}],
 "pending_request_id":"req_7","doc_id":"...","revision": 42}
```

---

## 3. Decision gates + agent state machine (pause/resume)

### The gates (in order)

1. **Reference gate** — `search_reference_data` runs FIRST. A hit resolves the dim from the catalog (no ask). A standard match on a *user's real part* gets a **one-click confirm** ("Assuming standard USB-C, 8.94 mm — correct?"), never silent resolution.
2. **Process gate** — `resolve_process`. FDM prior; **ask only when a non-FDM cue scores *within margin* of FDM**, or two non-FDM tie, or material is impossible for the leading process. Otherwise record assumption and proceed. **One** process question, sharing the `≤4` clarify budget.
3. **Critical-dim gate** — `unresolved_critical()` must be empty before fit-critical geometry commits. **Criticality is deterministic** (taxonomy `critical_keys`), not an LLM vibe. Cosmetic-unknowns commit with defaults + a flag; they never block.

### The one policy that resolves the panel's biggest contradiction

**Default = never-block-the-run.** A missing critical dim produces **one clear ask**; if unanswered, the run proceeds with a recorded low-confidence assumption and a **loud FitReport warning** (`overall:"at_risk"`). This preserves clarify's never-block ethos (clarify.cpp:96) and the user's "never wedge the run" rule.

**Narrow MUST-BLOCK list (the only hard stops):**
- Material impossible for the chosen process (metal + FDM; tapped thread expected in plastic).
- A **press/bearing/slip fit resting on an unmeasured bore** *only when the user has opted into "strict fit."*

Strict-fit is an opt-in toggle, off by default. This keeps the autonomous loop from ever deadlocking while still giving the expert user a hard guarantee when they want one.

### State machine changes (`machine.{h,cpp}`, `run.cpp`)

- **Add verdict `"pause"`** to `StageOutcome.verdict` (currently `pass|retry|fail|skip`, machine.h:38).
- **Add a resume input channel** to `RunControl`: `std::function<ojson()> take_user_input` (returns answered needs, or null). Mirrors the existing `cancelled()` callback pattern (machine.h:75).
- **On a `pause` verdict**, the machine emits `{type:"needs_user_input", needs, request_id, blocking}` and **unwinds to the host** returning `status:"awaiting_user", stop_reason:"needs_user_input"`, with IntakeState already persisted. **No long-lived blocked thread** — the unwind+rehydrate approach is stateless across restart and structurally identical to how MCP works across turns (this is what makes parity real).
- **Resume** = host re-invokes `agent_run_design` with `options.resume={request_id}`; the INTAKE action rehydrates `intake`/`crit_dims`/`process` from the doc, applies answers, recomputes the gate, and proceeds to PLAN.

### Where geometry is blocked

- **In-app:** the gate is a sub-step of INTAKE, re-checked at the **INTAKE→PLAN** boundary. **v1 = pre-PLAN only.** (Mid-MODEL discovery — pausing when a snap-fit is realized during modeling — is deferred, but the schema does not preclude it: `crit_dims` rows can be added mid-run later.)
- **MCP:** `add_features`/`make_standard_part` consult the **same** gate on the persisted ledger; a feature touching an unresolved critical returns `{ok:false, code:"intake_required", blocking:[...], next:"confirm_dimensions"}` instead of building wrong geometry.

---

## 4. MCP tool surface + built-in agent stage (parity contract)

**Parity is a contract, not a coincidence:** both surfaces call **one shared module** (`engine/intake.py` for resolve/classify/plan/score/gate; the C++ agent reaches it via the existing `core_bridge`/ctypes seam exactly as `run.cpp` already calls doc ops). The records persist through the **same** doc RPCs into the **same** `.step+` keys. **The only divergence is image transport and UI.** A parity-gate test asserts: *given identical CLAIMS + doc + process*, both surfaces produce byte-identical needs/ledger/FitReport. Vision *extraction* is explicitly excluded from the parity guarantee.

### MCP tools (new, in `mcp_server/cad_tools.py`, JSON-string returns like existing tools)

| Tool | Args | Returns |
|---|---|---|
| `search_reference_data` | `query, part_number?, supplier?` | `{found, candidates[{source,key,label,specs,make_call}], miss_reason}` — the mandatory front door (wraps the 3-tier chain). |
| `resolve_process` | `request_or_spec, material?, quantity?, doc_id?` | ProcessResolution `{chosen, confidence, runner_up, ambiguous, clarification?{ask,suggested,options}, assumptions}`. If `ambiguous` the LLM asks; else may proceed. |
| `set_process` | `process, material?, doc_id?` | Binds ProcessProfile to doc; returns resolved profile (fit table, min_wall, thread strategy). |
| `request_part_photos` | `label, critical_features[], fit_types{}, process_hint?` | A **content list**: TextContent ask-script + an ImageContent example-framing diagram + the **static printable scale-card** path + the seeded ledger with required tiers. Creates a durable `pending_request_id`. **Does not block.** |
| `submit_part_photos` | `request_id, claims[{feature,value_mm,kind,view,scale_source,in_plane,agrees_across_views,edge_quality,caliper_readout?}], exif?` | LLM reports its in-context measurements + signals; tool computes confidence **deterministically** and returns the MeasuredPartProfile with `open_demands` populated. |
| `confirm_dimensions` | `doc_id, confirmations[{key,value_mm}]` | Closes demands via typed values (`resolved_by:user_confirmed`); returns `{resolved, still_blocking}`. |
| `intake_status` | `doc_id?` | `{status, needs, blocking_critdims, measured_parts, process}` — the poll/recover tool; idempotent on `request_id`. |
| `dfm_check` | `doc_id?, process?` | FitReport punch-list: `eval_thickness`/`eval_draft` against **profile** thresholds + FDM overhang/min-feature checks + fit reconciliation. |
| `fit_report` | `doc_id?` | The persisted FitReport (proves first-try fit before export). |
| `make_fit_coupon` | `interface_id, doc_id?` | Builds the mini test-part from stacked dims; returns STL path (reuses `export_part`). |
| `cad_guide("intake")` | — | New cheatsheet teaching: lookup-first → resolve_process → request/submit photos → confirm → gate → report. |

**The photo handshake in MCP (honest):** there is no inbound pixel tool and we don't build one. `request_part_photos` *returns* the ask + scale card → the human attaches photos in their Claude client → the LLM reads them natively → the LLM calls `submit_part_photos` with structured **claims**. Receipt is via the LLM's own vision, never a binary upload. This is symmetric with `render_part` returning an `Image` outward.

### Built-in agent (reuse vs replace clarify)

- **REPLACE** `clarify` as a *standalone one-shot gate*: its prompt/normalization folds into `intake.py`'s elicitation planner (text-only path becomes a degenerate case). **PRESERVE** its `ready_now()` never-block fallback as the weak-model behavior.
- **REPLACE** the host's "fold answers into the request string and re-launch" hack with a **true resume** (writes IntakeState via RPC, continues the suspended machine). String-folding loses the structured ledger and can't carry photos.
- **REUSE** the `studies` persistence machinery, the 3-tier lookup, `make_standard_part`'s datum-return shape (MeasuredPartProfile carries the same `datum` so the modeler seats it identically), the planner's appended-reference mechanism for the PROCESS BLOCK, and `image_part` — **but** point it at user-photo paths, which requires the **new** host file-intake plumbing (dropzone → `%APPDATA%/AI-CAD/intake/<doc>/` path → `DesignState` photo field → provider image part).
- **GUI:** generalize `drawClarifyCard` into a **needs card** that renders by `need.kind`: photo dropzone + caliper-annotation overlay (draw a line, type its length → `px_per_mm`); dimension-confirm (vision value + confidence, editable, Confirm/Correct); process picker (radio with model's pick pre-selected + "why"). Submit folds answers into IntakeState and resumes the run.

---

## 5. Manufacturing-process model (FDM-default + ambiguity rules)

**One Python module** (`engine/dfm/process_profiles.py`) owns the table + the rule-based resolver; both surfaces call it. It **replaces** the hard-coded prose in `planner.md` and the literal minima in `wall_thickness`/`draft_analysis` (which become raw primitives parameterized by profile thresholds).

### FDM-default clearances (the load-bearing numbers)

| Param | FDM (default) | CNC | Sheet | Injection | Laser |
|---|---|---|---|---|---|
| `min_wall_mm` | 1.0–1.2 | 0.5/tool | gauge | 1.0 | n/a |
| `hole_comp_mm` (added to Ø) | 0.20 | 0 | 0 | shrink% | kerf |
| `tolerance_band_mm` (±) | 0.30–0.40 | 0.05 | 0.10 | 0.10 | kerf |
| slip / location / press (diametral) | 0.40 / 0.20 / 0.10* | 0.05 / 0.02 / interf. | — | — | — |
| draft | none | none | — | **required 1–3°** | none |
| thread | **heat-set insert** (printed ≥M5) | tapped | — | molded | — |
| overhang flag | **>45°** | — | — | — | — |

\* FDM press fits are unreliable → steer to **heat-set insert / capture / clearance+fastener**, not interference.

**Required-tier scaling (drives the demand rule):** press/bearing/threaded interface → `measured` (typed caliper number); slip/clearance fit → `high`; cosmetic/bulk → `medium`. A photo dim feeding a tolerance-sensitive fit gets its fit class **downgraded one step** (press→location) and flagged "confirm critical dim" — unless a typed value closes it.

### Ambiguity-clarification rules (when to ask about process)

- **Default prior = FDM.** Bias to NOT asking — the user's primary method.
- **Short-circuit (no question):** an explicit process keyword ("print", "machine from aluminum", "laser cut") or an explicit material that pins the process.
- **Gate FIRES (one question):** (a) a non-FDM cue scores *within margin* of FDM with no explicit material/quantity; (b) two non-FDM cues tie (sheet-metal vs CNC); (c) **material impossible for leading process** → this one is **must-block**, not a soft ask.
- **Cue table (rule-based, explainable):** constant-thickness + bends/flanges → sheet_metal; threaded-in-metal + tight-tolerance/flatness → cnc_mill; body-of-revolution turned features → lathe; draft present OR qty ≥ ~1000 OR "molded" → injection; flat profile cut, "gasket"/"panel" → laser.
- **One process question max**, sharing the `≤4` budget, prepended when present.

**Calibration is a v1 requirement, not polish:** the FDM numbers are printer/material-specific and the expert user *will* notice a loose fit. Ship a **per-printer override** (XY-comp + hole-comp) persisted in **app config** that feeds `ProcessProfile.params`. Lead the FitReport with the assumed band so a wrong guess is *visible and one-click-correctable*, turning the calibration gap into a transparent assumption rather than a silent miss.

---

## 6. Staged roadmap (most first-try-fit value, least build — GUI rework is in flight)

Treat as **one subsystem in gated layers**, never four parallel tracks. Effort/value are relative.

### Gate 0 — The spine (build FIRST; everything depends on it)
**Value: enabling · Effort: M-H · Risk: highest**
1. **Freeze the ONE canonical schema** (§2) in `engine/intake.py` + shared C++ header. No tool or RPC is written before this.
2. **Clone `studies` persistence** for `measured_parts / process / crit_dims / fit_report / intake` (opaque top-level doc keys; new `doc_intake_*` RPCs mirroring `doc_sim_study_*`).
3. **Build the bare pause/resume handshake** — `pause` verdict + `RunControl.take_user_input` + unwind-to-host + rehydrate-from-doc + the host resume entry point that **replaces** the string-fold hack. **Prove it end-to-end with a TRIVIAL TEXT question** (and the stale-doc guard) before any metrology lands on top.

*Why first: the machine is provably linear/fire-and-finish; without a real suspend, every gate degrades to today's pre-flight-only clarify. And four schemas built in parallel guarantee drift that kills parity on day one.*

### Gate 1 — Process awareness + the fit brain (highest value-to-cost)
**Value: high (improves EVERY part, zero photos) · Effort: M · Risk: low-med**
1. **`engine/dfm/process_profiles.py`** — table + rule-based `resolve_process`. Inject the **PROCESS BLOCK** via the planner's existing append mechanism. **Parameterize** (don't rewrite) `eval_thickness`/`eval_draft`; add `dfm_check`. Ship the **per-printer override**. Add `test_process_resolve.py` (~15 canonical prompts, script-style per project convention).
2. **Mating-feature taxonomy + CriticalDimensionLedger gate** (the deterministic critical/cosmetic brain replacing clarify's opaque `ready:bool`), riding Gate 0's pause/resume and Gate 1's process numbers.

*This delivers first-try fit for the LARGE class of parts that reference standard parts (NEMA17, 608, M3) and need no photos at all — fit correctness with zero metrology risk. `search_reference_data` is the mandatory front door.*

### Gate 2 — Photo metrology (highest risk; narrow it)
**Value: high (but only for the no-model case) · Effort: H · Risk: high**
1. Deterministic **confidence scorer** + demand rule + the four photo tools (`request_part_photos` / `submit_part_photos` / `confirm_dimensions` / `intake_status`).
2. In-app: the **new** host photo-intake plumbing + the needs-card photo dropzone/caliper overlay.
3. **Policy, committed (not an open question):** any `measured`-tier critical dim **requires a user-typed caliper number**; the photo is corroboration only. This sidesteps both the "in-app vision can't read calipers" risk and the cross-surface extraction-parity problem in one move.

### Gate 3 — Fit-test coupon (ship it; do not cut)
**Value: high ROI for FDM · Effort: S-M · Risk: low**
`make_fit_coupon` scoped to the **three easy archetypes** (bore, bolt-pattern, connector cutout). Converts an uncertain photo measurement into a 20-min / 5 g physical verification — arguably the single best first-try-fit guarantee in the packet. Derived from the same stacked dims + `export_part`.

---

## 7. Honest risks + what NOT to build

### Top risks (and mitigations)
1. **Pause/resume is the long pole and the keystone.** It touches `machine.{h,cpp}` + `RunControl` + `run.cpp` + host UI + doc rehydration, on a loop the memory already flags for stale-doc/multi-doc bugs. → **Build it first, in isolation, prove it on the existing text clarify, with the stale-doc guard, before anything depends on it.**
2. **Schema drift.** Four definitions of the same records = parity dead on arrival. → **One frozen schema in one module (Gate 0); all surfaces import, never redefine.**
3. **Vision parity is aspirational as literally stated.** The in-app provider (possibly a weak/local model) will not read a ruler as well as Claude in MCP. → **Redefine parity as identical SCHEMA + GATE + SCORER + PROCESS-MATH given identical CLAIMS; require typed values for measured-tier dims so the dim that matters most is provider-independent.**
4. **Uncalibrated FDM numbers.** A press at 0.1 mm or slip at 0.4 mm will be wrong on a fraction of printers; the expert user will notice. → **Ship the per-printer override in v1; lead the FitReport with the assumed band (visible, one-click correct).**
5. **Over-asking kills trust** (the user wants ship-builds, not interrogation). → **Standard-lookup-first is a hard precondition, not a suggestion; criticality is taxonomy-deterministic; cosmetic-unknowns silently default-and-flag; photo ask is the LAST resort after lookup and a plain typed-number question; `≤4` cap inherited.**
6. **MCP resume across long gaps** vs autosave doc-id reassignment (`_DOC_ALIASES`, a known live failure class). → **Bind the pending request to doc-resident IntakeState; make `submit_part_photos` idempotent on `request_id`; `intake_status` recovers state; verify the alias chain in a test.**

### What NOT to build (cut from v1)
- **ArUco/checkerboard generator + homography rectification + any OpenCV-class dependency.** The active GPL/license audit (just removed gmsh to make the product sellable) makes a new heavyweight vision dep a real procurement cost. → Ship a **static printable scale-card PDF + a "shoot straight-on" guard**; ruler/coin/typed-caliper cover the realistic user. Homography is a v2 luxury.
- **EXIF-focal-length distortion priors.** Over-engineered for v1.
- **Mid-MODEL (in-loop) pause.** v1 gates **pre-PLAN only** — a stated, documented limitation. The schema must not preclude adding it later, but don't build the in-`MODEL`-loop suspend now.
- **Multi-part cross-referenced FitReports** (shaft of part A must match bore of part B). Out of scope for v1; schema leaves room.
- **A learned confidence/process model.** Hand-tuned deterministic rubric ships now (reproducible + parity-safe). Revisit only if it proves too conservative and over-asks.
- **An inbound MCP pixel tool.** Doesn't exist, doesn't need to — the external LLM's native vision is the transport.

---

**The single most important thing to get right:** the parity contract — *one canonical schema + one shared decision module both surfaces call, with pause/resume honoring it.* Build the schema and the pause/resume first; make the MCP tools and the in-app stage thin adapters over the shared core. Get this wrong and you have two subtly different products wearing one name; get it right and an FDM maker's no-model part fits on the first print, whether they drove it through Claude or the in-app Copilot.

Relevant files grounding this plan: `core/agent/clarify.{h,cpp}`, `core/agent/machine.{h,cpp}` (verdict set machine.h:38, `cancelled` machine.h:75), `core/agent/run.cpp` (stages :361, `image_part` :243-244), `core/agent/providers.cpp` (:21-23), `core/doc/session.cpp` (`studies` :3223+), `mcp_server/cad_tools.py` (`render_part`→Image :883), `engine/parts_library.py`, `engine/vendor_parts.py`, `engine/parts_providers.py`, and the new `engine/intake.py` + `engine/dfm/process_profiles.py` this plan introduces.