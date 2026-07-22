---
id: T-RF14
title: RF wave-3 (stretch) doc_open mmap/zero-copy + tiny-op marshalling fast path (node R3b)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF14-docio-mmap
depends_on: [T-RF13]
lane: modeling-core
kind: new
priority: 4
---

## Context

PERF_AUDIT_2026-07-11.md took doc_open from 85→440 opens/s; the remaining floor is
memmove (75%) — mmap/zero-copy was noted as future work. docs/CPP_PERF.md: the C++ core
LOSES on tiny ops (evaluate 0.5×, STL export 0.6×) due to the C-ABI JSON marshalling
floor. STRETCH: only run if perf numbers say it matters.

## Scope

mmap/zero-copy read path for .step+ doc_open (cache.aicm already STORED/raw-copy), and a
marshalling fast path for high-frequency tiny ops (eval_*, export) if the probe shows
the floor is user-visible. Behind the same perf ratchet (tools/perf_regression.py +
Webservice perf_probe).

## Definition of done

- [ ] Measurable opens/s or tiny-op latency win on the ratchet, no regressions.
- [ ] gate.sh green; REFACTOR_LOG handoff; skip-with-reason is an acceptable close.
