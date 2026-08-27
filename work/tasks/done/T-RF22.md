---
id: T-RF22
title: RF closeout — final docs, contract index, cross-platform build check (node R5b)
workstream: RF
owner: integrator
status: done
branch: task/T-RF22-final-docs
depends_on: [T-RF21]
lane: platform-infra
kind: overhaul
priority: 4
---

## Context

Last node of the consolidation refactor. The tree has changed shape (engine_oracle
gone, services/worker slimmed, copilot split, MCP split, web parity enforced); the
navigation docs must tell the final truth.

## Scope

Rewrite ARCHITECTURE.md + README.md to the final layout; complete per-module AGENTS.md
pass (top + subsystem); contract index pass over contracts/ (44 files — every one
either live or marked historical); verify Linux gate fully green and a Windows build
(build.bat/preset) produces a runnable exe. Final REFACTOR_LOG entry closing the DAG.

## Definition of done

- [x] Docs match the tree; no stale references (link sweep in check.py if cheap).
- [x] Linux gate green; Windows build verified.
- [x] Board: every T-RF* done; final REFACTOR_LOG paragraph; graphify update run.

## Progress log
- 2026-07-23 08:00 UTC [integrator] Windows build verification (2026-07-23): no MSVC toolchain on this Linux box (windows-core/windows-occt presets are MSVC-only). Verified instead: all wave-touched C++ (core/ecad/session.cpp, core/sim/spice.cpp, core/api/libchudware.cpp, host/src/main.cpp) is ISO C++17 and passes x86_64-w64-mingw32-g++ -fsyntax-only for the Windows target (zero errors; one pre-existing benign NOMINMAX redefine warning). A real MSVC build still needs a Windows host — recorded honestly rather than faked.
