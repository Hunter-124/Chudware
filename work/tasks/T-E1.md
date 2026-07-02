---
id: T-E1
title: Piping module scaffold + fitting catalog
workstream: E
owner: opus-lark
status: doing
branch: task/T-E1-piping-scaffold
day: 6
depends_on: [T-INT]
---

## Context

New discipline module following the ECAD precedent: contracts/piping_schema.md + engine/piping/. Pipe-run feature = OCCT sweep along a routed path in the assembly; fittings (elbow/tee/reducer/flange) from a CSV-backed catalog. B36.10/19 schedule data comes free from the MIT-licensed `fluids` library (fluids.piping); B16 fitting dims built from public manufacturer catalogs (ASME text itself is not bundleable).

## Definition of done

- [ ] `engine/piping/` with pipe_run + fitting feature types compiling to B-rep
- [ ] contracts/piping_schema.md frozen
- [ ] CSV fitting catalog with nearest_pipe() NPS lookup via fluids
- [ ] Routed pipe renders in viewport and exports STEP

## AI hints

- `graphify query "assembly routing sweep path"`
- fluids.piping source: https://github.com/CalebBell/fluids
- OSE Piping workbench CSVs (LGPL) as data reference: https://github.com/rkrenzler/ose-piping-workbench
