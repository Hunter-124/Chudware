# CAM bench (IP-4)

**Measures:** gouge-free + machined-fraction on a small CAM samples set. A candidate
operation plan is simulated (`cam_simulate`); a **gouge is a hard fail** — no
`machined_fraction`, however high, masks it. Success bar: `gouges == 0 AND
machined_fraction >= 0.90` (`TARGET_FRACTION` in `tools/flywheel/bench/benches/cam.py`).

## Substrate ruling (work/questions/T-H8.md Q1)

The T-H4 verifier battery (`services/verify/battery.py`) is a thin dispatcher whose
`_extract_score` (reward_shape `"scalar"`) walks a priority key list and scores the first
key it finds in the result JSON. The full scalar key list (in priority order) is:
`match_pct`, `score`, `min_fos`, `cd`, `max_von_mises_mpa`, `coverage_ratio`,
`machined_fraction`. Scoring rules:

| Key | Transform | Notes |
|---|---|---|
| `match_pct` | `min(val / 100.0, 1.0)` | Volumetric match percentage from `eval_compare` |
| `score` | raw 0..1 | Generic scalar score |
| `min_fos` | `min(val / 2.0, 1.0)` | Factor of safety; FoS ≥ 2 → 1.0 |
| `cd` | raw 0..1 | Drag coefficient (CFD) |
| `max_von_mises_mpa` | 1.0 if `ok` else 0.0 | FEA stress pass/fail |
| `coverage_ratio` | raw 0..1 | Area coverage fraction |
| `machined_fraction` | raw 0..1 | Fraction of stock volume removed by toolpaths (from `cam_simulate`) |

**CAM gouge pre-gate (T-BL23, landed 2026-07-03):** before the key loop, `_extract_score`
checks `gouges` in the result. If `gouges > 0`, score is forced to `0.0` regardless of
`machined_fraction` — a gouge is a hard fail. `gouges` counts voxel cells cut below the
part surface by more than `gouge_tol = 0.05 mm` (`core/cam/stock_sim.h`). Only `gouges`
is a hard gate; `rapid_collisions` (rapid moves through stock) is surfaced as a finding
but does NOT force the score to zero.

**Historical note:** at the time of T-H8 Q1 (pre-T-BL23), `_extract_score` did not
recognize `machined_fraction` or check `gouges`, so this bench read `cam_simulate` raw.
T-BL23 added both keys to the battery; the bench now goes through the standard battery
path for `cam_simulate` checks.

## Dataset

8 committed CAM job fixtures under `jobs/*.json`, spanning 3 part families (the held-out
`structure` split unit):

| family | fixtures | notes |
|---|---|---|
| `pocketed_block` | 4 | clean pass, a **gouge** case (pocket driven below stock bottom), a gouge-free-but-below-target case (oversized tool leaves corners uncut), a boundary case (`machined_fraction == 0.90` exactly) |
| `drilled_plate` | 2 | clean pass, a non-gouge `rapid_collisions` warning case (must NOT be gated to zero — only `gouges` is a hard fail) |
| `contoured_part` | 2 | clean pass, a **gouge** case (ball-endmill radius exceeds a fillet radius) |

Each fixture is a plain-dict CAM job spec: `stock` + `tools` + `setup` + a candidate
`op_plan` (built against `services.mcp.cam_tools.cam_add_operation`'s real kwargs: `type,
tool_number, geometry, heights, params`) + a recorded `expected_sim` (a `cam_simulate`
-shaped result for that exact job). `spec.json` freezes the content hash
(`tools/flywheel/bench/dataset.py`); a second machine gets the same hash from the same
committed bytes.

**Deterministic baseline.** `deterministic_op_plan(part_family, stock)` in `cam.py` is a
fixed, no-model roughing(+finishing) template — a centered pocket+contour pass for milled
families, a centered through-hole for `drilled_plate`. It is the "kill-switch candidate op
plan" whose (hand-recorded, since there's no C++ core running in CI to regenerate it
offline) prior outputs are what each clean fixture's `expected_sim` represents. Because the
fixtures themselves ARE the deterministic candidate's recorded results, `baselines
["deterministic"]` is computed directly from the per-example scores, not from a second
run.

## Honesty note: offline-recorded vs. live-recomputed

- **Offline (default, no core):** each fixture's `expected_sim` is used as-is. This is a
  **recorded**, not recomputed, result — it does not verify that today's `cam_simulate`
  still produces those exact numbers for that job. This mirrors the same honesty posture as
  `regen_fix`'s fixture-mode scoring and the T-H8 Q2 answer for spec→sketch/CAM/VLM.
- **Live (C++ core present):** the same job (stock/tools/setup/op_plan) is built through
  `services.mcp.cam_tools` (`cam_new_job` → `cam_add_tool` → `cam_add_setup` →
  `cam_add_operation` → `cam_generate` → `cam_simulate`) and `cam_simulate` is genuinely
  recomputed. The battery's own `cam_simulate` check is also run (`services.verify.battery
  .verify(None, ["cam_simulate"])`) against the same in-process current job and its score
  recorded alongside as the through-battery signal. Guarded skip-if-no-core: resolution of
  the core library goes entirely through `services.mcp.core_bridge`'s own
  `$CHUDWARE_LIBCHUDWARE` / `$CHUDWARE_DIST` / platform search — no sibling worktree `.so` path is
  ever hardcoded.

## Run

```
python -m tools.flywheel.bench.runner cam          # full (8 fixtures)
python -m tools.flywheel.bench.runner cam --smoke  # smoke sample (first 4)
```

(The bench module exposes `DRIVER = BenchDriver("cam", _run)`; wiring it into
`tools/flywheel/bench/runner.py`'s `REGISTRY` is a separate, small integration step left to
the runner's owner since this task does not edit `runner.py`.)

Tests: `tools/flywheel/bench/tests/test_cam.py` —
```
~/.local/share/Chudware/mcp-venv/bin/pytest tools/flywheel/bench/tests/test_cam.py -q
```
