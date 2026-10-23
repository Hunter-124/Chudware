# artifact_validity — artifact-graded trials of the in-app agent

One task suite, one executor, one grader. A trial gives the production agent one
headless turn (`scripts/run_mission.py`, the same `agent_admin_turn` the GUI composer
sends) in a fresh workspace, then reopens the ONE file the task declares
(`deliverable.step+`) in a separate grader process and runs every mandatory check
against it. The model's prose, tool previews and the event log are never graded.

## Run

```sh
# exact executor commands, nothing executed
.venv-mcp/bin/python -m benchmarks.artifact_validity.runner run \
    --tag T --tasks F1,C1 --model openai-codex:gpt-6-sol:high --dry-run

# sequential trials: tasks x replicates x models (model order rotates per replicate)
.venv-mcp/bin/python -m benchmarks.artifact_validity.runner run \
    --tag 2026-10-baseline --tasks F1,F2,F3,F4,C1,C2,C3 \
    --model openai-codex:gpt-6-sol:high --model anthropic:claude-opus-5-5:high \
    --replicates 3 [--hostd-bin core/build-occt-linux/bin/chudware-hostd]

.venv-mcp/bin/python -m benchmarks.artifact_validity.runner report  --tag 2026-10-baseline
.venv-mcp/bin/python -m benchmarks.artifact_validity.runner rescore --tag 2026-10-baseline
.venv-mcp/bin/python -m benchmarks.artifact_validity.runner report  --tag 2026-10-baseline --rescored
```

