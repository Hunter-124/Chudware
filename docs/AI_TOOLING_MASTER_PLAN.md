# AI Tooling & Infrastructure Master Plan

**Status:** Complete draft for ratification — all research passes integrated
(engineering data sources, provider/session auth, vision/metrology, translator
architecture+training, distillation theory, eval metrics; ToS/license claims verified
against primary sources 2026-07-02)
**Date:** 2026-07-02
**Owner:** Hunter + AI drivers
**Companions:** [PLATFORM_ADR.md](PLATFORM_ADR.md) (pillars 1–6),
[SPRINT_5DAY_PLAN.md](SPRINT_5DAY_PLAN.md), [GEOMETRY_LLM_REPRESENTATION_REVIEW.md](GEOMETRY_LLM_REPRESENTATION_REVIEW.md),
[HANDOFF_AI_EXPERIENCE.md](HANDOFF_AI_EXPERIENCE.md)

## 0. Why this plan exists

The platform ADR gives us a DAG orchestrator, sourcing, firmware, structural, and piping
pillars. This plan covers the layer underneath all of them: **the AI experience itself** —
what the models can see, ask, look up, and learn — so that any model from a 4B local
translator to a Mythos-class frontier model can drive modeling, simulation, programming
(CAM/firmware), and workflow planning **accurately**.

Design principle (inherited from the GSL review and binding here): **deterministic first,
learned second, and the model never guesses what a tool can tell it.** Every capability
below is a tool the model is *told how to use*, with structured input/output a small model
can handle.

## 1. Current-state inventory (verified 2026-07-02)

What already exists, so we build on it instead of duplicating:

| Area | State |
|---|---|
| Geometry→text (GSL) | **Mature.** `describe_part/sketch/scene`, `query_adjacency`, `hover`, captioned renders (`services/mcp/gsl_tools.py`, `cad_tools.py`). Deterministic, sid-stable. |
| ECAD→text | **Partial.** `ecad_status` is a real situational-awareness summary (components, nets, ERC, next actions) but bespoke — not part of GSL. |
| CAM→text | **Missing.** `cam_job_tree`/`cam_setup_sheet`/`cam_extract` return raw JSON; no narrative toolpath/G-code description. |
| FEA/CFD→text | **Thin.** Named scalar keys (`max_von_mises_mpa`, …) but no narrative interpretation; `cad_guide('review')` covers renders only. |
| Assemblies→text | **Missing.** `list_world_bodies`/`solve_mates`/`check_interference` are JSON; the only narrative fit text (`_format_fit`) is buried in the build loop. |
| Web research | **Absent entirely.** No search, no fetch, no datasheet/materials lookup anywhere in agent/MCP/engine. Cheatsheets are baked-in text. |
| Q/A with user | **Fragmented ×3.** MCP intake ledger (`confirm_dimensions`, `resolve_process`), C++ `clarify`/`ask` gates, and the DAG plan's `gate` nodes — no shared contract. Python build loop has none. |
| Images in | **Text protocol only.** `request_part_photos`/`submit_part_photos` assume the *calling* model has vision; no image ingestion, no camera, no metrology code. |
| Images out | **Working.** `render_part`/`contact_sheet` return `[image, caption]`. |
| Reverse engineering | **Working.** 5-pass deterministic recognizer, `eval_compare` verification, editability proof (`recon_tools.py`). Solids/mesh input only. |
| Providers | **Fragmented ×3.** Python `services/agent/providers/` (Anthropic/OpenAI/Gemini/llama.cpp/LM Studio + offline/ITAR mode), C++ `core/agent/providers.cpp` (port of same), external MCP clients. No subscription-session auth. |
| Tool governance | **Absent.** All ~150 MCP tools register unconditionally; no allow-listing (the DAG node contract requires it). |
| Material data | **Fragmented ×3.** `engine/sim/materials.json` (FEA), CAM cutting DB, DFM process profiles — no unified registry, no live lookup. |
| ML scaffold | **Gated scaffold.** `tools/gsl_dataset_gen.py` (7 families, per-face labels) + `tools/gsl_gnn/` GraphSAGE; hard go/no-go gate on `docs/RECOGNITION_RESIDUAL.md`. |

## 2. The seven capability pillars

Numbered continuing from the platform ADR (pillars 1–6 + interop track = 7).

### Pillar 8 — Research & knowledge tools (`engine/research/` + `research_*` MCP family)

The model must be able to *look things up* instead of recalling. One tool family,
ECAD-precedent module layout, with source-specific helpers so limited models get
**pre-digested, unit-normalized, provenance-stamped answers**, never raw HTML.

**v1 helper set (researched 2026-07, ToS-verified):**

1. `web_search(query, provider?)` — provider interface: **Tavily** as recommended BYO-key
   default (recurring free tier, LLM-ready answers + markdown), Exa/Serper alternates,
   **DuckDuckGo HTML as the zero-key fallback** (low volume, backoff, labeled "limited");
   skip Brave (free tier eliminated 2026-02) and shared SearXNG (single blockable IP —
   allow only as a user-supplied custom URL).
2. `fetch_page(url)` — built-in readability→markdown; Firecrawl `/scrape` for hard pages.
3. `extract_structured(url|content, json_schema)` — **grammar-constrained decoding**
   locally (Outlines / llama.cpp GBNF — a 4B model *cannot* emit invalid JSON) or
   Firecrawl `/extract` hosted; every value carries a verbatim `quote` that must
   substring-match the source or the extraction is rejected (cheapest anti-hallucination
   gate there is).
