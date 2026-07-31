---
name: design-for-manufacturability
description: Apply process-specific design-for-manufacturability checks without inventing a process, overconstraining the design, or treating advisory heuristics as universal blockers.
inject: auto
triggers:
  keywords: [design for manufacturability, manufacturability, DFM review, manufacturing review, injection molding, CNC machining, FDM printing, SLA printing]
  tools: [set_process, resolve_process, dfm_check]
  phases: []
---

# Design for Manufacturability

## Sequence

1. Identify the intended process, material, scale, and production quantity only when they affect the decision.
2. Resolve the process profile; if a missing choice changes geometry, ask rather than assume.
3. Apply only relevant checks: wall/tool access for machining, wall/overhang for printing, wall/draft for molding, bend rules for sheet metal.
4. Run `dfm_check`, fix named measured failures, and rerun it before export or release.
5. Record the selected process and any deliberate exception with its rationale.

## Restraint

- Do not block a concept on a rule for a process the user did not choose.
- Treat heuristics as guidance until a selected process profile or measured failure makes them actionable.
- Prefer one targeted fix over speculative redesign.
