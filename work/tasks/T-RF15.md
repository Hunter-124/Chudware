---
id: T-RF15
title: RF stream-Z firmware fw_* cutover to core/ (node R4a)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF15-firmware-cpp
depends_on: [T-RF3]
lane: ecad-firmware
kind: overhaul
priority: 3
---

## Context

Zero-Python stream: firmware RPC (fw_*) is Python-only in engine_oracle/firmware/
(contracts/firmware_rpc.md already frozen). Web client cannot reach python-worker
methods, so firmware is desktop-only until this lands. Routing table in
services/worker/worker_client.cpp is integrator-owned; core/api/libaicad.h merges go
through the integrator.

## Scope

Port the firmware engine surface to core/firmware (compile loop, pin-map, Renode
emulation seam, probe-rs hardware path per the frozen contract), reach parity on its
corpus, flip routing default, and delete engine_oracle/firmware/ in the same commit.
Mirror tests in core/tests/.

## Definition of done

- [x] fw_* served by libaicad with corpus parity; Python module deleted same commit.
- [x] MCP firmware tools unchanged on the surface (freshness rule if touched).
- [x] gate.sh green; REFACTOR_LOG handoff; web_parity inventory shrinks.
