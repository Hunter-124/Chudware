# host/ GUI Polish Tracker

Canonical checklist of every GUI change Hunter has requested for the host/ light-ribbon GUI.
**Hunter verifies each item live** (rebuild → red-pen); agents build-green + commit + ONE sanity shot max (no screenshot-iteration loops — see memory `hunter-verifies-ui`).

## How we work
1. Hunter rebuilds (`ninja -C host/build-linux AI-CAD`) and tests against this list.
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

---

## A. Ribbon / top toolbar
| # | Item | Status | Commit |
|---|------|--------|--------|
| A1 | Dropdown-menu buttons: icon horizontally centered on full button width (match plain buttons) | ✅ | `5e2835d7` |
| A2 | Dropdown arrow moved UNDER the icon toward bottom edge, proper padding (not a side column) | ✅ | `5e2835d7` |
| A3 | All button backgrounds/outlines get translucent gradient "liquid glass" resting effect (both themes) | ✅ | `5e2835d7` |

## B. Title bar
| # | Item | Status | Commit |
|---|------|--------|--------|
| B1 | All 11 chips (File, Search, Settings, Model, Drawing, Simulate, Electronics, Manufacture, PLM, Parts Library, AI) get generated icons shown WITH their text | ✅ | `afb9ad18` |

## C. Feature Manager header chip
| # | Item | Status | Commit |
|---|------|--------|--------|
| C1 | "Feature Manager" text centered in its background | ✅ | `55976c2e`/`c363bf81` |
| C2 | Icon fits inside the background (no overflow) | ✅ | same |
| C3 | Replace generic three-dot glyph with a meaningful feature-tree glyph, centered | ✅ (needs rebuild — code on master is correct) | same |
| C4 | Use that same feature-tree glyph for the Feature Manager **rail tab** icon (was the atlas "history" clock); Explorer/Configurations tab icons unchanged | ✅ | `558f59ca` |

## D. Feature tree rows
| # | Item | Status | Commit |
|---|------|--------|--------|
| D1 | Part1/Equations/Material/Origin/Plane/Feature rows vertically centered (were too high) | ✅ | `55976c2e` |
| D2 | Eye show/hide icons vertically centered | ✅ | `55976c2e` |
| D3 | "(unsaved)" filename box padding fixed + left text no longer clips | ✅ | `55976c2e` |
| D4 | Origin row icon no longer clips on the left | ✅ | `55976c2e` |
| D5 | All rows FM-standard styling | ✅ | `55976c2e` |

## E. Doc-tab strip
| # | Item | Status | Commit |
|---|------|--------|--------|
| E1 | "Part1" filename display centered in its background box | ✅ | `e5e90805` |
| E2 | "create file" / "+" button/icon centered in its box | ✅ | `e5e90805` |

## F. 3D origin display (viewport)
| # | Item | Status | Commit |
|---|------|--------|--------|
| F1 | Color-coded axes (X=red / Y=green / Z=blue), arrowheads, clear +/- X/Y/Z labels; corner orientation triad left untouched | ✅ | `620d4fea` |

## G. AI workspace
| # | Item | Status | Commit |
|---|------|--------|--------|
| G1 | Expanded Copilot chat | ✅ | `e940447d` |
| G2 | Expanded DAG view of current project (Project Plan tab) | ✅ | `e940447d` |
| G3 | View all sub-agents / sessions (sessions real; sub-agent live enumeration is honest empty-state — no worker fleet-listing method yet) | ✅ (partial, honest) | `e940447d` |
| G4 | Workflow configuration settings | ✅ | `e940447d` |
| G5 | Model assignment settings (editable, persisted to shared config) | ✅ | `e940447d` |

## H. PLM + Parts Library
| # | Item | Status | Commit |
|---|------|--------|--------|
| H1 | Start expanded, fill the screen, fit into place | ✅ | `96fcb167` |

## I. Manufacture workspace
| # | Item | Status | Commit |
|---|------|--------|--------|
| I1 | Own CAM tree (not the model FeatureManager); model tree no longer leaks on overlay (Copilot stays) | ✅ | `c363bf81` |
| I2 | CAM ribbon: New Job / Extract Part / Add Setup / Add Tool → Face/Contour/Pocket/Adaptive/Drill/3D Parallel/Waterline/Turn/Laser → Generate/Simulate/Show in 3D/gcode-flavor dropdown/Post | ✅ | `c363bf81` |
| I3 | CAM tree shows Job/Stock/Machining/Operation setup + jobs/tools + 3D models used for toolpaths | ✅ | `c363bf81` |

