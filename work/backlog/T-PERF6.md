---
id: T-PERF6
title: Kernel copy amplification — checkpoint O(N²), blob out-of-line, hit-path copies
workstream: perf
owner: unassigned
status: todo
branch: task/T-PERF6-kernel-copy-amplification
depends_on: []
lane: modeling-core
kind: overhaul
---

## Context

docs/PERF_AUDIT_2026-07-10.md §4 P1/P2. (a) capture_ckpt (document.cpp:1635-1658) value-copies results/statuses per feature = O(N²); import_cache keyed BY multi-MB embedded_brep blob string (1547-1553) → shared_ptr<const FeatureResult> + hash keys. (b) embedded_brep out-of-line content-addressed store (fixes per-edit whole-doc copies, undo dumps, chain re-dump at 1576). Schema/migration touch: save/load, PLM vault, Python readers. (c) cache-hit deep copies (1585, 7065, 7119) → shared/COW mesh buffers.

## Definition of done

- [ ] edit-last on 50-feature import-heavy doc: no MB-scale copies (profiled)
- [ ] full rebuild of deep tree ≥5x faster on checkpoint capture
- [ ] parity corpus green (bit-identical restore invariant kept)

## Progress (2026-07-10)

Landed in tree (status left todo for parent board):
- (a) `FeatureResultMap` shared_ptr slots + shared status_ptrs in CompileCkpt; import_cache
  keyed by FNV-1a(blob) not the blob string. Capture/restore share unchanged results.
- (c) `aicm::CowVec` COW on AicmBody mesh buffers; body-cache hit is pointer-share + stamp.
- (b) **deferred**: out-of-line content-addressed embedded_brep store (save/load/PLM schema).
