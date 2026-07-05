---
id: T-WIN1
title: Windows 10 full build bring-up - core + imgui GUI compiling, running, and shot-verified under MSVC
workstream: WIN
owner: unassigned
status: todo
branch: task/T-WIN1-win10-bringup
depends_on: []
lane: platform-infra
kind: fix
priority: 2
---

## Context

Windows parity track (new contributor: gissa, Windows 10, Opus driver). Recent
master commits added a windows-core CMake preset + docs/BUILD_WINDOWS.md and made
the OCCT-free core + ui/abstract compile under MSVC — the FULL app (OCCT core +
ui/imgui GUI) has not been brought up on a real Windows 10 box. Known landmine:
an MSVC fix_math.h /FI (force-include) wiring bug previously stalled Windows GUI
builds — check how fix_math.h is injected before trusting stale errors. Do NOT
touch services/agent/, services/browser/, or work/tasks/T-W*/T-X* — those belong
to two active parallel sessions; pull master frequently (they merge often).

## Steps

1. Read docs/BUILD_WINDOWS.md; install prerequisites (VS Build Tools/MSVC, CMake,
   Ninja); build the windows-core preset first, then attempt full core (OCCT) +
   ui/imgui. Fix breakages in platform seams only; guard fixes so Linux stays
   green (do not edit Linux-specific build files).
2. Get the app launching and a part rendering on Windows 10.
3. Run the AI_CAD_SHOT headless capture battery (all scenarios capture headless
   per the resolved shot-verify track); eyeball the PNGs honestly and write a
   parity report (what matches Linux, what doesn't) into docs/BUILD_WINDOWS.md.
4. Keep a running "Windows gotchas" section in that doc — you are writing the
   playbook for every future Windows contributor.

## Definition of done

- [ ] Full app builds + launches on Windows 10; steps reproducible from the doc
- [ ] Shot battery captures on Windows; parity report committed
- [ ] Linux untouched/green (CI or a note from the lead confirms)
