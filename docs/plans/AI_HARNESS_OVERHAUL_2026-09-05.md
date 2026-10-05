# AI Harness Overhaul — Auto-first settings for mechanical engineers

Owner: Hunter. Drafted 2026-09-05. **Status: partially adopted design record.**
The 2026-09-07 entry "Copilot preferences and Auto resolver — overhaul Phases 0–1" in
[`docs/REFACTOR_LOG.md`](../REFACTOR_LOG.md) records those phases and explicitly leaves
Settings/transparency Phases 2–3 and VERIFY LATER experiments untouched/unrun.
It is not evidence that the full UI proposal shipped. Current policy lives in
`services/agent/preferences.py`; current online-runtime and compaction authority is
[Chudcode integration](../../.claude/skills/cad-chudcode-integration/SKILL.md).
In particular, the user's selected compaction method must be honored; the proposal
below to hide that choice is not current policy. Source-line citations, model examples,
control counts and migration instructions below are the original design snapshot.

Original status: "PROPOSAL (no code changed)." Original target user: a mechanical
engineer or ME student using a CAD app — not an AI power user. Inspiration:
*The Harness Playbook*, Can Bölük, 2026-09-02 —
https://stencil.so/blog/harness-playbook. `docs/plans/AI_HARNESS_ROADMAP.md` (WS0–WS5)
covers harness *correctness/benchmarking* and trends toward MORE settings (WS2 adds
per-role rows); this plan is the opposite vector — see Open question 1.

## 1. What the playbook recommends, and what applies here

| # | Playbook claim (chapter) | Applies to a CAD copilot? |
|---|---|---|
| P1 | **Choose the operating modes first**; every subsystem must survive all of them (Ch.1, "design envelope": multiplexed workspace / remote driver / spectator / factorio) | YES, reduced: our envelope is local GUI, headless mission (`scripts/run_mission.py`), web parity (`Webservice/`). Fleet + untrusted-repo cases are power-user-only |
| P2 | **One authoritative session**, and **non-replayable state made unrepresentable** — correctness from constraint, not documentation (Ch.2; App.A: 17 stateful examples, 2 correct) | Session state is Chudcode's job (`cad-chudcode-integration/SKILL.md:17-25`) — our lesson is never to keep a GUI-side duplicate. Our real analogue is *settings*: one setting's truth is split across `config.json`, env vars and YAML |
| P4 | **Declare policy WITH the setting** (name, default, help string, scope flags at the definition site — no god object, no per-caller setter) and **inheritance needs no second setting** (a child seeds every value from the parent; pin children with one class cfg) (Ch.4, convars) | YES — the most applicable pair. Our defaults live in three languages, our help text lives in ImGui tooltips, and per-role model matrices exist only because inheritance was never the default |
| P6 | **One Director stack owns "who keeps control across turns"** so plan/goal/force-tool compose instead of hand-written mode mutexes (Ch.4) | Concept YES, implementation NO (Chudcode owns the loop). Our version: mode / plan / advisor / council must stop being four independent gates |
| P7 | **Model quirks are structured knowledge with explicit precedence; `unknown` is legal** (Ch.5) | YES for the *shape* of `model_rankings.yaml` + tier detection; the compat work itself is Chudcode's |
| P8 | **Compaction is scheduled, not triggered** — start ~10% before the limit from a snapshot, splice into the live branch (Ch.5) | YES as a *default*, NO as a *setting*. A user must never see "Snapcompact / Shake / Summary" |
| P9 | **Use a tiny local model for harness chores** — classification, titles, judging (Ch.5) | YES. Exactly how AUTO's task classifier should run; we already ship 4B GGUFs (`services/agent/model_manager.py:95-138`) |
| P10 | **Every permanent tool taxes every turn**; 5 tools beat a full roster ~2× on wall clock; long tail behind a stable discovery surface (Ch.6) | YES, partly done (`MAIN_CHAT_CORE_TOOLS` + `enable_tools`, `cad-chudcode-integration/SKILL.md:107-114`). Confirms "tool families" must NOT be a user setting |
| P12 | **Approve at the capability boundary, not the shell string** (Ch.6) | YES, and the right model for CAD: approve destructive geometry / overwrite / spend — not tool names |
| P13 | **Views are projections; controller ≠ actor** (Ch.1/2/7) | YES — GUI, AI workspace and web client must render one feed |
| P14 | **Define what verification means, or the agent invents a look-alike test** (Ch.7) | YES — `verify_battery` stays a floor, never a checkbox |