## J. Simulation workspace
| # | Item | Status | Commit |
|---|------|--------|--------|
| J1 | Each sim type (Static/Modal/Flow/Thermal/Fatigue/Motion/EM) gets its OWN ribbon tab with only its tools | ✅ | `3e848191` |
| J2 | Material/Fixtures/Loads/Mesh setup panels open ONLY when the ribbon tool is invoked (PropertyManager-style), not always-open accordions | ✅ | `4181b453` |
| J3 | Sim tree must NOT leak into Model/Drawing views | ✅ | `4181b453` |
| J4 | Sim tree gets FM-standard styling (glass pills, selection cards, dividers, status badges) | ✅ | `4181b453` |

## K. Electromagnetic simulation
| # | Item | Status | Commit |
|---|------|--------|--------|
| K1 | Full-wave EM backend: eigenmode/modal, freq-domain driven S-params, time-domain; FDTD/FEM/MoM; openEMS-primary pluggable adapters + others | ✅ | `025b0865` |
| K2 | Wire EM study setup forms into the Simulate EM tab (eigenmode/driven/time-domain) — backend is live+validated, tab currently only toasts | ✅ | `34964e5a`+`aac390c3` |

## L. Stability
| # | Item | Status | Commit |
|---|------|--------|--------|
| L1 | Freeze when leaving Electronics/Manufacture (down local-LLM probe saturated worker mutex) | ✅ | `50a54308` |
| L2 | Remaining crash/freeze on rapid Model→Drawing→Simulate→Electronics→Manufacture switching (Electronics per-frame ECAD focus-grab ate every click outside its window) | ✅ | `9178c8f6` |

## M. AI workspace v2 redesign (2026-07-07 — screenshot-specified, major rework)
Hunter's red-pen on the shipped AI hub: make it compact, transparency-first, DAG-centric. Screenshot marks regions: **DAG** upper-center, **conversation** lower-left (green box), **workflow settings** lower-right ("Settings/Options/Defaults"), inference settings **wiped**. Full plan: `host/AI_WORKSPACE_V2_PLAN.md` (`1761eafb`).

**Locked decisions (Hunter, 2026-07-07):**
- **Subagent steering = FULL** (M11/M15–M17): build the live subagent-session registry + steering RPCs up front — inject-into-running-subagent works, not read-only.
- **Thoughts (M13) = live token streaming** for normal completions; thoughts streamed live into a **collapsible textbox** in the chat when the provider exposes transparent thought traces; providers that only return summaries may land per-block on arrival.
- (orchestrator-decided) Schema **`agent_events/1`→`/2`**; **plain-language `target`** strings on tool_call (not hashed); **new GUI event-graph store** (not the ActivityPanel list); **one DAG per chat session** aggregating its turn run_ids.