4. `material_lookup(alloy, temper?)` → `{E, yield, uts, density, condition, source,
   source_url, quote, fetched_ts}` — **bundled first-party table (~200 common
   alloys/plastics) first**, runtime fetch with citation as fallback. **Never wired to
   MatWeb/MakeItFrom/Matmatch** — all three prohibit programmatic access (MatWeb blocks
   AI crawlers by name; a negotiated MatWeb corporate data license is the only bundling
   path if we ever want their breadth). Bundle-safe seeds: Materials Project (CC-BY,
   computed crystalline data only), USDA Wood Handbook (public domain), trade-association
   values transcribed as first-party fact tables. NIST SRD is **not** public domain
   (common false assumption — Standard Reference Data Act).
5. `thread_spec(designation)` / fit + fastener callout parsing — **computed from ISO 724 /
   ASME B1.1 formulas**, bundled, no fetch. Same posture for gears (formula generator,
   not database) and bearing boundary dims (first-party ISO-15 tables for common series).
6. `standard_section(kind, designation)` — AISC shapes (free official DB), pipe schedules
   (first-party transcription of the small B36 dataset), extrusion profiles as first-party
   parametric templates (TraceParts/80-20 data prohibits redistribution).
7. `component_search(query|mpn)` — Nexar/Digi-Key/Mouser (WS-B, already planned) + LCSC
   runtime-fetch for JLC assembly; datasheet URL priority: Octopart `best_datasheet` →
   Digi-Key `DatasheetUrl` → manufacturer site → aggregators last-resort only.
8. `datasheet_fetch(mpn|url)` + `footprint_fetch(mpn)` — PDF→markdown with table
   preservation (MIT-licensed tooling — pymupdf4llm is AGPL, avoid or isolate);
   SnapEDA/SamacSys per-user on-demand only (≤10-file redistribution limit enforced in
   tool policy). Feeds spec-sheet matching (WS-B T-B5).

Plus `unit_convert`/`eng_calc` (deterministic math the model must never do in its head).

**Hard rules from the legal pass:** the model must not convert units (schema extracts
`{raw_quote, value, unit-enum}`, code converts to SI with range checks — ksi/MPa is the
classic silent 6.9× error); never point an agent at McMaster-Carr (anti-robot ToS,
litigation history — official API is invite-only B2B, keep as link-out per WS-B);
runtime-fetch caches stay **per-user local** — syncing caches through our servers or
shipping them converts caching into redistribution.

- Caching layer: `%APPDATA%/AI-CAD/knowledge/` — raw content keyed by URL+hash, tiered
  TTLs (pricing=hours, datasheets=days, standards≈immutable), `fetched_ts` always
  surfaced; offline/ITAR mode serves cache + bundled data only.
- **Answer contract:** every helper returns `{answer, units, source_url, quote,
  fetched_ts, confidence, cached}` — a 4B model quotes it verbatim; a frontier model
  chains it.

### Pillar 9 — Interaction layer: modular Q/A (`contracts/interaction.md`)

One frozen contract for "agent asks the human", replacing three ad-hoc mechanisms:

- `ask_user(questions[], kind, blocking?)` — typed questions (choice, dimension+unit,
  free text, file/image request, approval) with suggested defaults, batched (≤4, the
  proven `clarify` cap), and a machine-readable answer schema.
- Transports, one contract many surfaces: in-app dialog (ImGui PropertyManager-style form),
  MCP elicitation (spec supports it — external clients prompt their own user), DAG `gate`
  nodes (WS-A T-A3 `interrupt()`), CLI prompt for headless runs.
- The intake ledger (`confirm_dimensions`), C++ `clarify`, and DAG gates become *callers*
  of this contract instead of parallel implementations.