`--model` is `provider:model[:effort]`, with provider/model ids from the runtime catalog
(Settings → AI, or Chudcode's model list) — not legacy defaults. Effort is one of
`minimal|low|medium|high|xhigh|max`; omit it to use the turn policy's effort.

Each trial is `runs/<tag>/<task>/<model>/<rep>/` (git-ignored):

| file | content |
|---|---|
| `prompt.txt` | the model-visible prompt (task notes/oracles never enter the workspace) |
| `work/` | the trial workspace as the turn left it (bundled after the turn; fixtures were copied in before it) |
| `sessions/` | the Chudcode session of this trial only |
| `events.jsonl` | the executor's worker event stream |
| `result.json` | `run_mission.py --result-json`: driver verdict + raw `agent_admin_turn` response (ok, reason, stop_reason, turn, usage, persistence) |
| `driver.log` | executor stdout/stderr |
| `grade.json` | grader output: evidence inventory + every check result |
| `manifest.json` | the trial record (below) |
| `rescores/<utc>.json` | later re-grades; the original manifest is never rewritten |

The turn itself runs in a live workspace OUTSIDE the repository,
`$CHUDWARE_AVBENCH_WORKSPACES/<tag>/<task>/<model>/<rep>` (default
`~/.cache/chudware-avbench/...`), which is moved into `work/` afterwards. This matters:
the agent's project root is the OUTERMOST ancestor carrying `.plm`/`.aicadvault`/
`.chudware` (`services/agent/project_context.resolve_project_root`), and this repository
carries them, so a workspace inside it would give the agent the whole repo as its working
directory (observed: the first smoke turn saved `deliverable.step+` into the repo root).
The runner refuses any workspace whose resolved project root is not the workspace itself.

A trial is never reused: a completed trial is skipped, a partial one (or a leftover live
workspace) aborts the run (pick a new tag). Every trial runs on a private engine instance
(`--instance-key avbench-<tag>-<task>-<model>-<rep>`, inherited `CHUDWARE_HOSTD_SOCKET`
dropped, `CHUDWARE_HOSTD_LINGER_SECS=20`), never the user's daemon. The grader runs the
in-process engine (`CHUDWARE_ENGINE_DAEMON=0`). The runner refuses a `chudware-hostd`
older than any core source unless `--allow-stale-engine` (recorded in the manifest).

## Tasks

`tasks/__init__.py` `REGISTRY` (contract: `tasks/schema.py`):

| id | domain | deliverable must contain |
|---|---|---|
| F1–F4 | facilities | hydraulic HPU, cooling-water piping network, pressure vessel, duct tree — saved studies checked against independent formulas (`oracles.py`) — see `tasks/facilities.py` |
| C1 | cad | 100×100×10 plate, 16 Ø6 through holes at {-30,-10,10,30}² (count, Ø, centres measured natively) |
| C2 | cad | edit of a supplied bushing fixture (bore 20→25) preserving the rest of the history |
| C3 | sim | 120×20×8 cantilever with a persisted static study (selectors, load, reactions, deflection, root stress, mesh evidence) |
| S1 | cad | 90×50×12 plate with one centred Ø10 through hole |
| S2 | cad | 40 mm cube with a centred Ø12×15 boss on +Z |
| S3 | cad | 50 mm box shelled to 3 mm, +Z face open (rim, floor, inner walls measured) |
| M1 | cad | revolved stepped shaft along X, Ø30×40 + Ø20×60, 1×45° chamfer on the Ø20 end (step spans, coaxiality, chamfer cone + its bounding circles, revolve in history) |
| M2 | cad | Ø100×12 flange, Ø40 bore, 6 × Ø9 on PCD 75 with the first hole on +X (count, PCD, 60° spacing, clocking) |
| M3 | cad | 3 mm L-bracket 60×40×50, bend R5 inside / R8 outside, one Ø6.5 hole per leg (thickness, concentric quarter-turn bend axes, hole spans and positions) |
| M4 | cad | 80×60×10 plate, 4 M6 counterbores at (±30, ±20): Ø6.6 through, Ø11 × 6.5 from +Z (drill/counterbore spans, flat floors, positions) |
| M5 | cad | hex standoff 10 AF × 25 along Z, flats normal to ±Y, Ø5 coaxial bore (six sides in 60° pairs, across-flats, orientation, bore) |
| H1 | cad | swept handrail tube Ø25/Ø21 on a planar XZ path, two 90° bends R50 (straights, analytic torus bends via `doc_face_points`, paired 2 mm wall, open annular ends, sweep in history) |
| H2 | cad | rectangular 100×60 → Ø80 duct adapter, 2 mm wall, 10 mm collars, open ends (collar walls, end rings, meridian/ring sections, loft in history) |
| H3 | sim | design-to-requirement 6061-T6 cantilever (σ ≤ 80 MPa, δ ≤ 0.5 mm, b ≥ 8, mass ≤ 1.25 × optimum) + Ø5.5 hole + persisted static study within 25 % of beam theory |
| H4 | cad | hinge ASSEMBLY: two knuckled leaves + Ø4 pin as part files, angle-mate revolute driven 90° by the native solver, exact pin clearance, no interference at 0° or over the fold |
| H5 | cad | redesign of a supplied M2 flange through its own history (OD 110, 8 × Ø9 on PCD 80, two 1 × 45° rim chamfers; original feature ids/params kept) |
| H6 | cad | 120×80×40 enclosure: R5 corners, uniform 2.5 mm shell open top, 4 Ø6 bosses at (±50, ±30) with Ø2.5 × 25 pilots |

Each task's `notes` field documents its oracle derivation and tolerances.

## Statuses

Per check (`CheckResult.status`) and per trial:

- **PASS** — the turn completed (`ok`, terminal, not cancelled, produced output), the
  declared artifact exists, was written during this trial (newer than the trial start and
  not byte-identical to an input fixture), and EVERY mandatory check passed.
- **FAIL** — an agent-attributable shortfall: a check failed, the artifact is missing /
  empty / unopenable / stale, the turn ran out of budget (`turn_budget`), produced no
  output, or the agent loop returned `ok=false`. A correct file left behind by a failed or
  budget-exhausted turn is still FAIL.
- **ERROR** — infrastructure, not a model verdict: provider/auth/rate-limit failures,
  executor or worker crash, missing result record, bench driver timeout, cancellation,
  grader crash or timeout, a check that raised, or a check set that does not match the
  task's declared checks.
- **UNAVAILABLE** — a check reports a capability this build lacks. Never a pass.

Precedence: ERROR > UNAVAILABLE > FAIL > PASS. The denominator is `len(task.checks)`:
a check that crashes is recorded as ERROR under its declared id (`@check("id")`), and a
dead grader process yields ERROR rows for every declared check. Nothing is dropped.

`report` prints, per task × model, `k/N` where N counts scored trials (PASS + FAIL);
ERROR and UNAVAILABLE trials are listed separately and must be re-run, never counted
as passes. A per-model macro pass rate averages the per-task rates. Every trial's checks
are printed with measured / expected / tolerance.

## What a PASS means

The production agent, given only the prompt (and any fixture), produced in one turn a
native file at the declared path whose reopened geometry, feature history and saved
studies satisfy every pre-registered numeric/topological check within the stated
tolerances, and the turn itself completed cleanly.

## Manifest

`manifest.json` records: task id/version, task content hash (prompt, fixtures, budget,
deliverable) and scorer hash (source files of the task's checks plus the shared grader
modules, and the declared check ids); replicate index; provider/model/effort; executor
command, live workspace and engine env; memory policy (per-project memory keyed by a
fresh per-trial workspace, no resume); repo HEAD,
dirty flag and dirty fingerprint (hash of `git status` + `git diff HEAD`); hostd binary
sha256/mtime and newer core sources; grader engine library sha256; timestamps and wall
time; decoded turn (status, reason, stop_reason, terminal text, persistence,
finalization errors); usage: the worker's token accumulator (`null` when it saw no
provider usage — unknown, never 0), token totals summed from the trial's Chudcode session
files (main + subagents), and `cost_usd` = Chudcode's per-message API-rate price estimate
(`null` if any message lacks a price; subscription billing differs); event digest (tool
calls, failures, runtime models); artifact sha256/size/mtime; every workspace file
(diagnostic only); every check result; verdict.