Out of scope (power-user-only): the DOM/journal rewrite, convar console with `bind`/`alias`, Director as a public
extension API, the `dyn` CLI, Python extension runtime, Elastic Speculative Slots. What we take is the discipline:
one owner per fact, defaults declared with the knob, inheritance by default, small permanent surface.

## 2. Inventory of the current user-facing AI surface

The codebase's own audit: **155 interactive controls on Settings→AI, of which 10 are "everyday"**
(`host/src/ui/settings_modal.cpp:6030-6046`); progressive disclosure exists — `Settings::showAdvanced`, default `false`
(`host/src/core/app_state.h:240`) — hiding 7 of 8 AI sub-tabs (`settings_modal.cpp:6050-6059`). Four top-level tabs
`0=Appearance 1=General 2=AI 3=Providers` (`:5891`, bar `:6279`); AI sub-tabs Harness/Roles/Prompts/Skills/Rules/
Benchmarks/Tools/Dev Preview (`:6905`); search index 129 rows (`:5894-6025`). One settings file shared by GUI, Python
and the C++ engine: `config.json` (`services/agent/config.py:51-73`; writer `host/src/core/app_state.cpp:870-1133`).

### A. Turn behaviour (the one "everyday" page)
| Setting | UI | Code | Default | Novice-hostile |
|---|---|---|---|---|
| Copilot mode Agent/Plan/Ask | composer drop-up `copilot/composer.cpp:2588-2650`; read-only grid `settings_modal.cpp:4020` | `copilotMode` `app_state.h:138`, enum `:82-86` | Agent (0) | no |
| Plan / Task list / Subagent / Advisor preference (each Off/Option/Encourage/Require) | `settings_modal.cpp:4073`, `:4079`, `:4086`, `:4090` | `planPreference`,`taskListPreference`,`subagentPreference`,`advisorPreference` `app_state.h:124-128` | 0 Off / 2 Encourage / 1 Option / 2 Encourage | **yes** — the code itself says a user "cannot tell Encourage from Require" (`:4065-4067`) |
| Proactive advisor · Company structure auto-route | `:4108`, `:4116` | `proactiveAdvisor`,`corporateMode` `app_state.h:130,133` | false / false | **yes** |
| Subagent tool-call budget (0–64) · Max subagent nesting (0–4) | `:4260`, `:4300` | `agent.subagent_tool_call_budget`; `agent.max_subagent_depth` (`services/agent/config.py:301-319`) | 0 = automatic / 2 | **yes** ("token grant", "nesting levels") |
| Editable feature sequence | `:4159` | `expCadSequenceMode` `app_state.h:219` | false | **yes** ("DeepCAD / CAD-Coder") |

### B. Models, providers, effort
| Setting | UI | Code | Default |
|---|---|---|---|
| Provider (11 entries) + 8 API-key fields + OAuth accounts | Providers tab `copilot/settings.cpp:461-926` | `provider`, `*Key` `app_state.h:99-115` | 9 = ZenMux, keys empty |
| Model | same + composer chip `composer.cpp:3100` | `model` `app_state.h:104` | `x-ai/grok-4.5-free` |
| Generation: reasoning effort · max response tokens · context window | composer chip `composer.cpp:3137-3150`; Providers "Generation" `copilot/settings.cpp:940,975,994` | `reasoningEffort`,`maxTokens`,`contextWindow` `app_state.h:116,148,151` | `"high"` / 0 / 0 = auto |
| Hidden models / disabled providers | `copilot/settings.cpp:1299`; `settings_modal.cpp:1867` | `hiddenModels`,`disabledProviders` `app_state.h:188,193` | empty |
| **15 model-role seats × (model, thinking)** | Roles sub-tab `settings_modal.cpp:3455-3676`, keys `:2745-2747` | `ai_roles` (`services/agent/role_models.py:44,47-63`) | Auto / `"auto"` |
| **Eng. Firm: 4 roles + 8 disciplines × (model, thinking)** | `settings_modal.cpp:3954-3989` | `eng_firm.models.*` `core/agent/config.h:117-140` | `{"mode":"default"}` |
| Vision & spatial split (4 fields) | `settings_modal.cpp:4343-4441` | `expVisionReview*` `app_state.h:213-216` | false / `qwen2.5-vl-7b-instruct` / `localhost:1234` |
| Offline mode / compliance mode | `:4125`, `:4138` | `offlineMode`,`complianceMode` `app_state.h:119,203` | false / `"off"` |

