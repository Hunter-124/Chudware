---
id: T-PERF5
title: Per-doc engine locking (split g_mtx)
workstream: perf
owner: unassigned
status: todo
branch: task/T-PERF5-per-doc-locking
depends_on: [T-PERF1, T-PERF2, T-PERF3]
lane: modeling-core
kind: overhaul
---

## Context

docs/PERF_AUDIT_2026-07-10.md §4. Global g_mtx (session.cpp:196 DocLock) serializes every RPC across docs; one heavy agent job queues everyone. Per-doc mutex + short registry lock; lock-ordering rule for cross-doc features (project_external_refs, assemblies, dep_notify). Contention meters exist (session.cpp:207-234) to validate. HIGH RISK — do LAST; GUI is already freeze-immune once T-PERF1..3 land.

## Definition of done

- [ ] two docs edit concurrently without cross-blocking (meter-verified)
- [ ] deadlock-free cross-doc ops (documented lock order + stress test)
