# Supervisor handoff — Fable foreman session (2026-07-02)

For the incoming Fable supervisor. Read this + [FOREMAN.md](FOREMAN.md) (protocol,
pitfalls, discipline rules, full check-in ledger) before doing anything. Your memory
directory has `subagent-supervision-lessons` and `pillar16-flywheel-decisions` —
recall them.

## Your role

You are the FOREMAN, not a builder: dispatch drivers, enforce the FOREMAN protocol,
review branches to REVIEW verdicts, answer `work/questions/<id>.md` files (commit
`ANSWER (Fable):` blocks), keep the check-in ledger current, batch true human-only
decisions into FOREMAN's "For Erik" section. Spend your own tokens on decisions and
reviews; Sonnet drivers build (Opus for gnarly cross-language work). fable-arc (a
peer Fable/Opus CLI session) has co-supervisor standing: it reviews the WS-B/E lanes
and its own arc — its rulings have been sound; don't duplicate its reviews.

## State at handoff

**~21 branches at status: review awaiting Erik's merges** (statuses live on branches;
the master board shows `doing` until merge — always audit via
`git show origin/<branch>:work/tasks/<id>.md | grep status`). Recorded merge orders:
B1a→B1b→B2→B3→B4 · E1→E2→{E3,E4} · F1→F2 · F14→F15→F16 · G2→G4 · rest independent
(H1,H2,H3,H4,F7,F13,F3,F5,F9,C1,D1). Merge-time flags: T-F2 changes desktop FEA
material defaults (fable-arc flagged, Erik must eyeball); trivial `server.py`
append conflicts everywhere (keep all lines).

**In flight:** fable-arc on T-F16 (last of its arc). Nothing else runs; no orphans
(audited — see FOREMAN STUCK-AUDIT line).

**Blocked next wave — dispatch the moment merges land** (`python3 work/tools/work.py
ready` after each merge): T-F4, T-F8, T-H5, T-H7, T-H8, T-B5, T-E5, T-F17, T-F6,
T-F11 (gated), T-F12, T-H6, T-H9, T-H10, T-H11. Every one already carries a
"Driver notes (Fable)" section — pre-paid pointers; dispatch prompts should tell
drivers to trust them.

## Dispatch playbook (proven today)

Spawn general-purpose agents, model sonnet, run_in_background, one per task. The
prompt must include, verbatim in spirit: (1) agent name `sonnet-<syllable>`; (2) run
`python3 work/tools/work.py prompt T-XX` and follow the work order exactly incl.
claim; (3) read FOREMAN.md first, all its rules binding; (4) **WORK INLINE — subagents
one at a time, results collected in the same turn; NO watchers/fire-and-forget** (the
#1 Sonnet failure mode; expect to re-enforce it near the finish line anyway with a
literal closeout checklist: gate → graphify update → honest DoD → status: review →
push → FOREMAN check-in → final summary); (5) commit+push every ~20 min; (6) known
pre-existing failures (selMulti, check.py feature_surface_audit, pytest-OCP skips) —
note, never chase; if gate pytest skips, run manually via
`~/.local/share/AI-CAD/mcp-venv/bin/pytest` and record counts; (7) chained tasks
branch FROM their predecessor's branch with merge order recorded in the REVIEW
check-in; (8) escalations to work/questions/T-XX.md; never merge, never set done;
(9) final message = concise report to you.

## Open threads the successor inherits

1. **T-F16** — review it when fable-arc posts REVIEW (or finish/redispatch if arc
   dies on its Fable budget; its arc log is docs/sessions/PILLAR15_ARC_LOG.md).
2. **Post-merge alignment commits** (small, first driver in the area does them):
   req_join ↔ contracts/requirements.md constants + T-H3 importing
   services/verify/unit_ranges.RANGES (FOREMAN 2026-07-02 note); T-H4's
   groundedness module conforming to T-F10's GroundednessChecker protocol
   (fable-arc's interface ruling in work/questions/T-F10.md — harness owns intrinsic
   tagging, checker owns membership only).
3. **T-F17 inheritance:** GUI-side action emission needs an `agent_action` bridge
   RPC (GUI can't link core/agent — C-ABI only; work/questions/T-H1.md).
4. **Human-gated, batch for Erik:** DVC remote bucket + Argilla/MLflow hosting boxes
   (T-H5/T-H6); AI-CAD Cloud relay anything; model-promotion criteria (T-H9);
   T-G2's four NEEDS-LIVE click-tests → T-G12 checklist; E3-hooks retarget decision
   (T-G4 backlog note); issue #1 stale-branch cleanup after merges.
5. **T-G5–T-G9** (GUI migration waves): swarm-integrator tasks — each wants a
   dedicated session with the flock'd build, not a fleet slot.
6. **Stale staged libaicad.so** predates sim_run_static — next C++ task refreshes it.

## Strategic context (one paragraph)

Today built Pillar 16's foundation: docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md (21 model
insertion points, flywheel design, roadmap), docs/VLM_OFFLINE_TRACK.md (Qwen3-VL-4B
primary), workstream H cut and largely built (telemetry capture → data store →
fleet training → serving/canary), plus F/B/E waves. Ratified decisions: capture now
train later; multi-state own-machine fleet, job-level distribution only; team dogfood
corpus first; R1 editability parity (model outputs land as doc-ops/document-schema
artifacts, always GUI-editable). The long line: merge → next wave (H5/H7/H8, F4/F8) →
benches (H8) → frontier-logged features (elicitation, regen-fix, drawing gen) →
first custom models (T-F11 translator, then T-H11 regen-fix per §6 of the survey).