### C. Context & memory
Compaction strategy Off/Snapcompact/Shake/Summary (`settings_modal.cpp:4551-4562`, default `"off"`); supersede stale
reads (`:4602`, true); drop useless turns (`:4607`, true); per-session auto-compaction (`:4649`) + auto-retry (`:4656`)
via `chudcode_set_auto_compaction`/`_auto_retry` (`services/agent/worker_rpc.py:2307-2329`); project memory (`:4133`,
true) + memory budget 100–2000 (`app_state.h:135`, default 400); Auto-Learn + auto-capture (`:4683`, `:4690`, false).

### D. Authoring & power surfaces
Prompts (role-prompt editor + `system_prompt` override, `settings_modal.cpp:7079-7233`, `services/agent/roles.py:184-243`);
Skills (`:5319`); Rules/context-injection triggers (`:5579`); Benchmarks + leaderboard (`:7262-7404`); Tools — global
limits (`:7286`), **per-role × 19 raw MCP tool-family checkboxes** (`:7405-7887`, families `:7428-7432`), per-tool
timeout/max-calls (`services/agent/config.py:818-822`), bug reports (`:8356`); Dev Preview prompt dump (`:8377`).

### E. Duplicates and overlaps to collapse
| # | Duplication | Evidence |
|---|---|---|
| 1 | **Four dials for one idea** (delegation breadth/depth) | `agent.max_subagent_depth`=2 (`config.py:301-319`) · `DEFAULT_SOFT_DEPTH_LIMIT`=8 (`services/agent/delegation.py:79-80`) · effort knob `delegation_depth` mirroring `DEPTH_CAP`=3 (`delegation.py:71-76`) · `agent.corporate.max_departments`=3 (`config.py:378-436`) |
| 2 | **Three per-role model trees** | `ai_roles` 15 seats · `eng_firm.models` 12 rows (`core/agent/config.h:117-140`) · `agent.prewalk_target`=`"@smol"` (`core/agent/config.h:104-112`) |
| 3 | **Three role taxonomies, colliding names** | C++ `kRoles` 15 prompts (`core/agent/roles.cpp:30-35`) · Python role cards ~30 (`services/agent/role_catalog.py:8-38`) · 15 `ai_roles` seats — joined by lossy many-to-one maps (`role_models.py:2063-2071`, aliases `:69-78`), so `model3d`/`cam_programmer`/`sim_engineer`/`spec_writer` funnel into one `"task"` bucket |
| 4 | **Three "how hard does it think" axes** | per-role `thinking` auto\|low\|medium\|high (`role_models.py:79-81`) · `ai_effort` dial eco\|standard\|deliberate\|maximum (`services/agent/effort.py:60-67`, `effort_presets.yaml:42-89`) · tier `deliberation` full\|standard\|minimal (`services/agent/tiers.py:130-161`) which the dial derives from |
| 5 | **`ai_effort` is unreachable from the GUI, and compaction is implemented twice** | Grep of `host/src` for `ai_effort\|effort_presets\|deliberation\|council\|ladder` finds no settings surface: the dial, the cognition ladder (`services/agent/ladder.py:36-73`) and councils (`services/agent/councils.py:73,86-87,96-100`) are settable only by hand-editing `config.json` or via `CHUDWARE_COUNCIL_*` (`councils.py:81-85,918-921`). Separately, Chudcode session auto-compaction and the offline-only constants in `services/agent/compaction.py:26-31` are two implementations behind one GUI combo |
| 6 | **Same value stored repeatedly, plus dead UI** | `planMode` duplicates `planPreference != 0` (`app_state.h:122-129`); `copilotMode` lives in three places (`Settings` `:138`, `AppState::aiInteraction` `:1669`, `ChatMsg::copilotMode` `:725`); `agent\|plan\|swarm\|edit\|ask` is frozen (`contracts/copilot_modes.md:5`) but the GUI only sends Agent/Plan/Ask, so `swarm`/`edit` are unreachable |