## Tests (offline, no model, no daemon)

```sh
CHUDWARE_ENGINE_DAEMON=0 .venv-mcp/bin/python -X utf8 -m pytest -q benchmarks/artifact_validity
```

Graders are tested against REAL parts/studies built by calling the tools directly
(`conftest.py`: `cad_tools`, `build_part`), with adversarial deliverables (wrong
dimension/location/face, missing study, wrong units, empty/missing/stale files, untouched
fixture). `test_runner.py` runs the whole trial pipeline with a stand-in executor that
receives the exact run_mission argument list; `test_scoring.py` decodes a recorded
current-format worker event log and terminal record.

## Limits

- Small samples: 3 replicates × 21 tasks cannot separate close models; report per-task
  counts and paired differences, not a single headline rank. No generation seed is
  applied; replicates are independent turns, identified by index.
- One turn per trial; continuation/recovery behaviour is not measured.
- Assemblies are covered by one task (H4: a three-part hinge, its revolute driven by the
  native `simulate_motion`, exact `eval_interference`). Its components are sibling part
  files the assembly references by relative path; a component saved outside the workspace
  is unloadable once the workspace is bundled into `work/` (FAIL, by design).
- Not covered: drawings (no checkable drawing deliverable is declared yet), ECAD
  schematics/PCBs (no native ECAD deliverable path), dynamics/multi-joint mechanisms, CAM,
  civil/site layout, and anything requiring the agent to report a number in prose (the
  retired mass/self-intersection prompts). Add them only as new tasks with artifact
  checks.
- Facilities/simulation tasks grade simplified engineering sizing against independent
  reference formulas; a PASS is not code certification.
- A stale or differently built engine changes what is measured; trust only trials whose
  manifest shows no newer core sources (or compare cohorts with identical hashes).
