---
id: T-PERF7
title: Owner-map checkpointing + size-relative tess deflection (DECISION)
workstream: perf
owner: unassigned
status: todo
branch: task/T-PERF7-ownermap-tess-deflection
depends_on: []
lane: modeling-core
kind: fix
---

## Context

docs/PERF_AUDIT_2026-07-10.md §4 P2. (a) owner map rebuilt over ALL intermediates every compile (document.cpp:7143-7215) even when features reused — fold face/edge owners into CompileCkpt. (b) DECISION NEEDED: tess deflection is fixed 0.05mm absolute (7082) — huge parts mesh at millions of tris. Fix = bbox-diagonal-relative, but golden-image/mesh-hash tests churn. Hunter to approve the visual/test-baseline change before implementing (b).

## Definition of done

- [ ] owner_ms ~0 on incremental edits (phase_ms verified)
- [ ] (if approved) 2m part meshes in O(100k) tris; goldens re-baselined