### F. Concepts a novice cannot be expected to understand
Undefinable for this audience: "role" (three meanings), "cycle/loop", "subagent nesting", "advisor", "council",
"deliberation", "cognition ladder", "compaction / Snapcompact / Shake", "context window", "token grant", "MCP tool
family", "DAG / mission", "Seek-CAD split", "capability profile A/B/C", "tier", "prewalk". Shipped strings: *"How
freely the main agent may spawn subagents. Off disables nesting; Require pushes multi-agent splits."*
(`settings_modal.cpp:4087-4088`); *"Off = no advisor. Option = only when the model asks. Encourage = stronger guidance
tone when proactive fires."* (`:4091-4093`); *"Convenes a 2-round multi-seat deliberation (build_strategy, spec_review,
failure_review, …)"* (`host/src/ui/tool_panel_tables.h:1160`); bare tooltip *"MCP tool."* (`settings_modal.cpp:7495`).
And there is **no user documentation to fall back on** — `docs/manual/` is generated `cad_guide` prompt text addressed
to the model, not to a human (`docs/manual/index.md:1-16`).

## 3. Proposed model: four preferences, one Auto, one Advanced drawer

P4/P5 applied — each preference is declared once with its default and help string and *derives* everything below it.

| # | Preference | Choices | Default | Derives |
|---|---|---|---|---|
| 1 | **How I want to work** | `Do it` · `Check with me` · `Answer only` | Check with me | `copilot_mode` (agent/plan/ask), plan + task-list policy, approval gates for destructive ops |
| 2 | **Priority** | `Auto` · `Best results` · `Fastest` · `Lowest cost` | **Auto** | model + thinking per turn, deliberation, ladder/council, delegation depth, compaction schedule, roster width |
| 3 | **Where it runs** | `Cloud` · `This computer only` | Cloud | `offline_mode`, `compliance_mode`, provider filter, vision routing, redaction seat |
| 4 | **Double-check my work** | `On` · `Off` | On | advisor pull, `verify_battery` floor, interference/battery gates, reviewer pass, council at ≥5 features |

| Retired user setting | Now derived from |
|---|---|
| `planPreference`, `taskListPreference` | Pref 1 |
| `subagentPreference`, `agent.max_subagent_depth`, `subagent_tool_call_budget`, `corporateMode`, `agent.corporate.*`, `advisorPreference`, `proactiveAdvisor`, `ai_effort` level, ladder, council | Prefs 2 & 4 + task kind |
| `reasoningEffort`, `ai_roles.*.thinking`, tier `deliberation` | Pref 2 (one resolved `thinking` per turn) |
| `ai_roles` 15 seats, `eng_firm.models` 12 rows, `agent.prewalk_target` | Pref 2 → **3 seats**: `chat`, `worker`, `private` |
| `complianceMode`, `offlineMode`, `expVisionReview*`, `ai_roles.redaction` | Pref 3 |
| compaction strategy / supersede / drop-useless / session toggles | always-on scheduled compaction (P8) |
| per-role × 19 tool-family checkboxes | automatic roster (`MAIN_CHAT_CORE_TOOLS` + `enable_tools`, P10) |
| `expCadSequenceMode` | always on — it is what a parametric CAD user wants |

### 3.1 AUTO rules
The classifier runs on the bundled local model (P9; `model_manager.model_for_role_shape("fanout")`,
`services/agent/model_manager.py:431-445`) — free, fast, never a frontier call.

