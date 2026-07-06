# Autopilot — piled questions for gissa

opus-sweep/opus-wynn is running in autonomous mode (2026-07-06): grabbing + completing the
next ready tasks back-to-back. Questions I'd normally ask are collected here instead of
blocking — I proceed with the best-judgment default noted, and gissa can correct async.
Genuine contract/architecture blockers get their own `work/questions/T-XX.md` escalation.

Format: `- [ ] (TASK) question — MY DEFAULT: <what I did / assumed>`

## Open
- [ ] (T-X7 item 4) Browser-session prices into the API-only `bom_price` "no-scraping" seam?
      Full escalation in work/questions/T-X7.md Q1. DEFAULT: left unwired, item 3 exposes
      prices on the catalog record instead; awaiting ruling.
- [ ] (T-BL36) Keep the ~3px feature-family category color strip, or remove it too as "the
      left bar"? MY DEFAULT: kept it (distinct semantic from selection); animated border is
      the selection cue. 1-line follow-up if you want it gone.

- [ ] (T-G11) Deleting host/ (46k LOC reference tree) is EXPLICITLY human-gated — the task
      requires a *human*-verified Windows build recorded in the task, and says "an agent must
      NOT delete host/ on Linux green alone." I've agent-verified AI-CAD2 builds+runs on
      Windows (T-G25) and am verifying the windows-app Ninja preset now. MY DEFAULT: I will
      NOT delete host/ autonomously — I'm doing the safe X1 prep/verification only and leaving
      the deletion + human sign-off for you. When you're ready: build ShopCraft on Windows,
      smoke-test (open part/sketch/extrude/save), record your name+date in work/tasks/T-G11.md,
      then the host/ deletion can proceed. Want me to do the deletion once you've recorded the
      human verify, or keep it fully human-owned?

## Resolved
(none yet)
