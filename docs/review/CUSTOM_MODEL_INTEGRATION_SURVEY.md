# Custom Model Integration Survey — ShopCraft (AI-CAD)

**Status:** Exploratory directive executed 2026-07-02; decisions in §0 confirmed by Erik
same day. Companion to [AI_TOOLING_MASTER_PLAN.md](../AI_TOOLING_MASTER_PLAN.md) (pillars
8–15) — this survey adds **Pillar 16 (data flywheel & training pipeline)** and the
**Workstream H** execution map.
**Evidence base:** five parallel codebase surveys (AI surfaces, verifiers, formats,
telemetry, training scaffold) + one web-research pass (small VLMs, appendix A).

## 0. Scope decisions (asked & answered 2026-07-02)

1. **Telemetry vs the ratified synthetic-only decision:** *build capture now, train
   later.* Opt-in logging/flywheel plumbing ships with features; training sets stay
   synthetic-only until the T-F11 ship gate reviews harvested data. Master-plan
   decision #4 stands.
2. **Training fleet:** a pool of **our own machines, mixed Windows/Linux, in different
   states — NOT one LAN.** Combined with the "no cross-internet distributed training"
   constraint this fixes the design: **job-level distribution** (one whole training run
   per machine, coordinated through a shared queue + data remote over the internet),
   never multi-node single-run training.
3. **DAG placement:** new claimable **Workstream H** cut into `work/tasks/` now;
   decision-heavy items marked in-task as human-gated.
4. **Initial corpus:** our team's dogfooding first (implicitly consented, logged fully).
   External users get explicit opt-in that ships with the plumbing, **default OFF**.

Binding inherited rules (master plan §3): deterministic first, learned second; the model
never invents a numeral; every learned output degrades to a deterministic fallback;
small-model floor (usable by a 4B local model); offline/ITAR mode always coherent.

One rule this survey adds, from the directive's parity constraint:

> **R1 — Editability parity.** Every model output must land as an artifact of an
> existing human-editable document format (doc-ops JSON → feature tree, sketch entities,
> mates, requirements doc, drawing ops, deliverable specs) that the user can then edit
> **visually and numerically in the GUI with parametric relations intact**. A model may
> never introduce an opaque artifact class. Consequence (§3.4): converge generation on
> **doc-ops JSON** rather than free-form build123d scripts as the primary target.

---

## 1. Model insertion points

Notation: **I/O** = model input → output (output format must be human-editable text, R1);
**Lat** = interactive (<10 s), turn (10 s–5 min), background; **Init** = initial backend
(frontier API / small local / micro = <1M-param NNUE-style); **Verify** = deterministic
check(s) from §4.

### 1.1 Existing today