| Signal | Rule | Grounded in |
|---|---|---|
| **Chat question** (no geometry verb, no mutation intent) | cheapest capable model, `thinking=low`, no subagents, no advisor; the roster follows the user's work style (a chat question in Agent mode can still edit) | `_TASK_CLASS_MIN_RANK["simple"]=1` `role_models.py:161-172` |
| **Modelling** (features on one doc) | mid tier, `thinking=medium`, verify-battery floor, no delegation | `min_rank multi_feature_part=3` (same table) |
| **Simulation / analysis** | strongest available for setup, `thinking=high`, sim precheck hard-block on | `min_rank["analysis"]=4`; `CHUDWARE_SIM_PRECHECK_BLOCK=1` `services/agent/advisor/sim_advisor.py:1078` |
| **Long mission** (multi-deliverable or ≥2 docs) | strong planner + cheap leaves, delegation depth 2, task list on, council when features ≥5 | `_DEFAULT_FEATURE_THRESHOLD=5` `councils.py:73`; depth 2 `config.py:301-319` |
| **Document size** | est. context ≥50% of window → schedule compaction + trim tool schemas; features >40 → bump tier | `CONTEXT_LENGTH_TIER_MAP` `tiers.py:81-85`; `tool_schema_trim` `tiers.py:130-161` |
| **Provider availability / 429** | walk cost classes local 0 → vllm 1 → subscription 2 → openrouter 4 → metered 5, skipping unauthenticated; on a rate limit wait out retry-after inside the turn, then take the next class — one plain sentence, never a silent downgrade | `_COST_CLASS` `role_models.py:141-158`; `chudware-dev/SKILL.md:185-196` |
| **Pref 3 = this computer only** | hard filter to loopback providers first; if none, refuse and name the fix | offline-safe table `contracts/providers.md` §5 (FROZEN) |

Auto never changes Prefs 1, 3 or 4 — only model, thinking, deliberation depth, roster width and compaction timing.
Every choice emits one feed row (§4).

### 3.2 Proposed Settings layout
```
┌ Settings ─────────────────────────────────────────────────── [Advanced ○ ] ─┐
│  Appearance   General   ▸ Copilot ◂   Accounts                              │
├─────────────────────────────────────────────────────────────────────────────┤
│  How I want to work                                                         │
│    ( ) Do it            Builds and edits without asking.                    │
│    (•) Check with me    Shows a plan first; asks before it overwrites.      │
│    ( ) Answer only      Explains and measures. Never changes your model.    │
│  Priority                                                                   │
│    (•) Auto   ( ) Best results   ( ) Fastest   ( ) Lowest cost              │
│    Auto picks the model per request: a question → Grok 4.5 Free, modelling  │
│    → Claude Opus 5, a long job → strong planner + cheap helpers.            │
│  Where it runs  (•) Cloud   ( ) This computer only — nothing leaves this PC │
│  Double-check my work      [x]  Measures + reviews before "done". Slower.   │
│  Memory of this project    [x]  Remembers decisions, per project folder.    │
│  Show what the AI is doing [x]  Steps, tools and timing in the chat.        │
│  ── Advanced ───────────────────────────── (only when the switch is on) ──  │
│  Models   chat ▾Auto  worker ▾Auto  private ▾Auto  Thinking ▾Auto           │
│  Behaviour  Delegation depth [2]  Wrap-up hint after [auto]  Team mode [ ]  │
│  Context      Compaction: scheduled at [80]% of window   Memory [400] tok   │
│  Authoring    Prompts…  Skills…  Rules…  Tools…  Benchmarks…                │
│  Diagnostics  Assembled prompt…   Bug reports [x]   Repair runtime          │
└─────────────────────────────────────────────────────────────────────────────┘
```
The Accounts tab (renamed from Providers) keeps sign-in, keys and usage only — no behaviour knobs.

### 3.3 Default values
| Key | Default | Change vs today |
|---|---|---|
| `copilot.work_style` | `check_with_me` | new; today `copilotMode=0` Agent (`app_state.h:138`) |
| `copilot.priority` | `auto` | new; today effort is pinned `reasoningEffort="high"` (`app_state.h:116`) |
| `copilot.location` | `cloud` | equals today's `offlineMode=false` |
| `copilot.double_check` | `true` | today advisor=Encourage(2); council/ladder unreachable |
| `ai_roles.{chat,worker,private}` | `"auto"` | replaces 15 seats + 12 Eng-Firm rows |
| resolved `thinking` | `auto` per turn | today `"high"` globally |
| `agent.max_subagent_depth` · compaction | 2 · scheduled at 80% | depth unchanged (`config.py:301-319`); compaction was `"off"` (`settings_modal.cpp:4551`) |
| `memory_enabled` / `memory_token_budget` | true / 400 | unchanged (`app_state.h:134-135`) |
| `show_advanced` · provider / model | false · ZenMux `x-ai/grok-4.5-free` | unchanged (`app_state.h:240`, `:99-104`) |

