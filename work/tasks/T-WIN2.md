---
id: T-WIN2
title: Windows CI runner — build + smoke the host/ GUI on Hunter's Windows box
workstream: X
owner: unassigned
status: todo
branch: task/T-WIN2-windows-ci-runner
depends_on: []
lane: platform-infra
kind: new
priority: 2
---

## Context

Hunter has a Windows machine on the local network already serving as a CI runner
for his cloudcastle project; replicate that setup here so Windows bring-up stops
needing manual hardware time. This unblocks T-WIN1 (Windows 10 full-app bring-up),
which is currently `status: blocked` on "needs a Windows machine running the
CURRENT host/ tree" — every prior Windows pass has been a manual, ad-hoc checkout
on that box.

## Definition of done

- [ ] Runner registered and reachable
- [ ] `./build.sh`-equivalent MSVC build of core/ + host/ green on Windows
- [ ] The app launches and produces a headless capture
- [ ] The `AppState::tryStartWorker()` `#ifndef _WIN32` gate at
      `host/src/core/app_state.cpp:580` assessed (it currently disables the
      in-process worker fallback on Windows and causes headless-capture hangs)
- [ ] Result wired into the existing CI reporting
- [ ] T-WIN1 unblocked or explicitly re-scoped

## Progress log
