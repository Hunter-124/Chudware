---
id: T-RF23
title: Interactive app exit hang — Chromium threads outlive CefShutdown (found in R0c)
workstream: RF
owner: unassigned
status: done
branch: task/T-RF23-cef-exit-hang
depends_on: []
lane: app-gui
kind: fix
priority: 2
---

## Context

Found while closing wave 0 (2026-07-22): after run_gui's teardown completes fully
(chat persisted, rpc/mesh/worker/simClient stopped, `ui::cef::shutdown()` called,
glfwTerminate done), the process does not exit — ~30 Chromium threads
(ThreadPoolForeg, Chrome_InProcGp, ANGLE-GC, NetworkService, CacheThread_Blo,
CrShutdownDetec) stay alive and static/AtExit destruction never finishes ("No valid
task runner, exiting ungracefully."). Diagnosed via /proc task dump + teardown
tracing (AI_CAD_DEBUG_SHUTDOWN=1). The --ui-screenshot smoke path now exits via
`_Exit(rc)` after run_gui returns (main.cpp) so the gate is unblocked — the
INTERACTIVE exit path (user closes the window) still hangs the same way.

## Scope

Root-cause the CEF shutdown on Linux: likely CefShutdown() semantics with the
external message pump (pump_if_due model in cef_app.cpp), browser refcount leaks
(pooled headless browsers — cef_headless.cpp, web panels), or a CefRefPtr alive at
CefShutdown. The CrShutdownDetec watchdog exists but never fires — check why.
Fix so a normal window close exits the process cleanly. If a proper fix is
infeasible, escalate with evidence before widening the _Exit scope.

## Definition of done

- [x] Interactive close exits the process (no lingering Billet + Chromium threads).
- [x] The _Exit escape on --ui-screenshot removed or justified in a comment.
- [ ] gate.sh green; REFACTOR_LOG handoff.