- Every answer persists to the run/doc record (like intake's ledger) so a resumed or
  handed-off agent never re-asks.
- Q/A is also *proactive*: verify-node failures can raise a question with the evidence
  attached (render + numbers) rather than silently retrying.

### Pillar 10 — Vision & capture: images and cameras in (`engine/vision/` + `vision_*` tools)

Close the loop that `request_part_photos` only gestures at: real pixels into the agent.

**Architecture law (researched 2026-07):** VLMs identify features and pick pixel
coordinates; **deterministic code does the measuring** (fiducial homography + calibrated
pixel scale). Benchmarks show frontier VLMs read physical scales at only 10–32% accuracy
and estimate dimensions with ~20–50% error zero-shot — the existing confidence-scored
claims protocol is the right shape, it just needs a deterministic verifier behind it.
MCP has **no typed image-input** mechanism (tool results carry images fine; inputs
don't) — the out-of-band upload + photo-ID reference our intake protocol already uses is
the correct 2026 pattern. Keep agent-facing images ≤1024 px / <1 MB (Claude Desktop tool-
result cap) with a `resource_link` to full-res.

- v1 — **image ingestion + fiducial metrology** (weeks, no GPU, all Apache/BSD deps):
  accept images by path/drag-drop; extend `submit_part_photos` to register real files and
  return photo IDs; downscaled images flow to any multimodal provider (the built-in agent
  providers gain an image slot). Ship a printable ChArUco/ArUco sheet (ArUco lives in
  core OpenCV `objdetect`, Apache-2.0); `measure_from_photo(photo_id, px_a, px_b)` does
  deterministic in-plane measurement, marker pixel-area drives the confidence score.
  UI honesty: "±2–5% for in-plane dimensions with the marker flat on the measurement
  plane and large in frame; out-of-plane dims flagged low-confidence."
- v2 — **guided multi-photo capture + photogrammetry** (quarter-scale; NVIDIA GPU for
  dense): QR → local HTTP upload page (the qrcp/LocalSend pattern — the phone is the
  camera users actually have; industry precedent: Polycam/Matterport never capture on
  desktop) with a guided shot list; **Meshroom/AliceVision (MPL-2.0) headless subprocess**
  as primary reconstruction (COLMAP BSD as SfM alternate — audit binary deps; OpenMVS is
  AGPL, don't bundle); fiducials give the scale photogrammetry lacks; then
  **Open3D (MIT) cleanup → PCL (BSD) plane/cylinder RANSAC → existing deterministic
  recognizer**. Avoid PyMeshLab (GPL in-process) and CGAL shape detection (GPL tier).
  Expectation: ~0.5–2% on textured parts; shiny machined surfaces need matte spray.
- v3 — **live camera/depth/LiDAR** (later): OpenCV `VideoCapture` for turntable/fixture
  rigs only; import PLY/OBJ/USDZ/E57 from phone scan apps (prefer raw photos+poses re-run
  through the v2 pipeline — phone LiDAR is cm-class, not for small parts); Orbbec
  Femto / RealSense (both alive, Apache-2.0 SDKs) for continuous capture. Watch, don't
  build on: Gaussian-splat meshing (not CAD-grade edges yet) and ML point-cloud→CAD
  (Point2CAD/CAD-Recode are non-commercial licenses — benchmarks only).
- All extracted dimensions flow through the existing intake confidence tiers — vision
  never silently writes geometry.

### Pillar 11 — Total state→text: GSL parity everywhere (`contracts/narration.md`)

Finish what GSL started: **every domain state the app holds must have a deterministic
`describe_*`** so any model, vision or not, large or small, can read the design.

- `describe_toolpaths(job)` — per-op narrative: strategy, tool, stepdown/over, engagement
  stats, cycle time, remaining stock, collisions/gouges; G-code digest (bounds, feeds
  actually emitted, tool changes).
- `describe_results(study)` — FEA/CFD narrative: where the max stress is (named faces via
  sid join), margin vs material yield, mode shapes in words, convergence caveats.
- `describe_board(pcb)` / promotion of `ecad_status` into the GSL layer — placement
  narrative, routing completion, clearance hot spots, stackup.
- `describe_assembly(world)` — mate graph in words, DOF per component, fit/clearance
  narration (lift `_format_fit` out of the build loop), interference summary.
- `describe_project(run)` — the DAG run itself in words: nodes done/blocked, budgets,
  open questions (feeds WS-A `dag_status`).
- Shared narration style guide + `sid`-stable naming, same as `describe_part`; every
  number traceable to a query the model could run itself.
- **Unified material registry** (`engine/materials/`) merging sim/CAM/DFM tables with
  provenance + research-tool write-back; single source for all narrations.

### Pillar 12 — Provider & session layer (`contracts/providers.md`)

One provider stack, subscription-friendly, governing every agent surface:

- **MCP-first distribution:** users on Claude/Codex/Gemini/Cursor subscriptions drive
  AI-CAD through the MCP server they already have — this is already true and stays the
  flagship path. The user's agent client *is* the copilot brain riding their own
  subscription, 100% ToS-clean. Invest in a first-run "Connect to Claude Code / Cursor /
  Codex / Gemini CLI" wizard that writes the client config (extend
  `installer/setup_mcp_clients.py`), and adopt **MCP elicitation** (stable in the spec,
  shipped in Claude Code) as a Pillar-9 transport. Do **not** build on MCP *sampling* —
  unimplemented by every major client and deprecated in the MCP draft spec (2026-07-28).
- **Session auth — only where blessed (researched 2026-07, decision-grade):**
  - **PROHIBITED, never ship:** reusing Claude Pro/Max or Gemini consumer OAuth tokens in
    a third-party app. Anthropic blocks it server-side, states it in the Agent SDK terms,
    and sent OpenCode a legal takedown; Google mass-banned even paying subscribers
    (enforced since 2026-03-25). A commercial closed-source product is a bigger target
    than any of those tools were.
  - **Blessed, ship:** **OpenRouter PKCE** ("Sign in with OpenRouter" — officially
    documented for third-party apps; one login, hundreds of models, user-balance
    billing). Evaluate the **GitHub Copilot SDK** (public preview 2026-04, MIT; runs on
    the user's Copilot subscription via GitHub OAuth — verify gh.io/terms before
    shipping). Pursue an **OpenAI Partner Network** conversation for sanctioned
    ChatGPT-subscription login (OpenAI has blessed exactly this for opencode). Anthropic's
    metered **Agent SDK credit** is the one sanctioned subscription-linked Claude route.
  - **BYO API key** stays for pros/enterprise; local models (LM Studio/llama.cpp/Ollama
    endpoint) stay first-class; offline/ITAR mode preserved.
- **AI-CAD Cloud (company-hosted relay):** for users who won't attach an account or key,
  we host our own model inference gateway — an OpenAI-compatible relay endpoint tied to
  the planned account system (limited trial quota → paid metering). It is just another
  provider adapter under `contracts/providers.md` (the contract makes it a config entry,
  not a code path), so the client work is nearly free; the server side (gateway, quota,
  billing hooks, which upstream models we resell vs self-host) is a separate later
  project. **Gate: configuration and rollout decisions go through the human driver
  before any implementation** — this plan only reserves the seam.
- **Converge the 3 provider layers:** freeze `contracts/providers.md` (message schema,
  tool-call schema, image slots, streaming, auth kinds); Python + C++ implement the same
  contract; adding a provider becomes one contract-conformant adapter.
- **Tool governance:** per-context tool allow-listing (the DAG node contract already
  requires `tools:` allow-lists) — registry tags every MCP tool with family + risk class
  (read/write/spend/external), enforced at dispatch, surfaced in the autonomy dial.

> RESEARCH SLOT R2: per-provider subscription-auth reality + ToS verdicts, what
> opencode-style tools do, MCP sampling/elicitation client support — being filled.

### Pillar 13 — Learned translator track (`tools/translator/`)

The user-directed research track: a tiny trainable model (≤4B, runs in a couple GB VRAM)
that *augments* the deterministic narration layer — plus NNUE-style micro-evaluators
where cheap learned scoring beats hand tuning. Hard inherited gates: deterministic first;
learned output feeds templated/verified text only; train only where a measured residual
exists (`docs/RECOGNITION_RESIDUAL.md` pattern extends to each new domain).

**Architecture principle (the boundary that decides everything):** deterministic engines
compute the facts; the learned layer only narrates, semantically labels ("this is an
adaptive-clearing roughing op", "synchronous buck stage"), and summarizes
task-conditioned ("describe only what changed"). The translator never reads raw
B-rep/G-code/netlist bytes and never invents a numeral — numbers are copied from the
input serialization and verified post-hoc; on verification failure the system degrades
to the deterministic GSL template, never to fiction.

- **Stage 0 — serialization freeze (no training, biggest lever):** one canonical compact
  GSL text serialization per domain (`gsl-ser/v1`, versioned, tag in every prompt) —
  stable entity IDs, degree-ordered adjacency, minified numbers with units. Controlled
  studies show serialization choice swings small-model accuracy 5–60% — more than any
  other knob we control.
- **Stage 1 — SFT translator:** base = **Qwen3-4B-Instruct-2507** (Apache 2.0, 262K
  context, native tool calling, LM Studio day-0) with a parallel **Gemma 3 4B** run
  (best IFEval in class, official QAT int4, matches our existing local default) — eval
  gate picks the winner. Ruled out: Gemma 3n E4B (its "4B effective" footprint exists
  only on Google's edge stack — llama.cpp quantizes all 8B raw params, text-only);
  Qwen2.5-3B (non-commercial research license); **DiffusionGemma** (shipped 2026-06,
  Apache 2.0, but 26B-MoE/16.8 GB quantized, no server mode — watch, don't build on).
  Multimodality not needed — input is serialized text. QLoRA r=16–32 needs only
  ~3–6 GB VRAM; **150–300k pairs ≈ 10–21 h/epoch on one 4090** (a weekend), 1M pairs
  wants the second GPU. Iterate data mix weekly rather than scaling volume.
- **Dataset factory:** extend `tools/gsl_dataset_gen.py` beyond 7 families; the
  deterministic describers are the free label source; mix across domains (B-rep ~40%,
  sketch/ECAD/CAM ~15% each, FEA 5%, contrastive "what changed" + correction pairs 10%);
  template captions paraphrase-augmented offline by a large LLM, then **re-verified by
  the groundedness checker before entering the training set** (never trust the
  paraphraser); round-trip verification (text → re-extract slots → diff state) filters
  hallucinated pairs; oversample rare feature combinations (distillation loses rare
  branches first).
- **Stage 2 — GRPO groundedness polish:** TRL `GRPOTrainer` with a *deterministic*
  reward function (parse claimed numbers → compare against kernel-measured values →
  penalize unsupported numerals) — the verifiable-reward regime; no critic, no learned
  reward model (exact, free, unhackable). 4B LoRA GRPO ≈ 18–20 GB — **fits one 24 GB
  card**; two-GPU setup puts vLLM rollouts on card 2. Multi-term reward (groundedness +
  slot coverage + format) to block reward-hacking-by-omission.
- **Stage 3 — NNUE-style micro-evaluators** (parallel track, <1M-param MLPs over
  features the engine already computes, microseconds on CPU, minutes-to-hours to train),
  in order of free-signal availability: (1) **feature-recognition hypothesis ranker**
  (synthetic factory knows true face ownership — same labels as the GraphSAGE gate);
  (2) **DFM severity ranker** (labels from our own `dfm_check` + CAM sim outcomes —
  rank findings, never replace the exact rule engine); (3) **sketch-constraint
  suggestion ranker** (mask-one-out self-supervision on finished sketches);
  (4) **agent next-action value head** (geometric distance-to-goal via `eval_compare`
  as free reward) — only once multi-step search exists. Same contract as the GNN gate:
  outputs rank deterministically-generated candidates, never emit geometry or numbers.
- **What NOT to train (deterministic wins, per evidence):** raw G-code/toolpath
  understanding (LLMs demonstrably fail at toolpath state-tracking; a modal interpreter +
  stock simulation recovers everything — train only narration of the simulator's
  output); netlist enumeration/connectivity (exact graph traversal; train only
  open-ended topology-intent labels); any numeric computation; feature recognition where
  the deterministic recognizer succeeds; learned reward models.
- **Serving:** merged LoRA → GGUF Q4_K_M (~2.5 GB) via LM Studio/llama.cpp (drop-in for
  the existing local-model default); GBNF/`json_schema` grammar for structured caption
  fields; the GRPO groundedness checker re-runs at inference as the output gate.
- **Eval harness first:** numeric-fidelity + hallucination-rate benchmarks exist *before*
  any training run; the GNN go/no-go discipline applies to every model in this track.
  Design (researched 2026-07): numbers are the dominant error class in data-to-text
  (44% of all errors in the one gold-standard study) and generic metrics (BLEU, NLI,
  entity-overlap) either mishandle or explicitly exclude numerals — so the primary
  metric is **SER-style slot accuracy**: enumerate every dimension/tolerance/material/
  count in the source state, score added/missing/wrong-value per slot with
  unit-normalized numeric comparison (we hold the ground-truth state, so this is exact,
  reference-free, and free to compute). Secondary: extraction-precision over free text
  (with the caveat that extraction-based checks missed ~2/3 of number errors when
  audited — a small human-audited gold set validates the automatic metric itself).
  Report intrinsic (wrong value for a real feature) vs extrinsic (invented feature)
  hallucination separately; stratify numeric accuracy by magnitude/precision/unit
  (FERMAT-style); score example-level ROC-AUC, not corpus averages. Verify the base
  model's tokenizer digit handling before committing (subword tokenization measurably
  degrades numeracy).

**Literature survey (verified 2026-07, primary sources):**

- **The proven recipe at our scale:** CAD-Recode (ICCV 2025) maps geometry → executable
  CadQuery Python with a **Qwen2-1.5B + one linear projection layer** — SOTA, weights and
  its 1M-program synthetic dataset public, full-parameter trainable on one 24GB card.
  cadrille (ICLR 2026 oral, Apache-2.0) extends it multimodal (points/images/text,
  Qwen2-VL-2B) and adds **GRPO RL with programmatic geometric feedback** — exactly the
  groundedness-reward loop we want, and our kernel can *be* the reward function
  (`eval_compare` match_pct as reward). BRepFormer (ICMR 2025) verifiably trains its
  B-rep feature-recognition transformer on a **single RTX 4090**.
- **The field converged on our representation:** 2025–26 work (CAD-Recode, cadrille,
  CAD-Coder, Text-to-CadQuery) abandoned bespoke command-token vocabularies for
  **CadQuery/Python as the CAD↔text interface** because off-the-shelf LLMs already read
  it. Our engine is build123d — the same ecosystem. Our translator trains on the
  representation the product already executes.
- **Grounding "face #37":** Pointer-CAD (CVPR 2026) emits pointers into the current
  B-rep's entity set instead of coordinates — published validation of GSL's sid-handle
  approach; B-repLer (SIGGRAPH 2026) edits history-free B-reps via a latent geometric
  encoder. Both closed/unreleased so far — concepts to mirror, not code to use.
- **Our size class is demonstrated repeatedly:** Text-to-CadQuery full-SFTs **Gemma3-1B
  and Qwen2.5-3B** end-to-end (the closest published template for exactly the model the
  user described); OpenECAD ships working 0.55B–3.1B CAD VLMs; FlexCAD does
  hierarchy-text CAD editing with a LoRA of only ~3.4M trainable params; CADFusion adds
  DPO on *rendered outputs* (visual-feedback preference tuning — pairs naturally with
  our render pipeline).
- **Reusable, license-clean anchors:** cadrille (Apache-2.0, SFT code + weights),
  FlexCAD + CADFusion + CAD-Editor (all Microsoft, MIT — controllable generation,
  visual-feedback DPO, locate-then-infill editing + automated training-pair synthesis),
  CAD-Coder VLM (Apache-2.0). **License cautions:** Text2CAD code+dataset are
  CC BY-NC-SA (non-commercial — read the recipe, regenerate the data ourselves);
  Point2CAD/CAD-Recode code CC BY-NC; BrepGen GPL-3.
- **What doesn't fit a 24GB budget:** DTGBrepGen (4×A800), CAD-MLLM (16×H800),
  full-parameter 7B GRPO. The envelope: full-parameter SFT ≤3B, QLoRA ≤8B, GRPO ≤2B.
- **The gap is real:** state→text (our direction) is the *least covered* in the
  literature — nearly everything is text→CAD. Only STEP-LLM's serialization scheme,
  B-repLer's direct-B-rep editing, and CAD-Recode's "code as LLM-readable state" touch
  it. A translator distilled from our deterministic GSL over our own doc schema is
  genuinely novel ground where synthetic supervision is free.
- **"Distill the deterministic teacher" is a proven pattern** (data programming /
  Snorkel line, NeurIPS 2016 → VLDB 2018 → Google DryBell SIGMOD 2019): a neural student
  trained on rule-system outputs *retains the rules' precision while extending coverage*
  (DryBell: +58% coverage at +4.5% quality), with supervised-learning-rate error decay
  as unlabeled volume grows; template-teacher → small-LM naturalizer is published
  practice (Kale & Rastogi, EMNLP 2020). A deterministic teacher's low-entropy targets
  are exactly the regime where small students track teachers best (Zhou ICLR 2020).
  **Hazards to engineer around:** rare rule branches degrade first under distillation —
  oversample unusual feature/state combinations; teacher noise is *amplified*, not just
  inherited — the GSL must be correct before any training run; prefer soft/structured
  supervision over naive hard-label imitation (reduces faithfulness hallucination);
  expect interpolation within the generator grammar's pattern space, **not**
  extrapolation to structurally novel states (Lake & Baroni) — so the eval set must
  include held-out *structures*, not just held-out samples, and the deterministic path
  stays the fallback for anything out-of-distribution.

**Ship gates (each stage passes before the next, and before any deployment):**
numeric fidelity ≥99.5% (every numeral matches a value in the input serialization,
slot-keyed, unit-normalized); unsupported-claim rate <0.1% on a held-out set that
includes out-of-family parts (held-out *structures*, not just samples); round-trip
re-extraction diff; slot coverage ≥98%; and the kill-switch gate — **the translator must
beat the raw GSL templates on downstream agent task success while never losing to them
on fidelity; if it doesn't beat templates, don't ship it — templates are free.**

### Pillar 14 — Agentic workflow completion (binds it all together)

What the DAG orchestrator (WS-A) needs from this plan to make agents genuinely able to
model, simulate, program, and re-plan:

- **Node context packs** pull from Pillar 11 describers (never raw JSON dumps).
- **Verify nodes** gain research-backed checks (material data provenance, datasheet
  cross-check) and vision checks (render-vs-photo comparison for reverse engineering).
- **Plan mutation:** `dag_replan` — an agent (or the user) can splice nodes into a
  checkpointed run (add a "source cheaper material" branch after a failed cost gate);
  end conditions + retry budgets are mandatory on spliced nodes, so loops still terminate.
- **Workflow templates:** reusable graph fragments (design→simulate→iterate;
  ecad→firmware→emulate; model→CAM→post→cost) shipped as JSON, remixable by the planner.
- **Self-check battery as one tool:** `design_review()` runs interference/overlap/DFM/
  DRC/D-C/budget and returns a narrated verdict — the standard verify node.
- **Escalation:** every node can raise `ask_user` (Pillar 9) with evidence instead of
  failing silently; autonomy dial decides whether it blocks.
- **Mission-control UX (transparency + steering, user-mandated 2026-07-02):** the agent
  is a glass box, not a spinner. The app shows, live and streaming: the current plan
  (DAG run view fed by `describe_project`), the active node, each thought/action/tool
  call as it happens (the host `activity_panel`/agent-step stream is the seed), and
  every artifact produced (Pillar 15 cards). The user can steer AT ANY TIME: pause
  (T-A3 `interrupt()`), answer/override open questions (Pillar 9), inject an
  instruction, splice the plan (`dag_replan`), or turn the autonomy dial per-run and
  per-node mid-flight. Autonomy balance is explicit: at `auto` the agent decides and
  keeps a visible audit trail; at `approve` it proposes design criteria and decisions
  as diffs the user accepts; at `manual` it drafts, the user drives. Intent
  interpretation is a first-class step: vague prompts become an editable **design
  brief** (requirements doc per T-BL09 schema) the user signs off before the run —
  verify nodes then check against it, and "stay on track" suggestions are deviations
  from that brief, with evidence.

### Pillar 15 — Deliverables & document tooling (`services/deliverables/` + `deliverable_*` tools)

Added 2026-07-02 (user-mandated): everything a professional engineer ships besides the
CAD file itself — presentations, reports, calc packages, spreadsheets, manuals — must be
producible by the built-in agents inside the one application, and previewable in-app.

**Open-source-first (binding):** never build bespoke parsers/writers for common formats.
Engines (license-guardrail compliant per CONTRIBUTING.md):

| Capability | Engine | License | Mode |
|---|---|---|---|
| .pptx write | python-pptx | MIT | in-process |
| .docx write | python-docx | MIT | in-process |
| .xlsx read/write (real formulas) | openpyxl | MIT | in-process |
| Charts/plots | matplotlib | PSF | in-process |
| HTML/MD → PDF | WeasyPrint / reportlab | BSD | in-process |
| Format conversion (md↔docx↔odt…) | pandoc | GPL | **subprocess only** |
| Office → PDF (preview/export) | LibreOffice headless | MPL-2.0 | **subprocess** |
| PDF → raster preview | pdfium (or poppler) | BSD (GPL) | pdfium in-process; poppler subprocess |

- **Tool family:** `make_presentation(spec)`, `make_report(spec)`, `make_workbook(spec)`,
  `make_manual(doc)`, `render_document(path) → preview PNGs`, `list_artifacts(run)`.
  Spec-driven (JSON outline → template), so a 4B model can call them (rule §3.2).
- **Provenance rule applies (§3.3):** template slots pull numbers from Pillar-11
  describers, FEA/CFD results, BOM/cost rollups (WS-B), CAM setup sheets, and drawings —
  the model composes and narrates, it never types a load-bearing numeral by hand.
- **Standard engineering templates v1:** design-review deck (rendered views/contact
  sheets, mass props, FEA margins, open risks); calc package (docx/pdf, equations +
  assumptions + cited material data from Pillar 8); BOM/cost workbook (xlsx with live
  formulas); test/verification report (requirements → evidence, T-BL09 join); assembly
  instructions + user manual (absorbs backlog T-BL11's manual half).
- **Universal preview:** one service-side `render_document` pipeline (LibreOffice→PDF→
  raster for office formats; native for pdf/svg/png/csv/md) feeding an in-app preview
  pane + Copilot **artifact cards** — every agent-produced file shows as a card with
  thumbnail, open-externally, and regenerate actions. External MCP clients get the same
  previews as image results.
- Artifacts persist under the project (PDM-lite T-BL10 revisions them later) and are
  listed by `describe_project` so a resumed agent knows what it already shipped.

### Pillar 16 — Data flywheel & training pipeline (`tools/flywheel/` + Workstream H)

Added 2026-07-02 (custom-model integration survey — full text in
[CUSTOM_MODEL_INTEGRATION_SURVEY.md](CUSTOM_MODEL_INTEGRATION_SURVEY.md); VLM research
in [VLM_OFFLINE_TRACK.md](VLM_OFFLINE_TRACK.md)). Expands Pillar 13 from one translator
into a product-wide program: 21 model insertion points, telemetry capture
(`ai_artifact` envelope + accept/edit/reject actions extending `contracts/events.md`),
consent tiers (off|meta|full; external default OFF; offline/ITAR forces off),
`aicad.req/1` requirements layer, verifier battery as RL-reward vector, DVC + Argilla
(two human audit gates: data approval, model promotion), claim-based fleet training
runner (own machines, mixed OS, multi-state — job-level distribution only, never
cross-internet single-run training), vLLM per-task LoRA routing with
shadow→canary→promote rollout and routing-table rollback, and the offline VLM track
(Qwen3-VL-4B primary). Scope decisions ratified 2026-07-02: capture ships now,
training on telemetry stays gated at T-F11 (decision #4 stands); team dogfood is the
initial corpus. New rule **R1 (editability parity):** every model output lands as an
artifact of an existing human-editable document format — generation converges on
doc-ops JSON, never opaque artifacts.

## 3. Cross-cutting rules

1. **Contract-first:** each pillar freezes its `contracts/*.md` before code (C++ cutover
   absorbs it later, same as ECAD precedent).
2. **Small-model floor:** every tool must be usable by a 4B local model — structured
   in/out, explicit units, no judgment calls hidden in prose. `cad_guide` grows a topic
   per new family.
3. **Provenance everywhere:** any number the model quotes traces to a tool result with a
   source + timestamp, or to the B-rep itself.
4. **Offline/ITAR mode** stays coherent: research tools degrade to cache+bundled data;
   translator models run local by construction.
5. **Licensing/monetization:** account-based authorization planned separately (trial/
   personal approval + commercial billing) — not designed here, but nothing in this plan
   may assume a specific license regime.
6. **Open-source-first for commodity tooling:** file-format read/write, parsing,
   previews, converters, and computer-interaction plumbing come from established OSS
   libraries (MIT/BSD/Apache in-process; GPL/MPL subprocess-only; AGPL never — the
   CONTRIBUTING.md guardrails). We build only the engineering-specific layer on top
   (templates, provenance joins, GSL narration). Rebuilding a .docx writer is a bug.
7. **Transparency by default:** every agent surface streams its plan, current action,
   and tool calls to the UI as it runs, and every run is steerable (pause / answer /
   inject / replan / autonomy dial) without killing it. No fire-and-forget agents.

## 4. Execution map (draft — placement decided in closing Q/A)

Proposed as **Workstream F** task files (claimable by autopilot agents) once approved:

| Task | Pillar | Content | Depends on |
|---|---|---|---|
| T-F1 | 8 | `contracts/research_tools.md` + `engine/research/` scaffold + `web_search`/`fetch_page` with provider interface + cache | — |
| T-F2 | 8 | Source helpers v1 (material_lookup, datasheet_fetch, standards_ref, unit_convert) + unified material registry seed | T-F1 |
| T-F3 | 9 | `contracts/interaction.md` + `ask_user` core + in-app dialog + MCP elicitation transport | — |
| T-F4 | 9 | Migrate intake/clarify/DAG-gate callers onto interaction contract | T-F3, T-A3 |
| T-F5 | 10 | Image ingestion v1: submit_image, provider image slots, fiducial scale-check | — |
| T-F6 | 10 | Guided capture + phone handoff + photogrammetry subprocess → reverse_engineer | T-F5 |
| T-F7 | 11 | `contracts/narration.md` + describe_toolpaths + describe_results | — |
| T-F8 | 11 | describe_assembly + describe_board (GSL promotion) + describe_project | T-F7 |
| T-F9 | 12 | `contracts/providers.md` + tool registry/allow-listing + session-auth spike + AI-CAD Cloud relay seam (config-only, human-gated) | — |
| T-F10 | 13 | `gsl-ser/v1` serialization freeze + eval harness (slot-based numeric fidelity) + dataset factory extension (no training yet) | T-F7 |
| T-F11 | 13 | Translator SFT v0 (Qwen3-4B + Gemma 3 4B runs) + GRPO polish + LM Studio serving (gated on T-F10 eval) | T-F10 |
| T-F13 | 13 | Micro-evaluator #1: feature-recognition hypothesis ranker (gated on RECOGNITION_RESIDUAL) | — |
| T-F12 | 14 | dag_replan + workflow templates + design_review battery | T-A4, T-F7 |
| T-F14 | 15 | `contracts/deliverables.md` + `services/deliverables/` scaffold + OSS engine bundle + `make_*`/`render_document` tools | — |
| T-F15 | 15 | Engineering deliverable templates v1 (deck, calc package, workbook, test report, manual) with provenance joins | T-F14 |
| T-F16 | 15 | Universal in-app preview pane + Copilot artifact cards (service-side render_document pipeline) | T-F14 |
| T-F17 | 14 | Mission-control UX: live plan/thought/action stream, steer-anytime, autonomy dial UI, design-brief elicitation | T-A3, T-F3 |

Dependencies into the existing sprint: T-F4→T-A3 (gates), T-F12→T-A4 (verify nodes),
research helpers feed WS-B T-B5 (spec-sheet AI path) and WS-D T-D4 (code-compliance RAG).

**Workstream H (Pillar 16)** cut 2026-07-02: T-H1 telemetry contract · T-H2 canonical
serialization + doc_diff · T-H3 aicad.req/1 (supersedes T-BL09) · T-H4 verifier battery ·
T-H5 data store/Argilla · T-H6 fleet runner · T-H7 frontier logging + shadow ·
T-H8 eval benches · T-H9 vLLM serving/canary (dep T-F9) · T-H10 offline VLM (dep T-F5) ·
T-H11 regen-error diagnosis. Task map + rationale:
[CUSTOM_MODEL_INTEGRATION_SURVEY.md](CUSTOM_MODEL_INTEGRATION_SURVEY.md) §7.

## 5. Execution decisions (recommended defaults — confirm before cutting tasks)

Asked 2026-07-02; user AFK, so these are recorded as recommendations, not decisions:

1. **Sequencing vs the 5-day sprint:** recommended — cut all T-F tasks into
   `work/tasks/` now; F1/F3/F5/F7/F9 have zero file overlap with workstreams A–E and
   are immediately claimable by autopilot agents. **CONFIRMED by Erik 2026-07-02**
   (plan-audit session): workstream F cut into `work/tasks/` including the new
   Pillar 15 tasks T-F14–T-F17.
2. **First drivers:** recommended Pillar 8 (research tools — unblocks accurate answers
   for limited models and feeds T-B5/T-D4) and Pillar 11 (narration — biggest GSL gap
   and prerequisite for translator training data) in parallel.
   **ADOPTED 2026-07-02:** T-F1 + T-F7 are day-2 tasks (first claimed); T-F3/T-F5
   follow at day 3.
3. **Phone-handoff local listener (v2 capture):** recommended default-off; enabling
   shows port + per-session token in the QR so only QR holders can upload.
   **ADOPTED 2026-07-02** — written into T-F6's DoD.
4. **Translator training data:** synthetic-only; revisit opt-in real-design telemetry
   at the T-F11 ship gate. **ADOPTED 2026-07-02** — T-F10's dataset factory is
   synthetic by construction; telemetry decision deferred to the T-F11 gate.

---

## Pillar 17 — Unified Standard Parts Library (added 2026-07-03, Erik directive)

One of the product's most valuable features: the AI designs ANYTHING — electronics,
assemblies, piping, structures — from standard parts through ONE surface. Six catalogs
already exist (core/parts families, BOLTS-lineage fasteners/bearings, KiCad+vendor ECAD,
AISC/lumber sections, piping fittings, ISO 1219 hydraulics); Pillar 17 federates them
rather than rewriting them. Architecture (frozen at contracts/parts.md when T-P1 lands):
PartRecord envelope + CatalogProvider protocol + adapter per catalog; SQLite-FTS local
index + live vendor providers; part_search / part_get / part_place MCP tools;
R1-editable insertion in every domain; standard-before-custom bias in the agent guide.
Execution: workstream P (T-P1 contract+search core → T-P2 all-domain adapters+vendor
federation → T-P3 insertion parity+agent wiring+demo). Paid catalogs (TraceParts/Werk24,
backlog T-BL12) join as providers behind the same seam when volume justifies spend.

## Pillar 18 — Feature-UI coherence & productization (added 2026-07-03, project-lead pass)

Every Pillar 8–17 backend earns a UI through workstream U, ALL of it built on the T-U1
style-conformance kit (pattern library + panel scaffold + automated style checks) so
parallel agents produce ONE coherent liquid-glass app, enforced by the recursive T-G12
walkthrough loop. Panels: parts browser (U2), CAM workspace (U3), BOM/sourcing (U4),
requirements+verification (U5), sim studies (U6), new-doc-kind viewers (U7), model &
telemetry transparency (U8). Productization is workstream Q: installers+first-run (Q1,
consent default OFF), autosave/crash recovery + consent-tied diagnostics (Q2), sample
gallery + tours (Q3, Pillar-17 dogfood), auto-generated living manual (Q4, the reserved
make_manual template). UI edit-parity for the U7 doc kinds is deliberately deferred to
walkthrough-driven prioritization.
