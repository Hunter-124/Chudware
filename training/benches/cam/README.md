# CAM bench (IP-4)

**Measures:** gouge-free + machined-fraction on a small CAM samples set. A candidate
operation plan is simulated (`cam_simulate`); a **gouge is a hard fail** — no
`machined_fraction`, however high, masks it. Success bar: `gouges == 0 AND
machined_fraction >= 0.90` (`TARGET_FRACTION` in `tools/flywheel/bench/benches/cam.py`).

## Substrate ruling (work/questions/T-H8.md Q1)

The T-H4 verifier battery (`services/verify/battery.py`) is a thin dispatcher: its generic
`_extract_score` only recognizes the scalar keys `match_pct/score/min_fos/cd/
max_von_mises_mpa/coverage_ratio`. `cam_simulate`'s real result fields —
`machined_fraction` (0..1) and `gouges` (int count) — are not in that list, so scoring
`cam_simulate` only through the battery silently degrades to a bare pass/fail `ok` and
discards exactly the two numbers this bench needs.

**Ruling (proceeding, non-blocking):** this bench reads `cam_simulate`'s **raw** result
directly — `sim["results"][i]` with fields `{ok, error, removed_volume_mm3,
remaining_volume_mm3, stock_volume_mm3, machined_fraction, cycle_time_s, gouges,
max_gouge_mm, rapid_collisions, warnings}` — and separately records the battery's own
`cam_simulate` check score (a bare 0/1) alongside as the through-battery signal
(`detail["battery_cam_simulate"]`, live mode only). `services/verify/battery.py` is **not
modified** by this bench; extending `_extract_score` to understand CAM's scalar fields is
a T-H4 follow-up, filed but out of scope here.

## Dataset

8 committed CAM job fixtures under `jobs/*.json`, spanning 3 part families (the held-out
`structure` split unit):

| family | fixtures | notes |
|---|---|---|
| `pocketed_block` | 4 | clean pass, a **gouge** case (pocket driven below stock bottom), a gouge-free-but-below-target case (oversized tool leaves corners uncut), a boundary case (`machined_fraction == 0.90` exactly) |
| `drilled_plate` | 2 | clean pass, a non-gouge `rapid_collisions` warning case (must NOT be gated to zero — only `gouges` is a hard fail) |
| `contoured_part` | 2 | clean pass, a **gouge** case (ball-endmill radius exceeds a fillet radius) |

Each fixture is a plain-dict CAM job spec: `stock` + `tools` + `setup` + a candidate
`op_plan` (built against `mcp_server.cam_tools.cam_add_operation`'s real kwargs: `type,
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
  `mcp_server.cam_tools` (`cam_new_job` → `cam_add_tool` → `cam_add_setup` →
  `cam_add_operation` → `cam_generate` → `cam_simulate`) and `cam_simulate` is genuinely
  recomputed. The battery's own `cam_simulate` check is also run (`services.verify.battery
  .verify(None, ["cam_simulate"])`) against the same in-process current job and its score
  recorded alongside as the through-battery signal. Guarded skip-if-no-core: resolution of
  the core library goes entirely through `mcp_server.core_bridge`'s own
  `$AI_CAD_LIBAICAD` / `$AI_CAD_DIST` / platform search — no sibling worktree `.so` path is
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
~/.local/share/AI-CAD/mcp-venv/bin/pytest tools/flywheel/bench/tests/test_cam.py -q
```
