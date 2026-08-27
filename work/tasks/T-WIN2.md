---
id: T-WIN2
title: Windows CI runner — build + smoke the host/ GUI on Hunter's Windows box
workstream: X
owner: unassigned
status: done
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

- [x] Runner registered and reachable (`ryzen-7900x3d`, online with
      `cc-win-package`)
- [x] MSVC build of core/ + host/ green on Windows (native-build run
      33032526086, Windows x86_64 job)
- [x] Release-only job launches the app and validates a real PNG headless
      capture through the in-process C++ fallback
- [x] The obsolete `AppState::tryStartWorker()` `#ifndef _WIN32` exclusion
      removed; Windows now follows the cross-platform C++ fallback
- [x] Result wired into the existing native-build step summary
- [x] T-WIN1 unblocked for owner `gissa`

## Progress log
- 2026-08-27: Existing `ryzen-7900x3d` CAD runner and
  `cc-win-package` routing confirmed. Added a release-only Windows GUI smoke
  that forces the managed worker failure, loads the just-built
  `libchudware.dll` through `cpp_core`, and validates a real PNG. Removed the
  obsolete Windows-only fallback exclusion in
  `AppState::tryStartWorker()`. Per Hunter's direction, Windows/macOS native
  jobs run only for `v*` release tags or an explicit manual dispatch; ordinary
  pushes do not claim those runners.
