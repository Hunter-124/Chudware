# host/ GUI Polish Tracker

Canonical checklist of every GUI change Hunter has requested for the host/ light-ribbon GUI.
**Hunter verifies each item live** (rebuild → red-pen); agents build-green + commit + ONE sanity shot max (no screenshot-iteration loops — see memory `hunter-verifies-ui`).

## How we work
1. Hunter rebuilds (`ninja -C host/build-linux Chudware`) and tests against this list.
2. Hunter reports what's still wrong / new asks → logged here as new rows.
3. Orchestrator dispatches a focused sub-agent per cluster; agent commits, updates its row.
4. Never merge/close a row as VERIFIED until Hunter confirms it live.

## Status legend
- ✅ COMMITTED — in master, awaiting Hunter's live verify
- ☑ VERIFIED — Hunter confirmed good (done)
- 🔧 IN FLIGHT — an agent is working it now
- ⬜ PENDING — not started / opt-in awaiting Hunter's go
- 🐛 REOPENED — Hunter found it still broken after a commit

## Verification log
- **2026-07-07** — Hunter live-verified the full checklist (rows A–J, H1, F1, L1): "gui stuff all looks good." All ✅ rows below are considered ☑ VERIFIED as of this pass unless later reopened.

## Archive
Resolved sections through 2026-08 are in [`archive/gui-polish-tracker-2026-08.md`](archive/gui-polish-tracker-2026-08.md).

---

## Open / next-up queue (orchestrator dispatches on Hunter's go)
- _(new rows land here as Hunter red-pens the list above)_
