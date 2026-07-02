# T-E5 questions — sonnet-lux

## Q1: FMU backend — fmpy not available; native primary recommended

**Context:** The task specifies "One OpenHydraulics FMU circuit simulated via fmpy". Checked:
- `fmpy` not installed (`ModuleNotFoundError`)
- No `.fmu` files in the tree
- No Modelica compiler present (`omc`/`dymola` not found)
- OpenHydraulics ships Modelica *source*, not pre-compiled FMUs

**Recommended answer:** Follow the E2/E3 precedent — native hydraulic solver primary (Newton
nodal on pump curves + Bernoulli head), `fmpy`/FMU as an optional soft-fail backend. DoD box
for FMU marked `[~]` with reason: "fmpy not installed in CI; FMU backend is a seam (soft-fail
+ licenses.md row) following the CoolProp/thermo pattern from T-E3". The native solver covers
all the meaningful physics (pump head curves, cylinder sizing, valve Cv flow, accumulator
pre-charge). A future task can drop in a compiled FMU when a Modelica toolchain is available.

**Alternative:** Require fmpy install and block on FMU — not recommended (blocks the BOM
sourcing and sizing tools which are independently valuable and fully testable).

**Proceeding unblocked** with native solver + seam.

---

## Q2: Module placement — new `engine/hydraulic/` or extend `engine/piping/`?

**Context:** A hydraulic schematic is symbols + nets (hoses), not geometric 3-D pipe runs with
NPS schedules. ECAD is the structural precedent: `engine/ecad/schema.py` = symbols/nets, the
piping module = swept solids + fluid network. Hydraulic power circuits are closer to the ECAD
pattern than to piping.

**Recommended answer:** New module `engine/hydraulic/` with:
- `schema.py` — `aicad.hydraulic/1` doc kind, ISO 1219 symbol set (pump, cylinder, valve,
  accumulator, reservoir, motor, filter), nets = hoses
- `solve.py` — cylinder/pump sizing calcs (F=P·A, Q=A·v, pump power P=ΔP·Q/η)
- `bom.py` — sourcing field shape for bom_rollup seam

New contract file: `contracts/hydraulic_schema.md`.
MCP registration: new `hydraulic_tools.py` + append-only `server.py` block.

**Alternative:** Extend `engine/piping/` — not recommended; piping is a 3-D swept-solid +
fluid-network doc, hydraulic schematics have a different structure (symbols placed at [x,y],
hose nets connecting ports, no schedule/NPS, no 3-D path).

**Proceeding** with new `engine/hydraulic/` module.

---

## Q3: BOM sourcing seam — bom_rollup not on this branch

**Context:** `bom_rollup` / `BomLine` live on the B chain (B1a→B1b→B2) which has not been
merged to master. This branch (chained off T-E3) predates those.

**Recommended answer:** Emit hydraulic components with VendorPart-compatible sourcing fields
(same shape as `engine/vendor_parts.py VendorPart`: `mpn`, `manufacturer`, `description`,
`quantity`, `unit`, `category`). Test the field shape directly. Note join as merge-time seam
(same pattern as T-E4's CFD handoff). DoD box `[~]` with reason.

**Proceeding unblocked.**
