---
id: T-E3
title: Gas/heat networks + fluid properties
workstream: E
owner: opus-reed
status: doing
branch: task/T-E3-gas-heat-props
day: 8
depends_on: [T-E2]
---

## Context

pandapipes (BSD-3) backend for compressible gas and heat networks; CoolProp (MIT) for thermophysical properties (replaces hardcoded density/viscosity everywhere in the piping module). Operating-point solve: intersect pump curve with system curve.

## Definition of done

- [ ] pandapipes backend selectable per network type
- [ ] CoolProp property lookups wired into solver inputs
- [ ] Operating-point tool returns duty point + NPSH margin

## AI hints

- pandapipes: https://github.com/e2nIEE/pandapipes
- CoolProp: https://github.com/CoolProp/CoolProp