**Build waves:** W1 (parallel, non-colliding) = Phase 0 backend event/stream instrumentation + Phase 1 GUI layout refactor. W2 (after W1) = Phase 2 DAG, Phase 3 transparency, Phase 4-bk subagent registry/steering, Phase 5 chat UX. W3 = Phase 4-gui subagent open/steer.
### Layout / chrome
| # | Item | Status | Commit |
|---|------|--------|--------|
| M1 | Make the AI tab more compact overall | ✅ | `500340e9` |
| M2 | WIPE the inference-settings block from the AI panel (Model, Reasoning effort, Max response tokens, Model context window, API key, Privacy, Vision reviewer) — those live ONLY on the main Settings page | ✅ | `500340e9` — parity was ALREADY complete on the main Settings page (Settings→AI `drawProviderModelPicker` covers provider/model/effort/max-tokens/context/API-key/base-URL/vision/Privacy-Offline; Settings→Experimental has the separate vision reviewer). No additions to `settings_modal.cpp` needed; no parallel store. |
| M3 | AI panel keeps ONLY AI *workflow* settings (Supervised/Autopilot, refine passes, max output tokens, context window), relocated to lower-right ("Settings/Options/Defaults" zone) | ✅ | `500340e9` |
| M4 | DAG view occupies the upper main area, above the workflow settings | ✅ | `500340e9` |
| M5 | Conversation window (message log + composer) moves to lower-left (green box); composer laid out per red box: `Permissions ▾` row → `[Model picker ▾][Effort ▾][ctx display]` → `Design/Edit/Ask/Permissions` mode tabs → input | ✅ (partial) | `500340e9` — window relocated lower-left + always visible; reuses shared `chatDrawComposer` (all controls present: Design/Edit/Ask + Permissions + model picker + Thinking/effort + ctx meter + input). EXACT top-to-bottom red-box ordering deferred: composer atoms are file-private statics in `legacy_chat.cpp`; reordering them would restructure the shared docked-Copilot composer (out of scope per the ownership rule). |
| M6 | Remove the "Conversation" tab; conversation window is always visible (not a tab) | ✅ | `500340e9` |
| M7 | Tab switcher becomes **Agent Workflow** \| **Session Plan** (switches the DAG pane only) | ✅ | `500340e9` — both tabs render the buildgraph view in Phase 1; the live agent-execution DAG lands under Agent Workflow in Phase 2. |
| M8 | Rename "Project Plan" → "Session Plan"; scope it per chat session (tied to each session) | ✅ (partial) | `500340e9` — renamed + per-session selection plumbing wired (`s_ai.selSession` ← active conversation id). Per-session run FILTER deferred: `Conversation` has no `run_id` yet (PLAN §2.1), so no session→run index exists — TODO left in `drawDagView` for Phase 2. |
| M21 | **Layout rework v2 (Hunter red-pen 2026-07-07 screenshot — Phase 1 layout "did not go as planned").** Target geometry: **chat = narrow full-height left column** (green box; sits right of the Sessions sidebar, compressed — small portion of window width, its right bound moved LEFT); **DAG pane = upper-right** (purple box; its LEFT bound moves right to meet the chat's right edge); **Options/workflow-settings = lower-right, below the DAG and right of the chat** (yellow box; re-flow the settings content sensibly for that size). Remove the stray "Conversation" text label. Add **visible dividers** between the three panels (subtle separator lines, NOT purple). | ✅ | `814b1d75` (+ the bulk of the layout landed inside `c7efe357`, which committed the shared working tree mid-edit — see 814b1d75's message). Chat = clamp(30% body, 300–440·S) full height; DAG upper-right; options lower-right (34% height, 150–260·S) re-flowed into two side-by-side groups (Workflow radios \| Limits knobs) with the footnote spanning below, single stack when narrow; glassBorder 1px hairlines on both boundaries; "Conversation" header deleted; DAG empty-state sub-line now wrap-centers in the narrower pane. |
### DAG (the centerpiece)
| # | Item | Status | Commit |
|---|------|--------|--------|
| M9 | **Agent Workflow DAG** = live agent-execution DAG for the selected session: nodes for tool calls (+ what they act on), context injections, subagent/worker/specialist branches off the main branch, advisor/council chats, interruptions/special helpers; updates live as execution happens | ✅ (partial) | `cedad5fd` — new GUI-side event store (`s_agdag` in `ai_workspace.cpp`, `ui::feedAgentDagEvent`) fed from the `app.cpp` `JobState.events` GL-thread drain; tree built by joining `parent_id`→`id` (`/2` §2.1). **Live node kinds** (render as they stream): `agent.tool_call` (tool + human `target` + `latency_ms` + `ok`), `agent.thought` (completed block), `agent.context_injected` (source + label + tokens). **Emit-wired, EMPTY until a mission delegates** (node kinds exist in code, light up on first real event, nothing fabricated): `agent.spawn` now HAS a production sink (Phase 4, `b3d1e923` — mission path threads `DelegationTree(on_event=…)`; spawn nodes are click-to-open, M11) but fires only when a mission agent calls `delegate`; `agent.advisor` remains sink-less (T-W23 unmerged) — the header keeps the honest "no sub-agent / advisor branches yet" until events arrive. No run ⇒ clean empty-state (screenshot-verified via `AI_CAD_SHOT=aiws`). `agent.thought.delta` is provider-gated (non-streaming seam) — completed thought shown on arrival. **Scope:** aggregates the currently-drained run (`Conversation` still has no `run_id`, M8) — not yet per-session-across-turns. NOT touched: `AgentEvent`/`jobs.cpp` (the `/2` record already arrives as JSON in `AgentEvent.text`; consumer parses it). **Not yet observed live** — consumed on-contract; awaits a real model run emitting `orch_agent.*` through this transport. |
| M10 | Collapsible/scalable DAG nodes — expand/collapse tool calls & sub-parts for easier tracking | ✅ (impl; interaction unverified on a populated tree) | `cedad5fd` + toggle/UAF fix — dedicated `drawAgentDagCanvas` (own tree layout, separate from the buildgraph canvas) hides collapsed subtrees BEFORE layout: per-node `[-]`/`[+]` toggle (fires on a tap = mouse-RELEASE with near-zero drag, since the full-canvas nav button claims the press — a drag pans instead), pre-order indented rows, pan/zoom scaffolding shared with the Session Plan camera; scalable via wheel-zoom. **Collapse/scale/UAF-safe-spawn all implemented; the toggle click could only be exercised on the empty-state screenshot — clicking a populated node needs a live `agent.*` run, so the interaction is not yet visually confirmed on a real tree (Hunter red-pens live).** |
| M11 | Click a subagent node (including active) → open/steer that subagent's chat in the main conversation window | ✅ (FULL steer, per the locked decision) | `b3d1e923` (backend) + `9a18af77` (GUI). **Backend:** file-backed cross-process sub-agent session registry (`services/agent/subagent_registry.py`, `~/.local/share/AI-CAD/cache/subagents/`: meta + transcript.jsonl + steer-queue.jsonl) auto-populated by every `DelegationTree.request_delegation` (status transitions mirrored); worker RPCs `subagent_list`/`subagent_transcript`/`subagent_steer`/`subagent_terminate` (`subagent_rpc.py`, registered in `engine_oracle/worker.py`). **Injection semantics (honest):** `subagent_steer` QUEUES; `agent_turn.run_turn(subagent_id=…)` drains the queue **before each model completion** — the steer lands as a user message at the sub-agent's next turn boundary, cross-process (GUI writes the queue file, the runner reads it mid-turn; proven by test). Sub-agents EXECUTE via `MissionDispatch.delegate` → one bounded synchronous `agent_turn` when a provider is bound (org-layer mission path threads tree+provider from `loop.run_mission`); without a provider the node returns `executed:false` with an explicit note. **GUI:** Sub-agents sidebar fed by async `subagent_list` (4s poll, never sync in draw); clicking a sidebar row OR a spawn/subagent DAG node (incl. running) swaps the sub-agent chat into the conversation column; composer sends `subagent_steer`. **Exercisable live:** end-to-end registry→RPC→GUI verified in the aiws screenshot (test-spawned sessions listed + openable). **Gated:** production sub-agents only exist when a mission agent actually calls `delegate` (org layer, default ON) — no model has been observed doing so in a real run yet; 12 offline tests prove the full spawn→run→steer→terminate loop with a mock provider. |
### Chat transparency (troubleshoot the harness/loops)
| # | Item | Status | Commit |
|---|------|--------|--------|
| M12 | Show context-injection events inline in the chat | ✅ (renders live once the backend emits) | `3910ab6a` — `agent.context_injected` → compact dim inline chip (bookmark icon + "Context · <source>: <label> (~N tok)"), appended at arrival position in the running turn's timeline (new `ActivityItem.rowKind=1`, `feedTurnActivity` in `legacy_chat.cpp`; shared renderer, so the docked Copilot shows it too). Fed by the EXISTING app.cpp `orch_*` drain — no new drain path. **Interleaving:** events anchor to the in-flight assistant turn's activity timeline (arrival-ordered among stage rows), not inside the prose bubble — a per-turn transparency flow, per the plan's fallback. Backend emits this kind on the chat path (M20), but **not yet observed live in the GUI** — verified against the /2 contract; empty-state screenshot confirms no-events ⇒ chat unchanged, nothing fabricated. |
| M13 | Show the model's streamed thoughts (when streamed) | ✅ (per-block live; delta streaming coded-but-gated) | `3910ab6a` — `agent.thought` → collapsible thought textbox in the chat (`rowKind=2`): collapsed by default with a one-line ellipsized preview, click to expand; honors the show-thinking toggle. **HONEST split:** the completed-block path is what can run today (provider seam is non-streaming — contract §3.2 `agent.thought.delta` has NO producer); the **delta accumulator is fully coded** (deltas coalesce per parent span into one growing box that stays auto-open while streaming, finalized + auto-collapsed by the matching completed `agent.thought`, also closed by `run_finished`/`mission_finished`) and lights up the moment a streaming provider lands — it just cannot fire yet, so only the per-block half is exercisable. Neither half observed with a live model run yet. |
| M14 | Show tool calls inline: which tool, what it acts on, and all program parts the AI touches (max transparency) | ✅ (renders live once the backend emits) | `3910ab6a` — `agent.tool_call` → inline glass row per call (`rowKind=3`): ok/fail status glyph, wrench icon, tool name, latency ("0.4s" / red "failed" + red border), and the human-readable `target` ("what it acts on", /2 field) as a dim line beneath. Expandable `(details)` drawer is wired but only activates when a detail body exists — the /2 record carries digests only today, so no drawer content is fabricated. Tool latency / ctx token estimates are excluded from the turn's usage-footer sums; typed rows don't trip the stage-thinking auto-collapse. `agent.tool_call` is the M20-proven live kind on the DAG/mission path — **rendered on-contract, not yet eyeballed with a real run** (Hunter red-pens live). |
### Subagent chat viewing / steering
| # | Item | Status | Commit |
|---|------|--------|--------|
| M15 | Viewing a subagent chat recolors the whole conversation (clear "you're in a subagent" indication) | ✅ | `9a18af77` — the WHOLE conversation column re-tints (warm statusWarn wash + 2 px border) and a solid "SUB-AGENT · role · status" banner strip heads the panel; the transcript child gets its own tint. Unmistakable vs. the neutral main chat; theme palette only (no hardcoded colors). Docked Copilot untouched (`legacy_chat.cpp` zero edits). |
| M16 | Lock the user from easily ending a subagent chat | ✅ | `9a18af77` + `b3d1e923` — NO close/end control in the sub-agent view; the only exit is the explicit "Back to main chat" button (tooltip states the sub-agent keeps running). Terminate is a deliberately small dim "Terminate…" → confirm modal → RPC, AND the `subagent_terminate` RPC independently refuses without `confirm=true` (double gate). Termination is honored at the sub-agent's next turn boundary (clean stop, never a hard kill) — semantics stated in the tooltip + modal. |
| M17 | Model / effort / starting-prompt are read-only when viewing a subagent chat | ✅ | `9a18af77` — model / token-budget (effort) / starting brief render as read-only info rows from registry data ("inherited from parent mission (read-only)" when unset); no editable controls exist in the view. The composer is steering-only (`subagent_steer`) and says so ("applied at the sub-agent's next turn boundary", live pending-steer count from `subagent_transcript`). |
### Chat UX
| # | Item | Status | Commit |
|---|------|--------|--------|
| M18 | Prompt box auto-grows upward (with a max cap) as the user types a long prompt | ⬜ | — |
| M19 | Endless scroll-back with lazy-loading of older messages (no crash on large history) | ⬜ | — |
| M22 | **Composer restyle v2 (Hunter mockup 2026-07-07 — supersedes/refines the M5 red-box ordering).** Layout (his sketch; layout is binding, style/colors free): row 1 = `Permissions ▾` alone on top; row 2 = the large prompt input box; bottom edge inside/below the box = `[AI Model Picker ▾] [Effort ▾] [📎 paperclip]` left-aligned, **send button at the right edge** (his green arrow). **Send swaps to a RED SQUARE STOP button while a turn is in progress** (stop = cancel the in-flight turn). | ⬜ (queued, Phase 5) | — |
| M23 | **File attachments in the composer:** the paperclip (where his red circle is) opens the native file-explorer dialog to attach files; attached files display as **chips above the chat input (Claude Code style)** — name + remove ✕; attachments ride the next send. | ⬜ (queued, Phase 5) | — |
### Backend / instrumentation (load-bearing — feeds M9/M12/M13/M14)
| # | Item | Status | Commit |
|---|------|--------|--------|
| M20 | Agent-execution event stream: harness/orchestrator emits structured, timestamped, parent-linked events (tool call start/end + target, context injection, subagent spawn/branch, advisor/council call) that feed the Agent Workflow DAG + transparency chat live. **Phase 0 backend DONE** — contract bumped `agent_events/1`→`/2` (§2.1 parent-linkage `id`/`parent_id`/`actor`; new `agent.context_injected`/`agent.spawn`/`agent.advisor` + human `target` on `agent.tool_call`; forward-compat `agent.thought.delta`). Emit helper `services/agent/agent_events.py` (19 tests). **Proven live+persist:** `agent.tool_call` (target+latency) through executor→node_runner→agent_turn on the DAG/mission path (journal-wrapped `emit`, no double-write). **Chat-path live:** `agent.thought` (completed block — provider seam is non-streaming) + `agent.context_injected` in `loop.run_turn`. **`agent.spawn` UN-GATED (`b3d1e923`, Phase 4):** `loop.run_mission` now constructs the mission's `DelegationTree(on_event=…)` (org layer, default ON since T-W22) and threads it through `node_runner.make_default_agent_fn` → `MissionDispatch` — a mission agent calling `delegate` streams `agent.spawn` live to the GUI drain + persists it (tree-owned journal) + registers a steerable session (M11). Honest residual: fires only when a model actually calls `delegate` in a mission — proven by offline tests, not yet observed in a live model run. **Still GATED:** `agent.advisor` (`run_ladder` + `council_cb` double-gated, T-W23 unmerged), `agent.thought.delta` (provider-gated). GUI drain (`AgentEvent`/jobs.cpp `/2` fields) = WIRE REQUEST in host/WIRE_REQUESTS.md (Phase 2/3 owns). | 🔧 | `c4c9bb5d` |

