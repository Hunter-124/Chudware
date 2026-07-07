# Handoff — Design-Pipeline Benchmarking & Optimization

**Written:** 2026-07-05, end of a long integrator session. **For:** a fresh session
continuing the tokenmaxx work. **Author context:** the previous session designed and merged
the entire design-pipeline pillar; this document hands off the part that remains — actually
*running* it, *auditing* the parts it produces, and *optimizing* the helper/workflow
combinations until it builds "crazy complex, fully-engineered" parts.

---

## 1. What "done" means (Hunter's actual goal — read this first)

The infrastructure is built. The goal now is **empirical**: have the local models *actually
build real complex parts/assemblies* through the full AI workflow, **benchmark which
combinations of helpers/councils/workflows produce the best parts**, and **iterate/fix until
the output is genuinely well-engineered and auditable**. Hunter wants to personally audit the
produced parts to pick the best options. This is a measure → diagnose → fix → re-measure loop,
not a feature-build. Do NOT add more features until the existing pipeline demonstrably
produces good parts end-to-end.

---

## 2. Current state on `master` (all gate-verified, pushed)

The **entire design-pipeline pillar** (workstream DP) is merged — see
`docs/AGENT_DESIGN_PIPELINE.md` (architecture of record). Cognitive engine + presentation:

- **DP1** contract (`contracts/design_pipeline.md`, aicad.designpipe/1) — six stages, ledger
  source-of-truth, gates read the ledger not prose.
- **DP2** feature_architect (functional decomposition) · **DP3** geometry_designer + the
  **op-aware helper registry** (the differentiator — right calculator auto-loaded per op,
  material/process-gated) · **DP4** linkage_planner (topology/datums/interfaces).
- **DP6** diagnostic advisors ("advise, don't guess-loop": interference → real overlap
  mm³/axis/depth + fix tool) · **DP5** overseer audit-and-reroute (errors route upstream to
  the owning stage, master-intent pinned non-truncatable).
- **DP8** council policy (per-stage, tier-scaled: local wide / frontier narrow) · **DP9**
  hierarchical delegation (overseer-discretion recursion, budget meter + kill-switch, no hard
  cap) · **DP7** sketch/GSL helpers (auto-constrain, text-status for no-vision models).
- **DP10** auto-mode router + gym tuning loop · **DP11** transparency panel · **DP12**
  model-pref + Auto/Simple/Advanced settings · **DP13** multi-file view · **DP15** workspace
  switching · **DP17** interactive DAG widget · **DP18** ETA estimator.
