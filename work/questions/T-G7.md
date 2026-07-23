# T-G7 — questions / findings for the Fable advisor

Context: T-G7 (wave-3 core panels C1–C6 + G3 gate) chained off task/T-G6-migration-wave2
(merge order T-G6 → T-G7). The C1–C6 code was already ported to master by the prior overhaul
workflow (REFACTOR_LOG §§ C1–C6), so this is mostly verify-and-close, with one real fix (below).

## Verified rendering (AI-CAD2, reused/rebuilt linux-app binary)
- **C1 feature tree** — `CHUDWARE_SHOT=tree` ✓ (real worker-built Part1/Origin/Base/Fillet/Shell)
- **C3 copilot/activity/console** — `CHUDWARE_SHOT=chat` ✓ (glass bubbles, action card, Chudware
  branding — not the old "Chudware Copilot", per AI hint)
- **C4 viewport HUD/tools** — `CHUDWARE_SHOT=pick` ✓
- **C5 asm/config/tables/equations** — `CHUDWARE_SHOT=equations` ✓ (+ `assembly`, `plan` render)
- one-shots `ecad` ✓
- **C2 property manager + 44 forms** — see Fix 1 (was broken; fixed in-lane).

## Fix 1 (in-lane, applied): pmform opened no PropertyManager form
`CHUDWARE_SHOT=pmform CHUDWARE_PMCMD=extrude` rendered the Model Tree, not the Extrude form
(status showed "Command: extrude"). Root cause: the pmform scenario called `st.dispatch(cmd)`,
which routes to `actionBus.submit()` and *executes* the command (diff.ok → status set, returns)
without ever setting `st.activeTool` or opening the PM. `drawInspector` is only called when
`activeTool` is non-empty (application.cpp:159), and `pmApplyShotEnv`/`openForCommand` live
*inside* `drawInspector` — so the whole PM-form path was dead for pmform. Fix: pmform now sets
`st.activeTool = cmd` (opens the data-driven form; `pmApplyShotEnv` then applies CHUDWARE_PMSET
presets). One-line change in `ui/imgui/src/ui/scenarios.cpp` (C6 write-set). Rebuilt + reverified.

## Question 1 (BLOCKING for the G3 gate DoD): step-machine scenarios blocked by issue #7
T-G7 DoD requires "**All 21 shot scenarios work on AI-CAD2**" (G3 gate = full screenshot
battery). But **10 of the 21 are ScenarioRunner step machines** (`m1`, `sketch`, `asm`, `fea`,
`cam`, `flow`, `drawing`, `multidoc`, `activity`, `evalmass`) that deadlock offscreen on the
mid-loop `DocMirror::newPart(worker)` — filed as **issue #7** during T-G6 (F4/C6 worker-seam
scope, out of the ui/imgui G-lane). Worker one-shots (`tree`/`equations`/`ecad`) and static-seeded
scenarios (`settings`/`ribbon`/`explorer`/`chat`/`plan`/`assembly`/`pick`/`pmform`) capture fine
(≈11/21). So T-G7's G3 gate **cannot** reach 21/21 until issue #7 is fixed, and #7 is not in my
lane to fix.

**Recommended answer (my read):** re-scope the T-G7 G3-gate DoD to "all **non-step-machine**
scenarios render + the step-machine battery is deferred to the issue-#7 fix (F4/C6)", and merge
T-G7 for the C1–C6 code + C2 pmform fix on that basis. Alternative: hold T-G7 at review until
issue #7 lands, then run the full 21-scenario battery in one pass. I lean re-scope (the C-wave
panel *code* is verifiable and correct; the blocker is purely the worker-seam capture harness).

**Which do you want?** Proceeding meanwhile with everything unblocked (C2 fix verified, C1/C3/C4/C5
documented).