## N. Ribbon variant-button sizing (2026-07-07 — Hunter red-pen)
Split/dropdown ribbon buttons whose label changes with the selected variant don't recompute width for the new label → text clips (too narrow) or bleeds into the neighbor (too wide), making menu scaling look broken. Fix: size each variant button to fit its **widest** variant label (stable width, never clips/bleeds, no neighbor reflow). **Fix EVERY instance.**
| # | Item | Status | Commit |
|---|------|--------|--------|
| N1 | 3-Pt Arc button too narrow — text doesn't fit; widen to fit | ✅ | 0e17cb8e |
| N2 | Rectangle split button: shrinks too small on "Ctr. Rect"; grows too big on "Parallelogram" and bleeds into the Circle button | ✅ | 0e17cb8e |
| N3 | Features ribbon Revolve/Sweep/Loft → Cut mode: text gets cut off inside the button in cut mode | ✅ | 0e17cb8e |
| N4 | Features ribbon: after switching Revolve/Sweep/Loft to a Cut mode you **can't switch back** (stuck one-way toggle) | ✅ | 0e17cb8e |
| N5 | Audit ALL split/variant ribbon buttons for the same width-not-recomputed bug; apply the widest-label sizing rule everywhere | ✅ | 0e17cb8e |

**Fix (all N1–N5, ribbon.cpp):** split/variant big buttons now size to their **widest-variant width** (`ribbon_bigBtnWMax` / `ribbon_bigBtnAsSmallWMax` = max over parent + injected default + every `sub_of` variant label), threaded identically through measure (`ribbon_groupNaturalW`), advance, and draw (new `wOverride` param on `ribbon_bigBtn`/`ribbon_bigBtnAsSmall`) so the face label can change without reflow/clip/bleed. `kBigBtnMaxW` raised 96→120 so no-space labels like "Parallelogram" fit. **N4:** `ribbon_flyoutPopup` now injects the functional parent (`stretch==false`, not a `kParentDefaults` host) as the first flyout row, so selecting it sets `s_LastUsed[parent]=parent` and `resolveFace` falls back to the parent face — the variant toggle cycles both ways for every split button.

