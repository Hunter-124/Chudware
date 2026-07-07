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
Hunter's red-pen on the shipped AI hub: make it compact, transparency-first, DAG-centric. Screenshot marks regions: **DAG** upper-center, **conversation** lower-left (green box), **workflow settings** lower-right ("Settings/Options/Defaults"), inference settings **wiped**.
### Layout / chrome
| # | Item | Status | Commit |
|---|------|--------|--------|
| M1 | Make the AI tab more compact overall | ⬜ | — |
| M2 | WIPE the inference-settings block from the AI panel (Model, Reasoning effort, Max response tokens, Model context window, API key, Privacy, Vision reviewer) — those live ONLY on the main Settings page | ⬜ | — |
| M3 | AI panel keeps ONLY AI *workflow* settings (Supervised/Autopilot, refine passes, max output tokens, context window), relocated to lower-right ("Settings/Options/Defaults" zone) | ⬜ | — |
| M4 | DAG view occupies the upper main area, above the workflow settings | ⬜ | — |
| M5 | Conversation window (message log + composer) moves to lower-left (green box); composer laid out per red box: `Permissions ▾` row → `[Model picker ▾][Effort ▾][ctx display]` → `Design/Edit/Ask/Permissions` mode tabs → input | ⬜ | — |
| M6 | Remove the "Conversation" tab; conversation window is always visible (not a tab) | ⬜ | — |
| M7 | Tab switcher becomes **Agent Workflow** \| **Session Plan** (switches the DAG pane only) | ⬜ | — |
| M8 | Rename "Project Plan" → "Session Plan"; scope it per chat session (tied to each session) | ⬜ | — |
### DAG (the centerpiece)
| # | Item | Status | Commit |
|---|------|--------|--------|
| M9 | **Agent Workflow DAG** = live agent-execution DAG for the selected session: nodes for tool calls (+ what they act on), context injections, subagent/worker/specialist branches off the main branch, advisor/council chats, interruptions/special helpers; updates live as execution happens | ⬜ | — |
| M10 | Collapsible/scalable DAG nodes — expand/collapse tool calls & sub-parts for easier tracking | ⬜ | — |
| M11 | Click a subagent node (including active) → open/steer that subagent's chat in the main conversation window | ⬜ | — |
### Chat transparency (troubleshoot the harness/loops)
| # | Item | Status | Commit |
|---|------|--------|--------|
| M12 | Show context-injection events inline in the chat | ⬜ | — |
| M13 | Show the model's streamed thoughts (when streamed) | ⬜ | — |
| M14 | Show tool calls inline: which tool, what it acts on, and all program parts the AI touches (max transparency) | ⬜ | — |
### Subagent chat viewing / steering
| # | Item | Status | Commit |
|---|------|--------|--------|
| M15 | Viewing a subagent chat recolors the whole conversation (clear "you're in a subagent" indication) | ⬜ | — |
| M16 | Lock the user from easily ending a subagent chat | ⬜ | — |
| M17 | Model / effort / starting-prompt are read-only when viewing a subagent chat | ⬜ | — |
### Chat UX
| # | Item | Status | Commit |
|---|------|--------|--------|
| M18 | Prompt box auto-grows upward (with a max cap) as the user types a long prompt | ⬜ | — |
| M19 | Endless scroll-back with lazy-loading of older messages (no crash on large history) | ⬜ | — |
### Backend / instrumentation (load-bearing — feeds M9/M12/M13/M14)
| # | Item | Status | Commit |
|---|------|--------|--------|
| M20 | Agent-execution event stream: harness/orchestrator emits structured, timestamped, parent-linked events (tool call start/end + target, context injection, subagent spawn/branch, advisor/council call, interruption) that feed the Agent Workflow DAG + transparency chat live. **Architecture pass in progress** — gap analysis vs existing AgentEvent/ActivityItem/buildgraph. | 🔧 | — |

---

## Open / next-up queue (orchestrator dispatches on Hunter's go)
- **M (AI workspace v2)** — 🔧 architecture/plan pass running (2026-07-07). Big multi-phase rework; will decompose into ordered build agents after the plan lands. M20 (event model) is the load-bearing prerequisite for M9/M12–M14.
- **K2** — EM study forms — ✅ committed `34964e5a`+`aac390c3` (2026-07-07), awaiting Hunter's live verify.
- **G3** — richer live sub-agent enumeration (needs a worker fleet-listing RPC; currently honest empty-state). **Likely subsumed by M9/M11/M20.**
- _(new rows land here as Hunter red-pens the list above)_