## 4. Transparency without jargon
1. **"Why this model" chip** beside the composer model chip: *"Auto chose Claude Opus 5: you asked for a new part and
   this project is large."* Reuses the `window_source` provenance pattern (`copilot/composer.cpp:1974-1978`).
2. **Before a long run**: *"About 3–6 minutes, roughly 40¢ on your Anthropic plan. Start / Use a cheaper model."*
   Inputs exist (`services/agent/eta_estimator.py`; cost strip `copilot/activity_timeline.cpp:1976`); no currency
   figure is shown today. 3. **Per-turn receipt stays** (`activity_timeline.cpp:2042-2110`) in plain words: *"12
   actions · 2 retried · 1 part saved · 48 s · Opus 5"*. 4. **Capability, not tool names** (P12): *"This turn can
   measure, sketch and save. It cannot delete files or spend money."* — from the effective roster, not a checkbox page.
5. **Failures name the fix** — keep `errorHuman` (`copilot/chat_log.cpp:932-937`), extend to rate limits and offline
   refusals: *"xAI is rate-limited for 3 minutes. Waiting, then switching to Grok 4.5 Free."* 6. **One live "what Auto
   is doing" line** in the AI workspace so the projection rule (P13) holds across GUI, workspace and web client.
   Retired jargon: compaction, deliberation, subagent, advisor, role, MCP, tier, token grant.

## 5. Migration plan
**Phase 0 — freeze semantics, change nothing.** New `services/agent/preferences.py` owns the four preferences, their
defaults, help strings and the full derivation table (P4), consumed by `worker_rpc._agent_admin_turn`,
`mission_dispatch.linear_role_prompt`, `copilot_mode_tools.apply_mode_tools`. No deletions. Risk: none.

**Phase 1 — Auto resolver.** Task classifier on the bundled local model (`model_manager.py:431-445`) plus the §3.1
rules, replacing the seat lookup in `role_models.resolve_role_model` for the 3 remaining seats. Touches
`services/agent/role_models.py`, `effort.py` (collapse to resolver inputs), `tiers.py` (keep detection, drop the
user-facing `deliberation` field). **Deleted:** `effort_presets.yaml`, the `ai_effort` key and its 20 knobs
(§2.E.5), `agent.prewalk_target`. Risk: mission-lane regressions; pin with `services/agent/test_*`.

**Phase 2 — Settings UI cutover.** Rewrite the AI tab as §3.2. Delete `drawModelRolesSection` (`settings_modal.cpp:3455-3676`),
`drawEngFirmModelAssignments` (`:3954-3989`), `drawVisionRoutingSection` (`:4343-4441`), the four preference combos
(`:4073-4093`), the compaction combo (`:4551-4562`), the per-role × 19 tool-family matrix (`:7405-7887`). In
`host/src/ui/copilot/settings.cpp` + `host/src/core/app_state.h/.cpp` retire `planPreference`, `taskListPreference`,
`subagentPreference`, `advisorPreference`, `planMode`, `proactiveAdvisor`, `corporateMode`, `expVisionReview*`,
`expCadSequenceMode`; add the four preference fields; `kSettingsEntries` 129 rows → ~40. `loadSettings` migrates old
keys once and stops writing them — no aliases, no shims. **Risk:** the merge-overlay discipline in
`applyGuiOwnedSettings` (`app_state.cpp:889-891`) exists because a full overwrite once wiped `subscription_auth`, so
every removed key must be erased deliberately while `ai_roles`, `plm.*` and `subscription_auth` survive.

**Phase 3 — transparency.** §4 items 1–6 in `copilot/composer.cpp`, `activity_timeline.cpp`, `chat_log.cpp`,
`host/src/ui/ai/ai_workspace.cpp`; touch `services/agent/chudcode_events.py` only if a new feed row is needed — event
mapping is single-display and terminal-strict (`cad-chudcode-integration/SKILL.md:154-169`).

