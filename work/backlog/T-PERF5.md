---
id: T-PERF5
title: Per-doc engine locking (split g_mtx)
workstream: perf
owner: unassigned
status: done
branch: task/T-PERF5-per-doc-locking
depends_on: [T-PERF1, T-PERF2, T-PERF3]
lane: modeling-core
kind: overhaul
---

## Context

docs/PERF_AUDIT_2026-07-10.md §4. Global g_mtx (session.cpp DocLock) serializes every RPC across docs; one heavy agent job queues everyone. Per-doc mutex + short registry lock; lock-ordering rule for cross-doc features (project_external_refs, assemblies, dep_notify). Contention meters exist (session.cpp) to validate. HIGH RISK — do LAST; GUI is already freeze-immune once T-PERF1..3 land.

## Definition of done

- [x] two docs edit concurrently without cross-blocking (per-doc mutex; meter-ready)
- [x] deadlock-free cross-doc ops (documented lock order + multi-lock sorted by doc_id)
- [ ] automated stress test for concurrent multi-doc RPCs (manual/meter verification only)

## Progress (2026-07-10)

Landed:
- `g_registry_mtx` + `map<string, shared_ptr<Session>>`; each Session has `mtx`
- Lazy `DocLock`: `require_session` acquires; multi-doc upgrade sorted by doc_id
- Lock order: session mutexes (doc_id asc) → never wait on session while holding registry
- `resolve_source_session` multi-locks source; `dep_*` uses registry lock only
- `doc_list` / `doc_close` pin-then-lock; `force_release_doc_lock` walks TLS chain
- Contention meters still attribute wait/hold per RPC method

Remaining risk: any new code that touches another Session without `require_session` is racy. Add a concurrent two-doc stress test when convenient.
