---
id: T-RF3
title: RF wave-0 gate hardening — WASM smoke + web parity probe + check.py fatal (node R0c)
workstream: RF
owner: integrator
status: done
branch: task/T-RF3-gate-hardening
depends_on: [T-RF1, T-RF2]
lane: platform-infra
kind: new
priority: 2
---

## Context

The web app (chudware.com) is the same host/src/ui compiled to WASM on the shared
chudware-hostd daemon, but the gate never builds WASM and nothing detects when a
python-worker-only method becomes reachable from the web client (web can't spawn the
Python worker). check.py currently runs RECORDED non-fatal on pre-existing reds.

## Scope

Extend scripts/gate.sh: (1) WASM build smoke via ./build.sh wasm (honest SKIP if emsdk
absent); (2) web parity probe — scripted scenario over hostd asserting no
python-worker-required method is reachable from the shipped web feature set, driven from
a new contracts/web_parity.md `web_unsupported` inventory so gaps fail loud; (3) fix or
explicitly scope the current check.py reds and make CHECK_PY_FATAL=1 the default.

## Definition of done

- [x] gate.sh runs WASM smoke + parity probe stages with SKIP-on-missing-env.
- [x] contracts/web_parity.md inventory exists and the probe enforces it.
- [x] check.py fatal by default with zero unscoped reds.
- [x] Full gate green; REFACTOR_LOG.md handoff appended.
