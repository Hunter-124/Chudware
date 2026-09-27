# CAD Agent Stack Inventory (services/agent)

Scope: `/home/hunter/Repositories/CAD/services/agent` (Python, ~150 modules + tests),
its host boundary in `/home/hunter/Repositories/CAD/host`, and `services/agent/AGENTS.md`.

## Top-line framing (read this first)

`services/agent/AGENTS.md` states the ground truth: **the Python agent LOOP is
already dead code**. The single agent entry point is the C++ core
(`core/agent/run.cpp`, `agent_run_v2` / `agent_mission_v1`); the legacy
build123d code-gen loop (`loop.py` `run`/`run_turn`/`Session`, `run_mission`,
the `agent_design` RPC, `run_agent.py` CLI) was deleted in migration "T-RF5".
`loop.py` today (321 lines) is a stub holding only `SYSTEM_PROMPT`/`PLAN_PROMPT`
string constants (served to the GUI Settings prompt editor) and
`apply_context_refs` (folds GUI context-ref picks into request text; used by
`agent_turn.py`). **Do not port `loop.py` as "the loop" — there is no live
Python loop to replace.** (`services/agent/AGENTS.md:1-18`)

What IS live and load-bearing:
- `agent_turn.py` (6912 lines) — bounded tool-calling turn used by DAG role
  nodes and mission dispatch. Provider-agnostic over `Provider.complete(system,
  messages) -> str`, dispatches JSON-fenced tool calls against the MCP tool
  registry (`services.verify.battery._get_fn`), budgeted by tier
  (`max_tool_calls`), every turn ledger-logged. (`agent_turn.py:1-123`)
- `worker_rpc.py` (1346 lines) — the actual C++⇄Python RPC surface: merges
  subscription/session/skills/triggers/roles/bench RPC tables that the C++
  `engine_oracle/worker.py` registers. This (not loop.py) is the real host
  boundary for everything user-facing that isn't the C++ agent core.
  (`worker_rpc.py:1-44`)
