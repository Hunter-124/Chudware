---
id: T-Q4
title: User manual auto-generation — cad_guide + inventory → living docs
workstream: Q
owner: opus-flint
status: doing
branch: task/T-Q4-prod
day: 4
depends_on: []
---

## Context

PROJECT-LEAD ADDITION: docs rot. We already generate a 757-row UI inventory (T-G12) and
have rich cad_guide topics + the deliverables engine — compose them into a regenerable
user manual instead of hand-writing one.

## Files / areas touched

- tools/manual_gen.py: compose cad_guide topics + ui_inventory output + contracts
  summaries into docs/manual/ markdown; render to PDF via the deliverables engine
  (make_report path) as the distributable
- CI-able check: manual regen is clean (no drift) — advisory gate stage

## Definition of done

- [ ] docs/manual/ regenerates deterministically; PDF renders via deliverables
- [ ] Coverage assert: every ribbon command in the inventory appears in the manual
      (name + one-liner minimum, auto-stubbed where no guide text exists)
- [ ] Drift check script + advisory wiring
- [ ] gate; graphify update .

## Driver notes (Fable)

- make_manual was RESERVED in the deliverables registry (T-F15 question ruling) — this
  task is why. Land the template through the frozen registry path, provenance rules
  intact (generated sections tagged; no fabricated prose about unimplemented features —
  the inventory is the truth source).
