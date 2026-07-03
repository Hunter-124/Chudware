---
id: T-BL23
title: Verifier battery CAM scoring keys - machined_fraction + gouges in _extract_score
workstream: backlog
owner: sonnet-gorse
status: doing
branch: task/T-BL23-battery-cam-keys
depends_on: []
lane: cam
kind: fix
priority: 3
---

## Context

**Filed 2026-07-03 (Fable advisor, closing T-H8 Q1).** `battery.py::_extract_score`
recognizes only scalar keys (`match_pct`/`score`/`min_fos`/...). `cam_simulate` emits
`machined_fraction` and `gouges`, so any CAM result routed through the shared verifier
battery collapses to bare pass/fail — silently discarding a machining-safety metric
(gouges) and a coverage metric. The T-H8 CAM bench works around this by reading
cam_simulate raw; the shared infra should not mislead the next consumer.

## Definition of done

- [ ] `machined_fraction` recognized as a scalar score key
- [ ] `gouges` recognized as a binary-safety key (any gouge → fail, count surfaced)
- [ ] T-H8 CAM bench switched off its raw-read workaround onto the battery path,
      producing identical verdicts on its existing fixtures
- [ ] Battery unit tests cover both new keys
- [ ] req_join's ad-hoc `met_statuses = ("pass","verified","ok")` reconciled onto the
      T-H3 `VALID_STATUSES` ladder (see T-H8 questions file, second item)
- [ ] `_gap_is_manifold` fixed for OCCT builds (T-BL19 finding): it reads a `free_edges`
      scalar `eval_check` never emits, so it blindly passes ANY body. Parse the `issues`
      list instead — `free_edges` issue or error-severity = non-manifold; `tiny_edges`
      warnings at curved poles/seams are cosmetic. Port the correct predicate from
      `scripts/modeling_battery.py` (T-BL19 branch).

## AI hints

- `work/questions/T-H8.md` Q1 — exact key list and the workaround location
- `graphify query "verifier battery score extraction"` — battery.py structure (T-H4)