- `agent_events.py` — builders for the `aicad.agent_events/2` schema
  (`agent.tool_call`+target, `agent.thought`, `agent.context_injected`,
  `agent.spawn`, `agent.advisor`), bridging two emitter conventions
  (`EventJournal.emit(kind,payload)` vs loop.py's `sink(data)`). This IS the
  transparency/DAG event stream the C++ GUI (`host/src/ui/ai/ai_workspace.cpp`,
  `activity_timeline.cpp`, `feed.cpp`) joins by `parent_id`→`id` to render the
  Agent Workflow DAG and inline transparency rows. Frozen contract, versioned
  (`/1`→`/2`). (`agent_events.py:1-46`; host greps above)

## Host boundary (C++ ties)

- **RPC**: C++ `engine_oracle/worker.py` (not in host/, in services/ presumably)
  registers `WORKER_METHODS` built from `worker_rpc.py`'s per-feature tables
  (subscription_*, session_compact/shake/handoff/btw, skills_*, triggers_*/packs_*,
  ai_roles_*/model_rankings_*, bench_*). Every RPC returns `{"ok": bool, ...}` —
  never a traceback; GUI hides on `unknown_method`.
- **Event stream**: `contracts/agent_events.md` (frozen, bumped `/1`→`/2`) is the
  single schema. Storage `~/.local/share/Chudware/cache/runs/<run_id>/events.jsonl`
  (persist-then-callback+fsync) via `services/dag/event_journal.py`
  `EventJournal.emit`. C++ side: `host/src/core/app_state.h` (`AgentEvent`
  struct), `host/src/core/jobs.cpp` (drain wrapper), `host/src/ui/ai/ai_workspace.cpp`
  (event-graph store, GL-thread only, plain static no mutex), `activity_timeline.cpp`
  / `feed.cpp` (inline transparency rows). This is a bespoke, already-working
  pipe — an OMP TUI/event mechanism would NOT replace this; at most OMP's event
  *shape/kind vocabulary* could inform additions.
- **Subagent registry/steering**: `subagent_registry.py` is file-backed
  (`~/.local/share/Chudware/cache/subagents/<id>.{json,transcript.jsonl,steer.jsonl}`)
  specifically because subagents run in a different process than the GUI-facing
  worker; steering is queued and drained at the subagent's next turn boundary
  (never a hard interrupt). This is a cross-process design constraint any
  SIDECAR/FORK option must respect — OMP's in-process agent tree does not have
  this problem and a naive port would need to re-invent this file-backed
  handoff or unify processes.
- **Sim/CAM/PLM workers**: `host/src/core/sim_client.*` documents worker_rpc.md
  conventions (no host-side timeout, explicit cancel) — a general pattern any
  new sidecar bridge (bun) must also follow (explicit cancel, no host timeout
  assumption baked into transport).

## Subsystem table

| Subsystem | Purpose | Key files | Maturity | OMP equivalent (more advanced?) |
|---|---|---|---|---|
| Agent loop | Legacy stub only; real loop is C++ `core/agent/run.cpp` | `loop.py` (321L, gutted) | **dead/stub** in Python; solid in C++ (not read here) | N/A — OMP's TUI agent loop is irrelevant; C++ is CAD's real loop |
| Single-turn tool exec | Bounded tool-calling turn for DAG/mission nodes | `agent_turn.py` (6912L) | solid, heavily hardened (secret scrubbing, provider-error formatting, budgets) | OMP `coding-agent`/`agent` packages have a fuller autonomous loop w/ streaming, retries — comparable maturity but different shape (CAD's is turn-bounded, non-streaming) |
| Providers | Provider-neutral `complete`/`complete_ex` adapters | `providers/base.py`, `anthropic_provider.py`, `openai_provider.py`, `xai_sub_provider.py` (72K!), `cursor_sub_provider.py` (46K), `codex_*`, `claude_cli_provider.py`, `kimi_sub_provider.py`, `sub_auth.py`, `usage.py` (102K), `subscription_login.py`, `oauth_pkce.py` | **solid, very mature** — subscription/OAuth login flows, per-provider quirks, usage tracking | OMP `ai` package is the analogous seam (see OmpProviderSwitch scout) — CAD's is arguably MORE built-out for consumer subscription auth (xai/cursor/kimi/codex sub-providers); OMP likely stronger on raw API breadth/streaming |
| Project memory | SQLite+FTS5 recall/remember, token-budgeted | `project_memory.py` (329L) | solid, small, self-contained | OMP `mnemopi` — comparable concept (durable project memory), but OMP's is a full package; CAD's is a lean 300-line module. Worth comparing designs (see OmpMemSnap) |
| Compaction | Transcript archival compaction, shake, handoff | `compaction.py` (1054L), `session_cmds.py` (376L) | solid — **explicitly a direct port of OMP's `snapcompact` text-phase + `handoff-document.md`** (see file docstrings quoting omp verbatim) | Already ported/adapted from OMP `snapcompact`; frame/vision-phase compaction "deferred" — a gap where OMP might be MORE advanced (image/frame compaction) |
| Advisors | Mid-run steering, sim triage, dissent/trigger system | `advisor/` (30+ files): `overseer.py` (38K), `triggers.py`+`triggers.yaml` (48K/16K), `sim_advisor.py` (43K), `failures.py` (32K), `change_request.py`, `reroute.py`, `preflight.py`, `engine.py`, context_packs/*.md | solid, CAD-domain-specific (sim/tolerance/thread/fit triage) | No direct OMP equivalent — this is CAD-domain knowledge (engineering triage), not a generic coding-agent feature. KEEP CAD's own |
| Proactive advisor | Opt-in token-cheap mid-run nudges on thrash/failures | `proactive_advisor.py` (406L) | solid, deliberately conservative (cooldowns/caps) | Analogous to OMP's proactive-advisor concept per `chudware-ai-session-settings` skill notes — likely CAD-originated already, or a close port; check overlap before touching |
| Councils | Multi-seat same-model deliberation w/ typed positions + dissent | `councils.py` (1059L), `council_policy.yaml` | solid, contract-frozen (§19) | No OMP equivalent — bespoke CAD design-review mechanism. KEEP |
| Delegation | Hierarchical delegation tree, budget meter, approval chain, depth caps | `delegation.py` (1054L) | solid, contract-frozen (§12), heavily rail-guarded (max_subagent_depth, soft_depth_limit, max_agents, BudgetMeter) | OMP `swarm-extension`/`agent` packages have subagent spawning too (see OmpArch/OmpTuiEngine scouts) — compare depth/budget model; CAD's is CAD-specific but the *mechanism* (tree + approval + budget meter) is generic and a good PORT/compare candidate |
| Coordination | Per-run resource-claim board (doc/feature/file ownership) to prevent parallel-worker clobber | `coordination.py` (488L) | solid, file-sidecar + flock | Generic concept; may overlap OMP's multi-agent coordination primitives — compare |
| Org (mission depts) | Clusters deliverables into departments, channels, requests, report cadence | `org.py` (690L) | solid, contract-frozen (§18) | CAD/PLM-domain-specific (deliverable manifests) — no OMP equivalent |
| Corporate mode | Director/department-head/worker/team-lead org structure for subagent orchestration, enforced comms channels | `corporate.py` (2162L!) | solid, very large, contract-shaped (`aicad.corporate/1`) | No OMP equivalent — this is a bespoke multi-agent-company simulation layered ON TOP of delegation.py + coordination.py. KEEP (huge, CAD-specific investment) |
| Ladder (design cognition) | Former four-rung design pipeline; no production caller remained | Role card and `ladder_*.yaml` playbooks remain loadable; Python ladder executor/compiler/validators were removed | historical contract, not a live runtime | No live OMP comparison |
| Mission dispatch | Run-scoped tool resolution for mission-context tools (ledger, dag_plan, ask_user, delegate, part_search) before falling to global registry | `mission_dispatch.py` (1979L) | solid, explicit honesty rules (no fabricated ask_user answers) | No OMP equivalent as such; conceptually similar to any tool-router-with-context, but content is CAD-mission-specific |
| Tiers/preferences/capability | Tier profiles (budgets), turn-level Copilot preference resolver, capability profile A/B/C | `tiers.py`+`tiers.yaml`, `preferences.py`, `capability.py` | live; the obsolete effort wrapper and effort preset table were removed | OMP has model-tier/effort concepts too; CAD's local GGUF tiers remain |
| Routing / model_manager | Role→model binding table (`routing.yaml`, single source of truth), local GGUF bundled-model provisioning | `routing.py` (822L), `model_manager.py` (446L), `model_catalog.py`, `model_rankings.yaml` | solid | OMP has provider/model routing (see OmpProviderSwitch); CAD's local-model bundling (qwen3-4b/gemma3-4b-qat via llama.cpp, resumable download) is a CAD-specific concern OMP likely doesn't need (OMP is cloud/subscription-first) |
| Guardrails | HARD physics/process floors (destructive-op classification, auto-checkpoint, interference/orphan-sketch gates) | `guardrails.py` (1244L) | solid, contract-tied to harness-tiers-spec | No OMP equivalent — CAD geometry safety. KEEP, non-negotiable |
| Redaction | ITAR-grade outbound scrub/rehydrate pipeline for compliance_mode=redact/local_only | `redaction.py` (784L) | solid, 4-stage pipeline (deterministic scrub → optional local-LLM flag pass → rehydrate → image-block) | No OMP equivalent (compliance-specific to CAD's regulated-industry customers). KEEP |
| Capability scaffolds | A/B capability-tier scaffolding (forced grounding/read-back etc.), no-op at C (frontier freedom) | `scaffolds.py` (901L) | solid | No OMP equivalent |
| Playbooks / pipeline / design_recipe | Deterministic 8-phase mission pipeline glue (manifest validation, phase gates, replan routing); NL→tool-recipe matcher; YAML playbooks | `pipeline/` (`manifest.py`, `phases.py` 25K, `replan.py`, `functional_decomposition.py`, `linkage_integrity.py`, `workflow_router.py`), `playbooks/*.yaml` (11 files), `recipes/*.yaml` (12 files), `design_recipe.py` (174L) | solid, contract-driven (§16/§17), composes ledger+delegation+roles+buildgraph | No OMP equivalent — CAD mission-domain. KEEP |
| Session cmds / workspace / ledger | `/handoff`,`/btw`,compact,shake host RPCs; multi-doc workspace (write/read leases, per-doc event streams, buildgraph fan-out); persistent mission-state ledger (event-sourced, budgeted render) | `session_cmds.py` (376L, **handoff prompt is literally lifted from OMP's `handoff-document.md`**), `workspace.py` (601L), `ledger.py` (812L) | solid, ledger is contract-frozen (§2), explicitly modeled as "anti-context-loss… cold-starts from ledger_render(budget) instead of chat history" | ledger.py's design goal (compact state instead of full history) parallels OMP's context/compaction philosophy generally, but ledger.py is bespoke event-sourced state, not a port. `session_cmds.py`'s handoff format IS already an OMP port |
| Tool system | Category packs (anti-context-flood `enable_tools`), tool schemas, tool families/hints | `tool_categories.py` (293L), `tool_schemas.py` (9K), `tool_hints.py`, `tool_categories.py` | solid | OMP has a tool/catalog package (`catalog`) — compare category-pack vs OMP's tool discovery model (see OmpExtTools scout) |
| Skills | Progressive-disclosure skill discovery/activation, explicitly modeled on OMP skills | `skills/__init__.py` (903L) + `skills/builtin/` | solid, **explicitly "modeled on Anthropic Agent Skills / omp progressive disclosure"** | Direct conceptual port already done; compare CAD's `skill://` resolver against OMP's actual skill loader for feature gaps (trigger conditions, mid-prompt activation regex already present here) |
| Subagent registry | Cross-process live subagent session registry + steering queue (file-backed) | `subagent_registry.py` (324L), `subagent_rpc.py` | solid, explicit honesty limits (steer/terminate only honored at next turn boundary) | OMP's in-process agent tree doesn't need this cross-process file-backed design — a SIDECAR bridge must account for this boundary explicitly |
| worker_rpc / agent_events | Host (C++) RPC surface + event schema/builders | `worker_rpc.py` (1346L), `agent_events.py` (444L) | solid, frozen contracts | This IS the host boundary — see above. Not a "port OMP" target; OMP has nothing analogous to a C++ desktop-app RPC bridge (its own bun-sidecar-bridge work is the closest parallel infra CAD would reuse in reverse) |
| Roles / role_models / role_catalog / role_adoption | Role card system (YAML `roles/*.yaml`), role→model overrides, role catalog, adoption rules (who may self-adopt vs must request) | `roles.py` (42.4K), `role_models.py` (71.6K!), `role_catalog.py`, `role_adoption.py`, `roles/*.yaml` (29 files) | solid, very large investment | Loosely parallels OMP's dual prompt/role systems (see `chudware-prompt-catalog` skill — that skill is literally about this exact YAML-vs-.md prompt-system duality already) |
| Regen / diagnosis / shadow-log reporting | Rebuild failure diagnostics and comparison-log reporting | `regen_diagnosis.py`, `regen_card.py`, `tools/flywheel/shadow_report.py` | live diagnostics and report reader; unused shadow executor removed | No OMP equivalent |
| Buildgraph | DAG planner/executor/rules/schema for build strategy | `buildgraph/` (`planner.py` 17.5K, `executor.py`, `rules.py`, `schema.py`, `mcp_tools.py`) | solid | CAD-domain-specific (feature build ordering), not an OMP concept |

## AGENTS.md constraints (attribution / safety invariants for any port)

From `services/agent/AGENTS.md` (909 bytes, full text read):
1. **Owner note**: "N5e (C++ cutover) / current Python implementation" — signals
   this whole tree is mid-migration toward C++; anything ported here should
   expect a shrinking Python footprint, not a growing one.
2. **The single agent entry is the C++ core** (`agent_run_v2`/`agent_mission_v1`).
   Any OMP feature landed as "Python service" must be consumed FROM the C++
   core (via worker_rpc or a new RPC), not assumed to be the main loop.
3. **`loop.py` is explicitly marked do-not-extend**: "Do not add to this module;
   it shrinks to nothing once those consumers migrate." Any temptation to park
   an OMP port inside `loop.py` is directly against stated intent.
4. Multiple modules carry FROZEN contract references (`contracts/advisor.md`
   §8/§12/§14/§16-§19/§21/§22, `contracts/agent_events.md`, `harness-tiers-spec`).
   These are schema-versioned (e.g. `/1`→`/2` bump precedent already exists) —
   any port that touches these shapes must go through an explicit version bump,
   not silently added fields.
5. No global attribution/license clause specific to third-party code was found
   in AGENTS.md itself, but `compaction.py` and `session_cmds.py` already
   contain explicit provenance comments ("Session compaction engines (omp
   snapcompact text-phase + shake)", "structure LITERALLY from omp
   handoff-document.md") — establishing the house convention: **when porting
   OMP logic, say so in the docstring at the port site.** Any new PORT/SIDECAR
   work should follow this same explicit-attribution convention.
6. Entry point for tests: `pytest services/agent -q`; extremely large existing
   test suite (100+ `test_*.py`, many "mega*_fixes" regression tests) — any
   port must not break this suite; it is the regression safety net for a
   heavily contract-driven, frozen-schema codebase.

## Summary (for the options menu)

CAD's `services/agent` is NOT a thin scaffold — it's a ~150-module, contract-frozen
(§2–§22), heavily tested Python service layer, but its own `AGENTS.md` says
the real agent loop already moved to C++ (`core/agent/run.cpp`); `loop.py` is a
371-line corpse of prompt constants. The live Python surface is `agent_turn.py`
(bounded tool-calling turn, 6.9K lines), `worker_rpc.py` (the actual C++↔Python
RPC bridge), and `agent_events.py` (the `/2` event schema feeding the GUI's
Agent Workflow DAG via `host/src/ui/ai/ai_workspace.cpp` joining parent_id→id).

Two OMP ports already exist and are attributed in-code: `compaction.py` is
explicitly "omp snapcompact text-phase + shake," and `session_cmds.py`'s
handoff prompt is "literally from omp handoff-document.md" — so PORT is a
proven, working pattern here, not hypothetical.

Everything else large and CAD-specific (councils, ladder, org, corporate mode,
delegation, mission_dispatch, guardrails, redaction, buildgraph, playbooks/
pipeline) has NO OMP equivalent — these are CAD/mechanical-engineering-domain
reasoning scaffolds (frozen contracts §12/§16-22) that should be KEPT, not
replaced; OMP has nothing resembling "corporate mode" (2162-line director/
department-head org sim) or "ladder" (fresh-context rung pipeline for
geometry design). Where OMP genuinely has more mature general-purpose
infrastructure — provider breadth/streaming, memory (mnemopi) design, subagent
tree ergonomics, skill triggers — the comparison needs a feature-by-feature
look against the modules named above, but the "swap the loop for OMP's" framing
doesn't apply: there is no live Python loop, and the C++ core is the actual
target any FORK/SIDECAR option must integrate with via `worker_rpc.py` +
`agent_events.py`, respecting the cross-process subagent-registry file-backed
design and the frozen-contract versioning discipline.
