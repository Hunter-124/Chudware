---
id: T-RF13
title: RF wave-3 WASM adaptive-quality ladder (node R3a)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF13-wasm-quality-ladder
depends_on: [T-RF10, T-RF11, T-RF12]
lane: app-gui
kind: new
priority: 3
---

## Context

PERF_AUDIT_2026-07-16.md: web/WASM viewport is GPU-fill-bound on weak iGPUs (23–25 FPS
on UHD 620, p50 33.3 ms vsync cliff). Suspects: full-res 4× MSAA + glBlitFramebuffer
resolve (host/src/renderer.cpp:581), liquid-glass panel blur, Kawase blur
(renderer.cpp:2231). The adaptive-quality ladder was diagnosed but never built. WS RPC
latency was ruled out (p50 0.34 ms).

## Scope

Implement dynamic quality: render-scale + MSAA 4×→2×→off + blur-quality steps, driven by
measured frame time (target 60 FPS, floor 30). WASM defaults to adaptive; native desktop
defaults to full quality (no behavior change). Small HUD quality indicator in
viewport_panel.cpp. Validate with Webservice/gateway/tests/perf_probe.py + browser FPS
beacons; update Webservice/perf/baseline.json.

## Definition of done

- [ ] WASM build holds ≥30 FPS on a weak-iGPU scenario without manual settings.
- [x] Native desktop output pixel-identical at default quality.
- [ ] perf_probe + client beacons updated; gate.sh green; REFACTOR_LOG handoff.
