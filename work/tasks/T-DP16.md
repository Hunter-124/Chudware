---
id: T-DP16
title: AI-Agent workspace — full-screen comprehensive copilot (DAG + worker steering + session mgmt)
workstream: DP
owner: unassigned
status: in_progress
branch: task/T-DP16-ai-agent-workspace
depends_on: [T-DP15, T-DP17]
lane: app-gui
kind: new
priority: 2
---

## Context

Presentation layer of the guided design pipeline — see
[docs/AGENT_DESIGN_PIPELINE.md](../../docs/AGENT_DESIGN_PIPELINE.md) §9 (Hunter directive,
2026-07-05). MANDATORY: graphify to orient before editing; REUSE the existing AI-tab
machinery. Design language LOCKED: pinned glass columns, no docked layout, dark-mode
contrast per the T-U1 kit, ui_style_check clean. Verify HEADLESS only
(xvfb-run CHUDWARE_SHOT=...), never launch headed.

**Card corrected 2026-08-03 — the original framing was stale.** It said "ui/imgui is the
ONLY live GUI (host/ reference-only)". That was reverted: `ui/` now holds only `viewport/`,
there is no `ui/imgui`, and `build.sh` is authoritative — "the legacy `ui/imgui` /
`ui/abstract` shells are retired; host/ is the single cross-platform GUI target." The
workspace ALSO already largely ships. Verified surface (2026-08-03):

- `host/src/ui/ai/` is ~5.3k lines: `ai_workspace.cpp` (1855), `docs_panel.cpp` (969),
  `plan_board.cpp` (794), `rules_authoring.cpp` (653), `active_work.cpp` (411),
  `metrics_view.cpp` (277), `ai_prefs.cpp` (102).
- `drawAiWorkspace` = rail | chat column | DAG pane; `drawNavColumn`/`drawViewPane` carry
  eight views: `0 Agent Hub · 1 Session Plan · 2 Active Work · 3 Documents · 4 Sessions ·
  5 Leaderboard · 6 Metrics · 7 Options`.
- Sub-agent control plane already wired: `subagent_list` / `subagent_transcript` /
  `subagent_steer` / `subagent_terminate` (`services/agent/subagent_rpc.py`), consumed at
  the sub-agent turn boundary in `agent_turn.py:2039-2067`.

So DoD boxes 1 and 4 already ship, and box 2 is half-done (redirect + kill only).

## Scope

Doc §9.2. Close the genuine remainder, not the parts that already shipped.

**Gap 1 — worker steering is missing pause/resume, live model/effort change, and spawn
approve/deny.** The registry (`services/agent/subagent_registry.py`) has no `paused`
state, no runtime override, and no approval decision. Worse, `routing.yaml`
`approval_policy.deeper: overseer` means every depth>1 delegation is registered
`status: pending` and **nothing in the tree ever calls `DelegationTree.approve()`** — the
`delegate()` docstring tells the caller to "proceed with sibling work", so deep
delegations are permanently stranded with no human path to release them. Human
approve/deny is the fix.

**Gap 2 — session management has switch/resume but no fork.** `copilot_internal.h` exposes
`newConversation` / `switchConversation` / `closeConversation` / `archiveConversation`;
there is no `forkConversation`.

Enforcement point for every new control is the SAME turn boundary that already drains
steers and honours terminate — the registry is the cross-process control plane, and
`run_turn` is the single place it is obeyed. Nothing is a hard kill; everything lands at a
turn boundary and says so in the transcript.

## Definition of done

- [x] AI-Agent workspace renders the DAG centerpiece + steering rail + session list.
      (Ships: `drawAgentHubView`, `drawNavColumn`, view 4 `chatDrawSessionsList`.)
- [ ] Worker steering actions wired to the delegation/mission control seams:
      - [x] redirect (`subagent_steer`) and kill (`subagent_terminate`)
      - [ ] pause / resume
      - [ ] live model + effort change
      - [ ] pending-spawn approve / deny (releases the stranded depth>1 delegations)
- [ ] Session management: switch/fork/resume works against real sessions.
      (switch/resume ship; `forkConversation` is the remainder.)
- [x] Overlay copilot unchanged in modeling; comprehensive view only in this workspace.
- [ ] Shot scenarios (populated mission, empty state) render without crashing;
      ui_style_check clean; `python3 scripts/check.py` PASS. Visual judgment is Hunter's.
