# T-G6 — questions / findings for the Fable advisor

## Finding 1 (non-blocking): systemic offscreen deadlock in ScenarioRunner step-machines

**Context.** T-G6 is verify-and-close: all of S1–S6 + H5 + G2 already landed on master via
the prior overhaul workflow. I changed no code. Building AI-CAD2 from master and running the
wave-2 verify scenarios, the shell chrome all renders correctly (see work/tasks/T-G6.md report).

**The finding.** Every worker-driven *step-machine* scenario deadlocks when run headless/
offscreen: `sketch`, `multidoc`, `m1`, `fea`, `cam`, `flow`, `drawing`, `asm`, `activity`,
`evalmass`. The step machine's mid-loop `st.docMirror.newPart(st.worker, err)` (a *second*/
in-tick worker part creation) never returns; the shot loop special-cases these scenarios to
ignore `--shot --frames N` and instead wait for `scenarioShotDone`, so it spins until the
outer `timeout` kills it (exit 124, no PNG). Worker **one-shot** scenarios (`tree`,
`equations`, `ecad`) capture fine — verified `tree` → exit 0 with a real engine-built
Base/Fillet/Shell feature tree. So the worker CAN build a part; the hang is specific to a
mid-frame-loop `newPart` re-entry.

**Impact on T-G6.** Blocks the *screenshot* verification of the `multidoc` (S2) and `sketch`
(H5) scenarios only. The S2 doc-tab band and H5 sketch-mode chrome + DOF solver both render
(proven via static scenarios: `ribbon`+`AI_CAD_TAB=1` shows the sketch ribbon + "Fully
Defined"). This is F4/C6 worker-seam territory (C6 REFACTOR_LOG already flagged "complex
scenarios use DocMirror/worker directly until C1–C5/H1–H4 panels land"), outside my
`ui/imgui/src/ui/**` shell-chrome write-set.

**Recommended action.** FILED as GitHub issue #7 (https://github.com/Hunter-124/CAD/issues/7).
Original recommendation below stands — advisor's call on assignee.

File a GitHub issue against the F4/C6 worker seam:
"ScenarioRunner step-machine scenarios deadlock offscreen on mid-loop
`DocMirror::newPart(worker)` — blocks --shot capture for sketch/multidoc/m1/fea/…".
Do NOT hold T-G6 on it (wave-2 shell chrome is complete; H5 wave-2 bar is compile + overlay
milestone, both met). I did not open the issue myself to avoid overlapping with F4/C6 owners —
advisor's call on assignee.

**Question:** agree this is out-of-scope for T-G6 and should be a separate F4/C6 issue, and
that the H5 wave-2 milestone (compiles + overlay path wired/live) is satisfied without the
blocked entity-overlay screenshot? (My read: yes.)