## O. AI functional / backend bugs (2026-07-07)
| # | Item | Status | Commit |
|---|------|--------|--------|
| O1 | **Model picker is decorative** — GUI provider+model selection (composer dropdown, `s.provider` ~legacy_chat.cpp:1822) is NOT threaded into the agent-turn RPC payloads `agent_ask_v2` (~L2696) / `agent_clarify_v1` (~L2748) (nor equivalents in ai_workspace.cpp). Backend `services/agent/config.py get_provider()` is called with no override → falls back to config.json/env default, silently ignoring the user's pick. **Fix:** thread provider/model through those RPC payloads + any sibling agent-turn calls; Python RPC handlers accept the override and pass `get_provider(name=...)`. Trace GUI state → payload → handler → get_provider end-to-end; add/update Python test if coverage exists. **QUEUED — fire after Wave 1 (Phase 0 + Phase 1) lands; collides with both agents' files (ai_workspace.cpp, services/agent).** | ⬜ (queued) | — |

---

## Open / next-up queue (orchestrator dispatches on Hunter's go)
- **M (AI workspace v2)** — 🔧 architecture/plan pass running (2026-07-07). Big multi-phase rework; will decompose into ordered build agents after the plan lands. M20 (event model) is the load-bearing prerequisite for M9/M12–M14.
- **K2** — EM study forms — ✅ committed `34964e5a`+`aac390c3` (2026-07-07), awaiting Hunter's live verify.
- **G3** — richer live sub-agent enumeration. **SUBSUMED by M11 (`b3d1e923`/`9a18af77`):** the worker now exposes `subagent_list` and the sidebar lists live registry sessions (clickable → open/steer). Empty-state remains honest when nothing has been delegated.
- _(new rows land here as Hunter red-pens the list above)_
