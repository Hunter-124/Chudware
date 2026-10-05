---
id: T-BL14
title: GNN adapter training for GO families + sw_fixtures STEP export
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL14-gnn-adapter-training
depends_on: []
lane: ml-flywheel
kind: new
priority: 5
deferred: yes
---

## Context

Deferred follow-ups from the AI-experience lane, parked so they are not lost:

1. **GNN adapter training** — `docs/review/RECOGNITION_RESIDUAL.md` gave a GO verdict for
   exactly three families (ribbed_bracket, shelled_enclosure, swept_pipe); deterministic
   recognition suffices elsewhere. Train per `tools/gsl_gnn/README.md`. Gated on that
   procedure; do not start speculative training for other families.
2. **sw_fixtures STEP export** — `corpora/sw_fixtures/*.step` are absent so the
   SW-fixture eval path in `tools/gsl_eval.py` is dormant. Export STEPs from the SLDPRT
   fixtures to activate it.

## Definition of done

- [ ] Adapters trained + evaluated for the three GO families only, results appended to
      docs/review/RECOGNITION_RESIDUAL.md
- [ ] sw_fixtures STEPs present; gsl_eval picks them up and reports
