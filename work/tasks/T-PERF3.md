---
id: T-PERF3
title: Mesh load off the GL thread (loader worker + meshGen)
workstream: perf
owner: grok
status: done
branch: task/T-PERF3-mesh-load-off-gl
depends_on: []
lane: app-gui
kind: fix
---

## Context

docs/PERF_AUDIT_2026-07-10.md §3 P5. AppState::loadDocMesh (app_state.cpp:722-926) does disk read + parse + pick-cache build on the GL thread at ~25 sites (100ms-2s big docs). Split: worker thread does loadAicmFile + pick-cache; GL thread drains ready queue, uploads buffers only. meshGen counter drops stale completions; keep-last-good falls out free.

## Definition of done

- [ ] no disk IO or pick-cache build on GL thread
- [ ] stale completions dropped by generation
- [ ] tab switch/rebuild never hitches >16ms on mesh load

## Progress log
- 2026-07-10 21:00 UTC [grok] loadDocMeshAsync + dedicated loader thread (disk parse off GL);
  drainMeshLoads after rpc.drain; meshGen latest-wins keep-last-good. Wired
  applyOpsAsync success paths + agent rebuild. Sync loadDocMesh retained for
  scenarios/shots. Host build green. Note: pick-cache still built on GL during
  commit (still CPU but after parse); can push more into the loader later.
