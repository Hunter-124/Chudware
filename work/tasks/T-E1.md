---
id: T-E1
title: Piping module scaffold + fitting catalog
workstream: E
owner: opus-lark
status: done
branch: task/T-E1-piping-scaffold
day: 6
depends_on: [T-INT]
lane: simulation
kind: new
---

## Context

New discipline module following the ECAD precedent: contracts/piping_schema.md + engine/piping/. Pipe-run feature = OCCT sweep along a routed path in the assembly; fittings (elbow/tee/reducer/flange) from a CSV-backed catalog. B36.10/19 schedule data comes free from the MIT-licensed `fluids` library (fluids.piping); B16 fitting dims built from public manufacturer catalogs (ASME text itself is not bundleable).

## Definition of done

- [x] `engine/piping/` with pipe_run + fitting feature types compiling to B-rep
- [x] contracts/piping_schema.md frozen
- [x] CSV fitting catalog with nearest_pipe() NPS lookup via fluids
- [x] Routed pipe renders in viewport and exports STEP

## Completion notes (opus-lark)

- `engine/piping/` (`schema.py`, `catalog.py`, `threed.py`, `data/fittings.csv`) + tests. `engine`
  is a symlink to `engine_oracle`, so files live under `engine_oracle/piping/`.
- pipe_run = genuine OCCT sweep of an annular section along the routed 3-D centreline with
  tangent-arc fillets at bends (bend radius honoured; spline fallback). Fittings elbow/tee/reducer/
  flange build parametric B-rep from the CSV + fluids sizes.
- `catalog.nearest_pipe` wraps `fluids.piping.nearest_pipe` (MIT), mm out; degrades contract-shaped
  `{ok:false,reason,hint}` when fluids absent or size unknown. `fitting_dims` similar.
- `piping_to_step` -> STEP on disk; `piping_to_part_doc` -> `aicad.part/1` import feature (same
  envelope as ECAD `board_to_part_doc`) so the host opens it in the viewport. STEP round-trips
  (re-import non-empty) in `test_threed`.
- requirements.txt + contracts/licenses.md updated (fluids MIT row).
- NOTE: `depends_on: [T-INT]` is still unmet — the piping module is self-contained (imports only
  `engine.piping.*` + build123d/fluids), so nothing here blocks on T-INT; a future integration
  seam (MCP `piping_*` tools / doc-router wiring) is deliberately out of this scaffold's scope.

## AI hints

- `graphify query "assembly routing sweep path"`
- fluids.piping source: https://github.com/CalebBell/fluids
- OSE Piping workbench CSVs (LGPL) as data reference: https://github.com/rkrenzler/ose-piping-workbench
