# Narration Style Guide

Shared contract for all AI-CAD text-narration tools (`describe_toolpaths`, `describe_results`,
and any future narration tool). Tools that produce structured text about CAD/CAM/FEA data
**must** follow this guide instead of ad-hoc formatting.

---

## Core principle: narrate, never recompute

Every numeral in the output text **must be traceable to a named tool call or a named key
in the tool's response payload**. The narration layer is a lens over existing numbers — it
does not recompute derived values except:

- Factor of safety: `yield_mpa / max_von_mises_mpa` (explicitly labelled as such)
- Margin: `yield_mpa - max_von_mises_mpa` (explicitly labelled as such)

All other values (stress, displacement, cycle time, gouge counts, G-code bounds, feed
ranges) are copied from the source tool response and attributed with `(source: <tool>)`.

---

## Rounding conventions

| Quantity | Precision | Example |
|---|---|---|
| Linear dimensions (mm) | 3 significant figures (use `:.3g`) | `6.35mm`, `0.254mm` |
| Stresses (MPa) | 1 decimal place | `247.3 MPa` |
| Displacements (mm) | 3 decimal places | `0.012 mm` |
| Factors of safety | 2 decimal places | `1.43` |
| Percentages | 1 decimal place | `87.5%` |
| Cycle times (seconds) | 1 decimal place | `142.7s` |
| Feed rates (mm/min) | 0 decimal places (integer) | `1500 mm/min` |
| Frequencies (Hz) | 1 decimal place | `234.1 Hz` |
| G-code coordinate bounds | 3 significant figures | `X[-5.00..95.0] mm` |

---

## Adjective rule

**No adjective without a number behind it.** Forbidden patterns:

- "high stress" — replace with "stress 312.4 MPa (94% of yield)"
- "slow cycle time" — replace with "cycle 847.2s"
- "large displacement" — replace with "displacement 2.341 mm"

---

## Graceful degradation

When data is missing, say so **explicitly** — never silently omit a section.

Required phrases (use these exact patterns):

| Situation | Required output |
|---|---|
| No simulation results | `No simulation results attached — run run_fea_static or run_fea_modal first.` |
| No CAM job active | `No CAM job. Call cam_new_job first.` |
| Toolpaths not generated | `Toolpaths not yet generated — run cam_generate first.` |
| Simulation not run | `No simulation results — run cam_simulate for cycle time and gouge checks.` |
| G-code not available | `G-code: not available — run cam_post first.` |
| Face/stress location unknown | `Stress location: not resolved — call hover(sid) after query_geometry().` |
| No study by that name | `No study named '<name>'. Available: [<list>]` |

---

## Stable sid naming

Face references in narration text use the `sid` identifiers from `describe_part`'s face
layer — never raw face ordinals or coordinates. Example: `face f3 (planar on top)`.

When a sid cannot be resolved, state it explicitly rather than omitting:
`face location not resolved — call hover(sid) to locate`.

---

## Section order

All narration output follows this canonical section order:

1. **Header summary** — tool/job/study name, type, counts (setups, operations, DOF, etc.)
2. **Per-item details** — one block per setup/op or per result category
3. **Caveats / warnings** — convergence notes, occlusion disclaimers, missing-data notices

Tools **must not** reorder these sections even when some are empty (use the graceful
degradation phrases to fill empty sections).

---

## Source attribution

Each scalar value carries a parenthetical source tag at line end:

```
Max von Mises stress: 247.3 MPa  (source: run_fea_static)
Max displacement: 0.031 mm  (source: run_fea_static)
Mode 1: 234.1 Hz  (source: run_fea_modal)
```

The G-code digest attributes its values as `(source: cam_post G-code parse)`.

---

## Tools covered by this contract

### `describe_toolpaths` (CAM)

Data sources (in call order):

1. `cam_job_tree` → op list: strategy/type, tool_number, name, setup hierarchy
2. `cam_simulate` → `gouge_count`, `rapid_through_count`, `machined_fraction_pct`,
   cycle time per setup
3. `cam_post` → G-code text parsed for: coordinate bounds (X/Y/Z from G0/G1 lines),
   feed range (F values), tool-change count (T+M6 lines)

Output skeleton:

```
CAM job "<name>" — <N> setup(s), <N> operation(s) total
Setup "<name>" [<machine>] — <N> ops
  Op "<name>" [<type>]: T<num> <tool_type> ø<diam>mm
    Stepdown <val>mm, stepover <val>mm, stock-to-leave <val>mm
    Engage: <val>%
    Simulation: machined <pct>%, cycle ~<sec>s
    Gouges: <N>; rapid-through-stock: <N>
  G-code digest: <N> lines, bounds X[<min>..<max>] Y[<min>..<max>] Z[<min>..<max>] mm,
    feed <min>–<max> mm/min, <N> tool change(s)
(Source: cam_job_tree, cam_simulate, cam_post — numbers narrated, never recomputed)
```

### `describe_results` (FEA/CFD)

Data sources:

1. `doc_studies` → stored study metadata (type, material, mesh parameters)
2. Study `results` dict: `max_von_mises_mpa`, `max_disp_mm`, `min_fos`, `n_dof`,
   `freqs_hz[]`, CFD scalars
3. `doc_study_overlay` (best-effort) → per-face max von Mises for sid join

Output skeleton:

```
FEA results — "<study_name>" (<type>)
Material: <name> (yield <val> MPa, source: run_fea_static material record)

Static stress:
  Max von Mises stress: <val> MPa  (source: run_fea_static)
  Stress location: face <sid> (<surf> on <side>)
  Max displacement: <val> mm  (source: run_fea_static)
  Min factor of safety: <val>  (yield / max stress; >1 means holds)
  Margin: <val> MPa above/below yield
  Mesh: <n_dof> DOF  (mesh_fineness <val>, tet10 elements)

Modal frequencies (first <N> modes):
  Mode 1: <val> Hz  (source: run_fea_modal)
  ...

Convergence note: tet10 (2nd-order) elements used; re-run with finer mesh
(mesh_fineness → 0.8) to verify convergence for final design.
(Source: doc_studies — numbers narrated from stored study, never recomputed)
```

---

## Adding a new narration tool

1. Name it `describe_<domain>` (lowercase, underscore-separated).
2. List every data-source tool call at the top of its docstring with the key names it reads.
3. Follow the section order: header → per-item → caveats.
4. Apply the rounding table above.
5. Apply the graceful-degradation phrases.
6. End the output with a `(Source: ...)` attribution line.
7. Add a test in `test_gsl_tools.py` with at least: (a) graceful-no-data case, and (b)
   happy-path case asserting key slot presence (MPa, mm, Hz, etc.) and at least one
   numeral via regex.