- Also this session: **SIM3** (sim material dialog + advisor GUI wiring), **SIM4** (solver
  validation + fixed issue #17), **X7** (4 vendor adapters).

Locked design decisions live in `docs/AGENT_DESIGN_PIPELINE.md` + memory
`design-pipeline-architecture.md` + `plm-fine-detail-decisions.md`.

---

## 3. THE KEY FINDING — the pipeline executes; T-DP19 was a red herring

The gym real-runs (W30/W32/DP10) all scored 0.000 and DP10 reported the pipeline "dies at
PLAN with No module named 'langgraph.checkpoint.sqlite'". **That was an interpreter bug, not a
code bug.** The module IS installed in the gate venv and `services/dag/executor.py` imports it
fine in-process. The gym must be run with the venv python:

```
~/.local/share/AI-CAD/mcp-venv/bin/python   # NOT system python3
```

**Verified this session:** with the correct interpreter + the design-pipeline flag ON, a real
C1 mission executes end-to-end — many successful qwen3-4b turns, past PLAN. (I also declared
`langgraph-checkpoint-sqlite` in `requirements.txt`; it was installed-but-undeclared.)

**T-DP19 is therefore mostly a "run it right + re-run for real gradients" task, not a deep
fix.** Confirm the gym CLI/runner defaults to `sys.executable` or the mcp-venv, then re-run.

---

## 4. HOW TO RUN THE BENCHMARK (the core loop)

**a. Start the local model server (CUDA, qwen3-4b):**
```
~/.local/share/AI-CAD/llamacpp/build-cuda/bin/llama-server \
  -m ~/.local/share/AI-CAD/models/qwen3-4b/Qwen3-4B-Q4_K_M.gguf \
  -ngl 99 -np 4 -c 24576 --port 1234 --host 127.0.0.1 &
# wait for curl -s http://127.0.0.1:1234/health -> {"status":"ok"}  (~30-60s, ~7 GB VRAM)
```
Models available under `~/.local/share/AI-CAD/models/`: qwen3-4b (prompted-JSON/fanout),
gemma3-4b-qat (GBNF-capable), gemma4-e4b, qwen3vl4b. Role mapping: qwen3=fanout,
gemma3-qat=GBNF roles (see `benchmarks/tokenmaxx/REPORT.md`).

Tokenmaxxing (2026-07-07): the gym now has an explicit tier per local model (start the
matching model's llama-server first, per model), so `--tiers` can target any of them, or
several in one cycle to compare:
- `local-24k` -> qwen3-4b (fastest; no GBNF grammar)
- `local-24k-gemma3-qat` -> gemma3-4b-qat (smallest footprint; full GBNF compliance)
- `local-24k-gemma4-e4b` -> gemma4-e4b (larger/slower GBNF backup; comparison-only)
No "council" (multi-model) tier yet — see `tools/flywheel/bench/gym/runner.py`'s note
above `_TIER_LOCAL_MODEL`: the T-DP8 council primitive supports diverse-model seats but
isn't wired into a seam the gym's single-provider cells can reach.

**b. Run a real mission through the FULL pipeline (design stages ON):**
```
cd /home/hunter/Repositories/CAD
AICAD_DESIGN_PIPELINE=1 \
AI_CAD_LIBAICAD=$PWD/core/build-occt-linux/bin/libaicad.so \
AI_CAD_PROVIDER=llamacpp AI_CAD_TIER=local-24k \
~/.local/share/AI-CAD/mcp-venv/bin/python -m tools.flywheel.bench.gym.cli \
  run --cycle bench-live --challenges C1 --tiers local-24k --real
```
- `AICAD_DESIGN_PIPELINE=1` turns on the new DECOMPOSE/GEOMETRY/LINKAGE/BUILDPATH stages
  (default OFF; the whole point of the benchmark is to exercise them).
- `AI_CAD_LIBAICAD=...build-occt-linux/bin/libaicad.so` is the OCCT+Netgen engine (real CAD
  ops + sim). `build-linux` lacks Netgen/sim RPCs.
- Challenges (real engineering parts): C1 flanged_bushing, C2 clamshell_enclosure, C3
  bearing_block, C4 two_part_hinge, C5 bracket_cam, C6 bushing_mod. C2/C4 are assemblies.

**c. Audit the output (what Hunter wants to see):**
- Ledger: `tools/flywheel/bench/gym/reports/runs/bench-live/<cell>/ledger.json` — the design
  story (features → geometry_spec → linkage → build_node events, with provenance).
- Report md/json in `.../reports/`. Taxonomy classifies failures
  (incoherent_geometry, execution_stall, etc.).
- To SEE the actual part: the mission writes a doc; export a STEP/render and eyeball it (drive
  the engine like `scratchpad/drive_thermal.py` did, or `export_part`). **Auditing the real
  geometry is the point — don't trust pass/fail alone.**

**d. The tuning loop (which helper combo is best — DP10):**
```
~/.local/share/AI-CAD/mcp-venv/bin/python -m tools.flywheel.bench.gym.tuning \
  run --configs eco maximum          # runs configs over the C-suite, writes tuning_results.json
```
The router (`services/agent/pipeline/workflow_router.py`) then picks the measured-best
scaffold per task-class. Once real gradients exist, this is the optimization signal.

---

## 5. What to expect / likely first blockers (diagnose in this order)

The pipeline *runs*, but it very likely does NOT yet produce good parts. Prior real runs
(W30/W32, pre-fix) showed the failure moved UP the stack as bugs were fixed:
1. **Tool kwargs** — models call real tools but guessed args (fixed by W32 schema injection —
   verify it's active in the design-pipeline path).
2. **SPECIFY/DECOMPOSE ledger-first** — models put content in the handoff instead of
   `ledger_note` calls; the gate reads the ledger. spec_writer/feature_architect cards were
   updated to enforce ledger-first — verify the models actually comply.
3. **Invented ids** — models reference run_id/doc_id/feature ids that don't exist. Mission
   template should inject the real ids (W32 open concern — check it landed for DP stages).
4. **Geometry coherence** — even when ops execute, the PART may be wrong. This is where the
   op-aware helpers (DP3) + diagnostic advisors (DP6) + overseer reroute (DP5) should kick in.
   Audit whether they actually fire and whether the councils (DP8) improve outcomes.

For each: read the ledger, find where it breaks, fix the role card / helper / wiring, re-run.
Role cards are lead-owned prompt files in `services/agent/roles/*.yaml` — the highest-leverage
tuning surface. Numbers must always cite tools; never let a model invent a dimension.

---

## 6. Remaining DP tasks (board: `python3 work/tools/work.py board | grep DP`)

- **T-DP19** — gym real-run interpreter fix + re-run for real per-class gradients (mostly:
  ensure venv python; the "missing module" is a red herring, see §3).
- **T-DP16** — full-screen AI-Agent workspace body (unblocked; DP15 shipped the framework +
  empty shells; DP17 DAG widget + DP11 transparency + DP13 multi-file are the pieces to embed).
- **T-DP14** — design-pipeline ↔ PLM wiring; **BLOCKED on R5** (PLM editor GUI — dead WIP on
  `origin/task/T-R5-plm-editor-gui`, does NOT build; needs a real finisher for the plm_panel).
- **T-X7b** — vendor wave-2 follow-ups (McMaster STEP import + bom_price seam).
- **T-BL45** — 3D-sketcher architecture: PARKED for Hunter's A/B/C decision (a frozen design
  proposal on `origin/task/T-BL45-3d-sketcher-plan`; do NOT merge without his call; it also
  re-collides task-id T-BL51 — renumber at merge).

---

## 7. Process rules that saved this session (follow them)

- **Integrator/merge protocol:** `git merge --no-ff origin/task/X` on master; NEVER
  `git pull --rebase` after local merges; reconcile remote with `git fetch && git merge
  origin/master --no-edit`. Statuses flip to `done` only after gate PASS (edit `status:` in
  the task file). Board mutations: `python3 work/tools/work.py note/claim/validate`.
- **Union-merge traps:** dict/set literals where HEAD's last entry borrows the shared closing
  brace (add the missing `},`); same-line edits (union the items). After every resolved python
  file `ast.parse` it; run `set -o pipefail` before piping pytest through `tail` (a masked exit
  code caused a bad commit once).
- **Gate:** `bash scripts/gate.sh` (~15 min; run in background — it exceeds the 10-min
  foreground limit). Pre-existing FAIL-RECORDED sets (issue #16 + `test_vlm_track` port-1234
  flake + T-V2) are non-fatal; RESULT: PASS is what matters. Do NOT poll with
  `pgrep -f "bash scripts/gate.sh"` in a wait-loop — the loop's own argv matches and deadlocks;
  wait on a completion marker/notification.
- **Subagents:** they repeatedly STALL trusting "background notification" (say "waiting on the
  gate/monitor" then stop). Tell every driver: "do NOT end your turn to wait; poll in-process
  with sleep-loops; only stop at status=review or truly blocked." Audit a stalled driver via
  `git -C <worktree> log/status` + `git ls-remote` BEFORE nudging — several had finished but
  not pushed. Spawn with explicit `model: opus|sonnet`.
- **Parallelism:** role-adding tasks collide on `services/agent/roles.py` STAGES,
  `routing.yaml` assignments, `test_roles.py` count, `phases.py` gates — run role-adders
  ONE AT A TIME or tell each to update all four itself. GUI tasks collide on
  `application.cpp`/`workspace_bar.cpp`/`scenarios.h` — batch-merge and resolve once.
- **GUI verification:** ui/imgui only (host/ is reference-only). Headless shots only
  (`xvfb-run -a env AI_CAD_NO_FILE_DIALOGS=1 AI_CAD_SHOT=<name> ./build/linux-app/bin/AI-CAD2
  --shot out.png --frames 12`); EYEBALL the PNG (Read it). `scripts/ui_style_check.py` clean.
- **Stray worktree edits leak into the main checkout** (saw it 3×: session.cpp, advisor.md).
  Check `git status` before big operations; verify+commit if good, `git checkout` if stray.
- **Honesty:** never fabricate benchmark numbers/gradients (a driver correctly reported 0.000
  rather than invent a curve). Every dimension cites a tool/table/calc. Contracts are frozen —
  bump the version header AND the changelog line together.

---

## 8. Immediate next steps for the new session

1. `git pull` master (everything above is pushed; HEAD ~`9c7adaa0`).
2. Check the in-flight run: `ls -t tools/flywheel/bench/gym/reports/runs/bench-live/*/*/ledger.json`
   — a C1 real mission was executing at handoff time; read its ledger to see how far it got and
   what it produced. Kill any leftover `llama-server` / `gym.cli` process first (`pkill -f
   llama-server; pkill -f gym.cli`) and restart clean.
3. Run §4b for C1, read the ledger (§4c), find the top blocker (§5), fix the role card / wiring,
   re-run. Then widen to C2–C6 and the tuning loop (§4d).
4. Report to Hunter with the ACTUAL PART (STEP/render) + which helper config produced it, so he
   can audit and choose. That is the deliverable.

Budget note: the prior session ended near the weekly limit mid-first-run. The local-model
compute is free (GPU); the cost is orchestration tokens — so favor letting runs complete on the
GPU and reading concise ledgers/reports over verbose transcripts.