**Phase 4 — docs.** `docs/manual/` is model-facing (`docs/manual/index.md:1-16`), so first-run user documentation for
the Copilot page is written fresh (four preferences, one page). Handoff appended to `docs/REFACTOR_LOG.md`.

**Contract safety.** `contracts/*` is append-only and nothing here removes a frozen shape: the mode vocabulary
(`contracts/copilot_modes.md:5`), the provider/adapter interface, auth-kind taxonomy and offline-safe table
(`contracts/providers.md` §2/§4/§5) and every `aicad.*/1` envelope stay — we stop *exposing* values, we do not delete
fields. The one item needing an owner decision is `contracts/providers.md` §14 ("ALL role assignments are user-owned…
every role appears as an editable row") — a Hunter directive, not a frozen schema (Open question 1).

**Other risks.** (a) Auto mis-routing a mission to a cheap model — mitigated by the cost-class walk plus one visible
"why" row. (b) Removing the per-role table before Auto earns trust — the 3 seats stay as the escape hatch. (c) Losing
a capability by hiding a knob — the existing rule holds: `config.json` stays authoritative and is read whether or not
a control is painted (`app_state.h:232-240`). (d) `scripts/run_mission.py` must resolve the same preferences.

### VERIFY LATER (no benchmarking now — inference budget)
Every §3 choice is **provisional**. Free models first (ZenMux `x-ai/grok-4.5-free`, bundled GGUFs); frontier only to
settle what free models cannot.

| Decision | Cheapest experiment |
|---|---|
| Auto's task classifier is accurate enough | Hand-label 60 real prompts from existing session JSONL; run the bundled local model as classifier offline; measure confusion. Zero paid inference |
| `thinking=low` suffices for chat questions | Replay 20 archived Q&A turns at low vs high on the free model; compare correctness by eye. ~40 free calls |
| Cheap-leaf delegation saves money without failing; depth 2 vs 3 | Re-run one archived `mega-*` mission fixture with leaves forced local — exactly the `mega-7` "cheap-share >0, token share 0.72%" case (`docs/plans/AI_HARNESS_ROADMAP.md:166-170`); compare pass count and tokens, then repeat once at depth 3 |
| Council threshold ≥5 features is right | Sweep 3/5/8 on the deterministic gym C1–C15, local models only (`CHUDWARE_COUNCIL_FEATURE_THRESHOLD`, `councils.py:918`) |
| Compaction at 80% vs 90% | Replay one long archived session through the compaction path offline; measure retained-fact rate on 10 hand-written questions. Zero paid inference |
| Verify-battery-always is not too slow | Time 10 modelling turns with the gate on/off on a free model; report added seconds |
| Auto's provider fallback order | Fault-inject 429/401 against the mock provider (`CHUDWARE_PROVIDER=mock`) and assert the walk order. Zero inference |
| Local-only mode is usable for real modelling | 5 bracket/plate prompts on the bundled 4B GGUFs; record pass/fail so the UI can warn honestly |
| Four preferences actually cover real use | Watch 3 ME students work for 30 min each; count Advanced-drawer opens and what they went looking for |

## 6. Open questions for Hunter
1. `contracts/providers.md` §14 records your directive that **every** role — including `overseer` — is an editable GUI
   row. Collapsing 15 `ai_roles` seats + 12 Eng-Firm rows to 3 contradicts it: retire it, or keep the matrix in Advanced?
2. Is company-structure / Eng-Firm mode still a product feature, or may it be deleted outright
   (`core/agent/config.h:117-140`, `settings_modal.cpp:3954-3989`, `services/agent/corporate.py`)?
3. Default work style: `Check with me` (proposed, safer for students) or `Do it` (today's Agent)?
4. Real currency, or time plus "on your plan"? A dollar estimate needs pricing we only have when the catalog marks a
   model `priced` (`host/src/ui/copilot/copilot.h:86`).
5. Does the composer keep three chips (mode / model / thinking), or drop to one **Auto** chip that expands on click —
   i.e. does the per-turn model override stay first-class?
6. Benchmarks, Prompts, Skills, Rules, Dev Preview: keep in the Advanced drawer, or move behind a developer flag
   (`CHUDWARE_DEVELOPER_BUILD`, `services/agent/chudcode_host.py:1475-1479`) so a student build never shows them?
