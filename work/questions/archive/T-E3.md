# Questions — T-E3 (Gas/heat networks + fluid properties)
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

## Q1 — Backend: native compressible/heat solver vs. pandapipes  (RESOLVED-BY-DRIVER, advisor sign-off requested)

**Context.** The task-file DoD names a `pandapipes` (BSD-3) backend and `CoolProp` (MIT)
for properties. Neither is installed in the tree, and both are new pip dependencies. This
is the exact situation the T-E2 supervisor ruling already decided for WNTR/EPANET: *native
solver primary, `fluids` for commodity correlations, WNTR/EPANET descriptive-of-intent.*
My DRIVER addenda bind that ruling to T-E3: *"gas/heat network solving extends the native
`solve.py` pattern; fluid properties come from established OSS (`fluids`/`ht`/`thermo`
family)."*

**Decision taken (proceeding unblocked).** Mirror the E2 ruling exactly:

1. **Gas mode** — extend `engine_oracle/piping/solve.py` with a native *isothermal
   compressible* network solve. Potential variable = absolute P² (Pa²); nodal **mass**
   conservation (Weymouth/general-isothermal link equation, friction from `fluids`). This
   is the direct compressible analogue of the incompressible head solve, exactly as the
   MNA circuit solver is the analogue there. Selectable per network type via
   `network_type="gas"|"liquid"`.
2. **Properties** — new `engine_oracle/piping/properties.py`. Backend order:
   CoolProp (MIT) → thermo (MIT, Caleb Bell family) when importable (feature-detected,
   soft-fail), else a **cited** reference-data + first-principles fallback (ideal-gas
   density from cited molar mass; Sutherland viscosity with cited constants; IAPWS/NIST
   cited water spot values). **Every returned value carries a `source` citation; nothing
   is fabricated. Out-of-range / unknown fluid ⇒ contract-shaped `{ok:false,reason,hint}`.**
   This is what "CoolProp property lookups wired into solver inputs" delivers — the solver
   pulls rho/mu (and vapour pressure for NPSH) from `properties.py` instead of the
   hardcoded WATER dict.
3. **Operating point** — new `operating_point()` in `solve.py` + `piping_operating_point`
   MCP tool: intersect the pump curve with the system curve → duty point (Q*, H*) and
   NPSH available margin (NPSHa − NPSHr).

**Rationale.** Same four points as T-E2 Q1: (a) addenda are explicit direct orders; (b)
pandapipes/CoolProp are not present — hard-depending on them would make gate/pytest fail
on an uninstalled dep and enlarge the license surface; (c) `circuit.py`/`solve.py` are the
house pattern for network solves; (d) the DoD *substance* (per-network-type solve, tool/
cited property lookups feeding the solver, duty-point + NPSH margin) is delivered natively.

**Recommended answer:** APPROVE native compressible-gas solver + cited/optional-backend
properties module as primary; read "pandapipes"/"CoolProp" in the DoD as
descriptive-of-intent. CoolProp/thermo remain welcome as optional feature-detected
backends (licenses.md rows added) if ever installed; not required for T-E3.

**Alternative:** add CoolProp (MIT) as an installed hard dep + pandapipes (BSD-3) backend
— deferred unless the advisor wants it now; both would be optional/soft-fail regardless.
