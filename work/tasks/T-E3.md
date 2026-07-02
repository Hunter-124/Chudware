---
id: T-E3
title: Gas/heat networks + fluid properties
workstream: E
owner: opus-reed
status: done
branch: task/T-E3-gas-heat-props
day: 8
depends_on: [T-E2]
---

## Context

pandapipes (BSD-3) backend for compressible gas and heat networks; CoolProp (MIT) for thermophysical properties (replaces hardcoded density/viscosity everywhere in the piping module). Operating-point solve: intersect pump curve with system curve.

## Definition of done

- [x] pandapipes backend selectable per network type — native isothermal-compressible
      **gas** solve added to `engine/piping/solve.py` (`network_type="gas"`, Weymouth-form
      mass-balance on P²), selectable per network type alongside the existing liquid solve.
      Per the T-E2 WNTR ruling (see `work/questions/T-E3.md` Q1 + `work/questions/T-E2.md`),
      "pandapipes" is descriptive-of-intent; the native solver is primary. pandapipes remains
      an optional future backend behind the same seam.
- [x] CoolProp property lookups wired into solver inputs — new `engine/piping/properties.py`
      resolves the solver `fluid` (`{"name","T","P"}`) to sourced rho/mu/pvap. CoolProp (MIT)
      + thermo (MIT) are optional feature-detected backends; a **cited** built-in model
      (ideal-gas + Sutherland + IAPWS water table) is the dependency-free fallback. Never
      fabricates: unknown fluid / out-of-range → contract-shaped. licenses.md rows added.
- [x] Operating-point tool returns duty point + NPSH margin — `solve.operating_point()` +
      MCP `piping_operating_point`: intersects pump curve with system curve (explicit or
      doc-derived) → duty point (Q*, H*) + NPSHa/NPSHr/margin (vapour pressure sourced).

Verification: `python -m pytest engine_oracle/piping -q` → 38 passed (gas single-pipe vs
Weymouth closed form; duty point vs analytic root; NPSH by hand). `bash scripts/gate.sh` fails
only on the three KNOWN pre-existing stages (check.py FAIL-RECORDED; ui/imgui
`PanelField::selMulti`; pytest marker-filter "25 deselected"/exit-5) — none touched here.

## AI hints

- pandapipes: https://github.com/e2nIEE/pandapipes
- CoolProp: https://github.com/CoolProp/CoolProp
