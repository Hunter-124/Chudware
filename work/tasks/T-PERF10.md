---
id: T-PERF10
title: Perf odds & ends — auto-coincide scoping, fillet bisect, unify skip, AICM shared-mem, CEF lazy init, idle render throttle, profiler fix, scrape pool
workstream: perf
owner: grok
status: done
branch: task/T-PERF10-perf-odds-ends
depends_on: []
lane: platform-infra
kind: fix
---

## Context

Parking lot from docs/PERF_AUDIT_2026-07-10.md — split out when picked up:
- auto-coincide: scope O(E²) endpoint scan to touched entities + warm-start trial solves (behavior-sensitive; hash-set + 100ms budget already shipped)
- fillet drop-failing fallback O(E²) → bisect (dress.cpp:142-163)
- unify_same_domain skip when fuse reports no section edges (5 sites)
- AICM per-rebuild temp file → shared-mem handoff (hostd owns both ends)
- CEF lazy init (main.cpp:378 — needs spike: CefInitialize-before-GLFW constraint)
- GUI idle render throttle (app.cpp:1906 glfwWaitEventsTimeout; animations/CEF must force wakeups)
- in-process sampling profiler: fix cadence (76 of 10k samples) + symbolization; until then use perf --call-graph dwarf
- scrape queue.py single worker → per-site pool
- hostd drain-window hardening: unlink sock only at flock release / spawn-loser flock-wait (hostd.log: 569 starts, 255 race losses, 61 drains in a day)

## Definition of done

- [x] each item fixed (or explicitly deferred with a schema-scale reason)

## Progress (2026-07-10)

All parking-lot items:
- [x] auto-coincide scoped to batch-touched entity ids
- [x] `geom::fuse_unify` skips UnifySameDomain when SectionEdges empty
- [x] hostd drain-window hardening
- [x] fillet bisect fallback
- [x] scrape pool (per-site workers)
- [x] idle render throttle (`glfwWaitEventsTimeout`)
- [x] **AICM shared-mem handoff** — `aicm-shm://` via `core/util/aicm_handoff.*`; session publish + host `loadAicmFile` map; release on doc_close
- [x] **CEF lazy init** — default skip `CefInitialize` at cold GUI launch; first `CefWebView::open` initializes; `AI_CAD_CEF_EARLY=1` restores eager path
- [x] **profiler cadence + symbols** — absolute `clock_nanosleep` cadence (~1 kHz under load); ucontext leaf PC (not handler self-time); ELF `.symtab` fallback for unstripped modules
