---
id: T-FIN15
title: Freeze aicad.simhandoff/1 - generalized cross-sim handoff and verify contract
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN15-simhandoff-contract
depends_on: []
lane: simulation
kind: new
priority: 2
---

## Context

piping_cfd_handoff/piping_verify_cfd proved the pattern: 1D/authoring-domain solve ->
one-call geometry+BC handoff into a 3D solver -> verify tool cross-checking the two with a
tolerance verdict. Generalize it: contracts/sim_handoff.md defining handoff envelopes
(source result ref, derived geometry refs, generated BCs/loads with provenance, units),
verify envelopes (quantity compared, tolerance, verdict), and study provenance so any
result can name what produced it. Refactor the piping pair to declare conformance (no
behavior change) and provide a small helper lib for building handoffs so T-FIN16/19/20/22
implement against one seam.

## Files / areas touched

- contracts/sim_handoff.md — new frozen contract
- services/mcp/simhandoff.py — envelope builder/validator helpers
- services/mcp/piping_tools.py — declare conformance, adopt helpers
- tests: envelope validation + piping regression unchanged

## Definition of done

- Contract documents handoff+verify envelopes with worked piping example
- Helper lib validates envelopes; piping tools pass through it with identical results
- cad_guide('simulation') gains a cross-sim handoff section