| # | Insertion point | I/O | Lat | Init | Verify |
|---|---|---|---|---|---|
| IP-1 | **Sketch/part generation (build loop)** — `services/agent/loop.py` + C++ `core/agent/run.cpp` | request + GSL/describe context + render → build123d script (Py path) or **doc-ops JSON** (C++ path) + decision JSON | turn | frontier | rebuild `feature_status`, `eval_check` (BRepCheck/manifold), `measure_part` vs requested dims, `check_feature_overlap`, render self-review |
| IP-2 | **Plan call** — `loop.py:_make_plan` | request → `{parts, dimensions, features}` JSON | turn | frontier | schema; downstream dims re-measured |
| IP-3 | **Clarify/ask gates** — `core/agent/clarify.cpp`, `ask.cpp` | state digest → question JSON `{question, options}` | interactive | small local | answered/unanswered; migrates to Pillar-9 `ask_user` |
| IP-4 | **CAM program fast-path assist** — deterministic `cam_program` exists; model chooses strategy/sequence/params | job tree + stock + GSL part text → CAM ops JSON | turn | frontier | `cam_simulate` (gouge_count, rapid_through_count, machined_fraction_pct), post-processor success |
| IP-5 | **Reverse-engineering candidate ranking** — deterministic 5-pass recognizer emits candidates; ranker orders them (= T-F13 micro-evaluator #1) | face/adjacency features → ranking | background | micro | `eval_compare` match_pct (free reward) |
| IP-6 | **ECAD assist** — `ecad_design`, autoroute params, placement | netlist/status text → ECAD ops JSON | turn | frontier | `ecad_run_erc/drc/dfm`, impedance checks, SPICE |

### 1.2 Planned in the master plan (restated here for completeness)

| # | Insertion point | I/O | Lat | Init | Verify |
|---|---|---|---|---|---|
| IP-7 | **State→text translator** (T-F10/T-F11) | gsl-ser/v1 state → narration | interactive | small local (the flagship custom model) | slot-fidelity harness, groundedness checker |
| IP-8 | **DFM severity ranker** (P13 stage-3 #2) | `dfm_check` findings → ranked | background | micro | rule engine is the label source |
| IP-9 | **Sketch-constraint suggestion ranker** (P13 stage-3 #3) | sketch state → candidate constraint ranking | interactive | micro | `check_sketch` (DOF, conflicts); mask-one-out self-supervision |
| IP-10 | **Agent next-action value head** (P13 stage-3 #4) | run state → action value | background | micro | `eval_compare` distance-to-goal |
| IP-11 | **Deliverables composition** (P15) | describer outputs → `make_report/presentation/workbook` spec JSON | background | frontier | schema; provenance joins (numbers only from describers) |

### 1.3 New insertion points identified by this survey

| # | Insertion point | I/O | Lat | Init | Verify |
|---|---|---|---|---|---|
| IP-12 | **Requirements elicitation** — vague prompt → editable **design brief** (`aicad.req/1`, §3.5; feeds T-F17's sign-off flow and T-BL09) | chat + attachments → requirements doc | turn | frontier | schema validation, unit/range checks, slot coverage vs conversation, req-ID join to verify nodes |
| IP-13 | **Spec → sketch generation** — brief section → constrained sketch | req doc + plane context → `sketch_add_entity`/`sketch_add_constraint` ops | turn | frontier→small | `check_sketch` (status=full, no conflicts), region closure, dims re-measured vs spec |
| IP-14 | **Part parametrization** — imported/dumb geometry → named params + `equations.globals`; also "make this dimension driving" | feature tree → rename/equation ops | background | frontier→small | rebuild invariance (geometry unchanged after re-expression), `compare_parts` ≈100% |
| IP-15 | **Assembly mate assistance** — propose mates from geometry + intent | `describe_assembly` + GSL → mate ops JSON | interactive | frontier→small | `solve_mates` convergence, DOF per component, `check_interference` |
| IP-16 | **Standards/parts lookup & substitution** — callout parsing, substitute suggestions | text/BOM row → standard-part params / `component_search` query | interactive | small + Pillar-8 tools | `make_standard_part` param validation, `thread_spec` formulas, datasheet quote substring-match |
| IP-17 | **Dimension/tolerance suggestion** — drawing dims placement, fit-driven tolerances | feature + fit intent → dimension/tolerance ops | interactive | frontier→small | solver, `fit_report`, tolerance-stack check (gap, §4.3), drawing dimension-coverage check (gap) |
| IP-18 | **Regeneration-error diagnosis** — failed rebuild → diagnosis + fix ops | `feature_status` errors + dangling refs + doc JSON → explanation + candidate fix ops | interactive | frontier → **first new custom-model migration (§6)** | apply fix → rebuild passes; nothing else changed (`compare_parts` on unaffected bodies) |
| IP-19 | **Naming/organization** — feature/part/config names, folder & BOM grouping | tree → rename ops | background | small local | uniqueness/schema only (weak reward → preference data) |
| IP-20 | **Drawing/documentation generation** — view selection, section placement, dimension schemes; manual/test-report text | doc + brief → drawing ops + P15 specs | background | frontier | `review_drawing` render, dimension-coverage (gap), req-ID evidence join |
| IP-21 | **Vision family (offline VLM)** — (a) render self-review for local models, (b) drawing/callout reading, (c) photo triage (feature ID only, never measuring), (d) render-vs-photo compare for recon | images ≤1024px + question → structured claims JSON (confidence-tiered, intake protocol) | turn | small VLM (appendix A) | render pipeline ground truth (we know the geometry behind every synthetic render); fiducial metrology for photos; claims schema |

The `experimental.vision_review` scaffold in `config.example.json` (default-off,
loopback-only) is already the right seam for IP-21 — it just has no model behind it yet.

---

## 2. Data flywheel audit

### 2.1 What already exists (strongest asset first)

1. **`events.jsonl` is 80% of a training-capture layer already.** Every C++ agent run
   persists `run_started / stage_started / agent_call (prompt_hash, context) /
   agent_output (raw ≤16 KB + uncapped sidecar, parsed_ok, duration) / stage_result
   (pass|retry|fail) / run_finished` to
   `<appdata>/AI-CAD/cache/runs/<run_id>/events.jsonl` (`core/agent/events.cpp`,
   `contracts/events.md`). Persist-then-callback, replayable, tested against. **A
   failed attempt followed by a passing retry is a free preference pair with a
   verifier-backed justification — being logged today, harvested by nobody.**
2. **Python loop iteration history:** per-iteration scripts
   (`agent_model_iter{i}.py`), IterationLog (ok/error/diff/dims/volume), contact-sheet
   PNGs (`services/agent/loop.py:650-704`). Correction traces in embryo.
3. **Intake ledger:** human answers with confidence tiers persist into the `.step+`
   doc — human-verified dimension ground truth, but no timestamps/audit trail yet.
4. **Undo/redo:** journals exist per domain (part revisions; CAM 50-snapshot; ECAD
   100-snapshot canonical JSON) but are **in-memory only** — the single best negative
   signal (undo-right-after-AI-apply) evaporates on close.
5. **Issue reporter** (`work/tools/issue_reporter.py`): proven pattern for structured,
   AI-elaborated human feedback → reuse its shape for in-product "flag this AI output".

### 2.2 What's missing

No accept/edit/reject affordances (or events) on AI output in the copilot; no persisted
doc-diff journal; no central export; no consent surface; no crash/exception events; no
"was this helpful" signal; intake ledger lacks who/when/why-rejected.

### 2.3 Telemetry schema (extends `contracts/events.md`; cut as T-H1)

Every AI output becomes an **artifact envelope**, every user reaction an **action event**:

```jsonl
{"kind":"ai_artifact", "artifact_id":"a7f3…", "run_id":"…", "insertion_point":"IP-13",
 "model":{"provider":"anthropic","model":"claude-fable-5","adapter":null},
 "prompt_hash":"…", "input_ref":"sidecar path", "output_ref":"sidecar path",
 "base_revision":17, "verifier_scores":{"check_sketch.status":"full","dof":0},
 "latency_ms":8250}
{"kind":"ai_artifact_action", "artifact_id":"a7f3…",
 "action":"accepted|applied|edited|rejected|regenerated|undone|flagged",
 "revision":18, "edit_ops_ref":"sidecar path (ops the user applied on top)",
 "undo_within_ops":0, "dwell_ms":41000, "note":"optional user text"}
{"kind":"doc_semantic_diff", "artifact_id":"a7f3…", "from_revision":17,
 "to_revision":21, "diff_ref":"sidecar path (feature-level diff, §3.3)"}
```

Session envelope: `{"consent":"off|meta|full", "origin":"team|external",
"app_version", "os"}`. `consent:"off"` ⇒ events still written locally for the user's
own replay (existing behavior) but the exporter refuses them.

**Derivations (mechanical, in the exporter):**

- **SFT examples** — `ai_artifact` + `accepted/applied` with no `edited` within the
  session ⇒ `(input_ref → output_ref)` pair, tagged by insertion point + verifier scores.
- **Preference pairs** — (a) `regenerated`: old output = rejected, new accepted output =
  chosen; (b) retry loops in events.jsonl: failed attempt vs passing attempt;
  (c) `undone` within K ops ⇒ negative vs any later accepted artifact for the same request.
- **Correction traces** — `edited`: model output + the user's follow-up ops diff =
  exactly the DPO/GRPO-ready "here's what a human changed" trace. Requires persisting
  undo/ops journal to disk (T-H1 DoD).
- **Elicitation gold** — design-brief sign-off (T-F17): model draft vs user-signed brief
  diff.
- **Intake gold** — ledger confirmations gain `{who, ts, superseded_value, reason?}`.

### 2.4 Consent & privacy design (opt-in, cut as part of T-H1/T-H5)

- **Three tiers:** `off` (external default) / `meta` (event kinds + verifier scores +
  hashes, no geometry, no prompts) / `full` (everything; team dogfood default).
- First-run consent card + Settings toggle; consent state stamped into every exported
  record; **offline/ITAR mode hard-forces `off`** and disables the exporter.
- Export is **user-initiated or visibly scheduled**, never silent: `flywheel export`
  bundles runs → local staging dir the user can inspect before upload (per-user local
  cache stays local — same posture as the Pillar-8 cache redistribution rule).
- Scrubbing in the exporter: strip API keys/paths/usernames, hash machine ids, drop
  attachments unless `full`. External `full` consent additionally requires per-project
  opt-in (a project may contain a customer's IP even if the user consents globally).
- Legal cleanliness (directive constraint): corpus = our telemetry (consented) +
  synthetic factory + licensed/OSS datasets; provenance field on every training record.

---

## 3. Intermediate-representation review

### 3.1 Verdict: the document layer is already a good model target

`aicad.part/1`, `aicad.asm/1`, `aicad.drawing/1`, `aicad.sch/1` are versioned,
text-JSON, feature-history-preserving, stable-ID'd (`f1/e1/c1/k1/m1/s1/w1…`), and
diffable; doc-ops is a clean declarative mutation vocabulary with batch placeholders
(`$0→f9` id_map). Drawings store definitions and recompute view geometry (associative —
correct). This is better than most commercial CAD as a training substrate.

### 3.2 Signal-degrading spots (fix list, cut as T-H2)

| Issue | Where | Fix |
|---|---|---|
| Float noise (`599.9999…`, 1e-16 centroid dust) | doc JSON quantities, query_geometry | **Canonical dump policy:** round to fixed decimals (µm for mm lengths, 1e-3 deg), applied at serialize time; already partially specced by GSL Stage 0 |
| Non-canonical key/array order in some emitters | ops responses | sorted-keys canonical dump everywhere (ECAD already does this for undo snapshots — generalize) |
| `owner_map` `[body, topo_id]` instability across rebuilds (W1) | ops-response | content-hash stable ids (GSL Stage 1c) — already planned; training data must key on sids, never topo ordinals |
| Unsigned `axis`, leaked `hint_index` | selectors/query_geometry | GSL Stage 0 emissions (side, signed normal, concavity, radius) |
| Timestamps inside `.step+ meta.json` | container | exclude meta.json from diffs; never put ts in `document.json` |
| `derived_from.doc_name` (user-renamable) | doc_ops §2.5 | treat uuid as key, name as display-only |
| Binary blobs: `.npz` results, `.aicm` overlays, PNG | `.step+` studies | fine — results are **derived** state; models never target them; exporter records scalar summaries only |
| `.step+` ZIP container | save path | fine for product; for training/PDM diffing, exporter extracts `document.json` (+ T-BL10 PDM-lite synergy) |
| Sheet-metal bend records in session ctx only | feature_params L265 | persist to doc if models ever target flat patterns |

### 3.3 New tool: semantic doc diff (T-H2)

`doc_diff(rev_a, rev_b) → {features_added/edited/removed, params_changed
[{feature, param, old, new}], sketch_deltas, mate_deltas}` — feature-level, not
line-level. Needed by: correction traces (§2.3), the `diff()` GSL surface, mission-
control "what the agent changed" cards, and PDM-lite later. Cheap: both revisions are
already canonical JSON.

### 3.4 Converge generation on doc-ops JSON (R1 consequence)

The Python loop's build123d scripts are human-editable *text* but not the format users
edit *visually* — a script-built part arrives as opaque geometry unless re-imported
through recognition. The C++ path already emits doc-ops. **Recommendation:** all
generation insertion points (IP-1, 12–18, 20) target doc-ops JSON / document schemas as
the canonical output; build123d remains a power-user escape hatch and the kernel for
the synthetic factory. This single decision is what guarantees "users can change it
visually and numerically with automatic relations" for every model output, and it
matches the field's convergence on executable-but-structured CAD representations.

### 3.5 Requirements layer — `aicad.req/1`

> **Normative definition moved to `contracts/requirements.md`** (T-H3, 2026-07-02).
> This section is now a summary pointer only. For the canonical schema, field rules,
> valid verifiers list, status ladder, persistence, and markdown projection spec —
> see `contracts/requirements.md`.

`aicad.req/1` is simultaneously the **elicitation target format (IP-12)**, the
**T-F17 design-brief sign-off format**, the **verify-node contract (WS-A)**, and
the **eval spec for spec→sketch/part benchmarks (T-H8)**.

Key rules (normative in `contracts/requirements.md`):
- Only `quantity` fields carry numbers; `text` is prose.
- Every requirement names ≥1 deterministic verifier from `VALID_VERIFIERS`.
- `user_confirmed` requires a Pillar-9 `ask_user` answer or explicit user edit.
- `verified`/`failed` are set by automated verifiers; `waived` by the user only.
- Markdown projection is a deterministic read-only view (never parsed back).

MCP tools: `req_create`, `req_update`, `req_status`, `req_report`
(`services/mcp/req_tools.py`).  Pure brain: `services/requirements/requirements.py`.

---

## 4. Verifier inventory → reward map

Full 30-verifier inventory with paths lives in the survey evidence; operative summary:

### 4.1 Subsecond (reward-loop safe — can score every RL rollout)

`check_sketch` (status/DOF/conflicts) · `measure_part` · `measure_between` ·
`query_geometry` · `describe_part/sketch` (GSL) · `fit_report` · `list/make_standard_part`
param validation · `ecad_run_erc` · impedance checks · `eval_check`
(BRepCheck_Analyzer + free/tiny-edge, sliver census) · feature rebuild `feature_status`
(implicit in every ops apply).

### 4.2 Seconds (per-candidate gates, CI) / minutes (sparse gates)

Seconds: `check_feature_overlap` · `check_interference` · `check_symmetry` ·
`wall_thickness` · `draft_analysis` · `compare_parts` (match_pct — ground truth for
recon & parametrization) · `solve_mates` · `ecad_run_drc/dfm` · `ecad_simulate` (op/dc) ·
`cam_simulate` (gouge/rapid/machined-fraction) · `review_drawing`.
Minutes: `run_fea_static/modal` · `run_cfd_flow` · `run_cfd_accurate` (**Linux/Mac
only — never put OpenFOAM in a cross-platform reward path**).

### 4.3 Gaps worth building (part of T-H4)

1. **GSL groundedness checker** (planned in P13 — promote to standalone verifier: parse
   any model text → slot-match numerals vs state; it is also the IP-7 output gate).
2. **Standalone `is_manifold()` / export round-trip check** (STEP re-import → compare).
3. **Unit-sanity validator** (range checks per quantity kind; the ksi/MPa 6.9× guard).
4. **Drawing dimension-coverage** (every functional feature dimensioned exactly once).
5. **Tolerance-stack check** (T-BL08 — becomes IP-17's reward when built).
6. **Requirements join checker** (`aicad.req/1` statuses vs actual verifier evidence).

### 4.4 Reward map (verifier × insertion point)

| Verifier | Serves | Role |
|---|---|---|
| rebuild feature_status + eval_check | IP-1, 13, 14, 18 | binary reward + CI gate |
| check_sketch DOF/conflicts | IP-9, 13 | dense reward (constraint count toward full) |
| measure_part / measure_between vs spec | IP-1, 12, 13, 17 | scalar error reward |
| compare_parts match_pct | IP-5, 14, 18, 21d | continuous reward (cadrille-style GRPO) |
| solve_mates + interference | IP-15 | binary + clearance scalar |
| cam_simulate gouge/fraction | IP-4 | multi-term reward |
| ERC/DRC/DFM finding counts | IP-6, 8 | negative reward per finding |
| groundedness/slot harness (T-F10) | IP-7, 11, 20, 21 | reward + inference output gate |
| fit_report + tolerance stack | IP-16, 17 | pass/fail gate |
| render pipeline ground truth | IP-21 a/b/d | free labels for VLM DPO/GRPO |
| req join checker | IP-12, 20 | CI gate |

Multi-term rewards (groundedness + coverage + format) everywhere, per the P13
anti-reward-hacking rule.

---

## 5. Pipeline integration plan (OSS-first)

**Stack:** Unsloth + TRL (SFT/DPO/GRPO) · PEFT/QLoRA · **DVC** (data+model versioning) ·
**Argilla** self-hosted (audit UI) · **MLflow** self-hosted (tracking; keeps telemetry
off third parties) · **vLLM** (server adapters) · llama.cpp/GGUF (in-app local serving).
No Prefect/Dagster initially — see orchestration.

1. **Telemetry export path:** `tools/flywheel/export.py` scans
   `<appdata>/AI-CAD/cache/runs/**` + session dirs → consent-filtered, scrubbed JSONL
   shards → DVC-tracked `training-data/` repo (S3-compatible remote; fleet spans
   states, so remote storage is the sync point). Provenance stamped per record.
2. **Data versioning:** DVC; datasets are immutable versioned snapshots
   (`corpus/ip18/v3`); training configs reference dataset hashes, giving full
   reproducibility for the human audit gates.
3. **Audit UI:** Argilla (docker, one box). Two mandatory human gates (per directive):
   **data-approval** (records flow telemetry→Argilla queue→approved dataset) and
   **model-promotion** (eval report + sample transcripts reviewed before any adapter is
   promoted past canary).
4. **Orchestration — reuse the work.py pattern, not a new scheduler.** Training jobs are
   markdown/YAML files in a git-synced `training/jobs/` queue with atomic claim
   (identical mechanics to `work/tools/work.py claim`, which the fleet already runs
   across states). A runner (`tools/flywheel/runner.py`) claims a job, `dvc pull`s the
   dataset, runs the whole job locally (SFT/GRPO/eval), `dvc push`es artifacts, logs to
   MLflow, posts a work_note. Satisfies: multi-state fleet, mixed OS, zero cross-internet
   single-run training, and the team already trusts the coordination idiom. Graduate to
   Prefect only if job graphs get deep.
5. **Windows/Linux training parity:** torch+PEFT+TRL run native on both; **Unsloth and
   vLLM are effectively Linux/WSL2** — the runner declares per-job `requires:
   [cuda>=X, os: any|linux, vram>=Y]` and Windows boxes claim only compatible jobs
   (native TRL path) or run the Unsloth path under WSL2. GGUF conversion + llama.cpp
   inference are parity-clean on both. Eval harness and dataset factory are stdlib+
   engine, parity-clean today.
6. **Eval benchmark suite (T-H8):** product-task benches derived from real usage +
   synthetic factory, one per insertion point shipping a model: elicitation
   (conversation→brief, scored by req-schema slot coverage vs human-audited gold),
   spec→sketch (solver status + dims), regen-fix (corruption corpus: mutate known-good
   docs into failing ones → % fixed), narration (T-F10 harness — shared, not
   duplicated), CAM (gouge-free + fraction), VLM render-review (claims vs known
   geometry). Each bench = frozen dataset hash + scoring script + baseline row
   (frontier model + deterministic fallback). CI hook: optional `gate.sh` stage 6 runs
   smoke-size benches; RECOGNITION_RESIDUAL go/no-go discipline extends to every bench.
7. **Serving & routing (T-H9):** server mode = one vLLM instance, per-task LoRA routing
   (`--enable-lora`, adapter per insertion point — the OpenAI-compatible endpoint slots
   into the existing provider layer + T-F9 contract as just another provider with an
   `adapter` field). Local mode = merged GGUF per task via llama.cpp/LM Studio (P13
   serving posture; VLM adds an mmproj). Task→model routing table lives in the T-F9
   registry: `{insertion_point → provider, model, adapter_version, fallback}` where
   **fallback is always the deterministic path or frontier**, honoring the kill-switch
   rule.
8. **Canary + rollback:** stage 1 **shadow mode** — custom model runs alongside the
   incumbent on real (consented) traffic, outputs scored by verifiers, never shown;
   stage 2 canary — routing table sends N% of that insertion point's calls (per-user
   opt-in "early adopter" toggle), verifier scores + accept-rates compared; stage 3
   promote (human gate). Rollback = routing-table flip to previous adapter version
   (adapters are DVC-versioned artifacts; nothing is overwritten). All three states
   visible in Settings — the user always knows which brain is answering.

---

## 6. Prioritized roadmap

Scoring value × feasibility × training-data availability (H/M/L compressed):

| Rank | Insertion point | Why |
|---|---|---|
| 1 | IP-7 translator (committed: T-F10/11) | unlimited synthetic data, exact rewards, already planned — the pathfinder for the whole pipeline |
| 2 | IP-18 regen-error diagnosis | corruption factory = unlimited synthetic data; binary verifier reward; every real failure is telemetry gold; high user pain |
| 3 | IP-12 requirements elicitation | unlocks verify-against-brief for every other agent task; every copilot session generates data; frontier-strength task today |
| 4 | IP-13 spec→sketch | dense solver reward; mask-one-out + synthetic; needs IP-12's format first |
| 5 | IP-9 constraint ranker (micro) | trains in hours on CPU-adjacent budget; instant UX win |
| 6 | IP-21 VLM offline eyes | closes the biggest offline-mode gap; synthetic renders free; needs base-model pick (appendix A) |
| 7 | IP-4 CAM assist | good rewards, weaker data volume until CAM usage grows |
| 8 | IP-17 dim/tol suggestion | blocked on tolerance-stack verifier (T-BL08) |
| 9 | IP-15 mates, IP-16 standards, IP-20 drawings, IP-14 parametrization | solid but later; IP-19 naming rides along as preference data |

**First three to ship frontier-backed (logging everything, T-H7 middleware):**

1. **IP-12 elicitation** — bench: brief slot-coverage/precision vs human-audited gold on
   ≥50 real team sessions; success = ≥90% coverage, 100% schema-valid, user sign-off
   edit distance trending down.
2. **IP-18 regen-error diagnosis** — bench: corruption corpus (≥500 mutated docs across
   feature families); success = ≥70% auto-fixed (rebuild passes, unaffected bodies
   compare ≈100%), zero fixes that silently change intent dims.
3. **IP-20 drawing/doc generation** (with IP-11 deliverables) — bench: dimension-coverage
   + review_drawing render audit + req-evidence join; success = ≥95% coverage, 0
   ungrounded numerals (groundedness checker).

**First custom-model migration:** IP-7 translator stays the committed first (T-F11 gates
unchanged). **First *new* migration: IP-18** — after one quarter of frontier logging:
SFT on synthetic corruption corpus + harvested (fail→fix) traces, GRPO with
rebuild-passes reward, Qwen3-4B base per P13, served per §5.7. Success = matches
frontier fix-rate on the frozen bench within 5 points at <1/50th cost, never below the
deterministic-suggestion baseline (kill-switch rule).

---

## 7. Master DAG update & tooling plan (executed)

**Workstream H** cut into `work/tasks/` (this commit): T-H1 telemetry contract &
capture · T-H2 canonical serialization + semantic diff · T-H3 `aicad.req/1`
requirements schema · T-H4 verifier battery + gap verifiers · T-H5 training data store
+ Argilla audit · T-H6 fleet training runner · T-H7 frontier logging middleware +
shadow mode · T-H8 product eval benches · T-H9 vLLM/LoRA serving + canary/rollback ·
T-H10 offline VLM track · T-H11 regen-error diagnosis (frontier ship → first new
migration). Dependencies wired into T-F9/T-F10/T-F5/T-F17; DAG regenerated in
`work/DAG.md`. Master plan gains Pillar 16 §§ pointing here.

New tooling directories this creates over time: `tools/flywheel/` (export, runner,
scrub), `training/` (jobs queue, configs; DVC-tracked data lives in its own repo),
`contracts/telemetry.md`, `contracts/requirements.md`.

---

## Appendix A — Small VLM candidates for the offline vision track (IP-21)

Full verified survey (licenses, llama.cpp vision support, GGUF sizes, RLHF recipe
maturity, task-fit benchmarks, rule-outs, all with primary-source URLs):
**[VLM_OFFLINE_TRACK.md](VLM_OFFLINE_TRACK.md)**. Operative conclusions:

- **Primary: Qwen3-VL-4B-Instruct** (upgrade path 8B) — Apache-2.0, llama.cpp vision
  merged (mmproj), DocVQA 95.3 / OCRBench 881 at 4B, native multi-image, Unsloth GRPO
  proven at 4-bit on 16 GB; keeps the ML stack on one tokenizer family with the planned
  Qwen3-4B text translator.
- **Drawing specialist: granite-vision-4.1-4b** (Apache-2.0, official GGUF+mmproj,
  table/callout extraction). **Hedge: Gemma 4 E4B-it** (Apache-2.0, day-0 llama.cpp).
- **Fine-tuning is mandatory for drawings:** zero-shot mechanical-drawing accuracy is
  ~60% (MechVQA), but the benchmark's own fine-tuned 4B beats Gemini-3-Pro — a
  domain-tuned small VLM demonstrably wins this task class.
- **Recipe:** QLoRA SFT (vision+LLM layers) → TRL DPO → Unsloth GRPO with frozen vision
  tower, deterministic render-grounded rewards (cadrille precedent: online GRPO > DPO
  when the reward is programmatic — ours is our own kernel).
- **Division of labor confirmed by the difference-detection literature** (frontier VLMs
  <41% recall on fine diffs): VLM proposes coarse claims through the intake confidence
  protocol; deterministic code verifies and measures. Never invert this.
- **Ruled out:** Qwen2.5-VL-3B (non-commercial), Moondream 3 (BUSL + cloud-only FT +
  single-image), Phi-4-multimodal & PaliGemma 2 (no llama.cpp vision), FastVLM
  (research license), EasyR1 as framework (full-FT only).

Execution and the go/no-go residual measurement live in task **T-H10**.
